from __future__ import annotations

import json
from typing import Any

from hardci import __version__
from hardci.tools import HardCIToolService
from hardci.types import JsonObject

MCP_PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_MCP_PROTOCOL_VERSIONS = {"2024-11-05", "2025-03-26", "2025-06-18"}

JSONRPC_PARSE_ERROR = -32700
JSONRPC_INVALID_REQUEST = -32600
JSONRPC_METHOD_NOT_FOUND = -32601
JSONRPC_INVALID_PARAMS = -32602
JSONRPC_INTERNAL_ERROR = -32603

EMPTY_OBJECT_SCHEMA: JsonObject = {"type": "object", "properties": {}, "additionalProperties": False}

MCP_TOOL_NAMES = [
    "hardci_debugger_info",
    "hardci_probe_target",
    "hardci_artifact_upload",
    "hardci_flash_firmware",
    "hardci_reset_target",
    "hardci_debug_start_session",
    "hardci_debug_stop_session",
    "hardci_debug_get_session_status",
    "hardci_debug_set_breakpoint",
    "hardci_debug_list_breakpoints",
    "hardci_debug_clear_breakpoints",
    "hardci_debug_continue",
    "hardci_debug_halt",
    "hardci_debug_get_stop_reason",
    "hardci_debug_symbol_info",
    "hardci_debug_dump_symbol_ihex",
    "hardci_get_last_report",
    "hardci_classify_last_error",
    "hardci_com_ports_list",
    "hardci_com_session_start",
    "hardci_com_session_stop",
    "hardci_com_write",
    "hardci_com_read",
    "hardci_can_buses_list",
    "hardci_can_session_start",
    "hardci_can_session_stop",
    "hardci_can_send",
    "hardci_can_read",
    "hardci_adapters_list",
    "hardci_adapter_session_start",
    "hardci_adapter_session_stop",
    "hardci_adapter_set_value",
    "hardci_adapter_inject_fault",
    "hardci_adapter_clear_fault",
    "hardci_adapter_measure",
]

