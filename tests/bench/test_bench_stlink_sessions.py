"""Typed debug sessions on the STM32CubeProgrammer backend, driven over the MCP stdio server against the board.

The same probe this tier configures for OpenOCD is put on `type: stlink` here,
with the STM32_Programmer_CLI of the STM32CubeCLT tree that
`AGENTIC_HIL_BENCH_CUBECLT` names, in a copy of the tier's configuration that this
module writes and removes. `debuggers.<name>.gdb_server_executable` is left
unset, so the ST-LINK_gdbserver the sessions run is the one the product finds by
itself beside that CLI. The session tools are driven through `agentic-hil
mcp-stdio` exactly as an agent drives them; behind them the product runs
ST-LINK_gdbserver and a GDB of its own. Nothing in this file starts either, opens
the probe or speaks to the board.

Every test here carries the `cubeclt` mark: where no STM32CubeCLT tree is named,
on a bench run directly without one or in a bench image built without
`--cubeclt-archive`, the module is deselected, never skipped.

What only the board can say about a session on this backend:

* whether `monitor reset` through ST-LINK_gdbserver takes the core to the reset
  vector and leaves it halted, and whether the line the product reads as the
  proof of it is printed by the server this bench runs.
* whether a breakpoint set through the server is the one the core stops on, and
  whether a resume that nothing stops is interrupted and contained.
* whether ending a session leaves the core halted. ST-LINK_gdbserver resumes the
  core when its GDB client leaves and when it is asked to stop, and the product
  kills the server under a connected GDB instead; that this keeps the core
  halted is a claim about the probe and the core, and the counter the demo's
  SysTick drives is what proves it: read before the session ends, read again
  after a pause through a new session, and moved by no more than the next
  connect itself moves it.
* whether a server that ends with a session open gives the probe back, so the
  next server can open one.

The module ends with the demo put back on the board and running, through the
tier's own plan runner, because a session ends with the core held halted and the
modules after this one expect a board that runs the demo.

Nothing here names a bench. The probe, the executable and the session's values
are read out of the product's own answers and asserted on their shape.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
import yaml

from .conftest import BENCH_ONLY, CUBECLT_ENV, Bench, put_on_board
from .test_bench_debug_sessions import McpServer, blocking_incident, workspace_image

pytestmark = [pytest.mark.bench, BENCH_ONLY, pytest.mark.cubeclt]

# Where STM32CubeCLT puts the two programs inside its tree, and what the server
# found beside the CLI has to be.
PROGRAMMER_CLI = Path("STM32CubeProgrammer") / "bin" / "STM32_Programmer_CLI"
GDB_SERVER = Path("STLink-gdb-server") / "bin" / "ST-LINK_gdbserver"
# The interrupt handler the demo's millisecond counter is incremented in, and
# the counter itself. The handler runs every millisecond once the firmware has
# started its SysTick, so a breakpoint on it is reached within one.
HANDLER = "SysTick_Handler"
COUNTER = "uptime_ms"
# A resume that a breakpoint ends needs room for the round trip only; one that
# nothing ends is meant to run out, and the core runs free for that long.
REACHABLE_STOP_TIMEOUT_S = 15.0
UNREACHABLE_STOP_TIMEOUT_S = 3.0
# How long the core is left alone between the end of one session and the start
# of the next. The demo's counter moves by a thousand a second on a running
# core, so a core that was resumed in between reads at least this many
# milliseconds further on.
SETTLE_S = 2.0
# What the next session's own connect may move the counter by. With `-g` the
# server halts the core where it runs when GDB connects, and the core left
# halted by a killed server read 0 and then 1 ms further on through the next
# connects in tests/fixtures/st_link_gdbserver_7_14_0_linux_teardown_recordings.json
# (scenario `session_ended_by_killing_the_server`). The bound is the pyOCD
# module's, a quarter of the pause, so a core left running through it cannot
# pass for one the connect moved.
NEXT_CONNECT_RUN_BOUND_MS = 500
# How the product records the end of an ST-LINK_gdbserver session: the server is
# killed under a connected GDB, because the server resumes the core when GDB
# leaves and when it is asked to stop.
SERVER_KILLED_BEFORE_DETACH = "server_killed_before_gdb_detach"


def said(answer: object) -> str:
    """The whole answer, for an assertion message: pytest shortens anything that is not a string, and the decisive line is deep in it."""
    return json.dumps(answer, indent=1, sort_keys=True, default=str)


def cubeclt_root() -> Path:
    """The STM32CubeCLT tree this run was given; the `cubeclt` mark deselects the module without one."""
    return Path(os.environ[CUBECLT_ENV])


@pytest.fixture(scope="module")
def stlink_bench(bench: Bench) -> Iterator[Bench]:
    """A copy of the tier's configuration with the bound debugger on `type: stlink`.

    The probe, its permissions and the debug section are the tier's own; the
    backend, its executable and the interface are what changes, and no GDB
    server is named. The copy sits beside the configuration `init` wrote, under
    this session's own root, and is removed afterwards. The tier's own
    configuration is never written."""
    executable = cubeclt_root() / PROGRAMMER_CLI
    assert executable.is_file(), f"{CUBECLT_ENV} names a tree without {PROGRAMMER_CLI.as_posix()}"
    document = bench.configuration()
    entry = document["debuggers"][bench.debugger_name()]
    entry["type"] = "stlink"
    entry["executable"] = str(executable)
    entry["interface"] = "SWD"
    for key in ("interface_cfg", "target_cfg", "target_type", "gdb_server_executable"):
        entry.pop(key, None)
    variant = bench.config.parent / "bench-stlink-sessions.yaml"
    variant.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    try:
        yield Bench(project=bench.project, config=variant, config_root=bench.config_root, state_root=bench.state_root)
    finally:
        variant.unlink(missing_ok=True)


@pytest.fixture(scope="module", autouse=True)
def the_demo_runs_afterwards(bench: Bench, firmware: Path) -> Iterator[None]:
    """The demo back on the board and running once this module is done with it."""
    yield
    report = put_on_board(bench, firmware)
    if report.get("ok") is not True:
        pytest.fail(f"the demo firmware could not be put back on the board after the ST-LINK_gdbserver sessions: {report.get('summary')}", pytrace=False)


@pytest.fixture
def stlink_servers(stlink_bench: Bench) -> Iterator[Callable[[], McpServer]]:
    """MCP servers on the stlink configuration for one test, and the bench given back afterwards.

    The same containment as the OpenOCD and pyOCD sessions modules: every server
    is shut down, its open session stopped first, and a quarantine left behind
    is cleared through `agentic-hil recover` and fails the test for having been
    needed."""
    started: list[McpServer] = []

    def start() -> McpServer:
        server = McpServer(stlink_bench)
        started.append(server)
        return server

    try:
        yield start
    finally:
        left_behind: list[str] = []
        for server in reversed(started):
            try:
                server.shut_down()
            except Exception as error:
                left_behind.append(f"a server did not shut down: {type(error).__name__}: {error}\n{server.diagnosis()}")
        incident = blocking_incident(stlink_bench)
        if incident is not None:
            recovered = stlink_bench.run("recover", "--confirm-safe-state", "--quarantine-id", str(incident.get("quarantine_id") or ""))
            left_behind.append(
                "this test left the bench blocked; recovery was run here so the next test starts clean.\n"
                f"cleanup_reasons: {incident.get('cleanup_reasons')}\n"
                f"incident_stands: {incident.get('incident_stands')}\n"
                f"recover exited {recovered.returncode}: {recovered.stdout}{recovered.stderr}"
            )
        if left_behind:
            pytest.fail("\n".join(left_behind), pytrace=False)


def session_log(bench: Bench, log_path: str) -> dict:
    recorded = bench.project / log_path
    assert recorded.is_file(), f"the result named a session log that is not there: {log_path}"
    return json.loads(recorded.read_text(encoding="utf-8"))


def assert_ended_with_the_core_held(stopped: dict, bench: Bench) -> None:
    """A stop that says the core was confirmed halted and the server was gone before GDB left."""
    assert stopped["ok"] is True, said(stopped)
    assert stopped["backend"] == "stlink", said(stopped)
    assert stopped["safe_state_confirmed"] is True, said(stopped)
    assert stopped["halt_not_confirmed"] is False, said(stopped)
    assert stopped["detach_resume_guard_confirmed"] is True, said(stopped)
    assert stopped["summary"] == "Debug session stopped with the target confirmed halted.", said(stopped["summary"])
    guard = session_log(bench, stopped["log_path"])["detach_guard"]
    assert guard["kind"] == SERVER_KILLED_BEFORE_DETACH, said(guard)
    assert guard["server_exited"] is True, said(guard)


def test_a_reset_halt_session_runs_to_a_breakpoint_halts_and_stops(stlink_servers, stlink_bench: Bench, gdb: None, firmware: Path) -> None:
    """The whole session on ST-LINK_gdbserver: reset into halt, breakpoint, resume, stop reason, halt, stop.

    The server is the one found beside the configured CLI, started with that
    CLI's directory as `-cp`. The reset is confirmed by the line the server
    prints for `monitor reset`, not assumed from a command accepted. The resume
    stops on the breakpoint this caller set and names it. The stop reason read
    afterwards is that same stop. A halt asked of a core that is already
    stopped answers the stop it is in. And the session ends with the core
    confirmed halted and the server gone before GDB left."""
    server = stlink_servers()

    started = server.tool("debug_start_session", {"image_path": workspace_image(stlink_bench, firmware), "mode": "reset_halt"})

    assert started["ok"] is True, said(started)
    assert started["backend"] == "stlink", said(started)
    assert started["mode"] == "reset_halt", said(started)
    session = started["session"]
    assert session["status"] == "halted", said(session)
    assert session["load_phase"] == "pre_load_reset_confirmed", said(session)
    assert session["firmware_load_status"] == "not_started", said(session)
    assert isinstance(session["gdb_port"], int) and session["gdb_port"] > 0, said(session)

    placed = server.tool("debug_set_breakpoint", {"location": HANDLER})
    assert placed["ok"] is True, said(placed)
    tracked = placed["breakpoint"]

    stopped_at = server.tool("debug_continue", {"timeout_s": REACHABLE_STOP_TIMEOUT_S})
    assert stopped_at["ok"] is True, said(stopped_at)
    assert stopped_at["stop_reason"] == "breakpoint_hit", said(stopped_at)
    assert stopped_at["target_ok"] is True, said(stopped_at)
    stop = stopped_at["stop"]
    assert stop["breakpoint_expected"] is True, said(stop)
    assert stop["breakpoint_id"] == tracked["id"], said([stop, tracked])
    assert stop["frame"]["function"] == HANDLER, said(stop)

    reason = server.tool("debug_get_stop_reason")
    assert reason["ok"] is True, said(reason)
    assert reason["stop_reason"] == "breakpoint_hit", said(reason)
    assert reason["stop"]["breakpoint_id"] == tracked["id"], said(reason)

    halted = server.tool("debug_halt")
    assert halted["ok"] is True, said(halted)
    assert halted["stop_reason"] == "breakpoint_hit", said(halted)
    assert halted["summary"].startswith("Target was already stopped"), said(halted)
    assert halted.get("quarantined") is not True, said(halted)

    stopped = server.tool("debug_stop_session")
    assert_ended_with_the_core_held(stopped, stlink_bench)
    command = session_log(stlink_bench, stopped["log_path"])["server_command"]
    root = cubeclt_root()
    assert Path(command[0]).resolve() == (root / GDB_SERVER).resolve(), said(command)
    assert Path(command[command.index("-cp") + 1]).resolve() == (root / PROGRAMMER_CLI).resolve().parent, said(command)
    assert "-g" in command and "-d" in command, said(command)


def test_a_resume_nothing_stops_is_interrupted_and_the_core_does_not_run_once_the_session_ends(
    stlink_servers, stlink_bench: Bench, gdb: None, firmware: Path
) -> None:
    """The interrupt through ST-LINK_gdbserver, and the teardown guarantee, measured on the counter.

    A resume with no breakpoint runs out, and the product halts the core before
    it answers. The counter the demo's SysTick drives is then read through the
    session, the session is ended, and after a pause a new session attaches and
    reads it again. An attach does not reset, so the second reading is the
    first plus whatever the core ran in between. The server resumes a core
    whose GDB leaves and resumes it on its own shutdown; a teardown that let
    either happen would show here as the whole pause on the counter."""
    server = stlink_servers()
    image = workspace_image(stlink_bench, firmware)
    started = server.tool("debug_start_session", {"image_path": image, "mode": "reset_halt"})
    assert started["ok"] is True, said(started)

    timed_out = server.tool("debug_continue", {"timeout_s": UNREACHABLE_STOP_TIMEOUT_S})
    assert timed_out["ok"] is False, said(timed_out)
    assert timed_out["error_type"] == "timeout", said(timed_out)
    assert timed_out["halt_confirmed"] is True, said(timed_out)
    assert timed_out["target_state"] == "halted", said(timed_out)
    assert timed_out.get("quarantined") is not True, said(timed_out)

    before = server.tool("debug_symbol_value", {"symbol": COUNTER})
    assert before["ok"] is True, said(before)
    assert before["session"]["session_id"] == started["session"]["session_id"], said(before)
    # The core ran for the length of the resume, so the counter has moved off
    # the zero the reset left it at; a reading of zero would prove nothing below.
    assert before["value_unsigned"] > 0, said(before)

    assert_ended_with_the_core_held(server.tool("debug_stop_session"), stlink_bench)
    time.sleep(SETTLE_S)

    attached = server.tool("debug_start_session", {"image_path": image, "mode": "attach"})
    assert attached["ok"] is True, said(attached)
    assert attached["session"]["load_phase"] == "target_connected", said(attached["session"])
    after = server.tool("debug_symbol_value", {"symbol": COUNTER})
    assert after["ok"] is True, said(after)
    ran_ms = after["value_unsigned"] - before["value_unsigned"]
    assert 0 <= ran_ms < NEXT_CONNECT_RUN_BOUND_MS, (before["value_unsigned"], after["value_unsigned"], SETTLE_S)

    assert_ended_with_the_core_held(server.tool("debug_stop_session"), stlink_bench)


def test_a_load_session_writes_the_demo_through_st_link_gdbserver_and_says_the_load_committed(
    stlink_servers, stlink_bench: Bench, gdb: None, firmware: Path
) -> None:
    """The one mode that writes the board, through ST-LINK_gdbserver.

    The image is the demo the board already runs, so the board ends holding
    what it started with. `firmware_load_status` is the claim, with the reset
    after the download confirmed by the server's own line."""
    server = stlink_servers()

    started = server.tool("debug_start_session", {"image_path": workspace_image(stlink_bench, firmware), "mode": "load"})

    assert started["ok"] is True, said(started)
    session = started["session"]
    assert session["status"] == "halted", said(session)
    assert session["firmware_load_status"] == "committed", said(session)
    assert session["load_phase"] == "post_load_reset_confirmed", said(session)

    assert_ended_with_the_core_held(server.tool("debug_stop_session"), stlink_bench)


