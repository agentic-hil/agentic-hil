"""The Docker gate itself must fail closed on incomplete or false evidence."""

from __future__ import annotations

import inspect
import io
import json
import subprocess
import urllib.error
from pathlib import Path

import pytest
import yaml
from parameter_rows import check_parameter_rows, parameter_rows

from evals.install.registration_gate import (
    AGENTS,
    REPORT_PREFIX,
    SCENARIOS,
    SCENARIOS_BY_MODE,
    SCRIPT_SCENARIOS,
    published_release,
    release_floor,
)
from tools.test_agent_registration import combine_reports, container_command, main, read_report, run_logged


def passing_report() -> dict:
    return {
        "ok": True,
        "mode": "all",
        "versions": {"codex": "codex-cli 0.145.0", "claude-code": "2.1.218 (Claude Code)"},
        "cases": [{"agent": agent, "scenario": scenario, "status": "passed"} for agent in AGENTS for scenario in SCENARIOS],
    }


def test_gate_accepts_only_complete_evidence() -> None:
    report = passing_report()
    assert read_report("diagnostics\n" + REPORT_PREFIX + json.dumps(report) + "\n") == report


def test_zero_exit_without_one_valid_report_is_not_success(tmp_path: Path) -> None:
    def check_row(_workspace, _patch, output: str) -> None:
        with pytest.raises((ValueError, KeyError, AssertionError)):
            read_report(output)

    rows = parameter_rows(["", "all tests passed\n", REPORT_PREFIX + "{", REPORT_PREFIX + "{}", REPORT_PREFIX + "{}\n" + REPORT_PREFIX + "{}"], ids=['', 'all tests passed\\n', 'AGENT_REGISTRATION_GATE={', 'AGENT_REGISTRATION_GATE={}', 'AGENT_REGISTRATION_GATE={}\\nAGENT_REGISTRATION_GATE={}'])
    check_parameter_rows(tmp_path, rows, check_row)


def test_gate_rejects_incomplete_matrix(tmp_path: Path) -> None:
    def check_row(_workspace, _patch, mutation: str) -> None:
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

    rows = parameter_rows(["empty", "missing-agent", "missing-case", "duplicate", "failed", "skipped", "unfinished", "ok-false", "no-versions"])
    check_parameter_rows(tmp_path, rows, check_row)


def test_missing_docker_fails_and_replaces_old_green_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "report.json").write_text(json.dumps(passing_report()), encoding="utf-8")
    monkeypatch.setattr("tools.test_agent_registration.shutil.which", lambda _name: None)
    assert main(["--output", str(tmp_path)]) == 1
    assert json.loads((tmp_path / "report.json").read_text())["ok"] is False


def stage_report(mode: str) -> dict:
    report = passing_report()
    report["mode"] = mode
    report["image_id"] = f"sha256:{mode}"
    report["cases"] = [row for row in report["cases"] if row["scenario"] in SCENARIOS_BY_MODE[mode]]
    for row in report["cases"]:
        if row["scenario"] in SCRIPT_SCENARIOS:
            row["installer_sha256"] = "checkout-installer"
    return report


def test_combined_gate_requires_real_script_cases() -> None:
    wheel = stage_report("wheel")
    script = stage_report("script")
    report = combine_reports([script, wheel], "checkout-installer")
    assert len(report["cases"]) == 20
    with pytest.raises(ValueError, match="both script and wheel"):
        combine_reports([wheel], "checkout-installer")
    with pytest.raises(AssertionError):
        read_report(REPORT_PREFIX + json.dumps(wheel))


def test_script_stage_must_execute_the_reviewed_installer() -> None:
    with pytest.raises(ValueError, match="this checkout's install.sh"):
        combine_reports([stage_report("script"), stage_report("wheel")], "different-installer")


@pytest.mark.parametrize(("mode", "network"), [("script", "bridge"), ("wheel", "none")])
def test_only_script_downloads_get_network_without_host_secrets(mode: str, network: str) -> None:
    command = container_command("docker", "test-case", "sha256:tested", mode)
    assert command[command.index("--network") + 1] == network
    assert not {"-e", "--env", "--env-file", "-v", "--volume", "--mount", "--privileged"} & set(command)


def test_build_and_container_failures_cannot_pass(tmp_path: Path) -> None:
    def check_row(tmp_path, monkeypatch, exit_code: int) -> None:
        def fail(command: list[str], **kwargs) -> subprocess.CompletedProcess:
            kwargs["stdout"].write("container diagnostic\n")
            return subprocess.CompletedProcess(command, exit_code)

        monkeypatch.setattr("tools.test_agent_registration.subprocess.run", fail)
        with pytest.raises(RuntimeError, match="container diagnostic"):
            run_logged(["docker", "run", "test-image"], tmp_path / "container.log", 30)

    rows = parameter_rows([1, 125, 137])
    check_parameter_rows(tmp_path, rows, check_row)


def test_timeout_is_not_converted_to_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def timeout(command: list[str], **kwargs) -> None:
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr("tools.test_agent_registration.subprocess.run", timeout)
    with pytest.raises(subprocess.TimeoutExpired):
        run_logged(["docker", "run", "test-image"], tmp_path / "container.log", 30)