MCP_TOOLS: list[JsonObject] = [
    {"name": "hardci_debugger_info", "description": "Check whether the configured debugger backend is available.", "inputSchema": EMPTY_OBJECT_SCHEMA},
    {"name": "hardci_probe_target", "description": "Probe the configured embedded target through the configured debugger.", "inputSchema": EMPTY_OBJECT_SCHEMA},
    {"name": "hardci_artifact_upload", "description": "Upload a local or base64-encoded firmware artifact into the configured HardCI artifact store.", "inputSchema": {"type": "object", "properties": {"image_path": {"type": "string"}, "filename": {"type": "string"}, "data_base64": {"type": "string"}}, "oneOf": [{"required": ["image_path"]}, {"required": ["filename", "data_base64"]}], "additionalProperties": False}},
    {"name": "hardci_flash_firmware", "description": "Flash a validated firmware artifact. Provide exactly one of image_path or artifact_id.", "inputSchema": {"type": "object", "properties": {"image_path": {"type": "string"}, "artifact_id": {"type": "string"}}, "oneOf": [{"required": ["image_path"]}, {"required": ["artifact_id"]}], "additionalProperties": False}},
    {"name": "hardci_reset_target", "description": "Reset the configured target through the configured debugger.", "inputSchema": {"type": "object", "properties": {"mode": {"type": "string", "enum": ["run", "halt", "init"], "default": "run"}}, "additionalProperties": False}},
    {"name": "hardci_debug_start_session", "description": "Start a typed debug session for a validated ELF artifact.", "inputSchema": {"type": "object", "properties": {"image_path": {"type": "string"}, "artifact_id": {"type": "string"}, "mode": {"type": "string", "enum": ["attach", "reset_halt", "load"], "default": "attach"}, "timeout_s": {"type": "number", "minimum": 0}}, "oneOf": [{"required": ["image_path"]}, {"required": ["artifact_id"]}], "additionalProperties": False}},
    {"name": "hardci_debug_stop_session", "description": "Stop the active typed debug session.", "inputSchema": {"type": "object", "properties": {"timeout_s": {"type": "number", "minimum": 0}}, "additionalProperties": False}},
    {"name": "hardci_debug_get_session_status", "description": "Return active debug-session status.", "inputSchema": EMPTY_OBJECT_SCHEMA},
    {"name": "hardci_debug_set_breakpoint", "description": "Set a typed breakpoint by symbol/function name or file and line.", "inputSchema": {"type": "object", "properties": {"location": {"oneOf": [{"type": "string"}, {"type": "object"}]}}, "required": ["location"], "additionalProperties": False}},
    {"name": "hardci_debug_list_breakpoints", "description": "List breakpoints in the active debug session.", "inputSchema": EMPTY_OBJECT_SCHEMA},
    {"name": "hardci_debug_clear_breakpoints", "description": "Clear all breakpoints from the active debug session.", "inputSchema": EMPTY_OBJECT_SCHEMA},
    {"name": "hardci_debug_continue", "description": "Continue target execution until stop or timeout.", "inputSchema": {"type": "object", "properties": {"timeout_s": {"type": "number", "minimum": 0}}, "additionalProperties": False}},
    {"name": "hardci_debug_halt", "description": "Halt the target in the active debug session.", "inputSchema": {"type": "object", "properties": {"timeout_s": {"type": "number", "minimum": 0}}, "additionalProperties": False}},
    {"name": "hardci_debug_get_stop_reason", "description": "Return the last structured stop reason.", "inputSchema": EMPTY_OBJECT_SCHEMA},
    {"name": "hardci_debug_symbol_info", "description": "Resolve an allowed debug symbol.", "inputSchema": {"type": "object", "properties": {"symbol": {"type": "string"}}, "required": ["symbol"], "additionalProperties": False}},
    {"name": "hardci_debug_dump_symbol_ihex", "description": "Read an allowed symbol from target memory and write Intel HEX.", "inputSchema": {"type": "object", "properties": {"symbol": {"type": "string"}, "output_path": {"type": "string"}}, "required": ["symbol", "output_path"], "additionalProperties": False}},
    {"name": "hardci_get_last_report", "description": "Return the most recent structured HardCI report.", "inputSchema": EMPTY_OBJECT_SCHEMA},
    {"name": "hardci_classify_last_error", "description": "Classify the most recent HardCI/debugger failure.", "inputSchema": EMPTY_OBJECT_SCHEMA},
    {"name": "hardci_com_ports_list", "description": "List configured named COM ports and detected host serial ports.", "inputSchema": EMPTY_OBJECT_SCHEMA},
    {"name": "hardci_com_session_start", "description": "Open a configured COM port and start a background feedback session.", "inputSchema": {"type": "object", "properties": {"port_id": {"type": "string"}, "clear_buffer": {"type": "boolean", "default": True}}, "required": ["port_id"], "additionalProperties": False}},
    {"name": "hardci_com_session_stop", "description": "Stop a configured COM port session.", "inputSchema": {"type": "object", "properties": {"port_id": {"type": "string"}}, "required": ["port_id"], "additionalProperties": False}},
    {"name": "hardci_com_write", "description": "Write text or hex stimulus to an active COM port session.", "inputSchema": {"type": "object", "properties": {"port_id": {"type": "string"}, "text": {"type": "string"}, "hex": {"type": "string"}}, "required": ["port_id"], "oneOf": [{"required": ["text"]}, {"required": ["hex"]}], "additionalProperties": False}},
    {"name": "hardci_com_read", "description": "Read buffered feedback from an active COM port session.", "inputSchema": {"type": "object", "properties": {"port_id": {"type": "string"}, "max_bytes": {"type": "integer", "minimum": 1}, "wait_timeout_s": {"type": "number", "minimum": 0, "default": 0}}, "required": ["port_id"], "additionalProperties": False}},
    {"name": "hardci_can_buses_list", "description": "List configured named CAN buses and active session status.", "inputSchema": EMPTY_OBJECT_SCHEMA},
    {"name": "hardci_can_session_start", "description": "Open a configured CAN bus session.", "inputSchema": {"type": "object", "properties": {"bus_id": {"type": "string"}, "clear_rx_queue": {"type": "boolean", "default": True}}, "required": ["bus_id"], "additionalProperties": False}},
    {"name": "hardci_can_session_stop", "description": "Stop a configured CAN bus session.", "inputSchema": {"type": "object", "properties": {"bus_id": {"type": "string"}}, "required": ["bus_id"], "additionalProperties": False}},
    {"name": "hardci_can_send", "description": "Send one classic CAN frame on an active configured CAN bus session.", "inputSchema": {"type": "object", "properties": {"bus_id": {"type": "string"}, "frame_id": {"oneOf": [{"type": "integer", "minimum": 0}, {"type": "string"}]}, "extended": {"type": "boolean", "default": False}, "rtr": {"type": "boolean", "default": False}, "data_hex": {"type": "string", "default": ""}}, "required": ["bus_id", "frame_id"], "additionalProperties": False}},
    {"name": "hardci_can_read", "description": "Read CAN frames from an active configured CAN bus session.", "inputSchema": {"type": "object", "properties": {"bus_id": {"type": "string"}, "max_frames": {"type": "integer", "minimum": 1}, "wait_timeout_s": {"type": "number", "minimum": 0, "default": 0}}, "required": ["bus_id"], "additionalProperties": False}},
    {"name": "hardci_adapters_list", "description": "List configured test adapters (sensor/actuator/fault simulation) and session status.", "inputSchema": EMPTY_OBJECT_SCHEMA},
    {"name": "hardci_adapter_session_start", "description": "Start a session with a configured test adapter bridge.", "inputSchema": {"type": "object", "properties": {"adapter_id": {"type": "string"}}, "required": ["adapter_id"], "additionalProperties": False}},
    {"name": "hardci_adapter_session_stop", "description": "Stop a configured test adapter session.", "inputSchema": {"type": "object", "properties": {"adapter_id": {"type": "string"}}, "required": ["adapter_id"], "additionalProperties": False}},
    {"name": "hardci_adapter_set_value", "description": "Set a configured test adapter channel to a value (e.g. simulated sensor temperature).", "inputSchema": {"type": "object", "properties": {"adapter_id": {"type": "string"}, "channel": {"type": "string"}, "value": {"type": "number"}, "unit": {"type": "string"}}, "required": ["adapter_id", "channel", "value"], "additionalProperties": False}},
    {"name": "hardci_adapter_inject_fault", "description": "Inject a configured fault state (e.g. open sensor, short to GND) on a test adapter.", "inputSchema": {"type": "object", "properties": {"adapter_id": {"type": "string"}, "fault": {"type": "string"}, "channel": {"type": "string"}}, "required": ["adapter_id", "fault"], "additionalProperties": False}},
    {"name": "hardci_adapter_clear_fault", "description": "Clear an injected fault (or all faults) on a test adapter.", "inputSchema": {"type": "object", "properties": {"adapter_id": {"type": "string"}, "fault": {"type": "string"}, "channel": {"type": "string"}}, "required": ["adapter_id"], "additionalProperties": False}},
    {"name": "hardci_adapter_measure", "description": "Measure a configured test adapter channel and return the structured value.", "inputSchema": {"type": "object", "properties": {"adapter_id": {"type": "string"}, "channel": {"type": "string"}}, "required": ["adapter_id", "channel"], "additionalProperties": False}},
]

