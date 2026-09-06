"""The session's account of whether the target runs, against the bench's answers.

Three defects one board showed in the same module, `backends/gdbdebug.py`, each
a session that ended in a quarantine over a target that was doing exactly what
it had been told (#492, #493, #495). The bench tier carries the hardware half
of these, in tests/bench/test_bench_breakpoints.py and test_bench_symbols.py;
this is the unit half, driven through the fake GDB with the behaviours the
bench recorded opted in, so the same sequences can be run on every push. The
fake's model of asynchronous MI comes from the GDB manual and the issue's
reasoning, not from a recording, because the bench had never run with the
setting on: what the board does with it is the bench tier's to prove.
"""
from __future__ import annotations

import contextlib
from collections.abc import Iterator
from pathlib import Path

import pytest
from test_debug_sessions import (
    TIMEOUT_TEST_CAP_S,
    UNTYPED_SYMBOL_TABLE,
    debug_service,
    record_mi_commands,
    start_debug_session,
)

from agentic_hil.tools import AgenticHILToolService

# The fake's opt-in behaviours, spelled where the fake spells them.
BENCH_RUN_STATE = "bench_run_state"
MI_ASYNC_UNSUPPORTED = "mi_async_unsupported"
HALT_TIMEOUT = "halt_timeout"
INTERRUPT_LOST_ONCE = "interrupt_lost_once"
BEHAVIOR_ENVIRONMENT_VARIABLE = "FAKE_GDB_BEHAVIOR"
# The text the fake answers for the setting it does not have, as MI decodes it.
# Not GDB's recorded words (the fake says so where it defines them); what is
# asserted is that whatever the debugger answered reaches the caller.
MI_ASYNC_REFUSAL = 'No symbol "mi" in current context.'
INTERRUPT_COMMAND = "-exec-interrupt --all"
MI_ASYNC_COMMAND = "-gdb-set mi-async on"
# A resume that has a breakpoint to arrive at, and one that has nothing to stop
# it. The fake stops a resumed target twenty milliseconds after a breakpoint is
# live, so the first is generous and the second only needs to be past that.
REACHABLE_STOP_TIMEOUT_S = 5.0
UNREACHABLE_STOP_TIMEOUT_S = 0.5
BREAKPOINT_SYMBOL = "test_done"
ABSENT_SYMBOL = "missing_symbol"
SYMBOL_TOOLS = ("debug_symbol_info", "debug_symbol_value", "debug_dump_symbol_ihex")


