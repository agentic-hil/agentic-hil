"""One machine's permission to put a container on its Docker daemon.

Two tools in this directory start long-lived containers against the one daemon
this machine has: `ci_linux.py` runs the suite the way the Linux CI runners do,
and `loop_in_container.py` runs the agent review loop. Three or four of those at
once starve each other until a run dies mid-flight, so they take turns, and they
can only take turns if they queue on the same thing. This module is that thing,
imported by both rather than implemented in each: two mechanisms that do not
read each other's records are two runs that cannot see each other, which is the
entire failure being prevented.

Both tools are standalone scripts run from checkouts that may not be installed,
so this is stdlib only and lives beside them.

Who reads the lock file: those two scripts, and nothing else. It is written by
one of them on an operator's own machine, read by the next run of either on that
machine, and understood by neither the MCP server nor any other tool in this
repository.

It is deliberately *not* a device lock. The bench mutex lives one level further
down, in `~/.agentic-hil/device-locks/`, is owned by the server, and is the one
`AGENTS.md` says never to delete by hand. This one holds no hardware: it holds a
Docker daemon's capacity, and deleting it while no container of these scripts is
running costs nothing.

Format: one JSON object, UTF-8, indented so it can be read in a terminal.

    {
      "version": 1,
      "owner_id": "9f2c...",      random, so a holder can recognise its own record
      "pid": 21044,               the process to check for liveness, on `host` only
      "host": "BENCH-01",         a pid from another machine cannot be judged here
      "started_at": "2026-09-01T09:12:33Z",
      "root": "C:/Users/dev/work/agentic-hil",   which checkout is running
      "tool": "tools/ci_linux.py",               which of them is holding it
      "runs_for": "6 to 7 minutes",              what the wait is likely to cost
      "image": "python:3.12",
      "pytest_args": ["-q"]
    }

The absolute path is in there on purpose: the first question of somebody looking
at a queued run is which of their checkouts is holding it, and this file never
leaves the machine that wrote it. `tool` and `runs_for` are there for the second
question, which only exists now that two tools share the file: a suite run and a
review loop are minutes and hours apart, and a queue message that cannot tell
them apart tells whoever is waiting nothing about whether to wait. The holder
writes both, because the holder is the only run that knows what it started.
"""

from __future__ import annotations

import json
import os
import secrets
import socket
import sys
import time
from collections.abc import Callable
from contextlib import contextmanager, suppress
from datetime import datetime, timezone
from pathlib import Path

LOCK_ROOT_NAME = ".agentic-hil"
# The file keeps the name `ci_linux.py` gave it when it was the only tool that
# took it. Renaming it to something the loop is also named by would be tidier
# and would cost the one property the file has: a checkout from before the loop
# shared it goes on queueing at this exact path, and a run that queues somewhere
# else is a run the others cannot see.
LOCK_NAME = "ci-linux.lock"
# A second file beside the lock, and a different kind of thing: never a record,
# never removed, holding nobody. Runs take a machine-wide OS lock on it for the
# split second in which the lock file is created, or a dead one read, judged and
# replaced, so that step is indivisible across every run on this machine. The
# record lock says *who* holds the machine; this one serialises the moments two
# runs could each undo the other's takeover, or one read another's half-written
# record. See `_machine_wide_guard`.
LOCK_GUARD_NAME = "ci-linux.lock.guard"
# Bumped when a reader of this file could otherwise misread it. An unknown
# version is never judged stale: a newer checkout of this repository running
# beside an older one is an ordinary day here, and the older one must queue
# behind it rather than delete its lock. Adding `tool` and `runs_for` did not
# bump it, because a reader that does not know those fields does not misread the
# record: it reads every field it judges by exactly as before and merely says
# less about the holder. Bumping would have been the expensive answer, since a
# version an older checkout cannot read is also a record it will not break when
# its holder dies.
LOCK_RECORD_VERSION = 1
LOCK_POLL_INTERVAL_S = 2.0
# A wait is not bounded, because the thing being waited for is a full suite or a
# review loop and a queue of three legitimately takes an hour; it is loud
# instead, repeating who holds the lock so a wait can never be mistaken for a
# hang. `--no-wait` is the way out for somebody who knows better.
LOCK_NOTICE_INTERVAL_S = 60.0
# The one field that turns a holder record into a machine no run may take over.
# A run that exits with a container it could not confirm gone leaves it: the
# daemon is still in use, but the pid that held the lock is about to die, and a
# plain record naming a dead pid is exactly what the next run breaks on liveness
# -- onto a daemon the leftover container never left, the starvation this whole
# file exists to prevent. `stale_reason` never breaks a record carrying this,
# and `holder_notice` explains it: it is cleared by an operator who has stopped
# the container, never automatically.
CLEANUP_REQUIRED_FIELD = "cleanup_required"

