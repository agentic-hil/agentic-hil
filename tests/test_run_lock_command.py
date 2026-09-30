"""The machine's run lock as a command, for callers that are not these Python tools.

`tools/run_lock.py` is the queue every container run on this machine takes its
turn in. Two callers cannot import it: a workflow whose hardware steps are
separate steps of one job, and a run by hand of something that is not one of
the tools beside it. Both take the same lock through the same code, as a
command, so nothing on the machine meets them as `device_busy` halfway through
a plan.

`take` holds the machine for the life of the process that started it, the
job's own process when a step runs it with exec, and records it as the holder,
so the lock outlives the step that took it and falls with the job however the
job ends. `give-back` returns it from a later step. `run -- <command>` holds it
for one command's life and passes the command's status through.

What is proved here with real processes is the part a fake cannot show: that
the record names a process whose death the next run can see, so a job or a
wrapper that is killed leaves a lock the next run breaks as stale instead of
one it waits behind for ever. The queue itself is driven through the same seam
the other tools' tests use.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import suppress
from pathlib import Path

import pytest
from support import scaled_time_bound

TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))

import bench_in_container  # noqa: E402
import run_lock  # noqa: E402

SCRIPT = TOOLS / "run_lock.py"
# The interpreter itself. A virtual environment on Windows puts a launcher at
# sys.executable that starts the base interpreter as a child of its own, so a
# process started from it does not have the pid of the Python that runs the
# script, and these tests are about exactly those pids. Everything started here
# needs the standard library alone.
PYTHON = getattr(sys, "_base_executable", None) or sys.executable
WORKFLOW = ".github/workflows/hardware-bench.yml"


@pytest.fixture(autouse=True)
def a_machine_of_this_tests_own(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """No test here may reach the machine's real run lock, or be reached by it.

    The home directory is where the lock lives, so pointing it at `tmp_path`
    gives each test a machine of its own. The environment is what the commands
    started below inherit, so they queue on the same private file.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HOME", str(home))
    return home


@pytest.fixture(autouse=True)
def a_job_started_this_command(monkeypatch: pytest.MonkeyPatch) -> Iterator[int]:
    """A live process of the test's own, as the parent of a `take` called here.

    A `take` called in this process would otherwise follow this suite's own
    parent, which the test does not choose: in the Linux container that runs
    the suite it is pid 1, which `take` rightly refuses as a process that lives
    as long as the machine. A sleeping process stands in for the job whose step
    started the command, and lives until the test ends. That a real step's
    parent is the job is what the tests below that start a job of their own
    show.
    """
    job = subprocess.Popen([PYTHON, "-c", "import time; time.sleep(600)"])
    monkeypatch.setattr(os, "getppid", lambda: job.pid)
    try:
        yield job.pid
    finally:
        stop(job)


def a_lock_held_by(pid: int, **fields: object) -> Path:
    """Leave a holder record behind, as a run that took the lock would have."""
    record: dict[str, object] = {
        "version": run_lock.LOCK_RECORD_VERSION,
        "owner_id": "0123456789abcdef",
        "pid": pid,
        "host": socket.gethostname(),
        "started_at": "2026-09-01T09:12:33Z",
        "root": str(Path(os.path.expanduser("~")) / "work" / "agentic-hil"),
        "tool": "tools/bench_in_container.py",
        "runs_for": "10 to 30 minutes",
    }
    record.update(fields)
    path = run_lock.lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    return path


def the_record() -> dict:
    return json.loads(run_lock.lock_path().read_text(encoding="utf-8"))


def take(state: Path, *extra: str, pid: int | None = None) -> int:
    return run_lock.main(
        [
            "take",
            "--state",
            str(state),
            "--tool",
            WORKFLOW,
            "--runs-for",
            "about a minute",
            "--for-the-life-of",
            str(os.getppid() if pid is None else pid),
            *extra,
        ]
    )


def a_finished_process() -> int:
    process = subprocess.Popen([PYTHON, "-c", "pass"])
    process.wait()
    return process.pid


def names_this_machine(text: str) -> list[str]:
    """The identities of this machine a line of public output must not carry."""
    found = []
    for value in bench_in_container.host_identities():
        value = value.strip()
        if len(value) < bench_in_container.SHORTEST_WITHHELD or value.lower() in bench_in_container.TOO_COMMON:
            continue
        if value.lower() in text.lower():
            found.append(value)
    return found


