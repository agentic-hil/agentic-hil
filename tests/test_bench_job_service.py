"""The bench server's job service on the real job store and scheduler (#891).

Every test runs the adapter over the real `JobStore` and `Scheduler` of a
version 4 bench. The worker source is the one fake (tests/bench_job_fakes.py),
so no process is started and no device is reached, except in the one test that
runs the real `serverjobs` worker on a plan that only waits.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import zipfile
from concurrent.futures import Future
from pathlib import Path
from threading import Event, Thread
from typing import Any

import pytest
from bench_job_fakes import (
    IMAGE,
    IMAGE_PATH,
    LOG_BYTES,
    TERMINAL,
    FakeWorkerSource,
    job_bench,
    open_service,
    plan,
    serving,
    stored_jobs,
    wait_for_state,
    wait_until,
)
from httpserver_fakes import request
from support import scaled_time_bound

from agentic_hil import httpserver
from agentic_hil.benchjobs import JobWorkerSource, JobWorkspacePreparer
from agentic_hil.config import ConfigError
from agentic_hil.httpapi import ByteRange, JobServiceError
from agentic_hil.jobstore import Job, JobStore
from agentic_hil.knowledge import ERROR_CATALOGUE, lookup_remedy, remediation_fields
from agentic_hil.report import canonical_run_report_path
from agentic_hil.types import JsonObject

REPO = Path(__file__).resolve().parents[1]
# What anyone may read of anyone's job in the jobs list.
PUBLIC_JOB_FIELDS = {"job_id", "user", "plan_name", "plan_digest", "devices", "state", "duration_s", "ok", "error_type"}


def submit(service: Any, plan_bytes: bytes, user: str, *, artifacts: dict[str, bytes] | None = None, key: str | None = None) -> JsonObject:
    return service.submit(plan_bytes, artifacts or {}, idempotency_key=key, submitted_by=user)


def refusal(call: Any) -> JobServiceError:
    """The JobServiceError `call` raises."""
    with pytest.raises(JobServiceError) as refused:
        call()
    return refused.value


def test_submit_and_check_plan_go_through_the_real_scheduler(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bench = job_bench(tmp_path, monkeypatch)
    workers = FakeWorkerSource()
    with open_service(bench, workers) as service:
        checked = service.check_plan(plan(name="held checked"), {}, submitted_by="alice")
        assert checked["ok"] is True, checked
        assert checked["devices"] == ["dut"]
        assert checked["would_start"] is True
        # Checking created no job.
        assert stored_jobs(bench) == {}

        submitted = submit(service, plan(name="held first"), "alice")
        job_id, token = submitted["job_id"], submitted["job_token"]
        assert submitted["state"] in {"queued", "starting", "running"}, submitted
        assert submitted["missing_artifacts"] == []
        record = stored_jobs(bench)[job_id]
        # Only a digest of the token is kept, never the token.
        assert record["token_sha256"] == hashlib.sha256(token.encode("utf-8")).hexdigest()
        assert token not in (bench.job_root / "queue.json").read_text(encoding="utf-8")
        assert record["submitter_label"] == "alice"
        running = wait_for_state(service, job_id, {"running"})
        assert running["user"] == "alice"
        assert running["devices"] == ["dut"]
        assert token not in json.dumps(running)

        # A second plan for the same board is told whose job it would wait behind.
        behind = service.check_plan(plan(name="held second"), {}, submitted_by="bob")
        assert behind["would_start"] is False
        assert {"device": "dut", "reason": "held", "held_by": {"job_id": job_id}} in behind["waiting_for"]

        # A refused plan is refused the same way by both, and leaves no job behind.
        refused_plan = plan(name="held refused", device="undeclared")
        submitted_refusal = refusal(lambda: submit(service, refused_plan, "alice"))
        checked_refusal = refusal(lambda: service.check_plan(refused_plan, {}, submitted_by="alice"))
        assert "validation_error" in submitted_refusal.details
        assert checked_refusal.error_type == submitted_refusal.error_type
        assert set(stored_jobs(bench)) == {job_id}
        assert sorted(path.name for path in bench.job_root.iterdir() if path.is_dir()) == [job_id]


def test_a_foreign_token_or_none_cannot_reach_another_submitters_job(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bench = job_bench(tmp_path, monkeypatch)
    workers = FakeWorkerSource()
    with open_service(bench, workers) as service, serving(service) as server:
        yaml_body = {"Content-Type": "application/yaml"}
        own = request(server, "POST", "/v1/jobs", body=plan(name="alice's"), headers={**yaml_body, "From": "alice"})
        other = request(server, "POST", "/v1/jobs", body=plan(name="bob's", device="other"), headers={**yaml_body, "From": "bob"})
        assert own.status == other.status == 202, (own, other)
        job_id, token = own.json()["job_id"], own.json()["job_token"]
        foreign_token = other.json()["job_token"]
        assert wait_for_state(service, job_id, TERMINAL)["state"] == "finished"

        catalogue = remediation_fields("job_token_invalid")
        for method, suffix in (("DELETE", ""), ("GET", "/report"), ("GET", "/junit"), ("GET", "/evidence"), ("GET", "/logs/0")):
            path = f"/v1/jobs/{job_id}{suffix}"
            foreign = request(server, method, path, headers={"Authorization": f"Bearer {foreign_token}"})
            assert foreign.status == 403, (method, path, foreign)
            answered = foreign.json()
            assert answered["error_type"] == "job_token_invalid", (method, path, answered)
            assert {key: answered.get(key) for key in catalogue} == catalogue, answered
            assert foreign_token not in foreign.body.decode("utf-8")
            missing = request(server, method, path)
            assert missing.status == 401, (method, path, missing)
            assert missing.json()["error_type"] == "job_token_missing"

        # Nothing a foreign caller tried changed the job, and its own token still reads it.
        assert service.status(job_id).status["state"] == "finished"
        mine = request(server, "GET", f"/v1/jobs/{job_id}/report", headers={"Authorization": f"Bearer {token}"})
        assert mine.status == 200, mine
        assert mine.json()["ok"] is True


def test_a_job_runs_to_its_report_junit_evidence_and_logs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bench = job_bench(tmp_path, monkeypatch)
    workers = FakeWorkerSource()
    with open_service(bench, workers) as service:
        submitted = submit(service, plan(name="passing"), "alice")
        job_id, token = submitted["job_id"], submitted["job_token"]
        final = wait_for_state(service, job_id, TERMINAL)
        assert final["state"] == "finished", final
        assert final["ok"] is True

        # The events end with the job; an ended job has no next revision to wait for.
        batch = service.events_after(job_id, last_event_id=None, timeout_s=scaled_time_bound(5))
        assert batch.complete is True
        assert batch.events and batch.events[-1].status["state"] == "finished"
        assert service.wait_for_revision(job_id, after_revision=service.status(job_id).revision, timeout_s=0.01) is None

        report = service.report(job_id, job_token=token)
        assert report["ok"] is True
        assert report["name"] == "passing"
        assert b"<testsuite" in service.junit(job_id, job_token=token)
        evidence = service.evidence(job_id, job_token=token)
        assert evidence.media_type == "application/zip"
        assert evidence.filename == f"{job_id}-evidence.zip"
        names = zipfile.ZipFile(io.BytesIO(evidence.body)).namelist()
        assert any(name.endswith("run-summary.json") for name in names), names

        whole = service.log(job_id, 0, job_token=token, byte_range=None, follow=False, timeout_s=0.0)
        assert (whole.body, whole.first, whole.total_size, whole.complete) == (LOG_BYTES, 0, len(LOG_BYTES), True)
        head = service.log(job_id, 0, job_token=token, byte_range=ByteRange(0, 3), follow=False, timeout_s=0.0)
        assert head.body == LOG_BYTES[:4]
        beyond = refusal(lambda: service.log(job_id, 0, job_token=token, byte_range=ByteRange(10_000, None), follow=False, timeout_s=0.0))
        assert beyond.error_type == "range_not_satisfiable"
        assert beyond.details["total_size"] == len(LOG_BYTES)
        absent = refusal(lambda: service.log(job_id, 5, job_token=token, byte_range=None, follow=False, timeout_s=0.0))
        assert absent.error_type == "job_not_found"


def test_uploads_complete_a_job_that_awaits_artifacts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bench = job_bench(tmp_path, monkeypatch)
    workers = FakeWorkerSource()
    plan_bytes = plan(name="passing upload", image=IMAGE_PATH)
    with open_service(bench, workers) as service:
        submitted = submit(service, plan_bytes, "alice")
        job_id, token = submitted["job_id"], submitted["job_token"]
        assert submitted["state"] == "awaiting_artifacts", submitted
        assert submitted["missing_artifacts"] == [IMAGE_PATH]

        assert refusal(lambda: service.upload_artifact(job_id, "build/other.elf", IMAGE, job_token=token)).error_type == "artifact_unreferenced"
        assert refusal(lambda: service.upload_artifact(job_id, IMAGE_PATH, IMAGE, job_token="not-this-jobs-token")).error_type == "job_token_invalid"
        uploaded = service.upload_artifact(job_id, IMAGE_PATH, IMAGE, job_token=token)
        assert uploaded["path"] == IMAGE_PATH
        assert uploaded["sha256"] == hashlib.sha256(IMAGE).hexdigest()
        assert uploaded["size_bytes"] == len(IMAGE)
        assert uploaded["missing_artifacts"] == []
        assert refusal(lambda: service.upload_artifact(job_id, IMAGE_PATH, IMAGE, job_token=token)).error_type == "artifact_already_uploaded"

        # The job runs on its own workspace, holding exactly the bytes that were sent.
        workspace = bench.job_root / job_id / "workspace"
        assert (workspace / IMAGE_PATH).read_bytes() == IMAGE
        assert (workspace / ".agentic-hil" / "testconfig.yaml").read_bytes() == plan_bytes
        assert wait_for_state(service, job_id, TERMINAL)["state"] == "finished"


def test_withdraw_answers_by_the_state_the_job_is_in(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bench = job_bench(tmp_path, monkeypatch)
    workers = FakeWorkerSource()
    with open_service(bench, workers) as service:
        first = submit(service, plan(name="held running"), "alice")
        running_id, running_token = first["job_id"], first["job_token"]
        wait_for_state(service, running_id, {"running"})
        assert refusal(lambda: service.report(running_id, job_token=running_token)).error_type == "job_not_ended"

        second = submit(service, plan(name="held queued"), "bob")
        queued_id, queued_token = second["job_id"], second["job_token"]
        assert service.status(queued_id).status["state"] == "queued"
        left = service.withdraw(queued_id, job_token=queued_token)
        assert left["started"] is False, left
        assert left["state"] == "withdrawn"
        assert refusal(lambda: service.withdraw(queued_id, job_token=queued_token)).error_type == "job_withdrawn"

        stopping = service.withdraw(running_id, job_token=running_token)
        assert stopping["started"] is True, stopping
        assert stopping["state"] == "withdrawing"
        assert wait_for_state(service, running_id, TERMINAL)["state"] == "withdrawn"
        assert workers.handle_of(running_id) in workers.stops
        assert refusal(lambda: service.withdraw(running_id, job_token=running_token)).error_type == "job_withdrawn"

        third = submit(service, plan(name="passing", device="other"), "carol")
        assert wait_for_state(service, third["job_id"], TERMINAL)["state"] == "finished"
        assert refusal(lambda: service.withdraw(third["job_id"], job_token=third["job_token"])).error_type == "job_finished"


def test_reopening_keeps_jobs_tokens_and_idempotent_replays(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bench = job_bench(tmp_path, monkeypatch)
    workers = FakeWorkerSource()
    kept = plan(name="held kept")
    with open_service(bench, workers) as service:
        first = submit(service, kept, "alice", key="repeat-key")
        assert submit(service, kept, "alice", key="repeat-key") == first
        other = submit(service, plan(name="held other key", device="other"), "alice", key="another-key")
        assert other["job_id"] != first["job_id"]
        wait_for_state(service, first["job_id"], {"running"})
    handle = workers.handle_of(first["job_id"])
    # Closing the service ended no worker.
    assert workers.terminations == []
    assert handle not in workers.stops

    with open_service(bench, workers) as reopened:
        replay = submit(reopened, kept, "alice", key="repeat-key")
        assert replay["job_id"] == first["job_id"]
        assert replay["job_token"] is None
        assert set(stored_jobs(bench)) == {first["job_id"], other["job_id"]}
        # The attempt an earlier server started is asked to stop, not adopted.
        assert wait_for_state(reopened, first["job_id"], TERMINAL)["state"] == "worker_gone"
        assert handle in workers.stops
        # The token issued before the restart still reads the job.
        report = reopened.report(first["job_id"], job_token=first["job_token"])
        assert report["ok"] is False


def test_queue_jobs_device_activity_metrics_and_drain(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bench = job_bench(tmp_path, monkeypatch)
    workers = FakeWorkerSource()
    with open_service(bench, workers) as service:
        holding = submit(service, plan(name="held a"), "alice")["job_id"]
        wait_for_state(service, holding, {"running"})
        waiting = submit(service, plan(name="held b"), "bob")["job_id"]

        entries = service.queue().entries
        assert [entry["job_id"] for entry in entries] == [waiting]
        assert entries[0]["queue_position"] == 1
        assert {"device": "dut", "reason": "held", "held_by": {"job_id": holding}} in entries[0]["waiting_for"]

        assert [job["job_id"] for job in service.jobs(state=None, device="dut", user="bob", since=None)] == [waiting]
        listed = service.jobs(state=None, device=None, user=None, since=None)
        assert [job["job_id"] for job in listed] == [waiting, holding]
        assert all(set(job) <= PUBLIC_JOB_FIELDS for job in listed), listed
        assert refusal(lambda: service.jobs(state=None, device=None, user=None, since="not a time")).error_type == "invalid_argument"

        activity = service.device_activity()
        assert activity["dut"].state == "held"
        assert activity["dut"].job_id == holding
        metrics = service.metrics()
        assert "agentic_hil_queue_length 1" in metrics
        assert 'agentic_hil_device_busy{device="dut"} 1' in metrics

        drained = service.drain("other")
        assert drained["device"] == "other"
        assert drained["drained"] is True
        parked = submit(service, plan(name="held c", device="other"), "carol")["job_id"]
        status = service.status(parked).status
        assert status["state"] == "queued"
        assert {"device": "other", "reason": "drained"} in status["waiting_for"]
        assert service.device_activity()["other"].drained is True

        assert refusal(lambda: service.drain("no-such-device")).error_type == "device_not_found"
        assert refusal(lambda: service.recover("no-such-device", operator_statement="checked", accept_config_change=False)).error_type == "device_not_found"


def test_artifacts_that_never_arrive_expire_the_job(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bench = job_bench(tmp_path, monkeypatch, limits={"max_artifact_wait_s": 1})
    workers = FakeWorkerSource()
    with open_service(bench, workers) as service:
        submitted = submit(service, plan(name="passing never uploaded", image=IMAGE_PATH), "alice")
        job_id, token = submitted["job_id"], submitted["job_token"]
        assert submitted["state"] == "awaiting_artifacts"
        final = wait_for_state(service, job_id, TERMINAL)
        assert final["state"] == "expired", final
        assert final["error_type"] == "artifact_missing"
        report = service.report(job_id, job_token=token)
        assert report["ok"] is False
        assert report["error_type"] == "artifact_missing"
        assert report["job_id"] == job_id
        assert (report["submitted_by"], report["submitted_by_source"]) == ("alice", "client")
        assert refusal(lambda: service.withdraw(job_id, job_token=token)).error_type == "job_expired"
        assert refusal(lambda: service.upload_artifact(job_id, IMAGE_PATH, IMAGE, job_token=token)).error_type == "job_expired"
        assert workers.starts == []


def test_recover_runs_outside_the_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A recover that waits on its device holds up no status read and no upload."""
    bench = job_bench(tmp_path, monkeypatch)
    workers = FakeWorkerSource()
    entered, release = Event(), Event()
    asked: list[dict[str, Any]] = []

    def blocking_recover(*args: Any, **kwargs: Any) -> JsonObject:
        asked.append(kwargs)
        entered.set()
        assert release.wait(scaled_time_bound(15)), "the test never released the recover call"
        return {"ok": True, "tool": "hardware_recover", "nothing_to_recover": True}

    with open_service(bench, workers, recover_call=blocking_recover) as service:
        awaiting = submit(service, plan(name="passing upload", image=IMAGE_PATH), "alice")
        recovered: Future[JsonObject] = Future()
        meanwhile: Future[tuple[JsonObject, JsonObject]] = Future()

        def recover() -> None:
            try:
                recovered.set_result(service.recover("dut", operator_statement="checked the board", accept_config_change=False))
            except BaseException as error:
                recovered.set_exception(error)

        def read_and_upload() -> None:
            try:
                status = service.status(awaiting["job_id"]).status
                uploaded = service.upload_artifact(awaiting["job_id"], IMAGE_PATH, IMAGE, job_token=awaiting["job_token"])
                meanwhile.set_result((status, uploaded))
            except BaseException as error:
                meanwhile.set_exception(error)

        recovering = Thread(target=recover, daemon=True)
        recovering.start()
        try:
            assert entered.wait(scaled_time_bound(10)), "recover never reached its tool call"
            other = Thread(target=read_and_upload, daemon=True)
            other.start()
            other.join(scaled_time_bound(10))
            assert not other.is_alive(), "a status read and an upload waited for a recover call"
            status, uploaded = meanwhile.result()
            assert status["state"] == "awaiting_artifacts"
            assert uploaded["missing_artifacts"] == []
            assert not recovered.done()
        finally:
            release.set()
            recovering.join(scaled_time_bound(15))
        assert recovered.result() == {"ok": True, "tool": "hardware_recover", "nothing_to_recover": True}
        assert asked and asked[0]["operator_statement"] == "checked the board"
        assert asked[0]["accept_config_change"] is False


