from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from evals.install.runner import docker_security_options

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "loopimage"))
import agent_review_loop  # noqa: E402
import entrypoint  # noqa: E402
import loop_in_container  # noqa: E402


def _epoch_milliseconds(offset: timedelta) -> int:
    return int((datetime.now(timezone.utc) + offset).timestamp() * 1000)


def _stored_logins(home: Path, *, refresh_expires_in: timedelta = timedelta(days=30)) -> None:
    """Write the three files the wrapper mounts, with a claude login it can judge."""
    (home / ".claude").mkdir(parents=True, exist_ok=True)
    (home / ".codex").mkdir(parents=True, exist_ok=True)
    (home / ".claude" / ".credentials.json").write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "a",
                    "refreshToken": "r",
                    "expiresAt": _epoch_milliseconds(timedelta(days=1)),
                    "refreshTokenExpiresAt": _epoch_milliseconds(refresh_expires_in),
                }
            }
        ),
        encoding="utf-8",
    )
    (home / ".claude.json").write_text("{}", encoding="utf-8")
    (home / ".codex" / "auth.json").write_text(json.dumps({"tokens": {"access_token": "a"}}), encoding="utf-8")


def _at_home(monkeypatch: pytest.MonkeyPatch, home: Path) -> None:
    # Path.home() reads USERPROFILE on Windows and HOME elsewhere; setting both
    # keeps this test honest on either.
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HOME", str(home))


def _command(tmp_path: Path, *, forwarded: list[str] | None = None) -> list[str]:
    home = tmp_path / "profile"
    _stored_logins(home)
    repository = tmp_path / "repository"
    repository.mkdir()
    return loop_in_container.container_command(
        docker="docker",
        image="agentic-hil-loop:test",
        container_name="agentic-hil-loop-test",
        home_volume="agentic-hil-loop-test-home",
        repository=repository,
        files=[
            ("claude-auth", home / ".claude" / ".credentials.json"),
            ("codex-auth", home / ".codex" / "auth.json"),
        ],
        identity=("A Developer", "developer@example.invalid"),
        forwarded=forwarded or [],
    )


def _mounts(command: list[str]) -> list[str]:
    return [value for flag, value in zip(command, command[1:], strict=False) if flag == "--mount"]


def test_the_container_keeps_every_isolation_option_the_evaluation_sets(tmp_path: Path) -> None:
    """Measured peaks fit inside the evaluation's limits, so none of them is relaxed."""
    command = _command(tmp_path)
    baseline = docker_security_options()

    start = command.index(baseline[0])
    assert command[start : start + len(baseline)] == baseline
    for expected in ("--read-only", "no-new-privileges", "--memory"):
        assert expected in command
    for never in ("--privileged", "--cap-add", "--user"):
        assert never not in command


def test_each_login_is_its_own_read_only_mount_and_no_profile_directory_is(tmp_path: Path) -> None:
    command = _command(tmp_path)
    profile = (tmp_path / "profile").resolve().as_posix()

    logins = [mount for mount in _mounts(command) if loop_in_container.MOUNTED_FILES in mount]
    assert len(logins) == 2
    for mount in logins:
        assert mount.endswith(",readonly")
        assert f"dst={loop_in_container.MOUNTED_FILES}/" in mount
    # The directories those files live in hold every project the operator has
    # ever opened, and none of that is the container's business.
    assert not any(f"src={profile}," in mount for mount in _mounts(command))


def test_the_repository_is_the_only_writable_bind_mount(tmp_path: Path) -> None:
    command = _command(tmp_path)
    binds = [mount for mount in _mounts(command) if mount.startswith("type=bind")]
    writable = [mount for mount in binds if not mount.endswith(",readonly")]

    assert len(writable) == 1
    assert f"dst={loop_in_container.MOUNTED_REPOSITORY}" in writable[0]
    # A home built from copied logins never lands on this machine's filesystem.
    home = [mount for mount in _mounts(command) if f"dst={loop_in_container.CONTAINER_HOME}" in mount]
    assert home and home[0].startswith("type=volume")


