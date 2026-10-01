"""Typed debug sessions over each backend's own GDB server (#624).

The session layer was written against OpenOCD's GDB server, and three of its
steps spoke OpenOCD: the reset into halt, the guard that keeps the core halted
when GDB lets go, and the line and words a starting server is read by. Those
steps are now each backend's to answer. The first half of this file holds
OpenOCD to the exact command line and the exact GDB/MI commands it was sent
before that split, so moving the steps changed nothing on the one backend that
already had sessions. The rest holds the other servers to their own recordings.
"""

from __future__ import annotations

import contextlib
import json
from pathlib import Path

import pytest
from conftest import write_config
from fixtures.fake_gdb import PYOCD_GDBSERVER, PYOCD_RESET_UNCONFIRMED
from fixtures.fake_pyocd import (
    CONSOLE_OFF_OPTION,
    GDBSERVER_EVENTS_VARIABLE,
    GDBSERVER_READY_WORDS,
    GDBSERVER_RECORDING,
    GDBSERVER_SCENARIO_VARIABLE,
    GDBSERVER_STARTUP_SCENARIO,
)
from support import scaled_time_bound
from test_debug_sessions import BOOT_COUNTER_SYMBOL_TABLE, START_TIMEOUT_S, debug_service, start_debug_session

from agentic_hil.backends import gdbdebug
from agentic_hil.backends.common import invocation
from agentic_hil.backends.pyocd import SESSIONLESS_DEBUG_READS, pack_install_commands
from agentic_hil.config import load_config, resolve_work_path

IMAGE = "<image>"
PORT = "<port>"
OPENOCD_RESET_HALT = '-interpreter-exec console "monitor reset halt"'
OPENOCD_DETACH_GUARD = '-interpreter-exec console "monitor $_TARGETNAME configure -event gdb-detach {}; $_TARGETNAME configure -event gdb-end {}"'
OPENOCD_SESSION_PROLOGUE = [
    "-gdb-set pagination off",
    "-gdb-set confirm off",
    "-gdb-set mi-async on",
    f"-file-exec-and-symbols {IMAGE}",
    f"-target-select extended-remote localhost:{PORT}",
]


def session_log(service, started: dict) -> dict:
    return json.loads((Path(service.config.work_dir) / started["log_path"]).read_text(encoding="utf-8"))


def sent_commands(service, started: dict) -> list[str]:
    """The GDB/MI commands the session sent, in order, with the two values a run chooses replaced.

    The image is staged under a fresh directory and the port is reserved per
    start, so each is named by a placeholder; every other byte is compared.
    """
    port = str(started["gdb_port"])
    commands = []
    for entry in session_log(service, started)["gdb_commands"]:
        command = str(entry["command"])
        if command.startswith("-file-exec-and-symbols "):
            command = f"-file-exec-and-symbols {IMAGE}"
        commands.append(command.replace(f"localhost:{port}", f"localhost:{PORT}"))
    return commands


def run_breakpoint_cycle(service, mode: str) -> dict:
    started = start_debug_session(service, mode=mode)
    assert started["ok"] is True, started
    assert service.call("debug_set_breakpoint", {"location": {"symbol": "test_done"}})["ok"] is True
    continued = service.call("debug_continue", {"timeout_s": 5})
    assert continued["stop_reason"] == "breakpoint_hit", continued
    assert service.call("debug_halt", {"timeout_s": 5})["ok"] is True
    stopped = service.call("debug_stop_session")
    assert stopped["ok"] is True, stopped
    assert stopped["safe_state_confirmed"] is True
    assert stopped["detach_resume_guard_confirmed"] is True
    return started


@pytest.mark.parametrize(
    ("mode", "after_connect"),
    [
        ("attach", []),
        ("reset_halt", [OPENOCD_RESET_HALT]),
        ("load", [OPENOCD_RESET_HALT, "-target-download", OPENOCD_RESET_HALT]),
    ],
)
def test_openocd_sessions_send_the_same_gdb_mi_commands_as_before_the_server_hooks(tmp_path: Path, mode: str, after_connect: list[str]) -> None:
    service = debug_service(tmp_path)
    try:
        started = run_breakpoint_cycle(service, mode)
    finally:
        service.close()

    assert sent_commands(service, started) == [
        *OPENOCD_SESSION_PROLOGUE,
        *after_connect,
        '-break-insert "test_done"',
        "-exec-continue",
        OPENOCD_DETACH_GUARD,
        "-gdb-exit",
    ]


