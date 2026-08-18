from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from conftest import FAKE_STLINK, write_config

import agentic_hil.cli
import agentic_hil.tools
from agentic_hil.backends.common import CompletedCommand, cube_clt_programmer_paths
from agentic_hil.backends.stlink import stlink_target_info
from agentic_hil.bench import BenchMutex
from agentic_hil.bootstrap import (
    PROJECT_PROFILE,
    apply_discovery_to_template,
    correlate_com_port,
    discover_attached_hardware,
    select_probe_id,
)
from agentic_hil.cli import DEFAULT_CONFIG_TEMPLATE, adopt_hardware, init_config
from agentic_hil.config import (
    ConfigError,
    debugger_drives_hardware,
    debugger_is_placeholder,
    load_authoritative_config,
    load_config,
)
from agentic_hil.coordination import DEBUGGER_DISCOVERY_RESOURCE
from agentic_hil.devices import config_devices, debugger_device
from agentic_hil.knowledge import remediation_fields
from agentic_hil.report import read_last_report
from agentic_hil.tools import AgenticHILToolService, project_config_create
from agentic_hil.types import CURRENT_CONFIG_VERSION, JsonObject, fold_hardware_id


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
        # Every flag a generation can grant is granted by default now, so what a
        # profile can still say is "not this one". allow_reset below is the
        # narrowing — it has to be a flag the default leaves *true*, or the test
        # passes without the profile being consulted at all. The version 1 read
        # grant beside it is the request that is dropped, and allow_mass_erase is
        # a profile agreeing with a default that is already false.
        "debuggers": {"dut": {"timeout_s": 30, "permissions": {"allow_probe": True, "allow_flash": True, "allow_reset": False, "allow_mass_erase": False}}},
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
    # this writes is the current version, where reading needs none and the keys
    # are refused by name, so the request is satisfied by dropping it.
    assert configured["version"] == CURRENT_CONFIG_VERSION
    # Granted unless the profile said otherwise: a flag it does not name follows
    # the generated default, and one it names is honoured — which can only
    # narrow, because the default it would have to beat is already true wherever
    # a generation grants anything. allow_raw_debugger_commands is not named here
    # and is false, which is the generated default arriving through this path
    # rather than through the template text.
    assert configured["debuggers"]["dut"]["permissions"] == {
        "allow_flash": True,
        "allow_reset": False,
        "allow_debug_execution": True,
        "allow_raw_debugger_commands": False,
        "allow_mass_erase": False,
    }
    assert configured["com_ports"]["dut_uart"]["device"] == "COM3"
    assert configured["com_ports"]["dut_uart"]["permissions"] == {"allow_write": False}


@pytest.mark.parametrize("baudrate", ["fast", None, [], True], ids=repr)
def test_a_profile_baudrate_that_is_not_a_number_is_refused_by_name(baudrate: object) -> None:
    """A hand-written profile is the one input here with no schema in front of it.

    `int(profile_port.get("baudrate", 115200))` turned `baudrate: fast` into a
    `ValueError` out of a generation that had already read the board. `True` is
    in the list for the opposite reason: `int(True)` is `1`, which the
    configuration schema accepts as a baudrate, so the traceback was not even the
    worst case — a value nobody meant would have been written into the file and
    opened on the port."""
    template = yaml.safe_load(DEFAULT_CONFIG_TEMPLATE)
    profile = {
        "target": {"name": "demo", "controller": "stm32f446ret6"},
        "debuggers": {"dut": {"timeout_s": 30, "permissions": {}}},
        "com_ports": {"uart": {"baudrate": baudrate, "permissions": {}}},
    }
    discovery = {
        "executable": "C:/ST/STM32_Programmer_CLI.exe",
        "probe_id": "STLINK123",
        "target": {"controller": "STM32F446RE"},
        "com_port": {"device": "COM3"},
    }

    with pytest.raises(ConfigError) as excinfo:
        apply_discovery_to_template(template, profile, discovery)

    assert excinfo.value.error_type == "invalid_argument"
    # Named to the key in the file the operator has to edit, not to "baudrate".
    assert excinfo.value.details["field"] == "com_ports.uart.baudrate"
    assert excinfo.value.details["profile"] == PROJECT_PROFILE
    assert "115200" in excinfo.value.summary
    assert excinfo.value.to_dict()["remediation"]


