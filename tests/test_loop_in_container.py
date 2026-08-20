from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import types
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
        "import pathlib, subprocess, sys, uuid\n"
        "sys.stdin.read()\n"
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


# --- no commit is two rounds: one declined, one stalled with the work in the tree ---

# What the stalled implementer wrote before it stopped. Asserted byte for byte
# wherever the work is recovered: a recovery that rewrites the fix has lost it as
# surely as one that discards it.
FIX = "def fixed():\n    return 'the whole fix, written and never committed'\n"


def _stalling_agent(tmp_path: Path, *, then: str) -> Path:
    """An agent that writes a complete fix, never commits it, and does `then` next time.

    The observed failure: the implementer finished, started the test suite, and
    died waiting on a notification its harness never delivered. From the loop's
    side that is an agent that exited without committing, with a working tree full
    of finished work -- indistinguishable, until now, from one that read the
    findings and declined the round.
    """
    script = tmp_path / "stalling_agent.py"
    attempts = (tmp_path / "attempts.txt").as_posix()
    script.write_text(
        "import pathlib, subprocess, sys\n"
        # The loop writes the prompt to stdin right after the spawn. An agent
        # that exits without reading it hands the loop EPIPE instead of a
        # round, which is a race about scheduling rather than anything this
        # test is about, and it failed a CI leg. Every other fake agent in
        # this file drains stdin first, and so does every real agent CLI.
        "sys.stdin.read()\n"
        f"attempts = pathlib.Path({attempts!r})\n"
        "seen = len(attempts.read_text(encoding='utf-8')) if attempts.exists() else 0\n"
        "attempts.write_text('x' * (seen + 1), encoding='utf-8')\n"
        "if seen == 0:\n"
        f"    pathlib.Path('fix.py').write_text({FIX!r}, encoding='utf-8')\n"
        "    sys.stdout.write('wrote the fix, then never came back to commit it\\n')\n"
        "    raise SystemExit(0)\n"
        f"{then}\n",
        encoding="utf-8",
    )
    return script


_COMMITS = (
    "subprocess.run(['git', 'add', '-A'], check=True)\n"
    "subprocess.run(['git', 'commit', '-q', '-m', 'fix: finish what the stalled attempt wrote'], check=True)\n"
)
_STALLS_AGAIN = "sys.stdout.write('and did not commit it this time either\\n')\n"
_REMOVES_IT = "pathlib.Path('fix.py').unlink()\nsys.stdout.write('that was not work worth keeping\\n')\n"


def _one_round(repository: Path, script: Path, monkeypatch: pytest.MonkeyPatch, *extra: str) -> int:
    """One implement round against a faked agent, with the review canned clean.

    `extra` goes on the end of the command line, so a test that is about one
    option says only that option and inherits the rest of the shape every other
    test here runs in.
    """
    monkeypatch.setattr(agent_review_loop, "resolve_executable", lambda name, _label: name)
    monkeypatch.setattr(agent_review_loop, "claude_command", lambda _options: [sys.executable, str(script)])
    monkeypatch.setattr(agent_review_loop, "perform_review", _clean_verdict)
    return agent_review_loop.main(
        ["--repo", str(repository), "--task", "x", "--max-rounds", "1", "--heartbeat", "0"]
        + ["--review-checkout", "none", *extra]
    )


def _round_records(repository: Path) -> list[dict[str, object]]:
    summary = next((repository / ".agentic-loop" / "logs").rglob("run.json")).read_text(encoding="utf-8")
    return json.loads(summary)["rounds"]


class _FrozenClock:
    """One second, for two runs that must still not share a directory."""

    @staticmethod
    def now() -> datetime:
        return datetime(2026, 8, 16, 22, 52, 33)