@contextlib.contextmanager
def settled_afterwards(service: AgenticHILToolService) -> Iterator[None]:
    """Close the service, and make the clean close part of the claim.

    Every sequence here is one the product must leave a session it can end with
    the target confirmed halted, so on the way out `close()` has to succeed. On
    the way out of a failed assertion the close is allowed to refuse (the defect
    under test is exactly what leaves it unable to reconfirm the halt), and that
    refusal must not replace the assertion that found the defect.
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


def seeded_service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, behavior: str) -> AgenticHILToolService:
    """A service whose fake GDB knows its behaviour before its first command.

    The artifact trailer reaches the fake only when `-file-exec-and-symbols`
    is executed, the third command of a session, so a claim about a startup
    command sent before or after it would pass or fail on where the product
    placed the command rather than on what it did. The GDB child inherits the
    test process environment, and the fake reads the same string from it at
    start; the trailer is written as well, for the commands after it.
    """
    monkeypatch.setenv(BEHAVIOR_ENVIRONMENT_VARIABLE, behavior)
    return debug_service(tmp_path, fake_gdb_behavior=behavior)


def gdb_commands(service: AgenticHILToolService) -> list[str]:
    session = service.backend._debug.session
    assert session is not None and session.gdb is not None
    return [str(entry["command"]) for entry in session.gdb.history()]


def symbol_arguments(tool: str, symbol: str) -> dict:
    if tool == "debug_dump_symbol_ihex":
        return {"symbol": symbol, "output_path": "build/lookup.hex"}
    return {"symbol": symbol}


# #495: a resume that runs into its timeout, and the halt that has to follow it.


@pytest.mark.parametrize("mode", ["attach", "reset_halt"])
def test_a_resume_with_nothing_to_stop_it_times_out_and_halts_the_running_target(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    """The unit twin of the bench test of the same name.

    No breakpoint is live, so the resumed target never stops on its own. The
    call has to report the timeout, contain the target before it answers, and,
    because containment succeeded, not quarantine anything. A GDB in
    synchronous MI does not read the interrupt while the target runs, so all of
    that turns on the session having asked for asynchronous MI first. In both
    modes the issue was observed in: the bench test attaches, the issue's own
    session was halted after `reset_halt`.
    """
    monkeypatch.setattr("agentic_hil.backends.gdbdebug.CONTINUE_COMMAND_TIMEOUT_CAP_S", TIMEOUT_TEST_CAP_S)
    service = debug_service(tmp_path, fake_gdb_behavior=BENCH_RUN_STATE)
    with settled_afterwards(service):
        assert start_debug_session(service, mode=mode)["ok"] is True
        assert service.call("debug_list_breakpoints")["breakpoints"] == []

        timed_out = service.call("debug_continue", {"timeout_s": UNREACHABLE_STOP_TIMEOUT_S})

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
        # The stop that was recorded is the halt that was confirmed, not the
        # deadline that ran out: the result carries both facts.
        assert timed_out["target_stop_reason"] != "timeout", timed_out
        assert service.coordinator.blocked is False

        status = service.call("debug_get_session_status")
        assert status["ok"] is True, status
        assert status["active"] is True, status
        assert status["status"] == "halted", status
        assert status.get("quarantined") is not True, status
        assert status.get("cleanup_required") is not True, status

        stopped = service.call("debug_stop_session")
        assert stopped["ok"] is True, stopped
        assert stopped["status"] == "stopped", stopped


def test_clearing_a_breakpoint_the_target_is_sitting_on_leaves_the_next_resume_able_to_run_and_be_halted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The unit twin of the second bench test under #495.

    Clear at a breakpoint, read the stop reason, resume again. Removing the
    breakpoint does not move the core, so the stop reason is still the hit; and
    with nothing left to stop it, the second resume runs out and has to be
    halted the same way the first test's is. The clear half already holds on
    the fake (`clear_breakpoints` restores the stop reason it found), so it is
    pinned here as the neighbour that must not move; the timeout half is what
    is red until the session asks for asynchronous MI.
    """
    monkeypatch.setattr("agentic_hil.backends.gdbdebug.CONTINUE_COMMAND_TIMEOUT_CAP_S", TIMEOUT_TEST_CAP_S)
    service = debug_service(tmp_path, fake_gdb_behavior=BENCH_RUN_STATE)
    with settled_afterwards(service):
        assert start_debug_session(service)["ok"] is True
        set_result = service.call("debug_set_breakpoint", {"location": {"symbol": BREAKPOINT_SYMBOL}})
        assert set_result["ok"] is True, set_result
        stopped = service.call("debug_continue", {"timeout_s": REACHABLE_STOP_TIMEOUT_S})
        assert stopped["stop_reason"] == "breakpoint_hit", stopped

        cleared = service.call("debug_clear_breakpoints")
        assert cleared["ok"] is True, cleared
        assert cleared["cleared"] == 1, cleared

        reason = service.call("debug_get_stop_reason")
        assert reason["ok"] is True, reason
        assert reason["stop_reason"] == "breakpoint_hit", reason
        assert reason["stop"]["breakpoint_id"] == set_result["breakpoint"]["id"], reason

        resumed = service.call("debug_continue", {"timeout_s": UNREACHABLE_STOP_TIMEOUT_S})

        assert resumed["ok"] is False, resumed
        assert resumed["error_type"] == "timeout", resumed
        assert "already stopped" not in resumed["summary"].lower(), resumed
        assert resumed["halt_command_acknowledged"] is True, resumed
        assert resumed["halt_confirmed"] is True, resumed
        assert resumed.get("cleanup_required") is not True, resumed
        assert service.call("debug_get_session_status")["status"] == "halted"
        assert service.call("debug_stop_session")["ok"] is True


