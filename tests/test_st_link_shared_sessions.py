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
from fixtures.fake_st_link_gdbserver import SHARED_PORT_VARIABLE
from fixtures.fake_stlink_server import EVENTS_VARIABLE as STLINK_SERVER_EVENTS_VARIABLE
from fixtures.fake_stlink_server import PORT_VARIABLE as STLINK_SERVER_PORT_VARIABLE
from support import scaled_time_bound
from test_debug_sessions import start_debug_session
from test_gdbserver_sessions import server_events, session_log, st_link_session_service

from agentic_hil.backends import gdbdebug, stlink

FAKE_STLINK_SERVER = Path(__file__).parent / "fixtures" / "fake_stlink_server.py"
# The deadline of a start whose server never says its port listens. The fake has
# printed every other recorded startup line long before it runs out, so a start
# that took one of them for the ready line would have connected already.
NEVER_READY_TIMEOUT_S = scaled_time_bound(2.0)


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
    """Every process tree the session layer ends, in order, with whether the GDB server was gone by then."""
    terminate = gdbdebug.terminate_process_tree
    calls: list[dict] = []

    def recorded(process, timeout_s, **kwargs):
        gdb_servers = [call["process"] for call in calls if call["which"] == "gdb_server"]
        which = "stlink_server" if "fake_stlink_server" in " ".join(map(str, process.args)) else "gdb_server"
        calls.append({"which": which, "process": process, "graceful": kwargs.get("graceful", True), "gdb_server_gone": all(server.poll() is not None for server in gdb_servers) and bool(gdb_servers)})
        return terminate(process, timeout_s, **kwargs)

    monkeypatch.setattr(gdbdebug, "terminate_process_tree", recorded)
    return calls


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


@pytest.mark.skipif(os.name != "nt", reason="the Windows end: each process ends with its Job Object")
def test_windows_stop_ends_the_gdb_servers_job_before_the_stlink_servers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Windows has no SIGKILL and no SIGTERM: the end there is each tree's Job Object terminated, the GDB server's first.

    That is a kill for both. stlink-server died of the signal on SIGTERM
    without a shutdown of its own (exit status -15, recorded), so its Job ending
    is the same end; the order is what keeps the GDB server from being cut off
    mid-transfer by the server it reaches the probe through."""
    use_stlink_server(tmp_path, monkeypatch, str(FAKE_STLINK_SERVER))
    from agentic_hil import process as process_module

    terminate_job = process_module._terminate_windows_job
    ended: list[int] = []

    def recorded(handle: int) -> None:
        ended.append(handle)
        terminate_job(handle)

    monkeypatch.setattr(process_module, "_terminate_windows_job", recorded)
    service, _ = st_link_session_service(tmp_path, monkeypatch)
    try:
        started = start_debug_session(service, mode="attach")
        assert started["ok"] is True, started
        session = service.backend._debug.session
        gdb_server_job = session.server._agentic_hil_job_handle
        stlink_server_job = session.companion.process._agentic_hil_job_handle
        assert isinstance(gdb_server_job, int) and isinstance(stlink_server_job, int)
        stopped = service.call("debug_stop_session")
    finally:
        with contextlib.suppress(RuntimeError):
            service.close()

    assert stopped["ok"] is True, stopped
    assert ended[:2] == [gdb_server_job, stlink_server_job], ended
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
