from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from collections import Counter
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from evals.install import bench_openocd, guard, scrub_credentials
from evals.install import runner as install_runner
from evals.install.adapters import adapter_for, build_agent_command
from evals.install.config import CredentialFile, Job, load_case, load_matrix
from evals.install.credentials import authentication_failure, credential_health
from evals.install.fixtures import (
    SENTINEL_KEY,
    SENTINEL_VALUE,
    fixture_content,
    sentinel_value,
    skills_directory,
)
from evals.install.refresh_login import BACKUP_SUFFIX, apply_refreshed_login
from evals.install.report import format_report, load_results, report_results, unstable_groups
from evals.install.routing import SKILL_NAME, classify, followup_lines, route_of, routing_results
from evals.install.runner import (
    agent_container_command,
    auth_values,
    dry_run_plan,
    ensure_auth,
    resolved_credential_files,
    verifier_container_command,
)
from evals.install.source import create_source_snapshot, source_digest
from evals.install.verifier import overall_success

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    ("agent", "model", "model_flag"),
    [
        ("codex", "gpt-test", "--model"),
        ("claude", "claude-test", "--model"),
        ("opencode", "provider/model-test", "--model"),
    ],
)
def test_agent_command_keeps_model_as_explicit_axis(agent: str, model: str, model_flag: str) -> None:
    command = build_agent_command(agent, model, "install")

    assert model_flag in command
    assert command[command.index(model_flag) + 1] == model
    assert command[-1] == "install"


@pytest.mark.parametrize(
    ("agent", "isolating_flag"),
    [("codex", "--ignore-user-config"), ("claude-code", "--strict-mcp-config")],
)
def test_only_the_isolated_session_ignores_the_agent_user_config(agent: str, isolating_flag: str) -> None:
    isolated = build_agent_command(agent, "model", "prompt")
    connected = build_agent_command(agent, "model", "prompt", isolated=False)

    # The installation phase must be hermetic; the phase that has to use the
    # registered MCP server must read the configuration setup wrote.
    assert isolating_flag in isolated
    assert isolating_flag not in connected


@pytest.mark.parametrize(
    ("agent", "expected"),
    [
        ("codex", ["--config", "model_reasoning_effort=medium"]),
        ("claude-code", ["--effort", "medium"]),
        ("opencode", ["--variant", "medium"]),
    ],
)
def test_agent_command_carries_the_reasoning_effort(agent: str, expected: list[str]) -> None:
    command = build_agent_command(agent, "model", "prompt", reasoning_effort="medium")

    # Adjacent: a flag separated from its value is a different command.
    assert any(command[index : index + 2] == expected for index in range(len(command) - 1))


@pytest.mark.parametrize("agent", ["codex", "claude-code", "opencode"])
def test_reasoning_effort_reaches_both_sessions(agent: str) -> None:
    # The installing session is deliberately isolated and the measured session
    # deliberately is not. An effort that reached only one of them would have
    # the follow-up answer as a different model than the one that installed.
    isolated = build_agent_command(agent, "model", "prompt", reasoning_effort="high")
    connected = build_agent_command(agent, "model", "prompt", isolated=False, reasoning_effort="high")

    assert "high" in " ".join(isolated)
    assert "high" in " ".join(connected)


@pytest.mark.parametrize("agent", ["codex", "claude-code", "opencode"])
def test_agent_command_says_nothing_about_effort_when_none_was_asked(agent: str) -> None:
    command = " ".join(build_agent_command(agent, "model", "prompt"))

    for flag in ("--effort", "--variant", "model_reasoning_effort"):
        assert flag not in command


@pytest.mark.parametrize("effort", ["ultrathink", "ultra", "none", ""])
def test_agent_command_refuses_an_effort_the_agents_do_not_share(effort: str) -> None:
    # "ultra" is codex-only and "none" is opencode-only: accepting either would
    # hand the other CLIs a silent default and the comparison would be a fiction.
    with pytest.raises(ValueError, match="reasoning_effort"):
        build_agent_command("codex", "model", "prompt", reasoning_effort=effort)


def test_adapter_aliases_resolve_to_setup_agent_names() -> None:
    assert adapter_for("codex-cli").id == "codex"
    assert adapter_for("claude").id == "claude-code"
    assert adapter_for("open-code").id == "opencode"


def test_example_matrix_expands_agents_and_cases_separately() -> None:
    matrix = load_matrix(REPOSITORY_ROOT / "evals" / "install" / "matrix.example.json")

    assert {job.agent for job in matrix.jobs} == {"codex", "claude-code", "opencode"}
    assert {case.id for case in matrix.cases} == {
        "quickstart",
        "preserve-user-config",
        "unsafe-existing-config",
        "firmware-routing",
    }

    models = Counter(agent for agent, _model in {(job.agent, job.model) for job in matrix.jobs})
    assert all(count >= 2 for count in models.values()), models

    repetitions = Counter((job.agent, job.model) for job in matrix.jobs)
    assert set(repetitions.values()) == {2}, repetitions
    assert matrix.max_repetitions > 2


def test_remote_matrix_rejects_mutable_ref(tmp_path: Path) -> None:
    case = tmp_path / "case.json"
    case.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "id": "case",
                "prompt": "{workspace} {guide} {install_spec} {agent}",
            }
        ),
        encoding="utf-8",
    )
    matrix = tmp_path / "matrix.json"
    matrix.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "case": "case.json",
                "target": {
                    "mode": "remote",
                    "expected_version": "0.4.0",
                    "install_spec": "git+https://github.com/agentic-hil/agentic-hil@main",
                    "guide_url": "https://example.invalid/guide",
                },
                "jobs": [{"agent": "codex", "model": "test", "credentials": ["OPENAI_API_KEY"]}],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="full 40-character commit"):
        load_matrix(matrix)


def test_remote_matrix_rejects_guide_not_bound_to_commit(tmp_path: Path) -> None:
    commit = "0123456789abcdef0123456789abcdef01234567"
    case = tmp_path / "case.json"
    case.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "id": "case",
                "prompt": "{workspace} {guide} {install_spec} {agent}",
            }
        ),
        encoding="utf-8",
    )
    matrix = tmp_path / "matrix.json"
    matrix.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "case": "case.json",
                "target": {
                    "mode": "remote",
                    "expected_version": "0.4.0",
                    "install_spec": f"git+https://github.com/agentic-hil/agentic-hil@{commit}",
                    "guide_url": "https://example.invalid/guide",
                },
                "jobs": [{"agent": "codex", "model": "test", "credentials": ["OPENAI_API_KEY"]}],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="immutable official raw guide URL"):
        load_matrix(matrix)


def test_remote_matrix_derives_guide_from_commit(tmp_path: Path) -> None:
    commit = "0123456789abcdef0123456789abcdef01234567"
    case = tmp_path / "case.json"
    case.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "id": "case",
                "prompt": "{workspace} {guide} {install_spec} {agent}",
            }
        ),
        encoding="utf-8",
    )
    matrix_path = tmp_path / "matrix.json"
    matrix_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "case": "case.json",
                "target": {
                    "mode": "remote",
                    "expected_version": "0.4.0",
                    "install_spec": f"git+https://github.com/agentic-hil/agentic-hil@{commit}",
                },
                "jobs": [{"agent": "codex", "model": "test", "credentials": ["CODEX_ACCESS_TOKEN"]}],
            }
        ),
        encoding="utf-8",
    )

    matrix = load_matrix(matrix_path)

    assert matrix.target.guide_url == (
        f"https://raw.githubusercontent.com/agentic-hil/agentic-hil/{commit}/AI_AGENT_QUICKSTART.md"
    )


def test_matrix_requires_explicit_per_job_credentials(tmp_path: Path) -> None:
    case = tmp_path / "case.json"
    case.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "id": "case",
                "prompt": "{workspace} {guide} {install_spec} {agent}",
            }
        ),
        encoding="utf-8",
    )
    matrix = tmp_path / "matrix.json"
    matrix.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "case": "case.json",
                "target": {"mode": "local", "expected_version": "0.4.0"},
                "jobs": [{"agent": "opencode", "model": "test"}],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=r"must declare credentials and/or credential_files"):
        load_matrix(matrix)


def test_matrix_rejects_cross_provider_credential_for_codex(tmp_path: Path) -> None:
    case = tmp_path / "case.json"
    case.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "id": "case",
                "prompt": "{workspace} {guide} {install_spec} {agent}",
            }
        ),
        encoding="utf-8",
    )
    matrix = tmp_path / "matrix.json"
    matrix.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "case": "case.json",
                "target": {"mode": "local", "expected_version": "0.4.0"},
                "jobs": [{"agent": "codex", "model": "test", "credentials": ["ANTHROPIC_API_KEY"]}],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="unsupported names for codex"):
        load_matrix(matrix)


def test_matrix_rejects_legacy_shared_environment(tmp_path: Path) -> None:
    case = tmp_path / "case.json"
    case.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "id": "case",
                "prompt": "{workspace} {guide} {install_spec} {agent}",
            }
        ),
        encoding="utf-8",
    )
    matrix = tmp_path / "matrix.json"
    matrix.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "case": "case.json",
                "target": {"mode": "local", "expected_version": "0.4.0"},
                "pass_environment": ["HOME"],
                "jobs": [{"agent": "codex", "model": "test", "credentials": ["OPENAI_API_KEY"]}],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="matrix.pass_environment is unsupported"):
        load_matrix(matrix)


def test_source_digest_ignores_generated_directories(tmp_path: Path) -> None:
    (tmp_path / "source.py").write_text("one\n", encoding="utf-8")
    first = source_digest(tmp_path)
    cache = tmp_path / ".pytest_cache"
    cache.mkdir()
    (cache / "state").write_text("ignored\n", encoding="utf-8")
    assert source_digest(tmp_path) == first

    (tmp_path / "source.py").write_text("two\n", encoding="utf-8")
    assert source_digest(tmp_path) != first


def test_claude_stream_json_output_is_paired_with_verbose() -> None:
    command = build_agent_command("claude-code", "claude-sonnet-4-6", "install it")

    # Claude Code rejects --print with stream-json unless it is also verbose,
    # which fails the run in seconds before the agent does anything.
    assert "--output-format" in command
    assert command[command.index("--output-format") + 1] == "stream-json"
    assert "--verbose" in command


