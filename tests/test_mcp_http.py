"""MCP over Streamable HTTP at /mcp, against scripted services over real loopback sockets.

AC2 and AC6 of #890 need the real job store, a bench and the device locks; what
is proven here is the transport's share of them, never the claims themselves.
"""

from __future__ import annotations

import json
import re
import shlex
import sys
from pathlib import Path
from urllib.parse import urlsplit

from httpserver_fakes import (
    IMAGE,
    JOB_ID,
    JOB_PATH,
    MCP_CLIENT_NAME,
    MCP_HEADERS,
    MCP_PROTOCOL_VERSION,
    PLAN,
    TOKEN,
    Call,
    ScriptedBenchReads,
    ScriptedJobService,
    WaitReply,
    background_mcp,
    mcp_call,
    mcp_initialize,
    mcp_request,
    no_cors,
    request,
    rpc,
    running_http,
    service_error,
    short_settings,
    sse_messages,
    submission,
)
from support import scaled_time_bound

from agentic_hil.contracts import MCP_TOOLS
from agentic_hil.httpapi import JobEvent, JobEvents, JobServiceError, JobStatus

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - exercised on 3.10 only
    import tomli as tomllib

PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"
BENCH_TOOLS = frozenset({
    "test_reactor_check", "test_reactor_run", "test_reactor_status", "test_reactor_withdraw", "test_reactor_report",
    "bench_describe", "bench_queue_status", "bench_jobs_list",
    "debugger_probes_list", "debugger_info", "com_ports_list", "can_buses_list", "hardware_lease_status",
})
WRITING_TOOLS = frozenset({"test_reactor_run", "test_reactor_withdraw"})
LOCAL_READ_TOOLS = ("debugger_probes_list", "debugger_info", "com_ports_list", "can_buses_list", "hardware_lease_status")
READ_TOOLS = ("bench_describe", "bench_queue_status", "bench_jobs_list", *LOCAL_READ_TOOLS)
DIRECT_TOOLS = (
    "flash_firmware", "reset_target", "probe_target", "artifact_upload", "com_session_start", "com_write", "can_send",
    "debug_start_session", "power_supply_set", "bench_run_start", "hardware_recover", "project_config_set",
    "test_reactor_stop",
)
PLAN_TEXT = PLAN.decode("utf-8")
UNKNOWN_SESSION = "fixture-unknown-session"


def test_initialize_and_tools_list_serve_exactly_the_plan_only_tools() -> None:
    """AC1: the handshake, exactly the 13 plan-only tools, and every other name refused before any call."""
    service = ScriptedJobService()
    reads = ScriptedBenchReads()
    with running_http(service, bench_reads=reads) as server:
        initialize = rpc("initialize", {
            "protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": MCP_CLIENT_NAME, "version": "1.0"},
        })
        response = mcp_request(server, "POST", message=initialize)
        assert response.status == 200
        assert response.headers["content-type"].split(";", 1)[0] == "application/json"
        session = response.headers["mcp-session-id"]
        assert re.fullmatch(r"[\x21-\x7e]{22,}", session), session
        result = response.json()["result"]
        assert result["protocolVersion"] == MCP_PROTOCOL_VERSION
        assert result["capabilities"] == {"tools": {"listChanged": False}}
        assert result["serverInfo"]["name"] == "agentic-hil"
        instructions = result["instructions"]
        sentences = re.split(r"(?<=[.!?])\s+", instructions)
        assert any("plan" in sentence.lower() and "upload" in sentence.lower() for sentence in sentences), instructions
        stdio_only = {str(tool["name"]) for tool in MCP_TOOLS} - BENCH_TOOLS
        assert [name for name in sorted(stdio_only) if name in instructions] == [], instructions

        notified = mcp_request(server, "POST", message={"jsonrpc": "2.0", "method": "notifications/initialized"}, session=session)
        assert notified.status == 202
        assert notified.body == b""

        listed = mcp_request(server, "POST", message=rpc("tools/list", request_id=2), session=session)
        assert listed.status == 200
        tools = {tool["name"]: tool for tool in listed.json()["result"]["tools"]}
        assert set(tools) == BENCH_TOOLS
        for name, tool in tools.items():
            assert tool["annotations"]["readOnlyHint"] is (name not in WRITING_TOOLS), name
            assert tool["annotations"]["openWorldHint"] is False, name
            assert tool["inputSchema"]["additionalProperties"] is False, name
        local = {str(tool["name"]): tool["inputSchema"] for tool in MCP_TOOLS}
        for name in LOCAL_READ_TOOLS:
            assert tools[name]["inputSchema"] == local[name], name

        for request_id, name in enumerate(DIRECT_TOOLS, start=3):
            refused = mcp_call(server, session, name, {}, request_id=request_id)
            assert refused["isError"] is True, name
            assert refused["structuredContent"]["ok"] is False, name
            assert refused["structuredContent"]["tool"] == name
            assert refused["structuredContent"]["error_type"] == "unknown_tool", name
    assert service.calls == []
    assert reads.calls == []