HARDCI_WORKFLOW_PROMPT = """Use HardCI as the safe gate to the configured embedded hardware.

Workflow:
1. Build the firmware first.
2. Check debugger availability with hardci_debugger_info if setup is unclear.
3. Probe the target before flashing.
4. Flash only validated artifacts from configured allowed roots.
5. Read structured results after every hardware action.
6. Use configured COM port ids, CAN bus ids, adapter ids, channel names, and fault names only.
7. If ok is false, diagnose using error_type, backend_error_type, likely_causes, report_path, and log_path.

Safety rules:
- Do not request raw OpenOCD or debugger commands.
- Do not request arbitrary shell access for hardware actions.
- Do not flash files outside configured artifact roots.
- Treat permission_denied as authoritative and stop.
"""

MCP_PROMPTS = [{"name": "hardci_embedded_workflow", "description": "Safe workflow for using HardCI hardware tools from an AI agent."}]


def parse_error_response() -> JsonObject:
    return error_response(None, JSONRPC_PARSE_ERROR, "Parse error")


def oversized_message_response(max_message_chars: int) -> JsonObject:
    return error_response(None, JSONRPC_INVALID_REQUEST, "Request too large", {"max_message_chars": max_message_chars})


def handle_mcp_message(message: Any, tools: HardCIToolService) -> JsonObject | list[JsonObject] | None:
    if isinstance(message, list):
        if not message:
            return error_response(None, JSONRPC_INVALID_REQUEST, "Invalid Request")
        responses = [response for item in message if (response := handle_single_mcp_message(item, tools)) is not None]
        return responses or None
    return handle_single_mcp_message(message, tools)


def handle_single_mcp_message(message: Any, tools: HardCIToolService) -> JsonObject | None:
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


def handle_method(request_id: Any, method: str, params: Any, tools: HardCIToolService) -> JsonObject:
    if method == "initialize":
        params_object = params_object_or_throw(params)
        requested_version = params_object.get("protocolVersion")
        negotiated_version = requested_version if requested_version in SUPPORTED_MCP_PROTOCOL_VERSIONS else MCP_PROTOCOL_VERSION
        return result_response(request_id, {"protocolVersion": negotiated_version, "capabilities": {"tools": {"listChanged": False}, "prompts": {"listChanged": False}, "resources": {"subscribe": False, "listChanged": False}}, "serverInfo": {"name": "hardci", "version": __version__}})
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


def call_tool(params: Any, tools: HardCIToolService) -> JsonObject:
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
    return {"content": [{"type": "text", "text": json.dumps(result, indent=2)}], "structuredContent": result, "isError": result.get("ok") is False}


def get_prompt(params: Any) -> JsonObject:
    params_object = params_object_or_throw(params)
    if params_object.get("name") != "hardci_embedded_workflow":
        text = "Unknown HardCI prompt. Use hardci_embedded_workflow."
        return {"description": "Unknown HardCI prompt.", "messages": [{"role": "user", "content": {"type": "text", "text": text}}]}
    return {"description": "Safe workflow for using HardCI hardware tools from an AI agent.", "messages": [{"role": "user", "content": {"type": "text", "text": HARDCI_WORKFLOW_PROMPT}}]}


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
