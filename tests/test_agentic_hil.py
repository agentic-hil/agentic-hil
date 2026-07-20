from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import (
    FAKE_STLINK_UNCONFIRMED,
    SIM_NTC_ADAPTER,
    write_authoritative_config,
    write_config,
)

from agentic_hil.artifacts import ArtifactManager
from agentic_hil.backends.pyocd import parse_pyocd_probes
from agentic_hil.backends.stlink import stlink_empty_result, stlink_probe_ids
from agentic_hil.can import CanFrame, ProcessCanAdapterSession, open_python_can_adapter
from agentic_hil.cli import (
    build_parser,
    doctor,
    entrypoint,
    init_config,
    initialized_config_path,
    install_skill,
    mcp_config,
    schema,
    test_schema,
)
from agentic_hil.comports import ComPortService
from agentic_hil.config import ConfigError, load_config
from agentic_hil.mcp import MCP_PROTOCOL_VERSION, MCP_TOOL_NAMES, MCP_TOOLS, handle_mcp_message
from agentic_hil.process import process_group_kwargs, register_process_group, terminate_process_tree
from agentic_hil.tools import AgenticHILToolService


def mcp_tool_call(service: AgenticHILToolService, name: str, arguments: dict | None = None) -> dict:
    response = handle_mcp_message(
        {"jsonrpc": "2.0", "id": name, "method": "tools/call", "params": {"name": name, "arguments": arguments or {}}},
        service,
    )
    assert isinstance(response, dict)
    return response["result"]["structuredContent"]


def test_init_config_writes_deterministic_deny_by_default_external_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    config_path = initialized_config_path(workspace)

    result = init_config()

    assert result["ok"] is True
    assert result["path"] == str(config_path)
    config_text = config_path.read_text(encoding="utf-8")
    assert f"workspace_root: {json.dumps(str(workspace.resolve()))}" in config_text
    assert "allow_probe: false" in config_text
    assert "allow_reset: false" in config_text
    assert "allow_upload: false" in config_text
    assert initialized_config_path(workspace) == config_path


def test_schema_exports_bundled_config_schema(tmp_path: Path) -> None:
    schema_path = tmp_path / "config.schema.json"
    result = schema(str(schema_path))
    assert result["ok"] is True
    assert "Agentic HIL configuration" in schema_path.read_text(encoding="utf-8")


def test_test_schema_exports_bundled_testconfig_schema(tmp_path: Path) -> None:
    schema_path = tmp_path / "testconfig.schema.json"
    result = test_schema(str(schema_path))
    assert result["ok"] is True
    assert "test reactor configuration" in schema_path.read_text(encoding="utf-8")

    denied = test_schema(str(schema_path))
    assert denied["ok"] is False
    assert denied["error_type"] == "schema_exists"

    assert test_schema(str(schema_path), force=True)["ok"] is True


def test_doctor_reports_named_debugger_selectors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "workspace"
    write_authoritative_config(
        workspace,
        monkeypatch,
        debuggers_yaml="debuggers:\n  probe_b:\n    type: openocd\n    resource_id: rb\n",
        devices_yaml="devices:\n  dut_a:\n    debugger: true\n  dut_b:\n    debugger: probe_b\n",
        permissions_yaml="permissions: {}\n",
    )
    monkeypatch.chdir(workspace)

    result = doctor()

    assert result["ok"] is True
    assert result["devices"]["dut_a"]["debugger"] == "default"
    assert result["devices"]["dut_b"]["debugger"] == "probe_b"


def test_mcp_config_writes_project_mcp_json(tmp_path: Path) -> None:
    output_path = tmp_path / ".mcp.json"
    result = mcp_config(str(output_path))
    assert result["ok"] is True
    content = json.loads(output_path.read_text(encoding="utf-8"))
    assert content["mcpServers"]["agentic-hil"]["command"] == "agentic-hil"
    assert "mcp-stdio" in content["mcpServers"]["agentic-hil"]["args"]


