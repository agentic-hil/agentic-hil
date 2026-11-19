"""The fair queue, driven explicitly with controlled observations and time."""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import pytest
import yaml
from conftest import FAKE_OPENOCD, write_authoritative_config
from test_test_reactor import RecordingService

from agentic_hil.bench import BenchMutex, BenchOwner, resource_digest
from agentic_hil.config import load_authoritative_config
from agentic_hil.jobstore import SchedulerSettings
from agentic_hil.scheduler import BenchHolderSource, Scheduler
from agentic_hil.test_reactor import TestReactor, load_test_config, plan_devices
from agentic_hil.types import AgenticHILConfig


@dataclass
class Clock:
    now: float = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@dataclass
class Holders:
    records: dict = field(default_factory=dict)

    def holder(self, lock_key):
        return copy.deepcopy(self.records.get(lock_key))

    def hold(self, keys):
        for key in keys:
            self.records[key] = {
                "state": "held", "resource": key,
                "acquired_at": "2026-01-01T00:00:00+00:00",
                "heartbeat_at": "2026-01-01T00:00:00+00:00",
                "owner": {"frontend": "local-cli", "label": "scripted holder"},
            }

    def release(self, keys):
        for key in keys:
            self.records.pop(key, None)


@dataclass
class Incidents:
    reasons: dict = field(default_factory=dict)
    reads: list = field(default_factory=list)

    def standing(self, lock_keys: Sequence[str]) -> Mapping[str, dict]:
        self.reads.append(tuple(lock_keys))
        return {key: copy.deepcopy(self.reasons[key]) for key in lock_keys if key in self.reasons}


class Workers:
    """Only worker observations. No queue, admission or reservation decisions."""

    def __init__(self):
        self.records = {}
        self.gone = set()
        self.starts = []
        self.stops = []
        self.terminations = []
        self.allocated = 0
        self.publish_running = True

    def new_handle(self):
        self.allocated += 1
        return f"run-{self.allocated:016x}"

    def start(self, config, handle, plan, *, wait_s):
        assert wait_s == 0
        self.starts.append((handle, config, Path(plan), wait_s))
        self.records[handle] = {
            "state": "running" if self.publish_running else "starting",
            "run": handle,
        }

    def record(self, config, handle):
        return copy.deepcopy(self.records.get(handle))

    def is_gone(self, config, handle):
        return handle in self.gone

    def stop(self, config, handle):
        self.stops.append(handle)

    def terminate(self, config, handle):
        # Death is a separate observation, deliberately controlled by the test.
        self.terminations.append(handle)

    def finish(self, handle, **fields):
        self.records[handle] = {**self.records[handle], "state": "finished", "run_ok": True, **fields}
        self.gone.add(handle)


@dataclass
class Bench:
    workspace: Path
    config: AgenticHILConfig
    clock: Clock
    workers: Workers
    holders: Holders
    incidents: Incidents
    settings: SchedulerSettings

    def scheduler(self, **injected):
        return Scheduler(
            self.config, self.settings, clock=self.clock,
            worker_source=injected.pop("worker_source", self.workers),
            holder_source=injected.pop("holder_source", self.holders),
            incident_source=injected.pop("incident_source", self.incidents), **injected,
        )

    def plan(self, name, steps, *, version=4):
        path = self.workspace / "plans" / f"{name}.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump({"version": version, "name": name, "steps": steps}), encoding="utf-8")
        return path

    def keys(self, plan):
        loaded = load_test_config(str(plan), str(self.workspace))
        return plan_devices(self.config, loaded).lock_keys


