"""Standard-library loopback HTTP transport for submitted plans.

The service is the only path to jobs: this module reads no configuration,
plan, artifact or result files, and never builds, imports or executes uploads.
Long polls and event streams use their connection threads and bounded service
reads. Bearer credentials and URL queries are never written to request logs.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from collections.abc import Mapping
from email import policy
from email.message import Message
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Event
from typing import cast
from urllib.parse import parse_qs, quote, unquote, urlsplit

from agentic_hil import bench, httpread, mcp_http
from agentic_hil.httpapi import BenchReads, ByteRange, JobService, JobServiceError, McpBenchReads, ServerSettings
from agentic_hil.types import JsonObject


def validate_listen_address(settings: ServerSettings) -> None:
    """Refuse other listen addresses before constructing a socket."""
    if settings.host != "127.0.0.1":
        raise JobServiceError("server_listen_refused", "serve listens only on 127.0.0.1; #891 introduces non-loopback listening.")


def server_lock_warning() -> str | None:
    """Check existing machine-lock policy without changing or creating locks."""
    if bench.machine_lock_root_applies() and bench.machine_device_lock_root() is None:
        return (
            "Runs of other accounts do not see the server's locks, and the server does not see their locks. "
            + bench.MACHINE_DEVICE_LOCK_SETUP
        )
    return None


class _JobHTTPServer(ThreadingHTTPServer):
    def __init__(
        self, settings: ServerSettings, service: JobService, bench_reads: McpBenchReads | None = None,
        *, bench: BenchReads | None = None,
    ) -> None:
        self.settings = settings
        self.service = service
        self.bench = bench
        self.mcp = mcp_http.McpEndpoint(settings, service, bench_reads)
        self.stopping = Event()
        super().__init__((settings.host, settings.port), _JobRequestHandler)

    def server_close(self) -> None:
        self.stopping.set()
        super().server_close()


def create_http_server(
    settings: ServerSettings, service: JobService, *,
    bench_reads: McpBenchReads | None = None, bench: BenchReads | None = None,
) -> ThreadingHTTPServer:
    """Construct the production IPv4 loopback listener for the supplied service."""
    validate_listen_address(settings)
    return _JobHTTPServer(settings, service, bench_reads, bench=bench)


_ERROR_STATUS = {
    "artifact_validation_failed": 422,
    "origin_refused": 403,
    "host_refused": 403,
    "job_token_invalid": 403,
    "job_token_missing": 401,
    "artifact_unreferenced": 409,
    "artifact_already_uploaded": 409,
    "per_user_limit": 429,
    "queue_full": 503,
    "job_not_found": 404,
    "request_too_large": 413,
    "unsupported_media_type": 415,
    "range_not_satisfiable": 416,
    "invalid_argument": 400,
    "admin_loopback_only": 403,
    "device_not_found": 404,
    "bench_view_unavailable": 503,
}
_ENDED_ERRORS = {"job_finished", "job_withdrawn", "job_expired", "job_runtime_exceeded"}


class _JobRequestHandler(BaseHTTPRequestHandler):
    # HTTP/1.0 closes each response, including streams of unknown final length.
    # It also lets a refused body remain unread without reusing that connection.
    def log_message(self, format: str, *args: object) -> None:
        """Do not log request lines or credentials."""

    @property
    def job_server(self) -> _JobHTTPServer:
        return cast(_JobHTTPServer, self.server)

    def do_GET(self) -> None:
        self._handle()

    def do_POST(self) -> None:
        self._handle()

    def do_PUT(self) -> None:
        self._handle()

    def do_DELETE(self) -> None:
        self._handle()

    def do_HEAD(self) -> None:
        self._handle()

    def do_OPTIONS(self) -> None:
        self._handle()

    def _handle(self) -> None:
        self._headers_sent = False
        try:
            self._browser_guard()
            self._route()
        except JobServiceError as error:
            if not self._headers_sent:
                self._error(error)
        except (ConnectionError, TimeoutError):
            # A disconnected streaming client does not change any job outcome.
            pass
        finally:
            self.close_connection = True

    def _browser_guard(self) -> None:
        settings = self.job_server.settings
        origins = self.headers.get_all("Origin", [])
        if origins and (len(origins) != 1 or origins[0] not in settings.allowed_origins):
            raise JobServiceError("origin_refused", "The request Origin is not allowed by this server.")
        hosts = self.headers.get_all("Host", [])
        if len(hosts) != 1 or not self._host_allowed(hosts[0]):
            raise JobServiceError("host_refused", "The request Host is not this listener or a configured hostname.")

    def _host_allowed(self, value: str) -> bool:
        try:
            authority = urlsplit("//" + value)
            if authority.path or authority.query or authority.fragment or authority.username is not None or authority.password is not None:
                return False
            host = authority.hostname
            if authority.port not in (None, self.job_server.server_port):
                return False
        except ValueError:
            return False
        settings = self.job_server.settings
        return host is not None and host.lower() in {settings.host, *(name.lower() for name in settings.hostnames)}

    def _token(self) -> str:
        values = self.headers.get_all("Authorization", [])
        parts = values[0].split() if len(values) == 1 else []
        if len(parts) != 2 or parts[0].lower() != "bearer":
            raise JobServiceError("job_token_missing", "Authorization: Bearer is required for this job resource.")
        return parts[1]

    def _body(self) -> bytes:
        lengths = self.headers.get_all("Content-Length", [])
        if len(lengths) != 1 or re.fullmatch(r"[0-9]+", lengths[0]) is None or self.headers.get("Transfer-Encoding") is not None:
            raise JobServiceError("invalid_argument", "POST and PUT require a single valid Content-Length and no Transfer-Encoding.")
        length = int(lengths[0])
        if length > self.job_server.settings.max_upload_mb * 1024 * 1024:
            raise JobServiceError("request_too_large", "The request exceeds the server upload limit.")
        body = self.rfile.read(length)
        if len(body) != length:
            raise JobServiceError("invalid_argument", "The request body ended before Content-Length bytes arrived.")
        return body

    def _plan(self) -> tuple[bytes, dict[str, bytes]]:
        # Read the length-bounded complete request before any admission call.
        body = self._body()
        content_type = self.headers.get("Content-Type", "")
        header = Message()
        header["Content-Type"] = content_type
        if header.get_content_type() == "application/yaml":
            return body, {}
        if header.get_content_type() != "multipart/form-data":
            raise JobServiceError("unsupported_media_type", "Submit application/yaml or multipart/form-data with a plan part.")
        try:
            envelope = BytesParser(policy=policy.default).parsebytes(
                b"MIME-Version: 1.0\r\nContent-Type: " + content_type.encode("ascii") + b"\r\n\r\n" + body
            )
        except (UnicodeError, ValueError) as error:
            raise JobServiceError("invalid_argument", "The multipart envelope is malformed.") from error
        if not envelope.is_multipart() or any(part.defects for part in envelope.walk()):
            raise JobServiceError("invalid_argument", "The complete multipart envelope and closing boundary are required.")
        parts: dict[str, bytes] = {}
        for part in envelope.iter_parts():
            name = part.get_param("name", header="content-disposition")
            if part.is_multipart() or part.get_content_disposition() != "form-data" or not isinstance(name, str) or not name or name in parts:
                raise JobServiceError("invalid_argument", "Each multipart part needs a unique form-data name, including one plan part.")
            payload = part.get_payload(decode=True)
            if not isinstance(payload, bytes):
                raise JobServiceError("invalid_argument", "A multipart part has no byte payload.")
            parts[name] = payload
        if "plan" not in parts:
            raise JobServiceError("invalid_argument", "The multipart submission needs a plan part.")
        return parts.pop("plan"), parts

    def _digest(self, body: bytes) -> None:
        value = self.headers.get("Content-Digest")
        if value is None:
            return
        members = [member.strip() for member in value.split(",")]
        sha256 = [member for member in members if member.startswith("sha-256=")]
        try:
            match = re.fullmatch(r"sha-256=:([A-Za-z0-9+/]*={0,2}):", sha256[0]) if len(sha256) == 1 else None
            if match is None:
                raise ValueError("missing or malformed sha-256 digest")
            digest = base64.b64decode(match[1], validate=True)
            if digest != hashlib.sha256(body).digest():
                raise ValueError("digest mismatch")
        except (ValueError, binascii.Error) as error:
            raise JobServiceError("artifact_validation_failed", "Content-Digest does not provide the matching SHA-256 of the artifact body.") from error

    def _route(self) -> None:
        parsed = urlsplit(self.path)
        answered = httpread.route(self.command, parsed, self.headers, self.client_address, self.job_server.service, self.job_server.bench, self._body)
        if answered is not None:
            self._respond(answered.code, answered.body, answered.media_type, answered.headers)
            return
        path = parsed.path
        if path == "/mcp":
            mcp_http.handle(self)
            return
        service = self.job_server.service
        if self.command == "POST" and path in ("/v1/jobs", "/v1/plans/check"):
            plan, artifacts = self._plan()
            submitted_by = self.headers.get("From")
            if path == "/v1/plans/check":
                self._json(200, service.check_plan(plan, artifacts, submitted_by=submitted_by))
            else:
                result = service.submit(plan, artifacts, idempotency_key=self.headers.get("Idempotency-Key"), submitted_by=submitted_by)
                self._json(202, result, {
                    "Location": "/v1/jobs/" + quote(str(result["job_id"]), safe=""),
                    "Retry-After": str(self.job_server.settings.retry_after_s),
                })
            return
        segments = path.split("/")
        if len(segments) < 4 or segments[1:3] != ["v1", "jobs"] or not segments[3]:
            raise JobServiceError("job_not_found", "The requested job resource does not exist.")
        job_id = unquote(segments[3])
        suffix = segments[4:]
        if self.command == "GET" and not suffix:
            self._status(job_id)
        elif self.command == "GET" and suffix == ["events"]:
            self._events(job_id)
        elif self.command == "DELETE" and not suffix:
            result = service.withdraw(job_id, job_token=self._token())
            if result["started"]:
                self._json(202, result)
            else:
                self._respond(204)
        elif self.command == "PUT" and len(suffix) >= 2 and suffix[0] == "artifacts":
            token = self._token()
            body = self._body()
            media_type = self.headers.get_content_type() if self.headers.get("Content-Type") else "application/octet-stream"
            if media_type != "application/octet-stream":
                raise JobServiceError("unsupported_media_type", "An artifact PUT takes application/octet-stream bytes.")
            self._digest(body)
            self._json(201, service.upload_artifact(job_id, unquote("/".join(suffix[1:])), body, job_token=token))
        elif self.command == "GET" and suffix == ["report"]:
            self._json(200, service.report(job_id, job_token=self._token()))
        elif self.command == "GET" and suffix == ["junit"]:
            self._respond(200, service.junit(job_id, job_token=self._token()), "application/xml")
        elif self.command == "GET" and suffix == ["evidence"]:
            content = service.evidence(job_id, job_token=self._token())
            headers = {"Content-Disposition": "attachment; filename*=UTF-8''" + quote(content.filename, safe="")} if content.filename else {}
            self._respond(200, content.body, content.media_type, headers)
        elif self.command == "GET" and len(suffix) == 2 and suffix[0] == "logs" and suffix[1].isdigit():
            self._log(job_id, int(suffix[1]), parse_qs(parsed.query).get("follow") == ["1"])
        else:
            raise JobServiceError("job_not_found", "The requested job resource does not exist.")

    def _status(self, job_id: str) -> None:
        service = self.job_server.service
        snapshot = service.status(job_id)
        etag = f'"{snapshot.revision}"'
        validators = [item.strip().removeprefix("W/") for item in self.headers.get("If-None-Match", "").split(",")]
        if etag in validators or "*" in validators:
            wait = re.search(r"(?:^|[,;])\s*wait\s*=\s*([0-9]+)(?=\s*(?:[,;]|$))", self.headers.get("Prefer", ""))
            changed = service.wait_for_revision(
                job_id, after_revision=snapshot.revision,
                timeout_s=min(float(wait[1]), self.job_server.settings.prefer_wait_cap_s),
            ) if wait else None
            if changed is None or changed.revision <= snapshot.revision:
                self._respond(304, headers={"ETag": etag})
                return
            snapshot = changed
        self._json(200, snapshot.status, {"ETag": f'"{snapshot.revision}"'})

    def _events(self, job_id: str) -> None:
        cursor = self.headers.get("Last-Event-ID")
        while not self.job_server.stopping.is_set():
            batch = self.job_server.service.events_after(job_id, last_event_id=cursor, timeout_s=self.job_server.settings.sse_keepalive_s)
            if not self._headers_sent:
                self._start(200, "text/event-stream", {"Cache-Control": "no-cache"})
            for event in batch.events:
                data = json.dumps(event.status, ensure_ascii=True)
                self.wfile.write(f"id: {event.event_id}\ndata: {data}\n\n".encode())
                cursor = event.event_id
            if not batch.events and not batch.complete:
                self.wfile.write(b": keep-alive\n\n")
            self.wfile.flush()
            if batch.complete:
                return

    def _range(self) -> ByteRange | None:
        value = self.headers.get("Range")
        if value is None:
            return None
        match = re.fullmatch(r"bytes=([0-9]*)-([0-9]*)", value)
        if match is None or not (match[1] or match[2]):
            raise JobServiceError("invalid_argument", "A log Range must specify one byte range.")
        return ByteRange(int(match[1]) if match[1] else None, int(match[2]) if match[2] else None)

    def _log(self, job_id: str, n: int, follow: bool) -> None:
        token = self._token()
        byte_range = self._range()
        while not self.job_server.stopping.is_set():
            result = self.job_server.service.log(
                job_id, n, job_token=token, byte_range=byte_range, follow=follow,
                timeout_s=self.job_server.settings.sse_keepalive_s if follow else 0.0,
            )
            if not self._headers_sent:
                headers = {"Accept-Ranges": "bytes"}
                code = 206 if byte_range is not None else 200
                if byte_range is not None:
                    headers["Content-Range"] = f"bytes {result.first}-{result.first + len(result.body) - 1}/{result.total_size}"
                if not follow:
                    self._respond(code, result.body, "application/octet-stream", headers)
                    return
                self._start(code, "application/octet-stream", headers)
            self.wfile.write(result.body)
            self.wfile.flush()
            if result.complete:
                return
            byte_range = ByteRange(result.first + len(result.body), None)

    def _error(self, error: JobServiceError) -> None:
        if "validation_error" in error.details:
            code = 422
        elif self.command == "DELETE" and error.error_type in _ENDED_ERRORS:
            code = 409
        else:
            code = _ERROR_STATUS.get(error.error_type, 500)
        headers: dict[str, str] = {}
        if code == 401:
            headers["WWW-Authenticate"] = "Bearer"
        if code in (429, 503):
            headers["Retry-After"] = str(error.details.get("retry_after_s", self.job_server.settings.retry_after_s))
        if code == 416 and "total_size" in error.details:
            headers["Content-Range"] = f"bytes */{error.details['total_size']}"
        self._json(code, error.to_dict(), headers)

    def _start(self, code: int, media_type: str, headers: Mapping[str, str]) -> None:
        self.send_response(code)
        self.send_header("Content-Type", media_type)
        self.send_header("Connection", "close")
        for name, value in headers.items():
            self.send_header(name, value)
        self.end_headers()
        self._headers_sent = True

    def _respond(self, code: int, body: bytes = b"", media_type: str = "application/json", headers: Mapping[str, str] | None = None) -> None:
        self._start(code, media_type, {"Content-Length": str(len(body)), **(headers or {})})
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code: int, body: JsonObject, headers: Mapping[str, str] | None = None) -> None:
        self._respond(code, json.dumps(body, ensure_ascii=True).encode("utf-8"), headers=headers)
