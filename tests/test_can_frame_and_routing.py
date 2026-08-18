"""Four CAN correctness gaps in one issue (#276), all in the direct python-can path.

(1) `PythonCanAdapterSession` built `can.Message` with no `is_fd`, and nothing
retained the bus's own mode, so an `fd: true` bus kept ACKing its own sends
while writing nothing but classic frames onto the wire.

(2) The payload length check compared only against the configured
`max_frame_data_bytes`, which the schema let an operator set past what the
wire itself allows: a classic (non-FD) bus could be configured to accept an
FD-sized payload, and an FD bus accepted lengths no real DLC code encodes.
CAN FD also carries no remote frame at all, which nothing here refused either.

(3) On Linux, `adapter: peak` with a channel shaped like a SocketCAN netdev
(`can0`, `vcan0`, `slcan0`) passed the channel-shape guard and was then routed
to python-can's `pcan` interface, which cannot open a kernel netdev at all.

(4) A `socketcan` bus's configured `bitrate` was put into `can.Bus(**kwargs)`,
where python-can's SocketCAN backend drops it without a word, and then
reported back next to an open session as if this call had set it.

python-can is faked throughout, in the shape test_can_listen_only.py already
established: none of this needs hardware, a real SocketCAN interface, or a
real PEAK adapter, and none of it can prove what real silicon does. What it
proves is that the mode a bus was configured with reaches the driver, that a
payload the wire cannot carry is refused before anything is sent, that a
channel naming a kernel netdev opens through the backend that can open one,
and that a report never states a bitrate this process did not set.
"""

from __future__ import annotations

import dataclasses
import errno
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import DEFAULT_TEST_PERMISSIONS, write_config

from agentic_hil.can import (
    CAN_FD_FRAME_DATA_LENGTHS,
    CLASSIC_CAN_MAX_FRAME_DATA_BYTES,
    CanBusService,
    CanFrame,
    PythonCanAdapterSession,
    can_adapter_library_missing,
    open_python_can_adapter,
    payload_frame,
    peak_channel_uses_socketcan,
    socketcan_interface_missing,
)
from agentic_hil.config import ConfigError, load_config
from agentic_hil.knowledge import (
    CAN_ADAPTER_LIBRARY_MISSING_ERROR,
    CAN_CLASSIC_FRAME_TOO_LARGE_ERROR,
    CAN_FD_FRAME_LENGTH_INVALID_ERROR,
    CAN_FD_REMOTE_FRAME_ERROR,
    CAN_INTERFACE_NOT_FOUND_ERROR,
)

POSIX_ONLY = pytest.mark.skipif(os.name == "nt", reason="POSIX-only PEAK channel routing")


def can_config(
    tmp_path: Path,
    *,
    adapter: str = "socketcan",
    channel: str = "can0",
    fd: bool = False,
    bitrate: int = 500000,
    max_frame_data_bytes: int | None = None,
):
    lines = [
        "can_buses:\n",
        "  bench:\n",
        f'    adapter: "{adapter}"\n',
        f'    channel: "{channel}"\n',
        f"    fd: {'true' if fd else 'false'}\n",
        f"    bitrate: {bitrate}\n",
    ]
    if max_frame_data_bytes is not None:
        lines.append(f"    max_frame_data_bytes: {max_frame_data_bytes}\n")
    return load_config(str(write_config(tmp_path, can_buses_yaml="".join(lines), permissions=DEFAULT_TEST_PERMISSIONS)))


class FakeBusState:
    ACTIVE = "active"
    PASSIVE = "passive"


class FakeMessage:
    """Stands in for `can.Message`: records exactly what it was built with,
    the same way a fake bus records what it was sent, so a test can read the
    kwargs a real `can.Message(...)` call would have received."""

    def __init__(self, **kwargs: object) -> None:
        self.arbitration_id = kwargs.get("arbitration_id", 0)
        self.is_extended_id = kwargs.get("is_extended_id", True)
        self.is_remote_frame = kwargs.get("is_remote_frame", False)
        self.is_fd = kwargs.get("is_fd", False)
        self.data = kwargs.get("data", b"")
        self.kwargs = kwargs