def eventually(condition, what: str, seconds: float = 20.0) -> None:
    deadline = time.monotonic() + scaled_time_bound(seconds)
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError(f"not within {seconds}s: {what}")
        time.sleep(0.05)


def stop(process: subprocess.Popen) -> None:
    if process.poll() is None:
        process.kill()
    process.wait()


def stop_pid(pid: int) -> None:
    # SIGTERM ends a sleeping Python on POSIX, and on Windows os.kill with it is
    # TerminateProcess: either way the leftover of a test is gone. Never 0 or
    # below, which on POSIX addresses a whole process group, this test's own.
    if pid <= 0:
        return
    with suppress(OSError):
        os.kill(pid, signal.SIGTERM)


# take


def test_take_holds_the_machine_for_the_life_of_the_process_that_started_it(tmp_path: Path) -> None:
    state = tmp_path / "run-lock.json"

    assert take(state) == 0

    record = the_record()
    assert record["pid"] == os.getppid()
    assert record["host"] == socket.gethostname()
    assert record["tool"] == WORKFLOW
    assert record["runs_for"] == "about a minute"
    assert record["version"] == run_lock.LOCK_RECORD_VERSION
    # A live holder: the next run queues behind it rather than breaking it.
    assert run_lock.stale_reason(record, run_lock.lock_path()) is None
    kept = json.loads(state.read_text(encoding="utf-8"))
    assert kept["owner_id"] == record["owner_id"]
    assert kept["held"] is True


