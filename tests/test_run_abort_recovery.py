"""A failed run aborts, and the abort recovers the bench.

The model these tests hold: when something goes wrong during a run, the run
stops and its result is "failed" — that is the verdict and nothing here softens
it — and then a recovery action runs so the next run has a board to start on.
What used to happen instead was a padlock: an incident that refused every
hardware call, including the reset that would have settled it, until a person
came to a shell and signed for it.

So the teeth are in three places. The seam is called from the run teardown
rather than from one caller. The calls that *are* the remedy run during an
incident while the calls that merely use the bench do not. And a recovery that
actually performed the act the incident named as unconfirmed clears it, with a
ledger line, rather than leaving it for somebody.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import write_config

from agentic_hil.config import load_config
from agentic_hil.coordination import (
    ATTESTATION_RECOVERY_ACTION,
    LEASE_RELEASE_RETRY_REASON,
    RECOVERY_ACTION_VIA,
    RECOVERY_ACTOR_SERVER,
    HardwareCoordinator,
)
from agentic_hil.report import CONTACT_MARKER_KEY, CONTACT_MARKER_SOURCE_KEY, write_report
from agentic_hil.test_reactor import TestReactor
from agentic_hil.tools import AgenticHILToolService

# Machine-wide device locks contend across sibling clones, so this name is this
# module's alone.
RESOURCE = "physical:run-abort-recovery-probe"
# The resource the dead owner below held. Its own name so an inherited incident
# can never be confused with one this module's live owner raised.
DEAD_OWNER_RESOURCE = "physical:run-abort-recovery-dead-owner-uart"
# What `status()` and `acquire()` write over a project record left `active` by a
# process that is gone and cannot be proved innocent.
DEAD_OWNER_REASON = "owner_process_exited_without_release"
# An incident a reset settles: the session start never confirmed, so the target
# may be running and only driving it into a defined state answers that.
RESET_REASON = "debug_session_start_unconfirmed"
# Incidents a reset does *not* settle: a serial handle that may still be open and
# its reader thread, a CAN adapter that may still be initialised on the bus. A
# reset driven into the target and a probe re-read speak for neither, so a
# performed recovery action must leave these for the route that verifies the
# peripheral itself.
PERIPHERAL_CLEANUP_REASONS = (
    "com_open_cleanup_unconfirmed",
    "com_cleanup_unconfirmed",
    "can_open_cleanup_unconfirmed",
    "can_adapter_cleanup_unconfirmed",
)


class FakeBackend:
    """A probe that records what was driven and can be told to refuse.

    Deliberately not a mock of the recovery seam — the whole question these
    tests ask is whether a reset actually reached a target, so the double is at
    the hardware boundary and the assertions read its log."""

    def __init__(self, *, reset_ok: bool = True, probe_ok: bool = True, flash_ok: bool = True, target_detected: bool = True) -> None:
        self.calls: list[str] = []
        self.reset_ok = reset_ok
        self.probe_ok = probe_ok
        self.flash_ok = flash_ok
        self.target_detected = target_detected

    def probe_target(self) -> dict:
        self.calls.append("probe_target")
        return {"ok": self.probe_ok, "tool": "probe_target", "target_detected": self.target_detected if self.probe_ok else False}

    def reset_target(self, mode: str = "run") -> dict:
        self.calls.append(f"reset_target:{mode}")
        return {"ok": self.reset_ok, "tool": "reset_target", "mode": mode}

    def flash_firmware(self, *args, **kwargs) -> dict:
        self.calls.append("flash_firmware")
        return {"ok": self.flash_ok, "tool": "flash_firmware", "reset_after_flash": True}

    def close(self) -> None:
        return None


def config_for(workspace: Path, *, auto_recover: str | None = None, allow_reset: bool = True):
    written = write_config(
        workspace,
        permissions={"allow_probe": True, "allow_flash": True, "allow_reset": allow_reset},
        auto_recover=auto_recover,
    )
    written.write_text(
        "permissions:\n  allow_recover: true\n" + written.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    return load_config(str(written))


def quarantine(service: AgenticHILToolService, reason: str, *, audit_broken: bool = False) -> str:
    """The incident a failed run leaves behind, raised on this service's own
    coordinator.

    A live owner's own incident, and the inherited kind is set up by
    `leave_contacted_dead_owner` below: the two shapes reach `retryable_incident`
    through different halves of it, so the tests that care keep them apart."""
    lease = service.coordinator.acquire(RESOURCE)
    service.coordinator.quarantine_lease(lease, reason, audit_broken=audit_broken)
    incident = service.coordinator.quarantine_id
    assert isinstance(incident, str)
    return incident


def leave_contacted_dead_owner(workspace: Path, *, audit_ok: bool = True, **config_kwargs):
    """A project record from a process that died with a session it had opened.

    The endurance bench's shape, built the way the coordinator builds it so a
    change to what an active project record contains breaks these tests instead
    of quietly making them describe a record nobody writes. The last report
    carries a contact marker, which is what stops `status()` releasing the dead
    owner as provably innocent: the port was driven, so the incident is real and
    a recovery action is what answers it.
    """
    config = config_for(workspace, **config_kwargs)
    setup = HardwareCoordinator(config, "dead-owner-setup")
    lease = {
        "lease_id": "dead-owner-lease",
        "resources": [DEAD_OWNER_RESOURCE],
        "lease_state": "active",
        "safe_state_confirmed": False,
        "processes_reaped": False,
        "audit_ok": audit_ok,
        "cleanup_required": False,
        "quarantined": False,
        "cleanup_reasons": [],
        "quarantine_id": None,
    }
    record = setup._base_record("active", [DEAD_OWNER_RESOURCE])
    record["leases"] = [lease]
    write_report(
        config,
        {
            "ok": False,
            "tool": "com_session_start",
            "error_type": "com_reader_start_failed",
            "summary": "COM port reader could not be started; the port was closed.",
            "side_effect_committed": False,
            "side_effect_status": "not_started",
            "retry_safe": True,
            "audit_ok": True,
            "lease_id": "dead-owner-lease",
            "resources": [DEAD_OWNER_RESOURCE],
            "lease_state": "active",
            CONTACT_MARKER_KEY: "2026-08-07T09:00:00Z",
            CONTACT_MARKER_SOURCE_KEY: "serial_open",
        },
    )
    marker = setup._base_record("active", [DEAD_OWNER_RESOURCE])
    marker["lease_id"] = "dead-owner-lease"
    setup._write_record(DEAD_OWNER_RESOURCE, marker)
    setup._write_record(setup.project_key, record)
    return config


def ledger(config) -> list[dict]:
    path = Path(HardwareCoordinator(config, "ledger-reader").root) / "recovery.jsonl"
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def recovery_action_lines(config) -> list[dict]:
    return [line for line in ledger(config) if line.get("via") == RECOVERY_ACTION_VIA]


def failing_flash_plan(tmp_path: Path):
    # Imported here rather than at module scope: pytest tries to collect any
    # module-level name starting with `Test` and warns about these two, which
    # are a plan and a step and not test classes.
    from agentic_hil.test_reactor import TestConfig, TestStep

    firmware = tmp_path / "build" / "app.elf"
    firmware.parent.mkdir(parents=True, exist_ok=True)
    firmware.write_bytes(b"\x7fELF" + b"\x00" * 12)
    return TestConfig(
        path=str(tmp_path / "plan.yaml"),
        name="abort",
        steps=[TestStep("flash", {"image_path": "build/app.elf"}, debugger="dut")],
    )


# ---------------------------------------------------------------------------
# A. The abort calls the recovery action.


def test_a_failed_run_aborts_into_a_recovery_action(tmp_path: Path) -> None:
    """The end-to-end shape: a step fails, the run stops and says so, and a
    reset-into-halt reaches the board without anybody being asked for anything.

    `ok` is asserted false in the same breath as the recovery, because that is
    the pairing the whole design turns on — the bench is usable again and the
    test still failed."""
    config = config_for(tmp_path)
    backend = FakeBackend(flash_ok=False)
    service = AgenticHILToolService(config, backend=backend)
    try:
        result = TestReactor(config, service).run(failing_flash_plan(tmp_path))
    finally:
        service.close()

    assert result["ok"] is False, result
    assert result["failed_step"] == 1
    recovery = result["recovery"]
    assert recovery["attempted"] is True, recovery
    assert recovery["outcome"] == "recovered", recovery
    assert recovery["actions"] == ["reap_processes", "reset_halt", "probe_target"]
    assert recovery["safe_state_predicate"] == "reset_halt"
    # The claim is about the board, so it is read off the board's own log.
    assert "reset_target:halt" in backend.calls
    assert backend.calls.index("reset_target:halt") < backend.calls.index("probe_target")


def test_a_passing_run_is_left_alone(tmp_path: Path) -> None:
    """Recovery is what an abort does, not what every run does. A green run has
    nothing to recover from and driving a reset after one would be a physical
    act nobody asked for."""
    config = config_for(tmp_path)
    backend = FakeBackend()
    service = AgenticHILToolService(config, backend=backend)
    try:
        result = TestReactor(config, service).run(failing_flash_plan(tmp_path))
    finally:
        service.close()

    assert result["ok"] is True, result
    assert "recovery" not in result
    assert "reset_target:halt" not in backend.calls


def test_the_run_result_names_a_withheld_recovery_and_why(tmp_path: Path) -> None:
    """A bench that forbids reset does not get the recovery for free, and the
    result says which setting made that choice — otherwise the operator of a
    bench that quietly never recovers has nothing to read."""
    config = config_for(tmp_path, allow_reset=False)
    backend = FakeBackend(flash_ok=False)
    service = AgenticHILToolService(config, backend=backend)
    try:
        result = TestReactor(config, service).run(failing_flash_plan(tmp_path))
    finally:
        service.close()

    recovery = result["recovery"]
    assert recovery["attempted"] is False, recovery
    assert recovery["reason_not_attempted"] == "allow_reset_missing"
    assert "allow_reset" in recovery["summary"]
    assert "reset_target:halt" not in backend.calls


def test_a_bench_with_recovery_off_takes_no_action(tmp_path: Path) -> None:
    config = config_for(tmp_path, auto_recover="off")
    backend = FakeBackend(flash_ok=False)
    service = AgenticHILToolService(config, backend=backend)
    try:
        result = TestReactor(config, service).run(failing_flash_plan(tmp_path))
    finally:
        service.close()

    recovery = result["recovery"]
    assert recovery["attempted"] is False
    assert recovery["reason_not_attempted"] == "auto_recover_policy_off"
    assert recovery["auto_recover_policy"] == "off"
    assert backend.calls == ["flash_firmware"]


def test_the_generated_default_is_the_one_that_recovers(tmp_path: Path) -> None:
    """A bench that never named a policy still gets the recovery, and the result
    says the value came from the default rather than from anybody's choice."""
    config = config_for(tmp_path)
    assert config.recovery.auto_recover == "reset_halt"
    assert config.recovery.auto_recover_explicit is False
    backend = FakeBackend(flash_ok=False)
    service = AgenticHILToolService(config, backend=backend)
    try:
        result = TestReactor(config, service).run(failing_flash_plan(tmp_path))
    finally:
        service.close()

    assert result["recovery"]["auto_recover_policy_source"] == "default"
    assert result["recovery"]["outcome"] == "recovered"


