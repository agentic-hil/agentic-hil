"""Clearing a quarantine over MCP, and the class boundary that stays.

`hardware_lease_status` has distinguished `auto_recoverable` from
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
    lease_config_sha256,
)
from agentic_hil.knowledge import RECOVERY_PHYSICAL_CHECK_ERROR, catalogue_entry, recovery_operator_command
from agentic_hil.tools import AgenticHILToolService

TOOL = "hardware_recover"
# Machine-wide device locks contend across sibling clones.
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
    """A *standing* incident on disk, raised the way a live owner raises one and
    then left behind by an owner that is gone.

    `audit_broken=True` beside the reason, and that is the whole of what #216
    changed here. Since the quarantine narrowed, an incident stands only while
    the evidence chain for this bench is damaged: everything else ends with the
    call that raised it, so a bench built without this flag has nothing for
    `hardware_recover` to be asked about and answers `nothing_to_recover`. Every
    test below is about what the tool does with an incident that *is* standing,
    so the setup builds one; the reason keeps naming the physical state that
    decides which route out applies, because the two facts are independent and
    a real bench that loses its ledger mid-call carries both.

    What the narrowed tool does with a bench that is not standing has its own
    tests at the end of this file."""
    owner = HardwareCoordinator(config, "quarantine-setup")
    lease = owner.acquire(RESOURCE)
    lease.quarantine(reason, audit_broken=True)
    incident = owner.quarantine_id
    owner.close()
    assert isinstance(incident, str)
    return incident


def open_incident(config, reason: str) -> str:
    """An incident on disk that does not stand: the ordinary aftermath of a
    failed call, left behind by an owner that never came back through the seam
    that ends one."""
    owner = HardwareCoordinator(config, "open-incident-setup")
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
    no-contact evidence check and still fires on any unreleased record. So an
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
    lease.quarantine(LEASE_RELEASE_RETRY_REASON, audit_broken=True)
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
    # Strictly inside what the machine may settle, never beyond it. Asked by
    # membership: what the machine may settle is defined by an exclusion and
    # enumerates nothing, so a set difference would answer about the wrong thing.
    assert all(reason in coordinator.recoverable_reasons() for reason in coordinator.agent_recoverable_reasons())


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
    """Open by default like every grant since 0.8.0."""
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
    and a confirmation one gives oneself is not a confirmation.

    Narrowed twice, and the thing being protected has not moved. The original pin
    was "no properties at all"; then `operator_statement` arrived, which cannot
    be satisfied by a value the caller invents because what it has to contain is
    what a person said. Now `accept_config_change` is here and it is a boolean,
    so the rule has to be stated by what the boolean is about rather than by its
    type: no argument of any spelling may assert something about a physical
    board. That one says two configuration digests, both of them printed in the
    refusal that asks for it, have been looked at, which is a claim about two
    strings this process wrote.
    """
    schema = schema_for(TOOL)

    assert set(schema.get("properties", {})) == {"operator_statement", "accept_config_change"}
    assert schema["properties"]["operator_statement"]["type"] == "string"
    # Empty is not a statement: a caller with nothing to relay must be refused
    # rather than allowed to satisfy the argument with "".
    assert schema["properties"]["operator_statement"]["minLength"] == 1
    assert schema["properties"]["accept_config_change"] == {
        "type": "boolean",
        "default": False,
        "description": schema["properties"]["accept_config_change"]["description"],
    }
    assert schema["properties"]["accept_config_change"]["default"] is False
    assert schema.get("additionalProperties") is False
    # Still nothing the tool demands: the no-contact reasons clear with no
    # arguments, exactly as before.
    assert not schema.get("required")
    assert [key for key, value in schema.get("properties", {}).items() if value.get("type") == "boolean"] == [
        "accept_config_change"
    ]
    serialized = json.dumps(schema)
    for spelling in ("confirm", "safe_state", "force", "quarantine_id", "board", "powered"):
        assert spelling not in serialized, spelling