def test_the_session_asks_gdb_for_asynchronous_mi_before_the_target_first_runs(tmp_path: Path) -> None:
    """The mechanism, stated once: `-gdb-set mi-async on` is on the wire before
    the first `-exec-continue`, so an interrupt sent while the target runs is
    read at all. The two tests above prove the consequence; this pins the cause
    so a regression is named by the command that went missing."""
    service = debug_service(tmp_path, fake_gdb_behavior=BENCH_RUN_STATE)
    with settled_afterwards(service):
        assert start_debug_session(service)["ok"] is True
        assert service.call("debug_set_breakpoint", {"location": {"symbol": BREAKPOINT_SYMBOL}})["ok"] is True
        assert service.call("debug_continue", {"timeout_s": REACHABLE_STOP_TIMEOUT_S})["ok"] is True

        commands = gdb_commands(service)

        assert MI_ASYNC_COMMAND in commands, commands
        assert commands.index(MI_ASYNC_COMMAND) < commands.index("-exec-continue"), commands


def test_a_gdb_without_asynchronous_mi_is_refused_when_the_session_starts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A GDB that cannot do it says so at `debug_start_session`, not at the
    first timeout. Attach mode, so the refusal lands before anything on the
    target was touched: nothing to quarantine, and the caller is told the
    setting's name and what the debugger answered, whatever that was.

    `not_started` and `retry_safe` pin where the setting is asked for: before
    `-target-select`, which is the last point a refusal costs nothing. Whether
    it comes before or after the file is loaded is not pinned, which is why the
    fake is seeded through the environment rather than the artifact alone.
    """
    service = seeded_service(tmp_path, monkeypatch, MI_ASYNC_UNSUPPORTED)
    recorded = record_mi_commands(monkeypatch)
    with settled_afterwards(service):
        started = start_debug_session(service, mode="attach")

        assert started["ok"] is False, started
        assert started["error_type"] == "gdb_async_unsupported", started
        assert "mi-async" in started["summary"], started
        assert MI_ASYNC_REFUSAL in started["summary"] + str(started.get("backend_error", "")), started
        assert not any(command.startswith("-target-select") for command in recorded), recorded
        assert started["side_effect_status"] == "not_started", started
        assert started["retry_safe"] is True, started
        assert started.get("cleanup_required") is not True, started
        assert service.coordinator.blocked is False
        assert service.call("debug_get_session_status")["active"] is False


def test_a_resume_that_reaches_a_breakpoint_still_reports_it_with_asynchronous_mi(tmp_path: Path) -> None:
    """The neighbour that must not move: with the bench's run state and
    asynchronous MI, a resume that arrives at a breakpoint is still the ordinary
    stop it always was, attributed to the breakpoint the caller set, and the
    session ends confirmed halted."""
    service = debug_service(tmp_path, fake_gdb_behavior=BENCH_RUN_STATE)
    with settled_afterwards(service):
        assert start_debug_session(service)["ok"] is True
        set_result = service.call("debug_set_breakpoint", {"location": {"symbol": BREAKPOINT_SYMBOL}})
        assert set_result["ok"] is True, set_result

        stopped = service.call("debug_continue", {"timeout_s": REACHABLE_STOP_TIMEOUT_S})

        assert stopped["ok"] is True, stopped
        assert stopped["stop_reason"] == "breakpoint_hit", stopped
        assert stopped["stop"]["breakpoint_id"] == set_result["breakpoint"]["id"], stopped
        assert stopped["session"]["status"] == "halted", stopped
        assert service.call("debug_stop_session")["ok"] is True


# #492: halting a target that is already stopped.


def test_halting_a_target_already_stopped_at_a_breakpoint_answers_that_it_was_already_stopped(tmp_path: Path) -> None:
    """The unit twin of the bench test of the same name.

    The breakpoint stop was consumed by the `debug_continue` that waited for
    it, so a poll finds nothing new; the session's own record still says the
    target is halted at that breakpoint, and that is the answer owed. The
    timeout passed to the halt bounds only the wait for a stop that never comes,
    which is the defect; the answer the tool owes needs no wait at all.
    """
    service = debug_service(tmp_path, fake_gdb_behavior=BENCH_RUN_STATE)
    with settled_afterwards(service):
        assert start_debug_session(service)["ok"] is True
        set_result = service.call("debug_set_breakpoint", {"location": {"symbol": BREAKPOINT_SYMBOL}})
        assert set_result["ok"] is True, set_result
        stopped = service.call("debug_continue", {"timeout_s": REACHABLE_STOP_TIMEOUT_S})
        assert stopped["stop_reason"] == "breakpoint_hit", stopped

        halted = service.call("debug_halt", {"timeout_s": 1.0})

        assert halted["ok"] is True, halted
        assert halted["stop_reason"] == "breakpoint_hit", halted
        assert halted["target_ok"] is True, halted
        assert halted["stop"]["breakpoint_id"] == set_result["breakpoint"]["id"], halted
        assert halted["summary"].startswith("Target was already stopped"), halted
        assert halted.get("quarantined") is not True, halted
        assert halted.get("cleanup_required") is not True, halted
        assert halted["session"]["status"] == "halted", halted
        assert service.coordinator.blocked is False

        # Still the session it was: the next resume runs and arrives again.
        resumed = service.call("debug_continue", {"timeout_s": REACHABLE_STOP_TIMEOUT_S})
        assert resumed["ok"] is True, resumed
        assert resumed["stop_reason"] == "breakpoint_hit", resumed
        assert service.call("debug_stop_session")["ok"] is True


def test_halting_a_target_sitting_in_a_fault_answers_the_fault_it_is_in(tmp_path: Path) -> None:
    """The same early return, for a stop the caller needs to hear about.

    A target that stopped in a fault handler is already stopped, and the halt
    answers with that fault rather than overwriting it with a fresh halt or
    waiting for a stop that never comes. The answer is the stop's own verdict:
    not ok, named as the target exception it is, and still no quarantine, since
    the target is exactly where the fault left it.
    """
    service = debug_service(tmp_path, fake_gdb_behavior=f"{BENCH_RUN_STATE}+hardfault")
    with settled_afterwards(service):
        assert start_debug_session(service)["ok"] is True
        assert service.call("debug_set_breakpoint", {"location": {"symbol": BREAKPOINT_SYMBOL}})["ok"] is True
        faulted = service.call("debug_continue", {"timeout_s": REACHABLE_STOP_TIMEOUT_S})
        assert faulted["stop_reason"] == "exception", faulted

        halted = service.call("debug_halt", {"timeout_s": 1.0})

        assert halted["ok"] is False, halted
        assert halted["error_type"] == "target_exception", halted
        assert halted["stop_reason"] == "exception", halted
        assert halted["stop"]["exception_type"] == "hardfault", halted
        assert halted["summary"].startswith("Target was already stopped"), halted
        assert halted.get("quarantined") is not True, halted
        assert halted.get("cleanup_required") is not True, halted
        assert halted["session"]["status"] == "halted", halted
        assert service.coordinator.blocked is False

        # The issue's printed sequence ends here, and so must this one: a
        # session whose target sits in a fault is still a session that ends
        # with the halt confirmed and nothing retained.
        stopped = service.call("debug_stop_session")
        assert stopped["ok"] is True, stopped
        assert stopped["status"] == "stopped", stopped
        assert stopped["safe_state_confirmed"] is True, stopped


@pytest.mark.parametrize("mode", ["attach", "reset_halt", "load"])
def test_halting_straight_after_the_session_started_answers_that_it_was_already_stopped(tmp_path: Path, mode: str) -> None:
    """The halt the issue leads with: a caller that halts defensively before
    reading memory, straight after the session started.

    Every mode ends `debug_start_session` with the target halted and nothing
    observed as a stop: the session's own record says halted, no stop reason is
    recorded yet, and nothing has run since. On the bench that interrupt is
    acknowledged and no stop follows, so today this times out and quarantines,
    which is the issue's own description. The answer is owed from the session's
    state: already stopped, and the reply says why, that the session started
    with the target halted and nothing has resumed it since. It is recorded as
    the `halted` stop it is, so the read the tool's own refusal points a caller
    at (`debug_get_stop_reason`, "Run debug_continue or debug_halt first") has
    something to say once a halt has answered; and no interrupt goes on the
    wire at all, because a target that never ran has nothing to interrupt.
    """
    service = debug_service(tmp_path, fake_gdb_behavior=BENCH_RUN_STATE)
    with settled_afterwards(service):
        assert start_debug_session(service, mode=mode)["ok"] is True
        assert service.call("debug_get_stop_reason")["error_type"] == "stop_reason_not_available"

        halted = service.call("debug_halt", {"timeout_s": 1.0})

        assert halted["ok"] is True, halted
        assert halted["stop_reason"] == "halted", halted
        assert halted["target_ok"] is True, halted
        assert halted["stop"]["backend_stop_reason"] == "session_start", halted
        assert halted["summary"].startswith("Target was already stopped"), halted
        assert "session started" in halted["summary"], halted
        assert halted.get("quarantined") is not True, halted
        assert halted.get("cleanup_required") is not True, halted
        assert halted["session"]["status"] == "halted", halted
        assert service.coordinator.blocked is False
        assert INTERRUPT_COMMAND not in gdb_commands(service), gdb_commands(service)

        reason = service.call("debug_get_stop_reason")
        assert reason["ok"] is True, reason
        assert reason["stop_reason"] == "halted", reason

        # Still the session it was: a breakpoint set now is reached by the
        # next resume, which is the resume the halt was supposed to precede.
        assert service.call("debug_set_breakpoint", {"location": {"symbol": BREAKPOINT_SYMBOL}})["ok"] is True
        resumed = service.call("debug_continue", {"timeout_s": REACHABLE_STOP_TIMEOUT_S})
        assert resumed["ok"] is True, resumed
        assert resumed["stop_reason"] == "breakpoint_hit", resumed
        assert service.call("debug_stop_session")["ok"] is True


def test_halting_twice_answers_the_halt_that_was_recorded(tmp_path: Path) -> None:
    """The second halt answers the first. A halt that answered from the
    session's state recorded `halted`; the halt after it finds that record,
    answers it the same way, and still sends nothing."""
    service = debug_service(tmp_path, fake_gdb_behavior=BENCH_RUN_STATE)
    with settled_afterwards(service):
        assert start_debug_session(service, mode="attach")["ok"] is True
        first = service.call("debug_halt", {"timeout_s": 1.0})
        assert first["ok"] is True, first
        assert first["stop_reason"] == "halted", first

        second = service.call("debug_halt", {"timeout_s": 1.0})

        assert second["ok"] is True, second
        assert second["stop_reason"] == "halted", second
        assert second["summary"].startswith("Target was already stopped"), second
        assert second["session"]["status"] == "halted", second
        assert INTERRUPT_COMMAND not in gdb_commands(service), gdb_commands(service)
        assert service.call("debug_stop_session")["ok"] is True


def test_halting_after_a_timeout_contained_the_target_answers_the_interrupt_that_stopped_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A halt that was confirmed by an interrupt is a recorded stop like any
    other: the timeout path's containment recorded the SIGINT stop as `halted`,
    and the explicit halt after it answers that stop rather than interrupting a
    target that is no longer running. One interrupt on the wire, not two."""
    monkeypatch.setattr("agentic_hil.backends.gdbdebug.CONTINUE_COMMAND_TIMEOUT_CAP_S", TIMEOUT_TEST_CAP_S)
    service = debug_service(tmp_path, fake_gdb_behavior=BENCH_RUN_STATE)
    with settled_afterwards(service):
        assert start_debug_session(service, mode="attach")["ok"] is True
        timed_out = service.call("debug_continue", {"timeout_s": UNREACHABLE_STOP_TIMEOUT_S})
        assert timed_out["halt_confirmed"] is True, timed_out

        halted = service.call("debug_halt", {"timeout_s": 1.0})

        assert halted["ok"] is True, halted
        assert halted["stop_reason"] == "halted", halted
        assert halted["stop"]["backend_stop_reason"] == "signal-received", halted
        assert halted["stop"]["signal"]["name"] == "SIGINT", halted
        assert halted["summary"].startswith("Target was already stopped"), halted
        assert halted["session"]["status"] == "halted", halted
        assert gdb_commands(service).count(INTERRUPT_COMMAND) == 1, gdb_commands(service)
        assert service.call("debug_stop_session")["ok"] is True