# ---------------------------------------------------------------------------
# The recovery action against a real incident.


def test_the_recovery_action_resolves_the_incident_the_run_raised(tmp_path: Path) -> None:
    """The point of the whole change: an incident a reset settles is settled by
    the reset, and the bench is free afterwards without a person in the loop."""
    config = config_for(tmp_path)
    backend = FakeBackend()
    service = AgenticHILToolService(config, backend=backend)
    try:
        incident = quarantine(service, RESET_REASON)
        assert service.coordinator.status()["blocked"] is True
        recovery = service.recover_after_failed_run(["dut"])

        assert recovery["outcome"] == "recovered", recovery
        assert recovery["incident_resolved"] is True, recovery
        assert recovery["resolved_reason"] == RESET_REASON
        assert recovery["resolved_quarantine_id"] == incident
        assert service.coordinator.status()["blocked"] is False
    finally:
        service.close()


def test_the_resolution_is_recorded_as_a_recovery_action(tmp_path: Path) -> None:
    """No quarantine ends without a line naming what ended it. `recovery_action`
    has to stay distinct from an operator's signature and from the no-contact
    class: three different kinds of evidence, and an audit that spelled them the
    same could not tell afterwards which had happened."""
    config = config_for(tmp_path)
    service = AgenticHILToolService(config, backend=FakeBackend())
    try:
        quarantine(service, RESET_REASON)
        service.recover_after_failed_run(["dut"])
    finally:
        service.close()

    lines = [line for line in ledger(config) if line.get("via") == "recovery_action"]
    assert len(lines) == 1, ledger(config)
    assert lines[0]["actor"] == "server"
    assert lines[0]["attestation"] == "recovery_action_verified"
    assert lines[0]["reason"] == RESET_REASON
    assert lines[0]["resources"] == [RESOURCE]


