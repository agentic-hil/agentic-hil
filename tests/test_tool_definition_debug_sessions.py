"""What the two debug session lifecycle tools tell a caller before the first call.

`debug_start_session` and `debug_stop_session` open and close the GDB session
every other typed debug tool runs in. A caller reads their definitions, as a
host serves them through `tools/list`, to decide whether a session can exist on
this bench at all, what it must have configured first, what each argument
means, which state the board is in once either call returns, and what each
refusal it can meet means.

The first half of this file reads the served definitions and checks their
meaning, not their wording. Where a reversed claim would mislead a caller
(halted or resumed, kept or dropped, needed or refused, true or false), the
check binds the words that carry the claim to one clause and rejects the
reversed claim outright. Which backends run sessions is kept to one phrase in
each description, so a change to that support edits one phrase.

The second half holds the behaviour those definitions describe, through the
fake debugger, wherever no existing test already holds it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from conftest import DEFAULT_TEST_PERMISSIONS, FAKE_PYOCD, write_config
from test_debug_sessions import debug_service, start_debug_session, stlink_dump_service

import agentic_hil
from agentic_hil.backends import gdbdebug
from agentic_hil.backends.gdbdebug import GDB_COMMAND_TIMEOUT_CAP_S, STOP_SESSION_TIMEOUT_CAP_S
from agentic_hil.config import load_config
from agentic_hil.mcp import handle_mcp_message
from agentic_hil.tools import AgenticHILToolService

START = "debug_start_session"
STOP = "debug_stop_session"
LIFECYCLE_TOOLS = (START, STOP)
MODES = ("attach", "reset_halt", "load")
# What the configuration falls back to when a debugger entry names no
# `timeout_s` (config.py, `positive_timeout_config(raw.get("timeout_s"), 60.0, ...)`).
CONFIGURED_DEBUGGER_TIMEOUT_DEFAULT = "60"
# Both timeouts are raised to this before the ceiling applies (gdbdebug.py,
# `max(0.1, timeout_s)` in `start_session` and `stop_session`).
TIMEOUT_FLOOR = "0.1"
BACKEND_NAMES = ("OpenOCD", "pyOCD", "STM32CubeProgrammer")
IDENTIFIER = re.compile(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b")

SECONDS = re.compile(r"\bseconds?\b|\b\d+(?:\.\d+)? ?s\b", re.IGNORECASE)
CEILING = re.compile(r"\b(ceiling|at most|cannot (exceed|raise|extend)|never (exceeds?|raises?|extends?|above|more)|capped|caps?|no (more|longer) than|not (above|beyond|more than))\b", re.IGNORECASE)
FLOOR = re.compile(r"\b(at least|minimum|floor|no less than|not below|never below)\b", re.IGNORECASE)
WHOLE_CALL = re.compile(r"\b(whole|entire|total|overall)\b", re.IGNORECASE)
NO_SESSION = re.compile(r"\b(no (active |debug )?session|without (an active |a )?session|nothing (is )?open)\b", re.IGNORECASE)
NEGATED = re.compile(r"\b(no|not|never|without|off|disables?|disabled|turns? off|prevents?|blocks?|pins?)\b", re.IGNORECASE)
ENDS = re.compile(r"\b(ends?|exits?|stops?|terminates?|closes?|shuts? down|kills?|quits?)\b", re.IGNORECASE)
STARTS = re.compile(r"\b(starts?|launch(es)?|spawns?|opens?|restarts?|keeps? (running|alive))\b", re.IGNORECASE)
DROPPED = re.compile(r"\b(drops?|dropped|discards?|clears?|cleared|ends?|gone|removes?|removed|deletes?|deleted|lost|with it)\b", re.IGNORECASE)
KEPT = re.compile(r"\b(keeps?|kept|retains?|preserves?|survives?|persists?|remains?|stays?)\b", re.IGNORECASE)
FREED = re.compile(r"\b(frees?|freed|releases?|released|unblocks?|again|available|usable)\b", re.IGNORECASE)
STILL_BLOCKED = re.compile(r"\b(keeps?|stays?|remains?|still)\b[^,.;]*\b(blocked|busy|refused|held|locked)\b|\bblocks?\b", re.IGNORECASE)
HELD = re.compile(r"\b(quarantin\w*|held|holds|retains?|retained|keeps?)\b", re.IGNORECASE)
REFUSED_OR_OFF_BEFORE = re.compile(r"\b(refused|refuses|denied|not|never|without|unless)\b", re.IGNORECASE)
OFF_AFTER = r"\W{0,3}(off|false|disabled|unset|not granted)\b"

# ---------------------------------------------------------------------------
# Reading the definitions the way a host does.

def listed_tools(tmp_path: Path) -> dict[str, dict]:
    """Every tool the server answers `tools/list` with, by name."""
    service = AgenticHILToolService(load_config(str(write_config(tmp_path))))
    try:
        response = handle_mcp_message({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, service)
    finally:
        service.close()
    assert isinstance(response, dict), response
    return {str(tool["name"]): tool for tool in response["result"]["tools"]}


def property_texts(tool: dict) -> dict[str, str]:
    properties = tool["inputSchema"].get("properties", {})
    return {name: str(schema.get("description", "")) for name, schema in properties.items()}


def definition_text(tool: dict) -> str:
    """The description and every property description, as one text."""
    return " ".join([str(tool["description"]), *property_texts(tool).values()])


def description_sentences(text: str) -> list[str]:
    """Sentences, split only at a full stop, so a clause after a semicolon or a
    colon stays with the sentence it qualifies."""
    return [sentence for sentence in re.split(r"(?<=\.)\s+", text.strip()) if sentence]


def clauses(text: str) -> list[str]:
    """Sentences split once more at a semicolon: the unit one claim lives in."""
    return [clause.strip() for sentence in description_sentences(text) for clause in sentence.split(";") if clause.strip()]


def pieces(text: str) -> list[str]:
    """Clauses split once more at a comma: one step of a listed sequence."""
    return [piece.strip() for clause in clauses(text) for piece in clause.split(",") if piece.strip()]


def containing(units: list[str], pattern: str, flags: int = 0) -> list[str]:
    return [unit for unit in units if re.search(pattern, unit, flags)]


def mode_segments(text: str) -> dict[str, str]:
    """What the mode description says about each mode: the text from the first
    mention of one mode to the first mention of the next, without the name."""
    starts = {}
    for mode in MODES:
        found = re.search(rf"\b{mode}\b", text)
        assert found, (mode, text)
        starts[mode] = found
    order = sorted(MODES, key=lambda mode: starts[mode].start())
    assert order == list(MODES), (order, text)
    segments = {}
    for index, mode in enumerate(MODES):
        end = starts[MODES[index + 1]].start() if index + 1 < len(MODES) else len(text)
        segments[mode] = text[starts[mode].end():end]
    return segments


def source_literals() -> set[str]:
    """Every snake_case word the package's own source quotes as a string: the
    error types, result fields and configuration keys it really produces or
    reads. Read from the installed package, so this holds in an sdist too."""
    literals: set[str] = set()
    for path in Path(agentic_hil.__file__).parent.rglob("*.py"):
        literals.update(re.findall(r"[\"']([a-z][a-z0-9]*(?:_[a-z0-9]+)+)[\"']", path.read_text(encoding="utf-8")))
    return literals


def unknown_identifiers(text: str, listed: dict[str, dict], tool: dict, known: set[str]) -> list[str]:
    """Snake_case words in `text` that are neither a listed tool, one of this
    tool's own arguments or enum values, nor a string the source produces."""
    words = set(IDENTIFIER.findall(text))
    return sorted(words - set(listed) - schema_words(tool) - known)


