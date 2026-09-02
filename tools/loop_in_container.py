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
mounts -- never a profile directory.

A rotated token is not merely stale, though, which is what issue #391 cost an
operator: a refresh token is single use, so once the copy inside the container
has spent it, this machine holds a value the provider has already replaced and
no later run can log in at all. Both halves of that are answered here. The
Codex login's expiry is read out of the access token it stores rather than
reported as unknown, so a refresh that is due happens on this machine like the
Claude one; and because both CLIs refresh again inside a run that outlasts an
access token, both containerised copies are taken back, at the end of each
invocation from the container that is still running and at the end of the run
from the home volume the container stages them into. Neither is a way for the
container to write here: `docker cp` and a read-only volume mount are how the
file is read, and `apply_refreshed_login` refuses a document that is not the
login it replaces and keeps the previous one beside it.

The operator's standing instructions to each CLI travel the same way, when they
exist. A fresh home has none, and a fresh home is what the first containerised
round ran in: the agent had never been told this operator's rules and signed its
commit with an attribution trailer they forbid.

The home the container builds from those files is a Docker volume that is
deleted unconditionally afterwards, once the logins staged in it have been read
back. The evaluation scrubs instead, because its evidence is inspected later;
here the only products are the commits in the repository mount and those two
files, so the home outlives the container by the length of a single read and no
longer.

One containerised run at a time per machine, and the lock that decides it is the
one `tools/ci_linux.py` takes: `run_lock.py` beside this file, on
`~/.agentic-hil/ci-linux.lock`. A review loop is a container on the same daemon
as a full-suite run, and several of those starve each other until one dies
mid-flight, so this run queues behind whoever holds the machine and holds it
itself for exactly as long as its own container is up -- and, in the one ending
where the container cannot be confirmed removed, until an operator stops it, for
the machine is still in use however that run exited. A loop is the long end of
that queue: it runs for hours where a suite run is minutes, so its record says
so, and whoever waits for it is told what it is waiting for rather than left to
guess. ``--no-wait`` refuses instead of queueing.

Usage:

    python tools/loop_in_container.py --task "..." --max-rounds 3
    python tools/loop_in_container.py --start-with review --codex-model gpt-5.1-codex
    python tools/loop_in_container.py --rebuild --task "..."
    python tools/loop_in_container.py --no-wait --task "..."

Exit codes:

    0-19  the loop's own, passed through exactly as `agent_review_loop.py`
          returned them: 0 clean, 1 rounds exhausted, 2 stalled, 3 failed,
          4 no progress.
    20    this wrapper failed and there is no review to report -- no Docker, a
          refused pre-flight, an interrupted run, or a container or volume it
          could not remove. Which one is a sentence on stderr.

`review loop exit code: N` goes to stderr whenever the loop ran at all, so the
review's own result is still readable when a cleanup failure has taken the exit
status. A caller that only wants the verdict can read the status alone: nothing
below 20 is ever this wrapper's.

Every option this wrapper does not consume is handed to `agent_review_loop.py`
unchanged, except the four that decide what the container isolates -- `--repo`,
`--codex-sandbox`, `--review-checkout` and `--review-checkout-dir`. Those are
set here and refused rather than forwarded: a second `--codex-sandbox` is how
the value that was measured stops being the value that runs, and a reviewer
checkout pointed back at the repository mount is the Windows failure this
wrapper exists to bury. `--codex-arg` is forwarded, because it is how a caller
reaches a Codex flag the loop does not model, but the payloads that reach the
same four decisions from inside it are refused by name; `tools/loopimage/
loop_options.py` is where both halves read that rule from.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import posixpath
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from collections.abc import Callable
from pathlib import Path, PurePosixPath

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
CONTAINER_DIRECTORY = REPOSITORY_ROOT / "tools" / "loopimage"
# evals/ is a source tree rather than an installed package, and the container's
# own modules are not on the path of a script run as
# "python tools/loop_in_container.py" either. The third is this file's own
# directory, where the run lock it shares with tools/ci_linux.py lives: running
# the file puts it there first anyway, and naming it is what keeps the import
# working when something reaches this module by another route.
sys.path.insert(0, str(REPOSITORY_ROOT))
sys.path.insert(0, str(CONTAINER_DIRECTORY))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from loop_options import (  # noqa: E402
    CODEX_ARGUMENT_OPTION,
    isolating_codex_arguments,
    owned_options,
    paperwork_values,
)
from run_lock import RunLock, RunLockBusy  # noqa: E402

