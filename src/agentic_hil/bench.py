"""Machine-wide exclusivity for one physical device.

Read access needs no permission any more (decision 0018), so the mutex carries
the protective load on its own: a run that holds a board exclusively cannot be
disturbed from outside, and an observer while no run holds the board disturbs
nobody. Two properties follow, and both are about *where* the lock lives rather
than what it locks.

**One place per machine.** The lock is keyed on the physical device
(``physical:<resource_id>``, ``probe:<serial>``, ``probe-exe:<executable>``,
``com:<device>``, ``can:<adapter>:<channel>``) and lives under the user's home, never under
``state_root``. On 2026-08-02 two sessions held the same Nucleo and could not
see each other because their ``state_root`` differed; a lock kept per
configuration is not a bench lock. There is deliberately no environment override
for the location: an override is how two sessions diverge again. A home is one
account's, though, and two accounts on one board diverge the same way (#777),
so on Linux a directory an administrator creates for a group,
``/var/lib/agentic-hil/device-locks``, takes the home's place for every account
whenever it exists, and one that exists and cannot be used refuses the lock
rather than send it home. Windows closes the same split without an
administrator (#885): every device lock there is also a named pipe beside the
file, a name that belongs to no account and that every account and session of
the machine sees (``windows_device_lock``). The file stays, because an older
version of the same account knows only the file.

**Crash safety without an operator.** Ownership is the OS advisory lock on
``<digest>.lock``, which the kernel drops when the holding process dies. A
killed run therefore frees its devices with no quarantine, no
``recover --confirm-safe-state``, and no waiting. The pipe is the kernel's in
the same way: its name goes with the holder's handle. The sibling
``<digest>.holder.json`` only *describes* the holder so a refusal can name it;
it is advisory, and taking the OS lock over a record that still says ``held``
is how this module detects that the previous owner died.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import secrets
import socket
import stat
import sys
import threading
import time
from collections.abc import Callable, Iterable
from contextlib import ExitStack, suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from agentic_hil import windows_device_lock
from agentic_hil.config import (
    ConfigError,
    _close_windows_handles,
    _open_directory_fd,
    _windows_hold_directory_chain,
    _windows_open_regular_file,
    atomic_write_text,
    safe_directory,
    safe_read_text,
)
from agentic_hil.knowledge import remediation_fields
from agentic_hil.redact import filesystem_error_detail
from agentic_hil.types import JsonObject, fold_device_path, fold_hardware_id

HOLDER_RECORD_VERSION = 1
BENCH_ROOT_NAME = ".agentic-hil"
DEVICE_LOCK_DIRECTORY_NAME = "device-locks"
# The directory every account of one group keeps its device locks in, where an
# administrator created it. Fixed like the home directory, for the same reason.
MACHINE_DEVICE_LOCK_ROOT = Path("/var/lib/agentic-hil/device-locks")
# What a lock file or record in it is created with, set explicitly: a umask of
# 077 would otherwise leave a file another account of the group cannot open.
MACHINE_LOCK_FILE_MODE = 0o660
MACHINE_DEVICE_LOCK_SETUP = (
    "sudo groupadd agentic-hil; sudo install -d -m 0755 /var/lib/agentic-hil; "
    "sudo install -d -o root -g agentic-hil -m 2770 /var/lib/agentic-hil/device-locks; "
    "sudo usermod -aG agentic-hil <account>"
)
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
PHYSICAL_RESOURCE_PREFIXES = ("physical:", "probe:", "probe-exe:", "com:", "can:")

_LOCAL_LOCKS: set[str] = set()
_LOCAL_SHARED_LOCKS: dict[str, int] = {}
_LOCAL_LOCKS_GUARD = threading.Lock()


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class _LifetimeLock:
    def __init__(self, path: Path, mode: int | None = None, *, shared: bool = False):
        self.path = path
        self.mode = mode
        self.shared = shared
        self.descriptor = -1
        self.directory_handles: list[int] = []
        self.locked = False
        self.local_key = os.path.normcase(str(path))
        self._registered = False

    def acquire(self) -> None:
        with _LOCAL_LOCKS_GUARD:
            readers = _LOCAL_SHARED_LOCKS.get(self.local_key, 0)
            if self._registered or self.local_key in _LOCAL_LOCKS or (readers and not self.shared):
                raise BlockingIOError(f"Lock is already held in this process: {self.path}")
            if self.shared:
                _LOCAL_SHARED_LOCKS[self.local_key] = readers + 1
            else:
                _LOCAL_LOCKS.add(self.local_key)
            self._registered = True
        try:
            if os.name == "nt":
                self.directory_handles = _windows_hold_directory_chain(self.path.parent)
                self.descriptor = _windows_open_regular_file(self.path, read=True, write=True, create=True)
            else:
                flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
                self.descriptor = os.open(self.path, flags, 0o600 if self.mode is None else self.mode)
            opened = os.fstat(self.descriptor)
            if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
                raise OSError("Coordination lock is not a single-link regular file.")
            if self.mode is not None and os.name != "nt" and opened.st_uid == os.geteuid() and stat.S_IMODE(opened.st_mode) != self.mode:
                os.fchmod(self.descriptor, self.mode)
            if os.name == "nt" and self.shared:
                _windows_shared_lock(self.descriptor, acquire=True)
                self.locked = True
            elif os.name == "nt":
                os.lseek(self.descriptor, 0, os.SEEK_SET)
                import msvcrt

                msvcrt.locking(self.descriptor, msvcrt.LK_NBLCK, 1)  # type: ignore[attr-defined]
                self.locked = True
            else:
                import fcntl

                operation = fcntl.LOCK_SH if self.shared else fcntl.LOCK_EX
                fcntl.flock(self.descriptor, operation | fcntl.LOCK_NB)
                self.locked = True
        except BaseException:
            self.release()
            raise

    def release(self) -> None:
        errors: list[BaseException] = []
        if self.descriptor >= 0:
            descriptor = self.descriptor
            try:
                if self.locked and os.name == "nt" and self.shared:
                    _windows_shared_lock(descriptor, acquire=False)
                elif self.locked and os.name == "nt":
                    import msvcrt

                    os.lseek(descriptor, 0, os.SEEK_SET)
                    msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)  # type: ignore[attr-defined]
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
            if self.descriptor < 0 and self._registered:
                with _LOCAL_LOCKS_GUARD:
                    if self.shared:
                        readers = _LOCAL_SHARED_LOCKS[self.local_key] - 1
                        if readers:
                            _LOCAL_SHARED_LOCKS[self.local_key] = readers
                        else:
                            del _LOCAL_SHARED_LOCKS[self.local_key]
                    else:
                        _LOCAL_LOCKS.discard(self.local_key)
                    self._registered = False
        if errors:
            raise errors[0]


def _windows_shared_lock(descriptor: int, *, acquire: bool) -> None:
    """Lock byte zero in shared mode, the same range as msvcrt's exclusive lock."""
    import ctypes
    import msvcrt
    from ctypes import wintypes

    class Overlapped(ctypes.Structure):
        _fields_ = [
            ("Internal", ctypes.c_size_t), ("InternalHigh", ctypes.c_size_t),
            ("Offset", wintypes.DWORD), ("OffsetHigh", wintypes.DWORD),
            ("hEvent", wintypes.HANDLE),
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    overlap = Overlapped()
    handle = msvcrt.get_osfhandle(descriptor)  # type: ignore[attr-defined]
    if acquire:
        operation = kernel.LockFileEx
        operation.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(Overlapped)]
        operation.restype = wintypes.BOOL
        ok = operation(handle, 1, 0, 1, 0, ctypes.byref(overlap))  # LOCKFILE_FAIL_IMMEDIATELY
    else:
        operation = kernel.UnlockFileEx
        operation.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(Overlapped)]
        operation.restype = wintypes.BOOL
        ok = operation(handle, 0, 1, 0, ctypes.byref(overlap))
    if not ok:
        raise ctypes.WinError(ctypes.get_last_error())  # type: ignore[attr-defined]


