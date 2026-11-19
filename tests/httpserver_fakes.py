"""Scripted JobService replies and real-socket helpers, without a scheduler."""

from __future__ import annotations

import json
from collections import defaultdict, deque
from collections.abc import Callable, Iterator, Mapping
from concurrent.futures import Future
from contextlib import contextmanager
from dataclasses import dataclass
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from threading import Event, Lock, Thread
from typing import Any

from support import scaled_time_bound

from agentic_hil import httpserver
from agentic_hil.httpapi import (
    ByteRange,
    JobContent,
    JobEvents,
    JobServiceError,
    JobStatus,
    LogRead,
    ServerSettings,
)
from agentic_hil.types import JsonObject

PLAN = b"version: 1\nsteps:\n  - action: flash\n    image_path: build/app.elf\n"
IMAGE = b"\x7fELF\x00fixture firmware\xff\r\n"
TOKEN = "test-job-token"
JOB_ID = "job-example"
JOB_PATH = f"/v1/jobs/{JOB_ID}"
ARTIFACT_PATH = f"{JOB_PATH}/artifacts/build/app.elf"


@dataclass(frozen=True)
class Call:
    method: str
    args: tuple[Any, ...]
    kwargs: dict[str, Any]


class ScriptedJobService:
    """Return only planted replies, recording the exact Protocol calls."""

    def __init__(self) -> None:
        self.calls: list[Call] = []
        self.published: list[str] = []
        self.scripts: dict[str, deque[Any]] = defaultdict(deque)
        self.waiters: list[WaitReply] = []
        self.lock = Lock()

    def script(self, method: str, *replies: Any) -> None:
        with self.lock:
            self.scripts[method].extend(replies)
            self.waiters.extend(reply for reply in replies if isinstance(reply, WaitReply))

    def _call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        call = Call(method, args, kwargs)
        with self.lock:
            self.calls.append(call)
            assert self.scripts[method], f"Unexpected JobService call: {call}"
            reply = self.scripts[method].popleft()
        if isinstance(reply, BaseException):
            raise reply
        if callable(reply):
            reply = reply(call)
        if method == "submit":
            self.published.append(reply["job_id"])
        return reply

    def release_waiters(self) -> None:
        for waiter in self.waiters:
            if waiter.entered.is_set():
                waiter.release.set()

    def submit(
        self, plan: bytes, artifacts: Mapping[str, bytes], *,
        idempotency_key: str | None, submitted_by: str | None,
    ) -> JsonObject:
        return self._call("submit", plan, dict(artifacts), idempotency_key=idempotency_key, submitted_by=submitted_by)

    def check_plan(
        self, plan: bytes, artifacts: Mapping[str, bytes], *, submitted_by: str | None,
    ) -> JsonObject:
        return self._call("check_plan", plan, dict(artifacts), submitted_by=submitted_by)

    def upload_artifact(self, job_id: str, path: str, body: bytes, *, job_token: str) -> JsonObject:
        return self._call("upload_artifact", job_id, path, body, job_token=job_token)

    def status(self, job_id: str) -> JobStatus:
        return self._call("status", job_id)

    def wait_for_revision(self, job_id: str, *, after_revision: int, timeout_s: float) -> JobStatus | None:
        return self._call("wait_for_revision", job_id, after_revision=after_revision, timeout_s=timeout_s)

    def events_after(self, job_id: str, *, last_event_id: str | None, timeout_s: float) -> JobEvents:
        return self._call("events_after", job_id, last_event_id=last_event_id, timeout_s=timeout_s)

    def withdraw(self, job_id: str, *, job_token: str) -> JsonObject:
        return self._call("withdraw", job_id, job_token=job_token)

    def report(self, job_id: str, *, job_token: str) -> JsonObject:
        return self._call("report", job_id, job_token=job_token)

    def junit(self, job_id: str, *, job_token: str) -> bytes:
        return self._call("junit", job_id, job_token=job_token)

    def evidence(self, job_id: str, *, job_token: str) -> JobContent:
        return self._call("evidence", job_id, job_token=job_token)

    def log(
        self, job_id: str, n: int, *, job_token: str,
        byte_range: ByteRange | None, follow: bool, timeout_s: float,
    ) -> LogRead:
        return self._call("log", job_id, n, job_token=job_token, byte_range=byte_range, follow=follow, timeout_s=timeout_s)


class WaitReply:
    """A test publishes a reply only after the request is inside its wait."""

    def __init__(self) -> None:
        self.entered = Event()
        self.release = Event()
        self.reply: Any = None

    def __call__(self, call: Call) -> Any:
        self.entered.set()
        assert self.release.wait(scaled_time_bound(1)), "Test did not publish the scripted change before the wait bound"
        return self.reply

    def publish(self, reply: Any) -> None:
        assert self.entered.wait(scaled_time_bound(10)), "Request never entered the service wait"
        self.reply = reply
        self.release.set()


