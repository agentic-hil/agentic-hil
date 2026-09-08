"""The Docker gate itself must fail closed on incomplete or false evidence."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import yaml

from evals.install.registration_gate import AGENTS, REPORT_PREFIX, SCENARIOS
from tools.test_agent_registration import main, read_report, run_logged


def passing_report() -> dict:
    return {
        "ok": True,
        "versions": {"codex": "codex-cli 0.145.0", "claude-code": "2.1.218 (Claude Code)"},
        "cases": [{"agent": agent, "scenario": scenario, "status": "passed"} for agent in AGENTS for scenario in SCENARIOS],
    }


def test_gate_accepts_only_complete_evidence() -> None:
    report = passing_report()
    assert read_report("diagnostics\n" + REPORT_PREFIX + json.dumps(report) + "\n") == report


@pytest.mark.parametrize("output", ["", "all tests passed\n", REPORT_PREFIX + "{", REPORT_PREFIX + "{}", REPORT_PREFIX + "{}\n" + REPORT_PREFIX + "{}"])
def test_zero_exit_without_one_valid_report_is_not_success(output: str) -> None:
    with pytest.raises((ValueError, KeyError, AssertionError)):
        read_report(output)


@pytest.mark.parametrize("mutation", ["empty", "missing-agent", "missing-case", "duplicate", "failed", "skipped", "unfinished", "ok-false", "no-versions"])
def test_gate_rejects_incomplete_matrix(mutation: str) -> None:
    report = passing_report()
    if mutation == "empty":
        report["cases"] = []
    elif mutation == "missing-agent":
        report["cases"] = [row for row in report["cases"] if row["agent"] == "codex"]
    elif mutation == "missing-case":
        report["cases"].pop()
    elif mutation == "duplicate":
        report["cases"][-1] = report["cases"][0]
    elif mutation == "ok-false":
        report["ok"] = False
    elif mutation == "no-versions":
        report["versions"] = {}
    else:
        report["cases"][0]["status"] = mutation
    with pytest.raises(AssertionError):
        read_report(REPORT_PREFIX + json.dumps(report))


def test_missing_docker_fails_and_replaces_old_green_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "report.json").write_text(json.dumps(passing_report()), encoding="utf-8")
    monkeypatch.setattr("tools.test_agent_registration.shutil.which", lambda _name: None)
    assert main(["--output", str(tmp_path)]) == 1
    assert json.loads((tmp_path / "report.json").read_text())["ok"] is False


@pytest.mark.parametrize("exit_code", [1, 125, 137])
def test_build_and_container_failures_cannot_pass(exit_code: int, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(command: list[str], **kwargs) -> subprocess.CompletedProcess:
        kwargs["stdout"].write("container diagnostic\n")
        return subprocess.CompletedProcess(command, exit_code)

    monkeypatch.setattr("tools.test_agent_registration.subprocess.run", fail)
    with pytest.raises(RuntimeError, match="container diagnostic"):
        run_logged(["docker", "run", "test-image"], tmp_path / "container.log", 30)


def test_timeout_is_not_converted_to_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def timeout(command: list[str], **kwargs) -> None:
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr("tools.test_agent_registration.subprocess.run", timeout)
    with pytest.raises(subprocess.TimeoutExpired):
        run_logged(["docker", "run", "test-image"], tmp_path / "container.log", 30)


def test_required_ci_cannot_skip_registration_gate() -> None:
    workflow = Path(__file__).resolve().parents[1] / ".github/workflows/ci.yml"
    jobs = yaml.safe_load(workflow.read_text(encoding="utf-8"))["jobs"]
    gate = jobs["agent-registration"]
    assert not gate.get("if") and not gate.get("continue-on-error")
    checks = [step for step in gate["steps"] if step.get("run") == "python tools/test_agent_registration.py"]
    assert len(checks) == 1 and not checks[0].get("if") and not checks[0].get("continue-on-error")
    required = jobs["required-ci"]
    assert "agent-registration" in required["needs"]
    assert required["if"] == "${{ always() }}"
    assert '"${{ needs.agent-registration.result }}" != "success"' in required["steps"][0]["run"]
