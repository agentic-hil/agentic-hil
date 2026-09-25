from __future__ import annotations

import json
import os
import shutil
import struct
import subprocess
import tempfile
import threading
import time
import uuid
from collections.abc import Generator, Iterator
from contextlib import suppress
from pathlib import Path

import pytest
import yaml

pytest_plugins = ["pytester", "suite_ledger"]

from support import (  # noqa: E402
    hold_the_process_table,
    remove_trusted_launcher,
    scaled_time_bound,
    sweep_stale_launchers,
)

# The real index reader, bound before the autouse fixture below replaces the
# name it lives under. A test about the reader itself calls this one, and every
# other test gets the stub.
from agentic_hil.upgrade import _newest_released_version as read_the_release_index  # noqa: E402,F401

# Where every test's isolated HOME, config, state and temporary storage go.
# Resolved once, here, out of the system temp root and before any test has
# redirected anything.
#
# It used to be derived from `tmp_path.parent`, which follows `--basetemp`
# wherever it is pointed -- including inside this clone, which is what a
# developer does to keep a scratch path off Windows MAX_PATH. An isolated HOME
# under the working directory is a home inside a project, and the user-level
# half of `agent-install` refuses exactly that through `_external_user_path`, so
# a basetemp choice decided whether unrelated tests passed. The system temp root
# is never inside the repository, whatever the basetemp is.
#
# Per process, so two suites on one machine cannot meet here, and short, because
# every sandbox path is derived from it and MAX_PATH is the reason a basetemp
# gets moved in the first place. On POSIX the parent is /tmp itself rather than
# gettempdir(): macOS answers gettempdir() with its /var/folders/... token path,
# which is long enough that a per-test TMPDIR derived from it pushes the CAN
# broker's AF_UNIX socket addresses past the platform's 104-byte limit, and the
# whole macOS CI matrix failed on exactly that. /tmp is the shortest root every
# POSIX platform has; resolved, because on macOS /tmp is itself a symlink to
# /private/tmp and the path rules refuse a component that is a symlink, so the
# sandbox has to carry the real spelling. The per-process directory under it is
# this user's own, and everything below stays per-test as before.
SANDBOX_PREFIX = "ahil-pt-"
_SANDBOX_PARENT = Path("/tmp").resolve() if os.name == "posix" else Path(tempfile.gettempdir()).resolve()
SANDBOX_ROOT = _SANDBOX_PARENT / f"{SANDBOX_PREFIX}{os.getpid()}"
# How long a sibling root may sit untouched before a later session sweeps it. A
# live session creates and removes a sandbox inside its own root on every single
# test, so a root this old is residue: a directory Windows refused to delete
# because a detached child still held a lock file when the test that spawned it
# ended. Under the old location pytest's own numbered root eventually collected
# such leftovers; here nothing else on the machine knows what this directory is.
# Generous, because the cost of being wrong is deleting a live suite's sandbox.
STALE_SANDBOX_AGE_S = 6 * 60 * 60


@pytest.fixture(scope="session", autouse=True)
def _clean_up_trusted_launcher() -> Iterator[None]:
    """Remove the session's trusted launcher, wherever a test created it.

    The sweep on the way in is what catches the sessions that never reached the
    finalizer on the way out: a run that is killed or crashes leaves its
    launcher in the real home under its own pid, and nothing else on the machine
    knows what that directory is. It only ever removes a sibling whose pid names
    no live process, so a suite running beside this one keeps its own."""
    sweep_stale_launchers()
    yield
    remove_trusted_launcher()


@pytest.fixture(scope="session", autouse=True)
def _clean_up_sandbox_root() -> Iterator[None]:
    """Keep this session's sandbox root to itself, and take nothing with it.

    Each test's own sandbox is removed by its own finalizer; this is what
    catches a test that died before its finalizer ran, and the root directory
    itself."""
    sweep_stale_sandbox_roots()
    yield
    shutil.rmtree(SANDBOX_ROOT, ignore_errors=True)


def sweep_stale_sandbox_roots(now: float | None = None) -> list[Path]:
    """Remove sandbox roots no session has touched in STALE_SANDBOX_AGE_S.

    Returns what it removed. A root belonging to a suite running beside this one
    is fresh by construction and is never touched, which is the whole property
    that matters here."""
    cutoff = (time.time() if now is None else now) - STALE_SANDBOX_AGE_S
    removed: list[Path] = []
    for candidate in SANDBOX_ROOT.parent.glob(f"{SANDBOX_PREFIX}*"):
        if candidate == SANDBOX_ROOT:
            continue
        with suppress(OSError):
            if candidate.is_dir() and candidate.stat().st_mtime < cutoff:
                shutil.rmtree(candidate, ignore_errors=True)
                removed.append(candidate)
    return removed


class SandboxEscaped(AssertionError):
    """A test resolved a user-level path back onto the operator's own profile."""


