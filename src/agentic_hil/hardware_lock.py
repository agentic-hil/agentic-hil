from __future__ import annotations

import errno
import hashlib
import json
import os
import socket
import threading
import uuid
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from agentic_hil.types import JsonObject


class HardwareLockError(Exception):
    pass


class HardwareQuarantinedError(HardwareLockError):
    def __init__(self, details: JsonObject):
        self.details = details
        super().__init__("Project hardware state is unconfirmed.")


@dataclass(frozen=True)
class HardwareLeaseState:
    version: int
    state: Literal["active", "quarantined"]
    project_key: str
    lease_id: str
    quarantine_id: str
    pid: int
    hostname: str
    started_at: str
    updated_at: str
    source: str
    config_path: str
    reason: str | None
    active_resources: list[JsonObject]
    inspection_errors: list[JsonObject]


class ProjectHardwareLock:
    _owners: dict[str, str] = {}
    _owners_guard = threading.Lock()

    def __init__(self, config_path: str):
        self.config_path = str(Path(config_path).resolve())
        self.project_key = hashlib.sha256(os.path.normcase(self.config_path).encode("utf-8")).hexdigest()
        self.state_dir = hardware_state_directory()
        self.path = self.state_dir / f"hardware-{self.project_key}.lock"
        self.state_path = self.state_dir / f"hardware-{self.project_key}.state.json"
        self.quarantine_path = self.state_path
        self.owner_token = ""
        self.lease_id = ""
        self.handle: Any = None
        self.mode: Literal["none", "normal", "recovery"] = "none"
        self.recovery_incident_id: str | None = None
        self._instance_guard = threading.RLock()

    def acquire(self, *, recovery: bool = False, source: str = "agentic_hil", ignore_quarantine: bool | None = None) -> bool:
        with self._instance_guard:
            return self._acquire(recovery=recovery, source=source, ignore_quarantine=ignore_quarantine)

    def _acquire(self, *, recovery: bool, source: str, ignore_quarantine: bool | None) -> bool:
        if ignore_quarantine is not None:
            recovery = ignore_quarantine
        if self.handle is not None:
            requested_mode = "recovery" if recovery else "normal"
            if self.mode != requested_mode:
                raise HardwareLockError("Hardware lock mode cannot change while held.")
            return True
        self.owner_token = uuid.uuid4().hex
        with self._owners_guard:
            if str(self.path) in self._owners:
                return False
            self._owners[str(self.path)] = self.owner_token
        try:
            self._open_handle()
            if not self._acquire_os_lock():
                self._close_handle()
                self._clear_owner_reservation()
                return False
            state = self._read_state()
            if state is not None and not recovery:
                self._release_os_lock()
                raise HardwareQuarantinedError(state)
            if not recovery:
                self.mode = "normal"
                self.lease_id = uuid.uuid4().hex
                self._write_state(self._new_active_state(source))
            else:
                self.mode = "recovery"
                self.recovery_incident_id = str(state.get("quarantine_id")) if isinstance(state, dict) else None
        except HardwareLockError:
            if self.handle is not None:
                self._release_os_lock()
            else:
                self._clear_owner_reservation()
            raise
        except OSError as error:
            if self.handle is not None:
                self._release_os_lock()
            else:
                self._clear_owner_reservation()
            raise HardwareLockError(str(error)) from error
        except BaseException:
            if self.handle is not None:
                self._release_os_lock()
            else:
                self._clear_owner_reservation()
            raise
        return True

    def confirm_safe_and_release(self) -> None:
        with self._instance_guard:
            self._confirm_safe_and_release()

    def _confirm_safe_and_release(self) -> None:
        if self.handle is None:
            return
        try:
            self._confirm_safe()
        finally:
            self._release_os_lock()

    def confirm_safe(self) -> None:
        with self._instance_guard:
            self._confirm_safe()

    def _confirm_safe(self) -> None:
        if self.handle is None:
            raise HardwareLockError("No hardware lease is held.")
        if self.mode != "normal":
            raise HardwareLockError("Only a normal hardware lease can confirm safe completion.")
        state = self._read_state()
        if state is None or state.get("state") != "active" or state.get("lease_id") != self.lease_id:
            raise HardwareLockError("Current active hardware lease state is missing or owned by another lease.")
        self._delete_state()

    def quarantine_and_release(
        self,
        *,
        reason: str,
        source: str,
        active_resources: list[JsonObject],
        inspection_errors: list[JsonObject],
    ) -> JsonObject:
        with self._instance_guard:
            marker = self.mark_quarantined(reason=reason, source=source, active_resources=active_resources, inspection_errors=inspection_errors)
            self._release_os_lock()
            return marker

    def status(self) -> JsonObject:
        state = self._read_state()
        busy = self._owner_in_current_process() or not self._probe_lock_available()
        return {
            "ok": True,
            "busy": busy,
            "quarantined": isinstance(state, dict) and state.get("state") == "quarantined",
            "hardware_state_unconfirmed": state is not None,
            "state": state,
            "lock_path": str(self.path),
            "state_path": str(self.state_path),
        }

    def release(self) -> None:
        self.release_os_lock()

    def release_os_lock(self) -> None:
        with self._instance_guard:
            self._release_os_lock()

    def quarantine_info(self) -> JsonObject | None:
        return self._read_state()

    def mark_quarantined(
        self,
        *,
        reason: str,
        source: str,
        active_resources: list[JsonObject],
        inspection_errors: list[JsonObject],
    ) -> JsonObject:
        with self._instance_guard:
            self._require_owner()
            active_state = self._read_state()
            if active_state is None or active_state.get("state") != "active" or active_state.get("lease_id") != self.lease_id:
                raise HardwareLockError("Current active hardware lease state is missing or owned by another lease.")
            marker = self._quarantined_state_from_active(active_state, reason=reason, source=source, active_resources=active_resources, inspection_errors=inspection_errors)
            self._write_state(marker)
            return marker

    def clear_quarantine(self, expected_quarantine_id: str) -> None:
        with self._instance_guard:
            self._require_owner()
            if self.mode != "recovery":
                raise HardwareLockError("Quarantine can only be cleared under a recovery lock.")
            state = self._read_state()
            if state is None or state.get("state") not in {"active", "quarantined"} or state.get("quarantine_id") != expected_quarantine_id:
                raise HardwareLockError("Hardware quarantine changed after operator inspection.")
            self._delete_state()

    def update_active_state(self, *, source: str, active_resources: list[JsonObject], operation: JsonObject | None) -> None:
        with self._instance_guard:
            self._require_owner()
            if self.mode != "normal":
                raise HardwareLockError("Only a normal hardware lease can update active state.")
            state = self._read_state()
            if state is None or state.get("state") != "active" or state.get("lease_id") != self.lease_id:
                raise HardwareLockError("Current active hardware lease state is missing or owned by another lease.")
            marker = {**state, "updated_at": utc_now_iso(), "source": source, "active_resources": active_resources, "operation": operation}
            self._write_state(marker)

    @classmethod
    def owner_is_active(cls, config_path: str, owner_token: str) -> bool:
        lock = cls(config_path)
        with cls._owners_guard:
            return cls._owners.get(str(lock.path)) == owner_token

    def _new_active_state(self, source: str) -> JsonObject:
        started_at = utc_now_iso()
        return {
            "version": 1,
            "state": "active",
            "project_key": self.project_key,
            "lease_id": self.lease_id,
            "quarantine_id": self.lease_id,
            "pid": os.getpid(),
            "hostname": socket.gethostname(),
            "started_at": started_at,
            "updated_at": started_at,
            "source": source,
            "config_path": self.config_path,
            "reason": None,
            "active_resources": [],
            "inspection_errors": [],
        }

    def _quarantined_state_from_active(
        self,
        active_state: JsonObject,
        *,
        reason: str,
        source: str,
        active_resources: list[JsonObject],
        inspection_errors: list[JsonObject],
    ) -> JsonObject:
        return {
            **active_state,
            "state": "quarantined",
            "updated_at": utc_now_iso(),
            "source": source,
            "reason": reason,
            "active_resources": active_resources,
            "inspection_errors": inspection_errors,
        }

    def _read_state(self) -> JsonObject | None:
        try:
            text = self.state_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as error:
            marker_id = "unreadable-" + hashlib.sha256(f"{self.project_key}:{error}".encode()).hexdigest()
            return self._invalid_state("state_unreadable", str(error), marker_id, recovery_blocked=True)
        try:
            stat = self.state_path.stat()
        except OSError as error:
            marker_id = "unreadable-" + hashlib.sha256(f"{self.project_key}:{error}".encode()).hexdigest()
            return self._invalid_state("state_unreadable", str(error), marker_id, recovery_blocked=True)
        fingerprint = f"{text}\0{stat.st_size}\0{stat.st_mtime_ns}\0{getattr(stat, 'st_ino', 0)}"
        marker_id = "invalid-" + hashlib.sha256(fingerprint.encode()).hexdigest()
        try:
            raw = json.loads(text)
        except json.JSONDecodeError as error:
            return self._invalid_state("state_invalid", str(error), marker_id)
        if not isinstance(raw, dict) or not self._state_is_valid(raw):
            return self._invalid_state("state_invalid", "Hardware lease state marker failed schema validation.", marker_id)
        return raw

    def _state_is_valid(self, state: JsonObject) -> bool:
        return (
            state.get("version") == 1
            and state.get("state") in {"active", "quarantined"}
            and state.get("project_key") == self.project_key
            and isinstance(state.get("lease_id"), str)
            and bool(state.get("lease_id"))
            and isinstance(state.get("quarantine_id"), str)
            and bool(state.get("quarantine_id"))
            and isinstance(state.get("pid"), int)
            and isinstance(state.get("hostname"), str)
            and isinstance(state.get("started_at"), str)
            and isinstance(state.get("updated_at"), str)
            and isinstance(state.get("source"), str)
            and isinstance(state.get("config_path"), str)
            and isinstance(state.get("active_resources"), list)
            and isinstance(state.get("inspection_errors"), list)
        )

    def _invalid_state(self, reason: str, error: str, marker_id: str, recovery_blocked: bool = False) -> JsonObject:
        return {
            "version": 1,
            "state": "quarantined",
            "project_key": self.project_key,
            "lease_id": marker_id,
            "quarantine_id": marker_id,
            "pid": 0,
            "hostname": "unknown",
            "started_at": utc_now_iso(),
            "updated_at": utc_now_iso(),
            "source": "hardware_lock",
            "config_path": self.config_path,
            "reason": reason,
            "recovery_blocked": recovery_blocked,
            "active_resources": [],
            "inspection_errors": [{"type": "hardware_state", "error": error}],
        }

    def _write_state(self, marker: JsonObject) -> None:
        self._ensure_state_directory()
        temp_path = self.state_path.with_name(f"{self.state_path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temp_path.open("w", encoding="utf-8") as handle:
                json.dump(marker, handle, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            if os.name != "nt":
                temp_path.chmod(0o600)
            os.replace(temp_path, self.state_path)
            fsync_directory(self.state_path.parent)
        except (OSError, TypeError, ValueError) as error:
            with suppress(OSError):
                temp_path.unlink()
            raise HardwareLockError(str(error)) from error

    def _delete_state(self) -> None:
        try:
            self.state_path.unlink()
            fsync_directory(self.state_path.parent)
        except FileNotFoundError:
            return
        except OSError as error:
            raise HardwareLockError(str(error)) from error

    def _open_handle(self) -> None:
        self._ensure_state_directory()
        existed = self.path.exists()
        self.handle = self.path.open("a+b")
        if os.name != "nt":
            self.path.chmod(0o600)
        if os.name == "nt" and os.fstat(self.handle.fileno()).st_size == 0:
            self.handle.seek(0)
            self.handle.write(b"0")
            self.handle.flush()
        if not existed:
            self.handle.flush()
            os.fsync(self.handle.fileno())
            fsync_directory(self.path.parent)

    def _acquire_os_lock(self) -> bool:
        if self.handle is None:
            raise HardwareLockError("Hardware lease file is not open.")
        try:
            if os.name == "nt":
                import msvcrt

                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            if error.errno in {errno.EACCES, errno.EAGAIN}:
                return False
            raise HardwareLockError(str(error)) from error
        return True

    def _release_os_lock(self) -> None:
        if self.handle is None:
            return
        try:
            with suppress(OSError):
                if os.name == "nt":
                    import msvcrt

                    self.handle.seek(0)
                    msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        finally:
            with self._owners_guard:
                if self._owners.get(str(self.path)) == self.owner_token:
                    self._owners.pop(str(self.path), None)
            self._close_handle()
            self.mode = "none"
            self.recovery_incident_id = None
            self.lease_id = ""
            self.owner_token = ""

    def _probe_lock_available(self) -> bool:
        handle: Any = None
        try:
            self._ensure_state_directory()
            existed = self.path.exists()
            handle = self.path.open("a+b")
            if os.name != "nt":
                self.path.chmod(0o600)
            if os.name == "nt" and os.fstat(handle.fileno()).st_size == 0:
                handle.seek(0)
                handle.write(b"0")
                handle.flush()
            if not existed:
                handle.flush()
                os.fsync(handle.fileno())
                fsync_directory(self.path.parent)
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            return True
        except OSError as error:
            if error.errno in {errno.EACCES, errno.EAGAIN}:
                return False
            raise HardwareLockError(str(error)) from error
        finally:
            if handle is not None:
                with suppress(OSError):
                    handle.close()

    def _owner_in_current_process(self) -> bool:
        with self._owners_guard:
            return str(self.path) in self._owners

    def _clear_owner_reservation(self) -> None:
        with self._owners_guard:
            if self._owners.get(str(self.path)) == self.owner_token:
                self._owners.pop(str(self.path), None)

    def _ensure_state_directory(self) -> None:
        try:
            missing: list[Path] = []
            current = self.state_dir
            while not current.exists():
                missing.append(current)
                if current.parent == current:
                    break
                current = current.parent
            for directory in reversed(missing):
                try:
                    directory.mkdir(mode=0o700)
                except FileExistsError:
                    if not directory.is_dir():
                        raise
                else:
                    fsync_directory(directory.parent)
            if os.name != "nt":
                self.state_dir.parent.chmod(0o700)
                self.state_dir.chmod(0o700)
                fsync_directory(self.state_dir.parent)
                fsync_directory(self.state_dir)
        except OSError as error:
            raise HardwareLockError(str(error)) from error

    def _require_owner(self) -> None:
        with self._owners_guard:
            owned = self._owners.get(str(self.path)) == self.owner_token
        if self.handle is None or not owned:
            raise HardwareLockError("Hardware lease is not owned by this lock instance.")

    def _close_handle(self) -> None:
        if self.handle is not None:
            with suppress(OSError):
                self.handle.close()
            self.handle = None


def hardware_state_directory() -> Path:
    if os.name == "nt":
        configured = os.environ.get("LOCALAPPDATA")
        fallback = Path.home() / "AppData" / "Local"
    else:
        configured = os.environ.get("XDG_STATE_HOME")
        fallback = Path.home() / ".local" / "state"
    if not fallback.is_absolute():
        raise HardwareLockError("User state directory fallback is not absolute.")
    candidate = Path(configured).expanduser() if configured else fallback
    base = candidate if candidate.is_absolute() else fallback
    return base / "agentic-hil" / "hardware"


def marker_owner_is_alive(marker: JsonObject) -> bool:
    if marker.get("hostname") != socket.gethostname():
        return False
    pid = marker.get("pid")
    if not isinstance(pid, int) or pid <= 0:
        return False
    return process_is_alive(pid)


def process_is_alive(pid: int) -> bool:
    if os.name == "nt":
        return _windows_process_is_alive(pid)
    # POSIX only: on Windows os.kill(pid, 0) TERMINATES the target process.
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _windows_process_is_alive(pid: int) -> bool:
    import ctypes
    from ctypes import wintypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    ERROR_ACCESS_DENIED = 5
    STILL_ACTIVE = 259
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ctypes.get_last_error() == ERROR_ACCESS_DENIED
    try:
        exit_code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return True
        return exit_code.value == STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    flags = getattr(os, "O_DIRECTORY", 0) | os.O_RDONLY
    fd: int | None = None
    try:
        fd = os.open(path, flags)
        os.fsync(fd)
    except OSError as error:
        unsupported = {errno.EINVAL, getattr(errno, "ENOTSUP", errno.EINVAL), getattr(errno, "EOPNOTSUPP", errno.EINVAL)}
        if error.errno not in unsupported:
            raise
    finally:
        if fd is not None:
            with suppress(OSError):
                os.close(fd)
