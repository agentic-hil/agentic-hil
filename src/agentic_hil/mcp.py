from __future__ import annotations

import json
from typing import Any

from agentic_hil import __version__
from agentic_hil.contracts import MCP_TOOL_NAMES as MCP_TOOL_NAMES
from agentic_hil.contracts import MCP_TOOLS as MCP_TOOLS
from agentic_hil.report import overall_success
from agentic_hil.tools import AgenticHILToolService
from agentic_hil.types import JsonObject

MCP_PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_MCP_PROTOCOL_VERSIONS = {"2024-11-05", "2025-03-26", "2025-06-18"}

JSONRPC_PARSE_ERROR = -32700
JSONRPC_INVALID_REQUEST = -32600
JSONRPC_METHOD_NOT_FOUND = -32601
JSONRPC_INVALID_PARAMS = -32602
JSONRPC_INTERNAL_ERROR = -32603

AGENTIC_HIL_WORKFLOW_PROMPT = """Use Agentic Hardware-in-the-Loop (Agentic HIL) as the safe gate to the configured embedded hardware.

Workflow:
1. Build the firmware first.
2. Check debugger availability with debugger_info if setup is unclear.
3. If multiple probes are attached, discover their IDs with debugger_probes_list before selecting one in the authoritative config.
4. Probe the target before flashing.
5. Flash only validated artifacts from configured allowed roots; flashing does not reset unless reset_after_flash is true.
6. Read structured results after every hardware action.
7. Use configured COM port, CAN bus, and test-adapter ids only.
8. Continue only when ok is true, target_ok is not false, and audit_ok is not false.
9. On any composite failure, diagnose using error_type, backend_error_type, likely_causes, report_path, and log_path.

Safety rules:
- Do not request raw OpenOCD or debugger commands.
- Do not request arbitrary shell access for hardware actions.
- Do not flash files outside configured artifact roots.
- Treat permission_denied as authoritative and stop.
"""

MCP_PROMPTS = [{"name": "agentic_hil_embedded_workflow", "description": "Safe workflow for using Agentic HIL hardware tools from an AI agent."}]


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
        return result_response(request_id, {"protocolVersion": negotiated_version, "capabilities": {"tools": {"listChanged": False}, "prompts": {"listChanged": False}, "resources": {"subscribe": False, "listChanged": False}}, "serverInfo": {"name": "agentic-hil", "version": __version__}})
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
    if method in {"resources/list", "resources/templates/list"}:
        return result_response(request_id, {"resourceTemplates" if method == "resources/templates/list" else "resources": []})
    return error_response(request_id, JSONRPC_METHOD_NOT_FOUND, "Method not found", {"method": method})


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
    return {"content": [{"type": "text", "text": json.dumps(result, indent=2)}], "structuredContent": result, "isError": not overall_success(result)}


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
    return {"content": [{"type": "text", "text": json.dumps(result, indent=2)}], "structuredContent": result, "isError": True}


def result_response(request_id: Any, result: JsonObject) -> JsonObject:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def error_response(request_id: Any, code: int, message: str, data: JsonObject | None = None) -> JsonObject:
    error: JsonObject = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}
