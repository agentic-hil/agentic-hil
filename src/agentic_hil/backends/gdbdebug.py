from __future__ import annotations

import re
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path

from agentic_hil.artifacts import sha256_file
from agentic_hil.backends.common import (
    NOT_CONTACTED,
    contains_any,
    invocation,
    spawn_command,
    which,
)
from agentic_hil.config import (
    GDB_AUTODETECT_CANDIDATES,
    ConfigError,
    display_path,
    executable_is_disabled,
    safe_configured_directory,
)
from agentic_hil.elfsymbols import read_elf_byte_order, read_elf_symbol
from agentic_hil.gdbmi import (
    GdbMiClient,
    GdbMiStopResult,
    mi_field,
    mi_string,
    parse_gdb_integer,
    write_intel_hex_file,
)
from agentic_hil.knowledge import GDB_NOT_CONFIGURED_SCOPE, exclusive_permission_summary, remediation_fields
from agentic_hil.process import spawn_managed_process, terminate_process_tree
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
# The widths a run of bytes has one integer reading at, and the order it is read
# in where the image names none. See decode_symbol_value and image_byte_order
# for why both are stated rather than assumed.
INTEGER_VALUE_WIDTHS = frozenset({1, 2, 4, 8})
DEFAULT_SYMBOL_VALUE_BYTE_ORDER = "little"
GDB_COMMAND_TIMEOUT_CAP_S = 10.0
CONTINUE_COMMAND_TIMEOUT_CAP_S = 5.0
STOP_SESSION_TIMEOUT_CAP_S = 5.0
CLOSE_SESSION_TIMEOUT_S = 1.0
INITIAL_STOP_POLL_TIMEOUT_S = 0.05
OUTPUT_TAIL_CHARS = 65536
# Keyed by (halt unconfirmed, detach-guard unconfirmed). A retry that could not
# gather new evidence reports both as unconfirmed even when only one was the
# original cause, so the phrasing has to make sense for that combination too,
# not just for whichever single proof failed on the attempt that discovered it.
_UNCONFIRMED_TEARDOWN_PROOFS = {
    (True, False): "the target's halt could not be reconfirmed",
    (False, True): "the backend's auto-resume-on-detach override could not be confirmed installed",
    (True, True): "neither the target's halt nor the backend's auto-resume-on-detach override could be reconfirmed",
}
_UNCONFIRMED_TEARDOWN_CLOSE_REASONS = {
    (True, False): "the target was halted",
    (False, True): "the backend's auto-resume-on-detach override was installed",
    (True, True): "the target was halted or the backend's auto-resume-on-detach override was installed",
}


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
        # Set only when a teardown leaves the target's own state (not merely
        # process cleanup) unconfirmed: halt could not be reconfirmed, or the
        # backend's auto-resume-on-detach override could not be verified
        # installed. A retried teardown must not treat this the way it treats
        # a status left `cleanup_required` by a plain process-cleanup failure
        # (where the target state proof already succeeded) -- see
        # `stop_session` and `close`.
        self.hardware_state_unconfirmed = False


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
        reservation = reserve_tcp_port()
        gdb_port = reservation.port
        try:
            server_args = self._build_server_args(str(resolved_server["executable_path"]), gdb_port, mode != "attach")
            log_path = str(Path(logs_directory(self.config)) / f"gdb-debug-{timestamp_for_filename()}.json")
        except BaseException:
            reservation.release()
            raise
        # The server binds this port by number, so the reservation has to go
        # first; releasing it here, immediately before the spawn, is the shortest
        # the handover window gets. See ReservedTcpPort for what the hold buys.
        reservation.release()
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
            )
        except OSError as error:
            # The spawn itself raised: no server process ever existed, so
            # nothing was started that could have touched the target. Marked as
            # such so the failed call refuses instead of quarantining a board
            # it provably never reached.
            return self._report({"ok": False, "tool": tool, "backend": self.backend_name, "error_type": "debugger_not_found", "summary": "Debug server process could not be started.", "backend_error": str(error), "target_contacted": False, "side_effect_committed": False, "side_effect_status": "not_started", "retry_safe": True})

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
            return {"ok": True, "tool": tool, "backend": self.backend_name, "active": False, "status": "stopped", "safe_state_confirmed": True, "summary": "No debug session is active."}
        timeout = min(self.config.debugger.timeout_s, STOP_SESSION_TIMEOUT_CAP_S)
        if timeout_s is not None:
            timeout = min(timeout, max(0.1, timeout_s))
        # Fixed sequence, in this order, every time a session ends: confirm the
        # target is halted (reconfirming with an explicit interrupt if the last
        # known state does not already say so), tell the backend not to resume it
        # on its own once this connection goes away, and only then tear the
        # connection down. See `_confirm_halted_before_end` and
        # `_pin_no_resume_on_detach` for why each step exists.
        #
        # Skipped when the session already entered as cleanup_required *and*
        # that state was left by a plain process-cleanup failure, not by a
        # target-state incident (`session.hardware_state_unconfirmed`): a
        # cleanup-only failure already ran this same sequence over the
        # connection and got a real proof out of it before cleanup itself
        # failed, so a retry only needs to finish tidying up, not re-prove
        # what it already proved.
        #
        # A target-state incident is different: nothing about retrying this
        # call can manufacture new evidence about a connection that is
        # already, permanently gone, so `halt_confirmed`/`detach_pinned` stay
        # false and the incident is preserved every time this is called again,
        # until an operator's recovery or a later machine action that resets
        # and rereads the target establishes the board state. That is tracked
        # at the lease level, not synthesized here from a retry alone.
        already_unsettled = session.status == "cleanup_required"
        retry_without_new_evidence = already_unsettled and session.hardware_state_unconfirmed
        if retry_without_new_evidence:
            halt_confirmed = False
            detach_pinned = False
        elif already_unsettled:
            halt_confirmed = True
            detach_pinned = True
        else:
            halt_confirmed = self._confirm_halted_before_end(session, timeout)
            detach_pinned = self._pin_no_resume_on_detach(session, timeout)
        cleanup_error = self._cleanup_session(session, timeout)
        if cleanup_error is not None:
            session.status = "cleanup_required"
            if not halt_confirmed or not detach_pinned:
                session.hardware_state_unconfirmed = True
            return self._report({"ok": False, "tool": tool, "backend": self.backend_name, "active": True, "status": "cleanup_required", "hardware_state": "unknown", "cleanup_required": True, "safe_state_confirmed": False, "halt_not_confirmed": not halt_confirmed, "detach_resume_guard_confirmed": detach_pinned, "error_type": "cleanup_failed", "cleanup_error": cleanup_error, "session": self._session_status(session), "log_path": display_path(self.config, session.log_path), "summary": "Debug session cleanup failed; ownership is retained for retry."})
        if not halt_confirmed or not detach_pinned:
            session.status = "cleanup_required"
            session.hardware_state_unconfirmed = True
            unconfirmed_what = _UNCONFIRMED_TEARDOWN_PROOFS[(not halt_confirmed, not detach_pinned)]
            return self._report({
                "ok": False,
                "tool": tool,
                "backend": self.backend_name,
                "active": True,
                "status": "cleanup_required",
                "hardware_state": "unknown",
                "cleanup_required": True,
                "safe_state_confirmed": False,
                "halt_not_confirmed": not halt_confirmed,
                "detach_resume_guard_confirmed": detach_pinned,
                "error_type": "halt_not_confirmed" if not halt_confirmed else "detach_resume_not_confirmed",
                "session": self._session_status(session),
                "log_path": display_path(self.config, session.log_path),
                "summary": f"Debug session processes were cleaned up, but {unconfirmed_what} before the session ended; ownership is retained for retry.",
            })
        session.status = "stopped"
        session.hardware_state_unconfirmed = False
        self.session = None
        return self._report({"ok": True, "tool": tool, "backend": self.backend_name, "active": False, "status": "stopped", "safe_state_confirmed": True, "halt_not_confirmed": False, "detach_resume_guard_confirmed": True, "session": self._session_status(session), "log_path": display_path(self.config, session.log_path), "summary": "Debug session stopped with the target confirmed halted."})

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
        # The one permission that gates lifting the core off a halt. Every other
        # session tool reads or holds the target rather than resuming it, so none
        # of them are gated on this: opening a session (an SWD attach halts the
        # core) and inspecting one already needs no more than allow_probe already
        # requires, the same "reading may still perturb, but exclusivity answers
        # for that" rule AgenticHILConfig.read_free states for the rest of this
        # project. Checked before the "already stopped" fast path below on
        # purpose: whether the target happens to be sitting in a fault is not a
        # reason to skip asking whether this bench may resume it at all, and
        # `debug_get_stop_reason` already answers the same question this fast
        # path would without needing this grant.
        if not self.config.debugger.permissions.allow_debug_execution:
            return self._report(self._permission_denied(
                tool,
                "Resuming target execution requires allow_debug_execution in the authoritative config.",
                remediation_scope="allow_debug_execution",
            ))
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

    def symbol_value(self, symbol: str) -> JsonObject:
        """One allowed symbol's bytes, returned to the caller.

        The gap the other two symbol tools leave between them: `symbol_info`
        answers where a symbol is and how large it is, `dump_symbol_ihex`
        answers with a file on disk, and the bytes this backend already reads on
        the way to writing that file never reach anybody who wanted to compare
        them against an expected value (#174).

        The same read as the dump, ending differently. Same allowlist, same
        `debug.max_dump_size_bytes` cap on the resolved size, same
        `_read_memory_bytes` path; what changes is that nothing is written
        anywhere and the bytes come back as `hex` plus, at an integer width, the
        two readings of them, in the order the session's own image declares.
        """
        tool = "debug_symbol_value"
        session_result = self._require_session(tool)
        if not session_result["ok"]:
            return self._report(session_result)
        session = session_result["session"]
        resolved = self._resolve_symbol(tool, session, symbol)
        if not resolved["ok"]:
            return self._report(resolved)
        size_bytes = int(resolved["size_bytes"])
        if size_bytes > self.config.debug.max_dump_size_bytes:
            return self._report({"ok": False, "tool": tool, "backend": self.backend_name, "error_type": "permission_denied", "summary": "Symbol read exceeds debug.max_dump_size_bytes.", "symbol": symbol, "size_bytes": size_bytes, "max_dump_size_bytes": self.config.debug.max_dump_size_bytes})
        memory = self._read_memory_bytes(tool, session, int(resolved["address_value"]), size_bytes)
        if not memory["ok"]:
            return self._report(memory)
        self._write_session_log(session)
        return self._report({
            "ok": True,
            "tool": tool,
            "backend": self.backend_name,
            "symbol": symbol,
            "address": resolved["address"],
            "size_bytes": size_bytes,
            "resolved_from": resolved["resolved_from"],
            # The session's own artifact, which is the image GDB loaded and the
            # target is running, so its EI_DATA is the target's byte order.
            **decode_symbol_value(memory["data"], image_byte_order(str(session.artifact["resolved_path"]))),
            "session": self._session_status(session),
            "log_path": display_path(self.config, session.log_path),
            "summary": "Symbol value read from target memory.",
        })

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
            write_intel_hex_file(Path(str(output["resolved_path"])), int(resolved["address_value"]), memory["data"], workspace=self.config.work_dir)
        except (ConfigError, OSError) as error:
            return self._report({"ok": False, "tool": tool, "backend": self.backend_name, "error_type": "output_write_failed", "summary": "Intel HEX output file could not be written.", "backend_error": str(error)})
        self._write_session_log(session)
        return self._report({"ok": True, "tool": tool, "backend": self.backend_name, "symbol": symbol, "address": resolved["address"], "size_bytes": size_bytes, "resolved_from": resolved["resolved_from"], "output": output, "session": self._session_status(session), "log_path": display_path(self.config, session.log_path), "summary": "Symbol memory dumped as Intel HEX."})

    def close(self) -> None:
        session = self.session
        if session is not None and session.status != "stopped":
            # See stop_session for why a session already entered as
            # cleanup_required skips straight to tidying up only when that
            # status came from a plain process-cleanup failure, and why a
            # target-state incident (hardware_state_unconfirmed) instead
            # keeps demanding proof this call cannot manufacture.
            already_unsettled = session.status == "cleanup_required"
            retry_without_new_evidence = already_unsettled and session.hardware_state_unconfirmed
            if retry_without_new_evidence:
                halt_confirmed = False
                detach_pinned = False
            elif already_unsettled:
                halt_confirmed = True
                detach_pinned = True
            else:
                halt_confirmed = self._confirm_halted_before_end(session, CLOSE_SESSION_TIMEOUT_S)
                detach_pinned = self._pin_no_resume_on_detach(session, CLOSE_SESSION_TIMEOUT_S)
            cleanup_error = self._cleanup_session(session, CLOSE_SESSION_TIMEOUT_S)
            if cleanup_error is not None:
                session.status = "cleanup_required"
                if not halt_confirmed or not detach_pinned:
                    session.hardware_state_unconfirmed = True
                raise RuntimeError(f"Debug session cleanup failed: {cleanup_error}")
            if not halt_confirmed or not detach_pinned:
                session.status = "cleanup_required"
                session.hardware_state_unconfirmed = True
                reason = _UNCONFIRMED_TEARDOWN_CLOSE_REASONS[(not halt_confirmed, not detach_pinned)]
                raise RuntimeError(f"Debug session closed without reconfirming {reason}.")
            session.status = "stopped"
            session.hardware_state_unconfirmed = False
        self.session = None

    def discard_after_recovery(self) -> None:
        """Retire whatever session is on file without asking it for teardown
        proof it cannot give.

        `stop_session` and `close()` both refuse to clear a session left
        `cleanup_required` with `hardware_state_unconfirmed` set, on purpose:
        nothing about calling either again can manufacture evidence about a
        connection that is already gone. But an independent recovery --
        `reset_target` verified into halt, or a machine/operator recovery that
        reaped this owner's leftover processes and re-read the probe -- speaks
        for the same hardware more directly than that stale session ever could,
        and it is only ever called once that evidence has already cleared the
        coordination-level incident this session's own failure raised. Holding
        onto the session past that point serves nothing: it only makes the next
        `debug_start_session` refuse with `session_already_active` over a
        session the coordinator itself now considers settled, and makes a
        later `close()` raise trying to reconfirm what recovery already did."""
        self.session = None

    def _start_permission(self, tool: str, mode: str) -> JsonObject:
        permissions = self.config.debugger.permissions
        if not self.config.probe_allowed():
            return self._permission_denied(tool, "Debug sessions require allow_probe in the authoritative config.")
        if mode != "attach" and not permissions.allow_reset:
            return self._permission_denied(tool, f"Debug session mode '{mode}' requires allow_reset in the authoritative config.")
        if permissions.allow_raw_debugger_commands:
            return self._permission_denied(tool, exclusive_permission_summary("A debug session", "allow_raw_debugger_commands", self.config.debugger_id))
        if mode == "load":
            if not permissions.allow_flash:
                return self._permission_denied(tool, "Debug session mode 'load' requires allow_flash in the authoritative config.")
            if permissions.allow_mass_erase:
                return self._permission_denied(tool, exclusive_permission_summary("Debug session mode 'load'", "allow_mass_erase", self.config.debugger_id))
        return {"ok": True}

    def _permission_denied(self, tool: str, summary: str, *, remediation_scope: str | None = None) -> JsonObject:
        result = {"ok": False, "tool": tool, "backend": self.backend_name, "error_type": "permission_denied", "summary": summary}
        if remediation_scope is not None:
            result.update(remediation_fields("permission_denied", remediation_scope))
        return result

    def _resolve_gdb(self) -> JsonObject:
        return resolve_gdb_executable(self.config, self.backend_name)

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
        """Address and size for one symbol: the debug information first, the ELF's
        symbol table when the debug information has nothing to say.

        The expression evaluator is asked first because it is the route that
        understands types, scopes and C++ names. It needs a typed symbol for
        both `&symbol` and `sizeof(symbol)`, and an object the assembler defined
        with `.global` / `.type` / `.size` and no DWARF has no type for either,
        the vector table being the standard example. That symbol is not
        under-specified, it is described somewhere else: `st_value` and
        `st_size` are in the symbol table of the very ELF this session loaded,
        so that is where the answer comes from when GDB declines (#187).

        The fallback narrows nothing. The allowlist is checked before either
        route runs, `debug.max_dump_size_bytes` is applied by the callers to
        whichever size came back, and a symbol neither route can describe is
        refused exactly as it was before, now naming what the symbol table said
        as well.
        """
        validated = self._validate_symbol(tool, symbol)
        if not validated["ok"]:
            return validated
        # The stop reason as it stood before the queries. An untyped symbol makes
        # GDB answer `^error`, and _gdb_command records every failed command as a
        # debugger_error stop, which would make the next debug_continue
        # short-circuit on "Target is already stopped" because a symbol lookup
        # took the second route. Restored below for the same reason
        # clear_breakpoints restores it: resolving a symbol does not change
        # target execution state.
        prior_stop_reason = session.stop_reason
        address_value, failed = self._evaluate_symbol_expression(session, f"(unsigned long)&{symbol}")
        size_value = None
        if failed is None:
            size_value, failed = self._evaluate_symbol_expression(session, f"sizeof({symbol})")
        if failed is None:
            return {"ok": True, "symbol": symbol, "address": hex(int(address_value)), "address_value": int(address_value), "size_bytes": int(size_value), "resolved_from": "debug_info"}
        if failed.timed_out or getattr(failed, "audit_failure", False):
            # A query that never got an answer says nothing about the symbol. The
            # symbol table would answer a question the caller never got to ask
            # and would hide a debugger that has stopped responding or an audit
            # trail that has broken, so neither is covered by this fallback.
            return self._symbol_expression_failure(tool, symbol, failed)
        table = read_elf_symbol(str(session.artifact["resolved_path"]), symbol)
        if not table["ok"]:
            return {**self._symbol_expression_failure(tool, symbol, failed), "symbol_table_lookup": table["reason"]}
        if session.stop_reason is not None and str(session.stop_reason.get("stop_reason")) == "debugger_error":
            session.stop_reason = prior_stop_reason
        address = int(table["address"])
        return {"ok": True, "symbol": symbol, "address": hex(address), "address_value": address, "size_bytes": int(table["size_bytes"]), "resolved_from": "elf_symbol_table"}

    def _evaluate_symbol_expression(self, session: GdbDebugSession, expression: str) -> tuple[int | None, object | None]:
        """One expression evaluation, as a value or as the response that did not
        produce one. The response travels back rather than a message, because
        whether it timed out and whether the audit latch refused it decide
        whether the symbol table may be consulted at all."""
        response = self._gdb_command(session, f"-data-evaluate-expression {mi_string(expression)}")
        value = parse_gdb_integer(mi_field(response.line, "value")) if response.ok else None
        return value, None if value is not None else response

    def _symbol_expression_failure(self, tool: str, symbol: str, response: object) -> JsonObject:
        if getattr(response, "ok", False):
            return {"ok": False, "tool": tool, "backend": self.backend_name, "error_type": "symbol_resolution_failed", "summary": "GDB returned an unparsable symbol address or size.", "symbol": symbol, "side_effect_committed": False}
        return self._symbol_failure(tool, symbol, getattr(response, "error_message", None), bool(getattr(response, "timed_out", False)))

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

    def _confirm_halted_before_end(self, session: GdbDebugSession, timeout_s: float) -> bool:
        """Whether the target is confirmed halted, reconfirming with an explicit
        interrupt if the session's last known state does not already say so.

        Called only on the way a session ends (`stop_session`, `close`), never
        on the way through: a debug server commonly resumes an attached target
        on its own once the last GDB connection detaches or exits, so what a
        session leaves running is decided the moment before that happens, not
        after. `debug_continue`'s own timeout path already earns this proof for
        a stop it was already waiting on; this is the same proof taken for a
        stop nobody asked for, because ending the session is not exempt from
        it.

        Vacuously ``True`` for a session that never reached a target:
        `start_session` can fail before `-target-select` ever runs, and this
        same teardown path tears a failed start down too. There is no remote
        connection for a detach to resume anything on, so there is nothing to
        confirm, and a proof demanded of a session that never touched the board
        would be a proof of nothing.
        """
        if session.load_phase in {"not_started", "server_spawned", "server_ready"}:
            return True
        if session.gdb is None or not session.gdb.is_running():
            return False
        self._refresh_session_stop(session)
        if session.status == "halted":
            return True
        response = self._gdb_command(session, "-exec-interrupt --all", timeout_s, containment=True)
        if not response.ok:
            return False
        assert session.gdb is not None
        stop = session.gdb.wait_for_stop(timeout_s)
        confirmed = not stop.timed_out and stop.reason != "debugger_error"
        if confirmed:
            session.stop_reason = self._stop_reason_from_gdb(session, stop)
            session.status = "halted"
        else:
            session.status = "running"
            session.stop_reason = {"stop_reason": "timeout", "backend_stop_reason": "timeout", "halt_confirmed": False}
        return confirmed

    def _pin_no_resume_on_detach(self, session: GdbDebugSession, timeout_s: float) -> bool:
        """Tell OpenOCD not to resume the target on its own once this GDB
        connection goes away, and report whether that was confirmed accepted.

        OpenOCD's ``gdb-detach`` and ``gdb-end`` target events resume the core
        by default the moment the last GDB connection ends, which is exactly
        backwards for a session this project just finished proving halted with
        `_confirm_halted_before_end`. That proof covers the instant before the
        connection ends; this is what keeps the disconnect itself from moving
        the target a moment later, by overriding both events to do nothing
        before anything that could trigger either one runs.

        Fixed and sent every time a session ends, so a later change to this
        sequence has to edit this function to drop the override rather than
        having it fall out of a reordering somewhere else. Never allowed to
        overturn a halt already confirmed: `_gdb_command` marks a failed
        command as a fresh ``debugger_error`` stop, and a defensive command
        nobody asked for is not license to make a confirmed halt read as
        lost, so a failure here restores the state this method was called
        with instead of leaving that behind.

        This is no longer best effort from the caller's point of view: a
        session teardown that cannot verify this override is installed must
        not report the target's post-detach state as safe, because OpenOCD's
        default is to resume it. The caller decides what that means for the
        overall result; this method only reports whether the override itself
        was confirmed.
        """
        if session.load_phase in {"not_started", "server_spawned", "server_ready"}:
            return True
        if session.gdb is None or not session.gdb.is_running():
            return False
        prior_stop_reason = session.stop_reason
        prior_status = session.status
        response = self._gdb_command(
            session,
            '-interpreter-exec console "monitor $_TARGETNAME configure -event gdb-detach {}; $_TARGETNAME configure -event gdb-end {}"',
            timeout_s,
            containment=True,
        )
        if not response.ok:
            session.stop_reason = prior_stop_reason
            session.status = prior_status
            return False
        return True

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


