from __future__ import annotations

import math
import threading
from pathlib import Path

from agentic_hil.adapters import AdapterService
from agentic_hil.artifacts import ArtifactManager
from agentic_hil.can import CanBusService
from agentic_hil.comports import ComPortService
from agentic_hil.config import ConfigError
from agentic_hil.contracts import validate_tool_arguments
from agentic_hil.coordination import (
    DEBUGGER_DISCOVERY_RESOURCE,
    CoordinationError,
    HardwareCoordinator,
    HardwareLease,
    debugger_effect_resources,
)
from agentic_hil.debugger import DebuggerBackend, create_debugger_backend
from agentic_hil.process import cleanup_registered_processes, managed_process_owner
from agentic_hil.provisional import cleanup_provisional_handles
from agentic_hil.report import (
    attach_canonical_audit_evidence,
    audit_unavailable,
    ensure_audit_ready,
    overall_success,
    read_last_report,
    write_report,
)
from agentic_hil.types import AgenticHILConfig, JsonObject


class AgenticHILToolService:
    def __init__(
        self,
        config: AgenticHILConfig,
        backend: DebuggerBackend | None = None,
        artifacts: ArtifactManager | None = None,
        com_ports: ComPortService | None = None,
        can_buses: CanBusService | None = None,
        adapters: AdapterService | None = None,
        coordinator: HardwareCoordinator | None = None,
        frontend: str = "python",
    ):
        self.config = config
        self.coordinator = coordinator or HardwareCoordinator(config, frontend)
        self.backend = backend or create_debugger_backend(self.config)
        self.artifacts = artifacts or ArtifactManager(self.config)
        self.com_ports = com_ports or ComPortService(self.config, self.coordinator)
        self.can_buses = can_buses or CanBusService(self.config, self.coordinator)
        self.adapters = adapters or AdapterService(self.config, self.coordinator)
        self._debug_artifact: JsonObject | None = None
        self._debug_lease: HardwareLease | None = None
        self._lifecycle_lock = threading.RLock()
        self._dispatch_local = threading.local()
        self._state = "open"
        self._dispatch_depth = 0

    @property
    def _dispatch_depth(self) -> int:
        return int(getattr(self._dispatch_local, "depth", 0))

    @_dispatch_depth.setter
    def _dispatch_depth(self, value: int) -> None:
        self._dispatch_local.depth = value

    def debugger_info(self) -> JsonObject:
        if not self.config.permissions.allow_probe:
            return tool_error("debugger_info", "permission_denied", "Debugger execution is disabled by the authoritative config.")
        return self.backend.info()

    def debugger_probes_list(self) -> JsonObject:
        if self._dispatch_depth == 0:
            return self.call("debugger_probes_list")
        if not self.config.permissions.allow_probe:
            return tool_error("debugger_probes_list", "permission_denied", "Debugger probe discovery is disabled by the authoritative config.")
        result = self.backend.list_probes()
        if result.get("cleanup_required") is not True and result.get("side_effect_status") not in {"unknown", "partial"}:
            result = {**result, "side_effect_committed": False, "side_effect_status": "not_started", "retry_safe": True}
        return result

    def probe_target(self) -> JsonObject:
        if self._dispatch_depth == 0:
            return self.call("probe_target")
        return self.backend.probe_target()

    def flash_firmware(self, payload: JsonObject | None = None) -> JsonObject:
        if self._dispatch_depth == 0:
            return self.call("flash_firmware", payload)
        payload = payload or {}
        if not self.config.permissions.allow_flash:
            return tool_error("flash_firmware", "permission_denied", "Flashing is disabled by the authoritative config.")
        image_path = payload.get("image_path")
        artifact_id = payload.get("artifact_id")
        if bool(image_path) == bool(artifact_id):
            return tool_error("flash_firmware", "invalid_argument", "Provide exactly one of image_path or artifact_id.")
        reset_after_flash = payload.get("reset_after_flash", False)
        if not isinstance(reset_after_flash, bool):
            return tool_error("flash_firmware", "invalid_argument", "reset_after_flash must be a boolean.")
        if reset_after_flash and not self.config.permissions.allow_reset:
            return tool_error("flash_firmware", "permission_denied", "Post-flash reset is disabled by the authoritative config.")
        validation = self.artifacts.validate_local_path(str(image_path)) if image_path else self.artifacts.resolve_artifact_id(str(artifact_id))
        if not validation["ok"]:
            return validation
        staged = self.artifacts.stage_for_backend(validation["artifact"], "flash_firmware")
        if not staged["ok"]:
            return staged
        try:
            return self.backend.flash_firmware(staged["artifact"], reset_after_flash)
        finally:
            self.artifacts.release_stage(staged["artifact"])

    def artifact_upload(self, payload: JsonObject | None = None) -> JsonObject:
        return self.artifacts.upload(payload)

    def reset_target(self, mode: str = "run") -> JsonObject:
        if self._dispatch_depth == 0:
            return self.call("reset_target", {"mode": mode})
        if not self.config.permissions.allow_reset:
            return tool_error("reset_target", "permission_denied", "Target reset is disabled by the authoritative config.")
        return self.backend.reset_target(mode)

    def debug_start_session(self, payload: JsonObject | None = None) -> JsonObject:
        if self._dispatch_depth == 0:
            return self.call("debug_start_session", payload)
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
        staged = self.artifacts.stage_for_backend(artifact, "debug_start_session")
        if not staged["ok"]:
            return staged
        try:
            result = self.backend.debug_start_session(staged["artifact"], str(payload.get("mode", "attach")), number_argument(payload.get("timeout_s")))
        except BaseException:
            self.artifacts.release_stage(staged["artifact"])
            raise
        if result.get("ok") or result.get("cleanup_required"):
            self._debug_artifact = staged["artifact"]
        else:
            self.artifacts.release_stage(staged["artifact"])
        return result

    def debug_stop_session(self, payload: JsonObject | None = None) -> JsonObject:
        if self._dispatch_depth == 0:
            return self.call("debug_stop_session", payload)
        result = self.backend.debug_stop_session(number_argument((payload or {}).get("timeout_s")))
        if result.get("ok") and self._debug_artifact is not None:
            self.artifacts.release_stage(self._debug_artifact)
            self._debug_artifact = None
        return result

    def debug_get_session_status(self) -> JsonObject:
        return self._status_result(self.backend.debug_get_session_status())

    def _status_result(self, result: JsonObject) -> JsonObject:
        """Status reads must feed the same lease-quarantine path as effects when
        they surface a broken audit latch."""
        if result.get("audit_ok") is False and self._debug_lease is not None and self._debug_lease.audit_ok:
            self._debug_lease.quarantine("debug_audit_broken", audit_broken=True)
            return {**result, **self._debug_lease.status()}
        return result

    def debug_set_breakpoint(self, payload: JsonObject | None = None) -> JsonObject:
        if self._dispatch_depth == 0:
            return self.call("debug_set_breakpoint", payload)
        location = (payload or {}).get("location")
        has_symbol_location = isinstance(location, str) and bool(location.strip())
        has_typed_location = isinstance(location, dict) and bool(location)
        if not has_symbol_location and not has_typed_location:
            return tool_error("debug_set_breakpoint", "invalid_argument", "location must be a non-empty string or object.")
        return self.backend.debug_set_breakpoint({"location": location})

    def debug_list_breakpoints(self) -> JsonObject:
        return self._status_result(self.backend.debug_list_breakpoints())

    def debug_clear_breakpoints(self) -> JsonObject:
        if self._dispatch_depth == 0:
            return self.call("debug_clear_breakpoints")
        return self.backend.debug_clear_breakpoints()

    def debug_continue(self, payload: JsonObject | None = None) -> JsonObject:
        if self._dispatch_depth == 0:
            return self.call("debug_continue", payload)
        return self.backend.debug_continue(number_argument((payload or {}).get("timeout_s")))

    def debug_halt(self, payload: JsonObject | None = None) -> JsonObject:
        if self._dispatch_depth == 0:
            return self.call("debug_halt", payload)
        return self.backend.debug_halt(number_argument((payload or {}).get("timeout_s")))

    def debug_get_stop_reason(self) -> JsonObject:
        return self._status_result(self.backend.debug_get_stop_reason())

    def debug_symbol_info(self, payload: JsonObject | None = None) -> JsonObject:
        if self._dispatch_depth == 0:
            return self.call("debug_symbol_info", payload)
        symbol = (payload or {}).get("symbol")
        if not isinstance(symbol, str) or not symbol.strip():
            return tool_error("debug_symbol_info", "invalid_argument", "symbol must be a non-empty string.")
        return self.backend.debug_symbol_info(symbol.strip())

    def debug_dump_symbol_ihex(self, payload: JsonObject | None = None) -> JsonObject:
        if self._dispatch_depth == 0:
            return self.call("debug_dump_symbol_ihex", payload)
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
        if report.get("ok") is not True and report.get("tool") == "get_last_report":
            return report
        return {"ok": True, "tool": "get_last_report", "report": attach_canonical_audit_evidence(self.config, report)}

    def classify_last_error(self) -> JsonObject:
        return self.backend.classify_last_error()

    def call(self, name: str, arguments: JsonObject | None = None) -> JsonObject:
        with self._lifecycle_lock:
            if self._state != "open":
                return {"ok": False, "tool": name, "error_type": "service_closed" if self._state == "closed" else "service_cleanup_required", "summary": "Agentic HIL service is not accepting new calls.", "side_effect_committed": False, "cleanup_required": self._state == "cleanup_required"}
            return self._call_unlocked(name, arguments)

    def _call_unlocked(self, name: str, arguments: JsonObject | None = None) -> JsonObject:
        if arguments is None:
            args: JsonObject = {}
        elif not isinstance(arguments, dict):
            return {"ok": False, "tool": name, "error_type": "invalid_argument", "field": "$", "validator": "type", "summary": "Tool arguments must be an object."}
        else:
            args = arguments
        validation_error = validate_tool_arguments(name, args)
        if validation_error is not None:
            return validation_error
        dispatch = {
            "debugger_info": lambda: self.debugger_info(),
            "debugger_probes_list": lambda: self.debugger_probes_list(),
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
            "com_session_stop": lambda: self.com_ports.session_stop(str(args.get("port_id", ""))),
            "com_write": lambda: self.com_ports.write(str(args.get("port_id", "")), {key: value for key, value in args.items() if key in {"text", "hex"}}),
            "com_read": lambda: self.com_ports.read(str(args.get("port_id", "")), args.get("max_bytes"), args.get("wait_timeout_s", 0.0)),
            "can_buses_list": lambda: self.can_buses.list_buses(),
            "can_session_start": lambda: self.can_buses.session_start(args.get("bus_id", ""), args.get("clear_rx_queue", True)),
            "can_session_stop": lambda: self.can_buses.session_stop(str(args.get("bus_id", ""))),
            "can_send": lambda: self.can_buses.send(str(args.get("bus_id", "")), {key: value for key, value in args.items() if key != "bus_id"}),
            "can_read": lambda: self.can_buses.read(str(args.get("bus_id", "")), args.get("max_frames"), args.get("wait_timeout_s", 0.0)),
            "adapters_list": lambda: self.adapters.list_adapters(),
            "adapter_session_start": lambda: self.adapters.session_start(str(args.get("adapter_id", ""))),
            "adapter_session_stop": lambda: self.adapters.session_stop(str(args.get("adapter_id", ""))),
            "adapter_set_value": lambda: self.adapters.set_value(str(args.get("adapter_id", "")), adapter_payload(args)),
            "adapter_inject_fault": lambda: self.adapters.inject_fault(str(args.get("adapter_id", "")), adapter_payload(args)),
            "adapter_clear_fault": lambda: self.adapters.clear_fault(str(args.get("adapter_id", "")), adapter_payload(args)),
            "adapter_measure": lambda: self.adapters.measure(str(args.get("adapter_id", "")), adapter_payload(args)),
        }
        if name in dispatch:
            if self.coordinator.blocked and name in audited_hardware_tools() and name not in containment_tools():
                return {
                    "ok": False,
                    "tool": name,
                    "error_type": "resource_quarantined",
                    "summary": "Hardware effects are blocked until unresolved cleanup or audit state is recovered.",
                    "cleanup_required": True,
                    "quarantined": True,
                    "retry_safe": False,
                }
            if name in audited_hardware_tools():
                try:
                    ensure_audit_ready(self.config)
                except (ConfigError, OSError) as error:
                    return audit_unavailable(name, error)
            try:
                if name in debugger_effect_tools() or name == "debug_stop_session":
                    permission_failure = self._debug_permission_failure(name, args)
                    if permission_failure is not None:
                        return permission_failure
                    return self._coordinated_debug_call(name, lambda: self._invoke_dispatch(dispatch[name]))
                return self._invoke_dispatch(dispatch[name])
            except BaseException as error:
                if name in audited_hardware_tools() or name in containment_tools():
                    poison_error = self._poison_quietly("unknown_hardware_exception", error, audit_broken=isinstance(error, (ConfigError, OSError)))
                    # Report the quarantine state that actually holds instead of a
                    # hardcoded claim: a poison failure must not fake protection.
                    quarantined_now = self.coordinator.blocked or any(item.state in {"cleanup_required", "quarantined"} for item in self.coordinator.leases.values())
                    if not isinstance(error, Exception):
                        if poison_error is not None:
                            error.args = (*error.args, f"Quarantine error: {poison_error}")
                        raise
                    result: JsonObject = {
                        "ok": False,
                        "tool": name,
                        "error_type": "audit_failed_after_action" if isinstance(error, (ConfigError, OSError)) else "hardware_action_exception",
                        "summary": "Hardware action failed and its physical state is unconfirmed.",
                        "side_effect_status": "unknown",
                        "retry_safe": False,
                        "cleanup_required": True,
                        "quarantined": quarantined_now,
                        "quarantine_id": self.coordinator.quarantine_id,
                        "backend_error": str(error),
                    }
                    if poison_error is not None:
                        result["quarantine_error"] = str(poison_error)
                    if isinstance(error, (ConfigError, OSError)):
                        result.update({"audit_ok": False, "audit_error": error.to_dict() if isinstance(error, ConfigError) else {"error_type": type(error).__name__, "backend_error": str(error)}})
                    written = write_report(self.config, result)
                    if written.get("audit_ok") is False:
                        self._poison_quietly("hardware_exception_audit_broken", audit_broken=True)
                        quarantined_now = self.coordinator.blocked or quarantined_now
                    return {**written, "cleanup_required": True, "quarantined": quarantined_now, "quarantine_id": self.coordinator.quarantine_id}
                if isinstance(error, ConfigError):
                    return {"tool": name, **error.to_dict()}
                raise
        return {"ok": False, "tool": name, "error_type": "unknown_tool", "summary": "Unknown Agentic HIL tool."}

    def hardware_lease_status(self) -> JsonObject:
        return self.coordinator.status()

    def _poison_quietly(self, reason: str, error: object | None = None, *, audit_broken: bool = False) -> Exception | None:
        """Quarantine the coordinator without letting a coordination failure mask the primary hardware error."""
        try:
            self.coordinator.poison(reason, error, audit_broken=audit_broken)
        except Exception as poison_error:
            return poison_error
        return None

    def _invoke_dispatch(self, callback) -> JsonObject:
        self._dispatch_depth += 1
        try:
            with managed_process_owner(self.coordinator.owner_marker):
                return callback()
        finally:
            self._dispatch_depth -= 1

    def _debug_permission_failure(self, name: str, args: JsonObject) -> JsonObject | None:
        if name == "flash_firmware":
            if not self.config.permissions.allow_flash:
                return self._invoke_dispatch(lambda: self.flash_firmware(args))
            if args.get("reset_after_flash") is True and not self.config.permissions.allow_reset:
                return self._invoke_dispatch(lambda: self.flash_firmware(args))
        if name == "reset_target" and not self.config.permissions.allow_reset:
            return self._invoke_dispatch(lambda: self.reset_target(args.get("mode", "run")))
        if name == "probe_target" and not self.config.permissions.allow_probe:
            return self._invoke_dispatch(self.probe_target)
        if name == "debugger_probes_list" and not self.config.permissions.allow_probe:
            return self._invoke_dispatch(self.debugger_probes_list)
        if name == "debug_start_session":
            mode = args.get("mode", "attach")
            permissions = self.config.permissions
            denied = (
                not permissions.allow_probe
                or (mode != "attach" and not permissions.allow_reset)
                or permissions.allow_raw_debugger_commands
                or (mode == "load" and (not permissions.allow_flash or permissions.allow_mass_erase))
            )
            if denied:
                return self._invoke_dispatch(lambda: self.debug_start_session(args))
        return None

    def _coordinated_debug_call(self, name: str, callback) -> JsonObject:
        one_shot = name in {"debugger_probes_list", "probe_target", "flash_firmware", "reset_target"}
        starts_session = name == "debug_start_session"
        lease = self._debug_lease
        if one_shot or starts_session:
            if lease is not None:
                if starts_session:
                    result = callback()
                    if self._result_requires_quarantine(result):
                        lease.quarantine("debug_session_start_unconfirmed", audit_broken=result.get("audit_ok") is False)
                    return self._lease_result(result, lease)
                return {"ok": False, "tool": name, "error_type": "resource_busy", "summary": "Debugger resource already has an active owner lease.", "retry_safe": True}
            try:
                resources = (DEBUGGER_DISCOVERY_RESOURCE,) if name == "debugger_probes_list" else debugger_effect_resources(self.config)
                lease = self.coordinator.acquire(*resources)
            except CoordinationError as error:
                return {"tool": name, "side_effect_committed": False, **error.result}
        try:
            result = callback()
        except BaseException as error:
            if lease is not None:
                lease.quarantine("debugger_call_exception", error)
                if starts_session:
                    self._debug_lease = lease
            raise
        if lease is None:
            return result
        if one_shot:
            requires_quarantine = self._result_requires_quarantine(result)
            if requires_quarantine:
                lease.quarantine("debugger_result_unconfirmed", audit_broken=result.get("audit_ok") is False)
            written = self._lease_result(result, lease)
            if not requires_quarantine and written.get("audit_ok") is not False and lease.state == "active":
                lease.release()
            return self._recommit_lease_report(written, lease)
        if starts_session:
            retain_lease = False
            if result.get("ok") is True or result.get("cleanup_required") is True:
                self._debug_lease = lease
                retain_lease = True
                if self._result_requires_quarantine(result):
                    lease.quarantine("debug_session_start_unconfirmed", audit_broken=result.get("audit_ok") is False)
            elif self._result_requires_quarantine(result):
                lease.quarantine("debug_session_start_unconfirmed", audit_broken=result.get("audit_ok") is False)
                self._debug_lease = lease
                retain_lease = True
            written = self._lease_result(result, lease)
            if written.get("audit_ok") is False:
                self._debug_lease = lease
                retain_lease = True
            if not retain_lease and lease.state == "active":
                lease.release()
            return self._recommit_lease_report(written, lease)
        if name == "debug_stop_session":
            if overall_success(result):
                lease.resolve_retryable_cleanup("debug_session_cleanup_unconfirmed")
                if lease.state != "active":
                    result = {**result, "ok": False, "error_type": "cleanup_required", "summary": "Debug process cleanup completed, but prior target state remains unconfirmed."}
                written = self._lease_result(result, lease)
                if lease.state == "active" and written.get("audit_ok") is not False and lease.release():
                    self._debug_lease = None
                return self._recommit_lease_report(written, lease)
            else:
                lease.quarantine("debug_session_cleanup_unconfirmed", audit_broken=result.get("audit_ok") is False)
            return self._lease_result(result, lease)
        if name == "debug_clear_breakpoints" and overall_success(result) and result.get("backend_reconciled") is True:
            lease.resolve_retryable_cleanup("debug_breakpoint_cleanup_unconfirmed")
        if name == "debug_halt" and overall_success(result):
            lease.resolve_retryable_cleanup("debug_target_state_unconfirmed")
        if self._result_requires_quarantine(result):
            reason = "debug_breakpoint_cleanup_unconfirmed" if name in {"debug_set_breakpoint", "debug_clear_breakpoints"} else "debug_target_state_unconfirmed" if name in {"debug_continue", "debug_halt"} else "debug_session_result_unconfirmed"
            lease.quarantine(reason, audit_broken=result.get("audit_ok") is False)
        return self._lease_result(result, lease)

    def _lease_result(self, result: JsonObject, lease: HardwareLease) -> JsonObject:
        enriched = {**result, **lease.status()}
        written = write_report(self.config, enriched)
        if written.get("audit_ok") is False and lease.audit_ok:
            lease.quarantine("debug_coordination_report_audit_broken", audit_broken=True)
            written = write_report(self.config, {**written, **lease.status()})
        return {**written, **lease.status()}

    def _recommit_lease_report(self, written: JsonObject, lease: HardwareLease) -> JsonObject:
        """Re-commit the report after a terminal lease transition so the persisted
        report evidence carries the same lease_state as the returned result."""
        status = lease.status()
        if written.get("lease_state") == status.get("lease_state"):
            return {**written, **status}
        final = write_report(self.config, {**written, **status})
        if final.get("audit_ok") is False:
            if lease.state == "released":
                self._poison_quietly("lease_release_report_audit_broken", audit_broken=True)
                return {**final, "ok": False, "cleanup_required": True, "quarantined": True, "quarantine_id": self.coordinator.quarantine_id}
            if lease.audit_ok:
                lease.quarantine("debug_coordination_report_audit_broken", audit_broken=True)
                final = write_report(self.config, {**final, **lease.status()})
        return {**final, **lease.status()}

    def _result_requires_quarantine(self, result: JsonObject) -> bool:
        if result.get("audit_ok") is False or result.get("cleanup_required") is True:
            return True
        if result.get("error_type") in {
            "permission_denied",
            "invalid_argument",
            "session_already_active",
            "session_not_active",
            "not_supported",
            "artifact_not_found",
            "artifact_validation_failed",
            "output_validation_failed",
            "resource_busy",
            "resource_quarantined",
        }:
            return False
        if result.get("side_effect_status") in {"unknown", "partial"}:
            return True
        return result.get("error_type") is not None and result.get("side_effect_committed") is not False and result.get("side_effect_status") != "committed"

    def close(self) -> None:
        with self._lifecycle_lock:
            if self._state == "closed":
                return
            self._state = "closing"
            try:
                self._close_unlocked()
            except BaseException:
                self._state = "cleanup_required"
                raise
            self._state = "closed"

    def _close_unlocked(self) -> None:
        errors: list[tuple[str, BaseException]] = []
        interrupt: BaseException | None = None
        for name, resource in [
            ("backend", self.backend),
            ("artifacts", self.artifacts),
            ("com_ports", self.com_ports),
            ("can_buses", self.can_buses),
            ("adapters", self.adapters),
        ]:
            try:
                resource.close()
            except BaseException as error:
                errors.append((name, error))
                if name == "backend" and self._debug_lease is not None:
                    debug_sessions = getattr(self.backend, "_debug", None)
                    audit_broken = getattr(debug_sessions, "_audit_broken", None) is not None
                    try:
                        self._debug_lease.quarantine("debug_backend_cleanup_exception", error, audit_broken=audit_broken)
                    except BaseException as quarantine_error:
                        errors.append(("backend_coordination", quarantine_error))
                if interrupt is None and isinstance(error, (KeyboardInterrupt, SystemExit)):
                    interrupt = error
            else:
                if name == "backend" and self._debug_lease is not None:
                    lease = self._debug_lease
                    shutdown_result: JsonObject | None = None
                    try:
                        shutdown_result = self._lease_result(
                            {
                                "ok": True,
                                "tool": "debug_stop_session",
                                "active": False,
                                "status": "stopped",
                                "summary": "Debug session stopped during service shutdown.",
                            },
                            lease,
                        )
                        if shutdown_result.get("audit_ok") is False:
                            raise RuntimeError("Debug shutdown report could not be persisted.")
                        lease.resolve_retryable_cleanup("debug_backend_cleanup_exception")
                        if lease.state != "active":
                            raise RuntimeError("Debug shutdown completed, but prior target state remains unconfirmed.")
                        if not lease.release():
                            raise RuntimeError("Debug shutdown lease release remained unconfirmed.")
                    except BaseException as error:
                        try:
                            lease.quarantine("debug_shutdown_reporting_failed", error, audit_broken=bool(shutdown_result and shutdown_result.get("audit_ok") is False))
                        except BaseException as quarantine_error:
                            errors.append(("backend_coordination", quarantine_error))
                        errors.append(("backend_coordination", error))
                        if interrupt is None and isinstance(error, (KeyboardInterrupt, SystemExit)):
                            interrupt = error
                    else:
                        self._debug_lease = None
        for provisional_error in cleanup_provisional_handles(self.coordinator.owner_marker):
            errors.append(("provisional_handle", RuntimeError(provisional_error)))
        process_errors = cleanup_registered_processes(owner_marker=self.coordinator.owner_marker)
        errors.extend(("process_registry", RuntimeError(error)) for error in process_errors)
        if not errors:
            self.coordinator.close()
        if interrupt is not None:
            if errors:
                cleanup_details = "; ".join(f"{name}: {type(error).__name__}: {error}" for name, error in errors)
                interrupt.args = (*interrupt.args, f"Cleanup errors: {cleanup_details}")
            raise interrupt
        if errors:
            details = "; ".join(f"{name}: {type(error).__name__}: {error}" for name, error in errors)
            raise RuntimeError(f"Agentic HIL service cleanup failed: {details}") from errors[0][1]


