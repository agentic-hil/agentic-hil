from __future__ import annotations

import json
import os
import re
import stat
import struct
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from dataclasses import replace
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import FAKE_OPENOCD, write_authoritative_config, write_config
from support import owner_only_write

from agentic_hil.artifacts import ArtifactManager
from agentic_hil.backends.common import spawn_command
from agentic_hil.backends.gdbdebug import GdbDebugSessions
from agentic_hil.bridge import BridgeCleanupError, ProcessBridgeSession
from agentic_hil.can import CAN_DRAIN_TIMEOUT_S, CanBusService, CanBusSession, parse_can_id, payload_frame
from agentic_hil.cli import debugger_probes
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
from agentic_hil.contracts import validate_tool_arguments
from agentic_hil.gdbmi import GdbMiClient
from agentic_hil.mcp import handle_mcp_message
from agentic_hil.report import (
    append_canonical_audit_log,
    append_jsonl,
    append_jsonl_audited,
    attach_canonical_audit_evidence,
    canonical_audit_evidence,
    canonical_audit_log_path,
    ensure_audit_ready,
    logs_directory,
    overall_success,
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
def test_world_writable_lock_dir_blocks_hardware_coordination(tmp_path: Path) -> None:
    # Coordination state is created when hardware is actually coordinated, not
    # when the coordinator is constructed, so acquiring is where the directory
    # has to be refused — the last moment before a lock could be taken.
    from agentic_hil.coordination import HardwareCoordinator

    config = load_config(str(write_config(tmp_path)))
    locks = Path(config.state_root) / "coordination" / "locks"
    locks.mkdir(parents=True)
    locks.chmod(0o777)
    try:
        coordinator = HardwareCoordinator(config, "owner")
        assert not (Path(config.state_root) / "coordination" / "records").exists()
        with pytest.raises(ConfigError) as excinfo:
            coordinator.acquire("physical:test")
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
    # open() is a no-op here: the port is now configured before it is opened,
    # so the modem lines are decided rather than left to pyserial's defaults.
    handle = SimpleNamespace(closed=False, close=lambda: setattr(handle, "closed", True), open=lambda: None)
    monkeypatch.setitem(sys.modules, "serial", SimpleNamespace(Serial=lambda *args, **kwargs: handle))
    monkeypatch.setattr("agentic_hil.comports.ComPortSession", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("construct failed")))

    result = service._open_serial("dut", config.com_ports["dut"], str(tmp_path / "com.jsonl"), SimpleNamespace())

    assert result["ok"] is False
    assert handle.closed is True


def test_can_session_constructor_failure_shuts_down_raw_bus(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from agentic_hil.can import open_python_can_adapter

    config = load_test_config(tmp_path, can_buses_yaml='can_buses:\n  bench:\n    adapter: "socketcan"\n    channel: "vcan0"\n')
    # An interface whose timing this host cannot answer for is not opened at all,
    # so describe one that matches to reach the construction failure under test.
    _socketcan_link(monkeypatch, bitrate=config.can_buses["bench"].bitrate)
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

    handle = SimpleNamespace(close=flaky_close, open=lambda: None)
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
    _socketcan_link(monkeypatch, bitrate=config.can_buses["bench"].bitrate)
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


def test_can_queue_drain_budget_excludes_the_pre_drain_audit_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = load_test_config(tmp_path, can_buses_yaml='can_buses:\n  bench:\n    adapter: "socketcan"\n    channel: "vcan0"\n    max_buffer_frames: 2\n')
    service = CanBusService(config)
    clock = {"now": 1000.0}

    def slow_append(config_arg, log_path, record):
        if record.get("event") == "queue_clear_start":
            clock["now"] += CAN_DRAIN_TIMEOUT_S * 5
        return append_jsonl_audited(config_arg, log_path, record)

    monkeypatch.setattr("agentic_hil.can.append_jsonl_audited", slow_append)
    monkeypatch.setattr("agentic_hil.can.time", SimpleNamespace(monotonic=lambda: clock["now"]))

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
    session = CanBusSession("bench", config.can_buses["bench"], adapter, str(tmp_path / "can-slow-audit.jsonl"))
    service.sessions["bench"] = session

    result = service.session_start("bench", clear_rx_queue=True)

    assert result["ok"] is True
    assert adapter.frames == []
    assert adapter.reads == 4


def test_can_queue_drain_budget_still_bounds_adapter_time(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = load_test_config(tmp_path, can_buses_yaml='can_buses:\n  bench:\n    adapter: "socketcan"\n    channel: "vcan0"\n    max_buffer_frames: 2\n')
    service = CanBusService(config)
    clock = {"now": 1000.0}
    monkeypatch.setattr("agentic_hil.can.time", SimpleNamespace(monotonic=lambda: clock["now"]))

    class SlowBusyAdapter:
        adapter_name = "fake"

        def __init__(self) -> None:
            self.reads = 0

        def read(self, max_frames: int, wait_timeout_s: float) -> dict:
            self.reads += 1
            clock["now"] += CAN_DRAIN_TIMEOUT_S * 0.6
            return {"ok": True, "frames": [{"id": 1}, {"id": 2}]}

        def status(self) -> dict:
            return {"active": True}

    adapter = SlowBusyAdapter()
    session = CanBusSession("bench", config.can_buses["bench"], adapter, str(tmp_path / "can-slow-adapter.jsonl"))
    service.sessions["bench"] = session

    result = service.session_start("bench", clear_rx_queue=True)

    assert result["error_type"] == "can_queue_clear_limit"
    assert adapter.reads == 2
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
        permissions={"allow_com_read": False},
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
        load_test_config(tmp_path, permissions={"allow_flash": True, "allow_reset": False})
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
        permissions={flag: False},
    )
    service = AgenticHILToolService(config)

    result = service.call(tool, arguments)

    assert result["ok"] is False, f"{tool} must be blocked when {flag} is false"
    assert result["error_type"] == "permission_denied"


def test_startup_config_is_frozen_when_file_permissions_change(tmp_path: Path) -> None:
    config_path = write_config(tmp_path, permissions={"allow_probe": False})
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
    config_path = write_authoritative_config(workspace, monkeypatch, permissions={})
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
        permissions={},
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
    write_authoritative_config(workspace, monkeypatch, permissions={})

    with pytest.raises(ConfigError) as rejected:
        load_authoritative_config(other)

    assert rejected.value.error_type == "config_invalid"
    assert rejected.value.details["workspace_root"] == str(workspace.resolve())
    assert rejected.value.details["expected_workspace"] == str(other.resolve())


def test_authoritative_config_override_need_not_use_canonical_workspace_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "workspace"
    canonical = write_authoritative_config(workspace, monkeypatch, permissions={})
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

    assert rejected.value.error_type == "config_invalid"
    assert rejected.value.details["validator"] == "required"


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


@pytest.mark.parametrize("section", ["com_ports", "can_buses"])
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
        permissions={},
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
        permissions={},
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
        permissions={},
    )

    with pytest.raises(ConfigError) as rejected:
        load_authoritative_config(workspace)

    assert rejected.value.error_type == "config_invalid"
    assert rejected.value.details["field"] == "can_buses.injected.args"