def make_bench(tmp_path, monkeypatch, **settings):
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    executable = tmp_path / "fake_openocd.py"
    executable.write_bytes(FAKE_OPENOCD.read_bytes())
    extra = {}
    for name, identity in (("other", "second"),):
        extra[name] = {
            "type": "openocd", "executable": executable.as_posix(),
            "probe_id": f"fixture-{identity}", "interface": "SWD",
            "permissions": {"allow_probe": True, "allow_flash": True, "allow_reset": True,
                            "allow_debug_execution": True, "allow_raw_debugger_commands": False,
                            "allow_mass_erase": False},
        }
    buses = {
        name: {
            "adapter": "process", "channel": name, "executable": executable.as_posix(),
            "permissions": {"allow_read": True, "allow_write": True},
            "shares": {participant: {"permissions": {"allow_read": True, "allow_write": True}}
                       for participant in ("ecu_a", "ecu_b")},
        }
        for name in ("shared", "unrelated")
    }
    write_authoritative_config(
        workspace, monkeypatch, debugger_executable=executable, probe_id="fixture-first",
        debuggers_yaml=yaml.safe_dump({"debuggers": extra}),
        can_buses_yaml=yaml.safe_dump({"can_buses": buses}), allowed_roots=["build"],
        com_ports_yaml=yaml.safe_dump({"com_ports": {
            name: {"device": "fixture-unopened-port", "serial_number": "fixture-uart",
                   "permissions": {"allow_read": True, "allow_write": True}}
            for name in ("alias_a", "alias_b")
        }}),
    )
    monkeypatch.chdir(workspace)
    config = load_authoritative_config(str(workspace))
    return Bench(workspace, config, Clock(), Workers(), Holders(), Incidents(),
                 SchedulerSettings(job_root=workspace / "jobs", **settings))


def delay(device="dut", seconds=2):
    return {"action": "delay", "device": device, "duration_ms": int(seconds * 1000)}


def can_steps(participant, bus="shared"):
    return [
        {"action": "can_open", "bus_id": bus, "participant": participant, "clear_rx_queue": False},
        {"action": "can_close", "bus_id": bus, "participant": participant},
    ]


def submit(scheduler, plan, label="alice", **kwargs):
    return scheduler.submit(plan=plan, artifacts={}, submitter_label=label, **kwargs)


def status(scheduler, job):
    return scheduler.status(job.job_id)


def device_status(scheduler, job, key):
    return status(scheduler, job)["devices"][resource_digest(key)]


def running(scheduler, job):
    scheduler.tick()
    assert status(scheduler, job)["state"] == "running", status(scheduler, job)
    return status(scheduler, job)["attempt_handle"]


def test_passing_then_reservation(tmp_path, monkeypatch):
    bench = make_bench(tmp_path, monkeypatch, retry_interval_s=3600)
    scheduler = bench.scheduler()
    single = bench.plan("single", [delay()])
    seed = submit(scheduler, single)
    scheduler.tick()
    handle = running(scheduler, seed)
    bench.clock.advance(20)
    bench.workers.finish(handle)
    scheduler.tick()
    pair = bench.plan("pair", [delay(), delay("other")])
    key_a, key_b = bench.keys(single)[0], bench.keys(bench.plan("b", [delay("other")]))[0]
    bench.holders.hold([key_b])
    oldest = submit(scheduler, pair)
    young1 = submit(scheduler, single, "bob")
    young2 = submit(scheduler, single, "carol")
    assert device_status(scheduler, oldest, key_a)["position"] == 1
    assert device_status(scheduler, young1, key_a)["position"] == 2
    assert device_status(scheduler, young2, key_a)["position"] == 3
    assert device_status(scheduler, oldest, key_b)["position"] == 1
    estimate = status(scheduler, young1)["estimate"]
    assert estimate["lower_bound_s"] == 2
    assert estimate["history_samples"] == 1
    assert estimate["duration_s"] >= 20
    scheduler.tick()
    assert status(scheduler, oldest)["state"] == "queued"
    h1 = running(scheduler, young1)
    assert status(scheduler, young2)["state"] == "queued"
    bench.clock.advance(40)
    bench.workers.finish(h1)
    scheduler.tick()
    h2 = running(scheduler, young2)
    assert status(scheduler, young2)["estimate"]["history_samples"] == 2
    assert status(scheduler, young2)["estimate"]["duration_s"] > estimate["duration_s"]
    bench.workers.finish(h2)
    scheduler.tick()
    later = submit(scheduler, single, "dave")
    bench.clock.advance(bench.settings.reserve_after_s + 1)
    scheduler.tick()
    for key in bench.keys(pair):
        assert device_status(scheduler, oldest, key)["reserved_for"] == oldest.job_id
    assert status(scheduler, later)["state"] == "queued"
    bench.holders.release([key_b])
    scheduler.tick()
    running(scheduler, oldest)
    assert status(scheduler, later)["state"] == "queued"
    assert all(start[3] == 0 for start in bench.workers.starts)


