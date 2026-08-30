from __future__ import annotations

import math
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

import yaml

from agentic_hil import __version__
from agentic_hil.adopt import (
    PROJECT_CONFIG_ADOPT,
    configured_probe_resource,
    discover_under_hardware_lease,
    project_config_adopt_hardware,
)
from agentic_hil.artifacts import ArtifactManager
from agentic_hil.bench import BenchMutex, DeviceBusyError, validated_wait
from agentic_hil.bootstrap import DEFAULT_PROJECT_PROFILE, apply_discovery_to_template, discover_attached_hardware
from agentic_hil.can import CanBusService
from agentic_hil.comports import ComPortService
from agentic_hil.config import (
    CONFIG_ENV,
    DEFAULT_CONFIG_TEMPLATE,
    GENERATED_WRITE_PERMISSIONS,
    ConfigError,
    authoritative_config_target,
    bind_debugger,
    carry_over_permissions,
    generated_permissions,
    grant_every_permission,
    load_authoritative_config,
    permission_summary,
    provisionable_state_root,
    secure_atomic_write_text,
    secure_optional_read_text,
    secure_remove_file,
    secure_user_file_lock,
    write_generated_config,
)
from agentic_hil.configreload import PROJECT_CONFIG_RELOAD, reload_description
from agentic_hil.configstate import config_status, with_config_status
from agentic_hil.configwrite import (
    NOT_STARTED,
    PROJECT_CONFIG_DESCRIBE,
    PROJECT_CONFIG_SET,
    permission_surface,
    project_config_describe,
    project_config_set,
)
from agentic_hil.contracts import MCP_TOOL_NAMES, validate_tool_arguments
from agentic_hil.coordination import (
    ATTESTATION_NO_CONTACT_CLASS,
    ATTESTATION_OPERATOR_VIA_AGENT,
    ATTESTATION_RECOVERY_ACTION,
    DEBUGGER_DISCOVERY_RESOURCE,
    DEBUGGER_READONLY_RESULT_REASON,
    DEBUGGER_READONLY_TARGET_STATE_REASON,
    RECOVERY_ACTION_REASONS,
    RECOVERY_ACTION_VIA,
    RECOVERY_ACTOR_AGENT,
    RETRYABLE_CLEANUP_REASONS,
    CoordinationError,
    HardwareCoordinator,
    HardwareLease,
    debugger_effect_resources,
    nothing_standing_result,
)
from agentic_hil.debugger import DebuggerBackend, create_debugger_backend
from agentic_hil.devices import DeviceError, can_device, resolve_devices, uart_device
from agentic_hil.knowledge import (
    RECOVERY_PHYSICAL_CHECK_ERROR,
    attach_quarantine_guidance,
    recovery_operator_command,
    remediation_fields,
)
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
from agentic_hil.runlifecycle import request_run_stop, run_status
from agentic_hil.types import AgenticHILConfig, DebuggerPermissions, JsonObject, fold_hardware_id
from agentic_hil.upgrade import SERVER_UPGRADE, server_upgrade


