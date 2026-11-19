"""MCP over Streamable HTTP (2025-06-18) at /mcp: the bench's plan-only tools (#890).

The listener hands every /mcp request here after its Origin and Host checks, so
a browser is refused before a body is read. A request reaches the job service
and the read provider and nothing else: no device, port, lock or file, and no
argument is read as configuration or permission. Results pass through as the
/v1 routes pass them, which is why a job token survives in the answer that
carries it.
"""

from __future__ import annotations

import json
import secrets
import shlex
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from threading import Event, Lock
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, urlsplit

from jsonschema import Draft202012Validator

from agentic_hil import __version__
from agentic_hil.contracts import EMPTY_OBJECT_SCHEMA, NONEMPTY_STRING, object_schema, validate_tool_arguments
from agentic_hil.httpapi import BenchReads, JobService, JobServiceError, ServerSettings
from agentic_hil.knowledge import remediation_fields
from agentic_hil.mcp import (
    JSONRPC_INVALID_REQUEST,
    JSONRPC_METHOD_NOT_FOUND,
    error_response,
    handle_single_mcp_message,
    params_object_or_throw,
    parse_error_response,
    result_response,
    tool_result_text,
)
from agentic_hil.types import JsonObject

if TYPE_CHECKING:
    from agentic_hil.httpserver import _JobRequestHandler

BENCH_MCP_PROTOCOL_VERSION = "2025-06-18"

# Its own text: a bench server has none of the direct tools the stdio
# instructions steer an agent through, so it repeats none of that guidance.
BENCH_SERVER_INSTRUCTIONS = (
    "This bench takes test plans only, and artifacts arrive by upload with the command a test_reactor_run answer gives. "
    "Check a plan with test_reactor_check first, follow a job with test_reactor_status and read its outcome with "
    "test_reactor_report."
)

_PLAN = {**NONEMPTY_STRING, "description": "The test plan as YAML text, the body POST /v1/jobs takes, checked on arrival the same way."}
_RUN = {**NONEMPTY_STRING, "description": "The job id, the job_id of the test_reactor_run answer."}
_JOB_TOKEN = {
    **NONEMPTY_STRING,
    "description": "The job_token of the test_reactor_run answer. Only needed outside the MCP session that submitted the job.",
}
_FILTER = {**NONEMPTY_STRING, "description": "Optional filter, passed to the bench as given."}

