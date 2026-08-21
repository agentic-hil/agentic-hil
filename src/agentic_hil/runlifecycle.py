"""What a test run is, once it outlives the command that started it.

A run used to be an invocation: it existed while a process sat in front of it
and was gone when that process returned. An endurance plan bounded by wall time
is hours of that, and the only way to end one early was to kill the process,
which is the dead-owner route: the bench is left to the recovery machinery
instead of to an orderly finish.

Three things make a run addressable instead. A **handle**, minted when the run
starts and printed at once, names it. A **record** under the coordination state
says what that handle is doing, written only by the process running it. A
**stop request** is a second file, written by anybody who can name the handle
and read by the run itself between steps.

Two files rather than one field, because they have two writers. A record with a
progress field and a stop field would be written by the run and by whoever wants
it to end, and two writers on one file is how a stop request gets erased by the
next progress update. Each file here has exactly one writer and any number of
readers.

Whether the process behind a handle is still there is not asked of its pid. A
pid is reused, and "is pid 4213 alive" answers a different question a moment
later. The run holds a lifetime lock for as long as it runs, the same kind the
bench mutex holds on a device, so the operating system answers instead: a lock
that can be taken is a lock whose holder is gone, and a worker killed hard is
therefore the same thing to this module as it is to the bench.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import subprocess
import sys
import time
from pathlib import Path
from types import TracebackType

from agentic_hil.bench import MAX_WAIT_S, _LifetimeLock, utc_now_iso, validated_wait
from agentic_hil.config import (
    ConfigError,
    atomic_write_text,
    derived_state_directory,
    display_path,
    safe_read_text,
)
from agentic_hil.process import spawn_detached_process
from agentic_hil.report import last_report_path
from agentic_hil.types import AgenticHILConfig, JsonObject

RUN_RECORD_VERSION = 1
# A handle is minted here and never taken from anywhere else, so this pattern is
# the whole of what a caller may name: a handle is turned into a file name, and
# a caller-supplied string that is not this shape is refused rather than
# sanitized into some neighbouring path.
RUN_HANDLE_PATTERN = re.compile(r"^run-[0-9a-f]{16}$")
# How long the start command waits for a detached worker to publish what it is
# doing. Not a wait for the report: the worker publishes as soon as it holds its
# devices, or as soon as it knows it cannot have them. Bounded, because a handle
# printed for a worker that never came up would be a handle that means nothing.
WORKER_PUBLISH_TIMEOUT_S = 30.0
WORKER_PUBLISH_POLL_S = 0.05
# The least time between two writes of a run's record. A plan whose steps are
# fast would otherwise write the record thousands of times a second to say what
# it is on. The step about to run is always published before it runs, so what
# this bounds is repetition, not freshness.
PROGRESS_WRITE_INTERVAL_S = 0.25
# The least time between two looks at the stop file. Asked between every step,
# so a plan of very short steps must not turn a stop check into the run's main
# expense. Far below the slice a waiting step is interrupted on, so it costs a
# stop nothing in responsiveness.
STOP_POLL_INTERVAL_S = 0.1
# How many finished runs the coordination state keeps. Every run leaves a
# record, including the synchronous ones, so without this the directory grows
# for the life of the bench. Only terminal records are ever removed, oldest
# first, and a record whose run is still going is not a candidate at all.
RUN_RECORDS_KEPT = 100
# How often a record write is retried while somebody is reading it. One process
# writes a record and any number poll it, and on Windows a rename over a file
# another process has open fails outright: without this, a status poll landing
# on the wrong millisecond would silently cost a run the one write that says it
# is running, and the start command would then time out on a run that was fine.
RECORD_WRITE_ATTEMPTS = 10
RECORD_WRITE_BACKOFF_S = 0.02
# The reader's half of the same contention, and it has more than one face. A
# read that lands in the instant the writer's os.replace swaps the record meets
# whichever one the platform shows. Windows answers a sharing violation. POSIX
# answers with the file rule instead: the rename drops the replaced inode's link
# count to zero while the reader still holds it open, which reads as "not a
# single-link regular file", and a reader that opens a moment earlier finds the
# name leading to a different inode by the time it checks, which reads as
# "changed while it was being opened". A writer that is not atomic adds two
# more, a name that is briefly absent and a file only half of which has landed.
# None of those is a state the record is in; each is a moment between two states,
# so the read waits it out a few times before it calls the state unreadable.
_RECORD_READ_ATTEMPTS = 6
_RECORD_READ_RETRY_DELAY_S = 0.05
# How long after a worker process exits the start command waits for its record
# before calling it a failure. Not zero, because the process it spawned is not
# always the process that runs the plan: an interpreter that re-executes itself
# exits as soon as its own child is up, and the record is what says which
# process is really running this handle.
WORKER_EXIT_GRACE_S = 2.0

# The states a record can be in. `starting` is the window between the start
# command writing the record and the worker holding its devices; the rest are
# what a reader acts on.
RUN_STARTING = "starting"
RUN_RUNNING = "running"
RUN_FINISHED = "finished"
RUN_STOPPED = "stopped"
# Not a state a record is ever written in: it is what a reader concludes from a
# record that says `running` while the lock behind it can be taken.
RUN_WORKER_GONE = "worker_gone"
TERMINAL_RUN_STATES = frozenset({RUN_FINISHED, RUN_STOPPED})


def runs_directory(config: AgenticHILConfig) -> Path:
    """Where run records live, beside the coordination state they belong to."""
    return derived_state_directory(config.state_root, "coordination", "runs")


def new_run_handle() -> str:
    return f"run-{secrets.token_hex(8)}"


def validated_run_handle(handle: str) -> str:
    if not isinstance(handle, str) or RUN_HANDLE_PATTERN.match(handle) is None:
        raise ConfigError(
            "invalid_argument",
            "A test run handle is the value the start command printed: `run-` followed by sixteen hexadecimal digits.",
            {"field": "run", "value": str(handle)[:64]},
        )
    return handle


def record_path(config: AgenticHILConfig, handle: str) -> Path:
    return runs_directory(config) / f"{validated_run_handle(handle)}.json"


def lock_path(config: AgenticHILConfig, handle: str) -> Path:
    return runs_directory(config) / f"{validated_run_handle(handle)}.lock"


def stop_path(config: AgenticHILConfig, handle: str) -> Path:
    return runs_directory(config) / f"{validated_run_handle(handle)}.stop"


def read_run_record(config: AgenticHILConfig, handle: str) -> JsonObject | None:
    """One run's record, or None when this bench has never heard of it.

    A read can meet the writer publishing the next record, and a publication is
    a moment rather than a state: what a reader sees while one is under way says
    nothing about what the run is doing, only that it is being said again. So
    every face of that moment is waited out rather than reported, and they are
    not all the same face. Windows refuses a rename over a file somebody has
    open, POSIX lets it through and leaves the reader holding an inode the file
    rule no longer recognises, and a writer that unlinked before it renamed
    would show a name that is briefly absent or a file only half of which has
    landed. The patience is bounded and it is patience, not denial: what a
    record still torn on the last look ends in is the refusal it always ended
    in, still naming the reason that look gave, because the decisive cause is
    the whole of what a caller can act on.

    Two answers are not waited on, and for the same reason: they are states
    rather than moments. A file that was not there on the very first look is a
    handle this bench has never heard of, which is what ``run_not_found`` is for
    and what the start command polls on, so it is answered at once and only a
    file that was there and then was not counts as a transition. And a record
    that parses whole while naming a version this code cannot read is another
    version's file, which no amount of looking again will change."""
    path = record_path(config, handle)
    # Whether any look in this call found something under the name. It is what
    # separates "there is no such run" from "the writer is between two steps".
    seen = False
    for attempt in range(_RECORD_READ_ATTEMPTS):
        last_look = attempt == _RECORD_READ_ATTEMPTS - 1
        try:
            text = safe_read_text(path)
        except FileNotFoundError:
            if not seen or last_look:
                return None
            time.sleep(_RECORD_READ_RETRY_DELAY_S)
            continue
        except (ConfigError, OSError, UnicodeDecodeError) as error:
            # Every refusal safe_read_text can raise here is a statement about
            # the file as it is in this instant, and the instant is exactly what
            # the writer's replace changes.
            seen = True
            if last_look:
                raise ConfigError("run_state_invalid", "Test run state could not be read.", {"run": handle, "backend_error": str(error)}) from error
            time.sleep(_RECORD_READ_RETRY_DELAY_S)
            continue
        seen = True
        try:
            value = json.loads(text)
        except ValueError as error:
            if last_look:
                raise ConfigError("run_state_invalid", "Test run state is not valid JSON.", {"run": handle, "backend_error": str(error)}) from error
            time.sleep(_RECORD_READ_RETRY_DELAY_S)
            continue
        if not isinstance(value, dict) or value.get("version") != RUN_RECORD_VERSION:
            raise ConfigError("run_state_invalid", "Test run state is not a record this version can read.", {"run": handle})
        return value
    return None