def test_the_job_workspace_is_placed_in_the_directory_the_store_created(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bench = job_bench(tmp_path, monkeypatch)
    preparer = JobWorkspacePreparer(bench.config)
    store = JobStore(bench.job_root)
    created = store.create("testconfig.yaml")
    workspace = created.directory / "workspace"
    # The store makes the workspace directory; preparation fills it.
    assert workspace.is_dir()
    plan_bytes = plan(name="placed", image=IMAGE_PATH)
    created.plan_path.write_bytes(plan_bytes)
    staged = created.directory / "artifacts" / "build" / "app.elf"
    staged.parent.mkdir(parents=True, exist_ok=True)
    staged.write_bytes(IMAGE)
    job = Job(created.job_id, created.directory, created.plan_path, {IMAGE_PATH: staged})

    config, plan_path = preparer.prepare(job)
    assert Path(config.work_dir) == workspace
    assert Path(preparer.config_for(job).work_dir) == workspace
    assert Path(plan_path) == workspace / ".agentic-hil" / "testconfig.yaml"
    assert Path(plan_path).read_bytes() == plan_bytes
    assert (workspace / IMAGE_PATH).read_bytes() == IMAGE
    # Preparing again verifies what was placed and answers the same.
    again, again_plan = preparer.prepare(job)
    assert (Path(again.work_dir), Path(again_plan)) == (workspace, Path(plan_path))

    (workspace / IMAGE_PATH).write_bytes(b"\x7fELFsomething else")
    with pytest.raises(ConfigError) as changed:
        preparer.prepare(job)
    assert changed.value.error_type == "job_input_invalid"

    # A workspace something else wrote into is not a fresh one.
    stray = store.create("testconfig.yaml")
    stray.plan_path.write_bytes(plan(name="stray"))
    (stray.directory / "workspace" / "left-behind.txt").write_text("not placed by a preparation\n", encoding="utf-8")
    with pytest.raises(ConfigError) as occupied:
        preparer.prepare(Job(stray.job_id, stray.directory, stray.plan_path, {}))
    assert occupied.value.error_type == "job_input_invalid"


def test_the_job_worker_writes_the_self_declared_submitter_into_the_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The real worker, on a plan that only waits: what the client said it is reaches the report, marked as said."""
    bench = job_bench(tmp_path, monkeypatch)
    source = JobWorkerSource(bench.config, submitter_of=lambda job_id: "carol")
    preparer = JobWorkspacePreparer(bench.config)
    store = JobStore(bench.job_root)
    created = store.create("testconfig.yaml")
    created.plan_path.write_bytes(plan(name="worker run"))
    job = Job(created.job_id, created.directory, created.plan_path, {})
    config, plan_path = preparer.prepare(job)
    # The worker is `python -m agentic_hil.serverjobs`, run from this checkout's sources.
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(filter(None, (str(REPO / "src"), os.environ.get("PYTHONPATH")))))

    handle = source.new_handle()
    source.start(config, handle, Path(plan_path), wait_s=0)
    source.perform_pending()
    wait_until(lambda: source.is_gone(config, handle), "the worker's end")

    record = source.record(config, handle)
    assert record is not None and record["state"] in {"finished", "stopped"}, record
    report = json.loads(canonical_run_report_path(config, handle).read_text(encoding="utf-8"))
    assert report["job_id"] == created.job_id
    assert report["submitted_by"] == "carol"
    assert report["submitted_by_source"] == "client"


def test_each_harmonized_refusal_has_one_name() -> None:
    """One name for the per-submitter limit and one for the runtime cap: the scheduler's (#891)."""
    texts = {name: (REPO / "src" / "agentic_hil" / name).read_text(encoding="utf-8") for name in ("httpserver.py", "knowledge.py")}
    if any(old in text for text in texts.values() for old in ("per_user_limit", "job_runtime_exceeded")):
        raise AssertionError("per_user_limit and job_runtime_exceeded are still refusal names")
    assert "plan_runtime_exceeded" in httpserver._ENDED_ERRORS
    assert httpserver._ERROR_STATUS["max_queued_jobs_per_user"] == 429
    assert httpserver._ERROR_STATUS["job_not_ended"] == 409
    for name in ("max_queued_jobs_per_user", "plan_runtime_exceeded", "job_not_ended"):
        assert lookup_remedy(name) is not None, name
    assert not {"per_user_limit", "job_runtime_exceeded", "job_store_unavailable"} & set(ERROR_CATALOGUE)