def schema_words(tool: dict) -> set[str]:
    """Input property names and enum values: identifiers a definition names
    that are arguments rather than tools."""
    words: set[str] = set()
    for name, schema in tool["inputSchema"].get("properties", {}).items():
        words.add(name)
        words.update(str(value) for value in schema.get("enum", []))
    return words


@pytest.fixture(scope="module")
def listed(tmp_path_factory: pytest.TempPathFactory) -> dict[str, dict]:
    return listed_tools(tmp_path_factory.mktemp("listed"))


@pytest.fixture(scope="module")
def known_literals() -> set[str]:
    return source_literals()


def test_both_lifecycle_tools_are_listed_with_every_input_property_described(listed: dict[str, dict]) -> None:
    for name in LIFECYCLE_TOOLS:
        assert name in listed, sorted(listed)
        described = property_texts(listed[name])
        assert described, name
        undescribed = sorted(prop for prop, text in described.items() if not text.strip())
        assert undescribed == [], (name, undescribed)


@pytest.mark.parametrize("name", LIFECYCLE_TOOLS)
def test_backend_support_is_one_phrase_saying_openocd_runs_sessions_and_the_others_refuse(listed: dict[str, dict], name: str) -> None:
    """Typed sessions exist only under OpenOCD on this code (openocd.py
    `debug_start_session`); pyOCD and STM32CubeProgrammer refuse both calls
    with `not_supported` (pyocd.py and stlink.py, `debug_session_unsupported`).
    Kept to one sentence of the description, and out of the property
    descriptions, so a change to the support edits one place."""
    tool = listed[name]
    description = str(tool["description"])

    naming_a_backend = [sentence for sentence in description_sentences(description) if any(backend in sentence for backend in BACKEND_NAMES)]
    assert len(naming_a_backend) == 1, (name, naming_a_backend, description)
    sentence = naming_a_backend[0]
    parts = re.split(r"[;,:]", sentence)

    supported = [part for part in parts if "OpenOCD" in part]
    assert supported, sentence
    assert all(re.search(r"\b(only|required|needs?|requires?|runs?|supports?)\b", part) for part in supported), sentence
    # The direction: OpenOCD is never the backend that refuses.
    assert not any("not_supported" in part for part in supported), sentence
    refusing = [part for part in parts if "not_supported" in part]
    assert refusing, sentence
    assert all(re.search(r"\b(others?|other backends?|pyOCD|STM32CubeProgrammer|elsewhere|any other)\b", part) for part in refusing), sentence

    in_properties = {prop: text for prop, text in property_texts(tool).items() if any(backend in text for backend in BACKEND_NAMES)}
    assert in_properties == {}, in_properties


