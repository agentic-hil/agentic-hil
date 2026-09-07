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

That configuration line is the one thing this change costs, and the pins below
bound it in both directions: it brings the scheduler and nothing else, so a bare
`pytest` is still one process whose failures carry no worker id, and it makes
`pytest-xdist` a plugin the checkout cannot run without, which is why the pins
also read the declaration it depends on.

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
CONTRIBUTING = REPOSITORY_ROOT / "CONTRIBUTING.md"
INSTALL_MODULE = Path(install_scripts.__file__).resolve()
INSTALL_MODULE_PATH = INSTALL_MODULE.relative_to(REPOSITORY_ROOT).as_posix()
THIS_MODULE_PATH = Path(__file__).resolve().relative_to(REPOSITORY_ROOT).as_posix()

# The name of the constant the install module is expected to declare, so the
# group's name is written down once with its reason beside it instead of being
# repeated on every decorator that uses it. This constant is the one shape these
# pins prescribe beyond what the issue decided, and it is what lets a test read
# the name rather than repeat a fourth spelling of it.
GROUP_CONSTANT = "STEP_FIVE_PROCESS_TABLE_GROUP"
# The fallback keeps this module importable before the constant exists, so a run
# against the unfixed tree reports failing assertions rather than a collection
# error that says nothing about what is missing. Every pin that compares against
# it asserts the constant first, so a tree that never declares it fails on that
# sentence and not on a mark that looks right.
SHARED_GROUP = getattr(install_scripts, GROUP_CONSTANT, "install-step-five-process-table")

# What makes a test a party to the shared table, read off the test's own source
# rather than off a list somebody has to remember to extend. The first two are
# the helpers that put an agent shaped process into the table: the interpreter
# wearing npm's shape, and the child that lingers while the installer runs. The
# third is the sentence step 5 prints when it found none, which is the assertion
# a stranger's planted process turns red.
PLANTING_HELPERS = ("_a_node_shaped_interpreter", "_a_process_that_lingers")
ABSENCE_SENTENCE = "no agent CLI of yours is running"

WINDOWS_ONLY = pytest.mark.skipif(
    os.name != "nt",
    reason="step 5's restart block runs under Windows PowerShell 5.1, which exists on Windows alone",
)
# The nested runs below start real pytest sessions in this checkout. The
# acceptance starts two Windows PowerShell installs, each under the install
# module's own 180 second per script budget, and the group is exactly what makes
# them run one after the other on a single worker rather than side by side: two
# budgets, the session's own startup, and the collection of the module they
# live in. The bound is for a hang of the whole nested session, not for a slow
# install.
NESTED_RUN_TIMEOUT_S = 900
# The other nested run collects and runs one configuration pin, so it is a
# session startup and nothing else.
QUICK_NESTED_RUN_TIMEOUT_S = 300
# A comment, in every file the caller scan reads: YAML, Python and the
# Dockerfile all start one with `#`. Stripped before the scan looks for an
# option, because a file that explains beside the option why it is not passed
# here is a file that names the option in prose.
COMMENT = re.compile(r"(?:^|\s)#.*$")


def _absence_sentence_constants() -> tuple[str, ...]:
    """Module level constants of the install module that carry the absence sentence.

    The two parties today assert the sentence as a literal. The module already
    keeps its other transcript lines as constants (`RESTART_LINES`, `CALM_LINE`),
    so a test written tomorrow may assert this one through a constant instead,
    and it is a party all the same. Its name is then what its source contains.
    """
    found = []
    for name, value in vars(install_scripts).items():
        if name.startswith("_") or not name.isupper():
            continue
        parts = [value] if isinstance(value, str) else list(value) if isinstance(value, (tuple, list)) else []
        if any(isinstance(part, str) and ABSENCE_SENTENCE in part for part in parts):
            found.append(name)
    return tuple(found)


