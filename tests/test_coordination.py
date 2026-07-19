from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from conftest import write_config

from agentic_hil.adapters import AdapterService
from agentic_hil.bridge import BRIDGE_PROTOCOL_VERSION, BridgeCleanupError, ProcessBridgeSession
from agentic_hil.can import CanBusService, normalize_received_frames
from agentic_hil.cli import debugger_probes, entrypoint
from agentic_hil.config import ConfigError, load_config
from agentic_hil.coordination import (
    DEBUGGER_DISCOVERY_RESOURCE,
    CoordinationError,
    HardwareCoordinator,
    _LifetimeLock,
    resource_digest,
)
from agentic_hil.mcp import call_tool
from agentic_hil.process import (
    cleanup_registered_processes,
    managed_process_owner,
    process_group_kwargs,
    spawn_managed_process,
    terminate_process_tree,
)
from agentic_hil.report import overall_success, read_last_report, report_state_path, write_report
from agentic_hil.tools import AgenticHILToolService


def config_for(workspace: Path, **kwargs):
    return load_config(str(write_config(workspace, **kwargs)))


def test_live_owner_blocks_second_coordinator_before_resource_use(tmp_path: Path) -> None:
    config = config_for(tmp_path)
    first = HardwareCoordinator(config, "first")
    second = HardwareCoordinator(config, "second")
    lease = first.acquire("physical:test")
    try:
        with pytest.raises(CoordinationError) as excinfo:
            second.acquire("physical:test")
        assert excinfo.value.result["error_type"] == "resource_busy"
    finally:
        lease.release()
        first.close()
        second.close()


def test_pinned_state_root_coordinates_processes_with_different_environments(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_path = write_config(tmp_path)
    ready = tmp_path / "owner-ready"
    stop = tmp_path / "owner-stop"
    script = """
import sys, time
from pathlib import Path
from agentic_hil.config import load_config
from agentic_hil.coordination import HardwareCoordinator
config = load_config(sys.argv[1])
coordinator = HardwareCoordinator(config, 'child')
lease = coordinator.acquire('physical:shared-environment')
Path(sys.argv[2]).write_text('ready', encoding='utf-8')
while not Path(sys.argv[3]).exists():
    time.sleep(0.02)
lease.release()
coordinator.close()
"""
    environment = os.environ.copy()
    environment["LOCALAPPDATA"] = str(tmp_path / "child-state")
    environment["XDG_STATE_HOME"] = str(tmp_path / "child-state")
    dependency_root = str(Path(yaml.__file__).resolve().parents[1])
    source_root = str(Path(__file__).resolve().parents[1] / "src")
    environment["PYTHONPATH"] = os.pathsep.join([dependency_root, source_root, environment.get("PYTHONPATH", "")])
    child = subprocess.Popen([sys.executable, "-c", script, str(config_path), str(ready), str(stop)], env=environment)
    try:
        assert wait_for_file(ready, child), "child did not acquire pinned-root lease"
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "parent-state"))
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "parent-state"))
        coordinator = HardwareCoordinator(load_config(str(config_path)), "parent")
        with pytest.raises(CoordinationError) as excinfo:
            coordinator.acquire("physical:shared-environment")
        assert excinfo.value.result["error_type"] == "resource_busy"
    finally:
        stop.write_text("stop", encoding="utf-8")
        child.wait(timeout=10)


def wait_for_file(path: Path, child: subprocess.Popen, timeout_s: float = 10) -> bool:
    deadline = time.monotonic() + timeout_s
    while not path.exists() and child.poll() is None and time.monotonic() < deadline:
        time.sleep(0.02)
    return path.exists()


def test_different_projects_conflict_on_same_physical_resource(tmp_path: Path) -> None:
    first_config = config_for(tmp_path / "first")
    second_config = config_for(tmp_path / "second")
    first = HardwareCoordinator(first_config, "first")
    second = HardwareCoordinator(second_config, "second")
    lease = first.acquire("physical:shared")
    try:
        with pytest.raises(CoordinationError) as excinfo:
            second.acquire("physical:shared")
        assert excinfo.value.result["error_type"] == "resource_busy"
    finally:
        lease.release()
        first.close()
        second.close()


def test_lifetime_lock_rejects_final_component_symlink(tmp_path: Path) -> None:
    target = tmp_path / "target.lock"
    target.write_text("0", encoding="utf-8")
    link = tmp_path / "link.lock"
    try:
        link.symlink_to(target)
    except OSError as error:
        pytest.skip(f"file symlinks unavailable: {error}")

    lock = _LifetimeLock(link)
    with pytest.raises((ConfigError, OSError)):
        lock.acquire()
    assert lock.descriptor == -1


def test_stale_resource_keeps_project_cleanup_required(tmp_path: Path) -> None:
    config = config_for(tmp_path)
    coordinator = HardwareCoordinator(config, "stale-resource")
    resource = "physical:stale"
    coordinator._write_record(resource, coordinator._base_record("active", [resource]))
    coordinator._write_record(coordinator.project_key, coordinator._base_record("released", []))

    with pytest.raises(CoordinationError) as excinfo:
        coordinator.acquire(resource)

    assert excinfo.value.result["error_type"] == "resource_quarantined"
    status = coordinator.status()
    assert status["blocked"] is True
    assert status["record"]["state"] == "cleanup_required"
    assert status["record"]["resources"] == [resource]
    assert coordinator.recover(safe_state_confirmed=True, quarantine_id=status["quarantine_id"])["ok"] is True


def test_process_cleanup_is_scoped_to_service_owner() -> None:
    with managed_process_owner("first-owner"):
        first = spawn_managed_process([sys.executable, "-c", "import time; time.sleep(60)"], **process_group_kwargs())
    with managed_process_owner("second-owner"):
        second = spawn_managed_process([sys.executable, "-c", "import time; time.sleep(60)"], **process_group_kwargs())
    try:
        assert cleanup_registered_processes(owner_marker="first-owner") == []
        assert first.poll() is not None
        assert second.poll() is None
    finally:
        if first.poll() is None:
            terminate_process_tree(first, 5)
        if second.poll() is None:
            terminate_process_tree(second, 5)


@pytest.mark.parametrize(
    ("service_type", "patch_target"),
    [
        (CanBusService, "agentic_hil.can.cleanup_registered_processes"),
        (AdapterService, "agentic_hil.adapters.cleanup_registered_processes"),
    ],
)
def test_direct_process_service_reaps_owned_orphans_before_close(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    service_type,
    patch_target: str,
) -> None:
    service = service_type(config_for(tmp_path))
    owners: list[str | None] = []

    def fail_cleanup(*, owner_marker: str | None = None) -> list[str]:
        owners.append(owner_marker)
        return ["orphan remained"]

    monkeypatch.setattr(patch_target, fail_cleanup)
    with pytest.raises(RuntimeError, match="orphan remained"):
        service.close()
    assert owners == [service.coordinator.owner_marker]
    assert service.coordinator._state == "open"

    monkeypatch.setattr(patch_target, lambda *, owner_marker=None: [])
    service.close()
    assert service.coordinator._state == "closed"


