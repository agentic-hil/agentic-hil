"""What a debug session answers about a command the debugger refused, and about itself afterwards.

Six issues found on the same module, `backends/gdbdebug.py`, and on the two
layers that read its results (`tools.py`'s quarantine decision and the
catalogue text a caller is sent to). Every sequence here is one an agent
reaches by ordinary use: a breakpoint location typed wrong, a symbol the target
will not read, a GDB that dies under a resume, a stop whose teardown could not
be confirmed. None of them changed anything on the board, and each one was
answered as though something had.

* #653: a `debug_continue` whose stop arrives as `debugger_error` reports
  "Target stopped" with a `committed` effect, so the caller reads a core that
  halted at a known point and the bench is not held.
* #652: `debug_halt` on a session the status read calls `active` with status
  `error` answers "No debug session is active. Start one with
  debug_start_session first.", while the quarantine guide and the tool's own
  definition promise that a halt lifts the hold.
* #651: `debug_list_breakpoints` answers `active: true` with the breakpoints of
  a session that ended `cleanup_required`, and says nothing about either.
* #650: a refusal that sent nothing to GDB adds a second cleanup reason to the
  incident, which is what makes an incident the product settles by itself one
  only an operator can end.
* #649: a command GDB refused with `^error` holds the bench as if its effect
  were unknown, and the next audited call loses the session and its
  breakpoints.
* #648: a command GDB refused with `^error` overwrites the target's last stop
  with `debugger_error`, so the session can no longer resume.

Driven through the deterministic fakes the suite already has: the fake GDB's own
opt-in behaviours (`tests/fixtures/fake_gdb.py`) for a GDB that exits, a target
whose run state is the bench's, and a memory read the debugger refuses, and the
transport-level wrapper the issues themselves prescribe for the refusals the
fake has no behaviour for. No hardware recording is invented here; where a
message stands in for a real debugger's words, it says so in its own name.

Three of the six issues leave a design decision open and name the options. The
tests for those accept every named option and reject only what the issue
reports, so approving them settles nothing the issue left to be settled. Each
such assertion says in a comment which options it accepts. What is left
uncovered for the same reason is listed beside the tests that stop short.

One test here is green before any fix and says so in its own docstring: it pins
the neighbour a fix for #649 could take with it. A command whose GDB died while
it was pending reaches the product as the same `result_class="error"` a refused
command does, and telling those apart is the whole of #649, so the case that
must keep holding the bench is pinned beside the cases that must stop holding
it.
"""
from __future__ import annotations

import contextlib
import re
from collections.abc import Iterator
from pathlib import Path

import pytest
from fixtures.fake_gdb import (
    BENCH_RUN_STATE,
    GDB_EXITS_AFTER_RUNNING,
    GDB_EXITS_BEFORE_ANSWERING,
    HALT_TIMEOUT,
    MEMORY_READ_REFUSAL,
    MEMORY_READ_REFUSED,
)
from test_debug_backend_refusals import (
    UNCONFIRMED_CLOSE_SENTENCE,
    answered_within,
    assert_close_refused_to_call_the_target_settled,
    closed_reporting_its_own_failure,
)
from test_debug_sessions import TIMEOUT_TEST_CAP_S, debug_service, start_debug_session

from agentic_hil.contracts import MCP_TOOLS
from agentic_hil.gdbmi import GdbMiCommandResult
from agentic_hil.tools import AgenticHILToolService

BREAKPOINT_SYMBOL = "test_done"
READABLE_SYMBOL = "CTC_array"
# A resume that has a breakpoint to arrive at, and one that has nothing to stop
# it, as tests/test_debug_session_run_state.py sizes them against the same fake.
REACHABLE_STOP_TIMEOUT_S = 5.0
UNREACHABLE_STOP_TIMEOUT_S = 0.5
# The wait a call whose GDB has died is given before the test calls it hung. Long
# enough that a loaded machine does not fail it, short enough that a transport
# which never answers is reported as a red test rather than a stopped suite.
DEAD_GDB_ANSWER_S = 30.0
DEAD_GDB_CLOSE_S = 60.0
INTERRUPT_COMMAND = "-exec-interrupt --all"
BREAKPOINT_INSERT_COMMAND = "-break-insert"
RESUME_COMMAND = "-exec-continue"

# The incident reasons these sequences raise, spelled where coordination.py
# spells them so a reason that is renamed fails these tests by name.
SESSION_CLEANUP_UNCONFIRMED = "debug_session_cleanup_unconfirmed"
TARGET_STATE_UNCONFIRMED = "debug_target_state_unconfirmed"

