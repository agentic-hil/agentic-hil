from __future__ import annotations

import json
import re
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


def load_config(config_path: str | None = None, work_dir: str | None = None) -> AgenticHILConfig:
    resolved_config_path = resolve_config_path(config_path)
    base = Path(work_dir or Path.cwd()).resolve()
    config_file = Path(resolved_config_path)
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
        max_dump_size_bytes=positive_integer_config(raw.get("max_dump_size_bytes"), 1024 * 1024, "debug.max_dump_size_bytes"),
    )


def artifacts_config(raw: JsonObject) -> ArtifactsConfig:
    return ArtifactsConfig(
        allowed_roots=string_list(raw.get("allowed_roots"), ["build"]),
        upload_directory=str(raw.get("upload_directory", ".agentic-hil/artifacts")),
        allowed_extensions=[item.lower() for item in string_list(raw.get("allowed_extensions"), [".elf", ".hex", ".bin"])],
        max_upload_size_mb=int(raw.get("max_upload_size_mb", 64)),
        allow_upload=bool(raw.get("allow_upload", True)),
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
    return PermissionsConfig(
        allow_probe=bool(raw.get("allow_probe", True)),
        allow_flash=bool(raw.get("allow_flash", True)),
        allow_com_read=bool(raw.get("allow_com_read", True)),
        allow_com_write=bool(raw.get("allow_com_write", True)),
        allow_can_read=bool(raw.get("allow_can_read", True)),
        allow_can_write=bool(raw.get("allow_can_write", True)),
        allow_adapter_read=bool(raw.get("allow_adapter_read", True)),
        allow_adapter_write=bool(raw.get("allow_adapter_write", True)),
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
