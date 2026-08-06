from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from evals.install.runner import docker_security_options

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "loopimage"))
import agent_review_loop  # noqa: E402
import agent_shim  # noqa: E402
import entrypoint  # noqa: E402
import loop_in_container  # noqa: E402
import loop_options  # noqa: E402


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

    assert loop_in_container.main(["--task", "anything"]) == loop_in_container.EXIT_WRAPPER_FAILED


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


def test_one_rule_answers_for_both_halves() -> None:
    """Two enforcement points were the point; two copies of the rule were the hole."""
    assert loop_in_container.owned_options is loop_options.owned_options is entrypoint.owned_options
    assert (
        loop_in_container.isolating_codex_arguments
        is loop_options.isolating_codex_arguments
        is entrypoint.isolating_codex_arguments
    )


@pytest.mark.parametrize(
    "forwarded",
    [
        # The outer option name is the same in every one of these; the payload is
        # what moves the reviewer, so the payload is what has to be read.
        ["--task", "x", "--codex-arg=--cd=/repo"],
        ["--task", "x", "--codex-arg", "--cd", "--codex-arg", "/repo"],
        ["--task", "x", "--codex-arg=-C/repo"],
        ["--task", "x", "--codex-arg=-C"],
        ["--task", "x", "--codex-arg=--sandbox=read-only"],
        ["--task", "x", "--codex-arg=-sread-only"],
        ["--task", "x", "--codex-arg=--dangerously-bypass-approvals-and-sandbox"],
        ["--task", "x", "--codex-arg=-c", "--codex-arg=sandbox_mode=read-only"],
        ["--task", "x", "--codex-arg=-csandbox_mode=read-only"],
        ["--task", "x", "--codex-arg=--config=sandbox_workspace_write.network_access=true"],
        # --codex-arg is itself reachable by any unambiguous abbreviation.
        ["--task", "x", "--codex-a=--cd=/repo"],
    ],
)
def test_a_nested_codex_argument_cannot_move_the_reviewer_out_of_its_checkout(forwarded: list[str]) -> None:
    """The loop appends these after its own, and Codex takes the last occurrence."""
    with pytest.raises(loop_in_container.Refused, match="move the reviewer"):
        loop_in_container.forwarded_paperwork(forwarded)
    with pytest.raises(SystemExit, match="move the reviewer"):
        entrypoint.loop_command(forwarded)


@pytest.mark.parametrize(
    "forwarded",
    [
        ["--task", "x", "--codex-arg=--json"],
        ["--task", "x", "--codex-arg=-c", "--codex-arg=model_verbosity=high"],
        ["--task", "x", "--codex-arg=-cmodel=gpt-5.1-codex"],
        ["--task", "x", "--codex-arg", "--skip-git-repo-check"],
    ],
)
def test_a_codex_argument_that_decides_nothing_about_isolation_is_forwarded(forwarded: list[str]) -> None:
    """--codex-arg stays the way to reach a Codex flag this loop does not model."""
    loop_in_container.forwarded_paperwork(forwarded)

    assert entrypoint.loop_command(forwarded)[-2:] == ["--review-checkout-dir", str(entrypoint.REVIEW_CHECKOUT)]


def test_the_reviewers_working_root_is_named_after_everything_forwarded() -> None:
    """Ordering is the half of this that does not depend on a list of flag names."""
    options = argparse.Namespace(
        codex_bin="codex",
        codex_model=None,
        codex_effort=None,
        codex_sandbox="danger-full-access",
        codex_arg=["--cd=/repo", "--sandbox=read-only"],
    )
    workspace = Path("/tmp/review")
    command = agent_review_loop.codex_command(
        options, Path("/tmp/schema.json"), Path("/tmp/last.txt"), Path("/repo/reviews"), workspace
    )

    assert command[-4:] == ["--cd", str(workspace), "--sandbox", "danger-full-access"]
    assert command.index("--cd=/repo") < command.index("--cd")


def test_each_agent_gets_the_environment_of_the_tree_it_works_in() -> None:
    """One shared virtualenv answered `importlib.metadata` out of the wrong commit."""
    implement = agent_shim.virtualenv(agent_shim.ROLES["claude"])
    review = agent_shim.virtualenv(agent_shim.ROLES["codex"])

    assert implement != review
    # The home volume, which the wrapper deletes, and not the tmpfs, which is
    # charged against the memory limit the container's own peaks are measured
    # against.
    assert implement.parent == agent_shim.HOME and review.parent == agent_shim.HOME
    assert agent_shim.export_directory("review").parent.parent == agent_shim.SCRATCH


def test_an_agents_environment_is_rebuilt_when_its_commit_changes_the_recipe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "python").write_text("", encoding="utf-8")
    recipes = [b"[project]\nname = 'a'\n", b"[project]\nname = 'a'\n", b"[project]\nname = 'b'\n"]
    installs: list[Path] = []

    monkeypatch.setattr(agent_shim, "virtualenv", lambda _role: venv)
    monkeypatch.setattr(agent_shim, "recipe", lambda _source: recipes.pop(0))
    monkeypatch.setattr(agent_shim, "export_directory", lambda role: tmp_path / "export" / role)
    monkeypatch.setattr(agent_shim, "export_head", lambda _source, _destination: None)
    monkeypatch.setattr(
        agent_shim,
        "run",
        lambda command, _timeout: installs.append(Path(command[0])) or subprocess.CompletedProcess([], 0, b"", b""),
    )

    for _ in range(3):
        agent_shim.build_environment("review", tmp_path / "source")

    # Three rounds, two recipes: the middle one changed nothing and paid nothing.
    assert len(installs) == 2