def test_a_recovery_that_fails_leaves_the_incident_standing(tmp_path: Path) -> None:
    """Failing closed is the rule recovery inherits: the one path allowed to
    unblock hardware must never unblock it on an action that did not work."""
    config = config_for(tmp_path)
    service = AgenticHILToolService(config, backend=FakeBackend(reset_ok=False))
    try:
        quarantine(service, RESET_REASON)
        recovery = service.recover_after_failed_run(["dut"])

        assert recovery["attempted"] is True
        assert recovery["outcome"] == "failed", recovery
        assert recovery["failed_action"] == "reset_halt"
        assert service.coordinator.status()["blocked"] is True
    finally:
        service.close()

    assert [line for line in ledger(config) if line.get("via") == "recovery_action"] == []


@pytest.mark.parametrize("reason", PERIPHERAL_CLEANUP_REASONS)
def test_a_peripheral_cleanup_incident_is_not_settled_by_a_target_reset(tmp_path: Path, reason: str) -> None:
    """The narrowing the reason set exists to make.

    A reset driven into halt and a probe re-read speak for the target and nothing
    else: a serial handle that may still be open holding modem lines, the reader
    thread behind it, a CAN adapter that may still be on the bus, are not theirs
    to attest. So the recovery action still runs — the next run wants a defined
    board — but the incident keeps the operator's route rather than being cleared
    without the evidence its name asks for. Admitting it by a negative name test
    is exactly what handed a still-busy peripheral back to service.
    """
    config = config_for(tmp_path)
    backend = FakeBackend()
    service = AgenticHILToolService(config, backend=backend)
    try:
        incident = quarantine(service, reason)
        assert service.coordinator.status()["blocked"] is True
        assert reason not in service.coordinator.recoverable_reasons()

        recovery = service.recover_after_failed_run(["dut"])

        # The board was still driven into a defined state for the next run...
        assert recovery["outcome"] == "recovered", recovery
        assert "reset_target:halt" in backend.calls
        # ...but the target reset does not speak for the peripheral, so the
        # incident stays for the route that can verify it.
        assert recovery["incident_resolved"] is False, recovery
        assert recovery["incident_open"] is True, recovery
        assert recovery["quarantine_id"] == incident
        assert service.coordinator.status()["blocked"] is True
    finally:
        service.close()

    # Nothing recorded a clearance that never happened.
    assert [line for line in ledger(config) if line.get("via") == "recovery_action"] == []