# Windows, where `os.kill(pid, 0)` is not a liveness probe: CPython maps every
# signal other than the console events onto TerminateProcess, so asking that
# question there kills the run being asked about.
_WINDOWS_SYNCHRONIZE = 0x00100000
_WINDOWS_WAIT_OBJECT_0 = 0x00000000
_WINDOWS_ERROR_INVALID_PARAMETER = 87


def announce(text: str) -> None:
    """The fallback voice, for a lock nobody handed one to.

    Each tool passes its own, so a line about the queue is tellable from a line
    the container printed. stderr for the same reason theirs use it: stdout
    carries the run's own output.
    """
    print(f"run lock: {text}", file=sys.stderr, flush=True)


def lock_path() -> Path:
    """The one file every containerised run on this machine queues on.

    `~/.agentic-hil/ci-linux.lock`: fixed, with no environment override, for the
    same reason the device locks have none. An override is how two runs stop
    seeing each other, and two runs that cannot see each other is the entire
    failure this file exists to prevent. The home directory is where every
    process of this user arrives without having to agree on a configuration or a
    checkout first, and these scripts are often run from several checkouts at
    once.

    Computing the path creates nothing; the directory appears when a lock is
    actually taken.
    """
    return Path(os.path.expanduser("~")) / LOCK_ROOT_NAME / LOCK_NAME


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
    """The holder in the terms a waiting operator can act on.

    The pid and host lead, because they are what identifies the run. What it is
    and how long it takes come after: a record from a checkout that predates two
    tools sharing this file carries neither, and a notice that has to name them
    to say anything at all would have nothing to say about the runs most likely
    to be holding the machine while the older checkout is still around.
    """
    if not record:
        return "a run whose lock file carries no readable record"
    who = f"pid {record.get('pid')} on {record.get('host') or 'an unnamed host'}"
    started_at = record.get("started_at")
    if started_at:
        who = f"{who}, started {started_at}"
    root = record.get("root")
    if root:
        who = f"{who}, in {root}"
    tool = record.get("tool")
    if tool:
        who = f"{who}, running {tool}"
    runs_for = record.get("runs_for")
    if tool and runs_for:
        # The scale of the wait, from the only run that knows it. A suite run is
        # minutes and a review loop is hours, and somebody deciding whether to
        # queue or come back later is deciding on exactly that difference.
        who = f"{who}, which usually takes {runs_for}"
    return who


def written_on_this_machine(record: dict) -> bool:
    """Whether this record's pid means anything here.

    An exact match, so a record that names no host at all is as foreign as one
    that names somebody else's: a home directory can be a roaming share, and a
    pid is only a name on the machine that issued it. Nothing these tools write
    is missing the field, so a record without one was not written by them and is
    not something to judge a live run by.
    """
    return record.get("host") == socket.gethostname()


def holder_notice(record: dict | None, path: Path | None = None) -> str:
    """What is said about a lock this run may not take.

    An unreadable record gets its own notice rather than the "another run holds
    it" line: this run genuinely cannot tell whether a holder is still writing
    the file or it is a corrupt leftover, and it never removes a file it cannot
    read, so it says so and points at the one safe way out -- an operator who
    knows no run is using the machine removing the file by hand.
    """
    if record is None:
        where = f" at {path}" if path is not None else ""
        return (
            f"the lock file{where} carries no readable holder record. This run cannot tell a "
            "holder still writing it -- including one on an older checkout that creates outside "
            "this machine's guard -- from a corrupt leftover, and never removes a file it cannot "
            "read. If you are certain no container run of these tools is using this machine, "
            "remove the file by hand"
        )
    text = f"another run holds the lock: {describe_holder(record)}"
    cleanup = record.get(CLEANUP_REQUIRED_FIELD)
    if cleanup:
        # Judged before version or host, because the reason it may not be taken
        # over is neither of those: a run left a container it could not confirm
        # removed, so the machine is in use however dead the pid now looks. The
        # only way out is an operator who has stopped that container.
        return (
            f"{text}. That run exited with a container it could not confirm removed, so the machine is still in "
            f"use and this lock is not taken over from here: {cleanup} Once no container of these tools is "
            "running, remove the lock file by hand"
        )
    if record.get("version") != LOCK_RECORD_VERSION:
        return f"{text}. That record was written by a different version of this tool, so its holder is not judged from here"
    if not written_on_this_machine(record):
        return f"{text}. That record was not written on this machine, so this one cannot tell whether the process is still running"
    return text


