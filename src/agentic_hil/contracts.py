from __future__ import annotations

import math
import re
from typing import Any

from jsonschema import Draft202012Validator

from agentic_hil.config import format_field_path
from agentic_hil.types import JsonObject

EMPTY_OBJECT_SCHEMA: JsonObject = {"type": "object", "properties": {}, "additionalProperties": False}
NONEMPTY_STRING: JsonObject = {"type": "string", "minLength": 1}
TIMEOUT: JsonObject = {"type": "number", "minimum": 0}
BREAKPOINT_LOCATION: JsonObject = {
    "oneOf": [
        {"type": "string", "pattern": r"^[A-Za-z_][A-Za-z0-9_]*(?:::[A-Za-z_][A-Za-z0-9_]*)*$"},
        {
            "type": "object",
            "additionalProperties": False,
            "required": ["symbol"],
            "properties": {"symbol": {"type": "string", "pattern": r"^[A-Za-z_][A-Za-z0-9_]*(?:::[A-Za-z_][A-Za-z0-9_]*)*$"}},
        },
        {
            "type": "object",
            "additionalProperties": False,
            "required": ["function"],
            "properties": {"function": {"type": "string", "pattern": r"^[A-Za-z_][A-Za-z0-9_]*(?:::[A-Za-z_][A-Za-z0-9_]*)*$"}},
        },
        {
            "type": "object",
            "additionalProperties": False,
            "required": ["file", "line"],
            "properties": {
                "file": {"type": "string", "pattern": r"^[A-Za-z0-9_./\\:-]+$", "not": {"pattern": r"(?:^|[/\\])\.\.(?:$|[/\\])"}},
                "line": {"type": "integer", "minimum": 1},
            },
        },
    ]
}


def object_schema(
    properties: JsonObject | None = None,
    *,
    required: list[str] | None = None,
    one_of: list[JsonObject] | None = None,
) -> JsonObject:
    schema: JsonObject = {"type": "object", "properties": properties or {}, "additionalProperties": False}
    if required:
        schema["required"] = required
    if one_of:
        schema["oneOf"] = one_of
    return schema


