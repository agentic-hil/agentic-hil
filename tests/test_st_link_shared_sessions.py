"""Sessions on the stlink backend reach the probe through stlink-server (#624).

A session ends ST-LINK_gdbserver with a kill, because every end the server
runs itself resumed the core. Killed while it held the probe's USB itself, the
server left the probe refusing the next start (`Target USB comms error`) and the
next STM32_Programmer_CLI call (`DEV_USB_COMM_ERR`) in the product's own stop
cycles on the reference board. Started with `-t`, the server reaches the probe
through stlink-server instead, and killed the same way it left neither refused
and the core where it was halted, in every stop recorded. So a session starts
stlink-server first, ends it only after the GDB server is gone, and says which
way it reached the probe. The recordings are
st_link_gdbserver_7_14_0_linux_ends_recordings.json and
st_link_gdbserver_7_14_0_linux_session_stops_recordings.json beside the fakes.

There is one stlink-server per port on a machine, and every session that
reaches a probe through it shares it, whichever service started it. So the
product starts it once, records it beside the device locks with every session
using it, and ends it only when the last of them is done and no other program
still holds a connection to it. Ended at once after the GDB server was killed,
it left the next start refused (`TCPCMD OPEN_DEV FAIL`); ended half a second
later, never (st_link_gdbserver_7_14_0_linux_server_ends_recordings.json).
"""

from __future__ import annotations

import contextlib
import json
import os
import socket
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

import pytest
from fixtures.fake_gdb import MI_ASYNC_UNSUPPORTED, ST_LINK_GDBSERVER
from fixtures.fake_st_link_gdbserver import NEVER_READY_VARIABLE as ST_LINK_NEVER_READY_VARIABLE
from fixtures.fake_st_link_gdbserver import OPEN_REFUSAL_SCENARIO, SHARED_PORT_VARIABLE
from fixtures.fake_stlink_server import EVENTS_VARIABLE as STLINK_SERVER_EVENTS_VARIABLE
from fixtures.fake_stlink_server import OPEN_DEV_REFUSED
from fixtures.fake_stlink_server import PORT_VARIABLE as STLINK_SERVER_PORT_VARIABLE
from fixtures.fake_stlink_server import SCENARIO_VARIABLE as STLINK_SERVER_SCENARIO_VARIABLE
from support import scaled_time_bound
from test_debug_sessions import START_TIMEOUT_S, start_debug_session
from test_gdbserver_sessions import ST_LINK_SCENARIO_VARIABLE, server_events, session_log, st_link_session_service

from agentic_hil.backends import gdbdebug, stlink, stlink_server
from agentic_hil.knowledge import remediation_fields
from agentic_hil.process import snapshot_process_images

RECORDINGS = Path(__file__).parent / "fixtures"
FAKE_STLINK_SERVER = RECORDINGS / "fake_stlink_server.py"
SERVER_ENDS_RECORDING = RECORDINGS / "st_link_gdbserver_7_14_0_linux_server_ends_recordings.json"
RESTARTS_RECORDING = RECORDINGS / "st_link_gdbserver_7_14_0_linux_restarts_recordings.json"
OPEN_DEV_LINE = "Error: TCPCMD OPEN_DEV FAIL, internal assoc not key created"
# The deadline of a start whose server never says its port listens. The fake has
# printed every other recorded startup line long before it runs out, so a start
# that took one of them for the ready line would have connected already.
NEVER_READY_TIMEOUT_S = scaled_time_bound(2.0)
# Hosts whose kernel answers which connections are open on a port: Linux in
# /proc/net/tcp and tcp6, Windows through GetTcpTable and GetTcp6Table.
CONNECTIONS_COUNTED = sys.platform == "win32" or sys.platform.startswith("linux")


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as unused:
        unused.bind(("127.0.0.1", 0))
        return int(unused.getsockname()[1])


def use_stlink_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, executable: str | None) -> tuple[int, Path]:
    """What a session finds: `executable` as the stlink-server on this host, and a port of its own for 7184.

    Returns the port and the file the fake stlink-server writes its events to."""
    port = free_port()
    events = tmp_path / "stlink-server-events.jsonl"
    monkeypatch.setattr(stlink, "STLINK_SERVER_PORT", port)
    monkeypatch.setattr(stlink, "find_stlink_server", lambda: executable)
    monkeypatch.setenv(STLINK_SERVER_PORT_VARIABLE, str(port))
    monkeypatch.setenv(SHARED_PORT_VARIABLE, str(port))
    monkeypatch.setenv(STLINK_SERVER_EVENTS_VARIABLE, str(events))
    return port, events


