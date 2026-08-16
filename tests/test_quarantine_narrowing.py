"""The quarantine is the audit halt; everything else proves itself at the next contact.

The rule this file holds, in one line: a gate is only owed where the missing
proof cannot come back on its own. A target's state comes back with the next
reset into halt and an answering probe. A serial handle's and a CAN adapter's
come back with their own next open, which the operating system refuses by itself
if the handle is really stuck. An evidence chain does not come back at all,
because no reset writes a report that was never written, and that is the one
family that still holds the bench.

What each section is for:

* **A** is the family that still stands, held here rather than only in the files
  that grew around it, so a reader of this file can see both halves of the rule
  in one place.
* **B** is the abort-with-recovery arc, unchanged, with the standing state gone
  from behind it: the recovery runs, a confirmed one is attested, and whatever
  it did not settle ends when the call does.
* **C** is the peripheral pair, which never opens an incident at all.
* **D** is the dead owner, reaped and recovered as before, holding nothing when
  the recovery cannot run.
* **E** is the evidence itself: every line the gate era wrote is still written.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import DEFAULT_TEST_PERMISSIONS, write_config

from agentic_hil.comports import ComPortService
from agentic_hil.config import load_config
from agentic_hil.coordination import (
    ATTESTATION_NO_STANDING_STATE,
    ATTESTATION_RECOVERY_ACTION,
    RECOVERY_ACTION_VIA,
    RECOVERY_ACTOR_SERVER,
    HardwareCoordinator,
    debugger_effect_resources,
)
from agentic_hil.report import CONTACT_MARKER_KEY, CONTACT_MARKER_SOURCE_KEY, write_report
from agentic_hil.tools import AgenticHILToolService

# Machine-wide device locks contend across sibling clones, so these names are
# this module's alone.
RESOURCE = "physical:quarantine-narrowing-probe"
DEAD_OWNER_RESOURCE = "physical:quarantine-narrowing-dead-owner-uart"
DEAD_OWNER_REASON = "owner_process_exited_without_release"
# An unconfirmed reset: the target may be running, and only driving it into a
# defined state and reading it back answers that.
TARGET_REASON = "debugger_result_unconfirmed"
PORT_ID = "dut"


class FakeBackend:
    def __init__(self, *, reset_ok: bool = True, probe_ok: bool = True, target_detected: bool = True) -> None:
        self.calls: list[str] = []
        self.reset_ok = reset_ok
        self.probe_ok = probe_ok
        self.target_detected = target_detected

    def probe_target(self) -> dict:
        self.calls.append("probe_target")
        return {"ok": self.probe_ok, "tool": "probe_target", "target_detected": self.target_detected if self.probe_ok else False}

    def reset_target(self, mode: str = "run") -> dict:
        self.calls.append(f"reset_target:{mode}")
        if self.reset_ok:
            return {"ok": True, "tool": "reset_target", "mode": mode}
        return {"ok": False, "tool": "reset_target", "mode": mode, "error_type": "reset_failed", "side_effect_status": "unknown", "summary": "Reset result unconfirmed."}

    def close(self) -> None:
        return None

    def sessionless_debug_tools(self) -> frozenset[str]:
        return frozenset()


def config_for(workspace: Path, *, auto_recover: str | None = None, com_ports_yaml: str | None = None):
    extra = {"com_ports_yaml": com_ports_yaml} if com_ports_yaml is not None else {}
    written = write_config(
        workspace,
        permissions={**DEFAULT_TEST_PERMISSIONS, "allow_probe": True, "allow_flash": True, "allow_reset": True},
        auto_recover=auto_recover,
        **extra,
    )
    written.write_text("permissions:\n  allow_recover: true\n" + written.read_text(encoding="utf-8"), encoding="utf-8")
    return load_config(str(written))


def com_config(workspace: Path):
    return config_for(workspace, com_ports_yaml=f'com_ports:\n  {PORT_ID}:\n    device: "COM_TEST"\n')


def ledger(config) -> list[dict]:
    path = Path(HardwareCoordinator(config, "ledger-reader").root) / "recovery.jsonl"
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# A. The family that still stands.


def test_a_broken_audit_still_quarantines_and_still_gates(tmp_path: Path) -> None:
    """The exception the whole narrowing is defined against. The reason names a
    ledger that could not be written, and nothing a later call does writes it,
    so the bench is held and the stimulus class waits."""
    config = config_for(tmp_path)
    service = AgenticHILToolService(config, backend=FakeBackend())
    try:
        lease = service.coordinator.acquire(RESOURCE)
        lease.quarantine("com_audit_broken", audit_broken=True)

        assert service.coordinator.incident_stands is True
        refused = service.call("bench_run_start", {"devices": [{"kind": "debugger"}]})

        assert refused["ok"] is False, refused
        assert refused["error_type"] == "resource_quarantined"
        # And it is still there afterwards: the seam that ends every other
        # incident refuses this one by construction.
        assert service.coordinator.incident_stands is True
        assert service.coordinator.stand_down() is None
    finally:
        service.close()

    assert ledger(config) == []


def test_no_automatic_route_ends_a_broken_audit(tmp_path: Path) -> None:
    """Neither the recovery action nor the seam behind it. A reset drives a
    target and writes no ledger, so the one thing this reason is missing is the
    one thing no automatic route can supply."""
    config = config_for(tmp_path)
    backend = FakeBackend()
    service = AgenticHILToolService(config, backend=backend)
    try:
        lease = service.coordinator.acquire(RESOURCE)
        lease.quarantine("debug_audit_broken", audit_broken=True)

        assert service._attempt_machine_recovery() is None
        assert service.recover_after_failed_run(["dut"])["incident_open"] is True
        assert service.coordinator.incident_stands is True
    finally:
        service.close()

    assert [line for line in ledger(config) if line.get("via") == RECOVERY_ACTION_VIA] == []


def test_the_operator_route_is_the_way_out_of_a_broken_audit(tmp_path: Path) -> None:
    """Unchanged, and asserted here so the narrowing cannot quietly take the
    exit with the gate."""
    config = config_for(tmp_path)
    owner = HardwareCoordinator(config, "audit-owner")
    lease = owner.acquire(RESOURCE)
    lease.quarantine("com_report_audit_broken", audit_broken=True)
    incident = str(owner.quarantine_id)
    owner.close()

    operator = HardwareCoordinator(config, "operator-cli")
    assert operator.status()["incident_stands"] is True
    recovered = operator.recover(safe_state_confirmed=True, quarantine_id=incident)

    assert recovered["ok"] is True, recovered
    assert operator.status()["blocked"] is False
    assert [line["attestation"] for line in ledger(config)] == ["operator_confirmation"]


# ---------------------------------------------------------------------------
# B. The failed run: recovery unchanged, standing state gone.


def test_a_confirmed_recovery_is_attested_and_leaves_no_incident(tmp_path: Path) -> None:
    """The arc that already existed, asserted end to end so the removal of the
    gate cannot be mistaken for a removal of the recovery. The reset ran, the
    probe answered, and the ledger says a recovery action established the safe
    state, not that nobody needed one."""
    config = config_for(tmp_path, auto_recover="reset_halt")
    backend = FakeBackend()
    service = AgenticHILToolService(config, backend=backend)
    try:
        lease = service.coordinator.acquire(*debugger_effect_resources(config))
        lease.quarantine(TARGET_REASON)
        service._quarantined_lease = lease

        settled = service.recover_after_failed_run(["dut"])

        assert settled["outcome"] == "recovered", settled
        assert settled["incident_resolved"] is True, settled
        assert backend.calls == ["reset_target:halt", "probe_target"]
        assert service.coordinator.blocked is False
    finally:
        service.close()

    lines = ledger(config)
    assert [line["attestation"] for line in lines] == [ATTESTATION_RECOVERY_ACTION]
    assert lines[0]["via"] == RECOVERY_ACTION_VIA
    assert lines[0]["reason"] == TARGET_REASON


def test_a_failed_run_whose_recovery_could_not_confirm_leaves_no_hold(tmp_path: Path) -> None:
    """The half #216 changed. The board would not reset, so nothing attested
    anything, and the answer to that is not a padlock: the reason is recorded,
    the ledger says the incident ended unattested, and the next call meets a
    bench with nothing on it."""
    config = config_for(tmp_path, auto_recover="reset_halt")
    backend = FakeBackend(reset_ok=False)
    service = AgenticHILToolService(config, backend=backend)
    try:
        result = service.call("reset_target", {"mode": "run"})

        assert result["ok"] is False, result
        assert result["cleanup_reasons"] == [TARGET_REASON]
        assert result["quarantined"] is False
        assert result["incident_stood_down"]["reasons"] == [TARGET_REASON]
        assert service.coordinator.blocked is False
    finally:
        service.close()

    lines = [line for line in ledger(config) if line.get("recovery") == "incident_stood_down"]
    assert [line["attestation"] for line in lines] == [ATTESTATION_NO_STANDING_STATE]
    assert lines[0]["actor"] == RECOVERY_ACTOR_SERVER
    assert lines[0]["reasons"] == [TARGET_REASON]


def test_the_next_probe_refuses_on_its_own_evidence_and_not_on_a_hold(tmp_path: Path) -> None:
    """Fail-fast arises naturally. A board that is still unreachable makes the
    next call fail for the reason it is actually failing for, which is what an
    operator reading the result needs, instead of a refusal about an incident
    somebody has to sign off."""
    config = config_for(tmp_path, auto_recover="reset_halt")
    backend = FakeBackend(reset_ok=False, probe_ok=False)
    service = AgenticHILToolService(config, backend=backend)
    try:
        service.call("reset_target", {"mode": "run"})

        second = service.call("probe_target", {})

        assert second["ok"] is False, second
        assert second.get("error_type") != "resource_quarantined"
        assert "probe_target" in backend.calls
    finally:
        service.close()


def test_the_next_run_starts_without_a_ritual(tmp_path: Path) -> None:
    """What the change is for: no operator, no command line, no state files
    deleted, and a bench that had a bad run is a bench the next run can use."""
    config = config_for(tmp_path, auto_recover="reset_halt")
    service = AgenticHILToolService(config, backend=FakeBackend(reset_ok=False))
    try:
        assert service.call("reset_target", {"mode": "run"})["ok"] is False

        started = service.call("bench_run_start", {"devices": [{"kind": "debugger"}]})

        assert started["ok"] is True, started
        assert service.call("bench_run_stop")["ok"] is True
    finally:
        service.close()


# ---------------------------------------------------------------------------
# C. The peripheral pair: recorded, never an incident.


class _FailingHandle:
    """A port that got as far as a handle and then refuses both the open and the
    close, which is exactly the state `com_open_cleanup_unconfirmed` is named
    for: something exists on the host and nothing here can say it is shut."""

    def __init__(self) -> None:
        self.is_open = True

    def open(self) -> None:
        raise OSError("open failed after the handle existed")

    def close(self) -> None:
        raise OSError("close failed too")


class _WorkingHandle:
    def __init__(self) -> None:
        self.is_open = False
        self.in_waiting = 0

    def open(self) -> None:
        self.is_open = True

    def read(self, size: int) -> bytes:
        return b""

    def write(self, data: bytes) -> int:
        return len(data)

    def flush(self) -> None:
        return None

    def reset_input_buffer(self) -> None:
        return None

    def cancel_read(self) -> None:
        return None

    def close(self) -> None:
        self.is_open = False


def _install_serial(monkeypatch: pytest.MonkeyPatch, handle) -> None:
    monkeypatch.setitem(sys.modules, "serial", SimpleNamespace(Serial=lambda *args, **kwargs: handle))


def test_an_open_whose_cleanup_did_not_confirm_records_it_and_holds_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The refusal carries the reason and its remediation, the lease record
    carries the event, and no incident is opened: a handle that is really still
    open makes the next open fail through the operating system, and one that is
    not makes it succeed."""
    config = com_config(tmp_path)
    service = AgenticHILToolService(config)
    _install_serial(monkeypatch, _FailingHandle())
    try:
        result = service.call("com_session_start", {"port_id": PORT_ID})

        assert result["ok"] is False, result
        assert result["cleanup_reasons"] == ["com_open_cleanup_unconfirmed"]
        assert result["quarantine_guidance"][0]["reason"] == "com_open_cleanup_unconfirmed"
        assert result["quarantined"] is False
        assert "incident_stood_down" not in result
        assert service.coordinator.blocked is False
        assert service.coordinator.quarantine_id is None
    finally:
        service.close()

    assert ledger(config) == []


