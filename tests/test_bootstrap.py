from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from agentic_hil.backends.common import CompletedCommand, cube_clt_programmer_paths
from agentic_hil.backends.stlink import stlink_target_info
from agentic_hil.bootstrap import (
    apply_discovery_to_template,
    correlate_com_port,
    discover_attached_hardware,
    select_probe_id,
)
from agentic_hil.cli import DEFAULT_CONFIG_TEMPLATE, init_config
from agentic_hil.config import debugger_drives_hardware, debugger_is_placeholder, load_config
from agentic_hil.types import fold_hardware_id


def test_stlink_target_info_extracts_one_identity() -> None:
    assert stlink_target_info("ST-LINK SN : ABC123\nDevice name : STM32F446RE\n") == {
        "probe_id": "ABC123",
        "controller": "STM32F446RE",
    }
    assert stlink_target_info("Device name : STM32F446RE\n") is None


def test_correlate_com_port_requires_one_serial_match() -> None:
    """One hardware identity, the same one `select_probe_id` and the lock use.

    Case folds and nothing else. This used to strip punctuation as well, on the
    theory that a correlation keys nothing — but the port it picks is written into
    `com_ports.<name>.device` by adoption, so a probe serial `06:6A-FF30` matching
    a sole host port whose serial is really `066AFF30` puts a possibly different
    device into an entry that may already allow writes. A tie guard cannot see a
    single false match. Not correlating is the safe failure: the caller reports
    the COM port as unavailable with the host inventory attached."""
    available = {
        "ok": True,
        "ports": [
            {"device": "COM3", "serial_number": "066AFF30"},
            {"device": "COM4", "serial_number": "OTHER"},
        ],
    }
    assert correlate_com_port("066aff30", available) == {"device": "COM3", "serial_number": "066AFF30"}
    # A serial the two inventories spell differently is not this probe's port as
    # far as anything here can prove, so it is not offered as one.
    assert correlate_com_port("06:6A-FF30", available) is None
    available["ports"].append({"device": "COM5", "serial_number": "066aff30"})
    assert correlate_com_port("066AFF30", available) is None


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


def _fixed_stlink(monkeypatch: pytest.MonkeyPatch, listing: str, *, commands: list[list[str]] | None = None) -> None:
    """The real discovery path with only the two ST-Link processes replaced.

    Everything selection does — folding, refusing, choosing the enumerated
    spelling — runs for real; a test that mocked `discover_attached_hardware`
    itself would be testing its own forwarding."""
    responses = iter(
        [
            CompletedCommand(listing, "", 0, False, False),
            CompletedCommand("ST-LINK SN : IGNORED\nDevice name : STM32F446RE\n", "", 0, False, False),
        ]
    )

    def fake_spawn(command: list[str], cwd: str, timeout_s: float) -> CompletedCommand:
        if commands is not None:
            commands.append(command)
        return next(responses)

    monkeypatch.setattr("agentic_hil.bootstrap.find_stm32_programmer_cli", lambda: str(Path("C:/ST/STM32_Programmer_CLI.exe")))
    monkeypatch.setattr("agentic_hil.bootstrap.spawn_command", fake_spawn)
    monkeypatch.setattr("agentic_hil.bootstrap.list_available_com_ports", lambda tool: {"ok": True, "ports": []})


def test_a_requested_probe_is_matched_by_hardware_identity_not_by_spelling(monkeypatch: pytest.MonkeyPatch) -> None:
    """`abcdef` selects the probe enumerated as `ABCDEF`.

    A probe serial is an opaque hardware id, and every device lock key in this
    repository folds it for that reason: one ST-Link is `0669FF…` to
    STM32CubeProgrammer and `0669ff…` to udev. Exact string membership answered
    `adapter_not_found` — "plug the board in" — for a board that is plugged in.
    The enumerated spelling is what goes on to the toolchain and into the file."""
    commands: list[list[str]] = []
    _fixed_stlink(monkeypatch, "ST-LINK SN : 0669FF303430\nST-LINK SN : 066AFF495451\n", commands=commands)

    result = discover_attached_hardware(probe_id="0669ff303430")

    assert result["ok"] is True, result
    assert result["probe_id"] == "0669FF303430"
    assert commands[1][-1] == "sn=0669FF303430"


def test_a_serial_that_is_not_attached_is_still_refused_naming_the_ones_that_are(monkeypatch: pytest.MonkeyPatch) -> None:
    """Folding selects among what is attached; it never adds to it."""
    _fixed_stlink(monkeypatch, "ST-LINK SN : 0669FF303430\n")

    result = discover_attached_hardware(probe_id="066AFF495451")

    assert result["ok"] is False
    assert result["error_type"] == "adapter_not_found"
    assert result["requested_probe_id"] == "066AFF495451"
    assert [entry["probe_id"] for entry in result["probes"]] == ["0669FF303430"]


