"""The JSON-RPC envelope around the tools, pinned to the JSON-RPC 2.0 and MCP specifications (#503).

Everything below is protocol handling with no hardware behind it: what the
server writes back for a notification (nothing), for a line that is not JSON
(the parse error, with the session kept), for a method it does not serve, for
params that are not an object, for a tools/call whose name or arguments have
the wrong shape, for a tool that raises, and what it closes when the host ends
the session. Each of these is something an agent host or an operator sees, and
until this file none of them was asserted against the server.

The exchange a real client opens with (`initialize`, then the
`notifications/initialized` notification, then `tools/list`) is spelled here
in the shape the MCP specification gives it. No recording of a real host's
stdio stream is checked in yet; the protocol rule these tests hold the server
to is the specification's, so no tool is needed to state it.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import write_authoritative_config, write_config
from test_can_frame_and_routing import RecordingBus, fake_can_module
from test_implicit_single_action_run import DEVICE, PORT_ID, FakeBackend, FakeSerialHandle, config_for

from agentic_hil.config import load_config
from agentic_hil.knowledge import remediation_fields
from agentic_hil.mcp import handle_mcp_message
from agentic_hil.stdio import run_stdio_server
from agentic_hil.tools import AgenticHILToolService

JSONRPC_PARSE_ERROR = -32700
JSONRPC_INVALID_REQUEST = -32600
JSONRPC_METHOD_NOT_FOUND = -32601
JSONRPC_INVALID_PARAMS = -32602
JSONRPC_INTERNAL_ERROR = -32603

# The three lines every MCP client opens a session with, in the order the
# specification gives them. The notification carries no id, so it expects no
# reply: a reply with `id: null` would be an unsolicited message on the stream
# the client is parsing.
INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "envelope-test", "version": "0"}},
}
INITIALIZED = {"jsonrpc": "2.0", "method": "notifications/initialized"}
CANCELLED = {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 99, "reason": "user"}}
TOOLS_LIST = {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}

# A credential the way a package index URL carries one. If a tool's exception
# quotes it, the wire must not.
CREDENTIALED_URL = "https://user:tok@host/simple/"


def lines(*messages: object) -> str:
    """The stdin of a session: one JSON line per message, newline terminated."""
    return "".join(json.dumps(message) + "\n" for message in messages)


def serve(tools: object, stdin: str, config=None) -> tuple[int, list[dict]]:
    """Run the stdio loop over ``stdin`` and hand back the exit code and every reply, parsed."""
    output = io.StringIO()
    exit_code = run_stdio_server(config, input_stream=io.StringIO(stdin), output_stream=output, tools=tools)  # type: ignore[arg-type]
    return exit_code, [json.loads(line) for line in output.getvalue().splitlines()]


def tools_call(request_id: object, name: object, arguments: object = "omitted", *, params: object = "built") -> dict:
    """A tools/call request. ``arguments`` left at its default is left out of the params."""
    if params != "built":
        return {"jsonrpc": "2.0", "id": request_id, "method": "tools/call", "params": params}
    built: dict = {"name": name}
    if arguments != "omitted":
        built["arguments"] = arguments
    return {"jsonrpc": "2.0", "id": request_id, "method": "tools/call", "params": built}


class RecordingService:
    """A tool service that records what the envelope handed it and answers ok."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []
        self.closed = False

    def call(self, name: str, arguments: dict | None = None) -> dict:
        self.calls.append((name, arguments))
        return {"ok": True, "tool": name, "summary": "recorded"}

    def close(self) -> None:
        self.closed = True


class RaisingService(RecordingService):
    """A tool service whose every call raises the error it was built with."""

    def __init__(self, error: BaseException) -> None:
        super().__init__()
        self.error = error

    def call(self, name: str, arguments: dict | None = None) -> dict:
        self.calls.append((name, arguments))
        raise self.error


def real_service(tmp_path: Path) -> AgenticHILToolService:
    return AgenticHILToolService(load_config(str(write_config(tmp_path))), frontend="mcp")


# --- notifications ---------------------------------------------------------


