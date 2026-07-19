from __future__ import annotations

import math
import subprocess
from contextlib import suppress
from pathlib import Path

from agentic_hil.backends.common import command_for_log, invocation
from agentic_hil.bridge import BRIDGE_PROTOCOL_VERSION, BridgeCleanupError, ProcessBridgeSession, public_backend_result
from agentic_hil.config import ConfigError, display_path, resolve_work_path, safe_append_text
from agentic_hil.coordination import CoordinationError, HardwareCoordinator, HardwareLease, adapter_resource
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
from agentic_hil.types import AdapterConfig, AgenticHILConfig, JsonObject


class AdapterBridgeSession(ProcessBridgeSession):
    adapter_name = "process"
    error_prefix = "adapter_bridge"
    bridge_label = "Test adapter bridge"


class AdapterSession:
    def __init__(self, adapter_id: str, adapter_config: AdapterConfig, bridge: AdapterBridgeSession, log_path: str, lease: HardwareLease):
        self.adapter_id = adapter_id
        self.adapter_config = adapter_config
        self.bridge = bridge
        self.log_path = log_path
        self.started_at = utc_now_iso()
        self.active = True
        self.audit_broken = False
        self.lease = lease
        self.safe_state_confirmed = False
        self.process_reaped = False


class AdapterService:
    def __init__(self, config: AgenticHILConfig, coordinator: HardwareCoordinator | None = None):
        self.config = config
        self.coordinator = coordinator or HardwareCoordinator(config, "adapter-service")
        self._owns_coordinator = coordinator is None
        self.sessions: dict[str, AdapterSession] = {}

    def reconfigure(self, config: AgenticHILConfig) -> None:
        for adapter_id, session in list(self.sessions.items()):
            permissions_revoked = not config.permissions.allow_adapter_read and not config.permissions.allow_adapter_write
            if permissions_revoked or config.adapters.get(adapter_id) != session.adapter_config:
                self._stop_session(session, "config_reloaded")
                self.sessions.pop(adapter_id, None)
        self.config = config

    def list_adapters(self) -> JsonObject:
        adapters = {adapter_id: self._adapter_status(adapter_config, self.sessions.get(adapter_id)) for adapter_id, adapter_config in self.config.adapters.items()}
        return {"ok": True, "tool": "adapters_list", "adapters": adapters, "summary": f"{len(adapters)} configured test adapter(s)."}

    def session_start(self, adapter_id: str) -> JsonObject:
        adapter = self._configured_adapter(adapter_id, "adapter_session_start")
        if not adapter["ok"]:
            return self._write_report(adapter)
        if not self.config.permissions.allow_adapter_read and not self.config.permissions.allow_adapter_write:
            return self._write_report(self._permission_denied("adapter_session_start", "Test adapter reading and writing are disabled by the authoritative config.", adapter_id))
        existing = self.sessions.get(adapter_id)
        if existing and self._session_is_active(existing):
            return self._write_report({"ok": True, "tool": "adapter_session_start", "adapter_id": adapter_id, "already_active": True, "session": self._session_status(existing), "summary": "Test adapter session is already active."})
        if existing:
            try:
                self._stop_session(existing, "replaced")
            except Exception as error:
                return self._write_report(self._close_failure("adapter_session_start", adapter_id, error))
            self.sessions.pop(adapter_id, None)
        try:
            log_path = str(Path(logs_directory(self.config)) / f"adapter-{timestamp_for_filename()}-{safe_filename(adapter_id, 'adapter')}.jsonl")
            safe_append_text(log_path, "")
        except (ConfigError, OSError) as error:
            return audit_unavailable("adapter_session_start", error)
        try:
            lease = self.coordinator.acquire(adapter_resource(self.config, adapter_id))
        except CoordinationError as error:
            return self._write_report({"tool": "adapter_session_start", "adapter_id": adapter_id, "side_effect_committed": False, **error.result})
        try:
            with managed_process_owner(self.coordinator.owner_token):
                opened = open_adapter_bridge(self.config, adapter_id, adapter["adapter_config"])
        except BaseException as error:
            lease.quarantine("adapter_open_interrupted", error)
            raise
        if not opened["ok"]:
            if opened.get("cleanup_required") and isinstance(opened.get("session"), AdapterBridgeSession):
                session = AdapterSession(adapter_id, adapter["adapter_config"], opened["session"], log_path, lease)
                session.active = False
                self.sessions[adapter_id] = session
                lease.quarantine("adapter_open_cleanup_unconfirmed", opened.get("cleanup_error"))
            failure = {key: value for key, value in opened.items() if key != "session"}
            if opened.get("cleanup_required"):
                return self._write_report(failure)
            safe_to_release = opened.get("side_effect_committed") is False or opened.get("cleanup_confirmed") is True
            if not safe_to_release:
                lease.quarantine("adapter_open_cleanup_unconfirmed", opened.get("backend_error"))
            return self._write_unattached_lease_report(failure, lease, release_if_safe=safe_to_release)
        bridge = opened["session"]
        try:
            session = AdapterSession(adapter_id, adapter["adapter_config"], bridge, log_path, lease)
        except BaseException as error:
            try:
                bridge.close()
            except BaseException as close_error:
                lease.quarantine("adapter_session_setup_cleanup_unconfirmed", close_error)
                raise RuntimeError(f"Test adapter session setup failed and bridge cleanup remains unconfirmed: {close_error}") from error
            lease.release()
            raise
        self.sessions[adapter_id] = session
        audit_error = append_jsonl(session.log_path, {"event": "start", "adapter_id": adapter_id, "executable": session.adapter_config.executable})
        result = {"ok": True, "tool": "adapter_session_start", "adapter_id": adapter_id, "already_active": False, "adapter_result": public_backend_result(opened), "session": self._session_status(session), "summary": "Test adapter session started."}
        if audit_error is not None:
            session.audit_broken = True
            with suppress(BaseException):
                self._stop_session(session, "audit_failed")
            result["cleanup_required"] = True
        return self._write_report(mark_audit_failure(result, audit_error) if audit_error is not None else result)

    def session_stop(self, adapter_id: str) -> JsonObject:
        adapter = self._configured_adapter(adapter_id, "adapter_session_stop")
        if not adapter["ok"]:
            return self._write_report(adapter)
        session = self.sessions.get(adapter_id)
        if session is None:
            return self._write_report({"ok": True, "tool": "adapter_session_stop", "adapter_id": adapter_id, "was_active": False, "summary": "Test adapter session was not active."})
        try:
            audit_error = self._stop_session(session, "requested", defer_release=True)
        except Exception as error:
            return self._write_report(self._close_failure("adapter_session_stop", adapter_id, error))
        result = {"ok": True, "tool": "adapter_session_stop", "adapter_id": adapter_id, "was_active": True, "session": self._session_status(session), "summary": "Test adapter session stopped."}
        written = self._write_report(mark_audit_failure(result, audit_error) if audit_error is not None else result)
        if written.get("audit_ok") is False:
            return {**written, "cleanup_required": True, "quarantined": True}
        if not session.lease.release(safe_state_confirmed=session.safe_state_confirmed, processes_reaped=session.process_reaped):
            return self._write_report(self._close_failure("adapter_session_stop", adapter_id, RuntimeError("Lease release remained unconfirmed.")))
        self.sessions.pop(adapter_id, None)
        return {**written, **session.lease.status()}

    def set_value(self, adapter_id: str, payload: JsonObject) -> JsonObject:
        tool = "adapter_set_value"
        invalid = reject_extra_payload(tool, payload, {"channel", "value", "unit"})
        if invalid is not None:
            return self._write_report(invalid)
        session_result = self._writable_session(adapter_id, tool)
        if not session_result["ok"]:
            return self._write_report(session_result)
        session = session_result["session"]
        channel = self._allowed_channel(session, tool, payload.get("channel"))
        if not channel["ok"]:
            return self._write_report(channel)
        value = payload.get("value")
        try:
            finite_value = isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
        except OverflowError:
            finite_value = False
        if not finite_value:
            return self._write_report({"ok": False, "tool": tool, "adapter_id": adapter_id, "error_type": "invalid_argument", "summary": "value must be a finite number."})
        unit = payload.get("unit")
        if unit is not None and not isinstance(unit, str):
            return self._write_report({"ok": False, "tool": tool, "adapter_id": adapter_id, "error_type": "invalid_argument", "summary": "unit must be a string."})
        params: JsonObject = {"channel": channel["channel"], "value": value}
        if unit is not None:
            params["unit"] = unit
        return self._bridge_action(session, tool, "set_value", params)

    def inject_fault(self, adapter_id: str, payload: JsonObject) -> JsonObject:
        tool = "adapter_inject_fault"
        invalid = reject_extra_payload(tool, payload, {"fault", "channel"})
        if invalid is not None:
            return self._write_report(invalid)
        session_result = self._writable_session(adapter_id, tool)
        if not session_result["ok"]:
            return self._write_report(session_result)
        session = session_result["session"]
        fault = self._allowed_fault(session, tool, payload.get("fault"), required=True)
        if not fault["ok"]:
            return self._write_report(fault)
        params: JsonObject = {"fault": fault["fault"]}
        optional_channel = self._optional_channel(session, tool, payload.get("channel"))
        if not optional_channel["ok"]:
            return self._write_report(optional_channel)
        if optional_channel.get("channel") is not None:
            params["channel"] = optional_channel["channel"]
        return self._bridge_action(session, tool, "inject_fault", params)

    def clear_fault(self, adapter_id: str, payload: JsonObject) -> JsonObject:
        tool = "adapter_clear_fault"
        invalid = reject_extra_payload(tool, payload, {"fault", "channel"})
        if invalid is not None:
            return self._write_report(invalid)
        session_result = self._containment_session(adapter_id, tool)
        if not session_result["ok"]:
            return self._write_report(session_result)
        session = session_result["session"]
        params: JsonObject = {}
        if payload.get("fault") is not None:
            fault = self._allowed_fault(session, tool, payload.get("fault"), required=False)
            if not fault["ok"]:
                return self._write_report(fault)
            params["fault"] = fault["fault"]
        optional_channel = self._optional_channel(session, tool, payload.get("channel"))
        if not optional_channel["ok"]:
            return self._write_report(optional_channel)
        if optional_channel.get("channel") is not None:
            params["channel"] = optional_channel["channel"]
        return self._bridge_action(session, tool, "clear_fault", params)

    def measure(self, adapter_id: str, payload: JsonObject) -> JsonObject:
        tool = "adapter_measure"
        invalid = reject_extra_payload(tool, payload, {"channel"})
        if invalid is not None:
            return self._write_report(invalid)
        if not self.config.permissions.allow_adapter_read:
            return self._write_report(self._permission_denied(tool, "Test adapter reading is disabled by the authoritative config.", adapter_id))
        session_result = self._active_session(adapter_id, tool)
        if not session_result["ok"]:
            return self._write_report(session_result)
        session = session_result["session"]
        channel = self._allowed_channel(session, tool, payload.get("channel"))
        if not channel["ok"]:
            return self._write_report(channel)
        return self._bridge_action(session, tool, "measure", {"channel": channel["channel"]})

    def close(self) -> None:
        errors: list[tuple[str, BaseException]] = []
        interrupt: BaseException | None = None
        for adapter_id in list(self.sessions):
            try:
                result = self.session_stop(adapter_id)
                if not overall_success(result):
                    raise RuntimeError(str(result.get("summary", "Test adapter cleanup failed.")))
            except BaseException as error:
                errors.append((adapter_id, error))
                if interrupt is None and isinstance(error, (KeyboardInterrupt, SystemExit)):
                    interrupt = error
        if self._owns_coordinator and not errors:
            self.coordinator.close()
        if interrupt is not None:
            interrupt.args = (*interrupt.args, "Cleanup errors: " + "; ".join(f"{adapter_id}: {type(error).__name__}: {error}" for adapter_id, error in errors))
            raise interrupt
        if errors:
            details = "; ".join(f"{adapter_id}: {type(error).__name__}: {error}" for adapter_id, error in errors)
            raise RuntimeError(f"Test adapter cleanup failed: {details}") from errors[0][1]

    def _bridge_action(self, session: AdapterSession, tool: str, method: str, params: JsonObject) -> JsonObject:
        response = session.bridge.request(method, params, session.adapter_config.timeout_s)
        if not response.get("ok"):
            result = {"tool": tool, "adapter_id": session.adapter_id, "log_path": display_path(self.config, session.log_path), **response}
            result.setdefault("error_type", "adapter_bridge_error")
            result.setdefault("summary", "Test adapter bridge reported an error.")
            if result.get("side_effect_committed") is not False and result.get("side_effect_status") is None:
                result.update({"side_effect_status": "unknown", "cleanup_required": True})
            audit_error = append_jsonl(session.log_path, {"event": "error", "method": method, **result})
            return self._write_report(mark_audit_failure(result, audit_error) if audit_error is not None else result)
        allowed_fields = {
            "set_value": {"ok", "backend", "summary", "channel", "value", "unit"},
            "inject_fault": {"ok", "backend", "summary", "fault"},
            "clear_fault": {"ok", "backend", "summary", "fault"},
            "measure": {"ok", "backend", "summary", "channel", "value", "unit", "fault"},
        }[method]
        value = response.get("value")
        valid_value = value is None or isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
        valid_measurement = method != "measure" or value is not None
        valid_strings = all(field not in response or isinstance(response[field], str) for field in ("backend", "summary", "channel", "unit"))
        valid_fault = "fault" not in response or response["fault"] is None or isinstance(response["fault"], str)
        if set(response) - allowed_fields or not valid_value or not valid_measurement or not valid_strings or not valid_fault:
            invalid = {"ok": False, "tool": tool, "adapter_id": session.adapter_id, "error_type": "adapter_bridge_invalid_response", "summary": f"Test adapter bridge returned an invalid {method} response.", "side_effect_status": "unknown", "cleanup_required": True}
            audit_error = append_jsonl(session.log_path, {"event": "error", "method": method, **invalid})
            return self._write_report(mark_audit_failure(invalid, audit_error) if audit_error is not None else invalid)
        result = {"ok": True, "tool": tool, "adapter_id": session.adapter_id, **params, "adapter_result": public_backend_result(response), "log_path": display_path(self.config, session.log_path), "summary": f"Test adapter {method} completed."}
        if "value" in response:
            result["value"] = response["value"]
        if "unit" in response:
            result["unit"] = response["unit"]
        audit_error = append_jsonl(session.log_path, {"event": method, **params, "adapter_result": public_backend_result(response)})
        return self._write_report(mark_audit_failure(result, audit_error) if audit_error is not None else result)

    def _configured_adapter(self, adapter_id: str, tool: str) -> JsonObject:
        if not adapter_id:
            return {"ok": False, "tool": tool, "error_type": "invalid_argument", "summary": "adapter_id is required."}
        adapter_config = self.config.adapters.get(adapter_id)
        if adapter_config is None:
            return {"ok": False, "tool": tool, "adapter_id": adapter_id, "error_type": "adapter_not_configured", "summary": "Test adapter is not available in the authoritative config.", "configured_adapters": sorted(self.config.adapters.keys())}
        return {"ok": True, "adapter_config": adapter_config}

    def _active_session(self, adapter_id: str, tool: str) -> JsonObject:
        adapter = self._configured_adapter(adapter_id, tool)
        if not adapter["ok"]:
            return adapter
        session = self.sessions.get(adapter_id)
        if session is None or self.coordinator.blocked or session.audit_broken or session.lease.state != "active" or not self._session_is_active(session):
            if session is not None and (self.coordinator.blocked or session.audit_broken or session.lease.state != "active"):
                return {"ok": False, "tool": tool, "adapter_id": adapter_id, "error_type": "resource_quarantined", "summary": "Test adapter requires cleanup or audit recovery before further actions.", "cleanup_required": True, "quarantined": True}
            return {"ok": False, "tool": tool, "adapter_id": adapter_id, "error_type": "session_not_active", "summary": "Test adapter session is not active. Start it with adapter_session_start first."}
        return {"ok": True, "session": session}

    def _writable_session(self, adapter_id: str, tool: str) -> JsonObject:
        if not self.config.permissions.allow_adapter_write:
            return self._permission_denied(tool, "Test adapter writing is disabled by the authoritative config.", adapter_id)
        return self._active_session(adapter_id, tool)

    def _containment_session(self, adapter_id: str, tool: str) -> JsonObject:
        if not self.config.permissions.allow_adapter_write:
            return self._permission_denied(tool, "Test adapter writing is disabled by the authoritative config.", adapter_id)
        adapter = self._configured_adapter(adapter_id, tool)
        if not adapter["ok"]:
            return adapter
        session = self.sessions.get(adapter_id)
        if session is None or session.bridge.status().get("active") is False:
            return {"ok": False, "tool": tool, "adapter_id": adapter_id, "error_type": "session_not_active", "summary": "Test adapter session is not available for containment."}
        return {"ok": True, "session": session}

    def _allowed_channel(self, session: AdapterSession, tool: str, channel: object) -> JsonObject:
        if not isinstance(channel, str) or not channel:
            return {"ok": False, "tool": tool, "adapter_id": session.adapter_id, "error_type": "invalid_argument", "summary": "channel must be a non-empty string."}
        if channel not in session.adapter_config.channels:
            return {"ok": False, "tool": tool, "adapter_id": session.adapter_id, "channel": channel, "error_type": "channel_not_configured", "summary": "Channel is not available for this test adapter in the authoritative config.", "configured_channels": session.adapter_config.channels}
        return {"ok": True, "channel": channel}

    def _optional_channel(self, session: AdapterSession, tool: str, channel: object) -> JsonObject:
        if channel is None:
            return {"ok": True, "channel": None}
        return self._allowed_channel(session, tool, channel)

    def _allowed_fault(self, session: AdapterSession, tool: str, fault: object, required: bool) -> JsonObject:
        if not isinstance(fault, str) or not fault:
            summary = "fault must be a non-empty string." if required else "fault must be a non-empty string when provided."
            return {"ok": False, "tool": tool, "adapter_id": session.adapter_id, "error_type": "invalid_argument", "summary": summary}
        if fault not in session.adapter_config.faults:
            return {"ok": False, "tool": tool, "adapter_id": session.adapter_id, "fault": fault, "error_type": "fault_not_configured", "summary": "Fault is not available for this test adapter in the authoritative config.", "configured_faults": session.adapter_config.faults}
        return {"ok": True, "fault": fault}

    def _adapter_status(self, adapter_config: AdapterConfig, session: AdapterSession | None) -> JsonObject:
        result: JsonObject = {"executable": adapter_config.executable, "channels": adapter_config.channels, "faults": adapter_config.faults, "timeout_s": adapter_config.timeout_s, "session_active": False}
        if session is not None:
            result.update(self._session_status(session))
        return result

    def _session_status(self, session: AdapterSession) -> JsonObject:
        return {"session_active": self._session_is_active(session), "started_at": session.started_at, "bridge_status": session.bridge.status(), "log_path": display_path(self.config, session.log_path)}

    def _session_is_active(self, session: AdapterSession) -> bool:
        bridge_status = session.bridge.status()
        return session.active and bridge_status.get("active") is not False and bridge_status.get("cleanup_required") is not True

    def _stop_session(self, session: AdapterSession, reason: str, *, defer_release: bool = False) -> Exception | None:
        try:
            close_result = session.bridge.close()
        except BaseException as error:
            session.active = False
            details = error.result if isinstance(error, BridgeCleanupError) else error
            session.lease.quarantine("adapter_bridge_cleanup_unconfirmed", details)
            raise
        session.active = False
        session.safe_state_confirmed = close_result.get("safe_state_confirmed") is True
        session.process_reaped = close_result.get("process_reaped") is True
        audit_error = append_jsonl(session.log_path, {"event": "stop", "reason": reason})
        if audit_error is not None or session.audit_broken:
            session.audit_broken = True
            session.lease.quarantine("adapter_audit_broken", audit_error, audit_broken=True)
            raise RuntimeError("Test adapter audit state is broken; resource remains quarantined.")
        elif not defer_release:
            released = session.lease.release(
                safe_state_confirmed=session.safe_state_confirmed,
                processes_reaped=session.process_reaped,
            )
            if not released:
                raise RuntimeError("Test adapter safe-state release remains unconfirmed.")
        return audit_error

    def _close_failure(self, tool: str, adapter_id: str, error: Exception) -> JsonObject:
        return {
            "ok": False,
            "tool": tool,
            "adapter_id": adapter_id,
            "error_type": "adapter_bridge_close_failed",
            "summary": "Test adapter session could not be closed and remains registered for cleanup retry.",
            "backend_error": str(error),
        }

    def _write_report(self, result: JsonObject) -> JsonObject:
        prepared = mark_side_effect(result)
        adapter_id = prepared.get("adapter_id")
        session = self.sessions.get(adapter_id) if isinstance(adapter_id, str) else None
        unsafe_effect = prepared.get("side_effect_status") in {"unknown", "partial"}
        if session is not None and unsafe_effect:
            session.lease.quarantine("adapter_effect_unconfirmed")
        if session is not None:
            prepared = {**prepared, **session.lease.status()}
        written = write_report(self.config, prepared)
        if session is not None and written.get("audit_ok") is False:
            session.audit_broken = True
            session.lease.quarantine("adapter_report_audit_broken", audit_broken=True)
            written = write_report(self.config, {**written, **session.lease.status()})
        return written

    def _write_unattached_lease_report(self, result: JsonObject, lease: HardwareLease, *, release_if_safe: bool) -> JsonObject:
        written = write_report(self.config, {**mark_side_effect(result), **lease.status()})
        if written.get("audit_ok") is False:
            lease.quarantine("adapter_report_audit_broken", audit_broken=True)
            return write_report(self.config, {**written, **lease.status()})
        if release_if_safe and not lease.release():
            return write_report(self.config, {**written, **lease.status(), "ok": False, "cleanup_required": True, "summary": "Adapter lease release remained unconfirmed."})
        return {**written, **lease.status()}

    def _permission_denied(self, tool: str, summary: str, adapter_id: str | None = None) -> JsonObject:
        result: JsonObject = {"ok": False, "tool": tool, "error_type": "permission_denied", "summary": summary}
        if adapter_id:
            result["adapter_id"] = adapter_id
        return result


