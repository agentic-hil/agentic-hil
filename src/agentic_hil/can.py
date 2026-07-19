from __future__ import annotations

import math
import os
import re
import subprocess
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from agentic_hil.backends.common import command_for_log, invocation
from agentic_hil.bridge import BRIDGE_PROTOCOL_VERSION, BridgeCleanupError, ProcessBridgeSession, public_backend_result
from agentic_hil.config import ConfigError, display_path, resolve_work_path, safe_append_text
from agentic_hil.coordination import (
    CoordinationError,
    DetachedHardwareLease,
    HardwareCoordinator,
    HardwareLease,
    can_resource,
)
from agentic_hil.process import managed_process_owner, process_group_kwargs, spawn_managed_process
from agentic_hil.report import (
    append_jsonl,
    audit_unavailable,
    logs_directory,
    mark_audit_failure,
    mark_side_effect,
    overall_success,
    safe_filename,
    timestamp_for_filename,
    utc_now_iso,
    write_report,
)
from agentic_hil.types import AgenticHILConfig, CanBusConfig, JsonObject

SUPPORTED_CAN_ADAPTERS = ["peak", "socketcan", "process"]


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

    def close(self) -> JsonObject | None: ...

    def status(self) -> JsonObject: ...


class CanBusSession:
    def __init__(self, bus_id: str, bus_config: CanBusConfig, adapter_session: CanAdapterSession, log_path: str, lease: HardwareLease | None = None):
        self.bus_id = bus_id
        self.bus_config = bus_config
        self.adapter_session = adapter_session
        self.log_path = log_path
        self.started_at = utc_now_iso()
        self.active = True
        self.audit_broken = False
        self.lease = lease or DetachedHardwareLease()
        self.safe_state_confirmed = False
        self.process_reaped = False