def user_level_paths() -> tuple[Path, ...]:
    """Where this process would put user-level files if it wrote one right now.

    `Path.home()` is where all three agent integrations live: `~/.claude.json`
    and `~/.claude/skills`, `~/.codex`, `~/.config/opencode`. `tool_owned_user_roots`
    names every tree Agentic HIL creates for itself, the authoritative
    configurations, the state root and `~/.agentic-hil`, both where the
    environment points now and where a profile with the variable unset falls
    back to. Each of them is read out of the variables `isolated_config_environment`
    redirects, and neither call opens or creates anything, which is what makes
    asking cheap enough to do around every test.
    """
    from agentic_hil.config import absolute_without_symlinks, tool_owned_user_roots

    return (absolute_without_symlinks(Path.home()), *tool_owned_user_roots())


# The same question answered here, at import time, before the first fixture has
# redirected anything: these are the operator's own directories, and no test may
# resolve back onto them. Compared case-insensitively because that is how Windows
# compares paths, and a test reaches these variables by writing strings into them.
REAL_USER_PATHS = frozenset(os.path.normcase(str(path)) for path in user_level_paths())


def assert_still_sandboxed(when: str) -> None:
    """Fail while the damage is still one CLI call away, naming who did it.

    The suite's whole safety story is that `isolated_config_environment` moves
    every user-level path into the sandbox, and until #270 any test could revert
    that with one line: `monkeypatch.undo()` in a test body reverted the redirect
    along with everything else recorded on the test's own instance. The three CLI
    calls that followed ran against the developer's real profile, and the
    `uninstall` among them removed all three installed agent skills and their MCP
    registrations. Nothing in the run looked wrong, which is why this has to be
    asked here rather than inferred later from what a profile is missing.

    The comparison is equality with the paths captured before the first redirect,
    not a prefix test against the sandbox: a test is free to point HOME at
    `tmp_path`, or through `pytester` at pytest's own basetemp, and those are
    isolated too. Only landing back on the real thing is the failure. On Windows
    a prefix test would also refuse the entire suite, because the per-user Temp
    root that holds the sandbox is itself inside the real profile.
    """
    escaped = [path for path in user_level_paths() if os.path.normcase(str(path)) in REAL_USER_PATHS]
    if not escaped:
        return
    raise SandboxEscaped(
        f"User-level paths point at the operator's own profile {when}: "
        + ", ".join(str(path) for path in escaped)
        + ". Something reverted the redirect `isolated_config_environment` installs, so an `agentic-hil` call from here"
        + " writes to, or uninstalls from, the real profile instead of the sandbox."
    )


@pytest.hookimpl(wrapper=True)
def pytest_runtest_call(item: pytest.Item) -> Generator[None, object, object]:
    """Check the sandbox on both sides of every test body.

    Before it runs, because a fixture can lose the redirect as easily as a test
    can, and the test about to run against the real profile is the one worth
    naming. After it has run, because that is where a test body's own doing shows
    up. In a `finally`, because a test that broke the isolation and then failed
    for its own reason is exactly the case where the breach is the news.
    """
    assert_still_sandboxed(f"before {item.nodeid} ran")
    try:
        result = yield
    finally:
        assert_still_sandboxed(f"after {item.nodeid} ran")
    return result


HEARTBEAT_THREAD_NAME = "agentic-hil-heartbeat"
_REPORTED_HEARTBEAT_THREADS: set[int] = set()


@pytest.hookimpl(wrapper=True)
def pytest_runtest_teardown(item: pytest.Item, nextitem: pytest.Item | None) -> Generator[None, object, object]:
    """No device hold outlives the test that took it.

    A `BenchMutex` refreshes every held device from a thread it starts with the
    first hold and stops with the last release. A test that drops its owner
    without releasing (a simulated crash, a coordinator never closed) leaves
    that thread running for the rest of the worker's life, writing into a
    sandbox that no longer exists, through helpers a later test may have
    patched: one such refresh landed inside a test asserting that its patched
    directory helper was never called, and the leg went red on a line the
    test never touched. After every finalizer has run, so a fixture's own close
    counts, the thread is named here rather than in whichever test it hits.
    A stopped thread leaves its wait at once; only a leaked one is still alive
    after the join. A leaked thread cannot be stopped from here and stays for
    the rest of the worker, so it is charged to the test that leaked it and to
    no test after.
    """
    result = yield
    pumps = [thread for thread in threading.enumerate() if thread.name == HEARTBEAT_THREAD_NAME]
    for thread in pumps:
        thread.join(timeout=2.0)
    leaked = [thread for thread in pumps if thread.is_alive() and thread.ident not in _REPORTED_HEARTBEAT_THREADS]
    _REPORTED_HEARTBEAT_THREADS.update(thread.ident for thread in leaked if thread.ident is not None)
    assert not leaked, f"{item.nodeid} left {len(leaked)} device heartbeat thread(s) running: a bench hold was never released"
    return result


