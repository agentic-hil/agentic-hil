"""A lease whose incident a recovery ended goes back, and a refusal names only what holds the bench.

An adoption read that times out quarantines the probe, and when the lease it
was read under cannot be recorded as released, a `lease_release_unconfirmed`
incident holds the bench with that lease registered under it. Ending that
incident sets the lease back to `active`. The end of a call then gave it back
only after an ending it ran itself, so a recovery anywhere else, the one at the
start of the next hardware call or the one a recovery-class call settles, left
the lease registered with nothing left to give it back. Every configuration
call after that was refused as if a run or a session held the bench, and the
advice to stop them freed nothing, until the server restarted.

The second half is what such a refusal says while the lease is still held under
its incident: there is no run and no session to stop, so the refusal may not
send the caller to stop one.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import DEFAULT_TEST_PERMISSIONS
from test_config_adopt import (
    PROBE_SERIAL,
    _RecoveryBackend,
    _set_auto_recover,
    _timed_out_read,
    attached,
    document_of,
    placeholder_bench,
)
from test_config_write import SESSION_STOP_CALLS

from agentic_hil.adopt import PROJECT_CONFIG_ADOPT
from agentic_hil.config import load_authoritative_config
from agentic_hil.configreload import PROJECT_CONFIG_RELOAD
from agentic_hil.configwrite import PROJECT_CONFIG_DESCRIBE, PROJECT_CONFIG_SET
from agentic_hil.coordination import LEASE_RELEASE_RETRY_REASON
from agentic_hil.knowledge import CONFIG_DESCRIPTION_RIGHT, CONFIG_WRITE_RIGHT
from agentic_hil.tools import PROJECT_CONFIG_CREATE, AgenticHILToolService


def fail_the_first_releases(service: AgenticHILToolService, monkeypatch: pytest.MonkeyPatch, count: int) -> None:
    """Make the first ``count`` lease releases of this service fail to persist, and no other."""
    original_persist = service.coordinator._persist_lease
    failures = [OSError("injected lease-release persistence failure") for _ in range(count)]

    def failing_persist(lease, state=None, incident_override=None):
        if state == "released" and failures:
            raise failures.pop()
        return original_persist(lease, state=state, incident_override=incident_override)

    monkeypatch.setattr(service.coordinator, "_persist_lease", failing_persist)


def recovery_bench(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, policy: str | None, **grants: bool) -> tuple[Path, AgenticHILToolService]:
    """The placeholder bench, under ``policy`` or the default one, on a probe double a recovery can drive."""
    workspace, path = placeholder_bench(tmp_path, monkeypatch, permissions=DEFAULT_TEST_PERMISSIONS, **{CONFIG_DESCRIPTION_RIGHT: True, **grants})
    if policy is not None:
        _set_auto_recover(path, policy)
    return path, AgenticHILToolService(load_authoritative_config(workspace), backend=_RecoveryBackend(), frontend="mcp")


def timed_out_bench(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, policy: str | None, **grants: bool) -> tuple[Path, AgenticHILToolService]:
    """The same bench with a board whose first read is reaped mid-attach."""
    monkeypatch.setattr("agentic_hil.adopt.discover_attached_hardware", _timed_out_read())
    return recovery_bench(tmp_path, monkeypatch, policy, **grants)


@pytest.mark.parametrize(
    ("policy", "failed_releases"),
    [(None, 1), ("readonly", 1), ("reset_halt", 2)],
    ids=["default_policy", "readonly", "reset_halt_twice"],
)
def test_an_adoption_after_a_lease_that_could_not_be_given_back_is_not_refused_for_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, policy: str | None, failed_releases: int) -> None:
    """The read's lease goes back once the incident holding it ends, whichever recovery ended it.

    The first adoption times out and its lease cannot be given back. Under
    `readonly`, and under `reset_halt` when the lease fails twice, the bench is
    still held under `lease_release_unconfirmed` when that call returns, and the
    next adoption's own recovery re-reads the probe and ends the incident first.
    Under the default policy the end of the first call's recovery ends it. The
    board answers the second read, and nothing may refuse that adoption as a
    configuration write inside a run: no run was declared, no session was
    opened, and the only lease was the one the read took."""
    path, tools = timed_out_bench(tmp_path, monkeypatch, policy)
    fail_the_first_releases(tools, monkeypatch, failed_releases)
    try:
        first = tools.call(PROJECT_CONFIG_ADOPT, {"apply": True})
        assert first["ok"] is False, first
        assert first["error_type"] == "resource_quarantined", first

        attached(monkeypatch)
        adopted = tools.call(PROJECT_CONFIG_ADOPT, {"apply": True})

        assert adopted["ok"] is True, adopted
        assert adopted["applied"] is True, adopted
        assert tools.coordinator.blocked is False
        assert tools.open_hardware_holds() is None, tools.open_hardware_holds()
    finally:
        tools.close()
    assert document_of(path)["debuggers"]["dut"]["probe_id"] == PROBE_SERIAL


def test_a_probe_that_ends_the_incident_gives_back_the_lease_left_under_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The recovery-class half: a `probe_target` that ends the incident gives the lease back too.

    A lease a call took on the COM port cannot be recorded as released, so it
    stays registered under a `lease_release_unconfirmed` incident. With
    `recovery.auto_recover: off` nothing is driven on the way into a call, and
    the agent asks for the re-read itself: `probe_target` detects the target and
    ends the incident, and the lease left under it goes back with it. No session
    holds that lease, and none is left to give it back later."""
    _, tools = recovery_bench(tmp_path, monkeypatch, "off")
    try:
        lease = tools.coordinator.acquire("com:COM9")
        fail_the_first_releases(tools, monkeypatch, 1)
        assert lease.release() is False
        assert tools.coordinator.blocked is True, tools.coordinator.status()
        assert LEASE_RELEASE_RETRY_REASON in tools.coordinator.status()["cleanup_reasons"]

        probed = tools.call("probe_target")

        assert probed["ok"] is True, probed
        assert probed.get("incident_resolved") is True, probed
        assert tools.coordinator.blocked is False
        assert tools.open_hardware_holds() is None, tools.open_hardware_holds()
    finally:
        tools.close()


