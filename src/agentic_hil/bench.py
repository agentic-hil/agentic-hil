"""Machine-wide exclusivity for one physical device.

Read access needs no permission any more (decision 0018), so the mutex carries
the protective load on its own: a run that holds a board exclusively cannot be
disturbed from outside, and an observer while no run holds the board disturbs
nobody. Two properties follow, and both are about *where* the lock lives rather
than what it locks.

**One place per machine.** The lock is keyed on the physical device
(``physical:<resource_id>``, ``probe:<identity>``, ``com:<device>``,
``can:<adapter>:<channel>``) and lives under the user's home, never under
``state_root``. On 2026-08-02 two sessions held the same Nucleo and could not
see each other because their ``state_root`` differed; a lock kept per
configuration is not a bench lock. There is deliberately no environment override
for the location — an override is how two sessions diverge again.

**Crash safety without an operator.** Ownership is the OS advisory lock on
``<digest>.lock``, which the kernel drops when the holding process dies. A
killed run therefore frees its devices with no quarantine, no
``recover --confirm-safe-state``, and no waiting. The sibling
``<digest>.holder.json`` only *describes* the holder so a refusal can name it;
it is advisory, and taking the OS lock over a record that still says ``held``
is how this module detects that the previous owner died.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import socket
import stat
import threading
import time
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from agentic_hil.config import (
    ConfigError,
    _close_windows_handles,
    _windows_hold_directory_chain,
    _windows_open_regular_file,
    atomic_write_text,
    safe_read_text,
    secure_user_directory,
)
from agentic_hil.redact import filesystem_error_detail
from agentic_hil.types import JsonObject

HOLDER_RECORD_VERSION = 1
BENCH_ROOT_NAME = ".agentic-hil"
DEVICE_LOCK_DIRECTORY_NAME = "device-locks"
# Written into every holder record so a contender can judge a heartbeat's age
# against what the holder promised, not against a constant compiled into it.
HEARTBEAT_INTERVAL_S = 30.0
WAIT_POLL_INTERVAL_S = 0.2
# A wait is a decision the caller makes explicitly; it is still bounded, because
# an unbounded one is silent waiting wearing a parameter.
MAX_WAIT_S = 900.0
# Resource prefixes that name a physical device. `project:` identifies a
# configuration and `debugger-discovery:` a host-wide enumeration, and locking
# either machine-wide would serialize unrelated benches instead of protecting a
# board.
PHYSICAL_RESOURCE_PREFIXES = ("physical:", "probe:", "com:", "can:")

_LOCAL_LOCKS: set[str] = set()
_LOCAL_LOCKS_GUARD = threading.Lock()


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


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
                # Closing the fd also drops the OS lock, so if the explicit unlock
                # above raised but the close succeeds, the lock is still gone;
                # never leave `locked` True on a freed descriptor.
                self.descriptor = -1
                self.locked = False
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


class DeviceBusyError(RuntimeError):
    """A declared device is held by somebody else, and the result names them."""

    def __init__(self, result: JsonObject):
        super().__init__(str(result.get("summary", "Physical device is held by another owner.")))
        self.result = result


def is_physical_resource(resource: str) -> bool:
    return resource.startswith(PHYSICAL_RESOURCE_PREFIXES)


def physical_resources(resources: object) -> list[str]:
    if isinstance(resources, str):
        candidates: list[str] = [resources]
    else:
        candidates = [item for item in (resources or []) if isinstance(item, str)]
    return sorted({item for item in candidates if is_physical_resource(item)})


def device_lock_root() -> Path:
    """Where every process on this machine looks for a device lock.

    ``~/.agentic-hil/device-locks``: the home directory is the one location whose
    ancestors pass the trust check on a stock profile on both platforms — on
    Windows ``%APPDATA%`` and ``%LOCALAPPDATA%`` inherit AppData's app-capability
    ACE (``S-1-15-3-*``) and are rejected — and it is reachable by every process
    of this user without agreeing on a configuration first.
    """
    home = Path(os.path.expanduser("~"))
    return secure_user_directory(
        home / BENCH_ROOT_NAME / DEVICE_LOCK_DIRECTORY_NAME,
        field="device_lock_root",
        label="Machine-wide device lock directory",
    )


def resource_digest(resource: str) -> str:
    return hashlib.sha256(resource.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class BenchOwner:
    """Who holds a device, in terms a stranger's refusal may repeat.

    Deliberately excludes the coordinator's ``owner_marker`` and every absolute
    path: a contender needs to identify the holder, not to be handed the token
    that proves ownership or the environment-derived paths the redaction policy
    keeps out of operator-facing output."""

    owner_id: str
    pid: int
    host: str
    frontend: str
    label: str | None = None

    def as_json(self) -> JsonObject:
        payload: JsonObject = {"owner_id": self.owner_id, "pid": self.pid, "host": self.host, "frontend": self.frontend}
        if self.label:
            payload["label"] = self.label
        return payload


@dataclass
class _HeldDevice:
    resource: str
    lock: _LifetimeLock
    acquired_at: str
    holders: int = 1
    reclaimed_from: JsonObject | None = None


class BenchMutex:
    """One owner's hold on a set of physical devices.

    ``acquire`` is all-or-nothing over sorted resource names, so two owners
    reaching for an overlapping set cannot each keep half of it. Re-acquiring a
    device this owner already holds is a no-op that increments a holder count:
    the run takes the lock once and every hardware call inside it borrows the
    same hold instead of racing its own owner.
    """

    def __init__(self, *, frontend: str = "python", label: str | None = None, root: Path | None = None):
        self.owner = BenchOwner(owner_id=secrets.token_hex(8), pid=os.getpid(), host=socket.gethostname(), frontend=frontend, label=label)
        self._root = root
        self._held: dict[str, _HeldDevice] = {}
        self._guard = threading.RLock()

    @property
    def root(self) -> Path:
        if self._root is None:
            self._root = device_lock_root()
        return self._root

    def held_resources(self) -> frozenset[str]:
        with self._guard:
            return frozenset(self._held)

    def holds(self, resource: str) -> bool:
        with self._guard:
            return resource in self._held

    def acquire(self, resources: list[str] | tuple[str, ...], *, wait_s: float = 0.0) -> list[str]:
        """Hold every named physical device, or hold none of them.

        Returns the resources this call newly took (an already-held device
        contributes nothing). Raises DeviceBusyError naming the holder, which is
        the whole point: silent waiting hides a collision, so a wait happens only
        when the caller asked for one and it is bounded."""
        wanted = physical_resources(resources)
        if not wanted:
            return []
        deadline = time.monotonic() + _validated_wait(wait_s)
        with self._guard:
            taken: list[str] = []
            try:
                for resource in wanted:
                    if self._take(resource, deadline):
                        taken.append(resource)
            except BaseException:
                for resource in reversed(taken):
                    self._drop(resource)
                raise
            return taken

    def release(self, resources: list[str] | tuple[str, ...]) -> None:
        with self._guard:
            for resource in physical_resources(resources):
                self._drop(resource)

    def release_all(self) -> None:
        with self._guard:
            for resource in sorted(self._held):
                held = self._held[resource]
                held.holders = 1
                self._drop(resource)

    def heartbeat(self) -> None:
        """Refresh every held device's record so a stranger can age the hold.

        Best effort by construction: the OS lock, not this record, is what makes
        the hold true, and a failed heartbeat must never drop a device that is
        physically in use."""
        with self._guard:
            for resource in sorted(self._held):
                try:
                    self._write_holder(resource, "held")
                except (ConfigError, OSError):
                    continue

    def holder(self, resource: str) -> JsonObject | None:
        """The recorded holder of a device, whether or not it is still alive."""
        return self._read_holder(resource)

    def busy_result(self, resource: str, *, waited_s: float = 0.0) -> JsonObject:
        record = self._read_holder(resource) or {}
        holder = record.get("owner") if isinstance(record.get("owner"), dict) else None
        result: JsonObject = {
            "ok": False,
            "error_type": "device_busy",
            "summary": _busy_summary(resource, holder),
            "resource": resource,
            "retry_safe": True,
            "side_effect_committed": False,
            "held_since": record.get("acquired_at"),
            "heartbeat_at": record.get("heartbeat_at"),
        }
        if holder is not None:
            result["holder"] = holder
            if holder.get("pid") == os.getpid():
                result["holder_is_this_process"] = True
        age = _age_seconds(record.get("heartbeat_at"))
        if age is not None:
            result["heartbeat_age_s"] = age
            interval = record.get("heartbeat_interval_s")
            if isinstance(interval, (int, float)) and age > max(float(interval) * 4, 60.0):
                result["holder_heartbeat_stale"] = True
        if waited_s > 0:
            result["waited_s"] = round(waited_s, 3)
        return result

    def _take(self, resource: str, deadline: float) -> bool:
        held = self._held.get(resource)
        if held is not None:
            held.holders += 1
            return False
        started = time.monotonic()
        lock = _LifetimeLock(self.root / f"{resource_digest(resource)}.lock")
        while True:
            try:
                lock.acquire()
                break
            except (BlockingIOError, OSError) as error:
                if time.monotonic() >= deadline:
                    result = self.busy_result(resource, waited_s=time.monotonic() - started)
                    if isinstance(error, OSError) and not isinstance(error, BlockingIOError):
                        result.update(filesystem_error_detail(error))
                    raise DeviceBusyError(result) from error
                time.sleep(min(WAIT_POLL_INTERVAL_S, max(deadline - time.monotonic(), 0.0)))
                lock = _LifetimeLock(self.root / f"{resource_digest(resource)}.lock")
        # The lock is ours, so any record still claiming the device belongs to an
        # owner the OS already reaped. Say so instead of quietly overwriting it:
        # "the previous run died" is exactly what an operator needs to hear, and
        # it is the evidence that a crash frees the bench without a manual step.
        previous = self._read_holder(resource)
        reclaimed = previous if isinstance(previous, dict) and previous.get("state") == "held" else None
        self._held[resource] = _HeldDevice(resource=resource, lock=lock, acquired_at=utc_now_iso(), reclaimed_from=_reclaim_detail(reclaimed))
        # A device whose record cannot be written is still exclusively ours; only
        # the ability to name us to a contender is lost.
        with suppress(ConfigError, OSError):
            self._write_holder(resource, "held")
        return True

    def _drop(self, resource: str) -> None:
        held = self._held.get(resource)
        if held is None:
            return
        held.holders -= 1
        if held.holders > 0:
            return
        with suppress(ConfigError, OSError):
            self._write_holder(resource, "released", released=True)
        self._held.pop(resource, None)
        held.lock.release()

    def _holder_path(self, resource: str) -> Path:
        return self.root / f"{resource_digest(resource)}.holder.json"

    def _write_holder(self, resource: str, state: str, *, released: bool = False) -> None:
        held = self._held.get(resource)
        record: JsonObject = {
            "version": HOLDER_RECORD_VERSION,
            "state": state,
            "resource": resource,
            "owner": self.owner.as_json(),
            "acquired_at": held.acquired_at if held is not None else utc_now_iso(),
            "heartbeat_at": utc_now_iso(),
            "heartbeat_interval_s": HEARTBEAT_INTERVAL_S,
        }
        if released:
            record["released_at"] = record["heartbeat_at"]
        if held is not None and held.reclaimed_from is not None:
            record["reclaimed_from"] = held.reclaimed_from
        atomic_write_text(self._holder_path(resource), json.dumps(record, indent=2) + "\n")

    def _read_holder(self, resource: str) -> JsonObject | None:
        try:
            text = safe_read_text(self._holder_path(resource))
        except FileNotFoundError:
            return None
        except (OSError, UnicodeDecodeError, ConfigError):
            return None
        try:
            value = json.loads(text)
        except ValueError:
            return None
        if not isinstance(value, dict) or value.get("version") != HOLDER_RECORD_VERSION:
            return None
        return value

    def reclaimed(self, resource: str) -> JsonObject | None:
        with self._guard:
            held = self._held.get(resource)
            return None if held is None else held.reclaimed_from


def _validated_wait(wait_s: object) -> float:
    if wait_s is None or wait_s is False:
        return 0.0
    if isinstance(wait_s, bool) or not isinstance(wait_s, (int, float)):
        raise ConfigError("invalid_argument", "A device wait must be a number of seconds.", {"field": "wait_s", "value": wait_s})
    value = float(wait_s)
    if value < 0:
        raise ConfigError("invalid_argument", "A device wait must not be negative.", {"field": "wait_s", "value": wait_s})
    if value > MAX_WAIT_S:
        raise ConfigError(
            "invalid_argument",
            f"A device wait must be at most {MAX_WAIT_S:.0f} seconds; an unbounded wait is silent waiting wearing a parameter.",
            {"field": "wait_s", "value": wait_s, "max_wait_s": MAX_WAIT_S},
        )
    return value


def _reclaim_detail(record: JsonObject | None) -> JsonObject | None:
    if record is None:
        return None
    owner = record.get("owner") if isinstance(record.get("owner"), dict) else {}
    return {
        "owner": owner,
        "held_since": record.get("acquired_at"),
        "last_heartbeat_at": record.get("heartbeat_at"),
        "reason": "owner_process_exited_without_release",
        "summary": "The previous owner exited without releasing this device; the operating system dropped its lock and it was taken without operator recovery.",
    }


def _busy_summary(resource: str, holder: JsonObject | None) -> str:
    if not holder:
        return f"Physical device {resource} is held by another Agentic HIL owner."
    label = holder.get("label")
    who = f"pid {holder.get('pid')} on {holder.get('host')} ({holder.get('frontend')})"
    if label:
        who = f"{who} running {label}"
    return f"Physical device {resource} is held by {who}."


def _age_seconds(timestamp: object) -> float | None:
    if not isinstance(timestamp, str) or not timestamp:
        return None
    try:
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return round((datetime.now(timezone.utc) - parsed).total_seconds(), 3)
