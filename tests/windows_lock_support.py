"""Native peers and handshakes for the Windows device-lock tests.

Raw peers deliberately own only a pipe, never a product device lock. Product
peers use BenchMutex and substitute only its private pipe-name function so two
isolated file roots can meet at the endpoint supplied by their parent.
"""

from __future__ import annotations

import ctypes
import json
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from ctypes import wintypes
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from support import process_is_running, publish_atomically, published, read_when_published, scaled_time_bound

GENERIC_READ = 0x80000000
FILE_ALL_ACCESS = 0x001F01FF
FILE_GENERIC_READ = 0x00120089
FILE_CREATE_PIPE_INSTANCE = 0x00000004
READ_CONTROL = 0x00020000
IDENTIFICATION = 0x00100000 | 0x00010000


class NativePipe:
    """Small independent Win32 client/raw-peer adapter, with lazy DLL loading."""

    def __init__(self) -> None:
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.security = ctypes.WinDLL("advapi32", use_last_error=True)
        pointer = ctypes.c_void_p
        dword_pointer = ctypes.POINTER(wintypes.DWORD)
        self.bind(self.kernel.CreateNamedPipeW, [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                               wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
                                               wintypes.DWORD, pointer], wintypes.HANDLE)
        self.bind(self.kernel.CreateFileW, [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                          pointer, wintypes.DWORD, wintypes.DWORD,
                                          wintypes.HANDLE], wintypes.HANDLE)
        self.bind(self.kernel.ConnectNamedPipe, [wintypes.HANDLE, pointer], wintypes.BOOL)
        self.bind(self.kernel.ReadFile, [wintypes.HANDLE, pointer, wintypes.DWORD,
                                       dword_pointer, pointer], wintypes.BOOL)
        self.bind(self.kernel.WriteFile, [wintypes.HANDLE, pointer, wintypes.DWORD,
                                        dword_pointer, pointer], wintypes.BOOL)
        self.bind(self.kernel.PeekNamedPipe, [wintypes.HANDLE, pointer, wintypes.DWORD,
                                            dword_pointer, dword_pointer, dword_pointer], wintypes.BOOL)
        self.bind(self.kernel.CloseHandle, [wintypes.HANDLE], wintypes.BOOL)
        self.bind(self.kernel.GetCurrentProcess, [], wintypes.HANDLE)
        self.bind(self.kernel.LocalFree, [pointer], pointer)
        self.bind(self.security.OpenProcessToken, [wintypes.HANDLE, wintypes.DWORD,
                                                 ctypes.POINTER(wintypes.HANDLE)], wintypes.BOOL)
        self.bind(self.security.GetTokenInformation, [wintypes.HANDLE, ctypes.c_int, pointer,
                                                     wintypes.DWORD, dword_pointer], wintypes.BOOL)
        self.bind(self.security.ConvertSidToStringSidW, [pointer, ctypes.POINTER(pointer)], wintypes.BOOL)
        self.bind(self.security.GetSecurityInfo, [wintypes.HANDLE, ctypes.c_int, wintypes.DWORD,
                                                pointer, pointer, ctypes.POINTER(pointer),
                                                pointer, ctypes.POINTER(pointer)], wintypes.DWORD)
        self.bind(self.security.GetAclInformation, [pointer, pointer, wintypes.DWORD,
                                                  ctypes.c_int], wintypes.BOOL)
        self.bind(self.security.GetAce, [pointer, wintypes.DWORD, ctypes.POINTER(pointer)], wintypes.BOOL)

    @staticmethod
    def bind(function, arguments: list, result) -> None:
        function.argtypes = arguments
        function.restype = result

    @staticmethod
    def check(ok: object) -> None:
        if not ok:
            raise ctypes.WinError(ctypes.get_last_error())

    def close(self, handle: int) -> None:
        self.check(self.kernel.CloseHandle(handle))

    def create_raw(self, name: str) -> int:
        # Outbound, first instance, one message-mode instance, remote rejected.
        handle = self.kernel.CreateNamedPipeW(name, 0x2 | 0x00080000, 0x4 | 0x2 | 0x8,
                                             1, 65536, 65536, 0, None)
        if handle == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        return int(handle)

    def serve_once(self, handle: int, record: dict) -> None:
        connected = self.kernel.ConnectNamedPipe(handle, None)
        if not connected and ctypes.get_last_error() != 535:  # ERROR_PIPE_CONNECTED
            raise ctypes.WinError(ctypes.get_last_error())
        answer = {**record, "heartbeat_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds")}
        payload = json.dumps(answer).encode("utf-8")
        written = wintypes.DWORD()
        self.check(self.kernel.WriteFile(handle, payload, len(payload), ctypes.byref(written), None))
        assert written.value == len(payload), "raw holder did not send its complete record"
        # Do not disconnect: that would discard an answer the reader has not consumed.

    def open_reader(self, name: str, *, access: int = GENERIC_READ) -> int:
        deadline = time.monotonic() + scaled_time_bound(20)
        while True:
            handle = self.kernel.CreateFileW(name, access, 0, None, 3, IDENTIFICATION, None)
            if handle != ctypes.c_void_p(-1).value:
                return int(handle)
            error = ctypes.get_last_error()
            if error != 231 or time.monotonic() >= deadline:  # ERROR_PIPE_BUSY
                raise ctypes.WinError(error)
            time.sleep(0.01)

    def read_record(self, handle: int) -> dict:
        deadline = time.monotonic() + scaled_time_bound(20)
        data = bytearray()
        while time.monotonic() < deadline:
            available = wintypes.DWORD()
            self.check(self.kernel.PeekNamedPipe(handle, None, 0, None, ctypes.byref(available), None))
            if not available.value:
                time.sleep(0.01)
                continue
            buffer = ctypes.create_string_buffer(min(available.value, 65536))
            count = wintypes.DWORD()
            ok = self.kernel.ReadFile(handle, buffer, len(buffer), ctypes.byref(count), None)
            if not ok and ctypes.get_last_error() != 234:  # ERROR_MORE_DATA
                raise ctypes.WinError(ctypes.get_last_error())
            data.extend(buffer.raw[:count.value])
            assert len(data) <= 65536, "holder record exceeded the test reader's bound"
            try:
                record = json.loads(data)
            except (ValueError, UnicodeDecodeError):
                continue
            assert isinstance(record, dict), record
            return record
        raise AssertionError("the connected holder did not send a complete JSON record")

    def sid_text(self, sid: int) -> str:
        text = ctypes.c_void_p()
        self.check(self.security.ConvertSidToStringSidW(sid, ctypes.byref(text)))
        try:
            return ctypes.wstring_at(text.value)
        finally:
            self.kernel.LocalFree(text)

    def token_user_sid(self) -> str:
        token = wintypes.HANDLE()
        self.check(self.security.OpenProcessToken(self.kernel.GetCurrentProcess(), 0x8, ctypes.byref(token)))
        try:
            size = wintypes.DWORD()
            self.security.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))  # TokenUser
            assert size.value, "TokenUser did not report its required buffer size"
            buffer = ctypes.create_string_buffer(size.value)
            self.check(self.security.GetTokenInformation(token, 1, buffer, len(buffer), ctypes.byref(size)))
            return self.sid_text(ctypes.c_void_p.from_buffer(buffer).value)
        finally:
            self.close(token)

    def dacl(self, handle: int) -> list[tuple[int, int, int, str]]:
        class AclSize(ctypes.Structure):
            _fields_ = [("count", wintypes.DWORD), ("used", wintypes.DWORD), ("free", wintypes.DWORD)]

        acl = ctypes.c_void_p()
        descriptor = ctypes.c_void_p()
        error = self.security.GetSecurityInfo(handle, 6, 4, None, None, ctypes.byref(acl),
                                              None, ctypes.byref(descriptor))  # SE_KERNEL_OBJECT, DACL
        if error:
            raise ctypes.WinError(error)
        try:
            assert acl.value, "a NULL DACL grants everybody access"
            size = AclSize()
            self.check(self.security.GetAclInformation(acl, ctypes.byref(size), ctypes.sizeof(size), 2))
            entries = []
            for index in range(size.count):
                ace = ctypes.c_void_p()
                self.check(self.security.GetAce(acl, index, ctypes.byref(ace)))
                kind = ctypes.c_ubyte.from_address(ace.value).value
                flags = ctypes.c_ubyte.from_address(ace.value + 1).value
                # ACCESS_ALLOWED_ACE: header, DWORD mask, then the variable-length SID.
                assert kind == 0, f"unexpected pipe ACE type {kind}"
                mask = wintypes.DWORD.from_address(ace.value + 4).value
                entries.append((kind, flags, mask, self.sid_text(ace.value + 8)))
            return entries
        finally:
            self.kernel.LocalFree(descriptor)


