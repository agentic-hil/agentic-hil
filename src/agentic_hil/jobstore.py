"""The bench job queue's durable half: its settings, its jobs and their files.

A job is a directory under ``job_root`` and an entry in one snapshot beside
those directories. The snapshot holds every job the queue still knows, the
next sequence number and the duration history of each plan digest, and it is
replaced whole on every change. A scheduler that starts again therefore reads
the queue exactly as the last change left it: the same sequences, the same
queue times, and the same attempts still to reconcile.

Nothing here decides who runs. That is the scheduler's; this module only keeps
what it decided, refuses a value it cannot keep, and removes what retention
says is old.
"""

from __future__ import annotations

import json
import math
import re
import secrets
import shutil
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from .config import ConfigError, atomic_write_text, safe_directory, safe_read_text
from .types import JsonObject

if TYPE_CHECKING:
    from .test_reactor import TestStep

JOB_STORE_VERSION = 1
SNAPSHOT_NAME = "queue.json"
# A job id is minted here and never taken from a caller, so this pattern is the
# whole of what a caller may name: it becomes a directory name, and anything
# else is refused rather than turned into some neighbouring path.
JOB_ID_PATTERN = re.compile(r"^job-[0-9a-f]{16}$")
# What every job directory holds, created with the job so a reader finds the
# same layout whatever state the job reached.
JOB_DIRECTORIES = ("plan", "artifacts", "workspace", "report", "logs")
# How many finished durations of one plan digest the estimate is made from.
HISTORY_SAMPLES_KEPT = 20

AWAITING_ARTIFACTS = "awaiting_artifacts"
QUEUED = "queued"
STARTING = "starting"
RUNNING = "running"
WITHDRAWING = "withdrawing"
STOPPING = "stopping"
FINISHED = "finished"
STOPPED = "stopped"
WITHDRAWN = "withdrawn"
EXPIRED = "expired"
REJECTED = "rejected"
RUNTIME_EXCEEDED = "runtime_exceeded"
WORKER_GONE = "worker_gone"
FAILED_TO_START = "start_failed"
# An attempt exists in these: a worker was asked for and has not been seen to end.
ACTIVE_JOB_STATES = frozenset({STARTING, RUNNING, WITHDRAWING, STOPPING})
# Nothing more happens to a job in these except retention.
TERMINAL_JOB_STATES = frozenset({FINISHED, STOPPED, WITHDRAWN, EXPIRED, REJECTED, RUNTIME_EXCEEDED, WORKER_GONE, FAILED_TO_START})

_DURATION_SETTINGS = ("reserve_after_s", "max_queue_s", "max_job_runtime_s", "stop_grace_s", "retry_interval_s")
_POSITIVE_DURATION_SETTINGS = ("max_queue_s", "max_job_runtime_s", "stop_grace_s", "retry_interval_s")


def invalid_input(summary: str, details: JsonObject | None = None) -> ConfigError:
    """The refusal of a value the queue was handed: a submission, a job id, an artifact, a setting or a stored queue."""
    return ConfigError("job_input_invalid", summary, details or {})


@dataclass(kw_only=True)
class SchedulerSettings:
    """The queue's policy. Every duration is in seconds of the scheduler's clock.

    ``reserve_after_s`` is how long a job waits before the devices it needs are
    claimed for it, and zero makes the queue strictly first in, first out.
    ``max_queue_s`` caps how long any job may wait, ``max_job_runtime_s`` how
    long an attempt may run, and ``stop_grace_s`` how long a stopped worker has
    before it is ended. ``retry_interval_s`` is how long a job waits before it
    tries again past a holder record, or after a worker found its devices held.
    """

    job_root: Path
    reserve_after_s: float = 600.0
    max_queue_s: float = 86400.0
    max_job_runtime_s: float = 900.0
    max_queued_jobs_per_user: int = 5
    retention_days: int = 14
    stop_grace_s: float = 10.0
    retry_interval_s: float = 1.0

    def __post_init__(self) -> None:
        self.job_root = Path(self.job_root)
        for name in _DURATION_SETTINGS:
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise invalid_input(f"The scheduler setting `{name}` must be a finite number of seconds.", {"field": name, "value": str(value)})
            setattr(self, name, float(value))
        if self.reserve_after_s < 0:
            raise invalid_input("The scheduler setting `reserve_after_s` must not be negative.", {"field": "reserve_after_s"})
        for name in _POSITIVE_DURATION_SETTINGS:
            if getattr(self, name) <= 0:
                raise invalid_input(f"The scheduler setting `{name}` must be greater than zero.", {"field": name})
        for name, least in (("max_queued_jobs_per_user", 1), ("retention_days", 0)):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < least:
                raise invalid_input(f"The scheduler setting `{name}` must be a whole number of at least {least}.", {"field": name, "value": str(value)})


@dataclass
class Job:
    job_id: str
    directory: Path
    plan_path: Path
    # Each artifact by the path the plan names it under, to the copy staged in the job's own directory.
    artifacts: dict[str, Path] = field(default_factory=dict)


