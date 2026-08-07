from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Literal

JsonObject = dict[str, Any]


def raised_errno(error: BaseException, target: int) -> bool:
    """Whether ``target`` is the OS error number this exception was raised over.

    Walked over ``__cause__`` and ``__context__`` rather than matched against the
    message, because the message is the part a backend is free to reword: on the
    SocketCAN path the number reaches the caller inside a wrapper's cause chain,
    and a classifier that read `"No such device"` would answer differently under a
    non-English libc and identically to a device whose *name* contains the words.
    Both attributes are consulted for the same reason — `OSError.errno` is where
    the kernel's number lives, and a library wrapper such as `can.CanError` copies
    it onto `error_code` when it re-raises.

    A leaf-module helper because both hardware adapters classify this way and
    neither should have to import the other to do it.

    Bounded and cycle-safe: an exception chain is caller-supplied data here.
    """
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if any(getattr(current, field_name, None) == target for field_name in ("errno", "error_code")):
            return True
        current = current.__cause__ or current.__context__
    return False


# --- how a device identity folds case ------------------------------------
#
# Two rules, because a device's identity is built from two different kinds of
# value and only one of them belongs to a filesystem. Both live here rather than
# beside either caller: config.py validates identities and devices.py derives
# them, and config.py sits below devices.py in the import graph (bench imports
# config, devices imports bench), so this leaf module is the one place both can
# agree from.


def fold_hardware_id(value: str) -> str:
    """Fold an opaque hardware identifier, identically on every platform.

    A ``resource_id``, a probe serial and a CAN channel each name a physical
    unit, not a file. ``os.path.normcase`` is therefore the wrong instrument for
    them: it folds case on Windows and does nothing at all on POSIX, so two
    config entries naming one unit in two spellings would collapse to one lock
    on Windows and stay two locks on Linux — the same bench, two different
    exclusivity guarantees, and no error on either.

    Of the two consistent answers, folding is the safe one. An over-collapse
    costs concurrency and announces itself: a run waits, or is told which owner
    holds the board. An under-collapse lets two runs each believe they hold the
    same board, which is the failure the mutex exists to prevent. Nothing is
    merged silently either way — two entries whose ``resource_id`` values differ
    only in case are refused at config load (``config.validate_resource_ids``),
    so an operator who meant them as two units is asked, not overruled."""
    return value.casefold()


def fold_device_path(value: str) -> str:
    """Fold a path-like device value the way its own filesystem does.

    A debugger executable and a serial device name (``COM7``,
    ``/dev/ttyACM0``) are named by the host, and whether case distinguishes two
    of them is the host's rule rather than ours: ``COM7`` and ``com7`` open one
    port on Windows, while ``/dev/ttyACM0`` and ``/dev/ttyacm0`` are two
    different names on Linux. ``os.path.normcase`` is exactly that rule, which
    is why the platform dependence it carries is correct here and wrong in
    ``fold_hardware_id``."""
    return os.path.normcase(value)


def format_usb_id(value: int | None) -> str:
    """A USB vendor or product id in the spelling every tool but pyserial uses.

    pyserial reports them as integers, and that is what a configuration stores —
    but nobody reads `1155` off a board. `lsusb`, Windows' device manager and
    pyserial's own ``hwid`` all print the four-digit hex (``VID:PID=0483:374F``),
    so that is what a refusal has to say out loud, and both spellings travel: the
    payload carries the integer the file holds, the summary carries the hex the
    operator can look up."""
    return "none" if value is None else f"{value:04x}"


# The directory udev fills with one symlink per serial port, named after the
# vendor, the product and the device's own serial number:
#
#     /dev/serial/by-id/usb-STMicroelectronics_STM32_STLink_0669FF...-if02
#
# Deliberately not ``/dev/serial/by-path``. That directory is stable too, but
# against a *different* question: it names the USB topology slot, so it survives
# a reboot and does not survive moving the board to the next socket. The name
# this project wants is the one that follows the board.
SERIAL_BY_ID_DIRECTORY = "/dev/serial/by-id/"


