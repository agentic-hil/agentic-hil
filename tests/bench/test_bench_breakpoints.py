"""Breakpoints and execution control, on the board, through the MCP server.

Everything here is driven over `agentic-hil mcp-stdio`, because that is where
this half of the product lives: the command line exposes no `debug-continue`
and no `debug-set-breakpoint`, and the plan vocabulary offers only the composite
`run_until_breakpoint`. An agent that sets a breakpoint, resumes the core and
reads back where it stopped does it through the tools below and through nothing
else, so that is the surface these tests hold.

Why a real board is needed for all of it: a breakpoint is only real once a core
executes into it. Whether `-break-insert` resolved a name to an address in the
image this session loaded, whether the stop that came back is the breakpoint the
caller set rather than an unexpected trap, whether a resume that nothing will
stop leaves the target contained or free-running, and what the session says
about itself afterwards, are all answers a fake gives by construction and the
board gives by running.

Each test gets its own server process, and each test that needs one its own debug
session, opened in `reset_halt` mode so the core starts from the reset vector
every time. That is what makes the file order-independent and repeatable: no test
inherits another's program counter, its breakpoint numbering or its stop reason.
Nothing here flashes, erases or writes to the target: `reset_halt` resets and
stops, the firmware on the board stays the starter's, and the ELF is opened for
its symbols.

Teardown is unconditional and runs on a failed test as well as a passing one: the
session is stopped, the server's stdin is closed so its own `close()` releases
the lease, and any quarantine left behind is cleared through
`agentic-hil recover`, which is the operator's route and the one this tier is
allowed to use on its own configuration. Three tests here leave a quarantine, and
each says so in its docstring.

At most one quarantining call per session, and that is a constraint on how these
tests may be written rather than a preference. A debug call whose result the
product cannot vouch for quarantines the session's lease; the next audited debug
call then meets a blocked bench and runs the automatic recovery, which reaps this
owner's debugger processes and discards the session. A test that made two such
calls in one session would have its second answered by a bench the first one had
already taken apart, and would go red saying nothing about the contract it was
written for.

Nothing here asserts a value that identifies this bench. Session ids, breakpoint
backend ids and probe identities are read out of the product's own answers and
asserted for shape.
"""

from __future__ import annotations

import contextlib
import json
import queue
import subprocess
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from .conftest import BENCH_ONLY, Bench, child_command

pytestmark = [pytest.mark.bench, BENCH_ONLY]

# The demo's own source, as the project copy carries it, and the statement the
# file-and-line breakpoint is put on. Read out of the file at run time rather
# than written here as a number: a line number pinned in a test is a test that
# moves a breakpoint onto a blank line the first time somebody edits the demo.
DEMO_SOURCE = Path("Src") / "main.c"
# What GDB is asked for. The basename is what the compiler recorded and what
# matches whichever prefix the build used to reach the file.
DEMO_SOURCE_NAME = "main.c"
# The one statement in the demo that runs on every SysTick, which is what makes
# a breakpoint on it certain to be reached within a millisecond of the firmware
# enabling the timer.
COUNTER_STATEMENT = "uptime_ms++"
# Two functions this firmware defines, and one it does not. The first is entered
# exactly once per reset, which is what a resume from the reset vector is aimed
# at; the second is the interrupt handler the line above lives in.
ENTRY_FUNCTION = "main"
HANDLER_FUNCTION = "SysTick_Handler"
UNDEFINED_FUNCTION = "a_function_this_firmware_does_not_define"

# How long the server may take to answer, per method. Bounded because a server
# that starts and never answers would otherwise block a read forever: no timeout
# plugin is configured anywhere in this repository, so an unbounded read runs the
# job to its ceiling and reports a timeout instead of a test saying what went
# quiet.
INITIALIZE_TIMEOUT_S = 60.0
CALL_TIMEOUT_S = 90.0
# A session start spawns a debug server, connects GDB to it and resets the
# target, each of them bounded by the configured debugger timeout rather than by
# anything here, so this has to sit above that budget rather than inside it.
START_SESSION_TIMEOUT_S = 240.0
SERVER_EXIT_TIMEOUT_S = 30.0

