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

The four surfaces under test, and the neighbours that must not move:

* the refusal itself, which has the foreign record open and drops everything in
  it but the quarantine id,
* `hardware_lease_status`, which answers machine-wide for live holds through
  `device_holds` and per project for everything else, then states in words that
  nothing on this bench is standing,
* `doctor`, which reports a ready bench three seconds before the first call is
  refused,
* `recover` in the blocked workspace, which routes through that same status,
* and, unchanged: a single workspace whose own incident stands, a released
  foreign record, a foreign incident on a resource this project does not
  declare, and a clean bench, where all of this stays silent.

Nothing here asks for a second workspace to be able to clear a neighbour's
incident, and nothing releases anything: the refusal stays a refusal and the
signature stays where it was raised. What changes is only what the operator is
told. Nothing may leak either: the project identifier is a digest, and no
absolute path, environment-derived path or workspace may reach any of these
answers.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
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
    com_resource,
    debugger_effect_resources,
    debugger_resource,
)
from agentic_hil.knowledge import catalogue_entry, remediation_fields
from agentic_hil.tools import AgenticHILToolService

# The reasons the reported incident carried, both of them in the reset-halt
# recoverable set, which is what makes it the case #534 is about.
FOREIGN_REASONS = ("debug_target_state_unconfirmed", "debug_session_cleanup_unconfirmed")
# A probe id of this file's own, so a failure here names this file's fixture and
# not a probe another test happened to spell the same way.
SHARED_PROBE_ID = "CROSSPROJECTPROBE"
# The free-bench claim that is false while a neighbour's incident stands.
FREE_BENCH_SENTENCE = "Nothing on this bench is held and no incident is standing."
# What `recover` answers today in the workspace whose every hardware call is
# refused. Matched as its opening clause, because the answer continues past it.
NOTHING_STANDING_SENTENCE = "Nothing on this bench is standing."
# A declared serial port, so the section can be pinned on a resource that is not
# the debugger: it is defined over the devices `device_holds` already walks, and
# `config_devices(...).lock_keys` carries COM ports and CAN buses too.
COM_PORTS_YAML = 'com_ports:\n  dut:\n    device: "COM_CROSSPROJECT"\n'
COM_PORT_ID = "dut"


def config_for(workspace: Path, **kwargs: Any):
    kwargs.setdefault("probe_id", SHARED_PROBE_ID)
    return load_config(str(write_config(workspace, **kwargs)))


def debugger_effects(coordinator: HardwareCoordinator) -> tuple[str, ...]:
    return debugger_effect_resources(coordinator.config)


def foreign_incident(
    tmp_path: Path,
    *,
    auto_recover: str | None = None,
    reasons: tuple[str, ...] = FOREIGN_REASONS,
    audit_broken: bool = False,
    resources: Callable[[HardwareCoordinator], Sequence[str]] = debugger_effects,
    keep_open: bool = False,
    **config_kwargs: Any,
) -> tuple[HardwareCoordinator, str]:
    """One workspace leaves an unresolved incident on the shared resources.

    Exactly the shape the report describes: the discovery pseudo-resource and
    the shared probe under one incident, the owner gone. The coordinator is
    returned closed unless a test needs it alive, for its `project_key` and its
    configuration.
    """
    owner = HardwareCoordinator(config_for(tmp_path / "owning-workspace", auto_recover=auto_recover, **config_kwargs), "owning-workspace")
    lease = owner.acquire(*resources(owner))
    for reason in reasons:
        lease.quarantine(reason, audit_broken=audit_broken)
    quarantine_id = str(lease.quarantine_id)
    if not keep_open:
        owner.close()
    return owner, quarantine_id


def blocked_workspace(tmp_path: Path, **kwargs: Any) -> HardwareCoordinator:
    """The second workspace on the same machine and the same physical probe."""
    return HardwareCoordinator(config_for(tmp_path / "blocked-workspace", **kwargs), "blocked-workspace")


def refusal_of(coordinator: HardwareCoordinator, *resources: str) -> dict[str, Any]:
    with pytest.raises(CoordinationError) as excinfo:
        coordinator.acquire(*(resources or debugger_effect_resources(coordinator.config)))
    return dict(excinfo.value.result)


