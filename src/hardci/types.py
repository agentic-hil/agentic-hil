from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

JsonObject = dict[str, Any]


@dataclass(frozen=True)
class TargetConfig:
    name: str
    controller: str


@dataclass(frozen=True)
class DebuggerConfig:
    type: Literal["openocd", "stlink", "pyocd"]
    executable: str | None
    probe_id: str | None
    target_type: str | None
    interface: str
    interface_cfg: str
    target_cfg: str
    flash_address: str | None
    timeout_s: float


@dataclass(frozen=True)
class DebugInterfaceConfig:
    gdb_executable: str | None
    allowed_symbols: list[str]
    max_dump_size_bytes: int


@dataclass(frozen=True)
class ArtifactsConfig:
    allowed_roots: list[str]
    upload_directory: str
    allowed_extensions: list[str]
    max_upload_size_mb: int
    allow_upload: bool


@dataclass(frozen=True)
class ComPortConfig:
    device: str
    baudrate: int
    timeout_s: float
    write_timeout_s: float
    encoding: str
    max_buffer_bytes: int
    max_write_bytes: int


@dataclass(frozen=True)
class CanBusConfig:
    adapter: Literal["peak", "socketcan", "process"]
    channel: str
    bitrate: int
    fd: bool
    data_bitrate: int | None
    pcanbasic_dll: str | None
    executable: str | None
    args: list[str]
    timeout_s: float
    poll_interval_ms: int
    receive_own_messages: bool
    listen_only: bool
    max_buffer_frames: int
    max_frame_data_bytes: int


@dataclass(frozen=True)
class AdapterConfig:
    executable: str
    args: list[str]
    timeout_s: float
    channels: list[str]
    faults: list[str]


@dataclass(frozen=True)
class ValidationConfig:
    require_existing_file: bool
    require_allowed_root: bool
    require_allowed_extension: bool
    compute_sha256: bool
    inspect_known_formats: bool


@dataclass(frozen=True)
class PermissionsConfig:
    allow_probe: bool
    allow_flash: bool
    allow_reset: bool
    allow_com_read: bool
    allow_com_write: bool
    allow_can_read: bool
    allow_can_write: bool
    allow_adapter_read: bool
    allow_adapter_write: bool
    allow_raw_debugger_commands: bool
    allow_mass_erase: bool


@dataclass(frozen=True)
class ReportsConfig:
    directory: str


@dataclass(frozen=True)
class LogsConfig:
    directory: str


@dataclass(frozen=True)
class HardCIConfig:
    config_path: str
    work_dir: str
    target: TargetConfig
    debugger: DebuggerConfig
    debug: DebugInterfaceConfig
    artifacts: ArtifactsConfig
    com_ports: dict[str, ComPortConfig]
    can_buses: dict[str, CanBusConfig]
    adapters: dict[str, AdapterConfig]
    validation: ValidationConfig
    permissions: PermissionsConfig
    reports: ReportsConfig
    logs: LogsConfig