def test_openocd_session_containment_and_teardown_send_the_same_commands_as_before_the_server_hooks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A continue that runs out its time and an explicit halt on a running target, the two paths that interrupt."""
    monkeypatch.setenv("FAKE_GDB_BEHAVIOR", "bench_run_state")
    service = debug_service(tmp_path, fake_gdb_behavior="bench_run_state")
    try:
        started = start_debug_session(service, mode="attach")
        assert started["ok"] is True, started
        continued = service.call("debug_continue", {"timeout_s": 0.5})
        assert continued["ok"] is False, continued
        assert continued["stop_reason"] == "timeout", continued
        assert continued["halt_confirmed"] is True, continued
        stopped = service.call("debug_stop_session")
        assert stopped["ok"] is True, stopped
    finally:
        service.close()

    assert sent_commands(service, started) == [
        *OPENOCD_SESSION_PROLOGUE,
        "-exec-continue",
        "-exec-interrupt --all",
        OPENOCD_DETACH_GUARD,
        "-gdb-exit",
    ]


@pytest.mark.parametrize(("mode", "startup"), [("attach", "init; halt"), ("reset_halt", "init; reset halt"), ("load", "init; reset halt")])
def test_openocd_debug_server_command_line_is_unchanged_by_the_server_hooks(tmp_path: Path, mode: str, startup: str) -> None:
    service = debug_service(tmp_path)
    try:
        started = run_breakpoint_cycle(service, mode)
    finally:
        service.close()

    config = service.config
    executable = resolve_work_path(config, config.debugger.executable)
    assert session_log(service, started)["server_command"] == [
        *invocation(str(executable)),
        "-f",
        "interface/stlink.cfg",
        "-f",
        "target/stm32f4x.cfg",
        "-c",
        "bindto 127.0.0.1",
        "-c",
        f"gdb_port {started['gdb_port']}",
        "-c",
        "tcl_port disabled",
        "-c",
        "telnet_port disabled",
        "-c",
        startup,
    ]


# --- pyOCD: sessions through `pyocd gdbserver` --------------------------------
#
# The server is the fake in tests/fixtures/fake_pyocd.py, which prints what
# pyOCD 0.45.1 printed on the reference board, and the GDB is the fake in
# fake_gdb.py answering with what that board's GDB was answered. Three things
# differ from OpenOCD and are each held here: the server is told its port, its
# probe and its target and starts nothing else that listens; the reset into halt
# is pyOCD's `monitor reset halt`, believed only when pyOCD says the core halted;
# and pyOCD has no command that keeps the core halted once GDB lets go (the
# recorded GDB that exited left it running), so the session ends the server
# before GDB detaches and reports the guard confirmed only when the server is
# gone.

PYOCD_UID = "PYOCD123"
PYOCD_TARGET = "stm32f446retx"
PYOCD_RESET_HALT = '-interpreter-exec console "monitor reset halt"'


def pyocd_recording() -> dict:
    return json.loads(GDBSERVER_RECORDING.read_text(encoding="utf-8"))


def pyocd_session_service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, behavior: str = PYOCD_GDBSERVER, **config_kwargs):
    """A pyOCD bench with the fake server and the fake GDB replaying the recording.

    Returns the service and the file the fake server writes its events to: its
    start with the arguments it was given, the port it listened on, and every
    client it accepted and lost."""
    events = tmp_path / "pyocd-gdbserver-events.jsonl"
    monkeypatch.setenv(GDBSERVER_EVENTS_VARIABLE, str(events))
    monkeypatch.delenv(GDBSERVER_SCENARIO_VARIABLE, raising=False)
    config_kwargs.setdefault("probe_id", PYOCD_UID)
    service = debug_service(tmp_path, fake_gdb_behavior=behavior, debugger_type="pyocd", target_type=PYOCD_TARGET, **config_kwargs)
    return service, events


def server_events(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def close_unsettled(service) -> None:
    """Close a service whose session was left unsettled on purpose.

    Closing one is refused with a RuntimeError naming what is unconfirmed; that
    refusal is the product's and is asserted elsewhere."""
    with contextlib.suppress(RuntimeError):
        service.close()


