from __future__ import annotations

import re
import socket
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path

from agentic_hil.config import ConfigError, display_path, safe_configured_directory
from agentic_hil.gdbmi import (
    GdbMiClient,
    GdbMiStopResult,
    mi_field,
    mi_string,
    parse_gdb_integer,
    write_intel_hex_file,
)
from agentic_hil.process import process_group_kwargs, spawn_managed_process, terminate_process_tree
from agentic_hil.report import (
    logs_directory,
    mark_audit_failure,
    mark_side_effect,
    timestamp_for_filename,
    utc_now_iso,
    write_audit_log,
    write_report,
)
from agentic_hil.types import AgenticHILConfig, JsonObject

DEBUG_MODES = ["attach", "reset_halt", "load"]
GDB_AUTODETECT_CANDIDATES = ["arm-none-eabi-gdb", "gdb-multiarch", "gdb"]
DEBUG_SYMBOL_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:::[A-Za-z_][A-Za-z0-9_]*)*$")
BREAKPOINT_FILE_PATTERN = re.compile(r"^[A-Za-z0-9_./\\:-]+$")
MEMORY_CONTENTS_PATTERN = re.compile(r"^(?:[0-9a-fA-F]{2})*$")
TARGET_EXCEPTION_MARKERS = [
    ("hardfault", "hardfault"),
    ("hard_fault", "hardfault"),
    ("memmanage", "memmanage"),
    ("busfault", "busfault"),
    ("usagefault", "usagefault"),
    ("default_handler", "default_handler"),
]
SIGNAL_EXCEPTION_NAMES = {"SIGABRT", "SIGBUS", "SIGFPE", "SIGILL", "SIGSEGV"}
ABNORMAL_STOP_REASONS = {"debugger_error", "exception", "fault", "timeout", "unexpected_breakpoint"}
TCP_POLL_INTERVAL_S = 0.05
TCP_CONNECT_TIMEOUT_S = 0.2
MEMORY_READ_CHUNK_BYTES = 1024
GDB_COMMAND_TIMEOUT_CAP_S = 10.0
CONTINUE_COMMAND_TIMEOUT_CAP_S = 5.0
STOP_SESSION_TIMEOUT_CAP_S = 5.0
CLOSE_SESSION_TIMEOUT_S = 1.0
INITIAL_STOP_POLL_TIMEOUT_S = 0.05
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
        self.server_readers: list[threading.Thread] = []
        self.load_phase = "not_started"
        self.firmware_load_status = "not_started"


class _AuditRefusedResponse:
    """Failed-command view returned when the permanent audit latch refuses a new
    MI command (executed=False) or invalidates one whose evidence write failed
    right after execution (executed=True)."""

    def __init__(self, error: Exception, original: object | None = None):
        self.executed = original is not None and bool(getattr(original, "ok", False))
        self.line = str(getattr(original, "line", "")) if original is not None else ""
        self.records = list(getattr(original, "records", [])) if original is not None else []
        self.result_class = "error"
        self.timed_out = False
        self.error_message = f"Debug audit write failed; further hardware commands are blocked: {error}"
        self.ok = False
        self.audit_failure = True


