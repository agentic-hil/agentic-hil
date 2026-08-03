from __future__ import annotations

import hashlib
import json
import os
import secrets
import threading
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

from agentic_hil.bench import (
    BenchMutex,
    DeviceBusyError,
    _LifetimeLock,
    is_physical_resource,
    physical_resources,
    utc_now_iso,
)
from agentic_hil.config import (
    ConfigError,
    atomic_write_text,
    safe_append_text,
    safe_read_bytes,
    safe_read_text,
    trusted_state_directory,
)
from agentic_hil.devices import (
    CanDevice,
    DebuggerDevice,
    Device,
    DeviceSet,
    UartDevice,
    lock_keys,
)
from agentic_hil.redact import filesystem_error_detail, redact_sensitive
from agentic_hil.types import AgenticHILConfig, JsonObject

LEASE_VERSION = 2
DEBUGGER_DISCOVERY_RESOURCE = "debugger-discovery:all"
# Absolute paths under the environment-derived state_root / config location are
# kept in the on-disk record for incident matching, but never emitted to an
# operator or MCP sink.
_RECORD_OUTPUT_HIDDEN_KEYS = ("config_path", "workspace")
LEASE_RELEASE_RETRY_REASON = "lease_release_unconfirmed"
# A one-shot that only ever reads (probe discovery, probe_target) intends no
# physical effect, so an unconfirmed result of one leaves the board where it was
# and a read-only re-read can settle it. flash_firmware and reset_target keep the
# undifferentiated debugger_result_unconfirmed: either may have left the target
# running, and nothing on the host can tell that from the outside.
DEBUGGER_READONLY_RESULT_REASON = "debugger_readonly_result_unconfirmed"
RETRYABLE_CLEANUP_REASONS = frozenset(
    {
        "com_buffer_clear_unconfirmed",
        "debug_backend_cleanup_exception",
        "debug_breakpoint_cleanup_unconfirmed",
        "debug_session_cleanup_unconfirmed",
        "debug_session_result_unconfirmed",
        "debug_target_state_unconfirmed",
        DEBUGGER_READONLY_RESULT_REASON,
        LEASE_RELEASE_RETRY_REASON,
    }
)
# Reasons a verified reset-into-halt settles but a read-only re-read cannot: the
# target may be running after an unconfirmed flash, reset, or session start, and
# only driving it into a defined state answers that. Reaching for this set is a
# physical act, so it is gated on the bench's recovery.auto_recover policy.
RESET_RECOVERABLE_CLEANUP_REASONS = RETRYABLE_CLEANUP_REASONS | frozenset(
    {
        "debug_session_start_unconfirmed",
        "debugger_result_unconfirmed",
    }
)
def _public_record(record: JsonObject | None) -> JsonObject | None:
    if not isinstance(record, dict):
        return record
    redacted = redact_sensitive(record)
    assert isinstance(redacted, dict)
    return {key: value for key, value in redacted.items() if key not in _RECORD_OUTPUT_HIDDEN_KEYS}


def _record_cleanup_reasons(record: JsonObject | None) -> list[str]:
    """Every distinct cleanup reason an incident record carries.

    An adopted incident keeps one top-level ``reason``; a quarantine this owner
    raised keeps one per lease. Both have to reach the operator, because the
    reason is the only field that separates a retryable toolchain fault from an
    unconfirmed physical effect that needs a bench inspection."""
    if not isinstance(record, dict):
        return []
    reasons: list[str] = []
    top_level = record.get("reason")
    if isinstance(top_level, str) and top_level:
        reasons.append(top_level)
    leases = record.get("leases")
    for lease in leases if isinstance(leases, list) else []:
        if not isinstance(lease, dict):
            continue
        lease_reasons = lease.get("cleanup_reasons")
        for reason in lease_reasons if isinstance(lease_reasons, list) else []:
            if isinstance(reason, str) and reason and reason not in reasons:
                reasons.append(reason)
    return reasons


class CoordinationError(RuntimeError):
    def __init__(self, result: JsonObject):
        super().__init__(str(result.get("summary", "Hardware resource coordination failed.")))
        self.result = result


class HardwareLease:
    def __init__(self, coordinator: HardwareCoordinator, lease_id: str, resources: list[str], locks: list[_LifetimeLock], bench_resources: list[str] | None = None):
        self.coordinator = coordinator
        self.lease_id = lease_id
        self.resources = resources
        self.locks = locks
        # Devices whose machine-wide hold this lease took itself, and must give
        # back. Inside a declared run the hold belongs to the run, so this is
        # empty and the run outlives every call it contains.
        self.bench_resources = list(bench_resources or [])
        self.state = "active"
        self.safe_state_confirmed = False
        self.processes_reaped = False
        self.audit_ok = True
        self.errors: list[JsonObject] = []
        self.quarantine_id: str | None = None
        self.valid = True

    def release(self, *, safe_state_confirmed: bool = True, processes_reaped: bool = True, audit_ok: bool = True) -> bool:
        return self.coordinator.release_lease(
            self,
            safe_state_confirmed=safe_state_confirmed,
            processes_reaped=processes_reaped,
            audit_ok=audit_ok,
        )

    def quarantine(self, reason: str, error: object | None = None, *, audit_broken: bool = False) -> None:
        self.coordinator.quarantine_lease(self, reason, error, audit_broken=audit_broken)

    def resolve_retryable_cleanup(self, reason: str) -> bool:
        return self.coordinator.resolve_retryable_cleanup(self, reason)

    def cleanup_reasons(self) -> list[str]:
        """Distinct reasons this lease is holding cleanup state, in the order they occurred."""
        reasons: list[str] = []
        for error in self.errors:
            reason = error.get("reason")
            if isinstance(reason, str) and reason and reason not in reasons:
                reasons.append(reason)
        return reasons

    def status(self) -> JsonObject:
        return {
            "lease_id": self.lease_id,
            "resources": list(self.resources),
            "lease_state": self.state,
            "safe_state_confirmed": self.safe_state_confirmed,
            "processes_reaped": self.processes_reaped,
            "audit_ok": self.audit_ok,
            "cleanup_required": self.state in {"cleanup_required", "quarantined"},
            "quarantined": self.state in {"cleanup_required", "quarantined"},
            # The reason decides whether a caller may retry or an operator has to
            # inspect the bench, so it travels with the lease into the project
            # record and every tool result instead of only into the marker file.
            "cleanup_reasons": self.cleanup_reasons(),
            "quarantine_id": self.quarantine_id,
        }


