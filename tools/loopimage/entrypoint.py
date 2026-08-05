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

The virtualenv it builds installs a *snapshot* of the project rather than the
mount, so the one environment both agents share maps ``agentic_hil`` to neither
working tree. Each agent's tree comes first on its own ``PYTHONPATH`` instead:
the implementer's is the mount it is editing, the reviewer's is the checkout of
the commit it is reviewing.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

HOME = Path("/home/loop")
REPO = Path("/repo")
MOUNTED_FILES = Path("/run/loop-agent-files")
TEMPORARY_FILES = Path("/tmp/loop-agent-files")
# Everything scratch lives on the tmpfs, never on the repository mount. Temp
# directories the suite hardens on the mount are the whole reason this container
# exists.
SCRATCH = Path("/tmp")
REVIEW_CHECKOUT = SCRATCH / "agentic-loop-review"
# The committed tree, cloned here so that installing it writes its build
# leavings here rather than into the operator's checkout.
PROJECT_SNAPSHOT = SCRATCH / "loop-project"
VENV = HOME / ".venv"

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

# The extras the suite needs; the project's own requirements come with them.
EXTRAS = ("dev", "can")

# Inner options that decide what this container isolates rather than how the
# loop is run. tools/loop_in_container.py refuses them on the host, before any
# model is spent; this is the second half of the same rule, so a value that
# reached the loop another way cannot move the reviewer's checkout back onto the
# repository mount or hand codex a sandbox that was never measured here.
CONTAINER_OWNED_OPTIONS = ("--repo", "--codex-sandbox", "--review-checkout", "--review-checkout-dir")


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


def report(command: list[str]) -> None:
    """Print what a CLI says about itself, so a transcript records the build."""
    result = subprocess.run(command, text=True, capture_output=True, timeout=120, check=False)
    answer = (result.stdout or result.stderr).strip().splitlines()
    print(f"{command[0]}: {answer[0] if answer else f'exit {result.returncode}'}", flush=True)


def prepare_repository() -> None:
    # The mount belongs to another user id, and git refuses a repository it does
    # not recognise as its own before it will read a single ref. Both paths are
    # needed: the reviewer's checkout is made with `git clone --local`, which
    # resolves the source to its .git directory and checks the ownership of
    # that, so marking the work tree alone leaves the clone refused.
    for path in (REPO, REPO / ".git"):
        subprocess.run(["git", "config", "--global", "--add", "safe.directory", str(path)], check=True, timeout=60)


def snapshot_command() -> list[str]:
    return ["git", "clone", "--quiet", "--no-hardlinks", str(REPO), str(PROJECT_SNAPSHOT)]


def install_command() -> list[str]:
    return [str(VENV / "bin" / "pip"), "install", "--quiet", f"{PROJECT_SNAPSHOT}[{','.join(EXTRAS)}]"]


def install_project() -> None:
    """Install a snapshot of the project, and import from neither copy of it.

    Two things have to be true at once. The suite reads the real distribution
    metadata in one place -- which extras are installed -- so the project must
    be installed and not only its requirements: without it,
    `test_installed_extras_names_only_the_extras_whose_requirements_are_present`
    fails in every round for a reason no agent put there.

    And no install may map ``agentic_hil`` to a working tree, because there are
    two of them. An editable install of ``/repo`` resolves the import to the
    implementer's tree, so the reviewer -- running from a checkout pinned to the
    commit under review -- would have tested the implementer's code, across the
    bind mount and possibly mid-edit. It would also have setuptools write
    ``src/agentic_hil.egg-info`` into the mount, where nothing disposable
    belongs.

    So the install is a plain one, from a clone of the committed tree on the
    container's own filesystem: metadata and console script exist, the build
    writes its own leavings next to the clone, and the copy in site-packages is
    only ever the fallback -- each agent's tree comes first on PYTHONPATH, the
    implementer's mount for one and the reviewer's checkout for the other.
    """
    subprocess.run(snapshot_command(), check=True, timeout=600)
    subprocess.run([sys.executable, "-m", "venv", str(VENV)], check=True, timeout=300)
    subprocess.run(install_command(), check=True, timeout=1800)


def loop_command(forwarded: list[str]) -> list[str]:
    arguments = [argument for argument in forwarded if argument != "--"]
    # A prefix, not an equality: the loop's parser expands any unambiguous
    # abbreviation, so refusing `--codex-sandbox` while `--codex-sand` reaches
    # the same destination refuses nothing.
    owned = sorted(
        {
            name
            for argument in arguments
            if (name := argument.split("=", 1)[0]).startswith("--")
            for option in CONTAINER_OWNED_OPTIONS
            if option.startswith(name)
        }
    )
    if owned:
        raise SystemExit(
            f"these options decide what this container isolates and cannot be forwarded into it: {', '.join(owned)}"
        )
    return [
        str(VENV / "bin" / "python"),
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
    report(["claude", "--version"])
    report(["codex", "--version"])
    install_project()

    environment = dict(os.environ)
    # Both agents and everything they start get their scratch space on the
    # tmpfs. The reviewer's checkout is placed there explicitly below for the
    # same reason.
    environment.update({key: str(SCRATCH) for key in ("TMPDIR", "TMP", "TEMP")})
    # The implementer edits this tree, so this is the tree its tests must
    # import. The reviewer's own checkout replaces this in the environment
    # agent_review_loop.reviewer_env builds for it.
    environment["PYTHONPATH"] = str(REPO / "src")

    command = loop_command(list(args.loop_args))
    print(f"loop: {' '.join(command)}", flush=True)
    try:
        return subprocess.run(command, cwd=str(REPO), env=environment, check=False).returncode
    finally:
        report_peaks()


if __name__ == "__main__":
    raise SystemExit(main())