def plan_lower_bound_s(steps: Sequence[TestStep]) -> float:
    """The least time a plan can take: its delays, and each repeat at the fewest iterations it allows.

    A repeat bounded by `count` alone runs its body that many times. One bounded
    by `duration_s` alone starts iterations until the time has passed and never
    cuts one in half, so it takes at least the longer of that time and one
    body. With both, the first bound reached ends it, which is the smaller of
    the two. Any other step is counted as taking no time: it may answer at once.
    """
    total = 0.0
    for step in steps:
        if step.action == "delay":
            total += float(step.arguments.get("duration_ms", 0)) / 1000.0
        elif step.action == "repeat":
            body = plan_lower_bound_s(step.steps)
            count = step.arguments.get("count")
            duration = step.arguments.get("duration_s")
            bounds = [bound for bound in (None if count is None else count * body, None if duration is None else max(float(duration), body)) if bound is not None]
            total += min(bounds) if bounds else body
    return total


class JobStore:
    """Every job the queue knows, kept in one snapshot under ``root``."""

    def __init__(self, root: Path):
        self.root = safe_directory(root)
        self.path = self.root / SNAPSHOT_NAME
        self.jobs: dict[str, JsonObject] = {}
        self.history: dict[str, list[float]] = {}
        self.next_sequence = 1
        self._written: str | None = None
        self._load()

    def _load(self) -> None:
        try:
            text = safe_read_text(self.path)
        except FileNotFoundError:
            return
        except (ConfigError, OSError, UnicodeDecodeError) as error:
            raise invalid_input("The bench job queue's snapshot could not be read.", {"path": str(self.path), "backend_error": str(error)}) from error
        try:
            value = json.loads(text)
        except ValueError as error:
            raise invalid_input("The bench job queue's snapshot is not valid JSON.", {"path": str(self.path), "backend_error": str(error)}) from error
        jobs = value.get("jobs") if isinstance(value, dict) else None
        history = value.get("history") if isinstance(value, dict) else None
        next_sequence = value.get("next_sequence") if isinstance(value, dict) else None
        readable = (
            isinstance(value, dict)
            and value.get("version") == JOB_STORE_VERSION
            and isinstance(jobs, dict)
            and all(isinstance(job_id, str) and JOB_ID_PATTERN.match(job_id) and isinstance(job, dict) for job_id, job in jobs.items())
            and isinstance(history, dict)
            and all(isinstance(samples, list) and all(isinstance(sample, (int, float)) for sample in samples) for samples in history.values())
            and isinstance(next_sequence, int)
            and not isinstance(next_sequence, bool)
            and next_sequence >= 1
        )
        if not readable:
            raise invalid_input("The bench job queue's snapshot is not one this version can read.", {"path": str(self.path)})
        assert isinstance(jobs, dict) and isinstance(history, dict) and isinstance(next_sequence, int)
        self.jobs = jobs
        self.history = {digest: [float(sample) for sample in samples] for digest, samples in history.items()}
        self.next_sequence = next_sequence
        self._written = text

    def save(self) -> None:
        """Replace the snapshot whole, and only when something in it changed."""
        text = json.dumps({"version": JOB_STORE_VERSION, "next_sequence": self.next_sequence, "jobs": self.jobs, "history": self.history}, indent=2, sort_keys=True) + "\n"
        if text == self._written:
            return
        atomic_write_text(self.path, text)
        self._written = text

    def take_sequence(self) -> int:
        """The next place in the queue. Only a job that enters the queue takes one."""
        sequence = self.next_sequence
        self.next_sequence += 1
        return sequence

    def directory(self, job_id: str) -> Path:
        return self.root / validated_job_id(job_id)

    def create(self, plan_name: str) -> Job:
        """A new job's id and directory, with every directory a job holds."""
        name = Path(plan_name).name
        if name in {"", ".", ".."}:
            name = "plan.yaml"
        while True:
            job_id = f"job-{secrets.token_hex(8)}"
            if job_id not in self.jobs and not (self.root / job_id).exists():
                break
        directory = safe_directory(self.root / job_id)
        for child in JOB_DIRECTORIES:
            safe_directory(directory / child)
        return Job(job_id, directory, directory / "plan" / name)

    def get(self, job_id: str) -> JsonObject:
        data = self.jobs.get(validated_job_id(job_id))
        if data is None:
            raise invalid_input("This bench job queue has no job under that id.", {"field": "job_id", "job_id": job_id})
        return data

    def job(self, data: JsonObject) -> Job:
        artifacts = data.get("artifacts") or {}
        return Job(
            str(data["job_id"]),
            self.directory(str(data["job_id"])),
            Path(str(data["plan_path"])),
            {name: Path(str(staged["path"])) for name, staged in sorted(artifacts.items())},
        )

    def samples(self, plan_digest: str | None) -> list[float]:
        return list(self.history.get(plan_digest, ())) if plan_digest else []

    def record_duration(self, plan_digest: str | None, seconds: float) -> None:
        if not plan_digest:
            return
        samples = [*self.history.get(plan_digest, []), max(0.0, float(seconds))]
        self.history[plan_digest] = samples[-HISTORY_SAMPLES_KEPT:]

    def retire(self, job_id: str) -> bool:
        """Remove a job's directory and its entry, or leave both for the next pass."""
        directory = self.directory(job_id)
        try:
            if directory.is_symlink():
                directory.unlink()
            elif directory.exists():
                shutil.rmtree(directory)
        except OSError:
            return False
        del self.jobs[job_id]
        return True


def validated_job_id(job_id: str) -> str:
    if not isinstance(job_id, str) or JOB_ID_PATTERN.match(job_id) is None:
        raise invalid_input("A job id is the value submission returned: `job-` followed by sixteen hexadecimal digits.", {"field": "job_id", "value": str(job_id)[:64]})
    return job_id