# The transport answers the issues ask for, built here rather than in the fake:
# the fake accepts every breakpoint insert and answers every resume and
# interrupt, which is what the rest of the suite relies on, so a refusal is
# injected at the transport the product calls instead of taking that away.
#
# The insert's message is the one #648 and #649 quote from a real GDB. No
# recording of a real GDB refusing `-exec-continue` or an interrupt exists, so
# those two carry text that says so rather than a plausible sentence a later
# reader could mistake for a recording; what the tests claim about them is only
# that whatever the debugger answered reaches the caller.
NO_SOURCE_FILE_MESSAGE = "No source file named missing.c."
UNRECORDED_RESUME_REFUSAL = "fake GDB refused -exec-continue; no recording of a real refusal exists"
UNRECORDED_INTERRUPT_REFUSAL = "fake GDB refused -exec-interrupt --all; no recording of a real refusal exists"
REFUSED_INSERT_LINE = '1^error,msg="No source file named missing.c."'
REFUSED_RESUME_LINE = f'2^error,msg="{UNRECORDED_RESUME_REFUSAL}"'
REFUSED_INTERRUPT_LINE = f'3^error,msg="{UNRECORDED_INTERRUPT_REFUSAL}"'
REFUSED_INSERT = GdbMiCommandResult(result_class="error", line=REFUSED_INSERT_LINE, records=[REFUSED_INSERT_LINE], error_message=NO_SOURCE_FILE_MESSAGE)
REFUSED_RESUME = GdbMiCommandResult(result_class="error", line=REFUSED_RESUME_LINE, records=[REFUSED_RESUME_LINE], error_message=UNRECORDED_RESUME_REFUSAL)
REFUSED_INTERRUPT = GdbMiCommandResult(result_class="error", line=REFUSED_INTERRUPT_LINE, records=[REFUSED_INTERRUPT_LINE], error_message=UNRECORDED_INTERRUPT_REFUSAL)
# What `GdbMiClient.command` returns for a command that was never answered,
# field for field, so the one case #652 builds by hand is the case the transport
# in fact produces.
TIMED_OUT_COMMAND = GdbMiCommandResult(result_class="timeout", line="", timed_out=True, error_message="GDB/MI command timed out.")

# The two sentences #652 says cannot both stand while a halt on an `error`
# session is refused: the quarantine guide's physical check for
# `debug_target_state_unconfirmed`, and the `debug_halt` definition's. Each is
# read for the promise it makes and for a correctly directed condition: only
# while the session is not in error, or unless the session is in error. An
# option (a) fix satisfies the test by making the halt run. A rewrite that drops
# the promise altogether satisfies it too.
HALT_PROMISE_IN_THE_GUIDE = "debug_halt"
HALT_PROMISE_IN_THE_DEFINITION = "halt can lift it"
ERROR_SESSION_QUALIFIER = re.compile(
    r"(?P<kind>only while|only when|unless).{0,80}(?:session|status).{0,40}error",
    re.IGNORECASE,
)


def halt_promise_is_conditioned_on_session_state(text: str, promise: str) -> bool:
    """Allow a removed promise or a promise limited to non-error sessions."""
    lowered = text.lower()
    promise_at = lowered.find(promise.lower())
    if promise_at < 0:
        return True
    nearby_text = lowered[promise_at : promise_at + 180]
    for match in ERROR_SESSION_QUALIFIER.finditer(nearby_text):
        condition = match.group(0)
        says_not_in_error = re.search(r"\bnot\b.{0,24}\berror\b", condition) is not None
        if (match.group("kind").lower() == "unless" and not says_not_in_error) or (
            match.group("kind").lower() != "unless" and says_not_in_error
        ):
            return True
    return False


# What option (c) of #651 owes the caller instead of a field: a definition that
# says the list can be the record of a session that ended unconfirmed. Either
# phrase ties the record to a session that ended unconfirmed; neither appears in
# the definition today.
UNCONFIRMED_SESSION_IN_A_DEFINITION = (
    "record of a session that ended unconfirmed",
    "record may describe a session that ended unconfirmed",
)

# The summary #653 asks for says the debugger failed while the core was running.
# The sentence is the product's to write, so what is read out of it is that it
# names the debugger and does not claim a stop, in any of the spellings an
# honest sentence about this would use. Today's summary, "Target stopped:
# debugger_error.", names the debugger and claims the stop.
THE_DEBUGGER_FAILED = ("debugger", "gdb")
THE_CORE_WAS_NOT_STOPPED = ("running", "resumed", "ran")
A_TARGET_THAT_STOPPED = "target stopped"


def tool_description(name: str) -> str:
    return next(str(tool["description"]) for tool in MCP_TOOLS if tool["name"] == name)


@contextlib.contextmanager
def settled_afterwards(service: AgenticHILToolService) -> Iterator[None]:
    """Close the service, and make the clean close part of the claim.

    The same contract tests/test_debug_session_run_state.py states at length:
    every sequence wrapped in this is one the product must leave a session it
    can end with the target confirmed halted, so the close has to succeed on the
    way out. On the way out of a failed assertion the close is allowed to refuse
    (what the test found is often exactly what leaves it unable to reconfirm the
    halt), and that refusal must not replace the assertion that found it.
    """
    try:
        yield
    except BaseException:
        try:
            service.close()
        except RuntimeError:
            service.coordinator.close()
        raise
    try:
        service.close()
    except RuntimeError:
        service.coordinator.close()
        raise


def closed_however_the_session_ended(service: AgenticHILToolService) -> None:
    """Close the service, and stand the bench down whichever way the close went.

    For the tests whose ending turns on a decision the issue leaves open: a
    bench left held refuses the close, because the lease cannot be released, and
    a bench left settled closes cleanly. Asserting either would pick an option
    the issue did not. Both have to stop holding this worker's bench, which is
    what closing the coordinator does, and the close's own contract is pinned
    where it is not open, in tests/test_debug_sessions.py and
    tests/test_debug_session_run_state.py.
    """
    try:
        service.close()
    except RuntimeError:
        service.coordinator.close()


