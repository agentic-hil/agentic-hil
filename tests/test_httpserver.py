"""HTTP acceptance against scripted service replies over real loopback sockets."""

from __future__ import annotations

import base64
import hashlib
import json

from httpserver_fakes import (
    ARTIFACT_PATH,
    IMAGE,
    JOB_ID,
    JOB_PATH,
    PLAN,
    TOKEN,
    Call,
    ScriptedJobService,
    WaitReply,
    background_request,
    multipart,
    no_cors,
    request,
    running_http,
    service_error,
    short_settings,
    submission,
)
from support import scaled_time_bound

from agentic_hil import httpserver
from agentic_hil.httpapi import JobEvent, JobEvents, JobStatus

YAML = {"Content-Type": "application/yaml"}
BEARER = {"Authorization": f"Bearer {TOKEN}"}


def test_plan_rejection_matches_check_without_creating_job() -> None:
    service = ScriptedJobService()
    validation = {
        "error_type": "test_plan_rejected", "summary": "Recorded plan refusal.",
        "step": 1, "field": "image_path", "route": "dut", "action": "flash",
    }
    with running_http(service) as server:
        for error_type in ("test_unmapped_plan_error", "queue_full"):
            error = service_error(error_type, {"validation_error": validation})
            expected = error.to_dict()
            service.script("submit", error)
            service.script("check_plan", error)
            for path in ("/v1/jobs", "/v1/plans/check"):
                response = request(server, "POST", path, body=PLAN, headers=YAML)
                assert response.status == 422
                assert response.json() == expected
                assert response.json()["error_type"] == error_type
                assert response.json()["validation_error"] == validation
                assert "location" not in response.headers
                assert service.published == []
            assert service.calls[-2:] == [
                Call("submit", (PLAN, {}), {"idempotency_key": None, "submitted_by": None}),
                Call("check_plan", (PLAN, {}), {"submitted_by": None}),
            ]

        unmapped = service_error("test_unmapped_plan_error")
        expected = unmapped.to_dict()
        service.script("submit", unmapped)
        response = request(server, "POST", "/v1/jobs", body=PLAN, headers=YAML)
        assert response.status == 500
        assert response.json() == expected
        assert "location" not in response.headers

        preview = {
            "validation": {"ok": True}, "devices": ["dut"], "estimate": {"runtime_s": 12},
            "would_start_now": False,
            "waiting_for": [{"device": "dut", "position": 2, "held_by": {"job_id": "job-holder"}}],
        }
        service.script("check_plan", preview)
        response = request(server, "POST", "/v1/plans/check", body=PLAN, headers=YAML)
        assert response.status == 200
        assert response.json() == preview
        assert "location" not in response.headers
        assert service.calls[-1] == Call("check_plan", (PLAN, {}), {"submitted_by": None})
        assert service.published == []


def test_artifact_upload_completion_is_forwarded() -> None:
    service = ScriptedJobService()
    accepted = submission("awaiting_artifacts")
    queued = {
        "job_id": JOB_ID, "state": "queued", "devices": ["dut", "dut-uart"],
        "sequence": 7, "queue_position": 1, "waiting_for": [], "summary": "Queued.",
    }
    uploaded = {"job_id": JOB_ID, "state": "queued", "missing_artifacts": []}
    service.script("submit", accepted)
    service.script("upload_artifact", uploaded)
    service.script("status", JobStatus(2, queued))
    settings = short_settings()
    with running_http(service, settings) as server:
        response = request(server, "POST", "/v1/jobs", body=PLAN, headers=YAML)
        assert response.status == 202
        assert response.headers["location"] == JOB_PATH
        assert response.headers["retry-after"] == str(settings.retry_after_s)
        assert response.json() == accepted
        assert service.calls == [Call("submit", (PLAN, {}), {"idempotency_key": None, "submitted_by": None})]

        response = request(server, "PUT", ARTIFACT_PATH, body=IMAGE, headers=BEARER)
        assert response.status == 201
        assert service.calls[-1] == Call("upload_artifact", (JOB_ID, "build/app.elf", IMAGE), {"job_token": TOKEN})
        response = request(server, "GET", JOB_PATH)
        assert response.status == 200
        assert response.headers["etag"] == '"2"'
        assert response.json() == queued
        assert service.calls[-1] == Call("status", (JOB_ID,), {})