def test_two_runs_that_start_in_the_same_second_get_temporary_roots_of_their_own(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Shared temporary storage needs a name for the run, not for the second.

    The scratch root was `<repo name>-<run id>` under the system temp, and the
    run id names the second. A machine runs several clones of one project and a
    suite builds fixture repositories that are all called the same thing, so two
    runs starting together met in one directory: one created a round directory
    the other had just removed, and the round died on the collision rather than
    on anything it was reviewing.
    """
    monkeypatch.setattr(agent_review_loop, "datetime", _FrozenClock)
    roots: list[str] = []
    for name in ("first", "second"):
        (tmp_path / name).mkdir()
        repository = _repository(tmp_path / name)
        agent = _stalling_agent(tmp_path / name, then=_COMMITS)

        assert _one_round(repository, agent, monkeypatch) == agent_review_loop.EXIT_CLEAN

        roots += [line for line in capsys.readouterr().out.splitlines() if line.startswith("scratch")]

    # Same repository name, same second, and still two directories.
    assert len(roots) == 2, roots
    assert roots[0] != roots[1], roots


def test_a_no_commit_round_that_left_work_in_the_tree_is_finished_rather_than_called_declined(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The round reported "produced no commit" while the whole fix lay in the tree; it was salvaged by hand."""
    repository = _repository(tmp_path)

    exit_code = _one_round(repository, _stalling_agent(tmp_path, then=_COMMITS), monkeypatch)

    assert exit_code == agent_review_loop.EXIT_CLEAN
    # The retry committed, so the round has a commit an agent stood behind and the
    # review ran on it -- rather than a stalled run and a hand salvage next morning.
    assert _in(repository, "log", "-1", "--format=%s").strip() == "fix: finish what the stalled attempt wrote"
    assert _in(repository, "show", "--name-only", "--format=", "HEAD").split() == ["fix.py"]
    # Byte for byte what the stalled attempt wrote: the retry finishes that work,
    # it does not reimplement the task over the top of it.
    assert (repository / "fix.py").read_text(encoding="utf-8") == FIX
    assert _in(repository, "status", "--porcelain").strip() == ""
    # The round is logged as the thing it was, not as a round nobody can explain.
    out = capsys.readouterr().out
    assert "round 1: implementer stalled with uncommitted work" in out
    assert "fix.py" in out
    record = _round_records(repository)[0]
    assert (record["stalled"], record["retried"], record["salvaged"]) == (True, True, None)
    # The run summary names what was in the tree, not just that something was.
    assert record["uncommitted_paths"] == ["?? fix.py"]


def test_a_no_commit_round_with_a_clean_tree_is_still_a_declined_round(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An implementer that looked and judged there was nothing to do is not a stall."""
    repository = _repository(tmp_path)
    declining = tmp_path / "declining_agent.py"
    declining.write_text(
        "import sys\nsys.stdout.write('the findings are wrong; changing nothing\\n')\n", encoding="utf-8"
    )
    head = _in(repository, "rev-parse", "HEAD")

    exit_code = _one_round(repository, declining, monkeypatch)

    assert exit_code == agent_review_loop.EXIT_STALLED
    assert _in(repository, "rev-parse", "HEAD") == head  # no retry, and nothing committed on its behalf
    captured = capsys.readouterr()
    assert "round 1: Claude Code produced no commit." in captured.out
    assert "stopping as stalled" in captured.err
    assert "stalled with uncommitted work" not in captured.out
    record = _round_records(repository)[0]
    assert (record["stalled"], record["retried"], record["salvaged"]) == (False, False, None)
    # One implementer invocation, so one transcript: nothing was handed back.
    assert not list((repository / ".agentic-loop" / "logs").rglob("*-finish.log"))


def test_work_the_implementer_will_not_commit_twice_is_committed_by_the_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The tree is the only copy of that round; the run must not end with it still only there."""
    repository = _repository(tmp_path)

    exit_code = _one_round(repository, _stalling_agent(tmp_path, then=_STALLS_AGAIN), monkeypatch)

    assert exit_code == agent_review_loop.EXIT_CLEAN
    subject = _in(repository, "log", "-1", "--format=%s").strip()
    assert subject == "wip(review-loop): round 1 left the implementer's work uncommitted"
    assert "never committed it, twice" in _in(repository, "log", "-1", "--format=%b")
    # Preserved exactly, and no longer only in a working tree.
    assert (repository / "fix.py").read_text(encoding="utf-8") == FIX
    assert _in(repository, "show", "HEAD:fix.py") == FIX
    assert _in(repository, "status", "--porcelain").strip() == ""
    record = _round_records(repository)[0]
    assert (record["stalled"], record["retried"]) == (True, True)
    assert record["salvaged"] is not None and record["commits"] == [record["salvaged"]]


_COMMITS_PART_AND_LEAVES_MORE_DIRTY = (
    "subprocess.run(['git', 'add', 'fix.py'], check=True)\n"
    "subprocess.run(['git', 'commit', '-q', '-m', 'fix: finish what the stalled attempt wrote'], check=True)\n"
    "pathlib.Path('extra.py').write_text('# more of the round, never committed\\n', encoding='utf-8')\n"
    "sys.stdout.write('committed part of it and left the rest in the tree\\n')\n"
)


def test_a_partial_second_commit_does_not_hide_the_rest_still_left_uncommitted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`HEAD` moving is not the same as the round being finished.

    The second attempt commits `fix.py` -- moving `HEAD` -- and leaves `extra.py`
    sitting dirty in the tree. A recovery that stops at "HEAD moved" would send
    only the partial commit to review and lose `extra.py` exactly as if this
    recovery path did not exist; it must be swept into history instead.
    """
    repository = _repository(tmp_path)

    exit_code = _one_round(repository, _stalling_agent(tmp_path, then=_COMMITS_PART_AND_LEAVES_MORE_DIRTY), monkeypatch)

    assert exit_code == agent_review_loop.EXIT_CLEAN
    # Two commits: the partial one the agent stood behind, and the salvage that
    # picked up what it still left dirty.
    subject = _in(repository, "log", "-1", "--format=%s").strip()
    assert subject == "wip(review-loop): round 1 left the implementer's work uncommitted"
    assert _in(repository, "log", "-2", "--format=%s").splitlines() == [
        "wip(review-loop): round 1 left the implementer's work uncommitted",
        "fix: finish what the stalled attempt wrote",
    ]
    assert _in(repository, "show", "HEAD:fix.py") == FIX
    assert _in(repository, "show", "HEAD:extra.py") == "# more of the round, never committed\n"
    assert _in(repository, "status", "--porcelain").strip() == ""
    record = _round_records(repository)[0]
    assert (record["stalled"], record["retried"]) == (True, True)
    assert record["salvaged"] is not None
    # Both commits reached the review, not just the partial one.
    assert len(record["commits"]) == 2


def test_a_recovery_attempt_that_is_cut_short_still_leaves_the_stalled_work_in_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rescue can hit the wall the round hit, and it must not take the work down with it."""
    repository = _repository(tmp_path)
    # Attempt one stalls; attempt two runs into --timeout, which is the failure
    # that left the work uncommitted in the first place.
    script = _stalling_agent(tmp_path, then="import time\ntime.sleep(120)\n")
    monkeypatch.setattr(agent_review_loop, "resolve_executable", lambda name, _label: name)
    monkeypatch.setattr(agent_review_loop, "claude_command", lambda _options: [sys.executable, str(script)])

    exit_code = agent_review_loop.main(
        ["--repo", str(repository), "--task", "x", "--max-rounds", "1", "--timeout", "2", "--heartbeat", "0"]
        + ["--review-checkout", "none"]
    )

    assert exit_code == agent_review_loop.EXIT_FAILED
    # The run ends, but not with the only copy of the round in a working tree.
    assert _in(repository, "log", "-1", "--format=%s").startswith("wip(review-loop): round 1")
    assert _in(repository, "show", "HEAD:fix.py") == FIX
    record = _round_records(repository)[0]
    assert (record["stalled"], record["retried"]) == (True, True)
    assert record["salvaged"] is not None


def test_an_implementer_that_takes_its_own_leftovers_back_out_ends_the_round_declined(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rule 5 of the finish prompt: reject the work by removing it, not by leaving it lying there."""
    repository = _repository(tmp_path)
    head = _in(repository, "rev-parse", "HEAD")

    exit_code = _one_round(repository, _stalling_agent(tmp_path, then=_REMOVES_IT), monkeypatch)

    assert exit_code == agent_review_loop.EXIT_STALLED
    assert _in(repository, "rev-parse", "HEAD") == head  # nothing to preserve, so nothing is committed
    record = _round_records(repository)[0]
    assert (record["stalled"], record["retried"], record["salvaged"]) == (True, True, None)


# What the reviewer leaves in the implementer's tree under --review-checkout
# none. A scratch note it wrote in the repository instead of in its scratch
# directory is the shape this was reported in; a cache a test run built is the
# same thing with a longer name.
REVIEWER_LEFTOVER = "codex-scratch.txt"


def _reviewer_that_leaves_a_file(
    setup: agent_review_loop.ReviewSetup,
    record: agent_review_loop.RoundRecord,
    number: int,
    _range: str,
    _previous: Path | None,
    _commit: str,
) -> tuple[agent_review_loop.Verdict, Path]:
    """A reviewer that writes into the tree it just reviewed.

    `--review-checkout none` is what lets it: the reviewer works in the
    implementer's own tree rather than a checkout of its own, so what it leaves
    is still lying there when the next round's implementer runs. The verdict is
    CHANGES_REQUESTED because the loop has to reach that next round for any of
    this to matter.
    """
    review = setup.review_dir / f"round-{number:02d}.md"
    review.write_text("REVIEW_STATUS: CHANGES_REQUESTED\n", encoding="utf-8")
    (setup.repo / REVIEWER_LEFTOVER).write_text("the reviewer's own scratch\n", encoding="utf-8")
    record.findings = 1
    record.status = agent_review_loop.CHANGES_REQUESTED
    return agent_review_loop.Verdict(agent_review_loop.CHANGES_REQUESTED, 1, str(review), ""), review


def _two_rounds(repository: Path, script: Path, monkeypatch: pytest.MonkeyPatch) -> int:
    """Two rounds sharing one tree, with a reviewer that leaves something in it."""
    monkeypatch.setattr(agent_review_loop, "resolve_executable", lambda name, _label: name)
    monkeypatch.setattr(agent_review_loop, "claude_command", lambda _options: [sys.executable, str(script)])
    monkeypatch.setattr(agent_review_loop, "perform_review", _reviewer_that_leaves_a_file)
    return agent_review_loop.main(
        ["--repo", str(repository), "--task", "x", "--max-rounds", "2", "--heartbeat", "0"]
        + ["--review-checkout", "none"]
    )


def _agent(tmp_path: Path, name: str, *attempts: str) -> Path:
    """An agent that does something different on each invocation of a run.

    The stall tests above each need one script whose behaviour is a sequence, and
    counting invocations in a file beside the script is how `_stalling_agent`
    already does it. This is the same counter with the sequence spelled out by
    the caller, because these tests care about the third invocation as much as
    the first.
    """
    script = tmp_path / f"{name}.py"
    counter = (tmp_path / f"{name}-attempts.txt").as_posix()
    body = "".join(
        f"if seen == {index}:\n" + "".join(f"    {line}\n" for line in step.splitlines()) + "    raise SystemExit(0)\n"
        for index, step in enumerate(attempts)
    )
    script.write_text(
        "import pathlib, subprocess, sys\n"
        # As every fake agent here does, and as every real agent CLI does: an
        # agent that exits without reading races the prompt the loop writes
        # straight after the spawn and hands it EPIPE instead of a round.
        "sys.stdin.read()\n"
        f"attempts = pathlib.Path({counter!r})\n"
        "seen = len(attempts.read_text(encoding='utf-8')) if attempts.exists() else 0\n"
        "attempts.write_text('x' * (seen + 1), encoding='utf-8')\n" + body,
        encoding="utf-8",
    )
    return script


def _attempts(tmp_path: Path, name: str) -> int:
    counter = tmp_path / f"{name}-attempts.txt"
    return len(counter.read_text(encoding="utf-8")) if counter.exists() else 0


_COMMIT_ROUND_ONE = (
    f"pathlib.Path('fix.py').write_text({FIX!r}, encoding='utf-8')\n"
    "subprocess.run(['git', 'add', 'fix.py'], check=True)\n"
    "subprocess.run(['git', 'commit', '-q', '-m', 'fix: round one'], check=True)\n"
)
_DECLINES = "sys.stdout.write('the findings are wrong; changing nothing\\n')\n"
_WRITES_WITHOUT_COMMITTING = (
    "pathlib.Path('more.py').write_text('# round two, never committed\\n', encoding='utf-8')\n"
    "sys.stdout.write('wrote it, then never came back to commit it\\n')\n"
)
_COMMITS_ONLY_ITS_OWN = (
    "subprocess.run(['git', 'add', 'more.py'], check=True)\n"
    "subprocess.run(['git', 'commit', '-q', '-m', 'fix: finish what the stalled attempt wrote'], check=True)\n"
)


def test_a_round_the_implementer_declined_is_not_a_stall_because_the_reviewer_left_a_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The misread `--review-checkout none` bought, and what it cost.

    The reviewer shares the implementer's tree there, so its leavings are in that
    tree when the next round's implementer looks at the findings and declines.
    The classification asked whether the tree was dirty, so that declined round
    read as a stall: a second implementer invocation, up to another whole
    `--timeout`, to be told there was nothing to finish, and `run.json` recording
    `stalled` for a round that was nothing of the kind. The question is what the
    round added now, so the reviewer's file is inherited rather than counted, and
    the round is reported as the declined round it is.
    """
    repository = _repository(tmp_path)
    agent = _agent(tmp_path, "commits_then_declines", _COMMIT_ROUND_ONE, _DECLINES)

    exit_code = _two_rounds(repository, agent, monkeypatch)

    assert exit_code == agent_review_loop.EXIT_STALLED
    # Two invocations, one per round: the third is the one this used to spend.
    assert _attempts(tmp_path, "commits_then_declines") == 2
    assert not list((repository / ".agentic-loop" / "logs").rglob("*-finish.log"))
    captured = capsys.readouterr()
    assert "round 2: Claude Code produced no commit." in captured.out
    assert "stalled with uncommitted work" not in captured.out
    declined = _round_records(repository)[1]
    assert (declined["stalled"], declined["retried"], declined["salvaged"]) == (False, False, None)
    # Round one's commit and nothing after it, and the reviewer's file is still
    # exactly where the reviewer put it: this decides, it does not tidy.
    assert _in(repository, "rev-list", "--count", "HEAD").strip() == "2"
    assert _in(repository, "status", "--porcelain").strip() == f"?? {REVIEWER_LEFTOVER}"


def test_a_retry_that_finished_the_job_is_not_salvaged_over_the_reviewers_leavings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same misread at the other end of the recovery.

    Here the second round really did stall, and the retry really did commit what
    it was handed. Asking the whole tree afterwards still found the reviewer's
    file, so "it finished the job" read false and the loop put a work-in-progress
    commit on top of a round that was complete, sweeping the reviewer's leavings
    into it. Measured against what the round inherited, the retry's commit is the
    end of it.
    """
    repository = _repository(tmp_path)
    agent = _agent(
        tmp_path,
        "stalls_then_finishes",
        _COMMIT_ROUND_ONE,
        _WRITES_WITHOUT_COMMITTING,
        _COMMITS_ONLY_ITS_OWN,
    )

    exit_code = _two_rounds(repository, agent, monkeypatch)

    assert exit_code == agent_review_loop.EXIT_ROUNDS_EXHAUSTED  # the canned reviewer never comes back clean
    assert _attempts(tmp_path, "stalls_then_finishes") == 3
    stalled = _round_records(repository)[1]
    assert (stalled["stalled"], stalled["retried"], stalled["salvaged"]) == (True, True, None)
    # The retry's own commit is the last word; no wip commit was made over it.
    assert _in(repository, "log", "-1", "--format=%s").strip() == "fix: finish what the stalled attempt wrote"
    assert "wip(review-loop)" not in _in(repository, "log", "--format=%s")
    # And the reviewer's file was neither committed nor removed.
    assert _in(repository, "status", "--porcelain").strip() == f"?? {REVIEWER_LEFTOVER}"


def test_no_stall_retry_reports_the_stalled_round_instead_of_handing_it_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The switch the recovery never had.

    The retry is worth its cost by default, but it is a second agent invocation
    and it can take another whole `--timeout`; on an overnight run of an hour a
    round that is a real slice of the night spent on a round that has already
    failed once to commit what it wrote. `--no-stall-retry` restores the
    stop-immediately behaviour that predated the recovery, with the report the
    recovery brought with it: the round is named a stall, the paths are listed on
    the console and in `run.json`, and the work is left exactly where it is
    rather than committed on the implementer's behalf.
    """
    repository = _repository(tmp_path)
    head = _in(repository, "rev-parse", "HEAD")

    exit_code = _one_round(repository, _stalling_agent(tmp_path, then=_COMMITS), monkeypatch, "--no-stall-retry")

    assert exit_code == agent_review_loop.EXIT_STALLED
    # One invocation: the agent that would have committed on a second one never
    # gets it, which is the whole of what the flag buys.
    assert (tmp_path / "attempts.txt").read_text(encoding="utf-8") == "x"
    assert not list((repository / ".agentic-loop" / "logs").rglob("*-finish.log"))
    # Nothing committed on its behalf, and nothing thrown away either.
    assert _in(repository, "rev-parse", "HEAD") == head
    assert (repository / "fix.py").read_text(encoding="utf-8") == FIX
    out = capsys.readouterr().out
    assert "round 1: implementer stalled with uncommitted work" in out
    assert "?? fix.py" in out
    assert "--no-stall-retry" in out
    record = _round_records(repository)[0]
    assert (record["stalled"], record["retried"], record["salvaged"]) == (True, False, None)
    assert record["uncommitted_paths"] == ["?? fix.py"]
    summary = next((repository / ".agentic-loop" / "logs").rglob("run.json")).read_text(encoding="utf-8")
    assert json.loads(summary)["stall_retry"] is False


def test_no_stall_retry_leaves_it_to_the_no_commit_rule_whether_the_run_goes_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without the retry a stalled round is a no-commit round, and that rule already exists.

    So the two options compose rather than each having their own idea of when a
    run ends: `--continue-on-no-commit` carries the round to review the same way
    it carries a declined one, and the work stays in the tree for the next round
    to pick up. What it must not do is end the run clean: the review runs on a
    commit-only range that never saw the stalled work, so a clean verdict is not
    a verdict on it, and the run stops as stalled rather than claiming otherwise.
    """
    repository = _repository(tmp_path)

    exit_code = _one_round(
        repository, _stalling_agent(tmp_path, then=_COMMITS), monkeypatch, "--no-stall-retry", "--continue-on-no-commit"
    )

    # The canned review comes back clean, but the round's own work never reached
    # it, so the run is stalled, not clean -- and the work is still in the tree.
    assert exit_code == agent_review_loop.EXIT_STALLED
    assert (repository / "fix.py").read_text(encoding="utf-8") == FIX
    record = _round_records(repository)[0]
    assert (record["stalled"], record["retried"]) == (True, False)


def test_a_stalled_file_cannot_produce_a_clean_run_under_the_default_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The default checkout is where the false-clean is worst: the file is not even there.

    `--review-checkout clone` (the default) reviews a fresh checkout of the
    committed head, so a stalled round's uncommitted work does not exist in the
    tree the reviewer reads at all. A clean verdict on that checkout says nothing
    about the work, and `--no-stall-retry --continue-on-no-commit` must not let it
    end the run at exit 0 with `fix.py` still dirty and never looked at.
    """
    repository = _repository(tmp_path)

    monkeypatch.setattr(agent_review_loop, "resolve_executable", lambda name, _label: name)
    monkeypatch.setattr(
        agent_review_loop, "claude_command", lambda _options: [sys.executable, str(_stalling_agent(tmp_path, then=_COMMITS))]
    )
    monkeypatch.setattr(agent_review_loop, "perform_review", _clean_verdict)
    # No --review-checkout: the run takes the default clone, which is the whole point.
    exit_code = agent_review_loop.main(
        ["--repo", str(repository), "--task", "x", "--max-rounds", "1", "--heartbeat", "0"]
        + ["--no-stall-retry", "--continue-on-no-commit"]
    )

    assert exit_code == agent_review_loop.EXIT_STALLED
    assert (repository / "fix.py").read_text(encoding="utf-8") == FIX
    assert _in(repository, "status", "--porcelain").strip() == "?? fix.py"
    captured = capsys.readouterr()
    assert "the review never saw" in f"{captured.out}{captured.err}"


def _requested_then_clean(
    setup: agent_review_loop.ReviewSetup,
    record: agent_review_loop.RoundRecord,
    number: int,
    _range: str,
    _previous: Path | None,
    _commit: str,
) -> tuple[agent_review_loop.Verdict, Path]:
    """Round 1 requests changes so the run goes on; every round after comes back clean.

    The two-round shape the carried-across guard needs: the stall is in round 1,
    whose findings keep the loop alive, and the clean verdict that must not end
    the run arrives a round later, once the stall is a round in the past.
    """
    if number == 1:
        review = setup.review_dir / f"round-{number:02d}.md"
        review.write_text("REVIEW_STATUS: CHANGES_REQUESTED\n", encoding="utf-8")
        record.findings = 1
        record.status = agent_review_loop.CHANGES_REQUESTED
        return agent_review_loop.Verdict(agent_review_loop.CHANGES_REQUESTED, 1, str(review), ""), review
    return _clean_verdict(setup, record, number, _range, _previous, _commit)


def test_a_stall_that_outlives_its_round_still_blocks_a_clean_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The guard has to remember the stall across rounds, not only within the one that stalled.

    Round 1 stalls under `--no-stall-retry --continue-on-no-commit`: it writes
    `fix.py`, never commits it, and its review requests changes, so the run goes
    on with the file still dirty. Round 2's implementer declines -- no change, no
    commit -- so nothing is *newly* uncommitted this round, and its review, which
    measures committed history under the default clone checkout, never sees
    `fix.py` and comes back clean. That work was left by round 1 and no review has
    ever inspected it, so the run must stop as stalled rather than exit 0 -- which
    is exactly what a per-round record of the leftover forgot the moment round 2
    began.
    """
    repository = _repository(tmp_path)
    agent = _stalling_agent(tmp_path, then=_DECLINES)

    monkeypatch.setattr(agent_review_loop, "resolve_executable", lambda name, _label: name)
    monkeypatch.setattr(agent_review_loop, "claude_command", lambda _options: [sys.executable, str(agent)])
    monkeypatch.setattr(agent_review_loop, "perform_review", _requested_then_clean)
    # No --review-checkout: the default clone, where round 1's file is absent from
    # the reviewer's tree entirely.
    exit_code = agent_review_loop.main(
        ["--repo", str(repository), "--task", "x", "--max-rounds", "2", "--heartbeat", "0"]
        + ["--no-stall-retry", "--continue-on-no-commit"]
    )

    assert exit_code == agent_review_loop.EXIT_STALLED
    # Round 1's work is still exactly where it was left, and never became a commit.
    assert (repository / "fix.py").read_text(encoding="utf-8") == FIX
    assert _in(repository, "status", "--porcelain").strip() == "?? fix.py"
    assert "fix.py" not in _in(repository, "log", "--name-only", "--format=")
    # Round 1 is the stall; round 2 merely declined, so only the first is marked.
    records = _round_records(repository)
    assert (records[0]["stalled"], records[0]["retried"]) == (True, False)
    assert (records[1]["stalled"], records[1]["retried"]) == (False, False)
    captured = capsys.readouterr()
    assert "the review never saw" in f"{captured.out}{captured.err}"


def test_committing_the_stalled_work_a_round_later_lets_the_run_end_clean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The carried guard clears once the work is committed into a range a review reads.

    The mirror of the block: a stalled round's leftover stops a clean exit only
    while it is *still* uncommitted and unseen. Round 1 stalls with `fix.py` dirty
    and its review requests changes; round 2 commits `fix.py`, so it lands in that
    round's diff range and its clean verdict is a verdict on it. The run ends
    clean rather than clinging to a stall the tree no longer holds.
    """
    repository = _repository(tmp_path)
    agent = _stalling_agent(tmp_path, then=_COMMITS)

    monkeypatch.setattr(agent_review_loop, "resolve_executable", lambda name, _label: name)
    monkeypatch.setattr(agent_review_loop, "claude_command", lambda _options: [sys.executable, str(agent)])
    monkeypatch.setattr(agent_review_loop, "perform_review", _requested_then_clean)
    exit_code = agent_review_loop.main(
        ["--repo", str(repository), "--task", "x", "--max-rounds", "2", "--heartbeat", "0"]
        + ["--no-stall-retry", "--continue-on-no-commit"]
    )

    assert exit_code == agent_review_loop.EXIT_CLEAN
    # The work is committed and reviewed, and nothing is left dirty to block on.
    assert _in(repository, "show", "HEAD:fix.py") == FIX
    assert _in(repository, "status", "--porcelain").strip() == ""


_WRITES_FIX_WITHOUT_COMMITTING = (
    f"pathlib.Path('fix.py').write_text({FIX!r}, encoding='utf-8')\n"
    "sys.stdout.write('wrote the fix, then never came back to commit it\\n')\n"
)
_STAGES_STALLED_AND_COMMITS_ANOTHER = (
    "pathlib.Path('other.py').write_text('# unrelated round 2 work\\n', encoding='utf-8')\n"
    "subprocess.run(['git', 'add', 'other.py'], check=True)\n"
    "subprocess.run(['git', 'commit', '-q', '-m', 'fix: an unrelated round 2 commit'], check=True)\n"
    # Stage the stalled path after that commit: '?? fix.py' becomes 'A  fix.py',
    # the same path under a new porcelain status, still uncommitted and unreviewed.
    "subprocess.run(['git', 'add', 'fix.py'], check=True)\n"
    "sys.stdout.write('committed only other.py and staged the stalled fix.py\\n')\n"
)


def test_staging_the_stalled_file_a_round_later_does_not_slip_a_clean_run_past_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Remembering the stall by its whole porcelain line lost it the round its status changed.

    Round 1 stalls with `?? fix.py` dirty and its review requests changes, so the
    run goes on. Round 2 stages that same file -- `?? fix.py` becomes `A  fix.py`
    -- and commits only an unrelated `other.py`. A guard that carried the whole
    porcelain line no longer recognised the staged line as the work it was holding,
    dropped it, and let round 2's clean verdict (on a range that is only the
    `other.py` commit) end the run at exit 0 with `fix.py` staged and never
    reviewed. Tracked by name, the path is still outstanding and the run stops as
    stalled.
    """
    repository = _repository(tmp_path)
    agent = _agent(
        tmp_path, "stalls_then_stages", _WRITES_FIX_WITHOUT_COMMITTING, _STAGES_STALLED_AND_COMMITS_ANOTHER
    )

    monkeypatch.setattr(agent_review_loop, "resolve_executable", lambda name, _label: name)
    monkeypatch.setattr(agent_review_loop, "claude_command", lambda _options: [sys.executable, str(agent)])
    monkeypatch.setattr(agent_review_loop, "perform_review", _requested_then_clean)
    # No --review-checkout: the default clone, where the staged fix.py is absent
    # from the reviewer's tree entirely.
    exit_code = agent_review_loop.main(
        ["--repo", str(repository), "--task", "x", "--max-rounds", "2", "--heartbeat", "0"]
        + ["--no-stall-retry", "--continue-on-no-commit"]
    )

    assert exit_code == agent_review_loop.EXIT_STALLED
    assert _attempts(tmp_path, "stalls_then_stages") == 2
    # The unrelated commit landed; the stalled file is staged but never committed.
    assert _in(repository, "log", "-1", "--format=%s").strip() == "fix: an unrelated round 2 commit"
    assert _in(repository, "show", "--name-only", "--format=", "HEAD").split() == ["other.py"]
    assert "fix.py" not in _in(repository, "log", "--name-only", "--format=")
    assert _in(repository, "status", "--porcelain").strip() == "A  fix.py"
    assert (repository / "fix.py").read_text(encoding="utf-8") == FIX
    captured = capsys.readouterr()
    assert "the review never saw" in f"{captured.out}{captured.err}"


_MODIFIES_A_TRACKED_FILE_WITHOUT_COMMITTING = (
    "pathlib.Path('README.md').write_text('start\\nround 1 work nobody committed\\n', encoding='utf-8')\n"
    "sys.stdout.write('edited README.md and never came back to commit it\\n')\n"
)

_RENAMES_THE_STALLED_FILE_AND_COMMITS_ANOTHER = (
    "pathlib.Path('other.py').write_text('# unrelated round 2 work\\n', encoding='utf-8')\n"
    "subprocess.run(['git', 'add', 'other.py'], check=True)\n"
    "subprocess.run(['git', 'commit', '-q', '-m', 'fix: an unrelated round 2 commit'], check=True)\n"
    # The stalled path leaves under a new name after that commit: the work is
    # still there, still uncommitted, and the name round 1 knew it by is gone.
    "subprocess.run(['git', 'mv', 'README.md', 'renamed.md'], check=True)\n"
    "sys.stdout.write('committed only other.py and renamed the stalled file\\n')\n"
)


def test_renaming_the_stalled_file_a_round_later_does_not_slip_a_clean_run_past_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Tracking by name lost the work to the one change a name cannot survive.

    Round 1 stalls with `README.md` modified and its review requests changes, so
    the run goes on. Round 2 renames that file and commits only an unrelated
    `other.py`. Porcelain reports a rename as its destination and keeps the source
    in a field of its own, so the name the guard was holding vanished from the
    tree's paths, nothing added `renamed.md` back, and round 2's clean verdict on
    a range that is only the `other.py` commit ended the run at exit 0 with the
    work still uncommitted and never reviewed. Followed across the rename, the
    path is still outstanding and the run stops as stalled.
    """
    repository = _repository(tmp_path)
    agent = _agent(
        tmp_path,
        "stalls_then_renames",
        _MODIFIES_A_TRACKED_FILE_WITHOUT_COMMITTING,
        _RENAMES_THE_STALLED_FILE_AND_COMMITS_ANOTHER,
    )

    monkeypatch.setattr(agent_review_loop, "resolve_executable", lambda name, _label: name)
    monkeypatch.setattr(agent_review_loop, "claude_command", lambda _options: [sys.executable, str(agent)])
    monkeypatch.setattr(agent_review_loop, "perform_review", _requested_then_clean)
    exit_code = agent_review_loop.main(
        ["--repo", str(repository), "--task", "x", "--max-rounds", "2", "--heartbeat", "0"]
        + ["--no-stall-retry", "--continue-on-no-commit"]
    )

    assert exit_code == agent_review_loop.EXIT_STALLED
    assert _attempts(tmp_path, "stalls_then_renames") == 2
    assert _in(repository, "log", "-1", "--format=%s").strip() == "fix: an unrelated round 2 commit"
    assert _in(repository, "show", "--name-only", "--format=", "HEAD").split() == ["other.py"]
    # The renamed work is staged, uncommitted, and outside every reviewed range.
    assert "renamed.md" not in _in(repository, "log", "--name-only", "--format=")
    assert _in(repository, "status", "--porcelain").strip().startswith("R")
    assert "round 1 work nobody committed" in (repository / "renamed.md").read_text(encoding="utf-8")
    captured = capsys.readouterr()
    assert "the review never saw" in f"{captured.out}{captured.err}"


_WRITES_AN_UNTRACKED_FILE_WITHOUT_COMMITTING = (
    f"pathlib.Path('fix.py').write_text({FIX!r}, encoding='utf-8')\n"
    "sys.stdout.write('wrote an untracked fix.py, then never came back to commit it\\n')\n"
)
_RENAMES_THE_UNTRACKED_STALLED_FILE_AND_COMMITS_ANOTHER = (
    "pathlib.Path('other.py').write_text('# unrelated round 2 work\\n', encoding='utf-8')\n"
    "subprocess.run(['git', 'add', 'other.py'], check=True)\n"
    "subprocess.run(['git', 'commit', '-q', '-m', 'fix: an unrelated round 2 commit'], check=True)\n"
    # Move the untracked stalled path on the filesystem: git never tracked it, so
    # there is no rename record to carry the source across. The work is still
    # there, still uncommitted, under a name round 1 never saw.
    "pathlib.Path('fix.py').rename('renamed.py')\n"
    "sys.stdout.write('committed only other.py and moved the untracked stalled file\\n')\n"
)


def test_moving_an_untracked_stalled_file_a_round_later_does_not_slip_a_clean_run_past_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A rename git cannot see still must not smuggle the work past the guard.

    Round 1 stalls with an untracked `fix.py` and its review requests changes, so
    the run goes on. Round 2 commits only an unrelated `other.py`, then moves
    `fix.py` to `renamed.py` on the filesystem. An untracked file has no source
    identity in porcelain -- `?? renamed.py` is all the tree reports, with no
    rename record to follow -- so the name the guard was holding simply vanished
    from the dirty paths. The round produced a commit, so the declined-stall carry
    never ran and nothing added `renamed.py` back; round 2's clean verdict on a
    range that is only the `other.py` commit then ended the run at exit 0 with the
    work still uncommitted and never reviewed. An outstanding path that vanished
    with no commit to explain it keeps this round's new dirt outstanding, so the
    run stops as stalled instead.
    """
    repository = _repository(tmp_path)
    agent = _agent(
        tmp_path,
        "stalls_then_moves_untracked",
        _WRITES_AN_UNTRACKED_FILE_WITHOUT_COMMITTING,
        _RENAMES_THE_UNTRACKED_STALLED_FILE_AND_COMMITS_ANOTHER,
    )

    monkeypatch.setattr(agent_review_loop, "resolve_executable", lambda name, _label: name)
    monkeypatch.setattr(agent_review_loop, "claude_command", lambda _options: [sys.executable, str(agent)])
    monkeypatch.setattr(agent_review_loop, "perform_review", _requested_then_clean)
    exit_code = agent_review_loop.main(
        ["--repo", str(repository), "--task", "x", "--max-rounds", "2", "--heartbeat", "0"]
        + ["--no-stall-retry", "--continue-on-no-commit"]
    )

    assert exit_code == agent_review_loop.EXIT_STALLED
    assert _attempts(tmp_path, "stalls_then_moves_untracked") == 2
    assert _in(repository, "log", "-1", "--format=%s").strip() == "fix: an unrelated round 2 commit"
    assert _in(repository, "show", "--name-only", "--format=", "HEAD").split() == ["other.py"]
    # The moved work is untracked, uncommitted, and outside every reviewed range.
    assert "renamed.py" not in _in(repository, "log", "--name-only", "--format=")
    assert _in(repository, "status", "--porcelain").strip() == "?? renamed.py"
    assert (repository / "renamed.py").read_text(encoding="utf-8") == FIX
    captured = capsys.readouterr()
    assert "the review never saw" in f"{captured.out}{captured.err}"


_A_DIFFERENT_FIX = "# a different fix.py, unrelated to the stalled one\n"
_REUSES_THE_MOVED_NAME_FOR_A_DIFFERENT_COMMIT = (
    # Move the untracked stalled fix.py aside on the filesystem first -- git never
    # tracked it, so there is no rename record and the work lives on as
    # `?? renamed.py`, under a name round 1 never saw.
    "pathlib.Path('fix.py').rename('renamed.py')\n"
    # Then write a *different* fix.py and commit it. The old source name is reused
    # by an unrelated file, so `fix.py` reaches this round's commit diff while the
    # stalled content sits untracked under renamed.py, committed nowhere.
    f"pathlib.Path('fix.py').write_text({_A_DIFFERENT_FIX!r}, encoding='utf-8')\n"
    "subprocess.run(['git', 'add', 'fix.py'], check=True)\n"
    "subprocess.run(['git', 'commit', '-q', '-m', 'fix: a different fix.py entirely'], check=True)\n"
    "sys.stdout.write('reused the untracked name for a different committed file\\n')\n"
)


def test_reusing_the_moved_untracked_name_for_a_different_commit_does_not_slip_a_clean_run_past_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A commit under the old name is no proof the moved content was committed.

    Round 1 stalls with an untracked `fix.py` and its review requests changes, so
    the run goes on. Round 2 moves that file to `renamed.py` on the filesystem --
    untracked, so no rename record to follow -- then writes a *different* `fix.py`
    and commits it. The old source name now appears in the round's commit diff, so
    a guard that reads pathname membership there as "the outstanding object was
    committed" drops `fix.py`, never carries `renamed.py`, and lets round 2's clean
    verdict end the run at exit 0 with the stalled work still untracked and never
    reviewed. Pathname membership is no such proof: a followed name that vanished
    with no porcelain rename keeps this round's new dirt outstanding, so the run
    stops as stalled with the moved content still in the tree.
    """
    repository = _repository(tmp_path)
    agent = _agent(
        tmp_path,
        "stalls_then_reuses_the_name",
        _WRITES_AN_UNTRACKED_FILE_WITHOUT_COMMITTING,
        _REUSES_THE_MOVED_NAME_FOR_A_DIFFERENT_COMMIT,
    )

    monkeypatch.setattr(agent_review_loop, "resolve_executable", lambda name, _label: name)
    monkeypatch.setattr(agent_review_loop, "claude_command", lambda _options: [sys.executable, str(agent)])
    monkeypatch.setattr(agent_review_loop, "perform_review", _requested_then_clean)
    exit_code = agent_review_loop.main(
        ["--repo", str(repository), "--task", "x", "--max-rounds", "2", "--heartbeat", "0"]
        + ["--no-stall-retry", "--continue-on-no-commit"]
    )

    assert exit_code == agent_review_loop.EXIT_STALLED
    assert _attempts(tmp_path, "stalls_then_reuses_the_name") == 2
    assert _in(repository, "log", "-1", "--format=%s").strip() == "fix: a different fix.py entirely"
    # The committed fix.py is the unrelated round 2 file, not the stalled work.
    assert _in(repository, "show", "HEAD:fix.py") == _A_DIFFERENT_FIX
    # The stalled work is untracked, uncommitted, and outside every reviewed range.
    assert "renamed.py" not in _in(repository, "log", "--name-only", "--format=")
    assert _in(repository, "status", "--porcelain").strip() == "?? renamed.py"
    assert (repository / "renamed.py").read_text(encoding="utf-8") == FIX
    captured = capsys.readouterr()
    assert "the review never saw" in f"{captured.out}{captured.err}"


_OPERATOR_DIRT = "# the operator's own work in progress, uncommitted before the run started\n"
_MOVES_THE_STALLED_FILE_ONTO_INHERITED_DIRT = (
    # The stalled untracked file moves onto a pathname that was already dirty when
    # the round began, overwriting the operator's own work in progress there. The
    # destination's dirt predates the round, so subtracting what the round started
    # with removes exactly the name the work now lives under.
    "pathlib.Path('fix.py').replace('renamed.py')\n"
    # Then the name round 1 stalled under is reused for unrelated content and
    # committed, so it leaves the dirty set with nothing of the stall in it.
    f"pathlib.Path('fix.py').write_text({_A_DIFFERENT_FIX!r}, encoding='utf-8')\n"
    "subprocess.run(['git', 'add', 'fix.py'], check=True)\n"
    "subprocess.run(['git', 'commit', '-q', '-m', 'fix: a different fix.py entirely'], check=True)\n"
    "sys.stdout.write('moved the stall onto the inherited dirt and committed a different fix.py\\n')\n"
)


def test_moving_the_stalled_file_onto_inherited_dirt_does_not_slip_a_clean_run_past_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Under --allow-dirty the destination of a move can be dirt the run inherited.

    Round 1 stalls with an untracked `fix.py` and its review requests changes, so
    the run goes on. Round 2 moves that file onto `renamed.py`, which the operator
    had left dirty before the run started, then writes a different `fix.py` and
    commits it. Both halves of the guard then miss the work: the name round 1 knew
    it by left the dirty set, and the name it now lives under is subtracted out by
    `present_dirty - inherited_dirty`, because that path was already dirty when the
    round began. `outstanding_paths` ended empty and round 2's clean verdict on a
    range that is only the `fix.py` commit ended the run at exit 0 with round 1's
    work sitting untracked under a name no review ever read. A pathname is exactly
    what a move onto already-dirty ground destroys, so the destination is
    recognised by the content itself, and the run stops as stalled.
    """
    repository = _repository(tmp_path)
    # The operator's own uncommitted work, at the pathname round 2 will move onto.
    (repository / "renamed.py").write_text(_OPERATOR_DIRT, encoding="utf-8")
    agent = _agent(
        tmp_path,
        "stalls_then_moves_onto_dirt",
        _WRITES_AN_UNTRACKED_FILE_WITHOUT_COMMITTING,
        _MOVES_THE_STALLED_FILE_ONTO_INHERITED_DIRT,
    )

    monkeypatch.setattr(agent_review_loop, "resolve_executable", lambda name, _label: name)
    monkeypatch.setattr(agent_review_loop, "claude_command", lambda _options: [sys.executable, str(agent)])
    monkeypatch.setattr(agent_review_loop, "perform_review", _requested_then_clean)
    exit_code = agent_review_loop.main(
        ["--repo", str(repository), "--task", "x", "--max-rounds", "2", "--heartbeat", "0"]
        + ["--allow-dirty", "--no-stall-retry", "--continue-on-no-commit"]
    )

    assert exit_code == agent_review_loop.EXIT_STALLED
    assert _attempts(tmp_path, "stalls_then_moves_onto_dirt") == 2
    # The committed fix.py is the unrelated round 2 file, not the stalled work.
    assert _in(repository, "log", "-1", "--format=%s").strip() == "fix: a different fix.py entirely"
    assert _in(repository, "show", "HEAD:fix.py") == _A_DIFFERENT_FIX
    # The stalled work is untracked, uncommitted, and outside every reviewed range.
    assert "renamed.py" not in _in(repository, "log", "--name-only", "--format=")
    assert _in(repository, "status", "--porcelain").strip() == "?? renamed.py"
    assert (repository / "renamed.py").read_text(encoding="utf-8") == FIX
    captured = capsys.readouterr()
    assert "the review never saw" in f"{captured.out}{captured.err}"


_ROUND_TWO_EDIT = "# and a round 2 edit on top of it\n"
_COMMITS_THE_STALL_AND_REWRITES_THE_INHERITED_DIRT = (
    # The stalled work reaches a commit under its own name -- the review reading it.
    "subprocess.run(['git', 'add', 'fix.py'], check=True)\n"
    "subprocess.run(['git', 'commit', '-q', '-m', 'fix: finish what the stalled attempt wrote'], check=True)\n"
    # The round also rewrites an inherited dirty file. Whether that is the operator's
    # own continuation or the stall's edited work hidden in place, the tree is the
    # same, so it cannot be told from a stall and is kept as one.
    f"pathlib.Path('renamed.py').write_text({_OPERATOR_DIRT + _ROUND_TWO_EDIT!r}, encoding='utf-8')\n"
    "sys.stdout.write('committed the stalled fix and rewrote the inherited dirt\\n')\n"
)


def test_rewriting_inherited_dirt_beside_a_committed_stall_stops_as_stalled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A committed stall cannot clear an inherited file the round also rewrote.

    Committing the tracked stall under its own name is no proof that the stall's work
    reached review, because that same tree is what an attacker leaves: a hard link (or
    a plain in-place write) can put the stall's edited work into an inherited dirty
    file while a stale copy of its original bytes is what gets committed. Round 1
    stalls with an untracked `fix.py`; round 2 commits it -- so `fix.py` reaches a
    reviewed commit -- and rewrites `renamed.py`, which the operator left dirty before
    the run started. Nothing observable says whether `renamed.py` now holds the
    operator's own edit or the stall's hidden work, and calling it clean would exit 0
    over the latter, so the round it rewrote is kept and the run stops as stalled with
    the work still in the tree for the operator to commit or clear.
    """
    repository = _repository(tmp_path)
    (repository / "renamed.py").write_text(_OPERATOR_DIRT, encoding="utf-8")
    agent = _agent(
        tmp_path,
        "stalls_then_commits_beside_rewritten_dirt",
        _WRITES_AN_UNTRACKED_FILE_WITHOUT_COMMITTING,
        _COMMITS_THE_STALL_AND_REWRITES_THE_INHERITED_DIRT,
    )

    monkeypatch.setattr(agent_review_loop, "resolve_executable", lambda name, _label: name)
    monkeypatch.setattr(agent_review_loop, "claude_command", lambda _options: [sys.executable, str(agent)])
    monkeypatch.setattr(agent_review_loop, "perform_review", _requested_then_clean)
    exit_code = agent_review_loop.main(
        ["--repo", str(repository), "--task", "x", "--max-rounds", "2", "--heartbeat", "0"]
        + ["--allow-dirty", "--no-stall-retry", "--continue-on-no-commit"]
    )

    assert exit_code == agent_review_loop.EXIT_STALLED
    # The stall is committed, but the inherited file the round rewrote is retained and
    # left exactly where the round left it, uncommitted and outside every reviewed range.
    assert _in(repository, "show", "HEAD:fix.py") == FIX
    assert _in(repository, "status", "--porcelain").strip() == "?? renamed.py"
    assert (repository / "renamed.py").read_text(encoding="utf-8") == _OPERATOR_DIRT + _ROUND_TWO_EDIT
    captured = capsys.readouterr()
    assert "the review never saw" in f"{captured.out}{captured.err}"


_INHERITED_LF_DIRT = "def edited():\n    return 'the operator left this dirty, in LF'\n"
_COMMITS_THE_STALL_AND_GIVES_INHERITED_DIRT_CRLF_LINE_ENDINGS = (
    # The stall is committed under its own name, so a name vanishes and the inherited
    # dirt is examined. renamed.py is rewritten with CRLF line endings but the same
    # text; under core.autocrlf both the inherited LF and the new CRLF clean to the
    # one LF blob, so git names them alike and the round changed nothing there.
    "subprocess.run(['git', 'add', 'fix.py'], check=True)\n"
    "subprocess.run(['git', 'commit', '-q', '-m', 'fix: finish the stall'], check=True)\n"
    "pathlib.Path('renamed.py').write_bytes("
    f"{_INHERITED_LF_DIRT!r}.replace('\\n', '\\r\\n').encode())\n"
    "sys.stdout.write('committed the stall and gave the inherited dirt CRLF endings\\n')\n"
)


def test_a_crlf_only_rewrite_of_inherited_dirt_under_a_clean_filter_is_not_a_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Inherited dirt named the way git names it is unchanged by a line-ending rewrite.

    The retained inherited dirt is found by comparing the content the round left with
    the content it inherited, and both must be named the way `git add` would or a
    filter turns an untouched file into a false stall. `core.autocrlf=true` cleans CRLF
    to LF on the way into git, so a file whose only change is its line endings stores
    the very same blob. Round 1 stalls with an untracked `fix.py`; round 2 commits it
    -- vanishing a followed name, which puts the inherited dirt under examination --
    and rewrites `renamed.py` from LF to CRLF with the same text. A raw hash of the
    bytes on disk would read the CRLF file as changed and stop the clean run as
    stalled; naming it through git's clean filter sees the one LF blob unchanged, so
    the operator's file is left alone and the run ends clean.
    """
    repository = _repository(tmp_path)
    _in(repository, "config", "core.autocrlf", "true")
    # The operator's own uncommitted work, in LF, before the run starts.
    (repository / "renamed.py").write_text(_INHERITED_LF_DIRT, encoding="utf-8")
    agent = _agent(
        tmp_path,
        "stalls_then_reendlines_the_inherited_dirt",
        _WRITES_AN_UNTRACKED_FILE_WITHOUT_COMMITTING,
        _COMMITS_THE_STALL_AND_GIVES_INHERITED_DIRT_CRLF_LINE_ENDINGS,
    )

    monkeypatch.setattr(agent_review_loop, "resolve_executable", lambda name, _label: name)
    monkeypatch.setattr(agent_review_loop, "claude_command", lambda _options: [sys.executable, str(agent)])
    monkeypatch.setattr(agent_review_loop, "perform_review", _requested_then_clean)
    exit_code = agent_review_loop.main(
        ["--repo", str(repository), "--task", "x", "--max-rounds", "2", "--heartbeat", "0"]
        + ["--allow-dirty", "--no-stall-retry", "--continue-on-no-commit"]
    )

    assert exit_code == agent_review_loop.EXIT_CLEAN
    # The stall is committed and reviewed; the inherited file, changed only in its line
    # endings, is named as the unchanged blob it is and left exactly where it lay.
    assert _in(repository, "show", "HEAD:fix.py") == FIX
    assert _in(repository, "status", "--porcelain").strip() == "?? renamed.py"
    assert (repository / "renamed.py").read_bytes() == _INHERITED_LF_DIRT.replace("\n", "\r\n").encode()


_MOVES_THE_STALLED_FILE_ONTO_INHERITED_DIRT_AND_EDITS_IT = (
    # The move-onto-inherited-dirt case, but the round edits the file after it
    # lands. `replace` carries the stalled fix.py's inode onto renamed.py, and the
    # truncating write then changes its bytes in place while keeping that inode, so
    # the content the round began with is gone from the tree. Neither the pathname
    # subtraction nor the content match can name the work any longer; only the
    # inode the move preserved still does.
    "pathlib.Path('fix.py').replace('renamed.py')\n"
    f"pathlib.Path('renamed.py').write_text({FIX + _ROUND_TWO_EDIT!r}, encoding='utf-8')\n"
    # Then reuse the stalled name for unrelated content and commit it, so the name
    # round 1 knew leaves the dirty set with nothing of the stall in it.
    f"pathlib.Path('fix.py').write_text({_A_DIFFERENT_FIX!r}, encoding='utf-8')\n"
    "subprocess.run(['git', 'add', 'fix.py'], check=True)\n"
    "subprocess.run(['git', 'commit', '-q', '-m', 'fix: a different fix.py entirely'], check=True)\n"
    "sys.stdout.write('moved the stall onto the inherited dirt, edited it, and committed a different fix.py\\n')\n"
)


def test_editing_the_stalled_file_after_moving_it_onto_inherited_dirt_does_not_slip_a_clean_run_past_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A move onto inherited dirt plus an edit changes the bytes, not the inode.

    The sharper edge of the move-onto-inherited-dirt case: round 2 not only moves
    round 1's untracked `fix.py` onto the operator's dirty `renamed.py`, it also
    edits the file once it lands. Now all three of the cheaper identities miss it.
    The name round 1 knew left the dirty set; the name it lives under is subtracted
    out by `present_dirty - inherited_dirty`, because that path was inherited
    dirty; and the content the round captured is gone, so matching bytes cannot
    find it either. The one thing the move could not take away is the inode the
    filesystem carried onto the new name and the in-place edit reused, so the
    destination is recognised by that, `outstanding_paths` does not end empty, and
    round 2's clean verdict stops the run as stalled rather than exiting 0 over
    round 1's work sitting untracked under a name no review ever read.
    """
    repository = _repository(tmp_path)
    # The operator's own uncommitted work, at the pathname round 2 will move onto.
    (repository / "renamed.py").write_text(_OPERATOR_DIRT, encoding="utf-8")
    agent = _agent(
        tmp_path,
        "stalls_then_moves_onto_dirt_and_edits",
        _WRITES_AN_UNTRACKED_FILE_WITHOUT_COMMITTING,
        _MOVES_THE_STALLED_FILE_ONTO_INHERITED_DIRT_AND_EDITS_IT,
    )

    monkeypatch.setattr(agent_review_loop, "resolve_executable", lambda name, _label: name)
    monkeypatch.setattr(agent_review_loop, "claude_command", lambda _options: [sys.executable, str(agent)])
    monkeypatch.setattr(agent_review_loop, "perform_review", _requested_then_clean)
    exit_code = agent_review_loop.main(
        ["--repo", str(repository), "--task", "x", "--max-rounds", "2", "--heartbeat", "0"]
        + ["--allow-dirty", "--no-stall-retry", "--continue-on-no-commit"]
    )

    assert exit_code == agent_review_loop.EXIT_STALLED
    assert _attempts(tmp_path, "stalls_then_moves_onto_dirt_and_edits") == 2
    # The committed fix.py is the unrelated round 2 file, not the stalled work.
    assert _in(repository, "log", "-1", "--format=%s").strip() == "fix: a different fix.py entirely"
    assert _in(repository, "show", "HEAD:fix.py") == _A_DIFFERENT_FIX
    # The stalled work, plus the round-2 edit, is untracked and outside every range.
    assert "renamed.py" not in _in(repository, "log", "--name-only", "--format=")
    assert _in(repository, "status", "--porcelain").strip() == "?? renamed.py"
    assert (repository / "renamed.py").read_text(encoding="utf-8") == FIX + _ROUND_TWO_EDIT
    captured = capsys.readouterr()
    assert "the review never saw" in f"{captured.out}{captured.err}"


_ATOMICALLY_REPLACES_INHERITED_DIRT_WITH_THE_STALLED_CONTENT = (
    # The move onto inherited dirt is itself an atomic save: the stalled content
    # is written to a temp file and renamed over the destination, so renamed.py
    # ends with a fresh inode -- not the stalled fix.py's -- and the stalled bytes.
    f"pathlib.Path('atomic.tmp').write_text({FIX!r}, encoding='utf-8')\n"
    "pathlib.Path('atomic.tmp').replace('renamed.py')\n"
    # The stalled name is then removed and reused for an unrelated commit, so it
    # leaves the dirty set with nothing of the stall left under it.
    "pathlib.Path('fix.py').unlink()\n"
    f"pathlib.Path('fix.py').write_text({_A_DIFFERENT_FIX!r}, encoding='utf-8')\n"
    "subprocess.run(['git', 'add', 'fix.py'], check=True)\n"
    "subprocess.run(['git', 'commit', '-q', '-m', 'fix: a different fix.py entirely'], check=True)\n"
    "sys.stdout.write('atomically replaced the inherited dirt with the stalled content, committed a different fix.py\\n')\n"
)


def test_an_atomic_save_that_moves_the_stall_onto_inherited_dirt_does_not_slip_a_clean_run_past_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Atomic replacement is the move: a fresh inode on the destination, the stalled bytes intact.

    The move onto inherited dirt performed the way editors and tools write files:
    round 2 writes round 1's stalled content to a temp file and renames it over the
    operator's dirty `renamed.py`, so the destination takes the temp file's inode
    rather than the stalled fix.py's. The inode `relocated_stall` would match on is
    gone, but the atomic save carried the stalled bytes across unchanged, so the
    content half still names the work under its new pathname. The run keeps it
    outstanding and stops as stalled rather than exiting 0 over round 1's work
    sitting untracked under a name no review ever read.
    """
    repository = _repository(tmp_path)
    # The operator's own uncommitted work, at the pathname round 2 will land on.
    (repository / "renamed.py").write_text(_OPERATOR_DIRT, encoding="utf-8")
    agent = _agent(
        tmp_path,
        "stalls_then_atomically_replaces_the_dirt",
        _WRITES_AN_UNTRACKED_FILE_WITHOUT_COMMITTING,
        _ATOMICALLY_REPLACES_INHERITED_DIRT_WITH_THE_STALLED_CONTENT,
    )

    monkeypatch.setattr(agent_review_loop, "resolve_executable", lambda name, _label: name)
    monkeypatch.setattr(agent_review_loop, "claude_command", lambda _options: [sys.executable, str(agent)])
    monkeypatch.setattr(agent_review_loop, "perform_review", _requested_then_clean)
    exit_code = agent_review_loop.main(
        ["--repo", str(repository), "--task", "x", "--max-rounds", "2", "--heartbeat", "0"]
        + ["--allow-dirty", "--no-stall-retry", "--continue-on-no-commit"]
    )

    assert exit_code == agent_review_loop.EXIT_STALLED
    assert _attempts(tmp_path, "stalls_then_atomically_replaces_the_dirt") == 2
    # The committed fix.py is the unrelated round 2 file, not the stalled work.
    assert _in(repository, "log", "-1", "--format=%s").strip() == "fix: a different fix.py entirely"
    assert _in(repository, "show", "HEAD:fix.py") == _A_DIFFERENT_FIX
    # The stalled content is untracked under its new name, outside every range.
    assert "renamed.py" not in _in(repository, "log", "--name-only", "--format=")
    assert _in(repository, "status", "--porcelain").strip() == "?? renamed.py"
    assert (repository / "renamed.py").read_text(encoding="utf-8") == FIX
    captured = capsys.readouterr()
    assert "the review never saw" in f"{captured.out}{captured.err}"


_MOVES_THE_STALLED_FILE_ONTO_INHERITED_DIRT_THEN_SAVES_OVER_IT_ATOMICALLY = (
    # A move onto inherited dirt, then an atomic save on top of it. `replace`
    # carries the stalled fix.py's inode onto renamed.py, and the atomic save then
    # throws that inode away: the edited bytes go to a temp file that is renamed
    # over renamed.py, so the destination takes the temp file's inode and the temp
    # file's bytes. Neither the inode the move preserved nor the content the round
    # began with survives on it -- the sharper edge the in-place edit above could
    # not reach, and how an ordinary editor or formatter writes a file.
    "pathlib.Path('fix.py').replace('renamed.py')\n"
    f"pathlib.Path('atomic.tmp').write_text({FIX + _ROUND_TWO_EDIT!r}, encoding='utf-8')\n"
    "pathlib.Path('atomic.tmp').replace('renamed.py')\n"
    # Then reuse the stalled name for unrelated content and commit it.
    f"pathlib.Path('fix.py').write_text({_A_DIFFERENT_FIX!r}, encoding='utf-8')\n"
    "subprocess.run(['git', 'add', 'fix.py'], check=True)\n"
    "subprocess.run(['git', 'commit', '-q', '-m', 'fix: a different fix.py entirely'], check=True)\n"
    "sys.stdout.write('atomically saved the moved stall over the inherited dirt, committed a different fix.py\\n')\n"
)


def test_an_atomic_save_over_inherited_dirt_after_a_move_does_not_slip_a_clean_run_past_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The move an identity cannot follow: an atomic save replaces the inode and the bytes both.

    The move-onto-inherited-dirt case pushed past where identity can reach. Round 2
    moves round 1's untracked `fix.py` onto the operator's dirty `renamed.py`, then
    saves the edited file the way editors do -- write a temp file, rename it over
    the target -- which hands `renamed.py` the temp file's inode and the temp
    file's bytes. The inode the move carried is gone, the content the round began
    with is gone, and the stalled name has been reused for an unrelated commit, so
    neither `relocated_stall` identity names the work. What is left to go on is
    that the stalled content reached no commit the review read while an
    inherited-dirty path was rewritten; the run keeps that path outstanding and
    stops as stalled rather than exiting 0 over round 1's work sitting untracked
    under a name no review ever read.
    """
    repository = _repository(tmp_path)
    # The operator's own uncommitted work, at the pathname round 2 will move onto.
    (repository / "renamed.py").write_text(_OPERATOR_DIRT, encoding="utf-8")
    agent = _agent(
        tmp_path,
        "stalls_then_atomic_saves_over_the_dirt",
        _WRITES_AN_UNTRACKED_FILE_WITHOUT_COMMITTING,
        _MOVES_THE_STALLED_FILE_ONTO_INHERITED_DIRT_THEN_SAVES_OVER_IT_ATOMICALLY,
    )

    monkeypatch.setattr(agent_review_loop, "resolve_executable", lambda name, _label: name)
    monkeypatch.setattr(agent_review_loop, "claude_command", lambda _options: [sys.executable, str(agent)])
    monkeypatch.setattr(agent_review_loop, "perform_review", _requested_then_clean)
    exit_code = agent_review_loop.main(
        ["--repo", str(repository), "--task", "x", "--max-rounds", "2", "--heartbeat", "0"]
        + ["--allow-dirty", "--no-stall-retry", "--continue-on-no-commit"]
    )

    assert exit_code == agent_review_loop.EXIT_STALLED
    assert _attempts(tmp_path, "stalls_then_atomic_saves_over_the_dirt") == 2
    # The committed fix.py is the unrelated round 2 file, not the stalled work.
    assert _in(repository, "log", "-1", "--format=%s").strip() == "fix: a different fix.py entirely"
    assert _in(repository, "show", "HEAD:fix.py") == _A_DIFFERENT_FIX
    # The stalled work, plus the round-2 edit, is untracked and outside every range.
    assert "renamed.py" not in _in(repository, "log", "--name-only", "--format=")
    assert _in(repository, "status", "--porcelain").strip() == "?? renamed.py"
    assert (repository / "renamed.py").read_text(encoding="utf-8") == FIX + _ROUND_TWO_EDIT
    captured = capsys.readouterr()
    assert "the review never saw" in f"{captured.out}{captured.err}"


# A newline is a byte a POSIX filename may hold; the destination is named with one so
# the transport that hashes it is exercised on a name a newline-delimited channel splits.
_NEWLINE_DEST = "rename\nd.py"
_MOVES_THE_STALL_ONTO_A_NEWLINE_NAMED_INHERITED_DIRT_ATOMICALLY = (
    f"dst = {_NEWLINE_DEST!r}\n"
    # Move the stall onto the operator's dirty destination whose name holds a newline,
    # then atomic-save the edited bytes over it: the edit goes to a temp file renamed
    # over dst, so dst takes a fresh inode and fresh bytes and neither identity
    # relocated_stall matches on follows the work there. Only the conservative backstop,
    # which asks whether an inherited-dirty path's content was rewritten, is left.
    "pathlib.Path('fix.py').replace(dst)\n"
    f"pathlib.Path('atomic.tmp').write_text({FIX + _ROUND_TWO_EDIT!r}, encoding='utf-8')\n"
    "pathlib.Path('atomic.tmp').replace(dst)\n"
    # Only an unrelated file is committed.
    "pathlib.Path('other.py').write_text('# unrelated round 2 work\\n', encoding='utf-8')\n"
    "subprocess.run(['git', 'add', 'other.py'], check=True)\n"
    "subprocess.run(['git', 'commit', '-q', '-m', 'fix: an unrelated round 2 commit'], check=True)\n"
    "sys.stdout.write('atomic-saved the edited stall onto the newline-named inherited dirt, committed other.py\\n')\n"
)


@pytest.mark.skipif(sys.platform == "win32", reason="a newline in a filename is a POSIX-only name")
def test_an_atomic_save_over_a_newline_named_inherited_dirt_does_not_slip_a_clean_run_past_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A newline in the destination name must not make its rewrite compare as unchanged.

    The atomic-save-over-inherited-dirt case, but the operator's dirty destination is
    named with a newline in it -- a valid POSIX filename. Round 2 moves round 1's
    untracked `fix.py` onto it and atomic-saves edited bytes over it, so only the
    conservative backstop -- which asks whether an inherited-dirty path's content was
    rewritten -- is left to catch it. That backstop hashes the destination through
    `git hash-object`; a newline-delimited transport would split the name in two and
    hash nothing under it, so the path compared as unchanged against itself -- absent on
    both sides -- and the run exited 0 over the edited stall no review read. Passed
    positionally, the name is hashed whole, its content differs from the operator's own,
    and the run keeps it outstanding and stops as stalled.
    """
    repository = _repository(tmp_path)
    # The operator's own uncommitted work, at the newline-named path round 2 moves onto.
    (repository / _NEWLINE_DEST).write_text(_OPERATOR_DIRT, encoding="utf-8")
    agent = _agent(
        tmp_path,
        "stalls_then_atomic_saves_over_a_newline_name",
        _WRITES_AN_UNTRACKED_FILE_WITHOUT_COMMITTING,
        _MOVES_THE_STALL_ONTO_A_NEWLINE_NAMED_INHERITED_DIRT_ATOMICALLY,
    )

    monkeypatch.setattr(agent_review_loop, "resolve_executable", lambda name, _label: name)
    monkeypatch.setattr(agent_review_loop, "claude_command", lambda _options: [sys.executable, str(agent)])
    monkeypatch.setattr(agent_review_loop, "perform_review", _requested_then_clean)
    exit_code = agent_review_loop.main(
        ["--repo", str(repository), "--task", "x", "--max-rounds", "2", "--heartbeat", "0"]
        + ["--allow-dirty", "--no-stall-retry", "--continue-on-no-commit"]
    )

    assert exit_code == agent_review_loop.EXIT_STALLED
    assert _attempts(tmp_path, "stalls_then_atomic_saves_over_a_newline_name") == 2
    # The committed file is the unrelated round 2 one, not the stalled work.
    assert _in(repository, "log", "-1", "--format=%s").strip() == "fix: an unrelated round 2 commit"
    assert _in(repository, "show", "--name-only", "--format=", "HEAD").split() == ["other.py"]
    # The edited stall sits under the newline-named path, uncommitted and unreviewed.
    assert (repository / _NEWLINE_DEST).read_text(encoding="utf-8") == FIX + _ROUND_TWO_EDIT
    captured = capsys.readouterr()
    assert "the review never saw" in f"{captured.out}{captured.err}"


_STALLED_SYMLINK_TARGET = "the-stalled-symlink-points-here"
_OPERATOR_SYMLINK_TARGET = "where-the-operator-left-their-own-link"
_LEAVES_AN_UNTRACKED_BROKEN_SYMLINK_WITHOUT_COMMITTING = (
    # A broken symlink is a work-tree object git names by the hash of its link text,
    # not the (missing) target: `is_file`/`stat` follow it and see nothing, so it
    # once carried neither a content nor a filesystem identity to be followed by.
    f"pathlib.Path('fix.py').symlink_to({_STALLED_SYMLINK_TARGET!r})\n"
    "sys.stdout.write('left an untracked broken symlink fix.py, never came back to commit it\\n')\n"
)
_MOVES_THE_STALLED_SYMLINK_ONTO_INHERITED_DIRT = (
    # Move the untracked broken symlink onto the operator's dirty broken symlink. A
    # rename does not follow either link, so renamed.py becomes the stalled symlink
    # itself, carrying its inode; git tracked neither, so no rename record follows it,
    # and the destination was inherited dirty, so `present_dirty - inherited_dirty`
    # subtracts it out. Only the link's own inode and link text still name the work.
    "pathlib.Path('fix.py').replace('renamed.py')\n"
    # Commit only unrelated work.
    "pathlib.Path('other.py').write_text('# unrelated round 2 work\\n', encoding='utf-8')\n"
    "subprocess.run(['git', 'add', 'other.py'], check=True)\n"
    "subprocess.run(['git', 'commit', '-q', '-m', 'fix: an unrelated round 2 commit'], check=True)\n"
    "sys.stdout.write('moved the stalled broken symlink onto the inherited dirt, committed other.py\\n')\n"
)


def test_moving_a_broken_symlink_onto_an_inherited_dirty_broken_symlink_does_not_slip_a_clean_run_past_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A moved broken symlink is a stall like any other, and once had no identity to follow.

    Round 1 stalls with an untracked broken symlink `fix.py`; its review requests
    changes, so the run goes on. Round 2 moves `fix.py` onto the operator's dirty broken
    symlink `renamed.py` and commits only an unrelated `other.py`. A broken symlink was
    once invisible to both identities: `git hash-object` on the path opens nothing and
    fails, and `stat` follows the link to a target that is not there and reports no
    inode, so neither the content nor the filesystem map named the stall, the destination
    was subtracted out as inherited dirt, and the run exited 0 over work no review read.
    Named by its link text and its own `lstat` inode, the moved symlink is followed onto
    `renamed.py`, so the run keeps it outstanding and stops as stalled.
    """
    repository = _repository(tmp_path)
    # The operator's own uncommitted broken symlink, at the path round 2 moves onto.
    try:
        (repository / "renamed.py").symlink_to(_OPERATOR_SYMLINK_TARGET)
    except (OSError, NotImplementedError):
        pytest.skip("this machine does not allow creating a symlink")
    agent = _agent(
        tmp_path,
        "stalls_then_moves_a_broken_symlink",
        _LEAVES_AN_UNTRACKED_BROKEN_SYMLINK_WITHOUT_COMMITTING,
        _MOVES_THE_STALLED_SYMLINK_ONTO_INHERITED_DIRT,
    )

    monkeypatch.setattr(agent_review_loop, "resolve_executable", lambda name, _label: name)
    monkeypatch.setattr(agent_review_loop, "claude_command", lambda _options: [sys.executable, str(agent)])
    monkeypatch.setattr(agent_review_loop, "perform_review", _requested_then_clean)
    exit_code = agent_review_loop.main(
        ["--repo", str(repository), "--task", "x", "--max-rounds", "2", "--heartbeat", "0"]
        + ["--allow-dirty", "--no-stall-retry", "--continue-on-no-commit"]
    )

    assert exit_code == agent_review_loop.EXIT_STALLED
    assert _attempts(tmp_path, "stalls_then_moves_a_broken_symlink") == 2
    # The committed file is the unrelated round 2 one, not the stalled work.
    assert _in(repository, "log", "-1", "--format=%s").strip() == "fix: an unrelated round 2 commit"
    assert _in(repository, "show", "--name-only", "--format=", "HEAD").split() == ["other.py"]
    # The stalled symlink sits under its new name, uncommitted and never reviewed.
    assert _in(repository, "status", "--porcelain").strip() == "?? renamed.py"
    assert (repository / "renamed.py").is_symlink()
    assert os.readlink(repository / "renamed.py") == _STALLED_SYMLINK_TARGET
    captured = capsys.readouterr()
    assert "the review never saw" in f"{captured.out}{captured.err}"


_MOVES_THE_STALL_BENEATH_AN_INHERITED_UNTRACKED_DIRECTORY = (
    "pathlib.Path('other.py').write_text('# unrelated round 2 work\\n', encoding='utf-8')\n"
    "subprocess.run(['git', 'add', 'other.py'], check=True)\n"
    "subprocess.run(['git', 'commit', '-q', '-m', 'fix: an unrelated round 2 commit'], check=True)\n"
    # Move the untracked stall beneath a directory the run inherited untracked. The
    # default status mode reports that whole directory as one `?? operator-work/`
    # record and never descends, so the moved file's own name never appears among the
    # dirty paths; only listing every untracked file gives it a name to carry.
    "pathlib.Path('fix.py').rename('operator-work/fix.py')\n"
    "sys.stdout.write('committed only other.py and moved the stall beneath the inherited untracked dir\\n')\n"
)


def test_moving_a_stall_beneath_an_inherited_untracked_directory_does_not_slip_a_clean_run_past_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A stall hidden inside a collapsed untracked directory still must not exit clean.

    Round 1 stalls with an untracked `fix.py`; its review requests changes, so the run
    goes on. Round 2 commits only an unrelated `other.py`, then moves `fix.py` beneath
    `operator-work/`, a directory the run inherited untracked. Under the default
    untracked mode git reports that directory as one `?? operator-work/` record and
    never descends, so the moved file has no name of its own in either the inherited or
    the present dirty paths: the directory record collides with itself and is subtracted
    out, holds no blob or inode the stall matches, and the run exited 0 over work no
    review read. Listing every untracked file gives `operator-work/fix.py` a name the
    round's new dirt carries, so the run keeps it outstanding and stops as stalled.
    """
    repository = _repository(tmp_path)
    # The operator's own untracked directory, inherited before the run starts.
    (repository / "operator-work").mkdir()
    (repository / "operator-work" / "wip.txt").write_text(_OPERATOR_DIRT, encoding="utf-8")
    agent = _agent(
        tmp_path,
        "stalls_then_hides_under_an_untracked_dir",
        _WRITES_AN_UNTRACKED_FILE_WITHOUT_COMMITTING,
        _MOVES_THE_STALL_BENEATH_AN_INHERITED_UNTRACKED_DIRECTORY,
    )

    monkeypatch.setattr(agent_review_loop, "resolve_executable", lambda name, _label: name)
    monkeypatch.setattr(agent_review_loop, "claude_command", lambda _options: [sys.executable, str(agent)])
    monkeypatch.setattr(agent_review_loop, "perform_review", _requested_then_clean)
    exit_code = agent_review_loop.main(
        ["--repo", str(repository), "--task", "x", "--max-rounds", "2", "--heartbeat", "0"]
        + ["--allow-dirty", "--no-stall-retry", "--continue-on-no-commit"]
    )

    assert exit_code == agent_review_loop.EXIT_STALLED
    assert _attempts(tmp_path, "stalls_then_hides_under_an_untracked_dir") == 2
    assert _in(repository, "log", "-1", "--format=%s").strip() == "fix: an unrelated round 2 commit"
    assert _in(repository, "show", "--name-only", "--format=", "HEAD").split() == ["other.py"]
    # The moved work is untracked, uncommitted, and outside every reviewed range.
    assert "operator-work/fix.py" not in _in(repository, "log", "--name-only", "--format=")
    assert (repository / "operator-work" / "fix.py").read_text(encoding="utf-8") == FIX
    captured = capsys.readouterr()
    assert "the review never saw" in f"{captured.out}{captured.err}"


_STALLED_A = "def a():\n    return 'stalled a, written and never committed'\n"
_STALLED_B = "def b():\n    return 'stalled b, written and never committed'\n"
_WRITES_TWO_UNTRACKED_FILES_WITHOUT_COMMITTING = (
    f"pathlib.Path('a.py').write_text({_STALLED_A!r}, encoding='utf-8')\n"
    f"pathlib.Path('b.py').write_text({_STALLED_B!r}, encoding='utf-8')\n"
    "sys.stdout.write('wrote untracked a.py and b.py, then never came back to commit them\\n')\n"
)
_COMMITS_ONE_STALL_AND_ATOMIC_SAVES_THE_OTHER_ONTO_INHERITED_DIRT = (
    # a.py reaches a commit under its own name -- the review reads it, so its bytes
    # and its inode both land among the round's commits and it is the reviewed half.
    "subprocess.run(['git', 'add', 'a.py'], check=True)\n"
    "subprocess.run(['git', 'commit', '-q', '-m', 'fix: commit a.py, the half a review reads'], check=True)\n"
    # b.py moves onto the operator's dirty renamed_b.py, carrying its inode there
    # while it is still alive, and an atomic save then throws that inode and those
    # bytes away: the edited work goes to a temp file renamed over renamed_b.py.
    "pathlib.Path('b.py').replace('renamed_b.py')\n"
    # The old b.py name is reused for an unrelated commit while b.py's original inode
    # is still alive at renamed_b.py, so the new b.py cannot be handed that inode --
    # what makes the regression deterministic rather than a bet on the allocator.
    f"pathlib.Path('b.py').write_text({_A_DIFFERENT_FIX!r}, encoding='utf-8')\n"
    "subprocess.run(['git', 'add', 'b.py'], check=True)\n"
    "subprocess.run(['git', 'commit', '-q', '-m', 'fix: an unrelated new b.py'], check=True)\n"
    f"pathlib.Path('atomic.tmp').write_text({_STALLED_B + _ROUND_TWO_EDIT!r}, encoding='utf-8')\n"
    "pathlib.Path('atomic.tmp').replace('renamed_b.py')\n"
    "sys.stdout.write('committed a.py, reused b.py for an unrelated commit, atomic-saved b onto the dirt\\n')\n"
)


def test_committing_one_of_two_stalls_does_not_clear_the_other_moved_onto_inherited_dirt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """One reviewed stall must not vouch for a second the same round atomic-saved away.

    Two outstanding files, and the round accounts for each on its own or not at all.
    Round 1 stalls with untracked `a.py` and `b.py`; round 2 commits `a.py` under its
    own name -- reviewed and gone -- while moving `b.py` onto the operator's dirty
    `renamed_b.py` and atomic-saving edited bytes over it, then reuses the `b.py` name
    for an unrelated commit. A guard that suppressed the destination retention the
    moment *any* stalled blob appeared among the round's commits read `a.py`'s bytes
    as clearing the whole round, dropped `renamed_b.py`, and exited 0 with `b.py`'s
    work sitting untracked under a name no review read. Asking, per object, whether
    its own inode and content both reached the round's commits keeps `b.py`
    outstanding -- its inode is gone and its bytes were never committed -- while
    `a.py`, whose inode and bytes are both in the commit, stops nothing, so the run
    stalls over the one stall that was not reviewed.
    """
    repository = _repository(tmp_path)
    # The operator's own uncommitted work, at the pathname round 2 will move b onto.
    (repository / "renamed_b.py").write_text(_OPERATOR_DIRT, encoding="utf-8")
    agent = _agent(
        tmp_path,
        "stalls_two_then_commits_one",
        _WRITES_TWO_UNTRACKED_FILES_WITHOUT_COMMITTING,
        _COMMITS_ONE_STALL_AND_ATOMIC_SAVES_THE_OTHER_ONTO_INHERITED_DIRT,
    )

    monkeypatch.setattr(agent_review_loop, "resolve_executable", lambda name, _label: name)
    monkeypatch.setattr(agent_review_loop, "claude_command", lambda _options: [sys.executable, str(agent)])
    monkeypatch.setattr(agent_review_loop, "perform_review", _requested_then_clean)
    exit_code = agent_review_loop.main(
        ["--repo", str(repository), "--task", "x", "--max-rounds", "2", "--heartbeat", "0"]
        + ["--allow-dirty", "--no-stall-retry", "--continue-on-no-commit"]
    )

    assert exit_code == agent_review_loop.EXIT_STALLED
    assert _attempts(tmp_path, "stalls_two_then_commits_one") == 2
    # a.py is committed and reviewed; the committed b.py is the unrelated round 2 one.
    assert _in(repository, "show", "HEAD~1:a.py") == _STALLED_A
    assert _in(repository, "show", "HEAD:b.py") == _A_DIFFERENT_FIX
    # b.py's stalled work, plus the round-2 edit, is untracked and outside every range.
    assert "renamed_b.py" not in _in(repository, "log", "--name-only", "--format=")
    assert _in(repository, "status", "--porcelain").strip() == "?? renamed_b.py"
    assert (repository / "renamed_b.py").read_text(encoding="utf-8") == _STALLED_B + _ROUND_TWO_EDIT
    captured = capsys.readouterr()
    assert "the review never saw" in f"{captured.out}{captured.err}"


_COMMITS_A_COPY_OF_THE_STALLED_BYTES_AND_ATOMIC_SAVES_THE_STALL_ONTO_INHERITED_DIRT = (
    # A copy of the stalled bytes reaches a commit at another path while the stall's
    # own inode is still alive under fix.py, so the copy is a different object -- the
    # bytes are committed, the object is not.
    f"pathlib.Path('copy.py').write_text({FIX!r}, encoding='utf-8')\n"
    "subprocess.run(['git', 'add', 'copy.py'], check=True)\n"
    "subprocess.run(['git', 'commit', '-q', '-m', 'fix: a copy of the stalled bytes at another path'], check=True)\n"
    # The stall then moves onto the operator's dirty renamed.py and an atomic save
    # replaces both its identities: the edited work goes to a temp file renamed over
    # renamed.py, so neither the stalled inode nor the stalled bytes name it there.
    "pathlib.Path('fix.py').replace('renamed.py')\n"
    f"pathlib.Path('atomic.tmp').write_text({FIX + _ROUND_TWO_EDIT!r}, encoding='utf-8')\n"
    "pathlib.Path('atomic.tmp').replace('renamed.py')\n"
    "sys.stdout.write('committed a copy of the stalled bytes, atomic-saved the stall onto the dirt\\n')\n"
)


def test_committing_a_copy_of_the_stalled_bytes_does_not_clear_the_stall_atomic_saved_away(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The stalled bytes reaching a commit is not the stalled object reaching one.

    Round 1 stalls with untracked `fix.py`; round 2 commits a *copy* of its bytes at
    `copy.py`, then moves `fix.py` onto the operator's dirty `renamed.py` and
    atomic-saves edited bytes over it. The copy puts the stalled content among the
    round's commits without the object itself ever being committed, so a guard that
    read stalled content in the range as the stall reviewed dropped `renamed.py` and
    exited 0 over the work no review saw. The object's identity, not its bytes, is
    the proof: `copy.py` is a different inode from the stalled `fix.py`, so the stall
    clears the inode half of the check nowhere in the commit and stays outstanding,
    and the run stops as stalled with the moved-and-edited work still in the tree.
    """
    repository = _repository(tmp_path)
    # The operator's own uncommitted work, at the pathname round 2 will move onto.
    (repository / "renamed.py").write_text(_OPERATOR_DIRT, encoding="utf-8")
    agent = _agent(
        tmp_path,
        "stalls_then_commits_a_copy",
        _WRITES_AN_UNTRACKED_FILE_WITHOUT_COMMITTING,
        _COMMITS_A_COPY_OF_THE_STALLED_BYTES_AND_ATOMIC_SAVES_THE_STALL_ONTO_INHERITED_DIRT,
    )

    monkeypatch.setattr(agent_review_loop, "resolve_executable", lambda name, _label: name)
    monkeypatch.setattr(agent_review_loop, "claude_command", lambda _options: [sys.executable, str(agent)])
    monkeypatch.setattr(agent_review_loop, "perform_review", _requested_then_clean)
    exit_code = agent_review_loop.main(
        ["--repo", str(repository), "--task", "x", "--max-rounds", "2", "--heartbeat", "0"]
        + ["--allow-dirty", "--no-stall-retry", "--continue-on-no-commit"]
    )

    assert exit_code == agent_review_loop.EXIT_STALLED
    assert _attempts(tmp_path, "stalls_then_commits_a_copy") == 2
    # The committed copy carries the stalled bytes; the stalled object was not committed.
    assert _in(repository, "show", "HEAD:copy.py") == FIX
    assert "fix.py" not in _in(repository, "status", "--porcelain")
    # The stalled work, plus the round-2 edit, is untracked and outside every range.
    assert "renamed.py" not in _in(repository, "log", "--name-only", "--format=")
    assert _in(repository, "status", "--porcelain").strip() == "?? renamed.py"
    assert (repository / "renamed.py").read_text(encoding="utf-8") == FIX + _ROUND_TWO_EDIT
    captured = capsys.readouterr()
    assert "the review never saw" in f"{captured.out}{captured.err}"


_WRITES_AN_EMPTY_UNTRACKED_FILE_WITHOUT_COMMITTING = (
    "pathlib.Path('empty.py').write_bytes(b'')\n"
    "sys.stdout.write('wrote an untracked empty.py, then never came back to fill or commit it\\n')\n"
)
_MOVES_THE_EMPTY_STALL_ONTO_INHERITED_DIRT_THEN_ATOMIC_SAVES_REAL_WORK = (
    # The empty stall moves onto the operator's dirty renamed.py, carrying its inode
    # there while it is still alive. An empty file has no content identity, only this
    # one, so the inode is the whole of what follows it.
    "pathlib.Path('empty.py').replace('renamed.py')\n"
    # An unrelated file is committed while the stall's inode is still alive at
    # renamed.py, so the commit's own object cannot be handed that inode.
    "pathlib.Path('other.py').write_text('# unrelated round 2 work\\n', encoding='utf-8')\n"
    "subprocess.run(['git', 'add', 'other.py'], check=True)\n"
    "subprocess.run(['git', 'commit', '-q', '-m', 'fix: an unrelated round 2 commit'], check=True)\n"
    # An atomic save then writes real work over renamed.py, freeing the empty stall's
    # inode: the edited bytes go to a temp file renamed over the destination.
    f"pathlib.Path('atomic.tmp').write_text({FIX!r}, encoding='utf-8')\n"
    "pathlib.Path('atomic.tmp').replace('renamed.py')\n"
    "sys.stdout.write('moved the empty stall onto the dirt, committed other.py, atomic-saved real work over it\\n')\n"
)


def test_a_zero_byte_stall_atomic_saved_onto_inherited_dirt_does_not_slip_a_clean_run_past_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A contentless stall still has an inode, and the fallback must be asked for it.

    Round 1 stalls with an untracked *empty* `empty.py`, which carries no content
    identity for a byte-only check to match. Round 2 moves it onto the operator's
    dirty `renamed.py`, commits an unrelated file, then atomic-saves real work over
    `renamed.py`. A fallback gated on the stalled bytes existing at all never ran for
    the empty stall, so the round-2 work landed on inherited dirt and the run exited
    0 over it. The empty file's inode is the identity it does have, and asking the
    fallback by inode -- contentless objects proven by inode alone -- keeps
    `renamed.py` outstanding, so the run stops as stalled with the work still in the
    tree.
    """
    repository = _repository(tmp_path)
    # The operator's own uncommitted work, at the pathname round 2 will move onto.
    (repository / "renamed.py").write_text(_OPERATOR_DIRT, encoding="utf-8")
    agent = _agent(
        tmp_path,
        "stalls_empty_then_atomic_saves",
        _WRITES_AN_EMPTY_UNTRACKED_FILE_WITHOUT_COMMITTING,
        _MOVES_THE_EMPTY_STALL_ONTO_INHERITED_DIRT_THEN_ATOMIC_SAVES_REAL_WORK,
    )

    monkeypatch.setattr(agent_review_loop, "resolve_executable", lambda name, _label: name)
    monkeypatch.setattr(agent_review_loop, "claude_command", lambda _options: [sys.executable, str(agent)])
    monkeypatch.setattr(agent_review_loop, "perform_review", _requested_then_clean)
    exit_code = agent_review_loop.main(
        ["--repo", str(repository), "--task", "x", "--max-rounds", "2", "--heartbeat", "0"]
        + ["--allow-dirty", "--no-stall-retry", "--continue-on-no-commit"]
    )

    assert exit_code == agent_review_loop.EXIT_STALLED
    assert _attempts(tmp_path, "stalls_empty_then_atomic_saves") == 2
    # The committed file is the unrelated round 2 one, not the stalled work.
    assert _in(repository, "log", "-1", "--format=%s").strip() == "fix: an unrelated round 2 commit"
    # The round-2 work atomic-saved onto the dirt is untracked and outside every range.
    assert "renamed.py" not in _in(repository, "log", "--name-only", "--format=")
    assert _in(repository, "status", "--porcelain").strip() == "?? renamed.py"
    assert (repository / "renamed.py").read_text(encoding="utf-8") == FIX
    captured = capsys.readouterr()
    assert "the review never saw" in f"{captured.out}{captured.err}"


_HARD_LINKS_THE_STALL_TO_AN_ALIAS_AND_ATOMIC_SAVES_THE_STALL_ONTO_INHERITED_DIRT = (
    # A hard link carries the stall's inode onto a second name while the stall is
    # still alive, so alias.py and fix.py are the one object under two names.
    "pathlib.Path('alias.py').hardlink_to('fix.py')\n"
    # The stall then moves onto the operator's dirty renamed.py and an atomic save
    # replaces both its identities there: the edited work goes to a temp file renamed
    # over renamed.py, so renamed.py takes a fresh inode and the edited bytes while
    # alias.py keeps the stall's original inode and original bytes.
    "pathlib.Path('fix.py').replace('renamed.py')\n"
    f"pathlib.Path('atomic.tmp').write_text({FIX + _ROUND_TWO_EDIT!r}, encoding='utf-8')\n"
    "pathlib.Path('atomic.tmp').replace('renamed.py')\n"
    # Only the alias is committed. The stall's inode and its bytes both reach a commit
    # -- but under alias.py, not the name the stall was followed to -- so a range-wide
    # check for either would read the object as reviewed and gone.
    "subprocess.run(['git', 'add', 'alias.py'], check=True)\n"
    "subprocess.run(['git', 'commit', '-q', '-m', 'fix: commit the hard-linked alias'], check=True)\n"
    "sys.stdout.write('hard-linked the stall to alias.py, atomic-saved the edit onto the dirt, committed the alias\\n')\n"
)


def test_committing_a_hard_linked_alias_does_not_clear_the_stall_atomic_saved_away(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An alias sharing the stall's inode must not prove the object itself was committed.

    An inode is not one file's alone: a hard link puts it under a second name. Round 1
    stalls with untracked `fix.py`; round 2 hard-links it to `alias.py`, moves `fix.py`
    onto the operator's dirty `renamed.py`, atomic-saves edited bytes over it, and
    commits only `alias.py`. The alias carries the stall's original inode *and* its
    original bytes into the commit, so a proof that asked whether the stall's identity
    reached the range read it as reviewed, dropped `renamed.py`, and exited 0 over the
    edited work no review saw. No identity taken at a pathname is a sound answer here --
    the alias can carry the stall's inode and bytes onto any committed name -- so the
    inherited dirt the round rewrote is kept whenever a followed name vanishes,
    whatever a proof would say. `renamed.py` holds the edited work under no reviewed
    name, so the object stays outstanding and the run stops as stalled.
    """
    repository = _repository(tmp_path)
    # The operator's own uncommitted work, at the pathname round 2 will move onto.
    (repository / "renamed.py").write_text(_OPERATOR_DIRT, encoding="utf-8")
    agent = _agent(
        tmp_path,
        "stalls_then_commits_a_hard_linked_alias",
        _WRITES_AN_UNTRACKED_FILE_WITHOUT_COMMITTING,
        _HARD_LINKS_THE_STALL_TO_AN_ALIAS_AND_ATOMIC_SAVES_THE_STALL_ONTO_INHERITED_DIRT,
    )

    monkeypatch.setattr(agent_review_loop, "resolve_executable", lambda name, _label: name)
    monkeypatch.setattr(agent_review_loop, "claude_command", lambda _options: [sys.executable, str(agent)])
    monkeypatch.setattr(agent_review_loop, "perform_review", _requested_then_clean)
    exit_code = agent_review_loop.main(
        ["--repo", str(repository), "--task", "x", "--max-rounds", "2", "--heartbeat", "0"]
        + ["--allow-dirty", "--no-stall-retry", "--continue-on-no-commit"]
    )

    assert exit_code == agent_review_loop.EXIT_STALLED
    assert _attempts(tmp_path, "stalls_then_commits_a_hard_linked_alias") == 2
    # The committed alias carries the stalled bytes; the stalled object was not committed
    # at the name it was followed to, and the edited work is nowhere in history.
    assert _in(repository, "show", "HEAD:alias.py") == FIX
    assert "renamed.py" not in _in(repository, "log", "--name-only", "--format=")
    # The stalled work, plus the round-2 edit, is untracked and outside every range.
    assert _in(repository, "status", "--porcelain").strip() == "?? renamed.py"
    assert (repository / "renamed.py").read_text(encoding="utf-8") == FIX + _ROUND_TWO_EDIT
    captured = capsys.readouterr()
    assert "the review never saw" in f"{captured.out}{captured.err}"


_HARD_LINKS_THE_STALL_AND_RETURNS_THE_ALIAS_TO_THE_FOLLOWED_NAME = (
    # As above, but the alias is moved back onto the followed name and *that* is what
    # gets committed. The stall's original inode and original bytes are restored at
    # fix.py itself, so asking whether the stall was committed at the very name it was
    # followed to now answers yes -- and still must not clear the edited work.
    "pathlib.Path('alias.py').hardlink_to('fix.py')\n"
    "pathlib.Path('fix.py').replace('renamed.py')\n"
    f"pathlib.Path('atomic.tmp').write_text({FIX + _ROUND_TWO_EDIT!r}, encoding='utf-8')\n"
    "pathlib.Path('atomic.tmp').replace('renamed.py')\n"
    # The alias, carrying the stall's original inode and bytes, is moved back onto the
    # followed name and committed there. fix.py now reaches a commit with exactly the
    # identity the reconciliation captured, at exactly the name it followed.
    "pathlib.Path('alias.py').replace('fix.py')\n"
    "subprocess.run(['git', 'add', 'fix.py'], check=True)\n"
    "subprocess.run(['git', 'commit', '-q', '-m', 'fix: commit the returned alias'], check=True)\n"
    "sys.stdout.write('returned the alias to fix.py, atomic-saved the edit onto the dirt, committed fix.py\\n')\n"
)


def test_returning_a_hard_linked_alias_to_the_followed_name_does_not_clear_the_stall(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A stall's identity restored at its own followed name is still no proof of review.

    The variant a path-keyed proof cannot answer: the alias carries the stall's
    original inode and bytes back onto the followed name before committing, so `fix.py`
    reaches a commit holding exactly the identity the round captured, at exactly the
    name it was followed to. Round 1 stalls with untracked `fix.py`; round 2 hard-links
    it to `alias.py`, moves `fix.py` onto the operator's dirty `renamed.py`,
    atomic-saves the edited bytes over it, moves `alias.py` back onto `fix.py`, and
    commits `fix.py`. A proof asking whether the stall's inode and bytes are what the
    range committed at the followed name now says yes, and would drop `renamed.py` and
    exit 0 over the edited work no review saw. Because that answer is forgeable, the
    inherited dirt the round rewrote is kept regardless, so the stall stays outstanding
    and the run stops as stalled.
    """
    repository = _repository(tmp_path)
    # The operator's own uncommitted work, at the pathname round 2 will move onto.
    (repository / "renamed.py").write_text(_OPERATOR_DIRT, encoding="utf-8")
    agent = _agent(
        tmp_path,
        "stalls_then_returns_a_hard_linked_alias",
        _WRITES_AN_UNTRACKED_FILE_WITHOUT_COMMITTING,
        _HARD_LINKS_THE_STALL_AND_RETURNS_THE_ALIAS_TO_THE_FOLLOWED_NAME,
    )

    monkeypatch.setattr(agent_review_loop, "resolve_executable", lambda name, _label: name)
    monkeypatch.setattr(agent_review_loop, "claude_command", lambda _options: [sys.executable, str(agent)])
    monkeypatch.setattr(agent_review_loop, "perform_review", _requested_then_clean)
    exit_code = agent_review_loop.main(
        ["--repo", str(repository), "--task", "x", "--max-rounds", "2", "--heartbeat", "0"]
        + ["--allow-dirty", "--no-stall-retry", "--continue-on-no-commit"]
    )

    assert exit_code == agent_review_loop.EXIT_STALLED
    assert _attempts(tmp_path, "stalls_then_returns_a_hard_linked_alias") == 2
    # fix.py is committed at its own followed name with the stall's original bytes; the
    # edited work sits in renamed.py under no reviewed name and is retained.
    assert _in(repository, "show", "HEAD:fix.py") == FIX
    assert "renamed.py" not in _in(repository, "log", "--name-only", "--format=")
    assert _in(repository, "status", "--porcelain").strip() == "?? renamed.py"
    assert (repository / "renamed.py").read_text(encoding="utf-8") == FIX + _ROUND_TWO_EDIT
    captured = capsys.readouterr()
    assert "the review never saw" in f"{captured.out}{captured.err}"


def test_content_identity_is_only_asked_of_a_path_that_has_content(tmp_path: Path) -> None:
    """A directory, a path that is gone and an empty file identify nothing.

    `blob_ids` answers with git's own object id, so two paths holding the same
    bytes get the same answer wherever they sit. The three cases with no answer
    are left out rather than given a shared placeholder: an empty file's content
    is identical to every other empty file's, so matching on it would report the
    stall as having arrived anywhere a round happened to truncate a file.
    """
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "here.py").write_bytes(b"same bytes\n")
    (tmp_path / "elsewhere.py").write_bytes(b"same bytes\n")
    (tmp_path / "other.py").write_bytes(b"different bytes\n")
    (tmp_path / "empty.py").write_bytes(b"")
    (tmp_path / "subdir").mkdir()
    asked = {"here.py", "elsewhere.py", "other.py", "empty.py", "subdir/", "gone.py"}

    found = agent_review_loop.blob_ids(tmp_path, asked)

    assert set(found) == {"here.py", "elsewhere.py", "other.py"}
    assert found["here.py"] == found["elsewhere.py"] != found["other.py"]
    # Git's own object id for the content on disk, the identity `git add` would store.
    assert _in(tmp_path, "hash-object", "here.py").strip() == found["here.py"]


@pytest.mark.skipif(sys.platform == "win32", reason="a newline in a filename is a POSIX-only name")
def test_content_identity_survives_a_newline_in_the_pathname(tmp_path: Path) -> None:
    """A newline is a byte a POSIX name may hold, and it must not split the path in two.

    `blob_ids` hashes a whole set through one `git hash-object`, and a newline-delimited
    transport would read a name that itself contains a newline as two paths -- hashing
    the wrong bytes, or none, under a name the reconciliation then compares as unchanged.
    Passed positionally, the name is carried through whole, so it gets an id at all, that
    id is git's own for its content, and it differs from an unrelated file's.
    """
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    newline = "rename\nd.py"
    (tmp_path / newline).write_bytes(b"the stalled work, under a name with a newline in it\n")
    (tmp_path / "plain.py").write_bytes(b"an unrelated file\n")

    found = agent_review_loop.blob_ids(tmp_path, {newline, "plain.py"})

    # The name is hashed whole, not split, so both paths get an id and the two differ.
    assert set(found) == {newline, "plain.py"}
    assert found[newline] != found["plain.py"]
    # Git's own object id for the content under that exact name.
    assert _in(tmp_path, "hash-object", "--", newline).strip() == found[newline]


def test_argv_batches_split_by_byte_budget_and_never_drop_a_name() -> None:
    """A positional argv has a length the platform enforces, so the names are batched.

    The batching keeps each run's order, splits when the next name would push a run past
    the budget, and puts a single name too large for the budget in a run of its own
    rather than dropping or splitting it -- so concatenating the runs is exactly the
    names asked, in order.
    """
    names = ["aa", "bb", "cc", "dd"]
    # A budget of three bytes takes one two-byte name (cost 3) per run.
    batches = agent_review_loop._argv_batches(names, budget=3)
    assert batches == [["aa"], ["bb"], ["cc"], ["dd"]]
    # A wider budget packs several names into a run until the next would overflow it.
    assert agent_review_loop._argv_batches(names, budget=7) == [["aa", "bb"], ["cc", "dd"]]
    # A name past the budget is its own run, not dropped or split.
    assert agent_review_loop._argv_batches(["abcdefgh"], budget=3) == [["abcdefgh"]]
    # The concatenation of the runs is always the names, in order.
    assert [name for batch in agent_review_loop._argv_batches(names, budget=3) for name in batch] == names
    assert agent_review_loop._argv_batches([]) == []


def test_hash_object_lines_ids_up_across_several_batches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Split into runs, the ids still come back one per name and in the order asked.

    The transport is chunked so a large dirty tree does not overflow the argv, and the
    join across the runs must not scramble the answer. With the batching forced to one
    name per run, several `git hash-object` calls answer the set, and every name still
    gets its own id in order.
    """
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    names = []
    for index in range(5):
        name = f"f{index}.py"
        (tmp_path / name).write_bytes(f"contents of {name}\n".encode())
        names.append(name)
    # One name per run, so the ordering across the joins is what is under test.
    monkeypatch.setattr(
        agent_review_loop, "_argv_batches", lambda values, budget=0: [[value] for value in values]
    )

    ids = agent_review_loop._hash_object(tmp_path, names)

    assert ids is not None
    # Each id is git's own for the matching name, in order -- the join did not scramble.
    assert ids == [_in(tmp_path, "hash-object", "--", name).strip() for name in names]


def test_blob_ids_use_the_repositorys_object_format_and_clean_filters(tmp_path: Path) -> None:
    """The work-tree id is git's own, so inherited dirt and the round's edit compare alike.

    Every content comparison the reconciliation makes -- what the round inherited
    against what it left, in `relocated_stall` and `changed_inherited_dirty` -- must
    name both sides the way git itself would, or a filter turns an untouched file into
    a false change. A raw hash of the bytes on disk cannot: a `core.autocrlf` clean
    filter rewrites CRLF to LF on the way into git, and an `extensions.objectFormat=sha256`
    repository names objects in 64 hex digits, not sha1's 40. Both are here at once, and
    hashing through git -- the path's attributes applied, the repository's format used
    -- names the work-tree file the way `git add` would, so a line-ending-only change
    reads as the same blob.
    """
    subprocess.run(["git", "init", "-q", "--object-format=sha256", str(tmp_path)], check=True)
    for name, value in (
        ("user.name", "A Developer"),
        ("user.email", "dev@example.invalid"),
        ("commit.gpgsign", "false"),
        ("core.autocrlf", "true"),
    ):
        subprocess.run(["git", "-C", str(tmp_path), "config", name, value], check=True)
    (tmp_path / "crlf.py").write_bytes(b"line one\r\nline two\r\n")

    found = agent_review_loop.blob_ids(tmp_path, {"crlf.py"})

    # Sha256's 64 hex digits, not sha1's 40, and git's own id for the filtered content.
    assert len(found["crlf.py"]) == 64
    assert found["crlf.py"] == _in(tmp_path, "hash-object", "crlf.py").strip()
    # The clean filter normalises line endings, so the LF form names the same blob.
    (tmp_path / "crlf.py").write_bytes(b"line one\nline two\n")
    assert agent_review_loop.blob_ids(tmp_path, {"crlf.py"}) == found


def test_filesystem_identity_tells_apart_files_that_content_cannot(tmp_path: Path) -> None:
    """Two paths with the same bytes get distinct inodes; an empty file still gets one.

    Where `blob_ids` reads the bytes, `object_ids` reads the filesystem's own
    identity, and the two disagree exactly where a move plus an edit needs them to.
    Same-content paths that `blob_ids` cannot tell apart have distinct inodes here,
    and an empty file -- which `blob_ids` drops because every empty file shares its
    content -- has an identity of its own, so a stalled file emptied after a move
    is still followed. A path that is gone has none either way.
    """
    (tmp_path / "here.py").write_bytes(b"same bytes\n")
    (tmp_path / "elsewhere.py").write_bytes(b"same bytes\n")
    (tmp_path / "empty.py").write_bytes(b"")
    asked = {"here.py", "elsewhere.py", "empty.py", "gone.py"}

    found = agent_review_loop.object_ids(tmp_path, asked)

    assert set(found) == {"here.py", "elsewhere.py", "empty.py"}
    # Identical bytes, distinct objects -- the tell `blob_ids` cannot give.
    assert found["here.py"] != found["elsewhere.py"]
    # A rename carries the inode with it; the empty file keeps the identity a move
    # would preserve even as the round rewrites what is in it.
    (tmp_path / "here.py").rename(tmp_path / "moved.py")
    assert agent_review_loop.object_id(tmp_path / "moved.py") == found["here.py"]


def test_changed_inherited_dirty_keeps_what_the_round_rewrote_and_leaves_what_it_did_not(tmp_path: Path) -> None:
    """Rewritten, filled and cleared count as changed; the untouched and the gone do not.

    The conservative backstop's one judgement: which inherited-dirty paths this
    round rewrote. A path whose content moved from what it began with is a place a
    move onto inherited dirt could have landed -- gaining content it had none of
    and losing the content it had among the ways it moves -- and is kept. The
    operator's own file the round never touched reads the same and is left out, as
    is a path no longer present at all.
    """
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "rewritten.py").write_bytes(b"what the round left behind\n")
    (tmp_path / "untouched.py").write_bytes(b"the operator's own work, as the round found it\n")
    (tmp_path / "filled.py").write_bytes(b"content it had none of before\n")
    (tmp_path / "cleared.py").write_bytes(b"")
    # What two of them held as the round began, hashed the way the reconciliation
    # captured `inherited_blobs`: the rewritten path had other bytes, the cleared
    # path had content it has since lost. The filled path was empty then and so
    # carried no content identity, which is why it is absent below.
    (tmp_path / "started_rewritten").write_bytes(b"what the round started with\n")
    (tmp_path / "started_cleared").write_bytes(b"content the round cleared out\n")
    inherited_blobs = {
        "rewritten.py": agent_review_loop.blob_id(tmp_path, "started_rewritten"),
        "untouched.py": agent_review_loop.blob_id(tmp_path, "untouched.py"),
        "cleared.py": agent_review_loop.blob_id(tmp_path, "started_cleared"),
    }
    inherited_dirty = {"rewritten.py", "untouched.py", "filled.py", "cleared.py", "gone.py"}
    present = {"rewritten.py", "untouched.py", "filled.py", "cleared.py"}

    found = agent_review_loop.changed_inherited_dirty(tmp_path, present, inherited_dirty, inherited_blobs)

    assert found == {"rewritten.py", "filled.py", "cleared.py"}


def test_content_identity_of_a_symlink_is_its_link_text_not_its_target(tmp_path: Path) -> None:
    """A symlink is named by the mode 120000 blob git stores for it -- its link text.

    `git hash-object` over a symlink path follows it: for a live link it hashes the
    target file's content, and for a broken link it opens nothing and fails. Git stores
    neither -- a symlink is a blob of the link text -- so `blob_ids` reads the link and
    hashes that. A live link therefore does not borrow its target's identity, and a
    broken link gets an identity at all, which is the tell a move onto inherited dirt
    would otherwise leave nothing to follow.
    """
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "target.txt").write_bytes(b"the target file's own content\n")
    try:
        (tmp_path / "live").symlink_to("target.txt")
        (tmp_path / "broken").symlink_to("nowhere")
    except (OSError, NotImplementedError):
        pytest.skip("this machine does not allow creating a symlink")

    found = agent_review_loop.blob_ids(tmp_path, {"live", "broken", "target.txt"})

    # Every path gets an id, the broken link included.
    assert set(found) == {"live", "broken", "target.txt"}
    # The id is git's own for the link -- the blob it stages under mode 120000 --
    # not the content of what it points at.
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
    assert found["live"] == _in(tmp_path, "rev-parse", ":live").strip()
    assert found["broken"] == _in(tmp_path, "rev-parse", ":broken").strip()
    # The live link does not borrow the target's content identity.
    assert found["live"] != found["target.txt"]


def test_filesystem_identity_of_a_symlink_is_its_own_entry_not_its_target(tmp_path: Path) -> None:
    """`object_id` names the link's own inode, so a move that carries it is followed.

    Read through the target, a symlink would borrow the target's inode -- or none at
    all when the link is broken and the target cannot be `stat`'d -- and a rename that
    carried the link onto a new name would look like it landed on an unrelated object.
    Read with `lstat`, the link has an identity of its own that the rename carries, which
    is what lets a moved symlink stall be followed by inode.
    """
    (tmp_path / "target.txt").write_bytes(b"the target\n")
    try:
        (tmp_path / "live").symlink_to("target.txt")
        (tmp_path / "broken").symlink_to("nowhere")
    except (OSError, NotImplementedError):
        pytest.skip("this machine does not allow creating a symlink")

    live = agent_review_loop.object_id(tmp_path / "live")
    broken = agent_review_loop.object_id(tmp_path / "broken")

    # A broken link has an identity of its own though its target cannot be stat'd.
    assert broken is not None
    # The live link is named by its own entry, not the target's it resolves to.
    assert live is not None and live != agent_review_loop.object_id(tmp_path / "target.txt")
    # A rename carries the link's inode onto the new name -- the tell a move preserves.
    (tmp_path / "broken").rename(tmp_path / "moved")
    assert agent_review_loop.object_id(tmp_path / "moved") == broken


def test_changed_inherited_dirty_follows_a_symlink_by_its_link_text(tmp_path: Path) -> None:
    """A symlink whose target string the round rewrote is a rewrite like any other file.

    An atomic save can land a moved symlink stall on an inherited-dirty symlink with a
    fresh inode and fresh link text, so the conservative backstop is all that is left to
    catch it; it must read the destination's link text change. A link now pointing
    somewhere new differs from the blob it began with and is kept; one left pointing
    where the operator had it reads the same and is dropped.
    """
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    try:
        (tmp_path / "rewritten").symlink_to("where-it-points-now")
        (tmp_path / "untouched").symlink_to("where-the-operator-left-it")
        (tmp_path / "started_rewritten").symlink_to("where-it-began")
    except (OSError, NotImplementedError):
        pytest.skip("this machine does not allow creating a symlink")
    # What the rewritten link pointed at as the round began, captured the way
    # `inherited_blobs` is; the untouched link is its own before-state.
    inherited_blobs = {
        "rewritten": agent_review_loop.blob_id(tmp_path, "started_rewritten"),
        "untouched": agent_review_loop.blob_id(tmp_path, "untouched"),
    }
    inherited_dirty = {"rewritten", "untouched"}
    present = {"rewritten", "untouched"}

    found = agent_review_loop.changed_inherited_dirty(tmp_path, present, inherited_dirty, inherited_blobs)

    assert found == {"rewritten"}


def test_the_second_attempt_is_told_what_is_in_the_tree_and_keeps_the_first_ones_transcript(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Re-running the round's own prompt would invite a second implementation over the first."""
    repository = _repository(tmp_path)

    _one_round(repository, _stalling_agent(tmp_path, then=_COMMITS), monkeypatch)

    logs = repository / ".agentic-loop" / "logs"
    first = next(logs.rglob("round-01-claude.log")).read_text(encoding="utf-8")
    second = next(logs.rglob("round-01-claude-finish.log")).read_text(encoding="utf-8")
    # The evidence of what the stalled attempt did is not overwritten by the retry.
    assert "wrote the fix, then never came back to commit it" in first
    assert "UNCOMMITTED WORK" not in first
    # And the retry is handed the work rather than the task again.
    assert "UNCOMMITTED WORK" in second
    assert "?? fix.py" in second
    assert "it has already run once" in second
    for forbidden in ("git reset --hard", "git clean", "git stash"):
        assert forbidden in second


def test_the_loops_own_paperwork_is_not_read_as_the_implementers_work(tmp_path: Path) -> None:
    """A review document left in a tracked directory would make every declined round a stall."""
    repository = _repository(tmp_path)
    notes = repository / "notes"
    notes.mkdir()
    (notes / "index.md").write_text("mine\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repository), "add", "notes/index.md"], check=True)
    subprocess.run(["git", "-C", str(repository), "commit", "-q", "-m", "notes"], check=True)
    # Tracked, so paperwork_dir writes no .gitignore of its own: git reports this.
    directory = agent_review_loop.paperwork_dir(repository, notes, "20260101-000000")
    (directory / "round-01.md").write_text("four findings\n", encoding="utf-8")

    assert "notes/" in _in(repository, "status", "--porcelain")
    assert agent_review_loop.uncommitted(repository, (notes,)) == ""
    # The round's own work is still seen through the same exclusion.
    (repository / "fix.py").write_text(FIX, encoding="utf-8")
    assert "fix.py" in agent_review_loop.uncommitted(repository, (notes,))


def test_every_implementer_prompt_forbids_waiting_on_a_notification(tmp_path: Path) -> None:
    """The round that stalled announced it was waiting for the test suite to notify it.

    Nothing in this harness sends an agent anything, so that wait runs to
    `--timeout` with the round's work finished and uncommitted. The rule belongs
    in all three implementer prompts, the one that recovers from it included."""
    scratch = tmp_path / "round-01-claude"
    prompts = [
        agent_review_loop.implement_prompt("task", tmp_path / "reviews", "main", scratch),
        agent_review_loop.fix_prompt("task", tmp_path / "review.md", 2, "main", scratch),
        agent_review_loop.finish_prompt("task", None, 2, "main", scratch, "?? fix.py"),
    ]

    for prompt in prompts:
        flattened = " ".join(prompt.split())  # the block is wrapped; the rule is one sentence
        assert "FOREGROUND ONLY" in flattened
        assert "Run every command in the foreground and wait for its own output." in flattened
        assert "do not wait on a notification, a monitor, or a completion event" in flattened


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
def test_terminate_tree_kills_a_child_that_outlived_its_reaped_group_leader(tmp_path: Path) -> None:
    """A reaped leader is not an empty group; its descendants must still be killed.

    The agent leads its own process group, and a POSIX process group outlives its
    leader: the descendants that inherited it stay in it after the leader exits.
    When the leader has already exited and been reaped by the time
    `_terminate_tree` runs -- the agent quitting a moment after the timeout fired --
    `os.getpgid(leader_pid)` raises `ProcessLookupError` even while those
    descendants are still alive and writing the tree. The old code read the group
    that way and returned on that error as if it were empty, so `salvage_commit`
    ran against a tree a survivor was still editing. `_terminate_tree` now signals
    the group by the pid-as-pgid fixed at launch, so the survivor is killed even
    with the leader already gone (or, failing that, cleanup is reported unconfirmed
    so salvage is skipped -- the one thing it must never do is return clean here)."""
    workdir = tmp_path / "work"
    workdir.mkdir()
    child_pid_file = workdir / "child.pid"
    script = tmp_path / "leader_that_exits.py"
    script.write_text(
        "import os, pathlib, subprocess, sys\n"
        "devnull = open(os.devnull, 'w')\n"
        # A child left in the leader's own process group (no start_new_session), so
        # it stays in the group whose id is the leader's pid once the leader exits.
        "child = subprocess.Popen(\n"
        "    [sys.executable, '-c', 'import time; time.sleep(120)'],\n"
        "    stdout=devnull, stderr=devnull,\n"
        ")\n"
        "pathlib.Path('child.pid').write_text(str(child.pid), encoding='utf-8')\n"
        # The leader returns here, leaving the child alive behind it.
        ,
        encoding="utf-8",
    )

    process = subprocess.Popen(
        [sys.executable, str(script)],
        cwd=str(workdir),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    # Reap the leader before cleanup runs, reproducing the race exactly: the leader
    # is gone, so os.getpgid(leader_pid) now raises ProcessLookupError -- the lookup
    # the old code returned early on -- while its child lives on in the group whose
    # id is the leader's pid, which the kernel keeps reserved while a member remains.
    process.wait(timeout=30)
    child_pid = int(child_pid_file.read_text())
    assert _pid_alive(child_pid), "the child must outlive its leader for this test to mean anything"
    with pytest.raises(ProcessLookupError):
        os.getpgid(process.pid)

    try:
        agent_review_loop._terminate_tree(process, grace_s=5.0)
    except agent_review_loop.CleanupUnconfirmed:
        # Acceptable: reporting cleanup unconfirmed skips salvage. The bug under
        # test was returning as if the group were empty while the child still ran.
        return

    assert not _pid_alive(child_pid), "a descendant survived after its group leader had been reaped"


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
    """A signal that cannot be delivered to a group that still has a member.

    `_terminate_tree` used to suppress every signalling error, so a killpg that
    failed with EPERM was indistinguishable from one that worked. A member that
    was never signalled is still there and still writing, which is the one thing
    that must not be read as a clean kill. The group here really does have one --
    the live process below, never signalled because the delivery is refused --
    so the report names both halves: the signal that did not land, and the group
    that did not clear. Issue #320 narrowed this to the second half; a delivery
    error over a group that then reads empty is the tree being gone rather than
    a survivor, and is no longer raised (see the test below)."""
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True
    )
    monkeypatch.setattr(agent_review_loop, "_signal_group", lambda pgid, sig: False)
    try:
        with pytest.raises(agent_review_loop.CleanupUnconfirmed) as excinfo:
            agent_review_loop._terminate_tree(process, grace_s=0.1)
    finally:
        process.kill()
        process.wait()

    assert str(excinfo.value) == (
        f"could not signal process group {process.pid} with SIGTERM while killing pid {process.pid}, "
        "and the group still had a member after a 0s wait"
    )


@pytest.mark.skipif(sys.platform == "win32", reason="the process-group kill path is POSIX-only")
def test_terminate_tree_takes_an_empty_group_over_a_kill_that_could_not_be_delivered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Issue #320: the SIGKILL that fails because there is nobody left to kill.

    Darwin answers EPERM, not ESRCH, for a group the kernel still knows but whose
    remaining members are all zombies: its group iterator skips zombies, and a
    pass that signalled nobody is reported as a permission failure rather than as
    an empty group. `_terminate_tree` walks through exactly that state by design,
    so it was raising `could not signal process group N with SIGKILL` over a tree
    that was already gone -- a survivor an operator goes looking for and cannot
    find, and a round refused for it.

    The whole sequence the macOS leg hit is replayed here as injected answers
    rather than as timing, so it reproduces on any POSIX host and the ending is
    decided by the code: the SIGTERM lands, the group will not clear before its
    deadline, the SIGKILL is refused, and by the time membership is asked again
    the reaper has been past and the group is empty. Empty is the same positive
    confirmation the delivered path returns on, so this returns."""
    process = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True)
    process.wait(timeout=30)
    attempted: list[int] = []

    def killpg_as_macos_answered(pgid: int, sig: int) -> None:
        assert pgid == process.pid
        if sig != 0:
            attempted.append(sig)
            if sig == agent_review_loop.signal.SIGKILL:
                raise PermissionError()
            return
        # `_group_gone` asking: unclearable until the SIGKILL has been attempted,
        # then empty, which is the reaper collecting the last zombie member.
        if agent_review_loop.signal.SIGKILL not in attempted:
            raise PermissionError()
        raise ProcessLookupError()

    monkeypatch.setattr(agent_review_loop.os, "killpg", killpg_as_macos_answered)

    agent_review_loop._terminate_tree(process, grace_s=0.2)

    assert attempted == [agent_review_loop.signal.SIGTERM, agent_review_loop.signal.SIGKILL]


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


def test_terminate_tree_without_a_job_runs_taskkill_best_effort_then_always_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review round 3 finding 1: without a Job Object, nothing has tracked this
    tree's membership since before it ran its first instruction, so a
    `taskkill /T` walk is racing a live process tree the same way repeated
    `tasklist` snapshots were shown to in round 2 -- a descendant born after
    the walk, or even after the kill, is invisible to it, and no amount of
    re-enumeration can rule that out. taskkill is still run, once, as a
    courtesy, but even a clean-looking result -- 0 here -- must never be
    trusted as proof the tree is empty."""
    monkeypatch.setattr(agent_review_loop.sys, "platform", "win32")
    calls: list[list[str]] = []

    def fake_run(*arguments: object, **_keywords: object) -> subprocess.CompletedProcess:
        argv = list(arguments[0])  # type: ignore[index]
        calls.append(argv)
        assert argv[0] == "taskkill"
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(agent_review_loop.subprocess, "run", fake_run)

    class _FakeProcess:
        pid = 4243

        def kill(self) -> None:
            pass

        def poll(self) -> int:
            return 1

        def wait(self, timeout: float | None = None) -> int:
            return 1

    with pytest.raises(agent_review_loop.CleanupUnconfirmed, match="no Windows Job Object"):
        agent_review_loop._terminate_tree(_FakeProcess(), grace_s=0.1, job=None)  # type: ignore[arg-type]

    assert len(calls) == 1
    assert calls[0][:4] == ["taskkill", "/F", "/T", "/PID"]


def test_terminate_tree_without_a_job_raises_cleanup_unconfirmed_even_when_taskkill_itself_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other half: a taskkill that reports outright failure changes nothing
    about the outcome -- this branch was never going to trust its answer
    either way, so a failing exit code and a clean one lead to the same raise.
    """
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

        def poll(self) -> int:
            return 1

        def wait(self, timeout: float | None = None) -> int:
            return 1

    with pytest.raises(agent_review_loop.CleanupUnconfirmed, match="no Windows Job Object"):
        agent_review_loop._terminate_tree(_FakeProcess(), grace_s=0.1, job=None)  # type: ignore[arg-type]


def test_taskkill_tree_decodes_with_the_same_care_as_git_and_the_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    """taskkill is a console app, and on a non-English Windows its OEM codepage is
    not the ANSI codepage `text=True` decodes with by default -- the same mismatch
    that made a smart quote in a Codex message raise UnicodeEncodeError and
    deadlock a round, which is why `git()` and the agent's own `Popen` above both
    pin `encoding="utf-8", errors="replace"`. A byte outside that decoded a walk's
    stdout or stderr used to raise inside `subprocess.run`'s reader thread, an
    exception a background thread cannot hand back to `_terminate_tree` -- it
    surfaces as a lost taskkill confirmation, not as this test's own failure. This
    call is now asked far more often, once a failure is retried within the grace
    period, so a decode this narrow was going to be met.
    """
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        agent_review_loop.subprocess,
        "run",
        lambda *args, **kwargs: calls.append(kwargs) or subprocess.CompletedProcess(args[0], 0, "", ""),
    )

    agent_review_loop._taskkill_tree(4321)

    assert calls[0]["encoding"] == "utf-8"
    assert calls[0]["errors"] == "replace"


class _FakeSuspendedProcess:
    """A `Popen`-shaped double for `_track_in_job_object`'s job paths: it reads
    `.pid` and `._handle` and is collected with `.wait()` once its job has been
    confirmed empty, never spawns anything real, and needs no actual Windows
    underneath it -- the job-tracking wrappers it is handed to are monkeypatched
    below, the same way `_win32_job_fake` replaces the Job Object wrappers for
    the terminate-tree path rather than requiring a real job."""

    pid = 9001
    _handle = 0xFEED

    def wait(self, timeout: float | None = None) -> int:
        return 1


class _FakeSuspendedProcessKillable:
    """Adds the process-handle surface `_track_in_job_object` needs when it has
    to kill the still-suspended process directly, without a job: `.kill()`,
    `.poll()`, `.wait()` alongside the `.pid` / `._handle` that
    `_FakeSuspendedProcess` above already provides. `.kills` counts the kills so
    a test can assert the launch was failed rather than resumed."""

    pid = 9002
    _handle = 0xFACE

    def __init__(self, *, alive_after_kill: bool = False, kill_error: Exception | None = None) -> None:
        self._alive_after_kill = alive_after_kill or kill_error is not None
        self._kill_error = kill_error
        self.kills = 0

    def kill(self) -> None:
        self.kills += 1
        if self._kill_error is not None:
            raise self._kill_error

    def poll(self) -> int | None:
        return None if self._alive_after_kill else 1

    def wait(self, timeout: float | None = None) -> int:
        return 1


def _tracked_launch(process: object, *, suspended: bool = True) -> object:
    """Run `_track_in_job_object` the way `run_agent` does: an ownership record
    that exists before the call, and a `finally` around it that releases whatever
    the call recorded.

    Review round 6 finding 1: the release used to live inside the tracking call,
    where it could not cover the one path that left it -- the return that handed
    the job to `run_agent`. Now the call only records, so what a failed launch
    gives back is a property of the pair, and these tests exercise the pair."""
    agent_job = agent_review_loop._AgentJob(process=process, suspended=suspended)  # type: ignore[arg-type]
    try:
        agent_review_loop._track_in_job_object(agent_job)
    finally:
        agent_review_loop._release_job(agent_job)
    return agent_job


def test_track_in_job_object_creates_assigns_and_resumes(monkeypatch: pytest.MonkeyPatch) -> None:
    """The happy path: a job is created, the suspended process is assigned to it,
    and only then resumed -- so it cannot spawn anything before it is a member,
    closing the gap review round 2 finding 1 identified in the snapshot-based
    taskkill walk. Each step is recorded in the launch's ownership record as the
    kernel grants it, and nothing is returned: since review round 6 finding 1 the
    record, not a return value, is what `run_agent` releases from."""
    calls: list[str] = []
    monkeypatch.setattr(agent_review_loop, "_create_job_object", lambda: (calls.append("create"), 555)[1])
    monkeypatch.setattr(
        agent_review_loop,
        "_assign_process_to_job",
        lambda job, handle: (calls.append(f"assign:{job}:{handle}"), True)[1],
    )
    monkeypatch.setattr(agent_review_loop, "_resume_process_threads", lambda pid: (calls.append(f"resume:{pid}"), True)[1])
    monkeypatch.setattr(agent_review_loop, "_close_handle", lambda handle: calls.append(f"close:{handle}"))
    agent_job = agent_review_loop._AgentJob(process=_FakeSuspendedProcess(), suspended=True)  # type: ignore[arg-type]

    assert agent_review_loop._track_in_job_object(agent_job) is None

    assert (agent_job.handle, agent_job.member, agent_job.suspended) == (555, True, False)
    assert calls == ["create", f"assign:555:{_FakeSuspendedProcess._handle}", "resume:9001"]


def test_track_in_job_object_fails_the_launch_when_a_job_cannot_be_created(monkeypatch: pytest.MonkeyPatch) -> None:
    """Review round 4 finding 1: an agent that could not be placed under
    authoritative lifetime tracking is never resumed.

    Round 2 resumed it anyway, on the argument that a narrower fallback beat
    losing agent execution; round 3 finding 1 showed that fallback can never
    confirm a tree empty, and round 4 finding 1 showed the ordinary exits never
    reach cleanup at all, so an untracked agent's detached descendant simply
    outlives the round. The kill happens here, while the process is still
    suspended and has therefore spawned nothing, because this is the last
    moment at which "the whole tree is gone" is a statement anything can
    truthfully make about it."""
    resumed: list[int] = []
    monkeypatch.setattr(agent_review_loop, "_create_job_object", lambda: None)
    monkeypatch.setattr(agent_review_loop, "_resume_process_threads", lambda pid: resumed.append(pid) or True)
    process = _FakeSuspendedProcessKillable()

    with pytest.raises(agent_review_loop.AgentError, match="could not be placed under a Windows Job Object"):
        _tracked_launch(process)

    assert resumed == []
    assert process.kills == 1


def test_track_in_job_object_closes_the_job_and_fails_the_launch_when_assignment_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Assignment fails on a host where this process is already confined to a job
    that forbids nesting (pre-Windows 8, or an operator's own job policy). The
    orphaned job handle must not leak, and the process must not be resumed into
    a round whose cleanup could never be confirmed."""
    closed: list[int] = []
    resumed: list[int] = []
    monkeypatch.setattr(agent_review_loop, "_create_job_object", lambda: 555)
    monkeypatch.setattr(agent_review_loop, "_assign_process_to_job", lambda job, handle: False)
    monkeypatch.setattr(agent_review_loop, "_close_handle", lambda handle: closed.append(handle))
    monkeypatch.setattr(agent_review_loop, "_resume_process_threads", lambda pid: resumed.append(pid) or True)
    process = _FakeSuspendedProcessKillable()

    with pytest.raises(agent_review_loop.AgentError):
        _tracked_launch(process)

    assert closed == [555]
    assert resumed == []
    assert process.kills == 1


def test_track_in_job_object_fails_the_launch_when_a_step_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """Whatever goes wrong in the Win32 calls, the outcome is the same as a
    clean failure to create or assign: no tracking, so no run. The original
    error is chained onto the failure rather than lost."""
    resumed: list[int] = []

    def _boom() -> int | None:
        raise OSError("no more job objects")

    monkeypatch.setattr(agent_review_loop, "_create_job_object", _boom)
    monkeypatch.setattr(agent_review_loop, "_resume_process_threads", lambda pid: resumed.append(pid) or True)
    process = _FakeSuspendedProcessKillable()

    with pytest.raises(agent_review_loop.AgentError) as excinfo:
        _tracked_launch(process)

    assert isinstance(excinfo.value.__cause__, OSError)
    assert resumed == []
    assert process.kills == 1


def test_track_in_job_object_closes_an_already_created_job_when_assignment_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review round 3 finding 2's last point: the old `except Exception: job =
    None` dropped the handle `_create_job_object` had already handed back
    without closing it if `_assign_process_to_job` raised instead of merely
    returning False -- the assignment-returns-False case above already closed
    it, but a raise skipped straight past that line."""
    closed: list[int] = []

    def _boom(job: int, handle: int) -> bool:
        raise OSError("AssignProcessToJobObject failed")

    monkeypatch.setattr(agent_review_loop, "_create_job_object", lambda: 555)
    monkeypatch.setattr(agent_review_loop, "_assign_process_to_job", _boom)
    monkeypatch.setattr(agent_review_loop, "_close_handle", lambda handle: closed.append(handle))
    monkeypatch.setattr(agent_review_loop, "_resume_process_threads", lambda pid: True)
    process = _FakeSuspendedProcessKillable()

    with pytest.raises(agent_review_loop.AgentError):
        _tracked_launch(process)

    assert closed == [555]
    assert process.kills == 1


def test_track_in_job_object_kills_the_still_suspended_process_when_resume_fails_with_a_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review round 3 finding 2: a resume that returns False used to be
    swallowed, leaving the process suspended forever while `_track_in_job_object`
    returned the job handle as if launch had succeeded -- `run_agent` then
    waited out the whole round timeout for a process that never ran a single
    instruction. With a job already assigned, the still-suspended process (it
    ran no instruction, so it has no descendants of its own yet) is killed
    through the job, confirmed empty, and the failure raised immediately
    instead of silently returning."""
    monkeypatch.setattr(agent_review_loop, "_create_job_object", lambda: 555)
    monkeypatch.setattr(agent_review_loop, "_assign_process_to_job", lambda job, handle: True)
    monkeypatch.setattr(agent_review_loop, "_resume_process_threads", lambda pid: False)
    calls = _win32_job_fake(monkeypatch, active_counts=[0])

    with pytest.raises(agent_review_loop.AgentError) as excinfo:
        _tracked_launch(_FakeSuspendedProcess())

    assert not isinstance(excinfo.value, agent_review_loop.CleanupUnconfirmed)
    assert calls["terminate"] == [555]
    assert calls["close"] == [555]


@pytest.mark.parametrize(
    ("alive_after_kill", "kill_error"),
    [(True, None), (False, OSError("TerminateProcess failed"))],
    ids=["survives-the-kill", "the-kill-itself-errors"],
)
def test_track_in_job_object_raises_cleanup_unconfirmed_when_the_untracked_root_will_not_die(
    monkeypatch: pytest.MonkeyPatch, alive_after_kill: bool, kill_error: Exception | None
) -> None:
    """Fail safe, not fail available: killing the still-suspended process is the
    whole basis for failing an untracked launch cleanly, so a process that is
    still alive afterwards is reported as CleanupUnconfirmed rather than the
    plainer AgentError -- the same distinction `_terminate_tree` makes for a tree
    that resists its own kill, and the one the caller reads to skip salvage. A
    kill that errors rather than failing quietly is the same situation and gets
    the same answer, not an OSError nothing in the loop is looking for."""
    monkeypatch.setattr(agent_review_loop, "_create_job_object", lambda: None)
    monkeypatch.setattr(agent_review_loop, "_resume_process_threads", lambda pid: True)
    process = _FakeSuspendedProcessKillable(alive_after_kill=alive_after_kill, kill_error=kill_error)

    with pytest.raises(agent_review_loop.CleanupUnconfirmed):
        _tracked_launch(process)


def test_track_in_job_object_treats_a_raised_resume_error_the_same_as_a_false_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other half of round 3 finding 2: a resume that raises instead of
    returning False must not escape uncaught and leave the suspended process and
    its assigned job handle unmanaged -- it is handled the same way a False
    result is, and the original error is chained onto the failure this raises."""

    def _boom(pid: int) -> bool:
        raise OSError("OpenThread failed")

    monkeypatch.setattr(agent_review_loop, "_create_job_object", lambda: 555)
    monkeypatch.setattr(agent_review_loop, "_assign_process_to_job", lambda job, handle: True)
    monkeypatch.setattr(agent_review_loop, "_resume_process_threads", _boom)
    calls = _win32_job_fake(monkeypatch, active_counts=[0])

    with pytest.raises(agent_review_loop.AgentError) as excinfo:
        _tracked_launch(_FakeSuspendedProcess())

    assert isinstance(excinfo.value.__cause__, OSError)
    assert calls["terminate"] == [555]
    assert calls["close"] == [555]


def test_track_in_job_object_confirms_and_closes_the_job_when_the_handoff_is_interrupted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review round 5 finding 1: the release used to be an `except Exception`
    per step, so an exception that is not an `Exception` -- and an unattended
    loop's is the operator's Ctrl-C -- escaped between a successful assignment
    and the return that hands the job to `run_agent`, with the job neither
    terminated nor closed. Worse than a leaked handle, the resume may already
    have succeeded when the interrupt lands, so what is left inside that job is a
    running agent. The interrupt still reaches the caller; the job does not
    outlive it. Since round 6 finding 1 the release that makes that true is the
    owner's, not the tracking call's, which is why this runs the pair."""

    def _interrupt(pid: int) -> bool:
        raise KeyboardInterrupt

    monkeypatch.setattr(agent_review_loop, "_create_job_object", lambda: 555)
    monkeypatch.setattr(agent_review_loop, "_assign_process_to_job", lambda job, handle: True)
    monkeypatch.setattr(agent_review_loop, "_resume_process_threads", _interrupt)
    calls = _win32_job_fake(monkeypatch, active_counts=[0])

    with pytest.raises(KeyboardInterrupt):
        _tracked_launch(_FakeSuspendedProcess())

    assert calls["terminate"] == [555]
    assert calls["close"] == [555]


def test_track_in_job_object_reports_a_job_it_could_not_empty_over_an_interrupt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same interrupt, but the job will not confirm empty. CleanupUnconfirmed
    replaces the interrupt rather than the interrupt burying it: Ctrl-C ends the
    run either way, and only that type also tells the caller not to commit the
    tree a survivor may still be writing to. The handle is still closed exactly
    once, and the interrupt stays attached as the context."""

    def _interrupt(pid: int) -> bool:
        raise KeyboardInterrupt

    def refuse_to_confirm(job: int, process: object, grace_s: float) -> None:
        raise agent_review_loop.CleanupUnconfirmed(f"job {job} still had a member")

    monkeypatch.setattr(agent_review_loop, "_create_job_object", lambda: 555)
    monkeypatch.setattr(agent_review_loop, "_assign_process_to_job", lambda job, handle: True)
    monkeypatch.setattr(agent_review_loop, "_resume_process_threads", _interrupt)
    calls = _win32_job_fake(monkeypatch, active_counts=[0])
    monkeypatch.setattr(agent_review_loop, "_confirm_job_empty", refuse_to_confirm)

    with pytest.raises(agent_review_loop.CleanupUnconfirmed) as excinfo:
        _tracked_launch(_FakeSuspendedProcess())

    assert isinstance(excinfo.value.__context__, KeyboardInterrupt)
    assert calls["close"] == [555]


def test_track_in_job_object_kills_the_suspended_root_when_the_setup_is_interrupted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other half of round 5 finding 1: an interrupt before the process is a
    job member. Nothing is tracking it and nothing ever will, so it gets the
    untracked launch's answer -- the created-but-unassigned handle closed, and
    the root killed while it is still suspended and provably childless -- and
    the interrupt carries on afterwards rather than being converted into an
    AgentError the loop would salvage a round from."""

    def _interrupt(job: int, handle: int) -> bool:
        raise KeyboardInterrupt

    closed: list[int] = []
    monkeypatch.setattr(agent_review_loop, "_create_job_object", lambda: 555)
    monkeypatch.setattr(agent_review_loop, "_assign_process_to_job", _interrupt)
    monkeypatch.setattr(agent_review_loop, "_close_handle", lambda handle: closed.append(handle))
    monkeypatch.setattr(agent_review_loop, "_resume_process_threads", lambda pid: True)
    process = _FakeSuspendedProcessKillable()

    with pytest.raises(KeyboardInterrupt):
        _tracked_launch(process)

    assert closed == [555]
    assert process.kills == 1


def test_track_in_job_object_raises_cleanup_unconfirmed_when_an_interrupted_setups_root_will_not_die(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """And when that root outlives its kill, the interrupt gives way to
    CleanupUnconfirmed for the same reason the job path does: a suspended
    process that would not die is one the loop must not commit around."""

    def _interrupt() -> int | None:
        raise KeyboardInterrupt

    monkeypatch.setattr(agent_review_loop, "_create_job_object", _interrupt)
    monkeypatch.setattr(agent_review_loop, "_resume_process_threads", lambda pid: True)
    process = _FakeSuspendedProcessKillable(alive_after_kill=True)

    with pytest.raises(agent_review_loop.CleanupUnconfirmed) as excinfo:
        _tracked_launch(process)

    assert isinstance(excinfo.value.__context__, KeyboardInterrupt)
    assert process.kills == 1


def _win32_job_fake(
    monkeypatch: pytest.MonkeyPatch, *, active_counts: list[int | None], terminate_ok: bool = True
) -> dict[str, list]:
    """Route the Job Object wrappers `_terminate_tree` and `_release_job` use
    once they have a job handle -- each call answers from `active_counts` in
    order, standing in for the kernel's own live member count rather than a
    snapshot this module took itself. A count asked for after the list runs out
    gets its last entry, so a release that confirms a job `_terminate_tree` has
    already emptied reads the same answer twice rather than falling off the
    end."""
    calls: dict[str, list] = {"terminate": [], "count": [], "close": []}

    def fake_terminate(job: int) -> bool:
        calls["terminate"].append(job)
        return terminate_ok

    remaining = list(active_counts)
    last = active_counts[-1] if active_counts else 0

    def fake_count(job: int) -> int | None:
        calls["count"].append(job)
        return remaining.pop(0) if remaining else last

    def fake_close(job: int) -> None:
        calls["close"].append(job)

    monkeypatch.setattr(agent_review_loop, "_terminate_job", fake_terminate)
    monkeypatch.setattr(agent_review_loop, "_job_active_process_count", fake_count)
    monkeypatch.setattr(agent_review_loop, "_close_handle", fake_close)
    return calls


def test_terminate_tree_with_a_job_confirms_empty_from_the_kernels_own_count(monkeypatch: pytest.MonkeyPatch) -> None:
    """The job reports empty on the first ask: TerminateJobObject, then a clean
    return once the kernel's own count is zero -- no `tasklist` involved.

    The handle is borrowed, not consumed: since review round 6 finding 1 this
    call never closes it, because `_release_job` holds it across the call and
    closes it exactly once afterwards. That is what removes the interval in which
    a handle cleared out of the caller's record had not yet reached this frame's
    cleanup."""
    monkeypatch.setattr(agent_review_loop.sys, "platform", "win32")
    calls = _win32_job_fake(monkeypatch, active_counts=[0])

    class _FakeProcess:
        pid = 4321

        def kill(self) -> None:
            pass

        def poll(self) -> int:
            return 1

        def wait(self, timeout: float | None = None) -> int:
            return 1

    agent_review_loop._terminate_tree(_FakeProcess(), grace_s=1.0, job=777)  # type: ignore[arg-type]

    assert calls["terminate"] == [777]
    assert calls["close"] == []


def test_terminate_tree_with_a_job_waits_out_a_descendant_born_after_the_kill(monkeypatch: pytest.MonkeyPatch) -> None:
    """Review round 2 finding 1, closed rather than narrowed: a survivor
    (analogous to the reported root 4243 / captured survivor 5000) spawns a late
    child (analogous to 6000) that is only visible on a later poll. Because the
    child was born to a process the job already had -- not to one this module
    was still snapshotting -- the job's own count reflects it immediately, and
    this must keep waiting rather than declaring victory on a stale zero."""
    monkeypatch.setattr(agent_review_loop.sys, "platform", "win32")
    # 2 (survivor + late child, mid-termination), 1 (survivor gone, child lingers),
    # 0 (fully empty) -- unlike a `tasklist` snapshot, each answer is the job's
    # own live count, so a member born between polls is never missed.
    calls = _win32_job_fake(monkeypatch, active_counts=[2, 1, 0])

    class _FakeProcess:
        pid = 4243

        def kill(self) -> None:
            pass

        def poll(self) -> int:
            return 1

        def wait(self, timeout: float | None = None) -> int:
            return 1

    agent_review_loop._terminate_tree(_FakeProcess(), grace_s=5.0, job=777)  # type: ignore[arg-type]

    assert len(calls["count"]) >= 3


def test_terminate_tree_with_a_job_raises_when_it_never_empties(monkeypatch: pytest.MonkeyPatch) -> None:
    """A member wedged past the grace period is reported, not silently treated as gone."""
    monkeypatch.setattr(agent_review_loop.sys, "platform", "win32")
    calls = _win32_job_fake(monkeypatch, active_counts=[3, 2, 1])

    class _FakeProcess:
        pid = 4243

        def kill(self) -> None:
            pass

        def poll(self) -> int:
            return 1

        def wait(self, timeout: float | None = None) -> int:
            return 1

    with pytest.raises(agent_review_loop.CleanupUnconfirmed, match="could not be confirmed empty"):
        agent_review_loop._terminate_tree(_FakeProcess(), grace_s=0.2, job=777)  # type: ignore[arg-type]

    # And the borrowed handle is not closed here even on the failure path: the
    # owner closes it once, after this raises, and KILL_ON_JOB_CLOSE makes that
    # close itself one more attempt to kill whatever is still in the job.
    assert calls["close"] == []


def test_terminate_tree_with_a_job_fails_safe_when_the_job_cannot_be_queried(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unqueryable job (the handle itself gone bad) must not be read as an
    empty one -- the same fail-safe-not-fail-available reasoning as the taskkill
    path's `_all_confirmed_gone` returning None."""
    monkeypatch.setattr(agent_review_loop.sys, "platform", "win32")
    _win32_job_fake(monkeypatch, active_counts=[None])

    class _FakeProcess:
        pid = 4243

        def kill(self) -> None:
            pass

        def poll(self) -> int:
            return 1

        def wait(self, timeout: float | None = None) -> int:
            return 1

    with pytest.raises(agent_review_loop.CleanupUnconfirmed, match="could not be confirmed empty"):
        agent_review_loop._terminate_tree(_FakeProcess(), grace_s=0.1, job=777)  # type: ignore[arg-type]


def test_terminate_tree_with_a_job_still_checks_the_agent_handle(monkeypatch: pytest.MonkeyPatch) -> None:
    """The job reading empty says nothing about the agent handle `run_agent` is
    itself holding a pipe open against, mirroring the same check the taskkill
    path makes below."""
    monkeypatch.setattr(agent_review_loop.sys, "platform", "win32")
    _win32_job_fake(monkeypatch, active_counts=[0])

    class _SurvivingProcess:
        pid = 4244

        def kill(self) -> None:
            pass

        def poll(self) -> None:
            return None

        def wait(self, timeout: float | None = None) -> int:
            raise subprocess.TimeoutExpired("agent", timeout or 0)

    with pytest.raises(agent_review_loop.CleanupUnconfirmed, match="still running after its job was terminated"):
        agent_review_loop._terminate_tree(_SurvivingProcess(), grace_s=0.1, job=777)  # type: ignore[arg-type]


def _fake_tracking(monkeypatch: pytest.MonkeyPatch, *, job: int = 42, seen: list[int] | None = None) -> None:
    """Stand in for `_track_in_job_object` in the `run_agent` tests that are not
    about the tracking call itself.

    It writes `job` into the launch's ownership record exactly as the real call
    does once the kernel has granted every step -- a handle, the agent assigned
    to it, and the agent resumed -- so `run_agent`'s own release has the same
    thing to answer for. Since review round 6 finding 1 that record, rather than
    a returned handle, is the whole of what the call gives its caller."""

    def fake_track(agent_job: object) -> None:
        if seen is not None:
            seen.append(agent_job.process.pid)  # type: ignore[attr-defined]
        agent_job.handle, agent_job.member, agent_job.suspended = job, True, False  # type: ignore[attr-defined]

    monkeypatch.setattr(agent_review_loop, "_track_in_job_object", fake_track)


def test_run_agent_launches_windows_suspended_and_tracks_it_in_a_job_object(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`run_agent` requests `CREATE_SUSPENDED` and hands the process to
    `_track_in_job_object` before doing anything else with it -- before the
    reader thread starts, before the prompt is written -- so nothing the agent
    does can run ahead of being made a job member. The real `CREATE_SUSPENDED`
    flag only means something to a real Windows kernel, so the `Popen` call
    itself is intercepted here: what is under test is that `run_agent` asked
    for it and wired the result into `_terminate_tree`, not that a suspended
    process actually blocks on this host.
    """
    monkeypatch.setattr(agent_review_loop.sys, "platform", "win32")
    seen_creationflags: list[int] = []
    real_popen = subprocess.Popen

    def fake_popen(*args: object, **kwargs: object) -> subprocess.Popen:
        seen_creationflags.append(kwargs.pop("creationflags"))  # type: ignore[arg-type]
        return real_popen(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(agent_review_loop.subprocess, "Popen", fake_popen)
    tracked: list[int] = []
    _fake_tracking(monkeypatch, seen=tracked)
    # The agent exits on its own below, so `run_agent`'s own normal-exit
    # cleanup (review round 3 finding 3) confirms and closes job 42; these
    # stand in for the kernel the same way they do for `_terminate_tree`.
    job_calls = _win32_job_fake(monkeypatch, active_counts=[0])

    script = tmp_path / "quick.py"
    script.write_text("import sys\nsys.stdin.read()\nsys.stdout.write('done\\n')\n", encoding="utf-8")

    agent_review_loop.run_agent(
        [sys.executable, str(script)],
        "prompt",
        tmp_path,
        "[claude r1]",
        tmp_path / "log.txt",
        timeout=30,
        dry_run=False,
        heartbeat=0,
    )

    assert seen_creationflags == [agent_review_loop._CREATE_SUSPENDED]
    assert len(tracked) == 1
    assert job_calls["terminate"] == [42]
    assert job_calls["close"] == [42]


def test_run_agent_threads_the_tracked_job_into_terminate_tree_on_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The job `_track_in_job_object` recorded must be the one `_terminate_tree`
    is asked to confirm empty -- not a fresh, unrelated one -- or the whole point
    of assigning it before the process could spawn anything is lost the moment
    cleanup runs. It is lent to that call and closed once by the owner
    afterwards, which the test below this one asserts."""
    monkeypatch.setattr(agent_review_loop.sys, "platform", "win32")
    real_popen = subprocess.Popen

    def fake_popen(*args: object, **kwargs: object) -> subprocess.Popen:
        kwargs.pop("creationflags", None)
        return real_popen(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(agent_review_loop.subprocess, "Popen", fake_popen)
    _fake_tracking(monkeypatch)
    seen_jobs: list[int | None] = []
    real_terminate_tree = agent_review_loop._terminate_tree

    def fake_terminate_tree(process: object, *, grace_s: float = 5.0, job: int | None = None) -> None:
        seen_jobs.append(job)
        real_terminate_tree(process, grace_s=grace_s, job=job)  # type: ignore[arg-type]

    monkeypatch.setattr(agent_review_loop, "_terminate_tree", fake_terminate_tree)
    # The real `_terminate_tree` is used above (only its `job` argument is
    # intercepted), so with a job it takes the Job Object branch; these stand
    # in for the kernel calls that branch makes.
    _win32_job_fake(monkeypatch, active_counts=[0])

    script = tmp_path / "slow.py"
    script.write_text("import sys, time\nsys.stdout.write('go\\n')\nsys.stdout.flush()\ntime.sleep(120)\n", encoding="utf-8")

    with pytest.raises(agent_review_loop.AgentTimeout):
        agent_review_loop.run_agent(
            [sys.executable, str(script)],
            "prompt",
            tmp_path,
            "[claude r1]",
            tmp_path / "log.txt",
            timeout=0.3,
            dry_run=False,
            heartbeat=0,
        )

    assert seen_jobs == [42]


def _forced_windows_popen(monkeypatch: pytest.MonkeyPatch) -> list[subprocess.Popen[str]]:
    """Let the forced-Windows `run_agent` tests below start a real process here,
    and hand back every process they start so a test can ask whether it is still
    running.

    `CREATE_SUSPENDED` means something only to a real Windows kernel, and on
    POSIX `Popen` rejects any nonzero `creationflags` outright, so the flag is
    dropped and the process starts running immediately. Each script these tests
    launch therefore blocks on reading its stdin as its very first act, and
    `run_agent` writes that stdin only once the launch is tracked: the read
    stands in for the suspension, so whatever the script does after it models
    exactly what a real suspended agent could do only once it was resumed.
    """
    started: list[subprocess.Popen[str]] = []
    real_popen = subprocess.Popen

    def fake_popen(*args: object, **kwargs: object) -> subprocess.Popen:
        kwargs.pop("creationflags", None)
        process = real_popen(*args, **kwargs)  # type: ignore[arg-type]
        started.append(process)
        return process

    monkeypatch.setattr(agent_review_loop.subprocess, "Popen", fake_popen)
    return started


@pytest.mark.parametrize("exit_code", [7, 0])
def test_run_agent_never_runs_an_agent_it_could_not_place_under_a_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, exit_code: int
) -> None:
    """Review round 4 finding 1, in both the shapes it was reported in.

    An untracked agent used to be resumed and run to completion, and `run_agent`
    reached its normal-exit path with no cleanup call at all: an ordinary
    nonzero code went straight to `AgentError`, which `implement_round` salvages
    -- `git add -A` over a tree a detached descendant is still writing -- and a
    successful exit returned into the loop's commit inspection with the same
    survivor still running. Nothing could be added there to close it: once the
    root has exited, a PID-rooted taskkill cannot find descendants that are
    already orphaned. So neither exit is reached any more. The agent here would
    spawn a detached writer and exit `exit_code`; it never gets the stdin that
    stands in for its resume, and the worktree is untouched when the round ends.
    """
    monkeypatch.setattr(agent_review_loop.sys, "platform", "win32")
    started = _forced_windows_popen(monkeypatch)
    monkeypatch.setattr(agent_review_loop, "_create_job_object", lambda: None)
    resumed: list[int] = []
    monkeypatch.setattr(agent_review_loop, "_resume_process_threads", lambda pid: resumed.append(pid) or True)

    worktree = tmp_path / "worktree"
    worktree.mkdir()
    survivor = worktree / "written-after-the-round.txt"
    writer = tmp_path / "writer.py"
    writer.write_text(
        "import pathlib, sys, time\ntime.sleep(0.5)\npathlib.Path(sys.argv[1]).write_text('raced the round')\n",
        encoding="utf-8",
    )
    agent = tmp_path / "agent.py"
    agent.write_text(
        "import subprocess, sys\n"
        "sys.stdin.read()\n"
        "subprocess.Popen([sys.executable, sys.argv[1], sys.argv[2]], start_new_session=True)\n"
        f"sys.exit({exit_code})\n",
        encoding="utf-8",
    )

    with pytest.raises(agent_review_loop.AgentError) as excinfo:
        agent_review_loop.run_agent(
            [sys.executable, str(agent), str(writer), str(survivor)],
            "prompt",
            worktree,
            "[claude r1]",
            tmp_path / "log.txt",
            timeout=30,
            dry_run=False,
            heartbeat=0,
        )

    # Past the delay the writer would have waited out, had it ever been started.
    # This is the assertion the reported defect fails: the round was over and the
    # descendant wrote into the worktree afterwards.
    time.sleep(1.2)
    assert not survivor.exists()
    assert list(worktree.iterdir()) == []
    assert resumed == []
    assert started and all(process.poll() is not None for process in started)
    # A plain AgentError, salvageable, and safely so: this one is raised over a
    # tree the agent never touched, not over one a survivor may still be in.
    assert "could not be placed under a Windows Job Object" in str(excinfo.value)
    assert not isinstance(excinfo.value, agent_review_loop.CleanupUnconfirmed)


def _exploding_reader_thread(monkeypatch: pytest.MonkeyPatch, error: Exception, *, not_before: Path | None = None) -> None:
    """Fail `run_agent` between `_track_in_job_object` and its stdin/wait
    handling, where review round 4 finding 2 showed the job handle escaping
    every close path. Only the module's own `threading` reference is replaced,
    never the real `threading.Thread`, which the test session itself needs.

    A thread that cannot be created is tied to no particular moment in the round,
    so `not_before` is how a test chooses which moment it is reproducing: the
    failure is held until the agent has written that file. Without it the agent
    is usually still in its first instructions when the failure lands, which is
    the one shape where there is nothing yet for the release to answer for."""

    class _ExplodingThread:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def start(self) -> None:
            deadline = time.monotonic() + 30
            while not_before is not None and not not_before.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            raise error

        def join(self, timeout: float | None = None) -> None:
            pass

    monkeypatch.setattr(agent_review_loop, "threading", types.SimpleNamespace(Thread=_ExplodingThread))


def test_run_agent_confirms_and_closes_the_job_when_the_reader_thread_cannot_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review round 4 finding 2: a failure after the job handle is acquired and
    before the stdin/wait handling used to walk straight out of `run_agent`,
    closing nothing and killing nothing -- the raw handle leaked and the agent,
    already assigned to that job, was left running. The handle now has one
    owner: whatever the exception, the job is terminated, confirmed empty, and
    closed exactly once on the way out, and the exception itself still reaches
    the caller."""
    monkeypatch.setattr(agent_review_loop.sys, "platform", "win32")
    started = _forced_windows_popen(monkeypatch)
    _fake_tracking(monkeypatch)
    _exploding_reader_thread(monkeypatch, RuntimeError("can't start new thread"))
    job_calls = _win32_job_fake(monkeypatch, active_counts=[0])
    recorded_terminate = agent_review_loop._terminate_job

    def terminate_and_kill(job: int) -> bool:
        # TerminateJobObject really does reach every member, the agent included.
        # Modelling that is what lets the assertion below be about the process
        # this run actually left behind rather than about a fake's bookkeeping.
        for process in started:
            process.kill()
        return recorded_terminate(job)

    monkeypatch.setattr(agent_review_loop, "_terminate_job", terminate_and_kill)

    agent = tmp_path / "agent.py"
    agent.write_text("import sys\nsys.stdin.read()\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="can't start new thread"):
        agent_review_loop.run_agent(
            [sys.executable, str(agent)],
            "prompt",
            tmp_path,
            "[claude r1]",
            tmp_path / "log.txt",
            timeout=30,
            dry_run=False,
            heartbeat=0,
        )

    assert job_calls["terminate"] == [42]
    assert job_calls["close"] == [42]
    assert started and all(process.poll() is not None for process in started)


def test_run_agent_reports_a_job_it_could_not_empty_over_the_failure_that_reached_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same injected failure, but the job will not confirm empty. The handle is
    still closed exactly once, and the CleanupUnconfirmed replaces the original
    exception on the way out rather than being swallowed by it: a descendant
    that may still be writing to the tree outranks whatever failed above it, and
    only that type stops the caller from salvaging the tree underneath it. The
    failure it replaced is still attached as its context."""
    monkeypatch.setattr(agent_review_loop.sys, "platform", "win32")
    started = _forced_windows_popen(monkeypatch)
    _fake_tracking(monkeypatch)
    _exploding_reader_thread(monkeypatch, RuntimeError("can't start new thread"))
    job_calls = _win32_job_fake(monkeypatch, active_counts=[1])

    def refuse_to_confirm(job: int, process: subprocess.Popen[str], grace_s: float) -> None:
        # The root died with the job; something detached in it did not, which is
        # the case the kernel's count reports and this stands in for.
        process.kill()
        process.wait(timeout=10)
        raise agent_review_loop.CleanupUnconfirmed(f"job {job} still had a member")

    monkeypatch.setattr(agent_review_loop, "_confirm_job_empty", refuse_to_confirm)

    agent = tmp_path / "agent.py"
    agent.write_text("import sys\nsys.stdin.read()\n", encoding="utf-8")

    with pytest.raises(agent_review_loop.CleanupUnconfirmed) as excinfo:
        agent_review_loop.run_agent(
            [sys.executable, str(agent)],
            "prompt",
            tmp_path,
            "[claude r1]",
            tmp_path / "log.txt",
            timeout=30,
            dry_run=False,
            heartbeat=0,
        )

    assert isinstance(excinfo.value.__context__, RuntimeError)
    assert job_calls["close"] == [42]
    assert started and all(process.poll() is not None for process in started)


def _agent_with_a_descendant(tmp_path: Path, *, descendant_ignores_sigterm: bool = False) -> Path:
    """An agent that starts one long-lived process in its own group, records both
    pids, and then blocks for the rest of the round.

    The descendant is the shell-with-a-test-runner the group kill exists to
    reach: it is left in the agent's own process group (no `start_new_session`),
    so signalling only the leader would leave it running and writing. The final
    `sys.stdin.read()` is what keeps the agent there to be killed. It never
    returns in these tests, because the failure they inject lands before
    `run_agent` writes the prompt, and it is also why the agent never exits into
    the EPIPE race an agent that stops reading hands the loop.

    The agent publishes its own pid only once the descendant has reported itself
    ready, so a test that holds its injected failure until `agent.pid` appears is
    holding it until the whole tree is standing rather than until the leader is.
    With `descendant_ignores_sigterm` the descendant installs `SIG_IGN` for
    SIGTERM before reporting ready, which is how a test asks for a group that
    still has a live member once the SIGTERM has been and gone: without it the
    group is empty by then, and what the second signal meets is whichever of its
    members the reaper has not collected yet."""
    descendant = tmp_path / "descendant.py"
    descendant.write_text(
        "import pathlib, signal, time\n"
        + ("signal.signal(signal.SIGTERM, signal.SIG_IGN)\n" if descendant_ignores_sigterm else "")
        + "pathlib.Path('child.ready').write_text('1', encoding='utf-8')\n"
        "time.sleep(120)\n",
        encoding="utf-8",
    )
    script = tmp_path / "agent_with_a_descendant.py"
    script.write_text(
        "import os, pathlib, subprocess, sys, time\n"
        "devnull = open(os.devnull, 'w')\n"
        "child = subprocess.Popen(\n"
        "    [sys.executable, str(pathlib.Path(__file__).with_name('descendant.py'))],\n"
        "    stdout=devnull, stderr=devnull,\n"
        ")\n"
        "pathlib.Path('child.pid').write_text(str(child.pid), encoding='utf-8')\n"
        "ready, deadline = pathlib.Path('child.ready'), time.monotonic() + 30\n"
        "while not ready.exists() and time.monotonic() < deadline:\n"
        "    time.sleep(0.02)\n"
        "pathlib.Path('agent.pid').write_text(str(os.getpid()), encoding='utf-8')\n"
        "sys.stdin.read()\n",
        encoding="utf-8",
    )
    return script


@pytest.mark.skipif(sys.platform == "win32", reason="the process group is the POSIX half; Windows answers through a job")
def test_run_agent_kills_a_posix_agent_that_a_failure_after_the_launch_left_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review round 4 finding 2 on the platform the loop actually runs on.

    The Windows half of that finding was closed by giving `run_agent` one owner
    for the job. On POSIX there is no job, and the same `finally` had nothing to
    release, so the identical failure -- `reader.start()` raising, before a single
    byte of the prompt has been written -- walked out with the agent and
    everything it had already started still running. Nothing downstream picks
    that up either: `main` catches what it recognises and goes on to remove the
    reviewer checkout, sweep the scratch root and write the summary while the
    agent edits the tree it is tidying. The release now signals the group the
    agent leads, so the failure still reaches the caller and leaves nothing
    behind it."""
    workdir = tmp_path / "work"
    workdir.mkdir()
    agent = _agent_with_a_descendant(tmp_path)
    _exploding_reader_thread(monkeypatch, RuntimeError("can't start new thread"), not_before=workdir / "agent.pid")

    with pytest.raises(RuntimeError, match="can't start new thread"):
        agent_review_loop.run_agent(
            [sys.executable, str(agent)],
            "prompt",
            workdir,
            "[claude r1]",
            tmp_path / "log.txt",
            timeout=30,
            dry_run=False,
            heartbeat=0,
        )

    agent_pid = int((workdir / "agent.pid").read_text())
    child_pid = int((workdir / "child.pid").read_text())
    assert not _pid_alive(agent_pid), "the agent survived the failure that ended its round"
    assert not _pid_alive(child_pid), "a descendant survived the failure that ended its round"


@pytest.mark.skipif(sys.platform == "win32", reason="the process group is the POSIX half; Windows answers through a job")
def test_run_agent_reports_a_posix_group_that_will_not_clear_over_the_failure_that_reached_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same injected failure, but the group will not read empty.

    The precedence is the one the job path already applies, and it matters for
    the same reason: a member that may still be writing to the tree outranks
    whatever failed above it, and CleanupUnconfirmed is the only type that stops
    `implement_round` from salvaging underneath it. The failure it replaced is
    still attached as its context, so the transcript keeps saying what actually
    went wrong.

    Only the liveness answer is forced: both signals really are delivered to the
    agent's group, which is what the recorded pair asserts. Whether the processes
    have been collected yet is not asserted, because a member that has just been
    killed is briefly a zombie and `kill(pid, 0)` cannot tell that from a live
    one -- the waiting that normally settles it is exactly what this test has
    taken away.

    That is also why the descendant ignores SIGTERM (issue #320). Forcing the
    liveness answer to no leaves the group's real membership to say what it likes,
    and with a descendant that dies of the SIGTERM the group is down to whatever
    the reaper has not collected yet by the time the SIGKILL goes out: nothing at
    all under a fast one, a zombie under a slower one. Darwin refuses to signal a
    group of nothing but zombies, so on the leg that met one the run ended on the
    delivery report instead of on this one and the assertion below met the wrong
    string. A member that outlives the SIGTERM makes the group the test claims to
    have the group it actually has, and the SIGKILL is then delivered on every
    POSIX host rather than on most of them."""
    workdir = tmp_path / "work"
    workdir.mkdir()
    agent = _agent_with_a_descendant(tmp_path, descendant_ignores_sigterm=True)
    _exploding_reader_thread(monkeypatch, RuntimeError("can't start new thread"), not_before=workdir / "agent.pid")
    monkeypatch.setattr(agent_review_loop, "_group_gone", lambda pgid, *, deadline: False)
    signalled: list[int] = []
    delivering = agent_review_loop._signal_group
    monkeypatch.setattr(
        agent_review_loop, "_signal_group", lambda pgid, sig: signalled.append(sig) or delivering(pgid, sig)
    )

    with pytest.raises(agent_review_loop.CleanupUnconfirmed) as excinfo:
        agent_review_loop.run_agent(
            [sys.executable, str(agent)],
            "prompt",
            workdir,
            "[claude r1]",
            tmp_path / "log.txt",
            timeout=30,
            dry_run=False,
            heartbeat=0,
        )

    assert isinstance(excinfo.value.__context__, RuntimeError)
    assert "still had a member after SIGKILL" in str(excinfo.value)
    assert signalled == [agent_review_loop.signal.SIGTERM, agent_review_loop.signal.SIGKILL]


@pytest.mark.skipif(sys.platform == "win32", reason="the process group is the POSIX half; Windows answers through a job")
def test_run_agent_does_not_signal_the_group_of_a_posix_agent_that_exited_on_its_own(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one POSIX state the release deliberately says nothing about.

    A process group's id is its leader's pid, and the kernel reserves it only
    while the group still has a member. An agent that exited on its own has been
    reaped by `process.wait()` before the release runs, so if its group is empty
    that number is already free for somebody else's group and `killpg` on it
    would signal strangers -- which is worse than the survivor it was aimed at,
    and would be aimed at nothing in the overwhelmingly common case where the
    agent left nothing behind. So the release asks `poll()` first and signals
    only an agent still there to answer for. What that leaves open is the POSIX
    half of review round 4 finding 1, which needs an identity for the tree rather
    than a number for it, the way the Windows job handle is one."""
    workdir = tmp_path / "work"
    workdir.mkdir()
    agent = tmp_path / "agent.py"
    agent.write_text("import sys\nsys.stdin.read()\nsys.stdout.write('done\\n')\n", encoding="utf-8")
    killed: list[int] = []
    monkeypatch.setattr(agent_review_loop, "_terminate_tree", lambda process, **kwargs: killed.append(process.pid))

    agent_review_loop.run_agent(
        [sys.executable, str(agent)],
        "prompt",
        workdir,
        "[claude r1]",
        tmp_path / "log.txt",
        timeout=30,
        dry_run=False,
        heartbeat=0,
    )

    assert killed == []


def test_run_agent_leaves_no_live_agent_when_the_launch_is_interrupted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review round 5 finding 1 where the loop actually meets it: a Ctrl-C in
    the handoff, with a real process on the other end of it.

    The agent here is a job member and running -- in this model it has been
    since `Popen` returned, standing in for a resume that had already succeeded
    when the interrupt arrived. Nothing further downstream can clean up after it:
    `main` catches KeyboardInterrupt and walks on through its own summary and
    scratch-removal path with the agent still running in the tree it is tidying.
    So the release happens for an exception that is not an `Exception` as much as
    for one that is -- and since round 6 finding 1 it happens in `run_agent`'s
    own `finally`, which has owned this launch since before the tracking call was
    made rather than only once it returned a handle."""
    monkeypatch.setattr(agent_review_loop.sys, "platform", "win32")
    started = _forced_windows_popen(monkeypatch)
    forced_popen = agent_review_loop.subprocess.Popen

    def popen_with_a_process_handle(*args: object, **kwargs: object) -> subprocess.Popen:
        process = forced_popen(*args, **kwargs)  # type: ignore[operator]
        # Unlike the tests that replace `_track_in_job_object` outright, this one
        # runs the real tracking call, and that call reads the `_handle` only a
        # Windows `Popen` has to assign the process to the job. Only where the
        # platform does not provide one: on a real Windows host that attribute is
        # the kernel handle `Popen.__del__` polls with, and overwriting it leaves
        # the finalizer to fail on an invalid handle long after the test passed.
        # `_assign_process_to_job` is replaced below, so the value is never read.
        if not hasattr(process, "_handle"):
            process._handle = 0xFEED  # type: ignore[attr-defined]
        return process

    monkeypatch.setattr(agent_review_loop.subprocess, "Popen", popen_with_a_process_handle)
    monkeypatch.setattr(agent_review_loop, "_create_job_object", lambda: 42)
    monkeypatch.setattr(agent_review_loop, "_assign_process_to_job", lambda job, handle: True)

    def resume_then_interrupt(pid: int) -> bool:
        raise KeyboardInterrupt

    monkeypatch.setattr(agent_review_loop, "_resume_process_threads", resume_then_interrupt)
    job_calls = _win32_job_fake(monkeypatch, active_counts=[0])
    recorded_terminate = agent_review_loop._terminate_job

    def terminate_and_kill(job: int) -> bool:
        # TerminateJobObject really does reach every member, the agent included,
        # which is what makes the assertion below about this run's own leftover
        # process rather than about a fake's bookkeeping.
        for process in started:
            process.kill()
        return recorded_terminate(job)

    monkeypatch.setattr(agent_review_loop, "_terminate_job", terminate_and_kill)

    agent = tmp_path / "agent.py"
    agent.write_text("import sys\nsys.stdin.read()\n", encoding="utf-8")

    with pytest.raises(KeyboardInterrupt):
        agent_review_loop.run_agent(
            [sys.executable, str(agent)],
            "prompt",
            tmp_path,
            "[claude r1]",
            tmp_path / "log.txt",
            timeout=30,
            dry_run=False,
            heartbeat=0,
        )

    assert job_calls["terminate"] == [42]
    assert job_calls["close"] == [42]
    assert started and all(process.poll() is not None for process in started)


def test_run_agent_releases_a_job_whose_handle_never_reached_the_caller(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review round 6 finding 1: the interrupt that lands at the handover itself.

    Everything the tracking call does succeeds here -- the job is created, the
    agent is assigned to it, and the resume returns True -- and only then does
    the KeyboardInterrupt arrive, on the boundary where the handle was about to
    be returned. That was the one interval neither frame's cleanup covered. The
    old helper's `finally` read the flag saying it had handed the job over, while
    `run_agent` had not yet stored anything, so a job holding a resumed agent was
    left with no owner at all: nothing terminated it, nothing confirmed it empty,
    nothing closed the handle, and a raw Win32 handle held as an int is not
    something the interpreter collects on the way out.

    Nothing is handed over now. The launch's record holds the handle from the
    instant the kernel granted it, so the owner that was already active when this
    began terminates the job, confirms the kernel's own member count reads zero,
    and closes the handle exactly once -- and the interrupt still reaches the
    caller. The real tracking call runs here, wrapped only to raise as it
    returns, so what the release finds is what the real one records."""
    monkeypatch.setattr(agent_review_loop.sys, "platform", "win32")
    started = _forced_windows_popen(monkeypatch)
    forced_popen = agent_review_loop.subprocess.Popen

    def popen_with_a_process_handle(*args: object, **kwargs: object) -> subprocess.Popen:
        process = forced_popen(*args, **kwargs)  # type: ignore[operator]
        # Only where the platform does not provide one: see the sibling test
        # above. A real Windows `Popen` already carries the handle the tracking
        # call needs, and it is the one its finalizer polls with.
        if not hasattr(process, "_handle"):
            process._handle = 0xFEED  # type: ignore[attr-defined]
        return process

    monkeypatch.setattr(agent_review_loop.subprocess, "Popen", popen_with_a_process_handle)
    monkeypatch.setattr(agent_review_loop, "_create_job_object", lambda: 42)
    monkeypatch.setattr(agent_review_loop, "_assign_process_to_job", lambda job, handle: True)
    monkeypatch.setattr(agent_review_loop, "_resume_process_threads", lambda pid: True)
    real_track = agent_review_loop._track_in_job_object

    def track_then_interrupt(agent_job: object) -> None:
        # A complete, successful tracking call -- assigned and resumed -- with
        # the interrupt on its way out, which is the reviewer's reproduction at
        # the return line rather than at the resume the round before it.
        real_track(agent_job)
        raise KeyboardInterrupt

    monkeypatch.setattr(agent_review_loop, "_track_in_job_object", track_then_interrupt)
    job_calls = _win32_job_fake(monkeypatch, active_counts=[0])
    recorded_terminate = agent_review_loop._terminate_job

    def terminate_and_kill(job: int) -> bool:
        # TerminateJobObject really does reach every member, the agent included,
        # so the survivor assertion below is about this run's own process rather
        # than about a fake's bookkeeping.
        for process in started:
            process.kill()
        return recorded_terminate(job)

    monkeypatch.setattr(agent_review_loop, "_terminate_job", terminate_and_kill)

    agent = tmp_path / "agent.py"
    agent.write_text("import sys\nsys.stdin.read()\n", encoding="utf-8")

    with pytest.raises(KeyboardInterrupt):
        agent_review_loop.run_agent(
            [sys.executable, str(agent)],
            "prompt",
            tmp_path,
            "[claude r1]",
            tmp_path / "log.txt",
            timeout=30,
            dry_run=False,
            heartbeat=0,
        )

    assert job_calls["terminate"] == [42]
    assert job_calls["count"] == [42]
    assert job_calls["close"] == [42]
    assert started and all(process.poll() is not None for process in started)


def test_run_agent_closes_a_job_lent_to_terminate_tree_exactly_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other half of one owner: `_terminate_tree` borrows the handle.

    It used to consume it, which meant `run_agent` had to clear its own record
    before the call so the `finally` would not close it twice -- and an interrupt
    between that clear and the call's own `try` left the handle in a temporary
    nobody owned, with the agent still running inside the job it named. That is
    the same shape as the finding above, so it gets the same answer: the record
    keeps the handle across the call, `_terminate_tree` never closes it, and the
    release closes it once afterwards. From outside, one owner looks like two
    terminations -- the call's and the release's re-confirmation of an already
    empty job -- and exactly one close, over an agent that is still killed and
    reaped by the timeout path that borrowed it."""
    monkeypatch.setattr(agent_review_loop.sys, "platform", "win32")
    started = _forced_windows_popen(monkeypatch)
    _fake_tracking(monkeypatch)
    job_calls = _win32_job_fake(monkeypatch, active_counts=[0])

    agent = tmp_path / "slow.py"
    agent.write_text("import sys, time\nsys.stdin.read()\ntime.sleep(120)\n", encoding="utf-8")

    with pytest.raises(agent_review_loop.AgentTimeout):
        agent_review_loop.run_agent(
            [sys.executable, str(agent)],
            "prompt",
            tmp_path,
            "[claude r1]",
            tmp_path / "log.txt",
            timeout=0.5,
            dry_run=False,
            heartbeat=0,
        )

    assert job_calls["terminate"] == [42, 42]
    assert job_calls["close"] == [42]
    assert started and all(process.poll() is not None for process in started)


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
        "import pathlib, subprocess, sys, uuid\n"
        "sys.stdin.read()\n"
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
