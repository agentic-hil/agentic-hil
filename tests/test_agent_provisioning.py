"""One-time agent provisioning of a project configuration.

The invariant every test here circles: an agent can enable itself up to
observation, never up to modification. Reading needs no permission under
decision 0018, so the configuration an agent generates for itself must reach
exactly that far — every write permission false, including the one that would
let it write the file again.

The ratchet is the configuration. There is no second state store, and the tests
below say why that is enough: the agent cannot choose what it generates, so a
delete-and-regenerate cycle yields the same restrictive file. What that costs is
a human's customised permissions, which is a downgrade, not an escalation.
"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest
import yaml

from agentic_hil.config import (
    load_authoritative_config,
    project_config_path,
    provisionable_state_root,
    user_state_root,
)
from agentic_hil.configwrite import PROJECT_CONFIG_DESCRIBE, PROJECT_CONFIG_SET
from agentic_hil.contracts import MCP_TOOL_NAMES, TOOL_SCHEMAS
from agentic_hil.mcp import handle_mcp_message
from agentic_hil.tools import PROJECT_CONFIG_CREATE, AgenticHILToolService, UnprovisionedToolService

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
    monkeypatch.setattr("agentic_hil.tools.discover_attached_hardware", lambda: discovery)
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
        assert document["version"] == 2
        # The session that generated it can read the bench through it, without a
        # restart nothing would have told the host about.
        assert service.config is not None
        assert service.config.debugger is not None
        assert service.config.debugger.probe_id == "STLINK123"
    finally:
        service.close()


def test_generated_configuration_grants_nothing_at_all(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = bench(tmp_path, monkeypatch)
    attached_hardware(monkeypatch)
    service = UnprovisionedToolService(workspace)
    try:
        result = service.call(PROJECT_CONFIG_CREATE)
        document = written_document(result)

        assert document["permissions"] == {
            "allow_config_write": False,
            "allow_config_description_write": False,
            "allow_config_permissions_write": False,
        }
        assert document["debuggers"]["dut"]["permissions"] == {
            "allow_flash": False,
            "allow_reset": False,
            "allow_raw_debugger_commands": False,
            "allow_mass_erase": False,
        }
        assert document["com_ports"]["dut_uart"]["permissions"] == {"allow_write": False}
        assert document["artifacts"]["allow_upload"] is False
        assert document["debug"]["allow_all_symbols"] is False
        # Nothing anywhere in the file is granted, whatever it is called.
        assert granted(document) == set()

        # And the file it wrote is what refuses the next write.
        again = service.call(PROJECT_CONFIG_CREATE)

        assert again["error_type"] == "permission_denied"
        assert again["permission"] == "allow_config_write"
        assert again["reason"] == "config_write_denied"
        assert written_document(result) == document
    finally:
        service.close()


def test_generated_configuration_ignores_permissions_the_workspace_asks_for(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A profile in the repository must not decide what the board may be told to do.

    `agentic-hil init` fills a configuration from this file on purpose: a person
    ran it. Over MCP the same file is repository-controlled data, and honouring
    its permission block would turn "generate once" into complete
    self-authorization — write the profile, then ask for the configuration.
    """
    workspace = bench(tmp_path, monkeypatch)
    (workspace / "agentic-hil.config.example.yaml").write_text(
        "target:\n"
        "  name: demo\n"
        "  controller: stm32f446ret6\n"
        "debuggers:\n"
        "  dut:\n"
        "    permissions:\n"
        "      allow_flash: true\n"
        "      allow_reset: true\n"
        "      allow_mass_erase: true\n"
        "com_ports:\n"
        "  uart:\n"
        "    permissions:\n"
        "      allow_write: true\n",
        encoding="utf-8",
    )
    attached_hardware(monkeypatch)
    service = UnprovisionedToolService(workspace)
    try:
        result = service.call(PROJECT_CONFIG_CREATE)

        assert granted(written_document(result)) == set()
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
        # Provenance is a note to a reader, never policy: what gates the next
        # write is the permission beside it, not what the file says wrote it.
        assert load_authoritative_config(workspace).permissions.allow_config_write is False
    finally:
        service.close()