@dataclass(frozen=True)
class Response:
    status: int
    headers: dict[str, str]
    body: bytes

    def json(self) -> JsonObject:
        value = json.loads(self.body)
        assert isinstance(value, dict)
        return value


def request(
    server: ThreadingHTTPServer, method: str, path: str, *,
    body: bytes | None = None, headers: Mapping[str, str] | None = None,
    withhold_body: bool = False,
) -> Response:
    connection = HTTPConnection("127.0.0.1", server.server_port, timeout=scaled_time_bound(10))
    try:
        if withhold_body:
            connection.putrequest(method, path, skip_host=True)
            combined = {"Host": f"127.0.0.1:{server.server_port}", **(headers or {})}
            combined["Content-Length"] = str(len(body or b""))
            for name, value in combined.items():
                connection.putheader(name, value)
            connection.endheaders()
        else:
            connection.request(method, path, body=body, headers=dict(headers or {}))
        response = connection.getresponse()
        return Response(response.status, {key.lower(): value for key, value in response.getheaders()}, response.read())
    finally:
        connection.close()


@contextmanager
def background_request(
    server: ThreadingHTTPServer, service: ScriptedJobService, method: str, path: str, **kwargs: Any,
) -> Iterator[Future[Response]]:
    future: Future[Response] = Future()

    def work() -> None:
        try:
            future.set_result(request(server, method, path, **kwargs))
        except BaseException as error:
            future.set_exception(error)

    worker = Thread(target=work, daemon=True)
    worker.start()
    try:
        yield future
    finally:
        service.release_waiters()
        worker.join(scaled_time_bound(15))
        assert not worker.is_alive(), "HTTP client thread did not finish"


def short_settings(**overrides: Any) -> ServerSettings:
    return ServerSettings(**{
        "port": 0,
        "prefer_wait_cap_s": scaled_time_bound(1),
        "sse_keepalive_s": scaled_time_bound(1),
        **overrides,
    })


@contextmanager
def running_http(service: ScriptedJobService, settings: ServerSettings | None = None) -> Iterator[ThreadingHTTPServer]:
    # Deliberately called here, when a test enters the context, never in setup.
    server = httpserver.create_http_server(settings or short_settings(), service)
    worker = Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
    worker.start()
    try:
        yield server
    finally:
        service.release_waiters()
        stopper = Thread(target=server.shutdown, daemon=True)
        stopper.start()
        stopper.join(scaled_time_bound(15))
        server.server_close()
        worker.join(scaled_time_bound(15))
        assert not stopper.is_alive(), "HTTP shutdown did not finish"
        assert not worker.is_alive(), "HTTP server thread did not finish"


def multipart() -> tuple[bytes, str]:
    boundary = "fixture-submission-boundary"
    body = (
        f'--{boundary}\r\nContent-Disposition: form-data; name="plan"; filename="testconfig.yaml"\r\n'
        "Content-Type: application/yaml\r\n\r\n"
    ).encode() + PLAN + (
        f'\r\n--{boundary}\r\nContent-Disposition: form-data; name="build/app.elf"; filename="app.elf"\r\n'
        "Content-Type: application/octet-stream\r\n\r\n"
    ).encode() + IMAGE + f"\r\n--{boundary}--\r\n".encode()
    return body, f"multipart/form-data; boundary={boundary}"


def submission(state: str = "queued") -> JsonObject:
    return {
        "job_id": JOB_ID, "job_token": TOKEN, "state": state,
        "missing_artifacts": ["build/app.elf"] if state == "awaiting_artifacts" else [],
        "queue_position": None if state == "awaiting_artifacts" else 1, "waiting_for": [],
    }


def service_error(error_type: str, details: JsonObject | None = None) -> JobServiceError:
    return JobServiceError(error_type, f"Scripted {error_type}.", {"likely_causes": ["Recorded service refusal."], **(details or {})})


def no_cors(response: Response) -> None:
    assert not any(name.startswith("access-control-") for name in response.headers), response.headers


def patch_seam(monkeypatch: Any, name: str, replacement: Callable[..., Any]) -> None:
    """Patch the owning seam and any existing frontend import of that seam."""
    from agentic_hil import cli, httpapi

    monkeypatch.setattr(httpapi, name, replacement)
    for module in (cli, httpserver):
        if hasattr(module, name):
            monkeypatch.setattr(module, name, replacement)
