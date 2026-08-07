from __future__ import annotations

import json
from typing import Any

from agentic_hil import __version__
from agentic_hil.contracts import MCP_TOOL_NAMES as MCP_TOOL_NAMES
from agentic_hil.contracts import MCP_TOOLS as MCP_TOOLS
from agentic_hil.knowledge import MCP_RESOURCE_TEMPLATES as MCP_RESOURCE_TEMPLATES
from agentic_hil.knowledge import MCP_RESOURCES as MCP_RESOURCES
from agentic_hil.knowledge import read_resource
from agentic_hil.redact import redact_sensitive
from agentic_hil.report import overall_success
from agentic_hil.tools import AgenticHILToolService
from agentic_hil.types import JsonObject

MCP_PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_MCP_PROTOCOL_VERSIONS = {"2024-11-05", "2025-03-26", "2025-06-18"}

# Delivered with initialize, which is the only thing this server can say before
# the agent decides anything. A refusal and a skill both arrive too late for a
# caller that reaches for a shell first: measured, a small model ran st-flash
# before it had called a single tool here.
SERVER_INSTRUCTIONS = (
    "This project's hardware is reachable only through these tools. Every request that would touch the "
    "target board — flashing, resetting, probing, debugging, UART or CAN traffic, "
    "firmware artifacts, test reports — is answered by calling one of them, before reaching for a "
    "shell.\n"
    "Never substitute openocd, pyocd, st-flash, st-info, st-util, JLinkExe, gdb, screen, minicom, "
    "picocom, cansend or candump, a Makefile target that runs one of them, or direct access to "
    "/dev/tty*, COM* or a SocketCAN interface. Those bypass the policy these tools enforce, and the "
    "operator cannot see or audit what they did.\n"
    "A sequence that spans several calls — flash, then reset, then read — is declared once with "
    "bench_run_start and closed with bench_run_stop. Without that declaration each call holds its device "
    "only for its own duration and the board is free in between; with it, the devices you named are yours "
    "for the whole run and are the only ones the run may touch.\n"
    "A permission_denied result is the answer to the request, not an obstacle: report it, name the "
    "permission that is denied, and stop. Never edit the authoritative configuration to grant "
    "yourself a permission — it belongs to the operator — and never carry out the action another way.\n"
    "Changing the configuration also goes through these tools, never through your own file tools. "
    "project_config_describe says which keys you may change in this state and which permission would "
    "open a locked one; project_config_set then sets named keys with scalar values. Two permissions "
    "gate it: allow_config_description_write for what the bench is — target, probe_id, port device and "
    "baudrate, CAN bus settings — and allow_config_permissions_write for every permission key, which is "
    "each permissions: block plus the two grants that sit directly on a section, artifacts.allow_upload "
    "and debug.allow_all_symbols. Read "
    "agentic-hil://reference/config-shape before guessing a key.\n"
    "If the configuration holds placeholders because the board was plugged in after it was written — "
    "probe_id: null, executable: null, controller: unknown-controller — call "
    "project_config_adopt_hardware instead of printing values for a person to retype. It reads the "
    "attached probe, fills in only the identity keys that are still unset, reports any key that already "
    "holds somebody's value rather than replacing it, and writes only with apply: true.\n"
    "This server parses that configuration once, at startup, and does not reload it while it runs. A result carrying "
    "config_stale: true says the file is no longer the one it is enforcing — the backend it names and the permissions "
    "it enforces are not the ones in the file now. config_status.description_source says which document they are: "
    "`startup` for all of it, or `description_reload` when a project_config_reload_description has since moved the "
    "devices and the backend onto a newer document while the permissions stayed the startup ones. What to do about it is decided by "
    "config_status.state and is not the same for all three: `changed` — the file on disk differs from the one this "
    "server loaded; `missing` — the file is gone, so it has to be "
    "restored before there is anything to restart onto; `unreadable` — it is there and will not open, so it has to be "
    "made readable first. Never ask for a restart on `missing` or `unreadable` before the file has been restored or "
    "made readable. The status compares two digests and claims nothing beyond that — it does not say what the file "
    "now contains, and it does not promise a restart will succeed.\n"
    "On `changed` there are two ways forward and they cover different halves of the file. If what moved is the "
    "description of the bench — target, a debuggers/com_ports/can_buses entry, a probe id, a COM device, a baudrate — "
    "call project_config_reload_description. It takes no arguments, re-reads exactly those four sections, and clears "
    "the staleness for them without a restart. It re-reads no permission at all, in either direction: a device this "
    "server has never seen arrives with no grant, so you can probe and read it and cannot flash, reset, mass-erase or "
    "write to it until an operator restarts the server, and the result says so. Everything else — every permission, "
    "version, workspace_root, state_root, debug, artifacts, validation, recovery, reports, logs — is adopted by a "
    "restart and by nothing else, so for those ask the operator to restart the MCP server once (the one the agent "
    "host started for this workspace, not the agentic-hil command line, which reads the file fresh every time and is "
    "already current) and report the startup error if it does not come back, because the file it found is then the "
    "thing to repair. Those two are the whole of it: do not try to make the server pick a change up by editing the "
    "file again or by calling a configuration write tool.\n"
    "If a tool answers config_file_not_found, this project has no configuration yet: call "
    "project_config_create. It takes no arguments, generates the file from the hardware attached to "
    "this machine, and every permission in it is true — flashing, reset, COM and CAN writes, artifact "
    "upload, unrestricted symbol access, and all three permissions.allow_config_* grants — except "
    "allow_raw_debugger_commands and allow_mass_erase, which are false. Those two are not capabilities "
    "you are missing: there is no tool here for either, and while either is true flash_firmware on that "
    "probe is refused. The bench is workable from that file without anybody editing YAML, flashing "
    "included. Report where it is and what it granted, and ask the operator which permissions this "
    "bench should not have — never ask for those two to be turned on.\n"
    "Permissions move one way here. project_config_set writes false into a permission and never any "
    "other value — not true into one you never touched, and not into one you set to false a moment "
    "earlier; a call that tries is refused as permission_widening_denied. So you can narrow this bench "
    "on the operator's word and can never widen it. Setting "
    "permissions.allow_config_permissions_write: false is the last permission change that tool can "
    "make; the result of that call says so. Regenerating is the operator's, with `agentic-hil init "
    "--force`; never delete or move a configuration to get a different one.\n"
    "Facts about this server are published as resources; read them instead of its source code or its "
    "installed package. resources/list carries agentic-hil://reference/debugger-backends (which config "
    "field each debugger backend requires, discovers, or ignores), .../target-support (which field "
    "names the target, which values are known good), .../errors (every error_type with its fix), "
    ".../platform-paths (where each file lives on each platform), .../lease-lifecycle, "
    ".../config-schema, and .../config-shape (what a configuration looks like and how to change it)."
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
10. For quarantined hardware, stop effects and ask the operator to inspect lease-status, physically confirm the current quarantine_id, and run recover --confirm-safe-state --quarantine-id <id>. If recovery returns config_changed, the operator verifies the config delta and reruns with --accept-config-change.

Safety rules:
- Do not request raw OpenOCD or debugger commands.
- Do not request arbitrary shell access for hardware actions.
- Do not flash files outside configured artifact roots.
- Treat permission_denied as authoritative and stop.
- Never delete coordination state or retry around cleanup_required, quarantine, partial effects, or unknown hardware state.
"""

MCP_PROMPTS = [{"name": "agentic_hil_embedded_workflow", "description": "Safe workflow for using Agentic HIL hardware tools from an AI agent."}]


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
    except (TypeError, ValueError) as error:
        return error_response(request_id, JSONRPC_INVALID_PARAMS, "Invalid params", {"summary": str(error)})
    except Exception as error:
        return error_response(request_id, JSONRPC_INTERNAL_ERROR, "Internal error", {"summary": str(error)})


def handle_method(request_id: Any, method: str, params: Any, tools: AgenticHILToolService) -> JsonObject:
    if method == "initialize":
        params_object = params_object_or_throw(params)
        requested_version = params_object.get("protocolVersion")
        negotiated_version = requested_version if requested_version in SUPPORTED_MCP_PROTOCOL_VERSIONS else MCP_PROTOCOL_VERSION
        return result_response(request_id, {"protocolVersion": negotiated_version, "capabilities": {"tools": {"listChanged": False}, "prompts": {"listChanged": False}, "resources": {"subscribe": False, "listChanged": False}}, "serverInfo": {"name": "agentic-hil", "version": __version__}, "instructions": SERVER_INSTRUCTIONS})
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
    if not isinstance(name, str):
        return mcp_tool_error("unknown", "invalid_argument", "tools/call requires a string name.")
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        return mcp_tool_error(name, "invalid_argument", "tools/call arguments must be an object.")
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
    raise TypeError("JSON-RPC params must be an object.")


def mcp_tool_error(tool: str, error_type: str, summary: str) -> JsonObject:
    result = {"ok": False, "tool": tool, "error_type": error_type, "summary": summary}
    return {"content": [{"type": "text", "text": tool_result_text(result)}], "structuredContent": result, "isError": True}


def result_response(request_id: Any, result: JsonObject) -> JsonObject:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def error_response(request_id: Any, code: int, message: str, data: JsonObject | None = None) -> JsonObject:
    error: JsonObject = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}
