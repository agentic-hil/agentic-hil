from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from support import trusted_program

from agentic_hil.backends.common import CompletedCommand, cube_clt_programmer_paths
from agentic_hil.backends.stlink import stlink_target_info
from agentic_hil.bootstrap import apply_discovery_to_template, correlate_com_port, discover_attached_hardware
from agentic_hil.cli import DEFAULT_CONFIG_TEMPLATE, init_config


def test_stlink_target_info_extracts_one_identity() -> None:
    assert stlink_target_info("ST-LINK SN : ABC123\nDevice name : STM32F446RE\n") == {
        "probe_id": "ABC123",
        "controller": "STM32F446RE",
    }
    assert stlink_target_info("Device name : STM32F446RE\n") is None


def test_correlate_com_port_requires_one_exact_serial_match() -> None:
    available = {
        "ok": True,
        "ports": [
            {"device": "COM3", "serial_number": "06:6A-FF30"},
            {"device": "COM4", "serial_number": "OTHER"},
        ],
    }
    assert correlate_com_port("066aff30", available) == {"device": "COM3", "serial_number": "06:6A-FF30"}
    available["ports"].append({"device": "COM5", "serial_number": "066AFF30"})
    assert correlate_com_port("066aff30", available) is None


def test_bootstrap_discovery_uses_fixed_readonly_stlink_commands(monkeypatch: pytest.MonkeyPatch) -> None:
    commands: list[list[str]] = []
    responses = iter(
        [
            CompletedCommand("ST-LINK SN : STLINK123\n", "", 0, False, False),
            CompletedCommand("ST-LINK SN : STLINK123\nDevice name : STM32F446RE\n", "", 0, False, False),
        ]
    )

    def fake_spawn(command: list[str], cwd: str, timeout_s: float) -> CompletedCommand:
        commands.append(command)
        return next(responses)

    monkeypatch.setattr("agentic_hil.bootstrap.find_stm32_programmer_cli", lambda: str(Path("C:/ST/STM32_Programmer_CLI.exe")))
    monkeypatch.setattr("agentic_hil.bootstrap.spawn_command", fake_spawn)
    monkeypatch.setattr(
        "agentic_hil.bootstrap.list_available_com_ports",
        lambda tool: {"ok": True, "tool": tool, "ports": [{"device": "COM3", "serial_number": "STLINK123"}]},
    )

    result = discover_attached_hardware()

    assert result["ok"] is True
    assert result["probe_id"] == "STLINK123"
    assert result["target"]["controller"] == "STM32F446RE"
    assert result["com_port"]["device"] == "COM3"
    assert commands[0][-3:] == ["-q", "-l", "st-link-only"]
    assert commands[1][-5:] == ["-q", "-c", "port=SWD", "mode=HOTPLUG", "sn=STLINK123"]
    assert all("-w" not in command and "-rst" not in command and "-halt" not in command for command in commands)


def test_target_not_detected_is_clean_bootstrap_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    responses = iter(
        [
            CompletedCommand("ST-LINK SN : STLINK123\n", "", 0, False, False),
            CompletedCommand("", "Error: No STM32 target found", 1, False, False),
        ]
    )
    monkeypatch.setattr("agentic_hil.bootstrap.find_stm32_programmer_cli", lambda: str(Path("C:/ST/STM32_Programmer_CLI.exe")))
    monkeypatch.setattr("agentic_hil.bootstrap.spawn_command", lambda *args: next(responses))
    monkeypatch.setattr("agentic_hil.bootstrap.list_available_com_ports", lambda tool: {"ok": True, "ports": []})

    result = discover_attached_hardware()

    assert result["error_type"] == "target_not_detected"
    assert result["cleanup_required"] is False
    assert result["side_effect_status"] == "not_started"
    assert result["hardware_state"] == "unchanged"


