from __future__ import annotations

import math
import re
import threading
import time
from contextlib import suppress
from pathlib import Path

from agentic_hil.config import ConfigError, display_path, safe_append_text
from agentic_hil.coordination import (
    CoordinationError,
    DetachedHardwareLease,
    HardwareCoordinator,
    HardwareLease,
)
from agentic_hil.devices import uart_device
from agentic_hil.provisional import (
    cleanup_provisional_handles,
    discharge_provisional_handle,
    register_provisional_handle,
)
from agentic_hil.report import (
    append_jsonl,
    append_jsonl_audited,
    audit_unavailable,
    logs_directory,
    mark_audit_failure,
    mark_side_effect,
    overall_success,
    recommit_report_with_status,
    safe_filename,
    timestamp_for_filename,
    utc_now_iso,
    write_report,
)
from agentic_hil.types import AgenticHILConfig, ComPortConfig, JsonObject

# How long a short write is given to finish beyond the port's own write timeout.
# The schema permits `write_timeout_s: 0`, which makes every write non-blocking
# and a short one ordinary, so a bounded grace period is what turns the common
# case back into a complete write without letting a wedged port hold the caller.
PARTIAL_WRITE_COMPLETION_S = 1.0


def list_available_com_ports(tool: str = "com_ports_available") -> JsonObject:
    try:
        from serial.tools import list_ports
    except ImportError:
        return {
            "ok": False,
            "tool": tool,
            "error_type": "serial_backend_not_available",
            "summary": "pyserial is not installed or could not be imported.",
            "likely_causes": ["install Agentic HIL with its runtime dependencies", "pyserial installation is broken"],
        }
    try:
        ports = [available_port_info(port) for port in list_ports.comports()]
        return {"ok": True, "tool": tool, "ports": ports, "summary": f"{len(ports)} available COM port(s)."}
    except OSError as error:
        return {
            "ok": False,
            "tool": tool,
            "error_type": "com_port_discovery_failed",
            "summary": "Available COM ports could not be listed.",
            "backend_error": str(error),
            "likely_causes": ["serial backend reported an OS error", "USB serial driver state changed during discovery"],
        }


class ComPortSession:
    def __init__(self, port_id: str, port_config: ComPortConfig, serial_handle: object, log_path: str, lease: HardwareLease | None = None, *, start_reader: bool = True):
        self.port_id = port_id
        self.port_config = port_config
        self.serial_handle = serial_handle
        self.log_path = log_path
        self.started_at = utc_now_iso()
        self.active = True
        self.buffer = bytearray()
        self.overflow_bytes = 0
        self.reader_error: JsonObject | None = None
        self.lock = threading.Lock()
        self.io_lock = threading.Lock()
        self.reader: threading.Thread | None = None
        self.audit_broken = False
        self.lease = lease or DetachedHardwareLease()
        if start_reader:
            self.start_reader()

    def start_reader(self) -> None:
        if self.reader is not None:
            return
        reader = threading.Thread(target=self._reader_loop, daemon=True)
        self.reader = reader
        try:
            reader.start()
        except BaseException:
            if not reader.is_alive():
                self.reader = None
            raise

    def _reader_loop(self) -> None:
        while self.active:
            try:
                with self.io_lock:
                    waiting = int(getattr(self.serial_handle, "in_waiting", 0) or 0)
                    read_size = min(max(waiting, 1), self.port_config.max_buffer_bytes, 4096)
                    data = self.serial_handle.read(read_size)
                    if data:
                        chunk = bytes(data)
                        with self.lock:
                            self.buffer.extend(chunk)
                            overflow = len(self.buffer) - self.port_config.max_buffer_bytes
                            if overflow > 0:
                                del self.buffer[:overflow]
                                self.overflow_bytes += overflow
                if not data:
                    time.sleep(0.01)
                    continue
                audit_error = append_jsonl(self.log_path, {"direction": "rx", "bytes": len(chunk), "hex": chunk.hex(), "text": decode_bytes(chunk, self.port_config.encoding)})
                if audit_error is not None:
                    self.reader_error = {"error_type": "audit_write_failed", "summary": "COM port feedback could not be audited.", "backend_error": str(audit_error)}
                    self.audit_broken = True
                    self.lease.quarantine("com_reader_audit_broken", audit_error, audit_broken=True)
                    self.active = False
                    break
            except Exception as error:  # serial backends raise implementation-specific exception classes
                if self.active:
                    self.reader_error = {
                        "error_type": "serial_read_failed",
                        "summary": "COM port reader failed.",
                        "backend_error": str(error),
                        "likely_causes": likely_causes("serial_read_failed"),
                    }
                    append_jsonl(self.log_path, {"event": "error", **self.reader_error})
                break