def test_a_server_that_ends_with_an_stlink_session_open_hands_the_board_to_the_next_one(
    stlink_servers, stlink_bench: Bench, gdb: None, firmware: Path
) -> None:
    """An agent host that ends its server mid session must not take the probe with it.

    ST-LINK_gdbserver holds the probe for as long as it runs, and a second one
    is refused the probe while it does (recorded as `Failed to connect to
    device`). So the claim that matters is the last one: after the abandoned
    server is gone the bench reads free and the next server opens a session on
    the same probe."""
    abandoned = stlink_servers()
    started = abandoned.tool("debug_start_session", {"image_path": workspace_image(stlink_bench, firmware), "mode": "attach"})
    assert started["ok"] is True, said(started)
    first_session_id = started["session"]["session_id"]

    assert abandoned.shut_down(stop_session=False) == 0, abandoned.diagnosis()

    handed_back = stlink_bench.document("lease-status")[1]
    assert handed_back["blocked"] is False, said(handed_back)
    assert handed_back["bench_held"] is False, said(handed_back)
    assert handed_back["owner_active"] is False, said(handed_back)

    successor = stlink_servers()
    reopened = successor.tool("debug_start_session", {"image_path": workspace_image(stlink_bench, firmware), "mode": "attach"})
    assert reopened["ok"] is True, said(reopened) + chr(10) + successor.diagnosis()
    assert reopened["session"]["status"] == "halted", said(reopened["session"])
    assert reopened["session"]["session_id"] != first_session_id, said(reopened["session"])

    assert_ended_with_the_core_held(successor.tool("debug_stop_session"), stlink_bench)

