"""Windows named-pipe ownership for device locks (#885).

On Windows a device lock is two locks, both taken without blocking: the file in
the profile, as everywhere else, and then a name in the kernel's pipe
namespace. A profile is one account's, so two accounts on one board never met
in a file. A pipe name belongs to no account: every account and every session
sees it, a service in session 0 included, the first process to create it is
the only one that gets it, and it is gone the moment that process closes it or
dies. Creating one needs no privilege and no setup.

`BenchMutex` takes the pipe right after the file and gives it back before the
file, so a contender of the same account still meets the file first and keeps
the file's diagnostics. Only a contender whose own file was free reads the
pipe: another account, or a process whose lock root is somewhere else.

The pipe also answers. Once the whole device set is taken (#881) a thread of the
holder hands whoever connects the holder record as JSON, stamped with the time
of the answer, which is the heartbeat. It answers from a copy and never takes
the mutex's `_guard`. A contender takes only the pid as fact, and from the
kernel (`GetNamedPipeServerProcessId`); every other field is the holder's own
statement, passed on the way a file record is.
"""
from __future__ import annotations

import functools
import hashlib
import json
import os
import threading
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

# How long a contender waits for a connected holder to answer before it reports
# the holder stale. Read at every question rather than once.
ANSWER_TIMEOUT_S = 3.0
PIPE_NAME_PREFIX = "\\\\.\\pipe\\agentic-hil-"
# The largest answer either side handles: the holder's pipe buffers, and the
# most a contender reads before it gives up on what it was sent.
MAX_ANSWER_BYTES = 65536
# How long the holder keeps a reader connected after answering it. A reader that
# neither takes its answer nor closes would otherwise keep every other reader
# out for as long as it liked; it keeps nobody out of the device either way.
READER_LINGER_S = 10.0
# How long a release waits for the serving thread to leave. The thread is woken
# by an event of its own and leaves at once; the bound only keeps a release from
# hanging on a thread that did not.
STOP_JOIN_S = 5.0
SERVE_THREAD_NAME = "agentic-hil-device-pipe"
_POLL_S = 0.01
_RETRY_MS = 200

_PIPE_ACCESS_DUPLEX = 0x00000003
_FILE_FLAG_FIRST_PIPE_INSTANCE = 0x00080000
_FILE_FLAG_OVERLAPPED = 0x40000000
_PIPE_TYPE_MESSAGE = 0x00000004
_PIPE_READMODE_MESSAGE = 0x00000002
_PIPE_REJECT_REMOTE_CLIENTS = 0x00000008
_GENERIC_READ = 0x80000000
_OPEN_EXISTING = 3
_SECURITY_SQOS_PRESENT = 0x00100000
_SECURITY_IDENTIFICATION = 0x00010000
_TOKEN_QUERY = 0x0008
_TOKEN_USER = 1
_SDDL_REVISION_1 = 1
_INFINITE = 0xFFFFFFFF
_WAIT_OBJECT_0 = 0
_WAIT_TIMEOUT = 0x102
_ERROR_MORE_DATA = 234
_ERROR_PIPE_BUSY = 231
_ERROR_NO_DATA = 232
_ERROR_PIPE_CONNECTED = 535
_ERROR_IO_PENDING = 997


class PipeTaken(BlockingIOError):
    """The pipe a device lock needs could not be created.

    Another process holds the name, or the name could not be made at all. A
    BlockingIOError like the file lock's, so the refusal built from it is the
    plain `device_busy` and never a filesystem fault: every failure to create
    the pipe means busy, never free. `name` is None when no name was made, and
    then nobody can be asked who holds it."""

    def __init__(self, name: str | None):
        super().__init__(f"The device-lock pipe {name or '(no name)'} is held by another process.")
        self.name = name


