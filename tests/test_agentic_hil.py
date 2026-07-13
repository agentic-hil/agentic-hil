from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import FAKE_STLINK_UNCONFIRMED, SIM_NTC_ADAPTER, write_config

from agentic_hil.artifacts import ArtifactManager
from agentic_hil.can import CanFrame, ProcessCanAdapterSession, open_python_can_adapter
from agentic_hil.cli import init_config, install_skill, mcp_config, schema
from agentic_hil.comports import ComPortService
from agentic_hil.config import ConfigError, load_config
from agentic_hil.mcp import MCP_PROTOCOL_VERSION, MCP_TOOL_NAMES, MCP_TOOLS, handle_mcp_message
from agentic_hil.tools import AgenticHILToolService


def mcp_tool_call(service: AgenticHILToolService, name: str, arguments: dict | None = None) -> dict:
    response = handle_mcp_message(
        {"jsonrpc": "2.0", "id": name, "method": "tools/call", "params": {"name": name, "arguments": arguments or {}}},
        service,
    )
    assert isinstance(response, dict)
    return response["result"]["structuredContent"]


def test_init_config_writes_starter_config(tmp_path: Path) -> None:
    config_path = tmp_path / ".agentic-hil" / "config.yaml"
    result = init_config(str(config_path))
    assert result["ok"] is True
    assert "target:" in config_path.read_text(encoding="utf-8")


def test_schema_exports_bundled_config_schema(tmp_path: Path) -> None:
    schema_path = tmp_path / "config.schema.json"
    result = schema(str(schema_path))
    assert result["ok"] is True
    assert "Agentic HIL project configuration" in schema_path.read_text(encoding="utf-8")


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


def test_config_loads_defaults(tmp_path: Path) -> None:
    config = load_config(str(write_config(tmp_path)), str(tmp_path))
    assert config.target.name == "example-target"
    assert config.debugger.probe_id is None
    assert config.artifacts.allowed_extensions == [".elf", ".hex", ".bin"]
    assert config.can_buses == {}
    assert config.permissions.allow_can_read is True


def test_tool_call_fails_closed_when_reloaded_config_is_invalid(tmp_path: Path) -> None:
    config_path = write_config(tmp_path)
    service = AgenticHILToolService(load_config(str(config_path), str(tmp_path)))
    try:
        config_path.write_text("permissions: [invalid\n", encoding="utf-8")
        result = service.call("probe_target")
    finally:
        service.close()

    assert result["ok"] is False
    assert result["tool"] == "probe_target"
    assert result["error_type"] == "config_invalid"


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
        str(tmp_path),
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
    config = load_config(str(write_config(tmp_path, probe_id="STLINK123")), str(tmp_path))
    service = AgenticHILToolService(config)
    try:
        probe = mcp_tool_call(service, "probe_target")
    finally:
        service.close()
    assert probe["ok"] is True
    log_text = (tmp_path / probe["log_path"]).read_text(encoding="utf-8")
    assert "adapter serial STLINK123" in log_text


def test_openocd_flash_defaults_to_no_reset_and_can_reset_explicitly(tmp_path: Path) -> None:
    firmware = tmp_path / "build" / "firmware.elf"
    firmware.parent.mkdir(parents=True)
    firmware.write_bytes(b"\x7fELFfake")
    config = load_config(str(write_config(tmp_path)), str(tmp_path))
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
    config = load_config(str(write_config(tmp_path, debugger_type="stlink", probe_id="STLINK123")), str(tmp_path))
    service = AgenticHILToolService(config)
    try:
        info = mcp_tool_call(service, "debugger_info")
        probe = mcp_tool_call(service, "probe_target")
        flash = mcp_tool_call(service, "flash_firmware", {"image_path": "build/firmware.elf"})
    finally:
        service.close()
    assert info["ok"] is True
    assert probe["ok"] is True
    assert flash["ok"] is True
    assert flash["operation_result"]["confirmed"] is True
    assert flash["reset_after_flash"] is False
    log_text = (tmp_path / flash["log_path"]).read_text(encoding="utf-8")
    assert "port=SWD" in log_text
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
        str(tmp_path),
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
    assert "firmware.elf" in flash_log
    assert reset["ok"] is True
    reset_log = (tmp_path / reset["log_path"]).read_text(encoding="utf-8")
    assert "reset halt" in reset_log