def worker_is_gone(config: AgenticHILConfig, handle: str) -> bool:
    """Whether the process that was running this handle has ended.

    Asked of the operating system rather than of a pid: the run holds its lock
    for exactly as long as it runs, so a lock this can take is a run whose
    process is no longer there, however it went. Taken and given straight back,
    because the answer is the acquisition and nothing else."""
    lock = _LifetimeLock(lock_path(config, handle))
    try:
        lock.acquire()
    except (BlockingIOError, PermissionError, OSError):
        return False
    lock.release()
    return True


def stop_requested_at(config: AgenticHILConfig, handle: str) -> str | None:
    """When a stop was asked for, or None. The file's existence is the request;
    its content is only there to say who asked and when.

    This reader wants no retry of the record reader's kind, and for a reason
    worth stating rather than leaving to look like an omission. The record has
    many states and a reader that catches it mid-publication must not report the
    moment as one of them; this file has one, and the request is the file being
    there. So every way of failing to read it lands on the same answer the file's
    existence already gives, an in-flight replace included, and waiting would
    only make a stop slower to take effect."""
    try:
        text = safe_read_text(stop_path(config, handle))
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError, ConfigError):
        # A stop request that cannot be read is still a stop request. Refusing
        # to honour it because its own note is unreadable would keep a bench
        # busy over a detail nothing acts on.
        return utc_now_iso()
    try:
        value = json.loads(text)
    except ValueError:
        return utc_now_iso()
    requested = value.get("requested_at") if isinstance(value, dict) else None
    return str(requested) if isinstance(requested, str) else utc_now_iso()


