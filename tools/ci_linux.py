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

The container's exit status is not taken at face value. A clone that cannot
build a usable tree is reported as itself, and a run is only allowed to succeed
if pytest said what it did: no summary line, or a summary accounting for no
tests at all, fails the run. Exit 0 after nothing was collected has been
observed, and a green result for a suite that never executed is worse than no
result at all.

One run at a time per machine. Three or four full-suite containers on one
Docker daemon starve each other until the suite dies mid-flight without a
summary line, which the check above then correctly reports as nothing known to
have been tested: a wasted run, a retry, and a partial line somebody may read as
progress. A run therefore takes a lock for its whole duration and queues behind
whoever holds it, naming the holder while it waits. ``--no-wait`` refuses
instead of queueing.

The lock itself is `run_lock.py` beside this file, and `loop_in_container.py`
takes the same one: a review loop is another long-lived container on the same
daemon, and two mechanisms that do not read each other's records are two runs
that cannot see each other.

Usage:

    python tools/ci_linux.py                     # the whole suite
    python tools/ci_linux.py tests/test_hardening.py -x
    python tools/ci_linux.py --python 3.13       # a different interpreter
    python tools/ci_linux.py --no-wait           # refuse if a run is in flight

Everything after the recognised options is handed to pytest unchanged.
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path

# This is a standalone script, run as `python tools/ci_linux.py` from checkouts
# that are not installed, so the module beside it is reached by naming this
# directory rather than by a package import. Running the file that way already
# puts it first on the path; the insert is what keeps `import run_lock` working
# when something imports this module by another route.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from run_lock import RunLock, RunLockBusy  # noqa: E402

# CI runs 3.10 through 3.13; this runs one of them. 3.12 is the newest version
# every dependency ships wheels for, so the image builds without a compiler and
# the run stays fast. Override with --python to reproduce a version-specific
# failure.
DEFAULT_PYTHON = "3.12"
EXIT_NO_DOCKER = 2
# The clone step and the missing result are separate failures and get separate
# statuses, because "the tree never built" and "the tests never reported" ask
# for different things from whoever reads the exit code.
EXIT_CLONE_FAILED = 3
EXIT_NO_RESULT = 4
# A run refused because another one holds the machine is not a test failure and
# must not read as one: a script that retries on red would otherwise retry the
# collision it was just told about.
EXIT_LOCKED = 5

# What this run writes into the lock record about itself, for whoever queues
# behind it. The scale is measured rather than estimated: 2700-odd tests in one
# process, start to finish, timed on this repository's own benches at 4m51s with
# an idle daemon and at 6 to 7 minutes on a working one. It is in the record
# rather than in the waiting run, because the run that has to decide whether to
# wait is the one that did not start this container and cannot know what it is.
TOOL_NAME = "tools/ci_linux.py"
RUNS_FOR = "5 to 7 minutes"

# The container prints this before it gives up on the clone. A run that dies
# there has to go red naming the clone: relying on `set -e` alone made a broken
# checkout surface as a pytest step that never happened, and once it had been
# observed exiting 0 with nothing collected, the step that failed was the one
# piece of information the exit code did not carry.
CLONE_FAILURE_MARKER = "CI_LINUX_CLONE_FAILED"

# `pip install -e` needs the metadata build; `git clone` needs git. The full
# python image carries both, the slim variants do not, and diagnosing that from
# inside a container is a waste of an afternoon.
IMAGE = "python:{version}"

# The mount is owned by the host user, not the container's root, so git treats
# /src as unsafe and refuses to read it; `safe.directory` waives that check for
# the one path we clone from.
# `"$@"` rather than the arguments interpolated into the text: they arrive as
# `bash -c <script> <$0> <arg>...` and stay one argument each. Joining them with
# spaces split `-k "foo and bar"` into three and handed any quote, `$` or `;` in
# a path or expression to Bash, which is the opposite of "passed to pytest
# unchanged".
# The clone is checked twice, and neither check is redundant. `git clone` can
# die outright -- a work tree whose `.git/objects/info/alternates` names a path
# the container cannot resolve makes it exit 128 without leaving /work behind --
# and it can also return having produced a directory that is not this
# repository. Only the second one reaches the `pip install`, where it fails as
# something else entirely.
CLONE_SCRIPT = f"""
set -e
git config --global --add safe.directory /src
if ! git clone -q /src /work; then
    echo "{CLONE_FAILURE_MARKER}: git clone of /src into /work failed" >&2
    exit {EXIT_CLONE_FAILED}
fi
cd /work
if [ ! -f pyproject.toml ]; then
    echo "{CLONE_FAILURE_MARKER}: the clone of /src left no pyproject.toml in /work" >&2
    exit {EXIT_CLONE_FAILED}
fi
"""

SCRIPT = CLONE_SCRIPT + """
pip install -q -e '.[dev,can]'
exec python -m pytest "$@"
"""
# `bash -c` takes the word after the script as `$0`, not as `$1`. It is only a
# label for error messages; naming it is what keeps the first real argument in
# `"$@"`.
SCRIPT_ARGV0 = "ci_linux"


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


