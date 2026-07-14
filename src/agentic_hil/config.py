from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from dataclasses import replace
from importlib import resources
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator, SchemaError

from agentic_hil.types import (
    AdapterConfig,
    AgenticHILConfig,
    ArtifactsConfig,
    CanBusConfig,
    ComPortConfig,
    DebuggerConfig,
    DebugInterfaceConfig,
    JsonObject,
    LogsConfig,
    PermissionsConfig,
    ReportsConfig,
    TargetConfig,
    ValidationConfig,
)

DEFAULT_CONFIG_PATH = ".agentic-hil/config.yaml"
CONFIG_SCHEMA_ID = "https://agentic-hil.local/schemas/config.schema.json"
CONFIG_SCHEMA_RESOURCE = "schemas/config.schema.json"
TRUSTED_POLICY_ENV = "AGENTIC_HIL_POLICY"
GDB_AUTODETECT_CANDIDATES = ["arm-none-eabi-gdb", "gdb-multiarch", "gdb"]


class ConfigError(Exception):
    def __init__(self, error_type: str, summary: str, details: JsonObject | None = None):
        super().__init__(summary)
        self.error_type = error_type
        self.summary = summary
        self.details = details or {}

    def to_dict(self) -> JsonObject:
        return {"ok": False, "error_type": self.error_type, "summary": self.summary, **self.details}


def config_schema_text() -> str:
    return resources.files("agentic_hil").joinpath(CONFIG_SCHEMA_RESOURCE).read_text(encoding="utf-8")


def config_schema() -> JsonObject:
    return json.loads(config_schema_text())


def validate_config_schema(raw: JsonObject, config_path: str | None = None) -> None:
    schema = config_schema()
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as error:
        details: JsonObject = {"schema": CONFIG_SCHEMA_RESOURCE, "schema_error": str(error)}
        if config_path is not None:
            details["path"] = config_path
        raise ConfigError("config_schema_invalid", "Bundled Agentic HIL configuration schema is invalid.", details) from error

    errors = sorted(Draft202012Validator(schema).iter_errors(raw), key=lambda item: list(item.absolute_path))
    if errors:
        raise_config_validation_error(errors[0], config_path)


def resolve_config_path(config_path: str | None = None) -> str:
    return config_path or DEFAULT_CONFIG_PATH


