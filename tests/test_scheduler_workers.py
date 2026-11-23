"""Actual reactor processes for queue contention, stopping and worker death."""

from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from pathlib import Path

import pytest
from bench_job_fakes import wait_until
from support import TIME_SCALE_VARIABLE, scaled_time_bound
from test_scheduler import delay, make_bench, status, submit

from agentic_hil import runlifecycle
from agentic_hil.bench import BenchMutex
from agentic_hil.process import terminate_process_tree
from agentic_hil.report import audit_logs_directory, canonical_run_report_path
from agentic_hil.scheduler import Scheduler


@contextmanager
def owned_workers(monkeypatch):
    """A transparent capture of the real spawn, with teardown by owned handle."""
    children = {}
    original = runlifecycle.spawn_run_worker
    source = str(Path(__file__).resolve().parents[1] / "src")
    monkeypatch.setenv("PYTHONPATH", source + os.pathsep + os.environ.get("PYTHONPATH", ""))
    monkeypatch.setenv(TIME_SCALE_VARIABLE, str(max(3, scaled_time_bound(1))))

    def spawn(config, handle, test_config_path, *, wait_s):
        assert wait_s == 0
        child = original(config, handle, test_config_path, wait_s=wait_s)
        children[handle] = child
        return child

    monkeypatch.setattr(runlifecycle, "spawn_run_worker", spawn)
    try:
        yield children
    finally:
        for child in children.values():
            try:
                if child.poll() is None:
                    terminate_process_tree(child, scaled_time_bound(10), graceful=False)
            finally:
                child.wait(timeout=scaled_time_bound(30))


def drive_until(scheduler, predicate, *, description):
    """Wall time bounds child readiness only; scheduler deadlines use its clock."""
    deadline = time.monotonic() + scaled_time_bound(60)
    while True:
        scheduler.tick()
        result = predicate()
        if result:
            return result
        assert time.monotonic() < deadline, description
        time.sleep(0.05)


def record_for(bench, scheduler, job):
    handle = status(scheduler, job).get("attempt_handle")
    return runlifecycle.read_run_record(bench.config, handle) if handle else None


def reached_step(bench, scheduler, job, step):
    record = record_for(bench, scheduler, job) or {}
    return record if record.get("state") == "running" and (record.get("progress") or {}).get("step") == step else None


def actual_scheduler(bench, **kwargs):
    # The default adapter must run master's worker, never the controlled fake.
    return Scheduler(bench.config, bench.settings, clock=bench.clock, **kwargs)


def test_refused_start_returns_to_original_queue_entry(tmp_path, monkeypatch):
    bench = make_bench(tmp_path, monkeypatch)
    plan = bench.plan("contended", [{"action": "reset", "device": "dut"}, delay("other")])
    keys = bench.keys(plan)
    holder = BenchMutex(frontend="local-cli", label="local owner")
    holder.acquire([keys[-1]], wait_s=0)
    other = BenchMutex(frontend="test-observer")
    try:
        # Deliberately stale advisory source: the worker must discover the lock.
        scheduler = actual_scheduler(bench, holder_source=bench.holders, incident_source=bench.incidents)
        with owned_workers(monkeypatch) as children:
            job = submit(scheduler, plan)
            original = status(scheduler, job)

            def refused():
                record = record_for(bench, scheduler, job) or {}
                return record if record.get("error_type") == "device_busy" and runlifecycle.worker_is_gone(bench.config, record["run"]) else None

            record = drive_until(scheduler, refused, description="worker never published its actual device_busy refusal")
            drive_until(scheduler, lambda: status(scheduler, job)["state"] == "queued",
                        description="refused start did not return to the queue")
            view = status(scheduler, job)
            assert (view["sequence"], view["queued_at"], view["queue_deadline"]) == (
                original["sequence"], original["queued_at"], original["queue_deadline"],
            )
            assert view["started"] is False
            assert holder.holds(keys[-1])
            other.acquire(keys[:-1], wait_s=0)
            other.release_all()
            report = json.loads(canonical_run_report_path(bench.config, record["run"]).read_text(encoding="utf-8"))
            assert report["error_type"] == "device_busy"
            assert report["steps"] == []
            assert report["cleanup"] == []
            assert record["run"] in children
            children[record["run"]].wait(timeout=scaled_time_bound(30))
    finally:
        other.release_all()
        holder.release_all()


