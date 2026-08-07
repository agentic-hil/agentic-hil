from __future__ import annotations

import errno as errno_module
import importlib
import json
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
)
from agentic_hil.devices import can_device
from agentic_hil.knowledge import (
    CAN_INTERFACE_NOT_FOUND_ERROR,
    LISTEN_ONLY_UNCONFIRMED_ERROR,
    LISTEN_ONLY_UNSUPPORTED_ERROR,
    remediation_fields,
)
from agentic_hil.process import (
    cleanup_registered_processes,
    current_process_owner,
    managed_process_owner,
    process_group_kwargs,
    spawn_managed_process,
)
from agentic_hil.provisional import (
    cleanup_provisional_handles,
    discharge_provisional_handle,
    register_provisional_handle,
)
from agentic_hil.report import (
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
from agentic_hil.types import AgenticHILConfig, CanBusConfig, JsonObject

SUPPORTED_CAN_ADAPTERS = ["peak", "socketcan", "process"]
CAN_DRAIN_BATCH_LIMIT = 16
CAN_DRAIN_TIMEOUT_S = 1.0

# How far each adapter can be held to `listen_only: true`, and by what evidence.
# Read as data by `can_buses_list`, so a caller can see the scope of the
# guarantee before it starts a session rather than after a refusal.
#
# The three are genuinely different mechanisms, not one mechanism with three
# names. python-can 4.6.1 accepts a `listen_only=` keyword on exactly three
# backends — slcan, vector and nixnet — and none of them is reachable from here.
# Neither `pcan` nor `socketcan` takes that keyword, and neither rejects it: it
# lands in `**kwargs`, is forwarded to `BusABC.__init__(**kwargs: object)`, and
# is discarded without a word. That is why passing it through was never going to
# be the fix.
LISTEN_ONLY_ENFORCEMENT: dict[str, str] = {
    # PcanBus expresses listen-only as `state=BusState.PASSIVE`, which sets
    # PCAN_LISTEN_ONLY through PCANBasic. Set, re-asserted after the channel is
    # initialized, and read back from the driver before the session opens.
    "peak": "driver_verified",
    # SocketCAN's listen-only is a controller ctrlmode owned by the kernel and
    # set out of band with `ip link`. A raw CAN socket cannot change it, so here
    # the flag is a precondition that is measured, never a setting that is
    # applied.
    "socketcan": "link_verified",
    # A bridge is code this project did not write. It is told, and its `open`
    # response must say it complied; forwarding alone proves nothing.
    "process": "bridge_confirmed",
}

# `ip` is read for a *proof*, so it is not resolved off PATH: a PATH entry an
# unprivileged process can write is a PATH entry that can answer "listen-only"
# for an interface that is ACKing. These are the locations iproute2 installs to.
IP_COMMAND_PATHS = ("/sbin/ip", "/usr/sbin/ip", "/bin/ip", "/usr/bin/ip")
LINK_QUERY_TIMEOUT_S = 5.0


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
        # The bus config carries its own permissions, so an inequality here also
        # covers a revoked grant: a session may not outlive the permission that
        # authorized it.
        for bus_id, session in list(self.sessions.items()):
            if config.can_buses.get(bus_id) != session.bus_config:
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
        bus_permissions = bus["bus_config"].permissions
        if not self.config.can_read_allowed(bus["bus_config"]) and not bus_permissions.allow_write:
            return self._write_report(self._permission_denied("can_session_start", "Reading and writing this CAN bus are disabled by the authoritative config.", bus_id))
        if clear_rx_queue and not self.config.can_read_allowed(bus["bus_config"]):
            return self._write_report(self._permission_denied("can_session_start", "Clearing this CAN bus receive queue requires permissions.allow_read on the bus.", bus_id))
        existing = self.sessions.get(bus_id)
        if existing and self._session_is_active(existing):
            if clear_rx_queue:
                cleared = self._drain_rx_queue(existing)
                if not cleared["ok"]:
                    return self._write_report(cleared)
            return self._write_report({"ok": True, "tool": "can_session_start", "bus_id": bus_id, "already_active": True, "frames_drained": cleared.get("frames_drained", 0) if clear_rx_queue else 0, "session": self._session_status(existing), "summary": "CAN bus session is already active."})
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
            lease = self.coordinator.acquire(can_device(self.config, bus_id))
        except CoordinationError as error:
            return self._write_report({"tool": "can_session_start", "bus_id": bus_id, "side_effect_committed": False, **error.result})
        try:
            with managed_process_owner(self.coordinator.owner_marker):
                opened = open_adapter(self.config, bus_id, bus_config, False)
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
        adapter_provisional = register_provisional_handle(self.coordinator.owner_marker, f"can-adapter:{bus_id}", adapter_session.close)
        try:
            session = CanBusSession(bus_id, bus_config, adapter_session, log_path, lease)
        except BaseException as error:
            try:
                adapter_session.close()
            except BaseException as close_error:
                lease.quarantine("can_session_setup_cleanup_unconfirmed", close_error)
                raise RuntimeError(f"CAN session setup failed and adapter cleanup remains unconfirmed: {close_error}") from error
            discharge_provisional_handle(adapter_provisional)
            lease.release()
            raise
        discharge_provisional_handle(adapter_provisional)
        self.sessions[bus_id] = session
        cleared: JsonObject = {"ok": True, "frames_drained": 0}
        if clear_rx_queue and self.config.can_read_allowed(bus_config):
            try:
                cleared = self._drain_rx_queue(session)
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
                failure = {**cleared, "ok": False, "tool": "can_session_start", "bus_id": bus_id, "summary": "CAN receive queue could not be cleared; the adapter was closed.", "backend_error": str(error), "cleanup_confirmed": True}
                written = self._write_report(failure)
                if written.get("audit_ok") is False:
                    return written
                if not session.lease.release(safe_state_confirmed=session.safe_state_confirmed, processes_reaped=session.process_reaped):
                    return self._write_report({"ok": False, "tool": "can_session_start", "bus_id": bus_id, "error_type": "can_adapter_close_failed", "summary": "CAN lease release remained unconfirmed.", "cleanup_required": True})
                self.sessions.pop(bus_id, None)
                if isinstance(error, (KeyboardInterrupt, SystemExit)):
                    raise error
                return recommit_report_with_status(self.config, written, session.lease.status())
        audit_error = append_jsonl_audited(self.config, session.log_path, {"event": "start", "bus_id": bus_id, "adapter": bus_config.adapter, "channel": bus_config.channel, "bitrate": bus_config.bitrate})
        result = {"ok": True, "tool": "can_session_start", "bus_id": bus_id, "already_active": False, "adapter": adapter_session.adapter_name, "adapter_result": public_backend_result(opened), "frames_drained": cleared.get("frames_drained", 0), "session": self._session_status(session), "summary": "CAN bus session started."}
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
        return recommit_report_with_status(self.config, written, session.lease.status())

    def send(self, bus_id: str, payload: JsonObject) -> JsonObject:
        bus = self._configured_bus(bus_id, "can_send")
        if not bus["ok"]:
            return self._write_report(bus)
        if not bus["bus_config"].permissions.allow_write:
            return self._write_report(self._permission_denied("can_send", "Writing to this CAN bus is disabled by the authoritative config.", bus_id))
        session_result = self._active_session(bus_id, "can_send")
        if not session_result["ok"]:
            return self._write_report(session_result)
        session = session_result["session"]
        parsed = payload_frame(session.bus_config, payload)
        if not parsed["ok"]:
            parsed["bus_id"] = bus_id
            return self._write_report(parsed)
        frame = parsed["frame"]
        try:
            sent = session.adapter_session.send(frame)
        except BaseException as error:
            session.lease.quarantine("can_send_effect_unconfirmed", error)
            raise
        if not sent["ok"]:
            result = {"tool": "can_send", "bus_id": bus_id, "adapter": session.adapter_session.adapter_name, "frame": frame_result(frame), "log_path": display_path(self.config, session.log_path), **sent}
            if result.get("side_effect_committed") is not False and result.get("side_effect_status") is None:
                result.update({"side_effect_status": "unknown", "cleanup_required": True})
            audit_error = append_jsonl_audited(self.config, session.log_path, {"event": "error", "direction": "tx", **result})
            return self._write_report(mark_audit_failure(result, audit_error) if audit_error is not None else result)
        result = {"ok": True, "tool": "can_send", "bus_id": bus_id, "adapter": session.adapter_session.adapter_name, "frame": frame_result(frame), "adapter_result": public_backend_result(sent), "log_path": display_path(self.config, session.log_path), "summary": "CAN frame sent."}
        audit_error = append_jsonl_audited(self.config, session.log_path, {"direction": "tx", **result})
        return self._write_report(mark_audit_failure(result, audit_error) if audit_error is not None else result)

    def read(self, bus_id: str, max_frames: object | None = None, wait_timeout_s: object = 0.0) -> JsonObject:
        bus = self._configured_bus(bus_id, "can_read")
        if not bus["ok"]:
            return self._write_report(bus)
        if not self.config.can_read_allowed(bus["bus_config"]):
            return self._write_report(self._permission_denied("can_read", "Reading this CAN bus is disabled by the authoritative config.", bus_id))
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
        try:
            read = session.adapter_session.read(parsed_max_frames, max(0.0, min(parsed_wait_timeout_s, session.bus_config.timeout_s, 60.0)))
        except BaseException as error:
            session.lease.quarantine("can_read_effect_unconfirmed", error)
            raise
        if not read["ok"]:
            result = {"tool": "can_read", "bus_id": bus_id, "adapter": session.adapter_session.adapter_name, "log_path": display_path(self.config, session.log_path), **read}
            if result.get("side_effect_committed") is not False and result.get("side_effect_status") is None:
                result.update({"side_effect_status": "unknown", "cleanup_required": True})
            audit_error = append_jsonl_audited(self.config, session.log_path, {"event": "error", "direction": "rx", **result})
            return self._write_report(mark_audit_failure(result, audit_error) if audit_error is not None else result)
        frames = normalize_received_frames(read.get("frames", []))
        if frames is None:
            return self._write_report({"ok": False, "tool": "can_read", "bus_id": bus_id, "adapter": session.adapter_session.adapter_name, "error_type": "can_adapter_invalid_response", "summary": "CAN adapter returned malformed frame data.", "side_effect_status": "unknown", "cleanup_required": True})
        result = {"ok": True, "tool": "can_read", "bus_id": bus_id, "adapter": session.adapter_session.adapter_name, "frames_read": len(frames), "frames": frames, "adapter_result": public_backend_result(read, ["frames"]), "log_path": display_path(self.config, session.log_path), "summary": "CAN frame(s) read." if frames else "No CAN frames were available."}
        audit_error = append_jsonl_audited(self.config, session.log_path, {"direction": "rx", **result})
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
        errors.extend(("provisional_handle", RuntimeError(error)) for error in cleanup_provisional_handles(self.coordinator.owner_marker))
        if self._owns_coordinator:
            errors.extend(("process_registry", RuntimeError(error)) for error in cleanup_registered_processes(owner_marker=self.coordinator.owner_marker))
            if not errors:
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
        # `listen_only` is reported here, and with it the evidence that would
        # back it on this adapter, so the scope of the guarantee is readable
        # before a session is started rather than only in the refusal that
        # follows one.
        result: JsonObject = {"adapter": bus_config.adapter, "channel": bus_config.channel, "bitrate": bus_config.bitrate, "fd": bus_config.fd, "listen_only": bus_config.listen_only, "listen_only_enforcement": LISTEN_ONLY_ENFORCEMENT[bus_config.adapter], "max_buffer_frames": bus_config.max_buffer_frames, "max_frame_data_bytes": bus_config.max_frame_data_bytes, "session_active": False}
        if session is not None:
            result.update(self._session_status(session))
        return result

    def _session_status(self, session: CanBusSession) -> JsonObject:
        return {"session_active": self._session_is_active(session), "started_at": session.started_at, "adapter": session.adapter_session.adapter_name, "adapter_status": session.adapter_session.status(), "log_path": display_path(self.config, session.log_path)}

    def _session_is_active(self, session: CanBusSession) -> bool:
        adapter_status = session.adapter_session.status()
        return session.active and adapter_status.get("active") is not False and adapter_status.get("cleanup_required") is not True

    def _drain_rx_queue(self, session: CanBusSession) -> JsonObject:
        drained = 0
        audit_error = append_jsonl_audited(self.config, session.log_path, {"event": "queue_clear_start", "bus_id": session.bus_id})
        if audit_error is not None:
            session.audit_broken = True
            session.lease.quarantine("can_queue_clear_audit_broken", audit_error, audit_broken=True)
            return mark_audit_failure({"ok": False, "tool": "can_session_start", "bus_id": session.bus_id, "error_type": "can_queue_clear_failed", "summary": "CAN receive queue clear could not be audited before execution.", "side_effect_committed": False, "side_effect_status": "not_started"}, audit_error)
        # The drain budget bounds adapter time only, so it starts after the pre-drain audit write.
        deadline = time.monotonic() + CAN_DRAIN_TIMEOUT_S
        for _ in range(CAN_DRAIN_BATCH_LIMIT):
            if time.monotonic() >= deadline:
                break
            result = session.adapter_session.read(session.bus_config.max_buffer_frames, 0)
            if not overall_success(result):
                cleared = {"ok": False, "tool": "can_session_start", "bus_id": session.bus_id, "error_type": "can_queue_clear_failed", "summary": str(result.get("summary", "CAN receive queue could not be cleared.")), "backend_result": public_backend_result(result), "frames_drained": drained, "side_effect_committed": drained > 0, "side_effect_status": "partial" if drained else "not_started", "retry_safe": drained == 0}
                return self._audit_queue_clear_result(session, cleared)
            frames = result.get("frames")
            if not isinstance(frames, list):
                cleared = {"ok": False, "tool": "can_session_start", "bus_id": session.bus_id, "error_type": "can_queue_clear_failed", "summary": "CAN adapter returned an invalid queue-drain response.", "frames_drained": drained, "side_effect_committed": drained > 0, "side_effect_status": "partial" if drained else "not_started", "retry_safe": drained == 0}
                return self._audit_queue_clear_result(session, cleared)
            drained += len(frames)
            if not frames:
                return self._audit_queue_clear_result(session, {"ok": True, "frames_drained": drained, "side_effect_committed": drained > 0, "side_effect_status": "committed" if drained else "not_started", "retry_safe": drained == 0})
        return self._audit_queue_clear_result(session, {"ok": False, "tool": "can_session_start", "bus_id": session.bus_id, "error_type": "can_queue_clear_limit", "summary": "CAN receive queue did not become empty within the bounded drain limit.", "frames_drained": drained, "side_effect_committed": drained > 0, "side_effect_status": "partial" if drained else "not_started", "retry_safe": drained == 0})

    def _audit_queue_clear_result(self, session: CanBusSession, result: JsonObject) -> JsonObject:
        audit_error = append_jsonl_audited(self.config, session.log_path, {"event": "queue_clear_complete", **result})
        if audit_error is None:
            return result
        session.audit_broken = True
        session.lease.quarantine("can_queue_clear_audit_broken", audit_error, audit_broken=True)
        return mark_audit_failure(result, audit_error)

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
        audit_error = append_jsonl_audited(self.config, session.log_path, {"event": "stop", "reason": reason})
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
        if session is not None and unsafe_effect and prepared.get("cleanup_confirmed") is not True:
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
        return recommit_report_with_status(self.config, written, lease.status())

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
            # recv() transmits nothing: a failed direct-adapter read proves no
            # frame was sent by this call, so it refuses instead of reporting
            # an unknown bus effect (hardci-hq#97). The process-bridge adapter
            # stays markerless — a broken bridge process is an unknown.
            return {"ok": False, "error_type": "can_read_failed", "summary": "CAN adapter failed to read frames.", "backend_error": str(error), "side_effect_committed": False, "side_effect_status": "not_started", "retry_safe": True}

    def close(self) -> JsonObject:
        shutdown = getattr(self.bus, "shutdown", None)
        if callable(shutdown):
            shutdown()
        self.active = False
        return {"ok": True, "safe_state_confirmed": True, "process_reaped": True}

    def status(self) -> JsonObject:
        return {"active": self.active, "backend": self.adapter_name}


def _raised_errno(error: BaseException, target: int) -> bool:
    """Whether ``target`` is the OS error number this exception was raised over.

    Walked over ``__cause__`` and ``__context__`` rather than matched against the
    message, because the message is the part python-can is free to reword: on the
    SocketCAN path the number reaches us inside a wrapper's cause chain, and a
    classifier that read `"No such device"` would answer differently under a
    non-English libc and identically to a device whose *name* contains the words.
    Both attributes are consulted for the same reason — `OSError.errno` is where
    the kernel's number lives, and `can.CanError.error_code` is where python-can's
    own wrappers copy it when they re-raise.

    Bounded and cycle-safe: an exception chain is caller-supplied data here.
    """
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if any(getattr(current, field, None) == target for field in ("errno", "error_code")):
            return True
        current = current.__cause__ or current.__context__
    return False


def socketcan_interface_missing(error: BaseException, bus_id: str, bus_config: CanBusConfig) -> JsonObject | None:
    """The refusal for a SocketCAN interface that does not exist, or ``None``.

    Constructor time only, ENODEV only, and both halves of that are the contract.

    ``SocketcanBus()`` creates a raw socket and binds it to the named interface.
    `ENODEV` there means the kernel has no such netdev: the bind never happened,
    no controller was addressed, and contact was not merely unproven but
    impossible. That is a refusal about the host's configuration, in the same
    class as a missing toolchain — the bench stays in service.

    Not widened past the constructor, and not past this one number. `ENETDOWN`
    on a bound socket is an interface that exists and went down under a session
    that may already have been on the bus, and a send failure says nothing about
    what the controller did with the frame; both keep the markerless result and
    the quarantine it earns.

    Why this exists at all (hardci-hq#127): the SocketCAN classifier above admits
    only `CanInitializationError` as proof of no contact, and python-can never
    raises that class for a failed bind — 4.6.1 re-raises the bare `OSError`, and
    the wrapper it uses elsewhere is `CanOperationError`, which is not a subclass
    of it. So the one failure that is provably harmless — a `can0` that is simply
    not there after a re-enumeration — quarantined a bench that nothing had
    touched.
    """
    if bus_config.adapter == "peak" or not _raised_errno(error, errno_module.ENODEV):
        return None
    return {
        "ok": False,
        "tool": "can_session_start",
        "bus_id": bus_id,
        "adapter": bus_config.adapter,
        "error_type": CAN_INTERFACE_NOT_FOUND_ERROR,
        "field": f"can_buses.{bus_id}.channel",
        "channel": bus_config.channel,
        "summary": (
            f"SocketCAN interface {bus_config.channel} does not exist on this host, so the session was refused before "
            "any controller was addressed: the socket had nothing to bind to."
        ),
        "backend_error": str(error),
        "target_contacted": False,
        "side_effect_committed": False,
        "side_effect_status": "not_started",
        "retry_safe": True,
        **remediation_fields(CAN_INTERFACE_NOT_FOUND_ERROR),
    }


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

    def open_failure(error: BaseException) -> JsonObject:
        return {"ok": False, "tool": "can_session_start", "bus_id": bus_id, "adapter": bus_config.adapter, "error_type": "can_adapter_open_failed", "summary": "CAN adapter could not be opened.", "backend_error": str(error)}

    interface = "pcan" if bus_config.adapter == "peak" else "socketcan"
    bus_kwargs: JsonObject = {
        "interface": interface,
        "channel": bus_config.channel,
        "bitrate": bus_config.bitrate,
        "fd": bus_config.fd,
        "receive_own_messages": bus_config.receive_own_messages,
    }
    if bus_config.listen_only:
        refused = listen_only_precondition(can, interface, bus_id, bus_config, bus_kwargs)
        if refused is not None:
            return refused
    # Which backend can prove, from the exception class alone, that it never
    # joined the bus. SocketCAN can: the controller is brought up out of band
    # with `ip link`, and `SocketcanBus()` only creates and binds a raw socket,
    # so a failure there leaves the interface exactly as this call found it.
    #
    # PCAN deliberately cannot, and is deliberately absent. python-can 4.6.1
    # raises `PcanCanInitializationError` from four `SetValue` calls that run
    # *after* `PCANBasic.Initialize` has already succeeded — error frames, echo
    # frames, auto-reset, the receive event — and every one of them raises
    # before `BusABC.__init__`, so no bus object exists to shut down, the
    # best-effort destructor has no `_is_shutdown` state to consult, and nothing
    # is returned here that could be closed. The class carries no phase marker
    # to tell those apart from the `Initialize` refusal above them. An
    # initialized PCAN channel is on the bus: it ACKs, and at a wrong bitrate it
    # emits error frames. That is the unknown quarantine exists for, so this
    # path keeps the markerless result and the lease stays contained.
    #
    # Read defensively off the module because tests (and old python-can) may not
    # provide the class.
    proves_no_contact = getattr(can, "CanInitializationError", None) if interface == "socketcan" else None
    initialization_errors = (proves_no_contact,) if isinstance(proves_no_contact, type) else ()
    try:
        bus = can.Bus(**bus_kwargs)
    except Exception as error:
        if interface == "socketcan":
            # Asked before the class check below, because it answers a stronger
            # question than that check can: the class says which failures
            # *cannot* have joined the bus, and this says the interface the bind
            # would have joined is not on this host at all.
            missing = socketcan_interface_missing(error, bus_id, bus_config)
            if missing is not None:
                return missing
        failure = open_failure(error)
        if initialization_errors and isinstance(error, initialization_errors):
            # Provably never on the bus: refuse, do not quarantine
            # (hardci-hq#97). Every other exception class, and every failure on
            # a backend that is not in `initialization_errors`, keeps the
            # markerless result — a partially initialized channel participates
            # on the bus (it ACKs and can emit error frames at a wrong bitrate),
            # and nothing here can disprove one was left behind.
            failure.update({"side_effect_committed": False, "side_effect_status": "not_started", "retry_safe": True})
        return failure
    shutdown = getattr(bus, "shutdown", None)
    try:
        provisional = register_provisional_handle(current_process_owner(), f"can:{bus_id}", shutdown if callable(shutdown) else lambda: None)
    except BaseException as register_error:
        try:
            if callable(shutdown):
                shutdown()
        except BaseException as cleanup_error:
            if not isinstance(register_error, Exception):
                register_error.args = (*register_error.args, f"Cleanup error: {cleanup_error}")
                raise
            return {**open_failure(register_error), "cleanup_error": str(cleanup_error)}
        if not isinstance(register_error, Exception):
            raise
        return {**open_failure(register_error), "cleanup_confirmed": True}
    try:
        session = PythonCanAdapterSession(bus_config.adapter, bus, bus_config.timeout_s)
    except BaseException as primary_error:
        try:
            if callable(shutdown):
                shutdown()
        except BaseException as cleanup_error:
            # Keep the raw bus registered for a retried service.close(); the
            # markerless result is what quarantines the lease over the
            # unconfirmed adapter.
            if not isinstance(primary_error, Exception):
                primary_error.args = (*primary_error.args, f"Cleanup error: {cleanup_error}")
                raise
            return {**open_failure(primary_error), "cleanup_error": str(cleanup_error)}
        discharge_provisional_handle(provisional)
        if not isinstance(primary_error, Exception):
            raise
        # The adapter was confirmed shut down, so the bus ends where a normal
        # open/shutdown cycle ends it.
        return {**open_failure(primary_error), "cleanup_confirmed": True}
    unconfirmed = listen_only_unconfirmed(can, interface, bus, bus_id, bus_config)
    if unconfirmed is not None:
        # An initialized PCAN channel is already on the bus, so unlike the
        # SocketCAN precondition above this cannot refuse before contact. What
        # it bounds is the exposure: an unconfirmed listen-only is closed at the
        # open instead of being carried, silently, for the length of a session.
        try:
            session.close()
        except BaseException as cleanup_error:
            # Keep the bus registered for a retried service.close() and stay
            # markerless, so the lease quarantines over the unconfirmed adapter.
            if not isinstance(cleanup_error, Exception):
                raise
            return {**unconfirmed, "cleanup_error": str(cleanup_error)}
        discharge_provisional_handle(provisional)
        return {**unconfirmed, "cleanup_confirmed": True}
    discharge_provisional_handle(provisional)
    result = {"ok": True, "tool": "can_session_start", "bus_id": bus_id, "adapter": bus_config.adapter, "backend": interface, "session": session, "summary": "CAN adapter opened."}
    if bus_config.listen_only:
        result.update({"listen_only": True, "listen_only_enforcement": LISTEN_ONLY_ENFORCEMENT[bus_config.adapter]})
    return result


def listen_only_precondition(can_module: object, interface: str, bus_id: str, bus_config: CanBusConfig, bus_kwargs: JsonObject) -> JsonObject | None:
    """Settle `listen_only: true` before anything is opened, where that is possible.

    Returns a refusal, or ``None`` and the mutated ``bus_kwargs`` the mode is
    requested through. Two backends, two different answers, and the asymmetry is
    the whole point of this function existing.
    """
    if interface == "socketcan":
        link = socketcan_link_listen_only(bus_config.channel)
        if link["listen_only"] is True:
            return None
        return {
            "ok": False,
            "tool": "can_session_start",
            "bus_id": bus_id,
            "adapter": bus_config.adapter,
            "error_type": LISTEN_ONLY_UNSUPPORTED_ERROR,
            "field": f"can_buses.{bus_id}.listen_only",
            "listen_only_requested": True,
            "listen_only_confirmed": False,
            "link_state": link["detail"],
            "summary": (
                f"`listen_only: true` cannot be honoured on SocketCAN interface {bus_config.channel}: the mode belongs to the "
                f"kernel's CAN controller, not to the socket this process opens, and it is not in force. {link['detail']}"
            ),
            "side_effect_committed": False,
            "side_effect_status": "not_started",
            "retry_safe": True,
            **remediation_fields(LISTEN_ONLY_UNSUPPORTED_ERROR, bus_config.adapter),
        }
    # PCAN. python-can takes no `listen_only` keyword; the mode is
    # `state=BusState.PASSIVE`, which sets PCAN_LISTEN_ONLY through PCANBasic.
    passive = getattr(getattr(can_module, "BusState", None), "PASSIVE", None)
    if passive is None:
        return {
            "ok": False,
            "tool": "can_session_start",
            "bus_id": bus_id,
            "adapter": bus_config.adapter,
            "error_type": LISTEN_ONLY_UNSUPPORTED_ERROR,
            "field": f"can_buses.{bus_id}.listen_only",
            "listen_only_requested": True,
            "listen_only_confirmed": False,
            "summary": (
                "`listen_only: true` cannot be honoured: this python-can installation exposes no `can.BusState.PASSIVE`, which is "
                "the only way the PCAN backend expresses listen-only. Install python-can 4.x."
            ),
            "side_effect_committed": False,
            "side_effect_status": "not_started",
            "retry_safe": True,
            **remediation_fields(LISTEN_ONLY_UNSUPPORTED_ERROR, bus_config.adapter),
        }
    bus_kwargs["state"] = passive
    return None


def listen_only_unconfirmed(can_module: object, interface: str, bus: object, bus_id: str, bus_config: CanBusConfig) -> JsonObject | None:
    """Whether an opened adapter failed to confirm the listen-only it was asked for.

    Returns ``None`` when there is nothing to answer for — the flag is off, or
    the backend settled it before the bus existed.
    """
    if not bus_config.listen_only or interface != "pcan":
        return None
    state = pcan_listen_only_state(can_module, bus)
    if state["listen_only"] is True:
        return None
    return {
        "ok": False,
        "tool": "can_session_start",
        "bus_id": bus_id,
        "adapter": bus_config.adapter,
        "error_type": LISTEN_ONLY_UNCONFIRMED_ERROR,
        "field": f"can_buses.{bus_id}.listen_only",
        "listen_only_requested": True,
        "listen_only_confirmed": False,
        "driver_state": state["detail"],
        "summary": (
            f"`listen_only: true` was requested on PCAN channel {bus_config.channel} and the driver did not confirm it, so the "
            f"channel was closed rather than used to observe a bus it would have ACKed on. {state['detail']}"
        ),
        **remediation_fields(LISTEN_ONLY_UNCONFIRMED_ERROR, bus_config.adapter),
    }


def pcan_listen_only_state(can_module: object, bus: object) -> JsonObject:
    """Re-assert PCAN's listen-only parameter after initialization, then measure it.

    Both halves are needed. python-can 4.6.1 applies `state` from the
    constructor *before* `PCANBasic.Initialize` and discards the `SetValue`
    return code, so the constructor's request may never have reached an
    initialized channel and nothing would have said so. Assigning the property
    again runs the same `SetValue` against a channel that exists; reading
    PCAN_LISTEN_ONLY back turns the result from a promise into a measurement.
    """
    passive = getattr(getattr(can_module, "BusState", None), "PASSIVE", None)
    if passive is not None:
        try:
            bus.state = passive
        except Exception as error:
            return {"listen_only": False, "detail": f"PCAN refused the listen-only mode: {type(error).__name__}: {error}."}
    basic = _optional_module("can.interfaces.pcan.basic")
    parameter = getattr(basic, "PCAN_LISTEN_ONLY", None)
    expected = getattr(basic, "PCAN_PARAMETER_ON", None)
    error_ok = getattr(basic, "PCAN_ERROR_OK", None)
    get_value = getattr(getattr(bus, "m_objPCANBasic", None), "GetValue", None)
    handle = getattr(bus, "m_PcanHandle", None)
    if parameter is None or expected is None or error_ok is None or not callable(get_value) or handle is None:
        return {"listen_only": False, "detail": "The PCANBasic listen-only parameter could not be read back from this python-can build, so the mode is unverifiable here."}
    try:
        status, value = get_value(handle, parameter)
    except Exception as error:
        return {"listen_only": False, "detail": f"Reading PCAN_LISTEN_ONLY back from the driver failed: {type(error).__name__}: {error}."}
    if status != error_ok:
        return {"listen_only": False, "detail": f"The driver refused to report PCAN_LISTEN_ONLY (status {status})."}
    if value != expected:
        return {"listen_only": False, "detail": "The driver reports PCAN_LISTEN_ONLY off: this channel would send dominant ACK bits."}
    return {"listen_only": True, "detail": "The driver reports PCAN_LISTEN_ONLY on."}


def socketcan_link_listen_only(channel: str) -> JsonObject:
    """Whether the kernel has this CAN interface in listen-only mode.

    Nothing here sets anything: SocketCAN's listen-only is a controller ctrlmode
    carried on the netdev, and a raw CAN socket cannot change it. Every answer
    that is not a positive reading is reported as *not* in listen-only, because
    the flag exists to be a proof and an unread ctrlmode proves nothing.
    """
    unknown = "The interface's control mode could not be read"
    executable = next((path for path in IP_COMMAND_PATHS if Path(path).is_file()), None)
    if executable is None:
        # The platform answer is the more useful one where it applies, and it is
        # decided by what was found rather than before looking, so that the whole
        # path stays reachable from a test on either operating system.
        return {"listen_only": False, "detail": "SocketCAN interfaces, and the listen-only control mode, exist only on Linux." if os.name == "nt" else f"{unknown}: iproute2 (`ip`) is not installed at any of {', '.join(IP_COMMAND_PATHS)}."}
    command = [executable, "-details", "-json", "link", "show", "dev", channel]
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=LINK_QUERY_TIMEOUT_S, check=False)
    except (OSError, subprocess.SubprocessError) as error:
        return {"listen_only": False, "detail": f"{unknown}: `ip link show dev {channel}` could not be run ({type(error).__name__}: {error})."}
    if completed.returncode != 0:
        return {"listen_only": False, "detail": f"{unknown}: `ip link show dev {channel}` exited {completed.returncode} ({completed.stderr.strip()[:200] or 'no output'})."}
    try:
        links = json.loads(completed.stdout)
    except (json.JSONDecodeError, ValueError):
        return {"listen_only": False, "detail": f"{unknown}: `ip -json link show` returned no JSON; iproute2 4.15 or newer reports the control mode this way."}
    if not isinstance(links, list) or len(links) != 1 or not isinstance(links[0], dict):
        return {"listen_only": False, "detail": f"{unknown}: `ip -json link show dev {channel}` did not describe exactly one interface."}
    link_info = links[0].get("linkinfo")
    kind = link_info.get("info_kind") if isinstance(link_info, dict) else None
    if kind != "can":
        if not isinstance(kind, str):
            return {"listen_only": False, "detail": f"{unknown}: `ip -json link show dev {channel}` reported no link kind, so nothing says this is a CAN controller."}
        return {"listen_only": False, "detail": f"{channel} is a {kind} interface, which has no CAN controller and therefore no listen-only mode to be in."}
    info_data = link_info.get("info_data")
    # A CAN link with no ctrlmode flags set omits the key; that is a reading, not
    # a gap. `ctrlmode` is an array in every iproute2 that emits JSON; a scalar
    # is accepted defensively rather than read as an absence.
    raw_modes = info_data.get("ctrlmode", []) if isinstance(info_data, dict) else None
    modes = raw_modes if isinstance(raw_modes, list) else [raw_modes]
    if raw_modes is None or not all(isinstance(mode, str) for mode in modes):
        return {"listen_only": False, "detail": f"{unknown}: `ip -json link show dev {channel}` reported no readable ctrlmode."}
    if not any(mode.strip().lower() == "listen-only" for mode in modes):
        return {"listen_only": False, "detail": f"{channel} is up without listen-only, so its controller sends dominant ACK bits."}
    return {"listen_only": True, "detail": f"The kernel reports {channel} in listen-only mode."}


