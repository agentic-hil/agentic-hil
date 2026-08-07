"""Clearing a quarantine over MCP, and the class boundary that stays.

hardci-hq#128. `hardware_lease_status` has distinguished `auto_recoverable` from
the rest since 0.8.0, but `recover` lived only at the shell — so on a host with no
shell an agent could see an incident, explain it, and do nothing about it, even
when the incident had provably never touched a board.

The tooth is the class boundary, and it is not a permission. `--confirm-safe-state`
attests that a physical board is still and holds the firmware somebody expects,
which is a claim about the world; no grant on any bench hands that over, and the
tool has no argument with which an agent could make it. These tests hold both
halves: the reasons that name no contact clear, and the ones that need a person
refuse with the exact command line for that person.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import write_config

from agentic_hil.config import load_config
from agentic_hil.configwrite import ACTOR_AGENT
from agentic_hil.contracts import MCP_TOOLS, TOOL_ANNOTATIONS
from agentic_hil.coordination import (
    ATTESTATION_NO_CONTACT_CLASS,
    LEASE_RELEASE_RETRY_REASON,
    HardwareCoordinator,
)
from agentic_hil.knowledge import RECOVERY_PHYSICAL_CHECK_ERROR, catalogue_entry, recovery_operator_command
from agentic_hil.tools import AgenticHILToolService

TOOL = "hardware_recover"
# Machine-wide device locks contend across sibling clones (hardci-hq#116).
RESOURCE = "physical:recover-tool-probe"


def with_project_permissions(path: Path, **grants: bool) -> Path:
    """Put a project `permissions:` block on a config the test helper wrote.

    `write_config` speaks the per-device grant vocabulary and writes no
    project-scoped block at all, which is its own case worth keeping: a file that
    names nothing grants nothing, `allow_recover` included.
    """
    block = "permissions:\n" + "".join(f"  {flag}: {str(bool(value)).lower()}\n" for flag, value in grants.items())
    path.write_text(block + path.read_text(encoding="utf-8"), encoding="utf-8")
    return path


def config_for(workspace: Path, *, allow_recover: bool = True, **write_config_kwargs):
    written = write_config(workspace, **write_config_kwargs)
    with_project_permissions(
        written,
        allow_config_write=True,
        allow_config_description_write=True,
        allow_config_permissions_write=True,
        allow_recover=allow_recover,
    )
    return load_config(str(written))


def quarantine(config, reason: str) -> str:
    """An incident on disk, raised the way a live owner raises one and then left
    behind by an owner that is gone."""
    owner = HardwareCoordinator(config, "quarantine-setup")
    lease = owner.acquire(RESOURCE)
    lease.quarantine(reason)
    incident = owner.quarantine_id
    owner.close()
    assert isinstance(incident, str)
    return incident


def ledger(config) -> list[dict]:
    path = Path(HardwareCoordinator(config, "ledger-reader").root) / "recovery.jsonl"
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# The reasons the tool may clear.


def test_a_no_contact_reason_is_cleared_over_mcp(tmp_path: Path) -> None:
    """`lease_release_unconfirmed` is the clearest case in the catalogue: the
    hardware action completed and the *record* of the release is what failed, so
    the device saw nothing afterwards and there is nothing to inspect."""
    config = config_for(tmp_path)
    incident = quarantine(config, LEASE_RELEASE_RETRY_REASON)
    service = AgenticHILToolService(config)
    try:
        result = service.call(TOOL, {})

        assert result["ok"] is True, result
        assert result["was_quarantined"] is True
        assert result["cleared_reasons"] == [LEASE_RELEASE_RETRY_REASON]
        assert result["recovered_quarantine_id"] == incident
        assert result["resources"] == [RESOURCE]
        assert service.coordinator.status()["blocked"] is False
    finally:
        service.close()


def test_the_audit_line_names_the_agent_and_not_a_person(tmp_path: Path) -> None:
    """Two ways into one transition, and the ledger has to say which was taken —
    otherwise a recovery nobody signed reads exactly like one somebody did."""
    config = config_for(tmp_path)
    quarantine(config, LEASE_RELEASE_RETRY_REASON)
    service = AgenticHILToolService(config)
    try:
        service.call(TOOL, {})
    finally:
        service.close()

    lines = ledger(config)
    assert len(lines) == 1
    assert lines[0]["actor"] == ACTOR_AGENT
    assert lines[0]["via"] == "mcp:hardware_recover"
    assert lines[0]["attestation"] == ATTESTATION_NO_CONTACT_CLASS


def test_the_result_says_which_evidence_cleared_it(tmp_path: Path) -> None:
    config = config_for(tmp_path)
    quarantine(config, LEASE_RELEASE_RETRY_REASON)
    service = AgenticHILToolService(config)
    try:
        result = service.call(TOOL, {})

        assert result["actor"] == ACTOR_AGENT
        assert result["attestation"] == ATTESTATION_NO_CONTACT_CLASS
        assert "operator-confirmed" not in result["summary"]
    finally:
        service.close()


def test_a_bench_with_nothing_quarantined_answers_instead_of_failing(tmp_path: Path) -> None:
    """Idempotent, and the annotation says so, so a second call has to be free."""
    config = config_for(tmp_path)
    quarantine(config, LEASE_RELEASE_RETRY_REASON)
    service = AgenticHILToolService(config)
    try:
        first = service.call(TOOL, {})
        second = service.call(TOOL, {})

        assert first["ok"] is True
        assert second["ok"] is True
        assert second["was_quarantined"] is False
        assert len(ledger(config)) == 1
    finally:
        service.close()


def test_hardware_calls_work_again_afterwards(tmp_path: Path) -> None:
    """The point of the tool: a bench that was blocked is in service again
    without anybody walking to it."""
    config = config_for(tmp_path)
    quarantine(config, LEASE_RELEASE_RETRY_REASON)
    service = AgenticHILToolService(config)
    try:
        assert service.call(TOOL, {})["ok"] is True

        assert service.call("probe_target", {}).get("error_type") != "resource_quarantined"
    finally:
        service.close()


def test_a_hardware_call_that_adopted_the_incident_first_widens_its_reasons(tmp_path: Path) -> None:
    """Pinned rather than worked around, because it decides how the tool is used.

    Taking a lease over a record a dead owner left behind stamps
    `owner_process_exited_without_release` onto the incident, on top of whatever
    reason it already carried — `acquire`'s adoption path predates the
    hardci-hq#127 evidence check and still fires on any unreleased record. So an
    incident that was clearable from here stops being clearable once a hardware
    call has been tried against it, and this tool belongs before that retry
    rather than after it.
    """
    config = config_for(tmp_path)
    quarantine(config, LEASE_RELEASE_RETRY_REASON)
    service = AgenticHILToolService(config)
    try:
        assert service.call("probe_target", {})["error_type"] == "resource_quarantined"

        result = service.call(TOOL, {})

        assert result["error_type"] == RECOVERY_PHYSICAL_CHECK_ERROR
        assert result["physical_check_reasons"] == ["owner_process_exited_without_release"]
        assert LEASE_RELEASE_RETRY_REASON in result["cleanup_reasons"]
    finally:
        service.close()


# ---------------------------------------------------------------------------
# The reasons it refuses, and how.


@pytest.mark.parametrize(
    "reason",
    [
        "owner_process_exited_without_release",
        "safe_state_unconfirmed",
        "process_reap_unconfirmed",
        "audit_broken",
    ],
)
def test_a_physical_reason_refuses_with_the_operator_command(tmp_path: Path, reason: str) -> None:
    config = config_for(tmp_path)
    incident = quarantine(config, reason)
    service = AgenticHILToolService(config)
    try:
        result = service.call(TOOL, {})

        assert result["ok"] is False
        assert result["error_type"] == RECOVERY_PHYSICAL_CHECK_ERROR
        assert result["physical_check_reasons"] == [reason]
        assert result["operator_command"] == f"agentic-hil recover --confirm-safe-state --quarantine-id {incident}"
        assert result["quarantined"] is True
        # The four facts the signer needs travel with the command they are for.
        assert any(entry["reason"] == reason for entry in result["quarantine_guidance"])
        assert service.coordinator.status()["blocked"] is True
        assert ledger(config) == []
    finally:
        service.close()


def test_a_mixed_incident_refuses_on_the_reason_that_needs_a_person(tmp_path: Path) -> None:
    """One clearable reason does not make an incident clearable; the boundary is
    the whole incident, the way the operator's own recovery is."""
    config = config_for(tmp_path)
    owner = HardwareCoordinator(config, "quarantine-setup")
    lease = owner.acquire(RESOURCE)
    lease.quarantine(LEASE_RELEASE_RETRY_REASON)
    lease.quarantine("safe_state_unconfirmed")
    owner.close()
    service = AgenticHILToolService(config)
    try:
        result = service.call(TOOL, {})

        assert result["error_type"] == RECOVERY_PHYSICAL_CHECK_ERROR
        assert result["physical_check_reasons"] == ["safe_state_unconfirmed"]
        assert LEASE_RELEASE_RETRY_REASON in result["cleanup_reasons"]
    finally:
        service.close()