class GdbDebugSessions:
    """Typed GDB/MI debug sessions against a gdbserver-providing debugger process (e.g. OpenOCD)."""

    def __init__(
        self,
        config: AgenticHILConfig,
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
        # Permanent audit latch: once evidence persistence breaks, it stays
        # broken for this service instance; it is never consumed by reporting.
        self._audit_broken: Exception | None = None

    def start_session(self, artifact: JsonObject, mode: str = "attach", timeout_s: float | None = None) -> JsonObject:
        tool = "debug_start_session"
        if mode not in DEBUG_MODES:
            return self._report({"ok": False, "tool": tool, "backend": self.backend_name, "error_type": "invalid_argument", "summary": "Invalid debug session mode.", "allowed_values": DEBUG_MODES})
        if self.session is not None and self.session.status != "stopped":
            return self._report({"ok": False, "tool": tool, "backend": self.backend_name, "error_type": "session_already_active", "summary": "A debug session is already active. Stop it with debug_stop_session first.", "session": self._session_status(self.session)})
        permission = self._start_permission(tool, mode)
        if not permission["ok"]:
            return self._report(permission)
        if self._audit_broken is not None:
            return self._report({"ok": False, "tool": tool, "backend": self.backend_name, "error_type": "audit_broken", "summary": "Debug audit evidence is broken; resolve the recorded incident before starting new sessions.", "cleanup_required": True, "quarantined": True, "side_effect_committed": False, "side_effect_status": "not_started"})
        resolved_server = self._resolve_server()
        if not resolved_server.get("ok"):
            return self._report({"tool": tool, **resolved_server})
        resolved_gdb = self._resolve_gdb()
        if not resolved_gdb["ok"]:
            return self._report({"tool": tool, **resolved_gdb})

        timeout = self.config.debugger.timeout_s if timeout_s is None else min(self.config.debugger.timeout_s, max(0.1, timeout_s))
        started_at = utc_now_iso()
        start = time.perf_counter()
        gdb_port = reserve_tcp_port()
        server_args = self._build_server_args(str(resolved_server["executable_path"]), gdb_port, mode != "attach")
        log_path = str(Path(logs_directory(self.config)) / f"gdb-debug-{timestamp_for_filename()}.json")
        try:
            server = spawn_managed_process(
                server_args,
                cwd=str(Path(str(resolved_server["executable_path"])).parent),
                text=True,
                encoding="utf-8",
                errors="replace",
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                **process_group_kwargs(),
            )
        except OSError as error:
            return self._report({"ok": False, "tool": tool, "backend": self.backend_name, "error_type": "debugger_not_found", "summary": "Debug server process could not be started.", "backend_error": str(error)})

        session = GdbDebugSession(f"debug-{timestamp_for_filename()}", artifact, mode, gdb_port, server, server_args, log_path)
        session.load_phase = "server_spawned"
        self.session = session
        try:
            self._start_output_readers(session)
        except BaseException as error:
            cleanup_error = self._cleanup_session(session, STOP_SESSION_TIMEOUT_CAP_S)
            if cleanup_error is not None:
                session.status = "cleanup_required"
            else:
                self.session = None
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                if cleanup_error is not None:
                    error.args = (*error.args, f"Cleanup error: {cleanup_error}")
                raise
            result = {"ok": False, "tool": tool, "backend": self.backend_name, "error_type": "debug_session_setup_failed", "summary": "Debug server output readers could not be started.", "backend_error": str(error)}
            if cleanup_error is not None:
                result.update({"cleanup_required": True, "cleanup_error": cleanup_error})
            else:
                result.update({"cleanup_confirmed": True, **self._startup_effect_fields(session)})
                if result.get("cleanup_required") is True:
                    session.status = "cleanup_required"
                    self.session = session
            return self._report(result)

        if not wait_for_tcp_port(gdb_port, timeout, server):
            failure = self._start_failure(session, tool, started_at, start, timed_out=server.poll() is None)
            cleanup_error = self._cleanup_session(session, STOP_SESSION_TIMEOUT_CAP_S)
            if cleanup_error is not None:
                failure["cleanup_error"] = cleanup_error
                failure["cleanup_required"] = True
                session.status = "cleanup_required"
            else:
                failure.update({"cleanup_confirmed": True, **self._startup_effect_fields(session, timed_out=True)})
                if failure.get("cleanup_required") is True:
                    session.status = "cleanup_required"
                else:
                    self.session = None
            return self._report(failure)
        session.load_phase = "server_ready"

        try:
            session.gdb = GdbMiClient(str(resolved_gdb["executable"]), str(Path(str(resolved_gdb["executable"])).parent))
        except BaseException as error:
            cleanup_error = self._cleanup_session(session, STOP_SESSION_TIMEOUT_CAP_S)
            if cleanup_error is not None:
                session.status = "cleanup_required"
            else:
                self.session = None
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                if cleanup_error is not None:
                    error.args = (*error.args, f"Cleanup error: {cleanup_error}")
                raise
            result = {"ok": False, "tool": tool, "backend": self.backend_name, "error_type": "gdb_start_failed", "summary": "GDB/MI process could not be initialized.", "backend_error": str(error)}
            if cleanup_error is not None:
                result.update({"cleanup_required": True, "cleanup_error": cleanup_error})
            else:
                result.update({"cleanup_confirmed": True, **self._startup_effect_fields(session)})
                if result.get("cleanup_required") is True:
                    session.status = "cleanup_required"
                    self.session = session
            return self._report(result)
        initialized = self._initialize_gdb(session, timeout)
        if not initialized["ok"]:
            cleanup_error = self._cleanup_session(session, STOP_SESSION_TIMEOUT_CAP_S)
            result = {"tool": tool, "backend": self.backend_name, "started_at": started_at, **initialized, "log_path": display_path(self.config, log_path)}
            if cleanup_error is not None:
                result["cleanup_error"] = cleanup_error
                result["cleanup_required"] = True
                session.status = "cleanup_required"
            else:
                result["cleanup_confirmed"] = True
                if result.get("side_effect_status") not in {"unknown", "partial"}:
                    result.update({"side_effect_committed": False, "side_effect_status": "not_started", "retry_safe": True})
                    self.session = None
                else:
                    result["cleanup_required"] = True
                    session.status = "cleanup_required"
            return self._report(result)

        session.status = "halted"
        self._refresh_session_stop(session, INITIAL_STOP_POLL_TIMEOUT_S)
        self._write_session_log(session)
        result: JsonObject = {
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
        }
        result.update(target_stop_fields(session.stop_reason))
        return self._report(result)

    def stop_session(self, timeout_s: float | None = None) -> JsonObject:
        tool = "debug_stop_session"
        session = self.session
        if session is None or session.status == "stopped":
            return {"ok": True, "tool": tool, "backend": self.backend_name, "active": False, "status": "stopped", "summary": "No debug session is active."}
        timeout = min(self.config.debugger.timeout_s, STOP_SESSION_TIMEOUT_CAP_S)
        if timeout_s is not None:
            timeout = min(timeout, max(0.1, timeout_s))
        cleanup_error = self._cleanup_session(session, timeout)
        session.status = "cleanup_required" if cleanup_error is not None else "stopped"
        if cleanup_error is not None:
            return self._report({"ok": False, "tool": tool, "backend": self.backend_name, "active": True, "status": "cleanup_required", "hardware_state": "unknown", "cleanup_required": True, "error_type": "cleanup_failed", "cleanup_error": cleanup_error, "session": self._session_status(session), "log_path": display_path(self.config, session.log_path), "summary": "Debug session cleanup failed; ownership is retained for retry."})
        self.session = None
        return self._report({"ok": True, "tool": tool, "backend": self.backend_name, "active": False, "status": "stopped", "session": self._session_status(session), "log_path": display_path(self.config, session.log_path), "summary": "Debug session stopped."})

    def get_session_status(self) -> JsonObject:
        session = self.session
        if session is not None and session.status not in {"stopped", "error", "cleanup_required"} and session.gdb is not None:
            self._refresh_session_stop(session)
        active = session is not None and session.status != "stopped"
        result: JsonObject = {"ok": True, "tool": "debug_get_session_status", "backend": self.backend_name, "active": active, "status": session.status if session else "stopped", "session": self._session_status(session) if session else None}
        if session is not None and session.status == "cleanup_required":
            result.update({"cleanup_required": True, "hardware_state": "unknown", "quarantined": True})
        result.update(target_stop_fields(session.stop_reason if session else None))
        return self._status_report(result)

    def set_breakpoint(self, location: JsonObject | str) -> JsonObject:
        tool = "debug_set_breakpoint"
        session_result = self._require_session(tool)
        if not session_result["ok"]:
            return self._report(session_result)
        session = session_result["session"]
        normalized = normalize_breakpoint_location(tool, location)
        if not normalized["ok"]:
            return self._report(normalized)
        normalized_location = normalized["location"]
        if "symbol" in normalized_location:
            authorized = self._validate_symbol(tool, str(normalized_location["symbol"]))
            if not authorized["ok"]:
                return self._report(authorized)
        elif not self.config.debug.allow_all_symbols:
            return self._report({
                "ok": False,
                "tool": tool,
                "backend": self.backend_name,
                "error_type": "permission_denied",
                "summary": "File and line breakpoints require debug.allow_all_symbols.",
            })
        response = self._gdb_command(session, f"-break-insert {mi_string(normalized['gdb_location'])}")
        backend_id = mi_field(response.line, "number")
        valid_backend_id = isinstance(backend_id, str) and backend_id.isdigit()
        if response.ok and valid_backend_id:
            breakpoint = {"id": session.next_breakpoint_id, "backend_id": backend_id, "location": normalized["location"], "gdb_location": normalized["gdb_location"]}
            session.next_breakpoint_id += 1
            session.breakpoints.append(breakpoint)
            self._write_session_log(session)
            if self._audit_broken is not None:
                return self._report({"ok": False, "tool": tool, "backend": self.backend_name, "error_type": "audit_broken", "summary": "Breakpoint was set but its audit evidence could not be persisted.", "cleanup_required": True, "quarantined": True, "side_effect_committed": True, "side_effect_status": "unknown", "breakpoint": breakpoint, "session": self._session_status(session), "log_path": display_path(self.config, session.log_path)})
            return self._report({"ok": True, "tool": tool, "backend": self.backend_name, "breakpoint": breakpoint, "session": self._session_status(session), "log_path": display_path(self.config, session.log_path), "summary": "Breakpoint set."})
        # Lost ACK, unparsable backend ID, or post-execution audit break: the
        # breakpoint may exist on the target, so it is tracked provisionally and
        # only backend reconciliation may later prove it gone.
        effect_unconfirmed = response.timed_out or bool(getattr(response, "executed", False)) or (response.ok and not valid_backend_id)
        if effect_unconfirmed:
            provisional = {"id": session.next_breakpoint_id, "backend_id": backend_id if valid_backend_id else None, "location": normalized["location"], "gdb_location": normalized["gdb_location"], "provisional": True}
            session.next_breakpoint_id += 1
            session.breakpoints.append(provisional)
            self._write_session_log(session)
            failure = {**self._gdb_failure(tool, session, response.error_message or "Breakpoint insert was not confirmed by the backend.", response.timed_out, response=response), "side_effect_status": "unknown", "cleanup_required": True, "provisional_breakpoint": provisional}
            return self._report(failure)
        return self._report(self._gdb_failure(tool, session, response.error_message, response.timed_out, response=response))

    def list_breakpoints(self) -> JsonObject:
        session = self.session
        active = session is not None and session.status != "stopped"
        return self._status_report({"ok": True, "tool": "debug_list_breakpoints", "backend": self.backend_name, "active": active, "breakpoints": list(session.breakpoints) if session else []})

    def clear_breakpoints(self) -> JsonObject:
        tool = "debug_clear_breakpoints"
        session_result = self._require_session(tool)
        if not session_result["ok"]:
            return self._report(session_result)
        session = session_result["session"]
        # Preserve the target's stop reason: a tolerated "No breakpoint number N"
        # delete makes _gdb_command clobber stop_reason to debugger_error, which
        # would spuriously short-circuit the next debug_continue. Clearing
        # breakpoints does not change target execution state.
        prior_stop_reason = session.stop_reason
        # Reconcile from the backend's authoritative list FIRST and delete only
        # numbers GDB actually reports. Driving deletes from the local list would
        # re-issue `-break-delete N` for an id GDB already removed after a lost
        # ACK, and real GDB answers "No breakpoint number N", wedging every retry.
        remaining = self._backend_breakpoint_numbers(session)
        if remaining is None:
            return self._report({"ok": False, "tool": tool, "backend": self.backend_name, "error_type": "breakpoint_reconciliation_failed", "summary": "Backend breakpoint list could not be read; cleanup remains unconfirmed.", "cleanup_required": True, "side_effect_status": "unknown", "session": self._session_status(session), "log_path": display_path(self.config, session.log_path)})
        cleared = 0
        for number in remaining:
            response = self._gdb_command(session, f"-break-delete {number}")
            if not response.ok and not _is_missing_breakpoint_error(response):
                return self._report({**self._gdb_failure(tool, session, response.error_message, response.timed_out, response=response), "side_effect_status": "unknown", "cleanup_required": True})
            cleared += 1
        confirm = self._backend_breakpoint_numbers(session)
        if confirm is None or confirm:
            return self._report({"ok": False, "tool": tool, "backend": self.backend_name, "error_type": "breakpoint_reconciliation_failed", "summary": "Backend still reports breakpoints after cleanup.", "remaining_backend_breakpoints": confirm, "cleanup_required": True, "side_effect_status": "unknown", "session": self._session_status(session), "log_path": display_path(self.config, session.log_path)})
        # The backend is authoritatively empty; only now is the local list safe
        # to drop, including provisional entries whose ACK was lost.
        session.breakpoints.clear()
        # Restore the pre-clear stop reason if a tolerated missing-breakpoint
        # delete poisoned it to a spurious debugger_error.
        if session.stop_reason is not None and str(session.stop_reason.get("stop_reason")) == "debugger_error":
            session.stop_reason = prior_stop_reason
        self._write_session_log(session)
        result: JsonObject = {"ok": True, "tool": tool, "backend": self.backend_name, "cleared": cleared, "backend_reconciled": True, "session": self._session_status(session), "log_path": display_path(self.config, session.log_path), "summary": "All breakpoints cleared and reconciled with the backend."}
        if self._audit_broken is not None:
            result.update({"ok": False, "error_type": "audit_broken", "cleanup_required": True, "quarantined": True})
        return self._report(result)

    def _backend_breakpoint_numbers(self, session: GdbDebugSession) -> list[str] | None:
        response = self._gdb_command(session, "-break-list")
        if not response.ok:
            return None
        # Only top-level breakpoint numbers; multi-location rows use N.M ids that
        # the plain-integer capture deliberately skips.
        return re.findall(r'number="(\d+)"', response.line)

    def continue_execution(self, timeout_s: float | None = None) -> JsonObject:
        tool = "debug_continue"
        session_result = self._require_session(tool)
        if not session_result["ok"]:
            return self._report(session_result)
        session = session_result["session"]
        self._refresh_session_stop(session)
        if session.stop_reason is not None and str(session.stop_reason.get("stop_reason")) in {"debugger_error", "exception", "fault"}:
            return self._report(self._stopped_result(tool, session, "Target is already stopped"))
        timeout = self.config.debugger.timeout_s if timeout_s is None else min(self.config.debugger.timeout_s, max(0.1, timeout_s))
        session.status = "running"
        session.stop_reason = None
        response = self._gdb_command(session, "-exec-continue", min(timeout, CONTINUE_COMMAND_TIMEOUT_CAP_S))
        # A post-execution audit break (executed=True) means the target is now
        # running: fall through to the wait/interrupt containment below so it is
        # not abandoned free-running. The broken audit is still reported by
        # `_report`, and the lease quarantines on audit_ok=False.
        audit_refused_running = bool(getattr(response, "audit_failure", False) and getattr(response, "executed", False))
        if response.result_class not in {"running", "done"} and not audit_refused_running:
            session.status = "error"
            return self._report(self._gdb_failure(tool, session, response.error_message, response.timed_out, response=response))
        assert session.gdb is not None
        stop = session.gdb.wait_for_stop(timeout)
        if stop.timed_out:
            interrupt = self._gdb_command(session, "-exec-interrupt --all", min(CONTINUE_COMMAND_TIMEOUT_CAP_S, self.config.debugger.timeout_s), containment=True)
            confirmed = session.gdb.wait_for_stop(min(CONTINUE_COMMAND_TIMEOUT_CAP_S, self.config.debugger.timeout_s)) if interrupt.ok else GdbMiStopResult(line="", reason="timeout", timed_out=True)
            halt_confirmed = not confirmed.timed_out and confirmed.reason != "debugger_error"
            if halt_confirmed:
                session.stop_reason = self._stop_reason_from_gdb(session, confirmed)
                session.status = "halted"
            else:
                session.status = "running"
                session.stop_reason = {"stop_reason": "timeout", "backend_stop_reason": "timeout", "halt_confirmed": False}
            self._write_session_log(session)
            result: JsonObject = {"ok": False, "tool": tool, "backend": self.backend_name, "error_type": "timeout", "summary": "Target did not stop before the timeout.", "stop_reason": "timeout", "stop": session.stop_reason, "session": self._session_status(session), "log_path": display_path(self.config, session.log_path), "halt_requested": True, "halt_command_acknowledged": interrupt.ok, "halt_confirmed": halt_confirmed, "target_state": "halted" if halt_confirmed else "unknown", "side_effect_committed": halt_confirmed, "side_effect_status": "committed" if halt_confirmed else "unknown"}
            result.update(target_stop_fields(session.stop_reason))
            return self._report(result)
        session.stop_reason = self._stop_reason_from_gdb(session, stop)
        stop_reason = str(session.stop_reason.get("stop_reason"))
        session.status = "error" if stop_reason == "debugger_error" else "halted"
        result = self._stopped_result(tool, session, "Target stopped")
        self._write_session_log(session)
        return self._report(result)

    def halt(self, timeout_s: float | None = None) -> JsonObject:
        tool = "debug_halt"
        session_result = self._require_session(tool)
        if not session_result["ok"]:
            return self._report(session_result)
        session = session_result["session"]
        if self._refresh_session_stop(session) is not None:
            return self._report(self._stopped_result(tool, session, "Target was already stopped"))
        timeout = min(self.config.debugger.timeout_s, GDB_COMMAND_TIMEOUT_CAP_S)
        if timeout_s is not None:
            timeout = min(timeout, max(0.1, timeout_s))
        response = self._gdb_command(session, "-exec-interrupt --all", timeout, containment=True)
        if not response.ok:
            return self._report(self._gdb_failure(tool, session, response.error_message, response.timed_out, response=response))
        assert session.gdb is not None
        stop = session.gdb.wait_for_stop(timeout)
        session.stop_reason = self._stop_reason_from_gdb(session, stop)
        if stop.timed_out or stop.reason == "debugger_error":
            session.status = "running" if stop.timed_out else "error"
            self._write_session_log(session)
            result: JsonObject = {"ok": False, "tool": tool, "backend": self.backend_name, "error_type": "timeout" if stop.timed_out else "debugger_error", "summary": "Target halt was requested but not confirmed.", "stop_reason": session.stop_reason.get("stop_reason"), "stop": session.stop_reason, "session": self._session_status(session), "log_path": display_path(self.config, session.log_path), "halt_requested": True, "halt_command_acknowledged": True, "halt_confirmed": False, "target_state": "unknown", "side_effect_status": "unknown"}
            result.update(target_stop_fields(session.stop_reason))
            return self._report(result)
        session.status = "halted"
        self._write_session_log(session)
        return self._report(self._stopped_result(tool, session, "Target halted"))

    def get_stop_reason(self) -> JsonObject:
        tool = "debug_get_stop_reason"
        session_result = self._require_session(tool)
        if not session_result["ok"]:
            return self._report(session_result)
        session = session_result["session"]
        self._refresh_session_stop(session)
        if session.stop_reason is None:
            return self._status_report({"ok": False, "tool": tool, "backend": self.backend_name, "error_type": "stop_reason_not_available", "summary": "No stop reason has been recorded yet. Run debug_continue or debug_halt first."})
        result = {"ok": True, "tool": tool, "backend": self.backend_name, "stop_reason": session.stop_reason.get("stop_reason"), "stop": session.stop_reason, "session": self._session_status(session)}
        result.update(target_stop_fields(session.stop_reason))
        return self._status_report(result)

    def symbol_info(self, symbol: str) -> JsonObject:
        tool = "debug_symbol_info"
        session_result = self._require_session(tool)
        if not session_result["ok"]:
            return self._report(session_result)
        session = session_result["session"]
        resolved = self._resolve_symbol(tool, session, symbol)
        if not resolved["ok"]:
            return self._report(resolved)
        return self._report({**resolved, "tool": tool, "backend": self.backend_name, "session": self._session_status(session), "log_path": display_path(self.config, session.log_path), "summary": "Symbol resolved."})

    def dump_symbol_ihex(self, symbol: str, output: JsonObject) -> JsonObject:
        tool = "debug_dump_symbol_ihex"
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
            safe_configured_directory(self.config, str(Path(str(output["resolved_path"])).parent), f"{tool}.output_path")
            write_intel_hex_file(Path(str(output["resolved_path"])), int(resolved["address_value"]), memory["data"])
        except (ConfigError, OSError) as error:
            return self._report({"ok": False, "tool": tool, "backend": self.backend_name, "error_type": "output_write_failed", "summary": "Intel HEX output file could not be written.", "backend_error": str(error)})
        self._write_session_log(session)
        return self._report({"ok": True, "tool": tool, "backend": self.backend_name, "symbol": symbol, "address": resolved["address"], "size_bytes": size_bytes, "output": output, "session": self._session_status(session), "log_path": display_path(self.config, session.log_path), "summary": "Symbol memory dumped as Intel HEX."})

    def close(self) -> None:
        session = self.session
        if session is not None and session.status != "stopped":
            cleanup_error = self._cleanup_session(session, CLOSE_SESSION_TIMEOUT_S)
            if cleanup_error is not None:
                session.status = "cleanup_required"
                raise RuntimeError(f"Debug session cleanup failed: {cleanup_error}")
            session.status = "stopped"
        self.session = None

    def _start_permission(self, tool: str, mode: str) -> JsonObject:
        permissions = self.config.permissions
        if not permissions.allow_probe:
            return self._permission_denied(tool, "Debug sessions require allow_probe in the authoritative config.")
        if mode != "attach" and not permissions.allow_reset:
            return self._permission_denied(tool, f"Debug session mode '{mode}' requires allow_reset in the authoritative config.")
        if permissions.allow_raw_debugger_commands:
            return self._permission_denied(tool, "Debug sessions are disabled while raw debugger commands are allowed.")
        if mode == "load":
            if not permissions.allow_flash:
                return self._permission_denied(tool, "Debug session mode 'load' requires allow_flash in the authoritative config.")
            if permissions.allow_mass_erase:
                return self._permission_denied(tool, "Debug session mode 'load' is disabled while mass erase is allowed.")
        return {"ok": True}

    def _permission_denied(self, tool: str, summary: str) -> JsonObject:
        return {"ok": False, "tool": tool, "backend": self.backend_name, "error_type": "permission_denied", "summary": summary}

    def _resolve_gdb(self) -> JsonObject:
        from agentic_hil.backends.common import which

        configured = self.config.debug.gdb_executable
        if configured:
            has_path_separator = "/" in configured or "\\" in configured
            if Path(configured).is_absolute() or has_path_separator:
                from agentic_hil.config import resolve_work_path

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
        return {"ok": False, "backend": self.backend_name, "error_type": "gdb_not_found", "summary": "No GDB executable could be found.", "likely_causes": ["install arm-none-eabi-gdb or gdb-multiarch", "set debug.gdb_executable in the authoritative project config"]}

    def _initialize_gdb(self, session: GdbDebugSession, timeout: float) -> JsonObject:
        commands = ["-gdb-set pagination off", "-gdb-set confirm off", f"-file-exec-and-symbols {mi_string(str(session.artifact['resolved_path']))}"]
        for command in commands:
            response = self._gdb_command(session, command, min(timeout, GDB_COMMAND_TIMEOUT_CAP_S))
            if not response.ok:
                return {**self._gdb_failure("debug_start_session", session, response.error_message or f"GDB startup command failed: {command}", response.timed_out, response=response), **self._startup_effect_fields(session, response.timed_out)}
        session.load_phase = "target_connect_started"
        target = self._gdb_command(session, f"-target-select extended-remote localhost:{session.gdb_port}", min(timeout, GDB_COMMAND_TIMEOUT_CAP_S))
        if not target.ok:
            return {**self._gdb_failure("debug_start_session", session, target.error_message, target.timed_out, response=target), **self._startup_effect_fields(session, target.timed_out)}
        session.load_phase = "target_connected"
        if session.mode != "attach":
            session.load_phase = "pre_load_reset_started"
            reset = self._gdb_command(session, '-interpreter-exec console "monitor reset halt"', min(timeout, GDB_COMMAND_TIMEOUT_CAP_S))
            if not reset.ok:
                return {**self._gdb_failure("debug_start_session", session, reset.error_message, reset.timed_out, response=reset), **self._startup_effect_fields(session, reset.timed_out)}
            session.load_phase = "pre_load_reset_confirmed"
        if session.mode == "load":
            session.load_phase = "download_started"
            session.firmware_load_status = "partial_or_unknown"
            download = self._gdb_command(session, "-target-download", min(timeout, GDB_COMMAND_TIMEOUT_CAP_S))
            if not download.ok:
                return {**self._gdb_failure("debug_start_session", session, download.error_message, download.timed_out, response=download), **self._startup_effect_fields(session, download.timed_out)}
            session.load_phase = "download_confirmed"
            session.firmware_load_status = "committed"
            session.load_phase = "post_load_reset_started"
            reset = self._gdb_command(session, '-interpreter-exec console "monitor reset halt"', min(timeout, GDB_COMMAND_TIMEOUT_CAP_S))
            if not reset.ok:
                return {**self._gdb_failure("debug_start_session", session, reset.error_message, reset.timed_out, response=reset), **self._startup_effect_fields(session, reset.timed_out)}
            session.load_phase = "post_load_reset_confirmed"
        return {"ok": True, "load_phase": session.load_phase, "firmware_load_status": session.firmware_load_status}

    def _startup_effect_fields(self, session: GdbDebugSession, timed_out: bool = False) -> JsonObject:
        fields: JsonObject = {"load_phase": session.load_phase, "firmware_load_status": session.firmware_load_status}
        if session.mode == "attach" and session.load_phase in {"not_started", "server_spawned", "server_ready"}:
            return {**fields, "side_effect_committed": False, "side_effect_status": "not_started", "retry_safe": True}
        if session.load_phase in {"download_started", "download_confirmed", "post_load_reset_started", "post_load_reset_confirmed"}:
            status = "unknown" if timed_out else "partial"
            return {**fields, "side_effect_committed": True, "side_effect_status": status, "retry_safe": False, "target_state": "unknown", "hardware_state": "unknown", "cleanup_required": True, "command_timed_out": timed_out}
        return {**fields, "side_effect_status": "unknown", "retry_safe": False, "target_state": "unknown", "hardware_state": "unknown", "cleanup_required": True, "command_timed_out": timed_out}

    def _require_session(self, tool: str) -> JsonObject:
        session = self.session
        if session is None or session.status in {"stopped", "error", "cleanup_required"} or session.gdb is None or not session.gdb.is_running():
            if session is not None and session.status == "cleanup_required":
                return {"ok": False, "tool": tool, "backend": self.backend_name, "error_type": "resource_quarantined", "summary": "Debug session requires cleanup before further effects.", "cleanup_required": True, "quarantined": True, "hardware_state": "unknown"}
            return {"ok": False, "tool": tool, "backend": self.backend_name, "error_type": "session_not_active", "summary": "No debug session is active. Start one with debug_start_session first."}
        return {"ok": True, "session": session}

    def _gdb_command(self, session: GdbDebugSession, command: str, timeout_s: float | None = None, *, containment: bool = False):
        # Non-containment commands are refused before reaching the target once
        # the audit latch is set; containment (halt/interrupt) stays available
        # but never lifts the latch.
        if self._audit_broken is not None and not containment:
            return _AuditRefusedResponse(self._audit_broken)
        assert session.gdb is not None
        timeout = min(self.config.debugger.timeout_s, GDB_COMMAND_TIMEOUT_CAP_S) if timeout_s is None else timeout_s
        response = session.gdb.command(command, timeout)
        self._write_session_log(session)
        if not response.ok:
            session.stop_reason = {"stop_reason": "debugger_error", "backend_stop_reason": "timeout" if response.timed_out else "error", "backend_error": response.error_message}
            return response
        if self._audit_broken is not None and not containment:
            # The command executed but its evidence write failed: surface the
            # audit break synchronously so no multi-command path continues.
            return _AuditRefusedResponse(self._audit_broken, response)
        return response

    def _gdb_failure(self, tool: str, session: GdbDebugSession, message: str | None, timed_out: bool, response: object | None = None) -> JsonObject:
        if response is not None and getattr(response, "audit_failure", False):
            executed = bool(getattr(response, "executed", False))
            return {
                "ok": False,
                "tool": tool,
                "backend": self.backend_name,
                "error_type": "audit_broken",
                "backend_error_type": "audit_write_failed",
                "summary": message or "Debug audit write failed; further hardware commands are blocked.",
                "cleanup_required": True,
                "quarantined": True,
                "side_effect_committed": executed,
                "side_effect_status": "unknown" if executed else "not_started",
                "retry_safe": False,
                "session": self._session_status(session),
                "log_path": display_path(self.config, session.log_path),
            }
        error_type = "timeout" if timed_out else "debugger_error"
        backend_error_type = "gdb_timeout" if timed_out else "gdb_error"
        return {"ok": False, "tool": tool, "backend": self.backend_name, "error_type": error_type, "backend_error_type": backend_error_type, "summary": message or "GDB/MI command failed.", "session": self._session_status(session), "log_path": display_path(self.config, session.log_path)}

    def _refresh_session_stop(self, session: GdbDebugSession, wait_timeout_s: float = 0.0) -> JsonObject | None:
        assert session.gdb is not None
        stop = session.gdb.poll_stop()
        if stop is None and wait_timeout_s > 0:
            candidate = session.gdb.wait_for_stop(wait_timeout_s)
            stop = None if candidate.timed_out else candidate
        if stop is None:
            return None
        session.stop_reason = self._stop_reason_from_gdb(session, stop)
        stop_reason = str(session.stop_reason.get("stop_reason"))
        session.status = "error" if stop_reason == "debugger_error" else "halted"
        self._write_session_log(session)
        return session.stop_reason

    def _stopped_result(self, tool: str, session: GdbDebugSession, summary_prefix: str) -> JsonObject:
        stop = session.stop_reason or {"stop_reason": "unknown", "backend_stop_reason": "unknown"}
        stop_reason = str(stop.get("stop_reason"))
        ok = stop_reason not in ABNORMAL_STOP_REASONS
        result: JsonObject = {"ok": ok, "tool": tool, "backend": self.backend_name, "stop_reason": stop_reason, "stop": stop, "session": self._session_status(session), "log_path": display_path(self.config, session.log_path), "summary": f"{summary_prefix}: {stop_reason}."}
        result.update({"side_effect_committed": True, "side_effect_status": "committed", **target_stop_fields(stop)})
        if not ok:
            result["error_type"] = stop_error_type(stop_reason)
            result["suggested_actions"] = suggested_actions_for_stop(stop_reason)
        return result

    def _stop_reason_from_gdb(self, session: GdbDebugSession, stop: GdbMiStopResult) -> JsonObject:
        if stop.timed_out:
            return {"stop_reason": "timeout", "backend_stop_reason": "timeout"}
        if stop.error_message:
            return {"stop_reason": "debugger_error", "backend_stop_reason": stop.reason, "backend_error": stop.error_message}
        lower = stop.line.lower()
        backend_breakpoint_id = mi_field(stop.line, "bkptno")
        matching = next((item for item in session.breakpoints if item.get("backend_id") == backend_breakpoint_id), None) if backend_breakpoint_id is not None else None
        exception_type = exception_type_from_stop_line(lower)
        signal_name = mi_field(stop.line, "signal-name")
        signal_meaning = mi_field(stop.line, "signal-meaning")
        if stop.reason == "breakpoint-hit":
            stop_reason = "breakpoint_hit" if matching is not None else "unexpected_breakpoint"
        elif stop.reason in {"exited-normally", "exited"}:
            stop_reason = "target_exit"
        elif exception_type is not None:
            stop_reason = "exception"
        elif "reset_handler" in lower or "reset" in lower:
            stop_reason = "reset"
        elif stop.reason == "signal-received":
            if signal_name == "SIGTRAP":
                stop_reason = "unexpected_breakpoint"
            elif signal_name in SIGNAL_EXCEPTION_NAMES:
                stop_reason = "exception"
                exception_type = exception_type or signal_name.lower()
            elif signal_name == "SIGINT":
                stop_reason = "halted"
            else:
                stop_reason = "signal"
        elif stop.reason == "debugger-error":
            stop_reason = "debugger_error"
        else:
            stop_reason = "unknown"
        result: JsonObject = {"stop_reason": stop_reason, "backend_stop_reason": stop.reason}
        if exception_type is not None:
            result["exception_type"] = exception_type
            result["fault_type"] = exception_type
        if signal_name is not None or signal_meaning is not None:
            signal: JsonObject = {}
            if signal_name is not None:
                signal["name"] = signal_name
            if signal_meaning is not None:
                signal["meaning"] = signal_meaning
            result["signal"] = signal
        if backend_breakpoint_id is not None:
            result["backend_breakpoint_id"] = backend_breakpoint_id
            if matching is not None:
                result["breakpoint_expected"] = True
                result["breakpoint_id"] = matching["id"]
                result["breakpoint"] = matching
            else:
                result["breakpoint_expected"] = False
        elif stop_reason == "unexpected_breakpoint":
            result["breakpoint_expected"] = False
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
            return {"ok": False, "tool": tool, "backend": self.backend_name, "error_type": "symbol_resolution_failed", "summary": "GDB returned an unparsable symbol address or size.", "symbol": symbol, "side_effect_committed": False}
        return {"ok": True, "symbol": symbol, "address": hex(address_value), "address_value": address_value, "size_bytes": size_value}

    def _validate_symbol(self, tool: str, symbol: str) -> JsonObject:
        if not isinstance(symbol, str) or DEBUG_SYMBOL_PATTERN.match(symbol) is None:
            return {"ok": False, "tool": tool, "backend": self.backend_name, "error_type": "invalid_argument", "summary": "symbol must be a valid C/C++ identifier."}
        allowed = self.config.debug.allowed_symbols
        if not self.config.debug.allow_all_symbols and symbol not in allowed:
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
        return {"ok": False, "tool": tool, "backend": self.backend_name, "error_type": error_type, "summary": message or "Symbol could not be resolved.", "symbol": symbol, "side_effect_committed": False}

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

    def _cleanup_session(self, session: GdbDebugSession, timeout_s: float) -> str | None:
        errors: list[tuple[str, BaseException]] = []
        interrupt: BaseException | None = None
        if session.gdb is not None:
            try:
                session.gdb.close(timeout_s)
            except BaseException as error:
                errors.append(("gdb", error))
                if isinstance(error, (KeyboardInterrupt, SystemExit)):
                    interrupt = error
        try:
            terminate_process_tree(session.server, timeout_s)
            if session.server.poll() is None:
                raise RuntimeError("Debug server remained active after kill.")
            readers = getattr(session, "server_readers", [])
            for reader in readers:
                if reader.ident is not None:
                    reader.join(timeout=timeout_s)
            if any(reader.is_alive() for reader in readers):
                raise RuntimeError("Debug server output readers remained active after process cleanup.")
        except BaseException as error:
            errors.append(("debug_server", error))
            if interrupt is None and isinstance(error, (KeyboardInterrupt, SystemExit)):
                interrupt = error
        self._write_session_log(session)
        if self._audit_broken is not None:
            errors.append(("audit", self._audit_broken))
        if interrupt is not None:
            interrupt.args = (*interrupt.args, "Cleanup errors: " + "; ".join(f"{name}: {type(error).__name__}: {error}" for name, error in errors))
            raise interrupt
        if errors:
            return "; ".join(f"{name}: {type(error).__name__}: {error}" for name, error in errors)
        return None

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

        session.server_readers = [
            threading.Thread(target=read, args=(session.server.stdout, "server_stdout"), daemon=True),
            threading.Thread(target=read, args=(session.server.stderr, "server_stderr"), daemon=True),
        ]
        for reader in session.server_readers:
            reader.start()

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
            "load_phase": session.load_phase,
            "firmware_load_status": session.firmware_load_status,
        }

    def _write_session_log(self, session: GdbDebugSession) -> None:
        import json

        payload = {
            "session_id": session.session_id,
            "mode": session.mode,
            "status": session.status,
            "stop_reason": session.stop_reason,
            "server_command": session.server_args,
            "server_stdout_tail": session.server_stdout,
            "server_stderr_tail": session.server_stderr,
            "gdb_commands": session.gdb.history() if session.gdb else [],
            "breakpoints": list(session.breakpoints),
            "load_phase": session.load_phase,
            "firmware_load_status": session.firmware_load_status,
        }
        error = write_audit_log(self.config, session.log_path, json.dumps(payload, indent=2) + "\n")
        if error is not None:
            self._audit_broken = error

    def _report(self, result: JsonObject) -> JsonObject:
        result = mark_side_effect(result)
        if self._audit_broken is not None:
            # The latch is deliberately not cleared: every later result keeps
            # carrying the audit break until the incident is recovered.
            result = mark_audit_failure(result, self._audit_broken)
        return write_report(self.config, result)

    def _status_report(self, result: JsonObject) -> JsonObject:
        """Read-only status paths share the audit gate: with a broken latch they
        must fail closed and persist the evidence instead of returning ok."""
        if self._audit_broken is None:
            return result
        return self._report({**result, "ok": False, "error_type": "audit_broken", "cleanup_required": True, "quarantined": True})


