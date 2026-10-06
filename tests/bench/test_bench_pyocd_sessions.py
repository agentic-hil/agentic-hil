"""Typed debug sessions on the pyOCD backend, driven over the MCP stdio server against the board.

The same probe this tier configures for OpenOCD is put on `type: pyocd` here, in
a copy of the tier's configuration that this module writes and removes, and the
session tools are driven through `agentic-hil mcp-stdio` exactly as an agent
drives them. Behind them the product runs `pyocd gdbserver` and a GDB of its own;
nothing in this file starts either, opens the probe or speaks to the board.

What only the board can say about a session on this backend:

* whether `monitor reset halt` really takes the core to the reset vector through
  pyOCD's server, and whether the line the product reads as the proof of it is
  printed by the server this bench runs.
* whether a breakpoint set through pyOCD's server is the one the core stops on,
  and whether a resume that nothing stops is interrupted and contained.
* whether ending a session leaves the core halted. pyOCD resumes the core when
  its GDB client leaves, and the product ends pyOCD's server under a connected
  GDB instead; that this keeps the core halted is a claim about the probe and
  the core, and the counter the demo's SysTick drives is what proves it: read
  before the session ends, read again after a pause through a new session, and
  moved by no more than the next connect itself moves it.
* whether a server that ends with a session open gives the probe back, so the
  next server can open one.
* whether a reset into halt without a session holds across the reads after it.
  Each of those calls is a pyOCD process of its own, and the counter read twice
  with a pause between is what says whether any of them let the core run.

The module ends with the demo put back on the board and running, through the
tier's own plan runner, because a session ends with the core held halted and the
modules after this one expect a board that runs the demo.

Nothing here names a bench. The probe, the executable and the session's values
are read out of the product's own answers and asserted on their shape.
"""

from __future__ import annotations

import json
import shutil
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml

from .conftest import BENCH_ONLY, Bench, put_on_board
from .test_bench_debug_sessions import McpServer, blocking_incident, workspace_image

pytestmark = [pytest.mark.bench, BENCH_ONLY]

# The part pyOCD is told it is talking to: the Nucleo-F446RE's, from the CMSIS
# pack the bench image installs and the recordings were made against.
TARGET_TYPE = "stm32f446retx"
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
# What the next session's own connect may move the counter by. pyOCD runs the
# pack's DebugCoreStart sequence at every connect, which clears the halt bit,
# and halts the core again a few tens of milliseconds later: 21 to 27 ms in
# tests/fixtures/pyocd_0_45_1_gdbserver_next_connect_recordings.json, 21 to 37
# ms on this bench since. A quarter of the pause, so a core left running
# through it cannot pass for one the connect moved.
NEXT_CONNECT_RUN_BOUND_MS = 500
# How the product records the end of a pyOCD session: the server is ended under
# a connected GDB, because pyOCD resumes the core when GDB leaves.
SERVER_ENDED_BEFORE_DETACH = "server_terminated_before_gdb_detach"
# How long the core is left alone between two reads after a reset into halt.
# A core that runs moves the demo's counter by about a thousand a second.
READS_APART_S = 1.0


@dataclass(frozen=True)
class PyocdBench(Bench):
    """The tier's project and roots, with a configuration that puts the probe on pyOCD.

    pyOCD finds the CMSIS pack that describes the part under the data home, so
    the data home is the operator's, beside the device locks; everything else
    stays this session's own."""

    def environment(self, **overrides: str) -> dict[str, str]:
        environment = super().environment(**overrides)
        environment["XDG_DATA_HOME"] = str(Path(environment["HOME"]) / ".local" / "share")
        return environment


@pytest.fixture(scope="module")
def pyocd_bench(bench: Bench) -> Iterator[PyocdBench]:
    """A copy of the tier's configuration with the bound debugger on `type: pyocd`.

    The probe, its permissions and the debug section are the tier's own; the
    backend, its executable and the part are what changes. The copy sits beside
    the configuration `init` wrote, under this session's own root, and is
    removed afterwards. The tier's own configuration is never written."""
    executable = shutil.which("pyocd")
    assert executable, "pyocd is not on PATH, and the bench image installs it for this module"
    document = bench.configuration()
    entry = document["debuggers"][bench.debugger_name()]
    entry["type"] = "pyocd"
    entry["executable"] = executable
    entry["target_type"] = TARGET_TYPE
    entry.pop("interface_cfg", None)
    entry.pop("target_cfg", None)
    variant = bench.config.parent / "bench-pyocd-sessions.yaml"
    variant.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    try:
        yield PyocdBench(project=bench.project, config=variant, config_root=bench.config_root, state_root=bench.state_root)
    finally:
        variant.unlink(missing_ok=True)


