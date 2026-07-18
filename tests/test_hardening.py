from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import FAKE_OPENOCD, SIM_NTC_ADAPTER, write_authoritative_config, write_config

from agentic_hil.adapters import AdapterService
from agentic_hil.artifacts import ArtifactManager
from agentic_hil.backends.gdbdebug import GdbDebugSessions
from agentic_hil.bridge import ProcessBridgeSession
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
)
from agentic_hil.gdbmi import GdbMiClient
from agentic_hil.mcp import handle_mcp_message
from agentic_hil.report import (
    append_jsonl,
    ensure_audit_ready,
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


def test_bridge_close_propagates_reap_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    child = SimpleNamespace(stdout=[], stderr=[], poll=lambda: 0)
    session = ProcessBridgeSession(child)
    monkeypatch.setattr(
        "agentic_hil.bridge.terminate_process_tree",
        lambda process, timeout: (_ for _ in ()).throw(subprocess.TimeoutExpired("bridge", timeout)),
    )

    with pytest.raises(subprocess.TimeoutExpired):
        session.close()

    assert session.closed is False


def test_adapter_service_retains_session_after_bridge_reap_failure(tmp_path: Path) -> None:
    adapters_yaml = f'adapters:\n  ntc:\n    executable: "{SIM_NTC_ADAPTER.as_posix()}"\n    channels: ["temperature"]\n'
    config = load_test_config(tmp_path, adapters_yaml=adapters_yaml)
    service = AdapterService(config)

    def fail_close() -> None:
        raise subprocess.TimeoutExpired("bridge", 1)

    bridge = SimpleNamespace(close=fail_close, status=lambda: {"active": True})
    session = SimpleNamespace(
        adapter_id="ntc",
        adapter_config=config.adapters["ntc"],
        bridge=bridge,
        log_path=str(tmp_path / ".agentic-hil" / "logs" / "adapter.jsonl"),
        started_at="now",
        active=True,
    )
    service.sessions["ntc"] = session

    result = service.session_stop("ntc")

    assert result["ok"] is False
    assert result["error_type"] == "adapter_bridge_close_failed"
    assert service.sessions["ntc"] is session
    assert session.active is True
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
    assert result["validation"]["allowed_root"] is False


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
    assert result["error_type"] == "unsafe_configured_path"


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
    assert result["error_type"] == "unsafe_configured_path"
    assert "must-not-be-read" not in json.dumps(result)


def test_parallel_jsonl_appends_keep_every_event_exactly_once(tmp_path: Path) -> None:
    log_path = tmp_path / "events.jsonl"

    with ThreadPoolExecutor(max_workers=32) as executor:
        list(executor.map(lambda event_id: append_jsonl(str(log_path), {"event_id": event_id}), range(1000)))

    events = [json.loads(line)["event_id"] for line in log_path.read_text(encoding="utf-8").splitlines()]
    assert len(events) == 1000
    assert sorted(events) == list(range(1000))


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
        result = manager.stage_for_backend(validation["artifact"], "flash_firmware")
    finally:
        manager.close()

    assert result["ok"] is False
    assert result["tool"] == "flash_firmware"
    assert result["error_type"] == "artifact_too_large"


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