def test_discovery_applies_project_requirements() -> None:
    template = yaml.safe_load(DEFAULT_CONFIG_TEMPLATE)
    profile = {
        "target": {"name": "demo", "controller": "stm32f446ret6"},
        "debuggers": {"dut": {"timeout_s": 30, "permissions": {"allow_probe": True, "allow_flash": True, "allow_reset": True, "allow_raw_debugger_commands": True}}},
        "com_ports": {"uart": {"baudrate": 115200, "permissions": {"allow_read": True, "allow_write": False}}},
    }
    discovery = {
        "executable": "C:/ST/STM32_Programmer_CLI.exe",
        "probe_id": "STLINK123",
        "target": {"controller": "STM32F446RE"},
        "com_port": {"device": "COM3"},
    }

    configured = apply_discovery_to_template(template, profile, discovery)

    assert configured["target"] == {"name": "demo", "controller": "stm32f446ret6"}
    assert configured["debuggers"]["dut"]["type"] == "stlink"
    assert configured["debuggers"]["dut"]["probe_id"] == "STLINK123"
    # The profile above still requests the version 1 read grants. The document
    # this writes is version 2, where reading needs none and the keys are
    # refused by name, so the request is satisfied by dropping it.
    assert configured["version"] == 2
    assert configured["debuggers"]["dut"]["permissions"] == {
        "allow_flash": True,
        "allow_reset": True,
        "allow_raw_debugger_commands": False,
        "allow_mass_erase": False,
    }
    assert configured["com_ports"]["dut_uart"]["device"] == "COM3"
    assert configured["com_ports"]["dut_uart"]["permissions"] == {"allow_write": False}


def test_init_uses_hardware_discovery_when_project_profile_exists(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    (workspace / "agentic-hil.config.example.yaml").write_text(
        """target:\n  name: demo\n  controller: stm32f446ret6\ndebuggers:\n  dut:\n    permissions:\n      allow_probe: true\n      allow_flash: true\n      allow_reset: true\ncom_ports:\n  uart:\n    baudrate: 115200\n    permissions:\n      allow_read: true\n      allow_write: false\n""",
        encoding="utf-8",
    )
    # A trusted copy, not the checkout: loading the generated configuration pins
    # the executable and refuses one another local user could replace.
    executable = trusted_program(Path(__file__).resolve())
    monkeypatch.setattr(
        "agentic_hil.cli.discover_attached_hardware",
        lambda: {
            "ok": True,
            "executable": str(executable),
            "probe_id": "STLINK123",
            "target": {"controller": "STM32F446RE"},
            "com_port": {"device": "COM3"},
            "side_effect_status": "not_started",
            "hardware_state": "unchanged",
        },
    )

    result = init_config()
    written = yaml.safe_load(Path(result["path"]).read_text(encoding="utf-8"))

    assert result["ok"] is True
    assert result["hardware_discovery"]["ok"] is True
    assert written["debuggers"]["dut"]["type"] == "stlink"
    assert written["debuggers"]["dut"]["permissions"]["allow_flash"] is True
    assert written["com_ports"]["dut_uart"]["device"] == "COM3"
    # A bootstrapped config is a version 2 config: it loads, which it could not
    # do while carrying a read permission this version refuses by name.
    assert written["version"] == 2
    assert "allow_probe" not in written["debuggers"]["dut"]["permissions"]
    assert "allow_read" not in written["com_ports"]["dut_uart"]["permissions"]


def test_a_generated_configuration_permits_the_whole_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The new default reaches an operator through generation, not through a
    # changed reading of a file that already exists. A project set up today
    # flashes out of cmake-build-debug without curating a list first.
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)

    written = yaml.safe_load(Path(init_config()["path"]).read_text(encoding="utf-8"))

    assert written["artifacts"]["allowed_roots"] == ["."]
    assert yaml.safe_load(DEFAULT_CONFIG_TEMPLATE)["artifacts"]["allowed_roots"] == ["."]


def test_cube_clt_programmer_paths_find_versioned_install(tmp_path: Path) -> None:
    executable = tmp_path / "STM32CubeCLT_1.22.0" / "STM32CubeProgrammer" / "bin" / "STM32_Programmer_CLI.exe"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"exe")

    assert cube_clt_programmer_paths(tmp_path) == [str(executable)]
