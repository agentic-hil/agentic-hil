from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import FAKE_OPENOCD, SIM_NTC_ADAPTER, write_authoritative_config, write_config

from agentic_hil.adapters import AdapterService
from agentic_hil.artifacts import ArtifactManager
from agentic_hil.backends.common import spawn_command
from agentic_hil.backends.gdbdebug import GdbDebugSessions
from agentic_hil.bridge import BridgeCleanupError, ProcessBridgeSession
from agentic_hil.can import CanBusService, CanBusSession, parse_can_id, payload_frame
from agentic_hil.comports import ComPortService, ComPortSession
from agentic_hil.comstdio import run_com_stdio
from agentic_hil.config import (
    _PATH_LOCKS,
    ConfigError,
    _close_windows_handles,
    _windows_hold_directory_chain,
    load_authoritative_config,
    load_config,
    project_state_directory,
    trusted_state_directory,
    validated_state_root,
)
from agentic_hil.gdbmi import GdbMiClient
from agentic_hil.mcp import handle_mcp_message
from agentic_hil.report import (
    append_canonical_audit_log,
    append_jsonl,
    attach_canonical_audit_evidence,
    canonical_audit_evidence,
    canonical_audit_log_path,
    ensure_audit_ready,
    logs_directory,
    read_last_failure,
    read_last_report,
    report_lock_path,
    report_state_path,
    write_report,
)
from agentic_hil.stdio import run_stdio_server
from agentic_hil.tools import AgenticHILToolService
from agentic_hil.types import CanBusConfig

WAIT_TIMEOUT_S = 5.0
POLL_INTERVAL_S = 0.01

COM_PORT_YAML = 'com_ports:\n  dut:\n    device: "/dev/ttyAGENTIC_HILTEST"\n'
CAN_BUS_YAML = 'can_buses:\n  bench:\n    adapter: "process"\n    channel: "vcan0"\n    executable: "python"\n'


def load_test_config(tmp_path: Path, **kwargs):
    return load_config(str(write_config(tmp_path, **kwargs)))


def wait_until(predicate, timeout_s: float = WAIT_TIMEOUT_S) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(POLL_INTERVAL_S)
    return predicate()


def test_state_root_is_pinned_and_outside_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = load_test_config(tmp_path)
    original = project_state_directory(config)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "changed"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "changed"))

    assert project_state_directory(config) == original
    assert not original.is_relative_to(tmp_path)


def test_state_root_inside_workspace_is_rejected(tmp_path: Path) -> None:
    path = write_config(tmp_path, state_root=tmp_path / "state")
    with pytest.raises(ConfigError) as excinfo:
        load_config(str(path))
    assert excinfo.value.details["field"] == "state_root"


def test_relative_state_root_is_rejected(tmp_path: Path) -> None:
    path = write_config(tmp_path)
    text = path.read_text(encoding="utf-8")
    text = re.sub(r"^state_root:.*$", "state_root: relative-state", text, flags=re.MULTILINE)
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError) as excinfo:
        load_config(str(path))
    assert excinfo.value.details["field"] == "state_root"


@pytest.mark.skipif(os.name == "nt", reason="POSIX ownership and mode semantics")
def test_group_writable_state_root_is_rejected(tmp_path: Path) -> None:
    path = write_config(tmp_path)
    config = load_config(str(path))
    root = Path(config.state_root)
    root.chmod(0o770)
    try:
        with pytest.raises(ConfigError) as excinfo:
            load_config(str(path))
        assert excinfo.value.error_type == "unsafe_configured_path"
    finally:
        root.chmod(0o700)


