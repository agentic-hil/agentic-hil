from __future__ import annotations

import hashlib
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
from yaml.constructor import ConstructorError
from yaml.resolver import BaseResolver

from agentic_hil.types import (
    AgenticHILConfig,
    ArtifactsConfig,
    CanBusConfig,
    ComPortConfig,
    DebuggerConfig,
    DebugInterfaceConfig,
    DeviceConfig,
    JsonObject,
    LogsConfig,
    PermissionsConfig,
    ReportsConfig,
    TargetConfig,
    ValidationConfig,
)

CONFIG_ENV = "AGENTIC_HIL_CONFIG"
CONFIG_SCHEMA_ID = "https://agentic-hil.local/schemas/config.schema.json"
CONFIG_SCHEMA_RESOURCE = "schemas/config.schema.json"
GDB_AUTODETECT_CANDIDATES = ["arm-none-eabi-gdb", "gdb-multiarch", "gdb"]


class ConfigError(Exception):
    def __init__(self, error_type: str, summary: str, details: JsonObject | None = None):
        super().__init__(summary)
        self.error_type = error_type
        self.summary = summary
        self.details = details or {}

    def to_dict(self) -> JsonObject:
        return {"ok": False, "error_type": self.error_type, "summary": self.summary, **self.details}


class UniqueKeyLoader(yaml.SafeLoader):
    pass


def construct_unique_mapping(loader: UniqueKeyLoader, node: yaml.MappingNode, deep: bool = False) -> JsonObject:
    loader.flatten_mapping(node)
    result: JsonObject = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in result
        except TypeError as error:
            raise ConstructorError("while constructing a mapping", node.start_mark, "found an unhashable key", key_node.start_mark) from error
        if duplicate:
            raise ConstructorError("while constructing a mapping", node.start_mark, f"found duplicate key {key!r}", key_node.start_mark)
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


UniqueKeyLoader.add_constructor(BaseResolver.DEFAULT_MAPPING_TAG, construct_unique_mapping)


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


def load_config(config_path: str) -> AgenticHILConfig:
    """Parse one config file. Production entrypoints must use load_authoritative_config."""
    config_file = Path(config_path).expanduser().resolve()
    resolved_config_path = str(config_file)
    if not config_file.exists():
        raise ConfigError(
            "config_file_not_found",
            "Agentic HIL configuration file could not be found.",
            {"path": resolved_config_path},
        )

    try:
        loaded = yaml.load(config_file.read_text(encoding="utf-8"), Loader=UniqueKeyLoader)
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

    workspace_value = str(raw["workspace_root"])
    workspace_requested = Path(workspace_value).expanduser()
    if not workspace_requested.is_absolute():
        raise ConfigError(
            "config_invalid",
            "workspace_root must be an absolute path.",
            {"path": resolved_config_path, "field": "workspace_root", "value": workspace_value},
        )
    workspace = workspace_requested.resolve()
    if not workspace.is_dir():
        raise ConfigError(
            "config_invalid",
            "workspace_root must be an existing directory.",
            {"path": resolved_config_path, "field": "workspace_root", "value": workspace_value},
        )

    target_raw = mapping(raw.get("target"), "target")
    devices_raw = mapping(raw.get("devices"), "devices")
    debugger_raw = mapping(raw.get("debugger"), "debugger")
    debug_raw = mapping(raw.get("debug"), "debug")
    artifacts_raw = mapping(raw.get("artifacts"), "artifacts")
    com_ports_raw = mapping(raw.get("com_ports"), "com_ports")
    can_buses_raw = mapping(raw.get("can_buses"), "can_buses")
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

    com_ports = {name: com_port_config(name, value) for name, value in com_ports_raw.items()}
    devices = {name: device_config(name, value) for name, value in devices_raw.items()}
    validate_devices(devices, com_ports)

    return AgenticHILConfig(
        config_path=resolved_config_path,
        work_dir=str(workspace),
        workspace_root=str(workspace),
        target=target_config(target_raw),
        devices=devices,
        debugger=debugger_config(debugger_raw, debugger_type),
        debug=debug_interface_config(debug_raw),
        artifacts=artifacts_config(artifacts_raw),
        com_ports=com_ports,
        can_buses={name: can_bus_config(name, value) for name, value in can_buses_raw.items()},
        validation=validation_config(validation_raw),
        permissions=permissions_config(permissions_raw),
        reports=reports_config(reports_raw),
        logs=logs_config(logs_raw),
    )


