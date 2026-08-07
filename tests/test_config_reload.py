"""A changed device description does not need the MCP server restarted.

The reported session is one sentence long: *"ich musste den
MCP-Server auch manuell neustarten, wenn ich etwas an der Konfiguration der
Geraete geaendert habe"*. A board was plugged in, written into the authoritative
file, and stayed invisible to the running server until somebody restarted the
agent host's MCP server — because a server parses its configuration once and
enforces that one document until it exits.

That rule was written for the **permission** half and it still holds there: the
grants were taken as a whole, and a document that turned up underneath a running
server may not widen them. What it also did, and was never meant to do, was hold
the **description** half hostage.

So these tests are in four groups.

The first is the report: a board written into the file becomes usable without a
restart. The second is the property the whole thing rests on — the permissions do
not move, in either direction, and a device this server has never seen arrives
with nothing. The third is the refusals: a held bench, an open incident, a file
that is gone, unreadable, or will not load. The fourth is what the answers say
afterwards, which is where a reload could quietly start lying: `config_status`
must stop calling the description it just took stale, and must not thereby hide
that the permissions in force are older than the file.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml
from conftest import FAKE_STLINK, write_authoritative_config

from agentic_hil.config import load_authoritative_config, permission_summary
from agentic_hil.configreload import (
    NOT_RELOADED_SECTIONS,
    PROJECT_CONFIG_RELOAD,
    RELOAD_IN_OPEN_RUN_ERROR,
    RELOADED_SECTIONS,
    merged_description,
    reload_description,
)
from agentic_hil.configstate import (
    DESCRIPTION_FROM_RELOAD,
    DESCRIPTION_FROM_STARTUP,
    STATE_CHANGED,
    STATE_MISSING,
    STATE_UNCHANGED,
    STATE_UNREADABLE,
)
from agentic_hil.contracts import MCP_TOOL_NAMES, TOOL_SCHEMAS
from agentic_hil.knowledge import CONFIG_SHAPE_URI, ERROR_CATALOGUE, read_resource
from agentic_hil.tools import AgenticHILToolService

COM_PORTS = 'com_ports:\n  dut_uart:\n    device: "COM9"\n    baudrate: 115200\n'


def bench(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **kwargs) -> tuple[Path, Path]:
    """One stlink probe and one COM port, version 2, every device grant on.

    Version 2 because that is what a generated configuration is and what the
    "arrives without a grant" property has to be shown against: under version 2
    reading is free, so a new device being readable is *not* a grant leaking
    through, and every mutating permission on it still has to be closed."""
    workspace = (tmp_path / "workspace").resolve()
    root = Path(os.environ["APPDATA"]) / "config-reload"
    kwargs.setdefault("com_ports_yaml", COM_PORTS)
    path = write_authoritative_config(
        workspace,
        monkeypatch,
        config_root=root,
        config_version=2,
        debugger_type="stlink",
        **kwargs,
    )
    monkeypatch.chdir(workspace)
    return workspace, path


def service(workspace: Path) -> AgenticHILToolService:
    return AgenticHILToolService(load_authoritative_config(workspace), frontend="mcp")


def rewrite(path: Path, edit) -> None:
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    edit(document)
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")


def add_second_board(document: dict) -> None:
    """The report, literally: a board plugged in and written down afterwards."""
    template = document["debuggers"]["dut"]
    document["debuggers"]["spare"] = {
        **template,
        "type": "stlink",
        "executable": FAKE_STLINK.as_posix(),
        "probe_id": "SPARE-0002",
        # A hand-widened entry, wider than any generation writes: the
        # last two are generated false. That is exactly what
        # must not reach a running server through a reload.
        "permissions": {"allow_flash": True, "allow_reset": True, "allow_raw_debugger_commands": True, "allow_mass_erase": True},
    }
    document["debuggers"]["dut"]["probe_id"] = "DUT-0001"
    document["com_ports"]["spare_uart"] = {"device": "COM11", "baudrate": 9600, "permissions": {"allow_write": True}}


# ---------------------------------------------------------------------------
# The report: a board becomes usable without a restart.


def test_a_board_written_into_the_file_is_visible_without_a_restart(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace, path = bench(tmp_path, monkeypatch)
    tools = service(workspace)
    try:
        assert sorted(tools.config.debuggers) == ["dut"]
        rewrite(path, add_second_board)
        # Still the old bench, because nothing reloads on its own. That is the
        # half of the decision this change does not touch.
        assert sorted(tools.config.debuggers) == ["dut"]
        assert tools.call("com_ports_list")["config_status"]["state"] == STATE_CHANGED

        result = tools.call(PROJECT_CONFIG_RELOAD)

        assert result["ok"] is True
        assert result["reloaded"] is True
        assert result["description_changed"] is True
        assert sorted(tools.config.debuggers) == ["dut", "spare"]
        assert sorted(tools.config.com_ports) == ["dut_uart", "spare_uart"]
        assert tools.config.debuggers["spare"].probe_id == "SPARE-0002"
        assert tools.config.com_ports["spare_uart"].baudrate == 9600
        # The board is reachable now, through the tools rather than only in the
        # config object: it shows up in the answer an agent actually reads.
        assert "spare_uart" in tools.call("com_ports_list")["ports"]
    finally:
        tools.close()

    assert "debuggers.spare.probe_id" in result["description_changes"]
    assert "com_ports.spare_uart.device" in result["description_changes"]


def test_a_changed_probe_id_reaches_the_bound_backend(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Not only the config object: the collaborators built from it move too.

    The service constructs a backend, a COM service, a CAN service and an
    artifact manager from one configuration. A reload that swapped only
    `self.config` would leave every one of them answering out of the old one."""
    workspace, path = bench(tmp_path, monkeypatch, probe_id="OLD-PROBE")
    tools = service(workspace)
    try:
        assert tools.backend.config.debugger.probe_id == "OLD-PROBE"
        rewrite(path, lambda document: document["debuggers"]["dut"].update({"probe_id": "NEW-PROBE"}))
        assert tools.call(PROJECT_CONFIG_RELOAD)["ok"] is True

        assert tools.config.debugger.probe_id == "NEW-PROBE"
        assert tools.backend.config.debugger.probe_id == "NEW-PROBE"
        assert tools.com_ports.config.debugger.probe_id == "NEW-PROBE"
        assert tools.can_buses.config.debugger.probe_id == "NEW-PROBE"
        assert tools.artifacts.config.debugger.probe_id == "NEW-PROBE"
        assert tools.coordinator.config.debugger.probe_id == "NEW-PROBE"
    finally:
        tools.close()