def test_an_agent_runs_with_its_own_virtualenv_first_on_path(monkeypatch: pytest.MonkeyPatch) -> None:
    venv = Path("/venv")
    binaries = str(venv / "bin")
    monkeypatch.setenv("PATH", os.pathsep.join(["/opt/loop/bin", binaries, "/usr/bin"]))

    environment = agent_shim.child_environment(venv)

    # First, and once: an inherited copy further down would otherwise stay.
    assert environment["PATH"].split(os.pathsep) == [binaries, "/opt/loop/bin", "/usr/bin"]
    assert environment["VIRTUAL_ENV"] == str(venv)


def test_the_loop_driver_needs_no_environment_of_its_own() -> None:
    """It is standard library only, and neither agent's virtualenv is its business."""
    command = entrypoint.loop_command(["--task", "x"])

    assert command[0] == sys.executable
    assert command[1] == str(entrypoint.REPO / "tools" / "agent_review_loop.py")


def test_the_tool_caches_the_suite_writes_stay_off_the_repository_mount() -> None:
    """TMPDIR does not move these, and a round of the loop left .ruff_cache on the mount."""
    environment = entrypoint.loop_environment()

    for key in ("RUFF_CACHE_DIR", "MYPY_CACHE_DIR", "COVERAGE_FILE", "TMPDIR", "TMP", "TEMP"):
        assert Path(environment[key]).is_relative_to(entrypoint.SCRATCH)
    assert f"-o cache_dir={entrypoint.PYTEST_CACHE}" in environment["PYTEST_ADDOPTS"]
    assert not any(
        value.startswith(str(entrypoint.REPO))
        for key, value in environment.items()
        if key.endswith(("_CACHE_DIR", "_FILE")) or key in {"TMPDIR", "TMP", "TEMP"}
    )


def test_the_reviewer_imports_the_commit_it_is_reviewing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    checkout = tmp_path / "review"
    (checkout / "src").mkdir(parents=True)
    monkeypatch.setenv("PYTHONPATH", str(tmp_path / "elsewhere"))

    env = agent_review_loop.agent_env(tmp_path / "scratch", checkout)

    assert env["PYTHONPATH"].split(os.pathsep)[0] == str(checkout / "src")
    # A repository without a src directory is left exactly as it was.
    plain = tmp_path / "plain"
    plain.mkdir()
    assert agent_review_loop.agent_env(tmp_path / "scratch", plain)["PYTHONPATH"] == str(tmp_path / "elsewhere")


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
    for value in ("/var/tmp/reviews", "../outside/reviews", "C:\\reviews", "C:/reviews", "c:reviews"):
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


def test_a_paperwork_directory_that_is_a_symlink_out_of_the_mount_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spelling is all a host can check; a symlink is only visible in the container."""
    mount = tmp_path / "repo"
    (mount / ".agentic-loop").mkdir(parents=True)
    (mount / "notes").mkdir()
    monkeypatch.setattr(entrypoint, "REPO", mount)

    entrypoint.check_paperwork(["--review-dir", "notes"])

    escaped = tmp_path / "elsewhere"
    escaped.mkdir()
    try:
        (mount / "escape").symlink_to(escaped, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("this machine does not allow creating a symlink")

    with pytest.raises(SystemExit, match="outside the repository mounted at"):
        entrypoint.check_paperwork(["--review-dir", "escape/reviews"])
    # The default is checked too: an operator whose .agentic-loop is a link out
    # of the tree forwards nothing at all and still loses every artifact.
    (mount / ".agentic-loop").rmdir()
    (mount / ".agentic-loop").symlink_to(escaped, target_is_directory=True)
    with pytest.raises(SystemExit, match="--log-dir lands at"):
        entrypoint.check_paperwork(["--task", "x"])


def test_a_task_that_mentions_a_credential_is_not_a_credential_failure() -> None:
    """The catalogue's phrases are ordinary English; a task may contain any of them."""
    pending: dict[str, str] = {}
    transcript = [
        "loop: /usr/local/bin/python /repo/tools/agent_review_loop.py --task handle unauthorized errors\n",
        "[claude r1] $ /opt/loop/bin/claude -p --output-format text\n",
        "[claude r1] I will handle unauthorized errors in the middleware.\n",
        "[claude r1] exit 0 after 91s (log: /repo/.agentic-loop/logs/round-01-claude.log)\n",
        "[codex  r1] ORIGINAL TASK: handle unauthorized errors\n",
        "[codex  r1] exit 0 after 47s (log: /repo/.agentic-loop/logs/round-01-codex.log)\n",
    ]

    assert [loop_in_container.credential_evidence(line, pending) for line in transcript] == [None] * len(transcript)