class DeviceBusyError(RuntimeError):
    """A declared device is held by somebody else, and the result names them."""

    def __init__(self, result: JsonObject):
        super().__init__(str(result.get("summary", "Physical device is held by another owner.")))
        self.result = result


class DeviceWaitStoppedError(RuntimeError):
    """The wait for a held device ended because the waiting run was asked to stop.

    Not a busy refusal, though it is raised from the same loop: the device may
    well have come free a moment later. The run was asked to end, and a run
    that holds nothing yet ends by taking nothing, which is what the result
    says: stopped, before any step, with nothing to release."""

    def __init__(self, result: JsonObject):
        super().__init__(str(result.get("summary", "The wait for a physical device was stopped on request.")))
        self.result = result


def is_physical_resource(resource: str) -> bool:
    return resource.startswith(PHYSICAL_RESOURCE_PREFIXES)


def fold_resource_name(resource: str) -> str:
    """Fold a resource name the way the device it names already folded it.

    ``Device.lock_key`` is where a lock key is built, and it folds the config
    field it was built from: an opaque hardware id one way and a host path the
    other (``types.fold_hardware_id``, ``types.fold_device_path``). A name that
    reaches the mutex without a device behind it never met that step, so
    ``com:COM9`` and ``com:com9`` became two lock files for one port and a
    caller that spelled its own key locked something no configured device ever
    locks. This is that same rule expressed over the finished name, applied
    where the lock is created rather than only where a key happens to be
    derived.

    Idempotent on every key ``Device.lock_key`` can produce, on Windows and on
    POSIX alike. That is the property being bought: folding a derived key
    changes nothing, so a hand-written spelling lands *on* the derived key
    instead of beside it.

    Names that are not device keys are returned untouched, deliberately.
    ``project:<digest>`` is a hex digest of a configuration and
    ``debugger-discovery:all`` names a host-wide enumeration; neither is a
    board, neither is reachable under a second spelling, and folding them would
    only suggest they were hardware."""
    # Opaque hardware ids, folded case on every platform. `com:serial:` is tried
    # before `com:` below because the longer prefix carries an adapter serial
    # while the shorter one carries a host path, and they fold by different
    # rules.
    for prefix in ("com:serial:", "physical:", "can:"):
        if resource.startswith(prefix):
            return prefix + fold_hardware_id(resource[len(prefix) :])
    # `probe:` is a probe serial or the `type` fallback: both hardware ids that
    # `DebuggerDevice.lock_key` builds with `fold_hardware_id`, which casefolds on
    # POSIX as well as on Windows. Folding it as a path instead left a
    # hand-written `probe:<SERIAL>` uppercase on POSIX while the configured
    # `probe_id` locked `probe:<serial>`, so two spellings of one probe bypassed
    # each other. The one exception is the legacy executable
    # spelling that predates `probe-exe:`: an executable is a host path, and a
    # debugger identified by one still holds `probe:<path>` beside its
    # `probe-exe:` key so a pre-upgrade process or a raw caller cannot take the
    # probe out from under it. A path and a serial are told apart by being an
    # absolute filesystem path, which a configured executable is (pinning makes it
    # one) and which a probe serial or a backend type name never is: a stricter
    # test than "contains a separator", so a serial that happened to carry one is
    # still a hardware id and the round-1 fix above stands.
    if resource.startswith("probe:"):
        value = resource[len("probe:") :]
        if _is_executable_path(value):
            return "probe:" + fold_device_path(value)
        return "probe:" + fold_hardware_id(value)
    # Host paths. `com:<device>` is a serial device name and `probe-exe:` a
    # debugger executable; the path rule leaves an opaque name alone on POSIX and
    # lowercases it on Windows, which is what `fold_device_path` already did to
    # it by the time it was a name. Folding case into a POSIX path would rewrite
    # `probe-exe:/usr/bin/Foo` to a name no configured debugger ever locks: the
    # same bug facing the other way, which is why the executable keeps a prefix
    # of its own rather than sharing `probe:`.
    for prefix in ("com:", "probe-exe:"):
        if resource.startswith(prefix):
            return prefix + fold_device_path(resource[len(prefix) :])
    return resource