def number_argument(value: object) -> float | None:
    if value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def adapter_payload(args: JsonObject) -> JsonObject:
    return {key: value for key, value in args.items() if key != "adapter_id"}


def tool_error(tool: str, error_type: str, summary: str) -> JsonObject:
    return {"ok": False, "tool": tool, "error_type": error_type, "summary": summary}


def audited_hardware_tools() -> set[str]:
    return {
        "debugger_probes_list", "probe_target", "flash_firmware", "reset_target", "debug_start_session",
        "debug_set_breakpoint", "debug_continue", "debug_symbol_info", "debug_dump_symbol_ihex",
        "com_session_start", "com_write", "com_read", "can_session_start", "can_send", "can_read",
        "adapter_session_start", "adapter_set_value", "adapter_inject_fault", "adapter_measure",
    }


def containment_tools() -> set[str]:
    return {
        "debug_stop_session", "debug_clear_breakpoints", "debug_halt",
        "com_session_stop", "can_session_stop", "adapter_session_stop", "adapter_clear_fault",
    }


def debugger_effect_tools() -> set[str]:
    return {
        "debugger_probes_list", "probe_target", "flash_firmware", "reset_target", "debug_start_session",
        "debug_set_breakpoint", "debug_continue", "debug_halt", "debug_clear_breakpoints", "debug_symbol_info", "debug_dump_symbol_ihex",
    }
