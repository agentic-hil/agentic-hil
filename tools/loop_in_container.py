"""Run the Claude/Codex review loop in a Linux container instead of on Windows.

`tools/agent_review_loop.py` drives two agent CLIs against this repository. Run
on a Windows host, three separate things break it, and all three are properties
of the host rather than of the loop:

* pytest hardens its temporary directories -- inheritance cut, access granted
  through OWNER RIGHTS alone -- and the host sandbox stamps freshly created
  directories with per-session foreign SIDs. A reviewer's test run then dies
  with `PermissionError: [WinError 5]` on a directory the previous round made.
* Codex under `--sandbox workspace-write` could not run the suite at all. Adding
  `--add-dir` on Local AppData was a crutch and still failed on delete-pending.
* The POSIX half of the suite does not run here, which is where the platform
  bugs are: three changes in two days were green on Windows and red on CI.

In a Linux container there are no NTFS ACLs, no host sandbox, and the suite runs
on the platform CI runs it on. The container is also the isolation boundary,
which is why Codex runs inside it with its own sandbox off: see the comment in
`tools/loopimage/entrypoint.py`. That is safe *there* and nowhere else, so this
wrapper offers no option that weakens the container.

The credential handling is the arrangement `evals/install/` already uses, for
the reason its `credentials.py` states: a token refreshed inside a container may
rotate the refresh token, and the rotated value dies with the container while
this machine keeps the stale one. So health is decided here, refreshing happens
here, and the container receives individual login files as read-only bind
mounts -- never a profile directory. `credential_health` reports `unknown` for
the Codex login because that file states no expiry; that is a real gap in the
pre-flight and it is the evaluation's gap too, so it is reported rather than
papered over.

The home the container builds from those files is a Docker volume that is
deleted unconditionally afterwards. The evaluation scrubs instead, because its
evidence is inspected later; here the only product is the commits in the
repository mount, so the home has no reason to outlive the run.

Usage:

    python tools/loop_in_container.py --task "..." --max-rounds 3
    python tools/loop_in_container.py --start-with review --codex-model gpt-5.1-codex
    python tools/loop_in_container.py --rebuild --task "..."

Every option this wrapper does not consume is handed to `agent_review_loop.py`
unchanged.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
# evals/ is a source tree rather than an installed package, so a script run as
# "python tools/loop_in_container.py" cannot import it without this.
sys.path.insert(0, str(REPOSITORY_ROOT))

from evals.install.credentials import authentication_failure, credential_health  # noqa: E402
from evals.install.runner import (  # noqa: E402
    docker_mount,
    docker_security_options,
    docker_subprocess_environment,
    remove_container,
    remove_volume,
    run_capture,
)

CONTAINER_DIRECTORY = REPOSITORY_ROOT / "tools" / "loopimage"
# Everything the image is built from. The tag carries their digest, so editing
# the Dockerfile or the entrypoint produces a tag that does not exist yet and
# gets built, instead of a silent run against yesterday's recipe.
RECIPE = ("Dockerfile", "entrypoint.py", "package.json", "package-lock.json")
IMAGE_NAME = "agentic-hil-loop"

MOUNTED_REPOSITORY = "/repo"
CONTAINER_HOME = "/home/loop"
MOUNTED_CREDENTIALS = "/run/loop-agent-logins"

EXIT_NO_DOCKER = 2
EXIT_REFUSED = 3

# One file per mount, addressed by kind, exactly as the evaluation does it. The
# profile directories these live in are never mounted: they hold every project
# the operator has ever opened, and none of that is the container's business.
CREDENTIAL_FILES = {
    "claude-auth": Path(".claude") / ".credentials.json",
    "claude-config": Path(".claude.json"),
    "codex-auth": Path(".codex") / "auth.json",
}
# claude-config carries account and onboarding state rather than a token, so
# there is no expiry in it to judge.
HEALTH_CHECKED = ("claude-auth", "codex-auth")
# Forwarded by name only when set on this machine, so a container never receives
# an empty variable that looks like a configured credential.
CREDENTIAL_ENVIRONMENT = ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "OPENAI_API_KEY")

# Measured rather than guessed, from the evaluation's own limits. The suite
# alone peaks at 287 MiB and 37 processes in this image; a full round adds the
# editable install, the reviewer's clone and two Node CLIs on top of that, and
# the container reports its own peak on every run (see report_peaks in the
# entrypoint) so this number stays checkable. Memory is the one limit raised:
# the evaluation's 2g is close enough to a measured round to be the cap that
# kills it. The process limit and everything else in docker_security_options()
# are taken unchanged.
MEMORY_LIMIT = "3g"
PIDS_LIMIT = "512"

# The loop announces both directories on startup; they are the run's paperwork
# and they land in the mount, so they can be named on this side afterwards.
ANNOUNCED_DIRECTORY = re.compile(r"^(reviews|logs)\s+:\s+(/repo/\S+)\s*$")


class Refused(RuntimeError):
    """The run must not start, for a reason worth printing on its own."""


def repository_root(start: Path) -> Path:
    """The work tree this file belongs to, so the tool works from any cwd."""
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=start,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return REPOSITORY_ROOT
    return Path(result.stdout.strip())


def docker_source(path: Path) -> str:
    """The path in the form the Docker CLI expects on this platform.

    Docker Desktop on Windows takes `C:/path`; a POSIX absolute path passes
    through unchanged. `as_posix()` produces both from a `Path`.
    """
    return path.resolve().as_posix()


def recipe_digest() -> str:
    digest = hashlib.sha256()
    for name in RECIPE:
        digest.update(name.encode("utf-8"))
        digest.update((CONTAINER_DIRECTORY / name).read_bytes())
    return digest.hexdigest()[:12]


def image_present(docker: str, image: str) -> bool:
    return run_capture([docker, "image", "inspect", "--format", "{{.Id}}", image], 60).returncode == 0


def build_image(docker: str, image: str) -> None:
    print(f"building {image} from {CONTAINER_DIRECTORY / 'Dockerfile'}", flush=True)
    completed = subprocess.run(
        [
            docker,
            "build",
            "--file",
            str(CONTAINER_DIRECTORY / "Dockerfile"),
            "--tag",
            image,
            # The image needs nothing from outside this directory, and the
            # repository root's .dockerignore excludes everything but evals/.
            str(CONTAINER_DIRECTORY),
        ],
        check=False,
        env=docker_subprocess_environment(docker),
    )
    if completed.returncode != 0:
        raise Refused(f"could not build {image}")


def git_identity(repo: Path) -> tuple[str, str]:
    """Who the container commits as.

    `git var` applies git's own precedence -- GIT_AUTHOR_* over config -- and
    fails with the message that tells an operator exactly what to set. A
    container with no identity fails at the first `git commit`, which is after
    a round of model spend, so the question is asked here instead.
    """
    result = run_capture(["git", "-C", str(repo), "var", "GIT_AUTHOR_IDENT"], 30)
    if result.returncode != 0:
        raise Refused(
            "this machine has no git identity, so the container could not commit anything:\n"
            f"{result.stderr.strip() or result.stdout.strip()}"
        )
    identity = result.stdout.strip()
    match = re.match(r"^(?P<name>.*?)\s*<(?P<email>[^>]*)>", identity)
    if match is None:
        raise Refused(f"could not read a name and address out of git's author identity: {identity!r}")
    return match.group("name"), match.group("email")


def refresh_stale_login(kind: str, detail: str) -> None:
    """Refresh a login on this machine, where a rotated token survives the run.

    Starting the CLI is what refreshes it; there is no command that only
    refreshes, so this spends one very small session to do it.
    """
    if kind != "claude-auth":
        raise Refused(
            f"the {kind} login needs refreshing here first ({detail}), and this wrapper knows no "
            "host-side refresh for it. Start its CLI once on this machine, then rerun."
        )
    executable = shutil.which("claude")
    if executable is None:
        raise Refused(
            f"the {kind} login needs refreshing ({detail}) and claude is not on PATH here. "
            "Refreshing it inside the container can rotate the token and leave this machine's "
            "copy rejected. Start Claude Code once on this machine, then rerun."
        )
    print(f"{kind}: {detail}; starting Claude Code here once so the refreshed token stays on this machine", flush=True)
    completed = subprocess.run(
        [executable, "-p", "--output-format", "text", "reply with: ok"],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    if completed.returncode != 0:
        raise Refused(f"the {kind} refresh attempt failed: {completed.stderr.strip() or completed.stdout.strip()}")


def preflight_credentials() -> list[tuple[str, Path]]:
    resolved: list[tuple[str, Path]] = []
    for kind, relative in CREDENTIAL_FILES.items():
        path = Path.home() / relative
        if not path.is_file():
            raise Refused(f"{kind}: no stored login at {path}. Sign in with the CLI on this machine first.")
        resolved.append((kind, path.resolve()))

    for kind, path in resolved:
        if kind not in HEALTH_CHECKED:
            continue
        state, detail = credential_health(kind, path)
        if state == "expired":
            raise Refused(f"the {kind} login is unusable: {detail}. Sign in again, then rerun.")
        if state == "stale":
            refresh_stale_login(kind, detail)
            state, detail = credential_health(kind, path)
            if state != "ok":
                raise Refused(
                    f"the {kind} login is still {state} after refreshing here: {detail}. "
                    "Sign in again, then rerun."
                )
        print(f"{kind}: {state} ({detail})", flush=True)
    return resolved


def container_security_options() -> list[str]:
    """The evaluation's options, with the two this loop's own work outgrows.

    Substituted rather than appended: two `--memory` flags on one command line
    is a bet on which one Docker keeps, and `.index` fails loudly here if the
    evaluation ever stops setting one of them.
    """
    options = docker_security_options()
    for flag, value in (("--memory", MEMORY_LIMIT), ("--pids-limit", PIDS_LIMIT)):
        options[options.index(flag) + 1] = value
    return options


def container_command(
    *,
    docker: str,
    image: str,
    container_name: str,
    home_volume: str,
    repository: Path,
    credentials: list[tuple[str, Path]],
    identity: tuple[str, str],
    forwarded: list[str],
) -> list[str]:
    name, email = identity
    command = [
        docker,
        "run",
        "--rm",
        "--name",
        container_name,
        # An agent CLI starts shells that start more processes; without a real
        # init to reap them, zombies accumulate against --pids-limit.
        "--init",
        *container_security_options(),
        "--mount",
        docker_mount("volume", home_volume, CONTAINER_HOME),
        # Read-write, and the one deviation that matters: the loop's product is
        # the commits it makes, and they have to land in the repository this
        # was started from.
        "--mount",
        docker_mount("bind", docker_source(repository), MOUNTED_REPOSITORY),
        "--workdir",
        MOUNTED_REPOSITORY,
        # git reads these before it reads any config, so the commits carry the
        # identity this machine commits under.
        "--env",
        f"GIT_AUTHOR_NAME={name}",
        "--env",
        f"GIT_AUTHOR_EMAIL={email}",
        "--env",
        f"GIT_COMMITTER_NAME={name}",
        "--env",
        f"GIT_COMMITTER_EMAIL={email}",
    ]
    for kind, path in credentials:
        command += [
            "--mount",
            docker_mount("bind", docker_source(path), f"{MOUNTED_CREDENTIALS}/{kind}", readonly=True),
        ]
    for variable in CREDENTIAL_ENVIRONMENT:
        if os.environ.get(variable):
            command += ["--env", variable]
    return [
        *command,
        image,
        "--credential-kinds",
        *[kind for kind, _path in credentials],
        "--",
        *forwarded,
    ]


def stream(command: list[str], repository: Path) -> tuple[int, dict[str, str], str | None]:
    """Run the container, printing its output as it arrives.

    Returns the exit code, the loop's own directories translated back to this
    side of the mount, and the first phrase that looks like an agent CLI
    refusing a login -- which is the failure worth naming rather than debugging.
    """
    announced: dict[str, str] = {}
    failure: str | None = None
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        env=docker_subprocess_environment(command[0]),
    )
    assert process.stdout is not None
    for line in process.stdout:
        print(line.rstrip(), flush=True)
        match = ANNOUNCED_DIRECTORY.match(line)
        if match is not None:
            inside = match.group(2)
            announced[match.group(1)] = str(repository / Path(inside[len(MOUNTED_REPOSITORY) + 1 :]))
        if failure is None:
            failure = authentication_failure(line)
    return process.wait(), announced, failure


def main(argv: list[str] | None = None) -> int:
    # Agent output is UTF-8; the default Windows console codepage is not.
    for handle in (sys.stdout, sys.stderr):
        if hasattr(handle, "reconfigure"):
            handle.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        # No abbreviations: every unrecognised option belongs to the inner loop,
        # and prefix matching here would swallow one of its options instead.
        allow_abbrev=False,
    )
    parser.add_argument("--docker", default="docker", help="Docker CLI to use.")
    parser.add_argument("--repo", type=Path, default=None, help="Repository to mount and work in.")
    parser.add_argument("--image", default=None, help="Container image, overriding the one built from tools/loopimage.")
    parser.add_argument("--rebuild", action="store_true", help="Build the image even when it is already present.")
    options, forwarded = parser.parse_known_args(argv)
    # Both calling styles work: with or without the separator ci_linux.py uses.
    forwarded = [argument for argument in forwarded if argument != "--"]

    if shutil.which(options.docker) is None:
        print(
            f"{options.docker} was not found on PATH.\n"
            "This wrapper exists to run the review loop on Linux, where the suite's temporary\n"
            "directories are not NTFS and Codex needs no sandbox of its own. Without Docker,\n"
            "run tools/agent_review_loop.py directly and expect the Windows failures it names.",
            file=sys.stderr,
        )
        return EXIT_NO_DOCKER

    docker = shutil.which(options.docker) or options.docker
    repository = (options.repo or repository_root(Path(__file__).resolve().parent)).resolve()
    image = options.image or f"{IMAGE_NAME}:{recipe_digest()}"

    try:
        credentials = preflight_credentials()
        identity = git_identity(repository)
        if options.rebuild or not image_present(docker, image):
            build_image(docker, image)
    except Refused as error:
        print(f"error: {error}", file=sys.stderr)
        return EXIT_REFUSED

    token = uuid.uuid4().hex[:12]
    container_name = f"agentic-hil-loop-{token}"
    home_volume = f"agentic-hil-loop-{token}-home"
    result = run_capture([docker, "volume", "create", "--label", "agentic-hil.loop=true", home_volume], 60)
    if result.returncode != 0:
        print(f"error: could not create the home volume: {result.stderr.strip()}", file=sys.stderr)
        return EXIT_REFUSED

    command = container_command(
        docker=docker,
        image=image,
        container_name=container_name,
        home_volume=home_volume,
        repository=repository,
        credentials=credentials,
        identity=identity,
        forwarded=forwarded,
    )
    print(f"repository : {repository} (read-write at {MOUNTED_REPOSITORY})")
    print(f"image      : {image}")
    print(f"identity   : {identity[0]} <{identity[1]}>")
    print(f"logins     : {', '.join(kind for kind, _path in credentials)} (read-only, one file each)")
    print(f"home volume: {home_volume} (deleted when this exits)")
    print(f"$ {' '.join(command)}", flush=True)

    announced: dict[str, str] = {}
    failure: str | None = None
    try:
        exit_code, announced, failure = stream(command, repository)
    except KeyboardInterrupt:
        print("\ninterrupted; stopping the container.", file=sys.stderr)
        exit_code = EXIT_REFUSED
    finally:
        for remove, kind, name in ((remove_container, "container", container_name), (remove_volume, "volume", home_volume)):
            try:
                remove(docker, name)
            except (OSError, RuntimeError, subprocess.SubprocessError) as error:
                # Worth naming: the home volume is the one place a copy of a
                # login could outlive the run.
                print(f"warning: could not remove the {kind} {name}: {error}", file=sys.stderr)

    if failure is not None and exit_code != 0:
        print(
            f"\nan agent CLI reported an authentication failure inside the container: {failure!r}\n"
            "The stored login needs to be renewed on this machine; the container cannot fix it.",
            file=sys.stderr,
        )
    print(f"\nreviews    : {announced.get('reviews', repository / '.agentic-loop' / 'reviews')}")
    print(f"logs       : {announced.get('logs', repository / '.agentic-loop' / 'logs')}")
    print(f"commits    : in {repository}; nothing was pushed and no pull request was opened.")
    return exit_code


if __name__ == "__main__":  # pragma: no cover - entry point
    raise SystemExit(main())