def test_start_says_what_it_opens_and_how_the_session_is_inspected_and_ended(listed: dict[str, dict]) -> None:
    description = str(listed[START]["description"])

    assert re.search(r"\bGDB\b", description), description
    assert re.search(r"\bELF\b", description), description
    # The tools that run inside the session it opens, so a caller can tell the
    # lifecycle apart from halting or resuming the core within one.
    inside = containing(clauses(description), r"debug_continue")
    assert inside and all("debug_halt" in clause and re.search(r"\bsession\b", clause) for clause in inside), (inside, description)
    assert all(re.search(r"\b(for|needs?|run in|runs in|inside|within|uses?|requires?)\b", clause) for clause in inside), inside
    assert not re.search(r"\bdebug_(continue|halt)\s+(also\s+)?(starts?|opens?|ends?|stops?|closes?)\s+(the\s+|a\s+)?session\b", description), description
    # Inspecting and ending the session.
    inspect = containing(clauses(description), r"\bdebug_get_session_status\b")
    assert inspect and all(re.search(r"\b(inspects?|checks?|reads?|shows?|reports?|status)\b", clause, re.IGNORECASE) for clause in inspect), inspect
    end = containing(clauses(description), rf"\b{STOP}\b")
    assert end and all(ENDS.search(clause) for clause in end), end


def test_start_names_its_prerequisites_on_the_bench(listed: dict[str, dict]) -> None:
    """GDB from `debug.gdb_executable` or found on PATH (gdbdebug.py
    `resolve_gdb_executable`, config.py `GDB_AUTODETECT_CANDIDATES`), and the
    debugger entry's own scripts the debug server is started with (openocd.py
    `_debug_server_args`, `interface_cfg` and `target_cfg`)."""
    description = str(listed[START]["description"])

    needs = containing(clauses(description), r"\b(needs?|requires?)\b", re.IGNORECASE)
    assert needs, description
    assert any(re.search(r"\bGDB\b", clause) and "gdb_executable" in clause for clause in needs), needs
    assert any(re.search(r"\bdebugger\b[^.;]*\b(scripts?|executable|interface_cfg|target_cfg)\b", clause) for clause in needs), needs


def test_start_says_the_state_it_returns_in_and_what_a_second_start_answers(listed: dict[str, dict]) -> None:
    description = str(listed[START]["description"])

    # Every mode returns with the core halted (gdbdebug.py, start ends with
    # `session.status = "halted"`).
    halted = containing(clauses(description), r"\bhalted\b")
    assert any(re.search(r"\b(returns?|leaves?|stays?|ends?|with)\b", clause, re.IGNORECASE) for clause in halted), (halted, description)
    assert not re.search(r"\bcore\s+(is\s+)?(running|runs|resumed|resumes)\b", description), description
    # The result fields: where the core stopped, and the session's own log
    # (gdbdebug.py `start_session`, `session` and `log_path`).
    assert any("stop_reason" in clause and re.search(r"\bsession\b", clause) for clause in clauses(description)), description
    assert "log_path" in description, description
    # One session at a time: a second start is refused, never a replacement.
    second = containing(clauses(description), r"\bsession_already_active\b")
    assert second, description
    assert all(re.search(r"\b(another|second|again|existing|already|open|while|one at a time)\b", clause, re.IGNORECASE) for clause in second), second
    assert not any(re.search(r"\b(replaces?|restarts?|closes?|ends?|reuses?)\b", clause, re.IGNORECASE) for clause in second), second


def test_start_binds_each_grant_to_the_modes_that_need_it(listed: dict[str, dict]) -> None:
    """gdbdebug.py `_start_permission`: a mode other than attach needs
    `allow_reset`; `load` needs `allow_flash` and is refused while
    `allow_mass_erase` is granted; every mode is refused while
    `allow_raw_debugger_commands` is granted. The probe grant is a version 1
    key only: from version 2 probing needs no grant (types.py `probe_allowed`,
    `READ_FREE_CONFIG_VERSION`), so naming it unqualified would send a
    caller to a key its file refuses."""
    tool = listed[START]
    definition = definition_text(tool)
    segments = mode_segments(property_texts(tool)["mode"])

    assert not re.search(r"\ballow_(reset|flash)\b", segments["attach"]), segments["attach"]
    assert "allow_reset" in segments["reset_halt"], segments["reset_halt"]
    assert "allow_flash" not in segments["reset_halt"], segments["reset_halt"]
    assert "allow_reset" in segments["load"], segments["load"]
    assert "allow_flash" in segments["load"], segments["load"]

    # Mass erase refuses `load` rather than enabling it.
    assert "allow_mass_erase" in segments["load"], segments["load"]
    assert re.search(r"\b(not|never|without|unless|refused|refuses|denied)\b[^.;]*\ballow_mass_erase\b|\ballow_mass_erase" + OFF_AFTER, segments["load"]), segments["load"]
    assert not re.search(r"\b(needs?|requires?)\s+(?:allow_\w+(?:\s*,\s*(?:and\s+)?|\s+and\s+))*allow_mass_erase\b", segments["load"]), segments["load"]
    assert "allow_mass_erase" not in segments["attach"] + segments["reset_halt"], segments

    # Raw debugger commands refuse every mode.
    raw = containing(clauses(definition), r"\ballow_raw_debugger_commands\b")
    assert raw, definition
    for clause in raw:
        before = clause.split("allow_raw_debugger_commands", 1)[0]
        assert re.search(r"\ballow_raw_debugger_commands" + OFF_AFTER, clause) or REFUSED_OR_OFF_BEFORE.search(before), clause

    for clause in containing(clauses(definition), r"\ballow_probe\b"):
        assert re.search(r"\b(version 1|legacy)\b", clause, re.IGNORECASE), clause


