from __future__ import annotations

import os
import re
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from agentic_hil.backends.common import command_for_log, invocation
from agentic_hil.bridge import BridgeCloseResult, ProcessBridgeSession, public_backend_result, reap_unmanaged_child
from agentic_hil.config import display_path, resolve_work_path
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
from agentic_hil.types import AgenticHILConfig, CanBusConfig, JsonObject

SUPPORTED_CAN_ADAPTERS = ["peak", "socketcan", "process"]
CAN_FD_VALID_DATA_LENGTHS = frozenset([0, 1, 2, 3, 4, 5, 6, 7, 8, 12, 16, 20, 24, 32, 48, 64])


@dataclass(frozen=True)
class CanFrame:
    id: int
    extended: bool
    rtr: bool
    data: bytes


class CanAdapterSession(Protocol):
    adapter_name: str

    def send(self, frame: CanFrame) -> JsonObject: ...

    def read(self, max_frames: int, wait_timeout_s: float) -> JsonObject: ...

    def close(self) -> object: ...

    def status(self) -> JsonObject: ...


class CanBusSession:
    def __init__(self, bus_id: str, bus_config: CanBusConfig, adapter_session: CanAdapterSession, log_path: str):
        self.bus_id = bus_id
        self.bus_config = bus_config
        self.adapter_session = adapter_session
        self.log_path = log_path
        self.started_at = utc_now_iso()
        self.active = True
        self.cleanup_unconfirmed = False
        self.close_confirmed = False


