"""The text block of a `tools/call` result is a compact projection of the result.

A result goes out twice: whole as `structuredContent`, and as the JSON text an
agent host puts into the model's context, where it stays for the rest of the
session and is paid for again on every later request. The text leaves out keys
whose value is empty, the nine top-level fields that only restate their default,
and advice the same server session was already sent. `structuredContent` stays
whole and `isError` is computed as it always was, so every test here also holds
the structured half to the result the tool service answered.

The results are built, so each rule is pinned on its own, and the stdio loop
serves them: one loop is one MCP session, which is what the advice memory
belongs to.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from conftest import DEFAULT_TEST_PERMISSIONS, write_config
from result_text import assert_text_projects, text_document
from test_mcp_envelope import RecordingService, lines, serve, tools_call

from agentic_hil.config import load_config
from agentic_hil.contracts import invalid_argument
from agentic_hil.knowledge import (
    ERROR_CATALOGUE,
    PERMISSION_KEY_PLACEHOLDER,
    attach_quarantine_guidance,
    exclusive_permission_fields,
    exclusive_permission_summary,
    permission_denied_fields,
    permission_denied_summary,
    permission_key,
    remediation_fields,
)
from agentic_hil.mcp import handle_mcp_message

ERRORS = "agentic-hil://reference/errors/"
FLASH_PERMISSION = permission_key("debuggers", "dut", "allow_flash")

# Exactly the top-level pairs the text leaves out as defaults.
DEFAULT_PAIRS = {
    "side_effect_committed": False,
    "side_effect_status": "not_started",
    "hardware_state": "unchanged",
    "cleanup_required": False,
    "quarantined": False,
    "audit_ok": True,
    "cleanup_ok": True,
    "target_ok": True,
    "config_stale": False,
}


class ScriptedService(RecordingService):
    """A tool service that answers each call with a copy of the next result it was built with."""

    def __init__(self, *answers: dict) -> None:
        super().__init__()
        self.answers = list(answers)

    def call(self, name: str, arguments: dict | None = None) -> dict:
        self.calls.append((name, arguments))
        return copy.deepcopy(self.answers.pop(0))


def one_session(*answers: dict) -> list[dict]:
    """One server session whose tool service answers each call with the next of ``answers``.

    Every call is made in the same stdio loop, so they share whatever the server
    remembers. Hands back each call's result."""
    service = ScriptedService(*answers)
    requests = [tools_call(index, answer["tool"], {}) for index, answer in enumerate(answers, start=1)]
    exit_code, replies = serve(service, lines(*requests))
    assert exit_code == 0
    assert [reply["id"] for reply in replies] == [request["id"] for request in requests], replies
    return [reply["result"] for reply in replies]


def without(document: dict, *keys: str) -> dict:
    return {key: value for key, value in document.items() if key not in keys}


def served_entry(uri: str) -> dict:
    """The catalogue entry `resources/read` answers for ``uri``, parsed."""
    response = handle_mcp_message({"jsonrpc": "2.0", "id": 1, "method": "resources/read", "params": {"uri": uri}}, RecordingService())  # type: ignore[arg-type]
    assert isinstance(response, dict) and "result" in response, response
    return json.loads(response["result"]["contents"][0]["text"])


def permission_refusal() -> dict:
    """A permission refusal as a backend builds one: the key, the next step and the catalogue's advice for it."""
    return {
        "ok": False,
        "tool": "flash_firmware",
        "error_type": "permission_denied",
        "summary": permission_denied_summary("Flashing is disabled by the authoritative config.", FLASH_PERMISSION),
        **permission_denied_fields(FLASH_PERMISSION),
        **remediation_fields("permission_denied", permission=FLASH_PERMISSION),
    }


# --- empty values ---------------------------------------------------------------