@pytest.fixture(scope="module", autouse=True)
def the_demo_runs_afterwards(bench: Bench, firmware: Path) -> Iterator[None]:
    """The demo back on the board and running once this module is done with it."""
    yield
    report = put_on_board(bench, firmware)
    if report.get("ok") is not True:
        pytest.fail(f"the demo firmware could not be put back on the board after the pyOCD sessions: {report.get('summary')}", pytrace=False)


@pytest.fixture
def pyocd_servers(pyocd_bench: PyocdBench) -> Iterator[Callable[[], McpServer]]:
    """MCP servers on the pyOCD configuration for one test, and the bench given back afterwards.

    The same containment as the OpenOCD sessions module: every server is shut
    down, its open session stopped first, and a quarantine left behind is
    cleared through `agentic-hil recover` and fails the test for having been
    needed."""
    started: list[McpServer] = []

    def start() -> McpServer:
        server = McpServer(pyocd_bench)
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
        incident = blocking_incident(pyocd_bench)
        if incident is not None:
            recovered = pyocd_bench.run("recover", "--confirm-safe-state", "--quarantine-id", str(incident.get("quarantine_id") or ""))
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
    """A stop that says the core was confirmed halted and pyOCD's server was gone before GDB left."""
    assert stopped["ok"] is True, stopped
    assert stopped["backend"] == "pyocd", stopped
    assert stopped["safe_state_confirmed"] is True, stopped
    assert stopped["halt_not_confirmed"] is False, stopped
    assert stopped["detach_resume_guard_confirmed"] is True, stopped
    assert stopped["summary"] == "Debug session stopped with the target confirmed halted.", stopped["summary"]
    guard = session_log(bench, stopped["log_path"])["detach_guard"]
    assert guard["kind"] == SERVER_ENDED_BEFORE_DETACH, guard
    assert guard["server_exited"] is True, guard


def test_a_reset_halt_session_runs_to_a_breakpoint_halts_and_stops(pyocd_servers, pyocd_bench: PyocdBench, gdb: None, firmware: Path) -> None:
    """The whole session on pyOCD: reset into halt, breakpoint, resume, stop reason, halt, stop.

    Each step is the claim a fake answers by construction. The reset is
    confirmed by the line pyOCD prints for it, not assumed from a command
    accepted. The resume stops on the breakpoint this caller set and names it.
    The stop reason read afterwards is that same stop. A halt asked of a core
    that is already stopped answers the stop it is in. And the session ends
    with the core confirmed halted and pyOCD's server gone before GDB left."""
    server = pyocd_servers()

    started = server.tool("debug_start_session", {"image_path": workspace_image(pyocd_bench, firmware), "mode": "reset_halt"})

    assert started["ok"] is True, started
    assert started["backend"] == "pyocd", started
    assert started["mode"] == "reset_halt", started
    session = started["session"]
    assert session["status"] == "halted", session
    assert session["load_phase"] == "pre_load_reset_confirmed", session
    assert session["firmware_load_status"] == "not_started", session
    assert isinstance(session["gdb_port"], int) and session["gdb_port"] > 0, session

    placed = server.tool("debug_set_breakpoint", {"location": HANDLER})
    assert placed["ok"] is True, placed
    tracked = placed["breakpoint"]

    stopped_at = server.tool("debug_continue", {"timeout_s": REACHABLE_STOP_TIMEOUT_S})
    assert stopped_at["ok"] is True, stopped_at
    assert stopped_at["stop_reason"] == "breakpoint_hit", stopped_at
    assert stopped_at["target_ok"] is True, stopped_at
    stop = stopped_at["stop"]
    assert stop["breakpoint_expected"] is True, stop
    assert stop["breakpoint_id"] == tracked["id"], (stop, tracked)
    assert stop["frame"]["function"] == HANDLER, stop

    reason = server.tool("debug_get_stop_reason")
    assert reason["ok"] is True, reason
    assert reason["stop_reason"] == "breakpoint_hit", reason
    assert reason["stop"]["breakpoint_id"] == tracked["id"], reason

    halted = server.tool("debug_halt")
    assert halted["ok"] is True, halted
    assert halted["stop_reason"] == "breakpoint_hit", halted
    assert halted["summary"].startswith("Target was already stopped"), halted
    assert halted.get("quarantined") is not True, halted

    stopped = server.tool("debug_stop_session")
    assert_ended_with_the_core_held(stopped, pyocd_bench)