def _backend_kind(config: AgenticHILConfig | None) -> str | None:
    """Which backend class a configuration calls for, or None for no probe.

    The one thing `DebuggerBackend.reconfigure` cannot fix. Every backend keeps
    its own config, so this reads the same answer off an object as off a
    configuration and the two are compared without a second attribute to keep in
    step."""
    debugger = getattr(config, "debugger", None)
    return debugger.type if debugger is not None else None


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
        # Which backend class drives this bench is decided by `debuggers.<n>.type`
        # and by whether a probe is bound at all, so a description reload can
        # invalidate the *object* and not only its config — a bench that
        # configured no debugger and now configures one holds an
        # UnboundDebuggerBackend that no `reconfigure` can turn into an OpenOCD
        # one. That rebuild is only ours to make when we built the backend: an
        # injected one belongs to its caller.
        self._owns_backend = backend is None
        self.artifacts = artifacts or ArtifactManager(self.config)
        self.com_ports = com_ports or ComPortService(self.config, self.coordinator)
        self.can_buses = can_buses or CanBusService(self.config, self.coordinator)
        self._debug_artifact: JsonObject | None = None
        # The last ELF this service put on the target, kept so a backend with no
        # debug session of its own can still resolve a symbol against the image
        # the board is actually running. Only a confirmed flash writes it, and
        # only of a .elf: a .hex or .bin carries no symbol table, and an
        # unconfirmed flash is exactly the case where nobody knows what is on
        # the board. It is a fact about this bench, not an artifact handle —
        # nothing is staged or held open by it.
        self._symbol_elf: JsonObject | None = None
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
        """Which debugger backend this server drives, and which file said so.

        The configuration is named in every case, including the two refusals.
        This is the answer an operator compares against `agentic-hil doctor`, and
        the two came apart silently once already: `doctor` read the file and said
        `pyocd` while this said `stlink` out of the version loaded at startup,
        with nothing anywhere to say which was which.

        The file is sampled after the backend has answered, never before.
        `backend.info()` runs a version subprocess and is allowed seconds; a
        status taken before it would report an edit made during that window as
        `unchanged`, which is the one answer this block exists to stop being
        given by accident."""
        if self.config.debugger is None:
            result = unbound_debugger_error("debugger_info", self.config)
        elif not self.config.probe_allowed():
            result = tool_error("debugger_info", "permission_denied", "Debugger execution is disabled by the authoritative config.")
        else:
            result = self.backend.info()
        return with_config_status(result, config_status(self.config), prominent=True)

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
            result = self.backend.flash_firmware(staged["artifact"], reset_after_flash)
        except BaseException:
            # A backend that raised may have contacted or partly written the
            # target, so the ELF this bench remembered no longer provably
            # describes the image on the board. `_remember_symbol_elf` runs only
            # on a returned result and is never reached here, so drop the source
            # explicitly before the failure propagates: the coordination wrapper
            # reports the effect as unknown, and a later ST-Link dump must not
            # resolve a symbol against a build a failed flash may have replaced.
            # Only a returned result that proves the flash never started keeps the
            # old source (see `_remember_symbol_elf`).
            self._symbol_elf = None
            raise
        finally:
            self.artifacts.release_stage(staged["artifact"])
        self._remember_symbol_elf(validation["artifact"], result)
        return result

    def _remember_symbol_elf(self, artifact: JsonObject, result: JsonObject) -> None:
        """Record the flashed ELF as this bench's symbol source, or drop it.

        A confirmed flash decides what the target runs, so it decides the symbol
        source outright: an ELF becomes it, and a `.hex`/`.bin` carries no
        symbols, so whatever ELF this bench remembered no longer describes the
        image on the board and is dropped rather than left to answer for a build
        that is no longer there.

        An unconfirmed flash may have written part of a new image. Only a flash
        that provably never reached the target (`side_effect_status` still
        `not_started`) leaves the remembered ELF proven; anything else — an
        unknown or partial hardware effect — drops it rather than resolve a
        symbol against an image the board may no longer be running.

        The pre-staging artifact is kept, not the staged copy — staging is
        released as soon as the backend returns, while this path stays readable
        for as long as the file does. Its digest is kept with it and revalidated
        at dump time, so a rebuild that replaces the file cannot be read while
        the result still claims the flashed image's bytes."""
        if result.get("ok") is True:
            self._symbol_elf = artifact if Path(str(artifact["resolved_path"])).suffix.lower() == ".elf" else None
            return
        if result.get("side_effect_status") != "not_started":
            self._symbol_elf = None

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
        # The same offer the two reads get, and this one needs it most: where a
        # symbol lives is a property of the image, so a backend with no session
        # to ask can answer it from the ELF a confirmed flash proved is on the
        # target, without opening a probe at all. A backend that has a session
        # ignores it.
        return self.backend.debug_symbol_info(symbol.strip(), self._symbol_elf)

    def debug_symbol_value(self, payload: JsonObject | None = None) -> JsonObject:
        if self._dispatch_depth == 0:
            return self.call("debug_symbol_value", payload)
        symbol = (payload or {}).get("symbol")
        if not isinstance(symbol, str) or not symbol.strip():
            return tool_error("debug_symbol_value", "invalid_argument", "symbol must be a non-empty string.")
        # The same offer the dump gets: an ELF a confirmed flash proved is on the
        # target, for a backend with no session to ask. A backend that has one
        # ignores it.
        return self.backend.debug_symbol_value(symbol.strip(), self._symbol_elf)

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
        return self.backend.debug_dump_symbol_ihex(symbol.strip(), output["output"], self._symbol_elf)

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
                result: JsonObject = {"ok": False, "tool": name, "error_type": "service_closed" if self._state == "closed" else "service_cleanup_required", "summary": "Agentic HIL service is not accepting new calls.", "side_effect_committed": False, "cleanup_required": self._state == "cleanup_required"}
            else:
                result = self._call_unlocked(name, arguments)
                result = self._stand_down_after_call(result)
            # A configuration that has moved since startup belongs in every
            # answer, not only in the two that name it outright: a caller reading
            # a flash result has the same reason to know that the policy it was
            # decided by is no longer the policy on disk. Answers that already
            # carry the block — `debugger_info`, and the configuration tools —
            # are left alone, so there is one statement per result and it always
            # comes from the same check.
            #
            # Prominence is decided by the tool that was *asked for*, not by
            # which code path answered it. `debugger_info` and
            # `project_config_describe` promise the block in every case, and
            # several of their answers never reach the body that attaches it: the
            # unbound-probe refusal is returned from the gate below, and a
            # `ConfigError` out of the describe path is caught before its own
            # result is built. Deciding here means an early refusal from one of
            # those two carries the same statement as a success.
            #
            # A quarantining result additionally carries, per reason, what was
            # attempted, what is confirmed, what remains unknown, and what the
            # operator checks on the physical board before signing
            # `--confirm-safe-state`. One catalogue (knowledge.py), one attach
            # point, so every tool's quarantine reads the same way.
            result = attach_quarantine_guidance(result)
            if "config_status" in result:
                return result
            return with_config_status(result, config_status(self.config), prominent=name in prominent_config_status_tools())

    def _stand_down_after_call(self, result: JsonObject) -> JsonObject:
        """End an incident this call left open that owes nobody a gate.

        The last thing a call does, and deliberately the last: the run teardown
        and the recovery-class settlement have both already run by the time this
        is asked. What has not, for a call outside a run, is the automatic
        recovery the acquire path used to run on the *next* call, and that is why
        it is attempted here first. The gate it used to sit behind is gone, so
        this is where it belongs: the report exists, the call is finished, and
        the reset-into-halt this bench's policy allows is exactly what settles an
        unconfirmed target. Nothing about the action changed, only when it is
        asked for.

        What it does not settle stands down, and that is the narrowing: an
        incident the policy withheld, or whose predicate did not confirm, is not
        a hold. The target's state is what the next reset and probe say it is,
        and a peripheral's is what the next open says.

        The envelope keeps its reasons and its guidance: what the call could not
        confirm is exactly what the caller needs to read, and it is the same text
        the quarantine carried. What goes is the claim that the bench is held."""
        if not isinstance(result, dict) or self.coordinator.run_active or self._debug_lease is not None:
            # A declared run is the agent's own hold, and its teardown is where
            # the recovery belongs: resetting the board at the end of step one
            # because step one failed would drive the target out from under step
            # two, and the verdict is the run's. `bench_run_stop` brings the same
            # incident back here with the run closed.
            #
            # A registered debug session is the agent's own hold on this bench,
            # and it is what makes both halves of this seam wrong here. The
            # recovery action reaps this owner's debugger processes, so running
            # it would take the live session with it; and ending the incident
            # without it would leave a partially written image with nothing
            # having driven the target into a defined state. The same reasoning
            # keeps session-scoped calls out of `implicit_run_tools`. So the
            # incident waits for the session: `debug_stop_session` and the
            # recovery-class calls stay reachable through it, and whichever of
            # them ends the session brings the next call here with the hold gone.
            return result
        if self.coordinator.blocked:
            try:
                self._attempt_machine_recovery()
            except Exception as error:
                # Same rule as everywhere else recovery runs: a fault inside it
                # never fails open, and never replaces the call's own answer.
                self._poison_quietly("machine_recovery_failed", error, audit_broken=isinstance(error, (ConfigError, OSError)))
        stood_down = self.coordinator.stand_down()
        if stood_down is None:
            return result
        # Whatever the ended incident left registered goes back, because the call
        # that took it is over and the bench is not held for it any more. A
        # lease a live COM or CAN session owns is the exception and the only one:
        # the session is still there, still usable, and still the thing that
        # gives its device back. Without this the leases an incident used to keep
        # would sit registered for the rest of the process, and every later call
        # that asks whether this server holds anything would be told yes.
        session_leases = {id(session.lease) for session in (*self.com_ports.sessions.values(), *self.can_buses.sessions.values())}
        for lease in list(self.coordinator.leases.values()):
            if lease.state == "active" and id(lease) not in session_leases:
                lease.release()
        if self._quarantined_lease is not None and self._quarantined_lease.state != "active":
            self._quarantined_lease = None
        # Attached while the flags still say quarantined, so the reasons keep the
        # remediation text they had; `call` re-attaching it is a no-op.
        result = attach_quarantine_guidance(result)
        # `quarantined` and nothing else. It is the one field that says the bench
        # is being held, and it is the one that stopped being true. Its neighbour
        # `cleanup_required` says something the stand-down did not change: this
        # call left work behind, a debug session to stop, a state nobody
        # confirmed, and a caller reading a failed result still has to know it.
        return {**result, "incident_stood_down": stood_down, **({"quarantined": False} if result.get("quarantined") is True else {})}

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
            "debug_symbol_value": lambda: self.debug_symbol_value(args),
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
            # Deliberately outside every set above this dispatch. A plan run is
            # not one hardware call: it declares its own run over its own
            # service, and each of its steps passes through this gate on that
            # service under the permissions of the device the step names. Putting
            # the wrapper in `audited_hardware_tools` or `implicit_run_tools`
            # would wrap a run in a run and add a refusal the command line does
            # not have; putting it in `probe_addressing_tools` would demand one
            # configured probe of a plan that addresses boards by name.
            "test_reactor_run": lambda: self.test_reactor_run(args),
            "test_reactor_status": lambda: self.test_reactor_status(args),
            "test_reactor_stop": lambda: self.test_reactor_stop(args),
            # Deliberately not in `audited_hardware_tools`: it is the one call
            # that exists to answer a blocked bench, so the gate above — which
            # refuses every hardware tool while an incident is open — must not
            # stand in front of it.
            "hardware_recover": lambda: self.hardware_recover(args.get("operator_statement"), args.get("accept_config_change") is True),
            # On a configured server this is the authorized-rewrite half: a
            # configuration already exists, so the call is refused unless a
            # person set permissions.allow_config_write on it. It also reads a
            # board, so it goes in with this service's own coordinator and is
            # refused while this service holds a run or a session, exactly like
            # the adoption call below.
            PROJECT_CONFIG_CREATE: lambda: project_config_create(Path(self.config.work_dir), self.config, coordinator=self.coordinator, open_holds=self.open_hardware_holds()),
            PROJECT_CONFIG_DESCRIBE: lambda: project_config_describe(Path(self.config.work_dir), self.config, open_holds=self.open_hardware_holds()),
            PROJECT_CONFIG_SET: lambda: project_config_set(Path(self.config.work_dir), self.config, args.get("changes"), open_holds=self.open_hardware_holds()),
            # This one reads a board, so it goes in with this service's own
            # coordinator: the locks it takes are the locks a run here already
            # holds, and a probe held by another process on this machine answers
            # device_busy instead of being connected to behind its owner's back.
            PROJECT_CONFIG_ADOPT: lambda: project_config_adopt_hardware(Path(self.config.work_dir), self.config, args, coordinator=self.coordinator, open_holds=self.open_hardware_holds()),
            # Reads the file and changes nothing on disk, so no grant gates it —
            # reading is free here, and this cannot widen anything: it takes the
            # description and leaves every permission where it was. A bench whose
            # allow_config_description_write is false must still be able to pick
            # up a board somebody plugged in.
            PROJECT_CONFIG_RELOAD: lambda: self.reload_description(),
            # The one tool that changes neither the bench nor its configuration
            # but the code enforcing both. It takes this service's own
            # coordinator status rather than asking the locks a second time: a
            # declared run holds the devices and not the project lock, so
            # `bench_held` is the only reading that calls a held bench held,
            # and the refusal is the open-run rule applied to the release
            # instead of to the permissions.
            SERVER_UPGRADE: lambda: server_upgrade(self.config, self.coordinator.status()),
        }
        if name in dispatch:
            if name in debugger_tools() and self.config.debugger is None:
                return unbound_debugger_error(name, self.config)
            # A naming check, not a hardware one: with only one configured
            # debugger this call can only ever mean that one entry, so there is
            # no other name it could be confused with and nothing to demand a
            # probe_id to resolve. Two or more configured debuggers do carry
            # that ambiguity, and the bound one is refused until its own
            # probe_id says which physical probe it is. See
            # unnamed_probe_error for what the single-debugger exemption does
            # and does not cover.
            if name in probe_addressing_tools() and len(self.config.debuggers) > 1 and self.config.debugger is not None and self.config.debugger.probe_id is None:
                return unnamed_probe_error(name, self.config)
            blocked_before = self.coordinator.blocked
            if blocked_before and name in audited_hardware_tools() and name not in containment_tools():
                try:
                    recovered = self._attempt_machine_recovery()
                except Exception as error:
                    # Recovery is the one path allowed to unblock hardware, so a
                    # fault inside it must never fail open: the incident simply
                    # stays exactly as it was and the operator still owns it.
                    self._poison_quietly("machine_recovery_failed", error, audit_broken=isinstance(error, (ConfigError, OSError)))
                    recovered = None
                # The automatic attempt still runs for every tool — it is the
                # cheapest way out and it costs a caller nothing. What the
                # recovery class is exempt from is the *refusal* when that
                # attempt did not settle the incident: those calls are the
                # remedy, and refusing them was what left an incident with no
                # way out but a person at a shell.
                #
                # `incident_stands` rather than `blocked_before`, and that is the
                # whole of the narrowing on this path: the attempt is unchanged,
                # and only a broken evidence chain still refuses when it did not
                # settle. Anything else stands down at the end of this call and
                # the next one meets the bench with nothing on it.
                if recovered is None and self.coordinator.incident_stands and name not in recovery_class_tools():
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
            if name in audit_gated_tools():
                try:
                    ensure_audit_ready(self.config)
                except (ConfigError, OSError) as error:
                    return audit_unavailable(name, error)
            action = dispatch[name]
            # Run semantics apply to every effectful interaction, whether it is
            # one action or fifty; a declared run is only the explicit spelling.
            # A bare effect call therefore declares its own single-action run
            # here, at the one place every tool passes through, so the wrap is
            # never a thing each tool has to remember to do.
            if name in implicit_run_tools() and not self.coordinator.run_active and not self.coordinator.incident_stands:
                return self._in_implicit_run(name, args, lambda: self._dispatch_tool(name, args, blocked_before, action))
            return self._dispatch_tool(name, args, blocked_before, action)
        return {"ok": False, "tool": name, "error_type": "unknown_tool", "summary": "Unknown Agentic HIL tool."}

    def _in_implicit_run(self, name: str, args: JsonObject, action) -> JsonObject:
        """Perform one bare effect call as the single-action run it really is.

        Before this, an effect tool called with no run open sat outside the run
        model entirely: it took its lease, did its thing, and if it failed into
        an incident there was no teardown to hang a recovery action on — the
        only way back was `hardware_recover` or a person at a shell. A declared
        run around the same call has had that since the abort seam existed.

        So the call declares the run itself, over exactly the resources it is
        about to lease, and the declaration is the same `begin_run` an agent's
        own `bench_run_start` makes: the same hold, the same lease record, the
        same audit trail. The teardown is the same too — literally
        `bench_run_stop`'s, shared rather than copied — so an incident left
        standing aborts into `recover_after_failed_run` exactly as a declared
        run's would.

        Never entered while a run is open (the declared run already holds
        everything, and a second declaration would be refused), and never while
        an incident stands: a new run is the stimulus class and waits for the
        one incident that still gates, while the recovery class must reach the
        board *through* it, and wrapping either in a run it cannot open would
        break both. An incident that is open and does not stand is not one of
        those cases: `begin_run` lets it through, and the teardown below is
        where its recovery action runs.
        """
        try:
            resources = implicit_run_resources(self.config, name, args)
        except (CoordinationError, DeviceError):
            # Nothing resolvable to declare means nothing to hold. The call
            # answers for its own argument exactly as it did before, rather than
            # having this layer restate a refusal it does not own.
            return action()
        try:
            declaration = self.coordinator.begin_run(resources, label=f"{IMPLICIT_RUN_LABEL}{name}")
        except CoordinationError as error:
            return {"tool": name, "side_effect_committed": False, **error.result}
        declared = [item for item in declaration.get("declared_devices") or [] if isinstance(item, str)]
        try:
            result = action()
        except BaseException:
            # The run still ends and still recovers: an exception on the way out
            # is the failure this seam exists for. Its own recovery block has
            # nowhere to go — the exception is the answer — but the bench is
            # given back either way.
            self._recover_if_run_left_an_incident(self._end_implicit_run(), declared)
            raise
        recovery = self._recover_if_run_left_an_incident(self._end_implicit_run(), declared)
        if not isinstance(result, dict):
            return result
        # Said in the result, because "this call ran in a run" and "an agent
        # declared a run around this call" are different facts and an operator
        # reading the report afterwards has to be able to tell them apart.
        run: JsonObject = {
            "implicit": True,
            "declared_devices": declared,
            "run_label": declaration.get("run_label"),
            "run_started_at": declaration.get("run_started_at"),
            "aborted": recovery is not None,
        }
        return {**result, "run": run} if recovery is None else {**result, "run": run, "recovery": recovery}

    def _end_implicit_run(self) -> bool:
        """Close the implicit run, never raising over the call's own answer."""
        try:
            self.coordinator.end_run()
        except CoordinationError:
            # `end_run` is idempotent and this run was opened two statements
            # ago, so this is close to unreachable; if it ever is reached, the
            # bench state below is still read honestly and the call's own result
            # is not replaced by a bookkeeping failure.
            return False
        return True

    def _dispatch_tool(self, name: str, args: JsonObject, blocked_before: bool, action) -> JsonObject:
        try:
            if name in debugger_effect_tools() or name == "debug_stop_session":
                permission_failure = self._debug_permission_failure(name, args)
                if permission_failure is not None:
                    return permission_failure
                return self._settle_after_recovery_class_call(name, blocked_before, self._coordinated_debug_call(name, lambda: self._invoke_dispatch(action)))
            return self._settle_after_recovery_class_call(name, blocked_before, self._invoke_dispatch(action))
        except BaseException as error:
            if name in audited_hardware_tools() or name in containment_tools():
                poison_error = self._poison_quietly("unknown_hardware_exception", error, audit_broken=isinstance(error, (ConfigError, OSError)))
                # Report the quarantine state that actually holds instead of a
                # hardcoded claim: a poison failure must not fake protection.
                # An open incident that owes no gate is not that state, so it is
                # not reported as one; the stand-down at the end of the call is
                # what makes the two agree.
                quarantined_now = self.coordinator.incident_stands
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
                    quarantined_now = self.coordinator.incident_stands or quarantined_now
                return {**written, "cleanup_required": True, "quarantined": quarantined_now, "quarantine_id": self.coordinator.quarantine_id}
            if isinstance(error, ConfigError):
                return {"tool": name, **error.to_dict()}
            raise

    def reload_description(self) -> JsonObject:
        """Take the device description on disk, keeping the permissions in force.

        The swap is one statement in the middle of this method, and everything
        around it is either the decision (in `configreload`, which builds a whole
        new configuration and hands it back rather than touching this one) or the
        handover to the collaborators. Nothing here field-pokes a live config:
        the object every collaborator holds is replaced with the new one, and the
        collaborators that keep state derived from it — an open COM session, a
        cached probe uid, a gdb session — are the ones that decide what of it
        survives.

        It runs on the dispatch path, so it is already under `_lifecycle_lock`
        and inside the same serialization every other call gets. That, plus the
        refusal while anything is held, is what makes "at a defined point" true:
        no call is part-way through reading `self.config` while it changes.
        """
        reloaded, result = reload_description(
            self.config,
            open_holds=self.open_hardware_holds(),
            quarantine=self._unresolved_incident(),
        )
        if reloaded is None:
            return result
        self._swap_config(reloaded)
        return result

    def _unresolved_incident(self) -> JsonObject | None:
        """This bench's open incident, or None when there is none.

        Separate from `open_hardware_holds` because it is a different fact: a
        quarantine can outlive every lease that produced it, and it is then
        standing with nothing held. A description reload is refused on it all
        the same — the operator is about to be asked to physically check the
        devices the incident names, and a name that meant another board by the
        time they read it is the one thing that check cannot survive.

        Only the standing kind refuses. An incident nobody has to check is one
        nobody is about to read a device name off, so there is nothing for a
        rename to invalidate."""
        if not self.coordinator.incident_stands:
            return None
        return {
            "quarantined": True,
            "quarantine_id": self.coordinator.quarantine_id,
            "cleanup_required": True,
            "cleanup_reasons": sorted({reason for lease in self.coordinator.leases.values() for reason in lease.cleanup_reasons()}),
        }

    def _swap_config(self, config: AgenticHILConfig) -> None:
        self.config = config
        self.coordinator.reconfigure(config)
        # `create_debugger_backend` dispatches on the bound probe's type, so a
        # config whose binding or type moved needs a different object and not a
        # different field. The old one is closed first: the reload is refused
        # while anything is held, so there is no session to lose here, and
        # dropping the object without closing it would leak whatever it opened.
        if self._owns_backend and _backend_kind(config) != _backend_kind(getattr(self.backend, "config", None)):
            self.backend.close()
            self.backend = create_debugger_backend(config)
        else:
            self.backend.reconfigure(config)
        self.artifacts.reconfigure(config)
        self.com_ports.reconfigure(config)
        self.can_buses.reconfigure(config)

    def hardware_lease_status(self) -> JsonObject:
        return self.coordinator.status()

    def hardware_recover(self, operator_statement: str | None = None, accept_config_change: bool = False) -> JsonObject:
        """Clear this bench's quarantine, on its own or on a relayed statement.

        Before this tool a quarantine could be seen and explained over MCP and
        cleared nowhere but at a shell, so on a host that has no shell an agent
        watched a bench it could not return to service — including for incidents
        that had provably never touched a board.

        Two things gate it, and they are different in kind. `permissions
        .allow_recover` is authorization: this bench's operator decides whether an
        agent is in the recovery business at all, open by default like every grant
        since 0.8.0 and narrowable to false and not back, through the same
        one-way `project_config_set` as the rest.

        The class boundary is not authorization and no grant reaches it. Safe
        state — that a physical board is still and holds the firmware somebody
        expects — is a claim about the world, and the only entity that can make
        it is a person. That has not changed. What changed is who may carry the
        sentence: an agent in a chat window has a person on the other end of it,
        and telling that person to go find a shell, on a host that may not have
        one, was never protecting anything. It only meant the claim was made out
        of band and the ledger never saw it.

        So the boundary now reads: the agent may not *make* the claim, and may
        relay one. `operator_statement` is a string and not a boolean precisely
        because of that split — a boolean is a flag the caller sets for itself,
        which is why `confirm_safe_state` does not exist here and will not, while
        a sentence about a bench is something the caller has to have been given.
        Nothing here can verify it was; the tool's own description says outright
        that inventing one writes a false ledger record with the agent named as
        actor, and the ledger keeps `operator_statement_via_agent` distinct from
        an operator's own `operator_confirmation` so the two are never read as
        the same evidence afterwards. The operator's command line stays in every
        refusal as the direct route for when there is nobody to ask.

        The transition, when it is allowed, is the same `coordinator.recover`
        the CLI performs — one implementation, one set of marker-consistency
        checks, one ledger — with the actor and the attestation saying which of
        the two ways in was taken.

        `accept_config_change` is the second half of that sameness, and it is a
        boolean for the reason the paragraph above says `confirm_safe_state`
        cannot be one: it attests nothing about a board. It states that the
        difference between two configuration digests, both of which the refusal
        printed, has been looked at. The refusal named this override from the
        day it existed and the schema then refused the argument it named, so the
        one way forward the tool handed an agent dead-ended on the tool itself,
        for every incident older than a configuration edit, including the edit
        that granted `allow_recover` in the first place. It travels into the same
        ledger field the CLI writes, `config_change_accepted`, beside both
        digests.

        Idempotent, and honestly so: a bench with no open incident answers `ok`
        with `was_quarantined: false` rather than failing, the way `bench_run_stop`
        and `com_session_stop` do, so a second call after a successful one is
        free.
        """
        # Read first, refuse second, and deliberately in that order. Reading this
        # bench's state needs no grant anywhere in this project, nothing is
        # cleared before the grant is checked, and a refusal that could not name
        # the incident would send the operator hunting for an id — on a host that
        # has no shell to run `lease-status` in, which is the situation this tool
        # exists for.
        status = self.coordinator.status()
        quarantine_id = status.get("quarantine_id")
        reasons = [reason for reason in status.get("cleanup_reasons", []) if isinstance(reason, str)]
        # Asked before the grant, because `allow_recover` gates the operator
        # route and this is not it: being told there is nothing to clear needs no
        # permission, and refusing the answer would send an agent hunting for a
        # shell to run a command that would do nothing. `incident_stands` comes
        # off the read above, so it is this bench's state and not a guess: an
        # incident that is open and owes no gate is one the next hardware call
        # answers for, and no signature moves it.
        if not status.get("incident_stands"):
            return nothing_standing_result(status)
        if not self.config.permissions.allow_recover:
            return {
                "ok": False,
                "tool": "hardware_recover",
                "error_type": "permission_denied",
                "permission": "permissions.allow_recover",
                "summary": "Clearing a quarantine over MCP is disabled by the authoritative config; the operator recovers this bench from the command line.",
                "quarantine_id": quarantine_id,
                "cleanup_reasons": reasons,
                "operator_command": recovery_operator_command(quarantine_id),
                "side_effect_committed": False,
                "retry_safe": False,
                **remediation_fields("permission_denied", "allow_recover"),
            }
        allowed = self.coordinator.agent_recoverable_reasons()
        # An incident with no named reason is not a reason class this can judge.
        # It goes to the operator with the rest, because "unnamed" is the one
        # thing an automatic clearance must never read as "harmless".
        physical = sorted(set(reasons) - allowed) if reasons else ["unnamed_incident"]
        # No sentence settles a broken audit. The reason names a ledger that
        # could not be written, and clearing it on a statement would put the
        # attestation into the very file whose failure raised the incident —
        # so this one family keeps the operator's own route, as it did before.
        audit_broken = sorted(reason for reason in reasons if "audit_broken" in reason)
        if physical and operator_statement and not audit_broken:
            # The operator answered, and the agent is relaying what they said.
            # This is not the agent attesting anything — it holds no opinion
            # about the board and the ledger does not record one. It records a
            # quoted person, under a label that stays distinct from a signature
            # made at the operator's own command line.
            cleared = self.coordinator.recover(
                safe_state_confirmed=True,
                quarantine_id=quarantine_id if isinstance(quarantine_id, str) else None,
                accept_config_change=accept_config_change,
                actor=RECOVERY_ACTOR_AGENT,
                via="mcp:hardware_recover",
                attestation=ATTESTATION_OPERATOR_VIA_AGENT,
                operator_statement=operator_statement,
            )
            if cleared.get("ok") is not True:
                return {**cleared, "cleanup_reasons": reasons, "operator_command": recovery_operator_command(quarantine_id)}
            return {**cleared, "was_quarantined": True, "cleared_reasons": reasons}
        if physical:
            # `quarantine_guidance` is attached by `call`, so the four facts the
            # signer needs travel with the command they are needed for.
            return {
                "ok": False,
                "tool": "hardware_recover",
                "error_type": RECOVERY_PHYSICAL_CHECK_ERROR,
                "summary": (
                    "This quarantine names a state only a person can speak for, so it needs an operator_statement: "
                    "ask the operator what state the bench is in and call again with their answer."
                    if reasons
                    else "This quarantine names no reason at all, so nothing here can classify it; an unnamed incident is the last "
                    "thing to read as harmless. Ask the operator what state the bench is in and call again with their answer."
                ),
                # The argument, not just the rule. A refusal that only says "a
                # person must do this" sends an agent looking for a shell it may
                # not have; what is actually needed is the person's sentence, and
                # the agent is talking to them.
                "next_step": (
                    "Ask the operator, in chat: is the board powered, still, and holding the firmware you expect? "
                    "Call hardware_recover again with operator_statement set to what they answered, in their words. "
                    "It is written to the recovery ledger verbatim, recorded as their statement relayed by you."
                ),
                "missing_argument": "operator_statement",
                "quarantine_id": quarantine_id,
                "cleanup_reasons": reasons,
                "physical_check_reasons": physical,
                "agent_clearable_reasons": sorted(allowed),
                # Said outright, because a reason this bench's own policy can
                # settle would otherwise read as needing a person: it does not,
                # it needs the predicate run, and the next hardware call runs it.
                "auto_recover_policy": status.get("auto_recover_policy"),
                "auto_recoverable": bool(status.get("auto_recoverable")),
                # The whole line, with this incident's id already in it.
                "operator_command": recovery_operator_command(quarantine_id),
                "cleanup_required": True,
                "quarantined": True,
                "retry_safe": False,
                "side_effect_committed": False,
                **remediation_fields(RECOVERY_PHYSICAL_CHECK_ERROR),
            }
        recovered = self.coordinator.recover(
            safe_state_confirmed=True,
            quarantine_id=quarantine_id if isinstance(quarantine_id, str) else None,
            accept_config_change=accept_config_change,
            actor=RECOVERY_ACTOR_AGENT,
            via="mcp:hardware_recover",
            attestation=ATTESTATION_NO_CONTACT_CLASS,
        )
        if recovered.get("ok") is not True:
            return {**recovered, "cleanup_reasons": reasons, "operator_command": recovery_operator_command(quarantine_id)}
        return {**recovered, "was_quarantined": True, "cleared_reasons": reasons}

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
            # The raw value, not `float()` over it. The mutex is the layer that
            # decides what a wait may be, and it rejects `bool` on purpose —
            # converting first handed it `1.0` for a `wait_s: true` nobody could
            # have meant, and handed it a `ValueError`/`TypeError` for a string
            # or a null that went straight past the handler below as a traceback.
            # The schema gate catches both over MCP; this method is also called
            # directly, and a conversion that only holds while something upstream
            # is watching is not a contract.
            wait_s = validated_wait(payload.get("wait_s", 0.0))
        except ConfigError as error:
            return {"tool": "bench_run_start", "side_effect_committed": False, "side_effect_status": "not_started", "hardware_state": "unchanged", "retry_safe": False, **error.to_dict()}
        try:
            return self.coordinator.begin_run(
                devices,
                label=str(payload["label"]) if payload.get("label") is not None else None,
                wait_s=wait_s,
            )
        except CoordinationError as error:
            return {"tool": "bench_run_start", "side_effect_committed": False, **error.result}

    def bench_run_stop(self) -> JsonObject:
        """Close the run and give the devices back.

        Idempotent: closing a run that is not open is how an agent recovers from
        losing track of its own state, so it answers instead of refusing.

        This is the interactive half of the run teardown, so it carries the same
        recovery seam the reactor's abort path does: an agent that drove its own
        steps and hit a failure ends its run here, and the bench it hands back
        should be one the next run can start on. The trigger is an incident still
        standing after the devices are released — an interactive run has no step
        list to read a verdict off, and an open incident is the same statement in
        the form this call can actually see."""
        declared = [item for item in (self.coordinator.run_status().get("declared_devices") or []) if isinstance(item, str)]
        try:
            stopped = self.coordinator.end_run()
        except CoordinationError as error:
            return {"tool": "bench_run_stop", "side_effect_committed": False, **error.result}
        recovery = self._recover_if_run_left_an_incident(True, declared)
        return stopped if recovery is None else {**stopped, "recovery": recovery}

    def _recover_if_run_left_an_incident(self, ended: bool, declared: list[str]) -> JsonObject | None:
        """The run teardown's trigger, shared by both interactive spellings.

        A declared run and an implicit single-action run end the same way, so
        they ask the same question in the same place rather than each carrying a
        copy of it: is an incident still standing now the devices are back? That
        is the failure signal an interactive run has — there is no step list to
        read a verdict off — and the implicit run inherits it unchanged, which is
        what makes a bare effect call abort and recover exactly as the same call
        does between `bench_run_start` and `bench_run_stop`.

        Returns the run result's `recovery` block, or None when there was nothing
        to recover from."""
        if not ended or not self.coordinator.blocked:
            return None
        return self.recover_after_failed_run(declared)

    def bench_run_status(self) -> JsonObject:
        return self.coordinator.run_status()

    def test_reactor_run(self, payload: JsonObject) -> JsonObject:
        """Run a plan, or start one detached, on the configuration this server holds.

        The whole of what this adds to the command line is the binding: the CLI
        runs the plan against the configuration it discovers from the working
        directory a person is standing in, and this runs it against the one this
        server was started on. Everything after that is the same code: the same
        plan loader, the same workspace check on the path, the same lock
        declaration, the same per-step permissions, the same report. So there is
        no capability here that `agentic-hil test-reactor` did not already have,
        and nothing an agent can reach through this that it could not reach by
        shelling out. The point is that it no longer has to.

        The two refusals the command line prints and exits non-zero on are
        returned rather than raised, and unchanged: a plan that does not load, or
        resolves outside `workspace_root`, and a coordination failure the run
        could not answer for itself.
        """
        # Deferred, and it has to be: `reactorrun` reaches the reactor, which
        # imports this module, so the import cannot stand at the top of this file.
        # Nothing is paid for it at startup either, since a server that is never
        # asked to run a plan never loads the reactor.
        from agentic_hil.reactorrun import run_plan, start_plan_detached

        path = payload.get("test_config_path")
        test_config_path = path if isinstance(path, str) else None
        try:
            if payload.get("detach") is True:
                return start_plan_detached(self.config, test_config_path)
            return run_plan(self.config, test_config_path)
        except ConfigError as error:
            return {"tool": "test_reactor_run", "side_effect_committed": False, **error.to_dict()}
        except CoordinationError as error:
            return {"tool": "test_reactor_run", "side_effect_committed": False, **error.result}

    def test_reactor_status(self, payload: JsonObject) -> JsonObject:
        """What a run handle is doing, or what handles this bench knows."""
        handle = payload.get("run")
        try:
            return run_status(self.config, handle if isinstance(handle, str) else None)
        except ConfigError as error:
            return {"tool": "test_reactor_status", "side_effect_committed": False, **error.to_dict()}

    def test_reactor_stop(self, payload: JsonObject) -> JsonObject:
        """Ask a run to end after the step it is in."""
        try:
            return request_run_stop(self.config, str(payload.get("run", "")))
        except ConfigError as error:
            return {"tool": "test_reactor_stop", "side_effect_committed": False, **error.to_dict()}

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
        # The same line the run teardown and a recovery-class call write, because
        # this is the same event: a recovery action ran, its predicate confirmed,
        # and the incident ended on that evidence. Which of the three asked for
        # it is not what an audit of a quarantine that ended without a person
        # needs to distinguish, and spelling this one differently said the safe
        # state came from a reason class when a reset had established it.
        if not self.coordinator.resolve_retryable_incident(
            reason,
            allowed=allowed,
            via=RECOVERY_ACTION_VIA,
            attestation=ATTESTATION_RECOVERY_ACTION,
        ):
            return None
        self._release_recovered_leases()
        return report

    def recover_after_failed_run(self, devices: list[str] | None = None) -> JsonObject:
        """Put the bench back into a state the next run can start from.

        This is the run teardown's recovery seam. A run that failed has already
        delivered its verdict — the failure is the result, and nothing here
        changes it — so what is left to do is leave the bench usable instead of
        leaving a padlock on it for a person to come and take off. The actions
        are the ones `_attempt_machine_recovery` performs on the acquire path:
        reap this owner's leftover debugger processes, drive the target into a
        halted state where the bench's policy and the probe's grants allow it,
        and re-read the probe to see that it answers.

        It runs whether or not the failure raised an incident, because the two
        cases want the same thing: a step that failed on an assertion leaves a
        board in whatever state the test drove it to, and the next run should not
        inherit it. When there *is* an incident and the actions performed the
        thing it named as unconfirmed, the incident is resolved here too — see
        `RECOVERY_ACTION_REASONS` for which reasons that covers and why the
        `audit_broken` families are not among them.

        Returns the run result's `recovery` block. It never raises: a fault
        inside recovery must not replace the run's own verdict with a crash."""
        block: JsonObject = {"attempted": False, "actions": [], "outcome": "skipped"}
        if devices:
            block["devices"] = sorted(devices)
        policy = self.config.recovery
        block["auto_recover_policy"] = policy.auto_recover
        block["auto_recover_policy_source"] = "config" if policy.auto_recover_explicit else "default"
        withheld = self._recovery_withheld_reason()
        if withheld is not None:
            reason, summary = withheld
            return {**block, "reason_not_attempted": reason, "summary": summary}
        try:
            return {**block, **self._perform_recovery_actions()}
        except Exception as error:
            # Same rule as the acquire path: recovery may unblock hardware, so a
            # fault inside it fails closed. The incident, if there is one, stays.
            self._poison_quietly("run_recovery_failed", error, audit_broken=isinstance(error, (ConfigError, OSError)))
            return {
                **block,
                "attempted": True,
                "outcome": "error",
                "summary": f"Recovery after the failed run raised {type(error).__name__} and the bench was left held.",
            }

    def _recovery_withheld_reason(self) -> tuple[str, str] | None:
        """Why no recovery action will run, or None. Named in the run result.

        A bench that withholds recovery is a bench somebody has to attend to by
        hand, so the result says which setting made that choice rather than
        reporting an attempt that quietly did nothing."""
        policy = self.config.recovery
        if policy.auto_recover == "off":
            return (
                "auto_recover_policy_off",
                "recovery.auto_recover is off, so this bench takes no recovery action after a failed run.",
            )
        if not self.config.probe_allowed():
            return (
                "probe_not_permitted",
                "No configured probe may be read, so nothing here can establish what state the target is in.",
            )
        debugger = self.config.debugger
        if policy.auto_recover == "reset_halt" and (debugger is None or not debugger.permissions.allow_reset):
            return (
                "allow_reset_missing",
                "recovery.auto_recover asks for reset_halt but this probe does not grant allow_reset, so the target "
                "was left in whatever state the failed run put it in.",
            )
        return None

    def _perform_recovery_actions(self) -> JsonObject:
        """Reap, reset into halt, re-read the probe; then settle any incident."""
        actions: list[str] = ["reap_processes"]
        result: JsonObject = {"attempted": True, "actions": actions}
        if cleanup_registered_processes(owner_marker=self.coordinator.owner_marker):
            return {
                **result,
                "outcome": "failed",
                "failed_action": "reap_processes",
                "summary": "Leftover debugger processes from this owner could not be reaped, so no target action followed.",
            }
        reset_halt = self.config.recovery.auto_recover == "reset_halt"
        if reset_halt:
            actions.append("reset_halt")
            reset = self._invoke_dispatch(lambda: self.backend.reset_target("halt"))
            if not overall_success(reset):
                return {
                    **result,
                    "outcome": "failed",
                    "failed_action": "reset_halt",
                    "summary": "The target did not confirm a reset into halt, so the bench stays as the failed run left it.",
                }
        actions.append("probe_target")
        verification = self._invoke_dispatch(self.backend.probe_target)
        if not overall_success(verification) or verification.get("target_detected") is not True:
            return {
                **result,
                "outcome": "failed",
                "failed_action": "probe_target",
                "summary": "The probe did not report a detected target after the recovery action.",
            }
        result["safe_state_predicate"] = "reset_halt" if reset_halt else "readonly_probe"
        return {**result, "outcome": "recovered", **self._settle_incident_after_recovery(reset_halt)}

    def _settle_incident_after_recovery(self, reset_halt: bool) -> JsonObject:
        """Clear the incident the run was standing under, if the actions answered it.

        A read-only re-read only settles what a re-read can settle; a verified
        reset into halt settles everything but the `audit_broken` families. The
        distinction is the same one the acquire path draws, and it is the whole
        reason the wide set is not simply handed to every caller.

        "The incident the run raised" and "the incident the run inherited" are
        the same thing here, deliberately. A dead owner's unconfirmed cleanup is
        exactly what the reset just erased, so an incident this owner adopted
        ends on the same evidence as one its own lease raised; what the previous
        process failed to confirm is not a fact about the board any more."""
        allowed = RECOVERY_ACTION_REASONS if reset_halt else RETRYABLE_CLEANUP_REASONS
        reason = self.coordinator.retryable_incident(allowed)
        if reason is None:
            if not self.coordinator.blocked:
                return {"incident_resolved": False, "incident_open": False}
            return {
                "incident_resolved": False,
                "incident_open": True,
                "incident_summary": "The incident this run raised is not one a recovery action settles; it needs the operator's route.",
                "quarantine_id": self.coordinator.quarantine_id,
            }
        quarantine_id = self.coordinator.quarantine_id
        if not self.coordinator.resolve_retryable_incident(
            reason,
            allowed=allowed,
            via=RECOVERY_ACTION_VIA,
            attestation=ATTESTATION_RECOVERY_ACTION,
        ):
            return {"incident_resolved": False, "incident_open": True, "quarantine_id": quarantine_id}
        self._release_recovered_leases()
        reblocked = self._lease_release_reblocked()
        if reblocked is not None:
            return reblocked
        return {"incident_resolved": True, "incident_open": False, "resolved_reason": reason, "resolved_quarantine_id": quarantine_id}

    def _settle_after_recovery_class_call(self, name: str, blocked_before: bool, result: JsonObject) -> JsonObject:
        """Let a recovery-class call that succeeded clear the incident it ran under.

        The call is the predicate. Nothing else here attests anything: the reset
        that came back confirmed, or the probe that reported a detected target,
        is the same evidence the acquire path's own recovery collects, arrived at
        because the agent asked for it instead of because a gate ran it. So the
        incident ends the same way, with the same ledger line.

        Silent on failure by design — a recovery-class call whose incident could
        not be cleared still returns its own result. The bench stays held and the
        next call says so; swallowing the call's answer to report a bookkeeping
        problem would lose the thing the caller actually asked for."""
        if not blocked_before or name not in recovery_class_tools() or not isinstance(result, dict):
            return result
        allowed = recovery_class_settles(name, result)
        if allowed is None:
            return result
        reason = self.coordinator.retryable_incident(allowed)
        if reason is None:
            return result
        quarantine_id = self.coordinator.quarantine_id
        try:
            resolved = self.coordinator.resolve_retryable_incident(
                reason,
                allowed=allowed,
                via=RECOVERY_ACTION_VIA,
                attestation=ATTESTATION_RECOVERY_ACTION,
            )
        except Exception:
            return result
        if not resolved:
            return result
        self._release_recovered_leases()
        reblocked = self._lease_release_reblocked()
        if reblocked is not None:
            # The reason cleared, but giving the lease back could not be confirmed
            # and a fresh incident re-quarantined the bench. Forcing
            # `cleanup_required`/`quarantined` to false here would tell a caller to
            # continue on a bench `hardware_lease_status` still shows as held.
            return {**result, **reblocked}
        # The envelope was stamped while the incident still stood; saying
        # `cleanup_required` on the call that just ended it would send a caller
        # looking for a quarantine that is no longer there.
        return {
            **result,
            "cleanup_required": False,
            "quarantined": False,
            "incident_resolved": True,
            "resolved_reason": reason,
            "resolved_quarantine_id": quarantine_id,
        }

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

    def _release_recovered_leases(self) -> bool:
        """Drop every lease handle the cleared incident left behind.

        Machine recovery only runs while no lease is active and it reaps this
        owner's debugger processes, so any debug session is definitively gone.
        Keeping its handle would let a later call act as if a live session were
        still attached to the board. Dropping the service's own handle is not
        enough on its own, though: the backend that actually owns the session
        object (`GdbDebugSessions.session`) is told to discard it too, or a
        session left `cleanup_required` with `hardware_state_unconfirmed` set
        stays on file there and keeps refusing `debug_start_session` with
        `session_already_active` even after the coordinator considers the
        incident it raised settled.

        Returns whether every release confirmed. `HardwareLease.release()` fails
        closed: a release that cannot persist its own record re-quarantines the
        bench under a fresh `lease_release_unconfirmed` incident, and a caller
        that stamped `incident_resolved: true` regardless would contradict what
        `hardware_lease_status` then reports."""
        handles: list[HardwareLease] = []
        for lease in (self._debug_lease, self._quarantined_lease):
            if lease is not None and not any(lease is held for held in handles):
                handles.append(lease)
        if self._debug_lease is not None:
            debug_sessions = getattr(self.backend, "_debug", None)
            if debug_sessions is not None:
                debug_sessions.discard_after_recovery()
        self._debug_lease = None
        self._quarantined_lease = None
        if self._debug_artifact is not None:
            self.artifacts.release_stage(self._debug_artifact)
            self._debug_artifact = None
        released = True
        for lease in handles:
            if not lease.release():
                released = False
        return released

    def _lease_release_reblocked(self) -> JsonObject | None:
        """The open-incident envelope when giving the leases back re-quarantined the bench.

        Inspected after `_release_recovered_leases`, and the coordinator's own
        final state is the authority: a release that could not be confirmed fails
        closed, so if the coordinator is blocked again the incident this run
        raised was cleared but the bench is not free — a new
        `lease_release_unconfirmed` incident stands in its place. Reporting
        success here would tell a caller to continue on a quarantined bench, so
        this returns the new open incident's cleanup/quarantine fields instead;
        `None` when the release confirmed and the bench really is clear."""
        if not self.coordinator.blocked:
            return None
        reasons = sorted({reason for lease in self.coordinator.leases.values() for reason in lease.cleanup_reasons()})
        return {
            "incident_resolved": False,
            "incident_open": True,
            "cleanup_required": True,
            "quarantined": True,
            "quarantine_id": self.coordinator.quarantine_id,
            "cleanup_reasons": reasons,
            "incident_summary": "The incident this run raised was cleared, but returning its hardware lease could not be confirmed, so the bench stays quarantined under a new incident.",
        }

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
        if name == "probe_target" and not self.config.probe_allowed():
            return self._invoke_dispatch(self.probe_target)
        if name == "debugger_probes_list" and not self.config.probe_allowed():
            return self._invoke_dispatch(self.debugger_probes_list)
        # A symbol read the backend serves with no session behind it is a
        # standalone probe contact the same way `probe_target` is, so it needs
        # the same gate. A backend that instead answers it through a session
        # (OpenOCD) names it in no `sessionless_debug_tools()` set, and
        # `debug_start_session` below is where that permission was already
        # checked.
        if name in self.backend.sessionless_debug_tools() and not self.config.probe_allowed():
            return self._invoke_dispatch(lambda: getattr(self, name)(args))
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
        # Backend-aware, because a symbol read is a one-shot on the backend that
        # has no session to hold its lease and a session-scoped call on the one
        # that does. Only the symbol reads are ever both, and only the backend
        # knows which it is, so that is the one thing asked and only about those
        # two: ST-Link and pyOCD name them so they take a real one-shot
        # debugger lease, OpenOCD names neither so they keep running on the
        # session lease. Every other debug tool's class is fixed, and a partial
        # double standing in for one of them is never asked to answer this.
        # Asked through `sessionless_debug_reads` rather than of the backend
        # directly, because the test reactor asks the same question of the same
        # place when it decides whether a plan step needs a `debug_start` before
        # it: a lease class and a plan gate that could disagree would run a step
        # outside any lease.
        sessionless_read = name in sessionless_debug_reads(self.backend)
        one_shot = name in debugger_one_shot_tools() or sessionless_read
        starts_session = name == "debug_start_session"
        lease = self._debug_lease
        for_recovery = name in recovery_class_tools() and self.coordinator.blocked
        if one_shot or starts_session:
            # A recovery-class one-shot runs on whichever hold the incident left
            # standing — the lease a quarantined session kept, or the one a
            # quarantined one-shot kept. Both still hold the locks it needs, so
            # asking for a second lease is refused as busy by the very hold the
            # incident left; queueing the remedy behind the incident is what
            # made the incident a padlock in the first place.
            held = next((item for item in (lease, self._quarantined_lease) if item is not None and item.state == "cleanup_required"), None)
            if one_shot and for_recovery and held is not None:
                lease = held
            elif lease is not None:
                if starts_session:
                    result = callback()
                    if self._result_requires_quarantine(result):
                        lease.quarantine("debug_session_start_unconfirmed", audit_broken=result.get("audit_ok") is False)
                    return self._lease_result(result, lease)
                return {"ok": False, "tool": name, "error_type": "resource_busy", "summary": "Debugger resource already has an active owner lease.", "retry_safe": True}
            else:
                try:
                    resources = (DEBUGGER_DISCOVERY_RESOURCE,) if name == "debugger_probes_list" else debugger_effect_resources(self.config)
                    lease = self.coordinator.acquire(*resources, for_recovery=for_recovery)
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
            # A sessionless symbol read is a memory read: `-r` attaches and reads,
            # so a failure the backend proved never reached the target leaves the
            # board unchanged, and one it could not confirm leaves the target's
            # state — not this host's bookkeeping — in question. It settles and
            # quarantines exactly like `probe_target`.
            read_only = name in {"debugger_probes_list", "probe_target"} or sessionless_read
            if requires_quarantine and read_only and self._readonly_failure_is_settled(result):
                # A failed read whose backend named an abort point before the
                # target is a failed call, not an unconfirmed board: the board is
                # exactly as the last effectful call left it. Quarantining here
                # is what turned an unplugged probe into a physical
                # `recover --confirm-safe-state` stop.
                requires_quarantine = False
                result = {**result, "hardware_state": "unchanged"}
                # An explicit retry_safe from the backend (e.g. an ambiguity a
                # retry cannot resolve) survives; only the absent field becomes
                # the reclassification's own answer.
                result.setdefault("retry_safe", True)
            if requires_quarantine:
                if not read_only:
                    unconfirmed = "debugger_result_unconfirmed"
                elif result.get("target_contacted") is False:
                    # The target was never addressed, so its run state is not in
                    # question and only this host's bookkeeping is: a re-read
                    # settles it.
                    unconfirmed = DEBUGGER_READONLY_RESULT_REASON
                else:
                    unconfirmed = DEBUGGER_READONLY_TARGET_STATE_REASON
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

    def _readonly_failure_is_settled(self, result: JsonObject) -> bool:
        """Whether a read-only one-shot's failure result leaves nothing unknown.

        The abort point has to be the backend's claim, never this layer's
        inference from fields the backend did not write. Only the backend knows
        where its own run stopped, and each one says so the same way — with
        ``target_contacted: false`` and ``side_effect_status: not_started``,
        which it writes exactly where it has the evidence: OpenOCD when a
        classification that names a failure to reach the target meets an absent
        init-stage marker, pyOCD and ST-Link when the toolchain's own words say
        no probe was opened or no target answered.

        Absence of those fields is not the opposite claim, and it was read as one
        until this check demanded them. A returned result proves the spawned
        toolchain child was reaped, so nothing will act on the board from here
        on; it proves nothing about what already happened, and a read on this
        bench is not passive — an SWD attach halts the core, and a process killed
        at its deadline never ran the ``shutdown`` in its own command string.
        Exceptions never reach this — a raise proves nothing about the child
        process and keeps quarantining as ``debugger_call_exception``."""
        return (
            result.get("target_contacted") is False
            and result.get("side_effect_status") == "not_started"
            and result.get("audit_ok") is not False
            and result.get("cleanup_required") is not True
            and result.get("quarantined") is not True
            and result.get("side_effect_committed") is not True
        )

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
    # project_config_adopt_hardware and project_config_create are here because
    # they read a probe, not because they write the configuration: both enumerate
    # and connect in HOTPLUG mode, which is the same board contact
    # debugger_probes_list makes. So they are blocked while this bench is
    # quarantined, and an exception inside either quarantines and comes back as
    # the standard structured hardware failure rather than escaping the service.
    # Proving the audit trail writable first is the one thing this set no longer
    # decides on its own; `audit_gated_tools` says why one of them is exempt.
    return {
        "debugger_probes_list", "probe_target", "flash_firmware", "reset_target", "debug_start_session",
        "debug_set_breakpoint", "debug_continue", "debug_symbol_info", "debug_symbol_value", "debug_dump_symbol_ihex",
        "com_session_start", "com_write", "com_read", "can_session_start", "can_send", "can_read",
        PROJECT_CONFIG_ADOPT, PROJECT_CONFIG_CREATE,
    }