def test_authoritative_config_pins_debugger_and_openocd_scripts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "workspace"
    host = tmp_path / "tools"
    interface_cfg = host / "trusted-interface.cfg"
    target_cfg = host / "trusted-target.cfg"
    # Owner-only, not the ambient umask: OpenOCD executes these scripts as Tcl,
    # so one another local user could write is refused rather than pinned.
    owner_only_write(interface_cfg, "# trusted interface\n")
    owner_only_write(target_cfg, "# trusted target\n")
    config_path = write_authoritative_config(
        workspace,
        monkeypatch,
        debugger_executable=FAKE_OPENOCD,
        permissions={"allow_probe": True},
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


def test_authoritative_config_rejects_debuggers_that_pin_onto_one_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Two pyocd entries reached through different spellings of one binary (an
    # absolute path and a bare PATH name), both naming the same probe serial.
    # Distinct executables never made these distinct boards, and pinning
    # collapses them onto one file, so the config is refused.
    toolchain = tmp_path / "toolchain"
    toolchain.mkdir()
    shim = toolchain / "pyocd-shim.bat"
    shim.write_text("@echo off\n", encoding="utf-8")
    os.chmod(shim, 0o755)
    workspace = tmp_path / "workspace"
    write_authoritative_config(
        workspace,
        monkeypatch,
        debugger_type="pyocd",
        debugger_executable=shim,
        debugger_name="dut_a",
        probe_id="PROBE-A",
        auto_probe_ids=False,
        debuggers_yaml='debuggers:\n  probe_b:\n    type: pyocd\n    probe_id: "PROBE-A"\n    executable: "pyocd-shim.bat"\n',
        permissions={"allow_probe": True},
    )
    monkeypatch.setenv("PATH", str(toolchain) + os.pathsep + os.environ.get("PATH", ""))

    with pytest.raises(ConfigError) as rejected:
        load_authoritative_config(workspace)

    assert rejected.value.error_type == "config_invalid"
    assert rejected.value.details["field"] == "debuggers.probe_b.probe_id"
    assert rejected.value.details["other_debugger"] == "dut_a"


def test_authoritative_config_rejects_relative_openocd_scripts_when_debugger_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    config_path = write_authoritative_config(
        workspace,
        monkeypatch,
        debugger_executable=FAKE_OPENOCD,
        permissions={"allow_probe": True},
    )
    text = config_path.read_text(encoding="utf-8")
    config_path.write_text(
        text.replace((config_path.parent / "interface.cfg").as_posix(), "interface/stlink.cfg"), encoding="utf-8"
    )

    with pytest.raises(ConfigError) as rejected:
        load_authoritative_config(workspace)

    assert rejected.value.error_type == "config_invalid"
    assert rejected.value.details["field"] == "debuggers.dut.interface_cfg"


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


def test_unbound_debugger_refusal_does_not_demand_an_impossible_argument(tmp_path: Path) -> None:
    # No MCP tool schema carries a probe name, so a refusal telling the caller to
    # "name the debugger" would ask for something the protocol cannot express —
    # the shape of refusal this codebase records as having driven a model to
    # reach for st-flash instead.
    config = load_test_config(
        tmp_path,
        probe_id="PROBE-A",
        debuggers_yaml='debuggers:\n  probe_b:\n    type: openocd\n    probe_id: "PROBE-B"\n',
    )
    assert config.debugger is None
    service = AgenticHILToolService(config)
    try:
        result = service.call("probe_target")
    finally:
        service.close()

    assert result["ok"] is False
    assert result["error_type"] == "not_supported"
    assert result["retry_safe"] is False
    assert result["side_effect_committed"] is False
    assert "test-reactor" in result["summary"]
    assert sorted(result["configured_debuggers"]) == ["dut", "probe_b"]
    assert validate_tool_arguments("probe_target", {"debugger": "dut"}) is not None


def test_zero_debugger_refusal_does_not_claim_several_are_configured(tmp_path: Path) -> None:
    config_path = write_config(tmp_path)
    text = config_path.read_text(encoding="utf-8")
    config_path.write_text(re.sub(r"(?ms)^debuggers:\n(?:  .*\n)+", "debuggers: {}\n", text), encoding="utf-8")
    config = load_config(str(config_path))
    assert config.debuggers == {}
    service = AgenticHILToolService(config)
    try:
        result = service.call("debugger_info")
    finally:
        service.close()

    assert result["error_type"] == "not_supported"
    assert result["configured_debuggers"] == []
    assert "several" not in result["summary"]
    assert result["retry_safe"] is False


def test_staging_refusal_before_any_backend_call_does_not_quarantine_the_lease(tmp_path: Path) -> None:
    # require_existing_file: false lets validation pass on a file that is not
    # there yet, so nothing about those bytes was checked. Staging must refuse —
    # but the refusal happened before the backend ran, so it must not be
    # recorded as an unconfirmed hardware effect and poison the bench.
    config_path = write_config(tmp_path)
    text = config_path.read_text(encoding="utf-8")
    config_path.write_text(text.replace("logs:\n", "validation:\n  require_existing_file: false\nlogs:\n", 1), encoding="utf-8")
    config = load_config(str(config_path))
    service = AgenticHILToolService(config)
    try:
        (tmp_path / "build").mkdir(parents=True, exist_ok=True)
        validated = service.artifacts.validate_local_path("build/app.elf")
        assert validated["ok"] is True
        assert validated["artifact"]["integrity_sha256"] is None

        (tmp_path / "build" / "app.elf").write_bytes(b"\x7fELF" + b"\x00" * 12)
        staged = service.artifacts.stage_for_backend(validated["artifact"], "flash_firmware")

        assert staged["ok"] is False
        assert staged["error_type"] == "artifact_changed"
        assert staged["side_effect_committed"] is False
        assert service._result_requires_quarantine(staged) is False
    finally:
        service.close()


def test_dump_output_path_outside_the_workspace_is_refused_up_front(tmp_path: Path) -> None:
    # The mirror of the artifact read check: without it a symlinked directory
    # inside the workspace only failed after the plan had already flashed and
    # halted the board.
    config = load_test_config(tmp_path)
    service = AgenticHILToolService(config)
    try:
        outside = tmp_path.parent / "escaped.hex"
        result = service.artifacts.validate_output_path(str(outside), "debug_dump_symbol_ihex")
    finally:
        service.close()

    assert result["ok"] is False
    assert result["error_type"] == "output_validation_failed"
    assert result["validation"]["within_workspace"] is False
    assert "workspace_root" in result["summary"]


def test_two_debuggers_naming_one_probe_id_are_rejected_despite_distinct_resource_ids(tmp_path: Path) -> None:
    # resource_id only renames the coordination lease. probe_id is what the
    # backends turn into `adapter serial` / `sn=` / `--uid`, so two entries on
    # one serial are one board however their leases are named.
    with pytest.raises(ConfigError) as rejected:
        load_config(
            str(
                write_config(
                    tmp_path,
                    debugger_name="board_a",
                    probe_id="SN-001",
                    debuggers_yaml='debuggers:\n  board_b:\n    type: openocd\n    probe_id: "SN-001"\n    resource_id: "bench-b"\n',
                )
            )
        )

    assert rejected.value.error_type == "config_invalid"
    assert rejected.value.details["field"] == "debuggers.board_b.probe_id"
    assert rejected.value.details["other_debugger"] == "board_a"


def test_classify_last_error_works_without_any_debugger(tmp_path: Path) -> None:
    # The failure record is written by whichever tool failed, so a project with
    # no probe must still be able to ask what went wrong.
    config_path = write_config(tmp_path, com_ports_yaml='com_ports:\n  dut_uart:\n    device: "COM_TEST"\n')
    text = config_path.read_text(encoding="utf-8")
    config_path.write_text(re.sub(r"(?ms)^debuggers:\n(?:  .*\n)+", "debuggers: {}\n", text), encoding="utf-8")
    service = AgenticHILToolService(load_config(str(config_path)))
    try:
        service.call("com_session_start", {"port_id": "dut_uart"})
        result = service.call("classify_last_error")
    finally:
        service.close()

    assert result["error_type"] != "not_supported"
    assert result["tool"] == "classify_last_error"


def test_write_only_com_port_can_actually_be_written(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # allow_write without allow_read used to be a grant that could never be
    # used: the session is the only way to reach the port, and opening it was
    # gated on allow_read alone.
    config = load_test_config(
        tmp_path,
        com_ports_yaml='com_ports:\n  reset_line:\n    device: "COM_TEST"\n    permissions:\n      allow_read: false\n      allow_write: true\n',
    )
    service = ComPortService(config)
    written: list[bytes] = []
    monkeypatch.setattr(
        service,
        "_open_serial",
        lambda port_id, port_config, log_path, lease: {
            "ok": True,
            "session": ComPortSession(port_id, port_config, _RecordingSerial(written), log_path, lease, start_reader=False),
        },
    )
    try:
        started = service.session_start("reset_line")
        assert started["ok"] is True, started
        assert service.sessions["reset_line"].reader is None

        result = service.write("reset_line", {"text": "reset\n"})
        assert result["ok"] is True, result
        assert written == [b"reset\n"]

        denied = service.read("reset_line")
        assert denied["error_type"] == "permission_denied"
    finally:
        service.close()


class _RecordingSerial:
    is_open = True
    in_waiting = 0

    def __init__(self, sink: list[bytes]) -> None:
        self._sink = sink

    def write(self, data: bytes) -> int:
        self._sink.append(bytes(data))
        return len(data)

    def flush(self) -> None:
        return None

    def read(self, size: int) -> bytes:
        return b""

    def close(self) -> None:
        self.is_open = False


def test_artifact_that_never_existed_is_reported_as_not_found(tmp_path: Path) -> None:
    # With require_existing_file: false the file may be absent at validation.
    # Reporting that as "changed after validation" sends the caller back to
    # re-validate, which keeps succeeding; the artifact has to be built.
    config_path = write_config(tmp_path)
    text = config_path.read_text(encoding="utf-8")
    config_path.write_text(text.replace("logs:\n", "validation:\n  require_existing_file: false\nlogs:\n", 1), encoding="utf-8")
    service = AgenticHILToolService(load_config(str(config_path)))
    try:
        (tmp_path / "build").mkdir(parents=True, exist_ok=True)
        validated = service.artifacts.validate_local_path("build/app.elf")
        assert validated["ok"] is True

        staged = service.artifacts.stage_for_backend(validated["artifact"], "flash_firmware")
    finally:
        service.close()

    assert staged["error_type"] == "artifact_not_found"
    assert "Build it first" in staged["summary"]
    assert staged["side_effect_committed"] is False


def test_probe_discovery_without_any_debugger_does_not_blame_a_permission(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Nothing is switched off when nothing is configured, and the CLI must not
    # contradict the MCP surface for the same config.
    workspace = tmp_path / "workspace"
    write_authoritative_config(workspace, monkeypatch)
    config_path = Path(os.environ["AGENTIC_HIL_CONFIG"])
    text = config_path.read_text(encoding="utf-8")
    config_path.write_text(re.sub(r"(?ms)^debuggers:\n(?:  .*\n)+", "debuggers: {}\n", text), encoding="utf-8")
    monkeypatch.chdir(workspace)

    result = debugger_probes()

    assert result["ok"] is False
    assert result["error_type"] == "not_supported"
    assert result["configured_debuggers"] == []
    assert "permission" not in result["summary"].lower()


def test_multi_probe_discovery_fails_when_any_probe_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # One probe answering must not report the run as healthy while another
    # failed: the CLI exit code is derived from this verdict alone, and a
    # containment marker on any probe has to reach the top level.
    from agentic_hil import cli as cli_module

    workspace = tmp_path / "workspace"
    write_authoritative_config(
        workspace,
        monkeypatch,
        debugger_name="probe_a",
        probe_id="PROBE-A",
        debuggers_yaml=(
            'debuggers:\n  probe_b:\n    type: openocd\n    probe_id: "PROBE-B"\n'
            f'    executable: "{FAKE_OPENOCD.as_posix()}"\n'
        ),
    )
    monkeypatch.chdir(workspace)

    answers = {
        "probe_a": {"ok": True, "tool": "debugger_probes_list", "probes": []},
        "probe_b": {"ok": False, "tool": "debugger_probes_list", "error_type": "debugger_not_found", "cleanup_required": True, "quarantined": True, "quarantine_id": "q-1"},
    }

    class FakeService:
        def __init__(self, config, *args, **kwargs) -> None:
            self._name = config.debugger_id

        def call(self, name: str, arguments: dict | None = None) -> dict:
            return answers[self._name]

        def close(self) -> None:
            return None

    monkeypatch.setattr(cli_module, "AgenticHILToolService", FakeService)

    result = cli_module.debugger_probes()

    assert result["ok"] is False
    assert "probe_b" in result["summary"]
    assert result["cleanup_required"] is True
    assert result["quarantined"] is True
    assert result["quarantine_id"] == "q-1"
    assert overall_success(result) is False


def test_debug_load_refusal_names_the_permission_that_fired(tmp_path: Path) -> None:
    # Preflight is the only diagnosis on this path — the backend's per-flag
    # messages are never reached — so it must not point at a flag the operator
    # can see is false.
    from agentic_hil.test_reactor import TestConfig, TestReactor, TestStep

    config = load_test_config(tmp_path, permissions={"allow_probe": True, "allow_reset": True, "allow_flash": True, "allow_mass_erase": True})
    firmware = tmp_path / "build" / "app.elf"
    firmware.parent.mkdir(parents=True, exist_ok=True)
    firmware.write_bytes(b"\x7fELF" + b"\x00" * 12)
    service = AgenticHILToolService(config)
    try:
        plan = TestConfig(path=str(tmp_path / "plan.yaml"), name="load", steps=[TestStep("debug_start", {"image_path": "build/app.elf", "mode": "load"}, debugger="dut")])
        result = TestReactor(config, service).run(plan)
    finally:
        service.close()

    assert result["ok"] is False
    assert result["validation_error"]["permission"] == "allow_mass_erase"
    assert "mass erase" in result["validation_error"]["summary"]


def test_artifact_outside_the_workspace_is_refused_with_the_real_reason(tmp_path: Path) -> None:
    # With require_allowed_root off, the allowed-roots refusal no longer fires
    # first and this is the path that used to answer "must be a single-link
    # regular file" with a backend_error about an *output* file. No permission
    # relaxes workspace containment, so a caller told "regular file" keeps
    # retrying a path that can never work.
    config_path = write_config(tmp_path)
    text = config_path.read_text(encoding="utf-8")
    config_path.write_text(text.replace("logs:\n", "validation:\n  require_allowed_root: false\nlogs:\n", 1), encoding="utf-8")
    service = AgenticHILToolService(load_config(str(config_path)))
    try:
        outside = tmp_path.parent / "elsewhere.elf"
        outside.write_bytes(b"\x7fELF" + b"\x00" * 12)
        result = service.artifacts.validate_local_path(str(outside))
    finally:
        service.close()

    assert result["ok"] is False
    assert result["validation"]["within_workspace"] is False
    assert "workspace_root" in result["summary"]
    assert "single-link regular file" not in result["summary"]


@pytest.mark.skipif(os.name != "nt", reason="directory junctions are a Windows construct")
def test_artifact_behind_a_link_that_leaves_the_workspace_is_named_too(tmp_path: Path) -> None:
    # A lexical containment check reports this path as contained, so the
    # refusal fell back to the misleading "single-link regular file" wording on
    # exactly the case where naming the reason matters most.
    workspace = tmp_path / "ws"
    outside = tmp_path / "outside"
    outside.mkdir(parents=True)
    (outside / "app.elf").write_bytes(b"\x7fELF" + b"\x00" * 12)
    config_path = write_config(workspace)
    text = config_path.read_text(encoding="utf-8")
    config_path.write_text(text.replace("logs:\n", "validation:\n  require_allowed_root: false\nlogs:\n", 1), encoding="utf-8")
    subprocess.run(["cmd", "/c", "mklink", "/J", str(workspace / "build"), str(outside)], check=True, capture_output=True)

    service = AgenticHILToolService(load_config(str(config_path)))
    try:
        result = service.artifacts.validate_local_path("build/app.elf")
    finally:
        service.close()

    assert result["ok"] is False
    assert result["validation"]["within_workspace"] is False
    assert "workspace_root" in result["summary"]


@pytest.mark.skipif(os.name != "nt", reason="directory junctions are a Windows construct")
def test_dump_into_an_allowed_root_that_is_a_link_is_not_falsely_refused(tmp_path: Path) -> None:
    # validate_output_path resolves symlinks while the allowed roots are built
    # lexically, so an allowed root that is itself a link inside the workspace
    # refused every legitimate dump into it.
    workspace = tmp_path / "ws"
    config_path = write_config(workspace)
    (workspace / "real").mkdir(parents=True, exist_ok=True)
    subprocess.run(["cmd", "/c", "mklink", "/J", str(workspace / "build"), str(workspace / "real")], check=True, capture_output=True)

    service = AgenticHILToolService(load_config(str(config_path)))
    try:
        result = service.artifacts.validate_output_path("build/dump.hex", "debug_dump_symbol_ihex")
    finally:
        service.close()

    assert result["ok"] is True, result
    assert result["validation"]["allowed_root"] is True
    assert result["validation"]["within_workspace"] is True


def _without_the_root_check(config_path: Path) -> Path:
    """Switch `validation.require_allowed_root` off in a written config.

    The widest a configuration can be. What still refuses after this is what was
    never the root list's job in the first place."""
    text = config_path.read_text(encoding="utf-8")
    config_path.write_text(text.replace("logs:\n", "validation:\n  require_allowed_root: false\nlogs:\n", 1), encoding="utf-8")
    return config_path


def _link_directory(link: Path, target: Path) -> None:
    """A directory link at `link` pointing at `target`, on either platform."""
    if os.name == "nt":
        # A junction needs no privilege; a Windows symlink does.
        subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], check=True, capture_output=True)
    else:
        link.symlink_to(target, target_is_directory=True)


def test_an_artifact_anywhere_in_the_workspace_is_accepted(tmp_path: Path) -> None:
    # The case the whole change is for: CLion builds into cmake-build-debug,
    # PlatformIO into .pio/build/<env>. Neither is "build", and with "." neither
    # has to be named before the first flash.
    config_path = write_config(tmp_path, allowed_roots=["."])
    firmware = tmp_path / "cmake-build-debug" / "nested" / "app.elf"
    firmware.parent.mkdir(parents=True)
    firmware.write_bytes(b"\x7fELF" + b"\x00" * 12)
    service = AgenticHILToolService(load_config(str(config_path)))
    try:
        result = service.artifacts.validate_local_path("cmake-build-debug/nested/app.elf")
    finally:
        service.close()

    assert result["ok"] is True, result
    assert result["validation"]["allowed_root"] is True
    assert result["validation"]["within_workspace"] is True
    # Nothing else was skipped on the way: the content was still read and hashed.
    assert result["validation"]["sha256_computed"] is True
    assert result["validation"]["elf_header"] is True


def test_the_whole_workspace_is_still_only_the_workspace(tmp_path: Path) -> None:
    # With the root list at its widest *and* switched off, containment is the
    # only thing left holding — which is the claim this change rests on.
    workspace = tmp_path / "ws"
    config_path = _without_the_root_check(write_config(workspace, allowed_roots=["."]))
    outside = tmp_path / "elsewhere.elf"
    outside.write_bytes(b"\x7fELF" + b"\x00" * 12)
    service = AgenticHILToolService(load_config(str(config_path)))
    try:
        result = service.artifacts.validate_local_path(str(outside))
        traversed = service.artifacts.validate_local_path("../elsewhere.elf")
    finally:
        service.close()

    assert result["ok"] is False
    assert result["validation"]["within_workspace"] is False
    assert "workspace_root" in result["summary"]
    assert traversed["ok"] is False
    assert traversed["validation"]["path_traversal_safe"] is False


def test_a_link_out_of_the_whole_workspace_is_still_refused(tmp_path: Path) -> None:
    # A build directory that is a link to somewhere else is contained lexically
    # and not in fact. Widening the roots must not make that the accepted case.
    workspace = tmp_path / "ws"
    outside = tmp_path / "outside"
    outside.mkdir(parents=True)
    (outside / "app.elf").write_bytes(b"\x7fELF" + b"\x00" * 12)
    config_path = _without_the_root_check(write_config(workspace, allowed_roots=["."]))
    _link_directory(workspace / "cmake-build-debug", outside)

    service = AgenticHILToolService(load_config(str(config_path)))
    try:
        result = service.artifacts.validate_local_path("cmake-build-debug/app.elf")
    finally:
        service.close()

    assert result["ok"] is False
    assert result["validation"]["within_workspace"] is False
    assert "workspace_root" in result["summary"]


def test_the_whole_workspace_still_refuses_a_foreign_extension(tmp_path: Path) -> None:
    config_path = write_config(tmp_path, allowed_roots=["."])
    artifact = tmp_path / "cmake-build-debug" / "notes.txt"
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"\x7fELF" + b"\x00" * 12)
    service = AgenticHILToolService(load_config(str(config_path)))
    try:
        result = service.artifacts.validate_local_path("cmake-build-debug/notes.txt")
    finally:
        service.close()

    assert result["ok"] is False
    assert result["validation"]["allowed_extension"] is False
    assert "extension" in result["summary"]


def test_the_whole_workspace_still_inspects_the_format(tmp_path: Path) -> None:
    config_path = write_config(tmp_path, allowed_roots=["."])
    artifact = tmp_path / "cmake-build-debug" / "app.elf"
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"this is not an ELF file")
    service = AgenticHILToolService(load_config(str(config_path)))
    try:
        result = service.artifacts.validate_local_path("cmake-build-debug/app.elf")
    finally:
        service.close()

    assert result["ok"] is False
    assert result["validation"]["elf_header"] is False
    assert "plausibility" in result["summary"]


