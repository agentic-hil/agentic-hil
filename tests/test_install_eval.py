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

from evals.install import guard, scrub_credentials
from evals.install import runner as install_runner
from evals.install.adapters import adapter_for, build_agent_command
from evals.install.config import CredentialFile, Job, load_case, load_matrix
from evals.install.credentials import authentication_failure, credential_health
from evals.install.fixtures import SENTINEL_KEY, SENTINEL_VALUE, fixture_content, sentinel_value
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
    # executed. Where they disagree, the container's evidence decides — in both
    # directions, and absent evidence decides nothing.
    typed = {"followup": True, "raw_commands": ["openocd"], "mcp_calls": 1, "cli_calls": 0, "config_reads": 0}
    silent = {**typed, "raw_commands": []}

    assert route_of({**typed, "guard_triggered": False}) == "mcp"
    assert route_of({**typed, "guard_triggered": True}) == "raw"
    assert route_of({**typed, "guard_triggered": None}) == "raw"
    # A command the guard recorded but the transcript never spelled out.
    assert route_of({**silent, "guard_triggered": True}) == "raw"
    assert route_of({**silent, "guard_triggered": None}) == "mcp"


def test_guard_verdict_is_read_from_the_verification_result() -> None:
    from evals.install.routing import GUARD_CHECK, guard_triggered

    fired = {"checks": [{"name": GUARD_CHECK, "ok": False, "detail": "openocd"}]}
    silent = {"checks": [{"name": GUARD_CHECK, "ok": True, "detail": "none"}]}

    assert guard_triggered(fired) is True
    assert guard_triggered(silent) is False
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


def test_routing_gate_covers_the_treatment_arm_only() -> None:
    def entry(case_id: str, mcp_calls: int) -> dict:
        return {
            "group": (case_id, "codex", "model", "default"),
            "status": "passed",
            "followup": True,
            "mcp_calls": mcp_calls,
            "cli_calls": 0,
            "raw_commands": [],
            "config_reads": 1 if not mcp_calls else 0,
            "skill_referenced": bool(mcp_calls),
        }

    args = argparse.Namespace(output="unused")
    analysed = [entry("firmware-routing", 2), entry("firmware-routing-without-skill", 0)]

    # A control run that skips MCP is the measurement, not a regression.
    assert routing_results(args, analysed) == 0
    assert routing_results(args, [entry("firmware-routing", 0)]) == 1


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
    # Two levels of one model are two measurements, not a collision — and two
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
    # Two workers, one slot for each model: the fast queue drains while the
    # first slow run is still going, instead of queueing behind a stuck model.
    assert finished.count("fast") == fast
    assert finished.index("slow") >= fast - 2


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
    write-backs racing can leave the copy here rejected — which is how a stored
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
    monkeypatch.setattr(guard.sys, "argv", ["/opt/eval-guard/bin/openocd", "-f", "board.cfg"])

    assert guard.main() == 126

    events = (tmp_path / ".agentic-hil-eval" / "guard-events.jsonl").read_text(encoding="utf-8")
    recorded = json.loads(events.splitlines()[0])
    assert recorded["command"] == "openocd"
    assert "Agentic HIL" in recorded["reason"]


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
    assert "Unstable — repetitions disagreed:" in report
    assert "quickstart | opencode m (default): 1x failed, 1x passed" in report


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
        ({"hardware_state": "unknown"}, False),
    ],
)
def test_independent_success_predicate(patch: dict[str, object], expected: bool) -> None:
    result = {
        "ok": True,
        "target_ok": True,
        "audit_ok": True,
        "cleanup_ok": True,
        "cleanup_required": False,
        "quarantined": False,
        "lease_state": None,
        "side_effect_status": "none",
        "hardware_state": "not_applicable",
        **patch,
    }

    assert overall_success(result) is expected
