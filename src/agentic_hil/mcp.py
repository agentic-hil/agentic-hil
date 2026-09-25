from __future__ import annotations

import json
from typing import Any

from agentic_hil import __version__
from agentic_hil.contracts import MCP_TOOL_NAMES as MCP_TOOL_NAMES
from agentic_hil.contracts import MCP_TOOLS as MCP_TOOLS
from agentic_hil.contracts import invalid_argument
from agentic_hil.knowledge import MCP_RESOURCE_TEMPLATES as MCP_RESOURCE_TEMPLATES
from agentic_hil.knowledge import MCP_RESOURCES as MCP_RESOURCES
from agentic_hil.knowledge import read_resource
from agentic_hil.redact import redact_sensitive, redact_stream_text
from agentic_hil.report import overall_success
from agentic_hil.tools import AgenticHILToolService, UnprovisionedToolService
from agentic_hil.types import JsonObject

MCP_PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_MCP_PROTOCOL_VERSIONS = {"2024-11-05", "2025-03-26", "2025-06-18"}

# Delivered with initialize, which is the only thing this server can say before
# the agent decides anything. A refusal and a skill both arrive too late for a
# caller that reaches for a shell first: measured, a small model ran st-flash
# before it had called a single tool here.
#
# It is also what an agent host puts into the system prompt of every request of
# every session this server is registered in, whether or not that session ever
# touches a board, so it holds at most 1,800 characters: what has to precede a
# first call and nothing else. A stale file, a missing one and a refused
# widening are each said by the result that meets them, with its catalogue
# entry, to the session that meets them.
SERVER_INSTRUCTIONS = (
    "This project's target board is reachable only through these tools. Every request that touches it (flashing, "
    "resetting, probing, debugging, UART or CAN traffic, firmware artifacts, test reports) is answered by calling "
    "them, before reaching for a shell.\n"
    "Never substitute openocd, pyocd, st-flash, st-info, st-util, JLinkExe, gdb, screen, minicom, picocom, cansend, "
    "candump, a Makefile target that runs one of them, or direct access to /dev/tty*, COM* or a SocketCAN interface: "
    "they bypass the policy these tools enforce, and the operator cannot audit them.\n"
    "A permission_denied result is the answer: report the denied permission and stop. Never edit the authoritative "
    "configuration to grant yourself a permission (it belongs to the operator), and never carry out the action "
    "another way.\n"
    "The configuration changes only through project_config_describe and project_config_set, never through your own "
    "file tools.\n"
    "flash_firmware with reset_after_flash and capture flashes, resets and returns the UART output in one call; "
    "com_read waits for a pattern with until and can_read for a frame with until_id, so neither needs polling. "
    "bench_run_start and bench_run_stop are for a longer sequence driven call by call; a whole test plan runs "
    "through test_reactor_run.\n"
    "Result text leaves out fields at their default: an absent side_effect_status is not_started, absent "
    "cleanup_required and quarantined are false, absent audit_ok, cleanup_ok and target_ok are true, an absent "
    "hardware_state is unchanged. Catalogue advice this session already received is left out too; advice_uri says "
    "where to read it again.\n"
    "Facts about this server are published as resources (resources/list, agentic-hil://reference/...); read them "
    "instead of its source or its installed package."
)

# What a server with no configuration to bind says instead, in 500 characters at
# most. It is registered at user scope as often as not, so every request in every
# project without a board pays for this text. What a session needs once a
# configuration exists arrives with the result that created it and with each
# refusal after that.
UNPROVISIONED_SERVER_INSTRUCTIONS = (
    "This project has no Agentic HIL configuration yet. A request that touches a target board starts with "
    "project_config_create and then goes through these tools, before reaching for a shell. Never substitute openocd, "
    "pyocd, st-flash, JLinkExe, gdb, screen, minicom, candump, a Makefile target that runs one of them, or direct "
    "/dev/tty* or COM* access."
)

JSONRPC_PARSE_ERROR = -32700
JSONRPC_INVALID_REQUEST = -32600
JSONRPC_METHOD_NOT_FOUND = -32601
JSONRPC_INVALID_PARAMS = -32602
JSONRPC_INTERNAL_ERROR = -32603
# MCP-defined, not JSON-RPC: the code resources/read returns for a URI this
# server does not serve.
MCP_RESOURCE_NOT_FOUND = -32002