class CanBusService:
    def __init__(self, config: AgenticHILConfig, coordinator: HardwareCoordinator | None = None):
        self.config = config
        self.coordinator = coordinator or HardwareCoordinator(config, "can-service")
        self._owns_coordinator = coordinator is None
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
        if not isinstance(bus_id, str) or not isinstance(clear_rx_queue, bool):
            return self._write_report({"ok": False, "tool": "can_session_start", "error_type": "invalid_argument", "summary": "bus_id must be a string and clear_rx_queue must be a boolean.", "side_effect_committed": False})
        bus = self._configured_bus(bus_id, "can_session_start")
        if not bus["ok"]:
            return self._write_report(bus)
        if not self.config.permissions.allow_can_read and not self.config.permissions.allow_can_write:
            return self._write_report(self._permission_denied("can_session_start", "CAN reading and writing are disabled by the authoritative config.", bus_id))
        existing = self.sessions.get(bus_id)
        if existing and self._session_is_active(existing):
            return self._write_report({"ok": True, "tool": "can_session_start", "bus_id": bus_id, "already_active": True, "session": self._session_status(existing), "summary": "CAN bus session is already active."})
        if existing:
            try:
                self._stop_session(existing, "replaced")
            except Exception as error:
                return self._write_report({"ok": False, "tool": "can_session_start", "bus_id": bus_id, "error_type": "can_adapter_close_failed", "summary": "Previous CAN bus session could not be closed and remains registered for cleanup retry.", "backend_error": str(error)})
            self.sessions.pop(bus_id, None)
        bus_config = bus["bus_config"]
        try:
            log_path = str(Path(logs_directory(self.config)) / f"can-{timestamp_for_filename()}-{safe_filename(bus_id, 'bus')}.jsonl")
            safe_append_text(log_path, "")
        except (ConfigError, OSError) as error:
            return audit_unavailable("can_session_start", error)
        try:
            lease = self.coordinator.acquire(can_resource(self.config, bus_id))
        except CoordinationError as error:
            return self._write_report({"tool": "can_session_start", "bus_id": bus_id, "side_effect_committed": False, **error.result})
        try:
            with managed_process_owner(self.coordinator.owner_token):
                opened = open_adapter(self.config, bus_id, bus_config, clear_rx_queue)
        except BaseException as error:
            lease.quarantine("can_open_interrupted", error)
            raise
        if not opened["ok"]:
            if opened.get("cleanup_required") and isinstance(opened.get("session"), ProcessCanAdapterSession):
                session = CanBusSession(bus_id, bus_config, opened["session"], log_path, lease)
                session.active = False
                self.sessions[bus_id] = session
                lease.quarantine("can_open_cleanup_unconfirmed", opened.get("cleanup_error"))
            failure = {key: value for key, value in opened.items() if key != "session"}
            if opened.get("cleanup_required"):
                return self._write_report(failure)
            safe_to_release = opened.get("side_effect_committed") is False or opened.get("cleanup_confirmed") is True
            if not safe_to_release:
                lease.quarantine("can_open_cleanup_unconfirmed", opened.get("backend_error"))
            return self._write_unattached_lease_report(failure, lease, release_if_safe=safe_to_release)
        adapter_session = opened["session"]
        session = CanBusSession(bus_id, bus_config, adapter_session, log_path, lease)
        self.sessions[bus_id] = session
        if clear_rx_queue and self.config.permissions.allow_can_read:
            try:
                cleared = adapter_session.read(bus_config.max_buffer_frames, 0)
                if not overall_success(cleared):
                    if cleared.get("audit_ok") is False:
                        session.audit_broken = True
                    raise RuntimeError(str(cleared.get("summary", cleared.get("error_type", "CAN queue clear failed."))))
            except BaseException as error:
                try:
                    self._stop_session(session, "start_failed", defer_release=True)
                except BaseException as close_error:
                    written = self._write_report({"ok": False, "tool": "can_session_start", "bus_id": bus_id, "error_type": "can_adapter_close_failed", "summary": "CAN initialization failed and the session remains registered for cleanup retry.", "backend_error": str(close_error)})
                    if isinstance(error, (KeyboardInterrupt, SystemExit)):
                        error.args = (*error.args, f"Cleanup error: {close_error}")
                        raise error from close_error
                    if isinstance(close_error, (KeyboardInterrupt, SystemExit)):
                        raise
                    return written
                failure = {"ok": False, "tool": "can_session_start", "bus_id": bus_id, "error_type": "can_adapter_open_failed", "summary": "CAN receive queue could not be cleared; the adapter was closed.", "backend_error": str(error), "cleanup_confirmed": True, "side_effect_committed": False}
                written = self._write_report(failure)
                if written.get("audit_ok") is False:
                    return written
                if not session.lease.release(safe_state_confirmed=session.safe_state_confirmed, processes_reaped=session.process_reaped):
                    return self._write_report({"ok": False, "tool": "can_session_start", "bus_id": bus_id, "error_type": "can_adapter_close_failed", "summary": "CAN lease release remained unconfirmed.", "cleanup_required": True})
                self.sessions.pop(bus_id, None)
                if isinstance(error, (KeyboardInterrupt, SystemExit)):
                    raise error
                return {**written, **session.lease.status()}
        audit_error = append_jsonl(session.log_path, {"event": "start", "bus_id": bus_id, "adapter": bus_config.adapter, "channel": bus_config.channel, "bitrate": bus_config.bitrate})
        result = {"ok": True, "tool": "can_session_start", "bus_id": bus_id, "already_active": False, "adapter": adapter_session.adapter_name, "adapter_result": public_backend_result(opened), "session": self._session_status(session), "summary": "CAN bus session started."}
        if audit_error is not None:
            session.audit_broken = True
            with suppress(BaseException):
                self._stop_session(session, "audit_failed")
            result["cleanup_required"] = True
        return self._write_report(mark_audit_failure(result, audit_error) if audit_error is not None else result)

    def session_stop(self, bus_id: str) -> JsonObject:
        bus = self._configured_bus(bus_id, "can_session_stop")
        if not bus["ok"]:
            return self._write_report(bus)
        session = self.sessions.get(bus_id)
        if session is None:
            return self._write_report({"ok": True, "tool": "can_session_stop", "bus_id": bus_id, "was_active": False, "summary": "CAN bus session was not active."})
        try:
            audit_error = self._stop_session(session, "requested", defer_release=True)
        except Exception as error:
            return self._write_report({"ok": False, "tool": "can_session_stop", "bus_id": bus_id, "error_type": "can_adapter_close_failed", "summary": "CAN bus session could not be closed and remains registered for cleanup retry.", "backend_error": str(error)})
        result = {"ok": True, "tool": "can_session_stop", "bus_id": bus_id, "was_active": True, "session": self._session_status(session), "summary": "CAN bus session stopped."}
        written = self._write_report(mark_audit_failure(result, audit_error) if audit_error is not None else result)
        if written.get("audit_ok") is False:
            return {**written, "cleanup_required": True, "quarantined": True}
        if not session.lease.release(safe_state_confirmed=session.safe_state_confirmed, processes_reaped=session.process_reaped):
            return self._write_report({"ok": False, "tool": "can_session_stop", "bus_id": bus_id, "error_type": "can_adapter_close_failed", "summary": "CAN lease release remained unconfirmed.", "cleanup_required": True})
        self.sessions.pop(bus_id, None)
        return {**written, **session.lease.status()}

    def send(self, bus_id: str, payload: JsonObject) -> JsonObject:
        bus = self._configured_bus(bus_id, "can_send")
        if not bus["ok"]:
            return self._write_report(bus)
        if not self.config.permissions.allow_can_write:
            return self._write_report(self._permission_denied("can_send", "CAN writing is disabled by the authoritative config.", bus_id))
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
            if result.get("side_effect_committed") is not False and result.get("side_effect_status") is None:
                result.update({"side_effect_status": "unknown", "cleanup_required": True})
            audit_error = append_jsonl(session.log_path, {"event": "error", "direction": "tx", **result})
            return self._write_report(mark_audit_failure(result, audit_error) if audit_error is not None else result)
        result = {"ok": True, "tool": "can_send", "bus_id": bus_id, "adapter": session.adapter_session.adapter_name, "frame": frame_result(frame), "adapter_result": public_backend_result(sent), "log_path": display_path(self.config, session.log_path), "summary": "CAN frame sent."}
        audit_error = append_jsonl(session.log_path, {"direction": "tx", **result})
        return self._write_report(mark_audit_failure(result, audit_error) if audit_error is not None else result)

    def read(self, bus_id: str, max_frames: object | None = None, wait_timeout_s: object = 0.0) -> JsonObject:
        bus = self._configured_bus(bus_id, "can_read")
        if not bus["ok"]:
            return self._write_report(bus)
        if not self.config.permissions.allow_can_read:
            return self._write_report(self._permission_denied("can_read", "CAN reading is disabled by the authoritative config.", bus_id))
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
        if not math.isfinite(parsed_wait_timeout_s):
            return self._write_report({"ok": False, "tool": "can_read", "bus_id": bus_id, "error_type": "invalid_argument", "summary": "wait_timeout_s must be finite."})
        read = session.adapter_session.read(parsed_max_frames, max(0.0, min(parsed_wait_timeout_s, session.bus_config.timeout_s, 60.0)))
        if not read["ok"]:
            result = {"tool": "can_read", "bus_id": bus_id, "adapter": session.adapter_session.adapter_name, "log_path": display_path(self.config, session.log_path), **read}
            if result.get("side_effect_committed") is not False and result.get("side_effect_status") is None:
                result.update({"side_effect_status": "unknown", "cleanup_required": True})
            audit_error = append_jsonl(session.log_path, {"event": "error", "direction": "rx", **result})
            return self._write_report(mark_audit_failure(result, audit_error) if audit_error is not None else result)
        frames = normalize_received_frames(read.get("frames", []))
        if frames is None:
            return self._write_report({"ok": False, "tool": "can_read", "bus_id": bus_id, "adapter": session.adapter_session.adapter_name, "error_type": "can_adapter_invalid_response", "summary": "CAN adapter returned malformed frame data.", "side_effect_status": "unknown", "cleanup_required": True})
        result = {"ok": True, "tool": "can_read", "bus_id": bus_id, "adapter": session.adapter_session.adapter_name, "frames_read": len(frames), "frames": frames, "adapter_result": public_backend_result(read, ["frames"]), "log_path": display_path(self.config, session.log_path), "summary": "CAN frame(s) read." if frames else "No CAN frames were available."}
        audit_error = append_jsonl(session.log_path, {"direction": "rx", **result})
        return self._write_report(mark_audit_failure(result, audit_error) if audit_error is not None else result)

    def close(self) -> None:
        errors: list[tuple[str, BaseException]] = []
        interrupt: BaseException | None = None
        for bus_id in list(self.sessions):
            try:
                result = self.session_stop(bus_id)
                if not overall_success(result):
                    raise RuntimeError(str(result.get("summary", "CAN bus cleanup failed.")))
            except BaseException as error:
                errors.append((bus_id, error))
                if interrupt is None and isinstance(error, (KeyboardInterrupt, SystemExit)):
                    interrupt = error
        if self._owns_coordinator and not errors:
            self.coordinator.close()
        if interrupt is not None:
            interrupt.args = (*interrupt.args, "Cleanup errors: " + "; ".join(f"{bus_id}: {type(error).__name__}: {error}" for bus_id, error in errors))
            raise interrupt
        if errors:
            details = "; ".join(f"{bus_id}: {type(error).__name__}: {error}" for bus_id, error in errors)
            raise RuntimeError(f"CAN bus cleanup failed: {details}") from errors[0][1]

    def _configured_bus(self, bus_id: str, tool: str) -> JsonObject:
        if not bus_id:
            return {"ok": False, "tool": tool, "error_type": "invalid_argument", "summary": "bus_id is required."}
        bus_config = self.config.can_buses.get(bus_id)
        if bus_config is None:
            return {"ok": False, "tool": tool, "bus_id": bus_id, "error_type": "can_bus_not_configured", "summary": "CAN bus is not available in the authoritative config.", "configured_buses": sorted(self.config.can_buses.keys())}
        return {"ok": True, "bus_config": bus_config}

    def _active_session(self, bus_id: str, tool: str) -> JsonObject:
        bus = self._configured_bus(bus_id, tool)
        if not bus["ok"]:
            return bus
        session = self.sessions.get(bus_id)
        if session is None or self.coordinator.blocked or session.audit_broken or session.lease.state != "active" or not self._session_is_active(session):
            if session is not None and (self.coordinator.blocked or session.audit_broken or session.lease.state != "active"):
                return {"ok": False, "tool": tool, "bus_id": bus_id, "error_type": "resource_quarantined", "summary": "CAN bus requires cleanup or audit recovery before further actions.", "cleanup_required": True, "quarantined": True}
            return {"ok": False, "tool": tool, "bus_id": bus_id, "error_type": "session_not_active", "summary": "CAN bus session is not active. Start it with can_session_start first."}
        return {"ok": True, "session": session}

    def _bus_status(self, bus_config: CanBusConfig, session: CanBusSession | None) -> JsonObject:
        result: JsonObject = {"adapter": bus_config.adapter, "channel": bus_config.channel, "bitrate": bus_config.bitrate, "fd": bus_config.fd, "max_buffer_frames": bus_config.max_buffer_frames, "max_frame_data_bytes": bus_config.max_frame_data_bytes, "session_active": False}
        if session is not None:
            result.update(self._session_status(session))
        return result

    def _session_status(self, session: CanBusSession) -> JsonObject:
        return {"session_active": self._session_is_active(session), "started_at": session.started_at, "adapter": session.adapter_session.adapter_name, "adapter_status": session.adapter_session.status(), "log_path": display_path(self.config, session.log_path)}

    def _session_is_active(self, session: CanBusSession) -> bool:
        adapter_status = session.adapter_session.status()
        return session.active and adapter_status.get("active") is not False and adapter_status.get("cleanup_required") is not True

    def _stop_session(self, session: CanBusSession, reason: str, *, defer_release: bool = False) -> Exception | None:
        try:
            close_result = session.adapter_session.close() or {"safe_state_confirmed": True, "process_reaped": True}
        except BaseException as error:
            session.active = False
            details = error.result if isinstance(error, BridgeCleanupError) else error
            session.lease.quarantine("can_adapter_cleanup_unconfirmed", details)
            raise
        session.active = False
        session.safe_state_confirmed = close_result.get("safe_state_confirmed") is True
        session.process_reaped = close_result.get("process_reaped") is True
        audit_error = append_jsonl(session.log_path, {"event": "stop", "reason": reason})
        if audit_error is not None or session.audit_broken:
            session.audit_broken = True
            session.lease.quarantine("can_audit_broken", audit_error, audit_broken=True)
            raise RuntimeError("CAN audit state is broken; resource remains quarantined.")
        elif not defer_release:
            released = session.lease.release(
                safe_state_confirmed=session.safe_state_confirmed,
                processes_reaped=session.process_reaped,
            )
            if not released:
                raise RuntimeError("CAN safe-state release remains unconfirmed.")
        return audit_error

    def _write_report(self, result: JsonObject) -> JsonObject:
        prepared = mark_side_effect(result)
        bus_id = prepared.get("bus_id")
        session = self.sessions.get(bus_id) if isinstance(bus_id, str) else None
        unsafe_effect = prepared.get("side_effect_status") in {"unknown", "partial"}
        if session is not None and unsafe_effect:
            session.lease.quarantine("can_effect_unconfirmed")
        if session is not None:
            prepared = {**prepared, **session.lease.status()}
        written = write_report(self.config, prepared)
        if session is not None and written.get("audit_ok") is False:
            session.audit_broken = True
            session.lease.quarantine("can_report_audit_broken", audit_broken=True)
            written = write_report(self.config, {**written, **session.lease.status()})
        return written

    def _write_unattached_lease_report(self, result: JsonObject, lease: HardwareLease, *, release_if_safe: bool) -> JsonObject:
        written = write_report(self.config, {**mark_side_effect(result), **lease.status()})
        if written.get("audit_ok") is False:
            lease.quarantine("can_report_audit_broken", audit_broken=True)
            return write_report(self.config, {**written, **lease.status()})
        if release_if_safe and not lease.release():
            return write_report(self.config, {**written, **lease.status(), "ok": False, "cleanup_required": True, "summary": "CAN lease release remained unconfirmed."})
        return {**written, **lease.status()}

    def _permission_denied(self, tool: str, summary: str, bus_id: str | None = None) -> JsonObject:
        result: JsonObject = {"ok": False, "tool": tool, "error_type": "permission_denied", "summary": summary}
        if bus_id:
            result["bus_id"] = bus_id
        return result