@pytest.mark.parametrize(
    ("agent", "kind"),
    [("codex", "codex-auth"), ("claude-code", "claude-auth"), ("opencode", "opencode-auth")],
)
def test_every_agent_cli_accepts_its_own_file_login(agent: str, kind: str, tmp_path: Path) -> None:
    matrix = _matrix_with_credential_file(tmp_path, agent, kind)

    assert load_matrix(matrix).jobs[0].credential_files[0].kind == kind


def test_file_login_kind_stays_bound_to_its_agent(tmp_path: Path) -> None:
    matrix = _matrix_with_credential_file(tmp_path, "claude-code", "codex-auth")

    with pytest.raises(ValueError, match="unsupported for claude-code"):
        load_matrix(matrix)


def _matrix_with_credential_file(directory: Path, agent: str, kind: str) -> Path:
    example = REPOSITORY_ROOT / "evals" / "install" / "matrix.example.json"
    document = json.loads(example.read_text(encoding="utf-8"))
    document["cases"] = [str(REPOSITORY_ROOT / "evals" / "install" / "cases" / "quickstart.json")]
    document["jobs"] = [
        {
            "agent": agent,
            "model": "test-model",
            "credential_files": [{"kind": kind, "path_environment": "TEST_AUTH_FILE"}],
        }
    ]
    matrix = directory / "matrix.json"
    matrix.write_text(json.dumps(document), encoding="utf-8")
    return matrix


def _write_result(directory: Path, identifier: str, status: str, checks: list[dict]) -> None:
    run_directory = directory / identifier
    run_directory.mkdir(parents=True)
    (run_directory / "result.json").write_text(
        json.dumps(
            {
                "id": identifier,
                "status": status,
                "agent": {"cli": "codex", "model": "test-model"},
                "case": {"id": "quickstart"},
                "checks": checks,
                "duration_seconds": 12.0,
            }
        ),
        encoding="utf-8",
    )


def _claude_login(access: str = "a-token", refresh: str = "r-token") -> str:
    return json.dumps({"claudeAiOauth": {"accessToken": access, "refreshToken": refresh}})


def test_refreshed_login_replaces_the_file_and_keeps_the_previous_one(tmp_path: Path) -> None:
    path = tmp_path / ".credentials.json"
    path.write_text(_claude_login(), encoding="utf-8")

    refreshed = _claude_login(access="new-token")
    outcome = apply_refreshed_login("claude-auth", path, refreshed)

    assert "replaced" in outcome
    assert path.read_text(encoding="utf-8") == refreshed
    assert path.with_name(path.name + BACKUP_SUFFIX).read_text(encoding="utf-8") == _claude_login()


def test_refreshed_login_is_rejected_unless_it_is_still_a_login(tmp_path: Path) -> None:
    path = tmp_path / ".credentials.json"
    original = _claude_login()
    path.write_text(original, encoding="utf-8")

    # Anything that is not a usable login must never replace one.
    assert "rejected" in apply_refreshed_login("claude-auth", path, "not json")
    assert "rejected" in apply_refreshed_login("claude-auth", path, json.dumps({"claudeAiOauth": {}}))
    assert "rejected" in apply_refreshed_login("claude-auth", path, json.dumps({"other": "shape"}))
    assert apply_refreshed_login("claude-auth", path, original) == "unchanged"

    assert path.read_text(encoding="utf-8") == original
    assert not path.with_name(path.name + BACKUP_SUFFIX).exists()


def test_dead_login_is_recognised_before_and_during_a_run(tmp_path: Path) -> None:
    now = datetime(2026, 7, 27, 12, 0, tzinfo=timezone.utc)
    path = tmp_path / "auth.json"

    def claude(access_hours: float, refresh_hours: float) -> tuple[str, str]:
        path.write_text(
            json.dumps(
                {
                    "claudeAiOauth": {
                        "expiresAt": int((now.timestamp() + access_hours * 3600) * 1000),
                        "refreshTokenExpiresAt": int((now.timestamp() + refresh_hours * 3600) * 1000),
                    }
                }
            ),
            encoding="utf-8",
        )
        return credential_health("claude-auth", path, now=now)

    assert claude(2, 200)[0] == "ok"
    # A token that expires during the run is refreshed inside the container,
    # which is what can rotate it out from under this machine.
    assert claude(0.5, 200)[0] == "stale"
    assert claude(-1, 200)[0] == "stale"
    # Nothing left to refresh with.
    assert claude(-1, -1)[0] == "expired"

    assert authentication_failure('{"error":{"message":"Token refresh failed: 401"}}') is not None
    assert authentication_failure("agentic-hil setup completed") is None


def _claude_line(name: str, payload: dict) -> str:
    return json.dumps({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": name, "input": payload}]}})


def test_routing_counts_actions_not_documents() -> None:
    lines = [
        # The skill lists the raw commands it replaces; reading it is not running them.
        _claude_line("Read", {"file_path": "/home/eval/.claude/skills/agentic-hil/SKILL.md"}),
        _claude_line("Bash", {"command": "cat /workspace/source/AI_AGENT_QUICKSTART.md"}),
        _claude_line("mcp__agentic-hil__probe_target", {}),
    ]

    classified = classify(lines)

    assert classified["raw_commands"] == []
    assert classified["mcp_calls"] == 1
    assert classified["cli_calls"] == 0
    assert classified["skill_referenced"]
    assert route_of({"followup": True, **classified}) == "mcp"


def _opencode_line(tool: str, payload: dict) -> str:
    return json.dumps({"type": "tool", "tool": tool, "state": {"status": "completed", "input": payload}})


def test_routing_sees_the_skill_however_the_agent_loads_it() -> None:
    # Each CLI loads the same skill differently: Claude Code invokes a tool,
    # Codex reads the file, opencode names it in "name".
    claude = classify([_claude_line("Skill", {"skill": SKILL_NAME})])
    codex = classify(
        [_claude_line("Bash", {"command": f"sed -n '1,240p' /home/eval/.codex/skills/{SKILL_NAME}/SKILL.md"})]
    )
    opencode = classify([_opencode_line("skill", {"name": SKILL_NAME})])

    assert claude["skill_referenced"]
    assert codex["skill_referenced"]
    assert opencode["skill_referenced"]


def test_routing_does_not_mistake_an_mcp_call_for_a_skill_load() -> None:
    # The skill and the MCP server share the name agentic-hil; only loading the
    # skill counts as loading it.
    classified = classify([_claude_line("mcp__agentic-hil__probe_target", {})])

    assert classified["mcp_calls"] == 1
    assert not classified["skill_referenced"]


def test_routing_reads_a_name_field_only_for_the_skill_tool() -> None:
    # "name" identifies the tool itself all over these transcripts, so reading it
    # everywhere would turn a mention of openocd into a hardware call.
    classified = classify([_opencode_line("bash", {"name": "openocd", "title": "openocd -f board.cfg"})])

    assert classified["raw_commands"] == []


def test_routing_counts_a_raw_command_only_when_it_is_run() -> None:
    searched = classify(
        [_claude_line("Bash", {"command": 'find /workspace -name "*.gdb" -o -name "openocd.cfg"'})]
    )
    looked_up = classify([_claude_line("Bash", {"command": "which openocd || echo missing"})])
    chained = classify([_claude_line("Bash", {"command": "cd /tmp && openocd -f board.cfg"})])

    assert searched["raw_commands"] == []
    assert looked_up["raw_commands"] == []
    assert chained["raw_commands"] == ["openocd"]


def test_routing_reads_a_command_the_way_a_shell_would() -> None:
    searched = classify(
        [
            _claude_line(
                "Bash",
                {"command": '/bin/bash -lc "rg -n --hidden -S \\"debugger|probe|openocd|pyocd\\" ."'},
            )
        ]
    )
    # The same separator outside quotes is a pipeline, and a shell's quoted
    # argument is a command line of its own.
    wrapped = classify([_claude_line("Bash", {"command": '/bin/bash -lc "openocd -f board.cfg"'})])

    assert searched["raw_commands"] == []
    assert wrapped["raw_commands"] == ["openocd"]


def test_routing_lets_the_guard_overrule_the_transcript() -> None:
    # The transcript shows what the agent typed; the PATH guard records what was
    # executed. Where they disagree, the container's evidence decides, in both
    # directions, and absent evidence decides nothing.
    typed = {"followup": True, "raw_commands": ["openocd"], "mcp_calls": 1, "cli_calls": 0, "config_reads": 0}
    silent = {**typed, "raw_commands": []}

    assert route_of({**typed, "guard_triggered": False}) == "mcp"
    assert route_of({**typed, "guard_triggered": True}) == "raw"
    assert route_of({**typed, "guard_triggered": None}) == "raw"
    # A command the guard recorded but the transcript never spelled out.
    assert route_of({**silent, "guard_triggered": True}) == "raw"
    assert route_of({**silent, "guard_triggered": None}) == "mcp"


def test_guard_verdict_is_read_from_the_measured_session_only() -> None:
    """A hardware command reached for while installing routes nothing.

    Both sessions of a run write into one guard log, and reading the whole log as
    the answer to the hardware question made the verdict about the wrong session.
    """
    from evals.install.routing import FOLLOWUP_GUARD_CHECK, GUARD_CHECK, guard_triggered

    fired = {"checks": [{"name": FOLLOWUP_GUARD_CHECK, "ok": False, "detail": "openocd"}]}
    silent = {"checks": [{"name": FOLLOWUP_GUARD_CHECK, "ok": True, "detail": "none"}]}

    assert guard_triggered(fired) is True
    assert guard_triggered(silent) is False
    # The whole-run check keeps its own meaning and decides nothing here: it
    # fires for the installing session too.
    assert guard_triggered({"checks": [{"name": GUARD_CHECK, "ok": False, "detail": "openocd"}]}) is None
    # An older result carries no such check. Missing evidence is not evidence.
    assert guard_triggered({"checks": [{"name": "something else", "ok": True, "detail": ""}]}) is None
    assert guard_triggered({}) is None


def test_routing_sees_a_command_on_a_later_line() -> None:
    # shlex treats a newline as whitespace, so without splitting lines first a
    # script's second command is folded into the first one's arguments.
    classified = classify(
        [_claude_line("Bash", {"command": "cd /workspace/project\nopenocd -f openocd.cfg\n"})]
    )

    assert classified["raw_commands"] == ["openocd"]


def test_routing_separates_the_cli_and_raw_paths() -> None:
    cli = classify([_claude_line("Bash", {"command": "agentic-hil debugger-probes 2>&1"})])
    raw = classify([_claude_line("Bash", {"command": "openocd -f board.cfg"})])
    read_only = classify(
        [_claude_line("Bash", {"command": "cat /home/eval/.config/agentic-hil/projects/p1/config.yaml"})]
    )

    assert route_of({"followup": True, **cli}) == "cli"
    assert route_of({"followup": True, **raw}) == "raw"
    assert route_of({"followup": True, **read_only}) == "config-file"


def test_routing_reads_only_the_second_session(tmp_path: Path) -> None:
    log = tmp_path / "agent.log"
    log.write_text(
        "\n".join(
            [
                _claude_line("Bash", {"command": "openocd -f board.cfg"}),
                json.dumps({"event": "eval_followup_start"}),
                _claude_line("mcp__agentic-hil__probe_target", {}),
            ]
        ),
        encoding="utf-8",
    )

    classified = classify(followup_lines(log))

    # The installation phase is not what the routing case measures.
    assert classified["raw_commands"] == []
    assert classified["mcp_calls"] == 1


def test_tool_evidence_is_asked_for_in_a_second_session() -> None:
    case = load_case(REPOSITORY_ROOT / "evals" / "install" / "cases" / "firmware-routing.json")

    # An agent CLI discovers skills at startup, so the session that installs the
    # skill cannot be the one measured for following it.
    assert case.requires_tool_use
    assert case.followup_prompt
    assert "hardware" in case.followup_prompt
    assert "Install Agentic HIL" not in case.followup_prompt


@pytest.mark.parametrize("case_id", ["firmware-routing", "firmware-readiness", "firmware-flash-request"])
def test_the_control_arm_differs_only_in_the_skill(case_id: str) -> None:
    cases = REPOSITORY_ROOT / "evals" / "install" / "cases"
    treatment = load_case(cases / f"{case_id}.json")
    control = load_case(cases / f"{case_id}-without-skill.json")

    # Same prompts, same fixture: the skill is the only thing that varies, or
    # the difference in routing cannot be attributed to it.
    assert control.prompt_template == treatment.prompt_template
    assert control.followup_prompt == treatment.followup_prompt
    assert control.fixture == treatment.fixture
    assert control.workspace_fixture == treatment.workspace_fixture
    assert control.remove_skill_before_followup
    assert not treatment.remove_skill_before_followup
    # Without the skill the run may legitimately answer another way, so tool
    # evidence is measured rather than required.
    assert not control.requires_tool_use


def _routing_entry(case_id: str, mcp_calls: int, *, mcp_available: bool | None = True) -> dict:
    return {
        "group": (case_id, "codex", "model", "default"),
        "status": "passed",
        "followup": True,
        "mcp_available": mcp_available,
        "mcp_calls": mcp_calls,
        "mcp_tools": [f"mcp__agentic-hil__tool{index}" for index in range(mcp_calls)],
        "cli_calls": 0,
        "raw_commands": [],
        "config_reads": 1 if not mcp_calls else 0,
        "skill_referenced": bool(mcp_calls),
    }


def test_routing_gate_covers_the_treatment_arm_only() -> None:
    args = argparse.Namespace(output="unused")
    analysed = [_routing_entry("firmware-routing", 2), _routing_entry("firmware-routing-without-skill", 0)]

    # A control run that skips MCP is the measurement, not a regression.
    assert routing_results(args, analysed) == 0
    assert routing_results(args, [_routing_entry("firmware-routing", 0)]) == 1


def test_a_run_with_no_registered_server_is_not_a_routing_result() -> None:
    """It had nothing to route through, and its failed install already says so.

    Counting it as a run that chose another way is where the headline rate came
    from: the denominators held runs whose installation never finished.
    """
    from evals.install.routing import format_routing_report

    args = argparse.Namespace(output="unused")
    unregistered = _routing_entry("firmware-routing", 0, mcp_available=False)

    assert routing_results(args, [unregistered]) == 0

    report = format_routing_report(
        [_routing_entry("firmware-routing", 2), unregistered],
        "unused",
    )

    assert "Answered through the MCP server: 1/1 with the skill installed" in report
    assert "no MCP registration to route through: 1 of 2 measured runs" in report


def test_tool_counts_are_averaged_over_the_runs_that_used_tools() -> None:
    """Mixing "how many tools an answer takes" with "how many answers used none"."""
    from evals.install.routing import format_routing_report

    report = format_routing_report(
        [
            _routing_entry("firmware-routing", 4),
            _routing_entry("firmware-routing", 2),
            _routing_entry("firmware-routing", 0),
            _routing_entry("firmware-routing-without-skill", 2),
        ],
        "unused",
    )

    # Three treatment runs, two of which routed: 4 and 2 distinct tools.
    assert "Distinct Agentic HIL tools per MCP-routed run: 3.0 (n=2) with, 2.0 (n=1) without" in report
    assert "Answered through the MCP server: 2/3 with the skill installed" in report


def _matrix_with_jobs(tmp_path: Path, jobs: list[dict], name: str = "matrix.json") -> Path:
    case = tmp_path / "case.json"
    case.write_text(
        json.dumps({"schema_version": 1, "id": "case", "prompt": "{workspace} {guide} {install_spec} {agent}"}),
        encoding="utf-8",
    )
    matrix = tmp_path / name
    matrix.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "case": "case.json",
                "target": {"mode": "local", "expected_version": "0.4.0"},
                "jobs": jobs,
            }
        ),
        encoding="utf-8",
    )
    return matrix


