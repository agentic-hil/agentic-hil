# Copyright 2026 Hannes Pauli
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError


DEFAULT_CONFIG_PATH = Path(".aihil/config.yaml")


class ConfigError(Exception):
    def __init__(self, error_type: str, summary: str, **details: Any) -> None:
        super().__init__(summary)
        self.error_type = error_type
        self.summary = summary
        self.details = details

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "ok": False,
            "error_type": self.error_type,
            "summary": self.summary,
        }
        result.update(self.details)
        return result


@dataclass(frozen=True)
class TargetConfig:
    name: str = "unknown-target"
    controller: str = "unknown-controller"


@dataclass(frozen=True)
class DebuggerConfig:
    type: str = field(default="openocd", metadata={"enum": ["openocd"]})
    executable: str | None = None
    interface_cfg: str = "interface/stlink.cfg"
    target_cfg: str = "target/stm32f4x.cfg"
    timeout_s: float = 60.0


@dataclass(frozen=True)
class ArtifactsConfig:
    allowed_roots: list[str] = field(default_factory=lambda: ["build"])
    upload_directory: str = ".aihil/artifacts"
    allowed_extensions: list[str] = field(default_factory=lambda: [".elf", ".hex", ".bin"])
    max_upload_size_mb: int = 64
    allow_upload: bool = True


@dataclass(frozen=True)
class ComPortConfig:
    device: str
    baudrate: int = 115200
    timeout_s: float = 0.1
    write_timeout_s: float = 1.0
    encoding: str = "utf-8"
    max_buffer_bytes: int = 65536
    max_write_bytes: int = 4096


@dataclass(frozen=True)
class ValidationConfig:
    require_existing_file: bool = True
    require_allowed_root: bool = True
    require_allowed_extension: bool = True
    compute_sha256: bool = True
    inspect_known_formats: bool = True


@dataclass(frozen=True)
class PermissionsConfig:
    allow_probe: bool = True
    allow_flash: bool = True
    allow_reset: bool = True
    allow_com_read: bool = True
    allow_com_write: bool = True
    allow_raw_debugger_commands: bool = False
    allow_mass_erase: bool = False


@dataclass(frozen=True)
class ReportsConfig:
    directory: str = ".aihil/reports"


@dataclass(frozen=True)
class LogsConfig:
    directory: str = ".aihil/logs"


@dataclass(frozen=True)
class AIHILConfig:
    config_path: Path
    work_dir: Path
    target: TargetConfig = field(default_factory=TargetConfig)
    debugger: DebuggerConfig = field(default_factory=DebuggerConfig)
    artifacts: ArtifactsConfig = field(default_factory=ArtifactsConfig)
    com_ports: dict[str, ComPortConfig] = field(default_factory=dict)
    validation: ValidationConfig = field(default_factory=ValidationConfig)
    permissions: PermissionsConfig = field(default_factory=PermissionsConfig)
    reports: ReportsConfig = field(default_factory=ReportsConfig)
    logs: LogsConfig = field(default_factory=LogsConfig)


CONFIG_SCHEMA_ID = "https://aihil.local/schemas/config.schema.json"
CONFIG_SCHEMA_RESOURCE = "schemas/config.schema.json"


def config_schema_text() -> str:
    return resources.files("aihil").joinpath("schemas").joinpath("config.schema.json").read_text(encoding="utf-8")


def config_schema() -> dict[str, Any]:
    return json.loads(config_schema_text())


def validate_config_schema(raw: dict[str, Any], path: str | Path | None = None) -> None:
    schema = config_schema()
    try:
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema).validate(raw)
    except SchemaError as exc:
        details: dict[str, Any] = {
            "schema": CONFIG_SCHEMA_RESOURCE,
            "schema_error": exc.message,
        }
        if path is not None:
            details["path"] = str(path)
        raise ConfigError("config_schema_invalid", "Bundled AI-HIL configuration schema is invalid.", **details) from exc
    except ValidationError as exc:
        _raise_config_validation_error(exc, path)