def test_a_configuration_that_names_its_roots_keeps_exactly_those(tmp_path: Path) -> None:
    # The anti-widening rule, from the operator's side: a file saying "build"
    # still means build, and a build directory of another shape is still refused.
    config_path = write_config(tmp_path, allowed_roots=["build"])
    config = load_config(str(config_path))
    assert config.artifacts.allowed_roots == ["build"]

    for name in ("build", "cmake-build-debug"):
        artifact = tmp_path / name / "app.elf"
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_bytes(b"\x7fELF" + b"\x00" * 12)
    service = AgenticHILToolService(config)
    try:
        named = service.artifacts.validate_local_path("build/app.elf")
        unnamed = service.artifacts.validate_local_path("cmake-build-debug/app.elf")
    finally:
        service.close()

    assert named["ok"] is True, named
    assert unnamed["ok"] is False
    assert unnamed["validation"]["allowed_root"] is False


def test_a_configuration_that_never_named_the_roots_still_means_build(tmp_path: Path) -> None:
    # The other half of the same rule: what the *absence* of the key means may
    # not change either, or every file written before this release widens on
    # update without anyone editing it.
    config = load_config(str(write_config(tmp_path, omit_allowed_roots=True)))
    assert config.artifacts.allowed_roots == ["build"]


def test_an_empty_allowed_roots_list_is_refused_by_name(tmp_path: Path) -> None:
    # An empty list is the other plausible spelling for "the whole project", and
    # here it has always meant the opposite. It is refused rather than left to
    # mean whichever of the two the reader assumed.
    config_path = write_config(tmp_path, allowed_roots=[])

    with pytest.raises(ConfigError) as excinfo:
        load_config(str(config_path))

    assert excinfo.value.error_type == "config_invalid"
    assert excinfo.value.details["field"] == "artifacts.allowed_roots"
    assert '["."]' in excinfo.value.details["migration"]["whole_workspace"]