# What a resume is given. The long one is for a breakpoint that will be reached
# in microseconds and only needs room for the round trip; the short one is for
# the resume that is meant to run out, and it is short because the target is
# free-running for exactly that long.
REACHABLE_STOP_TIMEOUT_S = 15.0
UNREACHABLE_STOP_TIMEOUT_S = 3.0

# The two grants these tests need, as the dotted keys the configuration uses.
# `agentic-hil init` writes both open, so a bench where either is closed is one
# whose configuration is not the one this tier generated.
ALL_SYMBOLS_KEY = "debug.allow_all_symbols"
EXECUTION_PERMISSION = "allow_debug_execution"


def demo_source_line(project: Path, statement: str) -> int:
    """The 1-based line the named statement is on, in the project's own copy."""
    source = (project / DEMO_SOURCE).read_text(encoding="utf-8").splitlines()
    found = [number for number, line in enumerate(source, start=1) if statement in line]
    assert len(found) == 1, f"{statement} is on {len(found)} lines of {DEMO_SOURCE.as_posix()}, and this breakpoint needs exactly one"
    return found[0]


def line_past_the_end(project: Path) -> int:
    """A line number that file has not got, for the breakpoint that must be refused."""
    return len((project / DEMO_SOURCE).read_text(encoding="utf-8").splitlines()) + 1000


