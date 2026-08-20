"""Drive a Claude Code implement/commit + Codex review loop until the review is clean.

The loop is deliberately asymmetric. Claude Code owns the working tree: it edits,
runs whatever checks the repository defines, and commits. Codex never edits code;
it reads the commits produced in that round, writes a review document, and returns
a machine-readable verdict. The next Claude round is handed that document as its
task. The loop ends when a review reports no findings, or when the configured
number of rounds is exhausted -- whichever comes first.

The two agents do not share a working tree. The reviewer gets a checkout of its
own at the commit under review, so its test runs, caches and temp directories
never land in the tree the implementer is about to commit, and "do not edit the
code" stops being a promise the prompt has to extract. The one path it may write
outside that checkout is the review document the next implementer round reads.

Four further properties make it safe to run unattended:

* Every round must produce at least one commit, and a round that produced none is
  classified against the working tree rather than assumed. A tree holding nothing
  the round did not start with is an implementer that looked and declined: a
  stalled loop rather than progress, so the run stops instead of burning the
  remaining rounds on an agent that has given up. Anything new is the opposite
  case, and was read as the same one: a round's work sitting uncommitted because
  the implementer stopped between its last edit and its `git commit`. That work
  is handed back to be finished and committed, and committed here if it comes
  back uncommitted a second time. What the tree already held is subtracted rather
  than counted, because under `--review-checkout none` the reviewer works in that
  same tree and its leavings would otherwise make every declined round a stall;
  `--no-stall-retry` reports such a round instead of handing it back, for a run
  that cannot spare the second invocation.
* The verdict is parsed from a JSON schema Codex is required to satisfy, not from
  prose. Prose like "looks good apart from ..." has no stable meaning to a loop
  condition, and guessing at it is how a loop exits one round before the bug.
* A round that cannot finish still commits. An implementer killed at `--timeout`
  leaves a whole round's work uncommitted, because a round commits once and at
  the end; that commit is made here instead, marked in its subject as the
  unfinished thing it is, so the next run starts from it.
* The loop stops when it stops contributing. `--max-rounds` counts rounds and
  cannot tell a round that closed four findings from one that moved four around,
  so `--stop-after-flat-rounds` watches the finding count instead.

The two agents do not have the same reach, either, and the run header says so:
Claude Code has the network and can read a linked issue, while Codex under
`--sandbox workspace-write` cannot. A task that points at a URL is complete for
the agent writing the code and empty for the one checking it against the intent.

Example:

    python tools/agent_review_loop.py \
        --task "Add a --timeout option to agentic-hil probe" \
        --claude-model opus \
        --codex-model gpt-5.1-codex \
        --max-rounds 5

Both agents run non-interactively with permissions pre-granted, so run this only
in a repository you are willing to let them commit to.
"""

from __future__ import annotations

import argparse
import contextlib
import ctypes
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from ctypes import wintypes
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import IO

CLEAN = "clean"
CHANGES_REQUESTED = "changes_requested"

# Codex is pinned to this shape so the loop condition reads a value, not a mood.
VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": [CLEAN, CHANGES_REQUESTED]},
        "findings": {"type": "integer", "minimum": 0},
        "review_file": {"type": "string"},
        "summary": {"type": "string"},
    },
    "required": ["status", "findings", "review_file", "summary"],
    "additionalProperties": False,
}

# Fallback for a Codex build that ignores --output-schema: the prompt also asks
# for this sentinel, and a loop that cannot read a verdict has to stop.
SENTINEL = re.compile(r"REVIEW_STATUS:\s*(CLEAN|CHANGES_REQUESTED)", re.IGNORECASE)

EXIT_CLEAN = 0
EXIT_ROUNDS_EXHAUSTED = 1
EXIT_STALLED = 2
EXIT_FAILED = 3
EXIT_NO_PROGRESS = 4

# Written into each directory the loop keeps its own paperwork in. `*` covers
# everything in the directory that holds it, this file included.
PAPERWORK_IGNORE = """\
# Written by tools/agent_review_loop.py. Everything here belongs to the review
# loop rather than to this repository: review documents, agent transcripts and
# run summaries. Ignoring it keeps the next run's preflight from refusing a tree
# this run dirtied, and keeps an agent's `git add -A` from committing the last
# run's paperwork along with its work.
*
"""

# How the salvage commit a cut-short round leaves behind is marked. Nothing
# downstream should mistake it for finished work, so it says so in the subject.
SALVAGE_SUBJECT = "wip(review-loop): round {number} was cut short before the implementer committed"
# The same, for a round whose implementer came back, left its work in the tree,
# and did not commit it even when handed it back to finish. A different sentence
# because it is a different failure: nothing was cut short, the round simply
# never reached its own commit.
STALL_SUBJECT = "wip(review-loop): round {number} left the implementer's work uncommitted"

# Every implementer prompt carries this, because the stall it describes is the
# one that cost a round. The transcript of that round has the agent announcing
# that it was waiting for the test suite to notify it, and then nothing until the
# timeout: the work was finished, and the `git commit` at the end of the round
# never happened. Nothing here sends an agent a notification, so waiting for one
# is waiting for the whole `--timeout`.
FOREGROUND_ONLY = """
FOREGROUND ONLY
Run every command in the foreground and wait for its own output. Do not put a
command in the background, and do not wait on a notification, a monitor, or a
completion event: nothing in this harness delivers one, so an agent that waits
for one waits until --timeout kills the round with its work uncommitted. That
includes the long ones. Wait for the command itself.
"""

# Codex turns network access on for `workspace-write` through this one setting,
# so a run that has it does not need to be told the reviewer is offline.
NETWORK_ENABLED = re.compile(r"^sandbox_workspace_write\.network_access\s*=\s*(true|1)\s*$", re.IGNORECASE)
# What a task description looks like when it points somewhere instead of saying
# what it means. The reviewer usually cannot follow either of these.
POINTS_OUTWARD = re.compile(r"https?://\S+|\bgh\s+(?:issue|pr)\s+view\b")

# Reviewing commits that already exist needs no task description, but both agents
# still have to be told what to measure the code against.
NO_TASK = (
    "No task was given. These commits already existed when the loop started. Judge them "
    "on their own merits: what the commit messages and the diff set out to do, and whether "
    "the code does it correctly."
)

# Refs a branch may have forked from. All of them are tried, and disagreement
# between them is reported rather than resolved by order: a repository holding
# both 'main' and 'master' has been seen to make first-match pick the wrong one.
MAIN_BRANCH_CANDIDATES = ("origin/HEAD", "origin/main", "origin/master", "main", "master")

# git clone --local hardlinks the object store, and the only failure that a
# second attempt without hardlinks can fix is the link itself failing. Anything
# else -- a destination git refused, a repository it could not read -- must be
# reported rather than retried, because the retry deletes what it is about to
# clone into.
HARDLINK_REFUSED = re.compile(r"cross-device|failed to create link", re.IGNORECASE)


@dataclass
class Verdict:
    status: str
    findings: int
    review_file: str
    summary: str

    @property
    def is_clean(self) -> bool:
        return self.status == CLEAN


@dataclass
class RoundRecord:
    number: int
    commits: list[str] = field(default_factory=list)
    review_file: str | None = None
    status: str | None = None
    findings: int | None = None
    summary: str | None = None
    # Set when the round ended in a commit the loop made itself because the
    # implementer never got that far. A reader of run.json has to be able to tell
    # that commit from one an agent stood behind.
    salvaged: str | None = None
    # Set when the round produced no commit and left work in the tree: an
    # implementer that stopped between its last edit and its `git commit`, which
    # is a different round from one that looked and declined, and has to be
    # readable as such afterwards.
    stalled: bool = False
    # What it left there. Named in the run summary as well as on the console,
    # because the reader deciding whether the recovery caught the whole round is
    # usually reading run.json days later.
    uncommitted_paths: list[str] = field(default_factory=list)
    # Set when that work was handed back to the implementer to finish.
    retried: bool = False


class AgentError(RuntimeError):
    """An agent invocation failed, timed out, or produced no usable answer."""


class AgentTimeout(AgentError):
    """The agent was still working when --timeout expired and was killed.

    Worth its own type: an agent killed mid-edit has a working tree full of
    unfinished work, which is a different situation from one that exited on its
    own and a different sentence in the commit that salvages it.
    """


class CleanupUnconfirmed(AgentError):
    """The killed agent's process tree could not be confirmed gone.

    Distinct from AgentTimeout on purpose, and more serious. A timeout leaves an
    unfinished but *quiescent* tree, which `salvage_commit` can commit. This says
    the shells and test processes the agent spawned may still be running and
    writing to the working tree -- so nothing may `git add` it, and the run has to
    stop with the tree untouched rather than capture a half-written one as a
    round's work. It ends the loop where salvage would have raced the survivors.
    """


class _Done(Exception):
    """Internal signal: the run finished before reaching the implement loop."""


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        raise AgentError(f"git {' '.join(args)} failed: {result.stderr.strip() or result.stdout.strip()}")
    return result.stdout.strip()


@dataclass
class Progress:
    """What the draining thread has seen, for the heartbeat to report."""

    lines: int = 0
    last: float | None = None  # time.monotonic() of the most recent line

    def describe(self) -> str:
        if self.last is None:
            return "no output yet"
        return f"{self.lines} lines, last {time.monotonic() - self.last:.0f}s ago"


def _pump(stream: IO[str], prefix: str, log_handle: IO[str], progress: Progress) -> None:
    """Drain the agent's output. Never stop draining, whatever a line contains.

    This loop is the only thing emptying the child's stdout pipe. An exception
    here does not merely lose a line: the pipe fills, the agent blocks on its
    next write, and the run dies at the timeout with nothing to show for it.
    That is not hypothetical -- one smart quote in a Codex message raised
    UnicodeEncodeError on a cp1252 console and deadlocked a whole round.
    """
    for line in stream:
        progress.lines += 1
        progress.last = time.monotonic()
        with contextlib.suppress(OSError, UnicodeError):
            log_handle.write(line)
            log_handle.flush()
        try:
            print(f"{prefix} {line.rstrip()}", flush=True)
        except (OSError, UnicodeError):
            safe = line.rstrip().encode("ascii", "replace").decode("ascii")
            with contextlib.suppress(OSError):
                print(f"{prefix} {safe}", flush=True)


# How often the Windows Job Object tracking an agent is asked whether it has
# emptied out after TerminateJobObject. See _terminate_tree.
JOB_CONFIRM_INTERVAL_S = 0.05

# Win32 API constants for the Job Object tracking path below. Not sourced from
# `subprocess` (it only defines the Windows creation-flag constants when built
# on Windows itself, so on any other host the names simply do not exist) or
# from `ctypes.wintypes` (it has no JOBOBJECTINFOCLASS or job-limit values --
# those are plain #define'd integers in the Windows SDK headers, not types).
_CREATE_SUSPENDED = 0x00000004
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9
_JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION_CLASS = 1
_TH32CS_SNAPTHREAD = 0x00000004
_THREAD_SUSPEND_RESUME = 0x0002
_THREAD32_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


class _IoCounters(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _JobObjectBasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_void_p),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _JobObjectExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JobObjectBasicLimitInformation),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class _JobObjectBasicAccountingInformation(ctypes.Structure):
    _fields_ = [
        ("TotalUserTime", ctypes.c_longlong),
        ("TotalKernelTime", ctypes.c_longlong),
        ("ThisPeriodTotalUserTime", ctypes.c_longlong),
        ("ThisPeriodTotalKernelTime", ctypes.c_longlong),
        ("TotalPageFaultCount", wintypes.DWORD),
        ("TotalProcesses", wintypes.DWORD),
        ("ActiveProcesses", wintypes.DWORD),
        ("TotalTerminatedProcesses", wintypes.DWORD),
    ]


