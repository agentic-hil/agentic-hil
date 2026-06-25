# Copyright 2026 Hannes Pauli
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import importlib
import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import AIHILConfig, ComPortConfig, display_path
from .report import logs_directory, timestamp_for_filename, utc_now_iso, write_report


def list_available_com_ports(tool: str = "aihil_com_ports_available") -> dict[str, Any]:
    try:
        list_ports_module = importlib.import_module("serial.tools.list_ports")
    except ImportError:
        return {
            "ok": False,
            "tool": tool,
            "error_type": "serial_backend_not_available",
            "summary": "pyserial is not installed or could not be imported.",
            "likely_causes": ["install AI-HIL with its runtime dependencies", "pyserial installation is broken"],
        }

    try:
        port_infos = list(list_ports_module.comports())
    except Exception as exc:  # pyserial may surface OS/backend-specific errors
        return {
            "ok": False,
            "tool": tool,
            "error_type": "com_port_discovery_failed",
            "summary": "Available COM ports could not be listed.",
            "backend_error": str(exc),
            "likely_causes": ["serial backend reported an OS error", "USB serial driver state changed during discovery"],
        }

    ports = [_available_port_info(port_info) for port_info in port_infos]
    return {
        "ok": True,
        "tool": tool,
        "ports": ports,
        "summary": f"{len(ports)} available COM port(s).",
    }


@dataclass
class ComPortSession:
    port_id: str
    port_config: ComPortConfig
    serial_handle: Any
    log_path: Path
    started_at: str
    stop_event: threading.Event = field(default_factory=threading.Event)
    lock: threading.Lock = field(default_factory=threading.Lock)
    log_lock: threading.Lock = field(default_factory=threading.Lock)
    buffer: bytearray = field(default_factory=bytearray)
    overflow_bytes: int = 0
    reader_error: dict[str, Any] | None = None
    thread: threading.Thread | None = None