BENCH_MCP_TOOLS: list[JsonObject] = [
    {
        "name": "test_reactor_check",
        "description": (
            "Check a test plan the way POST /v1/plans/check does, without submitting it: its validation, the devices it "
            "needs, a runtime estimate and whether it would start now or behind whom. A refused plan answers the "
            "validation_error check-plan gives, unchanged. Starts nothing."
        ),
        "inputSchema": object_schema({"plan": _PLAN}, required=["plan"]),
    },
    {
        "name": "test_reactor_run",
        "description": (
            "Submit a test plan as YAML text, checked on arrival as POST /v1/jobs checks it. Answers job_id, job_token, "
            "state and uploads: one command per artifact the plan still needs, run from the directory its artifact paths "
            "are relative to. The job queues once its last artifact is checked. Firmware never passes through this call."
        ),
        "inputSchema": object_schema({"plan": _PLAN}, required=["plan"]),
    },
    {
        "name": "test_reactor_status",
        "description": (
            "State, queue position, holder and step of a job on this bench; needs no token. With wait_s it answers on "
            "the next change rather than at once, and with a progressToken each job event also arrives as "
            "notifications/progress until the job ends or wait_s has passed."
        ),
        "inputSchema": object_schema(
            {
                "run": _RUN,
                "wait_s": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": 60,
                    "description": "Seconds to wait for the next change, at most 60. Absent or 0 answers at once.",
                },
            },
            required=["run"],
        ),
    },
    {
        "name": "test_reactor_withdraw",
        "description": (
            "Withdraw your own job as DELETE /v1/jobs/{id} does: a queued job leaves the queue at once (started false), "
            "a running one stops at its next step boundary, cleans up and keeps its report (started true). An ended "
            "job answers its own refusal. Outside the submitting session pass job_token."
        ),
        "inputSchema": object_schema({"run": _RUN, "job_token": _JOB_TOKEN}, required=["run"]),
    },
    {
        "name": "test_reactor_report",
        "description": (
            "The report of an ended job, unchanged, with junit_url, evidence_url and logs_url: where its JUnit, evidence "
            "and logs are fetched with Authorization: Bearer and the job_token. Outside the submitting session pass "
            "job_token."
        ),
        "inputSchema": object_schema({"run": _RUN, "job_token": _JOB_TOKEN}, required=["run"]),
    },
    {
        "name": "bench_describe",
        "description": "What this bench is: server, logical inventory, permissions, limits and the address of the plan schema. Reads only.",
        "inputSchema": dict(EMPTY_OBJECT_SCHEMA),
    },
    {
        "name": "bench_queue_status",
        "description": "The bench's devices, who holds them, the queue and its estimate. Reads only.",
        "inputSchema": dict(EMPTY_OBJECT_SCHEMA),
    },
    {
        "name": "bench_jobs_list",
        "description": (
            "Jobs on this bench as metadata, filtered by state, device, user or since. A job this session submitted "
            "also carries its report_url. Reads only."
        ),
        "inputSchema": object_schema({"state": _FILTER, "device": _FILTER, "user": _FILTER, "since": _FILTER}),
    },
    {
        "name": "debugger_probes_list",
        "description": "The debug probes attached to the bench, in the fields of the local tool of the same name. Reads only.",
        "inputSchema": dict(EMPTY_OBJECT_SCHEMA),
    },
    {
        "name": "debugger_info",
        "description": "The bench's installed debugger backends, in the fields of the local tool of the same name. Reads only.",
        "inputSchema": dict(EMPTY_OBJECT_SCHEMA),
    },
    {
        "name": "com_ports_list",
        "description": "The bench's configured COM ports, in the fields of the local tool of the same name. Reads only.",
        "inputSchema": dict(EMPTY_OBJECT_SCHEMA),
    },
    {
        "name": "can_buses_list",
        "description": "The bench's configured CAN buses, in the fields of the local tool of the same name. Reads only.",
        "inputSchema": dict(EMPTY_OBJECT_SCHEMA),
    },
    {
        "name": "hardware_lease_status",
        "description": (
            "Who holds which device on the bench, from every process, and any open incident, in the fields of the local "
            "tool of the same name. Reads only."
        ),
        "inputSchema": dict(EMPTY_OBJECT_SCHEMA),
    },
]