def test_the_next_open_is_the_proof(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The whole argument for recording rather than holding. Nobody was asked
    anything, and the port answers for itself."""
    config = com_config(tmp_path)
    service = AgenticHILToolService(config)
    _install_serial(monkeypatch, _FailingHandle())
    try:
        assert service.call("com_session_start", {"port_id": PORT_ID})["ok"] is False

        _install_serial(monkeypatch, _WorkingHandle())
        opened = service.call("com_session_start", {"port_id": PORT_ID})

        assert opened["ok"] is True, opened
        assert service.call("com_session_stop", {"port_id": PORT_ID})["ok"] is True
    finally:
        service.close()


def test_the_next_open_failing_is_its_own_refusal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """And the other half of the same argument: a handle that really is stuck
    keeps being refused, by the operating system, without anybody having had to
    predict it."""
    config = com_config(tmp_path)
    service = AgenticHILToolService(config)
    _install_serial(monkeypatch, _FailingHandle())
    try:
        first = service.call("com_session_start", {"port_id": PORT_ID})
        second = service.call("com_session_start", {"port_id": PORT_ID})

        assert first["ok"] is False
        assert second["ok"] is False, second
        assert second.get("error_type") != "resource_quarantined"
        assert second["cleanup_reasons"] == ["com_open_cleanup_unconfirmed"]
    finally:
        service.close()


def test_a_stop_whose_cleanup_did_not_confirm_records_it_on_the_lease(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The teardown half, at the service that owns it. The same reason is
    recorded and the same failure is raised; what does not happen is the
    incident."""
    config = com_config(tmp_path)
    coordinator = HardwareCoordinator(config, "com-stop")
    service = ComPortService(config, coordinator)
    _install_serial(monkeypatch, _WorkingHandle())
    try:
        assert service.session_start(PORT_ID)["ok"] is True
        session = service.sessions[PORT_ID]
        monkeypatch.setattr(session.serial_handle, "close", lambda: (_ for _ in ()).throw(OSError("close refused")))

        stopped = service.session_stop(PORT_ID)

        assert stopped["ok"] is False, stopped
        assert stopped["error_type"] == "com_port_close_failed"
        assert session.lease.reported_cleanup_reasons() == ["com_cleanup_unconfirmed"]
        assert session.lease.cleanup_reasons() == []
        assert coordinator.blocked is False
    finally:
        coordinator.close()


# ---------------------------------------------------------------------------
# D. The dead owner.


def leave_dead_owner(workspace: Path, **config_kwargs):
    """A project record from a process that died with a session it had opened.

    The contact marker is what stops `status()` releasing it as provably
    innocent, so the incident is real and a recovery action is what answers it.
    """
    config = config_for(workspace, **config_kwargs)
    setup = HardwareCoordinator(config, "dead-owner-setup")
    record = setup._base_record("active", [DEAD_OWNER_RESOURCE])
    record["leases"] = [
        {
            "lease_id": "dead-owner-lease",
            "resources": [DEAD_OWNER_RESOURCE],
            "lease_state": "active",
            "safe_state_confirmed": False,
            "processes_reaped": False,
            "audit_ok": True,
            "cleanup_required": False,
            "quarantined": False,
            "cleanup_reasons": [],
            "quarantine_id": None,
        }
    ]
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
            CONTACT_MARKER_KEY: "2026-08-11T09:00:00Z",
            CONTACT_MARKER_SOURCE_KEY: "serial_open",
        },
    )
    marker = setup._base_record("active", [DEAD_OWNER_RESOURCE])
    marker["lease_id"] = "dead-owner-lease"
    setup._write_record(DEAD_OWNER_RESOURCE, marker)
    setup._write_record(setup.project_key, record)
    return config