def load_config(
    config_path: str | None = None,
    work_dir: str | None = None,
) -> AgenticHILConfig:
    resolved_config_path = resolve_config_path(config_path)
    base = Path(work_dir or Path.cwd()).resolve()
    config_file = Path(resolved_config_path)
    if not config_file.is_absolute():
        config_file = base / config_file
    config_file = config_file.resolve()
    resolved_config_path = str(config_file)
    if not config_file.exists():
        raise ConfigError(
            "config_file_not_found",
            "Agentic HIL configuration file could not be found.",
            {"path": resolved_config_path},
        )

    try:
        loaded = yaml.safe_load(config_file.read_text(encoding="utf-8"))
    except OSError as error:
        raise ConfigError(
            "config_unreadable",
            "Agentic HIL configuration file could not be read.",
            {"path": resolved_config_path, "backend_error": str(error)},
        ) from error
    except UnicodeDecodeError as error:
        raise ConfigError(
            "config_invalid",
            "Agentic HIL configuration file is not valid UTF-8 text.",
            {"path": resolved_config_path},
        ) from error
    except yaml.YAMLError as error:
        raise ConfigError(
            "config_invalid",
            "Agentic HIL configuration file is not valid YAML.",
            {"path": resolved_config_path},
        ) from error

    raw: Any = loaded or {}
    if not isinstance(raw, dict):
        raise ConfigError("config_invalid", "Agentic HIL configuration root must be a mapping.", {"path": resolved_config_path})
    validate_config_schema(raw, resolved_config_path)

    target_raw = mapping(raw.get("target"), "target")
    debugger_raw = mapping(raw.get("debugger"), "debugger")
    debug_raw = mapping(raw.get("debug"), "debug")
    artifacts_raw = mapping(raw.get("artifacts"), "artifacts")
    com_ports_raw = mapping(raw.get("com_ports"), "com_ports")
    can_buses_raw = mapping(raw.get("can_buses"), "can_buses")
    adapters_raw = mapping(raw.get("adapters"), "adapters")
    validation_raw = mapping(raw.get("validation"), "validation")
    permissions_raw = mapping(raw.get("permissions"), "permissions")
    reports_raw = mapping(raw.get("reports"), "reports")
    logs_raw = mapping(raw.get("logs"), "logs")

    debugger_type = str(debugger_raw.get("type", "openocd"))
    if debugger_type not in {"openocd", "stlink", "pyocd"}:
        raise ConfigError(
            "config_invalid",
            "Unsupported debugger.type.",
            {"field": "debugger.type", "value": debugger_type, "allowed_values": ["openocd", "stlink", "pyocd"]},
        )

    return AgenticHILConfig(
        config_path=resolved_config_path,
        work_dir=str(base),
        target=target_config(target_raw),
        debugger=debugger_config(debugger_raw, debugger_type),
        debug=debug_interface_config(debug_raw),
        artifacts=artifacts_config(artifacts_raw),
        com_ports={name: com_port_config(name, value) for name, value in com_ports_raw.items()},
        can_buses={name: can_bus_config(name, value) for name, value in can_buses_raw.items()},
        adapters={name: adapter_config(name, value) for name, value in adapters_raw.items()},
        validation=validation_config(validation_raw),
        permissions=permissions_config(permissions_raw),
        reports=reports_config(reports_raw),
        logs=logs_config(logs_raw),
    )


def load_trusted_policy(policy_path: str | None, work_dir: str | None = None) -> AgenticHILConfig:
    base = Path(work_dir or Path.cwd()).resolve()
    if not policy_path:
        raise ConfigError(
            "trusted_policy_required",
            "MCP hardware access requires a host-managed trusted policy.",
            {"environment_variable": TRUSTED_POLICY_ENV},
        )
    requested = Path(policy_path)
    if not requested.is_absolute():
        raise ConfigError(
            "trusted_policy_invalid",
            "The trusted policy path must be absolute.",
            {"path": policy_path},
        )
    resolved = requested.resolve()
    if is_path_within(resolved, base):
        raise ConfigError(
            "trusted_policy_invalid",
            "The trusted policy must be stored outside the agent-writable workspace.",
            {"path": str(resolved), "work_dir": str(base)},
        )
    policy = load_config(str(resolved), str(base))
    return pin_trusted_paths(pin_trusted_executables(policy))


def pin_trusted_paths(policy: AgenticHILConfig) -> AgenticHILConfig:
    allowed_roots = [trusted_workspace_path(policy, root, f"artifacts.allowed_roots[{index}]") for index, root in enumerate(policy.artifacts.allowed_roots)]
    upload_directory = trusted_workspace_path(policy, policy.artifacts.upload_directory, "artifacts.upload_directory")
    reports_directory = trusted_workspace_path(policy, policy.reports.directory, "reports.directory")
    logs_directory = trusted_workspace_path(policy, policy.logs.directory, "logs.directory")
    return replace(
        policy,
        artifacts=replace(policy.artifacts, allowed_roots=allowed_roots, upload_directory=upload_directory),
        reports=replace(policy.reports, directory=reports_directory),
        logs=replace(policy.logs, directory=logs_directory),
    )


def trusted_workspace_path(policy: AgenticHILConfig, value: str, field: str) -> str:
    workspace = Path(policy.work_dir)
    requested = Path(value)
    lexical = absolute_without_symlinks(requested if requested.is_absolute() else workspace / requested)
    resolved = lexical.resolve()
    if not is_path_within_frozen(lexical, workspace) or not is_path_within(resolved, workspace):
        raise ConfigError(
            "trusted_policy_invalid",
            "Trusted artifact and output paths must remain inside the workspace.",
            {"field": field, "path": str(lexical), "work_dir": policy.work_dir},
        )
    return str(lexical)