def _process_table_needles() -> tuple[str, ...]:
    return (*PLANTING_HELPERS, ABSENCE_SENTENCE, *_absence_sentence_constants())


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
    needles = _process_table_needles()
    return {
        name: function
        for name, function in _test_functions().items()
        if any(needle in inspect.getsource(function) for needle in needles)
    }


def _group_names(function: object) -> list[str]:
    """The group name of every `xdist_group` mark carried by one test function.

    Read off the function object, which is where a decorator puts it. An
    implementation that grouped the parties through a module level `pytestmark`,
    a class, or `pytest_collection_modifyitems` would schedule them correctly
    and still fail the pins below; the decided behaviour is the mark on the test
    itself, so that a reader of the test sees why it is grouped.
    """
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


def _declared_group() -> str:
    declared = getattr(install_scripts, GROUP_CONSTANT, None)
    assert isinstance(declared, str) and declared.strip(), (
        f"{INSTALL_MODULE_PATH} declares no usable {GROUP_CONSTANT} ({declared!r}), so the tests that share "
        "the machine's process table have no one name to be grouped under"
    )
    return declared


def _pyproject() -> dict:
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))


def _addopts() -> str:
    options = _pyproject()["tool"]["pytest"]["ini_options"].get("addopts")
    assert isinstance(options, str), f"pyproject.toml's addopts is {options!r}, and these pins read it as one string"
    return options


def _without_comments(line: str) -> str:
    return COMMENT.sub("", line)


def _suite_callers() -> list[Path]:
    """Every file in this repository that starts a run of the suite.

    Globbed rather than listed, so a workflow added or renamed tomorrow is read
    too, and the four this repository owns today are asserted to exist so that a
    rename cannot make this pin quietly scan nothing.
    """
    workflows = REPOSITORY_ROOT / ".github" / "workflows"
    callers = sorted(workflows.glob("*.yml")) + sorted(workflows.glob("*.yaml"))
    callers += [REPOSITORY_ROOT / "tools" / "ci_linux.py", REPOSITORY_ROOT / "tools" / "container" / "Dockerfile"]
    return callers


