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
    _close_windows_handles,
    _windows_hold_directory_chain,
    _windows_open_regular_file,
    atomic_write_text,
    safe_directory,
    safe_read_bytes,
    safe_read_text,
    user_state_root,
)
from agentic_hil.types import AgenticHILConfig, JsonObject

LEASE_VERSION = 1
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
            try:
                if self.locked and os.name == "nt":
                    import msvcrt

                    os.lseek(self.descriptor, 0, os.SEEK_SET)
                    msvcrt.locking(self.descriptor, msvcrt.LK_UNLCK, 1)
                elif self.locked:
                    import fcntl

                    fcntl.flock(self.descriptor, fcntl.LOCK_UN)
            except BaseException as error:
                errors.append(error)
            try:
                os.close(self.descriptor)
            except BaseException as error:
                errors.append(error)
            finally:
                self.descriptor = -1
                self.locked = False
        try:
            _close_windows_handles(self.directory_handles)
        except BaseException as error:
            errors.append(error)
        finally:
            self.directory_handles.clear()
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

    def release(self, *, safe_state_confirmed: bool = True, processes_reaped: bool = True, audit_ok: bool = True) -> bool:
        return self.coordinator.release_lease(
            self,
            safe_state_confirmed=safe_state_confirmed,
            processes_reaped=processes_reaped,
            audit_ok=audit_ok,
        )

    def quarantine(self, reason: str, error: object | None = None, *, audit_broken: bool = False) -> None:
        self.coordinator.quarantine_lease(self, reason, error, audit_broken=audit_broken)

    def resolve_retryable_cleanup(self, *allowed_reasons: str) -> bool:
        return self.coordinator.resolve_retryable_cleanup(self, set(allowed_reasons) if allowed_reasons else None)

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
        self.root = safe_directory(user_state_root() / "coordination")
        self.lock_directory = safe_directory(self.root / "locks")
        self.record_directory = safe_directory(self.root / "records")
        self.project_key = project_resource(config)
        self.project_lock: _LifetimeLock | None = None
        self.leases: dict[str, HardwareLease] = {}
        self.blocked = False
        self._guard = threading.RLock()

    def acquire(self, *resources: str) -> HardwareLease:
        normalized = sorted(set(resource for resource in resources if resource))
        if not normalized:
            raise ValueError("At least one physical resource is required.")
        with self._guard:
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
                    stale = {**stale, "state": "quarantined", "quarantined_at": utc_now_iso(), "reason": "owner_process_exited_without_release"}
                    self._write_record(self.project_key, stale)
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
                        stale = {**stale, "state": "quarantined", "quarantined_at": utc_now_iso(), "reason": "owner_process_exited_without_release"}
                        self._write_record(resource, stale)
                        self.blocked = True
                        self._persist_project("cleanup_required", normalized)
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
                    self._persist_project(state, normalized)
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
            if lease.lease_id not in self.leases:
                return lease.state == "released"
            lease.safe_state_confirmed = safe_state_confirmed
            lease.processes_reaped = processes_reaped
            lease.audit_ok = lease.audit_ok and audit_ok
            if not safe_state_confirmed or not processes_reaped or not lease.audit_ok:
                reason = "safe_state_unconfirmed" if not safe_state_confirmed else "process_reap_unconfirmed" if not processes_reaped else "audit_broken"
                self.quarantine_lease(lease, reason, audit_broken=not lease.audit_ok)
                return False
            lease.state = "released"
            self._persist_lease(lease)
            for lock in reversed(lease.locks):
                lock.release()
            lease.locks.clear()
            self.leases.pop(lease.lease_id, None)
            self.blocked = any(item.state in {"cleanup_required", "quarantined"} for item in self.leases.values())
            if not self.leases:
                self._persist_project("released")
                if self.project_lock is not None:
                    self.project_lock.release()
                    self.project_lock = None
            else:
                self._persist_project("cleanup_required" if self.blocked else "active")
            return True

    def quarantine_lease(self, lease: HardwareLease, reason: str, error: object | None = None, *, audit_broken: bool = False) -> None:
        with self._guard:
            if lease.state == "released":
                return
            lease.state = "cleanup_required"
            lease.audit_ok = lease.audit_ok and not audit_broken
            details: JsonObject = {"reason": reason, "time": utc_now_iso()}
            if error is not None:
                details.update({"error_type": type(error).__name__, "summary": str(error)})
            lease.errors.append(details)
            self.blocked = True
            self._persist_lease(lease)
            self._persist_project("cleanup_required")

    def resolve_retryable_cleanup(self, lease: HardwareLease, allowed_reasons: set[str] | None = None) -> bool:
        with self._guard:
            retryable_reasons = allowed_reasons or {"debug_session_result_unconfirmed"}
            if lease.state != "cleanup_required" or not lease.audit_ok or any(error.get("reason") not in retryable_reasons for error in lease.errors):
                return False
            lease.state = "active"
            lease.errors.clear()
            self.blocked = any(item.state in {"cleanup_required", "quarantined"} for item in self.leases.values())
            self._persist_lease(lease)
            self._persist_project("cleanup_required" if self.blocked else "active")
            return True

    def status(self) -> JsonObject:
        record = self._read_record(self.project_key)
        owner_active = self.project_lock is not None
        if not owner_active:
            probe = _LifetimeLock(self.lock_directory / f"{resource_digest(self.project_key)}.lock")
            try:
                probe.acquire()
            except (BlockingIOError, OSError):
                owner_active = True
            else:
                try:
                    if record is not None and record.get("state") == "active":
                        record = {**record, "state": "quarantined", "quarantined_at": utc_now_iso(), "reason": "owner_process_exited_without_release"}
                        self._write_record(self.project_key, record)
                finally:
                    probe.release()
        blocked_state = bool(record and record.get("state") in {"cleanup_required", "quarantined"})
        return {
            "ok": True,
            "tool": "hardware_lease_status",
            "project_resource": self.project_key,
            "owner_active": owner_active,
            "blocked": self.blocked or blocked_state,
            "record": record,
            "leases": [lease.status() for lease in self.leases.values()],
        }

    def recover(self, *, safe_state_confirmed: bool) -> JsonObject:
        if not safe_state_confirmed:
            return {"ok": False, "tool": "hardware_recover", "error_type": "operator_confirmation_required", "summary": "Recovery requires explicit operator confirmation of physical safe state."}
        with self._guard:
            if self.project_lock is not None or self.leases:
                return {"ok": False, "tool": "hardware_recover", "error_type": "resource_busy", "summary": "Live owner still holds project resources."}
            try:
                project_lock = self._acquire_lock(self.project_key, [self.project_key])
            except CoordinationError as error:
                return error.result
            record = self._read_record(self.project_key) or {}
            resources = [item for item in record.get("resources", []) if isinstance(item, str)]
            locks: list[_LifetimeLock] = []
            try:
                for resource in sorted(set(resources)):
                    locks.append(self._acquire_lock(resource, resources))
                released = self._base_record("released", resources)
                released.update({"recovered_at": utc_now_iso(), "safe_state_confirmed": True})
                for resource in resources:
                    self._write_record(resource, released)
                self._write_record(self.project_key, released)
                self.blocked = False
                return {"ok": True, "tool": "hardware_recover", "resources": resources, "safe_state_confirmed": True, "summary": "Quarantined hardware resources were released after operator-confirmed recovery."}
            finally:
                for lock in reversed(locks):
                    lock.release()
                project_lock.release()

    def close(self) -> None:
        with self._guard:
            for lease in list(self.leases.values()):
                if lease.state not in {"cleanup_required", "quarantined"}:
                    self.quarantine_lease(lease, "owner_closed_with_active_lease")
                lease.state = "quarantined"
                self._persist_lease(lease)
                for lock in reversed(lease.locks):
                    lock.release()
                lease.locks.clear()
            if self.leases:
                self._persist_project("quarantined")
            if self.project_lock is not None:
                self.project_lock.release()
                self.project_lock = None

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

    def _persist_lease(self, lease: HardwareLease) -> None:
        record = self._base_record(lease.state, lease.resources)
        record.update(
            {
                "lease_id": lease.lease_id,
                "safe_state_confirmed": lease.safe_state_confirmed,
                "processes_reaped": lease.processes_reaped,
                "audit_ok": lease.audit_ok,
                "errors": list(lease.errors),
            }
        )
        for resource in lease.resources:
            self._write_record(resource, record)

    def _persist_project(self, state: str, resources: list[str] | None = None) -> None:
        if resources is None:
            resources = sorted({resource for lease in self.leases.values() for resource in lease.resources})
        record = self._base_record(state, resources)
        record["leases"] = [lease.status() for lease in self.leases.values()]
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
        try:
            value = json.loads(safe_read_text(self._record_path(resource)))
        except FileNotFoundError:
            return None
        if not isinstance(value, dict) or value.get("version") != LEASE_VERSION:
            raise CoordinationError({"ok": False, "error_type": "coordination_state_invalid", "summary": "Hardware coordination state is invalid and requires operator recovery."})
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
        }


def project_resource(config: AgenticHILConfig) -> str:
    identity = "\0".join([os.path.normcase(str(Path(config.config_path).resolve())), os.path.normcase(str(Path(config.work_dir).resolve()))])
    return f"project:{hashlib.sha256(identity.encode('utf-8')).hexdigest()}"


def debugger_resource(config: AgenticHILConfig) -> str:
    if config.debugger.resource_id:
        return f"physical:{os.path.normcase(config.debugger.resource_id)}"
    identity = config.debugger.probe_id or config.debugger.executable or config.debugger.type
    return f"probe:{os.path.normcase(str(identity))}"


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


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