def _is_executable_path(value: str) -> bool:
    """Whether a bare `probe:` value is the legacy executable spelling, not an id.

    The legacy `probe:<executable>` key is the only `probe:` name that is a
    host path, and a configured executable is always absolute: pinning
    resolves it to an absolute path before it is ever a lock key
    (`/usr/bin/openocd`, `C:\\Tools\\openocd.exe`). A probe serial and a
    backend type name are never absolute paths. Testing for an absolute path
    rather than for a separator is deliberate: a probe serial that happened to
    contain a `/` would be misread as a path by the looser test and split from
    the casefolded key its `probe_id` locks (the very split this fold exists
    to prevent, reintroduced), whereas it is not absolute and so stays a
    hardware id here.

    Rooted counts as absolute on purpose. Python 3.13 redefined
    ``ntpath.isabs`` so a drive-less rooted path (``\\opt\\tools\\openocd``,
    which is what ``os.path.normcase`` makes of a POSIX-style executable) stopped
    being absolute on Windows, so the same key folded as a path under 3.12 and
    as a hardware id under 3.13, and the legacy spelling stopped colliding with
    the ``probe-exe:`` holder on exactly one interpreter. A rooted value is
    still never a probe serial, so the wider reading reintroduces nothing."""
    return os.path.isabs(value) or value[:1] in ("/", "\\")