def open_adapter(config: AgenticHILConfig, bus_id: str, bus_config: CanBusConfig, clear_rx_queue: bool) -> JsonObject:
    if bus_config.adapter == "process":
        return open_process_adapter(config, bus_id, bus_config, clear_rx_queue)
    return open_python_can_adapter(config, bus_id, bus_config, clear_rx_queue)


class PythonCanAdapterSession:
    def __init__(self, adapter_name: str, bus: object, timeout_s: float):
        self.adapter_name = adapter_name
        self.bus = bus
        self.timeout_s = timeout_s
        self.active = True

    def send(self, frame: CanFrame) -> JsonObject:
        try:
            import can

            message = can.Message(arbitration_id=frame.id, is_extended_id=frame.extended, is_remote_frame=frame.rtr, data=frame.data)
            self.bus.send(message, timeout=self.timeout_s)
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

    def close(self) -> JsonObject:
        shutdown = getattr(self.bus, "shutdown", None)
        if callable(shutdown):
            shutdown()
        self.active = False
        return {"ok": True, "safe_state_confirmed": True, "process_reaped": True}

    def status(self) -> JsonObject:
        return {"active": self.active, "backend": self.adapter_name}


def open_python_can_adapter(config: AgenticHILConfig, bus_id: str, bus_config: CanBusConfig, clear_rx_queue: bool) -> JsonObject:
    if (
        bus_config.adapter == "peak"
        and not is_windows_peak_channel(bus_config.channel)
        and os.name != "nt"
        and not re.fullmatch(r"can\d+|vcan\d+|slcan\d+", bus_config.channel)
    ):
        return {"ok": False, "tool": "can_session_start", "bus_id": bus_id, "adapter": bus_config.adapter, "error_type": "config_invalid", "field": f"can_buses.{bus_id}.channel", "summary": "PEAK adapter on Linux expects a SocketCAN-style interface name such as can0.", "side_effect_committed": False}
    try:
        import can
    except ImportError:
        return {"ok": False, "tool": "can_session_start", "bus_id": bus_id, "adapter": bus_config.adapter, "error_type": "can_backend_not_available", "summary": "python-can is not installed. Install agentic-hil[can] to use direct CAN adapters.", "side_effect_committed": False}
    try:
        interface = "pcan" if bus_config.adapter == "peak" else "socketcan"
        bus = can.Bus(interface=interface, channel=bus_config.channel, bitrate=bus_config.bitrate, fd=bus_config.fd, receive_own_messages=bus_config.receive_own_messages)
        session = PythonCanAdapterSession(bus_config.adapter, bus, bus_config.timeout_s)
        return {"ok": True, "tool": "can_session_start", "bus_id": bus_id, "adapter": bus_config.adapter, "backend": interface, "session": session, "summary": "CAN adapter opened."}
    except Exception as error:
        return {"ok": False, "tool": "can_session_start", "bus_id": bus_id, "adapter": bus_config.adapter, "error_type": "can_adapter_open_failed", "summary": "CAN adapter could not be opened.", "backend_error": str(error)}