ROOT = Path(__file__).resolve().parents[1]
FAKE_OPENOCD = ROOT / "tests" / "fixtures" / "fake_openocd.py"
FAKE_OPENOCD_NO_TARGET = ROOT / "tests" / "fixtures" / "fake_openocd_no_target.py"
FAKE_OPENOCD_NO_PROBE = ROOT / "tests" / "fixtures" / "fake_openocd_no_probe.py"
FAKE_OPENOCD_MISSING_CFG = ROOT / "tests" / "fixtures" / "fake_openocd_missing_cfg.py"
FAKE_OPENOCD_UNCONFIRMED = ROOT / "tests" / "fixtures" / "fake_openocd_unconfirmed.py"
FAKE_OPENOCD_POST_INIT_UNCONFIRMED = ROOT / "tests" / "fixtures" / "fake_openocd_post_init_unconfirmed.py"
FAKE_OPENOCD_ERASE_REFUSED = ROOT / "tests" / "fixtures" / "fake_openocd_erase_refused.py"
FAKE_STLINK = ROOT / "tests" / "fixtures" / "fake_stlink.py"
FAKE_STLINK_UNCONFIRMED = ROOT / "tests" / "fixtures" / "fake_stlink_unconfirmed.py"
FAKE_STLINK_READ_UNCONFIRMED = ROOT / "tests" / "fixtures" / "fake_stlink_read_unconfirmed.py"
FAKE_STLINK_SHORT_READ = ROOT / "tests" / "fixtures" / "fake_stlink_short_read.py"
FAKE_STLINK_HALT_UNCONFIRMED = ROOT / "tests" / "fixtures" / "fake_stlink_halt_unconfirmed.py"
FAKE_STLINK_PARTIAL_CONFIRMATION = ROOT / "tests" / "fixtures" / "fake_stlink_partial_confirmation.py"
FAKE_STLINK_NO_TARGET = ROOT / "tests" / "fixtures" / "fake_stlink_no_target.py"
FAKE_STLINK_NO_PROBE = ROOT / "tests" / "fixtures" / "fake_stlink_no_probe.py"
FAKE_STLINK_ERASE_REFUSED = ROOT / "tests" / "fixtures" / "fake_stlink_erase_refused.py"
FAKE_STLINK_ERASE_MID_FLASH = ROOT / "tests" / "fixtures" / "fake_stlink_erase_mid_flash.py"
FAKE_PYOCD = ROOT / "tests" / "fixtures" / "fake_pyocd.py"
FAKE_PYOCD_NO_TARGET = ROOT / "tests" / "fixtures" / "fake_pyocd_no_target.py"
FAKE_PYOCD_SILENT_READ = ROOT / "tests" / "fixtures" / "fake_pyocd_silent_read.py"
FAKE_PYOCD_UNKNOWN_TARGET = ROOT / "tests" / "fixtures" / "fake_pyocd_unknown_target.py"
FAKE_PYOCD_ERASE_REFUSED = ROOT / "tests" / "fixtures" / "fake_pyocd_erase_refused.py"
FAKE_GDB = ROOT / "tests" / "fixtures" / "fake_gdb.py"


# A run leaves the working tree the way it found it (#544). A full run once left
# an 18 MB pip cache in the repository root and passed, and nothing in the run
# said so: the files were simply there afterwards, untracked, until someone
# looked. So the session lists the checkout's untracked, not ignored files when
# it starts and again when it finishes, and a file that appeared in between
# fails the run by name. What was already untracked is the developer's and does
# not count. Nothing is deleted, because what a test wrote is the evidence of
# which test wrote it, and every test keeps its own outcome, so nobody hunts for
# a failing test that does not exist.
#
# Only the process that sees the whole session looks: the xdist controller, or
# the one process of a run without workers. A worker starts after the
# controller and shares its tree, and a session a test starts (two tests in
# this suite start one of this checkout) begins and ends while the run around
# it keeps writing, so the listings of either would compare a tree other tests
# are changing. Such a session is told by the PYTEST_CURRENT_TEST pytest sets for
# the test that started it, which the session inherits; the run around it
# checks the tree, the session's files included. A tree without a `.git` of
# its own is left alone: the sdist gate in CI collects the suite inside an
# unpacked archive within the CI checkout, where git would answer for the
# outer repository. A machine without git runs the suite the way it did before
# the check existed.
TREE_LISTING_S = 60.0
_TREE_AT_START = pytest.StashKey[tuple[str, dict[str, str], frozenset[str]]]()
_TREE_NOT_CHECKED = pytest.StashKey[str]()


class _GitCouldNotList(Exception):
    """git did not list the tree; the message is its own last line, word for word."""


def _untracked_files(git: str, environment: dict[str, str]) -> frozenset[str]:
    """The checkout's untracked, not ignored files, named the way git names them from the root."""
    try:
        listed = subprocess.run(
            [git, "ls-files", "-z", "--others", "--exclude-standard"],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            timeout=scaled_time_bound(TREE_LISTING_S),
            check=False,
        )
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        raise _GitCouldNotList(str(error)) from error
    if listed.returncode != 0:
        said = listed.stderr.decode("utf-8", errors="replace").strip().splitlines()
        raise _GitCouldNotList(said[-1] if said else f"git exited with status {listed.returncode}")
    return frozenset(name for name in listed.stdout.decode("utf-8", errors="replace").split("\0") if name)


