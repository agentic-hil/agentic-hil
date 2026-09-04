from __future__ import annotations

import inspect
import json
import os
import re
import sys
from pathlib import Path

import pytest
import yaml
from conftest import FAKE_GDB, FAKE_OPENOCD, FAKE_STLINK, write_config

import agentic_hil.cli
import agentic_hil.tools
from agentic_hil.backends.common import CompletedCommand, cube_clt_programmer_paths
from agentic_hil.backends.stlink import stlink_target_info
from agentic_hil.bench import BenchMutex
from agentic_hil.bootstrap import (
    DEFAULT_PROJECT_PROFILE,
    DISCOVERED_BY_STLINK_CLI,
    DISCOVERED_BY_USB_INVENTORY,
    PLACEHOLDER_COM_PORTS_ANCHOR,
    PLACEHOLDER_TARGET_ANCHOR,
    PROFILE_KEYS_READ,
    PROJECT_PROFILE,
    apply_discovery_to_template,
    apply_profile_to_placeholder,
    autodetected_gdb,
    correlate_com_port,
    discover_attached_hardware,
    enumerate_attached_probes,
    openocd_target_name,
    select_probe_id,
    usb_stlink_probe_ids,
)
from agentic_hil.cli import DEFAULT_CONFIG_TEMPLATE, adopt_hardware, init_config
from agentic_hil.config import (
    GDB_AUTODETECT_CANDIDATES,
    ConfigError,
    debugger_drives_hardware,
    debugger_is_placeholder,
    load_authoritative_config,
    load_config,
)
from agentic_hil.coordination import DEBUGGER_DISCOVERY_RESOURCE
from agentic_hil.devices import config_devices, debugger_device, uart_device
from agentic_hil.knowledge import (
    DEBUGGER_BACKENDS_URI,
    EXCLUSIVE_FLASH_PERMISSIONS,
    read_resource,
    remediation_fields,
)
from agentic_hil.report import read_last_report
from agentic_hil.test_reactor import TestReactor, load_test_config
from agentic_hil.tools import AgenticHILToolService, project_config_create
from agentic_hil.types import CURRENT_CONFIG_VERSION, JsonObject, com_port_is_unbound, fold_hardware_id

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def test_stlink_target_info_extracts_one_identity() -> None:
    assert stlink_target_info("ST-LINK SN : ABC123\nDevice name : STM32F446RE\n") == {
        "probe_id": "ABC123",
        "controller": "STM32F446RE",
    }
    assert stlink_target_info("Device name : STM32F446RE\n") is None