def write_run_record(config: AgenticHILConfig, handle: str, record: JsonObject) -> None:
    """Publish a run's record, retrying while a reader holds the old one.

    The write is atomic, and that is a property of one call rather than a habit
    of this one: :func:`~agentic_hil.config.atomic_write_text` writes a whole new
    file under a temporary name in the record's own directory and renames it over
    the record, so the name never leads to nothing and never leads to half a
    document, and the temporary is in the same directory precisely so the rename
    is a rename rather than a copy across filesystems. Nothing here unlinks the
    record first, which is what would open a window where a reader finds no file
    at all, and the reader's patience is sized for the moment a rename is rather
    than for a window a writer left open.

    What the write is not is uncontended. A rename over a file somebody has open
    is refused on Windows, and the readers here are status polls that arrive
    whenever they arrive, so the write asks again rather than treating another
    process's curiosity as a reason the run cannot say what it is doing."""
    path = record_path(config, handle)
    text = json.dumps(record, indent=2) + "\n"
    for attempt in range(RECORD_WRITE_ATTEMPTS):
        try:
            atomic_write_text(path, text)
            return
        except (OSError, ConfigError):
            if attempt == RECORD_WRITE_ATTEMPTS - 1:
                raise
            time.sleep(RECORD_WRITE_BACKOFF_S)


def prune_run_records(config: AgenticHILConfig) -> None:
    """Drop the oldest finished runs once there are more than a bench needs.

    Only terminal records, and only their own three files. A run still going is
    never a candidate, so this cannot take the record out from under a reader
    asking what a live run is doing."""
    directory = runs_directory(config)
    try:
        records = sorted(directory.glob("run-*.json"), key=lambda item: item.stat().st_mtime, reverse=True)
    except OSError:
        return
    for path in records[RUN_RECORDS_KEPT:]:
        handle = path.stem
        try:
            record = read_run_record(config, handle)
        except ConfigError:
            record = None
        if record is not None and record.get("state") not in TERMINAL_RUN_STATES:
            continue
        for candidate in (path, directory / f"{handle}.stop", directory / f"{handle}.lock", directory / f"{handle}.log"):
            try:
                candidate.unlink()
            except OSError:
                # A file another process still holds, or already gone. Either
                # way there is nothing to do about it here: pruning is
                # housekeeping and must never be the reason a run fails.
                continue


