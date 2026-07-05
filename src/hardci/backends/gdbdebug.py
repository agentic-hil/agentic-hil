from __future__ import annotations

import re
import socket
import subprocess
import threading
import time
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path

from hardci.config import display_path
from hardci.gdbmi import (
    GdbMiClient,
    GdbMiStopResult,
    mi_field,
    mi_string,
    parse_gdb_integer,
    write_intel_hex_file,
)
from hardci.report import logs_directory, timestamp_for_filename, utc_now_iso, write_report
from hardci.types import HardCIConfig, JsonObject

DEBUG_MODES = ["attach", "reset_halt", "load"]
GDB_AUTODETECT_CANDIDATES = ["arm-none-eabi-gdb", "gdb-multiarch", "gdb"]
DEBUG_SYMBOL_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:::[A-Za-z_][A-Za-z0-9_]*)*$")
BREAKPOINT_FILE_PATTERN = re.compile(r"^[A-Za-z0-9_./\\:-]+$")
MEMORY_CONTENTS_PATTERN = re.compile(r"^(?:[0-9a-fA-F]{2})*$")
FAULT_MARKERS = ["hardfault", "memmanage", "busfault", "usagefault"]
TCP_POLL_INTERVAL_S = 0.05
TCP_CONNECT_TIMEOUT_S = 0.2
MEMORY_READ_CHUNK_BYTES = 1024
GDB_COMMAND_TIMEOUT_CAP_S = 10.0
CONTINUE_COMMAND_TIMEOUT_CAP_S = 5.0
STOP_SESSION_TIMEOUT_CAP_S = 5.0
CLOSE_SESSION_TIMEOUT_S = 1.0
OUTPUT_TAIL_CHARS = 65536


class GdbDebugSession:
    def __init__(self, session_id: str, artifact: JsonObject, mode: str, gdb_port: int, server: subprocess.Popen[str], server_args: list[str], log_path: str):
        self.session_id = session_id
        self.artifact = artifact
        self.mode = mode
        self.gdb_port = gdb_port
        self.server = server
        self.server_args = server_args
        self.log_path = log_path
        self.started_at = utc_now_iso()
        self.status = "starting"
        self.stop_reason: JsonObject | None = None
        self.breakpoints: list[JsonObject] = []
        self.next_breakpoint_id = 1
        self.gdb: GdbMiClient | None = None
        self.server_stdout = ""
        self.server_stderr = ""