def test_mcp_config_refuses_overwrite_without_force(tmp_path: Path) -> None:
    output_path = tmp_path / ".mcp.json"
    output_path.write_text("{}", encoding="utf-8")
    result = mcp_config(str(output_path))
    assert result["ok"] is False
    assert result["error_type"] == "mcp_config_exists"
    result_forced = mcp_config(str(output_path), force=True)
    assert result_forced["ok"] is True


def test_mcp_stdio_reports_missing_discovered_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)

    exit_code = entrypoint(["mcp-stdio"])

    result = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert result["error_type"] == "config_file_not_found"


def test_mcp_stdio_passes_authoritative_config_to_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "workspace"
    config_path = write_authoritative_config(
        workspace, monkeypatch, permissions_yaml="permissions:\n  allow_probe: false\n"
    )
    monkeypatch.delenv("AGENTIC_HIL_CONFIG")
    received: dict = {}

    def fake_server(config):
        received["config"] = config
        return 0

    monkeypatch.chdir(workspace)
    monkeypatch.setattr("agentic_hil.cli.run_stdio_server", fake_server)

    exit_code = entrypoint(["mcp-stdio"])

    assert exit_code == 0
    assert received["config"].config_path == str(config_path.resolve())
    assert received["config"].permissions.allow_probe is False


def test_doctor_succeeds_when_debugger_check_is_disabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "workspace"
    config_path = write_authoritative_config(workspace, monkeypatch, permissions_yaml="permissions: {}\n")
    monkeypatch.chdir(workspace)

    result = doctor()

    assert result["ok"] is True
    assert result["debugger"]["skipped"] is True
    assert result["config_path"] == str(config_path.resolve())