class _ThreadEntry32(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ThreadID", wintypes.DWORD),
        ("th32OwnerProcessID", wintypes.DWORD),
        ("tpBasePri", ctypes.c_long),
        ("tpDeltaPri", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
    ]


def _kernel32() -> ctypes.WinDLL:
    """kernel32, loaded fresh rather than cached at import time so importing this
    module never touches `ctypes.windll` -- an attribute that plain `ctypes`
    does not even define outside Windows -- and the whole job-tracking path
    below stays a set of ordinary functions the test suite can monkeypatch
    from a forced `sys.platform == "win32"`, the same way `_taskkill_tree`
    already is for the taskkill walk.
    """
    return ctypes.WinDLL("kernel32", use_last_error=True)


def _create_job_object() -> int | None:
    """A fresh, unnamed Job Object with KILL_ON_JOB_CLOSE set, or None if it
    could not be created or configured. KILL_ON_JOB_CLOSE means a handle leak
    on this module's part still cannot strand a member running: closing the
    last handle to the job kills whatever is still in it.
    """
    kernel32 = _kernel32()
    kernel32.CreateJobObjectW.restype = ctypes.c_void_p
    kernel32.CreateJobObjectW.argtypes = (ctypes.c_void_p, ctypes.c_wchar_p)
    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        return None
    info = _JobObjectExtendedLimitInformation()
    info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.SetInformationJobObject.argtypes = (ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
    if not kernel32.SetInformationJobObject(
        job, _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS, ctypes.byref(info), ctypes.sizeof(info)
    ):
        _close_handle(job)
        return None
    return job


def _assign_process_to_job(job: int, process_handle: int) -> bool:
    kernel32 = _kernel32()
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.AssignProcessToJobObject.argtypes = (ctypes.c_void_p, ctypes.c_void_p)
    return bool(kernel32.AssignProcessToJobObject(job, process_handle))


def _job_active_process_count(job: int) -> int | None:
    """How many processes the job currently has, or None if the job itself could
    not be queried. The kernel keeps this count itself as members are created
    and exit, so -- unlike a `tasklist` snapshot -- there is no gap between
    counting and answering for a member born in between to hide in.
    """
    kernel32 = _kernel32()
    info = _JobObjectBasicAccountingInformation()
    returned = wintypes.DWORD(0)
    kernel32.QueryInformationJobObject.restype = wintypes.BOOL
    kernel32.QueryInformationJobObject.argtypes = (
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    )
    ok = kernel32.QueryInformationJobObject(
        job,
        _JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION_CLASS,
        ctypes.byref(info),
        ctypes.sizeof(info),
        ctypes.byref(returned),
    )
    if not ok:
        return None
    return int(info.ActiveProcesses)


def _terminate_job(job: int) -> bool:
    kernel32 = _kernel32()
    kernel32.TerminateJobObject.restype = wintypes.BOOL
    kernel32.TerminateJobObject.argtypes = (ctypes.c_void_p, wintypes.UINT)
    return bool(kernel32.TerminateJobObject(job, 1))


def _close_handle(handle: int) -> None:
    kernel32 = _kernel32()
    kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
    kernel32.CloseHandle(handle)


def _resume_process_threads(pid: int) -> bool:
    """Resume every thread belonging to `pid`, after it was launched suspended so
    it could be assigned to a Job Object before it could execute a single
    instruction. A freshly created suspended process has exactly one thread --
    it has not run yet, so it cannot have started another -- but every thread
    the snapshot finds for the pid is resumed rather than assuming that,
    against a future Windows that ever starts one differently.

    `subprocess.Popen` closes the thread handle `CreateProcess` itself returns
    before this module ever sees it (`_execute_child` always does, whether or
    not `CREATE_SUSPENDED` was requested), so there is no handle left to resume
    directly; the thread is instead found again by walking a toolhelp32
    snapshot for `pid`.
    """
    kernel32 = _kernel32()
    kernel32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    kernel32.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
    snapshot = kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPTHREAD, 0)
    if not snapshot or snapshot == _THREAD32_INVALID_HANDLE_VALUE:
        return False
    try:
        kernel32.Thread32First.restype = wintypes.BOOL
        kernel32.Thread32First.argtypes = (ctypes.c_void_p, ctypes.POINTER(_ThreadEntry32))
        kernel32.Thread32Next.restype = wintypes.BOOL
        kernel32.Thread32Next.argtypes = (ctypes.c_void_p, ctypes.POINTER(_ThreadEntry32))
        kernel32.OpenThread.restype = ctypes.c_void_p
        kernel32.OpenThread.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.ResumeThread.restype = wintypes.DWORD
        kernel32.ResumeThread.argtypes = (ctypes.c_void_p,)

        resumed_any = False
        entry = _ThreadEntry32()
        entry.dwSize = ctypes.sizeof(_ThreadEntry32)
        found = bool(kernel32.Thread32First(snapshot, ctypes.byref(entry)))
        while found:
            if entry.th32OwnerProcessID == pid:
                thread_handle = kernel32.OpenThread(_THREAD_SUSPEND_RESUME, False, entry.th32ThreadID)
                if thread_handle:
                    if kernel32.ResumeThread(thread_handle) != 0xFFFFFFFF:
                        resumed_any = True
                    _close_handle(thread_handle)
            entry = _ThreadEntry32()
            entry.dwSize = ctypes.sizeof(_ThreadEntry32)
            found = bool(kernel32.Thread32Next(snapshot, ctypes.byref(entry)))
        return resumed_any
    finally:
        _close_handle(snapshot)


@dataclass
class _AgentJob:
    """One agent launch and everything acquired for it that something has to
    answer for: the process itself, the Windows Job Object tracking it, whether
    it has ever been allowed to run, and on POSIX the process group it leads.

    Review round 6 finding 1: ownership of the job used to move on
    `_track_in_job_object`'s return. That call released everything it had
    acquired on every path except the return, and `run_agent`'s `finally` owned
    it from the return onwards -- but a return is two frames with a value in
    flight between them, and an exception raised there belongs to neither owner.
    The helper's `finally` reads the flag saying it handed the job over, while
    the caller never received a handle and so has nothing to close. The exception
    an unattended loop actually meets there is the KeyboardInterrupt of an
    operator stopping it, and the agent may already be resumed by then, so what
    is left unowned is a job holding a running process. Nothing collects a raw
    Win32 handle held as a Python int when the local goes out of scope, and
    `main` catches KeyboardInterrupt and carries on through checkout cleanup,
    scratch removal and summary writing with that agent still writing the tree it
    is tidying.

    So nothing is handed over any more. `run_agent` allocates this record and
    enters its `try` before the first call that can acquire anything, and
    `_track_in_job_object` writes each state into it as the kernel grants that
    state, so all of them are visible to an owner that was already active when
    they came into being:

    * `process` -- bound before the local name `run_agent` goes on to use, so
      even an exception between those two names leaves something holding the
      suspended process that has to be killed.
    * `handle` -- the Job Object, from the instant `_create_job_object` returns
      one.
    * `member` -- `process` is assigned to `handle`, so the job answers for its
      whole tree and releasing it means confirming that job empty.
    * `suspended` -- `process` has never been resumed, so it has executed no
      instruction, is the entirety of its own tree, and releasing it means
      killing it directly. False on POSIX, where nothing is launched suspended.
    * `group_leader` -- `process` leads a process group of its own
      (`start_new_session` on the `Popen`), so every shell and test runner it
      starts shares its pgid, and releasing it means signalling that group. True
      on POSIX and False on Windows, where a job rather than a group is what a
      tree is answered for through.

    `member` and `suspended` never both act. A member is answered for through its
    job whether or not the resume that follows assignment ever ran, and a Windows
    non-member has provably never been resumed. `group_leader` acts alongside
    neither: both of those are set only on the Windows tracking path, and no job
    is ever created on POSIX.
    """

    process: subprocess.Popen[str] | None = None
    handle: int | None = None
    member: bool = False
    suspended: bool = False
    group_leader: bool = False


def _track_in_job_object(agent_job: _AgentJob) -> None:
    """Assign the agent -- launched suspended with `_CREATE_SUSPENDED` -- to a
    fresh Job Object and resume it, so every descendant it will ever create is
    a job member from the instant it exists, not from whenever this module's
    next `tasklist` snapshot happens to run.

    This is what closes review round 2 finding 1: a snapshot-based retry
    narrows the gap a fresh arrival can hide in down to the time between the
    last snapshot and taskkill's own walk, but cannot close it, because both
    are polling a snapshot rather than asking something that was tracking
    membership the whole time. A Job Object is that something -- the kernel
    updates its member count as processes are created and exit, and a process
    cannot be created and exit invisibly to a job it was already in before it
    ran its first instruction.

    An agent that could not be placed under a job is never resumed: the failure
    is raised, and `run_agent`'s release kills the still-suspended process.
    Round 2 let such a launch run untracked on the argument that a narrower
    fallback beat losing agent execution entirely; round 3 finding 1 then showed
    that fallback can never confirm a tree empty, and round 4 finding 1 showed
    the ordinary exits -- success and a plain nonzero code -- never even reach
    cleanup, so an untracked agent's detached descendant outlives the round and
    races the very salvage commit this work exists to protect. Nothing can be
    added to the no-job path to fix that: once the root has exited, a PID-rooted
    taskkill cannot discover descendants that are already orphaned. So the launch
    is failed at the one moment the answer is still knowable, while the process
    is suspended and has provably spawned nothing, and the run ends with
    AgentError (or CleanupUnconfirmed if even that kill cannot be confirmed)
    rather than with a round whose result could never be trusted.

    The resume is required for the same reason it has been since review round 3
    finding 2: a resume that silently failed left the process suspended
    forever, which `run_agent` then waited out for the whole round timeout
    because nothing told it the agent had never started. A resume that returns
    False or raises is not swallowed -- the process is still suspended, so it
    can have no descendants of its own yet, and is killed through the job it
    was just assigned to before this raises.

    This acquires nothing it owns. Every handle and every state it reaches is
    written into `agent_job` as the kernel grants it, and the `finally` in
    `run_agent` -- already active before this is called -- releases all of it
    through `_release_job`. Review round 5 finding 1 made the release here run
    for a KeyboardInterrupt as well as an `Exception`; round 6 finding 1 then
    showed that a release living here cannot cover the handover itself, because
    an interrupt between this frame's `return` and the caller's assignment is
    seen by neither frame's cleanup. There is no handover left to miss: failure
    is reported by raising, and nothing is returned that an exception could
    strand.

    What no arrangement removes is the instant between a kernel call succeeding
    and Python recording that it did -- the interpreter can raise a pending
    interrupt at any bytecode boundary. Only one of those instants matters here,
    and it is bounded rather than closed: between `_create_job_object` returning
    a handle and `agent_job.handle` being bound to it, an interrupt leaks that
    handle. What it cannot leak is a running process, because the job is empty
    for the whole of that instant -- the agent is not assigned to it until the
    next statement and is not resumed until after that -- and the still-suspended
    agent is killed by the release regardless, because `agent_job` has held it
    since before this was called. The two later boundaries are covered rather
    than bounded: an interrupt between `AssignProcessToJobObject` returning and
    `member` being set leaves the release closing a handle whose KILL_ON_JOB_CLOSE
    reaches the same process it then kills and confirms directly, and one between
    a successful resume and `suspended` being cleared is already inside the
    member branch, which answers for a resumed agent and a suspended one alike.
    """
    process = agent_job.process
    assert process is not None  # `run_agent` records the launch before it tracks it
    setup_error: Exception | None = None
    try:
        created = _create_job_object()
        if created is not None:
            agent_job.handle = created
            agent_job.member = _assign_process_to_job(created, process._handle)  # noqa: SLF001 -- see _resume_process_threads
    except Exception as error:
        setup_error = error

    if not agent_job.member:
        # Creation failed, assignment failed, or a Win32 call raised: there is no
        # kernel-tracked membership for this tree and there never will be one,
        # because a process can only be assigned to a job it is not yet running
        # ahead of. So the launch is failed rather than resumed, and the
        # still-suspended root is killed by `_release_job` -- while it has
        # executed no instruction and its death is therefore still confirmable,
        # which is exactly what nothing later in the round would be able to say
        # about it. That release is also what turns a kill it cannot confirm into
        # the CleanupUnconfirmed such a kill deserves, replacing this error.
        raise AgentError(
            f"pid {process.pid} could not be placed under a Windows Job Object, so nothing could ever confirm "
            "its process tree gone; it was killed while still suspended rather than run untracked. Run the "
            "loop through tools/loop_in_container.py, or from a host where this process is not already "
            "confined to a job object that forbids nesting."
        ) from setup_error

    try:
        resumed = _resume_process_threads(process.pid)
        resume_error: Exception | None = None
    except Exception as error:
        resumed = False
        resume_error = error
    if not resumed:
        raise AgentError(f"pid {process.pid} could not be resumed to start running") from resume_error
    agent_job.suspended = False


def _release_job(agent_job: _AgentJob) -> None:
    """Give up everything `agent_job` records, exactly once, and confirm the
    agent's tree gone with it.

    The one release for one owner: `run_agent` calls this from the `finally` it
    entered before the agent existed, so it runs for a successful round, an
    ordinary nonzero exit, a timeout, a failure inside the tracking call, and an
    operator's Ctrl-C landing anywhere in between. Five states reach it, and each
    has its own authoritative answer:

    * No process. `Popen` itself raised, so nothing was launched and nothing is
      owed.
    * The process leads its own group and has not been reaped. POSIX, where no
      job can be had at all: signal the group, reap the leader and wait the group
      out through `_terminate_tree`, so a failure that got past the stdin and
      wait handling does not walk out leaving the agent running.
    * The process is a member of `handle`. Terminate the job, wait for the
      kernel's own member count to read zero, collect the root, and close the
      handle exactly once. This is the only Windows state in which the agent can
      have run at all, so it is the only one where descendants are possible, and
      the job is what makes their absence knowable.
    * A job was created but the process was never assigned to it. Nothing is a
      member, so there is nothing to confirm; close the orphaned handle rather
      than leak it, and answer for the root below.
    * No job, or none the process ever joined. Kill the still-suspended root
      directly: it has executed no instruction, so it is the whole tree, and
      `poll()` is the confirmation. A kill that errors is not raised over that
      answer -- it is that answer, and `poll()` reports it as a
      CleanupUnconfirmed the loop is looking for rather than as an OSError
      nothing in it is.

    On POSIX this was a no-op until now, so review round 4 finding 2 stayed live
    there for the whole time it was closed on Windows: an exception between the
    launch and the stdin and wait handling -- `reader.start()` raising, an
    unexpected `process.wait()` failure, a heartbeat `print` onto a broken
    stdout, a `reader.join()` that will not -- walked out of `run_agent` past a
    `finally` that had nothing to do, and `main` then went on through checkout
    cleanup, scratch removal and summary writing with the agent still editing the
    tree it was tidying. The group is signalled for exactly that, and only while
    `process.poll()` is None, because that is the whole of the case: an agent
    still there to answer for.

    That guard is also what keeps the signal from straying. A process group's id
    is its leader's pid, and the kernel reserves it only while the group still
    has a member; once the leader has both exited and been reaped, the id is free
    for somebody else's group and `killpg` on it would signal strangers.
    `poll()` returning None says this process has not reaped the leader, and
    nothing else can, so the reservation holds. A leader that exits in the
    instant between that answer and the signal becomes a zombie, which is still a
    member, so it holds the reservation too.

    What that leaves open is the POSIX half of review round 4 finding 1, and it
    is left open rather than half-answered: an agent that exited on its own may
    have detached a descendant, and by the time anything here can ask, the leader
    is reaped and the group has no reuse-proof name left to signal. Windows
    answers that with a job handle, which is an identity rather than a number.
    POSIX needs its own equivalent -- a cgroup, or a pidfd taken before the wait
    -- rather than a harder look at a pgid, and that is a design rather than a
    fix. The two paths that call `_terminate_tree` themselves reach this with the
    leader already reaped, so it adds nothing there; a `_terminate_tree` that
    could not even deliver a signal leaves the leader alive and gets the same
    refusal a second time, which is the same answer twice rather than a new one.

    Clearing the record first makes this idempotent: a second call has nothing
    left to close, nothing left to kill and no group left to signal.

    Raises CleanupUnconfirmed when the root outlives its own kill or a POSIX
    group will not clear, and lets `_confirm_job_empty`'s through. Called from a
    `finally`, that replaces whatever was already propagating, which is the
    intended precedence: only that type stops the caller from committing the tree
    underneath a survivor.
    """
    process, handle = agent_job.process, agent_job.handle
    member, suspended = agent_job.member, agent_job.suspended
    leader = agent_job.group_leader
    agent_job.handle, agent_job.member, agent_job.suspended, agent_job.group_leader = None, False, False, False
    if process is None:
        return

    if leader:
        if process.poll() is None:
            _terminate_tree(process, grace_s=5.0)
        return

    if handle is not None:
        try:
            if member:
                _confirm_job_empty(handle, process, grace_s=5.0)
                # The job says nothing is running; collect the agent's own exit
                # so this does not walk away from an unreaped child.
                _reap_leader(process, grace_s=5.0)
        finally:
            _close_handle(handle)

    if member or not suspended:
        return
    with contextlib.suppress(OSError, ValueError):
        process.kill()
    _reap_leader(process, grace_s=5.0)
    if process.poll() is None:
        raise CleanupUnconfirmed(
            f"pid {process.pid} was never resumed under a Windows Job Object and then could not be "
            "confirmed killed while it was still suspended"
        )


def _taskkill_tree(pid: int) -> subprocess.CompletedProcess[str]:
    # encoding/errors as in git() and run_agent()'s own Popen above: taskkill is a
    # console app and prints in the OEM codepage, which on a non-English Windows
    # is not the ANSI codepage `text=True` would otherwise decode with -- the same
    # mismatch that made a smart quote in a Codex message raise UnicodeEncodeError
    # and deadlock a round. Asked far more often now that a failure is retried
    # within the grace period, so a decode this narrow was going to be met.
    return subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True, text=True, encoding="utf-8", errors="replace")


def _confirm_job_empty(job: int, process: subprocess.Popen[str], grace_s: float) -> None:
    """Terminate every member of `job` and wait until the kernel's own active-
    process count confirms none remain, or raise CleanupUnconfirmed.

    Does not close `job` -- `_release_job` owns that handle and closes it exactly
    once, whether or not this raises (`_terminate_tree`'s job branch still wants
    to check the agent's own process handle after this returns, and the release
    itself has a close of its own to make).
    """
    terminated = _terminate_job(job)
    deadline = time.monotonic() + grace_s
    remaining = _job_active_process_count(job)
    while remaining is not None and remaining > 0 and time.monotonic() < deadline:
        time.sleep(JOB_CONFIRM_INTERVAL_S)
        remaining = _job_active_process_count(job)
    if remaining != 0:
        detail = "" if terminated else "; TerminateJobObject itself reported failure"
        raise CleanupUnconfirmed(
            f"the Windows Job Object tracking pid {process.pid} could not be confirmed empty after "
            f"TerminateJobObject and a {grace_s:.0f}s wait (active process count: {remaining}){detail}"
        )


