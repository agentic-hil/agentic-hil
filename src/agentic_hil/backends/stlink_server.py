"""stlink-server, shared by every session on this machine that reaches a probe through it (#624).

STM32CubeCLT's stlink-server serves every in-circuit debugger or programmer on
the machine from one port, so there is one per port rather than one per
session. A session that needs it and finds none listening starts it, and it is
ended only once the last session using it is done:

* Who uses it is recorded beside the device locks, in one file per port and
  under a machine-wide lock of its own, so every service on the machine reads
  and writes the same record: the process started, by number and creation
  time, and every session using it, by its service's process and a token.
* It is started detached, outside the starting service's process tree,
  because that service may exit while another service's session still reaches
  its probe through it.
* A session that stops waits half a second once its GDB server is gone and
  then, under the lock, takes itself out of the record. While a session of a
  running service is still recorded, or the kernel shows a connection still
  open to the port (another program's), the server is left running and the
  record kept, so a later last session ends it. Otherwise it is ended: through
  the handle of the process that started it where that is this one, and by
  its recorded number otherwise, once the running process with that number is
  confirmed to be the one started.
* One another program started is never ended.

The half second: ended at once after its GDB server's kill, stlink-server
2.1.1 left the next start refused with `TCPCMD OPEN_DEV FAIL` in 6 of 40
recorded cycles; ended once it had released the probe's USB, at most 9 ms
after the kill, or half a second after the kill, in none of 79
(st_link_gdbserver_7_14_0_linux_server_ends_recordings.json). Kept running
across sessions whose GDB servers were killed, it refused every start from
the tenth on with `Target unknown error 33`
(st_link_gdbserver_7_14_0_linux_restarts_recordings.json), which is why a
server no session can be shown to use is ended rather than kept, also on a
host whose kernel publishes no connection table to count other programs by.
"""

from __future__ import annotations

import json
import os
import secrets
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from agentic_hil.backends.common import invocation
from agentic_hil.bench import BenchMutex, DeviceBusyError, device_lock_root, utc_now_iso
from agentic_hil.config import ConfigError, atomic_write_text, safe_read_text
from agentic_hil.process import ProcessImage, snapshot_process_images, spawn_detached_process, terminate_process_tree
from agentic_hil.types import JsonObject

RECORD_VERSION = 1
# stlink-server needs no options and listens on loopback; version 2.1.1
# refused `--auto-exit` and ended at once on SIGTERM, with exit status -15
# (recorded).
STLINK_SERVER_LISTEN_TIMEOUT_S = 10.0
STLINK_SERVER_OUTPUT_TAIL_LINES = 20
# How long a stopping session waits once its GDB server is gone before it
# decides about stlink-server. See the module's docstring for the rounds.
STLINK_SERVER_RELEASE_WAIT_S = 0.5
# How long a start or a stop waits for another one on the same port to be done
# with the record. A start holds it for at most the listen timeout.
STLINK_SERVER_LOCK_WAIT_S = 3 * STLINK_SERVER_LISTEN_TIMEOUT_S
# The line stlink-server logs when it cannot open a probe for a client.
OPEN_REFUSAL_MARKER = "OPEN_DEV FAIL"
# The kernel's state code for an established connection: /proc/net/tcp's
# `01`, and MIB_TCP_STATE_ESTAB on Windows.
_PROC_ESTABLISHED = "01"
_WINDOWS_ESTABLISHED = 5

# The servers this process started, by process number, so the session that
# ends one collects its exit status through the handle that started it.
_STARTED: dict[int, subprocess.Popen] = {}
_STARTED_GUARD = threading.Lock()


@dataclass
class Share:
    """One session's use of the stlink-server on a port, or why it has none.

    `record` is what the session reports as `probe_server`. `token` names the
    session in the machine's record, and is None where it is not in it: a
    session without stlink-server, and one sharing a server another program
    started. `pid` is the recorded server's process, `process` its handle where
    this session started it, and `log_path` and `log_offset` where its words
    since this session's start began are."""

    port: int
    record: JsonObject
    token: str | None = None
    pid: int | None = None
    process: subprocess.Popen | None = None
    log_path: Path | None = None
    log_offset: int = 0
    released: bool = False


