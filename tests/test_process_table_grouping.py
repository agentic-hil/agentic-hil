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

Two suites started at once on one machine meet in the table all the same: from
two clones, two worktrees or one checkout run twice, each with a scheduler that
has never heard of the other (#567). So every party also holds one lock that
every run of the suite on the machine takes, for as long as its process lives
and its scan runs. The pins below read the lock off the parties the way they
read the group, and show the collision it prevents between two real sessions.
"""

from __future__ import annotations

import inspect
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path

import pytest
import support
from support import scaled_time_bound

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
# rather than off a list somebody has to remember to extend. The first three are
# the helpers that put an agent shaped process into the table: the interpreter
# wearing npm's shape, the child that lingers while the installer runs, and the
# process in the shape a recording found the CLI in. The fourth is the sentence
# step 5 prints when it found none, which is the assertion a stranger's planted
# process turns red.
PLANTING_HELPERS = ("_a_node_shaped_interpreter", "_a_process_that_lingers", "_an_agent_cli_as_recorded")
ABSENCE_SENTENCE = "no agent CLI of yours is running"
# How a party takes the lock that keeps two sessions on one machine apart
# (#567). Like GROUP_CONSTANT, these names are the shape the pins prescribe
# beyond what the issue decided: a fixture each party asks for by name on the
# test function itself, which holds `support.hold_the_process_table` on the one
# path `support.PROCESS_TABLE_LOCK` names. Read through `getattr`, so a tree
# without them fails on assertions that say so rather than on an import.
LOCK_FIXTURE = "process_table_lock"
LOCK_PATH = "PROCESS_TABLE_LOCK"
LOCK_HELPER = "hold_the_process_table"
# The install module's test that plants `opencode` in its recorded shape and
# asks step 5's matcher for it, on either platform: what each of the two
# sessions below runs.
MEETING_PARTY = "test_step_five_names_the_agent_cli_this_test_started_in_the_real_process_table"

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
# The two sessions that meet each collect the install module and run one test,
# which asks one Windows PowerShell or one `sh` for one PID and waits for the
# other session at most twice. Kept apart, the second also waits for the first
# to finish.
MEETING_RUN_TIMEOUT_S = 300
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


def _grouped_here() -> dict[str, object]:
    """The tests of this module that carry the group, because the sessions they start plant into the table."""
    return {
        name: value
        for name, value in globals().items()
        if name.startswith("test_") and callable(value) and _group_names(value)
    }


def _takes_the_lock(function: object) -> bool:
    """Whether a test asks for the lock's fixture itself, as a parameter or through `usefixtures` on the function."""
    if LOCK_FIXTURE in inspect.signature(function).parameters:
        return True
    return any(
        mark.name == "usefixtures" and LOCK_FIXTURE in mark.args for mark in getattr(function, "pytestmark", [])
    )


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
        timeout=scaled_time_bound(timeout),
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


def test_every_party_to_the_process_table_holds_the_machine_wide_lock() -> None:
    """The group keeps one session's parties apart, and the lock keeps two sessions' apart (#567).

    `--dist loadgroup` schedules inside one session. A second session on the
    same machine has a scheduler of its own that has never heard of the first,
    and both read one process table. So every party holds one lock that every
    session on the machine takes: the planters and the absence check, found the
    way the pin above finds them, and the tests of this module whose nested
    sessions plant the same processes.
    """
    here = _grouped_here()
    assert "test_two_workers_run_the_whole_group_on_one_of_them" in here, sorted(here)
    parties = {f"{INSTALL_MODULE_PATH}::{name}": function for name, function in _parties_to_the_process_table().items()}
    parties.update({f"{THIS_MODULE_PATH}::{name}": function for name, function in here.items()})
    unlocked = sorted(node_id for node_id, function in parties.items() if not _takes_the_lock(function))
    assert not unlocked, (
        "these tests share the machine's process table with every other run of the suite on it, and must ask for "
        f"the `{LOCK_FIXTURE}` fixture on the test function itself: " + ", ".join(unlocked)
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


def test_the_document_that_says_how_to_run_the_suite_says_how_two_runs_share_the_process_table() -> None:
    """CONTRIBUTING.md names the lock beside the group it completes (#567).

    A second run on the machine now waits for the first run's step 5 tests,
    and one that waits out its bound fails naming a process that is not its
    own. Without the paragraph a contributor reads the first as a hang and the
    second as a stranger's bug. So the paragraph that names the fixture a party
    asks for also names the file every run takes, says that a second run
    waits and then fails naming the PID holding it, and says the file belongs
    to one account.
    """
    if not CONTRIBUTING.is_file():
        pytest.skip("CONTRIBUTING.md is repository content and does not ship in a source distribution")
    lock = getattr(support, LOCK_PATH, None)
    assert isinstance(lock, Path), f"tests/support.py declares no {LOCK_PATH} ({lock!r}), so there is no file to document"
    where = f"~/{lock.relative_to(support.REAL_HOME).as_posix()}"
    text = CONTRIBUTING.read_text(encoding="utf-8")
    paragraphs = [block for block in text.split("\n\n") if f"`{LOCK_FIXTURE}`" in block]
    assert paragraphs, f"CONTRIBUTING.md never names the `{LOCK_FIXTURE}` fixture a party to the process table asks for"
    facts = {
        "the file every run takes": where,
        "that a second run waits": "waits",
        "that a run that waited out the bound fails naming the holder's PID": "PID",
        "that the file belongs to one account": "account",
    }
    unsaid = [fact for fact, needle in facts.items() if not any(needle in block for block in paragraphs)]
    assert not unsaid, f"the paragraph naming `{LOCK_FIXTURE}` does not say {', '.join(unsaid)}:\n" + "\n\n".join(paragraphs)


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


# Another run of the suite on this machine, as far as the lock can tell: a
# process of its own that takes the lock at the path it is given, says the PID
# it holds it with, and keeps it until its stdin closes. Its own PID rather
# than the one `Popen` returns, because a virtual environment's python.exe on
# Windows is a launcher, and it is the interpreter it starts that holds the lock.
_HOLDER = """
import os
import sys
from pathlib import Path

sys.path.insert(0, sys.argv[1])
import support

with support.hold_the_process_table(sys.argv[3], wait_s=60, path=Path(sys.argv[2])):
    support.publish_atomically(sys.argv[4], str(os.getpid()))
    sys.stdin.read()
"""


def _the_lock_helper() -> Callable[..., object]:
    helper = getattr(support, LOCK_HELPER, None)
    assert callable(helper), (
        f"tests/support.py offers no {LOCK_HELPER} ({helper!r}), so nothing keeps two runs of the suite on this "
        "machine out of each other's process table"
    )
    return helper


def _a_session_holding_the_table(tmp_path: Path, lock: Path, holder: str) -> tuple[subprocess.Popen[str], int]:
    """A process of its own holding the lock at `lock` for `holder`, and the PID it holds it with."""
    held = tmp_path / "held"
    started = subprocess.Popen(
        [sys.executable, "-c", _HOLDER, str(Path(support.__file__).resolve().parent), str(lock), holder, str(held)],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.monotonic() + scaled_time_bound(60)
    while not support.published(held) and started.poll() is None and time.monotonic() < deadline:
        time.sleep(0.05)
    if not support.published(held):
        started.kill()
        _, said = started.communicate(timeout=scaled_time_bound(60))
        raise AssertionError(f"the process meant to hold {lock} for {holder} never said it did:\n{said}")
    return started, int(support.read_when_published(held))


def test_the_process_table_lock_is_one_path_for_every_session_on_the_machine(tmp_path: Path) -> None:
    """Every run of the suite on the machine meets at one lock, named once when support is imported (#567).

    Any clone, any worker, any session: so the path comes from nothing a
    checkout, a worker or a test's sandbox decides. It is read the way
    `support.REAL_HOME` is read, at import, before any test has moved the home
    it lies under, and a session started from another clone as another worker
    arrives at the same file. It lies beside the suite's other machine-wide
    lock, the one `tools/run_lock.py` keeps in `~/.agentic-hil`.
    """
    lock = getattr(support, LOCK_PATH, None)
    assert isinstance(lock, Path), (
        f"tests/support.py declares no {LOCK_PATH} ({lock!r}), so the runs of the suite on this machine have no "
        "one lock to meet at"
    )
    assert lock.is_absolute(), lock
    assert lock.is_relative_to(support.REAL_HOME), f"{lock} is not under the home support read at import, {support.REAL_HOME}"
    assert lock == support.REAL_HOME / ".agentic-hil" / "pytest-process-table.lock", (
        f"{lock} is not ~/.agentic-hil/pytest-process-table.lock, beside the lock tools/run_lock.py keeps there"
    )
    for own in (Path(os.path.expanduser("~")), Path(tempfile.gettempdir()), tmp_path, REPOSITORY_ROOT):
        assert not lock.is_relative_to(own), f"{lock} lies under {own}, which belongs to this test or this checkout alone"
    another_clone = tmp_path / "another-clone" / "tests"
    another_clone.mkdir(parents=True)
    shutil.copy2(support.__file__, another_clone / "support.py")
    elsewhere = subprocess.run(
        [
            sys.executable,
            "-c",
            f"import sys; sys.path.insert(0, sys.argv[1]); import support; print(support.{LOCK_PATH})",
            str(another_clone),
        ],
        cwd=another_clone,
        env={
            **os.environ,
            "HOME": str(support.REAL_HOME),
            "USERPROFILE": str(support.REAL_HOME),
            "PYTEST_XDIST_WORKER": "gw7",
            "PYTHONIOENCODING": "utf-8",
        },
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=scaled_time_bound(60),
        check=False,
    )
    assert elsewhere.returncode == 0, elsewhere.stderr
    assert elsewhere.stdout.strip() == str(lock), (
        f"a session started from another clone as another worker names {elsewhere.stdout.strip()}, and this one {lock}"
    )


def test_a_session_that_cannot_get_the_process_table_names_the_one_holding_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A party that waited out its bound fails saying who holds the table (#567).

    Two runs on one machine now wait for each other, and a wait can end
    without the table: the other run is stuck, or simply long. A bare timeout
    at that point sends the reader after the wrong run, so the failure names
    the PID holding the lock and the test or run it holds it for. The bound is
    a base figure widened by the runner's one time scale, like every other
    wall-clock bound in the suite.

    The lock lies in a directory nobody has made yet, as `~/.agentic-hil` is on
    a machine that has never run the product or the Linux runner, so the first
    run to take it makes the directory.
    """
    hold = _the_lock_helper()
    lock = tmp_path / "home" / ".agentic-hil" / "process-table.lock"
    alice = "tests/test_install_scripts.py::test_alice_holds_the_table"
    holder, holder_pid = _a_session_holding_the_table(tmp_path, lock, alice)
    monkeypatch.setenv(support.TIME_SCALE_VARIABLE, "4")
    try:
        began = time.monotonic()
        with pytest.raises((AssertionError, pytest.fail.Exception)) as refused, hold(
            "tests/test_install_scripts.py::test_bob_waits_for_it", wait_s=0.5, path=lock
        ):
            pass
        waited = time.monotonic() - began
    finally:
        holder.communicate(timeout=scaled_time_bound(60))
    said = str(refused.value)
    assert waited >= 1.9, f"the wait for the table ended after {waited:.2f}s, and 0.5s widened by a time scale of 4 is 2s"
    assert str(holder_pid) in said and alice in said, (
        f"a run that could not get the table was told {said!r}, which does not name its holder, PID {holder_pid} for {alice}"
    )


def test_the_process_table_is_free_again_as_soon_as_its_holder_is_gone(tmp_path: Path) -> None:
    """A run that dies holding the lock does not hold it any more.

    A run ended in the middle of a party, by Ctrl-C, a runner's timeout or a
    crash, never reaches the line that lets go, and every run on the machine
    after it would wait out its bound and fail naming a process that no longer
    exists. So what lets go is the operating system, when the holder's process
    ends, and the next run takes the table then.
    """
    hold = _the_lock_helper()
    lock = tmp_path / "process-table.lock"
    holder, holder_pid = _a_session_holding_the_table(tmp_path, lock, "tests/test_install_scripts.py::test_alice_holds_the_table")
    # Ended from outside, the way a crash ends it: nothing in the holder runs after this.
    ending = threading.Timer(1.0, os.kill, (holder_pid, getattr(signal, "SIGKILL", signal.SIGTERM)))
    try:
        began = time.monotonic()
        ending.start()
        with hold("tests/test_install_scripts.py::test_bob_waits_for_it", wait_s=30, path=lock):
            waited = time.monotonic() - began
    finally:
        ending.cancel()
        ending.join()
        holder.communicate(timeout=scaled_time_bound(60))
    assert waited >= 0.9, f"the table was taken {waited:.2f}s in, while the process holding it was still running"
    assert waited < scaled_time_bound(20), f"the table was taken {waited:.1f}s in, long after the process holding it was gone"


@pytest.mark.xdist_group(SHARED_GROUP)
def test_two_sessions_on_one_machine_never_read_each_others_agent_cli(tmp_path: Path) -> None:
    """The collision the group cannot prevent, read off two real sessions started at the same moment (#567).

    Each session runs the install module's test that plants `opencode` in the
    shape its recording found it in and asks step 5's matcher for it in the
    real process table, and the two meet through a directory both write into
    (see `PROCESS_TABLE_MEETING` there): a session that has planted waits for
    the other one to plant before it asks, and keeps its stand-in until the
    other one has asked. Each session has a scheduler of its own and neither
    has heard of the other, so unless the lock keeps them apart both stand-ins
    are in the table for both questions, and the matcher names one of them for
    both.

    This test is a party itself: the sessions it starts plant into the table
    every run on the machine shares, so it carries the group and takes the
    lock. They inherit this test's sandbox, and the lock they meet at lies
    under its home, not where this test holds its own.
    """
    install_scripts._skip_where_no_stand_in_can_be_planted()
    meeting = tmp_path / "meeting"
    meeting.mkdir()
    environment = {**os.environ, install_scripts.PROCESS_TABLE_MEETING: str(meeting)}
    # Short names, because on Windows each session's stand-in lies under its
    # basetemp at the depth npm's prefix gives it, and MAX_PATH counts all of it.
    transcripts = [tmp_path / f"s{number}.txt" for number in (1, 2)]
    sessions: list[subprocess.Popen[bytes]] = []
    try:
        for number, transcript in enumerate(transcripts, start=1):
            with transcript.open("wb") as output:
                sessions.append(
                    subprocess.Popen(
                        [
                            sys.executable,
                            "-m",
                            "pytest",
                            f"{INSTALL_MODULE_PATH}::{MEETING_PARTY}",
                            "-q",
                            "-p",
                            "no:cacheprovider",
                            f"--basetemp={tmp_path / f's{number}'}",
                        ],
                        cwd=REPOSITORY_ROOT,
                        env=environment,
                        stdout=output,
                        stderr=subprocess.STDOUT,
                    )
                )
        for session in sessions:
            session.wait(timeout=scaled_time_bound(MEETING_RUN_TIMEOUT_S))
    finally:
        for session in sessions:
            if session.poll() is None:
                session.kill()
                session.wait(timeout=scaled_time_bound(60))
    said = "\n".join(
        f"--- session {number} ---\n{transcript.read_text(encoding='utf-8', errors='replace')}"
        for number, transcript in enumerate(transcripts, start=1)
    )
    planted = install_scripts._meeting_marks(meeting, "planted")
    asked = install_scripts._meeting_marks(meeting, "asked")
    assert len(planted) == 2, f"two sessions were started and {len(planted)} of them planted a stand-in:\n{said}"
    stand_ins = {session: marks["stand_in"] for session, marks in planted.items()}
    crossed = sorted(
        (stand_ins[session], answer["named"])
        for session, answer in asked.items()
        if answer["named"].isdigit() and int(answer["named"]) in set(stand_ins.values()) - {stand_ins[session]}
    )
    assert not crossed, "two sessions on one machine read each other's agent CLI: " + "; ".join(
        f"the session that planted {mine} was told step 5 found {theirs}, which the other session planted"
        for mine, theirs in crossed
    )
    assert [session.returncode for session in sessions] == [0, 0], said
    apart = [mine for mine, marks in planted.items() if set(planted) - {mine} <= set(marks["gone_before_it_started"])]
    assert apart, (
        "both stand-ins were in the table at the same time, so nothing kept the two sessions apart and their answers "
        f"were right by luck: {planted}\n{said}"
    )


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
