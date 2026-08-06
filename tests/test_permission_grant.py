"""`agentic-hil grant` and `agentic-hil revoke`: the gate on the one-way street.

hardci-hq#96 built the ratchet — an agent writes `false` into a permission over
MCP and never `true` — and answered the other direction for a configuration that
does not exist yet: a generation opens everything. For one that does exist there
was no answer at all, and the three routes an operator had were a dead end each:
`project_config_set` only narrows, `init --force` calls `carry_over_permissions`
and so copies the `false` forward, and deleting the file does come back open
while throwing away the baudrate, the `resource_id`, the `state_root` and every
artifact root somebody set. hardci-hq#102 was filed after its owner took the
third route and paid exactly that.

The decisive test here is therefore not "a permission changed". It is
`test_the_bench_from_the_issue_is_repaired_without_losing_the_file`: the
permission opens *and* everything the third route destroys is still in the file
afterwards. That is the whole of what this command is for.

The rest defend the boundary it must not move. `grant` is a command and not a
tool: `test_no_tool_list_reaches_grant_or_revoke` asserts that against the
shipped schemas, the MCP handshake and the dispatch table rather than against a
reading of the code, and `test_the_mcp_ratchet_is_exactly_what_it_was` shows the
same file that this command opens still refuses `true` over MCP.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
import yaml
from conftest import write_authoritative_config

from agentic_hil.config import load_authoritative_config
from agentic_hil.configwrite import (
    PERMISSION_COMMAND_VALUES,
    PROJECT_CONFIG_SET,
    resolve_permission_key,
    set_permission,
)
from agentic_hil.contracts import TOOL_SCHEMAS
from agentic_hil.devices import config_devices
from agentic_hil.knowledge import (
    CONFIG_DESCRIPTION_RIGHT,
    CONFIG_GRANT_COMMAND,
    CONFIG_PERMISSIONS_RIGHT,
    CONFIG_REVOKE_COMMAND,
    CONFIG_WRITE_RIGHT,
    PERMISSION_CHANGE_IN_OPEN_RUN,
    lookup_remedy,
    remediation_fields,
)
from agentic_hil.mcp import handle_mcp_message
from agentic_hil.tools import AgenticHILToolService

# `write_config` names device grants by their source flag rather than by the key
# the file ends up with: `allow_can_write` becomes `can_buses.<n>.allow_write`.
GRANT_SOURCES = ("allow_flash", "allow_reset", "allow_raw_debugger_commands", "allow_mass_erase", "allow_com_write", "allow_can_write")
COM_PORTS = 'com_ports:\n  dut_uart:\n    device: "COM9"\n    baudrate: 921600\n'
CAN_BUSES = 'can_buses:\n  dut:\n    adapter: "peak"\n    channel: "PCAN_USBBUS1"\n    bitrate: 250000\n'
PROVENANCE = {"created_by": "agent", "created_via": "mcp:project_config_create", "created_at": "2026-01-01T00:00:00Z", "agentic_hil_version": "0.0.0"}


def bench(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    device_permissions: dict[str, bool] | None = None,
    **grants: bool,
) -> tuple[Path, Path]:
    """A bench somebody grew: settings all over it, and every permission closed.

    The device grants start `false` because that is the state hardci-hq#102 is
    about — a configuration generated before the inversion, or narrowed since.
    The project grants start `false` too unless a test asks otherwise, so the
    default here is the hardest case: a file `project_config_set` cannot move a
    permission in at all."""
    workspace = (tmp_path / "workspace").resolve()
    root = Path(os.environ["APPDATA"]) / "permission-grant"
    path = write_authoritative_config(
        workspace,
        monkeypatch,
        config_root=root,
        config_version=2,
        permissions=device_permissions or {},
        com_ports_yaml=COM_PORTS,
        can_buses_yaml=CAN_BUSES,
        allowed_roots=["build", "out/release"],
        max_dump_size_bytes=4194304,
    )
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    document["provenance"] = dict(PROVENANCE)
    document["permissions"] = {CONFIG_WRITE_RIGHT: False, CONFIG_DESCRIPTION_RIGHT: False, CONFIG_PERMISSIONS_RIGHT: False, **grants}
    # The values the third route destroys, named here so a test can show they
    # survived: a non-default baudrate and bitrate, a chosen `resource_id`, the
    # artifact roots and the state root.
    document["can_buses"]["dut"]["resource_id"] = "bench-3-body-can"
    path.write_text("# A person wrote this bench.\n" + yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    monkeypatch.chdir(workspace)
    return workspace, path


def document_of(path: Path) -> dict:
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def run(workspace: Path, command: str, *keys: str, open_holds: dict[str, Any] | None = None) -> dict:
    return set_permission(workspace, load_authoritative_config(workspace), list(keys), command=command, open_holds=open_holds)


# ---------------------------------------------------------------------------
# The bench from the issue.


def test_the_bench_from_the_issue_is_repaired_without_losing_the_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`can_buses.dut.allow_write` opens, and nothing else in the file moves.

    The owner's own words on hardci-hq#102, and the point of the whole command:
    the workaround it replaces was deleting the configuration, which returns an
    open one and throws away everything else. So the assertion that matters is
    not that the flag is true — it is that the baudrate, the bitrate, the
    `resource_id`, the state root and the artifact roots are still there."""
    workspace, path = bench(tmp_path, monkeypatch)
    before = document_of(path)
    assert before["can_buses"]["dut"]["permissions"]["allow_write"] is False

    result = run(workspace, "grant", "can_buses.dut.allow_write")

    assert result["ok"] is True, result
    after = document_of(path)
    assert after["can_buses"]["dut"]["permissions"]["allow_write"] is True
    # Everything the deletion workaround costs, item by item, straight out of
    # the issue's own list.
    assert after["com_ports"]["dut_uart"]["baudrate"] == 921600
    assert after["can_buses"]["dut"]["bitrate"] == 250000
    assert after["can_buses"]["dut"]["resource_id"] == "bench-3-body-can"
    assert after["state_root"] == before["state_root"]
    assert after["artifacts"]["allowed_roots"] == ["build", "out/release"]
    assert after["debug"]["max_dump_size_bytes"] == 4194304
    # And the general form of the same claim: the document is what it was apart
    # from the one permission and the provenance record of having moved it.
    before["can_buses"]["dut"]["permissions"]["allow_write"] = True
    assert {key: value for key, value in after.items() if key != "provenance"} == {key: value for key, value in before.items() if key != "provenance"}
    # The header a person reads first still says whose file this is.
    assert path.read_text(encoding="utf-8").startswith("# A person wrote this bench.\n")