@pytest.mark.parametrize("which", ["itself", "a finished process"])
def test_take_refuses_a_holder_that_is_not_the_process_that_started_it(
    which: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A lock for the life of the command itself would end with the step that
    took it, and one for a process that is gone would be broken by the next
    run at once: either way the steps after it would run unprotected."""
    state = tmp_path / "run-lock.json"
    pid = os.getpid() if which == "itself" else a_finished_process()

    assert take(state, pid=pid) == 2

    assert not run_lock.lock_path().exists()
    assert not state.exists()
    said = capsys.readouterr().err
    assert f"pid {pid}" in said
    assert f"pid {os.getppid()}" in said


def test_take_refuses_a_state_file_an_earlier_take_left(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A second take on the same state would forget the first one's record,
    which nothing could then give back."""
    state = tmp_path / "run-lock.json"
    assert take(state) == 0
    first = the_record()

    assert take(state) == 2

    assert the_record() == first
    assert json.loads(state.read_text(encoding="utf-8"))["owner_id"] == first["owner_id"]
    assert str(state.name) in capsys.readouterr().err


def test_take_queues_behind_a_live_holder_and_says_whom_without_naming_this_machine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Queued rather than refused, the way the container runner takes it, and
    the notice is fit for a public log: the holder's pid and what it runs stay,
    this machine's name and home directory are withheld."""
    state = tmp_path / "run-lock.json"
    a_lock_held_by(4242)
    monkeypatch.setattr(run_lock, "process_is_running", lambda pid: pid in {4242, os.getppid()})
    waits: list[float] = []

    def the_holder_finishes(seconds: float) -> None:
        waits.append(seconds)
        run_lock.lock_path().unlink()

    monkeypatch.setattr(run_lock, "wait_for_the_holder", the_holder_finishes)

    assert take(state) == 0

    assert waits
    assert the_record()["pid"] == os.getppid()
    said = capsys.readouterr().err
    assert "another run holds the lock: pid 4242" in said
    assert "tools/bench_in_container.py" in said
    assert bench_in_container.WITHHELD in said
    assert names_this_machine(said) == []


def test_the_record_says_when_the_machine_was_taken_rather_than_when_the_wait_began(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A take can wait hours for the machine, and the run queued behind it is
    told when its holder started: a time from before the wait would tell it
    the holder had been on the board all along."""
    state = tmp_path / "run-lock.json"
    a_lock_held_by(4242)
    monkeypatch.setattr(run_lock, "process_is_running", lambda pid: pid in {4242, os.getppid()})
    now = ["2026-09-28T00:24:09Z"]
    monkeypatch.setattr(run_lock, "utc_now_iso", lambda: now[0])

    def the_holder_finishes_later(seconds: float) -> None:
        now[0] = "2026-09-28T01:11:40Z"
        run_lock.lock_path().unlink()

    monkeypatch.setattr(run_lock, "wait_for_the_holder", the_holder_finishes_later)

    assert take(state) == 0

    assert the_record()["started_at"] == "2026-09-28T01:11:40Z"


def test_take_with_no_wait_refuses_a_held_machine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    state = tmp_path / "run-lock.json"
    a_lock_held_by(4242)
    monkeypatch.setattr(run_lock, "process_is_running", lambda pid: pid in {4242, os.getppid()})

    assert take(state, "--no-wait") == 5

    assert the_record()["pid"] == 4242
    assert not state.exists()
    said = capsys.readouterr().err
    assert "pid 4242" in said
    assert names_this_machine(said) == []


# give-back


def test_give_back_returns_the_machine_once(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    state = tmp_path / "run-lock.json"
    assert take(state) == 0

    assert run_lock.main(["give-back", "--state", str(state)]) == 0

    assert not run_lock.lock_path().exists()
    assert not state.exists()
    capsys.readouterr()
    # A second give-back, as a cleanup step after a failed one would run it,
    # has nothing to do and does not fail the job for it.
    assert run_lock.main(["give-back", "--state", str(state)]) == 0
    assert "nothing to give back" in capsys.readouterr().err


def test_give_back_removes_the_record_of_a_take_interrupted_before_it_said_so(tmp_path: Path) -> None:
    """The record is published before the take can write that it holds it, so
    a take stopped in between leaves a record only the owner id recognises."""
    state = tmp_path / "run-lock.json"
    assert take(state) == 0
    state.write_text(json.dumps({"owner_id": the_record()["owner_id"], "held": False}), encoding="utf-8")

    assert run_lock.main(["give-back", "--state", str(state)]) == 0

    assert not run_lock.lock_path().exists()


def test_give_back_never_removes_another_runs_lock_and_says_the_machine_was_lost(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A job whose lock was broken and taken while it believed it held the
    machine may have shared the board with another run: that fails the job
    instead of passing unnoticed, and the other run keeps its lock."""
    state = tmp_path / "run-lock.json"
    assert take(state) == 0
    run_lock.lock_path().unlink()
    a_lock_held_by(4242)

    assert run_lock.main(["give-back", "--state", str(state)]) == 1

    assert the_record()["pid"] == 4242
    said = capsys.readouterr().err
    assert "pid 4242" in said
    assert names_this_machine(said) == []


def test_give_back_without_a_take_has_nothing_to_do(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    a_lock_held_by(4242)

    assert run_lock.main(["give-back", "--state", str(tmp_path / "never-written.json")]) == 0

    assert the_record()["pid"] == 4242
    assert "nothing to give back" in capsys.readouterr().err


# An interrupted holder


# A job's process: it runs the take as a step with exec would, as its own
# child, prints the take's status, and then lives until it is killed.
A_JOB = """
import os, subprocess, sys, time
script, state = sys.argv[1], sys.argv[2]
status = subprocess.call([sys.executable, script, "take", "--state", state, "--tool", "a job",
                          "--runs-for", "a while", "--for-the-life-of", str(os.getpid())])
print(status, flush=True)
time.sleep(600)
"""


def test_the_lock_of_a_job_that_died_is_broken_as_stale_by_the_next_run(tmp_path: Path) -> None:
    state = tmp_path / "run-lock.json"
    job = subprocess.Popen([PYTHON, "-c", A_JOB, str(SCRIPT), str(state)], stdout=subprocess.PIPE, text=True)
    try:
        assert job.stdout.readline().strip() == "0"
        assert the_record()["pid"] == job.pid
        # While the job lives, the next run queues.
        with pytest.raises(run_lock.RunLockBusy):
            run_lock.RunLock(tool="the next run").acquire(wait=False)
    finally:
        stop(job)

    said: list[str] = []
    following = run_lock.RunLock(tool="the next run", announce=said.append)
    following.acquire(wait=False)
    try:
        assert the_record()["owner_id"] == following.record["owner_id"]
        assert any(f"held by pid {job.pid}" in line and "no longer running" in line for line in said)
    finally:
        following.release()


# run --

# The command a run holds the machine for: it reports the holder record it
# finds and leaves with a status of its own.
A_COMMAND = """
import json, os, sys
from pathlib import Path
record = json.loads((Path(os.path.expanduser("~")) / ".agentic-hil" / "ci-linux.lock").read_text(encoding="utf-8"))
print(record["pid"], record["tool"])
sys.exit(int(sys.argv[1]))
"""


@pytest.mark.parametrize("status", [0, 3])
def test_run_holds_the_machine_for_the_commands_life_and_passes_its_status_through(status: int) -> None:
    wrapper = subprocess.Popen(
        [
            PYTHON,
            str(SCRIPT),
            "run",
            "--tool",
            "a run by hand",
            "--",
            PYTHON,
            "-c",
            A_COMMAND,
            str(status),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        out, _err = wrapper.communicate(timeout=scaled_time_bound(60))
    finally:
        stop(wrapper)

    assert wrapper.returncode == status
    assert out.split() == [str(wrapper.pid), "a", "run", "by", "hand"]
    assert not run_lock.lock_path().exists()


# The command of a run whose wrapper is killed: it says where it is and waits.
A_LONG_COMMAND = """
import os, sys, time
from pathlib import Path
Path(sys.argv[1]).write_text(str(os.getpid()), encoding="utf-8")
time.sleep(600)
"""


def test_the_lock_of_a_run_whose_wrapper_was_killed_is_broken_as_stale_by_the_next_run(tmp_path: Path) -> None:
    started = tmp_path / "command.pid"
    wrapper = subprocess.Popen(
        [PYTHON, str(SCRIPT), "run", "--", PYTHON, "-c", A_LONG_COMMAND, str(started)],
        stderr=subprocess.DEVNULL,
    )
    try:
        eventually(started.exists, "the command started under the lock")
        eventually(lambda: started.read_text(encoding="utf-8").strip() != "", "the command wrote its pid")
        assert the_record()["pid"] == wrapper.pid
    finally:
        stop(wrapper)
        if started.exists() and started.read_text(encoding="utf-8").strip():
            stop_pid(int(started.read_text(encoding="utf-8")))

    said: list[str] = []
    following = run_lock.RunLock(tool="the next run", announce=said.append)
    following.acquire(wait=False)
    try:
        assert any(f"held by pid {wrapper.pid}" in line and "no longer running" in line for line in said)
    finally:
        following.release()


def test_run_with_no_wait_behind_a_live_holder_runs_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    ran = tmp_path / "ran"
    a_lock_held_by(4242)
    monkeypatch.setattr(run_lock, "process_is_running", lambda pid: pid == 4242)

    status = run_lock.main(["run", "--no-wait", "--", sys.executable, "-c", f"open({str(ran)!r}, 'w').close()"])

    assert status == 5
    assert not ran.exists()
    assert the_record()["pid"] == 4242
    assert names_this_machine(capsys.readouterr().err) == []


@pytest.mark.parametrize("argv", [["run"], ["run", "--"]])
def test_run_without_a_command_is_refused(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as refused:
        run_lock.main(argv)

    assert refused.value.code == 2
    assert not run_lock.lock_path().exists()


def test_run_of_a_command_that_cannot_start_gives_the_machine_back(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    status = run_lock.main(["run", "--", str(tmp_path / "no-such-command")])

    assert status == 127
    assert not run_lock.lock_path().exists()
    assert "no-such-command" in capsys.readouterr().err


# A command that waits to be told to stop, and takes its time over it, the
# way the tier puts the demo back on the board when it is interrupted.
A_COMMAND_THAT_CLEANS_UP = """
import signal, sys, time
from pathlib import Path
def stopping(signum, frame):
    time.sleep(1)
    Path(sys.argv[1]).write_text("cleaned up", encoding="utf-8")
    sys.exit(7)
signal.signal(signal.SIGTERM, stopping)
Path(sys.argv[2]).write_text("started", encoding="utf-8")
time.sleep(600)
"""


@pytest.mark.skipif(os.name == "nt", reason="SIGTERM on Windows is TerminateProcess, which no process can answer")
def test_run_passes_a_termination_on_and_holds_the_machine_until_the_command_is_done(tmp_path: Path) -> None:
    cleaned, started = tmp_path / "cleaned", tmp_path / "started"
    wrapper = subprocess.Popen(
        [
            PYTHON,
            str(SCRIPT),
            "run",
            "--",
            PYTHON,
            "-c",
            A_COMMAND_THAT_CLEANS_UP,
            str(cleaned),
            str(started),
        ],
        stderr=subprocess.DEVNULL,
    )
    try:
        eventually(started.exists, "the command started under the lock")
        wrapper.send_signal(signal.SIGTERM)
        # The command is still cleaning up, and the machine is still held.
        time.sleep(0.3)
        assert wrapper.poll() is None
        assert the_record()["pid"] == wrapper.pid
        assert wrapper.wait(timeout=scaled_time_bound(30)) == 7
    finally:
        stop(wrapper)

    assert cleaned.read_text(encoding="utf-8") == "cleaned up"
    assert not run_lock.lock_path().exists()