def test_zero_reservation_is_strict_fifo(tmp_path, monkeypatch):
    bench = make_bench(tmp_path, monkeypatch, reserve_after_s=0)
    scheduler = bench.scheduler()
    plan_a = bench.plan("a", [delay()])
    plan_b = bench.plan("b", [delay("other")])
    bench.holders.hold(bench.keys(plan_a))
    oldest = submit(scheduler, plan_a)
    same = submit(scheduler, plan_a, "bob")
    disjoint = submit(scheduler, plan_b, "carol")
    scheduler.tick()
    assert not bench.workers.starts
    assert all(status(scheduler, job)["state"] == "queued" for job in (oldest, same, disjoint))
    bench.holders.release(bench.keys(plan_a))
    scheduler.tick()
    first_handle = running(scheduler, oldest)
    assert status(scheduler, same)["state"] == "queued"
    assert status(scheduler, disjoint)["state"] == "queued"
    bench.workers.finish(first_handle)
    scheduler.tick()
    running(scheduler, same)
    running(scheduler, disjoint)
    assert [entry[0] for entry in bench.workers.starts] == [
        status(scheduler, job)["attempt_handle"] for job in (oldest, same, disjoint)
    ]


def test_disjoint_devices_and_can_participants_run_together(tmp_path, monkeypatch):
    bench = make_bench(tmp_path, monkeypatch)
    scheduler = bench.scheduler()
    a = bench.plan("a", [delay()])
    b = bench.plan("b", [delay("other")])
    uart = bench.plan("uart", [delay("alias_a")])
    alias = bench.plan("alias", [delay("alias_b")])
    ca = bench.plan("ca", can_steps("ecu_a"), version=6)
    cb = bench.plan("cb", can_steps("ecu_b"), version=6)
    first = submit(scheduler, a)
    jobs = [first, submit(scheduler, b), submit(scheduler, ca), submit(scheduler, cb), submit(scheduler, uart)]
    duplicate = submit(scheduler, alias, "bob")
    assert bench.keys(uart) == bench.keys(alias)
    assert not set(bench.keys(a)) & set(bench.keys(b))
    assert bench.keys(ca)[0].endswith("#ecu_a")
    assert bench.keys(cb)[0].endswith("#ecu_b")
    assert bench.keys(ca)[0].rsplit("#", 1)[0] == bench.keys(cb)[0].rsplit("#", 1)[0]
    assert bench.keys(ca)[0].startswith("can:process:")
    scheduler.tick()
    for job in jobs:
        running(scheduler, job)
    assert status(scheduler, duplicate)["state"] == "queued"
    assert len(bench.workers.starts) == 5


@pytest.mark.parametrize("outcome", ["expired", "withdrawn"], ids=["expired", "withdrawn"])
def test_queued_terminal_outcomes(tmp_path, monkeypatch, outcome):
    bench = make_bench(tmp_path, monkeypatch, max_queue_s=20, retry_interval_s=3600)
    scheduler = bench.scheduler()
    plan = bench.plan("wait", [delay()])
    bench.holders.hold(bench.keys(plan))
    job = submit(scheduler, plan, max_queue_s=5)
    capped = submit(scheduler, plan, "bob", max_queue_s=200)
    assert status(scheduler, job)["queue_deadline"] == bench.clock() + 5
    assert status(scheduler, capped)["queue_deadline"] == bench.clock() + 20
    before = status(scheduler, job)
    with pytest.raises(PermissionError):
        scheduler.withdraw(job.job_id, submitter_label="administrator")
    assert status(scheduler, job) == before
    if outcome == "expired":
        bench.clock.advance(6)
        scheduler.tick()
        assert (status(scheduler, job)["state"], status(scheduler, capped)["state"]) == ("expired", "queued")
        bench.clock.advance(15)
        scheduler.tick()
        assert status(scheduler, capped)["state"] == "expired"
    else:
        scheduler.withdraw(job.job_id, submitter_label="alice")
        assert status(scheduler, job)["state"] == "withdrawn"
        assert device_status(scheduler, capped, bench.keys(plan)[0])["position"] == 1
    assert status(scheduler, job)["state"] == outcome
    assert status(scheduler, job)["started"] is False
    assert not bench.workers.starts
    assert not bench.workers.stops


