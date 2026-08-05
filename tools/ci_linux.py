"""Run the test suite the way the Linux CI runners do, in a container.

A Windows developer cannot run the POSIX half of this suite, and the half that
does not run is exactly the half that breaks: three times in two days a change
was verified green on Windows and failed on all four POSIX runners -- a test
that assumed a debugger was installed, two that wrote `"nt"` into the shared
``os`` module, and one that asserted a refusal a branch had deliberately
removed. Every one of them was invisible locally and obvious here.

This is not a replacement for CI. CI runs four Python versions on three
platforms; this runs one version on Linux. It catches the platform class, which
is the class that keeps getting through.

The container clones the repository from a read-only mount rather than working
in the mount itself. That is the whole point of the arrangement:

* a bind mount carries the working tree, including ``.venv``, ``.testenv`` and
  whatever else is lying around, and an editable install from it would resolve
  to Windows paths;
* NTFS presents its own permission bits through the mount, so the mode and
  ownership tests -- the ones this exists to exercise -- would be measuring the
  mount driver rather than the code.

Cloning inside the container tests the committed tree, which is what CI tests.
Uncommitted work is therefore *not* covered: commit first, or expect a green
run that says nothing about what you changed.

Usage:

    python tools/ci_linux.py                     # the whole suite
    python tools/ci_linux.py tests/test_hardening.py -x
    python tools/ci_linux.py --python 3.13       # a different interpreter

Everything after the recognised options is handed to pytest unchanged.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

DEFAULT_PYTHON = "3.12"
EXIT_NO_DOCKER = 2

# `pip install -e` needs the metadata build; `git clone` needs git. The full
# python image carries both, the slim variants do not, and diagnosing that from
# inside a container is a waste of an afternoon.
IMAGE = "python:{version}"

SCRIPT = """
set -e
git config --global --add safe.directory /src
git clone -q /src /work
cd /work
pip install -q -e '.[dev,can]'
exec python -m pytest {args}
"""


def repository_root() -> Path:
    """The work tree this file belongs to, so the tool works from any cwd."""
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=Path(__file__).resolve().parent,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return Path(__file__).resolve().parents[1]
    return Path(result.stdout.strip())


def docker_mount_source(root: Path) -> str:
    """The path in the form the Docker CLI expects on this platform.

    Docker Desktop on Windows takes `C:/path`; a POSIX absolute path passes
    through unchanged. `as_posix()` produces both from a `Path`.
    """
    return root.resolve().as_posix()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--python", default=DEFAULT_PYTHON, help=f"Python version for the container image (default {DEFAULT_PYTHON}).")
    parser.add_argument("--image", default=None, help="Container image, overriding --python entirely.")
    parser.add_argument("pytest_args", nargs=argparse.REMAINDER, help="Passed to pytest unchanged.")
    options = parser.parse_args(argv)

    if shutil.which("docker") is None:
        print(
            "docker was not found on PATH.\n"
            "This tool is optional: it reproduces the Linux CI job locally, which is the\n"
            "only way a Windows machine sees the POSIX half of the suite. Without it, say\n"
            "in the pull request which tests you could not run.",
            file=sys.stderr,
        )
        return EXIT_NO_DOCKER

    root = repository_root()
    image = options.image or IMAGE.format(version=options.python)
    # `--` separates our options from pytest's; argparse.REMAINDER keeps it.
    forwarded = [argument for argument in options.pytest_args if argument != "--"]
    script = SCRIPT.format(args=" ".join(forwarded) if forwarded else "-q")

    # --init, or `bash -c` is PID 1 and reaps nothing: a SIGKILLed process-group
    # leader then lingers as a zombie that still counts as a live group member,
    # and test_process_cleanup_handles_exited_group_leader fails in the container
    # while passing everywhere an init exists. The CI runners have one; give the
    # container one too so this runs what CI runs.
    command = ["docker", "run", "--rm", "--init", "-v", f"{docker_mount_source(root)}:/src:ro", image, "bash", "-c", script]
    print(f"{image}: the committed tree at {root}", flush=True)
    return subprocess.run(command, check=False).returncode


if __name__ == "__main__":  # pragma: no cover - entry point
    raise SystemExit(main())
