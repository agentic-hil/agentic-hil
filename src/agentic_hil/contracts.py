from __future__ import annotations

import math
import re
from typing import Any

from jsonschema import Draft202012Validator

from agentic_hil.config import format_field_path
from agentic_hil.types import JsonObject

EMPTY_OBJECT_SCHEMA: JsonObject = {"type": "object", "properties": {}, "additionalProperties": False}
NONEMPTY_STRING: JsonObject = {"type": "string", "minLength": 1}
SYMBOL_NAME: JsonObject = {"type": "string", "pattern": r"^[A-Za-z_][A-Za-z0-9_]*(?:::[A-Za-z_][A-Za-z0-9_]*)*$"}
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


# One entry of a run's declaration. `id` names the *config entry*; the lock this
# resolves to is derived from the hardware behind it, so two entries describing
# one physical unit collapse onto one lock. The DUT is not a kind here: it is
# what the devices drive, not something that drives.
DEVICE_SELECTOR: JsonObject = {
    "type": "object",
    "additionalProperties": False,
    "required": ["kind"],
    "properties": {
        "kind": {"type": "string", "enum": ["debugger", "uart", "can"]},
        # Optional only for a debugger, and only when the project configures
        # exactly one: with several, naming none is how the wrong board is taken.
        "id": NONEMPTY_STRING,
    },
}