def test_a_probe_that_finds_no_target_is_not_a_recovery(tmp_path: Path) -> None:
    """The reset can return happily and the target still be gone. The predicate
    is the read-back, not the command."""
    config = config_for(tmp_path)
    service = AgenticHILToolService(config, backend=FakeBackend(target_detected=False))
    try:
        quarantine(service, RESET_REASON)
        recovery = service.recover_after_failed_run(["dut"])

        assert recovery["outcome"] == "failed"
        assert recovery["failed_action"] == "probe_target"
        assert service.coordinator.status()["blocked"] is True
    finally:
        service.close()


def test_an_audit_broken_incident_is_not_settled_by_any_action(tmp_path: Path) -> None:
    """The one family a performed action cannot answer. The reason names a
    ledger that could not be written, and no amount of driving the target writes
    it, so this keeps needing the operator's route however well the reset goes.
    """
    config = config_for(tmp_path)
    service = AgenticHILToolService(config, backend=FakeBackend())
    try:
        quarantine(service, "machine_recovery_audit_broken", audit_broken=True)
        recovery = service.recover_after_failed_run(["dut"])

        assert recovery["outcome"] == "recovered", recovery
        assert recovery["incident_resolved"] is False, recovery
        assert recovery["incident_open"] is True
        assert service.coordinator.status()["blocked"] is True
    finally:
        service.close()