def _one_nested_run(arguments: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
    """A pytest session of this checkout's own, which is where a schedule can be read."""
    return subprocess.run(
        [sys.executable, "-m", "pytest", *arguments, "-p", "no:cacheprovider"],
        cwd=REPOSITORY_ROOT,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def test_the_install_module_names_the_shared_group_once() -> None:
    """The group's name is a declared constant, not a string repeated on decorators.

    Two decorators carrying two spellings of nearly the same name are two
    groups, and two groups are two workers, which is the failure this whole
    change is about. A constant is the one place the name can be read from.
    """
    assert _declared_group()


def test_every_test_that_touches_the_process_table_carries_the_shared_group() -> None:
    """Planters and the test that asserts absence run under one `xdist_group`.

    Found by what the test does, not by a list kept by hand: a test that calls
    one of the planting helpers, or that asserts step 5's sentence for a machine
    with nothing running, is a party whether or not anybody remembered it.
    """
    declared = getattr(install_scripts, GROUP_CONSTANT, None)
    group = declared if isinstance(declared, str) and declared.strip() else SHARED_GROUP
    parties = _parties_to_the_process_table()
    assert parties, (
        f"no test in {INSTALL_MODULE_PATH} plants or asserts on an agent shaped process, "
        "so this pin is reading the wrong module or the helpers were renamed"
    )
    ungrouped = sorted(name for name, function in parties.items() if _group_names(function) != [group])
    # Said in the same sentence rather than left to the pin above, so a tree that
    # declares neither the constant nor the marks fails once with both facts on
    # it instead of sending the reader to a second failure for the name.
    fallback = (
        ""
        if declared is not None
        else f" (and {INSTALL_MODULE_PATH} declares no {GROUP_CONSTANT}, so that name is this module's fallback)"
    )
    assert not ungrouped, (
        "these tests share the machine's process table and must carry "
        f"@pytest.mark.xdist_group({group!r}) on the test function itself: " + ", ".join(ungrouped) + fallback
    )


def test_no_other_test_in_the_install_module_is_pinned_to_that_worker() -> None:
    """The group stays as small as the problem.

    Everything else in the module is independent, and a group that grew to hold
    it would turn a parallel run of the module back into a serial one.
    """
    parties = _parties_to_the_process_table()
    strays = sorted(
        name for name, function in _test_functions().items() if name not in parties and _group_names(function)
    )
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


def test_the_suite_configuration_takes_the_scheduler_and_not_the_workers() -> None:
    """The scheduler comes from the configuration; the worker count stays a command line.

    A bare `pytest` in a checkout is one process, so a failure is read without a
    worker id in front of it, and what each runner spends on cores stays that
    runner's decision. `--dist loadgroup` on its own is inert, which is what
    makes it safe to configure; `-n` on its own is not.
    """
    options = shlex.split(_addopts())
    workers = [option for option in options if option == "-n" or option.startswith(("-n", "--numprocesses"))]
    assert not workers, f"pyproject.toml's addopts asks for workers as well, so a bare pytest is no longer one process: {workers}"


def test_a_run_that_asks_for_no_workers_is_still_one_process() -> None:
    """And the same claim off a real session, because it is the one this change risks.

    The configuration now carries an xdist option, which is exactly the move the
    comment beside the dependency used to warn against. A session without `-n`
    reports no worker id at all, which is what says the scheduler option alone
    started nothing.
    """
    completed = _one_nested_run(
        [f"{THIS_MODULE_PATH}::test_the_suite_configuration_keeps_refusing_an_undeclared_marker", "-v"],
        timeout=QUICK_NESTED_RUN_TIMEOUT_S,
    )
    transcript = completed.stdout + completed.stderr
    assert completed.returncode == 0, transcript
    assert "[gw" not in completed.stdout, f"a run without -n started workers:\n{transcript}"


def test_the_scheduler_plugin_is_a_declared_dependency_of_the_checkout() -> None:
    """`pytest-xdist` stops being a convenience the moment `addopts` names its option.

    Without the plugin, an environment that reads this configuration cannot run
    one test: pytest refuses the whole session with `unrecognized arguments:
    --dist`. The declaration in the `dev` extra and the pin in the locked file
    are what every environment that reads `addopts` installs it from.
    """
    dev = _pyproject()["project"]["optional-dependencies"]["dev"]
    assert any(requirement.replace(" ", "").startswith("pytest-xdist") for requirement in dev), dev
    locked = (REPOSITORY_ROOT / "requirements" / "dev.txt").read_text(encoding="utf-8")
    assert re.search(r"(?m)^pytest-xdist==", locked), "requirements/dev.txt does not pin pytest-xdist"


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
    back to being inert. Every workflow in this repository is read, along with
    the Linux runner and the container invocation.
    """
    owned = [
        REPOSITORY_ROOT / ".github" / "workflows" / "ci.yml",
        REPOSITORY_ROOT / ".github" / "workflows" / "hardware-bench.yml",
        REPOSITORY_ROOT / "tools" / "ci_linux.py",
        REPOSITORY_ROOT / "tools" / "container" / "Dockerfile",
    ]
    callers = _suite_callers()
    missing = sorted(str(path.relative_to(REPOSITORY_ROOT).as_posix()) for path in owned if path not in callers)
    assert not missing, f"these callers of the suite were renamed or removed, so this pin no longer reads them: {missing}"
    offenders = []
    for caller in callers:
        for number, line in enumerate(caller.read_text(encoding="utf-8").splitlines(), start=1):
            if re.search(r"--dist[ =]", _without_comments(line)):
                offenders.append(f"{caller.relative_to(REPOSITORY_ROOT).as_posix()}:{number}: {line.strip()}")
    assert not offenders, "these callers pass a --dist of their own, which overrides the configured one:\n" + "\n".join(
        offenders
    )


def test_the_document_that_says_how_to_run_the_suite_says_where_the_scheduler_comes_from() -> None:
    """CONTRIBUTING.md is where this repository tells a contributor how to run its suite.

    Its paragraph about `xdist_group` used to end by telling the reader to add
    `--dist loadgroup` to whichever command line was meant to honour a group.
    With the option in `addopts` that instruction is wrong twice over: the
    command lines already have it, and a contributor who follows it writes the
    one thing this suite pins against.
    """
    if not CONTRIBUTING.is_file():
        pytest.skip("CONTRIBUTING.md is repository content and does not ship in a source distribution")
    text = CONTRIBUTING.read_text(encoding="utf-8")
    assert "xdist_group" in text, "CONTRIBUTING.md no longer says anything about the group marker"
    paragraphs = [block for block in text.split("\n\n") if "--dist loadgroup" in block]
    assert paragraphs, "CONTRIBUTING.md no longer names the scheduler the grouped tests need"
    silent = [block for block in paragraphs if "addopts" not in block]
    assert not silent, "these paragraphs name --dist loadgroup without saying it comes from addopts:\n" + "\n\n".join(
        silent
    )


def test_step_fives_scan_still_reads_the_whole_process_table() -> None:
    """The installer is not the thing that changes, and this is why.

    `Get-RunningAgentProcessId` enumerates every process on the machine and
    matches on the program being run. A scan narrowed to a directory, a
    temporary path or the script's own root would make this suite quiet and make
    an operator's real CLI invisible to the block that asks them to restart it.
    """
    source = (REPOSITORY_ROOT / "install.ps1").read_text(encoding="utf-8")
    start = source.find("function Get-RunningAgentProcessId")
    assert start != -1, "install.ps1 declares no Get-RunningAgentProcessId, which is the scan step 5 reads"
    end = source.find("\nfunction ", start + 1)
    body = source[start:] if end == -1 else source[start:end]
    assert "Get-CimInstance -ClassName Win32_Process" in body, body
    assert "-Filter" not in body, body
    for narrowing in ("PSScriptRoot", "env:TEMP", "env:TMP", "$PWD", "CurrentDirectory"):
        assert narrowing not in body, f"step 5's scan narrowed itself to {narrowing}, so a real agent CLI can hide from it"


def _without_group_suffix(reported: str) -> str:
    """The node id as it was asked for, with the group pytest-xdist appends taken off.

    pytest-xdist (3.8.0, the release `requirements/dev.txt` pins) reports a
    grouped test as `<node id>@<group name>`, so a lookup by the plain node id
    finds nothing exactly when the grouping worked, and the assertion above
    would read as a schedule that never happened.
    """
    for group in {SHARED_GROUP, getattr(install_scripts, GROUP_CONSTANT, SHARED_GROUP)}:
        suffix = f"@{group}"
        if reported.endswith(suffix):
            return reported[: -len(suffix)]
    return reported


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
    It also pays for the claim by running both installs a second time, one after
    the other on one worker, which is the price of reading a schedule off a real
    session instead of off a mark.
    """
    parties = sorted(_parties_to_the_process_table())
    assert len(parties) >= 2, parties
    node_ids = [f"{INSTALL_MODULE_PATH}::{name}" for name in parties]
    completed = _one_nested_run([*node_ids, "-n", "2", "--dist", "loadgroup", "-v"], timeout=NESTED_RUN_TIMEOUT_S)
    transcript = completed.stdout + completed.stderr
    workers: dict[str, set[str]] = {}
    for line in completed.stdout.splitlines():
        match = re.match(r"\[(gw\d+)\] \[\s*\d+%\] (\w+) (\S+)", line)
        if match is not None:
            workers.setdefault(_without_group_suffix(match.group(3)), set()).add(match.group(1))
    reported = {node_id: workers.get(node_id, set()) for node_id in node_ids}
    assert all(reported.values()), f"the nested run reported no worker for some of the group: {reported}\n{transcript}"
    on = sorted({worker for seen in reported.values() for worker in seen})
    assert len(on) == 1, f"the group straddled {len(on)} workers ({', '.join(on)}): {reported}\n{transcript}"
    assert completed.returncode == 0, transcript