from evals.install.credentials import (  # noqa: E402
    authentication_failure,
    credential_health,
    spent_refresh_token,
)
from evals.install.refresh_login import apply_refreshed_login  # noqa: E402
from evals.install.runner import (  # noqa: E402
    docker_mount,
    docker_security_options,
    docker_subprocess_environment,
    remove_container,
    remove_volume,
    run_capture,
)

# Everything the image is built from. The tag carries their digest, so editing
# the Dockerfile or any module that goes into the image produces a tag that does
# not exist yet and gets built, instead of a silent run against yesterday's
# recipe.
RECIPE = (
    "Dockerfile",
    "entrypoint.py",
    "agent_shim.py",
    "loop_options.py",
    "package.json",
    "package-lock.json",
)
IMAGE_NAME = "agentic-hil-loop"

MOUNTED_REPOSITORY = "/repo"
CONTAINER_HOME = "/home/loop"
MOUNTED_FILES = "/run/loop-agent-files"

# Everything below 20 is the inner loop's own code, passed through exactly as
# `agent_review_loop.py` produced it. 20 and up belong to this wrapper, and it
# uses one: no Docker, a refused pre-flight, an interrupted run and a container
# it could not remove are all "the wrapper never got a review out of this", and
# which of them it was is already a sentence on stderr. Numbering them instead
# would put three more values next to the loop's four and leave the next wrapper
# failure to be numbered by whoever adds it -- which is how EXIT_CLEANUP_FAILED
# came to be a third meaning for 4 rather than a new one.
EXIT_WRAPPER_FAILED = 20

# What this run writes into the shared lock record about itself. A run queued
# behind this one is deciding whether to wait, and a review loop is the one
# holder on this machine that can legitimately keep it waiting for hours: the
# default is three rounds of two agents, each of them building an environment
# and running the suite. Naming the scale in the record rather than in the run
# that reads it is what makes the queue message true, because the waiting run
# cannot know what the container it is waiting for is doing.
TOOL_NAME = "tools/loop_in_container.py"
RUNS_FOR = "hours"

# The loop's own exit code, on stderr, whenever the loop ran at all. A run whose
# home volume outlived it is not a successful run, whatever the loop inside it
# returned -- that volume is the one place a copy of a login can be -- so the
# wrapper's code has to replace it as the exit status. This line is how the
# review's own result survives that, and how a caller tells it from a number
# this wrapper made up.
INNER_EXIT = "review loop exit code: {code}"

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
# What comes back, and where the container keeps it. A refresh token is single
# use, so the copy that refreshes first is the only one that can refresh again:
# a run that lets its container refresh and then throws that container away
# leaves this machine holding a value the provider has already replaced, and
# every later run inherits it.
#
# Both OAuth logins reach that state here, and for the same reason: a run is
# hours and an access token is not, so the refresh made below before the run is
# not the last one. Codex makes another on its way into every session it starts
# in there; a Claude Code access token lasts about an hour, which the pre-flight
# says out loud when it prints `valid until` an hour ahead, so the implementer
# rounds refresh in there too. Returning only Codex left the Claude refresh
# token this machine holds spent by a run that reported taking a login back, and
# every Claude Code session on this machine reads that one file.
#
# tools/loopimage/entrypoint.py places each file at these paths and stages a copy
# of it in the home volume before it exits; both halves read that arrangement
# from the constants here and there, and a test holds them to each other.
RETURNED = ("claude-auth", "codex-auth")
CONTAINER_LOGIN_PATHS = {
    "claude-auth": f"{CONTAINER_HOME}/.claude/.credentials.json",
    "codex-auth": f"{CONTAINER_HOME}/.codex/auth.json",
}
# Which CLI can have rotated which login, so the end of an invocation reads back
# the file that invocation could have spent and no other. Every kind is read
# again from the home volume at the end of the run whatever happens; this is
# what bounds the cost of a run that never reaches its own ending to the round
# that was in flight. The mapping is by CLI rather than by role, because which
# of the two implements and which reviews is the caller's to choose and neither
# choice changes whose token a CLI refreshes.
AGENT_LOGINS = {"claude": ("claude-auth",), "codex": ("codex-auth",)}