def resolve_config_path(config_path: str | Path | None = None) -> Path:
    return Path(config_path) if config_path is not None else DEFAULT_CONFIG_PATH


def _raise_config_validation_error(error: ValidationError, path: str | Path | None) -> None:
    field = _schema_error_field(error)
    details: dict[str, Any] = {"field": field}
    if path is not None:
        details["path"] = str(path)

    if error.validator == "additionalProperties":
        details["allowed_fields"] = _schema_allowed_fields(error)
        raise ConfigError("config_invalid", "Unknown AI-HIL configuration field.", **details) from error

    if error.validator == "enum":
        details["allowed_values"] = list(error.validator_value)
        details["value"] = error.instance
        raise ConfigError("config_invalid", f"{field} has an unsupported value.", **details) from error

    if error.validator == "type":
        details["expected_type"] = error.validator_value
        details["value"] = error.instance
        raise ConfigError("config_invalid", f"{field} has the wrong type.", **details) from error

    details["schema_error"] = error.message
    details["value"] = error.instance
    raise ConfigError("config_invalid", error.message, **details) from error


def _schema_error_field(error: ValidationError) -> str:
    parts = list(error.absolute_path)
    if error.validator == "additionalProperties":
        unexpected = _unexpected_schema_property(error)
        if unexpected is not None:
            parts.append(unexpected)
    return _format_field_path(parts)


def _format_field_path(parts: list[Any]) -> str:
    result = ""
    for part in parts:
        if isinstance(part, int):
            result = f"{result}[{part}]" if result else f"[{part}]"
            continue
        result = str(part) if not result else f"{result}.{part}"
    return result or "$"


def _unexpected_schema_property(error: ValidationError) -> str | None:
    if not isinstance(error.instance, dict):
        return None
    allowed = set(error.schema.get("properties", {}))
    for key in error.instance:
        if not isinstance(key, str) or key not in allowed:
            return str(key)
    return None


def _schema_allowed_fields(error: ValidationError) -> list[str]:
    return sorted(str(field_name) for field_name in error.schema.get("properties", {}))


