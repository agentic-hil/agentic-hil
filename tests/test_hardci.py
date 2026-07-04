from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import FAKE_STLINK_UNCONFIRMED, write_config

from hardci.artifacts import ArtifactManager
from hardci.can import CanFrame, ProcessCanAdapterSession, open_python_can_adapter
from hardci.cli import init_config, install_skill, schema
from hardci.comports import ComPortService
from hardci.config import load_config
from hardci.mcp import MCP_PROTOCOL_VERSION, MCP_TOOL_NAMES, MCP_TOOLS, handle_mcp_message
from hardci.tools import HardCIToolService


def mcp_tool_call(service: HardCIToolService, name: str, arguments: dict | None = None) -> dict:
    response = handle_mcp_message(
        {"jsonrpc": "2.0", "id": name, "method": "tools/call", "params": {"name": name, "arguments": arguments or {}}},
        service,
    )
    assert isinstance(response, dict)
    return response["result"]["structuredContent"]


def test_init_config_writes_starter_config(tmp_path: Path) -> None:
    config_path = tmp_path / ".hardci" / "config.yaml"
    result = init_config(str(config_path))
    assert result["ok"] is True
    assert "target:" in config_path.read_text(encoding="utf-8")


def test_schema_exports_bundled_config_schema(tmp_path: Path) -> None:
    schema_path = tmp_path / "config.schema.json"
    result = schema(str(schema_path))
    assert result["ok"] is True
    assert "HardCI project configuration" in schema_path.read_text(encoding="utf-8")


def test_config_loads_defaults(tmp_path: Path) -> None:
    config = load_config(str(write_config(tmp_path)), str(tmp_path))
    assert config.target.name == "example-target"
    assert config.debugger.probe_id is None
    assert config.artifacts.allowed_extensions == [".elf", ".hex", ".bin"]
    assert config.can_buses == {}
    assert config.permissions.allow_can_read is True


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
    service = HardCIToolService(config)
    try:
        listed = mcp_tool_call(service, "hardci_can_buses_list")
    finally:
        service.close()
    assert listed["ok"] is True
    assert listed["buses"]["dut_can"]["adapter"] == "socketcan"
    assert "socketcan" in listed["supported_adapters"]


def test_openocd_passes_configured_probe_id(tmp_path: Path) -> None:
    config = load_config(str(write_config(tmp_path, probe_id="STLINK123")), str(tmp_path))
    service = HardCIToolService(config)
    try:
        probe = mcp_tool_call(service, "hardci_probe_target")
    finally:
        service.close()
    assert probe["ok"] is True
    log_text = (tmp_path / probe["log_path"]).read_text(encoding="utf-8")
    assert "adapter serial STLINK123" in log_text


def test_stlink_backend_probes_and_flashes_with_probe_id(tmp_path: Path) -> None:
    firmware = tmp_path / "build" / "firmware.elf"
    firmware.parent.mkdir(parents=True)
    firmware.write_bytes(b"\x7fELFfake")
    config = load_config(str(write_config(tmp_path, debugger_type="stlink", probe_id="STLINK123")), str(tmp_path))
    service = HardCIToolService(config)
    try:
        info = mcp_tool_call(service, "hardci_debugger_info")
        probe = mcp_tool_call(service, "hardci_probe_target")
        flash = mcp_tool_call(service, "hardci_flash_firmware", {"image_path": "build/firmware.elf"})
    finally:
        service.close()
    assert info["ok"] is True
    assert probe["ok"] is True
    assert flash["ok"] is True
    assert flash["operation_result"]["confirmed"] is True
    log_text = (tmp_path / flash["log_path"]).read_text(encoding="utf-8")
    assert "port=SWD" in log_text
    assert "sn=STLINK123" in log_text
    assert "-w" in log_text
    assert "-v" in log_text
    assert "-rst" in log_text


def test_stlink_rejects_unconfirmed_successful_exit(tmp_path: Path) -> None:
    config = load_config(
        str(write_config(tmp_path, debugger_type="stlink", debugger_executable=FAKE_STLINK_UNCONFIRMED)),
        str(tmp_path),
    )
    service = HardCIToolService(config)
    try:
        result = mcp_tool_call(service, "hardci_reset_target", {"mode": "run"})
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
    service = HardCIToolService(config)
    try:
        result = mcp_tool_call(service, "hardci_flash_firmware", {"image_path": "build/firmware.bin"})
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
    target = tmp_path / "skills" / "hardci-config-setup" / "SKILL.md"
    result = install_skill("open-code", str(target))
    assert result["ok"] is True
    assert result["agent"] == "opencode"
    assert "hardci_version" in target.read_text(encoding="utf-8")


def test_mcp_tool_registry_is_consistent(tmp_path: Path) -> None:
    assert [tool["name"] for tool in MCP_TOOLS] == MCP_TOOL_NAMES
    assert all(name.startswith("hardci_") for name in MCP_TOOL_NAMES)
    config = load_config(str(write_config(tmp_path)), str(tmp_path))
    service = HardCIToolService(config)
    try:
        for name in MCP_TOOL_NAMES:
            result = service.call(name, {})
            assert result.get("error_type") != "unknown_tool", f"{name} is advertised but not dispatched"
    finally:
        service.close()


def test_mcp_initialize_rejects_unsupported_protocol_version(tmp_path: Path) -> None:
    config = load_config(str(write_config(tmp_path)), str(tmp_path))
    service = HardCIToolService(config)
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