MCP_TOOLS: list[JsonObject] = [
    {"name": "debugger_info", "description": "Check whether the configured debugger backend is available.", "inputSchema": EMPTY_OBJECT_SCHEMA},
    {"name": "debugger_probes_list", "description": "List every connected probe ID visible to the configured debugger backend.", "inputSchema": EMPTY_OBJECT_SCHEMA},
    {"name": "probe_target", "description": "Probe the configured embedded target through the configured debugger.", "inputSchema": EMPTY_OBJECT_SCHEMA},
    {"name": "artifact_upload", "description": "Upload a local or base64-encoded firmware artifact into the configured Agentic HIL artifact store.", "inputSchema": object_schema({"image_path": NONEMPTY_STRING, "filename": NONEMPTY_STRING, "data_base64": NONEMPTY_STRING}, one_of=[{"required": ["image_path"]}, {"required": ["filename", "data_base64"]}])},
    {"name": "flash_firmware", "description": "Flash a validated firmware artifact. Provide exactly one of image_path or artifact_id.", "inputSchema": object_schema({"image_path": NONEMPTY_STRING, "artifact_id": NONEMPTY_STRING, "reset_after_flash": {"type": "boolean", "default": False}}, one_of=[{"required": ["image_path"]}, {"required": ["artifact_id"]}])},
    {"name": "reset_target", "description": "Reset the configured target through the configured debugger.", "inputSchema": object_schema({"mode": {"type": "string", "enum": ["run", "halt", "init"], "default": "run"}})},
    {"name": "debug_start_session", "description": "Start a typed debug session for a validated ELF artifact.", "inputSchema": object_schema({"image_path": NONEMPTY_STRING, "artifact_id": NONEMPTY_STRING, "mode": {"type": "string", "enum": ["attach", "reset_halt", "load"], "default": "attach"}, "timeout_s": TIMEOUT}, one_of=[{"required": ["image_path"]}, {"required": ["artifact_id"]}])},
    {"name": "debug_stop_session", "description": "Stop the active typed debug session.", "inputSchema": object_schema({"timeout_s": TIMEOUT})},
    {"name": "debug_get_session_status", "description": "Return active debug-session status.", "inputSchema": EMPTY_OBJECT_SCHEMA},
    {"name": "debug_set_breakpoint", "description": "Set a typed breakpoint by symbol/function name or file and line.", "inputSchema": object_schema({"location": BREAKPOINT_LOCATION}, required=["location"])},
    {"name": "debug_list_breakpoints", "description": "List breakpoints in the active debug session.", "inputSchema": EMPTY_OBJECT_SCHEMA},
    {"name": "debug_clear_breakpoints", "description": "Clear all breakpoints from the active debug session.", "inputSchema": EMPTY_OBJECT_SCHEMA},
    {"name": "debug_continue", "description": "Continue target execution until stop or timeout.", "inputSchema": object_schema({"timeout_s": TIMEOUT})},
    {"name": "debug_halt", "description": "Halt the target in the active debug session.", "inputSchema": object_schema({"timeout_s": TIMEOUT})},
    {"name": "debug_get_stop_reason", "description": "Return the last structured stop reason.", "inputSchema": EMPTY_OBJECT_SCHEMA},
    {"name": "debug_symbol_info", "description": "Resolve an allowed debug symbol.", "inputSchema": object_schema({"symbol": NONEMPTY_STRING}, required=["symbol"])},
    {"name": "debug_dump_symbol_ihex", "description": "Read an allowed symbol from target memory and write Intel HEX.", "inputSchema": object_schema({"symbol": NONEMPTY_STRING, "output_path": NONEMPTY_STRING}, required=["symbol", "output_path"])},
    {"name": "get_last_report", "description": "Return the most recent structured Agentic HIL report.", "inputSchema": EMPTY_OBJECT_SCHEMA},
    {"name": "classify_last_error", "description": "Classify the most recent Agentic HIL/debugger failure.", "inputSchema": EMPTY_OBJECT_SCHEMA},
    {"name": "com_ports_list", "description": "List configured named COM ports and detected host serial ports.", "inputSchema": EMPTY_OBJECT_SCHEMA},
    {"name": "com_session_start", "description": "Open a configured COM port and start a background feedback session.", "inputSchema": object_schema({"port_id": NONEMPTY_STRING, "clear_buffer": {"type": "boolean", "default": True}}, required=["port_id"])},
    {"name": "com_session_stop", "description": "Stop a configured COM port session.", "inputSchema": object_schema({"port_id": NONEMPTY_STRING}, required=["port_id"])},
    {"name": "com_write", "description": "Write text or hex stimulus to an active COM port session.", "inputSchema": object_schema({"port_id": NONEMPTY_STRING, "text": {"type": "string"}, "hex": {"type": "string"}}, required=["port_id"], one_of=[{"required": ["text"]}, {"required": ["hex"]}])},
    {"name": "com_read", "description": "Read buffered feedback from an active COM port session.", "inputSchema": object_schema({"port_id": NONEMPTY_STRING, "max_bytes": {"type": "integer", "minimum": 1}, "wait_timeout_s": TIMEOUT}, required=["port_id"])},
    {"name": "can_buses_list", "description": "List configured named CAN buses and active session status.", "inputSchema": EMPTY_OBJECT_SCHEMA},
    {"name": "can_session_start", "description": "Open a configured CAN bus session.", "inputSchema": object_schema({"bus_id": NONEMPTY_STRING, "clear_rx_queue": {"type": "boolean", "default": True}}, required=["bus_id"])},
    {"name": "can_session_stop", "description": "Stop a configured CAN bus session.", "inputSchema": object_schema({"bus_id": NONEMPTY_STRING}, required=["bus_id"])},
    {"name": "can_send", "description": "Send one classic CAN frame on an active configured CAN bus session.", "inputSchema": object_schema({"bus_id": NONEMPTY_STRING, "frame_id": {"oneOf": [{"type": "integer", "minimum": 0}, {"type": "string", "pattern": r"^(?:0[xX][0-9A-Fa-f]+|[0-9]+)$"}]}, "extended": {"type": "boolean", "default": False}, "rtr": {"type": "boolean", "default": False}, "data_hex": {"type": "string", "default": ""}}, required=["bus_id", "frame_id"])},
    {"name": "can_read", "description": "Read CAN frames from an active configured CAN bus session.", "inputSchema": object_schema({"bus_id": NONEMPTY_STRING, "max_frames": {"type": "integer", "minimum": 1}, "wait_timeout_s": TIMEOUT}, required=["bus_id"])},
    {"name": "adapters_list", "description": "List configured test adapters and session status.", "inputSchema": EMPTY_OBJECT_SCHEMA},
    {"name": "adapter_session_start", "description": "Start a configured test-adapter bridge session.", "inputSchema": object_schema({"adapter_id": NONEMPTY_STRING}, required=["adapter_id"])},
    {"name": "adapter_session_stop", "description": "Stop a configured test-adapter session.", "inputSchema": object_schema({"adapter_id": NONEMPTY_STRING}, required=["adapter_id"])},
    {"name": "adapter_set_value", "description": "Set an allowed test-adapter channel value.", "inputSchema": object_schema({"adapter_id": NONEMPTY_STRING, "channel": NONEMPTY_STRING, "value": {"type": "number"}, "unit": {"type": "string"}}, required=["adapter_id", "channel", "value"])},
    {"name": "adapter_inject_fault", "description": "Inject an allowed test-adapter fault.", "inputSchema": object_schema({"adapter_id": NONEMPTY_STRING, "fault": NONEMPTY_STRING, "channel": NONEMPTY_STRING}, required=["adapter_id", "fault"])},
    {"name": "adapter_clear_fault", "description": "Clear one or all injected test-adapter faults.", "inputSchema": object_schema({"adapter_id": NONEMPTY_STRING, "fault": NONEMPTY_STRING, "channel": NONEMPTY_STRING}, required=["adapter_id"])},
    {"name": "adapter_measure", "description": "Measure an allowed test-adapter channel.", "inputSchema": object_schema({"adapter_id": NONEMPTY_STRING, "channel": NONEMPTY_STRING}, required=["adapter_id", "channel"])},
]