def test_a_notification_gets_no_response() -> None:
    """`notifications/initialized` and `notifications/cancelled` are answered with
    silence, and nothing on the stream carries `id: null` for them: exactly the
    two requests in the session are answered, in order."""
    service = RecordingService()

    exit_code, replies = serve(service, lines(INITIALIZE, INITIALIZED, CANCELLED, TOOLS_LIST))

    assert exit_code == 0
    assert [reply["id"] for reply in replies] == [1, 2], replies
    assert all("result" in reply for reply in replies), replies
    assert not any(reply.get("id") is None for reply in replies), replies
    assert handle_mcp_message({"jsonrpc": "2.0", "method": "notifications/initialized"}, service) is None  # type: ignore[arg-type]
    assert handle_mcp_message(CANCELLED, service) is None  # type: ignore[arg-type]


# --- malformed lines ---------------------------------------------------------


def test_a_malformed_stdin_line_is_a_parse_error_and_the_next_request_still_answers() -> None:
    """JSON-RPC 2.0 section 4.1: a line that is not JSON is answered with -32700
    and `id: null`, the session is kept, and a blank line produces nothing. A
    line that parses only as Python (NaN, Infinity) is not JSON either."""
    service = RecordingService()
    stdin = '{not json\n\n{"jsonrpc":"2.0","id":1,"method":"ping"}\n{"jsonrpc":"2.0","id":NaN,"method":"ping"}\n{"jsonrpc":"2.0","id":3,"method":"ping"}\n'

    exit_code, replies = serve(service, stdin)

    assert exit_code == 0
    assert len(replies) == 4, replies
    assert replies[0]["error"]["code"] == JSONRPC_PARSE_ERROR, replies[0]
    assert replies[0]["error"]["message"] == "Parse error", replies[0]
    assert replies[0]["id"] is None, replies[0]
    assert replies[1] == {"jsonrpc": "2.0", "id": 1, "result": {}}, replies[1]
    assert replies[2]["error"]["code"] == JSONRPC_PARSE_ERROR, replies[2]
    assert replies[2]["id"] is None, replies[2]
    assert replies[3] == {"jsonrpc": "2.0", "id": 3, "result": {}}, replies[3]


# --- exceptions inside a tool ------------------------------------------------


def test_a_tool_that_raises_is_an_internal_error_response_not_a_dead_server() -> None:
    """An exception a tool lets out is answered as -32603 with the exception's
    text as `data.summary`, and the loop goes on: the ping after it is answered."""
    service = RaisingService(RuntimeError("boom"))

    exit_code, replies = serve(service, lines(tools_call(1, "bench_run_status", {}), {"jsonrpc": "2.0", "id": 2, "method": "ping"}))

    assert exit_code == 0
    assert [reply["id"] for reply in replies] == [1, 2], replies
    assert replies[0]["error"]["code"] == JSONRPC_INTERNAL_ERROR, replies[0]
    assert replies[0]["error"]["message"] == "Internal error", replies[0]
    assert replies[0]["error"]["data"]["summary"] == "boom", replies[0]
    assert replies[1] == {"jsonrpc": "2.0", "id": 2, "result": {}}, replies[1]
    assert service.closed is True