def send(directory: Path, name: str, value: object = True) -> None:
    publish_atomically(str(directory / name), json.dumps(value))


def receive(directory: Path, name: str) -> object:
    return json.loads(read_when_published(directory / name, scaled_time_bound(30)))


@dataclass
class Peer:
    directory: Path
    process: subprocess.Popen
    pid: int | None = None

    def wait_for_marker(self, name: str) -> object:
        deadline = time.monotonic() + scaled_time_bound(30)
        while not published(self.directory / name):
            if self.process.poll() is not None or time.monotonic() >= deadline:
                log = (self.directory / "child.log").read_text(encoding="utf-8")
                raise AssertionError(f"peer did not publish {name}: exit={self.process.poll()}\n{log}")
            time.sleep(0.01)
        return receive(self.directory, name)

    def send(self, name: str) -> None:
        send(self.directory, name)

    def reap(self) -> None:
        self.process.wait(timeout=scaled_time_bound(20))
        if self.pid is not None:
            deadline = time.monotonic() + scaled_time_bound(20)
            while process_is_running(self.pid) and time.monotonic() < deadline:
                time.sleep(0.01)
            assert not process_is_running(self.pid), "actual peer survived its launcher"

    def kill(self) -> None:
        if self.pid is not None and process_is_running(self.pid):
            with suppress(ProcessLookupError):
                os.kill(self.pid, signal.SIGTERM)
        elif self.process.poll() is None:
            self.process.kill()
        self.reap()