def physical_resources(resources: str | Iterable[object] | None) -> list[str]:
    if isinstance(resources, str):
        candidates: list[str] = [resources]
    else:
        candidates = [item for item in (resources or []) if isinstance(item, str)]
    return sorted({fold_resource_name(item) for item in candidates if is_physical_resource(item)})


def device_lock_root() -> Path:
    """Where every process on this machine looks for a device lock.

    ``~/.agentic-hil/device-locks``: fixed, with no configuration key and no
    environment override, because an override is how two sessions stop seeing
    each other. The home directory is chosen because every process of this user
    reaches it without having to agree on a configuration first. Where the
    machine directory exists, it is that instead, for every account alike. On
    Windows a hold taken here is also a named pipe, which is what another
    account sees.
    """
    machine = machine_device_lock_root()
    if machine is not None:
        return machine
    home = Path(os.path.expanduser("~"))
    return safe_directory(home / BENCH_ROOT_NAME / DEVICE_LOCK_DIRECTORY_NAME)


def machine_lock_root_applies() -> bool:
    """Linux alone: on macOS ``/var`` is a link, which the walk refuses, and
    Windows keeps its lock files under the profile and shares each hold through
    a named pipe instead (#885)."""
    return sys.platform.startswith("linux")


def machine_device_lock_root() -> Path | None:
    """The machine directory where it exists and can be used, None where it does not exist.

    Never created here: whether one group shares a board is the administrator's
    decision, made once with ``MACHINE_DEVICE_LOCK_SETUP``. One that exists and
    cannot be used is refused with ``device_lock_root_unusable``, never passed
    over for the home directory, because a lock kept there is a lock no other
    account sees, which is the failure the directory removes. The checks read
    mode bits and never owner or group ids: a rootless container sees the
    directory's group as an unmapped id, and the kernel still lets its members in.
    """
    if not machine_lock_root_applies():
        return None
    path = MACHINE_DEVICE_LOCK_ROOT
    try:
        status = os.lstat(path)
        parent = os.lstat(path.parent)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise _unusable_lock_root(path, f"cannot be looked at by this account ({error.strerror})") from None
    problem = _machine_lock_root_problem(path, status, parent)
    if problem is not None:
        raise _unusable_lock_root(path, problem)
    try:
        os.close(_open_directory_fd(path))
    except (ConfigError, OSError):
        raise _unusable_lock_root(path, "is reached through a link, and device locks are only kept where no link leads") from None
    return path


def _machine_lock_root_problem(path: Path, status: os.stat_result, parent: os.stat_result) -> str | None:
    mode = stat.S_IMODE(status.st_mode)
    if stat.S_ISLNK(parent.st_mode):
        return f"has a link for its parent {path.parent}, and device locks are only kept where no link leads"
    if stat.S_ISLNK(status.st_mode):
        return "is a link, and device locks are only kept where no link leads"
    if not stat.S_ISDIR(status.st_mode):
        return "is not a directory"
    if not mode & stat.S_ISGID:
        return "lacks the setgid bit, so a lock file one account creates in it would not carry the group the others open it through"
    if mode & 0o007:
        return f"grants access to others ({mode:04o}), and only the group's accounts may hold or read a device lock"
    if mode & 0o070 != 0o070:
        return f"does not give its group read, write and enter ({mode:04o})"
    if not os.access(path, os.R_OK | os.W_OK | os.X_OK):
        return "cannot be read, written and entered by this account: it is not in the directory's group, or it was added and has not logged in again"
    return None


