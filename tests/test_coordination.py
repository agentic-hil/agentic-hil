from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from conftest import write_config

from agentic_hil.adapters import AdapterService
from agentic_hil.bridge import BRIDGE_PROTOCOL_VERSION, BridgeCleanupError, ProcessBridgeSession
from agentic_hil.can import CanBusService, normalize_received_frames
from agentic_hil.config import ConfigError, load_config
from agentic_hil.coordination import CoordinationError, HardwareCoordinator, _LifetimeLock
from agentic_hil.mcp import call_tool
from agentic_hil.process import (
    cleanup_registered_processes,
    managed_process_owner,
    process_group_kwargs,
    spawn_managed_process,
    terminate_process_tree,
)
from agentic_hil.report import read_last_report, report_state_path, write_report
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
    assert coordinator.recover(safe_state_confirmed=True)["ok"] is True


def test_process_cleanup_is_scoped_to_service_owner() -> None:
    with managed_process_owner("first-owner"):
        first = spawn_managed_process([sys.executable, "-c", "import time; time.sleep(60)"], **process_group_kwargs())
    with managed_process_owner("second-owner"):
        second = spawn_managed_process([sys.executable, "-c", "import time; time.sleep(60)"], **process_group_kwargs())
    try:
        assert cleanup_registered_processes(owner_token="first-owner") == []
        assert first.poll() is not None
        assert second.poll() is None
    finally:
        if first.poll() is None:
            terminate_process_tree(first, 5)
        if second.poll() is None:
            terminate_process_tree(second, 5)


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
    recovered = coordinator.recover(safe_state_confirmed=True)
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


def test_mcp_marks_audit_and_target_failures_as_errors() -> None:
    class Tools:
        def __init__(self, result: dict):
            self.result = result

        def call(self, name: str, arguments: dict) -> dict:
            return self.result

    for result in ({"ok": True, "audit_ok": False}, {"ok": True, "target_ok": False}):
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


def test_unknown_can_effect_poisons_session_until_cleanup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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
        assert service.session_stop("bench")["ok"] is True
    finally:
        service.close()


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
        service.close()


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
        service.close()
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
