"""Software contracts for the opt-in, pre-standard-gate recovery check."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from agentic_hil.report import overall_success

ROOT = Path(__file__).resolve().parents[1]
STAGE = ROOT / "tests" / "bench" / "recovery_check.py"


def _load_stage():
    assert STAGE.is_file(), "the opt-in recovery-check stage has not been implemented"
    spec = importlib.util.spec_from_file_location("bench_recovery_check", STAGE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class FixtureRequest:
    def __init__(self) -> None:
        self.requested: list[str] = []

    def getfixturevalue(self, name: str) -> object:
        self.requested.append(name)
        return object()


@pytest.mark.parametrize(
    ("cubeprogrammer_present", "profile_controller"),
    [(True, "stm32f446ret6"), (False, None), (False, "unknown-controller")],
)
def test_recovery_fixture_refuses_unsafe_setup_before_requesting_bench(
    cubeprogrammer_present: bool,
    profile_controller: str | None,
) -> None:
    stage = _load_stage()
    request = FixtureRequest()

    with pytest.raises(stage.RecoveryCheckRefused):
        stage.guarded_bench(
            request,
            cubeprogrammer_present=cubeprogrammer_present,
            profile_controller=profile_controller,
        )

    assert request.requested == []


def test_recovery_fixture_requests_the_existing_bench_only_after_safe_preflight() -> None:
    stage = _load_stage()
    request = FixtureRequest()

    result = stage.guarded_bench(
        request,
        cubeprogrammer_present=False,
        profile_controller="stm32f446ret6",
    )

    assert request.requested == ["bench"]
    assert result is not None


@pytest.mark.parametrize(
    "status",
    [
        None,
        {"ok": False, "error_type": "status_unavailable"},
        {"ok": True, "blocked": False, "standing_incidents": []},
        {"ok": True, "audit_ok": False},
        {"ok": True, "audit_broken": True},
        {"ok": True, "incident_stands": True},
        {"ok": True, "standing_incidents": [{"summary": "foreign standing incident"}]},
    ],
)
def test_recovery_status_refusal_stops_before_run_or_reset(status: object) -> None:
    stage = _load_stage()
    server = FakeMcpServer([("hardware_lease_status", status)])

    with pytest.raises(stage.RecoveryCheckRefused):
        stage.run_recovery_check(server, "dut", object(), ())

    assert [name for name, _ in server.calls] == ["hardware_lease_status"]


def test_nonstanding_local_incident_does_not_require_operator_attestation() -> None:
    stage = _load_stage()
    server = FakeMcpServer(
        [
            ("hardware_lease_status", {"ok": True, "blocked": True, "incident_stands": False, "standing_incidents": []}),
            ("bench_run_start", {"ok": True, "summary": "opened STLINK123"}),
            ("reset_target", successful_reset()),
            ("probe_target", successful_probe()),
            ("hardware_lease_status", {"ok": True, "blocked": True, "incident_stands": False, "standing_incidents": []}),
            ("bench_run_stop", {"ok": True}),
            ("hardware_lease_status", {"ok": True, "blocked": False, "incident_stands": False, "standing_incidents": []}),
        ]
    )

    evidence = stage.run_recovery_check(server, "dut", object(), ("STLINK123",))

    assert [name for name, _ in server.calls] == [
        "hardware_lease_status",
        "bench_run_start",
        "reset_target",
        "probe_target",
        "hardware_lease_status",
        "bench_run_stop",
        "hardware_lease_status",
    ]
    assert server.calls[1][1]["devices"] == [{"kind": "debugger", "id": "dut"}]
    assert server.calls[2][1] == {"mode": "halt"}
    assert evidence["reset_result"]["result"]["ok"] is True
    assert evidence["probe_result"]["result"]["target_detected"] is True
    assert evidence["lease_status_before"]["incident_stands"] is False
    assert evidence["closure"]["lease_status_after_stop"]["incident_stands"] is False
    assert "STLINK123" not in str(evidence)
    assert "[redacted]" in evidence["run_start"]["summary"]


def successful_reset() -> dict:
    return {
        "ok": True,
        "target_ok": True,
        "target_contacted": True,
        "retry_safe": False,
        "audit_ok": True,
        "cleanup_ok": True,
        "cleanup_required": False,
        "quarantined": False,
        "lease_state": "active",
        "side_effect_status": "changed",
        "hardware_state": "changed",
    }


def successful_probe() -> dict:
    return {
        **successful_reset(),
        "target_detected": True,
        "side_effect_status": "unchanged",
        "hardware_state": "unchanged",
    }


@pytest.mark.parametrize(
    ("reset", "probe"),
    [
        ({"ok": False, "audit_ok": True}, successful_probe()),
        ({"ok": True, "cleanup_required": True, "audit_ok": True}, successful_probe()),
        (successful_reset(), {**successful_probe(), "target_detected": False}),
    ],
)
def test_recovery_requires_full_success_and_detected_target(reset: dict, probe: dict) -> None:
    stage = _load_stage()
    results = [
        ("hardware_lease_status", {"ok": True, "blocked": False, "incident_stands": False, "standing_incidents": []}),
        ("bench_run_start", {"ok": True}),
        ("reset_target", reset),
    ]
    if overall_success(reset):
        results.append(("probe_target", probe))
    results.extend(
        [
            ("bench_run_stop", {"ok": True}),
            ("hardware_lease_status", {"ok": True, "blocked": False, "incident_stands": False, "standing_incidents": []}),
        ]
    )
    server = FakeMcpServer(results)

    with pytest.raises(stage.RecoveryCheckRefused) as refused:
        stage.run_recovery_check(server, "dut", object(), ())

    names = [name for name, _ in server.calls]
    assert "bench_run_stop" in names
    assert names.index("bench_run_stop") > names.index("reset_target")
    if not overall_success(reset):
        assert "probe_target" not in names
        assert "reset_target" in str(refused.value)
        assert overall_success(refused.value.evidence["reset_result"]["result"]) is False
    else:
        assert refused.value.evidence["probe_result"]["result"]["target_detected"] is False


def test_failed_reset_keeps_native_result_and_run_closure_evidence() -> None:
    stage = _load_stage()
    reset = {
        "ok": False,
        "error_type": "debugger_error",
        "summary": "backend reset command failed",
        "programmer_output": {"returncode": 1, "stdout": "", "stderr": "native reset timeout"},
    }
    server = FakeMcpServer(
        [
            ("hardware_lease_status", {"ok": True, "blocked": False, "incident_stands": False, "standing_incidents": []}),
            ("bench_run_start", {"ok": True}),
            ("reset_target", reset),
            ("hardware_lease_status", {"ok": True, "blocked": True, "incident_stands": False, "standing_incidents": []}),
            ("bench_run_stop", {"ok": True, "recovery": {"attempted": True, "outcome": "recovered", "incident_open": False}}),
            ("hardware_lease_status", {"ok": True, "blocked": False, "incident_stands": False, "standing_incidents": []}),
        ]
    )

    with pytest.raises(stage.RecoveryCheckRefused) as refused:
        stage.run_recovery_check(server, "dut", object(), ())

    evidence = refused.value.evidence
    assert evidence["reset_result"]["programmer_output"]["stderr"] == "native reset timeout"
    assert evidence["closure"]["run_stop"]["recovery"]["outcome"] == "recovered"
    assert evidence["closure"]["lease_status_after_stop"]["blocked"] is False
    assert [name for name, _ in server.calls] == [
        "hardware_lease_status",
        "bench_run_start",
        "reset_target",
        "hardware_lease_status",
        "bench_run_stop",
        "hardware_lease_status",
    ]


def test_initial_status_call_exception_is_a_recorded_preflight_refusal() -> None:
    stage = _load_stage()
    server = FakeMcpServer(
        [("hardware_lease_status", OSError("status unavailable at /private/runner/home"))]
    )

    with pytest.raises(stage.RecoveryCheckRefused) as refused:
        stage.run_recovery_check(server, "dut", object(), ("/private/runner/home",))

    assert refused.value.evidence["lease_status_before"]["available"] is False
    assert [name for name, _ in server.calls] == ["hardware_lease_status"]
    assert "/private/runner/home" not in str(refused.value.evidence)


def test_a_refused_run_start_is_reported_through_its_redacted_copy(tmp_path: Path) -> None:
    """The message pytest writes into the JUnit body carries no probe serial.

    A `bench_run_start` refusal names its resources, and a debugger resource with
    a `probe_id` locks as `probe:<folded serial>`, so the raw result can carry the
    serial. Every sibling path in this stage raises with the redacted copy; this
    one interpolated the result itself, and the stage re-raises, so pytest wrote
    the unredacted text into the `<failure>` body. bench_in_container.py redacts
    the whole JUnit file afterwards, and the three `--live-device-tree` gate steps
    run this module without it.
    """
    stage = _load_stage()
    refusal = {
        "ok": False,
        "error_type": "permission_denied",
        "summary": "bench_run_start refused: probe:stlink123 is held by another run",
        "resources": ["probe:stlink123"],
    }
    server = FakeMcpServer(
        [
            ("hardware_lease_status", {"ok": True, "blocked": False, "incident_stands": False, "standing_incidents": []}),
            ("bench_run_start", refusal),
        ]
    )

    with pytest.raises(stage.RecoveryCheckRefused) as refused:
        stage.run_recovery_check(server, "dut", object(), ("STLINK123",))

    assert "failed its continue predicate" in str(refused.value)
    assert "STLINK123" not in str(refused.value), str(refused.value)
    assert "stlink123" not in str(refused.value), str(refused.value)
    assert "[redacted]" in str(refused.value), str(refused.value)
    # The refusal is still legible: its own error and headline reach the message.
    assert "permission_denied" in str(refused.value), str(refused.value)
    # No hardware call followed the refusal, which is what it refused.
    assert [name for name, _ in server.calls] == ["hardware_lease_status", "bench_run_start"]


class FakeMcpServer:
    def __init__(self, results: list[tuple[str, object]]) -> None:
        self.results = iter(results)
        self.calls: list[tuple[str, dict]] = []

    def call(self, name: str, arguments: dict | None = None) -> tuple[str, object]:
        self.calls.append((name, arguments or {}))
        expected_name, result = next(self.results)
        assert name == expected_name, (name, expected_name)
        if isinstance(result, BaseException):
            raise result
        return name, result

    def try_call(self, name: str, arguments: dict | None = None) -> object:
        _, result = self.call(name, arguments)
        return result