# The smallest session Codex has: outside a repository, persisting nothing of
# its own, and without the operator's config.toml, whose model, provider and
# hook settings are all ways for this probe to fail for a reason that is not the
# login. Leaving that file out is safe here in its own words: it does not load
# $CODEX_HOME/config.toml, and auth still uses $CODEX_HOME.
CODEX_SESSION = (
    "--skip-git-repo-check",
    "--ephemeral",
    "--ignore-user-config",
    "--color",
    "never",
    "--sandbox",
    "read-only",
)

# How each login is refreshed on this machine: the CLI to start, what to call it
# when telling an operator to start it themselves, and the smallest session that
# makes the CLI do it.
HOST_REFRESH = {
    "claude-auth": ("claude", "Claude Code", ["-p", "--output-format", "text", "reply with: ok"]),
    "codex-auth": ("codex", "Codex", ["exec", *CODEX_SESSION, "reply with: ok"]),
}
# Forwarded by name only when set on this machine, so a container never receives
# an empty variable that looks like a configured credential.
CREDENTIAL_ENVIRONMENT = ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "OPENAI_API_KEY")

# The loop announces both directories on startup; they are the run's paperwork.
# The path is taken whole rather than up to the first space, because a directory
# an operator named may contain one and half a path is worse than none.
ANNOUNCED_DIRECTORY = re.compile(r"^(reviews|logs)\s+:\s+(\S.*?)\s*$")

# Every line the loop prints on behalf of an agent carries that agent's round
# prefix. Anything without one -- this wrapper's own output, the entrypoint's
# echo of the command, the loop's summary -- is not an agent saying anything.
AGENT_LINE = re.compile(r"^(?P<agent>\[(?P<cli>claude|codex)\s+r\d+\])\s(?P<rest>.*)$")
# The loop prints this for every agent invocation, failed or not, and it is what
# ties a phrase in the output to an invocation that actually could not run.
AGENT_EXIT = re.compile(r"^exit (?P<code>\d+) after ")


class Refused(RuntimeError):
    """The run must not start, for a reason worth printing on its own."""