def audit_gated_tools() -> set[str]:
    """The audited tools whose audit trail is proven writable before they run.

    Every audited hardware tool but one. `project_config_create` is the way out
    of an unusable `state_root`, and `ensure_audit_ready` writes under exactly
    that root, so gating the two together made the broken thing guard the call
    that replaces it: a bench whose generated `state_root` the enforcer refuses
    answered `audit_unavailable` to every hardware action *and* to the
    regeneration, and the only route left was hand-editing the file the doctrine
    forbids editing by hand (#353).

    What it costs is stated rather than hidden: this one call reads a probe with
    no proof that an audit trail can be written, because the proof it would ask
    for is the thing it exists to restore. It is still refused while a run or a
    session holds the bench, still blocked while this bench is quarantined, still
    leases the probe it reads, and it writes a `state_root` chosen by
    `provisionable_state_root`, whose whole job is that the next write passes.
    """
    return audited_hardware_tools() - {PROJECT_CONFIG_CREATE}


def containment_tools() -> set[str]:
    return {
        "debug_stop_session", "debug_clear_breakpoints", "debug_halt",
        "com_session_stop", "can_session_stop",
    }


# What an implicit single-action run is labelled with. It is a run label like any
# other, so it reaches `bench_run_status` and the owner record on every lease the
# run holds, and a reader there can tell which kind of run held the bench without
# being told separately. The tool name follows it.
IMPLICIT_RUN_LABEL = "implicit:"
# The audited hardware tools that only read the bench. Written out here rather
# than read off the annotation table in contracts.py: nothing in this package
# decides behaviour from those hints — contracts.py says so and a test enforces
# it — so the two are held in step by a test comparing them instead, in
# tests/test_implicit_single_action_run.py. `probe_target` is deliberately not
# among them, and for the reason contracts.py gives at length: it connects, and
# an SWD attach halts a running core, so it is an effect on the target and its
# own failure path treats it as one.
_READ_ONLY_HARDWARE_TOOLS = frozenset({"debugger_probes_list", "com_read", "can_read", "debug_symbol_info", "debug_symbol_value"})
# Session starts are deliberately outside the implicit run. The hold one takes
# outlives the call that opened it — that is what a session is — so a
# single-action run around a session start would end, and release, while the
# session it opened was still running; the release would be a statement that is
# not true.
_SESSION_START_TOOLS = frozenset({"com_session_start", "can_session_start", "debug_start_session"})