MCP_TOOLS: list[JsonObject] = [
    {"name": "debugger_info", "description": "Check whether the configured debugger backend is available. Use this instead of running openocd, pyocd, or st-info yourself.", "inputSchema": EMPTY_OBJECT_SCHEMA},
    {"name": "debugger_probes_list", "description": "List every connected probe ID visible to the configured debugger backend. Use this instead of st-info --probe, pyocd list, or JLinkExe.", "inputSchema": EMPTY_OBJECT_SCHEMA},
    {"name": "probe_target", "description": "Probe the configured embedded target through the configured debugger. Use this instead of openocd, pyocd, or gdb.", "inputSchema": EMPTY_OBJECT_SCHEMA},
    {"name": "artifact_upload", "description": "Upload a local or base64-encoded firmware artifact into the configured Agentic HIL artifact store.", "inputSchema": object_schema({"image_path": NONEMPTY_STRING, "filename": NONEMPTY_STRING, "data_base64": NONEMPTY_STRING}, one_of=[{"required": ["image_path"]}, {"required": ["filename", "data_base64"]}])},
    {"name": "flash_firmware", "description": "Flash a validated firmware artifact. Provide exactly one of image_path or artifact_id. Use this instead of openocd, st-flash, or pyocd flash.", "inputSchema": object_schema({"image_path": NONEMPTY_STRING, "artifact_id": NONEMPTY_STRING, "reset_after_flash": {"type": "boolean", "default": False}}, one_of=[{"required": ["image_path"]}, {"required": ["artifact_id"]}])},
    {"name": "reset_target", "description": "Reset the configured target through the configured debugger. Use this instead of st-util, openocd, or a raw gdb reset.", "inputSchema": object_schema({"mode": {"type": "string", "enum": ["run", "halt", "init"], "default": "run"}})},
    {"name": "debug_start_session", "description": "Start a typed debug session for a validated ELF artifact.", "inputSchema": object_schema({"image_path": NONEMPTY_STRING, "artifact_id": NONEMPTY_STRING, "mode": {"type": "string", "enum": ["attach", "reset_halt", "load"], "default": "attach"}, "timeout_s": TIMEOUT}, one_of=[{"required": ["image_path"]}, {"required": ["artifact_id"]}])},
    {"name": "debug_stop_session", "description": "Stop the active typed debug session.", "inputSchema": object_schema({"timeout_s": TIMEOUT})},
    {"name": "debug_get_session_status", "description": "Return active debug-session status.", "inputSchema": EMPTY_OBJECT_SCHEMA},
    {"name": "debug_set_breakpoint", "description": "Set a typed breakpoint by symbol/function name or file and line.", "inputSchema": object_schema({"location": BREAKPOINT_LOCATION}, required=["location"])},
    {"name": "debug_list_breakpoints", "description": "List breakpoints in the active debug session.", "inputSchema": EMPTY_OBJECT_SCHEMA},
    {"name": "debug_clear_breakpoints", "description": "Clear all breakpoints from the active debug session.", "inputSchema": EMPTY_OBJECT_SCHEMA},
    {"name": "debug_continue", "description": "Continue target execution until stop or timeout.", "inputSchema": object_schema({"timeout_s": TIMEOUT})},
    {"name": "debug_halt", "description": "Halt the target in the active debug session.", "inputSchema": object_schema({"timeout_s": TIMEOUT})},
    {"name": "debug_get_stop_reason", "description": "Return the last structured stop reason.", "inputSchema": EMPTY_OBJECT_SCHEMA},
    {"name": "debug_symbol_info", "description": "Resolve an allowed debug symbol.", "inputSchema": object_schema({"symbol": SYMBOL_NAME}, required=["symbol"])},
    {"name": "debug_dump_symbol_ihex", "description": "Read an allowed symbol from target memory and write Intel HEX.", "inputSchema": object_schema({"symbol": SYMBOL_NAME, "output_path": NONEMPTY_STRING}, required=["symbol", "output_path"])},
    {"name": "get_last_report", "description": "Return the most recent structured Agentic HIL report.", "inputSchema": EMPTY_OBJECT_SCHEMA},
    {"name": "classify_last_error", "description": "Classify the most recent Agentic HIL/debugger failure.", "inputSchema": EMPTY_OBJECT_SCHEMA},
    {"name": "com_ports_list", "description": "List configured named COM ports and detected host serial ports. Use this instead of screen, minicom, picocom, or opening /dev/tty* or COM* directly.", "inputSchema": EMPTY_OBJECT_SCHEMA},
    {"name": "com_session_start", "description": "Open a configured COM port and start a background feedback session.", "inputSchema": object_schema({"port_id": NONEMPTY_STRING, "clear_buffer": {"type": "boolean", "default": True}}, required=["port_id"])},
    {"name": "com_session_stop", "description": "Stop a configured COM port session.", "inputSchema": object_schema({"port_id": NONEMPTY_STRING}, required=["port_id"])},
    {"name": "com_write", "description": "Write text or hex stimulus to an active COM port session.", "inputSchema": object_schema({"port_id": NONEMPTY_STRING, "text": {"type": "string"}, "hex": {"type": "string"}}, required=["port_id"], one_of=[{"required": ["text"]}, {"required": ["hex"]}])},
    {"name": "com_read", "description": "Read buffered feedback from an active COM port session. Use this instead of screen, minicom, or picocom.", "inputSchema": object_schema({"port_id": NONEMPTY_STRING, "max_bytes": {"type": "integer", "minimum": 1}, "wait_timeout_s": TIMEOUT}, required=["port_id"])},
    {"name": "can_buses_list", "description": "List configured named CAN buses and active session status. Use this instead of ip link or candump on a SocketCAN interface.", "inputSchema": EMPTY_OBJECT_SCHEMA},
    {"name": "can_session_start", "description": "Open a configured CAN bus session.", "inputSchema": object_schema({"bus_id": NONEMPTY_STRING, "clear_rx_queue": {"type": "boolean", "default": True}}, required=["bus_id"])},
    {"name": "can_session_stop", "description": "Stop a configured CAN bus session.", "inputSchema": object_schema({"bus_id": NONEMPTY_STRING}, required=["bus_id"])},
    {"name": "can_send", "description": "Send one classic CAN frame on an active configured CAN bus session. Use this instead of cansend.", "inputSchema": object_schema({"bus_id": NONEMPTY_STRING, "frame_id": {"oneOf": [{"type": "integer", "minimum": 0}, {"type": "string", "pattern": r"^(?:0[xX][0-9A-Fa-f]+|[0-9]+)$"}]}, "extended": {"type": "boolean", "default": False}, "rtr": {"type": "boolean", "default": False}, "data_hex": {"type": "string", "default": ""}}, required=["bus_id", "frame_id"])},
    {"name": "can_read", "description": "Read CAN frames from an active configured CAN bus session. Use this instead of candump.", "inputSchema": object_schema({"bus_id": NONEMPTY_STRING, "max_frames": {"type": "integer", "minimum": 1}, "wait_timeout_s": TIMEOUT}, required=["bus_id"])},
    {
        "name": "bench_run_start",
        "description": (
            "Declare a multi-step run and lock every device it names for the whole run, not for one call. "
            "Call this before a sequence like flash, reset, read: without it each call takes and releases its own "
            "device, and between two calls the board is free for anything else on this machine. Declared devices "
            "are held until bench_run_stop, the only devices this run may touch, and a device already held fails "
            "the call immediately naming its holder."
        ),
        "inputSchema": object_schema(
            {
                "devices": {"type": "array", "minItems": 1, "items": DEVICE_SELECTOR},
                "label": NONEMPTY_STRING,
                "wait_s": {"type": "number", "minimum": 0, "maximum": 900},
            },
            required=["devices"],
        ),
    },
    {
        "name": "bench_run_stop",
        "description": "End the declared run and release every device it held. Always call this when the run is finished; it is safe to call when no run is open.",
        "inputSchema": EMPTY_OBJECT_SCHEMA,
    },
    {
        "name": "bench_run_status",
        "description": "Report whether a run is open on this server, which devices it declared, and since when. Read this if you are not sure whether you still hold the bench.",
        "inputSchema": EMPTY_OBJECT_SCHEMA,
    },
    # No parameters, and that is the contract. The configuration is generated for
    # the workspace this server is bound to, out of what is attached to this
    # machine: a workspace_root argument would let a caller provision a project
    # this server was never pointed at, and a content argument would let it
    # decide its own permissions.
    {"name": "project_config_create", "description": "Generate this workspace's Agentic HIL configuration from attached hardware when it has none yet. Takes no arguments. Every permission in the generated file is true, including allow_raw_debugger_commands, allow_mass_erase and all three permissions.allow_config_* grants, so the bench is workable from it without anybody editing YAML — report what it granted and ask the operator which of it this bench should not have. Regenerating an existing configuration needs allow_config_write and carries the permissions already on disk over. Use this instead of writing a configuration by hand when a tool reports config_file_not_found.", "inputSchema": EMPTY_OBJECT_SCHEMA},
    # Reading is free, so this takes no arguments either: it answers for the one
    # configuration this server is bound to, in the state it is in.
    {
        "name": "project_config_describe",
        "description": (
            "Report which keys of this project's configuration you may change right now, which you may not, and which "
            "permission would open a locked one — for this configuration in this state, not in general. Also carries "
            "each key's current value and the value shape the shipped schema declares for it. Read this before "
            "project_config_set instead of guessing a key and being refused."
        ),
        "inputSchema": EMPTY_OBJECT_SCHEMA,
    },
    # Field-wise and scalar-valued, and both halves of that are the contract.
    # There is no argument that takes a document, and `value` admits no object
    # and no array — so no subtree, and therefore no permissions: block hidden
    # inside one, can arrive as a value. The agent names keys from a closed set;
    # it does not author this file.
    {
        "name": "project_config_set",
        "description": (
            "Change named keys of this project's configuration, field-wise. Two separate permissions gate it: "
            "allow_config_description_write for what the bench is (target, probe id, port device and baudrate, CAN bus "
            "settings) and allow_config_permissions_write for the permissions: blocks. Values are scalars checked "
            "against the shipped schema; the changed file is validated before it replaces the working one, and a write "
            "is refused while a run holds hardware. Use this instead of editing the configuration file yourself."
        ),
        "inputSchema": object_schema(
            {
                "changes": {
                    "type": "array",
                    "minItems": 1,
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["key", "value"],
                        "properties": {
                            "key": {"type": "string", "minLength": 1, "description": "Dotted configuration key, e.g. debuggers.dut.probe_id. project_config_describe lists the ones this caller may set."},
                            "value": {"type": ["string", "number", "integer", "boolean", "null"], "description": "A single scalar. Objects and arrays are refused: a whole subtree is content the agent authored, which is what this tool exists not to accept."},
                        },
                    },
                }
            },
            required=["changes"],
        ),
    },
    # Arguments select, never supply. Which of several attached probes, which
    # configured entry receives it — and nothing that could put a value of the
    # caller's choosing into the file. The values come from what is attached to
    # this machine, exactly as project_config_create's do.
    {
        "name": "project_config_adopt_hardware",
        "description": (
            "Read the attached probe and carry its identity into this project's configuration: probe id, the backend's "
            "executable, the detected controller, and the probe's own COM device. Use this when a configuration was "
            "written before the board was plugged in and holds placeholders, instead of printing values for a person to "
            "retype. Returns the plan by default and writes only with apply:true, through project_config_set and its "
            "permissions. A key that already holds a value nobody generated is reported, never overwritten."
        ),
        "inputSchema": object_schema(
            {
                "apply": {"type": "boolean", "default": False, "description": "Write the plan. Without it the call reads hardware and the configuration and changes nothing."},
                "probe_id": {**NONEMPTY_STRING, "description": "Which attached probe this is about. Only needed when more than one is attached; it selects among them and never adds one."},
                "debugger_id": {**NONEMPTY_STRING, "description": "Which configured debugger entry receives the values. Only needed when the configuration declares more than one."},
                "com_port_id": {**NONEMPTY_STRING, "description": "Which com_ports entry receives the discovered device. Created with every permission false if it does not exist."},
            }
        ),
    },
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