def test_doctor_closes_tool_service_after_debugger_check(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "workspace"
    write_authoritative_config(workspace, monkeypatch)
    lifecycle: list[str] = []

    class DoctorService:
        def __init__(self, config, frontend: str) -> None:
            assert frontend == "doctor"
            lifecycle.append("created")

        def call(self, name: str) -> dict:
            assert name == "debugger_info"
            lifecycle.append("called")
            return {"ok": True}

        def close(self) -> None:
            lifecycle.append("closed")

    monkeypatch.chdir(workspace)
    monkeypatch.setattr("agentic_hil.cli.AgenticHILToolService", DoctorService)

    result = doctor()

    assert result["ok"] is True
    assert lifecycle == ["created", "called", "closed"]


def test_com_stdio_uses_authoritative_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "workspace"
    config_path = write_authoritative_config(
        workspace,
        monkeypatch,
        com_ports_yaml='com_ports:\n  dut_uart:\n    device: "COM_TEST"\n',
    )
    received: dict = {}

    def fake_com_stdio(config, port_id, **kwargs):
        received["config"] = config
        received["port_id"] = port_id
        return 0

    monkeypatch.chdir(workspace)
    monkeypatch.setattr("agentic_hil.cli.run_com_stdio", fake_com_stdio)

    exit_code = entrypoint(["com-stdio", "--port", "dut_uart"])

    assert exit_code == 0
    assert received["config"].config_path == str(config_path.resolve())
    assert received["port_id"] == "dut_uart"


def test_config_loads_defaults(tmp_path: Path) -> None:
    config = load_config(str(write_config(tmp_path)))
    assert config.target.name == "example-target"
    assert config.debugger.probe_id is None
    assert config.artifacts.allowed_extensions == [".elf", ".hex", ".bin"]
    assert config.can_buses == {}
    assert config.permissions.allow_can_read is True


def test_tool_service_keeps_startup_config_when_file_becomes_invalid(tmp_path: Path) -> None:
    config_path = write_config(tmp_path)
    service = AgenticHILToolService(load_config(str(config_path)))
    try:
        config_path.write_text("permissions: [invalid\n", encoding="utf-8")
        result = service.call("probe_target")
    finally:
        service.close()

    assert result["ok"] is True


def test_mcp_lists_configured_socketcan_buses_without_opening_hardware(tmp_path: Path) -> None:
    config = load_config(
        str(
            write_config(
                tmp_path,
                can_buses_yaml='''can_buses:
  dut_can:
    adapter: "socketcan"
    channel: "can0"
    bitrate: 500000
''',
            )
        ),
    )
    service = AgenticHILToolService(config)
    try:
        listed = mcp_tool_call(service, "can_buses_list")
    finally:
        service.close()
    assert listed["ok"] is True
    assert listed["buses"]["dut_can"]["adapter"] == "socketcan"
    assert "socketcan" in listed["supported_adapters"]


def test_openocd_passes_configured_probe_id(tmp_path: Path) -> None:
    config = load_config(str(write_config(tmp_path, probe_id="STLINK123")))
    service = AgenticHILToolService(config)
    try:
        probe = mcp_tool_call(service, "probe_target")
    finally:
        service.close()
    assert probe["ok"] is True
    log_text = (tmp_path / probe["log_path"]).read_text(encoding="utf-8")
    assert "adapter serial STLINK123" in log_text


def test_openocd_probe_listing_reports_not_supported(tmp_path: Path) -> None:
    service = AgenticHILToolService(load_config(str(write_config(tmp_path))))
    try:
        result = mcp_tool_call(service, "debugger_probes_list")
    finally:
        service.close()
    assert result["ok"] is False
    assert result["error_type"] == "not_supported"


def test_stlink_lists_all_connected_probe_ids(tmp_path: Path) -> None:
    config = load_config(str(write_config(tmp_path, debugger_type="stlink")))
    service = AgenticHILToolService(config)
    try:
        result = mcp_tool_call(service, "debugger_probes_list")
    finally:
        service.close()
    assert result["ok"] is True, result
    assert result["probes"] == [{"probe_id": "STLINK123"}, {"probe_id": "STLINK456"}]


def test_pyocd_lists_all_connected_probe_ids(tmp_path: Path) -> None:
    config = load_config(str(write_config(tmp_path, debugger_type="pyocd")))
    service = AgenticHILToolService(config)
    try:
        result = mcp_tool_call(service, "debugger_probes_list")
    finally:
        service.close()
    assert result["ok"] is True, result
    assert result["probes"] == [{"probe_id": "PYOCD123"}, {"probe_id": "PYOCD456"}]


def test_probe_listing_requires_probe_permission(tmp_path: Path) -> None:
    config = load_config(
        str(write_config(tmp_path, debugger_type="stlink", permissions_yaml="permissions:\n  allow_probe: false\n")),
    )
    service = AgenticHILToolService(config)
    try:
        result = mcp_tool_call(service, "debugger_probes_list")
    finally:
        service.close()
    assert result["ok"] is False
    assert result["error_type"] == "permission_denied"


def test_probe_listing_parsers_fail_closed() -> None:
    assert stlink_probe_ids("ST-LINK SN  : 001\nSTLink SN: 002\n") == ["001", "002"]
    assert stlink_empty_result("No ST-LINK detected") is True
    assert parse_pyocd_probes("not-json")["error_type"] == "probe_discovery_failed"
    assert parse_pyocd_probes('{"status": 1, "boards": []}')["error_type"] == "probe_discovery_failed"


def test_debugger_probes_cli_uses_authoritative_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    write_authoritative_config(tmp_path, monkeypatch, debugger_type="stlink")
    monkeypatch.chdir(tmp_path)

    exit_code = entrypoint(["debugger-probes"])

    result = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert result["probes"] == [{"probe_id": "STLINK123"}, {"probe_id": "STLINK456"}]


def test_openocd_flash_defaults_to_no_reset_and_can_reset_explicitly(tmp_path: Path) -> None:
    firmware = tmp_path / "build" / "firmware.elf"
    firmware.parent.mkdir(parents=True)
    firmware.write_bytes(b"\x7fELFfake")
    config = load_config(str(write_config(tmp_path)))
    service = AgenticHILToolService(config)
    try:
        no_reset = mcp_tool_call(service, "flash_firmware", {"image_path": "build/firmware.elf"})
        with_reset = mcp_tool_call(service, "flash_firmware", {"image_path": "build/firmware.elf", "reset_after_flash": True})
    finally:
        service.close()
    assert no_reset["ok"] is True
    assert no_reset["reset_after_flash"] is False
    no_reset_log = (tmp_path / no_reset["log_path"]).read_text(encoding="utf-8")
    assert "program" in no_reset_log
    assert "verify reset" not in no_reset_log
    assert with_reset["ok"] is True
    assert with_reset["reset_after_flash"] is True
    reset_log = (tmp_path / with_reset["log_path"]).read_text(encoding="utf-8")
    assert "verify reset" in reset_log


def test_stlink_backend_probes_and_flashes_with_probe_id(tmp_path: Path) -> None:
    firmware = tmp_path / "build" / "firmware.elf"
    firmware.parent.mkdir(parents=True)
    firmware.write_bytes(b"\x7fELFfake")
    config = load_config(str(write_config(tmp_path, debugger_type="stlink", probe_id="STLINK123")))
    service = AgenticHILToolService(config)
    try:
        info = mcp_tool_call(service, "debugger_info")
        probe = mcp_tool_call(service, "probe_target")
        flash = mcp_tool_call(service, "flash_firmware", {"image_path": "build/firmware.elf"})
    finally:
        service.close()
    assert info["ok"] is True
    assert probe["ok"] is True
    probe_log = (tmp_path / probe["log_path"]).read_text(encoding="utf-8")
    assert "mode=HOTPLUG" in probe_log
    assert flash["ok"] is True
    assert flash["operation_result"]["confirmed"] is True
    assert flash["reset_after_flash"] is False
    log_text = (tmp_path / flash["log_path"]).read_text(encoding="utf-8")
    assert "port=SWD" in log_text
    assert "mode=HOTPLUG" in log_text
    assert "sn=STLINK123" in log_text
    assert "-w" in log_text
    assert "-v" in log_text
    assert "-rst" not in log_text


def test_pyocd_backend_probes_flashes_and_resets_with_probe_and_target(tmp_path: Path) -> None:
    firmware = tmp_path / "build" / "firmware.elf"
    firmware.parent.mkdir(parents=True)
    firmware.write_bytes(b"\x7fELFfake")
    config = load_config(
        str(write_config(tmp_path, debugger_type="pyocd", probe_id="PYOCD123", target_type="stm32f446re")),
    )
    service = AgenticHILToolService(config)
    try:
        info = mcp_tool_call(service, "debugger_info")
        probe = mcp_tool_call(service, "probe_target")
        flash = mcp_tool_call(service, "flash_firmware", {"image_path": "build/firmware.elf", "reset_after_flash": True})
        reset = mcp_tool_call(service, "reset_target", {"mode": "halt"})
    finally:
        service.close()
    assert info["ok"] is True
    assert "0.36.0" in info["version"]
    assert probe["ok"] is True
    assert probe["target_detected"] is True
    probe_log = (tmp_path / probe["log_path"]).read_text(encoding="utf-8")
    assert "--uid PYOCD123" in probe_log
    assert "--target stm32f446re" in probe_log
    assert flash["ok"] is True
    assert flash["reset_after_flash"] is True
    flash_log = (tmp_path / flash["log_path"]).read_text(encoding="utf-8")
    assert "flash" in flash_log
    assert "--no-reset" in flash_log
    assert "firmware.elf" in flash_log
    assert reset["ok"] is True
    reset_log = (tmp_path / reset["log_path"]).read_text(encoding="utf-8")
    assert "reset halt" in reset_log


def test_pyocd_requires_flash_address_for_bin_artifacts(tmp_path: Path) -> None:
    firmware = tmp_path / "build" / "firmware.bin"
    firmware.parent.mkdir(parents=True)
    firmware.write_bytes(b"\x01\x02\x03\x04")
    config = load_config(str(write_config(tmp_path, debugger_type="pyocd")))
    service = AgenticHILToolService(config)
    try:
        result = mcp_tool_call(service, "flash_firmware", {"image_path": "build/firmware.bin"})
    finally:
        service.close()
    assert result["ok"] is False
    assert result["error_type"] == "invalid_argument"
    assert "debugger.flash_address" in result["summary"]


def test_stlink_rejects_unconfirmed_successful_exit(tmp_path: Path) -> None:
    config = load_config(
        str(write_config(tmp_path, debugger_type="stlink", debugger_executable=FAKE_STLINK_UNCONFIRMED)),
    )
    service = AgenticHILToolService(config)
    try:
        result = mcp_tool_call(service, "reset_target", {"mode": "run"})
    finally:
        service.close()
    assert result["ok"] is False
    assert result["error_type"] == "reset_failed"
    assert result["backend_error_type"] == "reset_unconfirmed"


def test_stlink_requires_flash_address_for_bin_artifacts(tmp_path: Path) -> None:
    firmware = tmp_path / "build" / "firmware.bin"
    firmware.parent.mkdir(parents=True)
    firmware.write_bytes(b"\x01\x02\x03\x04")
    config = load_config(str(write_config(tmp_path, debugger_type="stlink")))
    service = AgenticHILToolService(config)
    try:
        result = mcp_tool_call(service, "flash_firmware", {"image_path": "build/firmware.bin"})
    finally:
        service.close()
    assert result["ok"] is False
    assert result["error_type"] == "invalid_argument"
    assert "debugger.flash_address" in result["summary"]


def test_artifact_validation_computes_sha256(tmp_path: Path) -> None:
    config = load_config(str(write_config(tmp_path)))
    firmware = tmp_path / "build" / "firmware.elf"
    firmware.parent.mkdir(parents=True)
    data = b"\x7fELFfake"
    firmware.write_bytes(data)
    result = ArtifactManager(config).validate_local_path("build/firmware.elf")
    assert result["ok"] is True
    assert result["artifact"]["sha256"] == hashlib.sha256(data).hexdigest()
    assert result["validation"]["sha256_computed"] is True


def test_artifact_validation_blocks_outside_root(tmp_path: Path) -> None:
    config = load_config(str(write_config(tmp_path)))
    firmware = tmp_path / "other" / "firmware.elf"
    firmware.parent.mkdir(parents=True)
    firmware.write_bytes(b"\x7fELF")
    result = ArtifactManager(config).validate_local_path("other/firmware.elf")
    assert result["ok"] is False
    assert result["error_type"] == "artifact_validation_failed"


def test_skill_install_supports_agent_aliases(tmp_path: Path) -> None:
    target = tmp_path / "skills" / "agentic-hil-config-setup" / "SKILL.md"
    result = install_skill("open-code", str(target))
    assert result["ok"] is True
    assert result["agent"] == "opencode"
    assert "agentic_hil_version" in target.read_text(encoding="utf-8")


def test_load_config_reports_unreadable_path_as_config_error(tmp_path: Path) -> None:
    config_path = tmp_path / ".agentic-hil" / "config.yaml"
    config_path.mkdir(parents=True)  # a directory passes exists() but cannot be read
    with pytest.raises(ConfigError) as excinfo:
        load_config(str(config_path))
    assert excinfo.value.error_type == "config_unreadable"


def test_load_config_reports_non_utf8_file_as_config_error(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_bytes(b"\xff\xfe\x00 broken")
    with pytest.raises(ConfigError) as excinfo:
        load_config(str(config_path))
    assert excinfo.value.error_type == "config_invalid"


def test_mcp_tool_registry_is_consistent(tmp_path: Path) -> None:
    assert [tool["name"] for tool in MCP_TOOLS] == MCP_TOOL_NAMES
    assert len(MCP_TOOL_NAMES) == 36
    assert len(set(MCP_TOOL_NAMES)) == 36
    assert all(not name.startswith("agentic_hil_") for name in MCP_TOOL_NAMES)
    config = load_config(str(write_config(tmp_path)))
    service = AgenticHILToolService(config)
    try:
        for name in MCP_TOOL_NAMES:
            result = service.call(name, {})
            assert result.get("error_type") != "unknown_tool", f"{name} is advertised but not dispatched"
    finally:
        service.close()


def test_mcp_initialize_rejects_unsupported_protocol_version(tmp_path: Path) -> None:
    config = load_config(str(write_config(tmp_path)))
    service = AgenticHILToolService(config)
    try:
        response = handle_mcp_message(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "1999-01-01"}},
            service,
        )
    finally:
        service.close()
    assert isinstance(response, dict)
    assert response["result"]["protocolVersion"] == MCP_PROTOCOL_VERSION