# #495, the running target: a halt that has to interrupt, and one that cannot.
#
# After the fix no tool call leaves the target running on purpose, so the only
# way a session meets one is a containment that failed for a real reason. The
# two tests here used to live in test_debug_sessions.py against the default
# fake, which answered an interrupt sent to a target that had never run with a
# stop; the bench does not (#492), and under the session-state rule that
# interrupt is never sent. Both are re-stated here on a target that is running.


def test_halting_a_running_target_stops_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`debug_halt` on a running target stops it, the issue's third sentence.

    The first interrupt is lost outright, never answered, so the resume's
    containment fails and the quarantine it earns is the honest one: the
    target really is still running and the session records it so. The halt
    that follows is the case the sentence describes, and with asynchronous MI
    it is possible: the interrupt is read while the target runs, the SIGINT
    stop is recorded as `halted`, and the session is halted again. What stands
    afterwards is the incident the failed containment raised, which the halt
    does not claim to settle; what this pins is that the halt reached the
    target and told the truth about it.
    """
    monkeypatch.setattr("agentic_hil.backends.gdbdebug.CONTINUE_COMMAND_TIMEOUT_CAP_S", TIMEOUT_TEST_CAP_S)
    service = debug_service(tmp_path, fake_gdb_behavior=f"{BENCH_RUN_STATE}+{INTERRUPT_LOST_ONCE}")
    try:
        assert start_debug_session(service, mode="attach")["ok"] is True
        timed_out = service.call("debug_continue", {"timeout_s": UNREACHABLE_STOP_TIMEOUT_S})
        assert timed_out["error_type"] == "timeout", timed_out
        assert timed_out["halt_command_acknowledged"] is False, timed_out
        assert timed_out["halt_confirmed"] is False, timed_out
        assert timed_out["target_state"] == "unknown", timed_out
        assert timed_out["session"]["status"] == "running", timed_out
        assert service.coordinator.blocked is True

        halted = service.call("debug_halt", {"timeout_s": 1.0})

        assert halted["ok"] is True, halted
        assert halted["stop_reason"] == "halted", halted
        assert halted["stop"]["backend_stop_reason"] == "signal-received", halted
        assert halted["stop"]["signal"]["name"] == "SIGINT", halted
        assert halted["summary"].startswith("Target halted"), halted
        assert halted["session"]["status"] == "halted", halted
        assert gdb_commands(service).count(INTERRUPT_COMMAND) == 2, gdb_commands(service)
        assert service.call("debug_get_session_status")["status"] == "halted"
    finally:
        # The target is confirmed halted, so the session can end with that
        # proof; the incident from the lost interrupt is the lease's to keep.
        try:
            service.close()
        except RuntimeError:
            service.coordinator.close()
            raise


def test_an_interrupt_that_is_acknowledged_but_never_stops_the_target_still_quarantines(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Containment that fails for a real reason is still a quarantine.

    Asynchronous MI makes the interrupt readable; it does not make a probe that
    lost the core answer for it. The interrupt is acknowledged and no stop ever
    follows: the resume's timeout path reports the halt unconfirmed and
    quarantines, the explicit halt after it (on a session that records the
    target running, so it does send its own interrupt) meets the same silence
    and reports the same, and the close refuses to call any of that a clean
    stop. The same claims the default-fake test made, on a target that is
    running when the interrupts are sent.
    """
    monkeypatch.setattr("agentic_hil.backends.gdbdebug.CONTINUE_COMMAND_TIMEOUT_CAP_S", TIMEOUT_TEST_CAP_S)
    service = debug_service(tmp_path, fake_gdb_behavior=f"{BENCH_RUN_STATE}+{HALT_TIMEOUT}")
    try:
        assert start_debug_session(service, mode="attach")["ok"] is True
        timed_out = service.call("debug_continue", {"timeout_s": UNREACHABLE_STOP_TIMEOUT_S})
        assert timed_out["error_type"] == "timeout", timed_out
        assert timed_out["halt_command_acknowledged"] is True, timed_out
        assert timed_out["halt_confirmed"] is False, timed_out
        assert timed_out["target_state"] == "unknown", timed_out
        assert timed_out["side_effect_status"] == "unknown", timed_out
        assert timed_out["session"]["status"] == "running", timed_out
        assert service.coordinator.blocked is True

        halted = service.call("debug_halt", {"timeout_s": 0.5})

        assert halted["ok"] is False, halted
        assert halted["error_type"] == "timeout", halted
        assert halted["halt_command_acknowledged"] is True, halted
        assert halted["halt_confirmed"] is False, halted
        assert halted["target_state"] == "unknown", halted
        assert halted["side_effect_status"] == "unknown", halted
        assert halted["cleanup_required"] is True, halted
        assert gdb_commands(service).count(INTERRUPT_COMMAND) == 2, gdb_commands(service)
        assert service.coordinator.blocked is True
        cleared = service.call("debug_clear_breakpoints")
        assert cleared["cleanup_required"] is True, cleared
        assert service.coordinator.blocked is True
    finally:
        # `close()` re-attempts the halt itself, meets the same silence, and
        # refuses to call that a clean stop before the lease is ever released.
        with pytest.raises(RuntimeError, match="reconfirming the target was halted"):
            service.close()
        service.coordinator.close()


