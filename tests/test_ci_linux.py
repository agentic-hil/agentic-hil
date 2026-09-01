"""What `tools/ci_linux.py` hands the container, and what it makes of the answer.

The tool's one documented promise about its arguments is that everything after
its own options reaches pytest unchanged. That promise is only worth as much as
the boundary between arguments survives the trip: the arguments go into a
`docker run ... bash -c` invocation, and a script that interpolates them into its
text hands every quote, `$` and `;` in a path or a `-k` expression to Bash, and
splits `-k "foo and bar"` into three arguments on the way.

The second half is what the tool does with the run. It used to forward the
container's exit status and assert nothing about what ran, which was observed
returning 0 after zero tests were collected: the clone inside the container
could not build a usable tree, and a green result came back for a suite that
never executed.

The third part is the queue. Several full-suite containers on one Docker daemon
starve each other until a run dies without a summary line, so a run holds a lock
file for its whole duration and queues behind whoever has it. What is tested
here is the decision, never a container and never a clock: the docker front is
faked, the answer to "is that pid alive" is faked, and the pause between two
attempts on the lock is the seam a test drives the queue through.

No Docker is needed for any of it: the command list is built before anything
runs, and the run is judged from its output.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import threading
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import ci_linux  # noqa: E402

# What a container that did its job prints last.
PLAUSIBLE_RESULT = "1476 passed, 35 skipped in 41.23s\n"


@pytest.fixture(autouse=True)
def a_machine_of_this_tests_own(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """No test in this file may reach the machine's real run lock.

    `main` takes `~/.agentic-hil/ci-linux.lock` for the length of a run, and
    every test here calls `main`. Pointing the home directory at `tmp_path`
    gives each test a machine of its own: nothing here can queue behind a real
    `ci_linux.py` an operator started, delete its lock, or make it wait for a
    test. The two environment variables are the two `os.path.expanduser` reads,
    Windows first.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HOME", str(home))
    return home


def a_lock_held_by(pid: int, **fields: object) -> Path:
    """Leave a holder record behind, as a run that took the lock would have."""
    record: dict[str, object] = {
        "version": ci_linux.CI_LOCK_RECORD_VERSION,
        "owner_id": "0123456789abcdef",
        "pid": pid,
        "host": socket.gethostname(),
        "started_at": "2026-09-01T09:12:33Z",
        "root": "/home/dev/agentic-hil",
        "image": "python:3.12",
        "pytest_args": ["-q"],
    }
    record.update(fields)
    path = ci_linux.ci_lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    return path


def only_these_are_running(monkeypatch: pytest.MonkeyPatch, *pids: int) -> None:
    """The fake liveness: every pid not named here belongs to a process that died."""
    monkeypatch.setattr(ci_linux, "process_is_running", lambda pid: pid in pids)


def never_waits(monkeypatch: pytest.MonkeyPatch) -> None:
    """A lock nobody holds must be taken, not queued on."""

    def refuse(seconds: float) -> None:
        raise AssertionError("the run waited for a lock it was entitled to break")

    monkeypatch.setattr(ci_linux, "wait_for_the_holder", refuse)


class FakeContainer:
    """The `docker run` process: some output, then an exit status."""

    def __init__(self, output: str, returncode: int) -> None:
        self.stdout = iter(output.splitlines(keepends=True))
        self._returncode = returncode

    def wait(self) -> int:
        return self._returncode