def test_each_streamable_http_case_answers_its_status() -> None:
    """AC1: what a 2025-06-18 host meets on the session, version, batch, response, GET and DELETE paths."""
    service = ScriptedJobService()
    with running_http(service) as server:
        session, _ = mcp_initialize(server)
        ping = rpc("ping", request_id=2)

        missing = mcp_request(server, "POST", message=ping)
        assert missing.status == 400
        assert missing.json()["jsonrpc"] == "2.0" and "error" in missing.json()
        unknown = mcp_request(server, "POST", message=ping, session=UNKNOWN_SESSION)
        assert unknown.status == 404
        assert unknown.json()["jsonrpc"] == "2.0" and "error" in unknown.json()
        wrong_version = mcp_request(server, "POST", message=ping, session=session, headers={"MCP-Protocol-Version": "2024-11-05"})
        assert wrong_version.status == 400
        negotiated = mcp_request(server, "POST", message=ping, session=session, headers={"MCP-Protocol-Version": None})
        assert negotiated.status == 200
        assert negotiated.json() == {"jsonrpc": "2.0", "id": 2, "result": {}}

        batch = mcp_request(server, "POST", message=[ping], session=session)
        assert batch.status == 400
        assert batch.json()["id"] is None
        assert batch.json()["error"]["code"] == -32600
        answered = mcp_request(server, "POST", message={"jsonrpc": "2.0", "id": 7, "result": {}}, session=session)
        assert answered.status == 202
        assert answered.body == b""

        stream = mcp_request(server, "GET", session=session)
        assert stream.status == 405
        assert stream.headers["allow"] == "POST, DELETE"

        assert mcp_request(server, "DELETE").status == 400
        assert mcp_request(server, "DELETE", session=UNKNOWN_SESSION).status == 404
        ended = mcp_request(server, "DELETE", session=session)
        assert ended.status == 204
        assert mcp_request(server, "POST", message=ping, session=session).status == 404
    assert service.calls == []


def test_check_run_upload_status_and_report_flow_in_one_session() -> None:
    """AC2, the transport's share: check, run, the upload by the returned command, status, report, with no token passed."""
    service = ScriptedJobService()
    settings = short_settings()
    preview = {"validation": {"ok": True}, "devices": ["dut"], "estimate": {"runtime_s": 12}, "would_start_now": True, "waiting_for": []}
    accepted = submission("awaiting_artifacts")
    running = JobStatus(3, {"job_id": JOB_ID, "state": "running", "step": 1, "summary": "Running."})
    finished = JobStatus(4, {"job_id": JOB_ID, "state": "finished", "summary": "Finished."})
    report = {"ok": True, "tool": "test_reactor_run", "run_ok": True, "steps": [{"action": "flash", "ok": True}]}
    service.script("check_plan", preview)
    service.script("submit", accepted)
    service.script("upload_artifact", {"job_id": JOB_ID, "state": "queued", "missing_artifacts": []})
    service.script("status", running)
    service.script("wait_for_revision", finished)
    service.script("report", report)
    with running_http(service, settings) as server:
        session, _ = mcp_initialize(server)
        base = f"http://127.0.0.1:{server.server_port}{JOB_PATH}"

        checked = mcp_call(server, session, "test_reactor_check", {"plan": PLAN_TEXT})
        assert checked["isError"] is False
        assert checked["structuredContent"] == {"ok": True, "tool": "test_reactor_check", **preview}

        run = mcp_call(server, session, "test_reactor_run", {"plan": PLAN_TEXT}, request_id=2)
        content = run["structuredContent"]
        assert run["isError"] is False
        assert content["ok"] is True and content["tool"] == "test_reactor_run"
        assert {key: content[key] for key in accepted} == accepted
        (upload,) = content["uploads"]
        assert upload["path"] == "build/app.elf"
        argv = shlex.split(upload["command"])
        assert argv == ["curl", "-T", "build/app.elf", "-H", f"Authorization: Bearer {TOKEN}", f"{base}/artifacts/build/app.elf"]
        assert TOKEN not in argv[-1]

        # The command's own PUT, with the bytes `-T` names, against the same listener.
        header, _, value = argv[4].partition(": ")
        put = request(server, "PUT", urlsplit(argv[-1]).path, body=IMAGE, headers={header: value})
        assert put.status == 201
        assert service.calls[-1] == Call("upload_artifact", (JOB_ID, "build/app.elf", IMAGE), {"job_token": TOKEN})

        status = mcp_call(server, session, "test_reactor_status", {"run": JOB_ID, "wait_s": 30}, request_id=3)
        assert status["structuredContent"] == {"ok": True, "tool": "test_reactor_status", **finished.status}
        waits = [call for call in service.calls if call.method == "wait_for_revision"]
        assert waits[0].args == (JOB_ID,)
        assert waits[0].kwargs["after_revision"] == running.revision
        assert 0 < waits[0].kwargs["timeout_s"] <= settings.prefer_wait_cap_s

        reported = mcp_call(server, session, "test_reactor_report", {"run": JOB_ID}, request_id=4)
        assert reported["isError"] is False
        assert reported["structuredContent"] == {
            "ok": True, "tool": "test_reactor_report", "job_id": JOB_ID, "report": report,
            "junit_url": f"{base}/junit", "evidence_url": f"{base}/evidence", "logs_url": f"{base}/logs/{{n}}",
        }
        for key in ("junit_url", "evidence_url", "logs_url"):
            assert TOKEN not in reported["structuredContent"][key], key

    assert [call.method for call in service.calls] == ["check_plan", "submit", "upload_artifact", "status", "wait_for_revision", "report"]
    assert service.calls[0] == Call("check_plan", (PLAN, {}), {"submitted_by": MCP_CLIENT_NAME})
    assert service.calls[1] == Call("submit", (PLAN, {}), {"idempotency_key": None, "submitted_by": MCP_CLIENT_NAME})
    assert service.calls[3] == Call("status", (JOB_ID,), {})
    assert service.calls[5] == Call("report", (JOB_ID,), {"job_token": TOKEN})