@dataclass(frozen=True)
class HolderAnswer:
    """What a contender learned from a held pipe.

    `pid` is the kernel's, read off the connection, and the one field taken as
    fact. `record` is what the holder said, when it said it in time. `stale` is
    a holder that kept the pipe and the connection and said nothing by the
    deadline. Nothing at all is a holder nobody can name."""

    pid: int | None = None
    record: dict[str, Any] | None = None
    stale: bool = False


def profile_device_lock_root() -> Path:
    """Where this process's account keeps its device locks, asked of the operating system.

    Read off the process token, never off HOME or USERPROFILE: those say where
    this process was told its home is, and a lock root they redirect is shared
    with no other account, so it must not take the names every account looks
    for."""
    import ctypes
    from ctypes import wintypes

    # Imported here: the mutex imports this module, and these two names are the
    # ones it appends to the home directory.
    from agentic_hil.bench import BENCH_ROOT_NAME, DEVICE_LOCK_DIRECTORY_NAME

    api = _api()
    with _process_token() as token:
        size = wintypes.DWORD(0)
        api.GetUserProfileDirectoryW(token, None, ctypes.byref(size))
        buffer = ctypes.create_unicode_buffer(max(size.value, 1))
        if not api.GetUserProfileDirectoryW(token, buffer, ctypes.byref(size)):
            raise _windows_error()
    return Path(buffer.value) / BENCH_ROOT_NAME / DEVICE_LOCK_DIRECTORY_NAME


def pipe_name(resource_digest: str, lock_root: Path) -> str:
    """The pipe a device lock under `lock_root` takes, for the digest of its folded resource name.

    Under this account's own profile root it is `agentic-hil-<digest>` in the
    machine's pipe namespace: the name every account and session of the machine
    looks for, with the digest that names the `<digest>.lock` file beside it and
    nothing of the account, the session or the package. Any other root, a
    test's sandboxed home or a process whose HOME points elsewhere, adds a
    digest of that root. One root shares its names and two roots do not, so a
    redirected home stays apart from the machine's own locks, as its files
    always were."""
    root = _normalized(lock_root)
    if root == _normalized(profile_device_lock_root()):
        return PIPE_NAME_PREFIX + resource_digest
    return f"{PIPE_NAME_PREFIX}{resource_digest}-{hashlib.sha256(root.encode('utf-8')).hexdigest()[:32]}"


def take(resource_digest: str, lock_root: Path) -> HeldPipe:
    """Create one device's pipe as its first and only instance, or raise PipeTaken."""
    name: str | None = None
    try:
        name = pipe_name(resource_digest, lock_root)
        return HeldPipe(name, _create(name))
    except OSError as error:
        raise PipeTaken(name) from error