# #493: a failed symbol lookup, and the stop reason it must leave alone.


@pytest.mark.parametrize("tool", SYMBOL_TOOLS)
def test_a_failed_lookup_leaves_the_session_able_to_resume(tmp_path: Path, tool: str) -> None:
    """The unit twin of the bench test of the same name, for each of the three
    tools that resolve a symbol.

    The first resume is the control: it proves this session and this breakpoint
    can do what the second resume is refused today. Between them is a lookup
    that found nothing, and that is the only thing between them. The stop
    reason describes the target, and a name the debugger could not resolve says
    nothing about the target.
    """
    service = debug_service(tmp_path)
    with settled_afterwards(service):
        assert start_debug_session(service)["ok"] is True
        set_result = service.call("debug_set_breakpoint", {"location": {"symbol": BREAKPOINT_SYMBOL}})
        assert set_result["ok"] is True, set_result
        reached = service.call("debug_continue", {"timeout_s": REACHABLE_STOP_TIMEOUT_S})
        assert reached["ok"] is True, reached
        assert reached["stop_reason"] == "breakpoint_hit", reached

        missed = service.call(tool, symbol_arguments(tool, ABSENT_SYMBOL))
        assert missed["ok"] is False, missed
        assert missed["error_type"] == "symbol_not_found", missed

        reason = service.call("debug_get_stop_reason")
        assert reason["ok"] is True, reason
        assert reason["stop_reason"] == "breakpoint_hit", reason
        assert reason["stop"]["breakpoint_id"] == set_result["breakpoint"]["id"], reason
        assert reason["session"]["status"] == "halted", reason

        resumed = service.call("debug_continue", {"timeout_s": REACHABLE_STOP_TIMEOUT_S})

        assert resumed["ok"] is True, resumed
        assert resumed["stop_reason"] == "breakpoint_hit", resumed
        assert "already stopped" not in resumed["summary"].lower(), resumed
        assert service.call("debug_stop_session")["ok"] is True