def test_the_recovery_guidance_matches_the_tool_contract_boundary() -> None:
    """The generated template, the public schema, and the embedded MCP workflow
    must all describe the boundary the tool contract enforces, so a reader of
    policy cannot be told the physical route does not exist while the tool has
    implemented it.

    The boundary, in one line: a reason that names no hardware contact clears
    with no argument, a reason that needs somebody at the board clears only by
    relaying an operator_statement, and the audit-broken and nobody-to-ask cases
    keep the operator's own command line. This is the consistency assertion the
    finding asked for — before it, the schema and generated comments said
    physical reasons are refused over MCP with no such argument, which the tool
    stopped being true of when operator_statement arrived.

    The authoritative agent instructions — `AGENTS.md` and both shipped skill
    copies — are held to the same boundary here. Round 1 left them still telling
    an agent that every remaining quarantine is the operator's to clear from a
    shell, which sends a host with no operator shell away from the very route
    `operator_statement` opened; the round-2 finding asked that these surfaces be
    pinned to the boundary too, so a reader of the instructions cannot be steered
    off the MCP route the tool implements."""
    from agentic_hil.config import DEFAULT_CONFIG_TEMPLATE
    from agentic_hil.knowledge import config_schema_document
    from agentic_hil.mcp import AGENTIC_HIL_WORKFLOW_PROMPT

    contract = next(tool["description"] for tool in MCP_TOOLS if tool["name"] == TOOL)
    schema_description = config_schema_document()["properties"]["permissions"]["properties"]["allow_recover"]["description"]
    repository_root = Path(__file__).resolve().parents[1]
    instruction_surfaces = {
        "agents_md": repository_root / "AGENTS.md",
        "skill_src": repository_root / "src" / "agentic_hil" / "skills" / "agentic-hil" / "SKILL.md",
        "skill_plugin": repository_root / "plugins" / "agentic-hil" / "skills" / "agentic-hil" / "SKILL.md",
    }
    surfaces = {
        "tool_contract": contract,
        "config_template": DEFAULT_CONFIG_TEMPLATE,
        "config_schema": schema_description,
        "mcp_workflow_prompt": AGENTIC_HIL_WORKFLOW_PROMPT,
        **{name: path.read_text(encoding="utf-8") for name, path in instruction_surfaces.items()},
    }
    for name, text in surfaces.items():
        lowered = text.lower()
        # A physical reason is cleared by relaying a statement, not refused outright.
        assert "operator_statement" in lowered, name
        # A no-contact reason still clears with none.
        assert "no argument" in lowered or "no arguments" in lowered or "no hardware contact" in lowered, name

    # The agent-facing instructions must name the MCP recovery tool, not send
    # every physical incident to the shell as round 1 still did.
    for name in instruction_surfaces:
        assert "hardware_recover" in surfaces[name], name

    # None of the generated surfaces still claims the physical route does not
    # exist over MCP — the exact stale wordings this finding removed.
    for name in ("config_template", "config_schema", "mcp_workflow_prompt"):
        lowered = surfaces[name].lower()
        assert "no argument on the mcp tool" not in lowered, name
        assert "refuse over mcp whatever" not in lowered, name
        assert "refused whatever this says" not in lowered, name

    # Pin the instruction-surface repair itself, not merely the presence of terms
    # that predated it. Both parent-commit skill copies already carried
    # `operator_statement`, `hardware_recover`, and a no-argument clause inside
    # their `hardware_recover` section, so every assertion above stays green even
    # after reverting the relay-guidance paragraph to the round-1 wording that
    # routed every chat-confirmed physical recovery back to the operator's shell.
    # Reject that obsolete directive on each surface directly. The skills wrapped
    # the CLI line, so whitespace is collapsed before matching.
    def collapse_whitespace(text: str) -> str:
        return " ".join(text.split())

    obsolete_shell_routing = {
        "skill_src": "ask them to run `agentic-hil recover --confirm-safe-state --quarantine-id <id>` after that check",
        "skill_plugin": "ask them to run `agentic-hil recover --confirm-safe-state --quarantine-id <id>` after that check",
        "agents_md": "otherwise ask the operator to inspect `agentic-hil lease-status` and resolve it with `agentic-hil recover",
    }
    for name, obsolete in obsolete_shell_routing.items():
        assert obsolete not in collapse_whitespace(surfaces[name]).lower(), name

    # And positively: the skills' relay path now hands what is left to
    # `hardware_recover` carrying the operator's statement, not to a shell. This
    # phrase is unique to the repaired paragraph — the unchanged `hardware_recover`
    # section says "carry their sentence", never "carrying their statement" — so a
    # revert of the repair drops it and this assertion fails.
    for name in ("skill_src", "skill_plugin"):
        assert "carrying their statement" in collapse_whitespace(surfaces[name]).lower(), name


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


# ---------------------------------------------------------------------------
# The operator's statement, relayed.
#
# The class boundary did not move: a claim about a physical bench still comes
# from a person. What moved is who may carry the sentence. Telling an agent in a
# chat window to send its operator hunting for a shell — on a host that may not
# have one — never protected the board; it only meant the claim was made out of
# band and the ledger never saw it.