def configured_gdb_missing(backend_name: str) -> JsonObject:
    """The refusal for a GDB this configuration names and this host has not got.

    Resolved before a debug server is spawned, so a missing GDB is a call that
    never started anything: refuse, do not quarantine. One builder because the
    offline symbol query raises the same refusal a second time, when the GDB
    that resolved will not start after all, and two copies of a refusal are two
    places for its advice to drift from the catalogue's.
    """
    return {
        "ok": False,
        "backend": backend_name,
        "error_type": "gdb_not_found",
        "summary": "Configured debug.gdb_executable could not be found.",
        "likely_causes": ["debug.gdb_executable points to a missing file", "GDB is not installed"],
        **remediation_fields("gdb_not_found"),
        "target_contacted": False,
        "side_effect_committed": False,
        "side_effect_status": "not_started",
        "retry_safe": True,
    }


def no_gdb_on_this_bench(backend_name: str) -> JsonObject:
    """The refusal for a bench that never named a GDB and had none to find.

    A different statement from the one above, and it took #359 to separate them:
    this one is about a configuration that is *right*, on a host that has no GDB
    installed, so nothing in the file is worth reading and no path is worth
    hunting for. What fixes it is a GDB on this host or the key set to one, and
    the catalogue entry behind the scope carries both.
    """
    return {
        "ok": False,
        "backend": backend_name,
        "error_type": "gdb_not_found",
        "summary": "No GDB executable could be found.",
        "likely_causes": ["debug.gdb_executable is not set in the authoritative project config", "no GDB was found on PATH when this server started"],
        **remediation_fields("gdb_not_found", GDB_NOT_CONFIGURED_SCOPE),
        "target_contacted": False,
        "side_effect_committed": False,
        "side_effect_status": "not_started",
        "retry_safe": True,
    }


