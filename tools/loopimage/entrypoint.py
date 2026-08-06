"""Container side of tools/loop_in_container.py: stage the logins, then loop.

This runs inside the throwaway container and never on a developer machine. It
mirrors evals/install/container_entrypoint.py, which is the tested arrangement
in this repository for handing a stored login to an agent CLI:

* each file arrives as its own read-only bind mount under
  ``/run/loop-agent-files``; the host profile directories are never mounted;
* the bytes are copied to tmpfs and the home path is a symlink to that copy, so
  the secret lives in RAM rather than in the home volume's backing store, and a
  CLI that rewrites its login file writes to a copy that dies with the
  container.

Unlike the evaluation, nothing is exported back to the host: the wrapper deletes
the home volume unconditionally, because the only product of a loop run is the
commits it made in the repository mount.

Neither agent is installed into a shared environment here. Each gets one of its
own, built from the committed tree it works in and rebuilt when that tree's
``pyproject.toml`` moves -- see ``agent_shim.py``, which stands in front of both
CLIs on PATH and does it.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

from agent_shim import REAL_CLI_DIRECTORY
from loop_options import (
    CODEX_ARGUMENT_OPTION,
    isolating_codex_arguments,
    owned_options,
    paperwork_values,
)

HOME = Path("/home/loop")
REPO = Path("/repo")
MOUNTED_FILES = Path("/run/loop-agent-files")
TEMPORARY_FILES = Path("/tmp/loop-agent-files")
# Everything scratch lives on the tmpfs, never on the repository mount. Temp
# directories the suite hardens on the mount are the whole reason this container
# exists.
SCRATCH = Path("/tmp")
REVIEW_CHECKOUT = SCRATCH / "agentic-loop-review"

# Same kind names as the evaluation where the file is the same file. The others
# are what a fresh home is missing and an agent still needs: the Claude Code
# account state, which is not a token but decides whether the CLI considers
# itself onboarded, and the operator's standing instructions to each CLI.
MOUNTED_PATHS = {
    "claude-auth": HOME / ".claude" / ".credentials.json",
    "claude-config": HOME / ".claude.json",
    "claude-instructions": HOME / ".claude" / "CLAUDE.md",
    "codex-auth": HOME / ".codex" / "auth.json",
    "codex-instructions": HOME / ".codex" / "AGENTS.md",
}

# Where the loop puts its paperwork when the caller names nothing, relative to
# --repo exactly as the loop resolves it.
DEFAULT_PAPERWORK = {"--review-dir": ".agentic-loop/reviews", "--log-dir": ".agentic-loop/logs"}

# Scratch every standard tool writes beside the code unless it is told
# otherwise. Setting TMPDIR moves what a tool asks the system for; it does not
# move these, and a round of the loop proved it -- Ruff left `.ruff_cache` on the
# repository mount, which on Windows is the NTFS filesystem whose leftover
# directories this container exists to stop inheriting.
TOOL_CACHES = {
    "RUFF_CACHE_DIR": SCRATCH / "ruff-cache",
    "MYPY_CACHE_DIR": SCRATCH / "mypy-cache",
    "COVERAGE_FILE": SCRATCH / "coverage" / ".coverage",
}
# pytest's cache has no environment variable of its own; the ini option it does
# have can be set for every invocation this way, including the ones an agent
# starts for itself.
PYTEST_CACHE = SCRATCH / "pytest-cache"


def place_mounted_files(kinds: list[str]) -> None:
    for kind in kinds:
        source = MOUNTED_FILES / kind
        if not source.is_file():
            raise FileNotFoundError(f"mount missing: {source}")
        temporary = TEMPORARY_FILES / kind
        temporary.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        shutil.copyfile(source, temporary)
        temporary.chmod(0o600)
        target = MOUNTED_PATHS[kind]
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if target.exists() or target.is_symlink():
            raise FileExistsError(f"destination already exists: {target}")
        target.symlink_to(temporary)


def cgroup_peak(name: str) -> int | None:
    try:
        return int((Path("/sys/fs/cgroup") / name).read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def report_peaks() -> None:
    """What the run actually cost.

    cgroup v2 keeps a high-water mark for the container's whole lifetime.
    Printing it on every run is what keeps the --memory and --pids-limit in
    tools/loop_in_container.py a measurement rather than a guess nobody
    revisits: raising either is a deviation from the evaluation's baseline, and
    a deviation should have a number behind it.
    """
    memory = cgroup_peak("memory.peak")
    processes = cgroup_peak("pids.peak")
    if memory is not None:
        print(f"peak memory: {memory / 1024 ** 2:.0f} MiB", flush=True)
    if processes is not None:
        print(f"peak processes: {processes}", flush=True)


def report(name: str) -> None:
    """Print what a CLI says about itself, so a transcript records the build.

    The real executable, not the name: the shim in front of it on PATH would
    build an agent's whole environment to answer a version question.
    """
    command = [str(REAL_CLI_DIRECTORY / name), "--version"]
    result = subprocess.run(command, text=True, capture_output=True, timeout=120, check=False)
    answer = (result.stdout or result.stderr).strip().splitlines()
    print(f"{name}: {answer[0] if answer else f'exit {result.returncode}'}", flush=True)


def prepare_repository() -> None:
    # The mount belongs to another user id, and git refuses a repository it does
    # not recognise as its own before it will read a single ref. Both paths are
    # needed: the reviewer's checkout is made with `git clone --local`, which
    # resolves the source to its .git directory and checks the ownership of
    # that, so marking the work tree alone leaves the clone refused.
    for path in (REPO, REPO / ".git"):
        subprocess.run(["git", "config", "--global", "--add", "safe.directory", str(path)], check=True, timeout=60)


def paperwork_directories(arguments: list[str]) -> dict[str, Path]:
    """Where this run's review documents and transcripts will actually land.

    A relative value is resolved against --repo, which is what the loop itself
    does with it.
    """
    chosen = {**DEFAULT_PAPERWORK, **paperwork_values(arguments)}
    return {option: Path(value) if Path(value).is_absolute() else REPO / value for option, value in chosen.items()}


def check_paperwork(arguments: list[str]) -> None:
    """Refuse a run whose only lasting product would die with the container.

    The host checks these paths as text, and text cannot see a symlink: an
    ``.agentic-loop`` pointing at /tmp passes every spelling rule, takes the
    reviews and the transcripts with the container, and leaves the wrapper
    naming a directory on the host that received nothing. In here the components
    exist, so resolving them answers the question instead of guessing at it.
    """
    mount = REPO.resolve()
    for option, path in sorted(paperwork_directories(arguments).items()):
        landing = path.resolve()
        if landing != mount and mount not in landing.parents:
            raise SystemExit(
                f"{option} lands at {landing}, outside the repository mounted at {mount}: this run's only "
                "lasting product would be deleted with the container. Name a directory inside the repository, "
                "and check whether one of its components is a symlink out of it."
            )


def loop_command(forwarded: list[str]) -> list[str]:
    arguments = [argument for argument in forwarded if argument != "--"]
    owned = owned_options(arguments)
    if owned:
        raise SystemExit(
            f"these options decide what this container isolates and cannot be forwarded into it: {', '.join(owned)}"
        )
    # The option is forwarded; these payloads of it are not. The loop appends
    # them after its own Codex arguments, and Codex takes the last occurrence --
    # so `--codex-arg=--cd=/repo` is the reviewer working in the implementer's
    # mount, whatever the loop asked for.
    isolating = isolating_codex_arguments(arguments)
    if isolating:
        raise SystemExit(
            f"these {CODEX_ARGUMENT_OPTION} payloads would move the reviewer out of its own checkout or replace "
            f"the sandbox this container settled on: {', '.join(isolating)}"
        )
    check_paperwork(arguments)
    return [
        # The loop driver itself needs nothing but the standard library, and the
        # environments the two agents run in are built per round by agent_shim.
        sys.executable,
        str(REPO / "tools" / "agent_review_loop.py"),
        *arguments,
        # Last, and after everything forwarded, because argparse takes the final
        # occurrence: an isolation option cannot be weakened by putting another
        # copy of it in front of these.
        "--repo",
        str(REPO),
        # Named rather than left to the default, so the value that survives an
        # abbreviation forwarded from outside is this one. 'none' would review
        # in the implementer's tree, which is the repository mount.
        "--review-checkout",
        "clone",
        # The container is the isolation boundary, so Codex needs no second one
        # inside it. Its own sandbox is what could not run this suite on the
        # host at all, and the blast radius of turning it off here is a
        # throwaway container holding one repository mount.
        "--codex-sandbox",
        "danger-full-access",
        # Committed work is the product; everything scratch stays on the tmpfs
        # and never reaches the repository mount.
        "--review-checkout-dir",
        str(REVIEW_CHECKOUT),
    ]


def loop_environment() -> dict[str, str]:
    environment = dict(os.environ)
    # Both agents and everything they start get their scratch space on the
    # tmpfs. The reviewer's checkout is placed there explicitly for the same
    # reason, and so are the tool caches below -- TMPDIR does not move those.
    environment.update({key: str(SCRATCH) for key in ("TMPDIR", "TMP", "TEMP")})
    environment.update({key: str(path) for key, path in TOOL_CACHES.items()})
    addopts = environment.get("PYTEST_ADDOPTS", "").strip()
    environment["PYTEST_ADDOPTS"] = f"{addopts} -o cache_dir={PYTEST_CACHE}".strip()
    # The implementer edits this tree, so this is the tree its tests must
    # import. The reviewer's own checkout replaces this in the environment
    # agent_review_loop.agent_env builds for it. That function also replaces
    # TMPDIR, TMP and TEMP with a directory of its own for each agent in each
    # round; these values are what everything before the first round uses.
    environment["PYTHONPATH"] = str(REPO / "src")
    return environment


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kinds", nargs="*", default=[])
    parser.add_argument("loop_args", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)

    if os.geteuid() == 0:
        raise RuntimeError("the loop container must not run as root")
    if not (REPO / ".git").exists():
        raise RuntimeError(f"no repository is mounted at {REPO}")

    place_mounted_files(list(args.kinds))
    prepare_repository()
    report("claude")
    report("codex")

    command = loop_command(list(args.loop_args))
    print(f"loop: {' '.join(command)}", flush=True)
    try:
        return subprocess.run(command, cwd=str(REPO), env=loop_environment(), check=False).returncode
    finally:
        report_peaks()


if __name__ == "__main__":
    raise SystemExit(main())