def test_a_fault_inside_a_tool_is_an_internal_error_not_the_callers_params(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A TypeError raised inside a tool is the server's fault, not the agent's
    parameters. Answered as -32602 it tells the agent to correct arguments that
    were right; the code for a fault behind a well-formed request is -32603."""
    service = real_service(tmp_path)
    try:
        monkeypatch.setattr(service, "bench_run_status", lambda: (_ for _ in ()).throw(TypeError("bug: 'NoneType' has no len()")))

        response = handle_mcp_message(tools_call(1, "bench_run_status", {}), service)

        assert isinstance(response, dict)
        assert response["error"]["code"] == JSONRPC_INTERNAL_ERROR, response["error"]
        assert response["error"]["message"] == "Internal error", response["error"]
    finally:
        service.close()


def test_a_fault_inside_a_tool_does_not_carry_the_credential_its_exception_quoted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every tool result passes through redaction before it reaches the wire. The
    error response built from a tool's exception must too: an exception that
    quotes a credentialed index URL must not hand the credential to the host."""
    service = real_service(tmp_path)
    try:
        monkeypatch.setattr(service, "bench_run_status", lambda: (_ for _ in ()).throw(RuntimeError(f"index refused: {CREDENTIALED_URL}")))

        response = handle_mcp_message(tools_call(1, "bench_run_status", {}), service)

        assert isinstance(response, dict)
        assert "error" in response, response
        assert "tok" not in json.dumps(response), response
        # The account and the host survive: they say what went wrong.
        assert "user" in json.dumps(response), response
    finally:
        service.close()


def test_a_params_shape_fault_is_still_the_callers_params() -> None:
    """The neighbour: params that are not an object are the request's fault and
    stay -32602, with the summary that says what an object is expected."""
    service = RecordingService()

    response = handle_mcp_message(tools_call(1, None, params=[1]), service)  # type: ignore[arg-type]

    assert isinstance(response, dict)
    assert response["error"]["code"] == JSONRPC_INVALID_PARAMS, response["error"]
    assert response["error"]["message"] == "Invalid params", response["error"]
    assert response["error"]["data"]["summary"] == "JSON-RPC params must be an object.", response["error"]
    assert service.calls == []


# --- tools/call argument shapes ---------------------------------------------


def test_tools_call_argument_shapes() -> None:
    """`arguments: null` reaches the tool as `{}`; a list is refused at the result
    level with the tool name kept; a non-string name is refused as tool
    `unknown`; params that are not an object are -32602. Agents do send
    `arguments: null`, so that one must succeed."""
    service = RecordingService()

    null_arguments = handle_mcp_message(tools_call(1, "bench_run_status", None), service)  # type: ignore[arg-type]
    omitted_arguments = handle_mcp_message(tools_call(2, "bench_run_status"), service)  # type: ignore[arg-type]
    list_arguments = handle_mcp_message(tools_call(3, "bench_run_status", [1]), service)  # type: ignore[arg-type]
    numeric_name = handle_mcp_message(tools_call(4, 5, {}), service)  # type: ignore[arg-type]
    list_params = handle_mcp_message(tools_call(5, None, params=[]), service)  # type: ignore[arg-type]
    no_params = handle_mcp_message({"jsonrpc": "2.0", "id": 6, "method": "tools/call"}, service)  # type: ignore[arg-type]

    assert service.calls == [("bench_run_status", {}), ("bench_run_status", {})], service.calls
    assert null_arguments["result"]["isError"] is False, null_arguments
    assert omitted_arguments["result"]["isError"] is False, omitted_arguments

    refused = list_arguments["result"]
    assert refused["isError"] is True, refused
    assert refused["structuredContent"]["error_type"] == "invalid_argument", refused
    assert refused["structuredContent"]["tool"] == "bench_run_status", refused
    assert json.loads(refused["content"][0]["text"]) == refused["structuredContent"], refused

    unknown = numeric_name["result"]
    assert unknown["isError"] is True, unknown
    assert unknown["structuredContent"]["error_type"] == "invalid_argument", unknown
    assert unknown["structuredContent"]["tool"] == "unknown", unknown

    assert list_params["error"]["code"] == JSONRPC_INVALID_PARAMS, list_params

    # No params member at all: there is no name to keep, so the refusal is the
    # result-level one for tool `unknown`, not a protocol error.
    absent = no_params["result"]
    assert absent["isError"] is True, absent
    assert absent["structuredContent"]["error_type"] == "invalid_argument", absent
    assert absent["structuredContent"]["tool"] == "unknown", absent
    assert service.calls == [("bench_run_status", {}), ("bench_run_status", {})], service.calls


def assert_reads_like_the_catalogues_invalid_argument(refusal: dict, *, tool: str, field: str) -> None:
    """What every invalid_argument on the surface carries: `field` and `validator`
    say what was wrong, and the remediation and do_not are the catalogue's own
    lists for the tool, the same ones `agentic-hil://reference/errors` serves."""
    catalogue = remediation_fields("invalid_argument", tool)
    assert refusal["error_type"] == "invalid_argument", refusal
    assert refusal["tool"] == tool, refusal
    assert refusal["field"] == field, refusal
    assert refusal["validator"] == "type", refusal
    assert refusal["remediation"] == catalogue["remediation"], refusal
    assert refusal["do_not"] == catalogue["do_not"], refusal


def test_a_tools_call_with_list_arguments_is_refused_the_way_every_schema_refusal_is() -> None:
    """The envelope's own invalid_argument reads like the catalogue's: `field`
    and `validator` say what was wrong, and the remediation and do_not every
    other invalid_argument carries are there to read with them."""
    service = RecordingService()

    response = handle_mcp_message(tools_call(1, "bench_run_status", [1]), service)  # type: ignore[arg-type]

    assert isinstance(response, dict)
    refusal = response["result"]["structuredContent"]
    assert_reads_like_the_catalogues_invalid_argument(refusal, tool="bench_run_status", field="$")
    assert refusal["summary"] == "tools/call arguments must be an object.", refusal
    assert service.calls == []


def test_the_services_own_list_refusal_reads_the_same_way(tmp_path: Path) -> None:
    """One layer down, `AgenticHILToolService.call` refuses a list of arguments
    itself, for a caller that is not the MCP envelope. It names the same field
    and validator, and it carries the same remediation: two refusals for the
    one mistake must read alike."""
    service = real_service(tmp_path)
    try:
        refusal = service.call("bench_run_status", [1])  # type: ignore[arg-type]
    finally:
        service.close()

    assert refusal["ok"] is False, refusal
    assert_reads_like_the_catalogues_invalid_argument(refusal, tool="bench_run_status", field="$")
    assert refusal["summary"] == "Tool arguments must be an object.", refusal


# --- the envelope faults over one session -------------------------------------


def test_a_notification_is_answered_with_silence_and_envelope_faults_carry_their_codes() -> None:
    """One session, six lines in, five replies out: nothing for the notification,
    -32600 for a request without `jsonrpc: "2.0"`, -32601 naming the method the
    server does not serve, -32602 for params that are not an object, and a
    result-level refusal for a tools/call whose name is not a string that reads
    like every other invalid_argument: `field`, `validator`, remediation and
    do_not included."""
    service = RecordingService()
    session = lines(
        INITIALIZE,
        INITIALIZED,
        {"id": 7, "method": "ping"},
        {"jsonrpc": "2.0", "id": 8, "method": "resources/subscribe", "params": {"uri": "agentic-hil://reference/errors"}},
        tools_call(9, None, params=[1]),
        tools_call(10, 5),
    )

    exit_code, replies = serve(service, session)

    assert exit_code == 0
    assert [reply["id"] for reply in replies] == [1, 7, 8, 9, 10], replies
    assert "result" in replies[0], replies[0]
    assert replies[1]["error"]["code"] == JSONRPC_INVALID_REQUEST, replies[1]
    assert replies[2]["error"]["code"] == JSONRPC_METHOD_NOT_FOUND, replies[2]
    assert replies[2]["error"]["data"]["method"] == "resources/subscribe", replies[2]
    assert replies[3]["error"]["code"] == JSONRPC_INVALID_PARAMS, replies[3]
    assert replies[3]["error"]["data"]["summary"] == "JSON-RPC params must be an object.", replies[3]
    refusal = replies[4]["result"]
    assert refusal["isError"] is True, refusal
    assert_reads_like_the_catalogues_invalid_argument(refusal["structuredContent"], tool="unknown", field="name")
    assert refusal["structuredContent"]["summary"] == "tools/call requires a string name.", refusal
    assert service.calls == []


# --- shutdown ---------------------------------------------------------------------


class SerialFactory:
    """`serial.Serial` as the fake, keeping every handle it built for inspection."""

    def __init__(self) -> None:
        self.handles: list[FakeSerialHandle] = []

    def __call__(self, *args: object, **kwargs: object) -> FakeSerialHandle:
        handle = FakeSerialHandle()
        self.handles.append(handle)
        return handle


def com_service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[AgenticHILToolService, SerialFactory]:
    factory = SerialFactory()
    monkeypatch.setitem(sys.modules, "serial", SimpleNamespace(Serial=factory))
    return AgenticHILToolService(config_for(tmp_path, com_port=True), backend=FakeBackend()), factory


class InterruptedStdin:
    """A stdin that hands over its lines and then raises the way Ctrl-C does."""

    def __init__(self, text: str) -> None:
        self.stream = io.StringIO(text)

    def readline(self, limit: int = -1) -> str:
        line = self.stream.readline(limit)
        if not line:
            raise KeyboardInterrupt
        return line


def test_eof_on_stdin_closes_the_sessions_the_run_opened(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """When the host closes stdin the server closes the sessions it holds (so the
    operator's next session can open the port), refuses further calls as
    `service_closed`, and returns 0."""
    service, factory = com_service(tmp_path, monkeypatch)
    output = io.StringIO()

    exit_code = run_stdio_server(
        service.config,
        input_stream=io.StringIO(lines(tools_call(1, "com_session_start", {"port_id": PORT_ID}))),
        output_stream=output,
        tools=service,
    )

    assert exit_code == 0
    started = json.loads(output.getvalue().splitlines()[0])["result"]
    assert started["isError"] is False, started
    assert len(factory.handles) == 1, factory.handles
    assert factory.handles[0].is_open is False
    assert service.call("probe_target")["error_type"] == "service_closed"


class ClosingBackend(FakeBackend):
    """The probe backend, recording whether the service closed it."""

    def __init__(self) -> None:
        super().__init__()
        self.closed = False

    def close(self) -> None:
        self.closed = True


# This file's own bus id and channel, so its device locks contend with no
# sibling checkout's tests.
BUS_ID = "envelope_can"
CAN_BUSES_YAML = f'can_buses:\n  {BUS_ID}:\n    adapter: "socketcan"\n    channel: "vcan7"\n    fd: false\n    bitrate: 500000\n'
COM_PORTS_YAML = f'com_ports:\n  {PORT_ID}:\n    device: "{DEVICE}"\n'


def test_eof_on_stdin_closes_the_backend_and_the_can_session_too(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The whole of what the server holds goes back at EOF: the probe backend,
    the COM port and the CAN bus a run opened over the same loop, so nothing
    the host's next session needs is still held by a process that has ended."""
    written = write_config(tmp_path, com_ports_yaml=COM_PORTS_YAML, can_buses_yaml=CAN_BUSES_YAML)
    serial_factory = SerialFactory()
    monkeypatch.setitem(sys.modules, "serial", SimpleNamespace(Serial=serial_factory))
    buses: list[RecordingBus] = []
    monkeypatch.setitem(sys.modules, "can", fake_can_module(lambda **kwargs: buses.append(RecordingBus()) or buses[-1]))
    backend = ClosingBackend()
    service = AgenticHILToolService(load_config(str(written)), backend=backend)
    output = io.StringIO()

    exit_code = run_stdio_server(
        service.config,
        input_stream=io.StringIO(lines(tools_call(1, "com_session_start", {"port_id": PORT_ID}), tools_call(2, "can_session_start", {"bus_id": BUS_ID, "clear_rx_queue": False}))),
        output_stream=output,
        tools=service,
    )

    assert exit_code == 0
    replies = [json.loads(line)["result"] for line in output.getvalue().splitlines()]
    assert [reply["isError"] for reply in replies] == [False, False], replies
    assert len(serial_factory.handles) == 1 and serial_factory.handles[0].is_open is False
    assert len(buses) == 1 and buses[0].closed is True
    assert backend.closed is True
    assert service.call("probe_target")["error_type"] == "service_closed"


def test_an_interrupt_during_readline_still_closes_the_sessions_and_propagates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A KeyboardInterrupt while the server waits for the next line closes the
    port the run opened all the same, and is re-raised so the process ends the
    way an interrupt ends it rather than pretending a clean stop."""
    service, factory = com_service(tmp_path, monkeypatch)
    output = io.StringIO()

    with pytest.raises(KeyboardInterrupt):
        run_stdio_server(
            service.config,
            input_stream=InterruptedStdin(lines(tools_call(1, "com_session_start", {"port_id": PORT_ID}))),  # type: ignore[arg-type]
            output_stream=output,
            tools=service,
        )

    assert json.loads(output.getvalue().splitlines()[0])["result"]["isError"] is False
    assert len(factory.handles) == 1, factory.handles
    assert factory.handles[0].is_open is False
    assert service.call("probe_target")["error_type"] == "service_closed"


def test_a_cleanup_error_at_eof_is_raised_rather_than_a_clean_exit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A port that will not close at EOF is not a clean stop: the error leaves
    run_stdio_server so the process exits non-zero, instead of a 0 that says
    the bench was released when it was not."""
    service, _factory = com_service(tmp_path, monkeypatch)
    monkeypatch.setattr(service.com_ports, "close", lambda: (_ for _ in ()).throw(RuntimeError("port would not close")))

    with pytest.raises(RuntimeError, match="port would not close"):
        run_stdio_server(
            service.config,
            input_stream=io.StringIO(lines(tools_call(1, "com_session_start", {"port_id": PORT_ID}))),
            output_stream=io.StringIO(),
            tools=service,
        )


# --- the real server child over byte pipes ------------------------------------


def run_child(cwd: Path, stdin: bytes) -> subprocess.CompletedProcess[bytes]:
    """`agentic-hil mcp-stdio` as a host runs it: the entry point over two byte pipes."""
    return subprocess.run(
        [sys.executable, "-m", "agentic_hil", "mcp-stdio"],
        input=stdin,
        capture_output=True,
        cwd=str(cwd),
        env=dict(os.environ),
        timeout=180,
        check=False,
    )


def test_mcp_stdio_child_keeps_stdout_for_protocol_messages(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A healthy start over a provisioned workspace: every request is answered on
    stdout in order, the notification is not, stderr stays empty and EOF exits
    0. A print, a logging handler or a traceback added anywhere on the startup
    path lands on one of the two streams and fails this."""
    workspace = tmp_path / "project"
    write_authoritative_config(workspace, monkeypatch)
    session = lines(INITIALIZE, INITIALIZED, TOOLS_LIST, tools_call(3, "bench_run_status", {})).encode("utf-8")

    finished = run_child(workspace, session)

    assert finished.returncode == 0, finished.stderr.decode("utf-8", "replace")
    assert finished.stderr == b"", finished.stderr.decode("utf-8", "replace")
    replies = [json.loads(line) for line in finished.stdout.decode("utf-8").splitlines()]
    assert [reply["id"] for reply in replies] == [1, 2, 3], replies
    assert replies[0]["result"]["serverInfo"]["name"] == "agentic-hil", replies[0]
    assert any(tool["name"] == "bench_run_status" for tool in replies[1]["result"]["tools"]), replies[1]
    assert replies[2]["result"]["isError"] is False, replies[2]
    assert replies[2]["result"]["structuredContent"]["run_active"] is False, replies[2]


def test_mcp_stdio_child_writes_a_startup_refusal_to_stderr_and_nothing_to_stdout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The other arm of #458: a configuration bound to another workspace is refused
    before the server starts, as one redacted JSON document on stderr, with
    stdout empty and exit 1, so the host's framing never meets a document that
    is not a frame."""
    project = tmp_path / "project"
    other = tmp_path / "other-project"
    other.mkdir()
    config = write_authoritative_config(project, monkeypatch)
    session = lines(INITIALIZE, INITIALIZED, TOOLS_LIST).encode("utf-8")

    finished = run_child(other, session)

    assert finished.returncode == 1, finished.stdout + finished.stderr
    assert finished.stdout == b"", finished.stdout
    refusal = json.loads(finished.stderr.decode("utf-8"))
    assert refusal["ok"] is False, refusal
    assert refusal["error_type"] == "config_invalid", refusal
    assert Path(refusal["workspace_root"]).resolve() == project.resolve(), refusal
    assert Path(refusal["expected_workspace"]).resolve() == other.resolve(), refusal
    assert Path(refusal["path"]).resolve() == config.resolve(), refusal