def test_the_change_is_reported_with_the_value_it_replaced(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace, _ = bench(tmp_path, monkeypatch)

    result = run(workspace, "grant", "can_buses.dut.allow_write")

    assert result["changed"] == [{"key": "can_buses.dut.permissions.allow_write", "previous_value": False, "value": True}]
    assert result["unchanged"] == []
    # A permission binds at startup and `project_config_reload_description`
    # re-reads none of them, so a result that did not say "restart" would leave
    # an operator watching a granted permission keep being refused.
    assert result["restart_required"] is True
    assert any("restart" in step.lower() for step in result["next_steps"])
    assert any("project_config_reload_description" in step for step in result["next_steps"])


def test_the_write_is_recorded_as_a_persons(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Provenance like any other write, with `human` as the actor."""
    workspace, path = bench(tmp_path, monkeypatch, device_permissions=dict.fromkeys(GRANT_SOURCES, True))
    assert document_of(path)["debuggers"]["dut"]["permissions"]["allow_mass_erase"] is True

    result = run(workspace, "revoke", "debuggers.dut.allow_mass_erase")

    assert result["ok"] is True, result
    provenance = document_of(path)["provenance"]
    assert provenance["last_modified_by"] == "human"
    assert provenance["last_modified_via"] == "cli:revoke"
    assert provenance["last_modified_keys"] == ["debuggers.dut.permissions.allow_mass_erase"]
    assert provenance["modification_count"] == 1
    # `created_by` answers who wrote the file, which a change does not alter.
    assert provenance["created_by"] == PROVENANCE["created_by"]
    assert "# Changed by a person through cli:revoke" in path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Both directions, and the terminal move.


def test_revoke_is_the_way_back_from_grant(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A CLI that only opened would be the next one-way street, reversed."""
    workspace, path = bench(tmp_path, monkeypatch)

    assert run(workspace, "grant", "debuggers.dut.allow_flash")["ok"] is True
    assert document_of(path)["debuggers"]["dut"]["permissions"]["allow_flash"] is True
    assert run(workspace, "revoke", "debuggers.dut.allow_flash")["ok"] is True

    assert document_of(path)["debuggers"]["dut"]["permissions"]["allow_flash"] is False
    assert document_of(path)["provenance"]["modification_count"] == 2


def test_the_terminal_move_is_no_longer_terminal_for_a_person(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`allow_config_permissions_write: false` closes MCP and not the shell.

    This is the hardest case hardci-hq#102 names: a bench whose permissions half
    is shut cannot be moved by `project_config_set` in either direction, whoever
    asks. Before this command, the only way back was regenerating the whole file
    or deleting it."""
    workspace, path = bench(tmp_path, monkeypatch)
    assert document_of(path)["permissions"][CONFIG_PERMISSIONS_RIGHT] is False

    reopened = run(workspace, "grant", f"permissions.{CONFIG_PERMISSIONS_RIGHT}")

    assert reopened["ok"] is True, reopened
    assert document_of(path)["permissions"][CONFIG_PERMISSIONS_RIGHT] is True
    # And closing it again from here says what closing it costs, in the words
    # the frozen notice already had — now naming the surgical way back too.
    froze = run(workspace, "revoke", f"permissions.{CONFIG_PERMISSIONS_RIGHT}")
    assert froze["ok"] is True, froze
    assert froze["permissions_frozen"]["closed_key"] == f"permissions.{CONFIG_PERMISSIONS_RIGHT}"
    steps = " ".join(froze["permissions_frozen"]["next_steps"])
    assert CONFIG_GRANT_COMMAND in steps
    assert "agentic-hil init --force" in steps


def test_the_mcp_ratchet_is_exactly_what_it_was(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The file this command opens still refuses `true` over MCP.

    The waiver belongs to the two commands and to nothing else: the permissions
    grant is consulted for every MCP caller, and the direction rule holds even
    when it is granted."""
    workspace, path = bench(tmp_path, monkeypatch, **{CONFIG_PERMISSIONS_RIGHT: True})
    tools = AgenticHILToolService(load_authoritative_config(workspace), frontend="mcp")
    try:
        widening = tools.call(PROJECT_CONFIG_SET, {"changes": [{"key": "can_buses.dut.permissions.allow_write", "value": True}]})
        assert widening["error_type"] == "permission_widening_denied"
        assert document_of(path)["can_buses"]["dut"]["permissions"]["allow_write"] is False

        # Narrowing still works over MCP, so the ratchet is intact rather than
        # merely broken in both directions.
        assert tools.call(PROJECT_CONFIG_SET, {"changes": [{"key": "debuggers.dut.permissions.allow_reset", "value": False}]})["ok"] is True
    finally:
        tools.close()

    # And the command line opens the same key on the same file a moment later.
    assert run(workspace, "grant", "can_buses.dut.allow_write")["ok"] is True
    assert document_of(path)["can_buses"]["dut"]["permissions"]["allow_write"] is True


def test_the_waiver_is_not_reachable_with_an_agents_provenance(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Even inside this process, the waiver refuses a non-human actor.

    `waive_permissions_grant` is a keyword of `project_config_set` and therefore
    is one import away for anything running in the server. It is bound to
    `ACTOR_HUMAN` at the entry point, so a call that carried it with the agent's
    own actor gets the refusal it would have got without it."""
    from agentic_hil.configwrite import project_config_set

    workspace, path = bench(tmp_path, monkeypatch)

    refused = project_config_set(
        workspace,
        load_authoritative_config(workspace),
        [{"key": "can_buses.dut.permissions.allow_write", "value": True}],
        open_holds=None,
        waive_permissions_grant=True,
    )

    assert refused["error_type"] == "permission_denied"
    assert refused["permission"] == CONFIG_PERMISSIONS_RIGHT
    assert document_of(path)["can_buses"]["dut"]["permissions"]["allow_write"] is False


# ---------------------------------------------------------------------------
# Not a tool.


def test_no_tool_list_reaches_grant_or_revoke(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Asserted against what is shipped, not against a reading of the code.

    Three surfaces, because "an agent cannot call it" is only true if all three
    say so: the schemas the contract module publishes, the list the MCP
    handshake actually answers `tools/list` with, and the dispatch table behind
    `tools/call`."""
    names = ("grant", "revoke", "agentic-hil grant", "permission_grant", "project_config_grant")
    assert not [name for name in names if name in TOOL_SCHEMAS]

    workspace, path = bench(tmp_path, monkeypatch)
    config = load_authoritative_config(workspace)
    tools = AgenticHILToolService(config, frontend="mcp")
    try:
        listed = handle_mcp_message({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, tools)
        published = {str(tool["name"]) for tool in listed["result"]["tools"]}
        assert published
        assert not published & set(names)
        # Nothing published mentions granting a permission either, so an agent
        # cannot find it by reading the list it does get.
        assert not [name for name in published if "grant" in name or name.endswith("revoke")]

        for name in names:
            answered = handle_mcp_message({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": name, "arguments": {"keys": ["can_buses.dut.allow_write"]}}}, tools)
            body = json.loads(answered["result"]["content"][0]["text"]) if "result" in answered else answered
            assert body.get("ok") is not True, (name, body)
        assert document_of(path)["can_buses"]["dut"]["permissions"]["allow_write"] is False
    finally:
        tools.close()


def test_the_command_is_only_on_the_command_line(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """And it is on the command line: the parser answers to both verbs."""
    from agentic_hil.cli import build_parser

    workspace, path = bench(tmp_path, monkeypatch)
    parser = build_parser()

    parsed = parser.parse_args(["grant", "can_buses.dut.allow_write", "debuggers.dut.allow_flash"])
    assert parsed.command == "grant"
    assert parsed.keys == ["can_buses.dut.allow_write", "debuggers.dut.allow_flash"]
    assert set(PERMISSION_COMMAND_VALUES) == {"grant", "revoke"}
    assert PERMISSION_COMMAND_VALUES["grant"] is True
    assert PERMISSION_COMMAND_VALUES["revoke"] is False

    # A verb with no key names nothing and is refused by the parser rather than
    # taken as "everything".
    with pytest.raises(SystemExit):
        parser.parse_args(["grant"])
    assert document_of(path)["can_buses"]["dut"]["permissions"]["allow_write"] is False
    assert workspace.exists()


# ---------------------------------------------------------------------------
# Single keys: the decision, as a test.


def test_a_whole_entry_is_not_a_name_this_command_takes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No section form and no wildcard — the open question, decided.

    A section form would mean whatever `config_rule_fields` reads out of the
    shipped schema at the moment it runs, so a permission added in a later
    release would silently join every section form an operator had learned to
    type. A named key means the same thing forever. The convenience is kept
    without the blast radius: several keys in one command, applied together."""
    workspace, path = bench(tmp_path, monkeypatch)

    for spelling in ("can_buses.dut", "can_buses.dut.*", "can_buses.dut.permissions", "*"):
        refused = run(workspace, "grant", spelling)
        assert refused["ok"] is False, (spelling, refused)
        assert refused["error_type"] == "invalid_argument"
        assert [item["key"] for item in refused["rejected_keys"]] == [spelling]

    # `*` is a legal entry name — the schema allows `[A-Za-z0-9_.-]+`, and every
    # entry id in this file is the operator's to choose. So a star cannot be
    # reserved as a wildcard without taking a name away, and it is read as the
    # entry it spells: one this configuration does not have. That is a second
    # reason there is no wildcard, and it is not one anybody chose.
    star = run(workspace, "grant", "can_buses.*.allow_write")
    assert star["ok"] is False, star
    assert star["unknown_entry"] == "can_buses.*"
    assert document_of(path)["can_buses"]["dut"]["permissions"]["allow_write"] is False


def test_a_name_that_is_not_a_permission_is_answered_with_the_ones_that_are(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The refusal is the discoverability answer, so there is no wildcard to need.

    The keys come out of this document rather than out of the patterns: nobody
    can type `can_buses.<name>.permissions.allow_write`, and what an operator
    needs is the entry names their own bench has."""
    workspace, _ = bench(tmp_path, monkeypatch)

    refused = run(workspace, "grant", "can_buses.dut.bitrate", "nonsense")

    assert refused["ok"] is False
    rejected = {item["key"]: item["reason"] for item in refused["rejected_keys"]}
    assert set(rejected) == {"can_buses.dut.bitrate", "nonsense"}
    # A description key is refused for being one, and is told where descriptions
    # are changed instead.
    assert "description" in rejected["can_buses.dut.bitrate"]
    assert "adopt-hardware" in rejected["can_buses.dut.bitrate"]
    available = refused["permission_keys_here"]
    assert "can_buses.dut.permissions.allow_write" in available
    assert "debuggers.dut.permissions.allow_mass_erase" in available
    assert "com_ports.dut_uart.permissions.allow_write" in available
    assert f"permissions.{CONFIG_PERMISSIONS_RIGHT}" in available
    assert "debug.allow_all_symbols" in available
    assert "artifacts.allow_upload" in available
    # Every one of them is a permission and none is a description key.
    assert "can_buses.dut.bitrate" not in available


def test_an_entry_this_configuration_does_not_have_is_named_as_such(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`can_buses.ghost.allow_write` spells a key the model recognises.

    So it is not caught by the name check above and reaches the write path,
    where a permission does not bring a device into existence — the rule that
    was already there, reached from this command too."""
    workspace, path = bench(tmp_path, monkeypatch)

    refused = run(workspace, "grant", "can_buses.ghost.permissions.allow_write")

    assert refused["ok"] is False, refused
    assert refused["error_type"] == "invalid_argument"
    assert refused["unknown_entry"] == "can_buses.ghost"
    assert "can_buses" not in {*document_of(path)["can_buses"]} - {"dut"}


def test_several_keys_are_applied_together_or_not_at_all(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The convenience a section form was wanted for, without its blast radius."""
    workspace, path = bench(tmp_path, monkeypatch)

    opened = run(workspace, "grant", "debuggers.dut.allow_flash", "debuggers.dut.allow_reset", "can_buses.dut.allow_write")

    assert opened["ok"] is True, opened
    assert [item["key"] for item in opened["changed"]] == [
        "debuggers.dut.permissions.allow_flash",
        "debuggers.dut.permissions.allow_reset",
        "can_buses.dut.permissions.allow_write",
    ]
    permissions = document_of(path)["debuggers"]["dut"]["permissions"]
    assert permissions["allow_flash"] is True
    assert permissions["allow_reset"] is True
    # What was not named is not opened. That is the whole argument for single
    # keys over a section form.
    assert permissions["allow_mass_erase"] is False
    assert permissions["allow_raw_debugger_commands"] is False
    # One provenance record for the one call.
    assert document_of(path)["provenance"]["modification_count"] == 1

    before = path.read_bytes()
    refused = run(workspace, "grant", "debuggers.dut.allow_mass_erase", "not_a_key")
    assert refused["ok"] is False
    assert path.read_bytes() == before


# ---------------------------------------------------------------------------
# Both spellings of one key.


def test_the_short_key_and_the_long_one_name_the_same_permission(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`can_buses.dut.allow_write` is what the issue wrote and what a file reads like.

    `can_buses.dut.permissions.allow_write` is what `project_config_set` and
    `project_config_describe` name. Both resolve through the one key model."""
    workspace, path = bench(tmp_path, monkeypatch)

    short, reason = resolve_permission_key("can_buses.dut.allow_write")
    long, _ = resolve_permission_key("can_buses.dut.permissions.allow_write")
    assert reason is None
    assert short == long
    assert short is not None and short.key == "can_buses.dut.permissions.allow_write"

    assert run(workspace, "grant", "can_buses.dut.permissions.allow_write")["ok"] is True
    assert document_of(path)["can_buses"]["dut"]["permissions"]["allow_write"] is True
    # And the same key twice in one command is one change, not two writes.
    both = run(workspace, "revoke", "can_buses.dut.allow_write", "can_buses.dut.permissions.allow_write")
    assert [item["key"] for item in both["changed"]] == ["can_buses.dut.permissions.allow_write"]


def test_the_short_form_never_shadows_a_description_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """It is only tried after the long reading has failed to resolve.

    So a real key is read as the key it is, and only a spelling that names
    nothing at all is rewritten."""
    workspace, _ = bench(tmp_path, monkeypatch)

    resolved, reason = resolve_permission_key("can_buses.dut.bitrate")
    assert resolved is None
    assert reason is not None and "description" in reason

    # An entry name with dots in it is read from the right, exactly as the key
    # model reads one, so the short form inherits that rather than guessing.
    document = yaml.safe_load(Path(load_authoritative_config(workspace).config_path).read_text(encoding="utf-8"))
    document["can_buses"]["a.b"] = {"adapter": "peak", "channel": "PCAN_USBBUS2", "permissions": {"allow_write": False}}
    Path(load_authoritative_config(workspace).config_path).write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")

    dotted, _ = resolve_permission_key("can_buses.a.b.allow_write")
    assert dotted is not None
    assert dotted.entry == "a.b"
    assert dotted.key == "can_buses.a.b.permissions.allow_write"


# ---------------------------------------------------------------------------
# Already open.


def test_opening_what_is_already_open_is_a_no_op_and_not_a_change(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Not an error — asking for what is already so is how somebody makes sure.

    But not a change either, and the difference is visible in the file: nothing
    is written, so there is no provenance bump and no change marker in a header
    that would otherwise tell the next reader the file has moved."""
    workspace, path = bench(tmp_path, monkeypatch, device_permissions={"allow_can_write": True})
    assert document_of(path)["can_buses"]["dut"]["permissions"]["allow_write"] is True
    before = path.read_bytes()

    result = run(workspace, "grant", "can_buses.dut.allow_write")

    assert result["ok"] is True, result
    assert result["changed"] == []
    assert result["unchanged"] == [{"key": "can_buses.dut.permissions.allow_write", "value": True}]
    assert result["restart_required"] is False
    assert "no-op" in result["summary"]
    assert path.read_bytes() == before


def test_a_call_that_mixes_a_no_op_with_a_change_writes_only_the_change(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace, path = bench(tmp_path, monkeypatch, device_permissions={"allow_can_write": True})

    result = run(workspace, "grant", "can_buses.dut.allow_write", "debuggers.dut.allow_flash")

    assert result["ok"] is True, result
    assert [item["key"] for item in result["changed"]] == ["debuggers.dut.permissions.allow_flash"]
    assert [item["key"] for item in result["unchanged"]] == ["can_buses.dut.permissions.allow_write"]
    assert result["restart_required"] is True
    # The provenance names what actually moved, not what was asked for.
    assert document_of(path)["provenance"]["last_modified_keys"] == ["debuggers.dut.permissions.allow_flash"]


# ---------------------------------------------------------------------------
# Not while somebody holds the bench.


def test_a_permission_does_not_move_while_something_holds_the_bench(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """hardci-hq#80's rule, reached from the command line.

    Those holds were taken under the permissions this file states, so opening
    one underneath them would move the rules during the run they govern."""
    workspace, path = bench(tmp_path, monkeypatch)
    from agentic_hil.cli import bench_open_holds

    config = load_authoritative_config(workspace)
    uart = next(key for key in config_devices(config).lock_keys if key.startswith("com:"))
    tools = AgenticHILToolService(config, frontend="mcp")
    try:
        assert bench_open_holds(config) is None
        lease = tools.coordinator.acquire(uart)
        try:
            holds = bench_open_holds(config)
            assert holds is not None
            # A lease takes both, so both questions answer yes here. The run
            # below is the case where only the second one does.
            assert holds["owner_active"] is True
            assert holds["held_devices"] == [uart]
            assert [item["resource"] for item in holds["busy_devices"]] == [uart]

            before = path.read_bytes()
            refused = set_permission(workspace, config, ["can_buses.dut.allow_write"], command="grant", open_holds=holds)
            assert refused["error_type"] == PERMISSION_CHANGE_IN_OPEN_RUN
            assert refused["open_holds"] == holds
            assert refused["retry_safe"] is True
            assert refused["side_effect_committed"] is False
            assert refused["remediation"] == remediation_fields(PERMISSION_CHANGE_IN_OPEN_RUN)["remediation"]
            assert any("lease-status" in step for step in refused["remediation"])
            assert path.read_bytes() == before
        finally:
            lease.release()
        assert bench_open_holds(config) is None
    finally:
        tools.close()

    # And the same command goes through once nothing is held.
    assert run(workspace, "grant", "can_buses.dut.allow_write")["ok"] is True


def test_a_run_blocks_it_as_completely_as_a_lease(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """And a run is exactly the case the project lock does not see.

    `begin_run` takes the machine-wide device locks and no project lock; the
    project lock is a lease's. So a run between two of its own calls is invisible
    to the check that catches a session, and asking only that question would have
    let a permission move in the middle of somebody's test — the one thing
    hardci-hq#80 rules out. The device locks are the second question, and this is
    the case that makes them necessary rather than thorough."""
    workspace, _ = bench(tmp_path, monkeypatch)
    from agentic_hil.cli import bench_open_holds

    config = load_authoritative_config(workspace)
    tools = AgenticHILToolService(config, frontend="mcp")
    try:
        assert tools.call("bench_run_start", {"devices": [{"kind": "uart", "id": "dut_uart"}], "label": "boot-smoke"})["ok"] is True
        holds = bench_open_holds(config)
        assert holds is not None
        assert holds["owner_active"] is False
        uart = next(key for key in config_devices(config).lock_keys if key.startswith("com:"))
        assert [item["resource"] for item in holds["busy_devices"]] == [uart]
        assert holds["held_devices"] == [uart]
        assert holds["busy_devices"][0]["holder"]["frontend"] == "mcp"
        assert holds["busy_devices"][0]["holder"]["pid"] == os.getpid()

        refused = set_permission(workspace, config, ["can_buses.dut.allow_write"], command="grant", open_holds=holds)
        assert refused["error_type"] == PERMISSION_CHANGE_IN_OPEN_RUN
        assert refused["command"] == "agentic-hil grant"

        assert tools.call("bench_run_stop")["ok"] is True
        assert bench_open_holds(config) is None
    finally:
        tools.close()


def test_a_typo_is_answered_before_somebody_elses_run_is(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An operator whose command was a typo is told about the typo.

    Being sent to wait for a run that has nothing to do with the mistake is a
    refusal that costs a second attempt to discover."""
    workspace, _ = bench(tmp_path, monkeypatch)

    refused = set_permission(
        workspace,
        load_authoritative_config(workspace),
        ["can_buses.dut.allow_wrtie"],
        command="grant",
        open_holds={"owner_active": True, "held_devices": ["com:COM9"]},
    )

    assert refused["error_type"] == "invalid_argument"


# ---------------------------------------------------------------------------
# The catalogue.


def test_the_new_refusal_is_in_the_catalogue_with_both_halves() -> None:
    remedy = lookup_remedy(PERMISSION_CHANGE_IN_OPEN_RUN)
    assert remedy is not None
    payload = remedy.as_json()
    assert CONFIG_GRANT_COMMAND in payload["meaning"]
    assert "config_write_in_open_run" in payload["meaning"]
    assert any("lease-status" in step for step in payload["remediation"])
    # The wrong fix that looks right is named, which is the whole reason
    # `do_not` exists on these entries.
    assert any("by hand" in step for step in payload["do_not"])


def test_the_widening_refusal_now_names_the_surgical_way_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An agent that reports "this permission is needed" should name the command.

    Before hardci-hq#102 the only answer was `init --force`, which for a grown
    bench is not a repair. The refusal names both now, and says which is which."""
    workspace, _ = bench(tmp_path, monkeypatch, **{CONFIG_PERMISSIONS_RIGHT: True})
    tools = AgenticHILToolService(load_authoritative_config(workspace), frontend="mcp")
    try:
        refused = tools.call(PROJECT_CONFIG_SET, {"changes": [{"key": "can_buses.dut.permissions.allow_write", "value": True}]})
    finally:
        tools.close()

    remediation = " ".join(refused["remediation"])
    assert CONFIG_GRANT_COMMAND in remediation
    assert "agentic-hil init --force" in remediation
    assert CONFIG_REVOKE_COMMAND == "agentic-hil revoke"
