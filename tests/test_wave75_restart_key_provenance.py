"""Every name a reload reports is a key somebody can find in the file.

#687 settled that for the grants: `restart_required_for` names
`debuggers.dut.permissions.allow_flash` and not a spelling the key model has
never heard of, because that list is matched by a caller against the keys it
just wrote and read by an operator looking for each one in the file. #686 then
gave that list a second source: the sections a reload does not take, so an
operator who narrowed `artifacts.allowed_roots` is no longer told the server now
runs that file.

The second source is built by serializing the loaded dataclasses, and two of
those carry a field that is not a configuration key at all. The GDB one was
removed. This module is about the other, and it is the same defect:
``recovery.auto_recover_explicit``.

``config.recovery_config`` derives it as ``"auto_recover" in raw``: not from what
the key says, from whether the document mentions it. A bench that never chose a
policy still gets ``reset_halt``, and the flag is what lets machine recovery say
so in the audit (``auto_recover_policy_source``) and warn the first time it
resets a target under a policy nobody picked. It is a fact about the load, the
schema declares no such key (``recovery`` has exactly ``auto_recover`` and
``max_attempts``, with ``additionalProperties: false``), `project_config_describe`
cannot list it and `project_config_set` cannot write it.

Serialized into the restart view it moves on its own in one ordinary case: an
operator writes down the policy their bench was already running, `auto_recover:
"reset_halt"`, the value in force does not change, and the reload answers that a
restart is owed for `recovery.auto_recover_explicit`. `config_status` then repeats
it, with "Restart the MCP server to adopt them" against a key that is in no file
and that a restart would not let anybody find.

So the tests here are three statements:

* the marker is never a reported key, asserted on the derived view, on the
  comparison that produces the list, and on the public reload answer;
* a real `recovery` change is still reported, by its own key, and the loaded
  policy still stays in force until a restart, so the repair is a filter and not
  a hole;
* the marker itself is untouched: still derived from key presence, still on the
  configuration object, still in force after a reload that does not adopt it.
  The read sites are covered where they already are, against a run that recovers
  (`tests/test_run_abort_recovery.py`, `tests/test_coordination.py`); what this
  module fences is the field surviving the fix at all.
"""

from __future__ import annotations

import os
from dataclasses import asdict, replace
from pathlib import Path

import pytest
import yaml
from conftest import write_authoritative_config

from agentic_hil.config import load_authoritative_config, permission_summary
from agentic_hil.configreload import (
    PROJECT_CONFIG_RELOAD,
    restart_required_changes,
    restart_view,
)
from agentic_hil.configstate import STATE_CHANGED, STATE_UNCHANGED
from agentic_hil.tools import AgenticHILToolService
from agentic_hil.types import RecoveryConfig

COM_PORTS = 'com_ports:\n  dut_uart:\n    device: "COM9"\n    baudrate: 115200\n'

# The field this module is about, and the dotted name it reaches a caller under.
MARKER = "auto_recover_explicit"
MARKER_KEY = f"recovery.{MARKER}"
# What the shipped schema declares for `recovery`, and therefore every name this
# section may contribute to a list of keys waiting for a restart. The section is
# `additionalProperties: false`, so this really is the whole set.
RECOVERY_KEYS = {"auto_recover", "max_attempts"}