def test_com_write_rejects_unencodable_text(tmp_path: Path) -> None:
    config = load_config(
        str(
            write_config(
                tmp_path,
                com_ports_yaml='''com_ports:
  dut_uart:
    device: "/dev/ttyNONEXISTENT"
    encoding: "ascii"
''',
            )
        ),
    )
    service = ComPortService(config)
    try:
        result = service.write("dut_uart", {"text": "Temperatur: 25 °C"})
    finally:
        service.close()
    assert result["ok"] is False
    assert result["error_type"] == "invalid_argument"


def spawn_ignoring_bridge_child() -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        text=True,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def pid_alive(pid: int) -> bool:
    if os.name == "nt":
        output = subprocess.run(["tasklist", "/FI", f"PID eq {pid}"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False).stdout
        return str(pid).encode("ascii") in output
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group cleanup regression")
def test_process_cleanup_kills_sigterm_ignoring_descendant(tmp_path: Path) -> None:
    pid_file = tmp_path / "descendant.pid"
    code = """
import signal, subprocess, sys, time
descendant = subprocess.Popen([sys.executable, '-c', 'import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)'])
open(sys.argv[1], 'w', encoding='utf-8').write(str(descendant.pid))
signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(SystemExit(0)))
time.sleep(30)
"""
    child = register_process_group(subprocess.Popen([sys.executable, "-c", code, str(pid_file)], **process_group_kwargs()))
    try:
        for _ in range(100):
            if pid_file.exists():
                break
            time.sleep(0.01)
        descendant_pid = int(pid_file.read_text(encoding="utf-8"))

        terminate_process_tree(child, 1.0)

        assert child.poll() is not None
        assert not pid_alive(descendant_pid)
    finally:
        if child.poll() is None:
            child.kill()


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group cleanup regression")
def test_process_cleanup_handles_exited_group_leader(tmp_path: Path) -> None:
    pid_file = tmp_path / "descendant.pid"
    code = """
import signal, subprocess, sys
descendant = subprocess.Popen([sys.executable, '-c', 'import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)'])
open(sys.argv[1], 'w', encoding='utf-8').write(str(descendant.pid))
"""
    child = register_process_group(subprocess.Popen([sys.executable, "-c", code, str(pid_file)], **process_group_kwargs()))
    try:
        child.wait(timeout=5)
        descendant_pid = int(pid_file.read_text(encoding="utf-8"))

        terminate_process_tree(child, 1.0)

        assert not pid_alive(descendant_pid)
    finally:
        if child.poll() is None:
            child.kill()


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object regression")
def test_registered_windows_process_cleanup_kills_descendant_after_leader_exit(tmp_path: Path) -> None:
    pid_file = tmp_path / "descendant.pid"
    code = """
import subprocess, sys
descendant = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
open(sys.argv[1], 'w', encoding='utf-8').write(str(descendant.pid))
"""
    child = register_process_group(subprocess.Popen([sys.executable, "-c", code, str(pid_file)], **process_group_kwargs()))
    child.wait(timeout=5)
    descendant_pid = int(pid_file.read_text(encoding="utf-8"))

    terminate_process_tree(child, 1.0)

    assert not pid_alive(descendant_pid)


@pytest.mark.skipif(os.name != "nt", reason="Windows suspended-start regression")
def test_windows_process_does_not_run_before_job_registration(tmp_path: Path) -> None:
    marker = tmp_path / "started"
    child = subprocess.Popen([sys.executable, "-c", "import pathlib,sys; pathlib.Path(sys.argv[1]).write_text('started')", str(marker)], **process_group_kwargs())
    try:
        time.sleep(0.1)
        assert not marker.exists()
        register_process_group(child)
        child.wait(timeout=5)
        assert marker.read_text(encoding="utf-8") == "started"
        terminate_process_tree(child, 1.0)
    finally:
        if child.poll() is None:
            child.kill()


@pytest.mark.skipif(os.name != "nt", reason="Windows fail-closed registration regression")
def test_windows_job_setup_failure_terminates_suspended_child(monkeypatch: pytest.MonkeyPatch) -> None:
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], **process_group_kwargs())
    monkeypatch.setattr("agentic_hil.process._create_windows_kill_job", lambda process: (_ for _ in ()).throw(OSError("job setup failed")))

    with pytest.raises(OSError, match="job setup failed"):
        register_process_group(child)

    assert child.poll() is not None