def resolve_gdb_executable(config: AgenticHILConfig, backend_name: str) -> JsonObject:
    """Find the GDB this bench is meant to use, by one rule for every backend.

    Extracted from the debug backend unchanged so the stlink backend's offline
    symbol query resolves GDB exactly as a debug session does: a configured
    `debug.gdb_executable` wins and is never silently replaced by a different
    GDB, and only an unconfigured bench falls back to autodetection. Both
    refusals name `debug.gdb_executable`, because that is the one field an
    operator sets to fix either.

    A pinned placeholder is neither of those states, and reading it as a
    configured path was the bug (#359). Pinning writes it into
    `debug.gdb_executable` when the file named no GDB and startup autodetected
    none, so on `load_authoritative_config`, which is the load production takes,
    every bench without a GDB arrived here carrying a path and was told its
    *configured* GDB was missing, for a file that had configured nothing. It is
    answered as what it records instead.

    Recognised rather than followed: the search the placeholder stands for has
    already run, at the one moment its answer is checked. Pinning is what proves
    a resolved GDB is a single-link regular file outside the workspace, and a
    `which` call at session start would prove neither, so a placeholder falling
    through to autodetection would run whatever a changed PATH now offers,
    including out of the workspace. The bench that has none keeps none until it
    is started again.
    """
    configured = config.debug.gdb_executable
    if configured and executable_is_disabled(configured):
        return no_gdb_on_this_bench(backend_name)
    if configured:
        has_path_separator = "/" in configured or "\\" in configured
        if Path(configured).is_absolute() or has_path_separator:
            from agentic_hil.config import resolve_work_path

            resolved = Path(resolve_work_path(config, configured))
            if resolved.is_file():
                return {"ok": True, "executable": str(resolved)}
        else:
            found = which(configured)
            if found is not None:
                return {"ok": True, "executable": found}
        return configured_gdb_missing(backend_name)
    for candidate in GDB_AUTODETECT_CANDIDATES:
        found = which(candidate)
        if found is not None:
            return {"ok": True, "executable": found}
    return no_gdb_on_this_bench(backend_name)