class CanBusService:
    def __init__(self, config: AgenticHILConfig):
        self.config = config
        self.sessions: dict[str, CanBusSession] = {}

    def reconfigure(self, config: AgenticHILConfig) -> None:
        for bus_id, session in list(self.sessions.items()):
            permissions_revoked = not config.permissions.allow_can_read and not config.permissions.allow_can_write
            if permissions_revoked or config.can_buses.get(bus_id) != session.bus_config:
                self._stop_session(session, "config_reloaded")
                self.sessions.pop(bus_id, None)
        self.config = config

    def list_buses(self) -> JsonObject:
        buses = {bus_id: self._bus_status(bus_config, self.sessions.get(bus_id)) for bus_id, bus_config in self.config.can_buses.items()}
        return {"ok": True, "tool": "can_buses_list", "buses": buses, "supported_adapters": SUPPORTED_CAN_ADAPTERS, "summary": f"{len(buses)} configured CAN bus(es)."}

    def session_start(self, bus_id: str, clear_rx_queue: bool = True) -> JsonObject:
        bus = self._configured_bus(bus_id, "can_session_start")
        if not bus["ok"]:
            return self._write_report(bus)
        if not self.config.permissions.allow_can_read and not self.config.permissions.allow_can_write:
            return self._write_report(self._permission_denied("can_session_start", "CAN reading and writing are disabled by .agentic-hil/config.yaml.", bus_id))
        existing = self.sessions.get(bus_id)
        if existing and self._session_is_active(existing):
            if clear_rx_queue and self.config.permissions.allow_can_read:
                cleared = existing.adapter_session.read(existing.bus_config.max_buffer_frames, 0)
                if not isinstance(cleared, dict) or cleared.get("ok") is not True:
                    return self._write_report(queue_clear_failure(bus_id, existing, cleared))
            return self._write_report({"ok": True, "tool": "can_session_start", "bus_id": bus_id, "already_active": True, "session": self._session_status(existing), "summary": "CAN bus session is already active."})
        if existing:
            self._stop_session(existing, "restart")
            self.sessions.pop(bus_id, None)
        bus_config = bus["bus_config"]
        log_path = str(Path(logs_directory(self.config)) / f"can-{timestamp_for_filename()}-{safe_filename(bus_id, 'bus')}.jsonl")
        provisional: CanBusSession | None = None

        def register_provisional(adapter_session: ProcessCanAdapterSession) -> None:
            nonlocal provisional
            provisional = CanBusSession(bus_id, bus_config, adapter_session, log_path)
            self.sessions[bus_id] = provisional

        try:
            opened = open_adapter(self.config, bus_id, bus_config, clear_rx_queue, register_provisional)
        except BaseException as error:
            if provisional is not None:
                provisional.active = False
                if provisional.bus_config.adapter == "process":
                    close_result = getattr(provisional.adapter_session, "last_close_result", None)
                    provisional.close_confirmed = isinstance(close_result, BridgeCloseResult) and close_result.cleanup_confirmed
                else:
                    try:
                        provisional.close_confirmed = provisional.adapter_session.status().get("active") is False
                    except BaseException:
                        provisional.close_confirmed = False
                provisional.cleanup_unconfirmed = not provisional.close_confirmed
                if not provisional.cleanup_unconfirmed:
                    self.sessions.pop(bus_id, None)
                    error._agentic_hil_completion_confirmed = True
            raise
        if not opened["ok"]:
            failed_adapter = opened.get("session")
            if provisional is not None:
                provisional.active = False
                provisional.cleanup_unconfirmed = bool(opened.get("cleanup_unconfirmed"))
                provisional.close_confirmed = not provisional.cleanup_unconfirmed
                if not provisional.cleanup_unconfirmed:
                    self.sessions.pop(bus_id, None)
            elif failed_adapter is not None:
                failed_session = CanBusSession(bus_id, bus_config, failed_adapter, log_path)
                failed_session.cleanup_unconfirmed = bool(opened.get("cleanup_unconfirmed"))
                self.sessions[bus_id] = failed_session
            return self._write_report(public_backend_result(opened))
        if provisional is None:
            adapter_session = opened["session"]
            provisional = CanBusSession(bus_id, bus_config, adapter_session, log_path)
            self.sessions[bus_id] = provisional
        session = provisional
        adapter_session = session.adapter_session
        try:
            if clear_rx_queue and self.config.permissions.allow_can_read:
                cleared = adapter_session.read(bus_config.max_buffer_frames, 0)
                if not isinstance(cleared, dict) or cleared.get("ok") is not True:
                    result = queue_clear_failure(bus_id, session, cleared)
                    try:
                        self._stop_session(session, "queue_clear_failed")
                    except BaseException as cleanup_error:
                        result.update({"completion_unconfirmed": True, "hardware_state_unconfirmed": True, "cleanup_error": str(cleanup_error)})
                        if not isinstance(cleanup_error, Exception):
                            raise
                    else:
                        self.sessions.pop(bus_id, None)
                        result.pop("completion_unconfirmed", None)
                        result.pop("hardware_state_unconfirmed", None)
                        result["completion_confirmed"] = True
                    return self._write_report(result)
            append_jsonl(session.log_path, {"event": "start", "bus_id": bus_id, "adapter": bus_config.adapter, "channel": bus_config.channel, "bitrate": bus_config.bitrate})
            return self._write_report({"ok": True, "tool": "can_session_start", "bus_id": bus_id, "already_active": False, "adapter": adapter_session.adapter_name, "adapter_result": public_backend_result(opened), "session": self._session_status(session), "summary": "CAN bus session started."})
        except BaseException as error:
            cleanup_confirmed = False
            try:
                self._stop_session(session, "start_failed")
            except BaseException:
                cleanup_confirmed = session.close_confirmed
            else:
                self.sessions.pop(bus_id, None)
                cleanup_confirmed = True
            if cleanup_confirmed:
                self.sessions.pop(bus_id, None)
                error._agentic_hil_completion_confirmed = True
            raise

    def session_stop(self, bus_id: str) -> JsonObject:
        bus = self._configured_bus(bus_id, "can_session_stop")
        if not bus["ok"]:
            return self._write_report(bus)
        session = self.sessions.get(bus_id)
        if session is None:
            try:
                return self._write_report({"ok": True, "tool": "can_session_stop", "bus_id": bus_id, "was_active": False, "summary": "CAN bus session was not active."})
            except BaseException as error:
                error._agentic_hil_completion_confirmed = True
                raise
        try:
            self._stop_session(session, "requested")
        except BaseException as error:
            if session.close_confirmed:
                self.sessions.pop(bus_id, None)
                error._agentic_hil_completion_confirmed = True
            raise
        self.sessions.pop(bus_id, None)
        try:
            return self._write_report({"ok": True, "tool": "can_session_stop", "bus_id": bus_id, "was_active": True, "session": self._session_status(session), "summary": "CAN bus session stopped."})
        except BaseException as error:
            error._agentic_hil_completion_confirmed = True
            raise

    def send(self, bus_id: str, payload: JsonObject) -> JsonObject:
        bus = self._configured_bus(bus_id, "can_send")
        if not bus["ok"]:
            return self._write_report(bus)
        if not self.config.permissions.allow_can_write:
            return self._write_report(self._permission_denied("can_send", "CAN writing is disabled by .agentic-hil/config.yaml.", bus_id))
        session_result = self._active_session(bus_id, "can_send")
        if not session_result["ok"]:
            return self._write_report(session_result)
        session = session_result["session"]
        parsed = payload_frame(session.bus_config, payload)
        if not parsed["ok"]:
            parsed["bus_id"] = bus_id
            return self._write_report(parsed)
        frame = parsed["frame"]
        sent = session.adapter_session.send(frame)
        if not sent["ok"]:
            result = {"tool": "can_send", "bus_id": bus_id, "adapter": session.adapter_session.adapter_name, "frame": frame_result(frame), "log_path": display_path(self.config, session.log_path), **sent}
            try:
                append_jsonl(session.log_path, {"event": "error", "direction": "tx", **result})
            except AuditWriteError as audit_error:
                raise annotate_audit_error(audit_error, result) from audit_error
            return self._write_report(result)
        result = {"ok": True, "tool": "can_send", "bus_id": bus_id, "adapter": session.adapter_session.adapter_name, "frame": frame_result(frame), "adapter_result": public_backend_result(sent), "log_path": display_path(self.config, session.log_path), "summary": "CAN frame sent."}
        try:
            append_jsonl(session.log_path, {"direction": "tx", **result})
        except AuditWriteError as audit_error:
            raise annotate_audit_error(audit_error, result) from audit_error
        return self._write_report(result)

    def read(self, bus_id: str, max_frames: object | None = None, wait_timeout_s: object = 0.0) -> JsonObject:
        bus = self._configured_bus(bus_id, "can_read")
        if not bus["ok"]:
            return self._write_report(bus)
        if not self.config.permissions.allow_can_read:
            return self._write_report(self._permission_denied("can_read", "CAN reading is disabled by .agentic-hil/config.yaml.", bus_id))
        session_result = self._active_session(bus_id, "can_read")
        if not session_result["ok"]:
            return self._write_report(session_result)
        session = session_result["session"]
        try:
            parsed_max_frames = session.bus_config.max_buffer_frames if max_frames is None else int(max_frames)
            parsed_wait_timeout_s = float(wait_timeout_s)
        except (TypeError, ValueError):
            return self._write_report({"ok": False, "tool": "can_read", "bus_id": bus_id, "error_type": "invalid_argument", "summary": "max_frames must be an integer and wait_timeout_s must be a number."})
        if parsed_max_frames < 1 or parsed_max_frames > session.bus_config.max_buffer_frames:
            return self._write_report({"ok": False, "tool": "can_read", "bus_id": bus_id, "error_type": "invalid_argument", "summary": "max_frames must be between 1 and configured max_buffer_frames.", "max_buffer_frames": session.bus_config.max_buffer_frames})
        read = session.adapter_session.read(parsed_max_frames, max(0.0, min(parsed_wait_timeout_s, 60.0)))
        if not read["ok"]:
            result = {"tool": "can_read", "bus_id": bus_id, "adapter": session.adapter_session.adapter_name, "log_path": display_path(self.config, session.log_path), **read}
            try:
                append_jsonl(session.log_path, {"event": "error", "direction": "rx", **result})
            except AuditWriteError as audit_error:
                raise annotate_audit_error(audit_error, result) from audit_error
            return self._write_report(result)
        normalized = normalize_received_frames(read.get("frames", []), session.bus_config)
        if not normalized["ok"]:
            result = {"tool": "can_read", "bus_id": bus_id, "adapter": session.adapter_session.adapter_name, "log_path": display_path(self.config, session.log_path), **normalized}
            try:
                append_jsonl(session.log_path, {"event": "error", "direction": "rx", **result})
            except AuditWriteError as audit_error:
                raise annotate_audit_error(audit_error, result) from audit_error
            return self._write_report(result)
        frames = normalized["frames"]
        result = {"ok": True, "tool": "can_read", "bus_id": bus_id, "adapter": session.adapter_session.adapter_name, "frames_read": len(frames), "frames": frames, "adapter_result": public_backend_result(read, ["frames"]), "log_path": display_path(self.config, session.log_path), "summary": "CAN frame(s) read." if frames else "No CAN frames were available."}
        try:
            append_jsonl(session.log_path, {"direction": "rx", **result})
        except AuditWriteError as audit_error:
            raise annotate_audit_error(audit_error, result) from audit_error
        return self._write_report(result)

    def close(self) -> None:
        first_error: Exception | None = None
        pending_base_exception: BaseException | None = None
        for bus_id, session in list(self.sessions.items()):
            try:
                self._stop_session(session, "shutdown")
            except BaseException as error:
                if isinstance(error, Exception) and first_error is None:
                    first_error = error
                elif not isinstance(error, Exception) and pending_base_exception is None:
                    pending_base_exception = error
                if session.close_confirmed:
                    self.sessions.pop(bus_id, None)
            else:
                self.sessions.pop(bus_id, None)
        if pending_base_exception is not None:
            raise pending_base_exception
        if first_error is not None:
            raise first_error

    def has_active_sessions(self) -> bool:
        return bool(self.active_session_ids())

    def active_session_ids(self) -> list[str]:
        return [bus_id for bus_id, session in self.sessions.items() if not session.close_confirmed]

    def cleanup_inspection_errors(self) -> list[JsonObject]:
        errors: list[JsonObject] = []
        for bus_id, session in self.sessions.items():
            if session.close_confirmed:
                continue
            try:
                adapter_active = session.adapter_session.status().get("active") is not False
            except Exception as error:
                session.cleanup_unconfirmed = True
                errors.append({"id": bus_id, "error": f"CAN adapter status could not be inspected: {error}"})
                continue
            close_result = getattr(session.adapter_session, "last_close_result", None)
            if session.cleanup_unconfirmed:
                errors.append({"id": bus_id, "error": "Physical safe state was not confirmed during CAN adapter cleanup."})
            elif not adapter_active and (not isinstance(close_result, BridgeCloseResult) or not close_result.cleanup_confirmed):
                session.cleanup_unconfirmed = True
                errors.append({"id": bus_id, "error": "CAN adapter ended without confirmed safe shutdown."})
        return errors

    def _configured_bus(self, bus_id: str, tool: str) -> JsonObject:
        if not bus_id:
            return {"ok": False, "tool": tool, "error_type": "invalid_argument", "summary": "bus_id is required."}
        bus_config = self.config.can_buses.get(bus_id)
        if bus_config is None:
            return {"ok": False, "tool": tool, "bus_id": bus_id, "error_type": "can_bus_not_configured", "summary": "CAN bus is not configured in .agentic-hil/config.yaml.", "configured_buses": sorted(self.config.can_buses.keys())}
        return {"ok": True, "bus_config": bus_config}

    def _active_session(self, bus_id: str, tool: str) -> JsonObject:
        bus = self._configured_bus(bus_id, tool)
        if not bus["ok"]:
            return bus
        session = self.sessions.get(bus_id)
        if session is None:
            return {"ok": False, "tool": tool, "bus_id": bus_id, "error_type": "session_not_active", "summary": "CAN bus session is not active. Start it with can_session_start first."}
        if not self._session_is_active(session):
            if session.cleanup_unconfirmed:
                return {"ok": False, "tool": tool, "bus_id": bus_id, "error_type": "hardware_state_unconfirmed", "summary": "CAN adapter ended without confirmed safe shutdown."}
            return {"ok": False, "tool": tool, "bus_id": bus_id, "error_type": "session_not_active", "summary": "CAN bus session is not active. Start it with can_session_start first."}
        return {"ok": True, "session": session}

    def _bus_status(self, bus_config: CanBusConfig, session: CanBusSession | None) -> JsonObject:
        result: JsonObject = {"adapter": bus_config.adapter, "channel": bus_config.channel, "bitrate": bus_config.bitrate, "fd": bus_config.fd, "max_buffer_frames": bus_config.max_buffer_frames, "max_frame_data_bytes": bus_config.max_frame_data_bytes, "session_active": False}
        if session is not None:
            result.update(self._session_status(session))
        return result

    def _session_status(self, session: CanBusSession) -> JsonObject:
        return {"session_active": self._session_is_active(session), "cleanup_unconfirmed": session.cleanup_unconfirmed, "close_confirmed": session.close_confirmed, "started_at": session.started_at, "adapter": session.adapter_session.adapter_name, "adapter_status": session.adapter_session.status(), "log_path": display_path(self.config, session.log_path)}

    def _session_is_active(self, session: CanBusSession) -> bool:
        if session.close_confirmed or session.cleanup_unconfirmed or not session.active:
            return False
        try:
            adapter_active = session.adapter_session.status().get("active") is not False
        except Exception:
            session.cleanup_unconfirmed = True
            return False
        if not adapter_active:
            close_result = getattr(session.adapter_session, "last_close_result", None)
            if not isinstance(close_result, BridgeCloseResult) or not close_result.cleanup_confirmed:
                session.cleanup_unconfirmed = True
            return False
        return True

    def _stop_session(self, session: CanBusSession, reason: str) -> None:
        session.active = False
        try:
            close_result = session.adapter_session.close()
        except BaseException:
            close_result = getattr(session.adapter_session, "last_close_result", None)
            session.close_confirmed = isinstance(close_result, BridgeCloseResult) and close_result.cleanup_confirmed
            session.cleanup_unconfirmed = not session.close_confirmed
            raise
        if session.bus_config.adapter == "process" and (not isinstance(close_result, BridgeCloseResult) or not close_result.cleanup_confirmed):
            session.cleanup_unconfirmed = True
            errors = close_result.errors if isinstance(close_result, BridgeCloseResult) else ["process adapter returned no structured close result"]
            raise RuntimeError("CAN adapter cleanup did not confirm a physical safe state: " + "; ".join(errors))
        if session.adapter_session.status().get("active") is not False:
            session.cleanup_unconfirmed = True
            raise RuntimeError("CAN adapter remained active after close.")
        session.close_confirmed = True
        try:
            append_jsonl(session.log_path, {"event": "stop", "reason": reason})
        except AuditWriteError as error:
            if error.completion_state == "unknown":
                error.completion_state = "confirmed"
            raise

    def _write_report(self, result: JsonObject) -> JsonObject:
        return write_report(self.config, result)

    def _permission_denied(self, tool: str, summary: str, bus_id: str | None = None) -> JsonObject:
        result: JsonObject = {"ok": False, "tool": tool, "error_type": "permission_denied", "summary": summary}
        if bus_id:
            result["bus_id"] = bus_id
        return result