def test_upload_command_names_the_listener_port_when_host_has_none() -> None:
    service = ScriptedJobService()
    service.script("submit", submission("awaiting_artifacts"))
    portless = {"Host": "127.0.0.1"}
    with running_http(service) as server:
        session, _ = mcp_initialize(server, headers=portless)
        run = mcp_call(server, session, "test_reactor_run", {"plan": PLAN_TEXT}, headers=portless)
        (upload,) = run["structuredContent"]["uploads"]
        assert shlex.split(upload["command"])[-1] == f"http://127.0.0.1:{server.server_port}{JOB_PATH}/artifacts/build/app.elf"


def test_refused_plan_arrives_whole_as_the_tool_result() -> None:
    """AC3: the refusal check-plan gives is the tool result, never a JSON-RPC error."""
    service = ScriptedJobService()
    validation = {
        "error_type": "test_plan_rejected", "summary": "Recorded plan refusal.",
        "step": 1, "field": "image_path", "route": "dut", "action": "flash",
    }
    refusal = service_error("test_config_invalid", {"validation_error": validation})
    service.script("check_plan", refusal)
    service.script("submit", refusal)
    with running_http(service) as server:
        session, _ = mcp_initialize(server)
        for request_id, name in enumerate(("test_reactor_check", "test_reactor_run"), start=2):
            message = rpc("tools/call", {"name": name, "arguments": {"plan": PLAN_TEXT}}, request_id)
            response = mcp_request(server, "POST", message=message, session=session)
            assert response.status == 200
            body = response.json()
            assert "error" not in body, body
            result = body["result"]
            assert result["isError"] is True
            assert result["structuredContent"] == refusal.to_dict()
            assert result["structuredContent"]["validation_error"] == validation
            text = json.loads(result["content"][0]["text"])
            for key in ("error_type", "summary", "likely_causes", "validation_error"):
                assert text[key] == refusal.to_dict()[key], key
    assert service.published == []
    assert [call.method for call in service.calls] == ["check_plan", "submit"]