@pytest.mark.skipif(os.name != "nt", reason="Windows missing Job Object regression")
def test_windows_missing_job_handle_terminates_suspended_child(monkeypatch: pytest.MonkeyPatch) -> None:
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], **process_group_kwargs())
    monkeypatch.setattr("agentic_hil.process._create_windows_kill_job", lambda process: None)

    with pytest.raises(OSError, match="no containment handle"):
        register_process_group(child)

    assert child.poll() is not None


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object verification regression")
def test_windows_job_cleanup_reports_remaining_processes(monkeypatch: pytest.MonkeyPatch) -> None:
    child = SimpleNamespace(_agentic_hil_job_handle=123, wait=lambda timeout: 0, poll=lambda: 0)
    closed: list[int] = []
    monkeypatch.setattr("agentic_hil.process._terminate_windows_job", lambda handle: None)
    monkeypatch.setattr("agentic_hil.process._wait_for_windows_job", lambda handle, timeout: False)
    monkeypatch.setattr("agentic_hil.process._close_windows_handle", lambda handle: closed.append(handle))

    with pytest.raises(RuntimeError, match="retained active processes"):
        terminate_process_tree(child, 0.1)  # type: ignore[arg-type]

    assert closed == []
    assert child._agentic_hil_job_handle == 123

    monkeypatch.setattr("agentic_hil.process._wait_for_windows_job", lambda handle, timeout: True)
    terminate_process_tree(child, 0.1)  # type: ignore[arg-type]

    assert closed == [123]
    assert child._agentic_hil_job_handle is None
    assert child._agentic_hil_tree_reaped is True