def test_a_relayed_operator_statement_clears_a_physical_reason(tmp_path: Path) -> None:
    config = config_for(tmp_path)
    incident = quarantine(config, "safe_state_unconfirmed")
    service = AgenticHILToolService(config)
    try:
        result = service.call(TOOL, {"operator_statement": "Board is powered down and on the bench, I looked."})

        assert result["ok"] is True, result
        assert result["was_quarantined"] is True
        assert result["recovered_quarantine_id"] == incident
        assert service.coordinator.status()["blocked"] is False
    finally:
        service.close()


def test_the_statement_is_recorded_verbatim_and_not_as_a_signature(tmp_path: Path) -> None:
    """The ledger has to keep a person quoted by a program distinct from a person
    at their own command line. Both are the operator's word; only one of them is
    the operator's signature, and an audit that spelled them the same could never
    tell afterwards which had happened."""
    config = config_for(tmp_path)
    quarantine(config, "safe_state_unconfirmed")
    said = "Powered off, USB unplugged, nothing else is attached to it."
    service = AgenticHILToolService(config)
    try:
        service.call(TOOL, {"operator_statement": said})
    finally:
        service.close()

    lines = ledger(config)
    assert len(lines) == 1
    assert lines[0]["operator_statement"] == said
    assert lines[0]["attestation"] == "operator_statement_via_agent"
    assert lines[0]["attestation"] != ATTESTATION_NO_CONTACT_CLASS
    assert lines[0]["actor"] == ACTOR_AGENT


def test_a_no_contact_reason_needs_no_statement(tmp_path: Path) -> None:
    """The statement is for the reasons that need a person, and asking for one
    where nothing was ever touched would teach an agent to produce sentences for
    a form rather than because somebody spoke."""
    config = config_for(tmp_path)
    quarantine(config, LEASE_RELEASE_RETRY_REASON)
    service = AgenticHILToolService(config)
    try:
        result = service.call(TOOL, {})

        assert result["ok"] is True, result
    finally:
        service.close()

    assert "operator_statement" not in ledger(config)[0]


def test_an_empty_statement_is_not_a_statement(tmp_path: Path) -> None:
    """`minLength: 1` on the wire, so a caller with nothing to relay cannot
    satisfy the argument by passing the absence of one."""
    config = config_for(tmp_path)
    quarantine(config, "safe_state_unconfirmed")
    service = AgenticHILToolService(config)
    try:
        result = service.call(TOOL, {"operator_statement": ""})

        assert result["ok"] is False
        assert result["error_type"] == "invalid_argument"
        assert service.coordinator.status()["blocked"] is True
    finally:
        service.close()


def test_the_refusal_explains_the_argument_and_the_lie_it_would_be(tmp_path: Path) -> None:
    """A refusal that only says "a person must do this" sends an agent looking
    for a shell. What is actually needed is the person's sentence, and the agent
    is already talking to them — so the refusal says to ask, says what happens to
    the answer, and says outright what inventing one would be. The operator's own
    command line stays in it as the route for when there is nobody to ask."""
    config = config_for(tmp_path)
    incident = quarantine(config, "safe_state_unconfirmed")
    service = AgenticHILToolService(config)
    try:
        result = service.call(TOOL, {})
    finally:
        service.close()

    assert result["ok"] is False
    assert result["error_type"] == RECOVERY_PHYSICAL_CHECK_ERROR
    assert result["missing_argument"] == "operator_statement"
    assert "ask the operator" in result["next_step"].lower()
    assert "ledger" in result["next_step"].lower()
    # The catalogue carries the warning, and it is the first thing in it.
    assert "never invent an `operator_statement`" in result["do_not"][0].lower()
    assert "false" in result["do_not"][0].lower()
    assert result["operator_command"] == recovery_operator_command(incident)


def test_a_statement_does_not_reach_the_audit_broken_family(tmp_path: Path) -> None:
    """The one family no sentence settles. The reason names a ledger that could
    not be written, and clearing it on a statement would put the attestation into
    the very file whose failure raised the incident."""
    config = config_for(tmp_path)
    owner = HardwareCoordinator(config, "audit-broken-setup")
    lease = owner.acquire(RESOURCE)
    lease.quarantine("audit_broken", audit_broken=True)
    owner.close()
    service = AgenticHILToolService(config)
    try:
        result = service.call(TOOL, {"operator_statement": "I looked at it, it is fine."})

        assert result["ok"] is False, result
        assert service.coordinator.status()["blocked"] is True
    finally:
        service.close()


# ---------------------------------------------------------------------------
# The override the refusal has always named.


