"""Fake power supplies for the class-layer and tool tests (issue #733).

A fake is declared the way a real supply type is: a static record in the
`POWER_SUPPLY_TYPES` shape, registered under a test-only type name before the
configuration loads, and a backend factory registered in
`POWER_SUPPLY_BACKENDS`. Every rule the class layer enforces is then checked
against these fakes, so it holds for any backend and not only for the RS-3005P.

The fakes speak only through the device-neutral interface. They raise only the
catalogue fault types a backend may raise, record every operation the class
layer asks of them, and keep their state on a shared `FakeUnit`, so the state of
the "hardware" survives the one connection per call the class layer opens.

Every identifier is synthetic and built from uuid4. No real bench serial, limit,
console port or baud rate appears here.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml
from conftest import write_authoritative_config

# The test-only supply types. Neither is a real model; both are registered for
# the length of one test with `register_fake_type`.
THRESHOLD_TYPE = "fake-threshold-psu"
TWO_CHANNEL_TYPE = "fake-two-channel-psu"

# Synthetic USB identity of the fakes' port: the pid.codes test vendor and its
# test product, which no shipped adapter enumerates as.
VID = 0x1209
PID = 0x0001
VID_TEXT = "1209"
PID_TEXT = "0001"

# The ranges of the fakes, deliberately unlike the rs3005p record so a class
# layer that reads a range from anywhere but the declared record is caught.
VOLTAGE_MAX = "20"
VOLTAGE_RESOLUTION = "0.01"
CURRENT_MAX = "3"
CURRENT_RESOLUTION = "0.001"

# Limits chosen so the effective limit differs from each source on its own: the
# voltage comes from the channel limit, the current from the supply limit, and
# neither equals the capability range or the present set point.
SUPPLY_VOLTAGE_LIMIT = "12"
SUPPLY_CURRENT_LIMIT = "0.8"
CHANNEL_VOLTAGE_LIMIT = "9.5"
CHANNEL_CURRENT_LIMIT = "1.2"
EFFECTIVE_VOLTAGE = Decimal(CHANNEL_VOLTAGE_LIMIT)
EFFECTIVE_CURRENT = Decimal(SUPPLY_CURRENT_LIMIT)
PRESENT_VOLTAGE = Decimal("5.00")
PRESENT_CURRENT = Decimal("0.250")

SETTERS = frozenset({"set_voltage", "set_current", "set_output"})
WRITES = SETTERS | {"arm_protection"}
# The operations that send nothing to the supply: the static capabilities, the
# connection's transcript and its last error, and closing the connection.
HOUSEKEEPING = frozenset({"capabilities", "evidence", "classify_last_error", "close"})


def record(
    *,
    channels: int = 1,
    measures: bool = True,
    output_readable: bool = True,
    ovp: str = "threshold",
    ocp: str = "threshold",
    ovp_readable: bool = True,
    ocp_readable: bool = True,
) -> dict[str, Any]:
    """A type record in the `POWER_SUPPLY_TYPES` shape."""
    return {
        "channels": channels,
        "ranges": {
            str(number): {
                "voltage": {"max": VOLTAGE_MAX, "resolution": VOLTAGE_RESOLUTION},
                "current": {"max": CURRENT_MAX, "resolution": CURRENT_RESOLUTION},
            }
            for number in range(1, channels + 1)
        },
        "measures": {"voltage": measures, "current": measures},
        "output_readable": output_readable,
        "protection": {"ovp": ovp, "ocp": ocp},
        "protection_readable": {"ovp": ovp_readable, "ocp": ocp_readable},
        "usb_ids": [[VID, PID]],
    }


THRESHOLD_RECORD = record()
TWO_CHANNEL_RECORD = record(channels=2, measures=False, ovp="unsupported", ocp="unsupported", ovp_readable=False, ocp_readable=False)


def fresh_serial() -> str:
    return f"PSU{uuid.uuid4().hex.upper()}"


def fresh_resource() -> str:
    return f"AHIL-PSU-{uuid.uuid4().hex.upper()}"


@dataclass
class ChannelState:
    set_voltage: Decimal = PRESENT_VOLTAGE
    set_current: Decimal = PRESENT_CURRENT
    output_on: bool = False
    ovp: Decimal | None = None
    ocp: Decimal | None = None


@dataclass
class FakeUnit:
    """One fake supply on the bench, shared by every connection the class layer opens to it.

    `calls` logs every operation in order across all connections, each as a
    tuple of the operation name and its arguments. The knobs below make the
    unit misbehave in exactly one documented way each."""

    record: dict[str, Any]
    idn_serial: str
    model: str = "FAKE-PSU"
    firmware: str = "F1"
    accepted: bool = True
    channels: dict[int, ChannelState] = field(default_factory=dict)
    calls: list[tuple[Any, ...]] = field(default_factory=list)
    connections: int = 0
    # Operations that get no reply. A setter or an arm that gets none may have
    # been applied, so its fault says it wrote; a query's fault says it did not.
    no_reply: set[str] = field(default_factory=set)
    # What a setter stores in place of what it was asked: the read-back mismatch.
    skew: dict[str, Decimal] = field(default_factory=dict)
    output_stuck: bool = False
    # How arm_protection misbehaves: "fault" raises the backend's own fault,
    # "mismatch" arms other thresholds, "missing" reads back no threshold even
    # though the record says it can.
    arm: str = "ok"
    # Arbitrary exceptions, by operation: the path a backend bug takes.
    raises: dict[str, BaseException] = field(default_factory=dict)
    # Set by the class layer's sleep between output off and on, when a test records it.
    sleeps: list[float] = field(default_factory=list)
    reads: int = 0

    def __post_init__(self) -> None:
        for number in range(1, int(self.record["channels"]) + 1):
            self.channels.setdefault(number, ChannelState())

    def factory(self, config: Any, supply_id: str) -> FakeBackend:
        self.connections += 1
        return FakeBackend(self, supply_id)

    def operations(self) -> list[tuple[Any, ...]]:
        """Every operation that sends something to the supply."""
        return [call for call in self.calls if call[0] not in HOUSEKEEPING]

    def writes(self) -> list[tuple[Any, ...]]:
        return [call for call in self.calls if call[0] in WRITES]

    def names(self) -> list[str]:
        return [call[0] for call in self.operations()]


class FakeBackend:
    """A `PowerSupplyBackend` over a `FakeUnit`, one per connection."""

    def __init__(self, unit: FakeUnit, supply_id: str) -> None:
        self.unit = unit
        self.supply_id = supply_id
        self.transcript: list[dict[str, Any]] = []
        self.last_error: dict[str, Any] = {}

    # --- helpers ---------------------------------------------------------

    def _log(self, *call: Any) -> None:
        self.unit.calls.append(call)
        name = call[0]
        if name in self.unit.raises:
            raise self.unit.raises[name]
        if name in self.unit.no_reply:
            from agentic_hil.types import PowerSupplyFault

            self.last_error = {"error_type": "power_supply_no_reply", "operation": name}
            raise PowerSupplyFault("power_supply_no_reply", f"The fake supply did not answer {name}.", wrote=name in WRITES)
        self.transcript.append({"command": name, "reply_hex": "", "latency_ms": 1})

    def _channel(self, channel: int) -> ChannelState:
        return self.unit.channels[channel]

    # --- the interface ---------------------------------------------------

    def identity(self) -> Any:
        from agentic_hil.types import SupplyIdentity

        self._log("identity")
        # A careless backend: its transcript carries the serial in clear, so a
        # sweep for it proves the class layer's own masking, not the backend's.
        self.transcript[-1]["reply"] = f"{self.unit.model} {self.unit.firmware} SN:{self.unit.idn_serial}"
        return SupplyIdentity(model=self.unit.model, firmware=self.unit.firmware, accepted=self.unit.accepted, serial=self.unit.idn_serial)

    def capabilities(self) -> dict[str, Any]:
        self.unit.calls.append(("capabilities",))
        return self.unit.record

    def channel_status(self, channel: int) -> Any:
        from agentic_hil.types import ChannelStatus

        self._log("channel_status", channel)
        self.unit.reads += 1
        state = self._channel(channel)
        record = self.unit.record
        measures = record["measures"]
        readable = record["protection_readable"]
        on = state.output_on
        return ChannelStatus(
            set_voltage_v=state.set_voltage,
            set_current_a=state.set_current,
            measured_voltage_v=(state.set_voltage if on else Decimal("0.00")) if measures["voltage"] else None,
            measured_current_a=(Decimal("0.012") if on else Decimal("0.000")) if measures["current"] else None,
            output_on=on if record["output_readable"] else None,
            regulation=("cv" if on else None) if record["output_readable"] else None,
            protection_tripped=False if record["output_readable"] else None,
            ovp_threshold_v=state.ovp if readable["ovp"] else None,
            ocp_threshold_a=state.ocp if readable["ocp"] else None,
            read_at=f"2026-10-22T12:00:{self.unit.reads % 60:02d}Z",
        )

    def set_voltage(self, channel: int, volts: Decimal) -> None:
        self._log("set_voltage", channel, volts)
        self._channel(channel).set_voltage = volts + self.unit.skew.get("set_voltage", Decimal(0))

    def set_current(self, channel: int, amps: Decimal) -> None:
        self._log("set_current", channel, amps)
        self._channel(channel).set_current = amps + self.unit.skew.get("set_current", Decimal(0))

    def set_output(self, channel: int, on: bool) -> None:
        self._log("set_output", channel, on)
        if not self.unit.output_stuck:
            self._channel(channel).output_on = on

    def arm_protection(self, channel: int, ovp_v: Decimal | None, ocp_a: Decimal | None) -> None:
        from agentic_hil.types import PowerSupplyFault

        if self.unit.arm == "no_reply":
            self.unit.no_reply.add("arm_protection")
        self._log("arm_protection", channel, ovp_v, ocp_a)
        if self.unit.arm == "fault":
            raise PowerSupplyFault("power_supply_protection_not_armed", "The fake supply refused the protection thresholds.", wrote=False)
        state = self._channel(channel)
        step = {"ovp": Decimal(VOLTAGE_RESOLUTION), "ocp": Decimal(CURRENT_RESOLUTION)}
        if ovp_v is not None:
            state.ovp = None if self.unit.arm == "missing" else ovp_v - step["ovp"] if self.unit.arm == "mismatch" else ovp_v
        if ocp_a is not None:
            state.ocp = None if self.unit.arm == "missing" else ocp_a - step["ocp"] if self.unit.arm == "mismatch" else ocp_a

    def evidence(self) -> list[dict[str, Any]]:
        self.unit.calls.append(("evidence",))
        return list(self.transcript)

    def classify_last_error(self) -> dict[str, Any]:
        self.unit.calls.append(("classify_last_error",))
        return dict(self.last_error)

    def close(self) -> None:
        self.unit.calls.append(("close",))


class FakeDebugger:
    """The board's probe, standing in so no debugger toolchain is ever started.

    The run teardown may drive it (reset into halt, then a probe); it records
    that and answers as a healthy probe does."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def probe_target(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        self.calls.append("probe_target")
        return {"ok": True, "tool": "probe_target", "target_detected": True}

    def reset_target(self, mode: str = "run") -> dict[str, Any]:
        self.calls.append(f"reset_target:{mode}")
        return {"ok": True, "tool": "reset_target", "mode": mode}

    def close(self) -> None:
        return None

    def sessionless_debug_tools(self) -> frozenset[str]:
        return frozenset()