def is_stable_device_name(value: str) -> bool:
    """Whether this serial device name survives replugging.

    ``/dev/ttyACM0`` and ``COM7`` are an enumeration order, not an identity: they
    say which device the host happened to see first, so attaching a second
    adapter can hand one board the name the other had. The kernel name is what an
    operator reads off ``dmesg``, and it is the wrong thing to write into a
    configuration.

    A pure question about the *name*, with no branch on the host asking it. A
    configuration is read on the machine whose paths it names — a POSIX device
    name in a file loaded on Windows names nothing either way — and a rule that
    answered differently per platform could not be pinned by a test that runs on
    both."""
    return value.startswith(SERIAL_BY_ID_DIRECTORY) and len(value) > len(SERIAL_BY_ID_DIRECTORY)


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
    """What one configured debug probe may do beyond reading it. Every flag
    belongs to exactly one probe, so taking flash away from a bring-up board
    leaves a second board that shares the project config untouched.

    The defaults here are ``False`` and stay that way, which is not the same
    question as what a *generated* file says. A generation writes every flag
    true but the two the flash interlock refuses on (hardci-hq#96,
    hardci-hq#107); these defaults decide what an existing file means where
    it says nothing, and turning them over would have widened every configuration
    already on disk at the moment of an update — the one thing a permission
    change may not do.

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
class ProjectPermissions:
    """What may be done to this project — its configuration, its installation,
    its incidents.

    Every flag belongs to the write class of decision 0018: reading the
    configuration needs no grant, writing it does. A generated configuration
    carries all of them true (hardci-hq#96), so an agent can describe the bench,
    narrow it on the operator's request, clear an incident nothing physical was
    part of, and lift the server onto the current release — without anyone
    opening YAML. The dataclass defaults stay ``False`` for the same reason the
    per-device ones do: they say what a file that names nothing means, and
    widening that would widen every configuration already on disk.

    What holds instead of a closed start is the direction. No call writes ``true``
    into a permission, so each of these can go from true to false and none of them
    back, and ``allow_config_permissions_write: false`` is the last permission
    change an agent can make at all. ``agentic-hil init --force`` is what reopens
    the file, and it is a person's command.

    Separate grants rather than one, because one would be a master key. Somebody
    who opens the file so an agent can enter a probe serial must not thereby have
    handed over the permissions block:

    ``allow_config_write``
        regenerate the whole file from hardware discovery
        (``project_config_create``). Contributes no permission value of its own —
        the permissions already on disk are carried over unchanged, so it is not
        a way back to the open skeleton.
    ``allow_config_description_write``
        set named description keys field-wise (``project_config_set``): what the
        bench *is*. Never reaches a ``permissions:`` block.
    ``allow_config_permissions_write``
        take permissions away field-wise, this key included. It hands over the
        taking-away and not the granting: only ``false`` may be written.
    ``allow_recover``
        clear a quarantine over MCP (``hardware_recover``), for the reasons that
        name no hardware contact and for those only (hardci-hq#128). Not about
        the configuration at all, which is why the class docstring above no
        longer says three: it sits here because it is project-scoped rather than
        device-scoped, and because a bench that wants an agent kept away from its
        incidents narrows it in the same block as the rest. What it cannot hand
        over is the physical attestation — that boundary is in the tool, not in
        this flag, and granting this does not move it.

    ``allow_upgrade``
        replace this installation with the newest release over MCP
        (``server_upgrade``, hardci-hq#126). Not about this file at all, and here
        anyway because it is the same class of decision and the same ratchet: an
        agent may close it and can never open it. The tool it gates takes no
        version and can only lift to latest, so closing this key cannot be
        undone by installing a release that reads it differently."""

    allow_config_write: bool = False
    allow_config_description_write: bool = False
    allow_config_permissions_write: bool = False
    allow_recover: bool = False
    allow_upgrade: bool = False


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
    # pyserial raises DTR and RTS on open, and a board that wires either to
    # reset is restarted by the act of listening to it. Both default to what
    # pyserial has always done, so no existing bench changes behaviour; setting
    # them false is how a target is observed provably undisturbed.
    assert_dtr: bool = True
    assert_rts: bool = True
    # The USB serial number of the adapter behind this port — for a Nucleo, the
    # ST-Link serial that `probe_id` also carries. What `probe_id` is to a debug
    # probe: an opaque hardware id, never a path, folding case everywhere. It is
    # what makes `device` merely the way in rather than the identity, so a port
    # that moves from ttyACM0 to ttyACM1 is refused instead of opened as if it
    # were still the same board.
    #
    # Optional, and unset in every configuration written before it existed. Unset
    # means the entry is identified by its kernel name alone, which is the
    # situation `UartDevice.identity_warning` names and `adopt-hardware` fills in.
    serial_number: str | None = None
    # The USB vendor and product ids of that same adapter, as pyserial reports
    # them: integers, and the file may also spell them the way `lsusb` does (see
    # `config.usb_id_config`). They are not a second serial — every ST-Link on
    # earth carries 0483:374f — so they name a *type* rather than a unit, and
    # they are deliberately absent from the lock key for that reason.
    #
    # What they buy is the thing a serial alone cannot: a USB serial number is
    # unique only within a vendor, so `serial_number` matching proves the right
    # unit only once the type agrees too, and an adapter that publishes no serial
    # at all (the cheap CH340 clones) gets a named type check instead of nothing
    # (hardci-hq#124). Optional and independent of each other, like
    # `serial_number` and unset in every configuration written before they
    # existed.
    vid: int | None = None
    pid: int | None = None
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
    # A controller outside listen-only sends dominant ACK bits, so this is not a
    # preference but the claim that observing this bus sends nothing. It is
    # enforced per adapter in `can.LISTEN_ONLY_ENFORCEMENT` — the three obtain
    # the mode three different ways — and an adapter that cannot be held to it
    # refuses the session rather than listening anyway (hardci-hq#103).
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
    # Project-scoped grants. Not per device, because writing this file is not a
    # property of any one board.
    permissions: ProjectPermissions = field(default_factory=ProjectPermissions)
    # The exact bytes this policy was parsed from, hashed, and the moment that
    # happened. A server enforces the configuration it loaded for as long as it
    # runs, so the only way one of its answers can say whether that is still the
    # file on disk is to carry what it read. Empty when the configuration did not
    # come from a file this process read — "unchanged" is then not a claim it may
    # make.
    #
    # `config_digest` is the document the *description* in force came from, and
    # it is the one `config_status` compares against disk. A description reload
    # (agentic_hil.configreload) moves it; nothing else does.
    config_digest: str = ""
    loaded_at: str = ""
    # The document the *permissions* in force came from. Equal to
    # `config_digest` for every configuration that was parsed in one piece, which
    # is every configuration this process loads from a file. They come apart only
    # after a description reload whose file also carried different permissions:
    # the description then comes from the file and the permissions still come
    # from the document parsed at startup, and this is what lets `config_status`
    # say so instead of reporting "unchanged" over a divergence it is hiding.
    # Empty means there is nothing to distinguish and no claim to make.
    permissions_digest: str = ""
    # Whether the description document in force *states* the grants being
    # enforced, which is a different question from where those grants came from.
    # A reload answers it afresh each time by comparing, and never moves
    # `permissions_digest` onto the document it compared against: the permission
    # objects still came from startup, and a later reload that does find a
    # difference would otherwise report an intermediate file as their source and
    # pair it with the startup `loaded_at` — two snapshots read as one.
    permissions_match_description: bool = True
    # When the description in force was last re-read from disk. Empty on a
    # configuration that has never been reloaded, which is the normal case.
    # `loaded_at` stays the startup parse, because that is when the permissions
    # were taken and they are never taken again.
    description_reloaded_at: str = ""

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
