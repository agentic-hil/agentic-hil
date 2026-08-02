from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

JsonObject = dict[str, Any]

# A configuration with no `version:` key was written under the deny-by-default
# read model and is still read under it. Version 2 has no read permission at
# all: probing, COM reads and CAN reads need no grant, and the schema refuses
# the keys that used to express one. Turning the default over without this
# marker would have widened every existing file silently on update, which is the
# one thing a permission change may not do.
LEGACY_CONFIG_VERSION = 1
READ_FREE_CONFIG_VERSION = 2
CURRENT_CONFIG_VERSION = READ_FREE_CONFIG_VERSION


@dataclass(frozen=True)
class TargetConfig:
    name: str
    controller: str


@dataclass(frozen=True)
class DebuggerPermissions:
    """What one configured debug probe may do beyond reading it. Every flag is
    deny-by-default and belongs to exactly one probe, so enabling flash on a
    bring-up board cannot grant it on a second board that shares the project
    config.

    ``allow_probe`` exists only for version 1 files. From version 2 on there is
    no read permission: exclusivity replaced it, and a version 2 config that
    still carries the key is refused rather than reinterpreted."""

    allow_probe: bool = False
    allow_flash: bool = False
    allow_reset: bool = False
    allow_raw_debugger_commands: bool = False
    allow_mass_erase: bool = False


@dataclass(frozen=True)
class IoPermissions:
    """What one configured COM port or CAN bus may do. ``allow_read`` is a
    version 1 field; see DebuggerPermissions."""

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
class RecoveryConfig:
    """How far this bench lets the owner clear its own quarantines.

    ``off`` always defers to the operator. ``readonly`` may reap this owner's
    debugger processes and re-read the probe, which touches nothing physical.
    ``reset_halt`` may additionally drive a reset-into-halt to establish a
    defined state, which is what settles an unconfirmed flash or reset — and is
    a physical action, so a bench whose peripherals react to a reset should say
    ``readonly`` here."""

    auto_recover: str = "reset_halt"
    max_attempts: int = 3
    # False when the config never named a policy, which is the one case an
    # operator has not actually chosen the physical actions being taken.
    auto_recover_explicit: bool = False


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
    recovery: RecoveryConfig = field(default_factory=RecoveryConfig)
    # The probe this config instance drives. A backend, a coordinator resource
    # and a debugger tool all act on exactly one probe, so the selection is made
    # once — by name — and carried here instead of re-derived at each call site.
    # It is bound automatically when the project configures a single debugger;
    # with several, the caller names one and an unbound config refuses debugger
    # work rather than picking a board.
    debugger_id: str | None = None
    debugger: DebuggerConfig | None = None
    # Which permission model this file is read under; see the constants above.
    config_version: int = LEGACY_CONFIG_VERSION

    @property
    def read_free(self) -> bool:
        """Whether reading this bench's devices needs no permission at all.

        Reading can still perturb a target — an SWD attach halts the core, a CAN
        controller outside listen_only sends dominant ACK bits, opening a port
        raises DTR. What answers that is exclusivity, not a grant: whoever holds
        the board cannot be disturbed, and whoever observes while no run holds it
        disturbs nobody."""
        return self.config_version >= READ_FREE_CONFIG_VERSION

    def probe_allowed(self, debugger: DebuggerConfig | None = None) -> bool:
        entry = self.debugger if debugger is None else debugger
        return self.read_free or (entry is not None and entry.permissions.allow_probe)

    def com_read_allowed(self, port: ComPortConfig) -> bool:
        return self.read_free or port.permissions.allow_read

    def can_read_allowed(self, bus: CanBusConfig) -> bool:
        return self.read_free or bus.permissions.allow_read
