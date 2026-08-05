from __future__ import annotations

import json
import os
import subprocess
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
        loop_in_container.check_logins(loop_in_container.mounted_files())


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
    loop_in_container.check_logins(resolved)

    assert [kind for kind, _path in resolved] == list(loop_in_container.LOGIN_FILES)


def test_a_refresh_that_cannot_be_started_is_a_reason_rather_than_a_traceback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(loop_in_container.shutil, "which", lambda _command: "/usr/bin/claude")

    def refuses_to_launch(*_args: object, **_kwargs: object) -> None:
        raise OSError(13, "Permission denied")

    monkeypatch.setattr(loop_in_container.subprocess, "run", refuses_to_launch)

    with pytest.raises(loop_in_container.Refused, match="could not start"):
        loop_in_container.refresh_stale_login("claude-auth", "expires in 3 days")


def test_a_refresh_that_hangs_says_the_login_was_not_refreshed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(loop_in_container.shutil, "which", lambda _command: "/usr/bin/claude")

    def hangs(*_args: object, **_kwargs: object) -> None:
        raise loop_in_container.subprocess.TimeoutExpired(cmd="claude", timeout=300)

    monkeypatch.setattr(loop_in_container.subprocess, "run", hangs)

    with pytest.raises(loop_in_container.Refused, match="did not finish within 300s"):
        loop_in_container.refresh_stale_login("claude-auth", "expires in 3 days")


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
    assert command[command.index("--task") + 1] == "x"


def test_the_isolation_options_are_the_last_word_in_the_inner_command() -> None:
    """argparse takes the final occurrence, so nothing forwarded can precede them."""
    command = entrypoint.loop_command(["--task", "x", "--max-rounds", "2"])
    enforced = command.index("--repo")

    assert command.index("--task") < enforced
    assert command.index("--max-rounds") < enforced
    for option in ("--repo", "--codex-sandbox", "--review-checkout-dir"):
        assert command.index(option) >= enforced


@pytest.mark.parametrize(
    "forwarded",
    [
        ["--task", "x", "--codex-sandbox", "workspace-write"],
        ["--task", "x", "--review-checkout", "none"],
        ["--review-checkout-dir=/repo/review", "--task", "x"],
        # The inner parser expands an unambiguous abbreviation to the same
        # destination, so an abbreviation is the option.
        ["--task", "x", "--codex-sand", "workspace-write"],
        ["--task", "x", "--review-checkou=none"],
    ],
)
def test_the_container_refuses_to_forward_what_decides_its_own_isolation(forwarded: list[str]) -> None:
    """Both halves refuse: the host before any spend, the entrypoint on arrival."""
    with pytest.raises(loop_in_container.Refused, match="what the container isolates"):
        loop_in_container.forwarded_paperwork(forwarded)
    with pytest.raises(SystemExit, match="cannot be forwarded"):
        entrypoint.loop_command(forwarded)


def test_both_halves_own_the_same_options() -> None:
    assert set(loop_in_container.CONTAINER_OWNED_OPTIONS) == set(entrypoint.CONTAINER_OWNED_OPTIONS)


def test_the_project_is_installed_from_a_snapshot_rather_than_from_the_mount() -> None:
    """An editable install of the mount resolves the reviewer's imports to it."""
    snapshot = entrypoint.snapshot_command()
    install = entrypoint.install_command()

    assert snapshot[-2:] == [str(entrypoint.REPO), str(entrypoint.PROJECT_SNAPSHOT)]
    assert entrypoint.PROJECT_SNAPSHOT.parent == entrypoint.SCRATCH
    assert install[-1] == f"{entrypoint.PROJECT_SNAPSHOT}[dev,can]"
    # Nothing is installed from the mount, and nothing is installed editable:
    # both are ways of pointing the shared virtualenv at one agent's tree.
    assert "--editable" not in install and "-e" not in install
    assert not any(str(entrypoint.REPO) in argument for argument in install)


def test_the_reviewer_imports_the_commit_it_is_reviewing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    checkout = tmp_path / "review"
    (checkout / "src").mkdir(parents=True)
    monkeypatch.setenv("PYTHONPATH", str(tmp_path / "elsewhere"))

    env = agent_review_loop.reviewer_env(checkout, dry_run=False)

    assert env is not None
    assert env["PYTHONPATH"].split(os.pathsep)[0] == str(checkout / "src")
    # A repository without a src directory is left exactly as it was.
    plain = tmp_path / "plain"
    plain.mkdir()
    assert agent_review_loop.reviewer_env(plain, dry_run=False)["PYTHONPATH"] == str(tmp_path / "elsewhere")


