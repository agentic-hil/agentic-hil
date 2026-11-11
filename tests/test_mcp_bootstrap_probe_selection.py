from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from conftest import FAKE_STLINK

from agentic_hil.backends.common import CompletedCommand
from agentic_hil.bench import BenchMutex
from agentic_hil.config import project_config_path
from agentic_hil.coordination import DEBUGGER_DISCOVERY_RESOURCE
from agentic_hil.mcp import handle_mcp_message
from agentic_hil.tools import PROJECT_CONFIG_CREATE, UnprovisionedToolService

PROBE_A = "STLINK_A831"
PROBE_B = "STLINK_B831"
PORT_A = "COM71"
PORT_B = "COM82"
TARGET_NAME = "STM32F446RE"


def _workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    return workspace.resolve()


def _two_attached_stlinks(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    commands: list[list[str]] = []
    listing = f"ST-LINK SN : {PROBE_A}\nST-LINK SN : {PROBE_B}\n"

    def fake_spawn(command: list[str], cwd: str, timeout_s: float) -> CompletedCommand:
        commands.append(list(command))
        if "-l" in command:
            return CompletedCommand(listing, "", 0, False, False)
        if "mode=HOTPLUG" in command:
            selected = next(argument.removeprefix("sn=") for argument in command if argument.startswith("sn="))
            return CompletedCommand(
                f"ST-LINK SN : {selected}\nDevice name : {TARGET_NAME}\n",
                "",
                0,
                False,
                False,
            )
        raise AssertionError(f"unexpected bootstrap process command: {command!r}")

    monkeypatch.setattr("agentic_hil.bootstrap.find_stm32_programmer_cli", lambda: FAKE_STLINK.as_posix())
    monkeypatch.setattr("agentic_hil.bootstrap.spawn_command", fake_spawn)
    monkeypatch.setattr(
        "agentic_hil.bootstrap.list_available_com_ports",
        lambda tool: {
            "ok": True,
            "tool": tool,
            "ports": [
                {"device": PORT_A, "serial_number": PROBE_A},
                {"device": PORT_B, "serial_number": PROBE_B},
            ],
        },
    )
    return commands


def _mcp_call(service: UnprovisionedToolService, name: str, arguments: dict[str, object]) -> dict[str, object]:
    response = handle_mcp_message(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
        service,
    )
    assert isinstance(response, dict)
    result = response["result"]
    assert isinstance(result, dict)
    return result


def test_unprovisioned_mcp_lists_both_probe_serials(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = _workspace(tmp_path, monkeypatch)
    commands = _two_attached_stlinks(monkeypatch)
    service = UnprovisionedToolService(workspace)
    try:
        response = _mcp_call(service, "debugger_probes_list", {})
        result = response["structuredContent"]
        probes = result.get("probes") if isinstance(result, dict) else None
        serials = [probe.get("probe_id") for probe in probes if isinstance(probe, dict)] if isinstance(probes, list) else []
        if response["isError"] is not False or not isinstance(result, dict) or result.get("ok") is not True or serials != [PROBE_A, PROBE_B]:
            raise AssertionError("unconfigured MCP enumeration must list both attached probe serials")

        assert result["tool"] == "debugger_probes_list"
        assert result["source"] == "bootstrap"
        assert result["backend"] == "stlink"
        assert result["discovered_by"] == "stm32cubeprogrammer_cli"
        assert all("mode=HOTPLUG" not in command for command in commands)
        assert len(commands) == 1
        assert commands[0][-3:] == ["-q", "-l", "st-link-only"]
        assert not project_config_path(workspace).exists()
        assert service.config is None
    finally:
        service.close()


def test_mcp_creation_selects_probe_and_matching_com_port(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = _workspace(tmp_path, monkeypatch)
    commands = _two_attached_stlinks(monkeypatch)
    service = UnprovisionedToolService(workspace)
    try:
        response = _mcp_call(service, PROJECT_CONFIG_CREATE, {"probe_id": PROBE_B})
        result = response["structuredContent"]
        if response["isError"] is not False or not isinstance(result, dict) or result.get("created") is not True:
            raise AssertionError("MCP creation must bind the selected probe and its matching COM port")

        path = project_config_path(workspace)
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert document["workspace_root"] == str(workspace)
        assert document["debuggers"]["dut"]["probe_id"] == PROBE_B
        assert document["com_ports"]["dut_uart"]["device"] == PORT_B
        assert document["com_ports"]["dut_uart"]["serial_number"] == PROBE_B
        assert service.config is not None
        assert service.config.debugger is not None
        assert service.config.debugger.probe_id == PROBE_B
        assert any("mode=HOTPLUG" in command and f"sn={PROBE_B}" in command for command in commands)
        assert not any("mode=HOTPLUG" in command and f"sn={PROBE_A}" in command for command in commands)

        original_bytes = path.read_bytes()
        command_count = len(commands)
        repeated = _mcp_call(service, PROJECT_CONFIG_CREATE, {"probe_id": PROBE_A})
        repeated_result = repeated["structuredContent"]
        assert repeated["isError"] is True
        assert isinstance(repeated_result, dict)
        assert repeated_result["error_type"] == "invalid_argument"
        assert path.read_bytes() == original_bytes
        assert len(commands) == command_count
    finally:
        service.close()


def test_mcp_creation_refuses_an_absent_serial(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = _workspace(tmp_path, monkeypatch)
    commands = _two_attached_stlinks(monkeypatch)
    service = UnprovisionedToolService(workspace)
    try:
        response = _mcp_call(service, PROJECT_CONFIG_CREATE, {"probe_id": "STLINK_ABSENT831"})
        result = response["structuredContent"]
        if response["isError"] is not True or not isinstance(result, dict) or result.get("error_type") != "adapter_not_found":
            raise AssertionError("MCP creation must refuse an absent probe serial as adapter_not_found")

        assert result["requested_probe_id"] == "STLINK_ABSENT831"
        assert [probe["probe_id"] for probe in result["probes"]] == [PROBE_A, PROBE_B]
        assert not project_config_path(workspace).exists()
        assert service.config is None
        assert len(commands) == 1
        assert commands[0][-3:] == ["-q", "-l", "st-link-only"]
    finally:
        service.close()


def test_ambiguous_mcp_creation_lists_choices_and_creation_route(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = _workspace(tmp_path, monkeypatch)
    commands = _two_attached_stlinks(monkeypatch)
    service = UnprovisionedToolService(workspace)
    try:
        response = _mcp_call(service, PROJECT_CONFIG_CREATE, {})
        result = response["structuredContent"]
        assert response["isError"] is True
        assert isinstance(result, dict)
        assert result["error_type"] == "ambiguous_hardware"
        assert [probe["probe_id"] for probe in result["probes"]] == [PROBE_A, PROBE_B]
        next_step = str(result.get("next_step", ""))
        if PROJECT_CONFIG_CREATE not in next_step or "probe_id" not in next_step:
            raise AssertionError("ambiguous MCP creation must name project_config_create with probe_id")

        remediation = result.get("remediation", [])
        assert any(PROJECT_CONFIG_CREATE in str(step) and "probe_id" in str(step) for step in remediation)
        assert not project_config_path(workspace).exists()
        assert service.config is None
        assert len(commands) == 1
        assert all("mode=HOTPLUG" not in command for command in commands)
    finally:
        service.close()


def test_unprovisioned_listing_obeys_and_releases_discovery_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _workspace(tmp_path, monkeypatch)
    commands = _two_attached_stlinks(monkeypatch)
    service = UnprovisionedToolService(workspace)
    holder = BenchMutex(frontend="test-holder")
    try:
        try:
            holder.acquire_named(DEBUGGER_DISCOVERY_RESOURCE)
            refused = _mcp_call(service, "debugger_probes_list", {})
            refused_result = refused["structuredContent"]
            assert refused["isError"] is True
            assert isinstance(refused_result, dict)
            assert refused_result["error_type"] == "device_busy"
            assert commands == []
        finally:
            holder.release_all()

        verifier = BenchMutex(frontend="test-verifier")
        try:
            listed = _mcp_call(service, "debugger_probes_list", {})
            assert listed["isError"] is False
            assert len(commands) == 1
            verifier.acquire_named(DEBUGGER_DISCOVERY_RESOURCE)
        finally:
            verifier.release_all()
    finally:
        service.close()
