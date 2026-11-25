"""A shared bench reports its jobs between queue admission and their final tick."""

from __future__ import annotations

import copy
from collections import Counter
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from http.server import ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import pytest
import yaml
from bench_job_fakes import IMAGE_PATH, job_bench, plan
from httpserver_fakes import (
    mcp_call,
    mcp_initialize,
    raise_handler_error,
    record_handler_errors,
    request,
    short_settings,
)
from support import scaled_time_bound

from agentic_hil import benchview, httpserver, runlifecycle
from agentic_hil.bench import BenchMutex, device_lock_root
from agentic_hil.benchjobs import BenchJobService
from agentic_hil.benchview import BenchView, McpBenchView, configured_device_keys
from agentic_hil.config import ConfigError, load_authoritative_config
from agentic_hil.scheduler import SchedulerSettings
from agentic_hil.types import AgenticHILConfig, JsonObject

HOLD_TIMES = {"held_since", "heartbeat_at", "heartbeat_age_s", "holder_heartbeat_stale"}
TWO_DEVICES = ("dut", "other")
RUNTIME_LIMIT = 60


class ScriptedWorker:
    """A start request creates neither a run record nor a device hold."""

    def __init__(self) -> None:
        self.starts: dict[str, str] = {}
        self.stops: set[str] = set()
        self.records: dict[str, JsonObject] = {}
        self.gone: set[str] = set()

    def new_handle(self) -> str:
        return runlifecycle.new_run_handle()

    def start(self, config: AgenticHILConfig, handle: str, plan: Path, *, wait_s: float) -> None:
        self.starts[Path(config.work_dir).parent.name] = handle

    def record(self, config: AgenticHILConfig, handle: str) -> JsonObject | None:
        return copy.deepcopy(self.records.get(handle))

    def is_gone(self, config: AgenticHILConfig, handle: str) -> bool:
        return handle in self.gone

    def stop(self, config: AgenticHILConfig, handle: str) -> None:
        self.stops.add(handle)

    def terminate(self, config: AgenticHILConfig, handle: str) -> None:
        raise AssertionError("the test advanced past the worker's stop grace period")

    def publish(self, job: str, state: str) -> None:
        handle = self.starts[job]
        self.records[handle] = {"run": handle, "state": state}

    def finish(self, job: str, *, state: str = "finished") -> None:
        self.publish(job, state)
        self.gone.add(self.starts[job])


@dataclass
class Bench:
    server: ThreadingHTTPServer
    service: BenchJobService
    view: BenchView
    mcp: McpBenchView
    worker: ScriptedWorker
    lock_root: Path
    keys: dict[str, tuple[str, ...]]
    now: float = 1000.0

    def tick(self) -> None:
        # Keep the production exclusion between the scheduler and API readers.
        # The background ticker is never started, so the test owns every tick.
        with self.service._condition:
            self.service.scheduler.tick()
            self.service._publish()
        self.service._perform_pending()

    def submit(self, body: bytes) -> JsonObject:
        response = request(self.server, "POST", "/v1/jobs", body=body, headers={"Content-Type": "application/yaml"})
        raise_handler_error(self.server)
        assert response.status == 202, response
        return response.json()

    def read(self, path: str) -> JsonObject:
        response = request(self.server, "GET", path)
        raise_handler_error(self.server)
        assert response.status == 200, response
        return response.json()

    def state(self, job: str, expected: str) -> None:
        reply = self.read(f"/v1/jobs/{job}")
        assert reply["state"] == expected, reply

    def withdraw(self, job: JsonObject) -> None:
        job_id = job["job_id"]
        headers = {"Authorization": f"Bearer {job['job_token']}"}
        response = request(self.server, "DELETE", f"/v1/jobs/{job_id}", headers=headers)
        raise_handler_error(self.server)
        assert response.status == 202, response
        assert response.json()["state"] == "withdrawing", response
        assert self.worker.starts[job_id] in self.worker.stops

    def leases(self) -> JsonObject:
        wire = self.read("/v1/leases")
        # Call the real provider on the pytest thread for return-value proof.
        direct = self.view.leases()
        assert stable_holds(wire["holds"]) == stable_holds(direct["holds"])
        assert wire["holds_readable"] == direct["holds_readable"]
        return wire

    @contextmanager
    def holder(self, keys: Sequence[str], *, label: str) -> Iterator[BenchMutex]:
        mutex = BenchMutex(frontend="job-hold-test", label=label, root=self.lock_root)
        mutex.acquire(tuple(keys))
        try:
            yield mutex
        finally:
            mutex.release_all()


