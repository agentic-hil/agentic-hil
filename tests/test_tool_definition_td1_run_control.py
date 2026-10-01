"""What a host reads about running, halting and breaking inside a debug session (#638).

Seven tools work inside the GDB session `debug_start_session` opens:
`debug_halt` and `debug_continue` move the core, `debug_set_breakpoint` and
`debug_clear_breakpoints` change where it will stop, and
`debug_get_session_status`, `debug_list_breakpoints` and
`debug_get_stop_reason` read what the session holds. A caller reads their
definitions, as a host serves them through `tools/list`, to tell the seven
apart, to know what each needs first (a session, a grant), what it does to the
board, what a repeated call answers, what bounds its time, and which result
fields and failure outcomes it acts on.

The first half reads the served definitions and checks their meaning, not their
wording. A fact that could be stated the wrong way round (halted or resumed,
kept or ended, needed or not, `true` or `false`) is checked as a relation inside
one sentence, clause or piece: a verb counts only where no negation stands
before it in its own fragment, and a boolean only next to the field it belongs
to. Every check is run against its own inverted statement as well, which it
must refuse. Which backends run sessions, and that the server binds a debugger
only when exactly one is configured, is kept to one phrase in each description,
so a change to that support edits one phrase.

The second half holds the behaviour those definitions describe, through the
fake debugger, where no existing test already holds it. The rest is held
elsewhere and not repeated: a breakpoint hit and its frame, and a clear that
empties the list (tests/test_debug_sessions.py, the full cycle), an unexpected
breakpoint and a target exception and the resume refused after one (the same
file), a symbol outside `debug.allowed_symbols` and a file breakpoint without
`debug.allow_all_symbols` (the same file), an insert whose acknowledgement is
lost and the clear that settles it (the same file), a continue that times out
and halts the target, a halt on a target that is already stopped, a halt that
is never confirmed, a clear that keeps the stop reason, and the stop reason
before anything stopped (tests/test_debug_session_run_state.py).
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest
from conftest import DEFAULT_TEST_PERMISSIONS, write_config
from fixtures.fake_gdb import EXPECTED_BREAKPOINT_STOP
from test_debug_session_run_state import (
    BENCH_RUN_STATE,
    INTERRUPT_COMMAND,
    INTERRUPT_LOST_ONCE,
    REACHABLE_STOP_TIMEOUT_S,
    UNREACHABLE_STOP_TIMEOUT_S,
    gdb_commands,
    settled_afterwards,
)
from test_debug_sessions import TIMEOUT_TEST_CAP_S, debug_service, start_debug_session, stlink_dump_service
from test_tool_definition_debug_sessions import (
    BACKEND_NAMES,
    CEILING,
    CONFIGURED_DEBUGGER_TIMEOUT_DEFAULT,
    ENDS,
    FLOOR,
    NO_SESSION,
    SECONDS,
    TIMEOUT_FLOOR,
    WHOLE_CALL,
    clauses,
    containing,
    definition_text,
    description_sentences,
    listed_tools,
    pieces,
    property_texts,
    pyocd_service,
    recording,
    source_literals,
    unknown_identifiers,
)
from test_tool_definition_debug_symbol_info import NEGATED_NEED, allows_either_policy_key
from test_tool_descriptions import DESCRIPTION_LIMIT, PROPERTY_DESCRIPTION_LIMIT

from agentic_hil.backends import gdbdebug
from agentic_hil.backends.gdbdebug import (
    CONTINUE_COMMAND_TIMEOUT_CAP_S,
    GDB_COMMAND_TIMEOUT_CAP_S,
    normalize_breakpoint_location,
)
from agentic_hil.config import load_config
from agentic_hil.contracts import validate_tool_arguments
from agentic_hil.gdbmi import GdbMiCommandResult
from agentic_hil.tools import AgenticHILToolService

START = "debug_start_session"
STOP = "debug_stop_session"
HALT = "debug_halt"
CONTINUE = "debug_continue"
STATUS = "debug_get_session_status"
STOP_REASON = "debug_get_stop_reason"
SET = "debug_set_breakpoint"
LIST = "debug_list_breakpoints"
CLEAR = "debug_clear_breakpoints"
TOOLS = (HALT, CONTINUE, STATUS, STOP_REASON, SET, LIST, CLEAR)
# The two reads that answer `ok` with `active: false` when nothing is open
# (gdbdebug.py `get_session_status`, `list_breakpoints`); the other five go
# through `_require_session` and answer `session_not_active`.
ANSWER_WITHOUT_SESSION = (STATUS, LIST)
REFUSE_WITHOUT_SESSION = (HALT, CONTINUE, STOP_REASON, SET, CLEAR)
READS = (STATUS, LIST, STOP_REASON)
# A symbol the fake debugger knows and stops at.
BREAKPOINT_SYMBOL = "test_done"
CALL_ARGUMENTS: dict[str, dict] = {SET: {"location": {"symbol": BREAKPOINT_SYMBOL}}}

NEGATION = re.compile(r"\b(no|not|never|neither|nor|without|unchanged)\b", re.IGNORECASE)
DENIED_BEFORE = r"\b(not|never|no|instead of|rather than)\s+(an?\s+)?"
OK_NOT_FALSE = r"\bok\b(?!\W{0,3}false\b)"
# Where a fragment ends: a full stop that closes a sentence, a semicolon, a
# comma or a colon. A full stop inside a number ("0.1") or a key
# ("debug.allowed_symbols") ends nothing.
FRAGMENT_END = re.compile(r"[;,:]|\.(?=\s|$)")
# A debugger entry's `timeout_s` may be any positive number (config.py
# `positive_timeout_config`), so one below the 0.1 floor is a valid entry.
ENTRY_BELOW_THE_FLOOR_S = 0.05


def produced_stop_reasons() -> set[str]:
    """Every `stop_reason` value the session code assigns, read from its source
    (gdbdebug.py `_stop_reason_from_gdb`, `continue_execution`, `halt`,
    `_gdb_command`): a definition that lists one the code never produces
    sends a caller looking for an answer that does not come."""
    source = Path(gdbdebug.__file__).read_text(encoding="utf-8")
    return set(re.findall(r"(?:\"stop_reason\"\s*:\s*|\bstop_reason\s*=\s*)\"([a-z_]+)\"", source))


PRODUCED_STOP_REASONS = produced_stop_reasons()


# ---------------------------------------------------------------------------
# The meaning checks, each a predicate over the text a host shows.


def first_sentence(text: str) -> str:
    sentences = description_sentences(text)
    return sentences[0] if sentences else ""


def asserted(pattern: str, text: str, flags: int = re.IGNORECASE) -> bool:
    """Whether `pattern` is stated rather than denied: some match of it has no
    negation between the start of its own fragment and the match itself.
    "Never halts the core" names the verb and does not state it."""
    for found in re.finditer(pattern, text, flags):
        fragment = FRAGMENT_END.split(text[: found.start()])[-1]
        if not NEGATION.search(fragment):
            return True
    return False


def backend_support_is_one_phrase(description: str) -> bool:
    """The rule the lifecycle tools follow: one sentence names a backend,
    OpenOCD as the one that runs sessions, the others as the ones that answer
    `not_supported`. Kept to one sentence on purpose (the brief's "one short
    phrase per description", and the lifecycle tools' own test), so a change
    to the support edits one place; within it any wording that keeps the two
    sides apart passes."""
    naming = [sentence for sentence in description_sentences(description) if any(backend in sentence for backend in BACKEND_NAMES)]
    if len(naming) != 1:
        return False
    parts = re.split(r"[;,:]", naming[0])
    supported = [part for part in parts if "OpenOCD" in part]
    refusing = [part for part in parts if "not_supported" in part]
    return (
        bool(supported)
        and all(re.search(r"\b(only|required|needs?|requires?|runs?|supports?|exists?|under)\b", part) for part in supported)
        and not any("not_supported" in part for part in supported)
        and bool(refusing)
        and all(re.search(r"\b(others?|other backends?|pyOCD|STM32CubeProgrammer|elsewhere|any other|else|otherwise)\b", part) for part in refusing)
    )


ONE_DEBUGGER = r"\bexactly one\b(?=[^.;]{0,30}\bdebuggers?\b)(?=[^.;]{0,30}\bconfigured\b)|\bconfigured\b[^.;]{0,20}\bexactly one\b[^.;]{0,20}\bdebuggers?\b"
MANY_DEBUGGERS = r"\b(at least one|one or more|any number of|several|two or more)\b[^.;]{0,20}\bdebuggers?\b"


def names_the_one_bound_debugger(description: str) -> bool:
    """The server binds a debugger only when exactly one is configured
    (config.py `load_config`, `single`); with none or several, every one of
    these calls answers `not_supported` before any session is looked at
    (tools.py `unbound_debugger_error`)."""
    found = containing(description_sentences(description), ONE_DEBUGGER, re.IGNORECASE)
    return bool(found) and all("not_supported" in sentence for sentence in found) and not re.search(MANY_DEBUGGERS, description, re.IGNORECASE)


def refuses_without_a_session(description: str) -> bool:
    """`session_not_active` is named as what a call meets with nothing open,
    never denied and never paired with `ok`."""
    found = containing(clauses(description), r"\bsession_not_active\b")
    return bool(found) and all(
        (NO_SESSION.search(clause) or START in clause)
        and not re.search(DENIED_BEFORE + r"session_not_active\b", clause, re.IGNORECASE)
        and not re.search(OK_NOT_FALSE, clause)
        for clause in found
    )


def answers_ok_and_inactive_without_a_session(description: str) -> bool:
    """With nothing open the read answers `ok` with `active: false`; if it
    names `session_not_active` at all, it says that is not the answer."""
    no_session = [sentence for sentence in description_sentences(description) if NO_SESSION.search(sentence)]
    if not any(re.search(r"\bok\b", sentence) and re.search(r"\bactive\W{0,3}false\b", sentence) for sentence in no_session):
        return False
    if any(re.search(r"\bok\W{0,3}false\b|\bactive\W{0,3}true\b", sentence) for sentence in no_session):
        return False
    return all(re.search(DENIED_BEFORE + "$", description[: found.start()], re.IGNORECASE) for found in re.finditer(r"\bsession_not_active\b", description))


# A claim that the read leaves everything as it was. Status and stop reason do
# not: each takes a stop GDB delivered since the last call into the session's
# record and rewrites the session log (gdbdebug.py `_refresh_session_stop`).
CHANGES_NOTHING = re.compile(r"\b(changes nothing|read-only|reads only|no side effects?|without changing|nothing (is )?changed|changes no state|no effect)\b", re.IGNORECASE)
MOVE_VERB = r"\b(halts?|resumes?|stops?|runs?|continues?|interrupts?)\b"
MOVES_THE_CORE = MOVE_VERB + r"[^.;,]{0,25}\b(core|target|board)\b"


def never_moves_the_core(description: str) -> bool:
    """None of the three reads sends GDB a command (gdbdebug.py
    `get_session_status`, `get_stop_reason`, `list_breakpoints`), so none
    halts or resumes the core; the definition says both, and says neither
    as something the read does."""
    moving = containing(pieces(description), MOVES_THE_CORE, re.IGNORECASE)
    return (
        any(re.search(r"\bhalt", piece, re.IGNORECASE) and re.search(r"\bresum", piece, re.IGNORECASE) for piece in moving)
        and not any(asserted(MOVE_VERB, piece) for piece in moving)
    )


def records_a_newly_arrived_stop(description: str) -> bool:
    """Status and stop reason poll GDB for a stop it delivered since the last
    call and record it in the session (gdbdebug.py `_refresh_session_stop`):
    a read that does that is not described as changing nothing."""
    return not CHANGES_NOTHING.search(description) and any(
        asserted(r"\b(records?|picks? up|takes? in|stores?|consumes?)\b[^.;]{0,30}\bstops?\b", clause) for clause in clauses(description)
    )


def lists_the_session_record_without_asking_gdb(description: str) -> bool:
    """`list_breakpoints` answers from the session's own list and sends GDB
    nothing; only `debug_clear_breakpoints` reads GDB's list (gdbdebug.py
    `list_breakpoints`, `_backend_breakpoint_numbers`). Every mention of GDB is
    the one the list does without, or the one the clear checks."""
    record = [clause for clause in clauses(description) if re.search(r"\b(own (record|list)|session's (record|list)|local|cached?)\b", clause, re.IGNORECASE)]
    without_gdb = any(re.search(r"\b(without|not|never)\s+(asking|querying|reading|consulting)\s+GDB\b", clause) for clause in record)
    gdb_pieces = containing(pieces(description), r"\bGDB\b")
    return without_gdb and all(re.search(r"\b(without|not|never)\s+(asking|querying|reading|consulting)\s+GDB\b", piece) or CLEAR in piece for piece in gdb_pieces)


def an_abnormal_stop_is_a_good_read(description: str) -> bool:
    """A read whose session holds an abnormal stop answers `ok` true with
    `target_ok` false and `target_error_type` (gdbdebug.py `target_stop_fields`,
    `get_session_status`, `get_stop_reason`): `ok` is the read, `target_ok` the
    core."""
    found = containing(description_sentences(description), r"\btarget_ok\b")
    return bool(found) and all(
        re.search(r"\btarget_ok\W{0,3}false\b", sentence)
        and re.search(r"\btarget_error_type\b", sentence)
        and re.search(r"\bok\W{0,3}(true|stays true)\b", sentence)
        and not re.search(r"\bok\W{0,3}false\b|\btarget_ok\W{0,3}true\b", sentence)
        and not re.search(r"\b(read|call|request)\s+(failed|fails)\b", sentence, re.IGNORECASE)
        for sentence in found
    )


def names_only_produced_stop_reasons(text: str) -> bool:
    """Every value listed after `stop_reason` in parentheses is one the code
    produces (`fault` is checked for but never assigned)."""
    listed = [word for inside in re.findall(r"\bstop_reason\s*\(([^)]*)\)", text) for word in re.findall(r"[a-z_]+", inside)]
    return set(listed) <= PRODUCED_STOP_REASONS


def lists_stop_reason_values(text: str) -> bool:
    return bool(re.search(r"\bstop_reason\s*\(([^)]*)\)", text)) and names_only_produced_stop_reasons(text)


# debug_halt


def halt_says_it_stops_the_core(description: str) -> bool:
    first = first_sentence(description)
    return bool(asserted(r"\b(halts?|stops?)\b", first) and re.search(r"\b(core|target|CPU)\b", first)) and not re.search(r"\b(resumes?|continues?|runs?)\b", first, re.IGNORECASE)


KEPT_OPEN = r"\b(stays?|remains?|kept|left|keeps?(\s+(it|the\s+session))?)\s+open\b"
SESSION_ENDED = r"\b(ends?|closes?|terminates?|tears? down)\b[^.;,]{0,25}\bsession\b|\bsession\b[^.;,]{0,25}\b(ends|is (ended|closed)|closes|terminates)\b"


def halt_keeps_the_session_open(description: str) -> bool:
    """The session stays open after a halt; `debug_stop_session` is what ends
    it (gdbdebug.py `halt` leaves `session.status` halted)."""
    units = clauses(description)
    kept = [clause for clause in units if re.search(r"\bsession\b", clause) and re.search(KEPT_OPEN, clause, re.IGNORECASE) and not re.search(r"\b(not|never|no longer)\b", clause)]
    ending = containing(units, rf"\b{STOP}\b")
    claims_end = [clause for clause in units if STOP not in clause and re.search(SESSION_ENDED, clause, re.IGNORECASE)]
    return bool(kept) and bool(ending) and all(asserted(ENDS.pattern, clause) for clause in ending) and not claims_end


def needs_no_execution_grant_if_named(text: str) -> bool:
    """`allow_debug_execution` gates only `debug_continue` (gdbdebug.py
    `continue_execution`); a halt that names it says it is not needed."""
    return all(re.search(NEGATED_NEED, clause, re.IGNORECASE) or re.search(r"\bwithout\b", clause, re.IGNORECASE) for clause in containing(clauses(text), r"\ballow_debug_execution\b"))


def a_stopped_core_gets_no_interrupt(text: str) -> bool:
    """A recorded stop, or a session that has not run since it started, is
    answered as it stands; no `-exec-interrupt` is sent (gdbdebug.py `halt`)."""
    return any(
        re.search(r"\b(already|stopped|halted)\b", clause, re.IGNORECASE)
        and re.search(r"\b(no|not|never|without)\b[^.;]{0,20}\binterrupt|\bnothing (is )?sent\b|\binterrupt\w*\b[^.;]{0,20}\b(skipped|not sent)\b", clause, re.IGNORECASE)
        for clause in clauses(text)
    )


def halt_names_its_stop(text: str) -> bool:
    return bool(re.search(r"\bstop_reason\b", text) and re.search(r"\bhalted\b", text))


def an_unconfirmed_halt_is_held(text: str) -> bool:
    """A halt whose stop never comes answers `ok` false, `halt_confirmed`
    false and `target_state` unknown, and the bench is quarantined (gdbdebug.py
    `halt`; tools.py `_result_requires_quarantine`)."""
    found = containing(description_sentences(text), r"\bhalt_confirmed\b")
    held = [
        sentence
        for sentence in found
        if re.search(r"\bhalt_confirmed\W{0,3}false\b", sentence)
        and re.search(r"\bunknown\b", sentence)
        and re.search(r"quarantin|\bcleanup_required\b", sentence, re.IGNORECASE)
        and re.search(r"\bok\W{0,3}false\b", sentence)
    ]
    inverted = [sentence for sentence in found if re.search(r"\bhalt_confirmed\W{0,3}true\b|\btarget_state\W{0,3}halted\b|\bok\W{0,3}true\b", sentence)]
    return bool(held) and not inverted


SHORT_RAISED = re.compile(
    rf"\b(under|below|less than)\s+{re.escape(TIMEOUT_FLOOR)}\b[^.;]{{0,20}}?\b(counts as|is raised to|becomes|is taken as|is treated as|rounds up to)\s+{re.escape(TIMEOUT_FLOOR)}\b", re.IGNORECASE
)


def raises_a_short_timeout_before_the_ceiling(text: str) -> bool:
    """`max(0.1, timeout_s)` is taken before the ceiling (gdbdebug.py `halt`,
    `continue_execution`): 0 is a valid argument (the schema's minimum) and
    counts as 0.1, while a debugger entry set below 0.1 still wins, so an
    unconditional "0.1 at least" is false for such an entry."""
    floored = containing(clauses(text), re.escape(TIMEOUT_FLOOR))
    return (
        any(SHORT_RAISED.search(clause) and re.search(r"\b(before|first)\b", clause, re.IGNORECASE) and not re.search(r"\bafter\b", clause, re.IGNORECASE) for clause in floored)
        and not any(FLOOR.search(clause) for clause in floored)
    )


def halt_timeout_bounds_each_step_at_the_cap(text: str) -> bool:
    """`min(debugger.timeout_s, 10)` when omitted, `min(that, max(0.1,
    timeout_s))` when given, spent on the interrupt and again on the wait for
    its stop (gdbdebug.py `halt`); running out answers `timeout`."""
    units = clauses(text)
    cap = rf"\b{int(GDB_COMMAND_TIMEOUT_CAP_S)}\b"
    capped = containing(units, cap)
    return bool(
        SECONDS.search(text)
        and capped
        and all(re.search(r"\b(default|ceiling)\b", clause, re.IGNORECASE) or CEILING.search(clause) for clause in capped)
        and any(re.search(r"\bdebugger\b", clause) and re.search(r"\b(lower|less|smaller|shorter|whichever is lower|minimum of)\b", clause) for clause in capped)
        and not any(re.search(r"\b(higher|greater|longer|larger)\b", clause) for clause in capped)
        and not re.search(rf"\b{CONFIGURED_DEBUGGER_TIMEOUT_DEFAULT}\b", text)
        and raises_a_short_timeout_before_the_ceiling(text)
        and not WHOLE_CALL.search(text)
        and not re.search(r"\b(together|combined|in total)\b", text, re.IGNORECASE)
        and re.search(r"\binterrupt", text, re.IGNORECASE)
        and re.search(r"\bstop\b", text)
        and re.search(r"\b(each|both|per|every)\b", text, re.IGNORECASE)
        and re.search(r"\btimeout\b", text)
    )


# debug_continue


def continue_says_it_resumes_and_waits(description: str) -> bool:
    first = first_sentence(description)
    return bool(
        asserted(r"\b(resumes?|runs?|continues?)\b", first)
        and re.search(r"\b(waits?|until)\b[^.]*\bstop", first, re.IGNORECASE)
        and not re.search(r"\b(not|never|without)\b[^.]{0,15}\bwait", first, re.IGNORECASE)
    )


def continue_needs_the_execution_grant(description: str) -> bool:
    """gdbdebug.py `continue_execution`: refused with `permission_denied`
    unless `allow_debug_execution` is granted."""
    found = containing(clauses(description), r"\ballow_debug_execution\b")
    sentences = containing(description_sentences(description), r"\ballow_debug_execution\b")
    return (
        bool(found)
        and all(re.search(r"\b(needs?|requires?)\b", clause, re.IGNORECASE) and not re.search(NEGATED_NEED, clause, re.IGNORECASE) for clause in found)
        and all("permission_denied" in sentence for sentence in sentences)
    )


def continue_leaves_an_exception_stop_alone(description: str) -> bool:
    """A core stopped in an exception (or a debugger error) is answered as it
    stands and not resumed (gdbdebug.py `continue_execution`, the "already
    stopped" path)."""
    resume = r"\b(resum\w*|continu\w*|runs?|restart\w*)\b"
    found = [clause for clause in clauses(description) if re.search(r"\b(exception|fault)s?\b", clause, re.IGNORECASE) and re.search(resume, clause, re.IGNORECASE)]
    return bool(found) and not any(asserted(resume, clause) for clause in found)


BAD_STOP = r"\b(false|not|error|errors|fails?|failure|abnormal)\b"


def continue_tells_good_stops_from_bad(text: str) -> bool:
    """`breakpoint_hit` is a good stop; `unexpected_breakpoint` and
    `target_exception` answer `ok` false (gdbdebug.py `_stopped_result`,
    `ABNORMAL_STOP_REASONS`, `stop_error_type`)."""
    parts = [part for part in re.split(r"[,;:]|(?<=\.)\s+", text) if part.strip()]
    bad = [part for part in parts if re.search(r"\b(unexpected_breakpoint|target_exception)\b", part)]
    hit = [part for part in parts if re.search(r"\bbreakpoint_hit\b", part)]
    return bool(
        "unexpected_breakpoint" in text
        and "target_exception" in text
        and bad
        and all(re.search(BAD_STOP, part, re.IGNORECASE) for part in bad)
        and hit
        and all(not re.search(r"\b(unexpected_breakpoint|target_exception)\b", part) and not re.search(BAD_STOP, part, re.IGNORECASE) for part in hit)
        and re.search(r"\bstop_reason\b", text)
        and re.search(r"\bframe\b", text)
    )


def continue_says_what_expiry_does(text: str) -> bool:
    """On expiry the core is interrupted and the answer says whether that halt
    was confirmed (gdbdebug.py `continue_execution`, the timeout path)."""
    found = [
        sentence
        for sentence in description_sentences(text)
        if re.search(r"\bhalt_confirmed\b", sentence) and re.search(r"\btimeout\b", sentence) and asserted(r"\binterrupt\w*|\bhalt(s|ed|ing)\b", sentence)
    ]
    return bool(found) and not any(re.search(r"\b(left|keeps?|stays?|remains?) running\b", sentence, re.IGNORECASE) for sentence in found)


def continue_expiry_steps_are_capped(text: str) -> bool:
    """On expiry the interrupt and the wait for its stop each get
    `min(5, debugger.timeout_s)` (gdbdebug.py `continue_execution`), on top of
    the wait `timeout_s` set: up to 5 seconds a step, not 5 in all."""
    return any(
        re.search(r"\binterrupt", clause, re.IGNORECASE)
        and re.search(r"\bstop\b", clause)
        and re.search(rf"\b{int(CONTINUE_COMMAND_TIMEOUT_CAP_S)}\s?(s|seconds)\b", clause)
        and re.search(r"\b(each|per step|apiece|both)\b", clause, re.IGNORECASE)
        and re.search(r"\b(up to|at most|no more than|max)\b", clause, re.IGNORECASE)
        and not re.search(r"\b(together|combined|in total|in all)\b", clause, re.IGNORECASE)
        for clause in clauses(text)
    )


def continue_timeout_is_the_wait_for_a_stop(text: str) -> bool:
    """The debugger entry's `timeout_s` when omitted, `min(that, max(0.1,
    timeout_s))` when given, spent on the wait for a stop (gdbdebug.py
    `continue_execution`); no 10 or 5 second cap applies to it. The resume's
    acknowledgement gets `min(that, 5)`, never more than the wait itself."""
    units = clauses(text)
    default = containing(units, r"\bdefault\b", re.IGNORECASE)
    ceiling = [clause for clause in units if CEILING.search(clause)]
    other_caps = rf"\b({int(GDB_COMMAND_TIMEOUT_CAP_S)}|{int(CONTINUE_COMMAND_TIMEOUT_CAP_S)})\b"
    return bool(
        SECONDS.search(text)
        and default
        and all(re.search(r"\bdebugger\b", clause) and re.search(rf"\b{CONFIGURED_DEBUGGER_TIMEOUT_DEFAULT}\b", clause) for clause in default)
        and ceiling
        and all(re.search(r"\bdebugger\b", clause) for clause in ceiling)
        and not any(re.search(other_caps, clause) for clause in default + ceiling)
        and raises_a_short_timeout_before_the_ceiling(text)
        and re.search(r"\bstop\b", text)
        and not WHOLE_CALL.search(text)
    )


# debug_set_breakpoint and its location


def set_says_it_adds_a_breakpoint(description: str) -> bool:
    return asserted(r"\b(adds?|sets?|inserts?|places?)\b[^.;]*\bbreakpoint\b", first_sentence(description))


def set_returns_the_breakpoint_id(description: str) -> bool:
    return asserted(r"\bbreakpoint\b[^.;]{0,30}\bid\b", description, 0)


def a_repeated_set_adds_another(description: str) -> bool:
    """Every successful insert is a new entry with the next id, also for a
    location already set (gdbdebug.py `set_breakpoint`, `next_breakpoint_id`)."""
    repeat = [clause for clause in clauses(description) if re.search(r"\b(each|again|repeat\w*|already set|twice|second)\b", clause, re.IGNORECASE)]
    return any(asserted(r"\b(adds?|another|new|two|separate)\b", clause) for clause in repeat) and not any(
        re.search(r"\b(replaces?|ignored?|ignores|no-op|idempotent|reuses?|same one|refused|fails?)\b", clause, re.IGNORECASE) for clause in repeat
    )


def set_leaves_the_core_where_it_is(description: str) -> bool:
    moving = [piece for piece in pieces(description) if re.search(r"\b(resum\w*|runs?|running|continu\w*|starts?)\b", piece, re.IGNORECASE)]
    return any(re.search(r"\bresum", piece, re.IGNORECASE) and NEGATION.search(piece) for piece in moving) and all(NEGATION.search(piece) or CONTINUE in piece for piece in moving)


def set_names_an_unconfirmed_insert(description: str) -> bool:
    """An insert whose acknowledgement is lost, or whose id cannot be read, is
    kept as a provisional entry and the session needs cleanup (gdbdebug.py
    `set_breakpoint`, `effect_unconfirmed`)."""
    found = containing(clauses(description), r"\b(cleanup_required|provisional_breakpoint)\b")
    return bool(found) and all(re.search(r"\b(unconfirmed|not confirmed|lost|unacknowledged)\b", clause, re.IGNORECASE) for clause in found)


# What each refusal of a set is about, as gdbdebug.py `set_breakpoint` decides
# it: the grant (`_validate_symbol`, `debug.allow_all_symbols`), the location
# (`normalize_breakpoint_location`), and GDB's own answer (`_gdb_failure`).
SET_REFUSALS = {
    "permission_denied": r"\b(grant\w*|allowed_symbols|allow_all_symbols|permission|allowed)\b",
    "invalid_argument": r"\b(location|argument|form|shape)\b",
    "debugger_error": r"\bGDB\b",
}


def set_names_its_refusals(description: str) -> bool:
    """Each refusal is named next to what it is about, and none of them is
    denied or paired with `ok` true."""
    for error_type, about in SET_REFUSALS.items():
        found = containing(pieces(description), rf"\b{error_type}\b")
        if not found or not all(re.search(about, piece) for piece in found):
            return False
        if re.search(DENIED_BEFORE + error_type, description, re.IGNORECASE) or any(re.search(r"\bok\W{0,3}true\b", piece) for piece in found):
            return False
    return True


def location_names_the_grant_for_each_form(text: str) -> bool:
    """A symbol or function needs `debug.allowed_symbols` or
    `debug.allow_all_symbols` (gdbdebug.py `_validate_symbol`); a file and line
    needs `debug.allow_all_symbols` alone (gdbdebug.py `set_breakpoint`)."""
    if not allows_either_policy_key(text):
        return False
    either = [clause for clause in clauses(text) if "allowed_symbols" in clause and "allow_all_symbols" in clause]
    if any(re.search(r"\bfile\b", clause) for clause in either):
        return False
    files = [clause for clause in clauses(text) if re.search(r"\bfile\b", clause) and "allow_all_symbols" in clause and not re.search(r"\ballowed_symbols\b", clause)]
    return any(re.search(r"\b(needs?|requires?|only with)\b", clause, re.IGNORECASE) and not re.search(NEGATED_NEED, clause, re.IGNORECASE) for clause in files)


def location_states_line_and_path_rules(text: str) -> bool:
    """`line` is an integer from 1 (contracts.py `BREAKPOINT_LOCATION`), and
    the file holds no `..` anywhere: the schema refuses only a `..` path
    segment, the session refuses `..` inside a name as well (gdbdebug.py
    `normalize_breakpoint_location`), so a rule worded as a segment or a parent
    directory promises a file name the call refuses."""
    line_ok = re.search(r"\bline\b[^.;]{0,30}(\bfrom 1\b|\b1-based\b|\bone-based\b|\bpositive\b|\bat least 1\b|>= ?1\b|\bstarting at 1\b)", text, re.IGNORECASE)
    line_inverted = re.search(r"\bline\b[^.;]{0,30}(\bfrom 0\b|\b0-based\b|\bzero-based\b)", text, re.IGNORECASE)
    dots_refused = re.search(r"\b(no|not|never|without)\W{0,3}\.\.|\.\.['\"`]?\s+(is\s+)?(refused|rejected|not allowed)", text, re.IGNORECASE)
    dots_allowed = re.search(r"\.\.['\"`]?\s+(is\s+)?(allowed|accepted|fine)", text, re.IGNORECASE)
    dots_narrowed = re.search(r"\.\.['\"`)]?\s*(path\s+)?(segments?|components?)\b|\b(parent|segments?|components?)\b[^.;,]{0,20}\.\.", text, re.IGNORECASE)
    return bool(line_ok) and not line_inverted and bool(dots_refused) and not dots_allowed and not dots_narrowed


def location_names_identifiers_for_the_symbol_forms(text: str) -> bool:
    """A string, `symbol` or `function` must be an identifier, optionally
    `::`-qualified (contracts.py `BREAKPOINT_LOCATION`, gdbdebug.py
    `DEBUG_SYMBOL_PATTERN`); the file form takes a path, not an identifier."""
    named = containing(clauses(text), r"\bidentifiers?\b", re.IGNORECASE)
    return bool(named) and all(re.search(r"\b(symbol|function)\b", clause) and not re.search(r"\"file\"|\bfile\b", clause) for clause in named)


def location_examples(text: str) -> tuple[list[str], list[str]]:
    """The JSON objects the text offers, and the double-quoted strings outside them."""
    objects = re.findall(r"\{[^{}]*\}", text)
    strings = re.findall(r"\"([^\"]+)\"", re.sub(r"\{[^{}]*\}", " ", text))
    return objects, strings


def gives_valid_location_examples(text: str) -> bool:
    """One example of each of the four forms the schema takes (contracts.py
    `BREAKPOINT_LOCATION`): a bare string, a `symbol` object, a `function`
    object and a `file` and `line` object; every example passes the schema and
    the backend's own normalization."""
    objects, strings = location_examples(text)
    parsed: list[object] = []
    for offered in objects:
        try:
            parsed.append(json.loads(offered))
        except ValueError:
            return False
    keys = [set(value) for value in parsed if isinstance(value, dict)]
    if not strings or not all(form in keys for form in ({"symbol"}, {"function"}, {"file", "line"})):
        return False
    return all(validate_tool_arguments(SET, {"location": value}) is None and normalize_breakpoint_location(SET, value)["ok"] for value in [*parsed, *strings])


# debug_list_breakpoints


def list_says_it_lists_breakpoints(description: str) -> bool:
    return asserted(r"\blists?\b[^.]*\bbreakpoints\b", first_sentence(description))


def list_names_its_field_and_siblings(description: str) -> bool:
    return bool(re.search(r"\bbreakpoints\b[^.;]{0,30}\b(id|backend_id|location)\b", description) and re.search(rf"\b({SET}|{CLEAR})\b", description))


# debug_clear_breakpoints


def clear_says_it_removes_every_breakpoint(description: str) -> bool:
    first = first_sentence(description)
    return bool(asserted(r"\b(deletes?|removes?|clears?)\b", first) and re.search(r"\b(all|every)\b", first, re.IGNORECASE) and re.search(r"\bbreakpoints?\b", first))


def clear_names_its_reconciliation(description: str) -> bool:
    """GDB's own list is read, every number deleted, and the list read again
    (gdbdebug.py `clear_breakpoints`, `_backend_breakpoint_numbers`): success
    answers `backend_reconciled` true, a list that cannot be read or is not
    empty afterwards answers `ok` false with
    `breakpoint_reconciliation_failed`."""
    failed = containing(clauses(description), r"\bbreakpoint_reconciliation_failed\b")
    return bool(
        re.search(r"\bGDB\b", description)
        and re.search(r"\bcleared\b", description)
        and re.search(r"\bbackend_reconciled\W{0,3}true\b", description)
        and not re.search(r"\bbackend_reconciled\W{0,3}false\b", description)
        and failed
        and all(re.search(r"\bok\W{0,3}false\b|\bcleanup_required\b", clause) and not re.search(r"\bok\W{0,3}true\b", clause) for clause in failed)
    )


def clear_leaves_the_core_alone(description: str) -> bool:
    moving = [clause for clause in clauses(description) if re.search(r"\b(resumes?|halts?|runs?|stops? the core)\b", clause, re.IGNORECASE)]
    return bool(moving) and all(re.search(r"\b(neither|nor|not|never|no|unchanged|stays?)\b", clause, re.IGNORECASE) for clause in moving)


def a_repeated_clear_clears_nothing(description: str) -> bool:
    repeat = [clause for clause in clauses(description) if re.search(r"\b(again|repeat\w*|second|twice)\b", clause, re.IGNORECASE)]
    return any(re.search(r"\b(0|zero|none|nothing)\b", clause, re.IGNORECASE) for clause in repeat) and not any(
        re.search(r"\b(fails?|refused|error|breakpoint_reconciliation_failed|session_not_active)\b", clause) for clause in repeat
    )


# debug_get_session_status and debug_get_stop_reason


def status_names_its_fields_and_sibling(description: str) -> bool:
    return all(re.search(pattern, description) for pattern in (r"\bactive\b", r"\bstatus\b", r"\bsession\b", r"\bcleanup_required\b", rf"\b{STOP_REASON}\b"))


def stop_reason_names_its_fields_and_sibling(text: str) -> bool:
    return all(re.search(pattern, text) for pattern in (r"\bstop_reason\b", r"\bstop\b", r"\bframe\b", r"\bbreakpoint_hit\b", rf"\b{STATUS}\b"))


def nothing_stopped_yet_is_its_own_outcome(text: str) -> bool:
    """A session with no recorded stop answers `stop_reason_not_available`
    (gdbdebug.py `get_stop_reason`)."""
    found = containing(clauses(text), r"\bstop_reason_not_available\b")
    return bool(found) and all(
        re.search(r"\b(before|yet|none|no stop|debug_continue|debug_halt)\b", clause, re.IGNORECASE) and not re.search(OK_NOT_FALSE, clause) for clause in found
    )


# ---------------------------------------------------------------------------
# Each check against a statement it must accept and the inversion it must refuse.

SELF_TESTS: list[tuple[Callable[[str], bool], list[str], list[str]]] = [
    (
        backend_support_is_one_phrase,
        [
            "Halts the core. OpenOCD backend only; others answer not_supported.",
            "Halts the core. Runs on OpenOCD only, pyOCD and STM32CubeProgrammer answer not_supported.",
            "Halts the core. Sessions exist only under OpenOCD; other backends answer not_supported.",
            "Halts the core. OpenOCD only, exactly one debugger configured; else not_supported.",
        ],
        ["Halts the core. OpenOCD answers not_supported.", "Halts the core. OpenOCD only. pyOCD answers not_supported.", "Halts the core.", "Halts the core. OpenOCD only; OpenOCD answers not_supported."],
    ),
    (
        names_the_one_bound_debugger,
        ["OpenOCD only, exactly one debugger configured; else not_supported.", "Needs exactly one configured debugger, else not_supported."],
        [
            "OpenOCD only, at least one debugger configured; else not_supported.",
            "OpenOCD only, exactly one debugger configured.",
            "OpenOCD only; else not_supported.",
            "Exactly one debugger configured; else not_supported. Works with several debuggers too.",
        ],
    ),
    (
        refuses_without_a_session,
        ["No session: session_not_active.", "Answers session_not_active until debug_start_session opened one."],
        ["No session: ok, not session_not_active.", "No session: ok, session_not_active.", "Answers session_not_active after a halt."],
    ),
    (
        answers_ok_and_inactive_without_a_session,
        ["No session: ok, active false, not session_not_active.", "With no session it answers ok and active false."],
        ["No session: session_not_active.", "No session: ok false, active false.", "No session: ok, active true.", "No session: ok, active false; session_not_active."],
    ),
    (
        never_moves_the_core,
        ["Records a newly arrived stop; never halts or resumes the core.", "Neither halts nor resumes the core."],
        ["Halts the core and returns the status.", "Never halts the core, then resumes the core.", "Reads the session.", "Never halts the core."],
    ),
    (
        records_a_newly_arrived_stop,
        ["Records a newly arrived stop; never halts or resumes the core.", "Picks up a stop GDB reported since the last call."],
        ["Reads the session; changes nothing.", "Read-only: records a newly arrived stop.", "Never records a stop.", "Never halts or resumes the core."],
    ),
    (
        lists_the_session_record_without_asking_gdb,
        [
            "Lists them from the session's own record without asking GDB. debug_clear_breakpoints removes them and checks GDB.",
            "Answers from the local list, not querying GDB.",
        ],
        ["Lists the breakpoints GDB reports.", "Lists the session's own record, then asks GDB.", "Lists the breakpoints from the session's own record."],
    ),
    (
        an_abnormal_stop_is_a_good_read,
        ["Abnormal stop: ok true, target_ok false, target_error_type.", "An abnormal stop answers ok true with target_ok false and target_error_type."],
        [
            "Abnormal stop: ok false, target_ok false, target_error_type.",
            "Abnormal stop: ok true, target_ok true, target_error_type.",
            "Abnormal stop: target_ok false.",
            "Abnormal stop: ok true, target_ok false, target_error_type: the read failed.",
            "Returns the stop.",
        ],
    ),
    (
        lists_stop_reason_values,
        ["Returns stop_reason (breakpoint_hit, halted, exception, timeout)."],
        ["Returns stop_reason (breakpoint_hit, fault).", "Returns stop_reason."],
    ),
    (
        halt_says_it_stops_the_core,
        ["Stops the core inside the session.", "Halts the target and waits for its stop."],
        ["Resumes the core.", "Halts and then resumes the core.", "Reads the session.", "Never halts the core.", "Does not stop the target."],
    ),
    (
        halt_keeps_the_session_open,
        ["Stops the core; the session stays open; debug_stop_session ends it.", "Halts the core and keeps the session open; debug_stop_session closes it."],
        [
            "Stops the core and ends the session.",
            "Stops the core; the session stays open; debug_stop_session keeps it.",
            "Stops the core; the session does not stay open; debug_stop_session ends it.",
            "Stops the core; the session stays open; debug_stop_session never ends it.",
        ],
    ),
    (
        needs_no_execution_grant_if_named,
        ["Needs no allow_debug_execution.", "Works without allow_debug_execution.", "Stops the core."],
        ["Needs allow_debug_execution.", "Requires allow_debug_execution granted."],
    ),
    (
        a_stopped_core_gets_no_interrupt,
        ["A stopped core gets no interrupt: its recorded stop is answered.", "Already halted: nothing is sent."],
        ["A stopped core is interrupted again.", "A halted core gets another interrupt."],
    ),
    (
        an_unconfirmed_halt_is_held,
        ["Unconfirmed: ok false, halt_confirmed false, target_state unknown, quarantined.", "If no stop follows: ok false, halt_confirmed false, state unknown, cleanup_required."],
        ["Unconfirmed: ok true, halt_confirmed false, target_state unknown, quarantined.", "Unconfirmed: ok false, halt_confirmed true, target_state halted.", "Unconfirmed: ok false, halt_confirmed false, target_state unknown."],
    ),
    (
        raises_a_short_timeout_before_the_ceiling,
        ["Default and ceiling: 10; under 0.1 counts as 0.1 before the ceiling.", "Ceiling: the entry. Below 0.1 is raised to 0.1 first."],
        [
            "Default and ceiling: 10; 0.1 at least.",
            "Default and ceiling: 10; under 0.1 counts as 0.1 after the ceiling.",
            "Default and ceiling: 10; under 0.1 counts as 0.1 first, so never below 0.1.",
            "Default and ceiling: 10; over 0.1 counts as 0.1 first.",
        ],
    ),
    (
        halt_timeout_bounds_each_step_at_the_cap,
        ["Seconds for the interrupt and for its stop, each. Default and ceiling: 10, or the debugger entry's timeout_s if lower; under 0.1 counts as 0.1 before the ceiling. Expiry: timeout."],
        [
            "Seconds for the interrupt and the stop together. Default 10, or the debugger entry's timeout_s if lower; under 0.1 counts as 0.1 before the ceiling. Expiry: timeout.",
            "Seconds for the interrupt and for its stop, each. Default and ceiling: 10, or the debugger entry's timeout_s if higher; under 0.1 counts as 0.1 before the ceiling. Expiry: timeout.",
            "Seconds for the interrupt and for its stop, each. Default: the debugger entry's timeout_s (60); under 0.1 counts as 0.1 before the ceiling. Expiry: timeout.",
            "Seconds for the interrupt and for its stop, each. Default and ceiling: 10, or the debugger entry's timeout_s if lower; 0.1 at least. Expiry: timeout.",
        ],
    ),
    (
        continue_says_it_resumes_and_waits,
        ["Resumes the core and waits for it to stop.", "Runs the target until it stops."],
        ["Resumes the core and does not wait for a stop.", "Halts the core.", "Never resumes the core until it stops."],
    ),
    (
        continue_needs_the_execution_grant,
        ["Needs allow_debug_execution (else permission_denied).", "Requires allow_debug_execution; without it, permission_denied."],
        ["Needs no allow_debug_execution.", "Needs allow_debug_execution.", "Works without allow_debug_execution."],
    ),
    (
        continue_leaves_an_exception_stop_alone,
        ["A core stopped in an exception or debugger_error is not resumed.", "Never continues past an exception stop."],
        ["A core stopped in an exception is resumed past it.", "Continues from an exception stop.", "An exception stop is resumed, not left alone."],
    ),
    (
        continue_tells_good_stops_from_bad,
        ["Returns stop_reason, stop (frame), target_ok: breakpoint_hit is ok; unexpected_breakpoint and target_exception are not."],
        [
            "Returns stop_reason, stop (frame): breakpoint_hit is not ok; unexpected_breakpoint and target_exception are not.",
            "Returns stop_reason, stop (frame): breakpoint_hit, unexpected_breakpoint and target_exception are ok.",
            "Returns stop_reason, stop: breakpoint_hit is ok; unexpected_breakpoint and target_exception are not.",
        ],
    ),
    (
        continue_says_what_expiry_does,
        ["Expiry: interrupt, its stop, up to 5 s each; timeout, halt_confirmed.", "On timeout the core is halted and halt_confirmed says whether that held."],
        ["On timeout the core is left running.", "On timeout the core is interrupted; halt_confirmed says so, but the core is left running.", "Expiry: no interrupt; timeout, halt_confirmed."],
    ),
    (
        continue_expiry_steps_are_capped,
        ["Expiry: interrupt, its stop, up to 5 s each; timeout, halt_confirmed.", "On expiry the interrupt and the wait for its stop get at most 5 seconds each."],
        ["Expiry: interrupt, its stop, 5 s in total at most.", "Expiry: interrupt, its stop; timeout.", "Expiry: interrupt, its stop, 5 s each."],
    ),
    (
        continue_timeout_is_the_wait_for_a_stop,
        ["Seconds to wait for a stop. Default and ceiling: the debugger entry's timeout_s (60 unless set); under 0.1 counts as 0.1 first."],
        [
            "Seconds to wait for a stop. Default and ceiling: 10, or the debugger entry's timeout_s; under 0.1 counts as 0.1 first.",
            "Seconds for the whole call. Default and ceiling: the debugger entry's timeout_s (60 unless set); under 0.1 counts as 0.1 first.",
            "Seconds to wait for a stop. Default: 60; under 0.1 counts as 0.1 first.",
            "Seconds to wait for a stop. Default and ceiling: the debugger entry's timeout_s (60 unless set); 0.1 at least.",
        ],
    ),
    (
        set_says_it_adds_a_breakpoint,
        ["Adds a breakpoint in the session.", "Sets one breakpoint."],
        ["Lists breakpoints.", "Removes a breakpoint.", "Never adds a breakpoint."],
    ),
    (
        set_returns_the_breakpoint_id,
        ["Returns breakpoint (id, backend_id).", "The breakpoint and its id."],
        ["Returns the location.", "Returns no breakpoint id."],
    ),
    (
        a_repeated_set_adds_another,
        ["Each call adds one, also at a location already set.", "Setting it again adds a second one."],
        ["Setting it again replaces the existing one.", "Each call at a location already set is ignored.", "Adds a breakpoint.", "A repeat never adds another."],
    ),
    (
        set_leaves_the_core_where_it_is,
        ["The core is not resumed, debug_continue runs to it.", "Does not resume the core."],
        ["Adds a breakpoint and resumes the core.", "Adds a breakpoint, the core runs to it."],
    ),
    (
        set_names_an_unconfirmed_insert,
        ["Unconfirmed insert: cleanup_required, provisional_breakpoint.", "provisional_breakpoint when the insert is lost."],
        ["Fails with timeout.", "Confirmed insert: cleanup_required."],
    ),
    (
        set_names_its_refusals,
        ["Refused: permission_denied (grant), invalid_argument (location), debugger_error (GDB)."],
        [
            "Refused: permission_denied (location), invalid_argument (grant), debugger_error (GDB).",
            "Refused: permission_denied (grant), invalid_argument (location).",
            "Never permission_denied (grant), invalid_argument (location), debugger_error (GDB).",
            "Refused: permission_denied (grant), invalid_argument (location), debugger_error (GDB) with ok true.",
        ],
    ),
    (
        location_names_the_grant_for_each_form,
        ["A symbol needs debug.allowed_symbols or debug.allow_all_symbols. A {\"file\", \"line\"} pair needs debug.allow_all_symbols."],
        [
            "A symbol needs debug.allowed_symbols and debug.allow_all_symbols. A file and line needs debug.allow_all_symbols.",
            "A symbol needs debug.allowed_symbols or debug.allow_all_symbols. A file and line needs no debug.allow_all_symbols.",
            "A symbol or a file needs debug.allowed_symbols or debug.allow_all_symbols.",
        ],
    ),
    (
        location_states_line_and_path_rules,
        ["Or {file, line}: line from 1, no '..'.", "The line is positive; a path with .. is refused."],
        [
            "Or {file, line}: line from 0, no '..'.",
            "Or {file, line}: line from 1; '..' is allowed.",
            "Or {file, line}: line from 1.",
            "Or {file, line}: line from 1, no '..' segment.",
            "Or {file, line}: line from 1, no parent segment '..'.",
        ],
    ),
    (
        location_names_identifiers_for_the_symbol_forms,
        ["A symbol or function: a C identifier. A file: a path.", "{\"symbol\": \"main\"}: identifier, ::-qualified allowed."],
        ["A symbol or function. A file: a path.", "A file: an identifier.", "Symbol, function or file: an identifier."],
    ),
    (
        gives_valid_location_examples,
        ["\"main\", {\"symbol\": \"main\"}, {\"function\": \"main\"}, or {\"file\": \"Src/main.c\", \"line\": 42}."],
        [
            "Symbol {\"function\": \"main\"} only.",
            "\"main\", {\"symbol\": \"main\"}, {\"function\": \"main\"}, or {\"file\": \"../main.c\", \"line\": 1}.",
            "\"main\", {\"symbol\": \"main\"}, {\"function\": \"main\"}, or {\"file\": \"Src/main.c\", \"line\": 0}.",
            "\"main\", {\"symbol\": \"main\"}, {\"function\": \"main\"}, or {file: main.c, line: 4}.",
            "\"a-b\", {\"symbol\": \"main\"}, {\"function\": \"main\"}, or {\"file\": \"main.c\", \"line\": 4}.",
            "{\"function\": \"main\"} or {\"file\": \"Src/main.c\", \"line\": 42}.",
            "\"main\", {\"symbol\": \"main\"} or {\"file\": \"Src/main.c\", \"line\": 42}.",
            "\"main\", {\"symbol\": \"main\"}, {\"function\": \"main\"}, or {\"file\": \"Src/main..c\", \"line\": 42}.",
        ],
    ),
    (
        list_says_it_lists_breakpoints,
        ["Lists the breakpoints debug_set_breakpoint added."],
        ["Clears the breakpoints.", "Lists the session status.", "Does not list the breakpoints."],
    ),
    (
        list_names_its_field_and_siblings,
        ["Returns breakpoints (id, backend_id, location); debug_clear_breakpoints removes them."],
        ["Returns breakpoints (id, backend_id, location).", "Returns the list; debug_clear_breakpoints removes them."],
    ),
    (
        clear_says_it_removes_every_breakpoint,
        ["Deletes every breakpoint GDB reports.", "Removes all breakpoints in the session."],
        ["Deletes one breakpoint.", "Lists all breakpoints.", "Never deletes every breakpoint."],
    ),
    (
        clear_names_its_reconciliation,
        ["Deletes what GDB reports: cleared, backend_reconciled true. Unconfirmed: ok false, breakpoint_reconciliation_failed."],
        [
            "Deletes what GDB reports: cleared, backend_reconciled true.",
            "Deletes every breakpoint: cleared.",
            "Deletes what GDB reports: cleared, backend_reconciled false. Unconfirmed: ok false, breakpoint_reconciliation_failed.",
            "Deletes what GDB reports: cleared, backend_reconciled true. Unconfirmed: ok true, breakpoint_reconciliation_failed.",
            "Deletes what GDB reports: cleared, backend_reconciled. Unconfirmed: ok false, breakpoint_reconciliation_failed.",
        ],
    ),
    (
        clear_leaves_the_core_alone,
        ["Neither resumes nor halts the core.", "The core stays halted; it never resumes it."],
        ["Resumes the core afterwards.", "Deletes every breakpoint."],
    ),
    (
        a_repeated_clear_clears_nothing,
        ["A repeat answers cleared 0.", "Called again, it clears nothing."],
        ["A repeat answers breakpoint_reconciliation_failed.", "Called again, it fails.", "Deletes every breakpoint."],
    ),
    (
        status_names_its_fields_and_sibling,
        ["Returns active, status (halted, running, cleanup_required), session. For the stop alone use debug_get_stop_reason."],
        ["Returns active and status (halted, running), session. For the stop alone use debug_get_stop_reason.", "Returns active, status (cleanup_required), session."],
    ),
    (
        stop_reason_names_its_fields_and_sibling,
        ["Returns stop_reason (breakpoint_hit, halted), stop (frame). For the session use debug_get_session_status."],
        ["Returns stop_reason (halted), stop (frame). For the session use debug_get_session_status.", "Returns stop_reason (breakpoint_hit), stop (frame)."],
    ),
    (
        nothing_stopped_yet_is_its_own_outcome,
        ["None yet: stop_reason_not_available.", "Before debug_continue or debug_halt: stop_reason_not_available."],
        ["After a stop: stop_reason_not_available.", "None yet: ok, stop_reason_not_available.", "Returns stop_reason."],
    ),
]


@pytest.mark.parametrize(("check", "accepted", "refused"), SELF_TESTS, ids=[check.__name__ for check, _, _ in SELF_TESTS])
def test_each_meaning_check_accepts_the_claim_and_refuses_its_inversion(check: Callable[[str], bool], accepted: list[str], refused: list[str]) -> None:
    assert [text for text in accepted if not check(text)] == []
    assert [text for text in refused if check(text)] == []


# ---------------------------------------------------------------------------
# The served definitions.


@pytest.fixture(scope="module")
def listed(tmp_path_factory: pytest.TempPathFactory) -> dict[str, dict]:
    return listed_tools(tmp_path_factory.mktemp("listed"))


@pytest.fixture(scope="module")
def known_literals() -> set[str]:
    return source_literals()


def description(listed: dict[str, dict], name: str) -> str:
    return str(listed[name]["description"])


@pytest.mark.parametrize("name", TOOLS)
def test_each_tool_is_listed_with_every_input_property_described(listed: dict[str, dict], name: str) -> None:
    assert name in listed, sorted(listed)
    undescribed = sorted(prop for prop, text in property_texts(listed[name]).items() if not text.strip())
    assert undescribed == [], (name, undescribed)


@pytest.mark.parametrize("name", TOOLS)
def test_each_definition_keeps_to_the_length_limits(listed: dict[str, dict], name: str) -> None:
    tool = listed[name]
    assert len(str(tool["description"])) <= DESCRIPTION_LIMIT, (name, len(str(tool["description"])))
    too_long = {prop: len(text) for prop, text in property_texts(tool).items() if len(text) > PROPERTY_DESCRIPTION_LIMIT}
    assert too_long == {}, (name, too_long)


@pytest.mark.parametrize("name", TOOLS)
def test_backend_support_is_one_phrase_saying_openocd_runs_sessions_and_the_others_refuse(listed: dict[str, dict], name: str) -> None:
    """Typed sessions exist only under OpenOCD on this code; pyOCD and
    STM32CubeProgrammer answer every one of these calls with `not_supported`
    (common.py `debug_session_unsupported`; knowledge.py `not_supported:stlink`
    and `not_supported:pyocd`)."""
    tool = listed[name]
    assert backend_support_is_one_phrase(str(tool["description"])), tool["description"]
    in_properties = {prop: text for prop, text in property_texts(tool).items() if any(backend in text for backend in BACKEND_NAMES)}
    assert in_properties == {}, in_properties


@pytest.mark.parametrize("name", TOOLS)
def test_each_description_names_the_session_it_works_in_and_the_one_debugger_it_binds(listed: dict[str, dict], name: str) -> None:
    text = description(listed, name)

    assert re.search(rf"\b{START}\b", text), text
    assert names_the_one_bound_debugger(text), text


@pytest.mark.parametrize("name", REFUSE_WITHOUT_SESSION)
def test_the_tools_that_need_a_session_say_what_they_answer_without_one(listed: dict[str, dict], name: str) -> None:
    """gdbdebug.py `_require_session`: no session, or one that has stopped,
    answers `session_not_active`."""
    assert refuses_without_a_session(description(listed, name)), description(listed, name)


@pytest.mark.parametrize("name", ANSWER_WITHOUT_SESSION)
def test_the_two_reads_say_they_answer_ok_and_inactive_without_a_session(listed: dict[str, dict], name: str) -> None:
    assert answers_ok_and_inactive_without_a_session(description(listed, name)), description(listed, name)


@pytest.mark.parametrize("name", READS)
def test_the_reads_say_they_never_move_the_core(listed: dict[str, dict], name: str) -> None:
    assert never_moves_the_core(description(listed, name)), description(listed, name)


@pytest.mark.parametrize("name", (STATUS, STOP_REASON))
def test_status_and_stop_reason_say_they_record_an_arrived_stop_and_answer_an_abnormal_one(listed: dict[str, dict], name: str) -> None:
    """Both take in a stop GDB delivered since the last call (gdbdebug.py
    `_refresh_session_stop`) and answer an abnormal one as a good read with a
    bad target (`target_stop_fields`)."""
    text = description(listed, name)

    assert records_a_newly_arrived_stop(text), text
    assert an_abnormal_stop_is_a_good_read(text), text


@pytest.mark.parametrize("name", TOOLS)
def test_every_stop_reason_a_definition_lists_is_one_the_code_produces(listed: dict[str, dict], name: str) -> None:
    assert "fault" not in PRODUCED_STOP_REASONS
    assert names_only_produced_stop_reasons(definition_text(listed[name])), definition_text(listed[name])


def test_halt_says_it_stops_the_core_and_keeps_the_session(listed: dict[str, dict]) -> None:
    tool = listed[HALT]
    text = description(listed, HALT)

    assert halt_says_it_stops_the_core(text), text
    assert halt_keeps_the_session_open(text), text
    assert needs_no_execution_grant_if_named(definition_text(tool)), definition_text(tool)


def test_halt_says_a_stopped_core_is_answered_as_it_stands(listed: dict[str, dict]) -> None:
    text = definition_text(listed[HALT])

    assert a_stopped_core_gets_no_interrupt(text), text
    assert halt_names_its_stop(text), text


def test_halt_names_what_an_unconfirmed_halt_answers(listed: dict[str, dict]) -> None:
    text = definition_text(listed[HALT])

    assert an_unconfirmed_halt_is_held(text), text


def test_halt_timeout_names_its_unit_default_ceiling_floor_and_that_it_bounds_each_step(listed: dict[str, dict]) -> None:
    text = property_texts(listed[HALT])["timeout_s"]

    assert halt_timeout_bounds_each_step_at_the_cap(text), text


def test_continue_says_it_resumes_until_a_stop_and_needs_the_execution_grant(listed: dict[str, dict]) -> None:
    text = description(listed, CONTINUE)

    assert continue_says_it_resumes_and_waits(text), text
    assert continue_needs_the_execution_grant(text), text
    assert continue_leaves_an_exception_stop_alone(text), text


def test_continue_names_the_stops_it_answers_and_what_expiry_does(listed: dict[str, dict]) -> None:
    text = definition_text(listed[CONTINUE])

    assert continue_tells_good_stops_from_bad(text), text
    assert continue_says_what_expiry_does(text), text


def test_continue_timeout_names_its_unit_default_ceiling_floor_and_what_it_bounds(listed: dict[str, dict]) -> None:
    text = property_texts(listed[CONTINUE])["timeout_s"]

    assert continue_timeout_is_the_wait_for_a_stop(text), text
    assert continue_expiry_steps_are_capped(text), text


def test_set_says_what_it_adds_what_a_repeat_does_and_that_the_core_stays(listed: dict[str, dict]) -> None:
    text = description(listed, SET)

    assert set_says_it_adds_a_breakpoint(text), text
    assert set_returns_the_breakpoint_id(text), text
    assert a_repeated_set_adds_another(text), text
    assert set_leaves_the_core_where_it_is(text), text
    assert set_names_an_unconfirmed_insert(text), text
    assert set_names_its_refusals(text), text


def test_location_names_each_form_its_grant_and_its_rules(listed: dict[str, dict]) -> None:
    text = property_texts(listed[SET])["location"]

    assert location_names_the_grant_for_each_form(text), text
    assert location_states_line_and_path_rules(text), text
    assert location_names_identifiers_for_the_symbol_forms(text), text
    assert gives_valid_location_examples(text), text


def test_list_says_what_it_lists_and_how_it_relates_to_its_siblings(listed: dict[str, dict]) -> None:
    text = description(listed, LIST)

    assert list_says_it_lists_breakpoints(text), text
    assert list_names_its_field_and_siblings(text), text
    assert lists_the_session_record_without_asking_gdb(text), text


def test_clear_says_it_reconciles_with_gdb_leaves_the_core_and_repeats_freely(listed: dict[str, dict]) -> None:
    text = description(listed, CLEAR)

    assert clear_says_it_removes_every_breakpoint(text), text
    assert clear_names_its_reconciliation(text), text
    assert clear_leaves_the_core_alone(text), text
    assert a_repeated_clear_clears_nothing(text), text


def test_status_names_its_fields_and_where_the_stop_alone_is_read(listed: dict[str, dict]) -> None:
    text = description(listed, STATUS)

    assert status_names_its_fields_and_sibling(text), text


def test_stop_reason_names_its_fields_and_what_it_answers_before_any_stop(listed: dict[str, dict]) -> None:
    text = definition_text(listed[STOP_REASON])

    assert stop_reason_names_its_fields_and_sibling(text), text
    assert lists_stop_reason_values(text), text
    assert nothing_stopped_yet_is_its_own_outcome(text), text


@pytest.mark.parametrize("name", TOOLS)
def test_every_identifier_a_definition_names_exists(listed: dict[str, dict], known_literals: set[str], name: str) -> None:
    tool = listed[name]

    assert unknown_identifiers(definition_text(tool), listed, tool, known_literals) == []


def test_the_annotations_agree_with_what_the_definitions_describe(listed: dict[str, dict]) -> None:
    """The three reads never move the core and send GDB nothing; status and
    stop reason do record a stop that arrived since the last call, which the
    hint does not cover, and readOnlyHint is kept as it stands (the hints are
    not changed unless they contradict the code). A set adds a new
    breakpoint each time and a continue runs on from wherever the last stop
    was, so neither is idempotent; a clear finds nothing the second time;
    nothing here is lost by any call, so nothing is destructive."""
    for name in READS:
        hints = listed[name]["annotations"]
        assert (hints["readOnlyHint"], hints["openWorldHint"]) == (True, False), (name, hints)
    for name, expected in ((SET, (False, False, False, False)), (CONTINUE, (False, False, False, False)), (HALT, (False, False, False, False)), (CLEAR, (False, False, True, False))):
        hints = listed[name]["annotations"]
        assert (hints["readOnlyHint"], hints["destructiveHint"], hints["idempotentHint"], hints["openWorldHint"]) == expected, (name, hints)


# ---------------------------------------------------------------------------
# The behaviour the definitions describe, through the fake debugger.


def test_without_a_session_the_two_reads_answer_inactive_and_the_rest_refuse(tmp_path: Path) -> None:
    service = debug_service(tmp_path)
    try:
        answers = {name: service.call(name, CALL_ARGUMENTS.get(name, {})) for name in TOOLS}
        blocked = service.coordinator.blocked
    finally:
        service.close()

    for name in ANSWER_WITHOUT_SESSION:
        assert answers[name]["ok"] is True, answers[name]
        assert answers[name]["active"] is False, answers[name]
    assert answers[LIST]["breakpoints"] == [], answers[LIST]
    for name in REFUSE_WITHOUT_SESSION:
        assert answers[name]["ok"] is False, answers[name]
        assert answers[name]["error_type"] == "session_not_active", answers[name]
        assert START in answers[name]["summary"], answers[name]
    assert blocked is False


def test_only_the_resume_needs_the_execution_grant(tmp_path: Path) -> None:
    service = debug_service(tmp_path, permissions={**DEFAULT_TEST_PERMISSIONS, "allow_debug_execution": False})
    try:
        assert start_debug_session(service, mode="attach")["ok"] is True
        added = service.call(SET, CALL_ARGUMENTS[SET])
        resumed = service.call(CONTINUE, {"timeout_s": REACHABLE_STOP_TIMEOUT_S})
        sent = gdb_commands(service)
        blocked = service.coordinator.blocked
        halted = service.call(HALT)
        cleared = service.call(CLEAR)
        assert service.call(STOP)["ok"] is True
    finally:
        service.close()

    assert resumed["ok"] is False, resumed
    assert resumed["error_type"] == "permission_denied", resumed
    assert "allow_debug_execution" in resumed["summary"], resumed
    assert "-exec-continue" not in sent, sent
    assert blocked is False
    assert added["ok"] is True, added
    assert halted["ok"] is True, halted
    assert halted["stop_reason"] == "halted", halted
    assert cleared["ok"] is True, cleared


def with_entry_timeout(debug: object, monkeypatch: pytest.MonkeyPatch, entry: float) -> None:
    """Run the session under a debugger entry whose `timeout_s` is `entry`.
    Set after the session started, so an entry below the floor bounds only
    the call under test and not the start that has to succeed first."""
    config = debug.config  # type: ignore[attr-defined]
    monkeypatch.setattr(debug, "config", replace(config, debugger=replace(config.debugger, timeout_s=entry)))


def recording_commands(debug: object, commands: tuple[str, ...], seen: list, monkeypatch: pytest.MonkeyPatch) -> None:
    """Record the timeout each of `commands` is sent with, and send it with
    the per-command cap instead, so the bound the code asks for is what is
    checked, not how fast the fake answers."""
    original = debug._gdb_command  # type: ignore[attr-defined]

    def wrapper(session, command, timeout_s=None, **kwargs):
        if command in commands:
            seen.append((command, timeout_s))
            timeout_s = GDB_COMMAND_TIMEOUT_CAP_S
        return original(session, command, timeout_s, **kwargs)

    monkeypatch.setattr(debug, "_gdb_command", wrapper)


@pytest.mark.parametrize(
    ("entry", "asked", "wait", "acknowledgement"),
    [
        (20.0, None, 20.0, CONTINUE_COMMAND_TIMEOUT_CAP_S),
        (20.0, 60.0, 20.0, CONTINUE_COMMAND_TIMEOUT_CAP_S),
        (20.0, 3.0, 3.0, 3.0),
        (20.0, 0.01, 0.1, 0.1),
        (20.0, 0.0, 0.1, 0.1),
        (ENTRY_BELOW_THE_FLOOR_S, 0.0, ENTRY_BELOW_THE_FLOOR_S, ENTRY_BELOW_THE_FLOOR_S),
    ],
    ids=["omitted", "above-the-entry", "below-the-entry", "below-the-floor", "zero", "entry-below-the-floor"],
)
def test_the_continue_timeout_is_the_wait_for_a_stop_and_never_longer_than_the_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entry: float, asked: float | None, wait: float, acknowledgement: float
) -> None:
    """A short request is raised to 0.1 before the entry caps it, so an entry
    below 0.1 still bounds the call (gdbdebug.py `continue_execution`). The
    resume's own acknowledgement is bounded by the same wait, at 5 seconds
    at most."""
    service = debug_service(tmp_path, timeout_s=20)
    debug = service.backend._debug
    waits: list = []
    sent: list = []
    try:
        assert start_debug_session(service, mode="attach")["ok"] is True
        assert service.call(SET, CALL_ARGUMENTS[SET])["ok"] is True
        if entry != 20.0:
            with_entry_timeout(debug, monkeypatch, entry)
        assert debug.session is not None
        recording(debug.session.gdb, "wait_for_stop", waits, monkeypatch, 0, REACHABLE_STOP_TIMEOUT_S)
        recording_commands(debug, ("-exec-continue",), sent, monkeypatch)
        resumed = service.call(CONTINUE, {} if asked is None else {"timeout_s": asked})
        monkeypatch.undo()
        assert service.call(STOP)["ok"] is True
    finally:
        monkeypatch.undo()
        service.close()

    assert resumed["ok"] is True, resumed
    assert resumed["stop_reason"] == "breakpoint_hit", resumed
    assert waits == [wait], waits
    assert sent == [("-exec-continue", acknowledgement)], sent


@pytest.mark.parametrize(
    ("entry", "asked", "wait", "step"),
    [(20.0, 3.0, 3.0, CONTINUE_COMMAND_TIMEOUT_CAP_S), (3.0, 1.0, 1.0, 3.0)],
    ids=["entry-above-the-step-cap", "entry-below-the-step-cap"],
)
def test_a_continue_that_runs_out_interrupts_and_waits_for_that_stop_each_up_to_five_seconds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entry: float, asked: float, wait: float, step: float
) -> None:
    """Nothing stops the resumed core, so the wait runs out. The interrupt
    that contains it and the wait for its stop each get the entry's timeout_s,
    5 seconds at most, on top of the caller's timeout (gdbdebug.py
    `continue_execution`, `CONTINUE_COMMAND_TIMEOUT_CAP_S`)."""
    service = debug_service(tmp_path, fake_gdb_behavior=BENCH_RUN_STATE, timeout_s=entry)
    debug = service.backend._debug
    waits: list = []
    sent: list = []
    with settled_afterwards(service):
        assert start_debug_session(service, mode="attach")["ok"] is True
        assert debug.session is not None
        gdb = debug.session.gdb
        original = gdb.wait_for_stop
        # The first wait runs out on the fake; the second has the interrupt's
        # stop to arrive at.
        runs = iter((UNREACHABLE_STOP_TIMEOUT_S, REACHABLE_STOP_TIMEOUT_S))

        def record_wait(timeout_s):
            waits.append(timeout_s)
            return original(next(runs))

        monkeypatch.setattr(gdb, "wait_for_stop", record_wait)
        recording_commands(debug, ("-exec-continue", INTERRUPT_COMMAND), sent, monkeypatch)
        timed_out = service.call(CONTINUE, {"timeout_s": asked})
        monkeypatch.undo()

        assert timed_out["ok"] is False, timed_out
        assert timed_out["error_type"] == "timeout", timed_out
        assert timed_out["halt_confirmed"] is True, timed_out
        assert sent == [("-exec-continue", wait), (INTERRUPT_COMMAND, step)], sent
        assert waits == [wait, step], waits
        assert service.call(STOP)["ok"] is True