def test_the_reviewers_clone_falls_back_when_a_hardlink_cannot_cross_filesystems(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, ...]] = []

    def fake_git(_repo: Path, *arguments: str) -> str:
        calls.append(arguments)
        if len(calls) == 1:
            raise agent_review_loop.AgentError("git clone failed: fatal: failed to create link: Invalid cross-device link")
        return ""

    monkeypatch.setattr(agent_review_loop, "git", fake_git)
    destination = tmp_path / "review"

    assert agent_review_loop.create_review_checkout("clone", tmp_path / "repo", destination) == destination
    assert "--no-hardlinks" not in calls[0]
    assert "--no-hardlinks" in calls[1]


def test_a_checkout_directory_that_already_exists_is_never_cloned_over(tmp_path: Path) -> None:
    """The retry deletes what the attempt made, so it may only ever make it."""
    destination = tmp_path / "existing"
    destination.mkdir()
    (destination / "work.txt").write_text("the operator's checkout", encoding="utf-8")

    with pytest.raises(agent_review_loop.AgentError, match="already exists"):
        agent_review_loop.create_review_checkout("clone", tmp_path / "repo", destination)

    assert (destination / "work.txt").is_file()


def test_a_clone_that_failed_for_any_other_reason_deletes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, ...]] = []

    def fake_git(_repo: Path, *arguments: str) -> str:
        calls.append(arguments)
        # git creates the destination before it reports what went wrong.
        (tmp_path / "review").mkdir(exist_ok=True)
        (tmp_path / "review" / "partial").write_text("x", encoding="utf-8")
        raise agent_review_loop.AgentError("git clone failed: fatal: repository '/repo' does not exist")

    monkeypatch.setattr(agent_review_loop, "git", fake_git)

    with pytest.raises(agent_review_loop.AgentError, match="does not exist"):
        agent_review_loop.create_review_checkout("clone", tmp_path / "repo", tmp_path / "review")

    assert len(calls) == 1
    # Whatever the failed attempt left is evidence, not something to clear up by
    # cloning again over the top of it.
    assert (tmp_path / "review" / "partial").is_file()


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


def test_a_repository_holding_the_profile_is_not_mounted_read_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dotfiles work tree rooted at home would hand over the logins themselves."""
    home = tmp_path / "profile"
    _stored_logins(home)
    _at_home(monkeypatch, home)
    files = loop_in_container.mounted_files()

    with pytest.raises(loop_in_container.Refused, match="contains this machine's profile"):
        loop_in_container.check_repository(home, files)
    with pytest.raises(loop_in_container.Refused, match="contains this machine's profile"):
        loop_in_container.check_repository(tmp_path, files)
    # A repository beside the profile is exactly the ordinary case.
    ordinary = tmp_path / "project"
    ordinary.mkdir()
    loop_in_container.check_repository(ordinary, files)


def test_a_login_inside_the_repository_is_refused_by_name(tmp_path: Path) -> None:
    repository = tmp_path / "project"
    (repository / ".claude").mkdir(parents=True)
    login = repository / ".claude" / ".credentials.json"
    login.write_text("{}", encoding="utf-8")

    with pytest.raises(loop_in_container.Refused, match="claude-auth file at"):
        loop_in_container.check_repository(repository, [("claude-auth", login)])


def test_a_comma_in_a_path_is_refused_instead_of_corrupting_the_mount(tmp_path: Path) -> None:
    """Docker's --mount value is comma separated and has no escape for one."""
    repository = tmp_path / "Doe, Jane"
    repository.mkdir()

    with pytest.raises(loop_in_container.Refused, match="contains a comma"):
        loop_in_container.check_repository(repository, [])

    login = tmp_path / "Doe, Jane.json"
    login.write_text("{}", encoding="utf-8")
    with pytest.raises(loop_in_container.Refused, match="contains a comma"):
        loop_in_container.check_repository(tmp_path / "project", [("claude-auth", login)])


