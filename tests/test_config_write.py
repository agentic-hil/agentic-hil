"""Field-wise configuration writes, and the boundary the two grants draw.

The invariant the ratchet established was that an agent can enable itself up to
observation and never up to modification. Field-wise writes open a door in that wall
and the owner decided the shape of it: **two** grants, not one. A single
``allow_config_write`` would have been a master key: set to let an agent enter a
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
from conftest import FAKE_GDB, FAKE_OPENOCD, FAKE_STLINK, write_authoritative_config

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
    DEBUGGER_FIELD_MATRIX,
    ErrorRemedy,
    ResolvedConfigKey,
    config_key_catalogue,
    config_key_schema,
    config_rule_fields,
    lookup_remedy,
    read_resource,
    remediation_fields,
    resolve_config_key,
)
from agentic_hil.mcp import handle_mcp_message
from agentic_hil.tools import AgenticHILToolService

COM_PORTS = 'com_ports:\n  dut_uart:\n    device: "COM9"\n    baudrate: 115200\n'
CAN_BUSES = 'can_buses:\n  body:\n    adapter: "peak"\n    channel: "PCAN_USBBUS1"\n'
PROVENANCE = {"created_by": "agent", "created_via": "mcp:project_config_create", "created_at": "2026-01-01T00:00:00Z", "agentic_hil_version": "0.0.0"}


def bench(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    config_root: Path | None = None,
    device_permissions: dict[str, bool] | None = None,
    debugger_type: str = "openocd",
    **grants: bool,
) -> tuple[Path, Path]:
    """A configuration with one probe, one port and one CAN bus, opened as asked.

    The configuration goes under the sandbox `isolated_config_environment` set up
    rather than under `tmp_path`: on Windows the per-user Temp ACL is replaceable,
    so a policy file there is refused by the ancestor check before any of this
    gets a chance to run.

    Device grants start false unless `device_permissions` says otherwise. That is
    no longer what a *generated* configuration looks like (since 0.8.0 a
    generation grants everything), but a bench a person narrowed is exactly the
    state most of these tests are about, and a test that needs something to take
    away asks for it."""
    workspace = (tmp_path / "workspace").resolve()
    root = config_root or Path(os.environ["APPDATA"]) / "config-write"
    path = write_authoritative_config(
        workspace,
        monkeypatch,
        config_root=root,
        config_version=2,
        permissions=device_permissions or {},
        debugger_type=debugger_type,
        com_ports_yaml=COM_PORTS,
        can_buses_yaml=CAN_BUSES,
    )
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
        assert locked["permission"] == f"permissions.{CONFIG_PERMISSIONS_RIGHT}"
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
    assert refused["permission"] == f"permissions.{CONFIG_PERMISSIONS_RIGHT}"


def test_the_permissions_grant_alone_opens_the_permissions_and_nothing_else(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace, path = bench(tmp_path, monkeypatch, device_permissions={"allow_flash": True}, **{CONFIG_PERMISSIONS_RIGHT: True})
    tools = service(workspace)
    try:
        described = tools.call(PROJECT_CONFIG_DESCRIBE)
        assert {entry["right"] for entry in described["writable_keys"]} == {CONFIG_PERMISSIONS_RIGHT}
        assert {entry["right"] for entry in described["locked_keys"]} == {CONFIG_DESCRIPTION_RIGHT}

        narrowed = tools.call(PROJECT_CONFIG_SET, changes(("debuggers.dut.permissions.allow_flash", False)))
        assert narrowed["ok"] is True, narrowed
        assert narrowed["permissions_changed"] == ["debuggers.dut.permissions.allow_flash"]

        refused = tools.call(PROJECT_CONFIG_SET, changes(("debuggers.dut.probe_id", "066AFF49")))
        assert refused["error_type"] == "permission_denied"
        assert refused["permission"] == f"permissions.{CONFIG_DESCRIPTION_RIGHT}"
        assert refused["denied_keys"] == ["debuggers.dut.probe_id"]
    finally:
        tools.close()
    document = document_of(path)
    assert document["debuggers"]["dut"]["permissions"]["allow_flash"] is False
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
        assert refused["permission"] == f"permissions.{CONFIG_DESCRIPTION_RIGHT}"
        assert refused["permission_key"] == f"permissions.{CONFIG_DESCRIPTION_RIGHT}"
        assert refused["path"] == str(path)
    finally:
        tools.close()
    assert path.read_bytes() == before, "a refusal must not write anything"


@pytest.mark.parametrize(
    ("grants", "change", "right"),
    [
        ({}, ("target.name", "renamed"), CONFIG_DESCRIPTION_RIGHT),
        ({CONFIG_DESCRIPTION_RIGHT: True}, ("debuggers.dut.permissions.allow_flash", True), CONFIG_PERMISSIONS_RIGHT),
    ],
)
def test_a_config_write_refusal_hands_over_the_operators_grant_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, grants: dict, change: tuple[str, Any], right: str
) -> None:
    """Both config-write refusals name the exact `agentic-hil grant` line (round 1, finding 3).

    The documented narrow recovery for a closed config-write grant is the
    operator-only `agentic-hil grant permissions.<key>`, and the same refusal's
    own remediation tells the agent not to edit the file with its own tools. The
    refusal used to say only that "a person edits the file", leaving the
    actionable command unnamed while discouraging the one thing it did name. Both
    the description-write and the permissions-write refusal now carry the exact
    command in the sentence a person reads first and in the machine-facing next
    step, so whichever a caller reports has the line to paste.
    """
    workspace, path = bench(tmp_path, monkeypatch, **grants)
    before = path.read_bytes()
    tools = service(workspace)
    try:
        refused = tools.call(PROJECT_CONFIG_SET, changes(change))
    finally:
        tools.close()

    assert refused["error_type"] == "permission_denied"
    assert refused["permission"] == f"permissions.{right}"
    grant = f"agentic-hil grant permissions.{right}"
    assert grant in refused["summary"]
    assert grant in refused["next_step"]
    # And it no longer offers editing the file as the way back, which the scoped
    # remediation on the same refusal forbids the agent from doing.
    assert "only a person editing that file can change that" not in refused["summary"]
    # The scope-specific remediation is still the catalogue's, so naming the grant
    # command did not cost the entry its scoped advice.
    assert refused["remediation"] == remediation_fields("permission_denied", right)["remediation"]
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
    assert refused["permission"] == f"permissions.{CONFIG_PERMISSIONS_RIGHT}"
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
    that names a subtree resolves: both halves are asserted here, because either
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
    So the model is broken here on purpose (told that a permission key belongs to
    the description grant) and the write must still be refused, because the
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
    assert refused["permission"] == f"permissions.{CONFIG_PERMISSIONS_RIGHT}"
    assert refused["denied_keys"] == ["debuggers.dut.permissions.allow_flash"]
    assert path.read_bytes() == before


def test_the_document_comparison_sees_a_permission_wherever_it_sits() -> None:
    """What the second check actually looks at.

    Not the paths the key model can produce: every `allow_*` key at any depth,
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
    field names: they are entry ids the operator chose, and `permissions`,
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
    """Hardware attached after init: it may be described, not empowered."""
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
    wrong thing. Sent as `false`, which is the only value this surface writes
    into a permission at all, so what is being tested is the entry and not the
    direction."""
    workspace, path = bench(tmp_path, monkeypatch, **{CONFIG_DESCRIPTION_RIGHT: True, CONFIG_PERMISSIONS_RIGHT: True})
    before = path.read_bytes()
    tools = service(workspace)
    try:
        refused = tools.call(PROJECT_CONFIG_SET, changes(("debuggers.ghost.permissions.allow_flash", False)))
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
    # moved since: one, not one per write.
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
    # The two grants the schema puts directly on a fixed section are part of the
    # permissions half, not outside it. A generation writes both true, so leaving
    # them unreachable left two things a generated bench grants that an operator
    # could take back only by opening the YAML.
    for grant in ("artifacts.allow_upload", "debug.allow_all_symbols"):
        section_grant = resolve_config_key(grant)
        assert section_grant is not None, grant
        assert section_grant.right == "allow_config_permissions_write", grant
    # Nothing outside the two halves is reachable, however it is spelled, and
    # that still includes the list-valued neighbours of those two grants, which
    # are not scalars.
    for outside in ("workspace_root", "state_root", "version", "artifacts.allowed_roots", "artifacts.upload_directory", "debug.allowed_symbols", "debug.max_dump_size_bytes", "recovery.auto_recover", "debuggers.dut.flash_address", "com_ports.dut_uart.assert_dtr", "provenance.created_by"):
        assert resolve_config_key(outside) is None, outside


def test_an_entry_name_with_a_dot_is_read_from_the_right() -> None:
    """Entry names may contain dots, so a key is read from its last component.

    Field names are a closed set and contain none, which makes that reading the
    only possible one, and keeps an entry called `a.probe_id` addressable."""
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


def test_the_can_bus_half_of_the_decision_covers_every_scalar_field_but_the_permissions() -> None:
    """`can_buses.<n>.*` in the decision is derived, not transcribed.

    A scalar field added to the schema becomes settable without a second edit,
    and cannot be silently left out of one either. A field whose value is a
    subtree is the exception, and is one by construction rather than by being
    remembered: this surface sets one value at a time, so an object or an array
    would be the agent authoring structure. `shares:` is the standing case: a
    bus's participant views are the operator's, edited in the file."""
    entry_schema = config_schema()["properties"]["can_buses"]["additionalProperties"]["properties"]
    subtrees = {name for name, node in entry_schema.items() if str(node.get("type")) in {"object", "array"}}
    settable = set(config_rule_fields(_rule("can_buses", under_permissions=False)))

    assert "shares" in subtrees, "the case this rule exists for is still in the schema"
    assert settable == set(entry_schema) - {"permissions"} - subtrees
    assert not settable & subtrees


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
    assert "workspace_root` (required)" in document
    assert "state_root` (required)" in document
    assert "## A worked example" in document
    assert "066AFF495451885087171450" in document and "stm32f446re" in document


def test_the_reference_says_how_to_change_a_configuration_and_what_cannot_be_done() -> None:
    document = (read_resource(CONFIG_SHAPE_URI) or {})["text"]

    assert PROJECT_CONFIG_SET in document and PROJECT_CONFIG_DESCRIBE in document
    assert "## What deliberately cannot be done" in document
    for refusal in ("your own file tools", "subtree", "while a run is open", "deleting a key"):
        assert refusal in document, refusal
    # The value shapes in it are the schema's, rendered, not a second list.
    for entry in config_key_catalogue():
        assert f"| `{entry['key']}` | `{entry['right']}` |" in document, entry["key"]
    assert "| `debuggers.<name>.probe_id` | `allow_config_description_write` | string or null, matching" in document
    assert "| `debuggers.<name>.permissions.allow_flash` | `allow_config_permissions_write` | boolean |" in document


def test_the_reference_scopes_the_ratchet_to_the_call_it_holds_for() -> None:
    """The security claim in the live resource is the one that is true.

    An agent reads this before it decides what it may do, and it said the file
    could only ever narrow, while `project_config_create` on the same surface
    can regenerate it open. The owner's decision on the generated default keeps
    regeneration outside the ratchet deliberately, so the resource has to say
    which call the direction belongs to and what the other one does."""
    document = (read_resource(CONFIG_SHAPE_URI) or {})["text"]

    assert "### Permissions move one way: through `project_config_set`" in document
    assert "### What the ratchet does not cover" in document
    assert "the MCP permission-write path can only narrow" in document
    # Regeneration, described as it behaves rather than as the ratchet, and at
    # the generated defaults, which are not "everything".
    assert "arrives at the generated defaults" in document
    assert "is at those same defaults" in document
    assert "`permissions.allow_config_write` to false" in document


def test_the_description_grant_remedy_gives_the_reason_that_is_still_true() -> None:
    """A shipped remedy that reasons from the removed closed default.

    It told an agent that reopening `allow_config_description_write` through
    `project_config_set` fails because no configuration hands out
    `allow_config_permissions_write`, and a generated configuration grants
    exactly that. On a bench where the description grant was
    narrowed and the permissions grant is still open, the resource gave a
    factually wrong reason for a correct refusal. The reason is the false-only
    ratchet, and it holds whether or not the gating grant is true."""
    from agentic_hil.knowledge import ERROR_CATALOGUE

    remedy = ERROR_CATALOGUE["permission_denied:allow_config_description_write"]
    text = " ".join(remedy.do_not)

    assert "no configuration hands out" not in text
    assert "writes only `false` into a permission" in text
    assert "A generated configuration grants that key" in text


def test_the_reference_names_the_two_grants_outside_a_permissions_block() -> None:
    """`artifacts` and `debug` are operator-only apart from their two grants.

    A generation grants `artifacts.allow_upload` and `debug.allow_all_symbols`
    like every other permission, so they are in the key model and an agent can
    narrow them. The resource listed both whole sections as unsettable, which
    left one document answering the same question two opposite ways."""
    document = (read_resource(CONFIG_SHAPE_URI) or {})["text"]

    assert "| `artifacts.allow_upload` | `allow_config_permissions_write` | boolean |" in document
    assert "| `debug.allow_all_symbols` | `allow_config_permissions_write` | boolean |" in document
    assert "widening `artifacts.allow_upload` or `debug.allow_all_symbols`" in document
    # And their list and path neighbours stay the operator's.
    assert "`artifacts.allowed_roots`, `debug.allowed_symbols`, `upload_directory`" in document
    assert "except the two grants below | not settable over MCP at all" in document


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

    This is the proof the second half of the issue asks for: from "board
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


def test_connect_mode_is_opened_by_the_description_grant_and_reaches_the_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The key an agent needs after a bench keeps failing its first flash.

    It sits with the four fields that already say what the bench *is*, not with
    the permissions: both of its values are a flash this configuration already
    allows, and which one this board needs is a fact about the board. So the
    description grant alone has to be enough to write it, and the value has to
    arrive in the file rather than in a refusal."""
    workspace, path = bench(tmp_path, monkeypatch, debugger_type="stlink", **{CONFIG_DESCRIPTION_RIGHT: True})
    tools = service(workspace)
    try:
        described = tools.call(PROJECT_CONFIG_DESCRIBE, {})
        writable = {entry["key"]: entry for entry in described["writable_keys"]}
        written = tools.call(PROJECT_CONFIG_SET, changes(("debuggers.dut.connect_mode", "under_reset")))
    finally:
        tools.close()

    assert "debuggers.dut.connect_mode" in writable
    assert writable["debuggers.dut.connect_mode"]["right"] == CONFIG_DESCRIPTION_RIGHT
    assert writable["debuggers.dut.connect_mode"]["value_schema"]["enum"] == ["hotplug", "under_reset"]
    assert written["ok"] is True, written
    assert written["permissions_changed"] == [], "a connect mode grants nothing"
    assert document_of(path)["debuggers"]["dut"]["connect_mode"] == "under_reset"