def stlink_server_listening(port: int) -> bool:
    """Whether something accepts connections on `port` on loopback."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.5)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def output_tail(path: Path) -> list[str]:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as error:
        return [f"{path.name} could not be read: {error}"]
    return lines[-STLINK_SERVER_OUTPUT_TAIL_LINES:]


def take_share(port: int, find_executable: Callable[[], str | None], log_path: Path, display: Callable[[Path], str], timeout_s: float) -> Share:
    """The stlink-server a starting session reaches the probe through: the one listening, or one it starts.

    Direct, with the reason, where there is none to reach it through: then the
    GDB server opens the probe's USB itself."""
    mutex = BenchMutex(frontend="stlink-server")
    try:
        mutex.acquire_named(_lock_name(port), wait_s=STLINK_SERVER_LOCK_WAIT_S)
    except DeviceBusyError:
        mutex.release_all()
        return Share(port, _direct(f"Another session kept the record of stlink-server on port {port} for {STLINK_SERVER_LOCK_WAIT_S:g} s"))
    except (ConfigError, OSError) as error:
        mutex.release_all()
        return Share(port, _direct(f"The record of stlink-server on port {port} could not be opened ({type(error).__name__})"))
    try:
        return _take_share(port, find_executable, log_path, display, timeout_s)
    finally:
        mutex.release_all()


def release_share(share: Share, timeout_s: float) -> None:
    """A session is done with its stlink-server: end it if this was its last user, and say in the record what happened."""
    if share.released:
        return
    share.released = True
    if share.token is None:
        if share.record.get("mode") == "shared":
            share.record["left_running"] = "Another program started it, so it is left to that one."
        return
    time.sleep(STLINK_SERVER_RELEASE_WAIT_S)
    mutex = BenchMutex(frontend="stlink-server")
    try:
        mutex.acquire_named(_lock_name(share.port), wait_s=STLINK_SERVER_LOCK_WAIT_S)
    except (DeviceBusyError, ConfigError, OSError) as error:
        mutex.release_all()
        share.record["left_running"] = f"The record of who uses it could not be taken ({type(error).__name__}), so it was left running."
        return
    try:
        _release(share, timeout_s)
    except (ConfigError, OSError) as error:
        share.record["left_running"] = f"The record of who uses it could not be updated ({type(error).__name__}), so it was left running."
    finally:
        mutex.release_all()


def open_refusal(share: Share | None) -> str | None:
    """The line stlink-server logged since `share`'s start began about a probe it could not open for a client, if any."""
    if share is None or share.log_path is None:
        return None
    try:
        with open(share.log_path, "rb") as log:
            log.seek(share.log_offset)
            text = log.read().decode("utf-8", errors="replace")
    except OSError:
        return None
    for line in text.splitlines():
        if OPEN_REFUSAL_MARKER in line:
            return line.strip()
    return None


def established_connections(port: int) -> int | None:
    """How many connections to `port` this host's kernel shows established on the listening side.

    None where the kernel publishes no table this reads: Linux has
    /proc/net/tcp and tcp6, Windows GetTcpTable and GetTcp6Table."""
    if os.name == "nt":
        try:
            return _windows_established(port)
        except (OSError, AttributeError):
            return None
    if sys.platform.startswith("linux"):
        return _proc_established(port)
    return None


def established_server_side(lines: Iterable[str], port: int) -> int:
    """The /proc/net/tcp lines whose own end is `port` and whose connection is established."""
    wanted = f":{port:04X}"
    count = 0
    for line in lines:
        fields = line.split()
        if len(fields) > 3 and fields[1].upper().endswith(wanted) and fields[3] == _PROC_ESTABLISHED:
            count += 1
    return count


