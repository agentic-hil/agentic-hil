from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from evals.install import runner
from evals.install.config import Case, CredentialFile, Job, Matrix, Target


def _source_tree(root: Path) -> Path:
    package = root / "src" / "agentic_hil"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text('__version__ = "0.4.0"\n', encoding="utf-8")
    contract = root / "evals" / "install" / "tools.list.expected"
    contract.parent.mkdir(parents=True)
    contract.write_text("probe_target\n", encoding="utf-8")
    return root


def _case() -> Case:
    return Case(
        id="cleanup",
        prompt_template="install",
        fixture="clean",
        expected_outcome="success",
    )


def _matrix(case: Case, job: Job) -> Matrix:
    return Matrix(
        target=Target(mode="local", expected_version="0.4.0"),
        cases=(case,),
        jobs=(job,),
        image="test-image",
    )


def _successful_verifier(command: list[str], _timeout: int = 120) -> subprocess.CompletedProcess[str]:
    if "evals.install.verifier" in command:
        verification = {"schema_version": 1, "ok": True, "checks": []}
        return subprocess.CompletedProcess(command, 0, json.dumps(verification), "")
    return subprocess.CompletedProcess(command, 0, "", "")


def _successful_agent(
    _command: list[str],
    log_path: Path,
    **_kwargs: object,
) -> tuple[int, bool, str | None]:
    log_path.write_text(
        json.dumps({"event": "eval_start", "agent_cli_version": "test"}) + "\n",
        encoding="utf-8",
    )
    return 0, False, None


@pytest.mark.parametrize("credential_mode", ["environment", "file"])
def test_run_never_retains_raw_volumes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    credential_mode: str,
) -> None:
    if credential_mode == "environment":
        monkeypatch.setenv("OPENAI_API_KEY", "environment-secret")
        credentials = ("OPENAI_API_KEY",)
        credential_files: tuple[CredentialFile, ...] = ()
    else:
        auth_file = tmp_path / "auth.json"
        auth_file.write_text('{"access_token":"file-secret-token"}\n', encoding="utf-8")
        monkeypatch.setenv("CODEX_AUTH_FILE", str(auth_file))
        credentials = ()
        credential_files = (CredentialFile(kind="codex-auth", path_environment="CODEX_AUTH_FILE"),)

    job = Job(
        agent="codex",
        model="test-model",
        repetition=1,
        timeout_seconds=60,
        credentials=credentials,
        credential_files=credential_files,
    )
    case = _case()
    source = _source_tree(tmp_path / "source")
    removed_volumes: list[str] = []

    monkeypatch.setattr(runner, "create_volume", lambda _docker, _name: None)
    monkeypatch.setattr(runner, "run_logged", _successful_agent)
    monkeypatch.setattr(runner, "run_capture", _successful_verifier)
    monkeypatch.setattr(runner, "remove_container", lambda _docker, _name: None)
    monkeypatch.setattr(runner, "remove_volume", lambda _docker, name: removed_volumes.append(name))

    result = runner.run_one(
        docker="docker",
        matrix=_matrix(case, job),
        case=case,
        job=job,
        source_root=source,
        host_source_root=source,
        output_root=tmp_path / "output",
        image_id="sha256:test",
    )

    assert result["status"] == "passed"
    assert result["cleanup"]["ok"] is True
    assert result["environment"]["home_volume"] is None
    assert result["environment"]["workspace_volume"] is None
    assert len(removed_volumes) == 2
    assert any(name.endswith("-home") for name in removed_volumes)
    assert any(name.endswith("-workspace") for name in removed_volumes)