@pytest.mark.parametrize(
    ("entry", "asked", "expected"),
    [
        (20.0, None, 10.0),
        (20.0, 60.0, 10.0),
        (20.0, 3.0, 3.0),
        (3.0, None, 3.0),
        (20.0, 0.01, 0.1),
        (20.0, 0.0, 0.1),
        (ENTRY_BELOW_THE_FLOOR_S, None, ENTRY_BELOW_THE_FLOOR_S),
        (ENTRY_BELOW_THE_FLOOR_S, 0.0, ENTRY_BELOW_THE_FLOOR_S),
    ],
    ids=["omitted", "above-the-cap", "below-the-cap", "entry-below-the-cap", "below-the-floor", "zero", "entry-below-the-floor", "zero-under-an-entry-below-the-floor"],
)
def test_the_halt_timeout_bounds_the_interrupt_and_the_wait_each_at_ten_seconds_at_most(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entry: float, asked: float | None, expected: float
) -> None:
    """On a running target, the only kind a halt sends an interrupt to: the
    first interrupt, the resume's own containment, is lost, so the session
    records the target running when the halt comes. A short request is
    raised to 0.1 before the ceiling applies, so an entry below 0.1 still
    bounds the halt (gdbdebug.py `halt`). The entry is set once the target
    runs, so the resume before it is not bounded by it."""
    monkeypatch.setattr(gdbdebug, "CONTINUE_COMMAND_TIMEOUT_CAP_S", TIMEOUT_TEST_CAP_S)
    service = debug_service(tmp_path, fake_gdb_behavior=f"{BENCH_RUN_STATE}+{INTERRUPT_LOST_ONCE}", timeout_s=20)
    debug = service.backend._debug
    sent: list = []
    waits: list = []
    try:
        assert start_debug_session(service, mode="attach")["ok"] is True
        running = service.call(CONTINUE, {"timeout_s": UNREACHABLE_STOP_TIMEOUT_S})
        assert running["session"]["status"] == "running", running
        if entry != 20.0:
            with_entry_timeout(debug, monkeypatch, entry)
        recording_commands(debug, (INTERRUPT_COMMAND,), sent, monkeypatch)
        assert debug.session is not None
        recording(debug.session.gdb, "wait_for_stop", waits, monkeypatch, 0, REACHABLE_STOP_TIMEOUT_S)
        halted = service.call(HALT, {} if asked is None else {"timeout_s": asked})
        monkeypatch.undo()
        interrupts = [timeout for _, timeout in sent]
    finally:
        monkeypatch.undo()
        try:
            service.close()
        except RuntimeError:
            service.coordinator.close()
            raise

    assert halted["ok"] is True, halted
    assert halted["stop_reason"] == "halted", halted
    assert (interrupts, waits) == ([expected], [expected])


