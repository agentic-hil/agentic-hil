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


def a_checkout(tree: Path) -> Path:
    """`tree` as a git checkout of its own, every file in it already tracked."""
    git(tree, "init", "-q")
    git(tree, "add", "-A")
    return tree


def run_the_suite(pytester: pytest.Pytester, tree: Path, *args: str, **environment: str) -> pytest.RunResult:
    """Run the tree's suite quietly, in a pytest process of its own, started in the tree's root.

    None of this run's own git or xdist context reaches it: an inherited GIT_DIR
    would point the inner git at another repository, and an inherited
    PYTEST_XDIST_WORKER would tell the inner controller it is a worker. The
    context is a private one, so nothing set here outlives the call.
    """
    with pytest.MonkeyPatch.context() as patch:
        for name in list(os.environ):
            if name.upper().startswith(("GIT_", "PYTEST_XDIST_")):
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
