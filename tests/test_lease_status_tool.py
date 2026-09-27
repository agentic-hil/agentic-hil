"""`hardware_lease_status` is the MCP tool the agent-facing texts send a caller to.

The server instructions, AGENTS.md, the error catalogue and the reload refusal
tell a caller to read `hardware_lease_status` where it has to decide whether the
bench is held and what to recover. The service had a method of that name and
`agentic-hil lease-status` printed what it returned, but no MCP tool carried the
name, so the call those texts name answered `unknown_tool`.

It is a read. It answers what `lease-status` answers for the same bench, and it
leaves the bench as it found it: the end of every other call recovers and stands
down an incident that does not stand, and a status read that did either would
drive the board to answer a question about it, and report an incident it had
just ended.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest
from test_implicit_single_action_run import FakeBackend, config_for
from test_recover_tool import open_incident

from agentic_hil.contracts import MCP_TOOL_NAMES, TOOL_ANNOTATIONS
from agentic_hil.mcp import handle_mcp_message
from agentic_hil.tools import AgenticHILToolService

TOOL = "hardware_lease_status"
REASON = "debugger_result_unconfirmed"


def mcp_call(service: AgenticHILToolService, name: str) -> dict:
    """The whole `tools/call` result, `isError` included, as an MCP client receives it."""
    response = handle_mcp_message({"jsonrpc": "2.0", "id": name, "method": "tools/call", "params": {"name": name, "arguments": {}}}, service)
    assert isinstance(response, dict), response
    return response["result"]


def test_the_lease_status_the_texts_name_is_an_mcp_tool(tmp_path: Path) -> None:
    """The reproduction: call `hardware_lease_status` through the MCP server.

    It answered `unknown_tool`. It is listed now, declared read-only, and on a
    bench with nothing on it answers the status with no error flag."""
    assert TOOL in MCP_TOOL_NAMES
    assert TOOL_ANNOTATIONS[TOOL]["readOnlyHint"] is True, TOOL_ANNOTATIONS[TOOL]
    service = AgenticHILToolService(config_for(tmp_path), backend=FakeBackend())
    try:
        result = mcp_call(service, TOOL)
    finally:
        service.close()

    answer = result["structuredContent"]
    assert answer.get("error_type") != "unknown_tool", answer
    assert result["isError"] is False, answer
    assert answer["ok"] is True, answer
    assert answer["tool"] == TOOL, answer
    assert answer["blocked"] is False, answer


def test_reading_the_lease_status_leaves_the_incident_it_reports(tmp_path: Path) -> None:
    """A status read over an open incident drives nothing and ends nothing.

    The incident is this workspace's own and does not stand: a target state
    nobody confirmed. The end of any other call would try the recovery action
    on it and then stand it down. This one answers with the incident, and the
    bench still carries it after the answer, with nothing sent to the probe."""
    config = config_for(tmp_path)
    incident = open_incident(config, REASON)
    backend = FakeBackend()
    service = AgenticHILToolService(config, backend=backend)
    try:
        answer = service.call(TOOL)
        after = service.coordinator.status()
    finally:
        service.close()

    assert answer["tool"] == TOOL, answer
    assert answer["blocked"] is True, answer
    assert answer["incident_stands"] is False, answer
    assert answer["quarantine_id"] == incident, answer
    assert REASON in answer["cleanup_reasons"], answer
    assert [entry.get("reason") for entry in answer["quarantine_guidance"]] == answer["cleanup_reasons"], answer
    assert "incident_stood_down" not in answer, answer
    assert after["blocked"] is True and after["quarantine_id"] == incident, after
    assert backend.calls == [], backend.calls


def test_the_tool_answers_what_lease_status_answers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The MCP answer and the command line name the same incident the same way.

    The texts send an agent to the tool and an operator to `agentic-hil
    lease-status`; the two read the same record, so they agree on whether the
    bench is blocked, on the incident, on whether it stands, and on what to do
    next."""
    from agentic_hil.cli import dispatch

    config = config_for(tmp_path)
    open_incident(config, REASON)
    monkeypatch.setattr("agentic_hil.cli.load_cli_authoritative_config", lambda _path: config)

    printed = dispatch(argparse.Namespace(command="lease-status"))
    service = AgenticHILToolService(config, backend=FakeBackend())
    try:
        answer = mcp_call(service, TOOL)["structuredContent"]
    finally:
        service.close()

    for field in ("blocked", "incident_stands", "quarantine_id", "cleanup_reasons", "auto_recoverable", "auto_recover_policy", "next_step"):
        assert answer[field] == printed[field], (field, answer[field], printed[field])