def test_a_machine_settleable_reason_is_still_refused_here(tmp_path: Path) -> None:
    """The narrowing that matters, and the one a reader is most likely to argue
    with. `debugger_result_unconfirmed` is inside this bench's `recoverable_
    reasons` under the default `reset_halt` policy — but what settles it is a
    verified reset into halt, actually performed, and this call performs nothing.
    So it refuses, and says which situation it is: the automatic path will run
    that predicate on the next hardware call."""
    config = config_for(tmp_path)
    quarantine(config, "debugger_result_unconfirmed")
    service = AgenticHILToolService(config)
    try:
        result = service.call(TOOL, {})

        assert result["error_type"] == RECOVERY_PHYSICAL_CHECK_ERROR
        assert result["physical_check_reasons"] == ["debugger_result_unconfirmed"]
        assert result["auto_recoverable"] is True
        assert result["auto_recover_policy"] == "reset_halt"
        assert any("auto_recoverable" in step for step in result["remediation"])
    finally:
        service.close()


def test_the_clearable_set_is_the_no_contact_class_and_nothing_else(tmp_path: Path) -> None:
    config = config_for(tmp_path)
    coordinator = HardwareCoordinator(config, "class-boundary")

    assert coordinator.agent_recoverable_reasons() == {"released_dead_owner_no_contact", LEASE_RELEASE_RETRY_REASON}
    # Strictly inside what the machine may settle, never beyond it.
    assert coordinator.agent_recoverable_reasons() - coordinator.recoverable_reasons() == {"released_dead_owner_no_contact"}


