"""Devices — what drives the DUT, and what the mutex locks.

The DUT is what a test *examines*; it holds nothing and locks nothing. A device
is what *reaches* it: the debug probe, the serial line, the CAN adapter, and
whatever comes after them. Everything those three have in common lives here
instead of once per backend:

**One identity, derived from hardware.** ``lock_key`` is the name the machine-wide
mutex locks, and it is built from what the config says about the *unit* — a
``resource_id``, a probe serial, a serial device, an adapter and channel — never
from the name of the config entry. Two entries describing one physical unit
therefore produce one key and collapse to one lock, which is the whole point: a
board reachable as ``dut_uart`` and as ``bootloader_uart`` is still one board.

**One mutex.** ``Device.acquire`` / ``DeviceSet.acquire`` are the only way a
device is taken. Backends no longer each reach for the lock in their own shape,
and a run acquires its whole declared set up front, in a fixed order, all or
nothing (see ``DeviceSet.acquire``).

**One execution shape.** ``Device.execute(service, action, arguments)`` is the
same call for a debugger, a UART and a CAN bus; the device knows which tools it
owns and which argument names its own config entry into them. Arguments stay
free-form so a device kind can grow a tool without a new plumbing path.
"""
from __future__ import annotations

import os
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import ClassVar, Protocol

from agentic_hil.bench import BenchMutex, is_physical_resource
from agentic_hil.types import AgenticHILConfig, CanBusConfig, ComPortConfig, DebuggerConfig, JsonObject

DEVICE_KINDS = ("debugger", "uart", "can")

# Named once so the refusal, the docs and the warning cannot drift apart. This is
# the one identity in this module that is NOT hardware-derived; see
# DebuggerDevice.identity_warning.
UNIDENTIFIED_DEBUGGER_WARNING = (
    "This debugger names no resource_id and no probe_id, so its lock identity falls back to the backend "
    "toolchain rather than to hardware. Two boards driven by the same backend on this machine would share "
    "one lock, and one board reached through two backends would take two. Give the entry a probe_id (or a "
    "resource_id shared by every entry that names this unit) so the lock follows the board."
)


class DeviceError(RuntimeError):
    """A device could not be named, and the result says which name failed."""

    def __init__(self, result: JsonObject):
        super().__init__(str(result.get("summary", "Device could not be resolved.")))
        self.result = result


class ToolCaller(Protocol):
    """What ``Device.execute`` needs from a tool service.

    Deliberately structural: ``agentic_hil.tools`` imports the coordination layer
    that imports this module, so naming AgenticHILToolService here would close a
    cycle for no benefit."""

    config: AgenticHILConfig

    def call(self, name: str, arguments: JsonObject | None = None) -> JsonObject: ...


@dataclass(frozen=True)
class Device:
    """One physical unit that drives the DUT.

    Subclasses supply the identity and the tool vocabulary; the mutex and the
    execution shape are inherited and identical for all of them."""

    kind: ClassVar[str] = "device"
    # The argument name a tool uses to address this device's config entry.
    # None for the debugger: no MCP tool schema carries a probe name, because
    # that surface drives exactly one bound probe.
    scope_field: ClassVar[str | None] = None
    # Tool names this device owns, plus short action aliases onto them. Both are
    # accepted by execute(), so a plan can say "flash" and a caller that already
    # knows the tool can say "flash_firmware".
    tools: ClassVar[frozenset[str]] = frozenset()
    actions: ClassVar[dict[str, str]] = {}

    config_id: str

    @property
    def lock_key(self) -> str:
        raise NotImplementedError

    @property
    def identity_source(self) -> str:
        """Which config field the lock key was derived from."""
        raise NotImplementedError

    @property
    def identity_warning(self) -> str | None:
        """Set when this key cannot tell two physical units apart on one machine."""
        return None

    def as_json(self) -> JsonObject:
        payload: JsonObject = {
            "kind": self.kind,
            "id": self.config_id,
            "lock_key": self.lock_key,
            "identity_source": self.identity_source,
        }
        warning = self.identity_warning
        if warning:
            payload["identity_warning"] = warning
        return payload

    # --- the mutex, in one place instead of per backend ------------------

    def acquire(self, bench: BenchMutex, *, wait_s: float = 0.0) -> bool:
        """Take this device machine-wide. True when this call newly took it."""
        return bool(bench.acquire([self.lock_key], wait_s=wait_s))

    def release(self, bench: BenchMutex) -> None:
        bench.release([self.lock_key])

    def is_held(self, bench: BenchMutex) -> bool:
        return bench.holds(self.lock_key)

    def holder(self, bench: BenchMutex) -> JsonObject | None:
        """The recorded holder of this device, whoever it is."""
        return bench.holder(self.lock_key)

    # --- execution with freely choosable arguments -----------------------

    def resolve_action(self, action: str) -> str | None:
        return self.actions.get(action) or (action if action in self.tools else None)

    def execute(self, service: ToolCaller, action: str, arguments: JsonObject | None = None) -> JsonObject:
        """Run one action on this device, with whatever arguments it takes.

        The device supplies its own config-entry name for the argument that
        addresses it, so a caller cannot ask ``dut_uart`` to write to
        ``bootloader_uart`` by passing a different ``port_id``."""
        tool = self.resolve_action(action)
        if tool is None:
            return {
                "ok": False,
                "tool": action,
                "error_type": "invalid_argument",
                "summary": f"A {self.kind} device has no action named '{action}'.",
                "device": self.as_json(),
                "supported_actions": sorted(set(self.actions) | set(self.tools)),
                "side_effect_committed": False,
                "retry_safe": False,
            }
        payload = dict(arguments or {})
        scope = self.scope_field
        if scope is not None:
            named = payload.get(scope)
            if named is not None and str(named) != self.config_id:
                return {
                    "ok": False,
                    "tool": tool,
                    "error_type": "invalid_argument",
                    "summary": f"This action names {scope} '{named}', but it was asked of device '{self.config_id}'.",
                    "device": self.as_json(),
                    "side_effect_committed": False,
                    "retry_safe": False,
                }
            payload[scope] = self.config_id
        refusal = self.routing_refusal(service, tool)
        if refusal is not None:
            return refusal
        return service.call(tool, payload)

    def routing_refusal(self, service: ToolCaller, tool: str) -> JsonObject | None:
        """Refuse an action the service cannot route to *this* unit."""
        return None