def test_widening_the_roots_does_not_move_where_uploads_are_stored(tmp_path: Path) -> None:
    # allowed_roots also governs the upload path. Widening it must not relocate
    # the store: that is artifacts.upload_directory and nothing else.
    config_path = write_config(tmp_path, allowed_roots=["."])
    firmware = tmp_path / "cmake-build-debug" / "app.bin"
    firmware.parent.mkdir(parents=True)
    firmware.write_bytes(b"\x01\x02\x03\x04")
    config = load_config(str(config_path))
    service = AgenticHILToolService(config)
    try:
        result = service.artifacts.upload({"image_path": "cmake-build-debug/app.bin"})
    finally:
        service.close()

    assert result["ok"] is True, result
    assert Path(result["artifact"]["resolved_path"]).parent == Path(config.work_dir) / ".agentic-hil" / "artifacts"


def test_a_dump_into_the_whole_workspace_still_obeys_the_extension(tmp_path: Path) -> None:
    # The same list decides where a debug dump may land. It moves with the
    # artifact roots by design; the extension check on that path does not.
    config_path = write_config(tmp_path, allowed_roots=["."])
    service = AgenticHILToolService(load_config(str(config_path)))
    try:
        accepted = service.artifacts.validate_output_path("cmake-build-debug/memory.hex", "debug_dump_symbol_ihex")
        refused = service.artifacts.validate_output_path("cmake-build-debug/memory.elf", "debug_dump_symbol_ihex")
    finally:
        service.close()

    assert accepted["ok"] is True, accepted
    assert accepted["validation"]["allowed_root"] is True
    assert refused["ok"] is False
    assert refused["validation"]["allowed_extension"] is False


def _pyocd_service(tmp_path: Path, probe_id: str | None):
    config = load_test_config(tmp_path, debugger_type="pyocd", probe_id=probe_id)
    return AgenticHILToolService(config)


def test_pyocd_probe_selector_is_canonicalized_to_a_full_uid(tmp_path: Path) -> None:
    # pyOCD matches --uid as a case-insensitive SUBSTRING, so a partial value
    # must be resolved against the enumerated probes and the full UID passed on,
    # or the configured name does not mean one board.
    service = _pyocd_service(tmp_path, "pyocd123")
    try:
        result = service.call("probe_target")
        assert result["ok"] is True, result
        assert service.backend._resolved_probe_uid == "PYOCD123"
        assert service.backend._connection_args()[:2] == ["--uid", "PYOCD123"]
    finally:
        service.close()