def test_a_resume_nothing_stops_is_interrupted_and_the_core_does_not_run_once_the_session_ends(
    pyocd_servers, pyocd_bench: PyocdBench, gdb: None, firmware: Path
) -> None:
    """The interrupt through pyOCD's server, and the teardown guarantee, measured on the counter.

    A resume with no breakpoint runs out, and the product halts the core before
    it answers. The counter the demo's SysTick drives is then read through the
    session, the session is ended, and after a pause a new session attaches and
    reads it again. An attach does not reset, so the second reading is the
    first plus whatever the core ran in between. pyOCD resumes a core whose GDB
    leaves; a teardown that let it would show here as the whole pause on the
    counter. What the new session's connect itself runs, pyOCD's and recorded,
    is all the reading may add."""
    server = pyocd_servers()
    image = workspace_image(pyocd_bench, firmware)
    started = server.tool("debug_start_session", {"image_path": image, "mode": "reset_halt"})
    assert started["ok"] is True, started

    timed_out = server.tool("debug_continue", {"timeout_s": UNREACHABLE_STOP_TIMEOUT_S})
    assert timed_out["ok"] is False, timed_out
    assert timed_out["error_type"] == "timeout", timed_out
    assert timed_out["halt_confirmed"] is True, timed_out
    assert timed_out["target_state"] == "halted", timed_out
    assert timed_out.get("quarantined") is not True, timed_out

    before = server.tool("debug_symbol_value", {"symbol": COUNTER})
    assert before["ok"] is True, before
    assert before["session"]["session_id"] == started["session"]["session_id"], before
    # The core ran for the length of the resume, so the counter has moved off
    # the zero the reset left it at; a reading of zero would prove nothing below.
    assert before["value_unsigned"] > 0, before

    assert_ended_with_the_core_held(server.tool("debug_stop_session"), pyocd_bench)
    time.sleep(SETTLE_S)

    attached = server.tool("debug_start_session", {"image_path": image, "mode": "attach"})
    assert attached["ok"] is True, attached
    assert attached["session"]["load_phase"] == "target_connected", attached["session"]
    after = server.tool("debug_symbol_value", {"symbol": COUNTER})
    assert after["ok"] is True, after
    ran_ms = after["value_unsigned"] - before["value_unsigned"]
    assert 0 <= ran_ms < NEXT_CONNECT_RUN_BOUND_MS, (before["value_unsigned"], after["value_unsigned"], SETTLE_S)

    assert_ended_with_the_core_held(server.tool("debug_stop_session"), pyocd_bench)


def test_a_load_session_writes_the_demo_through_pyocds_server_and_says_the_load_committed(
    pyocd_servers, pyocd_bench: PyocdBench, gdb: None, firmware: Path
) -> None:
    """The one mode that writes the board, through `pyocd gdbserver`.

    The image is the demo the board already runs, so the board ends holding
    what it started with. `firmware_load_status` is the claim, with the reset
    after the download confirmed by the server's own line."""
    server = pyocd_servers()

    started = server.tool("debug_start_session", {"image_path": workspace_image(pyocd_bench, firmware), "mode": "load"})

    assert started["ok"] is True, started
    session = started["session"]
    assert session["status"] == "halted", session
    assert session["firmware_load_status"] == "committed", session
    assert session["load_phase"] == "post_load_reset_confirmed", session

    assert_ended_with_the_core_held(server.tool("debug_stop_session"), pyocd_bench)


