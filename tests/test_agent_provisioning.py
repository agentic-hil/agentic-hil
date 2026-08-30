"""One-time agent provisioning of a project configuration.

The invariant every test here circles, since the generated
default turned over: **an agent can only ever reduce its own authority.** A generation
grants everything it can — the file it writes is workable from the first call,
flashing included — and the direction is what is defended: `project_config_set`
writes `false` into a permission and never `true`.

"Everything it can" carries a correction of its own: the two permissions in
`EXCLUSIVE_FLASH_PERMISSIONS` refuse flashing while they are true and put no tool
behind that, so a generation writes them false and the file works. They are the
one exception, they are derived from the constant here rather than spelled out,
and `test_generated_configurations_can_flash.py` is what holds them to it.

The statement kept from the version of these tests that pinned the opposite is
that *a generation decides the permission state completely rather than half*. It
was worth asserting when the answer was "nothing" and it is worth asserting now
that the answer is "everything", which is why they were turned around instead of
deleted.

The ratchet is still the configuration, and there is still no second state store.
A delete-and-regenerate cycle yields the same file the CLI would have written, so
what it costs is every narrowing somebody asked for and what it yields is nothing
that was not already reachable.
"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest
import yaml

from agentic_hil.adopt import PROJECT_CONFIG_ADOPT
from agentic_hil.bench import BenchMutex
from agentic_hil.config import (
    config_schema,
    load_authoritative_config,
    project_config_directories,
    project_config_directory,
    project_config_leaf,
    project_config_path,
    provisionable_state_root,
    user_state_root,
)
from agentic_hil.configreload import PROJECT_CONFIG_RELOAD
from agentic_hil.configwrite import PROJECT_CONFIG_DESCRIBE, PROJECT_CONFIG_SET
from agentic_hil.contracts import MCP_TOOL_NAMES, MCP_TOOLS, TOOL_SCHEMAS
from agentic_hil.coordination import HardwareLease
from agentic_hil.knowledge import CONFIG_SHAPE_URI, EXCLUSIVE_FLASH_PERMISSIONS, read_resource, safe_user_root
from agentic_hil.mcp import SERVER_INSTRUCTIONS, handle_mcp_message
from agentic_hil.report import read_last_report
from agentic_hil.tools import (
    PROJECT_CONFIG_CREATE,
    AgenticHILToolService,
    UnprovisionedToolService,
    project_config_create,
)
from agentic_hil.types import CURRENT_CONFIG_VERSION, fold_hardware_id

# The fake CLI a generated configuration is pinned to. It has to be a real file
# outside the workspace, because loading the configuration pins the executable.
FAKE_PROGRAMMER = Path(__file__).resolve()


def bench(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    return workspace.resolve()


def attached_hardware(monkeypatch: pytest.MonkeyPatch, **overrides: object) -> dict:
    """Stand in for bootstrap discovery, which is the engine of this path."""
    discovery = {
        "ok": True,
        "tool": "bootstrap_hardware_discovery",
        "backend": "stlink",
        "executable": str(FAKE_PROGRAMMER),
        "probe_id": "STLINK123",
        "target": {"probe_id": "STLINK123", "controller": "STM32F446RE"},
        "com_port": {"device": "COM3", "serial_number": "STLINK123"},
        "available_com_ports": {"ok": True, "ports": [{"device": "COM3", "serial_number": "STLINK123"}]},
        "side_effect_committed": False,
        "side_effect_status": "not_started",
        "hardware_state": "unchanged",
        "cleanup_required": False,
        "summary": "One attached STM32 target was identified through ST-Link HOTPLUG discovery.",
    }
    discovery.update(overrides)

    def discover(*, probe_id: object = None, before_connect: object = None) -> dict:
        # Generation reads a board, so on a configured server it goes in under
        # that server's coordinator and announces the probe it selected before it
        # connects. The stand-in has to make that call, or a test would pass with
        # the probe lease never taken.
        if callable(before_connect):
            refusal = before_connect(str(discovery.get("probe_id") or ""))
            if refusal is not None:
                return refusal
        return discovery

    # Both, and for one reason: a configured server reads the board through the
    # shared lifecycle in `adopt`, and an unprovisioned one — which has no policy
    # to audit against and no lease directory — still reads directly from
    # `tools`. Patching one of the two leaves half these tests talking to the
    # real STM32CubeProgrammer.
    monkeypatch.setattr("agentic_hil.tools.discover_attached_hardware", discover)
    monkeypatch.setattr("agentic_hil.adopt.discover_attached_hardware", discover)
    return discovery


def granted(document: object, prefix: str = "") -> set[str]:
    """Every `allow_*` flag in a document that is true, by dotted path."""
    if isinstance(document, dict):
        found: set[str] = set()
        for key, value in document.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            if str(key).startswith("allow_") and value is True:
                found.add(path)
            found |= granted(value, path)
        return found
    return set()


def declared(document: object, prefix: str = "") -> set[str]:
    """Every `allow_*` flag in a document, by dotted path, whatever its value."""
    if isinstance(document, dict):
        found: set[str] = set()
        for key, value in document.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            if str(key).startswith("allow_"):
                found.add(path)
            found |= declared(value, path)
        return found
    return set()


def withheld_by_design(document: object) -> set[str]:
    """Every `allow_*` flag a generation writes false on purpose, by dotted path.

    The `EXCLUSIVE_FLASH_PERMISSIONS` pair on each debugger entry, read off the
    constant rather than spelled out here, so that widening the pair moves these
    assertions with it instead of leaving them quietly weaker."""
    entries = document.get("debuggers") if isinstance(document, dict) else None
    if not isinstance(entries, dict):
        return set()
    return {f"debuggers.{name}.permissions.{flag}" for name, entry in entries.items() if isinstance(entry, dict) for flag in EXCLUSIVE_FLASH_PERMISSIONS}


def written_document(result: dict) -> dict:
    document = yaml.safe_load(Path(result["path"]).read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    return document


def test_agent_generates_the_configuration_it_could_not_find(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = bench(tmp_path, monkeypatch)
    attached_hardware(monkeypatch)
    service = UnprovisionedToolService(workspace)
    try:
        blocked = service.call("probe_target")
        assert blocked["error_type"] == "config_file_not_found"
        assert PROJECT_CONFIG_CREATE in blocked["next_step"]

        result = service.call(PROJECT_CONFIG_CREATE)

        assert result["ok"] is True, result
        assert result["created"] is True
        assert Path(result["path"]) == project_config_path(workspace)
        document = written_document(result)
        # Probe id, controller and COM device are the values discovery
        # contributes, and the only ones the agent side contributes at all.
        assert document["debuggers"]["dut"]["probe_id"] == "STLINK123"
        assert document["debuggers"]["dut"]["type"] == "stlink"
        assert document["target"]["controller"] == "stm32f446re"
        assert document["com_ports"]["dut_uart"]["device"] == "COM3"
        assert document["version"] == CURRENT_CONFIG_VERSION
        # The session that generated it can read the bench through it, without a
        # restart nothing would have told the host about.
        assert service.config is not None
        assert service.config.debugger is not None
        assert service.config.debugger.probe_id == "STLINK123"
    finally:
        service.close()


def test_generated_configuration_grants_everything_at_all(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The turned-around form of the test that pinned the closed default.

    The statement is the same one: a generation decides the permission state
    completely rather than half, so nothing in the file is left to a template's
    memory or to whatever filled it in. Only the answer changed, and with it the
    day of hand-edited YAML that used to stand between `init` and a working
    bench.
    """
    workspace = bench(tmp_path, monkeypatch)
    attached_hardware(monkeypatch)
    service = UnprovisionedToolService(workspace)
    try:
        result = service.call(PROJECT_CONFIG_CREATE)
        document = written_document(result)

        assert document["permissions"] == {
            "allow_config_write": True,
            "allow_config_description_write": True,
            "allow_config_permissions_write": True,
            "allow_recover": True,
            "allow_upgrade": True,
        }
        assert document["debuggers"]["dut"]["permissions"] == {
            "allow_flash": True,
            "allow_reset": True,
            "allow_debug_execution": True,
            # False, and that is what makes allow_flash above mean anything:
            # either one true refuses flash_firmware on this probe.
            "allow_raw_debugger_commands": False,
            "allow_mass_erase": False,
        }
        assert document["com_ports"]["dut_uart"]["permissions"] == {"allow_write": True}
        assert document["artifacts"]["allow_upload"] is True
        assert document["debug"]["allow_all_symbols"] is True
        # Everything the file declares is granted except that pair, whatever it
        # is called. The complete decision, stated as one assertion rather than
        # as the list above, which is the half that survives a key being added.
        assert declared(document) - granted(document) == withheld_by_design(document)
        assert granted(document), "a generated configuration that declares no permission proves nothing"

        # And the file it wrote is one this server may write again: the ratchet
        # is no longer "generate once", it is "narrow only".
        again = service.call(PROJECT_CONFIG_CREATE)

        assert again["ok"] is True, again
        assert again["created"] is False
        assert granted(written_document(again)) == granted(document)
    finally:
        service.close()