def test_a_repeated_set_adds_a_second_breakpoint_and_a_repeated_clear_clears_nothing(tmp_path: Path) -> None:
    service = debug_service(tmp_path)
    try:
        assert start_debug_session(service, mode="attach")["ok"] is True
        first = service.call(SET, CALL_ARGUMENTS[SET])
        second = service.call(SET, CALL_ARGUMENTS[SET])
        before = service.call(LIST)
        halted = service.call(HALT)
        cleared = service.call(CLEAR)
        again = service.call(CLEAR)
        after = service.call(LIST)
        reason = service.call(STOP_REASON)
        status = service.call(STATUS)
        assert service.call(STOP)["ok"] is True
    finally:
        service.close()

    assert first["ok"] is True and second["ok"] is True, (first, second)
    assert (first["breakpoint"]["id"], second["breakpoint"]["id"]) == (1, 2)
    assert first["breakpoint"]["backend_id"] != second["breakpoint"]["backend_id"]
    assert len(before["breakpoints"]) == 2, before
    assert halted["ok"] is True, halted
    assert (cleared["ok"], cleared["cleared"], cleared["backend_reconciled"]) == (True, 2, True), cleared
    assert (again["ok"], again["cleared"], again["backend_reconciled"]) == (True, 0, True), again
    assert after["breakpoints"] == [], after
    # Clearing moved nothing: the core is where the halt left it.
    assert (reason["ok"], reason["stop_reason"]) == (True, "halted"), reason
    assert (status["active"], status["status"]) == (True, "halted"), status