def debugger_one_shot_tools() -> set[str]:
    """Debugger tools that take their own lease for one call and give it back.

    Everything else in `debugger_effect_tools` runs through a session somebody
    already opened, on the lease that session holds — except the reads in
    `sessionless_capable_debug_tools`, whose class the backend decides."""
    return {"debugger_probes_list", "probe_target", "flash_firmware", "reset_target"}


def sessionless_capable_debug_tools() -> frozenset[str]:
    """The debug reads that are one-shot on a sessionless backend, session-scoped
    otherwise.

    A memory read is the one typed-debug operation a probe can serve with no GDB
    session behind it, so `debug_symbol_value` and `debug_dump_symbol_ihex` are
    the only tools whose one-shot-vs-session class is not fixed here but asked of
    the backend (`DebuggerBackend.sessionless_debug_tools`). Nothing else is ever
    leased as a one-shot on the strength of that answer, which is why it is the
    only question put to the backend and only for these two."""
    return frozenset({"debug_symbol_value", "debug_dump_symbol_ihex"})


def sessionless_debug_reads(backend: DebuggerBackend) -> frozenset[str]:
    """Which typed-debug reads this backend answers with no debug session open.

    The one place that answers that question, and it answers it from what the
    backend implements rather than from what it is called: the backend names the
    reads it serves standalone (`DebuggerBackend.sessionless_debug_tools`, which
    ST-Link answers with both of them because its `-r` attaches and reads with no
    GDB behind it), and that answer is narrowed to the reads whose class is the
    backend's to decide at all. No backend type is named here or in either
    caller, so a backend that grows the ability later is admitted by this same
    call, with nothing else to change.

    Two callers ask it, and they must not be able to disagree: the coordination
    layer, deciding whether a read takes its own one-shot debugger lease or runs
    on a session's, and the test reactor, deciding whether a plan carrying that
    read needs a `debug_start` step at all. A bench where one said yes and the
    other no would run a plan step outside any lease.

    Narrowed rather than trusted whole because a backend naming something outside
    that pair would be claiming a class this project never gives away: a
    breakpoint, a resume or a session lifecycle is a session operation on every
    backend, and honouring a wider claim would lease one as a standalone probe
    contact and let a plan carry it with no session open."""
    return frozenset(backend.sessionless_debug_tools()) & sessionless_capable_debug_tools()