@dataclass(frozen=True)
class DebuggerDevice(Device):
    """A debug probe.

    Identity prefers the operator-declared ``resource_id`` (the only field that
    can say "these two entries are one unit"), then the probe serial. The
    remaining fallbacks name a toolchain, not a board — see
    ``identity_warning``."""

    kind: ClassVar[str] = "debugger"
    tools: ClassVar[frozenset[str]] = frozenset(
        {
            "debugger_info", "debugger_probes_list", "probe_target", "flash_firmware", "reset_target",
            "debug_start_session", "debug_stop_session", "debug_get_session_status", "debug_set_breakpoint",
            "debug_list_breakpoints", "debug_clear_breakpoints", "debug_continue", "debug_halt",
            "debug_get_stop_reason", "debug_symbol_info", "debug_dump_symbol_ihex",
        }
    )
    actions: ClassVar[dict[str, str]] = {
        "info": "debugger_info",
        "probes_list": "debugger_probes_list",
        "probe": "probe_target",
        "flash": "flash_firmware",
        "reset": "reset_target",
        "debug_start": "debug_start_session",
        "debug_stop": "debug_stop_session",
        "set_breakpoint": "debug_set_breakpoint",
        "clear_breakpoints": "debug_clear_breakpoints",
        "continue": "debug_continue",
        "halt": "debug_halt",
        "symbol_info": "debug_symbol_info",
        "dump_memory": "debug_dump_symbol_ihex",
    }

    debugger: DebuggerConfig = field(repr=False)

    @property
    def lock_key(self) -> str:
        if self.debugger.resource_id:
            return f"physical:{os.path.normcase(self.debugger.resource_id)}"
        identity = self.debugger.probe_id or self.debugger.executable or self.debugger.type
        return f"probe:{os.path.normcase(str(identity))}"

    @property
    def identity_source(self) -> str:
        if self.debugger.resource_id:
            return "resource_id"
        if self.debugger.probe_id:
            return "probe_id"
        return "executable" if self.debugger.executable else "type"

    @property
    def identity_warning(self) -> str | None:
        return None if self.identity_source in {"resource_id", "probe_id"} else UNIDENTIFIED_DEBUGGER_WARNING

    def routing_refusal(self, service: ToolCaller, tool: str) -> JsonObject | None:
        """Never drive a probe this service is not bound to.

        No debugger tool schema carries a probe name, so a service bound to
        another entry would run this action on the wrong board silently. A
        multi-probe run gets one service per probe (see the test reactor)."""
        bound = getattr(service.config, "debugger_id", None)
        if bound == self.config_id:
            return None
        return {
            "ok": False,
            "tool": tool,
            "error_type": "not_supported",
            "summary": (
                f"This tool service drives debugger '{bound}', not '{self.config_id}', and no debugger tool "
                "can name a probe in its arguments. Use a service bound to this debugger."
            ),
            "device": self.as_json(),
            "bound_debugger": bound,
            "side_effect_committed": False,
            "side_effect_status": "not_started",
            "retry_safe": False,
        }