class DetachedHardwareLease:
    """In-memory lease used only by isolated low-level session tests."""

    def __init__(self) -> None:
        self.state = "active"

    def release(self, **_: object) -> bool:
        self.state = "released"
        return True

    def quarantine(self, reason: str, error: object | None = None, *, audit_broken: bool = False) -> None:
        self.state = "cleanup_required"

    def resolve_retryable_cleanup(self, reason: str) -> bool:
        return False

    def status(self) -> JsonObject:
        return {
            "lease_state": self.state,
            "cleanup_required": self.state == "cleanup_required",
            "quarantined": self.state == "cleanup_required",
        }


class HardwareCoordinator:
    def __init__(self, config: AgenticHILConfig, frontend: str = "python"):
        self.config = config
        self.frontend = frontend
        self.owner_marker = secrets.token_hex(32)
        self.owner_started_at = utc_now_iso()
        self.config_sha256 = hashlib.sha256(safe_read_bytes(config.config_path)).hexdigest()
        self._state_directories: dict[tuple[str, ...], Path] = {}
        self.project_key = project_resource(config)
        self.project_lock: _LifetimeLock | None = None
        # Exclusivity is machine-wide and keyed on the physical device, so it
        # cannot live beside the per-configuration state under state_root; see
        # agentic_hil.bench for where it lives and why.
        self.bench = BenchMutex(frontend=frontend)
        # None while no run is open. A run declares its devices up front, which
        # is what lets the mutex know what to lock and what makes a touch of an
        # undeclared device a refusal rather than a silent widening.
        self.declared_resources: frozenset[str] | None = None
        # The Device objects behind those keys, when the caller declared devices
        # rather than bare resource names. Only for reporting: the lock is on the
        # key, and two entries naming one unit share it.
        self.declared_devices: DeviceSet = DeviceSet()
        self.run_label: str | None = None
        self.run_started_at: str | None = None
        self.leases: dict[str, HardwareLease] = {}
        self.blocked = False
        self.quarantine_id: str | None = None
        self.incident_resources: set[str] = set()
        self._state = "open"
        self._guard = threading.RLock()

    def _state_directory(self, *parts: str) -> Path:
        """Create coordination state only once hardware coordination is used.

        Answering ``initialize`` or ``tools/list`` must not require a writable
        state root, so the trusted directory chain is built on first use.
        """
        directory = self._state_directories.get(parts)
        if directory is None:
            directory = trusted_state_directory(self.config.state_root, *parts)
            self._state_directories[parts] = directory
        return directory

    @property
    def root(self) -> Path:
        return self._state_directory("coordination")

    @property
    def lock_directory(self) -> Path:
        return self._state_directory("coordination", "locks")

    @property
    def record_directory(self) -> Path:
        return self._state_directory("coordination", "records")

    @property
    def run_active(self) -> bool:
        return self.declared_resources is not None

    def begin_run(self, resources: Sequence[Device | str] | DeviceSet, *, label: str | None = None, wait_s: float = 0.0) -> JsonObject:
        """Lock every declared device for the whole run.

        The lease taken per hardware call is not exclusivity: it is released the
        moment the call ends, so a second session slipping in between two steps
        of a run met no lock at all. The run holds its devices from here until
        end_run, and every call inside it borrows that hold.

        Accepts Device objects or already-derived resource names. Devices are the
        intended form — they carry the hardware identity and collapse two config
        entries naming one unit onto one lock before anything is taken."""
        with self._guard:
            self._require_open()
            if self.run_active:
                raise CoordinationError({"ok": False, "error_type": "run_already_active", "summary": "A device run is already open on this owner; close it before declaring another.", "declared_devices": sorted(self.declared_resources or ()), "run_label": self.run_label, "run_started_at": self.run_started_at, "retry_safe": False, "side_effect_committed": False})
            device_set = resources if isinstance(resources, DeviceSet) else DeviceSet.of([item for item in resources if isinstance(item, Device)])
            declared = physical_resources(lock_keys(resources if not isinstance(resources, DeviceSet) else resources.devices))
            if not declared:
                raise CoordinationError({"ok": False, "error_type": "invalid_argument", "summary": "A run must declare at least one physical device; the declaration is what the mutex locks and what every later call is checked against.", "retry_safe": False, "side_effect_committed": False})
            try:
                # One call for the whole set: sorted order and the unwind of a
                # partial acquisition both live in BenchMutex.acquire, so a run
                # that fails on its third device has already given back its first
                # two by the time this raises.
                self.bench.acquire(declared, wait_s=wait_s)
            except DeviceBusyError as error:
                raise CoordinationError({**error.result, "declared_devices": declared}) from error
            except ConfigError as error:
                raise CoordinationError({"ok": False, "error_type": error.error_type, "summary": error.summary, **error.details}) from error
            self.declared_resources = frozenset(declared)
            self.declared_devices = device_set
            self.run_label = label
            self.run_started_at = utc_now_iso()
            self.bench.owner = replace(self.bench.owner, label=label)
            self.bench.heartbeat()
            reclaimed = [detail for detail in (self.bench.reclaimed(resource) for resource in declared) if detail is not None]
            result: JsonObject = {
                "ok": True,
                "tool": "bench_run_start",
                "declared_devices": declared,
                "run_label": label,
                "run_started_at": self.run_started_at,
                "owner": self.bench.owner.as_json(),
                "summary": f"{len(declared)} device(s) are held for this run and released when it ends.",
            }
            if device_set:
                result["devices"] = device_set.as_json()
                warnings = device_set.warnings()
                if warnings:
                    result["warnings"] = warnings
            if reclaimed:
                result["reclaimed"] = reclaimed
            return result

    def end_run(self) -> JsonObject:
        with self._guard:
            declared = sorted(self.declared_resources or ())
            was_active = self.run_active
            self.declared_resources = None
            self.declared_devices = DeviceSet()
            self.run_label = None
            self.run_started_at = None
            self.bench.owner = replace(self.bench.owner, label=None)
            if declared:
                self.bench.release(declared)
            # A lease still open — a COM or CAN session the run left running —
            # keeps its own hold on the device, so ending the run here does not
            # pull a board out from under a live session. Say so rather than
            # letting the caller infer a release that did not happen.
            open_leases = sorted(lease.lease_id for lease in self.leases.values())
            result: JsonObject = {"ok": True, "tool": "bench_run_stop", "released_devices": declared, "run_was_active": was_active, "summary": f"{len(declared)} device(s) were released."}
            if open_leases:
                result["open_leases"] = open_leases
                result["still_held_devices"] = sorted(self.bench.held_resources())
                result["summary"] = f"The run ended, but {len(open_leases)} lease(s) are still open and keep their devices held; stop those sessions to free them."
            return result

    def run_status(self) -> JsonObject:
        """What this owner is holding for a run right now.

        An agent that lost track of its own run reads this instead of guessing:
        a run holds its devices until it is closed or the owning process ends,
        and nothing times it out — dropping a board that may be mid-operation is
        the silent failure the mutex exists to prevent."""
        with self._guard:
            declared = sorted(self.declared_resources or ())
            result: JsonObject = {
                "ok": True,
                "tool": "bench_run_status",
                "run_active": self.run_active,
                "declared_devices": declared,
                "run_label": self.run_label,
                "run_started_at": self.run_started_at,
                "owner": self.bench.owner.as_json(),
                "held_devices": sorted(self.bench.held_resources()),
                "summary": (
                    f"A run holds {len(declared)} device(s); they stay held until bench_run_stop or until this process ends."
                    if self.run_active
                    else "No run is open; each hardware call takes and releases its own device."
                ),
            }
            if self.declared_devices:
                result["devices"] = self.declared_devices.as_json()
            return result

    def _undeclared(self, resources: list[str]) -> list[str]:
        """Physical devices this run may not touch.

        A run that reaches past its own declaration is refused rather than
        widened: the declaration is the honest record of what a test touches,
        and the mutex could not have locked what it was never told about."""
        declared = self.declared_resources
        if declared is None:
            return []
        return [resource for resource in resources if is_physical_resource(resource) and resource not in declared]

    def acquire(self, *resources: Device | str) -> HardwareLease:
        """Take a lease on the named devices for the length of one call.

        Backends hand devices, not derived names: the lock reference belongs in
        one place, and a backend that spelled its own key could disagree with
        the run that declared it."""
        normalized = sorted(set(resource for resource in lock_keys(resources) if resource))
        if not normalized:
            raise ValueError("At least one physical resource is required.")
        with self._guard:
            self._require_open()
            undeclared = self._undeclared(normalized)
            if undeclared:
                raise CoordinationError(
                    {
                        "ok": False,
                        "error_type": "undeclared_device",
                        "summary": "This run did not declare the device it is reaching for, so the access is refused; add it to the test description and rerun.",
                        "undeclared_devices": undeclared,
                        "declared_devices": sorted(self.declared_resources or ()),
                        "run_label": self.run_label,
                        "retry_safe": False,
                        "side_effect_committed": False,
                    }
                )
            if self.blocked:
                raise CoordinationError(self._quarantined_result(normalized, "Current owner has unresolved cleanup or audit state."))
            acquired_project = False
            if self.project_lock is None:
                self.project_lock = self._acquire_lock(self.project_key, normalized)
                acquired_project = True
                try:
                    stale = self._read_record(self.project_key)
                except BaseException:
                    self.project_lock.release()
                    self.project_lock = None
                    raise
                if stale is not None and stale.get("state") not in {None, "released"}:
                    stale_resources = [item for item in stale.get("resources", []) if isinstance(item, str)] or normalized
                    self._adopt_incident(stale, stale_resources)
                    stale = {**stale, "version": LEASE_VERSION, "state": "quarantined", "quarantined_at": utc_now_iso(), "reason": "owner_process_exited_without_release", "quarantine_id": self.quarantine_id}
                    self._write_record(self.project_key, stale)
                    self._mark_incident_resources(stale_resources, "owner_process_exited_without_release", expected_project=self.project_key)
                    self.blocked = True
                    self.project_lock.release()
                    self.project_lock = None
                    raise CoordinationError(self._quarantined_result(normalized, "Previous owner exited without confirmed cleanup."))
            locks: list[_LifetimeLock] = []
            bench_taken: list[str] = []
            try:
                for resource in normalized:
                    lock = self._acquire_lock(resource, normalized)
                    locks.append(lock)
                    stale = self._read_record(resource)
                    if stale is not None and stale.get("state") not in {None, "released"}:
                        if not self._record_matches_project(stale):
                            raise CoordinationError({"ok": False, "error_type": "resource_quarantined", "summary": "Physical resource belongs to another unresolved project incident.", "resource": resource, "cleanup_required": True, "quarantined": True, "retry_safe": False, "quarantine_id": stale.get("quarantine_id")})
                        stale_resources = [item for item in stale.get("resources", []) if isinstance(item, str)] or [resource]
                        self._adopt_incident(stale, stale_resources)
                        self.blocked = True
                        self._persist_project("cleanup_required", stale_resources)
                        for held in reversed(locks):
                            held.release()
                        locks.clear()
                        self._mark_incident_resources(stale_resources, "owner_process_exited_without_release", expected_project=self.project_key)
                        raise CoordinationError(self._quarantined_result(normalized, "Physical resource requires explicit safe-state recovery."))
                # The project's own lock only keeps this configuration honest. A
                # device is shared with every other configuration on the machine,
                # so exclusivity is taken here too — inside a run this borrows the
                # hold the run already has, outside one it lasts for the call.
                try:
                    self.bench.acquire(normalized)
                except DeviceBusyError as error:
                    raise CoordinationError({**error.result, "resources": normalized}) from error
                # Every physical device this lease asked for is counted, held or
                # borrowed, so the release below is the exact mirror of the
                # acquire and a run's own hold survives the calls inside it.
                bench_taken = physical_resources(normalized)
                self.bench.heartbeat()
                lease = HardwareLease(self, secrets.token_hex(16), normalized, locks, bench_resources=bench_taken)
                self.leases[lease.lease_id] = lease
                self._persist_lease(lease)
                self._persist_project("active")
                return lease
            except BaseException:
                if bench_taken:
                    self.bench.release(bench_taken)
                for lock in reversed(locks):
                    lock.release()
                if acquired_project and not self.leases and self.project_lock is not None:
                    state = "cleanup_required" if self.blocked else "released"
                    self._persist_project(state, sorted(self.incident_resources) if self.blocked else normalized)
                    self.project_lock.release()
                    self.project_lock = None
                raise

    def release_lease(
        self,
        lease: HardwareLease,
        *,
        safe_state_confirmed: bool,
        processes_reaped: bool,
        audit_ok: bool,
    ) -> bool:
        with self._guard:
            if not self._valid_lease(lease):
                lease.valid = False
                lease.state = "stale"
                return False
            if lease.state != "active" and not self._release_retryable(lease):
                return False
            lease.safe_state_confirmed = safe_state_confirmed
            lease.processes_reaped = processes_reaped
            lease.audit_ok = lease.audit_ok and audit_ok
            if not safe_state_confirmed or not processes_reaped or not lease.audit_ok:
                reason = "safe_state_unconfirmed" if not safe_state_confirmed else "process_reap_unconfirmed" if not processes_reaped else "audit_broken"
                self._quarantine_registered_lease(lease, reason, audit_broken=not lease.audit_ok)
                return False
            # The lease stays registered, valid, and blocking until every durable
            # commit and every lock release is confirmed; any fault below turns it
            # into a retryable local incident instead of a silent fail-open.
            lease.state = "releasing"
            remaining = {lease_id: item for lease_id, item in self.leases.items() if lease_id != lease.lease_id}
            # Resources of THIS lease leave the incident on a clean release; an
            # adopted lease-less incident (residual) must keep the project blocked
            # so a successful unrelated release cannot erase it.
            residual_incident = self.incident_resources - set(lease.resources)
            try:
                self._persist_lease(lease, state="released")
                blocked_after = any(item.state in {"cleanup_required", "quarantined"} for item in remaining.values()) or bool(residual_incident)
                if blocked_after:
                    incident_after = sorted(residual_incident | {resource for item in remaining.values() for resource in item.resources})
                    self._persist_project("cleanup_required", incident_after, leases=remaining)
                    # Re-normalize sibling quarantined markers down to the shrunken
                    # incident set; otherwise a marker still listing the released
                    # resource fails recover()'s marker ⊆ incident check forever
                    # after an unclean owner death.
                    for item in remaining.values():
                        if item.state in {"cleanup_required", "quarantined"}:
                            self._persist_lease(item, incident_override=incident_after)
                elif remaining:
                    self._persist_project("active", leases=remaining)
                else:
                    self._persist_project("released", list(lease.resources), leases=remaining)
                while lease.locks:
                    lease.locks[-1].release()
                    lease.locks.pop()
                if not remaining and not residual_incident and self.project_lock is not None:
                    self.project_lock.release()
                    self.project_lock = None
            except BaseException as error:
                lease.locks = [lock for lock in lease.locks if lock.descriptor >= 0]
                try:
                    self._quarantine_registered_lease(lease, LEASE_RELEASE_RETRY_REASON, error)
                except BaseException as persist_error:
                    lease.errors.append({"reason": LEASE_RELEASE_RETRY_REASON, "time": utc_now_iso(), "error_type": type(persist_error).__name__, "summary": str(persist_error), "quarantine_id": lease.quarantine_id})
                if isinstance(error, (KeyboardInterrupt, SystemExit)):
                    # Fail-closed AND honor the interrupt: the lease is quarantined
                    # and retryable, but the interrupt still propagates.
                    raise
                return False
            lease.state = "released"
            lease.errors.clear()
            lease.quarantine_id = None
            self.leases.pop(lease.lease_id, None)
            lease.valid = False
            # Only now, with every state lock confirmed released, does the device
            # leave this owner's machine-wide hold. Inside a declared run the
            # count stays above zero and the run keeps the board.
            self.bench.release(lease.bench_resources)
            lease.bench_resources = []
            self.incident_resources.difference_update(lease.resources)
            self.blocked = any(item.state in {"cleanup_required", "quarantined"} for item in self.leases.values()) or bool(self.incident_resources)
            if not self.blocked:
                self.quarantine_id = None
                self.incident_resources.clear()
            return True

    def _release_retryable(self, lease: HardwareLease) -> bool:
        return (
            lease.state == "cleanup_required"
            and lease.audit_ok
            and bool(lease.errors)
            and all(error.get("reason") == LEASE_RELEASE_RETRY_REASON for error in lease.errors)
        )

    def quarantine_lease(self, lease: HardwareLease, reason: str, error: object | None = None, *, audit_broken: bool = False) -> None:
        with self._guard:
            if not self._valid_lease(lease):
                lease.valid = False
                lease.state = "stale"
                return
            self._quarantine_registered_lease(lease, reason, error, audit_broken=audit_broken)

    def resolve_retryable_cleanup(self, lease: HardwareLease, reason: str, *, allowed: frozenset[str] | None = None) -> bool:
        with self._guard:
            if not self._valid_lease(lease):
                lease.valid = False
                lease.state = "stale"
                return False
            # Callers that only re-read hardware stay on the default set; the
            # wider one is reachable only through a caller that has driven the
            # target into a defined state first.
            if reason not in (RETRYABLE_CLEANUP_REASONS if allowed is None else allowed):
                return False
            if lease.state != "cleanup_required" or not lease.audit_ok or any(error.get("reason") != reason for error in lease.errors):
                return False
            lease.state = "active"
            lease.errors.clear()
            lease.quarantine_id = None
            self.blocked = any(item.state in {"cleanup_required", "quarantined"} for item in self.leases.values())
            if not self.blocked:
                self.quarantine_id = None
                self.incident_resources.clear()
            self._persist_lease(lease)
            self._persist_project("cleanup_required" if self.blocked else "active")
            return True

    def recoverable_reasons(self) -> frozenset[str]:
        """The reasons this bench's policy and grants actually let the owner clear.

        ``reset_halt`` degrades to the read-only set without ``allow_reset``: the
        config may permit the stronger predicate while the bound probe does not
        authorize the reset it needs."""
        policy = self.config.recovery.auto_recover
        if policy == "off":
            return frozenset()
        debugger = self.config.debugger
        if policy == "reset_halt" and (debugger is None or debugger.permissions.allow_reset):
            return RESET_RECOVERABLE_CLEANUP_REASONS
        return RETRYABLE_CLEANUP_REASONS

    def retryable_incident(self, allowed: frozenset[str] | None = None) -> str | None:
        """The single reason this owner's whole incident consists of, if recoverable.

        Machine recovery may only address an incident this live owner raised and
        still holds. An incident adopted from a dead owner, a broken audit, a mix
        of reasons, or a still-active sibling lease all leave state that no check
        this process can run will settle, so each of those keeps needing the
        operator regardless of how wide ``allowed`` is."""
        with self._guard:
            if self._state != "open" or not self.blocked or self.project_lock is None:
                return None
            quarantined = [lease for lease in self.leases.values() if lease.state == "cleanup_required"]
            if not quarantined or any(lease.state == "active" for lease in self.leases.values()):
                return None
            if any(not lease.audit_ok for lease in quarantined):
                return None
            held = {resource for lease in quarantined for resource in lease.resources}
            if not self.incident_resources <= held:
                return None
            reasons = {reason for lease in quarantined for reason in lease.cleanup_reasons()}
            if len(reasons) != 1:
                return None
            reason = reasons.pop()
            return reason if reason in (RETRYABLE_CLEANUP_REASONS if allowed is None else allowed) else None

    def resolve_retryable_incident(self, reason: str, *, allowed: frozenset[str] | None = None) -> bool:
        """Clear a recoverable incident once the caller has verified safe state.

        The caller owns the verification and, with it, the decision of how wide
        ``allowed`` may be; this only performs the state transition, and only for
        the exact incident ``retryable_incident`` still reports."""
        with self._guard:
            permitted = RETRYABLE_CLEANUP_REASONS if allowed is None else allowed
            if reason not in permitted or self.retryable_incident(permitted) != reason:
                return False
            for lease in [item for item in self.leases.values() if item.state == "cleanup_required"]:
                if not self.resolve_retryable_cleanup(lease, reason, allowed=permitted):
                    return False
            return not self.blocked

    def poison(self, reason: str, error: object | None = None, *, audit_broken: bool = False, resources: list[str] | None = None) -> None:
        with self._guard:
            self._require_open()
            if self.leases:
                # Every still-registered lease is quarantined, no matter which
                # provisional state ("releasing", "released"-in-memory, ...) an
                # interrupted transition left behind.
                for lease in list(self.leases.values()):
                    self._quarantine_registered_lease(lease, reason, error, audit_broken=audit_broken)
                return
            if self.quarantine_id is None:
                self.quarantine_id = secrets.token_hex(16)
            self.blocked = True
            self.incident_resources.update(resources or [])
            if self.project_lock is None:
                self.project_lock = self._acquire_lock(self.project_key, resources or [self.project_key])
            self._persist_project("cleanup_required", sorted(self.incident_resources))

    def status(self) -> JsonObject:
        with self._guard:
            owner_active = self.project_lock is not None
            snapshot_atomic = True
            if owner_active:
                record = self._read_record(self.project_key)
            else:
                probe = self._project_probe_lock()
                try:
                    probe.acquire()
                except (BlockingIOError, OSError):
                    # A live owner holds the lock; the record can change under us,
                    # so the snapshot is advisory and must never trigger mutation.
                    owner_active = True
                    snapshot_atomic = False
                    record = self._read_record(self.project_key)
                else:
                    try:
                        # Only a record read while holding the probe lock may drive
                        # the exited-owner quarantine transition.
                        record = self._read_record(self.project_key)
                        if record is not None and record.get("state") == "active":
                            stale_resources = [item for item in record.get("resources", []) if isinstance(item, str)]
                            self._adopt_incident(record, stale_resources)
                            record = {**record, "version": LEASE_VERSION, "state": "quarantined", "quarantined_at": utc_now_iso(), "reason": "owner_process_exited_without_release", "quarantine_id": self.quarantine_id}
                            self._write_record(self.project_key, record)
                            self._mark_incident_resources(stale_resources, "owner_process_exited_without_release", expected_project=self.project_key)
                    finally:
                        probe.release()
            blocked_state = bool(record and record.get("state") in {"cleanup_required", "quarantined", "recovery_pending"})
            blocked = self.blocked or blocked_state
            reasons = _record_cleanup_reasons(record)
            return {
                "ok": True,
                "tool": "hardware_lease_status",
                "project_resource": self.project_key,
                "owner_active": owner_active,
                "snapshot_atomic": snapshot_atomic,
                "blocked": blocked,
                # Lifted out of the record so an operator can decide between
                # retrying and walking to the bench without opening state files.
                "cleanup_reasons": reasons,
                "auto_recoverable": blocked and bool(reasons) and all(reason in self.recoverable_reasons() for reason in reasons),
                "auto_recover_policy": self.config.recovery.auto_recover,
                "quarantine_id": self.quarantine_id or (record or {}).get("quarantine_id"),
                "lifecycle_state": self._state,
                # Strip secret-named fields AND the absolute env-derived paths
                # (config_path, workspace) before the record reaches an operator
                # terminal or MCP client.
                "record": _public_record(record),
                "leases": [lease.status() for lease in self.leases.values()],
            }

    def _project_probe_lock(self) -> _LifetimeLock:
        return _LifetimeLock(self.lock_directory / f"{resource_digest(self.project_key)}.lock")

    def recover(self, *, safe_state_confirmed: bool, quarantine_id: str | None = None, accept_config_change: bool = False) -> JsonObject:
        if not safe_state_confirmed:
            return {"ok": False, "tool": "hardware_recover", "error_type": "operator_confirmation_required", "summary": "Recovery requires explicit operator confirmation of physical safe state."}
        if not quarantine_id:
            return {"ok": False, "tool": "hardware_recover", "error_type": "quarantine_id_required", "summary": "Recovery requires the current quarantine_id from lease-status."}
        with self._guard:
            self._require_open()
            if self.project_lock is not None or self.leases:
                return {"ok": False, "tool": "hardware_recover", "error_type": "resource_busy", "summary": "Live owner still holds project resources."}
            try:
                project_lock = self._acquire_lock(self.project_key, [self.project_key])
            except CoordinationError as error:
                return error.result
            locks: list[_LifetimeLock] = []
            try:
                record = self._read_record(self.project_key) or {}
                state = record.get("state")
                if state not in {"cleanup_required", "quarantined", "recovery_pending"}:
                    return {"ok": False, "tool": "hardware_recover", "error_type": "resource_not_quarantined", "summary": "Project has no quarantined incident to recover."}
                if record.get("quarantine_id") != quarantine_id or not self._record_matches_project(record):
                    return {"ok": False, "tool": "hardware_recover", "error_type": "quarantine_changed", "summary": "Quarantine incident changed; inspect lease-status and confirm the current incident."}
                if not self._record_config_acceptable(record, accept_config_change):
                    return {
                        "ok": False,
                        "tool": "hardware_recover",
                        "error_type": "config_changed",
                        "summary": "Authoritative config changed since the incident was recorded; verify the config delta, then rerun recovery with the explicit operator override.",
                        "recorded_config_sha256": record.get("config_sha256"),
                        "current_config_sha256": self.config_sha256,
                        "override": "accept_config_change (CLI: --accept-config-change)",
                        "quarantine_id": quarantine_id,
                    }
                resources = [item for item in record.get("resources", []) if isinstance(item, str)]
                if len(resources) != len(set(resources)):
                    return {"ok": False, "tool": "hardware_recover", "error_type": "coordination_state_invalid", "summary": "Quarantine resource markers are inconsistent."}
                resuming = state == "recovery_pending"
                for resource in sorted(set(resources)):
                    locks.append(self._acquire_lock(resource, resources))
                    marker = self._read_record(resource)
                    marker_state = (marker or {}).get("state")
                    if marker is not None and marker_state == "released" and marker.get("recovered_quarantine_id") == quarantine_id:
                        # Already committed by an interrupted run of the same
                        # recovery; resuming past it is idempotent.
                        continue
                    if marker is not None and marker_state == "active" and self._record_matches_project(marker) and self._record_config_acceptable(marker, accept_config_change):
                        # An active marker under a dead owner (recover holds the
                        # project lock, so no live owner) is an orphaned resource of
                        # this incident. Operator-confirmed recovery releases it too;
                        # its marker legitimately lacks the incident quarantine_id.
                        continue
                    marker_resources = [item for item in (marker or {}).get("resources", []) if isinstance(item, str)]
                    if (
                        marker is None
                        or marker_state not in {"cleanup_required", "quarantined"}
                        or marker.get("quarantine_id") != quarantine_id
                        or not self._record_matches_project(marker)
                        or not self._record_config_acceptable(marker, accept_config_change)
                        or resource not in marker_resources
                        or not set(marker_resources) <= set(resources)
                    ):
                        return {"ok": False, "tool": "hardware_recover", "error_type": "quarantine_changed", "summary": "Quarantine resource markers changed; recovery remains blocked.", "resource": resource}
                audit_event = {
                    "event": "recovery",
                    "quarantine_id": quarantine_id,
                    "resources": resources,
                    "workspace": self.config.workspace_root,
                    "config_path": self.config.config_path,
                    "recorded_config_sha256": record.get("config_sha256"),
                    "current_config_sha256": self.config_sha256,
                    "config_change_accepted": bool(accept_config_change and record.get("config_sha256") != self.config_sha256),
                    "resumed": resuming,
                    "time": utc_now_iso(),
                }
                try:
                    safe_append_text(self.root / "recovery.jsonl", json.dumps(audit_event) + "\n")
                except BaseException as error:
                    return {"ok": False, "tool": "hardware_recover", "error_type": "recovery_audit_failed", "summary": "Recovery audit could not be persisted; quarantine remains active.", "backend_error": str(error), "audit_ok": False, "cleanup_required": True, "quarantined": True, "quarantine_id": quarantine_id}
                released = self._base_record("released", resources)
                released.update({"recovered_at": utc_now_iso(), "safe_state_confirmed": True, "recovered_quarantine_id": quarantine_id})
                try:
                    if not resuming:
                        pending = {**record, "version": LEASE_VERSION, "state": "recovery_pending", "resources": resources, "quarantine_id": quarantine_id, "recovery_started_at": utc_now_iso()}
                        self._write_record(self.project_key, pending)
                    for resource in resources:
                        self._write_record(resource, released)
                    self._write_record(self.project_key, released)
                except BaseException as error:
                    if isinstance(error, (KeyboardInterrupt, SystemExit)):
                        raise
                    return {"ok": False, "tool": "hardware_recover", "error_type": "recovery_persist_failed", "summary": "Recovery could not persist all released markers; rerun recovery with the same quarantine_id to resume it.", "backend_error": str(error), "retry_safe": True, "cleanup_required": True, "quarantined": True, "quarantine_id": quarantine_id}
                self.blocked = False
                self.quarantine_id = None
                self.incident_resources.clear()
                return {"ok": True, "tool": "hardware_recover", "resources": resources, "safe_state_confirmed": True, "recovered_quarantine_id": quarantine_id, "resumed": resuming, "config_change_accepted": audit_event["config_change_accepted"], "summary": "Quarantined hardware resources were released after operator-confirmed recovery."}
            finally:
                for lock in reversed(locks):
                    lock.release()
                project_lock.release()

    def close(self) -> None:
        with self._guard:
            if self._state == "closed":
                return
            self._state = "closing"
            errors: list[BaseException] = []
            for lease in list(self.leases.values()):
                if lease.state not in {"cleanup_required", "quarantined"}:
                    self._quarantine_registered_lease(lease, "owner_closed_with_active_lease")
                lease.state = "quarantined"
                self._persist_lease(lease)
                remaining: list[_LifetimeLock] = []
                for lock in reversed(lease.locks):
                    try:
                        lock.release()
                    except BaseException as error:
                        if lock.descriptor >= 0:
                            remaining.append(lock)
                            errors.append(error)
                lease.locks = list(reversed(remaining))
                lease.valid = False
            if self.leases:
                self._persist_project("quarantined")
            self.leases = {lease_id: lease for lease_id, lease in self.leases.items() if lease.locks}
            if self.project_lock is not None:
                try:
                    self.project_lock.release()
                except BaseException as error:
                    if self.project_lock.descriptor >= 0:
                        errors.append(error)
                if self.project_lock.descriptor < 0:
                    self.project_lock = None
            if errors:
                self._state = "cleanup_required"
                raise RuntimeError("Hardware coordinator lock cleanup failed: " + "; ".join(str(error) for error in errors)) from errors[0]
            self.leases.clear()
            # A closed owner holds nothing, run or no run: the machine-wide hold
            # must not outlive the process that took it, or a clean shutdown
            # would block the bench exactly the way a crash must not. This is the
            # path a lost MCP client takes — stdin reaches EOF, the server closes
            # its service, and an unclosed run is released here rather than
            # waiting for the operating system to reap the process.
            self.declared_resources = None
            self.declared_devices = DeviceSet()
            self.run_label = None
            self.run_started_at = None
            self.bench.release_all()
            self._state = "closed"

    def _acquire_lock(self, resource: str, requested: list[str]) -> _LifetimeLock:
        lock = _LifetimeLock(self.lock_directory / f"{resource_digest(resource)}.lock")
        try:
            lock.acquire()
        except (BlockingIOError, OSError) as error:
            raise CoordinationError(
                {
                    "ok": False,
                    "error_type": "resource_busy",
                    "summary": "Hardware resource is owned by another Agentic HIL process.",
                    "resources": requested,
                    "retry_safe": True,
                    "backend_error": str(error),
                }
            ) from error
        return lock

    def _persist_lease(self, lease: HardwareLease, state: str | None = None, incident_override: list[str] | None = None) -> None:
        record_state = state or lease.state
        resources = list(lease.resources)
        incident = set(incident_override) if incident_override is not None else self.incident_resources
        if record_state in {"cleanup_required", "quarantined"} and incident:
            # Every incident marker carries the full incident resource union so
            # project record and resource markers stay recovery-consistent.
            resources = sorted(set(resources) | incident)
        record = self._base_record(record_state, resources)
        record.update(
            {
                "lease_id": lease.lease_id,
                "safe_state_confirmed": lease.safe_state_confirmed,
                "processes_reaped": lease.processes_reaped,
                "audit_ok": lease.audit_ok,
                "errors": list(lease.errors),
                "quarantine_id": lease.quarantine_id,
            }
        )
        for resource in lease.resources:
            self._write_record(resource, record)

    def _persist_project(self, state: str, resources: list[str] | None = None, *, leases: dict[str, HardwareLease] | None = None) -> None:
        population = self.leases if leases is None else leases
        if resources is None:
            resources = sorted({resource for lease in population.values() for resource in lease.resources} | (self.incident_resources if state in {"cleanup_required", "quarantined"} else set()))
        record = self._base_record(state, resources)
        record["leases"] = [lease.status() for lease in population.values()]
        if state in {"cleanup_required", "quarantined"}:
            record["quarantine_id"] = self.quarantine_id
        self._write_record(self.project_key, record)

    def _base_record(self, state: str, resources: list[str]) -> JsonObject:
        return {
            "version": LEASE_VERSION,
            "state": state,
            "owner_marker": self.owner_marker,
            "owner_pid": os.getpid(),
            "owner_started_at": self.owner_started_at,
            "frontend": self.frontend,
            "workspace": str(Path(self.config.work_dir).resolve()),
            "config_path": str(Path(self.config.config_path).resolve()),
            "config_sha256": self.config_sha256,
            "project_resource": self.project_key,
            "resources": list(resources),
            "updated_at": utc_now_iso(),
        }

    def _record_path(self, resource: str) -> Path:
        return self.record_directory / f"{resource_digest(resource)}.json"

    def _read_record(self, resource: str) -> JsonObject | None:
        # The record's absolute path lives under the environment-derived state_root
        # and must never appear in a result destined for an operator/MCP sink; the
        # resource name identifies the record safely for diagnostics.
        path = self._record_path(resource)
        try:
            text = safe_read_text(path)
        except FileNotFoundError:
            return None
        except (OSError, UnicodeDecodeError, ConfigError) as error:
            raise CoordinationError({"ok": False, "error_type": "coordination_state_invalid", "summary": "Hardware coordination state could not be read and requires operator recovery.", "resource": resource, **filesystem_error_detail(error)}) from error
        try:
            value = json.loads(text)
        except ValueError as error:
            raise CoordinationError({"ok": False, "error_type": "coordination_state_invalid", "summary": "Hardware coordination state is corrupted and requires operator recovery.", "resource": resource, "backend_error": str(error)}) from error
        if not isinstance(value, dict) or value.get("version") not in {1, LEASE_VERSION}:
            raise CoordinationError({"ok": False, "error_type": "coordination_state_invalid", "summary": "Hardware coordination state is invalid and requires operator recovery.", "resource": resource})
        if value.get("version") == 1:
            value = {**value, "version": LEASE_VERSION}
            if value.get("state") in {"cleanup_required", "quarantined"}:
                value["quarantine_id"] = legacy_quarantine_id(value)
        state = value.get("state")
        resources_field = value.get("resources", [])
        typed = (
            (state is None or isinstance(state, str))
            and isinstance(resources_field, list)
            and all(isinstance(item, str) for item in resources_field)
            and all(value.get(field) is None or isinstance(value.get(field), str) for field in ("quarantine_id", "project_resource", "workspace", "config_path", "config_sha256", "owner_marker", "recovered_quarantine_id"))
        )
        if not typed:
            raise CoordinationError({"ok": False, "error_type": "coordination_state_invalid", "summary": "Hardware coordination state has invalid field types and requires operator recovery.", "resource": resource})
        return value

    def _write_record(self, resource: str, record: JsonObject) -> None:
        atomic_write_text(self._record_path(resource), json.dumps(record, indent=2) + "\n")

    def _quarantined_result(self, resources: list[str], summary: str) -> JsonObject:
        return {
            "ok": False,
            "error_type": "resource_quarantined",
            "summary": summary,
            "resources": resources,
            "cleanup_required": True,
            "quarantined": True,
            "retry_safe": False,
            "quarantine_id": self.quarantine_id,
        }

    def _require_open(self) -> None:
        if self._state != "open":
            raise CoordinationError({"ok": False, "error_type": "coordination_closed", "summary": "Hardware coordinator is closed.", "side_effect_committed": False})

    def _valid_lease(self, lease: HardwareLease) -> bool:
        return (
            self._state == "open"
            and lease.valid
            and self.leases.get(lease.lease_id) is lease
            and lease.coordinator is self
            and self.project_lock is not None
            and self.project_lock.locked
            and all(lock.locked for lock in lease.locks)
        )

    def _adopt_incident(self, record: JsonObject, resources: list[str]) -> None:
        incident = record.get("quarantine_id")
        self.quarantine_id = incident if isinstance(incident, str) and incident else secrets.token_hex(16)
        self.incident_resources.update(resources)
        self.blocked = True

    def _quarantine_registered_lease(self, lease: HardwareLease, reason: str, error: object | None = None, *, audit_broken: bool = False) -> None:
        if self.quarantine_id is None:
            self.quarantine_id = secrets.token_hex(16)
        grew = not self.incident_resources >= set(lease.resources)
        self.incident_resources.update(lease.resources)
        lease.state = "cleanup_required"
        lease.quarantine_id = self.quarantine_id
        lease.audit_ok = lease.audit_ok and not audit_broken
        details: JsonObject = {"reason": reason, "time": utc_now_iso(), "quarantine_id": self.quarantine_id}
        if error is not None:
            details.update({"error_type": type(error).__name__, "summary": str(error)})
        if not any(item.get("reason") == reason and item.get("summary") == details.get("summary") for item in lease.errors):
            lease.errors.append(details)
        self.blocked = True
        self._persist_lease(lease)
        if grew:
            # The incident union grew: re-persist markers of every other
            # quarantined lease so all markers agree on the same resource set.
            for item in self.leases.values():
                if item is not lease and item.state in {"cleanup_required", "quarantined"}:
                    self._persist_lease(item)
        self._persist_project("cleanup_required")

    def _record_matches_project(self, record: JsonObject) -> bool:
        return (
            record.get("project_resource") == self.project_key
            and record.get("workspace") == str(Path(self.config.work_dir).resolve())
            and record.get("config_path") == str(Path(self.config.config_path).resolve())
        )

    def _record_matches_config(self, record: JsonObject) -> bool:
        return self._record_matches_project(record) and record.get("config_sha256") == self.config_sha256

    def _record_config_acceptable(self, record: JsonObject, accept_config_change: bool) -> bool:
        if self._record_matches_config(record):
            return True
        return accept_config_change and self._record_matches_project(record)

    def _mark_incident_resources(self, resources: list[str], reason: str, *, expected_project: str) -> None:
        locks: list[_LifetimeLock] = []
        try:
            for resource in sorted(set(resources)):
                lock = self._acquire_lock(resource, resources)
                locks.append(lock)
                marker = self._read_record(resource)
                if marker is not None and marker.get("state") not in {None, "released"} and marker.get("project_resource") != expected_project:
                    raise CoordinationError({"ok": False, "error_type": "coordination_state_invalid", "summary": "Physical resource marker belongs to a different unresolved project incident.", "resource": resource})
                incident = {**self._base_record("quarantined", resources), "quarantined_at": utc_now_iso(), "reason": reason, "quarantine_id": self.quarantine_id}
                self._write_record(resource, incident)
        finally:
            for lock in reversed(locks):
                lock.release()