def fake_can_module(bus_factory) -> SimpleNamespace:
    return SimpleNamespace(Bus=bus_factory, BusState=FakeBusState, Message=FakeMessage, CanInitializationError=type("CanInitializationError", (Exception,), {}))


class RecordingBus:
    """A python-can bus stand-in that only records what it was sent."""

    def __init__(self) -> None:
        self.sent: list[FakeMessage] = []
        self.closed = False

    def send(self, message: FakeMessage, timeout: float | None = None) -> None:
        self.sent.append(message)

    def shutdown(self) -> None:
        self.closed = True


# ---------------------------------------------------------------------------
# (1) The bus's own fd mode reaches every outgoing `can.Message`.


def test_the_session_retains_fd_mode_and_carries_it_into_the_message(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unit level: `PythonCanAdapterSession` alone, with no config or service in
    the way. Fails before the fix, because `send()` never passed `is_fd` at
    all, so `FakeMessage`'s own default (`False`) would stand regardless of
    what the session was told."""
    monkeypatch.setitem(sys.modules, "can", fake_can_module(lambda **kwargs: None))
    bus = RecordingBus()
    session = PythonCanAdapterSession("socketcan", bus, 1.0, is_fd=True)

    result = session.send(CanFrame(id=0x123, extended=False, rtr=False, data=b"\x01\xff"))

    assert result["ok"] is True
    assert len(bus.sent) == 1
    assert bus.sent[0].is_fd is True, "the frame built for an fd session must itself be fd"


def test_a_classic_session_still_sends_is_fd_false(monkeypatch: pytest.MonkeyPatch) -> None:
    """The other direction, so the fix is a real plumbing of the flag and not a
    constant flipped to `True`."""
    monkeypatch.setitem(sys.modules, "can", fake_can_module(lambda **kwargs: None))
    bus = RecordingBus()
    session = PythonCanAdapterSession("socketcan", bus, 1.0, is_fd=False)

    session.send(CanFrame(id=0x123, extended=False, rtr=False, data=b"\x01"))

    assert bus.sent[0].is_fd is False


def test_an_fd_configured_bus_reaches_the_driver_through_the_whole_service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """End to end: config field to driver, with nothing stubbed between, the
    same shape test_can_listen_only.py uses for `listen_only`. The defect was
    invisible precisely because `can_session_start` still reported success and
    `can_send` still reported `ok: true` while writing classic frames."""
    config = can_config(tmp_path, adapter="socketcan", channel="can0", fd=True)
    bus = RecordingBus()
    opened: dict[str, object] = {}
    monkeypatch.setitem(sys.modules, "can", fake_can_module(lambda **kwargs: opened.update(kwargs) or bus))
    service = CanBusService(config)
    try:
        started = service.session_start("bench", clear_rx_queue=False)
        sent = service.send("bench", {"frame_id": "0x123", "data_hex": "01ff"})
    finally:
        service.close()

    assert started["ok"] is True
    assert opened["fd"] is True, "the bus itself opened fd, which was never the broken half"
    assert sent["ok"] is True
    assert len(bus.sent) == 1
    assert bus.sent[0].is_fd is True, "the frame that reached python-can carried the bus's own fd mode"


# ---------------------------------------------------------------------------
# (2) A payload the wire cannot carry is refused before anything is sent.


def test_schema_refuses_a_classic_bus_configured_past_eight_bytes(tmp_path: Path) -> None:
    """The belt: a classic bus cannot even be loaded with a payload ceiling the
    wire cannot carry, closing the hole at the source rather than only at
    send time."""
    with pytest.raises(ConfigError) as excinfo:
        can_config(tmp_path, adapter="socketcan", channel="can0", fd=False, max_frame_data_bytes=64)

    assert excinfo.value.error_type == "config_invalid"
    assert excinfo.value.details["field"] == "can_buses.bench.max_frame_data_bytes"
    json.dumps(excinfo.value.to_dict(), allow_nan=False)


def test_schema_still_allows_an_fd_bus_the_full_sixty_four_bytes(tmp_path: Path) -> None:
    config = can_config(tmp_path, adapter="socketcan", channel="can0", fd=True, max_frame_data_bytes=64)

    assert config.can_buses["bench"].max_frame_data_bytes == 64


def test_a_classic_bus_configured_past_eight_bytes_is_still_capped_at_the_wire(tmp_path: Path) -> None:
    """The suspenders: `payload_frame()` enforces the classic limit on its own
    terms, for a `CanBusConfig` the schema never saw, built directly the way
    the broker's own `dataclasses.replace` does. This is the bug exactly as
    filed: before the fix, the length check compared only against
    `max_frame_data_bytes`, so a classic bus configured with a high enough
    ceiling accepted an FD-sized payload."""
    config = can_config(tmp_path, adapter="socketcan", channel="can0", fd=False)
    bus = dataclasses.replace(config.can_buses["bench"], max_frame_data_bytes=64)

    refused = payload_frame(bus, {"frame_id": 1, "data_hex": "00" * 20})

    assert refused["ok"] is False
    assert refused["error_type"] == CAN_CLASSIC_FRAME_TOO_LARGE_ERROR
    assert refused["bytes_requested"] == 20
    assert refused["classic_max_frame_data_bytes"] == CLASSIC_CAN_MAX_FRAME_DATA_BYTES
    assert refused["remediation"]


def test_an_operators_tighter_classic_ceiling_still_applies_beneath_the_protocol_cap(tmp_path: Path) -> None:
    """Unaffected by the fix: an operator's own budget narrower than eight bytes
    keeps the plain `invalid_argument` it always had, since the protocol check
    only ever tightens what a classic bus accepts, never loosens it."""
    config = can_config(tmp_path, adapter="socketcan", channel="can0", fd=False, max_frame_data_bytes=4)
    bus = config.can_buses["bench"]

    accepted = payload_frame(bus, {"frame_id": 1, "data_hex": "00" * 4})
    refused = payload_frame(bus, {"frame_id": 1, "data_hex": "00" * 5})

    assert accepted["ok"] is True
    assert refused["ok"] is False
    assert refused["error_type"] == "invalid_argument"
    assert refused["max_frame_data_bytes"] == 4


@pytest.mark.parametrize("length", [9, 10, 11, 13, 15, 17, 63])
def test_fd_payload_lengths_outside_the_discrete_set_are_refused(tmp_path: Path, length: int) -> None:
    config = can_config(tmp_path, adapter="socketcan", channel="can0", fd=True)
    bus = config.can_buses["bench"]

    refused = payload_frame(bus, {"frame_id": 1, "data_hex": "00" * length})

    assert refused["ok"] is False
    assert refused["error_type"] == CAN_FD_FRAME_LENGTH_INVALID_ERROR
    assert refused["bytes_requested"] == length
    assert refused["allowed_lengths"] == list(CAN_FD_FRAME_DATA_LENGTHS)
    assert refused["remediation"]


@pytest.mark.parametrize("length", list(CAN_FD_FRAME_DATA_LENGTHS))
def test_every_discrete_fd_length_is_accepted(tmp_path: Path, length: int) -> None:
    config = can_config(tmp_path, adapter="socketcan", channel="can0", fd=True)
    bus = config.can_buses["bench"]

    accepted = payload_frame(bus, {"frame_id": 1, "data_hex": "00" * length})

    assert accepted["ok"] is True
    assert len(accepted["frame"].data) == length


def test_an_operators_tighter_fd_ceiling_still_applies_beneath_the_discrete_check(tmp_path: Path) -> None:
    """12 is a legal FD DLC length, so the protocol check has nothing to say
    about it; an operator's own tighter `max_frame_data_bytes` is what refuses
    it, exactly as it did before this fix."""
    config = can_config(tmp_path, adapter="socketcan", channel="can0", fd=True, max_frame_data_bytes=8)
    bus = config.can_buses["bench"]

    accepted = payload_frame(bus, {"frame_id": 1, "data_hex": "00" * 8})
    refused = payload_frame(bus, {"frame_id": 1, "data_hex": "00" * 12})

    assert accepted["ok"] is True
    assert refused["ok"] is False
    assert refused["error_type"] == "invalid_argument"
    assert refused["max_frame_data_bytes"] == 8


def test_a_remote_frame_is_refused_on_an_fd_bus(tmp_path: Path) -> None:
    config = can_config(tmp_path, adapter="socketcan", channel="can0", fd=True)
    bus = config.can_buses["bench"]

    refused = payload_frame(bus, {"frame_id": 1, "rtr": True, "data_hex": ""})

    assert refused["ok"] is False
    assert refused["error_type"] == CAN_FD_REMOTE_FRAME_ERROR
    assert refused["remediation"]


def test_a_remote_frame_is_unaffected_on_a_classic_bus(tmp_path: Path) -> None:
    config = can_config(tmp_path, adapter="socketcan", channel="can0", fd=False)
    bus = config.can_buses["bench"]

    accepted = payload_frame(bus, {"frame_id": 1, "rtr": True, "data_hex": ""})

    assert accepted["ok"] is True
    assert accepted["frame"].rtr is True


# ---------------------------------------------------------------------------
# (3) A `peak` channel naming a Linux kernel netdev opens through socketcan.


def test_peak_channel_uses_socketcan_is_false_for_pcanbasic_handles() -> None:
    """OS-independent: neither shape is netdev-like, so `os.name` never enters
    the decision for these two."""
    assert peak_channel_uses_socketcan("PCAN_USBBUS1") is False
    assert peak_channel_uses_socketcan("0x51") is False


@POSIX_ONLY
def test_peak_channel_uses_socketcan_is_true_for_kernel_netdev_shapes() -> None:
    assert peak_channel_uses_socketcan("can0") is True
    assert peak_channel_uses_socketcan("vcan1") is True
    assert peak_channel_uses_socketcan("slcan0") is True


@POSIX_ONLY
@pytest.mark.parametrize("channel", ["can0", "vcan0", "slcan0"])
def test_a_peak_bus_naming_a_kernel_netdev_opens_through_socketcan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, channel: str) -> None:
    """The bug exactly as filed: `adapter: peak` with a SocketCAN-style channel
    used to pass the channel-shape guard and then get handed to python-can's
    `pcan` interface, which cannot open a kernel netdev at all. Fails before
    the fix, because `interface` was `"pcan"` for every `peak` bus regardless
    of channel shape."""
    config = can_config(tmp_path, adapter="peak", channel=channel)
    seen_interfaces: list[str] = []

    def bus_factory(**kwargs: object) -> RecordingBus:
        seen_interfaces.append(str(kwargs["interface"]))
        return RecordingBus()

    monkeypatch.setitem(sys.modules, "can", fake_can_module(bus_factory))

    result = open_python_can_adapter(config, "bench", config.can_buses["bench"], False)

    assert result["ok"] is True
    assert seen_interfaces == ["socketcan"]
    assert result["backend"] == "socketcan"
    assert "socketcan" in result["summary"].lower()


@POSIX_ONLY
def test_a_peak_bus_naming_a_pcanbasic_handle_still_opens_through_pcan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The routing is by channel shape, not a blanket switch: a real PCANBasic
    handle (`libpcanbasic` on Linux) is unaffected."""
    config = can_config(tmp_path, adapter="peak", channel="PCAN_USBBUS1")
    seen_interfaces: list[str] = []

    def bus_factory(**kwargs: object) -> RecordingBus:
        seen_interfaces.append(str(kwargs["interface"]))
        return RecordingBus()

    monkeypatch.setitem(sys.modules, "can", fake_can_module(bus_factory))

    result = open_python_can_adapter(config, "bench", config.can_buses["bench"], False)

    assert result["ok"] is True
    assert seen_interfaces == ["pcan"]
    assert result["backend"] == "pcan"
    assert result["summary"] == "CAN adapter opened."


@POSIX_ONLY
def test_an_unrecognised_peak_channel_is_still_refused_before_anything_opens(tmp_path: Path) -> None:
    """The channel-shape guard is unchanged for a channel naming neither shape:
    the netdev arm existing now must not make it start accepting garbage."""
    config = can_config(tmp_path, adapter="peak", channel="not-a-real-channel")

    result = open_python_can_adapter(config, "bench", config.can_buses["bench"], False)

    assert result["ok"] is False
    assert result["error_type"] == "config_invalid"
    assert result["side_effect_committed"] is False


@POSIX_ONLY
def test_a_missing_kernel_netdev_refuses_cleanly_through_the_whole_open(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Once a `peak` bus is routed through `socketcan` for its channel, a
    vanished netdev is exactly the same provable non-event a native
    `socketcan` bus's missing interface is, and must get the same clean
    refusal rather than the markerless quarantine a blanket `adapter == "peak"`
    exclusion used to leave it in."""
    config = can_config(tmp_path, adapter="peak", channel="can0")

    def raising_bus(**kwargs: object) -> RecordingBus:
        raise OSError(errno.ENODEV, "No such device")

    monkeypatch.setitem(sys.modules, "can", fake_can_module(raising_bus))

    result = open_python_can_adapter(config, "bench", config.can_buses["bench"], False)

    assert result["ok"] is False
    assert result["error_type"] == CAN_INTERFACE_NOT_FOUND_ERROR
    assert result["side_effect_committed"] is False
    assert result["retry_safe"] is True


@POSIX_ONLY
def test_socketcan_interface_missing_now_answers_for_a_peak_bus_routed_through_it(tmp_path: Path) -> None:
    """Unit level on the classifier itself, independent of the OS-gated open
    path above, so the routing decision it reads is pinned on its own terms."""
    config = can_config(tmp_path, adapter="peak", channel="PCAN_USBBUS1")
    routed_through_socketcan = dataclasses.replace(config.can_buses["bench"], channel="can0")

    result = socketcan_interface_missing(OSError(errno.ENODEV, "gone"), "bench", routed_through_socketcan)

    assert result is not None
    assert result["error_type"] == CAN_INTERFACE_NOT_FOUND_ERROR


def test_socketcan_interface_missing_still_excludes_a_peak_bus_on_pcanbasic(tmp_path: Path) -> None:
    """OS-independent: a `peak` bus on a real PCANBasic handle keeps the old
    exclusion. ENODEV there cannot be the same provable non-event, because
    `pcan` was never asked to bind a netdev in the first place."""
    config = can_config(tmp_path, adapter="peak", channel="PCAN_USBBUS1")
    bus_config = config.can_buses["bench"]

    assert socketcan_interface_missing(OSError(errno.ENODEV, "gone"), "bench", bus_config) is None


@POSIX_ONLY
def test_library_missing_does_not_blame_pcanbasic_for_a_peak_bus_routed_through_socketcan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A Linux box with SocketCAN but no PCANBasic is the ordinary case
    `peak_channel_uses_socketcan` exists for, so an unrelated socketcan
    failure on such a bus must not be misreported as the vendor library
    being absent: this bus was never going to construct one."""
    monkeypatch.setattr("agentic_hil.can.pcan_basic_library_error", lambda: "PCANBasic.dll could not be loaded (simulated)")
    config = can_config(tmp_path, adapter="peak", channel="can0")
    bus_config = config.can_buses["bench"]

    result = can_adapter_library_missing(RuntimeError("unrelated socketcan failure"), "bench", bus_config)

    assert result is None


def test_library_missing_still_blames_pcanbasic_for_a_real_pcan_bus(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """OS-independent contrast: the same simulated-missing PCANBasic does fire
    for a `peak` bus that actually opens through it."""
    monkeypatch.setattr("agentic_hil.can.pcan_basic_library_error", lambda: "PCANBasic.dll could not be loaded (simulated)")
    config = can_config(tmp_path, adapter="peak", channel="PCAN_USBBUS1")
    bus_config = config.can_buses["bench"]

    result = can_adapter_library_missing(RuntimeError("unrelated"), "bench", bus_config)

    assert result is not None
    assert result["error_type"] == CAN_ADAPTER_LIBRARY_MISSING_ERROR
    assert result["missing_library"] == "PCAN-Basic API"


# ---------------------------------------------------------------------------
# (4) A socketcan bus never claims a bitrate this session did not set.


def test_a_socketcan_session_does_not_claim_its_configured_bitrate_was_applied(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The bug exactly as filed: `bitrate` reached
    `can.Bus(interface="socketcan", ...)`, where python-can drops it
    silently, and the session's own result still reported the configured
    number as if this call had set it."""
    config = can_config(tmp_path, adapter="socketcan", channel="can0", bitrate=250000)
    seen: dict[str, object] = {}
    monkeypatch.setitem(sys.modules, "can", fake_can_module(lambda **kwargs: seen.update(kwargs) or RecordingBus()))
    service = CanBusService(config)
    try:
        started = service.session_start("bench", clear_rx_queue=False)
    finally:
        service.close()

    assert started["ok"] is True
    # python-can was still handed the configured number, which is a harmless
    # no-op on this backend and not itself the defect.
    assert seen["bitrate"] == 250000
    # What changed: the result no longer lets that number stand as a claim
    # that this session set it.
    assert started["bitrate_verified"] is False
    assert "interface" in started["bitrate_note"]


def test_can_buses_list_carries_the_same_honesty_before_any_session_opens(tmp_path: Path) -> None:
    config = can_config(tmp_path, adapter="socketcan", channel="can0")
    service = CanBusService(config)
    try:
        listed = service.list_buses()["buses"]["bench"]
    finally:
        service.close()

    assert listed["bitrate"] == 500000
    assert listed["bitrate_verified"] is False
    assert "bitrate_note" in listed


def test_a_peak_session_carries_no_such_caveat(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`peak` is unaffected: `PcanBus` really does apply `bitrate` through
    `PCANBasic.Initialize`, so nothing here should add a caveat that is not
    true of this adapter."""
    config = can_config(tmp_path, adapter="peak", channel="PCAN_USBBUS1")
    monkeypatch.setitem(sys.modules, "can", fake_can_module(lambda **kwargs: RecordingBus()))
    service = CanBusService(config)
    try:
        started = service.session_start("bench", clear_rx_queue=False)
        listed = service.list_buses()["buses"]["bench"]
    finally:
        service.close()

    assert started["ok"] is True
    assert "bitrate_verified" not in started
    assert "bitrate_note" not in started
    assert "bitrate_verified" not in listed
    assert "bitrate_note" not in listed


@POSIX_ONLY
def test_a_peak_bus_routed_through_socketcan_gets_the_socketcan_bitrate_caveat_too(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Review finding: a `peak` bus routed through `socketcan` for its channel
    must carry the same bitrate honesty caveat a `socketcan` bus does, not the
    caveat-free report `test_a_peak_session_carries_no_such_caveat` proves for a
    genuine PCANBasic handle. The backend that actually opened is socketcan,
    whatever the bus is configured as, and socketcan is the one that silently
    drops `bitrate` -- reporting continued to key off `bus_config.adapter`
    ("peak") rather than the backend that actually opened, so this caveat never
    appeared for this route."""
    config = can_config(tmp_path, adapter="peak", channel="can0", bitrate=250000)
    monkeypatch.setitem(sys.modules, "can", fake_can_module(lambda **kwargs: RecordingBus()))
    service = CanBusService(config)
    try:
        started = service.session_start("bench", clear_rx_queue=False)
        listed = service.list_buses()["buses"]["bench"]
    finally:
        service.close()

    assert started["ok"] is True
    assert started["adapter_result"]["backend"] == "socketcan"
    assert started["bitrate_verified"] is False
    assert "interface" in started["bitrate_note"]
    assert listed["bitrate_verified"] is False
    assert "bitrate_note" in listed
