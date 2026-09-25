"""A leg that runs out of time names the tests it was running (#536).

A Windows leg that reached the job's limit was cancelled, and its log ended in
a row of dots and the runner's `The operation was canceled.` line: pytest never
printed its summary, so the red check named no test at all. `Run tests` now has
a limit of its own below the job's, so a leg that runs out of time ends inside
that step and the step after it still runs; this file pins what that step has
to go on and what it says.

The suite keeps a ledger while it runs: one line when a test starts and one when
it finishes, saying how it ended, one file per xdist worker, each line on disk
before the next thing happens, so a process killed in the middle of a test
leaves that test's start behind. It is written only when
`AGENTIC_HIL_TEST_LEDGER` names a directory, which `ci.yml` sets for `Run tests`
and for nothing else.

`tests/suite_ledger.py`, run as a script with that directory, is the reader. A
ledger whose session did not finish on its own terms, because it was killed or
interrupted, becomes an error annotation and a job summary naming every test
that had started and not finished, with its worker, and below them every test
that had finished failed or with an error before the end: the leg's log shows
those as an `F` or an `E` in a row of dots and never says which test it was. A
session that did finish, however badly, has had pytest name its failures
already, and the reader adds nothing to it.

Every session below is a real one in a subprocess: what is pinned is what a
process leaves on disk when it dies, and a session inside this process would
share its imports with the session running this file.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from support import scaled_time_bound

TESTS = Path(__file__).resolve().parent
READER = TESTS / "suite_ledger.py"
PLUGIN = "suite_ledger"
LEDGER_VARIABLE = "AGENTIC_HIL_TEST_LEDGER"
# What pytest-xdist puts in a worker's environment. A session started by a test
# that runs in a worker inherits all of it, and the sessions below must not.
XDIST_VARIABLES = ("PYTEST_XDIST_WORKER", "PYTEST_XDIST_WORKER_COUNT", "PYTEST_XDIST_TESTRUNUID")
# Hang guards, not claims about speed: a fresh interpreter with xdist behind it
# takes seconds to start on a loaded Windows machine.
SESSION_TIMEOUT_S = 180.0
READER_TIMEOUT_S = 60.0


@pytest.fixture(autouse=True)
def session_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing inherited from the session running this file, and the plugin importable.

    This suite runs under `-n auto`, and in CI with the ledger variable set, so a
    session started here would otherwise take itself for that worker and write
    into that ledger. The job summary's path goes too, so nothing started here
    can write into the real one. `tests/` goes on PYTHONPATH because the
    sessions below run in a directory of their own, where `-p suite_ledger`
    would find nothing.
    """
    for name in (*XDIST_VARIABLES, LEDGER_VARIABLE, "GITHUB_STEP_SUMMARY"):
        monkeypatch.delenv(name, raising=False)
    inherited = os.environ.get("PYTHONPATH", "")
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(part for part in (str(TESTS), inherited) if part))


def run_session(pytester: pytest.Pytester, *arguments: str) -> pytest.RunResult:
    """A pytest session in a subprocess, with the ledger plugin loaded by `-p`."""
    return pytester.runpytest_subprocess(
        "-p", PLUGIN, "-p", "no:cacheprovider", *arguments, timeout=scaled_time_bound(SESSION_TIMEOUT_S)
    )


def transcript(result: pytest.RunResult) -> str:
    return "\n".join([*result.outlines, *result.errlines])