# What the offline symbol query prints, and what is read back out of it. Named
# markers rather than GDB's own `$1 = ...` value history: a command that failed
# prints an error and no marker at all, so a missing answer can never be
# mistaken for a parsed one, and the reply survives GDB versions that number or
# decorate the echo differently.
GDB_SYMBOL_ADDRESS_MARKER = "AGENTIC_HIL_SYMBOL_ADDRESS="
GDB_SYMBOL_SIZE_MARKER = "AGENTIC_HIL_SYMBOL_SIZE="
GDB_SYMBOL_QUERY_TIMEOUT_S = 30.0
GDB_SYMBOL_MISSING_MARKERS = ["no symbol", "not defined"]


def gdb_symbol_query_args(gdb_executable: str, elf_path: str, symbol: str) -> list[str]:
    """A GDB that reads one symbol out of an ELF and attaches to nothing.

    `--batch` runs the two commands and exits; `-nx` keeps a developer's
    `.gdbinit` (which can define, alias or hook anything) out of a bench
    answer; `-q` drops the banner. No `target`, `attach` or `monitor` command
    appears, and none can: this is the whole command line, so the query cannot
    reach a probe even if one is connected.

    `printf` rather than `print` because it emits exactly the marker line and
    nothing on failure. The casts to `unsigned long` are what make the two
    values plain decimal integers regardless of the symbol's own type.
    """
    return [
        *invocation(gdb_executable),
        "--batch",
        "-nx",
        "-q",
        "-ex",
        f'printf "{GDB_SYMBOL_ADDRESS_MARKER}%lu\\n", (unsigned long)&{symbol}',
        "-ex",
        f'printf "{GDB_SYMBOL_SIZE_MARKER}%lu\\n", (unsigned long)sizeof({symbol})',
        elf_path,
    ]