def test_correlate_com_port_requires_one_serial_match() -> None:
    """One hardware identity, the same one `select_probe_id` and the lock use.

    Case folds and nothing else. This used to strip punctuation as well, on the
    theory that a correlation keys nothing, but the port it picks is written into
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

    Everything selection does (folding, refusing, choosing the enumerated
    spelling) runs for real; a test that mocked `discover_attached_hardware`
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
    `adapter_not_found` ("plug the board in") for a board that is plugged in.
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
    to a board the rest of the system does not think this call is about: the
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
        # narrowing: it has to be a flag the default leaves *true*, or the test
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
    # the generated default, and one it names is honoured, which can only
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


def test_a_profile_can_open_a_flash_interlock() -> None:
    """`agentic-hil init` honours a project's example configuration in both
    directions, including the two flash interlocks it otherwise leaves false.

    The default for `allow_raw_debugger_commands` and `allow_mass_erase` is
    false, so an example that names them is the one place a profile *widens*
    rather than narrows: the operator wrote them into a file they own and ran
    `init` against, and this path writes what they wrote. That is why the
    missing-configuration remediation must not promise both interlocks are
    false for the shell route; this pins the behaviour it now describes so a
    later change that silently clamps them false fails here and takes the
    remediation with it."""
    template = yaml.safe_load(DEFAULT_CONFIG_TEMPLATE)
    profile = {
        "target": {"name": "demo", "controller": "stm32f446ret6"},
        "debuggers": {"dut": {"permissions": {flag: True for flag in EXCLUSIVE_FLASH_PERMISSIONS}}},
    }
    discovery = {
        "executable": "C:/ST/STM32_Programmer_CLI.exe",
        "probe_id": "STLINK123",
        "target": {"controller": "STM32F446RE"},
    }

    configured = apply_discovery_to_template(template, profile, discovery)

    permissions = configured["debuggers"]["dut"]["permissions"]
    assert [flag for flag in EXCLUSIVE_FLASH_PERMISSIONS if not permissions[flag]] == [], (
        "a profile that opens the flash interlocks is honoured, so the generated file carries both true"
    )


def _shipped_profiles() -> list[Path]:
    """Every `agentic-hil.config.example.yaml` this repository ships.

    Walked rather than listed by name, so a profile added to a second example
    directory is held to the same rule on the day it lands. Dot directories are
    pruned: a developer's virtualenv lives in one and is not something this
    repository ships."""
    found: list[Path] = []
    for directory, subdirectories, files in os.walk(REPOSITORY_ROOT):
        subdirectories[:] = [name for name in subdirectories if not name.startswith(".")]
        if PROJECT_PROFILE in files:
            found.append(Path(directory) / PROJECT_PROFILE)
    return sorted(found)


def test_the_constant_names_the_keys_a_profile_is_actually_read_for() -> None:
    """`PROFILE_KEYS_READ` is worth something only while it matches the reads.

    The guard below holds every shipped profile to this list, so a list that had
    drifted from `apply_discovery_to_template` would hold them to the wrong one:
    a key the function stopped reading would stay welcome in a file this project
    ships, and a key it started reading would be turned away there. The constant
    is therefore compared against the profile lookups in the function itself
    rather than maintained beside them."""
    # The lookbehind keeps `target_profile.get("name")` out: that is a read of a
    # subkey the profile already handed over, not of the profile itself.
    read = set(re.findall(r'(?<!\w)profile\.get\("(\w+)"', inspect.getsource(apply_discovery_to_template)))

    assert read == set(PROFILE_KEYS_READ), sorted(read.symmetric_difference(PROFILE_KEYS_READ))


@pytest.mark.parametrize("profile_path", _shipped_profiles(), ids=lambda path: path.parent.name)
def test_a_shipped_profile_names_nothing_that_would_be_read_past(profile_path: Path) -> None:
    """A key this project ships in a profile has to be one the profile decides.

    The demo profile opened with `workspace_root` and `state_root`, each carrying
    a plausible absolute path and neither of them an input.
    `apply_discovery_to_template` reads four keys, and both roots are written
    over afterwards by the caller that generates the configuration. Editing
    either placeholder therefore had no effect, produced no warning, and left no
    line in the result of `init` saying the value had been read and discarded,
    which is the one answer this project does not give: a `baudrate` the same
    file names wrongly is refused against `com_ports.<name>.baudrate`. The two
    keys are gone, and this keeps the invitation from coming back under any
    name."""
    loaded = yaml.safe_load(profile_path.read_text(encoding="utf-8"))

    assert isinstance(loaded, dict)
    assert set(loaded) <= set(PROFILE_KEYS_READ), sorted(set(loaded) - set(PROFILE_KEYS_READ))


def test_the_guard_over_shipped_profiles_has_a_profile_to_guard() -> None:
    """A walk that finds nothing parametrizes to nothing and proves nothing."""
    assert _shipped_profiles()


# The bench the profiles below are filled in against. It decides no permission,
# which is the point: what is under test is what the profile and the generation
# agree to write, never what happened to be plugged in.
_PROFILE_BENCH = {
    "ok": True,
    "executable": str(Path(__file__).resolve()),
    "probe_id": "STLINK123",
    "target": {"probe_id": "STLINK123", "controller": "STM32F446RE"},
    "com_port": {"device": "COM3", "serial_number": "STLINK123"},
}


def _generated_from(profile_path: Path) -> JsonObject:
    """The configuration `agentic-hil init` writes for a bench with this profile."""
    profile = yaml.safe_load(profile_path.read_text(encoding="utf-8"))
    assert isinstance(profile, dict), profile_path
    template = yaml.safe_load(DEFAULT_CONFIG_TEMPLATE)
    assert isinstance(template, dict)
    return apply_discovery_to_template(template, profile, _PROFILE_BENCH)


@pytest.mark.parametrize("profile_path", _shipped_profiles(), ids=lambda path: path.parent.name)
def test_a_shipped_profile_generates_a_configuration_the_install_check_calls_safe(profile_path: Path) -> None:
    """A profile this project publishes may not produce a bench its own eval refuses.

    The install evaluation holds a generated configuration to "every declared
    `allow_*` is true except the two flash interlocks", and the demo profile
    shipped `com_ports.dut_uart.permissions.allow_write: false` while the STM32
    starter's copy of the same file shipped `true`. An agent that fetched the
    example from this repository and ran `agentic-hil init --force` therefore
    failed `authoritative config is safe :: com_ports.dut_uart.permissions were
    not all granted by the install: ['allow_write']`, and a stranger following
    the same trail landed on a bench whose serial line could not be written to,
    which surfaces much later as a refused test plan rather than here.

    Stated against the verifier's own function rather than against a restatement
    of its rule, so the two published profiles cannot come apart again and a
    change to the rule reaches this guard on the day it is made.

    Only the profiles in this checkout are walked. The starter's copy lives in
    `agentic-hil/stm32-starter`, a separate repository this suite has no offline
    reach into, so what pins the two together is that the rule they both have to
    satisfy is now enforced here and enforced by the eval that reads the
    starter's own generated configuration in a matrix run.
    """
    # Repository tooling, not package content: imported here so the shipped
    # test module still collects from a source distribution without evals/.
    from evals.install.verifier import every_declared_permission_granted

    generated = _generated_from(profile_path)

    findings = []
    for section in ("debuggers", "com_ports", "can_buses"):
        for name, entry in (generated.get(section) or {}).items():
            granted, detail = every_declared_permission_granted(section, name, entry["permissions"])
            if not granted:
                findings.append(detail)
    assert findings == [], f"{profile_path} generates a configuration the install check refuses"


@pytest.mark.parametrize("profile_path", _shipped_profiles(), ids=lambda path: path.parent.name)
def test_a_shipped_profile_still_generates_a_bench_that_can_flash(profile_path: Path) -> None:
    """The other half, or "grant everything" would satisfy the guard above.

    The two interlocked flags stay false, so the invariant is not met by writing
    `true` everywhere: a profile that reopened either would ship a bench whose
    `flash_firmware` is refused on that probe, which is the failure the pair
    exists to make impossible rather than one it would report."""
    permissions = _generated_from(profile_path)["debuggers"]["dut"]["permissions"]

    assert permissions["allow_flash"] is True
    assert [flag for flag in EXCLUSIVE_FLASH_PERMISSIONS if permissions[flag]] == []


def test_the_profile_that_fills_in_for_a_missing_one_is_held_to_the_same_rule() -> None:
    """Nobody edits `DEFAULT_PROJECT_PROFILE`, and it is still a profile.

    It is what both generation paths use when the workspace ships none, so a key
    added to it that nothing reads would be the same silence one file further
    in, with no operator able to see it at all."""
    assert set(DEFAULT_PROJECT_PROFILE) <= set(PROFILE_KEYS_READ)


@pytest.mark.parametrize("baudrate", ["fast", None, [], True], ids=repr)
def test_a_profile_baudrate_that_is_not_a_number_is_refused_by_name(baudrate: object) -> None:
    """A hand-written profile is the one input here with no schema in front of it.

    `int(profile_port.get("baudrate", 115200))` turned `baudrate: fast` into a
    `ValueError` out of a generation that had already read the board. `True` is
    in the list for the opposite reason: `int(True)` is `1`, which the
    configuration schema accepts as a baudrate, so the traceback was not even the
    worst case: a value nobody meant would have been written into the file and
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
    permission was granted: an operator who wrote `allow_mass_erase: false`
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


def test_init_tells_the_operator_when_a_profile_opened_a_flash_interlock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A profile that opens a flash interlock disables flashing, and `init` must say so.

    `allow_mass_erase` and `allow_raw_debugger_commands` default false so that
    `flash_firmware` works, and a project profile is the one input `init`
    honours that can write one true. When it does, flashing is refused on that
    probe -- so the summary and the permission next step must not repeat the
    standing "the two that are false so that flashing works". That told an
    operator whose profile opened `allow_mass_erase` the opposite of both the
    file it wrote and the bench it describes (review round 2, finding 1). The
    full true/false surface in `permissions` already carried the real value; this
    pins the user-facing prose to it, `allow_mass_erase` above all because it
    cannot be taken back once it has run."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    (workspace / "agentic-hil.config.example.yaml").write_text(
        "target:\n  name: demo\n  controller: stm32f446ret6\n"
        "debuggers:\n  dut:\n    permissions:\n      allow_mass_erase: true\n",
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
    interlock = "debuggers.dut.permissions.allow_mass_erase"

    assert result["ok"] is True, result
    # The file honoured the profile, so the interlock really is open, and it is
    # a widening rather than a narrowing, so it is deliberately absent from
    # `narrowed_permissions`; `permissions` below carries its true value.
    assert written["debuggers"]["dut"]["permissions"]["allow_mass_erase"] is True
    assert result["permissions"]["debuggers"]["dut"]["allow_mass_erase"] is True
    assert interlock not in result["narrowed_permissions"]
    # Neither user-facing field may claim the interlocks are false or that
    # flashing works: both must name the open interlock and say flashing is off.
    assert "the two that are false so that flashing works" not in result["summary"]
    assert interlock in result["summary"]
    assert "flashing is disabled" in result["summary"]
    granted_step = next(step for step in result["next_steps"] if step.startswith("Every permission in this file is true"))
    assert "which are false so that flashing works" not in granted_step
    assert interlock in granted_step
    assert "flash_firmware is refused" in granted_step


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
    for real: a double for `discover_attached_hardware` itself would let a caller
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
    `agentic-hil.config.example.yaml`, so a fresh installation (which has none)
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
    discovery answered and the one command that fills the file in afterwards.

    Discovery here fails with `debugger_not_found`, a missing toolchain and not
    a missing board, so the summary names that reason rather than reporting an
    absent bench: the toolchain, not the board, is what to install. The first
    next step is matched to that too, and installing a toolchain, not attaching
    a board, is what it tells the operator to do.

    Both toolchains, because discovery has two: `debugger_not_found` is now
    reached only when neither STM32CubeProgrammer nor OpenOCD is on the host, so
    the refusal and the remedy name both rather than sending an operator to
    STM32CubeProgrammer when `apt install openocd` is enough."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    monkeypatch.setattr("agentic_hil.bootstrap.find_stm32_programmer_cli", lambda: None)
    monkeypatch.setattr("agentic_hil.bootstrap.find_openocd", lambda: None)

    result = init_config()

    assert result["ok"] is True, result
    # Never None: "did not look" and "looked and found nothing" were the two
    # outcomes this key could not tell apart, and only one of them exists now.
    assert result["hardware_discovery"]["ok"] is False
    assert result["hardware_discovery"]["error_type"] == "debugger_not_found"
    assert result["summary"].startswith(
        "Hardware discovery did not configure a bench (Neither STM32CubeProgrammer's CLI (STM32_Programmer_CLI) nor "
        "OpenOCD (openocd) was found on this host"
    )
    assert result["summary"].endswith("with every permission granted except the two that are false so that flashing works.")
    assert "No attached bench was found" not in result["summary"]
    assert "STM32_Programmer_CLI" in result["next_steps"][0]
    assert "agentic-hil adopt-hardware" in result["next_steps"][0]
    # The remedy is the missing tool, not the board: a `debugger_not_found`
    # placeholder no longer tells the operator to attach a bench that may be
    # plugged in the whole time.
    assert "OpenOCD is the smaller of the two" in result["next_steps"][0]
    assert "Attach the bench" not in result["next_steps"][0]
    # And the second step is the evidence: which binaries were looked for, and
    # that neither resolved. Without it "no bench" was a claim with nothing
    # behind it.
    assert result["next_steps"][1] == (
        "Discovery looked for STM32_Programmer_CLI (STM32CubeProgrammer): not on this host; "
        "openocd (OpenOCD): not on this host."
    )