@pytest.mark.parametrize("outcome", ["runtime_exceeded", "withdrawn"], ids=["runtime_exceeded", "withdrawn"])
def test_running_terminal_outcomes(tmp_path, monkeypatch, outcome):
    bench = make_bench(tmp_path, monkeypatch)
    plan = bench.plan("running", [
        {"action": "reset", "device": "dut"}, delay(seconds=600),
        {"action": "reset", "device": "dut"},
    ])
    scheduler = actual_scheduler(bench)
    with owned_workers(monkeypatch) as children:
        job = submit(scheduler, plan)
        record = drive_until(scheduler, lambda: reached_step(bench, scheduler, job, 2),
                             description="worker never finished reset and entered its interruptible delay")
        handle = record["run"]
        assert handle in children
        before = status(scheduler, job)
        with pytest.raises(PermissionError):
            scheduler.withdraw(job.job_id, submitter_label="administrator")
        assert status(scheduler, job) == before
        assert not runlifecycle.stop_path(bench.config, handle).exists()
        if outcome == "runtime_exceeded":
            bench.clock.advance(bench.settings.max_job_runtime_s + 1)
            scheduler.tick()
        else:
            scheduler.withdraw(job.job_id, submitter_label="alice")
            assert status(scheduler, job)["state"] == "withdrawing"
        assert runlifecycle.stop_path(bench.config, handle).exists()
        drive_until(scheduler, lambda: status(scheduler, job)["state"] == outcome,
                    description=f"worker did not settle to {outcome}")
        children[handle].wait(timeout=scaled_time_bound(30))
        view = status(scheduler, job)
        assert view["started"] is True
        canonical = canonical_run_report_path(bench.config, handle)
        canonical_bytes = canonical.read_bytes()
        report = json.loads(canonical_bytes)
        assert view["canonical_report_path"] == str(canonical)
        assert Path(view["report_path"]).is_file()
        assert report["stopped"] is True
        assert report["cleanup_ok"] is True
        assert report["steps"][0]["action"] == "reset"
        assert report["steps"][0]["result"]["ok"] is True
        assert len([step for step in report["steps"] if step["action"] == "reset"]) == 1
        assert runlifecycle.worker_is_gone(bench.config, handle)
        if outcome == "runtime_exceeded":
            # Seed a test-owned existing line; retention must preserve its bytes.
            retention_ledger = audit_logs_directory(bench.config) / "retention-probe.jsonl"
            retention_ledger.write_text('{"fixture": "retention", "event": "existing-line"}\n', encoding="utf-8")
            state_root = Path(bench.config.state_root)
            ledger = {p: p.read_bytes() for p in state_root.rglob("*.jsonl")}
            for name in ("plan", "artifacts", "workspace", "report", "logs"):
                assert (job.directory / name).is_dir()
            bench.clock.advance(bench.settings.retention_days * 86400 + 1)
            scheduler.tick()
            assert not any((job.directory / name).exists() for name in ("plan", "artifacts", "workspace", "report", "logs"))
            assert canonical.read_bytes() == canonical_bytes
            assert all(path.read_bytes().startswith(content) for path, content in ledger.items())


def test_killed_worker_releases_devices_for_successor(tmp_path, monkeypatch):
    bench = make_bench(tmp_path, monkeypatch)
    plan = bench.plan("killed", [{"action": "reset", "device": "dut"}, delay(seconds=600)])
    successor_plan = bench.plan("successor", [{"action": "reset", "device": "dut"}])
    scheduler = actual_scheduler(bench)
    observer = BenchMutex(frontend="test-observer")
    with owned_workers(monkeypatch) as children:
        try:
            job = submit(scheduler, plan)
            record = drive_until(scheduler, lambda: reached_step(bench, scheduler, job, 2),
                                 description="worker never completed its first scripted device step")
            handle = record["run"]
            successor = submit(scheduler, successor_plan, "bob")
            scheduler.tick()
            assert status(scheduler, successor)["state"] == "queued"
            child = children[handle]
            terminate_process_tree(child, scaled_time_bound(10), graceful=False)
            child.wait(timeout=scaled_time_bound(30))
            wait_until(lambda: runlifecycle.worker_is_gone(bench.config, handle), "killed worker lifetime lock release")
            # This observer is the test, not a scheduler availability probe.
            observer.acquire(bench.keys(plan), wait_s=0)
            observer.release_all()
            assert bench.clock() == 1000.0
            drive_until(scheduler, lambda: status(scheduler, job)["state"] == "worker_gone",
                        description="killed worker was not reconciled")
            drive_until(scheduler, lambda: status(scheduler, successor)["state"] == "finished",
                        description="successor did not complete through existing recovery")
            successor_record = record_for(bench, scheduler, successor)
            assert successor_record["state"] == "finished"
            next_handle = successor_record["run"]
            assert next_handle != handle
            children[next_handle].wait(timeout=scaled_time_bound(30))
            report = json.loads(canonical_run_report_path(bench.config, next_handle).read_text(encoding="utf-8"))
            assert report["ok"] is True, report
            assert report["cleanup_ok"] is True
            partial = json.loads(Path(status(scheduler, job)["report_path"]).read_text(encoding="utf-8"))
            assert partial["run_record"]["progress"]["step"] == 2
            assert partial["cleanup_confirmed"] is False
        finally:
            observer.release_all()