class GdbDebugSessions:
    """Typed GDB/MI debug sessions against a gdbserver-providing debugger process (e.g. OpenOCD)."""

    def __init__(
        self,
        config: HardCIConfig,
        backend_name: str,
        resolve_server: Callable[[], JsonObject],
        build_server_args: Callable[[str, int, bool], list[str]],
        classify_server_output: Callable[[str], str],
    ):
        self.config = config
        self.backend_name = backend_name
        self._resolve_server = resolve_server
        self._build_server_args = build_server_args
        self._classify_server_output = classify_server_output
        self.session: GdbDebugSession | None = None

    def start_session(self, artifact: JsonObject, mode: str = "attach", timeout_s: float | None = None) -> JsonObject:
        tool = "hardci_debug_start_session"
        if mode not in DEBUG_MODES:
            return self._report({"ok": False, "tool": tool, "backend": self.backend_name, "error_type": "invalid_argument", "summary": "Invalid debug session mode.", "allowed_values": DEBUG_MODES})
        if self.session is not None and self.session.status != "stopped":
            return self._report({"ok": False, "tool": tool, "backend": self.backend_name, "error_type": "session_already_active", "summary": "A debug session is already active. Stop it with hardci_debug_stop_session first.", "session": self._session_status(self.session)})
        permission = self._start_permission(tool, mode)
        if not permission["ok"]:
            return self._report(permission)
        resolved_server = self._resolve_server()
        if not resolved_server.get("ok"):
            return self._report({"tool": tool, **resolved_server})
        resolved_gdb = self._resolve_gdb()
        if not resolved_gdb["ok"]:
            return self._report({"tool": tool, **resolved_gdb})

        timeout = self.config.debugger.timeout_s if timeout_s is None else max(0.1, timeout_s)
        started_at = utc_now_iso()
        start = time.perf_counter()
        gdb_port = reserve_tcp_port()
        server_args = self._build_server_args(str(resolved_server["executable_path"]), gdb_port, mode != "attach")
        log_path = str(Path(logs_directory(self.config)) / f"gdb-debug-{timestamp_for_filename()}.json")
        try:
            server = subprocess.Popen(server_args, cwd=self.config.work_dir, text=True, encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except OSError as error:
            return self._report({"ok": False, "tool": tool, "backend": self.backend_name, "error_type": "debugger_not_found", "summary": "Debug server process could not be started.", "backend_error": str(error)})

        session = GdbDebugSession(f"debug-{timestamp_for_filename()}", artifact, mode, gdb_port, server, server_args, log_path)
        self._start_output_readers(session)
        self.session = session

        if not wait_for_tcp_port(gdb_port, timeout, server):
            failure = self._start_failure(session, tool, started_at, start, timed_out=server.poll() is None)
            self._cleanup_session(session, STOP_SESSION_TIMEOUT_CAP_S)
            session.status = "error"
            return self._report(failure)

        session.gdb = GdbMiClient(str(resolved_gdb["executable"]), self.config.work_dir)
        initialized = self._initialize_gdb(session, timeout)
        if not initialized["ok"]:
            self._cleanup_session(session, STOP_SESSION_TIMEOUT_CAP_S)
            session.status = "error"
            return self._report({"tool": tool, "backend": self.backend_name, "started_at": started_at, **initialized, "log_path": display_path(self.config, log_path)})

        session.status = "halted"
        self._write_session_log(session)
        return self._report({
            "ok": True,
            "tool": tool,
            "backend": self.backend_name,
            "started_at": started_at,
            "finished_at": utc_now_iso(),
            "elapsed_ms": int((time.perf_counter() - start) * 1000),
            "session": self._session_status(session),
            "artifact": public_artifact(artifact),
            "mode": mode,
            "gdb_port": gdb_port,
            "log_path": display_path(self.config, log_path),
            "summary": "Debug session started and target is halted.",
        })

    def stop_session(self, timeout_s: float | None = None) -> JsonObject:
        tool = "hardci_debug_stop_session"
        session = self.session
        if session is None or session.status == "stopped":
            return {"ok": True, "tool": tool, "backend": self.backend_name, "active": False, "status": "stopped", "summary": "No debug session is active."}
        timeout = min(self.config.debugger.timeout_s, STOP_SESSION_TIMEOUT_CAP_S) if timeout_s is None else max(0.1, timeout_s)
        self._cleanup_session(session, timeout)
        session.status = "stopped"
        self.session = None
        return self._report({"ok": True, "tool": tool, "backend": self.backend_name, "active": False, "status": "stopped", "session": self._session_status(session), "log_path": display_path(self.config, session.log_path), "summary": "Debug session stopped."})

    def get_session_status(self) -> JsonObject:
        session = self.session
        active = session is not None and session.status not in {"stopped", "error"}
        return {"ok": True, "tool": "hardci_debug_get_session_status", "backend": self.backend_name, "active": active, "status": session.status if session else "stopped", "session": self._session_status(session) if session else None}

    def set_breakpoint(self, location: JsonObject | str) -> JsonObject:
        tool = "hardci_debug_set_breakpoint"
        session_result = self._require_session(tool)
        if not session_result["ok"]:
            return self._report(session_result)
        session = session_result["session"]
        normalized = normalize_breakpoint_location(tool, location)
        if not normalized["ok"]:
            return self._report(normalized)
        response = self._gdb_command(session, f"-break-insert {mi_string(normalized['gdb_location'])}")
        if not response.ok:
            return self._report(self._gdb_failure(tool, session, response.error_message, response.timed_out))
        breakpoint = {"id": session.next_breakpoint_id, "backend_id": mi_field(response.line, "number"), "location": normalized["location"], "gdb_location": normalized["gdb_location"]}
        session.next_breakpoint_id += 1
        session.breakpoints.append(breakpoint)
        self._write_session_log(session)
        return self._report({"ok": True, "tool": tool, "backend": self.backend_name, "breakpoint": breakpoint, "session": self._session_status(session), "log_path": display_path(self.config, session.log_path), "summary": "Breakpoint set."})

    def list_breakpoints(self) -> JsonObject:
        session = self.session
        active = session is not None and session.status not in {"stopped", "error"}
        return {"ok": True, "tool": "hardci_debug_list_breakpoints", "backend": self.backend_name, "active": active, "breakpoints": list(session.breakpoints) if session else []}

    def clear_breakpoints(self) -> JsonObject:
        tool = "hardci_debug_clear_breakpoints"
        session_result = self._require_session(tool)
        if not session_result["ok"]:
            return self._report(session_result)
        session = session_result["session"]
        for breakpoint in session.breakpoints:
            if breakpoint.get("backend_id"):
                response = self._gdb_command(session, f"-break-delete {breakpoint['backend_id']}")
                if not response.ok:
                    return self._report(self._gdb_failure(tool, session, response.error_message, response.timed_out))
        cleared = len(session.breakpoints)
        session.breakpoints = []
        self._write_session_log(session)
        return self._report({"ok": True, "tool": tool, "backend": self.backend_name, "cleared": cleared, "session": self._session_status(session), "log_path": display_path(self.config, session.log_path), "summary": "All breakpoints cleared."})

    def continue_execution(self, timeout_s: float | None = None) -> JsonObject:
        tool = "hardci_debug_continue"
        session_result = self._require_session(tool)
        if not session_result["ok"]:
            return self._report(session_result)
        session = session_result["session"]
        timeout = self.config.debugger.timeout_s if timeout_s is None else max(0.1, timeout_s)
        session.status = "running"
        session.stop_reason = None
        response = self._gdb_command(session, "-exec-continue", min(timeout, CONTINUE_COMMAND_TIMEOUT_CAP_S))
        if response.result_class not in {"running", "done"}:
            session.status = "error"
            return self._report(self._gdb_failure(tool, session, response.error_message, response.timed_out))
        assert session.gdb is not None
        stop = session.gdb.wait_for_stop(timeout)
        if stop.timed_out:
            self._gdb_command(session, "-exec-interrupt --all", min(CONTINUE_COMMAND_TIMEOUT_CAP_S, self.config.debugger.timeout_s))
            session.gdb.wait_for_stop(min(CONTINUE_COMMAND_TIMEOUT_CAP_S, self.config.debugger.timeout_s))
            session.status = "halted"
            session.stop_reason = {"stop_reason": "timeout", "backend_stop_reason": "timeout"}
            self._write_session_log(session)
            return self._report({"ok": False, "tool": tool, "backend": self.backend_name, "error_type": "timeout", "summary": "Target did not stop before the timeout; it was halted.", "stop_reason": "timeout", "stop": session.stop_reason, "session": self._session_status(session), "log_path": display_path(self.config, session.log_path)})
        session.stop_reason = self._stop_reason_from_gdb(session, stop)
        stop_reason = str(session.stop_reason.get("stop_reason"))
        session.status = "error" if stop_reason == "debugger_error" else "halted"
        ok = stop_reason not in {"fault", "debugger_error"}
        result: JsonObject = {"ok": ok, "tool": tool, "backend": self.backend_name, "stop_reason": stop_reason, "stop": session.stop_reason, "session": self._session_status(session), "log_path": display_path(self.config, session.log_path), "summary": f"Target stopped: {stop_reason}."}
        if not ok:
            result["error_type"] = "target_fault" if stop_reason == "fault" else "debugger_error"
        self._write_session_log(session)
        return self._report(result)

    def halt(self, timeout_s: float | None = None) -> JsonObject:
        tool = "hardci_debug_halt"
        session_result = self._require_session(tool)
        if not session_result["ok"]:
            return self._report(session_result)
        session = session_result["session"]
        timeout = min(self.config.debugger.timeout_s, GDB_COMMAND_TIMEOUT_CAP_S) if timeout_s is None else max(0.1, timeout_s)
        response = self._gdb_command(session, "-exec-interrupt --all", timeout)
        if not response.ok:
            return self._report(self._gdb_failure(tool, session, response.error_message, response.timed_out))
        assert session.gdb is not None
        stop = session.gdb.wait_for_stop(timeout)
        session.status = "halted"
        session.stop_reason = self._stop_reason_from_gdb(session, stop)
        self._write_session_log(session)
        return self._report({"ok": True, "tool": tool, "backend": self.backend_name, "stop": session.stop_reason, "session": self._session_status(session), "log_path": display_path(self.config, session.log_path), "summary": "Target halted."})

    def get_stop_reason(self) -> JsonObject:
        tool = "hardci_debug_get_stop_reason"
        session_result = self._require_session(tool)
        if not session_result["ok"]:
            return self._report(session_result)
        session = session_result["session"]
        if session.stop_reason is None:
            return {"ok": False, "tool": tool, "backend": self.backend_name, "error_type": "stop_reason_not_available", "summary": "No stop reason has been recorded yet. Run hardci_debug_continue or hardci_debug_halt first."}
        return {"ok": True, "tool": tool, "backend": self.backend_name, "stop_reason": session.stop_reason.get("stop_reason"), "stop": session.stop_reason, "session": self._session_status(session)}

    def symbol_info(self, symbol: str) -> JsonObject:
        tool = "hardci_debug_symbol_info"
        session_result = self._require_session(tool)
        if not session_result["ok"]:
            return self._report(session_result)
        session = session_result["session"]
        resolved = self._resolve_symbol(tool, session, symbol)
        if not resolved["ok"]:
            return self._report(resolved)
        return self._report({**resolved, "tool": tool, "backend": self.backend_name, "session": self._session_status(session), "log_path": display_path(self.config, session.log_path), "summary": "Symbol resolved."})

    def dump_symbol_ihex(self, symbol: str, output: JsonObject) -> JsonObject:
        tool = "hardci_debug_dump_symbol_ihex"
        session_result = self._require_session(tool)
        if not session_result["ok"]:
            return self._report(session_result)
        session = session_result["session"]
        resolved = self._resolve_symbol(tool, session, symbol)
        if not resolved["ok"]:
            return self._report(resolved)
        size_bytes = int(resolved["size_bytes"])
        if size_bytes > self.config.debug.max_dump_size_bytes:
            return self._report({"ok": False, "tool": tool, "backend": self.backend_name, "error_type": "permission_denied", "summary": "Symbol dump exceeds debug.max_dump_size_bytes.", "symbol": symbol, "size_bytes": size_bytes, "max_dump_size_bytes": self.config.debug.max_dump_size_bytes})
        memory = self._read_memory_bytes(tool, session, int(resolved["address_value"]), size_bytes)
        if not memory["ok"]:
            return self._report(memory)
        try:
            write_intel_hex_file(Path(str(output["resolved_path"])), int(resolved["address_value"]), memory["data"])
        except OSError as error:
            return self._report({"ok": False, "tool": tool, "backend": self.backend_name, "error_type": "output_write_failed", "summary": "Intel HEX output file could not be written.", "backend_error": str(error)})
        self._write_session_log(session)
        return self._report({"ok": True, "tool": tool, "backend": self.backend_name, "symbol": symbol, "address": resolved["address"], "size_bytes": size_bytes, "output": output, "session": self._session_status(session), "log_path": display_path(self.config, session.log_path), "summary": "Symbol memory dumped as Intel HEX."})

    def close(self) -> None:
        session = self.session
        if session is not None and session.status != "stopped":
            self._cleanup_session(session, CLOSE_SESSION_TIMEOUT_S)
            session.status = "stopped"
        self.session = None

    def _start_permission(self, tool: str, mode: str) -> JsonObject:
        permissions = self.config.permissions
        if not permissions.allow_probe:
            return self._permission_denied(tool, "Debug sessions require allow_probe in .hardci/config.yaml.")
        if permissions.allow_raw_debugger_commands:
            return self._permission_denied(tool, "Debug sessions are disabled while raw debugger commands are allowed.")
        if mode != "attach" and not permissions.allow_reset:
            return self._permission_denied(tool, f"Debug session mode '{mode}' requires allow_reset in .hardci/config.yaml.")
        if mode == "load":
            if not permissions.allow_flash:
                return self._permission_denied(tool, "Debug session mode 'load' requires allow_flash in .hardci/config.yaml.")
            if permissions.allow_mass_erase:
                return self._permission_denied(tool, "Debug session mode 'load' is disabled while mass erase is allowed.")
        return {"ok": True}

    def _permission_denied(self, tool: str, summary: str) -> JsonObject:
        return {"ok": False, "tool": tool, "backend": self.backend_name, "error_type": "permission_denied", "summary": summary}

    def _resolve_gdb(self) -> JsonObject:
        from hardci.backends.common import which

        configured = self.config.debug.gdb_executable
        if configured:
            has_path_separator = "/" in configured or "\\" in configured
            if Path(configured).is_absolute() or has_path_separator:
                from hardci.config import resolve_work_path

                resolved = Path(resolve_work_path(self.config, configured))
                if resolved.is_file():
                    return {"ok": True, "executable": str(resolved)}
            else:
                found = which(configured)
                if found is not None:
                    return {"ok": True, "executable": found}
            return {"ok": False, "backend": self.backend_name, "error_type": "gdb_not_found", "summary": "Configured debug.gdb_executable could not be found.", "likely_causes": ["debug.gdb_executable points to a missing file", "GDB is not installed"]}
        for candidate in GDB_AUTODETECT_CANDIDATES:
            found = which(candidate)
            if found is not None:
                return {"ok": True, "executable": found}
        return {"ok": False, "backend": self.backend_name, "error_type": "gdb_not_found", "summary": "No GDB executable could be found.", "likely_causes": ["install arm-none-eabi-gdb or gdb-multiarch", "set debug.gdb_executable in .hardci/config.yaml"]}

    def _initialize_gdb(self, session: GdbDebugSession, timeout: float) -> JsonObject:
        commands = [
            "-gdb-set pagination off",
            "-gdb-set confirm off",
            f"-file-exec-and-symbols {mi_string(str(session.artifact['resolved_path']))}",
            f"-target-select extended-remote localhost:{session.gdb_port}",
        ]
        if session.mode != "attach":
            commands.append('-interpreter-exec console "monitor reset halt"')
        if session.mode == "load":
            commands.append("-target-download")
            commands.append('-interpreter-exec console "monitor reset halt"')
        for command in commands:
            response = self._gdb_command(session, command, min(timeout, GDB_COMMAND_TIMEOUT_CAP_S))
            if not response.ok:
                return self._gdb_failure("hardci_debug_start_session", session, response.error_message or f"GDB startup command failed: {command}", response.timed_out)
        return {"ok": True}

    def _require_session(self, tool: str) -> JsonObject:
        session = self.session
        if session is None or session.status in {"stopped", "error"} or session.gdb is None or not session.gdb.is_running():
            return {"ok": False, "tool": tool, "backend": self.backend_name, "error_type": "session_not_active", "summary": "No debug session is active. Start one with hardci_debug_start_session first."}
        return {"ok": True, "session": session}

    def _gdb_command(self, session: GdbDebugSession, command: str, timeout_s: float | None = None):
        assert session.gdb is not None
        timeout = min(self.config.debugger.timeout_s, GDB_COMMAND_TIMEOUT_CAP_S) if timeout_s is None else timeout_s
        response = session.gdb.command(command, timeout)
        self._write_session_log(session)
        if not response.ok:
            session.stop_reason = {"stop_reason": "debugger_error", "backend_stop_reason": "timeout" if response.timed_out else "error", "backend_error": response.error_message}
        return response

    def _gdb_failure(self, tool: str, session: GdbDebugSession, message: str | None, timed_out: bool) -> JsonObject:
        error_type = "timeout" if timed_out else "debugger_error"
        backend_error_type = "gdb_timeout" if timed_out else "gdb_error"
        return {"ok": False, "tool": tool, "backend": self.backend_name, "error_type": error_type, "backend_error_type": backend_error_type, "summary": message or "GDB/MI command failed.", "session": self._session_status(session), "log_path": display_path(self.config, session.log_path)}

    def _stop_reason_from_gdb(self, session: GdbDebugSession, stop: GdbMiStopResult) -> JsonObject:
        if stop.timed_out:
            return {"stop_reason": "timeout", "backend_stop_reason": "timeout"}
        if stop.error_message:
            return {"stop_reason": "debugger_error", "backend_stop_reason": stop.reason, "backend_error": stop.error_message}
        lower = stop.line.lower()
        if stop.reason == "breakpoint-hit":
            stop_reason = "breakpoint_hit"
        elif stop.reason in {"exited-normally", "exited"}:
            stop_reason = "target_exit"
        elif stop.reason == "signal-received" or any(marker in lower for marker in FAULT_MARKERS):
            stop_reason = "fault"
        elif "reset_handler" in lower or "reset" in lower:
            stop_reason = "reset"
        elif stop.reason == "debugger-error":
            stop_reason = "debugger_error"
        else:
            stop_reason = "unknown"
        result: JsonObject = {"stop_reason": stop_reason, "backend_stop_reason": stop.reason}
        backend_breakpoint_id = mi_field(stop.line, "bkptno")
        if backend_breakpoint_id is not None:
            result["backend_breakpoint_id"] = backend_breakpoint_id
            matching = next((item for item in session.breakpoints if item.get("backend_id") == backend_breakpoint_id), None)
            if matching is not None:
                result["breakpoint_id"] = matching["id"]
                result["breakpoint"] = matching
        frame: JsonObject = {}
        for source_field, target_field in [("func", "function"), ("addr", "address"), ("file", "file")]:
            value = mi_field(stop.line, source_field)
            if value is not None:
                frame[target_field] = value
        line_number = parse_gdb_integer(mi_field(stop.line, "line"))
        if line_number is not None:
            frame["line"] = line_number
        if frame:
            result["frame"] = frame
        return result

    def _resolve_symbol(self, tool: str, session: GdbDebugSession, symbol: str) -> JsonObject:
        validated = self._validate_symbol(tool, symbol)
        if not validated["ok"]:
            return validated
        address_response = self._gdb_command(session, f"-data-evaluate-expression {mi_string(f'(unsigned long)&{symbol}')}")
        if not address_response.ok:
            return self._symbol_failure(tool, symbol, address_response.error_message, address_response.timed_out)
        address_value = parse_gdb_integer(mi_field(address_response.line, "value"))
        size_response = self._gdb_command(session, f"-data-evaluate-expression {mi_string(f'sizeof({symbol})')}")
        if not size_response.ok:
            return self._symbol_failure(tool, symbol, size_response.error_message, size_response.timed_out)
        size_value = parse_gdb_integer(mi_field(size_response.line, "value"))
        if address_value is None or size_value is None:
            return {"ok": False, "tool": tool, "backend": self.backend_name, "error_type": "symbol_resolution_failed", "summary": "GDB returned an unparsable symbol address or size.", "symbol": symbol}
        return {"ok": True, "symbol": symbol, "address": hex(address_value), "address_value": address_value, "size_bytes": size_value}

    def _validate_symbol(self, tool: str, symbol: str) -> JsonObject:
        if not isinstance(symbol, str) or DEBUG_SYMBOL_PATTERN.match(symbol) is None:
            return {"ok": False, "tool": tool, "backend": self.backend_name, "error_type": "invalid_argument", "summary": "symbol must be a valid C/C++ identifier."}
        allowed = self.config.debug.allowed_symbols
        if allowed and symbol not in allowed:
            return {"ok": False, "tool": tool, "backend": self.backend_name, "error_type": "permission_denied", "summary": "Symbol is not allowed by debug.allowed_symbols.", "symbol": symbol}
        return {"ok": True}

    def _symbol_failure(self, tool: str, symbol: str, message: str | None, timed_out: bool) -> JsonObject:
        if timed_out:
            return {"ok": False, "tool": tool, "backend": self.backend_name, "error_type": "timeout", "summary": "Symbol resolution timed out.", "symbol": symbol}
        lower = (message or "").lower()
        if "no symbol" in lower or "not defined" in lower:
            error_type = "symbol_not_found"
        elif "ambiguous" in lower:
            error_type = "symbol_ambiguous"
        else:
            error_type = "symbol_resolution_failed"
        return {"ok": False, "tool": tool, "backend": self.backend_name, "error_type": error_type, "summary": message or "Symbol could not be resolved.", "symbol": symbol}

    def _read_memory_bytes(self, tool: str, session: GdbDebugSession, address: int, size_bytes: int) -> JsonObject:
        data = bytearray()
        offset = 0
        while offset < size_bytes:
            chunk_size = min(MEMORY_READ_CHUNK_BYTES, size_bytes - offset)
            response = self._gdb_command(session, f"-data-read-memory-bytes {hex(address + offset)} {chunk_size}")
            if not response.ok:
                error_type = "timeout" if response.timed_out else "memory_read_failed"
                return {"ok": False, "tool": tool, "backend": self.backend_name, "error_type": error_type, "summary": response.error_message or "Target memory could not be read.", "address": hex(address + offset)}
            contents = mi_field(response.line, "contents")
            if contents is None or MEMORY_CONTENTS_PATTERN.match(contents) is None:
                return {"ok": False, "tool": tool, "backend": self.backend_name, "error_type": "memory_read_failed", "summary": "GDB returned unparsable memory contents.", "address": hex(address + offset)}
            data.extend(bytes.fromhex(contents))
            offset += chunk_size
        if len(data) != size_bytes:
            return {"ok": False, "tool": tool, "backend": self.backend_name, "error_type": "memory_read_failed", "summary": "GDB returned fewer memory bytes than requested.", "bytes_requested": size_bytes, "bytes_read": len(data)}
        return {"ok": True, "data": bytes(data)}

    def _cleanup_session(self, session: GdbDebugSession, timeout_s: float) -> None:
        if session.gdb is not None:
            with suppress(Exception):
                session.gdb.close(timeout_s)
        if session.server.poll() is None:
            session.server.terminate()
            with suppress(subprocess.TimeoutExpired):
                session.server.wait(timeout=timeout_s)
            if session.server.poll() is None:
                session.server.kill()
                with suppress(subprocess.TimeoutExpired):
                    session.server.wait(timeout=timeout_s)
        self._write_session_log(session)

    def _start_failure(self, session: GdbDebugSession, tool: str, started_at: str, start: float, timed_out: bool) -> JsonObject:
        output = f"{session.server_stdout}{session.server_stderr}"
        if timed_out:
            error_type, backend_error_type = "timeout", "gdb_server_not_ready"
            summary = "Debug server did not open its GDB port before the timeout."
        else:
            backend_error_type = self._classify_server_output(output)
            error_type = backend_error_type if backend_error_type != "unknown_debugger_error" else "debugger_error"
            summary = "Debug server exited before the GDB port became ready."
        return {"ok": False, "tool": tool, "backend": self.backend_name, "started_at": started_at, "finished_at": utc_now_iso(), "elapsed_ms": int((time.perf_counter() - start) * 1000), "error_type": error_type, "backend_error_type": backend_error_type, "summary": summary, "log_path": display_path(self.config, session.log_path)}

    def _start_output_readers(self, session: GdbDebugSession) -> None:
        def read(stream, attribute: str) -> None:
            if stream is None:
                return
            for line in stream:
                setattr(session, attribute, (getattr(session, attribute) + line)[-OUTPUT_TAIL_CHARS:])

        threading.Thread(target=read, args=(session.server.stdout, "server_stdout"), daemon=True).start()
        threading.Thread(target=read, args=(session.server.stderr, "server_stderr"), daemon=True).start()

    def _session_status(self, session: GdbDebugSession) -> JsonObject:
        return {
            "session_id": session.session_id,
            "status": session.status,
            "mode": session.mode,
            "started_at": session.started_at,
            "artifact": public_artifact(session.artifact),
            "breakpoints": list(session.breakpoints),
            "stop_reason": session.stop_reason,
            "gdb_port": session.gdb_port,
        }

    def _write_session_log(self, session: GdbDebugSession) -> None:
        import json

        payload = {
            "session_id": session.session_id,
            "mode": session.mode,
            "status": session.status,
            "server_command": session.server_args,
            "server_stdout_tail": session.server_stdout,
            "server_stderr_tail": session.server_stderr,
            "gdb_commands": session.gdb.history() if session.gdb else [],
        }
        with suppress(OSError):
            Path(session.log_path).write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    def _report(self, result: JsonObject) -> JsonObject:
        return write_report(self.config, result)


def public_artifact(artifact: JsonObject) -> JsonObject:
    return {"source": artifact.get("source"), "path": artifact.get("path"), "sha256": artifact.get("sha256")}


def normalize_breakpoint_location(tool: str, location: JsonObject | str) -> JsonObject:
    if isinstance(location, str):
        return normalize_symbol_location(tool, location)
    if isinstance(location, dict):
        symbol = location.get("symbol", location.get("function"))
        if symbol is not None:
            return normalize_symbol_location(tool, symbol)
        file_name = location.get("file")
        line_number = location.get("line")
        if isinstance(file_name, str) and BREAKPOINT_FILE_PATTERN.match(file_name) and ".." not in file_name and isinstance(line_number, int) and not isinstance(line_number, bool) and line_number > 0:
            return {"ok": True, "location": {"file": file_name, "line": line_number}, "gdb_location": f"{file_name}:{line_number}"}
    return {"ok": False, "tool": tool, "error_type": "invalid_argument", "summary": "location must be a symbol name or {file, line} with a safe file path and a positive line."}


def normalize_symbol_location(tool: str, symbol: object) -> JsonObject:
    if isinstance(symbol, str) and DEBUG_SYMBOL_PATTERN.match(symbol) is not None:
        return {"ok": True, "location": {"symbol": symbol}, "gdb_location": symbol}
    return {"ok": False, "tool": tool, "error_type": "invalid_argument", "summary": "Breakpoint symbol must be a valid C/C++ identifier."}


def reserve_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def wait_for_tcp_port(port: int, timeout_s: float, server: subprocess.Popen[str]) -> bool:
    deadline = time.monotonic() + max(0.1, timeout_s)
    while time.monotonic() < deadline:
        if server.poll() is not None:
            return False
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as candidate:
            candidate.settimeout(TCP_CONNECT_TIMEOUT_S)
            if candidate.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(TCP_POLL_INTERVAL_S)
    return False
