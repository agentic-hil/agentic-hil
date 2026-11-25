"""`serve` runs on the real pieces (#891).

The listener is built through `cli.bench_server`, the code path `serve` uses,
on 127.0.0.1 and a port the kernel chose. The bench view, the /mcp reads and the
job store behind it are the real ones; only the worker source is the fake, so
nothing starts a process or reaches a device.
"""

from __future__ import annotations

import json
import time
from dataclasses import replace
from pathlib import Path
from threading import Event, Thread

import pytest
from bench_job_fakes import FakeWorkerSource, job_bench, plan
from httpserver_fakes import mcp_call, mcp_initialize, request
from support import scaled_time_bound

from agentic_hil import benchjobs, cli, httpapi

REPO = Path(__file__).resolve().parents[1]


@pytest.mark.usefixtures("no_loopback_name_lookup")
def test_serve_wires_the_bench_view_the_mcp_reads_and_the_job_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bench = job_bench(tmp_path, monkeypatch)
    workers = FakeWorkerSource()
    monkeypatch.setattr(benchjobs, "JobWorkerSource", lambda *args, **kwargs: workers)
    loaded = httpapi.load_server_settings(bench.config)
    assert loaded is not None
    settings = replace(loaded, port=0)
    with cli.bench_server(bench.config, settings=settings) as server:
        worker = Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
        worker.start()
        try:
            # An open read endpoint answers from the bench view.
            devices = request(server, "GET", "/v1/devices")
            assert devices.status == 200, devices
            assert {device["name"] for device in devices.json()["devices"]} == {"dut", "other"}
            # /mcp answers its reads from the same view.
            session, _ = mcp_initialize(server)
            described = mcp_call(server, session, "bench_describe")["structuredContent"]
            assert described["ok"] is True
            assert {device["name"] for device in described["devices"]} == {"dut", "other"}

            # A submission lands in the real job store under job_root.
            submitted = request(server, "POST", "/v1/jobs", body=plan(name="held wired"), headers={"Content-Type": "application/yaml", "From": "alice"})
            assert submitted.status == 202, submitted
            job_id = submitted.json()["job_id"]
            assert job_id in json.loads((bench.job_root / "queue.json").read_text(encoding="utf-8"))["jobs"]

            # Once it runs nothing moves it, and HTTP and /mcp report the same status.
            deadline = time.monotonic() + scaled_time_bound(30)
            pause = Event()
            over_http = request(server, "GET", f"/v1/jobs/{job_id}").json()
            while over_http["state"] != "running":
                assert time.monotonic() < deadline, over_http
                pause.wait(scaled_time_bound(0.05))
                over_http = request(server, "GET", f"/v1/jobs/{job_id}").json()
            over_mcp = mcp_call(server, session, "test_reactor_status", {"run": job_id})["structuredContent"]
            assert over_mcp == {"ok": True, "tool": "test_reactor_status", **over_http}
        finally:
            server.shutdown()
            worker.join(scaled_time_bound(15))
    assert not worker.is_alive()


def test_mypy_checks_the_new_bench_server_modules() -> None:
    """The job service and the remote client are type-checked with the rest of the server."""
    configuration = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    checked = configuration.split("[tool.mypy]", 1)[1].split("\n[", 1)[0]
    for module in ("src/agentic_hil/benchjobs.py", "src/agentic_hil/remote.py"):
        assert f'"{module}"' in checked, module
