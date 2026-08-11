from __future__ import annotations

import math
import re
from typing import Any

from jsonschema import Draft202012Validator

from agentic_hil.config import format_field_path
from agentic_hil.knowledge import remediation_fields
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
    {"name": "reset_target", "description": "Reset the configured target through the configured debugger. Use this instead of st-util, openocd, or a raw gdb reset.", "inputSchema": object_schema({"mode": {"type": "string", "enum": ["run", "halt", "init"], "default": "run", "description": "How the target is left afterwards. 'run' resets and lets the target execute. 'halt' leaves the core stopped. 'init' additionally runs the target's reset-init event script — clock tree, wait states, watchdog — and is OpenOCD-only: the stlink and pyocd backends refuse it with not_supported rather than halting and calling that init."}})},
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
    {"name": "can_buses_list", "description": "List configured named CAN buses and active session status, with listen_only and the evidence that would back it on each adapter (listen_only_enforcement). Use this instead of ip link or candump on a SocketCAN interface.", "inputSchema": EMPTY_OBJECT_SCHEMA},
    {"name": "can_session_start", "description": "Open a configured CAN bus session. A bus configured listen_only refuses rather than opening when the adapter cannot be held to it.", "inputSchema": object_schema({"bus_id": NONEMPTY_STRING, "clear_rx_queue": {"type": "boolean", "default": True}}, required=["bus_id"])},
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
    # Two optional arguments, and their types are the contract. There is no
    # `confirm_safe_state` boolean here and there will not be: that flag attests
    # that a physical board is still and holds the expected firmware, and a flag
    # the caller sets for itself is not a confirmation of anything. A schema test
    # pins its absence, as another pins the absent version argument on the
    # upgrade tool. `operator_statement` is the opposite shape and that is why it
    # is allowed to exist: it cannot be satisfied by a value the caller invents,
    # because what it has to contain is what a person said. A boolean can be
    # guessed into existence; a sentence about the state of a bench has to come
    # from somebody. The tool refuses an empty one for the same reason.
    #
    # `accept_config_change` is a boolean and belongs here all the same, because
    # it attests nothing about a board. It says the difference between two
    # configuration digests, both printed in the refusal that asks for it, has
    # been looked at. The refusal named this override from the day it existed
    # while the schema refused the argument it named, so the one way forward the
    # tool handed an agent dead-ended on the tool itself.
    {
        "name": "hardware_recover",
        "description": (
            "Clear this bench's quarantine. Reasons that name a call which never reached the hardware — a lease "
            "release that could not persist its own record, an owner process that died before its call touched a "
            "board — clear with no arguments. Every other reason needs operator_statement: ask the operator in chat "
            "what state the bench is in, then pass back what they answered, in their words. It is written verbatim "
            "to the recovery ledger as their statement, relayed by you. Never write one you were not given — a "
            "ledger line that reflects no actual operator utterance is a false record with you recorded as the "
            "actor; when you have nobody to ask, relay the `agentic-hil recover --confirm-safe-state "
            "--quarantine-id <id>` line the refusal hands you and let them run it themselves. A refusal with "
            "error_type config_changed means the authoritative configuration was edited after the incident was "
            "recorded: show the operator the two digests on the result, and once they confirm the delta is "
            "understood, call again with accept_config_change: true (the operator's own line takes "
            "--accept-config-change instead). Needs permissions.allow_recover. Safe to call when nothing is "
            "quarantined; it answers was_quarantined: false. Use this instead of deleting the server's state "
            "files, which is never the fix."
        ),
        "inputSchema": object_schema(
            {
                "operator_statement": NONEMPTY_STRING,
                "accept_config_change": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "Accept that the authoritative configuration changed after the incident was recorded. Only "
                        "after the operator has reviewed the delta between the two digests the config_changed "
                        "refusal reports; it is written to the recovery ledger as config_change_accepted beside "
                        "both of them."
                    ),
                },
            }
        ),
    },
    # No parameters, and that is the contract. The configuration is generated for
    # the workspace this server is bound to, out of what is attached to this
    # machine: a workspace_root argument would let a caller provision a project
    # this server was never pointed at, and a content argument would let it
    # decide its own permissions.
    {"name": "project_config_create", "description": "Generate this workspace's Agentic HIL configuration from attached hardware when it has none yet. Takes no arguments. Every permission in the generated file is true — flashing, reset, COM and CAN writes, and all three permissions.allow_config_* grants — except allow_raw_debugger_commands and allow_mass_erase, which are false: either one being true refuses flash_firmware on that probe, and neither is a tool you have here. The bench is workable from that file without anybody editing YAML, flashing included. Report what it granted and ask the operator which of it this bench should not have; do not ask for those two to be turned on, because that is the one change that stops flashing. Regenerating an existing configuration needs allow_config_write and carries over the permissions of the configuration this server loaded at startup, for the entries that are still in it; an entry the regeneration discovers for the first time arrives at those same defaults, and a workspace whose configuration is gone gets a file at those defaults. Because this server does not reload, a narrowing you made with project_config_set in this session is not in what it loaded and a regeneration now puts that permission back — regenerating is the operator's call, not a way to narrow or to re-open. Use this instead of writing a configuration by hand when a tool reports config_file_not_found.", "inputSchema": EMPTY_OBJECT_SCHEMA},
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
            "settings) and allow_config_permissions_write for every permission key — each permissions: block, and the "
            "two grants that sit directly on a section, artifacts.allow_upload and debug.allow_all_symbols. Values are scalars checked "
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
    # No arguments, and that is the contract twice over. There is nothing to
    # select — this server is bound to one configuration and re-reads that one —
    # and there is no flag, because a flag is where "also take the permissions"
    # would eventually be asked for, and the answer to that is that no such flag
    # exists on this surface at all.
    {
        "name": "project_config_reload_description",
        "description": (
            "Re-read this bench's device description from the authoritative configuration without restarting the "
            "server: target, debuggers, com_ports and can_buses, minus every permissions: block. Call this when a "
            "board was plugged in and written into the file after this server started, instead of asking the operator "
            "for a restart. Permissions are not re-read at all, in either direction — a device this server has never "
            "seen arrives with no grant, so it can be probed and read and cannot be flashed, reset, mass-erased or "
            "written to until an operator restarts the server. Everything outside those four sections (version, "
            "workspace_root, state_root, debug, artifacts, validation, recovery, reports, logs, and the project "
            "permissions) still needs a restart, and the result names them. Refused while a run, a session or an "
            "unresolved incident holds this bench, and when the file is missing, unreadable or does not load."
        ),
        "inputSchema": EMPTY_OBJECT_SCHEMA,
    },
    # No arguments, and here the empty schema is the security property rather
    # than a convenience. There is deliberately no way to name a
    # version: this tool lifts the installation to the newest release and can do
    # nothing else. A `version` argument would put the whole permission model
    # behind it — an agent that can install 0.7.x installs a release that reads
    # `permissions:` under weaker rules, and every narrowing an operator made is
    # undone by a downgrade rather than by a permission change. Downgrades and
    # exact versions stay at the shell, with the operator, and
    # `additionalProperties: false` is what refuses one that arrives anyway.
    {
        "name": "server_upgrade",
        "description": (
            "Replace this Agentic HIL installation with the newest release, gated by permissions.allow_upgrade. Takes "
            "no arguments: it can only lift to the latest release, never to a version you name, so it cannot be used "
            "to install a build that reads this bench's permissions differently. It replaces the package on disk and "
            "does not change the code this server is running — a successful call answers upgraded_on_disk with "
            "previous_version, version, running_version and restart_required: true, and the operator restarting the "
            "MCP server is what loads it; the agentic-hil command line reads the new code straight away. Refused "
            "while a run or a session holds this bench, and refused on Windows, where the files of a running process "
            "are locked and only `agentic-hil upgrade` at a shell can replace them. Use this instead of running uv, "
            "pipx or pip yourself."
        ),
        "inputSchema": EMPTY_OBJECT_SCHEMA,
    },
]

