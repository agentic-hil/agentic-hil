from __future__ import annotations

import math
import threading
from datetime import datetime, timezone
from pathlib import Path

import yaml

from agentic_hil import __version__
from agentic_hil.artifacts import ArtifactManager
from agentic_hil.bootstrap import apply_discovery_to_template, discover_attached_hardware
from agentic_hil.can import CanBusService
from agentic_hil.comports import ComPortService
from agentic_hil.config import (
    CONFIG_ENV,
    DEFAULT_CONFIG_TEMPLATE,
    GENERATED_PROJECT_PERMISSIONS,
    GENERATED_WRITE_PERMISSIONS,
    ConfigError,
    authoritative_config_target,
    carry_over_permissions,
    deny_every_write,
    load_authoritative_config,
    provisionable_state_root,
    secure_atomic_write_text,
    secure_optional_read_text,
    secure_remove_file,
    secure_user_file_lock,
    write_generated_config,
)
from agentic_hil.configwrite import (
    PROJECT_CONFIG_DESCRIBE,
    PROJECT_CONFIG_SET,
    project_config_describe,
    project_config_set,
)
from agentic_hil.contracts import MCP_TOOL_NAMES, validate_tool_arguments
from agentic_hil.coordination import (
    DEBUGGER_DISCOVERY_RESOURCE,
    DEBUGGER_READONLY_RESULT_REASON,
    RETRYABLE_CLEANUP_REASONS,
    CoordinationError,
    HardwareCoordinator,
    HardwareLease,
    debugger_effect_resources,
)
from agentic_hil.debugger import DebuggerBackend, create_debugger_backend
from agentic_hil.devices import DeviceError, resolve_devices
from agentic_hil.knowledge import remediation_fields
from agentic_hil.process import cleanup_registered_processes, managed_process_owner
from agentic_hil.provisional import cleanup_provisional_handles
from agentic_hil.report import (
    attach_canonical_audit_evidence,
    audit_unavailable,
    claim_auto_recover_default_warning,
    ensure_audit_ready,
    overall_success,
    read_last_report,
    write_report,
)
from agentic_hil.types import AgenticHILConfig, DebuggerPermissions, JsonObject