class HeldPipe:
    """One device's pipe: the handle that owns the name, and the thread that answers on it.

    `take` creates it silent. It answers nothing until `publish` hands it the
    record of a completed set (#881), so a reader that connects to a tentative
    hold meets a pipe that is held and says nothing, and the rollback of that
    attempt closes the pipe under it: a tentative hold reads as busy, never as
    stale. Publishing again replaces the copy the thread answers from, which the
    thread reads without any lock of the mutex's."""

    def __init__(self, name: str, handle: int):
        self.name = name
        self._handle = handle
        self._record: str | None = None
        self._stop: int | None = None
        self._thread: threading.Thread | None = None
        self._closed = False

    def publish(self, record: Mapping[str, object]) -> None:
        """Answer every reader with `record` from now on, and start serving if this is the first."""
        # `reclaimed_from` is reported to the same account alone, which reads it
        # from the file; the pipe answers every account.
        self._record = json.dumps({key: value for key, value in record.items() if key != "reclaimed_from"})
        if self._thread is None and not self._closed:
            self._serve_in_background()

    def close(self) -> None:
        """Stop answering, cancel what is pending, and give the name back.

        Depends on no reader finishing and on no lock: the thread is woken by
        its own event, its I/O is cancelled, and the handle is closed whether or
        not the thread has left in time. A reader that is still connected keeps
        nothing; the name is free once the holder's handle is closed."""
        if self._closed:
            return
        self._closed = True
        api = _api()
        if self._stop is not None:
            api.SetEvent(self._stop)
            api.CancelIoEx(self._handle, None)
        if self._thread is not None:
            self._thread.join(timeout=STOP_JOIN_S)
        error = None if api.CloseHandle(self._handle) else _windows_error()
        if self._stop is not None:
            api.CloseHandle(self._stop)
        if error is not None:
            raise error

    def _serve_in_background(self) -> None:
        api = _api()
        stop = api.CreateEventW(None, True, False, None)
        if not stop:
            return
        thread = threading.Thread(target=self._serve, args=(int(stop),), name=SERVE_THREAD_NAME, daemon=True)
        try:
            thread.start()
        except RuntimeError:
            # A host that cannot start one more thread still holds the name.
            # What is lost is the answer, as when the heartbeat thread cannot
            # start: a reader reports the holder stale, never the device free.
            api.CloseHandle(stop)
            return
        self._stop, self._thread = int(stop), thread

    def _serve(self, stop: int) -> None:
        api = _api()
        event = api.CreateEventW(None, True, False, None)
        if not event:
            return
        overlapped = api.Overlapped()
        overlapped.hEvent = event
        try:
            while self._listen(api, overlapped, stop):
                self._answer(api, overlapped, stop)
                api.DisconnectNamedPipe(self._handle)
        finally:
            api.CloseHandle(event)

    def _listen(self, api: Any, overlapped: Any, stop: int) -> bool:
        """Wait for a reader: True once one is connected, False once serving ends."""
        import ctypes

        while not _signalled(api, stop):
            api.ResetEvent(overlapped.hEvent)
            if api.ConnectNamedPipe(self._handle, ctypes.byref(overlapped)):
                return True
            code = _last_error()
            if code == _ERROR_IO_PENDING:
                if not _complete(api, self._handle, overlapped, stop, _INFINITE):
                    return False
                code = _overlapped_error(api, self._handle, overlapped)
                if code == 0:
                    return True
            # A reader that connected before the holder listened, the one already
            # waiting when the completed set was published among them.
            if code == _ERROR_PIPE_CONNECTED:
                return True
            if code == _ERROR_NO_DATA:
                # A reader that came and went before the holder listened.
                api.DisconnectNamedPipe(self._handle)
                continue
            # The handle cannot listen just now. The hold stands; try again.
            if api.WaitForSingleObject(stop, _RETRY_MS) != _WAIT_TIMEOUT:
                return False
        return False

    def _answer(self, api: Any, overlapped: Any, stop: int) -> None:
        """Hand the connected reader the record, stamped now, and give it a while to take it and go."""
        import ctypes

        text = self._record
        if text is None:
            return
        record = json.loads(text)
        record["heartbeat_at"] = _answer_time()
        payload = json.dumps(record).encode("utf-8")
        linger_ms = int(READER_LINGER_S * 1000)
        api.ResetEvent(overlapped.hEvent)
        if not api.WriteFile(self._handle, payload, len(payload), None, ctypes.byref(overlapped)) and (
            _last_error() != _ERROR_IO_PENDING or not _complete(api, self._handle, overlapped, stop, linger_ms)
        ):
            return
        # A reader writes nothing, so this read ends when it closes its end. A
        # disconnect before that would throw away an answer it has not read yet.
        received = ctypes.create_string_buffer(1)
        api.ResetEvent(overlapped.hEvent)
        if not api.ReadFile(self._handle, received, 1, None, ctypes.byref(overlapped)) and _last_error() == _ERROR_IO_PENDING:
            _complete(api, self._handle, overlapped, stop, linger_ms)