def pin_trusted_executables(policy: AgenticHILConfig) -> AgenticHILConfig:
    debugger_enabled = any(
        [
            policy.permissions.allow_probe,
            policy.permissions.allow_flash,
            policy.permissions.allow_reset,
        ]
    )
    debugger_candidates = {
        "openocd": ["openocd"],
        "stlink": ["STM32_Programmer_CLI", "STM32_Programmer_CLI.exe"],
        "pyocd": ["pyocd"],
    }[policy.debugger.type]
    configured_debugger = policy.debugger.executable
    if configured_debugger is None and policy.debugger.type == "stlink":
        from agentic_hil.backends.common import find_stm32_programmer_cli

        configured_debugger = find_stm32_programmer_cli()
    debugger_executable = trusted_executable(
        policy,
        configured_debugger,
        "debugger.executable",
        candidates=debugger_candidates,
        required=debugger_enabled,
    )
    gdb_executable = trusted_executable(
        policy,
        policy.debug.gdb_executable,
        "debug.gdb_executable",
        candidates=GDB_AUTODETECT_CANDIDATES,
    )
    can_buses = {
        name: replace(
            bus,
            executable=trusted_executable(
                policy,
                bus.executable,
                f"can_buses.{name}.executable",
                workspace_relative=True,
                required=bus.adapter == "process",
            ),
        )
        for name, bus in policy.can_buses.items()
    }
    adapters = {
        name: replace(
            adapter,
            executable=trusted_executable(
                policy,
                adapter.executable,
                f"adapters.{name}.executable",
                workspace_relative=True,
                required=True,
            )
            or adapter.executable,
        )
        for name, adapter in policy.adapters.items()
    }
    debugger = replace(policy.debugger, executable=debugger_executable)
    if debugger_enabled and debugger.type == "openocd":
        debugger = replace(
            debugger,
            interface_cfg=trusted_regular_file(policy, debugger.interface_cfg, "debugger.interface_cfg"),
            target_cfg=trusted_regular_file(policy, debugger.target_cfg, "debugger.target_cfg"),
        )
    return replace(
        policy,
        debugger=debugger,
        debug=replace(policy.debug, gdb_executable=gdb_executable),
        can_buses=can_buses,
        adapters=adapters,
    )


def trusted_executable(
    policy: AgenticHILConfig,
    executable: str | None,
    field: str,
    *,
    workspace_relative: bool = False,
    candidates: list[str] | None = None,
    required: bool = False,
) -> str | None:
    if executable is None:
        for candidate in candidates or []:
            found = shutil.which(candidate)
            if found is not None:
                executable = found
                break
        if executable is None:
            if required:
                raise ConfigError(
                    "trusted_policy_invalid",
                    "Trusted policy enables hardware access but its executable could not be resolved at startup.",
                    {"field": field},
                )
            return disabled_executable_path(policy, field)
    requested = Path(executable)
    has_path_separator = "/" in executable or "\\" in executable
    if requested.is_absolute() or has_path_separator or workspace_relative:
        resolved = Path(resolve_work_path(policy, executable))
    else:
        found = shutil.which(executable)
        if found is None:
            raise ConfigError(
                "trusted_policy_invalid",
                "Trusted executable could not be resolved at startup.",
                {"field": field, "value": executable},
            )
        resolved = Path(found).resolve()
    if is_path_within(resolved, Path(policy.work_dir)):
        raise ConfigError(
            "trusted_policy_invalid",
            "Trusted executables must be stored outside the agent-writable workspace.",
            {"field": field, "path": str(resolved), "work_dir": policy.work_dir},
        )
    if not resolved.is_file():
        raise ConfigError(
            "trusted_policy_invalid",
            "Trusted executable must be an existing regular file.",
            {"field": field, "path": str(resolved)},
        )
    return str(resolved)


