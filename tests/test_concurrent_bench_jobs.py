"""A bench's disjoint jobs share incident accounting, not hardware leases."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from contextlib import closing, contextmanager
from dataclasses import replace
from pathlib import Path

import pytest
import yaml
from support import scaled_time_bound
from test_dead_owner_no_contact import no_contact_report
from test_read_until import ScriptedSerialHandle
from test_serial_port_identity import FakePortInfo
from test_server_jobs import (
    CLAIM,
    PLAN_NAME,
    REPO,
    _assert_attributed,
    _bench,
    _child,
    _child_environment,
    _install_child_fakes,
    _prepare,
    _rows,
    _write_document,
)

from agentic_hil.config import bind_debugger, load_authoritative_config
from agentic_hil.coordination import (
    DEBUGGER_DISCOVERY_RESOURCE,
    CoordinationError,
    HardwareCoordinator,
    com_resource,
    debugger_effect_resources,
    project_resource,
)
from agentic_hil.report import overall_success, write_report
from agentic_hil.runlifecycle import RUN_FINISHED, new_run_handle, read_run_record
from agentic_hil.serverjobs import _job_config
from agentic_hil.tools import AgenticHILToolService

DISCOVERY = DEBUGGER_DISCOVERY_RESOURCE


@pytest.fixture
def concurrent_bench(tmp_path, monkeypatch):
    workspace, path = _bench(tmp_path, monkeypatch)
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    primary = document["debuggers"]["dut"]
    document["debuggers"]["peer"] = {**primary, "probe_id": f'{primary["probe_id"]}-PEER'}
    for port in ("first", "second"):
        document["com_ports"][port] = {
            "device": f"COM_{port.upper()}_SCRIPTED",
            "serial_number": f'{primary["probe_id"]}-{port}',
            "permissions": {"allow_write": True},
        }
    _write_document(path, document)
    return load_authoritative_config(workspace)


@contextmanager
def _process(argv, cwd, environment, directory):
    # A process can report a refusal before it reaches the ready signal. File
    # output lets it finish even while the parent is waiting for that signal.
    with (directory / "stdout").open("wb") as stdout, (directory / "stderr").open("wb") as stderr:
        child = subprocess.Popen(
            [sys.executable, *argv],
            cwd=cwd,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
        )
    try:
        yield child
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=scaled_time_bound(10))


def _wait_for_signal(path, child=None):
    deadline = time.monotonic() + scaled_time_bound(30)
    while time.monotonic() < deadline:
        if path.exists():
            return True
        if child is not None and child.poll() is not None:
            return path.exists()
        time.sleep(0.02)
    raise TimeoutError(f"Timed out waiting for {path.name}")


def _finish(child, directory, allowed_codes):
    child.wait(timeout=scaled_time_bound(30))
    stdout = (directory / "stdout").read_text(encoding="utf-8")
    stderr = (directory / "stderr").read_text(encoding="utf-8")
    assert child.returncode in allowed_codes, (child.returncode, stdout, stderr)
    assert "Error in sitecustomize" not in stderr, stderr
    return stdout


def _lease_child(mode, config_path, workspace, job_id, resources, directory):
    """Hold a real coordinator in a separate process until the parent releases it."""
    bench = load_authoritative_config(workspace, config_path=config_path)
    config = _job_config(bench, job_id) if job_id else bench
    if mode == "workspace":
        config = replace(config, bench_ownership=None)
    coordinator = HardwareCoordinator(config, "test-child")
    if mode == "run":
        coordinator.begin_run(resources)
        lease = None
        ready = {}
    else:
        lease = coordinator.acquire(*resources)
        ready = {"lease_id": lease.lease_id}
        if mode == "crash":
            evidence = no_contact_report(
                coordinator, resources=list(lease.resources), lease_id=lease.lease_id,
                tool="com_session_start", error_type="com_port_open_failed",
                summary="The scripted port was never opened.",
            )
            assert write_report(config, evidence)["audit_ok"] is True
    temporary = directory / "ready.tmp"
    temporary.write_text(json.dumps(ready), encoding="utf-8")
    temporary.replace(directory / "ready")
    _wait_for_signal(directory / "release")
    if mode == "crash":
        os._exit(0)
    if lease is not None:
        assert lease.release()
    coordinator.close()


@contextmanager
def _held_in_child(tmp_path, bench, resources, *, mode="bench", job_id="holder"):
    directory = tmp_path / f"holder-signals-{job_id or 'local'}"
    directory.mkdir()
    environment = os.environ.copy()
    environment["PYTHONPATH"] = os.pathsep.join(map(str, (REPO / "tests", REPO / "src", REPO)))
    args = [mode, bench.config_path, bench.work_dir, job_id, resources, str(directory)]
    with _process(["-m", Path(__file__).stem, json.dumps(args)], bench.work_dir, environment, directory) as child:
        if not _wait_for_signal(directory / "ready", child):
            raise AssertionError(f"Lease holder exited before acquiring: {_finish(child, directory, (0,))}")
        ready = json.loads((directory / "ready").read_text(encoding="utf-8"))
        try:
            yield ready, directory / "release", child
        finally:
            (directory / "release").touch()
            _finish(child, directory, (0,))


@pytest.mark.parametrize(
    ("mode", "first", "second"),
    [
        pytest.param("bench", "uart", "uart", id="bench-uart-uart"),
        pytest.param("bench", "uart", "probe", id="bench-uart-probe"),
        pytest.param("workspace", "uart", "uart", id="workspace-uart-uart"),
        pytest.param("workspace", "probe", "probe", id="workspace-probe-probe"),
    ],
)
def test_disjoint_acquire(tmp_path, concurrent_bench, mode, first, second):
    bench = concurrent_bench
    first_resources = (
        (com_resource(bench, "first"),) if first == "uart"
        else debugger_effect_resources(bind_debugger(bench, "dut"))
    )
    second_resources = (
        (com_resource(bench, "second"),) if second == "uart"
        else debugger_effect_resources(bind_debugger(bench, "peer"))
    )
    config = _job_config(bench, "caller")
    if mode == "workspace":
        config = replace(config, bench_ownership=None)
    coordinator = HardwareCoordinator(config, "test-caller")
    with _held_in_child(tmp_path, bench, first_resources, mode=mode):
        try:
            lease = coordinator.acquire(*second_resources)
            assert set(lease.resources) == set(second_resources)
            assert lease.release()
        finally:
            coordinator.close()


def test_local_owner_and_job_refuse_the_same_device_by_name(tmp_path, concurrent_bench):
    bench = concurrent_bench
    resource = com_resource(bench, "first")
    with _held_in_child(tmp_path, bench, [resource], mode="run", job_id=""):
        coordinator = HardwareCoordinator(_job_config(bench, "caller"), "test-job")
        try:
            with pytest.raises(CoordinationError) as refusal:
                coordinator.acquire(resource)
            result = refusal.value.result
            assert result["error_type"] in {"device_busy", "resource_busy"}, result
            assert result.get("resources") == [resource] or result.get("resource") == resource, result
        finally:
            coordinator.close()


def test_discovery_refusal_identifies_the_contended_lock(tmp_path, concurrent_bench, monkeypatch):
    bench = concurrent_bench
    config = bind_debugger(replace(_job_config(bench, "caller"), bench_ownership=None), "dut")

    def unexpected_probe():
        pytest.fail("The probe backend ran during exclusive discovery")

    with _held_in_child(tmp_path, bench, [DISCOVERY], mode="workspace"):
        service = AgenticHILToolService(config)
        monkeypatch.setattr(service.backend, "probe_target", unexpected_probe)
        try:
            result = service.call("probe_target")
            assert result["error_type"] == "resource_busy", result
            if result.get("resources") != [DISCOVERY] and DISCOVERY not in result.values():
                raise AssertionError("The refusal did not identify the contended discovery lock.")
        finally:
            service.close()


def test_dead_worker_settles_without_erasing_live_owner(tmp_path, concurrent_bench):
    bench = concurrent_bench
    dead_resource = com_resource(bench, "first")
    live_resource = com_resource(bench, "second")
    spare_resource = com_resource(bench, "feedback")
    with _held_in_child(tmp_path, bench, [dead_resource], mode="crash") as (first, first_release, first_child):
        with closing(HardwareCoordinator(_job_config(bench, "caller"), "test-caller")) as caller:
            disjoint = caller.acquire(spare_resource)
            assert disjoint.release()

        with _held_in_child(tmp_path, bench, [live_resource], mode="crash", job_id="survivor") as (
            survivor, survivor_release, survivor_child,
        ):
            first_release.touch()
            _finish(first_child, first_release.parent, (0,))
            with closing(HardwareCoordinator(_job_config(bench, "successor"), "test-successor")) as successor:
                next_lease = successor.acquire(spare_resource)
                ledger = successor.root / "recovery.jsonl"
                first_recovery, = _no_contact_recoveries(ledger)
                assert first_recovery["lease_id"] == first["lease_id"]
                assert first_recovery["resources"] == [dead_resource]
                assert next_lease.release()
                reclaimed = successor.acquire(dead_resource)
                assert reclaimed.release()
                with pytest.raises(CoordinationError) as refusal:
                    successor.acquire(live_resource)
                result = refusal.value.result
                assert result["error_type"] in {"device_busy", "resource_busy"}, result
                assert result.get("resources") == [live_resource] or result.get("resource") == live_resource, result

            assert survivor_child.poll() is None
            survivor_release.touch()
            _finish(survivor_child, survivor_release.parent, (0,))

    # A fresh acquisition on a third device must still find the survivor's
    # original lease and use the report written before the first child exited.
    with closing(HardwareCoordinator(_job_config(bench, "last-caller"), "test-last-caller")) as last_caller:
        next_lease = last_caller.acquire(spare_resource)
        original, second_recovery = _no_contact_recoveries(ledger)
        assert original == first_recovery
        assert second_recovery["lease_id"] == survivor["lease_id"]
        assert second_recovery["resources"] == [live_resource]
        assert next_lease.release()
        reclaimed = last_caller.acquire(live_resource)
        assert reclaimed.release()


def _no_contact_recoveries(ledger):
    return [
        row
        for row in _rows(ledger)
        if row.get("recovery") == "dead_owner_no_contact"
    ]


class _WorkerSerial(ScriptedSerialHandle):
    def _record(self, action):
        with Path(os.environ["SERVERJOBS_CAPTURE"]).open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"action": action, "port": self.port}) + "\n")

    def open(self):
        super().open()
        self._record("uart_open")

    def write(self, data):
        self._record("uart_write")
        directory = Path(os.environ["CONCURRENT_JOB_SIGNALS"])
        (directory / "ready").touch()
        _wait_for_signal(directory / "release")
        return super().write(data)

    def close(self):
        super().close()
        self._record("uart_close")


def _install_worker_fakes():
    import serial
    from serial.tools import list_ports

    _install_child_fakes()
    serial.Serial = _WorkerSerial
    ports = [FakePortInfo(*port) for port in json.loads(os.environ["CONCURRENT_JOB_PORTS"])]
    list_ports.comports = lambda: ports


@contextmanager
def _start_worker(tmp_path, bench, job, label, handle, *, mode="normal"):
    directory = tmp_path / label
    directory.mkdir()
    capture = directory / "backend.jsonl"
    environment = _child_environment(directory, capture, mode)
    environment["CONCURRENT_JOB_SIGNALS"] = str(directory)
    environment["CONCURRENT_JOB_PORTS"] = json.dumps([
        (port.device, port.serial_number) for port in bench.com_ports.values() if port.serial_number
    ])
    (directory / "trusted-bootstrap" / "sitecustomize.py").write_text(
        "from test_concurrent_bench_jobs import _install_worker_fakes\n_install_worker_fakes()\n",
        encoding="utf-8",
    )
    args = [
        "-m", "agentic_hil.serverjobs", "--bench-config", bench.config_path,
        "--bench-workspace", bench.work_dir, "--job-id", label,
        "--submitted-by", CLAIM, "--plan", PLAN_NAME, "--run-handle", handle,
    ]
    with _process(args, job.work_dir, environment, directory) as child:
        yield child, directory, capture


def _worker_result(child, directory):
    report = json.loads(_finish(child, directory, (0, 1)))
    assert (child.returncode == 0) == overall_success(report), report
    return report


def _plan(port, *, debugger=None, pause=False, flash_after_close=False):
    steps = [{"port_id": port, "action": "uart_open"}]
    close = {"port_id": port, "action": "uart_close"}
    if debugger and not flash_after_close:
        steps.extend([
            {"debugger": debugger, "action": "flash", "image_path": "build/app.elf"},
            {"debugger": debugger, "action": "reset"},
        ])
    if pause:
        steps.append({"port_id": port, "action": "uart_write", "text": "proceed"})
    steps.append(close)
    if debugger and flash_after_close:
        steps.append({"debugger": debugger, "action": "flash", "image_path": "build/app.elf"})
    return json.dumps({"version": 3, "steps": steps}).encode()


@pytest.mark.parametrize("mode", ["normal", "audit-fault"])
def test_overlapping_workers(tmp_path, concurrent_bench, mode):
    bench = concurrent_bench
    fault = mode == "audit-fault"
    second_devices = {com_resource(bench, "second"), *debugger_effect_resources(bind_debugger(bench, "peer"))} - {DISCOVERY}
    first = _prepare(bench, "first", _plan("first", debugger="dut" if fault else None, pause=True, flash_after_close=fault))
    second = _prepare(bench, "second", _plan("second", debugger="peer", pause=fault))
    first_handle, second_handle = new_run_handle(), new_run_handle()
    quarantine_id = None
    with _start_worker(tmp_path, bench, first, "first", first_handle, mode=mode) as (a, a_signals, a_capture):
        if not _wait_for_signal(a_signals / "ready", a):
            pytest.fail(f"First worker exited before opening its UART: {_worker_result(a, a_signals)}")
        with _start_worker(tmp_path, bench, second, "second", second_handle) as (b, b_signals, b_capture):
            if fault and _wait_for_signal(b_signals / "ready", b):
                assert a.poll() is None and b.poll() is None
                (a_signals / "release").touch()
                a_result = _worker_result(a, a_signals)
                assert not overall_success(a_result), a_result
                assert b.poll() is None
                quarantine_id = _standing_bench_quarantine_id(bench, second_devices)
                first_actions = [row["action"] for row in _rows(a_capture)]
                assert first_actions[:4] == ["uart_open", "uart_write", "uart_close", "flash"], first_actions
                assert b.poll() is None
                (b_signals / "release").touch()
                b_result = _worker_result(b, b_signals)
            else:
                b_result = _worker_result(b, b_signals)
                assert a.poll() is None
                (a_signals / "release").touch()
                a_result = _worker_result(a, a_signals)

    record = read_run_record(second, second_handle)
    assert record and record["run"] == second_handle and record["state"] == RUN_FINISHED, record
    if fault:
        assert not overall_success(a_result), a_result
        assert a_result.get("audit_ok") is False or a_result.get("quarantined") is True, a_result
    else:
        assert overall_success(a_result), a_result
    if record["run_ok"] is not True:
        assert record.get("step_error_type") == "resource_busy", record
        if fault:
            raise AssertionError("The disjoint worker failed in the audit-fault scenario.")
        raise AssertionError("The disjoint worker failed while the first UART session was open.")
    assert overall_success(b_result), b_result
    _assert_attributed(b_result, "second")
    assert [row["action"] for row in _rows(b_capture)] == (
        ["uart_open", "flash", "reset", "uart_write", "uart_close"] if fault
        else ["uart_open", "flash", "reset", "uart_close"]
    )
    if not fault:
        assert [row["action"] for row in _rows(a_capture)] == ["uart_open", "uart_write", "uart_close"]
        _check_concurrency_documentation()
        return

    assert quarantine_id is not None, "The successful second worker never reached its audit-fault pause."
    assert _standing_bench_quarantine_id(bench, second_devices) == quarantine_id

    blocked_job = _prepare(bench, "blocked", _plan("second"))
    with _start_worker(tmp_path, bench, blocked_job, "blocked", new_run_handle()) as (child, signals, capture):
        blocked = _worker_result(child, signals)
        assert blocked["error_type"] == "resource_quarantined", blocked
        assert blocked["quarantine_id"] == quarantine_id and not _rows(capture), blocked
    recovered, calls = _child(tmp_path, [
        "-m", "agentic_hil", "recover", "--confirm-safe-state", "--quarantine-id", quarantine_id, "--json",
    ], bench.work_dir, label="bench-recover")
    assert overall_success(recovered) and not calls, recovered
    assert _bench_status(bench)["incident_stands"] is False


def _bench_status(bench):
    with closing(HardwareCoordinator(bench, "test-reader")) as coordinator:
        return coordinator.status(settle_dead_owner=False)


def _standing_bench_quarantine_id(bench, peer_devices):
    status = _bench_status(bench)
    assert status["incident_stands"] is True, status
    quarantine_id = status["quarantine_id"]
    assert isinstance(quarantine_id, str) and quarantine_id, status
    incident = status["record"]
    assert incident and incident["project_resource"] == project_resource(bench), incident
    assert incident["quarantine_id"] == quarantine_id, incident
    assert peer_devices.isdisjoint(incident["resources"]), incident
    return quarantine_id


def _check_concurrency_documentation():
    text = (REPO / "docs" / "shared-bench.md").read_text(encoding="utf-8").lower()
    paragraphs = [" ".join(paragraph.split()) for paragraph in text.split("\n\n")]
    assert any(
        re.search(r"\b(disjoint|separate|different|distinct|independent|non-overlapping)\b", paragraph)
        and re.search(r"\b(concurren\w*|overlap\w*|parallel|side by side|same time|at once)\b", paragraph)
        and "job" in paragraph and "device" in paragraph
        for paragraph in paragraphs
    ), "Document how jobs using disjoint devices can overlap."
    assert any("device" in paragraph and "never drive a device at once" in paragraph for paragraph in paragraphs) or any(
        "device" in paragraph and re.search(r"\b(exclusiv\w*|one (?:owner|run|job) at a time|must wait)\b", paragraph)
        for paragraph in paragraphs
    ), "Document exclusion on shared physical devices."
    assert any(
        re.search(r"\b(discover\w*|enumerat\w*)\b", paragraph)
        and re.search(r"\b(exclusiv\w*|wait\w*|serializ\w*|one at a time|cannot overlap|runs alone)\b", paragraph)
        for paragraph in paragraphs
    ), "Document when debugger discovery excludes other operations."


if __name__ == "__main__":
    mode, config_path, workspace, job_id, resources, directory = json.loads(sys.argv[1])
    _lease_child(mode, config_path, workspace, job_id, resources, Path(directory))
