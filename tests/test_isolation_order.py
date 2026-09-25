"""The sandbox is set up before a test's `monkeypatch` and undone after it (#563).

`isolated_config_environment` gives every test its own HOME, USERPROFILE,
profile directories, temporary storage and terminal size, takes
AGENTIC_HIL_CONFIG out, and puts the session's values back when the test ends.
A test that changes one of those through `monkeypatch` records the sandbox's
value as the one to restore, so the test's changes have to be undone first and
the isolation last. pytest registers a conftest's fixtures in the order their
names sort, and three autouse fixtures that take `monkeypatch` sort ahead of
the isolation, so the test's instance was created first and undone last: after
the isolation had put the session's value back, it restored the test's own
sandbox, which had just been deleted, and every session fixture, module
fixture and hook after it on that worker ran with that. It is how the pip cache
of #544 reached the repository root. AGENTIC_HIL_CONFIG went the other way:
what the test recorded was the isolation's absence, so the developer's own
value was gone for the rest of the session.

Every case here runs the suite's own `tests/conftest.py` in a pytest of its
own, the way tests/test_working_tree_strays.py does, so the order under test is
the one the suite itself gets. In each, a first test changes a value and a
second test asks for a session fixture. pytest sets that fixture up before the
second test's own isolation, so it sees exactly what the first test left
behind. The test names are short for the reason given there: pytester names
its directory after the test, and the trees below it have to stay inside the
Windows path limit.
"""

from __future__ import annotations

import re

import pytest
from test_working_tree_strays import a_tree_running_the_suites_conftest, everything_it_said, run_the_suite

# The issue's reproduction, word for word: the first test moves USERPROFILE
# through `monkeypatch`, and the session fixture set up for the second sees
# whatever the first left behind.
REPRODUCTION = """
import os

import pytest

AT_IMPORT = os.environ.get("USERPROFILE")


@pytest.fixture(scope="session")
def home_at_session_fixture_setup():
    return os.environ.get("USERPROFILE")


def test_one_sets_the_home_through_monkeypatch(monkeypatch, tmp_path):
    monkeypatch.setenv("USERPROFILE", str(tmp_path))


def test_two_sees_the_home_the_session_started_with(home_at_session_fixture_setup):
    assert home_at_session_fixture_setup == AT_IMPORT
"""

# An autouse fixture that takes `monkeypatch`, for appending to the conftest.
# Its name starts with an uppercase letter, which sorts before an underscore and
# before every lowercase letter; the case that appends it checks that it sorts
# ahead of every fixture defined there before relying on it.
SORTS_FIRST = "A_fixture_that_sorts_first"
TAKES_MONKEYPATCH_AND_SORTS_FIRST = f"""


@pytest.fixture(autouse=True)
def {SORTS_FIRST}(monkeypatch):
    pass
"""

# The same shape for the one variable the isolation takes out rather than
# points into the sandbox.
KEEPS_THE_DEVELOPERS_CONFIG = """
import os

import pytest

AT_IMPORT = os.environ.get("AGENTIC_HIL_CONFIG")


@pytest.fixture(scope="session")
def config_at_session_fixture_setup():
    return os.environ.get("AGENTIC_HIL_CONFIG")


def test_one_sets_the_config_through_monkeypatch(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENTIC_HIL_CONFIG", str(tmp_path / "agentic-hil.yaml"))


def test_two_sees_the_developers_config(config_at_session_fixture_setup):
    assert AT_IMPORT is not None, "the developer's AGENTIC_HIL_CONFIG never reached this session"
    assert config_at_session_fixture_setup == AT_IMPORT
"""

# The same shape for temporary storage, whose cached answer in `tempfile` moves
# with the variables, and a session fixture that asks for a scratch file.
KEEPS_THE_TEMPORARY_STORAGE = """
import os
import tempfile

import pytest

NAMES = ("TMPDIR", "TEMP", "TMP")
AT_IMPORT = ({name: os.environ.get(name) for name in NAMES}, tempfile.gettempdir())


@pytest.fixture(scope="session")
def temporary_storage_at_session_fixture_setup():
    handle, path = tempfile.mkstemp()
    os.close(handle)
    os.unlink(path)
    return ({name: os.environ.get(name) for name in NAMES}, tempfile.gettempdir())


def test_one_moves_the_temporary_storage_through_monkeypatch(monkeypatch, tmp_path):
    for name in NAMES:
        monkeypatch.setenv(name, str(tmp_path))
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))


def test_two_sees_the_temporary_storage_the_session_started_with(temporary_storage_at_session_fixture_setup):
    assert temporary_storage_at_session_fixture_setup == AT_IMPORT
"""

