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

import pytest
import yaml
from conftest import FAKE_PYOCD, write_authoritative_config

from agentic_hil.cli import doctor
from agentic_hil.config import config_digest, load_authoritative_config
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
from agentic_hil.tools import AgenticHILToolService

COM_PORTS = 'com_ports:\n  dut_uart:\n    device: "COM9"\n    baudrate: 115200\n'


def bench(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, device_grants: dict[str, bool] | None = None, **grants: bool) -> tuple[Path, Path]:
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
    assert "changed since this server loaded it" in after["summary"]
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
    assert "debugger_info" in checked["config_status"]["compare_with_running_server"]
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
    assert "changed since this server loaded it" in checked["summary"]


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
