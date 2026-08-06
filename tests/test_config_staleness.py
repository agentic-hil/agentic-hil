"""A running server says which configuration it is answering out of.

The session behind hardci-hq#77: an operator switched a debugger from `stlink`
to `pyocd`, and within the same minute `agentic-hil doctor` reported `pyocd`
while `debugger_info` over MCP reported `stlink`. Both were telling the truth
about different documents, and neither said so. It cost two round trips, and it
could have cost a flash sent to a backend the operator no longer configured —
the authoritative configuration is the permission boundary, so a server quietly
enforcing an older one is enforcing a policy nobody has chosen any more.

The tests here are in three groups. The first reproduces that disagreement and
shows it is now named. The second is about *how* the comparison is made — a
rewrite with identical bytes is not a change, and a change hidden under an
unchanged timestamp still is, which is the whole reason the comparison is a hash
of the loaded bytes and not a modification time. The third is about what is
deliberately *not* done: nothing reloads, the configuration in force after an
edit is still the one loaded at startup, and the file being gone is a third
answer rather than a variant of "changed".
"""

from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
import yaml
from conftest import FAKE_PYOCD, FAKE_STLINK, write_authoritative_config

from agentic_hil import configwrite
from agentic_hil.cli import doctor
from agentic_hil.config import ConfigError, config_digest, load_authoritative_config
from agentic_hil.configstate import (
    STATE_CHANGED,
    STATE_MISSING,
    STATE_UNCHANGED,
    STATE_UNKNOWN,
    STATE_UNREADABLE,
    config_status,
)
from agentic_hil.configwrite import PROJECT_CONFIG_DESCRIBE, PROJECT_CONFIG_SET
from agentic_hil.knowledge import (
    CONFIG_DESCRIPTION_RIGHT,
    CONFIG_PERMISSIONS_RIGHT,
    CONFIG_STALE_ERROR,
    CONFIG_WRITE_RIGHT,
    ERRORS_URI,
    read_resource,
)
from agentic_hil.tools import PROJECT_CONFIG_CREATE, AgenticHILToolService, UnprovisionedToolService

COM_PORTS = 'com_ports:\n  dut_uart:\n    device: "COM9"\n    baudrate: 115200\n'
SECOND_PROBE = f'debuggers:\n  spare:\n    type: "stlink"\n    executable: "{FAKE_STLINK.as_posix()}"\n'