def test_killed_owner_requires_explicit_recovery(tmp_path: Path) -> None:
    config_path = write_config(tmp_path)
    marker = tmp_path / "owner-ready"
    script = """
import sys, time
from pathlib import Path
from agentic_hil.config import load_config
from agentic_hil.coordination import HardwareCoordinator
config = load_config(sys.argv[1])
coordinator = HardwareCoordinator(config, 'child')
coordinator.acquire('physical:crash-test')
Path(sys.argv[2]).write_text('ready', encoding='utf-8')
time.sleep(60)
"""
    environment = os.environ.copy()
    dependency_root = str(Path(yaml.__file__).resolve().parents[1])
    source_root = str(Path(__file__).resolve().parents[1] / "src")
    environment["PYTHONPATH"] = os.pathsep.join([dependency_root, source_root, environment.get("PYTHONPATH", "")])
    child = subprocess.Popen([sys.executable, "-c", script, str(config_path), str(marker)], env=environment)
    try:
        deadline = time.monotonic() + 10
        while not marker.exists() and child.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert marker.exists(), "child did not acquire lease"
    finally:
        child.kill()
        child.wait(timeout=10)

    config = load_config(str(config_path))
    coordinator = HardwareCoordinator(config, "recovery")
    with pytest.raises(CoordinationError) as excinfo:
        coordinator.acquire("physical:crash-test")
    assert excinfo.value.result["error_type"] == "resource_quarantined"

    denied = coordinator.recover(safe_state_confirmed=False)
    assert denied["error_type"] == "operator_confirmation_required"
    quarantine_id = coordinator.status()["quarantine_id"]
    recovered = coordinator.recover(safe_state_confirmed=True, quarantine_id=quarantine_id)
    assert recovered["ok"] is True
    lease = coordinator.acquire("physical:crash-test")
    assert lease.release() is True
    coordinator.close()


@pytest.mark.parametrize(
    "response",
    [
        {"ok": False},
        {"ok": True, "protocol_version": BRIDGE_PROTOCOL_VERSION},
        {"ok": True, "protocol_version": BRIDGE_PROTOCOL_VERSION, "safe_state_confirmed": False},
    ],
)
def test_bridge_close_requires_safe_state_ack(monkeypatch: pytest.MonkeyPatch, response: dict) -> None:
    child = SimpleNamespace(stdout=[], stderr=[], poll=lambda: None)
    session = ProcessBridgeSession(child)
    monkeypatch.setattr(session, "request", lambda method, params, timeout: response)
    monkeypatch.setattr("agentic_hil.bridge.terminate_process_tree", lambda process, timeout: None)

    with pytest.raises(BridgeCleanupError) as excinfo:
        session.close()

    assert excinfo.value.result["safe_state_confirmed"] is False
    assert excinfo.value.result["process_reaped"] is True
    assert session.closed is False


def test_bridge_close_releases_only_after_ack_and_reap(monkeypatch: pytest.MonkeyPatch) -> None:
    child = SimpleNamespace(stdout=[], stderr=[], poll=lambda: None)
    session = ProcessBridgeSession(child)
    monkeypatch.setattr(
        session,
        "request",
        lambda method, params, timeout: {"ok": True, "protocol_version": BRIDGE_PROTOCOL_VERSION, "safe_state_confirmed": True},
    )
    monkeypatch.setattr("agentic_hil.bridge.terminate_process_tree", lambda process, timeout: None)

    result = session.close()

    assert result["ok"] is True
    assert result["safe_state_confirmed"] is True
    assert result["process_reaped"] is True
    assert session.closed is True


@pytest.mark.parametrize(
    ("response", "expected_error"),
    [
        ({"result": {"ok": True}, "error": {"summary": "ambiguous"}}, "bridge_invalid_response"),
        ({"result": {"summary": "missing status"}}, "bridge_invalid_response"),
        ({"result": {"ok": True}, "extra": "unsupported"}, "bridge_invalid_response"),
        ({"error": {"ok": True, "summary": "failed"}}, None),
    ],
)
def test_bridge_request_validates_response_envelope(response: dict, expected_error: str | None) -> None:
    class Input:
        session: ProcessBridgeSession

        def write(self, line: str) -> None:
            request = json.loads(line)
            self.session.pending[request["id"]].put({"id": request["id"], **response})

        def flush(self) -> None:
            pass

    input_stream = Input()
    child = SimpleNamespace(stdout=[], stderr=[], stdin=input_stream, poll=lambda: None)
    session = ProcessBridgeSession(child)
    input_stream.session = session

    result = session.request("read", {}, 0.1)

    assert result["ok"] is False
    if expected_error is not None:
        assert result["error_type"] == expected_error


def test_bridge_request_rejects_nonfinite_outgoing_json() -> None:
    child = SimpleNamespace(stdout=[], stderr=[], stdin=SimpleNamespace(write=lambda line: None, flush=lambda: None), poll=lambda: None)
    session = ProcessBridgeSession(child)

    result = session.request("write", {"value": float("inf")}, 0.1)

    assert result["ok"] is False
    assert result["error_type"] == "bridge_invalid_request"


def test_tool_validation_blocks_backend_before_audit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = config_for(tmp_path, can_buses_yaml='can_buses:\n  bench:\n    channel: "vcan0"\n')
    service = AgenticHILToolService(config)
    called = False

    def send(*args, **kwargs):
        nonlocal called
        called = True
        return {"ok": True}

    monkeypatch.setattr(service.can_buses, "send", send)
    try:
        result = service.call("can_send", {"bus_id": "bench", "frame_id": 1, "extended": "false", "data_hex": "01"})
    finally:
        service.close()

    assert result["error_type"] == "invalid_argument"
    assert result["field"] == "extended"
    assert called is False


def test_mcp_marks_unsafe_results_as_errors() -> None:
    class Tools:
        def __init__(self, result: dict):
            self.result = result

        def call(self, name: str, arguments: dict) -> dict:
            return self.result

    for result in (
        {"ok": True, "audit_ok": False},
        {"ok": True, "target_ok": False},
        {"ok": True, "cleanup_ok": False},
        {"ok": True, "cleanup_required": True},
        {"ok": True, "quarantined": True},
        {"ok": True, "lease_state": "cleanup_required"},
        {"ok": True, "side_effect_status": "unknown"},
        {"ok": True, "side_effect_status": "partial"},
        {"ok": True, "hardware_state": "unknown"},
    ):
        response = call_tool({"name": "probe_target", "arguments": {}}, Tools(result))  # type: ignore[arg-type]
        assert response["isError"] is True
        assert response["structuredContent"] == result