def edit_config(workspace: Path):
    """Change the authoritative configuration's bytes without changing its meaning.

    A comment line is enough: `recover` compares the digest of what was parsed,
    so any edit at all makes the incident older than the configuration in force,
    which is the situation `config_changed` exists to report.
    """
    path = workspace / ".agentic-hil" / "config.yaml"
    path.write_text(path.read_text(encoding="utf-8") + "\n# operator edited this after the incident\n", encoding="utf-8")
    return load_config(str(path))


def config_changed_incident(tmp_path: Path):
    """An incident recorded under one configuration, met by a server holding another."""
    config = config_for(tmp_path)
    incident = quarantine(config, LEASE_RELEASE_RETRY_REASON)
    recorded = lease_config_sha256(config)
    edited = edit_config(tmp_path)
    assert lease_config_sha256(edited) != recorded
    return edited, incident, recorded


def test_a_config_edit_after_the_incident_refuses_and_names_both_spellings(tmp_path: Path) -> None:
    """The refusal is the only thing that tells an agent what to do next, so it
    has to name the argument this tool takes as well as the operator's flag. It
    named only its own spelling of it for one release and the schema refused
    that spelling, which is how the one way forward became a dead end."""
    config, incident, recorded = config_changed_incident(tmp_path)
    service = AgenticHILToolService(config)
    try:
        result = service.call(TOOL, {})

        assert result["ok"] is False, result
        assert result["error_type"] == "config_changed"
        assert result["override"] == "accept_config_change (CLI: --accept-config-change)"
        assert result["recorded_config_sha256"] == recorded
        assert result["current_config_sha256"] == lease_config_sha256(config)
        assert result["quarantine_id"] == incident
        assert service.coordinator.status()["blocked"] is True
    finally:
        service.close()

    assert ledger(config) == []


def test_the_override_clears_it_and_the_ledger_carries_both_digests(tmp_path: Path) -> None:
    """The issue, end to end: the same call again with the argument the refusal
    named, and the incident is gone. The ledger records that the override was
    used and both digests, exactly as the operator's own command line does, so a
    reader can tell afterwards which configuration was assessed and which was in
    force."""
    config, incident, recorded = config_changed_incident(tmp_path)
    service = AgenticHILToolService(config)
    try:
        result = service.call(TOOL, {"accept_config_change": True})

        assert result["ok"] is True, result
        assert result["was_quarantined"] is True
        assert result["config_change_accepted"] is True
        assert result["recovered_quarantine_id"] == incident
        assert service.coordinator.status()["blocked"] is False
    finally:
        service.close()

    lines = ledger(config)
    assert len(lines) == 1, lines
    assert lines[0]["config_change_accepted"] is True
    assert lines[0]["recorded_config_sha256"] == recorded
    assert lines[0]["current_config_sha256"] == lease_config_sha256(config)
    assert lines[0]["actor"] == ACTOR_AGENT
    assert lines[0]["via"] == "mcp:hardware_recover"


def test_the_override_false_is_the_same_as_not_passing_it(tmp_path: Path) -> None:
    """A caller that spells the default out loud gets the default, not a silent
    acceptance: the override exists only when somebody set it."""
    config, _, _ = config_changed_incident(tmp_path)
    service = AgenticHILToolService(config)
    try:
        result = service.call(TOOL, {"accept_config_change": False})

        assert result["error_type"] == "config_changed", result
        assert service.coordinator.status()["blocked"] is True
    finally:
        service.close()

    assert ledger(config) == []


def test_the_override_is_no_way_past_the_physical_check(tmp_path: Path) -> None:
    """The class boundary does not move. `accept_config_change` says two digests
    were compared; it says nothing about a board, so an incident that needs
    somebody to look at one is refused with it exactly as without it."""
    config = config_for(tmp_path)
    quarantine(config, "safe_state_unconfirmed")
    edited = edit_config(tmp_path)
    service = AgenticHILToolService(edited)
    try:
        result = service.call(TOOL, {"accept_config_change": True})

        assert result["ok"] is False, result
        assert result["error_type"] == RECOVERY_PHYSICAL_CHECK_ERROR
        assert result["missing_argument"] == "operator_statement"
        assert service.coordinator.status()["blocked"] is True
    finally:
        service.close()

    assert ledger(edited) == []


