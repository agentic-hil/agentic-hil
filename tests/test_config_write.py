"""Field-wise configuration writes, and the boundary the two grants draw.

The invariant #71 established was that an agent can enable itself up to
observation and never up to modification. hardci-hq#80 opens a door in that wall
and the owner decided the shape of it: **two** grants, not one. A single
``allow_config_write`` would have been a master key — set to let an agent enter a
probe serial, it would have handed over, in the same motion and without saying
so, the ability for that agent to write ``allow_flash: true`` on itself.

So the tests here are mostly about what the description grant cannot reach. The
one that matters most is the last of the detour group: it breaks the key model on
purpose and shows the write is still refused, because the boundary is defended by
a comparison of the document as well as by a reading of the request. A check that
only inspected the request would be exactly as good as the parser is right.
"""

from __future__ import annotations

import json
import os
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
import yaml
from conftest import write_authoritative_config

from agentic_hil.config import SURVIVING_SECTION_KEYS, config_schema, load_authoritative_config
from agentic_hil.configwrite import (
    PROJECT_CONFIG_DESCRIBE,
    PROJECT_CONFIG_SET,
    description_view,
    permission_delta,
    permission_surface,
    project_config_set,
)
from agentic_hil.contracts import TOOL_SCHEMAS
from agentic_hil.knowledge import (
    CONFIG_DESCRIPTION_RIGHT,
    CONFIG_KEY_RULES,
    CONFIG_PERMISSIONS_RIGHT,
    CONFIG_SHAPE_URI,
    CONFIG_WRITE_RIGHT,
    ResolvedConfigKey,
    config_key_catalogue,
    config_key_schema,
    config_rule_fields,
    read_resource,
    remediation_fields,
    resolve_config_key,
)
from agentic_hil.mcp import handle_mcp_message
from agentic_hil.tools import AgenticHILToolService

COM_PORTS = 'com_ports:\n  dut_uart:\n    device: "COM9"\n    baudrate: 115200\n'
CAN_BUSES = 'can_buses:\n  body:\n    adapter: "peak"\n    channel: "PCAN_USBBUS1"\n'
PROVENANCE = {"created_by": "agent", "created_via": "mcp:project_config_create", "created_at": "2026-01-01T00:00:00Z", "agentic_hil_version": "0.0.0"}