def test_start_mode_says_what_each_mode_does_to_the_board(listed: dict[str, dict]) -> None:
    """openocd.py `_debug_server_args` (`init; halt` for attach, `init; reset
    halt` otherwise) and gdbdebug.py `_initialize_gdb` (a reset before and
    after `-target-download` for load)."""
    mode = property_texts(listed[START])["mode"]
    segments = mode_segments(mode)

    assert re.search(r"\bdefault\b", segments["attach"]), segments["attach"]
    assert not re.search(r"\bdefault\b", segments["reset_halt"] + segments["load"]), segments
    assert re.search(r"\bhalts?\b", segments["attach"]), segments["attach"]
    assert re.search(r"\b(no|without|not|never)\b( a)? reset", segments["attach"]), segments["attach"]
    assert not re.search(r"\b(flash|program)", segments["attach"], re.IGNORECASE), segments["attach"]

    assert re.search(r"\breset", segments["reset_halt"]), segments["reset_halt"]
    assert re.search(r"\bhalts?\b", segments["reset_halt"]), segments["reset_halt"]
    assert not re.search(r"\b(flash|program)", segments["reset_halt"], re.IGNORECASE), segments["reset_halt"]

    assert re.search(r"\b(flash|program)", segments["load"], re.IGNORECASE), segments["load"]
    assert re.search(r"\breset", segments["load"]), segments["load"]
    assert re.search(r"\bhalts?\b", segments["load"]), segments["load"]


def test_start_artifact_properties_say_which_elf_is_taken_and_that_only_one_is_given(listed: dict[str, dict]) -> None:
    """tools.py `debug_start_session`: exactly one of the two, an `.elf` only;
    artifacts.py `validate_local_path`: inside the workspace and, by default,
    the configured artifact roots; `artifact_id` names what `artifact_upload`
    stored."""
    described = property_texts(listed[START])
    image_path = described["image_path"]
    artifact_id = described["artifact_id"]

    assert re.search(r"\belf\b", image_path, re.IGNORECASE), image_path
    assert re.search(r"\b(inside|within|in|under)\b[^.;]*\bworkspace\b", image_path, re.IGNORECASE), image_path
    assert not re.search(r"(?<!never )(?<!not )\boutside\b", image_path, re.IGNORECASE), image_path
    assert re.search(r"\belf\b", artifact_id, re.IGNORECASE), artifact_id
    assert "artifact_upload" in artifact_id, artifact_id

    # Each names the other, and that only one of them is given.
    for other, text in (("artifact_id", image_path), ("image_path", artifact_id)):
        assert other in text, (other, text)
        assert re.search(r"\b(not both|exactly one|only one|one of|instead of|either)\b", text, re.IGNORECASE), text
        assert not re.search(r"\b(give|provide|pass|use|set)\s+both\b", text, re.IGNORECASE), text


def test_start_timeout_names_its_unit_default_ceiling_floor_and_what_it_bounds(listed: dict[str, dict]) -> None:
    """`min(debugger.timeout_s, max(0.1, timeout_s))`, the debugger entry's own
    `timeout_s` when omitted, spent whole on the debug server getting ready and
    on each GDB startup command, each of those capped at 10 s; running out
    answers `timeout` (gdbdebug.py `start_session`, `_initialize_gdb`,
    `_gdb_failure`, `_start_failure`)."""
    text = property_texts(listed[START])["timeout_s"]
    units = clauses(text)

    assert SECONDS.search(text), text
    default = containing(units, r"\bdefault\b", re.IGNORECASE)
    assert default, text
    assert all(re.search(r"\bdebugger\b", clause) and CONFIGURED_DEBUGGER_TIMEOUT_DEFAULT in clause for clause in default), default
    assert not any(re.search(rf"\b({int(GDB_COMMAND_TIMEOUT_CAP_S)}|{int(STOP_SESSION_TIMEOUT_CAP_S)})\b", clause) for clause in default), default
    ceiling = [clause for clause in units if CEILING.search(clause) and re.search(r"\bdebugger\b", clause)]
    assert ceiling, text
    assert any(TIMEOUT_FLOOR in clause and FLOOR.search(clause) for clause in units), text
    assert re.search(r"\bdebug server\b", text, re.IGNORECASE), text
    assert re.search(rf"\bGDB\b[^.;]{{0,40}}\b{int(GDB_COMMAND_TIMEOUT_CAP_S)} ?(s|seconds)\b", text), text
    assert any(re.search(r"\b(each|per|every)\b", clause) for clause in containing(units, r"\bGDB\b")), text
    assert any(re.search(r"\btimeout\b", clause) and re.search(r"\b(expir\w*|runs? out|exceed\w*|elapses?|after|late)\b", clause, re.IGNORECASE) for clause in units), text
    assert not WHOLE_CALL.search(text), text


