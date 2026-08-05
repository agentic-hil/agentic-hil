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

The operator's standing instructions to each CLI travel the same way, when they
exist. A fresh home has none, and a fresh home is what the first containerised
round ran in: the agent had never been told this operator's rules and signed its
commit with an attribution trailer they forbid.

The home the container builds from those files is a Docker volume that is
deleted unconditionally afterwards. The evaluation scrubs instead, because its
evidence is inspected later; here the only product is the commits in the
repository mount, so the home has no reason to outlive the run.

Usage:

    python tools/loop_in_container.py --task "..." --max-rounds 3
    python tools/loop_in_container.py --start-with review --codex-model gpt-5.1-codex
    python tools/loop_in_container.py --rebuild --task "..."

Every option this wrapper does not consume is handed to `agent_review_loop.py`
unchanged, except the four that decide what the container isolates -- `--repo`,
`--codex-sandbox`, `--review-checkout` and `--review-checkout-dir`. Those are
set here and refused rather than forwarded: a second `--codex-sandbox` is how
the value that was measured stops being the value that runs, and a reviewer
checkout pointed back at the repository mount is the Windows failure this
wrapper exists to bury.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import posixpath
import re
import shutil
import subprocess
import sys
import uuid
from pathlib import Path, PurePosixPath

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
MOUNTED_FILES = "/run/loop-agent-files"

EXIT_NO_DOCKER = 2
EXIT_REFUSED = 3
# A run whose home volume outlived it is not a successful run, whatever the loop
# inside it returned: that volume is the one place a copy of a login can be.
EXIT_CLEANUP_FAILED = 4

# Inner options that decide what the container isolates rather than how the loop
# is run. The wrapper sets all four and refuses to forward any of them: a second
# --codex-sandbox would take the value argparse saw last, and a
# --review-checkout-dir or --review-checkout none puts the reviewer's test runs
# back onto the repository mount, which is the Windows failure this exists to
# bury. tools/loopimage/entrypoint.py repeats the refusal on the far side.
CONTAINER_OWNED_OPTIONS = ("--repo", "--codex-sandbox", "--review-checkout", "--review-checkout-dir")
# Inner options naming where the run's paperwork lands. They are forwarded, and
# they are checked: a directory outside the mount dies with the container, and
# then nothing this wrapper printed about it was ever true.
PAPERWORK_OPTIONS = ("--review-dir", "--log-dir")
DEFAULT_PAPERWORK = {
    "--review-dir": f"{MOUNTED_REPOSITORY}/.agentic-loop/reviews",
    "--log-dir": f"{MOUNTED_REPOSITORY}/.agentic-loop/logs",
}

# One file per mount, addressed by kind, exactly as the evaluation does it. The
# profile directories these live in are never mounted: they hold every project
# the operator has ever opened, and none of that is the container's business.
LOGIN_FILES = {
    "claude-auth": Path(".claude") / ".credentials.json",
    "claude-config": Path(".claude.json"),
    "codex-auth": Path(".codex") / "auth.json",
}
# The operator's standing instructions to each CLI, mounted when they exist.
# A fresh home has none of them, and the first containerised round proved what
# that costs: the agent knew nothing of this operator's rules and signed its
# commit with an attribution trailer they forbid. The repository's own AGENTS.md
# and CLAUDE.md arrive with the mount; these two do not. Only the file itself
# travels: a file it includes is another path in a profile directory this
# deliberately does not mount, and the CLI reports the include as missing.
INSTRUCTION_FILES = {
    "claude-instructions": Path(".claude") / "CLAUDE.md",
    "codex-instructions": Path(".codex") / "AGENTS.md",
}
# claude-config carries account and onboarding state rather than a token, so
# there is no expiry in it to judge.
HEALTH_CHECKED = ("claude-auth", "codex-auth")
# Forwarded by name only when set on this machine, so a container never receives
# an empty variable that looks like a configured credential.
CREDENTIAL_ENVIRONMENT = ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "OPENAI_API_KEY")