def test_paperwork_that_would_die_with_the_container_is_refused(tmp_path: Path) -> None:
    for value in ("/var/tmp/reviews", "../outside/reviews", "C:\\reviews"):
        with pytest.raises(loop_in_container.Refused):
            loop_in_container.forwarded_paperwork(["--review-dir", value])

    landing = loop_in_container.forwarded_paperwork(["--log-dir=notes/logs", "--review-dir", "notes/reviews"])
    assert landing["--log-dir"] == "/repo/notes/logs"
    assert landing["--review-dir"] == "/repo/notes/reviews"
    # An abbreviation reaches the same option inside the loop, so it is checked
    # as that option rather than waved through unread.
    with pytest.raises(loop_in_container.Refused, match="outside the mounted repository"):
        loop_in_container.forwarded_paperwork(["--review-di", "/var/tmp/reviews"])


def test_an_announced_directory_is_named_on_this_side_of_the_mount(tmp_path: Path) -> None:
    match = loop_in_container.ANNOUNCED_DIRECTORY.match("reviews    : /repo/my notes/reviews/20260805-120000\n")

    assert match is not None
    assert match.group(2) == "/repo/my notes/reviews/20260805-120000"
    assert loop_in_container.on_this_side(match.group(2), tmp_path) == str(
        tmp_path / "my notes" / "reviews" / "20260805-120000"
    )
    # Nothing outside the mount has a path here, and inventing one sends an
    # operator looking for a directory that never existed.
    assert "inside the container only" in loop_in_container.on_this_side("/tmp/reviews", tmp_path)


def _ready_to_run(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Everything main() needs except the container itself."""
    home = tmp_path / "profile"
    _stored_logins(home)
    _at_home(monkeypatch, home)
    repository = tmp_path / "project"
    repository.mkdir()
    monkeypatch.setattr(loop_in_container.shutil, "which", lambda command: f"/usr/bin/{command}")
    monkeypatch.setattr(loop_in_container, "repository_root", lambda _start: repository)
    monkeypatch.setattr(loop_in_container, "git_identity", lambda _repo: ("A Developer", "dev@example.invalid"))
    monkeypatch.setattr(loop_in_container, "image_present", lambda _docker, _image: True)
    monkeypatch.setattr(
        loop_in_container,
        "run_capture",
        lambda _command, _timeout=120: subprocess.CompletedProcess([], 0, "", ""),
    )
    monkeypatch.setattr(loop_in_container, "remove_container", lambda _docker, _name: None)
    return repository


def test_a_home_volume_that_survives_the_run_fails_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The volume is where the logins were copied to; leaving it is not success."""
    repository = _ready_to_run(monkeypatch, tmp_path)
    monkeypatch.setattr(loop_in_container, "stream", lambda _command, _repository: (0, {}, None))

    def stuck(_docker: str, name: str) -> None:
        raise RuntimeError(f"Docker volume {name} still exists after removal")

    monkeypatch.setattr(loop_in_container, "remove_volume", stuck)

    assert loop_in_container.main(["--image", "loop:test", "--task", "x"]) == loop_in_container.EXIT_CLEANUP_FAILED
    captured = capsys.readouterr()
    assert "still exists after removal" in captured.err
    assert "-home" in captured.err
    assert f"commits    : in {repository}" in captured.out


def test_an_interrupted_run_that_cannot_clean_up_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _ready_to_run(monkeypatch, tmp_path)

    def interrupted(_command: list[str], _repository: Path) -> tuple[int, dict[str, str], str | None]:
        raise KeyboardInterrupt

    monkeypatch.setattr(loop_in_container, "stream", interrupted)
    monkeypatch.setattr(
        loop_in_container,
        "remove_volume",
        lambda _docker, name: (_ for _ in ()).throw(RuntimeError(f"could not remove {name}")),
    )

    assert loop_in_container.main(["--image", "loop:test", "--task", "x"]) == loop_in_container.EXIT_CLEANUP_FAILED
    assert "could not remove" in capsys.readouterr().err


def test_a_clean_run_reports_where_a_custom_review_directory_landed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repository = _ready_to_run(monkeypatch, tmp_path)
    monkeypatch.setattr(loop_in_container, "remove_volume", lambda _docker, _name: None)
    monkeypatch.setattr(loop_in_container, "stream", lambda _command, _repository: (0, {}, None))

    assert loop_in_container.main(["--image", "loop:test", "--task", "x", "--review-dir", "notes/reviews"]) == 0

    captured = capsys.readouterr()
    assert f"reviews    : {repository / 'notes' / 'reviews'}" in captured.out
    assert f"logs       : {repository / '.agentic-loop' / 'logs'}" in captured.out