def test_a_failed_lookup_the_symbol_table_also_refuses_leaves_the_stop_reason_alone(tmp_path: Path) -> None:
    """The second route, refused too. With a real ELF behind the session the
    lookup goes on to the symbol table after GDB declines, and a symbol neither
    route carries is refused with both answers on the result, as before (#187).
    That refusal is a refusal like the other: the recorded stop stays."""
    service = debug_service(tmp_path, elf_symbols=UNTYPED_SYMBOL_TABLE)
    with settled_afterwards(service):
        assert start_debug_session(service)["ok"] is True
        set_result = service.call("debug_set_breakpoint", {"location": {"symbol": BREAKPOINT_SYMBOL}})
        assert set_result["ok"] is True, set_result
        assert service.call("debug_continue", {"timeout_s": REACHABLE_STOP_TIMEOUT_S})["stop_reason"] == "breakpoint_hit"

        missed = service.call("debug_symbol_info", {"symbol": ABSENT_SYMBOL})
        assert missed["error_type"] == "symbol_not_found", missed
        assert missed["symbol_table_lookup"] == "not_found", missed

        reason = service.call("debug_get_stop_reason")
        assert reason["stop_reason"] == "breakpoint_hit", reason
        assert reason["stop"]["breakpoint_id"] == set_result["breakpoint"]["id"], reason
        resumed = service.call("debug_continue", {"timeout_s": REACHABLE_STOP_TIMEOUT_S})
        assert resumed["ok"] is True, resumed
        assert resumed["stop_reason"] == "breakpoint_hit", resumed