@pytest.hookimpl(tryfirst=True)
def pytest_sessionstart(session: pytest.Session) -> None:
    """List the tree before any test runs, and before xdist starts a worker that could write into it."""
    if hasattr(session.config, "workerinput") or "PYTEST_CURRENT_TEST" in os.environ or not (ROOT / ".git").exists():
        return
    git = shutil.which("git")
    if git is None:
        return
    # The finish is listed with this environment rather than the one the tests
    # leave behind. `--exclude-standard` reads the global excludes file, which
    # git finds through HOME, USERPROFILE and XDG_CONFIG_HOME. A test's
    # isolation puts those back when the test ends, even after a direct write,
    # but a fixture or hook that changes one of them outside a test's
    # isolation, by writing `os.environ` directly, leaves its value in place
    # for the rest of the session: listed that way, every file the developer
    # ignores globally would count as new.
    environment = dict(os.environ)
    try:
        session.config.stash[_TREE_AT_START] = (git, environment, _untracked_files(git, environment))
    except _GitCouldNotList as error:
        session.config.stash[_TREE_NOT_CHECKED] = f"git could not list it when the run started: {error}"


@pytest.hookimpl(wrapper=True, trylast=True)
def pytest_sessionfinish(session: pytest.Session) -> Generator[None, object, object]:
    """List the tree again once the session is over, and fail a passing run that left files in it.

    The innermost wrapper, so this runs after every plugin's own finish: after
    pytest has torn down what an early stop left set up and xdist has taken its
    workers down, and before the terminal reporter's summary. The report goes
    to the terminal from here rather than into a summary section, because
    `--no-summary` drops those, and the run would then fail with every line it
    printed saying that it passed.
    """
    result = yield
    config = session.config
    not_checked = config.stash.get(_TREE_NOT_CHECKED, None)
    new: list[str] = []
    if _TREE_AT_START in config.stash:
        git, environment, before = config.stash[_TREE_AT_START]
        try:
            after = _untracked_files(git, environment)
        except _GitCouldNotList as error:
            not_checked = f"git could not list it when the run finished: {error}"
        else:
            # pytest's own scratch space is not a stray, even inside the
            # checkout: a developer points `--basetemp` there to keep paths
            # short (see SANDBOX_ROOT), and every `tmp_path` of such a run lies
            # there, untracked and not ignored. Read the way pytest's own
            # tmpdir plugin reads it when the session finishes.
            basetemp = getattr(getattr(config, "_tmp_path_factory", None), "_basetemp", None)
            new = sorted(name for name in after - before if basetemp is None or not (ROOT / name).is_relative_to(basetemp))
    reporter = config.pluginmanager.get_plugin("terminalreporter")
    if reporter is not None and (new or not_checked):
        # A quiet run's progress line has no newline yet; pytest's own summary
        # adds one when it starts, and this comes first.
        reporter.write_line("")
        if not_checked:
            reporter.write_line(f"The working tree {ROOT} was not checked for files a test left behind: {not_checked}", yellow=True)
        else:
            reporter.write_sep("=", "a test wrote into the working tree", red=True)
            reporter.write_line(f"New since the run started, untracked, not ignored, and left in place in {ROOT}:")
            for name in new:
                reporter.write_line(f"  {name}")
    if new and session.exitstatus == pytest.ExitCode.OK:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
    return result