def gdb_symbol_marker_value(output: str, marker: str) -> int | None:
    for line in output.splitlines():
        stripped = line.strip()
        if stripped.startswith(marker):
            digits = stripped[len(marker) :].strip()
            if digits.isdigit():
                return int(digits)
    return None


def validate_debug_symbol(config: AgenticHILConfig, backend_name: str, tool: str, symbol: str) -> JsonObject:
    """The session path's two refusals, word for word, for a backend without one.

    Shared rather than approximated per backend: `debug.allowed_symbols` is the
    operator's statement about what may leave this bench at all, and a caller
    that reads the refusal must not be able to tell which backend wrote it, or
    the allowlist would look like a property of the debugger instead of a
    property of the project.
    """
    if not isinstance(symbol, str) or DEBUG_SYMBOL_PATTERN.match(symbol) is None:
        return {"ok": False, "tool": tool, "backend": backend_name, "error_type": "invalid_argument", "summary": "symbol must be a valid C/C++ identifier."}
    if not config.debug.allow_all_symbols and symbol not in config.debug.allowed_symbols:
        return {"ok": False, "tool": tool, "backend": backend_name, "error_type": "permission_denied", "summary": "Symbol is not allowed by debug.allowed_symbols.", "symbol": symbol}
    return {"ok": True}