# The loop announces both directories on startup; they are the run's paperwork.
# The path is taken whole rather than up to the first space, because a directory
# an operator named may contain one and half a path is worse than none.
ANNOUNCED_DIRECTORY = re.compile(r"^(reviews|logs)\s+:\s+(\S.*?)\s*$")


class Refused(RuntimeError):
    """The run must not start, for a reason worth printing on its own."""


def repository_root(start: Path) -> Path:
    """The exact top level of the work tree that will be mounted read-write.

    Not "somewhere inside a repository": the mount is the one writable thing the
    container gets, so what it covers has to be established rather than assumed
    from an argument. `git rev-parse --show-toplevel` is what decides it.
    """
    result = run_capture(["git", "-C", str(start), "rev-parse", "--show-toplevel"], 30)
    if result.returncode != 0:
        raise Refused(
            f"{start} is not inside a git work tree, and this loop's product is commits: "
            f"{result.stderr.strip() or result.stdout.strip() or 'git could not answer'}"
        )
    return Path(result.stdout.strip()).resolve()


def contains(parent: Path, child: Path) -> bool:
    """Whether `child` is `parent` or lies under it, on either platform's rules."""
    parent_key = Path(os.path.normcase(str(parent)))
    child_key = Path(os.path.normcase(str(child)))
    return child_key == parent_key or parent_key in child_key.parents


def check_repository(repository: Path, files: list[tuple[str, Path]]) -> None:
    """Refuse a mount that would carry this machine's profile into the container.

    The repository is the single read-write mount, so everything under it is
    writable from inside. A work tree rooted at the home directory -- a dotfiles
    repository is the ordinary way to have one -- would hand the container the
    stored logins themselves, writable, beside the read-only copies it is given
    on purpose. The evaluation keeps credentials outside the workspace for the
    same reason.

    Docker's --mount value is comma separated and has no escape for a comma, so
    a path holding one is refused by name here instead of turning into an opaque
    syntax error later. `evals/install/runner.py` refuses it in the same words.
    """
    home = Path.home().resolve()
    if contains(repository, home):
        raise Refused(
            f"{repository} contains this machine's profile ({home}), and it would be mounted read-write. "
            "The stored logins live in there; the container gets copies of the individual files instead. "
            "Point --repo at the project you want the loop to commit to."
        )
    for kind, path in files:
        if contains(repository, path):
            raise Refused(
                f"the {kind} file at {path} is inside {repository}, which is mounted read-write. "
                "A login the container can rewrite in place is not a login it was handed a copy of."
            )
    for label, path in (("the repository", repository), *((f"the {kind} file", path) for kind, path in files)):
        if "," in str(path):
            raise Refused(
                f"{label} path contains a comma: {path}. Docker's --mount value is comma separated and "
                "cannot carry one, and it refuses the run with a syntax error that names nothing."
            )


def inside_the_mount(option: str, value: str) -> str:
    """Where a forwarded directory lands inside the container, refusing an escape.

    The loop resolves a relative path against --repo, which is the mount, so a
    plain name is fine. An absolute path or one that climbs out lands on the
    container's own filesystem, where it is deleted with the container -- and
    this wrapper would then print a host path that nothing ever wrote.
    """
    if "\\" in value:
        raise Refused(
            f"{option} {value!r} is read inside a Linux container, where a backslash is part of the name "
            "rather than a separator. Use forward slashes."
        )
    landing = posixpath.normpath(posixpath.join(MOUNTED_REPOSITORY, value))
    if landing != MOUNTED_REPOSITORY and not landing.startswith(f"{MOUNTED_REPOSITORY}/"):
        raise Refused(
            f"{option} {value!r} lands at {landing} inside the container, outside the mounted repository, "
            "so it would be deleted with the container. Name a directory inside the repository."
        )
    return landing


def expansions(name: str, options: tuple[str, ...]) -> list[str]:
    """Which of `options` a forwarded name can reach.

    The inner loop's parser accepts any unambiguous prefix of an option, so a
    rule that reads `--codex-sandbox` and lets `--codex-sand` past reads
    nothing at all: both arrive at the same destination.
    """
    if not name.startswith("--"):
        return []
    return [option for option in options if option.startswith(name)]