def stale_reason(record: dict | None, path: Path) -> str | None:
    """Why this lock may be broken, or None if it may not be.

    A lock is broken only when this machine can prove its holder is gone: a
    record written here, by this version of the tool, naming a pid that is no
    longer running, or one whose pid these tools could never have written at
    all. Everything else is left alone -- a record from another machine, one
    written by a version this does not understand, and, most carefully, a file
    that carries no readable record.

    An empty or unreadable file is never broken. Age cannot prove its writer has
    exited and neither can a re-read: a run on an older checkout creates outside
    this machine's guard, so it can publish the path and then be descheduled for
    any length of time before it writes its record, and a file that has sat empty
    for a minute may still name a live holder the instant that run resumes. A
    current run never leaves an empty file -- `_create` publishes its record whole
    and atomically -- so an unreadable lock here is a legacy create in flight or a
    corrupt leftover, and both are waited on until the holder finishes or an
    operator removes the file by hand, rather than ever being taken over from
    here. Breaking one on age alone is the double-acquire this whole mechanism
    exists to prevent.

    A record marked cleanup-required is never broken either, and for the sharper
    version of the same reason: its holder is provably gone -- the pid is dead --
    and the machine is still in use anyway, because the run that left it could not
    confirm its container removed. Liveness is exactly the wrong question there,
    so it is not asked; an operator stops the container and clears the file (see
    `RunLock.retain_for_cleanup`).
    """
    if record is None:
        return None
    if record.get(CLEANUP_REQUIRED_FIELD):
        return None
    if record.get("version") != LOCK_RECORD_VERSION:
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
    private to this process; nothing outside these tools ever sees it.
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

    Reading the holder record, judging it dead, breaking it and creating a new
    one in its place has to be one indivisible step. It is not, on its own: two
    runs that both find the same dead holder each remove the file and each create
    it, and the one that returns first is holding a lock the second is about to
    delete out from under it -- two containers on one daemon, the single outcome
    this file exists to prevent. The create itself no longer opens a window of
    its own: `_create` publishes its record whole and atomically, so no reader
    ever catches the file empty and calls a write in progress a dead lock. What
    is left for the guard is the takeover -- read, judge, break, create as one
    unit -- which the atomic create cannot serialise on its own, because the
    loser's break can still fall between the winner's create and the winner being
    told it holds the lock.

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

    def __init__(self, record: dict | None, path: Path | None = None):
        super().__init__(holder_notice(record, path))
        self.record = record