def test_a_login_the_failing_agent_refused_is_still_reported() -> None:
    pending: dict[str, str] = {}
    transcript = [
        "[claude r1] $ /opt/loop/bin/claude -p --output-format text\n",
        "[claude r1] OAuth token expired\n",
        "[claude r1] exit 1 after 3s (log: /repo/.agentic-loop/logs/round-01-claude.log)\n",
    ]

    assert [loop_in_container.credential_evidence(line, pending) for line in transcript] == [
        None,
        None,
        "OAuth token expired",
    ]


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

    assert loop_in_container.main(["--image", "loop:test", "--task", "x"]) == loop_in_container.EXIT_WRAPPER_FAILED
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

    assert loop_in_container.main(["--image", "loop:test", "--task", "x"]) == loop_in_container.EXIT_WRAPPER_FAILED
    captured = capsys.readouterr()
    assert "could not remove" in captured.err
    # The loop never returned a code, so there is none to report.
    assert "review loop exit code" not in captured.err


def test_no_failure_of_the_wrapper_can_be_read_as_a_result_of_the_review() -> None:
    """The collision this replaces: EXIT_NO_DOCKER=2 sat on EXIT_STALLED, EXIT_REFUSED=3 on EXIT_FAILED."""
    loop_codes = {
        agent_review_loop.EXIT_CLEAN,
        agent_review_loop.EXIT_ROUNDS_EXHAUSTED,
        agent_review_loop.EXIT_STALLED,
        agent_review_loop.EXIT_FAILED,
        agent_review_loop.EXIT_NO_PROGRESS,
    }

    assert loop_in_container.EXIT_WRAPPER_FAILED not in loop_codes
    assert max(loop_codes) < loop_in_container.EXIT_WRAPPER_FAILED


@pytest.mark.parametrize(
    "inner",
    [
        agent_review_loop.EXIT_CLEAN,
        agent_review_loop.EXIT_ROUNDS_EXHAUSTED,
        agent_review_loop.EXIT_STALLED,
        agent_review_loop.EXIT_FAILED,
        agent_review_loop.EXIT_NO_PROGRESS,
    ],
)
def test_every_code_the_loop_returns_reaches_the_caller_unchanged(
    inner: int, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Transparency is the point of the wrapper; the container is not supposed to be visible in the code."""
    _ready_to_run(monkeypatch, tmp_path)
    monkeypatch.setattr(loop_in_container, "remove_volume", lambda _docker, _name: None)
    monkeypatch.setattr(loop_in_container, "stream", lambda _command, _repository: (inner, {}, None))

    assert loop_in_container.main(["--image", "loop:test", "--task", "x"]) == inner
    assert loop_in_container.INNER_EXIT.format(code=inner) in capsys.readouterr().err


def test_a_review_that_plateaued_and_a_container_that_leaked_are_told_apart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Both used to arrive as a bare 2 or 4, and a caller had nothing else to read."""
    _ready_to_run(monkeypatch, tmp_path)
    monkeypatch.setattr(
        loop_in_container, "stream", lambda _command, _repository: (agent_review_loop.EXIT_STALLED, {}, None)
    )
    monkeypatch.setattr(loop_in_container, "remove_volume", lambda _docker, _name: None)

    plateaued = loop_in_container.main(["--image", "loop:test", "--task", "x"])
    capsys.readouterr()

    def stuck(_docker: str, name: str) -> None:
        raise RuntimeError(f"Docker volume {name} still exists after removal")

    monkeypatch.setattr(loop_in_container, "remove_volume", stuck)
    leaked = loop_in_container.main(["--image", "loop:test", "--task", "x"])
    captured = capsys.readouterr()

    assert plateaued == agent_review_loop.EXIT_STALLED
    assert leaked == loop_in_container.EXIT_WRAPPER_FAILED
    assert plateaued != leaked
    # The volume has to take the exit status -- it is where a copy of a login can
    # be -- but the review still plateaued, and that is still readable.
    assert loop_in_container.INNER_EXIT.format(code=agent_review_loop.EXIT_STALLED) in captured.err
    assert "still exists after removal" in captured.err


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


# --- a round that cannot finish, and the four other things one run cost -------


def _repository(tmp_path: Path) -> Path:
    """A repository with one commit and an identity, ready to be committed into."""
    repository = tmp_path / "work"
    repository.mkdir()
    subprocess.run(["git", "init", "-q", str(repository)], check=True)
    for name, value in (
        ("user.name", "A Developer"),
        ("user.email", "dev@example.invalid"),
        ("commit.gpgsign", "false"),
    ):
        subprocess.run(["git", "-C", str(repository), "config", name, value], check=True)
    (repository / "README.md").write_text("start\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repository), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(repository), "commit", "-q", "-m", "initial"], check=True)
    return repository


def _slow_agent(tmp_path: Path) -> Path:
    """An agent that produces work and then never gets round to committing it."""
    script = tmp_path / "slow_agent.py"
    script.write_text(
        "import pathlib, sys, time\n"
        "pathlib.Path('half-done.py').write_text('# a round of work\\n', encoding='utf-8')\n"
        "sys.stdout.write('editing\\n')\n"
        "sys.stdout.flush()\n"
        "time.sleep(120)\n",
        encoding="utf-8",
    )
    return script


def _in(repository: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repository), *arguments], capture_output=True, text=True, check=True
    ).stdout