def register_fake_type(monkeypatch: pytest.MonkeyPatch, name: str, type_record: dict[str, Any]) -> None:
    """Declare a test-only supply type before any configuration naming it loads."""
    from agentic_hil import power_supply

    monkeypatch.setitem(power_supply.POWER_SUPPLY_TYPES, name, type_record)


def bind_fake(monkeypatch: pytest.MonkeyPatch, name: str, unit: FakeUnit) -> None:
    """Make `unit` the backend the class layer gets for supply type `name`.

    The registry is created for the test where the module has none, so a test
    still reaches its tool call and fails on what it checks."""
    from agentic_hil import power_supply

    if not hasattr(power_supply, "POWER_SUPPLY_BACKENDS"):
        monkeypatch.setattr(power_supply, "POWER_SUPPLY_BACKENDS", {}, raising=False)
    monkeypatch.setitem(power_supply.POWER_SUPPLY_BACKENDS, name, unit.factory)


def host_port(device: str, serial: str | None, *, vid: int | None = VID, pid: int | None = PID) -> SimpleNamespace:
    """One port as pyserial's enumeration hands it out."""
    hwid = f"USB VID:PID={vid or 0:04X}:{pid or 0:04X}" + (f" SER={serial}" if serial else "")
    return SimpleNamespace(
        device=device,
        name=device,
        description="synthetic power supply port",
        hwid=hwid,
        manufacturer=None,
        product=None,
        interface=None,
        location=None,
        serial_number=serial,
        vid=vid,
        pid=pid,
    )