def configured_sessionless_debug_reads(config: AgenticHILConfig, debugger_id: str | None = None) -> frozenset[str]:
    """The same answer, for a configured probe nothing has built a backend for.

    A caller holding a live backend asks `sessionless_debug_reads` directly. The
    test reactor's preflight holds none and must not acquire one: a plan it
    refuses has opened no service, taken no lock and created no directory. So it
    builds the backend object this configuration names, asks the one place above,
    and drops it again. That is affordable precisely because a backend does its
    work in its calls and not in its constructor: building one spawns no process,
    opens no probe, takes no lock and writes no file, and `close()` on one that
    never started a session only clears the session it does not have."""
    bound = config if debugger_id is None or debugger_id == config.debugger_id else bind_debugger(config, debugger_id)
    backend = create_debugger_backend(bound)
    try:
        return sessionless_debug_reads(backend)
    finally:
        backend.close()


def implicit_run_tools() -> set[str]:
    """Effect tools that a bare call wraps in a run it declares for itself.

    Derived rather than listed, so a tool that joins the incident-marking set
    later joins this one with it instead of being silently left outside the run
    model. The base is `audited_hardware_tools` — precisely the calls whose
    failure can raise an incident, and whose audit trail is proven writable
    before the board is touched — less four groups:

    * the read-only ones. A read leaves nothing for a recovery action to put
      back.
    * the session starts, for the reason given at `_SESSION_START_TOOLS`.
    * the calls made *through* an open debug session. A recovery action reaps
      this owner's debugger processes and drops the debug lease, so a run that
      opened and closed around one call inside a session would take the session
      with it — and a session is precisely the case where the agent's own hold
      already spans several calls. Its teardown is `debug_stop_session` and the
      containment tools beside it, which stay reachable through an incident on
      purpose.
    * the two configuration tools, which refuse while a run holds the bench —
      wrapping either in a run would make it refuse itself.

    A tool in this set is still only wrapped when there is no run open and no
    incident standing; see `_in_implicit_run`."""
    session_scoped_debug = debugger_effect_tools() - debugger_one_shot_tools()
    return audited_hardware_tools() - _READ_ONLY_HARDWARE_TOOLS - _SESSION_START_TOOLS - session_scoped_debug - {
        PROJECT_CONFIG_ADOPT,
        PROJECT_CONFIG_CREATE,
    }