@pytest.fixture(autouse=True)
def isolated_config_environment(
    tmp_path: Path,
    request: pytest.FixtureRequest,
) -> Path:
    # Owner-only, and outside the repository whatever `--basetemp` says: see
    # SANDBOX_ROOT. Keeping user policy and state out of tmp_path itself still
    # matters, because tests use tmp_path as the workspace cwd.
    SANDBOX_ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    test_sandbox = SANDBOX_ROOT / uuid.uuid4().hex[:12]
    home_root = test_sandbox / "home"
    config_root = test_sandbox / "config"
    state_root = test_sandbox / "state"
    temp_root = test_sandbox / "tmp"
    request.addfinalizer(lambda: shutil.rmtree(test_sandbox, ignore_errors=True))
    # Deliberately not the `monkeypatch` fixture. That instance is the test's
    # own, and `monkeypatch.undo()` in a test body reverts everything recorded on
    # it, this redirect included; one test did exactly that, and the CLI calls
    # that followed installed into and then uninstalled from the developer's real
    # profile (#270). An instance the fixture owns is out of reach of anything a
    # test does to its own. The test's own is the `monkeypatch` below, which
    # depends on this fixture, so pytest rolls the test's changes back onto
    # these values before the finalizer registered for this instance runs, and
    # the environment unwinds in the order it was built whatever the fixtures
    # are called (#563).
    isolation = pytest.MonkeyPatch()
    request.addfinalizer(isolation.undo)
    home_root.mkdir(parents=True)
    temp_root.mkdir(parents=True)
    isolation.setenv("HOME", str(home_root))
    isolation.setenv("USERPROFILE", str(home_root))
    isolation.setenv("APPDATA", str(config_root))
    isolation.setenv("XDG_CONFIG_HOME", str(config_root))
    isolation.setenv("XDG_CACHE_HOME", str(home_root / ".cache"))
    isolation.setenv("XDG_DATA_HOME", str(home_root / ".local" / "share"))
    isolation.setenv("LOCALAPPDATA", str(state_root))
    isolation.setenv("XDG_STATE_HOME", str(state_root))
    # Temporary storage is redirected for the same reason HOME is. `tempfile`
    # answers out of TMPDIR/TEMP/TMP, and the one directory it otherwise names
    # is shared by every suite on the machine: two runs that reached
    # `tools/agent_review_loop.py` in the same second met in one scratch root
    # named after the repository and that second, and deleted each other's round
    # directories. Anything that asks the system for scratch space now lands in
    # this test's sandbox, so two sandboxes cannot meet. `gettempdir` caches its
    # answer, so the module attribute moves too; the variables alone would reach
    # child processes and not this one.
    for variable in ("TMPDIR", "TEMP", "TMP"):
        isolation.setenv(variable, str(temp_root))
    isolation.setattr(tempfile, "tempdir", str(temp_root))
    # The width every rendered result wraps to is redirected for the reason the
    # paths above are: it belongs to the runner, and a rendering test compares
    # text. `shutil.get_terminal_size` reads COLUMNS and LINES before it asks the
    # device, and nothing sets either for a pytest process. A pytest-xdist worker
    # on Linux is handed COLUMNS=80 and LINES=24 all the same: importing
    # `readline` in the parent has GNU readline write its own 80x24 default into
    # the C environment with `setenv`, where the parent's own `os.environ`
    # snapshot never sees it and every child it spawns inherits it. So one
    # document wrapped to 78 columns under `-n auto` and to 86 in a single
    # process, on Linux alone, and an assertion counting a command in the prose
    # passed or failed on how the suite had been started (#390). Pinned to the
    # width the module falls back to when nothing answers, so the rendering is
    # the same everywhere and is nobody's terminal.
    isolation.setenv("COLUMNS", "88")
    isolation.setenv("LINES", "24")
    isolation.delenv("AGENTIC_HIL_CONFIG", raising=False)
    return config_root