def owned_options(forwarded: list[str]) -> list[str]:
    """The forwarded options that decide what the container isolates."""
    return sorted(
        {
            name
            for argument in forwarded
            if expansions(name := argument.split("=", 1)[0], CONTAINER_OWNED_OPTIONS)
        }
    )


def forwarded_paperwork(forwarded: list[str]) -> dict[str, str]:
    """Check what the caller forwards, and answer where the paperwork will land.

    Isolation is not a default here, so the options that decide it are refused
    rather than overridden: forwarding a second one is how `danger-full-access`
    stops being the value that was measured, and how the reviewer's checkout
    walks back onto the repository mount.
    """
    named = owned_options(forwarded)
    if named:
        raise Refused(
            f"{', '.join(named)} decide what the container isolates, so this wrapper sets them and does not "
            "forward them. The reviewer's checkout stays on the container's own filesystem, and codex runs "
            "with no sandbox of its own because the container is the boundary."
        )
    landing = dict(DEFAULT_PAPERWORK)
    for index, argument in enumerate(forwarded):
        name, separator, attached = argument.partition("=")
        reached = expansions(name, PAPERWORK_OPTIONS)
        if len(reached) != 1:
            # No match, or an abbreviation the inner parser will reject as
            # ambiguous; either way there is nothing here to place.
            continue
        if separator:
            landing[reached[0]] = inside_the_mount(name, attached)
        elif index + 1 < len(forwarded):
            landing[reached[0]] = inside_the_mount(name, forwarded[index + 1])
    return landing


