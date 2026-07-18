from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from io import BytesIO, StringIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import write_config

from agentic_hil.adapters import AdapterService, AdapterSession
from agentic_hil.bridge import BridgeCloseResult, ProcessBridgeSession
from agentic_hil.can import CanBusService, CanBusSession, open_python_can_adapter, parse_can_id, payload_frame
from agentic_hil.cli import hardware_recover, hardware_status
from agentic_hil.comports import ComPortService, ComPortSession
from agentic_hil.comstdio import run_com_stdio
from agentic_hil.config import load_config
from agentic_hil.hardware_lock import (
    HardwareLockError,
    HardwareQuarantinedError,
    ProjectHardwareLock,
    hardware_state_directory,
)
from agentic_hil.mcp import handle_mcp_message
from agentic_hil.stdio import run_stdio_server
from agentic_hil.tools import AgenticHILToolService, HardwareCleanupError
from agentic_hil.types import AdapterConfig, CanBusConfig

WAIT_TIMEOUT_S = 5.0
POLL_INTERVAL_S = 0.01

COM_PORT_YAML = 'com_ports:\n  dut:\n    device: "/dev/ttyAGENTIC_HILTEST"\n'
CAN_BUS_YAML = 'can_buses:\n  bench:\n    adapter: "process"\n    channel: "vcan0"\n    executable: "python"\n'
ADAPTER_YAML = 'adapters:\n  ntc:\n    executable: "python"\n    channels: ["temp"]\n    faults: ["open"]\n'


def load_test_config(tmp_path: Path, **kwargs):
    return load_config(str(write_config(tmp_path, **kwargs)), str(tmp_path))


def wait_until(predicate, timeout_s: float = WAIT_TIMEOUT_S) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(POLL_INTERVAL_S)
    return predicate()


def test_stdio_rejects_oversized_message_and_keeps_serving(tmp_path: Path) -> None:
    config = load_test_config(tmp_path)
    oversized = '{"jsonrpc": "2.0", "id": 1, "method": "ping", "pad": "' + "x" * 5000 + '"}'
    ping = '{"jsonrpc": "2.0", "id": 2, "method": "ping"}'
    output = StringIO()

    exit_code = run_stdio_server(
        config,
        input_stream=StringIO(oversized + "\n" + ping + "\n"),
        output_stream=output,
        max_message_chars=1000,
    )

    assert exit_code == 0
    responses = [json.loads(line) for line in output.getvalue().splitlines()]
    assert len(responses) == 2
    assert responses[0]["error"]["code"] == -32600
    assert responses[1]["id"] == 2
    assert "result" in responses[1]


def test_empty_jsonrpc_batch_returns_invalid_request(tmp_path: Path) -> None:
    service = AgenticHILToolService(load_test_config(tmp_path))

    response = handle_mcp_message([], service)

    assert isinstance(response, dict)
    assert response["error"]["code"] == -32600


class FailingSerialHandle:
    """Serial handle whose reads fail like an unplugged device; is_open stays True (pyserial behavior)."""

    is_open = True
    in_waiting = 0

    def read(self, size: int) -> bytes:
        raise OSError("device disconnected")

    def close(self) -> None:
        pass


class IdleSerialHandle:
    def __init__(self) -> None:
        self.is_open = True
        self.in_waiting = 0

    def read(self, size: int) -> bytes:
        time.sleep(POLL_INTERVAL_S)
        return b""

    def close(self) -> None:
        self.is_open = False


def test_com_session_with_dead_reader_reports_not_active(tmp_path: Path) -> None:
    config = load_test_config(tmp_path, com_ports_yaml=COM_PORT_YAML)
    service = ComPortService(config)
    log_path = tmp_path / ".agentic-hil" / "logs" / "test-com.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    session = ComPortSession("dut", config.com_ports["dut"], FailingSerialHandle(), str(log_path))
    service.sessions["dut"] = session

    assert wait_until(lambda: session.reader_error is not None), "reader thread never recorded its error"

    result = service.read_bytes("dut", 16, 0.0)
    assert result["ok"] is False
    assert result["error_type"] == "session_not_active"
    assert result["reader_error"]["error_type"] == "serial_read_failed"
    assert service._session_is_active(session) is False


