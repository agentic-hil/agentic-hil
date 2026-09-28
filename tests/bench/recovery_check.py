"""Opt-in status-gated reset-halt and probe before the standard bench tier."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

from agentic_hil.backends.common import find_stm32_programmer_cli
from agentic_hil.bootstrap import load_project_profile, profile_target_controller
from agentic_hil.report import overall_success
from tests.bench.conftest import BENCH_ONLY, DEMO, Bench
from tests.bench.pyocd_recordings import (
    capture_run_stop_with_lease_evidence,
    lease_status_evidence,
    pyocd_result_evidence,
    redact,
    redact_values,
    run_closure_succeeded,
)
from tests.bench.test_bench_faults import Server

pytestmark = [pytest.mark.bench, BENCH_ONLY]

TARGET_CONTROLLER = "stm32f446ret6"
RECOVERY_RECORDING_SCHEMA = "agentic-hil.recovery-check/v1"


class RecoveryCheckRefused(RuntimeError):
    """A required status or safe recovery condition was not established."""

    def __init__(self, message: str, evidence: dict | None = None) -> None:
        super().__init__(message)
        self.evidence = evidence or {}


def guarded_bench(request, *, cubeprogrammer_present: bool, profile_controller: str | None):
    """Evaluate no-contact prerequisites before lazily requesting the bench fixture."""
    if cubeprogrammer_present:
        raise RecoveryCheckRefused("the recovery check requires the default image without STM32CubeProgrammer")
    if profile_controller != TARGET_CONTROLLER:
        raise RecoveryCheckRefused("the shipped profile does not name the known STM32F446RET6 controller")
    return request.getfixturevalue("bench")


@pytest.fixture
def recovery_check_bench(request):
    # These checks intentionally happen before request.getfixturevalue("bench"):
    # the fixture's init path uses the shipped profile to avoid target contact,
    # while CubeProgrammer discovery would connect to ask the device its part.
    profile = load_project_profile(DEMO)
    controller = profile_target_controller(profile)
    cubeprogrammer_present = find_stm32_programmer_cli() is not None
    return guarded_bench(
        request,
        cubeprogrammer_present=cubeprogrammer_present,
        profile_controller=controller,
    )


def recovery_status_refusal(status: object) -> str | None:
    """Refuse before reset unless machine-wide standing/audit state is clear."""
    if not isinstance(status, dict) or status.get("ok") is not True:
        return "hardware_lease_status was unavailable or refused"
    if status.get("audit_ok") is False or status.get("audit_broken") is True:
        return "hardware_lease_status did not confirm an intact audit trail"
    if status.get("incident_stands") is not False:
        return "the current workspace has a standing or unconfirmed incident"
    standing = status.get("standing_incidents")
    if not isinstance(standing, list):
        return "hardware_lease_status did not provide the machine-wide standing incident list"
    if standing:
        return "another workspace has a standing machine-wide incident"
    return None


def run_recovery_check(server: Server, debugger_id: str, bench: Bench, private_values: tuple[str, ...]) -> dict:
    """Read status, then use one declared MCP run for reset-halt and probe only."""
    try:
        _, initial_status = server.call("hardware_lease_status")
    except Exception as error:
        evidence = {
            "lease_status_before": lease_status_evidence(None, private_values),
            "status_call_error": redact(f"{type(error).__name__}: {error}", private_values),
        }
        raise RecoveryCheckRefused("hardware_lease_status call failed", evidence) from error
    initial_evidence = lease_status_evidence(initial_status, private_values)
    evidence: dict = {"lease_status_before": initial_evidence}
    refusal = recovery_status_refusal(initial_status)
    if refusal is not None:
        raise RecoveryCheckRefused(f"{refusal}: {initial_evidence}", evidence)

    try:
        _, opened = server.call(
            "bench_run_start",
            {"devices": [{"kind": "debugger", "id": debugger_id}], "label": "incident-recovery-check"},
        )
    except Exception as error:
        evidence["run_start_error"] = redact(f"{type(error).__name__}: {error}", private_values)
        raise RecoveryCheckRefused("bench_run_start call failed", evidence) from error
    evidence["run_start"] = redact_values(opened, private_values)
    if not overall_success(opened):
        raise RecoveryCheckRefused(f"bench_run_start failed its continue predicate: {opened}", evidence)

    failure: RecoveryCheckRefused | None = None
    try:
        _, reset_result = server.call("reset_target", {"mode": "halt"})
        reset_evidence = pyocd_result_evidence(bench, reset_result, private_values)
        evidence["reset_result"] = reset_evidence
        if not overall_success(reset_result):
            failure = RecoveryCheckRefused("reset_target(mode=halt) failed its continue predicate", evidence)
        else:
            _, probe_result = server.call("probe_target")
            probe_evidence = pyocd_result_evidence(bench, probe_result, private_values)
            probe_evidence["result"]["target_detected"] = probe_result.get("target_detected")
            evidence["probe_result"] = probe_evidence
            if not overall_success(probe_result) or probe_result.get("target_detected") is not True:
                failure = RecoveryCheckRefused("probe_target did not confirm the target after reset-halt", evidence)
    except Exception as error:
        evidence["mcp_error"] = redact(f"{type(error).__name__}: {error}", private_values)
        failure = RecoveryCheckRefused("MCP recovery call failed", evidence)
    finally:
        closure = capture_run_stop_with_lease_evidence(server, private_values)
        evidence["closure"] = closure

    if failure is not None:
        raise RecoveryCheckRefused(str(failure), evidence)
    if not run_closure_succeeded(closure):
        raise RecoveryCheckRefused("bench_run_stop or post-stop lease status did not confirm a clean closure", evidence)
    return evidence


def test_target_incident_recovery_check(recovery_check_bench: Bench, tmp_path: Path, record_property) -> None:
    """Confirm the current lease state, then establish a known target state through MCP."""
    bench = recovery_check_bench
    debugger_id = bench.debugger_name()
    document = bench.configuration()
    debugger = document.get("debuggers", {}).get(debugger_id, {})
    config = document.get("com_ports", {})
    private_values = tuple(
        value
        for value in (
            str(debugger.get("probe_id") or ""),
            *(str(entry.get("device") or "") for entry in config.values() if isinstance(entry, dict)),
            str(bench.project),
            bench.project.as_posix(),
            str(bench.config_root),
            bench.config_root.as_posix(),
            str(bench.state_root),
            bench.state_root.as_posix(),
            str(Path.home()),
            Path.home().as_posix(),
        )
        if value
    )
    source_commit = os.environ.get("AGENTIC_HIL_BENCH_COMMIT")
    run_id = os.environ.get("AGENTIC_HIL_BENCH_RUN_ID")
    if source_commit is not None:
        assert re.fullmatch(r"[0-9a-f]{40}", source_commit), source_commit
    if run_id is not None:
        assert re.fullmatch(r"[A-Za-z0-9_.-]+", run_id), run_id

    server = Server(bench, tmp_path / "recovery-check-mcp.stderr")
    server.greet()
    evidence: dict = {}
    try:
        try:
            evidence = run_recovery_check(server, debugger_id, bench, private_values)
        except RecoveryCheckRefused as error:
            evidence = error.evidence
            evidence["outcome"] = "refused"
            evidence["reason"] = redact(str(error), private_values)
            record_property("agentic_hil_recovery_check_v1", _record_json(source_commit, run_id, evidence))
            raise
    finally:
        server.close()
    record_property("agentic_hil_recovery_check_v1", _record_json(source_commit, run_id, evidence))


def _record_json(source_commit: str | None, run_id: str | None, evidence: dict) -> str:
    return json.dumps(
        {
            "schema": RECOVERY_RECORDING_SCHEMA,
            "source_commit": source_commit,
            "run_id": run_id,
            "scenario": "status-gated-reset-halt-probe",
            "target_controller": TARGET_CONTROLLER,
            "evidence": evidence,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