# ---------------------------------------------------------------------------
# Tool annotations.
#
# `ToolAnnotations` as defined in the MCP schema for revision 2025-06-18 — the
# version this server advertises in `initialize` — and unchanged word for word
# in the current 2026-07-28 revision: `title`, `readOnlyHint`,
# `destructiveHint`, `idempotentHint`, `openWorldHint`.
#
# The title goes in `annotations.title` and not in the tool's own top-level
# `title`, deliberately. Both exist and the top-level one wins where a host
# supports it, but it only arrives with 2025-06-18 while `annotations.title`
# has been understood since 2025-03-26 — and this server still negotiates down
# to 2024-11-05. One string, in the field the widest set of revisions reads.
#
# Without them a host has the tool's name and nothing else, and it judges by
# appearance: `project_config_reload_description` sits beside
# `project_config_set` and `project_config_create`, which do write, so a host
# blocked the one call that re-reads the file and the operator reconnected twice
# instead. The description says the reload writes nothing — in
# prose, in the middle of a paragraph, not in the field a machine reads.
#
# Three rules govern what may be written here, and two definitions decide the
# hard cases.
#
# **They are claims about behaviour, so they have to be true.** Each entry
# below follows what the implementation demonstrably does, not what the name
# suggests; where the answer is arguable the argument is in the comment above
# the entry. The by-name test in tests/test_tool_annotations.py is the gate.
#
# **Silence is a claim, because two of the defaults are not `false`.** Per the
# schema, `destructiveHint` defaults to **true** and `openWorldHint` defaults to
# **true**; only `readOnlyHint` and `idempotentHint` default to false. So a tool
# that does not say `destructiveHint: false` has said it may destroy, and every
# tool has to state `openWorldHint: false` outright. Conversely
# `destructiveHint` and `idempotentHint` are, in the schema's own words,
# "meaningful only when `readOnlyHint == false`", so a read-only tool declares
# neither — a `destructiveHint` beside `readOnlyHint: true` is a contradiction,
# not a belt-and-braces.
#
# **They are hints, and this server enforces nothing by them.** The schema says
# so ("all properties in ToolAnnotations are hints ... clients should never make
# tool use decisions based on ToolAnnotations received from untrusted servers"),
# and nothing in this package reads this table to decide anything. What decides
# is the authoritative configuration's permissions and the machine-wide device
# locks, exactly as before.
#
# What `readOnlyHint` is measured against here: the bench — the target and its
# probe, the configured COM ports and CAN buses, the workspace and the
# authoritative configuration. It is *not* measured against this server's own
# record that a call happened. Every hardware tool writes a report, most take a
# device lease, and several append to the audit ledger; if that counted as
# modifying the environment then no tool here could ever be read-only and the
# distinction the host needs would not exist. The carve-out is for the trail
# only, and it is the sole carve-out.
#
# `openWorldHint` is `false` on every tool that acts on the bench, which is all
# of them but one. A bench is a closed, named set: the target and the probe, the
# COM ports and CAN buses the authoritative configuration declares, the artifact
# store under the workspace, and that configuration itself. The four tools that
# read what is physically attached rather than what is configured —
# `debugger_probes_list`, `com_ports_list`, `project_config_adopt_hardware` and
# `project_config_create` — are bounded by one machine's hardware, which is
# still a closed domain and not the open world the schema contrasts a web search
# against.
#
# `server_upgrade` is the exception and the first honest `openWorldHint: true`
# here. It hands the installation to `uv`, `pipx` or `pip`, which
# resolve against a package index over the network: what arrives is whatever the
# newest release is at that moment, from an entity outside this machine and
# outside this configuration. That is the open world exactly as the schema means
# it, and a `false` there would be the one place this table told a host the
# opposite of what the code does.
TOOL_ANNOTATIONS: dict[str, JsonObject] = {
    # Runs `<backend executable> --version` and reads the configuration. No
    # probe is opened and no board is contacted.
    "debugger_info": {"title": "Debugger backend availability", "readOnlyHint": True, "openWorldHint": False},
    # Enumerates USB probes and stops there: `pyocd json --probes --no-config`
    # and `STM32_Programmer_CLI -l st-link-only`, both of which the backends
    # document as never connecting to a target (pyocd.py, stlink.py). OpenOCD
    # cannot enumerate at all and refuses. Nothing on the board is touched.
    "debugger_probes_list": {"title": "List connected debug probes", "readOnlyHint": True, "openWorldHint": False},
    # The arguable one, and it is deliberately *not* read-only.
    #
    # It reads, and it connects without resetting — OpenOCD `init; targets`,
    # pyOCD `status`, ST-Link `-HOTPLUG` — which is exactly why recovery is
    # allowed to use it as a predicate (docs/security-design.md). But connecting
    # is not nothing, and this repository has already said so twice, in the
    # quarantine inventory (knowledge.py) and in
    # docs/security-design.md: "being read-only is not being passive: an SWD
    # attach halts the core". Bringing a running core under debug control is a
    # change to the target's execution state, and the failure path treats it as
    # one — a probe that names no abort point leaves the physical state unknown
    # and is settled by a verified reset-into-halt, never by re-reading, which
    # is not what one does about a call that changed nothing. The repository's
    # own classification agrees: `probe_target` is in `debugger_effect_tools()`.
    #
    # So: it acts on the board, reversibly (destructiveHint false — nothing is
    # erased and a reset restores execution) and repeatably (idempotentHint true
    # — it connects, reads, disconnects, and leaves the target where the first
    # call left it).
    "probe_target": {"title": "Probe the target", "readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    # Content-addressed: the artifact id is `sha256(bytes) + suffix` and the
    # stored path is that id, so the only file a second upload of the same bytes
    # can overwrite is the byte-identical one already there, and a symlink
    # destination is refused outright. Additive, and idempotent by construction.
    "artifact_upload": {"title": "Upload a firmware artifact", "readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    # The irreversible one. Erases and programs the target's flash: what was on
    # the device before the call cannot be recovered from anything this server
    # holds. Not idempotent — a second flash of the same image erases and writes
    # the same sectors again, and with `reset_after_flash` resets the board
    # again; the end state matches but the call is not free.
    "flash_firmware": {"title": "Flash firmware", "readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": False},
    # Resets the running target. Nothing stored is lost, so this is not
    # destructive; it is also not idempotent, because a second reset throws away
    # whatever the firmware did since the first one.
    "reset_target": {"title": "Reset the target", "readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False},
    # Destructive because of one mode: `mode: "load"` programs the ELF into
    # flash, which is why the gate demands `allow_flash` for it (tools.py) — the
    # same irreversible write as flash_firmware, reached through a different
    # tool. `reset_halt` additionally resets. Only `attach` is passive, and a
    # hint cannot be conditional on an argument, so the tool is annotated for
    # what it may do.
    "debug_start_session": {"title": "Start a debug session", "readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": False},
    # Containment: tears the session down and releases what it held. Calling it
    # with no session open answers `ok` with `active: false` rather than
    # failing, so repeating it is genuinely free.
    "debug_stop_session": {"title": "Stop the debug session", "readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    "debug_get_session_status": {"title": "Debug session status", "readOnlyHint": True, "openWorldHint": False},
    # Sets a breakpoint in the live session. Not idempotent: GDB numbers
    # breakpoints, so setting the same location twice yields two of them.
    "debug_set_breakpoint": {"title": "Set a breakpoint", "readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False},
    "debug_list_breakpoints": {"title": "List breakpoints", "readOnlyHint": True, "openWorldHint": False},
    # Reconciles against GDB's own list and deletes only what GDB reports, so a
    # second call finds nothing left and clears nothing; the backend states
    # outright that clearing breakpoints does not change target execution state.
    "debug_clear_breakpoints": {"title": "Clear all breakpoints", "readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    # Resumes the target, which then runs. Not idempotent — a second continue
    # runs further from wherever the first one stopped.
    "debug_continue": {"title": "Continue the target", "readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False},
    # Interrupts the target and waits for the stop to be confirmed. The stop is
    # an effect, so not read-only; nothing is lost by it, so not destructive.
    "debug_halt": {"title": "Halt the target", "readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False},
    "debug_get_stop_reason": {"title": "Last stop reason", "readOnlyHint": True, "openWorldHint": False},
    # Two GDB expression evaluations — `(unsigned long)&symbol` and
    # `sizeof(symbol)` — both answered out of the ELF's debug information. No
    # target memory is read and nothing is written, so this is a read even
    # though it needs a live session to ask through.
    "debug_symbol_info": {"title": "Resolve a debug symbol", "readOnlyHint": True, "openWorldHint": False},
    # Reads target memory, which changes nothing, and then writes the Intel HEX
    # file at `output_path` — an atomic replace of whatever was at that path
    # inside the workspace. That replacement is the destructive part, and it is
    # in the workspace rather than on the board. Not idempotent: the bytes come
    # from live memory, so a second call writes a different snapshot.
    "debug_dump_symbol_ihex": {"title": "Dump a symbol as Intel HEX", "readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": False},
    "get_last_report": {"title": "Last structured report", "readOnlyHint": True, "openWorldHint": False},
    "classify_last_error": {"title": "Classify the last failure", "readOnlyHint": True, "openWorldHint": False},
    # `serial.tools.list_ports.comports()` plus the configuration and this
    # server's own session state. No port is opened.
    "com_ports_list": {"title": "List COM ports", "readOnlyHint": True, "openWorldHint": False},
    # Destructive on two counts, both of them in the code. The open sets the
    # modem lines, and `assert_dtr`/`assert_rts` default to true — on a board
    # that wires DTR to reset, opening the port to listen restarts the target,
    # which comports.py says in as many words. And `clear_buffer` defaults to
    # true, so a call against an already-open session purges the driver's input
    # buffer and the session's own: received bytes that no read returned are
    # gone. That purge is also why it is not idempotent.
    "com_session_start": {"title": "Open a COM session", "readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": False},
    # Containment. Closing with no session open answers `ok` with
    # `was_active: false`.
    "com_session_stop": {"title": "Close a COM session", "readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    # The bytes themselves are additive — a stream is appended to — but what the
    # target does with them is the firmware's business and not knowable from
    # here, and stimulus cannot be taken back off the wire. This is the tool
    # `allow_write` exists to gate, and the hint should not read safer than the
    # permission does.
    "com_write": {"title": "Write to a COM port", "readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": False},
    # Read-only, with one thing worth stating plainly: it *consumes*. The bytes
    # it returns are deleted from the session buffer, so two calls do not return
    # the same data. What it does not do is touch the port, the target, or the
    # configuration — it never reaches the serial handle at all, only the buffer
    # a background reader fills. `readOnlyHint` asks about the environment, and
    # the environment is unchanged.
    "com_read": {"title": "Read from a COM port", "readOnlyHint": True, "openWorldHint": False},
    # The configuration plus each adapter's own `status()`. No adapter is opened
    # and no frame is exchanged.
    "can_buses_list": {"title": "List CAN buses", "readOnlyHint": True, "openWorldHint": False},
    # Opening a CAN channel puts a node on a shared bus: on PCAN the channel is
    # initialized and ACKs, and at a wrong bitrate emits error frames — can.py
    # says exactly that, and it is why the failure path quarantines. Nothing
    # this server does afterwards takes those ACKs back off the bus. The default
    # `clear_rx_queue` additionally drains queued frames on a repeat call, which
    # is the second reason it is not idempotent.
    "can_session_start": {"title": "Open a CAN session", "readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": False},
    # Containment. Stopping with no session open answers `ok` with
    # `was_active: false`.
    "can_session_stop": {"title": "Close a CAN session", "readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    # Same reasoning as com_write: one frame on a shared bus, gated by a
    # permission, and not recallable.
    "can_send": {"title": "Send a CAN frame", "readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": False},
    # `recv()` transmits nothing — can.py relies on exactly that to prove a
    # failed read sent no frame — so the bus is untouched. Like com_read it
    # consumes: the frames it returns are off the queue.
    "can_read": {"title": "Read CAN frames", "readOnlyHint": True, "openWorldHint": False},
    # Takes the machine-wide locks for every declared device and holds them
    # until bench_run_stop. It destroys nothing. It is not idempotent either: a
    # second call while a run is open is refused with `run_already_active`
    # rather than being absorbed, so a host must not treat repeating it as free.
    "bench_run_start": {"title": "Declare a bench run", "readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False},
    # Releases the run's devices, and closing a run that is not open is how an
    # agent recovers from losing track of its own state — it answers `ok` with
    # `run_was_active: false` and writes nothing.
    "bench_run_stop": {"title": "End the bench run", "readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    # In-memory only; it does not even read the holder files.
    "bench_run_status": {"title": "Bench run status", "readOnlyHint": True, "openWorldHint": False},
    # Not read-only: it rewrites this project's lease records and appends to the
    # recovery ledger, and what that changes is which calls the bench will accept
    # next. Not destructive, and that is a claim about what it reaches rather
    # than a comfortable default — it never drives the target, never opens a
    # port or a bus, and only ever clears reasons that name no hardware contact;
    # the ones that might have left a board somewhere are exactly the ones it
    # refuses. Idempotent: a second call finds nothing quarantined and answers
    # `ok` with `was_quarantined: false`.
    "hardware_recover": {"title": "Clear a bench quarantine", "readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    # Rewrites the authoritative configuration with no backup on disk, and
    # carries over the permissions of the document *this server loaded at
    # startup* — so a narrowing made with project_config_set in this session is
    # put back wider, and nothing here can undo that. It also re-reads the board
    # on every call and a newly discovered entry arrives fully granted, so two
    # calls are equal only if the bench did not move between them, which is not
    # a property this tool can promise.
    "project_config_create": {"title": "Generate the project configuration", "readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": False},
    # Reads the file and the loaded configuration and classifies keys. The only
    # writes anywhere in configwrite.py are on the `set` path.
    "project_config_describe": {"title": "Describe the configuration", "readOnlyHint": True, "openWorldHint": False},
    # Replaces named values in the authoritative file; the previous value is
    # kept in memory for a rollback and nowhere else. Not idempotent, and for a
    # concrete reason rather than a cautious one: every accepted call stamps
    # provenance and increments `modification_count`, so setting a key to the
    # value it already holds still rewrites the file.
    "project_config_set": {"title": "Change configuration keys", "readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": False},
    # Not read-only: it reads the attached probe on every call, and with
    # `apply: true` it writes the configuration. Not destructive: it fills only
    # keys that are unset or still hold the skeleton's placeholder, and reports
    # a key somebody set rather than replacing it. Idempotent: after the first
    # apply every key matches, so the second call proposes nothing, never
    # reaches the write, and answers "Nothing was written."
    "project_config_adopt_hardware": {"title": "Adopt attached hardware", "readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    # The tool the annotations were written for, and the claim holds: it writes
    # nothing. No configuration write, no report, no audit record —
    # configreload.py contains no write call of any kind and the tool is not in
    # `audited_hardware_tools()`. It contacts no hardware. What it changes is
    # this process's own view: it re-reads the authoritative file and swaps in
    # the device description, leaving every permission byte-for-byte as parsed
    # at startup. A second call against the same file reports that nothing
    # moved.
    "project_config_reload_description": {"title": "Reload the bench description", "readOnlyHint": True, "openWorldHint": False},
    # Not read-only: it replaces the installed package on disk. Not destructive
    # either, and the distinction is the point — nothing is erased that this
    # server holds, the configuration and the bench are untouched, and the
    # previous release is a `uv tool install "agentic-hil==X.Y.Z"` away because
    # the index still has it. Idempotent: it can only lift to the newest release,
    # so a second call finds the installation already there and answers
    # `already_current` without replacing anything. `openWorldHint: true` because
    # the package comes off a network index — see the note above.
    "server_upgrade": {"title": "Upgrade this Agentic HIL installation", "readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
}

# Attached here rather than written into each literal above so that the
# reasoning can sit next to the decision instead of at the end of a long line.
# A name missing from the table ships without annotations rather than raising at
# import: an unannotated tool is a degraded tool, not a broken server, and the
# by-name test in tests/test_tool_annotations.py is what refuses to let one
# through.
for _tool in MCP_TOOLS:
    _tool_annotations = TOOL_ANNOTATIONS.get(str(_tool["name"]))
    if _tool_annotations is not None:
        _tool["annotations"] = _tool_annotations

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
    # Every schema refusal on the MCP surface is built here, so this is where the
    # catalogue's fix is attached — the same one `agentic-hil://reference/errors`
    # serves and the same one `ConfigError.to_dict` merges in, rather than a
    # second wording that would drift from it. The scope is the tool rather than
    # the field: a per-field entry would be a copy of the input schemas that
    # nothing keeps in step with them, and the bare entry says how to read
    # `field` and `validator`, which is what the caller is missing.
    return {"ok": False, "tool": tool, "error_type": "invalid_argument", "field": field, "validator": validator, "summary": summary, **remediation_fields("invalid_argument", tool)}


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