def test_a_failed_lookup_on_a_session_with_no_recorded_stop_records_none(tmp_path: Path) -> None:
    """Nothing recorded before, nothing recorded after. A session straight out
    of an attach has no stop reason yet, and a failed lookup must not invent
    one: the read still says none is available, and the resume runs."""
    service = debug_service(tmp_path)
    with settled_afterwards(service):
        assert start_debug_session(service, mode="attach")["ok"] is True
        assert service.call("debug_set_breakpoint", {"location": {"symbol": BREAKPOINT_SYMBOL}})["ok"] is True
        assert service.call("debug_get_stop_reason")["error_type"] == "stop_reason_not_available"

        missed = service.call("debug_symbol_info", {"symbol": ABSENT_SYMBOL})
        assert missed["error_type"] == "symbol_not_found", missed

        reason = service.call("debug_get_stop_reason")
        assert reason["ok"] is False, reason
        assert reason["error_type"] == "stop_reason_not_available", reason
        resumed = service.call("debug_continue", {"timeout_s": REACHABLE_STOP_TIMEOUT_S})
        assert resumed["ok"] is True, resumed
        assert resumed["stop_reason"] == "breakpoint_hit", resumed


def test_a_failed_lookup_while_the_target_sits_in_a_fault_keeps_the_fault(tmp_path: Path) -> None:
    """The stop the lookup must leave alone can be one the caller needs to
    hear about. A target stopped in a fault handler stays `exception` through
    a refused lookup, so the next resume is still refused, and refused for the
    true reason rather than for a name that could not be resolved."""
    service = debug_service(tmp_path, fake_gdb_behavior="hardfault")
    with settled_afterwards(service):
        assert start_debug_session(service)["ok"] is True
        assert service.call("debug_set_breakpoint", {"location": {"symbol": BREAKPOINT_SYMBOL}})["ok"] is True
        faulted = service.call("debug_continue", {"timeout_s": REACHABLE_STOP_TIMEOUT_S})
        assert faulted["stop_reason"] == "exception", faulted

        missed = service.call("debug_symbol_info", {"symbol": ABSENT_SYMBOL})
        assert missed["error_type"] == "symbol_not_found", missed

        reason = service.call("debug_get_stop_reason")
        assert reason["stop_reason"] == "exception", reason
        assert reason["stop"]["exception_type"] == "hardfault", reason
        refused = service.call("debug_continue", {"timeout_s": REACHABLE_STOP_TIMEOUT_S})
        assert refused["ok"] is False, refused
        assert refused["error_type"] == "target_exception", refused
        assert refused["stop_reason"] == "exception", refused
        assert "already stopped" in refused["summary"].lower(), refused