def test_a_recovery_whose_lease_release_cannot_be_confirmed_does_not_report_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Clearing the reason is not the whole of it: the leases still have to be
    given back, and a release that cannot persist its own record fails closed —
    it re-quarantines the bench under a fresh `lease_release_unconfirmed`
    incident. A result that stamped `incident_resolved: true` regardless would
    tell a caller to continue on a bench `hardware_lease_status` still shows as
    `cleanup_required`. The settlement must inspect the coordinator's final state
    and report the new open incident instead."""
    config = config_for(tmp_path)
    service = AgenticHILToolService(config, backend=FakeBackend())
    try:
        quarantine(service, RESET_REASON)
        # The handle the recovery will try to give back once the reason clears.
        lease = next(iter(service.coordinator.leases.values()))
        service._quarantined_lease = lease

        original_persist = service.coordinator._persist_lease

        def failing_persist(lease_arg, state=None, incident_override=None):
            # Only the release's durable "released" write fails; resolving the
            # reason (which persists other states) is left to run.
            if state == "released":
                raise OSError("injected lease-release persistence failure")
            return original_persist(lease_arg, state=state, incident_override=incident_override)

        monkeypatch.setattr(service.coordinator, "_persist_lease", failing_persist)

        recovery = service.recover_after_failed_run(["dut"])

        assert recovery["outcome"] == "recovered", recovery
        assert recovery["incident_resolved"] is False, recovery
        assert recovery["incident_open"] is True
        assert recovery["cleanup_required"] is True
        assert recovery["quarantined"] is True
        assert LEASE_RELEASE_RETRY_REASON in recovery["cleanup_reasons"], recovery

        # And the tool a caller would read next agrees: the bench is not free.
        status = service.coordinator.status()
        assert status["blocked"] is True
        assert LEASE_RELEASE_RETRY_REASON in status["cleanup_reasons"], status
    finally:
        service.close()


def test_a_recovery_class_call_does_not_force_cleared_when_lease_release_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The second settlement path: a recovery-class call that clears its incident
    forces `cleanup_required` and `quarantined` to false on the envelope it
    returns. It must not do that when giving the lease back could not be
    confirmed and a fresh incident re-quarantined the bench — the call's own
    answer survives, but the stamped success does not."""
    config = config_for(tmp_path)
    service = AgenticHILToolService(config, backend=FakeBackend())
    try:
        quarantine(service, "debug_session_cleanup_unconfirmed")
        lease = next(iter(service.coordinator.leases.values()))
        service._quarantined_lease = lease

        original_persist = service.coordinator._persist_lease

        def failing_persist(lease_arg, state=None, incident_override=None):
            if state == "released":
                raise OSError("injected lease-release persistence failure")
            return original_persist(lease_arg, state=state, incident_override=incident_override)

        monkeypatch.setattr(service.coordinator, "_persist_lease", failing_persist)

        settled = service._settle_after_recovery_class_call(
            "probe_target", True, {"ok": True, "tool": "probe_target", "target_detected": True}
        )

        assert settled["ok"] is True, settled  # the probe's own answer is not lost
        assert settled["incident_resolved"] is False, settled
        assert settled["cleanup_required"] is True
        assert settled["quarantined"] is True
        assert LEASE_RELEASE_RETRY_REASON in settled["cleanup_reasons"], settled
        assert service.coordinator.status()["blocked"] is True
    finally:
        service.close()


# ---------------------------------------------------------------------------
# B. The recovery class runs during an incident; the stimulus class does not.


@pytest.mark.parametrize("tool", ["probe_target", "reset_target", "flash_firmware"])
def test_the_recovery_class_runs_during_an_incident(tmp_path: Path, tool: str) -> None:
    """These are the remedy, not a use of the bench. Refusing them was what made
    an incident a padlock whose only key was a person at a shell — for a
    condition a reset settles."""
    config = config_for(tmp_path)
    firmware = tmp_path / "build" / "app.elf"
    firmware.parent.mkdir(parents=True, exist_ok=True)
    firmware.write_bytes(b"\x7fELF" + b"\x00" * 12)
    arguments = {"flash_firmware": {"image_path": "build/app.elf"}, "reset_target": {"mode": "halt"}}.get(tool, {})
    backend = FakeBackend()
    service = AgenticHILToolService(config, backend=backend)
    try:
        quarantine(service, "safe_state_unconfirmed")
        result = service.call(tool, arguments)

        assert result.get("error_type") != "resource_quarantined", result
        assert result["ok"] is True, result
    finally:
        service.close()


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("com_session_start", {"port_id": "dut"}),
        ("can_session_start", {"bus_id": "dut"}),
        ("bench_run_start", {"devices": [{"kind": "debugger"}]}),
    ],
)
def test_the_stimulus_class_stays_behind_the_incident(tmp_path: Path, tool: str, arguments: dict) -> None:
    """The other half of the boundary, and the reason it is not simply "physical
    calls are refused": a reset drives the target harder than a `com_write`
    does. What separates them is what the call is for. A measurement taken
    across an unresolved incident is one nobody can stand behind afterwards."""
    config = config_for(tmp_path)
    service = AgenticHILToolService(config, backend=FakeBackend())
    try:
        quarantine(service, "safe_state_unconfirmed")
        result = service.call(tool, arguments)

        assert result["ok"] is False, result
    finally:
        service.close()