def _terminate_tree(process: subprocess.Popen[str], *, grace_s: float = 5.0, job: int | None = None) -> None:
    """Kill the agent and everything it spawned, or raise CleanupUnconfirmed.

    An agent that hit the timeout is usually blocked inside a child shell it
    started, and that shell has its own children -- a test runner, a compiler --
    still writing to the working tree. Killing only the agent leaves them running,
    and `salvage_commit` runs the moment this returns: a descendant that outlives
    this call races that commit, so it has to be gone, not merely signalled,
    before this returns. The timeout is where this is reached from most often but
    not the only place: `_release_job` calls it for a POSIX agent an exception
    left running, which is the same tree in the same danger, reached through a
    failure rather than through a deadline.

    On Windows, `job` -- the Job Object `run_agent` assigned this process to
    while it was still suspended, via `_track_in_job_object` -- is terminated as
    one kernel call and its member count polled until it reads zero: the job has
    tracked every process born under it since before the first one ever ran, so
    its own count is authoritative the instant it is asked, with no snapshot to
    go stale between being taken and being trusted. On POSIX the agent leads its
    own process group (``start_new_session`` on the `Popen`), so its shells and
    their children share its pgid; the whole group is signalled, the leader
    reaped so the group can empty, and the group waited out. A plain `kill(pid)`
    reached only the leader, which is the bug this fixes -- the grandchildren
    editing the tree never saw a signal at all.

    Returns only once the tree is *positively confirmed* gone. If the group still
    has a member once a signal and its deadline have passed -- whether the signal
    itself was delivered or could not be, which only decides which report is
    raised -- this raises CleanupUnconfirmed rather than returning: returning would
    tell the caller the tree is safe to commit when a process may still be editing
    it, which is the race this exists to prevent. On Windows without a job there
    is no kernel-tracked membership to ask, so this never returns cleanly at
    all; see the `job is None` branch below.

    Borrows `job` and never closes it, exactly as `_confirm_job_empty` does.
    Review round 6 finding 1: this used to consume the handle, so `run_agent` had
    to clear its own record of it before calling -- and an interrupt landing
    between that clear and this frame's `try` left the handle in a temporary
    belonging to nobody, with the agent still running inside the job it named.
    Borrowing deletes that window rather than narrowing it: `_release_job` holds
    the handle across this call and closes it once afterwards, whatever this does
    with the tree. The cost is that the release confirms an already-terminated
    job a second time, which is two kernel calls on a job that reads empty
    immediately.
    """
    if sys.platform == "win32" and job is not None:
        _confirm_job_empty(job, process, grace_s)
        # No exit code, not even a job's own member count, speaks for the
        # agent handle `run_agent` is actually holding a pipe open against.
        process.kill()
        _reap_leader(process, grace_s)
        if process.poll() is None:
            raise CleanupUnconfirmed(f"the agent process {process.pid} was still running after its job was terminated")
        return

    if sys.platform == "win32":
        # A Windows agent `run_agent` started never reaches this: since review
        # round 4 finding 1, `_track_in_job_object` fails the launch instead of
        # resuming a process it could not place under a job, so a running agent
        # always has one. This stays as the backstop for every other way a
        # `job=None` process could arrive here, and it is deliberately the
        # unhappy answer. Nothing has been tracking such a tree's membership
        # since before it ran its first instruction, so a `taskkill /T` walk is
        # racing a live tree the same way repeated `tasklist` snapshots were
        # shown to in review round 3 finding 1: a descendant born after the
        # walk, or even after the kill, is invisible to it, and re-enumerating
        # harder cannot rule that out -- only something that was tracking
        # membership the whole time (a Job Object) can. taskkill is still run
        # here, once, as a courtesy: a clean host is the common case and there
        # is no reason not to try. But the result is never trusted as proof of
        # anything, so this always raises CleanupUnconfirmed -- salvage_commit
        # must never race a descendant this call had no way to rule out.
        _taskkill_tree(process.pid)
        process.kill()
        _reap_leader(process, grace_s)
        raise CleanupUnconfirmed(
            f"pid {process.pid} has no Windows Job Object to authoritatively confirm its process tree is "
            "empty; taskkill was run best-effort but its result cannot prove no descendant survived"
        )

    # The agent leads its own process group: `start_new_session` ran setsid() in
    # the child, so its pgid equals its pid at launch. Target that pid-as-pgid
    # directly instead of reading it back with os.getpgid(pid). A POSIX process
    # group outlives its leader -- the descendants that inherited it stay in it
    # after the leader exits -- and once the leader has been reaped
    # os.getpgid(pid) raises ProcessLookupError even while those descendants are
    # still alive and writing the tree. The old code read the group that way and
    # returned on that error as if the group were empty, so the very members this
    # exists to kill were left running for `salvage_commit` to race. The kernel
    # keeps the pid-as-pgid reserved while the group has any member, so signalling
    # it here cannot stray onto an unrelated group.
    pgid = process.pid

    # SIGTERM first for a clean stop, then SIGKILL for whatever ignored it. After
    # each, the leader is reaped -- a group whose only remaining member is an
    # unreaped zombie leader never looks empty -- and the group waited out.
    #
    # A signal that could not be delivered is not by itself the failure, and
    # raising over it before asking anything else was issue #320. Darwin answers
    # EPERM, not ESRCH, for a group the kernel still knows but whose every
    # remaining member is a zombie: its group iterator skips zombies, and a pass
    # that signalled nobody is reported as a permission failure under POSIX
    # conformance rather than as an empty group. This path walks through exactly
    # that state by design -- the leader is a zombie from the moment it dies until
    # `_reap_leader` collects it, and a descendant the SIGTERM above killed is one
    # until init collects it -- so a delivery error here routinely describes a tree
    # that is already gone, and "could not signal" is then a survivor an operator
    # goes looking for and cannot find.
    #
    # The question that decides the answer is the membership one, so it is asked
    # first either way and the delivery error is reported only when the group also
    # will not read empty. That swallows nothing: a group with a live member that
    # is not ours to signal answers the `_group_gone` probe with the same refusal
    # for the whole wait, and still raises below.
    for sig in (signal.SIGTERM, signal.SIGKILL):
        delivered = _signal_group(pgid, sig)
        _reap_leader(process, grace_s)
        if _group_gone(pgid, deadline=time.monotonic() + grace_s):
            return
        if not delivered:
            raise CleanupUnconfirmed(
                f"could not signal process group {pgid} with {_signal_name(sig)} while killing pid {process.pid}, "
                f"and the group still had a member after a {grace_s:.0f}s wait"
            )
    raise CleanupUnconfirmed(
        f"process group {pgid} (agent pid {process.pid}) still had a member after SIGKILL and a {grace_s:.0f}s wait"
    )


def _signal_name(sig: int) -> str:
    try:
        return signal.Signals(sig).name
    except ValueError:  # pragma: no cover - only the two constants above are passed
        return str(sig)


def _signal_group(pgid: int, sig: int) -> bool:
    """Signal every process in the group.

    True when the signal was delivered or the group was already gone -- both mean
    there is nothing left this call failed to reach. False when it could not be
    delivered (``PermissionError`` or another ``OSError``): nothing in the group
    was reached, which is not the same as something being left in it. On Darwin it
    is also the answer for a group whose remaining members are all zombies, so the
    caller settles it by asking whether the group still has a member rather than
    by assuming one is there -- see `_terminate_tree`.
    """
    try:
        os.killpg(pgid, sig)
        return True
    except ProcessLookupError:
        return True
    except OSError:
        return False


def _reap_leader(process: subprocess.Popen[str], grace_s: float) -> None:
    """Wait out the agent process so it is neither left running nor left a zombie."""
    with contextlib.suppress(subprocess.TimeoutExpired, OSError, ValueError):
        process.wait(timeout=grace_s)


def _group_gone(pgid: int, *, deadline: float) -> bool:
    """Whether the process group has no members left, waited for until `deadline`.

    `killpg(pgid, 0)` sends no signal; it asks whether the group still has a
    member. ``ProcessLookupError`` -- the group is empty -- is the *only* positive
    answer, and the only one that returns True. A ``PermissionError`` means a
    member is still there but not ours to signal, which is the opposite of gone;
    any other ``OSError`` leaves the answer unknown. Both keep waiting and then
    report not-gone, so `_terminate_tree` treats an unclearable group as cleanup
    unconfirmed rather than as the absence salvage may build on -- the "not gone
    yet" that salvage must never race.
    """
    while True:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return True
        except OSError:
            pass  # still present (EPERM) or unknowable: not the absence this needs
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)


def run_agent(
    command: list[str],
    prompt: str,
    cwd: Path,
    prefix: str,
    log_path: Path,
    timeout: float,
    dry_run: bool,
    env: dict[str, str] | None = None,
    heartbeat: float = 0.0,
) -> None:
    """Run an agent CLI with the prompt on stdin, mirroring output to console and log.

    The prompt goes over stdin rather than argv because a review document pasted
    into a prompt outgrows the Windows command-line limit long before it outgrows
    anything else.

    `claude -p` prints nothing at all until it has finished, so an implement round
    is silent for the thirty-five to fifty minutes it takes. Silence that long is
    indistinguishable from a hang -- it was read as one twice in a single session
    -- so `heartbeat` seconds of it produces a line saying how long the agent has
    been running and whether it has said anything yet. It is deliberately the
    loop's own line rather than a different `--output-format`: nothing here parses
    what an agent prints, and a machine-readable stream would change what a
    transcript is for the sake of a progress indicator.
    """
    printable = " ".join(command)
    print(f"\n{prefix} $ {printable}", flush=True)
    if dry_run:
        print(f"{prefix} [dry-run] prompt ({len(prompt)} chars) not sent", flush=True)
        return

    log_path.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    print(f"{prefix} started; limit {timeout:.0f}s (log: {log_path})", flush=True)
    progress = Progress()
    with log_path.open("w", encoding="utf-8") as log_handle:
        log_handle.write(f"$ {printable}\n\n--- prompt ---\n{prompt}\n--- output ---\n")
        # The owner of this launch, allocated and entered before there is a
        # launch to own. Review round 3 finding 3 was a handle closed on only
        # some paths; review round 4 finding 2 was the rest of them -- a
        # `reader.start()` that raises RuntimeError, an unexpected
        # `process.wait()` error, a heartbeat `print` that fails -- walking out
        # past every close there was; review round 6 finding 1 was the handover
        # between `_track_in_job_object` and this frame, which no `finally` on
        # either side of it covered. So ownership no longer moves at all: this
        # record holds the process and the job from the moment each exists, the
        # helpers below write into it, and `_release_job` in the `finally` is the
        # only thing that terminates, confirms, closes or kills.
        #
        # Which of the two lifetime states the launch below will be in is decided
        # by the platform rather than by anything that can fail, so both are set
        # here: on Windows the process is launched suspended and answered for
        # through a job, on POSIX it leads a process group and is answered for
        # through that. Neither says a launch happened -- `_release_job` reads
        # `process` for that -- only which question to ask if one did.
        agent_job = _AgentJob(suspended=sys.platform == "win32", group_leader=sys.platform != "win32")
        try:
            # The record's attribute is bound before the local -- Python assigns
            # chained targets left to right -- so an exception raised between the
            # two names still leaves the `finally` holding the suspended process
            # it has to kill.
            agent_job.process = process = subprocess.Popen(
                command,
                cwd=str(cwd),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                env=env,
                # POSIX: a new session/group, so `_terminate_tree` can signal the
                # shells and test processes the agent spawns as one group rather than
                # reaching only the agent itself; a no-op on Windows. Windows:
                # launched suspended (`_CREATE_SUSPENDED`; a no-op on POSIX, where
                # `creationflags` must stay 0 or Popen itself rejects it) so it
                # cannot spawn anything before `_track_in_job_object` below
                # assigns it to a Job Object and resumes it -- see that call and
                # `_terminate_tree`.
                start_new_session=True,
                creationflags=_CREATE_SUSPENDED if sys.platform == "win32" else 0,
            )
            if sys.platform == "win32":
                _track_in_job_object(agent_job)
            assert process.stdin is not None and process.stdout is not None
            reader = threading.Thread(target=_pump, args=(process.stdout, prefix, log_handle, progress), daemon=True)
            reader.start()
            try:
                process.stdin.write(prompt)
                process.stdin.close()
            except OSError as error:  # the agent exited before reading the prompt
                # It may still be alive and holding the working tree; do not leave it
                # running just because it stopped listening. The handle is lent to
                # the call, not given: `_release_job` still closes it below.
                _terminate_tree(process, job=agent_job.handle)
                reader.join(timeout=10)
                raise AgentError(f"{prefix} refused the prompt: {error}") from error

            deadline = started + timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    _terminate_tree(process, job=agent_job.handle)
                    reader.join(timeout=10)
                    raise AgentTimeout(f"{prefix} exceeded --timeout of {timeout:.0f}s")
                try:
                    returncode = process.wait(timeout=min(heartbeat, remaining) if heartbeat > 0 else remaining)
                    break
                except subprocess.TimeoutExpired:
                    if heartbeat > 0:
                        elapsed = time.monotonic() - started
                        print(f"{prefix} ... {elapsed:.0f}s of {timeout:.0f}s, {progress.describe()}", flush=True)
            reader.join(timeout=30)
        finally:
            # Reached on an agent that exited on its own -- success or an
            # ordinary nonzero code, where `_terminate_tree` is never called --
            # on a launch that could not be tracked or resumed, on the two paths
            # that did call `_terminate_tree` and lent it the handle, and on
            # every exception, interrupt included, that got past any of them.
            # They all need the same thing and all get it here: terminate the
            # job, wait for the kernel's own member count to read zero, and close
            # the handle exactly once. On POSIX, where there is no job to hold
            # any of that, the same call answers for the process group instead,
            # so an exception on its way past this frame no longer leaves the
            # agent running. Confirming rather than closing and
            # trusting KILL_ON_JOB_CLOSE is deliberate: that kill is
            # asynchronous, the same reason `_terminate_tree` does not close and
            # trust it either. A CleanupUnconfirmed raised here does replace an
            # exception on its way out, which is the right precedence -- a
            # descendant still writing to the tree outranks whatever failed above
            # it, and only that type stops the caller from salvaging the tree
            # underneath it.
            _release_job(agent_job)

    elapsed = time.monotonic() - started
    print(f"{prefix} exit {returncode} after {elapsed:.0f}s (log: {log_path})", flush=True)
    if returncode != 0:
        raise AgentError(f"{prefix} exited with {returncode}; see {log_path}")


def excluding(repo: Path, paths: tuple[Path, ...]) -> list[str]:
    """Pathspecs adding everything in `repo` except the loop's own directories.

    This repository gitignores `.agentic-loop/`, so `git add -A` leaves the run's
    review documents and transcripts alone here. Nothing guarantees that
    elsewhere, and a salvage commit that carries a review of itself is not the
    round's work. Directories outside the repository are dropped rather than
    named: git rejects a pathspec that leaves the work tree, and refusing the
    whole commit over one would defeat the point of making it.
    """
    pathspecs = ["--", "."]
    for path in paths:
        with contextlib.suppress(ValueError, OSError):
            relative = path.resolve().relative_to(repo.resolve())
            if relative != Path("."):
                pathspecs.append(f":!{relative.as_posix()}")
    return pathspecs


def uncommitted(repo: Path, paperwork: tuple[Path, ...] = ()) -> str:
    """What a round left in the working tree, the loop's own paperwork aside.

    `git status` takes the same pathspecs `salvage_commit` commits with, so a run
    whose review documents and transcripts sit in a repository that ignores
    nothing does not read its own paperwork as the implementer's work. Empty
    means the round left nothing behind.

    A git that will not answer is reported as empty, which is the reading that
    changes nothing: the round is then handled as the declined round it has
    always been handled as, and no tree is touched on the strength of a question
    that could not be asked.
    """
    with contextlib.suppress(AgentError):
        return git(repo, "status", "--porcelain", *excluding(repo, paperwork))
    return ""


def newly_uncommitted(inherited: str, present: str) -> str:
    """What the tree holds now that it did not hold when the round started.

    "The implementer produced no commit" is classified against the working tree,
    and until now the whole tree counted. Under `--review-checkout none` the
    reviewer works in the implementer's own tree, so anything the previous
    round's reviewer left there -- a file it wrote in the repository instead of
    in its scratch directory, a cache a test run built -- is still lying there
    when the next implementer looks at the findings and declines. That round then
    read as a stall: it cost a second implementer invocation, up to another whole
    `--timeout`, to be told there was nothing to finish, and `run.json` recorded
    `stalled` for a round that was declined, which is the wrong sentence for
    somebody reading it days later. The same leftovers made the other end of the
    recovery misfire too: a retry that committed everything it was handed still
    left them in the tree, so "it finished the job" read false and the loop made
    a work-in-progress commit over a round that was actually complete. Both stop
    once the question is what changed rather than what is there. `--allow-dirty`
    inherits the same benefit: the operator's own uncommitted changes are no
    longer read as this round's work either.

    Two identical porcelain lines are one state, so a path that was already dirty
    and that the implementer then edited further is not by itself news. That is
    the direction to be wrong in: such a round still reports that it produced no
    commit and still stops unless `--continue-on-no-commit` says otherwise, so
    nothing is lost that was not already lost before the recovery existed, and an
    implementer that really did a round's work has to have touched something the
    reviewer had not.

    Deciding is all this does. Once the loop does decide a round's work is in the
    tree, `salvage_commit` still commits the tree, leftovers included, exactly as
    an agent's own `git add -A` already would.
    """
    already = set(inherited.splitlines())
    return "\n".join(line for line in present.splitlines() if line not in already)