def stub_docker(
    monkeypatch: pytest.MonkeyPatch,
    output: str = PLAUSIBLE_RESULT,
    returncode: int = 0,
) -> list[list[str]]:
    """Replace docker with a container that prints `output` and exits `returncode`.

    Returns the list the commands are captured into.
    """
    captured: list[list[str]] = []

    class Completed:
        returncode = 0
        stdout = str(Path(__file__).resolve().parents[1])

    def fake_run(command: list[str], **kwargs: object) -> Completed:
        # `repository_root` shells out to git first; only the docker invocation
        # is the one under test.
        if command[:1] != ["git"]:
            captured.append(command)
        return Completed()

    def fake_popen(command: list[str], **kwargs: object) -> FakeContainer:
        captured.append(command)
        return FakeContainer(output, returncode)

    # The modules the tool reaches through, replaced on the tool rather than on
    # the real `subprocess` and `shutil`, so nothing else in this process runs
    # against a patched standard library.
    monkeypatch.setattr(ci_linux, "shutil", SimpleNamespace(which=lambda name: f"/usr/bin/{name}"))
    monkeypatch.setattr(
        ci_linux,
        "subprocess",
        SimpleNamespace(run=fake_run, Popen=fake_popen, PIPE=subprocess.PIPE, STDOUT=subprocess.STDOUT),
    )
    return captured


def built_command(monkeypatch: pytest.MonkeyPatch, argv: list[str]) -> list[str]:
    """Run `main` with docker stubbed out and return the command it assembled."""
    captured = stub_docker(monkeypatch)
    assert ci_linux.main(argv) == 0
    return captured[-1]


def pytest_arguments(command: list[str]) -> list[str]:
    """Everything after the script and its `$0`, which is what `"$@"` expands to."""
    index = command.index(ci_linux.SCRIPT)
    assert command[index + 1] == ci_linux.SCRIPT_ARGV0
    return command[index + 2 :]


def test_an_expression_with_spaces_stays_one_argument(monkeypatch: pytest.MonkeyPatch) -> None:
    """`-k "foo and bar"` is one argument to pytest and three to a shell."""
    command = built_command(monkeypatch, ["tests/test_hardening.py", "-k", "foo and bar", "-x"])

    assert pytest_arguments(command) == ["tests/test_hardening.py", "-k", "foo and bar", "-x"]


def test_shell_metacharacters_are_not_interpreted(monkeypatch: pytest.MonkeyPatch) -> None:
    """A path or expression carrying `$`, a quote or a `;` reaches pytest as
    written. Interpolated into the script text, Bash would have expanded the
    first and run the last."""
    hostile = "tests/test_it.py::test_x[$HOME; echo 'hi']"
    command = built_command(monkeypatch, [hostile])

    assert pytest_arguments(command) == [hostile]
    assert hostile not in ci_linux.SCRIPT


def test_the_script_forwards_the_positional_parameters(monkeypatch: pytest.MonkeyPatch) -> None:
    """The other half of the arrangement: the script has to consume them."""
    assert 'exec python -m pytest "$@"' in ci_linux.SCRIPT
    assert "{args}" not in ci_linux.SCRIPT


def test_no_arguments_still_runs_the_whole_suite_quietly(monkeypatch: pytest.MonkeyPatch) -> None:
    assert pytest_arguments(built_command(monkeypatch, [])) == ["-q"]


def test_the_separator_argparse_keeps_is_not_forwarded(monkeypatch: pytest.MonkeyPatch) -> None:
    assert pytest_arguments(built_command(monkeypatch, ["--", "-x"])) == ["-x"]


def test_only_the_wrappers_own_separator_is_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    """pytest ends its own option parsing with a `--` of its own, so "everything
    after the wrapper's options, unchanged" has to mean exactly that. Dropping
    every `--` turned `-- tests -- -x` into `tests -x` and changed what ran."""
    command = built_command(monkeypatch, ["--", "tests", "--", "-x"])

    assert pytest_arguments(command) == ["tests", "--", "-x"]


def test_a_separator_pytest_owns_survives_without_one_of_ours(monkeypatch: pytest.MonkeyPatch) -> None:
    assert pytest_arguments(built_command(monkeypatch, ["tests", "--", "-x"])) == ["tests", "--", "-x"]