def _unusable_lock_root(path: Path, problem: str) -> ConfigError:
    return ConfigError(
        "device_lock_root_unusable",
        f"The machine's device-lock directory {path} {problem}. Every device lock is refused until it is fixed: while it "
        "exists it is the one place every account keeps device locks, and a lock kept in a home directory instead is one "
        "no other account would see.",
        {"path": str(path), "problem": problem, "setup": MACHINE_DEVICE_LOCK_SETUP},
    )


def lock_file_mode(root: Path) -> int | None:
    """The mode a lock file or record under `root` is created with: the group's
    in the machine directory, and the default owner-only one anywhere else."""
    return MACHINE_LOCK_FILE_MODE if root == MACHINE_DEVICE_LOCK_ROOT else None


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
    # On Windows, the pipe every account sees beside the file only this account
    # sees (#885); None everywhere else.
    pipe: windows_device_lock.HeldPipe | None = None


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
        self._given_root = root
        self._root = root
        self._held: dict[str, _HeldDevice] = {}
        self._guard = threading.RLock()
        # The holder's own clock. A record was refreshed at each acquire and
        # never between them, so a run inside a long step (a delay, an expect
        # waiting out its timeout, a repeat block) took no lease for minutes and
        # read to a contender as a hung process once its last write aged past the
        # window. The hold is true for as long as this process holds the OS lock,
        # and the record has to say so for exactly that long: a thread refreshes
        # every held device on `HEARTBEAT_INTERVAL_S`, started with the first
        # hold and stopped with the last release. Best effort like `heartbeat`
        # itself: a refresh that cannot land drops nothing.
        self._pump: threading.Thread | None = None
        self._pump_stop = threading.Event()

    @property
    def root(self) -> Path:
        """Looked up again whenever this owner holds nothing, and kept while it
        holds anything: a server started before the machine directory existed
        moves over at its next acquire, and one owner's holds never straddle two
        directories."""
        with self._guard:
            if self._root is None or (self._given_root is None and not self._held):
                self._root = device_lock_root()
            return self._root

    def _lock(self, resource: str, root: Path) -> _LifetimeLock:
        return _LifetimeLock(root / f"{resource_digest(resource)}.lock", lock_file_mode(root))

    def held_resources(self) -> frozenset[str]:
        with self._guard:
            return frozenset(self._held)

    def holds(self, resource: str) -> bool:
        with self._guard:
            return resource in self._held

    def acquire(self, resources: list[str] | tuple[str, ...], *, wait_s: float = 0.0, stop_requested: Callable[[], bool] | None = None) -> list[str]:
        """Hold every named physical device, or hold none of them.

        Returns the resources this call newly took (an already-held device
        contributes nothing). Raises DeviceBusyError naming the holder, which is
        the whole point: silent waiting hides a collision, so a wait happens only
        when the caller asked for one and it is bounded.

        Each attempt takes the complete set or releases its tentative locks
        before waiting again. Existing holds keep their counts until a complete
        attempt succeeds, and their heartbeats continue during the wait.

        ``stop_requested`` is asked on every turn of the wait, and a wait it
        answers yes to ends at once with DeviceWaitStoppedError, everything this
        call had taken given back. It is how a run that is still waiting for its
        devices hears the cooperative stop: without it the stop file was read
        only between steps, and a run granted a long wait for a held bench had
        no step to be between for the whole of that wait."""
        wanted = physical_resources(resources)
        if not wanted:
            return []
        deadline = time.monotonic() + validated_wait(wait_s)
        return self._take(wanted, deadline, stop_requested, pipes=os.name == "nt")

    def acquire_named(self, resource: str, *, wait_s: float = 0.0, use_windows_pipe: bool = False) -> bool:
        """Hold one machine-wide lock by name, whether or not it is a device.

        `acquire` locks devices and skips everything else, because a run declares
        devices and the mutex's job is boards. A non-device name that still
        wants a machine-wide lock is the enumeration pseudo-resource
        `debugger-discovery:all`: it serialises probe enumeration across the
        machine rather than naming a board. Under a lease the coordinator locks it
        through its own per-resource lock beneath `state_root`; a bootstrap read
        has no `state_root`, so it takes the lock here, beside the device locks, so
        two first-time `init`s enumerate one at a time rather than over each other.
        Folded and taken through the same path as a device, so a caller cannot
        split it by spelling and the holder record names whoever holds it. Raises
        DeviceBusyError naming the holder, exactly as `acquire` does. Native
        picotool calls opt into the Windows pipe to share one invocation lock
        across user profiles."""
        deadline = time.monotonic() + validated_wait(wait_s)
        return bool(self._take([fold_resource_name(resource)], deadline, pipes=use_windows_pipe and os.name == "nt"))

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
            self._stop_heartbeat_pump()

    def _ensure_heartbeat_pump(self) -> None:
        """Start the refresh thread if it is not running. Called under `_guard`
        once a device is held; a thread that did not survive a fork is started
        again by the first hold the child takes."""
        if self._pump is not None and self._pump.is_alive():
            return
        self._pump_stop = threading.Event()
        pump = threading.Thread(target=self._pump_heartbeats, args=(self._pump_stop,), name="agentic-hil-heartbeat", daemon=True)
        try:
            pump.start()
        except RuntimeError:
            # A host that cannot start one more thread still holds the device
            # exclusively; what is lost is the freshness of the record, exactly
            # as when the record itself cannot be written. The event is set in
            # case the thread did launch before the failure was reported.
            self._pump_stop.set()
            return
        self._pump = pump

    def _pump_heartbeats(self, stop: threading.Event) -> None:
        # The interval is read on every wait rather than once, so the value a
        # record promises (`_write_holder` reads the same name) is the one the
        # refresh keeps to.
        while not stop.wait(HEARTBEAT_INTERVAL_S):
            self.heartbeat()

    def _stop_heartbeat_pump(self) -> None:
        """Called under `_guard`. The thread is only signalled, never joined: it
        takes `_guard` for its own refresh, and a join under it would wait on a
        thread waiting on us."""
        if self._pump is None:
            return
        self._pump_stop.set()
        self._pump = None

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

    def busy_result(
        self,
        resource: str,
        *,
        waited_s: float = 0.0,
        pipe: windows_device_lock.PipeTaken | None = None,
        observed_holder: JsonObject | None = None,
    ) -> JsonObject:
        """Describe contention, reusing an on-disk observation when the caller has one."""
        answer = None if pipe is None else windows_device_lock.ask(pipe.name)
        if answer is None:
            on_disk = observed_holder if observed_holder is not None else self._read_holder(resource) or {}
            # Only a record in state `held` says who holds the device. A released
            # one is what the last owner wrote as it let go, and the lock can be
            # held again while it is still on disk: before the new holder has
            # written its own, when that write failed, or under a lock that writes
            # none. Read as the holder, it named an owner that is gone, with its
            # old timestamps and a heartbeat that stopped when it let go.
            record = on_disk if on_disk.get("state") == "held" else {}
            holder = record.get("owner") if isinstance(record.get("owner"), dict) else None
        else:
            # The file was free and the pipe was not (#885): the holder is
            # another account, or a process whose lock root lies elsewhere. What
            # its pipe answers now is the evidence, never a record this account
            # wrote here earlier. The pid is the kernel's; the rest is the
            # holder's own statement.
            on_disk = {}
            record = answer.record or {}
            owner = record.get("owner")
            claimed = owner if isinstance(owner, dict) else {}
            holder = None if answer.pid is None else {**claimed, "pid": answer.pid}
        result: JsonObject = {
            "ok": False,
            "error_type": "device_busy",
            "summary": _busy_summary(resource, holder, released=on_disk.get("state") == "released"),
            "resource": resource,
            "retry_safe": True,
            "side_effect_committed": False,
            "held_since": record.get("acquired_at"),
            "heartbeat_at": record.get("heartbeat_at"),
        }
        if holder is not None:
            result["holder"] = holder
            if answer is None:
                # A pid names a process only within one PID namespace and one boot,
                # and the record comes from whichever process holds the lock, in a
                # directory another container or host may share. Whether this
                # process holds the lock is answered by its own lock table, which
                # no other process writes; the pid keeps the flag off a record some
                # earlier holder left behind and off a forked child, which inherits
                # the table.
                lock_key = _LifetimeLock(self.root / f"{resource_digest(resource)}.lock").local_key
                with _LOCAL_LOCKS_GUARD:
                    held_here = lock_key in _LOCAL_LOCKS
            else:
                # The kernel read this pid off the pipe: it is the process that holds it.
                held_here = True
            if held_here and holder.get("pid") == os.getpid():
                result["holder_is_this_process"] = True
        age = _age_seconds(record.get("heartbeat_at"))
        if age is not None:
            result["heartbeat_age_s"] = age
            interval = record.get("heartbeat_interval_s")
            if isinstance(interval, (int, float)) and age > max(float(interval) * 4, 60.0):
                result["holder_heartbeat_stale"] = True
        if answer is not None and answer.stale:
            # Connected, still held, and silent past the answer deadline: the
            # pipe's form of a heartbeat that stopped.
            result["holder_heartbeat_stale"] = True
        if waited_s > 0:
            result["waited_s"] = round(waited_s, 3)
        # The catalogue's steps, merged where the refusal is built, so every path
        # that forwards it whole (a bare hardware call, a run, a session start)
        # hands them out, not only the plan run that used to add them (#662).
        result.update(remediation_fields("device_busy"))
        return result

    def _take(self, resources: list[str], deadline: float, stop_requested: Callable[[], bool] | None = None, *, pipes: bool = False) -> list[str]:
        """Retry complete sets, publishing ownership only after all locks are ours.

        With `pipes` (devices on Windows, #885) each device is the file and then
        its pipe, and a pipe that cannot be created gives its file back with the
        rest of the attempt: busy, never free."""
        started = time.monotonic()
        while True:
            with self._guard:
                # Pin one root for this attempt, even while _held is empty.
                root = self.root
                locks: dict[str, _LifetimeLock] = {}
                held_pipes: dict[str, windows_device_lock.HeldPipe] = {}
                failure: tuple[str, OSError] | None = None
                with ExitStack() as rollback:
                    for resource in resources:
                        if resource in self._held:
                            continue
                        lock = self._lock(resource, root)
                        try:
                            lock.acquire()
                        except OSError as error:
                            failure = (resource, error)
                            break
                        rollback.callback(lock.release)
                        locks[resource] = lock
                        if not pipes:
                            continue
                        # After the file and given back before it, so a contender
                        # of this account still meets the file first. Silent until
                        # the whole set is ours: a reader of a tentative pipe sees
                        # it close under it on rollback, busy and never stale.
                        try:
                            pipe = windows_device_lock.take(resource_digest(resource), root)
                        except windows_device_lock.PipeTaken as error:
                            failure = (resource, error)
                            break
                        rollback.callback(pipe.close)
                        held_pipes[resource] = pipe
                    if failure is None:
                        # Read reclamation evidence only after the whole set is
                        # secured. A failed attempt must not overwrite a dead
                        # owner's record with its own held/released pair.
                        taken: dict[str, _HeldDevice] = {}
                        for resource, lock in locks.items():
                            previous = self._read_holder(resource, lock.path.parent)
                            reclaimed = previous if isinstance(previous, dict) and previous.get("state") == "held" else None
                            taken[resource] = _HeldDevice(
                                resource=resource,
                                lock=lock,
                                acquired_at=utc_now_iso(),
                                reclaimed_from=_reclaim_detail(reclaimed),
                                pipe=held_pipes.get(resource),
                            )
                        for resource in resources:
                            if resource in self._held:
                                self._held[resource].holders += 1
                        self._held.update(taken)
                        rollback.pop_all()
                        try:
                            # Unwritable advisory records do not drop OS locks.
                            for resource in taken:
                                with suppress(ConfigError, OSError):
                                    self._write_holder(resource, "held")
                            if taken:
                                self._ensure_heartbeat_pump()
                        except BaseException:
                            for resource in reversed(resources):
                                self._drop(resource)
                            raise
                        return list(taken)
                # ExitStack released every tentative lock without touching the
                # holder records, _held, existing counts or the heartbeat pump.
            resource, blocked_error = failure
            # A cooperative stop wins over expiry, as on the single-device path.
            if stop_requested is not None and stop_requested():
                raise DeviceWaitStoppedError(
                    {
                        "ok": False,
                        "error_type": "run_stopped",
                        "stopped": True,
                        "stopped_after_step": 0,
                        "summary": f"A stop was requested while this run waited for {resource}, so it ended before taking any device.",
                        "resource": resource,
                        "waited_s": round(time.monotonic() - started, 3),
                        "retry_safe": True,
                        "side_effect_committed": False,
                    }
                ) from blocked_error
            if time.monotonic() >= deadline:
                pipe_taken = blocked_error if isinstance(blocked_error, windows_device_lock.PipeTaken) else None
                result = self.busy_result(resource, waited_s=time.monotonic() - started, pipe=pipe_taken)
                if not isinstance(blocked_error, BlockingIOError):
                    result.update(filesystem_error_detail(blocked_error))
                raise DeviceBusyError(result) from blocked_error
            # Heartbeats and releases need _guard while this call waits.
            time.sleep(min(WAIT_POLL_INTERVAL_S, max(deadline - time.monotonic(), 0.0)))

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
        try:
            # The pipe before the file, the reverse of taking them; the file is
            # given back even when the pipe's cleanup fails.
            if held.pipe is not None:
                held.pipe.close()
        finally:
            held.lock.release()
        if not self._held:
            self._stop_heartbeat_pump()

    def _holder_path(self, resource: str, root: Path | None = None) -> Path:
        return (self.root if root is None else root) / f"{resource_digest(resource)}.holder.json"

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
        if held is not None and held.pipe is not None and state == "held":
            # What the pipe answers every account from now on, the owner's label
            # included (#885). Handed over before the write, so a record the disk
            # refuses still reaches a contender of another account.
            held.pipe.publish(record)
        # Beside the lock it describes, which is where a contender reads it.
        root = held.lock.path.parent if held is not None else self.root
        atomic_write_text(self._holder_path(resource, root), json.dumps(record, indent=2) + "\n", mode=lock_file_mode(root))

    def _read_holder(self, resource: str, root: Path | None = None) -> JsonObject | None:
        # A heartbeat atomically replaces the record. If it lands between open
        # and stat, the safe reader refuses the changed inode. Retry the full
        # safety check, bounded so a persistently unsafe path still yields no
        # holder and never keeps a contender waiting indefinitely.
        path = self._holder_path(resource, root)
        for _ in range(3):
            try:
                text = safe_read_text(path)
                break
            except ConfigError as error:
                if error.error_type != "unsafe_configured_path":
                    return None
            except (OSError, UnicodeDecodeError):
                return None
        else:
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