@contextmanager
def peer(directory: Path, mode: str, **arguments) -> Iterator[Peer]:
    directory.mkdir(parents=True)
    environment = os.environ.copy()
    repository = Path(__file__).resolve().parents[1]
    environment["PYTHONPATH"] = os.pathsep.join([str(repository / "src"), str(repository / "tests"),
                                               environment.get("PYTHONPATH", "")])
    environment["LOCALAPPDATA"] = str(directory / "state")
    environment["XDG_STATE_HOME"] = str(directory / "state")
    with (directory / "child.log").open("w", encoding="utf-8") as log:
        child = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), mode,
                                  str(directory), json.dumps(arguments)], env=environment,
                                 stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
        helper = Peer(directory, child)
        try:
            helper.pid = int(helper.wait_for_marker("actual-pid"))
            yield helper
        finally:
            if helper.pid is None and published(directory / "actual-pid"):
                helper.pid = int(receive(directory, "actual-pid"))
            helper.kill()


def _file_attempt(directory: Path, arguments: dict) -> None:
    from agentic_hil.bench import _LifetimeLock

    lock = _LifetimeLock(Path(arguments["path"]))
    try:
        lock.acquire()
    except OSError:
        send(directory, "result", {"acquired": False})
    else:
        try:
            send(directory, "result", {"acquired": True})
        finally:
            lock.release()


