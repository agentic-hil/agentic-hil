"""Every CAN refusal names causes about the bus, whatever backend the service has (#517).

`classify_failure_report` takes the causes off the report when it carries
`likely_causes` and otherwise asks a table for them. The tables it is handed are
`comports.likely_causes` and each debugger backend's `_likely_causes`, both keyed
by their own error types with a generic fallback, so no CAN error type reaches
either: `can_interface_not_found`, `can_adapter_library_missing`,
`can_channel_not_available`, `can_listen_only_unsupported`,
`can_queue_clear_failed` and `can_send_failed` all fall through to
`inspect the COM port log for details` or, through a service whose backend is a
debugger, to `inspect the debugger log for details`. An operator whose SocketCAN
channel is not a network device is sent to read a serial log that does not exist
for this failure and would say nothing about it if it did.

`can_interface_down` (#511) closes this for one type by carrying its own
`likely_causes` on the refusal, which the classifier passes through. The decided
behaviour is that every CAN refusal does the same: one causes table in
`agentic_hil.can` keyed by the CAN error types, each refusal carrying its causes
from that table, and the classifier asking that table for a report whose error
type is a CAN type, so `classify_last_error` and the refusal say the same thing
over every backend. `can_interface_down` keeps exactly the causes #511 pinned, so
the table and that refusal agree, and the COM port and debugger tables are
untouched.

These tests assert what a cause is about rather than its wording: each one names
the bus, the interface, the adapter, the channel or the frame, none of them names
another transport, and no CAN type answers one of the three generic fallbacks.
The one exception is `can_interface_down`, whose three causes are pinned verbatim
because #511 decided them and this change must not move them.

No hardware and no CAN interface: the reports are written straight into the
failure record the classifier reads, which is the seam the defect lives on.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from conftest import write_config

import agentic_hil.can as can_module_under_test
from agentic_hil.comports import likely_causes as com_port_likely_causes
from agentic_hil.config import load_config
from agentic_hil.debugger import UnboundDebuggerBackend
from agentic_hil.knowledge import (
    CAN_ADAPTER_LIBRARY_MISSING_ERROR,
    CAN_CHANNEL_NOT_AVAILABLE_ERROR,
    CAN_INTERFACE_DOWN_ERROR,
    CAN_INTERFACE_NOT_FOUND_ERROR,
    LISTEN_ONLY_UNSUPPORTED_ERROR,
)
from agentic_hil.report import classify_failure_report, write_report
from agentic_hil.tools import AgenticHILToolService

QUEUE_CLEAR_FAILED_ERROR = "can_queue_clear_failed"
SEND_FAILED_ERROR = "can_send_failed"

# Distinctive by design: device locks are machine-wide, so a bus id and channel
# shared with another clone's tests would contend across checkouts.
DOWN_BUS_ID = "likely_causes_down_bus"
DOWN_CHANNEL = "can517down"
# Recorded inside the container image on 2026-09-06 for #511 (kernel 6.18 under
# WSL2, iproute2-6.15.0, python-can 4.6.1, Python 3.12.14) and carried here
# verbatim: a down vcan's `flags` and `operstate`. IFF_UP is bit 0; 0x80 is
# IFF_NOARP, which a vcan carries whatever its state. Copies of
# `tests/test_can_interface_down.py`'s constants of the same names rather than
# imports of them, because importing that module here would close a cycle: it
# imports `test_can_listen_only`, which imports this module.
RECORDED_DOWN_FLAGS = "0x80\n"
RECORDED_DOWN_OPERSTATE = "down\n"

# Every error type a CAN refusal answers with that reaches the classifier. The
# order is the issue's.
CAN_ERROR_TYPES = [
    CAN_INTERFACE_NOT_FOUND_ERROR,
    CAN_ADAPTER_LIBRARY_MISSING_ERROR,
    CAN_CHANNEL_NOT_AVAILABLE_ERROR,
    LISTEN_ONLY_UNSUPPORTED_ERROR,
    QUEUE_CLEAR_FAILED_ERROR,
    SEND_FAILED_ERROR,
    CAN_INTERFACE_DOWN_ERROR,
]

# The three generic answers a CAN type currently reaches, one per table the
# classifier is handed.
COM_PORT_FALLBACK = ["inspect the COM port log for details"]
DEBUGGER_FALLBACK = ["inspect the debugger log for details"]
UNBOUND_FALLBACK = ["inspect the report and log for details"]

# Pinned by #511 and not this issue's to move: the causes the `can_interface_down`
# refusal carries, which the table has to answer with so that the refusal and
# `classify_last_error` cannot drift apart.
INTERFACE_DOWN_CAUSES = [
    "the interface was created and never brought up (`ip link set <dev> up` has not been run for it)",
    "the link was taken down out of band, by an operator or by a script, and nothing brought it back",
    "a USB CAN adapter was re-enumerated and its interface came back down",
]

# What "names the bus" means, as whole words: a cause about a CAN failure talks
# about one of these. Whole words because a substring test for "can" passes on
# the word "cannot", which every sentence may carry.
BUS_WORDS = (
    "can",
    "bus",
    "buses",
    "interface",
    "interfaces",
    "channel",
    "channels",
    "adapter",
    "adapters",
    "link",
    "controller",
    "frame",
    "frames",
    "queue",
    "driver",
    "drivers",
    "library",
    "socketcan",
    "pcan",
    "bitrate",
    "listen",
    "listening",
    "node",
    "vcan",
)
# The transports a CAN cause must never send the reader to.
OTHER_TRANSPORT_WORDS = ("com port", "serial", "debugger", "gdb", "openocd", "st-link", "swd", "jtag", "probe")

# One anchor per type, so that "names the bus" cannot be satisfied by seven
# copies of the same sentence: each list has to say something about its own
# failure. Alternatives, because the wording is the implementation's to choose.
TYPE_ANCHORS = {
    CAN_INTERFACE_NOT_FOUND_ERROR: ("interface", "channel", "netdev"),
    CAN_ADAPTER_LIBRARY_MISSING_ERROR: ("library", "driver", "install", "installed"),
    CAN_CHANNEL_NOT_AVAILABLE_ERROR: ("channel", "driver", "adapter"),
    LISTEN_ONLY_UNSUPPORTED_ERROR: ("listen", "listening", "controller"),
    QUEUE_CLEAR_FAILED_ERROR: ("queue", "receive", "buffer", "drain", "drained"),
    SEND_FAILED_ERROR: ("send", "sent", "transmit", "transmitted", "ack", "frame"),
    CAN_INTERFACE_DOWN_ERROR: ("interface", "link"),
}

DEBUGGER_TYPES = ["openocd", "stlink", "pyocd"]

# One debugger cause list per backend, verbatim from each backend's own table, so
# that a rewrite of the debugger causes is caught here rather than passing as
# "still not about a bus". `target_not_detected` because all three answer it and
# all three word it differently.
DEBUGGER_TARGET_NOT_DETECTED = {
    "openocd": ["DUT is not powered", "wrong interface configuration", "SWD/JTAG wiring issue", "debug probe already in use"],
    "stlink": ["DUT is not powered", "wrong SWD/JTAG interface selection", "SWD/JTAG wiring issue", "debug probe already in use"],
    "pyocd": ["DUT is not powered", "SWD/JTAG wiring issue", "debug probe already in use", "wrong debuggers.<name>.target_type for this device"],
}

# The CAN error types `agentic_hil.can` writes that this issue does not name. The
# table is keyed by the seven above and not by the `can_` prefix, so these keep
# answering whichever generic table the classifier was handed. Recorded because
# the difference between the two keyings is invisible to every other test here,
# and widening the table later is a decision somebody should take on purpose.
CAN_ERROR_TYPES_OUTSIDE_THE_TABLE = [
    "can_listen_only_mode",
    "can_listen_only_unconfirmed",
    "can_queue_clear_limit",
    "can_read_failed",
    "can_adapter_open_failed",
    "can_adapter_close_failed",
    "can_adapter_not_found",
    "can_adapter_process_start_failed",
    "can_adapter_invalid_response",
    "can_adapter_protocol_unsupported",
    "can_backend_not_available",
    "can_bus_not_configured",
    "can_fd_remote_frame_unsupported",
    "can_classic_frame_too_large",
    "can_fd_frame_length_invalid",
]

# Distinctive by design, as above: a bus whose receive queue refuses to drain.
QUEUE_BUS_ID = "likely_causes_queue_bus"
QUEUE_CHANNEL = "can517queue"


def says_any(text: str, words: tuple[str, ...]) -> bool:
    return any(re.search(rf"(?<![a-z]){re.escape(word)}(?![a-z])", text.lower()) for word in words)


def can_report(error_type: str) -> dict:
    """A failure record shaped like the CAN refusals, carrying no causes of its own.

    No `likely_causes` key on purpose: the pass-through branch is already proven
    by #511, and the branch this issue is about is the one that asks a table.
    """
    return {
        "ok": False,
        "tool": "can_session_start",
        "bus_id": "likely_causes_probe_bus",
        "adapter": "socketcan",
        "channel": "can517probe",
        "error_type": error_type,
        "summary": f"CAN session was refused as {error_type}.",
        "target_contacted": False,
        "side_effect_committed": False,
        "side_effect_status": "not_started",
    }


def config_for(tmp_path: Path, *, debugger_type: str = "openocd"):
    return load_config(str(write_config(tmp_path, debugger_type=debugger_type)))


def down_link_config(tmp_path: Path):
    yaml = "".join(
        [
            "can_buses:\n",
            f"  {DOWN_BUS_ID}:\n",
            '    adapter: "socketcan"\n',
            f'    channel: "{DOWN_CHANNEL}"\n',
            "    listen_only: false\n",
        ]
    )
    return load_config(str(write_config(tmp_path, can_buses_yaml=yaml)))


def publish_down_link(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A `/sys/class/net` of this test's own, holding one interface that is down."""
    root = tmp_path / "sys" / "class" / "net" / DOWN_CHANNEL
    root.mkdir(parents=True)
    (root / "flags").write_text(RECORDED_DOWN_FLAGS, encoding="utf-8")
    (root / "operstate").write_text(RECORDED_DOWN_OPERSTATE, encoding="utf-8")
    monkeypatch.setattr(can_module_under_test, "SYSFS_NET_CLASS", str(root.parent))