def test_a_failed_lookup_followed_by_a_halt_answers_the_breakpoint(tmp_path: Path) -> None:
    """#493 met #492 in this order on the bench: a lookup that found nothing,
    then the defensive halt before the memory read. The halt answers the
    breakpoint the target is still sitting on, because the lookup left that
    record alone and the halt decides from it."""
    service = debug_service(tmp_path, fake_gdb_behavior=BENCH_RUN_STATE)
    with settled_afterwards(service):
        assert start_debug_session(service)["ok"] is True
        set_result = service.call("debug_set_breakpoint", {"location": {"symbol": BREAKPOINT_SYMBOL}})
        assert set_result["ok"] is True, set_result
        assert service.call("debug_continue", {"timeout_s": REACHABLE_STOP_TIMEOUT_S})["stop_reason"] == "breakpoint_hit"
        assert service.call("debug_symbol_info", {"symbol": ABSENT_SYMBOL})["error_type"] == "symbol_not_found"

        halted = service.call("debug_halt", {"timeout_s": 1.0})

        assert halted["ok"] is True, halted
        assert halted["stop_reason"] == "breakpoint_hit", halted
        assert halted["stop"]["breakpoint_id"] == set_result["breakpoint"]["id"], halted
        assert halted["summary"].startswith("Target was already stopped"), halted
        assert INTERRUPT_COMMAND not in gdb_commands(service), gdb_commands(service)
        assert service.call("debug_stop_session")["ok"] is True