def test_deleting_the_configuration_lets_the_agent_generate_the_same_one_again(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The trade the config-resident ratchet makes, stated as a test.

    A configuration removed out of band puts the workspace back in the state it
    started from, and an agent may generate one again. That is a downgrade, not
    an escalation: the agent cannot choose what it generates, so what comes back
    is byte for byte the same deny-by-default skeleton, minus whatever a human
    had opened in the file that was deleted. The cycle yields no capability.
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

    # A person opens what their project needs, then the file is lost.
    opened = deepcopy(original)
    opened["debuggers"]["dut"]["permissions"]["allow_flash"] = True
    config_file.write_text(yaml.safe_dump(opened, sort_keys=False), encoding="utf-8")
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
    # No capability was gained: the second file grants exactly as little as the
    # first, and the flash permission the person had set is simply gone.
    assert granted(regenerated) == set()
    assert regenerated["debuggers"]["dut"]["permissions"]["allow_flash"] is False
    assert regenerated["permissions"] == {
        "allow_config_write": False,
        "allow_config_description_write": False,
        "allow_config_permissions_write": False,
    }
    assert {key: value for key, value in regenerated.items() if key != "provenance"} == {
        key: value for key, value in original.items() if key != "provenance"
    }


def test_a_running_server_may_not_replace_the_configuration_it_lost(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A server that holds a policy is gated by it, file present or not.

    Otherwise deleting the file underneath a running server would be a way to
    turn its own refusal into a fresh, writable start.
    """
    workspace = bench(tmp_path, monkeypatch)
    attached_hardware(monkeypatch)
    service = UnprovisionedToolService(workspace)
    try:
        created = service.call(PROJECT_CONFIG_CREATE)
        Path(created["path"]).unlink()

        refused = service.call(PROJECT_CONFIG_CREATE)
    finally:
        service.close()

    assert refused["error_type"] == "permission_denied"
    assert refused["permission"] == "allow_config_write"
    assert not Path(created["path"]).exists(), "a refused call must not write anything"


def test_the_tool_surface_cannot_take_the_configuration_away() -> None:
    """Nothing here removes or moves a configuration.

    Regenerating one costs a human their settings, so the surface offers no way
    to bring that about: no delete, no move, no path argument anywhere. The tool
    that regenerates the file takes no arguments at all, and the one that changes
    it takes named keys with scalar values — never a document, never a path.
    """
    assert sorted(name for name in MCP_TOOL_NAMES if "config" in name) == [
        PROJECT_CONFIG_CREATE,
        PROJECT_CONFIG_DESCRIBE,
        PROJECT_CONFIG_SET,
    ]
    assert not [name for name in MCP_TOOL_NAMES if any(verb in name for verb in ("delete", "remove", "move", "rename"))]
    assert TOOL_SCHEMAS[PROJECT_CONFIG_CREATE] == {"type": "object", "properties": {}, "additionalProperties": False}
    assert TOOL_SCHEMAS[PROJECT_CONFIG_DESCRIBE] == {"type": "object", "properties": {}, "additionalProperties": False}
    change = TOOL_SCHEMAS[PROJECT_CONFIG_SET]["properties"]["changes"]["items"]
    assert sorted(change["properties"]) == ["key", "value"]
    assert change["additionalProperties"] is False
    # No object and no array: a subtree is content the agent authored, and a
    # subtree can carry a permissions: block inside it.
    assert set(change["properties"]["value"]["type"]) == {"string", "number", "integer", "boolean", "null"}


def test_a_person_flipping_the_flag_lets_the_agent_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = bench(tmp_path, monkeypatch)
    attached_hardware(monkeypatch)
    service = UnprovisionedToolService(workspace)
    try:
        created = service.call(PROJECT_CONFIG_CREATE)
    finally:
        service.close()

    config_file = Path(created["path"])
    opened = written_document(created)
    opened["permissions"]["allow_config_write"] = True
    opened["debuggers"]["dut"]["permissions"]["allow_flash"] = True
    config_file.write_text(yaml.safe_dump(opened, sort_keys=False), encoding="utf-8")

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
    # … and every permission is the one the person set. The agent contributes no
    # permission value on this path either.
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


def test_a_configured_server_refuses_to_write_its_own_configuration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The ordinary server carries the same refusal.

    A project that was set up by a person is the common case, and the tool is
    advertised there too — it has to answer with the permission it is denied by,
    not by being absent from the list.
    """
    workspace = bench(tmp_path, monkeypatch)
    attached_hardware(monkeypatch)
    provisioner = UnprovisionedToolService(workspace)
    try:
        created = provisioner.call(PROJECT_CONFIG_CREATE)
    finally:
        provisioner.close()

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
    """Issue #64: the documented default is refused on a stock Windows profile.

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


def test_a_refused_config_location_returns_the_actionable_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Where no location can be picked, the refusal has to carry the way out.

    The configuration's own location is fixed by discovery rules, so unlike
    state_root there is nothing to fall back to. What the caller gets is the
    error type and the remediation the CLI returns for the same wall.
    """
    workspace = bench(tmp_path, monkeypatch)
    attached_hardware(monkeypatch)
    target = project_config_path(workspace)
    target.parent.parent.mkdir(parents=True, exist_ok=True)
    target.parent.write_text("a file where the configuration directory belongs", encoding="utf-8")
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
        lambda: {
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
        assert result["structuredContent"]["permissions"]["allow_config_write"] is False

        denied = handle_mcp_message(
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": PROJECT_CONFIG_CREATE, "arguments": {}}},
            service,
        )
        assert isinstance(denied, dict)
        assert denied["result"]["isError"] is True
        assert denied["result"]["structuredContent"]["error_type"] == "permission_denied"
    finally:
        service.close()


def test_default_state_root_is_still_preferred_when_it_passes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The fallback is a fallback, not a relocation of every profile's state."""
    workspace = bench(tmp_path, monkeypatch)

    assert provisionable_state_root(workspace) == user_state_root()