def recorded_terminations(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """Every process tree the session layer and the stlink-server coordination end, in order.

    Each with whether the GDB server was gone by then, and the monotonic time
    the end was asked for and returned."""
    calls: list[dict] = []

    def recording(terminate):
        def recorded(process, timeout_s, **kwargs):
            gdb_servers = [call["process"] for call in calls if call["which"] == "gdb_server"]
            which = "stlink_server" if "fake_stlink_server" in " ".join(map(str, process.args)) else "gdb_server"
            call = {"which": which, "process": process, "graceful": kwargs.get("graceful", True), "gdb_server_gone": all(server.poll() is not None for server in gdb_servers) and bool(gdb_servers), "called_at": time.monotonic()}
            calls.append(call)
            try:
                return terminate(process, timeout_s, **kwargs)
            finally:
                call["returned_at"] = time.monotonic()

        return recorded

    monkeypatch.setattr(gdbdebug, "terminate_process_tree", recording(gdbdebug.terminate_process_tree))
    monkeypatch.setattr(stlink_server, "terminate_process_tree", recording(stlink_server.terminate_process_tree))
    return calls


def another_service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A second service on this machine, with a probe and a state root of its own.

    A session holds the probe discovery of its state root for as long as it is
    open, so a second session overlaps the first only from another state root:
    another account's, or one configured apart."""
    return st_link_session_service(tmp_path / "second", monkeypatch, probe_id="STLINK456", state_root=tmp_path / "second-state")


def wait_for_stlink_server_event(events: Path, name: str, count: int = 1) -> None:
    deadline_s = scaled_time_bound(10)
    began = time.monotonic()
    while [event["event"] for event in server_events(events)].count(name) < count:
        assert time.monotonic() - began < deadline_s, server_events(events)
        time.sleep(0.05)


def test_st_link_session_reaches_the_probe_through_the_stlink_server_it_starts_and_ends_it_after_the_gdb_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """stlink-server first, the GDB server with `-t`, and at the stop the GDB server killed before stlink-server is ended.

    The GDB server is still killed, never asked to stop, because every end it
    runs itself resumed the core (recorded). stlink-server is asked to stop,
    which ended it at once when recorded (exit status -15), and only once the
    GDB server is gone, so nothing it carries for the GDB server is cut off."""
    _, stlink_events = use_stlink_server(tmp_path, monkeypatch, str(FAKE_STLINK_SERVER))
    calls = recorded_terminations(monkeypatch)
    service, gdb_server_events = st_link_session_service(tmp_path, monkeypatch)
    try:
        started = start_debug_session(service, mode="reset_halt")
        assert started["ok"] is True, started
        stopped = service.call("debug_stop_session")
    finally:
        service.close()

    assert stopped["ok"] is True, stopped
    assert stopped["safe_state_confirmed"] is True, stopped
    assert session_log(service, started)["server_command"][-1] == "-t"
    assert "shared_server_connected" in [event["event"] for event in server_events(gdb_server_events)]
    assert [event["event"] for event in server_events(stlink_events)][:2] == ["started", "listening"]
    shared = started["session"]["probe_server"]
    assert shared["mode"] == "shared", shared
    assert shared["started_by_session"] is True, shared
    assert shared["port"] == stlink.STLINK_SERVER_PORT, shared
    assert session_log(service, started)["probe_server"]["mode"] == "shared"
    ends = [call for call in calls if call["which"] == "stlink_server"]
    first_gdb_server_end = next(call for call in calls if call["which"] == "gdb_server")
    assert first_gdb_server_end["graceful"] is False, calls
    assert ends, calls
    assert calls.index(first_gdb_server_end) < calls.index(ends[0]), calls
    assert ends[0]["gdb_server_gone"] is True, calls
    assert ends[0]["graceful"] is True, calls
    assert all(call["process"].poll() is not None for call in calls)
    ended = stopped["session"]["probe_server"]
    assert ended["ended"] is True, ended
    assert ended["returncode"] is not None, ended


def test_st_link_session_shares_an_stlink_server_already_listening_and_leaves_it_running(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """One already on the port is used, never started a second time, and never ended by the session.

    stlink-server is there to be shared: another tool's session may be using
    it, and ending it would cut that one off."""
    _, stlink_events = use_stlink_server(tmp_path, monkeypatch, str(FAKE_STLINK_SERVER))
    already = subprocess.Popen([sys.executable, str(FAKE_STLINK_SERVER)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline_s = scaled_time_bound(10)
        began = time.monotonic()
        while "listening" not in [event["event"] for event in server_events(stlink_events)]:
            assert time.monotonic() - began < deadline_s, server_events(stlink_events)
            assert already.poll() is None
            time.sleep(0.05)
        calls = recorded_terminations(monkeypatch)
        service, _ = st_link_session_service(tmp_path, monkeypatch)
        try:
            started = start_debug_session(service, mode="attach")
            assert started["ok"] is True, started
            stopped = service.call("debug_stop_session")
        finally:
            service.close()

        assert stopped["ok"] is True, stopped
        assert session_log(service, started)["server_command"][-1] == "-t"
        assert [event["event"] for event in server_events(stlink_events)].count("started") == 1
        assert [call for call in calls if call["which"] == "stlink_server"] == []
        assert already.poll() is None
        shared = stopped["session"]["probe_server"]
        assert shared["mode"] == "shared", shared
        assert shared["started_by_session"] is False, shared
        assert shared["started_by_agentic_hil"] is False, shared
        assert shared["ended"] is False, shared
    finally:
        already.kill()
        already.wait(timeout=scaled_time_bound(10))


def test_st_link_session_without_stlink_server_reaches_the_probe_itself_and_says_what_its_stop_risks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No stlink-server on this host: the session runs as before, with no `-t`, and says so in every answer.

    Its stop still kills the server while it holds the probe's USB itself, which
    is what left the probe refusing the next opener in the recorded stop cycles.
    The answer says that, with the recorded count, rather than reading as safe
    as a shared session's."""
    use_stlink_server(tmp_path, monkeypatch, None)
    service, _ = st_link_session_service(tmp_path, monkeypatch)
    try:
        started = start_debug_session(service, mode="reset_halt")
        assert started["ok"] is True, started
        stopped = service.call("debug_stop_session")
    finally:
        service.close()

    assert stopped["ok"] is True, stopped
    assert "-t" not in session_log(service, started)["server_command"]
    for answer in (started, stopped):
        direct = answer["session"]["probe_server"]
        assert direct["mode"] == "direct", direct
        assert "stlink-server" in direct["reason"], direct
        assert "Target USB comms error" in direct["stop_risk"], direct
        assert "DEV_USB_COMM_ERR" in direct["stop_risk"], direct


def test_stlink_server_that_exits_without_listening_is_reported_and_the_session_reaches_the_probe_itself(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A found stlink-server that ends before its port listens: the session says how it ended and runs as one without it.

    No stlink-server failure has been recorded, so the stand-in prints nothing
    at all and only exits: what is asserted is the product's own reading of that
    exit, its status and the empty tail of the log it kept, and not words no
    recording has."""
    exits = tmp_path / "exits.py"
    exits.write_text("import sys\n\nsys.exit(3)\n", encoding="utf-8")
    port, _ = use_stlink_server(tmp_path, monkeypatch, str(exits))
    service, _ = st_link_session_service(tmp_path, monkeypatch)
    try:
        started = start_debug_session(service, mode="attach")
        assert started["ok"] is True, started
        assert service.call("debug_stop_session")["ok"] is True
    finally:
        service.close()

    assert "-t" not in session_log(service, started)["server_command"]
    direct = started["session"]["probe_server"]
    assert direct["mode"] == "direct", direct
    assert direct["returncode"] == 3, direct
    assert direct["output_tail"] == [], direct
    assert direct["reason"] == f"stlink-server exited with status 3 before it listened on port {port}, so ST-LINK_gdbserver opens the probe's USB itself.", direct
    assert (Path(service.config.work_dir) / direct["log_path"]).read_bytes() == b"", direct


@pytest.mark.skipif(os.name != "nt", reason="the Windows end: the GDB server's Job Object, then stlink-server's process tree")
def test_windows_stop_ends_the_gdb_servers_job_before_the_stlink_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Windows has no SIGKILL and no SIGTERM: the GDB server ends with its Job Object, and stlink-server's tree is ended after it.

    stlink-server is in no Job of the service that started it, because another
    service's session may still reach the probe through it when that service
    exits. Both ends are kills. stlink-server died of the signal on SIGTERM
    without a shutdown of its own (exit status -15, recorded), so a kill is the
    same end; the order is what keeps the GDB server from being cut off
    mid-transfer by the server it reaches the probe through."""
    use_stlink_server(tmp_path, monkeypatch, str(FAKE_STLINK_SERVER))
    from agentic_hil import process as process_module

    terminate_job = process_module._terminate_windows_job
    ended_jobs: list[tuple[int, float]] = []

    def recorded(handle: int) -> None:
        terminate_job(handle)
        ended_jobs.append((handle, time.monotonic()))

    monkeypatch.setattr(process_module, "_terminate_windows_job", recorded)
    calls = recorded_terminations(monkeypatch)
    service, _ = st_link_session_service(tmp_path, monkeypatch)
    try:
        started = start_debug_session(service, mode="attach")
        assert started["ok"] is True, started
        session = service.backend._debug.session
        gdb_server_job = session.server._agentic_hil_job_handle
        assert isinstance(gdb_server_job, int)
        assert getattr(session.companion.process, "_agentic_hil_job_handle", None) is None
        stopped = service.call("debug_stop_session")
    finally:
        with contextlib.suppress(RuntimeError):
            service.close()

    assert stopped["ok"] is True, stopped
    assert ended_jobs and ended_jobs[0][0] == gdb_server_job, ended_jobs
    ends = [call for call in calls if call["which"] == "stlink_server"]
    assert len(ends) == 1, calls
    assert ends[0]["called_at"] > ended_jobs[0][1], (ends, ended_jobs)
    assert ends[0]["gdb_server_gone"] is True, calls
    assert ends[0]["process"].poll() is not None
    assert stopped["session"]["probe_server"]["ended"] is True


def test_the_recorded_stop_cycles_back_the_shared_mode() -> None:
    """The committed stop cycles: the direct kill left the probe refusing the next opener, the shared one never did.

    Each round is the product's own: a reset-halt session, an attach session
    and a second attach session, each stopped, then a CLI call, cycle after
    cycle on the reference board. The shared round is at least 100 stops with
    no start and no CLI call refused, every stop confirmed, and the core not
    run over any stop."""
    recordings = Path(__file__).parent / "fixtures"
    stops = json.loads((recordings / "st_link_gdbserver_7_14_0_linux_session_stops_recordings.json").read_text(encoding="utf-8"))
    direct = stops["rounds"]["direct"]["summary"]
    shared = stops["rounds"]["shared"]["summary"]

    assert direct["starts_refused_in_cycles"], direct
    assert shared["probe_servers"] == ["shared"], shared
    assert shared["stops"] >= 100, shared
    assert shared["stops_not_confirmed"] == 0, shared
    assert shared["starts_refused_in_cycles"] == [], shared
    assert shared["cli_calls"] == shared["cycles"], shared
    assert shared["cli_refused_in_cycles"] == [], shared
    assert shared["ran_ms_over_the_stop"] and set(shared["ran_ms_over_the_stop"]) == {0}, shared
    assert shared["ran_ms_over_the_attach_stop"] and set(shared["ran_ms_over_the_attach_stop"]) == {0}, shared


def never_ready_start(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, prepare: Callable[[object], None] | None = None) -> dict:
    """A start whose GDB server never says its port listens, so the start runs out its time and ends a running server.

    The fake prints the recorded startup up to the ready line and then keeps
    running, which is the one way the end of a running server is reached at
    start: what that end can leave on the probe is what the tests below differ
    in. `prepare` is handed the service before the start, for a test that has
    something to arrange inside it."""
    monkeypatch.setenv(ST_LINK_NEVER_READY_VARIABLE, "1")
    service, _ = st_link_session_service(tmp_path, monkeypatch)
    if prepare is not None:
        prepare(service)
    try:
        started = service.call("debug_start_session", {"image_path": "build/app.elf", "mode": "attach", "timeout_s": NEVER_READY_TIMEOUT_S})
        return {"started": started, "status": service.call("debug_get_session_status")}
    finally:
        with contextlib.suppress(RuntimeError):
            service.close()
        service.coordinator.close()


def test_a_start_that_times_out_through_stlink_server_reports_the_probe_untouched(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Shared mode: the kill that ends the timed-out server never reached the probe's USB, so the start refuses rather than quarantining.

    ST-LINK_gdbserver with `-t` holds no probe of its own, and the recorded
    shared stop round killed 120 of these over 40 cycles without one start or
    one STM32_Programmer_CLI call being refused afterwards. The reset of a reset
    mode is a GDB command this start never got to send either, so there is
    nothing left to settle and the next call may have the bench."""
    use_stlink_server(tmp_path, monkeypatch, str(FAKE_STLINK_SERVER))
    answers = never_ready_start(tmp_path, monkeypatch)
    started, status = answers["started"], answers["status"]

    assert started["ok"] is False, started
    assert started["error_type"] == "timeout", started
    assert started["backend_error_type"] == "gdb_server_not_ready", started
    assert started["cleanup_confirmed"] is True, started
    assert started["side_effect_status"] == "not_started", started
    assert started["retry_safe"] is True, started
    assert started.get("cleanup_required") is not True, started
    assert started["probe_server"]["mode"] == "shared", started["probe_server"]
    assert started["probe_server"]["ended"] is True, started["probe_server"]
    assert status["active"] is False, status


def test_a_start_that_times_out_on_the_probes_usb_itself_reports_the_hardware_unknown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Direct mode: the kill that ends the timed-out server held the probe's USB, which the recordings say can leave it refusing.

    Nothing about the reset belonging to GDB says anything about that: 8 of 99
    recorded direct stops left the probe refusing its next opener, and this
    start's own cleanup is such a stop. So the answer reports the hardware and
    the side effect as unknown, refuses to call a retry safe, keeps the session
    for cleanup, and carries the companion record with the risk it names."""
    use_stlink_server(tmp_path, monkeypatch, None)
    answers = never_ready_start(tmp_path, monkeypatch)
    started, status = answers["started"], answers["status"]

    assert started["ok"] is False, started
    assert started["error_type"] == "timeout", started
    assert started["backend_error_type"] == "gdb_server_not_ready", started
    assert started["cleanup_confirmed"] is True, started
    assert started["side_effect_status"] == "unknown", started
    assert started["retry_safe"] is False, started
    assert started["hardware_state"] == "unknown", started
    assert started["target_state"] == "unknown", started
    assert started["cleanup_required"] is True, started
    assert started["probe_server"]["mode"] == "direct", started["probe_server"]
    assert "Target USB comms error" in started["probe_server"]["stop_risk"], started["probe_server"]
    assert status["active"] is True, status
    assert status["quarantined"] is True, status


def test_a_timed_out_server_that_exits_before_the_cleanup_still_reports_the_hardware_unknown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Direct mode, with the server exiting between the read that gives up on it and the cleanup that would end it.

    From that moment the kill is on its way, and the cleanup finds a process
    already gone: nothing it can read afterwards says whether the server exited
    on its own or was ended under it. So the answer stands on the read the start
    took while the server was still running, and is the same answer as the test
    above. What would have earned a refusal instead is a backend reading of the
    server's last words that placed its exit before any contact, and a server
    killed mid-wait leaves none."""
    use_stlink_server(tmp_path, monkeypatch, None)
    cleanup_found_the_server_gone: list[bool] = []

    def exit_the_server_before_the_cleanup(service) -> None:
        sessions = service.backend._debug
        cleanup = sessions._cleanup_session

        def ends_the_server_first(session, timeout_s: float):
            gdbdebug.terminate_process_tree(session.server, scaled_time_bound(10), graceful=False)
            cleanup_found_the_server_gone.append(session.server.poll() is not None)
            return cleanup(session, timeout_s)

        sessions._cleanup_session = ends_the_server_first

    answers = never_ready_start(tmp_path, monkeypatch, prepare=exit_the_server_before_the_cleanup)
    started, status = answers["started"], answers["status"]

    # The window this is about: the cleanup really did run on an exited server.
    assert cleanup_found_the_server_gone[:1] == [True], cleanup_found_the_server_gone
    assert started["ok"] is False, started
    assert started["error_type"] == "timeout", started
    assert started["backend_error_type"] == "gdb_server_not_ready", started
    assert started["cleanup_confirmed"] is True, started
    assert started["side_effect_status"] == "unknown", started
    assert started["retry_safe"] is False, started
    assert started["hardware_state"] == "unknown", started
    assert started["target_state"] == "unknown", started
    assert started["cleanup_required"] is True, started
    assert started["lease_state"] == "cleanup_required", started
    assert "target_contacted" not in started, started
    assert started["probe_server"]["mode"] == "direct", started["probe_server"]
    assert "Target USB comms error" in started["probe_server"]["stop_risk"], started["probe_server"]
    assert status["active"] is True, status
    assert status["quarantined"] is True, status


@pytest.mark.parametrize(("failing_step", "error_type"), [("output_readers", "debug_session_setup_failed"), ("gdb", "gdb_start_failed")])
def test_a_server_gone_before_a_failed_start_reads_it_still_reports_the_hardware_unknown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failing_step: str, error_type: str) -> None:
    """Direct mode, with the server already gone when a step of the start fails and the start reads whether it still runs.

    The two steps that fail with the server spawned and nothing of its output
    read for an exit: the readers of that output, and GDB, which starts once
    the recorded server has said its port listens. The start did not end this
    server, but the server held the probe's USB itself, and no reading of what
    it said on the way out places its exit before any contact: its output was
    never read to the end, or it had said its port listens, which it only does
    once it has the probe. So the answer is the same as for a server the start
    ended, and the session is kept for cleanup."""
    use_stlink_server(tmp_path, monkeypatch, None)
    service, _ = st_link_session_service(tmp_path, monkeypatch)
    sessions = service.backend._debug
    server_gone_before_the_read: list[bool] = []

    def end_the_server() -> None:
        server = sessions.session.server
        gdbdebug.terminate_process_tree(server, scaled_time_bound(10), graceful=False)
        server_gone_before_the_read.append(server.poll() is not None)

    if failing_step == "output_readers":

        def readers_do_not_start(session) -> None:
            end_the_server()
            raise RuntimeError("injected: the output readers do not start")

        sessions._start_output_readers = readers_do_not_start
    else:

        def gdb_does_not_start(*args, **kwargs):
            end_the_server()
            raise OSError("injected: GDB does not start")

        monkeypatch.setattr(gdbdebug, "GdbMiClient", gdb_does_not_start)
    try:
        started = start_debug_session(service, mode="attach")
        status = service.call("debug_get_session_status")
    finally:
        with contextlib.suppress(RuntimeError):
            service.close()
        service.coordinator.close()

    # The window this is about: the server was gone before the start read it.
    assert server_gone_before_the_read == [True], server_gone_before_the_read
    assert started["ok"] is False, started
    assert started["error_type"] == error_type, started
    assert started["cleanup_confirmed"] is True, started
    assert started["side_effect_status"] == "unknown", started
    assert started["retry_safe"] is False, started
    assert started["hardware_state"] == "unknown", started
    assert started["target_state"] == "unknown", started
    assert started["cleanup_required"] is True, started
    assert started["lease_state"] == "cleanup_required", started
    assert "target_contacted" not in started, started
    assert started["probe_server"]["mode"] == "direct", started["probe_server"]
    assert "Target USB comms error" in started["probe_server"]["stop_risk"], started["probe_server"]
    assert status["active"] is True, status
    assert status["quarantined"] is True, status


def test_a_gdb_refusal_before_the_connect_still_reports_a_direct_probe_unaccounted_for(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A GDB that cannot do asynchronous MI is refused before `-target-select`, and in direct mode that refusal still ends a running server.

    The refusal itself is unchanged: it names the setting and what GDB
    answered, and GDB never reached the target. What it may not add is that the
    bench is as it was, because ending the server holding the probe's USB is
    the recorded way to leave the probe refusing its next opener. So the answer
    keeps the session for cleanup and carries the companion record that says
    which way it reached the probe."""
    use_stlink_server(tmp_path, monkeypatch, None)
    monkeypatch.setenv("FAKE_GDB_BEHAVIOR", MI_ASYNC_UNSUPPORTED)
    service, _ = st_link_session_service(tmp_path, monkeypatch, behavior=f"{ST_LINK_GDBSERVER}+{MI_ASYNC_UNSUPPORTED}")
    try:
        started = service.call("debug_start_session", {"image_path": "build/app.elf", "mode": "attach", "timeout_s": NEVER_READY_TIMEOUT_S})
    finally:
        with contextlib.suppress(RuntimeError):
            service.close()
        service.coordinator.close()

    assert started["ok"] is False, started
    assert started["error_type"] == "gdb_async_unsupported", started
    assert "mi-async" in started["summary"], started
    assert started["side_effect_status"] == "unknown", started
    assert started["retry_safe"] is False, started
    assert started["hardware_state"] == "unknown", started
    assert started["cleanup_required"] is True, started
    assert "target_contacted" not in started, started
    assert started["probe_server"]["mode"] == "direct", started["probe_server"]
    assert "Target USB comms error" in started["probe_server"]["stop_risk"], started["probe_server"]


@pytest.mark.parametrize(("failure", "error_type"), [("mi_async_refused", "gdb_async_unsupported"), ("gdb_exited", "debugger_error")])
def test_a_server_gone_before_a_gdb_setup_command_fails_still_reports_the_hardware_unknown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str, error_type: str) -> None:
    """Direct mode, with the server already gone when a GDB command of the start's setup fails, before `-target-select`.

    The recorded refusal of asynchronous MI, and the first setup command
    failing because GDB itself has exited. Either way the start gives up on a
    server it did not end, but one that held the probe's USB itself and had
    said its port listens, which it only does once it has the probe. Nothing
    read what it said on the way out, so nothing places its exit before any
    contact, and the answer is the one a start gives for a server it ended."""
    use_stlink_server(tmp_path, monkeypatch, None)
    behavior, failing_command = ST_LINK_GDBSERVER, "-gdb-set pagination off"
    if failure == "mi_async_refused":
        monkeypatch.setenv("FAKE_GDB_BEHAVIOR", MI_ASYNC_UNSUPPORTED)
        behavior, failing_command = f"{ST_LINK_GDBSERVER}+{MI_ASYNC_UNSUPPORTED}", gdbdebug.MI_ASYNC_COMMAND
    service, _ = st_link_session_service(tmp_path, monkeypatch, behavior=behavior)
    sessions = service.backend._debug
    send = sessions._gdb_command
    server_gone_before_the_read: list[bool] = []

    def ends_the_server_first(session, command: str, *args, **kwargs):
        if command == failing_command:
            gdbdebug.terminate_process_tree(session.server, scaled_time_bound(10), graceful=False)
            server_gone_before_the_read.append(session.server.poll() is not None)
            if failure == "gdb_exited":
                gdbdebug.terminate_process_tree(session.gdb.child, scaled_time_bound(10), graceful=False)
                assert session.gdb.exited.wait(scaled_time_bound(10))
        return send(session, command, *args, **kwargs)

    sessions._gdb_command = ends_the_server_first
    try:
        started = start_debug_session(service, mode="attach")
        status = service.call("debug_get_session_status")
    finally:
        with contextlib.suppress(RuntimeError):
            service.close()
        service.coordinator.close()

    # The window this is about: the server was gone before the start read it.
    assert server_gone_before_the_read == [True], server_gone_before_the_read
    assert started["ok"] is False, started
    assert started["error_type"] == error_type, started
    assert started["cleanup_confirmed"] is True, started
    assert started["side_effect_status"] == "unknown", started
    assert started["retry_safe"] is False, started
    assert started["hardware_state"] == "unknown", started
    assert started["target_state"] == "unknown", started
    assert started["cleanup_required"] is True, started
    assert started["lease_state"] == "cleanup_required", started
    assert "target_contacted" not in started, started
    assert started["probe_server"]["mode"] == "direct", started["probe_server"]
    assert "Target USB comms error" in started["probe_server"]["stop_risk"], started["probe_server"]
    assert status["active"] is True, status
    assert status["quarantined"] is True, status


def test_a_gdb_refusal_before_the_connect_through_stlink_server_still_says_which_way_it_reached_the_probe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The same refusal in shared mode: the bench is as it was, and the answer still names the companion.

    Here the refusal keeps what it claims: ST-LINK_gdbserver with `-t` holds no
    probe of its own, so ending it leaves nothing unaccounted for, and GDB was
    refused before `-target-select`. What a caller reads a start by is still
    which way it reached the probe, and `probe_server` is where that is said, on
    this answer as on every other one a start gives once the companion was
    chosen."""
    use_stlink_server(tmp_path, monkeypatch, str(FAKE_STLINK_SERVER))
    monkeypatch.setenv("FAKE_GDB_BEHAVIOR", MI_ASYNC_UNSUPPORTED)
    service, _ = st_link_session_service(tmp_path, monkeypatch, behavior=f"{ST_LINK_GDBSERVER}+{MI_ASYNC_UNSUPPORTED}")
    try:
        started = service.call("debug_start_session", {"image_path": "build/app.elf", "mode": "attach", "timeout_s": NEVER_READY_TIMEOUT_S})
        status = service.call("debug_get_session_status")
    finally:
        with contextlib.suppress(RuntimeError):
            service.close()
        service.coordinator.close()

    assert started["ok"] is False, started
    assert started["error_type"] == "gdb_async_unsupported", started
    assert "mi-async" in started["summary"], started
    assert started["target_contacted"] is False, started
    assert started["side_effect_committed"] is False, started
    assert started["side_effect_status"] == "not_started", started
    assert started["retry_safe"] is True, started
    assert started.get("cleanup_required") is not True, started
    assert started["probe_server"]["mode"] == "shared", started["probe_server"]
    assert started["probe_server"]["ended"] is True, started["probe_server"]
    assert status["active"] is False, status


def test_st_link_session_ends_its_stlink_server_only_half_a_second_after_the_gdb_server_is_gone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The wait that kept the next start from being refused.

    Ended at once after the GDB server's kill, stlink-server left the next start
    refused with `TCPCMD OPEN_DEV FAIL` in 6 of 40 recorded cycles. Ended after
    it had released the probe's USB, which was at most 9 ms after the kill, or
    half a second after the kill, it left none refused in 79. So the session
    waits half a second once the GDB server is gone before it ends stlink-server."""
    _, stlink_events = use_stlink_server(tmp_path, monkeypatch, str(FAKE_STLINK_SERVER))
    calls = recorded_terminations(monkeypatch)
    service, _ = st_link_session_service(tmp_path, monkeypatch)
    try:
        started = start_debug_session(service, mode="attach")
        assert started["ok"] is True, started
        stopped = service.call("debug_stop_session")
    finally:
        service.close()

    assert stopped["ok"] is True, stopped
    gdb_server_end = next(call for call in calls if call["which"] == "gdb_server")
    ends = [call for call in calls if call["which"] == "stlink_server"]
    assert len(ends) == 1, calls
    assert ends[0]["called_at"] - gdb_server_end["returned_at"] >= stlink_server.STLINK_SERVER_RELEASE_WAIT_S, calls
    disconnected = [event["at"] for event in server_events(stlink_events) if event["event"] == "client_disconnected"]
    assert disconnected and max(disconnected) < ends[0]["called_at"], (server_events(stlink_events), calls)
    assert stopped["session"]["probe_server"]["ended"] is True


def test_two_sessions_share_one_stlink_server_and_the_first_stop_leaves_it_to_the_second(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Two services, two probes, one stlink-server: the one that started it stops first, and the other carries on.

    The first service starts stlink-server and records it; the second finds it
    listening, recorded as this product's, and joins it. The first stop leaves
    it running, because the second session still reaches its probe through it,
    and that session halts, continues and stops as before. Its stop is the last
    one, so it ends stlink-server."""
    _, stlink_events = use_stlink_server(tmp_path, monkeypatch, str(FAKE_STLINK_SERVER))
    calls = recorded_terminations(monkeypatch)
    first, _ = st_link_session_service(tmp_path / "first", monkeypatch)
    second = None
    try:
        started_first = start_debug_session(first, mode="attach")
        assert started_first["ok"] is True, started_first
        second, _ = another_service(tmp_path, monkeypatch)
        started_second = start_debug_session(second, mode="attach")
        assert started_second["ok"] is True, started_second
        stopped_first = first.call("debug_stop_session")
        assert stopped_first["ok"] is True, stopped_first
        assert stlink.stlink_server_listening(stlink.STLINK_SERVER_PORT)
        assert second.call("debug_set_breakpoint", {"location": {"symbol": "test_done"}})["ok"] is True
        continued = second.call("debug_continue", {"timeout_s": 5})
        assert continued["stop_reason"] == "breakpoint_hit", continued
        assert second.call("debug_halt", {"timeout_s": 5})["ok"] is True
        stopped_second = second.call("debug_stop_session")
    finally:
        first.close()
        if second is not None:
            second.close()

    assert stopped_second["ok"] is True, stopped_second
    assert stopped_second["safe_state_confirmed"] is True, stopped_second
    assert [event["event"] for event in server_events(stlink_events)].count("started") == 1
    left = stopped_first["session"]["probe_server"]
    assert left["started_by_session"] is True, left
    assert left["ended"] is False, left
    assert left["other_sessions"] == 1, left
    assert left["left_running"], left
    joined = started_second["session"]["probe_server"]
    assert joined["mode"] == "shared", joined
    assert joined["started_by_session"] is False, joined
    assert joined["started_by_agentic_hil"] is True, joined
    ended = stopped_second["session"]["probe_server"]
    assert ended["ended"] is True, ended
    assert ended["returncode"] is not None, ended
    ends = [call for call in calls if call["which"] == "stlink_server"]
    gdb_server_ends = [call for call in calls if call["which"] == "gdb_server"]
    assert len(ends) == 1, calls
    # Each GDB server is asked to end more than once over a stop (a server already gone is a no-op), so its first end is the one that counts.
    gdb_servers = list({id(call["process"]): call["process"] for call in gdb_server_ends}.values())
    first_end_of_each = [next(index for index, call in enumerate(calls) if call["process"] is server) for server in gdb_servers]
    assert len(gdb_servers) == 2 and max(first_end_of_each) < calls.index(ends[0]), calls
    assert ends[0]["gdb_server_gone"] is True, calls
    assert not stlink.stlink_server_listening(stlink.STLINK_SERVER_PORT)


@pytest.mark.skipif(not CONNECTIONS_COUNTED, reason="this host's kernel publishes no table of open connections to read")
def test_stlink_server_another_program_is_connected_to_is_left_running_and_a_later_last_session_ends_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A connection this product did not make is a user it cannot see in its own record.

    stlink-server is there to be shared, so another tool may be reaching a probe
    through the one a session started. The last session's stop counts the
    connections still open to it and leaves it running while there are any,
    recorded as this product's; the next session that is the last one ends it."""
    port, stlink_events = use_stlink_server(tmp_path, monkeypatch, str(FAKE_STLINK_SERVER))
    calls = recorded_terminations(monkeypatch)
    service, _ = st_link_session_service(tmp_path, monkeypatch)
    other = None
    try:
        started = start_debug_session(service, mode="attach")
        assert started["ok"] is True, started
        connected = [event["event"] for event in server_events(stlink_events)].count("client_connected")
        other = socket.create_connection(("127.0.0.1", port), timeout=scaled_time_bound(5))
        wait_for_stlink_server_event(stlink_events, "client_connected", connected + 1)
        stopped = service.call("debug_stop_session")
        assert stopped["ok"] is True, stopped
        assert stlink.stlink_server_listening(port)
        other.close()
        other = None
        again = start_debug_session(service, mode="attach")
        assert again["ok"] is True, again
        stopped_again = service.call("debug_stop_session")
    finally:
        if other is not None:
            other.close()
        service.close()

    left = stopped["session"]["probe_server"]
    assert left["ended"] is False, left
    assert left["other_sessions"] == 0, left
    assert left["open_connections"] >= 1, left
    assert left["left_running"], left
    joined = again["session"]["probe_server"]
    assert joined["started_by_session"] is False, joined
    assert joined["started_by_agentic_hil"] is True, joined
    ended = stopped_again["session"]["probe_server"]
    assert ended["ended"] is True, ended
    assert ended["open_connections"] == 0, ended
    assert [event["event"] for event in server_events(stlink_events)].count("started") == 1
    assert len([call for call in calls if call["which"] == "stlink_server"]) == 1, calls
    assert not stlink.stlink_server_listening(port)


def test_where_open_connections_cannot_be_counted_the_record_of_this_products_sessions_decides(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A host whose kernel publishes no connection table: the record of this product's sessions decides alone.

    Left running across sessions, stlink-server was measured refusing every
    start from its tenth killed client on (`Target unknown error 33`, the
    kept_running block of the restart round), so a server no session can be
    shown to use is ended rather than kept. The record says the connections
    were not counted."""
    use_stlink_server(tmp_path, monkeypatch, str(FAKE_STLINK_SERVER))
    monkeypatch.setattr(stlink_server, "established_connections", lambda port: None)
    service, _ = st_link_session_service(tmp_path, monkeypatch)
    try:
        started = start_debug_session(service, mode="attach")
        assert started["ok"] is True, started
        stopped = service.call("debug_stop_session")
    finally:
        service.close()

    ended = stopped["session"]["probe_server"]
    assert ended["ended"] is True, ended
    assert ended["open_connections"] is None, ended
    assert not stlink.stlink_server_listening(stlink.STLINK_SERVER_PORT)


@pytest.mark.skipif(snapshot_process_images() is None, reason="this host publishes no process table to confirm a process by")
def test_the_last_session_ends_an_stlink_server_another_service_process_started(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The service that started stlink-server is gone by the time the last session using it stops.

    That session ends it by the process recorded for it, once it has confirmed
    that the running process with that number is the one that was started (its
    creation time), so a reused number is never ended."""
    _, stlink_events = use_stlink_server(tmp_path, monkeypatch, str(FAKE_STLINK_SERVER))
    first, _ = st_link_session_service(tmp_path / "first", monkeypatch)
    second = None
    try:
        started_first = start_debug_session(first, mode="attach")
        assert started_first["ok"] is True, started_first
        second, _ = another_service(tmp_path, monkeypatch)
        started_second = start_debug_session(second, mode="attach")
        assert started_second["ok"] is True, started_second
        assert first.call("debug_stop_session")["ok"] is True
        first.close()
        # What a later process knows of the server: its record, not the handle of the process that started it.
        monkeypatch.setattr(stlink_server, "_STARTED", {})
        stopped_second = second.call("debug_stop_session")
    finally:
        first.close()
        if second is not None:
            second.close()

    ended = stopped_second["session"]["probe_server"]
    assert ended["ended"] is True, ended
    assert not stlink.stlink_server_listening(stlink.STLINK_SERVER_PORT)
    started_pid = next(event["pid"] for event in server_events(stlink_events) if event["event"] == "started")
    assert all(image.pid != started_pid for image in snapshot_process_images() or ())


def test_st_link_start_stlink_server_could_not_open_the_probe_for_is_its_own_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`TCPCMD OPEN_DEV FAIL` in stlink-server's log is not a target that did not answer.

    ST-LINK_gdbserver words that start as `Failed to connect to device. Please
    check power and cabling to target.`, the line a target that is off gives
    too. stlink-server's own log names what happened: it could not open the
    in-circuit debugger or programmer for the GDB server. The session reads that
    log from where its start began, and the answer carries the catalogue's
    measured way out."""
    use_stlink_server(tmp_path, monkeypatch, str(FAKE_STLINK_SERVER))
    monkeypatch.setenv(STLINK_SERVER_SCENARIO_VARIABLE, OPEN_DEV_REFUSED)
    service, _ = st_link_session_service(tmp_path, monkeypatch)
    monkeypatch.setenv(ST_LINK_SCENARIO_VARIABLE, OPEN_REFUSAL_SCENARIO)
    try:
        started = service.call("debug_start_session", {"image_path": "build/app.elf", "mode": "attach", "timeout_s": START_TIMEOUT_S})
    finally:
        # The refused start keeps its session for cleanup (below), and a service closed
        # over such a session says so by raising.
        with contextlib.suppress(RuntimeError):
            service.close()
        service.coordinator.close()

    assert started["ok"] is False, started
    assert started["error_type"] == "probe_server_open_failed", started
    assert started["backend_error_type"] == "probe_server_open_failed", started
    assert started["backend_error"] == OPEN_DEV_LINE, started
    assert started["remediation"], started
    assert started["remediation"] == remediation_fields("probe_server_open_failed", "stlink")["remediation"], started
    # The log says that an open failed, not that nothing this start sent before
    # it got through, and the server's own last line is `Device connect error`:
    # nothing places the end before any contact, so the start claims none.
    assert started["side_effect_status"] == "unknown", started
    assert started["retry_safe"] is False, started
    assert started["cleanup_required"] is True, started
    assert "target_contacted" not in started, started
    assert not stlink.stlink_server_listening(stlink.STLINK_SERVER_PORT)


def test_a_refusal_already_in_the_stlink_server_log_is_not_read_as_a_later_starts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Only what stlink-server wrote since a session's start began counts for that start.

    An `OPEN_DEV FAIL` line another client met earlier in the shared log says
    nothing about a later start that failed for its own reason, which keeps the
    GDB server's own classification: a probe another server holds, recorded as
    `Failed to connect to device`, is `target_not_detected`."""
    port, stlink_events = use_stlink_server(tmp_path, monkeypatch, str(FAKE_STLINK_SERVER))
    monkeypatch.setenv(STLINK_SERVER_SCENARIO_VARIABLE, OPEN_DEV_REFUSED)
    first, _ = st_link_session_service(tmp_path / "first", monkeypatch)
    second = None
    other = None
    try:
        started_first = start_debug_session(first, mode="attach")
        assert started_first["ok"] is True, started_first
        # Another program asks stlink-server for a probe and is refused, so the log holds the line.
        other = socket.create_connection(("127.0.0.1", port), timeout=scaled_time_bound(5))
        other.sendall(b"open" + bytes([10]))
        wait_for_stlink_server_event(stlink_events, "open_refused")
        other.close()
        other = None
        second, _ = another_service(tmp_path, monkeypatch)
        monkeypatch.setenv(ST_LINK_SCENARIO_VARIABLE, "probe_already_held_by_another_server")
        started_second = second.call("debug_start_session", {"image_path": "build/app.elf", "mode": "attach", "timeout_s": START_TIMEOUT_S})
        assert first.call("debug_stop_session")["ok"] is True
    finally:
        if other is not None:
            other.close()
        first.close()
        if second is not None:
            # Its refused start keeps the session for cleanup, which its close reports by raising.
            with contextlib.suppress(RuntimeError):
                second.close()
            second.coordinator.close()

    assert started_second["ok"] is False, started_second
    assert started_second["error_type"] == "target_not_detected", started_second


def test_the_recorded_server_ends_back_the_wait_and_the_way_out_of_a_refused_open() -> None:
    """The committed rounds behind the half second and behind the catalogue's numbers.

    Ended at once, stlink-server left the next start refused in 6 of 40 cycles;
    ended after the probe's USB was released or after half a second, in none of
    79, and the USB was released well inside half a second in every cycle that
    measured it. After every start refused with `OPEN_DEV FAIL` in either round,
    the next start came up."""
    ends = json.loads(SERVER_ENDS_RECORDING.read_text(encoding="utf-8"))
    summary = ends["summary"]
    at_once = summary["at_once"]
    waited = [summary["after_the_usb_released"], summary["after_half_a_second"]]
    assert stlink_server.STLINK_SERVER_RELEASE_WAIT_S == 0.5
    assert (len(at_once["next_starts_refused_in_cycles"]), at_once["next_starts"]) == (6, 40), at_once
    assert all(variant["next_starts_refused_in_cycles"] == [] for variant in waited), waited
    assert sum(variant["next_starts"] for variant in waited) == 79, waited
    released = [ms for variant in waited for ms in variant["usb_released_after_ms"] if ms is not None]
    assert released and max(released) < stlink_server.STLINK_SERVER_RELEASE_WAIT_S * 1000, released

    came_up: list[bool] = []
    restarts = json.loads(RESTARTS_RECORDING.read_text(encoding="utf-8"))["summary"]
    for block in restarts.values():
        if isinstance(block, dict) and "after_each_refusal" in block:
            refused_to_open = set(block["probe_server_open_dev_fail_in_cycles"])
            came_up.extend(entry["next_came_up"] for entry in block["after_each_refusal"] if entry["cycle"] in refused_to_open)
    cycles = ends["scenarios"]["server_ends"]
    for index, cycle in enumerate(cycles):
        if cycle["ready_at_s"] is None and any("OPEN_DEV FAIL" in str(line["line"]) for line in cycle["probe_server_output"] or []):
            came_up.append(cycles[index + 1]["ready_at_s"] is not None)
    assert came_up and all(came_up), came_up

    remedy = " ".join(remediation_fields("probe_server_open_failed", "stlink")["remediation"])
    assert f"{len(came_up)} of {len(came_up)}" in remedy, remedy
    assert "6 of 40" in remedy, remedy
    assert "0 of 79" in remedy, remedy


def test_open_connections_are_read_from_the_kernels_table_as_the_container_recorded_it() -> None:
    """The /proc/net/tcp lines recorded in the bench container before each end: stlink-server's listening socket and closed clients only.

    Neither is a connection anybody still holds: the listening socket is the
    server's own, and a client's side in TIME_WAIT is already closed."""
    cycles = json.loads(SERVER_ENDS_RECORDING.read_text(encoding="utf-8"))["scenarios"]["server_ends"]
    lines = [line for cycle in cycles for line in cycle.get("tcp_lines_before_the_end") or []]
    assert any(line.split()[3] == "0A" for line in lines) and any(line.split()[3] == "06" for line in lines), lines

    assert stlink_server.established_server_side(lines, 7184) == 0