def test_stop_says_what_it_leaves_on_the_board_and_how_it_differs_from_halting(listed: dict[str, dict]) -> None:
    """On success, in this order: the halt is confirmed, the resume a
    disconnect would trigger is turned off, GDB and the debug server end,
    the session and its breakpoints go, and the debugger lease is released
    (gdbdebug.py `stop_session` L453, L454, L483; `_cleanup_session` L1344,
    L1350; tools.py `_coordinated_debug_call` L2414)."""
    description = str(listed[STOP]["description"])
    steps = pieces(description)

    assert START in description, description
    # Distinguished from the in-session halt, which keeps the session open.
    halt = containing(clauses(description), r"\bdebug_halt\b")
    assert halt and all(re.search(r"\b(keeps?|open|inside|within|only|without ending|does not end)\b", clause) for clause in halt), halt
    assert not re.search(r"\bdebug_halt\s+(also\s+)?(ends?|closes?|stops?)\s+(the\s+|a\s+|it)", description), description

    # The core is left halted, never resumed.
    assert any(re.search(r"\b(confirms?|leaves?|keeps?|stays?|remains?)\b[^,.;]*\bhalted\b", step, re.IGNORECASE) for step in steps), steps
    resume = containing(steps, r"\bresum", re.IGNORECASE)
    assert resume, steps
    assert all(NEGATED.search(step) for step in resume), resume

    # GDB and the debug server end.
    processes = containing(steps, r"\bGDB\b|\bdebug server\b")
    assert any(re.search(r"\bGDB\b", step) for step in processes), processes
    assert any(re.search(r"\bdebug server\b", step) for step in processes), processes
    assert all(ENDS.search(step) for step in processes if not re.search(r"\bresum", step, re.IGNORECASE)), processes
    assert not any(STARTS.search(step) for step in processes), processes

    # The session's breakpoints end with it.
    breakpoints = containing(steps, r"\bbreakpoints?\b", re.IGNORECASE)
    assert breakpoints and all(DROPPED.search(step) for step in breakpoints), breakpoints
    assert not any(KEPT.search(step) for step in breakpoints), breakpoints

    # The debugger is given back to the one-shot tools a session holds it against.
    one_shot = containing(steps, r"\b(flash_firmware|reset_target)\b")
    assert "flash_firmware" in " ".join(one_shot) and "reset_target" in " ".join(one_shot), one_shot
    assert all(FREED.search(step) for step in one_shot), one_shot
    assert not any(STILL_BLOCKED.search(step) for step in one_shot), one_shot

    # All of the above is what `safe_state_confirmed: true` reports.
    success = containing(clauses(description), r"\bsafe_state_confirmed\b")
    assert any(re.search(r"\bsafe_state_confirmed\W{0,3}true\b", clause) and re.search(r"\b(success|succeeds|ok)\b", clause, re.IGNORECASE) for clause in success), success
    assert not any(re.search(r"\bsafe_state_confirmed\W{0,3}false\b", clause) for clause in success), success


def test_stop_names_its_result_fields_and_outcomes(listed: dict[str, dict]) -> None:
    """No session: ok, `active` false (gdbdebug.py L417-418). A teardown it
    could not prove: ok false, `status` cleanup_required, `hardware_state`
    unknown, the bench quarantined, with `halt_not_confirmed`,
    `detach_resume_not_confirmed` or `cleanup_failed` (gdbdebug.py L460,
    L476; tools.py L2421)."""
    tool = listed[STOP]
    description = str(tool["description"])
    definition = definition_text(tool)

    no_session = [sentence for sentence in description_sentences(description) if NO_SESSION.search(sentence)]
    assert no_session, description
    assert any(re.search(r"\bok\b", sentence) and re.search(r"\bactive\W{0,3}false\b", sentence) for sentence in no_session), no_session
    assert not any(re.search(r"\bok\W{0,3}false\b|\bactive\W{0,3}true\b", sentence) for sentence in no_session), no_session

    unproven = containing(description_sentences(description), r"\bcleanup_required\b")
    assert unproven, description
    for sentence in unproven:
        assert re.search(r"\bok\W{0,3}false\b", sentence), sentence
        assert re.search(r"\bunknown\b", sentence), sentence
        assert HELD.search(sentence), sentence
        assert not re.search(r"\bok\W{0,3}true\b|\bactive\W{0,3}false\b|\bsafe_state_confirmed\W{0,3}true\b", sentence), sentence

    for outcome in ("halt_not_confirmed", "detach_resume_not_confirmed", "cleanup_failed"):
        assert outcome in definition, (outcome, definition)