def test_withdraw_makes_the_delete_call_and_takes_a_token_outside_its_session() -> None:
    """AC4: the call DELETE /v1/jobs/{id} makes, its replies unchanged; outside its session the token is an argument."""
    service = ScriptedJobService()
    queued = {"job_id": JOB_ID, "state": "withdrawn", "started": False}
    running = {"job_id": JOB_ID, "state": "withdrawing", "started": True}
    ended = service_error("job_finished")
    service.script("submit", submission())
    service.script("withdraw", queued, running, ended, running)
    withdrawal = Call("withdraw", (JOB_ID,), {"job_token": TOKEN})
    with running_http(service) as server:
        session, _ = mcp_initialize(server)
        mcp_call(server, session, "test_reactor_run", {"plan": PLAN_TEXT})

        for reply in (queued, running):
            result = mcp_call(server, session, "test_reactor_withdraw", {"run": JOB_ID})
            assert result["isError"] is False
            assert result["structuredContent"] == {"ok": True, "tool": "test_reactor_withdraw", **reply}
            assert service.calls[-1] == withdrawal
        result = mcp_call(server, session, "test_reactor_withdraw", {"run": JOB_ID})
        assert result["isError"] is True
        assert result["structuredContent"] == ended.to_dict()

        other, _ = mcp_initialize(server)
        before = list(service.calls)
        result = mcp_call(server, other, "test_reactor_withdraw", {"run": JOB_ID})
        assert result["isError"] is True
        assert result["structuredContent"]["ok"] is False
        assert result["structuredContent"]["error_type"] == "job_token_missing"
        assert service.calls == before
        result = mcp_call(server, other, "test_reactor_withdraw", {"run": JOB_ID, "job_token": TOKEN})
        assert result["structuredContent"] == {"ok": True, "tool": "test_reactor_withdraw", **running}
        assert service.calls[-1] == withdrawal


def test_foreign_origin_and_host_are_refused_on_mcp() -> None:
    """AC5: #888's guard answers 403 on /mcp before the body is read and before any call."""
    service = ScriptedJobService()
    reads = ScriptedBenchReads()
    message = json.dumps(rpc("initialize", {"protocolVersion": MCP_PROTOCOL_VERSION, "capabilities": {}})).encode("utf-8")
    with running_http(service, bench_reads=reads) as server:
        for method, body in (("POST", message), ("DELETE", None)):
            for foreign, error_type in (
                ({"Origin": "https://foreign.example.invalid"}, "origin_refused"),
                ({"Host": "foreign.example.invalid"}, "host_refused"),
            ):
                headers = {**MCP_HEADERS, "Mcp-Session-Id": UNKNOWN_SESSION, **foreign}
                response = request(server, method, "/mcp", body=body, headers=headers, withhold_body=body is not None)
                assert response.status == 403, (method, foreign, response)
                assert response.json()["error_type"] == error_type
                no_cors(response)
    assert service.calls == []
    assert reads.calls == []


def test_status_with_a_progress_token_streams_progress_then_the_response() -> None:
    """With a progressToken, each job event is a notifications/progress, then the response, then the stream ends."""
    service = ScriptedJobService()
    settings = short_settings()
    queued = JobStatus(1, {"job_id": JOB_ID, "state": "queued", "summary": "Queued."})
    running = {"job_id": JOB_ID, "state": "running", "step": 1, "summary": "Running step 1."}
    finished = {"job_id": JOB_ID, "state": "finished", "summary": "Finished."}
    events = WaitReply()
    service.script("status", queued, JobStatus(3, finished))
    service.script("events_after", events)
    message = rpc("tools/call", {
        "name": "test_reactor_status", "arguments": {"run": JOB_ID, "wait_s": 30}, "_meta": {"progressToken": "fixture-progress"},
    }, request_id=5)
    with running_http(service, settings) as server:
        session, _ = mcp_initialize(server)
        with background_mcp(server, service, message, session=session) as pending:
            assert events.entered.wait(scaled_time_bound(10)), "the stream never asked for the job's events"
            assert not pending.done(), "the stream answered before the job's events arrived"
            events.publish(JobEvents((JobEvent("event-1", running), JobEvent("event-2", finished)), True))
            streamed = pending.result(timeout=scaled_time_bound(10))
    assert streamed.status == 200
    assert streamed.headers["content-type"].split(";", 1)[0] == "text/event-stream"
    messages = sse_messages(streamed.body)
    assert messages[:2] == [
        {"jsonrpc": "2.0", "method": "notifications/progress", "params": {"progressToken": "fixture-progress", "progress": 1, "message": "Running step 1."}},
        {"jsonrpc": "2.0", "method": "notifications/progress", "params": {"progressToken": "fixture-progress", "progress": 2, "message": "Finished."}},
    ]
    assert len(messages) == 3, messages
    final = messages[2]
    assert final["id"] == 5 and "error" not in final, final
    assert final["result"]["isError"] is False
    assert final["result"]["structuredContent"] == {"ok": True, "tool": "test_reactor_status", **finished}
    assert [call.method for call in service.calls] == ["status", "events_after", "status"]
    asked = service.calls[1]
    assert asked.args == (JOB_ID,)
    assert 0 < asked.kwargs["timeout_s"] <= settings.sse_keepalive_s