def test_a_dead_owner_is_still_reaped_and_recovered(tmp_path: Path) -> None:
    """Unchanged, and asserted first, because the removal of the hold is only
    defensible while the thing that used to lift it still runs."""
    config = leave_dead_owner(tmp_path, auto_recover="reset_halt")
    backend = FakeBackend()
    service = AgenticHILToolService(config, backend=backend)
    try:
        assert service.coordinator.status()["cleanup_reasons"] == [DEAD_OWNER_REASON]

        recovery = service.recover_after_failed_run(["dut"])

        assert recovery["outcome"] == "recovered", recovery
        assert recovery["incident_resolved"] is True, recovery
        assert backend.calls == ["reset_target:halt", "probe_target"]
    finally:
        service.close()

    assert [line["attestation"] for line in ledger(config)] == [ATTESTATION_RECOVERY_ACTION]


def test_a_dead_owner_whose_recovery_cannot_run_leaves_no_hold(tmp_path: Path) -> None:
    """`auto_recover: off` is a supported setting and it is the clearest form of
    "recovery cannot run": the bench takes no action, so there is nothing to
    attest, and holding the devices for an answer nobody is going to give is
    what #216 removed."""
    config = leave_dead_owner(tmp_path, auto_recover="off")
    service = AgenticHILToolService(config, backend=FakeBackend())
    try:
        assert service.coordinator.status()["cleanup_reasons"] == [DEAD_OWNER_REASON]

        result = service.call("probe_target", {})

        assert result["ok"] is True, result
        assert result["incident_stood_down"]["reasons"] == [DEAD_OWNER_REASON]
        assert service.coordinator.blocked is False
        assert service.coordinator.status()["blocked"] is False
    finally:
        service.close()

    stood_down = [line for line in ledger(config) if line.get("recovery") == "incident_stood_down"]
    assert [line["attestation"] for line in stood_down] == [ATTESTATION_NO_STANDING_STATE]


