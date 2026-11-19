"""The fair bench queue: jobs wait their turn, pass where the bench is free, and never probe a lock.

A job is admitted once. Its plan is loaded and preflighted exactly as the
reactor would load and preflight it, its least possible runtime is held to the
server's cap, and its submitter's open jobs to the per-submitter quota. An
admitted job takes the next sequence number and keeps it for as long as it
waits, a start its worker found refused included.

The queue is one list in sequence order. On every tick the oldest job whose
whole device set is free starts, and a younger job passes an older one only on
devices nothing older has claimed. Once a job has waited ``reserve_after_s``
its whole set is claimed for it, so nothing younger takes any of those devices
in front of it; with ``reserve_after_s`` at zero the queue is strictly first
in, first out. Devices conflict by their lock keys, so two names for one
physical unit are one device and two participants of one CAN bus are two.

Availability is read, never tested. A holder record is the advice the bench's
own mutex writes, read with `BenchMutex.holder`; nothing here opens, takes or
gives back a device lock, because a probe is a hold however short. A record can
outlive its holder, so a job held up by one tries again after
``retry_interval_s`` and lets its worker meet the real lock, and a worker
refused there returns the job to the place it had.

Status uses opaque resource digests as keys in ``devices``. Each device view
contains position, reserved_for, holder, held_since and estimated_free_at.
Job-level estimate fields describe the plan lower bound and digest history.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import secrets
import socket
import subprocess
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import suppress
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import TYPE_CHECKING, Any, Protocol, cast

from . import runlifecycle
from .artifacts import ArtifactManager, has_traversal_segment
from .bench import BenchMutex, resource_digest, utc_now_iso
from .config import (
    ConfigError,
    atomic_write_bytes,
    atomic_write_text,
    configured_work_path,
    safe_directory,
    safe_read_bytes,
    safe_read_text,
)
from .coordination import (
    STANDING_INCIDENT_STATES,
    CoordinationError,
    _read_record_at,
    _record_cleanup_reasons,
    _record_directory_if_present,
    record_auto_recoverable,
)
from .jobstore import (
    ACTIVE_JOB_STATES,
    AWAITING_ARTIFACTS,
    EXPIRED,
    FAILED_TO_START,
    FINISHED,
    QUEUED,
    REJECTED,
    RUNNING,
    RUNTIME_EXCEEDED,
    STARTING,
    STOPPING,
    TERMINAL_JOB_STATES,
    WITHDRAWING,
    WITHDRAWN,
    WORKER_GONE,
    Job,
    JobStore,
    SchedulerSettings,
    invalid_input,
    plan_lower_bound_s,
)
from .process import terminate_process_tree
from .report import canonical_run_report_path
from .test_reactor import TestConfig, TestReactor, TestStep, flatten_steps, load_test_config, plan_devices
from .types import AgenticHILConfig, JsonObject

if TYPE_CHECKING:
    from .tools import AgenticHILToolService

# The step argument that names a file a plan reads from the workspace, at any
# depth of a step's arguments: a flash image, a debug image, a defmt source.
ARTIFACT_PATH_FIELD = "image_path"
# What `submitter_label` may be. It is the caller's own word for who it is,
# which status says outright, so it is held to being a readable name.
SUBMITTER_LABEL_MAX_LENGTH = 128
# The jobs a submitter's quota counts: everything waiting for or holding a turn.
QUOTA_JOB_STATES = frozenset({QUEUED, *ACTIVE_JOB_STATES})
# How long a worker that has exited may take to show its record before it
# counts as gone. The process a spawn returns is not always the one that runs
# the plan, and the record is what says which process really runs a handle.
WORKER_EXIT_GRACE_S = runlifecycle.WORKER_EXIT_GRACE_S
# How long ending an owned worker's process tree may take.
WORKER_TERMINATE_TIMEOUT_S = 10.0
# Where a job's own account of its outcome is written, inside its directory.
JOB_REPORT_NAME = "job-report.json"
WORKER_LOG_NAME = "worker.log"


class WorkspacePreparer(Protocol):
    """Where a job runs and under which configuration.

    ``prepare`` is idempotent: admission calls it before preflight, and the
    start calls it again, so a second call checks what the first placed.
    ``config_for`` names the configuration the job is admitted, run and
    reported under, without placing anything.
    """

    def prepare(self, job: Job) -> tuple[AgenticHILConfig, Path]: ...
    def config_for(self, job: Job) -> AgenticHILConfig: ...


class IncidentSource(Protocol):
    def standing(self, lock_keys: Sequence[str]) -> Mapping[str, JsonObject]: ...


class HolderSource(Protocol):
    def holder(self, lock_key: str) -> JsonObject | None: ...


class WorkerSource(Protocol):
    """Attempt handles are allocated before the scheduler persists starting.

    start must use the supplied handle. Records and liveness are observations,
    not job-state decisions. terminate must bind the exact owned process.
    """

    def new_handle(self) -> str: ...
    def start(self, config: AgenticHILConfig, handle: str, plan: Path, *, wait_s: float) -> None: ...
    def record(self, config: AgenticHILConfig, handle: str) -> JsonObject | None: ...
    def is_gone(self, config: AgenticHILConfig, handle: str) -> bool: ...
    def stop(self, config: AgenticHILConfig, handle: str) -> None: ...
    def terminate(self, config: AgenticHILConfig, handle: str) -> None: ...


class ExistingWorkspacePreparer:
    """Run a job in the bench's own workspace, with the bench's own configuration.

    The stored plan is run as stored, and the artifacts it names are read where
    it names them. A staged artifact is the job's record of the bytes its
    submitter provided, so a workspace file that no longer has those bytes
    refuses the start rather than running something nobody submitted.
    """

    def __init__(self, config: AgenticHILConfig):
        self.config = config

    def config_for(self, job: Job) -> AgenticHILConfig:
        return self.config

    def prepare(self, job: Job) -> tuple[AgenticHILConfig, Path]:
        for relative, staged in sorted(job.artifacts.items()):
            try:
                provided = hashlib.sha256(safe_read_bytes(staged)).hexdigest()
                present = hashlib.sha256(safe_read_bytes(configured_work_path(self.config, relative), workspace=self.config.work_dir)).hexdigest()
            except (ConfigError, OSError) as error:
                raise invalid_input("An artifact this job was given could not be read back from the workspace.", {"artifact": relative, "backend_error": str(error)}) from error
            if present != provided:
                raise invalid_input("The workspace file an artifact of this job names no longer holds the bytes that were provided.", {"artifact": relative})
        return self.config, job.plan_path


class RecordedIncidentSource:
    """Standing incidents as the coordination records state them, read without taking any lock.

    An incident the workspace that raised it would settle by itself is left
    out: the next run on the device is what settles it, and holding every run
    back from it would leave it standing forever.
    """

    def __init__(self, config: AgenticHILConfig):
        self.config = config

    def standing(self, lock_keys: Sequence[str]) -> Mapping[str, JsonObject]:
        try:
            directory = _record_directory_if_present(self.config)
        except (ConfigError, OSError):
            return {}
        if directory is None:
            return {}
        found: dict[str, JsonObject] = {}
        for key in lock_keys:
            reason = self._reason(directory / f"{resource_digest(key)}.json", key)
            if reason is not None:
                found[key] = reason
        return found

    @staticmethod
    def _reason(path: Path, key: str) -> JsonObject | None:
        # A record is replaced atomically, and a read can still meet the
        # replace on a platform that refuses to open a file being renamed.
        for _ in range(3):
            try:
                record = _read_record_at(path, key)
                break
            except CoordinationError:
                continue
        else:
            return {"incident_state": "unreadable", "summary": "The coordination record for this device could not be read, so whether an incident holds it is unknown."}
        if record is None or record.get("state") not in STANDING_INCIDENT_STATES or record_auto_recoverable(record) is True:
            return None
        return {"incident_state": record.get("state"), "quarantine_id": record.get("quarantine_id"), "reasons": _record_cleanup_reasons(record)}


class BenchHolderSource:
    """The holder record of a device, read the way a refused contender reads it.

    Only ever `BenchMutex.holder`, a read of the record a holder wrote. No
    device lock is opened, taken or released here, not even to be given
    straight back: such a probe is a hold, and a run reaching for the device in
    that instant would be refused because of it.
    """

    def __init__(self) -> None:
        self._reader = BenchMutex(frontend="bench-queue")

    def holder(self, lock_key: str) -> JsonObject | None:
        return self._reader.holder(lock_key)


class RunWorkerSource:
    """The reactor's own detached worker, one per attempt, under the handle the scheduler allocated.

    Started by `runlifecycle.spawn_run_worker`, observed through the run record
    and the run's lifetime lock, stopped by the stop file every run reads, and
    ended only as the process this source started: a handle it did not start
    is one it cannot end, because a pid read off a record could by now name
    anything.
    """

    def __init__(self) -> None:
        self._children: dict[str, subprocess.Popen] = {}
        self._exited: dict[str, float] = {}

    def new_handle(self) -> str:
        return runlifecycle.new_run_handle()

    def start(self, config: AgenticHILConfig, handle: str, plan: Path, *, wait_s: float) -> None:
        self._children[handle] = runlifecycle.spawn_run_worker(config, handle, str(plan), wait_s=wait_s)

    def record(self, config: AgenticHILConfig, handle: str) -> JsonObject | None:
        return runlifecycle.read_run_record(config, handle)

    def is_gone(self, config: AgenticHILConfig, handle: str) -> bool:
        child = self._children.get(handle)
        if child is not None and child.poll() is None:
            return False
        if child is not None and _readable_record(config, handle) is None:
            exited = self._exited.setdefault(handle, time.monotonic())
            if time.monotonic() - exited < WORKER_EXIT_GRACE_S:
                return False
        return runlifecycle.worker_is_gone(config, handle)

    def stop(self, config: AgenticHILConfig, handle: str) -> None:
        # Written whatever the record says, so a worker still taking its
        # devices reads it inside that wait and ends there.
        request = {"run": handle, "requested_at": utc_now_iso(), "requested_by_pid": os.getpid()}
        atomic_write_text(runlifecycle.stop_path(config, handle), json.dumps(request, indent=2) + "\n")

    def terminate(self, config: AgenticHILConfig, handle: str) -> None:
        child = self._children.get(handle)
        if child is None:
            raise RuntimeError("this scheduler did not start that worker, so it has no process of its own to end; the job keeps its devices until the worker is gone")
        if child.poll() is None:
            terminate_process_tree(child, WORKER_TERMINATE_TIMEOUT_S, graceful=False)


def _readable_record(config: AgenticHILConfig, handle: str) -> JsonObject | None:
    try:
        return runlifecycle.read_run_record(config, handle)
    except ConfigError:
        return None


class _AdmissionService:
    """What preflight reads of a tool service, and nothing that can reach a device.

    Preflight asks a service for one thing, the artifact policy, so admission
    answers with the plan's own refusal without a coordinator, a backend or a
    session behind it.
    """

    def __init__(self, config: AgenticHILConfig):
        self.config = config
        self.artifacts = ArtifactManager(config)

    def close(self) -> None:
        self.artifacts.close()


def plan_artifact_paths(steps: Sequence[TestStep]) -> list[str]:
    """Every workspace file the plan names as an artifact, once each, in the order it first names them."""
    found: list[str] = []
    for step in flatten_steps(steps):
        for path in _named_paths(step.arguments):
            if path not in found:
                found.append(path)
    return found


def _named_paths(value: Any) -> Iterator[str]:
    if isinstance(value, dict):
        for key, item in value.items():
            if key == ARTIFACT_PATH_FIELD and isinstance(item, str):
                yield item
            else:
                yield from _named_paths(item)
    elif isinstance(value, list):
        for item in value:
            yield from _named_paths(item)


def _validated_label(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > SUBMITTER_LABEL_MAX_LENGTH or not value.isprintable():
        raise invalid_input(
            f"`submitter_label` must be a printable name of 1 to {SUBMITTER_LABEL_MAX_LENGTH} characters.",
            {"field": "submitter_label"},
        )
    return value


def _validated_artifact_name(value: object) -> str:
    """The path a plan names an artifact under: relative, inside the workspace, with no traversal."""
    if not isinstance(value, str) or not value or has_traversal_segment(value) or PurePosixPath(value.replace("\\", "/")).is_absolute() or PureWindowsPath(value).drive:
        raise invalid_input("An artifact is named by the relative workspace path its plan names it under.", {"field": "artifacts", "artifact": str(value)[:256]})
    return value


def _read_input(path: Path, what: str, workspace: str) -> bytes:
    """A file a submitter handed the queue, held to the workspace the way every plan and artifact is."""
    try:
        return safe_read_bytes(path, workspace=workspace)
    except (ConfigError, OSError) as error:
        raise invalid_input(f"The {what} file could not be read.", {"field": what, "path": str(path), "backend_error": str(error)}) from error


def _read_json(path: Path) -> JsonObject | None:
    try:
        value = json.loads(safe_read_text(path))
    except (ConfigError, OSError, UnicodeDecodeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _record_owner(record: JsonObject) -> JsonObject:
    """The owner a holder record names, or nothing when it names none."""
    owner = record.get("owner")
    return owner if isinstance(owner, dict) else {}


def _public_holder(record: JsonObject) -> JsonObject:
    """Who holds a device, by the names a contender may repeat and nothing else."""
    owner = _record_owner(record)
    return {"frontend": owner.get("frontend"), "label": owner.get("label")}


def _order(data: JsonObject) -> tuple[float, float]:
    sequence = data.get("sequence")
    return (math.inf if sequence is None else float(sequence), float(data.get("submitted_at") or 0.0))


class Scheduler:
    """The fair bench queue over one job root.

    ``tick`` is the whole of what moves a job: it reconciles every attempt with
    what its worker says, expires what waited past its deadline, reads the
    holder records and standing incidents of the devices jobs need, starts what
    may start, and retires what retention says is old. Every other method
    answers or records at once and leaves the bench to the next tick.
    """

    def __init__(
        self,
        config: AgenticHILConfig,
        settings: SchedulerSettings,
        *,
        clock: Callable[[], float] | None = None,
        worker_source: WorkerSource | None = None,
        holder_source: HolderSource | None = None,
        incident_source: IncidentSource | None = None,
        workspace_preparer: WorkspacePreparer | None = None,
    ):
        self.config = config
        self.settings = settings
        # Wall time by default, because deadlines are persisted and have to
        # mean the same thing to the scheduler that reads them after a restart.
        self.clock: Callable[[], float] = clock or time.time
        self.worker_source: WorkerSource = worker_source or RunWorkerSource()
        self.holder_source: HolderSource = holder_source or BenchHolderSource()
        self.incident_source: IncidentSource = incident_source or RecordedIncidentSource(config)
        self.workspace_preparer: WorkspacePreparer = workspace_preparer or ExistingWorkspacePreparer(config)
        self.store = JobStore(settings.job_root)
        # Who started each attempt. One this instance did not start was left by
        # a scheduler before it, which nobody supervises any more, so it is
        # asked to stop rather than adopted.
        self._instance = secrets.token_hex(8)
        self._host = socket.gethostname()
        # The last tick's observations, by lock key: a holder record that still
        # says held, and a standing incident.
        self._holders: dict[str, JsonObject] = {}
        self._incidents: dict[str, JsonObject] = {}
        # The lock keys an operator drained: granted to no new job until the
        # scheduler starts again. Held in memory and nowhere else.
        self.drained: set[str] = set()

    @property
    def holders(self) -> Mapping[str, JsonObject]:
        """The last tick's holder records that say held, by lock key."""
        return self._holders

    @property
    def incidents(self) -> Mapping[str, JsonObject]:
        """The last tick's standing incidents, by lock key."""
        return self._incidents

    # -- Submission --------------------------------------------------------

    def submit(
        self, *, plan: Path, artifacts: Mapping[str, Path], submitter_label: str,
        max_queue_s: float | None = None,
    ) -> Job:
        """Store a plan as a job and admit it, or say why it waits or was refused.

        The answer is the job; its status says which of the three it is.
        """
        source = Path(plan) if Path(plan).is_absolute() else Path(self.config.work_dir) / plan
        return self._submit(source.name, source, artifacts, submitter_label, max_queue_s, None)

    def submit_content(
        self, *, plan_name: str, plan: bytes, artifacts: Mapping[str, bytes], submitter_label: str,
        max_queue_s: float | None = None, record: Mapping[str, Any] | None = None,
    ) -> Job:
        """`submit` for a plan and artifacts handed over as bytes, as a submission over the network carries them.

        `record` holds fields merged into the job's record before it is first
        saved, which is how a caller keeps what it knows of the job with it.
        """
        return self._submit(plan_name, plan, artifacts, submitter_label, max_queue_s, record)

    def _submit(
        self, plan_name: str, plan: Path | bytes, artifacts: Mapping[str, Path] | Mapping[str, bytes],
        submitter_label: str, max_queue_s: float | None, record: Mapping[str, Any] | None,
    ) -> Job:
        now = self._now()
        job = self.store.create(plan_name)
        data: JsonObject = {
            "job_id": job.job_id,
            "state": REJECTED,
            "submitter_label": None,
            "submitted_at": now,
            "sequence": None,
            "queued_at": None,
            "queue_deadline": None,
            "plan_path": str(job.plan_path),
            "plan_name": None,
            "plan_digest": None,
            "lock_keys": [],
            "lower_bound_s": None,
            "artifacts": {},
            "missing_artifacts": [],
            "attempt_handle": None,
            "attempts": 0,
            "started": False,
        }
        data.update(record or {})
        try:
            data["submitter_label"] = _validated_label(submitter_label)
            data["queue_deadline"] = now + self._queue_bound(max_queue_s)
            content = plan if isinstance(plan, bytes) else _read_input(plan, "plan", self.config.work_dir)
            atomic_write_bytes(job.plan_path, content)
            data["plan_digest"] = hashlib.sha256(content).hexdigest()
            loaded = self._load_plan(data)
            self._stage(data, loaded, artifacts)
            self._advance(data, loaded)
        except ConfigError as error:
            data["state"] = REJECTED
            data["error_type"] = error.error_type
            data["summary"] = error.summary
            data["finished_at"] = now
        self.store.jobs[job.job_id] = data
        self.store.save()
        return self.store.job(data)

    def provide_artifacts(
        self, job_id: str, *, artifacts: Mapping[str, Path], submitter_label: str,
    ) -> Job:
        """Stage the artifacts a waiting job's plan names, and admit it once none is missing."""
        return self._provide(job_id, artifacts, submitter_label)

    def provide_content(
        self, job_id: str, *, artifacts: Mapping[str, bytes], submitter_label: str,
    ) -> Job:
        """`provide_artifacts` for artifacts handed over as bytes."""
        return self._provide(job_id, artifacts, submitter_label)

    def _provide(self, job_id: str, artifacts: Mapping[str, Path] | Mapping[str, bytes], submitter_label: str) -> Job:
        data = self.store.get(job_id)
        self._require_submitter(data, submitter_label)
        if data["state"] != AWAITING_ARTIFACTS:
            raise invalid_input("Only a job awaiting artifacts takes them.", {"field": "job_id", "job_id": job_id, "state": data["state"]})
        try:
            loaded = self._load_plan(data)
            self._stage(data, loaded, artifacts)
            self._advance(data, loaded)
        except ConfigError as error:
            data["state"] = REJECTED
            data["error_type"] = error.error_type
            data["summary"] = error.summary
            data["finished_at"] = self._now()
        self.store.save()
        return self.store.job(data)

    def withdraw(self, job_id: str, *, submitter_label: str) -> Job:
        """Take a job out of the queue, or ask its worker to stop. Only its submitter may."""
        data = self.store.get(job_id)
        self._require_submitter(data, submitter_label)
        now = self._now()
        if data["state"] in {AWAITING_ARTIFACTS, QUEUED}:
            data.update({"state": WITHDRAWN, "finished_at": now, "summary": "The submitter withdrew the job before it started."})
        elif data["state"] in ACTIVE_JOB_STATES and data.get("stop_reason") != WITHDRAWN:
            if data.get("stop_requested_at") is None:
                self._request_stop(data, WITHDRAWN, now)
            else:
                data["stop_reason"] = WITHDRAWN
            data["state"] = WITHDRAWING
        else:
            return self.store.job(data)
        self.store.save()
        return self.store.job(data)

    def _load_plan(self, data: JsonObject) -> TestConfig:
        # The stored copy is read inside the job's own plan directory: the
        # submitted file was held to the workspace when it was read, and the
        # job root need not be inside it.
        stored = Path(str(data["plan_path"]))
        loaded = load_test_config(str(stored), str(stored.parent))
        data["plan_name"] = loaded.name
        data["lock_keys"] = list(plan_devices(self.config, loaded).lock_keys)
        data["lower_bound_s"] = plan_lower_bound_s(loaded.steps)
        return loaded

    def _stage(self, data: JsonObject, loaded: TestConfig, artifacts: Mapping[str, Path] | Mapping[str, bytes]) -> None:
        """Copy each provided artifact into the job, under the path its plan names it by."""
        named = set(plan_artifact_paths(loaded.steps))
        directory = self.store.directory(str(data["job_id"])) / "artifacts"
        for name, source in artifacts.items():
            relative = _validated_artifact_name(name)
            if relative not in named:
                raise invalid_input("The plan names no artifact at this path.", {"field": "artifacts", "artifact": relative})
            content = source if isinstance(source, bytes) else _read_input(Path(source), "artifact", self.config.work_dir)
            destination = directory.joinpath(*PurePosixPath(relative.replace("\\", "/")).parts)
            safe_directory(destination.parent)
            atomic_write_bytes(destination, content)
            data["artifacts"][relative] = {"path": str(destination), "sha256": hashlib.sha256(content).hexdigest(), "size_bytes": len(content)}

    def _advance(self, data: JsonObject, loaded: TestConfig) -> None:
        config = self.workspace_preparer.config_for(self.store.job(data))
        missing = [path for path in plan_artifact_paths(loaded.steps) if path not in data["artifacts"] and not self._present(config, path)]
        data["missing_artifacts"] = missing
        if missing:
            data["state"] = AWAITING_ARTIFACTS
            data["summary"] = "The plan names artifacts that are not in the workspace and were not provided; the job enters the queue once they are."
            return
        self._admit(data, loaded)

    def _admit(self, data: JsonObject, loaded: TestConfig) -> None:
        """Refuse the plan as the reactor would, then hold it to the runtime cap and the quota, then queue it."""
        now = self._now()
        # Preflight reads the plan where it will run, so the job is placed first.
        config, _plan = self.workspace_preparer.prepare(self.store.job(data))
        refusal = self._preflight(loaded, config)
        if refusal is not None:
            data["error_type"] = refusal.get("error_type", "test_config_invalid")
            data["refusal_field"] = refusal.get("field")
            self._rejected(data, str(refusal.get("summary") or "The plan was refused at preflight."), now)
            return
        if data["lower_bound_s"] > self.settings.max_job_runtime_s:
            data["error_type"] = "plan_runtime_exceeded"
            self._rejected(data, f"The plan cannot finish in less than {data['lower_bound_s']:g} s, and an attempt may run for at most {self.settings.max_job_runtime_s:g} s.", now)
            return
        if self._open_jobs(data["submitter_label"]) >= self.settings.max_queued_jobs_per_user:
            data["error_type"] = "max_queued_jobs_per_user"
            self._rejected(data, f"This submitter already has {self.settings.max_queued_jobs_per_user} jobs waiting or running.", now)
            return
        data.update({"state": QUEUED, "sequence": self.store.take_sequence(), "queued_at": now, "summary": None})

    def _preflight(self, loaded: TestConfig, config: AgenticHILConfig) -> JsonObject | None:
        service = _AdmissionService(config)
        try:
            # A factory is what lets preflight accept a step on a probe other
            # than the bound one. Preflight never calls it.
            reactor = TestReactor(config, cast("AgenticHILToolService", service), service_factory=lambda _bound: cast("AgenticHILToolService", service))
            return reactor.preflight(loaded)
        except Exception as error:
            return {"summary": "The plan's preflight raised before it could answer.", "exception_type": type(error).__name__}
        finally:
            service.close()

    @staticmethod
    def _rejected(data: JsonObject, summary: str, now: float) -> None:
        data.update({"state": REJECTED, "summary": summary, "finished_at": now})

    # -- The tick ----------------------------------------------------------

    def tick(self) -> None:
        now = self._now()
        for data in self._jobs(ACTIVE_JOB_STATES):
            if data["state"] in ACTIVE_JOB_STATES:
                self._reconcile(data, now)
        for data in self._jobs({AWAITING_ARTIFACTS}):
            if now >= float(data["submitted_at"]) + self.settings.max_artifact_wait_s:
                data.update({
                    "state": EXPIRED,
                    "finished_at": now,
                    "error_type": "artifact_missing",
                    "summary": "The artifacts the plan names did not all arrive within max_artifact_wait_s, so the job never entered the queue.",
                })
        for data in self._jobs({AWAITING_ARTIFACTS, QUEUED}):
            if data.get("queue_deadline") is not None and now >= data["queue_deadline"]:
                data.update({"state": EXPIRED, "finished_at": now, "summary": "The job waited until its queue deadline without starting."})
        self._observe()
        self._dispatch(now)
        horizon = self.settings.retention_days * 86400
        for data in self._jobs(TERMINAL_JOB_STATES):
            if data.get("finished_at") is not None and now - data["finished_at"] >= horizon:
                self.store.retire(str(data["job_id"]))
        self.store.save()

    def next_due_at(self) -> float | None:
        """The earliest clock time at which a tick has something time-bound to do, or None."""
        now = self._now()
        due: list[float | None] = []
        for data in self.store.jobs.values():
            state = data["state"]
            if state in {AWAITING_ARTIFACTS, QUEUED}:
                due.append(data.get("queue_deadline"))
            if state == AWAITING_ARTIFACTS:
                due.append(float(data["submitted_at"]) + self.settings.max_artifact_wait_s)
            if state == QUEUED:
                aging = None if data.get("queued_at") is None else data["queued_at"] + self.settings.reserve_after_s
                due.extend(moment for moment in (data.get("retry_at"), data.get("hold_retry_at"), aging) if moment is not None and moment > now)
            if state in ACTIVE_JOB_STATES:
                if data.get("stop_requested_at") is not None:
                    due.append(None if data.get("terminated") else data["stop_requested_at"] + self.settings.stop_grace_s)
                elif data.get("dispatched_at") is not None:
                    due.append(data["dispatched_at"] + self.settings.max_job_runtime_s)
            if state in TERMINAL_JOB_STATES and data.get("finished_at") is not None:
                due.append(data["finished_at"] + self.settings.retention_days * 86400)
        moments = [float(moment) for moment in due if moment is not None]
        return min(moments) if moments else None

    def _reconcile(self, data: JsonObject, now: float) -> None:
        """Move one attempt on by what its worker says; never decide what the worker did."""
        handle = str(data["attempt_handle"])
        try:
            # Liveness first: a record read after the worker is seen gone is
            # the last one it wrote.
            gone = self.worker_source.is_gone(self.config, handle)
            record = self.worker_source.record(self.config, handle)
        except (ConfigError, OSError) as error:
            data["summary"] = f"The worker's record could not be read: {error}"
            return
        if isinstance(record, dict) and isinstance(record.get("pid"), int):
            data["worker_pid"] = record["pid"]
        run_state = record.get("state") if isinstance(record, dict) else None
        if gone:
            if isinstance(record, dict) and run_state in runlifecycle.TERMINAL_RUN_STATES and self._refused_at_start(data, record):
                self._requeue(data, now)
                return
            if isinstance(record, dict) and record.get("error_type") == "can_bus_incident" and record.get("incident_scope") == "bus":
                self._bus_incident(data, record, now)
            self._terminal(data, record if isinstance(record, dict) else None, now)
            return
        if run_state == runlifecycle.RUN_RUNNING:
            data["started"] = True
            if data["state"] == STARTING:
                data["state"] = RUNNING
        wanted = data.get("stop_reason")
        if wanted is None and data.get("owner") != self._instance:
            wanted = "restart"
        elif wanted is None and now - float(data["dispatched_at"]) > self.settings.max_job_runtime_s:
            wanted = RUNTIME_EXCEEDED
        if data.get("stop_requested_at") is None:
            if wanted is not None:
                self._request_stop(data, wanted, now)
        elif not data.get("terminated") and now >= data["stop_requested_at"] + self.settings.stop_grace_s:
            # Once: what ends the job is the worker being seen gone, not this.
            data["terminated"] = True
            try:
                self.worker_source.terminate(self.config, handle)
            except Exception as error:
                data["summary"] = f"The worker did not stop within the grace period and could not be ended: {error}"
            else:
                data["summary"] = "The worker did not stop within the grace period and was ended, so its cleanup is unconfirmed."

    @staticmethod
    def _refused_at_start(data: JsonObject, record: JsonObject) -> bool:
        """Whether the worker ended because a device it declared was held, before any step ran."""
        return data["state"] == STARTING and not data.get("started") and record.get("error_type") == "device_busy" and record.get("failed_step") is None

    def _requeue(self, data: JsonObject, now: float) -> None:
        """Back to the place the job had: its sequence, queue time and deadline are its own."""
        data.update(
            {
                "state": QUEUED,
                "retry_at": now + self.settings.retry_interval_s,
                "hold_retry_at": None,
                "dispatched_at": None,
                "stop_reason": None,
                "stop_requested_at": None,
                "terminated": False,
                "summary": "The worker found a device this plan declares held and ran no step; the job waits in its place and tries again.",
            }
        )

    def _request_stop(self, data: JsonObject, reason: str, now: float) -> None:
        """Ask the attempt's worker to stop, and say why: a withdrawal or a runtime cap names the outcome."""
        data["stop_reason"] = reason
        if data["state"] in {STARTING, RUNNING}:
            data["state"] = WITHDRAWING if reason == WITHDRAWN else STOPPING
        try:
            self.worker_source.stop(self.config, str(data["attempt_handle"]))
        except (ConfigError, OSError) as error:
            # Asked again on the next tick.
            data["summary"] = f"The stop request could not be written: {error}"
            return
        data["stop_requested_at"] = now

    def _bus_incident(self, data: JsonObject, record: JsonObject, now: float) -> None:
        """A bus every participant shares failed: the other jobs on it stop, and all of them keep the cause."""
        cause = {"error_type": record["error_type"], "incident_scope": record.get("incident_scope"), "bus": record.get("bus"), "summary": record.get("summary")}
        data["cause"] = cause
        bus = cause["bus"]
        if not isinstance(bus, str) or not bus:
            return
        for other in self._jobs(ACTIVE_JOB_STATES):
            if other is data or not any(key == bus or key.startswith(bus + "#") for key in other["lock_keys"]):
                continue
            other["cause"] = copy.deepcopy(cause)
            if other.get("stop_requested_at") is None:
                self._request_stop(other, "bus_incident", now)

    def _terminal(self, data: JsonObject, record: JsonObject | None, now: float) -> None:
        """The outcome of an attempt whose worker is gone, kept with the worker's own type.

        An attempt an earlier scheduler started is worker_gone whatever its
        worker did afterwards, stopped when asked, finished or died: nobody
        supervised it to its end, and its run record is all that says what it
        reached. A withdrawal or a runtime cap names the outcome when the
        worker honoured the stop, and also when the worker had to be ended
        after the grace period, which leaves its cleanup unconfirmed.
        """
        reason = data.get("stop_reason")
        run_state = record.get("state") if record is not None else None
        ended = run_state in runlifecycle.TERMINAL_RUN_STATES
        stopped_as_asked = run_state == runlifecycle.RUN_STOPPED or (ended and not data.get("started")) or (not ended and data.get("terminated"))
        if data.get("owner") != self._instance:
            state = WORKER_GONE
        elif reason in {WITHDRAWN, RUNTIME_EXCEEDED} and stopped_as_asked:
            state = str(reason)
        elif record is None:
            state = FAILED_TO_START
        elif not ended:
            state = WORKER_GONE
        else:
            state = str(run_state)
        data["state"] = state
        data["finished_at"] = now
        # The run's own verdict, as its worker recorded it: what a job's `ok` is read from.
        data["run_ok"] = record is not None and record.get("run_ok") is True
        if record is not None and "error_type" in record:
            data["error_type"] = record["error_type"]
        if state == RUNTIME_EXCEEDED:
            data["error_type"] = "plan_runtime_exceeded"
        elif state == WORKER_GONE:
            data["error_type"] = "run_worker_gone"
        elif state == FAILED_TO_START:
            data["error_type"] = "worker_start_failed"
        if state == FINISHED and record is not None and record.get("run_ok") is True and data.get("dispatched_at") is not None:
            self.store.record_duration(data.get("plan_digest"), now - float(data["dispatched_at"]))
        self._write_job_report(data, record)

    def _write_job_report(self, data: JsonObject, record: JsonObject | None) -> None:
        """The job's own account of its attempt, beside the run's report rather than instead of it."""
        handle = data.get("attempt_handle")
        directory = self.store.directory(str(data["job_id"]))
        canonical: Path | None = None
        if isinstance(handle, str):
            # The run reported under the configuration it ran under.
            try:
                config = self.workspace_preparer.config_for(self.store.job(data))
                canonical = canonical_run_report_path(config, handle)
            except (ConfigError, OSError):
                config = self.config
                canonical = None
            # The worker's own output, kept with the job: the runs directory
            # prunes it with the run's record.
            with suppress(ConfigError, OSError):
                atomic_write_bytes(directory / "logs" / WORKER_LOG_NAME, safe_read_bytes(runlifecycle.worker_log_path(config, handle)))
        canonical_report = _read_json(canonical) if canonical is not None else None
        report = {
            "job_id": data["job_id"],
            "state": data["state"],
            "started": bool(data.get("started")),
            "attempt_handle": handle,
            "plan_digest": data.get("plan_digest"),
            "summary": data.get("summary"),
            "cause": copy.deepcopy(data.get("cause")),
            "run_record": copy.deepcopy(record),
            "canonical_report_path": None if canonical_report is None else str(canonical),
            "canonical_report_missing": canonical_report is None,
            "cleanup_confirmed": canonical_report is not None and canonical_report.get("cleanup_ok") is True,
            "written_at": utc_now_iso(),
        }
        path = directory / "report" / JOB_REPORT_NAME
        try:
            atomic_write_text(path, json.dumps(report, indent=2, sort_keys=True) + "\n")
        except (ConfigError, OSError):
            return
        data["report_path"] = str(path)
        data["canonical_report_path"] = report["canonical_report_path"]

    def _observe(self) -> None:
        """Read what the devices queued jobs wait for say about themselves: who holds them, and what incident stands.

        A device a job of this queue occupies is not read. The queue waits for
        it whatever its records say, and they are the records that job's worker
        is writing: Windows refuses a rename over a file somebody has open, so a
        poll here could make the worker's own lease release fail and quarantine
        a device it had cleanly handed back.
        """
        occupied = {key for data in self._jobs(ACTIVE_JOB_STATES) for key in data["lock_keys"]}
        keys = sorted({key for data in self._jobs({QUEUED}) for key in data["lock_keys"]} - occupied)
        # The hold a worker this queue saw end left behind names that worker,
        # and the worker is gone: the record is all that is left of it.
        dead = {(key, data["worker_pid"]) for data in self._jobs({WORKER_GONE}) if isinstance(data.get("worker_pid"), int) for key in data["lock_keys"]}
        self._holders = {}
        for key in keys:
            record = self.holder_source.holder(key)
            if not isinstance(record, dict) or record.get("state") != "held":
                continue
            owner = _record_owner(record)
            if owner.get("host") == self._host and (key, owner.get("pid")) in dead:
                continue
            self._holders[key] = record
        standing = self.incident_source.standing(keys) if keys else {}
        self._incidents = {key: dict(reason) for key, reason in standing.items() if key in keys}

    def _dispatch(self, now: float) -> None:
        occupied = {key for data in self._jobs(ACTIVE_JOB_STATES) for key in data["lock_keys"]}
        strict = self.settings.reserve_after_s == 0
        claimed: dict[str, str] = {}
        for data in self._jobs({QUEUED}):
            keys = data["lock_keys"]
            if not self._blocked(data, keys, occupied, claimed, now):
                self._start(data, now)
                if data["state"] in ACTIVE_JOB_STATES:
                    occupied.update(keys)
                continue
            if strict:
                # Nothing younger starts while the oldest waiting job cannot.
                break
            if now - float(data["queued_at"]) >= self.settings.reserve_after_s:
                for key in keys:
                    claimed.setdefault(key, str(data["job_id"]))

    def _blocked(self, data: JsonObject, keys: list[str], occupied: set[str], claimed: Mapping[str, str], now: float) -> bool:
        if data.get("retry_at") is not None and now < data["retry_at"]:
            return True
        if any(key in occupied or key in self._incidents or key in self.drained or claimed.get(key, data["job_id"]) != data["job_id"] for key in keys):
            return True
        if not any(key in self._holders for key in keys):
            data["hold_retry_at"] = None
            return False
        # Held by a record that may be stale: wait, then let the worker meet
        # the lock itself.
        if data.get("hold_retry_at") is None:
            data["hold_retry_at"] = now + self.settings.retry_interval_s
        return now < data["hold_retry_at"]

    def _start(self, data: JsonObject, now: float) -> None:
        handle = self.worker_source.new_handle()
        data.update(
            {
                "state": STARTING,
                "attempt_handle": handle,
                "attempts": int(data.get("attempts") or 0) + 1,
                "dispatched_at": now,
                "owner": self._instance,
                "retry_at": None,
                "hold_retry_at": None,
                "stop_reason": None,
                "stop_requested_at": None,
                "terminated": False,
                "worker_pid": None,
                "summary": None,
            }
        )
        # Persisted before the worker exists: a scheduler that dies between the
        # two leaves an attempt to reconcile, never a worker nobody knows of.
        self.store.save()
        try:
            config, plan = self.workspace_preparer.prepare(self.store.job(data))
            self.worker_source.start(config, handle, plan, wait_s=0)
        except Exception as error:
            data["summary"] = f"The worker for this attempt could not be started: {error}"
            self._terminal(data, None, now)

    # -- Answers -----------------------------------------------------------

    def status(self, job_id: str) -> JsonObject:
        data = self.store.get(job_id)
        return {
            "job_id": data["job_id"],
            "state": data["state"],
            "started": bool(data.get("started")),
            "sequence": data.get("sequence"),
            "submitted_at": data.get("submitted_at"),
            "queued_at": data.get("queued_at"),
            "queue_deadline": data.get("queue_deadline"),
            "submitter_label": data.get("submitter_label"),
            # Nothing authenticates the label: it is the caller's own word.
            "submitter_label_source": "caller_supplied",
            "plan_name": data.get("plan_name"),
            "plan_digest": data.get("plan_digest"),
            "missing_artifacts": list(data.get("missing_artifacts") or []),
            "attempt_handle": data.get("attempt_handle"),
            "attempts": int(data.get("attempts") or 0),
            "error_type": copy.deepcopy(data.get("error_type")),
            "summary": data.get("summary"),
            "refusal_field": data.get("refusal_field"),
            "cause": copy.deepcopy(data.get("cause")),
            "estimate": {
                "lower_bound_s": data.get("lower_bound_s"),
                "history_samples": len(self.store.samples(data.get("plan_digest"))),
                "duration_s": self._expected_s(data),
            },
            "devices": self._device_views(data),
            "report_path": data.get("report_path"),
            "canonical_report_path": data.get("canonical_report_path"),
        }

    def _device_views(self, data: JsonObject) -> JsonObject:
        now = self._now()
        queued = self._jobs({QUEUED})
        claimed: dict[str, str] = {}
        for waiting in queued:
            if now - float(waiting["queued_at"]) >= self.settings.reserve_after_s:
                for key in waiting["lock_keys"]:
                    claimed.setdefault(key, str(waiting["job_id"]))
        occupants = {key: active for active in self._jobs(ACTIVE_JOB_STATES) for key in active["lock_keys"]}
        views: JsonObject = {}
        for key in data.get("lock_keys") or []:
            line = [waiting for waiting in queued if key in waiting["lock_keys"]]
            holder = self._holders.get(key)
            incident = self._incidents.get(key)
            occupant = occupants.get(key)
            free_at: float | None = None
            unknown: str | None = None
            if occupant is not None:
                expected = self._expected_s(occupant)
                free_at = None if expected is None else float(occupant["dispatched_at"]) + expected
                unknown = None if expected is not None else "The job holding this device has no duration estimate."
            elif holder is not None:
                unknown = "The device is held outside this queue, and nothing says for how long."
            elif incident is not None:
                unknown = "A standing incident holds the device until it is recovered."
            views[resource_digest(key)] = {
                "position": next((index for index, waiting in enumerate(line, start=1) if waiting is data), None),
                "reserved_for": claimed.get(key),
                "occupied_by": None if occupant is None else occupant["job_id"],
                "holder": None if holder is None else _public_holder(holder),
                "held_since": None if holder is None else holder.get("acquired_at"),
                "estimated_free_at": free_at,
                "estimate_unknown_reason": unknown,
                "blocked_by_incident": copy.deepcopy(incident),
            }
        return views

    def _expected_s(self, data: JsonObject) -> float | None:
        """How long the plan is expected to take: its finished history, never below its lower bound."""
        lower = data.get("lower_bound_s")
        samples = self.store.samples(data.get("plan_digest"))
        if not samples:
            return None if lower is None else float(lower)
        mean = sum(samples) / len(samples)
        return mean if lower is None else max(float(lower), mean)

    # -- Helpers -----------------------------------------------------------

    def _now(self) -> float:
        return float(self.clock())

    def _jobs(self, states: frozenset[str] | set[str]) -> list[JsonObject]:
        return sorted((data for data in self.store.jobs.values() if data["state"] in states), key=_order)

    def _open_jobs(self, label: str) -> int:
        return sum(1 for data in self.store.jobs.values() if data.get("submitter_label") == label and data["state"] in QUOTA_JOB_STATES)

    @staticmethod
    def _present(config: AgenticHILConfig, path: str) -> bool:
        """Whether the workspace the job runs in already holds the artifact, so it need not be provided."""
        try:
            return configured_work_path(config, path).is_file()
        except (OSError, ValueError):
            return False

    def _queue_bound(self, max_queue_s: float | None) -> float:
        if max_queue_s is None:
            return self.settings.max_queue_s
        if isinstance(max_queue_s, bool) or not isinstance(max_queue_s, (int, float)) or not math.isfinite(max_queue_s) or max_queue_s <= 0:
            raise invalid_input("`max_queue_s` must be a positive finite number of seconds.", {"field": "max_queue_s"})
        return min(float(max_queue_s), self.settings.max_queue_s)

    @staticmethod
    def _require_submitter(data: JsonObject, submitter_label: str) -> None:
        if not isinstance(submitter_label, str) or submitter_label != data.get("submitter_label"):
            raise PermissionError("Only the submitter that submitted a job may withdraw it or provide its artifacts.")