# The same shape with no `monkeypatch` in the test at all: pytester moves HOME
# and USERPROFILE through the one it requests itself.
KEEPS_THE_HOME_PYTESTER_MOVED = """
import os

import pytest

NAMES = ("HOME", "USERPROFILE")
AT_IMPORT = {name: os.environ.get(name) for name in NAMES}


@pytest.fixture(scope="session")
def home_at_session_fixture_setup():
    return {name: os.environ.get(name) for name in NAMES}


def test_one_runs_under_pytester(pytester):
    pass


def test_two_sees_the_home_the_session_started_with(home_at_session_fixture_setup):
    assert home_at_session_fixture_setup == AT_IMPORT
"""


def assert_both_passed(result: pytest.RunResult) -> None:
    """Both tests of the tree passed, and when they did not, everything the run said is the message."""
    output = everything_it_said(result)
    assert result.ret == pytest.ExitCode.OK, output
    result.assert_outcomes(passed=2)


def test_the_reproduction_passes(pytester: pytest.Pytester) -> None:
    """The issue's own reproduction gives `2 passed`, where it gave `1 failed, 1 passed`."""
    tree = a_tree_running_the_suites_conftest(pytester.path / "tree", REPRODUCTION)

    result = run_the_suite(pytester, tree)

    assert_both_passed(result)


def test_a_fixture_sorting_first(pytester: pytest.Pytester) -> None:
    """The order holds whatever the fixtures are called, so one more that sorts first changes nothing.

    Three fixtures that sort ahead of the isolation are what created the test's
    `monkeypatch` first, and reordering those three alone would pass the
    reproduction and leave the next such fixture to do the same again. So the
    reproduction runs once more with an autouse fixture that takes
    `monkeypatch` appended to the suite's conftest, under a name that sorts
    ahead of every fixture defined there, and it still passes. The premise is
    checked on the copied conftest first: a name that no longer sorts first
    would leave this case testing the reproduction again and nothing more.
    """
    tree = a_tree_running_the_suites_conftest(pytester.path / "tree", REPRODUCTION)
    conftest = tree / "tests" / "conftest.py"
    source = conftest.read_text(encoding="utf-8")
    first_defined = min(re.findall(r"^def (\w+)", source, re.MULTILINE))
    assert first_defined > SORTS_FIRST, f"{first_defined} in the suite's conftest sorts ahead of {SORTS_FIRST}"
    conftest.write_text(source + TAKES_MONKEYPATCH_AND_SORTS_FIRST, encoding="utf-8")

    result = run_the_suite(pytester, tree)

    assert_both_passed(result)


def test_the_developers_config(pytester: pytest.Pytester) -> None:
    """A developer's own AGENTIC_HIL_CONFIG is there again after a test that set one through `monkeypatch`.

    The isolation takes the variable out for every test, so a test that sets
    it records its absence as the value to restore. Undone after the
    isolation, that absence outlived the test, and the session fixture set up
    next found no config where the developer had named one.
    """
    tree = a_tree_running_the_suites_conftest(pytester.path / "tree", KEEPS_THE_DEVELOPERS_CONFIG)

    result = run_the_suite(pytester, tree, AGENTIC_HIL_CONFIG=str(pytester.path / "developers-own.yaml"))

    assert_both_passed(result)


def test_the_temporary_storage(pytester: pytest.Pytester) -> None:
    """Temporary storage a test moved through `monkeypatch` is where the session had it once the test is over.

    Both halves of it: the three variables, which child processes read, and
    `tempfile.tempdir`, the cached answer this process reads. Left on the
    test's sandbox, they sent the session fixture set up next into a directory
    that had just been deleted, and asking for a scratch file failed there.
    """
    tree = a_tree_running_the_suites_conftest(pytester.path / "tree", KEEPS_THE_TEMPORARY_STORAGE)

    result = run_the_suite(pytester, tree)

    assert_both_passed(result)


def test_pytesters_home_is_undone(pytester: pytest.Pytester) -> None:
    """A test that only takes `pytester` gives HOME and USERPROFILE back as well.

    pytester points both at its own directory through the `monkeypatch` it
    requests, which is why every pytester test in the suite changes them. The
    order has to hold for a `monkeypatch` a plugin's fixture asks for, not
    only for the one a test names.
    """
    tree = a_tree_running_the_suites_conftest(pytester.path / "tree", KEEPS_THE_HOME_PYTESTER_MOVED)

    result = run_the_suite(pytester, tree)

    assert_both_passed(result)