class ProcessCanAdapterSession(ProcessBridgeSession):
    adapter_name = "process"
    error_prefix = "can_adapter"
    bridge_label = "CAN adapter bridge"

    def __init__(self, child: subprocess.Popen[str], timeout_s: float = 10.0):
        super().__init__(child)
        self.timeout_s = timeout_s

    def send(self, frame: CanFrame) -> JsonObject:
        result = self.request("send", {"frame": bridge_frame(frame)}, self.timeout_s)
        if result.get("ok") is True and (set(result) - {"ok", "backend", "summary"} or not _optional_strings(result, "backend", "summary")):
            return invalid_can_bridge_response("send")
        return result

    def read(self, max_frames: int, wait_timeout_s: float) -> JsonObject:
        request_timeout_s = min(wait_timeout_s, self.timeout_s) + 1.0
        result = self.request("read", {"max_frames": max_frames, "wait_timeout_s": wait_timeout_s}, request_timeout_s)
        if result.get("ok") is True and (set(result) - {"ok", "backend", "summary", "frames"} or not _optional_strings(result, "backend", "summary") or not isinstance(result.get("frames"), list)):
            return invalid_can_bridge_response("read")
        return result


def open_process_adapter(config: AgenticHILConfig, bus_id: str, bus_config: CanBusConfig, clear_rx_queue: bool) -> JsonObject:
    if not bus_config.executable:
        return {"ok": False, "tool": "can_session_start", "bus_id": bus_id, "adapter": "process", "error_type": "config_invalid", "field": f"can_buses.{bus_id}.executable", "summary": "adapter: process requires executable.", "side_effect_committed": False}
    executable = resolve_work_path(config, bus_config.executable)
    if not Path(executable).is_file():
        return {"ok": False, "tool": "can_session_start", "bus_id": bus_id, "adapter": "process", "error_type": "can_adapter_not_found", "summary": "CAN adapter bridge executable could not be found.", "side_effect_committed": False}
    command = invocation(executable)
    try:
        child = spawn_managed_process(command, cwd=str(Path(executable).parent), text=True, encoding="utf-8", errors="replace", stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **process_group_kwargs())
    except OSError as error:
        return {"ok": False, "tool": "can_session_start", "bus_id": bus_id, "adapter": "process", "error_type": "can_adapter_process_start_failed", "summary": "CAN adapter bridge process could not be started.", "backend_error": str(error), "side_effect_committed": False}
    session = ProcessCanAdapterSession(child, bus_config.timeout_s)
    try:
        opened = session.request("open", {"channel": bus_config.channel, "bitrate": bus_config.bitrate, "fd": bus_config.fd, "data_bitrate": bus_config.data_bitrate, "receive_own_messages": bus_config.receive_own_messages, "listen_only": bus_config.listen_only, "clear_rx_queue": clear_rx_queue, "poll_interval_ms": bus_config.poll_interval_ms}, bus_config.timeout_s)
    except BaseException as primary_error:
        try:
            session.close()
        except BaseException as cleanup_error:
            primary_error.args = (*primary_error.args, f"Bridge cleanup error: {cleanup_error}")
        raise
    valid_open = (
        opened.get("ok") is True
        and opened.get("protocol_version") == BRIDGE_PROTOCOL_VERSION
        and not set(opened) - {"ok", "protocol_version", "backend", "summary"}
        and _optional_strings(opened, "backend", "summary")
    )
    if not valid_open:
        if opened.get("ok") is True:
            opened = {"ok": False, "error_type": "can_adapter_protocol_unsupported", "summary": "CAN process adapter must return a valid protocol version 2 open response."}
        try:
            session.close()
        except BridgeCleanupError as cleanup_error:
            return {"tool": "can_session_start", "bus_id": bus_id, "adapter": "process", "command": command_for_log(command), **opened, "cleanup_required": True, "cleanup_error": cleanup_error.result, "session": session}
        return {"tool": "can_session_start", "bus_id": bus_id, "adapter": "process", "command": command_for_log(command), **opened, "cleanup_confirmed": True, "side_effect_committed": False}
    return {"ok": True, "tool": "can_session_start", "bus_id": bus_id, "adapter": "process", "command": command_for_log(command), "backend": opened.get("backend", "process"), "session": session, "summary": "CAN adapter bridge opened."}