def test_script_floor_is_the_stamped_release_once_the_index_serves_it(tmp_path: Path) -> None:
    def check_row(_workspace, _patch, stamped: tuple, published: tuple, floor: tuple) -> None:
        assert release_floor(stamped, published) == floor

    rows = parameter_rows([
        # A release commit before its publish: the index cannot serve the stamped release yet.
        ((0, 22, 0), (0, 21, 5), (0, 21, 5)),
        # Once published, and on every commit after, the stamped release is the floor.
        ((0, 22, 0), (0, 22, 0), (0, 22, 0)),
        ((0, 22, 0), (0, 23, 1), (0, 22, 0)),
    ], multiple=True, ids=['stamped0-published0-floor0', 'stamped1-published1-floor1', 'stamped2-published2-floor2'])
    check_parameter_rows(tmp_path, rows, check_row)


def an_index_answering(version: str):
    def urlopen(url: str, timeout: float) -> io.BytesIO:
        assert url == "https://pypi.org/pypi/agentic-hil/json"
        return io.BytesIO(json.dumps({"info": {"version": version}}).encode())

    return urlopen


def test_published_release_is_read_from_the_index(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("evals.install.registration_gate.urllib.request.urlopen", an_index_answering("0.21.5"))
    assert published_release() == (0, 21, 5)
    monkeypatch.setattr("evals.install.registration_gate.urllib.request.urlopen", an_index_answering("0.22.0rc1"))
    with pytest.raises(AssertionError, match="0.22.0rc1"):
        published_release()


def test_a_final_release_the_index_serves_is_a_floor_whatever_its_spelling(tmp_path: Path) -> None:
    """An X.Y.Z-only regex reintroduced the false red it was fixing.

    `require` failed outright on anything with a fourth field, so a `0.21.4.post1`
    on the index would turn every script case red with nothing wrong under
    install.sh, and the message named the index answer rather than the installer,
    so it read like a gate bug. `release_floor` then caps the floor at RELEASE, so
    no index answer can raise it out of reach either way.
    """

    def check_row(_workspace, monkeypatch, served: str, release: tuple[int, ...]) -> None:
        monkeypatch.setattr("evals.install.registration_gate.urllib.request.urlopen", an_index_answering(served))

        assert published_release() == release

    rows = parameter_rows([
        # Every spelling of a final release the index can name as its newest.
        # None of these is a reason to fail a gate about install.sh: it installs
        # whichever of them the index serves, and the floor is the three fields.
        ("0.21.4.post1", (0, 21, 4)),
        ("0.21.4-1", (0, 21, 4)),
        ("0.21.4+local.1", (0, 21, 4)),
        ("1.0", (1, 0, 0)),
        ("2", (2, 0, 0)),
        ("v0.21.4", (0, 21, 4)),
        ("0.21.4.2", (0, 21, 4)),
        (" 0.21.4 ", (0, 21, 4)),
    ], multiple=True, ids=['0.21.4.post1-release0', '0.21.4-1-release1', '0.21.4+local.1-release2', '1.0-release3', '2-release4', 'v0.21.4-release5', '0.21.4.2-release6', ' 0.21.4 -release7'])
    check_parameter_rows(tmp_path, rows, check_row)


def test_an_index_answer_that_is_no_floor_at_all_is_still_refused(tmp_path: Path) -> None:
    """The two answers that really are not a floor: not a version, and a release
    `install.sh` would not install. `uv tool install` takes the newest final
    release, so comparing what it installed against a pre-release floor is the
    same false red from the other side."""

    def check_row(_workspace, monkeypatch, served: str) -> None:
        monkeypatch.setattr("evals.install.registration_gate.urllib.request.urlopen", an_index_answering(served))

        with pytest.raises(AssertionError):
            published_release()

    rows = parameter_rows(["", "not-a-version", "latest", "0.22.0rc1", "0.22.0.dev3", "0.22.0a1", "1.4.0-beta2"])
    check_parameter_rows(tmp_path, rows, check_row)


def test_an_index_that_cannot_be_reached_turns_the_gate_red(monkeypatch: pytest.MonkeyPatch) -> None:
    """The property the whole floor rests on, pinned rather than read off the call chain.

    `published_release` is called inside a case's `exercise`, whose exceptions
    `main` catches to leave `row["status"]` at `"failed"`. Nothing asserted that,
    so a refactor that caught the network error nearer the call site would turn
    the gate into a no-op with no test failing: an unreachable index would answer
    a floor of nothing and every case would pass.
    """
    import evals.install.registration_gate as gate

    def refuse(url: str, timeout: float):
        raise urllib.error.URLError("no route to the index")

    monkeypatch.setattr("evals.install.registration_gate.urllib.request.urlopen", refuse)

    with pytest.raises(urllib.error.URLError):
        gate.published_release()

    # And `main`'s own handler is what that reaches: a case whose body raises is
    # recorded failed, and `validate_report` refuses the report over it.
    row = {"agent": "codex", "scenario": "script-explicit-agent", "status": "failed"}
    report = {"ok": False, "mode": "script", "versions": dict.fromkeys(gate.AGENTS, "1.0.0"), "cases": [row]}
    with pytest.raises(AssertionError):
        gate.validate_report(report, mode="script")
    assert "except Exception:" in inspect.getsource(gate.main), "main no longer records a raising case as failed"


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
