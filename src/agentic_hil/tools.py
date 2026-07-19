from __future__ import annotations

import json
import math
import threading
from collections.abc import Callable
from contextlib import suppress
from dataclasses import replace
from pathlib import Path

from agentic_hil.adapters import AdapterService
from agentic_hil.artifacts import ArtifactManager
from agentic_hil.can import CanBusService
from agentic_hil.comports import ComPortService
from agentic_hil.config import ConfigError, load_config
from agentic_hil.debugger import DebuggerBackend, create_debugger_backend
from agentic_hil.hardware_lock import HardwareLockError, HardwareQuarantinedError, ProjectHardwareLock
from agentic_hil.report import AuditWriteError, read_last_report, utc_now_iso
from agentic_hil.types import AgenticHILConfig, JsonObject

HARDWARE_TOOLS = {
    "debugger_probes_list",
    "probe_target",
    "flash_firmware",
    "reset_target",
    "debug_start_session",
    "debug_stop_session",
    "debug_get_session_status",
    "debug_set_breakpoint",
    "debug_list_breakpoints",
    "debug_clear_breakpoints",
    "debug_continue",
    "debug_halt",
    "debug_get_stop_reason",
    "debug_symbol_info",
    "debug_dump_symbol_ihex",
    "com_session_start",
    "com_session_stop",
    "com_write",
    "com_read",
    "can_session_start",
    "can_session_stop",
    "can_send",
    "can_read",
    "adapter_session_start",
    "adapter_session_stop",
    "adapter_set_value",
    "adapter_inject_fault",
    "adapter_clear_fault",
    "adapter_measure",
}

INDETERMINATE_TIMEOUT_TOOLS = {
    "probe_target",
    "flash_firmware",
    "reset_target",
    "debug_set_breakpoint",
    "debug_clear_breakpoints",
    "debug_continue",
    "debug_halt",
}
INDETERMINATE_TRANSPORT_ERRORS = {
    "serial_write_failed",
    "can_send_failed",
    "can_adapter_timeout",
    "can_adapter_process_exited",
    "can_adapter_invalid_response",
    "adapter_bridge_timeout",
    "adapter_bridge_process_exited",
    "adapter_bridge_invalid_response",
}
DEBUG_SESSION_CONFLICT_TOOLS = {"probe_target", "flash_firmware", "reset_target"}
CLEANUP_TOOLS = {"debug_stop_session", "com_session_stop", "can_session_stop", "adapter_session_stop"}
SAFE_DIAGNOSTIC_TOOLS = {"get_last_report", "classify_last_error"}
NO_HARDWARE_ERROR_TYPES = {
    "permission_denied", "invalid_argument", "unknown_tool", "not_supported", "hardware_busy",
    "session_not_active", "session_already_active", "debug_session_active", "stop_reason_not_available",
    "com_port_not_configured", "can_bus_not_configured", "adapter_not_configured",
    "serial_backend_not_available", "can_backend_not_available", "can_adapter_not_found", "adapter_bridge_not_found",
    "debugger_not_found", "gdb_not_found", "debugger_config_not_found", "report_not_found",
    "artifact_validation_failed", "artifact_not_found", "artifact_too_large", "artifact_staging_failed",
    "output_validation_failed", "config_invalid", "hardware_lock_failed", "log_directory_unavailable",
}
TOOL_ARGUMENTS: dict[str, set[str]] = {
    "debugger_info": set(), "debugger_probes_list": {"debugger"}, "probe_target": set(), "artifact_upload": {"image_path", "filename", "data_base64"},
    "flash_firmware": {"image_path", "artifact_id", "reset_after_flash"}, "reset_target": {"mode"},
    "debug_start_session": {"image_path", "artifact_id", "mode", "timeout_s"}, "debug_stop_session": {"timeout_s"}, "debug_get_session_status": set(),
    "debug_set_breakpoint": {"location"}, "debug_list_breakpoints": set(), "debug_clear_breakpoints": set(), "debug_continue": {"timeout_s"}, "debug_halt": {"timeout_s"},
    "debug_get_stop_reason": set(), "debug_symbol_info": {"symbol"}, "debug_dump_symbol_ihex": {"symbol", "output_path"}, "get_last_report": set(), "classify_last_error": set(),
    "com_ports_list": set(), "com_session_start": {"port_id", "clear_buffer"}, "com_session_stop": {"port_id"}, "com_write": {"port_id", "text", "hex"}, "com_read": {"port_id", "max_bytes", "wait_timeout_s"},
    "can_buses_list": set(), "can_session_start": {"bus_id", "clear_rx_queue"}, "can_session_stop": {"bus_id"}, "can_send": {"bus_id", "frame_id", "id", "extended", "rtr", "data_hex", "hex"}, "can_read": {"bus_id", "max_frames", "wait_timeout_s"},
    "adapters_list": set(), "adapter_session_start": {"adapter_id"}, "adapter_session_stop": {"adapter_id"}, "adapter_set_value": {"adapter_id", "channel", "value", "unit"},
    "adapter_inject_fault": {"adapter_id", "fault", "channel"}, "adapter_clear_fault": {"adapter_id", "fault", "channel"}, "adapter_measure": {"adapter_id", "channel"},
}
REQUIRED_TOOL_ARGUMENTS: dict[str, set[str]] = {
    "debug_set_breakpoint": {"location"}, "debug_symbol_info": {"symbol"}, "debug_dump_symbol_ihex": {"symbol", "output_path"},
    "com_session_start": {"port_id"}, "com_session_stop": {"port_id"}, "com_write": {"port_id"}, "com_read": {"port_id"},
    "can_session_start": {"bus_id"}, "can_session_stop": {"bus_id"}, "can_send": {"bus_id"}, "can_read": {"bus_id"},
    "adapter_session_start": {"adapter_id"}, "adapter_session_stop": {"adapter_id"}, "adapter_set_value": {"adapter_id", "channel", "value"},
    "adapter_inject_fault": {"adapter_id", "fault"}, "adapter_clear_fault": {"adapter_id"}, "adapter_measure": {"adapter_id", "channel"},
}