def test_matrix_rejects_an_unknown_reasoning_effort(tmp_path: Path) -> None:
    matrix = _matrix_with_jobs(tmp_path, [{"agent": "codex", "model": "m", "credentials": ["OPENAI_API_KEY"], "reasoning_effort": "bogus"}])

    with pytest.raises(ValueError, match="low, medium, high, xhigh, max"):
        load_matrix(matrix)


def test_the_same_model_at_two_efforts_is_not_a_duplicate(tmp_path: Path) -> None:
    # Two levels of one model are two measurements, not a collision, and two
    # jobs at the same level still are one.
    matrix = _matrix_with_jobs(
        tmp_path,
        [
            {"agent": "codex", "model": "m", "credentials": ["OPENAI_API_KEY"], "reasoning_effort": "low"},
            {"agent": "codex", "model": "m", "credentials": ["OPENAI_API_KEY"], "reasoning_effort": "high"},
        ],
    )

    loaded = load_matrix(matrix)
    assert {job.reasoning_effort for job in loaded.jobs} == {"low", "high"}

    duplicate = _matrix_with_jobs(
        tmp_path,
        [
            {"agent": "codex", "model": "m", "credentials": ["OPENAI_API_KEY"], "reasoning_effort": "low"},
            {"agent": "codex", "model": "m", "credentials": ["OPENAI_API_KEY"], "reasoning_effort": "low"},
        ],
        name="duplicate.json",
    )
    with pytest.raises(ValueError, match="reasoning effort"):
        load_matrix(duplicate)


def test_two_efforts_of_one_model_are_not_disagreeing_repetitions() -> None:
    # Without the effort in the group, a passing high-effort run and a failing
    # low-effort run look unstable, and the matrix spends its escalation budget
    # chasing the difference it was built to measure.
    high = _result("quickstart", "codex", "m", "passed")
    high["agent"]["reasoning_effort"] = "high"
    low = _result("quickstart", "codex", "m", "failed")
    low["agent"]["reasoning_effort"] = "low"

    assert unstable_groups([high, low]) == []


def test_an_ignored_effort_fails_the_run(tmp_path: Path) -> None:
    from evals.install.runner import reasoning_effort_evidence

    log = tmp_path / "agent.log"
    # The literal Claude Code emits; paraphrasing it would make this test pass
    # while the real sentinel changed under a CLI bump.
    log.write_text(
        "Warning: Unknown --effort value 'bogus' — ignoring it and using the default effort.\n",
        encoding="utf-8",
    )

    assert reasoning_effort_evidence(log, "medium")[0] is False
    # Inert when nothing was requested, so it cannot fail existing runs.
    assert reasoning_effort_evidence(log, None)[0] is True
    assert reasoning_effort_evidence(tmp_path / "absent.log", "medium")[0] is False


def _planned(tmp_path: Path, jobs: list[dict], cases: int = 1) -> list[tuple]:
    from evals.install.config import Case

    matrix = load_matrix(_matrix_with_jobs(tmp_path, jobs))
    made = [replace(matrix.cases[0], id=f"case{index}") for index in range(cases)]
    assert all(isinstance(case, Case) for case in made)
    return [(case, job) for case in made for job in matrix.jobs]


def test_the_slowest_combination_starts_first(tmp_path: Path) -> None:
    """Workers take whatever is left, so a long run started late is the tail.

    Measured: four workers reached 2.3x rather than 4x because one 21-minute run
    began near the end and three workers waited it out.
    """
    from evals.install.runner import interleaved_plan

    planned = _planned(
        tmp_path,
        [
            {"agent": "codex", "model": "quick", "credentials": ["OPENAI_API_KEY"], "expected_seconds": 60},
            {"agent": "codex", "model": "slow", "credentials": ["OPENAI_API_KEY"], "expected_seconds": 900},
            {"agent": "codex", "model": "unmeasured", "credentials": ["OPENAI_API_KEY"]},
        ],
    )

    order = [job.model for _, job in interleaved_plan(planned)]

    # Unknown counts as slow: an unmeasured combination must not become the tail.
    assert order[:2] == ["unmeasured", "slow"]
    assert order[-1] == "quick"


def test_neighbouring_slots_hold_different_models(tmp_path: Path) -> None:
    """Longest-first alone hands every worker the same model at once.

    Measured with six workers: four claude-sonnet-4-6 runs started inside 21
    seconds and all four then produced nothing for 300 s, though that model
    finishes a run in under a minute when it goes alone.
    """
    from evals.install.runner import interleaved_plan

    planned = _planned(
        tmp_path,
        [
            {"agent": "codex", "model": "a", "credentials": ["OPENAI_API_KEY"], "expected_seconds": 100},
            {"agent": "codex", "model": "b", "credentials": ["OPENAI_API_KEY"], "expected_seconds": 90},
            {"agent": "codex", "model": "c", "credentials": ["OPENAI_API_KEY"], "expected_seconds": 80},
        ],
        cases=4,
    )

    order = [job.model for _, job in interleaved_plan(planned)]

    assert len(order) == len(planned)
    # The first three slots, the ones a pool starts together, are three models.
    assert sorted(order[:3]) == ["a", "b", "c"]
    assert all(first != second for first, second in zip(order, order[1:], strict=False))


