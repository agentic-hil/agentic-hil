"""A recovery action drives only a probe its own call holds.

A workspace's previous server can exit with an incident of its own open. The
next call in that workspace adopts the incident on its way into `acquire`,
before it takes the per-resource locks and the bench hold, and the recovery
action then settles it: a reset into halt and a re-read of the probe, at the end
of the call or in the teardown of the single-action run the call declared. When
the call was refused further in, because another workspace on the same probe
holds it through a run, a call in flight or an incident, the call held nothing,
and the recovery drove the shared probe all the same, with no lock and no bench
hold. The other workspace's target was reset and halted between its calls, and
this workspace recorded that reset as a machine-attested recovery of its own
incident.

A recovery takes the locks and the bench hold on the probe it drives, the way a
call that drives the probe takes them, and is refused the same way. A refused
recovery drives nothing and attests nothing. The incident it could not settle
ends the way an incident ends when the policy withholds the recovery: it is
stood down at the end of the call, and the next contact establishes what state
the target is in.

One test per case: what the other workspace has on the probe, and the call made
here. The backend stands in for the shared probe, and each test reads off it
what was driven. The controls at the end meet the same leftover incident with
the probe free, where the recovery still resets, re-reads and settles it.
"""

from __future__ import annotations

import json
from contextlib import suppress
from pathlib import Path
from typing import Any

import pytest
from conftest import DEFAULT_TEST_PERMISSIONS
from test_config_adopt import _RecoveryBackend, _set_auto_recover, _timed_out_read, placeholder_bench
from test_cross_project_quarantine import config_for, dead_owner_incident, debugger_effects, foreign_incident
from test_dead_owner_no_contact import dead_owner_record

from agentic_hil.adopt import PROJECT_CONFIG_ADOPT
from agentic_hil.config import load_authoritative_config
from agentic_hil.coordination import ATTESTATION_RECOVERY_ACTION, HardwareCoordinator
from agentic_hil.knowledge import CONFIG_DESCRIPTION_RIGHT
from agentic_hil.tools import AgenticHILToolService

# The resource the previous server's incident names. Not the shared probe: the
# other workspace could not have taken a probe this workspace's incident is
# marked on, and the recovery drives the probe whichever resource it names.
OWN_INCIDENT_RESOURCE = "physical:own-incident"
# An image for the calls that name one. None of them gets as far as reading it.
IMAGE = {"image_path": "build/firmware.elf"}


class _ListingBackend(_RecoveryBackend):
    """The same probe double, able to answer a probe listing as well."""

    def list_probes(self) -> dict:
        self.calls.append("list_probes")
        return {"ok": True, "tool": "debugger_probes_list", "probes": []}


def neighbour_workspace(tmp_path: Path) -> HardwareCoordinator:
    """The other workspace on the same probe, alive for the whole test."""
    return HardwareCoordinator(config_for(tmp_path / "neighbour-workspace"), "neighbour-workspace")


def this_workspace(
    tmp_path: Path, backend: _RecoveryBackend | None = None, **config_kwargs: Any
) -> tuple[AgenticHILToolService, _RecoveryBackend, str]:
    """This workspace's server, started over the incident its previous server left open.

    Returns the service, the double standing in for the shared probe, and the
    id of the leftover incident."""
    config = config_for(tmp_path / "this-workspace", **config_kwargs)
    dead_owner_incident(config)
    probe = backend or _RecoveryBackend()
    service = AgenticHILToolService(config, backend=probe, frontend="mcp")
    own = service.coordinator.status()
    assert own["blocked"] is True, own
    return service, probe, str(own["quarantine_id"])


def recovery_ledger(service: AgenticHILToolService) -> list[dict[str, Any]]:
    path = Path(service.coordinator.root) / "recovery.jsonl"
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def attested_recoveries(service: AgenticHILToolService) -> list[dict[str, Any]]:
    return [line for line in recovery_ledger(service) if line.get("attestation") == ATTESTATION_RECOVERY_ACTION]


