"""The bench server's job service: the HTTP API's jobs on the fair queue (#891).

`open_bench_job_service` is what `httpapi.open_job_service` answers with: one
`Scheduler` over the configured `job_root`, behind the `httpapi.JobService`
Protocol. `serverjobs` fills the scheduler's two seams. Every job runs in a
fresh workspace of its own, `<job_root>/<job_id>/workspace`, placed by
`serverjobs.prepare_job` from the bytes that were submitted, under the job
configuration `serverjobs` derives from the bench's. Every attempt is a
`python -m agentic_hil.serverjobs` worker under the handle the scheduler
allocated, which writes the submitter the client named into the report.

One condition guards the scheduler, its store and the job files. Under it run
the scheduler's own work and local file reads and writes, nothing else. A
worker's spawn and kill, a recover's tool call, a plan check, an evidence
bundle and every report, JUnit and log read run outside it, so nothing that
waits on a device, a process or a network holds up a status read or an upload.

A job's token is answered once, with its submission, and only its SHA-256 is
kept. A job's public status is this module's own reading of its record: its
devices by their logical names, where it waits and why, and a sentence of this
module's own per state, never what a device printed.
"""

from __future__ import annotations

import copy
import hashlib
import hmac
import io
import json
import math
import os
import secrets
import shutil
import subprocess
import sys
import threading
import time
import zipfile
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager, suppress
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from agentic_hil import runlifecycle
from agentic_hil.benchview import configured_device_keys
from agentic_hil.config import ConfigError, atomic_write_bytes, configured_work_path, safe_read_bytes, safe_read_text
from agentic_hil.httpapi import (
    ByteRange,
    DeviceActivity,
    JobContent,
    JobEvent,
    JobEvents,
    JobService,
    JobServiceError,
    JobStatus,
    LogRead,
    QueueSnapshot,
    ServerSettings,
)
from agentic_hil.jobstore import (
    ACTIVE_JOB_STATES,
    AWAITING_ARTIFACTS,
    EXPIRED,
    FAILED_TO_START,
    FINISHED,
    JOB_ID_PATTERN,
    QUEUED,
    REJECTED,
    RUNNING,
    RUNTIME_EXCEEDED,
    STARTING,
    STOPPED,
    STOPPING,
    TERMINAL_JOB_STATES,
    WITHDRAWING,
    WITHDRAWN,
    WORKER_GONE,
    Job,
    SchedulerSettings,
    invalid_input,
)
from agentic_hil.junit import junit_xml_document
from agentic_hil.process import spawn_detached_process, terminate_process_tree
from agentic_hil.report import canonical_run_report_path
from agentic_hil.runevidence import write_run_evidence
from agentic_hil.scheduler import QUOTA_JOB_STATES, WORKER_TERMINATE_TIMEOUT_S, RunWorkerSource, Scheduler, WorkerSource
from agentic_hil.serverjobs import _job_config, prepare_job
from agentic_hil.test_reactor import DEFAULT_TEST_CONFIG_PATH, TestStep, load_test_config
from agentic_hil.types import AgenticHILConfig, JsonObject

# How often the tick loop looks at the queue while a job waits or runs. It
# bounds how soon a worker's end is seen; it decides no outcome.
RECONCILE_INTERVAL_S = 0.5
# Where a job's plan is placed in its workspace, and the name it is stored under.
PLAN_PATH = DEFAULT_TEST_CONFIG_PATH
PLAN_NAME = PurePosixPath(DEFAULT_TEST_CONFIG_PATH).name
# Who a job is submitted under when the client names nobody.
ANONYMOUS = "anonymous"
# How many of a device's newest jobs its activity names.
RECENT_JOBS = 5
# The throwaway queue a plan check is admitted on, under the job root.
CHECK_PREFIX = "check-"
# What a job's evidence bundle is built in and cached as, inside its report directory.
EVIDENCE_DIRECTORY = "evidence"
REFUSAL_REPORT_NAME = "refusal-report.json"
# The fields of a job's record its status and its list entry carry as the record holds them.
_CARRIED_FIELDS = frozenset({"error_type"})
# What this module says of a job in each state. Never a worker's or a device's words.
_SUMMARIES: Mapping[str, str] = {
    AWAITING_ARTIFACTS: "The job waits for the artifacts its plan names; it enters the queue once every one has been uploaded.",
    QUEUED: "The job waits in the queue for the devices its plan names.",
    STARTING: "The bench is starting the job's worker.",
    RUNNING: "The job's plan is running on the bench.",
    WITHDRAWING: "The job was withdrawn while it ran; its worker stops at its next step boundary.",
    STOPPING: "The job's worker was asked to stop and stops at its next step boundary.",
    FINISHED: "The job's plan ran to its end; its report says how it went.",
    STOPPED: "The job's worker stopped before the plan's end; its report says after which step.",
    WITHDRAWN: "The submitter withdrew the job.",
    EXPIRED: "The job waited until its queue deadline without starting.",
    REJECTED: "The plan was refused when the job was admitted.",
    RUNTIME_EXCEEDED: "The job's attempt ran past max_job_runtime_s and was stopped.",
    WORKER_GONE: "The job's worker is gone without an orderly end, so the run has no verdict of its own.",
    FAILED_TO_START: "The bench could not start a worker for the job, so nothing ran.",
}
_ARTIFACTS_EXPIRED = "The artifacts the plan names did not all arrive within max_artifact_wait_s, so the job never entered the queue."


# -- The scheduler's seams, filled by serverjobs --------------------------------