@dataclass(frozen=True)
class UartDevice(Device):
    """A configured serial line."""

    kind: ClassVar[str] = "uart"
    scope_field: ClassVar[str | None] = "port_id"
    tools: ClassVar[frozenset[str]] = frozenset({"com_session_start", "com_session_stop", "com_write", "com_read"})
    actions: ClassVar[dict[str, str]] = {
        "open": "com_session_start",
        "close": "com_session_stop",
        "write": "com_write",
        "read": "com_read",
        "uart_open": "com_session_start",
        "uart_close": "com_session_stop",
        "uart_write": "com_write",
        "uart_read": "com_read",
    }

    port: ComPortConfig = field(repr=False)

    @property
    def lock_key(self) -> str:
        if self.port.resource_id:
            return f"physical:{os.path.normcase(self.port.resource_id)}"
        return f"com:{os.path.normcase(self.port.device)}"

    @property
    def identity_source(self) -> str:
        return "resource_id" if self.port.resource_id else "device"


@dataclass(frozen=True)
class CanDevice(Device):
    """A configured CAN adapter and channel."""

    kind: ClassVar[str] = "can"
    scope_field: ClassVar[str | None] = "bus_id"
    tools: ClassVar[frozenset[str]] = frozenset({"can_session_start", "can_session_stop", "can_send", "can_read"})
    actions: ClassVar[dict[str, str]] = {
        "open": "can_session_start",
        "close": "can_session_stop",
        "send": "can_send",
        "read": "can_read",
        "can_open": "can_session_start",
        "can_close": "can_session_stop",
    }

    bus: CanBusConfig = field(repr=False)

    @property
    def lock_key(self) -> str:
        if self.bus.resource_id:
            return f"physical:{os.path.normcase(self.bus.resource_id)}"
        return f"can:{self.bus.adapter}:{os.path.normcase(self.bus.channel)}"

    @property
    def identity_source(self) -> str:
        return "resource_id" if self.bus.resource_id else "channel"


@dataclass(frozen=True)
class DeviceSet:
    """The devices of one run, in the order they are taken.

    Built once by ``of()``, which fixes the order and collapses entries that name
    the same physical unit. Both properties matter for correctness, not for
    tidiness: without the fixed order two runs with overlapping devices can each
    hold what the other needs next, and without the collapse a run would ask for
    one board twice."""

    devices: tuple[Device, ...] = ()

    @classmethod
    def of(cls, devices: Iterable[Device]) -> DeviceSet:
        unique: dict[str, Device] = {}
        for device in devices:
            # First entry wins so the retained device is the one a caller named
            # first; the key is what the mutex uses, and it is identical anyway.
            unique.setdefault(device.lock_key, device)
        return cls(tuple(unique[key] for key in sorted(unique)))

    def __bool__(self) -> bool:
        return bool(self.devices)

    def __len__(self) -> int:
        return len(self.devices)

    def __iter__(self):
        return iter(self.devices)

    @property
    def lock_keys(self) -> list[str]:
        """Sorted and deduplicated, which is the acquisition order."""
        return [device.lock_key for device in self.devices]

    def acquire(self, bench: BenchMutex, *, wait_s: float = 0.0) -> list[str]:
        """Hold every device in this set, or hold none of them.

        The whole set goes into one ``BenchMutex.acquire`` on purpose. That is
        where the mechanism lives: it walks the keys in sorted order under one
        deadline and unwinds everything it already took when one of them is busy.
        Taking them here one device at a time would rebuild both, worse — and a
        partially acquired set is exactly what must never escape."""
        self.require_lockable()
        return bench.acquire(self.lock_keys, wait_s=wait_s)

    def unlockable_keys(self) -> list[str]:
        """Keys the machine-wide mutex would ignore rather than lock.

        BenchMutex locks physical resource names and silently passes over the
        rest, which is right for the host-wide probe enumeration and
        catastrophic for a device: the run would believe it held a board it
        never took. Always empty for the three shipped kinds; this exists so a
        fourth cannot introduce the gap quietly."""
        return [key for key in self.lock_keys if not is_device_lock_key(key)]

    def require_lockable(self) -> None:
        unlockable = self.unlockable_keys()
        if unlockable:
            raise DeviceError(
                {
                    "ok": False,
                    "error_type": "coordination_state_invalid",
                    "summary": "A declared device produced a lock key the machine-wide mutex does not lock; the run would have believed it held a board it never took.",
                    "unlockable_lock_keys": unlockable,
                    "side_effect_committed": False,
                    "retry_safe": False,
                }
            )

    def release(self, bench: BenchMutex) -> None:
        bench.release(self.lock_keys)

    def as_json(self) -> list[JsonObject]:
        return [device.as_json() for device in self.devices]

    def warnings(self) -> list[str]:
        return sorted({warning for device in self.devices if (warning := device.identity_warning)})