def test_pyocd_ambiguous_probe_selector_is_refused_before_touching_a_board(tmp_path: Path) -> None:
    # "PYOCD" is a substring of both connected probes, so pyOCD would silently
    # take the first one.
    service = _pyocd_service(tmp_path, "PYOCD")
    try:
        result = service.call("probe_target")
    finally:
        service.close()

    assert result["ok"] is False
    assert result["error_type"] == "adapter_not_found"
    assert result["side_effect_committed"] is False
    assert sorted(result["candidate_probe_ids"]) == ["PYOCD123", "PYOCD456"]


def test_pyocd_probe_selector_that_matches_nothing_is_refused(tmp_path: Path) -> None:
    service = _pyocd_service(tmp_path, "NOT-CONNECTED")
    try:
        result = service.call("probe_target")
    finally:
        service.close()

    assert result["ok"] is False
    assert result["error_type"] == "adapter_not_found"
    assert result["side_effect_committed"] is False
    assert result["configured_probe_id"] == "NOT-CONNECTED"


def test_pyocd_without_a_probe_id_passes_no_uid(tmp_path: Path) -> None:
    service = _pyocd_service(tmp_path, None)
    try:
        assert service.call("probe_target")["ok"] is True
        assert "--uid" not in service.backend._connection_args()
    finally:
        service.close()


# ---------------------------------------------------------------------------
# Trust boundaries: who may replace a program, a script, or a directory of them.


def _foreign_owner(info: os.stat_result) -> os.stat_result:
    """The same stat, reported as belonging to another local identity.

    A test cannot chown without root, and the property under test is exactly
    what happens when a component belongs to somebody else, so ownership is
    what is faked — never the mode, which the filesystem can carry for real."""
    fields = list(info)
    fields[4] = info.st_uid + 4242
    return os.stat_result(fields)


def _report_foreign_owner(monkeypatch: pytest.MonkeyPatch, path: Path) -> None:
    """Make one existing directory or file look foreign-owned to every check."""
    inode = path.stat().st_ino
    real_stat, real_fstat = os.stat, os.fstat

    def fake_stat(target, *args, **kwargs):
        info = real_stat(target, *args, **kwargs)
        return _foreign_owner(info) if info.st_ino == inode else info

    def fake_fstat(descriptor):
        info = real_fstat(descriptor)
        return _foreign_owner(info) if info.st_ino == inode else info

    monkeypatch.setattr(os, "stat", fake_stat)
    monkeypatch.setattr(os, "fstat", fake_fstat)