class RunRegistration:
    """The run record, from the process that is running it.

    Holds the lifetime lock for the length of the run, publishes what step it is
    on, answers whether a stop has been asked for, and writes exactly one
    terminal record on the way out however the run ended."""

    def __init__(self, config: AgenticHILConfig, handle: str, *, detached: bool):
        self.config = config
        self.handle = handle
        self.detached = detached
        self.report_path = display_path(config, last_report_path(config))
        self._lock = _LifetimeLock(lock_path(config, handle))
        self._base: JsonObject = {}
        self._terminal = False
        self._last_write = 0.0
        # The newest progress the throttle has not written yet, kept so the step
        # a run is really on is never older than one throttle interval.
        self._pending: JsonObject | None = None
        self._stop_seen: str | None = None
        self._stop_checked = 0.0

    @classmethod
    def take(cls, config: AgenticHILConfig, handle: str, *, name: str, test_config_path: str, detached: bool) -> RunRegistration:
        """Register this process as the one running `handle`.

        The lock is taken before the record is written, so two processes handed
        the same handle cannot both claim to be running it."""
        registration = cls(config, handle, detached=detached)
        try:
            registration._lock.acquire()
        except (BlockingIOError, PermissionError, OSError) as error:
            raise ConfigError(
                "run_already_active",
                "Another process is already running this test run handle.",
                {"run": handle},
            ) from error
        registration._base = {
            "version": RUN_RECORD_VERSION,
            "run": handle,
            "name": name,
            "test_config_path": display_path(config, test_config_path),
            "report_path": registration.report_path,
            "detached": detached,
            "pid": os.getpid(),
            "started_at": utc_now_iso(),
        }
        registration._write(RUN_STARTING, {})
        prune_run_records(config)
        return registration

    def __enter__(self) -> RunRegistration:
        return self

    def __exit__(self, kind: type[BaseException] | None, error: BaseException | None, traceback: TracebackType | None) -> bool:
        if not self._terminal:
            # A run that left by an exception still has to leave a record
            # saying so: a handle stuck at `running` with no process behind it
            # would send a reader to the dead-owner route for a run that in
            # fact ended in this process and cleaned up after itself.
            self._write(
                RUN_FINISHED,
                {
                    "run_ok": False,
                    "error_type": "reactor_exception" if error is not None else "unknown",
                    "exception_type": None if error is None else type(error).__name__,
                    "finished_at": utc_now_iso(),
                },
                force=True,
            )
            self._terminal = True
        self._lock.release()
        return False

    def running(self) -> None:
        """The run holds its devices and its first step is next."""
        self._write(RUN_RUNNING, {}, force=True)

    def progress(self, progress: JsonObject) -> None:
        """Say which step the run is on, as often as the interval allows.

        Held back rather than dropped, which is the whole of the difference. The
        step this is about to name may be the one that then waits ten minutes,
        so a throttle that discarded it would leave the record naming the step
        before it for as long as that step lasts, which is exactly the moment
        somebody watching a long run is watching. What the throttle skips is
        kept and written by the next thing that gets the chance, and
        `stop_requested` is that thing: the run asks it between every step and
        once per slice of every wait."""
        self._pending = progress
        self._flush_pending()

    def _flush_pending(self) -> None:
        if self._pending is not None and self._write(RUN_RUNNING, {"progress": self._pending}):
            self._pending = None

    def finish(self, result: JsonObject) -> None:
        """The one terminal record, written from the run's own result."""
        self._pending = None
        stopped = bool(result.get("stopped"))
        self._write(
            RUN_STOPPED if stopped else RUN_FINISHED,
            {
                "run_ok": bool(result.get("ok")),
                "error_type": result.get("error_type"),
                "failed_step": result.get("failed_step"),
                "stopped_after_step": result.get("stopped_after_step"),
                "report_path": result.get("report_path", self.report_path),
                "finished_at": utc_now_iso(),
            },
            force=True,
        )
        self._terminal = True

    def stop_requested(self) -> bool:
        """Whether somebody has asked this run to stop.

        Cached once true, and asked of the filesystem at most every
        `STOP_POLL_INTERVAL_S`: this is called between every step, and a plan of
        very short steps must not spend its time asking.

        It is also where a progress write the throttle held back gets written.
        The two belong together because they are asked in the same places for
        the same reason: this is what the run does while it is otherwise busy."""
        self._flush_pending()
        if self._stop_seen is not None:
            return True
        now = time.monotonic()
        if now - self._stop_checked < STOP_POLL_INTERVAL_S:
            return False
        self._stop_checked = now
        self._stop_seen = stop_requested_at(self.config, self.handle)
        return self._stop_seen is not None

    def _write(self, state: str, fields: JsonObject, *, force: bool = False) -> bool:
        """Publish the record, or say the throttle held this one back."""
        now = time.monotonic()
        if not force and now - self._last_write < PROGRESS_WRITE_INTERVAL_S:
            return False
        self._last_write = now
        record = {**self._base, **fields, "state": state, "updated_at": utc_now_iso()}
        try:
            write_run_record(self.config, self.handle, record)
        except (ConfigError, OSError, ValueError):
            # A record that could not be written is a run nobody can watch, not
            # a run that should stop. The plan is what the bench is for. Reported
            # as written all the same: asking again forever would turn a
            # read-only state directory into the run's main occupation.
            return True
        return True