@pytest.mark.parametrize("value", [".nan", ".inf", "-.inf"])
def test_nonfinite_config_timeout_is_rejected(tmp_path: Path, value: str) -> None:
    path = write_config(tmp_path)
    text = path.read_text(encoding="utf-8").replace("timeout_s: 5", f"timeout_s: {value}", 1)
    path.write_text(text, encoding="utf-8")

    with pytest.raises(ConfigError) as excinfo:
        load_config(str(path))

    assert excinfo.value.error_type == "config_invalid"
    assert excinfo.value.details["field"] == "debugger.timeout_s"
    json.dumps(excinfo.value.to_dict(), allow_nan=False)


def test_workspace_snapshots_never_bootstrap_canonical_state(tmp_path: Path) -> None:
    config = config_for(tmp_path)
    reports = tmp_path / ".agentic-hil" / "reports"
    reports.mkdir(parents=True)
    reports.joinpath("last-report.json").write_text('{"ok": false, "operation": "forged"}\n', encoding="utf-8")
    reports.joinpath("last-failure.json").write_text('{"ok": false, "operation": "forged"}\n', encoding="utf-8")

    missing = read_last_report(config)
    assert missing["error_type"] == "report_not_found"
    assert not Path(report_state_path(config)).is_relative_to(tmp_path)

    write_report(config, {"ok": True, "operation": "trusted"})
    assert read_last_report(config)["operation"] == "trusted"


def test_report_state_namespaces_config_and_workspace(tmp_path: Path) -> None:
    policy_dir = tmp_path / "policies"
    first_workspace = tmp_path / "first"
    second_workspace = tmp_path / "second"
    first = load_config(str(write_config(first_workspace, workspace_root=first_workspace, config_path=policy_dir / "first.yaml")))
    second = load_config(str(write_config(second_workspace, workspace_root=second_workspace, config_path=policy_dir / "second.yaml")))

    assert report_state_path(first) != report_state_path(second)
    write_report(first, {"ok": True, "operation": "first"})
    assert read_last_report(second)["error_type"] == "report_not_found"


def test_unknown_can_effect_remains_quarantined_after_session_cleanup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = config_for(tmp_path, can_buses_yaml='can_buses:\n  bench:\n    adapter: "socketcan"\n    channel: "vcan0"\n')

    class Adapter:
        adapter_name = "fake"

        def __init__(self) -> None:
            self.active = True
            self.send_calls = 0

        def read(self, max_frames: int, wait_timeout_s: float) -> dict:
            return {"ok": True, "frames": []}

        def send(self, frame) -> dict:
            self.send_calls += 1
            return {"ok": False, "error_type": "can_send_failed", "summary": "write outcome unknown"}

        def close(self) -> dict:
            self.active = False
            return {"ok": True, "safe_state_confirmed": True, "process_reaped": True}

        def status(self) -> dict:
            return {"active": self.active}

    adapter = Adapter()
    monkeypatch.setattr("agentic_hil.can.open_adapter", lambda *args, **kwargs: {"ok": True, "session": adapter, "backend": "fake"})
    service = CanBusService(config)
    try:
        assert service.session_start("bench", True)["ok"] is True
        first = service.send("bench", {"frame_id": 1, "data_hex": "01"})
        second = service.send("bench", {"frame_id": 1, "data_hex": "02"})
        assert first["cleanup_required"] is True
        assert second["error_type"] == "resource_quarantined"
        assert adapter.send_calls == 1
        stopped = service.session_stop("bench")
        assert stopped["ok"] is False
        assert stopped["cleanup_required"] is True
    finally:
        with pytest.raises(RuntimeError):
            service.close()
        service.coordinator.close()


def test_malformed_can_frames_quarantine_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = config_for(tmp_path, can_buses_yaml='can_buses:\n  bench:\n    adapter: "socketcan"\n    channel: "vcan0"\n')

    class Adapter:
        adapter_name = "fake"

        def __init__(self) -> None:
            self.read_calls = 0

        def read(self, max_frames: int, wait_timeout_s: float) -> dict:
            self.read_calls += 1
            return {"ok": True, "frames": [] if self.read_calls == 1 else [{"id": "invalid", "data_hex": "00"}]}

        def close(self) -> dict:
            return {"ok": True, "safe_state_confirmed": True, "process_reaped": True}

        def status(self) -> dict:
            return {"active": True}

    adapter = Adapter()
    monkeypatch.setattr("agentic_hil.can.open_adapter", lambda *args, **kwargs: {"ok": True, "session": adapter, "backend": "fake"})
    service = CanBusService(config)
    try:
        assert service.session_start("bench", True)["ok"] is True

        result = service.read("bench", 1, 0.0)

        assert result["error_type"] == "can_adapter_invalid_response"
        assert result["cleanup_required"] is True
        assert service.coordinator.blocked is True
    finally:
        with pytest.raises(RuntimeError):
            service.close()
        service.coordinator.close()


def test_normalize_received_frames_accepts_python_can_id_hex() -> None:
    frames = normalize_received_frames([{"id": 0x123, "id_hex": "0x123", "extended": False, "rtr": False, "data_hex": "0102", "dlc": 2}])

    assert frames is not None
    assert frames[0]["id"] == 0x123