class JobWorkspacePreparer:
    """One fresh workspace per job under `<job_root>/<job_id>/workspace`, placed by `serverjobs.prepare_job`.

    The job store makes the workspace directory with the rest of the job's, and
    the first preparation fills it with the stored plan and the staged
    artifacts. Every later one checks that each placed file still holds the
    bytes the job was given, so a job never runs something nobody submitted.
    """

    def __init__(self, bench: AgenticHILConfig) -> None:
        if bench.server is None:
            raise invalid_input("A job workspace is derived from a bench with a server block, and this bench has none.", {"field": "server"})
        self.bench = bench

    def config_for(self, job: Job) -> AgenticHILConfig:
        return _job_config(self.bench, job.job_id)

    def prepare(self, job: Job) -> tuple[AgenticHILConfig, Path]:
        onto_plan = next((relative for relative in sorted(job.artifacts) if Path(relative) == Path(PLAN_PATH)), None)
        if onto_plan is not None:
            raise invalid_input("An artifact cannot be placed where the job's plan is placed.", {"job_id": job.job_id, "artifact": onto_plan})
        try:
            files = {PLAN_PATH: safe_read_bytes(job.plan_path)}
            files.update({relative: safe_read_bytes(staged) for relative, staged in sorted(job.artifacts.items())})
            config = self.config_for(job)
            if _fresh(Path(config.work_dir)):
                artifacts = {relative: data for relative, data in files.items() if relative != PLAN_PATH}
                config = prepare_job(self.bench, job_id=job.job_id, submitted_by="", plan_name=PLAN_PATH, plan_bytes=files[PLAN_PATH], artifacts=artifacts)
                changed = None
            else:
                changed = _changed(config, files)
        except (ConfigError, OSError) as error:
            raise invalid_input("The job's workspace could not be placed from the bytes it was given.", {"job_id": job.job_id, "backend_error": str(error)}) from error
        if changed is not None:
            raise invalid_input("A file in the job's workspace does not hold the bytes the job was given.", {"job_id": job.job_id, "artifact": changed})
        return config, configured_work_path(config, PLAN_PATH)


def _fresh(workspace: Path) -> bool:
    """Not there yet, or the empty directory the job store made for it."""
    return not os.path.lexists(workspace) or (workspace.is_dir() and not workspace.is_symlink() and not any(workspace.iterdir()))


def _changed(config: AgenticHILConfig, files: Mapping[str, bytes]) -> str | None:
    """The first placed file that is missing or holds other bytes than `files` says, or None."""
    for relative, data in files.items():
        try:
            placed = safe_read_bytes(configured_work_path(config, relative), workspace=config.work_dir)
        except (ConfigError, OSError):
            return relative
        if placed != data:
            return relative
    return None


@dataclass(frozen=True)
class _Spawn:
    config: AgenticHILConfig
    handle: str
    job_id: str
    submitted_by: str
    plan: str


class JobWorkerSource(RunWorkerSource):
    """A `serverjobs` worker per attempt, under the handle the scheduler allocated.

    `start` and `terminate` only record what was asked, because the scheduler
    asks inside its tick, under the service's lock; `perform_pending` spawns
    and ends the processes once the tick has let go of it. A handle this source
    did not start is still refused at once, as `RunWorkerSource` refuses it.
    """

    def __init__(self, bench: AgenticHILConfig, *, submitter_of: Callable[[str], str]) -> None:
        super().__init__()
        self.bench = bench
        self.submitter_of = submitter_of
        self._pending_lock = threading.Lock()
        self._spawns: list[_Spawn] = []
        self._kills: list[str] = []

    def start(self, config: AgenticHILConfig, handle: str, plan: Path, *, wait_s: float) -> None:
        workspace = Path(config.work_dir)
        job_id = workspace.parent.name
        relative = Path(plan).relative_to(workspace).as_posix()
        spawn = _Spawn(config, handle, job_id, self.submitter_of(job_id), relative)
        with self._pending_lock:
            self._spawns.append(spawn)

    def terminate(self, config: AgenticHILConfig, handle: str) -> None:
        with self._pending_lock:
            if handle not in self._children and not any(spawn.handle == handle for spawn in self._spawns):
                raise RuntimeError("this scheduler did not start that worker, so it has no process of its own to end; the job keeps its devices until the worker is gone")
            self._kills.append(handle)

    def perform_pending(self) -> list[tuple[str, str]]:
        """Spawn and end what the last tick asked for. Answers each spawn that failed, with why."""
        with self._pending_lock:
            spawns, self._spawns = self._spawns, []
            kills, self._kills = self._kills, []
        failures: list[tuple[str, str]] = []
        for spawn in spawns:
            try:
                self._children[spawn.handle] = self._spawn(spawn)
            except Exception as error:
                failures.append((spawn.handle, str(error)))
        for handle in kills:
            child = self._children.get(handle)
            if child is None or child.poll() is not None:
                continue
            try:
                terminate_process_tree(child, WORKER_TERMINATE_TIMEOUT_S, graceful=False)
            except Exception as error:
                print(f"agentic-hil serve: the worker of {handle} could not be ended: {error}", file=sys.stderr)
        return failures

    def _spawn(self, spawn: _Spawn) -> subprocess.Popen:
        with open(runlifecycle.worker_log_path(spawn.config, spawn.handle), "ab") as log:
            return spawn_detached_process(
                [
                    sys.executable, "-m", "agentic_hil.serverjobs",
                    "--bench-config", str(self.bench.config_path),
                    "--bench-workspace", str(self.bench.work_dir),
                    "--job-id", spawn.job_id,
                    f"--submitted-by={spawn.submitted_by}",
                    "--plan", spawn.plan,
                    "--run-handle", spawn.handle,
                ],
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                cwd=str(spawn.config.work_dir),
            )


# -- Refusals, one function per family -------------------------------------------


def _not_found(job_id: object) -> JobServiceError:
    return JobServiceError("job_not_found", "This bench has no job under that id.", {"job_id": str(job_id)[:64]})