def worker_publish_window_s(wait_s: float) -> float:
    """How long the start command waits for a detached worker to publish.

    The worker stays in ``starting`` not only while it comes up but for as long
    as it is allowed to wait for a device another run holds: ``begin_run(wait_s=
    ...)`` sits inside that window. A deadline that counted only the startup
    would call a worker still legitimately waiting ``run_worker_unresponsive``
    and walk away from a live process that then acquires the devices and runs the
    whole plan behind a caller told the start had failed. So the base startup
    window is extended by the same bounded wait the worker was handed, clamped to
    the bench's own maximum; a nonsensical value falls back to the base window
    rather than raising, since the worker refuses it the same way and publishes a
    terminal record at once."""
    device_wait = float(wait_s) if isinstance(wait_s, (int, float)) and not isinstance(wait_s, bool) else 0.0
    return WORKER_PUBLISH_TIMEOUT_S + max(0.0, min(device_wait, MAX_WAIT_S))


def start_detached_run(config: AgenticHILConfig, test_config_path: str, *, wait_s: float) -> JsonObject:
    """Start a run in its own process and answer as soon as it has published.

    The wait is for the worker to say what it is doing, not for it to finish:
    it publishes as soon as it holds its devices, or as soon as it knows it
    cannot have them. A handle printed before that would be a handle that might
    name nothing, and the caller's next move is to ask about it.

    Two things the answer is careful about. The window it waits in accounts for
    the device wait the worker was handed, so a legitimate wait for a held bench
    is not mistaken for a stuck worker. And a worker that reached a terminal
    state before it could be caught at ``running`` — a refusal published at once,
    or a plan short enough to finish inside the window — is reported with its own
    verdict, not as a successful launch: launch success is reserved for a running
    worker and for an already-finished run only when it passed.

    The wait is validated here, before a worker is spawned. The worker refuses a
    bad wait too — it runs the same validator when it acquires its devices — but
    a non-finite wait handed to a detached process would strand it: a NaN deadline
    is one ``time.monotonic()`` never reaches, so a worker holding for a device
    polls forever, past the window this command waits in, and the caller is told
    the start failed while a live process keeps waiting. Refusing the value up
    front keeps an invalid wait out of a process nobody is watching, and the one
    validated finite value is what both the worker and the publication window are
    built from."""
    try:
        wait_s = validated_wait(wait_s)
    except ConfigError as error:
        return {"ok": False, "tool": "test_reactor_start", "side_effect_committed": False, "side_effect_status": "not_started", "hardware_state": "unchanged", "retry_safe": False, **error.to_dict()}
    handle = new_run_handle()
    report = display_path(config, last_report_path(config))
    worker = spawn_run_worker(config, handle, test_config_path, wait_s=wait_s)
    deadline = time.monotonic() + worker_publish_window_s(wait_s)
    exited_at: float | None = None
    while True:
        record = None
        try:
            record = read_run_record(config, handle)
        except ConfigError:
            record = None
        if record is not None and record.get("state") != RUN_STARTING:
            break
        if worker.poll() is not None and exited_at is None:
            exited_at = time.monotonic()
        if record is None and exited_at is not None and time.monotonic() - exited_at > WORKER_EXIT_GRACE_S:
            return {
                "ok": False,
                "tool": "test_reactor_start",
                "error_type": "run_worker_failed",
                "summary": "The detached run's worker process ended before it could say what it was doing.",
                "run": handle,
                "exit_code": worker.poll(),
                "worker_output": worker_output(config, handle),
                "retry_safe": True,
                "side_effect_committed": False,
            }
        if time.monotonic() >= deadline:
            # The worker outlasted even the wait it was granted, so it is stuck
            # rather than patiently holding for a device. It is not abandoned: a
            # stop is planted under its handle first, so if it does come alive
            # and reach the board it ends at the first step boundary instead of
            # running the whole plan behind a caller told the start had failed.
            _plant_stop_after_unresponsive(config, handle)
            return {
                "ok": False,
                "tool": "test_reactor_start",
                "error_type": "run_worker_unresponsive",
                "summary": "The detached run's worker process did not say what it was doing within the startup window; a cooperative stop was left under its handle in case it is still alive.",
                "run": handle,
                "worker_output": worker_output(config, handle),
                "retry_safe": False,
                "side_effect_committed": False,
            }
        time.sleep(WORKER_PUBLISH_POLL_S)
    state = str(record.get("state"))
    if state in TERMINAL_RUN_STATES:
        return _detached_terminal_result(handle, record, report)
    return {
        "ok": True,
        "tool": "test_reactor_start",
        "run": handle,
        "state": state,
        "detached": True,
        "name": record.get("name"),
        "test_config_path": record.get("test_config_path"),
        "report_path": record.get("report_path", report),
        "started_at": record.get("started_at"),
        "summary": (
            f"The run is detached under handle {handle}; ask `agentic-hil test-reactor-status --run {handle}` what it is doing "
            f"and `agentic-hil test-reactor-stop --run {handle}` to end it early."
        ),
    }


