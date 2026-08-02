from __future__ import annotations

import argparse
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from evals.install import runner


def _build_arguments(docker: Path) -> argparse.Namespace:
    return argparse.Namespace(
        docker=str(docker),
        image="agentic-hil-install-eval:test",
        codex_version=None,
        claude_version=None,
        opencode_version=None,
    )


def test_build_adds_docker_cli_directory_to_subprocess_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    docker = tmp_path / "Docker" / "resources" / "bin" / "docker.exe"
    docker.parent.mkdir(parents=True)
    docker.write_bytes(b"placeholder")
    original_path = str(tmp_path / "unrelated-bin")
    secret = "must-not-appear-in-command-or-output"
    observed: dict[str, Any] = {}

    monkeypatch.setenv("PATH", original_path)
    monkeypatch.setenv("OPENAI_API_KEY", secret)
    monkeypatch.setattr(runner.shutil, "which", lambda _command: str(docker))

    def capture(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        observed["command"] = command
        observed["environment"] = kwargs["env"]
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(runner.subprocess, "run", capture)

    assert runner.build_image(_build_arguments(docker)) == 0

    command = observed["command"]
    environment = observed["environment"]
    assert isinstance(command, list)
    assert isinstance(environment, dict)
    assert command[0] == str(docker)
    assert environment["PATH"].split(os.pathsep) == [str(docker.parent), original_path]
    assert os.environ["PATH"] == original_path
    assert secret not in repr(command)
    assert secret not in capsys.readouterr().out


def test_build_repins_requested_agent_cli_versions_in_the_lock_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A version override has to move the lock, because "npm ci" reads nothing else."""
    docker = tmp_path / "docker.exe"
    docker.write_bytes(b"placeholder")
    arguments = _build_arguments(docker)
    arguments.codex_version = "0.146.0"
    arguments.opencode_version = "1.19.0"
    commands: list[list[str]] = []

    monkeypatch.setattr(
        runner.shutil,
        "which",
        lambda command: "npm" if command == "npm" else str(docker),
    )

    def capture(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(runner.subprocess, "run", capture)

    assert runner.build_image(arguments) == 0

    npm_command, docker_command = commands
    assert npm_command[:2] == ["npm", "install"]
    assert "--package-lock-only" in npm_command
    # claude_version stayed None, so its pin must be left alone.
    assert npm_command[-2:] == ["@openai/codex@0.146.0", "opencode-ai@1.19.0"]
    assert "--build-arg" not in docker_command


def test_build_without_overrides_never_touches_the_lock_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    docker = tmp_path / "docker.exe"
    docker.write_bytes(b"placeholder")
    commands: list[list[str]] = []

    monkeypatch.setattr(runner.shutil, "which", lambda _command: str(docker))

    def capture(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(runner.subprocess, "run", capture)

    assert runner.build_image(_build_arguments(docker)) == 0

    assert len(commands) == 1
    assert commands[0][0] == str(docker)


def test_build_reports_the_missing_tool_when_an_override_needs_npm(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    docker = tmp_path / "docker.exe"
    docker.write_bytes(b"placeholder")
    arguments = _build_arguments(docker)
    arguments.claude_version = "2.1.219"

    monkeypatch.setattr(
        runner.shutil,
        "which",
        lambda command: None if command == "npm" else str(docker),
    )

    with pytest.raises(RuntimeError, match="npm is required"):
        runner.build_image(arguments)


def test_docker_subprocess_path_does_not_duplicate_cli_directory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    docker = tmp_path / "docker-bin" / "docker.exe"
    docker.parent.mkdir()
    docker.write_bytes(b"placeholder")
    other = tmp_path / "other-bin"
    monkeypatch.setenv("PATH", os.pathsep.join((str(docker.parent), str(other))))

    environment = runner.docker_subprocess_environment(str(docker))

    assert environment["PATH"].split(os.pathsep) == [str(docker.parent), str(other)]