def queue_bus_config(tmp_path: Path):
    yaml = "".join(
        [
            "can_buses:\n",
            f"  {QUEUE_BUS_ID}:\n",
            '    adapter: "socketcan"\n',
            f'    channel: "{QUEUE_CHANNEL}"\n',
            "    max_buffer_frames: 2\n",
        ]
    )
    return load_config(str(write_config(tmp_path, can_buses_yaml=yaml)))


class RefusingDrainAdapter:
    """An adapter session whose receive queue will not drain: `read` refuses.

    Enough of the adapter-session surface for `CanBusService` to drive it, the
    way `tests/test_hardening.py` drives the drain limit with a queue that never
    empties. Nothing is opened and no bus exists.
    """

    adapter_name = "fake"

    def read(self, max_frames: int, wait_timeout_s: float) -> dict:
        return {"ok": False, "error_type": "can_read_failed", "summary": "CAN adapter failed to read frames.", "backend_error": "staged drain failure"}

    def status(self) -> dict:
        return {"active": True}

    def close(self) -> dict:
        return {"ok": True, "safe_state_confirmed": True, "process_reaped": True}


def assert_causes_are_about_the_bus(causes: object, error_type: str, context: object) -> None:
    assert isinstance(causes, list) and causes, context
    assert causes not in (COM_PORT_FALLBACK, DEBUGGER_FALLBACK, UNBOUND_FALLBACK), context
    for cause in causes:
        assert isinstance(cause, str) and cause.strip(), context
        assert says_any(cause, BUS_WORDS), f"{cause!r} names nothing about the bus: {context}"
        assert not any(word in cause.lower() for word in OTHER_TRANSPORT_WORDS), f"{cause!r} sends the reader to another transport: {context}"
    assert any(says_any(cause, TYPE_ANCHORS[error_type]) for cause in causes), f"no cause is about {error_type}: {context}"


