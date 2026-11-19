"""A shared bench, its one fake and loopback helpers for the bench server tests (#891).

The bench is real: a version 4 configuration with a `server` block, and the
real job store and scheduler behind the job service. The one fake is the worker
source. It starts no process: an attempt it is asked for writes the run record
and the canonical run report a real worker would write, so no test reaches a
device. A plan whose name begins with `held` keeps running until the test
finishes it or the scheduler stops it; `failing...` ends with a failing report;
every other plan ends at once with a passing one.
"""

from __future__ import annotations

import copy
import hashlib
import json
import time
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from http.server import ThreadingHTTPServer
from pathlib import Path
from threading import Event, Lock, Thread
from typing import Any

import yaml
from conftest import FAKE_OPENOCD, write_authoritative_config
from httpserver_fakes import short_settings
from support import scaled_time_bound

from agentic_hil import httpserver, runlifecycle
from agentic_hil.benchjobs import open_bench_job_service
from agentic_hil.config import load_authoritative_config
from agentic_hil.httpapi import JobService, ServerSettings
from agentic_hil.jobstore import TERMINAL_JOB_STATES
from agentic_hil.report import canonical_run_report_path
from agentic_hil.types import AgenticHILConfig, JsonObject

IMAGE = b"\x7fELFshared-bench-fixture-image"
IMAGE_PATH = "build/app.elf"
# The one event log the fake worker leaves in a job's logs directory.
LOG_NAME = "fixture-run.jsonl"
LOG_BYTES = b'{"event": "fixture", "time": "2026-01-01T00:00:00Z"}\n'
# How often the tick loop looks at an active attempt. An input of the service,
# not a bound: it decides how soon a worker's end is seen, never an outcome.
RECONCILE_INTERVAL_S = 0.02
TERMINAL = frozenset(TERMINAL_JOB_STATES)


@dataclass
class JobBench:
    workspace: Path
    config_path: Path
    job_root: Path
    config: AgenticHILConfig

    def document(self) -> dict[str, Any]:
        loaded = yaml.safe_load(self.config_path.read_text(encoding="utf-8"))
        assert isinstance(loaded, dict)
        return loaded

    def with_server(self, **fields: Any) -> AgenticHILConfig:
        """Rewrite the `server` block with `fields` and load the bench again."""
        document = self.document()
        document["server"].update(fields)
        self.config_path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
        self.config = load_authoritative_config(self.workspace)
        return self.config


def job_bench(tmp_path: Path, monkeypatch: Any, **server: Any) -> JobBench:
    """A version 4 bench with two probes, `dut` and `other`, and a server block naming `job_root`."""
    workspace = (tmp_path / "bench").resolve()
    executable = tmp_path / "fake_openocd.py"
    executable.write_bytes(FAKE_OPENOCD.read_bytes())
    digest = hashlib.sha256(str(tmp_path).encode("utf-8")).hexdigest()[:12]
    second = {"other": {"type": "openocd", "executable": executable.as_posix(), "probe_id": f"JOBBENCH-OTHER-{digest}", "interface": "SWD"}}
    path = write_authoritative_config(
        workspace,
        monkeypatch,
        config_root=tmp_path / "configuration",
        config_version=4,
        state_root=tmp_path / "trusted-state",
        debugger_executable=executable,
        probe_id=f"JOBBENCH-DUT-{digest}",
        debuggers_yaml=yaml.safe_dump({"debuggers": second}),
    )
    job_root = (tmp_path / "jobs").resolve()
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    document["server"] = {"job_root": str(job_root), **server}
    document["artifacts"]["allow_upload"] = False
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return JobBench(workspace, path, job_root, load_authoritative_config(workspace))


def plan(*, name: str = "shared bench plan", device: str = "dut", image: str | None = None) -> bytes:
    """A version 4 plan that waits on `device`, flashing `image` first when one is named."""
    steps: list[JsonObject] = []
    if image is not None:
        steps.append({"debugger": device, "action": "flash", "image_path": image})
    steps.append({"action": "delay", "device": device, "duration_ms": 1})
    return yaml.safe_dump({"version": 4, "name": name, "steps": steps}, sort_keys=False).encode("utf-8")


def run_report(config: AgenticHILConfig, handle: str, *, name: str, ok: bool) -> JsonObject:
    """The canonical report of one fake attempt, in the fields a run report carries."""
    report: JsonObject = {
        "ok": ok,
        "tool": "test_reactor_run",
        "name": name,
        "test_config_path": str(Path(config.work_dir) / ".agentic-hil" / "testconfig.yaml"),
        "run": {"id": handle},
        "steps": [{"index": 1, "action": "delay", "device": "dut", "result": {"ok": ok, "summary": "Fixture step."}}],
        "summary": "Fixture run passed." if ok else "Fixture run failed.",
    }
    if not ok:
        report.update({"error_type": "fixture_step_failed", "failed_step": 1})
    return report


