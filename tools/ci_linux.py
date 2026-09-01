"""Run the test suite the way the Linux CI runners do, in a container.

A Windows developer cannot run the POSIX half of this suite, and the half that
does not run is exactly the half that breaks: three times in two days a change
was verified green on Windows and failed on all four POSIX runners -- a test
that assumed a debugger was installed, two that wrote `"nt"` into the shared
``os`` module, and one that asserted a refusal a branch had deliberately
removed. Every one of them was invisible locally and obvious here.

This is not a replacement for CI. CI runs four Python versions on three
platforms; this runs one version on Linux. It catches the platform class, which
is the class that keeps getting through.

The container clones the repository from a read-only mount rather than working
in the mount itself. That is the whole point of the arrangement:

* a bind mount carries the working tree, including ``.venv``, ``.testenv`` and
  whatever else is lying around, and an editable install from it would resolve
  to Windows paths;
* NTFS presents its own permission bits through the mount, so the mode and
  ownership tests -- the ones this exists to exercise -- would be measuring the
  mount driver rather than the code.

Cloning inside the container tests the committed tree, which is what CI tests.
Uncommitted work is therefore *not* covered: commit first, or expect a green
run that says nothing about what you changed.

The container's exit status is not taken at face value. A clone that cannot
build a usable tree is reported as itself, and a run is only allowed to succeed
if pytest said what it did: no summary line, or a summary accounting for no
tests at all, fails the run. Exit 0 after nothing was collected has been
observed, and a green result for a suite that never executed is worse than no
result at all.

One run at a time per machine. Three or four full-suite containers on one
Docker daemon starve each other until the suite dies mid-flight without a
summary line, which the check above then correctly reports as nothing known to
have been tested: a wasted run, a retry, and a partial line somebody may read as
progress. A run therefore takes a lock for its whole duration and queues behind
whoever holds it, naming the holder while it waits. ``--no-wait`` refuses
instead of queueing.

Usage:

    python tools/ci_linux.py                     # the whole suite
    python tools/ci_linux.py tests/test_hardening.py -x
    python tools/ci_linux.py --python 3.13       # a different interpreter
    python tools/ci_linux.py --no-wait           # refuse if a run is in flight

Everything after the recognised options is handed to pytest unchanged.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import time
from contextlib import contextmanager, suppress
from datetime import datetime, timezone
from pathlib import Path

# CI runs 3.10 through 3.13; this runs one of them. 3.12 is the newest version
# every dependency ships wheels for, so the image builds without a compiler and
# the run stays fast. Override with --python to reproduce a version-specific
# failure.
DEFAULT_PYTHON = "3.12"
EXIT_NO_DOCKER = 2
# The clone step and the missing result are separate failures and get separate
# statuses, because "the tree never built" and "the tests never reported" ask
# for different things from whoever reads the exit code.
EXIT_CLONE_FAILED = 3
EXIT_NO_RESULT = 4
# A run refused because another one holds the machine is not a test failure and
# must not read as one: a script that retries on red would otherwise retry the
# collision it was just told about.
EXIT_LOCKED = 5

# Who reads this file: `tools/ci_linux.py`, and nothing else. It is written by
# this script on an operator's own machine, read by the next run of this script
# on that machine, and understood by neither the MCP server nor any other tool
# in this repository.
#
# It is deliberately *not* a device lock. The bench mutex lives one level
# further down, in `~/.agentic-hil/device-locks/`, is owned by the server, and
# is the one `AGENTS.md` says never to delete by hand. This one holds no
# hardware: it holds a Docker daemon's capacity, and deleting it while no
# container of this script is running costs nothing.
#
# Format: one JSON object, UTF-8, indented so it can be read in a terminal.
#
#     {
#       "version": 1,
#       "owner_id": "9f2c...",      random, so a holder can recognise its own record
#       "pid": 21044,               the process to check for liveness, on `host` only
#       "host": "BENCH-01",         a pid from another machine cannot be judged here
#       "started_at": "2026-09-01T09:12:33Z",
#       "root": "C:/Users/dev/work/agentic-hil",   which checkout is running
#       "image": "python:3.12",
#       "pytest_args": ["-q"]
#     }
#
# The absolute path is in there on purpose: the first question of somebody
# looking at a queued run is which of their checkouts is holding it, and this
# file never leaves the machine that wrote it.
CI_LOCK_ROOT_NAME = ".agentic-hil"
CI_LOCK_NAME = "ci-linux.lock"
# A second file beside the lock, and a different kind of thing: never a record,
# never removed, holding nobody. Runs take a machine-wide OS lock on it for the
# split second in which the lock file is created, or a dead one read, judged and
# replaced, so that step is indivisible across every run on this machine. The
# record lock says *who* holds the machine; this one serialises the moments two
# runs could each undo the other's takeover, or one read another's half-written
# record. See `_machine_wide_guard`.
CI_LOCK_GUARD_NAME = "ci-linux.lock.guard"
# Bumped when a reader of this file could otherwise misread it. An unknown
# version is never judged stale: a newer checkout of this repository running
# beside an older one is an ordinary day here, and the older one must queue
# behind it rather than delete its lock.
CI_LOCK_RECORD_VERSION = 1
LOCK_POLL_INTERVAL_S = 2.0
# A wait is not bounded, because the thing being waited for is a full suite and
# a queue of three legitimately takes an hour; it is loud instead, repeating who
# holds the lock so a wait can never be mistaken for a hang. `--no-wait` is the
# way out for somebody who knows better.
LOCK_NOTICE_INTERVAL_S = 60.0
# The longest a live run is given between publishing the lock path and writing
# its record into it. `_create` does the two back to back, so any real gap is
# milliseconds; the margin is wide only so a run publishing under load -- or one
# on an older checkout that creates outside this machine's guard, where the
# guard cannot serialise its write against another run's read -- is never
# mistaken for a holder that died mid-write. An empty file younger than this is
# read as a write in progress and waited on; only one that has sat empty past it
# is judged abandoned and removed. Erring long only delays reclaiming a truly
# dead empty lock, which no run was going to take anyway; erring short is the
# double-acquire this whole mechanism exists to prevent.
EMPTY_LOCK_GRACE_S = 10.0

# Windows, where `os.kill(pid, 0)` is not a liveness probe: CPython maps every
# signal other than the console events onto TerminateProcess, so asking that
# question there kills the run being asked about.
_WINDOWS_SYNCHRONIZE = 0x00100000
_WINDOWS_WAIT_OBJECT_0 = 0x00000000
_WINDOWS_ERROR_INVALID_PARAMETER = 87

# The container prints this before it gives up on the clone. A run that dies
# there has to go red naming the clone: relying on `set -e` alone made a broken
# checkout surface as a pytest step that never happened, and once it had been
# observed exiting 0 with nothing collected, the step that failed was the one
# piece of information the exit code did not carry.
CLONE_FAILURE_MARKER = "CI_LINUX_CLONE_FAILED"

# `pip install -e` needs the metadata build; `git clone` needs git. The full
# python image carries both, the slim variants do not, and diagnosing that from
# inside a container is a waste of an afternoon.
IMAGE = "python:{version}"

# The mount is owned by the host user, not the container's root, so git treats
# /src as unsafe and refuses to read it; `safe.directory` waives that check for
# the one path we clone from.
# `"$@"` rather than the arguments interpolated into the text: they arrive as
# `bash -c <script> <$0> <arg>...` and stay one argument each. Joining them with
# spaces split `-k "foo and bar"` into three and handed any quote, `$` or `;` in
# a path or expression to Bash, which is the opposite of "passed to pytest
# unchanged".
# The clone is checked twice, and neither check is redundant. `git clone` can
# die outright -- a work tree whose `.git/objects/info/alternates` names a path
# the container cannot resolve makes it exit 128 without leaving /work behind --
# and it can also return having produced a directory that is not this
# repository. Only the second one reaches the `pip install`, where it fails as
# something else entirely.
CLONE_SCRIPT = f"""
set -e
git config --global --add safe.directory /src
if ! git clone -q /src /work; then
    echo "{CLONE_FAILURE_MARKER}: git clone of /src into /work failed" >&2
    exit {EXIT_CLONE_FAILED}