@pytest.mark.skipif(os.name == "nt", reason="POSIX ownership rules")
def test_a_foreign_owned_ancestor_is_refused_by_every_posix_trust_boundary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An ancestor is checked for ownership too, not only for its mode.

    A stranger's 0755 directory passes a mode check today and is a directory
    that stranger may chmod tomorrow, and then replace everything under it. The
    state root, a user configuration directory and a launcher all sit at the
    bottom of such a chain, so all three have to refuse it."""
    from agentic_hil.config import secure_user_directory, trusted_persistent_executable

    middle = tmp_path / "middle"
    leaf = middle / "leaf"
    leaf.mkdir(parents=True, mode=0o700)
    middle.chmod(0o755)
    launcher = leaf / "agentic-hil"
    launcher.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    launcher.chmod(0o700)

    # Owned by us, every component 0755 or tighter: all three accept it.
    assert validated_state_root(str(leaf), tmp_path / "workspace", "config.yaml") == leaf
    assert secure_user_directory(leaf) == leaf
    assert trusted_persistent_executable(str(launcher), workspace=tmp_path / "workspace") == str(launcher)

    _report_foreign_owner(monkeypatch, middle)

    with pytest.raises(ConfigError) as state_root_error:
        validated_state_root(str(leaf), tmp_path / "workspace", "config.yaml")
    assert state_root_error.value.to_dict()["error_type"] == "unsafe_configured_path"
    with pytest.raises(ConfigError) as user_directory_error:
        secure_user_directory(leaf)
    assert user_directory_error.value.to_dict()["error_type"] == "unsafe_configured_path"
    with pytest.raises(ConfigError) as launcher_error:
        trusted_persistent_executable(str(launcher), workspace=tmp_path / "workspace")
    assert launcher_error.value.to_dict()["error_type"] == "mcp_command_untrusted"


@pytest.mark.skipif(os.name == "nt", reason="POSIX ownership rules")
def test_a_root_owned_system_ancestor_is_still_accepted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The rule is "root or us", not "us": /, /home and /tmp belong to root."""
    from agentic_hil.config import secure_user_directory

    leaf = tmp_path / "state"
    leaf.mkdir(mode=0o700)
    real_stat, real_fstat = os.stat, os.fstat
    inode = tmp_path.stat().st_ino

    def as_root(info: os.stat_result) -> os.stat_result:
        fields = list(info)
        fields[4] = 0
        return os.stat_result(fields)

    monkeypatch.setattr(os, "stat", lambda target, *a, **k: (lambda info: as_root(info) if info.st_ino == inode else info)(real_stat(target, *a, **k)))
    monkeypatch.setattr(os, "fstat", lambda fd: (lambda info: as_root(info) if info.st_ino == inode else info)(real_fstat(fd)))

    assert validated_state_root(str(leaf), tmp_path / "workspace", "config.yaml") == leaf
    assert secure_user_directory(leaf) == leaf


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_a_world_writable_debugger_executable_is_refused_and_never_spawned(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The file a `.py` executable names is run through this interpreter.

    Reading a bench needs no device permission, so `debugger_info` spawns the
    configured program on a board whose every grant is false. A program another
    local user may rewrite is therefore refused before anything can spawn it."""
    spawned: list[list[str]] = []
    monkeypatch.setattr("agentic_hil.backends.common.spawn_managed_process", lambda command, **kwargs: spawned.append(command))
    attacker_owned = tmp_path.parent / f"attacker-{tmp_path.name}.py"
    attacker_owned.write_text("print('pwned')\n", encoding="utf-8")
    attacker_owned.chmod(0o777)
    workspace = tmp_path / "workspace"
    write_authoritative_config(workspace, monkeypatch, debugger_type="stlink", debugger_executable=attacker_owned)

    with pytest.raises(ConfigError) as error:
        load_authoritative_config(workspace)

    detail = error.value.to_dict()
    assert detail["error_type"] == "unsafe_configured_path"
    assert detail["field"] == "debuggers.dut.executable"
    assert spawned == [], "an untrusted executable must never reach a spawn"


@pytest.mark.skipif(os.name == "nt", reason="POSIX ownership rules")
def test_a_foreign_owned_debugger_executable_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    stranger_owned = tmp_path.parent / f"stranger-{tmp_path.name}.py"
    stranger_owned.write_text("print('pwned')\n", encoding="utf-8")
    stranger_owned.chmod(0o700)
    workspace = tmp_path / "workspace"
    write_authoritative_config(workspace, monkeypatch, debugger_type="stlink", debugger_executable=stranger_owned)
    _report_foreign_owner(monkeypatch, stranger_owned)

    with pytest.raises(ConfigError) as error:
        load_authoritative_config(workspace)

    assert error.value.to_dict()["error_type"] == "unsafe_configured_path"


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_a_group_writable_openocd_script_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An OpenOCD interface/target script is Tcl that OpenOCD executes."""
    workspace = tmp_path / "workspace"
    write_authoritative_config(workspace, monkeypatch)
    script = Path(os.environ["AGENTIC_HIL_CONFIG"]).parent / "interface.cfg"
    script.chmod(0o664)

    with pytest.raises(ConfigError) as error:
        load_authoritative_config(workspace)

    detail = error.value.to_dict()
    assert detail["error_type"] == "unsafe_configured_path"
    assert detail["field"] == "debuggers.dut.interface_cfg"


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_a_writable_openocd_script_is_refused_on_a_bench_that_granted_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Version 2 grants no permission and still reaches the probe.

    Reading needs no grant from version 2 on, so `probe_target` and a debug
    attach run with every flag false — and both hand these scripts to
    `openocd -f`. Validating them only when a grant was present left the
    deny-by-default configuration executing Tcl any local user could rewrite."""
    workspace = tmp_path / "workspace"
    write_authoritative_config(workspace, monkeypatch, debugger_executable=FAKE_OPENOCD, permissions={}, config_version=2)
    script = Path(os.environ["AGENTIC_HIL_CONFIG"]).parent / "interface.cfg"
    script.chmod(0o664)
    monkeypatch.setattr(subprocess, "Popen", _no_process_started)

    with pytest.raises(ConfigError) as error:
        load_authoritative_config(workspace)

    detail = error.value.to_dict()
    assert detail["error_type"] == "unsafe_configured_path"
    assert detail["field"] == "debuggers.dut.interface_cfg"


def _no_process_started(*args: object, **kwargs: object) -> None:
    raise AssertionError("a configuration refused at load must not have started a process")


def test_a_read_free_bench_that_granted_nothing_still_pins_its_openocd_scripts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other half: a trusted script is pinned to the file that was checked."""
    workspace = tmp_path / "workspace"
    write_authoritative_config(workspace, monkeypatch, debugger_executable=FAKE_OPENOCD, permissions={}, config_version=2)
    config_root = Path(os.environ["AGENTIC_HIL_CONFIG"]).parent

    config = load_authoritative_config(workspace)

    assert config.debugger.permissions.allow_flash is False
    assert config.debugger.interface_cfg == str((config_root / "interface.cfg").resolve())
    assert config.debugger.target_cfg == str((config_root / "target.cfg").resolve())


def test_a_relative_openocd_script_may_not_climb_out_of_the_search_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bench with no grant keeps naming OpenOCD's own bundled scripts.

    `interface/stlink.cfg` is resolved by OpenOCD inside the installation whose
    executable was already validated, so it is left alone. `../` leaves that
    installation, which is a file nothing here vouched for."""
    workspace = tmp_path / "workspace"
    config_path = write_authoritative_config(
        workspace, monkeypatch, debugger_executable=FAKE_OPENOCD, permissions={}, config_version=2
    )
    original = config_path.read_text(encoding="utf-8")
    config_path.write_text(
        original.replace((config_path.parent / "interface.cfg").as_posix(), "interface/stlink.cfg"), encoding="utf-8"
    )

    assert load_authoritative_config(workspace).debugger.interface_cfg == "interface/stlink.cfg"

    config_path.write_text(
        original.replace((config_path.parent / "interface.cfg").as_posix(), "../../attacker/stlink.cfg"), encoding="utf-8"
    )
    with pytest.raises(ConfigError) as error:
        load_authoritative_config(workspace)

    detail = error.value.to_dict()
    assert detail["error_type"] == "config_invalid"
    assert detail["field"] == "debuggers.dut.interface_cfg"


# ---------------------------------------------------------------------------
# What a direct CAN adapter is actually told, and what it refuses to be told.


class _FakeCanMessage:
    def __init__(self, **fields: object) -> None:
        self.fields = fields


class _FakeCanBus:
    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs
        self.sent: list[_FakeCanMessage] = []

    def send(self, message: _FakeCanMessage, timeout: float | None = None) -> None:
        self.sent.append(message)

    def shutdown(self) -> None:
        return None


def _fake_can_module(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    opened: list[_FakeCanBus] = []

    def bus(**kwargs: object) -> _FakeCanBus:
        created = _FakeCanBus(**kwargs)
        opened.append(created)
        return created

    module = SimpleNamespace(
        Bus=bus,
        Message=lambda **fields: _FakeCanMessage(**fields),
        BusState=SimpleNamespace(ACTIVE="ACTIVE", PASSIVE="PASSIVE"),
        opened=opened,
    )
    monkeypatch.setitem(sys.modules, "can", module)
    return module


def test_a_passive_peak_bus_is_opened_passive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`listen_only: true` has to reach the backend, or it is not listen-only.

    A PEAK bus opened without it acknowledges every frame on the wire with a
    dominant bit while the configuration says the bench only observes."""
    from agentic_hil.can import open_python_can_adapter

    module = _fake_can_module(monkeypatch)
    config = load_test_config(tmp_path, can_buses_yaml='can_buses:\n  bench:\n    adapter: "peak"\n    channel: "PCAN_USBBUS1"\n    listen_only: true\n')

    result = open_python_can_adapter(config, "bench", config.can_buses["bench"], False)

    assert result["ok"] is True, result
    assert module.opened[0].kwargs["state"] == "PASSIVE"
    assert module.opened[0].kwargs["interface"] == "pcan"
    result["session"].close()


def test_a_socketcan_bus_that_asks_for_listen_only_is_refused_not_opened(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """python-can cannot put a SocketCAN interface into listen-only.

    Opening it anyway would leave a bus this configuration calls silent
    acknowledging every frame, so the session is refused instead."""
    from agentic_hil.can import open_python_can_adapter

    module = _fake_can_module(monkeypatch)
    config = load_test_config(tmp_path, can_buses_yaml='can_buses:\n  bench:\n    adapter: "socketcan"\n    channel: "vcan0"\n    listen_only: true\n')

    result = open_python_can_adapter(config, "bench", config.can_buses["bench"], False)

    assert result["ok"] is False
    assert result["error_type"] == "config_invalid"
    assert result["field"] == "can_buses.bench.listen_only"
    assert result["side_effect_committed"] is False
    assert module.opened == [], "a bus that cannot be told what it was configured with must not be opened"


@pytest.mark.parametrize(
    ("adapter", "extra", "field"),
    [
        ("peak", "    fd: true\n    data_bitrate: 2000000\n", "data_bitrate"),
        ("socketcan", "    fd: true\n    data_bitrate: 2000000\n", "data_bitrate"),
        ("peak", '    pcanbasic_dll: "/opt/pcan/libpcanbasic.so"\n', "pcanbasic_dll"),
    ],
)
def test_a_direct_adapter_refuses_the_fields_it_cannot_carry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, adapter: str, extra: str, field: str) -> None:
    from agentic_hil.can import open_python_can_adapter

    module = _fake_can_module(monkeypatch)
    channel = "PCAN_USBBUS1" if adapter == "peak" else "vcan0"
    config = load_test_config(tmp_path, can_buses_yaml=f'can_buses:\n  bench:\n    adapter: "{adapter}"\n    channel: "{channel}"\n{extra}')

    result = open_python_can_adapter(config, "bench", config.can_buses["bench"], False)

    assert result["ok"] is False
    assert result["error_type"] == "config_invalid"
    assert result["field"] == f"can_buses.bench.{field}"
    assert module.opened == []


def test_an_fd_bus_sends_fd_frames_and_carries_more_than_eight_bytes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An FD configuration defaults to 64-byte payloads, so the frames it sends
    have to say they are FD; a classical frame cannot carry them at all."""
    from agentic_hil.can import CanFrame, open_python_can_adapter

    _fake_can_module(monkeypatch)
    config = load_test_config(tmp_path, can_buses_yaml='can_buses:\n  bench:\n    adapter: "socketcan"\n    channel: "vcan0"\n    fd: true\n')
    bus_config = config.can_buses["bench"]
    _socketcan_link(monkeypatch, bitrate=bus_config.bitrate)
    assert bus_config.max_frame_data_bytes == 64

    result = open_python_can_adapter(config, "bench", bus_config, False)
    session = result["session"]
    try:
        payload = bytes(range(16))
        assert payload_frame(bus_config, {"frame_id": "0x123", "data_hex": payload.hex()})["ok"] is True
        assert session.send(CanFrame(id=0x123, extended=False, rtr=False, data=payload))["ok"] is True
    finally:
        session.close()

    sent = session.bus.sent[0].fields
    assert sent["is_fd"] is True
    assert sent["data"] == payload


def test_a_classical_bus_configured_for_fd_payloads_is_refused_at_load(tmp_path: Path) -> None:
    """A classical CAN data field is eight bytes wide, whatever the file says.

    The schema allows up to 64 because CAN FD reaches that; the pairing with
    `fd` is what it cannot express, so it is checked here instead of leaving a
    bus configured to send frames the wire cannot hold."""
    with pytest.raises(ConfigError) as rejected:
        load_test_config(tmp_path, can_buses_yaml='can_buses:\n  bench:\n    adapter: "socketcan"\n    channel: "vcan0"\n    max_frame_data_bytes: 64\n')

    assert rejected.value.error_type == "config_invalid"
    assert rejected.value.details["field"] == "can_buses.bench.max_frame_data_bytes"
    assert rejected.value.details["maximum"] == 8


def test_a_classical_bus_refuses_an_over_eight_byte_payload_before_the_adapter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The same limit immediately before dispatch.

    A `CanBusConfig` reaching this process any other way must not hand nine
    bytes to an adapter opened with `is_fd=False`: the frame is rejected,
    truncated or malformed, and the audit would record bytes that never went
    out."""
    from agentic_hil.can import CanBusSession

    config = load_test_config(tmp_path, can_buses_yaml='can_buses:\n  bench:\n    adapter: "socketcan"\n    channel: "vcan0"\n')
    classical = replace(config.can_buses["bench"], max_frame_data_bytes=64)
    assert classical.fd is False

    refused = payload_frame(classical, {"frame_id": "0x123", "data_hex": bytes(range(9)).hex()})

    assert refused["ok"] is False
    assert refused["error_type"] == "invalid_argument"
    assert refused["max_frame_data_bytes"] == 8
    assert refused["bytes_requested"] == 9
    assert payload_frame(classical, {"frame_id": "0x123", "data_hex": bytes(range(8)).hex()})["ok"] is True

    class _RecordingAdapter:
        adapter_name = "fake"

        def __init__(self) -> None:
            self.sent: list[object] = []

        def send(self, frame: object) -> dict:
            self.sent.append(frame)
            return {"ok": True}

        def read(self, max_frames: int, wait_timeout_s: float) -> dict:
            return {"ok": True, "frames": []}

        def status(self) -> dict:
            return {"active": True}

        def close(self) -> dict:
            return {"safe_state_confirmed": True, "process_reaped": True}

    adapter = _RecordingAdapter()
    service = CanBusService(config)
    try:
        service.sessions["bench"] = CanBusSession("bench", classical, adapter, str(tmp_path / "can-classical.jsonl"))
        sent = service.send("bench", {"frame_id": "0x123", "data_hex": bytes(range(9)).hex()})

        assert sent["ok"] is False
        assert sent["error_type"] == "invalid_argument"
        assert adapter.sent == [], "a frame the protocol cannot carry must not reach the adapter"
    finally:
        service.close()


def test_a_classical_bus_still_sends_classical_frames(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from agentic_hil.can import CanFrame, open_python_can_adapter

    _fake_can_module(monkeypatch)
    config = load_test_config(tmp_path, can_buses_yaml='can_buses:\n  bench:\n    adapter: "socketcan"\n    channel: "vcan0"\n')
    _socketcan_link(monkeypatch, bitrate=config.can_buses["bench"].bitrate)

    result = open_python_can_adapter(config, "bench", config.can_buses["bench"], False)
    session = result["session"]
    try:
        session.send(CanFrame(id=0x1, extended=False, rtr=False, data=b"\x01"))
    finally:
        session.close()

    assert session.bus.sent[0].fields["is_fd"] is False


# ---------------------------------------------------------------------------
# SocketCAN bit timing belongs to the interface, so it is checked there.


def _socketcan_link(monkeypatch: pytest.MonkeyPatch, **fields: object):
    """Answer the link query with a described interface instead of this host's."""
    from agentic_hil import can as can_module

    link = can_module.SocketCanLink(**{"channel": "vcan0", "found": True, "kind": "can", "bitrate": None, "reason": "", **fields})
    monkeypatch.setattr(can_module, "socketcan_link", lambda channel, *args, **kwargs: link)
    return link


def test_a_socketcan_interface_timed_differently_is_refused_before_it_is_opened(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """python-can cannot carry a bitrate to SocketCAN at all.

    Linux CAN timing is set on the interface, so a configuration naming 500000
    over a 250 kbit/s interface used to open silently and then either hear
    nothing or put error frames on somebody else's bus, while every report kept
    repeating the number nobody applied."""
    from agentic_hil.can import open_python_can_adapter

    module = _fake_can_module(monkeypatch)
    _socketcan_link(monkeypatch, bitrate=250000)
    config = load_test_config(tmp_path, can_buses_yaml='can_buses:\n  bench:\n    adapter: "socketcan"\n    channel: "vcan0"\n    bitrate: 500000\n')

    result = open_python_can_adapter(config, "bench", config.can_buses["bench"], False)

    assert result["ok"] is False
    assert result["error_type"] == "config_invalid"
    assert result["field"] == "can_buses.bench.bitrate"
    assert result["configured_bitrate"] == 500000
    assert result["interface_bitrate"] == 250000
    assert result["side_effect_committed"] is False
    assert module.opened == [], "a bus timed differently from this configuration must not be opened"


def test_an_unconfigured_socketcan_interface_is_refused_too(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A CAN link that was never given timing reports bitrate 0 and cannot talk."""
    from agentic_hil.can import open_python_can_adapter

    module = _fake_can_module(monkeypatch)
    _socketcan_link(monkeypatch, bitrate=0)
    config = load_test_config(tmp_path, can_buses_yaml='can_buses:\n  bench:\n    adapter: "socketcan"\n    channel: "vcan0"\n')

    result = open_python_can_adapter(config, "bench", config.can_buses["bench"], False)

    assert result["ok"] is False
    assert result["field"] == "can_buses.bench.bitrate"
    assert "no bit timing configured" in result["summary"]
    assert module.opened == []


def test_a_matching_socketcan_interface_opens_and_is_reported_as_verified(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from agentic_hil.can import open_python_can_adapter

    module = _fake_can_module(monkeypatch)
    _socketcan_link(monkeypatch, bitrate=500000)
    config = load_test_config(tmp_path, can_buses_yaml='can_buses:\n  bench:\n    adapter: "socketcan"\n    channel: "vcan0"\n    bitrate: 500000\n')

    result = open_python_can_adapter(config, "bench", config.can_buses["bench"], False)
    try:
        assert result["ok"] is True, result
        assert result["bitrate_verified"] is True
        assert result["interface_bitrate"] == 500000
        assert result["session"].status()["bitrate_verified"] is True
        # Never handed to python-can, which discards it for this backend: the
        # interface is where the value was checked and where it holds.
        assert "bitrate" not in module.opened[0].kwargs
    finally:
        result["session"].close()


def test_an_unreadable_socketcan_timing_is_reported_rather_than_assumed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A virtual bus has no timing to report and no wire to disturb.

    It cannot contradict the configuration and cannot be mistimed, so it opens —
    saying the bitrate is unconfirmed rather than repeating it as fact."""
    from agentic_hil.can import open_python_can_adapter

    _fake_can_module(monkeypatch)
    _socketcan_link(monkeypatch, found=True, kind="vcan", bitrate=None, reason="interface vcan0 is of kind vcan, which carries no CAN bit timing")
    config = load_test_config(tmp_path, can_buses_yaml='can_buses:\n  bench:\n    adapter: "socketcan"\n    channel: "vcan0"\n')

    result = open_python_can_adapter(config, "bench", config.can_buses["bench"], False)
    try:
        assert result["ok"] is True, result
        assert result["bitrate_verified"] is False
        assert "vcan" in result["bitrate_unverified_reason"]
        assert result["session"].status()["bitrate_verified"] is False
    finally:
        result["session"].close()


@pytest.mark.parametrize(
    ("fields", "reason"),
    [
        ({"found": True, "kind": "can", "bitrate": None}, "interface can0 reports no bit timing"),
        ({"found": False, "kind": None, "bitrate": None}, "interface can0 was not found on this host"),
        ({"found": False, "kind": None, "bitrate": None}, "netlink is not available on this platform"),
    ],
)
def test_a_socketcan_bus_whose_timing_could_not_be_confirmed_is_not_opened(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fields: dict, reason: str
) -> None:
    """Unverified is not "matches".

    A physical `can` link with no readable timing, an interface that is not on
    this host and a netlink query that failed leave the same question open, and a
    bus opened on an answer nobody gave is the same wrong bus as one opened on a
    wrong answer: it hears nothing, or it puts error frames on somebody else's
    wire."""
    from agentic_hil.can import open_python_can_adapter

    module = _fake_can_module(monkeypatch)
    _socketcan_link(monkeypatch, channel="can0", reason=reason, **fields)
    config = load_test_config(tmp_path, can_buses_yaml='can_buses:\n  bench:\n    adapter: "socketcan"\n    channel: "can0"\n    bitrate: 500000\n')

    result = open_python_can_adapter(config, "bench", config.can_buses["bench"], False)

    assert result["ok"] is False, result
    assert result["error_type"] == "config_invalid"
    assert result["field"] == "can_buses.bench.bitrate"
    assert result["bitrate_verified"] is False
    assert result["bitrate_unverified_reason"] == reason
    assert result["configured_bitrate"] == 500000
    assert result["side_effect_committed"] is False
    assert module.opened == [], "a bus whose timing could not be confirmed must not be opened"


def test_a_can_session_refuses_to_start_on_an_unconfirmable_interface(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The refusal reaches the caller of can_session_start, not only the opener."""
    _fake_can_module(monkeypatch)
    _socketcan_link(monkeypatch, channel="can0", found=True, kind="can", bitrate=None, reason="interface can0 reports no bit timing")
    config = load_test_config(tmp_path, can_buses_yaml='can_buses:\n  bench:\n    adapter: "socketcan"\n    channel: "can0"\n')
    service = CanBusService(config)
    try:
        started = service.session_start("bench")

        assert started["ok"] is False, started
        assert started["error_type"] == "config_invalid"
        assert started["field"] == "can_buses.bench.bitrate"
        assert service.sessions == {}
    finally:
        service.close()


def _netlink_attribute(kind: int, payload: bytes) -> bytes:
    header = struct.pack("=HH", 4 + len(payload), kind)
    body = header + payload
    return body + b"\x00" * (-len(body) % 4)


def _netlink_link_message(name: str, kind: str, bitrate: int | None) -> bytes:
    info = _netlink_attribute(1, kind.encode() + b"\x00")  # IFLA_INFO_KIND
    if bitrate is not None:
        bittiming = struct.pack("=8I", bitrate, 0, 0, 0, 0, 0, 0, 0)
        info += _netlink_attribute(2, _netlink_attribute(1, bittiming))  # IFLA_INFO_DATA / IFLA_CAN_BITTIMING
    attributes = _netlink_attribute(3, name.encode() + b"\x00") + _netlink_attribute(18, info)  # IFLA_IFNAME, IFLA_LINKINFO
    body = struct.pack("=BBHiII", 0, 0, 0, 1, 0, 0) + attributes
    return struct.pack("=IHHII", 16 + len(body), 16, 0, 1, 0) + body  # RTM_NEWLINK


def test_the_kernels_bit_timing_is_read_out_of_the_link_dump() -> None:
    """The wire format, tested without a CAN device on the machine."""
    from agentic_hil.can import socketcan_link_from_netlink

    stream = _netlink_link_message("can0", "can", 250000) + _netlink_link_message("can1", "can", 1000000)

    link = socketcan_link_from_netlink(stream, "can1")

    assert link.found is True
    assert link.kind == "can"
    assert link.bitrate == 1000000


@pytest.mark.parametrize(
    ("channel", "kind", "bitrate"),
    [("vcan0", "vcan", None), ("can0", "can", None)],
)
def test_a_link_without_readable_timing_verifies_nothing(channel: str, kind: str, bitrate: int | None) -> None:
    from agentic_hil.can import socketcan_link_from_netlink

    link = socketcan_link_from_netlink(_netlink_link_message(channel, kind, bitrate), channel)

    assert link.found is True
    assert link.bitrate is None
    assert link.status() == {"bitrate_verified": False, "bitrate_unverified_reason": link.reason}


def test_an_absent_interface_is_not_a_verified_match() -> None:
    from agentic_hil.can import socketcan_link_from_netlink

    link = socketcan_link_from_netlink(_netlink_link_message("can0", "can", 500000), "can9")

    assert link.found is False
    assert link.bitrate is None


# ---------------------------------------------------------------------------
# A stimulus that was only half delivered is not a stimulus that was delivered.


class _ShortWriteSerial:
    """A handle that accepts a bounded number of bytes per write, like a driver
    whose transmit buffer is full and whose write timeout has expired."""

    is_open = True
    in_waiting = 0

    def __init__(self, accept_per_write: list[int]) -> None:
        self.accept_per_write = list(accept_per_write)
        self.written = bytearray()

    def write(self, data: bytes) -> int:
        accepted = self.accept_per_write.pop(0) if self.accept_per_write else 0
        accepted = min(accepted, len(data))
        self.written.extend(data[:accepted])
        return accepted

    def flush(self) -> None:
        return None

    def read(self, size: int) -> bytes:
        return b""

    def close(self) -> None:
        self.is_open = False


def _com_session(tmp_path: Path, handle: object) -> tuple[ComPortService, ComPortSession]:
    config = load_test_config(tmp_path, com_ports_yaml=COM_PORT_YAML)
    service = ComPortService(config)
    session = ComPortSession("dut", config.com_ports["dut"], handle, str(tmp_path / "com.jsonl"), start_reader=False)
    service.sessions["dut"] = session
    return service, session


def test_a_short_serial_write_is_not_reported_as_delivered(tmp_path: Path) -> None:
    """The count pyserial returns is the only evidence of what went out.

    Discarding it turns a truncated stimulus into a green result and an audit
    log that disagrees with the wire — the exact shape of a false green."""
    handle = _ShortWriteSerial([3])
    service, session = _com_session(tmp_path, handle)
    try:
        result = service.write_bytes("dut", b"ABCDEFGH")
    finally:
        service.close()

    assert result["ok"] is False
    assert result["error_type"] == "serial_write_incomplete"
    assert result["bytes_written"] == 3
    assert result["bytes_requested"] == 8
    assert result["side_effect_committed"] is True
    assert result["side_effect_status"] == "partial"
    assert result["retry_safe"] is False
    assert result["quarantined"] is True
    assert handle.written == b"ABC"
    logged = [json.loads(line) for line in Path(session.log_path).read_text(encoding="utf-8").splitlines()]
    transmitted = [entry for entry in logged if entry.get("direction") == "tx"]
    assert [entry["bytes"] for entry in transmitted] == [3]
    assert [entry["hex"] for entry in transmitted] == [b"ABC".hex()], "only confirmed bytes may be audited"


def test_a_write_the_port_finishes_on_a_later_attempt_is_a_success(tmp_path: Path) -> None:
    """A short write is ordinary with `write_timeout_s: 0`; the remainder is
    offered again inside a bounded deadline rather than failed immediately."""
    handle = _ShortWriteSerial([3, 0, 5])
    service, session = _com_session(tmp_path, handle)
    try:
        result = service.write_bytes("dut", b"ABCDEFGH")
    finally:
        service.close()

    assert result["ok"] is True, result
    assert result["bytes_written"] == 8
    assert handle.written == b"ABCDEFGH"
    logged = [json.loads(line) for line in Path(session.log_path).read_text(encoding="utf-8").splitlines()]
    assert [entry["bytes"] for entry in logged if entry.get("direction") == "tx"] == [8]


def test_a_serial_handle_that_reports_no_count_confirms_nothing(tmp_path: Path) -> None:
    """An unknown count is not a delivery, and must not be recorded as one."""
    handle = SimpleNamespace(is_open=True, in_waiting=0, write=lambda data: None, flush=lambda: None, close=lambda: None)
    service, session = _com_session(tmp_path, handle)
    try:
        result = service.write_bytes("dut", b"ABCDEFGH")
    finally:
        service.close()

    assert result["ok"] is False
    assert result["error_type"] == "serial_write_incomplete"
    assert result["bytes_written"] == 0
    assert result["side_effect_committed"] is False
    assert result["retry_safe"] is True
    logged = [json.loads(line) for line in Path(session.log_path).read_text(encoding="utf-8").splitlines()]
    assert [entry["bytes"] for entry in logged if entry.get("direction") == "tx"] == [0]


@pytest.mark.skipif(os.name == "nt", reason="POSIX umask")
def test_the_test_sandbox_is_owner_only_whatever_the_ambient_umask_is(tmp_path: Path) -> None:
    """The suite's own sandbox stands in for a developer's home, config and
    state directories, and the product refuses any of them another user could
    write. Inheriting the ambient umask makes them 0775 on any machine using
    `umask 002`, which turns every path check in the suite into a failure that
    says nothing about the code under test."""
    from support import owner_only_directory, owner_only_write

    sandbox = Path(os.environ["XDG_STATE_HOME"]).parent
    for directory in (sandbox, sandbox / "home", sandbox / "config", sandbox / "state"):
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700, directory

    previous = os.umask(0o002)
    try:
        created = owner_only_directory(tmp_path / "nested" / "deep")
        written = owner_only_write(tmp_path / "nested" / "deep" / "user.json", "{}\n")
    finally:
        os.umask(previous)

    assert stat.S_IMODE(created.stat().st_mode) == 0o700
    assert stat.S_IMODE((tmp_path / "nested").stat().st_mode) == 0o700
    assert stat.S_IMODE(written.stat().st_mode) == 0o600