def announce(text: str) -> None:
    """Say something in this wrapper's own voice, on the stream reserved for it.

    stderr, because stdout carries the container's output verbatim and a line
    this script wrote about itself must stay tellable from a line an agent wrote.
    """
    print(f"loop_in_container: {text}", file=sys.stderr, flush=True)


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

    This is the spelling half of the question, and it is all a host can answer:
    whether a component of the path is a symlink out of the mount is decided in
    the container, by `check_paperwork` in tools/loopimage/entrypoint.py.
    """
    if "\\" in value:
        raise Refused(
            f"{option} {value!r} is read inside a Linux container, where a backslash is part of the name "
            "rather than a separator. Use forward slashes."
        )
    # A drive letter is not a separator there either: `C:/reviews` would join
    # into `/repo/C:/reviews`, which Docker cannot even mount, and which this
    # wrapper would report back to the operator as `C:\reviews`.
    if re.match(r"^[A-Za-z]:", value):
        raise Refused(
            f"{option} {value!r} names a Windows drive, and the loop reads it inside a Linux container where "
            "there are none. Name a directory inside the repository instead."
        )
    landing = posixpath.normpath(posixpath.join(MOUNTED_REPOSITORY, value))
    if landing != MOUNTED_REPOSITORY and not landing.startswith(f"{MOUNTED_REPOSITORY}/"):
        raise Refused(
            f"{option} {value!r} lands at {landing} inside the container, outside the mounted repository, "
            "so it would be deleted with the container. Name a directory inside the repository."
        )
    return landing


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
    # The option itself is forwarded -- it is how a caller reaches a Codex flag
    # the loop does not model -- but not the payloads that decide where the
    # reviewer works. The loop appends them after its own Codex arguments and
    # Codex takes the last occurrence, so `--codex-arg=--cd=/repo` is the
    # reviewer in the implementer's mount however the loop was configured.
    isolating = isolating_codex_arguments(forwarded)
    if isolating:
        raise Refused(
            f"{', '.join(isolating)} passed through {CODEX_ARGUMENT_OPTION} would move the reviewer out of its "
            "own checkout or replace the sandbox this container settled on. Codex reads the last one it is "
            "given, so these arrive after the loop's own."
        )
    landing = dict(DEFAULT_PAPERWORK)
    for option, value in paperwork_values(forwarded).items():
        landing[option] = inside_the_mount(option, value)
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

    Starting the CLI is what refreshes it. Neither CLI has a command that only
    refreshes: `codex login` offers `status` and nothing else, and its `status`
    reads the stored file without asking the provider anything, so a login whose
    refresh token another copy has already spent still reports itself logged in.
    So this spends one very small session per login to have the refresh happen
    here, where the rotated value survives the run, and to find out here rather
    than three quarters of an hour into a review whether it can happen at all.
    """
    started = HOST_REFRESH.get(kind)
    if started is None:
        raise Refused(
            f"the {kind} login needs refreshing here first ({detail}), and this wrapper knows no "
            "host-side refresh for it. Start its CLI once on this machine, then rerun."
        )
    name, product, arguments = started
    executable = shutil.which(name)
    if executable is None:
        raise Refused(
            f"the {kind} login needs refreshing ({detail}) and {name} is not on PATH here. "
            "Refreshing it inside the container can rotate the token and leave this machine's "
            f"copy rejected. Start {product} once on this machine, then rerun."
        )
    print(f"{kind}: {detail}; starting {product} here once so the refreshed token stays on this machine", flush=True)
    try:
        completed = subprocess.run(
            [executable, *arguments],
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
            f"refreshed ({detail}). Start {product} once on this machine, then rerun."
        ) from error
    except OSError as error:
        raise Refused(
            f"the {kind} refresh attempt could not start {executable}: {error}. The login was not refreshed "
            f"({detail}). Start {product} once on this machine, then rerun."
        ) from error
    if completed.returncode != 0:
        spent = spent_refresh_token(f"{completed.stdout}\n{completed.stderr}")
        if spent is not None:
            # The state a run of this loop leaves behind when it has refreshed
            # inside the container and thrown the container away, and the reason
            # this check exists at all: the file looks like a login, `codex
            # login status` calls it one, and nothing on this machine can turn
            # it back into one. The CLI's own line goes with it, because that
            # line is the only place the answer was ever stated.
            raise Refused(
                f"the {kind} refresh token stored here has already been used, so no round could authenticate "
                f"and nothing on this machine can refresh it. {name} said: {spent}. Sign in again with "
                f"{product}, then rerun."
            )
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


def finished_agent(line: str) -> str | None:
    """Which CLI this line reports the end of an invocation of, if it is one.

    The loop prints one of these for every invocation, failed or not, and for
    the reviewer it is also the end of a round: the implementer of the next
    round has not started, and nothing else in the container is holding the
    login. It is the last moment in a round at which what that round refreshed
    to can still be read out of a container that is running.
    """
    match = AGENT_LINE.match(line)
    if match is None or AGENT_EXIT.match(match.group("rest")) is None:
        return None
    return match.group("cli")


def write_back(kind: str, host_path: Path, content: str | None) -> str | None:
    """Put a login the container refreshed back where this machine keeps it.

    `apply_refreshed_login` is the evaluation's, and it decides the same three
    things here: a document that is not the login it replaces is rejected rather
    than written, the previous file is kept beside the new one, and the
    replacement is a rename rather than a truncate-and-write. Nothing is said
    about a file that has not moved, because a run where no refresh happened is
    the ordinary run and has nothing to report.

    Returns the sentence to print, or None when there is nothing to say. Never
    raises: this is called from the cleanup of a run that may already be
    failing, and a login that could not be written back is a line in the report
    rather than a second failure on top of the first.
    """
    if content is None or not content.strip():
        return None
    try:
        outcome = apply_refreshed_login(kind, host_path, content)
    except OSError as error:
        return f"{kind}: could not be written back to {host_path}: {type(error).__name__}: {error}"
    return None if outcome == "unchanged" else f"{kind}: {outcome}"