def implicit_run_resources(config: AgenticHILConfig, name: str, args: JsonObject) -> list[object]:
    """What a bare effect call is about to lease, for its own run to declare.

    Exactly the resources the call itself takes, and derived the same way it
    derives them, because a run that declared anything else would be refused by
    its own `_undeclared` check the moment the call reached for a lease. Raises
    the same `DeviceError` / `CoordinationError` the call would raise on an
    argument that names no configured device; the caller lets the call answer
    for that itself."""
    if name in debugger_effect_tools():
        return list(debugger_effect_resources(config))
    if name == "com_write":
        return [uart_device(config, str(args.get("port_id", "")))]
    if name == "can_send":
        return [can_device(config, str(args.get("bus_id", "")))]
    return []


def recovery_class_tools() -> set[str]:
    """The calls that are the remedy for an incident, not a use of the bench.

    An incident is what a failed run leaves behind, and the answer to it is to
    put the target back into a known state. Refusing exactly the calls that do
    that — probe it, reset it, flash a known image onto it — made the incident a
    padlock: the one route out was a person at a command line, for a condition a
    reset settles. So these run *during* an incident.

    The line is what the call is for, not how physical it is: a reset drives the
    target harder than a `com_write` does. The stimulus class — `com_write`,
    `can_send`, session starts, new runs — stays behind the incident, because
    those neither establish a known state nor are meaningful without one, and a
    measurement taken across an unresolved incident is a measurement nobody can
    stand behind afterwards.

    `debug_halt` reaches the same conclusion from the other side and is already
    in `containment_tools`, which the same gate lets through."""
    return {"probe_target", "reset_target", "flash_firmware"}