def _pid_alive(pid: int) -> bool:
    """Whether a process still exists, without signalling it."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _cut_short(repository: Path, script: Path, monkeypatch: pytest.MonkeyPatch) -> int:
    monkeypatch.setattr(agent_review_loop, "resolve_executable", lambda name, _label: name)
    monkeypatch.setattr(agent_review_loop, "claude_command", lambda _options: [sys.executable, str(script)])
    return agent_review_loop.main(
        [
            "--repo",
            str(repository),
            "--task",
            "x",
            "--max-rounds",
            "1",
            "--timeout",
            "2",
            "--heartbeat",
            "0",
            "--review-checkout",
            "none",
        ]
    )


def test_a_round_the_timeout_cuts_short_is_committed_rather_than_discarded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rounds have run 79% of the way to the limit; the one that reached it cost the work."""
    repository = _repository(tmp_path)

    exit_code = _cut_short(repository, _slow_agent(tmp_path), monkeypatch)

    assert exit_code == agent_review_loop.EXIT_FAILED
    # The work is in a commit, the commit says it is unfinished, and it names the
    # timeout as the reason rather than leaving a mystery in the history.
    assert _in(repository, "log", "-1", "--format=%s").startswith("wip(review-loop): round 1")
    body = _in(repository, "log", "-1", "--format=%b")
    assert "ran out of time" in body
    assert "exceeded --timeout" in body
    assert "half-done.py" in _in(repository, "show", "--name-only", "--format=", "HEAD").split()
    # Nothing of the round is left in the tree, and this run's own paperwork is
    # not left in it either: the loop's directory carries a .gitignore of its
    # own, so it is invisible here as it is in a repository that ignores it.
    left = [line for line in _in(repository, "status", "--porcelain").splitlines() if line.strip()]
    assert left == []