def test_the_seam_runs_the_recovery_action_before_it_ends_an_incident(tmp_path: Path) -> None:
    """The order the whole design turns on. An incident is not ended and then
    recovered from: the recovery action gets its turn first, and only what it
    could not settle stands down. Held on a call with no run teardown behind it,
    so the attempt this asserts is the seam's own."""
    config = leave_dead_owner(tmp_path, auto_recover="reset_halt")
    backend = FakeBackend()
    service = AgenticHILToolService(config, backend=backend)
    try:
        assert service.coordinator.status()["cleanup_reasons"] == [DEAD_OWNER_REASON]

        service.call("bench_run_status")

        assert backend.calls == ["reset_target:halt", "probe_target"]
        assert service.coordinator.blocked is False
    finally:
        service.close()

    lines = ledger(config)
    assert [line["attestation"] for line in lines] == [ATTESTATION_RECOVERY_ACTION]
    assert lines[0]["reason"] == DEAD_OWNER_REASON


# ---------------------------------------------------------------------------
# E. Ledger fidelity.


def test_removing_the_gate_removed_no_written_evidence(tmp_path: Path) -> None:
    """One representative ex-quarantine path, held field by field against what
    the gate era wrote.

    The point of the assertion is what is *not* in the difference. A confirmed
    recovery of an unconfirmed reset writes the same line it always wrote: same
    event, same recovery kind, same reason, same actor, same route, same
    attestation, both configuration digests, and the resources it names. The
    narrowing took the standing state and nothing else, so an audit of a bench
    that recovered itself reads exactly as it did before."""
    config = config_for(tmp_path, auto_recover="reset_halt")
    service = AgenticHILToolService(config, backend=FakeBackend())
    try:
        lease = service.coordinator.acquire(*debugger_effect_resources(config))
        lease.quarantine(TARGET_REASON)
        service._quarantined_lease = lease
        incident = service.coordinator.quarantine_id

        assert service.recover_after_failed_run(["dut"])["incident_resolved"] is True
    finally:
        service.close()

    lines = ledger(config)
    assert len(lines) == 1, lines
    line = lines[0]
    assert line["event"] == "recovery"
    assert line["recovery"] == "incident_resolved"
    assert line["reason"] == TARGET_REASON
    assert line["actor"] == RECOVERY_ACTOR_SERVER
    assert line["via"] == RECOVERY_ACTION_VIA
    assert line["attestation"] == ATTESTATION_RECOVERY_ACTION
    assert line["quarantine_id"] == incident
    assert line["recorded_config_sha256"] == line["current_config_sha256"]
    assert line["resumed"] is False
    assert line["resources"]
    assert "time" in line


def test_an_incident_that_ends_unattested_says_so_rather_than_going_quiet(tmp_path: Path) -> None:
    """The one line the narrowing added, and the reason it had to be added: an
    incident that stopped blocking the bench without anybody attesting anything
    is still an event, and a ledger that recorded the attested endings and not
    this one could not be audited for what actually happened."""
    config = config_for(tmp_path, auto_recover="off")
    service = AgenticHILToolService(config, backend=FakeBackend(reset_ok=False))
    try:
        result = service.call("reset_target", {"mode": "run"})
        assert result["incident_stood_down"]["reasons"] == [TARGET_REASON]
        incident = result["incident_stood_down"]["quarantine_id"]
    finally:
        service.close()

    lines = ledger(config)
    assert len(lines) == 1, lines
    line = lines[0]
    assert line["event"] == "recovery"
    assert line["recovery"] == "incident_stood_down"
    assert line["reasons"] == [TARGET_REASON]
    assert line["actor"] == RECOVERY_ACTOR_SERVER
    assert line["attestation"] == ATTESTATION_NO_STANDING_STATE
    assert line["via"].startswith("coordination:")
    assert line["quarantine_id"] == incident
    assert line["recorded_config_sha256"] == line["current_config_sha256"]