def open_adapter(
    config: AgenticHILConfig,
    bus_id: str,
    bus_config: CanBusConfig,
    clear_rx_queue: bool,
    on_started: Callable[[CanAdapterSession], None] | None = None,
) -> JsonObject:
    if bus_config.adapter == "process":
        return open_process_adapter(config, bus_id, bus_config, clear_rx_queue, on_started)
    return open_python_can_adapter(config, bus_id, bus_config, clear_rx_queue, on_started)


class PythonCanAdapterSession:
    def __init__(self, adapter_name: str, bus: object, is_fd: bool = False, bitrate_switch: bool = False):
        self.adapter_name = adapter_name
        self.bus = bus
        self.is_fd = is_fd
        self.bitrate_switch = bitrate_switch
        self.active = True

    def send(self, frame: CanFrame) -> JsonObject:
        try:
            import can

            message = can.Message(
                arbitration_id=frame.id,
                is_extended_id=frame.extended,
                is_remote_frame=frame.rtr,
                is_fd=self.is_fd,
                bitrate_switch=self.bitrate_switch,
                error_state_indicator=False,
                data=frame.data,
            )
            self.bus.send(message)
            return {"ok": True, "backend": self.adapter_name}
        except Exception as error:
            return {"ok": False, "error_type": "can_send_failed", "summary": "CAN adapter failed to send a frame.", "backend_error": str(error)}

    def read(self, max_frames: int, wait_timeout_s: float) -> JsonObject:
        frames = []
        deadline = time.monotonic() + wait_timeout_s
        try:
            while len(frames) < max_frames:
                timeout = max(0.0, deadline - time.monotonic()) if wait_timeout_s > 0 and not frames else 0
                message = self.bus.recv(timeout=timeout)
                if message is None:
                    break
                frames.append({"id": message.arbitration_id, "id_hex": f"0x{message.arbitration_id:x}", "extended": bool(message.is_extended_id), "rtr": bool(message.is_remote_frame), "data_hex": bytes(message.data).hex(), "dlc": int(message.dlc)})
            return {"ok": True, "backend": self.adapter_name, "frames": frames}
        except Exception as error:
            return {"ok": False, "error_type": "can_read_failed", "summary": "CAN adapter failed to read frames.", "backend_error": str(error)}

    def close(self) -> None:
        shutdown = getattr(self.bus, "shutdown", None)
        if callable(shutdown):
            shutdown()
        self.active = False

    def status(self) -> JsonObject:
        return {"active": self.active, "backend": self.adapter_name}