def ask(name: str | None) -> HolderAnswer:
    """Ask the holder of a pipe who it is, bounded by `ANSWER_TIMEOUT_S`.

    Opened read-only and at identification level, so a pipe somebody created
    ahead of time cannot act in this process's name. A pipe that breaks or
    disconnects before it answers, one another reader keeps busy until the
    deadline, one that cannot be opened, and one whose server pid cannot be read
    all name nobody: busy, never stale and never free."""
    if name is None:
        return HolderAnswer()
    deadline = time.monotonic() + ANSWER_TIMEOUT_S
    while True:
        try:
            handle = _open_reader(name)
            break
        except OSError as error:
            if getattr(error, "winerror", None) != _ERROR_PIPE_BUSY or time.monotonic() >= deadline:
                return HolderAnswer()
        time.sleep(_POLL_S)
    try:
        return _read_answer(handle, deadline)
    finally:
        _api().CloseHandle(handle)


def _open_reader(name: str) -> int:
    """A read-only handle on the pipe `name`, opened at identification level."""
    api = _api()
    handle = api.CreateFileW(name, _GENERIC_READ, 0, None, _OPEN_EXISTING, _SECURITY_SQOS_PRESENT | _SECURITY_IDENTIFICATION, None)
    if handle is None or handle == api.invalid_handle:
        raise _windows_error()
    return int(handle)


def _read_answer(handle: int, deadline: float) -> HolderAnswer:
    import ctypes
    from ctypes import wintypes

    api = _api()
    server = wintypes.ULONG()
    if not api.GetNamedPipeServerProcessId(handle, ctypes.byref(server)):
        return HolderAnswer()
    pid = int(server.value)
    received = bytearray()
    while True:
        available = wintypes.DWORD()
        if not api.PeekNamedPipe(handle, None, 0, None, ctypes.byref(available), None):
            # Broken or disconnected before an answer: a rolled-back attempt, or
            # a holder that let go while this was asking.
            return HolderAnswer()
        if available.value:
            room = MAX_ANSWER_BYTES - len(received)
            if room <= 0:
                return HolderAnswer()
            chunk = ctypes.create_string_buffer(min(available.value, room))
            count = wintypes.DWORD()
            if not api.ReadFile(handle, chunk, len(chunk), ctypes.byref(count), None) and _last_error() != _ERROR_MORE_DATA:
                return HolderAnswer()
            received.extend(chunk.raw[: count.value])
            try:
                record = json.loads(received)
            except ValueError:
                continue
            return HolderAnswer(pid, record) if isinstance(record, dict) else HolderAnswer()
        if time.monotonic() >= deadline:
            return HolderAnswer(pid, stale=True)
        time.sleep(_POLL_S)


def _create(name: str) -> int:
    """The pipe's handle, created as the first and only instance of `name`.

    One instance, remote clients refused, not inherited by any child, so a
    debugger the holder started cannot keep the name after the holder died.
    Full control for the holder's own account and read for Authenticated Users:
    reading does not include creating an instance, so nobody can attach a second
    one and answer in the holder's name. The account is the token's user, not
    its default owner, which is Administrators for an elevated process."""
    import ctypes

    api = _api()
    descriptor = ctypes.c_void_p()
    rules = f"D:P(A;;FA;;;{_token_user_sid()})(A;;FR;;;AU)"
    if not api.ConvertStringSecurityDescriptorToSecurityDescriptorW(rules, _SDDL_REVISION_1, ctypes.byref(descriptor), None):
        raise _windows_error()
    try:
        attributes = api.SecurityAttributes(ctypes.sizeof(api.SecurityAttributes), descriptor.value, False)
        handle = api.CreateNamedPipeW(
            name,
            _PIPE_ACCESS_DUPLEX | _FILE_FLAG_FIRST_PIPE_INSTANCE | _FILE_FLAG_OVERLAPPED,
            _PIPE_TYPE_MESSAGE | _PIPE_READMODE_MESSAGE | _PIPE_REJECT_REMOTE_CLIENTS,
            1,
            MAX_ANSWER_BYTES,
            MAX_ANSWER_BYTES,
            0,
            ctypes.byref(attributes),
        )
        if handle is None or handle == api.invalid_handle:
            raise _windows_error()
        return int(handle)
    finally:
        api.LocalFree(descriptor)