def test_the_salvage_commit_leaves_the_runs_own_paperwork_out_of_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A repository that does not gitignore .agentic-loop would commit a review of itself."""
    repository = _repository(tmp_path)

    _cut_short(repository, _slow_agent(tmp_path), monkeypatch)

    assert ".agentic-loop" not in _in(repository, "show", "--name-only", "--format=", "HEAD")
    # The transcripts are still there to read; they are just not part of the commit.
    assert list((repository / ".agentic-loop" / "logs").rglob("round-01-claude.log"))


def test_the_loops_own_directory_ignores_itself_where_the_repository_ignores_nothing(tmp_path: Path) -> None:
    """`*` covers the directory holding it, that .gitignore included, so nothing is left to report."""
    repository = _repository(tmp_path)
    root = repository / ".agentic-loop" / "reviews"

    directory = agent_review_loop.paperwork_dir(repository, root, "20260101-000000")
    (directory / "round-01.md").write_text("four findings\n", encoding="utf-8")

    assert (root / ".gitignore").read_text(encoding="utf-8").splitlines()[-1] == "*"
    # Neither of the two ways the paperwork used to escape: a status the next
    # run's preflight refuses, and an agent's `git add -A`.
    assert _in(repository, "status", "--porcelain").strip() == ""
    subprocess.run(["git", "-C", str(repository), "add", "-A"], check=True)
    assert _in(repository, "diff", "--cached", "--name-only").strip() == ""


def test_a_repository_that_already_ignores_the_path_is_not_disturbed(tmp_path: Path) -> None:
    """Git does not descend into an ignored directory, so its own rule keeps answering."""
    repository = _repository(tmp_path)
    (repository / ".gitignore").write_text(".agentic-loop/\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repository), "add", ".gitignore"], check=True)
    subprocess.run(["git", "-C", str(repository), "commit", "-q", "-m", "ignore the loop"], check=True)
    before = _in(repository, "ls-files")

    agent_review_loop.paperwork_dir(repository, repository / ".agentic-loop" / "reviews", "20260101-000000")

    assert _in(repository, "status", "--porcelain").strip() == ""
    assert _in(repository, "ls-files") == before
    rule = _in(repository, "check-ignore", "-v", ".agentic-loop/reviews/20260101-000000")
    assert rule.startswith(".gitignore:1:.agentic-loop/")


def test_a_paperwork_directory_an_earlier_run_left_behind_is_ignored_from_now_on(tmp_path: Path) -> None:
    """Every repository the loop has already visited has one of these sitting in it, un-ignored."""
    repository = _repository(tmp_path)
    root = repository / ".agentic-loop" / "reviews"
    (root / "20251231-235959").mkdir(parents=True)
    (root / "20251231-235959" / "round-01.md").write_text("a run from before this existed\n", encoding="utf-8")

    agent_review_loop.paperwork_dir(repository, root, "20260101-000000")

    assert (root / ".gitignore").is_file()
    assert _in(repository, "status", "--porcelain").strip() == ""


def test_a_paperwork_directory_the_operator_tracks_is_left_alone(tmp_path: Path) -> None:
    """`*` in a directory somebody committed would hide their files rather than the loop's."""
    repository = _repository(tmp_path)
    notes = repository / "notes"
    notes.mkdir()
    (notes / "index.md").write_text("mine\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repository), "add", "notes/index.md"], check=True)
    subprocess.run(["git", "-C", str(repository), "commit", "-q", "-m", "notes"], check=True)

    directory = agent_review_loop.paperwork_dir(repository, notes, "20260101-000000")

    assert directory.is_dir()
    assert not (notes / ".gitignore").exists()


def _sweeping_agent(tmp_path: Path) -> Path:
    """An agent that commits the way agents do: everything the tree happens to hold."""
    script = tmp_path / "sweeping_agent.py"
    script.write_text(
        "import pathlib, subprocess, uuid\n"
        "name = 'change-%s.py' % uuid.uuid4().hex[:8]\n"
        "pathlib.Path(name).write_text('x\\n', encoding='utf-8')\n"
        "subprocess.run(['git', 'add', '-A'], check=True)\n"
        "subprocess.run(['git', 'commit', '-q', '-m', 'feat: ' + name], check=True)\n",
        encoding="utf-8",
    )
    return script


def _clean_verdict(
    setup: agent_review_loop.ReviewSetup,
    record: agent_review_loop.RoundRecord,
    number: int,
    _range: str,
    _previous: Path | None,
    _commit: str,
) -> tuple[agent_review_loop.Verdict, Path]:
    review = setup.review_dir / f"round-{number:02d}.md"
    review.write_text("REVIEW_STATUS: CLEAN\n", encoding="utf-8")
    record.findings = 0
    record.status = agent_review_loop.CLEAN
    return agent_review_loop.Verdict(agent_review_loop.CLEAN, 0, str(review), ""), review


def test_a_repository_that_ignores_nothing_stays_clean_across_two_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The second run is the one that used to be impossible: preflight refused the tree the first left."""
    repository = _repository(tmp_path)  # no .gitignore at all, unlike this repository
    monkeypatch.setattr(agent_review_loop, "resolve_executable", lambda name, _label: name)
    monkeypatch.setattr(
        agent_review_loop, "claude_command", lambda _options: [sys.executable, str(_sweeping_agent(tmp_path))]
    )
    monkeypatch.setattr(agent_review_loop, "perform_review", _clean_verdict)

    def run() -> int:
        return agent_review_loop.main(
            # No --allow-dirty: a tree the last run dirtied stops this outright.
            ["--repo", str(repository), "--task", "x", "--max-rounds", "1", "--heartbeat", "0"]
            + ["--review-checkout", "none"]
        )

    first = run()
    assert first == agent_review_loop.EXIT_CLEAN
    assert _in(repository, "status", "--porcelain").strip() == ""

    second = run()

    assert second == agent_review_loop.EXIT_CLEAN
    assert _in(repository, "status", "--porcelain").strip() == ""
    # Both rounds committed, and neither commit carries a line of the loop's own
    # paperwork -- which is what the agent's `git add -A` swept in before.
    assert _in(repository, "rev-list", "--count", "HEAD").strip() == "3"
    assert ".agentic-loop" not in _in(repository, "log", "--name-only", "--format=")
    # Ignored, not withheld: the reviews are on disk for a human to read.
    assert list((repository / ".agentic-loop" / "reviews").rglob("round-01.md"))
    assert (repository / ".agentic-loop" / "logs" / ".gitignore").is_file()


def test_a_tree_with_nothing_in_it_produces_no_salvage_commit(tmp_path: Path) -> None:
    """An agent that failed before touching anything has nothing worth a commit."""
    repository = _repository(tmp_path)
    head = _in(repository, "rev-parse", "HEAD")

    assert agent_review_loop.salvage_commit(repository, 1, agent_review_loop.AgentError("did not start")) is None
    assert _in(repository, "rev-parse", "HEAD") == head


def test_an_agent_that_says_nothing_for_minutes_still_reports_that_it_is_running(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`claude -p` prints nothing until it finishes; silence was read as a hang twice."""
    script = tmp_path / "quiet.py"
    script.write_text("import time\ntime.sleep(0.9)\n", encoding="utf-8")

    agent_review_loop.run_agent(
        [sys.executable, str(script)],
        "prompt",
        tmp_path,
        "[claude r1]",
        tmp_path / "log.txt",
        timeout=30,
        dry_run=False,
        heartbeat=0.2,
    )

    heartbeats = [line for line in capsys.readouterr().out.splitlines() if " of 30s, " in line]
    assert heartbeats, "an agent that printed nothing for its whole run printed nothing about itself either"
    assert all(line.startswith("[claude r1] ... ") for line in heartbeats)
    assert "no output yet" in heartbeats[0]


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="the process-group kill is the POSIX path; Windows walks the tree by pid with taskkill /T",
)
def test_a_timed_out_agents_descendant_is_dead_and_silent_before_salvage(tmp_path: Path) -> None:
    """A timeout leaves no descendant running and no late write in the tree.

    An agent blocked at the timeout is usually blocked inside a shell whose own
    children are still writing. Killing only the agent left them running, and
    `salvage_commit` runs the moment `run_agent` raises: a surviving descendant
    then races that commit and edits the tree after the round was recorded. The
    agent now leads its own process group and `_terminate_tree` signals the whole
    group and waits for it to be gone, so this delayed writer -- a grandchild of
    the loop -- is dead before the timeout is raised. Its stdout is redirected off
    the agent's pipe so the drain thread cannot mistake it for the agent still
    talking; the point under test is the kill, not the plumbing."""
    workdir = tmp_path / "work"
    workdir.mkdir()
    marker = workdir / "late-write.txt"
    child_pid_file = workdir / "child.pid"
    script = tmp_path / "agent_with_child.py"
    script.write_text(
        "import os, pathlib, subprocess, sys, time\n"
        "devnull = open(os.devnull, 'w')\n"
        # A grandchild that would write to the tree only after the timeout, then
        # stay alive -- so a survivor is both a late write and a live process.
        "child = subprocess.Popen(\n"
        "    [sys.executable, '-c',\n"
        "     \"import pathlib, time; time.sleep(2.0); \"\n"
        "     \"pathlib.Path('late-write.txt').write_text('a descendant wrote after the timeout', encoding='utf-8'); \"\n"
        "     \"time.sleep(120)\"],\n"
        "    stdout=devnull, stderr=devnull,\n"
        ")\n"
        "pathlib.Path('child.pid').write_text(str(child.pid), encoding='utf-8')\n"
        "sys.stdout.write('spawned\\n')\n"
        "sys.stdout.flush()\n"
        "time.sleep(120)\n",
        encoding="utf-8",
    )

    with pytest.raises(agent_review_loop.AgentTimeout):
        agent_review_loop.run_agent(
            [sys.executable, str(script)],
            "prompt",
            workdir,
            "[claude r1]",
            tmp_path / "log.txt",
            timeout=1,
            dry_run=False,
            heartbeat=0,
        )

    child_pid = int(child_pid_file.read_text())
    # Past when the descendant's write would have landed had it lived, so an
    # absent marker is the kill beating it rather than the check merely being early.
    time.sleep(1.5)
    assert not marker.exists(), "a descendant wrote to the working tree after the timeout"
    assert not _pid_alive(child_pid), "a descendant survived the timeout"