@contextlib.contextmanager
def gdb_refusing(service: AgenticHILToolService, command_prefix: str, response: GdbMiCommandResult) -> Iterator[None]:
    """The session's GDB answering one command with `response`, and every other as it would.

    The instrument #648 and #649 prescribe: "wrap the session's `gdb.command` so
    `-break-insert` returns `GdbMiCommandResult(result_class="error", ...)`".
    The wrapped command never reaches the fake, so nothing else about the fake's
    answers moves, and the product meets exactly the result class a real GDB's
    `^error` produces.
    """
    session = service.backend._debug.session
    assert session is not None and session.gdb is not None, "no session to refuse a command on"
    original = session.gdb.command

    def answer(mi_command: str, timeout_s: float) -> GdbMiCommandResult:
        if mi_command.startswith(command_prefix):
            return response
        return original(mi_command, timeout_s)

    session.gdb.command = answer
    try:
        yield
    finally:
        session.gdb.command = original


def session_the_detach_guard_left_unsettled(service: AgenticHILToolService, monkeypatch: pytest.MonkeyPatch) -> dict:
    """A session with one breakpoint whose stop could not install the detach guard.

    The setup #650 and #651 both point at, driven the way the detach guard tests
    in tests/test_debug_sessions.py drive it: OpenOCD resumes an attached target
    on `gdb-detach` unless the override is installed, so a refused monitor
    command leaves the stop unable to call the target settled. What is left is a
    session registered as `cleanup_required` with `hardware_state_unconfirmed`,
    its processes gone, its breakpoint still on its own record, and an incident
    whose single reason is `debug_session_cleanup_unconfirmed`.

    Returns the breakpoint the session recorded.
    """
    debug = service.backend._debug
    original = debug._gdb_command

    def refuse_detach_guard(session, command: str, timeout_s=None, **kwargs):
        if "gdb-detach" in command:
            return GdbMiCommandResult(result_class="error", line="", error_message="monitor command refused")
        return original(session, command, timeout_s, **kwargs)

    assert start_debug_session(service, mode="attach")["ok"] is True
    set_result = service.call("debug_set_breakpoint", {"location": {"symbol": BREAKPOINT_SYMBOL}})
    assert set_result["ok"] is True, set_result
    monkeypatch.setattr(debug, "_gdb_command", refuse_detach_guard)
    stopped = service.call("debug_stop_session")
    monkeypatch.setattr(debug, "_gdb_command", original)
    assert stopped["ok"] is False, stopped
    assert stopped["error_type"] == "detach_resume_not_confirmed", stopped
    assert stopped["status"] == "cleanup_required", stopped
    return dict(set_result["breakpoint"])


def guidance_physical_check(result: dict, reason: str) -> str:
    """The physical check the result's own quarantine guidance names for one reason."""
    guidance = result.get("quarantine_guidance")
    assert isinstance(guidance, list), result
    entry = next((item for item in guidance if item.get("reason") == reason), None)
    assert entry is not None, result
    return str(entry["physical_check"])


# #653: a resume whose debugger died, reported as a target that stopped.


def test_a_resume_whose_gdb_dies_is_not_reported_as_a_target_that_stopped(tmp_path: Path) -> None:
    """`^running`, then the pipe closes: the answer says the debugger failed while the core ran.

    The stop `debug_continue` waited for never comes; the exit comes instead,
    and it is built into the answer by the same `_stopped_result` a breakpoint
    hit uses, so the summary says the target stopped and the effect reads
    `committed`. Nothing confirmed that it stopped: the core was resumed and the
    only thing that could have observed it is gone. `debug_halt` already answers
    this shape of stop the other way, with the halt unconfirmed and the target
    state unknown, which holds the bench, and that is what is owed here.

    The close is unchanged and asserted as such: a session whose GDB died still
    refuses to call the target settled (#506), and no fix for this may buy its
    green by skipping that.
    """
    service = debug_service(tmp_path, fake_gdb_behavior=GDB_EXITS_AFTER_RUNNING)
    try:
        assert start_debug_session(service)["ok"] is True
        continued = answered_within(DEAD_GDB_ANSWER_S, lambda: service.call("debug_continue", {"timeout_s": 5}))
        blocked_after_the_resume = service.coordinator.blocked
    finally:
        closing = answered_within(DEAD_GDB_CLOSE_S, lambda: closed_reporting_its_own_failure(service))

    assert continued["ok"] is False, continued
    assert continued["error_type"] == "debugger_error", continued
    assert continued["stop_reason"] == "debugger_error", continued
    assert continued["stop"]["backend_error"] == "GDB process exited with code 0.", continued["stop"]
    assert continued["session"]["status"] == "error", continued
    # The two fields the halt answers this stop with, and the one the caller
    # reads before it believes the core is where it was told to be.
    assert continued.get("target_state") == "unknown", continued
    assert continued["side_effect_status"] == "unknown", continued
    assert continued.get("side_effect_committed") is not True, continued
    # The summary says the debugger failed while the core was running, and no
    # longer that the target stopped. The sentence is the product's to write;
    # what it may not do is name a stop nobody observed.
    summary = continued["summary"].lower()
    assert A_TARGET_THAT_STOPPED not in summary, continued
    assert any(word in summary for word in THE_DEBUGGER_FAILED), continued
    assert any(word in summary for word in THE_CORE_WAS_NOT_STOPPED), continued
    # The bench is held, under the reason a resume that lost the target's state
    # already raises when its containment fails.
    assert continued["cleanup_reasons"] == [TARGET_STATE_UNCONFIRMED], continued
    assert continued["quarantined"] is True, continued
    assert blocked_after_the_resume is True, "the bench was not held after a resume that lost the target"
    assert_close_refused_to_call_the_target_settled(closing, service)