def disabled_executable_path(policy: AgenticHILConfig, field: str) -> str:
    safe_field = re.sub(r"[^A-Za-z0-9_.-]", "_", field)
    return str(Path(policy.config_path).parent / f".agentic-hil-disabled-{safe_field}")


def trusted_regular_file(policy: AgenticHILConfig, value: str, field: str) -> str:
    requested = Path(value)
    if not requested.is_absolute():
        raise ConfigError(
            "trusted_policy_invalid",
            "Trusted debugger configuration files must use absolute paths.",
            {"field": field, "value": value},
        )
    resolved = requested.resolve()
    if is_path_within(resolved, Path(policy.work_dir)):
        raise ConfigError(
            "trusted_policy_invalid",
            "Trusted debugger configuration files must be stored outside the agent-writable workspace.",
            {"field": field, "path": str(resolved), "work_dir": policy.work_dir},
        )
    if not resolved.is_file():
        raise ConfigError(
            "trusted_policy_invalid",
            "Trusted debugger configuration file must be an existing regular file.",
            {"field": field, "path": str(resolved)},
        )
    return str(resolved)


def apply_trusted_policy(config: AgenticHILConfig, policy: AgenticHILConfig) -> AgenticHILConfig:
    if Path(config.work_dir).resolve() != Path(policy.work_dir).resolve():
        raise ConfigError(
            "trusted_policy_invalid",
            "Project configuration and trusted policy must use the same workspace.",
        )

    allow_all_symbols, allowed_symbols = restrict_symbols(config.debug, policy.debug)
    return AgenticHILConfig(
        config_path=config.config_path,
        work_dir=config.work_dir,
        target=config.target,
        debugger=replace(policy.debugger, timeout_s=min(config.debugger.timeout_s, policy.debugger.timeout_s)),
        debug=DebugInterfaceConfig(
            gdb_executable=policy.debug.gdb_executable,
            allowed_symbols=allowed_symbols,
            allow_all_symbols=allow_all_symbols,
            max_dump_size_bytes=min(config.debug.max_dump_size_bytes, policy.debug.max_dump_size_bytes),
        ),
        artifacts=ArtifactsConfig(
            allowed_roots=intersect_allowed_roots(config, policy),
            upload_directory=policy.artifacts.upload_directory,
            allowed_extensions=intersect_list(config.artifacts.allowed_extensions, policy.artifacts.allowed_extensions),
            max_upload_size_mb=min(config.artifacts.max_upload_size_mb, policy.artifacts.max_upload_size_mb),
            allow_upload=config.artifacts.allow_upload and policy.artifacts.allow_upload,
        ),
        com_ports={name: restrict_com_port(config.com_ports[name], policy.com_ports[name]) for name in config.com_ports if name in policy.com_ports},
        can_buses={name: restrict_can_bus(config.can_buses[name], policy.can_buses[name]) for name in config.can_buses if name in policy.can_buses},
        adapters={
            name: replace(
                policy.adapters[name],
                timeout_s=min(config.adapters[name].timeout_s, policy.adapters[name].timeout_s),
                channels=intersect_list(config.adapters[name].channels, policy.adapters[name].channels),
                faults=intersect_list(config.adapters[name].faults, policy.adapters[name].faults),
            )
            for name in config.adapters
            if name in policy.adapters
        },
        validation=ValidationConfig(
            require_existing_file=config.validation.require_existing_file or policy.validation.require_existing_file,
            require_allowed_root=config.validation.require_allowed_root or policy.validation.require_allowed_root,
            require_allowed_extension=config.validation.require_allowed_extension or policy.validation.require_allowed_extension,
            compute_sha256=config.validation.compute_sha256 or policy.validation.compute_sha256,
            inspect_known_formats=config.validation.inspect_known_formats or policy.validation.inspect_known_formats,
        ),
        permissions=PermissionsConfig(
            allow_probe=config.permissions.allow_probe and policy.permissions.allow_probe,
            allow_flash=config.permissions.allow_flash and policy.permissions.allow_flash,
            allow_reset=config.permissions.allow_reset and policy.permissions.allow_reset,
            allow_com_read=config.permissions.allow_com_read and policy.permissions.allow_com_read,
            allow_com_write=config.permissions.allow_com_write and policy.permissions.allow_com_write,
            allow_can_read=config.permissions.allow_can_read and policy.permissions.allow_can_read,
            allow_can_write=config.permissions.allow_can_write and policy.permissions.allow_can_write,
            allow_adapter_read=config.permissions.allow_adapter_read and policy.permissions.allow_adapter_read,
            allow_adapter_write=config.permissions.allow_adapter_write and policy.permissions.allow_adapter_write,
            allow_raw_debugger_commands=config.permissions.allow_raw_debugger_commands and policy.permissions.allow_raw_debugger_commands,
            allow_mass_erase=config.permissions.allow_mass_erase and policy.permissions.allow_mass_erase,
        ),
        reports=policy.reports,
        logs=policy.logs,
    )