def test_an_adoption_refused_for_a_lease_held_under_an_incident_names_no_run_and_no_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """What the refusal says while the read's lease is still held under its incident.

    With `recovery.auto_recover: off` the second adoption meets the lease the
    first read could not give back, registered under the incident that holds
    it. No run is open and no session holds it, so neither `bench_run_stop` nor
    a session stop frees it, and the refusal may not advise either. It names
    the lease, says it goes back when its incident ends, and points at
    `agentic-hil lease-status`, which names that incident. The end of the
    refused call stands the incident down and gives the lease back, so the
    adoption after it reads the board."""
    path, tools = timed_out_bench(tmp_path, monkeypatch, "off")
    fail_the_first_releases(tools, monkeypatch, 1)
    try:
        first = tools.call(PROJECT_CONFIG_ADOPT, {"apply": True})
        assert first["ok"] is False, first
        held = sorted(tools.coordinator.leases)
        assert held, tools.coordinator.status()

        attached(monkeypatch)
        refused = tools.call(PROJECT_CONFIG_ADOPT, {"apply": True})

        assert refused["error_type"] == "config_write_in_open_run", refused
        holds = refused["open_holds"]
        assert holds["run_active"] is False, holds
        assert holds["open_leases"] == held, holds
        assert holds["leases_under_incident"] == held, holds
        next_step = refused["next_step"]
        for call in ("bench_run_stop", *SESSION_STOP_CALLS):
            assert call not in next_step, (call, next_step)
        assert "COM or CAN session" not in next_step, next_step
        assert "agentic-hil lease-status" in next_step, next_step
        for lease_id in held:
            assert lease_id in next_step, (lease_id, next_step)

        assert tools.open_hardware_holds() is None, tools.open_hardware_holds()
        assert tools.call(PROJECT_CONFIG_ADOPT, {"apply": True})["ok"] is True
    finally:
        tools.close()
    assert document_of(path)["debuggers"]["dut"]["probe_id"] == PROBE_SERIAL


def test_describe_names_a_lease_held_under_an_incident_and_no_run_or_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`project_config_describe` reads the same holds and says the same thing about them.

    Its step for what is held named a run or a session and the call that ends
    each; for a lease held under an incident it names the lease, the incident
    it waits for and `agentic-hil lease-status`, and no call that stops a run or
    a session."""
    _, tools = timed_out_bench(tmp_path, monkeypatch, "off")
    fail_the_first_releases(tools, monkeypatch, 1)
    try:
        assert tools.call(PROJECT_CONFIG_ADOPT, {"apply": True})["ok"] is False
        held = sorted(tools.coordinator.leases)
        assert held, tools.coordinator.status()

        described = tools.call(PROJECT_CONFIG_DESCRIBE)

        assert described["writes_blocked_by_open_run"] is True, described
        assert described["open_holds"]["leases_under_incident"] == held, described["open_holds"]
        steps = described["next_steps"]
        for call in ("bench_run_stop", *SESSION_STOP_CALLS):
            assert not any(call in step for step in steps), (call, steps)
        assert not any("run or session is holding" in step for step in steps), steps
        hold_steps = [step for step in steps if "agentic-hil lease-status" in step and all(lease_id in step for lease_id in held)]
        assert len(hold_steps) == 1, steps
    finally:
        tools.close()


@pytest.mark.parametrize(
    ("tool", "arguments", "error_type"),
    [
        (PROJECT_CONFIG_SET, {"changes": [{"key": "debuggers.dut.permissions.allow_mass_erase", "value": False}]}, "config_write_in_open_run"),
        (PROJECT_CONFIG_CREATE, {}, "config_write_in_open_run"),
        (PROJECT_CONFIG_RELOAD, {}, "config_reload_in_open_run"),
    ],
    ids=["set", "create", "reload"],
)
def test_the_other_configuration_refusals_say_what_ends_a_lease_held_under_an_incident(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tool: str, arguments: dict[str, object], error_type: str
) -> None:
    """`project_config_set`, `project_config_create` and `project_config_reload_description` read the same holds.

    Each refuses while the read's lease is still held under its incident, as the
    adoption does. Their advice is the catalogue's, which named only a run and a
    session to close, and neither frees this lease: the holds name it as held
    under an incident, and the remediation says what ends that hold and where the
    incident is named. The end of the refused call gives the lease back."""
    _, tools = timed_out_bench(tmp_path, monkeypatch, "off", **{CONFIG_WRITE_RIGHT: True})
    fail_the_first_releases(tools, monkeypatch, 1)
    try:
        assert tools.call(PROJECT_CONFIG_ADOPT, {"apply": True})["ok"] is False
        held = sorted(tools.coordinator.leases)
        assert held, tools.coordinator.status()

        refused = tools.call(tool, arguments)

        assert refused["error_type"] == error_type, refused
        holds = refused["open_holds"]
        assert holds["run_active"] is False, holds
        assert holds["leases_under_incident"] == held, holds
        steps = [step for step in refused["remediation"] if "leases_under_incident" in step]
        assert len(steps) == 1, refused["remediation"]
        assert "agentic-hil lease-status" in steps[0], steps
        assert tools.open_hardware_holds() is None, tools.open_hardware_holds()
    finally:
        tools.close()