def _detached_terminal_result(handle: str, record: JsonObject, report: str) -> JsonObject:
    """The start command's answer for a run that already ended.

    The worker published a terminal record before it could be caught at
    ``running``, so there is nothing to launch — there is a verdict, and the
    caller gets it. ``ok`` is the run's own ``run_ok`` so a refused start (a held
    bench answers ``device_busy`` at once) exits non-zero, and the failure's
    ``error_type`` and failed-step fields travel with it instead of being dropped
    behind a handle to a run that is already over."""
    run_ok = record.get("run_ok") is True
    stopped = str(record.get("state")) == RUN_STOPPED
    verdict = "passed" if run_ok else f"did not pass ({record.get('error_type') or 'unknown'})"
    ended = "was stopped on request" if stopped else "ended"
    result: JsonObject = {
        "ok": run_ok,
        "tool": "test_reactor_start",
        "run": handle,
        "state": str(record.get("state")),
        "detached": True,
        "run_ok": run_ok,
        "name": record.get("name"),
        "test_config_path": record.get("test_config_path"),
        "report_path": record.get("report_path", report),
        "started_at": record.get("started_at"),
        "finished_at": record.get("finished_at"),
        "summary": (
            f"The detached run under handle {handle} {ended} before the start command returned and {verdict}; "
            f"its report is at {record.get('report_path', report)}."
        ),
    }
    if not run_ok:
        result["error_type"] = record.get("error_type") or "unknown"
        for field in ("failed_step", "stopped_after_step"):
            if record.get(field) is not None:
                result[field] = record.get(field)
    return result