class HardwareCleanupError(RuntimeError):
    def __init__(self, errors: list[tuple[str, Exception]]):
        self.errors = errors
        details = "; ".join(f"{name}: {error}" for name, error in errors)
        super().__init__(f"Hardware cleanup failed ({details}).")


class AgenticHILToolService:
    def __init__(
        self,
        config: AgenticHILConfig,
        backend: DebuggerBackend | None = None,
        artifacts: ArtifactManager | None = None,
        com_ports: ComPortService | None = None,
        can_buses: CanBusService | None = None,
        adapters: AdapterService | None = None,
        reload_config: bool = True,
        hardware_owner: str | None = None,
    ):
        self.config = config
        self.reload_config = reload_config
        self.hardware_owner = hardware_owner
        self._hardware_lock = ProjectHardwareLock(config.config_path)
        self.backend = backend or create_debugger_backend(config)
        self.artifacts = artifacts or ArtifactManager(config)
        self.com_ports = com_ports or ComPortService(config)
        self.can_buses = can_buses or CanBusService(config)
        self.adapters = adapters or AdapterService(config)
        self._hardware_call_guard = threading.RLock()
        self._operation_in_flight: JsonObject | None = None
        self._unconfirmed_operations: list[JsonObject] = []
        self._poisoned_state: JsonObject | None = None
        self._incident_persisted = False
        self._incident_id: str | None = None
        self._known_incident_errors: set[str] = set()

    def debugger_info(self) -> JsonObject:
        return self.backend.info()

    def debugger_probes_list(self, debugger_name: str = "default") -> JsonObject:
        if not self.config.permissions.allow_probe:
            return tool_error("debugger_probes_list", "permission_denied", "Debugger probe discovery is disabled by .agentic-hil/config.yaml.")
        if debugger_name == "default":
            return self.backend.list_probes()
        debugger = self.config.debuggers.get(debugger_name)
        if debugger is None:
            result = tool_error("debugger_probes_list", "invalid_argument", "Unknown configured debugger name.")
            result["debugger"] = debugger_name
            return result
        backend = create_debugger_backend(replace(self.config, debugger=debugger))
        try:
            result = backend.list_probes()
            result["debugger"] = debugger_name
            return result
        finally:
            backend.close()

    def probe_target(self) -> JsonObject:
        return self.backend.probe_target()

    def flash_firmware(self, payload: JsonObject | None = None) -> JsonObject:
        payload = payload or {}
        if not self.config.permissions.allow_flash:
            return tool_error("flash_firmware", "permission_denied", "Flashing is disabled by .agentic-hil/config.yaml.")
        image_path = payload.get("image_path")
        artifact_id = payload.get("artifact_id")
        if bool(image_path) == bool(artifact_id):
            return tool_error("flash_firmware", "invalid_argument", "Provide exactly one of image_path or artifact_id.")
        reset_after_flash = payload.get("reset_after_flash", False)
        if not isinstance(reset_after_flash, bool):
            return tool_error("flash_firmware", "invalid_argument", "reset_after_flash must be a boolean.")
        validation = self.artifacts.validate_local_path(str(image_path)) if image_path else self.artifacts.resolve_artifact_id(str(artifact_id))
        if not validation["ok"]:
            return validation
        return self.backend.flash_firmware(validation["artifact"], reset_after_flash)

    def artifact_upload(self, payload: JsonObject | None = None) -> JsonObject:
        return self.artifacts.upload(payload)

    def reset_target(self, mode: str = "run") -> JsonObject:
        if not self.config.permissions.allow_probe:
            return tool_error("reset_target", "permission_denied", "Target reset is disabled because permissions.allow_probe is false.")
        return self.backend.reset_target(mode)

    def debug_start_session(self, payload: JsonObject | None = None) -> JsonObject:
        payload = payload or {}
        image_path = payload.get("image_path")
        artifact_id = payload.get("artifact_id")
        if bool(image_path) == bool(artifact_id):
            return tool_error("debug_start_session", "invalid_argument", "Provide exactly one of image_path or artifact_id.")
        validation = self.artifacts.validate_local_path(str(image_path)) if image_path else self.artifacts.resolve_artifact_id(str(artifact_id), "debug_start_session")
        if not validation["ok"]:
            validation["tool"] = "debug_start_session"
            return validation
        artifact = validation["artifact"]
        if Path(str(artifact["resolved_path"])).suffix.lower() != ".elf":
            return tool_error("debug_start_session", "artifact_validation_failed", "Debug sessions require an ELF artifact with debug symbols.")
        return self.backend.debug_start_session(artifact, str(payload.get("mode", "attach")), number_argument(payload.get("timeout_s")))

    def debug_stop_session(self, payload: JsonObject | None = None) -> JsonObject:
        return self.backend.debug_stop_session(number_argument((payload or {}).get("timeout_s")))

    def debug_get_session_status(self) -> JsonObject:
        return self.backend.debug_get_session_status()

    def debug_set_breakpoint(self, payload: JsonObject | None = None) -> JsonObject:
        location = (payload or {}).get("location")
        has_symbol_location = isinstance(location, str) and bool(location.strip())
        has_typed_location = isinstance(location, dict) and bool(location)
        if not has_symbol_location and not has_typed_location:
            return tool_error("debug_set_breakpoint", "invalid_argument", "location must be a non-empty string or object.")
        return self.backend.debug_set_breakpoint({"location": location})

    def debug_list_breakpoints(self) -> JsonObject:
        return self.backend.debug_list_breakpoints()

    def debug_clear_breakpoints(self) -> JsonObject:
        return self.backend.debug_clear_breakpoints()

    def debug_continue(self, payload: JsonObject | None = None) -> JsonObject:
        return self.backend.debug_continue(number_argument((payload or {}).get("timeout_s")))

    def debug_halt(self, payload: JsonObject | None = None) -> JsonObject:
        return self.backend.debug_halt(number_argument((payload or {}).get("timeout_s")))

    def debug_get_stop_reason(self) -> JsonObject:
        return self.backend.debug_get_stop_reason()

    def debug_symbol_info(self, payload: JsonObject | None = None) -> JsonObject:
        symbol = (payload or {}).get("symbol")
        if not isinstance(symbol, str) or not symbol.strip():
            return tool_error("debug_symbol_info", "invalid_argument", "symbol must be a non-empty string.")
        return self.backend.debug_symbol_info(symbol.strip())

    def debug_dump_symbol_ihex(self, payload: JsonObject | None = None) -> JsonObject:
        payload = payload or {}
        symbol = payload.get("symbol")
        output_path = payload.get("output_path")
        if not isinstance(symbol, str) or not symbol.strip():
            return tool_error("debug_dump_symbol_ihex", "invalid_argument", "symbol must be a non-empty string.")
        if not isinstance(output_path, str) or not output_path.strip():
            return tool_error("debug_dump_symbol_ihex", "invalid_argument", "output_path must be a non-empty string.")
        output = self.artifacts.validate_output_path(output_path, "debug_dump_symbol_ihex")
        if not output["ok"]:
            return output
        return self.backend.debug_dump_symbol_ihex(symbol.strip(), output["output"])

    def get_last_report(self) -> JsonObject:
        report = read_last_report(self.config)
        if not report.get("ok") and report.get("error_type") in {"report_not_found", "report_unreadable", "config_invalid"}:
            return report
        return {"ok": True, "tool": "get_last_report", "report": report}

    def classify_last_error(self) -> JsonObject:
        return self.backend.classify_last_error()

    def hardware_state(self) -> JsonObject:
        state = self._session_hardware_state()
        inspection_errors = [*state["inspection_errors"], *self._unconfirmed_operations]
        if self._operation_in_flight is not None:
            inspection_errors.append({**self._operation_in_flight, "summary": "Hardware operation is still in flight."})
        return {
            "active": bool(state["active_resources"] or inspection_errors),
            "state_confirmed": not inspection_errors,
            "active_resources": state["active_resources"],
            "inspection_errors": inspection_errors,
        }

    def _session_hardware_state(self) -> JsonObject:
        active_resources: list[JsonObject] = []
        inspection_errors: list[JsonObject] = []
        try:
            if self.backend.has_active_session():
                active_resources.append({"type": "debugger", "id": "default"})
        except Exception as error:
            inspection_errors.append({"type": "debugger", "error": str(error)})
        for subsystem_name, get_active in (
            ("com", self.com_ports.active_session_ids),
            ("can", self.can_buses.active_session_ids),
            ("adapter", self.adapters.active_session_ids),
        ):
            try:
                for resource_id in get_active():
                    active_resources.append({"type": subsystem_name, "id": resource_id})
            except Exception as error:
                inspection_errors.append({"type": subsystem_name, "error": str(error)})
        for subsystem_name, get_errors in (
            ("com", getattr(self.com_ports, "cleanup_inspection_errors", lambda: [])),
            ("can", getattr(self.can_buses, "cleanup_inspection_errors", lambda: [])),
            ("adapter", getattr(self.adapters, "cleanup_inspection_errors", lambda: [])),
        ):
            try:
                for error in get_errors():
                    inspection_errors.append({"type": subsystem_name, **error})
            except Exception as error:
                inspection_errors.append({"type": subsystem_name, "error": str(error)})
        return {"active": bool(active_resources or inspection_errors), "state_confirmed": not inspection_errors, "active_resources": active_resources, "inspection_errors": inspection_errors}

    def has_active_hardware(self) -> bool:
        return bool(self.hardware_state()["active"])

    def call(self, name: str, arguments: JsonObject | None = None) -> JsonObject:
        with self._hardware_call_guard:
            return self._call(name, arguments)

    def _call(self, name: str, arguments: JsonObject | None = None) -> JsonObject:
        if arguments is not None and not isinstance(arguments, dict):
            return tool_error(name, "invalid_argument", "Tool arguments must be an object.")
        args = arguments or {}
        validation_error = validate_tool_arguments(name, args)
        if validation_error is not None:
            return validation_error
        if self._poisoned_state is not None and name not in CLEANUP_TOOLS:
            # A poisoned instance must not reload policy or touch backends; only
            # incident cleanup and purely local diagnostics remain available.
            if name in SAFE_DIAGNOSTIC_TOOLS:
                return self._dispatch(name, args)
            return self._poisoned_error(name)
        hardware_error = self._acquire_hardware(name)
        if hardware_error is not None:
            return hardware_error
        try:
            result = self._execute_call(name, args)
        except BaseException:
            # The original exception wins; a lease bookkeeping failure on top of
            # it still poisons the instance, and the on-disk marker stays
            # fail-closed for the next acquire.
            try:
                self._finalize_call_lease(name)
            except HardwareLockError as lease_error:
                self._record_unconfirmed_operation(name, utc_now_iso(), lease_error, "hardware_lease_failed")
            raise
        try:
            self._finalize_call_lease(name)
        except HardwareLockError as lease_error:
            self._record_unconfirmed_operation(name, utc_now_iso(), lease_error, "hardware_lease_failed")
            failure = tool_error(name, "hardware_state_unconfirmed", "Hardware lease bookkeeping failed after the operation; restart this service and recover the incident.")
            failure.update({"hardware_state_unconfirmed": True, "backend_error": str(lease_error), "operation_result": result})
            return failure
        return result

    def _execute_call(self, name: str, args: JsonObject) -> JsonObject:
        reload_error = None if (name in CLEANUP_TOOLS or self._poisoned_state is not None) else self._reload_config(name)
        if reload_error is not None:
            return reload_error
        if name in HARDWARE_TOOLS:
            def invoke() -> JsonObject:
                try:
                    if name in DEBUG_SESSION_CONFLICT_TOOLS:
                        try:
                            debug_active = self.backend.has_active_session()
                        except Exception as error:
                            return {"ok": False, "tool": name, "error_type": "hardware_state_unconfirmed", "completion_unconfirmed": True, "summary": f"Debug-session state could not be inspected: {error}"}
                        if debug_active:
                            return tool_error(name, "debug_session_active", "Stop the typed debug session before starting a one-shot debugger operation.")
                    return self._dispatch(name, args)
                except BaseException as error:
                    # An AuditWriteError means the hardware side already ran to a
                    # classified result; tearing the backend down here would destroy
                    # a healthy (possibly pre-existing) session over a log failure.
                    if name == "debug_start_session" and not isinstance(error, AuditWriteError):
                        cleanup_confirmed = False
                        try:
                            self.backend.close()
                            cleanup_confirmed = not self.backend.has_active_session()
                        except BaseException:
                            pass
                        if cleanup_confirmed:
                            error._agentic_hil_completion_confirmed = True
                    raise

            return self._invoke_hardware_operation(name, invoke)
        return self._dispatch(name, args)

    def _finalize_call_lease(self, name: str) -> None:
        if self._poisoned_state is not None and name in CLEANUP_TOOLS and self._hardware_lock.mode == "recovery":
            self._hardware_lock.release_os_lock()
        else:
            self._reconcile_hardware_lease()

    def _dispatch(self, name: str, args: JsonObject) -> JsonObject:
        dispatch = {
            "debugger_info": lambda: self.debugger_info(),
            "debugger_probes_list": lambda: self.debugger_probes_list(args.get("debugger", "default")),
            "probe_target": lambda: self.probe_target(),
            "flash_firmware": lambda: self.flash_firmware(args),
            "artifact_upload": lambda: self.artifact_upload(args),
            "reset_target": lambda: self.reset_target(args.get("mode", "run")),
            "debug_start_session": lambda: self.debug_start_session(args),
            "debug_stop_session": lambda: self.debug_stop_session(args),
            "debug_get_session_status": lambda: self.debug_get_session_status(),
            "debug_set_breakpoint": lambda: self.debug_set_breakpoint(args),
            "debug_list_breakpoints": lambda: self.debug_list_breakpoints(),
            "debug_clear_breakpoints": lambda: self.debug_clear_breakpoints(),
            "debug_continue": lambda: self.debug_continue(args),
            "debug_halt": lambda: self.debug_halt(args),
            "debug_get_stop_reason": lambda: self.debug_get_stop_reason(),
            "debug_symbol_info": lambda: self.debug_symbol_info(args),
            "debug_dump_symbol_ihex": lambda: self.debug_dump_symbol_ihex(args),
            "get_last_report": lambda: self.get_last_report(),
            "classify_last_error": lambda: self.classify_last_error(),
            "com_ports_list": lambda: self.com_ports.list_ports(),
            "com_session_start": lambda: self.com_ports.session_start(args.get("port_id", ""), args.get("clear_buffer", True)),
            "com_session_stop": lambda: self.com_ports.session_stop(args.get("port_id", "")),
            "com_write": lambda: self.com_ports.write(args.get("port_id", ""), {key: value for key, value in args.items() if key in {"text", "hex"}}),
            "com_read": lambda: self.com_ports.read(args.get("port_id", ""), args.get("max_bytes"), args.get("wait_timeout_s", 0.0)),
            "can_buses_list": lambda: self.can_buses.list_buses(),
            "can_session_start": lambda: self.can_buses.session_start(args.get("bus_id", ""), args.get("clear_rx_queue", True)),
            "can_session_stop": lambda: self.can_buses.session_stop(args.get("bus_id", "")),
            "can_send": lambda: self.can_buses.send(args.get("bus_id", ""), {key: value for key, value in args.items() if key != "bus_id"}),
            "can_read": lambda: self.can_buses.read(args.get("bus_id", ""), args.get("max_frames"), args.get("wait_timeout_s", 0.0)),
            "adapters_list": lambda: self.adapters.list_adapters(),
            "adapter_session_start": lambda: self.adapters.session_start(args.get("adapter_id", "")),
            "adapter_session_stop": lambda: self.adapters.session_stop(args.get("adapter_id", "")),
            "adapter_set_value": lambda: self.adapters.set_value(args.get("adapter_id", ""), adapter_payload(args)),
            "adapter_inject_fault": lambda: self.adapters.inject_fault(args.get("adapter_id", ""), adapter_payload(args)),
            "adapter_clear_fault": lambda: self.adapters.clear_fault(args.get("adapter_id", ""), adapter_payload(args)),
            "adapter_measure": lambda: self.adapters.measure(args.get("adapter_id", ""), adapter_payload(args)),
        }
        if name in dispatch:
            return dispatch[name]()
        return {"ok": False, "tool": name, "error_type": "unknown_tool", "summary": "Unknown Agentic HIL tool."}

    def _acquire_hardware(self, tool: str) -> JsonObject | None:
        if tool not in HARDWARE_TOOLS:
            return None
        if self._poisoned_state is not None:
            if tool in CLEANUP_TOOLS:
                if self.hardware_owner is not None:
                    if ProjectHardwareLock.owner_is_active(self.config.config_path, self.hardware_owner):
                        return None
                else:
                    marker = self._hardware_lock.quarantine_info()
                    if marker is not None and marker.get("quarantine_id") == self._incident_id:
                        try:
                            if self._hardware_lock.acquire(recovery=True, source="poisoned_service_cleanup"):
                                # Bind the cleanup atomically to the expected incident:
                                # the id read under the OS lock must still match.
                                if self._hardware_lock.recovery_incident_id == self._incident_id:
                                    return None
                                self._hardware_lock.release_os_lock()
                                result = self._poisoned_error(tool)
                                result["error_type"] = "incident_changed"
                                result["summary"] = "The hardware incident changed after this service instance was poisoned; recover it with the current quarantine id."
                                return result
                        except HardwareLockError:
                            pass
            return self._poisoned_error(tool)
        local_state = self.hardware_state()
        if local_state["inspection_errors"]:
            self._poison(local_state)
            if self.hardware_owner is None:
                try:
                    if self._hardware_lock.handle is None:
                        acquired = self._hardware_lock.acquire(source="local_state_quarantine")
                    else:
                        acquired = True
                    if acquired:
                        marker = self._hardware_lock.quarantine_and_release(
                            reason="hardware_cleanup_failed",
                            source="tool_service_preflight",
                            active_resources=local_state["active_resources"],
                            inspection_errors=local_state["inspection_errors"],
                        )
                        self._incident_persisted = True
                        self._incident_id = str(marker["quarantine_id"])
                except HardwareLockError:
                    pass
            return {
                "ok": False,
                "tool": tool,
                "error_type": "hardware_state_unconfirmed",
                "summary": "This Agentic HIL service instance observed an unconfirmed hardware state and must be restarted.",
                "local_state": self._poisoned_state,
            }
        if self.hardware_owner is not None:
            if ProjectHardwareLock.owner_is_active(self.config.config_path, self.hardware_owner):
                return None
            return tool_error(tool, "hardware_owner_invalid", "Hardware lease owner is no longer active.")
        if self._hardware_lock.handle is not None:
            return None
        try:
            acquired = self._hardware_lock.acquire(source=tool)
        except HardwareQuarantinedError as error:
            result = tool_error(tool, "hardware_state_unconfirmed", "Project hardware state is unconfirmed after an incomplete cleanup.")
            result["quarantine"] = error.details
            return result
        except HardwareLockError as error:
            result = tool_error(tool, "hardware_lock_failed", "Project hardware lease could not be acquired.")
            result["backend_error"] = str(error)
            return result
        if acquired:
            return None
        return tool_error(tool, "hardware_busy", "Project hardware is in use by another Agentic HIL process.")

    def _reconcile_hardware_lease(self) -> None:
        if self.hardware_owner is not None:
            return
        if self._incident_persisted and self._hardware_lock.handle is None:
            return
        state = self.hardware_state()
        if not state["active"]:
            self._hardware_lock.confirm_safe_and_release()
            return
        if state["inspection_errors"]:
            if self._hardware_lock.handle is None:
                try:
                    acquired = self._hardware_lock.acquire(source="tool_service_reconcile")
                except HardwareQuarantinedError:
                    self._poison(state)
                    self._incident_persisted = True
                    return
                if not acquired:
                    raise HardwareLockError("Unconfirmed hardware state exists without ownership of the project hardware lease.")
            self._poison(state)
            marker = self._hardware_lock.quarantine_and_release(
                reason="hardware_cleanup_failed",
                source="tool_service",
                active_resources=state["active_resources"],
                inspection_errors=state["inspection_errors"],
            )
            self._incident_persisted = True
            self._incident_id = str(marker["quarantine_id"])
            return
        if self._hardware_lock.handle is None and not self._hardware_lock.acquire(source="active_session_recovery"):
            raise HardwareLockError("Active hardware session exists without ownership of the project hardware lease.")

    def _reload_config(self, tool: str) -> JsonObject | None:
        if not self.reload_config:
            return None
        try:
            config = load_config(self.config.config_path, self.config.work_dir)
        except ConfigError as error:
            return {"tool": tool, **error.to_dict()}
        subsystems_match = all(getattr(subsystem, "config", config) == config for subsystem in (self.backend, self.artifacts, self.com_ports, self.can_buses, self.adapters))
        if config == self.config and subsystems_match:
            return None
        started_at = utc_now_iso()
        old_config = self.config
        old_backend = self.backend
        try:
            if config.debugger.type == self.config.debugger.type:
                self.backend.reconfigure(config)
            else:
                old_backend.close()
                self.backend = create_debugger_backend(config)
            self.artifacts.reconfigure(config)
            self.com_ports.reconfigure(config)
            self.can_buses.reconfigure(config)
            self.adapters.reconfigure(config)
            self.config = config
        except BaseException as error:
            rollback_errors: list[str] = []
            if self.backend is not old_backend:
                with suppress(BaseException):
                    self.backend.close()
                self.backend = old_backend
            for name, subsystem in (("debugger", self.backend), ("artifacts", self.artifacts), ("com", self.com_ports), ("can", self.can_buses), ("adapter", self.adapters)):
                try:
                    subsystem.reconfigure(old_config)
                except BaseException as rollback_error:
                    rollback_errors.append(f"{name}: {type(rollback_error).__name__}: {rollback_error}")
            self.config = old_config
            audit_confirmed = isinstance(error, AuditWriteError) and (error.completion_state == "confirmed" or getattr(error, "_agentic_hil_completion_confirmed", False) is True)
            if audit_confirmed and not rollback_errors:
                result = tool_error(tool, "audit_write_failed", "Project policy reload closed sessions safely, but a cleanup audit record could not be written.")
                result["completion_confirmed"] = True
                result["backend_error"] = str(error)
                return result
            self._record_unconfirmed_operation("config_reload", started_at, error, "config_reconfigure_failed")
            if not isinstance(error, Exception):
                raise
            result = tool_error(tool, "config_reconfigure_failed", "Project configuration could not be applied consistently; restart this service.")
            result.update({"completion_unconfirmed": True, "hardware_state_unconfirmed": True, "reload_error": str(error), "rollback_errors": rollback_errors})
            return result
        return None

    def cleanup_test_sessions(self) -> None:
        with self._hardware_call_guard:
            self._close_subsystems(
                (
                    ("debugger", self.backend.close),
                    ("adapters", self.adapters.close),
                    ("com", self.com_ports.close),
                    ("can", self.can_buses.close),
                )
            )

    def close(self) -> None:
        with self._hardware_call_guard:
            self._close_subsystems(
                (
                    ("debugger", self.backend.close),
                    ("com", self.com_ports.close),
                    ("can", self.can_buses.close),
                    ("adapters", self.adapters.close),
                )
            )

    def _close_subsystems(self, actions: tuple[tuple[str, Callable[[], None]], ...]) -> None:
        errors: list[tuple[str, Exception]] = []
        pending_base_exception: BaseException | None = None
        for name, close_action in actions:
            try:
                close_action()
            except BaseException as error:
                if isinstance(error, Exception):
                    errors.append((name, error))
                elif pending_base_exception is None:
                    pending_base_exception = error
        if self.hardware_owner is None:
            try:
                self._finish_hardware_lease()
            except BaseException as error:
                if isinstance(error, Exception):
                    errors.append(("hardware_lease", error))
                elif pending_base_exception is None:
                    pending_base_exception = error
        if pending_base_exception is not None:
            raise pending_base_exception
        if errors:
            raise HardwareCleanupError(errors)

    def _finish_hardware_lease(self) -> None:
        if self._incident_persisted and self._hardware_lock.handle is None:
            current = self._session_hardware_state()
            new_inspection_errors = [
                item
                for item in current["inspection_errors"]
                if json.dumps(item, sort_keys=True, default=str) not in self._known_incident_errors
            ]
            if not current["active_resources"] and not new_inspection_errors:
                return
            if self._hardware_lock.quarantine_info() is not None:
                return
            if not self._hardware_lock.acquire(source="tool_service_post_recovery_cleanup"):
                raise HardwareLockError("New cleanup failure could not acquire a fresh project hardware lease.")
            marker = self._hardware_lock.quarantine_and_release(
                reason="hardware_cleanup_failed",
                source="tool_service_post_recovery_cleanup",
                active_resources=current["active_resources"],
                inspection_errors=current["inspection_errors"],
            )
            self._incident_id = str(marker["quarantine_id"])
            return
        state = self.hardware_state()
        if state["active"]:
            self._poison(state)
            if self._hardware_lock.handle is None and not self._hardware_lock.acquire(source="tool_service_cleanup"):
                raise HardwareLockError("Active hardware cleanup state exists but the project hardware lease is owned elsewhere.")
            marker = self._hardware_lock.quarantine_and_release(
                reason="hardware_cleanup_failed",
                source="tool_service_cleanup",
                active_resources=state["active_resources"],
                inspection_errors=state["inspection_errors"],
            )
            self._incident_persisted = True
            self._incident_id = str(marker["quarantine_id"])
            return
        self._hardware_lock.confirm_safe_and_release()

    def _invoke_hardware_operation(self, tool: str, action: Callable[[], JsonObject]) -> JsonObject:
        operation: JsonObject = {"type": "operation", "tool": tool, "started_at": utc_now_iso()}
        self._operation_in_flight = operation
        try:
            if self.hardware_owner is None and self._hardware_lock.mode == "normal":
                state = self._session_hardware_state()
                try:
                    self._hardware_lock.update_active_state(source=tool, active_resources=state["active_resources"], operation=operation)
                except HardwareLockError as error:
                    # Nothing has touched hardware yet: fail the call as a lock
                    # error instead of quarantining an untouched rig.
                    result = tool_error(tool, "hardware_lock_failed", "Hardware lease bookkeeping could not record the operation; nothing was executed.")
                    result["backend_error"] = str(error)
                    result["completion_confirmed"] = True
                    return result
            result = action()
            if not isinstance(result, dict):
                raise RuntimeError("Hardware tool returned a non-object result.")
            if result_completion_unconfirmed(tool, result):
                self._record_unconfirmed_operation(tool, operation["started_at"], None, result.get("error_type"))
            elif self.hardware_owner is None and self._hardware_lock.mode == "normal":
                state = self._session_hardware_state()
                try:
                    self._hardware_lock.update_active_state(source=tool, active_resources=state["active_resources"], operation=None)
                except HardwareLockError as error:
                    # The operation itself is complete and classified; a failed
                    # observability update must not discard that verdict. The
                    # stale marker stays fail-closed on disk.
                    result["lease_update_error"] = str(error)
            return result
        except AuditWriteError as error:
            underlying = error.operation_result if isinstance(error.operation_result, dict) else {}
            state_confirmed = error.completion_state == "confirmed" or getattr(error, "_agentic_hil_completion_confirmed", False) is True
            embedded_unconfirmed = bool(underlying) and result_completion_unconfirmed(tool, underlying)
            embedded_confirmed = (
                underlying.get("ok") is True
                or underlying.get("completion_confirmed") is True
                or str(underlying.get("error_type", "")) in NO_HARDWARE_ERROR_TYPES
            )
            if embedded_unconfirmed or (not state_confirmed and not embedded_confirmed):
                self._record_unconfirmed_operation(tool, operation["started_at"], error, underlying.get("error_type") or "audit_write_failed")
                result = tool_error(tool, "hardware_state_unconfirmed", "Hardware completion could not be confirmed and its audit record could not be written.")
                result.update({"hardware_state_unconfirmed": True, "completion_unconfirmed": True, "audit_error": str(error)})
            else:
                result = tool_error(tool, "audit_write_failed", "Hardware completion was confirmed, but its audit record could not be written.")
                result["completion_confirmed"] = True
                result["backend_error"] = str(error)
            if underlying:
                result["operation_result"] = underlying
            return result
        except BaseException as error:
            if getattr(error, "_agentic_hil_completion_confirmed", False) is not True:
                self._record_unconfirmed_operation(tool, operation["started_at"], error)
            raise
        finally:
            self._operation_in_flight = None

    def _record_unconfirmed_operation(self, tool: str, started_at: object, error: BaseException | None, error_type: object = None) -> None:
        operation: JsonObject = {
            "type": "operation",
            "tool": tool,
            "started_at": str(started_at),
            "error_type": str(error_type or (type(error).__name__ if error is not None else "completion_unconfirmed")),
            "error": str(error) if error is not None else "",
            "summary": "Hardware operation completion was not confirmed.",
        }
        self._unconfirmed_operations.append(operation)
        self._known_incident_errors.add(json.dumps(operation, sort_keys=True, default=str))
        self._incident_persisted = False
        previously_active = self._poisoned_state["active_resources"] if self._poisoned_state else []
        self._poisoned_state = {"error_type": "hardware_state_unconfirmed", "active_resources": previously_active, "inspection_errors": [*self._unconfirmed_operations]}

    def _poison(self, state: JsonObject) -> None:
        self._poisoned_state = {
            "error_type": "hardware_state_unconfirmed",
            "active_resources": state["active_resources"],
            "inspection_errors": state["inspection_errors"],
        }
        for item in state["inspection_errors"]:
            self._known_incident_errors.add(json.dumps(item, sort_keys=True, default=str))

    def _poisoned_error(self, tool: str) -> JsonObject:
        return {
            "ok": False,
            "tool": tool,
            "error_type": "hardware_state_unconfirmed",
            "summary": "This Agentic HIL service instance observed an unconfirmed hardware state and must be restarted.",
            "local_state": self._poisoned_state,
        }