def debugger_device(config: AgenticHILConfig, debugger_id: str | None = None) -> DebuggerDevice:
    """The probe a call means: the one named, the one bound, or the only one.

    With several configured and none named there is no board this could mean, and
    picking one is how the wrong board gets flashed."""
    name = debugger_id or config.debugger_id or (next(iter(config.debuggers)) if len(config.debuggers) == 1 else None)
    if name is None or name not in config.debuggers:
        raise DeviceError(
            {
                "ok": False,
                "error_type": "invalid_argument" if name is None else "unknown_device",
                "summary": (
                    "No debugger is named and the project does not configure exactly one, so this run cannot say which probe it means."
                    if name is None
                    else f"The authoritative config declares no debugger named '{name}'."
                ),
                "configured_debuggers": sorted(config.debuggers),
                "side_effect_committed": False,
                "retry_safe": False,
            }
        )
    return DebuggerDevice(config_id=name, debugger=config.debuggers[name])


def uart_device(config: AgenticHILConfig, port_id: str) -> UartDevice:
    port = config.com_ports.get(port_id)
    if port is None:
        raise DeviceError(_unknown_device("uart", port_id, sorted(config.com_ports)))
    return UartDevice(config_id=port_id, port=port)


def can_device(config: AgenticHILConfig, bus_id: str) -> CanDevice:
    bus = config.can_buses.get(bus_id)
    if bus is None:
        raise DeviceError(_unknown_device("can", bus_id, sorted(config.can_buses)))
    return CanDevice(config_id=bus_id, bus=bus)


def _unknown_device(kind: str, name: str, configured: list[str]) -> JsonObject:
    return {
        "ok": False,
        "error_type": "unknown_device",
        "summary": f"The authoritative config declares no {kind} device named '{name}'.",
        "kind": kind,
        "id": name,
        "configured_devices": configured,
        "side_effect_committed": False,
        "retry_safe": False,
    }


def config_devices(config: AgenticHILConfig) -> DeviceSet:
    """Every device the authoritative config declares.

    The DUT (``target``) is deliberately absent: it is what the devices drive,
    not something that drives, so it has nothing to lock."""
    devices: list[Device] = [DebuggerDevice(config_id=name, debugger=entry) for name, entry in config.debuggers.items()]
    devices += [UartDevice(config_id=name, port=entry) for name, entry in config.com_ports.items()]
    devices += [CanDevice(config_id=name, bus=entry) for name, entry in config.can_buses.items()]
    return DeviceSet.of(devices)


def resolve_device(config: AgenticHILConfig, selector: object) -> Device:
    """One device from a caller-supplied ``{"kind": ..., "id": ...}``."""
    if not isinstance(selector, dict):
        raise DeviceError(
            {
                "ok": False,
                "error_type": "invalid_argument",
                "summary": "A device selector must be an object naming a kind and an id.",
                "value": selector,
                "device_kinds": list(DEVICE_KINDS),
                "side_effect_committed": False,
                "retry_safe": False,
            }
        )
    kind = selector.get("kind")
    name = selector.get("id")
    if kind == "debugger":
        return debugger_device(config, str(name) if name is not None else None)
    if kind == "uart":
        return uart_device(config, str(name))
    if kind == "can":
        return can_device(config, str(name))
    raise DeviceError(
        {
            "ok": False,
            "error_type": "invalid_argument",
            "summary": f"'{kind}' is not a device kind. The DUT is not a device: it is what the devices drive.",
            "kind": kind,
            "device_kinds": list(DEVICE_KINDS),
            "side_effect_committed": False,
            "retry_safe": False,
        }
    )


def resolve_devices(config: AgenticHILConfig, selectors: object) -> DeviceSet:
    """Every named device, resolved before anything is locked.

    Resolution is complete before acquisition begins on purpose: a run whose
    third selector names nothing must fail without ever having held the first
    two."""
    if not isinstance(selectors, (list, tuple)):
        raise DeviceError(
            {
                "ok": False,
                "error_type": "invalid_argument",
                "summary": "A run declares its devices as a list of selectors.",
                "value": selectors,
                "side_effect_committed": False,
                "retry_safe": False,
            }
        )
    return DeviceSet.of([resolve_device(config, selector) for selector in selectors])


def lock_keys(resources: Iterable[object]) -> list[str]:
    """Lock keys from a mix of devices and already-derived resource names."""
    keys: list[str] = []
    for item in resources:
        if isinstance(item, Device):
            keys.append(item.lock_key)
        elif isinstance(item, str):
            keys.append(item)
    return keys


def is_device_lock_key(key: str) -> bool:
    """Whether a name locks a physical unit rather than a project or a host."""
    return is_physical_resource(key)