def _is_missing_breakpoint_error(response: object) -> bool:
    """A `-break-delete N` that fails because GDB already removed N (e.g. after a
    lost ACK) is proof of deletion, not a cleanup failure."""
    if getattr(response, "audit_failure", False) or getattr(response, "timed_out", False):
        return False
    message = (getattr(response, "error_message", "") or "").lower()
    return "no breakpoint number" in message


def public_artifact(artifact: JsonObject) -> JsonObject:
    return {"source": artifact.get("source"), "path": artifact.get("path"), "sha256": artifact.get("sha256")}


def exception_type_from_stop_line(lower_line: str) -> str | None:
    for marker, exception_type in TARGET_EXCEPTION_MARKERS:
        if marker in lower_line:
            return exception_type
    return None


def stop_error_type(stop_reason: str) -> str:
    if stop_reason in {"exception", "fault"}:
        return "target_exception"
    return stop_reason


def suggested_actions_for_stop(stop_reason: str) -> list[str]:
    if stop_reason == "unexpected_breakpoint":
        return [
            "Target is halted; do not continue blindly.",
            "Inspect the returned frame and stop reason first.",
            "If this was a stale breakpoint, run debug_clear_breakpoints and set only the expected breakpoints again.",
            "If this was a firmware BKPT/assert, collect logs or memory evidence, then reset or restart the debug session.",
        ]
    if stop_reason in {"exception", "fault"}:
        return [
            "Target is halted in an exception/fault context; do not continue blindly.",
            "Inspect the returned frame, exception_type, signal, and available firmware logs.",
            "Collect required memory or symbol evidence before resetting the target.",
            "After diagnosis, reset or restart the debug session before rerunning the test.",
        ]
    if stop_reason == "debugger_error":
        return ["Check log_path and classify_last_error before retrying the debug action."]
    return []


def target_stop_fields(stop: JsonObject | None) -> JsonObject:
    if stop is None:
        return {}
    stop_reason = str(stop.get("stop_reason"))
    fields: JsonObject = {"target_ok": stop_reason not in ABNORMAL_STOP_REASONS, "target_stop_reason": stop_reason, "stop": stop}
    if stop_reason in ABNORMAL_STOP_REASONS:
        fields["target_error_type"] = stop_error_type(stop_reason)
        fields["suggested_actions"] = suggested_actions_for_stop(stop_reason)
    return fields


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