@pytest.mark.parametrize("mode", ["attach", "reset_halt", "load"])
def test_pyocd_debug_server_is_started_on_the_configured_probe_and_target_at_the_reserved_port(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    """The recorded command line, for every mode.

    The configured executable, the probe's full UID as enumeration resolved it,
    the configured target, `-W` so an unplugged probe is an answer rather than a
    wait, and the port the start reserved. `semihost_console_type=off` because
    without it the recorded server also listened on 4444, a fixed port another
    session or another tool may hold. The reset is a GDB command in every mode
    rather than a server option, so the line is the same for all three."""
    service, _ = pyocd_session_service(tmp_path, monkeypatch)
    try:
        started = run_breakpoint_cycle(service, mode)
    finally:
        service.close()

    config = service.config
    executable = Path(resolve_work_path(config, config.debugger.executable))
    assert session_log(service, started)["server_command"] == [
        *invocation(str(executable)),
        "gdbserver",
        "--port",
        str(started["gdb_port"]),
        "-O",
        CONSOLE_OFF_OPTION,
        "--uid",
        PYOCD_UID,
        "--target",
        PYOCD_TARGET,
        "-W",
    ]


@pytest.mark.parametrize(
    ("mode", "after_connect"),
    [
        ("attach", []),
        ("reset_halt", [PYOCD_RESET_HALT]),
        ("load", [PYOCD_RESET_HALT, "-target-download", PYOCD_RESET_HALT]),
    ],
)
def test_pyocd_sessions_send_no_openocd_command(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str, after_connect: list[str]) -> None:
    """The GDB/MI commands a session over pyOCD sends, and nothing of OpenOCD's.

    OpenOCD's detach guard is a Tcl event handler pyOCD has no word for: pyOCD
    answers an unknown monitor command with `^done` after an `Error:` record
    (scenario `unknown_monitor_command`), so sending it would have looked like
    success. No command takes its place, because the guard here is the server
    ending before GDB does."""
    service, _ = pyocd_session_service(tmp_path, monkeypatch)
    try:
        started = run_breakpoint_cycle(service, mode)
    finally:
        service.close()

    assert sent_commands(service, started) == [
        *OPENOCD_SESSION_PROLOGUE,
        *after_connect,
        '-break-insert "test_done"',
        "-exec-continue",
        "-gdb-exit",
    ]


def test_pyocd_session_ends_the_server_before_gdb_detaches_and_says_so(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The server is gone before GDB could let the core run.

    The recorded GDB that exited while pyOCD served it left the core running
    (scenario `session_ended_by_gdb_exit`), and the fake server records that as
    `resumes_core`. So the server must never see its client leave: it is ended
    first, and its events stop at the one client it accepted. That one client
    is GDB's, which is also the proof the start waited for the ready line rather
    than connecting to look: pyOCD serves one client and then exits, so a
    connect that checked the port would have ended the server it checked."""
    service, events_path = pyocd_session_service(tmp_path, monkeypatch)
    try:
        started = run_breakpoint_cycle(service, "reset_halt")
    finally:
        service.close()

    assert [event["event"] for event in server_events(events_path)] == ["started", "listening", "client_connected"]
    guard = session_log(service, started)["detach_guard"]
    assert guard["kind"] == "server_terminated_before_gdb_detach", guard
    assert guard["server_exited"] is True, guard


@pytest.mark.parametrize("mode", ["reset_halt", "load"])
def test_pyocd_reset_that_does_not_report_the_core_halted_is_not_taken_as_one(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    """`^done` alone proves nothing on pyOCD; its `Successfully halted` line does.

    pyOCD answers `monitor reset halt` with `^done` whether or not the core
    halted, and says which in a console record. A reset without the line is a
    target in an unknown state, so the start stops there, sends no download,
    and keeps the bench for cleanup."""
    service, _ = pyocd_session_service(tmp_path, monkeypatch, behavior=f"{PYOCD_GDBSERVER}+{PYOCD_RESET_UNCONFIRMED}")
    try:
        started = start_debug_session(service, mode=mode)
        log = session_log(service, started)
    finally:
        close_unsettled(service)

    assert started["ok"] is False, started
    assert started["error_type"] == "debugger_error", started
    assert started["backend_error_type"] == "reset_halt_not_confirmed", started
    assert "Failed to halt device on reset (state is RUNNING)" in started["summary"], started
    assert started["load_phase"] == "pre_load_reset_started", started
    assert started["side_effect_status"] == "unknown", started
    assert started["cleanup_required"] is True, started
    assert "-target-download" not in [entry["command"] for entry in log["gdb_commands"]]


def test_pyocd_reset_halt_session_does_not_report_the_connect_stop_as_its_own(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The stop GDB reports for the connect is before the reset, so it is not the session's stop.

    pyOCD halts the core when GDB connects and GDB reports that stop, in the
    function the core was running (`delay` in the recording). The reset then
    moves the core to the reset vector without a stop record of its own. Taken
    as the session's stop, `debug_halt` would name a place the core no longer
    is; drained, the session answers the way a reset-halted start on OpenOCD
    does, and the record stays in the log."""
    service, _ = pyocd_session_service(tmp_path, monkeypatch)
    try:
        started = start_debug_session(service, mode="reset_halt")
        assert started["ok"] is True, started
        halted = service.call("debug_halt", {"timeout_s": 5})
        log = session_log(service, started)
        assert service.call("debug_stop_session")["ok"] is True
    finally:
        service.close()

    assert started["session"]["stop_reason"] is None, started
    assert halted["ok"] is True, halted
    assert halted["stop_reason"] == "halted", halted
    assert halted["stop"]["backend_stop_reason"] == "session_start", halted
    assert any('func="delay"' in record for record in log["gdb_stop_records"]), log["gdb_stop_records"]


def test_pyocd_session_whose_server_outlives_the_guard_is_not_reported_safe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The guard is confirmed by the server having exited, not by having asked it to.

    The first terminate is made to do nothing, so the server is still serving
    GDB when the session would detach. That is the case the guard exists for,
    and it is answered with the unconfirmed-teardown vocabulary OpenOCD's guard
    uses when its own command fails."""
    terminate = gdbdebug.terminate_process_tree
    calls: list[object] = []

    def first_terminate_does_nothing(process, *args, **kwargs):
        calls.append(process)
        if len(calls) == 1:
            return None
        return terminate(process, *args, **kwargs)

    monkeypatch.setattr(gdbdebug, "terminate_process_tree", first_terminate_does_nothing)
    service, _ = pyocd_session_service(tmp_path, monkeypatch)
    try:
        started = start_debug_session(service, mode="reset_halt")
        assert started["ok"] is True, started
        stopped = service.call("debug_stop_session")
    finally:
        close_unsettled(service)

    assert stopped["ok"] is False, stopped
    assert stopped["error_type"] == "detach_resume_not_confirmed", stopped
    assert stopped["detach_resume_guard_confirmed"] is False, stopped
    assert stopped["hardware_state"] == "unknown", stopped


def recorded_failure(scenario: str) -> dict:
    return pyocd_recording()["scenarios"][scenario]


@pytest.mark.parametrize("mode", ["attach", "reset_halt"])
@pytest.mark.parametrize(
    ("scenario", "error_type", "decisive_line"),
    [
        ("unknown_probe_uid", "adapter_not_found", "No connected debug probe matches unique ID 'AGENTICHILNOSUCHPROBE0'"),
        ("probe_already_held_by_another_server", "adapter_not_found", "0000577 C Error: [Errno 16] Resource busy [__main__]"),
        ("unknown_target_type", "target_type_invalid", None),
    ],
)
def test_pyocd_server_that_exits_at_startup_is_classified_from_its_recorded_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str, scenario: str, error_type: str, decisive_line: str | None) -> None:
    """Each recorded way pyOCD's server refused to start, read from its own line.

    Every one of them exits before the probe carries anything: no probe by that
    UID, a probe another server holds (pyOCD 0.45.1 prints only `[Errno 16]
    Resource busy` for it), and a target type no installed pack provides. The
    start ends when the server does, names the line, and refuses rather than
    quarantining a board nothing reached. A reset mode is no different here,
    because pyOCD's reset is a GDB command the start never got to send."""
    recorded = recorded_failure(scenario)
    if decisive_line is None:
        decisive_line = recorded["output"][-1]["line"]
    service, events_path = pyocd_session_service(tmp_path, monkeypatch)
    monkeypatch.setenv(GDBSERVER_SCENARIO_VARIABLE, scenario)
    try:
        started = service.call("debug_start_session", {"image_path": "build/app.elf", "mode": mode, "timeout_s": START_TIMEOUT_S})
        status = service.call("debug_get_session_status")
    finally:
        service.close()

    assert started["ok"] is False, started
    assert started["error_type"] == error_type, started
    assert started["backend_error"] == decisive_line, started
    assert started["summary"].startswith("Debug server exited before the GDB port became ready"), started
    assert started["elapsed_ms"] < scaled_time_bound(START_TIMEOUT_S * 1000 / 2), started
    assert started["target_contacted"] is False, started
    assert started["side_effect_status"] == "not_started", started
    assert started["retry_safe"] is True, started
    assert started.get("cleanup_required") is not True, started
    assert status["active"] is False, status
    if error_type == "target_type_invalid":
        assert started["install_commands"] == pack_install_commands(PYOCD_TARGET), started
    assert [event["event"] for event in server_events(events_path)] == ["started"]


def test_pyocd_session_without_the_executable_starts_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service, events_path = pyocd_session_service(tmp_path, monkeypatch, debugger_executable=tmp_path / "missing" / "pyocd")
    try:
        started = start_debug_session(service, mode="attach")
    finally:
        service.close()

    assert started["ok"] is False, started
    assert started["error_type"] == "debugger_not_found", started
    assert started["target_contacted"] is False, started
    assert server_events(events_path) == []


def test_pyocd_session_on_a_probe_id_no_connected_probe_matches_starts_no_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The UID is resolved by enumeration before the server is started, as every other pyOCD call resolves it."""
    service, events_path = pyocd_session_service(tmp_path, monkeypatch, probe_id="NOSUCHPROBE")
    try:
        started = start_debug_session(service, mode="attach")
    finally:
        service.close()

    assert started["ok"] is False, started
    assert started["error_type"] == "adapter_not_found", started
    assert started["target_contacted"] is False, started
    assert server_events(events_path) == []


def test_pyocd_symbol_reads_run_inside_an_open_session_and_standalone_without_one(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """With a session open, a read goes through it; with none, the standalone read is unchanged.

    A standalone read is a second pyOCD process opening the probe the session's
    server holds, which is the `Resource busy` refusal recorded above. So while
    a session runs the backend serves no read without it, and the read is
    answered by the session's GDB from the image the session loaded."""
    service, _ = pyocd_session_service(tmp_path, monkeypatch, elf_symbols=BOOT_COUNTER_SYMBOL_TABLE)
    try:
        before = service.backend.sessionless_debug_tools()
        started = start_debug_session(service, mode="attach")
        assert started["ok"] is True, started
        during = service.backend.sessionless_debug_tools()
        value = service.call("debug_symbol_value", {"symbol": "boot_counter"})
        assert service.call("debug_stop_session")["ok"] is True
        after = service.backend.sessionless_debug_tools()
    finally:
        service.close()

    assert before == SESSIONLESS_DEBUG_READS
    assert during == frozenset()
    assert after == SESSIONLESS_DEBUG_READS
    assert value["ok"] is True, value
    assert value["session"]["session_id"] == started["session"]["session_id"], value
    assert "symbol_source" not in value, value


@pytest.mark.parametrize(("debugger_type", "opens"), [("openocd", True), ("pyocd", True), ("stlink", False)])
def test_which_configured_backends_open_debug_sessions(tmp_path: Path, debugger_type: str, opens: bool) -> None:
    from agentic_hil.tools import configured_opens_debug_sessions

    config = load_config(str(write_config(tmp_path, debugger_type=debugger_type, target_type=PYOCD_TARGET if debugger_type == "pyocd" else None)))

    assert configured_opens_debug_sessions(config) is opens


def test_pyocd_ready_line_is_the_recorded_line_for_the_reserved_port_only() -> None:
    """pyOCD's ready line ends with its core and logger, so it is matched as words, bounded at the port.

    `GDB server listening on port 51409 (core 0) [gdbserver]` is the recorded
    line. A port that is a prefix of the recorded one must not match, or a start
    could take another server's line for its own."""
    from agentic_hil.backends.gdbdebug import PYOCD_GDB_SERVER_STEPS

    startup = pyocd_recording()["scenarios"][GDBSERVER_STARTUP_SCENARIO]
    port = int(startup["gdb_port"])
    line = next(entry["line"] for entry in startup["output"] if GDBSERVER_READY_WORDS in entry["line"])
    served = pyocd_recording()["scenarios"]["session_ended_by_gdb_exit"]["server_after_gdb_exit"]["output"]
    connected = next(entry["line"] for entry in served if "connected on port" in entry["line"])
    connected_port = int(connected.split("connected on port ", 1)[1].split()[0])

    assert PYOCD_GDB_SERVER_STEPS.is_ready_line(line, port) is True
    assert PYOCD_GDB_SERVER_STEPS.is_ready_line(line, int(str(port)[:-1])) is False
    assert PYOCD_GDB_SERVER_STEPS.is_ready_line(connected, connected_port) is False