def test_stop_timeout_names_its_unit_default_ceiling_floor_and_that_it_bounds_each_step(listed: dict[str, dict]) -> None:
    """`min(debugger.timeout_s, 5)` when omitted, `min(that, max(0.1,
    timeout_s))` when given, spent on each teardown step in turn (gdbdebug.py
    `stop_session`)."""
    text = property_texts(listed[STOP])["timeout_s"]
    units = clauses(text)

    assert SECONDS.search(text), text
    assert re.search(r"\b(each|per|every)\b[^.;]*\bsteps?\b", text, re.IGNORECASE), text
    capped = [clause for clause in units if re.search(rf"\b{int(STOP_SESSION_TIMEOUT_CAP_S)}\b", clause)]
    assert capped, text
    assert all(re.search(r"\b(default|ceiling)\b", clause, re.IGNORECASE) or CEILING.search(clause) for clause in capped), capped
    assert any(re.search(r"\bdebugger\b", clause) and re.search(r"\b(lower|less|smaller|shorter|whichever is lower|minimum of)\b", clause) for clause in capped), capped
    assert not any(re.search(r"\b(higher|greater|longer|larger)\b", clause) for clause in capped), capped
    assert not re.search(rf"\b{CONFIGURED_DEBUGGER_TIMEOUT_DEFAULT}\b", text), text
    assert any(TIMEOUT_FLOOR in clause and FLOOR.search(clause) for clause in units), text
    assert not WHOLE_CALL.search(text), text


@pytest.mark.parametrize("name", LIFECYCLE_TOOLS)
def test_every_identifier_a_lifecycle_definition_names_exists(listed: dict[str, dict], known_literals: set[str], name: str) -> None:
    """Each snake_case word is a listed tool, one of the tool's own arguments
    or enum values, or a string the package source itself produces or reads
    (an error type, a result field, a configuration key)."""
    tool = listed[name]

    assert unknown_identifiers(definition_text(tool), listed, tool, known_literals) == []


def test_the_identifier_check_accepts_real_outcomes_and_rejects_a_misspelled_sibling(listed: dict[str, dict], known_literals: set[str]) -> None:
    tool = listed[START]
    real = "Answers debugger_not_found, debugger_error, gdb_not_found or session_already_active; end with debug_stop_session."

    assert unknown_identifiers(real, listed, tool, known_literals) == []
    assert unknown_identifiers("End with debug_stop_sesion.", listed, tool, known_literals) == ["debug_stop_sesion"]
    assert unknown_identifiers("Read session_staus.", listed, tool, known_literals) == ["session_staus"]


def test_the_annotations_agree_with_what_the_definitions_describe(listed: dict[str, dict]) -> None:
    """Start may program flash (`load`), so it is destructive and not idempotent;
    stop with nothing open answers ok, so repeating it is free."""
    start = listed[START]["annotations"]
    stop = listed[STOP]["annotations"]

    assert (start["readOnlyHint"], start["destructiveHint"], start["idempotentHint"], start["openWorldHint"]) == (False, True, False, False)
    assert (stop["readOnlyHint"], stop["destructiveHint"], stop["idempotentHint"], stop["openWorldHint"]) == (False, False, True, False)


# ---------------------------------------------------------------------------
# The behaviour the definitions describe, through the fake debugger.

def test_a_stop_with_no_session_answers_ok_and_inactive_before_and_after_a_session(tmp_path: Path) -> None:
    service = debug_service(tmp_path)
    try:
        before = service.call(STOP)
        assert start_debug_session(service, mode="attach")["ok"] is True
        stopped = service.call(STOP)
        again = service.call(STOP)
    finally:
        service.close()

    for result in (before, again):
        assert result["ok"] is True, result
        assert result["active"] is False, result
        assert result["status"] == "stopped", result
    assert stopped["ok"] is True, stopped
    assert stopped["active"] is False, stopped
    assert stopped["status"] == "stopped", stopped
    assert stopped["safe_state_confirmed"] is True, stopped
    assert stopped["halt_not_confirmed"] is False, stopped
    assert stopped["detach_resume_guard_confirmed"] is True, stopped


def test_a_stopped_session_takes_its_breakpoints_with_it(tmp_path: Path) -> None:
    service = debug_service(tmp_path)
    try:
        assert start_debug_session(service, mode="attach")["ok"] is True
        assert service.call("debug_set_breakpoint", {"location": {"symbol": "test_done"}})["ok"] is True
        assert len(service.call("debug_list_breakpoints")["breakpoints"]) == 1
        assert service.call(STOP)["ok"] is True

        after_stop = service.call("debug_list_breakpoints")
        status = service.call("debug_get_session_status")
        restarted = start_debug_session(service, mode="attach")
        in_new_session = service.call("debug_list_breakpoints")
        assert service.call(STOP)["ok"] is True
    finally:
        service.close()

    assert after_stop["active"] is False, after_stop
    assert after_stop["breakpoints"] == [], after_stop
    assert status["active"] is False, status
    assert restarted["ok"] is True, restarted
    assert restarted["session"]["breakpoints"] == [], restarted["session"]
    assert in_new_session["breakpoints"] == [], in_new_session