def adapter_payload(args: JsonObject) -> JsonObject:
    return {key: value for key, value in args.items() if key != "adapter_id"}


def cleanup_error_is_audit_only(error: BaseException) -> bool:
    """True when a cleanup failure is purely an audit-write failure after confirmed hardware completion."""
    if is_confirmed_audit_error(error):
        return True
    if isinstance(error, HardwareCleanupError):
        return bool(error.errors) and all(is_confirmed_audit_error(item) for _, item in error.errors)
    return False


def is_confirmed_audit_error(error: BaseException) -> bool:
    if not isinstance(error, AuditWriteError):
        return False
    return error.completion_state == "confirmed" or getattr(error, "_agentic_hil_completion_confirmed", False) is True


def result_completion_unconfirmed(tool: str, result: JsonObject) -> bool:
    error_type = str(result.get("error_type", ""))
    return (
        result.get("completion_unconfirmed") is True
        or result.get("hardware_state_unconfirmed") is True
        or error_type == "hardware_cleanup_failed"
        or (
            result.get("completion_confirmed") is not True
            and ((error_type == "timeout" and tool in INDETERMINATE_TIMEOUT_TOOLS) or error_type in INDETERMINATE_TRANSPORT_ERRORS)
        )
    )


def validate_tool_arguments(tool: str, args: JsonObject) -> JsonObject | None:
    allowed = TOOL_ARGUMENTS.get(tool)
    if allowed is not None:
        unknown = sorted(set(args) - allowed)
        if unknown:
            return tool_error(tool, "invalid_argument", f"Unknown argument(s): {', '.join(unknown)}.")
    missing = sorted(REQUIRED_TOOL_ARGUMENTS.get(tool, set()) - set(args))
    if missing:
        return tool_error(tool, "invalid_argument", f"Missing required argument(s): {', '.join(missing)}.")
    for key in ("debugger", "image_path", "artifact_id", "filename", "data_base64", "mode", "symbol", "output_path", "port_id", "text", "hex", "bus_id", "data_hex", "adapter_id", "channel", "fault", "unit"):
        if key in args and not isinstance(args[key], str):
            return tool_error(tool, "invalid_argument", f"{key} must be a string.")
    for key in ("reset_after_flash", "clear_buffer", "clear_rx_queue", "extended", "rtr"):
        if key in args and not isinstance(args[key], bool):
            return tool_error(tool, "invalid_argument", f"{key} must be a boolean.")
    for key in ("timeout_s", "wait_timeout_s", "value"):
        if key in args and (isinstance(args[key], bool) or not isinstance(args[key], (int, float)) or not math.isfinite(float(args[key]))):
            return tool_error(tool, "invalid_argument", f"{key} must be a finite number.")
    for key in ("max_bytes", "max_frames"):
        if key in args and (isinstance(args[key], bool) or not isinstance(args[key], int)):
            return tool_error(tool, "invalid_argument", f"{key} must be an integer.")
    if tool in {"flash_firmware", "debug_start_session"} and ("image_path" in args) == ("artifact_id" in args):
        return tool_error(tool, "invalid_argument", "Provide exactly one of image_path or artifact_id.")
    if tool == "artifact_upload":
        local = "image_path" in args
        encoded_fields = {"filename", "data_base64"} & set(args)
        if local:
            if encoded_fields:
                return tool_error(tool, "invalid_argument", "Provide image_path or filename with data_base64, not both.")
        elif encoded_fields != {"filename", "data_base64"}:
            return tool_error(tool, "invalid_argument", "Provide image_path or both filename and data_base64.")
    if tool == "com_write" and ("text" in args) == ("hex" in args):
        return tool_error(tool, "invalid_argument", "Provide exactly one of text or hex.")
    if tool == "can_send":
        if ("frame_id" in args) == ("id" in args):
            return tool_error(tool, "invalid_argument", "Provide exactly one of frame_id or id.")
        frame_id = args.get("frame_id", args.get("id"))
        if isinstance(frame_id, bool) or not isinstance(frame_id, (int, str)):
            return tool_error(tool, "invalid_argument", "frame_id must be an integer or string.")
    if "mode" in args:
        allowed_modes = {"run", "halt", "init"} if tool == "reset_target" else {"attach", "reset_halt", "load"}
        if args["mode"] not in allowed_modes:
            return tool_error(tool, "invalid_argument", f"mode must be one of: {', '.join(sorted(allowed_modes))}.")
    if tool == "debug_set_breakpoint":
        location = args.get("location")
        if not isinstance(location, (str, dict)) or isinstance(location, str) and not location.strip():
            return tool_error(tool, "invalid_argument", "location must be a non-empty string or object.")
        if isinstance(location, dict) and set(location) - {"symbol", "function", "file", "line"}:
            return tool_error(tool, "invalid_argument", "location contains unknown fields.")
    return None


def number_argument(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        return None
    return float(value)


def tool_error(tool: str, error_type: str, summary: str) -> JsonObject:
    return {"ok": False, "tool": tool, "error_type": error_type, "summary": summary}
