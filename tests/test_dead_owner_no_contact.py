"""A dead owner that provably never touched the bench is released, not quarantined.

`coordination.status()` adopted any exited owner whose project
record still said `active` as a quarantine, without ever asking what the dead
session had done. The reclassification of 0.8.0 (a quarantine needs the
*possibility* of an effect) had been applied to call failures and never to the
owner's death, so a `pkill` of a runner whose last call ended before any hardware
contact cost an operator a walk to the bench and a `recover --confirm-safe-state`.

The direction these tests pin is asymmetric on purpose. One case releases, and it
needs the evidence to be complete; every other case quarantines, including every
case where the evidence is merely missing. An unknown rounded down to safe is the
failure quarantine exists to prevent, so the "still quarantines" tests below are
the load-bearing half.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import write_config

from agentic_hil.config import load_config
from agentic_hil.configwrite import ACTOR_AGENT, ACTOR_HUMAN
from agentic_hil.coordination import (
    ATTESTATION_NO_CONTACT_CLASS,
    DEAD_OWNER_NO_CONTACT_REASON,
    NO_CONTACT_RECOVERABLE_REASONS,
    RECOVERY_ACTOR_AGENT,
    RECOVERY_ACTOR_HUMAN,
    RECOVERY_ACTOR_SERVER,
    HardwareCoordinator,
)
from agentic_hil.report import (
    CALL_SCOPED_LEASE_TOOLS,
    CONFIG_IN_FORCE_KEY,
    CONTACT_MARKER_KEY,
    CONTACT_MARKER_SOURCE_KEY,
    config_in_force,
    write_report,
)

# Machine-wide device locks contend across sibling clones, so
# every resource here is named for this file alone.
RESOURCE = "physical:dead-owner-no-contact-probe"
OTHER_RESOURCE = "physical:dead-owner-no-contact-uart"


def config_for(workspace: Path):
    return load_config(str(write_config(workspace)))


def dead_owner_record(coordinator: HardwareCoordinator, *, resources: list[str] | None = None, lease_id: str = "dead-lease-1", **lease_overrides) -> dict:
    """The record a live owner leaves behind while a call is in flight.

    Built through `_base_record`/`_persist_project`'s own shape rather than by
    hand, so a change to what an active project record contains breaks these
    tests instead of silently making them describe a record nobody writes.
    """
    held = resources or [RESOURCE]
    lease = {
        "lease_id": lease_id,
        "resources": list(held),
        "lease_state": "active",
        "safe_state_confirmed": False,
        "processes_reaped": False,
        "audit_ok": True,
        "cleanup_required": False,
        "quarantined": False,
        "cleanup_reasons": [],
        "quarantine_id": None,
    }
    lease.update(lease_overrides)
    record = coordinator._base_record("active", list(held))
    record["leases"] = [lease]
    return record


def no_contact_report(coordinator: HardwareCoordinator, *, resources: list[str] | None = None, lease_id: str = "dead-lease-1", tool: str = "can_session_start", **overrides) -> dict:
    report = {
        "ok": False,
        "tool": tool,
        "error_type": "can_interface_not_found",
        "summary": "SocketCAN interface does not exist on this host.",
        "side_effect_committed": False,
        "side_effect_status": "not_started",
        "retry_safe": True,
        "audit_ok": True,
        "lease_id": lease_id,
        "resources": list(resources or [RESOURCE]),
        "lease_state": "active",
    }
    report.update(overrides)
    return report


DEFAULT_REPORT = object()


def leave_dead_owner(workspace: Path, *, record_overrides: dict | None = None, report: object = DEFAULT_REPORT, resources: list[str] | None = None):
    """A project record from an owner that is gone, plus its last report.

    ``report=None`` leaves the report state empty, which is its own case; a test
    that wants a different report writes one itself afterwards.

    Nothing holds a lock: the owner is dead, which is exactly the state a second
    coordinator's `status()` meets when it takes the probe lock and finds an
    `active` record nobody owns.
    """
    config = config_for(workspace)
    setup = HardwareCoordinator(config, "dead-owner-setup")
    record = dead_owner_record(setup, resources=resources)
    if record_overrides:
        record.update(record_overrides)
    written = no_contact_report(setup, resources=resources) if report is DEFAULT_REPORT else report
    if written is not None:
        write_report(config, written)
    for resource in record["resources"]:
        marker = setup._base_record("active", list(record["resources"]))
        marker["lease_id"] = record["leases"][0]["lease_id"]
        setup._write_record(resource, marker)
    setup._write_record(setup.project_key, record)
    return config


def recovery_ledger(config) -> list[dict]:
    path = Path(HardwareCoordinator(config, "ledger-reader").root) / "recovery.jsonl"
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# The case that changes.


def test_an_owner_that_died_between_calls_without_contact_is_released(tmp_path: Path) -> None:
    """The bench case. Fails on the state before this change: `status()` wrote
    `owner_process_exited_without_release` over a session that never reached a
    board, and an operator had to sign it off."""
    config = leave_dead_owner(tmp_path)
    observer = HardwareCoordinator(config, "second-coordinator")

    status = observer.status()

    assert status["blocked"] is False
    assert status["cleanup_reasons"] == []
    assert "quarantine_guidance" not in status
    assert status["record"]["state"] == "released"
    assert status["record"]["released_reason"] == DEAD_OWNER_NO_CONTACT_REASON
    released = status["released_dead_owner"]
    assert released["reason"] == DEAD_OWNER_NO_CONTACT_REASON
    assert released["resources"] == [RESOURCE]
    assert released["evidence"]["side_effect_status"] == "not_started"
    assert released["evidence"]["last_report_tool"] == "can_session_start"


def test_the_release_leaves_every_resource_marker_free(tmp_path: Path) -> None:
    config = leave_dead_owner(tmp_path)
    observer = HardwareCoordinator(config, "second-coordinator")

    observer.status()

    assert observer._read_record(RESOURCE)["state"] == "released"
    assert observer._read_record(observer.project_key)["state"] == "released"
    # And the bench is workable again without anything else happening first.
    lease = observer.acquire(RESOURCE)
    assert lease.release() is True
    observer.close()


def test_the_release_is_recorded_in_the_recovery_ledger(tmp_path: Path) -> None:
    """Evidence first, and never silently: a bench that stops being quarantined
    has to leave a line saying who released it and on what."""
    config = leave_dead_owner(tmp_path)

    HardwareCoordinator(config, "second-coordinator").status()

    lines = recovery_ledger(config)
    assert len(lines) == 1
    assert lines[0]["event"] == "recovery"
    assert lines[0]["reason"] == DEAD_OWNER_NO_CONTACT_REASON
    assert lines[0]["actor"] == RECOVERY_ACTOR_SERVER
    assert lines[0]["attestation"] == ATTESTATION_NO_CONTACT_CLASS
    assert lines[0]["via"] == "coordination:second-coordinator"
    assert lines[0]["resources"] == [RESOURCE]


def test_a_second_status_call_finds_nothing_left_to_do(tmp_path: Path) -> None:
    config = leave_dead_owner(tmp_path)
    observer = HardwareCoordinator(config, "second-coordinator")

    first = observer.status()
    second = observer.status()

    assert first["blocked"] is False
    assert second["blocked"] is False
    assert "released_dead_owner" not in second
    assert len(recovery_ledger(config)) == 1


# ---------------------------------------------------------------------------
# Everything that is not released on evidence of no contact.
#
# The fallback moved with #216 and the question these cases ask did not. A dead
# owner nothing can vouch for used to be quarantined; now the incident is
# adopted, named, and stood down, because `owner_process_exited_without_release`
# names a target the next reset and probe speak for and a gate is only owed
# where the missing proof cannot come back. What must never happen is the
# *other* answer: a release recorded as `dead_owner_no_contact` asserts positive
# evidence that these reports do not carry, and asserting it would hand a bench
# back on a claim nobody made. That is what each case below holds.


def assert_no_contact_release_was_refused(status, config, why: str = "") -> None:
    assert "released_dead_owner" not in status, why
    assert status["cleanup_reasons"] == ["owner_process_exited_without_release"], why
    # Adopted, not released: nothing here vouched for the bench, and no ledger
    # line claims anything did. A status read runs no recovery action, so the
    # incident it inherits is left for the call that can, and the seam at the
    # end of that call is where it ends if the recovery could not settle it.
    assert status["incident_stands"] is False, why
    assert recovery_ledger(config) == [], why


def test_an_owner_that_died_with_a_committed_effect_is_not_released_as_no_contact(tmp_path: Path) -> None:
    config = leave_dead_owner(tmp_path, report=None)
    setup = HardwareCoordinator(config, "dead-owner-setup")
    write_report(config, no_contact_report(setup, side_effect_committed=True, side_effect_status="committed", ok=True, error_type=None))
    observer = HardwareCoordinator(config, "second-coordinator")

    status = observer.status()

    assert_no_contact_release_was_refused(status, config)
    # And the marker says so: quarantined under this incident, never released
    # with the `released_reason` the evidence-backed path writes.
    assert observer._read_record(RESOURCE)["state"] == "quarantined"
    assert "released_reason" not in observer._read_record(RESOURCE)


def test_an_owner_that_left_no_readable_record_at_all_is_not_released_as_no_contact(tmp_path: Path) -> None:
    """The indeterminable case: there is nothing to read, and nothing is not
    evidence of no contact."""
    config = leave_dead_owner(tmp_path, report=None)
    observer = HardwareCoordinator(config, "second-coordinator")

    status = observer.status()

    assert_no_contact_release_was_refused(status, config)


@pytest.mark.parametrize(
    ("overrides", "why"),
    [
        ({"side_effect_status": "unknown", "side_effect_committed": None}, "an unknown effect is not an absent one"),
        ({"side_effect_status": "partial", "side_effect_committed": True}, "a partial effect reached the board"),
        ({"lease_id": "another-lease"}, "a report about a lease that is gone says nothing about this one"),
        ({"lease_state": "released"}, "a report re-committed after a clean release describes a call that ended"),
        ({"audit_ok": False}, "a broken audit trail cannot be the evidence"),
        ({"resources": [OTHER_RESOURCE]}, "a report about other devices does not answer for these"),
        ({"tool": "can_read"}, "a read on an open session is silent about the open that preceded it"),
        ({"tool": "can_send"}, "a write on an open session likewise"),
    ],
)
def test_a_report_that_does_not_answer_the_question_is_not_released_as_no_contact(tmp_path: Path, overrides: dict, why: str) -> None:
    config = leave_dead_owner(tmp_path, report=None)
    setup = HardwareCoordinator(config, "dead-owner-setup")
    write_report(config, no_contact_report(setup, **overrides))
    observer = HardwareCoordinator(config, "second-coordinator")

    status = observer.status()

    assert_no_contact_release_was_refused(status, config, why)


def test_a_session_that_opened_its_port_is_not_released_as_no_contact(tmp_path: Path) -> None:
    """The gap between "this call did nothing" and "this session did nothing".

    `side_effect_committed: false` is a truthful claim about the call that wrote
    the report, and a session start writes several. One that opened the port and
    then failed on the reader thread, or on a receive buffer that would not
    clear, closes the port again and reports its own failure as never begun,
    while the open that preceded it drove DTR and RTS, and on a board that wires
    DTR to reset, reset it.

    So the pair alone would release a bench whose board had just been restarted.
    The contact marker is what tells the two apart, and this is the case where it
    does work the existing conditions cannot: everything else about this report
    is exactly the evidence the release asks for.
    """
    config = leave_dead_owner(tmp_path, report=None)
    setup = HardwareCoordinator(config, "dead-owner-setup")
    write_report(
        config,
        no_contact_report(
            setup,
            tool="com_session_start",
            error_type="com_reader_start_failed",
            summary="COM port reader could not be started; the port was closed.",
            cleanup_confirmed=True,
            **{CONTACT_MARKER_KEY: "2026-08-07T09:00:00Z", CONTACT_MARKER_SOURCE_KEY: "serial_open"},
        ),
    )
    observer = HardwareCoordinator(config, "second-coordinator")

    status = observer.status()

    assert_no_contact_release_was_refused(status, config)


def test_the_release_names_the_contact_question_it_asked(tmp_path: Path) -> None:
    """A release is signed evidence, so it has to say which questions were put to
    the report, including the one whose answer was an absence."""
    config = leave_dead_owner(tmp_path)
    observer = HardwareCoordinator(config, "second-coordinator")

    evidence = observer.status()["released_dead_owner"]["evidence"]

    assert CONTACT_MARKER_KEY in evidence
    assert evidence[CONTACT_MARKER_KEY] is None


def test_a_second_open_lease_is_not_answered_for_by_one_report(tmp_path: Path) -> None:
    """Only the last call left a report; the other lease has no evidence at all,
    and a partial answer is not one."""
    config = config_for(tmp_path)
    setup = HardwareCoordinator(config, "dead-owner-setup")
    record = dead_owner_record(setup, resources=[RESOURCE])
    record["leases"].append({**record["leases"][0], "lease_id": "dead-lease-2", "resources": [OTHER_RESOURCE]})
    record["resources"] = [RESOURCE, OTHER_RESOURCE]
    write_report(config, no_contact_report(setup))
    setup._write_record(setup.project_key, record)
    observer = HardwareCoordinator(config, "second-coordinator")

    status = observer.status()

    assert_no_contact_release_was_refused(status, config)


def test_a_report_written_under_another_configuration_is_not_evidence(tmp_path: Path) -> None:
    """Every report carries `config_in_force` so a record can
    say which document it was decided by. A report from a different one cannot
    answer for this lease, and the comparison is the reason the field exists."""
    config = leave_dead_owner(tmp_path, report=None)
    setup = HardwareCoordinator(config, "dead-owner-setup")
    stamped = no_contact_report(setup)
    stamped[CONFIG_IN_FORCE_KEY] = {**config_in_force(config), "digest": "sha256:" + "0" * 64}
    write_report(config, stamped)
    observer = HardwareCoordinator(config, "second-coordinator")

    status = observer.status()

    assert_no_contact_release_was_refused(status, config)


def test_a_record_already_carrying_an_incident_is_left_alone(tmp_path: Path) -> None:
    """An `active` record that already names a reason or an incident is not the
    state this fix is about, and re-deciding it would overwrite somebody else's
    finding."""
    config = leave_dead_owner(tmp_path, record_overrides={"quarantine_id": "prior-incident"})
    observer = HardwareCoordinator(config, "second-coordinator")

    status = observer.status()

    assert_no_contact_release_was_refused(status, config)


def test_a_marker_that_is_not_this_leases_hold_stops_the_release(tmp_path: Path) -> None:
    """The finding examined the project record; a resource marker in a state it
    never looked at is not covered by it."""
    config = leave_dead_owner(tmp_path)
    setup = HardwareCoordinator(config, "dead-owner-setup")
    marker = setup._read_record(RESOURCE)
    setup._write_record(RESOURCE, {**marker, "lease_id": "some-other-lease"})
    observer = HardwareCoordinator(config, "second-coordinator")

    status = observer.status()

    assert_no_contact_release_was_refused(status, config)


def test_a_live_owner_is_never_decided_about(tmp_path: Path) -> None:
    """The probe lock is the gate. A record read while somebody holds the lock is
    advisory and must trigger no transition in either direction."""
    config = config_for(tmp_path)
    live = HardwareCoordinator(config, "live-owner")
    lease = live.acquire(OTHER_RESOURCE)
    try:
        setup = HardwareCoordinator(config, "dead-owner-setup")
        write_report(config, no_contact_report(setup))
        setup._write_record(setup.project_key, dead_owner_record(setup))
        observer = HardwareCoordinator(config, "second-coordinator")

        status = observer.status()

        assert status["snapshot_atomic"] is False
        assert "released_dead_owner" not in status
        assert recovery_ledger(config) == []
    finally:
        lease.release()
        live.close()


# ---------------------------------------------------------------------------
# The class boundary the sibling issue reads.


def test_the_no_contact_class_is_agent_clearable_and_the_physical_one_is_not(tmp_path: Path) -> None:
    coordinator = HardwareCoordinator(config_for(tmp_path), "class-boundary")

    allowed = coordinator.agent_recoverable_reasons()

    assert DEAD_OWNER_NO_CONTACT_REASON in allowed
    assert allowed == NO_CONTACT_RECOVERABLE_REASONS
    # Inside what machine recovery may settle, never beyond it: that path runs a
    # predicate against the board and an agent's bookkeeping call runs none, so
    # the bookkeeping class has to be a subset of the predicate's. Asked by
    # membership, because the predicate's set answers `in` and enumerates
    # nothing.
    assert all(reason in coordinator.recoverable_reasons() for reason in allowed)
    # The reason the bench case produced, and the one that still needs a person.
    assert "owner_process_exited_without_release" not in allowed
    assert "safe_state_unconfirmed" not in allowed


def test_the_ledger_actors_are_spelled_the_way_provenance_spells_them() -> None:
    """One vocabulary for "who did this", or an audit cannot be read across the
    two places it is recorded."""
    assert RECOVERY_ACTOR_HUMAN == ACTOR_HUMAN
    assert RECOVERY_ACTOR_AGENT == ACTOR_AGENT
    assert RECOVERY_ACTOR_SERVER not in {ACTOR_AGENT, ACTOR_HUMAN}


def test_call_scoped_tools_are_the_ones_that_take_their_own_lease() -> None:
    """A tool joins this set because its lease is acquired inside the call it
    reports. Adding a session tool to it would let a `not_started` read on an
    open bus speak for the open that preceded it."""
    assert "can_read" not in CALL_SCOPED_LEASE_TOOLS
    assert "com_read" not in CALL_SCOPED_LEASE_TOOLS
    assert "can_send" not in CALL_SCOPED_LEASE_TOOLS
    assert "com_write" not in CALL_SCOPED_LEASE_TOOLS
    assert "debug_continue" not in CALL_SCOPED_LEASE_TOOLS
    assert {"probe_target", "flash_firmware", "reset_target", "can_session_start", "com_session_start"} <= CALL_SCOPED_LEASE_TOOLS