def test_empty_values_are_left_out_at_every_depth_and_array_elements_are_kept() -> None:
    """A key whose value is null, "", [] or {} is left out of the text, at the
    top level and in nested objects at any depth. Elements inside an array are
    kept as they are, null and empty ones included, because their position
    means something. `0` and `false` are values, not empty ones."""
    built = {
        "ok": True,
        "tool": "com_read",
        "summary": "Read 3 bytes.",
        "port_id": None,
        "backend_error": "",
        "warnings": [],
        "details": {},
        "data": {
            "text": "abc",
            "bytes": 3,
            "decode_error": None,
            "hex": "",
            "chunks": [],
            "meta": {},
            "flags": {"truncated": False, "note": None, "origin": {"port": "dut_uart", "device": None, "label": "", "lines": [], "extra": {}}},
        },
        "positions": [None, "", [], {}, 0, False],
        "count": 0,
        "complete": False,
    }

    (result,) = one_session(built)

    assert result["structuredContent"] == built, result
    assert result["isError"] is False, result
    assert text_document(result) == {
        "ok": True,
        "tool": "com_read",
        "summary": "Read 3 bytes.",
        "data": {"text": "abc", "bytes": 3, "flags": {"truncated": False, "origin": {"port": "dut_uart"}}},
        "positions": [None, "", [], {}, 0, False],
        "count": 0,
        "complete": False,
    }, result
    assert_text_projects(result)


# --- defaults -------------------------------------------------------------------


def test_the_nine_defaults_are_left_out_at_the_top_level_only() -> None:
    """Exactly these top-level pairs only restate a default and are left out:
    side_effect_committed false, side_effect_status "not_started",
    hardware_state "unchanged", cleanup_required false, quarantined false,
    audit_ok, cleanup_ok and target_ok true, and config_stale false. A field not
    on the list keeps its value, `false` included, and the same pairs inside a
    nested object or an array element are kept."""
    built = {
        "ok": True,
        "tool": "flash_firmware",
        "summary": "Flashed.",
        **DEFAULT_PAIRS,
        "target_contacted": False,
        "retry_safe": True,
        "recovery": dict(DEFAULT_PAIRS),
        "attempts": [dict(DEFAULT_PAIRS)],
    }

    (result,) = one_session(built)

    assert result["structuredContent"] == built, result
    assert result["isError"] is False, result
    assert text_document(result) == {
        "ok": True,
        "tool": "flash_firmware",
        "summary": "Flashed.",
        "target_contacted": False,
        "retry_safe": True,
        "recovery": DEFAULT_PAIRS,
        "attempts": [DEFAULT_PAIRS],
    }, result
    assert_text_projects(result)


@pytest.mark.parametrize(
    ("field", "value", "is_error"),
    [
        ("side_effect_committed", True, False),
        ("side_effect_status", "committed", False),
        ("side_effect_status", "unknown", True),
        ("side_effect_status", "partial", True),
        ("hardware_state", "unknown", True),
        ("cleanup_required", True, True),
        ("quarantined", True, True),
        ("audit_ok", False, True),
        ("cleanup_ok", False, True),
        ("target_ok", False, True),
        ("config_stale", True, False),
    ],
)
def test_a_listed_field_with_any_other_value_is_kept(field: str, value: object, is_error: bool) -> None:
    """A listed field holding anything but its default says something, and is
    kept while the defaults beside it are left out. `isError` is what it always
    was for that value."""
    built = {"ok": True, "tool": "flash_firmware", "summary": "Flashed.", **DEFAULT_PAIRS, field: value}

    (result,) = one_session(built)

    assert result["structuredContent"] == built, result
    assert result["isError"] is is_error, result
    assert text_document(result) == {"ok": True, "tool": "flash_firmware", "summary": "Flashed.", field: value}, result
    assert_text_projects(result)


@pytest.mark.parametrize("ok", [True, False])
@pytest.mark.parametrize("retry_safe", [True, False])
def test_ok_and_retry_safe_are_kept_whatever_their_value(ok: bool, retry_safe: bool) -> None:
    """`ok` says whether the call worked and `retry_safe` whether it may be sent
    again. Both are kept whatever they hold, beside a default and a null that
    are not."""
    built = {"ok": ok, "tool": "reset_target", "summary": "Reset.", "retry_safe": retry_safe, "side_effect_committed": False, "backend_error": None}

    (result,) = one_session(built)

    assert result["structuredContent"] == built, result
    assert result["isError"] is (not ok), result
    assert text_document(result) == {"ok": ok, "tool": "reset_target", "summary": "Reset.", "retry_safe": retry_safe}, result
    assert_text_projects(result)


