"""Open read endpoints and loopback-only admin endpoints of the bench server (#889).

The request handler hands every request here first. A request this module does
not own is answered None, and the handler routes it as before.

Anyone who reaches the listener may read the bench inventory and the metadata
of every job. Nothing a job produced leaves through here: every job record
passes one allowlist, `_PUBLIC_JOB_FIELDS`, and a job's report, logs and
evidence stay behind its token on the handler's own routes. Draining and
recovering a device are answered only to a socket peer on 127.0.0.1, and a
header naming another address changes nothing.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from email.message import Message
from functools import cache
from importlib import resources
from typing import Any
from urllib.parse import SplitResult, parse_qs, unquote

from agentic_hil.httpapi import INVENTORY_MAX_AGE_S, BenchReads, JobService, JobServiceError
from agentic_hil.knowledge import TEST_CONFIG_SCHEMA_RESOURCE, errors_document
from agentic_hil.types import JsonObject

_ADMIN_ACTIONS = frozenset({"drain", "recover"})
# What anyone may read of anyone's job. A record's content is never among it:
# not its summary, which for `uart_expect` quotes the bytes the board sent, and
# not its plan, artifacts, report, logs, evidence or token.
_PUBLIC_JOB_FIELDS = frozenset({"job_id", "user", "plan_name", "plan_digest", "devices", "state", "duration_s", "ok", "error_type"})
# What a waiting_for item may say beside the holding job's id.
_WAITING_FIELDS = frozenset({"device", "reason", "position"})
_JOB_FILTERS = ("state", "device", "user", "since")
_RECOVER_FIELDS = frozenset({"operator_statement", "accept_config_change"})
_INVENTORY: Mapping[str, Callable[[BenchReads], JsonObject]] = {
    "/v1/server": lambda bench: bench.server_info(),
    "/v1/devices": lambda bench: bench.devices(),
    "/v1/probes": lambda bench: bench.probes(),
    "/v1/ports": lambda bench: bench.ports(),
    "/v1/backends": lambda bench: bench.backends(),
    "/v1/leases": lambda bench: bench.leases(),
    "/v1/ledger": lambda bench: bench.ledger(),
    "/v1/doctor": lambda bench: bench.doctor(),
}
_READ_PATHS = frozenset({*_INVENTORY, "/v1/queue", "/v1/jobs", "/v1/errors", "/v1/schema/plan", "/v1/metrics"})
# The host inventory readings, which the bench view itself keeps for
# INVENTORY_MAX_AGE_S: a client may keep them as long and revalidate by ETag.
_VALIDATED_PATHS = frozenset({"/v1/probes", "/v1/ports"})
_JSON_TYPE = "application/json"
_METRICS_TYPE = "text/plain; version=0.0.4; charset=utf-8"
_SCHEMA_TYPE = "application/schema+json"


@dataclass(frozen=True)
class ReadResponse:
    code: int
    body: bytes
    media_type: str
    headers: Mapping[str, str] = field(default_factory=dict)


def _owned(command: str, path: str) -> bool:
    segments = path.split("/")
    device = len(segments) >= 4 and segments[1:3] == ["v1", "devices"] and bool(segments[3])
    if command == "GET":
        return path in _READ_PATHS or (device and len(segments) == 4)
    if command == "POST":
        return device and len(segments) == 5 and segments[4] in _ADMIN_ACTIONS
    return False


def route(
    command: str, parsed: SplitResult, headers: Message, peer: Any,
    service: JobService, bench: BenchReads | None, read_body: Callable[[], bytes],
) -> ReadResponse | None:
    """Answer an open read or a loopback admin request, or None for every other request."""
    if not _owned(command, parsed.path):
        return None
    path = parsed.path
    segments = path.split("/")
    if command == "POST":
        # Before the body: a refused peer's request is never read.
        _require_loopback(peer)
        name = unquote(segments[3])
        if segments[4] == "drain":
            return _json(service.drain(name))
        statement, accept_config_change = _recover_arguments(read_body())
        return _json(service.recover(name, operator_statement=statement, accept_config_change=accept_config_change))
    if path == "/v1/queue":
        return _json({"entries": [_queued(entry) for entry in service.queue().entries]})
    if path == "/v1/jobs":
        filters = _job_filters(parsed.query)
        jobs = service.jobs(state=filters["state"], device=filters["device"], user=filters["user"], since=filters["since"])
        return _json({"jobs": [_public(job) for job in jobs]})
    if path == "/v1/metrics":
        return ReadResponse(200, service.metrics().encode("utf-8"), _METRICS_TYPE)
    if path == "/v1/errors":
        return _json(errors_document())
    if path == "/v1/schema/plan":
        return ReadResponse(200, _plan_schema(), _SCHEMA_TYPE)
    reads = _require_bench(bench)
    answer = _INVENTORY[path](reads) if path in _INVENTORY else _with_public_jobs(reads.device(unquote(segments[3])))
    if path in _VALIDATED_PATHS:
        return _validated(answer, headers)
    return _json(answer)


def _require_loopback(peer: Any) -> None:
    """Admit the socket peer 127.0.0.1 and nobody else. A forwarding header is
    something the peer says about itself, so it is never read here."""
    address = peer[0] if isinstance(peer, tuple) and peer else None
    if address != "127.0.0.1":
        raise JobServiceError(
            "admin_loopback_only",
            "Draining and recovering a device are answered only to a client connected from 127.0.0.1 on the bench itself.",
        )


def _require_bench(bench: BenchReads | None) -> BenchReads:
    if bench is None:
        raise JobServiceError(
            "bench_view_unavailable",
            "This server runs without a bench view, so it answers no inventory read. The queue, the job list and every job route still answer.",
        )
    return bench


def _job_filters(query: str) -> dict[str, str | None]:
    given = parse_qs(query, keep_blank_values=True)
    refused = sorted(key for key, values in given.items() if key not in _JOB_FILTERS or len(values) != 1 or not values[0])
    if refused:
        raise JobServiceError(
            "invalid_argument",
            "The job list takes state, device, user and since, each at most once and with a value, and no other filter.",
            {"refused_filters": refused, "filters": list(_JOB_FILTERS)},
        )
    return {key: given[key][0] if key in given else None for key in _JOB_FILTERS}


def _recover_arguments(body: bytes) -> tuple[str, bool]:
    try:
        document = json.loads(body)
    except ValueError:
        document = None
    if isinstance(document, dict) and not set(document) - _RECOVER_FIELDS:
        statement = document.get("operator_statement")
        accept_config_change = document.get("accept_config_change", False)
        if isinstance(statement, str) and statement.strip() and isinstance(accept_config_change, bool):
            return statement, accept_config_change
    raise JobServiceError(
        "invalid_argument",
        "A recover request takes a JSON object with a non-empty operator_statement and, optionally, a boolean accept_config_change.",
        {"fields": sorted(_RECOVER_FIELDS)},
    )


def _public(record: Mapping[str, Any]) -> JsonObject:
    return {key: value for key, value in record.items() if key in _PUBLIC_JOB_FIELDS}


def _queued(entry: Mapping[str, Any]) -> JsonObject:
    projected = _public(entry)
    if "queue_position" in entry:
        projected["queue_position"] = entry["queue_position"]
    waiting = entry.get("waiting_for")
    if isinstance(waiting, (list, tuple)):
        projected["waiting_for"] = [_waiting(item) for item in waiting if isinstance(item, Mapping)]
    return projected


def _waiting(item: Mapping[str, Any]) -> JsonObject:
    """Why an entry waits, and for which job: the holder's id and nothing else of it."""
    kept: JsonObject = {}
    for key, value in item.items():
        if key in _WAITING_FIELDS:
            kept[key] = value
        elif key == "held_by" and isinstance(value, Mapping) and "job_id" in value:
            kept[key] = {"job_id": value["job_id"]}
    return kept


