# Copyright 2026 Hannes Pauli
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


DEFAULT_CONFIG_PATH = Path(".aihil/config.yaml")
DEFAULT_LISTEN = "127.0.0.1:8732"


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
class ServerConfig:
    listen: str = DEFAULT_LISTEN
    host: str = "127.0.0.1"
    port: int = 8732


@dataclass(frozen=True)
class TargetConfig:
    name: str = "unknown-target"
    controller: str = "unknown-controller"


@dataclass(frozen=True)
class DebuggerConfig:
    type: str = "openocd"
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
    server: ServerConfig = field(default_factory=ServerConfig)
    target: TargetConfig = field(default_factory=TargetConfig)
    debugger: DebuggerConfig = field(default_factory=DebuggerConfig)
    artifacts: ArtifactsConfig = field(default_factory=ArtifactsConfig)
    validation: ValidationConfig = field(default_factory=ValidationConfig)
    permissions: PermissionsConfig = field(default_factory=PermissionsConfig)
    reports: ReportsConfig = field(default_factory=ReportsConfig)
    logs: LogsConfig = field(default_factory=LogsConfig)


def resolve_config_path(config_path: str | Path | None = None) -> Path:
    return Path(config_path) if config_path is not None else DEFAULT_CONFIG_PATH


def parse_listen(value: str | None) -> tuple[str, int, str]:
    listen = value or DEFAULT_LISTEN
    if ":" not in listen:
        raise ConfigError(
            "config_invalid",
            "server.listen must use host:port format.",
            field="server.listen",
            value=listen,
        )
    host, port_text = listen.rsplit(":", 1)
    if not host:
        raise ConfigError(
            "config_invalid",
            "server.listen must include a host.",
            field="server.listen",
            value=listen,
        )
    try:
        port = int(port_text)
    except ValueError as exc:
        raise ConfigError(
            "config_invalid",
            "server.listen port must be an integer.",
            field="server.listen",
            value=listen,
        ) from exc
    if port < 1 or port > 65535:
        raise ConfigError(
            "config_invalid",
            "server.listen port must be between 1 and 65535.",
            field="server.listen",
            value=listen,
        )
    return host, port, listen


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
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(
            "config_invalid",
            "AI-HIL configuration file is not valid YAML.",
            path=str(path),
        ) from exc
    if not isinstance(raw, dict):
        raise ConfigError(
            "config_invalid",
            "AI-HIL configuration root must be a mapping.",
            path=str(path),
        )

    server_raw = _mapping(raw.get("server"), "server")
    host, port, listen = parse_listen(server_raw.get("listen", DEFAULT_LISTEN))

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
    validation_raw = _mapping(raw.get("validation"), "validation")
    permissions_raw = _mapping(raw.get("permissions"), "permissions")
    reports_raw = _mapping(raw.get("reports"), "reports")
    logs_raw = _mapping(raw.get("logs"), "logs")

    return AIHILConfig(
        config_path=path,
        work_dir=base,
        server=ServerConfig(listen=listen, host=host, port=port),
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


def _string_list(value: Any, default: list[str]) -> list[str]:
    if value is None:
        return list(default)
    if not isinstance(value, list):
        raise ConfigError("config_invalid", "Configuration value must be a list.")
    return [str(item) for item in value]
