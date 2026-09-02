"""Shared helpers that must live outside any single test module."""

from __future__ import annotations

import inspect
import os
import shutil
import time
from pathlib import Path

# Captured before any test monkeypatches HOME.
REAL_HOME = Path(os.path.expanduser("~"))
# The profile root on every platform. It used to divert to Local AppData on
# Windows because the trust check refused the per-user Temp ACL; it no longer
# does, and Local AppData was the wrong place anyway: a packaged host process
# has its writes there redirected into its own LocalCache, so the launcher never
# landed where the path said it did.
#
# The pid is in the name so two suites on one machine never share a launcher,
# and so a directory left by a run that never reached its finalizer can be
# judged by the next run rather than accumulating forever (see
# `sweep_stale_launchers`).
LAUNCHER_PREFIX = "agentic-hil-pytest-launcher-"
LAUNCHER_ROOT = REAL_HOME / f"{LAUNCHER_PREFIX}{os.getpid()}"

# Windows, where `os.kill(pid, 0)` is not a liveness probe: CPython maps every
# signal other than the console events onto TerminateProcess, so asking that
# question there kills the run being asked about.
_WINDOWS_SYNCHRONIZE = 0x00100000
_WINDOWS_WAIT_OBJECT_0 = 0x00000000
_WINDOWS_ERROR_INVALID_PARAMETER = 87


def publish_atomically(path: str, text: str) -> None:
    """Give ``path`` its whole value in one step, so no reader ever sees it empty.

    ``open(path, "w").write(value)`` puts the name on the disk first and the
    value in it afterwards, and a reader that polls for the name can land in
    between: a loaded `macos-latest` runner read a helper's pid file that existed
    with nothing in it and converted the empty string (issue #395). Writing under
    a temporary name in the same directory and renaming it over the target closes
    that window, because a rename either has happened or has not: the name is
    absent, or it carries the whole value.

    The imports are inside the body because this function's own source is what
    the suite's spawned helpers run. `PUBLISH_ATOMICALLY_SOURCE` is
    `inspect.getsource` of it, so a helper in a fresh interpreter publishes by
    the same rule the parent reads by, without importing anything from the suite
    and without a second copy of the rule to keep in step.
    """
    import os
    import time

    temporary = f"{path}.publishing"
    with open(temporary, "w", encoding="utf-8") as stream:
        stream.write(text)
    # Windows refuses a rename onto a name another process holds open, and a
    # reader polling for the value is exactly such a process. That can only bite
    # a second publication to the same name, since the first one has nothing to
    # replace, but a helper must not die of it either way.
    deadline = time.monotonic() + 5.0
    while True:
        try:
            os.replace(temporary, path)
            return
        except PermissionError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.01)


PUBLISH_ATOMICALLY_SOURCE = inspect.getsource(publish_atomically)


def published(path: Path) -> bool:
    """Whether ``path`` already carries a value published by the writer above.

    The question a poller has to ask instead of `exists()`. Under the rename a
    name that is there is complete, so the two answers agree; asking for the
    value keeps them agreeing if a helper is ever written back to a plain open.
    """
    try:
        return bool(path.read_text(encoding="utf-8").strip())
    except (FileNotFoundError, PermissionError):
        # Not there yet, or being renamed over on Windows. Both mean "not yet".
        return False


def read_when_published(path: Path, timeout_s: float = 5.0) -> str:
    """Wait for ``path`` to carry a value and return it, stripped.

    The default budget is the one #390's review settled for the same shape: a
    fresh interpreter under `pytest -n auto` can take seconds just to start, so a
    one second wait is not a helper that failed to publish, it is a runner under
    load. Five seconds is a generous multiple of an interpreter start and still
    fails the test rather than hanging it when a helper really never publishes.
    """
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            text = path.read_text(encoding="utf-8").strip()
        except (FileNotFoundError, PermissionError):
            text = ""
        if text:
            return text
        if time.monotonic() >= deadline:
            raise AssertionError(f"{path} carried no published value within {timeout_s}s")
        time.sleep(0.01)


def trusted_launcher() -> Path:
    """A launcher the product's trust rule accepts, created once per session.

    The rule rejects an executable that group or other may write, and a CI
    runner's Python lives under a tool cache that hands out exactly such modes,
    so `sys.executable` cannot stand in for an installed launcher: it is
    untrusted there and trusted here, which is why this only ever failed away
    from a developer machine. `tempfile.gettempdir()` is a forbidden root for an
    MCP command, so `tmp_path` cannot hold it either.
    """
    launcher = LAUNCHER_ROOT / "bin" / ("agentic-hil.exe" if os.name == "nt" else "agentic-hil")
    if not launcher.is_file():
        launcher.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        launcher.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        launcher.chmod(0o700)
    return launcher


