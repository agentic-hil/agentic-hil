from __future__ import annotations

import math
import re
from typing import Any

from jsonschema import Draft202012Validator

from agentic_hil.config import format_field_path
from agentic_hil.knowledge import remediation_fields
from agentic_hil.readuntil import CAN_ID_MAX, UNTIL_MAX_CHARACTERS, UNTIL_MAX_ENTRIES
from agentic_hil.types import JsonObject

EMPTY_OBJECT_SCHEMA: JsonObject = {"type": "object", "properties": {}, "additionalProperties": False}
NONEMPTY_STRING: JsonObject = {"type": "string", "minLength": 1}
SYMBOL_NAME: JsonObject = {"type": "string", "pattern": r"^[A-Za-z_][A-Za-z0-9_]*(?:::[A-Za-z_][A-Za-z0-9_]*)*$"}
TIMEOUT: JsonObject = {"type": "number", "minimum": 0}
# What a read waits for: one text or arbitration id, or a short list of them.
# The limits are the ones `readuntil` checks, which a caller outside these
# schemas goes through as well, together with the check no schema can make:
# that each text can be encoded in the port's own encoding.
UNTIL_TEXT: JsonObject = {"type": "string", "minLength": 1, "maxLength": UNTIL_MAX_CHARACTERS}
UNTIL: JsonObject = {"oneOf": [UNTIL_TEXT, {"type": "array", "minItems": 1, "maxItems": UNTIL_MAX_ENTRIES, "items": UNTIL_TEXT}]}
UNTIL_CAN_ID: JsonObject = {"type": "integer", "minimum": 0, "maximum": CAN_ID_MAX}
UNTIL_ID: JsonObject = {"oneOf": [UNTIL_CAN_ID, {"type": "array", "minItems": 1, "maxItems": UNTIL_MAX_ENTRIES, "items": UNTIL_CAN_ID}]}
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
        "kind": {
            "type": "string",
            "enum": ["debugger", "uart", "can"],
            "description": "debugger (a `debuggers` entry), uart (`com_ports`) or can (`can_buses`). The board under test is not a kind.",
        },
        # Optional only for a debugger, and then it means the debugger this
        # server is bound to, or the only one configured: with several and no
        # binding, naming none is how the wrong board is taken, so it is refused.
        "id": {
            **NONEMPTY_STRING,
            "description": (
                "The config entry's name, else `unknown_device`. Required for uart and can; a debugger may leave it "
                "out, meaning the one this server is bound to or the only one configured, else `invalid_argument`."
            ),
        },
        "participant": {
            **NONEMPTY_STRING,
            "description": (
                "Only for can, else `invalid_argument`. Required on a bus with `shares` (`can_participant_required`); "
                "a name the bus lacks fails `can_participant_not_configured`."
            ),
        },
    },
}