@pytest.fixture
def monkeypatch(isolated_config_environment: Path, monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """pytest's own `monkeypatch`, set up after the sandbox and undone before it, whoever asks for it.

    A change made through `monkeypatch` records the value it replaced, and for
    every variable the isolation redirects that value is the sandbox's, so the
    change has to be undone before the isolation is, or it puts the sandbox
    back after the isolation has restored the session. Without this fixture it
    was not: pytest sets a conftest's autouse fixtures up in the order their
    names sort, three of them take `monkeypatch` and sort ahead of the
    isolation, and the instance they share with the test was therefore created
    first and undone last. Every variable a test moved through it stayed on its
    deleted sandbox for the session fixtures, module fixtures and hooks after
    it on that worker, and a developer's own AGENTIC_HIL_CONFIG was gone for
    the rest of the session (#563).

    Depending on the isolation makes the order a matter of dependency instead
    of names. pytest resolves a fixture's name for the test that needs it, so
    this is the `monkeypatch` every requester under tests/ receives, the test
    itself, an autouse fixture or pytester alike, and asking for its own name
    reaches pytest's instance one level up. A fixture is set up after the
    fixtures it depends on and torn down before them, so this one is created
    once the sandbox is in place and undone while it still is, whatever any
    fixture is called.
    """
    return monkeypatch


@pytest.fixture(autouse=True)
def _no_host_stm32_toolchain(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bootstrap discovery finds no toolchain unless a test hands it one.

    `agentic-hil init` reads the attached bench whatever else is in the workspace,
    so any test that runs it would otherwise spawn whatever STM32CubeProgrammer is
    installed on the machine running the suite and connect to whatever is plugged
    into it: passing on a bare CI container and doing real hardware I/O on a
    developer's bench. A test that wants a toolchain patches this name itself, and
    that patch runs after this one.

    Both toolchains, because discovery has two ways in. With no
    STM32CubeProgrammer it enumerates ST-Link probes out of the host's USB serial
    inventory and drives them with OpenOCD, so a suite that hid only ST's CLI
    would take the fallback path on any machine with `openocd` on PATH and the
    skeleton path everywhere else: the same assertions answering differently per
    developer, which is what this fixture exists to prevent."""
    monkeypatch.setattr("agentic_hil.bootstrap.find_stm32_programmer_cli", lambda: None)
    monkeypatch.setattr("agentic_hil.bootstrap.find_openocd", lambda: None)


@pytest.fixture(autouse=True)
def _no_release_index_request(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test asks the index what the newest release is.

    `agentic-hil upgrade` reads the index before it may call an installation
    current, and a suite that let that request out would be slow behind a slow
    link and would answer differently on a machine with no network at all: the
    same assertions answering differently per developer, which is what the
    fixtures around this one exist to prevent. The default answer is the version
    under test, which is what every upgrade test written before the check was
    added assumed. A test about the check itself patches this name again, and
    that patch runs after this one."""
    from agentic_hil import __version__

    monkeypatch.setattr("agentic_hil.upgrade._newest_released_version", lambda: __version__)


@pytest.fixture(autouse=True)
def _no_host_gdb(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bootstrap discovery finds no GDB unless a test hands it one.

    The same rule as the toolchain above, for the same reason: discovery now
    reports the GDB this host answers with, and generation and `adopt-hardware`
    both write it. Left alone, every assertion about what a generated file
    contains or what adoption has left to carry would depend on whether the
    machine running the suite happens to have `arm-none-eabi-gdb` installed,
    which is true on a firmware developer's bench and false in a CI container. A
    test that wants a GDB patches this name itself."""
    monkeypatch.setattr("agentic_hil.bootstrap.autodetected_gdb", lambda: None)


# How long a test that reads the machine's process table waits for another run
# of the suite to let go of it, before the runner's time scale widens it. The
# longest hold is test_two_workers_run_the_whole_group_on_one_of_them in
# tests/test_process_table_grouping.py, whose nested run is bounded at 900 s,
# so a run queued behind it waits that out with room to spare.
PROCESS_TABLE_WAIT_S = 1200


@pytest.fixture
def process_table_lock(request: pytest.FixtureRequest) -> Iterator[None]:
    """Hold the machine's process table for the test that asks, against every other run of the suite (#567).

    `xdist_group` keeps the tests that plant a process shaped like an agent
    CLI, or assert that none is running, on one worker of one run. A second
    run on the machine, from another clone, another worktree or the same
    checkout, has a scheduler of its own and reads the same table, so each of
    those tests asks for this fixture by name as well. A run that finds the
    table held waits for it, and one that waits out the bound fails the test
    at setup naming the PID and the test holding it.
    """
    with hold_the_process_table(request.node.nodeid, wait_s=PROCESS_TABLE_WAIT_S):
        yield


def write_config(
    directory: Path,
    *,
    debugger_type: str = "openocd",
    debugger_executable: Path | None = None,
    probe_id: str | None = None,
    target_type: str | None = None,
    flash_address: str | None = None,
    gdb_executable: Path | None = None,
    allowed_symbols: list[str] | None = None,
    allow_all_symbols: bool | None = None,
    workspace_root: Path | None = None,
    state_root: Path | None = None,
    max_dump_size_bytes: int = 1048576,
    debuggers_yaml: str = "",
    com_ports_yaml: str = "com_ports: {}\n",
    can_buses_yaml: str = "can_buses: {}\n",
    permissions: dict[str, bool] | None = None,
    debugger_name: str = "dut",
    auto_probe_ids: bool = True,
    interface_cfg: str = "interface/stlink.cfg",
    target_cfg: str = "target/stm32f4x.cfg",
    # The budget a call written from this configuration runs under. Five seconds
    # suits the bulk of the suite, whose fakes answer at once. A test whose child
    # does real work hands this the same factor every wall-clock bound in the
    # suite takes, so a loaded host is granted the allowance rather than being
    # reported as a child that stopped working.
    timeout_s: float = 5,
    # Omitted from the written entry by default, so the bulk of the suite keeps
    # exercising the file that never named a connect mode and is read at the
    # default. A test that wants the key writes it.
    connect_mode: str | None = None,
    config_path: Path | None = None,
    auto_recover: str | None = None,
    recovery_max_attempts: int | None = None,
    config_version: int | None = None,
    allowed_roots: list[str] | None = None,
    omit_allowed_roots: bool = False,
) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    workspace_root = (workspace_root or directory).resolve()
    state_root = (state_root or Path(os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_STATE_HOME") or directory.parent / "user-state") / "agentic-hil").resolve()
    if debugger_executable is None:
        fake_by_type = {"stlink": FAKE_STLINK, "pyocd": FAKE_PYOCD}
        debugger_executable = fake_by_type.get(debugger_type, FAKE_OPENOCD)
    if allow_all_symbols is None:
        allow_all_symbols = allowed_symbols is None
    grants = DEFAULT_TEST_PERMISSIONS if permissions is None else permissions
    primary = {
        "type": debugger_type,
        "executable": debugger_executable.as_posix(),
        "probe_id": probe_id,
        "target_type": target_type,
        "interface": "SWD",
        "interface_cfg": interface_cfg,
        "target_cfg": target_cfg,
        "flash_address": flash_address,
        "timeout_s": timeout_s,
        **({"connect_mode": connect_mode} if connect_mode is not None else {}),
    }
    # Omitted entirely by default, so the common test config exercises the same
    # "policy was never named" path a config written before recovery existed has.
    recovery_lines = "".join(
        [
            "recovery:\n" if auto_recover is not None or recovery_max_attempts is not None else "",
            # Quoted: YAML 1.1 reads a bare `off` as the boolean False.
            f'  auto_recover: "{auto_recover}"\n' if auto_recover is not None else "",
            f"  max_attempts: {recovery_max_attempts}\n" if recovery_max_attempts is not None else "",
        ]
    )
    # Named explicitly by default, exactly as a generated configuration names it.
    # `omit_allowed_roots` is the other file worth testing: one that never named
    # the key and is therefore read under the historical ["build"].
    artifact_roots_line = "" if omit_allowed_roots else f"  allowed_roots: {json.dumps(allowed_roots if allowed_roots is not None else ['build'])}\n"
    config_path = config_path or directory / ".agentic-hil" / "config.yaml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    # No `version:` by default: the bulk of the suite exercises the file a
    # project already has on disk, which is read under version 1 and still needs
    # a granted read. A test that wants the version 2 model asks for it.
    version_line = "" if config_version is None else f"version: {config_version}\n"
    config_path.write_text(
        f"""{version_line}workspace_root: {workspace_root.as_posix()!r}
state_root: {state_root.as_posix()!r}
target:
  name: "example-target"
  controller: "stm32f4"
debug:
  gdb_executable: {('null' if gdb_executable is None else repr(gdb_executable.as_posix()))}
  allowed_symbols: {(allowed_symbols if allowed_symbols is not None else [])}
  allow_all_symbols: {str(allow_all_symbols).lower()}
  max_dump_size_bytes: {max_dump_size_bytes}
artifacts:
{artifact_roots_line}  allowed_extensions: [".elf", ".hex", ".bin"]
  upload_directory: ".agentic-hil/artifacts"
  max_upload_size_mb: 1
  allow_upload: true
{section_yaml("debuggers", debuggers_yaml, grants, extra={debugger_name: primary}, auto_probe_ids=auto_probe_ids, config_version=config_version)}{section_yaml("com_ports", com_ports_yaml, grants, config_version=config_version)}{section_yaml("can_buses", can_buses_yaml, grants, config_version=config_version)}reports:
  directory: ".agentic-hil/reports"
logs:
  directory: ".agentic-hil/logs"
{recovery_lines}""",
        encoding="utf-8",
    )
    return config_path


# A real ELF, small enough to write by hand, for the tests that exercise the
# symbol-table fallback. The rest of the suite hands the fakes a four-byte magic
# and nothing more, which is all the artifact validator inspects and all the fake
# GDB needs; a fallback that reads `st_value` and `st_size` out of a section
# table has to be proven against a file that really has one.
ELF_HEADER_SIZES = {32: 52, 64: 64}
ELF_SECTION_HEADER_SIZES = {32: 40, 64: 64}
ELF_SYMBOL_SIZES = {32: 16, 64: 24}
# Section indices in the file this builds: a .text for symbols to be defined
# against, the symbol table, and the strings its names live in.
ELF_TEXT_SECTION = 1
ELF_SYMTAB_SECTION = 2
ELF_STRTAB_SECTION = 3


def elf_with_symbols(entries, *, bits: int = 32, big_endian: bool = False, trailer: bytes = b"") -> bytes:
    """A minimal ELF whose symbol table carries exactly `entries`.

    Each entry is `(name, address, size)` or `(name, address, size, shndx)`; the
    section index defaults to the .text this builds, and passing 0 writes the
    undefined symbol a fallback must not answer from. A name repeated with a
    different address or size is how an ambiguous lookup is expressed.

    `trailer` is appended after the last section, where the fake GDB looks for
    its behaviour marker: bytes past everything the section table describes are
    invisible to a reader that seeks by offset, which is exactly what makes the
    marker and a valid ELF able to share one file.
    """
    order = ">" if big_endian else "<"
    names = bytearray(b"\x00")
    symbols = bytearray(_elf_symbol(order, bits, 0, 0, 0, 0))
    for entry in entries:
        name, address, size = entry[0], entry[1], entry[2]
        section_index = entry[3] if len(entry) > 3 else ELF_TEXT_SECTION
        symbols += _elf_symbol(order, bits, len(names), address, size, section_index)
        names += name.encode("utf-8") + b"\x00"
    header_size = ELF_HEADER_SIZES[bits]
    symbols_offset = header_size
    names_offset = symbols_offset + len(symbols)
    sections_offset = names_offset + len(names)
    sections = b"".join(
        _elf_section(order, bits, section_type, offset, size, link, entry_size)
        for section_type, offset, size, link, entry_size in [
            (0, 0, 0, 0, 0),
            (1, sections_offset, 0, 0, 0),
            (2, symbols_offset, len(symbols), ELF_STRTAB_SECTION, ELF_SYMBOL_SIZES[bits]),
            (3, names_offset, len(names), 0, 0),
        ]
    )
    return _elf_header(order, bits, sections_offset) + bytes(symbols) + bytes(names) + sections + trailer


def _elf_header(order: str, bits: int, sections_offset: int) -> bytes:
    identification = b"\x7fELF" + bytes([1 if bits == 32 else 2, 2 if order == ">" else 1, 1]) + b"\x00" * 9
    address_format = "I" if bits == 32 else "Q"
    return identification + struct.pack(
        f"{order}HHI{address_format}{address_format}{address_format}IHHHHHH",
        2,
        40 if bits == 32 else 183,
        1,
        0,
        0,
        sections_offset,
        0,
        ELF_HEADER_SIZES[bits],
        0,
        0,
        ELF_SECTION_HEADER_SIZES[bits],
        4,
        0,
    )


def _elf_section(order: str, bits: int, section_type: int, offset: int, size: int, link: int, entry_size: int) -> bytes:
    if bits == 32:
        return struct.pack(f"{order}IIIIIIIIII", 0, section_type, 0, 0, offset, size, link, 0, 1, entry_size)
    return struct.pack(f"{order}IIQQQQIIQQ", 0, section_type, 0, 0, offset, size, link, 0, 1, entry_size)


def _elf_symbol(order: str, bits: int, name_offset: int, address: int, size: int, section_index: int) -> bytes:
    if bits == 32:
        return struct.pack(f"{order}IIIBBH", name_offset, address, size, 0x11, 0, section_index)
    return struct.pack(f"{order}IBBHQQ", name_offset, 0x11, 0, section_index, address, size)


# The old flat permission names stay the vocabulary of the test helper: a test
# that wants "no COM write" should say so once, not repeat a permissions block
# under every com_ports entry it happens to declare.
DEFAULT_TEST_PERMISSIONS = {
    "allow_probe": True,
    "allow_flash": True,
    "allow_reset": True,
    "allow_debug_execution": True,
    "allow_com_read": True,
    "allow_com_write": True,
    "allow_can_read": True,
    "allow_can_write": True,
    "allow_raw_debugger_commands": False,
    "allow_mass_erase": False,
}
READ_PERMISSION_FLAGS = frozenset({"allow_probe", "allow_read"})
SECTION_GRANTS = {
    "debuggers": {
        "allow_probe": "allow_probe",
        "allow_flash": "allow_flash",
        "allow_reset": "allow_reset",
        "allow_debug_execution": "allow_debug_execution",
        "allow_raw_debugger_commands": "allow_raw_debugger_commands",
        "allow_mass_erase": "allow_mass_erase",
    },
    "com_ports": {"allow_read": "allow_com_read", "allow_write": "allow_com_write"},
    "can_buses": {"allow_read": "allow_can_read", "allow_write": "allow_can_write"},
}


def section_yaml(section: str, supplied: str, grants: dict[str, bool], extra: dict | None = None, *, auto_probe_ids: bool = True, config_version: int | None = None) -> str:
    """Render one named-device section, giving every entry the permissions it
    does not declare itself.

    Tests hand this helper raw YAML; a round-trip is the only way to reach each
    entry without guessing at indentation, and it also merges the helper's own
    primary debugger with any extra probes a multi-board test adds."""
    document = yaml.safe_load(supplied) or {}
    entries = {**(extra or {}), **(document.get(section) or {})}
    if not entries:
        return f"{section}: {{}}\n"
    defaults = {flag: bool(grants.get(source, False)) for flag, source in SECTION_GRANTS[section].items()}
    if config_version is not None and config_version >= 2:
        # Version 2 has no read permission to grant, and refuses the key.
        defaults = {flag: value for flag, value in defaults.items() if flag not in READ_PERMISSION_FLAGS}
    for entry in entries.values():
        entry.setdefault("permissions", defaults)
    if section == "debuggers" and len(entries) > 1 and auto_probe_ids:
        # A multi-probe config must name each probe's hardware, so give every
        # entry that did not declare one a distinct probe_id. A test that wants
        # a collision sets matching probe_ids itself; a test that wants the
        # missing-probe_id refusal passes auto_probe_ids=False.
        for index, (name, entry) in enumerate(entries.items()):
            if entry.get("probe_id") is None:
                entry["probe_id"] = f"TESTPROBE{index}-{name}"
    return yaml.safe_dump({section: entries}, sort_keys=False, default_flow_style=False)


def write_authoritative_config(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    config_root: Path | None = None,
    **kwargs,
) -> Path:
    workspace.mkdir(parents=True, exist_ok=True)
    root = (config_root or workspace.parent / "user-config").resolve()
    monkeypatch.setenv("APPDATA", str(root))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(root))
    from agentic_hil.config import project_config_path

    config_path = project_config_path(workspace)
    # Enabled OpenOCD access requires scripts outside the authorized workspace.
    interface_cfg = config_path.parent / "interface.cfg"
    target_cfg = config_path.parent / "target.cfg"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    interface_cfg.write_text("# test interface\n", encoding="utf-8")
    target_cfg.write_text("# test target\n", encoding="utf-8")
    kwargs.setdefault("interface_cfg", interface_cfg.as_posix())
    kwargs.setdefault("target_cfg", target_cfg.as_posix())
    # Extra probes need the same absolute scripts: an OpenOCD entry that keeps
    # the relative schema defaults is rejected at pinning, which would make a
    # multi-board test fail on script paths instead of what it means to check.
    extra = yaml.safe_load(kwargs.get("debuggers_yaml") or "") or {}
    if extra.get("debuggers"):
        for entry in extra["debuggers"].values():
            entry.setdefault("interface_cfg", interface_cfg.as_posix())
            entry.setdefault("target_cfg", target_cfg.as_posix())
        kwargs["debuggers_yaml"] = yaml.safe_dump(extra, sort_keys=False, default_flow_style=False)
    path = write_config(workspace, workspace_root=workspace, config_path=config_path, **kwargs)
    monkeypatch.setenv("AGENTIC_HIL_CONFIG", str(path.resolve()))
    return path