class FakeWorkerSource:
    """The scheduler's worker seam, with no process behind it."""

    def __init__(self) -> None:
        self.lock = Lock()
        self.records: dict[str, JsonObject] = {}
        self.configs: dict[str, AgenticHILConfig] = {}
        self.gone: set[str] = set()
        self.starts: list[str] = []
        self.stops: list[str] = []
        self.terminations: list[str] = []
        self.started = Event()

    def new_handle(self) -> str:
        return runlifecycle.new_run_handle()

    def start(self, config: AgenticHILConfig, handle: str, plan: Path, *, wait_s: float) -> None:
        loaded = yaml.safe_load(Path(plan).read_text(encoding="utf-8"))
        name = str(loaded.get("name") or "") if isinstance(loaded, dict) else ""
        with self.lock:
            self.starts.append(handle)
            self.configs[handle] = config
            self.records[handle] = {"version": 1, "run": handle, "state": "running", "name": name}
        if not name.startswith("held"):
            self.finish(handle, ok=not name.startswith("failing"))
        self.started.set()

    def perform_pending(self) -> None:
        """Nothing is deferred here: every start above took effect at once."""

    def finish(self, handle: str, *, ok: bool = True, state: str = "finished") -> None:
        """End one attempt the way a worker ends it: its report, its log, then its record."""
        with self.lock:
            config = self.configs[handle]
            record = dict(self.records[handle])
        report_path = canonical_run_report_path(config, handle)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(run_report(config, handle, name=str(record["name"]), ok=ok)), encoding="utf-8")
        logs = Path(config.logs.directory)
        logs.mkdir(parents=True, exist_ok=True)
        (logs / LOG_NAME).write_bytes(LOG_BYTES)
        with self.lock:
            self.records[handle] = {**record, "state": state, "run_ok": ok}
            self.gone.add(handle)

    def record(self, config: AgenticHILConfig, handle: str) -> JsonObject | None:
        with self.lock:
            return copy.deepcopy(self.records.get(handle))

    def is_gone(self, config: AgenticHILConfig, handle: str) -> bool:
        with self.lock:
            return handle in self.gone

    def stop(self, config: AgenticHILConfig, handle: str) -> None:
        """A worker asked to stop stops at its next step boundary and says so."""
        with self.lock:
            self.stops.append(handle)
            running = handle in self.records and handle not in self.gone
        if running:
            self.finish(handle, ok=False, state="stopped")

    def terminate(self, config: AgenticHILConfig, handle: str) -> None:
        with self.lock:
            self.terminations.append(handle)

    def handle_of(self, job_id: str) -> str:
        """The attempt the scheduler started for `job_id`: its workspace is `<job_root>/<job_id>/workspace`."""
        with self.lock:
            for handle, config in self.configs.items():
                if Path(config.work_dir).parent.name == job_id:
                    return handle
        raise AssertionError(f"no attempt was started for {job_id}")


@contextmanager
def open_service(
    bench: JobBench,
    workers: Any,
    *,
    settings: ServerSettings | None = None,
    recover_call: Callable[..., JsonObject] | None = None,
) -> Iterator[JobService]:
    """The real job service of `bench`, on the fake worker source."""
    with open_bench_job_service(
        bench.config,
        settings or short_settings(),
        worker_source=workers,
        recover_call=recover_call,
        reconcile_interval_s=RECONCILE_INTERVAL_S,
    ) as service:
        yield service


@contextmanager
def serving(service: JobService, settings: ServerSettings | None = None) -> Iterator[ThreadingHTTPServer]:
    """The real listener in front of `service`, on 127.0.0.1 and a port the kernel chose."""
    server = httpserver.create_http_server(settings or short_settings(), service)
    worker = Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
    worker.start()
    try:
        yield server
    finally:
        stopper = Thread(target=server.shutdown, daemon=True)
        stopper.start()
        stopper.join(scaled_time_bound(15))
        server.server_close()
        worker.join(scaled_time_bound(15))
        assert not stopper.is_alive(), "HTTP shutdown did not finish"
        assert not worker.is_alive(), "HTTP server thread did not finish"


def wait_for_state(service: JobService, job_id: str, states: Iterable[str]) -> JsonObject:
    """The job's status once its state is one of `states`, followed by revision, never by sleeping."""
    wanted = frozenset(states)
    deadline = time.monotonic() + scaled_time_bound(30)
    snapshot = service.status(job_id)
    while snapshot.status["state"] not in wanted:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AssertionError(f"{job_id} stayed {snapshot.status['state']} and never reached one of {sorted(wanted)}")
        changed = service.wait_for_revision(job_id, after_revision=snapshot.revision, timeout_s=remaining)
        snapshot = changed if changed is not None else service.status(job_id)
    return snapshot.status


def wait_until(predicate: Callable[[], bool], what: str) -> None:
    """Return once `predicate` holds; the bound only decides that it never did."""
    deadline = time.monotonic() + scaled_time_bound(60)
    pause = Event()
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError(f"{what} did not happen in time")
        pause.wait(scaled_time_bound(0.05))


def stored_jobs(bench: JobBench) -> dict[str, JsonObject]:
    """The job records of the store's snapshot, as the store wrote them to `<job_root>/queue.json`."""
    snapshot = bench.job_root / "queue.json"
    if not snapshot.exists():
        return {}
    jobs = json.loads(snapshot.read_text(encoding="utf-8"))["jobs"]
    assert isinstance(jobs, dict)
    return jobs