def test_com_session_start_report_failure_rolls_back_resource_and_lease(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = load_test_config(tmp_path, com_ports_yaml=COM_PORT_YAML)
    service = AgenticHILToolService(config)
    serial_handle = IdleSerialHandle()
    log_path = tmp_path / ".agentic-hil" / "logs" / "start-failure.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    session = ComPortSession("dut", config.com_ports["dut"], serial_handle, str(log_path))
    monkeypatch.setattr(service.com_ports, "_open_serial", lambda *_: {"ok": True, "session": session})
    monkeypatch.setattr("agentic_hil.comports.append_jsonl", lambda *_: (_ for _ in ()).throw(OSError("log full")))

    with pytest.raises(OSError, match="log full"):
        service.call("com_session_start", {"port_id": "dut"})

    assert "dut" not in service.com_ports.sessions
    assert serial_handle.is_open is False
    second_lock = ProjectHardwareLock(config.config_path)
    assert second_lock.acquire() is True
    second_lock.release()
    service.close()


class BlockingStdin:
    """Stdin stub that blocks like a cooked-mode TTY until released, then reports EOF."""

    def __init__(self) -> None:
        self.release = threading.Event()

    def read1(self, size: int) -> bytes:
        self.release.wait()
        return b""


class StubComPortService:
    """ComPortService substitute: one banner chunk is available immediately, then silence."""

    def __init__(self, config) -> None:
        self.banner_sent = False

    def session_start(self, port_id: str, clear_buffer: bool) -> dict:
        return {"ok": True}

    def write_bytes(self, port_id: str, data: bytes, tool: str) -> dict:
        return {"ok": True, "bytes_written": len(data)}

    def read_bytes(self, port_id: str, max_bytes: int, wait_timeout_s: float, tool: str) -> dict:
        if not self.banner_sent:
            self.banner_sent = True
            return {"ok": True, "bytes_read": 6, "data": {"text": "banner"}}
        return {"ok": True, "bytes_read": 0, "data": {"text": ""}}

    def session_stop(self, port_id: str) -> dict:
        return {"ok": True}

    def close(self) -> None:
        pass

    def has_active_sessions(self) -> bool:
        return False

    def active_session_ids(self) -> list[str]:
        return []


class UncloseableComPortService(StubComPortService):
    def close(self) -> None:
        raise OSError("device did not close")

    def active_session_ids(self) -> list[str]:
        return ["dut"]


def test_com_stdio_relays_device_output_while_stdin_is_blocked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = load_test_config(tmp_path, com_ports_yaml=COM_PORT_YAML)
    monkeypatch.setattr("agentic_hil.comstdio.ComPortService", StubComPortService)
    stdin = BlockingStdin()
    output = StringIO()
    worker = threading.Thread(
        target=run_com_stdio,
        kwargs={"config": config, "port_id": "dut", "input_stream": stdin, "output_stream": output, "error_stream": StringIO()},
        daemon=True,
    )

    worker.start()
    relayed = wait_until(lambda: "banner" in output.getvalue(), timeout_s=2.0)
    stdin.release.set()
    worker.join(timeout=WAIT_TIMEOUT_S)

    assert relayed, "device output was not relayed while stdin was still blocked"
    assert not worker.is_alive(), "com-stdio loop did not exit after stdin EOF"


def test_com_stdio_fails_closed_while_project_hardware_is_busy(tmp_path: Path) -> None:
    config = load_test_config(tmp_path, com_ports_yaml=COM_PORT_YAML)
    held_lock = ProjectHardwareLock(config.config_path)
    assert held_lock.acquire() is True
    errors = StringIO()
    try:
        exit_code = run_com_stdio(config, "dut", input_stream=BlockingStdin(), output_stream=StringIO(), error_stream=errors)
    finally:
        held_lock.release()

    result = json.loads(errors.getvalue())
    assert exit_code == 1
    assert result["error_type"] == "hardware_busy"


def test_com_stdio_quarantines_project_when_cleanup_leaves_active_port(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = load_test_config(tmp_path, com_ports_yaml=COM_PORT_YAML)
    monkeypatch.setattr("agentic_hil.comstdio.ComPortService", UncloseableComPortService)
    errors = StringIO()

    exit_code = run_com_stdio(config, "dut", input_stream=BytesIO(b""), output_stream=StringIO(), error_stream=errors, eof_idle_timeout_s=0.0)

    lines = errors.getvalue().splitlines()
    assert exit_code == 1
    assert len(lines) == 1
    result = json.loads(lines[0])
    assert result["error_type"] == "hardware_cleanup_failed"
    assert result["hardware_state_unconfirmed"] is True
    assert result["quarantine"]["source"] == "com_stdio"
    with pytest.raises(HardwareQuarantinedError):
        ProjectHardwareLock(config.config_path).acquire()


def test_quarantine_requires_owned_lock(tmp_path: Path) -> None:
    config = load_test_config(tmp_path)
    lock = ProjectHardwareLock(config.config_path)

    with pytest.raises(HardwareLockError):
        lock.mark_quarantined(reason="hardware_cleanup_failed", source="test", active_resources=[], inspection_errors=[])


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_hardware_state_files_use_private_permissions(tmp_path: Path) -> None:
    config = load_test_config(tmp_path)
    lock = ProjectHardwareLock(config.config_path)
    assert lock.acquire(source="test") is True
    try:
        assert lock.state_dir.stat().st_mode & 0o777 == 0o700
        assert lock.path.stat().st_mode & 0o777 == 0o600
        assert lock.state_path.stat().st_mode & 0o777 == 0o600
    finally:
        lock.release()


def test_relative_state_home_is_ignored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    variable = "LOCALAPPDATA" if os.name == "nt" else "XDG_STATE_HOME"
    monkeypatch.setenv(variable, "relative-state")
    monkeypatch.chdir(tmp_path)

    assert hardware_state_directory().is_absolute()
    assert not hardware_state_directory().is_relative_to(tmp_path)


def test_relative_state_home_and_fallback_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    variable = "LOCALAPPDATA" if os.name == "nt" else "XDG_STATE_HOME"
    monkeypatch.setenv(variable, "relative-state")
    monkeypatch.setattr(Path, "home", lambda: Path("relative-home"))

    with pytest.raises(HardwareLockError, match="not absolute"):
        hardware_state_directory()


def test_invalid_state_markers_get_content_bound_recovery_ids(tmp_path: Path) -> None:
    config = load_test_config(tmp_path)
    lock = ProjectHardwareLock(config.config_path)
    lock._ensure_state_directory()
    lock.state_path.write_text("{}", encoding="utf-8")
    first = lock.status()["state"]
    lock.state_path.write_text("[]", encoding="utf-8")
    second = lock.status()["state"]

    assert first["state"] == "quarantined"
    assert first["quarantine_id"] != second["quarantine_id"]
    stale = hardware_recover(config.config_path, acknowledge_hardware_checked=True, quarantine_id=str(first["quarantine_id"]))
    assert stale["error_type"] == "quarantine_changed"


def test_state_directory_fsync_failure_leaves_fail_closed_marker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = load_test_config(tmp_path)
    lock = ProjectHardwareLock(config.config_path)
    lock._ensure_state_directory()
    lock.path.touch()

    with monkeypatch.context() as patch:
        patch.setattr(lock, "_ensure_state_directory", lambda: None)
        patch.setattr("agentic_hil.hardware_lock.fsync_directory", lambda *_: (_ for _ in ()).throw(OSError("fsync failed")))
        with pytest.raises(HardwareLockError, match="fsync failed"):
            lock.acquire(source="test")

    assert lock.handle is None
    with pytest.raises(HardwareQuarantinedError):
        ProjectHardwareLock(config.config_path).acquire(source="test")


def test_acquire_rechecks_state_after_os_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = load_test_config(tmp_path)
    lock = ProjectHardwareLock(config.config_path)
    original_acquire = lock._acquire_os_lock

    def acquire_then_publish_state() -> bool:
        acquired = original_acquire()
        if acquired:
            lock.lease_id = "race"
            marker = lock._new_active_state("race")
            marker["state"] = "quarantined"
            marker["reason"] = "hardware_cleanup_failed"
            lock._write_state(marker)
        return acquired

    monkeypatch.setattr(lock, "_acquire_os_lock", acquire_then_publish_state)

    with pytest.raises(HardwareQuarantinedError) as excinfo:
        lock.acquire(source="test")

    assert excinfo.value.details["state"] == "quarantined"
    recovery = ProjectHardwareLock(config.config_path)
    assert recovery.acquire(recovery=True, source="test") is True
    recovery.clear_quarantine()
    recovery.release_os_lock()


def test_each_acquire_gets_fresh_lease_id(tmp_path: Path) -> None:
    config = load_test_config(tmp_path)
    lock = ProjectHardwareLock(config.config_path)

    assert lock.acquire(source="first") is True
    first = lock.status()["state"]["lease_id"]
    lock.confirm_safe_and_release()
    assert lock.acquire(source="second") is True
    second = lock.status()["state"]["lease_id"]
    lock.confirm_safe_and_release()

    assert first != second


def test_old_recovery_id_cannot_clear_newer_incident_from_same_lock(tmp_path: Path) -> None:
    config = load_test_config(tmp_path)
    lock = ProjectHardwareLock(config.config_path)
    assert lock.acquire(source="first") is True
    first = lock.quarantine_and_release(reason="hardware_cleanup_failed", source="test", active_resources=[], inspection_errors=[])
    assert hardware_recover(config.config_path, acknowledge_hardware_checked=True, quarantine_id=str(first["quarantine_id"]))["ok"] is True

    assert lock.acquire(source="second") is True
    second = lock.quarantine_and_release(reason="hardware_cleanup_failed", source="test", active_resources=[], inspection_errors=[])

    assert second["quarantine_id"] != first["quarantine_id"]
    stale = hardware_recover(config.config_path, acknowledge_hardware_checked=True, quarantine_id=str(first["quarantine_id"]))
    assert stale["error_type"] == "quarantine_changed"


def test_active_marker_blocks_after_owner_exits(tmp_path: Path) -> None:
    config_path = write_config(tmp_path)
    script = """
import sys
from agentic_hil.hardware_lock import ProjectHardwareLock
lock = ProjectHardwareLock(sys.argv[1])
if not lock.acquire(source='crash_test'):
    raise SystemExit(2)
"""
    completed = subprocess.run([sys.executable, "-c", script, str(config_path)], capture_output=True, text=True, check=False)
    assert completed.returncode == 0, completed.stderr

    with pytest.raises(HardwareQuarantinedError):
        ProjectHardwareLock(str(config_path)).acquire()

    recovery = ProjectHardwareLock(str(config_path))
    assert recovery.acquire(recovery=True, source="test") is True
    recovery.clear_quarantine()
    recovery.release_os_lock()


def test_hardware_status_reports_foreign_lock_busy(tmp_path: Path) -> None:
    config_path = write_config(tmp_path)
    script = """
import sys
import time
from agentic_hil.hardware_lock import ProjectHardwareLock
lock = ProjectHardwareLock(sys.argv[1])
if not lock.acquire(source='test'):
    raise SystemExit(2)
print('ready', flush=True)
time.sleep(30)
"""
    child = subprocess.Popen([sys.executable, "-c", script, str(config_path)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "ready"
        status = ProjectHardwareLock(str(config_path)).status()
        assert status["busy"] is True
        assert status["state"]["state"] == "active"
        recovery = hardware_recover(str(config_path), acknowledge_hardware_checked=True, quarantine_id=str(status["state"]["lease_id"]))
        assert recovery["error_type"] == "hardware_busy"
    finally:
        child.terminate()
        child.wait(timeout=WAIT_TIMEOUT_S)
        recovery = ProjectHardwareLock(str(config_path))
        if recovery.acquire(recovery=True, source="test"):
            recovery.clear_quarantine()
            recovery.release_os_lock()


def test_hardware_quarantine_is_project_scoped(tmp_path: Path) -> None:
    config_a = load_test_config(tmp_path / "a")
    config_b = load_test_config(tmp_path / "b")
    lock_a = ProjectHardwareLock(config_a.config_path)
    assert lock_a.acquire(source="test") is True
    lock_a.quarantine_and_release(reason="hardware_cleanup_failed", source="test", active_resources=[{"type": "debugger", "id": "default"}], inspection_errors=[])

    with pytest.raises(HardwareQuarantinedError):
        ProjectHardwareLock(config_a.config_path).acquire()
    lock_b = ProjectHardwareLock(config_b.config_path)
    assert lock_b.acquire() is True
    lock_b.release()


class ExplodingBridge:
    def __init__(self) -> None:
        self.close_attempted = False

    def status(self) -> dict:
        return {"active": True}

    def close(self) -> None:
        self.close_attempted = True
        raise RuntimeError("bridge already gone")


class UnsafeClosingBridge:
    def __init__(self) -> None:
        self.closed = False
        self.last_close_result: BridgeCloseResult | None = None

    def status(self) -> dict:
        return {"active": not self.closed}

    def close(self) -> BridgeCloseResult:
        self.closed = True
        self.last_close_result = BridgeCloseResult(process_reaped=True, safe_state_confirmed=False, close_response={"ok": False}, errors=["relay reset failed"])
        return self.last_close_result


def test_adapter_open_failure_retains_unconfirmed_cleanup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = load_test_config(tmp_path, adapters_yaml=ADAPTER_YAML)
    bridge = UnsafeClosingBridge()
    monkeypatch.setattr(
        "agentic_hil.adapters.open_adapter_bridge",
        lambda *_: {"ok": False, "error_type": "adapter_open_failed", "session": bridge, "cleanup_unconfirmed": True},
    )
    service = AdapterService(config)

    result = service.session_start("ntc")

    assert result["ok"] is False
    assert service.active_session_ids() == ["ntc"]
    assert service.cleanup_inspection_errors() == [{"id": "ntc", "error": "Physical safe state was not confirmed during adapter cleanup."}]


def test_interrupted_adapter_open_quarantines_provisional_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = load_test_config(tmp_path, adapters_yaml=ADAPTER_YAML)
    bridge = UnsafeClosingBridge()

    def interrupted_open(*args, **kwargs):
        on_started = args[3]
        on_started(bridge)
        bridge.close()
        raise KeyboardInterrupt

    monkeypatch.setattr("agentic_hil.adapters.open_adapter_bridge", interrupted_open)
    service = AgenticHILToolService(config, reload_config=False)

    with pytest.raises(KeyboardInterrupt):
        service.call("adapter_session_start", {"adapter_id": "ntc"})

    with pytest.raises(HardwareQuarantinedError):
        ProjectHardwareLock(config.config_path).acquire()


def test_adapter_service_close_stops_all_sessions_despite_bridge_failure(tmp_path: Path) -> None:
    config = load_test_config(tmp_path, adapters_yaml=ADAPTER_YAML)
    service = AdapterService(config)
    adapter_config = AdapterConfig(executable="python", args=[], timeout_s=1.0, channels=["temp"], faults=["open"])
    log_dir = tmp_path / ".agentic-hil" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    first = AdapterSession("a1", adapter_config, ExplodingBridge(), str(log_dir / "a1.jsonl"))
    second = AdapterSession("a2", adapter_config, ExplodingBridge(), str(log_dir / "a2.jsonl"))
    service.sessions.update({"a1": first, "a2": second})

    with pytest.raises(RuntimeError, match="bridge already gone"):
        service.close()

    assert first.bridge.close_attempted and second.bridge.close_attempted
    assert first.active is False and second.active is False
    assert service.has_active_sessions() is True


def test_tool_service_quarantines_unconfirmed_bridge_close(tmp_path: Path) -> None:
    config = load_test_config(tmp_path, adapters_yaml=ADAPTER_YAML)
    adapters = AdapterService(config)
    adapter_config = AdapterConfig(executable="python", args=[], timeout_s=1.0, channels=["temp"], faults=["open"])
    log_dir = tmp_path / ".agentic-hil" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    adapters.sessions["ntc"] = AdapterSession("ntc", adapter_config, UnsafeClosingBridge(), str(log_dir / "ntc.jsonl"))
    service = AgenticHILToolService(config, adapters=adapters, reload_config=False)

    with pytest.raises(HardwareCleanupError):
        service.close()

    with pytest.raises(HardwareQuarantinedError):
        ProjectHardwareLock(config.config_path).acquire()


def test_unconfirmed_bridge_stop_blocks_followup_hardware_action(tmp_path: Path) -> None:
    config = load_test_config(tmp_path, adapters_yaml=ADAPTER_YAML)
    adapters = AdapterService(config)
    adapter_config = AdapterConfig(executable="python", args=[], timeout_s=1.0, channels=["temp"], faults=["open"])
    log_dir = tmp_path / ".agentic-hil" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    adapters.sessions["ntc"] = AdapterSession("ntc", adapter_config, UnsafeClosingBridge(), str(log_dir / "ntc.jsonl"))
    service = AgenticHILToolService(config, adapters=adapters, reload_config=False)

    with pytest.raises(RuntimeError, match="physical safe state"):
        service.call("adapter_session_stop", {"adapter_id": "ntc"})

    blocked = service.call("probe_target")
    assert blocked["error_type"] == "hardware_state_unconfirmed"


class FailingCleanupSubsystem:
    def __init__(self, active: bool = False, error: str | None = None) -> None:
        self.active = active
        self.error = error
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1
        if self.error is not None:
            raise RuntimeError(self.error)
        self.active = False

    def has_active_sessions(self) -> bool:
        return self.active

    def active_session_ids(self) -> list[str]:
        return ["fake"] if self.active else []


class FailingCleanupBackend(FailingCleanupSubsystem):
    def has_active_session(self) -> bool:
        return self.active


class InspectionOnlySubsystem(FailingCleanupSubsystem):
    def cleanup_inspection_errors(self) -> list[dict]:
        return [{"id": "stale", "error": "cleanup unconfirmed"}]


class OperationBackend(FailingCleanupBackend):
    def __init__(self, *, reset_error: BaseException | None = None, reset_result: dict | None = None, flash_error: BaseException | None = None) -> None:
        super().__init__()
        self.reset_error = reset_error
        self.reset_result = reset_result or {"ok": True}
        self.flash_error = flash_error
        self.probe_calls = 0

    def reset_target(self, mode: str) -> dict:
        if self.reset_error is not None:
            raise self.reset_error
        return self.reset_result

    def probe_target(self) -> dict:
        self.probe_calls += 1
        return {"ok": True}

    def flash_firmware(self, artifact: dict, reset_after_flash: bool) -> dict:
        if self.flash_error is not None:
            raise self.flash_error
        return {"ok": True}


class DeadBridge:
    last_close_result = None

    def status(self) -> dict:
        return {"active": False}

    def close(self):
        return BridgeCloseResult(process_reaped=True, safe_state_confirmed=False, close_response={"ok": False}, errors=["bridge crashed"])


class DeadCanAdapter(DeadBridge):
    adapter_name = "process"


class InterruptAfterCloseBridge:
    def __init__(self, safe: bool) -> None:
        self.safe = safe
        self.closed = False
        self.last_close_result: BridgeCloseResult | None = None

    def status(self) -> dict:
        return {"active": not self.closed}

    def close(self):
        self.closed = True
        self.last_close_result = BridgeCloseResult(process_reaped=True, safe_state_confirmed=self.safe, close_response={"ok": self.safe, "safe_state_confirmed": self.safe}, errors=[] if self.safe else ["unsafe"])
        raise KeyboardInterrupt


class CleanupInterruptBackend(FailingCleanupBackend):
    def close(self) -> None:
        self.close_calls += 1
        raise KeyboardInterrupt


class ConcurrentResetBackend(OperationBackend):
    def __init__(self) -> None:
        super().__init__()
        self.active_calls = 0
        self.max_active_calls = 0
        self.guard = threading.Lock()

    def reset_target(self, mode: str) -> dict:
        with self.guard:
            self.active_calls += 1
            self.max_active_calls = max(self.max_active_calls, self.active_calls)
        time.sleep(0.05)
        with self.guard:
            self.active_calls -= 1
        return {"ok": True}


class BlockingResetBackend(OperationBackend):
    def __init__(self) -> None:
        super().__init__()
        self.started = threading.Event()
        self.release = threading.Event()

    def reset_target(self, mode: str) -> dict:
        self.started.set()
        assert self.release.wait(WAIT_TIMEOUT_S)
        return {"ok": True}


class InterruptingDebugBackend(OperationBackend):
    def debug_start_session(self, artifact: dict, mode: str, timeout_s: float | None) -> dict:
        self.active = True
        raise KeyboardInterrupt


class MalformedResultBackend(OperationBackend):
    def reset_target(self, mode: str):
        return None


class MalformedCloseCanAdapter:
    adapter_name = "process"

    def __init__(self) -> None:
        self.active = True

    def status(self) -> dict:
        return {"active": self.active}

    def close(self):
        self.active = False
        return None


class SafeClosingBridge:
    adapter_name = "process"

    def __init__(self) -> None:
        self.active = True
        self.last_close_result: BridgeCloseResult | None = None

    def status(self) -> dict:
        return {"active": self.active}

    def close(self) -> BridgeCloseResult:
        self.active = False
        self.last_close_result = BridgeCloseResult(process_reaped=True, safe_state_confirmed=True, close_response={"ok": True, "safe_state_confirmed": True}, errors=[])
        return self.last_close_result


class FakeChild:
    def __init__(self) -> None:
        self.active = True
        self.terminated = False

    def poll(self):
        return None if self.active else 0

    def terminate(self) -> None:
        self.terminated = True
        self.active = False

    def kill(self) -> None:
        self.active = False

    def wait(self, timeout: float | None = None) -> int:
        return 0


class FakeCanBus:
    def __init__(self) -> None:
        self.active = True

    def shutdown(self) -> None:
        self.active = False


def test_tool_service_close_attempts_every_subsystem_and_retains_unsafe_lease(tmp_path: Path) -> None:
    config = load_test_config(tmp_path)
    backend = FailingCleanupBackend(error="debug cleanup failed")
    com_ports = FailingCleanupSubsystem()
    can_buses = FailingCleanupSubsystem(active=True, error="CAN cleanup failed")
    adapters = FailingCleanupSubsystem()
    service = AgenticHILToolService(
        config,
        backend=backend,
        com_ports=com_ports,
        can_buses=can_buses,
        adapters=adapters,
        reload_config=False,
    )

    try:
        with pytest.raises(HardwareCleanupError) as excinfo:
            service.close()

        assert [name for name, _ in excinfo.value.errors] == ["debugger", "can"]
        assert backend.close_calls == com_ports.close_calls == can_buses.close_calls == adapters.close_calls == 1
        second_lock = ProjectHardwareLock(config.config_path)
        with pytest.raises(HardwareQuarantinedError):
            second_lock.acquire()
    finally:
        service._hardware_lock.release()


def test_interrupted_one_shot_operation_without_session_stays_quarantined(tmp_path: Path) -> None:
    config = load_test_config(tmp_path)
    service = AgenticHILToolService(config, backend=OperationBackend(reset_error=KeyboardInterrupt()), reload_config=False)

    with pytest.raises(KeyboardInterrupt):
        service.call("reset_target", {"mode": "run"})

    state = hardware_status(config.config_path)
    assert state["hardware_state_unconfirmed"] is True
    assert state["state"]["inspection_errors"][0]["tool"] == "reset_target"


def test_structured_one_shot_failure_is_completion_confirmed(tmp_path: Path) -> None:
    config = load_test_config(tmp_path)
    service = AgenticHILToolService(config, backend=OperationBackend(reset_result={"ok": False, "error_type": "reset_failed"}), reload_config=False)

    result = service.call("reset_target", {"mode": "run"})

    assert result["error_type"] == "reset_failed"
    assert hardware_status(config.config_path)["hardware_state_unconfirmed"] is False


def test_unhandled_flash_exception_without_session_stays_quarantined(tmp_path: Path) -> None:
    config = load_test_config(tmp_path)
    firmware = tmp_path / "build" / "app.elf"
    firmware.parent.mkdir(parents=True)
    firmware.write_bytes(b"\x7fELFtest")
    service = AgenticHILToolService(config, backend=OperationBackend(flash_error=OSError("transport lost")), reload_config=False)

    with pytest.raises(OSError, match="transport lost"):
        service.call("flash_firmware", {"image_path": "build/app.elf"})

    assert hardware_status(config.config_path)["hardware_state_unconfirmed"] is True


def test_malformed_hardware_result_stays_quarantined(tmp_path: Path) -> None:
    config = load_test_config(tmp_path)
    service = AgenticHILToolService(config, backend=MalformedResultBackend(), reload_config=False)

    with pytest.raises(TypeError, match="non-object"):
        service.call("reset_target", {"mode": "run"})

    assert hardware_status(config.config_path)["hardware_state_unconfirmed"] is True


def test_structured_hardware_timeout_stays_quarantined(tmp_path: Path) -> None:
    config = load_test_config(tmp_path)
    backend = OperationBackend(reset_result={"ok": False, "error_type": "timeout"})
    service = AgenticHILToolService(config, backend=backend, reload_config=False)

    assert service.call("reset_target", {"mode": "run"})["error_type"] == "timeout"

    assert hardware_status(config.config_path)["hardware_state_unconfirmed"] is True


def test_explicitly_confirmed_timeout_releases_hardware_lease(tmp_path: Path) -> None:
    config = load_test_config(tmp_path)
    backend = OperationBackend(reset_result={"ok": False, "error_type": "timeout", "completion_confirmed": True})
    service = AgenticHILToolService(config, backend=backend, reload_config=False)

    assert service.call("reset_target", {"mode": "run"})["error_type"] == "timeout"

    assert hardware_status(config.config_path)["hardware_state_unconfirmed"] is False


def test_interrupted_debug_start_closes_backend_before_quarantine_decision(tmp_path: Path) -> None:
    config = load_test_config(tmp_path)
    image = tmp_path / "firmware.elf"
    image.write_bytes(b"elf")
    backend = InterruptingDebugBackend()
    artifacts = SimpleNamespace(validate_local_path=lambda _: {"ok": True, "artifact": {"resolved_path": str(image)}})
    service = AgenticHILToolService(config, backend=backend, artifacts=artifacts, reload_config=False)

    with pytest.raises(KeyboardInterrupt):
        service.call("debug_start_session", {"image_path": str(image)})

    assert backend.active is False
    assert hardware_status(config.config_path)["hardware_state_unconfirmed"] is False


def test_dead_adapter_bridge_blocks_followup_before_backend_call(tmp_path: Path) -> None:
    config = load_test_config(tmp_path, adapters_yaml=ADAPTER_YAML)
    backend = OperationBackend()
    adapters = AdapterService(config)
    adapter_config = AdapterConfig(executable="python", args=[], timeout_s=1.0, channels=["temp"], faults=["open"])
    log_path = tmp_path / ".agentic-hil" / "logs" / "dead-adapter.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    adapters.sessions["ntc"] = AdapterSession("ntc", adapter_config, DeadBridge(), str(log_path))
    service = AgenticHILToolService(config, backend=backend, adapters=adapters, reload_config=False)

    state = service.hardware_state()
    blocked = service.call("probe_target")

    assert state["active"] is True
    assert state["inspection_errors"][0]["id"] == "ntc"
    assert blocked["error_type"] == "hardware_state_unconfirmed"
    assert backend.probe_calls == 0


def test_dead_process_can_bridge_is_unconfirmed(tmp_path: Path) -> None:
    config = load_test_config(tmp_path, can_buses_yaml=CAN_BUS_YAML)
    can_buses = CanBusService(config)
    log_path = tmp_path / ".agentic-hil" / "logs" / "dead-can.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    can_buses.sessions["bench"] = CanBusSession("bench", config.can_buses["bench"], DeadCanAdapter(), str(log_path))
    service = AgenticHILToolService(config, can_buses=can_buses, reload_config=False)

    state = service.hardware_state()

    assert state["active"] is True
    assert state["inspection_errors"][0]["id"] == "bench"


def test_process_can_rejects_malformed_close_result(tmp_path: Path) -> None:
    config = load_test_config(tmp_path, can_buses_yaml=CAN_BUS_YAML)
    can_buses = CanBusService(config)
    log_path = tmp_path / ".agentic-hil" / "logs" / "malformed-close.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    session = CanBusSession("bench", config.can_buses["bench"], MalformedCloseCanAdapter(), str(log_path))
    can_buses.sessions["bench"] = session

    with pytest.raises(RuntimeError, match="structured close result"):
        can_buses.session_stop("bench")

    assert session.cleanup_unconfirmed is True


@pytest.mark.parametrize("safe", [False, True])
def test_bridge_stop_interrupt_preserves_confirmed_cleanup_state(tmp_path: Path, safe: bool) -> None:
    config = load_test_config(tmp_path, adapters_yaml=ADAPTER_YAML)
    adapters = AdapterService(config)
    adapter_config = AdapterConfig(executable="python", args=[], timeout_s=1.0, channels=["temp"], faults=["open"])
    log_path = tmp_path / ".agentic-hil" / "logs" / "interrupt-close.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    adapters.sessions["ntc"] = AdapterSession("ntc", adapter_config, InterruptAfterCloseBridge(safe), str(log_path))
    service = AgenticHILToolService(config, adapters=adapters, reload_config=False)

    with pytest.raises(KeyboardInterrupt):
        service.call("adapter_session_stop", {"adapter_id": "ntc"})

    status = hardware_status(config.config_path)
    assert status["hardware_state_unconfirmed"] is (not safe)


def test_external_recovery_does_not_reenable_poisoned_service(tmp_path: Path) -> None:
    config = load_test_config(tmp_path, adapters_yaml=ADAPTER_YAML)
    backend = OperationBackend()
    adapters = AdapterService(config)
    adapter_config = AdapterConfig(executable="python", args=[], timeout_s=1.0, channels=["temp"], faults=["open"])
    log_dir = tmp_path / ".agentic-hil" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    adapters.sessions["ntc"] = AdapterSession("ntc", adapter_config, UnsafeClosingBridge(), str(log_dir / "ntc.jsonl"))
    service = AgenticHILToolService(config, backend=backend, adapters=adapters, reload_config=False)
    with pytest.raises(RuntimeError):
        service.call("adapter_session_stop", {"adapter_id": "ntc"})
    status = hardware_status(config.config_path)
    recovered = hardware_recover(config.config_path, acknowledge_hardware_checked=True, quarantine_id=str(status["quarantine_id"]))

    blocked = service.call("probe_target")

    assert recovered["ok"] is True
    assert blocked["error_type"] == "hardware_state_unconfirmed"
    assert backend.probe_calls == 0


def test_poisoned_service_does_not_recreate_recovered_incident_on_close(tmp_path: Path) -> None:
    config = load_test_config(tmp_path)
    service = AgenticHILToolService(config, backend=OperationBackend(reset_error=KeyboardInterrupt()), reload_config=False)
    with pytest.raises(KeyboardInterrupt):
        service.call("reset_target", {"mode": "run"})
    status = hardware_status(config.config_path)
    assert hardware_recover(config.config_path, acknowledge_hardware_checked=True, quarantine_id=str(status["quarantine_id"]))["ok"] is True

    assert service.call("probe_target")["error_type"] == "hardware_state_unconfirmed"
    service.close()

    assert hardware_status(config.config_path)["hardware_state_unconfirmed"] is False


def test_preflight_incident_is_not_recreated_after_recovery(tmp_path: Path) -> None:
    config = load_test_config(tmp_path)
    service = AgenticHILToolService(config, adapters=InspectionOnlySubsystem(), reload_config=False)

    assert service.call("probe_target")["error_type"] == "hardware_state_unconfirmed"
    status = hardware_status(config.config_path)
    assert hardware_recover(config.config_path, acknowledge_hardware_checked=True, quarantine_id=str(status["quarantine_id"]))["ok"] is True
    service.close()

    assert hardware_status(config.config_path)["hardware_state_unconfirmed"] is False


def test_cleanup_interrupt_still_closes_remaining_subsystems_and_finalizes_lease(tmp_path: Path) -> None:
    config = load_test_config(tmp_path)
    backend = CleanupInterruptBackend()
    com_ports = FailingCleanupSubsystem()
    can_buses = FailingCleanupSubsystem()
    adapters = FailingCleanupSubsystem()
    service = AgenticHILToolService(config, backend=backend, com_ports=com_ports, can_buses=can_buses, adapters=adapters, reload_config=False)
    assert service._hardware_lock.acquire(source="test") is True

    with pytest.raises(KeyboardInterrupt):
        service.close()

    assert backend.close_calls == com_ports.close_calls == can_buses.close_calls == adapters.close_calls == 1
    assert hardware_status(config.config_path)["hardware_state_unconfirmed"] is False


def test_hardware_calls_are_serialized_within_service(tmp_path: Path) -> None:
    config = load_test_config(tmp_path)
    backend = ConcurrentResetBackend()
    service = AgenticHILToolService(config, backend=backend, reload_config=False)
    threads = [threading.Thread(target=service.call, args=("reset_target", {"mode": "run"})) for _ in range(2)]

    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=WAIT_TIMEOUT_S)

    assert all(not thread.is_alive() for thread in threads)
    assert backend.max_active_calls == 1


def test_nonhardware_call_and_close_wait_for_hardware_operation(tmp_path: Path) -> None:
    config = load_test_config(tmp_path)
    backend = BlockingResetBackend()
    service = AgenticHILToolService(config, backend=backend, reload_config=False)
    hardware_thread = threading.Thread(target=lambda: service.call("reset_target", {"mode": "run"}))
    nonhardware_thread = threading.Thread(target=lambda: service.call("get_last_report"))
    close_thread = threading.Thread(target=service.close)

    hardware_thread.start()
    assert backend.started.wait(WAIT_TIMEOUT_S)
    nonhardware_thread.start()
    close_thread.start()
    time.sleep(0.05)

    assert nonhardware_thread.is_alive()
    assert close_thread.is_alive()
    assert hardware_status(config.config_path)["state"]["state"] == "active"

    backend.release.set()
    for thread in (hardware_thread, nonhardware_thread, close_thread):
        thread.join(timeout=WAIT_TIMEOUT_S)
        assert not thread.is_alive()

    assert hardware_status(config.config_path)["hardware_state_unconfirmed"] is False


def test_same_lock_instance_concurrent_acquire_does_not_leak_owner(tmp_path: Path) -> None:
    config = load_test_config(tmp_path)
    lock = ProjectHardwareLock(config.config_path)
    results: list[bool] = []
    threads = [threading.Thread(target=lambda: results.append(lock.acquire(source="thread"))) for _ in range(2)]

    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=WAIT_TIMEOUT_S)

    owner_token = lock.owner_token
    assert results == [True, True]
    assert ProjectHardwareLock.owner_is_active(config.config_path, owner_token) is True
    lock.release()
    assert ProjectHardwareLock.owner_is_active(config.config_path, owner_token) is False


def test_adapter_post_open_interrupt_runs_confirmed_cleanup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = load_test_config(tmp_path, adapters_yaml=ADAPTER_YAML)
    bridge = SafeClosingBridge()

    def opened(*args):
        args[3](bridge)
        return {"ok": True, "session": bridge}

    monkeypatch.setattr("agentic_hil.adapters.open_adapter_bridge", opened)
    monkeypatch.setattr("agentic_hil.adapters.append_jsonl", lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    service = AgenticHILToolService(config, reload_config=False)

    with pytest.raises(KeyboardInterrupt):
        service.call("adapter_session_start", {"adapter_id": "ntc"})

    assert bridge.last_close_result is not None and bridge.last_close_result.cleanup_confirmed
    state = hardware_status(config.config_path)
    assert state["hardware_state_unconfirmed"] is False, json.dumps(state, indent=2)


def test_can_post_open_interrupt_runs_confirmed_cleanup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = load_test_config(tmp_path, can_buses_yaml=CAN_BUS_YAML)
    bridge = SafeClosingBridge()

    def opened(*args):
        args[4](bridge)
        return {"ok": True, "session": bridge}

    monkeypatch.setattr("agentic_hil.can.open_adapter", opened)
    monkeypatch.setattr("agentic_hil.can.append_jsonl", lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    service = AgenticHILToolService(config, reload_config=False)

    with pytest.raises(KeyboardInterrupt):
        service.call("can_session_start", {"bus_id": "bench", "clear_rx_queue": False})

    assert bridge.last_close_result is not None and bridge.last_close_result.cleanup_confirmed
    state = hardware_status(config.config_path)
    assert state["hardware_state_unconfirmed"] is False, json.dumps(state, indent=2)


def test_adapter_bridge_constructor_interrupt_reaps_child_and_quarantines(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = load_test_config(tmp_path, adapters_yaml=ADAPTER_YAML)
    child = FakeChild()
    monkeypatch.setattr("agentic_hil.adapters.resolve_work_path", lambda *_: sys.executable)
    monkeypatch.setattr("agentic_hil.adapters.subprocess.Popen", lambda *args, **kwargs: child)
    monkeypatch.setattr("agentic_hil.adapters.AdapterBridgeSession", lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    service = AgenticHILToolService(config, reload_config=False)

    with pytest.raises(KeyboardInterrupt):
        service.call("adapter_session_start", {"adapter_id": "ntc"})

    assert child.terminated is True
    assert hardware_status(config.config_path)["hardware_state_unconfirmed"] is True


def test_process_can_constructor_interrupt_reaps_child_and_quarantines(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = load_test_config(tmp_path, can_buses_yaml=CAN_BUS_YAML)
    child = FakeChild()
    monkeypatch.setattr("agentic_hil.can.resolve_work_path", lambda *_: sys.executable)
    monkeypatch.setattr("agentic_hil.can.subprocess.Popen", lambda *args, **kwargs: child)
    monkeypatch.setattr("agentic_hil.can.ProcessCanAdapterSession", lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    service = AgenticHILToolService(config, reload_config=False)

    with pytest.raises(KeyboardInterrupt):
        service.call("can_session_start", {"bus_id": "bench"})

    assert child.terminated is True
    assert hardware_status(config.config_path)["hardware_state_unconfirmed"] is True


def test_python_can_registration_interrupt_closes_bus(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = load_test_config(tmp_path, can_buses_yaml=CAN_BUS_YAML.replace('adapter: "process"', 'adapter: "socketcan"'))
    bus = FakeCanBus()
    monkeypatch.setitem(sys.modules, "can", SimpleNamespace(Bus=lambda **_: bus))

    with pytest.raises(KeyboardInterrupt) as raised:
        open_python_can_adapter(config, "bench", config.can_buses["bench"], False, lambda _: (_ for _ in ()).throw(KeyboardInterrupt()))

    assert bus.active is False
    assert raised.value._agentic_hil_completion_confirmed is True


def test_python_can_registered_interrupt_preserves_original_and_safe_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    can_yaml = CAN_BUS_YAML.replace('adapter: "process"', 'adapter: "socketcan"')
    config = load_test_config(tmp_path, can_buses_yaml=can_yaml)
    bridge = SafeClosingBridge()

    def interrupted(*args):
        args[4](bridge)
        bridge.active = False
        raise KeyboardInterrupt

    monkeypatch.setattr("agentic_hil.can.open_adapter", interrupted)
    service = AgenticHILToolService(config, reload_config=False)

    with pytest.raises(KeyboardInterrupt):
        service.call("can_session_start", {"bus_id": "bench", "clear_rx_queue": False})

    assert hardware_status(config.config_path)["hardware_state_unconfirmed"] is False


def test_com_session_constructor_interrupt_closes_handle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = load_test_config(tmp_path, com_ports_yaml=COM_PORT_YAML)
    serial_handle = IdleSerialHandle()
    monkeypatch.setitem(sys.modules, "serial", SimpleNamespace(Serial=lambda *args, **kwargs: serial_handle))
    monkeypatch.setattr("agentic_hil.comports.ComPortSession", lambda *args, **kwargs: (_ for _ in ()).throw(KeyboardInterrupt()))
    service = AgenticHILToolService(config, reload_config=False)

    with pytest.raises(KeyboardInterrupt):
        service.call("com_session_start", {"port_id": "dut"})

    assert serial_handle.is_open is False
    assert hardware_status(config.config_path)["hardware_state_unconfirmed"] is False


def test_adapter_stop_report_interrupt_does_not_quarantine_safe_hardware(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = load_test_config(tmp_path, adapters_yaml=ADAPTER_YAML)
    log_path = tmp_path / ".agentic-hil" / "logs" / "adapter-stop-report.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    adapters = AdapterService(config)
    adapters.sessions["ntc"] = AdapterSession("ntc", config.adapters["ntc"], SafeClosingBridge(), str(log_path))
    monkeypatch.setattr("agentic_hil.adapters.write_report", lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    service = AgenticHILToolService(config, adapters=adapters, reload_config=False)

    with pytest.raises(KeyboardInterrupt):
        service.call("adapter_session_stop", {"adapter_id": "ntc"})

    assert hardware_status(config.config_path)["hardware_state_unconfirmed"] is False


def test_can_stop_report_interrupt_does_not_quarantine_safe_hardware(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = load_test_config(tmp_path, can_buses_yaml=CAN_BUS_YAML)
    log_path = tmp_path / ".agentic-hil" / "logs" / "can-stop-report.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    can_buses = CanBusService(config)
    can_buses.sessions["bench"] = CanBusSession("bench", config.can_buses["bench"], SafeClosingBridge(), str(log_path))
    monkeypatch.setattr("agentic_hil.can.write_report", lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    service = AgenticHILToolService(config, can_buses=can_buses, reload_config=False)

    with pytest.raises(KeyboardInterrupt):
        service.call("can_session_stop", {"bus_id": "bench"})

    assert hardware_status(config.config_path)["hardware_state_unconfirmed"] is False


def test_com_stop_report_interrupt_does_not_quarantine_safe_hardware(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = load_test_config(tmp_path, com_ports_yaml=COM_PORT_YAML)
    log_path = tmp_path / ".agentic-hil" / "logs" / "com-stop-report.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    serial_handle = IdleSerialHandle()
    com_ports = ComPortService(config)
    com_ports.sessions["dut"] = ComPortSession("dut", config.com_ports["dut"], serial_handle, str(log_path))
    monkeypatch.setattr("agentic_hil.comports.write_report", lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    service = AgenticHILToolService(config, com_ports=com_ports, reload_config=False)

    with pytest.raises(KeyboardInterrupt):
        service.call("com_session_stop", {"port_id": "dut"})

    assert serial_handle.is_open is False
    assert hardware_status(config.config_path)["hardware_state_unconfirmed"] is False


def test_noop_stop_report_interrupts_do_not_quarantine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = load_test_config(tmp_path, com_ports_yaml=COM_PORT_YAML, can_buses_yaml=CAN_BUS_YAML, adapters_yaml=ADAPTER_YAML)
    monkeypatch.setattr("agentic_hil.comports.write_report", lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    monkeypatch.setattr("agentic_hil.can.write_report", lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    monkeypatch.setattr("agentic_hil.adapters.write_report", lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    service = AgenticHILToolService(config, reload_config=False)

    for tool, arguments in (
        ("com_session_stop", {"port_id": "dut"}),
        ("can_session_stop", {"bus_id": "bench"}),
        ("adapter_session_stop", {"adapter_id": "ntc"}),
    ):
        with pytest.raises(KeyboardInterrupt):
            service.call(tool, arguments)

    assert hardware_status(config.config_path)["hardware_state_unconfirmed"] is False


def test_config_reload_closes_removed_session_and_releases_lease(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_path = write_config(tmp_path, com_ports_yaml=COM_PORT_YAML)
    config = load_config(str(config_path), str(tmp_path))
    service = AgenticHILToolService(config)
    serial_handle = IdleSerialHandle()
    log_path = tmp_path / ".agentic-hil" / "logs" / "reload.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    session = ComPortSession("dut", config.com_ports["dut"], serial_handle, str(log_path))
    monkeypatch.setattr(service.com_ports, "_open_serial", lambda *_: {"ok": True, "session": session})

    try:
        assert service.call("com_session_start", {"port_id": "dut"})["ok"] is True
        write_config(tmp_path)
        assert service.call("debugger_info")["ok"] is True

        assert "dut" not in service.com_ports.sessions
        assert serial_handle.is_open is False
        second_lock = ProjectHardwareLock(config.config_path)
        assert second_lock.acquire() is True
        second_lock.release()
    finally:
        service.close()


def test_artifact_upload_rejects_oversized_local_file(tmp_path: Path) -> None:
    config = load_test_config(tmp_path)
    build_dir = tmp_path / "build"
    build_dir.mkdir(parents=True, exist_ok=True)
    oversized = build_dir / "big.bin"
    oversized.write_bytes(b"\0" * (config.artifacts.max_upload_size_mb * 1024 * 1024 + 1))
    service = AgenticHILToolService(config)

    result = service.call("artifact_upload", {"image_path": "build/big.bin"})

    assert result["ok"] is False
    assert result["error_type"] == "artifact_too_large"


def test_com_ports_list_hides_host_ports_without_read_permission(tmp_path: Path) -> None:
    config = load_test_config(
        tmp_path,
        com_ports_yaml=COM_PORT_YAML,
        permissions_yaml="permissions:\n  allow_com_read: false\n",
    )
    service = ComPortService(config)

    result = service.list_ports()

    assert result["ok"] is True
    assert "dut" in result["ports"]
    assert result["available_com_ports"]["ok"] is False
    assert result["available_com_ports"]["error_type"] == "permission_denied"


def test_parse_can_id_rejects_booleans() -> None:
    assert parse_can_id(True) is None
    assert parse_can_id(False) is None
    assert parse_can_id(1) == 1


def test_payload_frame_rejects_boolean_frame_id() -> None:
    bus_config = CanBusConfig(
        adapter="process", channel="vcan0", bitrate=500000, fd=False, data_bitrate=None,
        pcanbasic_dll=None, executable=None, args=[], timeout_s=1.0, poll_interval_ms=10,
        receive_own_messages=False, listen_only=False, max_buffer_frames=16, max_frame_data_bytes=8,
    )

    result = payload_frame(bus_config, {"frame_id": True, "data_hex": "00"})

    assert result["ok"] is False
    assert result["error_type"] == "invalid_argument"


def test_bridge_stderr_is_capped_and_surfaced_in_errors() -> None:
    noisy_lines = ["x" * 1024 + "\n"] * 256
    child = SimpleNamespace(stdout=[], stderr=noisy_lines, stdin=None, poll=lambda: None)
    session = ProcessBridgeSession(child)

    assert wait_until(lambda: len(session.stderr) > 0)
    assert wait_until(lambda: session.stderr.endswith("x" * 1024 + "\n"))
    assert len(session.stderr) <= 65536

    error = session._bridge_error("timeout", "Bridge request timed out.")
    assert error["stderr_tail"]
    assert len(error["stderr_tail"]) <= 2000


def test_debug_set_breakpoint_requires_location(tmp_path: Path) -> None:
    service = AgenticHILToolService(load_test_config(tmp_path))

    result = service.call("debug_set_breakpoint", {})

    assert result["ok"] is False
    assert result["error_type"] == "invalid_argument"


def test_flash_rejects_non_boolean_reset_after_flash(tmp_path: Path) -> None:
    service = AgenticHILToolService(load_test_config(tmp_path))

    result = service.call("flash_firmware", {"image_path": "build/app.elf", "reset_after_flash": "false"})

    assert result["ok"] is False
    assert result["error_type"] == "invalid_argument"


PERMISSION_GATE_CASES = [
    ("allow_probe", "probe_target", {}),
    ("allow_flash", "flash_firmware", {"image_path": "build/app.elf"}),
    ("allow_com_read", "com_session_start", {"port_id": "dut"}),
    ("allow_com_write", "com_write", {"port_id": "dut", "text": "hi"}),
    ("allow_can_read", "can_read", {"bus_id": "bench"}),
    ("allow_can_write", "can_send", {"bus_id": "bench", "frame_id": 1, "data_hex": "00"}),
    ("allow_adapter_read", "adapter_measure", {"adapter_id": "ntc", "channel": "temp"}),
]


@pytest.mark.parametrize(("flag", "tool", "arguments"), PERMISSION_GATE_CASES)
def test_disabled_permission_blocks_tool(tmp_path: Path, flag: str, tool: str, arguments: dict) -> None:
    config = load_test_config(
        tmp_path,
        com_ports_yaml=COM_PORT_YAML,
        can_buses_yaml=CAN_BUS_YAML,
        adapters_yaml=ADAPTER_YAML,
        permissions_yaml=f"permissions:\n  {flag}: false\n",
    )
    service = AgenticHILToolService(config)

    result = service.call(tool, arguments)

    assert result["ok"] is False, f"{tool} must be blocked when {flag} is false"
    assert result["error_type"] == "permission_denied"