@pytest.mark.skipif(os.name == "nt", reason="POSIX sticky-bit and mode semantics")
def test_sticky_world_writable_final_state_root_is_rejected(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    root = tmp_path / "shared-root"
    root.mkdir()
    root.chmod(0o1777)
    try:
        with pytest.raises(ConfigError) as excinfo:
            validated_state_root(str(root), workspace, str(tmp_path / "config.yaml"))
        assert excinfo.value.error_type == "unsafe_configured_path"
        assert excinfo.value.details["field"] == "state_root"
    finally:
        root.chmod(0o700)


@pytest.mark.skipif(os.name == "nt", reason="POSIX sticky-bit and mode semantics")
def test_operator_owned_state_root_under_sticky_ancestor_is_accepted(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    sticky_parent = tmp_path / "tmp-like"
    sticky_parent.mkdir()
    sticky_parent.chmod(0o1777)
    owned = sticky_parent / "operator-owned-0700"
    owned.mkdir(mode=0o700)
    try:
        resolved = validated_state_root(str(owned), workspace, str(tmp_path / "config.yaml"))
        assert resolved == owned.resolve()
    finally:
        sticky_parent.chmod(0o700)


@pytest.mark.skipif(os.name == "nt", reason="POSIX ownership and mode semantics")
def test_world_writable_derived_state_dir_is_rejected(tmp_path: Path) -> None:
    config = load_config(str(write_config(tmp_path)))
    foreign = Path(config.state_root) / "coordination"
    foreign.mkdir(parents=True)
    foreign.chmod(0o777)
    try:
        with pytest.raises(ConfigError) as excinfo:
            trusted_state_directory(config.state_root, "coordination")
        assert excinfo.value.error_type == "unsafe_configured_path"
        assert excinfo.value.details["field"] == "state_root"
    finally:
        foreign.chmod(0o700)


@pytest.mark.skipif(os.name == "nt", reason="POSIX ownership and mode semantics")
def test_world_writable_lock_dir_blocks_coordinator_construction(tmp_path: Path) -> None:
    from agentic_hil.coordination import HardwareCoordinator

    config = load_config(str(write_config(tmp_path)))
    locks = Path(config.state_root) / "coordination" / "locks"
    locks.mkdir(parents=True)
    locks.chmod(0o777)
    try:
        with pytest.raises(ConfigError) as excinfo:
            HardwareCoordinator(config, "owner")
        assert excinfo.value.error_type == "unsafe_configured_path"
    finally:
        locks.chmod(0o700)


@pytest.mark.skipif(os.name != "nt", reason="Windows ACL semantics")
def test_broadly_writable_windows_state_root_is_rejected(tmp_path: Path) -> None:
    path = write_config(tmp_path)
    config = load_config(str(path))
    root = Path(config.state_root)
    grant = subprocess.run(["icacls", str(root), "/grant", "*S-1-1-0:(OI)(CI)M"], capture_output=True, text=True, check=False)
    if grant.returncode != 0:
        pytest.skip(f"could not set temporary test ACL: {grant.stderr}")
    try:
        with pytest.raises(ConfigError) as excinfo:
            load_config(str(path))
        assert excinfo.value.error_type == "unsafe_configured_path"
    finally:
        subprocess.run(["icacls", str(root), "/remove:g", "*S-1-1-0"], capture_output=True, check=False)


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


class RetryCloseSerialHandle:
    is_open = True
    in_waiting = 0

    def __init__(self) -> None:
        self.close_attempts = 0

    def read(self, size: int) -> bytes:
        return b""

    def close(self) -> None:
        self.close_attempts += 1
        if self.close_attempts == 1:
            raise OSError("port busy during close")
        self.is_open = False


class FailingCancelSerialHandle:
    is_open = True
    in_waiting = 0

    def __init__(self) -> None:
        self.close_attempts = 0

    def read(self, size: int) -> bytes:
        return b""

    def cancel_read(self) -> None:
        raise OSError("cancel failed")

    def close(self) -> None:
        self.close_attempts += 1
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


def test_com_session_stop_retains_session_when_close_fails_for_retry(tmp_path: Path) -> None:
    config = load_test_config(tmp_path, com_ports_yaml=COM_PORT_YAML)
    service = ComPortService(config)
    log_path = tmp_path / ".agentic-hil" / "logs" / "test-com-close.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handle = RetryCloseSerialHandle()
    session = ComPortSession("dut", config.com_ports["dut"], handle, str(log_path))
    service.sessions["dut"] = session

    first = service.session_stop("dut")
    second = service.session_stop("dut")

    assert first["ok"] is False
    assert first["error_type"] == "com_port_close_failed"
    assert second["ok"] is True
    assert handle.close_attempts == 2
    assert "dut" not in service.sessions


def test_com_session_stop_attempts_close_after_cancel_failure(tmp_path: Path) -> None:
    config = load_test_config(tmp_path, com_ports_yaml=COM_PORT_YAML)
    service = ComPortService(config)
    log_path = tmp_path / ".agentic-hil" / "logs" / "test-com-cancel.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handle = FailingCancelSerialHandle()
    session = ComPortSession("dut", config.com_ports["dut"], handle, str(log_path), start_reader=False)
    service.sessions["dut"] = session

    result = service.session_stop("dut")

    assert result["error_type"] == "com_port_close_failed"
    assert result["cleanup_required"] is True
    assert handle.close_attempts == 1


def test_com_open_failure_without_cleanup_confirmation_quarantines(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = load_test_config(tmp_path, com_ports_yaml=COM_PORT_YAML)
    service = ComPortService(config)
    monkeypatch.setattr(service, "_open_serial", lambda *args: {"ok": False, "tool": "com_session_start", "port_id": "dut", "error_type": "com_port_open_failed", "summary": "partial open unknown"})
    try:
        result = service.session_start("dut")

        assert result["cleanup_required"] is True
        assert result["lease_state"] == "cleanup_required"
        assert service.coordinator.blocked is True
    finally:
        service.close()


def test_com_open_failure_with_confirmed_cleanup_releases(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = load_test_config(tmp_path, com_ports_yaml=COM_PORT_YAML)
    service = ComPortService(config)
    monkeypatch.setattr(service, "_open_serial", lambda *args: {"ok": False, "tool": "com_session_start", "port_id": "dut", "error_type": "com_port_open_failed", "summary": "open rolled back", "cleanup_confirmed": True})

    result = service.session_start("dut")

    assert result["cleanup_confirmed"] is True
    assert result["lease_state"] == "released"
    assert result.get("cleanup_required") is not True


def test_com_reader_start_failure_reports_before_lease_release(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = load_test_config(tmp_path, com_ports_yaml=COM_PORT_YAML)
    service = ComPortService(config)
    handle = SimpleNamespace(is_open=True, in_waiting=0, close=lambda: setattr(handle, "is_open", False))

    def opened(port_id, port_config, log_path, lease):
        session = ComPortSession(port_id, port_config, handle, log_path, lease, start_reader=False)
        session.start_reader = lambda: (_ for _ in ()).throw(RuntimeError("thread failed"))
        return {"ok": True, "session": session}

    monkeypatch.setattr(service, "_open_serial", opened)
    result = service.session_start("dut")

    assert result["error_type"] == "com_reader_start_failed"
    assert result["lease_state"] == "released"
    # The persisted report must prove the SAME terminal lease_state it returned;
    # the action-time snapshot is no longer left behind under report_path.
    assert read_last_report(config)["lease_state"] == "released"
    assert result["report_path"] == read_last_report(config)["report_path"]
    assert service.sessions == {}


def test_com_reader_started_then_start_raises_is_joined_before_release(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = load_test_config(tmp_path, com_ports_yaml=COM_PORT_YAML)
    service = ComPortService(config)

    class BlockingHandle:
        is_open = True
        in_waiting = 0

        def __init__(self) -> None:
            self.closed = threading.Event()

        def read(self, size: int) -> bytes:
            self.closed.wait()
            return b""

        def close(self) -> None:
            self.is_open = False
            self.closed.set()

    handle = BlockingHandle()
    captured: list[ComPortSession] = []

    def opened(port_id, port_config, log_path, lease):
        session = ComPortSession(port_id, port_config, handle, log_path, lease, start_reader=False)
        captured.append(session)
        return {"ok": True, "session": session}

    original_start = threading.Thread.start

    def start_then_raise(thread: threading.Thread) -> None:
        original_start(thread)
        raise RuntimeError("start failed after launch")

    monkeypatch.setattr(service, "_open_serial", opened)
    monkeypatch.setattr(threading.Thread, "start", start_then_raise)

    result = service.session_start("dut")

    assert result["error_type"] == "com_reader_start_failed"
    assert result["lease_state"] == "released"
    assert captured[0].reader is not None
    assert not captured[0].reader.is_alive()
    assert handle.is_open is False


def test_new_com_session_clears_os_and_memory_buffers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = load_test_config(tmp_path, com_ports_yaml=COM_PORT_YAML)
    service = ComPortService(config)
    reset_calls = 0

    class BufferedHandle:
        is_open = True

        def reset_input_buffer(self) -> None:
            nonlocal reset_calls
            reset_calls += 1

        def close(self) -> None:
            self.is_open = False

    def opened(port_id, port_config, log_path, lease):
        session = ComPortSession(port_id, port_config, BufferedHandle(), log_path, lease, start_reader=False)
        session.buffer.extend(b"stale")
        session.overflow_bytes = 3
        session.start_reader = lambda: None
        return {"ok": True, "session": session}

    monkeypatch.setattr(service, "_open_serial", opened)
    try:
        result = service.session_start("dut", clear_buffer=True)

        assert result["ok"] is True
        assert reset_calls == 1
        assert service.sessions["dut"].buffer == b""
        assert service.sessions["dut"].overflow_bytes == 0
    finally:
        service.close()


def test_com_session_constructor_failure_closes_raw_handle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = load_test_config(tmp_path, com_ports_yaml=COM_PORT_YAML)
    service = ComPortService(config)
    handle = SimpleNamespace(closed=False, close=lambda: setattr(handle, "closed", True))
    monkeypatch.setitem(sys.modules, "serial", SimpleNamespace(Serial=lambda *args, **kwargs: handle))
    monkeypatch.setattr("agentic_hil.comports.ComPortSession", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("construct failed")))

    result = service._open_serial("dut", config.com_ports["dut"], str(tmp_path / "com.jsonl"), SimpleNamespace())

    assert result["ok"] is False
    assert handle.closed is True


def test_can_session_constructor_failure_shuts_down_raw_bus(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from agentic_hil.can import open_python_can_adapter

    config = load_test_config(tmp_path, can_buses_yaml='can_buses:\n  bench:\n    adapter: "socketcan"\n    channel: "vcan0"\n')
    bus = SimpleNamespace(closed=False, shutdown=lambda: setattr(bus, "closed", True))
    monkeypatch.setitem(sys.modules, "can", SimpleNamespace(Bus=lambda **kwargs: bus))
    monkeypatch.setattr("agentic_hil.can.PythonCanAdapterSession", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("construct failed")))

    result = open_python_can_adapter(config, "bench", config.can_buses["bench"], False)

    assert result["ok"] is False
    assert bus.closed is True


def test_com_double_failure_keeps_raw_handle_reachable_for_retry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from agentic_hil.provisional import provisional_handle_count

    config = load_test_config(tmp_path, com_ports_yaml=COM_PORT_YAML)
    service = ComPortService(config)
    close_calls = {"count": 0}

    def flaky_close() -> None:
        close_calls["count"] += 1
        if close_calls["count"] == 1:
            raise OSError("first close failed")

    handle = SimpleNamespace(close=flaky_close)
    monkeypatch.setitem(sys.modules, "serial", SimpleNamespace(Serial=lambda *args, **kwargs: handle))
    monkeypatch.setattr("agentic_hil.comports.ComPortSession", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("construct failed")))

    result = service._open_serial("dut", config.com_ports["dut"], str(tmp_path / "com.jsonl"), SimpleNamespace())

    assert result["ok"] is False
    assert provisional_handle_count(service.coordinator.owner_marker) == 1
    service.close()
    assert close_calls["count"] == 2
    assert provisional_handle_count(service.coordinator.owner_marker) == 0


def test_can_inner_double_failure_keeps_raw_bus_reachable_for_retry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from agentic_hil.can import open_python_can_adapter
    from agentic_hil.process import managed_process_owner
    from agentic_hil.provisional import cleanup_provisional_handles, provisional_handle_count

    config = load_test_config(tmp_path, can_buses_yaml='can_buses:\n  bench:\n    adapter: "socketcan"\n    channel: "vcan0"\n')
    shutdown_calls = {"count": 0}

    def flaky_shutdown() -> None:
        shutdown_calls["count"] += 1
        if shutdown_calls["count"] == 1:
            raise OSError("first shutdown failed")

    bus = SimpleNamespace(shutdown=flaky_shutdown)
    monkeypatch.setitem(sys.modules, "can", SimpleNamespace(Bus=lambda **kwargs: bus))
    monkeypatch.setattr("agentic_hil.can.PythonCanAdapterSession", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("construct failed")))

    owner = "owner-token-under-test"
    with managed_process_owner(owner):
        result = open_python_can_adapter(config, "bench", config.can_buses["bench"], False)
    assert result["ok"] is False
    assert provisional_handle_count(owner) == 1

    assert cleanup_provisional_handles(owner) == []
    assert shutdown_calls["count"] == 2
    assert provisional_handle_count(owner) == 0


def test_can_outer_session_constructor_failure_closes_adapter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from agentic_hil.provisional import provisional_handle_count

    config = load_test_config(tmp_path, can_buses_yaml='can_buses:\n  bench:\n    adapter: "socketcan"\n    channel: "vcan0"\n')
    service = CanBusService(config)
    adapter = SimpleNamespace(closed=False, adapter_name="fake", status=lambda: {"active": True})
    adapter.close = lambda: setattr(adapter, "closed", True)
    monkeypatch.setattr("agentic_hil.can.open_adapter", lambda *args, **kwargs: {"ok": True, "session": adapter})
    monkeypatch.setattr("agentic_hil.can.CanBusSession", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("outer construct failed")))

    with pytest.raises(RuntimeError, match="outer construct failed"):
        service.session_start("bench", clear_rx_queue=False)

    assert adapter.closed is True
    assert provisional_handle_count(service.coordinator.owner_marker) == 0
    assert service.coordinator.blocked is False
    service.close()


def test_active_can_session_drains_more_than_one_buffer_batch(tmp_path: Path) -> None:
    config = load_test_config(tmp_path, can_buses_yaml='can_buses:\n  bench:\n    adapter: "socketcan"\n    channel: "vcan0"\n    max_buffer_frames: 2\n')
    service = CanBusService(config)

    class QueuedAdapter:
        adapter_name = "fake"

        def __init__(self) -> None:
            self.frames = [1, 2, 3, 4, 5]
            self.reads = 0

        def read(self, max_frames: int, wait_timeout_s: float) -> dict:
            self.reads += 1
            batch = self.frames[:max_frames]
            del self.frames[:max_frames]
            return {"ok": True, "frames": [{"id": item} for item in batch]}

        def status(self) -> dict:
            return {"active": True}

        def close(self) -> dict:
            return {"safe_state_confirmed": True, "process_reaped": True}

    adapter = QueuedAdapter()
    session = CanBusSession("bench", config.can_buses["bench"], adapter, str(tmp_path / "can.jsonl"))
    service.sessions["bench"] = session

    result = service.session_start("bench", clear_rx_queue=True)

    assert result["ok"] is True
    assert result["already_active"] is True
    assert adapter.frames == []
    assert adapter.reads == 4


def test_active_can_session_quarantines_when_queue_never_drains(tmp_path: Path) -> None:
    config = load_test_config(tmp_path, can_buses_yaml='can_buses:\n  bench:\n    adapter: "socketcan"\n    channel: "vcan0"\n    max_buffer_frames: 2\n')
    service = CanBusService(config)

    class BusyAdapter:
        adapter_name = "fake"

        def read(self, max_frames: int, wait_timeout_s: float) -> dict:
            return {"ok": True, "frames": [{"id": 1}, {"id": 2}]}

        def status(self) -> dict:
            return {"active": True}

    session = CanBusSession("bench", config.can_buses["bench"], BusyAdapter(), str(tmp_path / "can-limit.jsonl"))
    service.sessions["bench"] = session

    result = service.session_start("bench", clear_rx_queue=True)

    assert result["error_type"] == "can_queue_clear_limit"
    assert result["frames_drained"] > config.can_buses["bench"].max_buffer_frames
    assert result["side_effect_committed"] is True
    assert result["side_effect_status"] == "partial"
    assert result["lease_state"] == "cleanup_required"


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


@pytest.mark.skipif(os.name == "nt", reason="POSIX select-based reader poll")
def test_stdin_reader_stops_without_external_input_on_posix(tmp_path: Path) -> None:
    from agentic_hil.comstdio import start_stdin_reader, stop_stdin_reader

    read_fd, write_fd = os.pipe()

    class PipeStream:
        def fileno(self) -> int:
            return read_fd

        def read(self, size: int) -> bytes:  # pragma: no cover - fallback path only
            return os.read(read_fd, size)

    reader = start_stdin_reader(PipeStream())
    try:
        # No byte is ever written to the pipe; the reader must still observe the
        # stop event via its poll timeout rather than staying parked in read().
        errors = stop_stdin_reader(reader, 0.5)
        assert errors == []
        assert not reader.thread.is_alive()
    finally:
        os.close(write_fd)
        with suppress(OSError):
            os.close(read_fd)


def test_stdin_reader_start_failure_closes_dup_fd(monkeypatch: pytest.MonkeyPatch) -> None:
    from agentic_hil import comstdio

    closed: list[int] = []
    real_close = os.close
    monkeypatch.setattr(comstdio.os, "dup", lambda fd: 4242)
    monkeypatch.setattr(comstdio.os, "close", lambda fd: closed.append(fd))

    def failing_start(self) -> None:
        raise RuntimeError("thread start failed")

    monkeypatch.setattr(comstdio.threading.Thread, "start", failing_start)

    class FdStream:
        def fileno(self) -> int:
            return 0

    with pytest.raises(RuntimeError, match="thread start failed"):
        comstdio.start_stdin_reader(FdStream())

    assert 4242 in closed, "the dup'd stdin fd must be closed when the reader thread fails to start"
    real_close  # noqa: B018 - keep reference so monkeypatch teardown restores cleanly


def test_com_stdio_completes_cleanup_when_reader_teardown_interrupts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = load_test_config(tmp_path, com_ports_yaml=COM_PORT_YAML)
    events: list[str] = []

    class TrackingService(StubComPortService):
        def read_bytes(self, port_id: str, max_bytes: int, wait_timeout_s: float, tool: str) -> dict:
            # Break the relay loop immediately so the cleanup path is reached.
            return {"ok": False, "error_type": "serial_read_failed"}

        def session_stop(self, port_id: str) -> dict:
            events.append("session_stop")
            return {"ok": True}

        def close(self) -> None:
            events.append("close")

    monkeypatch.setattr("agentic_hil.comstdio.ComPortService", TrackingService)

    def interrupting_stop(reader, timeout_s):
        raise KeyboardInterrupt("reader teardown interrupted")

    monkeypatch.setattr("agentic_hil.comstdio.stop_stdin_reader", interrupting_stop)

    with pytest.raises(KeyboardInterrupt):
        run_com_stdio(config, "dut", input_stream=BlockingStdin(), output_stream=StringIO(), error_stream=StringIO())

    assert events == ["session_stop", "close"], "session/service cleanup must still run after an interrupt in reader teardown"


def test_com_stdio_propagates_stdin_reader_error_after_cleanup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = load_test_config(tmp_path, com_ports_yaml=COM_PORT_YAML)
    monkeypatch.setattr("agentic_hil.comstdio.ComPortService", StubComPortService)

    class ErrorStdin:
        def read(self, size: int) -> bytes:
            raise OSError("stdin failed")

    with pytest.raises(OSError, match="stdin failed"):
        run_com_stdio(config, "dut", input_stream=ErrorStdin(), output_stream=StringIO(), error_stream=StringIO())


def test_com_stdio_cancels_and_joins_blocked_stdin_reader(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = load_test_config(tmp_path, com_ports_yaml=COM_PORT_YAML)

    class FailingReadService(StubComPortService):
        def read_bytes(self, port_id: str, max_bytes: int, wait_timeout_s: float, tool: str) -> dict:
            return {"ok": False, "error_type": "serial_read_failed"}

    class CancelStdin:
        def __init__(self) -> None:
            self.cancelled = threading.Event()
            self.finished = threading.Event()

        def read(self, size: int) -> bytes:
            self.cancelled.wait()
            self.finished.set()
            return b""

        def cancel_read(self) -> None:
            self.cancelled.set()

    stdin = CancelStdin()
    monkeypatch.setattr("agentic_hil.comstdio.ComPortService", FailingReadService)

    result = run_com_stdio(config, "dut", input_stream=stdin, output_stream=StringIO(), error_stream=StringIO())

    assert result == 1
    assert stdin.cancelled.is_set()
    assert stdin.finished.is_set()


def test_spawn_command_preserves_interrupt_when_cleanup_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    child = SimpleNamespace(communicate=lambda timeout: (_ for _ in ()).throw(KeyboardInterrupt("stop")))
    monkeypatch.setattr("agentic_hil.backends.common.spawn_managed_process", lambda *args, **kwargs: child)
    monkeypatch.setattr("agentic_hil.backends.common.terminate_process_tree", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("reap failed")))

    with pytest.raises(KeyboardInterrupt, match="stop") as excinfo:
        spawn_command(["tool"], str(Path.cwd()), 1)

    assert "Cleanup error: reap failed" in str(excinfo.value.args)


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
    assert result["side_effect_committed"] is False


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


def test_bridge_close_propagates_reap_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    child = SimpleNamespace(stdout=[], stderr=[], poll=lambda: 0)
    session = ProcessBridgeSession(child)
    monkeypatch.setattr(
        "agentic_hil.bridge.terminate_process_tree",
        lambda process, timeout: (_ for _ in ()).throw(subprocess.TimeoutExpired("bridge", timeout)),
    )

    with pytest.raises(BridgeCleanupError) as excinfo:
        session.close()

    assert session.closed is False
    assert excinfo.value.result["process_reaped"] is False


def test_adapter_service_retains_session_after_bridge_reap_failure(tmp_path: Path) -> None:
    adapters_yaml = f'adapters:\n  ntc:\n    executable: "{SIM_NTC_ADAPTER.as_posix()}"\n    channels: ["temperature"]\n'
    config = load_test_config(tmp_path, adapters_yaml=adapters_yaml)
    service = AdapterService(config)

    def fail_close() -> None:
        raise subprocess.TimeoutExpired("bridge", 1)

    bridge = SimpleNamespace(close=fail_close, status=lambda: {"active": True})
    lease = SimpleNamespace(state="active", audit_ok=True)

    def quarantine(*_args, **_kwargs) -> None:
        lease.state = "cleanup_required"

    lease.quarantine = quarantine
    lease.status = lambda: {"lease_status": lease.state, "cleanup_required": lease.state != "active"}
    session = SimpleNamespace(
        adapter_id="ntc",
        adapter_config=config.adapters["ntc"],
        bridge=bridge,
        log_path=str(tmp_path / ".agentic-hil" / "logs" / "adapter.jsonl"),
        started_at="now",
        active=True,
        audit_broken=False,
        lease=lease,
        safe_state_confirmed=False,
        process_reaped=False,
    )
    service.sessions["ntc"] = session

    result = service.session_stop("ntc")

    assert result["ok"] is False
    assert result["error_type"] == "adapter_bridge_close_failed"
    assert service.sessions["ntc"] is session
    assert session.active is False
    assert result["cleanup_required"] is True
    service.sessions.clear()


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


def test_flash_requires_reset_permission_only_when_requested(tmp_path: Path) -> None:
    service = AgenticHILToolService(
        load_test_config(tmp_path, permissions_yaml="permissions:\n  allow_flash: true\n  allow_reset: false\n")
    )

    without_reset = service.call("flash_firmware", {"image_path": "build/app.elf", "reset_after_flash": False})
    with_reset = service.call("flash_firmware", {"image_path": "build/app.elf", "reset_after_flash": True})

    assert without_reset["error_type"] != "permission_denied"
    assert with_reset["error_type"] == "permission_denied"


PERMISSION_GATE_CASES = [
    ("allow_probe", "probe_target", {}),
    ("allow_probe", "debugger_info", {}),
    ("allow_flash", "flash_firmware", {"image_path": "build/app.elf"}),
    ("allow_reset", "reset_target", {"mode": "run"}),
    ("allow_com_read", "com_session_start", {"port_id": "dut"}),
    ("allow_com_write", "com_write", {"port_id": "dut", "text": "hi"}),
    ("allow_can_read", "can_read", {"bus_id": "bench"}),
    ("allow_can_write", "can_send", {"bus_id": "bench", "frame_id": 1, "data_hex": "00"}),
]


@pytest.mark.parametrize(("flag", "tool", "arguments"), PERMISSION_GATE_CASES)
def test_disabled_permission_blocks_tool(tmp_path: Path, flag: str, tool: str, arguments: dict) -> None:
    config = load_test_config(
        tmp_path,
        com_ports_yaml=COM_PORT_YAML,
        can_buses_yaml=CAN_BUS_YAML,
        permissions_yaml=f"permissions:\n  {flag}: false\n",
    )
    service = AgenticHILToolService(config)

    result = service.call(tool, arguments)

    assert result["ok"] is False, f"{tool} must be blocked when {flag} is false"
    assert result["error_type"] == "permission_denied"


def test_startup_config_is_frozen_when_file_permissions_change(tmp_path: Path) -> None:
    config_path = write_config(tmp_path, permissions_yaml="permissions:\n  allow_probe: false\n")
    service = AgenticHILToolService(load_config(str(config_path)))
    try:
        config_text = config_path.read_text(encoding="utf-8")
        config_path.write_text(config_text.replace("allow_probe: false", "allow_probe: true"), encoding="utf-8")

        result = service.call("probe_target")
    finally:
        service.close()

    assert result["ok"] is False
    assert result["error_type"] == "permission_denied"


def test_startup_config_is_frozen_when_file_resources_change(tmp_path: Path) -> None:
    config_path = write_config(tmp_path)
    service = AgenticHILToolService(load_config(str(config_path)))
    try:
        config_text = config_path.read_text(encoding="utf-8")
        config_path.write_text(config_text.replace("com_ports: {}", COM_PORT_YAML.rstrip()), encoding="utf-8")

        result = service.call("com_ports_list")
    finally:
        service.close()

    assert result["ok"] is True
    assert "dut" not in result["ports"]


def test_authoritative_config_falls_back_to_canonical_project_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "workspace"
    config_path = write_authoritative_config(workspace, monkeypatch, permissions_yaml="permissions: {}\n")
    monkeypatch.delenv("AGENTIC_HIL_CONFIG")

    config = load_authoritative_config(workspace)

    assert config.config_path == str(config_path)


def test_authoritative_config_env_must_be_absolute(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTIC_HIL_CONFIG", "relative/config.yaml")

    with pytest.raises(ConfigError) as rejected:
        load_authoritative_config(tmp_path)

    assert rejected.value.error_type == "config_invalid"
    assert rejected.value.details["environment_variable"] == "AGENTIC_HIL_CONFIG"


def test_authoritative_config_accepts_absolute_external_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    config_path = write_config(
        workspace,
        config_path=tmp_path / "managed" / "bench.yaml",
        permissions_yaml="permissions: {}\n",
    )
    monkeypatch.setenv("AGENTIC_HIL_CONFIG", str(config_path.resolve()))

    config = load_authoritative_config(workspace)

    assert config.config_path == str(config_path.resolve())


def test_authoritative_config_must_be_outside_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_root = tmp_path / "user-config"
    monkeypatch.setenv("APPDATA", str(config_root))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_root))
    workspace = config_root / "agentic-hil" / "projects" / "inside" / "workspace"
    config_path = write_config(workspace, config_path=workspace / "config.yaml")
    monkeypatch.setenv("AGENTIC_HIL_CONFIG", str(config_path.resolve()))

    with pytest.raises(ConfigError) as rejected:
        load_authoritative_config(workspace)

    assert rejected.value.error_type == "config_invalid"
    assert rejected.value.details["workspace_root"] == str(workspace.resolve())


def test_authoritative_config_requires_exact_workspace_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    other = tmp_path / "other"
    other.mkdir()
    write_authoritative_config(workspace, monkeypatch, permissions_yaml="permissions: {}\n")

    with pytest.raises(ConfigError) as rejected:
        load_authoritative_config(other)

    assert rejected.value.error_type == "config_invalid"
    assert rejected.value.details["workspace_root"] == str(workspace.resolve())
    assert rejected.value.details["expected_workspace"] == str(other.resolve())


def test_authoritative_config_override_need_not_use_canonical_workspace_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "workspace"
    canonical = write_authoritative_config(workspace, monkeypatch, permissions_yaml="permissions: {}\n")
    alternate = canonical.parent.parent / "alternate" / "config.yaml"
    alternate.parent.mkdir()
    alternate.write_text(canonical.read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setenv("AGENTIC_HIL_CONFIG", str(alternate))

    config = load_authoritative_config(workspace)

    assert config.config_path == str(alternate.resolve())


def test_raw_config_requires_workspace_root(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("target: {}\n", encoding="utf-8")

    with pytest.raises(ConfigError) as rejected:
        load_config(str(config_path))

    assert rejected.value.error_type == "config_migration_required"
    assert "migrate-config" in rejected.value.details["next_step"]


def test_raw_config_rejects_duplicate_keys(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        f"workspace_root: {str(tmp_path.resolve())!r}\npermissions: {{}}\npermissions: {{}}\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError) as rejected:
        load_config(str(config_path))

    assert rejected.value.error_type == "config_invalid"
    assert "valid YAML" in rejected.value.summary


@pytest.mark.parametrize("section", ["devices", "com_ports", "can_buses", "adapters"])
@pytest.mark.parametrize("non_string_key", ["1", "true", "2026-07-18"])
def test_raw_config_rejects_non_string_mapping_keys(tmp_path: Path, section: str, non_string_key: str) -> None:
    config_path = write_config(tmp_path)
    text = config_path.read_text(encoding="utf-8")
    config_path.write_text(text.replace(f"{section}: {{}}", f"{section}:\n  {non_string_key}: {{}}"), encoding="utf-8")

    with pytest.raises(ConfigError) as rejected:
        load_config(str(config_path))

    assert rejected.value.error_type == "config_invalid"
    assert rejected.value.details["path"] == str(config_path.resolve())
    assert rejected.value.details["line"] > 0
    assert rejected.value.details["column"] > 0
    assert "backend_error" not in rejected.value.details


def test_authoritative_config_pins_external_can_bridge(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "workspace"
    write_authoritative_config(
        workspace,
        monkeypatch,
        can_buses_yaml=(
            'can_buses:\n  bench:\n    adapter: "process"\n    channel: "test"\n'
            f'    executable: "{FAKE_OPENOCD.as_posix()}"\n'
        ),
        permissions_yaml="permissions: {}\n",
    )

    config = load_authoritative_config(workspace)

    assert config.can_buses["bench"].executable == str(FAKE_OPENOCD.resolve())


def test_authoritative_config_rejects_workspace_can_bridge_executable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "workspace"
    bridge = workspace / "bridge.py"
    bridge.parent.mkdir(parents=True)
    bridge.write_text("print('untrusted')\n", encoding="utf-8")
    write_authoritative_config(
        workspace,
        monkeypatch,
        can_buses_yaml=f'can_buses:\n  injected:\n    adapter: "process"\n    channel: "test"\n    executable: "{bridge.as_posix()}"\n',
        permissions_yaml="permissions: {}\n",
    )

    with pytest.raises(ConfigError) as rejected:
        load_authoritative_config(workspace)

    assert rejected.value.error_type == "config_invalid"
    assert rejected.value.details["field"] == "can_buses.injected.executable"


def test_authoritative_config_rejects_process_bridge_arguments(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "workspace"
    bridge = workspace / "bridge.py"
    bridge.parent.mkdir(parents=True)
    bridge.write_text("print('untrusted')\n", encoding="utf-8")
    write_authoritative_config(
        workspace,
        monkeypatch,
        can_buses_yaml=(
            'can_buses:\n  injected:\n    adapter: "process"\n    channel: "test"\n'
            f'    executable: "{FAKE_OPENOCD.as_posix()}"\n    args: ["{bridge.as_posix()}"]\n'
        ),
        permissions_yaml="permissions: {}\n",
    )

    with pytest.raises(ConfigError) as rejected:
        load_authoritative_config(workspace)

    assert rejected.value.error_type == "config_migration_required"
    assert rejected.value.details["field"] == "can_buses.injected.args"


def test_authoritative_config_pins_external_adapter_bridge(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "workspace"
    write_authoritative_config(
        workspace,
        monkeypatch,
        adapters_yaml=(
            f'adapters:\n  ntc:\n    executable: "{SIM_NTC_ADAPTER.as_posix()}"\n'
            '    channels: ["temperature"]\n    faults: ["open"]\n'
        ),
        permissions_yaml="permissions: {}\n",
    )

    config = load_authoritative_config(workspace)

    assert config.adapters["ntc"].executable == str(SIM_NTC_ADAPTER.resolve())


def test_authoritative_config_rejects_workspace_adapter_bridge(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "workspace"
    bridge = workspace / "adapter.py"
    bridge.parent.mkdir(parents=True)
    bridge.write_text("print('untrusted')\n", encoding="utf-8")
    write_authoritative_config(
        workspace,
        monkeypatch,
        adapters_yaml=f'adapters:\n  injected:\n    executable: "{bridge.as_posix()}"\n',
        permissions_yaml="permissions: {}\n",
    )

    with pytest.raises(ConfigError) as rejected:
        load_authoritative_config(workspace)

    assert rejected.value.error_type == "config_invalid"
    assert rejected.value.details["field"] == "adapters.injected.executable"


def test_authoritative_config_rejects_adapter_bridge_arguments(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "workspace"
    write_authoritative_config(
        workspace,
        monkeypatch,
        adapters_yaml=(
            f'adapters:\n  injected:\n    executable: "{SIM_NTC_ADAPTER.as_posix()}"\n'
            '    args: ["workspace-script.py"]\n'
        ),
        permissions_yaml="permissions: {}\n",
    )

    with pytest.raises(ConfigError) as rejected:
        load_authoritative_config(workspace)

    assert rejected.value.error_type == "config_migration_required"
    assert rejected.value.details["field"] == "adapters.injected.args"


def test_authoritative_config_pins_debugger_and_openocd_scripts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "workspace"
    host = tmp_path / "tools"
    interface_cfg = host / "trusted-interface.cfg"
    target_cfg = host / "trusted-target.cfg"
    interface_cfg.parent.mkdir(parents=True)
    interface_cfg.write_text("# trusted interface\n", encoding="utf-8")
    target_cfg.write_text("# trusted target\n", encoding="utf-8")
    config_path = write_authoritative_config(
        workspace,
        monkeypatch,
        debugger_executable=FAKE_OPENOCD,
        permissions_yaml="permissions:\n  allow_probe: true\n",
    )
    config_text = config_path.read_text(encoding="utf-8")
    config_path.write_text(
        config_text.replace((config_path.parent / "interface.cfg").as_posix(), interface_cfg.as_posix()).replace(
            (config_path.parent / "target.cfg").as_posix(), target_cfg.as_posix()
        ),
        encoding="utf-8",
    )

    config = load_authoritative_config(workspace)

    assert config.debugger.executable == str(FAKE_OPENOCD.resolve())
    assert config.debugger.interface_cfg == str(interface_cfg.resolve())
    assert config.debugger.target_cfg == str(target_cfg.resolve())


def test_authoritative_config_rejects_relative_openocd_scripts_when_debugger_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    config_path = write_authoritative_config(
        workspace,
        monkeypatch,
        debugger_executable=FAKE_OPENOCD,
        permissions_yaml="permissions:\n  allow_probe: true\n",
    )
    text = config_path.read_text(encoding="utf-8")
    config_path.write_text(
        text.replace((config_path.parent / "interface.cfg").as_posix(), "interface/stlink.cfg"), encoding="utf-8"
    )

    with pytest.raises(ConfigError) as rejected:
        load_authoritative_config(workspace)

    assert rejected.value.error_type == "config_invalid"
    assert rejected.value.details["field"] == "debugger.interface_cfg"


def test_artifact_root_symlink_cannot_pivot_after_startup(tmp_path: Path) -> None:
    config = load_test_config(tmp_path)
    service = AgenticHILToolService(config)
    outside = tmp_path / "outside-artifacts"
    outside.mkdir()
    (outside / "firmware.elf").write_bytes(b"\x7fELFfake")
    try:
        (tmp_path / "build").symlink_to(outside, target_is_directory=True)
    except OSError as error:
        service.close()
        pytest.skip(f"directory symlinks unavailable: {error}")

    try:
        result = service.call("flash_firmware", {"image_path": "build/firmware.elf"})
    finally:
        service.close()

    assert result["ok"] is False
    assert result["error_type"] == "artifact_validation_failed"
    assert result["validation"]["allowed_root"] is True
    assert result["validation"]["regular_file"] is False


def test_report_directory_symlink_is_rejected(tmp_path: Path) -> None:
    config = load_test_config(tmp_path)
    service = AgenticHILToolService(config)
    outside = tmp_path / "outside-reports"
    outside.mkdir()
    try:
        (tmp_path / ".agentic-hil" / "reports").symlink_to(outside, target_is_directory=True)
    except OSError as error:
        service.close()
        pytest.skip(f"directory symlinks unavailable: {error}")

    try:
        result = service.call("get_last_report")
    finally:
        service.close()

    assert result["ok"] is False
    assert result["error_type"] == "report_not_found"


def test_last_report_file_symlink_is_rejected_without_reading_target(tmp_path: Path) -> None:
    config = load_test_config(tmp_path)
    reports = tmp_path / ".agentic-hil" / "reports"
    reports.mkdir(parents=True)
    outside = tmp_path / "outside-report.json"
    outside.write_text('{"secret": "must-not-be-read"}\n', encoding="utf-8")
    report_path = reports / "last-report.json"
    try:
        report_path.symlink_to(outside)
    except OSError as error:
        pytest.skip(f"file symlinks unavailable: {error}")
    service = AgenticHILToolService(config)
    try:
        result = service.call("get_last_report")
    finally:
        service.close()

    assert result["ok"] is False
    assert result["error_type"] == "report_not_found"
    assert "must-not-be-read" not in json.dumps(result)


def test_parallel_jsonl_appends_keep_every_event_exactly_once(tmp_path: Path) -> None:
    log_path = tmp_path / "events.jsonl"

    with ThreadPoolExecutor(max_workers=32) as executor:
        list(executor.map(lambda event_id: append_jsonl(str(log_path), {"event_id": event_id}), range(1000)))

    events = [json.loads(line)["event_id"] for line in log_path.read_text(encoding="utf-8").splitlines()]
    assert len(events) == 1000
    assert sorted(events) == list(range(1000))


def test_report_path_is_not_claimed_when_canonical_state_write_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = load_test_config(tmp_path)
    monkeypatch.setattr("agentic_hil.report.write_report_state", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("state denied")))

    result = write_report(config, {"ok": True, "tool": "probe_target"})

    assert result["audit_ok"] is False
    assert "report_path" not in result


def test_report_path_is_not_claimed_when_workspace_snapshot_write_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from agentic_hil import report as report_module

    config = load_test_config(tmp_path)
    original_write = report_module.safe_write_text

    def fail_snapshot(config, path, text, **kwargs):
        if Path(path).name == "last-report.json":
            raise OSError("snapshot denied")
        return original_write(config, path, text, **kwargs)

    monkeypatch.setattr("agentic_hil.report.safe_write_text", fail_snapshot)

    result = write_report(config, {"ok": True, "tool": "probe_target"})

    assert result["audit_ok"] is False
    assert "report_path" not in result


def test_path_lock_registry_does_not_keep_short_lived_paths(tmp_path: Path) -> None:
    _PATH_LOCKS.clear()

    for index in range(100):
        assert append_jsonl(str(tmp_path / f"event-{index}.jsonl"), {"event_id": index}) is None

    assert _PATH_LOCKS == {}


@pytest.mark.skipif(os.name != "nt", reason="Windows directory-handle behavior")
def test_windows_secure_io_handles_block_parent_rename(tmp_path: Path) -> None:
    parent = tmp_path / "audit"
    parent.mkdir()
    handles = _windows_hold_directory_chain(parent)
    try:
        with pytest.raises(OSError):
            os.replace(parent, tmp_path / "replacement")
    finally:
        _close_windows_handles(handles)


def test_can_close_failure_retains_session_for_retry(tmp_path: Path) -> None:
    config = load_test_config(tmp_path, can_buses_yaml=CAN_BUS_YAML)

    class RetryAdapter:
        adapter_name = "process"

        def __init__(self) -> None:
            self.active = True
            self.attempts = 0

        def close(self) -> None:
            self.attempts += 1
            if self.attempts == 1:
                raise OSError("bridge busy")
            self.active = False

        def status(self) -> dict:
            return {"active": self.active}

    adapter = RetryAdapter()
    log_path = tmp_path / ".agentic-hil" / "logs" / "can.jsonl"
    log_path.parent.mkdir(parents=True)
    session = CanBusSession("bench", config.can_buses["bench"], adapter, str(log_path))  # type: ignore[arg-type]
    service = CanBusService(config)
    service.sessions["bench"] = session

    first = service.session_stop("bench")
    second = service.session_stop("bench")

    assert first["error_type"] == "can_adapter_close_failed"
    assert second["ok"] is True
    assert adapter.attempts == 2
    assert "bench" not in service.sessions


def test_unavailable_audit_prevents_hardware_action(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    backend = SimpleNamespace(
        probe_target=lambda: calls.append("probe") or {"ok": True, "tool": "probe_target"},
        close=lambda: None,
    )
    service = AgenticHILToolService(load_test_config(tmp_path), backend=backend)
    monkeypatch.setattr("agentic_hil.report.safe_write_text", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk full")))
    try:
        result = service.call("probe_target")
    finally:
        service.close()

    assert result["ok"] is False
    assert result["error_type"] == "audit_unavailable"
    assert result["side_effect_committed"] is False
    assert calls == []


def test_com_log_path_failure_does_not_open_serial(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    opened: list[str] = []

    def serial_open(*args, **kwargs):
        opened.append("serial")
        return SimpleNamespace(is_open=True, close=lambda: None)

    monkeypatch.setitem(sys.modules, "serial", SimpleNamespace(Serial=serial_open))
    monkeypatch.setattr("agentic_hil.comports.logs_directory", lambda config: (_ for _ in ()).throw(ConfigError("unsafe_configured_path", "bad log path")))
    service = ComPortService(load_test_config(tmp_path, com_ports_yaml=COM_PORT_YAML))

    result = service.session_start("dut")

    assert result["error_type"] == "audit_unavailable"
    assert opened == []
    assert service.sessions == {}


def test_can_log_path_failure_does_not_open_adapter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    opened: list[str] = []

    monkeypatch.setattr("agentic_hil.can.logs_directory", lambda config: (_ for _ in ()).throw(ConfigError("unsafe_configured_path", "bad log path")))
    monkeypatch.setattr("agentic_hil.can.open_adapter", lambda *args, **kwargs: opened.append("adapter") or {"ok": True})
    service = CanBusService(load_test_config(tmp_path, can_buses_yaml=CAN_BUS_YAML))

    result = service.session_start("bench")

    assert result["error_type"] == "audit_unavailable"
    assert opened == []
    assert service.sessions == {}


def test_adapter_log_path_failure_does_not_spawn_bridge(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    opened: list[str] = []
    adapters_yaml = f'adapters:\n  ntc:\n    executable: "{SIM_NTC_ADAPTER.as_posix()}"\n    channels: ["temperature"]\n'

    monkeypatch.setattr("agentic_hil.adapters.logs_directory", lambda config: (_ for _ in ()).throw(ConfigError("unsafe_configured_path", "bad log path")))
    monkeypatch.setattr("agentic_hil.adapters.open_adapter_bridge", lambda *args, **kwargs: opened.append("bridge") or {"ok": True})
    service = AdapterService(load_test_config(tmp_path, adapters_yaml=adapters_yaml))

    result = service.session_start("ntc")

    assert result["error_type"] == "audit_unavailable"
    assert opened == []
    assert service.sessions == {}


def test_post_action_report_failure_preserves_committed_flash_result(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    firmware = tmp_path / "build" / "firmware.elf"
    firmware.parent.mkdir()
    firmware.write_bytes(b"\x7fELFfake")
    config = load_test_config(tmp_path, debugger_type="stlink")
    service = AgenticHILToolService(config)
    from agentic_hil import report as report_module

    original_write = report_module.safe_write_text

    def fail_last_report(config, path, text, **kwargs):
        if Path(path).name == "last-report.json":
            raise OSError("disk full")
        return original_write(config, path, text, **kwargs)

    monkeypatch.setattr(report_module, "safe_write_text", fail_last_report)
    try:
        result = service.call("flash_firmware", {"image_path": "build/firmware.elf"})
    finally:
        service.close()

    assert result["ok"] is True
    assert result["side_effect_committed"] is True
    assert result["audit_ok"] is False
    assert result["retry_safe"] is False
    assert result["audit_error"]["backend_error"] == "disk full"


def test_gdbmi_close_always_runs_process_tree_cleanup(monkeypatch: pytest.MonkeyPatch) -> None:
    child = SimpleNamespace(poll=lambda: 0, wait=lambda timeout: 0)
    client = object.__new__(GdbMiClient)
    client.child = child
    client.exited = threading.Event()
    client.exited.set()
    calls: list[object] = []
    monkeypatch.setattr("agentic_hil.gdbmi.terminate_process_tree", lambda process, timeout: calls.append(process))

    client.close(0.1)

    assert calls == [child]


def test_debug_server_cleanup_runs_after_leader_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    child = SimpleNamespace(poll=lambda: 0)
    sessions = object.__new__(GdbDebugSessions)
    sessions._audit_broken = None
    sessions._write_session_log = lambda session: None
    session = SimpleNamespace(gdb=None, server=child)
    calls: list[object] = []
    monkeypatch.setattr("agentic_hil.backends.gdbdebug.terminate_process_tree", lambda process, timeout: calls.append(process))

    result = sessions._cleanup_session(session, 0.1)

    assert result is None
    assert calls == [child]


def test_audit_ready_rejects_last_failure_symlink(tmp_path: Path) -> None:
    config = load_test_config(tmp_path)
    reports = tmp_path / ".agentic-hil" / "reports"
    reports.mkdir(parents=True)
    outside = tmp_path / "outside-last-failure.json"
    outside.write_text("{}\n", encoding="utf-8")
    target = reports / "last-failure.json"
    try:
        target.symlink_to(outside)
    except OSError as error:
        pytest.skip(f"file symlinks unavailable: {error}")

    with pytest.raises(ConfigError) as rejected:
        ensure_audit_ready(config)

    assert rejected.value.error_type == "unsafe_configured_path"


def test_audit_ready_rejects_last_failure_hardlink(tmp_path: Path) -> None:
    config = load_test_config(tmp_path)
    reports = tmp_path / ".agentic-hil" / "reports"
    reports.mkdir(parents=True)
    outside = tmp_path / "outside-last-failure.json"
    outside.write_text("{}\n", encoding="utf-8")
    target = reports / "last-failure.json"
    try:
        os.link(outside, target)
    except OSError as error:
        pytest.skip(f"hard links unavailable: {error}")

    with pytest.raises(ConfigError) as rejected:
        ensure_audit_ready(config)

    assert rejected.value.error_type == "unsafe_configured_path"


@pytest.mark.skipif(os.name == "nt", reason="POSIX FIFO regression")
def test_audit_ready_rejects_fifo_before_hardware_runs(tmp_path: Path) -> None:
    config = load_test_config(tmp_path)
    reports = tmp_path / ".agentic-hil" / "reports"
    reports.mkdir(parents=True)
    os.mkfifo(reports / "last-failure.json")
    calls: list[str] = []
    backend = SimpleNamespace(probe_target=lambda: calls.append("hardware") or {"ok": True}, close=lambda: None)
    service = AgenticHILToolService(config, backend=backend)
    try:
        result = service.call("probe_target")
    finally:
        service.close()

    assert result["error_type"] == "audit_unavailable"
    assert result["audit_ok"] is False
    assert calls == []


def test_report_pair_lock_keeps_last_files_on_same_operation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = load_test_config(tmp_path)
    from agentic_hil import report as report_module

    original_write = report_module.safe_write_text
    first_failure_started = threading.Event()
    release_first = threading.Event()
    second_write_started = threading.Event()

    def controlled_write(config, path, text, **kwargs):
        payload = json.loads(text)
        if payload["operation"] == "A" and Path(path).name == "last-failure.json":
            first_failure_started.set()
            assert release_first.wait(5)
        if payload["operation"] == "B":
            second_write_started.set()
        return original_write(config, path, text, **kwargs)

    monkeypatch.setattr(report_module, "safe_write_text", controlled_write)
    failures: list[Exception] = []

    def write(operation: str) -> None:
        try:
            write_report(config, {"ok": False, "operation": operation, "error_type": "test_failure"})
        except Exception as error:
            failures.append(error)

    first = threading.Thread(target=write, args=("A",))
    second = threading.Thread(target=write, args=("B",))
    first.start()
    assert first_failure_started.wait(5)
    second.start()
    assert not second_write_started.wait(0.1)
    release_first.set()
    first.join(5)
    second.join(5)

    assert failures == []
    assert not first.is_alive() and not second.is_alive()
    reports = tmp_path / ".agentic-hil" / "reports"
    assert json.loads((reports / "last-report.json").read_text(encoding="utf-8"))["operation"] == "B"
    assert json.loads((reports / "last-failure.json").read_text(encoding="utf-8"))["operation"] == "B"


def test_canonical_report_state_and_lock_are_outside_workspace(tmp_path: Path) -> None:
    config = load_test_config(tmp_path)

    result = write_report(config, {"ok": False, "operation": "A", "error_type": "test_failure"})

    assert result["audit_ok"] is True
    assert not Path(report_state_path(config)).is_relative_to(tmp_path)
    assert not Path(report_lock_path(config)).is_relative_to(tmp_path)


def test_canonical_report_state_ignores_mismatched_compatibility_snapshots(tmp_path: Path) -> None:
    config = load_test_config(tmp_path)
    write_report(config, {"ok": False, "operation": "A", "error_type": "test_failure"})
    reports = tmp_path / ".agentic-hil" / "reports"
    reports.joinpath("last-report.json").write_text('{"ok": false, "operation": "B"}\n', encoding="utf-8")
    reports.joinpath("last-failure.json").write_text('{"ok": false, "operation": "B"}\n', encoding="utf-8")

    assert read_last_report(config)["operation"] == "A"
    assert read_last_failure(config)["operation"] == "A"


def test_second_snapshot_failure_is_recorded_atomically_in_canonical_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = load_test_config(tmp_path)
    from agentic_hil import report as report_module

    original_write = report_module.safe_write_text

    def fail_last_report(config, path, text, **kwargs):
        if Path(path).name == "last-report.json":
            raise OSError("disk full")
        return original_write(config, path, text, **kwargs)

    monkeypatch.setattr(report_module, "safe_write_text", fail_last_report)

    result = write_report(config, {"ok": False, "operation": "B", "error_type": "test_failure"})

    assert result["audit_ok"] is False
    assert read_last_report(config)["operation"] == "B"
    assert read_last_report(config)["audit_ok"] is False
    assert read_last_failure(config)["operation"] == "B"
    assert read_last_failure(config)["audit_ok"] is False


def test_last_failure_write_failure_does_not_persist_successful_last_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = load_test_config(tmp_path)
    from agentic_hil import report as report_module

    original_write = report_module.safe_write_text

    def fail_last_failure(config, path, text, **kwargs):
        if Path(path).name == "last-failure.json":
            raise OSError("fsync failed")
        return original_write(config, path, text, **kwargs)

    monkeypatch.setattr(report_module, "safe_write_text", fail_last_failure)

    result = write_report(config, {"ok": False, "tool": "probe_target", "error_type": "probe_failed"})

    assert result["audit_ok"] is False
    assert not (tmp_path / ".agentic-hil" / "reports" / "last-report.json").exists()


def test_hardlinked_artifact_is_rejected(tmp_path: Path) -> None:
    config = load_test_config(tmp_path)
    outside = tmp_path / "outside" / "firmware.elf"
    outside.parent.mkdir()
    outside.write_bytes(b"\x7fELFoutside")
    linked = tmp_path / "build" / "firmware.elf"
    linked.parent.mkdir()
    try:
        os.link(outside, linked)
    except OSError as error:
        pytest.skip(f"hard links unavailable: {error}")

    manager = ArtifactManager(config)
    try:
        result = manager.validate_local_path("build/firmware.elf")
    finally:
        manager.close()

    assert result["ok"] is False
    assert result["error_type"] == "artifact_validation_failed"
    assert result["validation"]["single_link"] is False


def test_artifact_is_rechecked_and_staged_before_backend_use(tmp_path: Path) -> None:
    config = load_test_config(tmp_path)
    firmware = tmp_path / "build" / "firmware.elf"
    firmware.parent.mkdir()
    firmware.write_bytes(b"\x7fELForiginal")
    manager = ArtifactManager(config)
    try:
        validation = manager.validate_local_path("build/firmware.elf")
        assert validation["ok"] is True
        firmware.write_bytes(b"\x7fELFchanged")

        changed = manager.stage_for_backend(validation["artifact"], "flash_firmware")

        assert changed["ok"] is False
        assert changed["error_type"] == "artifact_changed"
        assert list(Path(manager._staging.name).iterdir()) == []

        validation = manager.validate_local_path("build/firmware.elf")
        staged = manager.stage_for_backend(validation["artifact"], "flash_firmware")
        assert staged["ok"] is True
        staged_path = Path(staged["artifact"]["resolved_path"])
        assert not staged_path.is_relative_to(tmp_path)
        assert staged_path.read_bytes() == firmware.read_bytes()
    finally:
        manager.close()


def test_backend_staging_rejects_artifact_above_limit(tmp_path: Path) -> None:
    config = load_test_config(tmp_path)
    firmware = tmp_path / "build" / "firmware.bin"
    firmware.parent.mkdir()
    firmware.write_bytes(b"x" * (1024 * 1024 + 1))
    manager = ArtifactManager(config)
    try:
        validation = manager.validate_local_path("build/firmware.bin")
    finally:
        manager.close()

    assert validation["ok"] is False
    assert validation["tool"] == "flash_firmware"
    assert validation["error_type"] == "artifact_too_large"


def test_backend_staging_io_error_is_structured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = load_test_config(tmp_path)
    firmware = tmp_path / "build" / "firmware.bin"
    firmware.parent.mkdir()
    firmware.write_bytes(b"approved")
    manager = ArtifactManager(config)
    validation = manager.validate_local_path("build/firmware.bin")
    monkeypatch.setattr("agentic_hil.artifacts.tempfile.mkdtemp", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk full")))
    try:
        result = manager.stage_for_backend(validation["artifact"], "debug_start_session")
    finally:
        manager.close()

    assert result["ok"] is False
    assert result["tool"] == "debug_start_session"
    assert result["error_type"] == "artifact_staging_failed"
    assert result["backend_error"] == "disk full"


def test_local_artifact_upload_rechecks_source_before_reading(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = load_test_config(tmp_path)
    firmware = tmp_path / "build" / "firmware.bin"
    firmware.parent.mkdir()
    firmware.write_bytes(b"approved")
    manager = ArtifactManager(config)
    validate = manager.validate_local_path

    def replace_after_validation(path: str):
        result = validate(path)
        firmware.write_bytes(b"replacement")
        return result

    monkeypatch.setattr(manager, "validate_local_path", replace_after_validation)
    try:
        result = manager._upload_local_path("build/firmware.bin")
    finally:
        manager.close()

    assert result["ok"] is False
    assert result["error_type"] == "artifact_changed"


def test_upload_does_not_remove_existing_backend_stage(tmp_path: Path) -> None:
    config = load_test_config(tmp_path)
    firmware = tmp_path / "build" / "firmware.bin"
    firmware.parent.mkdir()
    firmware.write_bytes(b"approved")
    manager = ArtifactManager(config)
    try:
        validation = manager.validate_local_path("build/firmware.bin")
        staged = manager.stage_for_backend(validation["artifact"], "debug_start_session")
        staged_path = Path(staged["artifact"]["resolved_path"])

        uploaded = manager._upload_local_path("build/firmware.bin")

        assert uploaded["ok"] is True
        assert staged_path.read_bytes() == b"approved"
    finally:
        manager.close()


def test_artifact_stage_rejects_ancestor_symlink_pivot(tmp_path: Path) -> None:
    config = load_test_config(tmp_path)
    firmware = tmp_path / "build" / "firmware.elf"
    firmware.parent.mkdir()
    firmware.write_bytes(b"\x7fELFsame-content")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / firmware.name).write_bytes(firmware.read_bytes())
    manager = ArtifactManager(config)
    try:
        validation = manager.validate_local_path("build/firmware.elf")
        original_build = tmp_path / "original-build"
        firmware.parent.rename(original_build)
        try:
            (tmp_path / "build").symlink_to(outside, target_is_directory=True)
        except OSError as error:
            pytest.skip(f"directory symlinks unavailable: {error}")

        staged = manager.stage_for_backend(validation["artifact"], "flash_firmware")

        assert staged["ok"] is False
        assert staged["error_type"] == "artifact_changed"
    finally:
        manager.close()


def test_flash_releases_private_backend_stage(tmp_path: Path) -> None:
    config = load_test_config(tmp_path)
    firmware = tmp_path / "build" / "firmware.elf"
    firmware.parent.mkdir()
    firmware.write_bytes(b"\x7fELFfake")
    service = AgenticHILToolService(config)
    staging_root = Path(service.artifacts._staging.name)
    try:
        result = service.call("flash_firmware", {"image_path": "build/firmware.elf"})

        assert result["ok"] is True
        assert list(staging_root.iterdir()) == []
    finally:
        service.close()


def test_canonical_audit_log_written_under_state_root(tmp_path: Path) -> None:
    config = load_test_config(tmp_path, com_ports_yaml=COM_PORT_YAML)
    workspace_log = str(Path(logs_directory(config)) / "com-effect.jsonl")

    assert append_jsonl(workspace_log, {"direction": "tx", "hex": "01"}, config) is None

    canonical = canonical_audit_log_path(config, workspace_log)
    assert canonical.exists()
    workspace_root = Path(config.workspace_root).resolve()
    assert workspace_root not in canonical.resolve().parents
    evidence = canonical_audit_evidence(config, workspace_log)
    assert evidence["canonical_log_present"] is True
    assert evidence["log_sequence"] == 1
    assert evidence["workspace_log_verified"] is True


def test_workspace_audit_log_tampering_is_detected(tmp_path: Path) -> None:
    config = load_test_config(tmp_path, com_ports_yaml=COM_PORT_YAML)
    workspace_log = str(Path(logs_directory(config)) / "com-effect.jsonl")
    assert append_jsonl(workspace_log, {"direction": "tx", "hex": "0a0b"}, config) is None
    canonical = canonical_audit_log_path(config, workspace_log)
    canonical_before = canonical.read_bytes()

    # The agent is assumed able to edit any workspace file; rewrite the log.
    Path(workspace_log).write_text('{"direction": "tx", "hex": "ffff"}\n', encoding="utf-8")
    tampered = canonical_audit_evidence(config, workspace_log)
    assert tampered["canonical_log_present"] is True
    assert tampered["workspace_log_verified"] is False
    assert canonical.read_bytes() == canonical_before

    # Deletion is likewise detected, canonical evidence still intact.
    Path(workspace_log).unlink()
    deleted = canonical_audit_evidence(config, workspace_log)
    assert deleted["canonical_log_present"] is True
    assert deleted["workspace_log_verified"] is False
    assert canonical.read_bytes() == canonical_before


def test_canonical_evidence_tolerates_extra_passive_workspace_lines(tmp_path: Path) -> None:
    # Effect events are mirrored canonically; passive COM RX feedback is written
    # to the workspace log only. Verification must treat the canonical log as an
    # ordered subset of the workspace log, not require byte equality.
    config = load_test_config(tmp_path, com_ports_yaml=COM_PORT_YAML)
    workspace_log = str(Path(logs_directory(config)) / "com-mixed.jsonl")
    assert append_jsonl(workspace_log, {"event": "start"}, config) is None  # mirrored
    assert append_jsonl(workspace_log, {"direction": "rx", "hex": "aa"}) is None  # workspace only
    assert append_jsonl(workspace_log, {"direction": "tx", "hex": "bb"}, config) is None  # mirrored
    assert append_jsonl(workspace_log, {"direction": "rx", "hex": "cc"}) is None  # workspace only

    evidence = canonical_audit_evidence(config, workspace_log)
    assert evidence["canonical_log_present"] is True
    assert evidence["workspace_log_verified"] is True

    # Deleting a mirrored effect line is still detected despite the RX lines.
    lines = [line for line in Path(workspace_log).read_text(encoding="utf-8").splitlines() if '"tx"' not in line]
    Path(workspace_log).write_text("\n".join(lines) + "\n", encoding="utf-8")
    tampered = canonical_audit_evidence(config, workspace_log)
    assert tampered["workspace_log_verified"] is False


def test_canonical_evidence_detects_reordered_effect_lines(tmp_path: Path) -> None:
    # Reordering effect records breaks the ordered-subsequence check, so an
    # attacker cannot hide an effect by shuffling the workspace log.
    config = load_test_config(tmp_path, com_ports_yaml=COM_PORT_YAML)
    workspace_log = str(Path(logs_directory(config)) / "com-reorder.jsonl")
    assert append_jsonl(workspace_log, {"event": "start", "seq": 1}, config) is None
    assert append_jsonl(workspace_log, {"direction": "tx", "seq": 2}, config) is None
    assert append_jsonl(workspace_log, {"event": "stop", "seq": 3}, config) is None

    original = [line for line in Path(workspace_log).read_text(encoding="utf-8").splitlines() if line]
    reordered = [original[2], original[0], original[1]]
    Path(workspace_log).write_text("\n".join(reordered) + "\n", encoding="utf-8")

    assert canonical_audit_evidence(config, workspace_log)["workspace_log_verified"] is False


def test_canonical_audit_sequence_is_monotonic_under_parallel_appends(tmp_path: Path) -> None:
    config = load_test_config(tmp_path, com_ports_yaml=COM_PORT_YAML)
    workspace_log = str(Path(logs_directory(config)) / "com-parallel.jsonl")
    total = 40

    def append(index: int) -> Exception | None:
        return append_canonical_audit_log(config, workspace_log, json.dumps({"seq": index}) + "\n")

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(append, range(total)))

    assert all(result is None for result in results)
    evidence = canonical_audit_evidence(config, workspace_log)
    assert evidence["log_sequence"] == total
    canonical = canonical_audit_log_path(config, workspace_log)
    lines = [line for line in canonical.read_text(encoding="utf-8").splitlines() if line]
    assert len(lines) == total


def test_report_api_surfaces_canonical_audit_evidence(tmp_path: Path) -> None:
    config = load_test_config(tmp_path, com_ports_yaml=COM_PORT_YAML)
    workspace_log = str(Path(logs_directory(config)) / "com-report.jsonl")
    assert append_jsonl(workspace_log, {"direction": "tx", "hex": "01"}, config) is None
    from agentic_hil.config import display_path

    result = {"ok": True, "tool": "com_write", "log_path": display_path(config, workspace_log)}
    enriched = attach_canonical_audit_evidence(config, result)
    assert enriched["canonical_audit"]["workspace_log_verified"] is True

    Path(workspace_log).write_text("tampered\n", encoding="utf-8")
    retampered = attach_canonical_audit_evidence(config, result)
    assert retampered["canonical_audit"]["workspace_log_verified"] is False
