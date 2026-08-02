from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

JsonObject = dict[str, Any]


@dataclass(frozen=True)
class TargetConfig:
    name: str
    controller: str


@dataclass(frozen=True)
class DebuggerPermissions:
    """What one configured debug probe may do. Every flag is deny-by-default and
    belongs to exactly one probe, so enabling flash on a bring-up board cannot
    grant it on a second board that shares the project config."""

    allow_probe: bool = False
    allow_flash: bool = False
    allow_reset: bool = False
    allow_raw_debugger_commands: bool = False
    allow_mass_erase: bool = False


@dataclass(frozen=True)
class IoPermissions:
    """What one configured COM port or CAN bus may do."""

    allow_read: bool = False
    allow_write: bool = False


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
    resource_id: str | None = None
    permissions: DebuggerPermissions = field(default_factory=DebuggerPermissions)
    # Unset means the project target; a named probe on a second board overrides it.
    target: TargetConfig | None = None


@dataclass(frozen=True)
class DebugInterfaceConfig:
    gdb_executable: str | None
    allowed_symbols: list[str]
    allow_all_symbols: bool
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
    resource_id: str | None = None
    permissions: IoPermissions = field(default_factory=IoPermissions)


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
    resource_id: str | None = None
    permissions: IoPermissions = field(default_factory=IoPermissions)


@dataclass(frozen=True)
class ValidationConfig:
    require_existing_file: bool
    require_allowed_root: bool
    require_allowed_extension: bool
    compute_sha256: bool
    inspect_known_formats: bool


@dataclass(frozen=True)
class ReportsConfig:
    directory: str


@dataclass(frozen=True)
class LogsConfig:
    directory: str


@dataclass(frozen=True)
class AgenticHILConfig:
    config_path: str
    work_dir: str
    workspace_root: str
    state_root: str
    target: TargetConfig
    debuggers: dict[str, DebuggerConfig]
    debug: DebugInterfaceConfig
    artifacts: ArtifactsConfig
    com_ports: dict[str, ComPortConfig]
    can_buses: dict[str, CanBusConfig]
    validation: ValidationConfig
    reports: ReportsConfig
    logs: LogsConfig
    # The probe this config instance drives. A backend, a coordinator resource
    # and a debugger tool all act on exactly one probe, so the selection is made
    # once — by name — and carried here instead of re-derived at each call site.
    # It is bound automatically when the project configures a single debugger;
    # with several, the caller names one and an unbound config refuses debugger
    # work rather than picking a board.
    debugger_id: str | None = None
    debugger: DebuggerConfig | None = None