def ledger_files(directory: Path) -> dict[str, list[dict]]:
    """Every line of every file in a ledger directory, parsed, by file name.

    An absent directory reads as an empty ledger, so a session that wrote
    nothing fails the assertion about what it should have written, not this.
    """
    if not directory.is_dir():
        return {}
    return {
        path.name: [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        for path in sorted(directory.iterdir())
        if path.is_file()
    }


def per_test_lines(files: dict[str, list[dict]]) -> list[dict]:
    """The lines that record a test starting or finishing, from every file.

    Whatever else a ledger says about its session is the reader's business.
    """
    return [
        line
        for lines in files.values()
        for line in lines
        if line.get("event") in ("start", "finish") and line.get("nodeid")
    ]


def events_of(lines: list[dict], name: str) -> list[str]:
    """What the ledger records for the test function `name`, in the order it was written."""
    return [line["event"] for line in lines if line["nodeid"].endswith(f"::{name}")]


def outcomes_of(lines: list[dict], name: str) -> list[object]:
    """How each finish line for the test function `name` says it ended."""
    return [line.get("outcome") for line in lines if line["event"] == "finish" and line["nodeid"].endswith(f"::{name}")]


def write_ledger(directory: Path, name: str, lines: list[dict]) -> Path:
    """One worker's file, written the way the plugin writes one: a JSON object a line."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    with path.open("a", encoding="utf-8") as stream:
        for line in lines:
            stream.write(json.dumps(line) + "\n")
    return path


def started(nodeid: str, worker: str) -> dict:
    return {"event": "start", "nodeid": nodeid, "worker": worker}


def finished(nodeid: str, worker: str, outcome: str = "passed") -> dict:
    return {"event": "finish", "nodeid": nodeid, "worker": worker, "outcome": outcome}


def read_ledger(directory: Path, summary: Path) -> subprocess.CompletedProcess[str]:
    """The reader, run the way the step after `Run tests` runs it."""
    return subprocess.run(
        [sys.executable, str(READER), str(directory)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={**os.environ, "GITHUB_STEP_SUMMARY": str(summary)},
        timeout=scaled_time_bound(READER_TIMEOUT_S),
        check=False,
    )


def summary_of(summary: Path) -> str:
    return summary.read_text(encoding="utf-8") if summary.is_file() else ""


def assert_named(
    completed: subprocess.CompletedProcess[str], summary: Path, named: dict[str, str], unnamed: tuple[str, ...]
) -> None:
    """The reader said the leg ran out of time and named each test in `named` with its worker.

    In an error annotation, which is what the checks page shows, and in the job
    summary, where a line that names a test names its worker and no other one.
    Nothing in `unnamed` appears in either.
    """
    output = f"{completed.stdout}\n{completed.stderr}"
    assert completed.returncode == 0, output
    annotation = "\n".join(line for line in completed.stdout.splitlines() if line.startswith("::error"))
    written = summary_of(summary)
    assert "ran out of" in annotation, output
    assert "ran out of" in written, written
    workers = set(named.values())
    for nodeid, worker in named.items():
        assert nodeid in annotation and worker in annotation, (nodeid, worker, annotation)
        lines = [line for line in written.splitlines() if nodeid in line]
        assert lines, (nodeid, written)
        for line in lines:
            assert worker in line, (nodeid, worker, line)
            assert [other for other in workers - {worker} if other in line] == [], (nodeid, line)
    for name in unnamed:
        assert name not in annotation and name not in written, (name, annotation, written)


def assert_listed_below(
    completed: subprocess.CompletedProcess[str], summary: Path, unfinished: tuple[str, ...], red: dict[str, str]
) -> None:
    """The tests in `red` come after every test in `unfinished`, each with how it ended.

    Below them in the annotation and in the job summary, so the list of what was
    running when the leg stopped reads first and the one of what went red before
    that is a list of its own. A summary line naming a test in `red` says
    whether it failed or ended in an error.
    """
    annotation = "\n".join(line for line in completed.stdout.splitlines() if line.startswith("::error"))
    written = summary_of(summary)
    for text in (annotation, written):
        last_unfinished = max(text.rindex(nodeid) for nodeid in unfinished)
        for nodeid in red:
            assert text.index(nodeid) > last_unfinished, (nodeid, text)
    for nodeid, outcome in red.items():
        lines = [line for line in written.splitlines() if nodeid in line]
        assert lines and all(outcome in line for line in lines), (nodeid, outcome, written)


def assert_nothing_added(completed: subprocess.CompletedProcess[str], summary: Path) -> None:
    """The reader ran and added neither an annotation nor a summary."""
    output = f"{completed.stdout}\n{completed.stderr}"
    assert completed.returncode == 0, output
    assert [line for line in completed.stdout.splitlines() if line.startswith("::")] == [], output
    assert summary_of(summary) == "", summary_of(summary)


def files_mentioning(root: Path, marker: bytes) -> list[Path]:
    """Every file under `root` whose bytes carry `marker`, bytecode caches aside."""
    if not root.is_dir():
        return []
    return [
        path
        for path in root.rglob("*")
        if "__pycache__" not in path.parts and path.is_file() and marker in path.read_bytes()
    ]


def test_the_suite_loads_the_ledger_plugin_itself(pytestconfig: pytest.Config) -> None:
    """`Run tests` names no plugin on its command line: the suite's conftest loads it.

    Loaded, it does nothing until the variable names a directory, which is what
    keeps a developer's run from writing a ledger nobody asked for.
    """
    assert pytestconfig.pluginmanager.has_plugin(PLUGIN), PLUGIN


def test_each_test_leaves_a_start_line_and_a_finish_line(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One line as a test starts and one as it finishes, whatever its outcome."""
    ledger = pytester.path / "ledger"
    monkeypatch.setenv(LEDGER_VARIABLE, str(ledger))
    pytester.makepyfile(
        test_outcomes="""
import pytest


def test_passes():
    pass


def test_fails():
    assert False


@pytest.mark.skip(reason="skipped on purpose")
def test_skipped():
    pass
"""
    )

    result = run_session(pytester)

    lines = per_test_lines(ledger_files(ledger))
    for name in ("test_passes", "test_fails", "test_skipped"):
        assert events_of(lines, name) == ["start", "finish"], (name, lines, transcript(result))
    workers = {line.get("worker") for line in lines}
    assert len(workers) == 1 and all(isinstance(worker, str) and worker for worker in workers), lines
    result.assert_outcomes(passed=1, failed=1, skipped=1)


def test_a_finish_line_says_how_its_test_ended(pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch) -> None:
    """In the words pytest's own summary uses.

    A test whose own code went wrong failed, and one whose fixture went wrong
    ended in an error, in its setup or in its teardown after a call that
    passed. Those two are what a leg that runs out of time names below the
    tests it was running.
    """
    ledger = pytester.path / "ledger"
    monkeypatch.setenv(LEDGER_VARIABLE, str(ledger))
    pytester.makepyfile(
        test_endings="""
import pytest


@pytest.fixture
def breaks_in_setup():
    raise RuntimeError("broken on the way in")


@pytest.fixture
def breaks_in_teardown():
    yield
    raise RuntimeError("broken on the way out")


def test_passes():
    pass


def test_fails():
    assert False


def test_setup_breaks(breaks_in_setup):
    pass


def test_teardown_breaks(breaks_in_teardown):
    pass


@pytest.mark.skip(reason="skipped on purpose")
def test_skipped():
    pass
"""
    )

    result = run_session(pytester)

    lines = per_test_lines(ledger_files(ledger))
    expected = {
        "test_passes": "passed",
        "test_fails": "failed",
        "test_setup_breaks": "error",
        "test_teardown_breaks": "error",
        "test_skipped": "skipped",
    }
    assert {name: outcomes_of(lines, name) for name in expected} == {
        name: [outcome] for name, outcome in expected.items()
    }, (lines, transcript(result))
    result.assert_outcomes(passed=2, failed=1, errors=2, skipped=1)


def test_each_xdist_worker_keeps_a_file_of_its_own(pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch) -> None:
    """A worker writes only its own file, so two workers never share one.

    The worker a line names is the id xdist gives it, the one a leg's log prints
    in `[gw0]` and in `worker 'gw0' crashed while running`.
    """
    ledger = pytester.path / "ledger"
    monkeypatch.setenv(LEDGER_VARIABLE, str(ledger))
    pytester.makepyfile(test_six="".join(f"def test_{index}():\n    pass\n\n\n" for index in range(6)))

    result = run_session(pytester, "-n", "2")

    files: dict[str, list[dict]] = {}
    for name, lines in ledger_files(ledger).items():
        recorded = per_test_lines({name: lines})
        if recorded:
            files[name] = recorded
    owners = {name: sorted({line["worker"] for line in lines}) for name, lines in files.items()}
    assert sorted(worker for workers in owners.values() for worker in workers) == ["gw0", "gw1"], (
        owners,
        transcript(result),
    )
    assert all(len(workers) == 1 for workers in owners.values()), owners
    everything = [line for lines in files.values() for line in lines]
    for index in range(6):
        assert sorted(events_of(everything, f"test_{index}")) == ["finish", "start"], (index, files)
    result.assert_outcomes(passed=6)


def test_a_test_killed_in_the_middle_is_named_with_its_worker(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The kill a runner's limit can end in, where no hook runs and no summary prints.

    `os._exit` ends the process on the spot. The start line of the test it ends
    is on disk only if it was flushed before the test began, and the reader has
    nothing else to go on.
    """
    ledger = pytester.path / "ledger"
    monkeypatch.setenv(LEDGER_VARIABLE, str(ledger))
    pytester.makepyfile(
        test_killed="""
import os


def test_finishes_before_the_kill():
    pass


def test_killed_in_the_middle():
    os._exit(1)


def test_never_reached():
    pass
"""
    )

    result = run_session(pytester)

    lines = per_test_lines(ledger_files(ledger))
    assert events_of(lines, "test_finishes_before_the_kill") == ["start", "finish"], (lines, transcript(result))
    killed = [line for line in lines if line["nodeid"].endswith("::test_killed_in_the_middle")]
    assert [line["event"] for line in killed] == ["start"], (lines, transcript(result))
    assert events_of(lines, "test_never_reached") == [], lines
    summary = tmp_path / "summary.md"

    completed = read_ledger(ledger, summary)

    assert_named(
        completed,
        summary,
        {killed[0]["nodeid"]: killed[0]["worker"]},
        ("test_finishes_before_the_kill", "test_never_reached"),
    )


@pytest.mark.parametrize("arguments", [(), ("-n", "1")], ids=["alone", "in-a-worker"])
def test_a_test_interrupted_in_the_middle_is_named_with_its_worker(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, arguments: tuple[str, ...]
) -> None:
    """A session interrupted with a test half run, the way Ctrl+C interrupts one.

    pytest still runs its own session teardown after an interrupt, so this
    ledger can say how its session ended, and what it says is that the session
    did not finish: the test that was running is named just as a killed one is.

    In a worker the interrupt is the worker's, and xdist ends the session with
    an exception of its own, the kind it also ends one with when `--maxfail`
    stops it. The session is still an interrupted one, because its worker was.
    """
    ledger = pytester.path / "ledger"
    monkeypatch.setenv(LEDGER_VARIABLE, str(ledger))
    pytester.makepyfile(
        test_interrupted="""
def test_finishes_before_the_interrupt():
    pass


def test_interrupted_in_the_middle():
    raise KeyboardInterrupt


def test_never_reached():
    pass
"""
    )

    result = run_session(pytester, *arguments)

    lines = per_test_lines(ledger_files(ledger))
    assert events_of(lines, "test_finishes_before_the_interrupt") == ["start", "finish"], (lines, transcript(result))
    interrupted = [line for line in lines if line["nodeid"].endswith("::test_interrupted_in_the_middle")]
    assert [line["event"] for line in interrupted] == ["start"], (lines, transcript(result))
    assert result.ret == pytest.ExitCode.INTERRUPTED, transcript(result)
    summary = tmp_path / "summary.md"

    completed = read_ledger(ledger, summary)

    assert_named(
        completed,
        summary,
        {interrupted[0]["nodeid"]: interrupted[0]["worker"]},
        ("test_finishes_before_the_interrupt", "test_never_reached"),
    )


def test_a_test_that_went_red_before_the_kill_is_named_below_the_one_cut_off(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The `F` a killed leg's row of dots shows, and never names.

    pytest names a failure in the summary it prints at the end of a session,
    and a session that is killed prints none, so a test that failed before the
    kill is named by the reader or by nobody. It is named below the test that
    was cut off, with its worker and how it ended; a test that passed before
    the kill is not named at all.
    """
    ledger = pytester.path / "ledger"
    monkeypatch.setenv(LEDGER_VARIABLE, str(ledger))
    pytester.makepyfile(
        test_red_before_the_kill="""
import os

import pytest


@pytest.fixture
def breaks_in_setup():
    raise RuntimeError("broken on the way in")


def test_assertion_that_does_not_hold():
    assert False


def test_with_a_broken_fixture(breaks_in_setup):
    pass


def test_passes_before_the_kill():
    pass


def test_killed_in_the_middle():
    os._exit(1)


def test_never_reached():
    pass
"""
    )

    result = run_session(pytester)

    lines = per_test_lines(ledger_files(ledger))
    killed = [line for line in lines if line["nodeid"].endswith("::test_killed_in_the_middle")]
    assert [line["event"] for line in killed] == ["start"], (lines, transcript(result))
    endings = {"test_assertion_that_does_not_hold": "failed", "test_with_a_broken_fixture": "error"}
    red = {
        line["nodeid"]: line["worker"]
        for line in lines
        if line["event"] == "finish" and line["nodeid"].rpartition("::")[2] in endings
    }
    assert len(red) == 2, (lines, transcript(result))
    summary = tmp_path / "summary.md"

    completed = read_ledger(ledger, summary)

    assert_named(
        completed,
        summary,
        {killed[0]["nodeid"]: killed[0]["worker"], **red},
        ("test_passes_before_the_kill", "test_never_reached"),
    )
    assert_listed_below(
        completed,
        summary,
        (killed[0]["nodeid"],),
        {nodeid: endings[nodeid.rpartition("::")[2]] for nodeid in red},
    )


def test_a_leg_whose_tests_failed_gets_nothing_added(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """pytest named the failure in its own summary, and a second voice would make
    the leg read as if it had run out of time."""
    ledger = pytester.path / "ledger"
    monkeypatch.setenv(LEDGER_VARIABLE, str(ledger))
    pytester.makepyfile(test_failing="def test_passes():\n    pass\n\n\ndef test_fails():\n    assert False\n")

    result = run_session(pytester)

    lines = per_test_lines(ledger_files(ledger))
    assert events_of(lines, "test_fails") == ["start", "finish"], transcript(result)
    assert outcomes_of(lines, "test_fails") == ["failed"], lines
    result.assert_outcomes(passed=1, failed=1)
    summary = tmp_path / "summary.md"
    assert_nothing_added(read_ledger(ledger, summary), summary)


def test_a_leg_that_failed_to_collect_gets_nothing_added(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A collection error ends pytest with the exit status an interrupt ends it with.

    Both are `ExitCode.INTERRUPTED`, and only one of them is a leg that ran out
    of time: pytest printed the collection error itself.
    """
    ledger = pytester.path / "ledger"
    monkeypatch.setenv(LEDGER_VARIABLE, str(ledger))
    pytester.makepyfile(
        test_fine="def test_fine():\n    pass\n",
        test_broken="import a_module_that_is_installed_nowhere\n",
    )

    result = run_session(pytester)

    assert "error during collection" in transcript(result), transcript(result)
    assert result.ret == pytest.ExitCode.INTERRUPTED, transcript(result)
    summary = tmp_path / "summary.md"
    assert_nothing_added(read_ledger(ledger, summary), summary)


def test_a_leg_whose_worker_crashed_gets_nothing_added(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A test that takes its worker down leaves a start and no finish, and is not cut off.

    xdist replaced the worker, the session went on to its end, and pytest named
    the test as the one that crashed, so the ledger's unfinished line is not a
    leg that ran out of time.
    """
    ledger = pytester.path / "ledger"
    monkeypatch.setenv(LEDGER_VARIABLE, str(ledger))
    pytester.makepyfile(
        test_crashing="import os\n\n\ndef test_takes_its_worker_down():\n    os._exit(1)\n\n\n"
        + "".join(f"def test_{index}():\n    pass\n\n\n" for index in range(3))
    )

    result = run_session(pytester, "-n", "2")

    lines = per_test_lines(ledger_files(ledger))
    assert events_of(lines, "test_takes_its_worker_down") == ["start"], (lines, transcript(result))
    assert "crashed while running" in transcript(result), transcript(result)
    result.assert_outcomes(passed=3, failed=1)
    summary = tmp_path / "summary.md"
    assert_nothing_added(read_ledger(ledger, summary), summary)


@pytest.mark.parametrize("value", [None, ""], ids=["unset", "empty"])
def test_a_run_without_a_ledger_directory_writes_no_ledger(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, value: str | None
) -> None:
    """Loaded and not asked for a ledger, the plugin writes nothing anywhere.

    An exported but empty variable is how a workflow spells "not set", and read
    as a path it is the working directory, so it counts as unset. Looked for in
    the session's working directory, which pytester also makes its HOME, and in
    every directory this test's environment names for temporary or per-user
    storage.
    """
    if value is not None:
        monkeypatch.setenv(LEDGER_VARIABLE, value)
    source = pytester.makepyfile(
        test_quiet="def test_ledger_probe_one():\n    pass\n\n\ndef test_ledger_probe_two():\n    pass\n"
    )

    result = run_session(pytester)

    assert result.ret == pytest.ExitCode.OK, transcript(result)
    result.assert_outcomes(passed=2)
    roots = {pytester.path}
    for name in ("TMPDIR", "TEMP", "TMP", "APPDATA", "LOCALAPPDATA"):
        if os.environ.get(name):
            roots.add(Path(os.environ[name]))
    mentions = [path for root in roots for path in files_mentioning(root, b"test_ledger_probe") if path != source]
    assert mentions == [], mentions


def test_a_session_a_test_starts_writes_nothing_into_the_ledger(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The ledger is the record of the session `Run tests` started, and of no other.

    This suite starts sessions of its own from inside its tests:
    `test_process_table_grouping.py` runs part of the suite on two workers, and
    `test_container_tier.py` collects a tier. Each inherits the environment of
    the worker that starts it. Writing into the ledger, such a session would
    put tests that never ran in `Run tests` beside the ones that did, and its
    ending could be read as the ending of the session that ran out of time.
    """
    ledger = pytester.path / "ledger"
    monkeypatch.setenv(LEDGER_VARIABLE, str(ledger))
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "test_nested.py").write_text("def test_in_the_nested_session():\n    pass\n", encoding="utf-8")
    pytester.makepyfile(
        test_outer=f"""
import subprocess
import sys


def test_starts_a_session_of_its_own():
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "{PLUGIN}", "-p", "no:cacheprovider", {str(nested)!r}],
        capture_output=True,
        text=True,
        timeout={scaled_time_bound(SESSION_TIMEOUT_S)!r},
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_runs_beside_it():
    pass
"""
    )

    result = run_session(pytester, "-n", "2")

    files = ledger_files(ledger)
    lines = per_test_lines(files)
    assert sorted(events_of(lines, "test_starts_a_session_of_its_own")) == ["finish", "start"], (
        lines,
        transcript(result),
    )
    assert "test_in_the_nested_session" not in json.dumps(files), files
    result.assert_outcomes(passed=2)


def test_every_test_a_leg_was_running_is_named_with_its_worker(tmp_path: Path) -> None:
    """Two workers cut off together, each in the middle of a test and after one it finished.

    Written by hand the way that kill leaves a ledger: a start and a finish for
    what completed, a start alone for what was running, and nothing saying the
    session finished, because the process that would have said so went with
    them.
    """
    ledger = tmp_path / "ledger"
    write_ledger(
        ledger,
        "gw0.jsonl",
        [
            started("tests/test_alpha.py::test_done_first", "gw0"),
            finished("tests/test_alpha.py::test_done_first", "gw0"),
            started("tests/test_alpha.py::test_cut_off_first", "gw0"),
        ],
    )
    write_ledger(
        ledger,
        "gw1.jsonl",
        [
            started("tests/test_beta.py::test_done_second", "gw1"),
            finished("tests/test_beta.py::test_done_second", "gw1"),
            started("tests/test_beta.py::test_cut_off_second", "gw1"),
        ],
    )
    summary = tmp_path / "summary.md"

    completed = read_ledger(ledger, summary)

    assert_named(
        completed,
        summary,
        {"tests/test_alpha.py::test_cut_off_first": "gw0", "tests/test_beta.py::test_cut_off_second": "gw1"},
        ("test_done_first", "test_done_second"),
    )


def test_every_test_that_went_red_before_the_end_is_listed_below_the_ones_cut_off(tmp_path: Path) -> None:
    """Two workers cut off, each after a test that failed or ended in an error.

    Both go below the two that were running, each with its own worker, and the
    tests that passed or were skipped before the end are not named.
    """
    ledger = tmp_path / "ledger"
    write_ledger(
        ledger,
        "gw0.jsonl",
        [
            started("tests/test_iota.py::test_done_first", "gw0"),
            finished("tests/test_iota.py::test_done_first", "gw0"),
            started("tests/test_iota.py::test_assertion_that_did_not_hold", "gw0"),
            finished("tests/test_iota.py::test_assertion_that_did_not_hold", "gw0", "failed"),
            started("tests/test_iota.py::test_cut_off_first", "gw0"),
        ],
    )
    write_ledger(
        ledger,
        "gw1.jsonl",
        [
            started("tests/test_kappa.py::test_with_a_broken_fixture", "gw1"),
            finished("tests/test_kappa.py::test_with_a_broken_fixture", "gw1", "error"),
            started("tests/test_kappa.py::test_left_out_on_purpose", "gw1"),
            finished("tests/test_kappa.py::test_left_out_on_purpose", "gw1", "skipped"),
            started("tests/test_kappa.py::test_cut_off_second", "gw1"),
        ],
    )
    summary = tmp_path / "summary.md"

    completed = read_ledger(ledger, summary)

    assert_named(
        completed,
        summary,
        {
            "tests/test_iota.py::test_cut_off_first": "gw0",
            "tests/test_kappa.py::test_cut_off_second": "gw1",
            "tests/test_iota.py::test_assertion_that_did_not_hold": "gw0",
            "tests/test_kappa.py::test_with_a_broken_fixture": "gw1",
        },
        ("test_done_first", "test_left_out_on_purpose"),
    )
    assert_listed_below(
        completed,
        summary,
        ("tests/test_iota.py::test_cut_off_first", "tests/test_kappa.py::test_cut_off_second"),
        {
            "tests/test_iota.py::test_assertion_that_did_not_hold": "failed",
            "tests/test_kappa.py::test_with_a_broken_fixture": "error",
        },
    )


def test_a_line_the_kill_cut_in_half_does_not_stop_the_reader(tmp_path: Path) -> None:
    """A process killed while writing leaves half a line, and the reader reads on.

    The half line is a finish that never landed, so its test still counts as
    running. A reader that stopped at the first line it could not parse would
    turn the one red this exists for back into a silent one.
    """
    ledger = tmp_path / "ledger"
    torn = write_ledger(ledger, "gw0.jsonl", [started("tests/test_gamma.py::test_cut_off_mid_write", "gw0")])
    with torn.open("a", encoding="utf-8") as stream:
        stream.write('{"event": "finish", "nodeid": "tests/test_gamma.py::test_cut_off_mid_wr')
    write_ledger(ledger, "gw1.jsonl", [started("tests/test_delta.py::test_cut_off_cleanly", "gw1")])
    summary = tmp_path / "summary.md"

    completed = read_ledger(ledger, summary)

    assert_named(
        completed,
        summary,
        {"tests/test_gamma.py::test_cut_off_mid_write": "gw0", "tests/test_delta.py::test_cut_off_cleanly": "gw1"},
        (),
    )


def test_a_leg_whose_suite_never_started_gets_nothing_added(tmp_path: Path) -> None:
    """The reader runs after any earlier failure, a lint failure among them, and
    then `Run tests` never ran and there is no ledger to read."""
    summary = tmp_path / "summary.md"

    assert_nothing_added(read_ledger(tmp_path / "never-written", summary), summary)


def test_a_test_id_the_annotation_would_misread_is_escaped(tmp_path: Path) -> None:
    """`%`, a carriage return and a line feed are syntax in an annotation's message.

    A parametrized id carrying a `%` is written the way the runner decodes back
    to the id, and the summary, which is a file and not a command, carries it
    as it is.
    """
    nodeid = "tests/test_epsilon.py::test_share[100%]"
    ledger = tmp_path / "ledger"
    write_ledger(ledger, "gw0.jsonl", [started(nodeid, "gw0")])
    summary = tmp_path / "summary.md"

    completed = read_ledger(ledger, summary)

    annotation = "\n".join(line for line in completed.stdout.splitlines() if line.startswith("::error"))
    assert "test_share[100%25]" in annotation, f"{completed.stdout}\n{completed.stderr}"
    assert "test_share[100%]" not in annotation, annotation
    assert nodeid in summary_of(summary), summary_of(summary)