@pytest.fixture
def bench(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Bench]:
    fixture = job_bench(tmp_path, monkeypatch)
    document = fixture.document()
    document["can_buses"] = {"shared": {
        "adapter": "process",
        "channel": fixture.config.debuggers["dut"].probe_id,
        "executable": fixture.config.debuggers["dut"].executable,
        "permissions": {"allow_write": True},
        "shares": {
            name: {"permissions": {"allow_write": True}}
            for name in ("ecu_a", "ecu_b")
        },
    }}
    fixture.config_path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    config = load_authoritative_config(fixture.workspace)
    settings = short_settings(host="127.0.0.1")
    lock_root = device_lock_root()
    worker = ScriptedWorker()
    service = BenchJobService(
        config, settings, SchedulerSettings(job_root=fixture.job_root, max_job_runtime_s=RUNTIME_LIMIT),
        worker_source=worker, recover_call=None, reconcile_interval_s=1,
    )
    try:
        with BenchView(config, settings, activity=service.device_activity) as view:
            mcp = McpBenchView(view, service)
            with httpserver.create_http_server(settings, service, bench=view, bench_reads=mcp) as server:
                active = Bench(server, service, view, mcp, worker, lock_root, configured_device_keys(config))
                service.scheduler.clock = lambda: active.now
                record_handler_errors(server)
                listener = Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
                listener.start()
                try:
                    yield active
                finally:
                    server.shutdown()
                    listener.join(scaled_time_bound(15))
                    assert not listener.is_alive()
    finally:
        service.close()


def plan_bytes(name: str, steps: list[JsonObject], *, version: int = 4) -> bytes:
    return yaml.safe_dump({"version": version, "name": name, "steps": steps}).encode("utf-8")


def dual_plan() -> bytes:
    return plan_bytes("two devices", [{"action": "delay", "device": name, "duration_ms": 1} for name in TWO_DEVICES])


def can_plan(participant: str) -> bytes:
    bus = {"bus_id": "shared", "participant": participant}
    return plan_bytes(participant, [
        {"action": "can_open", **bus, "clear_rx_queue": False},
        {"action": "can_close", **bus},
    ], version=6)


def hold_pairs(holds: list[JsonObject]) -> Counter[tuple[str, str | None]]:
    return Counter((hold["device"], hold.get("job_id")) for hold in holds)


def stable_holds(holds: list[JsonObject]) -> list[JsonObject]:
    return sorted(
        ({key: value for key, value in hold.items() if key != "heartbeat_age_s"} for hold in holds),
        key=lambda hold: (hold["device"], hold.get("job_id") or ""),
    )


def assert_empty_holder(hold: JsonObject, kind: str) -> None:
    assert hold["kind"] == kind, hold
    assert hold["holder"] == {}, hold
    assert HOLD_TIMES.isdisjoint(hold), hold


def assert_recorded_holder(hold: JsonObject, mutex: BenchMutex, key: str, *, kind: str) -> None:
    record = mutex.holder(key)
    assert record is not None and record["state"] == "held", record
    assert hold["kind"] == kind, hold
    assert hold["holder"] == {
        field: record["owner"][field]
        for field in ("owner_id", "pid", "host", "frontend", "label")
        if field in record["owner"]
    }, hold
    assert hold["held_since"] == record["acquired_at"], hold
    assert hold["heartbeat_at"] == record["heartbeat_at"], hold
    assert isinstance(hold["heartbeat_age_s"], (int, float)), hold


