from __future__ import annotations

import json
import weakref
from typing import Any

from agentic_hil import __version__
from agentic_hil.contracts import MCP_TOOL_NAMES as MCP_TOOL_NAMES
from agentic_hil.contracts import MCP_TOOLS as MCP_TOOLS
from agentic_hil.contracts import invalid_argument
from agentic_hil.knowledge import ERROR_CATALOGUE, ERROR_URI_PREFIX, read_resource, remediation_fields
from agentic_hil.knowledge import MCP_RESOURCE_TEMPLATES as MCP_RESOURCE_TEMPLATES
from agentic_hil.knowledge import MCP_RESOURCES as MCP_RESOURCES
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


# The top-level pairs the text of a result leaves out, because each only
# restates what a reader assumes when it is absent: nothing was committed or
# started, the hardware is as it was, nothing is held, and every check passed.
# The same field holding any other value says something and is kept, and so is
# every field not named here, `ok` and `retry_safe` included.
TEXT_DEFAULTS: dict[str, object] = {
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

# The top-level advice lists whose entries a session is sent once.
# `quarantine_guidance` is not one of them: no resource serves it again, and it
# is what a caller needs to recover the bench.
REPEATED_ADVICE_FIELDS = ("remediation", "likely_causes")

# What each tool service has sent, per advice field. One stdio loop serves one
# service, so this is one MCP session's memory: held in this process only, gone
# with the service, and never shared with another one.
_SENT_ADVICE: weakref.WeakKeyDictionary[Any, dict[str, set[str]]] = weakref.WeakKeyDictionary()


def sent_advice(tools: Any) -> dict[str, set[str]] | None:
    """The advice entries already sent in the session ``tools`` serves, per field.

    None for a service that cannot be referenced weakly, which is then sent
    every entry every time: a memory keyed any other way could outlive the
    service and hand a later one what an earlier session was told."""
    try:
        return _SENT_ADVICE.setdefault(tools, {})
    except TypeError:
        return None


def tool_result_text(payload: JsonObject, sent: dict[str, set[str]] | None = None) -> str:
    """The content text block of a tool result: a compact projection of it.

    The text is what an agent host puts into the model's context, where it stays
    for the rest of the session and is paid for again on every later request, so
    it carries what says something and nothing else. It is one JSON object
    without whitespace that always keeps `ok` and `tool`. A key whose value is
    null, "", [] or {} is left out at any depth, and so is a nested object left
    with no keys once its own empty keys are. An array keeps every element in
    its place, an object inside one as {} when nothing of it is left. At the top
    level, the pairs in `TEXT_DEFAULTS` are left out where they only restate
    their default.

    A top-level remediation or likely_causes entry this session was already sent
    in the same field is left out as well, and counted under `repeated_advice`.
    `advice_uri` names the error catalogue entry that serves the remediation left
    out, where one does. ``sent`` is that memory, and this adds the block's
    entries to it once the block is built, so nothing is left out within one
    block; without it, nothing is left out at all. Advice nested deeper, such as
    a step's own result inside a run, and `quarantine_guidance` are never left
    out.

    structuredContent stays the whole result. It is the document itself, for the
    hosts and programs that read fields rather than the model's context, and
    `isError` is decided from the same result; a default, an empty value and
    advice the session already has are all still there for a reader that needs
    them, so nothing the text leaves out is lost.
    """
    if not isinstance(payload, dict):
        return json.dumps(payload, separators=(",", ":"))
    text: JsonObject = {}
    repeated: dict[str, int] = {}
    left_out_remediation: list[Any] = []
    taken: dict[str, list[str]] = {}
    for key, value in payload.items():
        if key in ("ok", "tool"):
            text[key] = value
            continue
        if key in TEXT_DEFAULTS and _same(TEXT_DEFAULTS[key], value):
            continue
        value = _without_empty(value)
        if _is_empty(value):
            continue
        if sent is not None and key in REPEATED_ADVICE_FIELDS and isinstance(value, list):
            already = sent.get(key, set())
            identities = [json.dumps(entry, separators=(",", ":")) for entry in value]
            taken[key] = identities
            kept = [entry for entry, identity in zip(value, identities, strict=True) if identity not in already]
            if len(kept) < len(value):
                repeated[key] = len(value) - len(kept)
                if key == "remediation":
                    left_out_remediation = [entry for entry, identity in zip(value, identities, strict=True) if identity in already]
            if not kept:
                continue
            value = kept
        text[key] = value
    if repeated:
        text["repeated_advice"] = repeated
        uri = _advice_uri(payload, left_out_remediation)
        if uri is not None:
            text["advice_uri"] = uri
    serialized = json.dumps(text, separators=(",", ":"))
    if sent is not None:
        for key, identities in taken.items():
            sent.setdefault(key, set()).update(identities)
    return serialized


def _same(expected: object, value: object) -> bool:
    """Equal and of the same type, so a default `false` is not matched by a `0`."""
    return type(value) is type(expected) and value == expected


def _is_empty(value: Any) -> bool:
    return value is None or (isinstance(value, (str, list, tuple, dict)) and not value)


def _without_empty(value: Any) -> Any:
    """``value`` with every object's empty keys left out, worked from the bottom up.

    An array element is never removed or replaced, because its position means
    something: an object inside an array loses its own empty keys and stays in
    its place, as {} when none are left."""
    if isinstance(value, dict):
        projected = ((key, _without_empty(child)) for key, child in value.items())
        return {key: child for key, child in projected if not _is_empty(child)}
    if isinstance(value, (list, tuple)):
        return [_without_empty(child) for child in value]
    return value


def _advice_uri(payload: JsonObject, left_out: list[Any]) -> str | None:
    """The URI of the catalogue entry that serves every remediation entry left out.

    A result does not say which entry its advice came from, so each entry of its
    error_type is rendered the way the services render it, with the result's own
    permission key where the entry is written around one. The entry whose advice
    the result carries whole is named; failing that, the first that holds every
    entry left out. None when no entry does: the text never points at advice
    that is not there to read."""
    error_type = payload.get("error_type")
    if not left_out or not isinstance(error_type, str) or not error_type:
        return None
    permission = payload.get("permission")
    permission = permission if isinstance(permission, str) and permission else None
    holding: list[str] = []
    for key in ERROR_CATALOGUE:
        entry_type, _, scope = key.partition(":")
        if entry_type != error_type:
            continue
        advice = remediation_fields(error_type, scope or None, permission=permission)
        steps = advice.get("remediation", [])
        if all(entry in steps for entry in left_out):
            if steps == payload.get("remediation") and advice.get("do_not") == payload.get("do_not"):
                return f"{ERROR_URI_PREFIX}{key}"
            holding.append(key)
    return f"{ERROR_URI_PREFIX}{holding[0]}" if holding else None


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
    sent = sent_advice(tools)
    # The envelope's own two refusals are built where every schema refusal is
    # built, so they carry the field, the validator and the catalogue's fix the
    # agent reads together on every other invalid_argument.
    if not isinstance(name, str):
        return tool_error_result(invalid_argument("unknown", "name", "type", "tools/call requires a string name."), sent)
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        return tool_error_result(invalid_argument(name, "$", "type", "tools/call arguments must be an object."), sent)
    result = tools.call(name, arguments)
    # Defense-in-depth: strip any secret-named field before the result is
    # serialized into the MCP content text and structuredContent. isError is
    # computed from the raw result (redaction touches no success field).
    safe_result = redact_sensitive(result)
    return {"content": [{"type": "text", "text": tool_result_text(safe_result, sent)}], "structuredContent": safe_result, "isError": not overall_success(result)}


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


def tool_error_result(result: JsonObject, sent: dict[str, set[str]] | None = None) -> JsonObject:
    """A refusal the envelope raised itself, in the shape of a failed tool result.

    Its text is projected like any tool's, against the same session's memory of
    the advice it was sent."""
    return {"content": [{"type": "text", "text": tool_result_text(result, sent)}], "structuredContent": result, "isError": True}


def result_response(request_id: Any, result: JsonObject) -> JsonObject:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def error_response(request_id: Any, code: int, message: str, data: JsonObject | None = None) -> JsonObject:
    error: JsonObject = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}