def test_a_relayed_statement_carries_the_override_too(tmp_path: Path) -> None:
    """Both ways in take it, because both run the same transition: an operator
    who has looked at the board has usually looked at the configuration delta in
    the same breath, and sending them to a shell for the second half would be the
    dead end this removes."""
    config = config_for(tmp_path)
    quarantine(config, "safe_state_unconfirmed")
    edited = edit_config(tmp_path)
    service = AgenticHILToolService(edited)
    try:
        refused = service.call(TOOL, {"operator_statement": "Board is powered down on my desk.", "accept_config_change": False})
        assert refused["error_type"] == "config_changed", refused

        result = service.call(TOOL, {"operator_statement": "Board is powered down on my desk.", "accept_config_change": True})

        assert result["ok"] is True, result
        assert result["config_change_accepted"] is True
        assert service.coordinator.status()["blocked"] is False
    finally:
        service.close()

    lines = ledger(edited)
    assert len(lines) == 1, lines
    assert lines[0]["operator_statement"] == "Board is powered down on my desk."
    assert lines[0]["config_change_accepted"] is True


# ---------------------------------------------------------------------------
# The bench with nothing standing.
#
# Since #216 this is the ordinary state after a failed call: the incident ended
# when the call did, and the tool that exists to clear one has nothing to do.
# It says so instead of failing, because nothing went wrong.


def test_an_incident_that_does_not_stand_answers_nothing_to_recover(tmp_path: Path) -> None:
    """The reason names a target, and a target is what the next reset and probe
    speak for. There is no signature owed for it, so the tool does not ask for
    one and does not invent an error over a bench that is fine."""
    config = config_for(tmp_path)
    open_incident(config, "debugger_result_unconfirmed")
    service = AgenticHILToolService(config)
    try:
        result = service.call(TOOL, {})

        assert result["ok"] is True, result
        assert result["nothing_to_recover"] is True
        assert result["was_quarantined"] is False
        assert "error_type" not in result
        assert service.coordinator.status()["blocked"] is False
    finally:
        service.close()


def test_the_bench_that_answered_nothing_to_recover_is_actually_free(tmp_path: Path) -> None:
    """Not a softer refusal: the markers are released and the ledger says how."""
    config = config_for(tmp_path)
    open_incident(config, "safe_state_unconfirmed")
    service = AgenticHILToolService(config)
    try:
        assert service.call(TOOL, {})["nothing_to_recover"] is True
        assert service.call("probe_target", {}).get("error_type") != "resource_quarantined"
    finally:
        service.close()

    lines = ledger(config)
    assert [line["recovery"] for line in lines] == ["incident_stood_down"]
    assert lines[0]["attestation"] == "no_standing_state"
    assert lines[0]["reasons"] == ["safe_state_unconfirmed"]


def test_a_bench_with_no_incident_at_all_answers_the_same_way(tmp_path: Path) -> None:
    """One answer for the three ways of having nothing to clear: never had an
    incident, had one a recovery action settled, had one that stood down."""
    config = config_for(tmp_path)
    service = AgenticHILToolService(config)
    try:
        result = service.call(TOOL, {})

        assert result["ok"] is True
        assert result["nothing_to_recover"] is True
        assert result["was_quarantined"] is False
    finally:
        service.close()

    assert ledger(config) == []


def test_the_permission_gates_the_route_that_clears_something(tmp_path: Path) -> None:
    """`allow_recover` decides whether an agent may clear a quarantine. A bench
    with nothing standing has none to clear, so being told so needs no grant:
    refusing the answer would send an agent hunting for a shell to run a command
    that would do nothing."""
    config = config_for(tmp_path, allow_recover=False)
    open_incident(config, "debugger_result_unconfirmed")
    service = AgenticHILToolService(config)
    try:
        result = service.call(TOOL, {})

        assert result["ok"] is True, result
        assert result["nothing_to_recover"] is True
        assert "error_type" not in result
    finally:
        service.close()


def test_the_operator_command_line_answers_the_same_way(tmp_path: Path) -> None:
    """The CLI narrows with the tool, or an operator following a stale runbook
    would be told a bench that is fine cannot be recovered."""
    from agentic_hil.coordination import nothing_standing_result

    config = config_for(tmp_path)
    open_incident(config, "debugger_result_unconfirmed")
    coordinator = HardwareCoordinator(config, "operator-cli")
    status = coordinator.status()

    assert status["incident_stands"] is False
    answer = nothing_standing_result(status)
    assert answer["ok"] is True
    assert answer["nothing_to_recover"] is True
    coordinator.close()


def test_the_operator_command_line_still_clears_a_standing_quarantine(tmp_path: Path) -> None:
    """And the one route that still has work to do is untouched."""
    config = config_for(tmp_path)
    incident = quarantine(config, "audit_broken")
    coordinator = HardwareCoordinator(config, "operator-cli")
    status = coordinator.status()

    assert status["incident_stands"] is True
    recovered = coordinator.recover(safe_state_confirmed=True, quarantine_id=incident)

    assert recovered["ok"] is True
    assert recovered["recovered_quarantine_id"] == incident
    assert coordinator.status()["blocked"] is False