def remove_trusted_launcher() -> None:
    shutil.rmtree(LAUNCHER_ROOT, ignore_errors=True)


def sweep_stale_launchers() -> list[Path]:
    """Remove the launchers of runs that are no longer running, and say which.

    `remove_trusted_launcher` only runs when a session ends on its own terms. A
    run that is killed, crashes, or loses a worker never reaches it, and since
    the name carries the pid, every such run leaves a fresh directory in the
    real home: one bench had collected eighteen of them, one per interrupted run
    over a few weeks. So a session cleans up after its predecessors instead of
    trusting each of them to have cleaned up after itself.

    A directory is removed only when this machine can prove nobody owns it. A
    live pid, a pid this cannot get a readable answer about, a name that carries
    no pid at all, and a directory this process is not allowed to delete are all
    left exactly where they stand. That asymmetry is the whole design: the only
    damage a sweep can do is deleting the launcher a suite running beside this
    one is still using, and every uncertain answer therefore counts as alive.
    """
    removed: list[Path] = []
    for candidate in sorted(LAUNCHER_ROOT.parent.glob(f"{LAUNCHER_PREFIX}*")):
        # This session's own launcher needs no case of its own, which is the one
        # way this differs from the sandbox sweep beside it: that one judges by
        # age, and a long session's own root is exactly what looks old. Liveness
        # cannot make that mistake, because the pid in this run's own name is
        # this very process.
        pid = _launcher_pid(candidate.name)
        if pid is None or process_is_running(pid):
            continue
        try:
            shutil.rmtree(candidate)
        except OSError:
            # Another user's leftover, or one this platform will not let go of
            # while something still holds a file inside it. Not this run's to
            # judge and never a reason to fail a suite, so it stays where it is
            # and is not reported as removed.
            continue
        removed.append(candidate)
    return removed


def _launcher_pid(name: str) -> int | None:
    """The pid a launcher directory's name carries, or None if it carries none.

    Only the exact spelling `LAUNCHER_ROOT` writes counts, ascii digits naming a
    real pid and nothing else. A name that does not parse is not something this
    suite left behind, and a sweep has no business deleting a directory of
    somebody else's that merely shares the prefix. `isdigit` is asked after
    `isascii` because on its own it accepts spellings `int` then refuses,
    superscripts among them.
    """
    suffix = name[len(LAUNCHER_PREFIX) :]
    if not (suffix.isascii() and suffix.isdigit()):
        return None
    pid = int(suffix)
    return pid if pid > 0 else None


def process_is_running(pid: int) -> bool:
    """Whether `pid` is a live process on this machine, without disturbing it.

    The discipline `tools/run_lock.py` settled for exactly this question, spelled
    out again here rather than imported. `tools/` is repository tooling that
    never ships, `tests/conftest.py` imports this module at module scope, and a
    test tree that reaches into `tools/` from there fails to collect out of a
    source distribution; MANIFEST.in says so, and names the file that learned it
    the hard way. What the two must keep in step is the rule, not the code.

    Every uncertain answer is `True`, deliberately: this decides whether a
    directory may be deleted out from under whoever owns it, so "I cannot tell"
    means "leave it alone".
    """
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    if os.name == "nt":
        return _windows_process_is_running(pid)
    try:
        # Signal 0 checks for the process without delivering anything. Zero and
        # negative pids are excluded above because they address process groups.
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        # PermissionError says it exists and belongs to somebody else; anything
        # else is an answer this cannot read, and both mean "do not touch it".
        return True
    return True


def _windows_process_is_running(pid: int) -> bool:
    """The same question on Windows, where `os.kill(pid, 0)` would answer it by
    terminating the process. `WaitForSingleObject` on the process handle asks
    without touching it: a live process is unsignalled, an exited one is
    signalled at once. Exit codes are not consulted, because a process that
    exited with 259 is indistinguishable from a running one through
    `GetExitCodeProcess`, and this is the one call site where that matters."""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    open_process = kernel32.OpenProcess
    open_process.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    open_process.restype = wintypes.HANDLE
    wait_for_object = kernel32.WaitForSingleObject
    wait_for_object.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    wait_for_object.restype = wintypes.DWORD
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL

    handle = open_process(_WINDOWS_SYNCHRONIZE, False, pid)
    if not handle:
        # Only "no such process" is a dead process. Access denied means it is
        # alive and owned by somebody else, which is still alive.
        return ctypes.get_last_error() != _WINDOWS_ERROR_INVALID_PARAMETER
    try:
        return wait_for_object(handle, 0) != _WINDOWS_WAIT_OBJECT_0
    finally:
        close_handle(handle)
