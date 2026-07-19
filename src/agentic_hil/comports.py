from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from agentic_hil.config import display_path
from agentic_hil.report import (
    AuditWriteError,
    annotate_audit_error,
    append_jsonl,
    logs_directory,
    safe_filename,
    timestamp_for_filename,
    utc_now_iso,
    write_report,
)
from agentic_hil.types import AgenticHILConfig, ComPortConfig, JsonObject


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
    def __init__(self, port_id: str, port_config: ComPortConfig, serial_handle: object, log_path: str, start_reader: bool = True):
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
        self.reader: threading.Thread | None = None
        if start_reader:
            self.start_reader()

    def start_reader(self) -> None:
        if self.reader is None:
            self.reader = threading.Thread(target=self._reader_loop, daemon=True)
            self.reader.start()

    def _reader_loop(self) -> None:
        while self.active:
            try:
                waiting = int(getattr(self.serial_handle, "in_waiting", 0) or 0)
                data = self.serial_handle.read(waiting or 1)
                if not data:
                    continue
                chunk = bytes(data)
                with self.lock:
                    self.buffer.extend(chunk)
                    overflow = len(self.buffer) - self.port_config.max_buffer_bytes
                    if overflow > 0:
                        del self.buffer[:overflow]
                        self.overflow_bytes += overflow
                append_jsonl(self.log_path, {"direction": "rx", "bytes": len(chunk), "hex": chunk.hex(), "text": decode_bytes(chunk, self.port_config.encoding)})
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


@dataclass
class UnmanagedSerialHandle:
    port_id: str
    serial_handle: object
    cleanup_error: str