def test_a_clear_that_cannot_read_gdbs_list_holds_the_bench_until_a_clear_reconciles(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service = debug_service(tmp_path)
    debug = service.backend._debug
    try:
        assert start_debug_session(service, mode="attach")["ok"] is True
        assert service.call(SET, CALL_ARGUMENTS[SET])["ok"] is True
        monkeypatch.setattr(debug, "_backend_breakpoint_numbers", lambda session: None)
        failed = service.call(CLEAR)
        held = service.coordinator.blocked
        monkeypatch.undo()
        settled = service.call(CLEAR)
        freed = not service.coordinator.blocked
        assert service.call(STOP)["ok"] is True
    finally:
        monkeypatch.undo()
        service.close()

    assert failed["ok"] is False, failed
    assert failed["error_type"] == "breakpoint_reconciliation_failed", failed
    assert failed["cleanup_required"] is True, failed
    assert held is True
    assert (settled["ok"], settled["cleared"], settled["backend_reconciled"]) == (True, 1, True), settled
    assert freed is True


@pytest.mark.parametrize("location", [{"file": "../main.c", "line": 1}, {"file": "Src/main.c", "line": 0}], ids=["parent-segment", "line-zero"])
def test_a_file_location_outside_the_rules_is_refused_before_gdb_sees_it(tmp_path: Path, location: dict) -> None:
    service = debug_service(tmp_path)
    try:
        assert start_debug_session(service, mode="attach")["ok"] is True
        refused = service.call(SET, {"location": location})
        sent = gdb_commands(service)
        remaining = service.call(LIST)
        assert service.call(STOP)["ok"] is True
    finally:
        service.close()

    assert refused["ok"] is False, refused
    assert refused["error_type"] == "invalid_argument", refused
    assert not any(command.startswith("-break-insert") for command in sent), sent
    assert remaining["breakpoints"] == [], remaining


def test_status_after_a_stop_that_could_not_confirm_the_halt_reports_cleanup_required(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service = debug_service(tmp_path)
    debug = service.backend._debug
    try:
        assert start_debug_session(service, mode="attach")["ok"] is True
        monkeypatch.setattr(debug, "_confirm_halted_before_end", lambda session, timeout_s: False)
        stopped = service.call(STOP)
        monkeypatch.undo()
        status = service.call(STATUS)
    finally:
        monkeypatch.undo()
        with pytest.raises(RuntimeError, match="reconfirming the target was halted"):
            service.close()
        service.coordinator.close()

    assert stopped["error_type"] == "halt_not_confirmed", stopped
    assert status["ok"] is True, status
    assert status["status"] == "cleanup_required", status
    assert status["cleanup_required"] is True, status
    assert status["hardware_state"] == "unknown", status
    assert status["quarantined"] is True, status


@pytest.mark.parametrize("backend", ["stlink", "pyocd"])
def test_every_run_control_call_answers_not_supported_where_sessions_do_not_exist(tmp_path: Path, backend: str) -> None:
    service = stlink_dump_service(tmp_path) if backend == "stlink" else pyocd_service(tmp_path)
    try:
        answers = {name: service.call(name, CALL_ARGUMENTS.get(name, {})) for name in TOOLS}
    finally:
        service.close()

    for name, result in answers.items():
        assert result["ok"] is False, (backend, name, result)
        assert result["error_type"] == "not_supported", (backend, name, result)
        assert "OpenOCD" in result["summary"], (backend, name, result)
        assert result["target_contacted"] is False, (backend, name, result)


SECOND_DEBUGGER = "probe_b"


def service_with_debuggers(tmp_path: Path, count: int) -> AgenticHILToolService:
    """A service whose config declares no debugger, or two."""
    if count == 2:
        return debug_service(tmp_path, debuggers_yaml=f'debuggers:\n  {SECOND_DEBUGGER}:\n    type: openocd\n    probe_id: "PROBE-B"\n')
    path = write_config(tmp_path)
    path.write_text(re.sub(r"(?m)^debuggers:\n(?:  .*\n)+", "debuggers: {}\n", path.read_text(encoding="utf-8")), encoding="utf-8")
    return AgenticHILToolService(load_config(str(path)))


@pytest.mark.parametrize(("count", "configured"), [(0, []), (2, ["dut", SECOND_DEBUGGER])], ids=["none", "two"])
def test_every_run_control_call_answers_not_supported_unless_exactly_one_debugger_is_configured(tmp_path: Path, count: int, configured: list[str]) -> None:
    """The server binds a debugger only when exactly one is configured
    (config.py `load_config`); otherwise each of these calls is refused before
    any session is looked at (tools.py `unbound_debugger_error`)."""
    service = service_with_debuggers(tmp_path, count)
    try:
        answers = {name: service.call(name, CALL_ARGUMENTS.get(name, {})) for name in TOOLS}
    finally:
        service.close()

    for name, result in answers.items():
        assert result["ok"] is False, (name, result)
        assert result["error_type"] == "not_supported", (name, result)
        assert result["configured_debuggers"] == configured, (name, result)
        assert result["side_effect_status"] == "not_started", (name, result)
        assert result["retry_safe"] is False, (name, result)


@pytest.mark.parametrize("name", (HALT, CONTINUE))
def test_a_negative_timeout_is_refused_before_gdb_sees_anything(tmp_path: Path, name: str) -> None:
    """Only a short timeout is raised to 0.1; a negative one is not a
    timeout and is refused by the schema (contracts.py `TIMEOUT`)."""
    service = debug_service(tmp_path)
    try:
        assert start_debug_session(service, mode="attach")["ok"] is True
        before = gdb_commands(service)
        refused = service.call(name, {"timeout_s": -1.0})
        after = gdb_commands(service)
        blocked = service.coordinator.blocked
        assert service.call(STOP)["ok"] is True
    finally:
        service.close()

    assert refused["ok"] is False, refused
    assert refused["error_type"] == "invalid_argument", refused
    assert refused["field"] == "timeout_s", refused
    assert after == before, after[len(before):]
    assert blocked is False


def test_a_breakpoint_gdb_refuses_answers_debugger_error_and_leaves_none_behind(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """GDB answers the insert with an error: the call answers
    `debugger_error`, and nothing is added to the session's list. What else
    such a refusal does to the bench is not pinned here."""
    service = debug_service(tmp_path)
    debug = service.backend._debug
    try:
        assert start_debug_session(service, mode="attach")["ok"] is True
        assert debug.session is not None
        gdb = debug.session.gdb
        original = gdb.command

        def refuse_insert(mi_command, timeout_s):
            if mi_command.startswith("-break-insert"):
                return GdbMiCommandResult(result_class="error", line='^error,msg="No symbol table is loaded."', error_message="No symbol table is loaded.")
            return original(mi_command, timeout_s)

        monkeypatch.setattr(gdb, "command", refuse_insert)
        refused = service.call(SET, CALL_ARGUMENTS[SET])
        monkeypatch.undo()
        remaining = service.call(LIST)
    finally:
        monkeypatch.undo()
        try:
            service.close()
        except RuntimeError:
            service.coordinator.close()

    assert refused["ok"] is False, refused
    assert refused["error_type"] == "debugger_error", refused
    assert "provisional_breakpoint" not in refused, refused
    assert remaining["breakpoints"] == [], remaining


def test_a_file_name_with_two_dots_inside_passes_the_schema_and_is_refused_before_gdb_sees_it(tmp_path: Path) -> None:
    """The schema refuses `..` only as a whole path segment; the session
    refuses it anywhere in the path (gdbdebug.py
    `normalize_breakpoint_location`), so the definition says `..` without
    narrowing it to a segment."""
    location = {"file": "Src/main..c", "line": 1}
    service = debug_service(tmp_path)
    try:
        assert start_debug_session(service, mode="attach")["ok"] is True
        refused = service.call(SET, {"location": location})
        sent = gdb_commands(service)
        assert service.call(STOP)["ok"] is True
    finally:
        service.close()

    assert validate_tool_arguments(SET, {"location": location}) is None
    assert refused["ok"] is False, refused
    assert refused["error_type"] == "invalid_argument", refused
    assert not any(command.startswith("-break-insert") for command in sent), sent


def test_the_reads_send_gdb_nothing_and_status_records_a_stop_that_arrived(tmp_path: Path) -> None:
    """None of the three reads sends GDB a command. Status takes in a stop
    GDB delivered since the last call and records it, which is why the
    definitions say the reads never move the core rather than that they
    change nothing (gdbdebug.py `_refresh_session_stop`)."""
    service = debug_service(tmp_path)
    debug = service.backend._debug
    try:
        assert start_debug_session(service, mode="attach")["ok"] is True
        added = service.call(SET, CALL_ARGUMENTS[SET])
        before = gdb_commands(service)
        reads = {name: service.call(name) for name in READS}
        after = gdb_commands(service)
        assert debug.session is not None
        gdb = debug.session.gdb
        with gdb.lock:
            gdb.last_stop_line = EXPECTED_BREAKPOINT_STOP
        status = service.call(STATUS)
        reason = service.call(STOP_REASON)
        assert service.call(STOP)["ok"] is True
    finally:
        service.close()

    assert added["breakpoint"]["backend_id"] == "1", added
    assert (reads[STATUS]["ok"], reads[LIST]["ok"]) == (True, True), reads
    assert len(reads[LIST]["breakpoints"]) == 1, reads[LIST]
    assert reads[STOP_REASON]["error_type"] == "stop_reason_not_available", reads[STOP_REASON]
    assert after == before, after[len(before):]
    assert status["status"] == "halted", status
    assert status["target_stop_reason"] == "breakpoint_hit", status
    assert (reason["ok"], reason["stop_reason"]) == (True, "breakpoint_hit"), reason


def test_an_abnormal_stop_is_read_as_a_good_answer_about_a_bad_target(tmp_path: Path) -> None:
    """`ok` is the read, `target_ok` the core (gdbdebug.py
    `target_stop_fields`)."""
    service = debug_service(tmp_path, fake_gdb_behavior="hardfault")
    with settled_afterwards(service):
        assert start_debug_session(service)["ok"] is True
        assert service.call(SET, CALL_ARGUMENTS[SET])["ok"] is True
        faulted = service.call(CONTINUE, {"timeout_s": REACHABLE_STOP_TIMEOUT_S})
        assert faulted["stop_reason"] == "exception", faulted
        status = service.call(STATUS)
        reason = service.call(STOP_REASON)

        for read in (status, reason):
            assert read["ok"] is True, read
            assert read["target_ok"] is False, read
            assert read["target_error_type"] == "target_exception", read
        assert service.call(STOP)["ok"] is True