def open_python_can_adapter(
    config: AgenticHILConfig,
    bus_id: str,
    bus_config: CanBusConfig,
    clear_rx_queue: bool,
    on_started: Callable[[CanAdapterSession], None] | None = None,
) -> JsonObject:
    if (
        bus_config.adapter == "peak"
        and not is_windows_peak_channel(bus_config.channel)
        and os.name != "nt"
        and not re.fullmatch(r"can\d+|vcan\d+|slcan\d+", bus_config.channel)
    ):
        return {"ok": False, "tool": "can_session_start", "bus_id": bus_id, "adapter": bus_config.adapter, "error_type": "config_invalid", "field": f"can_buses.{bus_id}.channel", "summary": "PEAK adapter on Linux expects a SocketCAN-style interface name such as can0."}
    try:
        import can
    except ImportError:
        return {"ok": False, "tool": "can_session_start", "bus_id": bus_id, "adapter": bus_config.adapter, "error_type": "can_backend_not_available", "summary": "python-can is not installed. Install agentic-hil[can] to use direct CAN adapters."}
    interface = "pcan" if bus_config.adapter == "peak" else "socketcan"
    bus_options: dict[str, object] = {
        "interface": interface,
        "channel": bus_config.channel,
        "bitrate": bus_config.bitrate,
        "fd": bus_config.fd,
        "receive_own_messages": bus_config.receive_own_messages,
    }
    if bus_config.fd and bus_config.data_bitrate is not None:
        bus_options["data_bitrate"] = bus_config.data_bitrate
    if bus_config.listen_only:
        bus_state = getattr(can, "BusState", None)
        if bus_state is None or not hasattr(bus_state, "PASSIVE"):
            return {"ok": False, "tool": "can_session_start", "bus_id": bus_id, "adapter": bus_config.adapter, "error_type": "can_backend_not_available", "summary": "Configured listen_only mode is not supported by this python-can installation."}
        bus_options["state"] = bus_state.PASSIVE
    if bus_config.adapter == "peak" and bus_config.pcanbasic_dll is not None:
        bus_options["dll"] = bus_config.pcanbasic_dll
    try:
        bus = can.Bus(**bus_options)
    except Exception as error:
        return {"ok": False, "tool": "can_session_start", "bus_id": bus_id, "adapter": bus_config.adapter, "error_type": "can_adapter_open_failed", "summary": "CAN adapter could not be opened.", "backend_error": str(error)}
    try:
        session = PythonCanAdapterSession(bus_config.adapter, bus, bus_config.fd, bus_config.fd and bus_config.data_bitrate is not None)
        if on_started is not None:
            on_started(session)
    except BaseException as error:
        try:
            shutdown = getattr(bus, "shutdown", None)
            if callable(shutdown):
                shutdown()
            error._agentic_hil_completion_confirmed = True
        except BaseException:
            pass
        raise
    return {"ok": True, "tool": "can_session_start", "bus_id": bus_id, "adapter": bus_config.adapter, "backend": interface, "session": session, "summary": "CAN adapter opened."}