def assert_the_probe_was_left_alone(
    service: AgenticHILToolService, probe: _RecoveryBackend, result: dict[str, Any], own_id: str
) -> None:
    """Nothing reached the shared probe, and nothing says it was recovered.

    The incident that could not be recovered is stood down at the end of the
    call, as it is when the policy withholds the recovery."""
    assert probe.calls == [], probe.calls
    recovery = result.get("recovery")
    assert not (isinstance(recovery, dict) and recovery.get("incident_resolved") is True), result
    assert attested_recoveries(service) == [], recovery_ledger(service)
    assert result["incident_stood_down"]["quarantine_id"] == own_id, result
    assert service.coordinator.status()["blocked"] is False


def test_a_neighbour_s_declared_run_keeps_the_probe_from_this_workspace_s_recovery(tmp_path: Path) -> None:
    """The other workspace holds the probe through a declared run.

    `debug_start_session` here adopts the leftover incident and is then refused
    by the bench hold. A recovery at the end of the call would reset and halt the
    board under that run, between two of its steps."""
    neighbour = neighbour_workspace(tmp_path)
    neighbour.begin_run(debugger_effects(neighbour), label="neighbour-run")
    service, probe, own_id = this_workspace(tmp_path)
    try:
        result = service.call("debug_start_session", IMAGE)

        assert result["error_type"] == "device_busy", result
        assert_the_probe_was_left_alone(service, probe, result, own_id)
    finally:
        service.close()
        neighbour.end_run()
        neighbour.close()


def test_a_neighbour_s_call_in_flight_keeps_the_probe_from_this_workspace_s_recovery(tmp_path: Path) -> None:
    """The other workspace holds the probe through the lease of a call it is making."""
    neighbour = neighbour_workspace(tmp_path)
    lease = neighbour.acquire(*debugger_effects(neighbour))
    service, probe, own_id = this_workspace(tmp_path)
    try:
        result = service.call("debug_start_session", IMAGE)

        assert result["error_type"] == "resource_busy", result
        assert_the_probe_was_left_alone(service, probe, result, own_id)
    finally:
        service.close()
        lease.release()
        neighbour.close()


def test_a_neighbour_s_incident_whose_owner_is_alive_keeps_the_probe_from_this_workspace_s_recovery(
    tmp_path: Path,
) -> None:
    """The other workspace's incident holds the probe, and its owner still holds the locks."""
    owner, _ = foreign_incident(tmp_path, keep_open=True)
    service, probe, own_id = this_workspace(tmp_path)
    try:
        result = service.call("debugger_probes_list")

        assert result["error_type"] == "resource_busy", result
        assert_the_probe_was_left_alone(service, probe, result, own_id)
    finally:
        service.close()
        with suppress(Exception):
            owner.close()


@pytest.mark.parametrize("auto_recover", [None, "readonly"], ids=["reset_halt", "readonly"])
def test_a_neighbour_s_incident_whose_owner_exited_keeps_the_probe_from_this_workspace_s_run_teardown(
    tmp_path: Path, auto_recover: str | None
) -> None:
    """The other workspace's incident holds the probe, and its owner is gone.

    `probe_target` declares its own single-action run and is refused for the
    other workspace's incident, which still stands on the probe and still
    refuses the next call. The run's teardown is where the recovery would run.
    Under `readonly` it re-reads the probe without the reset, and a re-read is a
    connection to that board all the same. The teardown's block says why it did
    nothing, and the incident it could not recover is stood down after it."""
    foreign_incident(tmp_path)
    service, probe, own_id = this_workspace(tmp_path, auto_recover=auto_recover)
    try:
        result = service.call("probe_target")

        assert result["error_type"] == "resource_quarantined", result
        assert_the_probe_was_left_alone(service, probe, result, own_id)
        assert result["recovery"]["attempted"] is False, result
        assert result["recovery"]["actions"] == [], result
        assert result["recovery"]["reason_not_attempted"] == "probe_held_elsewhere", result
    finally:
        service.close()


