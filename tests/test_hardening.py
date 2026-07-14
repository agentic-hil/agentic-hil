from __future__ import annotations

import json
import os
import threading
import time
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import FAKE_OPENOCD, write_config

from agentic_hil.adapters import AdapterService, AdapterSession
from agentic_hil.artifacts import ArtifactManager
from agentic_hil.bridge import ProcessBridgeSession
from agentic_hil.can import parse_can_id, payload_frame
from agentic_hil.comports import ComPortService, ComPortSession
from agentic_hil.comstdio import run_com_stdio
from agentic_hil.config import ConfigError, apply_trusted_policy, load_config, load_trusted_policy
from agentic_hil.mcp import handle_mcp_message
from agentic_hil.stdio import run_stdio_server
from agentic_hil.tools import AgenticHILToolService
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


class ExplodingBridge:
    def __init__(self) -> None:
        self.close_attempted = False

    def status(self) -> dict:
        return {"active": True}

    def close(self) -> None:
        self.close_attempted = True
        raise RuntimeError("bridge already gone")


def test_adapter_service_close_stops_all_sessions_despite_bridge_failure(tmp_path: Path) -> None:
    config = load_test_config(tmp_path, adapters_yaml=ADAPTER_YAML)
    service = AdapterService(config)
    adapter_config = AdapterConfig(executable="python", args=[], timeout_s=1.0, channels=["temp"], faults=["open"])
    log_dir = tmp_path / ".agentic-hil" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    first = AdapterSession("a1", adapter_config, ExplodingBridge(), str(log_dir / "a1.jsonl"))
    second = AdapterSession("a2", adapter_config, ExplodingBridge(), str(log_dir / "a2.jsonl"))
    service.sessions.update({"a1": first, "a2": second})

    service.close()

    assert first.bridge.close_attempted and second.bridge.close_attempted
    assert first.active is False and second.active is False


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


@pytest.mark.parametrize("reset_after_flash", [False, True])
def test_flash_requires_reset_permission(tmp_path: Path, reset_after_flash: bool) -> None:
    service = AgenticHILToolService(
        load_test_config(tmp_path, permissions_yaml="permissions:\n  allow_flash: true\n  allow_reset: false\n")
    )

    result = service.call("flash_firmware", {"image_path": "build/app.elf", "reset_after_flash": reset_after_flash})

    assert result["ok"] is False
    assert result["error_type"] == "permission_denied"