def open_adapter_bridge(config: AgenticHILConfig, adapter_id: str, adapter_config: AdapterConfig) -> JsonObject:
    executable = resolve_work_path(config, adapter_config.executable)
    if not Path(executable).is_file():
        return {"ok": False, "tool": "adapter_session_start", "adapter_id": adapter_id, "error_type": "adapter_bridge_not_found", "summary": "Test adapter bridge executable could not be found.", "executable": adapter_config.executable, "side_effect_committed": False}
    command = invocation(executable)
    try:
        child = spawn_managed_process(command, cwd=str(Path(executable).parent), text=True, encoding="utf-8", errors="replace", stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **process_group_kwargs())
    except OSError as error:
        return {"ok": False, "tool": "adapter_session_start", "adapter_id": adapter_id, "error_type": "adapter_bridge_process_start_failed", "summary": "Test adapter bridge process could not be started.", "backend_error": str(error), "side_effect_committed": False}
    session = AdapterBridgeSession(child)
    try:
        opened = session.request("open", {"channels": adapter_config.channels, "faults": adapter_config.faults}, adapter_config.timeout_s)
    except BaseException as primary_error:
        try:
            session.close()
        except BaseException as cleanup_error:
            primary_error.args = (*primary_error.args, f"Bridge cleanup error: {cleanup_error}")
        raise
    valid_open = (
        opened.get("ok") is True
        and opened.get("protocol_version") == BRIDGE_PROTOCOL_VERSION
        and not set(opened) - {"ok", "protocol_version", "backend", "summary", "channels", "faults"}
        and all(field not in opened or isinstance(opened[field], str) for field in ("backend", "summary"))
        and all(field not in opened or isinstance(opened[field], list) and all(isinstance(item, str) for item in opened[field]) for field in ("channels", "faults"))
    )
    if not valid_open:
        if opened.get("ok") is True:
            opened = {"ok": False, "error_type": "adapter_bridge_protocol_unsupported", "summary": "Test adapter bridge must return a valid protocol version 2 open response."}
        try:
            session.close()
        except BridgeCleanupError as cleanup_error:
            return {"tool": "adapter_session_start", "adapter_id": adapter_id, "command": command_for_log(command), **opened, "cleanup_required": True, "cleanup_error": cleanup_error.result, "session": session}
        return {"tool": "adapter_session_start", "adapter_id": adapter_id, "command": command_for_log(command), **opened, "cleanup_confirmed": True, "side_effect_committed": False}
    return {"ok": True, "tool": "adapter_session_start", "adapter_id": adapter_id, "command": command_for_log(command), "backend": opened.get("backend", "process"), "session": session, "summary": "Test adapter bridge opened."}


def reject_extra_payload(tool: str, payload: JsonObject, allowed: set[str]) -> JsonObject | None:
    if set(payload) - allowed:
        return {"ok": False, "tool": tool, "error_type": "invalid_argument", "summary": "Test adapter payload contains unsupported fields.", "side_effect_committed": False}
    return None