def test_a_run_that_reported_no_result_at_all_is_not_a_pass(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    """The observed failure: exit 0, and not one test executed. Nothing in the
    output claims otherwise, and that absence is the whole signal."""
    stub_docker(monkeypatch, output="Cloning into '/work'...\nSuccessfully installed agentic-hil\n", returncode=0)

    assert ci_linux.main([]) == ci_linux.EXIT_NO_RESULT
    assert "no pytest summary line" in capsys.readouterr().err


def test_zero_collected_is_never_a_success_of_this_tool(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    stub_docker(monkeypatch, output="no tests ran in 0.01s\n", returncode=0)

    assert ci_linux.main([]) == ci_linux.EXIT_NO_RESULT
    assert "collected no tests" in capsys.readouterr().err


def test_a_clone_that_failed_goes_red_naming_the_clone(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    """A run that dies in the clone must not surface as a pytest step that never
    happened, and must not be softened by whatever the container exited with."""
    stub_docker(
        monkeypatch,
        output=f"error: unable to normalize alternate object path\n{ci_linux.CLONE_FAILURE_MARKER}: git clone of /src into /work failed\n",
        returncode=0,
    )

    assert ci_linux.main([]) == ci_linux.EXIT_CLONE_FAILED
    assert "clone the repository into a usable tree" in capsys.readouterr().err


def test_a_failing_suite_still_leaves_this_process_with_pytests_status(monkeypatch: pytest.MonkeyPatch) -> None:
    """The plausibility check adds a floor; it does not take over the result."""
    stub_docker(monkeypatch, output="==== 1 failed, 12 passed in 3.40s ====\n", returncode=1)

    assert ci_linux.main([]) == 1


def test_a_selection_that_only_skips_is_a_result(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skipped tests were collected. The floor is on what pytest found, not on
    what it chose to execute, or `ci_linux.py tests/test_windows_only.py` would
    fail on Linux for doing exactly what it is supposed to do."""
    stub_docker(monkeypatch, output="35 skipped in 1.20s\n", returncode=0)

    assert ci_linux.main([]) == 0


@pytest.mark.parametrize(
    ("line", "accounted"),
    [
        ("1476 passed, 35 skipped in 41.23s", 1511),
        ("======= 1 failed, 12 passed, 3 warnings in 3.40s =======", 13),
        ("37 tests collected in 0.42s", 37),
        ("1 error in 0.50s", 1),
        ("no tests ran in 0.01s", 0),
    ],
)
def test_the_summary_line_is_read_for_what_pytest_found(line: str, accounted: int) -> None:
    assert ci_linux.result_summary(f"some output\n{line}\n") is not None
    assert ci_linux.tests_accounted_for(ci_linux.result_summary(f"some output\n{line}\n")) == accounted


def test_a_line_that_merely_mentions_tests_is_not_a_summary() -> None:
    """The duration is what tells pytest's own last line from everything else
    that carries a number, including pip's."""
    assert ci_linux.result_summary("collected 1476 items\nSuccessfully installed 12 packages\n") is None


def posix_shell() -> str | None:
    """A shell that can run the container's clone step against this host's paths.

    `bash.exe` under System32 is the WSL launcher: it runs a Linux shell that
    cannot see the Windows paths this test writes, so it does not qualify.
    """
    bash = shutil.which("bash")
    if bash is None or Path(bash).parent.name.lower() == "system32":
        return None
    return bash


def commit_a_repository(path: Path) -> None:
    identity = ["-c", "user.email=ci@example.invalid", "-c", "user.name=ci", "-c", "commit.gpgsign=false"]
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    (path / "pyproject.toml").write_text("[project]\nname = 'sample'\n", encoding="utf-8")
    subprocess.run(["git", *identity, "-C", str(path), "add", "-A"], check=True)
    subprocess.run(["git", *identity, "-C", str(path), "commit", "-qm", "init"], check=True)


@pytest.mark.skipif(posix_shell() is None, reason="no POSIX shell that can address this host's paths")
def test_an_unresolvable_alternates_path_fails_the_clone_step(tmp_path: Path) -> None:
    """The tree the tool was observed passing on: its object store borrows
    through `.git/objects/info/alternates` at a path the clone cannot resolve.
    `git clone` dies, and the step that died has to be the one that is named."""
    origin, source, work = tmp_path / "origin", tmp_path / "src", tmp_path / "work"
    commit_a_repository(origin)
    subprocess.run(["git", "clone", "-q", "--shared", str(origin), str(source)], check=True)
    (source / ".git" / "objects" / "info" / "alternates").write_text(
        (tmp_path / "gone" / "objects").as_posix() + "\n", encoding="utf-8"
    )

    script = ci_linux.CLONE_SCRIPT.replace("/src", source.as_posix()).replace("/work", work.as_posix())
    result = subprocess.run([posix_shell(), "-c", script, "ci_linux"], capture_output=True, text=True, check=False)

    assert result.returncode == ci_linux.EXIT_CLONE_FAILED
    assert ci_linux.CLONE_FAILURE_MARKER in result.stdout + result.stderr
    assert not work.exists()


def test_the_container_script_keeps_the_clone_guard() -> None:
    """The guard is only worth anything if the script the container runs has it."""
    assert ci_linux.CLONE_SCRIPT in ci_linux.SCRIPT
    assert ci_linux.CLONE_FAILURE_MARKER in ci_linux.SCRIPT


def test_the_lock_lives_in_the_one_place_every_run_looks(a_machine_of_this_tests_own: Path) -> None:
    """Fixed under the home directory, like the device locks and for the same
    reason: two runs that resolve the lock differently are two runs that cannot
    see each other, which is the whole failure. Naming it also creates nothing,
    so a run that never starts leaves no directory behind."""
    assert ci_linux.ci_lock_path() == a_machine_of_this_tests_own / ".agentic-hil" / "ci-linux.lock"
    assert not ci_linux.ci_lock_path().exists()
    assert not (a_machine_of_this_tests_own / ".agentic-hil").exists()


def test_a_second_run_waits_for_the_holder_instead_of_starting_a_container(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """Two runs serialize. While a live holder has the lock, nothing is handed
    to docker; the wait ends when the holder's lock goes away, and only then
    does the container start. The holder finishing is scripted into the pause
    between two attempts, so what is asserted is the order of events and not the
    outcome of a race."""
    lock = a_lock_held_by(4242)
    only_these_are_running(monkeypatch, 4242)
    captured = stub_docker(monkeypatch)
    containers_started_while_waiting: list[int] = []

    def the_holder_finishes(seconds: float) -> None:
        containers_started_while_waiting.append(len(captured))
        lock.unlink()

    monkeypatch.setattr(ci_linux, "wait_for_the_holder", the_holder_finishes)

    assert ci_linux.main([]) == 0

    assert containers_started_while_waiting == [0]
    assert len(captured) == 1
    assert not lock.exists()
    err = capsys.readouterr().err
    assert "another run holds the lock: pid 4242" in err
    assert "started 2026-09-01T09:12:33Z" in err
    assert "--no-wait" in err


def test_a_long_wait_keeps_saying_who_it_is_waiting_for(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """A queue that goes quiet for twenty minutes cannot be told from a hang,
    and a full suite behind two others is a wait that long. The notice repeats.
    The clock is driven by the pause itself, so what is asserted here is the
    interval and not the passing of real time."""
    lock = a_lock_held_by(4242)
    only_these_are_running(monkeypatch, 4242)
    stub_docker(monkeypatch)
    clock = [0.0]
    monkeypatch.setattr(ci_linux, "time", SimpleNamespace(monotonic=lambda: clock[0]))

    def a_minute_passes(seconds: float) -> None:
        clock[0] += ci_linux.LOCK_NOTICE_INTERVAL_S
        if clock[0] >= 3 * ci_linux.LOCK_NOTICE_INTERVAL_S:
            lock.unlink()

    monkeypatch.setattr(ci_linux, "wait_for_the_holder", a_minute_passes)

    assert ci_linux.main([]) == 0

    err = capsys.readouterr().err
    assert err.count("pid 4242") == 3
    assert "still waiting after 120s" in err
    assert "the lock is free after 180s of waiting" in err


def test_the_lock_is_held_for_the_whole_container_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """Not for the moment before it: a lock released at the door would let the
    second container start while the first is still on the daemon, which is the
    starvation this exists to prevent. Asked from inside the running container,
    a second run is refused and names this process."""
    only_these_are_running(monkeypatch, os.getpid())
    stub_docker(monkeypatch)
    front = ci_linux.subprocess
    while_the_container_runs = front.Popen
    refusals: list[str] = []

    def a_second_run_tries(command: list[str], **kwargs: object) -> object:
        other = ci_linux.RunLock()
        try:
            other.acquire(wait=False)
        except ci_linux.RunLockBusy as busy:
            refusals.append(str(busy))
        else:
            other.release()
            refusals.append("the lock was free while a container was running")
        return while_the_container_runs(command, **kwargs)

    monkeypatch.setattr(front, "Popen", a_second_run_tries)

    assert ci_linux.main([]) == 0

    assert refusals and f"pid {os.getpid()}" in refusals[0]
    assert not ci_linux.ci_lock_path().exists()


def test_the_lock_is_given_back_even_when_the_run_goes_red(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failing suite is a finished run, and a finished run holds nothing."""
    stub_docker(monkeypatch, output="==== 1 failed, 12 passed in 3.40s ====\n", returncode=1)

    assert ci_linux.main([]) == 1
    assert not ci_linux.ci_lock_path().exists()


def test_a_lock_left_by_a_dead_holder_is_broken_and_the_reason_is_said(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """A crashed run must not take the machine with it. The lock is removed, the
    run starts, and the removal is stated: a lock file that disappeared without
    a word is indistinguishable from one that was never taken."""
    lock = a_lock_held_by(4242)
    only_these_are_running(monkeypatch, os.getpid())
    captured = stub_docker(monkeypatch)
    never_waits(monkeypatch)

    assert ci_linux.main([]) == 0

    assert len(captured) == 1
    assert not lock.exists()
    err = capsys.readouterr().err
    assert "held by pid 4242" in err
    assert "no longer running" in err


def test_a_lock_file_that_says_nothing_is_broken_and_the_reason_is_said(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """The other way a lock outlives its run: the file was created and its
    holder died before describing itself. There is nobody to name and nobody to
    check, and a file in that state can only be removed."""
    lock = ci_linux.ci_lock_path()
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("", encoding="utf-8")
    captured = stub_docker(monkeypatch)
    never_waits(monkeypatch)

    assert ci_linux.main([]) == 0

    assert len(captured) == 1
    assert "carries no readable holder record" in capsys.readouterr().err


def test_no_wait_refuses_naming_the_holder_and_starts_nothing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """The escape for somebody who knows better: refuse, say who has it, leave
    their lock exactly where it was, and exit with a status that is not a test
    failure so a retrying script cannot read it as one."""
    lock = a_lock_held_by(4242)
    only_these_are_running(monkeypatch, 4242)
    captured = stub_docker(monkeypatch)
    never_waits(monkeypatch)

    assert ci_linux.main(["--no-wait"]) == ci_linux.EXIT_LOCKED

    assert captured == []
    assert json.loads(lock.read_text(encoding="utf-8"))["pid"] == 4242
    err = capsys.readouterr().err
    assert "pid 4242" in err
    assert "started 2026-09-01T09:12:33Z" in err
    assert "--no-wait" in err


def test_no_wait_is_this_tools_option_and_never_reaches_pytest(monkeypatch: pytest.MonkeyPatch) -> None:
    assert pytest_arguments(built_command(monkeypatch, ["--no-wait", "tests", "-x"])) == ["tests", "-x"]


@pytest.mark.parametrize(("host", "named"), [("another-bench", "another-bench"), (None, "an unnamed host")])
def test_a_lock_from_another_machine_is_never_judged_by_a_local_pid(
    host: str | None, named: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """A home directory can be a roaming share, and a pid means nothing outside
    the machine that issued it. Here no process at all is running, so judging
    the record locally would call it dead and start a second container against a
    run that is very much alive. A record naming no machine is as foreign as one
    naming somebody else's: this tool always writes the field, so a record
    without it was not written by it and is nothing to judge a live run by."""
    a_lock_held_by(4242, host=host)
    only_these_are_running(monkeypatch)
    captured = stub_docker(monkeypatch)
    never_waits(monkeypatch)

    assert ci_linux.main(["--no-wait"]) == ci_linux.EXIT_LOCKED

    assert captured == []
    err = capsys.readouterr().err
    assert named in err
    assert "not written on this machine" in err


def test_a_lock_written_by_a_newer_version_of_this_tool_is_left_alone(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """Two checkouts at two commits on one machine is an ordinary day here. A
    record this version cannot read belongs to a run it cannot judge, and the
    older checkout queues behind the newer one rather than deleting its lock."""
    a_lock_held_by(4242, version=ci_linux.CI_LOCK_RECORD_VERSION + 1)
    only_these_are_running(monkeypatch)
    captured = stub_docker(monkeypatch)
    never_waits(monkeypatch)

    assert ci_linux.main(["--no-wait"]) == ci_linux.EXIT_LOCKED

    assert captured == []
    assert "different version of this tool" in capsys.readouterr().err


def test_a_run_never_queues_behind_its_own_record(monkeypatch: pytest.MonkeyPatch) -> None:
    """Taking the lock is a create followed by a read that confirms the record
    in the file is this run's. The read is injected to fail once here, the way a
    filesystem having a bad moment would fail it. What must not happen then is
    the run finding its own record, judging its own pid alive, and waiting for
    itself until somebody kills it."""
    real_read = ci_linux.read_lock_record
    reads: list[Path] = []

    def a_read_that_comes_back_empty_once(path: Path) -> dict | None:
        reads.append(path)
        return None if len(reads) == 1 else real_read(path)

    monkeypatch.setattr(ci_linux, "read_lock_record", a_read_that_comes_back_empty_once)
    never_waits(monkeypatch)
    lock = ci_linux.RunLock()

    lock.acquire()

    assert lock.held is True
    lock.release()
    assert not ci_linux.ci_lock_path().exists()


def test_a_run_that_would_break_a_dead_lock_yields_to_the_live_one_that_replaced_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The window the machine guard closes, told as one run's experience of it.

    A dead holder is on disk, and this run has every right to break it. But
    while it waits its turn at the guard, another run breaks the same dead lock
    and starts a container under a fresh, live record. When this run finally
    holds the guard it must read *that* record, recognise a live holder, and
    queue -- not remove it and start a second container against the run that now
    has the machine. Only once the live holder releases may this run take over.

    The takeover-by-a-rival is staged into the guard so the outcome is asserted,
    not raced: the first time this run enters the guard, a live record is already
    waiting where the dead one was.
    """
    a_lock_held_by(4242)  # a dead holder this run is entitled to break
    rival = os.getpid()  # the run that wins the takeover: this process, alive
    only_these_are_running(monkeypatch, rival)

    real_guard = ci_linux._machine_wide_guard
    a_rival_has_taken_over: list[bool] = []

    @contextmanager
    def a_rival_wins_before_we_reach_the_guard(path: Path):
        with real_guard(path):
            if not a_rival_has_taken_over:
                a_rival_has_taken_over.append(True)
                a_lock_held_by(rival)  # the live record a winning contender leaves
            yield

    monkeypatch.setattr(ci_linux, "_machine_wide_guard", a_rival_wins_before_we_reach_the_guard)

    def the_rival_finishes(seconds: float) -> None:
        # The live holder releases; only now is the machine this run's to take.
        ci_linux.ci_lock_path().unlink()

    monkeypatch.setattr(ci_linux, "wait_for_the_holder", the_rival_finishes)

    lock = ci_linux.RunLock()
    lock.acquire(wait=True)

    assert lock.held is True
    # The record on disk is this run's own, written only after the rival was
    # gone -- the rival's live record was waited out, never removed by this run.
    assert json.loads(ci_linux.ci_lock_path().read_text(encoding="utf-8"))["owner_id"] == lock.record["owner_id"]


@pytest.mark.skipif(
    os.name == "nt",
    reason="the guard's blocking take-over is POSIX flock; msvcrt retries rather than blocking, so the interleaving this forces is not the one that ships on Windows",
)
def test_two_runs_that_both_find_a_dead_holder_yield_exactly_one_container(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The race the guard exists for, run for real on two threads. Both read the
    same dead holder and both go to take the machine; without the guard each
    removes what the other just created and both would start a container. A
    barrier lines them up on the guard so their takeovers genuinely overlap, and
    the assertion is the invariant that must survive any interleaving: exactly
    one run comes out holding the lock, and one record -- a live one -- is left
    on disk."""
    a_lock_held_by(4242)
    only_these_are_running(monkeypatch, os.getpid())

    real_lock_fd = ci_linux._lock_fd_exclusive
    at_the_guard = threading.Barrier(2)

    def both_reach_the_guard_together(descriptor: int) -> None:
        # Each contender takes the guard exactly once here -- the winner to
        # break and create, the loser to read the winner's live record -- so the
        # barrier releases them into the lock together and the OS serialises the
        # takeover, not the test.
        at_the_guard.wait(timeout=10)
        real_lock_fd(descriptor)

    monkeypatch.setattr(ci_linux, "_lock_fd_exclusive", both_reach_the_guard_together)

    outcomes: list[str] = []
    record_outcome = threading.Lock()

    def contend() -> None:
        run = ci_linux.RunLock()
        try:
            run.acquire(wait=False)
            verdict = "held"
        except ci_linux.RunLockBusy:
            verdict = "busy"
        with record_outcome:
            outcomes.append(verdict)

    threads = [threading.Thread(target=contend) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)

    assert not any(thread.is_alive() for thread in threads)
    assert sorted(outcomes) == ["busy", "held"]
    surviving = json.loads(ci_linux.ci_lock_path().read_text(encoding="utf-8"))
    assert surviving["pid"] == os.getpid()


@pytest.mark.skipif(
    os.name == "nt",
    reason="the guard's blocking take-over is POSIX flock; msvcrt retries rather than blocking, so the interleaving this forces is not the one that ships on Windows",
)
def test_a_partial_first_write_is_never_taken_for_a_stale_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The publication window, run for real on two threads. A create makes the
    lock file visible with `O_EXCL` an instant before it writes the record into
    it. A contender that fails its own create must not read that empty file,
    judge it stale, break it, and start a second container against a creator
    that is about to return holding the lock.

    The two roles are staged so the outcome is asserted, not raced. The creator
    opens the file and pauses before writing the record. If it holds the guard
    while it pauses -- what the fix guarantees -- the contender blocks at the
    guard and cannot read the empty file at all, so the creator waits only until
    the contender has reached the guard, then finishes; the contender goes on to
    read a whole record and yield. If the create were *not* under the guard --
    the bug this exists to catch -- the contender would take the free guard,
    read the empty file, be held at the break until the creator had returned
    holding the lock, and only then break it: two holders, which the assertions
    reject.
    """
    only_these_are_running(monkeypatch, os.getpid())

    creator_thread: list[threading.Thread] = []
    creator_took_guard = threading.Event()
    first_file_is_visible = threading.Event()
    contender_reached_guard = threading.Event()
    contender_about_to_break = threading.Event()
    creator_has_returned = threading.Event()

    def is_creator() -> bool:
        return bool(creator_thread) and threading.current_thread() is creator_thread[0]

    real_lock_fd = ci_linux._lock_fd_exclusive

    def lock_fd_that_marks_who_is_at_the_guard(descriptor: int) -> None:
        # Fired just before the blocking take-over of the guard, by whichever
        # role reaches it: the creator sets a flag the pause below reads to tell
        # the fix from the bug; the contender announces it is at the guard so the
        # creator holding it knows the contender can only be blocked there.
        (creator_took_guard if is_creator() else contender_reached_guard).set()
        real_lock_fd(descriptor)

    monkeypatch.setattr(ci_linux, "_lock_fd_exclusive", lock_fd_that_marks_who_is_at_the_guard)

    real_dumps = json.dumps

    def dumps_that_pauses_the_creator_mid_publish(obj: object, **kwargs: object) -> str:
        # Called from `_create`, after `O_EXCL` has made the file visible and
        # before its record is written: the empty publication window itself.
        if is_creator():
            first_file_is_visible.set()
            if creator_took_guard.is_set():
                # The fix: the creator holds the guard, so a contender can only
                # block at it. Wait until it has, proving it stays blocked, then
                # finish -- no contender ever reads the empty file.
                contender_reached_guard.wait(timeout=10)
            else:
                # The bug: the create is not under the guard. Let the contender
                # read the empty file and reach the break, then finish and return
                # holding the lock before it breaks -- the double-acquire.
                contender_about_to_break.wait(timeout=10)
        return real_dumps(obj, **kwargs)

    monkeypatch.setattr(
        ci_linux,
        "json",
        SimpleNamespace(dumps=dumps_that_pauses_the_creator_mid_publish, loads=json.loads),
    )

    real_break = ci_linux.RunLock._break

    def break_that_waits_for_the_creator_to_return(self: ci_linux.RunLock) -> None:
        # Only reached on the buggy path, where the contender read the empty file
        # as stale. Holding it here until the creator has returned holding the
        # lock is what turns the window into the two-holder outcome the assertion
        # then rejects; the fix never lets the contender read, so this never runs.
        if not is_creator():
            contender_about_to_break.set()
            creator_has_returned.wait(timeout=10)
        real_break(self)

    monkeypatch.setattr(ci_linux.RunLock, "_break", break_that_waits_for_the_creator_to_return)

    verdicts: dict[str, str] = {}
    record_verdict = threading.Lock()

    def verdict_of(run: ci_linux.RunLock) -> str:
        try:
            run.acquire(wait=False)
        except ci_linux.RunLockBusy:
            return "busy"
        return "held" if run.held else "busy"

    def be_the_creator() -> None:
        outcome = verdict_of(ci_linux.RunLock())
        with record_verdict:
            verdicts["creator"] = outcome
        creator_has_returned.set()

    def be_the_contender() -> None:
        first_file_is_visible.wait(timeout=10)
        outcome = verdict_of(ci_linux.RunLock())
        with record_verdict:
            verdicts["contender"] = outcome

    creator = threading.Thread(target=be_the_creator, name="creator")
    contender = threading.Thread(target=be_the_contender, name="contender")
    creator_thread.append(creator)
    creator.start()
    contender.start()
    for thread in (creator, contender):
        thread.join(timeout=15)

    assert not creator.is_alive() and not contender.is_alive()
    assert verdicts.get("creator") == "held"
    assert verdicts.get("contender") == "busy"
    surviving = json.loads(ci_linux.ci_lock_path().read_text(encoding="utf-8"))
    assert surviving["pid"] == os.getpid()


def test_asking_whether_a_process_runs_must_not_end_it() -> None:
    """`os.kill(pid, 0)` is a liveness probe on POSIX and a kill on Windows,
    where CPython maps every signal but the console events onto
    TerminateProcess. The probe is asked about a real child here, and the child
    has to survive being asked about: a Windows implementation that reached for
    `os.kill` would answer this question by killing the run it was asked about."""
    child = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()"], stdin=subprocess.PIPE)
    try:
        assert ci_linux.process_is_running(child.pid) is True
        assert child.poll() is None
    finally:
        child.stdin.close()
        child.kill()
        child.wait()