def _plant_stop_after_unresponsive(config: AgenticHILConfig, handle: str) -> None:
    """Best-effort cooperative stop for a worker that never published in time.

    Written directly rather than through ``request_run_stop`` so it lands even
    when no record has appeared yet, and guarded so a state directory that cannot
    be written does not turn a timeout into a raise. The run reads this between
    its steps and inside a wait, so a worker that later comes alive ends early."""
    try:
        atomic_write_text(
            stop_path(config, handle),
            json.dumps({"run": handle, "requested_at": utc_now_iso(), "requested_by_pid": os.getpid()}, indent=2) + "\n",
        )
    except (ConfigError, OSError, ValueError):
        return


def spawn_run_worker(config: AgenticHILConfig, handle: str, test_config_path: str, *, wait_s: float) -> subprocess.Popen:
    """Start the worker that runs the plan under this handle.

    :func:`~agentic_hil.process.spawn_detached_process` rather than
    ``spawn_managed_process``, and for the reason that name exists: a managed
    child joins a kill-on-close Job Object owned by the run that started it, and
    a detached test run whose whole point is to outlive the command that started
    it would be killed the moment that command returned. Its lifetime is bounded
    by its own rules instead: the plan's bounds, and the stop request.

    Its output goes to a file beside its record rather than to a pipe: nobody is
    left reading a detached run's stderr once the command that started it has
    returned, and a full pipe would block the run mid-plan. The report is what
    a caller reads; this is for the case where there is no report because the
    worker could not get far enough to write one."""
    with open(runs_directory(config) / f"{validated_run_handle(handle)}.log", "ab") as handle_log:
        return spawn_detached_process(
            [
                sys.executable,
                "-m",
                "agentic_hil",
                "test-reactor",
                "--test-config",
                test_config_path,
                "--wait-s",
                str(wait_s),
                "--run-handle",
                handle,
            ],
            stdin=subprocess.DEVNULL,
            stdout=handle_log,
            stderr=handle_log,
            cwd=str(config.work_dir),
        )


def worker_output(config: AgenticHILConfig, handle: str, limit: int = 4000) -> str:
    """The tail of what a worker printed.

    Only ever read for a worker that failed to say what it was doing: with no
    record and no report, its own output is the only account of what happened,
    and a start command that answered "it did not come up" without it would send
    an operator to a file they do not know exists."""
    try:
        text = (runs_directory(config) / f"{validated_run_handle(handle)}.log").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return text[-limit:]


def run_status(config: AgenticHILConfig, handle: str | None = None) -> JsonObject:
    """What a handle is doing, or what handles this bench knows."""
    if handle is None:
        return known_runs(config)
    record = read_run_record(config, validated_run_handle(handle))
    if record is None:
        return {
            "ok": False,
            "tool": "test_reactor_status",
            "error_type": "run_not_found",
            "summary": "This bench has no record of a test run under that handle.",
            "run": handle,
            "retry_safe": False,
            "side_effect_committed": False,
        }
    state = str(record.get("state"))
    requested_at = stop_requested_at(config, handle)
    if state not in TERMINAL_RUN_STATES and worker_is_gone(config, handle):
        # The gone answer and the record were read in two moments, and a worker
        # that finished between them published its terminal record BEFORE it
        # released the lock the probe just took. So a takeable lock does not
        # decide anything by itself: the record is re-read on the far side of
        # it, and a terminal record found there is the truth of a worker that
        # published and left. Only a still-running record behind a takeable
        # lock is a worker that died with no orderly end.
        record = read_run_record(config, handle) or record
        state = str(record.get("state"))
        if state not in TERMINAL_RUN_STATES:
            return {
                **public_run_fields(record),
                "ok": True,
                "tool": "test_reactor_status",
                "state": RUN_WORKER_GONE,
                "stop_requested_at": requested_at,
                "summary": "The process that was running this plan is gone, so this run has no orderly end and no report of its own.",
                "next_step": (
                    "The bench is the dead-owner case the coordinator already handles: `agentic-hil lease-status` reads and heals it, "
                    "and names a quarantine id for `agentic-hil recover --confirm-safe-state --quarantine-id <id>` if the run had reached the board."
                ),
            }
    return {
        **public_run_fields(record),
        "ok": True,
        "tool": "test_reactor_status",
        "state": state,
        "stop_requested_at": requested_at,
        "summary": run_status_summary(state, record, requested_at),
    }


