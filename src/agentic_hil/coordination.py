from __future__ import annotations

import hashlib
import json
import os
import secrets
import stat
import threading
from datetime import datetime, timezone
from pathlib import Path

from agentic_hil.config import (
    ConfigError,
    _close_windows_handles,
    _windows_hold_directory_chain,
    _windows_open_regular_file,
    atomic_write_text,
    safe_append_text,
    safe_read_bytes,
    safe_read_text,
    trusted_state_directory,
)
from agentic_hil.types import AgenticHILConfig, JsonObject

LEASE_VERSION = 2
DEBUGGER_DISCOVERY_RESOURCE = "debugger-discovery:all"
LEASE_RELEASE_RETRY_REASON = "lease_release_unconfirmed"
RETRYABLE_CLEANUP_REASONS = frozenset(
    {
        "com_buffer_clear_unconfirmed",
        "debug_backend_cleanup_exception",
        "debug_breakpoint_cleanup_unconfirmed",
        "debug_session_cleanup_unconfirmed",
        "debug_session_result_unconfirmed",
        "debug_target_state_unconfirmed",
        LEASE_RELEASE_RETRY_REASON,
    }
)
_LOCAL_LOCKS: set[str] = set()
_LOCAL_LOCKS_GUARD = threading.Lock()


class CoordinationError(RuntimeError):
    def __init__(self, result: JsonObject):
        super().__init__(str(result.get("summary", "Hardware resource coordination failed.")))
        self.result = result