class ComPortService:
    def __init__(self, config: AgenticHILConfig, coordinator: HardwareCoordinator | None = None):
        self.config = config
        self.coordinator = coordinator or HardwareCoordinator(config, "com-service")
        self._owns_coordinator = coordinator is None
        self.sessions: dict[str, ComPortSession] = {}

    def reconfigure(self, config: AgenticHILConfig) -> None:
        # The port config carries its own permissions, so an inequality here also
        # covers a revoked grant: a session may not outlive the permission that
        # authorized it.
        for port_id, session in list(self.sessions.items()):
            if config.com_ports.get(port_id) != session.port_config:
                self._stop_session(session, "config_reloaded")
                self.sessions.pop(port_id, None)
        self.config = config

    def list_ports(self) -> JsonObject:
        ports: JsonObject = {}
        for port_id, port_config in self.config.com_ports.items():
            ports[port_id] = self._port_status(port_config, self.sessions.get(port_id))
        # Host discovery is not scoped to one configured port, so it needs at
        # least one port the operator allowed reading from.
        if any(self.config.com_read_allowed(port_config) for port_config in self.config.com_ports.values()):
            available = list_available_com_ports()
        else:
            available = {
                "ok": False,
                "tool": "com_ports_available",
                "error_type": "permission_denied",
                "summary": "Host COM port discovery is disabled by the authoritative config; no configured COM port has permissions.allow_read.",
            }
        available_count = len(available.get("ports", [])) if available.get("ok") else 0
        return {
            "ok": True,
            "tool": "com_ports_list",
            "ports": ports,
            "available_com_ports": available,
            "summary": f"{len(ports)} configured COM port(s), {available_count} available host COM port(s).",
        }

    def session_start(self, port_id: str, clear_buffer: bool = True) -> JsonObject:
        if not isinstance(port_id, str) or not isinstance(clear_buffer, bool):
            return self._write_report({"ok": False, "tool": "com_session_start", "error_type": "invalid_argument", "summary": "port_id must be a string and clear_buffer must be a boolean.", "side_effect_committed": False})
        port = self._configured_port(port_id, "com_session_start")
        if not port["ok"]:
            return self._write_report(port)
        # A write-only line is a legitimate configuration, and the session is the
        # only way to reach the port at all — gating the open on allow_read alone
        # made allow_write without allow_read a grant that could never be used.
        port_permissions = port["port_config"].permissions
        if not self.config.com_read_allowed(port["port_config"]) and not port_permissions.allow_write:
            return self._write_report(self._permission_denied("com_session_start", "Reading and writing this COM port are disabled by the authoritative config.", port_id))
        if clear_buffer and not self.config.com_read_allowed(port["port_config"]):
            # Clearing drains the receive buffer, which is a read.
            clear_buffer = False

        existing = self.sessions.get(port_id)
        if existing and self._session_is_active(existing):
            if clear_buffer:
                cleared = self._clear_buffers(existing)
                if not cleared["ok"]:
                    return self._write_report(cleared)
            return self._write_report({"ok": True, "tool": "com_session_start", "port_id": port_id, "already_active": True, "session": self._session_status(existing), "summary": "COM port session is already active."})
        if existing:
            try:
                self._stop_session(existing, "replaced")
            except Exception as error:
                return self._write_report(self._close_failure("com_session_start", port_id, error))
            self.sessions.pop(port_id, None)

        try:
            log_path = str(Path(logs_directory(self.config)) / f"com-{timestamp_for_filename()}-{safe_filename(port_id, 'port')}.jsonl")
            safe_append_text(log_path, "")
        except (ConfigError, OSError) as error:
            return audit_unavailable("com_session_start", error)
        try:
            lease = self.coordinator.acquire(uart_device(self.config, port_id))
        except CoordinationError as error:
            return self._write_report({"tool": "com_session_start", "port_id": port_id, "side_effect_committed": False, **error.result})
        try:
            opened = self._open_serial(port_id, port["port_config"], log_path, lease)
        except BaseException as error:
            lease.quarantine("com_open_interrupted", error)
            raise
        if not opened["ok"]:
            safe_to_release = opened.get("side_effect_committed") is False or opened.get("cleanup_confirmed") is True
            if not safe_to_release:
                lease.quarantine("com_open_cleanup_unconfirmed", opened.get("backend_error"))
            return self._write_unattached_lease_report(opened, lease, release_if_safe=safe_to_release)
        session = opened["session"]
        self.sessions[port_id] = session
        audit_error = append_jsonl_audited(self.config, session.log_path, {"event": "start", "port_id": port_id, "device": session.port_config.device})
        if audit_error is not None:
            session.audit_broken = True
            with suppress(BaseException):
                self._stop_session(session, "audit_failed")
            result = {"ok": True, "tool": "com_session_start", "port_id": port_id, "already_active": False, "session": self._session_status(session), "cleanup_required": True, "summary": "COM port opened, but audit initialization failed; resource is quarantined."}
            return self._write_report(mark_audit_failure(result, audit_error))
        if clear_buffer:
            cleared = self._clear_buffers(session)
            if not cleared["ok"]:
                try:
                    self._stop_session(session, "buffer_clear_failed", defer_release=True)
                except BaseException as close_error:
                    return self._write_report(self._close_failure("com_session_start", port_id, close_error))
                written = self._write_report({**cleared, "cleanup_confirmed": True})
                if written.get("audit_ok") is not False:
                    session.lease.resolve_retryable_cleanup("com_buffer_clear_unconfirmed")
                    if not session.lease.release():
                        return recommit_report_with_status(self.config, written, session.lease.status())
                    self.sessions.pop(port_id, None)
                return recommit_report_with_status(self.config, written, session.lease.status())
        try:
            # The background reader buffers incoming bytes, so it only runs for
            # a port the operator allowed reading from.
            if self.config.com_read_allowed(port["port_config"]):
                session.start_reader()
        except BaseException as error:
            try:
                self._stop_session(session, "start_failed", defer_release=True)
            except BaseException as close_error:
                written = self._write_report(self._close_failure("com_session_start", port_id, close_error))
                if isinstance(error, (KeyboardInterrupt, SystemExit)):
                    error.args = (*error.args, f"Cleanup error: {close_error}")
                    raise error from close_error
                if isinstance(close_error, (KeyboardInterrupt, SystemExit)):
                    raise
                return written
            failure = {"ok": False, "tool": "com_session_start", "port_id": port_id, "error_type": "com_reader_start_failed", "summary": "COM port reader could not be started; the port was closed.", "backend_error": str(error), "cleanup_confirmed": True, "side_effect_committed": False}
            written = self._write_report(failure)
            if written.get("audit_ok") is False:
                return written
            if not session.lease.release():
                return self._write_report(self._close_failure("com_session_start", port_id, RuntimeError("Lease release remained unconfirmed.")))
            self.sessions.pop(port_id, None)
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise error
            return recommit_report_with_status(self.config, written, session.lease.status())
        result = {"ok": True, "tool": "com_session_start", "port_id": port_id, "already_active": False, "session": self._session_status(session), "summary": "COM port session started."}
        return self._write_report(mark_audit_failure(result, audit_error) if audit_error is not None else result)

    def session_stop(self, port_id: str) -> JsonObject:
        port = self._configured_port(port_id, "com_session_stop")
        if not port["ok"]:
            return self._write_report(port)
        session = self.sessions.get(port_id)
        if session is None:
            return self._write_report({"ok": True, "tool": "com_session_stop", "port_id": port_id, "was_active": False, "summary": "COM port session was not active."})
        try:
            audit_error = self._stop_session(session, "requested", defer_release=True)
        except Exception as error:
            return self._write_report(self._close_failure("com_session_stop", port_id, error))
        result = {"ok": True, "tool": "com_session_stop", "port_id": port_id, "was_active": True, "session": self._session_status(session), "summary": "COM port session stopped."}
        written = self._write_report(mark_audit_failure(result, audit_error) if audit_error is not None else result)
        if written.get("audit_ok") is False:
            return {**written, "cleanup_required": True, "quarantined": True}
        if not session.lease.release():
            return self._write_report(self._close_failure("com_session_stop", port_id, RuntimeError("Lease release remained unconfirmed.")))
        self.sessions.pop(port_id, None)
        return recommit_report_with_status(self.config, written, session.lease.status())

    def write(self, port_id: str, payload: JsonObject) -> JsonObject:
        port = self._configured_port(port_id, "com_write")
        if not port["ok"]:
            return self._write_report(port)
        encoded = payload_bytes(port["port_config"], payload)
        if not encoded["ok"]:
            encoded["port_id"] = port_id
            return self._write_report(encoded)
        return self._write_report(self.write_bytes(port_id, encoded["data"], "com_write"))

    def write_bytes(self, port_id: str, data: bytes, tool: str = "com_write") -> JsonObject:
        port = self._configured_port(port_id, tool)
        if not port["ok"]:
            return port
        if not port["port_config"].permissions.allow_write:
            return self._permission_denied(tool, "Writing to this COM port is disabled by the authoritative config.", port_id)
        session_result = self._active_session(port_id, tool)
        if not session_result["ok"]:
            return session_result
        session = session_result["session"]
        if len(data) > session.port_config.max_write_bytes:
            return {"ok": False, "tool": tool, "port_id": port_id, "error_type": "invalid_argument", "summary": "COM port write exceeds configured max_write_bytes.", "bytes_requested": len(data), "max_write_bytes": session.port_config.max_write_bytes}
        try:
            confirmed = self._write_confirmed(session, data)
            flush = getattr(session.serial_handle, "flush", None)
            if callable(flush):
                flush()
        except BaseException as error:
            result = {"ok": False, "tool": tool, "port_id": port_id, "error_type": "serial_write_failed", "summary": "COM port write failed.", "backend_error": str(error), "likely_causes": likely_causes("serial_write_failed"), "log_path": display_path(self.config, session.log_path)}
            session.lease.quarantine("com_write_effect_unconfirmed", error)
            result.update({"side_effect_status": "unknown", "retry_safe": False, "cleanup_required": True, "quarantined": True})
            audit_error = append_jsonl_audited(self.config, session.log_path, {"event": "error", **result})
            if audit_error is not None:
                session.audit_broken = True
                session.lease.quarantine("com_write_audit_broken", audit_error, audit_broken=True)
                return mark_audit_failure(result, audit_error)
            if not isinstance(error, Exception):
                raise
            return result
        # Only the bytes the handle confirmed are audited, whether or not they
        # are all of them: a log that records the whole payload after a short
        # write is a log that disagrees with the wire.
        delivered = data[:confirmed]
        audit_error = append_jsonl_audited(self.config, session.log_path, {"direction": "tx", "bytes": confirmed, "hex": delivered.hex(), "text": decode_bytes(delivered, session.port_config.encoding)})
        if confirmed == len(data):
            result = {"ok": True, "tool": tool, "port_id": port_id, "bytes_written": confirmed, "data": data_result(delivered, session.port_config.encoding), "log_path": display_path(self.config, session.log_path), "summary": "Stimulus written to COM port."}
        else:
            # Part of a stimulus is not a smaller stimulus: the peer has half a
            # frame and the target is in a state nobody asked for, so this is a
            # failure with the effect named, not a success with a smaller count.
            result = {
                "ok": False,
                "tool": tool,
                "port_id": port_id,
                "error_type": "serial_write_incomplete",
                "summary": "COM port accepted only part of the stimulus within the write deadline.",
                "bytes_written": confirmed,
                "bytes_requested": len(data),
                "data": data_result(delivered, session.port_config.encoding),
                "likely_causes": likely_causes("serial_write_incomplete"),
                "log_path": display_path(self.config, session.log_path),
                "side_effect_committed": confirmed > 0,
                "side_effect_status": "partial" if confirmed else "not_started",
                "retry_safe": confirmed == 0,
            }
            if confirmed:
                # Nothing here knows what the target made of half a stimulus, so
                # the resource is held until somebody says the bench is safe.
                session.lease.quarantine("com_write_partial")
                result.update({"cleanup_required": True, "quarantined": True})
        if audit_error is not None:
            session.audit_broken = True
            session.lease.quarantine("com_write_audit_broken", audit_error, audit_broken=True)
            return mark_audit_failure(result, audit_error)
        return result

    def _write_confirmed(self, session: ComPortSession, data: bytes) -> int:
        """Write every byte, and return how many the handle confirmed.

        pyserial returns the number of bytes it accepted, and that number can be
        smaller than the payload without any exception being raised —
        `write_timeout_s` may be zero, which the schema permits and which makes
        a non-blocking write the normal case. A discarded return value therefore
        turns a truncated stimulus into a reported success, which is the shape a
        false green in a HIL run takes.

        The remainder is offered again until it is gone or the deadline passes,
        so an ordinary short write completes and a wedged port does not block
        the caller. A handle that reports no count at all is treated as having
        confirmed nothing: an unknown count is not a delivery."""
        deadline = time.monotonic() + max(session.port_config.write_timeout_s, PARTIAL_WRITE_COMPLETION_S)
        confirmed = 0
        while confirmed < len(data):
            written = session.serial_handle.write(data[confirmed:])
            if not isinstance(written, int) or isinstance(written, bool) or not 0 <= written <= len(data) - confirmed:
                return confirmed
            confirmed += written
            if confirmed >= len(data) or time.monotonic() >= deadline:
                break
            if written == 0:
                time.sleep(0.01)
        return confirmed

    def read(self, port_id: str, max_bytes: object | None = None, wait_timeout_s: object = 0.0) -> JsonObject:
        return self._write_report(self.read_bytes(port_id, max_bytes, wait_timeout_s, "com_read"))

    def read_bytes(self, port_id: str, max_bytes: object | None = None, wait_timeout_s: object = 0.0, tool: str = "com_read") -> JsonObject:
        port = self._configured_port(port_id, tool)
        if not port["ok"]:
            return port
        if not self.config.com_read_allowed(port["port_config"]):
            return self._permission_denied(tool, "Reading this COM port is disabled by the authoritative config.", port_id)
        session_result = self._active_session(port_id, tool)
        if not session_result["ok"]:
            return session_result
        session = session_result["session"]
        try:
            parsed_max_bytes = session.port_config.max_buffer_bytes if max_bytes is None else int(max_bytes)
            parsed_wait_timeout_s = float(wait_timeout_s)
        except (TypeError, ValueError):
            return {"ok": False, "tool": tool, "port_id": port_id, "error_type": "invalid_argument", "summary": "max_bytes must be an integer and wait_timeout_s must be a number."}
        if parsed_max_bytes < 1:
            return {"ok": False, "tool": tool, "port_id": port_id, "error_type": "invalid_argument", "summary": "max_bytes must be at least 1."}
        if not math.isfinite(parsed_wait_timeout_s):
            return {"ok": False, "tool": tool, "port_id": port_id, "error_type": "invalid_argument", "summary": "wait_timeout_s must be finite."}
        deadline = time.monotonic() + max(0.0, min(parsed_wait_timeout_s, 60.0))
        while self._session_is_active(session):
            with session.lock:
                if session.buffer:
                    break
            if time.monotonic() >= deadline:
                break
            time.sleep(0.01)
        with session.lock:
            data = bytes(session.buffer[:parsed_max_bytes])
            del session.buffer[:parsed_max_bytes]
            remaining = len(session.buffer)
        result: JsonObject = {"ok": True, "tool": tool, "port_id": port_id, "bytes_read": len(data), "buffer_remaining_bytes": remaining, "overflow_bytes": session.overflow_bytes, "data": data_result(data, session.port_config.encoding), "log_path": display_path(self.config, session.log_path), "summary": "Feedback read from COM port." if data else "No COM port feedback was available."}
        if session.reader_error:
            result["reader_error"] = session.reader_error
        return result

    def close(self) -> None:
        errors: list[tuple[str, BaseException]] = []
        interrupt: BaseException | None = None
        for port_id in list(self.sessions):
            try:
                result = self.session_stop(port_id)
                if not overall_success(result):
                    raise RuntimeError(str(result.get("summary", "COM port cleanup failed.")))
            except BaseException as error:
                errors.append((port_id, error))
                if interrupt is None and isinstance(error, (KeyboardInterrupt, SystemExit)):
                    interrupt = error
        for provisional_error in cleanup_provisional_handles(self.coordinator.owner_marker):
            errors.append(("provisional_handle", RuntimeError(provisional_error)))
        if self._owns_coordinator and not errors:
            self.coordinator.close()
        if interrupt is not None:
            interrupt.args = (*interrupt.args, "Cleanup errors: " + "; ".join(f"{port_id}: {type(error).__name__}: {error}" for port_id, error in errors))
            raise interrupt
        if errors:
            details = "; ".join(f"{port_id}: {type(error).__name__}: {error}" for port_id, error in errors)
            raise RuntimeError(f"COM port cleanup failed: {details}") from errors[0][1]

    def _open_serial(self, port_id: str, port_config: ComPortConfig, log_path: str, lease: HardwareLease) -> JsonObject:
        try:
            import serial
        except ImportError:
            return {"ok": False, "tool": "com_session_start", "port_id": port_id, "error_type": "serial_backend_not_available", "summary": "pyserial is not installed or could not be imported.", "likely_causes": ["install Agentic HIL with its runtime dependencies", "pyserial installation is broken"], "side_effect_committed": False}
        try:
            # Built unopened so the modem lines are decided BEFORE the port is
            # opened. Passing the device to the constructor opens it immediately
            # with pyserial's own defaults, which raise DTR and RTS — and on a
            # board that wires DTR to reset, listening to a target restarts it.
            # pyserial applies the requested states as part of open(); a driver
            # that pulses a line during the open itself is beyond what any host
            # can suppress, which is why listen_only and this pair are described
            # as evidence of intent, not as a hardware guarantee.
            serial_handle = serial.Serial()
            serial_handle.port = port_config.device
            serial_handle.baudrate = port_config.baudrate
            serial_handle.timeout = port_config.timeout_s
            serial_handle.write_timeout = port_config.write_timeout_s
            serial_handle.dtr = port_config.assert_dtr
            serial_handle.rts = port_config.assert_rts
            serial_handle.open()
            provisional = register_provisional_handle(self.coordinator.owner_marker, f"com:{port_id}", serial_handle.close)
            try:
                session = ComPortSession(port_id, port_config, serial_handle, log_path, lease, start_reader=False)
            except BaseException as primary_error:
                try:
                    serial_handle.close()
                except BaseException as cleanup_error:
                    # The raw handle stays registered so service.close() can retry
                    # closing it; do not discharge the provisional owner here.
                    raise RuntimeError(f"COM session construction failed and raw handle cleanup remains unconfirmed: {cleanup_error}") from primary_error
                discharge_provisional_handle(provisional)
                raise
            discharge_provisional_handle(provisional)
            return {"ok": True, "session": session}
        except Exception as error:
            return {"ok": False, "tool": "com_session_start", "port_id": port_id, "error_type": "com_port_open_failed", "summary": "COM port could not be opened.", "backend_error": str(error), "likely_causes": likely_causes("com_port_open_failed")}

    def _configured_port(self, port_id: str, tool: str) -> JsonObject:
        if not port_id:
            return {"ok": False, "tool": tool, "error_type": "invalid_argument", "summary": "port_id is required."}
        port_config = self.config.com_ports.get(port_id)
        if port_config is None:
            return {"ok": False, "tool": tool, "port_id": port_id, "error_type": "com_port_not_configured", "summary": "COM port is not available in the authoritative config.", "configured_ports": sorted(self.config.com_ports.keys())}
        return {"ok": True, "port_config": port_config}

    def _active_session(self, port_id: str, tool: str) -> JsonObject:
        port = self._configured_port(port_id, tool)
        if not port["ok"]:
            return port
        session = self.sessions.get(port_id)
        if session is None or self.coordinator.blocked or session.audit_broken or session.lease.state != "active" or not self._session_is_active(session):
            result: JsonObject = {"ok": False, "tool": tool, "port_id": port_id, "error_type": "session_not_active", "summary": "COM port session is not active. Start it with com_session_start first."}
            if session is not None and (self.coordinator.blocked or session.audit_broken or session.lease.state != "active"):
                result.update({"error_type": "resource_quarantined", "summary": "COM port requires cleanup or audit recovery before further actions.", "cleanup_required": True, "quarantined": True})
            if session is not None and session.reader_error:
                result["reader_error"] = session.reader_error
                result["summary"] = "COM port session failed and is no longer active. Start it again with com_session_start."
            return result
        return {"ok": True, "session": session}

    def _port_status(self, port_config: ComPortConfig, session: ComPortSession | None) -> JsonObject:
        # assert_dtr/assert_rts are reported because they decide whether merely
        # opening this port touches the target; a reader judging whether an
        # observation was passive needs to see them without opening the config.
        result: JsonObject = {"device": port_config.device, "baudrate": port_config.baudrate, "encoding": port_config.encoding, "max_buffer_bytes": port_config.max_buffer_bytes, "max_write_bytes": port_config.max_write_bytes, "assert_dtr": port_config.assert_dtr, "assert_rts": port_config.assert_rts, "session_active": False}
        if session is not None:
            result.update(self._session_status(session))
        return result

    def _session_status(self, session: ComPortSession) -> JsonObject:
        result: JsonObject = {"session_active": self._session_is_active(session), "started_at": session.started_at, "rx_buffer_bytes": len(session.buffer), "overflow_bytes": session.overflow_bytes, "log_path": display_path(self.config, session.log_path)}
        if session.reader_error:
            result["reader_error"] = session.reader_error
        return result

    def _session_is_active(self, session: ComPortSession) -> bool:
        if session.reader_error is not None:
            return False
        return session.active and bool(getattr(session.serial_handle, "is_open", True))

    def _clear_buffers(self, session: ComPortSession) -> JsonObject:
        reset_started = False
        try:
            with session.io_lock:
                reset = getattr(session.serial_handle, "reset_input_buffer", None)
                if callable(reset):
                    reset_started = True
                    reset()
                with session.lock:
                    session.buffer.clear()
                    session.overflow_bytes = 0
        except BaseException as error:
            result: JsonObject = {"ok": False, "tool": "com_session_start", "port_id": session.port_id, "error_type": "com_buffer_clear_failed", "summary": "COM input buffers could not be cleared.", "backend_error": str(error), "side_effect_committed": reset_started, "side_effect_status": "unknown" if reset_started else "not_started", "retry_safe": not reset_started}
            if reset_started:
                session.lease.quarantine("com_buffer_clear_unconfirmed", error)
                result.update({"cleanup_required": True, "quarantined": True})
            if not isinstance(error, Exception):
                raise
            return result
        return {"ok": True}

    def _stop_session(self, session: ComPortSession, reason: str, *, defer_release: bool = False) -> Exception | None:
        session.active = False
        errors: list[tuple[str, BaseException]] = []
        interrupt: BaseException | None = None
        cancel_read = getattr(session.serial_handle, "cancel_read", None)
        if callable(cancel_read):
            try:
                cancel_read()
            except BaseException as error:
                errors.append(("cancel_read", error))
                if isinstance(error, (KeyboardInterrupt, SystemExit)):
                    interrupt = error
        try:
            session.serial_handle.close()
        except BaseException as error:
            errors.append(("close", error))
            if interrupt is None and isinstance(error, (KeyboardInterrupt, SystemExit)):
                interrupt = error
        if session.reader is not None and session.reader is not threading.current_thread():
            try:
                session.reader.join(timeout=max(1.0, session.port_config.timeout_s + 0.5))
                if session.reader.is_alive():
                    raise RuntimeError("COM port reader remained active after close.")
            except BaseException as error:
                errors.append(("reader", error))
                if interrupt is None and isinstance(error, (KeyboardInterrupt, SystemExit)):
                    interrupt = error
        audit_error = append_jsonl_audited(self.config, session.log_path, {"event": "stop", "reason": reason})
        if audit_error is not None or session.audit_broken:
            session.audit_broken = True
            session.lease.quarantine("com_audit_broken", audit_error, audit_broken=True)
            errors.append(("audit", audit_error or RuntimeError("COM audit state was already broken.")))
        if errors:
            session.lease.quarantine("com_cleanup_unconfirmed", errors[0][1], audit_broken=session.audit_broken)
            details = "; ".join(f"{name}: {type(error).__name__}: {error}" for name, error in errors)
            if interrupt is not None:
                interrupt.args = (*interrupt.args, f"Cleanup errors: {details}")
                raise interrupt
            raise RuntimeError(details) from errors[0][1]
        if not defer_release and not session.lease.release():
            raise RuntimeError("COM resource release remains unconfirmed.")
        return audit_error

    def _close_failure(self, tool: str, port_id: str, error: Exception) -> JsonObject:
        return {
            "ok": False,
            "tool": tool,
            "port_id": port_id,
            "error_type": "com_port_close_failed",
            "summary": "COM port session could not be closed and remains registered for cleanup retry.",
            "backend_error": str(error),
        }

    def _write_report(self, result: JsonObject) -> JsonObject:
        prepared = mark_side_effect(result)
        port_id = prepared.get("port_id")
        session = self.sessions.get(port_id) if isinstance(port_id, str) else None
        unsafe_effect = prepared.get("side_effect_status") in {"unknown", "partial"}
        if session is not None and unsafe_effect:
            session.lease.quarantine("com_effect_unconfirmed")
        if session is not None:
            prepared = {**prepared, **session.lease.status()}
        written = write_report(self.config, prepared)
        if session is not None and written.get("audit_ok") is False:
            session.audit_broken = True
            session.lease.quarantine("com_report_audit_broken", audit_broken=True)
            written = write_report(self.config, {**written, **session.lease.status()})
        return written

    def _write_unattached_lease_report(self, result: JsonObject, lease: HardwareLease, *, release_if_safe: bool) -> JsonObject:
        written = write_report(self.config, {**mark_side_effect(result), **lease.status()})
        if written.get("audit_ok") is False:
            lease.quarantine("com_report_audit_broken", audit_broken=True)
            return write_report(self.config, {**written, **lease.status()})
        if release_if_safe and not lease.release():
            return write_report(self.config, {**written, **lease.status(), "ok": False, "cleanup_required": True, "summary": "COM lease release remained unconfirmed."})
        return recommit_report_with_status(self.config, written, lease.status())

    def _permission_denied(self, tool: str, summary: str, port_id: str | None = None) -> JsonObject:
        result: JsonObject = {"ok": False, "tool": tool, "error_type": "permission_denied", "summary": summary}
        if port_id:
            result["port_id"] = port_id
        return result