def test_restart_reconciles_workers_and_preserves_queue(tmp_path, monkeypatch):
    for variant in ("gone", "cooperative", "forced"):
        bench = make_bench(tmp_path / variant, monkeypatch)
        scheduler = bench.scheduler()
        plan = bench.plan("restart", [delay()])
        active = submit(scheduler, plan)
        scheduler.tick()
        handle = running(scheduler, active)
        waiting = [submit(scheduler, plan, label) for label in ("bob", "carol")]
        old = [status(scheduler, job) for job in waiting]
        if variant == "gone":
            bench.workers.gone.add(handle)
        restarted = bench.scheduler()
        restarted.tick()
        if variant != "gone":
            assert bench.workers.stops == [handle]
            assert len(bench.workers.starts) == 1
            assert status(restarted, waiting[0])["state"] == "queued"
            if variant == "forced":
                bench.clock.advance(bench.settings.stop_grace_s + 1)
                restarted.tick()
                assert bench.workers.terminations == [handle]
                assert len(bench.workers.starts) == 1
                assert status(restarted, active)["state"] != "worker_gone"
            bench.workers.gone.add(handle)
            restarted.tick()
        terminal = status(restarted, active)
        assert terminal["state"] == "worker_gone"
        assert terminal["started"] is True
        report = json.loads(Path(terminal["report_path"]).read_text(encoding="utf-8"))
        assert report["run_record"]["run"] == handle
        assert report["canonical_report_missing"] is True
        assert report["cleanup_confirmed"] is False
        for job, original in zip(waiting, old, strict=True):
            current = status(restarted, job)
            assert (current["sequence"], current["queued_at"]) == (original["sequence"], original["queued_at"])
        assert status(restarted, waiting[0])["state"] in {"starting", "running"}
        assert status(restarted, waiting[1])["state"] == "queued"
        next_job = submit(restarted, plan, "dave")
        assert status(restarted, next_job)["sequence"] == old[-1]["sequence"] + 1


def test_local_holder_status_and_release(tmp_path, monkeypatch):
    bench = make_bench(tmp_path, monkeypatch)
    scheduler = bench.scheduler(holder_source=BenchHolderSource())
    plan = bench.plan("local", [delay()])
    key = bench.keys(plan)[0]
    local = BenchMutex(frontend="mcp-stdio", label="local soak")
    local.owner = BenchOwner("private-owner-token", 123456789, "private-host", "mcp-stdio", "local soak")
    local.acquire([key], wait_s=0)
    try:
        job = submit(scheduler, plan)
        original_record = local.holder(key)
        # A read must never probe an OS lock, including a take-and-release probe.
        with monkeypatch.context() as no_probe:
            no_probe.setattr(BenchMutex, "acquire", lambda *a, **k: pytest.fail("scheduler probed a device lock"))
            scheduler.tick()
            view = device_status(scheduler, job, key)
        assert status(scheduler, job)["state"] == "queued"
        assert view["holder"]["frontend"] == "mcp-stdio"
        assert view["holder"]["label"] == "local soak"
        assert view["held_since"] == original_record["acquired_at"]
        assert view["position"] == 1
        assert view["estimated_free_at"] is None
        assert view["estimate_unknown_reason"]
        assert status(scheduler, job)["submitter_label_source"] == "caller_supplied"
        public = json.dumps(status(scheduler, job))
        for secret in ("private-owner-token", "123456789", "private-host", key, str(local.root)):
            assert secret not in public
        assert not bench.workers.starts
        local.release_all()
        scheduler.tick()
        running(scheduler, job)
        assert device_status(scheduler, job, key)["holder"] is None
        bench.workers.finish(status(scheduler, job)["attempt_handle"])
        scheduler.tick()
        # Reproduce a stale advisory record after release without acquiring a lock.
        held_reader = local.holder
        monkeypatch.setattr(BenchMutex, "holder", lambda self, resource: original_record if resource == key else held_reader(resource))
        stale = submit(scheduler, plan, "bob")
        scheduler.tick()
        assert status(scheduler, stale)["state"] == "queued"
        bench.clock.advance(bench.settings.retry_interval_s + 0.1)
        scheduler.tick()
        running(scheduler, stale)
    finally:
        local.release_all()