MCP_TOOL_NAMES = [str(tool["name"]) for tool in MCP_TOOLS]
TOOL_SCHEMAS = {str(tool["name"]): tool["inputSchema"] for tool in MCP_TOOLS}
TOOL_VALIDATORS = {name: Draft202012Validator(schema) for name, schema in TOOL_SCHEMAS.items()}


def validate_tool_arguments(name: str, arguments: JsonObject) -> JsonObject | None:
    validator = TOOL_VALIDATORS.get(name)
    if validator is None:
        return None
    nonfinite_field = find_nonfinite(arguments)
    if nonfinite_field is not None:
        return invalid_argument(name, nonfinite_field, "finite", "Tool arguments must contain only finite numbers.")
    errors = sorted(validator.iter_errors(arguments), key=lambda item: (list(item.absolute_path), str(item.validator)))
    if not errors:
        return None
    error = errors[0]
    parts = [str(part) for part in error.absolute_path]
    if error.validator == "required":
        missing = next((field for field in error.validator_value if field not in error.instance), None)
        if missing is not None:
            parts.append(str(missing))
    elif error.validator == "additionalProperties":
        match = re.search(r"'([^']+)' was unexpected", error.message)
        if match:
            parts.append(match.group(1))
    result = invalid_argument(name, format_field_path(parts), str(error.validator), "Tool arguments failed schema validation.")
    if error.validator == "enum":
        result["allowed_values"] = error.validator_value
    return result


def invalid_argument(tool: str, field: str, validator: str, summary: str) -> JsonObject:
    return {"ok": False, "tool": tool, "error_type": "invalid_argument", "field": field, "validator": validator, "summary": summary}


def find_nonfinite(value: Any, parts: list[str] | None = None) -> str | None:
    current = parts or []
    if isinstance(value, float) and not math.isfinite(value):
        return format_field_path(current)
    if isinstance(value, dict):
        for key, child in value.items():
            found = find_nonfinite(child, [*current, str(key)])
            if found is not None:
                return found
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found = find_nonfinite(child, [*current, str(index)])
            if found is not None:
                return found
    return None