def load_config(config_path: str | Path | None = None, work_dir: str | Path | None = None) -> AIHILConfig:
    path = resolve_config_path(config_path)
    base = Path(work_dir).resolve() if work_dir is not None else Path.cwd().resolve()
    if not path.exists():
        raise ConfigError(
            "config_file_not_found",
            "AI-HIL configuration file could not be found.",
            path=str(path),
        )

    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(
            "config_invalid",
            "AI-HIL configuration file is not valid YAML.",
            path=str(path),
        ) from exc
    raw = loaded if loaded is not None else {}
    if not isinstance(raw, dict):
        raise ConfigError(
            "config_invalid",
            "AI-HIL configuration root must be a mapping.",
            path=str(path),
        )
    validate_config_schema(raw, path)

    target_raw = _mapping(raw.get("target"), "target")
    debugger_raw = _mapping(raw.get("debugger"), "debugger")
    debugger_type = str(debugger_raw.get("type", "openocd"))
    if debugger_type != "openocd":
        raise ConfigError(
            "config_invalid",
            "Unsupported debugger.type.",
            field="debugger.type",
            value=debugger_type,
            allowed_values=["openocd"],
        )
    artifacts_raw = _mapping(raw.get("artifacts"), "artifacts")
    com_ports_raw = _mapping(raw.get("com_ports"), "com_ports")
    validation_raw = _mapping(raw.get("validation"), "validation")
    permissions_raw = _mapping(raw.get("permissions"), "permissions")
    reports_raw = _mapping(raw.get("reports"), "reports")
    logs_raw = _mapping(raw.get("logs"), "logs")

    return AIHILConfig(
        config_path=path,
        work_dir=base,
        target=TargetConfig(
            name=str(target_raw.get("name", "unknown-target")),
            controller=str(target_raw.get("controller", "unknown-controller")),
        ),
        debugger=DebuggerConfig(
            type=debugger_type,
            executable=_optional_string(debugger_raw.get("executable")),
            interface_cfg=str(debugger_raw.get("interface_cfg", "interface/stlink.cfg")),
            target_cfg=str(debugger_raw.get("target_cfg", "target/stm32f4x.cfg")),
            timeout_s=float(debugger_raw.get("timeout_s", 60)),
        ),
        artifacts=ArtifactsConfig(
            allowed_roots=_string_list(artifacts_raw.get("allowed_roots"), ["build"]),
            upload_directory=str(artifacts_raw.get("upload_directory", ".aihil/artifacts")),
            allowed_extensions=[
                ext.lower() for ext in _string_list(artifacts_raw.get("allowed_extensions"), [".elf", ".hex", ".bin"])
            ],
            max_upload_size_mb=int(artifacts_raw.get("max_upload_size_mb", 64)),
            allow_upload=bool(artifacts_raw.get("allow_upload", True)),
        ),
        com_ports={name: _com_port_config(name, value) for name, value in com_ports_raw.items()},
        validation=ValidationConfig(
            require_existing_file=bool(validation_raw.get("require_existing_file", True)),
            require_allowed_root=bool(validation_raw.get("require_allowed_root", True)),
            require_allowed_extension=bool(validation_raw.get("require_allowed_extension", True)),
            compute_sha256=bool(validation_raw.get("compute_sha256", True)),
            inspect_known_formats=bool(validation_raw.get("inspect_known_formats", True)),
        ),
        permissions=PermissionsConfig(
            allow_probe=bool(permissions_raw.get("allow_probe", True)),
            allow_flash=bool(permissions_raw.get("allow_flash", True)),
            allow_reset=bool(permissions_raw.get("allow_reset", True)),
            allow_com_read=bool(permissions_raw.get("allow_com_read", True)),
            allow_com_write=bool(permissions_raw.get("allow_com_write", True)),
            allow_raw_debugger_commands=bool(permissions_raw.get("allow_raw_debugger_commands", False)),
            allow_mass_erase=bool(permissions_raw.get("allow_mass_erase", False)),
        ),
        reports=ReportsConfig(directory=str(reports_raw.get("directory", ".aihil/reports"))),
        logs=LogsConfig(directory=str(logs_raw.get("directory", ".aihil/logs"))),
    )


def resolve_work_path(config: AIHILConfig, path: str | Path) -> Path:
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = config.work_dir / candidate
    return candidate.resolve()


def display_path(config: AIHILConfig, path: str | Path) -> str:
    candidate = Path(path)
    if not candidate.is_absolute():
        return candidate.as_posix()
    try:
        return candidate.resolve().relative_to(config.work_dir).as_posix()
    except ValueError:
        return str(candidate)


def _mapping(value: Any, field_name: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(
            "config_invalid",
            f"{field_name} must be a mapping.",
            field=field_name,
        )
    return value


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None


def _com_port_config(name: str, value: Any) -> ComPortConfig:
    raw = _mapping(value, f"com_ports.{name}")
    return ComPortConfig(
        device=str(raw["device"]),
        baudrate=int(raw.get("baudrate", 115200)),
        timeout_s=float(raw.get("timeout_s", 0.1)),
        write_timeout_s=float(raw.get("write_timeout_s", 1.0)),
        encoding=str(raw.get("encoding", "utf-8")),
        max_buffer_bytes=int(raw.get("max_buffer_bytes", 65536)),
        max_write_bytes=int(raw.get("max_write_bytes", 4096)),
    )


def _string_list(value: Any, default: list[str]) -> list[str]:
    if value is None:
        return list(default)
    if not isinstance(value, list):
        raise ConfigError("config_invalid", "Configuration value must be a list.")
    return [str(item) for item in value]