def test_process_can_adapter_close_reaps_child() -> None:
    child = spawn_ignoring_bridge_child()
    session = ProcessCanAdapterSession(child)
    from agentic_hil.bridge import BridgeCleanupError

    with pytest.raises(BridgeCleanupError) as excinfo:
        session.close()
    assert excinfo.value.result["safe_state_confirmed"] is False
    assert child.poll() is not None


def test_process_can_adapter_request_after_exit_returns_error() -> None:
    child = spawn_ignoring_bridge_child()
    session = ProcessCanAdapterSession(child)
    from agentic_hil.bridge import BridgeCleanupError

    with pytest.raises(BridgeCleanupError):
        session.close()
    result = session.send(CanFrame(id=1, extended=False, rtr=False, data=b""))
    assert result["ok"] is False


def test_process_can_adapter_read_allows_bridge_response_overhead(monkeypatch: pytest.MonkeyPatch) -> None:
    session = object.__new__(ProcessCanAdapterSession)
    session.timeout_s = 5.0
    captured: dict[str, object] = {}

    def fake_request(method: str, params: dict, timeout_s: float) -> dict:
        captured.update({"method": method, "params": params, "timeout_s": timeout_s})
        return {"ok": True, "frames": []}

    monkeypatch.setattr(session, "request", fake_request)

    result = session.read(max_frames=8, wait_timeout_s=5.0)

    assert result["ok"] is True
    assert captured["timeout_s"] == 6.0


