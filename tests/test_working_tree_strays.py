"""A run that leaves files in the working tree fails, and names every one (#544).

A full run left an 18 MB pip cache in the repository root, and nothing in the
run said so: the files were simply there afterwards, untracked, until someone
looked. The session lists the checkout's untracked, not ignored paths when it
starts and again when it finishes, and a path that appeared in between fails
the run by name.

Every case here runs the suite's own `tests/conftest.py` in a pytest of its
own, inside a tree of its own, so the check under test is the one the suite
itself runs, and a stray one of these runs leaves lands in that tree and never
in this repository. The test names are short on purpose: pytester names its
directory after the test, and the trees below it have to stay inside the
Windows path limit.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from support import scaled_time_bound

HERE = Path(__file__).resolve().parent

# Hang guards, not claims: one inner session of a test or two, one git call on
# a tree of five files.
INNER_RUN_S = 120.0
GIT_CALL_S = 60.0

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not on PATH, and these cases build a checkout with it")

# What every tree here ignores, the way the repository's own .gitignore does:
# the bytecode and the cache pytest writes while it runs are not strays.
GITIGNORE = "__pycache__/\n.pytest_cache/\n"

LEAVES_TWO_FILES_BEHIND = """
from pathlib import Path

TREE = Path(__file__).resolve().parents[1]


def test_that_leaves_two_files_behind():
    (TREE / "stray.txt").write_text("left behind", encoding="utf-8")
    cache = TREE / "pip" / "cache" / "http-v2"
    cache.mkdir(parents=True)
    (cache / "entry.body").write_bytes(b"a cached download")


def test_that_writes_nothing():
    pass
"""

LEAVES_ONE_FILE_BEHIND = """
from pathlib import Path

TREE = Path(__file__).resolve().parents[1]


def test_that_leaves_a_file_behind():
    (TREE / "stray.txt").write_text("left behind", encoding="utf-8")
"""

WRITES_ONLY_INTO_ITS_OWN_TMP_PATH = """
def test_that_writes_only_into_its_own_tmp_path(tmp_path):
    (tmp_path / "scratch.txt").write_text("mine", encoding="utf-8")
"""

ADDS_TO_A_FILE_THAT_WAS_ALREADY_THERE = """
from pathlib import Path

TREE = Path(__file__).resolve().parents[1]


def test_that_adds_to_a_file_that_was_already_there_and_leaves_a_new_one():
    with open(TREE / "notes.txt", "a", encoding="utf-8") as notes:
        notes.write("one more line")
    (TREE / "stray.txt").write_text("left behind", encoding="utf-8")
"""

WRITES_INTO_AN_IGNORED_DIRECTORY = """
from pathlib import Path

TREE = Path(__file__).resolve().parents[1]


def test_that_writes_into_an_ignored_directory_and_leaves_a_new_file():
    (TREE / "ignored-by-the-tree").mkdir()
    (TREE / "ignored-by-the-tree" / "cache.bin").write_bytes(b"ignored")
    (TREE / "stray.txt").write_text("left behind", encoding="utf-8")
"""

# The shape of the leak behind #544: a value a test set stays in the
# environment after it ended, here for the rest of the session.
MOVES_THE_HOME_FOR_THE_REST_OF_THE_SESSION = """
import os
from pathlib import Path

import pytest

TREE = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session", autouse=True)
def a_home_moved_and_never_moved_back(tmp_path_factory):
    elsewhere = tmp_path_factory.mktemp("elsewhere")
    for name in ("HOME", "USERPROFILE", "XDG_CONFIG_HOME"):
        os.environ[name] = str(elsewhere)


def test_that_leaves_a_file_behind():
    (TREE / "stray.txt").write_text("left behind", encoding="utf-8")
"""

WRITES_INTO_ITS_TMP_PATH_AND_LEAVES_A_FILE_BEHIND = """
from pathlib import Path

TREE = Path(__file__).resolve().parents[1]


def test_that_writes_into_its_tmp_path_and_leaves_a_file_behind(tmp_path):
    (tmp_path / "scratch.txt").write_text("mine", encoding="utf-8")
    (TREE / "stray.txt").write_text("left behind", encoding="utf-8")
"""

# Git reads the repository's config on every call, so a config it cannot parse
# stops the next call dead, the finish listing among them.
LEAVES_THE_REPOSITORY_UNREADABLE = """
from pathlib import Path

TREE = Path(__file__).resolve().parents[1]