def test_an_open_session_holds_the_debugger_against_flash_and_reset_until_it_stops(tmp_path: Path) -> None:
    service = debug_service(tmp_path)
    try:
        assert start_debug_session(service, mode="attach")["ok"] is True
        reset_during = service.call("reset_target", {"mode": "halt"})
        flash_during = service.call("flash_firmware", {"image_path": "build/app.elf"})
        assert service.call(STOP)["ok"] is True
        reset_after = service.call("reset_target", {"mode": "halt"})
        flash_after = service.call("flash_firmware", {"image_path": "build/app.elf"})
    finally:
        service.close()

    for result in (reset_during, flash_during):
        assert result["ok"] is False, result
        assert result["error_type"] == "resource_busy", result
    for result in (reset_after, flash_after):
        assert result["ok"] is True, result


@pytest.mark.parametrize("mode", MODES)
def test_each_start_mode_does_what_its_description_says_and_returns_the_core_halted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    service = debug_service(tmp_path)
    debug = service.backend._debug
    original = debug._gdb_command
    sent: list[str] = []

    def record(session, command, *args, **kwargs):
        sent.append(command)
        return original(session, command, *args, **kwargs)

    monkeypatch.setattr(debug, "_gdb_command", record)
    try:
        started = start_debug_session(service, mode=mode)
        server_startup = debug.session.server_args[-1] if debug.session is not None else None
        monkeypatch.undo()
        assert service.call(STOP)["ok"] is True
    finally:
        service.close()

    assert started["ok"] is True, started
    assert started["session"]["status"] == "halted", started["session"]
    resets = [index for index, command in enumerate(sent) if "monitor reset" in command]
    downloads = [index for index, command in enumerate(sent) if command == "-target-download"]
    if mode == "attach":
        assert server_startup == "init; halt", server_startup
        assert (resets, downloads) == ([], []), sent
    elif mode == "reset_halt":
        assert server_startup == "init; reset halt", server_startup
        assert len(resets) == 1 and downloads == [], sent
    else:
        assert server_startup == "init; reset halt", server_startup
        assert len(downloads) == 1 and len(resets) == 2, sent
        assert resets[0] < downloads[0] < resets[1], sent


def test_attach_needs_neither_reset_nor_flash_and_mass_erase_refuses_only_load(tmp_path: Path) -> None:
    without_reset_or_flash = {**DEFAULT_TEST_PERMISSIONS, "allow_reset": False, "allow_flash": False}
    service = debug_service(tmp_path / "plain", permissions=without_reset_or_flash)
    try:
        attached = start_debug_session(service, mode="attach")
        assert service.call(STOP)["ok"] is True
    finally:
        service.close()

    with_mass_erase = {**DEFAULT_TEST_PERMISSIONS, "allow_mass_erase": True}
    service = debug_service(tmp_path / "erase", permissions=with_mass_erase)
    try:
        loaded = start_debug_session(service, mode="load")
        attached_with_erase = start_debug_session(service, mode="attach")
        assert service.call(STOP)["ok"] is True
    finally:
        service.close()

    assert attached["ok"] is True, attached
    assert loaded["ok"] is False, loaded
    assert loaded["error_type"] == "permission_denied", loaded
    assert attached_with_erase["ok"] is True, attached_with_erase


def test_a_version_2_configuration_starts_a_session_without_a_probe_grant(tmp_path: Path) -> None:
    """Why the definitions never name `allow_probe` unqualified: the version 2
    file has no such key, and the session starts all the same."""
    service = debug_service(tmp_path, config_version=2)
    try:
        assert "allow_probe" not in (tmp_path / ".agentic-hil" / "config.yaml").read_text(encoding="utf-8")
        started = start_debug_session(service, mode="attach")
        assert service.call(STOP)["ok"] is True
    finally:
        service.close()

    assert started["ok"] is True, started


def test_the_start_takes_only_an_elf_from_inside_the_workspace(tmp_path: Path) -> None:
    service = debug_service(tmp_path)
    (tmp_path / "build" / "app.hex").write_text(":00000001FF\n", encoding="ascii")
    outside = tmp_path.parent / f"{tmp_path.name}-outside" / "app.elf"
    outside.parent.mkdir(parents=True, exist_ok=True)
    outside.write_bytes((tmp_path / "build" / "app.elf").read_bytes())
    try:
        not_an_elf = service.call(START, {"image_path": "build/app.hex", "mode": "attach"})
        not_inside = service.call(START, {"image_path": str(outside), "mode": "attach"})
        both = service.call(START, {"image_path": "build/app.elf", "artifact_id": "app.elf", "mode": "attach"})
    finally:
        service.close()

    assert not_an_elf["ok"] is False, not_an_elf
    assert not_an_elf["error_type"] == "artifact_validation_failed", not_an_elf
    assert not_inside["ok"] is False, not_inside
    assert not_inside["validation"]["within_workspace"] is False, not_inside
    assert both["ok"] is False, both
    assert both["error_type"] == "invalid_argument", both