def returned_login(docker: str, container_name: str, kind: str) -> str | None:
    """Read a login out of the container that is running, without asking it to help.

    `docker cp` reaches into the container from this side, so nothing inside has
    to cooperate and nothing has to be writable from in there for this to work.
    The link is followed on purpose: the entrypoint puts a symlink at that path
    and a CLI that refreshes either writes through it or replaces it, and this
    has to answer the same in both cases.

    None whenever the copy did not happen, which includes the ordinary case of a
    container that has already exited: the file lives on a tmpfs that goes with
    it, and what a finished run left behind is read out of the home volume
    instead.
    """
    with tempfile.TemporaryDirectory(prefix="agentic-hil-loop-") as directory:
        landing = Path(directory) / kind
        source = f"{container_name}:{CONTAINER_LOGIN_PATHS[kind]}"
        try:
            result = run_capture([docker, "cp", "--follow-link", source, str(landing)], 60)
        except (OSError, subprocess.SubprocessError):
            return None
        if result.returncode != 0 or not landing.is_file():
            return None
        return landing.read_text(encoding="utf-8", errors="replace")


def collect_from_container(
    docker: str, container_name: str, files: list[tuple[str, Path]], kinds: tuple[str, ...] = RETURNED
) -> list[str]:
    """Take back what the container holds now, while it is still running.

    `kinds` narrows that to the logins the invocation that just ended could have
    rotated; the default is every login that travels back at all.
    """
    lines = []
    for kind, path in files:
        if kind not in RETURNED or kind not in kinds:
            continue
        said = write_back(kind, path, returned_login(docker, container_name, kind))
        if said is not None:
            lines.append(said)
    return lines


def staged_login_command(
    *, docker: str, image: str, container_name: str, home_volume: str, kinds: list[str]
) -> list[str]:
    """Read what the run staged, out of the home volume, with no network and no writing.

    A second container because a Docker volume is not a directory this machine
    can open, and the loop's own container is gone by the time this runs. The
    same arrangement `evals/install/runner.py` reads a refreshed login back
    with, for the same reason.
    """
    return [
        docker,
        "run",
        "--rm",
        "--name",
        container_name,
        *docker_security_options(),
        "--network",
        "none",
        "--mount",
        docker_mount("volume", home_volume, CONTAINER_HOME, readonly=True),
        image,
        "--dump-logins",
        "--kinds",
        *kinds,
    ]


def collect_from_volume(
    *, docker: str, image: str, container_name: str, home_volume: str, files: list[tuple[str, Path]]
) -> list[str]:
    """Take back what the run left staged, however the run ended."""
    wanted = [(kind, path) for kind, path in files if kind in RETURNED]
    if not wanted:
        return []
    command = staged_login_command(
        docker=docker,
        image=image,
        container_name=container_name,
        home_volume=home_volume,
        kinds=[kind for kind, _path in wanted],
    )
    try:
        result = run_capture(command, 120)
    except (OSError, subprocess.SubprocessError) as error:
        return [f"the logins staged in {home_volume} could not be read: {type(error).__name__}: {error}"]
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        return [f"the logins staged in {home_volume} could not be read: {detail}"]
    try:
        staged = json.loads(result.stdout or "{}")
    except ValueError:
        return [f"the logins staged in {home_volume} came back as something other than JSON"]
    lines = []
    for kind, path in wanted:
        content = staged.get(kind) if isinstance(staged, dict) else None
        said = write_back(kind, path, content if isinstance(content, str) else None)
        if said is not None:
            lines.append(said)
    return lines


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
        # processes for the heaviest round measured while the virtualenv was
        # still a clone of the project on the tmpfs. Since each agent builds its
        # own environment on the home volume instead, an implementer round that
        # built one and ran ruff and pytest in it peaked at 755 MiB and 35
        # processes. That last figure covers one agent: the reviewer's own
        # environment has not been measured yet, because the run that would have
        # built it was refused for quota. The two are built one after the other,
        # so it is a peak rather than a sum -- but it is the number to watch.
        # Half of 2g, and 77 of 512: still the evaluation's limits, with room.
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