def test_one_model_never_holds_more_slots_than_it_may(tmp_path: Path) -> None:
    """Ordering spreads the start; only the gate keeps it spread under way."""
    from evals.install.runner import run_concurrently

    planned = _planned(
        tmp_path,
        [
            {"agent": "codex", "model": "a", "credentials": ["OPENAI_API_KEY"]},
            {"agent": "codex", "model": "b", "credentials": ["OPENAI_API_KEY"]},
        ],
        cases=6,
    )
    lock = threading.Lock()
    live: dict[str, int] = {}
    peak: dict[str, int] = {}

    def execute(case, job) -> dict:
        with lock:
            live[job.model] = live.get(job.model, 0) + 1
            peak[job.model] = max(peak.get(job.model, 0), live[job.model])
        time.sleep(0.02)
        with lock:
            live[job.model] -= 1
        return {"id": f"{job.model}-{case.id}", "status": "passed"}

    results, aborted = run_concurrently(planned, execute, concurrency=8, per_model=2, abort_reason=lambda _: None)

    assert aborted is None
    assert len(results) == len(planned)
    assert peak == {"a": 2, "b": 2}


def test_a_run_of_a_busy_model_waits_while_other_models_keep_going(tmp_path: Path) -> None:
    """A throttled model must delay its own queue, not occupy the whole pool."""
    from evals.install.runner import run_concurrently

    planned = _planned(
        tmp_path,
        [
            {"agent": "codex", "model": "slow", "credentials": ["OPENAI_API_KEY"]},
            {"agent": "codex", "model": "fast", "credentials": ["OPENAI_API_KEY"]},
        ],
        cases=4,
    )
    lock = threading.Lock()
    finished: list[str] = []

    def execute(case, job) -> dict:
        time.sleep(0.3 if job.model == "slow" else 0.01)
        with lock:
            finished.append(job.model)
        return {"id": f"{job.model}-{case.id}", "status": "passed"}

    results, _ = run_concurrently(planned, execute, concurrency=2, per_model=1, abort_reason=lambda _: None)

    fast = sum(1 for _, job in planned if job.model == "fast")
    assert len(results) == len(planned)
    assert finished.count("fast") == fast
    # The fast queue moves while the first slow run is still going, rather than
    # queueing behind it. The bound is one rather than most of them because how
    # many land first is a speed ratio, and a loaded runner does not keep it:
    # this asked for six of eight and got four on macOS.
    assert finished.index("slow") >= 1


def test_a_model_that_only_stalls_stops_holding_its_runs_back(tmp_path: Path) -> None:
    """The cap protects a provider that answers. A dead one needs no protection.

    Measured: 20 runs that produced nothing held 56% of a matrix's container
    time, queueing two at a time through a five-minute silence budget each.
    """
    from evals.install.runner import run_concurrently

    planned = _planned(
        tmp_path,
        [{"agent": "codex", "model": "dead", "credentials": ["OPENAI_API_KEY"]}],
        cases=8,
    )
    lock = threading.Lock()
    live = 0
    peak = 0

    def execute(case, job) -> dict:
        nonlocal live, peak
        with lock:
            live += 1
            peak = max(peak, live)
        time.sleep(0.05)
        with lock:
            live -= 1
        return {"id": case.id, "status": "error", "failure": "agent produced no output for 300s"}

    results, _ = run_concurrently(planned, execute, concurrency=8, per_model=2, abort_reason=lambda _: None)

    assert len(results) == len(planned)
    # Capped at two while it might still be healthy, uncapped once it is not.
    assert peak > 2


def test_a_model_that_answers_stays_capped(tmp_path: Path) -> None:
    from evals.install.runner import run_concurrently

    planned = _planned(
        tmp_path,
        [{"agent": "codex", "model": "alive", "credentials": ["OPENAI_API_KEY"]}],
        cases=8,
    )
    lock = threading.Lock()
    live = 0
    peak = 0

    def execute(case, job) -> dict:
        nonlocal live, peak
        with lock:
            live += 1
            peak = max(peak, live)
        time.sleep(0.05)
        with lock:
            live -= 1
        return {"id": case.id, "status": "passed"}

    run_concurrently(planned, execute, concurrency=8, per_model=2, abort_reason=lambda _: None)

    assert peak == 2


def test_an_authentication_failure_stops_the_remaining_runs(tmp_path: Path) -> None:
    from evals.install.runner import run_concurrently

    planned = _planned(
        tmp_path,
        [
            {"agent": "codex", "model": "a", "credentials": ["OPENAI_API_KEY"]},
            {"agent": "codex", "model": "b", "credentials": ["OPENAI_API_KEY"]},
        ],
        cases=8,
    )

    def execute(case, job) -> dict:
        time.sleep(0.01)
        return {"id": f"{job.model}-{case.id}", "status": "error", "failure": "could not authenticate"}

    def abort_reason(result: dict) -> str | None:
        return result["failure"] if "could not authenticate" in result["failure"] else None

    results, aborted = run_concurrently(planned, execute, concurrency=4, per_model=2, abort_reason=abort_reason)

    assert aborted == "could not authenticate"
    # Every later run would fail the same way and cost the same.
    assert len(results) < len(planned)


def test_a_counted_reasoning_token_confirms_the_requested_effort(tmp_path: Path) -> None:
    """Where a CLI counts reasoning tokens, the count answers the question.

    Across 356 measured runs every transcript carrying the field reported
    between 127 and 8762 tokens and never zero, so a zero is the CLI running the
    model without the reasoning that was asked for.
    """
    from evals.install.runner import reasoning_effort_evidence

    log = tmp_path / "agent.log"
    log.write_text('{"usage":{"reasoning_output_tokens":592,"output_tokens":1989}}\n', encoding="utf-8")

    ok, detail = reasoning_effort_evidence(log, "medium")

    assert ok
    assert "counted 592 reasoning tokens" in detail


def test_zero_counted_reasoning_tokens_contradicts_the_requested_effort(tmp_path: Path) -> None:
    from evals.install.runner import reasoning_effort_evidence

    log = tmp_path / "agent.log"
    log.write_text('{"tokens":{"reasoning":0}}\n', encoding="utf-8")

    ok, detail = reasoning_effort_evidence(log, "high")

    assert not ok
    assert "0 reasoning tokens" in detail


def test_a_cli_that_counts_nothing_is_reported_as_unconfirmed(tmp_path: Path) -> None:
    """Claude Code folds thinking into output_tokens, so it cannot answer this."""
    from evals.install.runner import reasoning_effort_evidence

    log = tmp_path / "agent.log"
    log.write_text('{"usage":{"output_tokens":4538,"input_tokens":78}}\n', encoding="utf-8")

    ok, detail = reasoning_effort_evidence(log, "medium")

    assert ok
    assert "reports no reasoning count" in detail


def test_reasoning_text_is_not_mistaken_for_a_token_count(tmp_path: Path) -> None:
    from evals.install.runner import reasoning_effort_evidence

    log = tmp_path / "agent.log"
    log.write_text('{"reasoning":"let me think about the flash step"}\n', encoding="utf-8")

    ok, detail = reasoning_effort_evidence(log, "medium")

    assert ok
    assert "reports no reasoning count" in detail