def bench(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    device_grants: dict[str, bool] | None = None,
    debuggers_yaml: str = "",
    **grants: bool,
) -> tuple[Path, Path]:
    """One probe on `stlink`, exactly the shape the reported session had.

    Under the sandbox `isolated_config_environment` provides rather than under
    `tmp_path`: on Windows the per-user Temp ACL is replaceable, so a policy file
    there is refused by the ancestor check before any of this runs."""
    workspace = (tmp_path / "workspace").resolve()
    root = Path(os.environ["APPDATA"]) / "config-staleness"
    path = write_authoritative_config(
        workspace,
        monkeypatch,
        config_root=root,
        config_version=2,
        debugger_type="stlink",
        permissions=device_grants or {},
        com_ports_yaml=COM_PORTS,
        debuggers_yaml=debuggers_yaml,
    )
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    document["permissions"] = {CONFIG_WRITE_RIGHT: False, CONFIG_DESCRIPTION_RIGHT: False, CONFIG_PERMISSIONS_RIGHT: False, **grants}
    path.write_text("# A person wrote this bench.\n" + yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    monkeypatch.chdir(workspace)
    return workspace, path


def service(workspace: Path) -> AgenticHILToolService:
    return AgenticHILToolService(load_authoritative_config(workspace), frontend="mcp")


def switch_to_pyocd(path: Path) -> None:
    """What the operator did: the same probe, a different backend.

    Both keys move, because a `pyocd` entry pointing at the STM32CubeProgrammer
    executable is not a configuration anyone would write."""
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    document["debuggers"]["dut"]["type"] = "pyocd"
    document["debuggers"]["dut"]["executable"] = FAKE_PYOCD.as_posix()
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")


def rewrite(path: Path, edit) -> None:
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    edit(document)
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")


# ---------------------------------------------------------------------------
# The reported disagreement, and the two answers that now name it.


def test_debugger_info_names_the_backend_it_answers_with_and_the_file_it_came_from(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The reproduction. `stlink` is still the answer, and it now says why.

    The server keeps serving what it loaded — that is the correct behaviour and
    is not what the issue asks to change. What it asks for is that the answer
    stop looking like a statement about the file that exists now."""
    workspace, path = bench(tmp_path, monkeypatch)
    tools = service(workspace)
    try:
        before = tools.call("debugger_info")
        switch_to_pyocd(path)
        after = tools.call("debugger_info")
    finally:
        tools.close()

    assert before["backend"] == "stlink"
    assert before["config_status"]["state"] == STATE_UNCHANGED
    assert "config_stale" not in before

    # Same backend, because nothing reloaded. Not the same silence.
    assert after["backend"] == "stlink"
    assert after["config_stale"] is True
    assert after["config_status"]["state"] == STATE_CHANGED
    assert after["config_status"]["path"] == str(path)
    assert after["config_status"]["loaded_digest"] != after["config_status"]["current_digest"]
    assert after["config_status"]["reload_required"] is True
    # Prominent, in the field a caller reads first, not only in a nested block.
    assert "is not the one this server loaded" in after["summary"]
    assert after["config_status"]["error_type"] == CONFIG_STALE_ERROR
    assert any("restart" in step for step in after["config_status"]["remediation"])


def test_doctor_publishes_what_a_running_server_has_to_be_compared_against(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The other half of the disagreement.

    `doctor` parses the file at the moment it is asked, so it is always current
    and can never be the one that is stale — which is exactly why it cannot
    speak for a server that started before the last edit. What it can do is
    publish the digest that makes the comparison possible, and say how."""
    workspace, path = bench(tmp_path, monkeypatch)
    tools = service(workspace)
    try:
        switch_to_pyocd(path)
        checked = doctor(None)
        served = tools.call("debugger_info")
    finally:
        tools.close()

    assert checked["config_status"]["state"] == STATE_UNCHANGED
    assert checked["config_status"]["loaded_digest"] == config_digest(path.read_bytes())
    note = checked["config_status"]["compare_with_running_server"]

    assert "debugger_info" in note
    # And it names both ways across. This note ended at "only restarting it
    # changes that", which stopped being true the moment a description reload
    # existed: that call is what adopts a changed `debuggers` entry into a
    # running server, and the restart is what everything else — every permission
    # included — still waits for. `doctor` is the last caller that made the
    # restart-only promise, and a digest mismatch is exactly where it is read.
    assert "only restarting" not in note
    assert "project_config_reload_description" in note
    assert "restart" in note
    assert "permissions" in note
    # The comparison the note asks for, made: two live answers, one file, and the
    # difference is now visible in both of them instead of in neither.
    assert checked["config_status"]["loaded_digest"] == served["config_status"]["current_digest"]
    assert checked["config_status"]["loaded_digest"] != served["config_status"]["loaded_digest"]


def test_doctor_names_a_file_that_moved_while_it_was_checking(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`doctor` is not instantaneous, so its own report can go stale.

    The probe checks spawn debugger processes and take seconds. A report printed
    out of a document that has since been replaced is the same defect at a
    smaller scale, so the check is made at the end against what the run was
    decided by."""
    workspace, path = bench(tmp_path, monkeypatch, device_grants={"allow_flash": True})

    def edits_the_file_while_it_checks(config, debugger_id: str):
        switch_to_pyocd(path)
        return {"ok": True, "tool": "debugger_info", "backend": "stlink"}, {"ok": True, "tool": "debugger_target_support", "status": "not_applicable"}

    monkeypatch.setattr("agentic_hil.cli._doctor_probe_check", edits_the_file_while_it_checks)

    checked = doctor(None)

    assert checked["config_status"]["state"] == STATE_CHANGED
    assert checked["config_stale"] is True
    assert "is not the one this server loaded" in checked["summary"]


def test_a_changed_configuration_reaches_every_answer_not_only_the_two_that_name_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A caller reading a device list has the same reason to know.

    The block is attached once per result and always by the same check, so no
    tool can be the one that answers out of an older policy without saying so."""
    workspace, path = bench(tmp_path, monkeypatch)
    tools = service(workspace)
    try:
        quiet = tools.call("com_ports_list")
        rewrite(path, lambda document: document["com_ports"]["dut_uart"].update({"device": "COM11"}))
        loud = tools.call("com_ports_list")
        elsewhere = tools.call("can_buses_list")
        device = tools.config.com_ports["dut_uart"].device
    finally:
        tools.close()

    assert "config_status" not in quiet
    assert loud["config_stale"] is True
    assert loud["config_status"]["state"] == STATE_CHANGED
    # And the port is still COM9, because the list comes out of the loaded policy.
    assert device == "COM9"
    assert elsewhere["config_stale"] is True


def test_a_file_edited_while_the_backend_answers_is_not_reported_as_unchanged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`debugger_info` samples the file after the backend, never before.

    `backend.info()` runs the toolchain's version command and is allowed
    seconds. A status taken before it would answer `unchanged` about a file the
    operator edited during exactly that window — a positive claim that the two
    documents agree, made out of a comparison that predates the divergence."""
    workspace, path = bench(tmp_path, monkeypatch)
    tools = service(workspace)
    try:

        def edits_the_file_while_it_answers() -> dict:
            switch_to_pyocd(path)
            return {"ok": True, "tool": "debugger_info", "backend": "stlink"}

        monkeypatch.setattr(tools.backend, "info", edits_the_file_while_it_answers)
        answered = tools.call("debugger_info")
    finally:
        tools.close()

    assert answered["backend"] == "stlink"
    assert answered["config_status"]["state"] == STATE_CHANGED
    assert answered["config_stale"] is True


def test_the_two_prominent_answers_carry_the_block_when_they_refuse(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every case means the refusals too.

    A refused call still answered out of some configuration, and which one is
    exactly what an operator comparing two answers is trying to establish. Both
    refusals below are returned before the body that attaches the block ever
    runs: the unbound-probe error comes out of the dispatch gate, and a describe
    against a file that is not this workspace's authoritative one is raised and
    caught before its result is built."""
    workspace, path = bench(tmp_path, monkeypatch, debuggers_yaml=SECOND_PROBE)
    tools = service(workspace)
    try:
        assert tools.config.debugger is None, "two probes, so none is bound"
        unbound = tools.call("debugger_info")

        elsewhere = path.parent / "elsewhere.yaml"
        elsewhere.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
        monkeypatch.setenv("AGENTIC_HIL_CONFIG", str(elsewhere))
        wrong_file = tools.call(PROJECT_CONFIG_DESCRIBE)
    finally:
        tools.close()

    assert unbound["error_type"] == "not_supported"
    assert unbound["config_status"]["state"] == STATE_UNCHANGED
    assert "config_stale" not in unbound

    assert wrong_file["error_type"] == "config_invalid"
    assert wrong_file["config_status"]["state"] == STATE_UNCHANGED


def test_an_edit_after_this_session_created_the_configuration_is_still_reported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The provisioning sequence, run to its end.

    Start with no configuration, generate one, let the operator take a
    permission back out of it, then ask this server something. The answer has to
    say that the file it is enforcing is no longer the file on disk: this is the
    one moment in this sequence where the loaded document and the file have
    provably come apart, and the narrowing the operator made is precisely what
    the server will not pick up until it is restarted."""
    workspace = (tmp_path / "workspace").resolve()
    workspace.mkdir(parents=True)
    monkeypatch.chdir(workspace)
    monkeypatch.setattr(
        "agentic_hil.tools.discover_attached_hardware",
        lambda: {
            "ok": True,
            "tool": "bootstrap_hardware_discovery",
            "backend": "stlink",
            "executable": str(FAKE_STLINK),
            "probe_id": "STLINK123",
            "target": {"probe_id": "STLINK123", "controller": "STM32F446RE"},
            "com_port": {"device": "COM3", "serial_number": "STLINK123"},
            "available_com_ports": {"ok": True, "ports": [{"device": "COM3", "serial_number": "STLINK123"}]},
            "side_effect_committed": False,
            "side_effect_status": "not_started",
            "hardware_state": "unchanged",
            "cleanup_required": False,
            "summary": "One attached STM32 target was identified.",
        },
    )
    provisioner = UnprovisionedToolService(workspace)
    try:
        created = provisioner.call(PROJECT_CONFIG_CREATE)
        assert created["ok"] is True, created
        path = Path(created["path"])

        # A person narrows what this project should not have, and takes the
        # regeneration grant with it. Nothing reloads.
        rewrite(
            path,
            lambda document: (
                document["debuggers"]["dut"]["permissions"].update({"allow_mass_erase": False}),
                document["permissions"].update({"allow_config_write": False}),
            ),
        )

        described = provisioner.call(PROJECT_CONFIG_DESCRIBE)
        elsewhere = provisioner.call("com_ports_list")
    finally:
        provisioner.close()

    assert described["config_stale"] is True
    assert described["config_status"]["state"] == STATE_CHANGED
    assert described["config_status"]["path"] == str(path)
    # The narrowing is in force on disk and not yet in this server, and the
    # answer carries both halves rather than one of them.
    assert described["permissions_in_force"]["allow_config_write"] is True
    assert [entry for entry in described["writable_keys"] if entry["key"] == "permissions.allow_config_write"][0]["current_value"] is False
    # And the same statement from the same check on an ordinary answer, so the
    # two cannot say different things about one file.
    assert elsewhere["config_status"]["current_digest"] == described["config_status"]["current_digest"]


# ---------------------------------------------------------------------------
# Why the comparison is a hash of the loaded bytes.


def test_a_rewrite_with_identical_bytes_is_not_a_change(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A `git checkout`, an editor saving on close, a re-run of `init`.

    Nothing about the policy moved, so nothing may be reported. A flag that
    fires on writes rather than on content is a flag an operator learns to
    ignore, and the one time it matters they ignore it then too."""
    workspace, path = bench(tmp_path, monkeypatch)
    config = load_authoritative_config(workspace)

    same = path.read_bytes()
    path.write_bytes(same)
    os.utime(path, (0, 0))
    then = config_status(config)
    os.utime(path, None)
    now = config_status(config)

    assert then["state"] == STATE_UNCHANGED
    assert now["state"] == STATE_UNCHANGED
    assert now["reload_required"] is False


def test_a_change_hidden_under_an_unchanged_timestamp_is_still_seen(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The other direction, and the one that matters.

    Timestamp granularity is a whole second or two on some filesystems, and a
    modification time can be set backwards outright. This is the case a
    stat-based check reports as unchanged; the content says otherwise."""
    workspace, path = bench(tmp_path, monkeypatch)
    config = load_authoritative_config(workspace)
    original = os.stat(path)

    switch_to_pyocd(path)
    os.utime(path, (original.st_atime, original.st_mtime))

    assert os.stat(path).st_mtime == original.st_mtime
    assert config_status(config)["state"] == STATE_CHANGED


def test_a_configuration_that_was_never_read_from_a_file_claims_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Unknown is not unchanged, and it is not stale either.

    Nothing established that the file has moved, so nothing may be reported as
    though it had — and nothing may be reported as current either."""
    workspace, _ = bench(tmp_path, monkeypatch)
    config = load_authoritative_config(workspace)

    unread = config_status(replace(config, config_digest=""))

    assert unread["state"] == STATE_UNKNOWN
    assert unread["reload_required"] is False
    assert config_status(None)["state"] == STATE_UNKNOWN


# ---------------------------------------------------------------------------
# A file that is gone or unreadable is neither of the two.


def test_a_configuration_that_disappeared_is_its_own_answer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """There is no document to restart onto.

    The remedy is not the one an unconfigured project gets: this server has a
    policy, in memory, and `project_config_create` would replace an operator's
    settings with a deny-by-default skeleton rather than recover them."""
    workspace, path = bench(tmp_path, monkeypatch)
    tools = service(workspace)
    try:
        path.unlink()
        answered = tools.call("com_ports_list")
    finally:
        tools.close()

    assert answered["config_status"]["state"] == STATE_MISSING
    assert answered["config_status"]["error_type"] == "config_file_not_found"
    assert answered["config_stale"] is True
    assert any("put the file back" in step for step in answered["config_status"]["remediation"])
    assert any("project_config_create" in step for step in answered["config_status"]["do_not"])


def test_a_missing_file_still_gets_an_answer_about_the_policy_in_force(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The remedy for a removed file names `project_config_describe`, so it has
    to work.

    Nothing else can show what is being enforced once the document is gone: the
    keys and values every other answer reports are read from disk. This one
    answers out of the loaded policy instead of refusing with the very error the
    remedy is written for, and says so rather than passing memory off as a file.
    """
    workspace, path = bench(
        tmp_path,
        monkeypatch,
        device_grants={"allow_flash": True},
        **{CONFIG_DESCRIPTION_RIGHT: True},
    )
    tools = service(workspace)
    try:
        path.unlink()
        described = tools.call(PROJECT_CONFIG_DESCRIBE)
        gone = config_status(tools.config)
    finally:
        tools.close()

    assert described["ok"] is True, described
    assert described["document_source"] == "loaded_policy"
    assert described["config_status"]["state"] == STATE_MISSING
    assert described["config_stale"] is True
    # The half nothing else exposes: what this server still enforces.
    assert described["permissions_in_force"][CONFIG_DESCRIPTION_RIGHT] is True
    assert described["permissions_in_force"]["debuggers"]["dut"]["allow_flash"] is True
    # And nothing is open, because there is no document to change and this
    # server will not create one over the operator's settings.
    assert described["writable_keys"] == []
    assert described["locked_keys"] == []
    assert all(entry["granted"] is False for entry in described["rights"])
    assert any("no file" in step or "no file at the path" in step for step in described["next_steps"])
    # The catalogue entry that sent the caller here says what it will get.
    assert any("permissions_in_force" in step for step in gone["remediation"])


def test_a_configuration_that_cannot_be_read_is_not_reported_as_unchanged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Unknown, and said as unknown.

    A read that fails says nothing about the content, so answering "unchanged"
    would be a claim nothing supports."""
    workspace, path = bench(tmp_path, monkeypatch)
    config = load_authoritative_config(workspace)

    path.unlink()
    path.mkdir()

    unreadable = config_status(config)

    assert unreadable["state"] == STATE_UNREADABLE
    assert unreadable["error_type"] == "config_unreadable"
    assert unreadable["current_digest"] is None
    assert unreadable["backend_error"]


def test_bytes_that_are_no_longer_utf8_are_unreadable_rather_than_changed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A restart cannot load this file, so it must not be sent to a restart.

    `changed` promises that restarting picks the new document up, and the state
    contract already lists non-UTF-8 bytes under `unreadable`. Hashing the raw
    bytes without decoding them first classified exactly that file as a
    restartable change, which is a remedy that cannot work."""
    workspace, path = bench(tmp_path, monkeypatch)
    config = load_authoritative_config(workspace)

    path.write_bytes(path.read_bytes() + b'\ncomment: "\xff\xfe"\n')

    status = config_status(config)

    assert status["state"] == STATE_UNREADABLE
    assert status["error_type"] == "config_unreadable"
    assert status["current_digest"] is None
    assert any("UTF-8" in step for step in status["remediation"])
    # The claim the state makes, checked against what a restart would actually
    # do with this file.
    with pytest.raises(ConfigError):
        load_authoritative_config(workspace)


# ---------------------------------------------------------------------------
# What is deliberately not done.


def test_nothing_reloads_and_the_policy_in_force_is_the_one_that_was_loaded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Reporting is the whole of the change.

    A reload would swap the permission situation under whatever the server is
    holding, and it would let a file that changed underneath a running server
    widen that server's own grants. Neither happens: the backend object, the
    bound probe and the permissions are the ones parsed at startup, however many
    calls are made after the edit."""
    workspace, path = bench(tmp_path, monkeypatch)
    tools = service(workspace)
    try:
        backend_before = tools.backend
        rewrite(path, lambda document: document["permissions"].update({CONFIG_DESCRIPTION_RIGHT: True, CONFIG_PERMISSIONS_RIGHT: True}))
        switch_to_pyocd(path)

        for _ in range(3):
            tools.call("debugger_info")

        assert tools.config.debugger is not None
        assert tools.config.debugger.type == "stlink"
        assert tools.backend is backend_before
        assert tools.config.permissions.allow_config_description_write is False
        # The added grants are on disk and are not in force; the write is still
        # refused, and the refusal is what says the file has moved.
        refused = tools.call(PROJECT_CONFIG_SET, {"changes": [{"key": "target.name", "value": "renamed"}]})
    finally:
        tools.close()

    assert refused["error_type"] == "permission_denied"
    assert refused["config_stale"] is True


def test_a_grant_revoked_since_startup_is_in_force_and_the_answer_says_which_half_is(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The asymmetry #88 established, made visible rather than routed around.

    `granted_rights` takes the narrower of the loaded policy and the file, so a
    revoked grant binds immediately and an added one does not. That is not a
    detail to hide behind a staleness flag: `project_config_describe` reads the
    keys off disk and gates them on the narrower side, and when the two
    documents differ it has to say so."""
    workspace, path = bench(tmp_path, monkeypatch, **{CONFIG_DESCRIPTION_RIGHT: True})
    tools = service(workspace)
    try:
        opened = tools.call(PROJECT_CONFIG_DESCRIBE)
        rewrite(path, lambda document: document["permissions"].update({CONFIG_DESCRIPTION_RIGHT: False}))
        closed = tools.call(PROJECT_CONFIG_DESCRIBE)
        refused = tools.call(PROJECT_CONFIG_SET, {"changes": [{"key": "target.name", "value": "renamed"}]})
    finally:
        tools.close()

    assert opened["config_status"]["state"] == STATE_UNCHANGED
    assert any(entry["key"] == "target.name" for entry in opened["writable_keys"])

    assert closed["config_stale"] is True
    assert closed["config_status"]["state"] == STATE_CHANGED
    assert not any(entry["key"] == "target.name" for entry in closed["writable_keys"])
    assert any("narrower" in step for step in closed["next_steps"])
    assert refused["error_type"] == "permission_denied"


def test_a_write_and_the_staleness_check_report_one_truth(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`reload_required` and the staleness block cannot disagree.

    A write through `project_config_set` makes the loaded version stale, and #88
    already answered `reload_required: true` for its own writes. Both now come
    off the same check, so an operator cannot be told "restart needed" by one
    mechanism and "no change" by the other."""
    workspace, _ = bench(tmp_path, monkeypatch, **{CONFIG_DESCRIPTION_RIGHT: True})
    tools = service(workspace)
    try:
        written = tools.call(PROJECT_CONFIG_SET, {"changes": [{"key": "target.name", "value": "renamed"}]})
        afterwards = tools.call("debugger_info")
    finally:
        tools.close()

    assert written["ok"] is True
    assert written["reload_required"] is True
    assert written["config_stale"] is True
    assert written["config_status"]["state"] == STATE_CHANGED
    assert afterwards["config_status"]["state"] == STATE_CHANGED
    assert afterwards["config_status"]["current_digest"] == written["config_status"]["current_digest"]
    assert any("reload_required" in step or "restart" in step for step in written["next_steps"])


def test_a_wrong_file_and_a_changed_file_stay_two_conditions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """They have different remedies, so they may not share one message.

    A server bound to a file that is not this workspace's authoritative one is
    refused by #88's check. That file can be perfectly unchanged at the same
    time, and saying "your configuration changed" there would send an operator
    to a restart that fixes nothing."""
    workspace, path = bench(tmp_path, monkeypatch, **{CONFIG_DESCRIPTION_RIGHT: True})
    tools = service(workspace)
    try:
        elsewhere = path.parent / "elsewhere.yaml"
        elsewhere.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
        monkeypatch.setenv("AGENTIC_HIL_CONFIG", str(elsewhere))
        refused = tools.call(PROJECT_CONFIG_SET, {"changes": [{"key": "target.name", "value": "renamed"}]})
        status = config_status(tools.config)
    finally:
        tools.close()

    assert refused["error_type"] == "config_invalid"
    assert "authoritative_path" in refused
    assert "config_stale" not in refused
    assert status["state"] == STATE_UNCHANGED


def test_the_errors_resource_serves_what_a_stale_answer_carries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """One catalogue, read by the answer and by the resource.

    The scoped entries exist because the unscoped `config_file_not_found` sends
    a caller to `project_config_create`, which is the right answer for a project
    that has no configuration and the wrong one for a running server whose
    configuration was removed underneath it."""
    workspace, path = bench(tmp_path, monkeypatch)
    config = load_authoritative_config(workspace)
    path.unlink()

    served = json.loads((read_resource(ERRORS_URI) or {})["text"])["entries"]
    gone = config_status(config)

    stale = next(entry for entry in served if entry["error_type"] == CONFIG_STALE_ERROR)
    scoped = next(entry for entry in served if entry["error_type"] == "config_file_not_found" and entry.get("scope") == "running_server")
    unscoped = next(entry for entry in served if entry["error_type"] == "config_file_not_found" and "scope" not in entry)

    assert stale["remediation"]
    assert gone["remediation"] == scoped["remediation"]
    assert gone["do_not"] == scoped["do_not"]
    assert scoped["remediation"] != unscoped["remediation"]


def test_every_agent_facing_copy_names_the_states_this_module_has() -> None:
    """The guidance beside a state has to describe the state the code produces.

    An agent reads whichever copy its host installed, so a state named in one and
    absent from another is an agent acting on a rule this server does not follow.
    `invalid` is the case in point: it was a sixth state asserting that a restart
    onto the file would fail, it was removed with the candidate validation that
    was the only thing that could establish it (hardci-hq#95), and a copy still
    naming it would send an operator looking for a state no answer can carry."""
    from agentic_hil.mcp import SERVER_INSTRUCTIONS

    root = Path(__file__).resolve().parents[1]
    copies = {
        "server instructions": SERVER_INSTRUCTIONS,
        "packaged skill": (root / "src" / "agentic_hil" / "skills" / "agentic-hil" / "SKILL.md").read_text(encoding="utf-8"),
        "plugin skill": (root / "plugins" / "agentic-hil" / "skills" / "agentic-hil" / "SKILL.md").read_text(encoding="utf-8"),
        "AGENTS.md": (root / "AGENTS.md").read_text(encoding="utf-8"),
        "README.md": (root / "README.md").read_text(encoding="utf-8"),
    }
    for name, text in copies.items():
        for state in (STATE_CHANGED, STATE_MISSING, STATE_UNREADABLE):
            assert f"`{state}`" in text, f"{name} does not name the {state} state"
        assert "`invalid`" not in text, f"{name} still names a state this server cannot produce"
    # And the states table in the README covers all five, so a reader counting
    # rows against the code finds the same set.
    for state in (STATE_UNCHANGED, STATE_CHANGED, STATE_MISSING, STATE_UNREADABLE, STATE_UNKNOWN):
        assert f"| `{state}` |" in copies["README.md"]


# ---------------------------------------------------------------------------
# What `changed` claims, and what it deliberately does not.


@pytest.mark.parametrize(
    ("what", "edit"),
    [
        # Not YAML at all.
        ("a document that will not parse", lambda document: document.__setitem__("broken", "[unclosed")),
        # Parseable, and refused by the shipped schema.
        ("a document the schema refuses", lambda document: document.__setitem__("debuggers", {"dut": {"type": "not-a-backend"}})),
        # Schema-valid, refused by a document rule no per-field schema expresses.
        ("a relative workspace_root", lambda document: document.__setitem__("workspace_root", "relative/path")),
        # Schema-valid, document-valid, refused when the loader pins paths.
        ("a reports directory outside the workspace", lambda document: document.__setitem__("reports", {"directory": "../outside"})),
    ],
)
def test_a_changed_file_that_will_not_load_is_still_only_a_changed_file(what: str, edit: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """One observation, and no forecast attached to it.

    `changed` used to mean "a restart loads what is on disk now", and holding
    that needed a candidate validation congruent with startup — two code paths
    that must agree, where every difference is a defect, and four were found in
    four review rounds. The claim is now the digest comparison and nothing else:
    the file differs from the one this server loaded. Each document here is one
    the loader really does refuse, asserted at the end, and each is `changed` all
    the same. The operator restarts, and startup says why if it will not come up.
    """
    workspace, path = bench(tmp_path, monkeypatch, device_grants={"allow_flash": True})
    config = load_authoritative_config(workspace)

    if what == "a document that will not parse":
        path.write_text(path.read_text(encoding="utf-8") + "\n  broken: [unclosed\n", encoding="utf-8")
    else:
        rewrite(path, edit)

    status = config_status(config)

    assert status["state"] == STATE_CHANGED, what
    assert status["error_type"] == CONFIG_STALE_ERROR, what
    assert status["reload_required"] is True
    assert status["current_digest"] and status["current_digest"] != status["loaded_digest"]
    # Nothing in the answer asserts the restart succeeds, and the remediation
    # says where the reason comes from when it does not.
    assert "will load" not in status["summary"]
    assert any("does not come up" in step for step in status["remediation"])
    # The document really is one the loader refuses, so this is the case the
    # removed state existed for and not a document that happens to load.
    with pytest.raises(ConfigError):
        load_authoritative_config(workspace)


def test_reading_the_status_of_a_changed_file_puts_nothing_on_the_machine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A status check runs per call, against a file the operator is still editing.

    Startup creates `state_root`. Nothing here may, because the document naming
    it is not in force and may be rolled back a second later — one `state_root`
    typo, repeated on every tool call, would otherwise scatter directories for
    configurations nobody chose."""
    workspace, path = bench(tmp_path, monkeypatch)
    config = load_authoritative_config(workspace)
    unwritten = Path(os.environ["APPDATA"]) / "config-staleness" / "never-created-state"

    rewrite(path, lambda document: document.__setitem__("state_root", str(unwritten)))

    status = config_status(config)

    assert status["state"] == STATE_CHANGED
    assert not unwritten.exists()


def test_a_changed_file_that_still_loads_is_reported_the_same_way(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The ordinary operator edit, which is what the whole module exists for."""
    workspace, path = bench(tmp_path, monkeypatch)
    config = load_authoritative_config(workspace)

    switch_to_pyocd(path)

    status = config_status(config)

    assert status["state"] == STATE_CHANGED
    assert status["error_type"] == CONFIG_STALE_ERROR
    assert status["reload_required"] is True
    # And it does load, so the one state covers both — which is the point: the
    # answer never had to tell them apart to be useful.
    assert load_authoritative_config(workspace).debugger.type == "pyocd"


def test_an_unreachable_path_is_unreadable_rather_than_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """"Gone" and "cannot be got at" send an operator to different places.

    `missing` carries restore-the-file remediation, and a stat that fails on an
    ancestor is not evidence that the file was deleted — the file may be sitting
    right there behind a directory that is no longer traversable. Asking
    `os.path.lexists` for a second opinion answered False for every such failure
    and turned all of them into `missing`."""
    workspace, path = bench(tmp_path, monkeypatch)
    config = load_authoritative_config(workspace)

    def blocked(file_path, **kwargs):
        raise PermissionError(13, "access is denied")

    monkeypatch.setattr("agentic_hil.configstate.safe_read_bytes", blocked)

    status = config_status(config)

    assert status["state"] == STATE_UNREADABLE
    assert status["error_type"] == "config_unreadable"
    assert "access is denied" in status["backend_error"]


def test_a_deleted_file_is_still_reported_as_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The other side of the same line: only the originating error decides."""
    workspace, path = bench(tmp_path, monkeypatch)
    config = load_authoritative_config(workspace)

    path.unlink()

    status = config_status(config)

    assert status["state"] == STATE_MISSING
    assert status["error_type"] == "config_file_not_found"


# ---------------------------------------------------------------------------
# One answer describes one version of the file.


def test_describe_reads_the_status_and_the_document_from_one_snapshot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Two reads can straddle an edit, and then one answer describes two files.

    The status was computed first and the document read after it. An edit in
    between returned `state: unchanged` beside values from a document that is not
    the unchanged one — a caller comparing the two halves of that answer is
    comparing two documents without being told."""
    workspace, path = bench(tmp_path, monkeypatch, **{CONFIG_DESCRIPTION_RIGHT: True})
    tools = service(workspace)
    seen: list[bytes] = []
    real = configwrite.secure_optional_read_bytes

    def edit_then_read(file_path):
        # The edit lands where the second read used to pick it up. There is only
        # one read now, so both halves of the answer have to move together.
        if os.path.normcase(str(file_path)) == os.path.normcase(str(path)) and not seen:
            switch_to_pyocd(path)
        raw = real(file_path)
        if os.path.normcase(str(file_path)) == os.path.normcase(str(path)):
            seen.append(raw or b"")
        return raw

    monkeypatch.setattr("agentic_hil.configwrite.secure_optional_read_bytes", edit_then_read)
    try:
        described = tools.call(PROJECT_CONFIG_DESCRIBE)
    finally:
        tools.close()

    assert len(seen) == 1, "two reads of one file can straddle an edit"
    assert described["config_status"]["current_digest"] == config_digest(seen[0])
    # Not `unchanged`, because the document the keys below came from is not the
    # one this server loaded — which is precisely the pairing that used to be
    # reportable the wrong way round.
    assert described["config_status"]["state"] == STATE_CHANGED
    backend = next(entry for entry in described["writable_keys"] + described["locked_keys"] if entry["key"] == "debuggers.dut.executable")
    assert backend["current_value"] == FAKE_PYOCD.as_posix()


def test_a_configuration_that_will_not_parse_still_gets_the_block_it_promises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`project_config_describe` carries `config_status` whatever the state is.

    That is what it is for, so a refusal from it has to as well. A malformed or
    non-UTF-8 file used to escape as an internal error from under the MCP layer
    instead — on the one tool documented to answer while everything else is
    refusing."""
    workspace, path = bench(tmp_path, monkeypatch, **{CONFIG_DESCRIPTION_RIGHT: True})
    tools = service(workspace)
    try:
        path.write_text(path.read_text(encoding="utf-8") + "\nbroken: [unclosed\n", encoding="utf-8")
        malformed = tools.call(PROJECT_CONFIG_DESCRIBE)
        path.write_bytes(b"\xff\xfeversion: 2\n")
        undecodable = tools.call(PROJECT_CONFIG_DESCRIBE)
    finally:
        tools.close()

    assert malformed["ok"] is False
    assert malformed["error_type"] == "config_invalid"
    assert malformed["config_status"]["state"] == STATE_CHANGED
    # And the half nothing else can report once the document is unusable.
    assert malformed["permissions_in_force"][CONFIG_DESCRIPTION_RIGHT] is True
    assert malformed["writable_keys"] == []

    assert undecodable["ok"] is False
    assert undecodable["error_type"] == "config_unreadable"
    assert undecodable["config_status"]["state"] == STATE_UNREADABLE
    assert undecodable["permissions_in_force"][CONFIG_DESCRIPTION_RIGHT] is True


def test_a_write_to_a_configuration_that_will_not_parse_is_refused_in_words(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The same for the write door, which reads the document before it changes it."""
    workspace, path = bench(tmp_path, monkeypatch, **{CONFIG_DESCRIPTION_RIGHT: True})
    tools = service(workspace)
    before = path.read_text(encoding="utf-8")
    try:
        path.write_text(before + "\nbroken: [unclosed\n", encoding="utf-8")
        refused = tools.call(PROJECT_CONFIG_SET, {"changes": [{"key": "target.name", "value": "renamed"}]})
    finally:
        tools.close()

    assert refused["ok"] is False
    assert refused["error_type"] == "config_invalid"
    assert refused["config_status"]["state"] == STATE_CHANGED
    assert "line" in refused, "the parser's own position, so the file can be repaired"