def test_a_resume_whose_gdb_died_with_the_command_pending_still_holds_the_bench(tmp_path: Path) -> None:
    """The neighbour #649's answer must not move, stated because nothing else states it.

    The transport reports a GDB that died with a command pending as
    `GdbMiCommandResult(result_class="error", timed_out=False)`, which is the
    same shape as the `^error` a live GDB answers a refused command with: the
    only difference is that one proves the command did not run and the other
    proves nothing at all. Here the pipe closed with `-exec-continue` pending,
    so whether the core was resumed is genuinely unknown, and the bench is held
    for it.

    A fix for #649 that reads `result_class == "error"` and no timeout as "the
    command did not run" would answer `not_started` here and stop holding a
    bench whose core may be running. The sibling tests in
    tests/test_debug_backend_refusals.py pin this call's words and its close,
    not its effect or its hold, so this is where that stays pinned.
    """
    service = debug_service(tmp_path, fake_gdb_behavior=GDB_EXITS_BEFORE_ANSWERING)
    try:
        assert start_debug_session(service)["ok"] is True
        continued = answered_within(DEAD_GDB_ANSWER_S, lambda: service.call("debug_continue", {"timeout_s": 5}))
        blocked_after_the_resume = service.coordinator.blocked
    finally:
        closing = answered_within(DEAD_GDB_CLOSE_S, lambda: closed_reporting_its_own_failure(service))

    assert continued["ok"] is False, continued
    assert continued["error_type"] == "debugger_error", continued
    assert continued["summary"] == "GDB process exited with code 0.", continued
    assert continued["side_effect_status"] == "unknown", continued
    assert continued.get("side_effect_committed") is not False, continued
    assert continued["cleanup_reasons"] == [TARGET_STATE_UNCONFIRMED], continued
    assert continued["quarantined"] is True, continued
    assert blocked_after_the_resume is True, "the bench was not held after a resume whose GDB died mid-command"
    assert_close_refused_to_call_the_target_settled(closing, service)


# #652: a halt on a session the status read calls active.


def test_a_halt_on_an_error_session_does_not_answer_that_no_session_is_active(tmp_path: Path) -> None:
    """The status read and the halt describe one session, so they cannot disagree about whether it exists.

    After a `debugger_error` stop the session is still registered, with status
    `error`, and `debug_get_session_status` answers `active: true` for it.
    `debug_halt` answers "No debug session is active. Start one with
    debug_start_session first.", which is the one thing that cannot be true of
    the session the read just described, and it names the start that the
    registered session is what refuses.

    The decision #652 leaves open is what the halt does instead, and both named
    options satisfy this test: (a) it acts on the session, so there is no
    refusal to be wrong; (b) it refuses in terms of the state it is in, with its
    own `error_type` or a summary naming `debug_stop_session`, which is the
    route the `session_not_active` remediation already names for an
    error-ended session. The `error_type` itself is left alone here for the same
    reason: under (b) it may stay as it is, with the summary carrying the
    difference.

    Here GDB is gone, so option (a) cannot apply (it covers an `error` session
    whose GDB still runs); the disjunction is kept so the same claim reads the
    same way wherever the halt does run.
    """
    service = debug_service(tmp_path, fake_gdb_behavior=GDB_EXITS_AFTER_RUNNING)
    try:
        assert start_debug_session(service)["ok"] is True
        continued = answered_within(DEAD_GDB_ANSWER_S, lambda: service.call("debug_continue", {"timeout_s": 5}))
        status = answered_within(DEAD_GDB_ANSWER_S, lambda: service.call("debug_get_session_status"))
        halted = answered_within(DEAD_GDB_ANSWER_S, lambda: service.call("debug_halt", {"timeout_s": 1}))
    finally:
        closing = answered_within(DEAD_GDB_CLOSE_S, lambda: closed_reporting_its_own_failure(service))

    assert continued["stop_reason"] == "debugger_error", continued
    assert status["active"] is True, status
    assert status["status"] == "error", status
    assert halted["ok"] is True or "No debug session is active" not in halted["summary"], halted
    answers_in_terms_of_the_error_session = (
        halted["ok"] is True
        or halted.get("error_type") != "session_not_active"
        or "debug_stop_session" in halted["summary"]
    )
    assert answers_in_terms_of_the_error_session, halted
    assert_close_refused_to_call_the_target_settled(closing, service)