def test_a_reset_during_an_incident_resolves_it(tmp_path: Path) -> None:
    """The agent asking for the remedy gets the same settlement the automatic
    path would have reached, because the evidence is the same: a confirmed reset
    into halt. Same ledger line, so the audit does not care which asked.

    `readonly` policy on purpose: it stops the automatic attempt at the gate
    from driving a reset of its own, so what clears the incident here is
    unambiguously the call the agent made. The policy governs what this bench
    does on its own initiative; `allow_reset` governs what it may be asked to
    do, and an explicitly requested reset is not an automatic one."""
    config = config_for(tmp_path, auto_recover="readonly")
    service = AgenticHILToolService(config, backend=FakeBackend())
    try:
        incident = quarantine(service, RESET_REASON)
        result = service.call("reset_target", {"mode": "halt"})

        assert result["ok"] is True, result
        assert result["incident_resolved"] is True, result
        assert result["resolved_quarantine_id"] == incident
        assert service.coordinator.status()["blocked"] is False
    finally:
        service.close()

    assert [line["via"] for line in ledger(config) if line.get("via") == "recovery_action"] == ["recovery_action"]


def test_a_read_only_probe_does_not_settle_what_only_a_reset_can(tmp_path: Path) -> None:
    """A probe that answers says the probe answers. It does not say the core is
    halted, so it cannot settle a reason that names a target which may be
    running — the distinction the acquire path already draws between its two
    predicates, held here too.

    `readonly` policy so the automatic attempt at the gate cannot drive a reset
    of its own and settle the incident before the read-only probe is asked to."""
    config = config_for(tmp_path, auto_recover="readonly")
    service = AgenticHILToolService(config, backend=FakeBackend())
    try:
        quarantine(service, RESET_REASON)
        result = service.call("probe_target", {})

        assert result["ok"] is True, result
        assert "incident_resolved" not in result
        assert service.coordinator.status()["blocked"] is True
    finally:
        service.close()


def test_a_follow_up_run_starts_without_a_ritual(tmp_path: Path) -> None:
    """What the change is for. A run fails, recovery clears what it raised, and
    the next run declares its devices and starts — no operator, no command line,
    no state files deleted."""
    config = config_for(tmp_path)
    service = AgenticHILToolService(config, backend=FakeBackend())
    try:
        quarantine(service, RESET_REASON)
        assert service.call("bench_run_start", {"devices": [{"kind": "debugger"}]})["ok"] is False

        service.recover_after_failed_run(["dut"])
        started = service.call("bench_run_start", {"devices": [{"kind": "debugger"}]})

        assert started["ok"] is True, started
        assert service.call("bench_run_stop")["ok"] is True
    finally:
        service.close()


def test_bench_run_stop_recovers_an_interactive_run_that_failed(tmp_path: Path) -> None:
    """The second caller of the same seam. An interactive run has no step list to
    read a verdict off, so the trigger is an incident still standing once the
    devices are back — which is the same statement in the form this call sees."""
    config = config_for(tmp_path)
    backend = FakeBackend()
    service = AgenticHILToolService(config, backend=backend)
    try:
        assert service.call("bench_run_start", {"devices": [{"kind": "debugger"}]})["ok"] is True
        service.coordinator.poison(RESET_REASON)
        assert service.coordinator.status()["blocked"] is True

        stopped = service.call("bench_run_stop")

        assert "recovery" in stopped, stopped
        assert stopped["recovery"]["attempted"] is True
        assert "reset_target:halt" in backend.calls
    finally:
        service.close()


def test_bench_run_stop_says_nothing_about_recovery_on_a_clean_run(tmp_path: Path) -> None:
    """A run that ended without an incident is not a failed run, and a recovery
    block on it would report an action that never happened."""
    config = config_for(tmp_path)
    backend = FakeBackend()
    service = AgenticHILToolService(config, backend=backend)
    try:
        service.call("bench_run_start", {"devices": [{"kind": "debugger"}]})
        stopped = service.call("bench_run_stop")

        assert stopped["ok"] is True
        assert "recovery" not in stopped
        assert backend.calls == []
    finally:
        service.close()


def test_a_no_contact_incident_still_clears_without_driving_the_board(tmp_path: Path) -> None:
    """The reason class that needs no predicate keeps needing none. Recovery
    running at every abort must not turn a bookkeeping failure into a reset."""
    config = config_for(tmp_path, auto_recover="readonly")
    backend = FakeBackend()
    service = AgenticHILToolService(config, backend=backend)
    try:
        quarantine(service, LEASE_RELEASE_RETRY_REASON)
        recovery = service.recover_after_failed_run(["dut"])

        assert recovery["incident_resolved"] is True, recovery
        assert recovery["safe_state_predicate"] == "readonly_probe"
        assert "reset_target:halt" not in backend.calls
    finally:
        service.close()


# ---------------------------------------------------------------------------
# C. The incident this owner inherited, which is the one the bench reported.