@pytest.mark.skipif(sys.platform == "win32", reason="the process-group kill path is POSIX-only")
def test_terminate_tree_reports_cleanup_unconfirmed_when_the_group_never_clears(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The failure the previous version returned from instead of reporting.

    After the SIGKILL pass `_group_gone` can still be false -- a member is wedged
    in an uninterruptible syscall, or the deadline was simply too short. The old
    `_terminate_tree` fell off the end of its loop and returned as if the tree were
    gone, and `salvage_commit` then ran against a tree a survivor might still be
    editing. It must raise instead, so the caller knows cleanup did not finish."""
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True
    )
    # The real SIGTERM/SIGKILL still go out; only the liveness answer is forced, so
    # this pins the decision `_terminate_tree` makes when the group looks unclear.
    monkeypatch.setattr(agent_review_loop, "_group_gone", lambda pgid, *, deadline: False)
    try:
        with pytest.raises(agent_review_loop.CleanupUnconfirmed):
            agent_review_loop._terminate_tree(process, grace_s=0.1)
    finally:
        process.kill()
        process.wait()


@pytest.mark.skipif(sys.platform == "win32", reason="the process-group kill path is POSIX-only")
def test_terminate_tree_reports_cleanup_unconfirmed_when_a_signal_cannot_be_delivered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A SIGKILL that cannot be delivered leaves the group's state unknown.

    `_terminate_tree` used to suppress every signalling error, so a killpg that
    failed with EPERM was indistinguishable from one that worked. A signal that
    did not land means the members are still there and were not stopped, which is
    the one thing that must not be read as a clean kill."""
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True
    )
    monkeypatch.setattr(agent_review_loop, "_signal_group", lambda pgid, sig: False)
    try:
        with pytest.raises(agent_review_loop.CleanupUnconfirmed):
            agent_review_loop._terminate_tree(process, grace_s=0.1)
    finally:
        process.kill()
        process.wait()


@pytest.mark.skipif(sys.platform == "win32", reason="killpg is POSIX-only")
def test_group_gone_treats_an_unclearable_group_as_not_gone(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only a group with no members left counts as gone.

    `killpg(pgid, 0)` raising `ProcessLookupError` is the group being empty --
    the one positive answer. A `PermissionError` is a member still there but not
    ours to signal, and the old code returned True for it, calling a group that
    demonstrably still had a process 'gone'. It has to keep waiting and then
    report not-gone, or an unkillable descendant would be reported as cleaned up."""
    monkeypatch.setattr(agent_review_loop.os, "killpg", lambda pgid, sig: (_ for _ in ()).throw(PermissionError()))
    assert agent_review_loop._group_gone(4321, deadline=time.monotonic() + 0.05) is False

    monkeypatch.setattr(agent_review_loop.os, "killpg", lambda pgid, sig: (_ for _ in ()).throw(ProcessLookupError()))
    assert agent_review_loop._group_gone(4321, deadline=time.monotonic() + 5.0) is True


def test_terminate_tree_reports_cleanup_unconfirmed_when_taskkill_cannot_confirm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Windows tree walk, faced the same way: a taskkill that did not confirm.

    `taskkill /T` returns 0 on a kill and 128 when the pid is already gone; any
    other code means it did not confirm the tree was killed. Exercised on every
    platform by forcing the Windows branch, because that is the half a POSIX CI
    never runs and where an unconfirmed kill would otherwise pass silently."""
    monkeypatch.setattr(agent_review_loop.sys, "platform", "win32")
    monkeypatch.setattr(
        agent_review_loop.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a[0] if a else [], 1, "", "the process could not be terminated"),
    )

    class _FakeProcess:
        pid = 4242

        def kill(self) -> None:
            pass

        def wait(self, timeout: float | None = None) -> int:
            return 1

    with pytest.raises(agent_review_loop.CleanupUnconfirmed):
        agent_review_loop._terminate_tree(_FakeProcess(), grace_s=0.1)  # type: ignore[arg-type]