def test_a_first_debugger_binds_a_server_that_had_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The backend object itself has to be rebuilt, not reconfigured.

    A bench with no debugger holds an `UnboundDebuggerBackend`, and no amount of
    handing it a new configuration turns it into an STLink one. This is the case
    where the reload has to replace the collaborator rather than update it."""
    workspace, path = bench(tmp_path, monkeypatch)
    rewrite(path, lambda document: document.__setitem__("debuggers", {}))
    tools = service(workspace)
    try:
        assert tools.config.debugger is None
        assert type(tools.backend).__name__ == "UnboundDebuggerBackend"
        assert tools.call("debugger_info")["error_type"] == "not_supported"

        rewrite(
            path,
            lambda document: document.__setitem__(
                "debuggers",
                {"dut": {"type": "stlink", "executable": FAKE_STLINK.as_posix(), "probe_id": "DUT-0001", "permissions": {}}},
            ),
        )
        assert tools.call(PROJECT_CONFIG_RELOAD)["ok"] is True

        assert tools.config.debugger_id == "dut"
        assert type(tools.backend).__name__ == "STLinkBackend"
        assert tools.call("debugger_info")["ok"] is True
    finally:
        tools.close()


def test_a_second_board_does_not_repoint_a_bound_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The binding follows the name this server drives, not the file's order."""
    workspace, path = bench(tmp_path, monkeypatch, probe_id="DUT-0001")
    tools = service(workspace)
    try:
        assert tools.config.debugger_id == "dut"
        rewrite(path, add_second_board)
        assert tools.call(PROJECT_CONFIG_RELOAD)["ok"] is True
        assert tools.config.debugger_id == "dut"
        assert tools.config.debugger.probe_id == "DUT-0001"
    finally:
        tools.close()


# ---------------------------------------------------------------------------
# The permissions do not move. Not narrowed, not widened, not compared.


