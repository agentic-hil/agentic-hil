"""Carrying an attached board into a configuration that was written without it.

`setup` ran with nothing plugged in, so `init` wrote placeholders,
and after plugging the board in there was no way back into the file. The agent
stopped correctly and printed a 24-character probe serial for a person to retype.

The four things the issue asks to be true are one test each, and they are the
first four here:

* a placeholder configuration can be completed without hand transcription,
* permissions do not move while that happens,
* a value a person set is never replaced,
* and the whole sequence (`setup` without a board, plug in, green `doctor`)
  runs without a person transcribing anything into the YAML. The board's own
  identity is carried with the file untouched; the one field that is not a
  board's identity, `debuggers.<name>.type`, decides which program drives the
  board and is deliberately outside every write door, so a bench whose skeleton
  says `openocd` and whose probe is an ST-Link needs that one operator edit. Both
  halves are a test, because "nobody edits YAML" is a claim worth being exact
  about.

The rest is what "unset" means precisely, what happens when discovery finds more
than one board or none, and what happens when it contradicts the file. Every one
of those has to have an answer a caller can act on; a silent choice among them is
the failure mode that would make this path worse than the retyping.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml
from conftest import FAKE_GDB, FAKE_STLINK, write_authoritative_config

from agentic_hil.adopt import (
    PROJECT_CONFIG_ADOPT,
    _release_refusal,
    is_unset,
    plan_adoption,
    project_config_adopt_hardware,
)
from agentic_hil.bench import BenchMutex
from agentic_hil.cli import adopt_hardware, doctor, init_config
from agentic_hil.config import DEFAULT_CONFIG_TEMPLATE, GENERATED_PROJECT_PERMISSIONS, load_authoritative_config
from agentic_hil.configstate import STATE_UNREADABLE
from agentic_hil.configwrite import ACTOR_HUMAN, PROJECT_CONFIG_SET
from agentic_hil.coordination import HardwareCoordinator, HardwareLease
from agentic_hil.knowledge import CONFIG_DESCRIPTION_RIGHT, CONFIG_PERMISSIONS_RIGHT, CONFIG_WRITE_RIGHT
from agentic_hil.tools import AgenticHILToolService
from agentic_hil.types import fold_hardware_id

PROBE_SERIAL = "0045002B3038510934333935"
PROVENANCE = {"created_by": "agent", "created_via": "mcp:project_config_create", "created_at": "2026-01-01T00:00:00Z", "agentic_hil_version": "0.0.0"}


def attached(monkeypatch: pytest.MonkeyPatch, **overrides: Any) -> dict:
    """Stand in for bootstrap discovery: one ST-Link, one target, one COM port.

    The `before_connect` hook is honoured rather than ignored: it is where the
    real discovery takes the machine-wide lock on the probe it is about to talk
    to, so a double that skipped it would let every test pass with that lock
    never taken."""
    discovery: dict[str, Any] = {
        "ok": True,
        "tool": "bootstrap_hardware_discovery",
        "backend": "stlink",
        "executable": FAKE_STLINK.as_posix(),
        "probe_id": PROBE_SERIAL,
        "target": {"probe_id": PROBE_SERIAL, "controller": "STM32F446RE"},
        "com_port": {"device": "COM9", "serial_number": PROBE_SERIAL},
        "available_com_ports": {"ok": True, "ports": [{"device": "COM9", "serial_number": PROBE_SERIAL}]},
        "side_effect_committed": False,
        "side_effect_status": "not_started",
        "hardware_state": "unchanged",
        "cleanup_required": False,
        "summary": "One attached STM32 target was identified through ST-Link HOTPLUG discovery.",
    }
    discovery.update(overrides)
    seen: dict[str, Any] = {}

    def discover(timeout_s: float = 10.0, *, probe_id: str | None = None, before_connect: Any = None, profile: Any = None) -> dict:
        seen["probe_id"] = probe_id
        if discovery.get("ok") is not True:
            return discovery
        if before_connect is not None:
            refusal = before_connect(str(discovery["probe_id"]))
            if refusal is not None:
                return refusal
        # Only past the hook: this stands for the HOTPLUG connect, the first
        # thing that is said to one particular board.
        seen["connected_probe_id"] = discovery["probe_id"]
        return discovery

    monkeypatch.setattr("agentic_hil.adopt.discover_attached_hardware", discover)
    discovery["_selected"] = seen
    return discovery


def placeholder_bench(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, config_version: int | None = 2, permissions: dict[str, bool] | None = None, **grants: bool) -> tuple[Path, Path]:
    """The file `init` writes when nothing was attached: type stlink, all null.

    `type` is the one field of the entry this path never writes, because a probe
    hands you an identity and never a choice of debug stack. A bench that means
    to be driven through ST-Link says so; everything else in the entry is still
    the placeholder `init` wrote.

    `config_version=None` is the file a project written before the read-free
    model already has on disk, where reading a probe still needs `allow_probe`."""
    workspace = (tmp_path / "workspace").resolve()
    path = write_authoritative_config(
        workspace,
        monkeypatch,
        config_root=Path(os.environ["APPDATA"]) / "config-adopt",
        config_version=config_version,
        permissions={} if permissions is None else permissions,
        debugger_type="stlink",
    )
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    document["target"] = {"name": "example-target", "controller": "unknown-controller"}
    document["debuggers"]["dut"].update({"probe_id": None, "executable": None})
    document["com_ports"] = {}
    document["provenance"] = dict(PROVENANCE)
    document["permissions"] = {CONFIG_WRITE_RIGHT: False, CONFIG_DESCRIPTION_RIGHT: False, CONFIG_PERMISSIONS_RIGHT: False, **grants}
    path.write_text("# Generated by Agentic HIL; every permission below is false.\n" + yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    monkeypatch.chdir(workspace)
    return workspace, path


def service(workspace: Path) -> AgenticHILToolService:
    return AgenticHILToolService(load_authoritative_config(workspace), frontend="mcp")


def document_of(path: Path) -> dict:
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def granted(node: object, prefix: str = "") -> dict[str, Any]:
    """Every permission in a document, by dotted path, whatever it is called."""
    found: dict[str, Any] = {}
    if isinstance(node, dict):
        for key, value in node.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            if str(key).startswith("allow_"):
                found[path] = value
            elif str(key) == "permissions" and isinstance(value, dict):
                found.update({f"{path}.{flag}": state for flag, state in value.items()})
            found.update(granted(value, path))
    return found


# ---------------------------------------------------------------------------
# The four boxes this has to tick.


def test_a_placeholder_configuration_is_completed_without_transcription(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The first box: the values land in the file, and nobody types them.

    The call carries no value of its own: every one of the four keys below comes
    out of discovery, and the tool's whole argument surface is booleans and
    names."""
    workspace, path = placeholder_bench(tmp_path, monkeypatch, **{CONFIG_DESCRIPTION_RIGHT: True})
    attached(monkeypatch)
    tools = service(workspace)
    try:
        planned = tools.call(PROJECT_CONFIG_ADOPT)
        assert planned["ok"] is True, planned
        assert planned["applied"] is False, "reading hardware must not write by itself"
        assert sorted(item["key"] for item in planned["carried"]) == [
            "com_ports.dut_uart.device",
            "com_ports.dut_uart.serial_number",
            "debuggers.dut.executable",
            "debuggers.dut.probe_id",
            "target.controller",
        ]
        assert document_of(path)["debuggers"]["dut"]["probe_id"] is None

        applied = tools.call(PROJECT_CONFIG_ADOPT, {"apply": True})
        assert applied["ok"] is True, applied
        assert applied["applied"] is True
    finally:
        tools.close()

    document = document_of(path)
    assert document["debuggers"]["dut"]["probe_id"] == PROBE_SERIAL
    assert document["debuggers"]["dut"]["executable"] == FAKE_STLINK.as_posix()
    assert document["target"]["controller"] == "stm32f446re"
    assert document["com_ports"]["dut_uart"]["device"] == "COM9"
    # One write path, so one provenance record: the same one project_config_set
    # writes, naming the call that actually moved the keys.
    assert document["provenance"]["last_modified_via"] == f"mcp:{PROJECT_CONFIG_ADOPT}"
    assert document["provenance"]["last_modified_by"] == "agent"
    assert document["provenance"]["modification_count"] == 1
    assert document["provenance"]["created_at"] == PROVENANCE["created_at"], "a change does not answer who wrote the file"


