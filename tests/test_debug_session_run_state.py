"""The session's account of whether the target runs, against the bench's answers.

Three defects one board showed in the same module, `backends/gdbdebug.py`, each
a session that ended in a quarantine over a target that was doing exactly what
it had been told (#492, #493, #495). The bench tier carries the hardware half
of these as strict expected failures; this is the unit half, driven through the
fake GDB with the behaviours the bench recorded opted in, so the same sequences
can be run on every push.
"""
from __future__ import annotations

import contextlib
from collections.abc import Iterator
from pathlib import Path

import pytest
from test_debug_sessions import TIMEOUT_TEST_CAP_S, UNTYPED_SYMBOL_TABLE, debug_service, start_debug_session

from agentic_hil.tools import AgenticHILToolService

# The fake's opt-in behaviours, spelled where the fake spells them.
BENCH_RUN_STATE = "bench_run_state"
MI_ASYNC_UNSUPPORTED = "mi_async_unsupported"
# GDB's words for the setting it does not have, as MI decodes them.
MI_ASYNC_REFUSAL = 'No symbol "mi" in current context.'
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


def gdb_commands(service: AgenticHILToolService) -> list[str]:
    session = service.backend._debug.session
    assert session is not None and session.gdb is not None
    return [str(entry["command"]) for entry in session.gdb.history()]


def symbol_arguments(tool: str, symbol: str) -> dict:
    if tool == "debug_dump_symbol_ihex":
        return {"symbol": symbol, "output_path": "build/lookup.hex"}
    return {"symbol": symbol}


# #495: a resume that runs into its timeout, and the halt that has to follow it.


def test_a_resume_with_nothing_to_stop_it_times_out_and_halts_the_running_target(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The unit twin of the bench test of the same name.

    No breakpoint is live, so the resumed target never stops on its own. The
    call has to report the timeout, contain the target before it answers, and,
    because containment succeeded, not quarantine anything. A GDB in
    synchronous MI does not read the interrupt while the target runs, so all of
    that turns on the session having asked for asynchronous MI first.
    """
    monkeypatch.setattr("agentic_hil.backends.gdbdebug.CONTINUE_COMMAND_TIMEOUT_CAP_S", TIMEOUT_TEST_CAP_S)
    service = debug_service(tmp_path, fake_gdb_behavior=BENCH_RUN_STATE)
    with settled_afterwards(service):
        assert start_debug_session(service, mode="attach")["ok"] is True
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
    halted the same way the first test's is.
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

        assert "-gdb-set mi-async on" in commands, commands
        assert commands.index("-gdb-set mi-async on") < commands.index("-exec-continue"), commands


def test_a_gdb_without_asynchronous_mi_is_refused_when_the_session_starts(tmp_path: Path) -> None:
    """A GDB that cannot do it says so at `debug_start_session`, not at the
    first timeout. Attach mode, so the refusal lands before anything on the
    target was touched: nothing to quarantine, and the caller is told what the
    debugger lacks in the debugger's own words as well as the setting's name."""
    service = debug_service(tmp_path, fake_gdb_behavior=MI_ASYNC_UNSUPPORTED)
    with settled_afterwards(service):
        started = start_debug_session(service, mode="attach")

        assert started["ok"] is False, started
        assert started["error_type"] == "gdb_async_unsupported", started
        assert "mi-async" in started["summary"], started
        assert MI_ASYNC_REFUSAL in started["summary"] + str(started.get("backend_error", "")), started
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