def test_read_tools_without_a_provider_answer_the_refusal_whole() -> None:
    service = ScriptedJobService()
    with running_http(service) as server:
        session, _ = mcp_initialize(server)
        for request_id, name in enumerate(READ_TOOLS, start=2):
            result = mcp_call(server, session, name, {}, request_id=request_id)
            content = result["structuredContent"]
            assert result["isError"] is True, name
            assert content == JobServiceError("bench_reads_unavailable", content["summary"], {"tool": name}).to_dict(), name
            assert "#889" in content["summary"], content
            assert content["remediation"], content
    assert service.calls == []


def test_read_tools_pass_provider_content_and_mark_only_own_jobs() -> None:
    service = ScriptedJobService()
    service.script("submit", submission())
    reads = ScriptedBenchReads()
    replies = {name: {"fixture_read": name, "entries": [{"name": "dut"}]} for name in READ_TOOLS if name != "bench_jobs_list"}
    for name, reply in replies.items():
        reads.script(name, reply)
    own = {"job_id": JOB_ID, "state": "queued", "user": MCP_CLIENT_NAME}
    foreign = {"job_id": "job-other", "state": "running", "user": "another-submitter"}
    reads.script("bench_jobs_list", {"jobs": [own, foreign], "count": 2})
    with running_http(service, bench_reads=reads) as server:
        session, _ = mcp_initialize(server)
        mcp_call(server, session, "test_reactor_run", {"plan": PLAN_TEXT})
        for request_id, (name, reply) in enumerate(replies.items(), start=2):
            result = mcp_call(server, session, name, {}, request_id=request_id)
            assert result["isError"] is False, name
            assert result["structuredContent"] == {"ok": True, "tool": name, **reply}, name

        listed = mcp_call(server, session, "bench_jobs_list", {"state": "queued", "device": "dut"}, request_id=20)
        assert listed["isError"] is False
        content = listed["structuredContent"]
        assert content["count"] == 2
        assert content["jobs"] == [{**own, "report_url": f"http://127.0.0.1:{server.server_port}{JOB_PATH}/report"}, foreign]
    assert reads.calls == [
        *(Call(name, (), {}) for name in replies),
        Call("bench_jobs_list", (), {"state": "queued", "device": "dut", "user": None, "since": None}),
    ]


def test_status_passes_a_waiting_holder_through_and_answers_on_the_change() -> None:
    """AC6, the transport's share only: the holder the service names arrives unchanged, and wait_s answers on the change."""
    service = ScriptedJobService()
    settings = short_settings()
    waiting = JobStatus(2, {
        "job_id": JOB_ID, "state": "queued",
        "waiting_for": [{"device": "dut", "position": 1, "held_by": {"frontend": "mcp", "label": "local run"}}],
        "summary": "Waiting for dut, held by a local run.",
    })
    running = JobStatus(3, {"job_id": JOB_ID, "state": "running", "step": 1, "summary": "Running."})
    change = WaitReply()
    service.script("status", waiting, waiting)
    service.script("wait_for_revision", change)
    wait = rpc("tools/call", {"name": "test_reactor_status", "arguments": {"run": JOB_ID, "wait_s": 30}}, request_id=3)
    with running_http(service, settings) as server:
        session, _ = mcp_initialize(server)
        now = mcp_call(server, session, "test_reactor_status", {"run": JOB_ID}, request_id=2)
        assert now["structuredContent"] == {"ok": True, "tool": "test_reactor_status", **waiting.status}
        with background_mcp(server, service, wait, session=session) as pending:
            assert change.entered.wait(scaled_time_bound(10)), "the call never entered the service's wait"
            assert not pending.done(), "the call answered before the job changed"
            change.publish(running)
            answered = pending.result(timeout=scaled_time_bound(10))
    assert answered.status == 200
    assert answered.json()["result"]["structuredContent"] == {"ok": True, "tool": "test_reactor_status", **running.status}
    waits = [call for call in service.calls if call.method == "wait_for_revision"]
    assert len(waits) == 1
    assert waits[0].args == (JOB_ID,)
    assert waits[0].kwargs["after_revision"] == waiting.revision
    assert 0 < waits[0].kwargs["timeout_s"] <= settings.prefer_wait_cap_s


def test_the_transport_module_is_in_the_configured_type_check() -> None:
    """The type check covers the MCP and stdio transports (`[tool.mypy]`), so the /mcp transport is one of its files."""
    files = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["tool"]["mypy"]["files"]
    if "src/agentic_hil/mcp_http.py" not in files:
        raise AssertionError("src/agentic_hil/mcp_http.py is not in [tool.mypy] files of pyproject.toml")