# The same five fields and the same rules as the stdio table in contracts.py:
# a read-only tool states neither effect hint, every tool states openWorldHint.
BENCH_TOOL_HINTS: dict[str, JsonObject] = {
    "test_reactor_check": {"title": "Check a test plan", "readOnlyHint": True, "openWorldHint": False},
    # A plan is free to flash, so the submission is annotated for what it may do.
    "test_reactor_run": {"title": "Submit a test plan", "readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": False},
    "test_reactor_status": {"title": "Job status", "readOnlyHint": True, "openWorldHint": False},
    # A second withdrawal changes nothing further: it answers the ended job's refusal.
    "test_reactor_withdraw": {"title": "Withdraw a job", "readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    "test_reactor_report": {"title": "Job report", "readOnlyHint": True, "openWorldHint": False},
    "bench_describe": {"title": "Describe the bench", "readOnlyHint": True, "openWorldHint": False},
    "bench_queue_status": {"title": "Bench queue status", "readOnlyHint": True, "openWorldHint": False},
    "bench_jobs_list": {"title": "List bench jobs", "readOnlyHint": True, "openWorldHint": False},
    "debugger_probes_list": {"title": "List connected debug probes", "readOnlyHint": True, "openWorldHint": False},
    "debugger_info": {"title": "Debugger backend availability", "readOnlyHint": True, "openWorldHint": False},
    "com_ports_list": {"title": "List COM ports", "readOnlyHint": True, "openWorldHint": False},
    "can_buses_list": {"title": "List CAN buses", "readOnlyHint": True, "openWorldHint": False},
    "hardware_lease_status": {"title": "Bench lease status", "readOnlyHint": True, "openWorldHint": False},
}
for _tool in BENCH_MCP_TOOLS:
    _tool["annotations"] = BENCH_TOOL_HINTS[str(_tool["name"])]

BENCH_MCP_TOOL_NAMES = [str(tool["name"]) for tool in BENCH_MCP_TOOLS]
BENCH_TOOL_VALIDATORS = {str(tool["name"]): Draft202012Validator(tool["inputSchema"]) for tool in BENCH_MCP_TOOLS}


@dataclass
class McpSession:
    """One Mcp-Session-Id: who the client said it is, and the jobs it submitted with their tokens."""

    client_name: str | None = None
    jobs: dict[str, str] = field(default_factory=dict)
    lock: Lock = field(default_factory=Lock)

    def remember(self, job_id: str, job_token: str) -> None:
        with self.lock:
            self.jobs[job_id] = job_token

    def token_for(self, job_id: str) -> str | None:
        with self.lock:
            return self.jobs.get(job_id)


class McpEndpoint:
    """The /mcp state of one listener. Sessions live in memory until DELETE or the process ends."""

    def __init__(self, settings: ServerSettings, service: JobService, bench_reads: BenchReads | None = None) -> None:
        self.settings = settings
        self.service = service
        self.bench_reads = bench_reads
        self._sessions: dict[str, McpSession] = {}
        self._lock = Lock()

    def open(self) -> tuple[str, McpSession]:
        session_id = secrets.token_urlsafe(32)
        session = McpSession()
        with self._lock:
            self._sessions[session_id] = session
        return session_id, session

    def get(self, session_id: str) -> McpSession | None:
        with self._lock:
            return self._sessions.get(session_id)

    def end(self, session_id: str) -> bool:
        with self._lock:
            return self._sessions.pop(session_id, None) is not None


@dataclass
class _McpCall:
    """What one request's tools see: the session, the address uploads go to, and the stream if it has one."""

    endpoint: McpEndpoint
    session: McpSession
    base_url: str
    stopping: Event
    progress_token: str | int | None = None
    emit: Callable[[JsonObject], None] | None = None
    keep_alive: Callable[[], None] | None = None


def handle(handler: _JobRequestHandler) -> None:
    """Answer one request to /mcp."""
    endpoint = handler.job_server.mcp
    if handler.command == "POST":
        _post(handler, endpoint)
    elif handler.command == "DELETE":
        known = _known_session(handler, endpoint)
        if known is not None:
            endpoint.end(known[0])
            handler._respond(204)
    else:
        # The server opens no stream of its own, which 2025-06-18 answers with 405.
        handler._respond(405, headers={"Allow": "POST, DELETE"})


def _post(handler: _JobRequestHandler, endpoint: McpEndpoint) -> None:
    body = handler._body()
    try:
        message = json.loads(body.decode("utf-8"), parse_constant=_refuse_constant)
    except ValueError:
        handler._json(400, parse_error_response())
        return
    if not isinstance(message, dict):
        summary = "POST /mcp carries one JSON-RPC message; 2025-06-18 has no batches."
        handler._json(400, error_response(None, JSONRPC_INVALID_REQUEST, "Invalid Request", {"summary": summary}))
        return
    if message.get("method") == "initialize" and "id" in message:
        _initialize(handler, endpoint, message)
        return
    known = _known_session(handler, endpoint)
    if known is None:
        return
    if "method" not in message and "id" in message and ("result" in message or "error" in message):
        # A response to a request this server never sends is accepted like a notification.
        handler._respond(202)
        return
    call = _McpCall(endpoint, known[1], _base_url(handler), handler.job_server.stopping)
    token = _progress_token(message)
    if token is not None:
        _stream(handler, message, call, token)
        return
    response = handle_single_mcp_message(message, call, _dispatch)
    if response is None:
        handler._respond(202)
    else:
        handler._json(200, response)


def _initialize(handler: _JobRequestHandler, endpoint: McpEndpoint, message: JsonObject) -> None:
    session_id, session = endpoint.open()
    call = _McpCall(endpoint, session, _base_url(handler), handler.job_server.stopping)
    response = handle_single_mcp_message(message, call, _dispatch)
    if response is None or "result" not in response:
        endpoint.end(session_id)
        handler._json(200, response or error_response(None, JSONRPC_INVALID_REQUEST, "Invalid Request"))
        return
    handler._json(200, response, {"Mcp-Session-Id": session_id})


def _known_session(handler: _JobRequestHandler, endpoint: McpEndpoint) -> tuple[str, McpSession] | None:
    """The request's session, or None once the refusal has been answered."""
    ids = handler.headers.get_all("Mcp-Session-Id", [])
    if len(ids) != 1:
        summary = "Mcp-Session-Id is required after initialize."
        handler._json(400, error_response(None, JSONRPC_INVALID_REQUEST, "Invalid Request", {"summary": summary}))
        return None
    session = endpoint.get(ids[0])
    if session is None:
        summary = "This Mcp-Session-Id is unknown or ended; initialize a new session."
        handler._json(404, error_response(None, JSONRPC_INVALID_REQUEST, "Invalid Request", {"summary": summary}))
        return None
    version = handler.headers.get("MCP-Protocol-Version")
    if version is not None and version != BENCH_MCP_PROTOCOL_VERSION:
        summary = f"This endpoint speaks MCP-Protocol-Version {BENCH_MCP_PROTOCOL_VERSION} only."
        handler._json(400, error_response(None, JSONRPC_INVALID_REQUEST, "Invalid Request", {"summary": summary}))
        return None
    return ids[0], session


def _base_url(handler: _JobRequestHandler) -> str:
    """The guard-validated Host's name with the listener's own port, so a Host without a port still works."""
    host = urlsplit("//" + (handler.headers.get("Host") or "")).hostname or handler.job_server.settings.host
    return f"http://{host}:{handler.job_server.server_port}"


def _progress_token(message: JsonObject) -> str | int | None:
    """The progressToken of a waiting test_reactor_status call, the one request answered as a stream."""
    params = message.get("params")
    if message.get("method") != "tools/call" or "id" not in message or not isinstance(params, dict):
        return None
    arguments = params.get("arguments")
    meta = params.get("_meta")
    wait_s = arguments.get("wait_s") if isinstance(arguments, dict) else None
    token = meta.get("progressToken") if isinstance(meta, dict) else None
    if params.get("name") != "test_reactor_status" or isinstance(wait_s, bool) or not isinstance(wait_s, (int, float)) or wait_s <= 0:
        return None
    if isinstance(token, bool) or not isinstance(token, (str, int)):
        return None
    return token


def _stream(handler: _JobRequestHandler, message: JsonObject, call: _McpCall, token: str | int) -> None:
    handler._start(200, "text/event-stream", {"Cache-Control": "no-cache"})

    def frame(payload: JsonObject) -> None:
        handler.wfile.write(b"data: " + json.dumps(payload, ensure_ascii=True).encode("utf-8") + b"\n\n")
        handler.wfile.flush()

    def keep_alive() -> None:
        handler.wfile.write(b": keep-alive\n\n")
        handler.wfile.flush()

    call.progress_token, call.emit, call.keep_alive = token, frame, keep_alive
    response = handle_single_mcp_message(message, call, _dispatch)
    if response is not None:
        frame(response)


def _refuse_constant(value: str) -> Any:
    raise ValueError(value)


def _dispatch(request_id: Any, method: str, params: Any, call: _McpCall) -> JsonObject:
    if method == "initialize":
        client = params_object_or_throw(params).get("clientInfo")
        name = client.get("name") if isinstance(client, dict) else None
        # What the client says it is, unverified: the submitted_by of its jobs.
        call.session.client_name = name if isinstance(name, str) else None
        return result_response(request_id, {
            "protocolVersion": BENCH_MCP_PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "agentic-hil", "version": __version__},
            "instructions": BENCH_SERVER_INSTRUCTIONS,
        })
    if method == "ping":
        return result_response(request_id, {})
    if method == "tools/list":
        return result_response(request_id, {"tools": BENCH_MCP_TOOLS})
    if method == "tools/call":
        return result_response(request_id, _call_tool(params, call))
    return error_response(request_id, JSONRPC_METHOD_NOT_FOUND, "Method not found", {"method": method})