def credential_evidence(line: str, pending: dict[str, str]) -> str | None:
    """Attribute a login-refusal phrase to the agent invocation that then failed.

    The phrases are the ones evals/install/credentials.py catalogues, and they
    are ordinary English: a run whose task is "handle unauthorized errors" has
    the word in the echoed prompt, in the model's own prose, and in the diff it
    writes. Reading them off the combined stream is how an operator gets told to
    renew a credential that was never the problem.

    So a phrase is only evidence when the agent it came from went on to exit
    non-zero, and the line it came from is the agent's output rather than the
    printed command line. Two gaps are left open on purpose: an agent that fails
    to start at all prints no exit line, and a reviewer discussing the word while
    failing for an unrelated reason still counts. Both cost a misleading hint
    rather than a wrong exit code.
    """
    match = AGENT_LINE.match(line)
    if match is None:
        return None
    agent, rest = match.group("agent"), match.group("rest")
    if rest.startswith("$ "):
        return None  # the command line the loop echoes: argv, not output
    exited = AGENT_EXIT.match(rest)
    if exited is None:
        if agent not in pending and authentication_failure(rest) is not None:
            # The whole line, not the substring authentication_failure() matched.
            # The real Codex reuse line leads with the generic "Failed to refresh
            # token" wording and only names reuse further along it, so the matched
            # substring is that generic prefix. main() reads this value back
            # through spent_refresh_token() to tell a spent credential from a
            # transient outage; keep the line whole so the reuse wording that
            # check needs -- and the provider's own instruction with it -- is
            # still there rather than truncated to the prefix a timeout shares.
            pending[agent] = rest.strip()
        return None
    phrase = pending.pop(agent, None)
    return phrase if exited.group("code") != "0" else None