# ---------------------------------------------------------------------------
# The gap: a CAN report classified over each table the classifier is handed.


@pytest.mark.parametrize("error_type", CAN_ERROR_TYPES)
def test_a_can_report_is_classified_about_the_bus_over_the_com_port_table(tmp_path: Path, error_type: str) -> None:
    """The COM port table is `comports.likely_causes`, keyed by the serial error
    types with `inspect the COM port log for details` for everything else, and a
    CAN error type is everything else."""
    config = config_for(tmp_path)
    write_report(config, can_report(error_type))

    classified = classify_failure_report(config, com_port_likely_causes)

    assert classified["ok"] is True, classified
    assert classified["error_type"] == error_type, classified
    assert_causes_are_about_the_bus(classified["likely_causes"], error_type, classified)


@pytest.mark.parametrize("debugger_type", DEBUGGER_TYPES)
@pytest.mark.parametrize("error_type", CAN_ERROR_TYPES)
def test_a_can_report_is_classified_about_the_bus_through_a_service_on_a_debugger(tmp_path: Path, error_type: str, debugger_type: str) -> None:
    """The whole path an agent takes: the failure is recorded by the CAN tool and
    read back with `classify_last_error`, whose table is the bound debugger's."""
    config = config_for(tmp_path, debugger_type=debugger_type)
    write_report(config, can_report(error_type))
    service = AgenticHILToolService(config)
    try:
        classified = service.call("classify_last_error")
    finally:
        service.close()

    assert classified["ok"] is True, classified
    assert classified["error_type"] == error_type, classified
    assert classified["source_tool"] == "can_session_start", classified
    assert_causes_are_about_the_bus(classified["likely_causes"], error_type, classified)