def test_tool_is_kept_even_when_it_is_empty() -> None:
    """The text always keeps `ok` and `tool`. A tools/call whose name is the
    empty string is answered for tool "", and that "" is kept where any other
    empty string is left out."""
    built = {"ok": False, "tool": "", "error_type": "unknown_tool", "summary": "No tool is named ''.", "hint": None}

    (result,) = one_session(built)

    assert result["structuredContent"] == built, result
    assert result["isError"] is True, result
    assert text_document(result) == {"ok": False, "tool": "", "error_type": "unknown_tool", "summary": "No tool is named ''."}, result
    assert_text_projects(result)


# --- advice a session was already sent -------------------------------------------


def test_the_same_refusal_twice_in_one_session_sends_its_advice_once() -> None:
    """The second time one session receives the same permission_denied refusal,
    its remediation entries are left out of the text, `repeated_advice` counts
    them, and `advice_uri` names the catalogue entry `resources/read` serves
    them from. `do_not`, `next_step` and `summary` are never left out, and the
    structured half carries the whole refusal both times."""
    built = permission_refusal()

    first, second = one_session(built, built)

    for result in (first, second):
        assert result["structuredContent"] == built, result
        assert result["isError"] is True, result
    assert text_document(first) == built, first
    repeated = text_document(second)
    assert repeated == {
        **without(built, "remediation"),
        "repeated_advice": {"remediation": len(built["remediation"])},
        "advice_uri": ERRORS + "permission_denied",
    }, second
    assert repeated["do_not"] == built["do_not"], second
    assert repeated["next_step"] == built["next_step"], second
    for result in (first, second):
        assert_text_projects(result)

    entry = served_entry(repeated["advice_uri"])
    assert entry["error_type"] == "permission_denied" and "scope" not in entry, entry
    assert [step.replace(PERMISSION_KEY_PLACEHOLDER, FLASH_PERMISSION) for step in entry["remediation"]] == built["remediation"], entry


def test_a_fresh_session_is_sent_the_advice_again() -> None:
    """What was sent is remembered per session. A second session, with its own
    tool service, has been sent nothing, so the same refusal on its first call
    carries the whole advice again and says nothing about repeats."""
    built = permission_refusal()

    _, repeated = one_session(built, built)
    (fresh,) = one_session(built)

    assert "remediation" not in text_document(repeated), repeated
    assert fresh["structuredContent"] == built, fresh
    assert fresh["isError"] is True, fresh
    assert text_document(fresh) == built, fresh
    assert_text_projects(fresh)


def test_an_entry_changed_by_one_character_is_sent() -> None:
    """Only an identical entry counts as sent. The same refusal with one
    remediation entry differing in its last character sends that entry, and
    leaves out the two the session already has."""
    first = permission_refusal()
    second = permission_refusal()
    changed = second["remediation"][1][:-1] + "!"
    assert changed != first["remediation"][1]
    second["remediation"][1] = changed

    results = one_session(first, second)

    assert [result["structuredContent"] for result in results] == [first, second], results
    assert [result["isError"] for result in results] == [True, True], results
    assert text_document(results[1]) == {
        **without(second, "remediation"),
        "remediation": [changed],
        "repeated_advice": {"remediation": 2},
        "advice_uri": ERRORS + "permission_denied",
    }, results[1]
    for result in results:
        assert_text_projects(result)


def test_advice_left_out_names_the_scoped_entry_it_came_from() -> None:
    """likely_causes are advice as well. An OpenOCD target_not_detected carries
    the backend's likely causes and the remediation of the catalogue entry
    scoped to `openocd`. Sent a second time, both lists are left out and
    counted, and `advice_uri` carries the scope suffix, because the advice came
    from the scoped entry (target_not_detected has no unscoped one)."""
    built = {
        "ok": False,
        "tool": "probe_target",
        "backend": "openocd",
        "error_type": "target_not_detected",
        "summary": "No target answered through OpenOCD.",
        "target_contacted": False,
        "retry_safe": True,
        "likely_causes": ["DUT is not powered", "wrong interface configuration", "SWD/JTAG wiring issue", "debug probe already in use"],
        **remediation_fields("target_not_detected", "openocd"),
    }

    first, second = one_session(built, built)

    for result in (first, second):
        assert result["structuredContent"] == built, result
        assert result["isError"] is True, result
    assert text_document(first) == built, first
    assert text_document(second) == {
        **without(built, "remediation", "likely_causes"),
        "repeated_advice": {"remediation": len(built["remediation"]), "likely_causes": 4},
        "advice_uri": ERRORS + "target_not_detected:openocd",
    }, second
    for result in (first, second):
        assert_text_projects(result)

    entry = served_entry(ERRORS + "target_not_detected:openocd")
    assert entry["scope"] == "openocd" and entry["remediation"] == built["remediation"], entry