def stream(
    command: list[str],
    repository: Path,
    agent_finished: Callable[[str], None] | None = None,
) -> tuple[int, dict[str, str], str | None]:
    """Run the container, printing its output as it arrives.

    Returns the exit code, the loop's own directories translated back to this
    side of the mount, and the first phrase that looks like an agent CLI
    refusing a login -- which is the failure worth naming rather than debugging.

    `agent_finished` is called with the name of each agent CLI as its invocation
    ends. That is the only place a caller can act on a round that has finished
    while the container it finished in is still there to be read.
    """
    announced: dict[str, str] = {}
    pending: dict[str, str] = {}
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
        evidence = credential_evidence(line, pending)
        if failure is None:
            failure = evidence
        agent = finished_agent(line)
        if agent is not None and agent_finished is not None:
            agent_finished(agent)
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
    parser.add_argument(
        "--no-wait",
        action="store_true",
        help="Refuse instead of queueing when another containerised run holds this machine.",
    )
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
        return EXIT_WRAPPER_FAILED

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
        return EXIT_WRAPPER_FAILED

    # Taken after the pre-flight, which can prompt for a login and spend a small
    # session refreshing one, and given back only once the container and its home
    # volume are gone: the window the lock covers is exactly the window this run
    # has something of its own on the daemon. It is the lock tools/ci_linux.py
    # takes, on the same file, because a suite container and a loop container
    # starve each other exactly as two suite containers do.
    lock = RunLock(
        tool=TOOL_NAME,
        runs_for=RUNS_FOR,
        root=repository,
        details={"image": image, "loop_args": forwarded},
        announce=announce,
    )
    try:
        lock.acquire(wait=not options.no_wait)
    except RunLockBusy as busy:
        # The wrapper's one code, and a sentence saying which of its failures
        # this was: a refusal here is "there is no review to report", exactly
        # like a refused pre-flight, and numbering it separately would put
        # another value next to the loop's own four.
        announce(f"{busy}. Refusing, because --no-wait was given")
        return EXIT_WRAPPER_FAILED
    except OSError as error:
        announce(f"the lock at {lock.path} could not be taken: {error}")
        return EXIT_WRAPPER_FAILED
    # Set true only if the container could not be confirmed removed. The lock is
    # released at the end for every other ending, but not that one: a container
    # still on the daemon is the machine still in use, and handing it to the next
    # run is the starvation the lock exists to prevent. Bound here so the outer
    # finally can read it however the run ends, including a volume-create failure
    # that returns before the container is ever started.
    container_unresolved = False
    try:
        token = uuid.uuid4().hex[:12]
        container_name = f"agentic-hil-loop-{token}"
        home_volume = f"agentic-hil-loop-{token}-home"
        result = run_capture([docker, "volume", "create", "--label", "agentic-hil.loop=true", home_volume], 60)
        if result.returncode != 0:
            print(f"error: could not create the home volume: {result.stderr.strip()}", file=sys.stderr)
            return EXIT_WRAPPER_FAILED

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
        # None until the loop returns a code of its own. An interrupt leaves it that
        # way, and a run that never reached a verdict has no verdict to report.
        inner: int | None = None

        def agent_finished(agent: str) -> None:
            # Both CLIs, each for its own login: an invocation that has ended is
            # an invocation whose session refreshed on its way in and is holding
            # nothing now, so this is the moment its file can be read out of a
            # container that is still running. Taking it back here bounds what a
            # run that never reaches its own ending can cost this machine to the
            # one round that was in flight.
            for line in collect_from_container(docker, container_name, files, AGENT_LOGINS.get(agent, ())):
                announce(line)

        try:
            inner, announced, failure = stream(command, repository, agent_finished)
        except KeyboardInterrupt:
            print("\ninterrupted; stopping the container.", file=sys.stderr)
        finally:
            # Before the volume goes, and inside the same cleanup the lock
            # release covers, so a run that failed, was refused mid-flight or was
            # interrupted hands this machine the same login a clean run does.
            for line in collect_from_volume(
                docker=docker,
                image=image,
                container_name=f"{container_name}-logins",
                home_volume=home_volume,
                files=files,
            ):
                announce(line)
            for remove, kind, name in ((remove_container, "container", container_name), (remove_volume, "volume", home_volume)):
                try:
                    remove(docker, name)
                except (OSError, RuntimeError, subprocess.SubprocessError) as error:
                    remaining.append(f"{kind} {name}: {type(error).__name__}: {error}")
                    # The container is the resource whose survival keeps the
                    # machine in use; a leftover volume holds a login copy but no
                    # daemon capacity. Only the first decides whether the lock may
                    # be released. `remove_container` raises when the container
                    # still exists after removal or Docker cannot confirm it gone.
                    if remove is remove_container:
                        container_unresolved = True

        if failure is not None and inner not in (None, 0):
            cause = (
                # Not "renew it here": a spent refresh token is what this run's
                # own copy leaves behind, so telling an operator the stored login
                # went stale would name the state and miss the cause.
                "A refresh token is single use, so the copy this run took in is what spent the one this "
                "machine held. Whatever the container refreshed to has been written back above, if it could "
                "be read; if nothing was, sign in again here."
                if spent_refresh_token(failure) is not None
                else "The stored login needs to be renewed on this machine; the container cannot fix it."
            )
            print(
                f"\nan agent CLI reported an authentication failure inside the container: {failure!r}\n{cause}",
                file=sys.stderr,
            )
        print(f"\nreviews    : {announced.get('reviews', on_this_side(paperwork['--review-dir'], repository))}")
        print(f"logs       : {announced.get('logs', on_this_side(paperwork['--log-dir'], repository))}")
        print(f"commits    : in {repository}; nothing was pushed and no pull request was opened.")
        if inner is not None:
            print(INNER_EXIT.format(code=inner), file=sys.stderr)
        if remaining:
            # The home volume is the one place a copy of a login can outlive the run,
            # and a CLI that rewrites its login file in place turns the tmpfs symlink
            # the entrypoint made into a regular file in that volume. A run that
            # leaves it behind is not a run that succeeded, whatever the loop
            # returned, so this is never folded into the loop's own exit code -- and
            # the loop's code is on stderr above rather than lost to this one.
            note = (
                "\nerror: this run left Docker resources behind, and the home volume is where the logins were "
                "copied to:\n  " + "\n  ".join(remaining) + "\nRemove them by hand."
            )
            if container_unresolved:
                # The container could still be on the daemon, so the machine is
                # still in use: this run keeps its lock rather than releasing it
                # onto the next run, and keeps it in a state that run will not
                # break on liveness. See the outer finally and RunLock.retain_for_cleanup.
                note += (
                    f"\nA container could not be confirmed removed, so this run keeps the lock at {lock.path} "
                    "rather than handing the machine to the next run onto a daemon the container is still on. "
                    "Stop the container, then remove that file by hand."
                )
            print(note, file=sys.stderr)
            return EXIT_WRAPPER_FAILED
        if inner is None:
            return EXIT_WRAPPER_FAILED
        return inner
    finally:
        # The machine goes back to the queue for every ending but one: a container
        # that could not be confirmed removed is the machine still in use, and
        # releasing the lock there starts the next run's container beside it. That
        # exit keeps the lock instead, in a cleanup-required state the next run
        # will not break on liveness; every other exit releases it as before.
        if container_unresolved:
            lock.retain_for_cleanup(
                "tools/loop_in_container.py exited with a container it could not confirm removed: "
                + "; ".join(remaining)
            )
        else:
            lock.release()


if __name__ == "__main__":  # pragma: no cover - entry point
    raise SystemExit(main())