# ---------------------------------------------------------------------------
# The permission.


def test_allow_recover_false_refuses_on_the_permission(tmp_path: Path) -> None:
    config = config_for(tmp_path, allow_recover=False)
    incident = quarantine(config, LEASE_RELEASE_RETRY_REASON)
    service = AgenticHILToolService(config)
    try:
        result = service.call(TOOL, {})

        assert result["ok"] is False
        assert result["error_type"] == "permission_denied"
        assert result["permission"] == "permissions.allow_recover"
        assert result["operator_command"] == recovery_operator_command(incident)
        assert result["remediation"]
        assert service.coordinator.status()["blocked"] is True
        assert ledger(config) == []
    finally:
        service.close()


def test_a_generated_configuration_grants_it() -> None:
    """Open by default like every grant since hardci-hq#96."""
    import yaml

    from agentic_hil.config import DEFAULT_CONFIG_TEMPLATE, GENERATED_PROJECT_PERMISSIONS

    assert "allow_recover" in GENERATED_PROJECT_PERMISSIONS
    assert yaml.safe_load(DEFAULT_CONFIG_TEMPLATE)["permissions"]["allow_recover"] is True


def test_a_configuration_that_names_nothing_grants_nothing() -> None:
    """The dataclass default stays false, so a file written before this key
    existed does not silently acquire it."""
    from agentic_hil.types import ProjectPermissions

    assert ProjectPermissions().allow_recover is False


def test_the_one_way_street_already_covers_the_new_key() -> None:
    """The ratchet is structural — every leaf inside a `permissions` mapping is a
    permission — so the new grant is on it without configwrite learning its name.
    That is what makes this a key and not a special case."""
    from agentic_hil.configwrite import permission_surface

    surface = permission_surface({"permissions": {"allow_recover": True, "allow_config_write": True}})

    assert "permissions.allow_recover" in surface


# ---------------------------------------------------------------------------
# The schema, and the argument that is deliberately not in it.


def schema_for(name: str) -> dict:
    return next(tool["inputSchema"] for tool in MCP_TOOLS if tool["name"] == name)


def test_the_schema_offers_no_way_to_confirm_a_safe_state() -> None:
    """The absence is the contract, so it is asserted rather than assumed.

    A `confirm_safe_state` argument would be a flag the caller sets for itself,
    and a confirmation one gives oneself is not a confirmation. The tool takes no
    arguments at all, which also closes every neighbouring spelling at once.
    """
    schema = schema_for(TOOL)

    assert schema.get("properties", {}) == {}
    assert schema.get("additionalProperties") is False
    assert not schema.get("required")
    serialized = json.dumps(schema)
    for spelling in ("confirm", "safe_state", "force", "override", "quarantine_id"):
        assert spelling not in serialized, spelling


def test_an_invented_confirmation_argument_is_refused_on_the_wire(tmp_path: Path) -> None:
    """Not only absent from the schema: rejected by the validator, so a caller
    cannot smuggle one past a host that forwards unknown keys."""
    config = config_for(tmp_path)
    quarantine(config, "safe_state_unconfirmed")
    service = AgenticHILToolService(config)
    try:
        result = service.call(TOOL, {"confirm_safe_state": True})

        assert result["ok"] is False
        assert result["error_type"] == "invalid_argument"
        assert service.coordinator.status()["blocked"] is True
    finally:
        service.close()


def test_the_annotations_say_what_it_does() -> None:
    assert TOOL_ANNOTATIONS[TOOL] == {
        "title": "Clear a bench quarantine",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }


@pytest.mark.parametrize("key", [RECOVERY_PHYSICAL_CHECK_ERROR, "permission_denied:allow_recover"])
def test_each_refusal_carries_its_own_fix(key: str) -> None:
    entry = catalogue_entry(key)

    assert entry is not None
    assert entry["remediation"]
    assert entry["do_not"]
    # Both point at the same route out, and neither invents a second one.
    assert any("operator_command" in step for step in entry["remediation"])
    assert any("state_root" in step for step in entry["do_not"])