def load_authoritative_config(expected_workspace: str | Path | None = None) -> AgenticHILConfig:
    expected = Path(expected_workspace or Path.cwd()).resolve()
    config_path = os.environ.get(CONFIG_ENV)
    if config_path:
        requested = Path(config_path).expanduser()
        if not requested.is_absolute():
            raise ConfigError(
                "config_invalid",
                f"{CONFIG_ENV} must contain an absolute path.",
                {"path": config_path, "environment_variable": CONFIG_ENV},
            )
    else:
        requested = project_config_path(expected)
    resolved = requested.resolve()
    config = load_config(str(resolved))
    workspace = Path(config.work_dir)
    if is_path_within_frozen(requested, workspace) or is_path_within(resolved, workspace):
        raise ConfigError(
            "config_invalid",
            "The authoritative config must be stored outside the workspace.",
            {"path": str(resolved), "workspace_root": config.workspace_root},
        )
    if workspace != expected:
        raise ConfigError(
            "config_invalid",
            "The authoritative config is bound to a different workspace.",
            {"path": str(resolved), "workspace_root": config.workspace_root, "expected_workspace": str(expected)},
        )
    if not config_path and resolved != project_config_path(workspace):
        raise ConfigError(
            "config_invalid",
            "The automatically discovered config is not canonical for this workspace.",
            {"path": str(resolved), "expected_path": str(project_config_path(workspace)), "workspace_root": config.workspace_root},
        )
    return pin_configured_paths(pin_configured_executables(config))


def project_config_directory() -> Path:
    if os.name == "nt":
        config_root = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    else:
        config_root = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return (config_root / "agentic-hil" / "projects").resolve()


def project_config_path(workspace: str | Path) -> Path:
    resolved = Path(workspace).resolve()
    safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "-", resolved.name).strip(".-") or "workspace"
    identity = os.path.normcase(str(resolved))
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:10]
    return project_config_directory() / f"{safe_name}-{digest}" / "config.yaml"


def pin_configured_paths(config: AgenticHILConfig) -> AgenticHILConfig:
    allowed_roots = [configured_workspace_path(config, root, f"artifacts.allowed_roots[{index}]") for index, root in enumerate(config.artifacts.allowed_roots)]
    upload_directory = configured_workspace_path(config, config.artifacts.upload_directory, "artifacts.upload_directory")
    reports_directory = configured_workspace_path(config, config.reports.directory, "reports.directory")
    logs_directory = configured_workspace_path(config, config.logs.directory, "logs.directory")
    return replace(
        config,
        artifacts=replace(config.artifacts, allowed_roots=allowed_roots, upload_directory=upload_directory),
        reports=replace(config.reports, directory=reports_directory),
        logs=replace(config.logs, directory=logs_directory),
    )


def configured_workspace_path(config: AgenticHILConfig, value: str, field: str) -> str:
    workspace = Path(config.work_dir)
    requested = Path(value)
    lexical = absolute_without_symlinks(requested if requested.is_absolute() else workspace / requested)
    resolved = lexical.resolve()
    if not is_path_within_frozen(lexical, workspace) or not is_path_within(resolved, workspace):
        raise ConfigError(
            "config_invalid",
            "Configured artifact and output paths must remain inside the workspace.",
            {"field": field, "path": str(lexical), "workspace_root": config.workspace_root},
        )
    return str(lexical)