def test_nothing_a_caller_forwards_reaches_the_docker_command_line(tmp_path: Path) -> None:
    """Options the wrapper does not consume are the inner loop's, not Docker's."""
    forwarded = ["--privileged", "--task", "make it so"]
    command = _command(tmp_path, forwarded=forwarded)

    image = command.index("agentic-hil-loop:test")
    for argument in forwarded:
        assert command.index(argument) > image
    assert command[-len(forwarded) :] == forwarded


def test_an_unusable_login_is_refused_before_any_model_is_spent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "profile"
    _stored_logins(home, refresh_expires_in=timedelta(days=-1))
    _at_home(monkeypatch, home)

    with pytest.raises(loop_in_container.Refused, match="Sign in again"):
        loop_in_container.mounted_files()


def test_a_missing_login_names_the_file_it_looked_for(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "profile"
    _stored_logins(home)
    (home / ".codex" / "auth.json").unlink()
    _at_home(monkeypatch, home)

    with pytest.raises(loop_in_container.Refused, match="codex-auth: no stored login"):
        loop_in_container.mounted_files()


def test_a_healthy_pair_of_logins_starts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "profile"
    _stored_logins(home)
    _at_home(monkeypatch, home)

    resolved = loop_in_container.mounted_files()

    assert [kind for kind, _path in resolved] == list(loop_in_container.LOGIN_FILES)


def test_the_image_tag_follows_the_recipe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An edited Dockerfile or entrypoint must not run out of yesterday's image."""
    recipe = tmp_path / "loopimage"
    recipe.mkdir()
    for name in loop_in_container.RECIPE:
        (recipe / name).write_bytes((loop_in_container.CONTAINER_DIRECTORY / name).read_bytes())
    monkeypatch.setattr(loop_in_container, "CONTAINER_DIRECTORY", recipe)

    before = loop_in_container.recipe_digest()
    (recipe / "entrypoint.py").write_text("# changed\n", encoding="utf-8")

    assert loop_in_container.recipe_digest() != before


def test_a_machine_without_docker_is_told_rather_than_traced(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(loop_in_container.shutil, "which", lambda _command: None)

    assert loop_in_container.main(["--task", "anything"]) == loop_in_container.EXIT_NO_DOCKER


def test_the_inner_loop_runs_codex_without_a_second_sandbox() -> None:
    """The container is the boundary; see the comment beside the flag."""
    command = entrypoint.loop_command(["--task", "x"])

    assert command[command.index("--codex-sandbox") + 1] == "danger-full-access"
    checkout = Path(command[command.index("--review-checkout-dir") + 1])
    assert checkout.parent == entrypoint.SCRATCH
    assert command[-2:] == ["--task", "x"]


def test_the_reviewers_clone_falls_back_when_a_hardlink_cannot_cross_filesystems(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, ...]] = []

    def fake_git(_repo: Path, *arguments: str) -> str:
        calls.append(arguments)
        if len(calls) == 1:
            raise agent_review_loop.AgentError("Invalid cross-device link")
        return ""

    monkeypatch.setattr(agent_review_loop, "git", fake_git)
    destination = tmp_path / "review"

    assert agent_review_loop.create_review_checkout("clone", tmp_path / "repo", destination) == destination
    assert "--no-hardlinks" not in calls[0]
    assert "--no-hardlinks" in calls[1]


def test_the_operators_standing_instructions_travel_with_the_logins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fresh home knows none of the operator's rules unless it is given them."""
    home = tmp_path / "profile"
    _stored_logins(home)
    (home / ".claude" / "CLAUDE.md").write_text("never sign a commit on my behalf\n", encoding="utf-8")
    _at_home(monkeypatch, home)

    resolved = loop_in_container.mounted_files()

    assert ("claude-instructions", (home / ".claude" / "CLAUDE.md").resolve()) in resolved
    # There is no codex file here, and an operator who keeps none is not refused.
    assert "codex-instructions" not in [kind for kind, _path in resolved]


def test_every_file_the_wrapper_mounts_has_a_place_in_the_container_home() -> None:
    sent = set(loop_in_container.LOGIN_FILES) | set(loop_in_container.INSTRUCTION_FILES)

    assert sent == set(entrypoint.MOUNTED_PATHS)