def project_resource(config: AgenticHILConfig) -> str:
    identity = "\0".join([os.path.normcase(str(Path(config.config_path).resolve())), os.path.normcase(str(Path(config.work_dir).resolve()))])
    return f"project:{hashlib.sha256(identity.encode('utf-8')).hexdigest()}"


# The four functions below are the coordination layer's view of a device's lock
# key. They derive nothing themselves any more: agentic_hil.devices owns the
# identity, so a backend, the reactor's declaration and a run over MCP cannot
# disagree about which lock a board falls under.
def debugger_resource(config: AgenticHILConfig) -> str:
    debugger = config.debugger
    if debugger is None:
        raise CoordinationError({"ok": False, "error_type": "invalid_argument", "summary": "No debugger is bound, so no probe resource can be leased.", "configured_debuggers": sorted(config.debuggers), "retry_safe": True})
    return DebuggerDevice(config_id=str(config.debugger_id), debugger=debugger).lock_key


def debugger_effect_resources(config: AgenticHILConfig) -> tuple[str, str]:
    # The discovery pseudo-resource is not a device: it names a host-wide
    # enumeration, and locking it machine-wide would serialize unrelated benches.
    return DEBUGGER_DISCOVERY_RESOURCE, debugger_resource(config)


def com_resource(config: AgenticHILConfig, port_id: str) -> str:
    return UartDevice(config_id=port_id, port=config.com_ports[port_id]).lock_key


def can_resource(config: AgenticHILConfig, bus_id: str) -> str:
    return CanDevice(config_id=bus_id, bus=config.can_buses[bus_id]).lock_key


def resource_digest(resource: str) -> str:
    return hashlib.sha256(resource.encode("utf-8")).hexdigest()


def legacy_quarantine_id(record: JsonObject) -> str:
    identity = "\0".join(str(record.get(field, "")) for field in ("owner_marker", "owner_started_at", "project_resource"))
    return "legacy-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]