def test_generated_configuration_ignores_permissions_the_workspace_asks_for(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A profile in the repository must not decide what the board may be told to do.

    `agentic-hil init` fills a configuration from this file on purpose: a person
    ran it. Over MCP the same file is repository-controlled data, and the
    generation ignores its permission block either way — which is now a check in
    the narrowing direction as well: a profile asking for `allow_mass_erase:
    false` does not reach a bench over MCP any more than one asking for `true`
    did. Repository content decides no permission here, in either direction.
    """
    workspace = bench(tmp_path, monkeypatch)
    (workspace / "agentic-hil.config.example.yaml").write_text(
        "target:\n"
        "  name: demo\n"
        "  controller: stm32f446ret6\n"
        "debuggers:\n"
        "  dut:\n"
        "    permissions:\n"
        "      allow_flash: false\n"
        "      allow_reset: true\n"
        "      allow_mass_erase: false\n"
        "com_ports:\n"
        "  uart:\n"
        "    permissions:\n"
        "      allow_write: false\n",
        encoding="utf-8",
    )
    attached_hardware(monkeypatch)
    service = UnprovisionedToolService(workspace)
    try:
        result = service.call(PROJECT_CONFIG_CREATE)

        document = written_document(result)
        assert declared(document) - granted(document) == withheld_by_design(document)
    finally:
        service.close()


def test_generated_configuration_says_an_agent_wrote_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = bench(tmp_path, monkeypatch)
    attached_hardware(monkeypatch)
    service = UnprovisionedToolService(workspace)
    try:
        result = service.call(PROJECT_CONFIG_CREATE)
        text = Path(result["path"]).read_text(encoding="utf-8")
        document = written_document(result)

        assert document["provenance"]["created_by"] == "agent"
        assert document["provenance"]["created_via"] == f"mcp:{PROJECT_CONFIG_CREATE}"
        assert document["provenance"]["created_at"].endswith("Z")
        # A reader opening the file sees it before any setting.
        assert text.startswith("# Generated by Agentic HIL for an AI agent")
        # And it says which way the file can move from here, because the header
        # is the one part of a generated file a person reads before the settings.
        assert "nothing an agent sends through it widens this file" in text
        assert "agentic-hil init --force" in text
        # It says it about `project_config_set` and it says what regeneration
        # does instead, because a header that promised the ratchet covered the
        # whole file would be describing a rule this project does not have — the
        # owner's decision on the generated default keeps creation out of it.
        assert "project_config_set" in text
        assert "arrives at the skeleton's defaults" in text
        assert "comes back at those defaults" in text
        # And the header states the pair it wrote false, so a reader does not
        # meet them as an unexplained narrowing further down.
        assert "allow_raw_debugger_commands and allow_mass_erase" in text
        assert "false so that\n# flashing works" in text
        # Provenance is a note to a reader, never policy: what decides the next
        # write is the permission beside it, not what the file says wrote it.
        assert load_authoritative_config(workspace).permissions.allow_config_write is True
    finally:
        service.close()


def test_deleting_the_configuration_lets_the_agent_generate_the_same_one_again(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The trade the config-resident ratchet makes, stated as a test.

    A configuration removed out of band puts the workspace back in the state it
    started from, and an agent may generate one again. What comes back is byte
    for byte the same skeleton, because the agent still cannot choose what it
    generates — so the cycle costs whatever a person had *narrowed* and yields
    the file `agentic-hil init` writes, which they could have run themselves.
    """
    workspace = bench(tmp_path, monkeypatch)
    attached_hardware(monkeypatch)
    first = UnprovisionedToolService(workspace)
    try:
        created = first.call(PROJECT_CONFIG_CREATE)
    finally:
        first.close()
    config_file = Path(created["path"])
    original = written_document(created)

    # A person narrows what their project should not have, then the file is lost.
    # allow_flash and not allow_mass_erase: the narrowing has to be of a flag a
    # generation leaves *true*, or the cycle this test measures costs nothing and
    # the assertion below passes without the file having moved.
    narrowed = deepcopy(original)
    narrowed["debuggers"]["dut"]["permissions"]["allow_flash"] = False
    config_file.write_text(yaml.safe_dump(narrowed, sort_keys=False), encoding="utf-8")
    config_file.unlink()

    second = UnprovisionedToolService(workspace)
    try:
        assert second.config is None
        again = second.call(PROJECT_CONFIG_CREATE)
    finally:
        second.close()

    assert again["ok"] is True, again
    assert again["created"] is True
    regenerated = written_document(again)
    # The second file is the first file: the same fixed skeleton, and the flash
    # grant the person had taken away is simply back. That is what the cycle
    # costs, and it is why the file is not where the direction is enforced.
    assert declared(regenerated) - granted(regenerated) == withheld_by_design(regenerated)
    assert regenerated["debuggers"]["dut"]["permissions"]["allow_flash"] is True
    assert regenerated["permissions"] == {
        "allow_config_write": True,
        "allow_config_description_write": True,
        "allow_config_permissions_write": True,
        "allow_recover": True,
        "allow_upgrade": True,
    }
    assert {key: value for key, value in regenerated.items() if key != "provenance"} == {
        key: value for key, value in original.items() if key != "provenance"
    }


def test_a_running_server_may_not_replace_the_configuration_it_lost(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A server that holds a policy is gated by it, file present or not.

    The permission that decides it is now granted by default, so what this pins
    is the other half: the server keeps enforcing the policy it loaded rather
    than treating a vanished file as an unconfigured workspace, and the write it
    then makes is the ordinary regeneration rather than a fresh start.
    """
    workspace = bench(tmp_path, monkeypatch)
    attached_hardware(monkeypatch)
    service = UnprovisionedToolService(workspace)
    try:
        created = service.call(PROJECT_CONFIG_CREATE)
        Path(created["path"]).unlink()

        rewritten = service.call(PROJECT_CONFIG_CREATE)
    finally:
        service.close()

    assert rewritten["ok"] is True, rewritten
    # Not "created": this server holds a policy, so it regenerated under it and
    # carried that policy's permissions over rather than starting over.
    assert rewritten["created"] is False


def test_a_running_server_that_lost_a_narrowed_configuration_may_not_widen_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Deleting the file is not a way past a narrowing this server is bound to.

    This is the case the old refusal covered and the one that still matters: a
    server whose configuration was narrowed and whose file then vanished must
    not be able to regenerate its way back to the open skeleton. It cannot,
    because a regeneration carries over the permissions of the policy in force —
    and the policy in force is the one this server loaded.
    """
    workspace = bench(tmp_path, monkeypatch)
    attached_hardware(monkeypatch)
    first = UnprovisionedToolService(workspace)
    try:
        created = first.call(PROJECT_CONFIG_CREATE)
        narrowed = written_document(created)
        narrowed["debuggers"]["dut"]["permissions"]["allow_mass_erase"] = False
        narrowed["artifacts"]["allow_upload"] = False
        Path(created["path"]).write_text(yaml.safe_dump(narrowed, sort_keys=False), encoding="utf-8")
    finally:
        first.close()

    bound = UnprovisionedToolService(workspace)
    try:
        assert bound.config is not None
        assert bound.config.debugger is not None
        assert bound.config.debugger.permissions.allow_mass_erase is False
        Path(created["path"]).unlink()

        rewritten = bound.call(PROJECT_CONFIG_CREATE)
    finally:
        bound.close()

    assert rewritten["ok"] is True, rewritten
    assert rewritten["created"] is False
    document = written_document(rewritten)
    assert document["debuggers"]["dut"]["permissions"]["allow_mass_erase"] is False
    assert document["artifacts"]["allow_upload"] is False


def test_the_tool_surface_cannot_take_the_configuration_away() -> None:
    """Nothing here removes or moves a configuration.

    Regenerating one costs a human their settings, so the surface offers no way
    to bring that about: no delete, no move, no path argument anywhere. The tool
    that regenerates the file takes no arguments at all, and the one that changes
    it takes named keys with scalar values — never a document, never a path.
    """
    assert sorted(name for name in MCP_TOOL_NAMES if "config" in name) == [
        PROJECT_CONFIG_ADOPT,
        PROJECT_CONFIG_CREATE,
        PROJECT_CONFIG_DESCRIBE,
        PROJECT_CONFIG_RELOAD,
        PROJECT_CONFIG_SET,
    ]
    assert not [name for name in MCP_TOOL_NAMES if any(verb in name for verb in ("delete", "remove", "move", "rename"))]
    assert TOOL_SCHEMAS[PROJECT_CONFIG_CREATE] == {"type": "object", "properties": {}, "additionalProperties": False}
    assert TOOL_SCHEMAS[PROJECT_CONFIG_DESCRIBE] == {"type": "object", "properties": {}, "additionalProperties": False}
    # No argument at all, and that is what keeps the reload from becoming a
    # second door onto the permissions: there is no flag for one to arrive under.
    assert TOOL_SCHEMAS[PROJECT_CONFIG_RELOAD] == {"type": "object", "properties": {}, "additionalProperties": False}
    change = TOOL_SCHEMAS[PROJECT_CONFIG_SET]["properties"]["changes"]["items"]
    assert sorted(change["properties"]) == ["key", "value"]
    assert change["additionalProperties"] is False
    # No object and no array: a subtree is content the agent authored, and a
    # subtree can carry a permissions: block inside it.
    assert set(change["properties"]["value"]["type"]) == {"string", "number", "integer", "boolean", "null"}
    # The adoption path selects and never supplies: one boolean deciding whether
    # to write, and three names choosing which probe and which entries this is
    # about. Nothing here could put a caller's own value into the file.
    adopt = TOOL_SCHEMAS[PROJECT_CONFIG_ADOPT]
    assert sorted(adopt["properties"]) == ["apply", "com_port_id", "debugger_id", "probe_id"]
    assert adopt["additionalProperties"] is False
    assert adopt.get("required") is None
    assert adopt["properties"]["apply"]["type"] == "boolean"
    assert all(adopt["properties"][name]["type"] == "string" for name in ("com_port_id", "debugger_id", "probe_id"))


def test_a_regeneration_carries_over_what_a_person_narrowed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A hardware refresh refreshes hardware, not what the bench may be told to do.

    The direction that matters here is the one a regeneration could break: a
    person took two permissions away, and a call made to pick up a new probe
    serial must not hand them back. The generation grants everything only where
    there is no configuration to read.
    """
    workspace = bench(tmp_path, monkeypatch)
    attached_hardware(monkeypatch)
    service = UnprovisionedToolService(workspace)
    try:
        created = service.call(PROJECT_CONFIG_CREATE)
    finally:
        service.close()

    config_file = Path(created["path"])
    narrowed = written_document(created)
    narrowed["debuggers"]["dut"]["permissions"]["allow_mass_erase"] = False
    narrowed["com_ports"]["dut_uart"]["permissions"]["allow_write"] = False
    config_file.write_text(yaml.safe_dump(narrowed, sort_keys=False), encoding="utf-8")

    attached_hardware(monkeypatch, probe_id="STLINK999", target={"controller": "STM32F411RE"}, com_port={"device": "COM7"})
    reopened = UnprovisionedToolService(workspace)
    try:
        rewritten = reopened.call(PROJECT_CONFIG_CREATE)
    finally:
        reopened.close()

    assert rewritten["ok"] is True, rewritten
    assert rewritten["created"] is False
    # The rewrite changed the file, not the configuration this server loaded.
    assert rewritten["reload_required"] is True
    document = written_document(rewritten)
    # The hardware facts are refreshed …
    assert document["debuggers"]["dut"]["probe_id"] == "STLINK999"
    assert document["com_ports"]["dut_uart"]["device"] == "COM7"
    # … and every permission is the one the person left behind. The agent
    # contributes no permission value on this path either, in either direction.
    assert document["permissions"]["allow_config_write"] is True
    assert document["debuggers"]["dut"]["permissions"]["allow_flash"] is True
    assert document["debuggers"]["dut"]["permissions"]["allow_mass_erase"] is False
    assert document["com_ports"]["dut_uart"]["permissions"]["allow_write"] is False


def test_regeneration_keeps_the_artifact_roots_the_person_set(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The skeleton names the whole workspace. A regeneration that carried the
    # skeleton's value would widen a file whose owner had narrowed it — the same
    # silent widening the carried-over permissions exist to prevent.
    workspace = bench(tmp_path, monkeypatch)
    attached_hardware(monkeypatch)
    service = UnprovisionedToolService(workspace)
    try:
        created = service.call(PROJECT_CONFIG_CREATE)
    finally:
        service.close()

    assert written_document(created)["artifacts"]["allowed_roots"] == ["."]

    config_file = Path(created["path"])
    opened = written_document(created)
    opened["permissions"]["allow_config_write"] = True
    opened["artifacts"]["allowed_roots"] = ["build", "dist"]
    config_file.write_text(yaml.safe_dump(opened, sort_keys=False), encoding="utf-8")

    attached_hardware(monkeypatch, probe_id="STLINK999")
    reopened = UnprovisionedToolService(workspace)
    try:
        rewritten = reopened.call(PROJECT_CONFIG_CREATE)
    finally:
        reopened.close()

    assert rewritten["ok"] is True, rewritten
    document = written_document(rewritten)
    assert document["debuggers"]["dut"]["probe_id"] == "STLINK999"
    assert document["artifacts"]["allowed_roots"] == ["build", "dist"]


def test_regeneration_keeps_the_artifact_extensions_the_person_set(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The roots name the whole workspace, so the extension list is one of the
    # checks that still refuse a foreign artifact. Carrying the roots and not
    # this would hand a bench narrowed to one format back to its owner
    # accepting three, off a call made to refresh a probe id.
    #
    # upload_directory and max_upload_size_mb come back as the skeleton wrote
    # them, and that is the intended line: they place a directory and size a
    # payload rather than say what may be flashed, and this same call
    # re-derives state_root from the environment on purpose. debug carries
    # allowed_symbols and leaves max_dump_size_bytes alone for the same reason.
    workspace = bench(tmp_path, monkeypatch)
    attached_hardware(monkeypatch)
    service = UnprovisionedToolService(workspace)
    try:
        created = service.call(PROJECT_CONFIG_CREATE)
    finally:
        service.close()

    assert written_document(created)["artifacts"]["allowed_extensions"] == [".elf", ".hex", ".bin"]

    config_file = Path(created["path"])
    opened = written_document(created)
    opened["permissions"]["allow_config_write"] = True
    opened["artifacts"]["allowed_extensions"] = [".bin"]
    opened["artifacts"]["upload_directory"] = ".agentic-hil/firmware"
    opened["artifacts"]["max_upload_size_mb"] = 4
    config_file.write_text(yaml.safe_dump(opened, sort_keys=False), encoding="utf-8")

    attached_hardware(monkeypatch, probe_id="STLINK999")
    reopened = UnprovisionedToolService(workspace)
    try:
        rewritten = reopened.call(PROJECT_CONFIG_CREATE)
    finally:
        reopened.close()

    assert rewritten["ok"] is True, rewritten
    document = written_document(rewritten)
    assert document["debuggers"]["dut"]["probe_id"] == "STLINK999"
    assert document["artifacts"]["allowed_extensions"] == [".bin"]
    assert document["artifacts"]["upload_directory"] == ".agentic-hil/artifacts"
    assert document["artifacts"]["max_upload_size_mb"] == 64


def test_a_configured_server_refuses_to_write_a_configuration_that_took_the_grant_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ordinary server carries the same refusal.

    A project that was set up by a person is the common case, and the tool is
    advertised there too — it has to answer with the permission it is denied by,
    not by being absent from the list. What reaches the refusal now is a
    configuration whose `allow_config_write` was taken away, which is a narrowing
    an agent can make itself and never take back.
    """
    workspace = bench(tmp_path, monkeypatch)
    attached_hardware(monkeypatch)
    provisioner = UnprovisionedToolService(workspace)
    try:
        created = provisioner.call(PROJECT_CONFIG_CREATE)
    finally:
        provisioner.close()

    narrowed = written_document(created)
    narrowed["permissions"]["allow_config_write"] = False
    Path(created["path"]).write_text(yaml.safe_dump(narrowed, sort_keys=False), encoding="utf-8")

    service = AgenticHILToolService(load_authoritative_config(workspace), frontend="mcp")
    try:
        refused = service.call(PROJECT_CONFIG_CREATE)
    finally:
        service.close()

    assert refused["error_type"] == "permission_denied"
    assert refused["permission"] == "allow_config_write"
    assert refused["retry_safe"] is False
    assert "only the operator may edit it" in refused["next_step"]
    assert written_document(created)["provenance"]["created_by"] == "agent"


def test_creation_is_only_for_the_workspace_the_server_is_bound_to(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = bench(tmp_path, monkeypatch)
    foreign = (tmp_path / "foreign").resolve()
    foreign.mkdir()
    attached_hardware(monkeypatch)
    service = UnprovisionedToolService(workspace)
    try:
        named = service.call(PROJECT_CONFIG_CREATE, {"workspace_root": str(foreign)})

        assert named["error_type"] == "invalid_argument"
        assert not project_config_path(foreign).exists()

        # Not even the working directory decides it: the binding was made when
        # the server started and nothing later can move it.
        monkeypatch.chdir(foreign)
        result = service.call(PROJECT_CONFIG_CREATE)

        assert result["workspace_root"] == str(workspace)
        assert Path(result["path"]) == project_config_path(workspace)
        assert not project_config_path(foreign).exists()
    finally:
        service.close()


def test_a_refused_state_root_falls_back_to_a_location_that_passes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The documented default is refused on a stock Windows profile.

    A generated configuration naming it would be written and then refused on
    load, so the creation path picks the location every refusal already
    recommends instead.
    """
    workspace = bench(tmp_path, monkeypatch)
    blocked = tmp_path / "blocked-state"
    blocked.mkdir()
    # user_state_root() appends "agentic-hil"; a file there is a location the
    # trust check refuses on both platforms without touching a single ACL.
    (blocked / "agentic-hil").write_text("not a directory", encoding="utf-8")
    monkeypatch.setenv("LOCALAPPDATA", str(blocked))
    monkeypatch.setenv("XDG_STATE_HOME", str(blocked))
    attached_hardware(monkeypatch)
    service = UnprovisionedToolService(workspace)
    try:
        result = service.call(PROJECT_CONFIG_CREATE)
    finally:
        service.close()

    assert result["ok"] is True, result
    fallback = Path(Path.home() / ".agentic-hil" / "state").resolve()
    assert Path(result["state_root"]) == fallback
    assert provisionable_state_root(workspace) == fallback
    assert written_document(result)["state_root"] == str(fallback)


def _block_config_directory(root: Path, workspace: Path) -> Path:
    """Put a file where this workspace's configuration directory belongs."""
    blocked = root / project_config_leaf(workspace)
    blocked.parent.mkdir(parents=True, exist_ok=True)
    blocked.write_text("a file where the configuration directory belongs", encoding="utf-8")
    return blocked


def test_a_refused_config_location_falls_through_to_the_trusted_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The configuration's own location has the walk `state_root` has (#354).

    It did not, and a stock installation inside an MSIX-packaged host therefore
    dead-ended on a refusal whose own remediation named `~/.agentic-hil` as a safe
    answer that no code path took.
    """
    workspace = bench(tmp_path, monkeypatch)
    attached_hardware(monkeypatch)
    _block_config_directory(project_config_directory(), workspace)
    service = UnprovisionedToolService(workspace)
    try:
        created = service.call(PROJECT_CONFIG_CREATE)
    finally:
        service.close()

    assert created["ok"] is True, created
    assert Path(created["path"]).parent.parent == Path(safe_user_root()) / "projects"
    # And it is found again from there, or the fall-through would have written a
    # file nothing can load.
    assert project_config_path(workspace) == Path(created["path"])
    assert load_authoritative_config(workspace).config_path == created["path"]


def test_a_refused_config_location_returns_the_actionable_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Where no location can be picked, the refusal has to carry the way out.

    The fall-through is a fallback and not a promise that one always exists, so
    with both candidate roots blocked what the caller gets is the error type and
    the remediation the CLI returns for the same wall.
    """
    workspace = bench(tmp_path, monkeypatch)
    attached_hardware(monkeypatch)
    for root in project_config_directories():
        _block_config_directory(root, workspace)
    service = UnprovisionedToolService(workspace)
    try:
        refused = service.call(PROJECT_CONFIG_CREATE)
    finally:
        service.close()

    assert refused["ok"] is False
    assert refused["error_type"] == "unsafe_configured_path"
    assert refused["tool"] == PROJECT_CONFIG_CREATE
    assert any(".agentic-hil" in step for step in refused["remediation"])
    assert refused["do_not"]
    assert not project_config_path(workspace).is_file(), "a refusal must not write anything"


def test_discovery_failure_leaves_the_grant_unspent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A configuration without discovery would name no probe and no port.

    Writing one would leave the project worse off than the config_file_not_found
    it started from, so it refuses and writes nothing; the same call works once a
    board is attached.
    """
    workspace = bench(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "agentic_hil.tools.discover_attached_hardware",
        lambda *args, **kwargs: {
            "ok": False,
            "tool": "bootstrap_hardware_discovery",
            "error_type": "adapter_not_found",
            "summary": "No ST-Link probe is attached.",
            "side_effect_committed": False,
            "side_effect_status": "not_started",
            "hardware_state": "unchanged",
            "cleanup_required": False,
        },
    )
    service = UnprovisionedToolService(workspace)
    try:
        refused = service.call(PROJECT_CONFIG_CREATE)

        assert refused["ok"] is False
        assert refused["error_type"] == "adapter_not_found"
        assert not project_config_path(workspace).exists()

        attached_hardware(monkeypatch)
        assert service.call(PROJECT_CONFIG_CREATE)["ok"] is True
    finally:
        service.close()


def test_provisioning_is_reachable_over_mcp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = bench(tmp_path, monkeypatch)
    attached_hardware(monkeypatch)
    service = UnprovisionedToolService(workspace)
    try:
        response = handle_mcp_message(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": PROJECT_CONFIG_CREATE, "arguments": {}}},
            service,
        )
        assert isinstance(response, dict)
        result = response["result"]

        assert result["isError"] is False
        assert result["structuredContent"]["created"] is True
        # The permission summary a host sees is the granted one, so the answer a
        # caller reads over the wire is the answer the file gives.
        assert result["structuredContent"]["permissions"]["allow_config_write"] is True
        assert result["structuredContent"]["permissions"]["debuggers"]["dut"]["allow_flash"] is True
        assert result["structuredContent"]["permissions"]["debuggers"]["dut"]["allow_mass_erase"] is False

        again = handle_mcp_message(
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": PROJECT_CONFIG_CREATE, "arguments": {}}},
            service,
        )
        assert isinstance(again, dict)
        assert again["result"]["isError"] is False
        assert again["result"]["structuredContent"]["created"] is False
    finally:
        service.close()


def test_default_state_root_is_still_preferred_when_it_passes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The fallback is a fallback, not a relocation of every profile's state."""
    workspace = bench(tmp_path, monkeypatch)

    assert provisionable_state_root(workspace) == user_state_root()


# ---------------------------------------------------------------------------
# The two proofs the open generated default asks for.


def test_an_empty_directory_reaches_a_hardware_action_with_no_yaml_editing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """From nothing to hardware, counted in round trips between human and agent.

    The whole of the open generated default, as a sequence. Under the closed default this run
    stopped at the second call with `permission_denied` on `allow_flash`, and the
    way on was a person finding the key in a YAML file outside the repository and
    setting it by hand — the day of work that produced this change. Here nobody
    opens an editor at any point.

    Flashing takes no extra call, and that is what the interlock correction fixed. Validated
    flashing and unrestricted debugger access are mutually exclusive policies
    (docs/security-design.md), and for a while a generated configuration granted
    both sides of that exclusion — so `flash_firmware` was refused on a file
    nobody had touched, and the way on was for somebody to know that two
    permissions being *on* was what blocked it. The generation writes that pair
    false now, and neither of them was a capability to give up: no MCP tool reads
    either as permission to do anything.
    """
    workspace = bench(tmp_path, monkeypatch)
    attached_hardware(monkeypatch)
    # Intel HEX rather than a raw binary: ST-Link needs `flash_address` for a
    # .bin, which is a backend's requirement rather than anything this is about.
    artifact = workspace / "build" / "app.hex"
    artifact.parent.mkdir(parents=True)
    artifact.write_text(":00000001FF\n", encoding="ascii")

    service = UnprovisionedToolService(workspace)
    try:
        # 1. The wall a new project hits, and the way out it names.
        blocked = service.call("probe_target")
        assert blocked["error_type"] == "config_file_not_found"
        assert PROJECT_CONFIG_CREATE in blocked["next_step"]

        # 2. One call, no arguments, no editor, and the bench is described and
        #    granted — including the two the interlock reads, which the
        #    generation writes false precisely so that step 3 works.
        created = service.call(PROJECT_CONFIG_CREATE)
        assert created["ok"] is True, created
        assert created["permissions"]["debuggers"]["dut"]["allow_flash"] is True
        assert created["permissions"]["debuggers"]["dut"]["allow_raw_debugger_commands"] is False
        assert created["permissions"]["debuggers"]["dut"]["allow_mass_erase"] is False
        assert any("mutually exclusive" in step for step in created["next_steps"]), created["next_steps"]

        # 3. Flashing works on the first try. There is no narrowing step between
        #    the generation and the first flash, and that is the whole point of
        #    this test: until the interlock correction there was one, and an operator who did
        #    not know to take it could not flash a freshly generated bench at all.
        flashed = AgenticHILToolService(load_authoritative_config(workspace), frontend="mcp")
        try:
            result = flashed.call("flash_firmware", {"image_path": "build/app.hex"})
        finally:
            flashed.close()
    finally:
        service.close()

    # Past every gate a permission decides, and stopped at the fake toolchain:
    # no `permission_denied` and no schema refusal on the way, so the only thing
    # left between an empty directory and a real board is the board.
    assert result["error_type"] not in ("permission_denied", "invalid_argument"), result
    assert result["artifact"]["path"].endswith("app.hex"), result
    # And the file it all came out of was never touched by hand, or touched
    # twice: one call wrote it and nothing edited it afterwards. The pair below
    # is false because the generation wrote it that way, not because anybody was
    # asked to fix a bench that would not flash.
    document = written_document(created)
    assert document["provenance"]["created_by"] == "agent"
    assert "last_modified_by" not in document["provenance"]
    assert declared(document) - granted(document) == {
        "debuggers.dut.permissions.allow_raw_debugger_commands",
        "debuggers.dut.permissions.allow_mass_erase",
    }


def test_a_narrowed_configuration_cannot_be_reopened_by_an_agent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The other half: everything an agent closes stays closed.

    Four attempts, and the fourth is the one that matters — a permission this
    same agent took away a moment earlier is exactly as unreachable as one it
    never touched. After the terminal move nothing on this surface can move a
    permission at all, and the call that made it said so.
    """
    workspace = bench(tmp_path, monkeypatch)
    attached_hardware(monkeypatch)
    service = UnprovisionedToolService(workspace)
    try:
        created = service.call(PROJECT_CONFIG_CREATE)
        assert created["ok"] is True, created
        path = Path(created["path"])

        # The operator asks for the bench to be narrowed, in prose. The agent
        # writes it, with a provenance record and no YAML editing.
        narrowed = service.call(
            PROJECT_CONFIG_SET,
            {"changes": [{"key": "debuggers.dut.permissions.allow_reset", "value": False}]},
        )
        assert narrowed["ok"] is True, narrowed
        assert narrowed["permissions_changed"] == ["debuggers.dut.permissions.allow_reset"]
        assert "permissions_frozen" not in narrowed
        assert written_document(created)["provenance"]["last_modified_by"] == "agent"

        # 1. It cannot put back what it just took away.
        reopened = service.call(
            PROJECT_CONFIG_SET,
            {"changes": [{"key": "debuggers.dut.permissions.allow_reset", "value": True}]},
        )
        assert reopened["error_type"] == "permission_widening_denied"
        assert reopened["widened_keys"] == ["debuggers.dut.permissions.allow_reset"]
        assert reopened["reopened_by"] == "agentic-hil init --force"
        assert reopened["side_effect_committed"] is False
        assert written_document(created)["debuggers"]["dut"]["permissions"]["allow_reset"] is False

        # 2. Nor can it slip one through beside a narrowing: a change is applied
        #    whole or not at all, so the narrowing is refused with the widening.
        mixed = service.call(
            PROJECT_CONFIG_SET,
            {
                "changes": [
                    {"key": "com_ports.dut_uart.permissions.allow_write", "value": False},
                    {"key": "debuggers.dut.permissions.allow_reset", "value": True},
                ]
            },
        )
        assert mixed["error_type"] == "permission_widening_denied"
        assert written_document(created)["com_ports"]["dut_uart"]["permissions"]["allow_write"] is True

        # 3. The terminal move, and it announces itself in its own result.
        froze = service.call(
            PROJECT_CONFIG_SET,
            {"changes": [{"key": "permissions.allow_config_permissions_write", "value": False}]},
        )
        assert froze["ok"] is True, froze
        frozen = froze["permissions_frozen"]
        assert frozen["closed_key"] == "permissions.allow_config_permissions_write"
        assert frozen["irreversible_by_agent"] is True
        assert frozen["reopened_by"] == "agentic-hil init --force"
        # What stands frozen, read out of the file rather than out of the
        # request: the one already taken away and the ones left granted.
        assert frozen["frozen_permissions"]["debuggers.dut.permissions.allow_reset"] is False
        assert frozen["frozen_permissions"]["debuggers.dut.permissions.allow_flash"] is True
        assert frozen["frozen_permissions"]["permissions.allow_config_permissions_write"] is False
        # And it is in the result of *that* call, not only in a reference.
        assert "agentic-hil init --force" in froze["summary"] + " ".join(froze["next_steps"])
        assert froze["next_steps"][0].startswith("Report this before anything else")

        # 4. After it, no permission moves in either direction. A new server on
        #    the same file, so the loaded policy is the narrowed one.
        service.close()
        after = AgenticHILToolService(load_authoritative_config(workspace), frontend="mcp")
        try:
            for value in (True, False):
                refused = after.call(PROJECT_CONFIG_SET, {"changes": [{"key": "debuggers.dut.permissions.allow_reset", "value": value}]})
                assert refused["error_type"] == "permission_denied", (value, refused)
                assert refused["permission"] == "allow_config_permissions_write"
        finally:
            after.close()
    finally:
        service.close()

    document = written_document(created)
    assert document["debuggers"]["dut"]["permissions"] == {
        "allow_flash": True,
        # The narrowing this test made, which no later call could put back.
        "allow_reset": False,
        "allow_debug_execution": True,
        # The generated default, false before anything narrowed anything.
        "allow_raw_debugger_commands": False,
        "allow_mass_erase": False,
    }
    assert document["permissions"]["allow_config_permissions_write"] is False
    assert path.exists()


# ---------------------------------------------------------------------------
# Regeneration reads a board, and is held to what every board read is held to.
#
# Under the closed default this path was reachable exactly once per workspace, so
# a run could not be open across it and there was nothing to coordinate with.
# A generation grants `allow_config_write` in every generated file, which makes
# regeneration an ordinary call an agent can make at any moment — including the
# middle of a run, and including while another process on this machine is driving
# the probe it would enumerate.


def test_regeneration_is_refused_while_this_server_holds_the_bench(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A run's holds were taken under the policy this call would replace.

    The same refusal `project_config_set` gives, one step earlier: this one also
    talks to the board before it writes anything."""
    workspace = bench(tmp_path, monkeypatch)
    attached_hardware(monkeypatch)
    provisioner = UnprovisionedToolService(workspace)
    try:
        created = provisioner.call(PROJECT_CONFIG_CREATE)
    finally:
        provisioner.close()
    assert created["ok"] is True, created
    before = Path(created["path"]).read_bytes()

    service = AgenticHILToolService(load_authoritative_config(workspace), frontend="mcp")
    try:
        started = service.call("bench_run_start", {"devices": [{"kind": "uart", "id": "dut_uart"}], "label": "boot-smoke"})
        assert started["ok"] is True, started

        refused = service.call(PROJECT_CONFIG_CREATE)

        assert refused["ok"] is False
        assert refused["error_type"] == "config_write_in_open_run"
        assert refused["open_holds"]["run_active"] is True
        assert refused["open_holds"]["run_label"] == "boot-smoke"
        assert refused["retry_safe"] is True
        assert any("bench_run_stop" in step for step in refused["remediation"])
        assert Path(created["path"]).read_bytes() == before

        assert service.call("bench_run_stop")["ok"] is True
        assert service.call(PROJECT_CONFIG_CREATE)["ok"] is True
    finally:
        service.close()


def test_regeneration_does_not_reach_a_probe_another_owner_holds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A board belongs to whoever holds it, whatever this file says.

    The holder is a stranger — another MCP server, another project's run. It has
    the physical probe, which is the lock this path takes between enumerating and
    connecting, so the HOTPLUG connect never happens and nothing is written."""
    workspace = bench(tmp_path, monkeypatch)
    attached_hardware(monkeypatch)
    provisioner = UnprovisionedToolService(workspace)
    try:
        created = provisioner.call(PROJECT_CONFIG_CREATE)
    finally:
        provisioner.close()
    before = Path(created["path"]).read_bytes()

    stranger = BenchMutex(frontend="stranger", label="other-bench-session")
    stranger.acquire([f"probe:{fold_hardware_id('STLINK123')}"])
    service = AgenticHILToolService(load_authoritative_config(workspace), frontend="mcp")
    try:
        refused = service.call(PROJECT_CONFIG_CREATE)
    finally:
        service.close()
        stranger.release_all()

    assert refused["ok"] is False
    assert refused["error_type"] == "device_busy"
    assert refused["holder"]["label"] == "other-bench-session"
    assert refused["side_effect_committed"] is False
    # The advice is about the owner of the board, not about attaching one.
    assert "attach" not in refused["next_step"].lower()
    assert Path(created["path"]).read_bytes() == before


def _regenerable_bench(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **entry: object) -> tuple[Path, Path]:
    """A workspace with a generated configuration, ready to be regenerated.

    ``entry`` is written onto the `debuggers.dut` entry afterwards, which is how
    a test gives that entry a `resource_id` — the canonical alias a run holds a
    board through, and the one a regeneration has to lock in its own right."""
    workspace = bench(tmp_path, monkeypatch)
    attached_hardware(monkeypatch)
    provisioner = UnprovisionedToolService(workspace)
    try:
        created = provisioner.call(PROJECT_CONFIG_CREATE)
    finally:
        provisioner.close()
    assert created["ok"] is True, created
    path = Path(created["path"])
    if entry:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        document["debuggers"]["dut"].update(entry)
        path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return workspace, path


def test_regeneration_does_not_reach_a_board_held_through_its_resource_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The lock a run holds is the configured entry's, not the enumerated serial.

    `resource_id` is how an operator says "this probe and this COM port are one
    unit", and it is the key every run on that bench holds. A regeneration that
    locked only the serial the enumeration returned would connect to a board
    another owner is holding under that alias — the whole point of the alias
    being that it is the identity, and the serial one spelling of it."""
    workspace, path = _regenerable_bench(tmp_path, monkeypatch, resource_id="bench-a")
    before = path.read_bytes()

    stranger = BenchMutex(frontend="stranger", label="other-bench-session")
    stranger.acquire([f"physical:{fold_hardware_id('bench-a')}"])
    service = AgenticHILToolService(load_authoritative_config(workspace), frontend="mcp")
    try:
        refused = service.call(PROJECT_CONFIG_CREATE)
    finally:
        service.close()
        stranger.release_all()

    assert refused["ok"] is False
    assert refused["error_type"] == "device_busy"
    assert refused["holder"]["label"] == "other-bench-session"
    assert f"physical:{fold_hardware_id('bench-a')}" in refused["requested_resources"]
    assert refused["side_effect_committed"] is False
    assert path.read_bytes() == before


def test_a_regeneration_is_written_into_the_audit_trail(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Reading a board is reading a board, whichever call did it.

    Regeneration enumerates and connects in HOTPLUG mode exactly as
    `debugger_probes_list` does, so it leaves the same record: which probe, which
    locks, and the state those leases actually ended in."""
    workspace, _ = _regenerable_bench(tmp_path, monkeypatch, resource_id="bench-a")

    service = AgenticHILToolService(load_authoritative_config(workspace), frontend="mcp")
    try:
        regenerated = service.call(PROJECT_CONFIG_CREATE)
        assert regenerated["ok"] is True, regenerated
        recorded = service.call("get_last_report")
    finally:
        service.close()

    report = recorded["report"]
    assert report["tool"] == PROJECT_CONFIG_CREATE
    assert report["probe_id"] == "STLINK123"
    assert report["resources"] == [
        "debugger-discovery:all",
        f"physical:{fold_hardware_id('bench-a')}",
        f"probe:{fold_hardware_id('STLINK123')}",
    ]
    # Written while the board was held, re-committed once the leases reached
    # their terminal state, so the record and the result agree about the bench.
    assert report["lease_state"] == "released"
    assert report["cleanup_required"] is False
    assert report["quarantined"] is False


def test_a_regeneration_whose_record_cannot_be_written_quarantines_the_probe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A board that was read and not recorded is not a board to write a file from.

    The read happened. If the audit trail cannot say so, the configuration is not
    rewritten on top of it: the leases stay taken and quarantined, and the
    operator owns the incident."""
    workspace, path = _regenerable_bench(tmp_path, monkeypatch)
    before = path.read_bytes()
    # After the read, not before it: `ensure_audit_ready` already refuses a bench
    # whose trail is unwritable before the board is touched, and the case here is
    # the other one — the record of a read that has already happened.
    monkeypatch.setattr("agentic_hil.adopt.write_report", lambda config, report: {**report, "audit_ok": False})

    service = AgenticHILToolService(load_authoritative_config(workspace), frontend="mcp")
    try:
        refused = service.call(PROJECT_CONFIG_CREATE)
    finally:
        service.close()

    assert refused["ok"] is False
    assert refused["error_type"] == "audit_failed_after_action"
    assert refused["quarantined"] is True
    assert refused["retry_safe"] is False
    assert path.read_bytes() == before


def test_a_regeneration_that_cannot_give_the_board_back_writes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`release()` returning False is an answer, not a formality.

    The generation path used to discard it, so a lease left registered,
    quarantined and blocking still came back as `ok: true`, `cleanup_required:
    false` — with the configuration rewritten under a board this process was
    still holding."""
    workspace, path = _regenerable_bench(tmp_path, monkeypatch)
    before = path.read_bytes()

    original = HardwareLease.release

    def release(self: HardwareLease, **kwargs: object) -> bool:
        if any(str(resource).startswith("probe:") for resource in self.resources):
            self.state = "cleanup_required"
            self.errors.append({"reason": "config_create_release_failed"})
            self.quarantine_id = "quarantine-test-0002"
            return False
        return bool(original(self, **kwargs))

    monkeypatch.setattr(HardwareLease, "release", release)

    service = AgenticHILToolService(load_authoritative_config(workspace), frontend="mcp")
    try:
        refused = service.call(PROJECT_CONFIG_CREATE)
        failure = service.call("classify_last_error")
    finally:
        service.close()

    assert refused["ok"] is False
    assert refused["error_type"] == "resource_quarantined"
    assert refused["cleanup_required"] is True
    assert refused["lease_state"] == "cleanup_required"
    assert refused["quarantine_id"] == "quarantine-test-0002"
    assert path.read_bytes() == before, "nothing is written under a lease this process still holds"
    assert failure["error_type"] == "resource_quarantined"
    assert failure["source_tool"] == PROJECT_CONFIG_CREATE


def test_a_regeneration_that_raises_comes_back_as_a_quarantine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail closed, and in the shape every other hardware call fails in.

    `project_config_create` was outside the audited-hardware exception path, so a
    discovery that raised escaped the service instead of producing the standard
    structured refusal — and nothing shut the bench behind it."""
    workspace, path = _regenerable_bench(tmp_path, monkeypatch)
    # A regeneration writes the same deterministic document the fixture wrote,
    # so equal bytes would be a correct outcome and prove nothing about a
    # rewrite. The marker is what a rewrite cannot preserve.
    path.write_bytes(path.read_bytes() + b"# an operator's note a regeneration does not carry over\n")
    before = path.read_bytes()

    def exploding(timeout_s: float = 10.0, *, probe_id: str | None = None, before_connect: object = None) -> dict:
        if callable(before_connect):
            before_connect("STLINK123")
        raise RuntimeError("STM32_Programmer_CLI died mid-connect")

    monkeypatch.setattr("agentic_hil.adopt.discover_attached_hardware", exploding)

    service = AgenticHILToolService(load_authoritative_config(workspace), frontend="mcp")
    try:
        failed = service.call(PROJECT_CONFIG_CREATE)
        assert failed["ok"] is False
        assert failed["error_type"] == "hardware_action_exception"
        # The reason is recorded and the bench is not held for it.
        assert failed["quarantined"] is False
        assert failed["incident_stood_down"]["reasons"] == ["config_create_discovery_exception", "unknown_hardware_exception"]
        assert failed["cleanup_required"] is True
        # And the retry is that next contact: a probe read that raised leaves a
        # target the next reset and probe speak for, so the bench is not shut
        # behind it and the regeneration that follows either reads the board or
        # fails on its own evidence.
        attached_hardware(monkeypatch)
        retried = service.call(PROJECT_CONFIG_CREATE)
    finally:
        service.close()

    assert retried["ok"] is True, retried
    assert path.read_bytes() != before


def test_a_regeneration_whose_terminal_record_fails_quarantines_the_bench(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The last write of the read is the one that had no consequences.

    The read is recorded while the board is held, the leases come back clean, and
    the record is then re-committed to say `released`. That last write is the one
    tested here, and it used to be the hole: the leases were already gone, so
    nothing was left to quarantine, the coordinator was never told, and the
    refusal returned the pre-failure lease status — `cleanup_required: false`,
    `quarantined: false`, `lease_state: released` — beside a summary saying the
    bench needed an operator. Every field a caller branches on said it was fine,
    and the next hardware call went through.
    """
    workspace, path = _regenerable_bench(tmp_path, monkeypatch)
    before = path.read_bytes()
    # Only the re-commit. `adopt.write_report` is the initial record and is left
    # alone; `report.write_report` is what `recommit_report_with_status` calls.
    monkeypatch.setattr("agentic_hil.report.write_report", lambda config, report: {**report, "audit_ok": False})

    service = AgenticHILToolService(load_authoritative_config(workspace), frontend="mcp")
    try:
        refused = service.call(PROJECT_CONFIG_CREATE)
        assert refused["ok"] is False
        assert refused["error_type"] == "audit_failed_after_action"
        assert refused["cleanup_required"] is True
        assert refused["quarantined"] is True
        assert refused["lease_state"] == "quarantined"
        assert refused["quarantine_id"]
        assert refused["retry_safe"] is False
        assert "agentic-hil recover" in refused["next_step"]
        # And the bench stays shut behind it, which is the whole point of raising
        # the incident on the coordinator rather than only reporting one.
        retried = service.call(PROJECT_CONFIG_CREATE)
        probed = service.call("debugger_probes_list")
    finally:
        service.close()

    assert retried["ok"] is False
    assert retried["error_type"] == "resource_quarantined"
    assert probed["ok"] is False
    assert probed["error_type"] == "resource_quarantined"
    assert path.read_bytes() == before


def test_an_unprovisioned_creator_that_finds_a_configuration_still_leases_the_board(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two unprovisioned servers racing, and the second one loses cleanly.

    Both miss the bind, both call `project_config_create` with no coordinator,
    and the first writes the file. The second then finds that configuration on
    the in-lock reread — the reread exists precisely so it does — and used to
    read the board directly from there: no audit, no lease, straight past the
    `resource_id` holder the file it had just loaded names, and outside the
    quarantine path. "No coordinator" is not "no configuration".

    The call made here is the exact one `UnprovisionedToolService` makes, with
    the configuration already in place: the post-race state, without a thread."""
    workspace, path = _regenerable_bench(tmp_path, monkeypatch, resource_id="bench-a")
    before = path.read_bytes()
    # The direct read is the failure mode. If it is reached at all, say so here
    # rather than inferring it from the file afterwards.
    monkeypatch.setattr(
        "agentic_hil.tools.discover_attached_hardware",
        lambda *args, **kwargs: pytest.fail("an unprovisioned creator read the board without a lease"),
    )

    stranger = BenchMutex(frontend="stranger", label="other-bench-session")
    stranger.acquire([f"physical:{fold_hardware_id('bench-a')}"])
    try:
        refused = project_config_create(workspace, None)
    finally:
        stranger.release_all()

    assert refused["ok"] is False
    assert refused["error_type"] == "device_busy"
    assert refused["holder"]["label"] == "other-bench-session"
    assert refused["side_effect_committed"] is False
    assert path.read_bytes() == before


def test_an_unprovisioned_creator_that_finds_a_configuration_writes_the_audit_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other half: with nobody holding the board, the read is a leased read.

    A refusal alone would be satisfied by a path that simply gave up. This shows
    the racing server goes through the same lifecycle every other caller does —
    the enumeration pseudo-resource, the configured alias and the enumerated
    probe all locked, and the read written to the audit trail."""
    workspace, _ = _regenerable_bench(tmp_path, monkeypatch, resource_id="bench-a")
    monkeypatch.setattr(
        "agentic_hil.tools.discover_attached_hardware",
        lambda *args, **kwargs: pytest.fail("an unprovisioned creator read the board without a lease"),
    )

    regenerated = project_config_create(workspace, None)

    assert regenerated["ok"] is True, regenerated
    report = read_last_report(load_authoritative_config(workspace))
    assert report["tool"] == PROJECT_CONFIG_CREATE
    assert report["resources"] == [
        "debugger-discovery:all",
        f"physical:{fold_hardware_id('bench-a')}",
        f"probe:{fold_hardware_id('STLINK123')}",
    ]
    assert report["lease_state"] == "released"
    assert report["cleanup_required"] is False


# ---------------------------------------------------------------------------
# What a generation says about itself is read out of what it wrote.


def test_a_regeneration_says_which_permissions_it_did_not_grant(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A carried-over narrowing is reported, not papered over.

    Regeneration is creation and the owner's clarification keeps it outside the
    one-way rule, so a regenerated file legitimately holds a mix: the permissions
    of the file it replaced on the entries that were already there, the skeleton's
    open default on anything discovered for the first time. What must not happen
    is a result — or a header a person reads first — that claims one when the
    other was written."""
    workspace = bench(tmp_path, monkeypatch)
    attached_hardware(monkeypatch)
    service = UnprovisionedToolService(workspace)
    try:
        created = service.call(PROJECT_CONFIG_CREATE)
    finally:
        service.close()

    # Nothing was narrowed, and the two flags a generation writes false are not
    # reported as a narrowing: nobody chose them for this bench.
    assert created["narrowed_permissions"] == []
    assert "flash the bench" in created["summary"]
    config_file = Path(created["path"])
    assert "Every permission below is true" in config_file.read_text(encoding="utf-8")

    narrowed = written_document(created)
    narrowed["debuggers"]["dut"]["permissions"]["allow_reset"] = False
    narrowed["artifacts"]["allow_upload"] = False
    config_file.write_text(yaml.safe_dump(narrowed, sort_keys=False), encoding="utf-8")

    attached_hardware(monkeypatch)
    reopened = UnprovisionedToolService(workspace)
    try:
        rewritten = reopened.call(PROJECT_CONFIG_CREATE)
    finally:
        reopened.close()

    assert rewritten["ok"] is True, rewritten
    assert rewritten["narrowed_permissions"] == ["artifacts.allow_upload", "debuggers.dut.permissions.allow_reset"]
    assert "carried over as false from the configuration this server loaded at startup" in rewritten["summary"]
    assert "every permission was carried over unchanged" not in rewritten["summary"]
    header = config_file.read_text(encoding="utf-8")
    assert "2 further permission(s) below are false" in header
    assert "#   debuggers.dut.permissions.allow_reset" in header
    # The two the generation writes false are stated as such and are *not* in
    # that count, so an operator reading the header is not told the bench came
    # back narrower than it did.
    assert "#   debuggers.dut.permissions.allow_mass_erase" not in header
    assert "false so that\n# flashing works" in header
    # And the next steps do not tell the agent to report a bench that is fully
    # granted when it is not.
    assert any("narrowed_permissions" in step for step in rewritten["next_steps"])


def test_the_live_contracts_describe_the_generation_that_actually_runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The server instructions and the tool description are read before the call.

    They are the only statement about this tool most callers ever see, so a
    sentence left over from the closed default is not stale documentation — it is
    a live instruction telling a connected agent that a bench it just generated
    grants nothing, at the moment it in fact grants flashing.

    Since the interlock correction the same applies in the other direction, and it is the
    sharper case: a contract that still said "every permission is true" would be
    telling an agent that the two flags the interlock reads are on, and the
    documented way to make flashing work would be to ask an operator to turn them
    off — advice for a bench that no longer exists, on a file where following it
    changes nothing."""
    workspace = bench(tmp_path, monkeypatch)
    attached_hardware(monkeypatch)
    service = UnprovisionedToolService(workspace)
    try:
        created = service.call(PROJECT_CONFIG_CREATE)
    finally:
        service.close()
    document = written_document(created)
    assert declared(document) - granted(document) == withheld_by_design(document)

    description = next(str(tool["description"]) for tool in MCP_TOOLS if tool["name"] == PROJECT_CONFIG_CREATE)
    for text in (SERVER_INSTRUCTIONS, description):
        assert "every permission in it is true" in text or "Every permission in the generated file is true" in text
        # Both flags named, and named as false: an agent that reads either of
        # these must not go looking for a narrowing that is already made.
        assert "allow_raw_debugger_commands and allow_mass_erase, which are false" in text
        assert "every permission in it is false" not in text
        assert "every write permission in the generated file is false" not in text
    # And what the schema says a provenance record means says it too.
    provenance = config_schema()["properties"]["provenance"]["properties"]["created_by"]["description"]
    assert "every permission true except allow_raw_debugger_commands and allow_mass_erase" in provenance


def test_a_narrowing_and_a_regeneration_in_one_session_put_the_loaded_grant_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The accepted behaviour, pinned as behaviour rather than left to a promise.

    A server parses its configuration once and does not reload, so the `existing`
    a regeneration carries permissions from is what it read at startup — not the
    file as `project_config_set` has since left it. One session that narrows and
    then regenerates therefore writes the wider loaded value back.

    That is not a bug to close. The owner's clarification puts regeneration
    outside the ratchet: it is creation, it belongs to the operator, and the rule
    that holds is that the *MCP write path* can only narrow. What is defended
    here is that every contract a caller reads says so, because a contract
    promising that a narrowing survives a regeneration would be describing a
    boundary that is not there."""
    workspace = bench(tmp_path, monkeypatch)
    attached_hardware(monkeypatch)
    provisioner = UnprovisionedToolService(workspace)
    try:
        created = provisioner.call(PROJECT_CONFIG_CREATE)
    finally:
        provisioner.close()
    assert created["ok"] is True, created
    path = Path(created["path"])

    # One service, one session: narrow, then regenerate, with no restart between.
    # allow_reset and not allow_mass_erase, because the narrowing has to be of a
    # flag a generation leaves true, or the regeneration below puts nothing back
    # and this passes without exercising anything.
    service = AgenticHILToolService(load_authoritative_config(workspace), frontend="mcp")
    try:
        narrowed = service.call(PROJECT_CONFIG_SET, {"changes": [{"key": "debuggers.dut.permissions.allow_reset", "value": False}]})
        assert narrowed["ok"] is True, narrowed
        assert written_document({"path": str(path)})["debuggers"]["dut"]["permissions"]["allow_reset"] is False

        attached_hardware(monkeypatch)
        regenerated = service.call(PROJECT_CONFIG_CREATE)
    finally:
        service.close()

    assert regenerated["ok"] is True, regenerated
    assert written_document(regenerated)["debuggers"]["dut"]["permissions"]["allow_reset"] is True
    assert regenerated["narrowed_permissions"] == []
    # And the result says which state it wrote from, so nobody reads the reopened
    # grant as a promise that was broken.
    assert "this server loaded at startup" in regenerated["summary"]
    assert any("since this server started" in step for step in regenerated["next_steps"])


def test_every_live_contract_says_regeneration_carries_the_loaded_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """"The permissions already on disk" was a promise this path does not keep.

    Each of these is served to a caller before or during the call, and each said
    a regeneration carries the permissions on disk over — which a caller can only
    read as "my narrowing survives this". It does not; the loaded configuration
    is what is carried. Every one of them has to say the true thing, because a
    caller that believes the false one regenerates to refresh a probe id and
    reopens the bench."""
    workspace = bench(tmp_path, monkeypatch)
    attached_hardware(monkeypatch)
    service = UnprovisionedToolService(workspace)
    try:
        created = service.call(PROJECT_CONFIG_CREATE)
    finally:
        service.close()

    description = next(str(tool["description"]) for tool in MCP_TOOLS if tool["name"] == PROJECT_CONFIG_CREATE)
    schema = config_schema()["properties"]["permissions"]
    served = {
        "tool description": description,
        "config-shape resource": read_resource(CONFIG_SHAPE_URI)["text"],
        "permissions schema": schema["description"],
        "allow_config_write schema": schema["properties"]["allow_config_write"]["description"],
        "provenance schema": config_schema()["properties"]["provenance"]["properties"]["created_by"]["description"],
        "generated header": Path(created["path"]).read_text(encoding="utf-8"),
    }
    for name, text in served.items():
        assert "loaded" in text, f"{name} must say the carried permissions are the loaded ones"
        assert "permissions already on disk" not in text, f"{name} still promises the on-disk state"
        assert "permissions on disk over" not in text, f"{name} still promises the on-disk state"