@functools.cache
def _token_user_sid() -> str:
    import ctypes
    from ctypes import wintypes

    api = _api()
    with _process_token() as token:
        size = wintypes.DWORD(0)
        api.GetTokenInformation(token, _TOKEN_USER, None, 0, ctypes.byref(size))
        buffer = ctypes.create_string_buffer(max(size.value, 1))
        if not api.GetTokenInformation(token, _TOKEN_USER, buffer, len(buffer), ctypes.byref(size)):
            raise _windows_error()
        # TOKEN_USER begins with the SID's pointer.
        sid = ctypes.c_void_p.from_buffer(buffer).value
        text = ctypes.c_void_p()
        if not api.ConvertSidToStringSidW(sid, ctypes.byref(text)) or not text.value:
            raise _windows_error()
        try:
            return ctypes.wstring_at(text.value)
        finally:
            api.LocalFree(text)


@contextmanager
def _process_token() -> Iterator[Any]:
    import ctypes
    from ctypes import wintypes

    api = _api()
    token = wintypes.HANDLE()
    if not api.OpenProcessToken(api.GetCurrentProcess(), _TOKEN_QUERY, ctypes.byref(token)):
        raise _windows_error()
    try:
        yield token
    finally:
        api.CloseHandle(token)


def _complete(api: Any, handle: int, overlapped: Any, stop: int, timeout_ms: int) -> bool:
    """Wait for the operation pending on `overlapped`: True once it completed, False once it was cancelled.

    Cancelled when `stop` is set or the time is up. Either way it has finished by
    the time this returns, so its buffer and its event may go."""
    import ctypes
    from ctypes import wintypes

    waited = (wintypes.HANDLE * 2)(overlapped.hEvent, stop)
    if api.WaitForMultipleObjects(2, waited, False, timeout_ms) == _WAIT_OBJECT_0:
        return True
    api.CancelIoEx(handle, ctypes.byref(overlapped))
    transferred = wintypes.DWORD()
    api.GetOverlappedResult(handle, ctypes.byref(overlapped), ctypes.byref(transferred), True)
    return False


def _overlapped_error(api: Any, handle: int, overlapped: Any) -> int:
    """The finished operation's error code, 0 when it succeeded."""
    import ctypes
    from ctypes import wintypes

    transferred = wintypes.DWORD()
    if api.GetOverlappedResult(handle, ctypes.byref(overlapped), ctypes.byref(transferred), False):
        return 0
    return _last_error()


def _signalled(api: Any, event: int) -> bool:
    """Whether `event` is set. A wait that fails counts as set: the holder is letting go."""
    return bool(api.WaitForSingleObject(event, 0) != _WAIT_TIMEOUT)


def _answer_time() -> str:
    # The holder record's own format, read off the clock at the answer.
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _normalized(path: Path) -> str:
    return os.path.normcase(os.path.realpath(os.fspath(path)))


def _last_error() -> int:
    import ctypes

    return int(ctypes.get_last_error())  # type: ignore[attr-defined]


def _windows_error() -> OSError:
    import ctypes

    error: OSError = ctypes.WinError(_last_error())  # type: ignore[attr-defined]
    return error