def run_status_summary(state: str, record: JsonObject, requested_at: str | None) -> str:
    if state in TERMINAL_RUN_STATES:
        verdict = "passed" if record.get("run_ok") else f"did not pass ({record.get('error_type') or 'unknown'})"
        ended = "was stopped on request" if state == RUN_STOPPED else "ended"
        return f"This run {ended} and {verdict}; its report is at {record.get('report_path')}."
    progress = record.get("progress") or {}
    where = f"step {progress.get('step')} ({progress.get('action')})" if progress.get("step") else "its first step"
    iteration = f", iteration {progress['iteration']}" if progress.get("iteration") else ""
    pending = " A stop has been requested; it ends after the step it is in." if requested_at else ""
    return f"This run is on {where}{iteration}.{pending}"


def public_run_fields(record: JsonObject) -> JsonObject:
    """The record, less the fields that name this host rather than this run."""
    return {key: value for key, value in record.items() if key not in {"version", "pid"}}


def known_runs(config: AgenticHILConfig) -> JsonObject:
    """Every run this bench still has a record of, newest first."""
    try:
        paths = sorted(runs_directory(config).glob("run-*.json"), key=lambda item: item.stat().st_mtime, reverse=True)
    except OSError:
        paths = []
    runs = []
    for path in paths:
        try:
            record = read_run_record(config, path.stem)
        except ConfigError:
            continue
        if record is None:
            continue
        state = str(record.get("state"))
        if state not in TERMINAL_RUN_STATES and worker_is_gone(config, path.stem):
            state = RUN_WORKER_GONE
        runs.append({"run": record.get("run"), "state": state, "name": record.get("name"), "started_at": record.get("started_at"), "updated_at": record.get("updated_at")})
    return {
        "ok": True,
        "tool": "test_reactor_status",
        "runs": runs,
        "active_runs": [item["run"] for item in runs if item["state"] not in TERMINAL_RUN_STATES and item["state"] != RUN_WORKER_GONE],
        "summary": f"This bench has records of {len(runs)} test run(s); name one with --run to see what it is doing.",
    }


def request_run_stop(config: AgenticHILConfig, handle: str) -> JsonObject:
    """Ask a run to end after the step it is in.

    Cooperative and nothing else: this writes a file, and the run reads it
    between its steps and inside a wait. Nothing here reaches the process, so a
    run cannot be left half way through a step by whoever asked it to stop."""
    validated_run_handle(handle)
    record = read_run_record(config, handle)
    if record is None:
        return {
            "ok": False,
            "tool": "test_reactor_stop",
            "error_type": "run_not_found",
            "summary": "This bench has no record of a test run under that handle.",
            "run": handle,
            "retry_safe": False,
            "side_effect_committed": False,
        }
    state = str(record.get("state"))
    if state in TERMINAL_RUN_STATES:
        return {
            **public_run_fields(record),
            "ok": True,
            "tool": "test_reactor_stop",
            "state": state,
            "stop_requested": False,
            "summary": "This run had already ended, so nothing was asked of it.",
        }
    if worker_is_gone(config, handle):
        return {
            **public_run_fields(record),
            "ok": False,
            "tool": "test_reactor_stop",
            "error_type": "run_worker_gone",
            "state": RUN_WORKER_GONE,
            "summary": "The process that was running this plan is gone, so there is nobody left to honour a cooperative stop.",
            "next_step": (
                "The bench is the dead-owner case the coordinator already handles: `agentic-hil lease-status` reads and heals it, "
                "and names a quarantine id for `agentic-hil recover --confirm-safe-state --quarantine-id <id>` if the run had reached the board."
            ),
            "retry_safe": False,
            "side_effect_committed": False,
        }
    requested_at = utc_now_iso()
    atomic_write_text(stop_path(config, handle), json.dumps({"run": handle, "requested_at": requested_at, "requested_by_pid": os.getpid()}, indent=2) + "\n")
    return {
        **public_run_fields(record),
        "ok": True,
        "tool": "test_reactor_stop",
        "state": state,
        "stop_requested": True,
        "stop_requested_at": requested_at,
        "summary": "A stop was requested; the run finishes the step it is in, closes its devices in the usual order and writes its report.",
    }