def test_cleanup_failure_marks_job_error_and_reports_resource_names(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "environment-secret")
    job = Job(
        agent="codex",
        model="test-model",
        repetition=1,
        timeout_seconds=60,
        credentials=("OPENAI_API_KEY",),
        credential_files=(),
    )
    case = _case()
    source = _source_tree(tmp_path / "source")

    monkeypatch.setattr(runner, "create_volume", lambda _docker, _name: None)
    monkeypatch.setattr(runner, "run_logged", _successful_agent)
    monkeypatch.setattr(runner, "run_capture", _successful_verifier)

    def remove_container(_docker: str, name: str) -> None:
        if name.endswith("-agent"):
            raise RuntimeError("container daemon unavailable")

    def remove_volume(_docker: str, name: str) -> None:
        if name.endswith("-home"):
            raise RuntimeError("volume daemon unavailable")

    monkeypatch.setattr(runner, "remove_container", remove_container)
    monkeypatch.setattr(runner, "remove_volume", remove_volume)

    result = runner.run_one(
        docker="docker",
        matrix=_matrix(case, job),
        case=case,
        job=job,
        source_root=source,
        host_source_root=source,
        output_root=tmp_path / "output",
        image_id="sha256:test",
    )

    remaining_containers = result["cleanup"]["remaining_containers"]
    remaining_volumes = result["cleanup"]["remaining_volumes"]
    assert result["status"] == "error"
    assert result["cleanup"]["ok"] is False
    assert any(name.endswith("-agent") for name in remaining_containers)
    assert any(name.endswith("-home") for name in remaining_volumes)
    assert result["environment"]["home_volume"] in remaining_volumes
    assert "container daemon unavailable" in result["failure"]
    assert "volume daemon unavailable" in result["failure"]


def test_timeout_removal_failure_stops_before_scrubber_and_verifier(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    auth_file = tmp_path / "auth.json"
    auth_file.write_text('{"access_token":"file-secret-token"}\n', encoding="utf-8")
    monkeypatch.setenv("CODEX_AUTH_FILE", str(auth_file))
    job = Job(
        agent="codex",
        model="test-model",
        repetition=1,
        timeout_seconds=60,
        credentials=(),
        credential_files=(CredentialFile(kind="codex-auth", path_environment="CODEX_AUTH_FILE"),),
    )
    case = _case()
    source = _source_tree(tmp_path / "source")
    capture_calls: list[list[str]] = []

    def timed_out_agent(
        _command: list[str],
        log_path: Path,
        **kwargs: object,
    ) -> tuple[int, bool, str | None]:
        log_path.write_text("{}\n", encoding="utf-8")
        container = str(kwargs["timeout_container"])
        return 137, True, f"container {container}: timeout removal failed"

    def capture(command: list[str], _timeout: int = 120) -> subprocess.CompletedProcess[str]:
        capture_calls.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    def remove_container(_docker: str, name: str) -> None:
        if name.endswith("-agent"):
            raise RuntimeError("agent still running")

    monkeypatch.setattr(runner, "create_volume", lambda _docker, _name: None)
    monkeypatch.setattr(runner, "run_logged", timed_out_agent)
    monkeypatch.setattr(runner, "run_capture", capture)
    monkeypatch.setattr(runner, "remove_container", remove_container)
    monkeypatch.setattr(runner, "remove_volume", lambda _docker, _name: None)

    result = runner.run_one(
        docker="docker",
        matrix=_matrix(case, job),
        case=case,
        job=job,
        source_root=source,
        host_source_root=source,
        output_root=tmp_path / "output",
        image_id="sha256:test",
    )

    assert result["status"] == "error"
    assert result["environment"]["credential_file_links_scrubbed"] is False
    assert any(name.endswith("-agent") for name in result["cleanup"]["remaining_containers"])
    assert capture_calls == []


def test_remove_container_checks_docker_remove_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    responses = iter(
        [
            subprocess.CompletedProcess(["inspect"], 0, "{}", ""),
            subprocess.CompletedProcess(["rm"], 1, "", "daemon refused removal"),
        ]
    )
    monkeypatch.setattr(runner, "run_capture", lambda _command, _timeout=120: next(responses))

    with pytest.raises(RuntimeError, match=r"container agent-container.*daemon refused removal"):
        runner.remove_container("docker", "agent-container")


def test_remove_volume_accepts_already_absent_resource(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def capture(command: list[str], _timeout: int = 120) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(command, 1, "", "Error: no such volume: gone")

    monkeypatch.setattr(runner, "run_capture", capture)

    runner.remove_volume("docker", "gone")

    assert calls == [["docker", "volume", "inspect", "gone"]]