def test_artifact_refusals_and_digest_check() -> None:
    service = ScriptedJobService()
    with running_http(service) as server:
        for error_type in ("artifact_unreferenced", "artifact_already_uploaded", "artifact_validation_failed"):
            error = service_error(error_type)
            expected = error.to_dict()
            service.script("upload_artifact", error)
            response = request(server, "PUT", ARTIFACT_PATH, body=IMAGE, headers=BEARER)
            assert response.status == (422 if error_type == "artifact_validation_failed" else 409)
            assert response.json() == expected

        wrong_digest = base64.b64encode(hashlib.sha256(b"different bytes").digest()).decode("ascii")
        before = list(service.calls)
        response = request(server, "PUT", ARTIFACT_PATH, body=IMAGE, headers={
            **BEARER, "Content-Digest": f"sha-256=:{wrong_digest}:",
        })
        assert response.status == 422
        assert response.json()["error_type"] == "artifact_validation_failed"
        assert service.calls == before

        digest = base64.b64encode(hashlib.sha256(IMAGE).digest()).decode("ascii")
        headers = {**BEARER, "Content-Digest": f"sha-256=:{digest}:"}
        duplicate = service_error("artifact_already_uploaded")
        service.script("upload_artifact", {"job_id": JOB_ID, "missing_artifacts": []}, duplicate)
        response = request(server, "PUT", ARTIFACT_PATH, body=IMAGE, headers=headers)
        assert response.status == 201
        assert service.calls[-1] == Call("upload_artifact", (JOB_ID, "build/app.elf", IMAGE), {"job_token": TOKEN})
        response = request(server, "PUT", ARTIFACT_PATH, body=IMAGE, headers=headers)
        assert response.status == 409
        assert response.json() == duplicate.to_dict()


def test_conditional_status_and_resumable_events() -> None:
    service = ScriptedJobService()
    settings = short_settings()
    before = JobStatus(1, {"job_id": JOB_ID, "state": "queued", "summary": "Queued."})
    after = JobStatus(2, {
        "job_id": JOB_ID, "state": "running", "step": 1, "iteration": 2,
        "holder": {"job_id": JOB_ID}, "estimate": {"runtime_s": 12}, "summary": "Running.",
    })
    state_wait = WaitReply()
    events_wait = WaitReply()
    service.script("status", before, before, after)
    service.script("wait_for_revision", state_wait, None)
    service.script("events_after", events_wait)
    with running_http(service, settings) as server:
        first = request(server, "GET", JOB_PATH)
        assert first.status == 200
        assert first.json() == before.status
        assert first.headers["etag"] == '"1"'

        with background_request(server, service, "GET", JOB_PATH, headers={
            "If-None-Match": first.headers["etag"], "Prefer": "wait=60",
        }) as pending:
            assert state_wait.entered.wait(scaled_time_bound(10)), "Long poll never entered its service wait"
            assert not pending.done(), "Long poll answered before the state changed"
            state_wait.publish(after)
            changed = pending.result(timeout=scaled_time_bound(10))
        assert changed.status == 200
        assert changed.headers["etag"] == '"2"'
        assert changed.json() == after.status
        waited = [call for call in service.calls if call.method == "wait_for_revision"]
        assert waited[0].args == (JOB_ID,)
        assert waited[0].kwargs["after_revision"] == 1
        assert 0 < waited[0].kwargs["timeout_s"] <= settings.prefer_wait_cap_s

        unchanged = request(server, "GET", JOB_PATH, headers={"If-None-Match": '"2"', "Prefer": "wait=0"})
        assert unchanged.status == 304
        assert unchanged.body == b""

        later = {"job_id": JOB_ID, "state": "finished", "summary": "Finished."}
        with background_request(server, service, "GET", f"{JOB_PATH}/events", headers={"Last-Event-ID": "event-1"}) as pending:
            assert events_wait.entered.wait(scaled_time_bound(10)), "SSE never entered its service wait"
            assert not pending.done(), "Event stream completed before later events were published"
            events_wait.publish(JobEvents((JobEvent("event-2", after.status), JobEvent("event-3", later)), True))
            streamed = pending.result(timeout=scaled_time_bound(10))
        assert streamed.status == 200
        assert streamed.headers["content-type"].split(";", 1)[0] == "text/event-stream"
        frames = streamed.body.decode("utf-8").replace("\r\n", "\n").split("\n\n")
        parsed = []
        for frame in frames:
            fields = [line.partition(":") for line in frame.splitlines() if line and not line.startswith(":")]
            ids = [value.strip() for name, _, value in fields if name == "id"]
            data = [value.lstrip() for name, _, value in fields if name == "data"]
            if data:
                assert len(ids) == 1
                parsed.append((ids[0], json.loads("\n".join(data))))
        assert parsed == [("event-2", after.status), ("event-3", later)]
        calls = [call for call in service.calls if call.method == "events_after"]
        assert calls[0].args == (JOB_ID,)
        assert calls[0].kwargs["last_event_id"] == "event-1"
        assert 0 < calls[0].kwargs["timeout_s"] <= settings.sse_keepalive_s