def intersect_allowed_roots(config: AgenticHILConfig, policy: AgenticHILConfig) -> list[str]:
    roots: list[str] = []
    for configured_root in config.artifacts.allowed_roots:
        configured = Path(resolve_work_path(config, configured_root))
        for policy_root in policy.artifacts.allowed_roots:
            trusted = configured_work_path(policy, policy_root)
            if is_path_within_frozen(configured, trusted):
                restricted = configured
            elif is_path_within_frozen(trusted, configured):
                restricted = trusted
            else:
                continue
            value = str(restricted)
            if value not in roots:
                roots.append(value)
    return roots


def restrict_symbols(configured: DebugInterfaceConfig, trusted: DebugInterfaceConfig) -> tuple[bool, list[str]]:
    if configured.allow_all_symbols and trusted.allow_all_symbols:
        return True, []
    if configured.allow_all_symbols:
        return False, list(trusted.allowed_symbols)
    if trusted.allow_all_symbols:
        return False, list(configured.allowed_symbols)
    return False, intersect_list(configured.allowed_symbols, trusted.allowed_symbols)


def restrict_com_port(configured: ComPortConfig, trusted: ComPortConfig) -> ComPortConfig:
    return replace(
        trusted,
        timeout_s=min(configured.timeout_s, trusted.timeout_s),
        write_timeout_s=min(configured.write_timeout_s, trusted.write_timeout_s),
        max_buffer_bytes=min(configured.max_buffer_bytes, trusted.max_buffer_bytes),
        max_write_bytes=min(configured.max_write_bytes, trusted.max_write_bytes),
    )


def restrict_can_bus(configured: CanBusConfig, trusted: CanBusConfig) -> CanBusConfig:
    return replace(
        trusted,
        timeout_s=min(configured.timeout_s, trusted.timeout_s),
        poll_interval_ms=max(configured.poll_interval_ms, trusted.poll_interval_ms),
        receive_own_messages=configured.receive_own_messages and trusted.receive_own_messages,
        listen_only=configured.listen_only or trusted.listen_only,
        max_buffer_frames=min(configured.max_buffer_frames, trusted.max_buffer_frames),
        max_frame_data_bytes=min(configured.max_frame_data_bytes, trusted.max_frame_data_bytes),
    )


def intersect_list(configured: list[str], trusted: list[str]) -> list[str]:
    allowed = set(trusted)
    return [value for value in configured if value in allowed]


def is_path_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def is_path_within_frozen(path: Path, root: Path) -> bool:
    try:
        absolute_without_symlinks(path).relative_to(absolute_without_symlinks(root))
        return True
    except ValueError:
        return False