def recovery_class_settles(name: str, result: JsonObject) -> frozenset[str] | None:
    """Which incident reasons this finished recovery-class call has answered.

    None when it answered none. A probe that reports a detected target has
    re-read the bench and nothing more, so it settles what a re-read settles; a
    confirmed reset into halt, or a flash that completed and reset, has put the
    target into a defined state and settles everything but the `audit_broken`
    families. Same distinction the acquire path draws between its two
    predicates, for the same reason: the evidence differs."""
    # The call's own answer, not `overall_success` of the envelope: a
    # recovery-class call runs on the incident's lease, so the result it comes
    # back in is still stamped `cleanup_required` by the very incident this is
    # deciding whether to clear. Asking the envelope would make the question
    # answer itself, always with no.
    if result.get("ok") is not True or result.get("audit_ok") is False:
        return None
    if name == "probe_target":
        return RETRYABLE_CLEANUP_REASONS if result.get("target_detected") is True else None
    if name == "reset_target":
        return RECOVERY_ACTION_REASONS if result.get("mode") == "halt" else RETRYABLE_CLEANUP_REASONS
    if name == "flash_firmware":
        return RECOVERY_ACTION_REASONS if result.get("reset_after_flash") is not False else RETRYABLE_CLEANUP_REASONS
    return None


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
    """Refuse to drive a board when the bound probe cannot be told apart from
    another configured one.

    This is a naming check, not a hardware one. A call here always drives
    whichever debugger this server is bound to; if that entry has no
    probe_id while other debuggers are configured, nothing says the bound
    name and the physical probe actually agree, which is the gap
    `config.validate_debuggers` closes between *configured* entries and
    cannot close here, because config load must work with no hardware
    attached and cannot demand an id nobody has discovered yet. This refusal
    is what closes it once a board is actually about to be driven, where
    picking the wrong one is the failure that matters. It fires only once
    more than one debugger is configured, because that is the only place the
    naming ambiguity this guards against can exist: a single configured
    debugger has no other entry to be confused with, so every call here can
    only ever mean that one, whether or not it names its probe. Refusing it
    too would not remove any ambiguity; it would only block the common
    single-board bench, including the Nucleo-F446RE + ST-Link + OpenOCD bench
    this project documents as its supported first path, for which
    `debugger_probes_list` answers `not_supported` (OpenOCD cannot
    self-enumerate a serial to write down), and it would give a probe with no
    serial pyOCD or ST-Link can read either (the debugger analogue of the
    CH340-style adapters `com_ports` already has to tolerate) no way to
    satisfy the demand at all.

    What the single-debugger exemption does not cover: whether the one probe
    behind an unnamed debugger is still the physical unit it was last run.
    Nothing here, and nothing at the pyOCD/ST-Link/OpenOCD boundary either,
    pins that without a probe_id: the coordination lock has the identical
    blind spot for the identical reason (see
    devices.DebuggerDevice.identity_warning). An operator who wants that
    guarantee sets probe_id, checked against the attached probe at the pyOCD
    and ST-Link hardware boundary and passed to OpenOCD as the adapter serial
    to open, whether one debugger is configured or several."""
    return {
        "ok": False,
        "tool": tool,
        "error_type": "not_supported",
        "summary": (
            f"Debugger '{config.debugger_id}' has no probe_id, so this call cannot confirm it means "
            f"'{config.debugger_id}' and not one of the project's other configured debuggers. "
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


def prominent_config_status_tools() -> set[str]:
    """The tools that state which configuration decided the answer, always.

    Every other result carries the block only when there is something to report.
    These two are where an operator goes to compare two answers about one bench —
    `debugger_info` against `agentic-hil doctor`, and `project_config_describe`
    for what the file leaves open — so for them "nothing has moved" has to be a
    statement rather than an absence. It holds for their refusals too: a call
    that is refused still answered out of some configuration, and which one it
    was is exactly what the operator is trying to establish."""
    return {"debugger_info", PROJECT_CONFIG_DESCRIBE}


def debugger_effect_tools() -> set[str]:
    return {
        "debugger_probes_list", "probe_target", "flash_firmware", "reset_target", "debug_start_session",
        "debug_set_breakpoint", "debug_continue", "debug_halt", "debug_clear_breakpoints", "debug_symbol_info", "debug_symbol_value", "debug_dump_symbol_ihex",
    }


# ---------------------------------------------------------------------------
# One-time agent provisioning of the project configuration.
#
# The engine is bootstrap.discover_attached_hardware(): probe id, controller and
# the probe's own COM device are precisely the values that are laborious to write
# by hand and impossible to guess, and they are the ONLY values the agent side
# contributes. Everything else is the fixed skeleton. The agent authors no
# content, so it does not decide allow_flash either — the skeleton does, and from
# 0.8.0 it decides it open, so the bench an operator gets back is one they
# can use without opening an editor.
#
# The gate still needs no state outside the configuration. A configuration
# deleted out of band lets an agent generate a fresh one, and the fresh one is
# this same skeleton — what the cycle costs is whatever a person or an agent had
# narrowed, and what it yields is the file `agentic-hil init` writes. The
# invariant is no longer in what the generation withholds; it is in the direction
# `project_config_set` allows, which is `true` to `false` and never back.
PROJECT_CONFIG_CREATE = "project_config_create"
# The fixed profile the generated document is filled from, shared with the CLI's
# own no-profile path so the two write one file for one machine. It is *fixed*
# here in a way it is not there: a profile read out of the workspace would be
# repository-controlled data deciding what the board may be told to do, which is
# exactly the self-authorization this path exists to avoid, so this path never
# looks for one. `agentic-hil init` does, because the file it would find is the
# operator's own.
GENERATED_PROJECT_PROFILE: JsonObject = DEFAULT_PROJECT_PROFILE
GENERATED_CONFIG_HEADER_OPENING = """# Generated by Agentic HIL for an AI agent that found no configuration for this
# workspace, out of what was attached to this machine at the time. No part of it
# was authored by the agent: the hardware facts come from discovery, and the
# permissions come from the fixed skeleton or from the configuration the server
# that wrote this had loaded.
"""
GENERATED_CONFIG_HEADER_CLOSING = """#
# You are the one who narrows it. Tell the agent in prose which permission a bench
# should not have and it writes false here, with a note in `provenance`. That write
# is `project_config_set`, and it puts false into a permission and no other value,
# so nothing an agent sends through it widens this file — closing
# permissions.allow_config_permissions_write is the last permission change it can
# make there at all.
#
# Regenerating is the other door and it is not that ratchet. Both
# `agentic-hil init --force` at your shell and `project_config_create` over MCP,
# while permissions.allow_config_write is true, write this file again from
# attached hardware: entries already in it keep the permissions of the
# configuration the writing server loaded, an entry the discovery finds for the
# first time arrives at the skeleton's defaults, and a file that has been deleted
# comes back at those defaults. That loaded state is the one it parsed at
# startup — a narrowing made over MCP since then is in this file and not in what
# that server holds, so a regeneration before it is restarted writes the older,
# wider value back. Set allow_config_write to false if this bench should not be
# regenerated from the agent side at all.
#
# One combination is worth knowing before you widen anything: validated flashing
# and unrestricted debugger access are mutually exclusive, so while
# allow_raw_debugger_commands or allow_mass_erase is true on a probe,
# flash_firmware on that probe is refused. That is why this file leaves both
# false. Set one true only if something outside these tools drives the probe that
# way, and expect flashing through them to stop while it is.
"""


def generated_default_narrowings(document: JsonObject) -> set[str]:
    """The paths a generation writes false itself, by dotted path.

    The `EXCLUSIVE_FLASH_PERMISSIONS` pair on every debugger entry, in practice —
    read from `generated_permissions` so it stays the same answer as the values
    actually written, for whatever set of sections and flags that covers."""
    expected: set[str] = set()
    for section in GENERATED_WRITE_PERMISSIONS:
        entries = document.get(section)
        if not isinstance(entries, dict):
            continue
        withheld = [flag for flag, granted in generated_permissions(section).items() if not granted]
        for name, entry in entries.items():
            if isinstance(entry, dict):
                expected.update(f"{section}.{name}.permissions.{flag}" for flag in withheld)
    return expected


def narrowed_permissions(document: JsonObject) -> list[str]:
    """Every permission this document withholds *beyond the generated default*.

    Read off the document that is about to be written, which is the only place
    the answer is. A regeneration puts the permissions of the file it replaces
    back on the entries that were already there, so a document leaving this list
    empty and one leaving it long are both ordinary outcomes of the same call —
    and a result that claimed the first while writing the second would be telling
    an operator their narrowed bench had just been reopened.

    "Beyond the generated default" rather than "every false value", because a
    generation writes two of them false itself, and those two are
    not a narrowing: nobody chose them for this bench and there is nothing for an
    operator to restore. Reporting them here would answer the question this list
    exists to answer — did anything survive, or get taken away? — with a standing
    yes on every fresh file. They are stated in the generated header instead, and
    `permissions` in the same result still carries the full true/false surface."""
    by_default = generated_default_narrowings(document)
    return sorted(path for path, granted in permission_surface(document).items() if not granted and path not in by_default)


def _generated_config_header(document: JsonObject) -> str:
    """The header of a generated file, saying what the file below it says.

    Not a fixed sentence. `# every permission below is true` was written on a
    regeneration that had just carried a narrowing over, and a header that
    contradicts the document under it is worse than no header: it is the one part
    a person reads before the keys.

    Two things can put a `false` in the document and they read differently, so
    they are stated apart: the pair a generation writes false on purpose, which is
    fixed text, and a permission carried over from what the writing server had
    loaded, which is listed."""
    carried = narrowed_permissions(document)
    opening = (
        "# Every permission below is true — probing, resetting, flashing, and the serial\n"
        "# and CAN writes of every entry here — except the two that are false so that\n"
        "# flashing works: allow_raw_debugger_commands and allow_mass_erase. Neither has\n"
        "# a tool behind it, and either one being true refuses flash_firmware on that\n"
        "# probe. See the interlock at the end of this header.\n"
    )
    if not carried:
        middle = opening
    else:
        listed = "\n".join(f"#   {path}" for path in carried)
        middle = (
            opening + "#\n"
            f"# {len(carried)} further permission(s) below are false, carried over from the\n"
            "# configuration the writing server had loaded rather than reset to the\n"
            "# skeleton's open default:\n"
            f"{listed}\n"
        )
    return GENERATED_CONFIG_HEADER_OPENING + middle + GENERATED_CONFIG_HEADER_CLOSING


def project_config_create(
    workspace: Path,
    existing: AgenticHILConfig | None,
    *,
    coordinator: HardwareCoordinator | None = None,
    open_holds: JsonObject | None = None,
) -> JsonObject:
    """Generate this workspace's authoritative configuration.

    ``existing`` is the configuration this server is bound to, or None when it
    started without one. It is never taken from a caller's argument: generation
    is for the workspace the server is bound to and for no other, and no
    parameter exists that could name a different one.

    ``coordinator`` and ``open_holds`` are the caller's own, and a configured
    server passes both. Generation reads a board — hardware discovery enumerates
    probes and connects in HOTPLUG mode — so it is a hardware call and is held to
    what every hardware call on this machine is held to: it is refused while this
    server holds a run or a session, and the probe it reads is leased, so a board
    another process owns answers `device_busy` instead of being connected to
    behind its owner's back. A server that has no configuration yet has neither,
    and there is nothing for it to coordinate against: no policy, no state root,
    no lease directory, and no run that could be holding anything."""
    try:
        return _project_config_create(workspace, existing, coordinator=coordinator, open_holds=open_holds)
    except ConfigError as error:
        return {"tool": PROJECT_CONFIG_CREATE, **error.to_dict(), "side_effect_committed": False, "side_effect_status": "not_started", "hardware_state": "unchanged"}


def _project_config_create(
    workspace: Path,
    existing: AgenticHILConfig | None,
    *,
    coordinator: HardwareCoordinator | None,
    open_holds: JsonObject | None,
) -> JsonObject:
    # Before the lock and before discovery. Those holds were taken under the
    # policy this file states, and a regeneration replaces that policy while it
    # also talks to the board the run is holding — one step earlier than a
    # configuration write is refused, for the same reason adoption refuses here.
    if existing is not None and open_holds:
        return _create_in_open_run_refusal(existing, open_holds)
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
        # config_file_not_found it started from, so it refuses instead. Read-only
        # is not the same as free, though: this enumerates probes and connects to
        # one, so on a configured server it goes in under that server's own
        # coordinator.
        discovery, refusal, unaudited = discover_for_generation(current, coordinator)
        if refusal is not None:
            return refusal
        if not overall_success(discovery):
            return {
                **discovery,
                "tool": PROJECT_CONFIG_CREATE,
                "summary": f"{discovery.get('summary', 'Hardware discovery failed.')} No configuration was generated.",
                "workspace_root": str(workspace),
                # A refusal discovery itself produced keeps its own next step. A
                # probe another process is holding aborts the read from inside
                # `before_connect`, and telling that caller to attach a probe
                # would be advice about the one thing that is not the problem.
                "next_step": str(discovery.get("next_step") or "Attach exactly one supported probe and target, then call this tool again. Nothing was written."),
            }
        state_root = provisionable_state_root(workspace)
        document = _generated_document(workspace, state_root, discovery)
        # Grant everything in both cases, so that "a generation decides the
        # permission state completely" holds by construction rather than by
        # reading what filled the skeleton in — a workspace profile must not be
        # what decides a permission on this path either way. A regeneration then
        # puts back exactly the permissions of `current`, so a bench somebody
        # narrowed does not come back open off a call made to refresh a probe id.
        #
        # `current` is this server's own loaded configuration whenever it has one,
        # and deliberately not a fresh read: the file may have been narrowed since
        # startup and the policy this server enforces has not moved with it, so
        # generating against the file would write permissions this server is not
        # running under. The cost is stated rather than fixed — in one session a
        # narrow-then-regenerate puts the loaded grant back, which is accepted
        # because regeneration is creation and the operator's.
        grant_every_permission(document)
        dropped: list[str] = [] if current is None else carry_over_permissions(document, current)
        narrowed = narrowed_permissions(document)
        text = _generated_config_header(document) + yaml.safe_dump(document, sort_keys=False, allow_unicode=False)

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
    # One check, one answer. A server that started without a configuration binds
    # what this call wrote and has nothing to reload; one that already had a
    # configuration is now serving a file that no longer exists in that form, and
    # `reload_required` here has to be the same fact that the staleness block in
    # every following answer reports, or an operator gets "no change" from one
    # mechanism and "restart needed" from the other.
    status = config_status(existing) if existing is not None else None
    result = {
        "ok": True,
        "tool": PROJECT_CONFIG_CREATE,
        # Read off the document that was written, never asserted. A regeneration
        # carries the permissions of the configuration this server loaded onto the
        # entries that were already in it and gives anything newly discovered the
        # skeleton's open default, so "every permission is granted" and "every
        # permission was carried over unchanged" are each true only sometimes, and
        # an operator cannot tell which from a sentence that never varies.
        "summary": _generated_summary(created=created, narrowed=narrowed),
        "created": created,
        "path": written.config_path,
        "workspace_root": written.workspace_root,
        "state_root": written.state_root,
        "optional_override": f"{CONFIG_ENV}={written.config_path}",
        "permissions": permission_summary(written),
        "narrowed_permissions": narrowed,
        "dropped_entries": dropped,
        # A server that started without a configuration binds the one it just
        # wrote. A server that already had one keeps serving what it loaded,
        # because swapping a configuration under open sessions and held device
        # leases is not something a tool call may do behind their back.
        "reload_required": status is not None and bool(status["reload_required"]),
        "hardware_discovery": discovery,
        "side_effect_committed": False,
        "side_effect_status": "not_started",
        "hardware_state": "unchanged",
        "cleanup_required": False,
        "next_steps": _generated_next_steps(written, created=created, narrowed=narrowed),
        **_unaudited_read_note(unaudited),
    }
    return result if status is None else with_config_status(result, status)


def _unaudited_read_note(reason: str | None) -> JsonObject:
    """What a regeneration says about reading the board without an audit trail.

    Only where it happened, and never quietly. The bench this exists for could
    not write a byte under the `state_root` its own configuration named, so the
    read that repaired it is the one read of that bench with no record of it, and
    a result that did not say so would be the same silence the defect was made
    of.
    """
    if reason is None:
        return {}
    return {
        "hardware_read_audited": False,
        "hardware_read_unaudited_reason": reason,
        "hardware_read_note": (
            "The previous configuration's state_root could not be written, so the attached probe was read without a "
            "lease and without an audit record, the way the first generation of a workspace is read. The machine-wide "
            "device locks were still held, so no board another owner holds was connected to. The state_root this call "
            "wrote passes the same check every later write applies, so the next read of this bench is audited again."
        ),
    }


def _generated_summary(*, created: bool, narrowed: list[str]) -> str:
    if created:
        return (
            "Project configuration generated from attached hardware; this server can probe, reset and flash the bench "
            "from it and write to its serial and CAN entries, and can narrow any of those on the operator's request. "
            "The two permissions `flash_firmware` is interlocked against — allow_raw_debugger_commands and "
            "allow_mass_erase — are false, which is what makes flashing work; see `next_steps`."
        )
    if not narrowed:
        return (
            "Project configuration regenerated from attached hardware; every permission in it is granted except the two "
            "that are false so that flashing works. The permissions come from the configuration this server loaded at "
            "startup, so a narrowing made over MCP since then is not among them."
        )
    return (
        f"Project configuration regenerated from attached hardware; {len(narrowed)} permission(s) were carried over as "
        "false from the configuration this server loaded at startup, and every other permission in it is granted "
        "except the two that are false so that flashing works. "
        "`narrowed_permissions` names them; anything this regeneration discovered for the first time is not among them "
        "and starts at the generated defaults, and so is anything narrowed over MCP since this server started."
    )


def _create_in_open_run_refusal(existing: AgenticHILConfig, open_holds: JsonObject) -> JsonObject:
    return {
        "ok": False,
        "tool": PROJECT_CONFIG_CREATE,
        "error_type": "config_write_in_open_run",
        "summary": (
            "This server is holding hardware right now. Regenerating reads the attached probe and replaces the policy "
            "those holds were taken under, so nothing was read and nothing was written."
        ),
        "open_holds": open_holds,
        "path": existing.config_path,
        "workspace_root": existing.workspace_root,
        **remediation_fields("config_write_in_open_run"),
        "side_effect_committed": False,
        "side_effect_status": "not_started",
        "hardware_state": "unchanged",
        "retry_safe": True,
    }


def discover_for_generation(
    current: AgenticHILConfig | None,
    coordinator: HardwareCoordinator | None,
    *,
    tool: str = PROJECT_CONFIG_CREATE,
    reason_prefix: str = "config_create",
    frontend: str = "mcp",
) -> tuple[JsonObject, JsonObject | None, str | None]:
    """Read what is attached, holding everything a probe read holds.

    Public because `agentic-hil init` writes the same file out of the same read
    and so is held to the same lifecycle. ``tool``,
    ``reason_prefix`` and ``frontend`` are the whole of what a caller varies:
    which name the audit record and every refusal carry, which name an incident
    is filed under, and which frontend an owned coordinator announces itself as.
    What is read, what is locked and what happens when any of it fails are not
    a caller's to vary — that was the defect.

    Not a partial copy of the adoption path any more but the same function:
    `discover_under_hardware_lease` proves the audit trail writable before the
    board is touched, locks the enumeration pseudo-resource and the probe the
    enumeration selects, writes the read to the audit trail, checks every release
    and re-commits the state the leases ended in, and quarantines on an
    exception, a failed release or a record it cannot write. A regeneration that
    reported `ok: true` over a board this process is still holding was the one
    thing that could not be allowed to differ between the two callers, so they no
    longer have two implementations to differ in.

    ``resources`` is what this call is about, and for a regeneration that is every
    configured entry that names a physical board — not only the bound one. The
    file being rewritten states the policy for all of them, so none may be under
    another owner's run while it is replaced, and the entry another process holds
    through its `resource_id` is exactly the case a lock on the enumerated serial
    alone would miss.

    A workspace with no configuration at all has none of that machinery — no
    policy to audit against, no `state_root` under which a lease could be
    recorded. It is the first `init` and the first `project_config_create` of a
    workspace, so refusing it outright would leave no way to reach a configured
    one at all; the audit trail and the lease record are what it goes without,
    and the caller says so in its own result rather than letting the read pass
    unremarked. But "this project holds no lease" was never "nobody holds this
    board": another workspace, another server, or an `adopt-hardware` in a
    different project can be mid-HOTPLUG on the same physical ST-Link right now.
    The enumeration pseudo-resource and the probe lock live under the user's
    home, keyed on the device and reachable with no configuration
    (`agentic_hil.bench`), so the bootstrap read still takes them — the host-wide
    enumeration before it enumerates, `probe:<serial>` before it says the first
    word to the selected board — and answers `device_busy` without connecting
    when either is held. Physical-device exclusion is not the part that needed a
    policy.

    ``current`` decides that, and ``coordinator`` never does. An unprovisioned
    server has no coordinator and hands None, and the in-lock reread is exactly
    where it may still find a configuration — the second of two racing servers
    creates none, it loads the one the first just wrote. Treating "no coordinator"
    as "no configuration" made that server read the board directly: unaudited,
    unleased, past the `resource_id` holder the file it had just loaded names, and
    outside the quarantine path. So a configuration without a coordinator gets one
    of its own for the length of the read, and the read goes through the same
    function every other caller uses.

    The third value is why the read went without a lease and an audit record,
    when it did, for the caller to publish. It is filled in for the one case this
    function decides on its own: a configuration that loads and names a
    `state_root` the enforcer refuses, which a regeneration will replace with a
    usable one — `generation_audit_barrier` is where that case is told apart from
    a corrupt report state or any other audit failure a regeneration does not
    repair, which stay refused. A caller that handed None already knows why it did
    and gets None back rather than a reason invented here.
    """
    if current is None:
        # No third answer here: the caller handed the None and already holds a
        # better reason than this function could invent for it.
        return (*_discover_without_policy(tool=tool, frontend=frontend), None)
    unusable = generation_audit_barrier(current)
    if unusable is not None:
        # The configuration loads and its `state_root` is the broken thing that a
        # regeneration replaces, so the machinery this read would go through does
        # not exist any more than it does before the first `init`: no report can
        # be written under that root, and no lease recorded. Gating generation on
        # it made the broken root guard the one call that replaces it, and letting
        # the read go through the leased path instead would touch the board and
        # then quarantine the bench on the audit record it cannot write, which is
        # the same dead end with a probe read spent on it (#353). The barrier
        # returns only for that one repairable case — see `generation_audit_barrier`
        # — so a corrupt report state or any other audit failure a regeneration
        # does not repair reaches the leased path below and its `audit_unavailable`
        # refusal, rather than being read around here (review round 0, finding 1).
        #
        # So this takes the route the first `init` takes, with one lock the first
        # `init` has no way to hold: a first init has no configuration and so no
        # aliases, but this bench does, and a board another owner holds through a
        # configured `resource_id` must not be read out from under it just because
        # the `state_root` that would record the read is broken. The same
        # configured device locks the leased path acquires are taken here beside
        # the host-wide enumeration lock and `probe:<serial>`, and a foreign holder
        # on any of them comes back `device_busy` with nothing said to the board
        # (review round 0, finding 2). The caller is told the read went unaudited
        # rather than being left to infer it.
        aliases = [key for name in current.debuggers for key in configured_probe_resource(current, name)]
        return (*_discover_without_policy(tool=tool, frontend=frontend, resources=aliases), unusable.error_type)
    if coordinator is None:
        owned = HardwareCoordinator(current, frontend=frontend)
        try:
            discovery, refusal = _discover_under_lease(current, owned, tool=tool, reason_prefix=reason_prefix)
        finally:
            # Closing hands back the project lock and leaves any incident this
            # read raised persisted, so the next owner adopts it rather than
            # starting clean. A close that cannot give a lock back marks the
            # coordinator `cleanup_required` and raises; this owner exists only
            # for the length of the read, so that has to become an answer rather
            # than an exception out of an MCP call.
            cleanup_error = _closed_cleanly(owned)
        if refusal is None and cleanup_error is not None:
            return {}, _lock_cleanup_refusal(cleanup_error, tool), None
        return discovery, refusal, None
    return (*_discover_under_lease(current, coordinator, tool=tool, reason_prefix=reason_prefix), None)


def generation_audit_barrier(current: AgenticHILConfig) -> ConfigError | None:
    """The `state_root` path failure a regeneration repairs, or None.

    `ensure_audit_ready` refuses for two unrelated reasons, and only one of them
    is this call's to route around. `unsafe_configured_path` is the `state_root`
    spelling itself being one the enforcer will not accept — it resolves
    elsewhere, leaves the workspace, or cannot be written — and that is exactly
    what a regeneration replaces, because `provisionable_state_root` chooses a
    root that passes the same check. Every other failure is about content under a
    `state_root` that is otherwise fine: a corrupt `report-state.json` is
    `config_invalid`, a full disk or a vanished mount is an `OSError`. A
    regeneration does not touch those, so reading the board around them would
    bypass the audit gate for a failure it does not repair and then report a
    repair that never happened — the next hardware call would meet the very same
    wall (review round 0, finding 1).

    So only the path refusal returns here, and only when the root this workspace
    would regenerate onto is a real one and a *different* one. A regeneration that
    lands back on the same broken spelling repairs nothing, and is left to the
    ordinary `audit_unavailable` refusal — the same answer every other unrepaired
    audit failure gets.
    """
    try:
        ensure_audit_ready(current)
    except OSError:
        return None
    except ConfigError as error:
        if error.error_type != "unsafe_configured_path":
            return None
        return error if _regeneration_moves_state_root(current) else None
    return None


def _regeneration_moves_state_root(current: AgenticHILConfig) -> bool:
    """Whether regenerating this workspace names a usable, different `state_root`.

    `provisionable_state_root` returns the exact root a regeneration writes,
    chosen to pass every check a later write applies, so a value it returns is
    usable by construction. The one thing left to establish is that it *moves*: a
    broken `state_root` whose replacement is the same spelling — a compromised
    subtree under a root that itself resolves cleanly, for one — is not repaired
    by writing the file to name it again, and reading the board around that would
    claim a repair that never lands. When nothing is provisionable at all a
    regeneration repairs nothing either, so that is False too, and the read is
    refused rather than spent.
    """
    try:
        replacement = provisionable_state_root(Path(current.workspace_root))
    except (ConfigError, OSError):
        return False
    return os.path.normcase(str(replacement)) != os.path.normcase(str(current.state_root))


def _discover_without_policy(*, tool: str, frontend: str, resources: list[str] | None = None) -> tuple[JsonObject, JsonObject | None]:
    """Bootstrap discovery with machine-wide physical exclusion but no lease.

    Two callers, and both read the board with no `state_root` to record a lease
    under and no policy to audit against: the first `init`/`project_config_create`
    of a workspace, which has no configuration at all, and a regeneration whose
    configuration loads but names a `state_root` the enforcer refuses, which has
    the same absence of a place to write. What is *not* absent is the risk the
    mutex exists for: the enumeration and the HOTPLUG connect are the same
    hardware read they are under a lease, and the same ST-Link can be held by
    another workspace on this machine. So the device locks are taken directly
    through `BenchMutex` — the host-wide enumeration pseudo-resource around the
    whole read, and `probe:<serial>` in `before_connect`, which is the last point
    before anything is said to the selected board — and a board another owner is
    holding comes back `device_busy` here exactly as it would through
    `discover_under_hardware_lease`, with nothing said to it.

    ``resources`` are the configured lock aliases the caller wants held for the
    read, and they are the whole of what the regeneration caller adds over a first
    `init`. A first init knows no alias, but a configured bench does, and a run
    another owner holds through a debugger's `resource_id` locks
    ``physical:<resource_id>`` — a key no enumeration derives, so the
    `probe:<serial>` locks alone would read the board out from under it. Acquired
    up front beside the enumeration lock and released with everything else, so the
    regeneration honours the same aliases the leased path does even though it has
    no `state_root` under which to record a lease (review round 0, finding 2).

    Returns the discovery and, when a lock refused, the refusal that is the whole
    answer. A refusal from `before_connect` is surfaced as that refusal rather
    than as the discovery result: `discover_attached_hardware` would otherwise
    hand it back as an unsuccessful read, which the caller would write a
    placeholder file from instead of refusing.
    """
    bench = BenchMutex(frontend=frontend)
    try:
        try:
            bench.acquire_named(DEBUGGER_DISCOVERY_RESOURCE)
            if resources:
                bench.acquire(resources)
        except DeviceBusyError as busy:
            return {}, _bootstrap_device_busy(busy.result, tool)

        refused: JsonObject = {}

        def before_connect(selected: str) -> JsonObject | None:
            resource = f"probe:{fold_hardware_id(selected)}"
            try:
                bench.acquire([resource])
            except DeviceBusyError as busy:
                refusal = _bootstrap_device_busy(busy.result, tool)
                refused.update(refusal)
                return refusal
            return None

        discovery = discover_attached_hardware(before_connect=before_connect)
        if refused:
            return {}, refused
        return discovery, None
    finally:
        bench.release_all()


def _bootstrap_device_busy(busy: JsonObject, tool: str) -> JsonObject:
    """A bootstrap read refused because the physical probe is held elsewhere.

    The machine-wide lock answered — another workspace or server is on this
    board — before anything was said to it. `discover_attached_hardware` reads
    the device directly here because there is no configuration to lease against,
    but "no lease" was never "no exclusion": the refusal names the holder and is
    retry-safe, exactly as the leased path's `device_busy` is."""
    return {**busy, "tool": tool, "side_effect_status": "not_started", "hardware_state": "unchanged"}


def _closed_cleanly(coordinator: HardwareCoordinator) -> Exception | None:
    try:
        coordinator.close()
    except Exception as error:  # noqa: BLE001 - reported, never swallowed
        return error
    return None


def _lock_cleanup_refusal(error: Exception, tool: str) -> JsonObject:
    return {
        "ok": False,
        "tool": tool,
        "error_type": "resource_quarantined",
        "summary": (
            "The attached probe was read and the coordination locks taken for that read could not all be given back, so "
            "nothing was written to the configuration."
        ),
        "backend_error": str(error),
        "next_step": "Resolve the incident with `agentic-hil recover` once the bench is known to be in a safe state, then call this again.",
        "cleanup_required": True,
        "quarantined": True,
        **NOT_STARTED,
        "retry_safe": False,
    }


def _discover_under_lease(current: AgenticHILConfig, coordinator: HardwareCoordinator, *, tool: str, reason_prefix: str) -> tuple[JsonObject, JsonObject | None]:
    resources = [key for name in current.debuggers for key in configured_probe_resource(current, name)]
    return discover_under_hardware_lease(
        current,
        coordinator,
        tool=tool,
        reason_prefix=reason_prefix,
        resources=resources,
    )


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


def _generated_next_steps(config: AgenticHILConfig, *, created: bool, narrowed: list[str]) -> list[str]:
    granted = (
        "that every permission in it is granted except the two that are false so that flashing works"
        if not narrowed
        else (
            f"that every permission in it is granted except the two that are false so that flashing works and the "
            f"{len(narrowed)} listed in `narrowed_permissions`, which were carried over from the configuration this "
            "server loaded at startup"
        )
    )
    steps = [
        f"Report where this configuration is, that an agent generated it, and {granted} — read `permissions` for the "
        "whole list. Name allow_mass_erase in particular wherever it is true: it cannot be taken back once it has run.",
        f"Ask the operator which of them this bench should not have. You can write false into any permission in "
        f"{config.config_path} with `project_config_set`; that call writes no other value, so nothing you do through it widens this file.",
    ]
    if created:
        steps.append(
            "Hardware tools read this configuration from now on; `debugger_info` confirms the bench is reachable, and "
            "probing, resetting, flashing and COM and CAN writes work as written."
        )
        steps.append(
            f"Two permissions are false in it on purpose: "
            f"`debuggers.{config.debugger_id or '<name>'}.permissions.allow_raw_debugger_commands` and "
            "`.allow_mass_erase`. Validated flashing and unrestricted debugger access are mutually exclusive policies, "
            "so either one being true refuses `flash_firmware` on that probe. Neither has a tool behind it here, so "
            "nothing is unavailable to you because of them. Do not ask the operator to turn them on to make flashing "
            "work — that is the one change that would stop it."
        )
    else:
        steps.append(
            "This server is still serving the configuration it loaded at startup. Ask the operator to restart the "
            "MCP server before relying on anything this rewrite changed — and note that the permissions just written "
            "came from that same loaded state, so any permission narrowed with `project_config_set` since this server "
            "started is granted again in the file now on disk."
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
            f"Call `{PROJECT_CONFIG_CREATE}`. It takes no arguments and writes a configuration from what is attached to "
            "this machine, with every permission granted but the two that refuse flashing while they are true, so the "
            "bench is workable straight away, flashing included. Report what it granted and ask the operator which "
            "permissions to take back."
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
        # Once a configuration exists this object is a forwarder and nothing
        # else. Every call goes through the bound service, including
        # `project_config_create`: that service is what attaches the staleness
        # block to a result, and the sequence this class exists for — start with
        # no configuration, create one, let the operator edit it, call create
        # again — ends in exactly the refusal that would otherwise be returned
        # around it. A `permission_denied` from an edited file, with nothing to
        # say the file had been edited, is the silence this whole block reports.
        if service is not None:
            return service.call(name, arguments)
        if name == PROJECT_CONFIG_CREATE:
            validation_error = validate_tool_arguments(name, arguments if isinstance(arguments, dict) else {})
            if validation_error is not None:
                return validation_error
            # The genuinely unprovisioned case, and the only one that bootstraps:
            # there is no configuration to be gated by, so the gate is the path
            # re-read under the lock inside `project_config_create`.
            result = project_config_create(self.workspace, None)
            if overall_success(result):
                # Bind what this call just wrote, so the tools it unblocks are
                # usable from the session that unblocked them.
                self._bind()
            return result
        if name not in MCP_TOOL_NAMES:
            return {"ok": False, "tool": name, "error_type": "unknown_tool", "summary": "Unknown Agentic HIL tool."}
        return unprovisioned_tool_error(name, self.workspace)

    def close(self) -> None:
        with self._lock:
            if self._service is not None:
                self._service.close()
