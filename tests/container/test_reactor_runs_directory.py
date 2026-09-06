"""What `test-reactor --detach` answers when the runs directory cannot be written (#505).

The detached start opens the worker's log under the coordination state's runs
directory before it spawns anything. A directory this process cannot write
into raised a bare `PermissionError` out of that open, which the command
printed as a Python traceback instead of the JSON refusal every other failure
is answered with. It is a kernel permission check that decides this, so the
tier is the container's: root passes every mode bit, and the command has to run
as a user the bits apply to.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
from contextlib import suppress
from pathlib import Path

import pytest

from .conftest import (
    COMMAND_TIMEOUT_S,
    CONTAINER_ONLY,
    REPOSITORY_ROOT,
    UnprivilegedTree,
    UnprivilegedUser,
    fixture_configuration,
    unprivileged_user,
)

pytestmark = [pytest.mark.container, CONTAINER_ONLY]

FAKE_OPENOCD = REPOSITORY_ROOT / "tests" / "fixtures" / "fake_openocd.py"
DELAY_PLAN = "version: 4\nsteps:\n  - {device: dut, action: delay, duration_ms: 20}\n"
# The plan the detached attempt is asked to run. Long on purpose: a worker that
# was spawned despite the refusal is still there when the process table is
# read, instead of having finished a 20 ms plan before the guard looked.
LONG_DELAY_PLAN = "version: 4\nsteps:\n  - {device: dut, action: delay, duration_ms: 600000}\n"


def fake_probe_tree(user: UnprivilegedUser) -> tuple[UnprivilegedTree, Path, Path]:
    """A tree the user owns, its debugger the fake OpenOCD, with a short and a long delay plan in it."""
    root = Path(tempfile.mkdtemp(prefix="agentic-hil-unprivileged-", dir="/tmp"))
    tree = UnprivilegedTree(root=root, user=user)
    tree.project.mkdir()
    tree.home.mkdir()
    (root / "tmp").mkdir()
    probe = root / "fake_openocd.py"
    probe.write_bytes(FAKE_OPENOCD.read_bytes())
    fixture_configuration(tree.project, tree.config_path, tree.state, executable=repr(str(probe)))
    plan = tree.project / ".agentic-hil" / "testconfig.yaml"
    plan.parent.mkdir(parents=True)
    plan.write_text(DELAY_PLAN, encoding="utf-8")
    long_plan = plan.with_name("long-testconfig.yaml")
    long_plan.write_text(LONG_DELAY_PLAN, encoding="utf-8")
    tree.give_away()
    return tree, plan, long_plan


def reactor_as_the_user(tree: UnprivilegedTree, *arguments: str) -> subprocess.CompletedProcess[str]:
    """`agentic-hil test-reactor ...` in the project, as the unprivileged user."""
    return subprocess.run(
        tree.user.command(sys.executable, "-m", "agentic_hil", "test-reactor", *arguments),
        cwd=str(tree.project),
        env={**os.environ, "AGENTIC_HIL_CONFIG": str(tree.config_path), **tree.environment()},
        capture_output=True,
        text=True,
        timeout=COMMAND_TIMEOUT_S,
        check=False,
    )


def reactor_workers_still_running() -> list[str]:
    """Every process on this host running a detached reactor worker, by pid.

    The long plan keeps such a worker alive for the whole test, so one that is
    there is one that was spawned despite the refusal, and it has to be ended
    before the tree is removed under it."""
    found: list[str] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            command_line = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        if b"agentic_hil" in command_line and b"test-reactor" in command_line and b"--run-handle" in command_line:
            found.append(entry.name)
    return found


def end_workers(pids: list[str]) -> None:
    for pid in pids:
        with suppress(ProcessLookupError, PermissionError):
            os.kill(int(pid), signal.SIGKILL)


def test_a_state_directory_that_cannot_be_written_refuses_a_detached_start_by_name() -> None:
    """A read-only runs directory is a JSON refusal and exit 1, not a traceback.

    The directory exists because a synchronous run as the same user created it;
    root then takes the user's write bit off it. The detached start after that
    answers a document naming the directory it could not write, exits 1, and
    leaves no worker behind: a worker that had been spawned would run the whole
    plan under a handle nobody can see.
    """
    user = unprivileged_user()
    tree, plan, long_plan = fake_probe_tree(user)
    runs = tree.state / "coordination" / "runs"
    try:
        warmed = reactor_as_the_user(tree, "--test-config", str(plan), "--json")
        assert warmed.returncode == 0, warmed.stdout + warmed.stderr
        assert runs.is_dir(), sorted(str(path) for path in tree.state.rglob("*"))
        os.chmod(runs, 0o500)

        refused = reactor_as_the_user(tree, "--detach", "--test-config", str(long_plan), "--json")
    finally:
        with_workers = reactor_workers_still_running()
        end_workers(with_workers)
        with suppress(OSError):
            os.chmod(runs, 0o700)
        tree.remove()

    assert "Traceback" not in refused.stderr, refused.stderr
    assert refused.returncode == 1, (refused.returncode, refused.stdout, refused.stderr)
    assert refused.stdout.strip(), f"no document on stdout; stderr was:\n{refused.stderr}"
    document = json.loads(refused.stdout)
    assert document["ok"] is False, document
    assert isinstance(document.get("error_type"), str) and document["error_type"], document
    assert str(runs) in json.dumps(document), document
    assert document.get("side_effect_committed") is False, document
    assert with_workers == [], with_workers