def test_incident_blocking_and_bus_cause(tmp_path, monkeypatch):
    bench = make_bench(tmp_path, monkeypatch)
    scheduler = bench.scheduler()
    ca = bench.plan("ca", can_steps("ecu_a"), version=6)
    cb = bench.plan("cb", can_steps("ecu_b"), version=6)
    cc = bench.plan("cc", can_steps("ecu_a", "unrelated"), version=6)
    key = bench.keys(ca)[0]
    reason = {"error_type": "resource_quarantined", "reason": "scripted standing audit incident"}
    bench.incidents.reasons[key] = reason
    a = submit(scheduler, ca)
    scheduler.tick()
    assert status(scheduler, a)["state"] == "queued"
    assert device_status(scheduler, a, key)["blocked_by_incident"] == reason
    assert not bench.workers.starts
    bench.incidents.reasons.clear()
    scheduler.tick()
    ha = running(scheduler, a)
    b, c = submit(scheduler, cb, "bob"), submit(scheduler, cc, "carol")
    scheduler.tick()
    hb, hc = running(scheduler, b), running(scheduler, c)
    bus = key.rsplit("#", 1)[0]
    cause = {"error_type": "can_bus_incident", "incident_scope": "bus", "bus": bus, "summary": "scripted bus loss"}
    bench.workers.finish(ha, run_ok=False, **cause)
    scheduler.tick()
    assert hb in bench.workers.stops
    assert hc not in bench.workers.stops
    bench.workers.finish(hb, run_ok=False)
    scheduler.tick()
    for job in (a, b):
        assert status(scheduler, job)["state"] == "finished"
        assert status(scheduler, job)["cause"] == cause
    assert status(scheduler, c)["state"] == "running"
    a2, b2 = submit(scheduler, ca), submit(scheduler, cb, "bob")
    scheduler.tick()
    ha2, hb2 = running(scheduler, a2), running(scheduler, b2)
    stops_before = list(bench.workers.stops)
    bench.workers.finish(ha2, run_ok=False, error_type="can_participant_incident", incident_scope="participant")
    scheduler.tick()
    assert bench.workers.stops == stops_before
    assert hb2 not in bench.workers.stops
    assert status(scheduler, b2)["state"] == "running"