def _token_refused(job_id: str) -> JobServiceError:
    return JobServiceError("job_token_invalid", "The bearer token is not the token this job was submitted with.", {"job_id": job_id})


def _ended_refusal(job_id: str, state: str) -> JobServiceError:
    details: JsonObject = {"job_id": job_id, "state": state}
    if state == WITHDRAWN:
        return JobServiceError("job_withdrawn", "The job has already been withdrawn.", details)
    if state == EXPIRED:
        return JobServiceError("job_expired", "The job has expired and takes nothing more.", details)
    if state == RUNTIME_EXCEEDED:
        return JobServiceError("plan_runtime_exceeded", "The job ran past max_job_runtime_s and has ended.", details)
    return JobServiceError("job_finished", "The job has already ended.", details)


def _not_ended(job_id: str, state: str) -> JobServiceError:
    return JobServiceError("job_not_ended", "The job has not ended yet, so it has no report.", {"job_id": job_id, "state": state})


def _admission_refusal(data: Mapping[str, Any], retry_after_s: int, *, over_quota: bool = False) -> JobServiceError:
    """The scheduler's refusal of a submitted plan, under the scheduler's own type."""
    summary = str(data.get("summary") or "The plan was refused when the job was admitted.")
    if over_quota or data.get("error_type") == "max_queued_jobs_per_user":
        return JobServiceError(
            "max_queued_jobs_per_user",
            summary if not over_quota else "This submitter already has as many jobs waiting or running as max_queued_jobs_per_user allows.",
            {"retry_after_s": retry_after_s},
        )
    error_type = data["error_type"]
    validation: JsonObject = {"error_type": error_type, "summary": summary, "field": data.get("refusal_field")}
    return JobServiceError(error_type, summary, {"validation_error": validation})


def _artifact_refusal(job_id: str, path: str, *, uploaded: bool) -> JobServiceError:
    details: JsonObject = {"job_id": job_id, "path": path}
    if uploaded:
        return JobServiceError("artifact_already_uploaded", "An artifact has already been accepted under this path, or the job takes no more.", details)
    return JobServiceError("artifact_unreferenced", "The job's plan names no artifact under this path.", details)


def _range_refused(total_size: int) -> JobServiceError:
    return JobServiceError("range_not_satisfiable", "The requested byte range lies outside the log.", {"total_size": total_size})


def _device_refused(name: str, devices: Sequence[str]) -> JobServiceError:
    return JobServiceError("device_not_found", f"This bench declares no device named {name!r}.", {"device": name, "devices": list(devices)})


def _filter_refused(value: str) -> JobServiceError:
    return JobServiceError(
        "invalid_argument",
        "`since` is an ISO-8601 time, such as 2026-01-01T00:00:00Z.",
        {"filter": "since", "value": value[:64]},
    )


# -- The service -----------------------------------------------------------------


@dataclass(frozen=True)
class _Queue:
    """What every job's place in the queue is read from, taken once per publication."""

    now: float
    occupants: Mapping[str, str]
    order: Sequence[str]
    claimed: Mapping[str, str]
    drained: frozenset[str]
    incidents: frozenset[str]
    holders: frozenset[str]


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _order(data: Mapping[str, Any]) -> tuple[float, float]:
    sequence = data.get("sequence")
    return (math.inf if sequence is None else float(sequence), float(data.get("submitted_at") or 0.0))


def _carried(data: Mapping[str, Any]) -> JsonObject:
    return {key: copy.deepcopy(data.get(key)) for key in _CARRIED_FIELDS}


def _summary(data: Mapping[str, Any]) -> str:
    state = str(data["state"])
    if state == EXPIRED and data.get("error_type") == "artifact_missing":
        return _ARTIFACTS_EXPIRED
    return _SUMMARIES.get(state, "The job is in a state this server does not describe.")


def _verdict(data: Mapping[str, Any]) -> bool | None:
    """None until the job ends; then whether its plan ran to a passing end."""
    if data["state"] not in TERMINAL_JOB_STATES:
        return None
    return data["state"] == FINISHED and data.get("run_ok") is True