class RunLock:
    """One machine's permission to run a container, held for the whole run.

    Ownership is the existence of the lock file, published whole in one atomic
    step so two runs cannot both create it and no reader ever catches it empty,
    and carrying the record that lets a contender name the holder or judge it
    dead. The record is a pid in a file rather than an OS lock held for the whole
    run, on purpose: these scripts are standalone by design, they run from
    checkouts that may not even be installed, and a run has to be able to say
    *which* checkout is holding the machine to somebody reading a queued run -- a
    whole-run flock names nobody. The cost of that
    choice is exactly one case, spelled out where it is paid: a holder that
    crashed and whose pid has since been reused looks alive, and the next run
    queues instead of starting. It waits; it never runs twice.

    Reading a holder, judging it dead, breaking it and creating the next in its
    place are the steps the record cannot make atomic on its own -- the create
    alone now is, published whole in one step -- and they run under a short-lived
    OS lock that the kernel releases the instant this process exits (see
    `_machine_wide_guard`). That guard is not the bench mutex and not the
    record lock; it is held only while a run takes the lock or breaks a dead one,
    never during the run itself.

    `tool` and `runs_for` are what this run tells the next one about itself: the
    script that is holding the machine, and the scale of the wait it is asking
    for. `details` is whatever else that tool wants a queued operator to see; it
    can never overwrite a field the queue judges by.
    """

    def __init__(
        self,
        *,
        tool: str = "",
        runs_for: str = "",
        details: dict[str, object] | None = None,
        path: Path | None = None,
        root: Path | None = None,
        announce: Callable[[str], None] = announce,
    ):
        self.path = lock_path() if path is None else path
        # The guard lives beside the record lock, so a test that redirects one
        # by pointing `path` elsewhere redirects the other with it.
        self.guard_path = self.path.with_name(LOCK_GUARD_NAME)
        self.announce = announce
        self.record: dict[str, object] = {
            "version": LOCK_RECORD_VERSION,
            "owner_id": secrets.token_hex(8),
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "started_at": utc_now_iso(),
            "root": "" if root is None else root.as_posix(),
            "tool": tool,
            "runs_for": runs_for,
        }
        for key, value in (details or {}).items():
            # setdefault rather than update: a tool's own payload describes the
            # run, and nothing in it may replace the pid, host or version a
            # contender judges this record by.
            self.record.setdefault(key, value)
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
            # Read, judge-stale, break and create all run under the one
            # machine-wide guard, so two runs that both find the same dead holder
            # cannot each break it and each create in its place: the guard admits
            # one takeover at a time, and the loser reads the winner's live record
            # instead of racing it. The create publishes a whole record
            # atomically (see `_create`), so a reader never catches this file
            # empty and mistakes a write in progress for a dead lock -- guarded or
            # not, on this checkout or an older one.
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
                        self.announce(stale)
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
                raise RunLockBusy(record, self.path)
            now = time.monotonic()
            if announced_at is None:
                self.announce(f"{holder_notice(record, self.path)}. Waiting for it to finish; pass --no-wait to refuse instead of queueing")
                announced_at = now
            elif now - announced_at >= LOCK_NOTICE_INTERVAL_S:
                self.announce(f"still waiting after {round(now - waited_from)}s: {describe_holder(record)}")
                announced_at = now
            wait_for_the_holder(LOCK_POLL_INTERVAL_S)

    def _mark_held(self, announced_at: float | None, waited_from: float) -> None:
        """Record that the lock is now ours, saying so if a wait preceded it."""
        self.held = True
        if announced_at is not None:
            self.announce(f"the lock is free after {round(time.monotonic() - waited_from)}s of waiting; starting")

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

    def retain_for_cleanup(self, detail: str) -> None:
        """Keep the machine held when a container may still be on the daemon.

        The one exit `release` must not take. `release` deletes the record, and a
        deleted record is a free machine: the next run takes it and starts a
        second container on a daemon the first one's container never left. Simply
        declining to delete is not enough -- this process is about to exit, and
        the next run judges a record whose pid is no longer running stale and
        breaks it (see `stale_reason`). So the record is rewritten in place to one
        carrying `CLEANUP_REQUIRED_FIELD`, which neither `stale_reason` nor a
        waiting acquirer will take over: the machine stays held until an operator
        stops the leftover container and removes the file by hand. `detail` is
        what that operator, and the next run's queue notice, are told about it.

        The rewrite is atomic -- a whole record staged and `os.replace`d over the
        lock, so no reader catches the file mid-write -- and it runs while this
        process is still alive, so a contender either reads the live pid before it
        or the cleanup mark after it, never a dead pid without the mark. Called
        from a cleanup path that must not raise: if the record is no longer this
        run's, or the rewrite fails, the file is left as it stands and the reason
        is announced.
        """
        if not self.held:
            return
        self.held = False
        if not self._is_ours():
            return
        record = dict(self.record)
        record[CLEANUP_REQUIRED_FIELD] = detail
        staging = self.path.with_name(f"{self.path.name}.{self.record['owner_id']}.cleanup")
        try:
            descriptor = os.open(staging, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(record, indent=2) + "\n")
            os.replace(staging, self.path)
        except OSError as error:
            with suppress(OSError):
                os.unlink(staging)
            self.announce(
                f"could not mark the lock at {self.path} as needing cleanup ({error}); it is left as it stands, "
                "so the next run may break it on liveness. Stop any leftover container and remove the file by hand"
            )

    def _create(self) -> bool:
        """Try to become the holder by publishing a complete record atomically.

        The record is written in full to a private staging file and then linked
        into the lock path in a single step, so the lock file is never seen empty
        -- there is no instant between the path appearing and its record landing
        for any reader to catch. That is what lets an empty file be judged
        unbreakable everywhere (see `stale_reason`): a current run cannot be the
        one that left it. `os.link` fails if the lock path already exists, which
        is how a create loses to a holder -- the fail-if-exists role `O_EXCL`
        played when the create was a create-then-write.

        The staging file is named for this run's `owner_id`, so no two runs share
        one, and it is removed whether the link wins or loses; a crash before the
        link leaves the staging file, never the lock, and a crash after it leaves
        a complete record naming a pid the next run can prove dead. The confirming
        read stays for the one thing the link does not cover: a filesystem that
        reports the link succeeded and then fails the read that follows. A run
        that cannot see its own record must not report that it holds the lock; it
        returns False, re-reads under the guard, and recognises the record there.
        """
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        staging = self.path.with_name(f"{self.path.name}.{self.record['owner_id']}.new")
        descriptor = os.open(staging, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(self.record, indent=2) + "\n")
            try:
                os.link(staging, self.path)
            except FileExistsError:
                return False
        finally:
            with suppress(FileNotFoundError):
                os.unlink(staging)
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