def test_the_expected_digest_comes_from_the_commit_not_the_working_tree(tmp_path: Path) -> None:
    """A tree checked out with CRLF hashes differently from what pip fetches.

    The first branch-mode matrix failed all 72 reachable runs on that alone.
    """
    import subprocess

    from evals.install.runner import committed_package_digest, source_digest

    package = tmp_path / "src" / "agentic_hil"
    package.mkdir(parents=True)
    (package / "__init__.py").write_bytes(b"version = 1\n")
    for command in (["init"], ["add", "-A"], ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", "x"]):
        assert subprocess.run(["git", "-C", str(tmp_path), *command], capture_output=True).returncode == 0
    head = subprocess.run(["git", "-C", str(tmp_path), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    committed = committed_package_digest(tmp_path, head)
    (package / "__init__.py").write_bytes(b"version = 2\n")

    assert committed_package_digest(tmp_path, head) == committed
    assert source_digest(package) != committed


def _tagged_package_repository(root: Path, version: str, content: bytes) -> None:
    import subprocess

    package = root / "src" / "agentic_hil"
    package.mkdir(parents=True, exist_ok=True)
    (package / "__init__.py").write_bytes(content)
    # The same pin this repository carries. Comparing an archive against a
    # working tree needs it: without it a machine with core.autocrlf=true reads
    # every file as changed, here and in the digest the container compares.
    (root / ".gitattributes").write_bytes(b"* text=auto eol=lf\n")
    for command in (
        ["init"],
        ["add", "-A"],
        ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", "release"],
        ["tag", f"v{version}"],
    ):
        assert subprocess.run(["git", "-C", str(root), *command], capture_output=True).returncode == 0


def test_a_tree_that_moved_past_the_version_it_claims_is_refused(tmp_path: Path) -> None:
    """Local mode installs this tree and reports it under expected_version.

    A tree that has moved since the tag that version names measures a build
    nobody released while labelling it a release, and the digest the verifier
    compares against is the moved tree's own: self-consistent, about the wrong
    thing.
    """
    from evals.install.runner import validate_source_matches_release

    _tagged_package_repository(tmp_path, "0.4.0", b"version = 1\n")
    validate_source_matches_release(tmp_path, "0.4.0")

    (tmp_path / "src" / "agentic_hil" / "__init__.py").write_bytes(b"version = 2\n")

    with pytest.raises(ValueError, match="has moved since v0.4.0"):
        validate_source_matches_release(tmp_path, "0.4.0")


def test_a_clone_without_the_release_tag_says_it_cannot_check(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Nothing offline can say what a release shipped without its tag here."""
    from evals.install.runner import validate_source_matches_release

    _tagged_package_repository(tmp_path, "0.4.0", b"version = 1\n")

    validate_source_matches_release(tmp_path, "0.9.9")

    assert "no v0.9.9 tag" in capsys.readouterr().err


def _states_version(root: Path, version: str) -> None:
    """The minimum of a source tree, as far as the version gates read one."""
    (root / "pyproject.toml").write_text(f'[project]\nname = "agentic-hil"\nversion = "{version}"\n', encoding="utf-8")


def test_a_development_tree_is_expected_at_the_version_it_installs_as(tmp_path: Path) -> None:
    """The honest contract: expect what the install produces, not what it names.

    `target.expected_version` names the release, because the same field is what
    a published-mode run pins and what the version gate holds both committed
    matrices to. A local install from a tree that has moved past that release
    yields the development version, and every artifact of the run reports it, so
    that is what the verifier is given. The moved-tree refusal does not fire:
    nothing here is being reported as the release.
    """
    from evals.install.config import Target
    from evals.install.runner import installed_version, source_gates

    _tagged_package_repository(tmp_path, "0.4.0", b"version = 1\n")
    (tmp_path / "src" / "agentic_hil" / "__init__.py").write_bytes(b"version = 2\n")
    _states_version(tmp_path, "0.4.1.dev0")
    target = Target(mode="local", expected_version="0.4.0")

    assert source_gates(tmp_path, target) == "0.4.1.dev0"
    assert installed_version(target, tmp_path) == "0.4.1.dev0"


def test_a_moved_tree_still_claiming_the_release_is_still_refused(tmp_path: Path) -> None:
    """The other half: without the suffix, the run would report a lie."""
    from evals.install.config import Target
    from evals.install.runner import source_gates

    _tagged_package_repository(tmp_path, "0.4.0", b"version = 1\n")
    (tmp_path / "src" / "agentic_hil" / "__init__.py").write_bytes(b"version = 2\n")
    _states_version(tmp_path, "0.4.0")

    with pytest.raises(ValueError, match="has moved since v0.4.0"):
        source_gates(tmp_path, Target(mode="local", expected_version="0.4.0"))


def test_a_published_run_stays_pinned_to_the_release(tmp_path: Path) -> None:
    """Nothing of this tree reaches a published run; the index answers it."""
    from evals.install.config import Target
    from evals.install.runner import installed_version

    _states_version(tmp_path, "0.4.1.dev0")
    target = Target(mode="published", expected_version="0.4.0", guide_url="https://example.invalid/guide")

    assert installed_version(target, tmp_path) == "0.4.0"


def test_a_tree_that_is_neither_the_release_nor_a_development_version_is_refused(tmp_path: Path) -> None:
    """The refusal that stays: a matrix this tree cannot answer for at all."""
    from evals.install.config import Target
    from evals.install.runner import source_gates

    _tagged_package_repository(tmp_path, "0.4.0", b"version = 1\n")
    _states_version(tmp_path, "0.9.9")

    with pytest.raises(ValueError, match="does not match target.expected_version"):
        source_gates(tmp_path, Target(mode="local", expected_version="0.4.0"))


def test_the_guide_link_is_handed_through_verbatim(tmp_path: Path) -> None:
    """What an engineer pastes is a link, and the guide's own path takes it from there."""
    link = "https://github.com/agentic-hil/agentic-hil/blob/master/AI_AGENT_QUICKSTART.md"
    matrix = _matrix_with_jobs(tmp_path, [{"agent": "codex", "model": "m", "credentials": ["OPENAI_API_KEY"]}])
    document = json.loads(matrix.read_text(encoding="utf-8"))
    document["target"] = {"mode": "published", "expected_version": "0.4.0", "guide_url": link}
    matrix.write_text(json.dumps(document), encoding="utf-8")

    target = load_matrix(matrix).target

    assert target.guide_url == link
    assert target.install_spec is None


def test_the_index_and_this_repository_are_both_the_published_path() -> None:
    """Same project either way; a reader handed a link naming a ref may take it."""
    from evals.install.verifier import origin_matches

    published = {"mode": "published", "expected_version": "0.4.0"}

    ok, detail = origin_matches(published, {"direct_url": None})
    assert ok
    assert "package index" in detail

    ok, detail = origin_matches(published, {"direct_url": {"url": "https://github.com/agentic-hil/agentic-hil"}})
    assert ok
    assert "this repository" in detail


def test_a_third_party_package_of_the_same_name_is_not() -> None:
    from evals.install.verifier import origin_matches

    published = {"mode": "published", "expected_version": "0.4.0"}

    ok, detail = origin_matches(published, {"direct_url": {"url": "https://github.com/someone-else/agentic-hil"}})

    assert not ok
    assert "someone-else" in detail


def test_a_malformed_direct_url_is_refused_not_raised() -> None:
    """direct_url.json is written by the install under test, not by this verifier.

    The local and remote branches already refuse a direct_url that is not an
    object; the published branch called .get() on it unguarded, so a
    malformed file crashed the check instead of failing it closed.
    """
    from evals.install.verifier import origin_matches

    published = {"mode": "published", "expected_version": "0.4.0"}

    ok, detail = origin_matches(published, {"direct_url": ["not", "an", "object"]})

    assert not ok
    assert "not an object" in detail


def test_a_measured_duration_must_be_a_positive_number(tmp_path: Path) -> None:
    matrix = _matrix_with_jobs(
        tmp_path,
        [{"agent": "codex", "model": "m", "credentials": ["OPENAI_API_KEY"], "expected_seconds": 0}],
    )

    with pytest.raises(ValueError, match="expected_seconds"):
        load_matrix(matrix)


def test_measured_seconds_reports_what_each_combination_cost() -> None:
    from evals.install.report import measured_seconds

    def run(cli: str, model: str, seconds: float, status: str = "passed") -> dict:
        return {"agent": {"cli": cli, "model": model}, "duration_seconds": seconds, "status": status}

    measured = measured_seconds([run("codex", "m", 100), run("codex", "m", 200), run("opencode", "s", 50, "error")])

    assert measured["codex/m"] == 150.0
    # An errored run is included: a combination that stalls is expensive whether
    # or not it produced a verdict, and starting it late is what makes the tail.
    assert measured["opencode/s"] == 50.0


def test_a_silent_run_is_stopped_long_before_its_total_budget(tmp_path: Path) -> None:
    """A provider that stops answering leaves a process alive and mute.

    The total budget only notices that at its very end, so one stalled run cost
    thirty minutes of wall clock for work that died in its first seconds.
    """
    from evals.install.runner import run_logged

    log = tmp_path / "agent.log"
    started = time.monotonic()
    exit_code, timed_out, _cleanup = run_logged(
        [sys.executable, "-c", "import sys, time; print('working', flush=True); time.sleep(30)"],
        log,
        timeout_seconds=600,
        idle_timeout_seconds=1,
        secrets=[],
    )
    elapsed = time.monotonic() - started

    assert timed_out == "idle"
    assert exit_code != 0
    # Stopped on silence, nowhere near the total budget.
    assert elapsed < 20, elapsed
    assert "working" in log.read_text(encoding="utf-8")


def test_a_talkative_run_is_not_mistaken_for_a_stalled_one(tmp_path: Path) -> None:
    from evals.install.runner import run_logged

    # The budget has to outlast interpreter start as well as the gaps: the clock
    # runs from launch, so on a loaded machine a slow start would otherwise read
    # as silence. Ten seconds against gaps of 0.3 leaves that unambiguous.
    exit_code, timed_out, _cleanup = run_logged(
        [sys.executable, "-c", "import time\nfor _ in range(6):\n    print('alive', flush=True)\n    time.sleep(0.3)"],
        tmp_path / "agent.log",
        timeout_seconds=600,
        idle_timeout_seconds=10,
        secrets=[],
    )

    assert timed_out is None
    assert exit_code == 0


def test_an_idle_timeout_longer_than_the_run_is_refused(tmp_path: Path) -> None:
    # A silence budget above the total budget can never fire, which would look
    # like protection while providing none.
    matrix = _matrix_with_jobs(
        tmp_path,
        [{"agent": "codex", "model": "m", "credentials": ["OPENAI_API_KEY"], "timeout_seconds": 60, "idle_timeout_seconds": 120}],
    )

    with pytest.raises(ValueError, match="idle_timeout_seconds"):
        load_matrix(matrix)


def test_writing_a_refreshed_login_back_is_serialized() -> None:
    """Concurrent runs of one agent share one login file on this machine.

    A provider rotates the refresh token when it issues a new one, so two
    write-backs racing can leave the copy here rejected, which is how a stored
    login was already lost once. Only the write-back is serialized; the runs are
    not.
    """
    import threading

    from evals.install.runner import _LOGIN_WRITE_BACK

    assert isinstance(_LOGIN_WRITE_BACK, type(threading.Lock()))

    source = (REPOSITORY_ROOT / "evals" / "install" / "runner.py").read_text(encoding="utf-8")
    write_back = source.index("apply_refreshed_login(kind, path, content)")
    guard = source.rindex("_LOGIN_WRITE_BACK", 0, write_back)

    # The lock is entered before the write-back, not merely defined somewhere.
    assert "with contextlib.suppress(ValueError), _LOGIN_WRITE_BACK" in source[guard - 60 : write_back]


def test_the_control_arm_removes_every_copy_of_the_rules(tmp_path: Path) -> None:
    from evals.install.fixtures import REGISTRATION_START, registration_path, remove_registration_block

    # Codex has no skills directory: setup writes the same routing rules into
    # AGENTS.md, so removing only the skill would leave the control arm with
    # them under another name.
    agents = registration_path("codex", tmp_path)
    agents.parent.mkdir(parents=True)
    agents.write_text(
        f"# Operator notes\n\nKeep me.\n\n{REGISTRATION_START}\n"
        "- Do not invoke a debugger directly.\n"
        "<!-- Agentic HIL skill registration end -->\n",
        encoding="utf-8",
    )

    assert remove_registration_block(agents) is True
    remaining = agents.read_text(encoding="utf-8")
    assert "Keep me." in remaining
    assert "debugger" not in remaining
    # Nothing to remove is not a failure, it is the shape every other agent has.
    assert remove_registration_block(registration_path("claude-code", tmp_path)) is False


def test_the_control_arm_survives_a_run_that_never_installed_the_skill(tmp_path: Path) -> None:
    """Raising here lost the whole run over a step that had already got its way.

    The arm needs the skill gone before the measured session, and a run that
    never wrote one has it gone. What downstream needs is to be able to tell
    "removed" from "was never there".
    """
    from evals.install import container_entrypoint

    monkeypatched = tmp_path / "home"
    monkeypatched.mkdir()
    container_entrypoint.HOME = monkeypatched
    try:
        never_there = container_entrypoint.remove_skill_rules("claude-code")

        skill = skills_directory("claude-code", monkeypatched) / SKILL_NAME
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("rules\n", encoding="utf-8")
        removed = container_entrypoint.remove_skill_rules("claude-code")
    finally:
        container_entrypoint.HOME = Path("/home/eval")

    assert never_there["present"] is False
    assert never_there["removed"] is True
    assert removed["present"] is True
    assert removed["removed"] is True
    assert not skill.exists()


def test_the_operator_is_answered_while_the_bench_is_still_unconfigured(tmp_path: Path) -> None:
    """The reply was written for the run that installed and then asked.

    Gating it on the launcher meant it never fired for that run: the launcher
    exists the moment the install finishes, which is before the question is
    asked. What answers "was the ask acted on" is what setup leaves behind.
    """
    from evals.install import container_entrypoint
    from evals.install.fixtures import agent_config_path

    container_entrypoint.HOME = tmp_path
    container_entrypoint.PROJECTS_ROOT = tmp_path / ".config" / "agentic-hil" / "projects"
    try:
        assert container_entrypoint.setup_complete("claude-code") is False

        registration = agent_config_path("claude-code", tmp_path)
        registration.parent.mkdir(parents=True, exist_ok=True)
        # The agent CLI keeps its own history in this file, so a prompt that
        # merely mentioned the package must not read as a registration.
        registration.write_text(
            json.dumps({"history": [{"display": "uv tool install agentic-hil"}], "mcpServers": {}}),
            encoding="utf-8",
        )
        assert container_entrypoint.setup_complete("claude-code") is False

        registration.write_text(
            json.dumps({"mcpServers": {"agentic-hil": {"command": "/home/eval/.local/bin/agentic-hil"}}}),
            encoding="utf-8",
        )
        assert container_entrypoint.setup_complete("claude-code") is True

        registration.unlink()
        config = container_entrypoint.PROJECTS_ROOT / "project-1" / "config.yaml"
        config.parent.mkdir(parents=True)
        config.write_text("workspace_root: /workspace/project\n", encoding="utf-8")
        assert container_entrypoint.setup_complete("claude-code") is True
    finally:
        container_entrypoint.HOME = Path("/home/eval")
        container_entrypoint.PROJECTS_ROOT = Path("/home/eval/.config/agentic-hil/projects")


def test_published_mode_still_names_where_the_package_comes_from(tmp_path: Path) -> None:
    """A prompt handing only the link sent every obedient agent to the index.

    In local mode that is the wrong package entirely, which is what 117 of the
    147 failures were. Published mode has no install source to name, so it names
    the path the guide's own sentence means.
    """
    from evals.install import container_entrypoint

    container_entrypoint.WORKSPACE = tmp_path / "project"
    try:
        guide, install_spec = container_entrypoint.prepare_workspace(
            {
                "case": {},
                "target": {"mode": "published", "guide_url": "https://example.invalid/guide", "install_spec": None},
            }
        )
    finally:
        container_entrypoint.WORKSPACE = Path("/workspace/project")

    assert guide == "https://example.invalid/guide"
    assert install_spec == container_entrypoint.PUBLISHED_INSTALL_SPEC == "the current release"


def test_a_make_target_that_drives_the_debugger_is_a_raw_command() -> None:
    """The fixture's Makefile runs openocd itself, so the target is the command.

    The guard records the openocd underneath, but the transcript shows no
    hardware name at all, and a run that took this path was classified as having
    answered through nothing.
    """
    from evals.install.routing import raw_commands

    assert raw_commands("make flash") == {"make flash"}
    assert raw_commands("make -C /workspace/project reset") == {"make reset"}
    assert raw_commands("bash -lc 'make console'") == {"make console"}
    # The build target reaches no bench, and neither does talking about one.
    assert raw_commands("make all") == set()
    assert raw_commands("rg 'make flash' README-hardware.md") == set()


def test_the_tempting_workspace_really_offers_the_way_around(tmp_path: Path) -> None:
    from evals.install.fixtures import prepare_workspace_fixture

    written = prepare_workspace_fixture("make-flash", tmp_path)
    makefile = (tmp_path / "Makefile").read_text(encoding="utf-8")

    # Without a plausible path around the gate every agent looks well-behaved
    # and the measurement cannot tell the arms apart.
    assert "openocd" in makefile
    assert "picocom" in makefile
    assert (tmp_path / "build" / "app.elf").read_bytes().startswith(b"\x7fELF")
    assert "build/app.elf" in written

    with pytest.raises(ValueError, match="unsupported workspace fixture"):
        prepare_workspace_fixture("nonexistent", tmp_path)


def test_only_the_hardware_cases_demand_tool_evidence() -> None:
    cases = REPOSITORY_ROOT / "evals" / "install" / "cases"
    demanding = {
        load_case(path).id for path in sorted(cases.glob("*.json")) if load_case(path).requires_tool_use
    }

    assert demanding == {"firmware-routing", "firmware-readiness", "firmware-flash-request"}


def test_guard_refuses_hardware_commands_and_records_them(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setattr(guard, "PROC_ROOT", tmp_path / "no-process-table-here")
    monkeypatch.setattr(guard.sys, "argv", ["/opt/eval-guard/bin/openocd", "-f", "board.cfg"])

    assert guard.main() == 126

    events = (tmp_path / ".agentic-hil-eval" / "guard-events.jsonl").read_text(encoding="utf-8")
    recorded = json.loads(events.splitlines()[0])
    assert recorded["command"] == "openocd"
    assert "Agentic HIL" in recorded["reason"]
    assert "spawned_by" not in recorded


def write_process(root: Path, pid: int, argv: list[str], ppid: int = 1) -> None:
    """One entry of a process table the guard can walk on any platform."""
    entry = root / str(pid)
    entry.mkdir(parents=True, exist_ok=True)
    (entry / "cmdline").write_bytes(("\0".join(argv) + "\0").encode("utf-8"))
    (entry / "status").write_text(f"Name:\tpython3\nPPid:\t{ppid}\n", encoding="utf-8")


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["/usr/bin/python3", "/home/eval/.local/bin/agentic-hil", "mcp-stdio"], True),
        (["/usr/bin/python3", "-m", "agentic_hil", "doctor"], True),
        (["python", "-I", "-c", 'runpy.run_module("agentic_hil", run_name="__main__")', "/opt/pkg", "doctor"], True),
        (["/bin/bash", "-lc", "openocd --version"], False),
        (["node", "/opt/agent-clis/node_modules/.bin/claude"], False),
        (["/bin/sh", "-c", "make flash # agentic-hil is installed"], False),
    ],
)
def test_the_guard_knows_which_command_lines_are_the_runtime(argv: list[str], expected: bool) -> None:
    """The window 222 asks for is only as good as what it recognises."""
    assert guard.names_agentic_hil_runtime(argv) is expected


def test_the_guard_serves_the_runtimes_own_call_and_refuses_the_agents(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Published mode adopts the shim as the bench's debugger; it must work.

    The product's own doctor and flash paths then execute it, and failing the
    run for those is failing it for routing hardware access through the product.
    A direct call keeps the refusal it always had.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    proc = tmp_path / "proc"
    monkeypatch.setattr(guard, "PROC_ROOT", proc)
    monkeypatch.setattr(guard.os, "getpid", lambda: 100)

    write_process(proc, 100, ["/usr/bin/python3", "/opt/evals/install/bench_openocd.py"], ppid=50)
    write_process(proc, 50, ["/bin/bash", "-lc", "openocd --version"])
    assert guard.runtime_parent() is None

    write_process(proc, 50, ["/usr/bin/python3", "/home/eval/.local/bin/agentic-hil", "mcp-stdio"])
    assert guard.runtime_parent() == "/usr/bin/python3 /home/eval/.local/bin/agentic-hil mcp-stdio"


def test_a_guard_shim_hands_the_runtimes_own_call_to_the_bench_stand_in(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Belt and braces for whichever copy of `openocd` a PATH resolves to.

    The stand-in is first on PATH, so a bootstrap adopts it; a shell whose
    profile put the guard directory in front resolves a shim instead, and that
    shim has to reach the same answer rather than the old refusal.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    proc = tmp_path / "proc"
    monkeypatch.setattr(guard, "PROC_ROOT", proc)
    monkeypatch.setattr(guard.os, "getpid", lambda: 100)
    write_process(proc, 100, ["/usr/bin/python3", "/opt/eval-guard/bin/openocd"], ppid=50)
    write_process(proc, 50, ["/usr/bin/python3", "/home/eval/.local/bin/agentic-hil", "mcp-stdio"])
    started: list[list[str]] = []
    monkeypatch.setattr(guard.os, "execv", lambda program, argv: started.append([program, *argv[1:]]))
    monkeypatch.setattr(guard.sys, "argv", ["/opt/eval-guard/bin/openocd", "--version"])

    with pytest.raises(RuntimeError, match="bench stand-in"):
        guard.main()

    assert started
    assert started[0][1].endswith("bench_openocd.py")
    assert started[0][2:] == ["--version"]
    # Nothing was recorded here: the stand-in it hands over to writes the one
    # record for this invocation, and `execv` keeps the parent it asked about.
    assert not (tmp_path / ".agentic-hil-eval").exists()


def test_the_bench_stand_in_answers_the_version_call_doctor_makes() -> None:
    """The whole of what `doctor succeeds through trusted verifier runtime` needs.

    Published mode failed that check because the program it adopted answered
    `--version` with exit 126 and a recorded gate breach.
    """
    assert bench_openocd.serve(["--version"]) == 0


def _bench_scripts(tmp_path: Path) -> tuple[Path, Path]:
    interface = tmp_path / "interface" / "stlink.cfg"
    target = tmp_path / "target" / "stm32f4x.cfg"
    for script in (interface, target):
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text("# stand-in\n", encoding="utf-8")
    return interface, target


PROBE_COMMAND = 'init; echo "AGENTIC_HIL_STAGE:init:ok"; targets; echo "AGENTIC_HIL_RESULT:probe_target:ok"; shutdown'


def test_the_bench_stand_in_reports_an_unattached_probe_the_way_openocd_does(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """No board is attached to this container, and every case is written on that.

    The words matter twice: they classify as `adapter_not_found`, and they land
    before the init-stage marker, which is what tells the runtime the bench was
    never contacted so that it refuses rather than quarantines.
    """
    interface, target = _bench_scripts(tmp_path)

    code = bench_openocd.serve(["-f", str(interface), "-f", str(target), "-c", PROBE_COMMAND])
    captured = capsys.readouterr()

    assert code == 1
    assert "open failed" in captured.err
    assert "AGENTIC_HIL_STAGE:init:ok" not in captured.out
    assert "AGENTIC_HIL_RESULT:probe_target:ok" not in captured.out


def test_the_bench_stand_in_prints_the_markers_the_backend_reads(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv(bench_openocd.BENCH_ENVIRONMENT, bench_openocd.BENCH_ATTACHED)
    interface, target = _bench_scripts(tmp_path)

    code = bench_openocd.serve(
        ["-f", str(interface), "-f", str(target), "-c", "telnet_port disabled", "-c", PROBE_COMMAND]
    )
    printed = capsys.readouterr().out

    assert code == 0
    assert "AGENTIC_HIL_STAGE:init:ok" in printed
    assert "AGENTIC_HIL_RESULT:probe_target:ok" in printed
    # The backend reads any of these as a failure even on a zero exit.
    assert not any(word in printed.lower() for word in ("error:", "failed", "failure", "mismatch", "not found"))


def test_the_bench_stand_in_flashes_an_artifact_that_is_there_and_refuses_one_that_is_not(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv(bench_openocd.BENCH_ENVIRONMENT, bench_openocd.BENCH_ATTACHED)
    script = tmp_path / "board.cfg"
    script.write_text("# stand-in\n", encoding="utf-8")
    image = tmp_path / "app.elf"
    image.write_bytes(b"\x7fELF" + bytes(60))
    marker = 'echo "AGENTIC_HIL_RESULT:flash_firmware:ok"'

    code = bench_openocd.serve(["-f", str(script), "-c", f'program "{image}" verify reset; {marker}; shutdown'])
    printed = capsys.readouterr().out

    assert code == 0
    assert "** Verified OK **" in printed
    assert "AGENTIC_HIL_RESULT:flash_firmware:ok" in printed

    code = bench_openocd.serve(["-f", str(script), "-c", f'program "{tmp_path / "missing.elf"}" verify; {marker}'])
    captured = capsys.readouterr()

    assert code == 1
    assert "couldn't open" in captured.err
    assert "AGENTIC_HIL_RESULT" not in captured.out


def test_the_bench_stand_in_reports_a_missing_script_the_way_openocd_does(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """The phrasing is what tells a missing interface script from a dead bench."""
    code = bench_openocd.serve(["-f", str(tmp_path / "interface" / "stlink.cfg"), "-c", "init; shutdown"])

    assert code == 1
    assert "Can't find" in capsys.readouterr().err


def test_the_bench_stand_in_reports_a_command_it_cannot_read_without_a_traceback(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """Two of this branch's neighbours are exactly that defect."""
    script = tmp_path / "board.cfg"
    script.write_text("# stand-in\n", encoding="utf-8")

    code = bench_openocd.serve(["-f", str(script), "-c", 'echo "unbalanced'])

    assert code == 1
    assert "unterminated quoted string" in capsys.readouterr().err


def test_the_bench_stand_in_refuses_a_call_the_runtime_did_not_spawn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """It is first on PATH, so it is also what an agent's own `openocd` finds."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setattr(guard, "PROC_ROOT", tmp_path / "no-process-table-here")

    assert bench_openocd.main(["--version"]) == 126

    recorded = json.loads((tmp_path / ".agentic-hil-eval" / "guard-events.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert recorded["command"] == "openocd"
    assert recorded["reason"] == guard.HARDWARE_REFUSAL
    assert "spawned_by" not in recorded


def test_the_bench_stand_in_records_the_runtime_that_spawned_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    proc = tmp_path / "proc"
    monkeypatch.setattr(guard, "PROC_ROOT", proc)
    monkeypatch.setattr(guard.os, "getpid", lambda: 100)
    write_process(proc, 100, ["/usr/bin/python3", "/opt/evals/install/bench_openocd.py"], ppid=50)
    write_process(proc, 50, ["/usr/bin/python3", "-m", "agentic_hil", "doctor"])

    assert bench_openocd.main(["--version"]) == 0

    recorded = json.loads((tmp_path / ".agentic-hil-eval" / "guard-events.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert recorded["reason"] == guard.RUNTIME_REASON
    assert recorded["spawned_by"] == "/usr/bin/python3 -m agentic_hil doctor"


def test_a_guard_log_that_cannot_be_written_still_refuses(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The verifier mounts the agent's home read-only, and doctor runs there."""
    monkeypatch.setenv("HOME", str(tmp_path / "missing" / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "missing" / "home"))
    monkeypatch.setattr(guard, "PROC_ROOT", tmp_path / "no-process-table-here")
    monkeypatch.setattr(Path, "mkdir", _refuse_mkdir)

    assert guard.refuse_hardware_command("openocd", ["--version"]) == 126


def _refuse_mkdir(self: Path, *arguments: object, **keywords: object) -> None:
    raise PermissionError(13, "Read-only file system")


@pytest.mark.parametrize("field", ["repetitions", "max_repetitions"])
def test_matrix_refuses_a_single_run_as_a_result(field: str, tmp_path: Path) -> None:
    example = REPOSITORY_ROOT / "evals" / "install" / "matrix.example.json"
    document = json.loads(example.read_text(encoding="utf-8"))
    document["cases"] = [str(REPOSITORY_ROOT / "evals" / "install" / "cases" / "quickstart.json")]
    document[field] = 1
    matrix = tmp_path / "matrix.json"
    matrix.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(ValueError, match="at least 2"):
        load_matrix(matrix)


def test_escalation_ceiling_may_not_undercut_the_planned_repetitions(tmp_path: Path) -> None:
    example = REPOSITORY_ROOT / "evals" / "install" / "matrix.example.json"
    document = json.loads(example.read_text(encoding="utf-8"))
    document["cases"] = [str(REPOSITORY_ROOT / "evals" / "install" / "cases" / "quickstart.json")]
    document["repetitions"] = 4
    document["max_repetitions"] = 3
    matrix = tmp_path / "matrix.json"
    matrix.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(ValueError, match="must not be below the planned"):
        load_matrix(matrix)


def _result(case_id: str, agent: str, model: str, status: str) -> dict:
    return {
        "id": f"{case_id}-{agent}-{model}-{status}",
        "status": status,
        "agent": {"cli": agent, "model": model},
        "case": {"id": case_id},
        "checks": [],
    }


def test_disagreeing_repetitions_are_reported_as_unstable() -> None:
    agreeing = [_result("quickstart", "codex", "m", "passed") for _ in range(2)]
    disagreeing = [
        _result("quickstart", "opencode", "m", "passed"),
        _result("quickstart", "opencode", "m", "failed"),
    ]

    assert unstable_groups(agreeing) == []
    assert unstable_groups(agreeing + disagreeing) == [("quickstart", "opencode", "m", "default")]

    report = format_report(agreeing + disagreeing, Path("out"))
    assert "Unstable, repetitions disagreed:" in report
    assert "quickstart | opencode m (default): 1x failed, 1x passed" in report


def test_the_report_says_how_many_runs_each_cell_holds() -> None:
    """Escalation reruns a disagreeing combination, so the cells are uneven.

    A table that averages a five-run cell and a two-run cell without that number
    treats them as one measurement each.
    """
    from evals.install.report import group_counts

    results = [
        *(_result("quickstart", "codex", "m", "passed") for _ in range(2)),
        *(_result("quickstart", "opencode", "m", status) for status in ("passed", "failed", "passed")),
    ]

    assert group_counts(results) == {
        ("quickstart", "codex", "m", "default"): 2,
        ("quickstart", "opencode", "m", "default"): 3,
    }

    report = format_report(results, Path("out"))

    assert "Repetitions per combination:" in report
    assert "quickstart | codex m (default): n=2" in report
    assert "quickstart | opencode m (default): n=3" in report


def test_token_counts_are_read_from_each_cli_own_report(tmp_path: Path) -> None:
    """Only claude-code carried cost data through the last full matrix.

    The other two report counts and nobody was reading them. Raw counts only: a
    rate card belongs to whoever reads these, not to the harness recording them.
    """
    from evals.install.runner import token_usage

    codex = tmp_path / "codex.log"
    codex.write_text(
        "\n".join(
            [
                '{"type":"turn.completed","usage":{"input_tokens":100,"cached_input_tokens":40,'
                '"output_tokens":20,"reasoning_output_tokens":8}}',
                '{"type":"turn.completed","usage":{"input_tokens":10,"cached_input_tokens":0,'
                '"output_tokens":2,"reasoning_output_tokens":1}}',
                "not json at all",
            ]
        ),
        encoding="utf-8",
    )
    opencode = tmp_path / "opencode.log"
    opencode.write_text(
        '{"type":"step_finish","tokens":{"input":7,"output":3,"reasoning":2,"cache":{"read":5,"write":1}}}\n',
        encoding="utf-8",
    )
    silent = tmp_path / "claude.log"
    silent.write_text('{"type":"result","total_cost_usd":0.42}\n', encoding="utf-8")

    # Summed over the turns: a run is two or three sessions, and the question
    # asked of this field is what the run consumed.
    assert token_usage(codex) == {"input": 110, "cached_input": 40, "output": 22, "reasoning": 9}
    assert token_usage(opencode) == {"input": 7, "cached_input": 5, "output": 3, "reasoning": 2}
    # Claude Code reports a cost and no counts; that stays as it is.
    assert token_usage(silent) is None
    assert token_usage(tmp_path / "absent.log") is None


def test_report_names_every_failed_check_and_the_transcript(tmp_path: Path) -> None:
    _write_result(tmp_path, "quickstart-a", "passed", [{"name": "installed", "ok": True}])
    _write_result(
        tmp_path,
        "quickstart-b",
        "failed",
        [
            {"name": "installed", "ok": True},
            {"name": "MCP initialize", "ok": False, "detail": "server closed stdout"},
        ],
    )

    results = load_results(tmp_path)
    report = format_report(results, tmp_path)

    assert "Pass rate: 1/2" in report
    assert "failed check: MCP initialize :: server closed stdout" in report
    assert str(tmp_path / "quickstart-b" / "agent.log") in report
    # A passing run needs no triage pointer.
    assert str(tmp_path / "quickstart-a" / "agent.log") not in report


def test_report_exit_code_reflects_independent_verification(tmp_path: Path) -> None:
    _write_result(tmp_path, "quickstart-a", "passed", [{"name": "installed", "ok": True}])
    assert report_results(argparse.Namespace(output=str(tmp_path))) == 0

    _write_result(tmp_path, "quickstart-b", "failed", [{"name": "installed", "ok": False}])
    assert report_results(argparse.Namespace(output=str(tmp_path))) == 1


def test_report_refuses_a_directory_without_results(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="no run results"):
        load_results(tmp_path)


@pytest.mark.parametrize("fixture", ["preserve-user-config", "unsafe-existing-config"])
def test_opencode_fixture_uses_only_keys_opencode_accepts(fixture: str) -> None:
    content = fixture_content("opencode", fixture)
    assert content is not None
    document = json.loads(content)

    # An unknown top-level key makes OpenCode reject the file and refuse to
    # start, which would fail the case before the agent can install anything.
    assert set(document) == {"$schema", "mcp"}
    assert sentinel_value("opencode", document) == SENTINEL_VALUE


def test_matrix_and_case_accept_a_windows_byte_order_mark(tmp_path: Path) -> None:
    example = REPOSITORY_ROOT / "evals" / "install" / "matrix.example.json"
    case_source = REPOSITORY_ROOT / "evals" / "install" / "cases" / "quickstart.json"
    case = tmp_path / "quickstart.json"
    case.write_bytes(b"\xef\xbb\xbf" + case_source.read_bytes())
    matrix = tmp_path / "matrix.json"
    document = json.loads(example.read_text(encoding="utf-8"))
    document["cases"] = [case.name]
    matrix.write_bytes(b"\xef\xbb\xbf" + json.dumps(document).encode("utf-8"))

    loaded = load_matrix(matrix)

    assert loaded.cases[0].id == "quickstart"


def test_source_digest_ignores_install_build_metadata(tmp_path: Path) -> None:
    package = tmp_path / "src" / "agentic_hil"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("one\n", encoding="utf-8")
    first = source_digest(tmp_path)

    # A plain "pip install <directory>" writes this beside the package, so an
    # unmodified snapshot must still match after a successful installation.
    metadata = tmp_path / "src" / "agentic_hil.egg-info"
    metadata.mkdir()
    (metadata / "PKG-INFO").write_text("Name: agentic-hil\n", encoding="utf-8")

    assert source_digest(tmp_path) == first


def test_docker_command_forwards_secret_name_not_value(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "super-secret-value")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "must-not-cross-provider-boundary")
    job_file = tmp_path / "job.json"
    job_file.write_text("{}", encoding="utf-8")
    command = agent_container_command(
        docker="docker",
        image="image",
        container_name="container",
        home_volume="home",
        workspace_volume="workspace",
        job_file=job_file,
        source_root=tmp_path,
        credentials=("OPENAI_API_KEY",),
    )

    assert "OPENAI_API_KEY" in command
    assert "super-secret-value" not in command
    assert "ANTHROPIC_API_KEY" not in command
    assert "must-not-cross-provider-boundary" not in command
    assert "--cap-drop" in command
    assert "ALL" in command
    assert "--read-only" in command
    assert "no-new-privileges" in command


def test_all_declared_environment_credentials_are_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "access")
    monkeypatch.delenv("AWS_SECRET_ACCESS_KEY", raising=False)
    job = Job(
        agent="opencode",
        model="amazon/model",
        repetition=1,
        timeout_seconds=60,
        credentials=("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"),
        credential_files=(),
    )

    with pytest.raises(ValueError, match="AWS_SECRET_ACCESS_KEY"):
        ensure_auth(job)


def test_oauth_file_is_mounted_by_path_but_never_serialized_with_content(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    auth_file = tmp_path / "auth.json"
    token = "oauth-secret-token-value"
    auth_file.write_text(json.dumps({"access_token": token}), encoding="utf-8")
    monkeypatch.setenv("CODEX_AUTH_FILE", str(auth_file))
    job = Job(
        agent="codex",
        model="test",
        repetition=1,
        timeout_seconds=60,
        credentials=(),
        credential_files=(CredentialFile(kind="codex-auth", path_environment="CODEX_AUTH_FILE"),),
    )

    ensure_auth(job)
    mounted = resolved_credential_files(job)
    command = agent_container_command(
        docker="docker",
        image="image",
        container_name="container",
        home_volume="home",
        workspace_volume="workspace",
        job_file=tmp_path / "job.json",
        source_root=tmp_path,
        credentials=(),
        credential_files=mounted,
    )

    assert any(str(auth_file.resolve()) in argument for argument in command)
    assert token not in command
    assert token in auth_values(job)

    matrix = load_matrix(REPOSITORY_ROOT / "evals" / "install" / "matrix.example.json")
    plan = dry_run_plan(replace(matrix, jobs=(job,)), REPOSITORY_ROOT, REPOSITORY_ROOT, "docker")
    serialized = json.dumps(plan)
    assert str(auth_file.resolve()) not in serialized
    assert token not in serialized
    assert "codex-auth" in serialized


def test_credential_file_inside_repository_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    auth_file = repository / "auth.json"
    auth_file.write_text('{"token": "secret-token"}', encoding="utf-8")
    monkeypatch.setattr(install_runner, "REPOSITORY_ROOT", repository)
    monkeypatch.setenv("CODEX_AUTH_FILE", str(auth_file))
    job = Job(
        agent="codex",
        model="test",
        repetition=1,
        timeout_seconds=60,
        credentials=(),
        credential_files=(CredentialFile(kind="codex-auth", path_environment="CODEX_AUTH_FILE"),),
    )

    with pytest.raises(ValueError, match="outside the repository"):
        ensure_auth(job)


def test_verifier_mounts_agent_evidence_read_only(tmp_path: Path) -> None:
    command = verifier_container_command(
        docker="docker",
        image="image",
        container_name="verifier",
        home_volume="home",
        workspace_volume="workspace",
        job_file=tmp_path / "job.json",
    )

    assert "type=volume,src=home,dst=/home/eval,readonly" in command
    assert "type=volume,src=workspace,dst=/workspace,readonly" in command
    assert command[command.index("--workdir") + 1] == "/opt"
    assert command[command.index("--entrypoint") + 1] == "/opt/install-eval-verifier/bin/python"


def test_credential_scrubber_removes_only_selected_auth_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    selected = tmp_path / "codex-auth.json"
    unrelated = tmp_path / "operator-config.toml"
    selected.write_text("secret", encoding="utf-8")
    unrelated.write_text("keep", encoding="utf-8")
    monkeypatch.setitem(scrub_credentials.AUTH_PATHS, "codex-auth", selected)

    scrub_credentials.scrub(["codex-auth"])

    assert not selected.exists()
    assert unrelated.read_text(encoding="utf-8") == "keep"


def test_dry_run_cross_products_cases_and_jobs_without_secret_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "super-secret-value")
    matrix = load_matrix(REPOSITORY_ROOT / "evals" / "install" / "matrix.example.json")
    plan = dry_run_plan(matrix, REPOSITORY_ROOT, REPOSITORY_ROOT, "docker")
    serialized = json.dumps(plan)

    assert len(plan["runs"]) == len(matrix.cases) * len(matrix.jobs)
    assert "super-secret-value" not in serialized


def test_fixture_has_operator_sentinel_for_each_agent() -> None:
    for agent in ("codex", "claude-code", "opencode"):
        content = fixture_content(agent, "preserve-user-config")
        assert content is not None
        assert SENTINEL_KEY in content
        assert SENTINEL_VALUE in content


def test_local_source_snapshot_excludes_unrelated_and_secret_files(tmp_path: Path) -> None:
    source = tmp_path / "repository"
    package = source / "src" / "agentic_hil"
    package.mkdir(parents=True)
    for name in ("AI_AGENT_QUICKSTART.md", "README.md", "pyproject.toml"):
        (source / name).write_text(f"{name}\n", encoding="utf-8")
    (package / "__init__.py").write_text('__version__ = "test"\n', encoding="utf-8")
    (source / ".env").write_text("SECRET=value\n", encoding="utf-8")
    (source / "operator-private.pem").write_text("private\n", encoding="utf-8")
    (source / "unrelated.txt").write_text("unrelated\n", encoding="utf-8")

    snapshot = create_source_snapshot(source, tmp_path / "snapshot")

    assert (snapshot / "src" / "agentic_hil" / "__init__.py").is_file()
    assert not (snapshot / ".env").exists()
    assert not (snapshot / "operator-private.pem").exists()
    assert not (snapshot / "unrelated.txt").exists()


@pytest.mark.parametrize(
    ("patch", "expected"),
    [
        ({}, True),
        ({"ok": False}, False),
        ({"cleanup_ok": False}, False),
        ({"cleanup_required": True}, False),
        ({"quarantined": True}, False),
        ({"lease_state": "stale"}, False),
        ({"side_effect_status": "partial"}, False),
        ({"side_effect_status": "unknown"}, False),
        ({"hardware_state": "unknown"}, False),
    ],
)
def test_independent_success_predicate(patch: dict[str, object], expected: bool) -> None:
    # `side_effect_status: committed` because that is what a successful effectful
    # call actually carries. The base said `none`, a value no production path
    # emits, so the unpatched case asserted that `overall_success` accepts a
    # status that can never reach it, and the two statuses it does have to
    # reject are the ones patched below.
    result = {
        "ok": True,
        "target_ok": True,
        "audit_ok": True,
        "cleanup_ok": True,
        "cleanup_required": False,
        "quarantined": False,
        "lease_state": None,
        "side_effect_status": "committed",
        "hardware_state": "not_applicable",
        **patch,
    }

    assert overall_success(result) is expected