def _optional_module(name: str) -> object | None:
    try:
        return importlib.import_module(name)
    except Exception:
        return None


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


# The `open` failures the bridge transport synthesizes *after* the request was
# handed to the child: nothing came back (`timeout`), or something came back that
# this protocol cannot read (`invalid_response`). The bridge may have acted on the
# request in either case, and nothing that arrived says whether it did. The two
# raised before the write — `can_adapter_process_exited`,
# `can_adapter_invalid_request` — are deliberately absent: they prove the bridge
# was never asked, which is what lets a refusal say the bus was not touched.
BRIDGE_OPEN_UNANSWERED = frozenset({"can_adapter_timeout", "can_adapter_invalid_response"})


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
        and not set(opened) - {"ok", "protocol_version", "backend", "summary", "listen_only"}
        and _optional_strings(opened, "backend", "summary")
        and ("listen_only" not in opened or isinstance(opened["listen_only"], bool))
    )
    # Being told is not the same as complying. The request has always carried
    # `listen_only`; a bridge that dropped it answered `ok` all the same, and the
    # caller was told a bus was being observed passively by a node that ACKed.
    # So the confirmation is required from the response, and only where the
    # guarantee was actually asked for — a bridge that never sees
    # `listen_only: true` is unaffected.
    listen_only_confirmed = not bus_config.listen_only or opened.get("listen_only") is True
    if not valid_open or not listen_only_confirmed:
        # `side_effect_committed: False` is a claim that the bridge is not on the
        # bus, and this path makes it only where something backs it: the bridge's
        # own `ok: false`, or a failure raised before its open request was sent.
        # A bridge that answered `ok: true` has opened, whatever disqualified the
        # response afterwards — its shape, or a listen-only it did not confirm —
        # and a request that was delivered and then not answered leaves the far
        # side's state to the far side. Those withhold the marker rather than
        # assume the reading that looks better, and `mark_side_effect` renders
        # the absence as `side_effect_status: unknown`. `cleanup_confirmed` still
        # releases the lease, so what changes is what the report claims and not
        # whether a bench goes into recovery (hardci-hq#112).
        bus_contact_unknown = opened.get("ok") is True or opened.get("error_type") in BRIDGE_OPEN_UNANSWERED
        if opened.get("ok") is True:
            opened = (
                {"ok": False, "error_type": "can_adapter_protocol_unsupported", "summary": "CAN process adapter must return a valid protocol version 2 open response."}
                if not valid_open
                else {
                    "ok": False,
                    "error_type": LISTEN_ONLY_UNCONFIRMED_ERROR,
                    "field": f"can_buses.{bus_id}.listen_only",
                    "listen_only_requested": True,
                    "listen_only_confirmed": False,
                    "summary": (
                        "`listen_only: true` was sent to this CAN bridge in the open request and its response did not confirm it, so the "
                        "session was refused rather than opened on an unproven promise. A bridge that honours the mode answers "
                        "`listen_only: true` in its open result."
                    ),
                    **remediation_fields(LISTEN_ONLY_UNCONFIRMED_ERROR, "process"),
                }
            )
        try:
            session.close()
        except BridgeCleanupError as cleanup_error:
            return {"tool": "can_session_start", "bus_id": bus_id, "adapter": "process", "command": command_for_log(command), **opened, "cleanup_required": True, "cleanup_error": cleanup_error.result, "session": session}
        contact: JsonObject = {} if bus_contact_unknown else {"side_effect_committed": False}
        return {"tool": "can_session_start", "bus_id": bus_id, "adapter": "process", "command": command_for_log(command), **opened, "cleanup_confirmed": True, **contact}
    result = {"ok": True, "tool": "can_session_start", "bus_id": bus_id, "adapter": "process", "command": command_for_log(command), "backend": opened.get("backend", "process"), "session": session, "summary": "CAN adapter bridge opened."}
    if bus_config.listen_only:
        result.update({"listen_only": True, "listen_only_enforcement": LISTEN_ONLY_ENFORCEMENT["process"]})
    return result


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