def bench(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **kwargs) -> tuple[Path, Path]:
    """One stlink probe and one COM port, version 2, `recovery:` as asked.

    No configuration-write grant, deliberately. `recovery` is in no key model, so
    `project_config_set` cannot reach it and the hand edit below is how an
    operator changes this section, which is the way in #686 describes."""
    workspace = (tmp_path / "workspace").resolve()
    path = write_authoritative_config(
        workspace,
        monkeypatch,
        config_root=Path(os.environ["APPDATA"]) / "wave75-restart-keys",
        config_version=2,
        debugger_type="stlink",
        com_ports_yaml=COM_PORTS,
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


def names_the_policy(value: str):
    """Write `recovery.auto_recover` down, keeping whatever else the section has."""

    def edit(document: dict) -> None:
        document["recovery"] = {**(document.get("recovery") or {}), "auto_recover": value}

    return edit


# ---------------------------------------------------------------------------
# The marker is never a reported key.


def test_the_restart_view_carries_only_the_recovery_keys_the_schema_declares(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The view is a key list, so its `recovery` section is the schema's two keys.

    Asserted as the whole set rather than as the marker's absence, because the
    rule is not "drop this one name": what belongs in a list an operator reads
    the file with is what the file can carry, and `additionalProperties: false`
    on this section says exactly what that is."""
    workspace, _ = bench(tmp_path, monkeypatch)
    config = load_authoritative_config(workspace)

    view = restart_view(config)

    assert set(view["recovery"]) == RECOVERY_KEYS, view["recovery"]
    # The two real keys keep their values, so this is a filter and not an empty
    # section: the default policy is still what the view reports it to be.
    assert view["recovery"]["auto_recover"] == "reset_halt", view["recovery"]
    assert view["recovery"]["max_attempts"] == 3, view["recovery"]


def test_a_marker_that_moved_on_its_own_is_no_restart_requirement(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The comparison itself, against two configurations that differ by the flag
    and by nothing else.

    Built with `dataclasses.replace` rather than from two files, so there is no
    other candidate for what moved: same policy, same attempt count, same
    everything, one boolean of load provenance apart."""
    workspace, _ = bench(tmp_path, monkeypatch)
    loaded = load_authoritative_config(workspace)
    assert loaded.recovery.auto_recover_explicit is False

    named = replace(loaded, recovery=replace(loaded.recovery, auto_recover_explicit=True))

    assert named.recovery.auto_recover == loaded.recovery.auto_recover
    assert named.recovery.max_attempts == loaded.recovery.max_attempts
    assert restart_required_changes(loaded, named) == []
    # And in the other direction, so this is about the field rather than about
    # which side of the comparison it sits on.
    assert restart_required_changes(named, loaded) == []


def test_writing_down_the_policy_already_in_force_is_not_a_restart(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The whole defect as a caller meets it, through the tool.

    The bench never named a policy, so it runs the `reset_halt` default. The
    operator writes that same value into the file, which is the thing the
    recovery warning asks them to do. Nothing in force moves, and the answer has
    to say so: a restart owed for `recovery.auto_recover_explicit` sends somebody
    to restart a server to adopt a key they cannot find in the document they just
    edited."""
    workspace, path = bench(tmp_path, monkeypatch)
    tools = service(workspace)
    try:
        assert tools.config.recovery.auto_recover == "reset_halt"
        assert tools.config.recovery.auto_recover_explicit is False
        grants = permission_summary(tools.config)

        rewrite(path, names_the_policy("reset_halt"))
        # The premise: the document now states the policy that was already in
        # force, so no value an operator can read has changed.
        on_disk = load_authoritative_config(workspace)
        assert on_disk.recovery.auto_recover == "reset_halt"
        assert on_disk.recovery.auto_recover_explicit is True

        result = tools.call(PROJECT_CONFIG_RELOAD)

        assert result["ok"] is True, result
        assert MARKER_KEY not in result["restart_required_for"], result["restart_required_for"]
        assert result["restart_required_for"] == [], result["restart_required_for"]
        assert "nothing moved" in result["summary"], result["summary"]
        assert "restart" not in result["summary"].lower(), result["summary"]
        assert not [step for step in result["next_steps"] if MARKER in step], result["next_steps"]

        # And the status does not repeat the claim. This is where it reached an
        # operator in words: "Restart the MCP server to adopt them", naming a key
        # that is in no file.
        status = result["config_status"]
        assert status["state"] == STATE_UNCHANGED, status
        assert MARKER_KEY not in status.get("restart_required_for", []), status
        assert MARKER not in status["summary"], status["summary"]

        assert permission_summary(tools.config) == grants, "no grant moves for a recovery edit"
    finally:
        tools.close()


# ---------------------------------------------------------------------------
# A real recovery change is still reported, by its own key.


def test_a_changed_recovery_policy_is_reported_under_its_own_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The guard that keeps the repair a filter: `off` is a different policy.

    `auto_recover` decides whether this machine may drive a physical reset on its
    own, so a file that moved it and a server that has not adopted it is exactly
    what `restart_required_for` exists to report. The marker moves here too (the
    key was not named before), and the answer must name the policy and only the
    policy."""
    workspace, path = bench(tmp_path, monkeypatch)
    tools = service(workspace)
    try:
        rewrite(path, names_the_policy("off"))

        result = tools.call(PROJECT_CONFIG_RELOAD)

        assert result["ok"] is True, result
        assert result["restart_required_for"] == ["recovery.auto_recover"], result["restart_required_for"]
        assert "recovery.auto_recover" in result["summary"], result["summary"]
        assert tools.config.recovery.auto_recover == "reset_halt", "the policy this server loaded stays in force"

        status = result["config_status"]
        assert status["state"] == STATE_CHANGED, status
        assert status["restart_required_for"] == ["recovery.auto_recover"], status
    finally:
        tools.close()


def test_a_changed_attempt_count_is_reported_under_its_own_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The other real key of the section, on a bench that already named a policy.

    Named, so the marker is true on both sides and cannot be what the answer is
    reporting. `max_attempts` decides how often machine recovery tries before it
    defers to a person, and it is deferred to a restart like everything else
    outside the four device sections."""
    workspace, path = bench(tmp_path, monkeypatch, auto_recover="reset_halt", recovery_max_attempts=3)
    tools = service(workspace)
    try:
        assert tools.config.recovery.auto_recover_explicit is True
        assert tools.config.recovery.max_attempts == 3

        rewrite(path, lambda document: document["recovery"].update({"max_attempts": 5}))

        result = tools.call(PROJECT_CONFIG_RELOAD)

        assert result["ok"] is True, result
        assert result["restart_required_for"] == ["recovery.max_attempts"], result["restart_required_for"]
        assert tools.config.recovery.max_attempts == 3, "the attempt count this server loaded stays in force"
        assert result["config_status"]["state"] == STATE_CHANGED, result["config_status"]
    finally:
        tools.close()


# ---------------------------------------------------------------------------
# The marker itself is untouched.


@pytest.mark.parametrize(
    ("written", "explicit"),
    [
        pytest.param(None, False, id="never-named"),
        # The case the whole module turns on: the same value the default already
        # had, written down, so only the provenance differs.
        pytest.param("reset_halt", True, id="named-as-the-default"),
        pytest.param("off", True, id="named-as-something-else"),
    ],
)
def test_the_marker_is_still_derived_from_whether_the_key_is_named(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, written: str | None, explicit: bool) -> None:
    """`"auto_recover" in raw`, unchanged, and still on the loaded object.

    The wrong way to stop reporting a field is to stop having it. This says the
    field is declared, derived the way it was, and present in the serialized
    policy, so a repair that reached past the derived view would be caught here
    rather than in whatever recovery the audit later understates."""
    assert MARKER in RecoveryConfig.__dataclass_fields__
    workspace, _ = bench(tmp_path, monkeypatch, **({} if written is None else {"auto_recover": written}))

    config = load_authoritative_config(workspace)

    assert config.recovery.auto_recover_explicit is explicit
    assert config.recovery.auto_recover == (written or "reset_halt")
    # Still in the configuration object's own serialization. Only the derived
    # restart view filters it; `asdict` on the policy is not that view.
    assert asdict(config.recovery)[MARKER] is explicit


def test_the_marker_stays_in_force_across_a_reload_that_does_not_adopt_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`recovery` is not a reloaded section, so the policy object and its
    provenance are the ones this server loaded, before and after.

    The reload takes a device key in the same call, so this is not the trivial
    case where nothing happened at all."""
    workspace, path = bench(tmp_path, monkeypatch, auto_recover="off")
    tools = service(workspace)
    try:
        loaded = tools.config.recovery
        assert loaded == RecoveryConfig(auto_recover="off", max_attempts=3, auto_recover_explicit=True)

        rewrite(path, lambda document: document["com_ports"]["dut_uart"].update({"baudrate": 9600}))
        result = tools.call(PROJECT_CONFIG_RELOAD)

        assert result["ok"] is True, result
        assert "com_ports.dut_uart.baudrate" in result["description_changes"], result
        assert tools.config.com_ports["dut_uart"].baudrate == 9600, "the reload did take something"
        assert tools.config.recovery == loaded
        assert tools.config.recovery.auto_recover_explicit is True
        assert MARKER_KEY not in result["restart_required_for"], result["restart_required_for"]
    finally:
        tools.close()