def standing_incidents(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """The section, with its shape asserted rather than filtered.

    A filter that dropped everything that is not a dict would let a section full
    of strings or nulls satisfy every `== []` in this file.
    """
    section = payload.get("standing_incidents")
    assert isinstance(section, list), payload
    assert all(isinstance(entry, dict) for entry in section), section
    return list(section)


def carries_no_path(payload: object, *forbidden: str) -> None:
    """No absolute path, no environment-derived path, no workspace.

    Applied to the answers this issue adds, because the record they are built
    from carries the neighbour's workspace and configuration path and the honest
    change carries the digest and nothing more.

    Every spelling of the path is compared, because `json.dumps` escapes a
    backslash: a leaked Windows path reaches the serialised text doubled, and a
    guard that compared only the raw string enforced nothing at all on the
    platform this suite mostly runs on.
    """
    text = json.dumps(payload, default=str).lower()
    for path in forbidden:
        for spelling in (path, Path(path).as_posix(), path.replace("\\", "\\\\")):
            assert spelling.lower() not in text, text

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

    The reasons are compared as a set. The record keeps them in the order they
    occurred and `_quarantined_result` sorts its own; which of the two spellings
    the refusal adopts is not a contract anybody reads, and pinning it would
    fail an implementation that copied the sibling it is being made to match.
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
    assert sorted(refusal["cleanup_reasons"]) == sorted(FOREIGN_REASONS)
    assert refusal["project_resource"] == owner.project_key
    next_step = str(refusal["next_step"])
    # What the next step has to say, and not merely which command names appear
    # in it: which workspace owns the incident, that this one cannot clear it,
    # and what the answer here will be if the operator tries.
    assert owner.project_key in next_step
    assert "another project" in next_step.lower()
    assert "nothing_to_recover" in next_step
    carries_no_path(refusal, str(tmp_path))


def test_the_refusal_reaches_a_caller_with_guidance_and_remediation(tmp_path: Path) -> None:
    """The guidance is missing for two independent reasons, and both are here.

    `attach_quarantine_guidance` is applied to every tool result at the service's
    own call boundary and returns the result unchanged when no reason is named,
    so a refusal that drops `cleanup_reasons` suppresses guidance that exists in
    the build. The catalogue is the second cause and is independent of it: with
    no `resource_quarantined` entry, `remediation_fields` answers an empty
    mapping wherever a refusal asks for it.

    Driven through the service rather than through the helper, because the
    helper being correct and the refusal actually arriving at a caller with
    guidance on it are two different claims. The second reason of the two names,
    as the unknown an operator has to resolve, whether a server process still
    holds the probe, which is precisely the fact the second workspace needed.
    """
    foreign_incident(tmp_path)
    service = AgenticHILToolService(config_for(tmp_path / "blocked-workspace"), frontend="mcp")
    try:
        result = service.call("debugger_probes_list")
    finally:
        service.close()

    assert result["ok"] is False
    assert result["error_type"] == "resource_quarantined"
    assert sorted(result["cleanup_reasons"]) == sorted(FOREIGN_REASONS)
    guidance = result["quarantine_guidance"]
    assert sorted(entry["reason"] for entry in guidance) == sorted(FOREIGN_REASONS)
    cleanup = next(entry for entry in guidance if entry["reason"] == "debug_session_cleanup_unconfirmed")
    assert "still holds the probe" in cleanup["unknown"]
    assert cleanup["physical_check"]
    # The catalogue half, on the result a caller actually receives.
    assert result["remediation"]
    carries_no_path(result["quarantine_guidance"], str(tmp_path))


def test_resource_quarantined_has_a_catalogue_entry_that_reaches_a_result() -> None:
    """The gap that is independent of the other three.

    `ERROR_CATALOGUE` has no `resource_quarantined` key, so `remediation_fields`
    returns an empty mapping including where the product already asks for it, and
    the browsable error reference has no page for the refusal that stops a bench
    outright. The retryable neighbour `device_busy` has an entry.

    Two entries and not one, in the scoped form the lookup already supports: an
    incident this project can clear and one it cannot need opposite advice, and a
    single entry would send half its readers to `agentic-hil recover` in a
    workspace where it answers `nothing_to_recover` by design.
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

    # The scoped half: a foreign incident, where the advice is the opposite one.
    foreign = catalogue_entry("resource_quarantined:foreign_project")
    assert foreign is not None
    assert foreign["scope"] == "foreign_project"
    assert foreign["remediation"] != entry["remediation"]
    assert remediation_fields("resource_quarantined", "foreign_project")["remediation"] == foreign["remediation"]


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
        assert sorted(entry["cleanup_reasons"]) == sorted(FOREIGN_REASONS)
        assert entry["project_resource"] == owner.project_key
        assert entry["state"] in {"cleanup_required", "quarantined"}
    # The false claim, and the part that had to change.
    assert FREE_BENCH_SENTENCE not in status["summary"]
    assert "another project" in status["summary"].lower()
    carries_no_path(status, str(tmp_path))


def test_the_standing_incident_section_covers_a_declared_com_port(tmp_path: Path) -> None:
    """The section is over the devices, not over the debugger.

    `config_devices(config).lock_keys` is what `device_holds` walks and it
    carries COM ports and CAN buses beside probes, so a neighbour's incident on
    a declared serial port refuses this project's next `com_session_start`
    exactly as one on the probe refuses its next flash. A section that answered
    only for the debugger would leave that operator on the same dead end this
    issue is about.
    """
    owner, quarantine_id = foreign_incident(
        tmp_path,
        reasons=("com_cleanup_unconfirmed",),
        resources=lambda coordinator: [com_resource(coordinator.config, COM_PORT_ID)],
        com_ports_yaml=COM_PORTS_YAML,
    )
    second = blocked_workspace(tmp_path, com_ports_yaml=COM_PORTS_YAML)
    try:
        status = second.status()
        refusal = refusal_of(second, com_resource(second.config, COM_PORT_ID))
    finally:
        second.close()

    entries = standing_incidents(status)
    assert {entry["resource"] for entry in entries} == {com_resource(second.config, COM_PORT_ID)}
    assert entries[0]["quarantine_id"] == quarantine_id
    assert entries[0]["project_resource"] == owner.project_key
    assert refusal["error_type"] == "resource_quarantined"
    assert refusal["cleanup_reasons"] == ["com_cleanup_unconfirmed"]
    assert FREE_BENCH_SENTENCE not in status["summary"]
    carries_no_path(status, str(tmp_path))


def test_the_section_empties_when_the_owning_workspace_resolves_the_incident(tmp_path: Path) -> None:
    """The other direction, and the one that says the report is a report.

    The resource becoming usable again is already covered; the sentence is not.
    A section that never emptied would be a permanent warning on a bench that is
    free, which is the same defect as the permanent all-clear on a bench that is
    not.
    """
    owner, quarantine_id = foreign_incident(tmp_path)
    second = blocked_workspace(tmp_path)
    try:
        assert standing_incidents(second.status())
    finally:
        second.close()

    recovery = HardwareCoordinator(owner.config, "owning-workspace-recovery")
    try:
        assert recovery.recover(safe_state_confirmed=True, quarantine_id=quarantine_id)["ok"] is True
    finally:
        recovery.close()

    after = blocked_workspace(tmp_path)
    try:
        status = after.status()
        lease = after.acquire(*debugger_effect_resources(after.config))
        assert lease.release() is True
    finally:
        after.close()

    assert standing_incidents(status) == []
    assert status["summary"] == FREE_BENCH_SENTENCE


def test_a_released_or_undeclared_foreign_record_stays_out_of_the_section(tmp_path: Path) -> None:
    """Two records that must not reach it, and neither would refuse anything.

    A foreign record in `released` is a workspace that finished, and a foreign
    incident on a resource this configuration does not declare cannot refuse a
    call this configuration can make. A section that listed either would tell an
    operator that their bench is blocked when the next call goes through.
    """
    finished = HardwareCoordinator(config_for(tmp_path / "finished-workspace"), "finished-workspace")
    assert finished.acquire(*debugger_effect_resources(finished.config)).release() is True
    finished.close()
    elsewhere = HardwareCoordinator(config_for(tmp_path / "elsewhere-workspace"), "elsewhere-workspace")
    elsewhere.acquire("physical:not-in-this-configuration").quarantine("debug_target_state_unconfirmed")
    elsewhere.close()

    second = blocked_workspace(tmp_path)
    try:
        status = second.status()
        lease = second.acquire(*debugger_effect_resources(second.config))
        assert lease.release() is True
    finally:
        second.close()

    assert standing_incidents(status) == []
    assert status["summary"] == FREE_BENCH_SENTENCE


def test_doctor_reports_the_bench_that_refuses_its_next_call(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """agentic-hil/agentic-hil#531: doctor never asks.

    It runs a config load, a state root test, a bench binding test and a per
    debugger toolchain check, builds no coordinator and reads no coordination
    record. On a bench in exactly this state it reported the state root ok, the
    binding ok and every device named, and the first flash three seconds later
    was refused. Doctor is where somebody looks before a run rather than after a
    refusal, so it carries the same section and the same distinction.

    The verdict moves with it. `doctor.ok` is read as "will this bench work",
    and answering yes over a bench whose next call is refused is the whole
    complaint, so the finding is named in `unhealthy` and `ok` is false. It is a
    finding `setup` keeps its configuration over: a neighbour's incident says
    nothing about the file that was just written.

    The leak guard is applied to the section and not to the whole report:
    `doctor` prints this project's own `config_path` by design, and that is the
    asking workspace's, not the neighbour's.
    """
    owner, quarantine_id = foreign_incident(tmp_path, auto_recover="reset_halt")
    second_config = config_for(tmp_path / "blocked-workspace")
    monkeypatch.setattr("agentic_hil.cli.load_cli_authoritative_config", lambda path: second_config)

    report = doctor()

    entries = standing_incidents(report)
    assert {entry["resource"] for entry in entries} == {DEBUGGER_DISCOVERY_RESOURCE, debugger_resource(second_config)}
    for entry in entries:
        assert entry["quarantine_id"] == quarantine_id
        assert sorted(entry["cleanup_reasons"]) == sorted(FOREIGN_REASONS)
        assert entry["project_resource"] == owner.project_key
        # #534 in the place somebody looks before a run: this incident settles
        # itself the moment the owning workspace makes any hardware call.
        assert entry["auto_recoverable"] is True
    assert report["ok"] is False
    assert "standing_incident" in report["unhealthy"]
    # A caller that keeps only the headline has to be told too, because the
    # verdict this command exists to give is whether the next call will work.
    assert "another project" in report["summary"].lower()
    carries_no_path(entries, str(tmp_path))


def test_doctor_reads_the_records_without_disturbing_a_live_owner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """doctor builds no coordinator, and this is how it reaches the records.

    The records are written atomically and are the only thing this section
    reads, so it takes no coordination lock and no device lock: a `doctor` that
    took either would block behind, or worse contend with, the very session
    whose incident it is reporting. Pinned against a live owner holding both its
    project lock and its device locks, which is exactly the case a lock-taking
    implementation would fail on.
    """
    owner, quarantine_id = foreign_incident(tmp_path, keep_open=True)
    second_config = config_for(tmp_path / "blocked-workspace")
    monkeypatch.setattr("agentic_hil.cli.load_cli_authoritative_config", lambda path: second_config)
    try:
        assert owner.project_lock is not None and owner.project_lock.locked

        report = doctor()

        entries = standing_incidents(report)
        assert {entry["quarantine_id"] for entry in entries} == {quarantine_id}
        # Undisturbed: every lock the live owner held before the call it still
        # holds after it, and its own answer is unchanged.
        assert owner.project_lock is not None and owner.project_lock.locked
        assert all(lock.locked for lease in owner.leases.values() for lock in lease.locks)
        assert owner.status()["blocked"] is True
    finally:
        owner.close()


def test_recover_in_the_blocked_workspace_stops_saying_nothing_is_standing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """agentic-hil/agentic-hil#531: the third dead end.

    `recover` routes through the same status read, finds nothing standing for
    this project and answers `nothing_to_recover`, and it is the command the
    documented operator path sends the caller to with the id the refusal named.
    It still clears nothing: a second workspace may not settle a neighbour's
    incident, and that is the rule this test keeps. What it must stop doing is
    telling the operator that the bench is free.

    The rule is checked against the bench and not against the answer's own
    fields: the record is read back and the next acquire is tried again, because
    `ok`, `was_quarantined` and an empty `resources` are what this answer returns
    by construction and would stay true over a release that happened on the side.
    """
    owner, quarantine_id = foreign_incident(tmp_path)
    second_config = config_for(tmp_path / "blocked-workspace")
    monkeypatch.setattr("agentic_hil.cli.load_cli_authoritative_config", lambda path: second_config)

    code = entrypoint(["recover", "--confirm-safe-state", "--quarantine-id", quarantine_id, "--json"])
    answer = json.loads(capsys.readouterr().out)

    assert code == 0
    # Nothing was released and nothing was signed for: the incident is not this
    # workspace's to settle.
    assert answer["ok"] is True
    assert answer["was_quarantined"] is False
    assert answer["resources"] == []
    # And the answer no longer contradicts the refusal the caller just met.
    assert not str(answer["summary"]).startswith(NOTHING_STANDING_SENTENCE)
    entries = standing_incidents(answer)
    assert {entry["quarantine_id"] for entry in entries} == {quarantine_id}
    assert {entry["project_resource"] for entry in entries} == {owner.project_key}
    carries_no_path(answer, str(tmp_path))

    # The bench itself, after the call: the foreign record is where it was and
    # the next hardware call from here is refused exactly as before.
    second = blocked_workspace(tmp_path)
    try:
        record = second._read_record(DEBUGGER_DISCOVERY_RESOURCE)
        assert record is not None
        assert record["state"] in {"cleanup_required", "quarantined"}
        assert record["quarantine_id"] == quarantine_id
        assert record["project_resource"] == owner.project_key
        assert refusal_of(second)["error_type"] == "resource_quarantined"
    finally:
        second.close()


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
    # The next step says which of the two cases this is, because that is the
    # difference the caller could not see and the whole of the fix.
    assert "any hardware call" in str(refusal["next_step"]).lower()
    entries = standing_incidents(status)
    assert {entry["resource"] for entry in entries} == {DEBUGGER_DISCOVERY_RESOURCE, debugger_resource(second.config)}
    assert all(entry["auto_recoverable"] is True for entry in entries)


def test_a_foreign_incident_that_needs_a_signature_is_not_called_recoverable(tmp_path: Path) -> None:
    """The other direction, and the same source for the answer.

    The owning bench has recovery switched off, so nothing in that workspace
    settles this incident by itself and the signature is owed at the bench. This
    project's own policy is the wide one, and reading the claim off it would
    promise a recovery that is not on offer anywhere.
    """
    owner, quarantine_id = foreign_incident(tmp_path, auto_recover="off")
    second = blocked_workspace(tmp_path, auto_recover="reset_halt")
    try:
        refusal = refusal_of(second)
        status = second.status()
    finally:
        second.close()

    assert refusal["auto_recoverable"] is False
    assert refusal["project_resource"] == owner.project_key
    # The signature, and where it is signed: the id the operator passes is this
    # incident's, and the workspace is the one that raised it.
    assert f"recover --confirm-safe-state --quarantine-id {quarantine_id}" in str(refusal["next_step"])
    entries = standing_incidents(status)
    assert {entry["resource"] for entry in entries} == {DEBUGGER_DISCOVERY_RESOURCE, debugger_resource(second.config)}
    assert all(entry["auto_recoverable"] is False for entry in entries)


def test_a_foreign_record_that_does_not_name_its_recoverable_set_makes_no_claim(tmp_path: Path) -> None:
    """A stale claim would be worse than no claim.

    The claim is read off what the owning workspace recorded its own policy and
    grants as permitting, at the moment it wrote the record. A record written by
    a version that recorded no such set cannot answer for the policy the incident
    was raised under: this build's set is this build's, and a reader that
    substituted it would be answering out of the wrong configuration in the one
    place #534 says must not. The honest answer is to name the workspace and
    leave the claim out, so `auto_recoverable` is absent rather than false:
    absent says nobody knows, false says a person has to walk to the bench.

    Seeded by stripping that one field from a genuine record, so the only thing
    that differs from the case above is the signal being tested.
    """
    owner, quarantine_id = foreign_incident(tmp_path)
    second = blocked_workspace(tmp_path)
    for resource in (DEBUGGER_DISCOVERY_RESOURCE, debugger_resource(second.config)):
        path = second._record_path(resource)
        record = json.loads(path.read_text(encoding="utf-8"))
        record.pop("recoverable_reasons", None)
        path.write_text(json.dumps(record) + "\n", encoding="utf-8")
    try:
        refusal = refusal_of(second)
        status = second.status()
    finally:
        second.close()

    assert refusal["project_resource"] == owner.project_key
    assert refusal["quarantine_id"] == quarantine_id
    assert sorted(refusal["cleanup_reasons"]) == sorted(FOREIGN_REASONS)
    assert "auto_recoverable" not in refusal
    assert "unknown" in str(refusal["next_step"]).lower()
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