def _call_tool(params: Any, call: _McpCall) -> JsonObject:
    params_object = params_object_or_throw(params)
    name = params_object.get("name")
    arguments = params_object.get("arguments")
    arguments = {} if arguments is None else arguments
    if not isinstance(name, str) or name not in _TOOLS:
        return _tool_result(_unknown_tool(name if isinstance(name, str) else "unknown"))
    refusal = validate_tool_arguments(name, arguments, validators=BENCH_TOOL_VALIDATORS)
    if refusal is not None:
        return _tool_result(refusal)
    try:
        result = _TOOLS[name](arguments, call)
    except JobServiceError as error:
        # The refusal is the result, whole: never folded into a JSON-RPC error.
        return _tool_result(error.to_dict())
    return _tool_result({"ok": True, "tool": name, **result})


def _tool_result(structured: JsonObject) -> JsonObject:
    return {
        "content": [{"type": "text", "text": tool_result_text(structured)}],
        "structuredContent": structured,
        "isError": structured.get("ok") is not True,
    }


def _unknown_tool(name: str) -> JsonObject:
    return {
        "ok": False,
        "tool": name,
        "error_type": "unknown_tool",
        "summary": "This bench takes test plans only; tools/list names every tool /mcp serves, and this is not one of them.",
        **remediation_fields("unknown_tool"),
    }