def validated_wait(wait_s: object) -> float:
    """The wait a caller asked for, as a number of seconds, or a named refusal.

    Public because the MCP surface has to reach it *before* it converts: a
    frontend that ran `float()` over the payload first handed this a value that
    had already lost the distinction it makes: `float(True)` is `1.0`, and a
    wait nobody could have meant became a one-second wait instead of an
    `invalid_argument`. Taking the raw object is the whole contract.
    """
    if wait_s is None or wait_s is False:
        return 0.0
    if isinstance(wait_s, bool) or not isinstance(wait_s, (int, float)):
        raise ConfigError("invalid_argument", "A device wait must be a number of seconds.", {"field": "wait_s", "value": wait_s})
    value = float(wait_s)
    # NaN slips past both range checks below (every comparison against it is
    # false), so a NaN wait would become a NaN deadline that `time.monotonic() >=
    # deadline` never reaches, and the holder of a device would be polled forever.
    # Infinity is caught by the upper bound already, but reject every non-finite
    # value here so the one validator is the single gate: a wait must be a real,
    # bounded number of seconds or it is refused before it can strand a worker.
    if not math.isfinite(value):
        raise ConfigError("invalid_argument", "A device wait must be a finite number of seconds.", {"field": "wait_s", "value": wait_s})
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


def _busy_summary(resource: str, holder: JsonObject | None, *, released: bool = False) -> str:
    if not holder:
        if released:
            return f"Physical device {resource} is held, but the current holder is not known: the record beside its lock is marked released and names nobody who holds it now."
        return f"Physical device {resource} is held by another Agentic HIL owner."
    label = holder.get("label")
    # A pipe that kept silent names its holder by the kernel's pid alone (#885).
    who = f"pid {holder.get('pid')}"
    if holder.get("host"):
        who = f"{who} on {holder.get('host')}"
    if holder.get("frontend"):
        who = f"{who} ({holder.get('frontend')})"
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