def bench(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, config_root: Path | None = None, **grants: bool) -> tuple[Path, Path]:
    """A configuration with one probe, one port and one CAN bus, opened as asked.

    The configuration goes under the sandbox `isolated_config_environment` set up
    rather than under `tmp_path`: on Windows the per-user Temp ACL is replaceable,
    so a policy file there is refused by the ancestor check before any of this
    gets a chance to run. Every device grant starts false, which is the state a
    generated configuration actually has."""
    workspace = (tmp_path / "workspace").resolve()
    root = config_root or Path(os.environ["APPDATA"]) / "config-write"
    path = write_authoritative_config(workspace, monkeypatch, config_root=root, config_version=2, permissions={}, com_ports_yaml=COM_PORTS, can_buses_yaml=CAN_BUSES)
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    document["provenance"] = dict(PROVENANCE)
    document["permissions"] = {CONFIG_WRITE_RIGHT: False, CONFIG_DESCRIPTION_RIGHT: False, CONFIG_PERMISSIONS_RIGHT: False, **grants}
    path.write_text("# A person wrote this bench.\n" + yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    monkeypatch.chdir(workspace)
    return workspace, path


def service(workspace: Path) -> AgenticHILToolService:
    return AgenticHILToolService(load_authoritative_config(workspace), frontend="mcp")


def document_of(path: Path) -> dict:
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def changes(*pairs: tuple[str, Any]) -> dict:
    return {"changes": [{"key": key, "value": value} for key, value in pairs]}


def sample_value(entry: dict) -> Any:
    """A value this key would accept, taken from the file or from the schema.

    Never invented: the current value already loaded, and a schema default is the
    schema's own statement about what belongs there."""
    if entry.get("current_value") is not None:
        return entry["current_value"]
    shape = dict(entry["value_schema"])
    if "default" in shape:
        return shape["default"]
    declared = shape.get("type")
    kinds = declared if isinstance(declared, list) else [declared]
    if "integer" in kinds or "number" in kinds:
        return shape.get("minimum", 1)
    return False if "boolean" in kinds else "x"


# ---------------------------------------------------------------------------
# Each grant alone opens exactly its own half.


def test_the_description_grant_alone_opens_the_description_and_nothing_else(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace, path = bench(tmp_path, monkeypatch, **{CONFIG_DESCRIPTION_RIGHT: True})
    tools = service(workspace)
    try:
        described = tools.call(PROJECT_CONFIG_DESCRIBE)
        assert {entry["right"] for entry in described["writable_keys"]} == {CONFIG_DESCRIPTION_RIGHT}
        assert {entry["right"] for entry in described["locked_keys"]} == {CONFIG_PERMISSIONS_RIGHT}

        # Every key the answer calls open really is, in one all-or-nothing call.
        opened = tools.call(PROJECT_CONFIG_SET, changes(*((entry["key"], sample_value(entry)) for entry in described["writable_keys"])))
        assert opened["ok"] is True, opened
        assert sorted(item["key"] for item in opened["changes"]) == sorted(entry["key"] for entry in described["writable_keys"])

        # And every key it calls locked really is, naming the grant that is missing.
        locked = tools.call(PROJECT_CONFIG_SET, changes(*((entry["key"], False) for entry in described["locked_keys"])))
        assert locked["error_type"] == "permission_denied"
        assert locked["permission"] == CONFIG_PERMISSIONS_RIGHT
        assert locked["denied_keys"] == sorted(entry["key"] for entry in described["locked_keys"])
    finally:
        tools.close()
    assert document_of(path)["permissions"][CONFIG_PERMISSIONS_RIGHT] is False


def test_a_reserved_looking_entry_id_is_writable_on_the_description_grant_alone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The documented split, checked on the ids that look like the other half.

    `permissions`, `provenance` and `allow_anything` are legal debugger, COM and
    CAN entry ids, and the permission surface read every one of them as a grant:
    changing `debuggers.permissions.probe_id` produced a permission delta named
    after the entry, so `project_config_describe` offered the key as
    description-writable and `project_config_set` then demanded
    `allow_config_permissions_write` for it. The entry was unusable under the
    split the tool documents.

    End to end rather than through the projection, because the projection was
    already right and the write was not."""
    workspace, path = bench(tmp_path, monkeypatch, **{CONFIG_DESCRIPTION_RIGHT: True})
    document = document_of(path)
    document["debuggers"] = {"permissions": document["debuggers"]["dut"]}
    document["com_ports"] = {"provenance": document["com_ports"]["dut_uart"]}
    document["can_buses"] = {"allow_anything": document["can_buses"]["body"]}
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")

    tools = service(workspace)
    try:
        described = tools.call(PROJECT_CONFIG_DESCRIBE)
        keys = {"debuggers.permissions.probe_id": "AAA0001", "com_ports.provenance.baudrate": 9600, "can_buses.allow_anything.channel": "PCAN_USBBUS2"}
        # Each is offered on the description grant, which is the promise the write
        # then has to keep.
        offered = {entry["key"]: entry["right"] for entry in described["writable_keys"]}
        assert {key: offered.get(key) for key in keys} == dict.fromkeys(keys, CONFIG_DESCRIPTION_RIGHT)

        applied = tools.call(PROJECT_CONFIG_SET, changes(*keys.items()))
        assert applied["ok"] is True, applied
    finally:
        tools.close()

    after = document_of(path)
    assert after["debuggers"]["permissions"]["probe_id"] == "AAA0001"
    assert after["com_ports"]["provenance"]["baudrate"] == 9600
    assert after["can_buses"]["allow_anything"]["channel"] == "PCAN_USBBUS2"
    # The entries' own grants are untouched, and still need the other right.
    assert after["debuggers"]["permissions"]["permissions"] == document["debuggers"]["permissions"]["permissions"]
    tools = service(workspace)
    try:
        refused = tools.call(PROJECT_CONFIG_SET, changes(("debuggers.permissions.permissions.allow_flash", True)))
    finally:
        tools.close()
    assert refused["error_type"] == "permission_denied"
    assert refused["permission"] == CONFIG_PERMISSIONS_RIGHT


def test_the_permissions_grant_alone_opens_the_permissions_and_nothing_else(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace, path = bench(tmp_path, monkeypatch, **{CONFIG_PERMISSIONS_RIGHT: True})
    tools = service(workspace)
    try:
        described = tools.call(PROJECT_CONFIG_DESCRIBE)
        assert {entry["right"] for entry in described["writable_keys"]} == {CONFIG_PERMISSIONS_RIGHT}
        assert {entry["right"] for entry in described["locked_keys"]} == {CONFIG_DESCRIPTION_RIGHT}

        granted = tools.call(PROJECT_CONFIG_SET, changes(("debuggers.dut.permissions.allow_flash", True)))
        assert granted["ok"] is True, granted
        assert granted["permissions_changed"] == ["debuggers.dut.permissions.allow_flash"]

        refused = tools.call(PROJECT_CONFIG_SET, changes(("debuggers.dut.probe_id", "066AFF49")))
        assert refused["error_type"] == "permission_denied"
        assert refused["permission"] == CONFIG_DESCRIPTION_RIGHT
        assert refused["denied_keys"] == ["debuggers.dut.probe_id"]
    finally:
        tools.close()
    document = document_of(path)
    assert document["debuggers"]["dut"]["permissions"]["allow_flash"] is True
    assert document["debuggers"]["dut"]["probe_id"] != "066AFF49"


def test_neither_grant_leaves_nothing_open_and_says_which_grant_would_open_what(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace, path = bench(tmp_path, monkeypatch)
    before = path.read_bytes()
    tools = service(workspace)
    try:
        described = tools.call(PROJECT_CONFIG_DESCRIBE)
        assert described["writable_keys"] == []
        assert described["locked_keys"]
        assert {entry["unlocked_by"] for entry in described["locked_keys"]} == {
            f"permissions.{CONFIG_DESCRIPTION_RIGHT}",
            f"permissions.{CONFIG_PERMISSIONS_RIGHT}",
        }

        refused = tools.call(PROJECT_CONFIG_SET, changes(("target.name", "renamed")))
        assert refused["permission"] == CONFIG_DESCRIPTION_RIGHT
        assert refused["permission_key"] == f"permissions.{CONFIG_DESCRIPTION_RIGHT}"
        assert refused["path"] == str(path)
    finally:
        tools.close()
    assert path.read_bytes() == before, "a refusal must not write anything"


# ---------------------------------------------------------------------------
# The detour. This is the group the owner asked to see.


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("debuggers.dut.permissions.allow_flash", True),
        ("debuggers.dut.permissions.allow_mass_erase", True),
        ("com_ports.dut_uart.permissions.allow_write", True),
        ("permissions.allow_config_permissions_write", True),
        ("permissions.allow_config_write", True),
    ],
)
def test_the_description_grant_cannot_widen_a_permission_directly(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, key: str, value: bool) -> None:
    workspace, path = bench(tmp_path, monkeypatch, **{CONFIG_DESCRIPTION_RIGHT: True})
    before = path.read_bytes()
    tools = service(workspace)
    try:
        refused = tools.call(PROJECT_CONFIG_SET, changes((key, value)))
    finally:
        tools.close()

    assert refused["error_type"] == "permission_denied"
    assert refused["permission"] == CONFIG_PERMISSIONS_RIGHT
    assert refused["denied_keys"] == [key]
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    ("key", "value"),
    [
        # A whole entry, carrying a permissions block inside it.
        ("debuggers.dut", {"permissions": {"allow_flash": True}}),
        # The permissions block itself, as one value.
        ("debuggers.dut.permissions", {"allow_flash": True}),
        # A whole section.
        ("debuggers", {"dut": {"permissions": {"allow_flash": True}}}),
        ("permissions", {"allow_config_permissions_write": True}),
        # And the same idea wearing a description key's name.
        ("target", {"name": "x", "permissions": {"allow_flash": True}}),
        ("can_buses.body", {"channel": "c", "permissions": {"allow_write": True}}),
    ],
)
def test_a_subtree_is_not_a_value_this_tool_accepts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, key: str, value: object) -> None:
    """The detour the owner named: replace a subtree that contains permissions.

    It never gets as far as a permission check, because there is no shape of this
    request the tool takes. `value` admits a scalar and nothing else, and no key
    that names a subtree resolves — both halves are asserted here, because either
    one alone would leave the other free to change.
    """
    workspace, path = bench(tmp_path, monkeypatch, **{CONFIG_DESCRIPTION_RIGHT: True})
    before = path.read_bytes()
    tools = service(workspace)
    try:
        refused = tools.call(PROJECT_CONFIG_SET, changes((key, value)))
        over_mcp = handle_mcp_message(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": PROJECT_CONFIG_SET, "arguments": changes((key, value))}},
            tools,
        )
    finally:
        tools.close()

    assert refused["error_type"] == "invalid_argument"
    assert isinstance(over_mcp, dict) and over_mcp["result"]["isError"] is True
    # The value never had a shape this tool takes …
    assert set(TOOL_SCHEMAS[PROJECT_CONFIG_SET]["properties"]["changes"]["items"]["properties"]["value"]["type"]) == {"string", "number", "integer", "boolean", "null"}
    # … and the key names nothing settable either.
    assert resolve_config_key(key) is None
    assert path.read_bytes() == before


def test_the_boundary_holds_even_when_the_key_model_is_wrong(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The reason there are two checks and not one.

    The key model is a reading of the request, and a boundary defended only by a
    reading of the request is defended exactly as well as that reading is right.
    So the model is broken here on purpose — told that a permission key belongs to
    the description grant — and the write must still be refused, because the
    permissions actually present in the document are compared before against
    after. This is the check that closes a detour nobody has found yet.
    """
    workspace, path = bench(tmp_path, monkeypatch, **{CONFIG_DESCRIPTION_RIGHT: True})
    mislabelled = ResolvedConfigKey(
        key="debuggers.dut.permissions.allow_flash",
        section="debuggers",
        entry="dut",
        field="allow_flash",
        under_permissions=True,
        # The lie: a permissions key claiming to belong to the description half.
        right=CONFIG_DESCRIPTION_RIGHT,
        pattern="debuggers.<name>.permissions.<flag>",
    )
    monkeypatch.setattr("agentic_hil.configwrite.resolve_config_key", lambda key: mislabelled if key == mislabelled.key else resolve_config_key(key))
    before = path.read_bytes()

    refused = project_config_set(workspace, load_authoritative_config(workspace), [{"key": mislabelled.key, "value": True}])

    assert refused["error_type"] == "permission_denied"
    assert refused["permission"] == CONFIG_PERMISSIONS_RIGHT
    assert refused["denied_keys"] == ["debuggers.dut.permissions.allow_flash"]
    assert path.read_bytes() == before


def test_the_document_comparison_sees_a_permission_wherever_it_sits() -> None:
    """What the second check actually looks at.

    Not the paths the key model can produce — every `allow_*` key at any depth,
    and every leaf of any mapping called `permissions`. A guard that only knew
    the model's own paths would be blind exactly where something that got past
    the model would land."""
    nested = {"debuggers": {"dut": {"permissions": {"allow_flash": False}}}, "artifacts": {"allow_upload": False}, "odd": [{"permissions": {"allow_write": False}}]}
    surface = permission_surface(nested)

    assert surface == {
        "debuggers.dut.permissions.allow_flash": False,
        "artifacts.allow_upload": False,
        "odd[0].permissions.allow_write": False,
    }
    widened = {"debuggers": {"dut": {"permissions": {"allow_flash": True}}}, "artifacts": {"allow_upload": False}, "odd": [{"permissions": {"allow_write": False}}]}
    assert permission_delta(surface, permission_surface(widened)) == ["debuggers.dut.permissions.allow_flash"]
    # An entry that appears carrying nothing but denials is not a change.
    added = {**nested, "com_ports": {"new": {"permissions": {"allow_write": False}}}}
    assert permission_delta(surface, permission_surface(added)) == []
    # The mirror view drops the grants the schema puts somewhere, and only those.
    assert permission_surface(description_view({"debuggers": {"dut": {"permissions": {"allow_flash": False}}}, "artifacts": {"allow_upload": False}})) == {}
    # A grant somewhere the schema does not put one survives into the description
    # view on purpose, so it needs the description right *and* the permissions
    # right rather than neither. Dropping it by name is what made an entry an
    # operator called `permissions` invisible to the compare-and-swap.
    assert permission_surface(description_view(nested)) == {"odd[0].permissions.allow_write": False}
    reserved = {"debuggers": {"permissions": {"probe_id": "AAA", "type": "stlink", "permissions": {"allow_flash": False}}}}
    assert description_view(reserved) == {"debuggers": {"permissions": {"probe_id": "AAA", "type": "stlink"}}}
    repointed = {"debuggers": {"permissions": {"probe_id": "BBB", "type": "stlink", "permissions": {"allow_flash": False}}}}
    assert description_view(reserved) != description_view(repointed), "a repointed entry is visible whatever it is named"


def test_an_entry_id_that_looks_like_a_grant_is_read_as_the_id_it_is() -> None:
    """The one level where this walk has to know the schema.

    Directly under `debuggers`, `com_ports` and `can_buses` the keys are not
    field names — they are entry ids the operator chose, and `permissions`,
    `provenance` and `allow_anything` are all legal ones. Reading them as grant
    names made every field of such an entry a permission: repointing
    `debuggers.permissions` at another board produced the delta
    `debuggers.permissions.probe_id` and demanded
    `allow_config_permissions_write`, while `project_config_describe` went on
    offering the same key as description-writable. The entry's real grants sit one
    level further down, where the schema puts them, and are still found there."""
    named = {
        "debuggers": {
            "permissions": {"probe_id": "AAA", "type": "stlink", "permissions": {"allow_flash": False}},
            "allow_anything": {"probe_id": "BBB", "type": "stlink", "permissions": {"allow_flash": False}},
        }
    }

    assert permission_surface(named) == {
        "debuggers.permissions.permissions.allow_flash": False,
        "debuggers.allow_anything.permissions.allow_flash": False,
    }
    repointed = deepcopy(named)
    repointed["debuggers"]["permissions"]["probe_id"] = "CCC"
    repointed["debuggers"]["allow_anything"]["probe_id"] = "DDD"
    assert permission_delta(permission_surface(named), permission_surface(repointed)) == []
    # And the entries' own grants are still grants, at the depth the schema uses.
    widened = deepcopy(named)
    widened["debuggers"]["allow_anything"]["permissions"]["allow_flash"] = True
    assert permission_delta(permission_surface(named), permission_surface(widened)) == ["debuggers.allow_anything.permissions.allow_flash"]
    # An entry is a mapping in every one of these sections, so a *scalar* under an
    # id level is not an entry that happens to be called `allow_flash`. That is
    # the shape this walk exists to catch, and narrowing the rule to entry ids
    # must not stop it catching it.
    assert permission_surface({"debuggers": {"allow_flash": True}}) == {"debuggers.allow_flash": True}


def test_a_new_device_an_agent_adds_arrives_granted_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """#76's case: hardware attached after init. It may be described, not empowered."""
    workspace, path = bench(tmp_path, monkeypatch, **{CONFIG_DESCRIPTION_RIGHT: True})
    tools = service(workspace)
    try:
        added = tools.call(PROJECT_CONFIG_SET, changes(("com_ports.probe_vcp.device", "COM11"), ("com_ports.probe_vcp.baudrate", 230400)))
    finally:
        tools.close()

    assert added["ok"] is True, added
    assert added["created_entries"] == ["com_ports.probe_vcp"]
    entry = document_of(path)["com_ports"]["probe_vcp"]
    assert entry["device"] == "COM11"
    assert entry["permissions"] == {"allow_write": False}
    assert added["permissions_changed"] == []


def test_a_permission_does_not_bring_a_device_into_existence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Only a description key creates an entry.

    Otherwise `debuggers.ghost.permissions.allow_flash` would conjure a probe
    that is not on the bench, and the refusal that followed would be about the
    wrong thing."""
    workspace, path = bench(tmp_path, monkeypatch, **{CONFIG_DESCRIPTION_RIGHT: True, CONFIG_PERMISSIONS_RIGHT: True})
    before = path.read_bytes()
    tools = service(workspace)
    try:
        refused = tools.call(PROJECT_CONFIG_SET, changes(("debuggers.ghost.permissions.allow_flash", True)))
    finally:
        tools.close()

    assert refused["error_type"] == "invalid_argument"
    assert refused["unknown_entry"] == "debuggers.ghost"
    assert path.read_bytes() == before


def test_a_new_device_still_needs_the_field_its_schema_requires(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace, path = bench(tmp_path, monkeypatch, **{CONFIG_DESCRIPTION_RIGHT: True})
    before = path.read_bytes()
    tools = service(workspace)
    try:
        refused = tools.call(PROJECT_CONFIG_SET, changes(("can_buses.chassis.bitrate", 250000)))
    finally:
        tools.close()

    assert refused["error_type"] == "invalid_argument"
    assert refused["missing_keys"] == ["can_buses.chassis.channel"]
    assert path.read_bytes() == before


# ---------------------------------------------------------------------------
# No writing into an open run.


def test_a_configuration_write_is_refused_while_a_run_holds_the_bench(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The mirror image of "a run may only touch what it declared".

    Those holds were taken under this file's policy. Changing it underneath them
    moves the rules during the run they govern."""
    workspace, path = bench(tmp_path, monkeypatch, **{CONFIG_DESCRIPTION_RIGHT: True})
    tools = service(workspace)
    try:
        started = tools.call("bench_run_start", {"devices": [{"kind": "uart", "id": "dut_uart"}], "label": "boot-smoke"})
        assert started["ok"] is True, started
        before = path.read_bytes()

        refused = tools.call(PROJECT_CONFIG_SET, changes(("target.name", "renamed")))
        described = tools.call(PROJECT_CONFIG_DESCRIBE)

        assert refused["error_type"] == "config_write_in_open_run"
        assert refused["open_holds"]["run_active"] is True
        assert refused["open_holds"]["run_label"] == "boot-smoke"
        assert refused["remediation"] == remediation_fields("config_write_in_open_run")["remediation"]
        assert any("bench_run_stop" in step for step in refused["remediation"])
        assert refused["retry_safe"] is True
        assert path.read_bytes() == before
        # The rights-aware answer says the same thing rather than promising a
        # write that would be refused.
        assert described["writes_blocked_by_open_run"] is True
        assert any("bench_run_stop" in step for step in described["next_steps"])

        assert tools.call("bench_run_stop")["ok"] is True
        assert tools.call(PROJECT_CONFIG_SET, changes(("target.name", "renamed")))["ok"] is True
    finally:
        tools.close()
    assert document_of(path)["target"]["name"] == "renamed"


def test_a_session_that_outlives_its_run_still_blocks_a_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A COM session keeps its device after `bench_run_stop`, by design.

    Asking only whether a run is open would miss exactly that: the board is still
    held and the policy would still move underneath it."""
    workspace, _ = bench(tmp_path, monkeypatch, **{CONFIG_DESCRIPTION_RIGHT: True})
    tools = service(workspace)
    try:
        assert tools.open_hardware_holds() is None
        lease = tools.coordinator.acquire("com:COM9")
        try:
            holds = tools.open_hardware_holds()
            assert holds is not None
            assert holds["open_leases"] == [lease.lease_id]
            assert holds["run_active"] is False

            refused = tools.call(PROJECT_CONFIG_SET, changes(("target.name", "renamed")))
            assert refused["error_type"] == "config_write_in_open_run"
        finally:
            lease.release()
        assert tools.call(PROJECT_CONFIG_SET, changes(("target.name", "renamed")))["ok"] is True
    finally:
        tools.close()


# ---------------------------------------------------------------------------
# Validate before replacing.


def test_a_value_the_shipped_schema_refuses_never_reaches_the_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace, path = bench(tmp_path, monkeypatch, **{CONFIG_DESCRIPTION_RIGHT: True})
    before = path.read_bytes()
    tools = service(workspace)
    try:
        refused = tools.call(PROJECT_CONFIG_SET, changes(("com_ports.dut_uart.baudrate", "as fast as it goes")))
    finally:
        tools.close()

    assert refused["error_type"] == "invalid_argument"
    assert refused["field"] == "changes.0.value"
    # The shape it names is the schema's own node, not a sentence written here.
    assert refused["value_schema"] == config_key_schema(_rule("com_ports", under_permissions=False), "baudrate")
    assert path.read_bytes() == before


def test_a_change_that_would_make_the_file_invalid_does_not_destroy_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Passes the schema, fails the authoritative rules. The valid file survives.

    An executable inside the workspace is exactly that: a string, so the schema
    takes it, and refused at load, because repository content must not be able to
    name the program that drives the board."""
    workspace, path = bench(tmp_path, monkeypatch, **{CONFIG_DESCRIPTION_RIGHT: True})
    before = path.read_bytes()
    inside = workspace / "tools" / "openocd.exe"
    inside.parent.mkdir(parents=True, exist_ok=True)
    inside.write_text("#!/bin/sh\n", encoding="utf-8")
    tools = service(workspace)
    try:
        refused = tools.call(PROJECT_CONFIG_SET, changes(("debuggers.dut.executable", inside.as_posix())))
    finally:
        tools.close()

    assert refused["ok"] is not True
    assert refused["error_type"] in {"unsafe_configured_path", "config_invalid", "executable_not_found"}, refused
    assert path.read_bytes() == before
    # Still loads, and still the file it was.
    assert load_authoritative_config(workspace).debuggers["dut"].executable != inside.as_posix()


def test_one_bad_key_refuses_the_whole_call(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace, path = bench(tmp_path, monkeypatch, **{CONFIG_DESCRIPTION_RIGHT: True})
    before = path.read_bytes()
    tools = service(workspace)
    try:
        refused = tools.call(PROJECT_CONFIG_SET, changes(("target.name", "renamed"), ("target.controller", 7)))
        twice = tools.call(PROJECT_CONFIG_SET, changes(("target.name", "a"), ("target.name", "b")))
    finally:
        tools.close()

    assert refused["error_type"] == "invalid_argument"
    assert twice["error_type"] == "invalid_argument"
    assert path.read_bytes() == before


# ---------------------------------------------------------------------------
# Provenance.


def test_a_change_records_what_moved_when_and_through_which_call(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace, path = bench(tmp_path, monkeypatch, **{CONFIG_DESCRIPTION_RIGHT: True})
    tools = service(workspace)
    try:
        first = tools.call(PROJECT_CONFIG_SET, changes(("debuggers.dut.probe_id", "066AFF495451885087171450")))
        assert first["ok"] is True, first
        second = tools.call(PROJECT_CONFIG_SET, changes(("target.name", "thermostat-dut"), ("com_ports.dut_uart.baudrate", 230400)))
        assert second["ok"] is True, second
    finally:
        tools.close()

    provenance = document_of(path)["provenance"]
    assert provenance["last_modified_by"] == "agent"
    assert provenance["last_modified_via"] == f"mcp:{PROJECT_CONFIG_SET}"
    assert provenance["last_modified_at"].endswith("Z")
    assert provenance["last_modified_keys"] == ["com_ports.dut_uart.baudrate", "target.name"]
    assert provenance["modification_count"] == 2
    # Who created the file is a different question and a change does not answer it.
    assert {key: provenance[key] for key in PROVENANCE} == PROVENANCE
    assert second["provenance"] == provenance

    text = path.read_text(encoding="utf-8")
    # The header a person wrote survives, and exactly one line says the file has
    # moved since — one, not one per write.
    assert text.startswith("# A person wrote this bench.\n")
    assert text.count("# Changed by an agent through mcp:") == 1


# ---------------------------------------------------------------------------
# The rights-aware answer.


def test_the_answer_differs_with_what_the_operator_granted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A general list would be wrong for most callers, which is why there is none."""
    answers = {}
    base = Path(os.environ["APPDATA"])
    for label, grants in (
        ("none", {}),
        ("description", {CONFIG_DESCRIPTION_RIGHT: True}),
        ("permissions", {CONFIG_PERMISSIONS_RIGHT: True}),
        ("both", {CONFIG_DESCRIPTION_RIGHT: True, CONFIG_PERMISSIONS_RIGHT: True}),
    ):
        workspace, _ = bench(tmp_path / label, monkeypatch, config_root=base / label, **grants)
        tools = service(workspace)
        try:
            answers[label] = tools.call(PROJECT_CONFIG_DESCRIBE)
        finally:
            tools.close()

    open_keys = {label: {entry["key"] for entry in answer["writable_keys"]} for label, answer in answers.items()}
    assert open_keys["none"] == set()
    assert open_keys["description"] and open_keys["permissions"]
    assert open_keys["description"].isdisjoint(open_keys["permissions"])
    assert open_keys["both"] == open_keys["description"] | open_keys["permissions"]
    assert len({frozenset(keys) for keys in open_keys.values()}) == 4, "four grant states, four different answers"
    for answer in answers.values():
        assert not {entry["key"] for entry in answer["locked_keys"]} & {entry["key"] for entry in answer["writable_keys"]}
        for entry in answer["locked_keys"]:
            assert entry["unlocked_by"] == f"permissions.{entry['right']}"
        assert answer["reference"] == CONFIG_SHAPE_URI


def test_the_answer_carries_the_value_shape_and_the_current_value(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace, _ = bench(tmp_path, monkeypatch, **{CONFIG_DESCRIPTION_RIGHT: True})
    tools = service(workspace)
    try:
        described = tools.call(PROJECT_CONFIG_DESCRIBE)
    finally:
        tools.close()

    by_key = {entry["key"]: entry for entry in described["writable_keys"] + described["locked_keys"]}
    assert by_key["com_ports.dut_uart.device"]["current_value"] == "COM9"
    assert by_key["com_ports.dut_uart.baudrate"]["value_schema"]["type"] == "integer"
    assert by_key["debuggers.dut.permissions.allow_flash"]["current_value"] is False
    # A device the file does not have yet is addressable, and the answer says how.
    patterns = {entry["key"]: entry for entry in described["new_entry_patterns"]}
    assert "com_ports.<name>.<field>" in patterns
    assert patterns["com_ports.<name>.<field>"]["granted"] is True
    assert "device" in patterns["com_ports.<name>.<field>"]["fields"]


def test_the_answer_needs_no_grant_at_all(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Reading is free under decision 0018, and this is a read."""
    workspace, _ = bench(tmp_path, monkeypatch)
    tools = service(workspace)
    try:
        described = tools.call(PROJECT_CONFIG_DESCRIBE)
    finally:
        tools.close()

    assert described["ok"] is True, described
    assert [right["granted"] for right in described["rights"]] == [False, False]
    assert described["regeneration"]["permission"] == CONFIG_WRITE_RIGHT


def test_a_workspace_with_no_configuration_is_sent_to_the_one_tool_that_helps(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from agentic_hil.tools import PROJECT_CONFIG_CREATE, UnprovisionedToolService

    workspace = (tmp_path / "empty").resolve()
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    unprovisioned = UnprovisionedToolService(workspace)
    try:
        for name in (PROJECT_CONFIG_DESCRIBE, PROJECT_CONFIG_SET):
            refused = unprovisioned.call(name, changes(("target.name", "x")) if name == PROJECT_CONFIG_SET else {})
            assert refused["error_type"] == "config_file_not_found"
            assert PROJECT_CONFIG_CREATE in refused["next_step"]
    finally:
        unprovisioned.close()


# ---------------------------------------------------------------------------
# One source for the value shapes, and one for the model.


def _rule(section: str, *, under_permissions: bool):
    return next(rule for rule in CONFIG_KEY_RULES if rule.section == section and rule.under_permissions == under_permissions)


def test_every_settable_key_resolves_to_a_node_of_the_shipped_schema() -> None:
    """No value shape is written down twice, so none can drift.

    The reference, the refusal and the check all read this one node."""
    schema = config_schema()
    for entry in config_key_catalogue():
        parts = str(entry["key"]).split(".")
        node = schema["properties"][parts[0]]
        if parts[1] == "<name>":
            node = node["additionalProperties"]
            parts = parts[1:]
        if parts[1] == "permissions":
            reference = node["properties"]["permissions"]["$ref"]
            node = schema["$defs"][reference.rsplit("/", 1)[-1]]
            parts = parts[1:]
        expected = node["properties"][parts[1]]

        assert entry["value_schema"] == expected, entry["key"]
        # Scalars only. A settable key whose value were an object or an array
        # would be a subtree, and a subtree is content the agent authored.
        declared = expected.get("type")
        kinds = set(declared if isinstance(declared, list) else [declared])
        assert not kinds & {"object", "array"}, entry["key"]


def test_the_key_model_maps_every_key_to_exactly_one_rule() -> None:
    for entry in config_key_catalogue():
        concrete = str(entry["key"]).replace("<name>", "dut")
        resolved = resolve_config_key(concrete)

        assert resolved is not None, concrete
        assert resolved.right == entry["right"], concrete
    # A key that names a container resolves to nothing at all.
    for container in ("debuggers", "debuggers.dut", "debuggers.dut.permissions", "permissions", "target"):
        assert resolve_config_key(container) is None, container
    # Nothing outside the two halves is reachable, however it is spelled.
    for outside in ("workspace_root", "state_root", "version", "artifacts.allow_upload", "debug.allow_all_symbols", "recovery.auto_recover", "debuggers.dut.flash_address", "com_ports.dut_uart.assert_dtr", "provenance.created_by"):
        assert resolve_config_key(outside) is None, outside


def test_an_entry_name_with_a_dot_is_read_from_the_right() -> None:
    """Entry names may contain dots, so a key is read from its last component.

    Field names are a closed set and contain none, which makes that reading the
    only possible one — and keeps an entry called `a.probe_id` addressable."""
    plain = resolve_config_key("debuggers.a.b.probe_id")
    awkward = resolve_config_key("debuggers.a.probe_id.probe_id")
    permissions = resolve_config_key("debuggers.a.permissions.permissions.allow_flash")

    assert plain is not None and plain.entry == "a.b" and plain.field == "probe_id"
    assert awkward is not None and awkward.entry == "a.probe_id"
    assert permissions is not None and permissions.entry == "a.permissions" and permissions.field == "allow_flash"


def test_every_project_grant_the_schema_declares_is_known_to_the_loader() -> None:
    """A grant added to the schema and forgotten in the loader is unloadable.

    `permissions` is also the name of a block three releases ago removed, so a
    key the migration check does not recognise is refused with an error about a
    file nobody has."""
    declared = set(config_schema()["properties"]["permissions"]["properties"])

    assert SURVIVING_SECTION_KEYS["permissions"] == declared
    assert {entry["key"] for entry in config_key_catalogue() if entry["key"].startswith("permissions.")} == {f"permissions.{name}" for name in declared}


def test_the_can_bus_half_of_the_decision_covers_every_field_but_the_permissions() -> None:
    """`can_buses.<n>.*` in the decision is derived, not transcribed.

    A field added to the schema becomes settable without a second edit, and
    cannot be silently left out of one either."""
    entry_schema = config_schema()["properties"]["can_buses"]["additionalProperties"]["properties"]

    assert set(config_rule_fields(_rule("can_buses", under_permissions=False))) == set(entry_schema) - {"permissions"}


# ---------------------------------------------------------------------------
# The reference half. Capability and description ship together or not at all.


def test_the_reference_describes_the_shape_of_a_configuration() -> None:
    contents = read_resource(CONFIG_SHAPE_URI)

    assert contents is not None
    document = contents["text"]
    assert contents["mimeType"] == "text/markdown"
    # Every section, so nobody has to open the schema to learn one exists.
    for section in config_schema()["properties"]:
        assert f"### `{section}`" in document, section
    # Which two are required, and a case worked through rather than described.
    assert "workspace_root` — required" in document
    assert "state_root` — required" in document
    assert "## A worked example" in document
    assert "066AFF495451885087171450" in document and "stm32f446re" in document


def test_the_reference_says_how_to_change_a_configuration_and_what_cannot_be_done() -> None:
    document = (read_resource(CONFIG_SHAPE_URI) or {})["text"]

    assert PROJECT_CONFIG_SET in document and PROJECT_CONFIG_DESCRIBE in document
    assert "## What deliberately cannot be done" in document
    for refusal in ("your own file tools", "subtree", "while a run is open", "deleting a key"):
        assert refusal in document, refusal
    # The value shapes in it are the schema's, rendered — not a second list.
    for entry in config_key_catalogue():
        assert f"| `{entry['key']}` | `{entry['right']}` |" in document, entry["key"]
    assert "| `debuggers.<name>.probe_id` | `allow_config_description_write` | string or null, matching" in document
    assert "| `debuggers.<name>.permissions.allow_flash` | `allow_config_permissions_write` | boolean |" in document


def test_the_reference_is_advertised_and_readable_over_mcp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace, _ = bench(tmp_path, monkeypatch)
    tools = service(workspace)
    try:
        listed = handle_mcp_message({"jsonrpc": "2.0", "id": 1, "method": "resources/list", "params": {}}, tools)
        read = handle_mcp_message({"jsonrpc": "2.0", "id": 2, "method": "resources/read", "params": {"uri": CONFIG_SHAPE_URI}}, tools)
    finally:
        tools.close()

    assert isinstance(listed, dict) and isinstance(read, dict)
    assert CONFIG_SHAPE_URI in {entry["uri"] for entry in listed["result"]["resources"]}
    assert read["result"]["contents"][0]["text"].startswith("# The shape of an Agentic HIL configuration")


def test_a_refused_write_names_the_missing_grant_and_serves_the_same_fix(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The refusal and the reference read one catalogue, like every other error."""
    workspace, _ = bench(tmp_path, monkeypatch, **{CONFIG_DESCRIPTION_RIGHT: True})
    tools = service(workspace)
    try:
        refused = tools.call(PROJECT_CONFIG_SET, changes(("debuggers.dut.permissions.allow_flash", True)))
        catalogue = json.loads((read_resource("agentic-hil://reference/errors") or {})["text"])
    finally:
        tools.close()

    served = next(entry for entry in catalogue["entries"] if entry.get("scope") == CONFIG_PERMISSIONS_RIGHT)
    assert refused["remediation"] == served["remediation"]
    assert refused["do_not"] == served["do_not"]
    assert refused["permission_key"] in served["remediation"][0] or CONFIG_PERMISSIONS_RIGHT in served["remediation"][0]
    assert refused["reference"] == CONFIG_SHAPE_URI


def test_a_server_bound_to_a_file_that_is_not_the_authoritative_one_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Validating before replacing is worth nothing if the wrong file is replaced.

    A server whose loaded configuration is not this workspace's authoritative one
    has two files and no right answer: changing the authoritative one moves a
    policy it is not running under, changing the loaded one moves a file nothing
    reads. It refuses rather than choosing."""
    workspace, path = bench(tmp_path, monkeypatch, **{CONFIG_DESCRIPTION_RIGHT: True})
    tools = service(workspace)
    before = path.read_bytes()
    try:
        elsewhere = path.parent / "elsewhere.yaml"
        elsewhere.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
        monkeypatch.setenv("AGENTIC_HIL_CONFIG", str(elsewhere))

        refused = tools.call(PROJECT_CONFIG_SET, changes(("target.name", "renamed")))
        described = tools.call(PROJECT_CONFIG_DESCRIBE)
    finally:
        tools.close()

    assert refused["error_type"] == "config_invalid"
    assert refused["authoritative_path"].endswith("elsewhere.yaml")
    assert described["error_type"] == "config_invalid"
    assert path.read_bytes() == before


def test_the_whole_path_works_over_mcp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """What an agent actually does: ask, then write, without a failed attempt.

    This is the proof the second half of the issue asks for — from "board
    attached" to a valid change with no round trip spent on a rejected guess."""
    workspace, path = bench(tmp_path, monkeypatch, **{CONFIG_DESCRIPTION_RIGHT: True})
    tools = service(workspace)

    def over_mcp(name: str, arguments: dict) -> dict:
        response = handle_mcp_message({"jsonrpc": "2.0", "id": name, "method": "tools/call", "params": {"name": name, "arguments": arguments}}, tools)
        assert isinstance(response, dict)
        return response["result"]

    try:
        described = over_mcp(PROJECT_CONFIG_DESCRIBE, {})
        assert described["isError"] is False
        open_now = {entry["key"] for entry in described["structuredContent"]["writable_keys"]}
        assert "debuggers.dut.probe_id" in open_now

        written = over_mcp(PROJECT_CONFIG_SET, changes(("debuggers.dut.probe_id", "066AFF495451885087171450")))
        assert written["isError"] is False, written
        assert written["structuredContent"]["reload_required"] is True
    finally:
        tools.close()

    assert document_of(path)["debuggers"]["dut"]["probe_id"] == "066AFF495451885087171450"


def test_the_server_says_before_the_first_call_that_this_is_the_way_to_change_a_config() -> None:
    """A refusal arrives after the decision to reach for a file tool.

    `initialize` is the only moment that precedes it, which is why the two calls
    and the two permissions are named there and not only in a resource."""
    from agentic_hil.mcp import SERVER_INSTRUCTIONS

    assert PROJECT_CONFIG_DESCRIBE in SERVER_INSTRUCTIONS
    assert PROJECT_CONFIG_SET in SERVER_INSTRUCTIONS
    assert CONFIG_DESCRIPTION_RIGHT in SERVER_INSTRUCTIONS
    assert CONFIG_PERMISSIONS_RIGHT in SERVER_INSTRUCTIONS
    assert CONFIG_SHAPE_URI in SERVER_INSTRUCTIONS
    # And the older rule it must not contradict.
    assert "Never edit the authoritative configuration" in SERVER_INSTRUCTIONS


def test_setup_still_denies_the_agents_own_file_tools_on_the_configuration(tmp_path: Path) -> None:
    """The precondition, not the contradiction: one door, and it is this one."""
    from agentic_hil.cli import _claude_code_deny_patterns

    patterns = _claude_code_deny_patterns(tmp_path / "config" / "config.yaml", tmp_path / "state")

    assert len(patterns) == 2
    assert any(pattern.startswith("//") and pattern.endswith("/config/**") for pattern in patterns), patterns
    assert any(pattern.startswith("//") and pattern.endswith("/state/**") for pattern in patterns), patterns