def _job_token(call: _McpCall, run: str, arguments: JsonObject) -> str:
    token = arguments.get("job_token")
    if isinstance(token, str):
        return token
    owned = call.session.token_for(run)
    if owned is not None:
        return owned
    raise JobServiceError(
        "job_token_missing",
        "This job was not submitted in this MCP session; pass the job_token its test_reactor_run answer gave.",
        {"job_id": run},
    )


def _reads(call: _McpCall, name: str) -> BenchReads:
    reads = call.endpoint.bench_reads
    if reads is None:
        raise JobServiceError(
            "bench_reads_unavailable",
            f"This bench server has no read provider, so {name} has nothing to answer from; the open read endpoints of #889 supply it.",
            {"tool": name},
        )
    return reads


def _job_url(call: _McpCall, job_id: str) -> str:
    return f"{call.base_url}/v1/jobs/{quote(job_id, safe='')}"


def _plan(arguments: JsonObject) -> bytes:
    return str(arguments["plan"]).encode("utf-8")


def _check(arguments: JsonObject, call: _McpCall) -> JsonObject:
    return call.endpoint.service.check_plan(_plan(arguments), {}, submitted_by=call.session.client_name)


def _run(arguments: JsonObject, call: _McpCall) -> JsonObject:
    submitted = call.endpoint.service.submit(_plan(arguments), {}, idempotency_key=None, submitted_by=call.session.client_name)
    job_id, token, missing = submitted.get("job_id"), submitted.get("job_token"), submitted.get("missing_artifacts")
    uploads: list[JsonObject] = []
    if isinstance(job_id, str) and isinstance(token, str):
        call.session.remember(job_id, token)
        for path in missing if isinstance(missing, list) else []:
            if isinstance(path, str):
                url = f"{_job_url(call, job_id)}/artifacts/{quote(path, safe='/')}"
                command = shlex.join(["curl", "-T", path, "-H", f"Authorization: Bearer {token}", url])
                uploads.append({"path": path, "command": command})
    return {**submitted, "uploads": uploads}