def pyocd_service(tmp_path: Path) -> AgenticHILToolService:
    config_path = write_config(tmp_path, debugger_type="pyocd", debugger_executable=FAKE_PYOCD, target_type="stm32f446re")
    elf_path = tmp_path / "build" / "app.elf"
    elf_path.parent.mkdir(parents=True, exist_ok=True)
    elf_path.write_bytes(b"\x7fELF" + b"\x00" * 12)
    return AgenticHILToolService(load_config(str(config_path)))


@pytest.mark.parametrize("backend", ["stlink", "pyocd"])
def test_both_lifecycle_calls_answer_not_supported_where_sessions_do_not_exist(tmp_path: Path, backend: str) -> None:
    """STM32CubeProgrammer and pyOCD refuse the stop as well as the start, so
    the "no session answers ok" outcome is an OpenOCD answer only."""
    service = stlink_dump_service(tmp_path) if backend == "stlink" else pyocd_service(tmp_path)
    try:
        started = service.call(START, {"image_path": "build/app.elf"})
        stopped = service.call(STOP)
    finally:
        service.close()

    for result in (started, stopped):
        assert result["ok"] is False, (backend, result)
        assert result["error_type"] == "not_supported", (backend, result)
        assert "OpenOCD" in result["summary"], (backend, result)
        assert result["target_contacted"] is False, (backend, result)


def recording(target: object, attribute: str, seen: list, monkeypatch: pytest.MonkeyPatch, position: int, run_with: float) -> None:
    """Wrap `target.attribute` so the timeout it is asked for, the argument at
    `position`, is recorded, and the fake runs with `run_with` instead: what is
    checked is the bound the code asks for, not how fast the fake answers."""
    original = getattr(target, attribute)

    def wrapper(*args, **kwargs):
        if len(args) > position:
            seen.append(args[position])
            args = (*args[:position], run_with, *args[position + 1:])
        else:
            seen.append(kwargs.get("timeout_s"))
            kwargs["timeout_s"] = run_with
        return original(*args, **kwargs)

    monkeypatch.setattr(target, attribute, wrapper)


@pytest.mark.parametrize(
    ("asked", "expected"),
    [(None, 20.0), (60.0, 20.0), (3.0, 3.0), (0.01, 0.1)],
    ids=["omitted", "above-the-entry", "below-the-entry", "below-the-floor"],
)
def test_the_start_timeout_can_shorten_the_configured_wait_but_never_extend_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, asked: float | None, expected: float) -> None:
    service = debug_service(tmp_path, timeout_s=20)
    debug = service.backend._debug
    readiness: list = []
    commands: list = []
    recording(gdbdebug, "wait_for_ready_line", readiness, monkeypatch, 1, 20.0)
    recording(gdbdebug, "wait_for_tcp_port", readiness, monkeypatch, 1, 20.0)
    recording(debug, "_gdb_command", commands, monkeypatch, 2, GDB_COMMAND_TIMEOUT_CAP_S)
    arguments: dict = {"image_path": "build/app.elf", "mode": "load"}
    if asked is not None:
        arguments["timeout_s"] = asked
    try:
        started = service.call(START, arguments)
        assert started["ok"] is True, started
        monkeypatch.undo()
        assert service.call(STOP)["ok"] is True
    finally:
        service.close()

    # The debug server's readiness wait gets the whole budget.
    assert readiness == [expected], readiness
    # Each GDB startup command gets the budget, never more than the cap.
    assert commands, commands
    assert all(value == min(expected, GDB_COMMAND_TIMEOUT_CAP_S) for value in commands), commands


@pytest.mark.parametrize(
    ("entry", "asked", "expected"),
    [(20, None, 5.0), (20, 30.0, 5.0), (20, 1.0, 1.0), (3, None, 3.0), (20, 0.01, 0.1)],
    ids=["omitted", "above-the-cap", "below-the-cap", "entry-below-the-cap", "below-the-floor"],
)
def test_the_stop_timeout_is_five_seconds_at_most_and_bounds_each_teardown_step(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entry: float, asked: float | None, expected: float) -> None:
    service = debug_service(tmp_path, timeout_s=entry)
    debug = service.backend._debug
    halt: list = []
    guard: list = []
    cleanup: list = []
    try:
        assert start_debug_session(service, mode="attach")["ok"] is True
        recording(debug, "_confirm_halted_before_end", halt, monkeypatch, 1, STOP_SESSION_TIMEOUT_CAP_S)
        recording(debug, "_pin_no_resume_on_detach", guard, monkeypatch, 1, STOP_SESSION_TIMEOUT_CAP_S)
        recording(debug, "_cleanup_session", cleanup, monkeypatch, 1, STOP_SESSION_TIMEOUT_CAP_S)
        stopped = service.call(STOP, {} if asked is None else {"timeout_s": asked})
    finally:
        monkeypatch.undo()
        service.close()

    assert stopped["ok"] is True, stopped
    assert (halt, guard, cleanup) == ([expected], [expected], [expected])