@pytest.mark.parametrize("error_type", CAN_ERROR_TYPES)
def test_a_can_report_is_classified_the_same_whatever_table_the_backend_hands_over(tmp_path: Path, error_type: str) -> None:
    """A bus failure is a fact about the bus, so the answer must not depend on
    which probe, or no probe at all, happens to be bound in this project."""
    config = config_for(tmp_path)
    write_report(config, can_report(error_type))

    tables = {"com_port": com_port_likely_causes, "unbound": None}
    answers = {name: (UnboundDebuggerBackend(config).classify_last_error() if table is None else classify_failure_report(config, table))["likely_causes"] for name, table in tables.items()}
    for debugger_type in DEBUGGER_TYPES:
        backend_config = config_for(tmp_path / debugger_type, debugger_type=debugger_type)
        write_report(backend_config, can_report(error_type))
        service = AgenticHILToolService(backend_config)
        try:
            answers[debugger_type] = service.call("classify_last_error")["likely_causes"]
        finally:
            service.close()

    assert len(set(map(tuple, answers.values()))) == 1, answers


def test_each_can_error_type_gets_causes_of_its_own(tmp_path: Path) -> None:
    """Seven types, seven answers. One list reused for all of them would satisfy
    every assertion above and still tell an operator nothing about which failure
    happened."""
    config = config_for(tmp_path)
    answers = {}
    for error_type in CAN_ERROR_TYPES:
        write_report(config, can_report(error_type))
        answers[error_type] = classify_failure_report(config, com_port_likely_causes)["likely_causes"]

    assert len(set(map(tuple, answers.values()))) == len(CAN_ERROR_TYPES), answers