def payload_frame(bus_config: CanBusConfig, payload: JsonObject) -> JsonObject:
    if set(payload) - {"frame_id", "extended", "rtr", "data_hex"}:
        return {"ok": False, "tool": "can_send", "error_type": "invalid_argument", "summary": "CAN frame contains unsupported fields."}
    parsed_id = parse_can_id(payload.get("frame_id"))
    if parsed_id is None:
        return {"ok": False, "tool": "can_send", "error_type": "invalid_argument", "summary": "frame_id must be an integer or hexadecimal string such as 0x123."}
    extended = payload.get("extended", False)
    rtr = payload.get("rtr", False)
    if not isinstance(extended, bool) or not isinstance(rtr, bool):
        return {"ok": False, "tool": "can_send", "error_type": "invalid_argument", "summary": "extended and rtr must be booleans."}
    max_id = 0x1FFFFFFF if extended else 0x7FF
    if parsed_id < 0 or parsed_id > max_id:
        return {"ok": False, "tool": "can_send", "error_type": "invalid_argument", "summary": "Extended CAN frame_id must be between 0 and 0x1fffffff." if extended else "Standard CAN frame_id must be between 0 and 0x7ff."}
    data_hex = payload.get("data_hex", "")
    if not isinstance(data_hex, str):
        return {"ok": False, "tool": "can_send", "error_type": "invalid_argument", "summary": "data_hex must be a string."}
    data = parse_hex_bytes(data_hex)
    if data is None:
        return {"ok": False, "tool": "can_send", "error_type": "invalid_argument", "summary": "data_hex must contain valid hexadecimal bytes."}
    if len(data) > bus_config.max_frame_data_bytes:
        return {"ok": False, "tool": "can_send", "error_type": "invalid_argument", "summary": "CAN frame data exceeds configured max_frame_data_bytes.", "bytes_requested": len(data), "max_frame_data_bytes": bus_config.max_frame_data_bytes}
    return {"ok": True, "frame": CanFrame(id=parsed_id, extended=extended, rtr=rtr, data=data)}


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


