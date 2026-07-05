from __future__ import annotations

import re
import threading
import time
from contextlib import suppress
from pathlib import Path

from hardci.config import display_path
from hardci.report import append_jsonl, logs_directory, safe_filename, timestamp_for_filename, utc_now_iso, write_report
from hardci.types import ComPortConfig, HardCIConfig, JsonObject


def list_available_com_ports(tool: str = "hardci_com_ports_available") -> JsonObject:
    try:
        from serial.tools import list_ports
    except ImportError:
        return {
            "ok": False,
            "tool": tool,
            "error_type": "serial_backend_not_available",
            "summary": "pyserial is not installed or could not be imported.",
            "likely_causes": ["install HardCI with its runtime dependencies", "pyserial installation is broken"],
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
    def __init__(self, port_id: str, port_config: ComPortConfig, serial_handle: object, log_path: str):
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


class ComPortService:
    def __init__(self, config: HardCIConfig):
        self.config = config
        self.sessions: dict[str, ComPortSession] = {}

    def list_ports(self) -> JsonObject:
        ports: JsonObject = {}
        for port_id, port_config in self.config.com_ports.items():
            ports[port_id] = self._port_status(port_config, self.sessions.get(port_id))
        available = list_available_com_ports()
        available_count = len(available.get("ports", [])) if available.get("ok") else 0
        return {
            "ok": True,
            "tool": "hardci_com_ports_list",
            "ports": ports,
            "available_com_ports": available,
            "summary": f"{len(ports)} configured COM port(s), {available_count} available host COM port(s).",
        }

    def session_start(self, port_id: str, clear_buffer: bool = True) -> JsonObject:
        port = self._configured_port(port_id, "hardci_com_session_start")
        if not port["ok"]:
            return self._write_report(port)
        if not self.config.permissions.allow_com_read:
            return self._write_report(self._permission_denied("hardci_com_session_start", "COM port reading is disabled by .hardci/config.yaml.", port_id))

        existing = self.sessions.get(port_id)
        if existing and self._session_is_active(existing):
            if clear_buffer:
                with existing.lock:
                    existing.buffer.clear()
            return self._write_report({"ok": True, "tool": "hardci_com_session_start", "port_id": port_id, "already_active": True, "session": self._session_status(existing), "summary": "COM port session is already active."})
        if existing:
            self.sessions.pop(port_id, None)

        opened = self._open_serial(port_id, port["port_config"])
        if not opened["ok"]:
            return self._write_report(opened)
        session = opened["session"]
        self.sessions[port_id] = session
        append_jsonl(session.log_path, {"event": "start", "port_id": port_id, "device": session.port_config.device})
        return self._write_report({"ok": True, "tool": "hardci_com_session_start", "port_id": port_id, "already_active": False, "session": self._session_status(session), "summary": "COM port session started."})

    def session_stop(self, port_id: str) -> JsonObject:
        port = self._configured_port(port_id, "hardci_com_session_stop")
        if not port["ok"]:
            return self._write_report(port)
        session = self.sessions.pop(port_id, None)
        if session is None:
            return self._write_report({"ok": True, "tool": "hardci_com_session_stop", "port_id": port_id, "was_active": False, "summary": "COM port session was not active."})
        self._stop_session(session, "requested")
        return self._write_report({"ok": True, "tool": "hardci_com_session_stop", "port_id": port_id, "was_active": True, "session": self._session_status(session), "summary": "COM port session stopped."})

    def write(self, port_id: str, payload: JsonObject) -> JsonObject:
        port = self._configured_port(port_id, "hardci_com_write")
        if not port["ok"]:
            return self._write_report(port)
        encoded = payload_bytes(port["port_config"], payload)
        if not encoded["ok"]:
            encoded["port_id"] = port_id
            return self._write_report(encoded)
        return self._write_report(self.write_bytes(port_id, encoded["data"], "hardci_com_write"))

    def write_bytes(self, port_id: str, data: bytes, tool: str = "hardci_com_write") -> JsonObject:
        if not self.config.permissions.allow_com_write:
            return self._permission_denied(tool, "COM port writing is disabled by .hardci/config.yaml.", port_id)
        session_result = self._active_session(port_id, tool)
        if not session_result["ok"]:
            return session_result
        session = session_result["session"]
        if len(data) > session.port_config.max_write_bytes:
            return {"ok": False, "tool": tool, "port_id": port_id, "error_type": "invalid_argument", "summary": "COM port write exceeds configured max_write_bytes.", "bytes_requested": len(data), "max_write_bytes": session.port_config.max_write_bytes}
        try:
            session.serial_handle.write(data)
            flush = getattr(session.serial_handle, "flush", None)
            if callable(flush):
                flush()
        except Exception as error:
            result = {"ok": False, "tool": tool, "port_id": port_id, "error_type": "serial_write_failed", "summary": "COM port write failed.", "backend_error": str(error), "likely_causes": likely_causes("serial_write_failed"), "log_path": display_path(self.config, session.log_path)}
            append_jsonl(session.log_path, {"event": "error", **result})
            return result
        append_jsonl(session.log_path, {"direction": "tx", "bytes": len(data), "hex": data.hex(), "text": decode_bytes(data, session.port_config.encoding)})
        return {"ok": True, "tool": tool, "port_id": port_id, "bytes_written": len(data), "data": data_result(data, session.port_config.encoding), "log_path": display_path(self.config, session.log_path), "summary": "Stimulus written to COM port."}

    def read(self, port_id: str, max_bytes: object | None = None, wait_timeout_s: object = 0.0) -> JsonObject:
        return self._write_report(self.read_bytes(port_id, max_bytes, wait_timeout_s, "hardci_com_read"))

    def read_bytes(self, port_id: str, max_bytes: object | None = None, wait_timeout_s: object = 0.0, tool: str = "hardci_com_read") -> JsonObject:
        if not self.config.permissions.allow_com_read:
            return self._permission_denied(tool, "COM port reading is disabled by .hardci/config.yaml.", port_id)
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
        sessions = list(self.sessions.values())
        self.sessions.clear()
        for session in sessions:
            self._stop_session(session, "shutdown")

    def _open_serial(self, port_id: str, port_config: ComPortConfig) -> JsonObject:
        try:
            import serial
        except ImportError:
            return {"ok": False, "tool": "hardci_com_session_start", "port_id": port_id, "error_type": "serial_backend_not_available", "summary": "pyserial is not installed or could not be imported.", "likely_causes": ["install HardCI with its runtime dependencies", "pyserial installation is broken"]}
        try:
            serial_handle = serial.Serial(port_config.device, port_config.baudrate, timeout=port_config.timeout_s, write_timeout=port_config.write_timeout_s)
            log_path = str(Path(logs_directory(self.config)) / f"com-{timestamp_for_filename()}-{safe_filename(port_id, 'port')}.jsonl")
            return {"ok": True, "session": ComPortSession(port_id, port_config, serial_handle, log_path)}
        except Exception as error:
            return {"ok": False, "tool": "hardci_com_session_start", "port_id": port_id, "error_type": "com_port_open_failed", "summary": "COM port could not be opened.", "backend_error": str(error), "likely_causes": likely_causes("com_port_open_failed")}

    def _configured_port(self, port_id: str, tool: str) -> JsonObject:
        if not port_id:
            return {"ok": False, "tool": tool, "error_type": "invalid_argument", "summary": "port_id is required."}
        port_config = self.config.com_ports.get(port_id)
        if port_config is None:
            return {"ok": False, "tool": tool, "port_id": port_id, "error_type": "com_port_not_configured", "summary": "COM port is not configured in .hardci/config.yaml.", "configured_ports": sorted(self.config.com_ports.keys())}
        return {"ok": True, "port_config": port_config}

    def _active_session(self, port_id: str, tool: str) -> JsonObject:
        port = self._configured_port(port_id, tool)
        if not port["ok"]:
            return port
        session = self.sessions.get(port_id)
        if session is None or not self._session_is_active(session):
            return {"ok": False, "tool": tool, "port_id": port_id, "error_type": "session_not_active", "summary": "COM port session is not active. Start it with hardci_com_session_start first."}
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
        return session.active and bool(getattr(session.serial_handle, "is_open", True))

    def _stop_session(self, session: ComPortSession, reason: str) -> None:
        session.active = False
        with suppress(Exception):
            session.serial_handle.close()
        append_jsonl(session.log_path, {"event": "stop", "reason": reason})

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
        return {"ok": False, "tool": "hardci_com_write", "error_type": "invalid_argument", "summary": "Provide exactly one of text or hex."}
    if has_text:
        if not isinstance(payload.get("text"), str):
            return {"ok": False, "tool": "hardci_com_write", "error_type": "invalid_argument", "summary": "text must be a string."}
        try:
            return {"ok": True, "data": payload["text"].encode(port_config.encoding)}
        except LookupError:
            return {"ok": False, "tool": "hardci_com_write", "error_type": "config_invalid", "summary": "COM port encoding is not supported by Python.", "encoding": port_config.encoding}
        except UnicodeEncodeError:
            return {"ok": False, "tool": "hardci_com_write", "error_type": "invalid_argument", "summary": "text cannot be encoded with the configured COM port encoding.", "encoding": port_config.encoding}
    if not isinstance(payload.get("hex"), str):
        return {"ok": False, "tool": "hardci_com_write", "error_type": "invalid_argument", "summary": "hex must be a string."}
    cleaned = re.sub(r"\s+", "", payload["hex"])
    if len(cleaned) % 2 != 0 or re.fullmatch(r"[0-9a-fA-F]*", cleaned) is None:
        return {"ok": False, "tool": "hardci_com_write", "error_type": "invalid_argument", "summary": "hex must contain valid hexadecimal bytes."}
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
