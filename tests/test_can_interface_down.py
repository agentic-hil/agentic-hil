"""A SocketCAN interface that exists and is down refuses before the socket is opened (#511).

The kernel lets a CAN_RAW socket bind an interface that is administratively
down, and then answers a receive and a send with ENETDOWN. So the product
answered a down link with two wrong things, neither naming the link: with the
default `clear_rx_queue` the pre-session drain failed and the start was refused
as `can_queue_clear_failed`, naming the drain; with `clear_rx_queue: false` the
start reported ok over a link that carries nothing, and the first send failed as
an unknown effect. A down interface is host configuration in the same class as
an absent one, which is refused cleanly as `can_interface_not_found`, so the
decided answer is its own `can_interface_down`: read before the socket is
opened, refused for both values of `clear_rx_queue`, and carrying its own
remediation, whose first step is `sudo ip link set <dev> up`.

The seam these tests drive is the kernel's own publication of the link state:
`agentic_hil.can.SYSFS_NET_CLASS` names the directory the flags file is read
under (`/sys/class/net` on a Linux host), `socketcan_interface_state(channel)`
reads `<root>/<channel>/flags` and answers `"up"`, `"down"` or `None`, and
`socketcan_interface_down(bus_id, bus_config)` is the refusal or `None`,
mirroring `socketcan_interface_missing`. Only a proven down link refuses: a state
that cannot be read (no sysfs entry, a non-Linux host, a channel that is not a
single path component) answers `None` and the session proceeds as it did before.
The signal is the `IFF_UP` bit of the flags file, because `operstate` reads
`unknown` on an up vcan and is not the signal.

Why a second reader of this netdev rather than `socketcan_link_listen_only`,
which already runs `ip -details -json link show dev <channel>` against the same
interface: three reasons, written down here so the next reader does not unify the
two by reflex. Sysfs needs no subprocess, so the state is read on the path that
refuses a session rather than by spawning a child on it. Sysfs answers on a host
with no iproute2 installed, where the `ip` reader can only report that it could
not look, and a non-answer may never become a refusal. And the two are not the
same question: a listen-only ctrlmode is a property of a CAN controller, which a
`vcan` does not have and answers nothing about, while `IFF_UP` is carried by
every netdev the kernel has. The `ip` reader stays exactly where it is, for the
one mode it was written for.

Every string a fake answers here was recorded inside the container image on
2026-09-06 (kernel 6.18 under WSL2, iproute2-6.15.0, python-can 4.6.1,
Python 3.12.14): a down vcan's `flags` reads `0x80` and its `operstate`
`down`; the same vcan after `ip link set up` reads `0x81` and `unknown`;
ENETDOWN is 100 and its strerror is `Network is down`; python-can's send on a
down link renders as `Failed to transmit: Network is down [Error Code 100]`,
which the container tier already pins verbatim over the
real kernel in
`test_a_link_taken_down_under_a_session_fails_the_send_as_an_unknown_effect_that_ends_with_the_call`;
an absent name has no sysfs directory and the bind answers ENODEV
(`No such device`). None of it needs a kernel, which is why this file runs on
Windows; the same behaviour over the real kernel is in
`tests/container/test_can_over_vcan.py`.
"""

from __future__ import annotations

import errno
import json
import os
import re
from pathlib import Path

import pytest
from conftest import write_config
from test_can_listen_only import completed, install_ip, ip_json

import agentic_hil.can as can_module_under_test
from agentic_hil.can import open_python_can_adapter
from agentic_hil.config import load_config
from agentic_hil.knowledge import CAN_INTERFACE_NOT_FOUND_ERROR, LISTEN_ONLY_UNSUPPORTED_ERROR, catalogue_entry
from agentic_hil.tools import AgenticHILToolService

# Distinctive by design: device locks are machine-wide, so a bus id and
# channel shared with another clone's tests would contend across checkouts.
BUS_ID = "enetdown_probe_bus"
CHANNEL = "can511down"
DOWN_ERROR = "can_interface_down"