class ComPortService:
    def __init__(self, config: AIHILConfig) -> None:
        self.config = config
        self._sessions: dict[str, ComPortSession] = {}
        self._lock = threading.Lock()

    def list_ports(self) -> dict[str, Any]:
        ports: dict[str, Any] = {}
        with self._lock:
            for port_id, port_config in self.config.com_ports.items():
                session = self._sessions.get(port_id)
                ports[port_id] = self._port_status(port_id, port_config, session)
        available = list_available_com_ports()
        available_count = len(available.get("ports", [])) if available.get("ok") else 0
        return {
            "ok": True,
            "tool": "aihil_com_ports_list",
            "ports": ports,
            "available_com_ports": available,
            "summary": f"{len(ports)} configured COM port(s), {available_count} available host COM port(s).",
        }

    def session_start(self, port_id: str, clear_buffer: bool = True) -> dict[str, Any]:
        port = self._configured_port(port_id, "aihil_com_session_start")
        if not port["ok"]:
            return self._write_report(port)
        if not self.config.permissions.allow_com_read:
            return self._write_report(
                self._permission_denied("aihil_com_session_start", "COM port reading is disabled by .aihil/config.yaml.", port_id)
            )

        with self._lock:
            existing = self._sessions.get(port_id)
            if existing and self._session_is_active(existing):
                if clear_buffer:
                    self._clear_session_buffer(existing)
                return self._write_report(
                    {
                        "ok": True,
                        "tool": "aihil_com_session_start",
                        "port_id": port_id,
                        "already_active": True,
                        "session": self._session_status(existing),
                        "summary": "COM port session is already active.",
                    }
                )
            if existing:
                self._sessions.pop(port_id, None)

            opened = self._open_serial(port_id, port["port_config"])
            if not opened["ok"]:
                return self._write_report(opened)

            session = opened["session"]
            thread = threading.Thread(target=self._reader_loop, args=(session,), name=f"aihil-com-{port_id}", daemon=True)
            session.thread = thread
            self._sessions[port_id] = session
            self._write_session_log(session, {"event": "start", "port_id": port_id, "device": session.port_config.device})
            thread.start()

        return self._write_report(
            {
                "ok": True,
                "tool": "aihil_com_session_start",
                "port_id": port_id,
                "already_active": False,
                "session": self._session_status(session),
                "summary": "COM port session started.",
            }
        )

    def session_stop(self, port_id: str) -> dict[str, Any]:
        port = self._configured_port(port_id, "aihil_com_session_stop")
        if not port["ok"]:
            return self._write_report(port)
        with self._lock:
            session = self._sessions.pop(port_id, None)
        if session is None:
            return self._write_report(
                {
                    "ok": True,
                    "tool": "aihil_com_session_stop",
                    "port_id": port_id,
                    "was_active": False,
                    "summary": "COM port session was not active.",
                }
            )

        self._stop_session(session, reason="requested")
        return self._write_report(
            {
                "ok": True,
                "tool": "aihil_com_session_stop",
                "port_id": port_id,
                "was_active": True,
                "session": self._session_status(session),
                "summary": "COM port session stopped.",
            }
        )

    def write(self, port_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        port = self._configured_port(port_id, "aihil_com_write")
        if not port["ok"]:
            return self._write_report(port)
        encoded = self._payload_bytes(port["port_config"], payload)
        if not encoded["ok"]:
            encoded["port_id"] = port_id
            return self._write_report(encoded)
        return self._write_report(self.write_bytes(port_id, encoded["data"], "aihil_com_write"))

    def write_bytes(self, port_id: str, data: bytes, tool: str = "aihil_com_write") -> dict[str, Any]:
        if not self.config.permissions.allow_com_write:
            return self._permission_denied(tool, "COM port writing is disabled by .aihil/config.yaml.", port_id)
        session_result = self._active_session(port_id, tool)
        if not session_result["ok"]:
            return session_result
        session = session_result["session"]
        if len(data) > session.port_config.max_write_bytes:
            return {
                "ok": False,
                "tool": tool,
                "port_id": port_id,
                "error_type": "invalid_argument",
                "summary": "COM port write exceeds configured max_write_bytes.",
                "bytes_requested": len(data),
                "max_write_bytes": session.port_config.max_write_bytes,
            }

        try:
            bytes_written = session.serial_handle.write(data)
            flush = getattr(session.serial_handle, "flush", None)
            if callable(flush):
                flush()
        except Exception as exc:  # pyserial raises several backend-specific exceptions
            result = {
                "ok": False,
                "tool": tool,
                "port_id": port_id,
                "error_type": "serial_write_failed",
                "summary": "COM port write failed.",
                "backend_error": str(exc),
                "likely_causes": self._likely_causes("serial_write_failed"),
                "log_path": display_path(self.config, session.log_path),
            }
            self._write_session_log(session, {"event": "error", **result})
            return result

        self._write_session_log(session, {"direction": "tx", "bytes": len(data), "hex": data.hex(), "text": self._decode(data, session.port_config.encoding)})
        return {
            "ok": True,
            "tool": tool,
            "port_id": port_id,
            "bytes_written": int(bytes_written) if bytes_written is not None else len(data),
            "data": self._data_result(data, session.port_config.encoding),
            "log_path": display_path(self.config, session.log_path),
            "summary": "Stimulus written to COM port.",
        }

    def read(self, port_id: str, max_bytes: Any = None, wait_timeout_s: Any = 0.0) -> dict[str, Any]:
        return self._write_report(self.read_bytes(port_id, max_bytes, wait_timeout_s, "aihil_com_read"))

    def read_bytes(
        self,
        port_id: str,
        max_bytes: Any = None,
        wait_timeout_s: Any = 0.0,
        tool: str = "aihil_com_read",
    ) -> dict[str, Any]:
        if not self.config.permissions.allow_com_read:
            return self._permission_denied(tool, "COM port reading is disabled by .aihil/config.yaml.", port_id)
        session_result = self._active_session(port_id, tool)
        if not session_result["ok"]:
            return session_result
        session = session_result["session"]

        try:
            max_bytes = session.port_config.max_buffer_bytes if max_bytes is None else int(max_bytes)
            wait_timeout_s = float(wait_timeout_s)
        except (TypeError, ValueError):
            return {
                "ok": False,
                "tool": tool,
                "port_id": port_id,
                "error_type": "invalid_argument",
                "summary": "max_bytes must be an integer and wait_timeout_s must be a number.",
            }
        if max_bytes < 1:
            return {
                "ok": False,
                "tool": tool,
                "port_id": port_id,
                "error_type": "invalid_argument",
                "summary": "max_bytes must be at least 1.",
            }
        wait_timeout_s = max(0.0, min(wait_timeout_s, 60.0))
        deadline = time.monotonic() + wait_timeout_s
        while self._session_buffer_size(session) == 0 and self._session_is_active(session) and time.monotonic() < deadline:
            time.sleep(0.01)

        data, remaining = self._consume_session_buffer(session, max_bytes)
        result: dict[str, Any] = {
            "ok": True,
            "tool": tool,
            "port_id": port_id,
            "bytes_read": len(data),
            "buffer_remaining_bytes": remaining,
            "overflow_bytes": session.overflow_bytes,
            "data": self._data_result(data, session.port_config.encoding),
            "log_path": display_path(self.config, session.log_path),
            "summary": "Feedback read from COM port." if data else "No COM port feedback was available.",
        }
        if session.reader_error:
            result["reader_error"] = session.reader_error
        return result

    def close(self) -> None:
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            self._stop_session(session, reason="shutdown")

    def active_session_status(self, port_id: str, tool: str = "aihil_com_session_status") -> dict[str, Any]:
        session_result = self._active_session(port_id, tool)
        if not session_result["ok"]:
            return session_result
        return {
            "ok": True,
            "tool": tool,
            "port_id": port_id,
            "session": self._session_status(session_result["session"]),
        }

    def _open_serial(self, port_id: str, port_config: ComPortConfig) -> dict[str, Any]:
        try:
            serial_module = importlib.import_module("serial")
        except ImportError:
            return {
                "ok": False,
                "tool": "aihil_com_session_start",
                "port_id": port_id,
                "error_type": "serial_backend_not_available",
                "summary": "pyserial is not installed or could not be imported.",
                "likely_causes": ["install AI-HIL with its runtime dependencies", "pyserial installation is broken"],
            }

        try:
            serial_handle = serial_module.serial_for_url(
                port_config.device,
                baudrate=port_config.baudrate,
                timeout=port_config.timeout_s,
                write_timeout=port_config.write_timeout_s,
            )
        except Exception as exc:
            return {
                "ok": False,
                "tool": "aihil_com_session_start",
                "port_id": port_id,
                "error_type": "com_port_open_failed",
                "summary": "COM port could not be opened.",
                "backend_error": str(exc),
                "likely_causes": self._likely_causes("com_port_open_failed"),
            }

        log_path = logs_directory(self.config) / f"com-{timestamp_for_filename()}-{_safe_filename(port_id)}.jsonl"
        return {
            "ok": True,
            "session": ComPortSession(
                port_id=port_id,
                port_config=port_config,
                serial_handle=serial_handle,
                log_path=log_path,
                started_at=utc_now_iso(),
            ),
        }

    def _reader_loop(self, session: ComPortSession) -> None:
        while not session.stop_event.is_set():
            try:
                waiting = int(getattr(session.serial_handle, "in_waiting", 0) or 0)
                data = session.serial_handle.read(max(1, waiting))
            except Exception as exc:
                if not session.stop_event.is_set():
                    error = {
                        "error_type": "serial_read_failed",
                        "summary": "COM port reader failed.",
                        "backend_error": str(exc),
                        "likely_causes": self._likely_causes("serial_read_failed"),
                    }
                    session.reader_error = error
                    self._write_session_log(session, {"event": "error", **error})
                break
            if not data:
                continue
            with session.lock:
                session.buffer.extend(data)
                overflow = len(session.buffer) - session.port_config.max_buffer_bytes
                if overflow > 0:
                    del session.buffer[:overflow]
                    session.overflow_bytes += overflow
            self._write_session_log(session, {"direction": "rx", "bytes": len(data), "hex": data.hex(), "text": self._decode(data, session.port_config.encoding)})

    def _active_session(self, port_id: str, tool: str) -> dict[str, Any]:
        port = self._configured_port(port_id, tool)
        if not port["ok"]:
            return port
        with self._lock:
            session = self._sessions.get(port_id)
        if session is None or not self._session_is_active(session):
            return {
                "ok": False,
                "tool": tool,
                "port_id": port_id,
                "error_type": "session_not_active",
                "summary": "COM port session is not active. Start it with aihil_com_session_start first.",
            }
        return {"ok": True, "session": session}

    def _configured_port(self, port_id: str, tool: str) -> dict[str, Any]:
        if not port_id:
            return {
                "ok": False,
                "tool": tool,
                "error_type": "invalid_argument",
                "summary": "port_id is required.",
            }
        port_config = self.config.com_ports.get(port_id)
        if port_config is None:
            return {
                "ok": False,
                "tool": tool,
                "port_id": port_id,
                "error_type": "com_port_not_configured",
                "summary": "COM port is not configured in .aihil/config.yaml.",
                "configured_ports": sorted(self.config.com_ports),
            }
        return {"ok": True, "port_config": port_config}

    def _port_status(self, port_id: str, port_config: ComPortConfig, session: ComPortSession | None) -> dict[str, Any]:
        result = {
            "device": port_config.device,
            "baudrate": port_config.baudrate,
            "encoding": port_config.encoding,
            "max_buffer_bytes": port_config.max_buffer_bytes,
            "max_write_bytes": port_config.max_write_bytes,
            "session_active": False,
        }
        if session is not None:
            result.update(self._session_status(session))
        return result

    def _session_status(self, session: ComPortSession) -> dict[str, Any]:
        result: dict[str, Any] = {
            "session_active": self._session_is_active(session),
            "started_at": session.started_at,
            "rx_buffer_bytes": self._session_buffer_size(session),
            "overflow_bytes": session.overflow_bytes,
            "log_path": display_path(self.config, session.log_path),
        }
        if session.reader_error:
            result["reader_error"] = session.reader_error
        return result

    def _session_is_active(self, session: ComPortSession) -> bool:
        return not session.stop_event.is_set() and session.thread is not None and session.thread.is_alive()

    def _session_buffer_size(self, session: ComPortSession) -> int:
        with session.lock:
            return len(session.buffer)

    def _clear_session_buffer(self, session: ComPortSession) -> None:
        with session.lock:
            session.buffer.clear()

    def _consume_session_buffer(self, session: ComPortSession, max_bytes: int) -> tuple[bytes, int]:
        with session.lock:
            data = bytes(session.buffer[:max_bytes])
            del session.buffer[:max_bytes]
            remaining = len(session.buffer)
        return data, remaining

    def _stop_session(self, session: ComPortSession, reason: str) -> None:
        session.stop_event.set()
        try:
            session.serial_handle.close()
        except Exception:
            pass
        if session.thread is not None and session.thread.is_alive():
            session.thread.join(timeout=max(1.0, session.port_config.timeout_s + 1.0))
        self._write_session_log(session, {"event": "stop", "reason": reason})

    def _payload_bytes(self, port_config: ComPortConfig, payload: dict[str, Any]) -> dict[str, Any]:
        has_text = "text" in payload and payload.get("text") is not None
        has_hex = "hex" in payload and payload.get("hex") is not None
        if has_text == has_hex:
            return {
                "ok": False,
                "tool": "aihil_com_write",
                "error_type": "invalid_argument",
                "summary": "Provide exactly one of text or hex.",
            }
        if has_text:
            text = payload.get("text")
            if not isinstance(text, str):
                return {
                    "ok": False,
                    "tool": "aihil_com_write",
                    "error_type": "invalid_argument",
                    "summary": "text must be a string.",
                }
            try:
                return {"ok": True, "data": text.encode(port_config.encoding)}
            except LookupError:
                return {
                    "ok": False,
                    "tool": "aihil_com_write",
                    "error_type": "config_invalid",
                    "summary": "COM port encoding is not supported by Python.",
                    "encoding": port_config.encoding,
                }
        hex_text = payload.get("hex")
        if not isinstance(hex_text, str):
            return {
                "ok": False,
                "tool": "aihil_com_write",
                "error_type": "invalid_argument",
                "summary": "hex must be a string.",
            }
        try:
            return {"ok": True, "data": bytes.fromhex(hex_text)}
        except ValueError:
            return {
                "ok": False,
                "tool": "aihil_com_write",
                "error_type": "invalid_argument",
                "summary": "hex must contain valid hexadecimal bytes.",
            }

    def _data_result(self, data: bytes, encoding: str) -> dict[str, Any]:
        return {
            "hex": data.hex(),
            "text": self._decode(data, encoding),
            "encoding": encoding,
        }

    def _decode(self, data: bytes, encoding: str) -> str:
        try:
            return data.decode(encoding, errors="replace")
        except LookupError:
            return data.decode("utf-8", errors="replace")

    def _write_session_log(self, session: ComPortSession, event: dict[str, Any]) -> None:
        entry = dict(event)
        entry.setdefault("time", utc_now_iso())
        with session.log_lock:
            with session.log_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, sort_keys=True) + "\n")

    def _write_report(self, result: dict[str, Any]) -> dict[str, Any]:
        return write_report(self.config, result)

    def _permission_denied(self, tool: str, summary: str, port_id: str | None = None) -> dict[str, Any]:
        result: dict[str, Any] = {
            "ok": False,
            "tool": tool,
            "error_type": "permission_denied",
            "summary": summary,
        }
        if port_id:
            result["port_id"] = port_id
        return result

    def _likely_causes(self, error_type: str) -> list[str]:
        causes = {
            "com_port_open_failed": [
                "configured COM port device does not exist",
                "COM port is already open in another program",
                "USB serial adapter is unplugged or driver is missing",
            ],
            "serial_read_failed": [
                "COM port was disconnected",
                "serial driver reported an I/O error",
                "another process interfered with the port",
            ],
            "serial_write_failed": [
                "COM port was disconnected",
                "serial driver write timed out",
                "target or USB serial adapter stopped responding",
            ],
        }
        return causes.get(error_type, ["inspect the COM port log for details"])


def _safe_filename(value: str) -> str:
    return "".join(char if char.isalnum() or char in {"-", "_", "."} else "_" for char in value) or "port"


def _available_port_info(port_info: Any) -> dict[str, Any]:
    result: dict[str, Any] = {"device": str(getattr(port_info, "device", ""))}
    for field_name in (
        "name",
        "description",
        "hwid",
        "manufacturer",
        "product",
        "interface",
        "location",
        "serial_number",
    ):
        value = getattr(port_info, field_name, None)
        if value is not None:
            result[field_name] = str(value)
    for field_name in ("vid", "pid"):
        value = getattr(port_info, field_name, None)
        if value is not None:
            try:
                result[field_name] = int(value)
            except (TypeError, ValueError):
                result[field_name] = str(value)
    return result