def test_a_connect_mode_the_backend_cannot_carry_out_rolls_the_file_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The per-backend half is enforced on the write path too, by the loader.

    Nothing in this surface knows what OpenOCD can do; what it does know is that
    the file it just wrote has to load as authoritative before it may replace the
    one that did. So the same refusal a hand-edited file gets arrives here, and
    the bench keeps the configuration it had."""
    workspace, path = bench(tmp_path, monkeypatch, **{CONFIG_DESCRIPTION_RIGHT: True})
    before = path.read_bytes()
    tools = service(workspace)
    try:
        refused = tools.call(PROJECT_CONFIG_SET, changes(("debuggers.dut.connect_mode", "under_reset")))
    finally:
        tools.close()

    assert refused["ok"] is False
    assert refused["error_type"] == "config_invalid"
    assert refused["field"] == "debuggers.dut.connect_mode"
    assert refused["allowed_values"] == ["hotplug"]
    assert path.read_bytes() == before, "nothing changed"


# ---------------------------------------------------------------------------
# `debuggers.<name>.type`, the one description key checked as a whole.


def rewrite_debugger(path: Path, *, drop: tuple[str, ...] = (), **fields: Any) -> None:
    """The bench's own entry as a person would have left it, minus or plus keys.

    Written back through the same header the fixture uses, so the file that
    reaches the write path is one this project would have produced."""
    document = document_of(path)
    entry = document["debuggers"]["dut"]
    for field in drop:
        entry.pop(field, None)
    entry.update(fields)
    path.write_text("# A person wrote this bench.\n" + yaml.safe_dump(document, sort_keys=False), encoding="utf-8")


def test_the_debug_stack_is_opened_by_the_description_grant_and_reaches_the_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The key a bench needs to move from one debug stack to another.

    It was kept out of this surface on the grounds that it changes what a call
    does to the board, and that reason does not survive next to `executable`,
    `interface_cfg` and `target_cfg`: those three already decide which binary
    runs with which scripts, and they are open. Which stack this bench runs is a
    description of this bench, so the description grant alone has to be enough,
    and the value has to arrive in the file rather than in a refusal.

    The switch onto openocd carries the two scripts openocd requires as well as
    the executable, because a real switch re-equips the entry from the request
    rather than inheriting the scripts the stlink entry happened to carry."""
    workspace, path = bench(tmp_path, monkeypatch, debugger_type="stlink", **{CONFIG_DESCRIPTION_RIGHT: True})
    tools = service(workspace)
    try:
        described = tools.call(PROJECT_CONFIG_DESCRIBE, {})
        writable = {entry["key"]: entry for entry in described["writable_keys"]}
        written = tools.call(
            PROJECT_CONFIG_SET,
            changes(
                ("debuggers.dut.type", "openocd"),
                ("debuggers.dut.executable", FAKE_OPENOCD.as_posix()),
                ("debuggers.dut.interface_cfg", "interface/stlink.cfg"),
                ("debuggers.dut.target_cfg", "target/stm32f4x.cfg"),
            ),
        )
    finally:
        tools.close()

    assert "debuggers.dut.type" in writable
    assert writable["debuggers.dut.type"]["right"] == CONFIG_DESCRIPTION_RIGHT
    assert writable["debuggers.dut.type"]["value_schema"]["enum"] == ["openocd", "stlink", "pyocd"]
    assert writable["debuggers.dut.type"]["current_value"] == "stlink"
    assert written["ok"] is True, written
    assert written["permissions_changed"] == [], "a debug stack grants nothing"
    assert document_of(path)["debuggers"]["dut"]["type"] == "openocd"
    assert load_authoritative_config(workspace).debuggers["dut"].type == "openocd"