def test_the_dead_owner_incident_the_run_inherited_is_settled_by_the_recovery_action(tmp_path: Path) -> None:
    """The bench case, and the narrowing it exposed.

    On the state before this change the recovery ran, confirmed a reset into
    halt, and then reported `incident_resolved: false` with "it needs the
    operator's route", because the incident had no lease of this owner behind it
    and `retryable_incident` refused that shape outright. What the dead owner
    left unconfirmed is exactly what the reset erased, so the same evidence
    settles it whichever process opened the session that failed.
    """
    config = leave_contacted_dead_owner(tmp_path)
    backend = FakeBackend()
    service = AgenticHILToolService(config, backend=backend)
    try:
        assert service.coordinator.status()["cleanup_reasons"] == [DEAD_OWNER_REASON]
        incident = service.coordinator.quarantine_id

        recovery = service.recover_after_failed_run(["dut"])

        assert recovery["outcome"] == "recovered", recovery
        assert recovery["safe_state_predicate"] == "reset_halt"
        assert recovery["incident_resolved"] is True, recovery
        assert recovery["incident_open"] is False
        assert recovery["resolved_reason"] == DEAD_OWNER_REASON
        assert recovery["resolved_quarantine_id"] == incident
        assert "incident_summary" not in recovery
        # The claim is about the board, so it is read off the board's own log.
        assert "reset_target:halt" in backend.calls
        assert service.coordinator.status()["blocked"] is False
    finally:
        service.close()

    lines = recovery_action_lines(config)
    assert len(lines) == 1, ledger(config)
    assert lines[0]["actor"] == RECOVERY_ACTOR_SERVER
    assert lines[0]["attestation"] == ATTESTATION_RECOVERY_ACTION
    assert lines[0]["reason"] == DEAD_OWNER_REASON
    assert lines[0]["resources"] == [DEAD_OWNER_RESOURCE]


def test_the_inherited_incident_leaves_no_marker_behind_and_the_next_run_starts(tmp_path: Path) -> None:
    """The whole point, end to end: an endurance run dies on step one against a
    bench a dead process left quarantined, the abort recovers it, and the next
    run declares its devices and starts. No operator, no command line, no state
    files deleted, and the resource marker the dead owner left is gone, so a
    fresh process cannot adopt the same incident all over again."""
    config = leave_contacted_dead_owner(tmp_path)
    service = AgenticHILToolService(config, backend=FakeBackend(flash_ok=False))
    try:
        result = TestReactor(config, service).run(failing_flash_plan(tmp_path))

        # The run still failed. Recovering the bench does not un-fail a test.
        assert result["ok"] is False, result
        assert service.coordinator.status()["blocked"] is False

        started = service.call("bench_run_start", {"devices": [{"kind": "debugger"}]})
        assert started["ok"] is True, started
        assert service.call("bench_run_stop")["ok"] is True
    finally:
        service.close()

    observer = HardwareCoordinator(config, "fresh-process")
    status = observer.status()
    assert status["blocked"] is False, status
    assert status["cleanup_reasons"] == []
    assert observer._read_record(DEAD_OWNER_RESOURCE)["state"] == "released"
    assert len(recovery_action_lines(config)) == 1, ledger(config)


def test_a_failed_recovery_leaves_the_inherited_incident_for_the_next_call(tmp_path: Path) -> None:
    """Failing closed, then converging. A board with no power refuses the reset,
    the incident stays exactly as it was and the recovery-class call that runs
    against it is refused too, the escalation as it has always been. Once the
    board answers, the next confirmed reset settles it with one ledger line, and
    nobody was asked for anything."""
    config = leave_contacted_dead_owner(tmp_path)
    backend = FakeBackend(reset_ok=False)
    service = AgenticHILToolService(config, backend=backend)
    try:
        incident = service.coordinator.status()["quarantine_id"]

        failed = service.recover_after_failed_run(["dut"])
        assert failed["outcome"] == "failed", failed
        assert failed["failed_action"] == "reset_halt"
        assert service.coordinator.status()["blocked"] is True
        assert service.coordinator.quarantine_id == incident
        assert recovery_action_lines(config) == []

        # The board is still dark: the remedy call escalates rather than
        # pretending, exactly as it does today.
        dark = service.call("reset_target", {"mode": "halt"})
        assert dark["ok"] is False, dark
        assert dark["error_type"] == "resource_quarantined"
        assert service.coordinator.status()["blocked"] is True

        # Somebody plugs the board back in.
        backend.reset_ok = True
        recovered = service.call("reset_target", {"mode": "halt"})

        assert recovered["ok"] is True, recovered
        assert service.coordinator.status()["blocked"] is False
    finally:
        service.close()

    lines = recovery_action_lines(config)
    assert len(lines) == 1, ledger(config)
    assert lines[0]["reason"] == DEAD_OWNER_REASON
    assert lines[0]["actor"] == RECOVERY_ACTOR_SERVER
    assert lines[0]["attestation"] == ATTESTATION_RECOVERY_ACTION