def test_the_table_answers_the_interface_down_causes_the_refusal_carries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The two agree, which is what keeps #511's answer from splitting in two.

    The refusal carries its causes and the classifier passes them through, so a
    table that answered something else for the same type would be a second
    wording of a decided answer, reachable from a record written before the
    refusal grew the field.
    """
    publish_down_link(tmp_path, monkeypatch)
    link_config = down_link_config(tmp_path)
    refused = can_module_under_test.socketcan_interface_down(DOWN_BUS_ID, link_config.can_buses[DOWN_BUS_ID])
    assert refused is not None and refused["likely_causes"] == INTERFACE_DOWN_CAUSES, refused

    config = config_for(tmp_path / "classifier")
    write_report(config, can_report(CAN_INTERFACE_DOWN_ERROR))
    classified = classify_failure_report(config, com_port_likely_causes)

    assert classified["likely_causes"] == INTERFACE_DOWN_CAUSES, classified


def test_a_queue_clear_that_will_not_drain_refuses_with_causes_of_its_own(tmp_path: Path) -> None:
    """`can_queue_clear_failed` at the refusal, not only at the classifier.

    The other six types are pinned on a real payload by the modules that own
    them; this one is written by `CanBusService._drain_rx_queue` and had no
    refusal-level test anywhere, so an implementation that keyed the table and
    left this payload bare would have gone unnoticed. The refusal and
    `classify_last_error` have to answer with the same list.
    """
    config = queue_bus_config(tmp_path)
    service = can_module_under_test.CanBusService(config)
    session = can_module_under_test.CanBusSession(QUEUE_BUS_ID, config.can_buses[QUEUE_BUS_ID], RefusingDrainAdapter(), str(tmp_path / "can-queue.jsonl"))
    service.sessions[QUEUE_BUS_ID] = session

    result = service.session_start(QUEUE_BUS_ID, clear_rx_queue=True)

    assert result["ok"] is False, result
    assert result["error_type"] == QUEUE_CLEAR_FAILED_ERROR, result
    assert_causes_are_about_the_bus(result["likely_causes"], QUEUE_CLEAR_FAILED_ERROR, result)
    classified = classify_failure_report(config, com_port_likely_causes)
    assert classified["likely_causes"] == result["likely_causes"], classified


@pytest.mark.parametrize("error_type", CAN_ERROR_TYPES_OUTSIDE_THE_TABLE)
def test_a_can_type_outside_the_table_still_answers_the_table_it_was_handed(tmp_path: Path, error_type: str) -> None:
    """The boundary, recorded rather than left to be read off the code.

    This issue names seven types and the table is keyed by those seven names, not
    by the `can_` prefix: the rest keep the generic answer until somebody decides
    otherwise. `can_listen_only_mode` is the one that will want revisiting, being
    as much a fact about a bus as the seven.
    """
    config = config_for(tmp_path)
    write_report(config, can_report(error_type))

    classified = classify_failure_report(config, com_port_likely_causes)

    assert classified["error_type"] == error_type, classified
    assert classified["likely_causes"] == COM_PORT_FALLBACK, classified


def test_a_can_report_that_carries_its_own_causes_still_wins(tmp_path: Path) -> None:
    """The pass-through branch is unchanged: a refusal that carried causes has
    already decided them, and the table must not overwrite what the failing call
    measured."""
    config = config_for(tmp_path)
    carried = ["the interface was renamed while this session was being opened"]
    write_report(config, {**can_report(CAN_INTERFACE_NOT_FOUND_ERROR), "likely_causes": carried})

    classified = classify_failure_report(config, com_port_likely_causes)

    assert classified["likely_causes"] == carried, classified


# ---------------------------------------------------------------------------
# The neighbours: the two tables this change does not touch.


def test_the_com_port_causes_are_unchanged() -> None:
    assert com_port_likely_causes("com_port_open_failed") == [
        "configured COM port device does not exist",
        "COM port is already open in another program",
        "USB serial adapter is unplugged or driver is missing",
    ]
    assert com_port_likely_causes("serial_read_failed") == [
        "COM port was disconnected",
        "serial driver reported an I/O error",
        "another process interfered with the port",
    ]
    assert com_port_likely_causes("serial_write_failed") == [
        "COM port was disconnected",
        "serial driver write timed out",
        "target or USB serial adapter stopped responding",
    ]
    assert com_port_likely_causes("serial_write_incomplete") == [
        "configured write_timeout_s is too short for this payload size and baudrate",
        "target or USB serial adapter is applying flow control",
        "COM port was disconnected partway through the write",
    ]
    assert com_port_likely_causes("no_such_serial_error") == COM_PORT_FALLBACK


@pytest.mark.parametrize("debugger_type", DEBUGGER_TYPES)
def test_the_debugger_causes_are_unchanged(tmp_path: Path, debugger_type: str) -> None:
    """Verbatim, and over the public path a caller takes.

    One type per backend rather than the whole table, chosen because the three
    backends word it differently: a rewrite that flattened them would be caught
    here. Read back through `classify_last_error` rather than off the backend's
    table attribute, so this pins what a caller receives.
    """
    config = config_for(tmp_path, debugger_type=debugger_type)
    write_report(config, {"ok": False, "tool": "probe_target", "error_type": "target_not_detected", "summary": "Debugger could not detect the target."})
    service = AgenticHILToolService(config)
    try:
        detected = service.call("classify_last_error")["likely_causes"]
        write_report(config, {"ok": False, "tool": "probe_target", "error_type": "no_such_debugger_error", "summary": "Debugger failed."})
        unknown = service.call("classify_last_error")["likely_causes"]
    finally:
        service.close()

    assert detected == DEBUGGER_TARGET_NOT_DETECTED[debugger_type], detected
    assert unknown == DEBUGGER_FALLBACK, unknown


def test_a_serial_report_still_answers_the_com_port_table(tmp_path: Path) -> None:
    config = config_for(tmp_path)
    write_report(config, {"ok": False, "tool": "com_read", "port_id": "bench", "error_type": "serial_read_failed", "summary": "COM port read failed."})

    classified = classify_failure_report(config, com_port_likely_causes)

    assert classified["likely_causes"] == com_port_likely_causes("serial_read_failed"), classified


def test_a_type_that_is_neither_still_falls_back_to_the_table_it_was_handed(tmp_path: Path) -> None:
    """The fallbacks stay reachable: this change narrows what falls through, it
    does not remove the generic answer for a type no table knows."""
    config = config_for(tmp_path)
    write_report(config, {"ok": False, "tool": "probe_target", "error_type": "some_unmapped_error", "summary": "Something failed."})

    assert classify_failure_report(config, com_port_likely_causes)["likely_causes"] == COM_PORT_FALLBACK
    assert UnboundDebuggerBackend(config).classify_last_error()["likely_causes"] == UNBOUND_FALLBACK