def normalize_received_frames(raw_frames: object) -> list[JsonObject] | None:
    if not isinstance(raw_frames, list):
        return None
    frames: list[JsonObject] = []
    for raw in raw_frames:
        if not isinstance(raw, dict) or set(raw) - {"id", "id_hex", "frame_id", "extended", "rtr", "data_hex", "hex", "dlc"}:
            return None
        frame_id = parse_can_id(raw.get("id", raw.get("frame_id")))
        data_hex = raw.get("data_hex", raw.get("hex", ""))
        extended = raw.get("extended", False)
        rtr = raw.get("rtr", False)
        if frame_id is None or not isinstance(data_hex, str) or not isinstance(extended, bool) or not isinstance(rtr, bool):
            return None
        data = parse_hex_bytes(data_hex)
        max_id = 0x1FFFFFFF if extended else 0x7FF
        dlc = raw.get("dlc", len(data) if data is not None else -1)
        expected_id_hex = f"0x{frame_id:x}"
        if data is None or frame_id < 0 or frame_id > max_id or not isinstance(dlc, int) or isinstance(dlc, bool) or dlc != len(data) or ("id_hex" in raw and raw["id_hex"] != expected_id_hex):
            return None
        frames.append({"id": frame_id, "id_hex": f"0x{frame_id:x}", "extended": extended, "rtr": rtr, "data_hex": data.hex(), "dlc": len(data)})
    return frames


def invalid_can_bridge_response(method: str) -> JsonObject:
    return {"ok": False, "error_type": "can_adapter_invalid_response", "summary": f"CAN process adapter returned an invalid {method} response.", "side_effect_status": "unknown", "cleanup_required": True}


def _optional_strings(result: JsonObject, *fields: str) -> bool:
    return all(field not in result or isinstance(result[field], str) for field in fields)


def is_windows_peak_channel(channel: str) -> bool:
    return channel.upper().startswith("PCAN_") or channel.lower().startswith("0x")