def test_the_hold_a_failed_resume_leaves_promises_no_halt_this_session_refuses(tmp_path: Path) -> None:
    """The guide and the definition send the caller to a halt; the session has to admit one.

    A `debug_continue` whose `-exec-continue` was never answered sets the
    session to `error` and holds the bench under
    `debug_target_state_unconfirmed`. The guidance that travels with that hold
    says a successful `debug_halt` clears it without an operator, and the
    `debug_halt` definition says a confirmed halt can lift it. No halt runs on
    an `error` session, so the caller is sent to a call that can never succeed
    and the bench waits for a person.

    Both named options satisfy this: (a) the halt acts on the session, GDB being
    still alive here, and clears the hold the guidance said it would; (b) the
    halt stays refused and the two sentences say that a halt lifts the hold only
    while the session is not in `error`. A rewrite that stops promising the halt
    at all satisfies it too. What fails is today's pairing: an unqualified
    promise beside a refusal.
    """
    service = debug_service(tmp_path)
    try:
        assert start_debug_session(service, mode="attach")["ok"] is True
        with gdb_refusing(service, RESUME_COMMAND, TIMED_OUT_COMMAND):
            continued = service.call("debug_continue", {"timeout_s": 5})

        assert continued["ok"] is False, continued
        assert continued["error_type"] == "timeout", continued
        assert continued["session"]["status"] == "error", continued
        assert continued["cleanup_reasons"] == [TARGET_STATE_UNCONFIRMED], continued
        promise = guidance_physical_check(continued, TARGET_STATE_UNCONFIRMED)

        halted = service.call("debug_halt", {"timeout_s": 1})

        halt_ran = halted["ok"] is True
        guide_promises_a_halt = HALT_PROMISE_IN_THE_GUIDE in promise
        guide_promise_is_conditioned = halt_promise_is_conditioned_on_session_state(promise, HALT_PROMISE_IN_THE_GUIDE)
        definition = tool_description("debug_halt")
        definition_promises_a_halt = HALT_PROMISE_IN_THE_DEFINITION in definition
        definition_promise_is_conditioned = halt_promise_is_conditioned_on_session_state(definition, HALT_PROMISE_IN_THE_DEFINITION)
        assert halt_ran or not guide_promises_a_halt or guide_promise_is_conditioned, (halted, promise)
        assert halt_ran or not definition_promises_a_halt or definition_promise_is_conditioned, (halted, definition)
        if halt_ran:
            # The promise, kept: the guidance says this clears without an
            # operator, so a halt that succeeded leaves nothing held.
            assert service.coordinator.blocked is False, halted
    finally:
        closed_however_the_session_ended(service)


# #651: the breakpoint list of a session that ended unconfirmed.