class AgenticHILToolService:
    def __init__(
        self,
        config: AgenticHILConfig,
        backend: DebuggerBackend | None = None,
        artifacts: ArtifactManager | None = None,
        com_ports: ComPortService | None = None,
        can_buses: CanBusService | None = None,
        coordinator: HardwareCoordinator | None = None,
        frontend: str = "python",
    ):
        self.config = config
        self.coordinator = coordinator or HardwareCoordinator(config, frontend)
        # An injected coordinator (e.g. a per-device multi-board service sharing
        # the base coordinator) is NOT owned here: this service must not close it
        # or run its owner-scoped process/handle cleanup on teardown.
        self._owns_coordinator = coordinator is None
        self.backend = backend or create_debugger_backend(self.config)
        self.artifacts = artifacts or ArtifactManager(self.config)
        self.com_ports = com_ports or ComPortService(self.config, self.coordinator)
        self.can_buses = can_buses or CanBusService(self.config, self.coordinator)
        self._debug_artifact: JsonObject | None = None
        self._debug_lease: HardwareLease | None = None
        # A one-shot lease that quarantined has no session handle to reach it
        # again. Without this the lease stays registered and unreachable for the
        # rest of the process lifetime, so even a retryable incident could only
        # ever be cleared by an operator running `agentic-hil recover`.
        self._quarantined_lease: HardwareLease | None = None
        self._machine_recovery_incident: str | None = None
        self._machine_recovery_attempts = 0
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

    @property
    def debugger_permissions(self) -> DebuggerPermissions:
        """The bound probe's grants, or nothing at all.

        An unbound config has no board to authorize, and _call_unlocked refuses
        those calls with the name it needs before any of these gates run; the
        deny-all fallback only keeps a direct method call from reading grants
        off a probe that was never selected."""
        return self.config.debugger.permissions if self.config.debugger is not None else DebuggerPermissions()

    def debugger_info(self) -> JsonObject:
        if self.config.debugger is None:
            return unbound_debugger_error("debugger_info", self.config)
        if not self.config.probe_allowed():
            return tool_error("debugger_info", "permission_denied", "Debugger execution is disabled by the authoritative config.")
        return self.backend.info()

    def debugger_probes_list(self) -> JsonObject:
        if self._dispatch_depth == 0:
            return self.call("debugger_probes_list")
        if not self.config.probe_allowed():
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
        if not self.debugger_permissions.allow_flash:
            return tool_error("flash_firmware", "permission_denied", "Flashing is disabled by the authoritative config.")
        image_path = payload.get("image_path")
        artifact_id = payload.get("artifact_id")
        if bool(image_path) == bool(artifact_id):
            return tool_error("flash_firmware", "invalid_argument", "Provide exactly one of image_path or artifact_id.")
        reset_after_flash = payload.get("reset_after_flash", False)
        if not isinstance(reset_after_flash, bool):
            return tool_error("flash_firmware", "invalid_argument", "reset_after_flash must be a boolean.")
        if reset_after_flash and not self.debugger_permissions.allow_reset:
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
        if not self.debugger_permissions.allow_reset:
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
            "bench_run_start": lambda: self.bench_run_start(args),
            "bench_run_stop": lambda: self.bench_run_stop(),
            "bench_run_status": lambda: self.bench_run_status(),
            # On a configured server this is the authorized-rewrite half: a
            # configuration already exists, so the call is refused unless a
            # person set permissions.allow_config_write on it.
            PROJECT_CONFIG_CREATE: lambda: project_config_create(Path(self.config.work_dir), self.config),
            PROJECT_CONFIG_DESCRIBE: lambda: project_config_describe(Path(self.config.work_dir), self.config, open_holds=self.open_hardware_holds()),
            PROJECT_CONFIG_SET: lambda: project_config_set(Path(self.config.work_dir), self.config, args.get("changes"), open_holds=self.open_hardware_holds()),
        }
        if name in dispatch:
            if name in debugger_tools() and self.config.debugger is None:
                return unbound_debugger_error(name, self.config)
            if name in probe_addressing_tools() and len(self.config.debuggers) > 1 and self.config.debugger is not None and self.config.debugger.probe_id is None:
                return unnamed_probe_error(name, self.config)
            if self.coordinator.blocked and name in audited_hardware_tools() and name not in containment_tools():
                try:
                    recovered = self._attempt_machine_recovery()
                except Exception as error:
                    # Recovery is the one path allowed to unblock hardware, so a
                    # fault inside it must never fail open: the incident simply
                    # stays exactly as it was and the operator still owns it.
                    self._poison_quietly("machine_recovery_failed", error, audit_broken=isinstance(error, (ConfigError, OSError)))
                    recovered = None
                if recovered is None:
                    return {
                        "ok": False,
                        "tool": name,
                        "error_type": "resource_quarantined",
                        "summary": "Hardware effects are blocked until unresolved cleanup or audit state is recovered.",
                        "cleanup_required": True,
                        "quarantined": True,
                        "retry_safe": False,
                        "auto_recovery_attempted": True,
                        "cleanup_reasons": sorted({reason for lease in self.coordinator.leases.values() for reason in lease.cleanup_reasons()}),
                        "quarantine_id": self.coordinator.quarantine_id,
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

    def open_hardware_holds(self) -> JsonObject | None:
        """What this server is holding, or None when it holds nothing.

        A configuration write while something is held would change the policy
        under an active lock — the mirror image of the rule that a run may only
        touch what it declared. Both a declared run and a lease taken outside one
        count: a COM session outlives `bench_run_stop` by design, so asking only
        whether a run is open would miss the case where a board is still held.
        """
        run = self.coordinator.run_status()
        leases = sorted(lease.lease_id for lease in self.coordinator.leases.values())
        if not run.get("run_active") and not leases:
            return None
        holds: JsonObject = {
            "run_active": bool(run.get("run_active")),
            "declared_devices": run.get("declared_devices") or [],
            "held_devices": run.get("held_devices") or [],
            "open_leases": leases,
        }
        if run.get("run_label"):
            holds["run_label"] = run["run_label"]
        if run.get("run_started_at"):
            holds["run_started_at"] = run["run_started_at"]
        return holds

    def bench_run_start(self, payload: JsonObject | None = None) -> JsonObject:
        """Open a run that holds its devices across several calls.

        Without this the agent path had no run boundary at all: every MCP call
        took its device, released it at the end of the call, and left the board
        free between flash, reset and read. Decision 0018 promises the opposite —
        every device a run names is held for the run's duration — and that
        promise only held for `agentic-hil test-reactor` until now.

        The devices are resolved completely before anything is locked, and the
        whole set is then taken in one all-or-nothing acquisition, so a run whose
        third device is busy has never held its first two."""
        payload = payload or {}
        try:
            devices = resolve_devices(self.config, payload.get("devices"))
        except DeviceError as error:
            return {"tool": "bench_run_start", "side_effect_committed": False, **error.result}
        try:
            return self.coordinator.begin_run(
                devices,
                label=str(payload["label"]) if payload.get("label") is not None else None,
                wait_s=float(payload.get("wait_s", 0.0)),
            )
        except CoordinationError as error:
            return {"tool": "bench_run_start", "side_effect_committed": False, **error.result}

    def bench_run_stop(self) -> JsonObject:
        """Close the run and give the devices back.

        Idempotent: closing a run that is not open is how an agent recovers from
        losing track of its own state, so it answers instead of refusing."""
        try:
            return self.coordinator.end_run()
        except CoordinationError as error:
            return {"tool": "bench_run_stop", "side_effect_committed": False, **error.result}

    def bench_run_status(self) -> JsonObject:
        return self.coordinator.run_status()

    def _poison_quietly(self, reason: str, error: object | None = None, *, audit_broken: bool = False) -> Exception | None:
        """Quarantine the coordinator without letting a coordination failure mask the primary hardware error."""
        try:
            self.coordinator.poison(reason, error, audit_broken=audit_broken)
        except Exception as poison_error:
            return poison_error
        return None

    def _attempt_machine_recovery(self) -> JsonObject | None:
        """Clear a recoverable incident without an operator, or refuse to.

        Two safe-state predicates, and the weakest one that can settle the open
        reason is the one that runs. Both first reap this owner's leftover
        debugger processes. `readonly_probe` then re-reads the probe through
        probe_target, which connects without resetting on every backend (OpenOCD
        `init; targets`, pyOCD `status`, ST-Link `-HOTPLUG`), so it cannot change
        the state it is attesting to. `reset_halt` additionally drives the target
        into a defined halted state first, which is what settles an unconfirmed
        flash, reset, or session start — a physical act, gated on the bench's
        recovery.auto_recover policy and on allow_reset.

        Returns the recovery report, or None when the incident is not machine
        recoverable or a predicate did not confirm the safe state."""
        allowed = self.coordinator.recoverable_reasons()
        reason = self.coordinator.retryable_incident(allowed) if allowed else None
        if reason is None or not self.config.probe_allowed():
            return None
        if not self._machine_recovery_attempt_allowed():
            return None
        # Never drive the board for a reason a re-read already settles: the
        # stronger predicate is a physical act, not a default.
        needs_reset = reason not in RETRYABLE_CLEANUP_REASONS
        if cleanup_registered_processes(owner_marker=self.coordinator.owner_marker):
            return None
        if needs_reset:
            reset = self._invoke_dispatch(lambda: self.backend.reset_target("halt"))
            if not overall_success(reset):
                return None
        verification = self._invoke_dispatch(self.backend.probe_target)
        if not overall_success(verification) or verification.get("target_detected") is not True:
            return None
        policy = self.config.recovery
        record: JsonObject = {
            "ok": True,
            "tool": "hardware_recover",
            "recovery": "machine_attested",
            "reason": reason,
            "quarantine_id": self.coordinator.quarantine_id,
            "safe_state_confirmed": True,
            "processes_reaped": True,
            "safe_state_predicate": "reset_halt" if needs_reset else "readonly_probe",
            "auto_recover_policy": policy.auto_recover,
            "auto_recover_policy_source": "config" if policy.auto_recover_explicit else "default",
            "summary": (
                "Target was reset into a halted state, verified, and the incident cleared without operator attestation."
                if needs_reset
                else "Cleanup state was verified by a read-only probe re-read and cleared without operator attestation."
            ),
            "verification": {"tool": verification.get("tool"), "backend": verification.get("backend"), "target_detected": True},
        }
        if needs_reset and not policy.auto_recover_explicit and claim_auto_recover_default_warning(self.config):
            record["warnings"] = [
                "recovery.auto_recover is not named in the authoritative config, so it defaulted to reset_halt and this "
                "recovery reset the target without an operator. Set recovery.auto_recover explicitly to choose this bench's policy."
            ]
        # Evidence first: a quarantine must never be cleared without a durable
        # record of what attested the safe state, so a failed audit write aborts
        # the recovery instead of silently unblocking the hardware.
        report = write_report(self.config, record)
        if report.get("audit_ok") is False:
            self._poison_quietly("machine_recovery_audit_broken", audit_broken=True)
            return None
        if not self.coordinator.resolve_retryable_incident(reason, allowed=allowed):
            return None
        self._release_recovered_leases()
        return report

    def _machine_recovery_attempt_allowed(self) -> bool:
        """Bound recovery attempts per incident.

        Recovery can drive the target, and a refused attempt leaves the
        quarantine standing, so every later call would retry it. A flapping probe
        must not translate into an unbounded series of resets."""
        incident = self.coordinator.quarantine_id
        if incident != self._machine_recovery_incident:
            self._machine_recovery_incident = incident
            self._machine_recovery_attempts = 0
        if self._machine_recovery_attempts >= self.config.recovery.max_attempts:
            return False
        self._machine_recovery_attempts += 1
        return True

    def _release_recovered_leases(self) -> None:
        """Drop every lease handle the cleared incident left behind.

        Machine recovery only runs while no lease is active and it reaps this
        owner's debugger processes, so any debug session is definitively gone.
        Keeping its handle would let a later call act as if a live session were
        still attached to the board."""
        handles: list[HardwareLease] = []
        for lease in (self._debug_lease, self._quarantined_lease):
            if lease is not None and not any(lease is held for held in handles):
                handles.append(lease)
        self._debug_lease = None
        self._quarantined_lease = None
        if self._debug_artifact is not None:
            self.artifacts.release_stage(self._debug_artifact)
            self._debug_artifact = None
        for lease in handles:
            lease.release()

    def _invoke_dispatch(self, callback) -> JsonObject:
        self._dispatch_depth += 1
        try:
            with managed_process_owner(self.coordinator.owner_marker):
                return callback()
        finally:
            self._dispatch_depth -= 1

    def _debug_permission_failure(self, name: str, args: JsonObject) -> JsonObject | None:
        if name == "flash_firmware":
            if not self.debugger_permissions.allow_flash:
                return self._invoke_dispatch(lambda: self.flash_firmware(args))
            if args.get("reset_after_flash") is True and not self.debugger_permissions.allow_reset:
                return self._invoke_dispatch(lambda: self.flash_firmware(args))
        if name == "reset_target" and not self.debugger_permissions.allow_reset:
            return self._invoke_dispatch(lambda: self.reset_target(args.get("mode", "run")))
        if name == "debug_continue" and not self.debugger_permissions.allow_debug_execution:
            return self._invoke_dispatch(lambda: self.debug_continue(args))
        if name == "probe_target" and not self.config.probe_allowed():
            return self._invoke_dispatch(self.probe_target)
        if name == "debugger_probes_list" and not self.config.probe_allowed():
            return self._invoke_dispatch(self.debugger_probes_list)
        if name == "debug_start_session":
            mode = args.get("mode", "attach")
            permissions = self.debugger_permissions
            denied = (
                not self.config.probe_allowed()
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
                unconfirmed = DEBUGGER_READONLY_RESULT_REASON if name in {"debugger_probes_list", "probe_target"} else "debugger_result_unconfirmed"
                lease.quarantine(unconfirmed, audit_broken=result.get("audit_ok") is False)
            written = self._lease_result(result, lease)
            if not requires_quarantine and written.get("audit_ok") is not False and lease.state == "active":
                lease.release()
            if lease.state != "released":
                self._quarantined_lease = lease
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
                written = self._lease_result(result, lease)
                # lease.release() handles both an active lease and a lease left in
                # the retryable "lease_release_unconfirmed" state by a prior
                # transient persist fault, so a debug release can converge instead
                # of sticking until process restart.
                if written.get("audit_ok") is not False and lease.release():
                    self._debug_lease = None
                    return self._recommit_lease_report(written, lease)
                if lease.state != "active":
                    written = self._lease_result({**result, "ok": False, "error_type": "cleanup_required", "summary": "Debug process cleanup completed, but prior target state remains unconfirmed."}, lease)
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
                        # release() converges an active lease or one left retryable
                        # by a prior release-persist fault; a genuine quarantine
                        # returns False and stays unconfirmed for operator recovery.
                        if not lease.release():
                            raise RuntimeError("Debug shutdown completed, but prior target state remains unconfirmed.")
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
        if self._owns_coordinator:
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


def tool_error(tool: str, error_type: str, summary: str) -> JsonObject:
    result = {"ok": False, "tool": tool, "error_type": error_type, "summary": summary}
    if error_type == "permission_denied":
        # A refusal that only states a fact reads to a caller like a closed door
        # to walk around. One weak model called this tool first, was refused,
        # diagnosed correctly through the other tools, and then flashed the board
        # with st-flash. The instruction belongs where the caller is looking.
        # Deliberately no verb phrase a caller could act on. An earlier wording
        # said "ask the operator to change the authoritative config" and a small
        # model rewrote the config itself to grant allow_flash.
        result["next_step"] = (
            "This refusal is the answer to the request. Report it and name the permission that is "
            "denied, then stop. You must not enable it: the authoritative configuration belongs to the "
            "operator and only the operator may edit it. You must not carry out the action another way "
            "either — a debugger, serial device or CAN adapter driven outside Agentic HIL defeats the "
            "policy this refusal enforces."
        )
    return result


def audited_hardware_tools() -> set[str]:
    return {
        "debugger_probes_list", "probe_target", "flash_firmware", "reset_target", "debug_start_session",
        "debug_set_breakpoint", "debug_continue", "debug_symbol_info", "debug_dump_symbol_ihex",
        "com_session_start", "com_write", "com_read", "can_session_start", "can_send", "can_read",
    }


def containment_tools() -> set[str]:
    return {
        "debug_stop_session", "debug_clear_breakpoints", "debug_halt",
        "com_session_stop", "can_session_stop",
    }


def unbound_debugger_error(tool: str, config: AgenticHILConfig) -> JsonObject:
    """Refuse a debugger call that has no probe to run on.

    Two different configurations land here and they need different answers. No
    MCP tool schema carries a probe name — this surface drives the one bound
    probe — so telling a caller to "name the debugger" would demand something
    the protocol cannot express, which is the shape of refusal that has already
    driven a model to reach for st-flash instead. Say what is actually wrong and
    which route exists, and mark the call unretryable so nobody loops on it."""
    configured = sorted(config.debuggers)
    if not configured:
        summary = (
            "No debugger is configured in the authoritative config, so this tool has no probe to act on. "
            "Only the operator can add one."
        )
    else:
        summary = (
            f"This MCP surface drives a single debug probe, and the authoritative config declares {len(configured)}, "
            "so none is bound. Debugger tools are unavailable here until the project configures exactly one probe. "
            "A multi-board run goes through `agentic-hil test-reactor` with a plan whose steps name their probe."
        )
    return {
        "ok": False,
        "tool": tool,
        # not_supported, not permission_denied: nothing is switched off here, the
        # surface simply cannot address a probe in this configuration.
        "error_type": "not_supported",
        "summary": summary,
        "configured_debuggers": configured,
        "side_effect_committed": False,
        "side_effect_status": "not_started",
        # No argument to this tool can change the outcome; retrying only wastes turns.
        "retry_safe": False,
    }


def probe_addressing_tools() -> set[str]:
    """Tools that drive one specific physical probe.

    Excludes the two that must keep working while an operator is still finding
    out which probes exist: debugger_probes_list enumerates them, and
    debugger_info only runs the toolchain's version command."""
    return debugger_tools() - {"debugger_probes_list", "debugger_info"}


def unnamed_probe_error(tool: str, config: AgenticHILConfig) -> JsonObject:
    """Refuse to drive a board when the bound probe names no hardware.

    Config load cannot demand a probe_id: discovering the ids is exactly what
    an operator does before they can write one down, and loading must work with
    no hardware attached. The demand belongs here, where a board is about to be
    driven and picking the wrong one is the failure that matters."""
    return {
        "ok": False,
        "tool": tool,
        "error_type": "not_supported",
        "summary": (
            f"Debugger '{config.debugger_id}' has no probe_id, and the project configures "
            f"{len(config.debuggers)} debuggers, so this call cannot say which board it means. "
            "Run `agentic-hil debugger-probes` to list the connected probes and give each entry its own probe_id. "
            "OpenOCD cannot enumerate probes; read the serial from the probe itself, or use a pyocd or stlink entry to discover it."
        ),
        "configured_debuggers": sorted(config.debuggers),
        "side_effect_committed": False,
        "side_effect_status": "not_started",
        "retry_safe": False,
    }


def debugger_tools() -> set[str]:
    """Every tool that acts on, or reports about, one debug probe. These need a
    bound probe: without one there is no board the call could mean."""
    return {
        *debugger_effect_tools(),
        "debugger_info", "debug_stop_session", "debug_get_session_status",
        "debug_list_breakpoints", "debug_get_stop_reason",
    }


def debugger_effect_tools() -> set[str]:
    return {
        "debugger_probes_list", "probe_target", "flash_firmware", "reset_target", "debug_start_session",
        "debug_set_breakpoint", "debug_continue", "debug_halt", "debug_clear_breakpoints", "debug_symbol_info", "debug_dump_symbol_ihex",
    }


# ---------------------------------------------------------------------------
# One-time agent provisioning of the project configuration.
#
# The engine is bootstrap.discover_attached_hardware(): probe id, controller and
# the probe's own COM device are precisely the values that are laborious to write
# by hand and impossible to guess, and they are the ONLY values the agent side
# contributes. Everything else is the deny-by-default skeleton. The agent authors
# no content, so it never decides allow_flash — an agent that could write the
# contents of this file would be granting itself the hardware, not configuring it.
#
# That closure is also why the gate needs no state outside the configuration. A
# configuration deleted out of band lets an agent generate a fresh one, and the
# fresh one is this same skeleton: the cycle costs a human their customised
# permissions and yields the agent nothing it did not already have.
PROJECT_CONFIG_CREATE = "project_config_create"
# The fixed profile the generated document is filled from. It carries names and
# transport defaults, never a permission: a profile read out of the workspace
# would be repository-controlled data deciding what the board may be told to do,
# which is exactly the self-authorization this path exists to avoid.
GENERATED_PROJECT_PROFILE: JsonObject = {
    "target": {"name": "detected-target"},
    "debuggers": {"dut": {"timeout_s": 60, "permissions": {}}},
    "com_ports": {"dut_uart": {"baudrate": 115200, "permissions": {}}},
}
GENERATED_CONFIG_HEADER = """# Generated by Agentic HIL for an AI agent that found no configuration for this
# workspace, out of what was attached to this machine at the time. No part of it
# was authored by the agent: every permission below is false, including
# permissions.allow_config_write, which is what stops the same agent from
# widening anything it wrote here. Reading a device needs no permission, so this
# file lets the agent observe the bench and nothing more.
#
# You are the one who opens it. Set the permission a task actually needs, and set
# permissions.allow_config_write: true only if you want the agent to regenerate
# this file from hardware discovery later.
"""


def project_config_create(workspace: Path, existing: AgenticHILConfig | None) -> JsonObject:
    """Generate this workspace's authoritative configuration.

    ``existing`` is the configuration this server is bound to, or None when it
    started without one. It is never taken from a caller's argument: generation
    is for the workspace the server is bound to and for no other, and no
    parameter exists that could name a different one."""
    try:
        return _project_config_create(workspace, existing)
    except ConfigError as error:
        return {"tool": PROJECT_CONFIG_CREATE, **error.to_dict(), "side_effect_committed": False, "side_effect_status": "not_started", "hardware_state": "unchanged"}


def _project_config_create(workspace: Path, existing: AgenticHILConfig | None) -> JsonObject:
    target_path = authoritative_config_target(workspace)
    with secure_user_file_lock(target_path):
        # The gate is the configuration and nothing else. A server bound to one
        # is gated by the permissions it loaded, even if the file has since been
        # removed underneath it: that server has a policy and may not replace it
        # with a fresh one. A server bound to none re-reads the path here, inside
        # the lock, so two of them racing cannot both conclude "no configuration"
        # and have the second overwrite what the first wrote.
        current = existing if existing is not None else _configuration_on_disk(workspace)
        if current is not None and not current.permissions.allow_config_write:
            return _config_write_denied(current, target_path)
        # Read-only, and the values it returns are the only ones the agent side
        # contributes. A file generated without discovery would name no probe and
        # no port, which leaves the project worse off than the
        # config_file_not_found it started from, so it refuses instead.
        discovery = discover_attached_hardware()
        if not overall_success(discovery):
            return {
                **discovery,
                "tool": PROJECT_CONFIG_CREATE,
                "summary": f"{discovery.get('summary', 'Hardware discovery failed.')} No configuration was generated.",
                "workspace_root": str(workspace),
                "next_step": "Attach exactly one supported probe and target, then call this tool again. Nothing was written.",
            }
        state_root = provisionable_state_root(workspace)
        document = _generated_document(workspace, state_root, discovery)
        # Deny first in both cases, so that "this tool introduces no grant" holds
        # by construction rather than by reading what filled the skeleton in. A
        # regeneration then puts back exactly the grants already on disk.
        deny_every_write(document)
        dropped: list[str] = [] if current is None else carry_over_permissions(document, current)
        text = GENERATED_CONFIG_HEADER + yaml.safe_dump(document, sort_keys=False, allow_unicode=False)

        previous_text = secure_optional_read_text(target_path)
        write_generated_config(target_path, workspace, text)
        try:
            written = load_authoritative_config(workspace)
        except ConfigError as error:
            # The generated text validated on a temporary file, so reaching here
            # means the authoritative rules refused what parsing accepted. The
            # operator's own file must not be what pays for that.
            return _rolled_back(target_path, previous_text, error)

    created = current is None
    return {
        "ok": True,
        "tool": PROJECT_CONFIG_CREATE,
        "summary": (
            "Deny-by-default project configuration generated from attached hardware; it denies further configuration writes by this server."
            if created
            else "Project configuration regenerated from attached hardware; every permission was carried over unchanged."
        ),
        "created": created,
        "path": written.config_path,
        "workspace_root": written.workspace_root,
        "state_root": written.state_root,
        "optional_override": f"{CONFIG_ENV}={written.config_path}",
        "permissions": _permission_summary(written),
        "dropped_entries": dropped,
        # A server that started without a configuration binds the one it just
        # wrote. A server that already had one keeps serving what it loaded,
        # because swapping a configuration under open sessions and held device
        # leases is not something a tool call may do behind their back.
        "reload_required": existing is not None,
        "hardware_discovery": discovery,
        "side_effect_committed": False,
        "side_effect_status": "not_started",
        "hardware_state": "unchanged",
        "cleanup_required": False,
        "next_steps": _generated_next_steps(written, created=created),
    }


def _configuration_on_disk(workspace: Path) -> AgenticHILConfig | None:
    """This workspace's configuration as it is right now, or None if there is none.

    Only an absent file counts as none. A configuration that exists and does not
    load is an error the caller has to see, not an empty slot to write over."""
    try:
        return load_authoritative_config(workspace)
    except ConfigError as error:
        if error.error_type == "config_file_not_found":
            return None
        raise


def _rolled_back(target_path: Path, previous_text: str | None, error: ConfigError) -> JsonObject:
    """Put back whatever was at the configuration path, and say so."""
    result: JsonObject = {
        "tool": PROJECT_CONFIG_CREATE,
        **error.to_dict(),
        "summary": "The generated configuration did not load as authoritative and the previous state of the file was restored.",
        "path": str(target_path),
        "side_effect_committed": False,
        "side_effect_status": "not_started",
        "hardware_state": "unchanged",
    }
    try:
        if previous_text is None:
            secure_remove_file(target_path)
        else:
            secure_atomic_write_text(target_path, previous_text)
    except (ConfigError, OSError) as failure:
        result["rollback_error"] = str(failure)
    return result


def _generated_document(workspace: Path, state_root: Path, discovery: JsonObject) -> JsonObject:
    template = yaml.safe_load(DEFAULT_CONFIG_TEMPLATE)
    if not isinstance(template, dict):  # pragma: no cover - the shipped skeleton is a mapping
        raise ConfigError("config_invalid", "The bundled configuration skeleton is not a mapping.", {"field": "template"})
    configured = apply_discovery_to_template(template, GENERATED_PROJECT_PROFILE, discovery)
    return {
        "workspace_root": str(workspace),
        "state_root": str(state_root),
        "provenance": {
            "created_by": "agent",
            "created_via": f"mcp:{PROJECT_CONFIG_CREATE}",
            "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "agentic_hil_version": __version__,
        },
        **configured,
    }


def _permission_summary(config: AgenticHILConfig) -> JsonObject:
    return {
        **{name: bool(getattr(config.permissions, name, False)) for name in GENERATED_PROJECT_PERMISSIONS},
        "debuggers": {name: {flag: bool(getattr(entry.permissions, flag)) for flag in GENERATED_WRITE_PERMISSIONS["debuggers"]} for name, entry in config.debuggers.items()},
        "com_ports": {name: {"allow_write": entry.permissions.allow_write} for name, entry in config.com_ports.items()},
        "can_buses": {name: {"allow_write": entry.permissions.allow_write} for name, entry in config.can_buses.items()},
    }


def _generated_next_steps(config: AgenticHILConfig, *, created: bool) -> list[str]:
    steps = [
        "Report where this configuration is and that an agent generated it.",
        f"A person opens what this project needs by editing {config.config_path}; that is the only way any write permission in it becomes true.",
    ]
    if created:
        steps.append(
            "This server will not write this configuration again: the file it just wrote denies that, and the refusal "
            "comes out of the file itself, so what holds is exactly what a person reads there."
        )
        steps.append("Hardware tools read this configuration from now on; `debugger_info` confirms the bench is reachable.")
    else:
        steps.append(
            "This server is still serving the configuration it loaded at startup. Ask the operator to restart the "
            "MCP server before relying on anything this rewrite changed."
        )
    return steps


def _config_write_denied(existing: AgenticHILConfig, target_path: Path) -> JsonObject:
    result = tool_error(
        PROJECT_CONFIG_CREATE,
        "permission_denied",
        "Writing this project's configuration is denied by permissions.allow_config_write in the configuration itself. "
        "This server may read it and not change it, and only a person editing that file can change that.",
    )
    return {
        **result,
        "permission": "allow_config_write",
        "reason": "config_write_denied",
        "workspace_root": existing.workspace_root,
        "path": str(target_path),
        "side_effect_committed": False,
        "side_effect_status": "not_started",
        "hardware_state": "unchanged",
        "retry_safe": False,
    }


def unprovisioned_tool_error(tool: str, workspace: Path) -> JsonObject:
    """Refuse a hardware call on a server that has no configuration yet."""
    return {
        "ok": False,
        "tool": tool,
        "error_type": "config_file_not_found",
        "summary": (
            "This workspace has no Agentic HIL configuration, so this call has no bench, no permission and no state "
            f"directory to use. `{PROJECT_CONFIG_CREATE}` generates one from attached hardware, once."
        ),
        "workspace_root": str(workspace),
        "next_step": (
            f"Call `{PROJECT_CONFIG_CREATE}`. It takes no arguments, writes a configuration in which every permission is "
            "false, and then denies itself further writes. Ask a person for anything beyond reading the bench."
        ),
        **remediation_fields("config_file_not_found"),
        "side_effect_committed": False,
        "side_effect_status": "not_started",
        "hardware_state": "unchanged",
        "retry_safe": False,
    }


class UnprovisionedToolService:
    """The MCP server for a workspace whose configuration does not exist yet.

    It advertises the same tools as a configured server and refuses all but one
    of them, because a tool list that changed underneath a running session would
    leave the host holding a list nothing announced a change to. The moment a
    configuration exists it binds a real service to it and hands every call on,
    so the session that generated the configuration is the session that can then
    read the bench.
    """

    def __init__(self, workspace: Path, frontend: str = "mcp"):
        self.workspace = Path(workspace).resolve()
        self._frontend = frontend
        self._service: AgenticHILToolService | None = None
        self._lock = threading.RLock()

    @property
    def config(self) -> AgenticHILConfig | None:
        service = self._bind()
        return service.config if service is not None else None

    def _bind(self) -> AgenticHILToolService | None:
        with self._lock:
            if self._service is None:
                try:
                    config = load_authoritative_config(self.workspace)
                except ConfigError:
                    return None
                self._service = AgenticHILToolService(config, frontend=self._frontend)
            return self._service

    def call(self, name: str, arguments: JsonObject | None = None) -> JsonObject:
        service = self._bind()
        if name == PROJECT_CONFIG_CREATE:
            validation_error = validate_tool_arguments(name, arguments if isinstance(arguments, dict) else {})
            if validation_error is not None:
                return validation_error
            result = project_config_create(self.workspace, service.config if service is not None else None)
            if overall_success(result):
                # Bind what this call just wrote, so the tools it unblocks are
                # usable from the session that unblocked them.
                self._bind()
            return result
        if service is None:
            if name not in MCP_TOOL_NAMES:
                return {"ok": False, "tool": name, "error_type": "unknown_tool", "summary": "Unknown Agentic HIL tool."}
            return unprovisioned_tool_error(name, self.workspace)
        return service.call(name, arguments)

    def close(self) -> None:
        with self._lock:
            if self._service is not None:
                self._service.close()