def resolve_symbol_offline(config: AgenticHILConfig, backend_name: str, tool: str, symbol: str, symbol_elf: JsonObject | None) -> JsonObject:
    """Address and size for one symbol, from an ELF and nothing else.

    A debug session resolves a symbol by asking the GDB that already has the
    image loaded and the target attached. There is no session on the ST-Link or
    pyOCD backends, so the same GDB is asked in batch mode against the ELF
    alone: no `target` command is issued, nothing is attached, and the probe is
    not opened until the read itself. That keeps the query harmless on a running
    board and means a bad symbol name costs a refusal rather than a hardware
    contact.

    Backend-independent by construction, which is why it lives here rather than
    on either backend: an ELF, a GDB and a digest are all it touches, and the
    two sessionless backends must answer a symbol question identically or the
    address a plan reads would depend on which probe it ran against (#344).

    The ELF is the one this service flashed, so the symbol table provably
    describes what is on the target, which is the guarantee a session gets from
    having loaded the image. Nothing else is accepted: resolving against some
    other build would answer with an address that is right about the file and
    wrong about the board.

    The image's byte order travels back with the address, for the caller that
    turns the bytes into a number. Read here rather than by that caller because
    here is where the trustworthy copy exists: the private staged file is
    deleted on the way out, and reading EI_DATA off the caller's path afterwards
    would be reading a file a rebuild is free to have replaced, which is the
    exact race the copy was made to close.
    """
    if symbol_elf is None:
        return {
            "ok": False,
            "tool": tool,
            "backend": backend_name,
            "error_type": "symbol_source_not_available",
            "summary": "No ELF with debug symbols has been flashed through this service, so no symbol table describes what is on the target. Flash the ELF with flash_firmware first.",
            "symbol": symbol,
            "likely_causes": ["no flash_firmware call has succeeded in this session", "the firmware that was flashed was a .hex or .bin, which carries no symbols", "the firmware on the target was flashed outside Agentic HIL"],
            **NOT_CONTACTED,
        }
    # The remembered path is the caller's own workspace file, which a rebuild is
    # free to overwrite at any moment, including in the window between hashing it
    # and handing the same path to a separately spawned GDB. A replacement landing
    # there makes GDB answer from bytes the digest never covered while the result
    # still carries the flashed image's digest, so an address that is right about
    # the new build and wrong about the board would travel out wearing the old
    # image's provenance. So the bytes are copied into a private file first; the
    # digest that gates the query and the bytes GDB reads are then the same
    # immutable file, and a later replacement of the caller's path cannot reach
    # it. A copy whose digest no longer matches the flashed image is refused,
    # exactly as a changed source on the original path was.
    elf_path = str(symbol_elf["resolved_path"])
    recorded_digest = symbol_elf.get("integrity_sha256") or symbol_elf.get("sha256")
    staging_dir: Path | None = None
    try:
        try:
            staging_dir = Path(tempfile.mkdtemp(prefix="agentic-hil-symbols-"))
            staged_elf = staging_dir / Path(elf_path).name
            shutil.copyfile(elf_path, staged_elf)
            staged_digest = sha256_file(staged_elf)
        except OSError as error:
            return {"ok": False, "tool": tool, "backend": backend_name, "error_type": "symbol_source_changed", "summary": "The ELF flashed through this service can no longer be read, so no symbol table is proven to describe what is on the target. Flash it again with flash_firmware.", "symbol": symbol, "backend_error": str(error), **NOT_CONTACTED}
        if not isinstance(recorded_digest, str) or staged_digest != recorded_digest:
            return {"ok": False, "tool": tool, "backend": backend_name, "error_type": "symbol_source_changed", "summary": "The ELF on disk no longer matches the image flashed through this service, so its symbol table is not proven to describe what is on the target. Flash the current build with flash_firmware first.", "symbol": symbol, "likely_causes": ["the ELF was rebuilt or replaced after it was flashed", "a different file now occupies the flashed artifact's path"], **NOT_CONTACTED}
        gdb = resolve_gdb_executable(config, backend_name)
        if not gdb["ok"]:
            return {**gdb, "tool": tool, "symbol": symbol}
        args = gdb_symbol_query_args(str(gdb["executable"]), str(staged_elf), symbol)
        completed = spawn_command(args, str(staging_dir), min(config.debugger.timeout_s, GDB_SYMBOL_QUERY_TIMEOUT_S))
        if completed.not_found:
            # The same refusal `resolve_gdb_executable` writes, from its builder
            # rather than from a copy: a GDB that resolved and then would not
            # start is the state that entry describes, and a second copy here
            # would be a second place for its advice to age.
            return {**configured_gdb_missing(backend_name), "tool": tool, "symbol": symbol, **NOT_CONTACTED}
        if completed.timed_out:
            return {"ok": False, "tool": tool, "backend": backend_name, "error_type": "timeout", "summary": "Symbol resolution timed out.", "symbol": symbol, **NOT_CONTACTED}
        output = f"{completed.stdout}{completed.stderr}"
        address_value = gdb_symbol_marker_value(output, GDB_SYMBOL_ADDRESS_MARKER)
        size_value = gdb_symbol_marker_value(output, GDB_SYMBOL_SIZE_MARKER)
        source = {"path": symbol_elf.get("path"), "sha256": symbol_elf.get("sha256")}
        order = image_byte_order(staged_elf)
        if address_value is not None and size_value is not None:
            return {"ok": True, "symbol": symbol, "address": hex(address_value), "address_value": address_value, "size_bytes": size_value, "resolved_from": "debug_info", "symbol_source": source, "image_byte_order": order}
        # Both markers come from expressions that need a typed symbol, and an
        # assembly-defined object carries no type for them, while its address
        # and size sit in the symbol table of this same staged ELF, which is the
        # digest-verified copy of the image on the board (#187). Read from that
        # copy rather than from the caller's path, so the fallback answers out of
        # exactly the bytes the flashed digest covers, as the marker query does.
        table = read_elf_symbol(staged_elf, symbol)
        if table["ok"]:
            address = int(table["address"])
            return {"ok": True, "symbol": symbol, "address": hex(address), "address_value": address, "size_bytes": int(table["size_bytes"]), "resolved_from": "elf_symbol_table", "symbol_source": source, "image_byte_order": order}
        lower = output.lower()
        error_type = "symbol_not_found" if contains_any(lower, GDB_SYMBOL_MISSING_MARKERS) else "symbol_resolution_failed"
        summary = "Symbol was not found in the flashed ELF." if error_type == "symbol_not_found" else "GDB returned no usable symbol address or size for the flashed ELF."
        return {"ok": False, "tool": tool, "backend": backend_name, "error_type": error_type, "summary": summary, "symbol": symbol, "symbol_table_lookup": table["reason"], **NOT_CONTACTED}
    finally:
        if staging_dir is not None:
            shutil.rmtree(staging_dir, ignore_errors=True)