def test_the_exclusive_refusal_names_its_own_scoped_entry() -> None:
    """permission_denied is two refusals under one error_type. The one a granted
    key causes carries the advice of the entry scoped `exclusive`, and the URI
    for its advice names that entry: the unscoped one would send the operator to
    grant the flag that is blocking them."""
    built = {
        "ok": False,
        "tool": "flash_firmware",
        "error_type": "permission_denied",
        "summary": exclusive_permission_summary("Flashing", "allow_raw_debugger_commands", "dut"),
        **exclusive_permission_fields("allow_raw_debugger_commands", "dut"),
    }
    blocking = permission_key("debuggers", "dut", "allow_raw_debugger_commands")

    first, second = one_session(built, built)

    for result in (first, second):
        assert result["structuredContent"] == built, result
        assert result["isError"] is True, result
    assert text_document(first) == built, first
    assert text_document(second) == {
        **without(built, "remediation"),
        "repeated_advice": {"remediation": len(built["remediation"])},
        "advice_uri": ERRORS + "permission_denied:exclusive",
    }, second
    for result in (first, second):
        assert_text_projects(result)

    entry = served_entry(ERRORS + "permission_denied:exclusive")
    assert entry["scope"] == "exclusive", entry
    assert [step.replace(PERMISSION_KEY_PLACEHOLDER, blocking) for step in entry["remediation"]] == built["remediation"], entry


def test_advice_without_a_catalogue_entry_is_counted_and_names_no_uri() -> None:
    """An error_type the catalogue has no entry for still has its repeated
    likely_causes left out and counted, and its text carries no `advice_uri`:
    there is no entry for one to name."""
    assert not [key for key in ERROR_CATALOGUE if key.partition(":")[0] == "timeout"]
    built = {
        "ok": False,
        "tool": "reset_target",
        "backend": "openocd",
        "error_type": "timeout",
        "summary": "Debugger command timed out.",
        "side_effect_status": "unknown",
        "hardware_state": "unknown",
        "retry_safe": False,
        "likely_causes": ["debugger stopped responding", "debug probe or target is stuck", "timeout_s is too low for this operation"],
    }

    first, second = one_session(built, built)

    for result in (first, second):
        assert result["structuredContent"] == built, result
        assert result["isError"] is True, result
    assert text_document(first) == built, first
    assert text_document(second) == {**without(built, "likely_causes"), "repeated_advice": {"likely_causes": 3}}, second
    for result in (first, second):
        assert_text_projects(result)


def quarantined_refusal(*reasons: str) -> dict:
    """The refusal a quarantined bench answers with, and the guidance a result carries for each of its reasons."""
    return attach_quarantine_guidance(
        {
            "ok": False,
            "tool": "flash_firmware",
            "error_type": "resource_quarantined",
            "summary": "Hardware effects are blocked until unresolved cleanup or audit state is recovered.",
            "cleanup_required": True,
            "quarantined": True,
            "retry_safe": False,
            "cleanup_reasons": sorted(reasons),
            "quarantine_id": "q-1",
        }
    )


def test_quarantine_guidance_already_sent_is_left_out_reason_by_reason() -> None:
    """quarantine_guidance is advice per reason. A later quarantined result that
    names a reason the session was already given guidance for sends only the
    guidance for its new reason. The reasons themselves, `quarantined` and
    `cleanup_required` are kept."""
    first = quarantined_refusal("debugger_result_unconfirmed")
    second = quarantined_refusal("debugger_result_unconfirmed", "com_write_effect_unconfirmed")
    assert len(second["quarantine_guidance"]) == 2 and first["quarantine_guidance"][0] in second["quarantine_guidance"], second
    new_guidance = [entry for entry in second["quarantine_guidance"] if entry["reason"] == "com_write_effect_unconfirmed"]

    results = one_session(first, second)

    assert [result["structuredContent"] for result in results] == [first, second], results
    assert [result["isError"] for result in results] == [True, True], results
    assert text_document(results[0]) == first, results[0]
    assert text_document(results[1]) == {
        **without(second, "quarantine_guidance"),
        "quarantine_guidance": new_guidance,
        "repeated_advice": {"quarantine_guidance": 1},
        "advice_uri": ERRORS + "resource_quarantined",
    }, results[1]
    for result in results:
        assert_text_projects(result)


