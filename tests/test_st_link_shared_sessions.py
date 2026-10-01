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
from pathlib import Path

import pytest
from fixtures.fake_st_link_gdbserver import SHARED_PORT_VARIABLE
from fixtures.fake_stlink_server import EVENTS_VARIABLE as STLINK_SERVER_EVENTS_VARIABLE
from fixtures.fake_stlink_server import PORT_VARIABLE as STLINK_SERVER_PORT_VARIABLE
from support import scaled_time_bound
from test_debug_sessions import start_debug_session
from test_gdbserver_sessions import server_events, session_log, st_link_session_service

from agentic_hil.backends import gdbdebug, stlink

FAKE_STLINK_SERVER = Path(__file__).parent / "fixtures" / "fake_stlink_server.py"


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
    """A found stlink-server that ends before its port listens: the session says how it ended and runs as one without it."""
    exits = tmp_path / "exits.py"
    exits.write_text("import sys\nprint('no listening here', file=sys.stderr)\nsys.exit(3)\n", encoding="utf-8")
    use_stlink_server(tmp_path, monkeypatch, str(exits))
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
    assert "no listening here" in direct["output_tail"], direct


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