def test_init_answers_a_profile_it_cannot_use_instead_of_raising(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The whole point of the named refusal: `agentic-hil init` returns a result.

    Discovery has already run and the board has already been read by the time the
    profile is applied, so a traceback here loses that work and tells an operator
    nothing about which line of their own file is wrong. Nothing is written, so a
    repeat after the edit starts from the same place."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    (workspace / PROJECT_PROFILE).write_text(
        "target:\n  name: demo\n  controller: stm32f446ret6\ncom_ports:\n  uart:\n    baudrate: fast\n",
        encoding="utf-8",
    )
    executable = Path(__file__).resolve()
    monkeypatch.setattr(
        "agentic_hil.tools.discover_attached_hardware",
        lambda *args, **kwargs: {
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

    assert result["ok"] is False
    assert result["error_type"] == "invalid_argument"
    assert result["field"] == "com_ports.uart.baudrate"
    assert result["remediation"]
    assert "No configuration was written." in result["summary"]
    assert not Path(result["path"]).exists()


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
        "agentic_hil.tools.discover_attached_hardware",
        lambda *args, **kwargs: {
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
    # A bootstrapped config is written at the current version: it loads, which it
    # could not do while carrying a read permission that version refuses by name,
    # nor with a COM port entry naming no hardware.
    assert written["version"] == CURRENT_CONFIG_VERSION
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

    The profile path is the operator's own, and the open generated default left it
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
        "agentic_hil.tools.discover_attached_hardware",
        lambda *args, **kwargs: {
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
    # allow_mass_erase is false here too, and is deliberately *not* in this list:
    # the generated default writes it false on every bench, so reporting it as
    # something the profile narrowed would be a standing false positive. `permissions` below still carries its actual value.
    assert result["narrowed_permissions"] == ["com_ports.dut_uart.permissions.allow_write"]
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
    assert result["summary"].endswith("with every permission granted except the two that are false so that flashing works.")
    assert result["permissions"]["allow_config_permissions_write"] is True
    assert result["permissions"]["debug"]["allow_all_symbols"] is True
    assert result["permissions"]["artifacts"]["allow_upload"] is True
    assert any(step.startswith("Every permission in this file is true") for step in result["next_steps"])
    # The permissions are open and the entry still drives nothing. The starter
    # entry names no toolchain and pinning does not find it one, so the steps say
    # that rather than promising a bench that works as written.
    assert any("names no toolchain yet" in step for step in result["next_steps"])
    written = load_config(str(Path(result["path"])), str(workspace))
    entry = written.debuggers["dut"]
    assert debugger_is_placeholder(entry)
    assert not debugger_drives_hardware(written, entry)


# ---------------------------------------------------------------------------
# One machine, one bench, and every path that reads it agreeing.


def _one_attached_stlink(monkeypatch: pytest.MonkeyPatch, *, serial: str = "STLINK123", controller: str = "STM32F446RE", device: str = "COM7") -> list[list[str]]:
    """One bench on this host, seen through the real discovery path.

    Only the two ST-Link processes and the host port inventory are replaced, so
    enumeration, selection, the HOTPLUG connect and the COM correlation all run
    for real — a double for `discover_attached_hardware` itself would let a caller
    that never calls it pass. It answers by command rather than out of an
    iterator, because more than one caller reads this host per test, and that is
    the whole point.

    Returns the commands this host was given, in order, so a test can say that
    the HOTPLUG connect did not happen rather than only that the caller was
    refused."""
    commands: list[list[str]] = []

    def fake_spawn(command: list[str], cwd: str, timeout_s: float) -> CompletedCommand:
        commands.append(list(command))
        listing = f"ST-LINK SN : {serial}\n"
        return CompletedCommand(listing if "-l" in command else f"{listing}Device name : {controller}\n", "", 0, False, False)

    monkeypatch.setattr("agentic_hil.bootstrap.find_stm32_programmer_cli", lambda: FAKE_STLINK.as_posix())
    monkeypatch.setattr("agentic_hil.bootstrap.spawn_command", fake_spawn)
    monkeypatch.setattr(
        "agentic_hil.bootstrap.list_available_com_ports",
        lambda tool: {"ok": True, "tool": tool, "ports": [{"device": device, "serial_number": serial}]},
    )
    return commands


def _bench_facts(path: Path) -> JsonObject:
    """What a written configuration says about the board in front of it.

    Only the keys discovery decides. Permissions, provenance and the header a
    generated file carries are each path's own business and differ on purpose."""
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    debugger = document["debuggers"]["dut"]
    port = next(iter(document.get("com_ports", {}).values()), {})
    return {
        "type": debugger["type"],
        "executable": debugger["executable"],
        "probe_id": debugger["probe_id"],
        "controller": document["target"]["controller"],
        "com_port_device": port.get("device"),
        "com_port_baudrate": port.get("baudrate"),
    }


def test_init_looks_for_attached_hardware_without_a_project_profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Looking is not what a project profile decides.

    `init` called discovery only when the workspace held an
    `agentic-hil.config.example.yaml`, so a fresh installation — which has none —
    got the placeholder skeleton on a machine with the board plugged in, and
    nothing in the result said that nothing had been looked for. The MCP server
    reads the same board unconditionally, which is how this arrived as "CLI
    discovery is broken": it was not broken, it never ran. A profile says how to
    name and narrow a bench that was found; the fixed default fills in for it when
    the workspace has none."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    assert not (workspace / PROJECT_PROFILE).exists()
    _one_attached_stlink(monkeypatch)

    result = init_config()
    written = yaml.safe_load(Path(result["path"]).read_text(encoding="utf-8"))

    assert result["ok"] is True, result
    assert result["hardware_discovery"]["ok"] is True
    assert result["summary"].startswith("Attached hardware was discovered and configured")
    assert written["debuggers"]["dut"]["type"] == "stlink"
    assert written["debuggers"]["dut"]["probe_id"] == "STLINK123"
    assert written["debuggers"]["dut"]["executable"] == FAKE_STLINK.as_posix()
    assert written["target"]["controller"] == "stm32f446re"
    assert written["com_ports"]["dut_uart"]["device"] == "COM7"


def test_init_that_found_nothing_says_so_instead_of_writing_silent_placeholders(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A bench that was looked for and not found is a finding, not a default.

    This is the other half of the report. An operator handed `probe_id: null` and
    a placeholder target, with no sentence anywhere about the board they can see
    plugged in, concludes that detection is broken. The result now names what
    discovery answered and the one command that fills the file in afterwards."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    monkeypatch.setattr("agentic_hil.bootstrap.find_stm32_programmer_cli", lambda: None)

    result = init_config()

    assert result["ok"] is True, result
    # Never None: "did not look" and "looked and found nothing" were the two
    # outcomes this key could not tell apart, and only one of them exists now.
    assert result["hardware_discovery"]["ok"] is False
    assert result["hardware_discovery"]["error_type"] == "debugger_not_found"
    assert result["summary"].startswith("No attached bench was found")
    assert result["summary"].endswith("with every permission granted except the two that are false so that flashing works.")
    assert "STM32CubeProgrammer CLI was not found." in result["next_steps"][0]
    assert "agentic-hil adopt-hardware" in result["next_steps"][0]


def test_init_and_the_server_describe_one_bench(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Two paths, one machine, one answer — the test that fails if they part.

    `agentic-hil init` at a shell and `project_config_create` over MCP both write
    a workspace's authoritative configuration out of what is attached, and an
    operator moves between them within one session: `setup` runs the first, the
    agent runs the second. The defect is precisely this pair disagreeing about
    a board that was plugged in the whole time, so what the two files say about the
    board is pinned here rather than left to two call sites to keep in step. What
    they may still differ in is stated by omission: permissions, provenance and the
    generated header are each path's own."""
    _one_attached_stlink(monkeypatch)
    shell_workspace = tmp_path / "shell"
    shell_workspace.mkdir()
    monkeypatch.chdir(shell_workspace)
    from_shell = init_config()
    assert from_shell["ok"] is True, from_shell

    server_workspace = tmp_path / "server"
    server_workspace.mkdir()
    from_server = project_config_create(server_workspace, None)
    assert from_server["ok"] is True, from_server

    assert _bench_facts(Path(from_shell["path"])) == _bench_facts(Path(from_server["path"]))
    assert _bench_facts(Path(from_shell["path"]))["probe_id"] == "STLINK123"


def test_a_configuration_init_wrote_leaves_adopt_hardware_nothing_to_carry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The reporter's own proof, turned round.

    That `agentic-hil adopt-hardware` repaired the file `init` had just written was
    how they learned the board had been discoverable all along — one command
    walking past what the next one found. On a bench that is attached, adoption now
    has nothing to carry: every key it would fill already holds the value `init`
    read off the same board through the same function."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    _one_attached_stlink(monkeypatch)
    assert init_config()["ok"] is True

    plan = adopt_hardware(dry_run=True)

    assert plan["ok"] is True, plan
    assert plan["carried"] == []
    assert plan["kept"] == []
    assert plan["unavailable"] == []
    assert {item["key"] for item in plan["already_current"]} == {
        "target.controller",
        "debuggers.dut.probe_id",
        "debuggers.dut.executable",
        "com_ports.dut_uart.device",
        # The port's own identity, which `init` now writes for
        # the same reason it writes the device: it read it off the same board.
        "com_ports.dut_uart.serial_number",
    }


# ---------------------------------------------------------------------------
# `init --force` is the reset, and it names what the reset cost.

# The sentences that had to go. Every one was true of
# `project_config_create`, which calls `carry_over_permissions`, and false of
# `agentic-hil init --force`, which never has — and every one shipped.
RETIRED_CARRY_OVER_CLAIMS = (
    "carries the existing grants over",
    "carries every existing grant over",
    "survives `init --force`",
    "a `false` survives it",
    "`init --force` keeps it",
    "still there afterwards",
    "`--force` does not clear it",
    "the `false` survives it",
)


def _narrow_by_hand(target: Path, *closures: str) -> None:
    """Close permissions in a written configuration the way an operator does.

    In the file, with an editor, which is the state this is about: a
    bench somebody narrowed weeks ago and has not thought about since."""
    text = target.read_text(encoding="utf-8")
    for flag in closures:
        assert f"{flag}: true" in text, f"the generated configuration does not grant {flag}"
        text = text.replace(f"{flag}: true", f"{flag}: false", 1)
    target.write_text(text, encoding="utf-8")


def test_init_force_names_every_narrowing_it_discarded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The dangerous case, and the whole of what the issue asked for.

    Somebody narrows a bench, runs `--force` weeks later for an unrelated reason,
    and the bench is open again with nothing saying so — the silent reopening the
    one-way street on `project_config_set` exists to prevent. The behaviour stays
    what its name says: `--force` already discards the baudrate, the
    `resource_id`, the `state_root` and the artifact roots, and a version that
    rescued permissions but not those would be harder to predict rather than
    easier. What it owes the operator is the list. Against master this fails —
    both permissions came back open and the result mentioned neither."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    target = Path(init_config()["path"])
    _narrow_by_hand(target, "allow_flash", "allow_upload")

    result = init_config(force=True)

    assert result["ok"] is True, result
    assert result["discarded_narrowings"] == ["artifacts.allow_upload", "debuggers.dut.permissions.allow_flash"]
    assert "discarded_narrowings_unreadable" not in result
    # Named where a person actually reads: the sentence and the field, not only
    # somewhere among the next steps a long result ends with.
    for key in result["discarded_narrowings"]:
        assert key in result["summary"], result["summary"]
    assert "open again" in result["summary"]
    # And the way back is in the result rather than in a document. `revoke`
    # closes what the reset reopened; `grant` is named as its inverse, and
    # `adopt-hardware` as the command this operator may have wanted instead.
    reset_step = result["next_steps"][0]
    assert "agentic-hil revoke <key>" in reset_step
    assert "agentic-hil grant <key>" in reset_step
    assert "agentic-hil adopt-hardware" in reset_step
    # The reset itself is unchanged, which is the decision taken on this issue.
    written = yaml.safe_load(target.read_text(encoding="utf-8"))
    assert written["debuggers"]["dut"]["permissions"]["allow_flash"] is True
    assert written["artifacts"]["allow_upload"] is True


def test_init_force_over_a_configuration_it_cannot_read_says_that_rather_than_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`--force` is the reset, so it has to work on the file most in need of one.

    "Nothing was lost" and "this could not be checked" are different answers and
    only one of them is safe to act on, so an unreadable predecessor is reported
    as itself instead of as an empty list — and the regeneration still happens."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    target = Path(init_config()["path"])
    target.write_text("workspace_root: [unclosed\n\tallow_flash: false\n", encoding="utf-8")

    result = init_config(force=True)

    assert result["ok"] is True, result
    assert "discarded_narrowings" not in result
    assert "not parseable YAML" in result["discarded_narrowings_unreadable"]
    assert result["discarded_narrowings_unreadable"] in result["summary"]
    assert "is not known" in result["summary"]
    assert "agentic-hil revoke <key>" in result["next_steps"][0]
    assert yaml.safe_load(target.read_text(encoding="utf-8"))["debuggers"]["dut"]["permissions"]["allow_flash"] is True


def test_init_force_replaces_a_configuration_that_is_not_utf8_at_all(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A truncated write, a UTF-16 save, something binary dropped on the path.

    The alias refusal inside `secure_atomic_write_text` is the guarded open and
    never the decode, so reading the existing object as text made a file that is
    not UTF-8 unwritable rather than replaceable — and `init --force`, the one
    command whose job is to replace it, failed with a decode error instead."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    target = Path(init_config()["path"])
    target.write_bytes(b"\xff\xfe\x00\x00truncated \x80\x81")

    result = init_config(force=True)

    assert result["ok"] is True, result
    assert "discarded_narrowings" not in result
    assert "not UTF-8 text" in result["discarded_narrowings_unreadable"]
    assert yaml.safe_load(target.read_text(encoding="utf-8"))["debuggers"]["dut"]["permissions"]["allow_flash"] is True


def test_init_force_restores_a_non_utf8_original_when_the_new_file_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed regeneration puts the exact original back, non-UTF-8 included.

    `--force` authorises replacing the file on success, not deleting the original
    after the replacement fails. The rollback snapshot used to be the decoded
    text, and a file that would not decode was recorded as absence — so when the
    regenerated file failed its final validation, `_restore_file_snapshots`
    removed the original it reported having restored. The
    snapshot is the exact bytes now, so the file that could not be read is the
    file that comes back, byte for byte, rather than a deleted path under a
    result that says "was rolled back"."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    target = Path(init_config()["path"])
    original = b"\xff\xfe\x00\x00truncated \x80\x81 not utf-8 at all"
    target.write_bytes(original)

    # The failure is the final validation, after the new file is already written
    # over the original — the double fault the rollback path exists to survive.
    def refuse_final_validation(_workspace: Path) -> object:
        raise ConfigError("config_invalid", "injected final-validation failure", {})

    monkeypatch.setattr("agentic_hil.cli.load_authoritative_config", refuse_final_validation)

    result = init_config(force=True)

    assert "rolled back" in result["summary"], result
    # A clean rollback, not a second fault dressed up as one: the original was
    # restorable because it was held as bytes.
    assert "rollback_errors" not in result, result.get("rollback_errors")
    # The whole of the fix: the exact original is on disk, neither deleted nor
    # re-encoded.
    assert target.read_bytes() == original


def test_first_init_refuses_a_probe_another_workspace_is_holding(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The first `init` reads the board under machine-wide exclusion, lease or not.

    A workspace with no configuration has no `state_root` to lease against, so the
    read goes without the audit trail — but "this project holds no lease" was
    never "nobody holds this board". Another workspace on the machine can be
    mid-HOTPLUG on the same ST-Link, and the probe lock lives under the user's
    home keyed on the device, reachable with no shared config. So the read takes
    it in `before_connect` — the last point before the HOTPLUG connect — and a
    held board comes back `device_busy` with nothing said to it, exactly as the
    leased path answers. Against the previous behaviour this
    connected regardless."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)

    other_workspace = BenchMutex(frontend="other-workspace")
    other_workspace.acquire([f"probe:{fold_hardware_id('STLINK123')}"])

    state = {"connected": False}

    def fake_discovery(timeout_s: float = 10.0, *, probe_id: object = None, before_connect: object = None) -> JsonObject:
        # Enumeration selected this probe; `before_connect` is the last gate before
        # HOTPLUG, and where the machine-wide probe lock is taken.
        if before_connect is not None:
            refusal = before_connect("STLINK123")
            if refusal is not None:
                return refusal
        state["connected"] = True
        return {
            "ok": True,
            "tool": "bootstrap_hardware_discovery",
            "backend": "stlink",
            "executable": str(Path(__file__).resolve()),
            "probe_id": "STLINK123",
            "target": {"controller": "STM32F446RE"},
            "com_port": {"device": "COM3"},
            "available_com_ports": {"ok": True, "ports": []},
            "side_effect_status": "not_started",
            "hardware_state": "unchanged",
        }

    monkeypatch.setattr("agentic_hil.tools.discover_attached_hardware", fake_discovery)

    try:
        result = init_config()
    finally:
        other_workspace.release_all()

    assert result["ok"] is False, result
    assert result["error_type"] == "device_busy"
    assert result["retry_safe"] is True
    # Nothing was said to the board, and nothing was written: the read is what was
    # refused, so there is no file to have written it from.
    assert state["connected"] is False, "a HOTPLUG connect was attempted on a held probe"
    assert not Path(result["path"]).exists()


def test_first_init_refuses_a_probe_another_workspace_holds_by_its_alias(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The alias case round 1 left open: the holder is a real configured debugger.

    The other workspace does not reach for `probe:<serial>` by hand — it holds an
    ordinary debugger the way any run does. That debugger names both a
    `resource_id` and the probe serial, so its primary lock is
    `physical:<resource_id>`, an alias only its own operator knows. A first `init`
    here can derive the serial from enumerating the attached ST-Link but never
    that alias, so it locks `probe:<serial>` and nothing else. The debugger now
    holds `probe:<serial>` alongside its `resource_id`, which is the whole fix:
    the two collide on the one name both sides can name. Before it,
    the first `init` took `probe:<serial>` unopposed and connected to a board the
    other workspace was holding under `physical:bench-a`."""
    other_workspace_dir = tmp_path / "other"
    other_config = load_config(
        str(
            write_config(
                other_workspace_dir,
                debuggers_yaml='debuggers:\n  bench:\n    type: stlink\n    probe_id: "STLINK123"\n    resource_id: "bench-a"\n',
            )
        )
    )
    holder = debugger_device(other_config, "bench")
    # A real run's hold: the primary alias plus the serial, taken as one set.
    assert holder.lock_key == "physical:bench-a"
    assert set(holder.lock_keys) == {"physical:bench-a", f"probe:{fold_hardware_id('STLINK123')}"}

    other_bench = BenchMutex(frontend="other-workspace")
    holder.acquire(other_bench)
    assert other_bench.holds("physical:bench-a")
    assert other_bench.holds(f"probe:{fold_hardware_id('STLINK123')}")

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)

    state = {"connected": False}

    def fake_discovery(timeout_s: float = 10.0, *, probe_id: object = None, before_connect: object = None) -> JsonObject:
        # Enumeration selects the attached ST-Link by its serial, which is all the
        # bootstrap read can see of it; `before_connect` locks `probe:<serial>`.
        if before_connect is not None:
            refusal = before_connect("STLINK123")
            if refusal is not None:
                return refusal
        state["connected"] = True
        return {
            "ok": True,
            "tool": "bootstrap_hardware_discovery",
            "backend": "stlink",
            "executable": str(Path(__file__).resolve()),
            "probe_id": "STLINK123",
            "target": {"controller": "STM32F446RE"},
            "com_port": {"device": "COM3"},
            "available_com_ports": {"ok": True, "ports": []},
            "side_effect_status": "not_started",
            "hardware_state": "unchanged",
        }

    monkeypatch.setattr("agentic_hil.tools.discover_attached_hardware", fake_discovery)

    try:
        result = init_config()
    finally:
        other_bench.release_all()

    assert result["ok"] is False, result
    assert result["error_type"] == "device_busy"
    assert result["retry_safe"] is True
    # The board the other workspace holds under its private alias was never
    # connected to: the serial lock is what caught it.
    assert state["connected"] is False, "a HOTPLUG connect reached a probe another workspace held by alias"
    assert not Path(result["path"]).exists()


def test_first_init_refuses_while_another_read_holds_the_enumeration_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Bootstrap enumeration serialises machine-wide too, not only the connect.

    `debugger-discovery:all` is the enumeration pseudo-resource. The leased path
    locks it beneath `state_root`; a bootstrap read has none, so it takes it as a
    machine-wide lock beside the device locks and two first-time `init`s enumerate
    one at a time. Held here, the read refuses before it ever runs `st-link -l`."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)

    other_read = BenchMutex(frontend="other-read")
    other_read.acquire_named(DEBUGGER_DISCOVERY_RESOURCE)

    def never(*args: object, **kwargs: object) -> JsonObject:
        raise AssertionError("enumeration ran while another read held the enumeration lock")

    monkeypatch.setattr("agentic_hil.tools.discover_attached_hardware", never)

    try:
        result = init_config()
    finally:
        other_read.release_all()

    assert result["ok"] is False, result
    assert result["error_type"] == "device_busy"
    assert not Path(result["path"]).exists()


def test_a_first_init_reports_no_discard_at_all(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """There was no file, so there was nothing to discard.

    An empty list is not the same as silence: a field reading `[]` beside a
    sentence about what was lost is a finding an operator has to rule out. Absent
    means absent."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)

    result = init_config()

    assert result["ok"] is True, result
    assert "discarded_narrowings" not in result
    assert "discarded_narrowings_unreadable" not in result
    assert result["summary"].endswith("with every permission granted except the two that are false so that flashing works.")
    assert not any("replaced a file" in step for step in result["next_steps"])


def test_init_force_over_a_file_that_narrowed_nothing_reports_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The ordinary reset, where the same silence is the honest answer.

    Everything else in the file is still gone — that is what `--force` is — but no
    permission moved, so there is no permission to name."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    assert init_config()["ok"] is True

    result = init_config(force=True)

    assert result["ok"] is True, result
    assert "discarded_narrowings" not in result
    assert "discarded_narrowings_unreadable" not in result
    assert result["summary"].endswith("with every permission granted except the two that are false so that flashing works.")


def test_the_shipped_documents_and_init_force_say_the_same_thing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The claim and the behaviour, asserted in one place, which is the point.

    This was not a bug beside a documentation error. It was the two
    drifting apart with nothing holding them together: three places in the
    shipped tree promised `init --force` carried a narrowing over, `init_config`
    never called `carry_over_permissions`, and the whole suite passed either way.
    So the sentences and the behaviour are pinned to each other here."""
    root = Path(__file__).resolve().parents[1]
    # The three the issue named, and `cli.py`, which said it a fourth time in the
    # docstring of `agentic-hil grant` — fifty lines below the code that reports
    # the discards, and missed by a sweep that had looked only at the documents.
    shipped = {
        "TROUBLESHOOTING.md": root / "TROUBLESHOOTING.md",
        "src/agentic_hil/configwrite.py": root / "src" / "agentic_hil" / "configwrite.py",
        "src/agentic_hil/cli.py": root / "src" / "agentic_hil" / "cli.py",
        "CHANGELOG.md": root / "CHANGELOG.md",
    }
    for name, path in shipped.items():
        text = path.read_text(encoding="utf-8")
        for claim in RETIRED_CARRY_OVER_CLAIMS:
            assert claim not in text, f"{name} still claims `init --force` keeps a narrowing: {claim!r}"
        assert "agentic-hil adopt-hardware" in text, f"{name} does not name the command that does keep the rest"

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    target = Path(init_config()["path"])
    _narrow_by_hand(target, "allow_flash")

    result = init_config(force=True)

    assert result["discarded_narrowings"] == ["debuggers.dut.permissions.allow_flash"]
    assert yaml.safe_load(target.read_text(encoding="utf-8"))["debuggers"]["dut"]["permissions"]["allow_flash"] is True
    # And the carrying regeneration stays `project_config_create`'s alone. A CLI
    # that starts calling it makes the retired sentences true again by the back
    # door, which is exactly the drift this test exists to catch — so the pin is
    # on the binding rather than on the word: to call it, `cli` must hold it.
    assert not hasattr(agentic_hil.cli, "carry_over_permissions")
    assert hasattr(agentic_hil.tools, "carry_over_permissions")

# `init` reads a board, so `init` is held to what a board read is
# held to.
#
# Enumerating probes and connecting in HOTPLUG mode is a hardware read whichever
# command does it — an SWD attach halts the core. `init` did it directly: no
# `before_connect`, no coordinator, no record, while `project_config_create`
# writes the same file from the same read under a lease. That the CLI is the
# operator's authority does not carry the exception; `adopt-hardware` makes the
# same argument about the grant, waives that, and leases anyway.


def test_init_does_not_connect_to_a_probe_another_owner_holds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`agentic-hil init --force` gets a refusal, not the board.

    The holder is a stranger — an MCP server, another project's run, a test
    reactor. It has the physical probe, which the configuration this workspace
    would replace names, so the open-run refusal answers first and the read is
    never reached. It used to be `device_busy` off the read itself, from the
    device lock taken between enumerating and connecting; the guarantee that test
    defended is unchanged and stronger — there is now no enumeration either — and
    the answer names why the write stopped rather than which lock it collided
    with. The holder is still named, in `open_holds`. Rewriting this workspace's
    whole policy in the middle of somebody else's run is not something an
    operator wants either, so nothing is written."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    commands = _one_attached_stlink(monkeypatch)
    first = init_config()
    assert first["ok"] is True, first
    path = Path(first["path"])
    before = path.read_bytes()
    commands.clear()

    stranger = BenchMutex(frontend="stranger", label="other-bench-session")
    stranger.acquire([f"probe:{fold_hardware_id('STLINK123')}"])
    try:
        refused = init_config(force=True)
    finally:
        stranger.release_all()

    assert refused["ok"] is False, refused
    assert refused["error_type"] == "config_write_in_open_run"
    assert [item["holder"]["label"] for item in refused["open_holds"]["busy_devices"]] == ["other-bench-session"]
    # The record and the refusal name the command that ran, not the MCP tool
    # that writes the same file — an incident has to say which one left it.
    assert refused["tool"] == "cli_init"
    assert not any("mode=HOTPLUG" in argument for command in commands for argument in command), commands
    assert commands == [], commands
    assert path.read_bytes() == before


# ---------------------------------------------------------------------------
# And a run that holds no probe is refused too, before the board is read.
#
# The probe refusal above is incidental: it is the device lock the read takes
# doing its job, not a rule about writing this file. Every other write of the
# authoritative configuration has that rule — `project_config_create`,
# `project_config_adopt_hardware` and `agentic-hil grant`/`revoke` all answer
# while a run, a lease or another terminal holds the bench — and `init --force`
# had none. A run that declared only a COM port or only a CAN bus holds no
# probe, so nothing stood in the way at all and the regeneration replaced the
# permissions, the baudrate and the device bindings underneath live
# measurements. These two tests are that hole.


def _initialised_bench(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, with_can_bus: bool = False) -> tuple[Path, Path, list[list[str]]]:
    """A workspace `init` has already configured from the attached board.

    Returns the workspace, the written configuration, and the list this host
    records its commands in — cleared, so anything in it afterwards was asked by
    the second `init` rather than by the first."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    commands = _one_attached_stlink(monkeypatch)
    first = init_config()
    assert first["ok"] is True, first
    path = Path(first["path"])
    if with_can_bus:
        # A bus the operator added afterwards. `init` writes no `can_buses`
        # section, and a bench that has one is exactly the case where the
        # regeneration used to walk through a run holding it.
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        document["can_buses"] = {"dut": {"adapter": "peak", "channel": "PCAN_USBBUS1", "bitrate": 250000}}
        path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    commands.clear()
    return workspace, path, commands


def _assert_refused_before_the_read(refused: JsonObject, *, path: Path, before: bytes, commands: list[list[str]], held: str) -> None:
    """The named refusal, and the proof that no board was touched to reach it."""
    assert refused["ok"] is False, refused
    assert refused["error_type"] == "config_write_in_open_run"
    # The command that ran, not the MCP tool that writes the same file.
    assert refused["tool"] == "cli_init"
    assert refused["retry_safe"] is True
    assert refused["side_effect_committed"] is False
    assert refused["hardware_state"] == "unchanged"
    assert refused["path"] == str(path)
    assert refused["open_holds"]["held_devices"] == [held]
    assert [item["resource"] for item in refused["open_holds"]["busy_devices"]] == [held]
    assert refused["remediation"] == remediation_fields("config_write_in_open_run")["remediation"]
    assert "lease-status" in refused["next_step"]
    # Raised before the bench read, so nothing was said to this host at all:
    # there is no enumeration and no HOTPLUG connect to inspect, and no
    # discovery in the result because none ran.
    assert commands == [], commands
    assert "hardware_discovery" not in refused
    assert path.read_bytes() == before


def test_init_force_is_refused_while_a_run_holds_only_a_com_port(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A run with a COM port and no probe: the case nothing used to stop.

    The port is held, the probe is not, so the board read `init --force` performs
    goes through — and behind it the whole file is replaced while a session is
    reading that port under the permissions and the baudrate it names. The
    refusal is now the same one every other write of this file answers, and it is
    raised before the read rather than fallen into by it."""
    workspace, path, commands = _initialised_bench(tmp_path, monkeypatch)
    config = load_authoritative_config(workspace)
    uart = next(key for key in config_devices(config).lock_keys if key.startswith("com:"))
    before = path.read_bytes()

    tools = AgenticHILToolService(config, frontend="mcp")
    try:
        assert tools.call("bench_run_start", {"devices": [{"kind": "uart", "id": "dut_uart"}], "label": "boot-smoke"})["ok"] is True

        refused = init_config(force=True)

        _assert_refused_before_the_read(refused, path=path, before=before, commands=commands, held=uart)
        # The run itself was not disturbed: it still holds what it declared.
        assert tools.call("bench_run_status")["run_active"] is True
        assert tools.call("bench_run_stop")["ok"] is True
    finally:
        tools.close()

    # And the reset goes through the moment the run ends. Nothing was lost by the
    # refusal; the regeneration is exactly as available after the run as before.
    assert init_config(force=True)["ok"] is True


def test_init_force_is_refused_while_a_run_holds_only_a_can_bus(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """And a CAN bus, which shares nothing with the probe lock at all.

    A COM port at least sits on the same physical adapter as the probe on this
    bench. A CAN bus does not, so a plan that stimulates the body bus while
    somebody runs `init --force` in another terminal was the case with no
    accidental protection whatsoever: the read succeeded, the file was replaced,
    and `allow_write` on the bus came back open underneath a run that had taken
    it closed."""
    workspace, path, commands = _initialised_bench(tmp_path, monkeypatch, with_can_bus=True)
    config = load_authoritative_config(workspace)
    bus = next(key for key in config_devices(config).lock_keys if key.startswith("can:"))
    before = path.read_bytes()

    tools = AgenticHILToolService(config, frontend="mcp")
    try:
        assert tools.call("bench_run_start", {"devices": [{"kind": "can", "id": "dut"}], "label": "body-bus"})["ok"] is True

        refused = init_config(force=True)

        _assert_refused_before_the_read(refused, path=path, before=before, commands=commands, held=bus)
        assert tools.call("bench_run_stop")["ok"] is True
    finally:
        tools.close()

    assert init_config(force=True)["ok"] is True


def test_a_first_init_is_not_refused_by_a_bench_it_has_no_configuration_for(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The refusal is asked of a configuration, so it cannot block the first one.

    A workspace with no loadable configuration names no devices, so there is
    nothing for a hold to be on and no policy for a run to have been taken under.
    Refusing here would leave no way to reach a configured bench at all, which is
    the same reason this case reads the board without a lease."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    _one_attached_stlink(monkeypatch)

    first = init_config()

    assert first["ok"] is True, first
    assert first["hardware_discovery"]["ok"] is True


def test_the_board_init_reads_is_written_into_the_audit_trail(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Reading a board is reading a board, whichever command did it.

    `init` left nothing behind at all, so a bench could be connected to with the
    audit trail showing no access. The record says which probe, which locks were
    held for it and the state those leases actually ended in — the same record
    `project_config_create` leaves for the same read."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    _one_attached_stlink(monkeypatch)
    first = init_config()
    assert first["ok"] is True, first

    regenerated = init_config(force=True)

    assert regenerated["ok"] is True, regenerated
    report = read_last_report(load_authoritative_config(workspace))
    assert report["tool"] == "cli_init"
    assert report["probe_id"] == "STLINK123"
    assert report["resources"] == ["debugger-discovery:all", f"probe:{fold_hardware_id('STLINK123')}"]
    # Written while the board was held and re-committed once the leases reached
    # their terminal state, so the record and the result agree about the bench.
    assert report["lease_state"] == "released"
    assert report["cleanup_required"] is False
    assert report["quarantined"] is False


def test_the_first_init_of_a_workspace_reads_directly_and_says_so(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The one case that cannot be leased, handled rather than refused.

    A workspace with no loadable configuration has no `state_root` to record a
    lease under and no policy to audit against — the machinery does not exist
    rather than being skipped — and it is also the one case where nothing on this
    machine can be holding a board on this project's behalf. Refusing it would
    make a first `init` impossible, which is worse than the defect. What it must
    not be is silent: the result names the unleased read and the reason, so the
    operator is not left inferring it from a record that is not there."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    _one_attached_stlink(monkeypatch)

    first = init_config()

    assert first["ok"] is True, first
    assert first["hardware_discovery"]["ok"] is True
    assert first["hardware_lease"] == {
        "leased": False,
        "reason": "config_file_not_found",
        "summary": first["hardware_lease"]["summary"],
    }
    assert "no loadable configuration" in first["hardware_lease"]["summary"]
    # And the very next one is leased, because by then there is something to
    # lease against. The unleased read is the first and only the first.
    assert init_config(force=True)["hardware_lease"] == {
        "leased": True,
        "tool": "cli_init",
        "summary": "The attached probe was read under a hardware lease, and that read is in the audit trail.",
    }