def test_init_reserves_absent_bench_wording_for_the_empty_probe_result(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`adapter_not_found` is the one placeholder that means "attach a board".

    A programmer that ran and enumerated no ST-Link is the empty-bench result,
    and there "No attached bench was found" is exactly the instruction. The
    wording stays reserved for it so it does not appear over a missing tool or a
    board that answered ambiguously."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    _fixed_stlink(monkeypatch, "No ST-LINK detected\n")

    result = init_config()

    assert result["ok"] is True, result
    assert result["hardware_discovery"]["error_type"] == "adapter_not_found"
    assert result["summary"].startswith("No attached bench was found, so the placeholder")
    # This is the one placeholder whose first next step does say "attach the
    # bench", because here the bench is what is missing.
    assert "Attach the bench and run" in result["next_steps"][0]
    assert "agentic-hil adopt-hardware" in result["next_steps"][0]


def test_init_and_the_server_describe_one_bench(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Two paths, one machine, one answer: the test that fails if they part.

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
    how they learned the board had been discoverable all along: one command
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
# The GDB this host answers with, which both paths read the same way.


def test_discovery_reports_the_first_gdb_the_loader_would_have_picked(monkeypatch: pytest.MonkeyPatch) -> None:
    """One list, one order, read from the loader rather than repeated here.

    A discovery that offered a different GDB from the one the runtime fallback
    resolves would be recommending a change of behaviour while claiming to write
    down the current one. So the candidates and their order are the loader's, and
    `arm-none-eabi-gdb` wins over a plain `gdb` that is also installed."""
    monkeypatch.setattr("agentic_hil.bootstrap.shutil.which", lambda name: f"/opt/{name}" if name in {"gdb-multiarch", "gdb"} else None)
    assert autodetected_gdb() == "/opt/gdb-multiarch"

    monkeypatch.setattr("agentic_hil.bootstrap.shutil.which", lambda name: f"/opt/{name}")
    assert autodetected_gdb() == f"/opt/{GDB_AUTODETECT_CANDIDATES[0]}"

    monkeypatch.setattr("agentic_hil.bootstrap.shutil.which", lambda name: None)
    assert autodetected_gdb() is None


def test_a_generated_configuration_names_the_gdb_this_host_answers_with(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`init` writes the toolchain it found, and adoption is left nothing.

    The same invariant as the test above, extended to the key that made it
    breakable: adoption now fills `debug.gdb_executable`, so a generation that
    left it null would have `adopt-hardware` repairing a file `init` had written
    a second earlier, on a machine where both read the same PATH. Writing it
    changes what the file *says* and not what it resolves to, because with the
    key null the loader runs this very lookup at every load."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    _one_attached_stlink(monkeypatch)
    monkeypatch.setattr("agentic_hil.bootstrap.autodetected_gdb", lambda: FAKE_GDB.as_posix())

    result = init_config()
    assert result["ok"] is True, result
    written = yaml.safe_load(Path(result["path"]).read_text(encoding="utf-8"))
    assert written["debug"]["gdb_executable"] == FAKE_GDB.as_posix()
    assert load_authoritative_config(workspace).debug.gdb_executable == str(FAKE_GDB)

    plan = adopt_hardware(dry_run=True)
    assert plan["ok"] is True, plan
    assert plan["carried"] == []
    assert "debug.gdb_executable" in {item["key"] for item in plan["already_current"]}


def test_a_host_with_no_gdb_generates_the_null_that_autodetects(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing found is written as nothing, which is a working configuration.

    This is the state the report came from: the tool installed before the
    toolchain, so generation had no GDB to name. The file has to stay loadable
    and the key has to stay the one the runtime fallback reads, so that plugging
    the toolchain in later and running `adopt-hardware` is the whole repair."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    _one_attached_stlink(monkeypatch)

    result = init_config()

    assert result["ok"] is True, result
    assert yaml.safe_load(Path(result["path"]).read_text(encoding="utf-8"))["debug"]["gdb_executable"] is None


# ---------------------------------------------------------------------------
# `init --force` is the reset, and it names what the reset cost.

# The sentences that had to go. Every one was true of
# `project_config_create`, which calls `carry_over_permissions`, and false of
# `agentic-hil init --force`, which never has, and every one shipped.
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
    and the bench is open again with nothing saying so: the silent reopening the
    one-way street on `project_config_set` exists to prevent. The behaviour stays
    what its name says: `--force` already discards the baudrate, the
    `resource_id`, the `state_root` and the artifact roots, and a version that
    rescued permissions but not those would be harder to predict rather than
    easier. What it owes the operator is the list. Against master this fails:
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
    as itself instead of as an empty list, and the regeneration still happens."""
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
    not UTF-8 unwritable rather than replaceable, and `init --force`, the one
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
    text, and a file that would not decode was recorded as absence, so when the
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
    # over the original: the double fault the rollback path exists to survive.
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
    read goes without the audit trail, but "this project holds no lease" was
    never "nobody holds this board". Another workspace on the machine can be
    mid-HOTPLUG on the same ST-Link, and the probe lock lives under the user's
    home keyed on the device, reachable with no shared config. So the read takes
    it in `before_connect` (the last point before the HOTPLUG connect) and a
    held board comes back `device_busy` with nothing said to it, exactly as the
    leased path answers. Against the previous behaviour this
    connected regardless."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)

    other_workspace = BenchMutex(frontend="other-workspace")
    other_workspace.acquire([f"probe:{fold_hardware_id('STLINK123')}"])

    state = {"connected": False}

    def fake_discovery(timeout_s: float = 10.0, *, probe_id: object = None, before_connect: object = None, profile: object = None) -> JsonObject:
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

    The other workspace does not reach for `probe:<serial>` by hand: it holds an
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

    def fake_discovery(timeout_s: float = 10.0, *, probe_id: object = None, before_connect: object = None, profile: object = None) -> JsonObject:
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

    Everything else in the file is still gone (that is what `--force` is), but no
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
    # docstring of `agentic-hil grant`, fifty lines below the code that reports
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
    # door, which is exactly the drift this test exists to catch, so the pin is
    # on the binding rather than on the word: to call it, `cli` must hold it.
    assert not hasattr(agentic_hil.cli, "carry_over_permissions")
    assert hasattr(agentic_hil.tools, "carry_over_permissions")

# `init` reads a board, so `init` is held to what a board read is
# held to.
#
# Enumerating probes and connecting in HOTPLUG mode is a hardware read whichever
# command does it: an SWD attach halts the core. `init` did it directly: no
# `before_connect`, no coordinator, no record, while `project_config_create`
# writes the same file from the same read under a lease. That the CLI is the
# operator's authority does not carry the exception; `adopt-hardware` makes the
# same argument about the grant, waives that, and leases anyway.


def test_init_does_not_connect_to_a_probe_another_owner_holds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`agentic-hil init --force` gets a refusal, not the board.

    The holder is a stranger: an MCP server, another project's run, a test
    reactor. It has the physical probe, which the configuration this workspace
    would replace names, so the open-run refusal answers first and the read is
    never reached. It used to be `device_busy` off the read itself, from the
    device lock taken between enumerating and connecting; the guarantee that test
    defended is unchanged and stronger (there is now no enumeration either) and
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
    # that writes the same file: an incident has to say which one left it.
    assert refused["tool"] == "cli_init"
    assert not any("mode=HOTPLUG" in argument for command in commands for argument in command), commands
    assert commands == [], commands
    assert path.read_bytes() == before


# ---------------------------------------------------------------------------
# And a run that holds no probe is refused too, before the board is read.
#
# The probe refusal above is incidental: it is the device lock the read takes
# doing its job, not a rule about writing this file. Every other write of the
# authoritative configuration has that rule: `project_config_create`,
# `project_config_adopt_hardware` and `agentic-hil grant`/`revoke` all answer
# while a run, a lease or another terminal holds the bench, and `init --force`
# had none. A run that declared only a COM port or only a CAN bus holds no
# probe, so nothing stood in the way at all and the regeneration replaced the
# permissions, the baudrate and the device bindings underneath live
# measurements. These two tests are that hole.


def _initialised_bench(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, with_can_bus: bool = False) -> tuple[Path, Path, list[list[str]]]:
    """A workspace `init` has already configured from the attached board.

    Returns the workspace, the written configuration, and the list this host
    records its commands in, cleared, so anything in it afterwards was asked by
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
    goes through, and behind it the whole file is replaced while a session is
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
    held for it and the state those leases actually ended in, the same record
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
    lease under and no policy to audit against (the machinery does not exist
    rather than being skipped) and it is also the one case where nothing on this
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
    # The `reason` is what says which of the two unleased cases this is; the
    # summary names both, because the other one, a configuration that loads and
    # whose state_root nothing can be written under, reaches the same note.
    assert "no configuration loaded" in first["hardware_lease"]["summary"]
    # And the very next one is leased, because by then there is something to
    # lease against. The unleased read is the first and only the first.
    assert init_config(force=True)["hardware_lease"] == {
        "leased": True,
        "tool": "cli_init",
        "summary": "The attached probe was read under a hardware lease, and that read is in the audit trail.",
    }


# ---------------------------------------------------------------------------
# The host that has OpenOCD and no STM32CubeProgrammer.
#
# The supported first path this project documents (Nucleo-F446RE + ST-Link +
# OpenOCD) on the platform it is usually run from. `apt install openocd` is one
# command; STM32CubeProgrammer is a registration wall, and a Linux workstation
# normally has the first and never the second. Discovery used to require the
# second, so `init` on that host wrote a placeholder and `debugger-probes` and
# `adopt-hardware` both refused `debugger_not_found`, with the ST-Link and its
# virtual COM port sitting in `agentic-hil com-ports` the whole time.
#
# The record below is one, as pyserial reports it on Ubuntu 24.04.

NUCLEO_VCP: JsonObject = {
    "device": "/dev/ttyACM0",
    "name": "ttyACM0",
    "description": "STM32 STLink - ST-Link VCP Ctrl",
    "hwid": "USB VID:PID=0483:374B SER=066AFF303435554157113106 LOCATION=1-2:1.2",
    "manufacturer": "STMicroelectronics",
    "product": "STM32 STLink",
    "interface": "ST-Link VCP Ctrl",
    "serial_number": "066AFF303435554157113106",
    "vid": 0x0483,
    "pid": 0x374B,
    "stable_device": "/dev/serial/by-id/usb-STMicroelectronics_STM32_STLink_066AFF303435554157113106-if02",
}

# The 32 ttyS entries the same host lists, abbreviated: motherboard UARTs with
# no USB descriptor at all. They are the noise the ST-Link has to be found in.
HOST_SERIAL_NOISE: list[JsonObject] = [{"device": f"/dev/ttyS{index}", "name": f"ttyS{index}"} for index in range(4)]

STARTER_PROFILE: JsonObject = {
    "target": {"name": "nucleo-f446re-starter", "controller": "stm32f446ret6"},
    "debuggers": {"dut": {"timeout_s": 60, "permissions": {}}},
    "com_ports": {"dut_uart": {"baudrate": 115200, "permissions": {}}},
}

OPENOCD_TARGETS_OUTPUT = """Open On-Chip Debugger 0.12.0
Info : clock speed 2000 kHz
Info : STLINK V2J37M27 (API v2) VID:PID 0483:374B
Info : Target voltage: 3.281853
Info : stm32f4x.cpu: Cortex-M4 r0p1 processor detected
    TargetName         Type       Endian TapName            State
--  ------------------ ---------- ------ ------------------ ------------
 0* stm32f4x.cpu       cortex_m   little stm32f4x.cpu       halted
"""


# The binary the fallback path resolves to. A real file, because the generated
# configuration is validated before it is written and an `executable` that does
# not exist is refused there: the point of this path is a file that loads.
FAKE_OPENOCD_PATH = str(FAKE_OPENOCD)


def _linux_openocd_host(
    monkeypatch: pytest.MonkeyPatch,
    *,
    ports: list[JsonObject] | None = None,
    spawn: object = None,
) -> list[list[str]]:
    """A host with OpenOCD on PATH, no STM32CubeProgrammer, and one Nucleo.

    Only the three host readings are replaced (which binaries resolve, what the
    serial inventory holds, and what a spawned process answers); everything
    discovery does with them runs for real. Returns the commands that were
    spawned, which is normally empty: the whole point of this path is that a
    probe is identified without a word being said to a board."""
    commands: list[list[str]] = []

    def fake_spawn(command: list[str], cwd: str, timeout_s: float) -> CompletedCommand:
        commands.append(command)
        if spawn is None:  # pragma: no cover - a test that spawns supplies one
            raise AssertionError(f"nothing should have been spawned: {command}")
        return spawn(command, cwd, timeout_s)  # type: ignore[operator]

    monkeypatch.setattr("agentic_hil.bootstrap.find_stm32_programmer_cli", lambda: None)
    monkeypatch.setattr("agentic_hil.bootstrap.find_openocd", lambda: FAKE_OPENOCD_PATH)
    monkeypatch.setattr("agentic_hil.bootstrap.spawn_command", fake_spawn)
    monkeypatch.setattr(
        "agentic_hil.bootstrap.list_available_com_ports",
        lambda tool: {"ok": True, "tool": tool, "ports": [*HOST_SERIAL_NOISE, *(ports if ports is not None else [NUCLEO_VCP])]},
    )
    return commands


def test_an_stlink_is_enumerated_from_the_usb_inventory_without_the_cube_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    """The reported host, answered.

    OpenOCD cannot enumerate probes itself, which is why discovery only ever had
    STM32CubeProgrammer's listing to ask. But the host already knows: an ST-Link
    publishes its serial in the USB descriptor of the virtual COM port it
    exposes, `agentic-hil com-ports` was already showing it, and that string is
    exactly what OpenOCD's `adapter serial` takes. So the probe is read out of
    the inventory, the toolchain is the OpenOCD on PATH, and nothing is said to a
    board: the profile names the controller, and a profile is a person's
    statement about their own bench."""
    _linux_openocd_host(monkeypatch)

    result = discover_attached_hardware(profile=STARTER_PROFILE)

    assert result["ok"] is True, result
    assert result["backend"] == "openocd"
    assert result["discovered_by"] == DISCOVERED_BY_USB_INVENTORY
    assert result["executable"] == FAKE_OPENOCD_PATH
    assert result["probe_id"] == "066AFF303435554157113106"
    assert result["target"] == {
        "probe_id": "066AFF303435554157113106",
        "controller": "stm32f446ret6",
        "source": "workspace_profile",
    }
    # And the port that carries the same serial, by its replug-proof name.
    assert result["com_port"]["stable_device"] == NUCLEO_VCP["stable_device"]


def test_only_the_stlink_usb_products_are_read_as_probe_serials() -> None:
    """A serial number is unique within a vendor and nowhere else.

    So the match is the vendor *and* one of the ST-Link product ids. A CH340
    that happens to publish the same string is not this probe, another ST device
    on the same vendor id is not a debug probe at all, and an ST-Link that
    published no serial is not something OpenOCD could be told to open."""
    inventory: JsonObject = {
        "ok": True,
        "ports": [
            NUCLEO_VCP,
            {"device": "/dev/ttyUSB0", "serial_number": "066AFF303435554157113106", "vid": 0x1A86, "pid": 0x7523},
            {"device": "/dev/ttyACM7", "serial_number": "SOMEOTHER", "vid": 0x0483, "pid": 0x5740},
            {"device": "/dev/ttyACM8", "vid": 0x0483, "pid": 0x374B},
        ],
    }

    assert usb_stlink_probe_ids(inventory) == ["066AFF303435554157113106"]
    # An inventory that could not be taken enumerates nothing rather than
    # guessing at an empty bench.
    assert usb_stlink_probe_ids({"ok": False, "summary": "pyserial is not installed"}) == []


def test_one_probe_on_several_interfaces_is_one_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """A V2-1 enumerates more than one port under one serial.

    Folded with the identity rule everything else here selects and locks by, so
    two interfaces of one probe are one probe rather than `ambiguous_hardware`,
    and the spelling written down is the one the host published."""
    second = {**NUCLEO_VCP, "device": "/dev/ttyACM1", "stable_device": None, "serial_number": "066aff303435554157113106"}
    _linux_openocd_host(monkeypatch, ports=[NUCLEO_VCP, second])

    listed = enumerate_attached_probes(com_ports={"ok": True, "ports": [NUCLEO_VCP, second]})

    assert [entry["probe_id"] for entry in listed["probes"]] == ["066AFF303435554157113106"]


def test_two_attached_stlinks_are_still_ambiguous_on_the_usb_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """The ambiguity rule is about boards, not about which enumeration found them."""
    other = {**NUCLEO_VCP, "device": "/dev/ttyACM1", "stable_device": None, "serial_number": "0669FF495451"}
    _linux_openocd_host(monkeypatch, ports=[NUCLEO_VCP, other])

    result = discover_attached_hardware(profile=STARTER_PROFILE)

    assert result["ok"] is False
    assert result["error_type"] == "ambiguous_hardware"
    assert sorted(entry["probe_id"] for entry in result["probes"]) == ["0669FF495451", "066AFF303435554157113106"]
    # And naming one selects it, exactly as it does with the CLI.
    selected = discover_attached_hardware(probe_id="0669ff495451", profile=STARTER_PROFILE)
    assert selected["ok"] is True, selected
    assert selected["probe_id"] == "0669FF495451"


def test_discovery_refuses_naming_both_toolchains_when_the_host_has_neither(monkeypatch: pytest.MonkeyPatch) -> None:
    """A refusal that names one tool sends an operator to the wrong install.

    STM32CubeProgrammer is the heavier of the two and the only one the old
    message mentioned. OpenOCD alone is enough for this bench, so the refusal
    names both, says which is smaller, and records what was looked for."""
    monkeypatch.setattr("agentic_hil.bootstrap.find_stm32_programmer_cli", lambda: None)
    monkeypatch.setattr("agentic_hil.bootstrap.find_openocd", lambda: None)
    monkeypatch.setattr("agentic_hil.bootstrap.list_available_com_ports", lambda tool: {"ok": True, "ports": [NUCLEO_VCP]})

    result = discover_attached_hardware(profile=STARTER_PROFILE)

    assert result["ok"] is False
    assert result["error_type"] == "debugger_not_found"
    assert "STM32_Programmer_CLI" in result["summary"]
    assert "openocd" in result["summary"]
    assert result["tools_searched"] == [
        {"name": "STM32_Programmer_CLI", "provided_by": "STM32CubeProgrammer", "path": None, "found": False},
        {"name": "openocd", "provided_by": "OpenOCD", "path": None, "found": False},
    ]
    # Nothing was enumerated, so nothing claims to have been: the inventory was
    # never read, and the result does not say it was empty.
    assert "stlink_ports" not in result


def test_the_cube_programmer_path_is_untouched_where_that_cli_is_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    """The new route is taken only where the old one cannot run.

    A host with STM32CubeProgrammer keeps the enumeration command, the HOTPLUG
    connect, the backend and the controller read off the board, with the same
    ST-Link sitting in the same inventory."""
    commands: list[list[str]] = []
    responses = iter(
        [
            CompletedCommand("ST-LINK SN : 066AFF303435554157113106\n", "", 0, False, False),
            CompletedCommand("ST-LINK SN : 066AFF303435554157113106\nDevice name : STM32F446RE\n", "", 0, False, False),
        ]
    )

    def fake_spawn(command: list[str], cwd: str, timeout_s: float) -> CompletedCommand:
        commands.append(command)
        return next(responses)

    monkeypatch.setattr("agentic_hil.bootstrap.find_stm32_programmer_cli", lambda: str(Path("C:/ST/STM32_Programmer_CLI.exe")))
    monkeypatch.setattr("agentic_hil.bootstrap.find_openocd", lambda: FAKE_OPENOCD_PATH)
    monkeypatch.setattr("agentic_hil.bootstrap.spawn_command", fake_spawn)
    monkeypatch.setattr("agentic_hil.bootstrap.list_available_com_ports", lambda tool: {"ok": True, "ports": [NUCLEO_VCP]})

    result = discover_attached_hardware(profile=STARTER_PROFILE)

    assert result["backend"] == "stlink"
    assert result["discovered_by"] == DISCOVERED_BY_STLINK_CLI
    assert result["executable"].endswith("STM32_Programmer_CLI.exe")
    # The board named itself, and the profile did not decide it.
    assert result["target"] == {"probe_id": "066AFF303435554157113106", "controller": "STM32F446RE"}
    assert commands[0][-3:] == ["-q", "-l", "st-link-only"]
    assert commands[1][-4:] == ["-c", "port=SWD", "mode=HOTPLUG", "sn=066AFF303435554157113106"]


def test_openocd_names_the_target_when_the_profile_does_not(monkeypatch: pytest.MonkeyPatch) -> None:
    """The second source, and it is read-only.

    `init`, `targets`, `shutdown` with the probe selected by `adapter serial`:
    no flash, no erase, no reset, no halt. What OpenOCD reports is the target the
    script created, which is a family rather than a part number, and that is why
    the profile is asked first."""

    def spawn(command: list[str], cwd: str, timeout_s: float) -> CompletedCommand:
        return CompletedCommand(OPENOCD_TARGETS_OUTPUT, "", 0, False, False)

    commands = _linux_openocd_host(monkeypatch, spawn=spawn)

    result = discover_attached_hardware(profile={"target": {"name": "demo"}})

    assert result["ok"] is True, result
    assert result["target"] == {"probe_id": "066AFF303435554157113106", "controller": "stm32f4x", "source": "openocd"}
    assert commands == [
        [
            sys.executable,
            FAKE_OPENOCD_PATH,
            "-f",
            "interface/stlink.cfg",
            "-c",
            "adapter serial 066AFF303435554157113106",
            "-f",
            "target/stm32f4x.cfg",
            "-c",
            "init",
            "-c",
            "targets",
            "-c",
            "shutdown",
        ]
    ]
    assert not any(word in " ".join(commands[0]) for word in ("program", "flash", "erase", "reset", "halt"))
    # The parser answers on OpenOCD's own table and refuses to pick between two,
    # because naming one core of two would be a guess a reader takes for a fact.
    assert openocd_target_name(OPENOCD_TARGETS_OUTPUT) == "stm32f4x"
    assert openocd_target_name(f"{OPENOCD_TARGETS_OUTPUT} 1  stm32f4x.ap        mem_ap     little stm32f4x.cpu       unknown\n") is None
    assert openocd_target_name("Error: init mode failed (unable to connect to the target)\n") is None


def test_a_probe_that_answers_with_no_target_still_configures_the_bench(monkeypatch: pytest.MonkeyPatch) -> None:
    """A controller nobody could name is a field to fill in, not a bench to withhold.

    The probe serial, the toolchain path and the board's own COM port were all
    found, and each of them is a thing an operator cannot retype correctly. So
    they are written, the controller is left at the placeholder, and the summary
    says which of the two happened."""

    def spawn(command: list[str], cwd: str, timeout_s: float) -> CompletedCommand:
        return CompletedCommand("", "Error: init mode failed (unable to connect to the target)\n", 1, False, False)

    _linux_openocd_host(monkeypatch, spawn=spawn)

    result = discover_attached_hardware(profile={"target": {"name": "demo"}})

    assert result["ok"] is True, result
    assert result["probe_id"] == "066AFF303435554157113106"
    assert result["target"] is None
    assert "target was not identified" in result["summary"]
    assert "no target" in result["target_discovery"]["summary"]


def test_the_generated_entry_names_the_backend_that_actually_answered() -> None:
    """A file that says `stlink` where there is no STM32CubeProgrammer is a file
    that loads and refuses at the first call.

    So the entry's `type` is the backend discovery ran on, and an OpenOCD entry
    keeps the two scripts OpenOCD reaches a board through: without them the
    entry names no route to the target at all. It is also the word
    `plan_adoption` compares an existing entry's `type` against, so a bench
    generated by `init` and one filled in by `adopt-hardware` agree."""
    configured = apply_discovery_to_template(
        yaml.safe_load(DEFAULT_CONFIG_TEMPLATE),
        STARTER_PROFILE,
        {
            "backend": "openocd",
            "executable": FAKE_OPENOCD_PATH,
            "probe_id": "066AFF303435554157113106",
            "target": {"controller": "stm32f446ret6"},
            "com_port": NUCLEO_VCP,
        },
    )

    entry = configured["debuggers"]["dut"]
    assert entry["type"] == "openocd"
    assert entry["executable"] == FAKE_OPENOCD_PATH
    assert entry["probe_id"] == "066AFF303435554157113106"
    assert entry["interface_cfg"] == "interface/stlink.cfg"
    assert entry["target_cfg"] == "target/stm32f4x.cfg"
    assert configured["target"] == {"name": "nucleo-f446re-starter", "controller": "stm32f446ret6"}
    # The port the probe carries, by its replug-proof name, with the identity
    # that makes the name checkable.
    assert configured["com_ports"]["dut_uart"] == {
        "device": NUCLEO_VCP["stable_device"],
        "baudrate": 115200,
        "serial_number": "066AFF303435554157113106",
        "vid": 0x0483,
        "pid": 0x374B,
        "permissions": {"allow_write": True},
    }


def test_a_discovery_that_found_the_cube_cli_still_writes_an_stlink_entry() -> None:
    """The neighbouring half: nothing about the Windows bench moved."""
    configured = apply_discovery_to_template(
        yaml.safe_load(DEFAULT_CONFIG_TEMPLATE),
        STARTER_PROFILE,
        {
            "backend": "stlink",
            "executable": "C:/ST/STM32_Programmer_CLI.exe",
            "probe_id": "066AFF303435554157113106",
            "target": {"controller": "STM32F446RE"},
            "com_port": None,
        },
    )

    assert configured["debuggers"]["dut"]["type"] == "stlink"
    assert configured["debuggers"]["dut"]["interface"] == "SWD"
    assert "interface_cfg" not in configured["debuggers"]["dut"]


def test_init_on_a_linux_openocd_host_writes_the_bench_it_found(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """End to end, on the host the report came from.

    `init` from the starter root wrote `probe_id: null`, `executable: null`,
    `com_ports: {}` and said no attached bench was found. It now writes the
    probe, the OpenOCD on PATH and the board's own UART, and says which
    enumeration answered and what it saw."""
    workspace = tmp_path / "starter"
    workspace.mkdir()
    (workspace / PROJECT_PROFILE).write_text(yaml.safe_dump(STARTER_PROFILE), encoding="utf-8")
    monkeypatch.chdir(workspace)
    _linux_openocd_host(monkeypatch)

    result = init_config()

    assert result["ok"] is True, result
    assert result["summary"].startswith("Attached hardware was discovered and configured")
    written = load_authoritative_config(workspace)
    assert written.debuggers["dut"].type == "openocd"
    assert written.debuggers["dut"].executable == FAKE_OPENOCD_PATH
    assert written.debuggers["dut"].probe_id == "066AFF303435554157113106"
    assert written.target.controller == "stm32f446ret6"
    assert written.com_ports["dut_uart"].device == NUCLEO_VCP["stable_device"]
    assert written.com_ports["dut_uart"].serial_number == "066AFF303435554157113106"
    # And the account of what discovery did, which is what "No attached bench
    # was found" left an operator no way to check.
    assert result["next_steps"][0] == (
        "Discovery looked for STM32_Programmer_CLI (STM32CubeProgrammer): not on this host; "
        f"openocd (OpenOCD): found at {FAKE_OPENOCD_PATH}. ST-Link serial port(s) on this host: "
        f"066AFF303435554157113106 on {NUCLEO_VCP['stable_device']}."
    )


def test_an_enumerated_stlink_is_never_reported_as_an_absent_bench(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The absent-bench wording has to be true to be said.

    An ST-Link that published no serial number enumerates no probe, so discovery
    answers `adapter_not_found` while the host inventory is showing the probe and
    its virtual COM port. Telling that operator no bench was found contradicts a
    listing they can read; the ports are named instead."""
    workspace = tmp_path / "starter"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    _linux_openocd_host(monkeypatch, ports=[{key: value for key, value in NUCLEO_VCP.items() if key != "serial_number"}])

    result = init_config()

    assert result["ok"] is True, result
    assert result["hardware_discovery"]["error_type"] == "adapter_not_found"
    assert "No attached bench was found" not in result["summary"]
    assert "No usable probe serial was enumerated" in result["summary"]
    assert NUCLEO_VCP["stable_device"] in result["summary"]
    assert "no serial on" in result["next_steps"][1]


def test_adopt_hardware_fills_a_placeholder_on_a_host_with_only_openocd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The way back into a placeholder, on the host that could not take it.

    `adopt-hardware --com-port dut_uart` refused `debugger_not_found` there for
    the same reason `init` wrote a placeholder: both read the board through one
    enumeration, and that enumeration needed a toolchain the host does not have.
    They share the read, so fixing it fixes both, and this pins that they do
    rather than that they happen to.

    The three values it carries are the three nobody retypes correctly: the
    24-character probe serial, the toolchain path, and which of the host's
    thirty-odd serial devices is this board's."""
    workspace = tmp_path / "starter"
    workspace.mkdir()
    (workspace / PROJECT_PROFILE).write_text(yaml.safe_dump(STARTER_PROFILE), encoding="utf-8")
    monkeypatch.chdir(workspace)
    # The ordinary order: the tool is installed, the board is plugged in after.
    _linux_openocd_host(monkeypatch, ports=[])
    placeholder = init_config()
    assert placeholder["ok"] is True, placeholder
    assert load_authoritative_config(workspace).debuggers["dut"].probe_id is None

    _linux_openocd_host(monkeypatch)
    result = adopt_hardware(com_port_id="dut_uart")

    assert result["ok"] is True, result
    assert result["applied"] is True
    carried = {item["key"]: item["value"] for item in result["carried"]}
    assert carried["debuggers.dut.probe_id"] == "066AFF303435554157113106"
    assert carried["debuggers.dut.executable"] == FAKE_OPENOCD_PATH
    # The controller is already right: the placeholder carried it out of this
    # project's profile, so adoption reports it as current rather than writing
    # it again. What only the attached hardware knows is what is left to carry.
    assert {"key": "target.controller", "value": "stm32f446ret6"} in result["already_current"]
    assert carried["com_ports.dut_uart.device"] == NUCLEO_VCP["stable_device"]
    assert carried["com_ports.dut_uart.serial_number"] == "066AFF303435554157113106"
    # And nothing under `unavailable` is about the toolchain: the entry the
    # skeleton wrote is `type: openocd`, and this discovery ran on openocd, so
    # the executable belongs in it.
    assert [item["key"] for item in result["unavailable"]] == []
    written = load_authoritative_config(workspace)
    assert written.debuggers["dut"].executable == FAKE_OPENOCD_PATH
    assert written.com_ports["dut_uart"].device == NUCLEO_VCP["stable_device"]


# ---------------------------------------------------------------------------
# The placeholder a project profile fills in, and the port it can declare
# without binding.


def test_the_placeholder_carries_the_profiles_names_instead_of_the_examples(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A workspace that says what its board is should not be handed `example-target`.

    The profile is on disk and names the board and the ports; the placeholder
    said `example-target`, `unknown-controller` and `com_ports: {}` anyway, so
    every fact the project had already stated had to be typed again after
    `adopt-hardware`. Now the names go in, and only the names: no device, which
    is the one thing discovery did not find out."""
    workspace = tmp_path / "starter"
    workspace.mkdir()
    (workspace / PROJECT_PROFILE).write_text(yaml.safe_dump(STARTER_PROFILE), encoding="utf-8")
    monkeypatch.chdir(workspace)
    _linux_openocd_host(monkeypatch, ports=[])

    result = init_config()

    assert result["ok"] is True, result
    written = load_authoritative_config(workspace)
    assert written.target.name == "nucleo-f446re-starter"
    assert written.target.controller == "stm32f446ret6"
    assert sorted(written.com_ports) == ["dut_uart"]
    port = written.com_ports["dut_uart"]
    assert port.baudrate == 115200
    assert port.permissions.allow_write is True
    # Declared and not bound: the entry says which port this project has and
    # says nothing about which of the host's devices it is.
    assert com_port_is_unbound(port) is True
    assert port.device == ""
    # The skeleton is still the annotated file an operator reads, which a YAML
    # round trip of the document would have thrown away.
    assert "# Every debug probe is a named entry" in Path(written.config_path).read_text(encoding="utf-8")


def test_a_workspace_with_no_profile_still_gets_the_shipped_skeleton(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing to carry, nothing carried.

    The built-in `DEFAULT_PROJECT_PROFILE` fills a *discovered* bench; it is not
    consulted here, because putting a `dut_uart` into a workspace that declared
    none would be this command inventing an entry rather than honouring one."""
    workspace = tmp_path / "bare"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    _linux_openocd_host(monkeypatch, ports=[])

    result = init_config()

    assert result["ok"] is True, result
    written = load_authoritative_config(workspace)
    assert written.target.name == "example-target"
    assert written.target.controller == "unknown-controller"
    assert written.com_ports == {}


def test_the_placeholder_anchors_are_in_the_skeleton_they_quote() -> None:
    """The substitution is textual, so what it quotes has to still be there.

    A skeleton that reworded either region would silently stop carrying the
    profile, which is the failure this whole change removes. `init` raises
    instead of writing that file, and this is the check that keeps the raise
    from ever being reached in the field."""
    assert PLACEHOLDER_TARGET_ANCHOR in DEFAULT_CONFIG_TEMPLATE
    assert PLACEHOLDER_COM_PORTS_ANCHOR in DEFAULT_CONFIG_TEMPLATE
    with pytest.raises(ConfigError) as refused:
        apply_profile_to_placeholder("target: {}\n", {"target": {"name": "demo"}})
    assert refused.value.error_type == "config_invalid"


def test_a_plan_naming_an_unbound_port_is_refused_by_the_port_not_by_the_plan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The refusal the reported operator actually saw, corrected.

    `test_config_invalid: Test step references a COM port that is not in the
    authoritative config (steps[1].port_id dut_uart)` was wrong twice: the plan
    named a port the project declares, and the thing that was missing was the
    device. The plan is not the thing to fix, so the refusal names the port, the
    key that is empty, and the command that fills it."""
    workspace = tmp_path / "starter"
    workspace.mkdir()
    (workspace / PROJECT_PROFILE).write_text(yaml.safe_dump(STARTER_PROFILE), encoding="utf-8")
    monkeypatch.chdir(workspace)
    _linux_openocd_host(monkeypatch, ports=[])
    assert init_config()["ok"] is True
    config = load_authoritative_config(workspace)

    plan = workspace / "nominal.testconfig.yaml"
    plan.write_text(
        yaml.safe_dump(
            {
                "version": 2,
                "name": "nominal",
                "steps": [{"action": "uart_open", "port_id": "dut_uart"}],
            }
        ),
        encoding="utf-8",
    )
    service = AgenticHILToolService(config)
    try:
        refused = TestReactor(config, service).run(load_test_config(str(plan), str(workspace)))
    finally:
        service.close()

    assert refused["ok"] is False
    validation = refused["validation_error"]
    assert validation["error_type"] == "com_port_not_bound"
    assert validation["port_id"] == "dut_uart"
    assert validation["unbound_key"] == "com_ports.dut_uart.device"
    assert "names no device yet" in validation["summary"]
    assert "not in the authoritative config" not in validation["summary"]
    assert "adopt-hardware" in validation["next_step"]


def test_every_com_tool_refuses_an_unbound_port_by_name(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """One gate, so the four tools cannot answer this differently.

    `com_session_start` would otherwise reach an open on the empty string and
    report whatever the operating system said about it, which is a backend error
    about a bench that was never bound rather than a refusal about a
    configuration."""
    workspace = tmp_path / "starter"
    workspace.mkdir()
    (workspace / PROJECT_PROFILE).write_text(yaml.safe_dump(STARTER_PROFILE), encoding="utf-8")
    monkeypatch.chdir(workspace)
    _linux_openocd_host(monkeypatch, ports=[])
    assert init_config()["ok"] is True
    service = AgenticHILToolService(load_authoritative_config(workspace))
    try:
        for tool, arguments in (
            ("com_session_start", {"port_id": "dut_uart"}),
            ("com_write", {"port_id": "dut_uart", "text": "hello"}),
            ("com_read", {"port_id": "dut_uart"}),
        ):
            refused = service.call(tool, arguments)
            assert refused["ok"] is False, (tool, refused)
            assert refused["error_type"] == "com_port_not_bound", tool
            assert refused["field"] == "com_ports.dut_uart.device", tool
            assert refused["side_effect_committed"] is False, tool
            assert refused["retry_safe"] is True, tool
    finally:
        service.close()


def test_a_bound_port_is_untouched_by_any_of_it(tmp_path: Path) -> None:
    """The neighbouring behaviour: a configuration that names a device is read
    exactly as it was, identity rule included."""
    path = write_config(
        tmp_path,
        com_ports_yaml=(
            "com_ports:\n"
            "  dut_uart:\n"
            '    device: "COM7"\n'
            "    baudrate: 115200\n"
            '    serial_number: "066AFF303435554157113106"\n'
        ),
    )
    config = load_config(str(path))

    port = config.com_ports["dut_uart"]
    assert port.device == "COM7"
    assert com_port_is_unbound(port) is False
    assert uart_device(config, "dut_uart").lock_key == "com:serial:066aff303435554157113106"


def test_an_unbound_port_locks_under_its_own_name_and_says_what_it_is(tmp_path: Path) -> None:
    """A key that is always something, and a warning that is true.

    `com:` for every unbound entry on the host would be one lock key shared by
    unrelated benches; the volatile-name warning would tell an operator their
    port may come to mean another board when it currently means none."""
    path = write_config(
        tmp_path,
        com_ports_yaml="com_ports:\n  dut_uart:\n    baudrate: 115200\n",
    )
    config = load_config(str(path))

    device = uart_device(config, "dut_uart")
    assert device.lock_key == "com:unbound:dut_uart"
    assert "names no device yet" in str(device.identity_warning)


def test_version_3_does_not_demand_an_identity_from_a_port_with_no_device(tmp_path: Path) -> None:
    """Version 3 is about a name that can come to reach another board.

    An entry with no device has no such name, so demanding it declare what
    identifies it would refuse the very file `init` writes when no bench was
    attached. The rule applies in full from the moment a device is named."""
    path = write_config(
        tmp_path,
        config_version=CURRENT_CONFIG_VERSION,
        com_ports_yaml="com_ports:\n  dut_uart:\n    baudrate: 115200\n",
    )

    config = load_config(str(path))

    assert com_port_is_unbound(config.com_ports["dut_uart"]) is True
    # And the same file with a kernel name and nothing else is still refused,
    # which is the rule this leaves alone.
    refused = write_config(
        tmp_path / "bound",
        config_version=CURRENT_CONFIG_VERSION,
        com_ports_yaml='com_ports:\n  dut_uart:\n    device: "COM7"\n    baudrate: 115200\n',
    )
    with pytest.raises(ConfigError) as error:
        load_config(str(refused))
    assert error.value.error_type == "config_invalid"
    assert "com_ports.dut_uart" in error.value.summary


def test_the_reference_resource_states_which_enumeration_answers() -> None:
    """An agent reading the backend matrix has to be able to predict the entry.

    Which of the two enumerations answers on a host decides the `type` and the
    `executable` a generated `debuggers` entry gets, so an agent that reads this
    resource and then reads a generated file has to find them agreeing. It is
    also where the answer to "why is this bench openocd" lives for an agent that
    never sees `init`'s own output."""
    served = read_resource(DEBUGGER_BACKENDS_URI)
    assert served is not None
    document = json.loads(str(served["text"]))

    rule = document["bootstrap_discovery"]
    assert "STM32_Programmer_CLI" in rule["rule"]
    assert "USB serial inventory" in rule["rule"]
    assert "type: openocd" in rule["usb_serial_inventory"]
    assert "type: stlink" in rule["stm32cubeprogrammer_cli"]
    assert "discovered_by" in rule["reported_as"]
    # And the read that names the target without that CLI is stated as
    # read-only, because an agent has to know what an `init` costs the board.
    assert "Neither flashes, erases, resets nor halts." in rule["target_identity_without_the_cli"]