# Recorded in the image: `/sys/class/net/<vcan>/flags` and `operstate` before
# and after `ip link set up`. IFF_UP is bit 0; 0x80 is IFF_NOARP, which a vcan
# carries whatever its state.
RECORDED_DOWN_FLAGS = "0x80\n"
RECORDED_DOWN_OPERSTATE = "down\n"
RECORDED_UP_FLAGS = "0x81\n"
RECORDED_UP_OPERSTATE = "unknown\n"
# Recorded: the OS number and text the kernel answers a receive or a send on a
# bound socket whose link is down, and python-can's wrapping of the send.
RECORDED_ENETDOWN = 100
RECORDED_ENETDOWN_STRERROR = "Network is down"
RECORDED_SEND_ON_DOWN_LINK = "Failed to transmit: Network is down"
# What python-can renders that exception as, and what the container tier reads
# off the real kernel's ENETDOWN: the library appends the code itself.
RECORDED_SEND_BACKEND_ERROR = f"{RECORDED_SEND_ON_DOWN_LINK} [Error Code {RECORDED_ENETDOWN}]"


def can_config(tmp_path: Path, *, channel: str = CHANNEL, adapter: str = "socketcan", listen_only: bool = False):
    yaml = "".join(
        [
            "can_buses:\n",
            f"  {BUS_ID}:\n",
            f'    adapter: "{adapter}"\n',
            f'    channel: "{channel}"\n',
            f"    listen_only: {'true' if listen_only else 'false'}\n",
        ]
    )
    return load_config(str(write_config(tmp_path, can_buses_yaml=yaml)))


def coordination_record_states(config) -> set[str]:
    records = Path(config.state_root) / "coordination" / "records"
    if not records.is_dir():
        return set()
    return {state for path in records.glob("*.json") if isinstance(state := json.loads(path.read_text(encoding="utf-8")).get("state"), str)}


def real_can_module():
    return pytest.importorskip("can", reason="python-can is an optional extra")


def fake_sysfs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A `/sys/class/net` of this test's own, with no interface in it yet.

    `raising=False` on purpose: the tests below that pin a neighbour which must
    not change are green on the code as it stands, and the seam they isolate
    themselves from does not exist there yet. Where the seam is the subject
    rather than the isolation, the test reads it directly and is red until it
    exists.
    """
    root = tmp_path / "sys" / "class" / "net"
    root.mkdir(parents=True)
    monkeypatch.setattr(can_module_under_test, "SYSFS_NET_CLASS", str(root), raising=False)
    return root


def publish_link(root: Path, channel: str, flags: str, operstate: str) -> None:
    """What the kernel publishes for one netdev, verbatim from the recording."""
    entry = root / channel
    entry.mkdir(exist_ok=True)
    (entry / "flags").write_text(flags, encoding="utf-8")
    (entry / "operstate").write_text(operstate, encoding="utf-8")


class FakeBus:
    """A python-can bus that opened: nothing to read, every send accepted."""

    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs
        self.sent: list[object] = []

    def recv(self, timeout: float | None = None) -> None:
        return None

    def send(self, message: object, timeout: float | None = None) -> None:
        self.sent.append(message)

    def shutdown(self) -> None:
        pass


def a_bus_that_opens(can, opened: list[FakeBus] | None = None):
    def factory(**kwargs: object) -> FakeBus:
        bus = FakeBus(**kwargs)
        if opened is not None:
            opened.append(bus)
        return bus

    return factory


class SocketOpenedOnADownLink(BaseException):
    """The socket was opened over a link the state read had already proven down.

    A `BaseException` on purpose: the open path turns every `Exception` into a
    backend failure, so an `AssertionError` here would be swallowed and reported
    as `can_adapter_open_failed`, and the test would read as a wrong error type
    rather than as the thing that actually went wrong.
    """


def a_bus_that_must_not_be_constructed(**kwargs: object) -> None:
    raise SocketOpenedOnADownLink(f"can.Bus({kwargs})")


def raising_bus(error: BaseException):
    def factory(**kwargs: object):
        raise error

    return factory


# ---------------------------------------------------------------------------
# The state read: the flags file, the IFF_UP bit, and nothing else.


def test_a_link_whose_flags_have_iff_up_clear_reads_down(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = fake_sysfs(tmp_path, monkeypatch)
    publish_link(root, CHANNEL, RECORDED_DOWN_FLAGS, RECORDED_DOWN_OPERSTATE)

    assert can_module_under_test.socketcan_interface_state(CHANNEL) == "down"


def test_a_link_whose_flags_have_iff_up_set_reads_up_whatever_operstate_says(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`operstate` is `unknown` on an up vcan (recorded), so it is not the signal;
    the IFF_UP bit is."""
    root = fake_sysfs(tmp_path, monkeypatch)
    publish_link(root, CHANNEL, RECORDED_UP_FLAGS, RECORDED_UP_OPERSTATE)

    assert can_module_under_test.socketcan_interface_state(CHANNEL) == "up"