def offline_symbol_info(config: AgenticHILConfig, backend_name: str, symbol: str, symbol_elf: JsonObject | None) -> JsonObject:
    """Where an allowed symbol lives and how large it is, from the ELF alone.

    The one typed-debug read that never opens a probe: an address and a size are
    properties of the image, and the image is on disk. It refused on the
    sessionless backends only because it was filed with the session family
    (#342 for ST-Link, #344 for pyOCD), which made a bench ask the hardware for
    a fact the hardware was never going to be asked for.

    The same offline resolution the memory reads run before they connect, and
    nothing after it. A caller sees the session path's fields and summary, plus
    the `symbol_source` that says which flashed image answered: the provenance a
    session gets for free by having loaded the image, and that a sessionless
    backend has to state.

    One body for both backends, because the answer must not depend on which
    probe is plugged in. Because no probe is opened, `allow_probe` does not gate
    it and no lease is taken: the coordination layer leases the reads that reach
    the board (`sessionless_debug_tools`), and this is not one of them. What does
    gate it is `debug.allowed_symbols`, which is a statement about what may leave
    this bench at all and applies to an address as much as to bytes.
    """
    tool = "debug_symbol_info"
    validated = validate_debug_symbol(config, backend_name, tool, symbol)
    if not validated["ok"]:
        return validated
    resolved = resolve_symbol_offline(config, backend_name, tool, symbol, symbol_elf)
    if not resolved["ok"]:
        return resolved
    return {
        "ok": True,
        "tool": tool,
        "backend": backend_name,
        "symbol": symbol,
        "address": resolved["address"],
        "size_bytes": int(resolved["size_bytes"]),
        "resolved_from": resolved["resolved_from"],
        "symbol_source": resolved["symbol_source"],
        "summary": "Symbol resolved.",
        # Nothing was driven, so this says so in the same words every other
        # refusal-before-contact on these backends uses. A resolution that never
        # opened a probe must not read as a hardware interaction to whatever
        # downstream is deciding what may be retried.
        **NOT_CONTACTED,
    }