def test_permissions_on_disk_do_not_move_across_a_reload(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The proof the decision asks for, in both directions at once.

    The file is edited so that one grant is turned on and another turned off,
    and the description is edited in the same write so the reload definitely
    took *something*. Afterwards the permission state is compared field by field
    against the one this server started with — not against the file."""
    workspace, path = bench(tmp_path, monkeypatch, probe_id="DUT-0001")
    tools = service(workspace)
    try:
        before = permission_summary(tools.config)
        assert before["debuggers"]["dut"]["allow_flash"] is True
        assert before["debuggers"]["dut"]["allow_mass_erase"] is False

        def widen_and_narrow(document: dict) -> None:
            document["debuggers"]["dut"]["permissions"]["allow_mass_erase"] = True
            document["debuggers"]["dut"]["permissions"]["allow_flash"] = False
            document["com_ports"]["dut_uart"]["permissions"]["allow_write"] = False
            document["artifacts"]["allow_upload"] = False
            document["debug"]["allow_all_symbols"] = False
            document["debuggers"]["dut"]["probe_id"] = "MOVED-0002"

        rewrite(path, widen_and_narrow)
        result = tools.call(PROJECT_CONFIG_RELOAD)

        assert result["ok"] is True
        assert result["permissions_reloaded"] is False
        # The description did move, so this is not the trivial "nothing changed".
        assert tools.config.debugger.probe_id == "MOVED-0002"
        # And the permissions did not, byte for byte.
        assert permission_summary(tools.config) == before
        # Reported rather than hidden: every grant the file states that this
        # server is not enforcing is listed, and the list is not empty.
        assert "debuggers.dut.allow_mass_erase" in result["permission_differences"]
        assert "debuggers.dut.allow_flash" in result["permission_differences"]
        assert "com_ports.dut_uart.allow_write" in result["permission_differences"]
        assert "artifacts.allow_upload" in result["permission_differences"]
        assert "debug.allow_all_symbols" in result["permission_differences"]
        assert result["restart_required_for"] == result["permission_differences"]
        assert "restart" in result["summary"].lower()
    finally:
        tools.close()


def test_a_narrowed_permission_is_still_not_adopted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """"Not only narrowing" is the sharpened condition, so it gets its own test.

    A reload that quietly took the narrower side would be defensible and is
    still wrong: it makes the permission state depend on when somebody called a
    tool, and the whole point is that it depends on nothing after startup."""
    workspace, path = bench(tmp_path, monkeypatch)
    tools = service(workspace)
    try:
        rewrite(path, lambda document: document["debuggers"]["dut"]["permissions"].update({"allow_flash": False, "allow_reset": False}))
        assert tools.call(PROJECT_CONFIG_RELOAD)["ok"] is True
        assert tools.config.debugger.permissions.allow_flash is True
        assert tools.config.debugger.permissions.allow_reset is True
        assert tools.debugger_permissions.allow_flash is True
    finally:
        tools.close()


def test_the_permission_model_version_is_not_re_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`version` decides whether reads need a grant at all, so it is a permission."""
    workspace, path = bench(tmp_path, monkeypatch)
    tools = service(workspace)
    try:
        assert tools.config.config_version == 2
        assert tools.config.read_free is True
        rewrite(path, lambda document: document["debuggers"]["dut"]["permissions"].update({"allow_probe": True}))
        rewrite(path, lambda document: document.__setitem__("version", 1))
        result = tools.call(PROJECT_CONFIG_RELOAD)
        assert result["ok"] is True
        assert tools.config.config_version == 2
        assert "version" in result["permission_differences"]
    finally:
        tools.close()


def test_a_device_this_server_never_saw_arrives_with_no_grant(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Absent is denied, and the gate that denies it is the ordinary one.

    The file grants the new board everything. What the server holds for it is
    nothing at all, so every mutating permission on it is false — and the checks
    that read those flags are the same checks every other device goes through."""
    workspace, path = bench(tmp_path, monkeypatch, probe_id="DUT-0001")
    tools = service(workspace)
    try:
        rewrite(path, add_second_board)
        result = tools.call(PROJECT_CONFIG_RELOAD)
        assert result["ok"] is True

        spare = tools.config.debuggers["spare"]
        assert spare.permissions.allow_flash is False
        assert spare.permissions.allow_reset is False
        assert spare.permissions.allow_raw_debugger_commands is False
        assert spare.permissions.allow_mass_erase is False
        assert tools.config.com_ports["spare_uart"].permissions.allow_write is False
        # Reading is free from version 2 on: exclusivity, not a grant, is what
        # protects a read. So the board is usable for exactly what it should be.
        assert tools.config.probe_allowed(spare) is True
        assert tools.config.com_read_allowed(tools.config.com_ports["spare_uart"]) is True
        # And the refusal a caller actually meets is the ordinary permission gate.
        denied = tools.call("com_write", {"port_id": "spare_uart", "text": "x"})
        assert denied["ok"] is False
        assert denied["error_type"] == "permission_denied"
        # Untouched entries keep exactly what this server loaded for them.
        assert tools.config.debuggers["dut"].permissions.allow_flash is True
        assert tools.config.com_ports["dut_uart"].permissions.allow_write is True
    finally:
        tools.close()


def test_a_renamed_entry_is_a_new_entry_and_arrives_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Carrying grants by name is the only rule, and a rename is not a match."""
    workspace, path = bench(tmp_path, monkeypatch, probe_id="DUT-0001")
    tools = service(workspace)
    try:

        def rename(document: dict) -> None:
            document["debuggers"] = {"board": document["debuggers"].pop("dut")}

        rewrite(path, rename)
        assert tools.call(PROJECT_CONFIG_RELOAD)["ok"] is True
        assert sorted(tools.config.debuggers) == ["board"]
        # It is the only entry, so the server binds it — and it holds nothing.
        assert tools.config.debugger_id == "board"
        assert tools.config.debuggers["board"].permissions.allow_flash is False
    finally:
        tools.close()


def test_the_sections_outside_the_device_description_are_not_re_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Refused by name and reported, rather than quietly skipped."""
    workspace, path = bench(tmp_path, monkeypatch)
    tools = service(workspace)
    try:
        before = tools.config

        def move_everything_else(document: dict) -> None:
            document["debug"]["max_dump_size_bytes"] = 4096
            document["artifacts"]["max_upload_size_mb"] = 99
            document["reports"]["directory"] = ".agentic-hil/other-reports"
            document["logs"]["directory"] = ".agentic-hil/other-logs"
            document["recovery"] = {"auto_recover": "off"}

        rewrite(path, move_everything_else)
        result = tools.call(PROJECT_CONFIG_RELOAD)

        assert result["ok"] is True
        assert tools.config.debug == before.debug
        assert tools.config.artifacts == before.artifacts
        assert tools.config.reports == before.reports
        assert tools.config.logs == before.logs
        assert tools.config.recovery == before.recovery
        assert tools.config.workspace_root == before.workspace_root
        assert tools.config.state_root == before.state_root
        named = {entry["section"] for entry in result["not_reloaded_sections"]}
        assert {"debug", "artifacts", "reports", "logs", "recovery", "validation", "version", "workspace_root", "state_root", "permissions"} <= named
        assert result["reloaded_sections"] == list(RELOADED_SECTIONS)
        assert all(entry["why"] for entry in result["not_reloaded_sections"])
    finally:
        tools.close()


# ---------------------------------------------------------------------------
# The refusals.


def test_refused_while_a_run_holds_the_bench(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace, path = bench(tmp_path, monkeypatch)
    tools = service(workspace)
    try:
        assert tools.call("bench_run_start", {"devices": [{"kind": "uart", "id": "dut_uart"}], "label": "reload"})["ok"] is True
        rewrite(path, add_second_board)
        refused = tools.call(PROJECT_CONFIG_RELOAD)

        assert refused["ok"] is False
        assert refused["error_type"] == RELOAD_IN_OPEN_RUN_ERROR
        assert refused["open_holds"]["run_active"] is True
        assert refused["side_effect_committed"] is False
        assert refused["retry_safe"] is True
        assert sorted(tools.config.debuggers) == ["dut"]
        assert refused["remediation"] == list(ERROR_CATALOGUE[RELOAD_IN_OPEN_RUN_ERROR].remediation)

        # And the moment the run is closed, the same call goes through.
        assert tools.call("bench_run_stop")["ok"] is True
        assert tools.call(PROJECT_CONFIG_RELOAD)["ok"] is True
        assert sorted(tools.config.debuggers) == ["dut", "spare"]
    finally:
        tools.close()


def test_refused_while_a_lease_outside_a_run_holds_the_bench(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A COM session outlives `bench_run_stop` by design, so a run check alone
    would miss a board that is still held."""
    workspace, path = bench(tmp_path, monkeypatch)
    tools = service(workspace)
    try:
        assert tools.open_hardware_holds() is None
        rewrite(path, add_second_board)
        lease = tools.coordinator.acquire("com:COM9")
        try:
            refused = tools.call(PROJECT_CONFIG_RELOAD)
            assert refused["error_type"] == RELOAD_IN_OPEN_RUN_ERROR
            assert refused["open_holds"]["open_leases"] == [lease.lease_id]
            assert refused["open_holds"]["run_active"] is False
            assert sorted(tools.config.debuggers) == ["dut"]
        finally:
            lease.release()
        assert tools.call(PROJECT_CONFIG_RELOAD)["ok"] is True
        assert sorted(tools.config.debuggers) == ["dut", "spare"]
    finally:
        tools.close()


def test_refused_while_an_incident_on_this_bench_is_unresolved(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A quarantine outlives the lease that produced it, and the operator is
    about to be asked to physically check devices this file names."""
    workspace, path = bench(tmp_path, monkeypatch)
    tools = service(workspace)
    try:
        tools.coordinator.blocked = True
        tools.coordinator.quarantine_id = "deadbeef"
        rewrite(path, add_second_board)
        refused = tools.call(PROJECT_CONFIG_RELOAD)

        assert refused["ok"] is False
        assert refused["error_type"] == "resource_quarantined"
        assert refused["quarantine_id"] == "deadbeef"
        assert refused["side_effect_committed"] is False
        assert sorted(tools.config.debuggers) == ["dut"]
    finally:
        tools.coordinator.blocked = False
        tools.close()


def test_refused_when_the_file_is_gone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace, path = bench(tmp_path, monkeypatch)
    tools = service(workspace)
    try:
        loaded = tools.config
        path.unlink()
        refused = tools.call(PROJECT_CONFIG_RELOAD)

        assert refused["ok"] is False
        assert refused["error_type"] == "config_file_not_found"
        assert refused["config_status"]["state"] == STATE_MISSING
        # The policy in force is unaffected by the file going away, and is the
        # half a caller cannot get any other way once it has.
        assert refused["permissions_in_force"] == permission_summary(loaded)
        assert tools.config is loaded
    finally:
        tools.close()


def test_refused_when_the_file_will_not_load(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace, path = bench(tmp_path, monkeypatch)
    tools = service(workspace)
    try:
        loaded = tools.config
        path.write_text("workspace_root: [not, a, string\n", encoding="utf-8")
        refused = tools.call(PROJECT_CONFIG_RELOAD)

        assert refused["ok"] is False
        assert refused["error_type"] == "config_invalid"
        assert refused["config_status"]["state"] == STATE_CHANGED
        assert refused["side_effect_committed"] is False
        assert tools.config is loaded
        assert any("doctor" in step for step in refused["next_steps"])
    finally:
        tools.close()


def test_refused_when_the_file_cannot_be_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace, path = bench(tmp_path, monkeypatch)
    tools = service(workspace)
    try:
        loaded = tools.config
        path.write_bytes(b"\xff\xfe\x00workspace_root:")
        refused = tools.call(PROJECT_CONFIG_RELOAD)

        assert refused["ok"] is False
        assert refused["error_type"] in {"config_invalid", "config_unreadable"}
        assert refused["config_status"]["state"] in {STATE_CHANGED, STATE_UNREADABLE}
        assert tools.config is loaded
    finally:
        tools.close()


def test_no_grant_gates_the_reload(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """It reads and writes nothing, so a closed file must not lock a board out.

    A bench whose `allow_config_description_write` is false is precisely the one
    an operator edits by hand — and it would be absurd for that choice to mean
    the board they just wrote down can never be picked up without a restart."""
    workspace, path = bench(tmp_path, monkeypatch)
    rewrite(
        path,
        lambda document: document.__setitem__(
            "permissions",
            {"allow_config_write": False, "allow_config_description_write": False, "allow_config_permissions_write": False},
        ),
    )
    tools = service(workspace)
    try:
        assert tools.config.permissions.allow_config_description_write is False
        rewrite(path, add_second_board)
        assert tools.call(PROJECT_CONFIG_RELOAD)["ok"] is True
        assert sorted(tools.config.debuggers) == ["dut", "spare"]
        # And it stays a read: the write surface is as shut as it was.
        assert tools.call("project_config_set", {"changes": [{"key": "target.name", "value": "x"}]})["error_type"] == "permission_denied"
    finally:
        tools.close()


# ---------------------------------------------------------------------------
# What the answers say afterwards.


def test_config_status_does_not_call_the_adopted_description_stale(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace, path = bench(tmp_path, monkeypatch)
    tools = service(workspace)
    try:
        rewrite(path, lambda document: document["debuggers"]["dut"].update({"probe_id": "NEW-PROBE"}))
        assert tools.call("debugger_info")["config_stale"] is True

        result = tools.call(PROJECT_CONFIG_RELOAD)
        assert result["config_status"]["state"] == STATE_UNCHANGED
        assert result["config_status"]["reload_required"] is False
        assert "config_stale" not in result

        after = tools.call("debugger_info")
        assert after["config_status"]["state"] == STATE_UNCHANGED
        assert "config_stale" not in after
        # Nothing to say about the permissions: this file's are the ones in force.
        assert "permissions_source" not in after["config_status"]
        assert result["permission_differences"] == []
    finally:
        tools.close()


def test_a_permission_divergence_stays_visible_after_a_reload(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The other half of the same requirement, and the one a reload could hide.

    Once the description in force is the file's, `config_status` reports the
    file as unchanged — so if that were the whole answer, a file whose grants
    are wider than the ones being enforced would look completely settled."""
    workspace, path = bench(tmp_path, monkeypatch)
    tools = service(workspace)
    try:

        def widen_and_describe(document: dict) -> None:
            document["debuggers"]["dut"]["permissions"]["allow_mass_erase"] = True
            document["debuggers"]["dut"]["probe_id"] = "NEW-PROBE"

        rewrite(path, widen_and_describe)
        assert tools.call(PROJECT_CONFIG_RELOAD)["ok"] is True

        after = tools.call("debugger_info")
        status = after["config_status"]
        assert status["state"] == STATE_UNCHANGED
        assert "config_stale" not in after
        source = status["permissions_source"]
        assert source["digest"] != status["current_digest"]
        assert source["description_reloaded_at"]
        assert "parsed at startup" in source["summary"]
        assert "Restart the MCP server" in source["summary"]
        assert "not this file's" in status["summary"]
        # And the grant itself did not move.
        assert tools.config.debugger.permissions.allow_mass_erase is False
    finally:
        tools.close()


def test_the_divergence_survives_the_file_going_away(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Which document the grants came from is a fact about this process.

    The digest comparison needs a file and stops when there is none. This does
    not, so the answer that matters most while the file is gone — what is being
    enforced, and where it came from — is still there."""
    workspace, path = bench(tmp_path, monkeypatch)
    tools = service(workspace)
    try:
        rewrite(path, lambda document: document["debuggers"]["dut"]["permissions"].update({"allow_mass_erase": True}))
        assert tools.call(PROJECT_CONFIG_RELOAD)["ok"] is True
        path.unlink()

        status = tools.call("debugger_info")["config_status"]
        assert status["state"] == STATE_MISSING
        assert status["permissions_source"]["description_reloaded_at"]
        assert "parsed at startup" in status["permissions_source"]["summary"]
    finally:
        tools.close()


def test_a_second_reload_that_agrees_clears_the_divergence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The divergence is a fact about two documents, not a flag that sticks."""
    workspace, path = bench(tmp_path, monkeypatch)
    tools = service(workspace)
    try:
        rewrite(path, lambda document: document["debuggers"]["dut"]["permissions"].update({"allow_mass_erase": True}))
        assert tools.call(PROJECT_CONFIG_RELOAD)["ok"] is True
        assert "permissions_source" in tools.call("debugger_info")["config_status"]

        rewrite(path, lambda document: document["debuggers"]["dut"]["permissions"].update({"allow_mass_erase": False}))
        rewrite(path, lambda document: document["debuggers"]["dut"].update({"probe_id": "NEW-PROBE"}))
        assert tools.call(PROJECT_CONFIG_RELOAD)["ok"] is True

        status = tools.call("debugger_info")["config_status"]
        assert status["state"] == STATE_UNCHANGED
        assert "permissions_source" not in status
    finally:
        tools.close()


def test_three_reloads_still_name_the_startup_document_as_the_permission_source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A → B (same grants) → C (a different grant), which is where a provenance
    that moves on equality goes wrong.

    B was adopted as the permission source because its grants happened to match,
    and C then carried B's digest forward — so `permissions_source` named a
    document the permission objects never came from and paired it with the
    startup `loaded_at`. The digest has to be A's throughout: A is where the
    grants in force were parsed, and no comparison against a later file changes
    that."""
    workspace, path = bench(tmp_path, monkeypatch)
    tools = service(workspace)
    try:
        startup_digest = tools.config.config_digest
        startup_loaded_at = tools.config.loaded_at

        # B: the description moves, the grants are written out identically.
        rewrite(path, lambda document: document["debuggers"]["dut"].update({"probe_id": "B-PROBE"}))
        assert tools.call(PROJECT_CONFIG_RELOAD)["ok"] is True
        assert "permissions_source" not in tools.call("debugger_info")["config_status"]
        b_digest = tools.config.config_digest
        assert b_digest != startup_digest
        assert tools.config.permissions_digest == startup_digest

        # C: now a grant differs, so the divergence has to be reported — against A.
        rewrite(path, lambda document: document["debuggers"]["dut"]["permissions"].update({"allow_mass_erase": True}))
        assert tools.call(PROJECT_CONFIG_RELOAD)["ok"] is True

        status = tools.call("debugger_info")["config_status"]
        source = status["permissions_source"]
        assert source["digest"] == startup_digest
        assert source["digest"] != b_digest
        assert source["digest"] != status["current_digest"]
        assert source["loaded_at"] == startup_loaded_at
        assert tools.config.debugger.permissions.allow_mass_erase is False
    finally:
        tools.close()


def test_an_edit_after_a_reload_is_stale_again(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A reload is not a licence: the next change is reported like any other."""
    workspace, path = bench(tmp_path, monkeypatch)
    tools = service(workspace)
    try:
        rewrite(path, lambda document: document["debuggers"]["dut"].update({"probe_id": "ONE"}))
        assert tools.call(PROJECT_CONFIG_RELOAD)["ok"] is True
        rewrite(path, lambda document: document["debuggers"]["dut"].update({"probe_id": "TWO"}))
        after = tools.call("debugger_info")
        assert after["config_stale"] is True
        status = after["config_status"]
        assert status["state"] == STATE_CHANGED
        assert tools.config.debugger.probe_id == "ONE"
        # And it says which of two documents each half came from. "Everything
        # comes from the version loaded at startup" was true while nothing could
        # re-read the file; here the devices come from the reload above and only
        # the permissions come from startup, and `loaded_digest` and `loaded_at`
        # name those two different snapshots.
        assert status["description_source"] == DESCRIPTION_FROM_RELOAD
        assert status["description_reloaded_at"] == tools.config.description_reloaded_at
        assert status["loaded_at"] == tools.config.loaded_at
        assert status["loaded_digest"] == tools.config.config_digest
        assert "last explicit description reload" in status["summary"]
        assert "permissions it enforces come from the version loaded at startup" in status["summary"]
    finally:
        tools.close()


def test_a_server_that_never_reloaded_still_says_everything_came_from_startup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The unchanged half of the same contract, so the correction above is not a
    licence to stop making the plain claim where it is true."""
    workspace, path = bench(tmp_path, monkeypatch)
    tools = service(workspace)
    try:
        rewrite(path, lambda document: document["debuggers"]["dut"].update({"probe_id": "ONE"}))
        status = tools.call("debugger_info")["config_status"]

        assert status["state"] == STATE_CHANGED
        assert status["description_source"] == DESCRIPTION_FROM_STARTUP
        assert status["description_reloaded_at"] is None
        assert status["loaded_digest"] == tools.config.config_digest
        assert "still comes from the version loaded at startup" in status["summary"]
    finally:
        tools.close()


def test_a_reload_that_finds_nothing_new_says_so(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace, _ = bench(tmp_path, monkeypatch)
    tools = service(workspace)
    try:
        result = tools.call(PROJECT_CONFIG_RELOAD)
        assert result["ok"] is True
        assert result["description_changed"] is False
        assert result["description_changes"] == []
        assert result["permission_differences"] == []
        assert "nothing moved" in result["summary"]
    finally:
        tools.close()


# ---------------------------------------------------------------------------
# The surface: contract, catalogue, reference, CLI.


def test_the_tool_is_declared_with_no_arguments(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert PROJECT_CONFIG_RELOAD in MCP_TOOL_NAMES
    assert TOOL_SCHEMAS[PROJECT_CONFIG_RELOAD] == {"type": "object", "properties": {}, "additionalProperties": False}
    workspace, _ = bench(tmp_path, monkeypatch)
    tools = service(workspace)
    try:
        refused = tools.call(PROJECT_CONFIG_RELOAD, {"adopt_permissions": True})
        assert refused["ok"] is False
        assert refused["error_type"] == "invalid_argument"
    finally:
        tools.close()


def test_the_refusal_and_the_scope_are_documented(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    entry = ERROR_CATALOGUE[RELOAD_IN_OPEN_RUN_ERROR]
    assert "bench_run_stop" in " ".join(entry.remediation)
    assert entry.do_not

    shape = read_resource(CONFIG_SHAPE_URI)["text"]
    assert PROJECT_CONFIG_RELOAD in shape
    assert "agentic-hil config-reload" in shape
    for section, _ in NOT_RELOADED_SECTIONS:
        assert f"`{section}`" in shape or section in shape

    errors = read_resource("agentic-hil://reference/errors")["text"]
    assert RELOAD_IN_OPEN_RUN_ERROR in errors


def test_the_cli_counterpart_previews_what_a_reload_would_take(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from agentic_hil.cli import build_parser, config_reload, dispatch

    workspace, path = bench(tmp_path, monkeypatch)
    result = config_reload(None)
    assert result["ok"] is True
    assert result["scope"] == "preview"
    assert result["tool"] == PROJECT_CONFIG_RELOAD
    assert sorted(result["would_reload"]["debuggers"]) == ["dut"]
    assert sorted(result["would_reload"]["com_ports"]) == ["dut_uart"]
    assert result["would_reload"]["target"]["name"] == "example-target"
    assert any("restart" in step.lower() for step in result["next_steps"])

    # It is reachable as a command, and it refuses a file that will not load
    # with the message the server would give.
    assert dispatch(build_parser().parse_args(["config-reload"]))["ok"] is True
    path.write_text("workspace_root: [broken\n", encoding="utf-8")
    refused = config_reload(None)
    assert refused["ok"] is False
    assert refused["error_type"] == "config_invalid"
    assert refused["scope"] == "preview"


def test_merged_description_takes_nothing_but_the_four_sections(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The merge on its own, without a service, field by field.

    Every field of the result is either the disk config's (the four device
    sections) or the loaded config's (everything else). Asserted as a whole so a
    field added to `AgenticHILConfig` later cannot be silently left out of
    either half."""
    workspace, path = bench(tmp_path, monkeypatch)
    loaded = load_authoritative_config(workspace)
    rewrite(path, add_second_board)
    rewrite(path, lambda document: document["debug"].update({"max_dump_size_bytes": 2048}))
    rewrite(path, lambda document: document["target"].update({"name": "renamed-target"}))
    disk = load_authoritative_config(workspace)

    merged = merged_description(loaded, disk)

    # `permissions_digest` is in the exempt set because it is asserted below to
    # be exactly the *loaded* document's — it is the one field the reload may
    # neither take from disk nor let drift.
    decided_by_the_reload = {
        "target",
        "debuggers",
        "com_ports",
        "can_buses",
        "debugger",
        "debugger_id",
        "config_digest",
        "permissions_digest",
        "permissions_match_description",
        "description_reloaded_at",
    }
    for field in type(loaded).__dataclass_fields__:
        if field in decided_by_the_reload:
            continue
        assert getattr(merged, field) == getattr(loaded, field), field
    assert merged.target.name == "renamed-target"
    assert sorted(merged.debuggers) == ["dut", "spare"]
    assert merged.debuggers["dut"].permissions == loaded.debuggers["dut"].permissions
    assert merged.debuggers["spare"].permissions.allow_flash is False
    assert merged.config_digest == disk.config_digest
    assert merged.permissions_digest == loaded.config_digest
    assert merged.debug.max_dump_size_bytes == loaded.debug.max_dump_size_bytes


def test_reload_description_needs_a_loaded_configuration() -> None:
    reloaded, result = reload_description(None)
    assert reloaded is None
    assert result["error_type"] == "config_file_not_found"


# ---------------------------------------------------------------------------
# The composition is what gets enforced, so the composition is what is checked.


def legacy_openocd_bench(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **kwargs) -> tuple[Path, Path]:
    """One OpenOCD probe under version 1, with probe/flash/reset all granted.

    Version 1 because that is the model where a debugger entry can be made
    unreachable by its own grants: from version 2 on reading is free, so every
    entry is reachable and pinning validates every OpenOCD entry's scripts."""
    workspace = (tmp_path / "workspace").resolve()
    root = Path(os.environ["APPDATA"]) / "config-reload-v1"
    path = write_authoritative_config(workspace, monkeypatch, config_root=root, debugger_type="openocd", **kwargs)
    monkeypatch.chdir(workspace)
    return workspace, path


def close_the_entry_and_relativize_its_scripts(document: dict) -> None:
    """The file an MCP caller can write: every access grant narrowed to false,
    and then script paths nothing in that file can run."""
    entry = document["debuggers"]["dut"]
    entry["permissions"] = dict.fromkeys(entry["permissions"], False)
    entry["interface_cfg"] = "interface/stlink.cfg"
    entry["target_cfg"] = "target/stm32f4x.cfg"


def test_a_reload_refuses_a_description_that_was_only_valid_while_narrowed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The validation/policy split the reload exists to avoid, from the other side.

    Pinning skips an OpenOCD entry's script check when nothing the file permits
    can drive it, so a version 1 document whose grants are all `false` loads with
    relative, unvalidated script names in it. A reload keeps that description and
    puts this server's startup grants — probe, flash and reset — behind it, and
    that pairing was validated by neither load. The composition is re-checked, so
    the reload refuses instead of arming an OpenOCD nothing had looked at."""
    workspace, path = legacy_openocd_bench(tmp_path, monkeypatch)
    tools = service(workspace)
    try:
        validated = tools.config.debuggers["dut"].interface_cfg
        assert Path(validated).is_absolute()

        rewrite(path, close_the_entry_and_relativize_its_scripts)
        # The premise: that file loads on its own terms, because under its own
        # grants the entry drives nothing and its scripts are never looked at.
        assert load_authoritative_config(workspace).debuggers["dut"].interface_cfg == "interface/stlink.cfg"

        refused = tools.call(PROJECT_CONFIG_RELOAD)

        assert refused["ok"] is False
        assert refused["error_type"] == "config_invalid"
        assert refused["field"] == "debuggers.dut.interface_cfg"
        assert refused["side_effect_status"] == "not_started"
        assert refused["retry_safe"] is True
        # And nothing moved: the server is still driving the validated scripts.
        assert tools.config.debuggers["dut"].interface_cfg == validated
        assert tools.config.debuggers["dut"].permissions.allow_flash is True
    finally:
        tools.close()


def test_a_narrowed_entry_whose_scripts_stay_valid_still_reloads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The other direction, so the check above is not just a refusal generator:
    the same narrowing with the scripts left absolute reloads as before."""
    workspace, path = legacy_openocd_bench(tmp_path, monkeypatch)
    tools = service(workspace)
    try:

        def narrow_only(document: dict) -> None:
            entry = document["debuggers"]["dut"]
            entry["permissions"] = dict.fromkeys(entry["permissions"], False)
            entry["probe_id"] = "MOVED-0002"

        rewrite(path, narrow_only)
        result = tools.call(PROJECT_CONFIG_RELOAD)

        assert result["ok"] is True
        assert tools.config.debugger.probe_id == "MOVED-0002"
        assert tools.config.debuggers["dut"].permissions.allow_flash is True
    finally:
        tools.close()


def test_a_version_one_read_grant_that_moves_is_reported_as_a_difference(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`allow_probe` and `allow_read` gate a version 1 bench, so a file that
    moves only one of them has permissions this server is not enforcing. The
    permission surface used to name only the write grants, so this reported an
    empty `permission_differences`, moved `permissions_digest` onto the file and
    dropped `permissions_source` — three separate claims that the two halves
    agreed when they did not."""
    workspace, path = legacy_openocd_bench(tmp_path, monkeypatch, com_ports_yaml=COM_PORTS)
    tools = service(workspace)
    try:
        assert permission_summary(tools.config)["debuggers"]["dut"]["allow_probe"] is True

        def close_the_reads(document: dict) -> None:
            document["debuggers"]["dut"]["permissions"]["allow_probe"] = False
            document["com_ports"]["dut_uart"]["permissions"]["allow_read"] = False

        rewrite(path, close_the_reads)
        result = tools.call(PROJECT_CONFIG_RELOAD)

        assert result["ok"] is True
        assert "debuggers.dut.allow_probe" in result["permission_differences"]
        assert "com_ports.dut_uart.allow_read" in result["permission_differences"]
        # Not adopted, and not hidden: the grants in force are still the
        # startup document's and `config_status` says which document that is.
        assert tools.config.debuggers["dut"].permissions.allow_probe is True
        assert tools.config.permissions_digest != tools.config.config_digest
        assert result["config_status"]["permissions_source"]["digest"] == tools.config.permissions_digest
    finally:
        tools.close()
