"""What `tools/list` tells an agent about `debug_symbol_info` (#627).

The definition is read the way a host reads it: through a `tools/list` request
the server answers. Every claim it has to carry is paired with the behaviour
that makes it true, produced through `tools/call` against the fake debuggers,
so a definition that names an outcome the code no longer gives fails here as
surely as one that leaves an outcome out. The checks are about meaning (a
field, a code, a configuration key, a sibling tool is named), never about the
sentence that names it.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path

import pytest
from test_debug_sessions import (
    debug_service,
    flash_symbol_source,
    pyocd_read_service,
    start_debug_session,
    stlink_dump_service,
)
from test_mcp_envelope import TOOLS_LIST, real_service, tools_call

from agentic_hil.backends.gdbdebug import DEBUG_SYMBOL_PATTERN
from agentic_hil.contracts import SYMBOL_NAME, validate_tool_arguments
from agentic_hil.mcp import handle_mcp_message
from agentic_hil.tools import AgenticHILToolService

TOOL = "debug_symbol_info"
# The fields of a resolution an agent acts on, on every backend.
RESULT_FIELDS = ("address", "size_bytes", "resolved_from")
# The two tools that read the symbol's bytes, which this one does not.
MEMORY_READING_SIBLINGS = ("debug_symbol_value", "debug_dump_symbol_ihex")
# The two configuration keys that decide whether a symbol is allowed.
SYMBOL_POLICY_KEYS = ("debug.allowed_symbols", "debug.allow_all_symbols")


@pytest.fixture
def listed(tmp_path: Path) -> dict[str, dict]:
    """Every listed tool by name, as a `tools/list` response carries it."""
    service = real_service(tmp_path)
    try:
        response = handle_mcp_message(TOOLS_LIST, service)
    finally:
        service.close()
    assert isinstance(response, dict), response
    return {tool["name"]: tool for tool in response["result"]["tools"]}


def tool_text(listed: dict[str, dict]) -> str:
    return listed[TOOL]["description"]


def symbol_property(listed: dict[str, dict]) -> dict:
    return listed[TOOL]["inputSchema"]["properties"]["symbol"]


def symbol_text(listed: dict[str, dict]) -> str:
    return str(symbol_property(listed).get("description", ""))


def definition_text(listed: dict[str, dict]) -> str:
    """Everything a host shows an agent about this tool in words."""
    return tool_text(listed) + "\n" + symbol_text(listed)


def names(text: str, term: str) -> bool:
    return re.search(rf"(?<![\w.]){re.escape(term)}(?![\w])", text) is not None


def call(service: AgenticHILToolService, symbol: str) -> dict:
    """A `debug_symbol_info` call the way a host makes it, answered as it sees it."""
    response = handle_mcp_message(tools_call(7, TOOL, {"symbol": symbol}), service)
    assert isinstance(response, dict), response
    return response["result"]


# --- the symbol parameter ----------------------------------------------------


def test_the_symbol_parameter_carries_its_own_description(listed: dict[str, dict]) -> None:
    assert symbol_text(listed).strip(), symbol_property(listed)


def test_the_symbol_description_names_the_syntax_the_schema_enforces(listed: dict[str, dict]) -> None:
    """An identifier, optionally `::`-qualified: what the listed pattern accepts."""
    text = symbol_text(listed)
    assert re.search(r"\bidentifier", text, re.IGNORECASE), text
    assert "::" in text, text


def test_the_symbol_description_gives_an_example_the_schema_accepts(listed: dict[str, dict]) -> None:
    text = symbol_text(listed)
    found = re.search(r"(?:e\.g\.|for example|such as|example)[:,]?\s*`?([A-Za-z_][A-Za-z0-9_]*(?:::[A-Za-z_][A-Za-z0-9_]*)*)", text, re.IGNORECASE)
    assert found is not None, text
    example = found.group(1)
    assert validate_tool_arguments(TOOL, {"symbol": example}) is None, example
    assert DEBUG_SYMBOL_PATTERN.match(example) is not None, example


def test_the_listed_schema_keeps_the_syntax_the_backends_enforce(listed: dict[str, dict]) -> None:
    """The description is added beside the constraint, never in place of it."""
    schema = listed[TOOL]["inputSchema"]
    prop = symbol_property(listed)
    assert prop["type"] == "string"
    assert prop["pattern"] == SYMBOL_NAME["pattern"] == DEBUG_SYMBOL_PATTERN.pattern
    assert schema["required"] == ["symbol"]
    assert schema["additionalProperties"] is False


# --- "allowed": the project's symbol policy ----------------------------------


def test_the_definition_says_what_the_operator_configures_to_allow_a_symbol(listed: dict[str, dict]) -> None:
    text = definition_text(listed)
    for key in SYMBOL_POLICY_KEYS:
        assert names(text, key), (key, text)
    assert names(text, "permission_denied"), text


@pytest.mark.parametrize(
    ("policy", "allowed"),
    [
        ({"allowed_symbols": ["boot_counter"], "allow_all_symbols": False}, True),
        ({"allowed_symbols": [], "allow_all_symbols": True}, True),
        ({"allowed_symbols": ["CTC_array"], "allow_all_symbols": False}, False),
        ({"allowed_symbols": [], "allow_all_symbols": False}, False),
    ],
    ids=["listed", "allow-all", "not-listed", "nothing-allowed"],
)
def test_the_two_named_keys_are_what_decide_whether_a_symbol_is_allowed(tmp_path: Path, policy: dict, allowed: bool) -> None:
    service = stlink_dump_service(tmp_path, **policy)
    try:
        assert flash_symbol_source(service)["ok"] is True
        result = call(service, "boot_counter")
    finally:
        service.close()

    answer = result["structuredContent"]
    if allowed:
        assert answer["ok"] is True, answer
    else:
        assert answer["ok"] is False, answer
        assert answer["error_type"] == "permission_denied", answer
        assert "address" not in answer, answer


# --- what must come first ----------------------------------------------------


def test_the_definition_names_what_must_come_before_the_call(listed: dict[str, dict]) -> None:
    """OpenOCD answers through the session; the other backends from the flashed ELF."""
    text = tool_text(listed)
    assert re.search(r"OpenOCD[^.]*\bdebug_start_session\b", text), text
    assert re.search(r"\bELF\b[^.]*\bflash_firmware\b|\bflash_firmware\b[^.]*\bELF\b", text), text
    for tool in ("debug_start_session", "flash_firmware"):
        assert tool in listed, tool


# --- what it answers ---------------------------------------------------------


def test_the_definition_names_the_result_fields(listed: dict[str, dict]) -> None:
    text = tool_text(listed)
    for field in RESULT_FIELDS:
        assert names(text, field), (field, text)
    assert re.search(r"\baddress\b[^.;]{0,40}\bhex\b|\bhex\b[^.;]{0,20}\baddress\b", text, re.IGNORECASE), text


def openocd_session(tmp_path: Path) -> AgenticHILToolService:
    service = debug_service(tmp_path)
    assert start_debug_session(service)["ok"] is True
    return service


def stlink_flashed(tmp_path: Path) -> AgenticHILToolService:
    service = stlink_dump_service(tmp_path)
    assert flash_symbol_source(service)["ok"] is True
    return service


def pyocd_flashed(tmp_path: Path) -> AgenticHILToolService:
    service = pyocd_read_service(tmp_path)
    assert flash_symbol_source(service)["ok"] is True
    return service


@pytest.mark.parametrize(
    "prepare",
    [openocd_session, stlink_flashed, pyocd_flashed],
    ids=["openocd-session", "stlink-flashed-elf", "pyocd-flashed-elf"],
)
def test_every_backend_answers_with_the_named_fields(tmp_path: Path, prepare: Callable[[Path], AgenticHILToolService]) -> None:
    service = prepare(tmp_path)
    try:
        result = call(service, "boot_counter")
    finally:
        service.close()

    answer = result["structuredContent"]
    assert result["isError"] is False, answer
    for field in RESULT_FIELDS:
        assert field in answer, (field, answer)
    assert answer["address"].startswith("0x"), answer
    int(answer["address"], 16)
    assert isinstance(answer["size_bytes"], int), answer
    assert answer["resolved_from"] in {"debug_info", "elf_symbol_table"}, answer


# --- how it differs from the two reads ---------------------------------------


def test_the_definition_tells_it_apart_from_the_tools_that_read_memory(listed: dict[str, dict]) -> None:
    text = tool_text(listed)
    for sibling in MEMORY_READING_SIBLINGS:
        assert names(text, sibling), (sibling, text)
        assert sibling in listed, sibling
    assert re.search(r"\b(?:no|not|never|without)\b[^.]*\bmemory\b", text, re.IGNORECASE), text


@pytest.mark.parametrize("prepare", [stlink_flashed, pyocd_flashed], ids=["stlink-flashed-elf", "pyocd-flashed-elf"])
def test_a_resolution_without_a_session_contacts_no_target(tmp_path: Path, prepare: Callable[[Path], AgenticHILToolService]) -> None:
    service = prepare(tmp_path)
    try:
        answer = call(service, "boot_counter")["structuredContent"]
    finally:
        service.close()

    assert answer["ok"] is True, answer
    assert answer["target_contacted"] is False, answer
    assert answer["hardware_state"] == "unchanged", answer


# --- how it fails ------------------------------------------------------------


def openocd_allowlist_without_the_symbol(tmp_path: Path) -> tuple[AgenticHILToolService, str]:
    service = debug_service(tmp_path, allowed_symbols=["CTC_array"])
    assert start_debug_session(service)["ok"] is True
    return service, "test_done"


def openocd_without_a_session(tmp_path: Path) -> tuple[AgenticHILToolService, str]:
    return debug_service(tmp_path), "CTC_array"


def openocd_unknown_symbol(tmp_path: Path) -> tuple[AgenticHILToolService, str]:
    return openocd_session(tmp_path), "missing_symbol"


def stlink_nothing_flashed(tmp_path: Path) -> tuple[AgenticHILToolService, str]:
    return stlink_dump_service(tmp_path), "boot_counter"


def pyocd_nothing_flashed(tmp_path: Path) -> tuple[AgenticHILToolService, str]:
    return pyocd_read_service(tmp_path), "boot_counter"


@pytest.mark.parametrize(
    ("prepare", "error_type"),
    [
        (openocd_allowlist_without_the_symbol, "permission_denied"),
        (openocd_without_a_session, "session_not_active"),
        (openocd_unknown_symbol, "symbol_not_found"),
        (stlink_nothing_flashed, "symbol_source_not_available"),
        (pyocd_nothing_flashed, "symbol_source_not_available"),
    ],
    ids=["not-allowed", "no-session", "unknown-symbol", "stlink-no-flashed-elf", "pyocd-no-flashed-elf"],
)
def test_the_definition_names_each_refusal_the_code_gives(
    tmp_path: Path, listed: dict[str, dict], prepare: Callable[[Path], tuple[AgenticHILToolService, str]], error_type: str
) -> None:
    service, symbol = prepare(tmp_path / "bench")
    try:
        result = call(service, symbol)
    finally:
        service.close()

    answer = result["structuredContent"]
    assert result["isError"] is True, answer
    assert answer["error_type"] == error_type, answer
    assert names(definition_text(listed), error_type), (error_type, definition_text(listed))


# --- the annotations stay true -----------------------------------------------


def test_the_annotations_still_describe_a_read_that_stays_on_this_bench(listed: dict[str, dict]) -> None:
    annotations = listed[TOOL]["annotations"]
    assert annotations["readOnlyHint"] is True
    assert annotations["openWorldHint"] is False
    assert annotations.get("destructiveHint") is not True