def test_a_neighbour_s_incident_whose_owner_exited_keeps_the_probe_from_this_workspace_s_adoption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The configuration write that reads the board, refused for the same incident."""
    workspace, path = placeholder_bench(
        tmp_path, monkeypatch, permissions=DEFAULT_TEST_PERMISSIONS, **{CONFIG_DESCRIPTION_RIGHT: True}
    )
    _set_auto_recover(path, "reset_halt")
    dead_owner_incident(load_authoritative_config(workspace))
    foreign_incident(tmp_path)
    monkeypatch.setattr("agentic_hil.adopt.discover_attached_hardware", _timed_out_read())
    probe = _RecoveryBackend()
    tools = AgenticHILToolService(load_authoritative_config(workspace), backend=probe, frontend="mcp")
    try:
        own = tools.coordinator.status()
        assert own["blocked"] is True, own
        refused = tools.call(PROJECT_CONFIG_ADOPT, {"apply": True})

        assert refused["error_type"] == "resource_quarantined", refused
        assert_the_probe_was_left_alone(tools, probe, refused, str(own["quarantine_id"]))
    finally:
        tools.close()


def test_an_incident_open_before_the_call_is_not_recovered_through_a_probe_a_neighbour_holds(tmp_path: Path) -> None:
    """The recovery a call attempts first, for an incident open before it starts.

    This workspace's previous server died inside a call, and the status read
    this server made adopted what it left. The next hardware call attempts the
    recovery before anything else, before its own lease is refused."""
    neighbour = neighbour_workspace(tmp_path)
    lease = neighbour.acquire(*debugger_effects(neighbour))
    config = config_for(tmp_path / "this-workspace")
    previous = HardwareCoordinator(config, "previous-server")
    record = dead_owner_record(previous, resources=[OWN_INCIDENT_RESOURCE])
    marker = previous._base_record("active", [OWN_INCIDENT_RESOURCE])
    marker["lease_id"] = record["leases"][0]["lease_id"]
    previous._write_record(OWN_INCIDENT_RESOURCE, marker)
    previous._write_record(previous.project_key, record)
    probe = _RecoveryBackend()
    service = AgenticHILToolService(config, backend=probe, frontend="mcp")
    try:
        service.coordinator.status()
        assert service.coordinator.blocked is True
        own_id = str(service.coordinator.quarantine_id)

        result = service.call("debug_start_session", IMAGE)

        assert result["error_type"] == "resource_busy", result
        assert_the_probe_was_left_alone(service, probe, result, own_id)
    finally:
        service.close()
        lease.release()
        neighbour.close()


@pytest.mark.parametrize("tool", ["probe_target", "flash_firmware"])
def test_a_call_the_neighbour_s_run_refuses_before_it_adopts_anything_drives_nothing(tmp_path: Path, tool: str) -> None:
    """The one case that never drove the probe, and must not start to.

    The single-action run is refused at `begin_run`, before the call reaches
    `acquire`, so the leftover incident is not adopted and nothing ends it."""
    neighbour = neighbour_workspace(tmp_path)
    neighbour.begin_run(debugger_effects(neighbour), label="neighbour-run")
    service, probe, _ = this_workspace(tmp_path)
    try:
        result = service.call(tool, IMAGE if tool == "flash_firmware" else None)

        assert result["error_type"] == "device_busy", result
        assert probe.calls == [], probe.calls
    finally:
        service.close()
        neighbour.end_run()
        neighbour.close()


def test_the_leftover_incident_on_a_free_probe_is_still_recovered_by_the_run_teardown(tmp_path: Path) -> None:
    """The control for the teardown: the probe is free, so the recovery takes it."""
    service, probe, own_id = this_workspace(tmp_path)
    try:
        result = service.call("probe_target")

        assert result["ok"] is True, result
        assert probe.calls == ["probe_target", "reset_target:halt", "probe_target"], probe.calls
        assert result["recovery"]["incident_resolved"] is True, result
        assert result["recovery"]["resolved_quarantine_id"] == own_id, result
        assert "incident_stood_down" not in result, result
        assert attested_recoveries(service), recovery_ledger(service)
        assert service.coordinator.status()["blocked"] is False
    finally:
        service.close()


def test_the_leftover_incident_on_a_free_probe_is_still_recovered_at_the_end_of_the_call(tmp_path: Path) -> None:
    """The control for the end of a call that declares no run."""
    service, probe, _ = this_workspace(tmp_path, backend=_ListingBackend())
    try:
        result = service.call("debugger_probes_list")

        assert result["ok"] is True, result
        assert probe.calls == ["list_probes", "reset_target:halt", "probe_target"], probe.calls
        assert "incident_stood_down" not in result, result
        assert attested_recoveries(service), recovery_ledger(service)
        assert service.coordinator.status()["blocked"] is False
    finally:
        service.close()