def _porcelain_z_records(raw: str) -> tuple[set[str], dict[str, str]]:
    """The pathnames in `git status --porcelain -z` output, and what a rename moved.

    Each record is `XY<space><path>` terminated by NUL; a rename or copy carries
    its source in a second NUL field. The destination is the name the tree now
    holds, so it is the path; the source is no longer in the tree, so it is not,
    but it is the name a previous round knew the same work by, and that is the
    second return value: source to destination.

    The two-character status is dropped on purpose: a path is the same
    outstanding work whether it is untracked, staged or modified, and
    identifying it by name is what keeps it from being lost the round its status
    changes. A rename changes the name itself, which is the one move that
    identity cannot survive on its own, so the caller is handed the way across.
    NUL delimiters keep a path with a space or a newline in it whole, which the
    newline-split `--porcelain` text cannot promise.
    """
    fields = raw.split("\0")
    paths: set[str] = set()
    moved: dict[str, str] = {}
    index = 0
    while index < len(fields):
        record = fields[index]
        index += 1
        if len(record) < 4:
            continue  # the empty field after the final NUL, or nothing parseable
        destination = record[3:]
        paths.add(destination)
        if "R" in record[:2] or "C" in record[:2]:
            if index < len(fields):
                # A copy leaves the source in the tree and a rename does not, but
                # either way this is the name the work was known by, and pointing
                # it at the destination costs nothing when the source is still
                # there under its own name as well.
                moved[fields[index]] = destination
            index += 1
    return paths, moved


def _porcelain_z_paths(raw: str) -> set[str]:
    """Just the pathnames, for a caller with no round to carry across."""
    return _porcelain_z_records(raw)[0]


def dirty_paths(repo: Path, paperwork: tuple[Path, ...] = ()) -> set[str]:
    """The set of work-tree paths git reports as dirty, the loop's paperwork aside."""
    return dirty_records(repo, paperwork)[0]


def dirty_records(repo: Path, paperwork: tuple[Path, ...] = ()) -> tuple[set[str], dict[str, str]]:
    """Those same paths, and where a rename in this status moved one of them.

    Outstanding stalled work is tracked by pathname rather than by the porcelain
    line that carries it: a file that was untracked (`?? fix.py`) one round and
    staged (`A  fix.py`) the next is the same unreviewed path, and forgetting it
    when its two-character status changed is how a clean verdict once exited over
    work no review had read. `--porcelain -z` is what makes the name usable as
    that identity -- read raw, not through `git`'s stripping, because the leading
    space of a ` M` status is part of the first record and `strip()` would eat it.
    Where a move lands the work on a name that was already dirty, the name is the
    thing that collides and `relocated_stall` reads the content instead.

    `--untracked-files=all` is what makes an untracked file its own record. The
    default mode reports a whole untracked directory as one `?? directory/` line and
    never descends, so a stall moved beneath a directory the run inherited untracked
    has no name of its own in either snapshot: the directory collides with itself,
    holds no blob or inode the stall would match, and the work sits under a name no
    review read while the loop calls the run clean. Listing every untracked file
    gives the moved path a name the reconciliation below can carry. Ignored files are
    still left out -- that would take `--ignored`, which this does not pass -- so the
    loop's own paperwork under a gitignored path stays invisible as before.

    Takes the same pathspecs as `uncommitted`, so the loop's own review documents
    and transcripts are not read as the implementer's work. A git that will not
    answer is an empty set, matching `uncommitted`: nothing is then carried, and
    no tree is touched on the strength of a question that could not be asked.
    """
    result = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain", "-z", "--untracked-files=all", *excluding(repo, paperwork)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        return set(), {}
    return _porcelain_z_records(result.stdout)


def blob_id(repo: Path, relative: str) -> str | None:
    """`relative`'s current content as git would name it, or None when it names nothing.

    Git's own object id for the file on disk, computed the way `git add` would: the
    repository's object format -- sha1, or the sha256 an `extensions.objectFormat`
    repository asks for -- over the content a clean filter would store, `core.autocrlf`
    and any `.gitattributes` conversion among them. Every comparison here is a
    work-tree file against another work-tree file -- the content the round inherited
    against the content it left -- so what matters is that both are named the same
    way, and naming them the way git itself would keeps that identity stable even
    where a filter rewrites content or the repository names objects in sha256, which a
    raw byte hash cannot promise across a re-read. See `blob_ids`, which hashes a whole
    set in one pass, and does the reading.

    A directory (an untracked one is reported as a path in its own right), a path
    that is gone, and one that will not open have no content to identify. Neither
    has an empty file: its content is identical to every other empty file's, so
    matching on it would say the stalled work had arrived at any path a round
    happened to truncate. All four are None rather than a shared placeholder.
    """
    return blob_ids(repo, {relative}).get(relative)


def has_content(path: Path) -> bool:
    """Whether `path` is a regular file with bytes in it, the only kind hashed by its own path.

    A directory, a path that is gone or will not `stat`, and an empty file are all
    excluded before git is asked: the first two hold no content to identify, and every
    empty file's content is every other's, so a blob id is asked only of what a match
    on it can tell apart. A symlink is excluded too, though it has a content identity of
    its own: `is_file` and `stat` follow it to its target, so `git hash-object` over the
    path would name the target's content rather than the mode `120000` blob git keeps for
    the link, and hashing its link text is `link_blob_id`'s job instead. See `blob_id`.
    """
    try:
        return not path.is_symlink() and path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def link_blob_id(repo: Path, path: Path) -> str | None:
    """The blob id git stores for the symlink `path` -- the hash of its link text, or None.

    Git keeps a symlink as a mode `120000` blob whose content is the target path the
    link holds, not the bytes of whatever it resolves to. `git hash-object` over the
    path would instead follow it and hash the target file -- naming the wrong object for
    a live link, and failing outright on a broken one that opens nothing -- so the link
    text is read with `readlink` and hashed on its own. The raw bytes go down
    `git hash-object --stdin`, which names them in the repository's object format just as
    the positional transport does, so a symlink a round inherited and the symlink it left
    compare in one namespace whatever object format the repository uses. None when the
    link cannot be read or git will not answer, matching the other identity helpers so a
    stall whose identity cannot be recomputed is kept rather than dropped as unchanged.
    """
    try:
        text = os.readlink(os.fsencode(path))
    except OSError:
        return None
    result = subprocess.run(
        ["git", "-C", str(repo), "hash-object", "--stdin"],
        input=text,
        capture_output=True,
    )
    if result.returncode != 0:
        return None
    return result.stdout.decode("utf-8", "replace").strip() or None


def blob_ids(repo: Path, paths: set[str]) -> dict[str, str]:
    """The content identity of each of `paths`, the ones that have none left out.

    Git's own object ids, from `git hash-object` over the whole set in one pass so a
    large dirty tree costs one child rather than one per path, and with the object
    format and clean filters the repository itself would apply -- so the content the
    round inherited and the content it left are named in one namespace, not a raw hash
    of the bytes that a filter or a sha256 repository would make disagree. Paths with
    no content to name are dropped before git is asked; a path that races the hash away
    and fails the batch is retried on its own, so one casualty does not take the rest
    of the set with it.

    A symlink is a work-tree object with a content identity too, but the positional
    transport cannot name it: `git hash-object` follows the link to its target. So the
    regular files are hashed in the batch above, and each symlink is named separately
    from its link text -- the mode `120000` blob git actually stores -- so a moved
    symlink stall is followed by the same content identity as any other file. See
    `link_blob_id`.
    """
    named = sorted(path for path in paths if has_content(repo / path))
    hashed = _hash_object(repo, named)
    if hashed is not None:
        resolved = dict(zip(named, hashed, strict=True))
    else:
        resolved = {}
        for path in named:
            one = _hash_object(repo, [path])
            if one:
                resolved[path] = one[0]
    for path in sorted(paths):
        link = repo / path
        if path not in resolved and link.is_symlink():
            blob = link_blob_id(repo, link)
            if blob is not None:
                resolved[path] = blob
    return resolved


def _argv_batches(names: list[str], budget: int = 8000) -> list[list[str]]:
    """`names` split into runs whose bytes fit an argv the platform will spawn.

    `git hash-object` takes its paths positionally so a newline in a name is carried
    through rather than read as a delimiter, but a command line has a length the
    platform enforces -- `E2BIG` on POSIX, a hard character cap on Windows -- where
    `--stdin-paths` had none. A conservative byte budget keeps each run well under the
    smallest such limit, so a large dirty tree costs a handful of children rather than
    raising. A single name past the budget is still a run of its own: a name cannot be
    split, and the run that holds it is refused or answered whole by git, not here.
    """
    batches: list[list[str]] = []
    current: list[str] = []
    used = 0
    for name in names:
        cost = len(name.encode()) + 1
        if current and used + cost > budget:
            batches.append(current)
            current, used = [], 0
        current.append(name)
        used += cost
    if current:
        batches.append(current)
    return batches