def test_pyocd_requires_flash_address_for_bin_artifacts(tmp_path: Path) -> None:
    firmware = tmp_path / "build" / "firmware.bin"
    firmware.parent.mkdir(parents=True)
    firmware.write_bytes(b"\x01\x02\x03\x04")
    config = load_config(str(write_config(tmp_path, debugger_type="pyocd")), str(tmp_path))
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
        str(tmp_path),
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
    config = load_config(str(write_config(tmp_path, debugger_type="stlink")), str(tmp_path))
    service = AgenticHILToolService(config)
    try:
        result = mcp_tool_call(service, "flash_firmware", {"image_path": "build/firmware.bin"})
    finally:
        service.close()
    assert result["ok"] is False
    assert result["error_type"] == "invalid_argument"
    assert "debugger.flash_address" in result["summary"]


def test_artifact_validation_computes_sha256(tmp_path: Path) -> None:
    config = load_config(str(write_config(tmp_path)), str(tmp_path))
    firmware = tmp_path / "build" / "firmware.elf"
    firmware.parent.mkdir(parents=True)
    data = b"\x7fELFfake"
    firmware.write_bytes(data)
    result = ArtifactManager(config).validate_local_path("build/firmware.elf")
    assert result["ok"] is True
    assert result["artifact"]["sha256"] == hashlib.sha256(data).hexdigest()
    assert result["validation"]["sha256_computed"] is True


def test_artifact_validation_blocks_outside_root(tmp_path: Path) -> None:
    config = load_config(str(write_config(tmp_path)), str(tmp_path))
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
        load_config(str(config_path), str(tmp_path))
    assert excinfo.value.error_type == "config_unreadable"