def payload_bytes(port_config: ComPortConfig, payload: JsonObject) -> JsonObject:
    if set(payload) - {"text", "hex"}:
        return {"ok": False, "tool": "com_write", "error_type": "invalid_argument", "summary": "COM write payload contains unsupported fields."}
    has_text = payload.get("text") is not None
    has_hex = payload.get("hex") is not None
    if has_text == has_hex:
        return {"ok": False, "tool": "com_write", "error_type": "invalid_argument", "summary": "Provide exactly one of text or hex."}
    if has_text:
        if not isinstance(payload.get("text"), str):
            return {"ok": False, "tool": "com_write", "error_type": "invalid_argument", "summary": "text must be a string."}
        try:
            return {"ok": True, "data": payload["text"].encode(port_config.encoding)}
        except LookupError:
            return {"ok": False, "tool": "com_write", "error_type": "config_invalid", "summary": "COM port encoding is not supported by Python.", "encoding": port_config.encoding}
        except UnicodeEncodeError:
            return {"ok": False, "tool": "com_write", "error_type": "invalid_argument", "summary": "text cannot be encoded with the configured COM port encoding.", "encoding": port_config.encoding}
    if not isinstance(payload.get("hex"), str):
        return {"ok": False, "tool": "com_write", "error_type": "invalid_argument", "summary": "hex must be a string."}
    cleaned = re.sub(r"\s+", "", payload["hex"])
    if len(cleaned) % 2 != 0 or re.fullmatch(r"[0-9a-fA-F]*", cleaned) is None:
        return {"ok": False, "tool": "com_write", "error_type": "invalid_argument", "summary": "hex must contain valid hexadecimal bytes."}
    return {"ok": True, "data": bytes.fromhex(cleaned)}