def test_a_readonly_bench_never_settles_the_inherited_incident_by_reading(tmp_path: Path) -> None:
    """The predicate still decides. A dead owner's session may have left the
    target running, and a probe that answers says the probe answers, so a bench
    whose policy forbids driving the target keeps this incident for the operator,
    and the board is not reset behind that policy's back."""
    config = leave_contacted_dead_owner(tmp_path, auto_recover="readonly")
    backend = FakeBackend()
    service = AgenticHILToolService(config, backend=backend)
    try:
        assert service.coordinator.status()["cleanup_reasons"] == [DEAD_OWNER_REASON]

        recovery = service.recover_after_failed_run(["dut"])

        assert recovery["outcome"] == "recovered", recovery
        assert recovery["safe_state_predicate"] == "readonly_probe"
        assert recovery["incident_resolved"] is False, recovery
        assert recovery["incident_open"] is True
        assert service.coordinator.status()["blocked"] is True
        assert "reset_target:halt" not in backend.calls
    finally:
        service.close()

    assert recovery_action_lines(config) == []


@pytest.mark.parametrize("reason", ["machine_recovery_audit_broken", "debug_coordination_report_audit_broken"])
def test_no_automatic_route_settles_an_audit_broken_incident(tmp_path: Path, reason: str) -> None:
    """The one family every automatic route keeps refusing, pinned across all
    three of them. The reason names a ledger that could not be written, and
    driving the target writes no ledger, so the widened rule must not reach it:
    not through the run teardown, not through the gate's own attempt, and not
    through a recovery-class call that came back confirmed."""
    config = config_for(tmp_path)
    service = AgenticHILToolService(config, backend=FakeBackend())
    try:
        incident = quarantine(service, reason, audit_broken=True)

        teardown = service.recover_after_failed_run(["dut"])
        assert teardown["outcome"] == "recovered", teardown
        assert teardown["incident_resolved"] is False, teardown
        assert teardown["incident_open"] is True

        assert service._attempt_machine_recovery() is None
        assert reason not in service.coordinator.recoverable_reasons()

        settled = service._settle_after_recovery_class_call(
            "reset_target", True, {"ok": True, "tool": "reset_target", "mode": "halt"}
        )
        assert "incident_resolved" not in settled, settled

        assert service.coordinator.status()["blocked"] is True
        assert service.coordinator.quarantine_id == incident
    finally:
        service.close()

    assert recovery_action_lines(config) == []


def test_an_inherited_incident_with_a_broken_audit_stays_with_the_operator(tmp_path: Path) -> None:
    """The same exclusion on the inherited half. The record is the only evidence
    a lease-less incident has, and a lease entry in it that says its audit is
    broken is the record saying it cannot be the evidence for anything."""
    config = leave_contacted_dead_owner(tmp_path, audit_ok=False)
    service = AgenticHILToolService(config, backend=FakeBackend())
    try:
        assert service.coordinator.status()["cleanup_reasons"] == [DEAD_OWNER_REASON]

        recovery = service.recover_after_failed_run(["dut"])

        assert recovery["outcome"] == "recovered", recovery
        assert recovery["incident_resolved"] is False, recovery
        assert recovery["incident_open"] is True
        assert service.coordinator.status()["blocked"] is True
    finally:
        service.close()

    assert recovery_action_lines(config) == []


def test_an_inherited_incident_recorded_under_another_configuration_is_refused(tmp_path: Path) -> None:
    """The third of the three questions `recover` asks, asked here too: a record
    stamped with configuration bytes this server did not load describes a bench
    under a policy nobody here can speak for, so no recovery action clears it and
    the `config_changed` route stays the operator's."""
    config = leave_contacted_dead_owner(tmp_path)
    service = AgenticHILToolService(config, backend=FakeBackend())
    try:
        service.coordinator.status()
        record = service.coordinator._read_record(service.coordinator.project_key)
        service.coordinator._write_record(service.coordinator.project_key, {**record, "config_sha256": "0" * 64})

        recovery = service.recover_after_failed_run(["dut"])

        assert recovery["incident_resolved"] is False, recovery
        assert recovery["incident_open"] is True
        assert service.coordinator.status()["blocked"] is True
    finally:
        service.close()

    assert recovery_action_lines(config) == []