def absolute_without_symlinks(path: Path) -> Path:
    return Path(os.path.abspath(path))


def configured_work_path(config: AgenticHILConfig, requested_path: str) -> Path:
    requested = Path(requested_path)
    return absolute_without_symlinks(requested if requested.is_absolute() else Path(config.work_dir) / requested)


def safe_configured_directory(config: AgenticHILConfig, requested_path: str, field: str) -> str:
    directory = configured_work_path(config, requested_path)
    if not is_path_within_frozen(directory, Path(config.work_dir)) or directory.resolve() != directory:
        raise ConfigError(
            "unsafe_configured_path",
            "Configured output directory contains a symlink or leaves the workspace.",
            {"field": field, "path": str(directory)},
        )
    directory.mkdir(parents=True, exist_ok=True)
    if directory.resolve() != directory:
        raise ConfigError(
            "unsafe_configured_path",
            "Configured output directory changed while it was being opened.",
            {"field": field, "path": str(directory)},
        )
    return str(directory)


def safe_file_path(file_path: str | Path, workspace: str | Path | None = None) -> Path:
    path = absolute_without_symlinks(Path(file_path))
    if workspace is not None and not is_path_within_frozen(path, Path(workspace)):
        raise ConfigError(
            "unsafe_configured_path",
            "Output file leaves the workspace.",
            {"path": str(path)},
        )
    if path.parent.resolve() != path.parent or path.is_symlink() or (path.exists() and os.lstat(path).st_nlink > 1):
        raise ConfigError(
            "unsafe_configured_path",
            "Output file or its parent directory contains a symlink.",
            {"path": str(path)},
        )
    return path