def data_result(data: bytes, encoding: str) -> JsonObject:
    return {"hex": data.hex(), "text": decode_bytes(data, encoding), "encoding": encoding}


def decode_bytes(data: bytes, encoding: str) -> str:
    try:
        return data.decode(encoding, errors="replace")
    except LookupError:
        return data.decode("utf-8", errors="replace")


def available_port_info(port_info: object) -> JsonObject:
    result: JsonObject = {"device": str(getattr(port_info, "device", "") or getattr(port_info, "name", ""))}
    for attr, output_name in [("name", "name"), ("description", "description"), ("hwid", "hwid"), ("manufacturer", "manufacturer"), ("product", "product"), ("interface", "interface"), ("location", "location"), ("serial_number", "serial_number")]:
        value = getattr(port_info, attr, None)
        if value is not None:
            result[output_name] = str(value)
    for attr, output_name in [("vid", "vid"), ("pid", "pid")]:
        value = getattr(port_info, attr, None)
        if value is not None:
            result[output_name] = value
    return result


def likely_causes(error_type: str) -> list[str]:
    return {
        "com_port_open_failed": ["configured COM port device does not exist", "COM port is already open in another program", "USB serial adapter is unplugged or driver is missing"],
        "serial_read_failed": ["COM port was disconnected", "serial driver reported an I/O error", "another process interfered with the port"],
        "serial_write_failed": ["COM port was disconnected", "serial driver write timed out", "target or USB serial adapter stopped responding"],
        "serial_write_incomplete": ["peer stopped reading and the driver buffer filled up", "write_timeout_s is too short for this payload at this baud rate", "flow control (RTS/CTS or XON/XOFF) is holding the line off"],
    }.get(error_type, ["inspect the COM port log for details"])