def _raw_holder(directory: Path, arguments: dict) -> None:
    native = NativePipe()
    handle = native.create_raw(arguments["endpoint"])
    try:
        send(directory, "held")
        if arguments.get("answer", False):
            record = {
                "version": 1, "state": "held", "resource": arguments["resource"],
                "owner": {"owner_id": "raw-pipe-peer", "pid": -885,
                          "host": "test-host", "frontend": "pipe-only"},
                "acquired_at": "2020-01-01T00:00:00.000Z", "heartbeat_interval_s": 30.0,
            }
            native.serve_once(handle, record)
        receive(directory, "release")
    finally:
        native.close(handle)
    send(directory, "released")


def _product_holder(directory: Path, arguments: dict) -> None:
    from agentic_hil import bench, windows_device_lock

    windows_device_lock.pipe_name = lambda digest, root: arguments["endpoint"]
    root = Path(arguments["root"])
    root.mkdir(parents=True)
    owner = bench.BenchMutex(root=root, frontend="pipe-test", label="published-holder")
    release_guard = threading.Event()
    guard_thread = None
    try:
        clock = bench.utc_now_iso
        if arguments.get("guard", False):
            bench.HEARTBEAT_INTERVAL_S = scaled_time_bound(120)
            bench.utc_now_iso = lambda: "2020-01-01T00:00:00.000Z"
        try:
            owner.acquire([arguments["resource"]])
        finally:
            bench.utc_now_iso = clock
        send(directory, "held", json.loads(owner._holder_path(arguments["resource"]).read_text(encoding="utf-8")))
        if arguments.get("guard", False):
            ready = threading.Event()

            def hold_guard() -> None:
                with owner._guard:
                    ready.set()
                    release_guard.wait()

            guard_thread = threading.Thread(target=hold_guard, daemon=True)
            guard_thread.start()
            assert ready.wait(scaled_time_bound(20)), "holder thread never acquired _guard"
            send(directory, "guard-held")
        receive(directory, "release")
    finally:
        release_guard.set()
        if guard_thread is not None:
            guard_thread.join(timeout=scaled_time_bound(20))
            assert not guard_thread.is_alive(), "guard thread did not exit"
        owner.release_all()
    send(directory, "released")


def _tentative_holder(directory: Path, arguments: dict) -> None:
    from agentic_hil import bench, windows_device_lock

    root = Path(arguments["root"])
    root.mkdir(parents=True, exist_ok=True)
    resource = arguments["resource"]
    later = arguments["later"]
    name = windows_device_lock.pipe_name
    windows_device_lock.pipe_name = lambda digest, path: (
        arguments["endpoint"] if digest == bench.resource_digest(resource) else name(digest, path)
    )
    acquire = bench._LifetimeLock.acquire

    def pause_at_later(lock) -> None:
        if lock.path == root / f"{bench.resource_digest(later)}.lock":
            send(directory, "tentative")
            receive(directory, "reader-connected")
        acquire(lock)

    bench._LifetimeLock.acquire = pause_at_later
    owner = bench.BenchMutex(root=root, frontend="tentative-test")
    try:
        try:
            owner.acquire([resource, later], wait_s=0)
        except bench.DeviceBusyError as error:
            send(directory, "rolled-back", {"result": error.result, "held": sorted(owner.held_resources())})
        else:
            raise AssertionError("whole-set attempt acquired the blocked later resource")
    finally:
        bench._LifetimeLock.acquire = acquire
        owner.release_all()


if __name__ == "__main__":
    work = Path(sys.argv[2])
    send(work, "actual-pid", os.getpid())
    operation = {"file": _file_attempt, "raw": _raw_holder, "product": _product_holder,
                 "tentative": _tentative_holder}[sys.argv[1]]
    operation(work, json.loads(sys.argv[3]))