def _read_document(path: Path | None) -> JsonObject | None:
    if path is None:
        return None
    try:
        value = json.loads(safe_read_text(path))
    except (ConfigError, OSError, UnicodeDecodeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _plan_steps(plan_path: Path) -> tuple[TestStep, ...]:
    try:
        return tuple(load_test_config(str(plan_path), str(plan_path.parent)).steps)
    except ConfigError:
        return ()


def _cursor(job_id: str, last_event_id: str | None) -> int:
    prefix = f"{job_id}:"
    if isinstance(last_event_id, str) and last_event_id.startswith(prefix) and last_event_id[len(prefix):].isdigit():
        return int(last_event_id[len(prefix):])
    return 0


def _since(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        moment = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as error:
        raise _filter_refused(value) from error
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.timestamp()


def _label_value(value: str) -> str:
    """A Prometheus label value: backslash, quote and newline escaped."""
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _hardware_recover(config: AgenticHILConfig, *, operator_statement: str, accept_config_change: bool) -> JsonObject:
    """`hardware_recover` on a tool service of this bench, closed again once it answered."""
    # Imported here: the tool service imports what imports this module.
    from agentic_hil.tools import AgenticHILToolService

    service = AgenticHILToolService(config, frontend="bench-server")
    try:
        return service.call("hardware_recover", {"operator_statement": operator_statement, "accept_config_change": accept_config_change})
    finally:
        service.close()


class BenchJobService:
    """Implements httpapi.JobService over one `Scheduler` and its job store."""

    def __init__(
        self,
        config: AgenticHILConfig,
        settings: ServerSettings,
        scheduler_settings: SchedulerSettings,
        *,
        worker_source: WorkerSource | None,
        recover_call: Callable[..., JsonObject] | None,
        reconcile_interval_s: float,
    ) -> None:
        self.config = config
        self.settings = settings
        self.preparer = JobWorkspacePreparer(config)
        self.workers: WorkerSource = worker_source if worker_source is not None else JobWorkerSource(config, submitter_of=self._submitter_of)
        self.scheduler = Scheduler(config, scheduler_settings, worker_source=self.workers, workspace_preparer=self.preparer)
        self.store = self.scheduler.store
        self._recover = recover_call or _hardware_recover
        self._interval = reconcile_interval_s
        self._condition = threading.Condition()
        self._wake = threading.Event()
        self._stopping = threading.Event()
        self._thread: threading.Thread | None = None
        self._statuses: dict[str, JsonObject] = {}
        # The first answer to each Idempotency-Key, kept for as long as this server runs.
        self._replays: dict[str, JsonObject] = {}
        self._evidence_locks: dict[str, threading.Lock] = {}
        self._keys = configured_device_keys(config)
        self._names: dict[str, list[str]] = {}
        for name, keys in self._keys.items():
            for key in keys:
                self._names.setdefault(key, []).append(name)
        # A plan check that ended with the server left its throwaway queue behind.
        for leftover in self.store.root.glob(CHECK_PREFIX + "*"):
            if leftover.is_dir():
                shutil.rmtree(leftover, ignore_errors=True)

    # -- Lifetime ----------------------------------------------------------

    def start(self) -> None:
        with self._condition:
            self._publish()
        self._thread = threading.Thread(target=self._run, name="bench-job-ticks", daemon=True)
        self._thread.start()

    def close(self) -> None:
        """Stop ticking and keep every record. A running worker is left to run its plan to its end."""
        self._stopping.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join()
        with self._condition:
            self.store.save()
            self._condition.notify_all()

    def _run(self) -> None:
        reported = False
        while not self._stopping.is_set():
            self._wake.clear()
            # Asked again after the clear: a close that woke the loop just before it is not lost.
            if self._stopping.is_set():
                break
            failed = False
            with self._condition:
                try:
                    self.scheduler.tick()
                except Exception as error:
                    failed = True
                    if not reported:
                        reported = True
                        print(f"agentic-hil serve: the job queue's tick raised and is retried: {error!r}", file=sys.stderr)
                self._publish()
                timeout = self._interval if failed else self._wait_s()
            self._perform_pending()
            self._wake.wait(timeout)

    def _wait_s(self) -> float | None:
        waits: list[float] = []
        due = self.scheduler.next_due_at()
        if due is not None:
            remaining = due - float(self.scheduler.clock())
            # A moment already past is one the tick could not settle: look again later, not at once.
            waits.append(remaining if remaining > 0 else self._interval)
        if any(data["state"] in ACTIVE_JOB_STATES or data["state"] == QUEUED for data in self.store.jobs.values()):
            waits.append(self._interval)
        return min(waits) if waits else None

    def _perform_pending(self) -> None:
        perform = getattr(self.workers, "perform_pending", None)
        failures = perform() if callable(perform) else None
        if not failures:
            return
        with self._condition:
            for handle, message in failures:
                for data in self.store.jobs.values():
                    if data.get("attempt_handle") == handle:
                        data["summary"] = f"The worker for this attempt could not be started: {message}"
            self.store.save()
        self._wake.set()

    def _submitter_of(self, job_id: str) -> str:
        # Asked inside the tick, which holds the condition already.
        return str(self.store.jobs[job_id].get("submitter_label") or ANONYMOUS)

    # -- Publication -------------------------------------------------------

    def _publish(self) -> None:
        """Recompute every job's public status, and move the revision of each one that changed."""
        queue = self._queue()
        changed = False
        for job_id, data in self.store.jobs.items():
            public = self._public(data, queue)
            digest = _digest(json.dumps(public, sort_keys=True, default=str))
            if data.get("status_sha256") != digest:
                data["status_sha256"] = digest
                data["revision"] = int(data.get("revision") or 0) + 1
                changed = True
            self._statuses[job_id] = public
        for gone in set(self._statuses) - set(self.store.jobs):
            del self._statuses[gone]
        if changed:
            self.store.save()
        self._condition.notify_all()

    def _queue(self) -> _Queue:
        jobs = list(self.store.jobs.values())
        now = float(self.scheduler.clock())
        occupants = {key: str(data["job_id"]) for data in jobs if data["state"] in ACTIVE_JOB_STATES for key in data.get("lock_keys") or []}
        queued = sorted((data for data in jobs if data["state"] == QUEUED), key=_order)
        claimed: dict[str, str] = {}
        for data in queued:
            if data.get("queued_at") is not None and now - float(data["queued_at"]) >= self.scheduler.settings.reserve_after_s:
                for key in data.get("lock_keys") or []:
                    claimed.setdefault(key, str(data["job_id"]))
        return _Queue(
            now,
            occupants,
            tuple(str(data["job_id"]) for data in queued),
            claimed,
            frozenset(self.scheduler.drained),
            frozenset(self.scheduler.incidents),
            frozenset(self.scheduler.holders),
        )

    def _names_of(self, key: str) -> list[str]:
        """The logical names of the devices a lock key holds; a CAN participant's key is its bus's."""
        return self._names.get(key) or self._names.get(key.split("#", 1)[0]) or []

    def _devices(self, keys: Sequence[str]) -> list[str]:
        return sorted({name for key in keys for name in self._names_of(key)})

    def _waiting_for(self, keys: Sequence[str], job_id: str | None, queue: _Queue) -> list[JsonObject]:
        """Why a job with these keys does not start, device by device."""
        waiting: list[JsonObject] = []
        for key in keys:
            reasons: list[tuple[str, str | None]] = []
            if key in queue.occupants:
                reasons.append(("held", queue.occupants[key]))
            if key in queue.drained:
                reasons.append(("drained", None))
            if key in queue.incidents:
                reasons.append(("incident", None))
            if key in queue.holders and key not in queue.occupants:
                reasons.append(("held_outside_queue", None))
            claimant = queue.claimed.get(key)
            if claimant is not None and claimant != job_id:
                reasons.append(("reserved", claimant))
            for name in self._names_of(key):
                for reason, holder in reasons:
                    item: JsonObject = {"device": name, "reason": reason}
                    if holder is not None:
                        item["held_by"] = {"job_id": holder}
                    if item not in waiting:
                        waiting.append(item)
        return waiting

    def _expected_s(self, data: Mapping[str, Any]) -> float | None:
        lower = data.get("lower_bound_s")
        samples = self.store.samples(data.get("plan_digest"))
        if not samples:
            return None if lower is None else float(lower)
        mean = sum(samples) / len(samples)
        return mean if lower is None else max(float(lower), mean)

    def _public(self, data: Mapping[str, Any], queue: _Queue) -> JsonObject:
        job_id = str(data["job_id"])
        state = str(data["state"])
        keys = list(data.get("lock_keys") or [])
        waiting = state == QUEUED
        status: JsonObject = {
            "job_id": job_id,
            "state": state,
            "started": bool(data.get("started")),
            "user": data.get("submitter_label"),
            "plan_name": data.get("plan_name"),
            "plan_digest": data.get("plan_digest"),
            "devices": self._devices(keys),
            "queue_position": queue.order.index(job_id) + 1 if waiting and job_id in queue.order else None,
            "waiting_for": self._waiting_for(keys, job_id, queue) if waiting else [],
            "missing_artifacts": list(data.get("missing_artifacts") or []),
            "estimate": None if state in TERMINAL_JOB_STATES else {"lower_bound_s": data.get("lower_bound_s"), "duration_s": self._expected_s(data)},
            "ok": _verdict(data),
            "summary": _summary(data),
        }
        status.update(_carried(data))
        return status

    def _entry(self, data: Mapping[str, Any], now: float) -> JsonObject:
        """A job as the job list shows it: its public fields and nothing a job produced."""
        dispatched = data.get("dispatched_at")
        finished = data.get("finished_at")
        duration: float | None = None
        if dispatched is not None:
            duration = round(max(0.0, float(finished if finished is not None else now) - float(dispatched)), 3)
        entry: JsonObject = {
            "job_id": data["job_id"],
            "user": data.get("submitter_label"),
            "plan_name": data.get("plan_name"),
            "plan_digest": data.get("plan_digest"),
            "devices": self._devices(list(data.get("lock_keys") or [])),
            "state": data["state"],
            "duration_s": duration,
            "ok": _verdict(data),
        }
        entry.update(_carried(data))
        return entry

    # -- Lookup ------------------------------------------------------------

    def _data(self, job_id: str) -> JsonObject:
        if not isinstance(job_id, str) or JOB_ID_PATTERN.match(job_id) is None or job_id not in self.store.jobs:
            raise _not_found(job_id)
        return self.store.jobs[job_id]

    def _owned(self, job_id: str, job_token: str) -> JsonObject:
        data = self._data(job_id)
        expected = data.get("token_sha256")
        if not isinstance(expected, str) or not isinstance(job_token, str) or not hmac.compare_digest(expected, _digest(job_token)):
            raise _token_refused(job_id)
        return data

    def _status_of(self, job_id: str) -> JobStatus:
        data = self._data(job_id)
        public = self._statuses.get(job_id) or self._public(data, self._queue())
        return JobStatus(int(data.get("revision") or 0), copy.deepcopy(public))

    def _canonical(self, data: Mapping[str, Any]) -> Path | None:
        handle = data.get("attempt_handle")
        if not isinstance(handle, str):
            return None
        try:
            return canonical_run_report_path(self.preparer.config_for(self.store.job(dict(data))), handle)
        except (ConfigError, OSError):
            return None

    def _refusal_document(self, data: Mapping[str, Any]) -> JsonObject:
        """The report of a job that left no run report: what became of it, in this module's words."""
        document: JsonObject = {
            "ok": False,
            "tool": "test_reactor_run",
            "job_id": data["job_id"],
            "state": data["state"],
            "summary": _summary(data),
            "submitted_by": data.get("submitter_label"),
            "submitted_by_source": "client",
        }
        document.update(_carried(data))
        return document

    def _ended(self, job_id: str, job_token: str) -> tuple[JsonObject, Path | None, JsonObject]:
        """An ended job of this token's: its record, its run report's path and its refusal document."""
        data = self._owned(job_id, job_token)
        if data["state"] not in TERMINAL_JOB_STATES:
            raise _not_ended(job_id, str(data["state"]))
        return data, self._canonical(data), self._refusal_document(data)

    def _submission(self, data: Mapping[str, Any], token: str | None) -> JsonObject:
        status = self._statuses[str(data["job_id"])]
        answer: JsonObject = {
            "job_id": data["job_id"],
            "job_token": token,
            "state": status["state"],
            "missing_artifacts": copy.deepcopy(status["missing_artifacts"]),
            "queue_position": status["queue_position"],
            "waiting_for": copy.deepcopy(status["waiting_for"]),
        }
        if token is None:
            answer["summary"] = "This submission was answered before. Its job token is shown once, in that first answer, and is not shown again."
        return answer

    def _retire(self, job_id: str) -> None:
        self.store.retire(job_id)
        self.store.save()
        with suppress(OSError):
            (self.store.root / f".{job_id}.lock").unlink()

    # -- Submission --------------------------------------------------------

    def submit(self, plan: bytes, artifacts: Mapping[str, bytes], *, idempotency_key: str | None, submitted_by: str | None) -> JsonObject:
        label = submitted_by or ANONYMOUS
        scope = _digest(idempotency_key + "\0" + label) if idempotency_key else None
        with self._condition:
            if scope is not None:
                replay = self._replays.get(scope)
                if replay is not None:
                    return copy.deepcopy(replay)
                kept = next((data for data in self.store.jobs.values() if data.get("idempotency_sha256") == scope), None)
                if kept is not None:
                    return self._submission(kept, None)
            token = secrets.token_urlsafe(32)
            record: JsonObject = {"token_sha256": _digest(token), "revision": 0}
            if scope is not None:
                record["idempotency_sha256"] = scope
            job = self.scheduler.submit_content(plan_name=PLAN_NAME, plan=bytes(plan), artifacts=dict(artifacts), submitter_label=label, record=record)
            data = self.store.jobs[job.job_id]
            if data["state"] == REJECTED:
                refusal = _admission_refusal(data, self.settings.retry_after_s)
                self._retire(job.job_id)
                raise refusal
            self._publish()
            answer = self._submission(data, token)
            if scope is not None:
                self._replays[scope] = copy.deepcopy(answer)
        self._wake.set()
        return answer

    def check_plan(self, plan: bytes, artifacts: Mapping[str, bytes], *, submitted_by: str | None) -> JsonObject:
        """The submission's admission on a throwaway queue, measured against the real one, and no job."""
        label = submitted_by or ANONYMOUS
        with self._condition:
            open_jobs = sum(1 for data in self.store.jobs.values() if data.get("submitter_label") == label and data["state"] in QUOTA_JOB_STATES)
            queue = self._queue()
        server = self.config.server
        assert server is not None
        root = self.store.root / f"{CHECK_PREFIX}{secrets.token_hex(8)}"
        try:
            bench = replace(self.config, server=replace(server, job_root=str(root)))
            throwaway = Scheduler(bench, replace(self.scheduler.settings, job_root=root), workspace_preparer=JobWorkspacePreparer(bench))
            job = throwaway.submit_content(plan_name=PLAN_NAME, plan=bytes(plan), artifacts=dict(artifacts), submitter_label=label)
            data = throwaway.store.jobs[job.job_id]
            if data["state"] == REJECTED:
                raise _admission_refusal(data, self.settings.retry_after_s)
            if data["state"] == QUEUED and open_jobs >= self.scheduler.settings.max_queued_jobs_per_user:
                raise _admission_refusal(data, self.settings.retry_after_s, over_quota=True)
            keys = list(data.get("lock_keys") or [])
            waiting = self._waiting_for(keys, None, queue)
            with self._condition:
                expected = self._expected_s(data)
            return {
                "ok": True,
                "plan_name": data.get("plan_name"),
                "plan_digest": data.get("plan_digest"),
                "devices": self._devices(keys),
                "missing_artifacts": list(data.get("missing_artifacts") or []),
                "estimate": {"lower_bound_s": data.get("lower_bound_s"), "duration_s": expected},
                "would_start": data["state"] == QUEUED and not waiting,
                "waiting_for": waiting,
            }
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def upload_artifact(self, job_id: str, path: str, body: bytes, *, job_token: str) -> JsonObject:
        with self._condition:
            data = self._owned(job_id, job_token)
            staged = data.get("artifacts") or {}
            if path in staged:
                raise _artifact_refusal(job_id, path, uploaded=True)
            if data["state"] in TERMINAL_JOB_STATES:
                raise _ended_refusal(job_id, str(data["state"]))
            if path not in set(data.get("missing_artifacts") or []):
                raise _artifact_refusal(job_id, path, uploaded=False)
            if data["state"] != AWAITING_ARTIFACTS:
                raise _artifact_refusal(job_id, path, uploaded=True)
            self.scheduler.provide_content(job_id, artifacts={path: bytes(body)}, submitter_label=str(data["submitter_label"]))
            data = self.store.jobs[job_id]
            self._publish()
            if data["state"] == REJECTED:
                raise _admission_refusal(data, self.settings.retry_after_s)
            accepted = data["artifacts"][path]
            status = self._statuses[job_id]
            answer: JsonObject = {
                "job_id": job_id,
                "path": path,
                "sha256": accepted["sha256"],
                "size_bytes": accepted["size_bytes"],
                "state": status["state"],
                "missing_artifacts": copy.deepcopy(status["missing_artifacts"]),
                "queue_position": status["queue_position"],
            }
        self._wake.set()
        return answer

    # -- Status and events -------------------------------------------------

    def status(self, job_id: str) -> JobStatus:
        with self._condition:
            return self._status_of(job_id)

    def wait_for_revision(self, job_id: str, *, after_revision: int, timeout_s: float) -> JobStatus | None:
        with self._condition:
            self._data(job_id)

            def changed() -> bool:
                data = self.store.jobs.get(job_id)
                return data is None or int(data.get("revision") or 0) > after_revision

            if not self._condition.wait_for(changed, timeout=max(0.0, timeout_s)):
                return None
            return self._status_of(job_id)

    def events_after(self, job_id: str, *, last_event_id: str | None, timeout_s: float) -> JobEvents:
        cursor = _cursor(job_id, last_event_id)
        with self._condition:
            self._data(job_id)

            def ready() -> bool:
                data = self.store.jobs.get(job_id)
                return data is None or int(data.get("revision") or 0) > cursor or data["state"] in TERMINAL_JOB_STATES

            self._condition.wait_for(ready, timeout=max(0.0, timeout_s))
            snapshot = self._status_of(job_id)
            ended = snapshot.status["state"] in TERMINAL_JOB_STATES
            events = (JobEvent(f"{job_id}:{snapshot.revision}", snapshot.status),) if snapshot.revision > cursor else ()
            return JobEvents(events, ended)

    def withdraw(self, job_id: str, *, job_token: str) -> JsonObject:
        with self._condition:
            data = self._owned(job_id, job_token)
            if data["state"] in TERMINAL_JOB_STATES:
                raise _ended_refusal(job_id, str(data["state"]))
            self.scheduler.withdraw(job_id, submitter_label=str(data["submitter_label"]))
            data = self.store.jobs[job_id]
            self._publish()
            # Left the queue at once, or asked its worker to stop.
            answer: JsonObject = {"job_id": job_id, "state": data["state"], "started": data["state"] != WITHDRAWN}
        self._wake.set()
        return answer

    # -- What a job produced -------------------------------------------------

    def report(self, job_id: str, *, job_token: str) -> JsonObject:
        with self._condition:
            _data, canonical, refusal = self._ended(job_id, job_token)
        return _read_document(canonical) or refusal

    def junit(self, job_id: str, *, job_token: str) -> bytes:
        with self._condition:
            data, canonical, refusal = self._ended(job_id, job_token)
            plan_path = Path(str(data["plan_path"]))
        report = _read_document(canonical) or refusal
        return junit_xml_document(report, plan_steps=_plan_steps(plan_path)).encode("utf-8")

    def evidence(self, job_id: str, *, job_token: str) -> JobContent:
        with self._condition:
            data, canonical, refusal = self._ended(job_id, job_token)
            directory = self.store.directory(job_id)
            workspace = Path(self.preparer.config_for(self.store.job(data)).work_dir)
            lock = self._evidence_locks.setdefault(job_id, threading.Lock())
        name = f"{job_id}-evidence.zip"
        # Built once per job, outside the lock and one request at a time, and kept.
        with lock:
            try:
                body = self._bundle(directory, name, canonical, refusal, workspace)
            except OSError as error:
                if not directory.is_dir():
                    raise _not_found(job_id) from error
                raise
        return JobContent(body, "application/zip", name)

    @staticmethod
    def _bundle(directory: Path, name: str, canonical: Path | None, refusal: JsonObject, workspace: Path) -> bytes:
        """The job's evidence, zipped: `runevidence` written from its run report, or from its refusal document."""
        cached = directory / "report" / name
        if cached.is_file():
            return safe_read_bytes(cached)
        report_path = canonical if canonical is not None and canonical.is_file() else None
        if report_path is None:
            report_path = directory / "report" / REFUSAL_REPORT_NAME
            atomic_write_bytes(report_path, (json.dumps(refusal, indent=2) + "\n").encode("utf-8"))
        out = directory / "report" / EVIDENCE_DIRECTORY
        if out.exists():
            shutil.rmtree(out)
        write_run_evidence(str(report_path), str(out), workspace=workspace, environ={})
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(out.rglob("*")):
                if path.is_file():
                    archive.write(path, path.relative_to(out).as_posix())
        body = buffer.getvalue()
        atomic_write_bytes(cached, body)
        return body

    def log(self, job_id: str, n: int, *, job_token: str, byte_range: ByteRange | None, follow: bool, timeout_s: float) -> LogRead:
        with self._condition:
            data = self._owned(job_id, job_token)
            directory = Path(self.preparer.config_for(self.store.job(data)).logs.directory)
            ended = data["state"] in TERMINAL_JOB_STATES
        try:
            logs = sorted(path for path in directory.iterdir() if path.is_file()) if directory.is_dir() else []
        except OSError:
            logs = []
        if not 0 <= n < len(logs):
            raise _not_found(job_id)
        path = logs[n]
        size = path.stat().st_size
        start, end = self._span(byte_range, size)
        if byte_range is not None and (start > size or (start >= size and not follow) or end < start):
            raise _range_refused(size)
        deadline = time.monotonic() + max(0.0, timeout_s)
        while follow and not ended and start >= size and time.monotonic() < deadline:
            with self._condition:
                self._condition.wait(timeout=min(self._interval, max(0.0, deadline - time.monotonic())))
                current = self.store.jobs.get(job_id)
                ended = current is None or current["state"] in TERMINAL_JOB_STATES
            size = path.stat().st_size
            start, end = self._span(byte_range, size)
        with open(path, "rb") as handle:
            handle.seek(start)
            body = handle.read(max(0, end - start))
        return LogRead(body, start, size, ended and start + len(body) >= size)

    @staticmethod
    def _span(byte_range: ByteRange | None, size: int) -> tuple[int, int]:
        """The first byte and the byte after the last one a range names in a log of `size` bytes."""
        if byte_range is None:
            return 0, size
        if byte_range.first is None:
            return max(0, size - (byte_range.last or 0)), size
        return byte_range.first, size if byte_range.last is None else min(size, byte_range.last + 1)

    # -- Reads of the whole queue ----------------------------------------------

    def jobs(self, *, state: str | None, device: str | None, user: str | None, since: str | None) -> tuple[JsonObject, ...]:
        threshold = _since(since)
        with self._condition:
            now = float(self.scheduler.clock())
            records = sorted(self.store.jobs.values(), key=lambda data: (float(data.get("submitted_at") or 0.0), _order(data)[0]), reverse=True)
            entries = [self._entry(data, now) for data in records]
        return tuple(
            entry
            for entry, data in zip(entries, records, strict=True)
            if (state is None or entry["state"] == state)
            and (device is None or device in entry["devices"])
            and (user is None or entry["user"] == user)
            and (threshold is None or float(data.get("submitted_at") or 0.0) >= threshold)
        )

    def queue(self) -> QueueSnapshot:
        with self._condition:
            now = float(self.scheduler.clock())
            entries = []
            for job_id in self._queue().order:
                data = self.store.jobs[job_id]
                status = self._statuses.get(job_id) or {}
                entries.append({
                    **self._entry(data, now),
                    "queue_position": status.get("queue_position"),
                    "waiting_for": copy.deepcopy(status.get("waiting_for") or []),
                })
        return QueueSnapshot(tuple(entries))

    def device_activity(self) -> Mapping[str, DeviceActivity]:
        with self._condition:
            queue = self._queue()
            now = queue.now
            newest = sorted(self.store.jobs.values(), key=lambda data: float(data.get("submitted_at") or 0.0), reverse=True)
            occupants: dict[str, dict[str, list[str]]] = {}
            for key, job_id in queue.occupants.items():
                for name in self._names_of(key):
                    occupants.setdefault(name, {}).setdefault(job_id, []).append(key)
            activity: dict[str, DeviceActivity] = {}
            for name, keys in self._keys.items():
                held_job_keys = {job_id: tuple(job_keys) for job_id, job_keys in occupants.get(name, {}).items()}
                held = next((queue.occupants[key] for key in keys if key in queue.occupants), None)
                if held is None:
                    held = next(iter(held_job_keys), None)
                reserved = next((queue.claimed[key] for key in keys if key in queue.claimed), None)
                recent = tuple(self._entry(data, now) for data in newest if name in self._devices(list(data.get("lock_keys") or [])))[:RECENT_JOBS]
                activity[name] = DeviceActivity(
                    "held" if held is not None else "reserved" if reserved is not None else None,
                    held if held is not None else reserved,
                    any(key in queue.drained for key in keys),
                    recent,
                    held_job_keys,
                )
        return activity

    def metrics(self) -> str:
        with self._condition:
            queue = self._queue()
            waited = [queue.now - float(self.store.jobs[job_id].get("queued_at") or queue.now) for job_id in queue.order]
            busy = {name: any(key in queue.occupants for key in keys) for name, keys in self._keys.items()}
        lines = [
            "# HELP agentic_hil_queue_length Jobs waiting in the bench's queue.",
            "# TYPE agentic_hil_queue_length gauge",
            f"agentic_hil_queue_length {len(queue.order)}",
            "# HELP agentic_hil_queue_wait_seconds How long the job that has waited longest has waited.",
            "# TYPE agentic_hil_queue_wait_seconds gauge",
            f"agentic_hil_queue_wait_seconds {round(max(waited, default=0.0), 3)}",
            "# HELP agentic_hil_device_busy Whether a job runs on the device.",
            "# TYPE agentic_hil_device_busy gauge",
            *(f'agentic_hil_device_busy{{device="{_label_value(name)}"}} {1 if held else 0}' for name, held in sorted(busy.items())),
        ]
        return "\n".join(lines) + "\n"

    # -- Admin, answered on loopback alone by the transport --------------------

    def _device_keys(self, device: str) -> tuple[str, ...]:
        if device not in self._keys:
            raise _device_refused(device, sorted(self._keys))
        return self._keys[device]

    def drain(self, device: str) -> JsonObject:
        """Grant the device to no new job until the bench server starts again."""
        with self._condition:
            keys = self._device_keys(device)
            self.scheduler.drained.update(keys)
            occupants = self._queue().occupants
            running = next((occupants[key] for key in keys if key in occupants), None)
            self._publish()
        self._wake.set()
        return {"device": device, "drained": True, "running_job": running}

    def recover(self, device: str, *, operator_statement: str, accept_config_change: bool) -> JsonObject:
        with self._condition:
            self._device_keys(device)
        # Outside the lock: a recover may wait on its device, and nothing else should wait with it.
        result = self._recover(self.config, operator_statement=operator_statement, accept_config_change=accept_config_change)
        with self._condition:
            self._publish()
        self._wake.set()
        return result


@contextmanager
def _opened(
    config: AgenticHILConfig,
    settings: ServerSettings,
    scheduler_settings: SchedulerSettings,
    worker_source: WorkerSource | None,
    recover_call: Callable[..., JsonObject] | None,
    reconcile_interval_s: float,
) -> Iterator[BenchJobService]:
    service = BenchJobService(
        config, settings, scheduler_settings,
        worker_source=worker_source, recover_call=recover_call, reconcile_interval_s=reconcile_interval_s,
    )
    service.start()
    try:
        yield service
    finally:
        service.close()


def open_bench_job_service(
    config: AgenticHILConfig,
    settings: ServerSettings,
    *,
    worker_source: WorkerSource | None = None,
    recover_call: Callable[..., JsonObject] | None = None,
    reconcile_interval_s: float = RECONCILE_INTERVAL_S,
) -> AbstractContextManager[JobService]:
    """The job service of this bench, open for as long as the context lasts.

    The queue's policy is the `server` block's: its job root, its limits and
    its retention. Closing the service stops its tick and keeps every record;
    a running worker runs its plan to its end under its device locks, and the
    next server asks it to stop and records the attempt as `worker_gone`.
    """
    server = config.server
    if server is None:
        raise invalid_input("The bench's job service is opened from a bench with a server block, and this bench has none.", {"field": "server"})
    limits = server.limits
    scheduler_settings = SchedulerSettings(
        job_root=Path(server.job_root),
        max_job_runtime_s=limits.max_job_runtime_s,
        max_queue_s=limits.max_queue_s,
        max_artifact_wait_s=limits.max_artifact_wait_s,
        reserve_after_s=limits.reserve_after_s,
        max_queued_jobs_per_user=limits.max_queued_jobs_per_user,
        retention_days=server.retention_days,
    )
    return _opened(config, settings, scheduler_settings, worker_source, recover_call, reconcile_interval_s)