AGENTIC_HIL_WORKFLOW_PROMPT = """Use Agentic Hardware-in-the-Loop (Agentic HIL) as the safe gate to the configured embedded hardware.

Workflow:
1. Build the firmware first.
2. Check debugger availability with debugger_info if setup is unclear.
3. If multiple probes are attached, discover their IDs with debugger_probes_list before selecting one in the authoritative config.
4. Probe the target before flashing.
5. Flash only validated artifacts from configured allowed roots; flashing does not reset unless reset_after_flash is true.
6. Read structured results after every hardware action.
7. Use configured COM port and CAN bus ids only.
8. Continue only when ok is true; target_ok, audit_ok, and cleanup_ok are not false; cleanup_required and quarantined are not true; lease_state is one of null, active, or released (any other value, including stale, blocks success); side_effect_status is neither unknown nor partial; and hardware_state is not unknown.
9. On any composite failure, diagnose using error_type, backend_error_type, likely_causes, report_path, and log_path.
10. A failed call is not a held bench: an incident ends when the call that raised it ends, and the result's cleanup_reasons and quarantine_guidance say what was left unconfirmed. hardware_recover on such a bench answers nothing_to_recover: true, so it is not the thing to reach for after an ordinary failure. For hardware that is genuinely quarantined (error_type resource_quarantined, the audit-broken family), stop effects and read hardware_lease_status, then clear it with hardware_recover. A reason that names no hardware contact clears with no argument. A reason that needs somebody to look at the board needs operator_statement: ask the operator in chat what state the bench is in and pass their answer back verbatim, and never invent one. The audit-broken family, and any case with nobody to ask, keep the operator's own route: relay the recover --confirm-safe-state --quarantine-id <id> line the refusal hands you and let them run it. If recovery returns config_changed, show the operator both digests; once they confirm the delta, call hardware_recover again with accept_config_change: true, or relay their own --accept-config-change line.

Safety rules:
- Do not request raw OpenOCD or debugger commands.
- Do not request arbitrary shell access for hardware actions.
- Do not flash files outside configured artifact roots.
- Treat permission_denied as authoritative and stop.
- Never delete coordination state or retry around cleanup_required, quarantine, partial effects, or unknown hardware state.
"""

MCP_PROMPTS = [{"name": "agentic_hil_embedded_workflow", "description": "Safe workflow for using Agentic HIL hardware tools from an AI agent."}]


def server_instructions(tools: AgenticHILToolService | UnprovisionedToolService) -> str:
    """The instructions `initialize` sends, chosen by whether there is a configuration to serve.

    Asked of the service rather than of the file: an unprovisioned server binds
    the moment a configuration loads, so one whose file was written between its
    start and the host's first message answers every call as a configured server
    and introduces itself as one."""
    if isinstance(tools, UnprovisionedToolService) and tools.config is None:
        return UNPROVISIONED_SERVER_INSTRUCTIONS
    return SERVER_INSTRUCTIONS


def tool_result_text(payload: JsonObject) -> str:
    """The serialized form of a tool result for the content text block.

    That block exists only so a host that does not read structuredContent still
    gets the result (the MCP specification recommends servers return both). It
    is parsed, never read as prose, so it carries no indentation: the same
    payload without the whitespace nobody reads.
    """
    return json.dumps(payload, separators=(",", ":"))


def parse_error_response() -> JsonObject:
    return error_response(None, JSONRPC_PARSE_ERROR, "Parse error")


def oversized_message_response(max_message_chars: int) -> JsonObject:
    return error_response(None, JSONRPC_INVALID_REQUEST, "Request too large", {"max_message_chars": max_message_chars})


def handle_mcp_message(message: Any, tools: AgenticHILToolService) -> JsonObject | list[JsonObject] | None:
    if isinstance(message, list):
        if not message:
            return error_response(None, JSONRPC_INVALID_REQUEST, "Invalid Request")
        responses = [response for item in message if (response := handle_single_mcp_message(item, tools)) is not None]
        return responses or None
    return handle_single_mcp_message(message, tools)


def handle_single_mcp_message(message: Any, tools: AgenticHILToolService) -> JsonObject | None:
    if not isinstance(message, dict):
        return error_response(None, JSONRPC_INVALID_REQUEST, "Invalid Request")
    request_id = message.get("id")
    is_notification = "id" not in message
    if message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str):
        return None if is_notification else error_response(request_id, JSONRPC_INVALID_REQUEST, "Invalid Request")
    if is_notification:
        return None
    try:
        return handle_method(request_id, str(message["method"]), message.get("params", {}), tools)
    except InvalidParamsError as error:
        return error_response(request_id, JSONRPC_INVALID_PARAMS, "Invalid params", {"summary": fault_summary(error)})
    except Exception as error:
        # Every other exception, a TypeError or ValueError included, is a fault
        # behind a well-formed request. Answered as -32602 it told the agent to
        # correct arguments that were right; the code for the server's own
        # fault is -32603.
        return error_response(request_id, JSONRPC_INTERNAL_ERROR, "Internal error", {"summary": fault_summary(error)})


class InvalidParamsError(ValueError):
    """The request's params are not what JSON-RPC lets a method take: -32602.

    Raised by the envelope's own checks and by nothing else, so that the code
    for the caller's mistake is never handed out for an exception a tool let
    escape."""