def test_two_device_starting_holds(bench: Bench, monkeypatch: pytest.MonkeyPatch) -> None:
    waiting = bench.submit(plan(name="missing image", image=IMAGE_PATH))
    assert waiting["state"] == "awaiting_artifacts", waiting
    assert bench.read("/v1/queue")["entries"] == []
    assert bench.leases()["holds"] == []

    submitted = bench.submit(dual_plan())
    job = submitted["job_id"]
    assert submitted["state"] == "queued", submitted
    assert [entry["job_id"] for entry in bench.read("/v1/queue")["entries"]] == [job]
    readable = bench.leases()
    assert readable["holds_readable"] is True and readable["holds"] == [], readable

    bench.tick()
    bench.state(job, "starting")
    assert bench.read("/v1/queue")["entries"] == []
    assert bench.worker.starts.keys() == {job}
    assert bench.worker.records == {}
    reader = BenchMutex(root=bench.lock_root)
    assert all(reader.holder(key) is None for name in TWO_DEVICES for key in bench.keys[name])
    expected = Counter((name, job) for name in TWO_DEVICES)
    leases = bench.leases()
    if hold_pairs(leases["holds"]) != expected:
        raise AssertionError("starting job has no hold for each declared device")
    assert leases["holds_readable"] is True
    for hold in leases["holds"]:
        assert_empty_holder(hold, "debugger")

    bench.worker.publish(job, "starting")
    bench.tick()
    bench.state(job, "starting")
    repeated = bench.leases()
    assert repeated["holds_readable"] is True
    assert stable_holds(repeated["holds"]) == stable_holds(leases["holds"])

    def unreadable_lock_root() -> Path:
        raise ConfigError("device_lock_root_unusable", "test lock directory cannot be read")

    with monkeypatch.context() as scoped:
        scoped.setattr(benchview, "_lock_root", unreadable_lock_root)
        unreadable = bench.leases()
        assert unreadable["holds_readable"] is False, unreadable
        assert stable_holds(unreadable["holds"]) == stable_holds(leases["holds"])


def test_job_holds_until_terminal_tick(bench: Bench) -> None:
    withdrawn = bench.submit(plan(name="will withdraw", device="dut"))
    stopping = bench.submit(plan(name="will exceed runtime", device="other"))
    jobs = {"dut": withdrawn["job_id"], "other": stopping["job_id"]}
    expected = Counter(jobs.items())
    bench.tick()
    for job in jobs.values():
        bench.worker.publish(job, "running")
    bench.tick()
    for job in jobs.values():
        bench.state(job, "running")

    keys = [key for device in jobs for key in bench.keys[device]]
    with bench.holder(keys, label="scripted worker") as mutex:
        holds = bench.leases()["holds"]
        assert hold_pairs(holds) == expected, holds
        for hold in holds:
            assert_recorded_holder(hold, mutex, bench.keys[hold["device"]][0], kind="debugger")
    for key in keys:
        record = mutex.holder(key)
        assert record is not None and record["state"] == "released", record

    holds = bench.leases()["holds"]
    if hold_pairs(holds) != expected:
        raise AssertionError("running job lost its holds after the worker released its devices")
    for hold in holds:
        assert_empty_holder(hold, "debugger")

    bench.now += RUNTIME_LIMIT - 1
    bench.withdraw(withdrawn)
    bench.state(jobs["dut"], "withdrawing")
    assert stable_holds(bench.leases()["holds"]) == stable_holds(holds)
    bench.now += 2
    bench.tick()
    bench.state(jobs["other"], "stopping")
    assert bench.worker.starts[jobs["other"]] in bench.worker.stops
    assert not bench.worker.gone
    assert stable_holds(bench.leases()["holds"]) == stable_holds(holds)

    for device, state in (("dut", "withdrawn"), ("other", "runtime_exceeded")):
        bench.worker.finish(jobs[device], state="stopped")
        assert hold_pairs(bench.leases()["holds"]) == expected
        bench.tick()
        bench.state(jobs[device], state)
        del expected[(device, jobs[device])]
        assert hold_pairs(bench.leases()["holds"]) == expected

    with bench.holder(bench.keys["dut"], label="outside the queue") as external:
        holds = bench.leases()["holds"]
        assert hold_pairs(holds) == {("dut", None): 1}, holds
        assert "job_id" not in holds[0], holds[0]
        assert_recorded_holder(holds[0], external, bench.keys["dut"][0], kind="debugger")