PERMISSION_GATE_CASES = [
    ("allow_probe", "probe_target", {}),
    ("allow_probe", "debugger_info", {}),
    ("allow_flash", "flash_firmware", {"image_path": "build/app.elf"}),
    ("allow_reset", "reset_target", {"mode": "run"}),
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


def test_live_reload_cannot_enable_startup_denied_permission(tmp_path: Path) -> None:
    config_path = write_config(tmp_path, permissions_yaml="permissions:\n  allow_probe: false\n")
    service = AgenticHILToolService(load_config(str(config_path), str(tmp_path)))
    try:
        config_text = config_path.read_text(encoding="utf-8")
        config_path.write_text(config_text.replace("allow_probe: false", "allow_probe: true"), encoding="utf-8")

        result = service.call("probe_target")
    finally:
        service.close()

    assert result["ok"] is False
    assert result["error_type"] == "permission_denied"


def test_live_reload_cannot_add_new_hardware_resource(tmp_path: Path) -> None:
    config_path = write_config(tmp_path)
    service = AgenticHILToolService(load_config(str(config_path), str(tmp_path)))
    try:
        config_text = config_path.read_text(encoding="utf-8")
        config_path.write_text(config_text.replace("com_ports: {}", COM_PORT_YAML.rstrip()), encoding="utf-8")

        result = service.call("com_ports_list")
    finally:
        service.close()

    assert result["ok"] is True
    assert "dut" not in result["ports"]


def test_trusted_policy_is_required_and_must_be_outside_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    policy_path = write_config(workspace)

    with pytest.raises(ConfigError) as missing:
        load_trusted_policy(None, str(workspace))
    assert missing.value.error_type == "trusted_policy_required"

    with pytest.raises(ConfigError) as inside:
        load_trusted_policy(str(policy_path.resolve()), str(workspace))
    assert inside.value.error_type == "trusted_policy_invalid"


def test_host_policy_caps_workspace_permissions_resources_and_validation(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    host = tmp_path / "host"
    project_path = write_config(
        workspace,
        com_ports_yaml=COM_PORT_YAML,
        permissions_yaml="permissions:\n  allow_probe: true\n  allow_com_read: true\n",
    )
    policy_path = write_config(
        host,
        permissions_yaml="permissions:\n  allow_probe: false\n  allow_com_read: true\n",
    )
    project = load_config(str(project_path), str(workspace))
    policy = load_trusted_policy(str(policy_path.resolve()), str(workspace))

    effective = apply_trusted_policy(project, policy)

    assert effective.permissions.allow_probe is False
    assert effective.permissions.allow_com_read is True
    assert effective.com_ports == {}
    assert effective.validation.require_allowed_root is True


def test_trusted_policy_missing_permissions_uses_deny_defaults(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    host = tmp_path / "host"
    project_path = write_config(workspace)
    policy_path = host / "policy.yaml"
    policy_path.parent.mkdir()
    policy_path.write_text("target: {}\n", encoding="utf-8")
    project = load_config(str(project_path), str(workspace))
    policy = load_trusted_policy(str(policy_path.resolve()), str(workspace))

    effective = apply_trusted_policy(project, policy)

    assert effective.permissions.allow_probe is False
    assert effective.permissions.allow_flash is False
    assert effective.permissions.allow_reset is False
    assert effective.artifacts.allow_upload is False


def test_trusted_policy_rejects_workspace_bridge_executable(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    host = tmp_path / "host"
    bridge = workspace / "bridge.py"
    bridge.parent.mkdir()
    bridge.write_text("print('untrusted')\n", encoding="utf-8")
    policy_path = write_config(
        host,
        adapters_yaml=f'adapters:\n  injected:\n    executable: "{bridge.as_posix()}"\n    channels: ["value"]\n',
        permissions_yaml="permissions: {}\n",
    )

    with pytest.raises(ConfigError) as rejected:
        load_trusted_policy(str(policy_path.resolve()), str(workspace))

    assert rejected.value.error_type == "trusted_policy_invalid"
    assert rejected.value.details["field"] == "adapters.injected.executable"


def test_disjoint_symbol_allowlists_deny_all_symbols(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    host = tmp_path / "host"
    project = load_config(str(write_config(workspace, allowed_symbols=["project_symbol"])), str(workspace))
    policy_path = write_config(host, allowed_symbols=["trusted_symbol"], permissions_yaml="permissions: {}\n")
    policy = load_trusted_policy(str(policy_path.resolve()), str(workspace))

    effective = apply_trusted_policy(project, policy)

    assert effective.debug.allow_all_symbols is False
    assert effective.debug.allowed_symbols == []


def test_trusted_policy_pins_debugger_and_openocd_scripts(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    host = tmp_path / "host"
    interface_cfg = host / "trusted-interface.cfg"
    target_cfg = host / "trusted-target.cfg"
    interface_cfg.parent.mkdir(parents=True)
    interface_cfg.write_text("# trusted interface\n", encoding="utf-8")
    target_cfg.write_text("# trusted target\n", encoding="utf-8")
    policy_path = write_config(
        host,
        debugger_executable=FAKE_OPENOCD,
        permissions_yaml="permissions:\n  allow_probe: true\n",
    )
    policy_text = policy_path.read_text(encoding="utf-8")
    policy_path.write_text(
        policy_text.replace("interface/stlink.cfg", interface_cfg.as_posix()).replace("target/stm32f4x.cfg", target_cfg.as_posix()),
        encoding="utf-8",
    )

    policy = load_trusted_policy(str(policy_path.resolve()), str(workspace))

    assert policy.debugger.executable == str(FAKE_OPENOCD.resolve())
    assert policy.debugger.interface_cfg == str(interface_cfg.resolve())
    assert policy.debugger.target_cfg == str(target_cfg.resolve())


def test_trusted_policy_rejects_relative_openocd_scripts_when_debugger_enabled(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    host = tmp_path / "host"
    policy_path = write_config(
        host,
        debugger_executable=FAKE_OPENOCD,
        permissions_yaml="permissions:\n  allow_probe: true\n",
    )

    with pytest.raises(ConfigError) as rejected:
        load_trusted_policy(str(policy_path.resolve()), str(workspace))

    assert rejected.value.error_type == "trusted_policy_invalid"
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

        validation = manager.validate_local_path("build/firmware.elf")
        staged = manager.stage_for_backend(validation["artifact"], "flash_firmware")
        assert staged["ok"] is True
        staged_path = Path(staged["artifact"]["resolved_path"])
        assert not staged_path.is_relative_to(tmp_path)
        assert staged_path.read_bytes() == firmware.read_bytes()
    finally:
        manager.close()