def test_submitter_queue_limit(tmp_path, monkeypatch):
    bench = make_bench(tmp_path, monkeypatch)
    scheduler = bench.scheduler()
    plan = bench.plan("quota", [delay()])
    bench.holders.hold(bench.keys(plan))
    first = submit(scheduler, plan)
    first_sequence = status(scheduler, first)["sequence"]
    jobs = [first, *(submit(scheduler, plan) for _ in range(4))]
    refused = submit(scheduler, plan)
    assert status(scheduler, refused)["state"] == "rejected"
    assert status(scheduler, refused)["sequence"] is None
    assert status(scheduler, refused)["error_type"] == "max_queued_jobs_per_user"
    outsider = submit(scheduler, plan, "bob")
    assert status(scheduler, outsider)["sequence"] == status(scheduler, jobs[-1])["sequence"] + 1
    bench.workers.publish_running = False
    bench.holders.release(bench.keys(plan))
    scheduler.tick()
    assert status(scheduler, first)["state"] == "starting"
    also_refused = submit(scheduler, plan)
    assert status(scheduler, also_refused)["state"] == "rejected"
    handle = status(scheduler, first)["attempt_handle"]
    bench.workers.finish(handle, run_ok=False, error_type="device_busy")
    scheduler.tick()
    assert status(scheduler, first)["sequence"] == first_sequence
    assert status(scheduler, submit(scheduler, plan))["state"] == "rejected"
    scheduler.withdraw(jobs[-1].job_id, submitter_label="alice")
    admitted = submit(scheduler, plan)
    assert status(scheduler, admitted)["sequence"] == status(scheduler, outsider)["sequence"] + 1
    flash = bench.plan("missing", [{"action": "flash", "device": "dut", "image_path": "build/missing.bin"}])
    awaiting = submit(scheduler, flash, "carol")
    assert status(scheduler, awaiting)["state"] == "awaiting_artifacts"
    assert status(scheduler, awaiting)["sequence"] is None
    artifact = bench.workspace / "build" / "missing.bin"
    artifact.parent.mkdir(exist_ok=True)
    artifact.write_bytes(b"\x01\x02\x03\x04")
    scheduler.provide_artifacts(awaiting.job_id, artifacts={"build/missing.bin": artifact}, submitter_label="carol")
    assert status(scheduler, awaiting)["state"] == "queued"
    assert status(scheduler, awaiting)["sequence"] == status(scheduler, admitted)["sequence"] + 1
    assert awaiting.plan_path.read_bytes() == flash.read_bytes()
    assert status(scheduler, awaiting)["plan_digest"] == hashlib.sha256(flash.read_bytes()).hexdigest()
    assert awaiting.plan_path.is_relative_to(awaiting.directory / "plan")
    assert set(("plan", "artifacts", "workspace", "report", "logs")) <= {path.name for path in awaiting.directory.iterdir()}
    assert any(path.read_bytes() == artifact.read_bytes() for path in (awaiting.directory / "artifacts").rglob("*.bin"))
    # Default execution uses the unchanged bench configuration and ordinary paths.
    for _, config, stored_plan, wait_s in bench.workers.starts:
        assert config is bench.config
        assert stored_plan.is_relative_to(bench.workspace)
        assert wait_s == 0


def test_plan_lower_bound_admission(tmp_path, monkeypatch):
    bench = make_bench(tmp_path, monkeypatch)
    scheduler = bench.scheduler()

    def repeat(body, **bounds):
        return {"action": "repeat", "steps": body, **bounds}

    rows = [
        ("delay", [delay(seconds=600)], 600, "queued"),
        ("equal", [delay(seconds=450), delay(seconds=450)], 900, "queued"),
        ("sum", [delay(seconds=451), delay(seconds=450)], 901, "rejected"),
        ("count", [repeat([delay(seconds=451)], count=2)], 902, "rejected"),
        ("duration", [repeat([delay(seconds=1)], duration_s=901)], 901, "rejected"),
        ("count_first", [repeat([delay(seconds=1)], count=1, duration_s=1800)], 1, "queued"),
        ("duration_first", [repeat([delay(seconds=1)], count=1000, duration_s=2)], 2, "queued"),
        ("nested", [repeat([repeat([delay(seconds=226)], count=2)], count=2)], 904, "rejected"),
        ("whole_iteration", [repeat([delay(seconds=450), delay(seconds=451)], count=1, duration_s=1)], 901, "rejected"),
    ]
    for name, steps, lower_bound, state in rows:
        plan = bench.plan(name, steps)
        job = submit(scheduler, plan, name)
        loaded = load_test_config(str(plan), str(bench.workspace))
        reactor = TestReactor(bench.config, RecordingService(), service_factory=lambda _: RecordingService())
        assert reactor.preflight(loaded) is None
        view = status(scheduler, job)
        assert view["state"] == state, (name, view)
        assert view["estimate"]["lower_bound_s"] == lower_bound
        if state == "rejected":
            assert view["error_type"] == "plan_runtime_exceeded"
            assert view["sequence"] is None
        else:
            assert isinstance(view["sequence"], int)
    assert not bench.workers.starts