class McpServer:
    """One `agentic-hil mcp-stdio` process, driven the way an agent host drives it.

    Requests go out one at a time and answers are read back off a pump thread, so
    a server that dies mid-call ends the read at once instead of on a deadline,
    and whatever it wrote to stderr on the way out is quoted into the failure
    rather than lost with the pipe.
    """

    def __init__(self, process: subprocess.Popen[str]) -> None:
        assert process.stdin is not None and process.stdout is not None and process.stderr is not None
        self._process = process
        self._answers: queue.Queue[str | None] = queue.Queue()
        self._diagnostics: list[str] = []
        self._request_id = 0
        self._pumps = [
            threading.Thread(target=self._pump_answers, args=(process.stdout,), daemon=True),
            threading.Thread(target=self._pump_diagnostics, args=(process.stderr,), daemon=True),
        ]
        for pump in self._pumps:
            pump.start()

    def _pump_answers(self, stream) -> None:
        for line in stream:
            self._answers.put(line)
        self._answers.put(None)

    def _pump_diagnostics(self, stream) -> None:
        for line in stream:
            self._diagnostics.append(line)

    def diagnostics(self) -> str:
        """The tail of what the server said for itself, for a failure to quote."""
        return "".join(self._diagnostics[-20:]).strip() or "(the server wrote nothing to stderr)"

    def _write(self, message: dict) -> None:
        stdin = self._process.stdin
        assert stdin is not None
        stdin.write(json.dumps(message) + "\n")
        stdin.flush()

    def _answer(self, method: str, timeout_s: float) -> dict:
        try:
            line = self._answers.get(timeout=timeout_s)
        except queue.Empty:
            raise AssertionError(f"the server did not answer {method} within {timeout_s:.0f}s. It said: {self.diagnostics()}") from None
        if line is None:
            raise AssertionError(f"the server closed its output before answering {method}. It said: {self.diagnostics()}")
        return json.loads(line)

    def request(self, method: str, params: dict, timeout_s: float) -> dict:
        self._request_id += 1
        request_id = self._request_id
        self._write({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        answered = self._answer(method, timeout_s)
        assert answered.get("id") == request_id, f"{method} was answered under another id: {answered}"
        assert "error" not in answered, f"{method} came back as a JSON-RPC error: {answered['error']}"
        return answered["result"]

    def initialize(self) -> dict:
        result = self.request(
            "initialize",
            {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "bench-tier", "version": "1"}},
            INITIALIZE_TIMEOUT_S,
        )
        assert result["protocolVersion"], result
        self._write({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
        return result

    def call(self, name: str, arguments: dict | None = None, timeout_s: float = CALL_TIMEOUT_S) -> tuple[bool, dict]:
        """One tool call: whether the host is told this is an error, and the document.

        Both halves, because they are two separate claims a host acts on. The
        document is what an agent reads; `isError` is what a host branches on
        before it reads anything, and a refusal that arrives with `isError`
        false is a refusal a host treats as a success.
        """
        result = self.request("tools/call", {"name": name, "arguments": arguments or {}}, timeout_s)
        document = result.get("structuredContent")
        assert isinstance(document, dict), f"{name} answered no structuredContent: {result}"
        content = result.get("content")
        assert isinstance(content, list) and content and content[0].get("type") == "text", f"{name} answered no content block: {result}"
        assert json.loads(content[0]["text"]) == document, f"{name} answered two different documents in one result: {result}"
        return bool(result.get("isError")), document

    def try_call(self, name: str, arguments: dict | None = None) -> dict | None:
        """A teardown call: best effort, and never the thing that fails a test.

        Teardown runs after a test that has already said what it found, including
        the one below that is expected to fail today. An exception raised here
        would be reported over that verdict, so what this cannot do it reports by
        answering None and letting the recovery step downstream deal with the
        bench.
        """
        if self._process.poll() is not None:
            return None
        try:
            _, document = self.call(name, arguments)
        except (AssertionError, OSError, ValueError):
            return None
        return document

    def close(self) -> None:
        process = self._process
        if process.poll() is None:
            if process.stdin is not None:
                with contextlib.suppress(OSError):
                    process.stdin.close()
            try:
                process.wait(SERVER_EXIT_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(SERVER_EXIT_TIMEOUT_S)
        for pump in self._pumps:
            pump.join(SERVER_EXIT_TIMEOUT_S)
        for stream in (process.stdout, process.stderr):
            if stream is not None and not stream.closed:
                stream.close()


def clear_any_quarantine(bench: Bench) -> dict:
    """Put the bench back the way the next test needs it, through the operator's route.

    `agentic-hil recover` is the whole of it. A bench with nothing standing
    answers `nothing_to_recover` and changes nothing, which is the ordinary case
    and is why this runs after every test rather than only after the one that
    quarantines. What is asserted afterwards is that no incident *stands*: a
    standing incident is the one state that refuses the next test's session, and
    a run that left one behind has to say so where it happened rather than three
    tests later.
    """
    _, before = bench.document("lease-status")
    quarantine_id = before.get("quarantine_id")
    if before.get("blocked") and isinstance(quarantine_id, str) and quarantine_id:
        _, recovered = bench.document("recover", "--confirm-safe-state", "--quarantine-id", quarantine_id)
        assert recovered["ok"] is True, recovered
    _, after = bench.document("lease-status")
    assert after["incident_stands"] is False, after
    return after


@pytest.fixture()
def server(bench: Bench) -> Iterator[McpServer]:
    """One MCP server per test, ended and recovered whatever the test did."""
    process = subprocess.Popen(
        child_command("mcp-stdio"),
        cwd=str(bench.project),
        env=bench.environment(),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    running = McpServer(process)
    try:
        running.initialize()
        yield running
    finally:
        running.close()
        clear_any_quarantine(bench)


@pytest.fixture()
def session(server: McpServer, bench: Bench, firmware: Path) -> Iterator[McpServer]:
    """A debug session on the demo ELF, reset and halted, closed in teardown.

    `reset_halt` rather than `attach`, so every test starts at the reset vector
    and a resume runs the firmware from its entry rather than from wherever the
    previous test left the core. Nothing is downloaded: the board keeps the
    firmware it has, and the ELF is opened for the symbols the breakpoints below
    are named out of.
    """
    configuration = bench.configuration()
    if configuration.get("debug", {}).get("allow_all_symbols") is not True:
        pytest.fail(f"this session's configuration does not grant {ALL_SYMBOLS_KEY}, and every breakpoint here needs it", pytrace=False)
    permissions = configuration["debuggers"][bench.debugger_name()]["permissions"]
    if permissions.get(EXECUTION_PERMISSION) is not True:
        pytest.fail(f"this session's configuration does not grant {EXECUTION_PERMISSION}, and every resume here needs it", pytrace=False)

    image = firmware.relative_to(bench.project).as_posix()
    errored, started = server.call("debug_start_session", {"image_path": image, "mode": "reset_halt"}, timeout_s=START_SESSION_TIMEOUT_S)
    if errored or started.get("ok") is not True:
        pytest.fail(f"this bench could not open a debug session on the demo firmware: {started.get('summary')}", pytrace=False)
    try:
        yield server
    finally:
        server.try_call("debug_stop_session")


def breakpoint_ids(document: dict) -> list[int]:
    return [item["id"] for item in document["breakpoints"]]


def assert_tracked_breakpoint(document: dict, location: dict, gdb_location: str) -> dict:
    """The shape every accepted breakpoint comes back in, whichever way it was named."""
    tracked = document["breakpoint"]
    assert isinstance(tracked["id"], int) and tracked["id"] > 0, tracked
    assert isinstance(tracked["backend_id"], str) and tracked["backend_id"].isdigit(), tracked
    assert tracked["location"] == location, tracked
    assert tracked["gdb_location"] == gdb_location, tracked
    # Only a set whose acknowledgement was lost is tracked provisionally, and a
    # provisional entry is one a later clear may find is not on the target. A
    # confirmed set that carried this flag would tell a caller its breakpoint may
    # not exist while it does.
    assert "provisional" not in tracked, tracked
    return tracked


def test_the_debug_tools_answer_session_not_active_before_a_session_is_started(server: McpServer) -> None:
    """The four session tools with nothing open, and the one that answers anyway.

    What this catches is a tool that treats "no session" as a hardware incident:
    the refusal has to be `session_not_active` and it has to leave the bench
    alone, because an agent that called a debug tool a step too early would
    otherwise have quarantined a board nothing had touched. `debug_list_breakpoints`
    is the one that still answers, because a listing of nothing is a fact and not
    a refusal, and an agent asking what is set before it opens a session gets an
    empty list rather than an error to diagnose.
    """
    for tool in ("debug_set_breakpoint", "debug_clear_breakpoints", "debug_continue", "debug_halt", "debug_get_stop_reason"):
        arguments = {"location": ENTRY_FUNCTION} if tool == "debug_set_breakpoint" else {}
        errored, refused = server.call(tool, arguments)
        assert errored is True, (tool, refused)
        assert refused["ok"] is False, (tool, refused)
        assert refused["error_type"] == "session_not_active", (tool, refused)
        assert refused.get("quarantined") is not True, (tool, refused)
        assert refused.get("cleanup_required") is not True, (tool, refused)

    errored, listed = server.call("debug_list_breakpoints")
    assert errored is False, listed
    assert listed["ok"] is True, listed
    assert listed["active"] is False, listed
    assert listed["breakpoints"] == [], listed


def test_a_breakpoint_named_by_function_is_accepted_and_echoed_back_as_a_symbol(session: McpServer) -> None:
    """The plain case, and the id the caller is meant to match a stop against.

    The backend id is GDB's number and the tracked id is the product's own, and
    they are different numbering schemes on purpose. A result that returned only
    the backend's, or returned an id for a set the backend never acknowledged,
    leaves a caller with nothing it can compare a later stop reason to.
    """
    errored, result = session.call("debug_set_breakpoint", {"location": ENTRY_FUNCTION})

    assert errored is False, result
    assert result["ok"] is True, result
    assert result["summary"] == "Breakpoint set.", result
    tracked = assert_tracked_breakpoint(result, {"symbol": ENTRY_FUNCTION}, ENTRY_FUNCTION)
    # The session carries it too, which is what `debug_get_session_status` shows
    # an agent that has lost track of what it set.
    assert result["session"]["breakpoints"] == [tracked], result["session"]


def test_a_breakpoint_named_by_file_and_line_is_accepted_and_echoed_back_as_a_file_and_line(session: McpServer, bench: Bench) -> None:
    """The other spelling, which is the one an agent reaches for over a source diff.

    It is normalized separately from the symbol form and gated on a separate
    permission, so it is asserted separately: a file and line that came back
    rewritten as a symbol, or joined into a location string the caller cannot
    compare with what it asked for, is a breakpoint the caller cannot tell apart
    from another one on the same function.
    """
    line = demo_source_line(bench.project, COUNTER_STATEMENT)

    errored, result = session.call("debug_set_breakpoint", {"location": {"file": DEMO_SOURCE_NAME, "line": line}})

    assert errored is False, result
    assert result["ok"] is True, result
    assert_tracked_breakpoint(result, {"file": DEMO_SOURCE_NAME, "line": line}, f"{DEMO_SOURCE_NAME}:{line}")


def test_the_same_location_set_twice_is_two_breakpoints_and_both_are_cleared(session: McpServer) -> None:
    """Setting one location twice, which is what a retry after a lost answer does.

    Two things have to hold and they are easy to get wrong in opposite
    directions. The second set is its own tracked breakpoint with its own id, so
    a caller that set two knows it holds two; and the clear afterwards accounts
    for both against the backend's own list, because a second breakpoint the
    product forgot is a breakpoint left on the target for the next session to
    stop on.
    """
    _, first = session.call("debug_set_breakpoint", {"location": ENTRY_FUNCTION})
    _, second = session.call("debug_set_breakpoint", {"location": ENTRY_FUNCTION})

    assert first["ok"] is True and second["ok"] is True, (first, second)
    assert first["breakpoint"]["id"] != second["breakpoint"]["id"], (first, second)
    assert first["breakpoint"]["backend_id"] != second["breakpoint"]["backend_id"], (first, second)
    assert first["breakpoint"]["location"] == second["breakpoint"]["location"], (first, second)

    _, listed = session.call("debug_list_breakpoints")
    assert breakpoint_ids(listed) == [first["breakpoint"]["id"], second["breakpoint"]["id"]], listed

    errored, cleared = session.call("debug_clear_breakpoints")
    assert errored is False, cleared
    assert cleared["ok"] is True, cleared
    assert cleared["cleared"] == 2, cleared
    assert cleared["backend_reconciled"] is True, cleared


def assert_the_debugger_refused_and_tracked_nothing(session: McpServer, location: object) -> None:
    """One location the debugger itself rejects, and the listing on either side.

    Exactly one of these per session, which is why the two spellings below are
    two tests rather than one loop. A `debugger_error` out of a set carries
    `side_effect_status: unknown`, which quarantines the session's lease, and the
    next audited debug call on a blocked bench runs the automatic recovery, which
    reaps this owner's debugger processes and discards the session. A second
    refusal in the same session would be answered `session_not_active` by a bench
    the first one had already taken apart. The two listings are read either side
    of the refusal, and neither is an audited call, so neither disturbs this.
    """
    _, before = session.call("debug_list_breakpoints")
    assert before["breakpoints"] == [], before

    errored, refused = session.call("debug_set_breakpoint", {"location": location})

    assert errored is True, (location, refused)
    assert refused["ok"] is False, (location, refused)
    assert refused["error_type"] == "debugger_error", (location, refused)
    assert refused["backend_error_type"] == "gdb_error", (location, refused)
    assert "breakpoint" not in refused, (location, refused)

    _, after = session.call("debug_list_breakpoints")
    assert after["breakpoints"] == [], after


def test_a_function_name_the_firmware_does_not_define_is_refused_and_is_tracked_nowhere(session: McpServer) -> None:
    """A symbol that resolves to no code, refused by the debugger itself.

    The refusal is a `debugger_error`, because the debugger is what decided it,
    and the decisive half is the listing afterwards: a breakpoint that was never
    inserted must not appear among the ones the session holds. Tracking it would
    tell a caller it has a breakpoint that can never be hit, and would make the
    next clear report removing something the target never had.

    The set that fails leaves this session's lease quarantined, which this file's
    own teardown clears through `agentic-hil recover` before the next test.
    """
    assert_the_debugger_refused_and_tracked_nothing(session, UNDEFINED_FUNCTION)


def test_a_line_the_source_file_does_not_have_is_refused_and_is_tracked_nowhere(session: McpServer, bench: Bench) -> None:
    """The same refusal for the file and line spelling, resolved by another route.

    Asserted separately from the symbol above because the two are normalized
    separately and handed to the debugger as different linespecs, so a file and
    line that GDB could not place could be tracked as a breakpoint while the
    symbol form was refused, and the listing would then offer a caller a
    breakpoint the target has not got.

    The set that fails leaves this session's lease quarantined, which this file's
    own teardown clears through `agentic-hil recover` before the next test.
    """
    location = {"file": DEMO_SOURCE_NAME, "line": line_past_the_end(bench.project)}
    assert_the_debugger_refused_and_tracked_nothing(session, location)


def test_a_location_that_is_not_a_location_is_an_invalid_argument_and_touches_nothing(session: McpServer) -> None:
    """Four malformed locations, refused before the debugger is asked anything.

    `invalid_argument` and not `debugger_error`: the difference is who is being
    told they got it wrong, and an agent handed a debugger error for its own
    malformed argument retries the same call against the board instead of fixing
    it. None of them may leave a mark either, which is what the listing at the
    end is for.
    """
    for location in (
        {"file": DEMO_SOURCE_NAME},
        {"line": 1},
        "not a symbol!",
        {},
    ):
        errored, refused = session.call("debug_set_breakpoint", {"location": location})
        assert errored is True, (location, refused)
        assert refused["ok"] is False, (location, refused)
        assert refused["error_type"] == "invalid_argument", (location, refused)
        assert refused.get("quarantined") is not True, (location, refused)
        assert refused.get("cleanup_required") is not True, (location, refused)

    _, listed = session.call("debug_list_breakpoints")
    assert listed["breakpoints"] == [], listed


def test_the_listing_starts_empty_and_then_carries_every_breakpoint_in_the_order_it_was_set(session: McpServer, bench: Bench) -> None:
    """None, one, then several, read back off the listing an agent navigates by.

    The order and the ids are the claim. A listing that deduplicated by location,
    reordered, or renumbered between calls would leave an agent unable to say
    which of several breakpoints a later stop belongs to, and the id in a stop
    reason is the only handle it has.
    """
    _, empty = session.call("debug_list_breakpoints")
    assert empty["ok"] is True, empty
    assert empty["active"] is True, empty
    assert empty["breakpoints"] == [], empty

    _, first = session.call("debug_set_breakpoint", {"location": ENTRY_FUNCTION})
    _, one = session.call("debug_list_breakpoints")
    assert breakpoint_ids(one) == [first["breakpoint"]["id"]], one
    assert one["breakpoints"] == [first["breakpoint"]], one

    line = demo_source_line(bench.project, COUNTER_STATEMENT)
    _, second = session.call("debug_set_breakpoint", {"location": HANDLER_FUNCTION})
    _, third = session.call("debug_set_breakpoint", {"location": {"file": DEMO_SOURCE_NAME, "line": line}})
    _, several = session.call("debug_list_breakpoints")

    expected = [first["breakpoint"], second["breakpoint"], third["breakpoint"]]
    assert several["breakpoints"] == expected, several
    assert len({item["backend_id"] for item in several["breakpoints"]}) == 3, several


def test_clearing_reconciles_with_the_backend_and_leaves_the_listing_empty_and_clearable_again(session: McpServer) -> None:
    """The clear, and the clear after it, which is the one that used to wedge.

    Cleanup is driven off the backend's own list, so a second clear over an
    already empty session is a no-op that still reports itself reconciled rather
    than an error about a breakpoint number that is gone. An agent that closes a
    session tidily runs exactly this sequence, and a refusal on the second call
    reads to it as a bench it has damaged.
    """
    _, first = session.call("debug_set_breakpoint", {"location": ENTRY_FUNCTION})
    _, second = session.call("debug_set_breakpoint", {"location": HANDLER_FUNCTION})
    assert first["ok"] is True and second["ok"] is True, (first, second)

    errored, cleared = session.call("debug_clear_breakpoints")
    assert errored is False, cleared
    assert cleared["ok"] is True, cleared
    assert cleared["cleared"] == 2, cleared
    assert cleared["backend_reconciled"] is True, cleared
    assert cleared["summary"] == "All breakpoints cleared and reconciled with the backend.", cleared

    _, listed = session.call("debug_list_breakpoints")
    assert listed["breakpoints"] == [], listed

    errored, again = session.call("debug_clear_breakpoints")
    assert errored is False, again
    assert again["ok"] is True, again
    assert again["cleared"] == 0, again
    assert again["backend_reconciled"] is True, again


def test_a_resume_stops_at_the_breakpoint_that_was_set_and_names_which_one(session: McpServer) -> None:
    """The core runs into a breakpoint, and the stop says whose it is.

    A stop the product cannot match to a breakpoint it set comes back as
    `unexpected_breakpoint`, which is an abnormal stop: it fails `target_ok`, it
    fails a plan step, and it tells an agent the board trapped on something
    nobody asked for. So the claim is not merely that execution stopped, it is
    that the stop was attributed: `breakpoint_hit`, expected, carrying the id
    this caller was given and the frame it stopped in.
    """
    _, set_result = session.call("debug_set_breakpoint", {"location": ENTRY_FUNCTION})
    tracked = set_result["breakpoint"]

    errored, stopped = session.call("debug_continue", {"timeout_s": REACHABLE_STOP_TIMEOUT_S})

    assert errored is False, stopped
    assert stopped["ok"] is True, stopped
    assert stopped["stop_reason"] == "breakpoint_hit", stopped
    assert stopped["target_ok"] is True, stopped
    assert stopped["target_stop_reason"] == "breakpoint_hit", stopped
    assert stopped["side_effect_status"] == "committed", stopped
    stop = stopped["stop"]
    assert stop["breakpoint_expected"] is True, stop
    assert stop["breakpoint_id"] == tracked["id"], (stop, tracked)
    assert stop["backend_breakpoint_id"] == tracked["backend_id"], (stop, tracked)
    assert stop["frame"]["function"] == ENTRY_FUNCTION, stop
    assert stopped["session"]["status"] == "halted", stopped["session"]

    # The same answer read back through the tool an agent asks after the fact,
    # which reads the session rather than waiting on the target.
    errored, reason = session.call("debug_get_stop_reason")
    assert errored is False, reason
    assert reason["stop_reason"] == "breakpoint_hit", reason
    assert reason["stop"]["breakpoint_id"] == tracked["id"], reason


def test_a_resume_to_a_file_and_line_breakpoint_stops_inside_that_function(session: McpServer, bench: Bench) -> None:
    """The file and line form, proved against code rather than against a parser.

    Setting it only proves the string was accepted. What this catches is a file
    and line that GDB resolved somewhere else, or to nothing that ever executes:
    the frame the core stopped in has to be the function that line is written in,
    and the stop has to be attributed to the breakpoint this caller set.
    """
    line = demo_source_line(bench.project, COUNTER_STATEMENT)
    _, set_result = session.call("debug_set_breakpoint", {"location": {"file": DEMO_SOURCE_NAME, "line": line}})
    tracked = set_result["breakpoint"]

    errored, stopped = session.call("debug_continue", {"timeout_s": REACHABLE_STOP_TIMEOUT_S})

    assert errored is False, stopped
    assert stopped["stop_reason"] == "breakpoint_hit", stopped
    stop = stopped["stop"]
    assert stop["breakpoint_id"] == tracked["id"], (stop, tracked)
    assert stop["breakpoint_expected"] is True, stop
    assert stop["frame"]["function"] == HANDLER_FUNCTION, stop


@pytest.mark.xfail(reason="#495", strict=True)
def test_a_resume_with_nothing_to_stop_it_times_out_and_halts_the_running_target(session: McpServer) -> None:
    """The resume that runs out, which is also how a running target gets halted.

    The demo's main loop never returns, so a resume with no breakpoint set is a
    core that will still be running when the timeout expires. Three things then
    have to be true at once and each has failed on its own: the call reports the
    timeout rather than a stop that did not happen; the target is contained
    before the answer is returned, because a bench left free-running is a bench
    the next test measures nothing on; and, precisely because containment
    succeeded, the failure is not a quarantine. A timeout that padlocked a bench
    whose target it had just halted made every over-short timeout an operator
    visit.
    """
    _, listed = session.call("debug_list_breakpoints")
    assert listed["breakpoints"] == [], listed

    errored, timed_out = session.call("debug_continue", {"timeout_s": UNREACHABLE_STOP_TIMEOUT_S})

    assert errored is True, timed_out
    assert timed_out["ok"] is False, timed_out
    assert timed_out["error_type"] == "timeout", timed_out
    assert timed_out["stop_reason"] == "timeout", timed_out
    assert timed_out["halt_requested"] is True, timed_out
    assert timed_out["halt_command_acknowledged"] is True, timed_out
    assert timed_out["halt_confirmed"] is True, timed_out
    assert timed_out["target_state"] == "halted", timed_out
    assert timed_out["side_effect_committed"] is True, timed_out
    assert timed_out["side_effect_status"] == "committed", timed_out
    assert timed_out.get("quarantined") is not True, timed_out
    assert timed_out.get("cleanup_required") is not True, timed_out
    # The stop that was recorded is the halt that was confirmed, not the deadline
    # that ran out: those are two different facts and the result carries both.
    assert timed_out["target_stop_reason"] != "timeout", timed_out

    # The session's own account of where that left it. What is read here is the
    # session state and not the target's last stop reason: which signal a probe
    # reports for an interrupt is that probe's business, and the claim is that
    # the session says the core is stopped and that it is still usable.
    _, status = session.call("debug_get_session_status")
    assert status["ok"] is True, status
    assert status["active"] is True, status
    assert status["status"] == "halted", status
    assert status.get("quarantined") is not True, status
    assert status.get("cleanup_required") is not True, status


@pytest.mark.xfail(reason="#495", strict=True)
def test_clearing_a_breakpoint_the_target_is_sitting_on_leaves_the_stop_reason_and_the_next_resume_alone(session: McpServer) -> None:
    """Clear at a breakpoint, then read the stop reason, then resume again.

    Removing a breakpoint does not move the core, so the stop reason afterwards
    is still the breakpoint that was hit. The failure mode is quiet and its
    consequence is not: a cleanup that overwrites the recorded stop with a
    debugger error makes the very next resume refuse with "target is already
    stopped", so an agent that tidied up after its own breakpoint can no longer
    run the board at all. Both halves are asserted, because the poisoned stop is
    only visible in the second one.
    """
    _, set_result = session.call("debug_set_breakpoint", {"location": ENTRY_FUNCTION})
    _, stopped = session.call("debug_continue", {"timeout_s": REACHABLE_STOP_TIMEOUT_S})
    assert stopped["stop_reason"] == "breakpoint_hit", stopped

    _, cleared = session.call("debug_clear_breakpoints")
    assert cleared["ok"] is True, cleared
    assert cleared["cleared"] == 1, cleared

    errored, reason = session.call("debug_get_stop_reason")
    assert errored is False, reason
    assert reason["stop_reason"] == "breakpoint_hit", reason
    assert reason["stop"]["breakpoint_id"] == set_result["breakpoint"]["id"], reason

    # The core is where the breakpoint left it and nothing stops it any more, so
    # this resume runs out. What matters is that it *ran*: a refusal here would
    # carry the already-stopped summary instead of the timeout.
    errored, resumed = session.call("debug_continue", {"timeout_s": UNREACHABLE_STOP_TIMEOUT_S})
    assert errored is True, resumed
    assert resumed["error_type"] == "timeout", resumed
    assert resumed["halt_confirmed"] is True, resumed
    assert "already stopped" not in resumed["summary"].lower(), resumed


@pytest.mark.xfail(reason="#492", strict=True)
def test_halting_a_target_already_stopped_at_a_breakpoint_answers_that_it_was_already_stopped(session: McpServer) -> None:
    """Halt at a breakpoint, which an agent asks for whenever it is unsure.

    Asking a stopped core to stop is the ordinary shape of a cautious caller: it
    hit a breakpoint, it is about to read memory, and it makes sure. The answer
    owed is the state the target is already in, and the tool has a path for
    exactly that. What happens instead is that the halt waits for a stop event
    the target has no reason to emit twice, times out, and quarantines the bench
    over a core that never moved, so a correct sequence ends with an operator
    being asked to inspect a board that is sitting exactly where it was told to
    sit.

    The quarantine that leaves behind is cleared by this file's own teardown,
    through `agentic-hil recover`, so the tests after it meet a bench with
    nothing standing.
    """
    _, set_result = session.call("debug_set_breakpoint", {"location": ENTRY_FUNCTION})
    _, stopped = session.call("debug_continue", {"timeout_s": REACHABLE_STOP_TIMEOUT_S})
    assert stopped["stop_reason"] == "breakpoint_hit", stopped

    errored, halted = session.call("debug_halt")

    assert errored is False, halted
    assert halted["ok"] is True, halted
    assert halted["stop_reason"] == "breakpoint_hit", halted
    assert halted["target_ok"] is True, halted
    assert halted["stop"]["breakpoint_id"] == set_result["breakpoint"]["id"], halted
    assert halted["summary"].startswith("Target was already stopped"), halted
    assert halted.get("quarantined") is not True, halted
    assert halted.get("cleanup_required") is not True, halted
