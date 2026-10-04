"""What the configuration surface says about itself, where two answers disagree.

Four reports, one surface, and every one of them is an answer contradicting
another answer about the same file. None of them is about hardware: each is
reproducible from a configuration bound through `AGENTIC_HIL_CONFIG`, an edit to
it, and the configuration tools.

**#688, one fault with two error types.** A configuration saved again as UTF-16
is `config_unreadable` to `project_config_describe` and `project_config_set`, and
`config_invalid` to `project_config_reload_description`, to a fresh load (which
is what startup performs) and to `agentic-hil doctor`. The reload answer carries
the second of those beside a `config_status` block that carries the first, so it
contradicts itself in one payload. The advice that travels with `config_invalid`
is about keys (`written_by_release`, `rejected_fields`, `allowed_fields`,
`agentic-hil schema`), and none of those fields is on a refusal whose whole
problem is the encoding. Which of the two types wins is the open decision in the
issue, so the tests here pin what holds either way: every path answers the same
one, the reload payload agrees with itself, and the advice names the encoding
rather than sending the caller to look for a bad key.

**#687, names that neither the file nor the key model has.**
`permission_differences` and `restart_required_for` flatten
`permission_summary`, which nests grants by section and entry without the
`permissions` level both the file and the key model have. They therefore report
`allow_recover` and `debuggers.dut.allow_flash` for keys spelled
`permissions.allow_recover` and `debuggers.dut.permissions.allow_flash`. Those
lists exist to be matched against the keys a caller just narrowed and to be
found in the file by an operator, and neither spelling can be.

**#686, a reload that reports the file adopted while old values stay in force.**
The reload takes four sections and lists the rest under `not_reloaded_sections`,
and then moves `config_digest` onto the whole file anyway. After a change that
lies only in `debug` or `artifacts` the answer says nothing moved, nothing is
waiting for a restart, and `config_status` calls the file byte-for-byte the one
in force, while the server keeps the startup values. The operator who narrowed
`artifacts.allowed_roots` is told the server now runs that file.

**#685, a restart asked for what a reload does.** Every successful
`project_config_set` and every applied `project_config_adopt_hardware` ends with
the same sentence about restarting the MCP server, whatever keys were written.
For `target`, `debuggers`, `com_ports` and `can_buses` keys the reload takes them
in place, which is what all three tool definitions say; a restart is owed for
`debug` keys and permission keys, and the advice that is right for those loses
its weight by being given for every key.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest
import yaml
from conftest import FAKE_GDB, write_authoritative_config
from test_config_adopt import attached, placeholder_bench

from agentic_hil.adopt import PROJECT_CONFIG_ADOPT
from agentic_hil.cli import doctor
from agentic_hil.config import ConfigError, load_authoritative_config
from agentic_hil.configreload import PROJECT_CONFIG_RELOAD
from agentic_hil.configstate import STATE_CHANGED, STATE_UNREADABLE
from agentic_hil.configwrite import PROJECT_CONFIG_DESCRIBE, PROJECT_CONFIG_SET
from agentic_hil.knowledge import (
    CONFIG_DESCRIPTION_RIGHT,
    CONFIG_PERMISSIONS_RIGHT,
    CONFIG_WRITE_RIGHT,
    resolve_config_key,
)
from agentic_hil.tools import AgenticHILToolService

COM_PORTS = 'com_ports:\n  dut_uart:\n    device: "COM9"\n    baudrate: 115200\n'
PROVENANCE = {"created_by": "agent", "created_via": "mcp:project_config_create", "created_at": "2026-01-01T00:00:00Z", "agentic_hil_version": "0.0.0"}


def bench(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **kwargs) -> tuple[Path, Path]:
    """One stlink probe and one COM port, version 2, both write grants open.

    Both configuration-write grants, because three of the four reports are about
    what an answer says after a write, and the fourth needs a file a caller could
    have narrowed through this surface. `allow_recover` is granted so that there
    is a project-scoped permission left to take away: it is the one the #687
    reproduction narrows, and unlike the three config rights it decides nothing
    about this surface, so closing it cannot change what the next call may do."""
    workspace = (tmp_path / "workspace").resolve()
    path = write_authoritative_config(
        workspace,
        monkeypatch,
        config_root=Path(os.environ["APPDATA"]) / "wave75-config",
        config_version=2,
        debugger_type="stlink",
        com_ports_yaml=COM_PORTS,
        **kwargs,
    )
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    document["provenance"] = dict(PROVENANCE)
    document["permissions"] = {
        CONFIG_WRITE_RIGHT: False,
        CONFIG_DESCRIPTION_RIGHT: True,
        CONFIG_PERMISSIONS_RIGHT: True,
        "allow_recover": True,
    }
    path.write_text("# A person wrote this bench.\n" + yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    monkeypatch.chdir(workspace)
    return workspace, path


def service(workspace: Path) -> AgenticHILToolService:
    return AgenticHILToolService(load_authoritative_config(workspace), frontend="mcp")


def rewrite(path: Path, edit) -> None:
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    edit(document)
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")


def changes(*pairs) -> dict:
    return {"changes": [{"key": key, "value": value} for key, value in pairs]}


def save_as_utf16(path: Path) -> None:
    """The same configuration, saved again the way a Windows editor may save it.

    Byte for byte the reproduction in #688: the document is unchanged and its
    encoding is the whole of what is wrong with it."""
    path.write_bytes(path.read_text(encoding="utf-8").encode("utf-16"))


# ---------------------------------------------------------------------------
# #688: one error type for a configuration that does not decode.


def answers_about_an_undecodable_configuration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, dict]:
    """Every path that meets the UTF-16 file, by the name that answers.

    The five the issue walks: the two tools that read the file to describe or
    change it, the reload, the fresh load `agentic-hil doctor` and server startup
    both perform, and `doctor` itself, whose answer is the one an operator is
    sent to compare against the others."""
    workspace, path = bench(tmp_path, monkeypatch)
    tools = service(workspace)
    try:
        save_as_utf16(path)
        answers = {
            PROJECT_CONFIG_DESCRIBE: tools.call(PROJECT_CONFIG_DESCRIBE, {}),
            PROJECT_CONFIG_SET: tools.call(PROJECT_CONFIG_SET, changes(("target.name", "renamed-target"))),
            PROJECT_CONFIG_RELOAD: tools.call(PROJECT_CONFIG_RELOAD),
        }
    finally:
        tools.close()
    answers["agentic_hil_doctor"] = doctor()
    with pytest.raises(ConfigError) as startup:
        load_authoritative_config(workspace)
    answers["startup"] = startup.value.to_dict()
    for name, answer in answers.items():
        assert answer["ok"] is False, (name, answer)
    return answers


def test_an_undecodable_configuration_is_one_error_type_on_every_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The whole of #688's expectation, and it is deliberately not a named type.

    Which of `config_unreadable` and `config_invalid` wins is the open decision
    in the issue, and either answer satisfies a caller: what no caller can act on
    is a fault whose name depends on which tool happened to meet it. So this
    compares the five answers against each other rather than against a constant,
    and it also settles the claim the `config_unreadable` advice already makes,
    that `agentic-hil doctor` "reads it the same way"."""
    answers = answers_about_an_undecodable_configuration(tmp_path, monkeypatch)

    answered = {name: answer["error_type"] for name, answer in answers.items()}
    assert len(set(answered.values())) == 1, answered