def test_load_config_reports_non_utf8_file_as_config_error(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_bytes(b"\xff\xfe\x00 broken")
    with pytest.raises(ConfigError) as excinfo:
        load_config(str(config_path), str(tmp_path))
    assert excinfo.value.error_type == "config_invalid"


def test_mcp_tool_registry_is_consistent(tmp_path: Path) -> None:
    assert [tool["name"] for tool in MCP_TOOLS] == MCP_TOOL_NAMES
    assert len(MCP_TOOL_NAMES) == 35
    assert len(set(MCP_TOOL_NAMES)) == 35
    assert all(not name.startswith("agentic_hil_") for name in MCP_TOOL_NAMES)
    config = load_config(str(write_config(tmp_path)), str(tmp_path))
    service = AgenticHILToolService(config)
    try:
        for name in MCP_TOOL_NAMES:
            result = service.call(name, {})
            assert result.get("error_type") != "unknown_tool", f"{name} is advertised but not dispatched"
    finally:
        service.close()


def test_mcp_initialize_rejects_unsupported_protocol_version(tmp_path: Path) -> None:
    config = load_config(str(write_config(tmp_path)), str(tmp_path))
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
        str(tmp_path),
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


def test_process_can_adapter_close_reaps_child() -> None:
    child = spawn_ignoring_bridge_child()
    session = ProcessCanAdapterSession(child)
    session.close()
    assert child.poll() is not None


def test_process_can_adapter_request_after_exit_returns_error() -> None:
    child = spawn_ignoring_bridge_child()
    session = ProcessCanAdapterSession(child)
    session.close()
    result = session.send(CanFrame(id=1, extended=False, rtr=False, data=b""))
    assert result["ok"] is False


NTC_ADAPTER_YAML = f'''adapters:
  ntc_sim:
    executable: "{SIM_NTC_ADAPTER.as_posix()}"
    channels: ["temperature", "resistance"]
    faults: ["open", "short_to_gnd", "short_to_vcc"]
'''


def test_adapter_config_loads(tmp_path: Path) -> None:
    config = load_config(str(write_config(tmp_path, adapters_yaml=NTC_ADAPTER_YAML)), str(tmp_path))
    assert config.adapters["ntc_sim"].channels == ["temperature", "resistance"]
    assert config.adapters["ntc_sim"].faults == ["open", "short_to_gnd", "short_to_vcc"]
    assert config.permissions.allow_adapter_read is True
    assert config.permissions.allow_adapter_write is True


def test_adapter_set_value_measure_and_fault_roundtrip(tmp_path: Path) -> None:
    config = load_config(str(write_config(tmp_path, adapters_yaml=NTC_ADAPTER_YAML)), str(tmp_path))
    service = AgenticHILToolService(config)
    try:
        listed = mcp_tool_call(service, "adapters_list")
        assert listed["ok"] is True
        assert listed["adapters"]["ntc_sim"]["session_active"] is False

        started = mcp_tool_call(service, "adapter_session_start", {"adapter_id": "ntc_sim"})
        assert started["ok"] is True

        set_result = mcp_tool_call(service, "adapter_set_value", {"adapter_id": "ntc_sim", "channel": "temperature", "value": 85})
        assert set_result["ok"] is True

        measured = mcp_tool_call(service, "adapter_measure", {"adapter_id": "ntc_sim", "channel": "temperature"})
        assert measured["ok"] is True
        assert measured["value"] == 85.0

        injected = mcp_tool_call(service, "adapter_inject_fault", {"adapter_id": "ntc_sim", "fault": "open"})
        assert injected["ok"] is True
        open_resistance = mcp_tool_call(service, "adapter_measure", {"adapter_id": "ntc_sim", "channel": "resistance"})
        assert open_resistance["value"] >= 1e9

        cleared = mcp_tool_call(service, "adapter_clear_fault", {"adapter_id": "ntc_sim"})
        assert cleared["ok"] is True
        hot_resistance = mcp_tool_call(service, "adapter_measure", {"adapter_id": "ntc_sim", "channel": "resistance"})
        assert 500 < hot_resistance["value"] < 5000  # 10k NTC (B=3950) at 85 degC

        stopped = mcp_tool_call(service, "adapter_session_stop", {"adapter_id": "ntc_sim"})
        assert stopped["ok"] is True
    finally:
        service.close()


def test_adapter_rejects_unconfigured_channel_fault_and_bad_value(tmp_path: Path) -> None:
    config = load_config(str(write_config(tmp_path, adapters_yaml=NTC_ADAPTER_YAML)), str(tmp_path))
    service = AgenticHILToolService(config)
    try:
        started = mcp_tool_call(service, "adapter_session_start", {"adapter_id": "ntc_sim"})
        assert started["ok"] is True

        bad_channel = mcp_tool_call(service, "adapter_set_value", {"adapter_id": "ntc_sim", "channel": "voltage", "value": 3.3})
        assert bad_channel["ok"] is False
        assert bad_channel["error_type"] == "channel_not_configured"

        bad_fault = mcp_tool_call(service, "adapter_inject_fault", {"adapter_id": "ntc_sim", "fault": "stuck"})
        assert bad_fault["ok"] is False
        assert bad_fault["error_type"] == "fault_not_configured"

        bad_value = mcp_tool_call(service, "adapter_set_value", {"adapter_id": "ntc_sim", "channel": "temperature", "value": True})
        assert bad_value["ok"] is False
        assert bad_value["error_type"] == "invalid_argument"
    finally:
        service.close()


def test_adapter_requires_active_session(tmp_path: Path) -> None:
    config = load_config(str(write_config(tmp_path, adapters_yaml=NTC_ADAPTER_YAML)), str(tmp_path))
    service = AgenticHILToolService(config)
    try:
        result = mcp_tool_call(service, "adapter_set_value", {"adapter_id": "ntc_sim", "channel": "temperature", "value": 25})
    finally:
        service.close()
    assert result["ok"] is False
    assert result["error_type"] == "session_not_active"


def test_adapter_write_permission_denied(tmp_path: Path) -> None:
    config = load_config(
        str(
            write_config(
                tmp_path,
                adapters_yaml=NTC_ADAPTER_YAML,
                permissions_yaml='''permissions:
  allow_adapter_write: false
''',
            )
        ),
        str(tmp_path),
    )
    service = AgenticHILToolService(config)
    try:
        started = mcp_tool_call(service, "adapter_session_start", {"adapter_id": "ntc_sim"})
        assert started["ok"] is True
        denied = mcp_tool_call(service, "adapter_set_value", {"adapter_id": "ntc_sim", "channel": "temperature", "value": 25})
        assert denied["ok"] is False
        assert denied["error_type"] == "permission_denied"
        measured = mcp_tool_call(service, "adapter_measure", {"adapter_id": "ntc_sim", "channel": "temperature"})
        assert measured["ok"] is True
    finally:
        service.close()


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
        str(tmp_path),
    )
    result = open_python_can_adapter(config, "dut_can", config.can_buses["dut_can"], True)
    assert result["ok"] is False
    assert result["error_type"] == "config_invalid"