def test_withdrawal_token_and_state_responses() -> None:
    service = ScriptedJobService()
    with running_http(service) as server:
        for path in (JOB_PATH, f"{JOB_PATH}?job_token={TOKEN}"):
            response = request(server, "DELETE", path)
            assert response.status == 401
            assert response.headers["www-authenticate"] == "Bearer"
            assert response.json()["error_type"] == "job_token_missing"
            assert service.calls == []

        wrong = service_error("job_token_invalid")
        service.script("withdraw", wrong)
        response = request(server, "DELETE", JOB_PATH, headers={"Authorization": "Bearer wrong-token"})
        assert response.status == 403
        assert response.json() == wrong.to_dict()
        assert service.calls[-1] == Call("withdraw", (JOB_ID,), {"job_token": "wrong-token"})

        queued = {"job_id": JOB_ID, "state": "withdrawn", "started": False}
        running = {"job_id": JOB_ID, "state": "withdrawing", "started": True}
        service.script("withdraw", queued, running)
        response = request(server, "DELETE", JOB_PATH, headers=BEARER)
        assert response.status == 204
        assert response.body == b""
        response = request(server, "DELETE", JOB_PATH, headers=BEARER)
        assert response.status == 202
        assert response.json() == running
        assert service.calls[-1] == Call("withdraw", (JOB_ID,), {"job_token": TOKEN})

        # The ended refusals by the transport's own set, so a name harmonized
        # with the scheduler's (#891) is the name this proves.
        for error_type in sorted(httpserver._ENDED_ERRORS):
            error = service_error(error_type)
            service.script("withdraw", error)
            response = request(server, "DELETE", JOB_PATH, headers=BEARER)
            assert response.status == 409
            assert response.json() == error.to_dict()


def test_idempotency_key_is_forwarded() -> None:
    service = ScriptedJobService()
    accepted = submission()
    service.script("submit", accepted, accepted)
    body, content_type = multipart()
    headers = {"Content-Type": content_type, "Idempotency-Key": "example-repeat-key", "From": "submitter@example.invalid"}
    with running_http(service) as server:
        for _ in range(2):
            response = request(server, "POST", "/v1/jobs", body=body, headers=headers)
            assert response.status == 202
            assert response.json() == accepted
            assert response.headers["location"] == JOB_PATH
    expected = Call("submit", (PLAN, {"build/app.elf": IMAGE}), {
        "idempotency_key": "example-repeat-key", "submitted_by": "submitter@example.invalid",
    })
    assert service.calls == [expected, expected]


def test_origin_and_host_guard_every_endpoint() -> None:
    service = ScriptedJobService()
    form, form_type = multipart()
    routes = [
        ("POST", "/v1/jobs", form, form_type),
        ("POST", "/v1/plans/check", PLAN, "application/yaml"),
        ("PUT", ARTIFACT_PATH, IMAGE, "application/octet-stream"),
        ("GET", JOB_PATH, None, None), ("GET", f"{JOB_PATH}/events", None, None),
        ("DELETE", JOB_PATH, None, None), ("GET", f"{JOB_PATH}/report", None, None),
        ("GET", f"{JOB_PATH}/junit", None, None), ("GET", f"{JOB_PATH}/evidence", None, None),
        ("GET", f"{JOB_PATH}/logs/0", None, None),
    ]
    with running_http(service) as server:
        for method, path, body, content_type in routes:
            for foreign, error_type in (
                ({"Origin": "https://foreign.example.invalid"}, "origin_refused"),
                ({"Host": "foreign.example.invalid"}, "host_refused"),
            ):
                headers = {**BEARER, **foreign}
                if content_type is not None:
                    headers["Content-Type"] = content_type
                response = request(server, method, path, body=body, headers=headers, withhold_body=body is not None)
                assert response.status == 403, (method, path, response)
                assert response.json()["error_type"] == error_type
                no_cors(response)
                assert service.calls == []

    public = {"job_id": JOB_ID, "state": "queued", "summary": "Queued."}
    service.script("status", JobStatus(1, public), JobStatus(1, public))
    settings = short_settings(hostnames=("bench.example.invalid",), allowed_origins=("https://client.example.invalid",))
    with running_http(service, settings) as server:
        for headers in ({}, {"Origin": "https://client.example.invalid", "Host": "bench.example.invalid"}):
            response = request(server, "GET", JOB_PATH, headers=headers)
            assert response.status == 200
            assert response.json() == public
            no_cors(response)
    assert service.calls == [Call("status", (JOB_ID,), {}), Call("status", (JOB_ID,), {})]
