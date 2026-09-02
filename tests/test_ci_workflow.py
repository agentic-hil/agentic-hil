"""How the hosted jobs invoke the suite, and where that decision is written.

In one process the Windows legs of this suite grew to about fourteen minutes of
pytest on an ordinary runner and twenty-three on a slow one, against a job limit
of twenty-five, and the limit cancelled two runs whose tests were green: one
inside the test step with 2307 tests already passed, one in the step after
pytest had printed its own summary. Nothing was hanging. The runner was slow,
and a serial suite has no answer to that except the retry, which costs the same
again for a result that was already known.

The suite is parallel by construction rather than by permission: every test gets
its own HOME, config, state and temporary storage, and its device-lock root, its
CAN broker endpoint and its run records all follow that HOME, which is why every
developer run has used `-n auto` all along. The hosted jobs pass it too.

What is pinned here is the split, because one word moves it in either direction:
the hosted run is parallel, the collection out of the built source distribution
stays a collection, and `addopts` still says nothing about workers, so a bare
`pytest` in a checkout is one process and a failure can be read without a worker
id in front of it.
"""

from __future__ import annotations

import shlex
from pathlib import Path

import pytest
import yaml

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPOSITORY_ROOT / ".github" / "workflows" / "ci.yml"
# Every spelling pytest-xdist takes for "how many workers". A step that reaches
# parallelism the long way round is still parallel, and a test that only knew
# `-n auto` would call it serial.
WORKER_OPTIONS = ("-n", "--numprocesses")


def worker_setting(command: list[str]) -> str | None:
    """What a command line says about xdist workers, or None if it says nothing."""
    for index, argument in enumerate(command):
        for option in WORKER_OPTIONS:
            if argument == option:
                return command[index + 1] if index + 1 < len(command) else ""
            if argument.startswith(f"{option}="):
                return argument.split("=", 1)[1]
        if argument.startswith("-n") and len(argument) > 2:
            return argument[2:]
    return None


def hosted_step(name: str) -> list[str]:
    """What one step of the matrix job runs, split the way a shell splits it.

    `.github/` is repository content and MANIFEST.in does not ship it, so an
    unpacked source distribution has no workflow to read. That checkout is
    collected rather than run in CI, and every checkout that runs this file has
    the file this reads.
    """
    if not WORKFLOW.is_file():
        pytest.skip(f"{WORKFLOW.name} is repository content and this checkout has none")
    steps = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]["test"]["steps"]
    named = [step for step in steps if step.get("name") == name]
    assert len(named) == 1, f"the test job has {len(named)} steps named {name!r}; this test is not reading it any more"
    return shlex.split(named[0]["run"])


def test_the_hosted_suite_runs_across_the_runners_cores() -> None:
    """One process is what put a green Windows run over the job's time limit."""
    command = hosted_step("Run tests")

    assert command[0] == "pytest", command
    assert worker_setting(command) == "auto", command


def test_the_source_distribution_is_still_only_collected() -> None:
    """That step asks whether pytest can find its way through what the sdist ships.

    Collecting is the whole question, the dev dependencies are already on the
    interpreter that built it, and running the suite a second time per matrix
    leg would buy nothing and cost every minute the step above just saved.
    """
    command = hosted_step("Collect the source distribution's tests")

    assert "--collect-only" in command, command
    assert worker_setting(command) is None, command


def test_a_bare_pytest_in_a_checkout_is_still_one_process(pytestconfig: pytest.Config) -> None:
    """The parallel run is a command line, not a property of the checkout.

    A worker count in `addopts` would reach every invocation, including the one
    a developer types to read a single failure without a worker id in front of
    it, and including tooling that runs pytest for reasons of its own.
    """
    addopts = list(pytestconfig.getini("addopts"))

    # The real ini, not an empty answer that would pass whatever it carried.
    assert "--strict-markers" in addopts, addopts
    assert worker_setting(addopts) is None, addopts