def test_shared_can_job_holds(bench: Bench) -> None:
    first = bench.submit(can_plan("ecu_a"))["job_id"]
    bench.tick()
    bench.state(first, "starting")
    expected = Counter({("shared", first): 1})
    first_holds = bench.leases()["holds"]
    if hold_pairs(first_holds) != expected:
        raise AssertionError("shared CAN bus does not show a hold for each active participant job")
    assert bench.read("/v1/devices/shared")["state"] == "held"
    assert_empty_holder(first_holds[0], "can")

    second = bench.submit(can_plan("ecu_b"))["job_id"]
    bench.tick()
    bench.state(second, "starting")
    expected[("shared", second)] = 1
    assert hold_pairs(bench.leases()["holds"]) == expected
    inventory = bench.read("/v1/devices")
    assert next(device for device in inventory["devices"] if device["name"] == "shared")["state"] == "held"
    bus_key, = bench.keys["shared"]
    first_key = bench.service.store.jobs[first]["lock_keys"][0]
    assert first_key == f"{bus_key}#ecu_a"

    with bench.holder([first_key], label="first participant") as participant:
        holds = bench.leases()["holds"]
        assert hold_pairs(holds) == expected
        by_job = {hold["job_id"]: hold for hold in holds}
        assert_recorded_holder(by_job[first], participant, first_key, kind="can")
        assert_empty_holder(by_job[second], "can")
        with bench.holder([bus_key], label="external CAN bus owner") as external:
            holds = bench.leases()["holds"]
            assert hold_pairs(holds) == expected
            by_job = {hold["job_id"]: hold for hold in holds}
            assert_recorded_holder(by_job[first], participant, first_key, kind="can")
            assert_recorded_holder(by_job[second], external, bus_key, kind="can")

    for job in (first, second):
        bench.worker.finish(job)
        assert hold_pairs(bench.leases()["holds"]) == expected
        bench.tick()
        bench.state(job, "finished")
        del expected[("shared", job)]
        assert hold_pairs(bench.leases()["holds"]) == expected


def test_mcp_job_holds(bench: Bench) -> None:
    session, _ = mcp_initialize(bench.server)
    withdrawn = bench.submit(plan(name="MCP withdrawal", device="dut"))
    stopping = bench.submit(plan(name="MCP runtime limit", device="other"))
    first, second = withdrawn["job_id"], stopping["job_id"]
    expected = Counter({("dut", first): 1, ("other", second): 1})

    def check_holds() -> None:
        queue = mcp_call(bench.server, session, "bench_queue_status")["structuredContent"]
        status = mcp_call(bench.server, session, "hardware_lease_status")["structuredContent"]
        assert queue["ok"] is True and status["ok"] is True
        direct = bench.mcp.hardware_lease_status()
        assert status["bench_held"] == direct["bench_held"]
        assert stable_holds(status["device_holds"]) == stable_holds(direct["device_holds"])
        rest = bench.read("/v1/leases")["holds"]
        assert queue["queue"] == bench.read("/v1/queue")["entries"]
        assert stable_holds(queue["holds"]) == stable_holds(rest)
        assert stable_holds(status["device_holds"]) == stable_holds(rest)
        if hold_pairs(status["device_holds"]) != expected or status["bench_held"] is not bool(expected):
            raise AssertionError("MCP lease status omits the starting job's device holds")

    bench.tick()
    for job in (first, second):
        bench.state(job, "starting")
    check_holds()
    for job in (first, second):
        bench.worker.publish(job, "starting")
    bench.tick()
    for job in (first, second):
        bench.state(job, "starting")
    check_holds()

    for job in (first, second):
        bench.worker.publish(job, "running")
    bench.tick()
    for job in (first, second):
        bench.state(job, "running")
    keys = [key for device in TWO_DEVICES for key in bench.keys[device]]
    with bench.holder(keys, label="MCP workers"):
        check_holds()
    check_holds()

    bench.now += RUNTIME_LIMIT - 1
    bench.withdraw(withdrawn)
    bench.state(first, "withdrawing")
    check_holds()
    bench.now += 2
    bench.tick()
    bench.state(second, "stopping")
    check_holds()
    for pair in (("dut", first), ("other", second)):
        bench.worker.finish(pair[1], state="stopped")
        bench.tick()
        del expected[pair]
        check_holds()