@pytest.mark.parametrize(
    ("flags", "why"),
    [
        (None, "no sysfs entry: the interface is absent, or this is not a Linux host"),
        ("", "an empty flags file"),
        ("not-a-number\n", "a flags file that is not a number"),
    ],
)
def test_a_state_that_cannot_be_read_is_no_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, flags: str | None, why: str) -> None:
    """Only a proven down link earns the refusal: an unreadable state is `None`, never `down`."""
    root = fake_sysfs(tmp_path, monkeypatch)
    if flags is not None:
        publish_link(root, CHANNEL, flags, RECORDED_DOWN_OPERSTATE)

    assert can_module_under_test.socketcan_interface_state(CHANNEL) is None, why


def test_a_missing_sysfs_root_reads_no_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The non-Linux host, and a Linux host without sysfs mounted, are the same
    answer: nothing was read, so nothing is refused."""
    monkeypatch.setattr(can_module_under_test, "SYSFS_NET_CLASS", str(tmp_path / "no-such-sys" / "class" / "net"))

    assert can_module_under_test.socketcan_interface_state(CHANNEL) is None


@pytest.mark.parametrize(
    "channel",
    [
        "",
        ".",
        "..",
        f"{CHANNEL}/../{CHANNEL}",
        f"sub/{CHANNEL}",
        f"../net/{CHANNEL}",
        f"/sys/class/net/{CHANNEL}",
        f"{CHANNEL}\\..\\{CHANNEL}",
    ],
)
def test_a_channel_that_is_not_a_single_path_component_reads_no_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, channel: str) -> None:
    """A channel is an interface name, and the state read joins it under a
    directory, so a name that is a path is not read as one.

    `can_buses.<name>.channel` is `str(raw["channel"])` and nothing else, so the
    string that reaches this read is whatever the configuration says. Every case
    here resolves, under the fake root, to the one interface that really is
    published and really is down, and the answer must still be no state at all:
    a name that is not a single component names no netdev the kernel has, so it
    proves nothing about a link, and only a proven down link refuses.
    """
    root = fake_sysfs(tmp_path, monkeypatch)
    publish_link(root, CHANNEL, RECORDED_DOWN_FLAGS, RECORDED_DOWN_OPERSTATE)
    (root / "sub").mkdir()
    publish_link(root / "sub", CHANNEL, RECORDED_DOWN_FLAGS, RECORDED_DOWN_OPERSTATE)

    assert can_module_under_test.socketcan_interface_state(channel) is None


def test_a_channel_that_is_not_a_single_path_component_refuses_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """And so the bus carrying such a channel is not refused as down: it goes on
    to the bind, which is what decides what the name is."""
    config = can_config(tmp_path, channel=f"{CHANNEL}/../{CHANNEL}")
    root = fake_sysfs(tmp_path, monkeypatch)
    publish_link(root, CHANNEL, RECORDED_DOWN_FLAGS, RECORDED_DOWN_OPERSTATE)

    assert can_module_under_test.socketcan_interface_down(BUS_ID, config.can_buses[BUS_ID]) is None


# ---------------------------------------------------------------------------
# The refusal, before the socket is opened.


def assert_refused_as_down(result: dict, *, channel: str = CHANNEL, adapter: str = "socketcan") -> None:
    """Every field the decided refusal carries.

    On the two `ip link` spellings, since the assertions below insist on one of
    them: #511 writes the command as `ip link set up <name>` and the catalogue's
    `can_interface_not_found` entry, which this refusal's neighbour, already
    writes `sudo ip link set <dev> up`. Both are valid iproute2. The catalogue's
    spelling wins, because it is the one an operator already meets on this
    surface and two entries that disagree about the same command read as two
    different commands. An implementation that copied the issue's word order is
    not wrong about anything but this, and this comment is why it fails here.
    """
    assert result["ok"] is False, result
    assert result["tool"] == "can_session_start", result
    assert result["bus_id"] == BUS_ID, result
    assert result["adapter"] == adapter, result
    assert result["error_type"] == DOWN_ERROR, result
    assert result["field"] == f"can_buses.{BUS_ID}.channel", result
    assert result["channel"] == channel, result
    assert result["interface_state"] == "down", result
    assert result["target_contacted"] is False, result
    assert result["side_effect_committed"] is False, result
    assert result["side_effect_status"] == "not_started", result
    assert result["retry_safe"] is True, result
    assert channel in result["summary"] and "down" in result["summary"], result
    assert re.search(r"sudo ip link set \S+ up", result["remediation"][0]), result["remediation"]
    assert f"ip link set {channel} up" in json.dumps(result), result
    assert any("recover --confirm-safe-state" in step for step in result["do_not"]), result


def test_a_link_that_is_down_is_refused_before_the_socket_is_opened(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    can = real_can_module()
    config = can_config(tmp_path)
    root = fake_sysfs(tmp_path, monkeypatch)
    publish_link(root, CHANNEL, RECORDED_DOWN_FLAGS, RECORDED_DOWN_OPERSTATE)
    monkeypatch.setattr(can, "Bus", a_bus_that_must_not_be_constructed)

    result = open_python_can_adapter(config, BUS_ID, config.can_buses[BUS_ID], False)

    assert_refused_as_down(result)


def test_the_refusal_helper_mirrors_the_not_found_one(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`socketcan_interface_down` answers the refusal for a down link and `None`
    for an up one and for an unreadable one, and it reads no python-can."""
    config = can_config(tmp_path)
    bus_config = config.can_buses[BUS_ID]
    root = fake_sysfs(tmp_path, monkeypatch)

    assert can_module_under_test.socketcan_interface_down(BUS_ID, bus_config) is None, "an unreadable state refused"
    publish_link(root, CHANNEL, RECORDED_UP_FLAGS, RECORDED_UP_OPERSTATE)
    assert can_module_under_test.socketcan_interface_down(BUS_ID, bus_config) is None, "an up link refused"
    publish_link(root, CHANNEL, RECORDED_DOWN_FLAGS, RECORDED_DOWN_OPERSTATE)
    refused = can_module_under_test.socketcan_interface_down(BUS_ID, bus_config)
    assert refused is not None
    assert_refused_as_down(refused)