def _status(arguments: JsonObject, call: _McpCall) -> JsonObject:
    run = str(arguments["run"])
    service = call.endpoint.service
    snapshot = service.status(run)
    wait_s = min(float(arguments.get("wait_s") or 0), 60.0, call.endpoint.settings.prefer_wait_cap_s)
    if wait_s <= 0:
        return snapshot.status
    if call.emit is not None:
        return _follow(run, wait_s, call)
    changed = service.wait_for_revision(run, after_revision=snapshot.revision, timeout_s=wait_s)
    return (changed or snapshot).status


def _follow(run: str, wait_s: float, call: _McpCall) -> JsonObject:
    """Each job event as a notifications/progress until the events are complete or wait_s has passed."""
    service = call.endpoint.service
    deadline = time.monotonic() + wait_s
    cursor: str | None = None
    progress = 0
    while not call.stopping.is_set():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        batch = service.events_after(run, last_event_id=cursor, timeout_s=min(remaining, call.endpoint.settings.sse_keepalive_s))
        for event in batch.events:
            progress += 1
            params: JsonObject = {"progressToken": call.progress_token, "progress": progress}
            summary = event.status.get("summary")
            if isinstance(summary, str):
                params["message"] = summary
            if call.emit is not None:
                call.emit({"jsonrpc": "2.0", "method": "notifications/progress", "params": params})
            cursor = event.event_id
        if batch.complete:
            break
        if not batch.events and call.keep_alive is not None:
            call.keep_alive()
    return service.status(run).status


def _withdraw(arguments: JsonObject, call: _McpCall) -> JsonObject:
    run = str(arguments["run"])
    return call.endpoint.service.withdraw(run, job_token=_job_token(call, run, arguments))


def _report(arguments: JsonObject, call: _McpCall) -> JsonObject:
    run = str(arguments["run"])
    report = call.endpoint.service.report(run, job_token=_job_token(call, run, arguments))
    job = _job_url(call, run)
    return {"job_id": run, "report": report, "junit_url": f"{job}/junit", "evidence_url": f"{job}/evidence", "logs_url": f"{job}/logs/{{n}}"}


def _jobs_list(arguments: JsonObject, call: _McpCall) -> JsonObject:
    listed = _reads(call, "bench_jobs_list").bench_jobs_list(
        state=arguments.get("state"), device=arguments.get("device"), user=arguments.get("user"), since=arguments.get("since"),
    )
    jobs = listed.get("jobs")
    if not isinstance(jobs, list):
        return listed
    return {**listed, "jobs": [_with_report_url(entry, call) for entry in jobs]}


def _with_report_url(entry: Any, call: _McpCall) -> Any:
    """A job this session submitted gains its report address; every other entry stays as the provider gave it."""
    job_id = entry.get("job_id") if isinstance(entry, dict) else None
    if not isinstance(job_id, str) or call.session.token_for(job_id) is None:
        return entry
    return {**entry, "report_url": f"{_job_url(call, job_id)}/report"}


_TOOLS: dict[str, Callable[[JsonObject, _McpCall], JsonObject]] = {
    "test_reactor_check": _check,
    "test_reactor_run": _run,
    "test_reactor_status": _status,
    "test_reactor_withdraw": _withdraw,
    "test_reactor_report": _report,
    "bench_describe": lambda arguments, call: _reads(call, "bench_describe").bench_describe(),
    "bench_queue_status": lambda arguments, call: _reads(call, "bench_queue_status").bench_queue_status(),
    "bench_jobs_list": _jobs_list,
    "debugger_probes_list": lambda arguments, call: _reads(call, "debugger_probes_list").debugger_probes_list(),
    "debugger_info": lambda arguments, call: _reads(call, "debugger_info").debugger_info(),
    "com_ports_list": lambda arguments, call: _reads(call, "com_ports_list").com_ports_list(),
    "can_buses_list": lambda arguments, call: _reads(call, "can_buses_list").can_buses_list(),
    "hardware_lease_status": lambda arguments, call: _reads(call, "hardware_lease_status").hardware_lease_status(),
}