def _hash_object(repo: Path, names: list[str]) -> list[str] | None:
    """`git hash-object` for `names`, one id per name in order, or None when it would not answer.

    The names are passed positionally, after `--`, not down `--stdin-paths`. That
    transport delimits paths by newline, and a newline is a byte a POSIX filename may
    hold, so a name that carried one was split into several: the batch then hashed the
    wrong content under the wrong names, or returned a count that no longer lined up
    and left the name unhashed. A rewritten inherited-dirty path named that way
    compared as unchanged against itself and exited a run clean over work no review
    read. A positional argv carries every byte of the name through untouched, and git
    still reads the path's `.gitattributes` filters and the repository's object format
    from it, so the id is the one `git add` would store.

    None is the batch git refused -- a path that vanished under it among the reasons --
    told apart from an empty set, which is simply no ids to return. The id count is
    checked against the names because a partial answer cannot be lined back up with the
    paths that asked for it, and a mismatch is refused rather than misattributed. The
    call is split into runs whose argv stays within the length a platform will spawn --
    the limit `--stdin-paths` was reached for and this transport meets again -- each run
    keeping its order so the ids still line up with the names across the joins.
    """
    if not names:
        return []
    ids: list[str] = []
    for batch in _argv_batches(names):
        result = subprocess.run(
            ["git", "-C", str(repo), "hash-object", "--", *batch],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if result.returncode != 0:
            return None
        ids.extend(result.stdout.splitlines())
    return ids if len(ids) == len(names) else None


def object_id(path: Path) -> tuple[int, int] | None:
    """`path`'s filesystem identity -- device and inode -- or None when it has none.

    The identity a move keeps when an edit takes the bytes away. `rename` and
    `replace` carry a file's inode onto its new name, and a later truncating write
    reuses that same inode rather than making a new one, so the object git will
    not have committed is still the object sitting under the new name however much
    the round changed inside it. Content is the thing a move preserves and an edit
    destroys; the inode is the thing both preserve, on one filesystem.

    Read with `lstat`, not `stat`, so a symlink is named by its own directory entry
    rather than by whatever it points at: a move carries the link's inode, `stat`
    would follow it to the target's -- or find nothing at all for a broken link and
    report no identity -- and the object git has not committed is the link, not its
    target. For a regular file the two agree, so nothing else changes.

    A path that is gone or will not `lstat` has no identity, and neither has one
    whose inode a platform reports as zero -- some do rather than answer, and two
    such paths sharing that zero would be read as the same object when they are
    not. All are None rather than a value that would collide, matching `blob_id`.
    The device is carried alongside the inode because an inode number is only
    unique within its filesystem, and a cross-device move is exactly where the
    number can be reused for an unrelated object.
    """
    try:
        info = path.lstat()
    except OSError:
        return None
    if not info.st_ino:
        return None
    return (info.st_dev, info.st_ino)


def object_ids(repo: Path, paths: set[str]) -> dict[str, tuple[int, int]]:
    """The filesystem identity of each of `paths`, the ones that have none left out."""
    asked = ((path, object_id(repo / path)) for path in paths)
    return {path: obj for path, obj in asked if obj is not None}


def relocated_stall(
    repo: Path,
    present: set[str],
    stalled: set[str],
    inherited: dict[str, str],
    stalled_objects: set[tuple[int, int]],
) -> set[str]:
    """Which of `present` now holds an object an outstanding path held -- by inode or byte.

    The tie-breaker for where pathnames lie. Outstanding stalled work is followed
    by name, and a move is carried by taking the round's new dirt, `present_dirty
    - inherited_dirty`; under `--allow-dirty` a move's destination need not be new
    dirt at all. A round that moves the stalled object onto a pathname the run
    inherited dirty has the destination subtracted out by exactly the term that
    keeps an operator's own uncommitted changes from being read as this round's
    work, and the object is then outstanding under no name at all. What the move
    did not change is what identifies it.

    Its inode is the first such thing, and the one an edit cannot take away: a
    move on one filesystem lands the outstanding object under a new name with its
    inode intact, and a round that then rewrites the file in place keeps that same
    inode, so `stalled_objects` -- the identities of the outstanding paths as the
    round found them -- still names it when the bytes no longer do. An inode a
    move carried onto a fresh name was that object's alone when the round began,
    so a match there is the move and nothing else; no guard against inherited dirt
    is needed, because inherited dirt has an inode of its own.

    Its bytes are the second, for the move an inode cannot follow -- across a
    device boundary, where `rename` copies and the number is not kept. `stalled`
    is the content identity of the outstanding paths as the round found them and
    `inherited` the same for every path that was already dirty then. A destination
    counts on content only when the round put the stalled content there: a path
    whose content is what it was when the round started is the state the run
    inherited, whoever else happens to hold the same bytes, and reading it as the
    stall arriving would stop a run over an operator's own file. That guard is
    what keeps the content half this narrow; retaining every possible destination
    instead would cost a false stall on every round that touches inherited dirt.
    """
    if not stalled and not stalled_objects:
        return set()
    by_inode = {path for path, obj in object_ids(repo, present).items() if obj in stalled_objects}
    by_content = {
        path for path, blob in blob_ids(repo, present).items() if blob in stalled and blob != inherited.get(path)
    }
    return by_inode | by_content


def changed_inherited_dirty(
    repo: Path, present: set[str], inherited_dirty: set[str], inherited_blobs: dict[str, str]
) -> set[str]:
    """Inherited-dirty paths still dirty whose content this round rewrote.

    The conservative destination a stall's own identity can no longer name. An
    atomic save -- write a temporary file, rename it over the target -- lands the
    outstanding object under an inherited-dirty name with a fresh inode and fresh
    bytes, so neither identity `relocated_stall` matches on follows it there; all
    that is left to go on is that a move onto inherited dirt rewrites its
    destination. A path the run inherited dirty and still holds dirty whose content
    is no longer what it began with is such a rewrite, whatever put it there, and
    is where a stall that cannot be proven reviewed or removed might have landed.

    `inherited_blobs` is the content of every inherited-dirty path as the round
    found it; a path missing from it was empty or unreadable then. A path is
    changed when its content now differs from that -- one that gained content it
    had none of, or lost the content it had, among them -- and unchanged when the
    two agree, which is the operator's own file this round never touched and does
    not stop the run.

    A path still holding content whose identity could not be recomputed now -- a
    symlink whose `readlink` failed, a file `git hash-object` declined -- agrees
    with an inherited blob that was equally uncapturable only by both being absent,
    and would drop out as unchanged though nothing proved it so. Such a path is
    kept instead: the same conservative reading taken wherever an identity cannot be
    established, and safe because it fires only for a path that still holds an object
    (a symlink or a non-empty file), never the operator's own empty file or directory.
    """
    still = present & inherited_dirty
    now = blob_ids(repo, still)
    changed = {path for path in still if now.get(path) != inherited_blobs.get(path)}
    unrecomputed = {
        path for path in still if path not in now and (has_content(repo / path) or (repo / path).is_symlink())
    }
    return changed | unrecomputed


def listed(entries: str, limit: int) -> str:
    """`entries` as at most `limit` lines, with a count standing in for the rest."""
    lines = entries.splitlines()
    if len(lines) <= limit:
        return "\n".join(lines)
    return "\n".join([*lines[:limit], f"... and {len(lines) - limit} more"])


def in_work_tree(repo: Path, path: Path) -> str | None:
    """`path` as a pathspec relative to the work tree, or None when it is outside it.

    Outside the work tree git cannot see the path at all, and the repository root
    itself is not a directory this may treat as the loop's own.
    """
    with contextlib.suppress(ValueError, OSError):
        relative = path.resolve().relative_to(repo.resolve()).as_posix()
        return None if relative == "." else relative
    return None


def tracked(repo: Path, relative: str) -> bool:
    """Whether the repository has anything of its own committed under `relative`.

    A directory the operator tracks is the operator's. When git cannot answer,
    the safe reading is that it is theirs.
    """
    try:
        return bool(git(repo, "ls-files", "--", relative))
    except AgentError:
        return True


def paperwork_dir(repo: Path, root: Path, run_id: str) -> Path:
    """Create this run's review or log directory, invisible to the repository it reviews.

    The loop's paperwork lives inside `--repo`, and in a repository that does not
    ignore the path that breaks the loop twice over: the next run's `preflight`
    refuses to start on the tree the last run dirtied, and under `--allow-dirty`
    an agent's own `git add -A` commits the last run's review documents along
    with its work. This repository ignores `.agentic-loop/` and so never showed
    it; the tool exists to be pointed at repositories that do not.

    So the root carries a `.gitignore` of `*`, which git applies to the directory
    holding it -- including that `.gitignore` itself, so there is nothing left to
    report -- and nothing is asked of the repository under review. A repository
    that already ignores the path is undisturbed: git does not descend into an
    ignored directory, so its own rule keeps answering and this file is never
    even read.

    A root with tracked files under it is skipped. `--review-dir` pointed at a
    directory the operator committed is theirs, and `*` there would hide their
    files from git rather than the loop's. Everything else gets one, including a
    root left behind by a run from before this existed, which is the shape every
    repository the loop has already visited is in.

    This does not replace the `excluding()` pathspecs in `salvage_commit`. They
    guard different things: this covers every `git add -A` an agent runs and the
    preflight of the next run, and stands down on a tracked directory; those are
    the loop's own commit, and hold for the reviewer checkout too, which is
    outside the repository by default and never gets a file written into it.
    """
    directory = root / run_id
    directory.mkdir(parents=True, exist_ok=True)
    ignore = root / ".gitignore"
    relative = in_work_tree(repo, root)
    if not ignore.exists() and relative is not None and not tracked(repo, relative):
        ignore.write_text(PAPERWORK_IGNORE, encoding="utf-8")
    return directory


def salvage_commit(
    repo: Path,
    number: int,
    error: AgentError,
    paperwork: tuple[Path, ...] = (),
    *,
    subject: str = SALVAGE_SUBJECT,
    cause: str | None = None,
) -> str | None:
    """Commit what a round left in the tree when the round is not going to finish.

    A round commits once, at the end. An implementer stopped before it got there
    -- killed at `--timeout`, or exited non-zero -- therefore leaves the whole
    round's work uncommitted, and the loop is about to stop and take the record
    of it with it. This is not a hypothesis: measured rounds ran 2861s, 2621s and
    2173s against the default hour, the first of them 79% of the way to being
    killed, and a round that was killed cost work somebody committed by hand
    afterwards.

    So it is committed instead, marked in the subject as the incomplete thing it
    is. Nothing here was reviewed and the round's own checks may never have run;
    an incomplete commit says that plainly and the next run can build on it,
    where a discarded working tree says nothing and cannot be built on at all.

    `subject` and `cause` are the two sentences that say which unfinished thing
    this is. They are overridden for the round that ends with the implementer
    back from a second attempt and its work still uncommitted: nothing was cut
    short there, so the default wording would describe a failure that did not
    happen. Everything else about the commit is the same, because the thing being
    committed is the same: a round's work nobody stood behind.

    Returns the one-line description of the commit, or None when there was
    nothing to commit or git refused it. A refusal is reported and not raised:
    this runs while another failure is already on its way out, and replacing that
    failure with this one loses the reason the run stopped.
    """
    try:
        if not git(repo, "status", "--porcelain"):
            return None
        git(repo, "add", "-A", *excluding(repo, paperwork))
        if not git(repo, "diff", "--cached", "--name-only"):
            return None  # nothing but ignored files and this run's own paperwork
        cause = cause or ("the agent ran out of time" if isinstance(error, AgentTimeout) else "the agent stopped")
        git(
            repo,
            "commit",
            "-m",
            subject.format(number=number),
            "-m",
            f"{cause}, so this is a round's work part-way through rather than a finished change. "
            "Nothing in it has been reviewed and the checks the round would have run may never have run. "
            "It is committed rather than discarded so that the next run starts from it instead of from an "
            "unexplained working tree.\n\n"
            f"Stopped by: {error}",
        )
    except AgentError as refusal:
        print(
            f"warning: could not commit what round {number} left behind ({refusal}). "
            f"The work is still in {repo}; commit it by hand before starting another run.",
            file=sys.stderr,
        )
        return None
    line = git(repo, "log", "--oneline", "-1")
    print(f"\nround {number}: committed the unfinished work as {line}", flush=True)
    return line


def round_scratch(root: Path, number: int, role: str) -> Path:
    """Create one agent's scratch directory for one round, never reusing one.

    pytest deletes an existing `--basetemp` and immediately recreates it. On
    Windows a directory whose deletion is still pending -- one handle left open
    by the previous round is enough -- cannot be recreated, and the round dies
    with `PermissionError: [WinError 5]`. Left to itself pytest also builds a
    hardened `pytest-of-<user>` root, whose inheritance is stripped and whose
    access is granted through OWNER RIGHTS alone, which a differently-tokened
    process cannot use at all. Both were watched happening, repeatedly, in one
    session.

    Setting TMP and TEMP for *this* process does not decide it, because the agent
    writes its own pytest command line. So the directory is made here, handed
    over in the environment, and named in the prompt -- and it is a new one every
    round, so there is never a previous round's directory to trip over.
    """
    directory = root / f"round-{number:02d}-{role}"
    attempt = 0
    while directory.exists():
        attempt += 1
        directory = root / f"round-{number:02d}-{role}-{attempt}"
    directory.mkdir(parents=True)
    return directory


def scratch_block(scratch: Path) -> str:
    """The paragraph that tells an agent where its temporary files go."""
    return f"""
SCRATCH SPACE
{scratch.as_posix()}
That directory was created for this round and nothing has used it before. TMP,
TEMP and TMPDIR already point at it. If you run pytest, give it a subdirectory
that does not exist yet -- `--basetemp={scratch.as_posix()}/pytest-1`, then
`pytest-2` for the next run. Do not choose a base temp directory anywhere else
and never reuse one: pytest deletes an existing `--basetemp` and immediately
recreates it, which fails on Windows while any handle from the previous run is
still open.
"""


def create_review_checkout(mode: str, repo: Path, destination: Path) -> Path | None:
    """Give the reviewer a checkout of its own, separate from the implementer's tree.

    Sharing one working tree makes the two agents contend for it: the reviewer's
    test runs drop caches and temp directories into the tree the implementer is
    about to commit, and nothing but a prompt stops a reviewer from "just fixing"
    what it found. A checkout at the reviewed commit removes both by construction,
    and it pins the review to committed code rather than to whatever the tree
    happens to hold at that moment.

    The cost is real and worth stating: a fresh checkout contains committed files
    only. Virtualenvs, build output, and anything else gitignored are absent, so
    the reviewer cannot reuse the implementer's installed environment.
    """
    if mode == "none":
        return None
    destination.parent.mkdir(parents=True, exist_ok=True)
    if mode == "clone":
        # The retry below deletes the destination, so nothing this call did not
        # create may be sitting there. --review-checkout-dir is a caller's
        # value: pointed at the repository itself -- and under
        # tools/loop_in_container.py that is a read-write mount of the
        # operator's own checkout -- a clone git safely refuses would otherwise
        # be followed by a delete that does not refuse anything.
        if destination.exists():
            raise AgentError(
                f"the reviewer's checkout directory already exists: {destination}. "
                "Name one that does not, or remove it first."
            )
        # --local hardlinks the object store when both sides share a filesystem,
        # so this stays cheap; a separate object store also means the reviewer
        # cannot damage the implementer's repository.
        try:
            git(repo, "clone", "--local", "--no-checkout", str(repo), str(destination))
        except AgentError as error:
            # A hardlink cannot cross a filesystem boundary, and under
            # tools/loop_in_container.py the repository is a bind mount while
            # this checkout is on the container's own filesystem. git reports
            # "Invalid cross-device link" and stops rather than falling back, so
            # the fallback is here: copy the objects instead of linking them.
            # Every other failure is the caller's to see.
            if not HARDLINK_REFUSED.search(str(error)):
                raise
            shutil.rmtree(destination, ignore_errors=True)
            git(repo, "clone", "--local", "--no-hardlinks", "--no-checkout", str(repo), str(destination))
    elif mode == "worktree":
        git(repo, "worktree", "add", "--detach", str(destination), "HEAD")
    else:
        raise AgentError(f"unknown --review-checkout mode: {mode}")
    return destination


def sync_review_checkout(mode: str, repo: Path, checkout: Path, commit: str) -> None:
    """Move the reviewer's checkout to the commit under review and scrub the rest."""
    if mode == "clone":
        # Fetch HEAD explicitly rather than relying on branch refspecs: the
        # implementer may have committed onto a detached HEAD, and those commits
        # are reachable from no branch a default fetch would bring across.
        git(checkout, "fetch", "--no-tags", "--quiet", str(repo), "+HEAD:refs/agentic-loop/head")
    git(checkout, "checkout", "--detach", "--quiet", commit)
    # Whatever the previous round's review left behind is not part of this round.
    git(checkout, "clean", "-xfdq")


def remove_review_checkout(mode: str, repo: Path, checkout: Path) -> None:
    if mode == "worktree":
        with contextlib.suppress(AgentError):
            git(repo, "worktree", "remove", "--force", str(checkout))
    if checkout.is_dir():
        # Git marks pack files read-only, which stops a plain rmtree on Windows.
        for path in sorted(checkout.rglob("*"), reverse=True):
            with contextlib.suppress(OSError):
                path.chmod(0o700)
        shutil.rmtree(checkout, ignore_errors=True)


def claude_command(options: argparse.Namespace) -> list[str]:
    command = [options.claude_bin, "-p", "--output-format", "text"]
    if options.claude_model:
        command += ["--model", options.claude_model]
    if options.claude_effort:
        command += ["--effort", options.claude_effort]
    command += ["--permission-mode", options.claude_permission_mode]
    if options.claude_allowed_tools:
        command += ["--allowedTools", *options.claude_allowed_tools]
    command += options.claude_arg
    return command


def codex_command(
    options: argparse.Namespace,
    schema_path: Path,
    last_message_path: Path,
    review_dir: Path,
    workspace: Path,
    scratch: Path | None = None,
) -> list[str]:
    command = [options.codex_bin, "exec", "-"]
    if options.codex_model:
        command += ["--model", options.codex_model]
    if options.codex_effort:
        command += ["-c", f"model_reasoning_effort={options.codex_effort}"]
    # The review document is the one thing the reviewer writes outside its own
    # workspace, because it is the one thing the implementer has to read next.
    command += ["--add-dir", str(review_dir)]
    if scratch is not None:
        # Its scratch directory is outside the workspace too, and a sandbox that
        # cannot write there is a reviewer that puts its pytest base temp
        # directory wherever it *can* write -- which was the review directory,
        # beside the document it was writing.
        command += ["--add-dir", str(scratch)]
    command += ["--output-schema", str(schema_path), "--output-last-message", str(last_message_path)]
    command += ["--color", "never"]
    command += options.codex_arg
    # Last, after everything the caller passed through, because Codex takes the
    # final occurrence of either. `--codex-arg=--cd=/repo` is otherwise a
    # reviewer working in the implementer's tree -- under
    # tools/loop_in_container.py, the read-write repository mount -- and
    # `--codex-arg=--sandbox=...` a sandbox nobody chose. Naming the working
    # root rather than relying on the process's cwd is what makes the ordering
    # mean something.
    command += ["--cd", str(workspace)]
    command += ["--sandbox", options.codex_sandbox]
    return command


def implement_prompt(task: str, review_dir: Path, branch: str, scratch: Path) -> str:
    return f"""You are the implementer in an automated implement-then-review loop.

Repository branch: {branch}

TASK
{task}
{scratch_block(scratch)}{FOREGROUND_ONLY}
RULES
1. Implement the task in this repository. Follow the repository's own conventions
   and the instructions in AGENTS.md / CLAUDE.md if present.
2. Run the repository's lint and test commands for the code you touched, and fix
   what you break.
3. Commit your work with `git commit`. One or more commits is fine; at least one
   commit is mandatory. Do not push, do not amend commits that already existed,
   do not create branches, do not open pull requests.
4. Do not write anything into {review_dir.as_posix()} -- that directory belongs to
   the reviewer.
5. Finish by printing a short summary of what you changed and which checks you ran.
"""


def fix_prompt(task: str, review_path: Path, round_number: int, branch: str, scratch: Path) -> str:
    return f"""You are the implementer in an automated implement-then-review loop.
This is round {round_number}. An independent reviewer read your previous commits
and wrote findings to a review document.

Repository branch: {branch}

ORIGINAL TASK
{task}

REVIEW DOCUMENT
{review_path.as_posix()}
{scratch_block(scratch)}{FOREGROUND_ONLY}
RULES
1. Read the review document first. Address every finding in it.
2. If a finding is wrong or not worth acting on, do not silently skip it: say so
   explicitly in your final summary with your reasoning, so the next review round
   sees the disagreement.
3. Run the repository's lint and test commands for the code you touched.
4. Commit your work with `git commit`. At least one commit is mandatory. Do not
   push, do not amend existing commits, do not create branches.
5. Do not edit the review document and do not write into its directory.
6. Finish by printing a short summary: finding addressed, how, and anything you
   deliberately rejected.
"""


def finish_prompt(
    task: str,
    review_path: Path | None,
    round_number: int,
    branch: str,
    scratch: Path,
    leftover: str,
) -> str:
    """The prompt for an implementer that has to finish work it already wrote.

    Handing it the round's original prompt again would be asking for a second
    implementation on top of the first: the findings it was answering may already
    be answered in the files it would overwrite, and the round would lose the work
    it was started to rescue. So this one says what is in the tree, forbids
    discarding it, and asks for the one step that was missing.
    """
    findings = (
        f"""
REVIEW DOCUMENT
{review_path.as_posix()}
The uncommitted work below was an answer to it; read both before you judge what
is left to do."""
        if review_path is not None
        else ""
    )
    return f"""You are the implementer in an automated implement-then-review loop.
This is round {round_number}, and it has already run once. That attempt ended
without committing, and the work it wrote is still here, uncommitted, in the
working tree.

Repository branch: {branch}

ORIGINAL TASK
{task}
{findings}

UNCOMMITTED WORK
{listed(leftover, 50)}

That is `git status --porcelain` in the repository root; `git diff` and
`git diff --stat` show what is in those files.
{scratch_block(scratch)}{FOREGROUND_ONLY}
RULES
1. Read that work first, with `git status` and `git diff`. It is a previous
   attempt's and it may already be complete. Do not start the task again from the
   beginning, and do not overwrite any of those files before you have read them.
2. Never discard it: no `git checkout --`, `git restore`, `git reset --hard`,
   `git stash` or `git clean` over those paths.
3. Finish whatever is unfinished, run the repository's lint and test commands for
   the code it touches, and fix what you break.
4. Commit it with `git commit`. At least one commit is mandatory: committing is
   the only thing that gets this round's work into the review, and it is the step
   the previous attempt never reached.
5. If what is in the tree is not work worth keeping, remove exactly those files
   (the one exception to rule 2) and say so explicitly in your final summary with
   your reasoning. Leaving them and committing nothing is not a way to reject
   them: the loop commits a tree left dirty as unfinished work.
6. Do not push, do not amend commits that already existed, do not create
   branches, do not open pull requests, and do not write into the review
   directory.
7. Finish by printing a short summary: what the previous attempt had done, what
   you added, and which checks you ran.
"""


def review_prompt(
    task: str,
    review_path: Path,
    diff_range: str,
    round_number: int,
    previous_review: Path | None,
    isolated: bool,
    scratch: Path,
    has_network: bool,
) -> str:
    workspace = ""
    if isolated:
        workspace = """
YOUR WORKSPACE
You are in a checkout of your own, at the commit under review. The implementer's
working tree is elsewhere and you cannot reach it -- that is deliberate, and it
means nothing you do here can disturb the code being written. It also means this
checkout holds committed files only: virtualenvs, build output and anything else
gitignored are absent. Install what you need here if you want to run something,
and say so in the review if you could not run it.

The places you can write are this checkout, the scratch directory named below,
and the directory the review document goes in. Nowhere else.

If the project's tests will not run, do not report that as a defect in the code
unless you have established that the code is why. Say plainly in the review what
you could not run and what you checked instead.
"""
    network = ""
    if not has_network:
        network = """
NO NETWORK
You have no network access: you cannot open a URL, fetch an issue, or run
`gh issue view`. The implementer could. If the task above points at something
rather than stating it, you cannot read what it points at -- judge the code
against the text you were given and say in the review which part of the intent
you could not check, rather than assuming it.
"""
    history = ""
    if previous_review is not None:
        history = f"""
PREVIOUS REVIEW
{previous_review.as_posix()}
Read it. Verify each earlier finding was actually addressed. A finding that came
back, or was answered with a change that does not fix it, is still a finding.
"""
    return f"""You are the reviewer in an automated implement-then-review loop. This is
review round {round_number}. You review code. You never edit code, never commit,
never revert, and never run the implementer's task yourself.

ORIGINAL TASK
{task}
{workspace}{network}{scratch_block(scratch)}
WHAT TO REVIEW
The commits in `{diff_range}`. Start with `git log --stat {diff_range}` and
`git diff {diff_range}`. Read the surrounding files when the diff alone is not
enough to judge correctness.
{history}
WHAT COUNTS AS A FINDING
Correctness bugs, broken or missing error handling, security problems, data loss,
API/behaviour regressions, tests that do not test what they claim, and violations
of the repository's documented conventions. Do not report pure style preferences,
and do not invent work outside the original task.

OUTPUT 1 -- the review document
Write your review to exactly this path: {review_path.as_posix()}
Create the parent directory if it does not exist. Use this structure:

    # Review round {round_number}
    - Range: {diff_range}
    - Verdict: CLEAN or CHANGES REQUESTED

    ## Findings
    ### <N>. <short title> (severity: blocker|major|minor)
    - File: path:line
    - Problem: ...
    - Suggested fix: ...

    ## Verified from previous round
    (only for round 2 and later)

If you have no findings, still write the document with an empty Findings section
and state what you checked and why it is sound.

OUTPUT 2 -- the verdict
Your final message must be a single JSON object and nothing else:

    {{"status": "clean" | "changes_requested",
      "findings": <integer count of findings>,
      "review_file": "{review_path.as_posix()}",
      "summary": "<one or two sentences>"}}

Use "clean" only when findings is 0. Also include the line
REVIEW_STATUS: CLEAN or REVIEW_STATUS: CHANGES_REQUESTED inside the review
document so the verdict survives even if the JSON is lost.
"""


def parse_verdict(last_message_path: Path, review_path: Path) -> Verdict:
    text = last_message_path.read_text(encoding="utf-8") if last_message_path.is_file() else ""
    for candidate in _json_candidates(text):
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and payload.get("status") in {CLEAN, CHANGES_REQUESTED}:
            return Verdict(
                status=str(payload["status"]),
                findings=int(payload.get("findings", 0)),
                review_file=str(payload.get("review_file", review_path.as_posix())),
                summary=str(payload.get("summary", "")).strip(),
            )

    for source in (text, review_path.read_text(encoding="utf-8") if review_path.is_file() else ""):
        match = SENTINEL.search(source)
        if match:
            status = CLEAN if match.group(1).lower() == "clean" else CHANGES_REQUESTED
            return Verdict(
                status=status,
                findings=0 if status == CLEAN else -1,
                review_file=review_path.as_posix(),
                summary="parsed from REVIEW_STATUS sentinel; no structured verdict was returned",
            )

    raise AgentError(
        f"the reviewer returned no readable verdict (checked {last_message_path} and {review_path}); "
        "stopping rather than guessing whether the review was clean"
    )


def corroborate(verdict: Verdict, review_path: Path) -> Verdict:
    """Accept "clean" only when the review document backs it up.

    "Clean" is the value that ends the loop, so it is the one worth doubting.
    Codex emits intermediate messages in the requested schema, not just the final
    one -- an observed run announced status "clean" with an empty review_file
    before it had read a single line of the diff. The final message is the one
    read here, but the same failure at the end of a run would retire an unfinished
    review as a pass. A clean verdict therefore has to agree with the document it
    claims to have written.
    """
    if not verdict.is_clean:
        return verdict

    if not review_path.is_file():
        raise AgentError(
            f"the reviewer reported '{CLEAN}' but wrote no review document at {review_path}; "
            "refusing to end the loop on an unverifiable pass"
        )

    document = review_path.read_text(encoding="utf-8", errors="replace")
    match = SENTINEL.search(document)
    if match is not None and match.group(1).lower() != CLEAN:
        stated = f"{review_path.name} says {match.group(1)}"
    elif verdict.findings > 0:
        stated = f"the verdict itself reports findings={verdict.findings}"
    else:
        return verdict

    print(
        f"warning: the reviewer's verdict said '{CLEAN}' but {stated}; treating the round as changes_requested",
        file=sys.stderr,
    )
    return Verdict(
        status=CHANGES_REQUESTED,
        findings=max(verdict.findings, 1),
        review_file=verdict.review_file,
        summary=f"downgraded from clean: {stated}",
    )


def _json_candidates(text: str) -> list[str]:
    """Yield the whole message first, then any fenced or trailing JSON object."""
    stripped = text.strip()
    candidates = [stripped] if stripped else []
    candidates += re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    last_open = text.rfind("{")
    last_close = text.rfind("}")
    if last_open != -1 and last_close > last_open:
        candidates.append(text[last_open : last_close + 1])
    return candidates


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Loop Claude Code (implement + commit) against Codex (review) until the review is clean.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    task_group = parser.add_mutually_exclusive_group()
    task_group.add_argument("--task", help="The task Claude Code should implement. Required unless --start-with review.")
    task_group.add_argument("--task-file", type=Path, help="Read the task from this file instead of --task.")

    parser.add_argument("--repo", type=Path, default=Path.cwd(), help="Repository to work in.")
    parser.add_argument(
        "--start-with",
        default="implement",
        choices=["implement", "review"],
        help=(
            "'implement' writes code first, then reviews it. 'review' reviews commits that already exist "
            "and only then hands them to the implementer."
        ),
    )
    parser.add_argument(
        "--review-base",
        default=None,
        metavar="REF",
        help=(
            "With --start-with review: review REF..HEAD. Defaults to the merge base with the repository's "
            "main branch."
        ),
    )
    parser.add_argument(
        "--review-range",
        default=None,
        metavar="RANGE",
        help="With --start-with review: review this git range verbatim, e.g. 'abc123..def456'. Overrides --review-base.",
    )
    parser.add_argument(
        "--max-rounds",
        type=int,
        default=5,
        help="Maximum implement+review rounds. 0 with --start-with review means review once and stop.",
    )
    parser.add_argument(
        "--review-dir",
        type=Path,
        default=Path(".agentic-loop/reviews"),
        help="Directory for review documents, relative to --repo unless absolute.",
    )
    parser.add_argument(
        "--log-dir",
        type=Path,
        default=Path(".agentic-loop/logs"),
        help="Directory for agent transcripts, relative to --repo unless absolute.",
    )

    parser.add_argument("--claude-bin", default="claude", help="Claude Code executable.")
    parser.add_argument("--claude-model", default=None, help="Model for Claude Code, e.g. opus or sonnet.")
    parser.add_argument(
        "--claude-effort",
        default=None,
        # Passed through verbatim. Pinning the accepted set here would mean this
        # script has to be edited before a newly shipped effort level can be used,
        # and the CLI rejects a bad value on its own anyway.
        metavar="LEVEL",
        help="Reasoning effort for Claude Code, passed through to --effort.",
    )
    parser.add_argument(
        "--claude-permission-mode",
        default="acceptEdits",
        choices=["acceptEdits", "auto", "bypassPermissions", "dontAsk"],
        help="Claude Code permission mode; the loop is unattended, so no mode that prompts is offered.",
    )
    parser.add_argument(
        "--claude-allowed-tools",
        nargs="*",
        default=["Bash", "Edit", "Write", "Read", "Glob", "Grep"],
        help="Tools granted to Claude Code. Bash is required for git commit.",
    )
    parser.add_argument(
        "--claude-arg",
        action="append",
        # Not default=[]: argparse appends into the default object itself, which
        # leaks arguments between calls when this module is driven from a test.
        default=None,
        help="Extra argument passed through to the Claude Code CLI (repeatable).",
    )

    parser.add_argument("--codex-bin", default="codex", help="Codex executable.")
    parser.add_argument("--codex-model", default=None, help="Model for Codex, e.g. gpt-5.1-codex.")
    parser.add_argument(
        "--codex-effort",
        "--codex-reasoning-effort",
        dest="codex_effort",
        default=None,
        metavar="LEVEL",
        help="Codex reasoning effort, passed through as -c model_reasoning_effort.",
    )
    parser.add_argument(
        "--review-checkout",
        default="clone",
        choices=["clone", "worktree", "none"],
        help=(
            "How the reviewer gets its own copy of the code. 'clone' is a separate repository, "
            "'worktree' shares the object store, 'none' reviews in the implementer's tree."
        ),
    )
    parser.add_argument(
        "--review-checkout-dir",
        type=Path,
        default=None,
        help="Where to put the reviewer's checkout. Default: a run-specific directory under the system temp dir.",
    )
    parser.add_argument(
        "--keep-review-checkout",
        action="store_true",
        help="Leave the reviewer's checkout in place after the run instead of deleting it.",
    )
    parser.add_argument(
        "--codex-sandbox",
        default="workspace-write",
        choices=["read-only", "workspace-write", "danger-full-access"],
        help="Codex sandbox. workspace-write is the minimum that lets it write the review document.",
    )
    parser.add_argument(
        "--codex-arg",
        action="append",
        default=None,
        help="Extra argument passed through to the Codex CLI (repeatable).",
    )

    parser.add_argument("--timeout", type=float, default=3600.0, help="Seconds allowed per agent invocation.")
    parser.add_argument(
        "--heartbeat",
        type=float,
        default=60.0,
        help="Seconds of silence before printing how long the agent has been running. 0 prints nothing.",
    )
    parser.add_argument(
        "--stop-after-flat-rounds",
        type=int,
        default=2,
        # Two, not one: a single round that finds the same number is weak
        # evidence, because a fix legitimately exposes something new. Two
        # consecutive is the pattern that was actually observed at the end of a
        # run (10, 9, 4, 4 and 8, 7, 6), where the closing rounds moved the
        # remainder around at roughly an hour each. See --help for the rest.
        help=(
            "Stop when this many consecutive review rounds fail to get the finding count below the fewest "
            "seen so far. 0 keeps going until --max-rounds."
        ),
    )
    parser.add_argument(
        "--allow-dirty",
        action="store_true",
        help="Start even if the working tree has uncommitted changes; they will be swept into agent commits.",
    )
    parser.add_argument(
        "--continue-on-no-commit",
        action="store_true",
        help=(
            "Keep looping when an implementation round produces no commit and leaves nothing new in the "
            "working tree, instead of stopping as stalled. A round that left uncommitted work behind is "
            "recovered rather than reported as no commit, unless --no-stall-retry says otherwise."
        ),
    )
    parser.add_argument(
        "--stall-retry",
        action=argparse.BooleanOptionalAction,
        default=True,
        # The recovery is worth its cost by default: the round it rescues is a
        # finished one, and the alternative was a hand salvage the next morning.
        # It is not always worth it, though, and until now there was no way to
        # say so. The second invocation is another --timeout at worst, which for
        # an overnight run of an hour a round is a real slice of the night spent
        # on a round that has already failed once to commit what it wrote.
        help=(
            "Hand a round that left uncommitted work back to the implementer to finish and commit. "
            "--no-stall-retry reports the stall and the paths instead, leaves the work exactly where it "
            "is, and lets the no-commit rule above decide whether the run goes on."
        ),
    )
    parser.add_argument("--dry-run", action="store_true", help="Print the agent commands without running them.")
    options = parser.parse_args(argv)
    options.claude_arg = options.claude_arg or []
    options.codex_arg = options.codex_arg or []
    if options.start_with == "implement":
        if not (options.task or options.task_file):
            parser.error("--task or --task-file is required unless --start-with review")
        if options.max_rounds < 1:
            parser.error("--max-rounds must be at least 1 when starting with the implementer")
        for name in ("--review-base", "--review-range"):
            if getattr(options, name[2:].replace("-", "_")):
                parser.error(f"{name} only applies to --start-with review")
    elif options.max_rounds < 0:
        parser.error("--max-rounds cannot be negative")
    if options.stop_after_flat_rounds < 0:
        parser.error("--stop-after-flat-rounds cannot be negative")
    if options.heartbeat < 0:
        parser.error("--heartbeat cannot be negative")
    return options


def reviewer_network(sandbox: str, codex_arguments: list[str]) -> bool:
    """Whether the reviewer will be able to reach the network, read off its sandbox.

    This is worth answering rather than assuming, because the two agents do not
    have the same reach and it is the reviewer -- the one checking the work
    against the intent -- that is cut off. Claude Code runs with the network and
    can read a linked issue; Codex under `--sandbox workspace-write` reports the
    call blocked by its proxy. A task that points at a URL is therefore complete
    for one agent and empty for the other.

    Codex denies the network under `read-only` and `workspace-write` and allows
    it under `danger-full-access`; the one setting that moves it is read here in
    every spelling Codex accepts for a config override. Nothing about the host's
    own connectivity is claimed -- a machine that is offline anyway is offline
    for both agents, and this cannot see that.
    """
    if sandbox == "danger-full-access":
        return True
    if sandbox != "workspace-write":
        return False
    expects_setting = False
    for argument in codex_arguments:
        setting = None
        if expects_setting:
            expects_setting, setting = False, argument
        elif argument in ("-c", "--config"):
            expects_setting = True
        elif argument.startswith("--config="):
            setting = argument[len("--config=") :]
        elif argument.startswith("-c") and len(argument) > 2:
            setting = argument[2:].lstrip("=")
        if setting is not None and NETWORK_ENABLED.match(setting):
            return True
    return False


def agent_env(scratch: Path, checkout: Path | None = None) -> dict[str, str]:
    """The environment one agent runs a round in: its own scratch, its own code.

    The scratch directory is this round's and no other round's, which is what
    stops the reviewer inheriting a temporary directory a previous round left
    delete-pending or hardened. Handing it over here is half the job -- an agent
    that writes its own pytest command line ignores TMP and TEMP when it passes
    `--basetemp` -- so `scratch_block` names the same directory in the prompt.
    PYTEST_DEBUG_TEMPROOT is set as well: it is what decides where pytest builds
    its own `pytest-of-<user>` root when nobody passes `--basetemp` at all, and
    that root is the one whose permissions are unusable from another process.
    """
    env = dict(os.environ)
    env.update({key: str(scratch) for key in ("TMP", "TEMP", "TMPDIR")})
    env["PYTEST_DEBUG_TEMPROOT"] = str(scratch)
    if checkout is None:
        return env
    source = checkout / "src"
    if source.is_dir():
        # A review is of one commit, so the reviewer's tests have to import that
        # commit's code. Without this they import whatever an installed copy
        # resolves to, which is the implementer's working tree -- possibly
        # dirty, possibly mid-edit, and under tools/loop_in_container.py on the
        # far side of a bind mount. PYTHONPATH comes ahead of everything
        # site-packages adds, so it decides this rather than argues with it.
        existing = env.get("PYTHONPATH")
        env["PYTHONPATH"] = os.pathsep.join([str(source), *([existing] if existing else [])])
    return env


@dataclass
class ReviewSetup:
    """Everything a review round needs that does not change between rounds."""

    options: argparse.Namespace
    repo: Path
    checkout: Path | None
    task: str
    review_dir: Path
    log_dir: Path
    schema_path: Path
    scratch_root: Path
    has_network: bool


def implement_round(
    setup: ReviewSetup,
    record: RoundRecord,
    number: int,
    prompt: str,
    scratch: Path,
    paperwork: tuple[Path, ...],
    stage: str = "",
) -> None:
    """Run the implementer once, and commit what it leaves behind if it does not come back.

    A round commits once, at the end, so an implementer that raised never got
    there and the tree holds the whole round's work while the run is already on
    its way out. `salvage_commit` puts it in history first and the round records
    that the commit is the loop's own rather than one an agent stood behind.

    `stage` names a second invocation inside one round, so its transcript and its
    scratch directory do not land on the first one's. The first transcript is the
    evidence of what the stalled attempt did before it stopped, and overwriting it
    would destroy the record of the very thing being recovered from.
    """
    options = setup.options
    prefix = f"[claude r{number} {stage}]" if stage else f"[claude r{number}]"
    log_name = f"round-{number:02d}-claude-{stage}.log" if stage else f"round-{number:02d}-claude.log"
    try:
        run_agent(
            claude_command(options),
            prompt,
            setup.repo,
            prefix,
            setup.log_dir / log_name,
            options.timeout,
            options.dry_run,
            env=agent_env(scratch),
            heartbeat=options.heartbeat,
        )
    except CleanupUnconfirmed as error:
        # The killed agent's shells and test processes were not confirmed gone, so
        # they may still be writing to the tree. Salvage would `git add -A` and
        # commit underneath them, capturing a half-written tree as if it were the
        # round's work -- the race salvage exists to avoid, arriving through
        # cleanup that could not finish. Touch nothing: end the run with the tree
        # as it is and let an operator resolve the survivors.
        print(
            f"\nround {number}: {error}. The working tree was left untouched -- a killed "
            f"agent's processes may still be running in {setup.repo}; stop them and inspect the tree "
            "by hand before starting another run.",
            file=sys.stderr,
        )
        raise
    except AgentError as error:
        # This ends the run, and the round's work is uncommitted: a round commits
        # once, at the end, and the implementer never got there. Commit it before
        # the run leaves it behind. Safe here and not above because the tree is
        # quiescent: the agent's process tree was confirmed gone before this
        # raised (`_terminate_tree`).
        salvaged = salvage_commit(setup.repo, number, error, paperwork)
        if salvaged is not None:
            record.commits = [salvaged]
            record.salvaged = salvaged
        raise


def report_stall(record: RoundRecord, number: int, leftover: str) -> None:
    """Say what the round left in the tree, and record it, before anything is done about it.

    Shared by the two ways a stall is handled, because what the round *is* does
    not depend on which one runs: `run.json` says `stalled` either way, and the
    paths are named on the console and in the summary either way. Only `retried`
    tells the two apart, which is the whole of the difference.
    """
    print(
        f"\nround {number}: implementer stalled with uncommitted work. It produced no commit and left "
        f"{len(leftover.splitlines())} path(s) in the working tree:"
    )
    for line in listed(leftover, 20).splitlines():
        print(f"  {line}")
    record.stalled = True
    record.uncommitted_paths = leftover.splitlines()


def recover_stalled_round(
    setup: ReviewSetup,
    record: RoundRecord,
    number: int,
    task: str,
    branch: str,
    previous_review: Path | None,
    paperwork: tuple[Path, ...],
    inherited: str,
) -> None:
    """Get the work of a round that produced no commit but left a dirty tree into history.

    "The implementer produced no commit" is two rounds, not one, and the working
    tree is what tells them apart. A tree holding nothing the round did not start
    with is an implementer that looked at the findings and declined the round;
    that is the caller's business and this is never reached. Anything new is a
    round that was finished and never committed, because the implementer stopped
    between its last edit and its `git commit` -- an agent waiting on a
    notification its harness never delivered, which was watched happening and
    cost a hand salvage the next morning. `inherited` is what the tree held
    before this round's implementer ran, so the question stays "what did this
    round add" rather than "what is lying here"; see `newly_uncommitted`.

    Recovery is the round's own step, not a new mechanism: the implementer is
    handed its own uncommitted work and asked for the commit it never made. Only
    if it comes back a second time without one is the tree committed here, marked
    as the unreviewed work in progress it is, so the review round has something to
    read and the next run starts from a commit rather than from a working tree
    nobody can explain.

    Nothing here cleans, resets or stashes: whatever the round produced is on
    disk, and every path out of this leaves it there or commits it.
    """
    repo = setup.repo
    leftover = newly_uncommitted(inherited, uncommitted(repo, paperwork))
    report_stall(record, number, leftover)
    print("handing it back to be finished and committed; the work stays exactly where it is.")

    record.retried = True
    before = git(repo, "rev-parse", "HEAD")
    scratch = round_scratch(setup.scratch_root, number, "claude-finish")
    implement_round(
        setup,
        record,
        number,
        finish_prompt(task, previous_review, number, branch, scratch, leftover),
        scratch,
        paperwork,
        stage="finish",
    )
    moved = git(repo, "rev-parse", "HEAD") != before
    still_uncommitted = newly_uncommitted(inherited, uncommitted(repo, paperwork))
    if moved and not still_uncommitted:
        return  # it finished the job; the round has its commits and goes to review
    if not moved and not still_uncommitted:
        # It read its own leftovers and took them back out. That is the declined
        # round the caller was about to report, arrived at deliberately.
        print(f"\nround {number}: the implementer removed what it had left behind rather than commit it.")
        return
    # Uncommitted work remains either way: a moved `HEAD` must never stand in for
    # a finished round while non-paperwork changes are still sitting in the tree
    # -- an agent that committed part of the fix and left the rest dirty would
    # otherwise send only the partial commit to review and lose the remainder
    # exactly as if this recovery path did not exist. Salvage whatever is left,
    # on top of any commit the second attempt already made.
    salvaged = salvage_commit(
        repo,
        number,
        AgentError(f"the implementer left round {number}'s work uncommitted twice"),
        paperwork,
        subject=STALL_SUBJECT,
        cause="the agent produced the work and never committed it, twice",
    )
    if salvaged is not None:
        record.commits = [salvaged]
        record.salvaged = salvaged


def perform_review(
    setup: ReviewSetup,
    record: RoundRecord,
    number: int,
    diff_range: str,
    previous_review: Path | None,
    commit: str,
) -> tuple[Verdict, Path] | None:
    """Run one review round. Returns None under --dry-run, where there is no verdict."""
    options = setup.options
    review_path = setup.review_dir / f"round-{number:02d}.md"
    last_message_path = setup.log_dir / f"round-{number:02d}-verdict.txt"
    if setup.checkout is not None:
        sync_review_checkout(options.review_checkout, setup.repo, setup.checkout, commit)
    workspace = setup.checkout or setup.repo
    scratch = round_scratch(setup.scratch_root, number, "codex")
    run_agent(
        codex_command(options, setup.schema_path, last_message_path, setup.review_dir, workspace, scratch),
        review_prompt(
            setup.task,
            review_path,
            diff_range,
            number,
            previous_review,
            setup.checkout is not None,
            scratch,
            setup.has_network,
        ),
        workspace,
        f"[codex  r{number}]",
        setup.log_dir / f"round-{number:02d}-codex.log",
        options.timeout,
        options.dry_run,
        env=agent_env(scratch, setup.checkout),
        heartbeat=options.heartbeat,
    )
    if options.dry_run:
        return None
    verdict = corroborate(parse_verdict(last_message_path, review_path), review_path)
    record.review_file = str(review_path)
    record.status = verdict.status
    record.findings = verdict.findings
    record.summary = verdict.summary
    if not review_path.is_file():
        print(f"warning: the reviewer did not write {review_path}", file=sys.stderr)

    findings = "unknown" if verdict.findings < 0 else str(verdict.findings)
    print(f"\nround {number} verdict: {verdict.status} ({findings} findings)")
    if verdict.summary:
        print(f"  {verdict.summary}")
    print(f"  review: {review_path}")
    return verdict, review_path


def resolve_initial_range(repo: Path, options: argparse.Namespace, head: str) -> str:
    """Work out what a review-first run should look at.

    An explicit range wins, then an explicit base. Failing both, guess where this
    branch forked from -- and if that guess would produce an empty range, say so
    instead of handing the reviewer nothing and calling the result clean.
    """
    if options.review_range:
        return options.review_range
    if options.review_base:
        base = git(repo, "rev-parse", "--verify", f"{options.review_base}^{{commit}}")
        if base == head:
            raise AgentError(f"--review-base {options.review_base} is HEAD; there would be nothing to review")
        return f"{base}..{head}"

    forks: dict[str, list[str]] = {}
    for candidate in MAIN_BRANCH_CANDIDATES:
        try:
            reference = git(repo, "rev-parse", "--verify", "--quiet", f"{candidate}^{{commit}}")
            base = git(repo, "merge-base", reference, head)
        except AgentError:
            continue  # the ref does not exist here, or shares no history with HEAD
        if base != head:  # HEAD is not ahead of this one, so it defines no range
            forks.setdefault(base, []).append(candidate)

    if len(forks) == 1:
        base, names = next(iter(forks.items()))
        count = git(repo, "rev-list", "--count", f"{base}..{head}")
        print(f"review base : {names[0]} (merge base {base[:12]}, {count} commits)")
        return f"{base}..{head}"

    if not forks:
        raise AgentError(
            "HEAD is not ahead of any of "
            f"{', '.join(MAIN_BRANCH_CANDIDATES)}, so there is no range to review. "
            "Pass --review-base REF or --review-range RANGE."
        )

    # Several plausible bases that disagree -- a repository holding both 'main'
    # and 'master', say. Guessing here is how a one-commit review silently
    # becomes a fifty-commit one, so the caller has to say which it meant.
    detail = ", ".join(
        f"{'/'.join(names)} -> {git(repo, 'rev-list', '--count', f'{base}..{head}')} commits"
        for base, names in forks.items()
    )
    raise AgentError(
        f"cannot tell what to review: HEAD forks from several candidate branches ({detail}). "
        "Pass --review-base REF or --review-range RANGE."
    )


def resolve_executable(name: str, label: str) -> str:
    found = shutil.which(name)
    if not found:
        raise AgentError(f"{label} executable not found on PATH: {name}")
    return found


def preflight(options: argparse.Namespace) -> tuple[Path, str, str]:
    repo = git(options.repo, "rev-parse", "--show-toplevel")
    repo_path = Path(repo).resolve()
    try:
        head = git(repo_path, "rev-parse", "HEAD")
    except AgentError as error:
        raise AgentError("the repository has no commits yet; make an initial commit first") from error
    branch = git(repo_path, "rev-parse", "--abbrev-ref", "HEAD")
    if not options.allow_dirty:
        dirty = git(repo_path, "status", "--porcelain")
        if dirty:
            raise AgentError(
                "the working tree is not clean. Commit or stash first, or pass --allow-dirty to let the "
                "agents commit the existing changes along with their own."
            )
    options.claude_bin = resolve_executable(options.claude_bin, "Claude Code")
    options.codex_bin = resolve_executable(options.codex_bin, "Codex")
    return repo_path, head, branch


def under(repo: Path, path: Path) -> Path:
    return path if path.is_absolute() else repo / path


def commits_since(repo: Path, previous: str) -> tuple[str, list[str]]:
    """HEAD, and the one-line log of everything committed since `previous`."""
    head = git(repo, "rev-parse", "HEAD")
    if head == previous:
        return head, []
    return head, git(repo, "log", "--oneline", f"{previous}..{head}").splitlines()


def rounds_without_progress(findings: list[int]) -> int:
    """How many of the most recent rounds failed to get below the fewest findings before them.

    Two runs of this loop went 10, 9, 4, 4 and 8, 7, 6. The early rounds close
    substance; the late ones move the remainder around at roughly an hour each,
    and `--max-rounds` cannot tell the difference because it only counts. This is
    the number that can: a run of rounds where the review is no smaller than the
    smallest it has already been.

    Compared against the fewest seen rather than against the previous round, so
    4 -> 5 -> 4 counts as two rounds of no progress instead of resetting on the
    way back down to where it already was. A count of -1 is a verdict read from
    the REVIEW_STATUS sentinel with no number in it; an unknown quantity is
    neither progress nor the absence of it, so it is left out entirely.
    """
    fewest: int | None = None
    flat = 0
    for count in (count for count in findings if count >= 0):
        if fewest is None or count < fewest:
            fewest, flat = count, 0
        else:
            flat += 1
    return flat


def main(argv: list[str] | None = None) -> int:
    # Agent output is UTF-8; the default Windows console codepage is not. Without
    # this, the first smart quote an agent prints takes the run down.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    options = parse_args(argv)
    try:
        repo, baseline, branch = preflight(options)
    except AgentError as error:
        print(f"error: {error}", file=sys.stderr)
        return EXIT_FAILED

    if options.task:
        task = options.task
    elif options.task_file:
        task = options.task_file.read_text(encoding="utf-8").strip()
    else:
        task = NO_TASK
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    review_root = under(repo, options.review_dir)
    log_root = under(repo, options.log_dir)
    review_dir = paperwork_dir(repo, review_root, run_id)
    log_dir = paperwork_dir(repo, log_root, run_id)

    schema_path = log_dir / "verdict-schema.json"
    schema_path.write_text(json.dumps(VERDICT_SCHEMA, indent=2), encoding="utf-8")

    # The two directories below live in shared temporary storage, so the name has
    # to identify this run and not merely this second. `run_id` names the second,
    # and the repository is often called the same thing twice on one machine (a
    # clone per agent, a fixture repository per suite), so two runs that started
    # together got one scratch root between them and swept each other's round
    # directories out from under themselves. The token is what distinguishes
    # them, and it is random rather than the process id because two runs in one
    # process are still two runs. The paperwork inside the repository keeps
    # naming the run by its time alone: that name is for a person to read, and
    # no two runs share a clone.
    temporary_name = f"{repo.name}-{run_id}-{secrets.token_hex(3)}"
    # Outside the repository by default: a checkout nested inside the tree it
    # isolates the reviewer from is not isolation, and git would have to be told
    # to ignore it.
    checkout_dir = options.review_checkout_dir or (
        Path(tempfile.gettempdir()) / "agentic-loop-review" / temporary_name
    )
    # Outside the repository for the same reason, and because everything under it
    # is a temporary directory an agent's tools created: none of it belongs in a
    # tree that is about to be committed, and `git add -A` in salvage_commit
    # would otherwise be the thing that swept it in.
    scratch_root = Path(tempfile.gettempdir()) / "agentic-loop-scratch" / temporary_name
    # The directories that are the loop's own rather than the round's work. Every
    # question this run asks about the working tree, and every commit it makes of
    # one, leaves them out: a review of a round is not part of that round.
    paperwork = (review_root, log_root, checkout_dir)
    has_network = reviewer_network(options.codex_sandbox, options.codex_arg)

    print(f"repository : {repo}")
    print(f"branch     : {branch}")
    print(f"baseline   : {baseline[:12]}")
    print(f"run id     : {run_id}")
    default = "cli default"
    print(f"implementer: claude model={options.claude_model or default} effort={options.claude_effort or default}")
    print(f"reviewer   : codex  model={options.codex_model or default} effort={options.codex_effort or default}")
    print(f"reviews    : {review_dir}")
    print(f"logs       : {log_dir}")
    print(f"scratch    : {scratch_root} (one directory per agent per round)")
    print(f"max rounds : {options.max_rounds}")
    if options.stop_after_flat_rounds:
        print(f"stop early : after {options.stop_after_flat_rounds} rounds that do not reduce the finding count")
    if not options.stall_retry:
        # Only when it is off. The retry is the default and saying so every run
        # would be a line nobody reads; the run that will not make it is the one
        # whose header has to say so before an operator waits for it.
        print("stall retry: off (--no-stall-retry); a round that leaves work uncommitted is reported, not finished")
    print(f"starts with: {options.start_with}")
    if has_network:
        print(f"network    : reviewer has network (codex --sandbox {options.codex_sandbox})")
    else:
        print(
            f"network    : reviewer has no network (codex --sandbox {options.codex_sandbox}); it cannot open a URL "
            "or run `gh issue view`, so the task has to carry what it means"
        )
        if POINTS_OUTWARD.search(task):
            # The implementer would read it and the reviewer would not, which
            # makes the description complete for the agent writing the code and
            # empty for the one checking it against the intent.
            print(
                "warning: the task points at something only a networked agent can read, and the reviewer is not "
                "one. Paste the content into --task/--task-file instead of linking to it.",
                file=sys.stderr,
            )
    initial_range = ""
    if options.start_with == "review":
        try:
            initial_range = resolve_initial_range(repo, options, baseline)
        except AgentError as error:
            print(f"error: {error}", file=sys.stderr)
            return EXIT_FAILED
        print(f"review of  : {initial_range}")
    if options.review_checkout == "none":
        print("review tree: the implementer's working tree (--review-checkout none)")
    else:
        print(f"review tree: {checkout_dir} ({options.review_checkout})")

    rounds: list[RoundRecord] = []
    previous_review: Path | None = None
    exit_code = EXIT_ROUNDS_EXHAUSTED
    last_head = baseline
    checkout: Path | None = None
    # Paths of stalled work that no review has seen, carried across rounds by
    # name. A round that leaves its own work uncommitted adds its paths; a later
    # round that commits or removes one drops it, whatever porcelain status it wore
    # in between. While anything is in here a clean verdict cannot end the run,
    # however many rounds after the stall it arrives.
    outstanding_paths: set[str] = set()

    try:
        if not options.dry_run:
            checkout = create_review_checkout(options.review_checkout, repo, checkout_dir)
        setup = ReviewSetup(options, repo, checkout, task, review_dir, log_dir, schema_path, scratch_root, has_network)

        if options.start_with == "review":
            # Round 0 reviews what is already committed. The implementer has not
            # run yet, so there is nothing of its own to report -- only a verdict.
            record = RoundRecord(number=0)
            rounds.append(record)
            print(f"\n{'=' * 72}\nround 0 (review of existing commits)\n{'=' * 72}")
            print(f"  range: {initial_range}")
            reviewed = perform_review(setup, record, 0, initial_range, None, baseline)
            if reviewed is None:  # --dry-run: the commands were printed, nothing ran
                print("\n[dry-run] stopping after the initial review; no verdict was produced.")
                exit_code = EXIT_CLEAN
                raise _Done
            verdict, review_path = reviewed
            if verdict.is_clean:
                print("\nthe existing commits reviewed clean; nothing for the implementer to do.")
                exit_code = EXIT_CLEAN
                raise _Done
            if options.max_rounds == 0:
                print("\n--max-rounds is 0: reviewed once, not handing the findings to the implementer.")
                raise _Done
            previous_review = review_path

        for number in range(1, options.max_rounds + 1):
            record = RoundRecord(number=number)
            rounds.append(record)
            print(f"\n{'=' * 72}\nround {number}/{options.max_rounds}\n{'=' * 72}")

            scratch = round_scratch(scratch_root, number, "claude")
            if previous_review is None:
                prompt = implement_prompt(task, review_dir, branch, scratch)
            else:
                prompt = fix_prompt(task, previous_review, number, branch, scratch)
            # What the tree already held before this round's implementer touched
            # it. Under --review-checkout none the reviewer shares that tree, and
            # under --allow-dirty the operator's own changes are in it, so this
            # is what keeps either from being read as the round's own work.
            inherited = "" if options.dry_run else uncommitted(repo, paperwork)
            # The same "what did this round start with" question by pathname, for
            # reconciling the carried-across stall below independently of porcelain
            # status; see `dirty_paths`.
            inherited_dirty = set() if options.dry_run else dirty_paths(repo, paperwork)
            # And by content and by filesystem identity, for the reconciliation
            # the pathnames cannot answer. A move can change an outstanding path's
            # name and can land it on a name that was already dirty, where the
            # subtraction below cannot see it. A pure move does not change the
            # bytes, and no move on one filesystem changes the inode even when the
            # round then edits the file in place; between them they still name the
            # object the tree has not committed. See `relocated_stall`. An atomic
            # save -- write a temp file and rename it over the destination --
            # changes both, so a move onto inherited dirt made that way is named by
            # neither; when a followed name has vanished the reconciliation then
            # keeps every inherited-dirty path the round rewrote, since the stall's
            # edited work may be hiding in one and no proof taken at a pathname can
            # tell that tree from an operator editing their own file beside a
            # committed stall. See `changed_inherited_dirty`. These are asked only
            # while something is outstanding to recognise: otherwise there is nothing
            # to compare against, and a dirty tree can be a large read.
            inherited_blobs = {} if options.dry_run or not outstanding_paths else blob_ids(repo, inherited_dirty)
            stalled_blobs = {inherited_blobs[path] for path in outstanding_paths if path in inherited_blobs}
            outstanding_ids = {} if options.dry_run or not outstanding_paths else object_ids(repo, outstanding_paths)
            stalled_objects = set(outstanding_ids.values())
            try:
                implement_round(setup, record, number, prompt, scratch, paperwork)
            except AgentError:
                # A salvage commit is the run's last commit, and the summary about
                # to be written reports the range that ends at it.
                if record.salvaged is not None:
                    last_head = git(repo, "rev-parse", "HEAD")
                raise

            head, new_commits = (last_head, []) if options.dry_run else commits_since(repo, last_head)
            record.commits = new_commits
            # Anything here is not a round that was declined: it is a round whose
            # work is sitting in the tree because the implementer never reached
            # its own commit.
            left_behind = (
                "" if new_commits or options.dry_run else newly_uncommitted(inherited, uncommitted(repo, paperwork))
            )
            # This round's share of work left uncommitted because the retry was
            # declined. Its paths are folded into `outstanding_paths` below, which
            # carries such work across rounds: the review runs on a commit-only
            # range -- and in the default separate checkout these files do not
            # exist at all -- so no round's clean verdict ever inspects it.
            kept_uncommitted = ""
            if left_behind and options.stall_retry:
                try:
                    recover_stalled_round(setup, record, number, task, branch, previous_review, paperwork, inherited)
                except AgentError:
                    if record.salvaged is not None:
                        last_head = git(repo, "rev-parse", "HEAD")
                    raise
                head, new_commits = commits_since(repo, last_head)
                record.commits = new_commits
            elif left_behind:
                report_stall(record, number, left_behind)
                print("not handing it back: --no-stall-retry. The work stays exactly where it is.")
                kept_uncommitted = left_behind
            # Reconcile the run's outstanding stalled work against the tree as it
            # stands now, by pathname. Prior rounds' leftovers that a later round
            # committed into a reviewed range, or took back out, have left the
            # working-tree status and drop off here; one whose only change was its
            # porcelain status -- untracked to staged, say -- is still dirty and so
            # is kept. This round's own leftover joins what remains: the paths dirty
            # now that the round did not start with, added when it was not handed
            # back. The guard after the review reads the total, not just this
            # round's share.
            if not options.dry_run:
                present_dirty, moved = dirty_records(repo, paperwork)
                # A rename is the one change a name cannot survive: the work is
                # still there, uncommitted and unreviewed, under a name no
                # earlier round ever saw. Following it first is what keeps the
                # filter below from reading `git mv` as "committed or taken
                # back". Applied every round, so a path renamed twice arrives.
                followed = {moved.get(path, path) for path in outstanding_paths}
                survived = {path for path in followed if path in present_dirty}
                # An outstanding name that leaves the dirty set is safe to drop
                # only when its work reached a commit a review read or came back
                # out of the tree. Porcelain records a rename git has content to
                # detect -- a staged or committed one, followed above -- but an
                # untracked file moved on the filesystem leaves none, so its name
                # vanishes while the work lives on under a name this round did not
                # start with, among `present_dirty - inherited_dirty`. The commit
                # diff cannot tell that apart from a committed one: the same
                # pathname can reach a commit as an unrelated new file while the
                # moved content sits untracked under its new name, so membership
                # in the diff is no proof the outstanding object was committed.
                # Whenever a followed name disappears with no porcelain rename to
                # explain it, carry the round's new dirt. A round that genuinely
                # finished the work left a clean tree, so `present_dirty` holds
                # nothing new to carry; one that moved the work away left it dirty
                # under its new name, and carrying keeps it from slipping the guard.
                vanished = followed - survived
                carry_new = bool(kept_uncommitted) or bool(vanished)
                outstanding_paths = survived
                if carry_new:
                    outstanding_paths |= present_dirty - inherited_dirty
                    # That subtraction assumes the move landed on clean ground.
                    # Under --allow-dirty it need not have: a destination the run
                    # inherited dirty is removed by the same term, and the work
                    # would be outstanding under no name at all. Whatever now
                    # holds the stalled object's own inode -- or, where the move
                    # crossed a device and could not carry it, its bytes -- is
                    # that name.
                    outstanding_paths |= relocated_stall(
                        repo, present_dirty, stalled_blobs, inherited_blobs, stalled_objects
                    )
                    # Both of those identities are the object's own, and neither
                    # survives the round editing the stall and relocating it. An
                    # atomic save -- write the edited bytes to a temporary file and
                    # rename it over the destination -- gives the inherited-dirty name
                    # the stall landed on a fresh inode and fresh bytes, so neither
                    # identity above follows it there. Once a followed name has
                    # vanished, then, the round may have rewritten an inherited-dirty
                    # path with the stall's outstanding work, and no proof taken at a
                    # pathname can rule that out. A `git commit` leaves the work tree
                    # be, so it is tempting to read an object still sitting at its
                    # followed name with the inode and bytes it began with as reviewed;
                    # but a hard link puts that original inode and those original bytes
                    # at any name -- the followed one included, once an alias is moved
                    # back onto it -- while the edited work sits elsewhere, and a round
                    # that simply writes that work into the inherited-dirty file in
                    # place needs no link at all. The two are one tree: a stall
                    # committed under its own name beside an operator's edited file, and
                    # edited stall work hidden in that file beside a stale commit of the
                    # original bytes, leave the very same inodes and the very same
                    # content, differing only in what the bytes mean. So the
                    # inherited-dirty paths this round rewrote are kept whenever a
                    # followed name vanished, without asking a forgeable proof to clear
                    # them -- a false stall the operator clears by committing being the
                    # safe error where a false clean exits 0 over work no review saw.
                    # See `changed_inherited_dirty`; the narrower `relocated_stall`
                    # identities above still carry every move they can name, so this
                    # retains only what they could not.
                    if vanished:
                        outstanding_paths |= changed_inherited_dirty(
                            repo, present_dirty, inherited_dirty, inherited_blobs
                        )
            if not new_commits and not options.dry_run:
                print(f"\nround {number}: Claude Code produced no commit.")
                if not options.continue_on_no_commit:
                    print("stopping as stalled; pass --continue-on-no-commit to keep going.", file=sys.stderr)
                    exit_code = EXIT_STALLED
                    break
            else:
                for line in new_commits:
                    print(f"  commit {line}")

            diff_range = f"{last_head}..{head}" if head != last_head else f"{baseline}..{head}"
            last_head = head

            reviewed = perform_review(setup, record, number, diff_range, previous_review, head)
            if reviewed is None:  # --dry-run
                print("\n[dry-run] stopping after one round; no verdict was produced.")
                exit_code = EXIT_CLEAN
                break

            verdict, review_path = reviewed
            if verdict.is_clean:
                if outstanding_paths:
                    # A clean verdict cannot terminate the run while a stalled
                    # round's work is still sitting uncommitted and unreviewed --
                    # whether this round left it or an earlier one did. Every
                    # review measured committed history, so calling that clean
                    # would exit 0 over changes nobody looked at. Stop as stalled
                    # instead, and leave the work exactly where it is.
                    print(
                        f"\nround {number}: the review came back clean, but "
                        f"{len(outstanding_paths)} path(s) a stalled round left uncommitted "
                        "are still in the tree, and the review never saw them; stopping as stalled rather than "
                        "calling the run clean. Commit the work, or drop --continue-on-no-commit.",
                        file=sys.stderr,
                    )
                    exit_code = EXIT_STALLED
                    break
                print(f"\nreview came back clean after {number} round(s).")
                exit_code = EXIT_CLEAN
                break

            previous_review = review_path
            counts = [entry.findings for entry in rounds if entry.findings is not None and entry.findings >= 0]
            flat = rounds_without_progress(counts)
            if options.stop_after_flat_rounds and flat >= options.stop_after_flat_rounds:
                print(
                    f"\n{flat} rounds in a row have not got the review below {min(counts)} findings; stopping "
                    "rather than spending another round moving the remainder around. Raise "
                    "--stop-after-flat-rounds, or set it to 0, to keep going.",
                    file=sys.stderr,
                )
                exit_code = EXIT_NO_PROGRESS
                break
        else:
            print(f"\nreached --max-rounds ({options.max_rounds}) with findings still open.", file=sys.stderr)
    except _Done:
        pass
    except AgentError as error:
        print(f"\nerror: {error}", file=sys.stderr)
        exit_code = EXIT_FAILED
    except KeyboardInterrupt:
        print("\ninterrupted.", file=sys.stderr)
        exit_code = EXIT_FAILED
    finally:
        if checkout is not None:
            if options.keep_review_checkout:
                print(f"\nreviewer checkout kept at {checkout}")
            else:
                remove_review_checkout(options.review_checkout, repo, checkout)
        # Per-round directories only exist so that no round inherits another
        # round's; a run that left them behind would be the next run's problem.
        # Errors are ignored on purpose: a scratch directory a tool still holds
        # open is not a reason to change what this run reports.
        if scratch_root.is_dir():
            shutil.rmtree(scratch_root, ignore_errors=True)

    summary = {
        "run_id": run_id,
        "repository": str(repo),
        "branch": branch,
        "baseline": baseline,
        "head": last_head,
        "task": task,
        "claude_model": options.claude_model,
        "claude_effort": options.claude_effort,
        "codex_model": options.codex_model,
        "codex_effort": options.codex_effort,
        "review_checkout": options.review_checkout,
        "start_with": options.start_with,
        "initial_range": initial_range or None,
        "max_rounds": options.max_rounds,
        "stop_after_flat_rounds": options.stop_after_flat_rounds,
        "stall_retry": options.stall_retry,
        "reviewer_network": has_network,
        "exit_code": exit_code,
        "rounds": [vars(record) for record in rounds],
    }
    summary_path = log_dir / "run.json"
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(f"\nsummary: {summary_path}")
    if last_head != baseline:
        print(f"commits: git log --oneline {baseline[:12]}..{last_head[:12]}")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