def _is_missing_breakpoint_error(response: object) -> bool:
    """A `-break-delete N` that fails because GDB already removed N (e.g. after a
    lost ACK) is proof of deletion, not a cleanup failure."""
    if getattr(response, "audit_failure", False) or getattr(response, "timed_out", False):
        return False
    message = (getattr(response, "error_message", "") or "").lower()
    return "no breakpoint number" in message


def image_byte_order(elf_path: str | Path) -> JsonObject:
    """Which byte order an image's integers are read in, and where that came from.

    The image is asked, because it is the only party to a memory read that knows.
    GDB's `-data-read-memory-bytes` and STM32CubeProgrammer's `-r` both answer in
    memory order and the target has no way to say how to read it, while the ELF
    behind the session states it in EI_DATA the way every ELF must. So a
    big-endian build decodes big-endian with nothing configured anywhere, and the
    field says which order was used rather than leaving a caller to assume the one
    a Cortex-M has (#249).

    `byte_order_from` is here because the fallback must be visible. A file that
    declares neither LSB nor MSB is not an ELF this project can read at all (the
    symbol-table parser refuses exactly that file), so nothing that has a
    firmware image in front of it reaches the default; what does is a stub or a
    truncated file, and a reader is then told the order was assumed rather than
    read off the image.
    """
    declared = read_elf_byte_order(elf_path)
    if declared is None:
        return {"byte_order": DEFAULT_SYMBOL_VALUE_BYTE_ORDER, "byte_order_from": "default"}
    return {"byte_order": declared, "byte_order_from": "elf_header"}


def decode_symbol_value(data: bytes, order: JsonObject) -> JsonObject:
    """The bytes a symbol holds, as hex and, where the width allows it, as a number.

    `hex` is always there and is the answer: the bytes in memory order, which is
    what a caller comparing against a firmware structure needs and the only
    reading that is right for every symbol, on every target, whatever the image
    declares. The two integer readings are an interpretation on top of it, and
    they are offered only for the four widths that have one: a 3-byte or
    408-byte object is not a number, and inventing a reading for it would be
    inventing the answer rather than returning it.

    `order` is `image_byte_order`'s statement about the image the bytes were
    read out of, and it travels into the result unchanged: the order is half of
    what a number means, so a result that carries one carries where it came
    from too.
    """
    byte_order = str(order["byte_order"])
    result: JsonObject = {"hex": data.hex(), **order}
    if len(data) in INTEGER_VALUE_WIDTHS:
        result["value_unsigned"] = int.from_bytes(data, byte_order, signed=False)
        result["value_signed"] = int.from_bytes(data, byte_order, signed=True)
    else:
        result["value_not_decoded"] = "size_bytes is not 1, 2, 4 or 8, so the bytes carry no single integer reading."
    return result


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


class ReservedTcpPort:
    """A loopback port this process holds until the debug server is spawned.

    The reservation used to be a bind to port 0 followed immediately by a close,
    which names a port and then stops defending it. Everything after that close
    is a window in which the kernel may hand the same number to anyone else who
    asks for an ephemeral port, and it does: eight processes taking 24000 such
    reservations on one Windows machine wrapped the dynamic range and were given
    7590 ports that another of them had also been given. Two debug servers then
    get told to bind one port, and both ways that lands are bad -- the loser
    fails to bind and the session reports a server that would not come up, or the
    two share the address and the debugger talks to whichever the kernel hands
    the connection to.

    So the socket stays bound for as long as the caller has not spawned anything,
    and :meth:`release` runs in the same breath as the spawn. A held address is
    refused to every other process on the platforms this ships on, measured
    across processes here: a plain bind gets ``WSAEADDRINUSE``, a
    ``SO_REUSEADDR`` bind -- how this project's own fake OpenOCD binds -- gets
    ``WSAEACCES``. ``SO_EXCLUSIVEADDRUSE`` and ``listen`` keep that true in the
    two cases a bare bind does not cover: Windows shares an address whose holder
    also set ``SO_REUSEADDR``, and Linux lets two ``SO_REUSEADDR`` sockets share
    a port while neither of them is listening.

    What this cannot do is hand the port over atomically: an out-of-process
    server binds by number, so a window remains between the release and its bind.
    Closing that one needs the server to choose the port and say which one it
    chose, which is a change to what this project tells OpenOCD to do rather than
    a change to how it asks the kernel for a number.
    """

    def __init__(self) -> None:
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            exclusive = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
            if exclusive is not None:
                self._socket.setsockopt(socket.SOL_SOCKET, exclusive, 1)
            self._socket.bind(("127.0.0.1", 0))
            self._socket.listen(1)
            self.port = int(self._socket.getsockname()[1])
        except BaseException:
            self._socket.close()
            raise

    def release(self) -> None:
        """Stop defending the port. Idempotent: the caller releases on the way to
        a spawn and the context manager releases again on the way out."""
        self._socket.close()

    def __enter__(self) -> ReservedTcpPort:
        return self

    def __exit__(self, *_exception: object) -> None:
        self.release()


def reserve_tcp_port() -> ReservedTcpPort:
    return ReservedTcpPort()


def wait_for_tcp_port(port: int, timeout_s: float, server: subprocess.Popen[str]) -> bool:
    """Whether something is listening on `port` before the server gives up or the
    deadline passes.

    Deliberately stated that narrowly. A successful connect establishes that the
    port answers, not that it is *this* server answering: identifying the process
    behind a listening socket needs an OS-specific query this does not make, so a
    server that lost the port race and a stranger that won it read the same here
    for as long as our own process has not exited yet. The narrow window that can
    happen in is what :class:`ReservedTcpPort` exists to keep narrow.
    """
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
