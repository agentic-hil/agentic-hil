"""The suite's own guard against two workers meeting in the machine's process table.

`tests/test_install_scripts.py` drives step 5 of `install.ps1` against the real
process table, which is what step 5 reads and what it has to keep reading: an
operator's agent CLI is wherever they started it, and a scan that only looked
inside the test's own directory would be a scan the real thing could hide from.
The cost of that is inside the suite rather than inside the installer. One test
plants a node process whose script carries an agent CLI's name; another asserts
that nothing of the sort is running. Run on two workers at once, the second
reads the first one's child and is told a restart is required.

The decided answer is scheduling, not a narrower scan: every test that plants an
agent shaped process and every test that asserts none is running carries one
`xdist_group` mark with one shared name, and the suite's own configuration asks
for `--dist loadgroup`, so a group never straddles two workers. The
configuration carries it rather than each caller, so a bare `pytest`, the CI
legs, `tools/ci_linux.py` and the container invocation all get it without a
change of their own.

Two suites started at once on one machine can still meet in the table. That is
outside what a scheduler can promise and stays what it is.
"""

from __future__ import annotations

import inspect
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from tests import test_install_scripts as install_scripts

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - exercised on 3.10 only
    import tomli as tomllib

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = REPOSITORY_ROOT / "pyproject.toml"
INSTALL_MODULE = Path(install_scripts.__file__).resolve()
INSTALL_MODULE_PATH = INSTALL_MODULE.relative_to(REPOSITORY_ROOT).as_posix()

# The name of the constant the install module is expected to declare, so the
# group's name is written down once with its reason beside it instead of being
# repeated on every decorator that uses it.
GROUP_CONSTANT = "STEP_FIVE_PROCESS_TABLE_GROUP"
# The fallback keeps this module importable before the constant exists, so a run
# against the unfixed tree reports failing assertions rather than a collection
# error that says nothing about what is missing.
SHARED_GROUP = getattr(install_scripts, GROUP_CONSTANT, "install-step-five-process-table")

# What makes a test a party to the shared table, read off the test's own source
# rather than off a list somebody has to remember to extend. The first two are
# the helpers that put an agent shaped process into the table: the interpreter
# wearing npm's shape, and the child that lingers while the installer runs. The
# third is the sentence step 5 prints when it found none, which is the assertion
# a stranger's planted process turns red.
PLANTING_HELPERS = ("_a_node_shaped_interpreter", "_a_process_that_lingers")
ABSENCE_SENTENCE = "no agent CLI of yours is running"
PROCESS_TABLE_NEEDLES = (*PLANTING_HELPERS, ABSENCE_SENTENCE)

WINDOWS_ONLY = pytest.mark.skipif(
    os.name != "nt",
    reason="step 5's restart block runs under Windows PowerShell 5.1, which exists on Windows alone",
)
# The nested run below starts two real Windows PowerShell installs, each of them
# under the install module's own 180 second per script budget. The budget here
# bounds a hang of the whole nested session, not a slow one.
NESTED_RUN_TIMEOUT_S = 900


def _test_functions() -> dict[str, object]:
    """Every test function the install module defines, by name."""
    found = {}
    for name, value in vars(install_scripts).items():
        if not name.startswith("test_") or not callable(value):
            continue
        if getattr(value, "__module__", None) != install_scripts.__name__:
            continue
        found[name] = value
    return found


def _parties_to_the_process_table() -> dict[str, object]:
    """The tests that plant an agent shaped process, or assert none is running."""
    return {
        name: function
        for name, function in _test_functions().items()
        if any(needle in inspect.getsource(function) for needle in PROCESS_TABLE_NEEDLES)
    }


def _group_names(function: object) -> list[str]:
    """The group name of every `xdist_group` mark on one test function."""
    names = []
    for mark in getattr(function, "pytestmark", []):
        if mark.name != "xdist_group":
            continue
        if mark.args:
            names.append(mark.args[0])
        elif "name" in mark.kwargs:
            names.append(mark.kwargs["name"])
        else:
            names.append(None)
    return names


def _addopts() -> str:
    declared = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    return declared["tool"]["pytest"]["ini_options"]["addopts"]


def test_the_install_module_names_the_shared_group_once() -> None:
    """The group's name is a declared constant, not a string repeated on decorators.

    Two decorators carrying two spellings of nearly the same name are two
    groups, and two groups are two workers, which is the failure this whole
    change is about. A constant is the one place the name can be read from.
    """
    declared = getattr(install_scripts, GROUP_CONSTANT, None)
    assert declared is not None, (
        f"{INSTALL_MODULE_PATH} declares no {GROUP_CONSTANT}, so the tests that share the "
        "machine's process table have no one name to be grouped under"
    )
    assert isinstance(declared, str) and declared.strip(), f"{GROUP_CONSTANT} is not a usable group name: {declared!r}"


def test_every_test_that_touches_the_process_table_carries_the_shared_group() -> None:
    """Planters and the test that asserts absence run under one `xdist_group`.

    Found by what the test does, not by a list kept by hand: a test that calls
    one of the planting helpers, or that asserts step 5's sentence for a machine
    with nothing running, is a party whether or not anybody remembered it.
    """
    parties = _parties_to_the_process_table()
    assert parties, (
        f"no test in {INSTALL_MODULE_PATH} plants or asserts on an agent shaped process, "
        "so this pin is reading the wrong module or the helpers were renamed"
    )
    ungrouped = sorted(name for name, function in parties.items() if _group_names(function) != [SHARED_GROUP])
    assert not ungrouped, (
        "these tests share the machine's process table and must carry "
        f"@pytest.mark.xdist_group({SHARED_GROUP!r}): " + ", ".join(ungrouped)
    )