def test_a_link_that_is_up_opens_the_ordinary_way(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    can = real_can_module()
    config = can_config(tmp_path)
    root = fake_sysfs(tmp_path, monkeypatch)
    publish_link(root, CHANNEL, RECORDED_UP_FLAGS, RECORDED_UP_OPERSTATE)
    opened: list[FakeBus] = []
    monkeypatch.setattr(can, "Bus", a_bus_that_opens(can, opened))

    result = open_python_can_adapter(config, BUS_ID, config.can_buses[BUS_ID], False)

    assert result["ok"] is True, result
    assert [bus.kwargs["channel"] for bus in opened] == [CHANNEL]


def test_a_listen_only_bus_on_a_down_link_is_refused_as_down_and_not_by_the_sentence_that_calls_it_up(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The state read settles a down link before the listen-only precondition does.

    `open_python_can_adapter` asks `listen_only_precondition` before the
    constructor, and on the socketcan route that reads the kernel's ctrlmode. A
    CAN link that is administratively down still reports `info_kind: can` with no
    ctrlmode flags, which is the branch that answers, verbatim,
    "<channel> is up without listen-only, so its controller sends dominant ACK
    bits." That sentence is false about a link that is down, and it is the
    refusal a `listen_only: true` bus on a down `can0` gets today. So the order
    is part of the contract and not an implementation detail: a fix that reads
    the link state after the precondition leaves #511 unfixed for exactly the
    buses whose refusal already says something untrue about the same link.

    The sentence is produced here rather than written down: the fake `ip` answers
    the JSON iproute2 emits for a `can` link with no ctrlmode, and the product's
    own reader turns it into the text this test then asserts is gone.
    """
    can = real_can_module()
    config = can_config(tmp_path, listen_only=True)
    root = fake_sysfs(tmp_path, monkeypatch)
    publish_link(root, CHANNEL, RECORDED_DOWN_FLAGS, RECORDED_DOWN_OPERSTATE)
    install_ip(monkeypatch, tmp_path, lambda command, **kwargs: completed(ip_json(None)))
    monkeypatch.setattr(can, "Bus", a_bus_that_must_not_be_constructed)
    false_sentence = can_module_under_test.socketcan_link_listen_only(CHANNEL)["detail"]
    assert false_sentence == f"{CHANNEL} is up without listen-only, so its controller sends dominant ACK bits.", false_sentence

    result = open_python_can_adapter(config, BUS_ID, config.can_buses[BUS_ID], False)

    assert_refused_as_down(result)
    assert result["error_type"] != LISTEN_ONLY_UNSUPPORTED_ERROR, result
    assert false_sentence not in json.dumps(result), result


def test_a_listen_only_bus_on_a_link_that_is_up_keeps_the_listen_only_refusal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The neighbour the reorder must not swallow: an up link without the
    ctrlmode is still `can_listen_only_unsupported`, with its own field and its
    own reading of the link."""
    can = real_can_module()
    config = can_config(tmp_path, listen_only=True)
    root = fake_sysfs(tmp_path, monkeypatch)
    publish_link(root, CHANNEL, RECORDED_UP_FLAGS, RECORDED_UP_OPERSTATE)
    install_ip(monkeypatch, tmp_path, lambda command, **kwargs: completed(ip_json(None)))
    monkeypatch.setattr(can, "Bus", a_bus_that_must_not_be_constructed)

    result = open_python_can_adapter(config, BUS_ID, config.can_buses[BUS_ID], False)

    assert result["error_type"] == LISTEN_ONLY_UNSUPPORTED_ERROR, result
    assert result["field"] == f"can_buses.{BUS_ID}.listen_only", result
    assert "interface_state" not in result, result


def test_a_listen_only_bus_on_a_link_whose_state_cannot_be_read_keeps_the_listen_only_refusal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No sysfs entry proves nothing about the link, so the precondition still
    has the last word and the new check took nothing away from it."""
    can = real_can_module()
    config = can_config(tmp_path, listen_only=True)
    fake_sysfs(tmp_path, monkeypatch)
    install_ip(monkeypatch, tmp_path, lambda command, **kwargs: completed(ip_json(None)))
    monkeypatch.setattr(can, "Bus", a_bus_that_must_not_be_constructed)

    result = open_python_can_adapter(config, BUS_ID, config.can_buses[BUS_ID], False)

    assert result["error_type"] == LISTEN_ONLY_UNSUPPORTED_ERROR, result


def test_a_state_that_cannot_be_read_does_not_refuse(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No sysfs entry for the name: the session proceeds exactly as before this
    refusal existed, and the bind is what decides."""
    can = real_can_module()
    config = can_config(tmp_path)
    fake_sysfs(tmp_path, monkeypatch)
    opened: list[FakeBus] = []
    monkeypatch.setattr(can, "Bus", a_bus_that_opens(can, opened))

    result = open_python_can_adapter(config, BUS_ID, config.can_buses[BUS_ID], False)

    assert result["ok"] is True, result
    assert len(opened) == 1


def test_a_pcan_handle_is_never_read_as_a_link(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The state is read on the socketcan route only. A `peak` bus on a PCANBasic
    handle opens through `pcan`, and a sysfs entry that happens to carry the
    handle's name says nothing about it."""
    config = can_config(tmp_path, channel="PCAN_USBBUS1", adapter="peak")
    root = fake_sysfs(tmp_path, monkeypatch)
    publish_link(root, "PCAN_USBBUS1", RECORDED_DOWN_FLAGS, RECORDED_DOWN_OPERSTATE)

    assert can_module_under_test.socketcan_interface_down(BUS_ID, config.can_buses[BUS_ID]) is None


def test_a_process_bridge_is_never_read_as_a_link(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = can_config(tmp_path, adapter="process")
    root = fake_sysfs(tmp_path, monkeypatch)
    publish_link(root, CHANNEL, RECORDED_DOWN_FLAGS, RECORDED_DOWN_OPERSTATE)

    assert can_module_under_test.socketcan_interface_down(BUS_ID, config.can_buses[BUS_ID]) is None


# ---------------------------------------------------------------------------
# Through the service: both values of clear_rx_queue, the lease, the listing,
# the classifier, and the retry once the link is up.


@pytest.mark.parametrize("clear_rx_queue", [True, False])
def test_a_down_link_is_refused_through_the_service_for_both_clear_rx_queue_values(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clear_rx_queue: bool) -> None:
    """The two wrong answers this replaces: `can_queue_clear_failed` naming the
    drain with the default, and `ok` over a dead link without it.

    The bus here is one that opens and whose `recv` answers `None`, so the red
    line on the code as it stands is `ok: True` for both parametrisations: the
    drain succeeds against this fake rather than failing as it does against the
    kernel. The operator's actual `can_queue_clear_failed` needs a receive that
    raises ENETDOWN, and that is the container tier's red line over the real
    kernel, in `test_can_over_vcan.py`. What this test adds to it is the whole
    service path around the refusal, and `opened` is what says the socket was
    never reached.
    """
    can = real_can_module()
    config = can_config(tmp_path)
    root = fake_sysfs(tmp_path, monkeypatch)
    publish_link(root, CHANNEL, RECORDED_DOWN_FLAGS, RECORDED_DOWN_OPERSTATE)
    opened: list[FakeBus] = []
    monkeypatch.setattr(can, "Bus", a_bus_that_opens(can, opened))
    service = AgenticHILToolService(config)
    try:
        result = service.call("can_session_start", {"bus_id": BUS_ID, "clear_rx_queue": clear_rx_queue})

        assert_refused_as_down(result)
        assert opened == [], "the socket was opened over a link the state read had already proven down"
        assert result.get("quarantined") is not True, result
        assert result.get("cleanup_required") is not True, result
        assert "quarantine_guidance" not in result, result
        assert result["lease_state"] == "released", result
        assert service.coordinator.blocked is False
        listed = service.call("can_buses_list")
        assert listed["buses"][BUS_ID]["session_active"] is False, listed

        classified = service.call("classify_last_error")
        assert classified["ok"] is True, classified
        assert classified["error_type"] == DOWN_ERROR, classified
        assert classified["source_tool"] == "can_session_start", classified
    finally:
        service.close()
    assert not coordination_record_states(config) & {"cleanup_required", "quarantined", "recovery_pending"}


def test_the_classifier_answers_causes_about_the_link_and_not_a_generic_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The half of "carried by the knowledge remediation table and the report
    classifier" that echoing the error type does not satisfy.

    `classify_last_error` reports an `error_type` for any type at all, by
    copying it out of the last report, so a test that asserts only that says
    nothing about the new type. The field the classifier actually decides is
    `likely_causes`, and its tables are the debugger's and the COM port's: a CAN
    error type reaches neither, so the fallback answers, and an operator whose
    link is down is sent to inspect a log about something else. The refusal
    carries its own causes and `classify_failure_report` passes a report's own
    `likely_causes` through, which is what makes the answer the link's on every
    backend rather than on whichever table happened to be consulted.
    """
    can = real_can_module()
    config = can_config(tmp_path)
    root = fake_sysfs(tmp_path, monkeypatch)
    publish_link(root, CHANNEL, RECORDED_DOWN_FLAGS, RECORDED_DOWN_OPERSTATE)
    monkeypatch.setattr(can, "Bus", a_bus_that_opens(can))
    service = AgenticHILToolService(config)
    try:
        refused = service.call("can_session_start", {"bus_id": BUS_ID})
        assert refused["error_type"] == DOWN_ERROR, refused
        causes = refused["likely_causes"]
        assert isinstance(causes, list) and causes, refused

        classified = service.call("classify_last_error")
    finally:
        service.close()

    assert classified["likely_causes"] == causes, classified
    assert classified["likely_causes"] not in (["inspect the debugger log for details"], ["inspect the COM port log for details"]), classified
    # Causes, not remediation: what would leave a link that exists administratively
    # down. Each one is about this interface, and none of them is about a queue,
    # a serial port or a debugger.
    assert all(("interface" in cause or "link" in cause or "adapter" in cause) for cause in classified["likely_causes"]), classified
    assert any("up" in cause for cause in classified["likely_causes"]), classified
    assert not any("COM port" in cause or "debugger" in cause for cause in classified["likely_causes"]), classified


def test_a_link_brought_up_after_the_refusal_opens_on_the_same_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The state is read per call, not pinned at load: the operator's `ip link
    set up` is answered by the next `can_session_start`, with no restart."""
    can = real_can_module()
    config = can_config(tmp_path)
    root = fake_sysfs(tmp_path, monkeypatch)
    publish_link(root, CHANNEL, RECORDED_DOWN_FLAGS, RECORDED_DOWN_OPERSTATE)
    opened: list[FakeBus] = []
    monkeypatch.setattr(can, "Bus", a_bus_that_opens(can, opened))
    service = AgenticHILToolService(config)
    try:
        refused = service.call("can_session_start", {"bus_id": BUS_ID})
        assert refused["error_type"] == DOWN_ERROR, refused
        assert opened == []

        publish_link(root, CHANNEL, RECORDED_UP_FLAGS, RECORDED_UP_OPERSTATE)
        started = service.call("can_session_start", {"bus_id": BUS_ID})

        assert started["ok"] is True, started
        assert started["summary"] == "CAN bus session started.", started
        assert len(opened) == 1
        assert service.call("can_session_stop", {"bus_id": BUS_ID})["ok"] is True
    finally:
        service.close()


# ---------------------------------------------------------------------------
# The neighbours that must not change.


def test_an_absent_interface_is_still_not_found_with_its_own_remediation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Two answers, two remediations: no sysfs entry reads no state, the bind
    answers ENODEV, and the refusal is the not-found one, whose steps list the
    host's interfaces rather than bringing a named one up."""
    can = real_can_module()
    config = can_config(tmp_path)
    fake_sysfs(tmp_path, monkeypatch)
    monkeypatch.setattr(can, "Bus", raising_bus(OSError(errno.ENODEV, "No such device")))

    result = open_python_can_adapter(config, BUS_ID, config.can_buses[BUS_ID], False)

    assert result["error_type"] == CAN_INTERFACE_NOT_FOUND_ERROR, result
    assert result["channel"] == CHANNEL, result
    assert "interface_state" not in result, result
    assert any("ip link show" in step for step in result["remediation"]), result
    # The two remediations open on different moves, which is the whole reason
    # for two types: this one sends the operator to find out what the host has,
    # the down one to bring a link it already has up.
    assert re.search(r"sudo ip link set \S+ up", result["remediation"][0]) is None, result["remediation"]


def test_a_link_that_goes_down_under_a_session_keeps_the_send_answer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """ENETDOWN on a bound socket is not this refusal: the session was on the
    link, what the controller did with the frame is unknown, and the answer stays
    `can_send_failed` with an unknown effect (the container tier pins the same
    over the real kernel)."""
    can = real_can_module()
    config = can_config(tmp_path)
    root = fake_sysfs(tmp_path, monkeypatch)
    publish_link(root, CHANNEL, RECORDED_UP_FLAGS, RECORDED_UP_OPERSTATE)

    class BusThatLosesItsLink(FakeBus):
        def send(self, message: object, timeout: float | None = None) -> None:
            raise can.CanOperationError(RECORDED_SEND_ON_DOWN_LINK, RECORDED_ENETDOWN)

    monkeypatch.setattr(can, "Bus", lambda **kwargs: BusThatLosesItsLink(**kwargs))
    service = AgenticHILToolService(config)
    try:
        assert service.call("can_session_start", {"bus_id": BUS_ID})["ok"] is True
        publish_link(root, CHANNEL, RECORDED_DOWN_FLAGS, RECORDED_DOWN_OPERSTATE)

        failed = service.call("can_send", {"bus_id": BUS_ID, "frame_id": "0x100", "data_hex": "01"})

        assert failed["ok"] is False, failed
        assert failed["error_type"] == "can_send_failed", failed
        assert failed["backend_error"] == RECORDED_SEND_BACKEND_ERROR, failed
        assert failed["side_effect_status"] == "unknown", failed
        assert failed["cleanup_required"] is True, failed
        assert "interface_state" not in failed, failed
    finally:
        service.close()


def test_a_constructor_time_enetdown_keeps_the_markerless_result(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Unchanged from #130: the number alone, without a state read that proved
    the link down, proves nothing about what the constructor did."""
    can = real_can_module()
    config = can_config(tmp_path)
    fake_sysfs(tmp_path, monkeypatch)
    monkeypatch.setattr(can, "Bus", raising_bus(can.CanOperationError(f"failed: {RECORDED_ENETDOWN_STRERROR}", RECORDED_ENETDOWN)))

    result = open_python_can_adapter(config, BUS_ID, config.can_buses[BUS_ID], False)

    assert result["error_type"] == "can_adapter_open_failed", result
    assert "side_effect_status" not in result, result


# ---------------------------------------------------------------------------
# The catalogue and the constant.


def test_the_new_type_has_its_own_constant_and_catalogue_entry() -> None:
    from agentic_hil import knowledge

    assert knowledge.CAN_INTERFACE_DOWN_ERROR == DOWN_ERROR
    entry = catalogue_entry(DOWN_ERROR)
    assert entry is not None
    assert entry["error_type"] == DOWN_ERROR
    assert "down" in entry["meaning"], entry["meaning"]
    assert re.search(r"sudo ip link set \S+ up", entry["remediation"][0]), entry["remediation"]
    assert any("recover --confirm-safe-state" in step for step in entry["do_not"]), entry


def test_the_two_refusals_stay_two_entries() -> None:
    """A link that is down and an interface that does not exist are two answers
    with two remediations: neither entry is the other, and the not-found entry
    was not widened to cover a down link."""
    down = catalogue_entry(DOWN_ERROR)
    missing = catalogue_entry(CAN_INTERFACE_NOT_FOUND_ERROR)

    assert down is not None and missing is not None
    assert down["meaning"] != missing["meaning"]
    assert down["remediation"] != missing["remediation"]
    assert "does not exist" in missing["meaning"] and "does not exist" not in down["meaning"].split(".")[0]


def test_the_refusal_is_reachable_without_a_can_import(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Like `socketcan_interface_missing`: the helper reads a file and the bus
    entry, and must not be the thing that needs python-can."""
    import sys

    config = can_config(tmp_path)
    root = fake_sysfs(tmp_path, monkeypatch)
    publish_link(root, CHANNEL, RECORDED_DOWN_FLAGS, RECORDED_DOWN_OPERSTATE)
    monkeypatch.setitem(sys.modules, "can", None)

    assert can_module_under_test.socketcan_interface_down(BUS_ID, config.can_buses[BUS_ID]) is not None


def test_the_recording_says_what_this_file_says_it_says() -> None:
    """The recorded strings differ in exactly the IFF_UP bit, and the recorded
    number is the ENETDOWN this Python knows: pinned so a later edit that swaps
    the recording for an invented one is visible."""
    assert int(RECORDED_DOWN_FLAGS, 16) & 0x1 == 0
    assert int(RECORDED_UP_FLAGS, 16) & 0x1 == 1
    assert int(RECORDED_UP_FLAGS, 16) ^ int(RECORDED_DOWN_FLAGS, 16) == 0x1
    # The number is the Linux kernel's. Windows numbers ENETDOWN 10050, and the
    # recording is of the kernel the container ran, not of this host.
    if os.name != "nt":
        assert errno.ENETDOWN == RECORDED_ENETDOWN
    # The one library fact, asserted against the installed python-can rather
    # than written down: the send text the container tier reads is what this
    # exception renders as, so the fake here raises what the kernel produces.
    can = real_can_module()
    assert str(can.CanOperationError(RECORDED_SEND_ON_DOWN_LINK, RECORDED_ENETDOWN)) == RECORDED_SEND_BACKEND_ERROR
