"""The open read endpoints and the loopback admin endpoints over real loopback sockets (#889).

Every reply here is planted on a scripted seam: the job store's on
`ScriptedJobService`, the bench inventory's on `ScriptedBenchReads`. What these
tests hold the transport to is what it asks of those seams, what it lets out of
their answers, and whom it refuses.
"""

from __future__ import annotations

import hashlib
import json
from importlib import resources

from httpserver_fakes import (
    JOB_ID,
    JOB_PATH,
    Call,
    ScriptedBenchReads,
    ScriptedJobService,
    answer,
    foreign_peer,
    running_reads,
    service_error,
)

from agentic_hil.httpapi import JobStatus, QueueSnapshot
from agentic_hil.knowledge import TEST_CONFIG_SCHEMA_RESOURCE, errors_document
from agentic_hil.types import JsonObject

# The metadata anyone may read of anyone's job, and nothing else (#889).
PUBLIC_JOB_FIELDS = ("job_id", "user", "plan_name", "plan_digest", "devices", "state", "duration_s", "ok", "error_type")

OTHER_JOB: JsonObject = {
    "job_id": "job-of-another-submitter",
    "user": "other@example.invalid",
    "plan_name": "uart-smoke",
    "plan_digest": "sha256:" + "a" * 64,
    "devices": ["dut", "dut-uart"],
    "state": "finished",
    "duration_s": 12.5,
    "ok": False,
    "error_type": "uart_expect_timeout",
}
assert tuple(OTHER_JOB) == PUBLIC_JOB_FIELDS

# What a store record of that job carries beyond its metadata: its content.
# `summary` is among it, because for `uart_expect` it quotes received bytes.
CONTENT: JsonObject = {
    "summary": "uart_expect saw 'other-boot-banner' where it expected 'ready'.",
    "plan": "version: 1\nsteps:\n  - action: uart_expect\n",
    "artifacts": {"build/app.elf": "sha256:" + "b" * 64},
    "report": {"steps": [{"action": "uart_expect", "received": "other-boot-banner"}]},
    "log": "other-boot-banner\r\n",
    "evidence": "evidence-of-another-job.zip",
    "received": "other-boot-banner",
    "job_token": "token-of-another-job",
}

# The inventory routes, the read each one asks of the bench seam, and its arguments.
INVENTORY_ROUTES = (
    ("/v1/server", "server_info", ()),
    ("/v1/devices", "devices", ()),
    ("/v1/devices/dut", "device", ("dut",)),
    ("/v1/probes", "probes", ()),
    ("/v1/ports", "ports", ()),
    ("/v1/backends", "backends", ()),
    ("/v1/leases", "leases", ()),
    ("/v1/ledger", "ledger", ()),
    ("/v1/doctor", "doctor", ()),
)

METRICS = (
    "# HELP agentic_hil_queue_length Jobs waiting for their devices.\n"
    "# TYPE agentic_hil_queue_length gauge\n"
    "agentic_hil_queue_length 1\n"
)
METRICS_TYPE = "text/plain; version=0.0.4; charset=utf-8"
JSON = {"Content-Type": "application/json"}
STATEMENT = "Checked at the bench: the target is halted, the supply is off and only the probe is attached."
JOB_WRITES = {"submit", "check_plan", "upload_artifact", "withdraw"}


def inventory_reply(method: str) -> JsonObject:
    if method == "device":
        return {"name": "dut", "kind": "debugger", "state": "free", "recent_jobs": [OTHER_JOB]}
    return {"ok": True, "read": method}


def content_in(body: bytes) -> list[str]:
    """The content keys and values an answer carries, which should be none."""
    text = body.decode("utf-8")
    keys = [key for key in CONTENT if f'"{key}"' in text]
    values = [value for value in CONTENT.values() if isinstance(value, str) and json.dumps(value)[1:-1] in text]
    return keys + values