def test_that_leaves_the_repository_unreadable():
    (TREE / ".git" / "config").write_text("[core", encoding="utf-8")
"""

# The shape of the suite's own nested runs: a test that starts a pytest session
# of the checkout it belongs to, and requires that session to pass quietly.
RUNS_A_SESSION_OF_ITS_OWN_TREE = """
import subprocess
import sys
from pathlib import Path

from support import scaled_time_bound

TREE = Path(__file__).resolve().parents[1]


def test_that_runs_a_session_of_its_own_tree():
    nested = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/nested_session.py", "-q", "-p", "no:cacheprovider"],
        cwd=TREE,
        capture_output=True,
        text=True,
        timeout=scaled_time_bound(120.0),
    )
    said = [line for line in (nested.stdout + nested.stderr).splitlines() if line.strip()]
    assert nested.returncode == 0 and len(said) == 2, nested.stdout + nested.stderr
"""


def a_tree_running_the_suites_conftest(tree: Path, test_module: str, *, gitignore: str = GITIGNORE) -> Path:
    """The suite's conftest and the module it imports, one test module, and nothing else."""
    (tree / "tests").mkdir(parents=True)
    for name in ("conftest.py", "support.py"):
        shutil.copyfile(HERE / name, tree / "tests" / name)
    (tree / "tests" / "test_inner.py").write_text(test_module, encoding="utf-8")
    (tree / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (tree / ".gitignore").write_text(gitignore, encoding="utf-8")
    return tree


def git(where: Path, *args: str, **environment: str) -> str:
    """Run git in `where` with none of this process's own GIT_ variables, and fail naming its error."""
    clean = {name: value for name, value in os.environ.items() if not name.upper().startswith("GIT_")}
    done = subprocess.run(
        ["git", *args],
        cwd=where,
        env={**clean, **environment},
        capture_output=True,
        text=True,
        timeout=scaled_time_bound(GIT_CALL_S),
        check=False,
    )
    assert done.returncode == 0, f"git {' '.join(args)} failed in {where}: {done.stderr.strip()}"
    return done.stdout


def gits_own_error(where: Path, *args: str) -> str:
    """The line git ends on when `git args` fails in `where`: its error, word for word, in whatever language it speaks here."""
    clean = {name: value for name, value in os.environ.items() if not name.upper().startswith("GIT_")}
    done = subprocess.run(
        ["git", *args],
        cwd=where,
        env=clean,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=scaled_time_bound(GIT_CALL_S),
        check=False,
    )
    said = done.stderr.strip().splitlines()
    assert done.returncode != 0 and said, f"git {' '.join(args)} was meant to fail in {where} and exited {done.returncode}: {done.stdout}"
    return said[-1]


def a_checkout(tree: Path) -> Path:
    """`tree` as a git checkout of its own, every file in it already tracked."""
    git(tree, "init", "-q")
    git(tree, "add", "-A")
    return tree


def run_the_suite(pytester: pytest.Pytester, tree: Path, *args: str, **environment: str) -> pytest.RunResult:
    """Run the tree's suite quietly, in a pytest process of its own, started in the tree's root.

    None of this run's own git, xdist or test context reaches it: an inherited
    GIT_DIR would point the inner git at another repository, an inherited
    PYTEST_XDIST_WORKER would tell the inner controller it is a worker, and an
    inherited PYTEST_CURRENT_TEST would tell it that a test of another session
    started it. The context is a private one, so nothing set here outlives the
    call.
    """
    with pytest.MonkeyPatch.context() as patch:
        for name in list(os.environ):
            if name.upper().startswith(("GIT_", "PYTEST_XDIST_")) or name.upper() == "PYTEST_CURRENT_TEST":
                patch.delenv(name)
        for name, value in environment.items():
            patch.setenv(name, value)
        patch.chdir(tree)
        return pytester.runpytest_subprocess("tests", "-q", "-p", "no:cacheprovider", *args, timeout=scaled_time_bound(INNER_RUN_S))


def everything_it_said(result: pytest.RunResult) -> str:
    return "\n".join([*result.outlines, *result.errlines])


def assert_it_said_nothing_but_its_result(result: pytest.RunResult, passed: int) -> None:
    """A quiet run that passed prints its progress line and its summary line, and not one line more."""
    output = everything_it_said(result)
    assert result.ret == pytest.ExitCode.OK, output
    result.assert_outcomes(passed=passed)
    said = [line for line in output.splitlines() if line.strip()]
    assert len(said) == 2 and said[0].startswith("."), output


@needs_git
def test_a_run_that_leaves_files_in_the_tree_fails_naming_each(pytester: pytest.Pytester) -> None:
    """The issue's own shape: a test wrote into the checkout, and the run passed.

    The run fails, and says why in words a developer can act on: every path
    that appeared, as git names it from the repository root, and that a test
    wrote it. The results of the tests themselves stay what they were, both
    passed, so the failure is the check's and nobody hunts for a failing test
    that does not exist. Nothing is deleted: what was written is the evidence
    of which test wrote it.
    """
    tree = a_checkout(a_tree_running_the_suites_conftest(pytester.path / "tree", LEAVES_TWO_FILES_BEHIND))

    result = run_the_suite(pytester, tree)

    output = everything_it_said(result)
    assert result.ret == pytest.ExitCode.TESTS_FAILED, output
    result.assert_outcomes(passed=2)
    assert "wrote into the working tree" in output, output
    assert "stray.txt" in output, output
    assert "pip/cache/http-v2/entry.body" in output, output
    assert (tree / "stray.txt").is_file()
    assert (tree / "pip" / "cache" / "http-v2" / "entry.body").is_file()


@needs_git
def test_a_run_that_writes_nothing_into_the_tree_passes_silently(pytester: pytest.Pytester) -> None:
    """A test that keeps to its own `tmp_path` is what every test should do, and it costs the run nothing."""
    tree = a_checkout(a_tree_running_the_suites_conftest(pytester.path / "tree", WRITES_ONLY_INTO_ITS_OWN_TMP_PATH))

    result = run_the_suite(pytester, tree)

    assert_it_said_nothing_but_its_result(result, passed=1)


@needs_git
def test_a_path_untracked_before_the_run_is_not_reported(pytester: pytest.Pytester) -> None:
    """What was already there is the developer's, even when a test adds to it.

    A clone in use has untracked files of its own: notes, a local config, a
    build nobody committed. The check compares the finish with the start, so
    only what appeared during the run is named, and a file that existed before
    it is not, even though a test appended to it.
    """
    tree = a_checkout(a_tree_running_the_suites_conftest(pytester.path / "tree", ADDS_TO_A_FILE_THAT_WAS_ALREADY_THERE))
    (tree / "notes.txt").write_text("the developer's own", encoding="utf-8")

    result = run_the_suite(pytester, tree)

    output = everything_it_said(result)
    assert result.ret == pytest.ExitCode.TESTS_FAILED, output
    assert "stray.txt" in output, output
    assert "notes.txt" not in output, output


@needs_git
def test_an_ignored_path_is_not_reported(pytester: pytest.Pytester) -> None:
    """What the tree's .gitignore ignores is not a stray, wherever a test put it.

    The listing is git's own `--exclude-standard` view, so a directory the
    repository ignores (a build output, a virtualenv, a tool's cache) can fill
    up during a run without failing it, while a new file beside it still does.
    """
    tree = a_checkout(
        a_tree_running_the_suites_conftest(
            pytester.path / "tree", WRITES_INTO_AN_IGNORED_DIRECTORY, gitignore=GITIGNORE + "ignored-by-the-tree/\n"
        )
    )

    result = run_the_suite(pytester, tree)

    output = everything_it_said(result)
    assert result.ret == pytest.ExitCode.TESTS_FAILED, output
    assert "stray.txt" in output, output
    assert "ignored-by-the-tree" not in output, output


@needs_git
def test_a_globally_ignored_path_stays_ignored_when_a_test_moved_home(pytester: pytest.Pytester) -> None:
    """The finish is read the way the start was, whatever a test left in the environment.

    `--exclude-standard` includes the developer's global excludes file, which
    git finds through HOME, USERPROFILE and XDG_CONFIG_HOME, and a test that
    sets one of those through `monkeypatch` leaves the sandbox's value behind
    when it ends (the same leak that sent pip's cache into the repository root).
    Read with the environment a test left, the finish would list every globally
    ignored file as new and blame a test for the developer's own editor files.
    So the premise is shown first, with git itself, and then the run: only the
    file a test wrote is named.
    """
    global_config = pytester.path / "global-config"
    (global_config / "git").mkdir(parents=True)
    (global_config / "git" / "ignore").write_text("personal-notes.txt\n", encoding="utf-8")
    elsewhere = pytester.path / "elsewhere"
    elsewhere.mkdir()
    tree = a_checkout(a_tree_running_the_suites_conftest(pytester.path / "tree", MOVES_THE_HOME_FOR_THE_REST_OF_THE_SESSION))
    (tree / "personal-notes.txt").write_text("the developer's own", encoding="utf-8")
    listing = ("ls-files", "--others", "--exclude-standard")
    assert "personal-notes.txt" not in git(tree, *listing, XDG_CONFIG_HOME=str(global_config))
    assert "personal-notes.txt" in git(tree, *listing, HOME=str(elsewhere), USERPROFILE=str(elsewhere), XDG_CONFIG_HOME=str(elsewhere))

    result = run_the_suite(pytester, tree, XDG_CONFIG_HOME=str(global_config))

    output = everything_it_said(result)
    assert result.ret == pytest.ExitCode.TESTS_FAILED, output
    assert "stray.txt" in output, output
    assert "personal-notes.txt" not in output, output


@needs_git
def test_pytests_own_basetemp_in_the_tree_is_not_reported(pytester: pytest.Pytester) -> None:
    """A `--basetemp` inside the checkout is pytest's scratch space, not a stray.

    Pointing `--basetemp` into the clone is what a developer does to keep
    scratch paths short under the Windows path limit, and `tests/conftest.py`
    names it as a practice the suite has to live with. Every `tmp_path` of such
    a run lies inside the tree, untracked and not ignored, so a check that
    listed it would fail every one of those runs with the run's own scratch
    files. pytest's own basetemp is therefore left out when it lies inside the
    repository, and a file a test left beside it is still named.
    """
    tree = a_checkout(a_tree_running_the_suites_conftest(pytester.path / "tree", WRITES_INTO_ITS_TMP_PATH_AND_LEAVES_A_FILE_BEHIND))

    result = run_the_suite(pytester, tree, f"--basetemp={tree / 'inner-basetemp'}")

    output = everything_it_said(result)
    assert list((tree / "inner-basetemp").rglob("scratch.txt")), f"the run's tmp_path was not inside the tree:\n{output}"
    assert result.ret == pytest.ExitCode.TESTS_FAILED, output
    assert "stray.txt" in output, output
    assert "inner-basetemp" not in output, output
    assert "scratch.txt" not in output, output


@needs_git
@pytest.mark.parametrize("where", ["no-repo", "nested"])
def test_outside_a_checkout_of_its_own_the_check_stays_silent(pytester: pytest.Pytester, where: str) -> None:
    """A tree that is not the root of a git checkout has no before and after to compare.

    The sdist gate in CI unpacks the source distribution inside the CI checkout
    and collects the suite there (`nested`): that tree is untracked as a whole,
    and git run inside it answers for the outer repository. A tree with no
    repository at all (`no-repo`) is what an unpacked archive anywhere else is.
    Either way the run is left exactly as it would be without the check: it
    passes, and it says nothing about the tree or about git.
    """
    if where == "no-repo":
        tree = a_tree_running_the_suites_conftest(pytester.path / "tree", LEAVES_ONE_FILE_BEHIND)
    else:
        outer = pytester.path / "outer"
        outer.mkdir()
        git(outer, "init", "-q")
        tree = a_tree_running_the_suites_conftest(outer / "sdist-check" / "unpacked", LEAVES_ONE_FILE_BEHIND)

    # Git looks no further up than this test's own directory, whatever holds it.
    result = run_the_suite(pytester, tree, GIT_CEILING_DIRECTORIES=str(pytester.path))

    assert_it_said_nothing_but_its_result(result, passed=1)
    assert (tree / "stray.txt").is_file()


@needs_git
def test_without_git_on_path_the_check_stays_silent(pytester: pytest.Pytester) -> None:
    """A machine without git runs the suite the way it ran before the check existed."""
    tree = a_checkout(a_tree_running_the_suites_conftest(pytester.path / "tree", LEAVES_ONE_FILE_BEHIND))
    no_git = pytester.path / "a-path-without-git"
    no_git.mkdir()

    result = run_the_suite(pytester, tree, PATH=str(no_git))

    assert_it_said_nothing_but_its_result(result, passed=1)
    assert (tree / "stray.txt").is_file()


@needs_git
def test_git_failing_at_the_start_says_so_in_one_line(pytester: pytest.Pytester) -> None:
    """A checkout git cannot read when the run starts is named in one line, and nothing else changes.

    The tree has a `.git` and git is on PATH, so this run is one the check is
    meant for, and without a start listing there is nothing to compare the
    finish with. Staying silent would read as a clean tree nobody looked at, so
    one line says the tree was not checked, carrying git's own error, and the
    run keeps the status its tests gave it, the file a test left included.
    """
    tree = a_checkout(a_tree_running_the_suites_conftest(pytester.path / "tree", LEAVES_ONE_FILE_BEHIND))
    (tree / ".git" / "config").write_text("[core", encoding="utf-8")
    error = gits_own_error(tree, "ls-files", "--others", "--exclude-standard")

    result = run_the_suite(pytester, tree)

    output = everything_it_said(result)
    assert result.ret == pytest.ExitCode.OK, output
    result.assert_outcomes(passed=1)
    said = [line for line in output.splitlines() if line.strip()]
    assert len(said) == 3, output
    assert len([line for line in said if error in line]) == 1, f"no line carries git's own error {error!r}:\n{output}"
    assert (tree / "stray.txt").is_file()


@needs_git
def test_git_failing_at_the_finish_says_so_in_one_line(pytester: pytest.Pytester) -> None:
    """A tree git cannot read at the end is named in one line, and the run keeps its status.

    The start was listed, so this run was meant to be checked, and the finish
    cannot be. Saying nothing would pass a run nobody checked, and failing it
    would charge git's trouble to the tests. So the status stays what the tests
    made it, and one line says the tree was not checked, carrying git's own
    error as git wrote it. The test breaks git through the file system alone,
    before the finish listing runs, so nothing here depends on timing.
    """
    tree = a_checkout(a_tree_running_the_suites_conftest(pytester.path / "tree", LEAVES_THE_REPOSITORY_UNREADABLE))

    result = run_the_suite(pytester, tree)

    error = gits_own_error(tree, "ls-files", "--others", "--exclude-standard")
    output = everything_it_said(result)
    assert result.ret == pytest.ExitCode.OK, output
    result.assert_outcomes(passed=1)
    said = [line for line in output.splitlines() if line.strip()]
    assert len(said) == 3, output
    assert len([line for line in said if error in line]) == 1, f"no line carries git's own error {error!r}:\n{output}"


@needs_git
def test_under_xdist_only_the_controller_lists_the_tree(pytester: pytest.Pytester) -> None:
    """The workers share one tree, so one process reads it, once at each end.

    A worker's view is useless here: it starts after the controller, so its
    start listing can already hold what another worker wrote, and whatever it
    concludes never reaches the result, because the controller ignores a
    worker's exit status. The controller is the process that sees the session
    begin and end, and the run it reports is the run the developer started.
    Git's own trace counts the listings across every process of the run.
    """
    tree = a_checkout(a_tree_running_the_suites_conftest(pytester.path / "tree", LEAVES_TWO_FILES_BEHIND))
    trace = pytester.path / "git-trace.log"

    result = run_the_suite(pytester, tree, "-n", "2", GIT_TRACE=str(trace))

    output = everything_it_said(result)
    assert result.ret == pytest.ExitCode.TESTS_FAILED, output
    result.assert_outcomes(passed=2)
    assert "stray.txt" in output, output
    assert "pip/cache/http-v2/entry.body" in output, output
    listings = [line for line in trace.read_text(encoding="utf-8").splitlines() if "built-in: git" in line and " ls-files" in line]
    assert len(listings) == 2, "\n".join(listings)


@needs_git
def test_a_session_a_test_starts_leaves_the_check_to_the_run(pytester: pytest.Pytester) -> None:
    """A pytest session started from inside a test is part of that test, and the run around it checks the tree.

    Two tests in this suite start a session of this checkout and require it to
    pass. Such a session begins and ends while the workers of the run that
    started it keep writing, so its own listings would compare a tree other
    tests are changing, and a file any of them left would fail it, and with it
    the test that started it, which wrote nothing. The run that owns the test
    sees the whole session and names the file; the session inside it passes
    and says nothing, which is what the test starting it requires here.
    """
    tree = a_tree_running_the_suites_conftest(pytester.path / "tree", RUNS_A_SESSION_OF_ITS_OWN_TREE)
    (tree / "tests" / "nested_session.py").write_text(LEAVES_ONE_FILE_BEHIND, encoding="utf-8")
    a_checkout(tree)

    result = run_the_suite(pytester, tree)

    output = everything_it_said(result)
    assert result.ret == pytest.ExitCode.TESTS_FAILED, output
    result.assert_outcomes(passed=1)
    assert "stray.txt" in output, output