# --- the envelope's own refusals ----------------------------------------------------


def test_the_envelopes_own_refusals_go_through_the_same_projection() -> None:
    """The refusals the envelope raises itself share the session's memory with
    every other result. Its first invalid_argument sends the catalogue's
    remediation whole. A tool's invalid_argument after it, and the envelope's
    next one, leave that remediation out and name the entry, and `do_not` stays
    in all three. All three tools answer from the unscoped entry."""
    catalogue = remediation_fields("invalid_argument")
    assert all(remediation_fields("invalid_argument", tool) == catalogue for tool in ("unknown", "com_read", "bench_run_status"))
    answered = invalid_argument("com_read", "port_id", "type", "port_id must be a string.")
    service = ScriptedService(answered)

    exit_code, replies = serve(service, lines(tools_call(1, 5), tools_call(2, "com_read", {}), tools_call(3, "bench_run_status", [1])))

    assert exit_code == 0
    assert [reply["id"] for reply in replies] == [1, 2, 3], replies
    assert service.calls == [("com_read", {})], service.calls
    results = [reply["result"] for reply in replies]
    structured = [
        invalid_argument("unknown", "name", "type", "tools/call requires a string name."),
        answered,
        invalid_argument("bench_run_status", "$", "type", "tools/call arguments must be an object."),
    ]
    assert [result["structuredContent"] for result in results] == structured, results
    assert [result["isError"] for result in results] == [True, True, True], results
    assert text_document(results[0]) == structured[0], results[0]
    for result, document in zip(results[1:], structured[1:], strict=True):
        assert text_document(result) == {
            **without(document, "remediation"),
            "repeated_advice": {"remediation": len(catalogue["remediation"])},
            "advice_uri": ERRORS + "invalid_argument",
        }, result
    for result in results:
        assert_text_projects(result)


# --- the service the server builds for itself ----------------------------------------


def test_a_server_session_remembers_its_advice_and_a_new_session_does_not(tmp_path: Path) -> None:
    """The same through the service `mcp-stdio` builds for itself. A probe the
    configuration refuses, asked twice in one session, carries the catalogue's
    remediation in the first text and not in the second. A new session over the
    same configuration and the same state directory starts with nothing sent,
    so nothing the first one remembered was left where the next one reads."""
    config = load_config(str(write_config(tmp_path, permissions={**DEFAULT_TEST_PERMISSIONS, "allow_probe": False})))

    exit_code, replies = serve(None, lines(tools_call(1, "probe_target", {}), tools_call(2, "probe_target", {})), config=config)
    assert exit_code == 0
    first, second = (reply["result"] for reply in replies)
    exit_code, replies = serve(None, lines(tools_call(1, "probe_target", {})), config=config)
    assert exit_code == 0
    (fresh,) = (reply["result"] for reply in replies)

    for result in (first, second, fresh):
        refusal = result["structuredContent"]
        assert refusal["error_type"] == "permission_denied" and refusal["remediation"] and refusal["do_not"], result
        assert result["isError"] is True, result
    refusal = first["structuredContent"]
    assert text_document(first)["remediation"] == refusal["remediation"], first
    sent_again = text_document(second)
    assert "remediation" not in sent_again, second
    assert sent_again["repeated_advice"] == {"remediation": len(refusal["remediation"])}, second
    assert sent_again["advice_uri"] == ERRORS + "permission_denied", second
    for key in ("summary", "permission", "next_step", "do_not"):
        assert sent_again[key] == refusal[key], (key, second)
    assert second["structuredContent"]["remediation"] == refusal["remediation"], second
    fresh_text = text_document(fresh)
    assert fresh_text["remediation"] == refusal["remediation"], fresh
    assert "repeated_advice" not in fresh_text and "advice_uri" not in fresh_text, fresh
    for result in (first, second, fresh):
        assert_text_projects(result)