def fake_inventory(monkeypatch: pytest.MonkeyPatch, *ports: SimpleNamespace, fails: bool = False) -> list[int]:
    """Make the host enumeration list exactly these ports, and count the enumerations.

    Patched at pyserial's own enumeration, so the inventory code above it runs
    unchanged, and no host port is listed or opened."""
    import serial.tools.list_ports

    asked: list[int] = []

    def comports() -> list[SimpleNamespace]:
        asked.append(len(asked) + 1)
        if fails:
            raise OSError("synthetic enumeration failure")
        return list(ports)

    monkeypatch.setattr(serial.tools.list_ports, "comports", comports)
    return asked


@dataclass
class Bench:
    """A loaded authoritative configuration with one fake supply, and everything a test reads back."""

    workspace: Path
    path: Path
    config: Any
    unit: FakeUnit
    supply_id: str
    port_id: str
    device: str
    port_serial: str | None
    resource: str
    asked: list[int]

    def service(self) -> Any:
        from agentic_hil.tools import AgenticHILToolService

        return AgenticHILToolService(self.config, backend=FakeDebugger())

    def secrets(self) -> tuple[str, ...]:
        return tuple(value for value in (self.port_serial, self.unit.idn_serial) if value)


def supply_entry(
    *,
    supply_type: str = THRESHOLD_TYPE,
    port_id: str = "psu",
    powers: tuple[str, ...] = ("dut",),
    limits: bool = True,
    channels: dict[str, dict[str, Any]] | None = None,
    allow_set: bool = True,
    allow_output: bool = True,
    idn_serial: str | None = None,
    channel_count: int = 1,
) -> dict[str, Any]:
    entry: dict[str, Any] = {"type": supply_type, "com_port": port_id, "powers": list(powers)}
    if limits:
        entry["voltage_limit_v"] = float(SUPPLY_VOLTAGE_LIMIT)
        entry["current_limit_a"] = float(SUPPLY_CURRENT_LIMIT)
        entry["channels"] = {
            str(number): {"voltage_limit_v": float(CHANNEL_VOLTAGE_LIMIT), "current_limit_a": float(CHANNEL_CURRENT_LIMIT)}
            for number in range(1, channel_count + 1)
        }
    if channels is not None:
        entry["channels"] = channels
    if idn_serial is not None:
        entry["idn_serial"] = idn_serial
    entry["permissions"] = {"allow_set": allow_set, "allow_output": allow_output}
    return entry