def test_a_server_that_ends_with_a_pyocd_session_open_hands_the_board_to_the_next_one(
    pyocd_servers, pyocd_bench: PyocdBench, gdb: None, firmware: Path
) -> None:
    """An agent host that ends its server mid session must not take the probe with it.

    pyOCD holds the probe for as long as its server runs, and a second pyOCD
    is refused the probe while it does. So the claim that matters is the last
    one: after the abandoned server is gone the bench reads free and the next
    server opens a session on the same probe."""
    abandoned = pyocd_servers()
    started = abandoned.tool("debug_start_session", {"image_path": workspace_image(pyocd_bench, firmware), "mode": "attach"})
    assert started["ok"] is True, started
    first_session_id = started["session"]["session_id"]

    assert abandoned.shut_down(stop_session=False) == 0, abandoned.diagnosis()

    handed_back = pyocd_bench.document("lease-status")[1]
    assert handed_back["blocked"] is False, handed_back
    assert handed_back["bench_held"] is False, handed_back
    assert handed_back["owner_active"] is False, handed_back

    successor = pyocd_servers()
    reopened = successor.tool("debug_start_session", {"image_path": workspace_image(pyocd_bench, firmware), "mode": "attach"})
    assert reopened["ok"] is True, reopened
    assert reopened["session"]["status"] == "halted", reopened["session"]
    assert reopened["session"]["session_id"] != first_session_id, reopened["session"]

    assert_ended_with_the_core_held(successor.tool("debug_stop_session"), pyocd_bench)


def test_a_reset_into_halt_without_a_session_holds_across_the_reads_after_it(
    pyocd_servers, pyocd_bench: PyocdBench, gdb: None, firmware: Path
) -> None:
    """`reset_target` with mode `halt`, then two reads of the demo's counter, with no session open (#631).

    Without a session the reset and each read are pyOCD processes of their own,
    each connecting to the probe and leaving it. The demo's SysTick moves the
    counter every millisecond once the core runs, so two reads a pause apart
    that return the same value are a core that stayed where the reset halted
    it. A core that any of those connects or disconnects let run reads about a
    thousand further on the second time. The image is flashed first because a
    read without a session resolves its symbol against the image this server
    flashed, and it is the demo the board already runs."""
    server = pyocd_servers()
    flashed = server.tool("flash_firmware", {"image_path": workspace_image(pyocd_bench, firmware), "reset_after_flash": True})
    assert flashed["ok"] is True, flashed

    halted = server.tool("reset_target", {"mode": "halt"})
    assert halted["ok"] is True, halted
    assert halted["backend"] == "pyocd", halted

    first = server.tool("debug_symbol_value", {"symbol": COUNTER})
    assert first["ok"] is True, first
    time.sleep(READS_APART_S)
    second = server.tool("debug_symbol_value", {"symbol": COUNTER})
    assert second["ok"] is True, second

    assert second["value_unsigned"] == first["value_unsigned"], (first["value_unsigned"], second["value_unsigned"], READS_APART_S)


def test_a_probe_of_a_core_reset_into_halt_without_a_session_leaves_it_halted(
    pyocd_servers, pyocd_bench: PyocdBench, gdb: None, firmware: Path
) -> None:
    """`reset_target` with mode `halt`, `probe_target`, then two reads of the demo's counter, with no session open.

    The probe is its own pyOCD process too, and the one call here that only
    looks. Two reads a pause apart after it that return the same value are a
    core the probe left where the reset halted it."""
    server = pyocd_servers()
    flashed = server.tool("flash_firmware", {"image_path": workspace_image(pyocd_bench, firmware), "reset_after_flash": True})
    assert flashed["ok"] is True, flashed

    halted = server.tool("reset_target", {"mode": "halt"})
    assert halted["ok"] is True, halted
    probed = server.tool("probe_target")
    assert probed["ok"] is True, probed
    assert probed["backend"] == "pyocd", probed

    first = server.tool("debug_symbol_value", {"symbol": COUNTER})
    assert first["ok"] is True, first
    time.sleep(READS_APART_S)
    second = server.tool("debug_symbol_value", {"symbol": COUNTER})
    assert second["ok"] is True, second

    assert second["value_unsigned"] == first["value_unsigned"], (first["value_unsigned"], second["value_unsigned"], READS_APART_S)