def pin_configured_executables(config: AgenticHILConfig) -> AgenticHILConfig:
    debugger_enabled = any(
        [
            config.permissions.allow_probe,
            config.permissions.allow_flash,
            config.permissions.allow_reset,
        ]
    )
    debugger_candidates = {
        "openocd": ["openocd"],
        "stlink": ["STM32_Programmer_CLI", "STM32_Programmer_CLI.exe"],
        "pyocd": ["pyocd"],
    }[config.debugger.type]
    configured_debugger = config.debugger.executable
    if configured_debugger is None and config.debugger.type == "stlink":
        from agentic_hil.backends.common import find_stm32_programmer_cli

        configured_debugger = find_stm32_programmer_cli()
    debugger_executable = configured_executable(
        config,
        configured_debugger,
        "debugger.executable",
        candidates=debugger_candidates,
        required=debugger_enabled,
    )
    gdb_executable = configured_executable(
        config,
        config.debug.gdb_executable,
        "debug.gdb_executable",
        candidates=GDB_AUTODETECT_CANDIDATES,
    )
    can_buses = {
        name: replace(
            bus,
            executable=configured_executable(
                config,
                bus.executable,
                f"can_buses.{name}.executable",
                workspace_relative=True,
                required=bus.adapter == "process",
            ),
        )
        for name, bus in config.can_buses.items()
    }
    debugger = replace(config.debugger, executable=debugger_executable)
    if debugger_enabled and debugger.type == "openocd":
        debugger = replace(
            debugger,
            interface_cfg=configured_external_file(config, debugger.interface_cfg, "debugger.interface_cfg"),
            target_cfg=configured_external_file(config, debugger.target_cfg, "debugger.target_cfg"),
        )
    return replace(
        config,
        debugger=debugger,
        debug=replace(config.debug, gdb_executable=gdb_executable),
        can_buses=can_buses,
    )


def configured_executable(
    config: AgenticHILConfig,
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
                    "config_invalid",
                    "The config enables hardware access but its executable could not be resolved at startup.",
                    {"field": field},
                )
            return disabled_executable_path(config, field)
    requested = Path(executable)
    has_path_separator = "/" in executable or "\\" in executable
    if requested.is_absolute() or has_path_separator or workspace_relative:
        resolved = Path(resolve_work_path(config, executable))
    else:
        found = shutil.which(executable)
        if found is None:
            raise ConfigError(
                "config_invalid",
                "Configured executable could not be resolved at startup.",
                {"field": field, "value": executable},
            )
        resolved = Path(found).resolve()
    if is_path_within(resolved, Path(config.work_dir)):
        raise ConfigError(
            "config_invalid",
            "Configured executables must be stored outside the workspace.",
            {"field": field, "path": str(resolved), "workspace_root": config.workspace_root},
        )
    if not resolved.is_file():
        raise ConfigError(
            "config_invalid",
            "Configured executable must be an existing regular file.",
            {"field": field, "path": str(resolved)},
        )
    return str(resolved)


def disabled_executable_path(config: AgenticHILConfig, field: str) -> str:
    safe_field = re.sub(r"[^A-Za-z0-9_.-]", "_", field)
    return str(Path(config.config_path).parent / f".agentic-hil-disabled-{safe_field}")


def configured_external_file(config: AgenticHILConfig, value: str, field: str) -> str:
    requested = Path(value)
    if not requested.is_absolute():
        raise ConfigError(
            "config_invalid",
            "Configured debugger files must use absolute paths.",
            {"field": field, "value": value},
        )
    resolved = requested.resolve()
    if is_path_within(resolved, Path(config.work_dir)):
        raise ConfigError(
            "config_invalid",
            "Configured debugger files must be stored outside the workspace.",
            {"field": field, "path": str(resolved), "workspace_root": config.workspace_root},
        )
    if not resolved.is_file():
        raise ConfigError(
            "config_invalid",
            "Configured debugger file must be an existing regular file.",
            {"field": field, "path": str(resolved)},
        )
    return str(resolved)


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


def device_config(name: str, value: Any) -> DeviceConfig:
    raw = mapping(value, f"devices.{name}")
    return DeviceConfig(debugger=bool(raw.get("debugger", False)), uart=optional_string(raw.get("uart")))


def validate_devices(devices: dict[str, DeviceConfig], com_ports: dict[str, ComPortConfig]) -> None:
    debugger_devices = [name for name, device in devices.items() if device.debugger]
    if len(debugger_devices) > 1:
        raise ConfigError(
            "config_invalid",
            "Only one device may use the globally configured debugger.",
            {"field": "devices", "debugger_devices": debugger_devices},
        )
    for name, device in devices.items():
        if device.uart is not None and device.uart not in com_ports:
            raise ConfigError(
                "config_invalid",
                "Device references an unknown UART from com_ports.",
                {"field": f"devices.{name}.uart", "value": device.uart},
            )


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