def make_bench(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    supply_type: str = THRESHOLD_TYPE,
    type_record: dict[str, Any] | None = None,
    unit_changes: dict[str, Any] | None = None,
    supply_changes: dict[str, Any] | None = None,
    port_serial: bool = True,
    idn_serial_in_config: bool = False,
    extra_com_ports: dict[str, Any] | None = None,
    debuggers: dict[str, Any] | None = None,
    inventory: list[SimpleNamespace] | None = None,
    bind: bool = True,
    limits: bool = True,
    drop_limits: tuple[str, ...] = (),
) -> Bench:
    """Write and load a bench with one fake supply `bench` on port `psu`, powering the debugger `dut`.

    The port names its USB ids and a fresh resource id; it names a fresh USB
    serial too unless `port_serial` is false, in which case the supply's
    `idn_serial` is the unit check (C4). The host inventory is faked to show
    exactly that port, unless `inventory` says otherwise.

    `limits=False` writes no limit at all; `drop_limits` removes single ones,
    named as `voltage_limit_v` or `channels.1.current_limit_a`."""
    if type_record is None:
        type_record = TWO_CHANNEL_RECORD if supply_type == TWO_CHANNEL_TYPE else THRESHOLD_RECORD
    register_fake_type(monkeypatch, supply_type, type_record)
    serial = fresh_serial() if port_serial else None
    idn = serial if serial is not None else fresh_serial()
    resource = fresh_resource()
    device = f"COM{200 + uuid.uuid4().int % 50}"
    port: dict[str, Any] = {"device": device, "resource_id": resource, "vid": VID_TEXT, "pid": PID_TEXT}
    if serial is not None:
        port["serial_number"] = serial
    com_ports: dict[str, Any] = {"psu": port, **(extra_com_ports or {})}
    entry = supply_entry(
        supply_type=supply_type,
        channel_count=int(type_record["channels"]),
        limits=limits,
        idn_serial=idn if (idn_serial_in_config or serial is None) else None,
    )
    for dotted in drop_limits:
        *parents, leaf = dotted.split(".")
        holder = entry
        for part in parents:
            holder = holder[part]
        del holder[leaf]
    entry.update(supply_changes or {})
    workspace = (tmp_path / "workspace").resolve()
    kwargs: dict[str, Any] = {}
    if debuggers is not None:
        kwargs["debuggers_yaml"] = yaml.safe_dump({"debuggers": debuggers}, sort_keys=False)
    path = write_authoritative_config(
        workspace,
        monkeypatch,
        config_version=3,
        com_ports_yaml=yaml.safe_dump({"com_ports": com_ports}, sort_keys=False),
        power_supplies_yaml=yaml.safe_dump({"power_supplies": {"bench": entry}}, sort_keys=False),
        **kwargs,
    )
    monkeypatch.chdir(workspace)
    from agentic_hil.config import load_authoritative_config

    config = load_authoritative_config(workspace)
    unit = FakeUnit(**{"record": type_record, "idn_serial": idn, **(unit_changes or {})})
    if bind:
        bind_fake(monkeypatch, supply_type, unit)
    asked = fake_inventory(monkeypatch, *(inventory if inventory is not None else [host_port(device, serial)]))
    return Bench(workspace=workspace, path=path, config=config, unit=unit, supply_id="bench", port_id="psu", device=device, port_serial=serial, resource=resource, asked=asked)


def record_sleep(monkeypatch: pytest.MonkeyPatch, unit: FakeUnit) -> list[float]:
    """Replace the class layer's sleep with one that records, so a cycle takes no wall time."""
    from agentic_hil import power_supply

    def sleep(seconds: float) -> None:
        unit.sleeps.append(seconds)
        unit.calls.append(("sleep", seconds))

    monkeypatch.setattr(power_supply, "sleep", sleep, raising=False)
    return unit.sleeps


def call(bench: Bench, name: str, arguments: dict[str, Any], *, service: Any | None = None) -> dict[str, Any]:
    """One tool call on a fresh service, closed afterwards, unless the test brings its own."""
    own = service is None
    service = service or bench.service()
    try:
        return service.call(name, arguments)
    finally:
        if own:
            service.close()


def text_of(value: Any) -> str:
    import json

    return json.dumps(value, ensure_ascii=False, default=str)


Factory = Callable[[Any, str], Any]