def test_adapter_malformed_measurement_quarantines_and_shutdown_reports(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    executable = Path(__file__).resolve().parents[1] / "examples" / "adapters" / "sim_ntc_adapter.py"
    config = config_for(tmp_path, adapters_yaml=f'adapters:\n  ntc:\n    executable: "{executable.as_posix()}"\n    channels: ["temperature"]\n')
    service = AdapterService(config)
    try:
        assert service.session_start("ntc")["ok"] is True
        bridge = service.sessions["ntc"].bridge
        request = bridge.request
        monkeypatch.setattr(bridge, "request", lambda method, params, timeout: {"ok": False, "error_type": "adapter_bridge_invalid_response", "summary": "missing boolean ok"} if method == "measure" else request(method, params, timeout))

        result = service.measure("ntc", {"channel": "temperature"})

        assert result["error_type"] == "adapter_bridge_invalid_response"
        assert result["cleanup_required"] is True
    finally:
        with pytest.raises(RuntimeError):
            service.close()
        service.coordinator.close()
    report = read_last_report(config)
    assert report["tool"] == "adapter_session_stop"
    assert report["lease_state"] == "cleanup_required"


def test_pyocd_does_not_reset_after_flash_audit_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from agentic_hil.backends.pyocd import PyOCDBackend

    config = config_for(tmp_path, debugger_type="pyocd")
    backend = PyOCDBackend(config)
    calls: list[list[str]] = []

    def run(tool: str, args: list[str]) -> dict:
        calls.append(args)
        return {"ok": True, "audit_ok": False, "audit_error": {"error_type": "audit_write_failed"}}

    monkeypatch.setattr(backend, "_run_pyocd", run)
    monkeypatch.setattr(backend, "_write_action_report", lambda result: result)

    result = backend.flash_firmware({"resolved_path": str(tmp_path / "build" / "app.elf"), "path": "build/app.elf"}, True)

    assert len(calls) == 1
    assert result["reset_after_flash"] is False
    assert result["reset_skipped_reason"] == "audit_failed"


def test_closed_coordinator_invalidates_stale_lease(tmp_path: Path) -> None:
    config = config_for(tmp_path)
    coordinator = HardwareCoordinator(config, "stale")
    lease = coordinator.acquire("physical:stale-lease")
    coordinator.close()
    record_before = coordinator._read_record(coordinator.project_key)

    assert lease.release() is False
    assert lease.state == "stale"
    assert coordinator._read_record(coordinator.project_key) == record_before
    with pytest.raises(CoordinationError, match="closed"):
        coordinator.acquire("physical:new")


def test_quarantined_lease_cannot_release_incident(tmp_path: Path) -> None:
    coordinator = HardwareCoordinator(config_for(tmp_path), "owner")
    lease = coordinator.acquire("physical:quarantine-release")
    lease.quarantine("unknown_effect")
    record_before = coordinator._read_record(coordinator.project_key)

    assert lease.release() is False
    assert lease.state == "cleanup_required"
    assert coordinator._read_record(coordinator.project_key) == record_before
    coordinator.close()


def test_close_retries_lock_release_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    coordinator = HardwareCoordinator(config_for(tmp_path), "owner")
    lease = coordinator.acquire("physical:close-retry")
    lock = lease.locks[0]
    original_release = lock.release
    monkeypatch.setattr(lock, "release", lambda: (_ for _ in ()).throw(OSError("close failed")))

    with pytest.raises(RuntimeError, match="close failed"):
        coordinator.close()
    assert coordinator._state == "cleanup_required"
    assert lease.lease_id in coordinator.leases

    monkeypatch.setattr(lock, "release", original_release)
    coordinator.close()
    assert coordinator._state == "closed"


def test_stale_multi_resource_incident_marks_every_resource_for_recovery(tmp_path: Path) -> None:
    config = config_for(tmp_path)
    setup = HardwareCoordinator(config, "setup")
    resources = ["physical:first", "physical:second"]
    active = setup._base_record("active", resources)
    for resource in resources:
        setup._write_record(resource, active)
    setup._write_record(setup.project_key, setup._base_record("released", []))

    with pytest.raises(CoordinationError):
        setup.acquire(resources[0])

    status = setup.status()
    for resource in resources:
        marker = setup._read_record(resource)
        assert marker["state"] == "quarantined"
        assert marker["resources"] == resources
        assert marker["quarantine_id"] == status["quarantine_id"]
    assert setup.recover(safe_state_confirmed=True, quarantine_id=status["quarantine_id"])["ok"] is True


def test_foreign_project_incident_is_not_rewritten(tmp_path: Path) -> None:
    first_config = config_for(tmp_path / "first")
    second_config = config_for(tmp_path / "second")
    first = HardwareCoordinator(first_config, "first")
    lease = first.acquire("physical:foreign")
    lease.quarantine("first_incident")
    first.close()
    marker_before = first._read_record("physical:foreign")
    second = HardwareCoordinator(second_config, "second")

    with pytest.raises(CoordinationError) as excinfo:
        second.acquire("physical:foreign")

    assert excinfo.value.result["error_type"] == "resource_quarantined"
    assert second._read_record("physical:foreign") == marker_before


def test_service_close_is_terminal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service = AgenticHILToolService(config_for(tmp_path))
    calls = 0

    def probe() -> dict:
        nonlocal calls
        calls += 1
        return {"ok": True}

    monkeypatch.setattr(service.backend, "probe_target", probe)
    service.close()
    result = service.call("probe_target")

    assert result["error_type"] == "service_closed"
    assert calls == 0
    service.close()


def test_dispatch_depth_is_thread_local(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = config_for(tmp_path, permissions_yaml="permissions:\n  allow_probe: true\n  allow_reset: false\n")
    service = AgenticHILToolService(config)
    calls = 0

    def reset(mode: str) -> dict:
        nonlocal calls
        calls += 1
        return {"ok": True}

    monkeypatch.setattr(service.backend, "reset_target", reset)
    service._dispatch_depth = 1
    results: list[dict] = []
    worker = threading.Thread(target=lambda: results.append(service.reset_target()))
    worker.start()
    worker.join()
    service._dispatch_depth = 0
    service.close()

    assert results[0]["error_type"] == "permission_denied"
    assert calls == 0


def test_recovery_requires_current_quarantine_id(tmp_path: Path) -> None:
    config = config_for(tmp_path)
    owner = HardwareCoordinator(config, "owner")
    lease = owner.acquire("physical:incident")
    lease.quarantine("test_incident")
    incident_a = owner.status()["quarantine_id"]
    owner.close()
    recovery = HardwareCoordinator(config, "recovery")

    wrong = recovery.recover(safe_state_confirmed=True, quarantine_id="wrong")
    assert wrong["error_type"] == "quarantine_changed"
    assert recovery.recover(safe_state_confirmed=True, quarantine_id=incident_a)["ok"] is True

    lease_b = recovery.acquire("physical:incident")
    lease_b.quarantine("new_incident")
    incident_b = recovery.status()["quarantine_id"]
    recovery.close()
    next_recovery = HardwareCoordinator(config, "next-recovery")
    assert incident_b != incident_a
    assert next_recovery.recover(safe_state_confirmed=True, quarantine_id=incident_a)["error_type"] == "quarantine_changed"
    assert next_recovery.recover(safe_state_confirmed=True, quarantine_id=incident_b)["ok"] is True


def quarantined_coordinator(tmp_path: Path) -> tuple[HardwareCoordinator, str, str]:
    config = config_for(tmp_path)
    owner = HardwareCoordinator(config, "owner")
    resource = "physical:incident"
    lease = owner.acquire(resource)
    lease.quarantine("test_incident")
    quarantine_id = str(owner.status()["quarantine_id"])
    owner.close()
    return HardwareCoordinator(config, "recovery"), resource, quarantine_id


def test_recovery_rejects_changed_resource_set(tmp_path: Path) -> None:
    recovery, resource, quarantine_id = quarantined_coordinator(tmp_path)
    marker = recovery._read_record(resource)
    assert marker is not None
    recovery._write_record(resource, {**marker, "resources": []})

    result = recovery.recover(safe_state_confirmed=True, quarantine_id=quarantine_id)

    assert result["error_type"] == "quarantine_changed"
    assert recovery.status()["blocked"] is True


def test_recovery_audit_failure_keeps_incident_quarantined(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    recovery, resource, quarantine_id = quarantined_coordinator(tmp_path)
    monkeypatch.setattr("agentic_hil.coordination.safe_append_text", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("audit denied")))

    result = recovery.recover(safe_state_confirmed=True, quarantine_id=quarantine_id)

    assert result["error_type"] == "recovery_audit_failed"
    assert result["audit_ok"] is False
    assert recovery._read_record(resource)["quarantine_id"] == quarantine_id
    assert recovery.status()["blocked"] is True


def test_only_one_parallel_recovery_can_release_incident(tmp_path: Path) -> None:
    first, _, quarantine_id = quarantined_coordinator(tmp_path)
    second = HardwareCoordinator(first.config, "second-recovery")
    barrier = threading.Barrier(2)
    results: list[dict] = []

    def recover(coordinator: HardwareCoordinator) -> None:
        barrier.wait()
        results.append(coordinator.recover(safe_state_confirmed=True, quarantine_id=quarantine_id))

    workers = [threading.Thread(target=recover, args=(coordinator,)) for coordinator in (first, second)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()

    assert sum(result.get("ok") is True for result in results) == 1
    assert {result.get("error_type") for result in results if not result.get("ok")} <= {"resource_busy", "resource_not_quarantined"}


@pytest.mark.parametrize("arguments", [["lease-status"], ["recover", "--confirm-safe-state", "--quarantine-id", "incident"]])
def test_coordination_cli_structures_corrupt_marker_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    arguments: list[str],
) -> None:
    config = config_for(tmp_path)
    coordinator = HardwareCoordinator(config, "setup")
    coordinator._record_path(coordinator.project_key).write_text('{"version": 999}\n', encoding="utf-8")
    monkeypatch.setattr("agentic_hil.cli.load_cli_authoritative_config", lambda path: config)

    exit_code = entrypoint(arguments)

    result = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert result["error_type"] == "coordination_state_invalid"
    probe = _LifetimeLock(coordinator.lock_directory / f"{resource_digest(coordinator.project_key)}.lock")
    probe.acquire()
    probe.release()


@pytest.mark.parametrize("unsafe", [{"cleanup_required": True}, {"quarantined": True}, {"cleanup_ok": False}, {"side_effect_status": "unknown"}, {"side_effect_status": "partial"}, {"lease_state": "cleanup_required"}, {"lease_state": "stale"}, {"hardware_state": "unknown"}])
def test_overall_success_rejects_unsafe_state(unsafe: dict) -> None:
    assert overall_success({"ok": True, **unsafe}) is False


def test_discovery_gate_conflicts_with_debug_effects(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = config_for(tmp_path)
    owner = HardwareCoordinator(config, "owner")
    gate = owner.acquire(DEBUGGER_DISCOVERY_RESOURCE)
    service = AgenticHILToolService(config)
    called = False

    def probe() -> dict:
        nonlocal called
        called = True
        return {"ok": True}

    monkeypatch.setattr(service.backend, "probe_target", probe)
    result = service.call("probe_target")
    assert result["error_type"] == "resource_busy"
    assert called is False
    gate.release()
    owner.close()
    service.close()


def test_cli_discovery_gate_blocks_before_backend(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = config_for(tmp_path)
    owner = HardwareCoordinator(config, "debug-owner")
    lease = owner.acquire(DEBUGGER_DISCOVERY_RESOURCE)
    called = False

    def list_probes() -> dict:
        nonlocal called
        called = True
        return {"ok": True, "probes": []}

    backend = SimpleNamespace(list_probes=list_probes, close=lambda: None)
    monkeypatch.setattr("agentic_hil.cli.load_authoritative_config", lambda workspace: config)
    monkeypatch.setattr("agentic_hil.tools.create_debugger_backend", lambda loaded: backend)
    try:
        result = debugger_probes()

        assert result["error_type"] == "resource_busy"
        assert called is False
    finally:
        lease.release()
        owner.close()


def test_discovery_stops_when_audit_is_unavailable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service = AgenticHILToolService(config_for(tmp_path))
    called = False

    def list_probes() -> dict:
        nonlocal called
        called = True
        return {"ok": True, "probes": []}

    monkeypatch.setattr(service.backend, "list_probes", list_probes)
    monkeypatch.setattr("agentic_hil.tools.ensure_audit_ready", lambda config: (_ for _ in ()).throw(OSError("audit denied")))
    try:
        result = service.call("debugger_probes_list")

        assert result["error_type"] == "audit_unavailable"
        assert result["side_effect_committed"] is False
        assert called is False
    finally:
        service.close()


@pytest.mark.parametrize("error", [RuntimeError("boom"), KeyboardInterrupt("stop"), SystemExit("exit")])
@pytest.mark.parametrize(
    ("service_name", "method", "tool", "arguments"),
    [
        ("com_ports", "write", "com_write", {"port_id": "dut", "text": "x"}),
        ("can_buses", "send", "can_send", {"bus_id": "bench", "frame_id": "0x123", "data_hex": "01"}),
        ("adapters", "set_value", "adapter_set_value", {"adapter_id": "fixture", "channel": "input", "value": 1.0}),
    ],
)
def test_unknown_hardware_exception_poisons_active_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error: BaseException,
    service_name: str,
    method: str,
    tool: str,
    arguments: dict,
) -> None:
    config = config_for(
        tmp_path,
        com_ports_yaml='com_ports:\n  dut:\n    device: "COM_TEST"\n',
        can_buses_yaml='can_buses:\n  bench:\n    adapter: "socketcan"\n    channel: "vcan0"\n',
        adapters_yaml=f'adapters:\n  fixture:\n    executable: "{Path(sys.executable).as_posix()}"\n    channels: ["input"]\n',
    )
    service = AgenticHILToolService(config)
    lease = service.coordinator.acquire("physical:exception")
    monkeypatch.setattr(getattr(service, service_name), method, lambda *args, **kwargs: (_ for _ in ()).throw(error))
    try:
        if isinstance(error, Exception):
            result = service.call(tool, arguments)
            assert result["error_type"] == "hardware_action_exception"
            assert result["quarantined"] is True
        else:
            with pytest.raises(type(error)):
                service.call(tool, arguments)
        assert lease.state == "cleanup_required"
        assert service.coordinator.blocked is True
        assert service.call(tool, arguments)["error_type"] == "resource_quarantined"
    finally:
        service.close()
    next_owner = HardwareCoordinator(config, "next-owner")
    with pytest.raises(CoordinationError) as excinfo:
        next_owner.acquire("physical:exception")
    assert excinfo.value.result["error_type"] == "resource_quarantined"
    quarantine_id = next_owner.status()["quarantine_id"]
    assert next_owner.recover(safe_state_confirmed=True, quarantine_id=quarantine_id)["ok"] is True


def test_foreign_project_marker_is_not_adopted_by_new_owner(tmp_path: Path) -> None:
    resource = "physical:shared-instrument"
    first = HardwareCoordinator(config_for(tmp_path / "first"), "first")
    first.acquire(resource).quarantine("first_incident")
    first_quarantine_id = str(first.status()["quarantine_id"])
    first.close()

    second = HardwareCoordinator(config_for(tmp_path / "second"), "second")
    with pytest.raises(CoordinationError) as excinfo:
        second.acquire(resource)
    assert excinfo.value.result["error_type"] == "resource_quarantined"
    assert excinfo.value.result["quarantine_id"] == first_quarantine_id
    marker = second._read_record(resource)
    assert marker["project_resource"] == first.project_key
    assert marker["quarantine_id"] == first_quarantine_id
    assert second.status()["blocked"] is False
    second.close()

    recovery = HardwareCoordinator(first.config, "first-recovery")
    assert recovery.recover(safe_state_confirmed=True, quarantine_id=first_quarantine_id)["ok"] is True
    recovery.close()
    third = HardwareCoordinator(config_for(tmp_path / "second"), "third")
    lease = third.acquire(resource)
    assert lease.release() is True
    third.close()


def test_hardware_exception_with_busy_project_lock_stays_fail_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = config_for(tmp_path, com_ports_yaml='com_ports:\n  dut:\n    device: "COM_TEST"\n')
    other = HardwareCoordinator(config, "other-owner")
    other.acquire("physical:other")
    service = AgenticHILToolService(config)
    monkeypatch.setattr(service.com_ports, "write", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("boom")))
    try:
        result = service.call("com_write", {"port_id": "dut", "text": "x"})

        assert result["error_type"] == "hardware_action_exception"
        assert result["quarantined"] is True
        assert "quarantine_error" in result
        assert service.coordinator.blocked is True
        assert service.call("com_write", {"port_id": "dut", "text": "x"})["error_type"] == "resource_quarantined"
    finally:
        service.close()
        other.close()


def failing_write_record(coordinator: HardwareCoordinator, should_fail):
    original = coordinator._write_record

    def wrapper(resource: str, record: dict) -> None:
        if should_fail(resource, record):
            raise OSError("injected write fault")
        original(resource, record)

    return wrapper


def restore_write_record(coordinator: HardwareCoordinator, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(coordinator, "_write_record", type(coordinator)._write_record.__get__(coordinator))


def test_release_resource_write_fault_blocks_and_stays_retryable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    coordinator = HardwareCoordinator(config_for(tmp_path), "owner")
    lease = coordinator.acquire("physical:release-fault")
    monkeypatch.setattr(coordinator, "_write_record", failing_write_record(coordinator, lambda resource, record: record.get("state") == "released"))

    assert lease.release() is False
    assert lease.state == "cleanup_required"
    assert lease.valid is True
    assert lease.lease_id in coordinator.leases
    assert coordinator.blocked is True
    assert all(lock.locked for lock in lease.locks)

    with pytest.raises(CoordinationError) as own_acquire:
        coordinator.acquire("physical:second-resource")
    assert own_acquire.value.result["error_type"] == "resource_quarantined"

    second_owner = HardwareCoordinator(coordinator.config, "second")
    with pytest.raises(CoordinationError) as foreign_acquire:
        second_owner.acquire("physical:release-fault")
    assert foreign_acquire.value.result["error_type"] == "resource_busy"

    restore_write_record(coordinator, monkeypatch)
    assert lease.release() is True
    assert lease.state == "released"
    assert coordinator.blocked is False
    assert coordinator._read_record("physical:release-fault")["state"] == "released"
    assert coordinator._read_record(coordinator.project_key)["state"] == "released"
    coordinator.close()
    second_owner.close()


def test_release_fault_with_total_write_failure_still_blocks_in_memory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    coordinator = HardwareCoordinator(config_for(tmp_path), "owner")
    lease = coordinator.acquire("physical:total-io-loss")
    monkeypatch.setattr(coordinator, "_write_record", failing_write_record(coordinator, lambda resource, record: True))

    assert lease.release() is False
    assert lease.state == "cleanup_required"
    assert coordinator.blocked is True
    assert lease.errors and all(error.get("reason") == "lease_release_unconfirmed" for error in lease.errors)

    with pytest.raises(CoordinationError):
        coordinator.acquire("physical:other")

    restore_write_record(coordinator, monkeypatch)
    assert lease.release() is True
    assert coordinator.blocked is False
    coordinator.close()


def test_release_project_write_fault_blocks_and_retries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    coordinator = HardwareCoordinator(config_for(tmp_path), "owner")
    lease = coordinator.acquire("physical:project-fault")
    monkeypatch.setattr(coordinator, "_write_record", failing_write_record(coordinator, lambda resource, record: resource == coordinator.project_key and record.get("state") == "released"))

    assert lease.release() is False
    assert lease.state == "cleanup_required"
    assert coordinator.blocked is True
    assert coordinator._read_record("physical:project-fault")["state"] == "cleanup_required"

    restore_write_record(coordinator, monkeypatch)
    assert lease.release() is True
    assert coordinator._read_record(coordinator.project_key)["state"] == "released"
    coordinator.close()


def test_release_lock_fault_blocks_and_retries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    coordinator = HardwareCoordinator(config_for(tmp_path), "owner")
    lease = coordinator.acquire("physical:lock-fault")
    lock = lease.locks[0]
    original_release = lock.release
    monkeypatch.setattr(lock, "release", lambda: (_ for _ in ()).throw(OSError("unlock failed")))

    assert lease.release() is False
    assert lease.state == "cleanup_required"
    assert coordinator.blocked is True
    assert lease.lease_id in coordinator.leases
    assert coordinator._read_record("physical:lock-fault")["state"] == "cleanup_required"

    monkeypatch.setattr(lock, "release", original_release)
    assert lease.release() is True
    assert lease.state == "released"
    assert coordinator._read_record("physical:lock-fault")["state"] == "released"
    coordinator.close()


def test_poison_quarantines_leases_in_provisional_release_states(tmp_path: Path) -> None:
    coordinator = HardwareCoordinator(config_for(tmp_path), "owner")
    releasing = coordinator.acquire("physical:releasing-state")
    released_in_memory = coordinator.acquire("physical:released-state")
    releasing.state = "releasing"
    released_in_memory.state = "released"

    coordinator.poison("unknown_hardware_exception")

    assert releasing.state == "cleanup_required"
    assert released_in_memory.state == "cleanup_required"
    assert coordinator.blocked is True
    for resource in ("physical:releasing-state", "physical:released-state"):
        assert coordinator._read_record(resource)["state"] == "cleanup_required"
    coordinator.close()


def test_recovery_converges_for_multi_lease_incident(tmp_path: Path) -> None:
    config = config_for(tmp_path)
    owner = HardwareCoordinator(config, "owner")
    lease_a = owner.acquire("physical:incident-a")
    lease_b = owner.acquire("physical:incident-b")
    lease_a.quarantine("first_failure")
    lease_b.quarantine("second_failure")
    quarantine_id = str(owner.status()["quarantine_id"])
    union = ["physical:incident-a", "physical:incident-b"]
    for resource in union:
        assert sorted(owner._read_record(resource)["resources"]) == union
    owner.close()

    recovery = HardwareCoordinator(config, "recovery")
    result = recovery.recover(safe_state_confirmed=True, quarantine_id=quarantine_id)

    assert result["ok"] is True
    assert sorted(result["resources"]) == union
    for resource in union:
        assert recovery._read_record(resource)["state"] == "released"
    assert recovery._read_record(recovery.project_key)["state"] == "released"


def test_recovery_resumes_idempotently_after_each_partial_persist_fault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = config_for(tmp_path)
    owner = HardwareCoordinator(config, "owner")
    lease = owner.acquire("physical:part-a", "physical:part-b")
    lease.quarantine("incident")
    quarantine_id = str(owner.status()["quarantine_id"])
    owner.close()

    recovery = HardwareCoordinator(config, "recovery")
    resources = sorted(["physical:part-a", "physical:part-b"])
    for fail_index in range(len(resources)):
        calls = {"count": -1}

        def fail_nth(resource: str, record: dict, fail_index: int = fail_index, calls: dict = calls) -> bool:
            if record.get("state") != "released":
                return False
            calls["count"] += 1
            return calls["count"] == fail_index

        monkeypatch.setattr(recovery, "_write_record", failing_write_record(recovery, fail_nth))
        result = recovery.recover(safe_state_confirmed=True, quarantine_id=quarantine_id)
        assert result["error_type"] == "recovery_persist_failed"
        assert result["retry_safe"] is True
        assert recovery._read_record(recovery.project_key)["state"] == "recovery_pending"
        restore_write_record(recovery, monkeypatch)

        blocked_probe = HardwareCoordinator(config, "probe")
        with pytest.raises(CoordinationError):
            blocked_probe.acquire("physical:part-a")
        blocked_probe.close()

    final = recovery.recover(safe_state_confirmed=True, quarantine_id=quarantine_id)
    assert final["ok"] is True
    for resource in resources:
        marker = recovery._read_record(resource)
        assert marker["state"] == "released"
        assert marker["recovered_quarantine_id"] == quarantine_id
    assert recovery._read_record(recovery.project_key)["state"] == "released"


def test_recovery_resume_flag_after_uninterrupted_partial_fault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = config_for(tmp_path)
    owner = HardwareCoordinator(config, "owner")
    lease = owner.acquire("physical:resume-a", "physical:resume-b")
    lease.quarantine("incident")
    quarantine_id = str(owner.status()["quarantine_id"])
    owner.close()

    recovery = HardwareCoordinator(config, "recovery")
    calls = {"count": -1}

    def fail_second(resource: str, record: dict) -> bool:
        if record.get("state") != "released":
            return False
        calls["count"] += 1
        return calls["count"] == 1

    monkeypatch.setattr(recovery, "_write_record", failing_write_record(recovery, fail_second))
    partial = recovery.recover(safe_state_confirmed=True, quarantine_id=quarantine_id)
    assert partial["error_type"] == "recovery_persist_failed"
    restore_write_record(recovery, monkeypatch)

    final = recovery.recover(safe_state_confirmed=True, quarantine_id=quarantine_id)
    assert final["ok"] is True
    assert final["resumed"] is True
    assert recovery._read_record(recovery.project_key)["state"] == "released"


def test_recovery_config_change_requires_explicit_audited_override(tmp_path: Path) -> None:
    config = config_for(tmp_path)
    owner = HardwareCoordinator(config, "owner")
    lease = owner.acquire("physical:config-change")
    lease.quarantine("incident")
    quarantine_id = str(owner.status()["quarantine_id"])
    recorded_sha = owner.config_sha256
    owner.close()

    config_file = Path(config.config_path)
    config_file.write_text(config_file.read_text(encoding="utf-8") + "\n# operator comment\n", encoding="utf-8")
    changed_config = load_config(str(config_file))
    recovery = HardwareCoordinator(changed_config, "recovery")

    refused = recovery.recover(safe_state_confirmed=True, quarantine_id=quarantine_id)
    assert refused["error_type"] == "config_changed"
    assert refused["recorded_config_sha256"] == recorded_sha
    assert refused["current_config_sha256"] == recovery.config_sha256
    assert recovery.status()["blocked"] is True

    accepted = recovery.recover(safe_state_confirmed=True, quarantine_id=quarantine_id, accept_config_change=True)
    assert accepted["ok"] is True
    assert accepted["config_change_accepted"] is True
    audit_lines = [json.loads(line) for line in (recovery.root / "recovery.jsonl").read_text(encoding="utf-8").splitlines()]
    assert audit_lines[-1]["config_change_accepted"] is True
    assert audit_lines[-1]["recorded_config_sha256"] == recorded_sha
    assert audit_lines[-1]["current_config_sha256"] == recovery.config_sha256


def test_arbitrary_quarantine_reason_cannot_be_caller_resolved(tmp_path: Path) -> None:
    coordinator = HardwareCoordinator(config_for(tmp_path), "owner")
    lease = coordinator.acquire("physical:arbitrary")
    lease.quarantine("arbitrary_unknown_effect")

    assert lease.resolve_retryable_cleanup("arbitrary_unknown_effect") is False
    assert lease.state == "cleanup_required"
    assert lease.release() is False
    assert coordinator.blocked is True
    assert coordinator.status()["blocked"] is True
    coordinator.close()


def test_status_race_does_not_quarantine_cleanly_released_owner(tmp_path: Path) -> None:
    config = config_for(tmp_path)
    owner = HardwareCoordinator(config, "owner")
    lease = owner.acquire("physical:status-race")
    observer = HardwareCoordinator(config, "observer")
    original_probe = observer._project_probe_lock

    class ReleaseBeforeAcquireLock:
        def __init__(self) -> None:
            self.inner = original_probe()

        def acquire(self) -> None:
            assert lease.release() is True
            owner.close()
            self.inner.acquire()

        def release(self) -> None:
            self.inner.release()

    observer._project_probe_lock = lambda: ReleaseBeforeAcquireLock()
    status = observer.status()

    assert status["blocked"] is False
    assert status["snapshot_atomic"] is True
    assert status["record"]["state"] == "released"
    assert observer._read_record(observer.project_key)["state"] == "released"
    observer.close()


def test_status_flags_non_atomic_snapshot_while_foreign_owner_holds_lock(tmp_path: Path) -> None:
    config = config_for(tmp_path)
    owner = HardwareCoordinator(config, "owner")
    lease = owner.acquire("physical:snapshot")
    observer = HardwareCoordinator(config, "observer")
    try:
        status = observer.status()
        assert status["owner_active"] is True
        assert status["snapshot_atomic"] is False
        assert status["blocked"] is False
    finally:
        lease.release()
        owner.close()
        observer.close()


@pytest.mark.parametrize(
    "payload",
    [b"{bad json", b"\xff\xfe\x00corrupt", b'{"version": 2, "state": 5}\n', b'{"version": 2, "state": "quarantined", "resources": 5}\n'],
)
def test_corrupt_coordination_records_are_structured_at_cli(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    payload: bytes,
) -> None:
    config = config_for(tmp_path)
    coordinator = HardwareCoordinator(config, "setup")
    coordinator._record_path(coordinator.project_key).write_bytes(payload)
    monkeypatch.setattr("agentic_hil.cli.load_cli_authoritative_config", lambda path: config)

    for arguments in (["lease-status"], ["recover", "--confirm-safe-state", "--quarantine-id", "incident"]):
        exit_code = entrypoint(arguments)
        captured = capsys.readouterr()
        result = json.loads(captured.out)
        assert exit_code == 1
        assert result["error_type"] == "coordination_state_invalid"
        assert "Traceback" not in captured.err


def test_recovery_releases_orphaned_active_resource_in_incident(tmp_path: Path) -> None:
    config = config_for(tmp_path)
    setup = HardwareCoordinator(config, "setup")
    quarantine_id = "incident-with-orphan-active"
    setup._write_record("physical:active-c", setup._base_record("active", ["physical:active-c"]))
    setup._write_record("physical:incident-a", {**setup._base_record("quarantined", ["physical:incident-a"]), "quarantine_id": quarantine_id, "reason": "boom"})
    setup._write_record(setup.project_key, {**setup._base_record("cleanup_required", ["physical:active-c", "physical:incident-a"]), "quarantine_id": quarantine_id})

    result = setup.recover(safe_state_confirmed=True, quarantine_id=quarantine_id)

    assert result["ok"] is True, result
    assert setup._read_record("physical:active-c")["state"] == "released"
    assert setup._read_record("physical:incident-a")["state"] == "released"
    assert setup._read_record(setup.project_key)["state"] == "released"


def test_retryable_release_success_does_not_poison_other_incident(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = config_for(tmp_path)
    owner = HardwareCoordinator(config, "owner")
    lease_a = owner.acquire("physical:a")
    lease_b = owner.acquire("physical:b")
    lease_b.quarantine("b_failure")
    quarantine_id = str(owner.status()["quarantine_id"])
    faults = {"remaining": 1}

    def one_shot(resource: str, record: dict) -> bool:
        if record.get("state") == "released" and resource == "physical:a" and faults["remaining"] > 0:
            faults["remaining"] -= 1
            return True
        return False

    monkeypatch.setattr(owner, "_write_record", failing_write_record(owner, one_shot))
    assert lease_a.release() is False
    assert lease_a.state == "cleanup_required"
    assert lease_a.release() is True

    assert "physical:a" not in owner.incident_resources
    assert owner._read_record("physical:a")["state"] == "released"
    project = owner._read_record(owner.project_key)
    assert project["state"] == "cleanup_required"
    assert "physical:a" not in project["resources"]
    assert "physical:b" in project["resources"]
    assert owner.blocked is True
    owner.close()

    recovery = HardwareCoordinator(config, "recovery")
    result = recovery.recover(safe_state_confirmed=True, quarantine_id=quarantine_id)
    assert result["ok"] is True, result
    assert sorted(result["resources"]) == ["physical:b"]


def test_adopted_lease_less_incident_survives_unrelated_release(tmp_path: Path) -> None:
    config = config_for(tmp_path)
    owner = HardwareCoordinator(config, "owner")
    lease_b = owner.acquire("physical:healthy-b")
    owner._write_record("physical:orphan-x", {**owner._base_record("quarantined", ["physical:orphan-x"]), "quarantine_id": "prior-incident", "reason": "prior_dead_owner"})

    with pytest.raises(CoordinationError):
        owner.acquire("physical:orphan-x")
    assert owner.blocked is True
    assert owner.status()["blocked"] is True

    # Releasing the unrelated healthy lease must not erase the adopted incident.
    assert lease_b.release() is True
    assert owner.blocked is True
    status = owner.status()
    assert status["blocked"] is True
    assert status["quarantine_id"] is not None
    assert owner._read_record(owner.project_key)["state"] == "cleanup_required"
    assert "physical:orphan-x" in owner._read_record(owner.project_key)["resources"]
    owner.close()


def test_redact_sensitive_strips_secret_named_keys() -> None:
    from agentic_hil.redact import redact_sensitive

    redacted = redact_sensitive(
        {
            "api_token": "abc",
            "device_secret": "xyz",
            "password": "p",
            "quarantine_id": "keep",
            "owner_marker": "keep",
            "nested": {"session_token": "t", "state": "active"},
            "leases": [{"auth_token": "l", "lease_state": "active"}],
        }
    )
    assert redacted["api_token"] == "[redacted]"
    assert redacted["device_secret"] == "[redacted]"
    assert redacted["password"] == "[redacted]"
    assert redacted["nested"]["session_token"] == "[redacted]"
    assert redacted["leases"][0]["auth_token"] == "[redacted]"
    # Non-secret-named keys, including the ownership marker, are preserved.
    assert redacted["quarantine_id"] == "keep"
    assert redacted["owner_marker"] == "keep"
    assert redacted["nested"]["state"] == "active"
    assert redacted["leases"][0]["lease_state"] == "active"


def test_lease_status_output_carries_no_secret_named_field(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    config = config_for(tmp_path)
    owner = HardwareCoordinator(config, "owner")
    lease = owner.acquire("physical:secret-leak")
    try:
        monkeypatch.setattr("agentic_hil.cli.load_cli_authoritative_config", lambda path: config)
        entrypoint(["lease-status"])
        printed = capsys.readouterr().out
        # No secret-named key ("*_token"/"*secret"/"password") appears in the
        # operator-facing output; the ownership marker is renamed to a
        # non-secret field and is not a credential.
        assert not re.search(r'"[A-Za-z0-9_]*(?:token|secret|password)"\s*:', printed, re.IGNORECASE)
    finally:
        lease.release()
        owner.close()


def test_release_fault_reraises_keyboard_interrupt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    owner = HardwareCoordinator(config_for(tmp_path), "owner")
    lease = owner.acquire("physical:interrupt")

    def interrupting_write(resource: str, record: dict) -> None:
        if record.get("state") == "released":
            raise KeyboardInterrupt("operator interrupt during release persist")

    monkeypatch.setattr(owner, "_write_record", interrupting_write)
    with pytest.raises(KeyboardInterrupt):
        lease.release()

    # Fail-closed despite the interrupt: lease quarantined and still blocking.
    assert owner.blocked is True
    assert lease.state == "cleanup_required"
    monkeypatch.setattr(owner, "_write_record", type(owner)._write_record.__get__(owner))
    owner.close()