def atomic_write_bytes(
    file_path: str | Path,
    data: bytes,
    *,
    workspace: str | Path | None = None,
) -> None:
    path = safe_file_path(file_path, workspace)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".agentic-hil-write-", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if path.parent.resolve() != path.parent:
            raise ConfigError("unsafe_configured_path", "Output directory changed while writing.", {"path": str(path)})
        os.replace(temporary_path, path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def atomic_write_text(
    file_path: str | Path,
    text: str,
    *,
    encoding: str = "utf-8",
    workspace: str | Path | None = None,
) -> None:
    atomic_write_bytes(file_path, text.encode(encoding), workspace=workspace)


def safe_append_text(file_path: str | Path, text: str, *, encoding: str = "utf-8") -> None:
    path = safe_file_path(file_path)
    existing = path.read_text(encoding=encoding) if path.exists() else ""
    atomic_write_text(path, existing + text, encoding=encoding)


def safe_write_text(
    config: AgenticHILConfig,
    file_path: str | Path,
    text: str,
    *,
    encoding: str = "utf-8",
) -> None:
    atomic_write_text(file_path, text, encoding=encoding, workspace=config.work_dir)


def resolve_work_path(config: AgenticHILConfig, requested_path: str) -> str:
    requested = Path(requested_path)
    candidate = requested if requested.is_absolute() else Path(config.work_dir) / requested
    return str(candidate.resolve())


def display_path(config: AgenticHILConfig, requested_path: str) -> str:
    requested = Path(requested_path)
    if not requested.is_absolute():
        return to_posix(str(requested))
    try:
        return to_posix(str(requested.resolve().relative_to(Path(config.work_dir).resolve())))
    except ValueError:
        return str(requested_path)


def to_posix(value: str) -> str:
    return value.replace("\\", "/")


def raise_config_validation_error(error: Any, config_path: str | None = None) -> None:
    details: JsonObject = {"field": schema_error_field(error)}
    if config_path is not None:
        details["path"] = config_path

    if error.validator == "additionalProperties":
        details["allowed_fields"] = sorted((error.schema.get("properties") or {}).keys())
        raise ConfigError("config_invalid", "Unknown Agentic HIL configuration field.", details) from error
    if error.validator == "enum":
        details["allowed_values"] = error.validator_value
        details["value"] = error.instance
        raise ConfigError("config_invalid", f"{details['field']} has an unsupported value.", details) from error
    if error.validator == "type":
        details["expected_type"] = error.validator_value
        details["value"] = error.instance
        raise ConfigError("config_invalid", f"{details['field']} has the wrong type.", details) from error

    details["schema_error"] = error.message
    details["value"] = error.instance
    raise ConfigError("config_invalid", error.message or "Configuration validation failed.", details) from error


def schema_error_field(error: Any) -> str:
    parts = [str(part) for part in error.absolute_path]
    if error.validator == "additionalProperties":
        match = re.search(r"'([^']+)' was unexpected", error.message)
        if match:
            parts.append(match.group(1))
    return format_field_path(parts)


def format_field_path(parts: list[str]) -> str:
    result = ""
    for part in parts:
        if part.isdigit():
            result = f"{result}[{part}]" if result else f"[{part}]"
        else:
            result = f"{result}.{part}" if result else part
    return result or "$"


def target_config(raw: JsonObject) -> TargetConfig:
    return TargetConfig(name=str(raw.get("name", "unknown-target")), controller=str(raw.get("controller", "unknown-controller")))


def debugger_config(raw: JsonObject, debugger_type: str) -> DebuggerConfig:
    return DebuggerConfig(
        type=debugger_type,  # type: ignore[arg-type]
        executable=optional_string(raw.get("executable")),
        probe_id=optional_string(raw.get("probe_id")),
        target_type=optional_string(raw.get("target_type")),
        interface=str(raw.get("interface", "SWD")),
        interface_cfg=str(raw.get("interface_cfg", "interface/stlink.cfg")),
        target_cfg=str(raw.get("target_cfg", "target/stm32f4x.cfg")),
        flash_address=optional_string(raw.get("flash_address")),
        timeout_s=float(raw.get("timeout_s", 60)),
    )


def debug_interface_config(raw: JsonObject) -> DebugInterfaceConfig:
    return DebugInterfaceConfig(
        gdb_executable=optional_string(raw.get("gdb_executable")),
        allowed_symbols=string_list(raw.get("allowed_symbols"), []),
        allow_all_symbols=bool(raw.get("allow_all_symbols", False)),
        max_dump_size_bytes=positive_integer_config(raw.get("max_dump_size_bytes"), 1024 * 1024, "debug.max_dump_size_bytes"),
    )


def artifacts_config(raw: JsonObject) -> ArtifactsConfig:
    return ArtifactsConfig(
        allowed_roots=string_list(raw.get("allowed_roots"), ["build"]),
        upload_directory=str(raw.get("upload_directory", ".agentic-hil/artifacts")),
        allowed_extensions=[item.lower() for item in string_list(raw.get("allowed_extensions"), [".elf", ".hex", ".bin"])],
        max_upload_size_mb=int(raw.get("max_upload_size_mb", 64)),
        allow_upload=bool(raw.get("allow_upload", False)),
    )


def com_port_config(name: str, value: Any) -> ComPortConfig:
    raw = mapping(value, f"com_ports.{name}")
    return ComPortConfig(
        device=str(raw["device"]),
        baudrate=int(raw.get("baudrate", 115200)),
        timeout_s=float(raw.get("timeout_s", 0.1)),
        write_timeout_s=float(raw.get("write_timeout_s", 1.0)),
        encoding=str(raw.get("encoding", "utf-8")),
        max_buffer_bytes=int(raw.get("max_buffer_bytes", 65536)),
        max_write_bytes=int(raw.get("max_write_bytes", 4096)),
    )


def can_bus_config(name: str, value: Any) -> CanBusConfig:
    raw = mapping(value, f"can_buses.{name}")
    adapter = str(raw.get("adapter", "peak"))
    if adapter not in {"peak", "socketcan", "process"}:
        raise ConfigError(
            "config_invalid",
            "Unsupported can_buses adapter.",
            {"field": f"can_buses.{name}.adapter", "value": adapter, "allowed_values": ["peak", "socketcan", "process"]},
        )
    fd = bool(raw.get("fd", False))
    return CanBusConfig(
        adapter=adapter,  # type: ignore[arg-type]
        channel=str(raw["channel"]),
        bitrate=int(raw.get("bitrate", 500000)),
        fd=fd,
        data_bitrate=None if raw.get("data_bitrate") is None else int(raw["data_bitrate"]),
        pcanbasic_dll=optional_string(raw.get("pcanbasic_dll")),
        executable=optional_string(raw.get("executable")),
        args=string_list(raw.get("args"), []),
        timeout_s=float(raw.get("timeout_s", 10.0)),
        poll_interval_ms=int(raw.get("poll_interval_ms", 10)),
        receive_own_messages=bool(raw.get("receive_own_messages", False)),
        listen_only=bool(raw.get("listen_only", False)),
        max_buffer_frames=int(raw.get("max_buffer_frames", 1024)),
        max_frame_data_bytes=int(raw.get("max_frame_data_bytes", 64 if fd else 8)),
    )


def adapter_config(name: str, value: Any) -> AdapterConfig:
    raw = mapping(value, f"adapters.{name}")
    return AdapterConfig(
        executable=str(raw["executable"]),
        args=string_list(raw.get("args"), []),
        timeout_s=float(raw.get("timeout_s", 10.0)),
        channels=string_list(raw.get("channels"), []),
        faults=string_list(raw.get("faults"), []),
    )


def validation_config(raw: JsonObject) -> ValidationConfig:
    return ValidationConfig(
        require_existing_file=bool(raw.get("require_existing_file", True)),
        require_allowed_root=bool(raw.get("require_allowed_root", True)),
        require_allowed_extension=bool(raw.get("require_allowed_extension", True)),
        compute_sha256=bool(raw.get("compute_sha256", True)),
        inspect_known_formats=bool(raw.get("inspect_known_formats", True)),
    )


def permissions_config(raw: JsonObject) -> PermissionsConfig:
    default = False
    return PermissionsConfig(
        allow_probe=bool(raw.get("allow_probe", default)),
        allow_flash=bool(raw.get("allow_flash", default)),
        allow_reset=bool(raw.get("allow_reset", default)),
        allow_com_read=bool(raw.get("allow_com_read", default)),
        allow_com_write=bool(raw.get("allow_com_write", default)),
        allow_can_read=bool(raw.get("allow_can_read", default)),
        allow_can_write=bool(raw.get("allow_can_write", default)),
        allow_adapter_read=bool(raw.get("allow_adapter_read", default)),
        allow_adapter_write=bool(raw.get("allow_adapter_write", default)),
        allow_raw_debugger_commands=bool(raw.get("allow_raw_debugger_commands", False)),
        allow_mass_erase=bool(raw.get("allow_mass_erase", False)),
    )


def reports_config(raw: JsonObject) -> ReportsConfig:
    return ReportsConfig(directory=str(raw.get("directory", ".agentic-hil/reports")))


def logs_config(raw: JsonObject) -> LogsConfig:
    return LogsConfig(directory=str(raw.get("directory", ".agentic-hil/logs")))


def mapping(value: Any, field_name: str) -> JsonObject:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError("config_invalid", f"{field_name} must be a mapping.", {"field": field_name})
    return value


def optional_string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text or None


def string_list(value: Any, default_value: list[str]) -> list[str]:
    if value is None:
        return list(default_value)
    if not isinstance(value, list):
        raise ConfigError("config_invalid", "Configuration value must be a list.")
    return [str(item) for item in value]


def positive_integer_config(value: Any, default_value: int, field: str) -> int:
    parsed = int(value if value is not None else default_value)
    if parsed < 1:
        raise ConfigError("config_invalid", f"{field} must be a finite integer >= 1.", {"field": field, "value": value})
    return parsed