def test_read_endpoints_answer_without_a_token_and_create_no_job() -> None:
    service = ScriptedJobService()
    bench = ScriptedBenchReads()
    for _path, method, _arguments in INVENTORY_ROUTES:
        bench.script(method, inventory_reply(method))
    bench.script("device", service_error("device_not_found"))
    waiting = {**OTHER_JOB, "state": "queued", "queue_position": 1, "waiting_for": []}
    service.script("queue", QueueSnapshot((waiting,)))
    service.script("jobs", (OTHER_JOB,), (OTHER_JOB,))
    service.script("metrics", METRICS)
    schema = resources.files("agentic_hil").joinpath(TEST_CONFIG_SCHEMA_RESOURCE).read_bytes()

    with running_reads(service, bench) as server:
        for path, method, arguments in INVENTORY_ROUTES:
            before = len(bench.calls)
            response = answer(server, "GET", path)
            assert response.status == 200, (path, response)
            assert response.headers["content-type"].split(";", 1)[0] == "application/json", path
            assert response.json() == inventory_reply(method), path
            assert bench.calls[before:] == [Call(method, arguments, {})], path
        missing = answer(server, "GET", "/v1/devices/no-such-device")
        assert missing.status == 404
        assert missing.json()["error_type"] == "device_not_found"
        assert bench.calls[-1] == Call("device", ("no-such-device",), {})

        queued = answer(server, "GET", "/v1/queue")
        assert queued.status == 200
        assert queued.json() == {"entries": [waiting]}
        listed = answer(server, "GET", "/v1/jobs")
        assert listed.status == 200
        assert listed.json() == {"jobs": [OTHER_JOB]}
        assert service.calls[-1] == Call("jobs", (), {"state": None, "device": None, "user": None, "since": None})
        filtered = answer(server, "GET", "/v1/jobs?state=finished&device=dut&user=other%40example.invalid&since=2026-11-19T00%3A00%3A00Z")
        assert filtered.status == 200
        assert service.calls[-1] == Call("jobs", (), {
            "state": "finished", "device": "dut", "user": "other@example.invalid", "since": "2026-11-19T00:00:00Z",
        })
        for query in ("state=finished&state=queued", "owner=other%40example.invalid"):
            before = list(service.calls)
            refused = answer(server, "GET", f"/v1/jobs?{query}")
            assert refused.status == 400, query
            assert refused.json()["error_type"] == "invalid_argument", query
            assert service.calls == before, query

        metrics = answer(server, "GET", "/v1/metrics")
        assert metrics.status == 200
        assert metrics.headers["content-type"] == METRICS_TYPE
        assert metrics.body == METRICS.encode("utf-8")

        before_static = (list(service.calls), list(bench.calls))
        errors = answer(server, "GET", "/v1/errors")
        assert errors.status == 200
        assert errors.json() == json.loads(json.dumps(errors_document()))
        plan_schema = answer(server, "GET", "/v1/schema/plan")
        assert plan_schema.status == 200
        assert plan_schema.headers["content-type"] == "application/schema+json"
        assert plan_schema.body == schema
        assert (service.calls, bench.calls) == before_static

    assert [call.method for call in service.calls] == ["queue", "jobs", "jobs", "metrics"]
    assert not JOB_WRITES & {call.method for call in service.calls}
    assert service.published == []

    # Without a bench view the inventory is refused by name, and nothing else changes.
    service = ScriptedJobService()
    service.script("queue", QueueSnapshot(()))
    status = JobStatus(1, {"job_id": JOB_ID, "state": "queued"})
    service.script("status", status)
    with running_reads(service) as server:
        for path, _method, _arguments in INVENTORY_ROUTES:
            refused = answer(server, "GET", path)
            assert refused.status == 503, (path, refused)
            assert refused.json()["error_type"] == "bench_view_unavailable", path
        assert service.calls == []
        assert answer(server, "GET", "/v1/queue").json() == {"entries": []}
        job = answer(server, "GET", JOB_PATH)
        assert job.status == 200
        assert job.json() == status.status
    assert [call.method for call in service.calls] == ["queue", "status"]


def test_another_submitters_job_shows_metadata_only() -> None:
    service = ScriptedJobService()
    bench = ScriptedBenchReads()
    record = {**OTHER_JOB, **CONTENT}
    waiting_for = [{
        "device": "dut", "reason": "held", "position": 1,
        "held_by": {"job_id": "job-holding-dut", "user": "holder@example.invalid", "summary": CONTENT["summary"]},
        "detail": CONTENT["received"],
    }]
    queued = {**record, "state": "queued", "queue_position": 2, "waiting_for": waiting_for}
    service.script("jobs", (record,))
    service.script("queue", QueueSnapshot((queued,)))
    bench.script("device", {"name": "dut", "kind": "debugger", "state": "held", "recent_jobs": [record]})

    with running_reads(service, bench) as server:
        listed = answer(server, "GET", "/v1/jobs")
        assert listed.status == 200
        assert listed.json() == {"jobs": [OTHER_JOB]}
        waiting = answer(server, "GET", "/v1/queue")
        assert waiting.status == 200
        assert waiting.json() == {"entries": [{
            **OTHER_JOB, "state": "queued", "queue_position": 2,
            "waiting_for": [{"device": "dut", "reason": "held", "position": 1, "held_by": {"job_id": "job-holding-dut"}}],
        }]}
        device = answer(server, "GET", "/v1/devices/dut")
        assert device.status == 200
        assert device.json()["recent_jobs"] == [OTHER_JOB]
        for response in (listed, waiting, device):
            assert content_in(response.body) == [], response.body

        # That job's own results stay behind its token.
        job = f"/v1/jobs/{OTHER_JOB['job_id']}"
        for suffix in ("report", "logs/0", "evidence"):
            before = list(service.calls)
            refused = answer(server, "GET", f"{job}/{suffix}")
            assert refused.status == 401, suffix
            assert refused.headers["www-authenticate"] == "Bearer", suffix
            assert refused.json()["error_type"] == "job_token_missing", suffix
            assert service.calls == before, suffix
        wrong = service_error("job_token_invalid")
        own_token = {"Authorization": "Bearer token-of-my-own-job"}
        for suffix, method in (("report", "report"), ("logs/0", "log"), ("evidence", "evidence")):
            service.script(method, wrong)
            refused = answer(server, "GET", f"{job}/{suffix}", headers=own_token)
            assert refused.status == 403, suffix
            assert refused.json() == wrong.to_dict(), suffix
            assert service.calls[-1].method == method, suffix
            assert service.calls[-1].kwargs["job_token"] == "token-of-my-own-job", suffix