def on_this_side(inside: str, repository: Path) -> str:
    """A path the container printed, named where this machine can open it."""
    landing = posixpath.normpath(inside)
    if landing == MOUNTED_REPOSITORY:
        return str(repository)
    if landing.startswith(f"{MOUNTED_REPOSITORY}/"):
        return str(repository / PurePosixPath(landing[len(MOUNTED_REPOSITORY) + 1 :]))
    # Not in the mount, so there is no path here to name: saying one anyway is
    # how an operator goes looking for a directory that never existed.
    return f"{inside} (inside the container only; it went with it)"


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
    try:
        completed = subprocess.run(
            [executable, "-p", "--output-format", "text", "reply with: ok"],
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        # A CLI that hangs here has not refreshed anything, and a traceback out
        # of the pre-flight says that far less clearly than the reason does.
        raise Refused(
            f"the {kind} refresh attempt did not finish within {error.timeout:.0f}s, so the login was not "
            f"refreshed ({detail}). Start Claude Code once on this machine, then rerun."
        ) from error
    except OSError as error:
        raise Refused(
            f"the {kind} refresh attempt could not start {executable}: {error}. The login was not refreshed "
            f"({detail}). Start Claude Code once on this machine, then rerun."
        ) from error
    if completed.returncode != 0:
        raise Refused(f"the {kind} refresh attempt failed: {completed.stderr.strip() or completed.stdout.strip()}")


def mounted_files() -> list[tuple[str, Path]]:
    """The individual files the container gets, named before anything is checked.

    The instruction files are optional: an operator who keeps none simply has
    none, and that is not a reason to refuse.
    """
    resolved: list[tuple[str, Path]] = []
    for kind, relative in LOGIN_FILES.items():
        path = Path.home() / relative
        if not path.is_file():
            raise Refused(f"{kind}: no stored login at {path}. Sign in with the CLI on this machine first.")
        resolved.append((kind, path.resolve()))

    for kind, relative in INSTRUCTION_FILES.items():
        path = Path.home() / relative
        if path.is_file():
            resolved.append((kind, path.resolve()))
        else:
            print(f"{kind}: none at {path}; the container gets the repository's instructions only", flush=True)
    return resolved


def check_logins(files: list[tuple[str, Path]]) -> None:
    """Decide the stored logins here, before any model is spent on a round.

    A login that cannot be used fails every round the same way, and refreshing
    one inside the container can rotate the token and leave this machine holding
    the value the provider has already replaced.
    """
    for kind, path in files:
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


def container_command(
    *,
    docker: str,
    image: str,
    container_name: str,
    home_volume: str,
    repository: Path,
    files: list[tuple[str, Path]],
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
        # init to reap them, zombies accumulate against --pids-limit. It is also
        # what makes the POSIX process-group tests pass here and fail under
        # tools/ci_linux.py, which has no reaper.
        "--init",
        # Taken unchanged, because the numbers say they can be. Measured in this
        # image with the container reporting its own cgroup peak (report_peaks
        # in the entrypoint): the suite alone 287 MiB and 37 processes, a full
        # implement-and-review round 405 MiB and 71, and 1019 MiB with 77
        # processes for the heaviest round measured since the virtualenv started
        # installing a clone of the project on the tmpfs. Half of 2g, and 77 of
        # 512: still the evaluation's limits, still with room.
        *docker_security_options(),
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
    for kind, path in files:
        command += [
            "--mount",
            docker_mount("bind", docker_source(path), f"{MOUNTED_FILES}/{kind}", readonly=True),
        ]
    for variable in CREDENTIAL_ENVIRONMENT:
        if os.environ.get(variable):
            command += ["--env", variable]
    return [
        *command,
        image,
        "--kinds",
        *[kind for kind, _path in files],
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
            announced[match.group(1)] = on_this_side(match.group(2), repository)
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
    parser.add_argument(
        "--repo",
        type=Path,
        default=None,
        help="Repository to mount and work in; its git top level is what gets mounted, read-write.",
    )
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
    image = options.image or f"{IMAGE_NAME}:{recipe_digest()}"

    try:
        # Cheapest refusals first: none of these costs a model call, and the
        # health check below can spend a small session refreshing a login.
        paperwork = forwarded_paperwork(forwarded)
        repository = repository_root(options.repo or Path(__file__).resolve().parent)
        files = mounted_files()
        check_repository(repository, files)
        check_logins(files)
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
        files=files,
        identity=identity,
        forwarded=forwarded,
    )
    print(f"repository : {repository} (read-write at {MOUNTED_REPOSITORY})")
    print(f"image      : {image}")
    print(f"identity   : {identity[0]} <{identity[1]}>")
    print(f"mounted    : {', '.join(kind for kind, _path in files)} (read-only, one file each)")
    print(f"home volume: {home_volume} (deleted when this exits)")
    print(f"$ {' '.join(command)}", flush=True)

    announced: dict[str, str] = {}
    failure: str | None = None
    remaining: list[str] = []
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
                remaining.append(f"{kind} {name}: {type(error).__name__}: {error}")

    if failure is not None and exit_code != 0:
        print(
            f"\nan agent CLI reported an authentication failure inside the container: {failure!r}\n"
            "The stored login needs to be renewed on this machine; the container cannot fix it.",
            file=sys.stderr,
        )
    print(f"\nreviews    : {announced.get('reviews', on_this_side(paperwork['--review-dir'], repository))}")
    print(f"logs       : {announced.get('logs', on_this_side(paperwork['--log-dir'], repository))}")
    print(f"commits    : in {repository}; nothing was pushed and no pull request was opened.")
    if remaining:
        # The home volume is the one place a copy of a login can outlive the run,
        # and a CLI that rewrites its login file in place turns the tmpfs symlink
        # the entrypoint made into a regular file in that volume. A run that
        # leaves it behind is not a run that succeeded, whatever the loop
        # returned, so this is never folded into the loop's own exit code.
        print(
            "\nerror: this run left Docker resources behind, and the home volume is where the logins were "
            "copied to:\n  " + "\n  ".join(remaining) + f"\nRemove them by hand. The loop itself exited {exit_code}.",
            file=sys.stderr,
        )
        return EXIT_CLEANUP_FAILED
    return exit_code


if __name__ == "__main__":  # pragma: no cover - entry point
    raise SystemExit(main())