@pytest.mark.skipif(sys.platform == "win32", reason="the process-group kill path is POSIX-only")
def test_run_agent_surfaces_cleanup_unconfirmed_rather_than_a_plain_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The distinct failure the loop reads to skip salvage.

    When the timeout fires and the tree cannot be confirmed gone, `run_agent` must
    raise `CleanupUnconfirmed`, not the ordinary `AgentTimeout`: the two are the
    same event but a different situation, and only the distinct type tells the
    caller a descendant may still be editing the tree."""
    workdir = tmp_path / "work"
    workdir.mkdir()
    script = tmp_path / "slow.py"
    script.write_text("import sys, time\nsys.stdout.write('go\\n')\nsys.stdout.flush()\ntime.sleep(120)\n", encoding="utf-8")
    monkeypatch.setattr(agent_review_loop, "_group_gone", lambda pgid, *, deadline: False)

    with pytest.raises(agent_review_loop.CleanupUnconfirmed):
        agent_review_loop.run_agent(
            [sys.executable, str(script)],
            "prompt",
            workdir,
            "[claude r1]",
            tmp_path / "log.txt",
            timeout=1,
            dry_run=False,
            heartbeat=0,
        )


@pytest.mark.skipif(sys.platform == "win32", reason="the process-group kill path is POSIX-only")
def test_a_round_whose_cleanup_is_unconfirmed_is_not_salvaged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Salvage is the working-tree mutation that must not run over live survivors.

    `salvage_commit` `git add -A`s the tree and commits it. If the killed agent's
    processes were not confirmed gone, one may still be writing, so committing now
    would capture a half-written tree as the round's work -- the very race salvage
    exists to avoid, arriving through cleanup that could not finish. The loop must
    leave the tree exactly as it is and fail, not commit it."""
    repository = _repository(tmp_path)
    # The kill still happens; only the confirmation is withheld, so the loop takes
    # the unconfirmed-cleanup path with a tree that is in fact quiescent -- which
    # is what lets the test assert on it without racing a real survivor.
    monkeypatch.setattr(agent_review_loop, "_group_gone", lambda pgid, *, deadline: False)

    exit_code = _cut_short(repository, _slow_agent(tmp_path), monkeypatch)

    assert exit_code == agent_review_loop.EXIT_FAILED
    # No salvage commit: HEAD is still the repository's one initial commit.
    assert _in(repository, "log", "--oneline").strip().count("\n") == 0
    assert _in(repository, "log", "-1", "--format=%s").strip() == "initial"
    # The round's work is left in the tree, uncommitted, for an operator to resolve
    # once the survivors are dealt with -- not swept into a commit underneath them.
    assert "half-done.py" in _in(repository, "status", "--porcelain")


def test_each_agent_gets_a_scratch_directory_no_round_has_used_before(tmp_path: Path) -> None:
    """pytest deletes an existing --basetemp and recreates it, which Windows refuses."""
    first = agent_review_loop.round_scratch(tmp_path, 1, "claude")
    second = agent_review_loop.round_scratch(tmp_path, 2, "claude")
    reviewer = agent_review_loop.round_scratch(tmp_path, 1, "codex")

    assert len({first, second, reviewer}) == 3
    assert all(path.is_dir() and not any(path.iterdir()) for path in (first, second, reviewer))
    # A directory left over from an interrupted run is not handed out again.
    again = agent_review_loop.round_scratch(tmp_path, 1, "claude")
    assert again != first
    assert again.is_dir()


def test_the_scratch_directory_is_named_in_the_prompt_and_not_only_in_the_environment(tmp_path: Path) -> None:
    """The agent writes its own pytest command line, so TMP and TEMP do not decide this."""
    scratch = tmp_path / "round-01-claude"
    prompts = [
        agent_review_loop.implement_prompt("task", tmp_path / "reviews", "main", scratch),
        agent_review_loop.fix_prompt("task", tmp_path / "review.md", 2, "main", scratch),
        agent_review_loop.review_prompt(
            "task", tmp_path / "review.md", "a..b", 1, None, True, scratch, has_network=False
        ),
    ]

    for prompt in prompts:
        assert f"--basetemp={scratch.as_posix()}/pytest-1" in prompt
    env = agent_review_loop.agent_env(scratch)
    assert [env[key] for key in ("TMP", "TEMP", "TMPDIR", "PYTEST_DEBUG_TEMPROOT")] == [str(scratch)] * 4


def test_the_reviewers_scratch_is_a_directory_its_sandbox_can_write_to(tmp_path: Path) -> None:
    """Without this the reviewer put its base temp directory beside the review document."""
    options = argparse.Namespace(
        codex_bin="codex", codex_model=None, codex_effort=None, codex_sandbox="workspace-write", codex_arg=[]
    )
    scratch = tmp_path / "scratch"

    command = agent_review_loop.codex_command(
        options, tmp_path / "schema.json", tmp_path / "last.txt", tmp_path / "reviews", tmp_path / "checkout", scratch
    )

    granted = [value for flag, value in zip(command, command[1:], strict=False) if flag == "--add-dir"]
    assert str(scratch) in granted


