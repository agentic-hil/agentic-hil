"""What a failed close leaves behind, and what clears it (#633, #660, #661, #665).

One rule for every COM and CAN session, on the stop path and the replace path
alike:

* A close that raised keeps the session registered with its lease, and the
  next stop retries the close. No path releases the lease of a session it
  leaves registered, so a retried stop never meets a lease that is already gone.
* A close that cannot be retried because the bridge process is gone (#633) is
  final: the unconfirmed close is recorded under `cleanup_reasons`, the bus is
  given back, and the next stop and start complete.
* A handle that closed under an incident the call may not end (inside a bench
  run) answers `session_lease_held`, which says the handle is closed and names
  the call that ends the incident, never `*_close_failed` (#660).
* A handle that closed beside a broken audit hands its lease into the standing
  incident, so the operator's `recover` can sign it while the server runs, and
  the shutdown does not raise for it (#661).

No hardware: a scripted serial line, a staged python-can bus, a broker
participant double and the fake bridge child the CAN suites already use.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from contextlib import suppress
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import write_config
from test_can_participant_sessions import SharedFakeParticipant
from test_error_catalogue_com import Line, LineHandle, call, wait_for
from test_read_until import DIE, close

from agentic_hil import comports
from agentic_hil.config import load_config
from agentic_hil.coordination import HardwareCoordinator
from agentic_hil.tools import AgenticHILToolService

FIXTURES = Path(__file__).parent / "fixtures"
FAKE_CAN_BRIDGE = FIXTURES / "fake_can_bridge.py"

# Port ids, devices and channels of this module alone: device locks are machine-wide.
PORT_ID = "close_state_com"
DEVICE = "/dev/ttyCLOSESTATE0"
COM_PORTS_YAML = f'com_ports:\n  {PORT_ID}:\n    device: "{DEVICE}"\n'

DIRECT_BUS = "close_state_direct"
PROCESS_BUS = "close_state_bridge"
SHARED_BUS = "close_state_shared"
CAN_BUSES_YAML = (
    "can_buses:\n"
    f"  {DIRECT_BUS}:\n"
    '    adapter: "socketcan"\n'
    '    channel: "vcanclose76"\n'
    f"  {PROCESS_BUS}:\n"
    '    adapter: "process"\n'
    '    channel: "vcanclose76bridge"\n'
    f'    executable: "{FAKE_CAN_BRIDGE.as_posix()}"\n'
    f"  {SHARED_BUS}:\n"
    "    adapter: process\n"
    "    channel: close-state-can\n"
    "    executable: fake-bridge\n"
    "    shares:\n"
    "      ecu_a:\n"
    "        permissions:\n"
    "          allow_read: true\n"
    "          allow_write: true\n"
)


# ---------------------------------------------------------------------------
# COM


def on_the_line(monkeypatch: pytest.MonkeyPatch, line: Line) -> None:
    monkeypatch.setitem(sys.modules, "serial", SimpleNamespace(Serial=lambda *args, **kwargs: LineHandle(line)))


@pytest.fixture
def com(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[SimpleNamespace]:
    """A server on this module's port, with its own state root: an incident a
    case leaves stands nowhere but here."""
    config = write_config(tmp_path / "workspace", com_ports_yaml=COM_PORTS_YAML, state_root=tmp_path / "state")
    service = AgenticHILToolService(load_config(str(config)), frontend="mcp")
    line = Line()
    on_the_line(monkeypatch, line)
    try:
        yield SimpleNamespace(service=service, line=line, config=config, monkeypatch=monkeypatch)
    finally:
        close(service)


def com_started(com: SimpleNamespace) -> None:
    started = call(com.service, "com_session_start", {"port_id": PORT_ID})
    assert started["ok"] is True, started


def test_a_replace_whose_close_failed_is_settled_by_a_retried_stop(com: SimpleNamespace) -> None:
    """#665, COM: the replace path keeps the lease with the session it leaves
    registered, so the retried stop closes it and gives the port back."""
    com_started(com)
    session = com.service.com_ports.sessions[PORT_ID]
    com.line.handles[DEVICE].feed(DIE)
    wait_for(lambda: session.reader_error is not None, "the reader to record its failure")
    com.line.refuse_close_once.add(DEVICE)

    refused = call(com.service, "com_session_start", {"port_id": PORT_ID})
    assert refused["error_type"] == "com_port_close_failed", refused
    assert com.service.com_ports.sessions.get(PORT_ID) is session, "the session stays registered for the retry"

    stopped = call(com.service, "com_session_stop", {"port_id": PORT_ID})
    assert stopped["ok"] is True and stopped["was_active"] is True, stopped
    assert stopped.get("cleanup_reasons"), stopped
    again = call(com.service, "com_session_stop", {"port_id": PORT_ID})
    assert again["ok"] is True and again["was_active"] is False, again
    reopened = call(com.service, "com_session_start", {"port_id": PORT_ID})
    assert reopened["ok"] is True and reopened["already_active"] is False, reopened


def test_a_stop_whose_close_failed_is_settled_by_a_retried_stop(com: SimpleNamespace) -> None:
    """The neighbour on the stop path, which already defers its release."""
    com_started(com)
    com.line.refuse_close_once.add(DEVICE)

    refused = call(com.service, "com_session_stop", {"port_id": PORT_ID})
    assert refused["error_type"] == "com_port_close_failed", refused
    stopped = call(com.service, "com_session_stop", {"port_id": PORT_ID})
    assert stopped["ok"] is True and stopped["was_active"] is True, stopped
    reopened = call(com.service, "com_session_start", {"port_id": PORT_ID})
    assert reopened["ok"] is True and reopened["already_active"] is False, reopened


def held_by_the_run(result: dict, tool: str) -> None:
    assert result["ok"] is False, result
    assert result["tool"] == tool, result
    assert result["error_type"] == "session_lease_held", result
    assert result.get("cleanup_confirmed") is not True, result
    assert "bench_run_stop" in result.get("next_step", ""), result
    assert result.get("remediation"), result


def test_a_buffer_clear_that_failed_inside_a_run_is_held_until_the_run_ends(com: SimpleNamespace) -> None:
    """#660, COM: the handle is closed and the lease is held for the run's
    teardown. Neither the start nor any stop says `cleanup_confirmed: true`
    while it is held, the stop answers a refusal of its own that names
    `bench_run_stop`, and after the run the stop completes."""
    run = call(com.service, "bench_run_start", {"devices": [{"kind": "uart", "id": PORT_ID}], "label": "close-state"})
    assert run["ok"] is True, run
    com.line.refuse_input_reset.add(DEVICE)

    refused = call(com.service, "com_session_start", {"port_id": PORT_ID, "clear_buffer": True})
    assert refused["error_type"] == "com_buffer_clear_failed", refused
    assert refused.get("cleanup_confirmed") is not True, refused
    com.line.refuse_input_reset.clear()

    for _ in range(2):
        held_by_the_run(call(com.service, "com_session_stop", {"port_id": PORT_ID}), "com_session_stop")
    held_by_the_run(call(com.service, "com_session_start", {"port_id": PORT_ID}), "com_session_start")

    ended = call(com.service, "bench_run_stop", {})
    assert ended["ok"] is True, ended
    stopped = call(com.service, "com_session_stop", {"port_id": PORT_ID})
    assert stopped["ok"] is True, stopped
    reopened = call(com.service, "com_session_start", {"port_id": PORT_ID})
    assert reopened["ok"] is True and reopened["already_active"] is False, reopened


def test_a_write_whose_effect_is_unknown_inside_a_run_is_held_until_the_run_ends(com: SimpleNamespace) -> None:
    """#660, COM, through the other way into the in-run incident: a write that
    died on the line."""
    run = call(com.service, "bench_run_start", {"devices": [{"kind": "uart", "id": PORT_ID}], "label": "close-state"})
    assert run["ok"] is True, run
    com_started(com)
    com.line.write_dies.add(DEVICE)
    written = call(com.service, "com_write", {"port_id": PORT_ID, "text": "ping\n"})
    assert written["ok"] is False, written
    com.line.write_dies.clear()

    for _ in range(2):
        held_by_the_run(call(com.service, "com_session_stop", {"port_id": PORT_ID}), "com_session_stop")

    assert call(com.service, "bench_run_stop", {})["ok"] is True
    stopped = call(com.service, "com_session_stop", {"port_id": PORT_ID})
    assert stopped["ok"] is True, stopped


def test_an_audit_broken_stop_can_be_signed_while_the_server_runs(com: SimpleNamespace) -> None:
    """#661: once the handle is closed the port's lock goes back into the
    standing incident. A retried stop finds nothing left to stop, the operator's
    `recover` signs the incident, the port opens again in the same server, and
    the shutdown does not raise."""
    com_started(com)

    def audit_fails(self: comports.ComPortSession, event: dict, config: object = None) -> Exception | None:
        return OSError(28, "No space left on device")

    original = comports.ComPortSession.append_audit
    com.monkeypatch.setattr(comports.ComPortSession, "append_audit", audit_fails)
    refused = call(com.service, "com_session_stop", {"port_id": PORT_ID})
    com.monkeypatch.setattr(comports.ComPortSession, "append_audit", original)
    assert refused["ok"] is False, refused
    quarantine_id = refused.get("quarantine_id")
    assert quarantine_id, refused

    again = call(com.service, "com_session_stop", {"port_id": PORT_ID})
    assert again["ok"] is True and again["was_active"] is False, again

    recovered = HardwareCoordinator(load_config(str(com.config)), "operator-cli").recover(safe_state_confirmed=True, quarantine_id=quarantine_id)
    assert recovered["ok"] is True, recovered

    reopened = call(com.service, "com_session_start", {"port_id": PORT_ID})
    assert reopened["ok"] is True and reopened["already_active"] is False, reopened
    stopped = call(com.service, "com_session_stop", {"port_id": PORT_ID})
    assert stopped["ok"] is True, stopped
    com.service.close()


def test_an_audit_broken_session_does_not_make_the_shutdown_raise(com: SimpleNamespace) -> None:
    """#661, the shutdown half: the handle is closed and the lease is in the
    incident, so closing the server has nothing left to fail on."""
    com_started(com)
    com.monkeypatch.setattr(comports.ComPortSession, "append_audit", lambda self, event, config=None: OSError(28, "No space left on device"))
    refused = call(com.service, "com_session_stop", {"port_id": PORT_ID})
    assert refused["ok"] is False and refused.get("quarantine_id"), refused
    com.service.close()


# ---------------------------------------------------------------------------
# CAN


class ShutdownRefusedOnce:
    """A python-can bus whose first shutdown raises."""

    def __init__(self, **kwargs: object) -> None:
        self.refuse_shutdown = False

    def recv(self, timeout: float | None = None) -> None:
        return None

    def send(self, message: object, timeout: float | None = None) -> None:
        return None

    def shutdown(self) -> None:
        if self.refuse_shutdown:
            self.refuse_shutdown = False
            raise OSError("staged shutdown failure")


class UnknownSendParticipant(SharedFakeParticipant):
    """A broker participant whose send cannot say whether the frame left."""

    def send(self, frame_id: int, data: bytes, *, extended: bool = False, rtr: bool = False) -> dict:
        return {"ok": False, "error_type": "can_send_failed", "summary": "Staged send whose effect is unknown.", "side_effect_status": "unknown"}


@pytest.fixture
def can(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[SimpleNamespace]:
    buses: list[ShutdownRefusedOnce] = []

    def open_bus(**kwargs: object) -> ShutdownRefusedOnce:
        bus = ShutdownRefusedOnce(**kwargs)
        buses.append(bus)
        return bus

    monkeypatch.setitem(sys.modules, "can", SimpleNamespace(Bus=open_bus, Message=lambda **kwargs: SimpleNamespace(**kwargs), CanInitializationError=type("CanInitializationError", (Exception,), {})))
    import agentic_hil.canbroker as broker_module

    monkeypatch.setattr(broker_module, "attach_participant", lambda config, bus_id, participant, **kwargs: UnknownSendParticipant(participant))
    config = write_config(tmp_path / "workspace", can_buses_yaml=CAN_BUSES_YAML, state_root=tmp_path / "state")
    service = AgenticHILToolService(load_config(str(config)))
    try:
        yield SimpleNamespace(service=service, buses=buses, monkeypatch=monkeypatch)
    finally:
        with suppress(RuntimeError):
            close(service)


def test_a_can_replace_whose_close_failed_is_settled_by_a_retried_stop(can: SimpleNamespace) -> None:
    """#665, CAN."""
    started = can.service.call("can_session_start", {"bus_id": DIRECT_BUS, "clear_rx_queue": False})
    assert started["ok"] is True, started
    session = can.service.can_buses.sessions[(DIRECT_BUS, None)]
    session.active = False
    can.buses[-1].refuse_shutdown = True

    refused = can.service.call("can_session_start", {"bus_id": DIRECT_BUS, "clear_rx_queue": False})
    assert refused["error_type"] == "can_adapter_close_failed", refused
    assert can.service.can_buses.sessions.get((DIRECT_BUS, None)) is session

    stopped = can.service.call("can_session_stop", {"bus_id": DIRECT_BUS})
    assert stopped["ok"] is True and stopped["was_active"] is True, stopped
    assert stopped.get("cleanup_reasons"), stopped
    again = can.service.call("can_session_stop", {"bus_id": DIRECT_BUS})
    assert again["ok"] is True and again["was_active"] is False, again
    reopened = can.service.call("can_session_start", {"bus_id": DIRECT_BUS, "clear_rx_queue": False})
    assert reopened["ok"] is True and reopened["already_active"] is False, reopened
    assert can.service.call("can_session_stop", {"bus_id": DIRECT_BUS})["ok"] is True


