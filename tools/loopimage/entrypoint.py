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


def install_project() -> None:
    """Install the mounted project the way a developer on Linux would.

    An editable install has setuptools write ``src/agentic_hil.egg-info`` into
    the mount. It is gitignored, so the loop's clean-tree check does not see it,
    and it is metadata for the source directory rather than for any environment,
    so a host virtualenv beside it keeps working.
    """
    subprocess.run([sys.executable, "-m", "venv", str(VENV)], check=True, timeout=300)
    subprocess.run(
        [str(VENV / "bin" / "pip"), "install", "--quiet", "--editable", f"{REPO}[dev,can]"],
        check=True,
        timeout=1800,
    )


def loop_command(forwarded: list[str]) -> list[str]:
    return [
        str(VENV / "bin" / "python"),
        str(REPO / "tools" / "agent_review_loop.py"),
        "--repo",
        str(REPO),
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
        # Forwarded last so a caller can override any default chosen here.
        *[argument for argument in forwarded if argument != "--"],
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

    command = loop_command(list(args.loop_args))
    print(f"loop: {' '.join(command)}", flush=True)
    try:
        return subprocess.run(command, cwd=str(REPO), env=environment, check=False).returncode
    finally:
        report_peaks()


if __name__ == "__main__":
    raise SystemExit(main())