def test_permissions_do_not_move_while_the_hardware_is_carried_in(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The second box, checked against the document rather than against the call.

    Every permission in the file is collected before and after, by walking what
    is actually there, including the COM port entry this call brings into
    existence, which arrives with its permissions written by the server."""
    workspace, path = placeholder_bench(tmp_path, monkeypatch, **{CONFIG_DESCRIPTION_RIGHT: True, CONFIG_PERMISSIONS_RIGHT: True})
    attached(monkeypatch)
    before = granted(document_of(path))
    tools = service(workspace)
    try:
        applied = tools.call(PROJECT_CONFIG_ADOPT, {"apply": True})
    finally:
        tools.close()

    assert applied["ok"] is True, applied
    assert applied["permissions_changed"] == []
    after = granted(document_of(path))
    assert not [key for key in set(before) | set(after) if before.get(key, False) != after.get(key, False)]
    # The port that did not exist before now does, and it may be read and not
    # written: the permission block came from the server, not from this call.
    assert after["com_ports.dut_uart.permissions.allow_write"] is False
    assert applied["created_entries"] == ["com_ports.dut_uart"]
    # Even with the permissions grant open, which is the state that could have
    # let something through: nothing in this path can name a permission key.
    assert all(not str(item["key"]).startswith("permissions") and ".permissions." not in str(item["key"]) for item in applied["carried"])


def test_a_value_a_person_set_is_reported_and_never_replaced(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The third box, and the one that decides whether this path is safe at all.

    The board is the one this entry is configured for (the probe serials agree)
    and the file still says something else about it than the hardware does. That
    is a person's statement about their own bench, maybe a controller variant
    they know better than a HOTPLUG read does, and it is reported rather than
    corrected."""
    workspace, path = placeholder_bench(tmp_path, monkeypatch, **{CONFIG_DESCRIPTION_RIGHT: True})
    document = document_of(path)
    document["debuggers"]["dut"]["probe_id"] = PROBE_SERIAL
    document["target"]["controller"] = "stm32f411re"
    document["com_ports"] = {"dut_uart": {"device": "COM4", "baudrate": 115200, "permissions": {"allow_write": False}}}
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    attached(monkeypatch)

    tools = service(workspace)
    try:
        applied = tools.call(PROJECT_CONFIG_ADOPT, {"apply": True})
    finally:
        tools.close()

    assert applied["ok"] is True, applied
    kept = {item["key"]: item for item in applied["kept"]}
    assert sorted(kept) == ["com_ports.dut_uart.device", "target.controller"]
    assert kept["target.controller"]["configured_value"] == "stm32f411re"
    assert kept["target.controller"]["discovered_value"] == "stm32f446re"
    after = document_of(path)
    assert after["target"]["controller"] == "stm32f411re"
    assert after["com_ports"]["dut_uart"]["device"] == "COM4"
    # The disagreement is named in what the caller is told to do next, so it
    # cannot be read as "everything is fine now".
    assert any("kept" in step for step in applied["next_steps"])


def test_a_different_attached_board_is_refused_whole_rather_than_partly_carried(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A probe disagreement is not a per-key disagreement.

    The identity keys describe one board between them. With `PROBE-A` configured
    and `PROBE-B` attached, carrying only the keys that happen to be unset would
    leave a `probe_id` naming A beside B's controller, B's toolchain path and B's
    COM device: a configuration whose debugger and UART address different
    boards, which loads and passes `doctor`. So nothing is planned and nothing is
    written."""
    workspace, path = placeholder_bench(tmp_path, monkeypatch, **{CONFIG_DESCRIPTION_RIGHT: True})
    document = document_of(path)
    document["debuggers"]["dut"]["probe_id"] = "066AFF495451885087171450"
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    before = path.read_text(encoding="utf-8")
    discovery = attached(monkeypatch)

    tools = service(workspace)
    try:
        refused = tools.call(PROJECT_CONFIG_ADOPT, {"apply": True})
    finally:
        tools.close()

    assert refused["ok"] is False
    assert refused["error_type"] == "hardware_mismatch"
    assert refused["configured_probe_id"] == "066AFF495451885087171450"
    assert refused["discovered_probe_id"] == PROBE_SERIAL
    assert "carried" not in refused, "a mismatched board produces no plan at all"
    assert path.read_text(encoding="utf-8") == before
    # And the configured serial is what selected the board in the first place, so
    # on a bench with both attached this reads the one the entry is for.
    assert discovery["_selected"]["probe_id"] == "066AFF495451885087171450"


def test_the_pure_planner_refuses_a_mixed_board_plan() -> None:
    """The same rule where a reviewer can see it, with no file involved.

    `plan_adoption` is the part that decides what would be written. A planner
    that produced a successful plan here would mean the refusal above lives in
    the caller and could be bypassed by any second caller."""
    document = {
        "debuggers": {"dut": {"type": "stlink", "probe_id": "PROBE-A", "executable": None}},
        "target": {"name": "example-target", "controller": "unknown-controller"},
        "com_ports": {},
    }
    discovery = {
        "ok": True,
        "backend": "stlink",
        "probe_id": "PROBE-B",
        "executable": "/opt/stm32/STM32_Programmer_CLI",
        "target": {"controller": "STM32F446RE"},
        "com_port": {"device": "COM9", "serial_number": "PROBE-B"},
    }
    plan = plan_adoption(document, discovery)
    assert plan["ok"] is False
    assert plan["error_type"] == "hardware_mismatch"
    assert plan["configured_probe_id"] == "PROBE-A"
    assert plan["discovered_probe_id"] == "PROBE-B"

    # A serial that differs only in the spelling every lock key in this
    # repository folds is the same board, and is planned normally.
    same_board = plan_adoption({**document, "debuggers": {"dut": {**document["debuggers"]["dut"], "probe_id": "probe-b"}}}, discovery)
    assert same_board["ok"] is True
    assert [item["key"] for item in same_board["kept"]] == ["debuggers.dut.probe_id"]
    assert sorted(item["key"] for item in same_board["carried"]) == ["com_ports.dut_uart.device", "com_ports.dut_uart.serial_number", "debuggers.dut.executable", "target.controller"]


def _skeleton_from_init(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str) -> Path:
    """Run `init` with nothing attached and return the file it wrote.

    Exactly what `setup` does on a bench whose board is still in its box: no
    profile in the workspace, no probe, so the shipped deny-by-default skeleton
    lands with its placeholders."""
    workspace = (tmp_path / "workspace").resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    config_root = Path(os.environ["APPDATA"]) / name
    monkeypatch.setenv("APPDATA", str(config_root))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_root))
    monkeypatch.chdir(workspace)
    monkeypatch.setattr("agentic_hil.tools.discover_attached_hardware", lambda *args, **kwargs: {"ok": False, "error_type": "adapter_not_found", "summary": "No ST-Link probe is attached."})
    started = init_config()
    assert started["ok"] is True, started
    path = Path(started["path"])
    placeholder = document_of(path)
    assert placeholder["debuggers"]["dut"]["probe_id"] is None
    assert placeholder["target"]["controller"] == "unknown-controller"
    return path


def test_the_untouched_skeleton_carries_the_board_with_nobody_editing_yaml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The fourth box on the real output of `init`, with the file untouched.

    Not a rewritten skeleton: the bytes `init_config` wrote, adopted as they are.
    The board's own identity (its serial, its controller, the COM device it
    exposes) lands with nobody typing anything. The backend's executable does
    not, and says why: the skeleton's `type` is `openocd` and the attached
    toolchain is STM32CubeProgrammer, so writing that path into this entry would
    produce a file that loads and fails at the first call. `type` is not a key
    adoption writes, because a probe hands you an identity and never a choice of
    debug stack, so the result names that switch instead of guessing at it. Since
    #343 the switch itself is a `project_config_set` call rather than a hand
    edit, and the `next_step` asserted below is what points at it."""
    path = _skeleton_from_init(tmp_path, monkeypatch, "config-adopt-untouched")
    before = path.read_text(encoding="utf-8")
    attached(monkeypatch)

    carried = adopt_hardware()

    assert carried["ok"] is True, carried
    assert carried["applied"] is True
    assert {item["key"] for item in carried["carried"]} == {
        "debuggers.dut.probe_id",
        "target.controller",
        "com_ports.dut_uart.device",
        "com_ports.dut_uart.serial_number",
    }
    unavailable = {item["key"]: item for item in carried["unavailable"]}
    assert "debuggers.dut.executable" in unavailable
    assert "openocd" in unavailable["debuggers.dut.executable"]["reason"]
    assert "debuggers.dut.type" in unavailable["debuggers.dut.executable"]["next_step"]

    filled = document_of(path)
    assert filled["debuggers"]["dut"]["probe_id"] == PROBE_SERIAL
    assert filled["target"]["controller"] == "stm32f446re"
    assert filled["com_ports"]["dut_uart"]["device"] == "COM9"
    # Untouched means untouched: the only difference between the two files is
    # what adoption itself wrote.
    assert document_of(path)["debuggers"]["dut"]["type"] == yaml.safe_load(before)["debuggers"]["dut"]["type"] == "openocd"

    checked = doctor()
    assert checked["ok"] is True, checked
    assert checked["debuggers"]["dut"]["probe_id"] == PROBE_SERIAL
    assert checked["target"]["controller"] == "stm32f446re"
    assert checked["com_ports"]["dut_uart"]["device"] == "COM9"


def test_setup_without_a_board_then_plugging_it_in_reaches_a_green_doctor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The fourth box end to end, with the one edit that is not a board's identity.

    `init` runs with nothing attached and writes the placeholder skeleton. The
    board arrives. `agentic-hil adopt-hardware` carries it in, as the operator,
    which is the authority `init` itself runs under, so no grant has to be edited
    into a file to make the command that fills the file work. Then `doctor` reads
    a configuration that names the probe.

    One line is edited first, and it is the one line adoption is not allowed to
    write: `debuggers.dut.type` decides which program reaches the board rather
    than what the board is. Everything a probe hands you follows from the
    hardware; see the test above for the same workflow on the untouched file.
    """
    path = _skeleton_from_init(tmp_path, monkeypatch, "config-adopt-end-to-end")

    # The operator plugs the board in. The skeleton's backend is OpenOCD, so the
    # one field that belongs to a backend rather than to a board has to be told
    # which backend this bench is; everything else follows from the hardware.
    switched = document_of(path)
    switched["debuggers"]["dut"]["type"] = "stlink"
    path.write_text(yaml.safe_dump(switched, sort_keys=False), encoding="utf-8")
    attached(monkeypatch)

    carried = adopt_hardware()
    assert carried["ok"] is True, carried
    assert carried["applied"] is True
    assert {item["key"] for item in carried["carried"]} == {
        "debuggers.dut.probe_id",
        "debuggers.dut.executable",
        "target.controller",
        "com_ports.dut_uart.device",
        "com_ports.dut_uart.serial_number",
    }
    filled = document_of(path)
    assert filled["debuggers"]["dut"]["probe_id"] == PROBE_SERIAL
    assert filled["provenance"]["last_modified_by"] == ACTOR_HUMAN
    assert filled["provenance"]["last_modified_via"] == "cli:adopt-hardware"
    # No grant was needed and none moved. The skeleton names every project
    # permission granted, and they are still exactly what it named: this path
    # carries hardware identity and touches no permission in either direction.
    assert filled["permissions"] == dict.fromkeys(GENERATED_PROJECT_PERMISSIONS, True)
    assert {CONFIG_WRITE_RIGHT, CONFIG_DESCRIPTION_RIGHT, CONFIG_PERMISSIONS_RIGHT} <= set(GENERATED_PROJECT_PERMISSIONS)

    checked = doctor()
    assert checked["ok"] is True, checked
    assert checked["debuggers"]["dut"]["probe_id"] == PROBE_SERIAL
    assert checked["target"]["controller"] == "stm32f446re"
    assert checked["com_ports"]["dut_uart"]["device"] == "COM9"
    # Green, and the check really ran: this is the whole of the open generated default in one
    # assertion. The skeleton granted everything, so the board this command just
    # entered is one this bench drives, and nobody opened a YAML file between the
    # empty directory and here. Under the closed default the same run reached a
    # green doctor with `skipped: true` and a hand edit still to come.
    assert checked["debugger"].get("skipped") is not True
    assert checked["debuggers"]["dut"]["check"]["ok"] is True
    assert checked["debuggers"]["dut"]["check"]["backend"] == "stlink"

    # And a permission the operator asks to have taken away is taken away, on
    # the same path, with no editor either.
    tools = service(Path(load_authoritative_config(Path.cwd()).workspace_root))
    try:
        narrowed = tools.call(PROJECT_CONFIG_SET, {"changes": [{"key": "debuggers.dut.permissions.allow_mass_erase", "value": False}]})
    finally:
        tools.close()
    assert narrowed["ok"] is True, narrowed
    assert document_of(path)["debuggers"]["dut"]["permissions"]["allow_mass_erase"] is False


# ---------------------------------------------------------------------------
# What "unset" means, precisely.


def test_unset_is_absent_null_empty_or_the_shipped_placeholder() -> None:
    """The definition, against the skeleton that actually writes the placeholders.

    Read out of `DEFAULT_CONFIG_TEMPLATE` rather than listed, so a template that
    changes its placeholder cannot leave this check recognising the old one."""
    skeleton = yaml.safe_load(DEFAULT_CONFIG_TEMPLATE)
    assert is_unset(None, "target", "controller")
    assert is_unset("", "target", "controller")
    assert is_unset("   ", "target", "controller")
    assert is_unset(skeleton["target"]["controller"], "target", "controller")
    assert is_unset(skeleton["debuggers"]["dut"]["probe_id"], "debuggers", "probe_id")
    # And everything else is a value somebody chose, including one that is wrong.
    assert not is_unset("stm32f446re", "target", "controller")
    assert not is_unset("unknown-controller-2", "target", "controller")
    assert not is_unset(PROBE_SERIAL, "debuggers", "probe_id")
    assert not is_unset("COM4", "com_ports", "device")


def test_a_value_that_already_matches_the_hardware_is_not_written_again(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Running this twice changes nothing the second time.

    An idempotent second call matters because the obvious operator reflex after
    plugging a second board in is to run it again."""
    workspace, path = placeholder_bench(tmp_path, monkeypatch, **{CONFIG_DESCRIPTION_RIGHT: True})
    attached(monkeypatch)
    tools = service(workspace)
    try:
        first = tools.call(PROJECT_CONFIG_ADOPT, {"apply": True})
        assert first["ok"] is True, first
        before = path.read_text(encoding="utf-8")
        second = tools.call(PROJECT_CONFIG_ADOPT, {"apply": True})
    finally:
        tools.close()

    assert second["ok"] is True, second
    assert second["applied"] is False
    assert second["carried"] == []
    assert sorted(item["key"] for item in second["already_current"]) == [
        "com_ports.dut_uart.device",
        "com_ports.dut_uart.serial_number",
        "debuggers.dut.executable",
        "debuggers.dut.probe_id",
        "target.controller",
    ]
    assert path.read_text(encoding="utf-8") == before
    assert document_of(path)["provenance"]["modification_count"] == 1


# ---------------------------------------------------------------------------
# More than one candidate, and none at all.


def test_two_attached_probes_are_named_rather_than_chosen(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Discovery refuses to pick a board, and says how to pick one."""
    workspace, path = placeholder_bench(tmp_path, monkeypatch, **{CONFIG_DESCRIPTION_RIGHT: True})
    attached(
        monkeypatch,
        ok=False,
        error_type="ambiguous_hardware",
        summary="More than one ST-Link probe is attached; discovery will not choose a board.",
        probes=[{"probe_id": PROBE_SERIAL}, {"probe_id": "066AFF495451885087171450"}],
        next_step="Ask which board this is about and name its serial as probe_id; every attached serial is listed under `probes`.",
    )
    tools = service(workspace)
    try:
        refused = tools.call(PROJECT_CONFIG_ADOPT, {"apply": True})
    finally:
        tools.close()

    assert refused["ok"] is False
    assert refused["error_type"] == "ambiguous_hardware"
    assert [entry["probe_id"] for entry in refused["probes"]] == [PROBE_SERIAL, "066AFF495451885087171450"]
    assert "probe_id" in refused["next_step"]
    assert document_of(path)["debuggers"]["dut"]["probe_id"] is None


def test_the_named_probe_reaches_discovery_and_selects_among_what_is_attached(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace, _ = placeholder_bench(tmp_path, monkeypatch, **{CONFIG_DESCRIPTION_RIGHT: True})
    discovery = attached(monkeypatch)
    tools = service(workspace)
    try:
        planned = tools.call(PROJECT_CONFIG_ADOPT, {"probe_id": PROBE_SERIAL})
    finally:
        tools.close()
    assert planned["ok"] is True, planned
    assert discovery["_selected"]["probe_id"] == PROBE_SERIAL


def test_two_configured_debuggers_and_no_name_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The mirror case: several entries in the file, and no way to know which."""
    workspace, path = placeholder_bench(tmp_path, monkeypatch, **{CONFIG_DESCRIPTION_RIGHT: True})
    document = document_of(path)
    # A distinct resource_id, because two entries that name no probe and nothing
    # else to tell them apart are one physical unit twice and refused at load.
    document["debuggers"]["spare"] = {**document["debuggers"]["dut"], "probe_id": None, "resource_id": "spare-board"}
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    attached(monkeypatch)

    tools = service(workspace)
    try:
        refused = tools.call(PROJECT_CONFIG_ADOPT, {"apply": True})
        assert refused["ok"] is False
        assert refused["error_type"] == "invalid_argument"
        assert refused["field"] == "debugger_id"
        assert refused["configured_debuggers"] == ["dut", "spare"]

        chosen = tools.call(PROJECT_CONFIG_ADOPT, {"apply": True, "debugger_id": "spare"})
    finally:
        tools.close()

    assert chosen["ok"] is True, chosen
    after = document_of(path)
    assert after["debuggers"]["spare"]["probe_id"] == PROBE_SERIAL
    assert after["debuggers"]["dut"]["probe_id"] is None


def test_nothing_attached_is_an_answer_and_not_a_written_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace, path = placeholder_bench(tmp_path, monkeypatch, **{CONFIG_DESCRIPTION_RIGHT: True})
    before = path.read_text(encoding="utf-8")
    attached(monkeypatch, ok=False, error_type="adapter_not_found", summary="No ST-Link probe is attached.")
    tools = service(workspace)
    try:
        refused = tools.call(PROJECT_CONFIG_ADOPT, {"apply": True})
    finally:
        tools.close()

    assert refused["ok"] is False
    assert refused["error_type"] == "adapter_not_found"
    assert refused["side_effect_committed"] is False
    assert path.read_text(encoding="utf-8") == before


def test_a_probe_without_its_own_com_port_says_so_instead_of_guessing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No serial-number match means no answer, and the host ports come back.

    A board wired to a separate USB-serial adapter has no port that carries the
    probe's serial. Picking the only host port that happens to be there is how a
    configuration ends up talking to somebody else's device."""
    workspace, path = placeholder_bench(tmp_path, monkeypatch, **{CONFIG_DESCRIPTION_RIGHT: True})
    attached(monkeypatch, com_port=None, available_com_ports={"ok": True, "ports": [{"device": "COM4", "serial_number": "SOMETHINGELSE"}]})
    tools = service(workspace)
    try:
        applied = tools.call(PROJECT_CONFIG_ADOPT, {"apply": True})
    finally:
        tools.close()

    assert applied["ok"] is True, applied
    unavailable = {item["key"]: item for item in applied["unavailable"]}
    assert "com_ports.<name>.device" in unavailable
    assert unavailable["com_ports.<name>.device"]["host_com_ports"] == [{"device": "COM4", "serial_number": "SOMETHINGELSE"}]
    assert document_of(path)["com_ports"] == {}
    # The three keys that could be answered still were.
    assert sorted(item["key"] for item in applied["carried"]) == ["debuggers.dut.executable", "debuggers.dut.probe_id", "target.controller"]


def test_several_com_ports_and_no_name_is_not_resolved_silently(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace, path = placeholder_bench(tmp_path, monkeypatch, **{CONFIG_DESCRIPTION_RIGHT: True})
    document = document_of(path)
    document["com_ports"] = {
        "dut_uart": {"device": "COM3", "baudrate": 115200, "permissions": {"allow_write": False}},
        "bootloader": {"device": "COM4", "baudrate": 115200, "permissions": {"allow_write": False}},
    }
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    attached(monkeypatch)

    tools = service(workspace)
    try:
        applied = tools.call(PROJECT_CONFIG_ADOPT, {"apply": True})
        assert applied["ok"] is True, applied
        assert applied["com_port_id"] is None
        unavailable = {item["key"]: item for item in applied["unavailable"]}
        assert unavailable["com_ports.<name>.device"]["configured_com_ports"] == ["bootloader", "dut_uart"]
        assert document_of(path)["com_ports"]["dut_uart"]["device"] == "COM3"

        # Naming one of them does not make it overwritable: a configured port
        # always has a device, and a device somebody wrote is not a placeholder.
        existing = tools.call(PROJECT_CONFIG_ADOPT, {"apply": True, "com_port_id": "bootloader"})
        assert existing["ok"] is True, existing
        assert {item["key"] for item in existing["kept"]} == {"com_ports.bootloader.device"}
        assert document_of(path)["com_ports"]["bootloader"]["device"] == "COM4"

        # Naming an entry that does not exist yet is how the probe's own port
        # gets one, and the server writes its permissions.
        named = tools.call(PROJECT_CONFIG_ADOPT, {"apply": True, "com_port_id": "probe_uart"})
    finally:
        tools.close()

    assert named["ok"] is True, named
    after = document_of(path)
    assert after["com_ports"]["probe_uart"]["device"] == "COM9"
    assert after["com_ports"]["probe_uart"]["permissions"] == {"allow_write": False}


# ---------------------------------------------------------------------------
# The backend the executable belongs to.


def test_a_toolchain_from_another_backend_is_reported_and_not_written(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An OpenOCD entry does not take an STM32CubeProgrammer path.

    It would load and then fail at the first call, and `type` is deliberately not
    a key adoption writes: a probe hands you an identity, not a choice of debug
    stack. Changing one is `project_config_set`, which the refusal names."""
    workspace, path = placeholder_bench(tmp_path, monkeypatch, **{CONFIG_DESCRIPTION_RIGHT: True})
    document = document_of(path)
    document["debuggers"]["dut"]["type"] = "openocd"
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    attached(monkeypatch)

    tools = service(workspace)
    try:
        applied = tools.call(PROJECT_CONFIG_ADOPT, {"apply": True})
    finally:
        tools.close()

    assert applied["ok"] is True, applied
    unavailable = {item["key"]: item for item in applied["unavailable"]}
    assert "debuggers.dut.executable" in unavailable
    assert "openocd" in unavailable["debuggers.dut.executable"]["reason"]
    after = document_of(path)
    assert after["debuggers"]["dut"]["executable"] is None
    assert after["debuggers"]["dut"]["type"] == "openocd", "adoption never changes which program drives the board"
    # The probe serial is not a backend question and still lands.
    assert after["debuggers"]["dut"]["probe_id"] == PROBE_SERIAL


# ---------------------------------------------------------------------------
# The other toolchain: which GDB reads this bench's images.


def test_the_gdb_this_host_answers_with_is_carried_into_a_bench_that_names_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The key generation leaves null on a machine that had no GDB yet.

    Install the tool, plug the board in, install the toolchain: three moments in
    any order, and the file written in the first of them cannot know the third.
    This is the way back into it, and it is the same way back the probe serial
    takes, so nobody opens the YAML for a path either.

    Unlike the backend's executable it is proposed whatever `type` the entry
    carries: GDB is not a backend's binary, and the stlink entry here resolves
    its symbols with one offline before it reads a byte of target memory."""
    workspace, path = placeholder_bench(tmp_path, monkeypatch, **{CONFIG_DESCRIPTION_RIGHT: True})
    attached(monkeypatch, gdb_executable=FAKE_GDB.as_posix())
    assert document_of(path)["debug"]["gdb_executable"] is None

    tools = service(workspace)
    try:
        planned = tools.call(PROJECT_CONFIG_ADOPT)
        applied = tools.call(PROJECT_CONFIG_ADOPT, {"apply": True})
    finally:
        tools.close()

    carried = {item["key"]: item for item in planned["carried"]}
    assert carried["debug.gdb_executable"]["value"] == FAKE_GDB.as_posix()
    assert carried["debug.gdb_executable"]["previous_value"] is None
    assert applied["ok"] is True, applied
    assert document_of(path)["debug"]["gdb_executable"] == FAKE_GDB.as_posix()
    assert load_authoritative_config(workspace).debug.gdb_executable == str(FAKE_GDB)
    # It is a description key, so this path still names no permission at all.
    assert applied["permissions_changed"] == []


def test_a_gdb_somebody_chose_is_reported_and_never_replaced(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Which GDB reads these images is a choice, and adoption overrules none.

    A bench with two toolchains installed is the ordinary case, and the one this
    project's images need is not always the one that comes first on PATH. So a
    key that already names a GDB comes back under `kept` with both values, the
    same as a controller a person set."""
    workspace, path = placeholder_bench(tmp_path, monkeypatch, **{CONFIG_DESCRIPTION_RIGHT: True})
    document = document_of(path)
    document["debug"]["gdb_executable"] = FAKE_GDB.as_posix()
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    other = tmp_path / "other-toolchain" / "gdb-multiarch"
    other.parent.mkdir(parents=True, exist_ok=True)
    other.write_text("#!/bin/sh\n", encoding="utf-8")
    attached(monkeypatch, gdb_executable=other.as_posix())

    tools = service(workspace)
    try:
        applied = tools.call(PROJECT_CONFIG_ADOPT, {"apply": True})
    finally:
        tools.close()

    assert applied["ok"] is True, applied
    kept = {item["key"]: item for item in applied["kept"]}
    assert kept["debug.gdb_executable"]["configured_value"] == FAKE_GDB.as_posix()
    assert kept["debug.gdb_executable"]["discovered_value"] == other.as_posix()
    assert [item["key"] for item in applied["carried"] if item["key"] == "debug.gdb_executable"] == []
    assert document_of(path)["debug"]["gdb_executable"] == FAKE_GDB.as_posix()


def test_no_gdb_on_this_host_is_nothing_to_carry_rather_than_a_null_written(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A host with no GDB installed says nothing about the key.

    Discovery reports what it found, and it found none, so the plan is silent
    about it in all four lists: the bench goes on autodetecting at runtime, which
    is what a null already means, and adoption has not turned "not installed"
    into a claim about the file.

    True before this key was adoptable and true after it, deliberately: it is the
    behaviour the new proposal must not have changed, so it is the one test here
    that passes on both sides."""
    workspace, path = placeholder_bench(tmp_path, monkeypatch, **{CONFIG_DESCRIPTION_RIGHT: True})
    attached(monkeypatch, gdb_executable=None)

    tools = service(workspace)
    try:
        applied = tools.call(PROJECT_CONFIG_ADOPT, {"apply": True})
    finally:
        tools.close()

    assert applied["ok"] is True, applied
    reported = [item["key"] for group in ("carried", "already_current", "kept", "unavailable") for item in applied[group]]
    assert "debug.gdb_executable" not in reported
    assert document_of(path)["debug"]["gdb_executable"] is None


def test_the_gdb_the_file_already_names_is_current_rather_than_kept(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Running it twice reports agreement, not a disagreement with itself.

    The second run finds the value the first one wrote, which is the same value
    this host answers with, so it belongs under `already_current` and there is
    nothing to write. A key that came back under `kept` here would have an
    operator looking for a conflict that does not exist."""
    workspace, path = placeholder_bench(tmp_path, monkeypatch, **{CONFIG_DESCRIPTION_RIGHT: True})
    attached(monkeypatch, gdb_executable=FAKE_GDB.as_posix())

    tools = service(workspace)
    try:
        first = tools.call(PROJECT_CONFIG_ADOPT, {"apply": True})
        second = tools.call(PROJECT_CONFIG_ADOPT, {"apply": True})
    finally:
        tools.close()

    assert first["ok"] is True and second["ok"] is True
    assert "debug.gdb_executable" in {item["key"] for item in first["carried"]}
    assert "debug.gdb_executable" in {item["key"] for item in second["already_current"]}
    assert "debug.gdb_executable" not in {item["key"] for item in second["carried"]}


# ---------------------------------------------------------------------------
# The grants, and the one door.


def test_without_the_description_grant_the_agent_gets_the_values_and_not_the_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A refusal that still removes the transcription.

    The grant belongs to the operator and this call does not argue with it. What
    it does is hand back the exact keys and values rather than a probe listing
    somebody has to correlate by hand, which is the whole complaint,
    answered even where the write is shut."""
    workspace, path = placeholder_bench(tmp_path, monkeypatch)
    before = path.read_text(encoding="utf-8")
    attached(monkeypatch)
    tools = service(workspace)
    try:
        refused = tools.call(PROJECT_CONFIG_ADOPT, {"apply": True})
    finally:
        tools.close()

    assert refused["ok"] is False
    assert refused["error_type"] == "permission_denied"
    assert refused["permission"] == CONFIG_DESCRIPTION_RIGHT
    assert refused["applied"] is False
    assert {item["key"]: item["value"] for item in refused["carried"]}["debuggers.dut.probe_id"] == PROBE_SERIAL
    assert refused["write"]["tool"] == PROJECT_CONFIG_SET
    assert path.read_text(encoding="utf-8") == before


def test_nothing_is_read_or_written_while_this_server_holds_hardware(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Refused one step earlier than a plain write, because this touches the board.

    Enumerating probes and connecting in HOTPLUG mode talks to hardware a run may
    be holding, so the refusal comes before discovery rather than before the
    write."""
    workspace, path = placeholder_bench(tmp_path, monkeypatch, **{CONFIG_DESCRIPTION_RIGHT: True})
    before = path.read_text(encoding="utf-8")
    reached: dict[str, bool] = {}

    def discover(timeout_s: float = 10.0, *, probe_id: str | None = None, before_connect: Any = None, profile: Any = None) -> dict:  # pragma: no cover - must not run
        reached["discovery"] = True
        return {"ok": True}

    monkeypatch.setattr("agentic_hil.adopt.discover_attached_hardware", discover)
    tools = service(workspace)
    try:
        started = tools.call("bench_run_start", {"devices": [{"kind": "debugger", "id": "dut"}], "label": "held"})
        assert started["ok"] is True, started
        refused = tools.call(PROJECT_CONFIG_ADOPT, {"apply": True})
        assert tools.call("bench_run_stop")["ok"] is True
    finally:
        tools.close()

    assert refused["ok"] is False
    assert refused["error_type"] == "config_write_in_open_run"
    assert refused["retry_safe"] is True
    assert "discovery" not in reached
    assert path.read_text(encoding="utf-8") == before


# ---------------------------------------------------------------------------
# The board this reads belongs to whoever holds it.
#
# Enumerating probes and connecting in HOTPLUG mode is a hardware read, and a
# read still perturbs: an SWD attach halts the core. What answers that in this
# repository is exclusivity rather than a grant, so this path has to take the
# same machine-wide locks, prove the same audit trail and quarantine the same
# way as every other read of a board. An MCP server that only checked its own
# open holds would be checking the one process that cannot be the problem.


def test_the_attached_probe_is_not_connected_to_while_another_owner_holds_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A second process on this machine gets `device_busy`, not the board.

    The holder here is a stranger: another MCP server, another
    `agentic-hil adopt-hardware`, a test reactor run in a different project. It
    holds the physical probe, which is exactly the lock this path takes before it
    says anything to it."""
    workspace, path = placeholder_bench(tmp_path, monkeypatch, **{CONFIG_DESCRIPTION_RIGHT: True})
    before = path.read_text(encoding="utf-8")
    discovery = attached(monkeypatch)

    stranger = BenchMutex(frontend="stranger", label="other-bench-session")
    stranger.acquire([f"probe:{fold_hardware_id(PROBE_SERIAL)}"])
    tools = service(workspace)
    try:
        refused = tools.call(PROJECT_CONFIG_ADOPT, {"apply": True})
    finally:
        tools.close()
        stranger.release_all()

    assert refused["ok"] is False
    assert refused["error_type"] == "device_busy"
    assert refused["holder"]["label"] == "other-bench-session"
    assert refused["side_effect_committed"] is False
    assert "connected_probe_id" not in discovery["_selected"], "the HOTPLUG connect must not have run"
    assert path.read_text(encoding="utf-8") == before


def test_the_operator_command_is_refused_the_same_way(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A person's authority over their own file is not authority over a bench.

    The CLI waives the description grant, and that is the only thing it waives.
    It brings its own coordinator, so a board another owner is driving is as
    unreachable from a terminal as it is over MCP."""
    workspace, path = placeholder_bench(tmp_path, monkeypatch)
    before = path.read_text(encoding="utf-8")
    discovery = attached(monkeypatch)

    stranger = BenchMutex(frontend="stranger", label="mcp-server")
    stranger.acquire([f"probe:{fold_hardware_id(PROBE_SERIAL)}"])
    try:
        refused = adopt_hardware()
    finally:
        stranger.release_all()

    assert refused["ok"] is False
    assert refused["error_type"] == "device_busy"
    assert refused["holder"]["label"] == "mcp-server"
    assert "connected_probe_id" not in discovery["_selected"]
    assert path.read_text(encoding="utf-8") == before


def test_the_probe_is_given_back_when_the_read_is_over(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A one-shot read holds for the call and not a moment longer.

    A lock this path forgot to release would be worse than one it never took: the
    board would stay unreachable for the rest of the process, with nothing saying
    why."""
    workspace, _ = placeholder_bench(tmp_path, monkeypatch, **{CONFIG_DESCRIPTION_RIGHT: True})
    attached(monkeypatch)
    tools = service(workspace)
    try:
        applied = tools.call(PROJECT_CONFIG_ADOPT, {"apply": True})
        assert applied["ok"] is True, applied
        assert tools.coordinator.leases == {}
    finally:
        tools.close()

    outsider = BenchMutex(frontend="stranger")
    try:
        assert outsider.acquire([f"probe:{fold_hardware_id(PROBE_SERIAL)}"])
    finally:
        outsider.release_all()


def test_the_hardware_read_is_written_into_the_audit_trail(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """What was read off which probe is recorded, like every other board read."""
    workspace, _ = placeholder_bench(tmp_path, monkeypatch, **{CONFIG_DESCRIPTION_RIGHT: True})
    attached(monkeypatch)
    tools = service(workspace)
    try:
        applied = tools.call(PROJECT_CONFIG_ADOPT, {"apply": True})
        assert applied["ok"] is True, applied
        recorded = tools.call("get_last_report")
    finally:
        tools.close()

    report = recorded["report"]
    assert report["tool"] == PROJECT_CONFIG_ADOPT
    assert report["probe_id"] == PROBE_SERIAL
    # Both locks the read took: the host-wide enumeration and the probe itself.
    assert report["resources"] == ["debugger-discovery:all", f"probe:{fold_hardware_id(PROBE_SERIAL)}"]
    # The record is written while the board is still held, then re-committed once
    # the leases reach their terminal state, so the persisted evidence and the
    # returned result never disagree about whether anything is still held.
    assert report["lease_state"] == "released"
    assert report["cleanup_required"] is False
    assert report["quarantined"] is False


def test_nothing_is_read_when_the_audit_trail_cannot_be_written(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A bench that cannot record what happened does not have it happen.

    Checked on the CLI path, which has no tool service in front of it to make the
    same check first."""
    workspace, path = placeholder_bench(tmp_path, monkeypatch)
    before = path.read_text(encoding="utf-8")
    discovery = attached(monkeypatch)

    def broken(config: Any) -> None:
        raise OSError("reports directory is read-only")

    monkeypatch.setattr("agentic_hil.adopt.ensure_audit_ready", broken)

    refused = adopt_hardware()

    assert refused["ok"] is False
    assert refused["error_type"] == "audit_unavailable"
    assert refused["audit_ok"] is False
    assert discovery["_selected"] == {}, "nothing was enumerated either"
    assert path.read_text(encoding="utf-8") == before


def test_a_read_that_raises_quarantines_the_probe_and_shuts_the_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail closed: what reached the board is unknown, so nobody gets it back.

    A discovery that raised used to leave the lock untaken and the next call free
    to connect to a probe whose state nothing had confirmed."""
    workspace, path = placeholder_bench(tmp_path, monkeypatch, **{CONFIG_DESCRIPTION_RIGHT: True})
    before = path.read_text(encoding="utf-8")

    def exploding(timeout_s: float = 10.0, *, probe_id: str | None = None, before_connect: Any = None, profile: Any = None) -> dict:
        if before_connect is not None:
            before_connect(PROBE_SERIAL)
        raise RuntimeError("STM32_Programmer_CLI died mid-connect")

    monkeypatch.setattr("agentic_hil.adopt.discover_attached_hardware", exploding)
    tools = service(workspace)
    try:
        failed = tools.call(PROJECT_CONFIG_ADOPT, {"apply": True})
        assert failed["ok"] is False
        assert failed["cleanup_required"] is True
        # Contained while the call ran and named on the way out. The bench is
        # not shut afterwards, because a probe read that raised leaves a target
        # the next reset and probe speak for, and the retry below is that next
        # contact rather than a walk back onto a board nobody vouched for.
        assert failed["quarantined"] is False
        assert failed["incident_stood_down"]["reasons"] == ["adopt_discovery_exception", "unknown_hardware_exception"]
        attached(monkeypatch)
        retried = tools.call(PROJECT_CONFIG_ADOPT, {"apply": True})
    finally:
        tools.close()

    assert retried["ok"] is True, retried
    assert path.read_text(encoding="utf-8") != before


def _probe_release_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only the probe lease fails to come back; the enumeration lease is clean.

    The asymmetric case on purpose: it is what a partial release actually looks
    like, and what an aggregate that reported the *first* non-active state got
    wrong."""
    original = HardwareLease.release

    def release(self: HardwareLease, **kwargs: Any) -> bool:
        if any(str(resource).startswith("probe:") for resource in self.resources):
            self.state = "cleanup_required"
            self.errors.append({"reason": "adopt_release_failed"})
            self.quarantine_id = "quarantine-test-0001"
            return False
        return bool(original(self, **kwargs))

    monkeypatch.setattr(HardwareLease, "release", release)


def test_a_release_that_fails_is_recorded_as_the_terminal_outcome(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The audit trail says what the answer says.

    The read is written as an `ok: true`, `lease_state: active` report before the
    release is tried, which is right. Stopping there was not: the release then
    failed, the call returned `resource_quarantined`, and nothing rewrote the
    record, so `get_last_report` still described a clean active discovery and
    `classify_last_error` had no failure at all. Both places an operator looks
    after a refusal said the bench was fine."""
    workspace, path = placeholder_bench(tmp_path, monkeypatch, **{CONFIG_DESCRIPTION_RIGHT: True})
    before = path.read_text(encoding="utf-8")
    attached(monkeypatch)
    _probe_release_fails(monkeypatch)

    tools = service(workspace)
    try:
        refused = tools.call(PROJECT_CONFIG_ADOPT, {"apply": True})
        recorded = tools.call("get_last_report")
        failure = tools.call("classify_last_error")
    finally:
        tools.close()

    assert refused["ok"] is False
    assert refused["error_type"] == "resource_quarantined"
    assert path.read_text(encoding="utf-8") == before, "nothing is written under a lease this process still holds"

    report = recorded["report"]
    assert report["ok"] is False
    assert report["error_type"] == "resource_quarantined"
    assert report["cleanup_required"] is True
    assert failure["error_type"] == "resource_quarantined", "the refusal reaches classify_last_error too"
    assert failure["source_tool"] == PROJECT_CONFIG_ADOPT


def test_a_discovery_that_never_said_what_it_committed_is_not_reported_as_committing_nothing() -> None:
    """agentic-hil/agentic-hil#141: absence is unknown, and was being rendered as no.

    `bool(discovery.get("side_effect_committed", False))` gave a discovery that
    set no marker the positive claim that nothing was committed, with
    `side_effect_status: not_started` beside it from the same absence, the one
    reading that tells an operator a quarantined bench is untouched. Every
    discovery result carries the marker today, which is what makes this worth
    removing rather than relying on: the refusal is what an operator reads after
    a release failed, and it must not invent the reassuring half of an answer
    nobody gave.
    """
    refusal = _release_refusal({"probe_id": PROBE_SERIAL}, {"lease_state": "quarantined"}, PROJECT_CONFIG_ADOPT)

    assert "side_effect_committed" not in refusal
    assert refusal["side_effect_status"] == "unknown"


@pytest.mark.parametrize(
    ("discovery", "expected"),
    [
        ({"side_effect_committed": False}, {"side_effect_committed": False, "side_effect_status": "not_started"}),
        ({"side_effect_committed": True}, {"side_effect_committed": True, "side_effect_status": "partial"}),
        (
            {"side_effect_committed": True, "side_effect_status": "committed"},
            {"side_effect_committed": True, "side_effect_status": "committed"},
        ),
    ],
    ids=["stated-no", "stated-yes", "stated-both"],
)
def test_a_discovery_that_did_say_keeps_what_it_said(discovery: dict[str, Any], expected: dict[str, Any]) -> None:
    """The fix only touches the silent case; a stated marker is carried unchanged."""
    refusal = _release_refusal(discovery, {"lease_state": "quarantined"}, PROJECT_CONFIG_ADOPT)

    assert {key: refusal[key] for key in expected} == expected


def test_a_partial_release_never_reports_the_aggregate_as_released(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """One lease back, one not, is not "released".

    The aggregate used to take the first non-`active` state it found. With the
    enumeration lease released and the probe lease in `cleanup_required` that was
    `lease_state: released` beside `cleanup_required: true`, and callers are told
    to treat anything other than null/active/released as blocking, so the one
    field they key on said the opposite of what was true about the board. The
    incident id went missing with it, leaving the refusal naming a `recover` run
    the operator could not address."""
    workspace, _ = placeholder_bench(tmp_path, monkeypatch, **{CONFIG_DESCRIPTION_RIGHT: True})
    attached(monkeypatch)
    _probe_release_fails(monkeypatch)

    tools = service(workspace)
    try:
        refused = tools.call(PROJECT_CONFIG_ADOPT, {"apply": True})
    finally:
        tools.close()

    assert refused["lease_state"] == "cleanup_required"
    assert refused["cleanup_required"] is True
    assert refused["quarantine_id"] == "quarantine-test-0001"
    # Both leases are named, in the state each of them actually reached, so a
    # partial release reads as the partial thing it is.
    by_state = {entry["lease_state"] for entry in refused["leases"]}
    assert by_state == {"released", "cleanup_required"}


# ---------------------------------------------------------------------------
# A configuration that closed the read.


def test_a_version_one_configuration_that_denies_probing_is_not_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`allow_probe: false` means no probe read, and this is a probe read.

    From version 2 on there is no read permission and exclusivity answers
    instead. A file written before that still says whether its probe may be read
    at all, and `probe_target` and `debugger_probes_list` are both refused on
    one. Being able to fill in a serial is not being able to widen that."""
    workspace, path = placeholder_bench(tmp_path, monkeypatch, config_version=None, permissions={"allow_probe": False}, **{CONFIG_DESCRIPTION_RIGHT: True})
    before = path.read_text(encoding="utf-8")
    discovery = attached(monkeypatch)
    tools = service(workspace)
    try:
        refused = tools.call(PROJECT_CONFIG_ADOPT)
        granted_probe = tools.call("debugger_probes_list")
    finally:
        tools.close()

    assert refused["ok"] is False
    assert refused["error_type"] == "permission_denied"
    assert refused["permission"] == "allow_probe"
    assert refused["permission_key"] == "debuggers.dut.permissions.allow_probe"
    # The same answer the ordinary probe read gives on this file, which is the
    # whole point: one policy, not two.
    assert granted_probe["error_type"] == "permission_denied"
    assert discovery["_selected"] == {}
    assert path.read_text(encoding="utf-8") == before


def test_a_version_one_configuration_that_grants_probing_is_carried_in(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The grant is consulted, not required: an open version 1 file works."""
    workspace, path = placeholder_bench(tmp_path, monkeypatch, config_version=None, permissions={"allow_probe": True}, **{CONFIG_DESCRIPTION_RIGHT: True})
    attached(monkeypatch)
    # A placeholder names no executable, so granting hardware access makes the
    # startup resolve one off PATH. Without this the test asserts that a
    # debugger happens to be installed on the machine running it: it passed on a
    # bench that has OpenOCD and failed on every CI runner that does not.
    monkeypatch.setattr(shutil, "which", lambda _name: str(FAKE_STLINK))
    tools = service(workspace)
    try:
        applied = tools.call(PROJECT_CONFIG_ADOPT, {"apply": True})
    finally:
        tools.close()

    assert applied["ok"] is True, applied
    assert document_of(path)["debuggers"]["dut"]["probe_id"] == PROBE_SERIAL


# ---------------------------------------------------------------------------
# The window between deciding and writing.


def test_a_key_filled_while_the_probe_was_being_read_is_not_overwritten(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The fill-only rule has to survive the seconds a probe read takes.

    The document is classified, then the board is read (seconds, not
    microseconds) and only then is the write lock taken. Another CLI or MCP
    process can fill a placeholder inside that window, and a plan that carried
    only `{key, value}` would overwrite the newer choice with the older decision.
    The plan carries what it saw, and a key that has moved refuses the call."""
    workspace, path = placeholder_bench(tmp_path, monkeypatch, **{CONFIG_DESCRIPTION_RIGHT: True})
    discovery = attached(monkeypatch)
    real = project_config_adopt_hardware.__globals__["discover_attached_hardware"]

    def slow(timeout_s: float = 10.0, *, probe_id: str | None = None, before_connect: Any = None, profile: Any = None) -> dict:
        # Somebody else writes this file while the board is being read.
        meanwhile = document_of(path)
        meanwhile["target"]["controller"] = "stm32f411re"
        path.write_text(yaml.safe_dump(meanwhile, sort_keys=False), encoding="utf-8")
        return real(timeout_s, probe_id=probe_id, before_connect=before_connect)

    monkeypatch.setattr("agentic_hil.adopt.discover_attached_hardware", slow)
    tools = service(workspace)
    try:
        refused = tools.call(PROJECT_CONFIG_ADOPT, {"apply": True})
    finally:
        tools.close()

    assert refused["ok"] is False
    assert refused["error_type"] == "config_changed_underneath"
    assert refused["retry_safe"] is True
    assert [item["key"] for item in refused["stale_keys"]] == ["target.controller"]
    assert refused["stale_keys"][0]["current_value"] == "stm32f411re"
    after = document_of(path)
    assert after["target"]["controller"] == "stm32f411re", "the newer choice survives"
    assert after["debuggers"]["dut"]["probe_id"] is None, "and the call wrote nothing at all"
    assert discovery["_selected"]["connected_probe_id"] == PROBE_SERIAL


def test_an_entry_named_like_a_reserved_key_is_still_covered_by_the_document_check(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The compare-and-swap has to see entries whatever the operator called them.

    `permissions`, `provenance` and `allow_anything` are legal debugger, COM and
    CAN entry ids. The document comparison used to drop every mapping key with
    one of those names at any depth, so an entry called `permissions` was outside
    it, and outside the per-key expectations too, which cover only the keys this
    call sets. A concurrent repoint of that entry from one board to another was
    therefore invisible, and the first board's discovery could still be committed
    onto it. Whether the id is odd is the operator's business; whether the
    document moved is not."""
    workspace, path = placeholder_bench(tmp_path, monkeypatch, **{CONFIG_DESCRIPTION_RIGHT: True})
    document = document_of(path)
    document["debuggers"] = {"permissions": {**document["debuggers"]["dut"], "probe_id": None}}
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    attached(monkeypatch)
    real = project_config_adopt_hardware.__globals__["discover_attached_hardware"]

    def slow(timeout_s: float = 10.0, *, probe_id: str | None = None, before_connect: Any = None, profile: Any = None) -> dict:
        # The entry is switched to another backend while this board is being
        # read. `type` is not a key adoption carries, so no per-key expectation
        # covers it, and the whole-document comparison is the only thing that can.
        meanwhile = document_of(path)
        meanwhile["debuggers"]["permissions"]["type"] = "openocd"
        path.write_text(yaml.safe_dump(meanwhile, sort_keys=False), encoding="utf-8")
        return real(timeout_s, probe_id=probe_id, before_connect=before_connect)

    monkeypatch.setattr("agentic_hil.adopt.discover_attached_hardware", slow)
    tools = service(workspace)
    try:
        refused = tools.call(PROJECT_CONFIG_ADOPT, {"apply": True, "debugger_id": "permissions"})
    finally:
        tools.close()

    assert refused["ok"] is False
    assert refused["error_type"] == "config_changed_underneath"
    after = document_of(path)
    assert after["debuggers"]["permissions"]["type"] == "openocd", "the newer choice survives"
    assert after["debuggers"]["permissions"]["executable"] is None, "and no STM32CubeProgrammer path was written into an OpenOCD entry"
    assert after["debuggers"]["permissions"]["probe_id"] is None, "the call wrote nothing at all"


def test_an_entry_named_like_a_reserved_key_can_still_be_adopted_into(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Being covered by the comparison is only half of being usable.

    The test above proves a concurrent edit to `debuggers.permissions` is seen.
    This is the other half: with nothing but `allow_config_description_write`, the
    entry can actually be filled in. The permission surface read every key under
    such an entry as a grant: `debuggers.permissions.probe_id` came back as a
    moved permission, so adoption refused with `permission_denied` naming
    `allow_config_permissions_write`, and the operator's only way out was to
    rename their board."""
    workspace, path = placeholder_bench(tmp_path, monkeypatch, **{CONFIG_DESCRIPTION_RIGHT: True})
    document = document_of(path)
    document["debuggers"] = {"permissions": document["debuggers"]["dut"]}
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    attached(monkeypatch)
    tools = service(workspace)
    try:
        applied = tools.call(PROJECT_CONFIG_ADOPT, {"apply": True, "debugger_id": "permissions"})
    finally:
        tools.close()

    assert applied["ok"] is True, applied
    after = document_of(path)
    assert after["debuggers"]["permissions"]["probe_id"] == PROBE_SERIAL
    assert after["debuggers"]["permissions"]["permissions"] == document["debuggers"]["permissions"]["permissions"], "no grant moved"


def test_the_plan_is_a_pure_reading_of_a_document(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No document, no plan: with nothing configured there is no entry to fill.

    The planner is the part a reviewer has to be able to reason about on its own,
    so it takes a document and a discovery result and returns a decision: no
    file, no permissions, no way to reach either."""
    plan = plan_adoption({"debuggers": {}}, {"ok": True, "backend": "stlink", "probe_id": PROBE_SERIAL})
    assert plan["ok"] is False
    assert plan["field"] == "debugger_id"


def test_an_unknown_debugger_name_is_refused_rather_than_created(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A probe entry is not something this brings into existence.

    A `debuggers` entry decides which backend drives a board and with which
    scripts; created from a name in a call it would be an OpenOCD entry nobody
    asked for. Filling one in is this path's job, adding one is not."""
    workspace, path = placeholder_bench(tmp_path, monkeypatch, **{CONFIG_DESCRIPTION_RIGHT: True})
    attached(monkeypatch)
    tools = service(workspace)
    try:
        refused = tools.call(PROJECT_CONFIG_ADOPT, {"apply": True, "debugger_id": "second_board"})
    finally:
        tools.close()

    assert refused["ok"] is False
    assert refused["error_type"] == "unknown_device"
    assert refused["configured_debuggers"] == ["dut"]
    assert "second_board" not in document_of(path)["debuggers"]


def test_the_operator_path_still_cannot_move_a_permission(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The CLI waives the description grant and nothing else.

    The before/after comparison inside the write path reads the document rather
    than the request, and it is not conditional on who is asking. This checks it
    from the outside: an operator run leaves every permission exactly as it was,
    including in the entry it creates."""
    workspace, path = placeholder_bench(tmp_path, monkeypatch)
    attached(monkeypatch)
    before = granted(document_of(path))

    result = adopt_hardware()

    assert result["ok"] is True, result
    after = granted(document_of(path))
    assert not [key for key in before if before[key] != after[key]]
    assert after["com_ports.dut_uart.permissions.allow_write"] is False
    assert after[f"permissions.{CONFIG_DESCRIPTION_RIGHT}"] is False
    assert after[f"permissions.{CONFIG_PERMISSIONS_RIGHT}"] is False


def test_the_cli_dry_run_writes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace, path = placeholder_bench(tmp_path, monkeypatch)
    attached(monkeypatch)
    before = path.read_text(encoding="utf-8")

    result = adopt_hardware(dry_run=True)

    assert result["ok"] is True, result
    assert result["applied"] is False
    assert len(result["carried"]) == 5
    assert path.read_text(encoding="utf-8") == before


def test_the_write_goes_through_the_one_door(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every change this path makes is a project_config_set change.

    Not a stylistic point: the grants, the schema check on each value, the
    validate-before-replace and the provenance record all live there, and a
    second way to write this file would have to reimplement every one of them."""
    workspace, _ = placeholder_bench(tmp_path, monkeypatch, **{CONFIG_DESCRIPTION_RIGHT: True})
    attached(monkeypatch)
    seen: list[dict] = []
    real = project_config_adopt_hardware.__globals__["project_config_set"]

    def recording(*args: Any, **kwargs: Any) -> dict:
        seen.append({"changes": args[2], "actor": kwargs.get("actor"), "via": kwargs.get("via")})
        return real(*args, **kwargs)

    monkeypatch.setattr("agentic_hil.adopt.project_config_set", recording)
    tools = service(workspace)
    try:
        applied = tools.call(PROJECT_CONFIG_ADOPT, {"apply": True})
    finally:
        tools.close()

    assert applied["ok"] is True, applied
    assert len(seen) == 1
    assert seen[0]["actor"] == "agent"
    assert seen[0]["via"] == f"mcp:{PROJECT_CONFIG_ADOPT}"
    assert all(sorted(change) == ["key", "value"] for change in seen[0]["changes"])


# ---------------------------------------------------------------------------
# The plan was decided against a document, and the write is that document's.


def test_a_probe_id_that_moves_during_the_read_refuses_the_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The mixed-board file this module refuses, arriving by the back door.

    `probe_id` is read before discovery and, when it is already set, decides
    which physical probe is read at all. It is then not one of the keys the write
    carries, so a per-key expectation cannot cover it: somebody repointing the
    entry from A to B while A was being read would have A's controller, A's
    toolchain and A's COM device committed into an entry that now names B. That
    is a configuration describing two boards, which is exactly what
    `_different_board` exists to prevent when the disagreement is visible.
    """
    workspace, path = placeholder_bench(tmp_path, monkeypatch, **{CONFIG_DESCRIPTION_RIGHT: True})
    attached(monkeypatch)
    real = project_config_adopt_hardware.__globals__["project_config_set"]

    def repoint_then_write(*args: Any, **kwargs: Any) -> dict:
        # Another writer, inside the window where the probe was being read.
        document = document_of(path)
        document["debuggers"]["dut"]["probe_id"] = "0011002B3038510934333935"
        path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
        return real(*args, **kwargs)

    monkeypatch.setattr("agentic_hil.adopt.project_config_set", repoint_then_write)
    tools = service(workspace)
    try:
        refused = tools.call(PROJECT_CONFIG_ADOPT, {"apply": True})
    finally:
        tools.close()

    assert refused["ok"] is False, refused
    assert refused["error_type"] == "config_changed_underneath"
    assert refused["retry_safe"] is True
    after = document_of(path)
    assert after["debuggers"]["dut"]["probe_id"] == "0011002B3038510934333935"
    assert after["debuggers"]["dut"]["executable"] is None, "nothing of the read board reached the entry"
    assert after["target"]["controller"] == "unknown-controller"


def test_a_backend_change_during_the_read_refuses_the_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`type` decided the plan and is outside every write door.

    The executable is carried only because the entry's `type` matches the backend
    discovery ran on. `type` is not a settable key, so the only way to notice it
    moved is to compare the document the plan was made against."""
    workspace, path = placeholder_bench(tmp_path, monkeypatch, **{CONFIG_DESCRIPTION_RIGHT: True})
    attached(monkeypatch)
    real = project_config_adopt_hardware.__globals__["project_config_set"]

    def retype_then_write(*args: Any, **kwargs: Any) -> dict:
        document = document_of(path)
        document["debuggers"]["dut"]["type"] = "pyocd"
        path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
        return real(*args, **kwargs)

    monkeypatch.setattr("agentic_hil.adopt.project_config_set", retype_then_write)
    tools = service(workspace)
    try:
        refused = tools.call(PROJECT_CONFIG_ADOPT, {"apply": True})
    finally:
        tools.close()

    assert refused["ok"] is False, refused
    assert refused["error_type"] == "config_changed_underneath"
    assert refused["document_changed"] is True
    assert document_of(path)["debuggers"]["dut"]["executable"] is None


def test_a_permission_changed_during_the_read_does_not_refuse_the_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The document check is on the description, and for a reason.

    Nothing this path plans reads a permission (it cannot name one), so a
    concurrent grant must not turn a correct plan into a refusal. Comparing the
    whole document would make every unrelated write by another process a reason
    to throw a hardware read away."""
    workspace, path = placeholder_bench(tmp_path, monkeypatch, **{CONFIG_DESCRIPTION_RIGHT: True})
    attached(monkeypatch)
    real = project_config_adopt_hardware.__globals__["project_config_set"]

    def grant_then_write(*args: Any, **kwargs: Any) -> dict:
        document = document_of(path)
        document["debuggers"]["dut"]["permissions"]["allow_reset"] = True
        path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
        return real(*args, **kwargs)

    monkeypatch.setattr("agentic_hil.adopt.project_config_set", grant_then_write)
    tools = service(workspace)
    try:
        applied = tools.call(PROJECT_CONFIG_ADOPT, {"apply": True})
    finally:
        tools.close()

    assert applied["ok"] is True, applied
    after = document_of(path)
    assert after["debuggers"]["dut"]["probe_id"] == PROBE_SERIAL
    assert after["debuggers"]["dut"]["permissions"]["allow_reset"] is True, "and the other writer's grant survived"


# ---------------------------------------------------------------------------
# A debugger that carries its own target.


def test_a_per_debugger_target_is_reported_rather_than_written_to_the_wrong_one(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`target.controller` at the top level is a different board's setting here.

    An entry with its own `target:` block overrides the project target for
    everything that runs on that probe, so writing the detected controller into
    the top-level block would not reach the board that was read: it would change
    the fallback the other entries use. The closed key model has no
    `debuggers.<name>.target.*` key, so the honest answer is to say where the
    value belongs and leave both targets alone."""
    workspace, path = placeholder_bench(tmp_path, monkeypatch, **{CONFIG_DESCRIPTION_RIGHT: True})
    document = document_of(path)
    document["debuggers"]["dut"]["target"] = {"name": "own-target", "controller": "unknown-controller"}
    document["debuggers"]["spare"] = {"type": "stlink", "executable": FAKE_STLINK.as_posix(), "permissions": {}}
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    attached(monkeypatch)

    tools = service(workspace)
    try:
        applied = tools.call(PROJECT_CONFIG_ADOPT, {"apply": True, "debugger_id": "dut"})
    finally:
        tools.close()

    assert applied["ok"] is True, applied
    assert not [item for item in applied["carried"] if item["key"] == "target.controller"]
    reported = [item for item in applied["unavailable"] if item["key"] == "debuggers.dut.target.controller"]
    assert len(reported) == 1, applied["unavailable"]
    assert reported[0]["discovered_value"] == "stm32f446re"
    assert "debuggers.dut.target" in reported[0]["next_step"]
    after = document_of(path)
    # Neither target moved, and the identity of the read board still landed.
    assert after["target"]["controller"] == "unknown-controller"
    assert after["debuggers"]["dut"]["target"]["controller"] == "unknown-controller"
    assert after["debuggers"]["dut"]["probe_id"] == PROBE_SERIAL


def _bench_with_own_target(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, controller: str) -> tuple[Path, Path]:
    workspace, path = placeholder_bench(tmp_path, monkeypatch, **{CONFIG_DESCRIPTION_RIGHT: True})
    document = document_of(path)
    document["debuggers"]["dut"]["target"] = {"name": "own-target", "controller": controller}
    document["debuggers"]["spare"] = {"type": "stlink", "executable": FAKE_STLINK.as_posix(), "permissions": {}}
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    attached(monkeypatch)
    return workspace, path


def test_a_per_debugger_target_that_already_agrees_is_reported_as_current(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing to place is not the same as nowhere to place it.

    Where the value cannot go is a fact about the key model; whether the value
    differs at all is a fact about the document, and the two were conflated. An
    override already naming the detected controller has nothing to do, so it
    belongs under `already_current`: telling the operator to go and set a value
    the file already holds is advice to make no change."""
    workspace, _ = _bench_with_own_target(tmp_path, monkeypatch, "stm32f446re")

    tools = service(workspace)
    try:
        applied = tools.call(PROJECT_CONFIG_ADOPT, {"apply": True, "debugger_id": "dut"})
    finally:
        tools.close()

    assert applied["ok"] is True, applied
    assert {"key": "debuggers.dut.target.controller", "value": "stm32f446re"} in applied["already_current"]
    assert not [item for item in applied["unavailable"] if item["key"] == "debuggers.dut.target.controller"]


def test_a_per_debugger_target_that_disagrees_is_kept_not_recommended_away(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A value somebody chose, reported as a disagreement and left alone.

    This is the same rule every other key follows, and the override was the one
    place it did not: a deliberate conflicting controller was answered with "set
    the discovered value", which is adoption recommending exactly the overwrite
    it refuses to perform. A deliberate override and a stale one look identical
    from here, so the answer names both values and asks."""
    workspace, path = _bench_with_own_target(tmp_path, monkeypatch, "stm32f411re")

    tools = service(workspace)
    try:
        applied = tools.call(PROJECT_CONFIG_ADOPT, {"apply": True, "debugger_id": "dut"})
    finally:
        tools.close()

    assert applied["ok"] is True, applied
    kept = {item["key"]: item for item in applied["kept"]}
    assert kept["debuggers.dut.target.controller"]["configured_value"] == "stm32f411re"
    assert kept["debuggers.dut.target.controller"]["discovered_value"] == "stm32f446re"
    assert not [item for item in applied["unavailable"] if item["key"] == "debuggers.dut.target.controller"]
    assert document_of(path)["debuggers"]["dut"]["target"]["controller"] == "stm32f411re"


# ---------------------------------------------------------------------------
# Giving the board back is a checked claim, not an assumption.


def test_a_lease_that_will_not_release_is_not_reported_as_a_clean_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`release` returns False and leaves the board quarantined and held.

    Discarding that answer reported successful discovery, wrote the
    configuration, and returned `ok: true` with `cleanup_required: false` about a
    bench that needs `agentic-hil recover` before anything else touches it."""
    workspace, path = placeholder_bench(tmp_path, monkeypatch, **{CONFIG_DESCRIPTION_RIGHT: True})
    before = path.read_text(encoding="utf-8")
    attached(monkeypatch)

    def refusing_release(self: Any, lease: Any, **kwargs: Any) -> bool:
        lease.quarantine("lease_release_retry", RuntimeError("durable record could not be written"))
        return False

    monkeypatch.setattr(HardwareCoordinator, "release_lease", refusing_release)
    tools = service(workspace)
    try:
        refused = tools.call(PROJECT_CONFIG_ADOPT, {"apply": True})
    finally:
        tools.close()

    assert refused["ok"] is False, refused
    assert refused["error_type"] == "resource_quarantined"
    assert refused["cleanup_required"] is True
    # The refusal is this call's own verdict about a lease it could not give
    # back, not a gate on the next one: the reason names no hardware contact,
    # and nothing stands on it afterwards.
    assert refused["quarantined"] is False
    assert refused["cleanup_reasons"] == ["lease_release_retry"]
    # The refusal is this call's own verdict about a lease it could not give
    # back, not a gate on the next one: the reason names no hardware contact and
    # nothing stands on it afterwards.
    assert tools.coordinator.incident_stands is False
    assert refused["retry_safe"] is False
    # The read did happen and its result is still handed over; what did not
    # happen is the write.
    assert refused["hardware_discovery"]["probe_id"] == PROBE_SERIAL
    assert path.read_text(encoding="utf-8") == before


# ---------------------------------------------------------------------------
# A configuration that will not parse is still this tool's answer to give.


def test_a_configuration_that_is_not_utf8_is_refused_rather_than_raised(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Reading the document is the first thing this does.

    A file saved as UTF-16 by a Windows editor made the decode escape as an
    internal error from under the MCP layer, on a tool whose whole contract is a
    structured answer."""
    workspace, path = placeholder_bench(tmp_path, monkeypatch, **{CONFIG_DESCRIPTION_RIGHT: True})
    attached(monkeypatch)
    tools = service(workspace)
    try:
        path.write_bytes(b"\xff\xfeversion: 2\n")
        refused = tools.call(PROJECT_CONFIG_ADOPT, {"apply": True})
    finally:
        tools.close()

    assert refused["ok"] is False, refused
    assert refused["error_type"] == "config_unreadable"
    assert refused["backend_error"]
    assert refused["config_status"]["state"] == STATE_UNREADABLE