def test_drain_reaches_the_seam_and_waiting_jobs_say_why() -> None:
    service = ScriptedJobService()
    drained = {"device": "dut", "drained": True, "running_job": "job-running-on-dut"}
    service.script("drain", drained)
    waiting = {**OTHER_JOB, "state": "queued", "queue_position": 1, "waiting_for": [{"device": "dut", "reason": "drained"}]}
    service.script("queue", QueueSnapshot((waiting,)))

    with running_reads(service) as server:
        response = answer(server, "POST", "/v1/devices/dut/drain")
        assert response.status == 200
        assert response.json() == drained
        assert service.calls == [Call("drain", ("dut",), {})]
        queued = answer(server, "GET", "/v1/queue")
        assert queued.status == 200
        assert queued.json() == {"entries": [waiting]}


def test_admin_endpoints_refuse_a_non_loopback_peer() -> None:
    service = ScriptedJobService()
    recovered = {"ok": True, "tool": "hardware_recover", "was_quarantined": True, "nothing_to_recover": False}
    service.script("recover", recovered, recovered)
    statement = json.dumps({"operator_statement": STATEMENT}).encode("utf-8")
    forwarded = {**JSON, "X-Forwarded-For": "127.0.0.1"}

    with running_reads(service) as server:
        with foreign_peer(server):
            for path, body in (
                ("/v1/devices/dut/drain", None),
                ("/v1/devices/dut/recover", statement),
                ("/v1/devices/dut/recover", b"{not json"),
            ):
                refused = answer(server, "POST", path, body=body, headers=forwarded, withhold_body=body is not None)
                assert refused.status == 403, (path, refused)
                assert refused.json()["error_type"] == "admin_loopback_only", path
            assert service.calls == []

        accepted = answer(server, "POST", "/v1/devices/dut/recover", body=json.dumps({
            "operator_statement": STATEMENT, "accept_config_change": True,
        }).encode("utf-8"), headers=JSON)
        assert accepted.status == 200
        assert accepted.json() == recovered
        assert service.calls == [Call("recover", ("dut",), {"operator_statement": STATEMENT, "accept_config_change": True})]
        accepted = answer(server, "POST", "/v1/devices/dut/recover", body=statement, headers=JSON)
        assert accepted.status == 200
        assert service.calls[-1] == Call("recover", ("dut",), {"operator_statement": STATEMENT, "accept_config_change": False})

        for body in (
            b"{}",
            json.dumps({"operator_statement": ""}).encode("utf-8"),
            json.dumps({"operator_statement": STATEMENT, "accept_config_change": "yes"}).encode("utf-8"),
            b"{not json",
        ):
            before = list(service.calls)
            refused = answer(server, "POST", "/v1/devices/dut/recover", body=body, headers=JSON)
            assert refused.status == 400, body
            assert refused.json()["error_type"] == "invalid_argument", body
            assert service.calls == before, body


def test_inventory_answers_carry_cache_validators() -> None:
    service = ScriptedJobService()
    bench = ScriptedBenchReads()
    probes = {"probes": {"dut": {"backend": "pyocd", "status": "present"}}, "present_not_configured": 1, "observed_at": "2026-11-19T04:00:00Z"}
    ports = {"ports": {"psu": {"status": "present"}}, "present_not_configured": 0, "observed_at": "2026-11-19T04:00:01Z"}
    bench.script("probes", probes, probes)
    bench.script("ports", ports, ports)

    with running_reads(service, bench) as server:
        for path, reading in (("/v1/probes", probes), ("/v1/ports", ports)):
            first = answer(server, "GET", path)
            assert first.status == 200, path
            assert first.json() == reading, path
            assert first.headers["cache-control"] == "max-age=5", path
            assert first.headers["etag"] == f'"{hashlib.sha256(first.body).hexdigest()}"', path
            again = answer(server, "GET", path, headers={"If-None-Match": first.headers["etag"]})
            assert again.status == 304, path
            assert again.body == b"", path
            assert again.headers["etag"] == first.headers["etag"], path
            assert again.headers["cache-control"] == "max-age=5", path