def test_a_participant_send_of_unknown_effect_inside_a_run_is_held_until_the_run_ends(can: SimpleNamespace) -> None:
    """#660, CAN: the refusal is the held answer, it names the participant, and
    a start of the same participant meets the same answer."""
    run = can.service.call("bench_run_start", {"devices": [{"kind": "can", "id": SHARED_BUS, "participant": "ecu_a"}]})
    assert run["ok"] is True, run
    started = can.service.call("can_session_start", {"bus_id": SHARED_BUS, "participant": "ecu_a", "clear_rx_queue": False})
    assert started["ok"] is True, started
    sent = can.service.call("can_send", {"bus_id": SHARED_BUS, "participant": "ecu_a", "frame_id": "0x100", "data_hex": "01"})
    assert sent["ok"] is False, sent

    for _ in range(2):
        held = can.service.call("can_session_stop", {"bus_id": SHARED_BUS, "participant": "ecu_a"})
        held_by_the_run(held, "can_session_stop")
        assert held.get("participant") == "ecu_a", held
    restart = can.service.call("can_session_start", {"bus_id": SHARED_BUS, "participant": "ecu_a", "clear_rx_queue": False})
    held_by_the_run(restart, "can_session_start")
    assert restart.get("participant") == "ecu_a", restart

    assert can.service.call("bench_run_stop", {})["ok"] is True
    stopped = can.service.call("can_session_stop", {"bus_id": SHARED_BUS, "participant": "ecu_a"})
    assert stopped["ok"] is True, stopped
    reopened = can.service.call("can_session_start", {"bus_id": SHARED_BUS, "participant": "ecu_a", "clear_rx_queue": False})
    assert reopened["ok"] is True and reopened["already_active"] is False, reopened
    assert can.service.call("can_session_stop", {"bus_id": SHARED_BUS, "participant": "ecu_a"})["ok"] is True