class ComPortService:
    def __init__(self, config: AgenticHILConfig):
        self.config = config
        self.sessions: dict[str, ComPortSession] = {}
        self._unmanaged_serial_handles: list[UnmanagedSerialHandle] = []

    def reconfigure(self, config: AgenticHILConfig) -> None:
        for port_id, session in list(self.sessions.items()):
            if not config.permissions.allow_com_read or config.com_ports.get(port_id) != session.port_config:
                self._stop_session(session, "config_reloaded")
                self.sessions.pop(port_id, None)
        self.config = config

    def list_ports(self) -> JsonObject:
        ports: JsonObject = {}
        for port_id, port_config in self.config.com_ports.items():
            ports[port_id] = self._port_status(port_config, self.sessions.get(port_id))
        if self.config.permissions.allow_com_read:
            available = list_available_com_ports()
        else:
            available = {
                "ok": False,
                "tool": "com_ports_available",
                "error_type": "permission_denied",
                "summary": "Host COM port discovery is disabled by .agentic-hil/config.yaml (allow_com_read).",
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
        port = self._configured_port(port_id, "com_session_start")
        if not port["ok"]:
            return self._write_report(port)
        if not self.config.permissions.allow_com_read:
            return self._write_report(self._permission_denied("com_session_start", "COM port reading is disabled by .agentic-hil/config.yaml.", port_id))

        existing = self.sessions.get(port_id)
        if existing and self._session_is_active(existing):
            if clear_buffer:
                reset_input_buffer = getattr(existing.serial_handle, "reset_input_buffer", None)
                if callable(reset_input_buffer):
                    try:
                        reset_input_buffer()
                    except Exception as error:
                        return self._write_report({"ok": False, "tool": "com_session_start", "port_id": port_id, "error_type": "serial_read_failed", "summary": "COM port input buffer could not be cleared before session use.", "backend_error": str(error), "likely_causes": likely_causes("serial_read_failed")})
                with existing.lock:
                    existing.buffer.clear()
                    existing.overflow_bytes = 0
            return self._write_report({"ok": True, "tool": "com_session_start", "port_id": port_id, "already_active": True, "session": self._session_status(existing), "summary": "COM port session is already active."})
        if existing:
            self._stop_session(existing, "restart")
            self.sessions.pop(port_id, None)

        opened = self._open_serial(port_id, port["port_config"])
        if not opened["ok"]:
            return self._write_report(opened)
        session = opened["session"]
        self.sessions[port_id] = session
        try:
            session.start_reader()
            append_jsonl(session.log_path, {"event": "start", "port_id": port_id, "device": session.port_config.device})
            return self._write_report({"ok": True, "tool": "com_session_start", "port_id": port_id, "already_active": False, "session": self._session_status(session), "summary": "COM port session started."})
        except BaseException as error:
            cleanup_confirmed = False
            try:
                self._stop_session(session, "start_failed")
            except BaseException:
                cleanup_confirmed = not bool(getattr(session.serial_handle, "is_open", True))
            else:
                self.sessions.pop(port_id, None)
                cleanup_confirmed = True
            if cleanup_confirmed:
                self.sessions.pop(port_id, None)
                error._agentic_hil_completion_confirmed = True
            raise

    def session_stop(self, port_id: str) -> JsonObject:
        port = self._configured_port(port_id, "com_session_stop")
        if not port["ok"]:
            return self._write_report(port)
        session = self.sessions.get(port_id)
        if session is None:
            try:
                return self._write_report({"ok": True, "tool": "com_session_stop", "port_id": port_id, "was_active": False, "summary": "COM port session was not active."})
            except BaseException as error:
                error._agentic_hil_completion_confirmed = True
                raise
        try:
            self._stop_session(session, "requested")
        except BaseException as error:
            if not bool(getattr(session.serial_handle, "is_open", True)):
                self.sessions.pop(port_id, None)
                error._agentic_hil_completion_confirmed = True
            raise
        self.sessions.pop(port_id, None)
        try:
            return self._write_report({"ok": True, "tool": "com_session_stop", "port_id": port_id, "was_active": True, "session": self._session_status(session), "summary": "COM port session stopped."})
        except BaseException as error:
            error._agentic_hil_completion_confirmed = True
            raise

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
        if not self.config.permissions.allow_com_write:
            return self._permission_denied(tool, "COM port writing is disabled by .agentic-hil/config.yaml.", port_id)
        session_result = self._active_session(port_id, tool)
        if not session_result["ok"]:
            return session_result
        session = session_result["session"]
        if len(data) > session.port_config.max_write_bytes:
            return {"ok": False, "tool": tool, "port_id": port_id, "error_type": "invalid_argument", "summary": "COM port write exceeds configured max_write_bytes.", "bytes_requested": len(data), "max_write_bytes": session.port_config.max_write_bytes}
        try:
            written = session.serial_handle.write(data)
            if not isinstance(written, int) or isinstance(written, bool):
                return {"ok": False, "tool": tool, "port_id": port_id, "error_type": "serial_write_failed", "completion_unconfirmed": True, "bytes_requested": len(data), "bytes_written": None, "summary": "Serial backend did not report a valid write count."}
            if written != len(data):
                return {"ok": False, "tool": tool, "port_id": port_id, "error_type": "serial_write_failed", "completion_unconfirmed": True, "bytes_requested": len(data), "bytes_written": written, "summary": "Serial write completed only partially."}
            flush = getattr(session.serial_handle, "flush", None)
            if callable(flush):
                flush()
        except Exception as error:
            result = {"ok": False, "tool": tool, "port_id": port_id, "error_type": "serial_write_failed", "summary": "COM port write failed.", "backend_error": str(error), "likely_causes": likely_causes("serial_write_failed"), "log_path": display_path(self.config, session.log_path)}
            try:
                append_jsonl(session.log_path, {"event": "error", **result})
            except AuditWriteError as audit_error:
                raise annotate_audit_error(audit_error, result) from audit_error
            return result
        result = {"ok": True, "tool": tool, "port_id": port_id, "bytes_written": len(data), "data": data_result(data, session.port_config.encoding), "log_path": display_path(self.config, session.log_path), "summary": "Stimulus written to COM port."}
        try:
            append_jsonl(session.log_path, {"direction": "tx", "bytes": len(data), "hex": data.hex(), "text": decode_bytes(data, session.port_config.encoding)})
        except AuditWriteError as audit_error:
            raise annotate_audit_error(audit_error, result) from audit_error
        return result

    def read(self, port_id: str, max_bytes: object | None = None, wait_timeout_s: object = 0.0) -> JsonObject:
        return self._write_report(self.read_bytes(port_id, max_bytes, wait_timeout_s, "com_read"))

    def read_bytes(self, port_id: str, max_bytes: object | None = None, wait_timeout_s: object = 0.0, tool: str = "com_read") -> JsonObject:
        if not self.config.permissions.allow_com_read:
            return self._permission_denied(tool, "COM port reading is disabled by .agentic-hil/config.yaml.", port_id)
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
        first_error: Exception | None = None
        pending_base_exception: BaseException | None = None
        for item in list(self._unmanaged_serial_handles):
            try:
                item.serial_handle.close()
                if bool(getattr(item.serial_handle, "is_open", False)):
                    raise RuntimeError("COM port remained open after close.")
            except BaseException as error:
                if isinstance(error, Exception) and first_error is None:
                    first_error = error
                elif not isinstance(error, Exception) and pending_base_exception is None:
                    pending_base_exception = error
            else:
                self._unmanaged_serial_handles.remove(item)
        for port_id, session in list(self.sessions.items()):
            try:
                self._stop_session(session, "shutdown")
            except BaseException as error:
                if isinstance(error, Exception) and first_error is None:
                    first_error = error
                elif not isinstance(error, Exception) and pending_base_exception is None:
                    pending_base_exception = error
            else:
                self.sessions.pop(port_id, None)
        if pending_base_exception is not None:
            raise pending_base_exception
        if first_error is not None:
            raise first_error

    def has_active_sessions(self) -> bool:
        return bool(self.active_session_ids())

    def active_session_ids(self) -> list[str]:
        active: list[str] = []
        for item in self._unmanaged_serial_handles:
            try:
                if bool(getattr(item.serial_handle, "is_open", True)):
                    active.append(item.port_id)
            except Exception:
                active.append(item.port_id)
        for port_id, session in self.sessions.items():
            try:
                if bool(getattr(session.serial_handle, "is_open", True)):
                    active.append(port_id)
            except Exception:
                active.append(port_id)
        return active

    def cleanup_inspection_errors(self) -> list[JsonObject]:
        errors: list[JsonObject] = []
        for item in self._unmanaged_serial_handles:
            try:
                may_be_open = bool(getattr(item.serial_handle, "is_open", True))
            except Exception:
                may_be_open = True
            if may_be_open:
                errors.append({"id": item.port_id, "error": item.cleanup_error, "summary": "COM handle remained open after failed session initialization."})
        return errors

    def _open_serial(self, port_id: str, port_config: ComPortConfig) -> JsonObject:
        try:
            import serial
        except ImportError:
            return {"ok": False, "tool": "com_session_start", "port_id": port_id, "error_type": "serial_backend_not_available", "summary": "pyserial is not installed or could not be imported.", "likely_causes": ["install Agentic HIL with its runtime dependencies", "pyserial installation is broken"]}
        try:
            log_path = str(Path(logs_directory(self.config)) / f"com-{timestamp_for_filename()}-{safe_filename(port_id, 'port')}.jsonl")
            serial_handle = serial.Serial(port_config.device, port_config.baudrate, timeout=port_config.timeout_s, write_timeout=port_config.write_timeout_s)
        except Exception as error:
            return {"ok": False, "tool": "com_session_start", "port_id": port_id, "error_type": "com_port_open_failed", "summary": "COM port could not be opened.", "backend_error": str(error), "likely_causes": likely_causes("com_port_open_failed")}
        try:
            session = ComPortSession(port_id, port_config, serial_handle, log_path, start_reader=False)
        except BaseException as error:
            result: JsonObject = {"ok": False, "tool": "com_session_start", "port_id": port_id, "error_type": "com_port_open_failed", "summary": "COM port session could not be initialized.", "backend_error": str(error), "likely_causes": likely_causes("com_port_open_failed")}
            cleanup_confirmed = False
            try:
                serial_handle.close()
                if bool(getattr(serial_handle, "is_open", False)):
                    raise RuntimeError("COM port remained open after close.")
                cleanup_confirmed = True
            except BaseException as cleanup_error:
                self._unmanaged_serial_handles.append(UnmanagedSerialHandle(port_id, serial_handle, str(cleanup_error)))
                result["cleanup_error"] = str(cleanup_error)
                result["hardware_state_unconfirmed"] = True
                result["completion_unconfirmed"] = True
                if not isinstance(cleanup_error, Exception):
                    raise
            if not isinstance(error, Exception):
                if cleanup_confirmed:
                    error._agentic_hil_completion_confirmed = True
                raise
            return result
        return {"ok": True, "session": session}

    def _configured_port(self, port_id: str, tool: str) -> JsonObject:
        if not port_id:
            return {"ok": False, "tool": tool, "error_type": "invalid_argument", "summary": "port_id is required."}
        port_config = self.config.com_ports.get(port_id)
        if port_config is None:
            return {"ok": False, "tool": tool, "port_id": port_id, "error_type": "com_port_not_configured", "summary": "COM port is not configured in .agentic-hil/config.yaml.", "configured_ports": sorted(self.config.com_ports.keys())}
        return {"ok": True, "port_config": port_config}

    def _active_session(self, port_id: str, tool: str) -> JsonObject:
        port = self._configured_port(port_id, tool)
        if not port["ok"]:
            return port
        session = self.sessions.get(port_id)
        if session is None or not self._session_is_active(session):
            result: JsonObject = {"ok": False, "tool": tool, "port_id": port_id, "error_type": "session_not_active", "summary": "COM port session is not active. Start it with com_session_start first."}
            if session is not None and session.reader_error:
                result["reader_error"] = session.reader_error
                result["summary"] = "COM port session failed and is no longer active. Start it again with com_session_start."
            return result
        return {"ok": True, "session": session}

    def _port_status(self, port_config: ComPortConfig, session: ComPortSession | None) -> JsonObject:
        result: JsonObject = {"device": port_config.device, "baudrate": port_config.baudrate, "encoding": port_config.encoding, "max_buffer_bytes": port_config.max_buffer_bytes, "max_write_bytes": port_config.max_write_bytes, "session_active": False}
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

    def _stop_session(self, session: ComPortSession, reason: str) -> None:
        session.active = False
        session.serial_handle.close()
        if bool(getattr(session.serial_handle, "is_open", False)):
            raise RuntimeError("COM port remained open after close.")
        try:
            append_jsonl(session.log_path, {"event": "stop", "reason": reason})
        except AuditWriteError as error:
            if error.completion_state == "unknown":
                error.completion_state = "confirmed"
            raise

    def _write_report(self, result: JsonObject) -> JsonObject:
        return write_report(self.config, result)

    def _permission_denied(self, tool: str, summary: str, port_id: str | None = None) -> JsonObject:
        result: JsonObject = {"ok": False, "tool": tool, "error_type": "permission_denied", "summary": summary}
        if port_id:
            result["port_id"] = port_id
        return result


def payload_bytes(port_config: ComPortConfig, payload: JsonObject) -> JsonObject:
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
    }.get(error_type, ["inspect the COM port log for details"])