@pytest.mark.parametrize(
    ("sandbox", "codex_arg", "expected"),
    [
        ("workspace-write", [], False),
        ("read-only", [], False),
        ("danger-full-access", [], True),
        ("workspace-write", ["-c", "sandbox_workspace_write.network_access=true"], True),
        ("workspace-write", ["-csandbox_workspace_write.network_access=true"], True),
        ("workspace-write", ["--config=sandbox_workspace_write.network_access=1"], True),
        ("workspace-write", ["-c", "model_verbosity=high"], False),
        # Turning it on for workspace-write says nothing about read-only.
        ("read-only", ["-c", "sandbox_workspace_write.network_access=true"], False),
    ],
)
def test_whether_the_reviewer_can_reach_the_network_is_read_rather_than_assumed(
    sandbox: str, codex_arg: list[str], expected: bool
) -> None:
    assert agent_review_loop.reviewer_network(sandbox, codex_arg) is expected


def test_a_reviewer_without_network_is_told_so_and_the_caller_is_warned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A task that links to an issue is complete for the implementer and empty for the reviewer."""
    repository = _repository(tmp_path)
    monkeypatch.setattr(agent_review_loop, "resolve_executable", lambda name, _label: name)

    agent_review_loop.main(
        [
            "--repo",
            str(repository),
            "--task",
            "implement https://github.com/agentic-hil/agentic-hil/issues/1",
            "--max-rounds",
            "1",
            "--dry-run",
            "--review-checkout",
            "none",
        ]
    )

    captured = capsys.readouterr()
    assert "network    : reviewer has no network" in captured.out
    assert "points at something only a networked agent can read" in captured.err
    # And the reviewer is told in its own prompt, so it does not spend turns trying.
    offline = agent_review_loop.review_prompt(
        "t", tmp_path / "r.md", "a..b", 1, None, True, tmp_path / "s", has_network=False
    )
    online = agent_review_loop.review_prompt(
        "t", tmp_path / "r.md", "a..b", 1, None, True, tmp_path / "s", has_network=True
    )
    assert "NO NETWORK" in offline
    assert "NO NETWORK" not in online


@pytest.mark.parametrize(
    ("findings", "flat"),
    [
        ([10, 9, 4, 4], 1),
        ([8, 7, 6], 0),
        ([4, 4, 4], 2),
        # Back where it has already been is not progress, so the round that went
        # up does not reset what the round coming down then repeats.
        ([4, 5, 4], 2),
        # A verdict read from the sentinel carries no number; it is neither.
        ([4, -1, 4], 1),
        ([], 0),
    ],
)
def test_a_review_that_stops_getting_smaller_is_counted(findings: list[int], flat: int) -> None:
    """10, 9, 4, 4 and 8, 7, 6 were measured; --max-rounds cannot tell them apart."""
    assert agent_review_loop.rounds_without_progress(findings) == flat


def test_a_run_that_has_stopped_contributing_ends_before_max_rounds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Five rounds were allowed; the third one that found the same four ends it."""
    repository = _repository(tmp_path)
    committer = tmp_path / "commit_agent.py"
    committer.write_text(
        "import pathlib, subprocess, uuid\n"
        "name = 'change-%s.py' % uuid.uuid4().hex[:8]\n"
        "pathlib.Path(name).write_text('x\\n', encoding='utf-8')\n"
        "subprocess.run(['git', 'add', name], check=True)\n"
        "subprocess.run(['git', 'commit', '-q', '-m', 'feat: ' + name], check=True)\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(agent_review_loop, "resolve_executable", lambda name, _label: name)
    monkeypatch.setattr(agent_review_loop, "claude_command", lambda _options: [sys.executable, str(committer)])

    def canned(
        setup: agent_review_loop.ReviewSetup,
        record: agent_review_loop.RoundRecord,
        number: int,
        _range: str,
        _previous: Path | None,
        _commit: str,
    ) -> tuple[agent_review_loop.Verdict, Path]:
        review = setup.review_dir / f"round-{number:02d}.md"
        review.write_text("REVIEW_STATUS: CHANGES_REQUESTED\n", encoding="utf-8")
        record.findings = 4
        record.status = agent_review_loop.CHANGES_REQUESTED
        return agent_review_loop.Verdict(agent_review_loop.CHANGES_REQUESTED, 4, str(review), ""), review

    monkeypatch.setattr(agent_review_loop, "perform_review", canned)

    exit_code = agent_review_loop.main(
        [
            "--repo",
            str(repository),
            "--task",
            "x",
            "--max-rounds",
            "5",
            "--stop-after-flat-rounds",
            "2",
            "--heartbeat",
            "0",
            "--review-checkout",
            "none",
        ]
    )

    assert exit_code == agent_review_loop.EXIT_NO_PROGRESS
    assert "have not got the review below 4 findings" in capsys.readouterr().err
    # Three rounds, not five: one that set the mark and two that did not beat it.
    summary = json.loads(next((repository / ".agentic-loop" / "logs").rglob("run.json")).read_text(encoding="utf-8"))
    assert len(summary["rounds"]) == 3
    assert summary["stop_after_flat_rounds"] == 2