# pytest's last line is the only place it says what it did: "1476 passed, 35
# skipped in 41.23s", "no tests ran in 0.01s" when the collection came up empty,
# "37 tests collected in 0.42s" under --collect-only. The `=` rules around it in
# the non-quiet output are stripped before matching, and the duration is what
# distinguishes that line from every other line carrying a number.
_SUMMARY_DURATION = re.compile(r"\bin\s+\d+(?:\.\d+)?\s*(?:s|seconds)\b")
_SUMMARY_COUNT = re.compile(
    r"(\d+)\s+(?:passed|failed|errors?|skipped|xfailed|xpassed|deselected|tests?\s+collected)\b"
)
_NO_TESTS_RAN = re.compile(r"\bno tests ran\b", re.IGNORECASE)


def result_summary(output: str) -> str | None:
    """pytest's own summary line, or None if the run never reported one.

    The last one wins: a suite that reruns or a plugin that prints its own
    timings can leave more than one candidate behind, and pytest's is last.
    """
    summary = None
    for raw_line in output.splitlines():
        line = raw_line.strip().strip("=").strip()
        if not _SUMMARY_DURATION.search(line):
            continue
        if _NO_TESTS_RAN.search(line) or _SUMMARY_COUNT.search(line):
            summary = line
    return summary


def tests_accounted_for(summary: str) -> int:
    """How many tests pytest says it found, run or skipped alike.

    Skipped and deselected tests count: they were collected, and a selection
    that legitimately matches only skips is still a result. Zero is not.
    """
    return sum(int(count) for count in _SUMMARY_COUNT.findall(summary))


def evaluate_run(returncode: int, output: str) -> tuple[int, str | None]:
    """This tool's exit status for a finished container run, and why.

    The container's own status is forwarded whenever it means something -- a
    failing suite must leave this process with pytest's code. It is replaced
    only where it means nothing: a run that exits 0 having tested nothing is a
    green result for work that never happened, which is the one outcome this
    tool must never report.
    """
    if CLONE_FAILURE_MARKER in output:
        return (
            returncode or EXIT_CLONE_FAILED,
            "the container could not clone the repository into a usable tree, so pytest never ran",
        )
    summary = result_summary(output)
    if summary is None:
        return (
            returncode or EXIT_NO_RESULT,
            "the run printed no pytest summary line, so nothing is known to have been tested",
        )
    if tests_accounted_for(summary) == 0:
        return (returncode or EXIT_NO_RESULT, f"pytest collected no tests: {summary}")
    return returncode, None


def announce(text: str) -> None:
    """Say something in this tool's own voice, on the stream reserved for it.

    stderr, because stdout carries the container's output verbatim and a line
    this script wrote about itself must stay tellable from a line pytest wrote.
    """
    print(f"ci_linux: {text}", file=sys.stderr, flush=True)


def run_container(command: list[str]) -> tuple[int, str]:
    """Run the container, echoing its output as it arrives and keeping a copy.

    The output is read rather than inherited because the exit status alone
    cannot tell a suite that failed from a suite that never ran. stderr is
    merged into it: the clone step reports its own failure there, and that
    report is what names the step.
    """
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        errors="replace",
    )
    captured: list[str] = []
    for line in process.stdout:
        captured.append(line)
        sys.stdout.write(line)
        sys.stdout.flush()
    return process.wait(), "".join(captured)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--python", default=DEFAULT_PYTHON, help=f"Python version for the container image (default {DEFAULT_PYTHON}).")
    parser.add_argument("--image", default=None, help="Container image, overriding --python entirely.")
    parser.add_argument(
        "--no-wait",
        action="store_true",
        help="Refuse instead of queueing when another run holds this machine.",
    )
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
    # `--` separates our options from pytest's, and argparse.REMAINDER keeps it,
    # so drop the leading one and nothing else: pytest uses a `--` of its own to
    # end its option parsing, and a filter over the whole list ate that too — so
    # `ci_linux.py -- tests -- -x` reached pytest as `tests -x`, which is not
    # "everything after the options, unchanged".
    forwarded = list(options.pytest_args)
    if forwarded and forwarded[0] == "--":
        forwarded.pop(0)

    # --init, or `bash -c` is PID 1 and reaps nothing: a SIGKILLed process-group
    # leader then lingers as a zombie that still counts as a live group member,
    # and test_process_cleanup_handles_exited_group_leader fails in the container
    # while passing everywhere an init exists. The CI runners have one; give the
    # container one too so this runs what CI runs.
    command = [
        "docker",
        "run",
        "--rm",
        "--init",
        "-v",
        f"{docker_mount_source(root)}:/src:ro",
        image,
        "bash",
        "-c",
        SCRIPT,
        SCRIPT_ARGV0,
        *(forwarded or ["-q"]),
    ]
    # Taken before the run is announced and released only after it is over, so
    # the window the lock covers is exactly the window a container is on the
    # daemon. Everything above it is argument handling that costs nothing and
    # should not make a queued run wait for it.
    lock = RunLock(
        tool=TOOL_NAME,
        runs_for=RUNS_FOR,
        root=root,
        details={"image": image, "pytest_args": forwarded or ["-q"]},
        announce=announce,
    )
    try:
        lock.acquire(wait=not options.no_wait)
    except RunLockBusy as busy:
        announce(f"{busy}. Refusing, because --no-wait was given")
        return EXIT_LOCKED
    except OSError as error:
        announce(f"the lock at {lock.path} could not be taken: {error}")
        return EXIT_LOCKED
    try:
        print(f"{image}: the committed tree at {root}", flush=True)
        returncode, output = run_container(command)
    finally:
        lock.release()
    status, reason = evaluate_run(returncode, output)
    if reason is not None:
        announce(reason)
    return status


if __name__ == "__main__":  # pragma: no cover - entry point
    raise SystemExit(main())