def test_the_debug_stack_stays_shut_without_the_description_grant(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Opened by one grant means locked without it, like its four siblings."""
    workspace, path = bench(tmp_path, monkeypatch, debugger_type="stlink")
    before = path.read_bytes()
    tools = service(workspace)
    try:
        described = tools.call(PROJECT_CONFIG_DESCRIBE, {})
        locked = {entry["key"]: entry for entry in described["locked_keys"]}
        refused = tools.call(PROJECT_CONFIG_SET, changes(("debuggers.dut.type", "openocd")))
    finally:
        tools.close()

    assert locked["debuggers.dut.type"]["right"] == CONFIG_DESCRIPTION_RIGHT
    assert refused["error_type"] == "permission_denied"
    assert refused["permission"] == f"permissions.{CONFIG_DESCRIPTION_RIGHT}"
    assert refused["denied_keys"] == ["debuggers.dut.type"]
    assert path.read_bytes() == before, "nothing changed"


def test_a_backend_switch_the_entry_is_not_equipped_for_is_refused_naming_what_is_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The half-switched entry, refused before it can be written.

    An ST-Link entry has no reason to carry OpenOCD's two scripts, and both
    fields have a schema default, so a `type` written on its own would load and
    then reach the board through whatever `interface/stlink.cfg` and
    `target/stm32f4x.cfg` resolve to on this host. That is a bench described as
    something it is not, and it fails at the first call rather than at the write
    that caused it. So the switch is refused, and the refusal names the two keys
    instead of leaving the caller to guess which of the entry it has to fill.

    The entry still carries the ST-Link binary it was flashing with, and that is
    named too: a real switch replaces it, so `executable` is one of the keys the
    same call has to carry, as a path or as `null`."""
    workspace, path = bench(tmp_path, monkeypatch, debugger_type="stlink", **{CONFIG_DESCRIPTION_RIGHT: True})
    rewrite_debugger(path, drop=("interface_cfg", "target_cfg"))
    before = path.read_bytes()
    tools = service(workspace)
    try:
        refused = tools.call(PROJECT_CONFIG_SET, changes(("debuggers.dut.type", "openocd")))
    finally:
        tools.close()

    assert refused["ok"] is False
    assert refused["error_type"] == "invalid_argument"
    assert refused["field"] == "debuggers.dut.interface_cfg"
    assert refused["missing_keys"] == ["debuggers.dut.executable", "debuggers.dut.interface_cfg", "debuggers.dut.target_cfg"]
    assert refused["debugger_type"] == "openocd"
    assert refused["rejected_key"] == "debuggers.dut.type"
    assert "interface_cfg" in refused["summary"] and "target_cfg" in refused["summary"]
    assert "executable" in refused["summary"], "the stale one belongs to the backend the entry is leaving"
    assert "executable" in refused["next_step"]
    assert path.read_bytes() == before, "nothing was written"


def test_a_type_only_switch_is_refused_even_when_the_entry_already_carries_the_new_backends_fields(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The trap the request-side check exists to close, on an untouched entry.

    A generated ST-Link entry carries `interface_cfg`, `target_cfg` and an
    `executable`: the two scripts because generation writes them whatever the
    backend, ignored by stlink, and the binary because stlink was flashing with
    one. Reading the post-change document would find all three present and let a
    `type`-only switch to openocd through, leaving the entry on openocd with the
    scripts generation happened to write and the ST-Link binary run as if it were
    openocd. Nothing here was dropped to force the refusal, the entry is exactly
    what this project produces, and the switch is refused all the same, because
    the *request* carried none of the fields the switch across needs. The refusal
    names every one of them so one more call lands the whole entry."""
    workspace, path = bench(tmp_path, monkeypatch, debugger_type="stlink", **{CONFIG_DESCRIPTION_RIGHT: True})
    entry = document_of(path)["debuggers"]["dut"]
    assert entry["interface_cfg"] and entry["target_cfg"] and entry["executable"], "the stale-but-present fields the check must not credit"
    before = path.read_bytes()
    tools = service(workspace)
    try:
        refused = tools.call(PROJECT_CONFIG_SET, changes(("debuggers.dut.type", "openocd")))
    finally:
        tools.close()

    assert refused["ok"] is False
    assert refused["error_type"] == "invalid_argument"
    assert refused["debugger_type"] == "openocd"
    assert refused["missing_keys"] == ["debuggers.dut.executable", "debuggers.dut.interface_cfg", "debuggers.dut.target_cfg"]
    assert path.read_bytes() == before, "nothing was written"


def test_writing_the_type_an_entry_already_has_is_not_a_switch_and_demands_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The rule compares the old `type` against the new one, not the mere presence of `type` in the call.

    Re-sending the backend an entry already names moves nothing across, so the
    entry carries none of its fields for a backend it is leaving: the executable
    it holds was chosen for this same backend, and nothing has to be resent to
    keep the entry honest. The switch contract is for a real switch, so a `type`
    written to its own value lands on the description grant like any other key
    rather than being refused for an executable it need not replace."""
    workspace, path = bench(tmp_path, monkeypatch, debugger_type="stlink", **{CONFIG_DESCRIPTION_RIGHT: True})
    executable_before = document_of(path)["debuggers"]["dut"]["executable"]
    tools = service(workspace)
    try:
        written = tools.call(PROJECT_CONFIG_SET, changes(("debuggers.dut.type", "stlink")))
    finally:
        tools.close()

    assert written["ok"] is True, written
    entry = document_of(path)["debuggers"]["dut"]
    assert entry["type"] == "stlink"
    assert entry["executable"] == executable_before, "a no-op type write does not demand a fresh executable"


def test_writing_openocd_to_an_entry_whose_type_is_omitted_is_a_no_op_not_a_switch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`type` is optional and defaults to openocd, so re-stating that default switches nothing.

    An entry that omits `type` already loads on openocd, the loader reads the
    field as `raw.get("type", "openocd")`, so writing `openocd` to it moves the
    entry from openocd to openocd, which is no switch at all. Measured against the
    raw field rather than the effective backend, the absent `type` reads as
    nothing and the write looks like a switch onto openocd, and the entry is then
    refused for the `executable`, `interface_cfg` and `target_cfg` a real switch
    would demand, though it is leaving no backend that chose them, and the refusal
    would name an executable belonging to a backend the entry is not leaving. The
    comparison uses the loader's own default, so this lands on the description
    grant and the entry keeps every field it already had."""
    workspace, path = bench(tmp_path, monkeypatch, **{CONFIG_DESCRIPTION_RIGHT: True})
    rewrite_debugger(path, drop=("type",))
    entry_before = document_of(path)["debuggers"]["dut"]
    assert "type" not in entry_before, "the optional field is genuinely absent"
    assert entry_before["executable"] and entry_before["interface_cfg"] and entry_before["target_cfg"]
    assert load_authoritative_config(workspace).debuggers["dut"].type == "openocd", "and it already loads on openocd"
    tools = service(workspace)
    try:
        written = tools.call(PROJECT_CONFIG_SET, changes(("debuggers.dut.type", "openocd")))
    finally:
        tools.close()

    assert written["ok"] is True, written
    assert "missing_keys" not in written, "a no-op type write demands no other field"
    entry_after = document_of(path)["debuggers"]["dut"]
    assert entry_after["type"] == "openocd"
    assert entry_after["executable"] == entry_before["executable"], "a no-op type write demands no fresh executable"
    assert load_authoritative_config(workspace).debuggers["dut"].type == "openocd"


def test_the_same_switch_lands_when_the_missing_keys_come_in_the_same_call(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Atomic, not blocked. The refusal above has to be an answer, not a wall.

    One call carries the whole entry across, and what comes out of it is a
    configuration that loads as authoritative on the new backend."""
    workspace, path = bench(tmp_path, monkeypatch, debugger_type="stlink", **{CONFIG_DESCRIPTION_RIGHT: True})
    rewrite_debugger(path, drop=("interface_cfg", "target_cfg"))
    tools = service(workspace)
    try:
        written = tools.call(
            PROJECT_CONFIG_SET,
            changes(
                ("debuggers.dut.type", "openocd"),
                ("debuggers.dut.executable", FAKE_OPENOCD.as_posix()),
                ("debuggers.dut.interface_cfg", "interface/stlink.cfg"),
                ("debuggers.dut.target_cfg", "target/stm32f4x.cfg"),
            ),
        )
    finally:
        tools.close()

    assert written["ok"] is True, written
    entry = document_of(path)["debuggers"]["dut"]
    assert (entry["type"], entry["interface_cfg"], entry["target_cfg"]) == ("openocd", "interface/stlink.cfg", "target/stm32f4x.cfg")
    reloaded = load_authoritative_config(workspace).debuggers["dut"]
    assert reloaded.type == "openocd"
    assert Path(reloaded.executable or "").resolve() == FAKE_OPENOCD.resolve()


def test_a_switch_onto_a_backend_that_needs_nothing_this_surface_writes_is_not_blocked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The rule demands only what a call could actually have supplied.

    `interface` on stlink and `target_type` on pyocd are required by those
    backends and are not keys this surface sets, so demanding them would turn a
    switch from atomic into impossible. Both have a working unset behaviour the
    field matrix states, which is why they stay the backends' own business."""
    workspace, path = bench(tmp_path, monkeypatch, **{CONFIG_DESCRIPTION_RIGHT: True})
    rewrite_debugger(path, drop=("interface", "interface_cfg", "target_cfg", "target_type"))
    tools = service(workspace)
    try:
        written = tools.call(PROJECT_CONFIG_SET, changes(("debuggers.dut.type", "stlink"), ("debuggers.dut.executable", FAKE_STLINK.as_posix())))
    finally:
        tools.close()

    assert written["ok"] is True, written
    assert document_of(path)["debuggers"]["dut"]["type"] == "stlink"
    assert "interface" not in document_of(path)["debuggers"]["dut"], "and it still works without one"
    assert load_authoritative_config(workspace).debuggers["dut"].interface == "SWD"


def test_the_switch_demands_only_the_fields_this_surface_could_have_supplied() -> None:
    """Why the demanded set is narrower than "everything the backend requires".

    The rule is read off the backend field matrix and intersected with the keys
    `project_config_set` writes, and this is the statement that intersection is
    doing real work: `interface` and `target_type` are required by their
    backends and are out of reach here, so a rule that demanded them would have
    made those two switches impossible rather than atomic. If either ever
    becomes settable, this test is where that decision has to be taken again."""
    settable = set(config_rule_fields(_rule("debuggers", under_permissions=False)))
    required = {backend: {name for name, node in fields.items() if isinstance(node, dict) and node.get("status") == "required"} for backend, fields in DEBUGGER_FIELD_MATRIX.items()}

    assert "type" in settable, "the key this whole group is about"
    assert required["openocd"] & settable == {"interface_cfg", "target_cfg"}
    assert required["stlink"] - settable == {"interface"}
    assert required["pyocd"] - settable == {"target_type"}


def test_a_debug_stack_the_schema_does_not_spell_never_reaches_the_document(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The enum half of the validation, and it is the schema's own.

    Nothing about the set of backends is written down a second time here: the
    value is checked against the node the loader validates against, before any
    change is applied to anything."""
    workspace, path = bench(tmp_path, monkeypatch, **{CONFIG_DESCRIPTION_RIGHT: True})
    before = path.read_bytes()
    tools = service(workspace)
    try:
        refused = tools.call(PROJECT_CONFIG_SET, changes(("debuggers.dut.type", "jlink")))
    finally:
        tools.close()

    assert refused["error_type"] == "invalid_argument"
    assert refused["field"] == "changes.0.value"
    assert refused["value_schema"]["enum"] == ["openocd", "stlink", "pyocd"]
    assert path.read_bytes() == before, "nothing changed"


def test_a_switch_the_new_backend_refuses_a_field_of_puts_the_previous_file_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The other half of a half-switch, and it is the loader that catches it.

    A field the new backend *requires* is checked before the write; a field it
    *refuses* is not checked twice, because the per-backend rule lives in the
    loader and validate-before-replace already turns it into the same "nothing
    was written". The bench keeps the configuration it had, and the same switch
    lands the moment the call settles the refused field too.

    The switch carries the two scripts and the executable openocd needs, so it
    is past the request-side check and it is the loader that catches the refused
    `connect_mode`, which is the point this test pins."""
    workspace, path = bench(tmp_path, monkeypatch, debugger_type="stlink", **{CONFIG_DESCRIPTION_RIGHT: True})
    rewrite_debugger(path, connect_mode="under_reset")
    before = path.read_bytes()
    equipped = (
        ("debuggers.dut.executable", FAKE_OPENOCD.as_posix()),
        ("debuggers.dut.interface_cfg", "interface/stlink.cfg"),
        ("debuggers.dut.target_cfg", "target/stm32f4x.cfg"),
    )
    tools = service(workspace)
    try:
        refused = tools.call(PROJECT_CONFIG_SET, changes(("debuggers.dut.type", "openocd"), *equipped))
        assert path.read_bytes() == before, "the previous file came back"
        settled = tools.call(
            PROJECT_CONFIG_SET,
            changes(("debuggers.dut.type", "openocd"), *equipped, ("debuggers.dut.connect_mode", "hotplug")),
        )
    finally:
        tools.close()

    assert refused["error_type"] == "config_invalid"
    assert refused["field"] == "debuggers.dut.connect_mode"
    assert refused["allowed_values"] == ["hotplug"]
    assert settled["ok"] is True, settled
    assert load_authoritative_config(workspace).debuggers["dut"].type == "openocd"


# ---------------------------------------------------------------------------
# `debug.gdb_executable`, the one description key of a section whose other
# settable key is a grant.


def test_the_gdb_this_bench_reads_with_is_opened_by_the_description_grant(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The key an operator whose GDB lives off PATH had no way to set.

    Nobody could: this surface refused it, `adopt-hardware` carried probe
    identity only, and no grant applied, so the whole of what was left was
    opening the YAML by hand. Which GDB reads this bench's images is the same
    class of fact as `debuggers.<name>.executable`, so the description grant
    alone has to be enough, and the value has to arrive in the file and in the
    configuration the next load enforces."""
    workspace, path = bench(tmp_path, monkeypatch, **{CONFIG_DESCRIPTION_RIGHT: True})
    tools = service(workspace)
    try:
        described = tools.call(PROJECT_CONFIG_DESCRIBE, {})
        writable = {entry["key"]: entry for entry in described["writable_keys"]}
        written = tools.call(PROJECT_CONFIG_SET, changes(("debug.gdb_executable", FAKE_GDB.as_posix())))
    finally:
        tools.close()

    assert "debug.gdb_executable" in writable
    assert writable["debug.gdb_executable"]["right"] == CONFIG_DESCRIPTION_RIGHT
    assert writable["debug.gdb_executable"]["current_value"] is None
    # What the key is for, and that leaving it null is a working state rather
    # than a hole, come out of the shipped schema like every other value shape.
    description = writable["debug.gdb_executable"]["value_schema"]["description"]
    assert "GDB" in description
    assert "autodetect" in description
    assert "arm-none-eabi-gdb, gdb-multiarch and gdb" in description
    assert written["ok"] is True, written
    assert written["permissions_changed"] == [], "which GDB reads an image grants nothing"
    assert document_of(path)["debug"]["gdb_executable"] == FAKE_GDB.as_posix()
    assert load_authoritative_config(workspace).debug.gdb_executable == str(FAKE_GDB)


def test_the_gdb_key_stays_shut_without_the_description_grant(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Opened by one grant means locked without it, like every description key."""
    workspace, path = bench(tmp_path, monkeypatch)
    before = path.read_bytes()
    tools = service(workspace)
    try:
        described = tools.call(PROJECT_CONFIG_DESCRIBE, {})
        locked = {entry["key"]: entry for entry in described["locked_keys"]}
        refused = tools.call(PROJECT_CONFIG_SET, changes(("debug.gdb_executable", FAKE_GDB.as_posix())))
    finally:
        tools.close()

    assert locked["debug.gdb_executable"]["right"] == CONFIG_DESCRIPTION_RIGHT
    assert refused["error_type"] == "permission_denied"
    assert refused["permission"] == f"permissions.{CONFIG_DESCRIPTION_RIGHT}"
    assert refused["denied_keys"] == ["debug.gdb_executable"]
    assert path.read_bytes() == before, "nothing changed"


def test_the_debug_section_is_split_across_both_rights(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`debug` is the one section with a key on each side of the split.

    `allow_all_symbols` is a grant and `gdb_executable` describes the bench, and
    they sit directly on the same section rather than one of them being inside a
    `permissions:` block. So the two have to answer to different grants out of
    one key model: a bench that handed over the permissions half and not the
    description half can still take `allow_all_symbols` away and still cannot
    name a GDB."""
    workspace, path = bench(tmp_path, monkeypatch, **{CONFIG_PERMISSIONS_RIGHT: True})
    tools = service(workspace)
    try:
        described = tools.call(PROJECT_CONFIG_DESCRIBE, {})
        writable = {entry["key"]: entry for entry in described["writable_keys"]}
        locked = {entry["key"]: entry for entry in described["locked_keys"]}
        narrowed = tools.call(PROJECT_CONFIG_SET, changes(("debug.allow_all_symbols", False)))
        refused = tools.call(PROJECT_CONFIG_SET, changes(("debug.gdb_executable", FAKE_GDB.as_posix())))
    finally:
        tools.close()

    assert writable["debug.allow_all_symbols"]["right"] == CONFIG_PERMISSIONS_RIGHT
    assert locked["debug.gdb_executable"]["right"] == CONFIG_DESCRIPTION_RIGHT
    assert narrowed["ok"] is True, narrowed
    assert refused["error_type"] == "permission_denied"
    assert refused["permission"] == f"permissions.{CONFIG_DESCRIPTION_RIGHT}"
    assert document_of(path)["debug"]["gdb_executable"] is None


def test_a_gdb_that_is_not_there_is_refused_in_the_loaders_own_words(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The value shape is the schema's; what a path has to *be* is the loader's.

    A string is a string to the schema, so nothing before the write can tell a
    GDB from a typo. What can is the rule already standing over every configured
    executable, and it is applied the way `connect_mode`'s per-backend rule is:
    the changed document has to load as authoritative before it may replace the
    one that did. So the refusal is the loader's own, naming this field, and the
    bench keeps the file it had."""
    workspace, path = bench(tmp_path, monkeypatch, **{CONFIG_DESCRIPTION_RIGHT: True})
    before = path.read_bytes()
    missing = (tmp_path / "toolchain" / "arm-none-eabi-gdb").resolve()
    tools = service(workspace)
    try:
        refused = tools.call(PROJECT_CONFIG_SET, changes(("debug.gdb_executable", missing.as_posix())))
    finally:
        tools.close()

    assert refused["ok"] is False
    assert refused["error_type"] == "config_invalid"
    assert refused["field"] == "debug.gdb_executable"
    assert refused["summary"].startswith("Configured executable must be an existing single-link regular file."), refused["summary"]
    assert "the previous file was restored" in refused["summary"]
    assert path.read_bytes() == before, "nothing changed"


def test_a_gdb_inside_the_workspace_is_refused_like_every_other_executable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The containment rule travels with the key, unchanged and unrestated.

    A toolchain the repository can rewrite is a toolchain an agent can rewrite,
    which is why no configured executable may live under `workspace_root`. This
    key is held to it because it is the loader that holds every one of them, so
    opening the key added no second copy of the rule to keep in step."""
    workspace, path = bench(tmp_path, monkeypatch, **{CONFIG_DESCRIPTION_RIGHT: True})
    before = path.read_bytes()
    inside = workspace / "tools" / "arm-none-eabi-gdb"
    inside.parent.mkdir(parents=True, exist_ok=True)
    inside.write_text("#!/bin/sh\n", encoding="utf-8")
    tools = service(workspace)
    try:
        refused = tools.call(PROJECT_CONFIG_SET, changes(("debug.gdb_executable", inside.as_posix())))
    finally:
        tools.close()

    assert refused["error_type"] == "config_invalid"
    assert refused["field"] == "debug.gdb_executable"
    assert refused["summary"].startswith("Configured executables must be stored outside the workspace."), refused["summary"]
    assert path.read_bytes() == before, "nothing changed"


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


# ---------------------------------------------------------------------------
# The `false`-only rule, and the surface it is about.
#
# The owner drew this line explicitly: the rule nails the MCP surface
# shut and nothing else. So there are two things to prove: that the MCP surface
# writes `false` and nothing else at all, including a `true` that would move
# nothing, and that a person at the command line is not held to it.


def test_a_permission_already_true_cannot_be_sent_true_again(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The case a before/after comparison cannot see.

    `true` for a permission that is already `true` produces no delta, so a rule
    that only watched for movement would accept it, rewrite the file and stamp
    `provenance` for a write the surface is not allowed to make at all. The rule
    is about the value, and the document comparison is the backstop behind it."""
    workspace, path = bench(
        tmp_path,
        monkeypatch,
        device_permissions={"allow_flash": True, "allow_reset": True},
        **{CONFIG_PERMISSIONS_RIGHT: True},
    )
    before = path.read_bytes()
    tools = service(workspace)
    try:
        refused = tools.call(PROJECT_CONFIG_SET, changes(("debuggers.dut.permissions.allow_flash", True)))
        # A truthy value the schema would not accept never gets as far as the
        # direction check: the shipped schema refuses it on the way in. Either
        # refusal is the same outcome (nothing applied) and the point is that
        # neither of them writes.
        coerced = tools.call(PROJECT_CONFIG_SET, changes(("debuggers.dut.permissions.allow_reset", 1)))
    finally:
        tools.close()

    assert refused["error_type"] == "permission_widening_denied"
    assert refused["widened_keys"] == ["debuggers.dut.permissions.allow_flash"]
    assert refused["retry_safe"] is False
    assert "false is the only value" in refused["summary"]
    assert coerced["ok"] is False
    assert coerced["error_type"] in ("permission_widening_denied", "invalid_argument")
    assert path.read_bytes() == before
    assert document_of(path)["provenance"] == PROVENANCE
    # The machine-readable half of the same refusal has to describe the same
    # thing. This result's `widened_keys` came out of the request and nothing in
    # the document would have moved, so a catalogue entry that spoke only of a
    # before/after delta made one refusal say two incompatible things.
    meaning = (lookup_remedy("permission_widening_denied") or ErrorRemedy("", ())).meaning
    assert "carried a permission value that was not `false`" in meaning
    assert "as the request spelled them" in meaning
    assert "would have moved nothing" in meaning
    assert refused["remediation"] == remediation_fields("permission_widening_denied")["remediation"]


def test_the_false_only_rule_does_not_reach_the_command_line(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An operator's own shell is not the surface this ratchet was decided for.

    The owner's own words: "we only nail the MCP shut, the
    rest is left to the user and the agent CLI". So the human actor keeps the
    authorization check (`allow_config_permissions_write` still gates the
    permissions half for everybody) and is not held to the direction."""
    workspace, path = bench(tmp_path, monkeypatch, **{CONFIG_PERMISSIONS_RIGHT: True})
    assert document_of(path)["debuggers"]["dut"]["permissions"]["allow_flash"] is False

    widened = project_config_set(
        workspace,
        load_authoritative_config(workspace),
        [{"key": "debuggers.dut.permissions.allow_flash", "value": True}],
        open_holds=None,
        actor="human",
        via="cli:test",
    )

    assert widened["ok"] is True, widened
    assert document_of(path)["debuggers"]["dut"]["permissions"]["allow_flash"] is True
    # The authority is `human` and what the file records is what this process
    # could tell, which with no terminal answering is the surface alone. The two
    # are separate questions; `via` names the command either way.
    assert document_of(path)["provenance"]["last_modified_by"] == "cli"
    assert document_of(path)["provenance"]["last_modified_via"] == "cli:test"

    # The grant itself is not waived for them: a bench that closed the
    # permissions half is closed to the command line too.
    closed = project_config_set(
        workspace,
        load_authoritative_config(workspace),
        [{"key": "permissions.allow_config_permissions_write", "value": False}],
        open_holds=None,
        actor="human",
        via="cli:test",
    )
    assert closed["ok"] is True, closed
    refused = project_config_set(
        workspace,
        load_authoritative_config(workspace),
        [{"key": "debuggers.dut.permissions.allow_mass_erase", "value": True}],
        open_holds=None,
        actor="human",
        via="cli:test",
    )
    assert refused["error_type"] == "permission_denied"
    assert refused["permission"] == f"permissions.{CONFIG_PERMISSIONS_RIGHT}"


# ---------------------------------------------------------------------------
# The two grants the schema puts on a section rather than in a permissions block.


def test_the_section_grants_a_generation_writes_can_be_narrowed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`debug.allow_all_symbols` and `artifacts.allow_upload` are permissions.

    A generation writes both `true` like every other permission. Leaving them out
    of the key model left two things a generated bench grants that an operator
    could take back only by opening the YAML, which is the single thing
    the ratchet exists to stop."""
    workspace, path = bench(tmp_path, monkeypatch, **{CONFIG_PERMISSIONS_RIGHT: True})
    opened = document_of(path)
    opened["debug"]["allow_all_symbols"] = True
    opened["artifacts"]["allow_upload"] = True
    path.write_text(yaml.safe_dump(opened, sort_keys=False), encoding="utf-8")

    tools = service(workspace)
    try:
        described = tools.call(PROJECT_CONFIG_DESCRIBE)
        narrowed = tools.call(
            PROJECT_CONFIG_SET,
            changes(("debug.allow_all_symbols", False), ("artifacts.allow_upload", False)),
        )
        # Still one way, on these two like on every other permission.
        refused = tools.call(PROJECT_CONFIG_SET, changes(("artifacts.allow_upload", True)))
    finally:
        tools.close()

    writable = {entry["key"]: entry["right"] for entry in described["writable_keys"]}
    assert writable["debug.allow_all_symbols"] == CONFIG_PERMISSIONS_RIGHT
    assert writable["artifacts.allow_upload"] == CONFIG_PERMISSIONS_RIGHT
    assert narrowed["ok"] is True, narrowed
    assert sorted(narrowed["permissions_changed"]) == ["artifacts.allow_upload", "debug.allow_all_symbols"]
    written = document_of(path)
    assert written["debug"]["allow_all_symbols"] is False
    assert written["artifacts"]["allow_upload"] is False
    assert refused["error_type"] == "permission_widening_denied"
    # Their list-valued neighbours stay out of reach: this surface writes scalars.
    assert document_of(path)["debug"]["allowed_symbols"] == opened["debug"]["allowed_symbols"]


def test_a_denied_section_grant_is_explained_as_the_permission_it_is(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The refusal a caller is handed has to describe the request it refused.

    Both keys are gated by `allow_config_permissions_write`, and the catalogue
    entry a caller reads said the change had "reached a `permissions:` block".
    Neither of these sits in one. An agent denied on `artifacts.allow_upload` was
    handed a machine-readable explanation of a request it had not made, and the
    right conclusion (this is a permission and an operator decides it) was left
    for it to guess."""
    workspace, path = bench(tmp_path, monkeypatch, **{CONFIG_DESCRIPTION_RIGHT: True})
    opened = document_of(path)
    opened["debug"]["allow_all_symbols"] = True
    opened["artifacts"]["allow_upload"] = True
    path.write_text(yaml.safe_dump(opened, sort_keys=False), encoding="utf-8")

    tools = service(workspace)
    try:
        for key in ("artifacts.allow_upload", "debug.allow_all_symbols"):
            refused = tools.call(PROJECT_CONFIG_SET, changes((key, False)))
            assert refused["error_type"] == "permission_denied", refused
            assert refused["permission"] == f"permissions.{CONFIG_PERMISSIONS_RIGHT}"
            assert refused["denied_keys"] == [key]
            # And the advice it carries is the catalogue's, for this exact grant.
            assert refused["remediation"] == remediation_fields("permission_denied", CONFIG_PERMISSIONS_RIGHT)["remediation"]
        # Both at once, and nothing is written for either.
        both = tools.call(PROJECT_CONFIG_SET, changes(("artifacts.allow_upload", False), ("debug.allow_all_symbols", False)))
        assert both["error_type"] == "permission_denied"
        assert both["denied_keys"] == ["artifacts.allow_upload", "debug.allow_all_symbols"]
    finally:
        tools.close()

    # The catalogue entry those refusals are derived from has to describe the
    # request that was actually made. Neither key sits in a `permissions:` block.
    meaning = (lookup_remedy("permission_denied", CONFIG_PERMISSIONS_RIGHT) or ErrorRemedy("", ())).meaning
    assert "artifacts.allow_upload" in meaning
    assert "debug.allow_all_symbols" in meaning
    assert "not only the ones inside a `permissions:` block" in meaning

    written = document_of(path)
    assert written["artifacts"]["allow_upload"] is True
    assert written["debug"]["allow_all_symbols"] is True


def test_the_frozen_notice_names_the_section_grants_too(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`frozen_permissions` is the whole list or it is misleading.

    An operator reads it as "this is what the bench is now allowed to do". A
    grant missing from it reads as "not granted", which for `allow_upload` and
    `allow_all_symbols` would be the opposite of what the file says."""
    workspace, path = bench(tmp_path, monkeypatch, **{CONFIG_PERMISSIONS_RIGHT: True})
    opened = document_of(path)
    opened["debug"]["allow_all_symbols"] = True
    opened["artifacts"]["allow_upload"] = True
    path.write_text(yaml.safe_dump(opened, sort_keys=False), encoding="utf-8")

    tools = service(workspace)
    try:
        froze = tools.call(PROJECT_CONFIG_SET, changes(("permissions.allow_config_permissions_write", False)))
    finally:
        tools.close()

    frozen = froze["permissions_frozen"]["frozen_permissions"]
    assert frozen["debug.allow_all_symbols"] is True
    assert frozen["artifacts.allow_upload"] is True
    assert frozen["permissions.allow_config_permissions_write"] is False
    # And the notice claims only what it closes: this call, not the whole file.
    assert "project_config_set" in froze["permissions_frozen"]["summary"]
    assert "agentic-hil init --force" in " ".join(froze["permissions_frozen"]["next_steps"])


def test_describe_scopes_the_direction_to_the_call_that_has_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The guidance an agent reads before writing says which call it is about.

    `project_config_describe` told callers that closing the permissions grant was
    the last permission change this server could make "at all", which is not
    true of a server that still holds `allow_config_write`: regenerating writes
    permissions again. Both branches name the call now."""
    workspace, path = bench(tmp_path, monkeypatch, **{CONFIG_PERMISSIONS_RIGHT: True})
    tools = service(workspace)
    try:
        open_steps = " ".join(tools.call(PROJECT_CONFIG_DESCRIBE)["next_steps"])
    finally:
        tools.close()

    assert "narrowed and never widened through `project_config_set`" in open_steps
    assert "the last permission change this call can make" in open_steps
    assert "project_config_create" in open_steps

    closed = document_of(path)
    closed["permissions"][CONFIG_PERMISSIONS_RIGHT] = False
    path.write_text(yaml.safe_dump(closed, sort_keys=False), encoding="utf-8")
    tools = service(workspace)
    try:
        closed_steps = " ".join(tools.call(PROJECT_CONFIG_DESCRIBE)["next_steps"])
    finally:
        tools.close()

    assert "fixed for this call" in closed_steps
    assert "through `project_config_set` in either direction" in closed_steps


def test_the_schema_says_what_each_door_does_to_a_permission() -> None:
    """The shipped schema is a contract a host may show verbatim.

    It promised that no call on the MCP surface writes `true` into a permission,
    which `project_config_create` does on any entry a regeneration meets for the
    first time. Scoped to the call that holds the direction, with the other door
    described as it behaves."""
    schema = config_schema()
    top = schema["description"]
    project = schema["properties"]["permissions"]["description"]
    write_grant = schema["properties"]["permissions"]["properties"]["allow_config_write"]["description"]
    permissions_grant = schema["properties"]["permissions"]["properties"]["allow_config_permissions_write"]["description"]

    assert "through project_config_set is false and nothing else" in top
    assert "regenerating it is a separate door" in top
    assert "project_config_set writes false into a permission and no other value" in project
    assert "gives an entry it discovers for the first time the same generated defaults" in project
    assert "is not the narrowing path and does not obey its ratchet" in write_grant
    assert "the last permission change it can make through project_config_set" in permissions_grant