class _LifetimeLock:
    def __init__(self, path: Path):
        self.path = path
        self.descriptor = -1
        self.directory_handles: list[int] = []
        self.locked = False
        self.local_key = os.path.normcase(str(path))

    def acquire(self) -> None:
        with _LOCAL_LOCKS_GUARD:
            if self.local_key in _LOCAL_LOCKS:
                raise BlockingIOError(f"Lock is already held in this process: {self.path}")
            _LOCAL_LOCKS.add(self.local_key)
        try:
            if os.name == "nt":
                self.directory_handles = _windows_hold_directory_chain(self.path.parent)
                self.descriptor = _windows_open_regular_file(self.path, read=True, write=True, create=True)
            else:
                flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
                self.descriptor = os.open(self.path, flags, 0o600)
            opened = os.fstat(self.descriptor)
            if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
                raise OSError("Coordination lock is not a single-link regular file.")
            if os.name == "nt":
                if opened.st_size == 0:
                    os.write(self.descriptor, b"0")
                os.lseek(self.descriptor, 0, os.SEEK_SET)
                import msvcrt

                msvcrt.locking(self.descriptor, msvcrt.LK_NBLCK, 1)
                self.locked = True
            else:
                import fcntl

                fcntl.flock(self.descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.locked = True
        except BaseException:
            self.release()
            raise

    def release(self) -> None:
        errors: list[BaseException] = []
        if self.descriptor >= 0:
            descriptor = self.descriptor
            try:
                if self.locked and os.name == "nt":
                    import msvcrt

                    os.lseek(descriptor, 0, os.SEEK_SET)
                    msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
                elif self.locked:
                    import fcntl

                    fcntl.flock(descriptor, fcntl.LOCK_UN)
            except BaseException as error:
                errors.append(error)
            else:
                # The OS lock is gone the moment unlock succeeds, even if the
                # following close() fails; reflect that so _valid_lease never
                # trusts a lock another process could now legitimately take.
                self.locked = False
            try:
                os.close(descriptor)
            except BaseException as error:
                errors.append(error)
            else:
                self.descriptor = -1
        try:
            _close_windows_handles(self.directory_handles)
        except BaseException as error:
            errors.append(error)
        finally:
            self.directory_handles.clear()
            if self.descriptor < 0:
                with _LOCAL_LOCKS_GUARD:
                    _LOCAL_LOCKS.discard(self.local_key)
        if errors:
            raise errors[0]


class HardwareLease:
    def __init__(self, coordinator: HardwareCoordinator, lease_id: str, resources: list[str], locks: list[_LifetimeLock]):
        self.coordinator = coordinator
        self.lease_id = lease_id
        self.resources = resources
        self.locks = locks
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
        self.owner_token = secrets.token_hex(32)
        self.owner_started_at = utc_now_iso()
        self.config_sha256 = hashlib.sha256(safe_read_bytes(config.config_path)).hexdigest()
        self.root = trusted_state_directory(config.state_root, "coordination")
        self.lock_directory = trusted_state_directory(config.state_root, "coordination", "locks")
        self.record_directory = trusted_state_directory(config.state_root, "coordination", "records")
        self.project_key = project_resource(config)
        self.project_lock: _LifetimeLock | None = None
        self.leases: dict[str, HardwareLease] = {}
        self.blocked = False
        self.quarantine_id: str | None = None
        self.incident_resources: set[str] = set()
        self._state = "open"
        self._guard = threading.RLock()

    def acquire(self, *resources: str) -> HardwareLease:
        normalized = sorted(set(resource for resource in resources if resource))
        if not normalized:
            raise ValueError("At least one physical resource is required.")
        with self._guard:
            self._require_open()
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
                lease = HardwareLease(self, secrets.token_hex(16), normalized, locks)
                self.leases[lease.lease_id] = lease
                self._persist_lease(lease)
                self._persist_project("active")
                return lease
            except BaseException:
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

    def resolve_retryable_cleanup(self, lease: HardwareLease, reason: str) -> bool:
        with self._guard:
            if not self._valid_lease(lease):
                lease.valid = False
                lease.state = "stale"
                return False
            if reason not in RETRYABLE_CLEANUP_REASONS:
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
            return {
                "ok": True,
                "tool": "hardware_lease_status",
                "project_resource": self.project_key,
                "owner_active": owner_active,
                "snapshot_atomic": snapshot_atomic,
                "blocked": self.blocked or blocked_state,
                "quarantine_id": self.quarantine_id or (record or {}).get("quarantine_id"),
                "lifecycle_state": self._state,
                "record": record,
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

    def _persist_lease(self, lease: HardwareLease, state: str | None = None) -> None:
        record_state = state or lease.state
        resources = list(lease.resources)
        if record_state in {"cleanup_required", "quarantined"} and self.incident_resources:
            # Every incident marker carries the full incident resource union so
            # project record and resource markers stay recovery-consistent.
            resources = sorted(set(resources) | self.incident_resources)
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
            "owner_token": self.owner_token,
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
        path = self._record_path(resource)
        try:
            text = safe_read_text(path)
        except FileNotFoundError:
            return None
        except (OSError, UnicodeDecodeError, ConfigError) as error:
            raise CoordinationError({"ok": False, "error_type": "coordination_state_invalid", "summary": "Hardware coordination state could not be read and requires operator recovery.", "record_path": str(path), "backend_error": str(error)}) from error
        try:
            value = json.loads(text)
        except ValueError as error:
            raise CoordinationError({"ok": False, "error_type": "coordination_state_invalid", "summary": "Hardware coordination state is corrupted and requires operator recovery.", "record_path": str(path), "backend_error": str(error)}) from error
        if not isinstance(value, dict) or value.get("version") not in {1, LEASE_VERSION}:
            raise CoordinationError({"ok": False, "error_type": "coordination_state_invalid", "summary": "Hardware coordination state is invalid and requires operator recovery.", "record_path": str(path)})
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
            and all(value.get(field) is None or isinstance(value.get(field), str) for field in ("quarantine_id", "project_resource", "workspace", "config_path", "config_sha256", "owner_token", "recovered_quarantine_id"))
        )
        if not typed:
            raise CoordinationError({"ok": False, "error_type": "coordination_state_invalid", "summary": "Hardware coordination state has invalid field types and requires operator recovery.", "record_path": str(path)})
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


def debugger_resource(config: AgenticHILConfig) -> str:
    if config.debugger.resource_id:
        return f"physical:{os.path.normcase(config.debugger.resource_id)}"
    identity = config.debugger.probe_id or config.debugger.executable or config.debugger.type
    return f"probe:{os.path.normcase(str(identity))}"


def debugger_effect_resources(config: AgenticHILConfig) -> tuple[str, str]:
    return DEBUGGER_DISCOVERY_RESOURCE, debugger_resource(config)


def com_resource(config: AgenticHILConfig, port_id: str) -> str:
    port = config.com_ports[port_id]
    return f"physical:{os.path.normcase(port.resource_id)}" if port.resource_id else f"com:{os.path.normcase(port.device)}"


def can_resource(config: AgenticHILConfig, bus_id: str) -> str:
    bus = config.can_buses[bus_id]
    return f"physical:{os.path.normcase(bus.resource_id)}" if bus.resource_id else f"can:{bus.adapter}:{os.path.normcase(bus.channel)}"


def adapter_resource(config: AgenticHILConfig, adapter_id: str) -> str:
    adapter = config.adapters[adapter_id]
    return f"physical:{os.path.normcase(adapter.resource_id)}" if adapter.resource_id else f"adapter:{os.path.normcase(str(Path(adapter.executable).resolve()))}"


def resource_digest(resource: str) -> str:
    return hashlib.sha256(resource.encode("utf-8")).hexdigest()


def legacy_quarantine_id(record: JsonObject) -> str:
    identity = "\0".join(str(record.get(field, "")) for field in ("owner_token", "owner_started_at", "project_resource"))
    return "legacy-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