def test_no_other_test_in_the_install_module_is_pinned_to_that_worker() -> None:
    """The group stays as small as the problem.

    Everything else in the module is independent, and a group that grew to hold
    it would turn a parallel run of the module back into a serial one.
    """
    parties = _parties_to_the_process_table()
    strays = sorted(name for name, function in _test_functions().items() if name not in parties and _group_names(function))
    assert not strays, (
        "these tests carry an xdist_group mark but neither plant nor assert on an agent shaped process: "
        + ", ".join(strays)
    )


def test_the_suite_configuration_asks_for_the_grouping_scheduler() -> None:
    """`--dist loadgroup` lives in `addopts`, so every caller of the suite gets it.

    A group mark is inert under the default scheduler, which distributes by
    test. Named here rather than in each caller, `pytest -n auto`, the CI legs,
    `tools/ci_linux.py` and the container invocation all schedule the same way
    without a change of their own, and a caller that forgets it cannot exist.
    """
    options = shlex.split(_addopts())
    pairs = list(zip(options, options[1:] + [""], strict=False))
    asked = ("--dist=loadgroup" in options) or (("--dist", "loadgroup") in pairs)
    assert asked, f"pyproject.toml's addopts does not ask for --dist loadgroup: {_addopts()!r}"


def test_the_suite_configuration_keeps_refusing_an_undeclared_marker() -> None:
    """The neighbour in the same setting, unchanged.

    `--strict-markers` is what makes a typo in a marker name a failure rather
    than a mark nothing acts on, and a scheduler option added beside it must not
    displace it.
    """
    assert "--strict-markers" in shlex.split(_addopts()), _addopts()


def test_nothing_that_runs_the_suite_overrides_the_scheduler_with_its_own() -> None:
    """No caller passes a `--dist` that would win over the configured one.

    pytest reads `addopts` first and the command line after it, so a caller with
    a `--dist` of its own decides the schedule for that run and the marks go
    back to being inert. The CI legs, the Linux runner and the container
    invocation are the callers this repository owns.
    """
    callers = [
        REPOSITORY_ROOT / ".github" / "workflows" / "ci.yml",
        REPOSITORY_ROOT / ".github" / "workflows" / "hardware-bench.yml",
        REPOSITORY_ROOT / "tools" / "ci_linux.py",
        REPOSITORY_ROOT / "tools" / "container" / "Dockerfile",
    ]
    offenders = []
    for caller in callers:
        if not caller.exists():
            continue
        for number, line in enumerate(caller.read_text(encoding="utf-8").splitlines(), start=1):
            if re.search(r"--dist[ =]", line):
                offenders.append(f"{caller.relative_to(REPOSITORY_ROOT).as_posix()}:{number}: {line.strip()}")
    assert not offenders, "these callers pass a --dist of their own, which overrides the configured one:\n" + "\n".join(
        offenders
    )


def test_step_fives_scan_still_reads_the_whole_process_table() -> None:
    """The installer is not the thing that changes, and this is why.

    `Get-RunningAgentProcessId` enumerates every process on the machine and
    matches on the program being run. A scan narrowed to a directory, a
    temporary path or the script's own root would make this suite quiet and make
    an operator's real CLI invisible to the block that asks them to restart it.
    """
    source = (REPOSITORY_ROOT / "install.ps1").read_text(encoding="utf-8")
    start = source.index("function Get-RunningAgentProcessId")
    body = source[start : source.index("\nfunction ", start + 1)]
    assert "Get-CimInstance -ClassName Win32_Process" in body, body
    assert "-Filter" not in body, body
    for narrowing in ("PSScriptRoot", "env:TEMP", "env:TMP", "$PWD", "CurrentDirectory"):
        assert narrowing not in body, f"step 5's scan narrowed itself to {narrowing}, so a real agent CLI can hide from it"


@WINDOWS_ONLY
@pytest.mark.xdist_group(SHARED_GROUP)
def test_two_workers_run_the_whole_group_on_one_of_them() -> None:
    """The acceptance: the marks and the scheduler together, read off a real run.

    A nested session with two workers over exactly the grouped tests. The
    verbose output prefixes every result with the worker that produced it, so
    one distinct `gwN` across all of them is the whole claim, and the run has to
    be green as well: two workers that took a test each are also what the
    failure looked like.

    This test is a party to the same table, because the session it starts plants
    the same processes, so it carries the same group and never runs beside them.
    """
    parties = sorted(_parties_to_the_process_table())
    assert len(parties) >= 2, parties
    node_ids = [f"{INSTALL_MODULE_PATH}::{name}" for name in parties]
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            *node_ids,
            "-n",
            "2",
            "--dist",
            "loadgroup",
            "-v",
            "-p",
            "no:cacheprovider",
        ],
        cwd=REPOSITORY_ROOT,
        capture_output=True,
        text=True,
        timeout=NESTED_RUN_TIMEOUT_S,
    )
    transcript = completed.stdout + completed.stderr
    workers = {}
    for line in completed.stdout.splitlines():
        match = re.match(r"\[(gw\d+)\] \[\s*\d+%\] (\w+) (\S+)", line)
        if match is not None:
            workers.setdefault(match.group(3), set()).add(match.group(1))
    reported = {node_id: workers.get(node_id, set()) for node_id in node_ids}
    assert all(reported.values()), f"the nested run reported no worker for some of the group: {reported}\n{transcript}"
    on = sorted({worker for seen in reported.values() for worker in seen})
    assert len(on) == 1, f"the group straddled {len(on)} workers ({', '.join(on)}): {reported}\n{transcript}"
    assert completed.returncode == 0, transcript