def _take_share(port: int, find_executable: Callable[[], str | None], log_path: Path, display: Callable[[Path], str], timeout_s: float) -> Share:
    with _STARTED_GUARD:
        # A server this process started and another one ended: collected here,
        # so it does not stay behind as a zombie of this process.
        for pid in [pid for pid, process in _STARTED.items() if process.poll() is not None]:
            del _STARTED[pid]
    images = snapshot_process_images()
    recorded = _read_record(port)
    if recorded is not None and not _alive(int(recorded["pid"]), int(recorded.get("created_ns") or 0), images):
        _drop_record(port)
        recorded = None
    token = secrets.token_hex(8)
    user = {"pid": os.getpid(), "created_ns": _created_ns(os.getpid(), images), "since": utc_now_iso()}
    if stlink_server_listening(port):
        if recorded is None:
            return Share(port, {"mode": "shared", "started_by_session": False, "started_by_agentic_hil": False, "port": port, "ended": False})
        users = _live_users(recorded, images)
        users[token] = user
        recorded["users"] = users
        try:
            _write_record(port, recorded)
        except (ConfigError, OSError) as error:
            return Share(port, {"mode": "shared", "started_by_session": False, "started_by_agentic_hil": True, "port": port, "ended": False, "unrecorded": f"This session could not add itself to the record ({type(error).__name__}), so it never ends the server."})
        log = Path(str(recorded["log"])) if recorded.get("log") else None
        return Share(
            port,
            {"mode": "shared", "started_by_session": False, "started_by_agentic_hil": True, "port": port, "ended": False},
            token=token,
            pid=int(recorded["pid"]),
            log_path=log,
            log_offset=_size(log),
        )
    executable = find_executable()
    if executable is None:
        return Share(port, _direct("No stlink-server was found on PATH"))
    began = time.monotonic()
    try:
        with open(log_path, "wb") as log:
            process = spawn_detached_process([*invocation(executable)], cwd=str(Path(executable).parent), stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
    except OSError as error:
        return Share(port, {**_direct(f"stlink-server could not be started ({type(error).__name__}: {error})"), "executable": executable})
    deadline = began + max(0.0, min(timeout_s, STLINK_SERVER_LISTEN_TIMEOUT_S))
    while True:
        if stlink_server_listening(port):
            listening_after_ms = int((time.monotonic() - began) * 1000)
            server = {"version": RECORD_VERSION, "port": port, "pid": process.pid, "created_ns": _created_ns(process.pid, snapshot_process_images()), "log": str(log_path), "started_at": utc_now_iso(), "users": {token: user}}
            try:
                _write_record(port, server)
            except (ConfigError, OSError) as error:
                terminate_process_tree(process, STLINK_SERVER_LISTEN_TIMEOUT_S)
                return Share(port, {**_direct(f"The record of stlink-server on port {port} could not be written ({type(error).__name__}), so the one started was ended"), "executable": executable})
            with _STARTED_GUARD:
                _STARTED[process.pid] = process
            return Share(
                port,
                {"mode": "shared", "started_by_session": True, "started_by_agentic_hil": True, "port": port, "executable": executable, "listening_after_ms": listening_after_ms, "log_path": display(log_path), "ended": False},
                token=token,
                pid=process.pid,
                process=process,
                log_path=log_path,
            )
        if process.poll() is not None or time.monotonic() >= deadline:
            break
        time.sleep(0.05)
    exited = process.poll() is not None
    if not exited:
        terminate_process_tree(process, STLINK_SERVER_LISTEN_TIMEOUT_S)
    reason = (
        f"stlink-server exited with status {process.returncode} before it listened on port {port}"
        if exited
        else f"stlink-server did not listen on port {port} within {int(min(timeout_s, STLINK_SERVER_LISTEN_TIMEOUT_S))} s and was ended"
    )
    return Share(port, {**_direct(reason), "executable": executable, "returncode": process.returncode, "output_tail": output_tail(log_path), "log_path": display(log_path)})


def _release(share: Share, timeout_s: float) -> None:
    images = snapshot_process_images()
    recorded = _read_record(share.port)
    if recorded is None or int(recorded["pid"]) != share.pid:
        share.record["left_running"] = "It was no longer recorded as the server this session used, so nothing was ended."
        return
    users = _live_users(recorded, images)
    users.pop(str(share.token), None)
    share.record["other_sessions"] = len(users)
    if users:
        recorded["users"] = users
        _write_record(share.port, recorded)
        share.record["left_running"] = f"{len(users)} other session(s) still reach a probe through it; the last of them ends it."
        return
    connections = established_connections(share.port)
    share.record["open_connections"] = connections
    if connections:
        recorded["users"] = {}
        _write_record(share.port, recorded)
        share.record["left_running"] = f"{connections} connection(s) to it from another program were still open; the next session to stop with none open ends it."
        return
    ended, returncode = _end_server(share.pid, int(recorded.get("created_ns") or 0), timeout_s)
    if not ended:
        recorded["users"] = {}
        _write_record(share.port, recorded)
        share.record["left_running"] = "The running process with its number could not be confirmed as the one started, so it was not ended."
        return
    _drop_record(share.port)
    share.record["ended"] = True
    share.record["returncode"] = returncode


def _end_server(pid: int | None, created_ns: int, timeout_s: float) -> tuple[bool, int | None]:
    """End the recorded server: through its handle where this process started it, else by its confirmed number."""
    if pid is None:
        return False, None
    with _STARTED_GUARD:
        process = _STARTED.pop(pid, None)
    if process is not None:
        if process.poll() is None:
            try:
                terminate_process_tree(process, timeout_s)
            except BaseException:
                with _STARTED_GUARD:
                    _STARTED[pid] = process
                raise
        return True, process.returncode
    return _end_by_number(pid, created_ns, timeout_s), None


def _end_by_number(pid: int, created_ns: int, timeout_s: float) -> bool:
    """End a server another process of this product started, once the process with its number is confirmed to be it."""
    images = snapshot_process_images()
    if images is None or not created_ns:
        return False
    if not _same_process(pid, created_ns, images):
        return True
    for force in (False, True):
        _signal(pid, force)
        deadline = time.monotonic() + max(0.1, timeout_s)
        while time.monotonic() < deadline:
            images = snapshot_process_images()
            if images is not None and not _same_process(pid, created_ns, images):
                return True
            time.sleep(0.05)
    return False


def _signal(pid: int, force: bool) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        return
    sig = signal.SIGKILL if force else signal.SIGTERM
    try:
        # Started detached, it leads a process group of its own.
        if os.getpgid(pid) == pid:
            os.killpg(pid, sig)
        else:
            os.kill(pid, sig)
    except ProcessLookupError:
        return


def _same_process(pid: int, created_ns: int, images: tuple[ProcessImage, ...]) -> bool:
    return any(image.pid == pid and image.created_ns == created_ns for image in images)


def _alive(pid: int, created_ns: int, images: tuple[ProcessImage, ...] | None) -> bool:
    """Whether the process recorded as `pid`, created at `created_ns`, still runs.

    Where this host publishes no process table, whether any process has that
    number."""
    if images is None:
        return _pid_exists(pid)
    for image in images:
        if image.pid == pid:
            return not created_ns or not image.created_ns or image.created_ns == created_ns
    return False


def _pid_exists(pid: int) -> bool:
    if os.name == "nt":
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _created_ns(pid: int, images: tuple[ProcessImage, ...] | None) -> int:
    for image in images or ():
        if image.pid == pid:
            return image.created_ns
    return 0


def _live_users(recorded: JsonObject, images: tuple[ProcessImage, ...] | None) -> dict[str, JsonObject]:
    users = recorded.get("users")
    if not isinstance(users, dict):
        return {}
    return {token: user for token, user in users.items() if isinstance(user, dict) and _alive(int(user.get("pid") or 0), int(user.get("created_ns") or 0), images)}


def _direct(reason: str) -> JsonObject:
    return {"mode": "direct", "reason": f"{reason}, so ST-LINK_gdbserver opens the probe's USB itself."}


def _lock_name(port: int) -> str:
    return f"stlink-server:{port}"


def _record_path(port: int) -> Path:
    return device_lock_root() / f"stlink-server-{port}.json"


def _read_record(port: int) -> JsonObject | None:
    try:
        recorded = json.loads(safe_read_text(_record_path(port)))
    except (ConfigError, OSError, ValueError):
        return None
    if not isinstance(recorded, dict) or recorded.get("version") != RECORD_VERSION or recorded.get("port") != port or not isinstance(recorded.get("pid"), int):
        return None
    return recorded


def _write_record(port: int, recorded: JsonObject) -> None:
    atomic_write_text(_record_path(port), json.dumps(recorded, sort_keys=True))


def _drop_record(port: int) -> None:
    try:
        _record_path(port).unlink()
    except FileNotFoundError:
        return


def _size(path: Path | None) -> int:
    if path is None:
        return 0
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _proc_established(port: int) -> int | None:
    lines: list[str] = []
    read = False
    for table in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            text = Path(table).read_text(encoding="ascii")
        except FileNotFoundError:
            continue
        except OSError:
            return None
        read = True
        lines.extend(text.splitlines()[1:])
    return established_server_side(lines, port) if read else None


def _windows_established(port: int) -> int:
    """GetTcpTable and GetTcp6Table, counted the way /proc/net/tcp is: established, with `port` as the own end."""
    import ctypes

    iphlpapi = ctypes.WinDLL("iphlpapi")
    count = 0
    # (function, size of one row, offset of the own port in it): MIB_TCPROW is
    # state, local address, local port, remote address, remote port;
    # MIB_TCP6ROW is state, local address (16 bytes), scope, local port, ...
    for name, row_size, port_offset in (("GetTcpTable", 20, 8), ("GetTcp6Table", 52, 24)):
        function = getattr(iphlpapi, name)
        function.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong), ctypes.c_int]
        function.restype = ctypes.c_ulong
        size = ctypes.c_ulong(0)
        buffer = None
        for _ in range(4):
            buffer = ctypes.create_string_buffer(max(size.value, 4))
            result = function(buffer, ctypes.byref(size), 0)
            if result == 0:
                break
            if result == 232:  # ERROR_NO_DATA: an empty table
                buffer = None
                break
            if result != 122:  # ERROR_INSUFFICIENT_BUFFER: asked again with the size it named
                raise OSError(result, f"{name} failed")
        else:
            raise OSError(122, f"{name} kept growing")
        if buffer is None:
            continue
        (entries,) = struct.unpack_from("<I", buffer, 0)
        for index in range(entries):
            offset = 4 + index * row_size
            (state,) = struct.unpack_from("<I", buffer, offset)
            (own_port,) = struct.unpack_from("<I", buffer, offset + port_offset)
            if state == _WINDOWS_ESTABLISHED and socket.ntohs(own_port & 0xFFFF) == port:
                count += 1
    return count