fi
cd /work
if [ ! -f pyproject.toml ]; then
    echo "{CLONE_FAILURE_MARKER}: the clone of /src left no pyproject.toml in /work" >&2
    exit {EXIT_CLONE_FAILED}
fi
"""

SCRIPT = CLONE_SCRIPT + """
pip install -q -e '.[dev,can]'
exec python -m pytest "$@"
"""
# `bash -c` takes the word after the script as `$0`, not as `$1`. It is only a
# label for error messages; naming it is what keeps the first real argument in
# `"$@"`.
SCRIPT_ARGV0 = "ci_linux"


def repository_root() -> Path:
    """The work tree this file belongs to, so the tool works from any cwd."""
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=Path(__file__).resolve().parent,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return Path(__file__).resolve().parents[1]
    return Path(result.stdout.strip())


def docker_mount_source(root: Path) -> str:
    """The path in the form the Docker CLI expects on this platform.

    Docker Desktop on Windows takes `C:/path`; a POSIX absolute path passes
    through unchanged. `as_posix()` produces both from a `Path`.
    """
    return root.resolve().as_posix()


# pytest's last line is the only place it says what it did: "1476 passed, 35
# skipped in 41.23s", "no tests ran in 0.01s" when the collection came up empty,
# "37 tests collected in 0.42s" under --collect-only. The `=` rules around it in
# the non-quiet output are stripped before matching, and the duration is what
# distinguishes that line from every other line carrying a number.
_SUMMARY_DURATION = re.compile(r"\bin\s+\d+(?:\.\d+)?\s*(?:s|seconds)\b")
_SUMMARY_COUNT = re.compile(
    r"(\d+)\s+(?:passed|failed|errors?|skipped|xfailed|xpassed|deselected|tests?\s+collected)\b"
)
_NO_TESTS_RAN = re.compile(r"\bno tests ran\b", re.IGNORECASE)


def result_summary(output: str) -> str | None:
    """pytest's own summary line, or None if the run never reported one.

    The last one wins: a suite that reruns or a plugin that prints its own
    timings can leave more than one candidate behind, and pytest's is last.
    """
    summary = None
    for raw_line in output.splitlines():
        line = raw_line.strip().strip("=").strip()
        if not _SUMMARY_DURATION.search(line):
            continue
        if _NO_TESTS_RAN.search(line) or _SUMMARY_COUNT.search(line):
            summary = line
    return summary


def tests_accounted_for(summary: str) -> int:
    """How many tests pytest says it found, run or skipped alike.

    Skipped and deselected tests count: they were collected, and a selection
    that legitimately matches only skips is still a result. Zero is not.
    """
    return sum(int(count) for count in _SUMMARY_COUNT.findall(summary))


def evaluate_run(returncode: int, output: str) -> tuple[int, str | None]:
    """This tool's exit status for a finished container run, and why.

    The container's own status is forwarded whenever it means something -- a
    failing suite must leave this process with pytest's code. It is replaced
    only where it means nothing: a run that exits 0 having tested nothing is a
    green result for work that never happened, which is the one outcome this
    tool must never report.
    """
    if CLONE_FAILURE_MARKER in output:
        return (
            returncode or EXIT_CLONE_FAILED,
            "the container could not clone the repository into a usable tree, so pytest never ran",
        )
    summary = result_summary(output)
    if summary is None:
        return (
            returncode or EXIT_NO_RESULT,
            "the run printed no pytest summary line, so nothing is known to have been tested",
        )
    if tests_accounted_for(summary) == 0:
        return (returncode or EXIT_NO_RESULT, f"pytest collected no tests: {summary}")
    return returncode, None


def announce(text: str) -> None:
    """Say something in this tool's own voice, on the stream reserved for it.

    stderr, because stdout carries the container's output verbatim and a line
    this script wrote about itself must stay tellable from a line pytest wrote.
    """
    print(f"ci_linux: {text}", file=sys.stderr, flush=True)


def ci_lock_path() -> Path:
    """The one file every `ci_linux.py` on this machine queues on.

    `~/.agentic-hil/ci-linux.lock`: fixed, with no environment override, for the
    same reason the device locks have none. An override is how two runs stop
    seeing each other, and two runs that cannot see each other is the entire
    failure this file exists to prevent. The home directory is where every
    process of this user arrives without having to agree on a configuration or a
    checkout first, and this script is often run from several checkouts at once.

    Computing the path creates nothing; the directory appears when a lock is
    actually taken.
    """
    return Path(os.path.expanduser("~")) / CI_LOCK_ROOT_NAME / CI_LOCK_NAME


def process_is_running(pid: int) -> bool:
    """Whether `pid` is a live process on this machine, without disturbing it.

    Every uncertain answer is `True`, deliberately: this decides whether a lock
    may be broken, and breaking one wrongly starts the second container that
    starves the first. "I cannot tell" therefore means "leave it alone".
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
        # else is an answer this cannot read, and both mean "do not break it".
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


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def read_lock_record(path: Path) -> dict | None:
    """The holder record in a lock file, or None if there is nothing readable.

    None is a judgement about the file, not about the holder: a file that
    carries no JSON object was written by a process that died between creating
    it and describing itself, and nothing in it can name anybody.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    try:
        value = json.loads(text)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def describe_holder(record: dict | None) -> str:
    """The holder in the terms a waiting operator can act on."""
    if not record:
        return "a run whose lock file carries no readable record"
    who = f"pid {record.get('pid')} on {record.get('host') or 'an unnamed host'}"
    started_at = record.get("started_at")
    if started_at:
        who = f"{who}, started {started_at}"
    root = record.get("root")
    if root:
        who = f"{who}, in {root}"
    return who


def written_on_this_machine(record: dict) -> bool:
    """Whether this record's pid means anything here.

    An exact match, so a record that names no host at all is as foreign as one
    that names somebody else's: a home directory can be a roaming share, and a
    pid is only a name on the machine that issued it. Nothing this tool writes
    is missing the field, so a record without one was not written by it and is
    not something to judge a live run by.
    """
    return record.get("host") == socket.gethostname()


def holder_notice(record: dict | None) -> str:
    """What is said about a lock this run may not take."""
    text = f"another run holds the lock: {describe_holder(record)}"
    if record and record.get("version") != CI_LOCK_RECORD_VERSION:
        return f"{text}. That record was written by a different version of this tool, so its holder is not judged from here"
    if record and not written_on_this_machine(record):
        return f"{text}. That record was not written on this machine, so this one cannot tell whether the process is still running"
    return text


def _empty_lock_is_still_being_written(path: Path) -> bool:
    """Whether an empty or unreadable lock file is young enough to be a holder
    still writing its record rather than one that died mid-write.

    A create publishes the path and then writes the record into it; a reader
    catching that instant sees no record and cannot tell a write in progress
    from an abandoned one. The machine guard serialises this run's own create
    against its own reads, but a run on an older checkout that creates outside
    the guard writes where this guard does not reach, so an empty file another
    run just published can still be read here -- and only the file's age tells
    a live publication from a dead one. The age is read from the modification
    time, set when the create makes the path and untouched until the record
    lands, so the clock starts at publication rather than at this read. A file
    already gone is not being written; a stat that fails for any other reason is
    left alone, because treating an unreadable file as breakable is the unsafe
    direction.
    """
    try:
        published_at = path.stat().st_mtime
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return (time.time() - published_at) < EMPTY_LOCK_GRACE_S


def stale_reason(record: dict | None, path: Path) -> str | None:
    """Why this lock may be broken, or None if it may not be.

    Three ways a lock survives its run: the file exists but carries nothing (the
    holder died while writing it), it names no usable pid, or it names one that
    is no longer running. Everything else is somebody else's lock, including a
    record from another machine and a record written by a version of this tool
    that this one does not understand.

    An empty or unreadable file is only judged abandoned once it has aged past
    the publication window: a file that has just appeared may be a run -- this
    one's or an older checkout's, guarded or not -- part way through its own
    create, and breaking it would delete a record that is about to name a live
    holder. That case is waited on, not removed.
    """
    if record is None:
        if _empty_lock_is_still_being_written(path):
            return None
        return f"the lock file at {path} carries no readable holder record, so it is being removed"
    if record.get("version") != CI_LOCK_RECORD_VERSION:
        return None
    if not written_on_this_machine(record):
        return None
    pid = record.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return f"the lock file at {path} names no usable holder pid, so it is being removed"
    if process_is_running(pid):
        return None
    return f"the lock at {path} was held by pid {pid}, started {record.get('started_at')}, which is no longer running, so it is being removed"


def wait_for_the_holder(seconds: float) -> None:
    """The pause between two attempts on the lock.

    Its own function so a test can drive the queue without spending the time,
    which is what keeps "two runs serialize" a deterministic assertion rather
    than a race between two sleeps.
    """
    time.sleep(seconds)


def _lock_fd_exclusive(descriptor: int) -> None:
    """Block until this process holds `descriptor`'s exclusive lock.

    POSIX `flock` and Windows `msvcrt.locking` are chosen for the one property
    that makes the guard safe: the operating system drops the lock when the
    descriptor is closed *or the process exits*, crash included. There is
    therefore no stale guard to break in turn -- the failure the record lock
    goes to such lengths to detect cannot happen here, because a dead holder of
    this lock is not holding it. The lock is advisory and the descriptor is
    private to this process; nothing outside this tool ever sees it.
    """
    if os.name == "nt":
        import msvcrt

        # `locking` works from the current offset; a byte at the start is the
        # whole of it. LK_LOCK blocks, retrying, until the byte is free. A run
        # holds the guard only for the moment of a takeover, so the wait a
        # contender sees here is that moment, not a queued suite.
        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_LOCK, 1)
    else:
        import fcntl

        fcntl.flock(descriptor, fcntl.LOCK_EX)


@contextmanager
def _machine_wide_guard(path: Path):
    """Hold the machine-wide guard for the length of the `with` block.

    Creating the lock file, reading the holder record, judging it dead and
    replacing it has to be one indivisible step. It is not, on its own: two runs
    that both find the same dead holder each remove the file and each create it,
    and the one that returns first is holding a lock the second is about to
    delete out from under it -- two containers on one daemon, the single outcome
    this file exists to prevent. A plain create is no safer alone: it publishes
    the path with `O_EXCL` an instant before it writes the record, and a
    contender that reads in that instant sees an empty file and calls it stale.
    `O_EXCL` cannot close either window, because the loser's confirming read
    comes after the winner has already been told it holds the lock.

    So the takeover runs under an OS lock that only one run on the machine can
    hold at a time. It is deliberately not the record lock grown a second job:
    that one must survive this process to name a running container to the next
    run, and this one must not survive it at all. An empty sentinel both runs
    open by the same path, locked and released by the kernel, is the whole of
    it. The file is created if absent and never removed; it carries nothing to
    remove.
    """
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        _lock_fd_exclusive(descriptor)
        yield
    finally:
        # Closing the descriptor releases the OS lock; the sentinel file stays.
        os.close(descriptor)


class RunLockBusy(RuntimeError):
    """Somebody else holds the machine, and the refusal names them."""

    def __init__(self, record: dict | None):
        super().__init__(holder_notice(record))
        self.record = record


class RunLock:
    """One machine's permission to run a container, held for the whole run.

    Ownership is the existence of the lock file, created with `O_EXCL` so two
    runs cannot both create it, and carrying the record that lets a contender
    name the holder or judge it dead. The record is a pid in a file rather than
    an OS lock held for the whole run, on purpose: this script is standalone by
    design, it runs from checkouts that may not even be installed, and a run has
    to be able to say *which* checkout is holding the machine to somebody
    reading a queued run -- a whole-run flock names nobody. The cost of that
    choice is exactly one case, spelled out where it is paid: a holder that
    crashed and whose pid has since been reused looks alive, and the next run
    queues instead of starting. It waits; it never runs twice.

    Creating the file, reading a holder, judging it dead and replacing it are
    the steps the record cannot make atomic on its own, and they run under a
    short-lived OS lock that the kernel releases the instant this process exits
    (see `_machine_wide_guard`). That guard is not the bench mutex and not the
    record lock; it is held only while a run takes the lock or breaks a dead one,
    never during the run itself.
    """

    def __init__(self, *, path: Path | None = None, root: Path | None = None, image: str = "", pytest_args: list[str] | None = None):
        self.path = ci_lock_path() if path is None else path
        # The guard lives beside the record lock, so a test that redirects one
        # by pointing `path` elsewhere redirects the other with it.
        self.guard_path = self.path.with_name(CI_LOCK_GUARD_NAME)
        self.record = {
            "version": CI_LOCK_RECORD_VERSION,
            "owner_id": secrets.token_hex(8),
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "started_at": utc_now_iso(),
            "root": "" if root is None else root.as_posix(),
            "image": image,
            "pytest_args": list(pytest_args or []),
        }
        self.held = False

    def acquire(self, *, wait: bool = True) -> None:
        """Hold the machine, queueing behind a live holder if asked to.

        Raises RunLockBusy when a live holder is in the way and `wait` is false.
        """
        said: set[str] = set()
        announced_at: float | None = None
        waited_from = time.monotonic()
        while True:
            record = None
            # Create, read, judge-stale and break all run under the one
            # machine-wide guard. A create publishes the path with `O_EXCL` for
            # the instant before it writes the record into it; taking the guard
            # across that instant is what stops a contender from failing its own
            # create, reading the half-made file, calling it stale, and breaking
            # a lock whose creator is about to return holding it. No run writes
            # this file except under the guard and no run reads it except under
            # the guard, so a reader sees a whole record or no file, never the
            # empty moment between a create and its write.
            with _machine_wide_guard(self.guard_path):
                if self._create():
                    self._mark_held(announced_at, waited_from)
                    return
                record = read_lock_record(self.path)
                if record and record.get("owner_id") == self.record["owner_id"]:
                    # This run's own record, which it can only be reading because
                    # the read that confirms a create came back empty once. The
                    # lock is ours and always was; without this, a run would
                    # queue behind itself and find a holder that stays alive as
                    # long as it waits.
                    self.held = True
                    return
                stale = stale_reason(record, self.path)
                if stale is not None:
                    if stale not in said:
                        announce(stale)
                        said.add(stale)
                    self._break()
                    if self._create():
                        self._mark_held(announced_at, waited_from)
                        return
                    # The break freed the path and this run's own create still
                    # did not take it. No other run can have slipped in under the
                    # guard, so this is the confirming read failing on the record
                    # this run just wrote; loop, read it back under the guard and
                    # recognise it as ours.
                    continue
            if not wait:
                raise RunLockBusy(record)
            now = time.monotonic()
            if announced_at is None:
                announce(f"{holder_notice(record)}. Waiting for it to finish; pass --no-wait to refuse instead of queueing")
                announced_at = now
            elif now - announced_at >= LOCK_NOTICE_INTERVAL_S:
                announce(f"still waiting after {round(now - waited_from)}s: {describe_holder(record)}")
                announced_at = now
            wait_for_the_holder(LOCK_POLL_INTERVAL_S)

    def _mark_held(self, announced_at: float | None, waited_from: float) -> None:
        """Record that the lock is now ours, saying so if a wait preceded it."""
        self.held = True
        if announced_at is not None:
            announce(f"the lock is free after {round(time.monotonic() - waited_from)}s of waiting; starting")

    def release(self) -> None:
        """Give the machine back, and only if it is still ours to give.

        A lock this run no longer owns is left alone: if its record has been
        replaced, this run was judged dead and broken by somebody who is now
        holding the file, and deleting it would hand the machine to a third run
        while that one is mid-container.
        """
        if not self.held:
            return
        self.held = False
        if self._is_ours():
            with suppress(OSError):
                os.unlink(self.path)

    def _create(self) -> bool:
        """Try to become the holder, and confirm that this run actually did.

        Always called under the machine-wide guard (see `acquire`), so all of it
        -- publishing the path with `O_EXCL`, writing the record, and confirming
        it -- is one step no other run can read into the middle of. `O_EXCL`
        stays as the backstop it always was: the create fails rather than
        truncating whatever a path already holds, so a run is never the one that
        made a file some other run is using.

        The confirming read stays for the one thing the guard does not cover: a
        filesystem that hands back the create's own descriptor and then fails
        the read that follows. A run that cannot see its own record must not
        report that it holds the lock; it returns False, re-reads under the
        guard, and recognises the record as its own there.
        """
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            return False
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(self.record, indent=2) + "\n")
        return self._is_ours()

    def _is_ours(self) -> bool:
        written = read_lock_record(self.path)
        return bool(written) and written.get("owner_id") == self.record["owner_id"]

    def _break(self) -> None:
        """Remove a lock whose holder is gone. Called only under the guard.

        Because the guard admits one run to the takeover at a time, the record
        this removes is exactly the one `acquire` read and judged dead a moment
        earlier under the same guard -- no other run can have replaced it in
        between. A file already gone is tolerated all the same, so a lock a
        holder cleared itself while dying is not a crash here; any other failure
        is real and is raised, because a stale lock that cannot be removed has
        to be said out loud, not waited on forever.
        """
        with suppress(FileNotFoundError):
            os.unlink(self.path)


def run_container(command: list[str]) -> tuple[int, str]:
    """Run the container, echoing its output as it arrives and keeping a copy.

    The output is read rather than inherited because the exit status alone
    cannot tell a suite that failed from a suite that never ran. stderr is
    merged into it: the clone step reports its own failure there, and that
    report is what names the step.
    """
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        errors="replace",
    )
    captured: list[str] = []
    for line in process.stdout:
        captured.append(line)
        sys.stdout.write(line)
        sys.stdout.flush()
    return process.wait(), "".join(captured)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--python", default=DEFAULT_PYTHON, help=f"Python version for the container image (default {DEFAULT_PYTHON}).")
    parser.add_argument("--image", default=None, help="Container image, overriding --python entirely.")
    parser.add_argument(
        "--no-wait",
        action="store_true",
        help="Refuse instead of queueing when another run holds this machine.",
    )
    parser.add_argument("pytest_args", nargs=argparse.REMAINDER, help="Passed to pytest unchanged.")
    options = parser.parse_args(argv)

    if shutil.which("docker") is None:
        print(
            "docker was not found on PATH.\n"
            "This tool is optional: it reproduces the Linux CI job locally, which is the\n"
            "only way a Windows machine sees the POSIX half of the suite. Without it, say\n"
            "in the pull request which tests you could not run.",
            file=sys.stderr,
        )
        return EXIT_NO_DOCKER

    root = repository_root()
    image = options.image or IMAGE.format(version=options.python)
    # `--` separates our options from pytest's, and argparse.REMAINDER keeps it,
    # so drop the leading one and nothing else: pytest uses a `--` of its own to
    # end its option parsing, and a filter over the whole list ate that too — so
    # `ci_linux.py -- tests -- -x` reached pytest as `tests -x`, which is not
    # "everything after the options, unchanged".
    forwarded = list(options.pytest_args)
    if forwarded and forwarded[0] == "--":
        forwarded.pop(0)

    # --init, or `bash -c` is PID 1 and reaps nothing: a SIGKILLed process-group
    # leader then lingers as a zombie that still counts as a live group member,
    # and test_process_cleanup_handles_exited_group_leader fails in the container
    # while passing everywhere an init exists. The CI runners have one; give the
    # container one too so this runs what CI runs.
    command = [
        "docker",
        "run",
        "--rm",
        "--init",
        "-v",
        f"{docker_mount_source(root)}:/src:ro",
        image,
        "bash",
        "-c",
        SCRIPT,
        SCRIPT_ARGV0,
        *(forwarded or ["-q"]),
    ]
    # Taken before the run is announced and released only after it is over, so
    # the window the lock covers is exactly the window a container is on the
    # daemon. Everything above it is argument handling that costs nothing and
    # should not make a queued run wait for it.
    lock = RunLock(root=root, image=image, pytest_args=forwarded or ["-q"])
    try:
        lock.acquire(wait=not options.no_wait)
    except RunLockBusy as busy:
        announce(f"{busy}. Refusing, because --no-wait was given")
        return EXIT_LOCKED
    except OSError as error:
        announce(f"the lock at {lock.path} could not be taken: {error}")
        return EXIT_LOCKED
    try:
        print(f"{image}: the committed tree at {root}", flush=True)
        returncode, output = run_container(command)
    finally:
        lock.release()
    status, reason = evaluate_run(returncode, output)
    if reason is not None:
        announce(reason)
    return status


if __name__ == "__main__":  # pragma: no cover - entry point
    raise SystemExit(main())