def test_the_breakpoint_list_of_a_cleanup_required_session_says_the_session_ended_unconfirmed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A caller checking its breakpoints before a resume is not told the session is finished.

    After a stop that could not confirm the detach guard, the session is
    `cleanup_required`, its processes are gone, and every effect tool on it
    answers `resource_quarantined`. `debug_get_session_status` says all of that.
    `debug_list_breakpoints` answers `ok: true`, `active: true` and the
    breakpoints the session recorded, with no status and none of those fields,
    so the record of a session that ended unconfirmed reads like a live one
    holding live breakpoints.

    #651 leaves the behaviour open and names three options, and this test
    accepts each: (a) the list carries the session's `status` and, for
    `cleanup_required`, the same `cleanup_required`, `quarantined` and
    `hardware_state` fields the status read carries; (b) it refuses with
    `resource_quarantined` as the other session tools do; (c) it answers as it
    does now and the definition says the list can be the record of a session
    that ended unconfirmed. What fails is the one case the issue reports:
    nothing in the result and nothing in the definition.
    """
    service = debug_service(tmp_path)
    try:
        recorded = session_the_detach_guard_left_unsettled(service, monkeypatch)

        status = service.call("debug_get_session_status")
        assert status["active"] is True, status
        assert status["status"] == "cleanup_required", status
        assert status["cleanup_required"] is True, status
        assert status["quarantined"] is True, status
        assert status["hardware_state"] == "unknown", status

        listed = service.call("debug_list_breakpoints")

        carries_the_session_s_state = (
            listed.get("status") == status["status"]
            and listed.get("cleanup_required") is True
            and listed.get("quarantined") is True
            and listed.get("hardware_state") == "unknown"
        )
        refuses_as_the_other_session_tools_do = listed.get("error_type") == "resource_quarantined"
        definition = tool_description("debug_list_breakpoints")
        definition_says_the_record_may_outlive_the_session = any(phrase in definition for phrase in UNCONFIRMED_SESSION_IN_A_DEFINITION)
        assert carries_the_session_s_state or refuses_as_the_other_session_tools_do or definition_says_the_record_may_outlive_the_session, (listed, definition)
        if listed.get("ok") is True:
            # Whichever of the two answering options was taken, the record it
            # answers with is the one the session holds: the list reads it
            # without asking GDB, and nothing here deleted anything. The
            # breakpoint is also why this matters: it is what the caller would
            # resume onto if it read this list as a live session's.
            assert listed["breakpoints"] == [recorded], listed
    finally:
        with pytest.raises(RuntimeError, match="auto-resume-on-detach"):
            service.close()
        service.coordinator.close()


# #650: what a refusal adds to an incident it never acted on.


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [("debug_halt", {"timeout_s": 1}), ("debug_clear_breakpoints", {})],
    ids=["halt", "clear-breakpoints"],
)
def test_a_refusal_on_a_cleanup_required_session_leaves_the_incidents_reasons_alone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tool: str, arguments: dict) -> None:
    """A refusal that sent nothing to GDB must not turn a settleable incident into an operator's.

    `debug_halt` is the natural first call on a session whose halt was not
    confirmed, and the catalogue sends callers to it. Nothing is sent: the
    session is `cleanup_required`, so the call is refused before it reaches
    GDB. The refusal is nevertheless recorded as a second cleanup reason, chosen
    by tool name, and automatic recovery settles only an incident with exactly
    one reason. One refused call therefore ends the route the product had out of
    its own incident: `probe_target` drives the target, reads it back, and still
    reports the incident open.

    Both refused calls here are containment tools, which the quarantine gate
    lets through on purpose, so each reaches the session layer's refusal exactly
    as a caller following the guidance does.
    """
    service = debug_service(tmp_path)
    with settled_afterwards(service):
        session_the_detach_guard_left_unsettled(service, monkeypatch)
        assert service.coordinator.retryable_incident() == SESSION_CLEANUP_UNCONFIRMED

        refused = service.call(tool, arguments)

        assert refused["ok"] is False, refused
        assert refused["error_type"] == "resource_quarantined", refused
        # The session's own state still travels on the refusal; what may not is a
        # reason for something this call never did.
        assert refused["cleanup_required"] is True, refused
        assert refused["quarantined"] is True, refused
        assert refused["cleanup_reasons"] == [SESSION_CLEANUP_UNCONFIRMED], refused
        assert service.coordinator.retryable_incident() == SESSION_CLEANUP_UNCONFIRMED

        # The route the catalogue promises for this reason, still open: a probe
        # that reads the target back ends the incident and the session with it.
        probed = service.call("probe_target")
        assert probed["ok"] is True, probed
        assert probed.get("quarantined") is not True, probed
        assert probed.get("cleanup_required") is not True, probed
        assert service.coordinator.blocked is False, probed


def test_a_refused_halt_leaves_a_new_session_able_to_start_over_the_settled_one(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The issue's last line: without the refused halt, `debug_start_session` succeeds.

    A start over a `cleanup_required` session is one of the two routes out of
    this incident, and it is the one a caller takes without being told to: the
    automatic recovery runs before the start is dispatched, settles the single
    reason, discards the session it settled, and the start then opens a new one.
    One refused `debug_halt` in between is enough to close that route, because
    the incident then names two reasons and the recovery settles none, so the
    start is refused as `session_already_active` over a session nothing can use.
    """
    service = debug_service(tmp_path)
    with settled_afterwards(service):
        session_the_detach_guard_left_unsettled(service, monkeypatch)

        refused = service.call("debug_halt", {"timeout_s": 1})
        assert refused["error_type"] == "resource_quarantined", refused

        started = start_debug_session(service, mode="attach")

        assert started["ok"] is True, started
        assert started["session"]["status"] == "halted", started
        assert service.coordinator.blocked is False, started
        assert service.call("debug_stop_session")["ok"] is True


