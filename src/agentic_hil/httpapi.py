"""Integration seams for the loopback job transport.

Configuration and job execution are supplied by the other bench-server parts.
"""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Protocol

from agentic_hil.config import ConfigError
from agentic_hil.types import AgenticHILConfig, JsonObject


@dataclass(frozen=True)
class ServerSettings:
    host: str = "127.0.0.1"
    port: int = 8765
    hostnames: tuple[str, ...] = ()
    allowed_origins: tuple[str, ...] = ()
    max_upload_mb: int = 64
    max_artifact_wait_s: float = 600.0
    prefer_wait_cap_s: float = 60.0
    sse_keepalive_s: float = 15.0
    retry_after_s: int = 1


@dataclass(frozen=True)
class JobStatus:
    revision: int
    status: JsonObject


@dataclass(frozen=True)
class JobEvent:
    event_id: str
    status: JsonObject


@dataclass(frozen=True)
class JobEvents:
    events: tuple[JobEvent, ...]
    complete: bool


@dataclass(frozen=True)
class JobContent:
    body: bytes
    media_type: str
    filename: str | None = None


@dataclass(frozen=True)
class ByteRange:
    first: int | None
    last: int | None


@dataclass(frozen=True)
class LogRead:
    body: bytes
    first: int
    total_size: int
    complete: bool


class JobServiceError(ConfigError):
    """A service failure, with validation_error in details for plan rejection.

    The transport preserves to_dict() and maps the validation marker before
    mapping error_type. Both submit and check_plan raise this for a refused
    plan, without creating a job. An unrecognised type without that marker is
    a server error, exposing an integration mismatch.
    """

    def __init__(self, error_type: str, summary: str, details: JsonObject | None = None) -> None:
        super().__init__(error_type, summary, details)


class JobService(Protocol):
    """The store owns validation, artifacts, tokens, scheduling and execution.

    Status dictionaries pass through unchanged. Waits must be atomic with
    revision/event observation and bounded by timeout_s. A withdrawal result
    carries started, distinguishing immediate withdrawal from cooperative stop.
    """

    def submit(
        self, plan: bytes, artifacts: Mapping[str, bytes], *,
        idempotency_key: str | None, submitted_by: str | None,
    ) -> JsonObject: ...

    def check_plan(
        self, plan: bytes, artifacts: Mapping[str, bytes], *, submitted_by: str | None,
    ) -> JsonObject: ...

    def upload_artifact(
        self, job_id: str, path: str, body: bytes, *, job_token: str,
    ) -> JsonObject: ...

    def status(self, job_id: str) -> JobStatus: ...

    def wait_for_revision(
        self, job_id: str, *, after_revision: int, timeout_s: float,
    ) -> JobStatus | None: ...

    def events_after(
        self, job_id: str, *, last_event_id: str | None, timeout_s: float,
    ) -> JobEvents: ...

    def withdraw(self, job_id: str, *, job_token: str) -> JsonObject: ...

    def report(self, job_id: str, *, job_token: str) -> JsonObject: ...

    def junit(self, job_id: str, *, job_token: str) -> bytes: ...

    def evidence(self, job_id: str, *, job_token: str) -> JobContent: ...

    def log(
        self, job_id: str, n: int, *, job_token: str,
        byte_range: ByteRange | None, follow: bool, timeout_s: float,
    ) -> LogRead: ...


class BenchReads(Protocol):
    """The content of the open read endpoints, which #889 supplies.

    The five tools that also exist locally answer in the local tools' field
    names. /mcp passes every answer through unchanged. Each method may raise
    JobServiceError.
    """

    def bench_describe(self) -> JsonObject: ...

    def bench_queue_status(self) -> JsonObject: ...

    def bench_jobs_list(
        self, *, state: str | None, device: str | None, user: str | None, since: str | None,
    ) -> JsonObject: ...

    def debugger_probes_list(self) -> JsonObject: ...

    def debugger_info(self) -> JsonObject: ...

    def com_ports_list(self) -> JsonObject: ...

    def can_buses_list(self) -> JsonObject: ...

    def hardware_lease_status(self) -> JsonObject: ...


def load_server_settings(config: AgenticHILConfig) -> ServerSettings | None:
    """Recognise settings on already-loaded configuration, pending #886.

    This adapter deliberately examines no block contents. The configuration
    integration replaces it; the current schema still refuses a server key.
    """
    return ServerSettings() if getattr(config, "server", None) is not None else None


def open_job_service(
    config: AgenticHILConfig, settings: ServerSettings,
) -> AbstractContextManager[JobService]:
    """Refuse until #887 supplies its context-managed store implementation.

    Settings, including max_artifact_wait_s, belong to that implementation.
    No job directory or worker is created by this transport integration seam.
    """
    raise JobServiceError("job_store_unavailable", "The job store is missing; serve cannot listen without a job service.")
