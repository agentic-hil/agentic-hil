"""What a second workspace is told when a neighbour's incident holds the bench.

Two projects on one machine can share one probe. When a session in one workspace
ends without standing down, it leaves a coordination record on the shared
resources and the next call from the second workspace is refused with
`resource_quarantined`. That refusal is correct. What follows it is not: the
refusal names no way forward, and every read-only command the operator then runs
in the second workspace answers that the bench is fine (#531). The incident that
produced the report was recoverable under its own project's policy the whole
time, and nothing said so, so it stood until somebody walked into the owning
workspace by hand (#534).

These are the first tests that reach the cross-project branch for anything
beyond its error type: the refusal's reasons, the owning workspace, the derived
claim about who can settle it, and the three commands that answer for the bench.

The four surfaces under test, and the two neighbours that must not move:

* the refusal itself, which has the foreign record open and drops everything in
  it but the quarantine id,
* `hardware_lease_status`, which answers machine-wide for live holds through
  `device_holds` and per project for everything else, then states in words that
  nothing on this bench is standing,
* `doctor`, which reports a ready bench three seconds before the first call is
  refused,
* `recover` in the blocked workspace, which routes through that same status,
* and, unchanged: a single workspace whose own incident stands, and a clean
  bench, where all of this stays silent.

Nothing here asks for a second workspace to be able to clear a neighbour's
incident, and nothing releases anything: the refusal stays a refusal and the
signature stays where it was raised. What changes is only what the operator is
told. Nothing may leak either: the project identifier is a digest, and no
absolute path, environment-derived path or workspace may reach any of these
answers.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from conftest import write_config

from agentic_hil.adopt import PROJECT_CONFIG_ADOPT, _release_refusal
from agentic_hil.cli import doctor, entrypoint
from agentic_hil.config import load_config
from agentic_hil.coordination import (
    DEBUGGER_DISCOVERY_RESOURCE,
    CoordinationError,
    HardwareCoordinator,
    debugger_effect_resources,
    debugger_resource,
)
from agentic_hil.knowledge import attach_quarantine_guidance, catalogue_entry, remediation_fields

# The reasons the reported incident carried, both of them in the reset-halt
# recoverable set, which is what makes it the case #534 is about.
FOREIGN_REASONS = ("debug_target_state_unconfirmed", "debug_session_cleanup_unconfirmed")
# Distinct per test file: the device lock root is machine-wide by construction,
# so a probe name shared with another test would contend rather than answer.
SHARED_PROBE_ID = "CROSSPROJECTPROBE"
# The free-bench claim that is false while a neighbour's incident stands.
FREE_BENCH_SENTENCE = "Nothing on this bench is held and no incident is standing."
# What `recover` answers today in the workspace whose every hardware call is
# refused.
NOTHING_STANDING_SENTENCE = "Nothing on this bench is standing."


def config_for(workspace: Path, **kwargs):
    kwargs.setdefault("probe_id", SHARED_PROBE_ID)
    return load_config(str(write_config(workspace, **kwargs)))


def foreign_incident(tmp_path: Path, *, auto_recover: str | None = None, reasons: tuple[str, ...] = FOREIGN_REASONS, audit_broken: bool = False) -> tuple[HardwareCoordinator, str]:
    """One workspace leaves an unresolved incident on the shared resources.

    Exactly the shape the report describes: the discovery pseudo-resource and
    the shared probe under one incident, the owner gone. The coordinator is
    returned closed, for its `project_key` and its configuration.
    """
    owner = HardwareCoordinator(config_for(tmp_path / "owning-workspace", auto_recover=auto_recover), "owning-workspace")
    lease = owner.acquire(*debugger_effect_resources(owner.config))
    for reason in reasons:
        lease.quarantine(reason, audit_broken=audit_broken)
    quarantine_id = str(lease.quarantine_id)
    owner.close()
    return owner, quarantine_id


def blocked_workspace(tmp_path: Path, **kwargs) -> HardwareCoordinator:
    """The second workspace on the same machine and the same physical probe."""
    return HardwareCoordinator(config_for(tmp_path / "blocked-workspace", **kwargs), "blocked-workspace")


def refusal_of(coordinator: HardwareCoordinator) -> dict[str, Any]:
    with pytest.raises(CoordinationError) as excinfo:
        coordinator.acquire(*debugger_effect_resources(coordinator.config))
    return dict(excinfo.value.result)


def standing_incidents(payload: dict[str, Any]) -> list[dict[str, Any]]:
    section = payload.get("standing_incidents")
    assert isinstance(section, list), payload
    return [entry for entry in section if isinstance(entry, dict)]


def carries_no_path(payload: object, *forbidden: str) -> None:
    """No absolute path, no environment-derived path, no workspace.

    Applied to the answers this issue adds, because the record they are built
    from carries the neighbour's workspace and configuration path and the honest
    change carries the digest and nothing more.
    """
    text = json.dumps(payload, default=str)
    for path in forbidden:
        assert path not in text, text
        assert Path(path).as_posix() not in text, text

    def walk(value: object) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                assert key not in {"workspace", "config_path"}, value
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(payload)


def test_the_cross_project_refusal_carries_the_reasons_the_owner_and_a_next_step(tmp_path: Path) -> None:
    """agentic-hil/agentic-hil#531: the refusal drops the record it has open.

    The branch builds a literal seven-field result and lifts only the quarantine
    id out of the foreign record, while every other quarantine refusal on this
    path goes through `_quarantined_result`, whose own comment says an
    acquire-time refusal names the reason too and not only `lease-status`. The
    record it just read carries the reasons the incident was raised for and the
    owning project's digest, and both are what the caller has no other way to
    learn: the reasons key the per-reason guidance, and the digest is the only
    thing that identifies the workspace where a recovery would be signed.

    Existing fields and the error type are unchanged, because callers branch on
    them and the refusal is still a refusal.
    """
    owner, quarantine_id = foreign_incident(tmp_path)
    second = blocked_workspace(tmp_path)
    try:
        refusal = refusal_of(second)
    finally:
        second.close()

    # Unchanged: the same error type, the same summary, the same fields.
    assert refusal["error_type"] == "resource_quarantined"
    assert refusal["summary"] == "Physical resource belongs to another unresolved project incident."
    assert refusal["resource"] == DEBUGGER_DISCOVERY_RESOURCE
    assert refusal["cleanup_required"] is True
    assert refusal["quarantined"] is True
    assert refusal["retry_safe"] is False
    assert refusal["quarantine_id"] == quarantine_id
    # New, and all of it out of the record the branch already opened.
    assert refusal["cleanup_reasons"] == list(FOREIGN_REASONS)
    assert refusal["project_resource"] == owner.project_key
    next_step = str(refusal["next_step"])
    assert owner.project_key in next_step
    assert "lease-status" in next_step and "recover" in next_step
    carries_no_path(refusal, str(tmp_path))


def test_the_refusals_reasons_reach_the_per_reason_guidance(tmp_path: Path) -> None:
    """The guidance is missing for two independent reasons, and this is the first.

    `attach_quarantine_guidance` is applied to every tool result and returns the
    result unchanged when no reason is named, so a refusal that drops
    `cleanup_reasons` suppresses guidance that exists in the build. The second
    reason of the two names, as the unknown an operator has to resolve, whether a
    server process still holds the probe, which is precisely the fact the second
    workspace needed.
    """
    foreign_incident(tmp_path)
    second = blocked_workspace(tmp_path)
    try:
        refusal = refusal_of(second)
    finally:
        second.close()

    guided = attach_quarantine_guidance(refusal)

    assert sorted(guided["quarantine_guidance"]) == sorted(FOREIGN_REASONS)


def test_resource_quarantined_has_a_catalogue_entry_that_reaches_a_result() -> None:
    """The gap that is independent of the other three.

    `ERROR_CATALOGUE` has no `resource_quarantined` key, so `remediation_fields`
    returns an empty mapping including where the product already asks for it, and
    the browsable error reference has no page for the refusal that stops a bench
    outright. The retryable neighbour `device_busy` has an entry.
    """
    entry = catalogue_entry("resource_quarantined")

    assert entry is not None
    assert entry["error_type"] == "resource_quarantined"
    assert entry["meaning"]
    assert entry["remediation"]
    # The lookup every refusal spreads, and the one that returned an empty
    # mapping for the refusal that stops a bench outright.
    assert remediation_fields("resource_quarantined")["remediation"] == entry["remediation"]
    # And it reaches the result that already asks for it: the adopt path's
    # release refusal spreads `remediation_fields` and got nothing back.
    refusal = _release_refusal({"probe_id": "probe", "side_effect_committed": False}, {"lease_state": "quarantined"}, PROJECT_CONFIG_ADOPT)
    assert refusal["remediation"] == entry["remediation"]


def test_lease_status_in_the_blocked_workspace_names_the_standing_incident(tmp_path: Path) -> None:
    """agentic-hil/agentic-hil#531: the command answers about the wrong scope, then overstates it.

    `status` derives `blocked`, `incident_stands`, `cleanup_reasons` and
    `auto_recoverable` from this project's own record, which on the second
    workspace is absent or clean, and then states in words that nothing on this
    bench is held and no incident is standing while the next hardware call is
    refused. It is not a per-project command by design: `device_holds` already
    probes the machine-wide device locks and names a foreign holder, so the
    command answers across workspaces for a live hold and not for an unresolved
    record on the same devices, and the second is the one that refuses.

    `blocked` keeps meaning that THIS project is blocked, because callers read it
    that way. The standing incident is its own section, over the resources the
    command already walks, and the summary sentence is conditioned on it.
    """
    owner, quarantine_id = foreign_incident(tmp_path)
    second = blocked_workspace(tmp_path)
    try:
        status = second.status()
    finally:
        second.close()

    # Unchanged, because callers branch on it: this project is not blocked.
    assert status["blocked"] is False
    assert status["incident_stands"] is False
    assert status["cleanup_reasons"] == []
    # The section, over the devices `device_holds` walks plus the discovery
    # pseudo-resource, which is where the reported incident sat.
    entries = standing_incidents(status)
    assert {entry["resource"] for entry in entries} == {DEBUGGER_DISCOVERY_RESOURCE, debugger_resource(second.config)}
    for entry in entries:
        assert entry["quarantine_id"] == quarantine_id
        assert entry["cleanup_reasons"] == list(FOREIGN_REASONS)
        assert entry["project_resource"] == owner.project_key
    # The false claim, and the part that had to change.
    assert FREE_BENCH_SENTENCE not in status["summary"]
    assert "another project" in status["summary"]
    carries_no_path(status, str(tmp_path))


def test_doctor_reports_the_bench_that_refuses_its_next_call(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """agentic-hil/agentic-hil#531: doctor never asks.

    It runs a config load, a state root test, a bench binding test and a per
    debugger toolchain check, builds no coordinator and reads no coordination
    record. On a bench in exactly this state it reported the state root ok, the
    binding ok and every device named, and the first flash three seconds later
    was refused. Doctor is where somebody looks before a run rather than after a
    refusal, so it carries the same section and the same distinction.
    """
    owner, quarantine_id = foreign_incident(tmp_path)
    second_config = config_for(tmp_path / "blocked-workspace")
    monkeypatch.setattr("agentic_hil.cli.load_cli_authoritative_config", lambda path: second_config)

    report = doctor()

    entries = standing_incidents(report)
    assert {entry["resource"] for entry in entries} == {DEBUGGER_DISCOVERY_RESOURCE, debugger_resource(second_config)}
    for entry in entries:
        assert entry["quarantine_id"] == quarantine_id
        assert entry["cleanup_reasons"] == list(FOREIGN_REASONS)
        assert entry["project_resource"] == owner.project_key
    # A caller that keeps only the headline has to be told too, because the
    # verdict this command exists to give is whether the next call will work.
    assert "another project" in report["summary"]
    carries_no_path(entries, str(tmp_path))


def test_recover_in_the_blocked_workspace_stops_saying_nothing_is_standing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """agentic-hil/agentic-hil#531: the third dead end.

    `recover` routes through the same status read, finds nothing standing for
    this project and answers `nothing_to_recover`, and it is the command the
    documented operator path sends the caller to with the id the refusal named.
    It still clears nothing: a second workspace may not settle a neighbour's
    incident, and that is the rule this test keeps. What it must stop doing is
    telling the operator that the bench is free.
    """
    owner, quarantine_id = foreign_incident(tmp_path)
    second_config = config_for(tmp_path / "blocked-workspace")
    monkeypatch.setattr("agentic_hil.cli.load_cli_authoritative_config", lambda path: second_config)

    entrypoint(["recover", "--confirm-safe-state", "--quarantine-id", quarantine_id, "--json"])
    answer = json.loads(capsys.readouterr().out)

    # Nothing was released and nothing was signed for: the incident is not this
    # workspace's to settle.
    assert answer["ok"] is True
    assert answer["was_quarantined"] is False
    assert answer["resources"] == []
    # And the answer no longer contradicts the refusal the caller just met.
    assert NOTHING_STANDING_SENTENCE not in answer["summary"]
    entries = standing_incidents(answer)
    assert {entry["quarantine_id"] for entry in entries} == {quarantine_id}
    assert {entry["project_resource"] for entry in entries} == {owner.project_key}
    carries_no_path(answer, str(tmp_path))


def test_a_foreign_incident_the_owning_workspace_would_settle_itself_is_named_as_such(tmp_path: Path) -> None:
    """agentic-hil/agentic-hil#534: recoverable the whole time, and standing for twenty three hours.

    Both reasons are in the reset-halt recoverable set, the owning bench runs
    that policy and its probe grants the reset, so any hardware call in that
    workspace would have settled the incident on its own evidence. Every call
    from here was refused instead, and nothing said which of the two cases this
    was: an incident that needs a signature at the bench, or one that needs
    somebody to run the owning workspace.

    Derived under the FOREIGN project's policy and grants, never this project's:
    the asking workspace here has recovery switched off, and reading the claim
    off its configuration would answer no about a bench that can settle itself.
    """
    owner, _ = foreign_incident(tmp_path, auto_recover="reset_halt")
    second = blocked_workspace(tmp_path, auto_recover="off")
    try:
        refusal = refusal_of(second)
        status = second.status()
    finally:
        second.close()

    assert refusal["auto_recoverable"] is True
    assert refusal["project_resource"] == owner.project_key
    assert all(entry["auto_recoverable"] is True for entry in standing_incidents(status))


def test_a_foreign_incident_that_needs_a_signature_is_not_called_recoverable(tmp_path: Path) -> None:
    """The other direction, and the same source for the answer.

    The owning bench has recovery switched off, so nothing in that workspace
    settles this incident by itself and the signature is owed at the bench. This
    project's own policy is the wide one, and reading the claim off it would
    promise a recovery that is not on offer anywhere.
    """
    owner, _ = foreign_incident(tmp_path, auto_recover="off")
    second = blocked_workspace(tmp_path, auto_recover="reset_halt")
    try:
        refusal = refusal_of(second)
        status = second.status()
    finally:
        second.close()

    assert refusal["auto_recoverable"] is False
    assert refusal["project_resource"] == owner.project_key
    assert all(entry["auto_recoverable"] is False for entry in standing_incidents(status))


def test_a_foreign_record_from_an_older_version_names_the_workspace_and_makes_no_claim(tmp_path: Path) -> None:
    """A stale claim would be worse than no claim.

    A record written by a version whose recoverable set is not this one's cannot
    answer for the policy the incident was raised under. The honest answer is to
    name the workspace and leave the claim out, so `auto_recoverable` is absent
    rather than false: absent says nobody knows, false says a person has to walk
    to the bench.
    """
    owner, _ = foreign_incident(tmp_path)
    second = blocked_workspace(tmp_path)
    legacy = {
        "version": 1,
        "state": "quarantined",
        "owner_marker": "0" * 64,
        "owner_pid": 4321,
        "owner_started_at": "2026-09-01T09:00:00.000Z",
        "frontend": "owning-workspace",
        "workspace": str(tmp_path / "owning-workspace"),
        "config_path": str(tmp_path / "owning-workspace" / ".agentic-hil" / "config.yaml"),
        "config_sha256": "0" * 64,
        "project_resource": owner.project_key,
        "resources": [DEBUGGER_DISCOVERY_RESOURCE, debugger_resource(second.config)],
        "updated_at": "2026-09-01T09:00:00.000Z",
        "reason": FOREIGN_REASONS[0],
    }
    for resource in legacy["resources"]:
        second._record_path(str(resource)).write_text(json.dumps(legacy) + "\n", encoding="utf-8")
    try:
        refusal = refusal_of(second)
        status = second.status()
    finally:
        second.close()

    assert refusal["project_resource"] == owner.project_key
    assert refusal["cleanup_reasons"] == [FOREIGN_REASONS[0]]
    assert "auto_recoverable" not in refusal
    entries = standing_incidents(status)
    assert entries and all(entry["project_resource"] == owner.project_key for entry in entries)
    assert all("auto_recoverable" not in entry for entry in entries)
    carries_no_path(refusal, str(tmp_path))
    carries_no_path(entries, str(tmp_path))


def test_a_workspace_whose_own_incident_stands_is_unchanged(tmp_path: Path) -> None:
    """The neighbour that must not move, one.

    A single workspace holding its own standing incident still reports `blocked`,
    still reports `incident_stands`, still names its reasons and still gets the
    recovery command with this incident's id in it. Nothing about a neighbour's
    record may reach an answer about this project's own, so the new section is
    empty here: this incident is not foreign, and it is already reported in full.
    """
    owner = HardwareCoordinator(config_for(tmp_path / "single-workspace"), "single-workspace")
    lease = owner.acquire(*debugger_effect_resources(owner.config))
    lease.quarantine("debug_report_audit_broken", audit_broken=True)
    try:
        status = owner.status()
    finally:
        owner.close()

    assert status["blocked"] is True
    assert status["incident_stands"] is True
    assert status["cleanup_reasons"] == ["debug_report_audit_broken"]
    assert "recover" in status["next_step"]
    assert str(status["quarantine_id"]) in status["next_step"]
    assert standing_incidents(status) == []


def test_a_clean_bench_stays_silent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The neighbour that must not move, two.

    With no incident anywhere on the machine, `lease-status` keeps the sentence
    it has, `doctor` keeps its verdict, and the new section is empty in both. A
    check that speaks on a clean bench is a check nobody reads on a broken one.
    """
    config = config_for(tmp_path / "clean-workspace")
    coordinator = HardwareCoordinator(config, "clean-workspace")
    try:
        status = coordinator.status()
    finally:
        coordinator.close()
    monkeypatch.setattr("agentic_hil.cli.load_cli_authoritative_config", lambda path: config)

    report = doctor()

    assert status["summary"] == FREE_BENCH_SENTENCE
    assert standing_incidents(status) == []
    assert report["ok"] is True, report
    assert report["unhealthy"] == []
    assert standing_incidents(report) == []