def test_a_bridge_that_exits_on_close_gives_the_bus_back(can: SimpleNamespace) -> None:
    """#633: the bridge answers `open` and exits on `close` without a reply.
    The reap succeeds, so the close cannot be retried: the stop records the
    unconfirmed close, gives the bus back, and the next stop and start
    complete."""
    can.monkeypatch.setenv("FAKE_CAN_BRIDGE_EXIT_ON_CLOSE", "1")
    started = can.service.call("can_session_start", {"bus_id": PROCESS_BUS, "clear_rx_queue": False})
    assert started["ok"] is True, started

    first = can.service.call("can_session_stop", {"bus_id": PROCESS_BUS})
    assert "can_adapter_cleanup_unconfirmed" in first.get("cleanup_reasons", []), first
    assert first.get("lease_state") == "released", first
    assert (PROCESS_BUS, None) not in can.service.can_buses.sessions

    again = can.service.call("can_session_stop", {"bus_id": PROCESS_BUS})
    assert again["ok"] is True and again["was_active"] is False, again
    can.monkeypatch.delenv("FAKE_CAN_BRIDGE_EXIT_ON_CLOSE")
    reopened = can.service.call("can_session_start", {"bus_id": PROCESS_BUS, "clear_rx_queue": False})
    assert reopened["ok"] is True and reopened["already_active"] is False, reopened
    stopped = can.service.call("can_session_stop", {"bus_id": PROCESS_BUS})
    assert stopped["ok"] is True and stopped["was_active"] is True, stopped
