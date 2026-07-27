from __future__ import annotations

import argparse
import json
from dataclasses import replace
from pathlib import Path

import pytest

from evals.install import runner as install_runner
from evals.install import scrub_credentials
from evals.install.adapters import adapter_for, build_agent_command
from evals.install.config import CredentialFile, Job, load_matrix
from evals.install.fixtures import SENTINEL_KEY, SENTINEL_VALUE, fixture_content, sentinel_value
from evals.install.report import format_report, load_results, report_results
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
    }
    assert len(matrix.jobs) * len(matrix.cases) == 9


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

    assert len(plan["runs"]) == 9
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