def fault_summary(error: BaseException) -> str:
    """An exception's text, fit for the wire.

    It is whatever the failing code quoted, and a tool that failed against a
    package index quotes the index URL with its credential. It takes the same
    content pass a captured process stream takes, because it is the same kind
    of text: the tool's words, not a summary this server wrote."""
    return redact_stream_text(str(error))


def handle_method(request_id: Any, method: str, params: Any, tools: AgenticHILToolService) -> JsonObject:
    if method == "initialize":
        params_object = params_object_or_throw(params)
        requested_version = params_object.get("protocolVersion")
        negotiated_version = requested_version if requested_version in SUPPORTED_MCP_PROTOCOL_VERSIONS else MCP_PROTOCOL_VERSION
        return result_response(request_id, {"protocolVersion": negotiated_version, "capabilities": {"tools": {"listChanged": False}, "prompts": {"listChanged": False}, "resources": {"subscribe": False, "listChanged": False}}, "serverInfo": {"name": "agentic-hil", "version": __version__}, "instructions": server_instructions(tools)})
    if method == "ping":
        return result_response(request_id, {})
    if method == "tools/list":
        return result_response(request_id, {"tools": MCP_TOOLS})
    if method == "tools/call":
        return result_response(request_id, call_tool(params, tools))
    if method == "prompts/list":
        return result_response(request_id, {"prompts": MCP_PROMPTS})
    if method == "prompts/get":
        return result_response(request_id, get_prompt(params))
    if method == "resources/list":
        return result_response(request_id, {"resources": MCP_RESOURCES})
    if method == "resources/templates/list":
        # The per-error and per-backend entries are templates, not resources: the
        # error catalogue is keyed by error_type and optional scope, so listing
        # every key would put dozens of near-identical entries in front of a
        # caller who wants exactly the one its result named.
        return result_response(request_id, {"resourceTemplates": MCP_RESOURCE_TEMPLATES})
    if method == "resources/read":
        return read_resource_response(request_id, params)
    return error_response(request_id, JSONRPC_METHOD_NOT_FOUND, "Method not found", {"method": method})


def read_resource_response(request_id: Any, params: Any) -> JsonObject:
    params_object = params_object_or_throw(params)
    uri = params_object.get("uri")
    if not isinstance(uri, str) or not uri:
        return error_response(request_id, JSONRPC_INVALID_PARAMS, "Invalid params", {"summary": "resources/read requires a string uri."})
    contents = read_resource(uri)
    if contents is None:
        return error_response(request_id, MCP_RESOURCE_NOT_FOUND, "Resource not found", {"uri": uri})
    return result_response(request_id, {"contents": [contents]})


def call_tool(params: Any, tools: AgenticHILToolService) -> JsonObject:
    params_object = params_object_or_throw(params)
    name = params_object.get("name")
    arguments = params_object.get("arguments", {})
    # The envelope's own two refusals are built where every schema refusal is
    # built, so they carry the field, the validator and the catalogue's fix the
    # agent reads together on every other invalid_argument.
    if not isinstance(name, str):
        return tool_error_result(invalid_argument("unknown", "name", "type", "tools/call requires a string name."))
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        return tool_error_result(invalid_argument(name, "$", "type", "tools/call arguments must be an object."))
    result = tools.call(name, arguments)
    # Defense-in-depth: strip any secret-named field before the result is
    # serialized into the MCP content text and structuredContent. isError is
    # computed from the raw result (redaction touches no success field).
    safe_result = redact_sensitive(result)
    return {"content": [{"type": "text", "text": tool_result_text(safe_result)}], "structuredContent": safe_result, "isError": not overall_success(result)}


def get_prompt(params: Any) -> JsonObject:
    params_object = params_object_or_throw(params)
    if params_object.get("name") != "agentic_hil_embedded_workflow":
        text = "Unknown Agentic HIL prompt. Use agentic_hil_embedded_workflow."
        return {"description": "Unknown Agentic HIL prompt.", "messages": [{"role": "user", "content": {"type": "text", "text": text}}]}
    return {"description": "Safe workflow for using Agentic HIL hardware tools from an AI agent.", "messages": [{"role": "user", "content": {"type": "text", "text": AGENTIC_HIL_WORKFLOW_PROMPT}}]}


def params_object_or_throw(params: Any) -> JsonObject:
    if params is None:
        return {}
    if isinstance(params, dict):
        return params
    raise InvalidParamsError("JSON-RPC params must be an object.")


def tool_error_result(result: JsonObject) -> JsonObject:
    """A refusal the envelope raised itself, in the shape of a failed tool result."""
    return {"content": [{"type": "text", "text": tool_result_text(result)}], "structuredContent": result, "isError": True}


def result_response(request_id: Any, result: JsonObject) -> JsonObject:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def error_response(request_id: Any, code: int, message: str, data: JsonObject | None = None) -> JsonObject:
    error: JsonObject = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}
