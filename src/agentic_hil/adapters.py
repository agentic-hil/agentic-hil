from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path

from agentic_hil.backends.common import command_for_log, invocation
from agentic_hil.bridge import BridgeCloseResult, ProcessBridgeSession, public_backend_result, reap_unmanaged_child
from agentic_hil.config import display_path, resolve_work_path
from agentic_hil.report import (
    append_jsonl,
    logs_directory,
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
    def __init__(self, adapter_id: str, adapter_config: AdapterConfig, bridge: AdapterBridgeSession, log_path: str):
        self.adapter_id = adapter_id
        self.adapter_config = adapter_config
        self.bridge = bridge
        self.log_path = log_path
        self.started_at = utc_now_iso()
        self.active = True
        self.cleanup_unconfirmed = False
        self.close_confirmed = False


class AdapterService:
    def __init__(self, config: AgenticHILConfig):
        self.config = config
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
            return self._write_report(self._permission_denied("adapter_session_start", "Test adapter reading and writing are disabled by .agentic-hil/config.yaml.", adapter_id))
        existing = self.sessions.get(adapter_id)
        if existing and self._session_is_active(existing):
            return self._write_report({"ok": True, "tool": "adapter_session_start", "adapter_id": adapter_id, "already_active": True, "session": self._session_status(existing), "summary": "Test adapter session is already active."})
        if existing:
            self._stop_session(existing, "restart")
            self.sessions.pop(adapter_id, None)
        log_path = str(Path(logs_directory(self.config)) / f"adapter-{timestamp_for_filename()}-{safe_filename(adapter_id, 'adapter')}.jsonl")
        provisional: AdapterSession | None = None

        def register_provisional(bridge: AdapterBridgeSession) -> None:
            nonlocal provisional
            provisional = AdapterSession(adapter_id, adapter["adapter_config"], bridge, log_path)
            self.sessions[adapter_id] = provisional

        try:
            opened = open_adapter_bridge(self.config, adapter_id, adapter["adapter_config"], register_provisional)
        except BaseException as error:
            if provisional is not None:
                close_result = provisional.bridge.last_close_result
                provisional.active = False
                provisional.close_confirmed = close_result is not None and close_result.cleanup_confirmed
                provisional.cleanup_unconfirmed = not provisional.close_confirmed
                if not provisional.cleanup_unconfirmed:
                    self.sessions.pop(adapter_id, None)
                    error._agentic_hil_completion_confirmed = True
            raise
        if not opened["ok"]:
            failed_bridge = opened.get("session")
            if provisional is not None:
                provisional.active = False
                provisional.cleanup_unconfirmed = bool(opened.get("cleanup_unconfirmed"))
                provisional.close_confirmed = not provisional.cleanup_unconfirmed
                if not provisional.cleanup_unconfirmed:
                    self.sessions.pop(adapter_id, None)
            elif failed_bridge is not None:
                failed_session = AdapterSession(adapter_id, adapter["adapter_config"], failed_bridge, log_path)
                failed_session.cleanup_unconfirmed = bool(opened.get("cleanup_unconfirmed"))
                self.sessions[adapter_id] = failed_session
            return self._write_report(public_backend_result(opened))
        if provisional is None:
            bridge = opened["session"]
            provisional = AdapterSession(adapter_id, adapter["adapter_config"], bridge, log_path)
            self.sessions[adapter_id] = provisional
        session = provisional
        try:
            append_jsonl(session.log_path, {"event": "start", "adapter_id": adapter_id, "executable": session.adapter_config.executable})
            return self._write_report({"ok": True, "tool": "adapter_session_start", "adapter_id": adapter_id, "already_active": False, "adapter_result": public_backend_result(opened), "session": self._session_status(session), "summary": "Test adapter session started."})
        except BaseException as error:
            cleanup_confirmed = False
            try:
                self._stop_session(session, "start_failed")
            except BaseException:
                cleanup_confirmed = session.close_confirmed
            else:
                self.sessions.pop(adapter_id, None)
                cleanup_confirmed = True
            if cleanup_confirmed:
                self.sessions.pop(adapter_id, None)
                error._agentic_hil_completion_confirmed = True
            raise

    def session_stop(self, adapter_id: str) -> JsonObject:
        adapter = self._configured_adapter(adapter_id, "adapter_session_stop")
        if not adapter["ok"]:
            return self._write_report(adapter)
        session = self.sessions.get(adapter_id)
        if session is None:
            try:
                return self._write_report({"ok": True, "tool": "adapter_session_stop", "adapter_id": adapter_id, "was_active": False, "summary": "Test adapter session was not active."})
            except BaseException as error:
                error._agentic_hil_completion_confirmed = True
                raise
        try:
            self._stop_session(session, "requested")
        except BaseException as error:
            if session.close_confirmed:
                self.sessions.pop(adapter_id, None)
                error._agentic_hil_completion_confirmed = True
            raise
        self.sessions.pop(adapter_id, None)
        try:
            return self._write_report({"ok": True, "tool": "adapter_session_stop", "adapter_id": adapter_id, "was_active": True, "session": self._session_status(session), "summary": "Test adapter session stopped."})
        except BaseException as error:
            error._agentic_hil_completion_confirmed = True
            raise

    def set_value(self, adapter_id: str, payload: JsonObject) -> JsonObject:
        tool = "adapter_set_value"
        session_result = self._writable_session(adapter_id, tool)
        if not session_result["ok"]:
            return self._write_report(session_result)
        session = session_result["session"]
        channel = self._allowed_channel(session, tool, payload.get("channel"))
        if not channel["ok"]:
            return self._write_report(channel)
        value = payload.get("value")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return self._write_report({"ok": False, "tool": tool, "adapter_id": adapter_id, "error_type": "invalid_argument", "summary": "value must be a number."})
        unit = payload.get("unit")
        if unit is not None and not isinstance(unit, str):
            return self._write_report({"ok": False, "tool": tool, "adapter_id": adapter_id, "error_type": "invalid_argument", "summary": "unit must be a string."})
        params: JsonObject = {"channel": channel["channel"], "value": value}
        if unit is not None:
            params["unit"] = unit
        return self._bridge_action(session, tool, "set_value", params)

    def inject_fault(self, adapter_id: str, payload: JsonObject) -> JsonObject:
        tool = "adapter_inject_fault"
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
        session_result = self._writable_session(adapter_id, tool)
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
        if not self.config.permissions.allow_adapter_read:
            return self._write_report(self._permission_denied(tool, "Test adapter reading is disabled by .agentic-hil/config.yaml.", adapter_id))
        session_result = self._active_session(adapter_id, tool)
        if not session_result["ok"]:
            return self._write_report(session_result)
        session = session_result["session"]
        channel = self._allowed_channel(session, tool, payload.get("channel"))
        if not channel["ok"]:
            return self._write_report(channel)
        return self._bridge_action(session, tool, "measure", {"channel": channel["channel"]})

    def close(self) -> None:
        first_error: Exception | None = None
        pending_base_exception: BaseException | None = None
        for adapter_id, session in list(self.sessions.items()):
            try:
                self._stop_session(session, "shutdown")
            except BaseException as error:
                if isinstance(error, Exception) and first_error is None:
                    first_error = error
                elif not isinstance(error, Exception) and pending_base_exception is None:
                    pending_base_exception = error
                if session.close_confirmed:
                    self.sessions.pop(adapter_id, None)
            else:
                self.sessions.pop(adapter_id, None)
        if pending_base_exception is not None:
            raise pending_base_exception
        if first_error is not None:
            raise first_error

    def has_active_sessions(self) -> bool:
        return bool(self.active_session_ids())

    def active_session_ids(self) -> list[str]:
        return [adapter_id for adapter_id, session in self.sessions.items() if not session.close_confirmed]

    def cleanup_inspection_errors(self) -> list[JsonObject]:
        errors: list[JsonObject] = []
        for adapter_id, session in self.sessions.items():
            if session.close_confirmed:
                continue
            try:
                bridge_active = session.bridge.status().get("active") is not False
            except Exception as error:
                session.cleanup_unconfirmed = True
                errors.append({"id": adapter_id, "error": f"Adapter bridge status could not be inspected: {error}"})
                continue
            close_result = getattr(session.bridge, "last_close_result", None)
            if session.cleanup_unconfirmed:
                errors.append({"id": adapter_id, "error": "Physical safe state was not confirmed during adapter cleanup."})
            elif not bridge_active and (close_result is None or not close_result.cleanup_confirmed):
                session.cleanup_unconfirmed = True
                errors.append({"id": adapter_id, "error": "Adapter bridge exited without confirmed physical safe state."})
        return errors

    def _bridge_action(self, session: AdapterSession, tool: str, method: str, params: JsonObject) -> JsonObject:
        response = session.bridge.request(method, params, session.adapter_config.timeout_s)
        if not response.get("ok"):
            result = {"tool": tool, "adapter_id": session.adapter_id, "log_path": display_path(self.config, session.log_path), **response}
            result.setdefault("error_type", "adapter_bridge_error")
            result.setdefault("summary", "Test adapter bridge reported an error.")
            append_jsonl(session.log_path, {"event": "error", "method": method, **result})
            return self._write_report(result)
        result = {"ok": True, "tool": tool, "adapter_id": session.adapter_id, **params, "adapter_result": public_backend_result(response), "log_path": display_path(self.config, session.log_path), "summary": f"Test adapter {method} completed."}
        if "value" in response:
            result["value"] = response["value"]
        if "unit" in response:
            result["unit"] = response["unit"]
        append_jsonl(session.log_path, {"event": method, **params, "adapter_result": public_backend_result(response)})
        return self._write_report(result)

    def _configured_adapter(self, adapter_id: str, tool: str) -> JsonObject:
        if not adapter_id:
            return {"ok": False, "tool": tool, "error_type": "invalid_argument", "summary": "adapter_id is required."}
        adapter_config = self.config.adapters.get(adapter_id)
        if adapter_config is None:
            return {"ok": False, "tool": tool, "adapter_id": adapter_id, "error_type": "adapter_not_configured", "summary": "Test adapter is not configured in .agentic-hil/config.yaml.", "configured_adapters": sorted(self.config.adapters.keys())}
        return {"ok": True, "adapter_config": adapter_config}

    def _active_session(self, adapter_id: str, tool: str) -> JsonObject:
        adapter = self._configured_adapter(adapter_id, tool)
        if not adapter["ok"]:
            return adapter
        session = self.sessions.get(adapter_id)
        if session is None:
            return {"ok": False, "tool": tool, "adapter_id": adapter_id, "error_type": "session_not_active", "summary": "Test adapter session is not active. Start it with adapter_session_start first."}
        if not self._session_is_active(session):
            if session.cleanup_unconfirmed:
                return {"ok": False, "tool": tool, "adapter_id": adapter_id, "error_type": "hardware_state_unconfirmed", "summary": "Adapter bridge exited without confirmed physical safe state."}
            return {"ok": False, "tool": tool, "adapter_id": adapter_id, "error_type": "session_not_active", "summary": "Test adapter session is not active. Start it with adapter_session_start first."}
        return {"ok": True, "session": session}

    def _writable_session(self, adapter_id: str, tool: str) -> JsonObject:
        if not self.config.permissions.allow_adapter_write:
            return self._permission_denied(tool, "Test adapter writing is disabled by .agentic-hil/config.yaml.", adapter_id)
        return self._active_session(adapter_id, tool)

    def _allowed_channel(self, session: AdapterSession, tool: str, channel: object) -> JsonObject:
        if not isinstance(channel, str) or not channel:
            return {"ok": False, "tool": tool, "adapter_id": session.adapter_id, "error_type": "invalid_argument", "summary": "channel must be a non-empty string."}
        if channel not in session.adapter_config.channels:
            return {"ok": False, "tool": tool, "adapter_id": session.adapter_id, "channel": channel, "error_type": "channel_not_configured", "summary": "Channel is not configured for this test adapter in .agentic-hil/config.yaml.", "configured_channels": session.adapter_config.channels}
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
            return {"ok": False, "tool": tool, "adapter_id": session.adapter_id, "fault": fault, "error_type": "fault_not_configured", "summary": "Fault is not configured for this test adapter in .agentic-hil/config.yaml.", "configured_faults": session.adapter_config.faults}
        return {"ok": True, "fault": fault}

    def _adapter_status(self, adapter_config: AdapterConfig, session: AdapterSession | None) -> JsonObject:
        result: JsonObject = {"executable": adapter_config.executable, "channels": adapter_config.channels, "faults": adapter_config.faults, "timeout_s": adapter_config.timeout_s, "session_active": False}
        if session is not None:
            result.update(self._session_status(session))
        return result

    def _session_status(self, session: AdapterSession) -> JsonObject:
        return {"session_active": self._session_is_active(session), "cleanup_unconfirmed": session.cleanup_unconfirmed, "close_confirmed": session.close_confirmed, "started_at": session.started_at, "bridge_status": session.bridge.status(), "log_path": display_path(self.config, session.log_path)}

    def _session_is_active(self, session: AdapterSession) -> bool:
        if session.close_confirmed or session.cleanup_unconfirmed or not session.active:
            return False
        try:
            bridge_active = session.bridge.status().get("active") is not False
        except Exception:
            session.cleanup_unconfirmed = True
            return False
        if not bridge_active:
            close_result = getattr(session.bridge, "last_close_result", None)
            if close_result is None or not close_result.cleanup_confirmed:
                session.cleanup_unconfirmed = True
            return False
        return True

    def _stop_session(self, session: AdapterSession, reason: str) -> None:
        session.active = False
        try:
            close_result = session.bridge.close()
        except BaseException:
            close_result = getattr(session.bridge, "last_close_result", None)
            session.close_confirmed = close_result is not None and close_result.cleanup_confirmed
            session.cleanup_unconfirmed = not session.close_confirmed
            raise
        if not isinstance(close_result, BridgeCloseResult) or not close_result.cleanup_confirmed:
            session.cleanup_unconfirmed = True
            errors = close_result.errors if isinstance(close_result, BridgeCloseResult) else ["bridge returned no structured close result"]
            raise RuntimeError("Test adapter bridge cleanup did not confirm a physical safe state: " + "; ".join(errors))
        if session.bridge.status().get("active") is not False:
            session.cleanup_unconfirmed = True
            raise RuntimeError("Test adapter bridge remained active after close.")
        session.close_confirmed = True
        append_jsonl(session.log_path, {"event": "stop", "reason": reason})

    def _write_report(self, result: JsonObject) -> JsonObject:
        return write_report(self.config, result)

    def _permission_denied(self, tool: str, summary: str, adapter_id: str | None = None) -> JsonObject:
        result: JsonObject = {"ok": False, "tool": tool, "error_type": "permission_denied", "summary": summary}
        if adapter_id:
            result["adapter_id"] = adapter_id
        return result


def open_adapter_bridge(
    config: AgenticHILConfig,
    adapter_id: str,
    adapter_config: AdapterConfig,
    on_started: Callable[[AdapterBridgeSession], None] | None = None,
) -> JsonObject:
    executable = resolve_work_path(config, adapter_config.executable)
    if not Path(executable).is_file():
        return {"ok": False, "tool": "adapter_session_start", "adapter_id": adapter_id, "error_type": "adapter_bridge_not_found", "summary": "Test adapter bridge executable could not be found.", "executable": adapter_config.executable}
    command = [*invocation(executable), *adapter_config.args]
    try:
        child = subprocess.Popen(command, cwd=config.work_dir, text=True, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except OSError as error:
        return {"ok": False, "tool": "adapter_session_start", "adapter_id": adapter_id, "error_type": "adapter_bridge_process_start_failed", "summary": "Test adapter bridge process could not be started.", "backend_error": str(error)}
    try:
        session = AdapterBridgeSession(child)
    except BaseException:
        reap_unmanaged_child(child)
        raise
    try:
        if on_started is not None:
            on_started(session)
        opened = session.request("open", {"channels": adapter_config.channels, "faults": adapter_config.faults}, adapter_config.timeout_s)
    except BaseException as error:
        try:
            close_result = session.close()
        except BaseException:
            close_result = session.last_close_result
        if close_result is not None and close_result.cleanup_confirmed:
            error._agentic_hil_completion_confirmed = True
        raise
    if not opened.get("ok"):
        result: JsonObject = {"tool": "adapter_session_start", "adapter_id": adapter_id, "command": command_for_log(command), **opened}
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
    return {"ok": True, "tool": "adapter_session_start", "adapter_id": adapter_id, "command": command_for_log(command), "backend": opened.get("backend", "process"), "session": session, "summary": "Test adapter bridge opened."}