def test_a_refused_symbol_read_on_a_cleanup_required_session_adds_no_reason_of_its_own(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The third tool #650 names, on a bench whose policy runs no recovery of its own.

    `debug_symbol_info` is not a containment tool, so on a bench that may
    recover automatically the incident is settled before the call is dispatched
    and the refusal it then answers names no session at all. With
    `recovery.auto_recover: off` nothing settles first, the call reaches the
    session layer's refusal, and what it records is visible on its own: today a
    `debug_session_result_unconfirmed` for a read that was never sent.

    The policy is named here only to reach that path. The claim is the same one
    either way, and it is the issue's own sentence: a refusal that sent nothing
    to GDB leaves the incident's reasons as they were.
    """
    service = debug_service(tmp_path, auto_recover="off")
    try:
        session_the_detach_guard_left_unsettled(service, monkeypatch)
        assert service.coordinator.retryable_incident() == SESSION_CLEANUP_UNCONFIRMED

        refused = service.call("debug_symbol_info", {"symbol": READABLE_SYMBOL})

        assert refused["ok"] is False, refused
        assert refused["error_type"] == "resource_quarantined", refused
        assert refused["cleanup_reasons"] == [SESSION_CLEANUP_UNCONFIRMED], refused
        assert service.coordinator.retryable_incident() == SESSION_CLEANUP_UNCONFIRMED
    finally:
        with pytest.raises(RuntimeError, match="auto-resume-on-detach"):
            service.close()
        service.coordinator.close()


# #649: what a command GDB refused says about its own effect.


@pytest.mark.parametrize(
    ("refused_command", "response", "tool", "arguments"),
    [
        (BREAKPOINT_INSERT_COMMAND, REFUSED_INSERT, "debug_set_breakpoint", {"location": {"file": "missing.c", "line": 10}}),
        (RESUME_COMMAND, REFUSED_RESUME, "debug_continue", {"timeout_s": 5}),
    ],
    ids=["breakpoint-insert", "resume"],
)
def test_a_command_gdb_refuses_answers_not_started_and_holds_nothing(tmp_path: Path, refused_command: str, response: GdbMiCommandResult, tool: str, arguments: dict) -> None:
    """`^error` means the command did not run, so there is nothing unknown to hold the bench for.

    No breakpoint was created and the core was not resumed: the command did not
    time out, and the transport does not mark it executed. The answers built for
    these refusals carry no `side_effect_committed`, which the quarantine
    decision reads as an unknown effect, so a location typed wrong holds the
    bench. The next audited call then runs the automatic recovery, the recovery
    discards the debug session, and the caller loses the session and every
    breakpoint on it over a command the board never saw.

    The breakpoint set before the refusal is what the caller stands to lose, and
    it is still on the session's record afterwards. What the refusal answers
    about the session's state is deliberately not claimed: #649 leaves open
    whether a refused resume leaves the session `halted` rather than `error`, so
    the claim here is that the session is still the caller's, not what the next
    call may do with it.
    """
    service = debug_service(tmp_path)
    with settled_afterwards(service):
        assert start_debug_session(service)["ok"] is True
        kept = service.call("debug_set_breakpoint", {"location": {"symbol": BREAKPOINT_SYMBOL}})
        assert kept["ok"] is True, kept
        recorded = dict(kept["breakpoint"])

        with gdb_refusing(service, refused_command, response):
            refusal = service.call(tool, arguments)

        assert refusal["ok"] is False, refusal
        assert refusal["error_type"] == "debugger_error", refusal
        assert str(response.error_message) in refusal["summary"], refusal
        assert refusal.get("side_effect_committed") is False, refusal
        assert refusal.get("side_effect_status") == "not_started", refusal
        assert refusal.get("quarantined") is not True, refusal
        assert refusal.get("cleanup_required") is not True, refusal
        assert service.coordinator.blocked is False, refusal
        listed = service.call("debug_list_breakpoints")
        assert listed["breakpoints"] == [recorded], listed

        # The next audited call is where the session used to go: the hold ran the
        # automatic recovery first, and the recovery discards the debug session,
        # so a caller that mistyped one location lost the session and every
        # breakpoint on it. Whatever this call answers (that follows the state a
        # refused command leaves, which the issue leaves open), it is not the
        # call that takes the session away.
        service.call("debug_symbol_info", {"symbol": READABLE_SYMBOL})
        assert service.backend._debug.session is not None, "the next audited call discarded the session"
        listed = service.call("debug_list_breakpoints")
        assert listed["breakpoints"] == [recorded], listed

        # And the session ends as a session, with the teardown proofs a stop
        # reports, rather than as the "no session is active" a discarded one
        # answers with.
        stopped = service.call("debug_stop_session")
        assert stopped["ok"] is True, stopped
        assert stopped.get("halt_not_confirmed") is False, stopped
        assert stopped["safe_state_confirmed"] is True, stopped


def test_a_refused_interrupt_says_the_halt_was_not_acknowledged_and_leaves_the_record_alone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A refused interrupt is a halt that was not acknowledged, and no stop at all (#649, #648).

    The session has to be running for a halt to send an interrupt, and after the
    fix to #495 the only way it is running is a containment that failed for a
    real reason, so the bench is already held under
    `debug_target_state_unconfirmed` when this halt is made. That is the second
    half of #649's expectation: a hold that was already standing stays as it
    was.

    Two things the refusal owes. From #649: the fields the halt's timed-out
    branch carries, so a caller reading `halt_confirmed` finds an answer, and
    the effect named as not started. From #648: the session's stop record left
    as it was, because a refused interrupt neither stopped nor started the core,
    and a record saying the core stopped on a debugger failure while the session
    says it is running describes no target that exists.
    """
    monkeypatch.setattr("agentic_hil.backends.gdbdebug.CONTINUE_COMMAND_TIMEOUT_CAP_S", TIMEOUT_TEST_CAP_S)
    service = debug_service(tmp_path, fake_gdb_behavior=f"{BENCH_RUN_STATE}+{HALT_TIMEOUT}")
    try:
        assert start_debug_session(service, mode="attach")["ok"] is True
        timed_out = service.call("debug_continue", {"timeout_s": UNREACHABLE_STOP_TIMEOUT_S})
        assert timed_out["error_type"] == "timeout", timed_out
        assert timed_out["halt_confirmed"] is False, timed_out
        assert timed_out["session"]["status"] == "running", timed_out
        assert timed_out["cleanup_reasons"] == [TARGET_STATE_UNCONFIRMED], timed_out

        with gdb_refusing(service, INTERRUPT_COMMAND, REFUSED_INTERRUPT):
            refused = service.call("debug_halt", {"timeout_s": UNREACHABLE_STOP_TIMEOUT_S})

        assert refused["ok"] is False, refused
        assert UNRECORDED_INTERRUPT_REFUSAL in refused["summary"], refused
        assert refused.get("halt_command_acknowledged") is False, refused
        assert refused.get("halt_confirmed") is False, refused
        assert refused.get("side_effect_committed") is False, refused
        assert refused.get("side_effect_status") == "not_started", refused
        # The incident is the one the failed containment raised, unchanged.
        assert refused["cleanup_reasons"] == [TARGET_STATE_UNCONFIRMED], refused

        reason = service.call("debug_get_stop_reason")
        assert reason["stop_reason"] == "timeout", reason
        assert reason["session"]["status"] == "running", reason
    finally:
        # The containment this fake never answers is the containment the close
        # has to try again, so the close still refuses to call the target
        # settled. That is unchanged by either issue.
        with pytest.raises(RuntimeError, match=UNCONFIRMED_CLOSE_SENTENCE):
            service.close()
        service.coordinator.close()


# #648: the stop record a refused command must leave alone.


def test_a_refused_breakpoint_insert_leaves_the_recorded_stop_alone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A location GDB does not know says nothing about where the core is.

    The sequence #648 prints. A session is resumed and arrives at its
    breakpoint, then one insert is refused, and from then on the session's stop
    record says the core stopped on a debugger failure: `debug_get_stop_reason`
    reports `debugger_error` with `target_ok: false`, and every later
    `debug_continue` is short-circuited with "Target is already stopped", so the
    session cannot resume and every breakpoint on it is lost with it.

    The same thing #493 settled for symbol resolution, for the insert. The clear
    in the middle is the issue's own step, and it is also what lifts the hold
    the refused insert raises today, so this test says what it says whether or
    not #649 has been answered yet. With no breakpoint left, the resume after it
    is the one that has nothing to stop it, as #495's tests drive it: what
    matters is that it ran at all.
    """
    monkeypatch.setattr("agentic_hil.backends.gdbdebug.CONTINUE_COMMAND_TIMEOUT_CAP_S", TIMEOUT_TEST_CAP_S)
    service = debug_service(tmp_path, fake_gdb_behavior=BENCH_RUN_STATE)
    with settled_afterwards(service):
        assert start_debug_session(service)["ok"] is True
        set_result = service.call("debug_set_breakpoint", {"location": {"symbol": BREAKPOINT_SYMBOL}})
        assert set_result["ok"] is True, set_result
        reached = service.call("debug_continue", {"timeout_s": REACHABLE_STOP_TIMEOUT_S})
        assert reached["stop_reason"] == "breakpoint_hit", reached

        with gdb_refusing(service, BREAKPOINT_INSERT_COMMAND, REFUSED_INSERT):
            missed = service.call("debug_set_breakpoint", {"location": {"file": "missing.c", "line": 10}})
        assert missed["ok"] is False, missed
        assert NO_SOURCE_FILE_MESSAGE in missed["summary"], missed

        reason = service.call("debug_get_stop_reason")
        assert reason["ok"] is True, reason
        assert reason["stop_reason"] == "breakpoint_hit", reason
        assert reason["stop"]["breakpoint_id"] == set_result["breakpoint"]["id"], reason
        assert reason["target_ok"] is True, reason

        cleared = service.call("debug_clear_breakpoints")
        assert cleared["ok"] is True, cleared
        assert cleared["cleared"] == 1, cleared

        resumed = service.call("debug_continue", {"timeout_s": UNREACHABLE_STOP_TIMEOUT_S})

        assert resumed["error_type"] == "timeout", resumed
        assert "already stopped" not in resumed["summary"].lower(), resumed
        assert resumed["halt_confirmed"] is True, resumed
        assert service.call("debug_get_session_status")["status"] == "halted"
        assert service.call("debug_stop_session")["ok"] is True


def test_a_refused_memory_read_leaves_the_recorded_stop_alone(tmp_path: Path) -> None:
    """An address the target will not answer says nothing about where the core is either.

    The read path of the same defect, driven through the fake's own
    `memory_read_refused` behaviour rather than a wrapper: the fake answers
    `-data-read-memory-bytes` with the `^error` a real GDB gives for an address
    the target refuses, which is the one answer here that an existing fixture
    already carries.

    What the refused read must leave behind is the breakpoint stop the target is
    still sitting on. Nothing is claimed here about the hold the refused read
    raises or about the resume after it: #649 leaves open whether a refused read
    inside a session that already holds the core counts as not started or keeps
    the unproven-state reading the one-shot read uses, and the resume's answer
    follows that decision.
    """
    service = debug_service(tmp_path, fake_gdb_behavior=MEMORY_READ_REFUSED)
    try:
        assert start_debug_session(service)["ok"] is True
        set_result = service.call("debug_set_breakpoint", {"location": {"symbol": BREAKPOINT_SYMBOL}})
        assert set_result["ok"] is True, set_result
        reached = service.call("debug_continue", {"timeout_s": REACHABLE_STOP_TIMEOUT_S})
        assert reached["stop_reason"] == "breakpoint_hit", reached

        refused = service.call("debug_symbol_value", {"symbol": READABLE_SYMBOL})
        assert refused["ok"] is False, refused
        assert refused["error_type"] == "memory_read_failed", refused
        assert MEMORY_READ_REFUSAL in refused["summary"], refused

        reason = service.call("debug_get_stop_reason")
        assert reason["ok"] is True, reason
        assert reason["stop_reason"] == "breakpoint_hit", reason
        assert reason["stop"]["breakpoint_id"] == set_result["breakpoint"]["id"], reason
        assert reason["target_ok"] is True, reason

        status = service.call("debug_get_session_status")
        assert status["status"] == "halted", status
        assert status["target_ok"] is True, status
    finally:
        closed_however_the_session_ended(service)