@functools.cache
def _api() -> Any:
    """The Win32 calls this module makes, bound once with their signatures."""
    import ctypes
    from ctypes import wintypes

    class SecurityAttributes(ctypes.Structure):
        _fields_ = [("nLength", wintypes.DWORD), ("lpSecurityDescriptor", wintypes.LPVOID), ("bInheritHandle", wintypes.BOOL)]

    class Overlapped(ctypes.Structure):
        _fields_ = [
            ("Internal", ctypes.c_void_p),
            ("InternalHigh", ctypes.c_void_p),
            ("Offset", wintypes.DWORD),
            ("OffsetHigh", wintypes.DWORD),
            ("hEvent", wintypes.HANDLE),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)  # type: ignore[attr-defined]
    userenv = ctypes.WinDLL("userenv", use_last_error=True)  # type: ignore[attr-defined]
    handle, dword, boolean, pointer = wintypes.HANDLE, wintypes.DWORD, wintypes.BOOL, ctypes.c_void_p
    dwords = ctypes.POINTER(wintypes.DWORD)
    overlapped = ctypes.POINTER(Overlapped)

    def bind(library: Any, name: str, arguments: list[Any], result: Any) -> Any:
        function = getattr(library, name)
        function.argtypes = arguments
        function.restype = result
        return function

    return SimpleNamespace(
        SecurityAttributes=SecurityAttributes,
        Overlapped=Overlapped,
        invalid_handle=ctypes.c_void_p(-1).value,
        CreateNamedPipeW=bind(
            kernel32, "CreateNamedPipeW", [wintypes.LPCWSTR, dword, dword, dword, dword, dword, dword, ctypes.POINTER(SecurityAttributes)], handle
        ),
        ConnectNamedPipe=bind(kernel32, "ConnectNamedPipe", [handle, overlapped], boolean),
        DisconnectNamedPipe=bind(kernel32, "DisconnectNamedPipe", [handle], boolean),
        CreateFileW=bind(kernel32, "CreateFileW", [wintypes.LPCWSTR, dword, dword, pointer, dword, dword, handle], handle),
        ReadFile=bind(kernel32, "ReadFile", [handle, pointer, dword, dwords, overlapped], boolean),
        WriteFile=bind(kernel32, "WriteFile", [handle, pointer, dword, dwords, overlapped], boolean),
        PeekNamedPipe=bind(kernel32, "PeekNamedPipe", [handle, pointer, dword, dwords, dwords, dwords], boolean),
        GetNamedPipeServerProcessId=bind(kernel32, "GetNamedPipeServerProcessId", [handle, ctypes.POINTER(wintypes.ULONG)], boolean),
        GetOverlappedResult=bind(kernel32, "GetOverlappedResult", [handle, overlapped, dwords, boolean], boolean),
        CancelIoEx=bind(kernel32, "CancelIoEx", [handle, overlapped], boolean),
        CreateEventW=bind(kernel32, "CreateEventW", [pointer, boolean, boolean, wintypes.LPCWSTR], handle),
        SetEvent=bind(kernel32, "SetEvent", [handle], boolean),
        ResetEvent=bind(kernel32, "ResetEvent", [handle], boolean),
        WaitForSingleObject=bind(kernel32, "WaitForSingleObject", [handle, dword], dword),
        WaitForMultipleObjects=bind(kernel32, "WaitForMultipleObjects", [dword, ctypes.POINTER(handle), boolean, dword], dword),
        CloseHandle=bind(kernel32, "CloseHandle", [handle], boolean),
        GetCurrentProcess=bind(kernel32, "GetCurrentProcess", [], handle),
        LocalFree=bind(kernel32, "LocalFree", [pointer], pointer),
        OpenProcessToken=bind(advapi32, "OpenProcessToken", [handle, dword, ctypes.POINTER(handle)], boolean),
        GetTokenInformation=bind(advapi32, "GetTokenInformation", [handle, ctypes.c_int, pointer, dword, dwords], boolean),
        ConvertSidToStringSidW=bind(advapi32, "ConvertSidToStringSidW", [pointer, ctypes.POINTER(pointer)], boolean),
        ConvertStringSecurityDescriptorToSecurityDescriptorW=bind(
            advapi32, "ConvertStringSecurityDescriptorToSecurityDescriptorW", [wintypes.LPCWSTR, dword, ctypes.POINTER(pointer), dwords], boolean
        ),
        GetUserProfileDirectoryW=bind(userenv, "GetUserProfileDirectoryW", [handle, pointer, dwords], boolean),
    )