def test_selection_folds_exactly_what_the_lock_key_folds_and_no_more(monkeypatch: pytest.MonkeyPatch) -> None:
    """Selection, locking and comparison have to name one probe.

    Selection used to strip every non-alphanumeric character, while lock keys and
    the adoption mismatch check only case-fold. A request for `06-69FF303430`
    therefore selected the attached `0669FF303430`, and the HOTPLUG connect went
    to a board the rest of the system does not think this call is about — the
    lock taken is keyed on a different identity, and the mismatch check that
    would have caught it runs after something was already said to that board.
    """
    commands: list[list[str]] = []
    _fixed_stlink(monkeypatch, "ST-LINK SN : 0669FF303430\n", commands=commands)

    result = discover_attached_hardware(probe_id="06-69FF303430")

    assert result["ok"] is False
    assert result["error_type"] == "adapter_not_found"
    assert len(commands) == 1, "nothing was said to any board"
    # And the folding that is the lock's own still selects.
    assert select_probe_id("0669ff303430", ["0669FF303430"]) == "0669FF303430"
    assert select_probe_id("06-69FF303430", ["0669FF303430"]) is None
    assert fold_hardware_id("0669ff303430") == fold_hardware_id("0669FF303430")


def test_nothing_reaches_the_board_when_before_connect_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    """The hook runs after enumeration and before anything is said to one probe.

    That is where a caller takes the machine-wide lock on the probe it is about
    to talk to, so the HOTPLUG connect must not have happened when it refuses."""
    commands: list[list[str]] = []
    _fixed_stlink(monkeypatch, "ST-LINK SN : 0669FF303430\n", commands=commands)
    seen: list[str] = []

    def refuse(probe_id: str) -> dict:
        seen.append(probe_id)
        return {"ok": False, "error_type": "device_busy", "summary": "held elsewhere"}

    result = discover_attached_hardware(before_connect=refuse)

    assert result == {"ok": False, "error_type": "device_busy", "summary": "held elsewhere"}
    assert seen == ["0669FF303430"], "the hook is given the enumerated spelling"
    assert len(commands) == 1, "only the enumeration ran"
    assert commands[0][-3:] == ["-q", "-l", "st-link-only"]


def test_discovery_applies_project_requirements() -> None:
    template = yaml.safe_load(DEFAULT_CONFIG_TEMPLATE)
    profile = {
        "target": {"name": "demo", "controller": "stm32f446ret6"},
        # Every flag is granted by default now, so what a profile can still say
        # is "not this one". allow_mass_erase below is the narrowing; the
        # version 1 read grants beside it are the request that is dropped.
        "debuggers": {"dut": {"timeout_s": 30, "permissions": {"allow_probe": True, "allow_flash": True, "allow_mass_erase": False}}},
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
    # Granted unless the profile said otherwise: a flag it does not name follows
    # the generated default, and one it names is honoured — which can now only
    # narrow, because the default it would have to beat is already true.
    assert configured["debuggers"]["dut"]["permissions"] == {
        "allow_flash": True,
        "allow_reset": True,
        "allow_raw_debugger_commands": True,
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
    executable = Path(__file__).resolve()
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


def test_init_reports_the_permissions_the_profile_actually_left_narrowed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A profile may narrow, and the result has to say when it did.

    The profile path is the operator's own, and hardci-hq#96's owner left it
    theirs: a flag a project's `agentic-hil.config.example.yaml` sets to `false`
    is honoured. What must not survive that is a summary claiming every
    permission was granted — an operator who wrote `allow_mass_erase: false`
    would be told their own decision had been overridden."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    (workspace / "agentic-hil.config.example.yaml").write_text(
        "target:\n  name: demo\n  controller: stm32f446ret6\n"
        "debuggers:\n  dut:\n    permissions:\n      allow_mass_erase: false\n"
        "com_ports:\n  uart:\n    baudrate: 115200\n    permissions:\n      allow_write: false\n",
        encoding="utf-8",
    )
    executable = Path(__file__).resolve()
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

    assert result["ok"] is True, result
    assert written["debuggers"]["dut"]["permissions"]["allow_mass_erase"] is False
    assert written["com_ports"]["dut_uart"]["permissions"]["allow_write"] is False
    assert result["narrowed_permissions"] == [
        "com_ports.dut_uart.permissions.allow_write",
        "debuggers.dut.permissions.allow_mass_erase",
    ]
    assert "every permission granted." not in result["summary"]
    assert "the project profile set to false" in result["summary"]
    assert result["permissions"]["debuggers"]["dut"]["allow_mass_erase"] is False
    assert any("allow_mass_erase" in step for step in result["next_steps"])
    assert not any(step.startswith("Every permission in this file is true:") for step in result["next_steps"])


def test_init_without_a_profile_still_reports_a_fully_granted_bench(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The skeleton path, which is the one the issue is about.

    Nothing narrowed, so the result says so plainly and the next steps name the
    destructive grant an operator has to decide about."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)

    result = init_config()

    assert result["ok"] is True, result
    assert result["narrowed_permissions"] == []
    assert result["summary"].endswith("with every permission granted.")
    assert result["permissions"]["allow_config_permissions_write"] is True
    assert result["permissions"]["debug"]["allow_all_symbols"] is True
    assert result["permissions"]["artifacts"]["allow_upload"] is True
    assert any(step.startswith("Every permission in this file is true:") for step in result["next_steps"])
    # The permissions are open and the entry still drives nothing. The starter
    # entry names no toolchain and pinning does not find it one, so the steps say
    # that rather than promising a bench that works as written.
    assert any("names no toolchain yet" in step for step in result["next_steps"])
    written = load_config(str(Path(result["path"])), str(workspace))
    entry = written.debuggers["dut"]
    assert debugger_is_placeholder(entry)
    assert not debugger_drives_hardware(written, entry)