def test_the_advice_for_an_undecodable_configuration_names_the_encoding(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The other half of the report: the fix has to fit the fault.

    `config_invalid`'s advice is written for a file whose keys are wrong, and on
    this refusal none of the fields it tells the caller to read is present: there
    is no `field`, no `rejected_fields`, no `allowed_fields` and no
    `written_by_release`, because nothing in the document was ever parsed. An
    agent acting on it looks for a bad key or an upgrade for a file whose only
    problem is how it was saved."""
    answers = answers_about_an_undecodable_configuration(tmp_path, monkeypatch)

    for name, answer in answers.items():
        advice = " ".join([str(answer["summary"]), *answer.get("remediation", []), *answer.get("do_not", [])])
        assert "UTF-8" in advice, (name, advice)
        for about_a_key in ("written_by_release", "rejected_fields", "allowed_fields", "agentic-hil schema"):
            assert about_a_key not in advice, (name, about_a_key, advice)


def test_the_reload_refusal_agrees_with_the_config_status_it_carries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The self-contradiction, which is one payload rather than two answers.

    The reload carries `config_status` so that the refusal and the state of the
    file cannot disagree. On this file they do: the refusal is the loader's
    `config_invalid` and the block is `config_unreadable` with `state:
    unreadable`, so a caller reading one field is told something the next field
    denies."""
    workspace, path = bench(tmp_path, monkeypatch)
    tools = service(workspace)
    try:
        save_as_utf16(path)
        refused = tools.call(PROJECT_CONFIG_RELOAD)
    finally:
        tools.close()

    assert refused["ok"] is False, refused
    assert refused["config_status"]["state"] == STATE_UNREADABLE, refused["config_status"]
    assert refused["error_type"] == refused["config_status"]["error_type"], refused


# ---------------------------------------------------------------------------
# #687: the names a reload reports are the keys of the file.


def narrowed_both_halves(tools: AgenticHILToolService) -> dict:
    """The #687 reproduction: one grant inside a device entry, one on the project."""
    written = tools.call(
        PROJECT_CONFIG_SET,
        changes(("debuggers.dut.permissions.allow_flash", False), ("permissions.allow_recover", False)),
    )
    assert written["ok"] is True, written
    return written


def test_a_reload_names_the_grants_it_is_waiting_for_by_their_configuration_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Both lists, both spellings, in the shape the file and the key model use.

    The two grants are narrowed through the surface that defines the spelling:
    `project_config_set` took `debuggers.dut.permissions.allow_flash` and
    `permissions.allow_recover` a moment ago, and a caller that then reads
    `restart_required_for` is matching it against exactly those strings."""
    workspace, _ = bench(tmp_path, monkeypatch)
    tools = service(workspace)
    try:
        narrowed_both_halves(tools)
        result = tools.call(PROJECT_CONFIG_RELOAD)
    finally:
        tools.close()

    assert result["ok"] is True, result
    for reported_as in ("permission_differences", "restart_required_for"):
        reported = result[reported_as]
        assert "debuggers.dut.permissions.allow_flash" in reported, (reported_as, reported)
        assert "permissions.allow_recover" in reported, (reported_as, reported)
        assert "debuggers.dut.allow_flash" not in reported, (reported_as, reported)
        assert "allow_recover" not in reported, (reported_as, reported)


def test_every_grant_a_reload_reports_resolves_as_a_configuration_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Not only the two the reproduction narrows: the whole list has to be keys.

    `version` is the one name in `permission_view` that is not a settable key and
    is spelled the way the file spells it, so it is excluded by name rather than
    by being allowed to stand for every unresolvable entry. Everything else is a
    permission, and every permission in this file is a key `resolve_config_key`
    knows, which is the same model `project_config_describe` lists from."""
    workspace, path = bench(tmp_path, monkeypatch)
    tools = service(workspace)
    try:
        narrowed_both_halves(tools)
        rewrite(path, lambda document: document["com_ports"]["dut_uart"]["permissions"].update({"allow_write": False}))
        rewrite(path, lambda document: document["artifacts"].update({"allow_upload": False}))
        rewrite(path, lambda document: document["debug"].update({"allow_all_symbols": False}))
        result = tools.call(PROJECT_CONFIG_RELOAD)
        described = tools.call(PROJECT_CONFIG_DESCRIBE, {})
    finally:
        tools.close()

    assert result["ok"] is True, result
    reported = [name for name in result["permission_differences"] if name != "version"]
    assert len(reported) >= 5, result["permission_differences"]
    assert [name for name in reported if resolve_config_key(name) is None] == [], reported
    # The same list read the other way round: these are keys this caller was
    # just shown, so the match an operator or a caller makes cannot fail.
    listed = {str(entry["key"]) for entry in [*described["writable_keys"], *described["locked_keys"]]}
    assert [name for name in reported if name not in listed] == [], (reported, sorted(listed))


# ---------------------------------------------------------------------------
# #686: a difference the reload cannot take is still a difference.


def test_a_reload_that_cannot_take_an_artifacts_change_says_a_restart_is_owed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The report's own walkthrough: a narrowed `artifacts.allowed_roots`.

    The operator took a root away from what may be flashed, and after the reload
    every field of the answer says the file is the one in force: nothing moved,
    nothing is waiting, and `config_status` calls the document byte-for-byte the
    one this server loaded. The server still accepts artifacts from the removed
    root, so the one sentence that was true before the reload (`changed`,
    `reload_required: true`) is the one the reload took away."""
    workspace, path = bench(tmp_path, monkeypatch, allowed_roots=["build", "release"])
    for root in ("build", "release"):
        (workspace / root).mkdir(parents=True, exist_ok=True)
    tools = service(workspace)
    try:
        in_force = tools.config.artifacts.allowed_roots
        assert len(in_force) == 2, in_force
        rewrite(path, lambda document: document["artifacts"].update({"allowed_roots": ["build"]}))
        before = tools.call(PROJECT_CONFIG_DESCRIBE, {})
        assert before["config_status"]["state"] == STATE_CHANGED, before["config_status"]

        result = tools.call(PROJECT_CONFIG_RELOAD)

        assert result["ok"] is True, result
        # The premise, and the half of the behaviour that is correct: the reload
        # does not take this section, so the old roots are still what is enforced.
        assert tools.config.artifacts.allowed_roots == in_force
        assert [name for name in result["restart_required_for"] if name == "artifacts" or name.startswith("artifacts.")], result["restart_required_for"]
        assert "nothing moved" not in result["summary"], result["summary"]
        assert result["config_status"]["state"] == STATE_CHANGED, result["config_status"]
        assert result["config_status"]["reload_required"] is True, result["config_status"]

        after = tools.call(PROJECT_CONFIG_DESCRIBE, {})
        assert after["config_status"]["state"] == STATE_CHANGED, after["config_status"]
        assert after.get("config_stale") is True, after["config_status"]
    finally:
        tools.close()


def test_a_reload_that_cannot_take_a_debug_change_says_a_restart_is_owed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The second way in, and the one a tool reaches on its own.

    `debug.gdb_executable` is settable under `allow_config_description_write` and
    `project_config_adopt_hardware` writes it, while both of those answers send
    the caller to the reload afterwards. The reload does not take `debug`, so the
    GDB that reads this bench's images is still the startup one, and the answer
    has to keep saying so."""
    workspace, _ = bench(tmp_path, monkeypatch)
    tools = service(workspace)
    try:
        in_force = tools.config.debug.gdb_executable
        written = tools.call(PROJECT_CONFIG_SET, changes(("debug.gdb_executable", FAKE_GDB.as_posix())))
        assert written["ok"] is True, written
        assert load_authoritative_config(workspace).debug.gdb_executable == str(FAKE_GDB)

        result = tools.call(PROJECT_CONFIG_RELOAD)

        assert result["ok"] is True, result
        assert tools.config.debug.gdb_executable == in_force
        assert [name for name in result["restart_required_for"] if name == "debug" or name.startswith("debug.")], result["restart_required_for"]
        assert "nothing moved" not in result["summary"], result["summary"]
        assert result["config_status"]["state"] == STATE_CHANGED, result["config_status"]

        after = tools.call(PROJECT_CONFIG_DESCRIBE, {})
        assert after["config_status"]["state"] == STATE_CHANGED, after["config_status"]
        assert after.get("config_stale") is True, after["config_status"]
    finally:
        tools.close()


# ---------------------------------------------------------------------------
# #685: the next steps follow the keys that were written.


def restart_steps(result: dict) -> list[str]:
    def requires_restart(step: str) -> bool:
        clauses = re.split(r"[,:;.!?]|\b(?:and|but|however)\b", step.lower())
        return any(
            "restart" in clause
            and "server" in clause
            and not re.search(r"\b(?:not|never|no|unnecessary|unneeded)\b", clause)
            for clause in clauses
        )

    return [step for step in result["next_steps"] if requires_restart(step)]


def reload_steps(result: dict) -> list[str]:
    return [step for step in result["next_steps"] if PROJECT_CONFIG_RELOAD in step]


def test_a_write_the_reload_takes_sends_the_caller_to_the_reload(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """One key, in a section the reload takes, and the restart is not owed.

    The second half is the proof rather than the claim: the reload is called and
    the new name is in force, in this process, without anything having been
    restarted. On a host where a restart ends the session, the step this answer
    carried was the difference between a caller carrying on and a caller
    stopping."""
    workspace, _ = bench(tmp_path, monkeypatch)
    tools = service(workspace)
    try:
        written = tools.call(PROJECT_CONFIG_SET, changes(("target.name", "renamed-target")))
        assert written["ok"] is True, written

        assert reload_steps(written), written["next_steps"]
        assert restart_steps(written) == [], written["next_steps"]

        reloaded = tools.call(PROJECT_CONFIG_RELOAD)
        assert reloaded["ok"] is True, reloaded
        assert "target.name" in reloaded["description_changes"], reloaded["description_changes"]
        assert tools.config.target.name == "renamed-target"
    finally:
        tools.close()


def test_a_debug_key_keeps_the_restart_step_and_is_named_by_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The advice that is right, kept for the keys it is right about.

    `debug` is not a section the reload takes, so this is the case where a
    restart really is the only way the value reaches the running server. What the
    step has to add is which key it is about: "something you wrote needs a
    restart" is exactly as actionable as the sentence every write used to end
    with."""
    workspace, _ = bench(tmp_path, monkeypatch)
    tools = service(workspace)
    try:
        written = tools.call(PROJECT_CONFIG_SET, changes(("debug.gdb_executable", FAKE_GDB.as_posix())))
    finally:
        tools.close()

    assert written["ok"] is True, written
    asked = restart_steps(written)
    assert asked, written["next_steps"]
    assert "debug.gdb_executable" in " ".join(asked), asked


def test_a_permission_key_keeps_the_restart_step_and_is_named_by_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The other half a reload re-reads in neither direction.

    A narrowed grant is the case where the restart step matters most, because
    until it happens the server is still enforcing the wider permission the
    operator has just taken away."""
    workspace, _ = bench(tmp_path, monkeypatch)
    tools = service(workspace)
    try:
        written = tools.call(PROJECT_CONFIG_SET, changes(("debuggers.dut.permissions.allow_flash", False)))
    finally:
        tools.close()

    assert written["ok"] is True, written
    asked = restart_steps(written)
    assert asked, written["next_steps"]
    assert "debuggers.dut.permissions.allow_flash" in " ".join(asked), asked


def test_a_mixed_write_asks_for_both_and_says_which_key_needs_which(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Both halves in one call, which is where a single sentence cannot do.

    One key the reload takes and one it does not, written together because
    `project_config_set` applies all or none. The answer owes the caller both
    next steps at once, and the restart one still has to name the key it is
    about: how the two are worded is the implementation's to choose, which is
    why this asserts that both are there and that the restart names the `debug`
    key, and not that the device key is absent from it."""
    workspace, _ = bench(tmp_path, monkeypatch)
    tools = service(workspace)
    try:
        written = tools.call(PROJECT_CONFIG_SET, changes(("target.name", "renamed-target"), ("debug.gdb_executable", FAKE_GDB.as_posix())))
    finally:
        tools.close()

    assert written["ok"] is True, written
    assert reload_steps(written), written["next_steps"]
    asked = restart_steps(written)
    assert asked, written["next_steps"]
    assert "debug.gdb_executable" in " ".join(asked), asked


def test_an_adoption_that_wrote_only_device_keys_sends_the_caller_to_the_reload(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Adoption follows the keys it wrote, like the call it writes through.

    Everything this plan carries is a probe serial, a toolchain for the entry, a
    detected controller and the probe's own COM device: four sections the reload
    takes. The `apply` description of this very tool already says to call
    `project_config_reload_description` afterwards, and the answer sent the
    operator to restart the server instead."""
    workspace, _ = placeholder_bench(tmp_path, monkeypatch, **{CONFIG_DESCRIPTION_RIGHT: True})
    attached(monkeypatch)
    tools = service(workspace)
    try:
        applied = tools.call(PROJECT_CONFIG_ADOPT, {"apply": True})
    finally:
        tools.close()

    assert applied["ok"] is True, applied
    assert applied["applied"] is True, applied
    assert [item["key"] for item in applied["carried"]], applied
    assert reload_steps(applied), applied["next_steps"]
    assert restart_steps(applied) == [], applied["next_steps"]


def test_an_adoption_that_wrote_the_gdb_keeps_the_restart_step_for_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The one key adoption writes that the reload does not take.

    `debug.gdb_executable` is in the plan whenever this host answers with a GDB,
    so adoption is a path to the restart case as much as to the reload one, and
    the same rule has to decide which sentence it ends with."""
    workspace, _ = placeholder_bench(tmp_path, monkeypatch, **{CONFIG_DESCRIPTION_RIGHT: True})
    attached(monkeypatch, gdb_executable=FAKE_GDB.as_posix())
    tools = service(workspace)
    try:
        applied = tools.call(PROJECT_CONFIG_ADOPT, {"apply": True})
    finally:
        tools.close()

    assert applied["ok"] is True, applied
    carried = {item["key"] for item in applied["carried"]}
    assert {"debug.gdb_executable", "debuggers.dut.executable", "target.controller"} <= carried, applied["carried"]
    assert reload_steps(applied), applied["next_steps"]
    asked = restart_steps(applied)
    assert asked, applied["next_steps"]
    assert "debug.gdb_executable" in " ".join(asked), asked