MCP_TOOLS: list[JsonObject] = [
    {"name": "debugger_info", "description": "Check the debugger backend is installed: runs only --version, at most 10 s; contacts no probe or board. Replaces openocd. Returns backend, executable, version, config_status. Next: debugger_probes_list (probe ids), probe_target (board). Failures: debugger_not_found (missing), timeout; not_supported unless exactly one debugger is configured; permission_denied without allow_probe (config version 1).", "inputSchema": EMPTY_OBJECT_SCHEMA},
    {"name": "debugger_probes_list", "description": "List the probe ids of the in-circuit debuggers or programmers on this host; connects to no board. Replaces st-info. Returns probes, each with a probe_id to set as the debugger entry's probe_id. pyOCD and STM32CubeProgrammer ask their CLI; OpenOCD reads the USB serial inventory, complete false (adapters it cannot read there: not_supported). Failure: probe_discovery_failed.", "inputSchema": EMPTY_OBJECT_SCHEMA},
    {"name": "probe_target", "description": "Connect the in-circuit debugger or programmer to the board and confirm it answers (target_detected true), with no reset and no flash; reset_target resets. Replaces openocd. Refused with resource_busy while a debug session is open. Failures: adapter_not_found (no probe), target_not_detected (board silent), debugger_not_found (backend missing), timeout (one command over debugger.timeout_s).", "inputSchema": EMPTY_OBJECT_SCHEMA},
    {"name": "artifact_upload", "description": "Upload a local or base64-encoded firmware artifact into the configured Agentic HIL artifact store.", "inputSchema": object_schema({"image_path": NONEMPTY_STRING, "filename": NONEMPTY_STRING, "data_base64": NONEMPTY_STRING}, one_of=[{"required": ["image_path"]}, {"required": ["filename", "data_base64"]}])},
    {"name": "flash_firmware", "description": "Flash a validated firmware artifact. Provide exactly one of image_path or artifact_id. Use this instead of openocd, st-flash, or pyocd flash. With capture and reset_after_flash, one call flashes, resets and returns the UART boot output. It needs no bench_run_start.", "inputSchema": object_schema({"image_path": NONEMPTY_STRING, "artifact_id": NONEMPTY_STRING, "reset_after_flash": {"type": "boolean", "default": False}, "capture": {**object_schema({"port_id": {**NONEMPTY_STRING, "description": "Configured COM port to read."}, "until": {**UNTIL, "description": "Text to wait for, or up to 8 texts, as in com_read."}, "wait_timeout_s": {**TIMEOUT, "description": "Seconds to wait once the flash ends; 10 by default, 60 at most."}, "max_bytes": {"type": "integer", "minimum": 1, "description": "Most bytes to return."}}, required=["port_id"]), "description": "Read a COM port across the flash. The capture starts when the port is opened, before the flash, and ends at a match, max_bytes or the wait. Needs reset_after_flash."}}, one_of=[{"required": ["image_path"]}, {"required": ["artifact_id"]}])},
    {"name": "reset_target", "description": "Reset the board through the in-circuit debugger or programmer: the firmware restarts, flash is kept. Replaces st-util. Needs allow_reset (else permission_denied). Refused with resource_busy while a debug session is open: debug_stop_session first. To check the board without a reset use probe_target. Failures: reset_failed (reset not confirmed), timeout (one command over debugger.timeout_s).", "inputSchema": object_schema({"mode": {"type": "string", "enum": ["run", "halt", "init"], "default": "run", "description": "Default 'run' (core executes); 'halt' stops the core at reset; 'init' also runs the reset-init script (clocks, wait states, watchdog), OpenOCD-only, else not_supported."}})},
    {"name": "debug_start_session", "description": "Opens a GDB session on an ELF for debug_continue and debug_halt. OpenOCD backend only; others answer not_supported. Needs GDB (debug.gdb_executable or PATH), the debugger entry's scripts and allow_raw_debugger_commands off. Returns the core halted: session (status, stop_reason), log_path. A second start: session_already_active. debug_get_session_status inspects it; debug_stop_session ends it.", "inputSchema": object_schema({"image_path": {**NONEMPTY_STRING, "description": "Path to a .elf file, relative to the workspace or absolute, inside the workspace and (by default) artifacts.allowed_roots. Give this or artifact_id, not both."}, "artifact_id": {**NONEMPTY_STRING, "description": "Id of an ELF stored with artifact_upload. Give this or image_path, not both."}, "mode": {"type": "string", "enum": ["attach", "reset_halt", "load"], "default": "attach", "description": "attach (default): halts the core, no reset. reset_halt: resets, halts; needs allow_reset. load: resets, flashes the ELF, resets, halts; needs allow_reset and allow_flash, not allow_mass_erase."}, "timeout_s": {**TIMEOUT, "description": "Seconds. Default and ceiling: the debugger entry's timeout_s (60 unless set); 0.1 at least. Bounds the debug server's start and each GDB step (10 s at most). Expiry: timeout."}}, one_of=[{"required": ["image_path"]}, {"required": ["artifact_id"]}])},
    {"name": "debug_stop_session", "description": "Ends the debug_start_session session; debug_halt keeps it open. Confirms the core halted, disables resume on detach, ends GDB and the debug server, drops breakpoints, frees flash_firmware and reset_target. OpenOCD backend only; others answer not_supported. Success: safe_state_confirmed true. No session: ok, active false. Unproven: ok false, cleanup_required, hardware_state unknown, quarantined.", "inputSchema": object_schema({"timeout_s": {**TIMEOUT, "description": "Seconds per teardown step. Default and ceiling: 5, or the debugger entry's timeout_s if lower; 0.1 at least. A failed or expired step: halt_not_confirmed, detach_resume_not_confirmed, cleanup_failed."}})},
    {"name": "debug_get_session_status", "description": "Reports the debug_start_session session: active, status (halted, running, cleanup_required), session. Records a newly arrived stop; never halts or resumes the core. Abnormal stop: ok true, target_ok false, target_error_type. No session: ok, active false. The stop alone: debug_get_stop_reason. OpenOCD only, exactly one debugger configured; else not_supported.", "inputSchema": EMPTY_OBJECT_SCHEMA},
    {"name": "debug_set_breakpoint", "description": "Adds a breakpoint in the debug_start_session session without resuming the core: breakpoint (id, backend_id); a repeat adds another. Refused: permission_denied (grant), invalid_argument (location), debugger_error (GDB). Unconfirmed insert: cleanup_required, provisional_breakpoint. No session: session_not_active. OpenOCD only, exactly one debugger configured; else not_supported.", "inputSchema": object_schema({"location": {**BREAKPOINT_LOCATION, "description": "\"main\", {\"symbol\": \"main\"}, {\"function\": \"main\"}: C identifier, needs debug.allowed_symbols or debug.allow_all_symbols. {\"file\": \"a.c\", \"line\": 9}: line from 1, no '..', needs debug.allow_all_symbols."}}, required=["location"])},
    {"name": "debug_list_breakpoints", "description": "Lists the breakpoints debug_set_breakpoint added in the debug_start_session session, from the session's own record without asking GDB: breakpoints (id, backend_id, location). Never halts or resumes the core. No session: ok, active false, breakpoints empty. debug_clear_breakpoints removes them and checks GDB. OpenOCD only, exactly one debugger configured; else not_supported.", "inputSchema": EMPTY_OBJECT_SCHEMA},
    {"name": "debug_clear_breakpoints", "description": "Deletes every breakpoint GDB reports in the debug_start_session session and checks none is left: cleared (count), backend_reconciled true. Neither resumes nor halts the core; a repeat answers cleared 0. Unconfirmed: ok false, breakpoint_reconciliation_failed, cleanup_required. No session: session_not_active. OpenOCD only, exactly one debugger configured; else not_supported.", "inputSchema": EMPTY_OBJECT_SCHEMA},
    {"name": "debug_continue", "description": "Resumes the core in the debug_start_session session until it stops. Needs allow_debug_execution, else permission_denied. Returns stop_reason, stop (frame): breakpoint_hit is ok; unexpected_breakpoint and target_exception are not. An exception or debugger_error stop is not resumed. No session: session_not_active. OpenOCD only, exactly one debugger configured; else not_supported.", "inputSchema": object_schema({"timeout_s": {**TIMEOUT, "description": "Seconds to wait for a stop. Default and ceiling: the debugger entry's timeout_s (60 unless set); under 0.1 counts as 0.1 first. Expiry: interrupt, its stop, up to 5 s each; timeout, halt_confirmed."}})},
    {"name": "debug_halt", "description": "Halts the core in the debug_start_session session, which stays open; debug_stop_session ends it. A core already stopped gets no interrupt: its recorded stop is answered. Returns stop_reason (halted), stop. Unconfirmed: ok false, halt_confirmed false, target_state unknown, quarantined. No session: session_not_active. OpenOCD only, exactly one debugger configured; else not_supported.", "inputSchema": object_schema({"timeout_s": {**TIMEOUT, "description": "Seconds for the interrupt and for its stop, each. Default and ceiling: 10, or the debugger entry's timeout_s if lower; under 0.1 counts as 0.1 before the ceiling. Expiry: timeout."}})},
    {"name": "debug_get_stop_reason", "description": "Why the core last stopped: stop_reason (breakpoint_hit, halted, exception), stop (frame). Records a newly arrived stop; never halts or resumes the core. Abnormal stop: ok true, target_ok false, target_error_type. None yet: stop_reason_not_available. Without debug_start_session: session_not_active. Session: debug_get_session_status. OpenOCD only, exactly one debugger configured; else not_supported.", "inputSchema": EMPTY_OBJECT_SCHEMA},
    {"name": "debug_symbol_info", "description": "Find an allowed symbol's hex address, size_bytes and resolved_from in the ELF. Reads no target memory; for bytes use debug_symbol_value, for Intel HEX debug_dump_symbol_ihex. OpenOCD needs debug_start_session (else session_not_active). pyOCD and STM32CubeProgrammer use the ELF this server last flashed via flash_firmware (else symbol_source_not_available; symbol_source_changed if changed since).", "inputSchema": object_schema({"symbol": {**SYMBOL_NAME, "description": "C identifier, optionally ::-qualified, e.g. boot_counter. Allowed if in debug.allowed_symbols or debug.allow_all_symbols is true (default false), else permission_denied. Not in ELF: symbol_not_found."}}, required=["symbol"])},
    {"name": "debug_symbol_value", "description": "Read an allowed symbol from target memory, like gdb print: hex, plus value_unsigned and value_signed at 1, 2, 4 or 8 bytes. Address: debug_symbol_info; file: debug_dump_symbol_ihex. OpenOCD needs debug_start_session (else session_not_active); pyOCD and STM32CubeProgrammer, the ELF last flashed via flash_firmware (else symbol_source_not_available). Over debug.max_dump_size_bytes: permission_denied.", "inputSchema": object_schema({"symbol": {**SYMBOL_NAME, "description": "C identifier, optionally ::-qualified, e.g. boot_counter. Allowed if in debug.allowed_symbols or debug.allow_all_symbols is true (default false), else permission_denied. Not in ELF: symbol_not_found."}}, required=["symbol"])},
    {"name": "debug_dump_symbol_ihex", "description": "Read an allowed symbol from target memory and write it as Intel HEX to output_path; returns address, size_bytes and output. Value inline: debug_symbol_value. OpenOCD needs debug_start_session (else session_not_active); pyOCD and STM32CubeProgrammer, the ELF last flashed via flash_firmware (else symbol_source_not_available). Over debug.max_dump_size_bytes: permission_denied.", "inputSchema": object_schema({"symbol": {**SYMBOL_NAME, "description": "C identifier, optionally ::-qualified, e.g. boot_counter. Allowed if in debug.allowed_symbols or debug.allow_all_symbols is true (default false), else permission_denied. Not in ELF: symbol_not_found."}, "output_path": {**NONEMPTY_STRING, "description": "Workspace path ending .hex or .ihex, no '..' segment, under artifacts.allowed_roots if validation.require_allowed_root (default true), else output_validation_failed. Folders created, a file replaced."}}, required=["symbol", "output_path"])},
    {"name": "get_last_report", "description": "Return the most recent structured Agentic HIL report.", "inputSchema": EMPTY_OBJECT_SCHEMA},
    {"name": "classify_last_error", "description": "Classify the most recent Agentic HIL/debugger failure.", "inputSchema": EMPTY_OBJECT_SCHEMA},
    {"name": "com_ports_list", "description": "List `com_ports` entries in `ports` by port_id, which com_session_start takes (not a device path), with `session_active`. Opens no port. `available_com_ports` holds host serial ports, or `permission_denied` unless a configured port may be read, `serial_backend_not_available` without pyserial, `com_port_discovery_failed` on an OS error; the call stays ok. Use this instead of picocom or /dev/tty*.", "inputSchema": EMPTY_OBJECT_SCHEMA},
    {"name": "com_session_start", "description": "Open the serial port a com_ports entry names and hold it, with its machine-wide lock, until com_session_stop; a reader buffers what the board sends for com_read. DTR and RTS are driven as configured on open, which can reset a board. device_busy, resource_busy or com_port_busy: another server, process or program has the port. permission_denied: neither reading nor writing is allowed.", "inputSchema": object_schema({"port_id": {**NONEMPTY_STRING, "description": "Name of a com_ports entry (com_ports_list shows them), not a device path. Unknown names fail with com_port_not_configured; a device that will not open, with com_port_open_failed."}, "clear_buffer": {"type": "boolean", "default": True, "description": "Default true: discard bytes received so far, also on a repeat start for the same port_id, which keeps a live session (already_active) and replaces a failed one. False keeps them for com_read."}}, required=["port_id"])},
    {"name": "com_session_stop", "description": "Close the com_session_start session on port_id: stop its reader, close the port, release its lock. Unread bytes are dropped; the session log at log_path keeps them. Without a session it returns ok with was_active false. If the reader does not end within max(1 s, timeout_s + 0.5 s) of the close, it returns com_port_close_failed and keeps the session registered; call again to retry.", "inputSchema": object_schema({"port_id": {**NONEMPTY_STRING, "description": "Name of a com_ports entry in the project configuration (com_ports_list shows them), not a device path; an unknown name fails with com_port_not_configured."}}, required=["port_id"])},
    {"name": "com_write", "description": "Send bytes over the session com_session_start opened on port_id (else session_not_active); needs the port's allow_write (else permission_denied). At most max_write_bytes encoded bytes, 4096 by default. bytes_written counts the bytes sent; a short write returns serial_write_incomplete. Example: com_session_start port_id \"uart0\", com_write text \"hello\\r\\n\", com_read until \"\\n\", com_session_stop.", "inputSchema": object_schema({"port_id": {**NONEMPTY_STRING, "description": "Name of a com_ports entry in the project configuration (com_ports_list shows them), not a device path; an unknown name fails with com_port_not_configured."}, "text": {"type": "string", "description": "Characters to send, encoded with the port's configured encoding (utf-8 by default) and sent as is: no line ending is added, so include \\r\\n yourself. Give text or hex, not both."}, "hex": {"type": "string", "description": "Bytes as hexadecimal byte pairs, whitespace ignored, for example \"0d 0a\". Give hex or text, not both; hex suits binary payloads."}}, required=["port_id"], one_of=[{"required": ["text"]}, {"required": ["hex"]}])},
    {"name": "com_read", "description": "Read what the board sent on a COM port session from com_session_start (else session_not_active), instead of screen, minicom or picocom. With until, one call waits for a pattern instead of polling. Each byte is returned once, as text and hex. If until is not seen in time, the answer is still ok, with until_matched false and the bytes so far.", "inputSchema": object_schema({"port_id": {**NONEMPTY_STRING, "description": "The com_ports entry name (see com_ports_list), not a device path; an unknown name gives com_port_not_configured. Under config version 1, permission_denied unless allow_read is true."}, "max_bytes": {"type": "integer", "minimum": 1, "description": "Most bytes to return; default the port's max_buffer_bytes (65536 unless configured). The rest stays buffered, see buffer_remaining_bytes. With until, a wait also ends once max_bytes are buffered."}, "wait_timeout_s": {**TIMEOUT, "description": "Seconds to wait. Without until the default is 0 and a wait ends at the first bytes to arrive; with until the default is 10 s. Waits are capped at 60 s."}, "until": {**UNTIL, "description": "Text, or up to 8 texts, matched literally as bytes in the port's encoding, not as a regex. Returns the feedback through the first match; the rest stays buffered."}}, required=["port_id"])},
    {"name": "can_buses_list", "description": "List every `can_buses` entry in `buses`, keyed by bus_id, the name can_session_start, can_send and can_read take. Each shows `session_active`, `listen_only` with its `listen_only_enforcement`, `fd`, `max_frame_data_bytes` and `max_buffer_frames`; a bus with `shares` adds `active_participants`. Needs no session and opens no adapter. Use this instead of ip link or candump.", "inputSchema": EMPTY_OBJECT_SCHEMA},
    {"name": "can_session_start", "description": "Open a session on a configured CAN bus for can_send and can_read, until can_session_stop or server exit. Returns `session`; a repeat call answers `already_active: true`. Fails `can_bus_not_configured`, `device_busy` or `resource_busy` (bus held), `can_adapter_timeout` (bridge `timeout_s`, default 10 s), and on a `listen_only` bus `can_listen_only_unsupported` or `can_listen_only_unconfirmed`.", "inputSchema": object_schema({"bus_id": {**NONEMPTY_STRING, "description": "A `can_buses` entry (can_buses_list). peak needs PCAN-Basic, socketcan an up interface, both via python-can (`agentic-hil[can]`, else `can_backend_not_available`); process runs a bridge `executable`."}, "participant": {**NONEMPTY_STRING, "description": "Required on a bus with `shares`; any other name, or any name on a bus without `shares`, fails `can_participant_not_configured`. A broker owns the adapter; each participant gets its own filtered queue."}, "clear_rx_queue": {"type": "boolean", "default": True, "description": "Default true: discard frames already queued, also on a repeat call, counted in `frames_drained`. Bounded: a queue that keeps filling fails `can_queue_clear_limit`. Ignored for a participant."}}, required=["bus_id"])},
    {"name": "can_session_stop", "description": "End the can_session_start session: the adapter closes, or the participant detaches (others stay, the broker stops after the last), and its lease is released (a declared run keeps the bus). can_send and can_read then answer `session_not_active`. No session open: `was_active: false`. A failed close, or a bridge silent for 1 s, answers `can_adapter_close_failed` and keeps the session for a retry.", "inputSchema": object_schema({"bus_id": {**NONEMPTY_STRING, "description": "A `can_buses` entry from can_buses_list, else `can_bus_not_configured`. Example: can_session_start(bus_id=\"bench\"), can_read(bus_id=\"bench\", max_frames=10), can_session_stop(bus_id=\"bench\")."}, "participant": {**NONEMPTY_STRING, "description": "Required on a bus with `shares`: the participant whose session to close. A name with no open session, or any name on a bus without `shares`, answers `was_active: false` and closes nothing."}}, required=["bus_id"])},
    {"name": "can_send", "description": "Send one CAN frame (CAN FD on an `fd: true` bus) on the can_session_start session, else `session_not_active`. Needs the bus `allow_write`, else `permission_denied`; a `listen_only` bus refuses with `can_listen_only_mode`. A repeat sends again. `ok`: the adapter accepted it, not that a node ACKed it. `can_send_failed` sets `cleanup_required`: it may have gone out. Use this instead of cansend.", "inputSchema": object_schema({"bus_id": {**NONEMPTY_STRING, "description": "A `can_buses` entry (can_buses_list), else `can_bus_not_configured`. The adapter gets the bus `timeout_s` (default 10 s) to take the frame; replies come from can_read."}, "participant": {**NONEMPTY_STRING, "description": "Required with `shares`; needs its own `allow_write`. frame_id must pass its filter (`can_participant_filter_violation`). Sends spend its budget; `can_participant_frame_budget_exhausted` ends its run."}, "frame_id": {"oneOf": [{"type": "integer", "minimum": 0}, {"type": "string", "pattern": r"^(?:0[xX][0-9A-Fa-f]+|[0-9]+)$"}], "description": "Arbitration id: an integer, a decimal string or a 0x hex string. 0 to 0x7FF, or 0 to 0x1FFFFFFF when extended, else `invalid_argument`. The result echoes the sent `frame`."}, "extended": {"type": "boolean", "default": False, "description": "Default false: 11-bit standard id. True: 29-bit extended id."}, "rtr": {"type": "boolean", "default": False, "description": "Default false: a data frame. True sends a remote frame; an `fd: true` bus refuses it with `can_fd_remote_frame_unsupported`."}, "data_hex": {"type": "string", "default": "", "description": "Hex byte pairs, whitespace ignored; default empty. At most `max_frame_data_bytes`; classic over 8 bytes: `can_classic_frame_too_large`; FD only 0-8,12,16,20,24,32,48,64: `can_fd_frame_length_invalid`."}}, required=["bus_id", "frame_id"])},
    {"name": "can_read", "description": "Read CAN frames from the session can_session_start opened, else `session_not_active`, into `frames` with `frames_read`. Returned frames are consumed. An empty read is still ok, `frames_read: 0`. With until_id, one call waits for a frame id instead of polling. Adapter faults: `can_read_failed`; `can_adapter_invalid_response` means can_session_stop, then start again. Use this instead of candump.", "inputSchema": object_schema({"bus_id": {**NONEMPTY_STRING, "description": "A `can_buses` entry (can_buses_list), else `can_bus_not_configured`."}, "participant": {**NONEMPTY_STRING, "description": "Required on a bus with `shares`. It reads only frames its filter accepts, never its own sends. Each frame read spends its budget; `can_participant_frame_budget_exhausted` ends its run."}, "max_frames": {"type": "integer", "minimum": 1, "description": "Most frames to return, 1 to the bus `max_buffer_frames` (1024 unless configured), else `invalid_argument`. Default `max_buffer_frames`."}, "wait_timeout_s": {**TIMEOUT, "description": "Seconds to wait for frames. Without until_id: default 0 (answer at once with what is queued), at most the bus `timeout_s` (default 10 s) and 60 s. With until_id: default 10 s, at most 60 s."}, "until_id": {**UNTIL_ID, "description": "Integer id (0 to 0x1FFFFFFF) to wait for, or a list of 1 to 8. Returns the frames read through the first match. Only the numeric id is compared, not the extended flag."}}, required=["bus_id"])},
    {
        "name": "bench_run_start",
        "description": (
            "Declare a run holding the devices a sequence like flash, reset, read needs until bench_run_stop or "
            "server exit, no timeout; without one the board is free between calls. All or nothing: a refused start "
            "holds none. Other devices fail `undeclared_device`; a second start, `run_already_active`; a standing "
            "incident, `resource_quarantined`. A single call, or flash_firmware with capture, needs no run."
        ),
        "inputSchema": object_schema(
            {
                "devices": {
                    "type": "array",
                    "minItems": 1,
                    "items": DEVICE_SELECTOR,
                    "description": (
                        "The devices the run holds, one selector each, all resolved before any is locked. Success "
                        "returns them as `declared_devices`, with `run_label`."
                    ),
                },
                "label": {
                    **NONEMPTY_STRING,
                    "description": (
                        "Optional, no default: free text returned as `run_label` and shown as the holder's label to "
                        "anyone refused `device_busy`."
                    ),
                },
                "wait_s": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": 900,
                    "description": (
                        "Seconds to wait for a device another holder has, 0 to 900. Default 0: a held device fails "
                        "`device_busy` at once."
                    ),
                },
            },
            required=["devices"],
        ),
    },
    {
        "name": "bench_run_stop",
        "description": (
            "End the bench_run_start run and release its devices (`released_devices`). With no run open it answers "
            "`run_was_active: false`. A COM, CAN or debug session still open keeps its own device: `open_leases`, "
            "`still_held_devices`. With an incident open it runs the recovery `recovery.auto_recover` allows (by "
            "default reset into halt, then a probe re-read), reported in `recovery`."
        ),
        "inputSchema": EMPTY_OBJECT_SCHEMA,
    },
    {
        "name": "bench_run_status",
        "description": (
            "Report whether this server has a run open (`run_active`), what it declared (`declared_devices`, "
            "`run_label`) and which devices it holds (`held_devices`); read it when unsure whether you still hold the "
            "bench. In memory only: what another process holds shows in hardware_lease_status."
        ),
        "inputSchema": EMPTY_OBJECT_SCHEMA,
    },
    # The plan is the declaration, so this tool takes no devices and no steps:
    # what a run touches is what its reviewed file says it touches, and an
    # argument that could add to that would put the plan and the run out of step.
    # `test_config_path` therefore selects a plan and nothing else, and is held to
    # the workspace exactly as the command line holds it.
    {
        "name": "test_reactor_run",
        "description": (
            "Run this project's test plan instead of `agentic-hil test-reactor`; read "
            "agentic-hil://reference/test-plan first. Refused before any step: `permission_denied`, "
            "`run_already_active` (a bench_run_start run open), `device_busy` (at once). A failing step runs the "
            "recovery `recovery.auto_recover` allows, result `recovery`. Plans stay in workspace_root; detach: "
            "test_reactor_status, test_reactor_stop."
        ),
        "inputSchema": object_schema(
            {
                "test_config_path": {
                    **NONEMPTY_STRING,
                    "description": (
                        "Path inside workspace_root, relative to it; default `.agentic-hil/testconfig.yaml`. Missing: "
                        "`test_config_not_found`; unreadable: `test_config_unreadable`; outside or malformed: "
                        "`test_config_invalid`."
                    ),
                },
                "detach": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "Default false: answers at the plan's end with `ok`, `steps`. True: once a worker holds the "
                        "devices (30 s max), not at the plan's end: `run`, `state` or the verdict; else "
                        "`run_worker_unresponsive`."
                    ),
                },
            }
        ),
    },
    {
        "name": "test_reactor_status",
        "description": (
            "Read what a test plan run is doing: `state` starting, running, then finished or stopped, or worker_gone "
            "(its process died, see hardware_lease_status), and `stop_requested_at`. `ok` means the read worked; "
            "`run_ok` is the verdict once ended, `canonical_report_path` its own report; `report_path` is a mirror "
            "the next run overwrites. Fails `run_not_found`, `run_state_invalid`."
        ),
        "inputSchema": object_schema(
            {
                "run": {
                    **NONEMPTY_STRING,
                    "description": (
                        "A handle from test_reactor_run's `run`: `run-` and 16 hex digits, else `invalid_argument`. "
                        "Leave it out to list this bench's runs: `runs`, `active_runs`."
                    ),
                },
            }
        ),
    },
    {
        "name": "test_reactor_stop",
        "description": (
            "Ask a test plan run to stop by writing a request it reads between steps. It finishes the step it is in "
            "(a delay or device wait ends early), closes its devices and writes its report; one still starting ends "
            "before any step. Answers at once `stop_requested: true`, also on a repeat; an ended run answers "
            "`stop_requested: false`. Fails `run_state_invalid`, or `run_worker_gone` (its process died)."
        ),
        "inputSchema": object_schema(
            {
                "run": {
                    **NONEMPTY_STRING,
                    "description": (
                        "A handle from test_reactor_run's `run`: `run-` and 16 hex digits, else `invalid_argument`; "
                        "one this bench never issued fails `run_not_found`."
                    ),
                },
            },
            required=["run"],
        ),
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
            "Clear this bench's quarantine instead of deleting state files. A reason naming no hardware contact needs "
            "no argument; any other needs operator_statement: ask the operator in chat and pass their answer verbatim, "
            "never one they did not give."
        ),
        "inputSchema": object_schema(
            {
                "operator_statement": NONEMPTY_STRING,
                "accept_config_change": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "Only after the operator has reviewed the two digests a config_changed refusal reports: "
                        "accepts that the configuration changed after the incident. Recorded in the ledger beside "
                        "both digests."
                    ),
                },
            }
        ),
    },
    # Reading is free, so this takes no arguments: it answers for the bench this
    # server is bound to, as `agentic-hil lease-status` does at a shell. The
    # server instructions, AGENTS.md and the error catalogue send a caller here
    # before `hardware_recover`, so the text says which incident is its to clear.
    {
        "name": "hardware_lease_status",
        "description": (
            "Read who holds this bench, from any process (`bench_held`, `held_devices`), and any open incident: "
            "`blocked`, `cleanup_reasons`, `auto_recoverable`, `quarantine_guidance`, `next_step`. Only "
            "`incident_stands: true` needs hardware_recover; otherwise the next hardware call settles it or stands it "
            "down. `standing_incidents` are other projects' incidents, not yours to clear. Drives no device."
        ),
        "inputSchema": EMPTY_OBJECT_SCHEMA,
    },
    # No parameters, and that is the contract. The configuration is generated for
    # the workspace this server is bound to, out of what is attached to this
    # machine: a workspace_root argument would let a caller provision a project
    # this server was never pointed at, and a content argument would let it
    # decide its own permissions.
    {"name": "project_config_create", "description": "Generate this workspace's configuration from attached hardware instead of writing one by hand: every permission true except allow_raw_debugger_commands and allow_mass_erase, which it writes false. Never ask for those two to be turned on. Regenerating is the operator's call, not a way to narrow or re-open.", "inputSchema": EMPTY_OBJECT_SCHEMA},
    # Reading is free, so this takes no arguments either: it answers for the one
    # configuration this server is bound to, in the state it is in.
    {
        "name": "project_config_describe",
        "description": (
            "List the configuration keys you may change and the permission that opens each locked one; read it before "
            "project_config_set instead of guessing."
        ),
        "inputSchema": EMPTY_OBJECT_SCHEMA,
    },
    # Field-wise and scalar-valued, and both halves of that are the contract.
    # There is no argument that takes a document, and `value` admits no object
    # and no array, so no subtree, and therefore no permissions: block hidden
    # inside one, can arrive as a value. The agent names keys from a closed set;
    # it does not author this file.
    {
        "name": "project_config_set",
        "description": (
            "Change named configuration keys instead of editing the configuration file yourself. "
            "allow_config_description_write gates the device description, allow_config_permissions_write the "
            "permissions, which can only be narrowed."
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
    # configured entry receives it, and nothing that could put a value of the
    # caller's choosing into the file. The values come from what is attached to
    # this machine, exactly as project_config_create's do.
    {
        "name": "project_config_adopt_hardware",
        "description": (
            "Fill this configuration's hardware placeholders from the attached probe instead of printing values for a "
            "person to retype; writes only with apply: true."
        ),
        "inputSchema": object_schema(
            {
                "apply": {"type": "boolean", "default": False, "description": "Write the plan. Without it the call reads hardware and the configuration and changes nothing."},
                "probe_id": {**NONEMPTY_STRING, "description": "Which attached probe this is about. Needed when more than one is attached; it selects among the attached probes and never adds one."},
                "debugger_id": {**NONEMPTY_STRING, "description": "Which configured debugger entry receives the values. Only needed when the configuration declares more than one."},
                "com_port_id": {**NONEMPTY_STRING, "description": "Which com_ports entry receives the discovered device. Created with every permission false if it does not exist."},
            }
        ),
    },
    # No arguments, and that is the contract twice over. There is nothing to
    # select (this server is bound to one configuration and re-reads that one),
    # and there is no flag, because a flag is where "also take the permissions"
    # would eventually be asked for, and the answer to that is that no such flag
    # exists on this surface at all.
    {
        "name": "project_config_reload_description",
        "description": (
            "Re-read target, debuggers, com_ports and can_buses (never permissions) from the configuration file after a "
            "board was added, instead of asking for a restart."
        ),
        "inputSchema": EMPTY_OBJECT_SCHEMA,
    },
    # No arguments, and here the empty schema is the security property rather
    # than a convenience. There is deliberately no way to name a
    # version: this tool lifts the installation to the newest release and can do
    # nothing else. A `version` argument would put the whole permission model
    # behind it: an agent that can install 0.7.x installs a release that reads
    # `permissions:` under weaker rules, and every narrowing an operator made is
    # undone by a downgrade rather than by a permission change. Downgrades and
    # exact versions stay at the shell, with the operator, and
    # `additionalProperties: false` is what refuses one that arrives anyway.
    {
        "name": "server_upgrade",
        "description": (
            "Upgrade this installation on disk to the newest release instead of running uv, pipx or pip; takes no "
            "arguments, never a version you name."
        ),
        "inputSchema": EMPTY_OBJECT_SCHEMA,
    },
]

# ---------------------------------------------------------------------------
# Tool annotations.
#
# `ToolAnnotations` as defined in the MCP schema for revision 2025-06-18 (the
# version this server advertises in `initialize`) and unchanged word for word
# in the current 2026-07-28 revision: `title`, `readOnlyHint`,
# `destructiveHint`, `idempotentHint`, `openWorldHint`.
#
# The title goes in `annotations.title` and not in the tool's own top-level
# `title`, deliberately. Both exist and the top-level one wins where a host
# supports it, but it only arrives with 2025-06-18 while `annotations.title`
# has been understood since 2025-03-26, and this server still negotiates down
# to 2024-11-05. One string, in the field the widest set of revisions reads.
#
# Without them a host has the tool's name and nothing else, and it judges by
# appearance: `project_config_reload_description` sits beside
# `project_config_set` and `project_config_create`, which do write, so a host
# blocked the one call that re-reads the file and the operator reconnected twice
# instead. The description says the reload re-reads the file, in prose, not in
# the field a machine reads.
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
# neither: a `destructiveHint` beside `readOnlyHint: true` is a contradiction,
# not a belt-and-braces.
#
# **They are hints, and this server enforces nothing by them.** The schema says
# so ("all properties in ToolAnnotations are hints ... clients should never make
# tool use decisions based on ToolAnnotations received from untrusted servers"),
# and nothing in this package reads this table to decide anything. What decides
# is the authoritative configuration's permissions and the machine-wide device
# locks, exactly as before.
#
# What `readOnlyHint` is measured against here: the bench (the target and its
# probe, the configured COM ports and CAN buses, the workspace and the
# authoritative configuration). It is *not* measured against this server's own
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
# read what is physically attached rather than what is configured
# (`debugger_probes_list`, `com_ports_list`, `project_config_adopt_hardware` and
# `project_config_create`) are bounded by one machine's hardware, which is
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
    # reads the host's USB serial inventory instead and reports complete false;
    # only an adapter that publishes no USB serial identity there is refused
    # not_supported (openocd.py). Nothing on the board is touched.
    "debugger_probes_list": {"title": "List connected debug probes", "readOnlyHint": True, "openWorldHint": False},
    # The arguable one, and it is deliberately *not* read-only.
    #
    # It reads, and it connects without resetting (OpenOCD `init; targets`,
    # pyOCD `status`, ST-Link `-HOTPLUG`), which is exactly why recovery is
    # allowed to use it as a predicate (docs/security-design.md). But connecting
    # is not nothing, and this repository has already said so twice, in the
    # quarantine inventory (knowledge.py) and in
    # docs/security-design.md: "being read-only is not being passive: an SWD
    # attach halts the core". Bringing a running core under debug control is a
    # change to the target's execution state, and the failure path treats it as
    # one: a probe that names no abort point leaves the physical state unknown
    # and is settled by a verified reset-into-halt, never by re-reading, which
    # is not what one does about a call that changed nothing. The repository's
    # own classification agrees: `probe_target` is in `debugger_effect_tools()`.
    #
    # So: it acts on the board, reversibly (destructiveHint false: nothing is
    # erased and a reset restores execution) and repeatably (idempotentHint true:
    # it connects, reads, disconnects, and leaves the target where the first
    # call left it).
    "probe_target": {"title": "Probe the target", "readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    # Content-addressed: the artifact id is `sha256(bytes) + suffix` and the
    # stored path is that id, so the only file a second upload of the same bytes
    # can overwrite is the byte-identical one already there, and a symlink
    # destination is refused outright. Additive, and idempotent by construction.
    "artifact_upload": {"title": "Upload a firmware artifact", "readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    # The irreversible one. Erases and programs the target's flash: what was on
    # the device before the call cannot be recovered from anything this server
    # holds. Not idempotent: a second flash of the same image erases and writes
    # the same sectors again, and with `reset_after_flash` resets the board
    # again; the end state matches but the call is not free.
    "flash_firmware": {"title": "Flash firmware", "readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": False},
    # Resets the running target. Nothing stored is lost, so this is not
    # destructive; it is also not idempotent, because a second reset throws away
    # whatever the firmware did since the first one.
    "reset_target": {"title": "Reset the target", "readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False},
    # Destructive because of one mode: `mode: "load"` programs the ELF into
    # flash, which is why the gate demands `allow_flash` for it (tools.py): the
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
    # Resumes the target, which then runs. Not idempotent: a second continue
    # runs further from wherever the first one stopped.
    "debug_continue": {"title": "Continue the target", "readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False},
    # Interrupts the target and waits for the stop to be confirmed. The stop is
    # an effect, so not read-only; nothing is lost by it, so not destructive.
    "debug_halt": {"title": "Halt the target", "readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False},
    "debug_get_stop_reason": {"title": "Last stop reason", "readOnlyHint": True, "openWorldHint": False},
    # Answered out of the ELF's debug information: on OpenOCD by two GDB
    # expression evaluations, `(unsigned long)&symbol` and `sizeof(symbol)`,
    # through the live session; on pyOCD and STM32CubeProgrammer from the ELF
    # last flashed, without a session. No target memory is read and nothing
    # is written, so this is a read.
    "debug_symbol_info": {"title": "Resolve a debug symbol", "readOnlyHint": True, "openWorldHint": False},
    # Reads target memory and returns it. A read changes nothing on the board,
    # and unlike the dump below it writes no file either, so nothing anywhere
    # is replaced, and this is the one memory-reading tool that is read-only in
    # the full sense. Repeating it is free in the same way `com_read` is:
    # the bytes may differ because the firmware moved on, which is the target's
    # doing and not the call's.
    "debug_symbol_value": {"title": "Read a symbol's value", "readOnlyHint": True, "openWorldHint": False},
    # Reads target memory, which changes nothing, and then writes the Intel HEX
    # file at `output_path`: an atomic replace of whatever was at that path
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
    # modem lines, and `assert_dtr`/`assert_rts` default to true: on a board
    # that wires DTR to reset, opening the port to listen restarts the target,
    # which comports.py says in as many words. And `clear_buffer` defaults to
    # true, so a call against an already-open session purges the driver's input
    # buffer and the session's own: received bytes that no read returned are
    # gone. That purge is also why it is not idempotent.
    "com_session_start": {"title": "Open a COM session", "readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": False},
    # Containment. Closing with no session open answers `ok` with
    # `was_active: false`.
    "com_session_stop": {"title": "Close a COM session", "readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    # The bytes themselves are additive (a stream is appended to), but what the
    # target does with them is the firmware's business and not knowable from
    # here, and stimulus cannot be taken back off the wire. This is the tool
    # `allow_write` exists to gate, and the hint should not read safer than the
    # permission does.
    "com_write": {"title": "Write to a COM port", "readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": False},
    # Read-only, with one thing worth stating plainly: it *consumes*. The bytes
    # it returns are deleted from the session buffer, so two calls do not return
    # the same data. What it does not do is touch the port, the target, or the
    # configuration: it never reaches the serial handle at all, only the buffer
    # a background reader fills. `readOnlyHint` asks about the environment, and
    # the environment is unchanged.
    "com_read": {"title": "Read from a COM port", "readOnlyHint": True, "openWorldHint": False},
    # The configuration plus each adapter's own `status()`. No adapter is opened
    # and no frame is exchanged.
    "can_buses_list": {"title": "List CAN buses", "readOnlyHint": True, "openWorldHint": False},
    # Opening a CAN channel puts a node on a shared bus: on PCAN the channel is
    # initialized and ACKs, and at a wrong bitrate emits error frames: can.py
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
    # `recv()` transmits nothing (can.py relies on exactly that to prove a
    # failed read sent no frame), so the bus is untouched. Like com_read it
    # consumes: the frames it returns are off the queue.
    "can_read": {"title": "Read CAN frames", "readOnlyHint": True, "openWorldHint": False},
    # Takes the machine-wide locks for every declared device and holds them
    # until bench_run_stop. It destroys nothing. It is not idempotent either: a
    # second call while a run is open is refused with `run_already_active`
    # rather than being absorbed, so a host must not treat repeating it as free.
    "bench_run_start": {"title": "Declare a bench run", "readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False},
    # Releases the run's devices, and closing a run that is not open is how an
    # agent recovers from losing track of its own state: it answers `ok` with
    # `run_was_active: false` and writes nothing.
    "bench_run_stop": {"title": "End the bench run", "readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    # In-memory only; it does not even read the holder files.
    "bench_run_status": {"title": "Bench run status", "readOnlyHint": True, "openWorldHint": False},
    # Annotated for what the plan it runs may do, the same rule
    # `debug_start_session` is annotated by: a hint cannot be conditional on the
    # file a caller names, and a plan is free to contain a `flash` step, which is
    # the irreversible write flash_firmware carries `destructiveHint: true` for.
    # A plan that only reads is not the one the hint has to be safe for. Not
    # idempotent for the same reason: a second run flashes, resets and stimulates
    # again, and a plan bounded by wall time does not even run the same length.
    "test_reactor_run": {"title": "Run a test plan", "readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": False},
    # Reads the run records under `state_root` and asks the operating system
    # whether the lock behind a handle is still held. It writes nothing and
    # reaches no device.
    "test_reactor_status": {"title": "Test run status", "readOnlyHint": True, "openWorldHint": False},
    # Not read-only: it leaves a stop request under the handle, and what that
    # changes is when the run ends. Not destructive to anything: the run it ends
    # closes its devices in the same order and writes the same report a passing
    # run does, so nothing is left half done and nothing on the target is erased
    # that the plan had not already written. Idempotent: a second request on a
    # still-running run asks for the end that is already coming, and one on a run
    # that has ended answers `ok` with `stop_requested: false` and writes nothing.
    "test_reactor_stop": {"title": "Stop a test run", "readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    # Not read-only: it rewrites this project's lease records and appends to the
    # recovery ledger, and what that changes is which calls the bench will accept
    # next. Not destructive, and that is a claim about what it reaches rather
    # than a comfortable default: it never drives the target, never opens a
    # port or a bus, and only ever clears an incident whose evidence chain is
    # what broke; the ones that might have left a board somewhere settle
    # themselves at the next contact and are not its to clear. Idempotent: a
    # second call finds nothing standing and answers `ok` with
    # `nothing_to_recover: true`.
    "hardware_recover": {"title": "Clear a bench quarantine", "readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    # Reads this project's lease and incident records and the device holds on
    # this machine, and changes neither: the end-of-call seam that recovers and
    # stands an incident down passes this call by, so the answer describes the
    # bench as the call found it and leaves it that way.
    "hardware_lease_status": {"title": "Bench lease status", "readOnlyHint": True, "openWorldHint": False},
    # Rewrites the authoritative configuration with no backup on disk, and
    # carries over the permissions of the document *this server loaded at
    # startup*, so a narrowing made with project_config_set in this session is
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
    # nothing. No configuration write, no report, no audit record:
    # configreload.py contains no write call of any kind and the tool is not in
    # `audited_hardware_tools()`. It contacts no hardware. What it changes is
    # this process's own view: it re-reads the authoritative file and swaps in
    # the device description, leaving every permission byte-for-byte as parsed
    # at startup. A second call against the same file reports that nothing
    # moved.
    "project_config_reload_description": {"title": "Reload the bench description", "readOnlyHint": True, "openWorldHint": False},
    # Not read-only: it replaces the installed package on disk. Not destructive
    # either, and the distinction is the point: nothing is erased that this
    # server holds, the configuration and the bench are untouched, and the
    # previous release is a `uv tool install "agentic-hil==X.Y.Z"` away because
    # the index still has it. Idempotent: it can only lift to the newest release,
    # so a second call finds the installation already there and answers
    # `already_current` without replacing anything. `openWorldHint: true` because
    # the package comes off a network index. See the note above.
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
    # catalogue's fix is attached: the same one `agentic-hil://reference/errors`
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
