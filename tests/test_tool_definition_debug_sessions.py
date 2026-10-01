"""What the two debug session lifecycle tools tell a caller before the first call.

`debug_start_session` and `debug_stop_session` open and close the GDB session
every other typed debug tool runs in. A caller reads their definitions, as a
host serves them through `tools/list`, to decide whether a session can exist on
this bench at all, what it must have configured first, what each argument
means, which state the board is in once either call returns, and what each
refusal it can meet means.

The first half of this file reads the served definitions and checks their
meaning, not their wording: that every input property is described, that the
units, defaults and ceilings the code applies are named, that the prerequisites,
result fields and failure outcomes the code produces are named, and that every
sibling tool named is one the server lists. Which backends run sessions is kept
to one sentence in each description, so a change to that support edits one
sentence.

The second half holds the behaviour those definitions describe, through the
fake debugger, wherever no existing test already holds it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from conftest import FAKE_PYOCD, write_config
from test_debug_sessions import debug_service, start_debug_session, stlink_dump_service

from agentic_hil.backends import gdbdebug
from agentic_hil.backends.gdbdebug import GDB_COMMAND_TIMEOUT_CAP_S, STOP_SESSION_TIMEOUT_CAP_S
from agentic_hil.config import load_config
from agentic_hil.mcp import handle_mcp_message
from agentic_hil.tools import AgenticHILToolService

START = "debug_start_session"
STOP = "debug_stop_session"
LIFECYCLE_TOOLS = (START, STOP)
# What the configuration falls back to when a debugger entry names no
# `timeout_s` (config.py, `positive_timeout_config(raw.get("timeout_s"), 60.0, ...)`).
CONFIGURED_DEBUGGER_TIMEOUT_DEFAULT = "60"
SECONDS = re.compile(r"\bseconds?\b|\b\d+(?:\.\d+)? ?s\b", re.IGNORECASE)
CEILING = re.compile(r"\b(ceiling|at most|cannot (exceed|raise|extend)|never (exceeds?|raises?|extends?|above|more)|capped|caps?|no (more|longer) than|not (above|beyond|more than))\b", re.IGNORECASE)
NO_SESSION = re.compile(r"\b(no (active |debug )?session|without (an active |a )?session|nothing (is )?open)\b", re.IGNORECASE)
BACKEND_NAMES = ("OpenOCD", "pyOCD", "STM32CubeProgrammer")


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


def schema_words(tool: dict) -> set[str]:
    """Input property names and enum values: identifiers a definition names
    that are arguments rather than tools."""
    words: set[str] = set()
    for name, schema in tool["inputSchema"].get("properties", {}).items():
        words.add(name)
        words.update(str(value) for value in schema.get("enum", []))
    return words


@pytest.fixture
def listed(tmp_path: Path) -> dict[str, dict]:
    return listed_tools(tmp_path)


def test_both_lifecycle_tools_are_listed_with_every_input_property_described(listed: dict[str, dict]) -> None:
    for name in LIFECYCLE_TOOLS:
        assert name in listed, sorted(listed)
        described = property_texts(listed[name])
        assert described, name
        undescribed = sorted(prop for prop, text in described.items() if not text.strip())
        assert undescribed == [], (name, undescribed)


@pytest.mark.parametrize("name", LIFECYCLE_TOOLS)
def test_backend_support_is_one_sentence_naming_openocd_and_what_the_others_answer(listed: dict[str, dict], name: str) -> None:
    """Typed sessions exist only under OpenOCD on this code; pyOCD and
    STM32CubeProgrammer refuse both calls with `not_supported`. Kept to one
    sentence of the description, and out of the property descriptions, so a
    change to the support edits one place."""
    tool = listed[name]
    description = str(tool["description"])

    naming_a_backend = [sentence for sentence in description_sentences(description) if any(backend in sentence for backend in BACKEND_NAMES)]
    assert len(naming_a_backend) == 1, (name, naming_a_backend, description)
    sentence = naming_a_backend[0]
    assert "OpenOCD" in sentence, sentence
    assert "not_supported" in sentence, sentence

    in_properties = {prop: text for prop, text in property_texts(tool).items() if any(backend in text for backend in BACKEND_NAMES)}
    assert in_properties == {}, in_properties


def test_start_says_what_it_opens_and_how_the_session_is_used_and_ended(listed: dict[str, dict]) -> None:
    description = str(listed[START]["description"])

    assert re.search(r"\bGDB\b", description), description
    assert re.search(r"\bELF\b", description), description
    # The tools that run inside the session it opens, so a caller can tell the
    # lifecycle apart from halting or resuming the core within one.
    for sibling in ("debug_continue", "debug_halt"):
        assert sibling in description, (sibling, description)
    # Inspecting and ending the session.
    assert "debug_get_session_status" in description, description
    assert STOP in description, description


def test_start_says_the_state_it_returns_in_and_what_a_second_start_answers(listed: dict[str, dict]) -> None:
    description = str(listed[START]["description"])

    # Every mode returns with the core halted (gdbdebug.py, start ends with
    # `session.status = "halted"` and "Debug session started and target is halted.").
    assert re.search(r"\bhalted\b", description), description
    # The result field a caller reads to learn where the core stopped.
    assert "stop_reason" in description, description
    # One session at a time.
    assert "session_already_active" in description, description


def test_start_names_the_permissions_each_mode_needs(listed: dict[str, dict]) -> None:
    tool = listed[START]
    definition = definition_text(tool)
    mode = property_texts(tool)["mode"]

    # Every mode needs the probe permission; the two that reset need reset, and
    # the one that programs flash needs flash (gdbdebug.py `_start_permission`).
    assert "allow_probe" in definition, definition
    assert "allow_reset" in mode, mode
    assert "allow_flash" in mode, mode


def test_start_mode_says_what_each_mode_does_to_the_board(listed: dict[str, dict]) -> None:
    mode = property_texts(listed[START])["mode"]

    for value in ("attach", "reset_halt", "load"):
        assert value in mode, (value, mode)
    assert re.search(r"\breset", mode, re.IGNORECASE), mode
    assert re.search(r"\bflash\b", mode, re.IGNORECASE), mode


def test_start_artifact_properties_say_where_the_elf_comes_from(listed: dict[str, dict]) -> None:
    described = property_texts(listed[START])

    assert re.search(r"\bworkspace\b", described["image_path"], re.IGNORECASE), described["image_path"]
    assert "artifact_upload" in described["artifact_id"], described["artifact_id"]


def test_start_timeout_names_its_unit_default_ceiling_and_what_it_bounds(listed: dict[str, dict]) -> None:
    """`min(debugger.timeout_s, max(0.1, timeout_s))`, the debugger entry's own
    `timeout_s` when omitted, spent on the debug server getting ready and on each
    GDB startup command, each of those capped at 10 s (gdbdebug.py)."""
    text = property_texts(listed[START])["timeout_s"]

    assert SECONDS.search(text), text
    assert re.search(r"\bdebugger\b", text, re.IGNORECASE), text
    assert CONFIGURED_DEBUGGER_TIMEOUT_DEFAULT in text, text
    assert CEILING.search(text), text
    assert re.search(r"\bdebug server\b", text, re.IGNORECASE), text
    assert re.search(r"\bGDB\b", text), text
    assert re.search(rf"\b{int(GDB_COMMAND_TIMEOUT_CAP_S)} ?s\b|\b{int(GDB_COMMAND_TIMEOUT_CAP_S)} seconds\b", text), text
    # What an expiry answers.
    assert re.search(r"\btimeout\b", text), text


def test_stop_says_what_it_leaves_on_the_board_and_how_it_differs_from_halting(listed: dict[str, dict]) -> None:
    description = str(listed[STOP]["description"])

    assert START in description, description
    # Distinguished from the in-session halt, which keeps the session open.
    assert "debug_halt" in description, description
    # The core is left halted rather than resumed: the halt is confirmed and
    # the resume a disconnect would trigger is turned off before GDB and the
    # debug server end (gdbdebug.py `stop_session`, `_pin_no_resume_on_detach`).
    assert re.search(r"\bhalted\b", description), description
    assert re.search(r"\bresum", description, re.IGNORECASE), description
    assert re.search(r"\bGDB\b", description), description
    assert re.search(r"\bdebug server\b", description, re.IGNORECASE), description
    # The session's breakpoints end with it.
    assert re.search(r"\bbreakpoints?\b", description, re.IGNORECASE), description
    # What the stop gives back: the debugger, for the one-shot tools a session
    # holds it against.
    for sibling in ("flash_firmware", "reset_target"):
        assert sibling in description, (sibling, description)


def test_stop_names_its_result_fields_and_outcomes(listed: dict[str, dict]) -> None:
    description = str(listed[STOP]["description"])

    # Success.
    assert "safe_state_confirmed" in description, description
    # No session: ok, with `active` false, so repeating it is free.
    no_session = [sentence for sentence in description_sentences(description) if NO_SESSION.search(sentence)]
    assert no_session, description
    assert any(re.search(r"\bok\b", sentence) and re.search(r"\bactive\b", sentence) and re.search(r"\bfalse\b", sentence) for sentence in no_session), no_session
    # A teardown it could not prove.
    assert "cleanup_required" in description, description


def test_stop_timeout_names_its_unit_default_ceiling_and_that_it_bounds_each_step(listed: dict[str, dict]) -> None:
    """`min(debugger.timeout_s, 5)` when omitted, `min(that, max(0.1, timeout_s))`
    when given, spent on each teardown step in turn (gdbdebug.py `stop_session`)."""
    text = property_texts(listed[STOP])["timeout_s"]

    assert SECONDS.search(text), text
    assert re.search(rf"\b{int(STOP_SESSION_TIMEOUT_CAP_S)}\b", text), text
    assert CEILING.search(text), text
    assert re.search(r"\bdebugger\b", text, re.IGNORECASE), text
    assert re.search(r"\beach\b", text, re.IGNORECASE), text
    assert re.search(r"\bhalt", text, re.IGNORECASE), text


@pytest.mark.parametrize("name", LIFECYCLE_TOOLS)
def test_every_tool_a_lifecycle_definition_names_is_listed(listed: dict[str, dict], name: str) -> None:
    tool = listed[name]
    prefixes = {tool_name.split("_", 1)[0] for tool_name in listed}
    named = set(re.findall(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b", definition_text(tool)))
    tool_shaped = {word for word in named if word.split("_", 1)[0] in prefixes} - schema_words(tool)

    unknown = sorted(tool_shaped - set(listed))
    assert unknown == [], unknown


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
    finally:
        service.close()

    for result in (reset_during, flash_during):
        assert result["ok"] is False, result
        assert result["error_type"] == "resource_busy", result
    assert reset_after["ok"] is True, reset_after


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


def recording(target: object, attribute: str, seen: list, monkeypatch: pytest.MonkeyPatch, position: int) -> None:
    """Wrap `target.attribute` so the argument at `position` (after self) is recorded."""
    original = getattr(target, attribute)

    def wrapper(*args, **kwargs):
        value = args[position] if len(args) > position else kwargs.get("timeout_s")
        seen.append(value)
        return original(*args, **kwargs)

    monkeypatch.setattr(target, attribute, wrapper)


@pytest.mark.parametrize(("asked", "expected"), [(None, 20.0), (60.0, 20.0), (3.0, 3.0)], ids=["omitted", "above-the-entry", "below-the-entry"])
def test_the_start_timeout_can_shorten_the_configured_wait_but_never_extend_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, asked: float | None, expected: float) -> None:
    service = debug_service(tmp_path, timeout_s=20)
    debug = service.backend._debug
    readiness: list = []
    commands: list = []
    recording(gdbdebug, "wait_for_ready_line", readiness, monkeypatch, 1)
    recording(gdbdebug, "wait_for_tcp_port", readiness, monkeypatch, 1)
    recording(debug, "_gdb_command", commands, monkeypatch, 2)
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
    [(20, None, 5.0), (20, 30.0, 5.0), (20, 1.0, 1.0), (3, None, 3.0)],
    ids=["omitted", "above-the-cap", "below-the-cap", "entry-below-the-cap"],
)
def test_the_stop_timeout_is_five_seconds_at_most_and_bounds_each_teardown_step(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entry: float, asked: float | None, expected: float) -> None:
    service = debug_service(tmp_path, timeout_s=entry)
    debug = service.backend._debug
    halt: list = []
    guard: list = []
    cleanup: list = []
    try:
        assert start_debug_session(service, mode="attach")["ok"] is True
        recording(debug, "_confirm_halted_before_end", halt, monkeypatch, 1)
        recording(debug, "_pin_no_resume_on_detach", guard, monkeypatch, 1)
        recording(debug, "_cleanup_session", cleanup, monkeypatch, 1)
        stopped = service.call(STOP, {} if asked is None else {"timeout_s": asked})
    finally:
        monkeypatch.undo()
        service.close()

    assert stopped["ok"] is True, stopped
    assert (halt, guard, cleanup) == ([expected], [expected], [expected])