@pytest.mark.skipif(os.name == "nt", reason="POSIX-only PEAK channel validation")
def test_peak_adapter_on_posix_requires_socketcan_channel(tmp_path: Path) -> None:
    config = load_config(
        str(
            write_config(
                tmp_path,
                can_buses_yaml='''can_buses:
  dut_can:
    adapter: "peak"
    channel: "USBBUS1"
''',
            )
        ),
    )
    result = open_python_can_adapter(config, "dut_can", config.can_buses["dut_can"], True)
    assert result["ok"] is False
    assert result["error_type"] == "config_invalid"


NTC_ADAPTER_YAML = f'''adapters:
  ntc_sim:
    executable: "{SIM_NTC_ADAPTER.as_posix()}"
    channels: ["temperature", "resistance"]
    faults: ["open", "short_to_gnd", "short_to_vcc"]
'''


def test_adapter_api_roundtrip_remains_available(tmp_path: Path) -> None:
    service = AgenticHILToolService(load_config(str(write_config(tmp_path, adapters_yaml=NTC_ADAPTER_YAML))))
    try:
        assert mcp_tool_call(service, "adapters_list")["adapters"]["ntc_sim"]["session_active"] is False
        assert mcp_tool_call(service, "adapter_session_start", {"adapter_id": "ntc_sim"})["ok"] is True
        assert mcp_tool_call(service, "adapter_set_value", {"adapter_id": "ntc_sim", "channel": "temperature", "value": 85})["ok"] is True
        measured = mcp_tool_call(service, "adapter_measure", {"adapter_id": "ntc_sim", "channel": "temperature"})
        assert measured["value"] == 85.0
        invalid = mcp_tool_call(service, "adapter_set_value", {"adapter_id": "ntc_sim", "channel": "temperature", "value": float("nan")})
        assert invalid["error_type"] == "invalid_argument"
        huge = mcp_tool_call(service, "adapter_set_value", {"adapter_id": "ntc_sim", "channel": "temperature", "value": 10**10000})
        assert huge["error_type"] == "invalid_argument"
        assert mcp_tool_call(service, "adapter_inject_fault", {"adapter_id": "ntc_sim", "fault": "open"})["ok"] is True
        assert mcp_tool_call(service, "adapter_clear_fault", {"adapter_id": "ntc_sim"})["ok"] is True
        assert mcp_tool_call(service, "adapter_session_stop", {"adapter_id": "ntc_sim"})["ok"] is True
    finally:
        service.close()


@pytest.mark.parametrize("command", ["init", "doctor", "mcp-stdio", "com-stdio"])
def test_legacy_cli_config_option_is_parsed(command: str) -> None:
    arguments = [command, "--config", "legacy.yaml"]
    if command == "com-stdio":
        arguments.extend(["--port", "dut"])

    parsed = build_parser().parse_args(arguments)

    assert parsed.config == "legacy.yaml"