def _with_public_jobs(answer: JsonObject) -> JsonObject:
    if "recent_jobs" not in answer:
        return answer
    recent = answer["recent_jobs"]
    jobs = [_public(job) for job in recent if isinstance(job, Mapping)] if isinstance(recent, (list, tuple)) else []
    return {**answer, "recent_jobs": jobs}


def _encoded(body: JsonObject) -> bytes:
    return json.dumps(body, ensure_ascii=True).encode("utf-8")


def _json(body: JsonObject) -> ReadResponse:
    return ReadResponse(200, _encoded(body), _JSON_TYPE)


def _validated(body: JsonObject, headers: Message) -> ReadResponse:
    encoded = _encoded(body)
    etag = f'"{hashlib.sha256(encoded).hexdigest()}"'
    validators = {"Cache-Control": f"max-age={INVENTORY_MAX_AGE_S}", "ETag": etag}
    offered = [item.strip().removeprefix("W/") for item in headers.get("If-None-Match", "").split(",")]
    if etag in offered or "*" in offered:
        return ReadResponse(304, b"", _JSON_TYPE, validators)
    return ReadResponse(200, encoded, _JSON_TYPE, validators)


@cache
def _plan_schema() -> bytes:
    return resources.files("agentic_hil").joinpath(TEST_CONFIG_SCHEMA_RESOURCE).read_bytes()