class ProcessCanAdapterSession(ProcessBridgeSession):
    adapter_name = "process"
    error_prefix = "can_adapter"
    bridge_label = "CAN adapter bridge"

    def send(self, frame: CanFrame) -> JsonObject:
        return self.request("send", {"frame": bridge_frame(frame)}, 10)

    def read(self, max_frames: int, wait_timeout_s: float) -> JsonObject:
        return self.request("read", {"max_frames": max_frames, "wait_timeout_s": wait_timeout_s}, max(10, wait_timeout_s + 1))


def open_process_adapter(
    config: AgenticHILConfig,
    bus_id: str,
    bus_config: CanBusConfig,
    clear_rx_queue: bool,
    on_started: Callable[[CanAdapterSession], None] | None = None,
) -> JsonObject:
    if not bus_config.executable:
        return {"ok": False, "tool": "can_session_start", "bus_id": bus_id, "adapter": "process", "error_type": "config_invalid", "field": f"can_buses.{bus_id}.executable", "summary": "adapter: process requires executable."}
    executable = resolve_work_path(config, bus_config.executable)
    if not Path(executable).is_file():
        return {"ok": False, "tool": "can_session_start", "bus_id": bus_id, "adapter": "process", "error_type": "can_adapter_not_found", "summary": "CAN adapter bridge executable could not be found."}
    command = [*invocation(executable), *bus_config.args]
    try:
        child = subprocess.Popen(command, cwd=config.work_dir, text=True, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except OSError as error:
        return {"ok": False, "tool": "can_session_start", "bus_id": bus_id, "adapter": "process", "error_type": "can_adapter_process_start_failed", "summary": "CAN adapter bridge process could not be started.", "backend_error": str(error)}
    try:
        session = ProcessCanAdapterSession(child)
    except BaseException:
        reap_unmanaged_child(child)
        raise
    try:
        if on_started is not None:
            on_started(session)
        opened = session.request("open", {"channel": bus_config.channel, "bitrate": bus_config.bitrate, "fd": bus_config.fd, "data_bitrate": bus_config.data_bitrate, "receive_own_messages": bus_config.receive_own_messages, "listen_only": bus_config.listen_only, "clear_rx_queue": clear_rx_queue, "poll_interval_ms": bus_config.poll_interval_ms}, bus_config.timeout_s)
    except BaseException as error:
        try:
            close_result = session.close()
        except BaseException:
            close_result = session.last_close_result
        if close_result is not None and close_result.cleanup_confirmed:
            error._agentic_hil_completion_confirmed = True
        raise
    if not opened.get("ok"):
        result: JsonObject = {"tool": "can_session_start", "bus_id": bus_id, "adapter": "process", "command": command_for_log(command), **opened}
        try:
            close_result = session.close()
        except Exception as error:
            result["cleanup_error"] = str(error)
            close_result = session.last_close_result
            if close_result is not None and close_result.cleanup_confirmed:
                result["cleanup_confirmed"] = True
                result["completion_confirmed"] = True
            else:
                result["session"] = session
                result["cleanup_unconfirmed"] = True
                result["completion_unconfirmed"] = True
            return result
        if close_result.cleanup_confirmed:
            result["cleanup_confirmed"] = True
            result["completion_confirmed"] = True
        else:
            result["session"] = session
            result["cleanup_error"] = "; ".join(close_result.errors)
            result["cleanup_unconfirmed"] = True
            result["completion_unconfirmed"] = True
        return result
    return {"ok": True, "tool": "can_session_start", "bus_id": bus_id, "adapter": "process", "command": command_for_log(command), "backend": opened.get("backend", "process"), "session": session, "summary": "CAN adapter bridge opened."}


def payload_frame(bus_config: CanBusConfig, payload: JsonObject) -> JsonObject:
    parsed_id = parse_can_id(payload.get("frame_id", payload.get("id")))
    if parsed_id is None:
        return {"ok": False, "tool": "can_send", "error_type": "invalid_argument", "summary": "frame_id must be an integer or hexadecimal string such as 0x123."}
    extended = payload.get("extended", False)
    rtr = payload.get("rtr", False)
    if not isinstance(extended, bool) or not isinstance(rtr, bool):
        return {"ok": False, "tool": "can_send", "error_type": "invalid_argument", "summary": "extended and rtr must be booleans."}
    max_id = 0x1FFFFFFF if extended else 0x7FF
    if parsed_id < 0 or parsed_id > max_id:
        return {"ok": False, "tool": "can_send", "error_type": "invalid_argument", "summary": "Extended CAN frame_id must be between 0 and 0x1fffffff." if extended else "Standard CAN frame_id must be between 0 and 0x7ff."}
    data_hex = payload.get("data_hex", payload.get("hex", ""))
    if not isinstance(data_hex, str):
        return {"ok": False, "tool": "can_send", "error_type": "invalid_argument", "summary": "data_hex must be a string."}
    data = parse_hex_bytes(data_hex)
    if data is None:
        return {"ok": False, "tool": "can_send", "error_type": "invalid_argument", "summary": "data_hex must contain valid hexadecimal bytes."}
    if len(data) > bus_config.max_frame_data_bytes:
        return {"ok": False, "tool": "can_send", "error_type": "invalid_argument", "summary": "CAN frame data exceeds configured max_frame_data_bytes.", "bytes_requested": len(data), "max_frame_data_bytes": bus_config.max_frame_data_bytes}
    if not bus_config.fd and len(data) > 8:
        return {"ok": False, "tool": "can_send", "error_type": "invalid_argument", "summary": "Classic CAN frames cannot contain more than 8 data bytes.", "bytes_requested": len(data), "max_frame_data_bytes": 8}
    if bus_config.fd and rtr:
        return {"ok": False, "tool": "can_send", "error_type": "invalid_argument", "summary": "CAN FD buses do not support remote (RTR) frames."}
    if bus_config.fd and len(data) not in CAN_FD_VALID_DATA_LENGTHS:
        return {"ok": False, "tool": "can_send", "error_type": "invalid_argument", "summary": "CAN FD frame data length must be 0-8, 12, 16, 20, 24, 32, 48, or 64 bytes.", "bytes_requested": len(data)}
    return {"ok": True, "frame": CanFrame(id=parsed_id, extended=extended, rtr=rtr, data=data)}


def queue_clear_failure(bus_id: str, session: CanBusSession, cleared: object) -> JsonObject:
    details = cleared if isinstance(cleared, dict) else {"backend_error": "CAN adapter returned a non-object queue-clear result."}
    return {
        "ok": False,
        "tool": "can_session_start",
        "bus_id": bus_id,
        "adapter": session.adapter_session.adapter_name,
        "error_type": str(details.get("error_type", "can_read_failed")),
        "backend_error": details.get("backend_error"),
        "queue_clear": details,
        "summary": "CAN receive queue could not be cleared before session use.",
    }


def parse_can_id(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value, 16 if value.lower().startswith("0x") else 10)
        except ValueError:
            return None
    return None


def parse_hex_bytes(value: str) -> bytes | None:
    cleaned = re.sub(r"\s+", "", value)
    if len(cleaned) % 2 != 0 or re.fullmatch(r"[0-9a-fA-F]*", cleaned) is None:
        return None
    return bytes.fromhex(cleaned)


def frame_result(frame: CanFrame) -> JsonObject:
    return {"id": frame.id, "id_hex": f"0x{frame.id:x}", "extended": frame.extended, "rtr": frame.rtr, "data_hex": frame.data.hex(), "dlc": len(frame.data)}


def bridge_frame(frame: CanFrame) -> JsonObject:
    return frame_result(frame)


def normalize_received_frames(raw_frames: object, bus_config: CanBusConfig) -> JsonObject:
    """Strictly validate adapter RX feedback. Malformed feedback must never look like a successful (or empty) read."""
    if not isinstance(raw_frames, list):
        return invalid_frames_response("frames", "Adapter frames must be an array.")
    frames: list[JsonObject] = []
    for index, raw in enumerate(raw_frames):
        field = f"frames[{index}]"
        if not isinstance(raw, dict):
            return invalid_frames_response(field, "Frame must be an object.")
        frame_id = parse_can_id(raw.get("id", raw.get("frame_id")))
        if frame_id is None:
            return invalid_frames_response(f"{field}.id", "Frame id must be an integer or hexadecimal string.")
        extended = raw.get("extended", False)
        rtr = raw.get("rtr", False)
        if not isinstance(extended, bool) or not isinstance(rtr, bool):
            return invalid_frames_response(f"{field}.extended", "extended and rtr must be booleans.")
        max_id = 0x1FFFFFFF if extended else 0x7FF
        if frame_id < 0 or frame_id > max_id:
            return invalid_frames_response(f"{field}.id", "Frame id is out of range for the frame format.")
        raw_data = raw.get("data_hex", raw.get("hex", ""))
        if not isinstance(raw_data, str):
            return invalid_frames_response(f"{field}.data_hex", "data_hex must be a string.")
        data = parse_hex_bytes(raw_data)
        if data is None:
            return invalid_frames_response(f"{field}.data_hex", "data_hex must contain valid hexadecimal bytes.")
        if len(data) > (64 if bus_config.fd else 8):
            return invalid_frames_response(f"{field}.data_hex", "Frame data exceeds the maximum length for this bus.")
        frames.append({"id": frame_id, "id_hex": f"0x{frame_id:x}", "extended": extended, "rtr": rtr, "data_hex": data.hex(), "dlc": len(data)})
    return {"ok": True, "frames": frames}


def invalid_frames_response(field: str, summary: str) -> JsonObject:
    return {
        "ok": False,
        "error_type": "can_adapter_invalid_response",
        "field": field,
        "summary": f"CAN adapter returned malformed frame feedback: {summary}",
    }


def is_windows_peak_channel(channel: str) -> bool:
    return channel.upper().startswith("PCAN_") or channel.lower().startswith("0x")
