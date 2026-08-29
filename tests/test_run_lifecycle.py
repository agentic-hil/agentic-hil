"""A run that outlives the command that started it, and one that can be asked to
end without being killed.

The stop half is asked of the reactor in this process, because what it is about
is where the question is asked and what the run does with the answer. The
detached half is asked of real worker processes, because what it is about is a
run that is somewhere else: a handle that names it, a record that says what it
is doing, and the difference between a worker that ended and a worker that was
killed.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from contextlib import suppress
from pathlib import Path

import pytest
from conftest import FAKE_OPENOCD, write_authoritative_config, write_config
from test_test_reactor import RecordingService, write_test_config

from agentic_hil.config import ConfigError, load_authoritative_config, load_config
from agentic_hil.test_reactor import TestReactor, load_test_config

LONG_DELAY_PLAN = "version: 4\nsteps:\n  - {device: dut, action: delay, duration_ms: 600000}\n"
# The same, with one step that actually drives the probe first, so a worker
# killed during the wait is a worker killed having reached the board.
KILLED_WORKER_PLAN = "version: 4\nsteps:\n  - {device: dut, action: reset}\n  - {device: dut, action: delay, duration_ms: 600000}\n"


class StopOnQuestion:
    """A stop request that arrives at a chosen moment.

    Answers no the first `after` times it is asked and yes from then on. The
    reactor asks before every step and once per slice of a wait, so counting the
    questions is how a test places the request exactly between two named steps
    or inside one named wait, without racing a real clock."""

    def __init__(self, after: int) -> None:
        self.after = after
        self.asked = 0

    def __call__(self) -> bool:
        self.asked += 1
        return self.asked > self.after


def bench_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, plan_text: str) -> tuple[Path, Path]:
    """A workspace with an authoritative config and one plan, as a real run has.

    The probe gets its own copy of the fake backend, because the device locks
    these tests are about are machine-wide and keyed on the hardware: one shared
    executable path would make every test here declare the same board, and a
    test whose worker is still on its way out would refuse the next test's run
    rather than let it fail its own assertion."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    probe = tmp_path / "fake_openocd.py"
    probe.write_bytes(FAKE_OPENOCD.read_bytes())
    write_authoritative_config(workspace, monkeypatch, debugger_executable=probe)
    monkeypatch.chdir(workspace)
    plan = workspace / ".agentic-hil" / "testconfig.yaml"
    plan.parent.mkdir(parents=True, exist_ok=True)
    plan.write_text(plan_text, encoding="utf-8")
    return workspace, plan


def kill_hard(pid: int) -> None:
    """End a process the way a lost terminal or a reboot does: without giving it
    a chance to close anything."""
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        return
    with suppress(ProcessLookupError):
        os.kill(pid, signal.SIGKILL)


def wait_for_state(config, handle: str, states: set[str], timeout_s: float = 60.0) -> dict:
    from agentic_hil.runlifecycle import run_status

    deadline = time.monotonic() + timeout_s
    while True:
        status = run_status(config, handle)
        if status.get("state") in states:
            return status
        assert time.monotonic() < deadline, status
        time.sleep(0.1)


def worker_log(config, handle: str) -> str:
    """What the worker printed, which is the only place a failure that stopped it
    before it could write a report shows up."""
    from agentic_hil.runlifecycle import runs_directory

    path = runs_directory(config) / f"{handle}.log"
    return path.read_text(encoding="utf-8", errors="replace") if path.is_file() else "<no worker log>"


def last_report(workspace: Path) -> dict:
    path = workspace / ".agentic-hil" / "reports" / "last-report.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


def wait_for_progress_step(workspace: Path, config, handle: str, step: int, timeout_s: float = 60.0) -> None:
    """Wait until the run says it is on a given top-level step.

    A run that reached a terminal state without ever getting there is not worth
    waiting out: what it did instead is in its report, and that is what a reader
    of the failure needs rather than a minute of nothing."""
    from agentic_hil.runlifecycle import TERMINAL_RUN_STATES, read_run_record

    deadline = time.monotonic() + timeout_s
    while True:
        record = read_run_record(config, handle) or {}
        if (record.get("progress") or {}).get("step") == step:
            return
        assert record.get("state") not in TERMINAL_RUN_STATES, (record, last_report(workspace))
        assert time.monotonic() < deadline, (record, last_report(workspace))
        time.sleep(0.05)


@pytest.fixture()
def detached_runs():
    """Every detached run a test started, ended before the next test begins.

    The device locks are machine-wide and keyed on the hardware, and every test
    here declares the same fake probe, so a worker still holding it would refuse
    the next test's run rather than fail its own assertion. Killed rather than
    asked to stop, because by teardown the test has already made its point and a
    kill is the shorter path to a released lock."""
    from agentic_hil.runlifecycle import read_run_record, worker_is_gone

    started: list[tuple[object, str]] = []
    yield started
    for config, handle in started:
        with suppress(Exception):
            record = read_run_record(config, handle)
            if record is not None and isinstance(record.get("pid"), int):
                kill_hard(int(record["pid"]))
            deadline = time.monotonic() + 30
            while not worker_is_gone(config, handle) and time.monotonic() < deadline:
                time.sleep(0.05)


# --- the cooperative stop --------------------------------------------------


def test_a_stop_is_honoured_between_two_steps(tmp_path: Path) -> None:
    # Asked before a step rather than after, so a run that has been asked to end
    # does not start work nobody will judge it on.
    config = load_config(str(write_config(tmp_path)))
    path = write_test_config(
        tmp_path,
        "version: 3\nsteps:\n  - {device: dut, action: reset}\n  - {device: dut, action: reset}\n  - {device: dut, action: reset}\n",
    )
    service = RecordingService()

    result = TestReactor(config, service, stop_requested=StopOnQuestion(2)).run(load_test_config(str(path), str(tmp_path)))  # type: ignore[arg-type]

    assert result["stopped"] is True
    assert result["stopped_after_step"] == 2
    assert [step["index"] for step in result["steps"]] == [1, 2]
    assert sum(1 for name, _ in service.calls if name == "reset_target") == 2


def test_a_stopped_run_is_not_a_passed_run(tmp_path: Path) -> None:
    # A stop is neither a pass nor a firmware failure, so it says which of the
    # two it is by its own name and keeps the records of what did run.
    config = load_config(str(write_config(tmp_path)))
    path = write_test_config(tmp_path, "version: 3\nsteps:\n  - {device: dut, action: reset}\n  - {device: dut, action: reset}\n")
    service = RecordingService()

    result = TestReactor(config, service, stop_requested=StopOnQuestion(1)).run(load_test_config(str(path), str(tmp_path)))  # type: ignore[arg-type]

    assert result["ok"] is False
    assert result["error_type"] == "run_stopped"
    assert result["stopped_after_step"] == 1
    assert "failed_step" not in result
    assert "step_error_type" not in result
    assert len(result["steps"]) == 1
    assert "stopped on request after step 1" in result["summary"]


def test_a_clean_stop_closes_the_devices_and_invokes_no_recovery(tmp_path: Path) -> None:
    # Devices closed and confirmed by the same cleanup a passing run uses means
    # there is nothing unconfirmed to recover, and driving the board again to
    # establish a state nothing disturbed would be the incident.
    config = load_config(
        str(write_config(tmp_path, com_ports_yaml='com_ports:\n  dut_uart:\n    device: "COM_TEST"\n'))
    )
    path = write_test_config(
        tmp_path,
        """version: 3
steps:
  - {device: dut_uart, action: uart_open}
  - {device: dut, action: debug_start, image_path: build/app.elf, mode: attach}
  - {device: dut, action: delay, duration_ms: 1}
""",
    )
    (tmp_path / "build").mkdir(exist_ok=True)
    (tmp_path / "build" / "app.elf").write_bytes(b"\x7fELF" + b"\x00" * 12)
    service = RecordingService()

    result = TestReactor(config, service, stop_requested=StopOnQuestion(2)).run(load_test_config(str(path), str(tmp_path)))  # type: ignore[arg-type]

    assert result["stopped"] is True
    # The order cleanup already had: a debug session closes before a serial line,
    # because a still-attached debugger can write to a line about to be closed.
    assert [item["action"] for item in result["cleanup"]] == ["debug_stop", "uart_close"]
    assert result["cleanup_ok"] is True
    assert "recovery" not in result
    assert service.recovery_calls == []


def test_a_stop_ends_a_delay_that_would_have_waited_ten_minutes(tmp_path: Path) -> None:
    # A step that waits ten minutes would make "between steps" mean ten minutes.
    # Slicing the wait is what makes the wait itself somewhere a stop is
    # answered, and the step says it was cut short rather than reporting the
    # number the plan asked for as what the board was given.
    config = load_config(str(write_config(tmp_path)))
    path = write_test_config(tmp_path, LONG_DELAY_PLAN)
    service = RecordingService()

    started = time.monotonic()
    result = TestReactor(config, service, stop_requested=StopOnQuestion(2)).run(load_test_config(str(path), str(tmp_path)))  # type: ignore[arg-type]
    elapsed_s = time.monotonic() - started

    assert elapsed_s < 30, elapsed_s
    step = result["steps"][0]["result"]
    assert step["ok"] is True
    assert step["stop_requested"] is True
    assert step["waited_ms"] < 600_000
    assert result["stopped"] is True
    # The step the stop cut short is the last step that ran, so it is where the
    # run says it stopped even though it was the plan's only step.
    assert result["stopped_after_step"] == 1


def test_a_delay_nobody_stops_waits_the_whole_duration(tmp_path: Path) -> None:
    # Slicing must not shorten a wait: the slices are counted against one
    # deadline rather than added up, so an uninterrupted delay takes exactly as
    # long as the plan asked for.
    config = load_config(str(write_config(tmp_path)))
    path = write_test_config(tmp_path, "version: 3\nsteps:\n  - {device: dut, action: delay, duration_ms: 600}\n")
    service = RecordingService()

    started = time.monotonic()
    result = TestReactor(config, service).run(load_test_config(str(path), str(tmp_path)))  # type: ignore[arg-type]
    elapsed_s = time.monotonic() - started

    assert result["ok"] is True, result
    assert elapsed_s >= 0.6, elapsed_s
    assert "stop_requested" not in result["steps"][0]["result"]


def test_a_stop_inside_a_repeat_block_ends_it_at_a_step_boundary(tmp_path: Path) -> None:
    # A block is not a step that has to finish, it is a list of steps that do,
    # so a stop ends it where it reached and the record says which iteration and
    # which step of the subset that was.
    config = load_config(str(write_config(tmp_path)))
    path = write_test_config(
        tmp_path,
        """version: 4
steps:
  - action: repeat
    count: 100
    steps:
      - {device: dut, action: reset}
      - {device: dut, action: reset}
""",
    )
    service = RecordingService()

    result = TestReactor(config, service, stop_requested=StopOnQuestion(4)).run(load_test_config(str(path), str(tmp_path)))  # type: ignore[arg-type]

    assert result["stopped"] is True
    assert result["stopped_after_step"] == 1
    block = result["steps"][0]
    assert block["result"]["exit_reason"] == "stopped"
    assert block["result"]["iterations_run"] == 2
    assert block["result"]["stopped_after_nested_step"] == 1
    assert [len(iteration["steps"]) for iteration in block["iterations"]] == [2, 1]
    assert sum(1 for name, _ in service.calls if name == "reset_target") == 3


# --- detached runs ---------------------------------------------------------


def test_a_detached_start_answers_with_a_handle_and_the_report_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, detached_runs) -> None:
    from agentic_hil.cli import start_detached_test_reactor

    workspace, plan = bench_workspace(tmp_path, monkeypatch, LONG_DELAY_PLAN)

    started = time.monotonic()
    result = start_detached_test_reactor(str(plan))
    elapsed_s = time.monotonic() - started

    detached_runs.append((load_authoritative_config(workspace), result["run"]))
    assert result["ok"] is True, result.get("worker_output") or result
    # The plan waits ten minutes; the start command does not.
    assert elapsed_s < 60, elapsed_s
    assert result["run"].startswith("run-")
    assert result["report_path"] == ".agentic-hil/reports/last-report.json"
    assert result["state"] == "running"
    assert result["detached"] is True


@pytest.mark.parametrize("bad_wait", [float("nan"), float("inf"), float("-inf")])
def test_a_detached_start_refuses_a_non_finite_wait_before_spawning_a_worker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad_wait: float) -> None:
    """A non-finite wait is refused up front, never handed to a detached worker.

    A NaN wait becomes a deadline `time.monotonic()` never reaches, so a worker
    holding for a device would poll it forever — past the window the start command
    waits in, leaving a live process behind a caller told the start failed.
    `start_detached_run` validates the wait before it spawns anything, so the
    value can never reach a process nobody is watching. The CLI's `type=float`
    accepts `nan`, so this path is externally reachable.
    """
    import agentic_hil.runlifecycle as runlifecycle_module
    from agentic_hil.runlifecycle import start_detached_run

    workspace, plan = bench_workspace(tmp_path, monkeypatch, LONG_DELAY_PLAN)
    config = load_authoritative_config(workspace)

    def fail_if_spawned(*args: object, **kwargs: object):
        raise AssertionError("a worker must not be spawned for a non-finite wait")

    monkeypatch.setattr(runlifecycle_module, "spawn_run_worker", fail_if_spawned)

    result = start_detached_run(config, str(plan), wait_s=bad_wait)

    assert result["ok"] is False, result
    assert result["error_type"] == "invalid_argument"
    assert result["side_effect_committed"] is False
    assert result["side_effect_status"] == "not_started"


def test_a_detached_start_on_a_held_bench_answers_with_the_run_that_was_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, detached_runs) -> None:
    # The start command waits for the worker to say what it is doing, not for it
    # to finish, and "what it is doing" includes having been refused the bench.
    # Answering with a handle to go and ask about would put a refusal the caller
    # could have had at once behind a second command. And because the run it
    # names already failed, the start's own verdict is that failure: `ok` false,
    # the refusal's `error_type`, and a CLI exit status that is not zero.
    from agentic_hil.bench import BenchMutex
    from agentic_hil.cli import result_succeeded, start_detached_test_reactor
    from agentic_hil.test_reactor import declared_devices, load_test_config

    workspace, plan = bench_workspace(tmp_path, monkeypatch, LONG_DELAY_PLAN)
    config = load_authoritative_config(workspace)
    stranger = BenchMutex(frontend="stranger", label="other-bench-session")
    stranger.acquire(declared_devices(config, load_test_config(str(plan), config.work_dir)))
    try:
        result = start_detached_test_reactor(str(plan))
    finally:
        stranger.release_all()

    detached_runs.append((config, result["run"]))
    assert result["state"] == "finished", result
    # The start does not dress a refused run up as a successful launch.
    assert result["ok"] is False, result
    assert result["run_ok"] is False, result
    assert result["error_type"] == "device_busy", result
    assert result_succeeded(result) is False, result
    report = json.loads((workspace / ".agentic-hil" / "reports" / "last-report.json").read_text(encoding="utf-8"))
    assert report["error_type"] == "device_busy"
    assert report["holder"]["label"] == "other-bench-session"
    assert report["steps"] == []
    assert report["run"] == result["run"]


def test_the_publish_window_grows_with_the_bounded_device_wait() -> None:
    """The startup deadline accounts for the wait the worker was handed.

    A worker told to wait for a held device stays in `starting` for the whole of
    that wait, so a deadline that counted only the fixed startup window would
    call it unresponsive while it does exactly what it was asked to — and then
    walk away from a live process that later acquires the devices and runs the
    plan. The wait is added on, bounded by the bench's own maximum, never
    unbounded, and a value the worker would refuse falls back to the base window
    rather than raising."""
    from agentic_hil.runlifecycle import MAX_WAIT_S, WORKER_PUBLISH_TIMEOUT_S, worker_publish_window_s

    assert worker_publish_window_s(0) == WORKER_PUBLISH_TIMEOUT_S
    assert worker_publish_window_s(120) == WORKER_PUBLISH_TIMEOUT_S + 120
    assert worker_publish_window_s(10_000) == WORKER_PUBLISH_TIMEOUT_S + MAX_WAIT_S
    assert worker_publish_window_s(-5) == WORKER_PUBLISH_TIMEOUT_S
    assert worker_publish_window_s(True) == WORKER_PUBLISH_TIMEOUT_S
    # A NaN the worker would refuse also falls back to the base window here rather
    # than producing a NaN deadline the start command would never reach.
    assert worker_publish_window_s(float("nan")) == WORKER_PUBLISH_TIMEOUT_S


def test_a_non_finite_device_wait_is_refused_by_the_shared_validator() -> None:
    """NaN and infinity are not waits: the one validator every acquisition shares
    refuses them.

    A NaN wait slips past a range check — every comparison against it is false —
    and would become a NaN deadline `time.monotonic()` never reaches, polling a
    held device forever. Refusing it in `validated_wait` closes that for every
    caller at once, the interactive acquisition and the detached worker alike,
    while a finite in-range wait still passes through unchanged.
    """
    from agentic_hil.bench import validated_wait
    from agentic_hil.config import ConfigError

    for bad in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ConfigError) as caught:
            validated_wait(bad)
        assert caught.value.error_type == "invalid_argument", bad
    assert validated_wait(12.5) == 12.5


def test_a_terminal_detached_record_is_answered_with_its_own_verdict() -> None:
    """A run that ended before the start caught it at `running` gets its verdict.

    Launch success is reserved for a running worker and for an already-finished
    run only when it passed. A finished-and-failed record — the held bench's
    `device_busy` is the case — is answered `ok: false` with the run's own
    `error_type` and failed-step fields, so the CLI exits non-zero rather than
    printing a handle to a run that is already over."""
    from agentic_hil.cli import result_succeeded
    from agentic_hil.runlifecycle import _detached_terminal_result

    handle = "run-" + "0" * 16
    passed = {"state": "finished", "run_ok": True, "name": "demo", "report_path": "r.json", "started_at": "t0", "finished_at": "t1"}
    ok = _detached_terminal_result(handle, passed, "fallback.json")
    assert ok["ok"] is True
    assert ok["run_ok"] is True
    assert "error_type" not in ok
    assert result_succeeded(ok) is True

    failed = {"state": "finished", "run_ok": False, "error_type": "device_busy", "failed_step": 0, "report_path": "r.json", "started_at": "t0", "finished_at": "t1"}
    bad = _detached_terminal_result(handle, failed, "fallback.json")
    assert bad["ok"] is False
    assert bad["run_ok"] is False
    assert bad["error_type"] == "device_busy"
    assert bad["failed_step"] == 0
    assert result_succeeded(bad) is False

    stopped = {"state": "stopped", "run_ok": False, "error_type": "run_stopped", "stopped_after_step": 2, "report_path": "r.json"}
    halted = _detached_terminal_result(handle, stopped, "fallback.json")
    assert halted["ok"] is False
    assert halted["state"] == "stopped"
    assert halted["stopped_after_step"] == 2
    assert "was stopped on request" in halted["summary"]


def test_a_detached_run_holds_its_devices_and_refuses_another_caller_by_name(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, detached_runs) -> None:
    # The concurrency story is the one the bench already had: locks are
    # machine-wide, a run holds what it declared from before its first step to
    # after its last, and a run being somewhere else changes nothing about that.
    from agentic_hil.cli import run_test_reactor, start_detached_test_reactor

    workspace, plan = bench_workspace(tmp_path, monkeypatch, LONG_DELAY_PLAN)
    second = plan.parent / "second.yaml"
    second.write_text("version: 3\nsteps:\n  - {device: dut, action: reset}\n", encoding="utf-8")
    config = load_authoritative_config(workspace)
    result = start_detached_test_reactor(str(plan))
    detached_runs.append((config, result["run"]))

    refused = run_test_reactor(str(second))

    assert refused["ok"] is False
    assert refused["error_type"] == "device_busy"
    assert refused["holder"]["frontend"] == "reactor"
    assert refused["holder"]["label"] == "testconfig"
    assert refused["steps"] == []


def test_a_detached_run_goes_from_running_to_finished(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, detached_runs) -> None:
    from agentic_hil.cli import start_detached_test_reactor
    from agentic_hil.runlifecycle import run_status

    workspace, plan = bench_workspace(tmp_path, monkeypatch, "version: 3\nsteps:\n  - {device: dut, action: delay, duration_ms: 1200}\n")

    result = start_detached_test_reactor(str(plan))
    config = load_authoritative_config(workspace)
    detached_runs.append((config, result["run"]))

    status = run_status(config, result["run"])
    assert status["state"] == "running", status
    finished = wait_for_state(config, result["run"], {"finished", "stopped", "worker_gone"})
    assert finished["state"] == "finished", worker_log(config, result["run"])
    assert finished["run_ok"] is True
    report = json.loads((workspace / ".agentic-hil" / "reports" / "last-report.json").read_text(encoding="utf-8"))
    assert report["ok"] is True
    assert report["run"] == result["run"]


def test_a_detached_run_goes_from_running_to_stopped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, detached_runs) -> None:
    from agentic_hil.cli import start_detached_test_reactor
    from agentic_hil.runlifecycle import request_run_stop, run_status

    workspace, plan = bench_workspace(tmp_path, monkeypatch, LONG_DELAY_PLAN)

    result = start_detached_test_reactor(str(plan))
    config = load_authoritative_config(workspace)
    detached_runs.append((config, result["run"]))
    status = run_status(config, result["run"])
    assert status["state"] == "running", (status, result)

    requested = request_run_stop(config, result["run"])

    assert requested["ok"] is True
    assert requested["stop_requested"] is True
    stopped = wait_for_state(config, result["run"], {"finished", "stopped", "worker_gone"})
    assert stopped["state"] == "stopped", stopped
    assert stopped["run_ok"] is False
    assert stopped["error_type"] == "run_stopped"
    report = json.loads((workspace / ".agentic-hil" / "reports" / "last-report.json").read_text(encoding="utf-8"))
    assert report["error_type"] == "run_stopped"
    assert report["stopped"] is True
    # An orderly end leaves no incident, so nothing drives the board afterwards.
    assert "recovery" not in report


def test_a_killed_worker_is_reported_gone_rather_than_guessed_at(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, detached_runs) -> None:
    # Whether the process behind a handle is still there is asked of the
    # operating system, not of its pid: the run holds a lifetime lock for as
    # long as it runs, so a lock that can be taken is a run whose process is
    # gone, however it went. A stop cannot be honoured by a process that is not
    # there, and saying so is the point: the way out is the recovery route.
    from agentic_hil.cli import start_detached_test_reactor
    from agentic_hil.runlifecycle import read_run_record, request_run_stop

    workspace, plan = bench_workspace(tmp_path, monkeypatch, KILLED_WORKER_PLAN)
    result = start_detached_test_reactor(str(plan))
    config = load_authoritative_config(workspace)
    detached_runs.append((config, result["run"]))
    record = read_run_record(config, result["run"])
    assert record is not None and isinstance(record["pid"], int)

    kill_hard(record["pid"])
    gone = wait_for_state(config, result["run"], {"worker_gone", "finished", "stopped"})

    assert gone["state"] == "worker_gone", gone
    assert "lease-status" in gone["next_step"]
    refused = request_run_stop(config, result["run"])
    assert refused["ok"] is False
    assert refused["error_type"] == "run_worker_gone"
    assert "lease-status" in refused["next_step"]


def test_a_killed_worker_leaves_the_bench_to_the_machinery_that_already_owns_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, detached_runs) -> None:
    # Nothing new holds a detached run's devices, so nothing new has to give
    # them back. The killed worker leaves the hold record naming a process that
    # is no longer there, exactly as any hard-killed owner does, and the mutex's
    # own ownership rule is what lets the next run take the board: the lock died
    # with the process. The coordinator reads the durable record and finds
    # nothing unconfirmed to quarantine, because this run had released the lease
    # its one hardware step took.
    from agentic_hil.bench import BenchMutex
    from agentic_hil.cli import run_test_reactor, start_detached_test_reactor
    from agentic_hil.coordination import HardwareCoordinator
    from agentic_hil.runlifecycle import read_run_record
    from agentic_hil.test_reactor import declared_devices, load_test_config

    workspace, plan = bench_workspace(tmp_path, monkeypatch, KILLED_WORKER_PLAN)
    second = plan.parent / "second.yaml"
    second.write_text("version: 3\nsteps:\n  - {device: dut, action: reset}\n", encoding="utf-8")
    config = load_authoritative_config(workspace)
    devices = declared_devices(config, load_test_config(str(plan), config.work_dir))
    result = start_detached_test_reactor(str(plan))
    detached_runs.append((config, result["run"]))
    record = read_run_record(config, result["run"])
    # The reset step has to have run, so the worker is killed having driven the
    # board rather than having only declared it.
    wait_for_progress_step(workspace, config, result["run"], 2)

    kill_hard(int(record["pid"]))
    wait_for_state(config, result["run"], {"worker_gone"})

    holds = [BenchMutex(frontend="probe").holder(device) for device in devices]
    assert [hold["state"] for hold in holds] == ["held"] * len(devices)
    assert {hold["owner"]["pid"] for hold in holds} == {record["pid"]}
    status = HardwareCoordinator(config, "operator-cli").status()
    assert status["blocked"] is False
    assert status["quarantine_id"] is None
    assert status["record"]["owner_pid"] == record["pid"]
    # And the bench is usable again without anything new being invented.
    recovered = run_test_reactor(str(second))
    assert recovered["ok"] is True, recovered


def test_a_run_handle_that_is_not_the_shape_the_start_command_prints_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A handle becomes a file name, so a caller-supplied string that is not the
    # minted shape is refused rather than sanitized into a neighbouring path.
    from agentic_hil.config import ConfigError
    from agentic_hil.runlifecycle import run_status

    workspace, _ = bench_workspace(tmp_path, monkeypatch, LONG_DELAY_PLAN)
    config = load_authoritative_config(workspace)

    for handle in ("../../etc/passwd", "run-notahandle", "run-0123456789abcdefg", ""):
        with pytest.raises(ConfigError) as refused:
            run_status(config, handle)
        assert refused.value.error_type == "invalid_argument"


def test_a_handle_this_bench_never_issued_is_not_found(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from agentic_hil.runlifecycle import request_run_stop, run_status

    workspace, _ = bench_workspace(tmp_path, monkeypatch, LONG_DELAY_PLAN)
    config = load_authoritative_config(workspace)

    assert run_status(config, "run-00000000000000ff")["error_type"] == "run_not_found"
    assert request_run_stop(config, "run-00000000000000ff")["error_type"] == "run_not_found"


def test_a_synchronous_run_registers_a_handle_it_can_be_stopped_by(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The synchronous invocation is unchanged in what it does, and registered
    # all the same: a plan running in one terminal can be asked to stop from
    # another instead of being killed, which is the dead-owner route.
    from agentic_hil.cli import run_test_reactor
    from agentic_hil.runlifecycle import known_runs, run_status

    workspace, plan = bench_workspace(tmp_path, monkeypatch, "version: 3\nsteps:\n  - {device: dut, action: delay, duration_ms: 50}\n")
    config = load_authoritative_config(workspace)

    result = run_test_reactor(str(plan))

    assert result["ok"] is True, result
    listed = known_runs(config)
    assert [item["state"] for item in listed["runs"]] == ["finished"]
    assert listed["runs"][0]["run"] == result["run"]
    assert listed["active_runs"] == []
    # The record is the run's own verdict, not merely a note that a handle
    # existed: a second terminal asks this and has to get the same answer the
    # terminal that ran it got.
    status = run_status(config, result["run"])
    assert status["run_ok"] is True
    assert status["error_type"] is None
    assert status["report_path"] == ".agentic-hil/reports/last-report.json"


def test_a_record_read_that_meets_the_writers_replace_waits_it_out(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The writer retries while a reader holds the record open; this is the
    # reader's half of the same contention. A status poll landing in the instant
    # of the writer's os.replace gets a sharing violation, and that is a moment,
    # not a state: the poll must answer the state, not report it unreadable.
    import agentic_hil.runlifecycle as runlifecycle

    workspace, _ = bench_workspace(tmp_path, monkeypatch, LONG_DELAY_PLAN)
    config = load_authoritative_config(workspace)
    record = json.dumps({"version": runlifecycle.RUN_RECORD_VERSION, "state": "running"})
    answers = iter([PermissionError("held by os.replace"), OSError("held by os.replace"), record])

    def contended_read(path):
        answer = next(answers)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    monkeypatch.setattr(runlifecycle, "safe_read_text", contended_read)
    monkeypatch.setattr(runlifecycle, "_RECORD_READ_RETRY_DELAY_S", 0.0)

    value = runlifecycle.read_run_record(config, "run-0123456789abcdef")

    assert value == {"version": runlifecycle.RUN_RECORD_VERSION, "state": "running"}


def test_a_record_read_that_never_gets_an_answer_is_bounded_and_honest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The retry is patience, not denial: a record that stays unreadable is still
    # unreadable, after a bounded number of looks, and the answer still names
    # the backend error.
    import agentic_hil.runlifecycle as runlifecycle
    from agentic_hil.config import ConfigError

    workspace, _ = bench_workspace(tmp_path, monkeypatch, LONG_DELAY_PLAN)
    config = load_authoritative_config(workspace)
    asked = []

    def refused_read(path):
        asked.append(path)
        raise PermissionError("held forever")

    monkeypatch.setattr(runlifecycle, "safe_read_text", refused_read)
    monkeypatch.setattr(runlifecycle, "_RECORD_READ_RETRY_DELAY_S", 0.0)

    with pytest.raises(ConfigError) as caught:
        runlifecycle.read_run_record(config, "run-0123456789abcdef")

    assert caught.value.error_type == "run_state_invalid"
    assert "held forever" in str(caught.value.details.get("backend_error"))
    assert len(asked) == runlifecycle._RECORD_READ_ATTEMPTS


# --- the record's publication, seen from a reader that arrives inside it ------
#
# A record is published by writing a whole new file beside it and renaming that
# over it, so a reader can arrive in the middle of the rename. What it meets
# there depends on the platform and never on the run: Windows refuses the rename
# while the reader holds the file, and POSIX lets the rename through and leaves
# the reader holding an inode whose last link the rename removed, or looking at a
# name that now leads to a different inode. Both POSIX faces come back out of
# `safe_read_text` as the refusals below, which is how #312 read on a slow macOS
# runner: a status poll answered "Test run state could not be read" for a run
# that was perfectly fine and about to say so.
#
# The race is placed rather than raced. Timing it would mean a test that fails
# once in a thousand runs on somebody else's machine, which is the shape of the
# bug and no way to hold a fix to it, so the writer's transition is handed to the
# reader as the script it would have seen.


def single_link_refusal(path: Path) -> ConfigError:
    """What POSIX answers a read of an inode the rename has just unlinked.

    The reader opened the old record, the writer's rename replaced the name, and
    the file rule now sees a regular file with no links at all."""
    return ConfigError("unsafe_configured_path", "Configured file must be a single-link regular file.", {"path": str(path)})


def swapped_inode_refusal(path: Path) -> ConfigError:
    """What POSIX answers when the rename lands between the open and the check
    that the name still leads to the file that was opened."""
    return ConfigError("unsafe_configured_path", "Configured file changed while it was being opened.", {"path": str(path)})


class ScriptedReads:
    """`safe_read_text` with a written script, so the transition is placed exactly.

    Answers the record with each scripted answer in turn and holds on the last
    one once the script runs out; an exception in the script is raised instead of
    returned. The stop file is answered as a file that is not there, because a
    status read asks for both and only the record is what these tests are about."""

    def __init__(self, *answers: object) -> None:
        self.answers = list(answers)
        self.record_reads = 0

    def __call__(self, path) -> str:
        if str(path).endswith(".stop"):
            raise FileNotFoundError(str(path))
        self.record_reads += 1
        answer = self.answers[min(self.record_reads, len(self.answers)) - 1]
        if isinstance(answer, BaseException):
            raise answer
        return str(answer)


def torn_record_bench(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *answers: object) -> tuple[object, str, ScriptedReads]:
    """A bench whose record reads follow a script, with the retry delay removed."""
    import agentic_hil.runlifecycle as runlifecycle

    workspace, _ = bench_workspace(tmp_path, monkeypatch, LONG_DELAY_PLAN)
    config = load_authoritative_config(workspace)
    reads = ScriptedReads(*answers)
    monkeypatch.setattr(runlifecycle, "safe_read_text", reads)
    monkeypatch.setattr(runlifecycle, "_RECORD_READ_RETRY_DELAY_S", 0.0)
    return config, "run-0123456789abcdef", reads


def whole_record(state: str = "running", **fields: object) -> str:
    import agentic_hil.runlifecycle as runlifecycle

    return json.dumps({"version": runlifecycle.RUN_RECORD_VERSION, "state": state, **fields})


def test_a_record_read_that_meets_the_rename_on_posix_waits_it_out(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The failure in #312 exactly: the file rule refused the record because the
    # writer's rename had taken the reader's inode out of the directory. Nothing
    # about the run had gone wrong, and the second face of the same instant is
    # the same non-answer, so both are waited out.
    import agentic_hil.runlifecycle as runlifecycle

    config, handle, reads = torn_record_bench(
        tmp_path,
        monkeypatch,
        single_link_refusal(Path("record.json")),
        swapped_inode_refusal(Path("record.json")),
        whole_record("running"),
    )

    value = runlifecycle.read_run_record(config, handle)

    assert value == {"version": runlifecycle.RUN_RECORD_VERSION, "state": "running"}
    assert reads.record_reads == 3


def test_a_record_read_that_meets_a_half_written_file_waits_for_the_whole_one(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # An empty file and a truncated one are the faces a writer that did not
    # rename would show. This writer does rename, so a reader never meets them
    # here; a reader that treated them as corruption would still be wrong, and
    # would be wrong on the day somebody publishes this file another way.
    import agentic_hil.runlifecycle as runlifecycle

    config, handle, reads = torn_record_bench(
        tmp_path,
        monkeypatch,
        "",
        '{"version": 1, "state": "run',
        whole_record("running", run_ok=None),
    )

    value = runlifecycle.read_run_record(config, handle)

    assert value["state"] == "running"
    assert reads.record_reads == 3


def test_a_record_that_vanishes_after_it_was_seen_waits_for_the_name_to_come_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The window an unlink-first writer would open: the name was there a moment
    # ago and is not there now. That is a transition, not an answer, and it is
    # told apart from "no such run" by the look that already found something.
    import agentic_hil.runlifecycle as runlifecycle

    config, handle, reads = torn_record_bench(
        tmp_path,
        monkeypatch,
        single_link_refusal(Path("record.json")),
        FileNotFoundError("the name is between two steps"),
        whole_record("finished", run_ok=True),
    )

    value = runlifecycle.read_run_record(config, handle)

    assert value["state"] == "finished"
    assert value["run_ok"] is True
    assert reads.record_reads == 3


def test_a_handle_with_no_record_at_all_is_answered_on_the_first_look(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The patience must not be paid by the answer that is not a transition. A
    # handle this bench never issued has no record and never will, and the start
    # command polls this same read while a worker is coming up, so a missing file
    # nothing has yet seen is answered at once rather than waited on six times.
    import agentic_hil.runlifecycle as runlifecycle

    config, handle, reads = torn_record_bench(tmp_path, monkeypatch, FileNotFoundError("never issued"))

    assert runlifecycle.read_run_record(config, handle) is None
    assert reads.record_reads == 1


def test_a_record_that_stays_torn_is_refused_with_the_reason_the_last_look_gave(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Patience, not denial. A file that never parses is not a transition however
    # long it is watched, and the refusal still hands over the parser's own
    # complaint rather than a tidier sentence with the cause taken out of it.
    import agentic_hil.runlifecycle as runlifecycle

    config, handle, reads = torn_record_bench(tmp_path, monkeypatch, '{"version": 1, "state": "run')

    with pytest.raises(ConfigError) as caught:
        runlifecycle.read_run_record(config, handle)

    assert caught.value.error_type == "run_state_invalid"
    assert caught.value.summary == "Test run state is not valid JSON."
    assert "Unterminated string" in str(caught.value.details.get("backend_error"))
    assert reads.record_reads == runlifecycle._RECORD_READ_ATTEMPTS


def test_a_record_the_file_rule_keeps_refusing_still_names_the_file_rule(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A path that is genuinely unsafe answers the same refusal it always did,
    # one bounded wait later, and the decisive cause travels with it in the
    # backend error and on the exception chain. Waiting out a moment must never
    # turn into swallowing a reason.
    import agentic_hil.runlifecycle as runlifecycle

    config, handle, reads = torn_record_bench(tmp_path, monkeypatch, single_link_refusal(Path("record.json")))

    with pytest.raises(ConfigError) as caught:
        runlifecycle.read_run_record(config, handle)

    assert caught.value.error_type == "run_state_invalid"
    assert caught.value.summary == "Test run state could not be read."
    assert "single-link regular file" in str(caught.value.details.get("backend_error"))
    assert isinstance(caught.value.__cause__, ConfigError)
    assert caught.value.__cause__.error_type == "unsafe_configured_path"
    assert reads.record_reads == runlifecycle._RECORD_READ_ATTEMPTS


def test_a_record_from_a_version_this_code_cannot_read_is_not_waited_on(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A whole document that names another version is a state, not a moment: it
    # will say the same thing on the sixth look, so it is refused on the first.
    import agentic_hil.runlifecycle as runlifecycle

    config, handle, reads = torn_record_bench(tmp_path, monkeypatch, json.dumps({"version": 999, "state": "running"}))

    with pytest.raises(ConfigError) as caught:
        runlifecycle.read_run_record(config, handle)

    assert caught.value.summary == "Test run state is not a record this version can read."
    assert reads.record_reads == 1


def test_a_status_poll_that_meets_the_rename_answers_the_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The reader #312 was actually reported by: a test waiting on a detached run
    # asks `run_status` in a loop, and one poll landing inside the writer's
    # rename raised out of the loop and failed the run's own test. It answers the
    # record now, which is what it was asking for.
    import agentic_hil.runlifecycle as runlifecycle

    config, handle, _ = torn_record_bench(
        tmp_path,
        monkeypatch,
        single_link_refusal(Path("record.json")),
        whole_record("stopped", run_ok=False, error_type="run_stopped", report_path="report.json"),
    )

    status = runlifecycle.run_status(config, handle)

    assert status["ok"] is True
    assert status["state"] == "stopped"
    assert status["error_type"] == "run_stopped"


def test_a_stop_request_that_cannot_be_read_is_still_a_stop_request(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The stop file has one state and the file being there is it, so the reader
    # that meets its replacement needs no patience: every way of failing to read
    # it lands on the answer its existence already gave, and waiting would only
    # make a stop slower.
    import agentic_hil.runlifecycle as runlifecycle

    workspace, _ = bench_workspace(tmp_path, monkeypatch, LONG_DELAY_PLAN)
    config = load_authoritative_config(workspace)
    handle = "run-0123456789abcdef"

    def refused_read(path):
        raise single_link_refusal(path)

    monkeypatch.setattr(runlifecycle, "safe_read_text", refused_read)

    assert runlifecycle.stop_requested_at(config, handle) is not None


def test_the_record_writer_publishes_only_by_rename_and_never_unlinks_first(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The reader's patience is bounded because the writer's transition is one
    # rename, in the record's own directory, over a name that always leads to a
    # whole document. A writer that unlinked first would open a window of
    # unbounded length instead, and no retry count is the right one for that.
    # Observed from inside the rename: at the instant the next record is about to
    # be published the name still holds the whole of the previous one.
    import agentic_hil.runlifecycle as runlifecycle

    workspace, _ = bench_workspace(tmp_path, monkeypatch, LONG_DELAY_PLAN)
    config = load_authoritative_config(workspace)
    handle = "run-0123456789abcdef"
    runlifecycle.runs_directory(config).mkdir(parents=True, exist_ok=True)
    path = runlifecycle.record_path(config, handle)
    real_replace = os.replace
    at_the_rename: list[bytes | None] = []

    def observed_replace(source, destination, **directory_fds):
        at_the_rename.append(path.read_bytes() if path.exists() else None)
        return real_replace(source, destination, **directory_fds)

    monkeypatch.setattr(os, "replace", observed_replace)
    starting = {"version": runlifecycle.RUN_RECORD_VERSION, "state": "starting"}
    running = {"version": runlifecycle.RUN_RECORD_VERSION, "state": "running"}
    runlifecycle.write_run_record(config, handle, starting)
    published = path.read_bytes()
    runlifecycle.write_run_record(config, handle, running)

    assert len(at_the_rename) == 2, at_the_rename
    # Nothing existed before the first publication, and the first publication was
    # still whole and still the published bytes when the second one landed.
    assert at_the_rename[0] is None
    assert at_the_rename[1] == published
    assert json.loads(published.decode("utf-8")) == starting
    assert json.loads(path.read_text(encoding="utf-8")) == running


def test_a_worker_that_finished_in_the_instant_of_the_probe_is_not_called_gone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The worker publishes its terminal record BEFORE it releases its lifetime
    # lock, so a status poll can take the lock in the instant between the two
    # and still owes the caller the record's own ending, not a guess. The gone
    # probe here finishes the worker as its side effect: exactly the boundary
    # the fourth CI sighting of this race landed on.
    import agentic_hil.runlifecycle as runlifecycle

    workspace, _ = bench_workspace(tmp_path, monkeypatch, LONG_DELAY_PLAN)
    config = load_authoritative_config(workspace)
    handle = "run-0123456789abcdef"
    runlifecycle.runs_directory(config).mkdir(parents=True, exist_ok=True)
    runlifecycle.write_run_record(config, handle, {"version": runlifecycle.RUN_RECORD_VERSION, "state": "running", "run_ok": None})

    def gone_and_just_finished(inner_config, inner_handle):
        runlifecycle.write_run_record(inner_config, inner_handle, {"version": runlifecycle.RUN_RECORD_VERSION, "state": "finished", "run_ok": True})
        return True

    monkeypatch.setattr(runlifecycle, "worker_is_gone", gone_and_just_finished)

    status = runlifecycle.run_status(config, handle)

    assert status["state"] == "finished", status
    assert status["run_ok"] is True


def test_a_takeable_lock_over_a_running_record_stays_the_dead_worker_answer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The re-read must not soften the honest case: a lock anyone can take while
    # the record still says running is a worker that died with no orderly end.
    import agentic_hil.runlifecycle as runlifecycle

    workspace, _ = bench_workspace(tmp_path, monkeypatch, LONG_DELAY_PLAN)
    config = load_authoritative_config(workspace)
    handle = "run-0123456789abcdef"
    runlifecycle.runs_directory(config).mkdir(parents=True, exist_ok=True)
    runlifecycle.write_run_record(config, handle, {"version": runlifecycle.RUN_RECORD_VERSION, "state": "running", "run_ok": None})
    monkeypatch.setattr(runlifecycle, "worker_is_gone", lambda *_: True)

    status = runlifecycle.run_status(config, handle)

    assert status["state"] == runlifecycle.RUN_WORKER_GONE, status


# --- the start command, waiting on a record it may never be able to read -----
#
# The publish-wait loop has one question to answer while a worker comes up: has
# it said what it is doing yet? "Not yet" is a reason to wait. "It said
# something and that something will not read" is not, because the reader has
# already waited every transient face of a publication out before it raises, and
# what it raises is a state. The loop used to answer both with `record = None`,
# spend the whole window on the second one and report `run_worker_unresponsive`,
# which is a claim about a process from a fact about a file.
#
# The same scripted reads place it. A worker is never spawned: what is under test
# is what the loop does with the reader's answer, and a real worker would supply
# a readable record and nothing else.


class DeadStillWorker:
    """A spawned worker that has not exited, for a loop that never gets that far.

    `start_detached_run` asks `poll()` once per turn to tell a worker that died
    from one that is still coming up. These tests end on the first turn, so the
    only thing it has to be is alive."""

    def poll(self) -> None:
        return None


def start_bench(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *answers: object):
    """`torn_record_bench` with the worker spawn taken out.

    The record script is the whole of the placement; the process behind the
    handle is not what these tests are about and is not started, so a scripted
    read is never racing a real one."""
    import agentic_hil.runlifecycle as runlifecycle

    config, _, reads = torn_record_bench(tmp_path, monkeypatch, *answers)
    monkeypatch.setattr(runlifecycle, "spawn_run_worker", lambda *_, **__: DeadStillWorker())
    return config, reads


def test_a_record_that_will_not_read_stops_the_start_waiting_and_says_why(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The whole of #323. A record confirmed unreadable on six looks is a state,
    # so there is nothing left to wait for: the start refuses at once, names the
    # record it could not read, and carries the reason that decided it rather
    # than burning the window and reporting a verdict on the worker's liveness.
    import agentic_hil.runlifecycle as runlifecycle

    config, reads = start_bench(tmp_path, monkeypatch, '{"version": 1, "state": "run')

    with pytest.raises(ConfigError) as caught:
        runlifecycle.start_detached_run(config, ".agentic-hil/testconfig.yaml", wait_s=0.0)

    refusal = caught.value.to_dict()
    assert refusal["ok"] is False
    assert refusal["error_type"] == "run_state_invalid"
    assert refusal["summary"] == "The detached run published a record this bench cannot read, so the start command cannot say what the run is doing."
    # It names the unreadable record, both by the handle the start minted and by
    # the file an operator has to go and look at.
    handle = refusal["run"]
    assert handle.startswith("run-")
    assert refusal["record_path"].endswith(f"{handle}.json")
    # And it carries the read's own account: what the read decided, and the
    # decisive complaint underneath it.
    assert refusal["record_error"] == "Test run state is not valid JSON."
    assert "Unterminated string" in str(refusal["backend_error"])
    assert isinstance(caught.value.__cause__, ConfigError)
    assert caught.value.__cause__.error_type == "run_state_invalid"
    # The proof that the window was not burned: exactly one read of the record,
    # which is the reader's own bounded looks and not one turn of the loop more.
    assert reads.record_reads == runlifecycle._RECORD_READ_ATTEMPTS
    # And the proof it is not the unresponsive path wearing a new message: that
    # path plants a stop under the handle, and this one deliberately does not,
    # because a record this reader cannot parse says nothing about whether the
    # plan behind it is running perfectly well.
    assert not runlifecycle.stop_path(config, handle).exists()


def test_a_record_that_is_not_there_yet_still_leaves_the_start_waiting(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The other way, and the half that must not regress: a handle with no record
    # under it is a worker that has not published yet, which is exactly what the
    # window exists for. The worker publishes on the next look and the start
    # answers with the running launch it always did.
    import agentic_hil.runlifecycle as runlifecycle

    config, reads = start_bench(
        tmp_path,
        monkeypatch,
        FileNotFoundError("the worker has not published yet"),
        whole_record("running", name="demo", started_at="t0"),
    )

    result = runlifecycle.start_detached_run(config, ".agentic-hil/testconfig.yaml", wait_s=0.0)

    assert result["ok"] is True, result
    assert result["state"] == "running"
    assert result["detached"] is True
    assert result["run"].startswith("run-")
    # One look that found nothing, then the one that found the record: the
    # absence was waited on and the publication ended the wait.
    assert reads.record_reads == 2


def test_a_record_from_another_version_refuses_the_start_with_its_own_reason(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The read refusal that carries no backend error of its own: a whole document
    # naming a version this code cannot read. The reason is the read's verdict
    # itself, so the refusal has to carry that rather than leaving the details
    # with nothing in them that says what went wrong.
    import agentic_hil.runlifecycle as runlifecycle

    config, reads = start_bench(tmp_path, monkeypatch, json.dumps({"version": 999, "state": "running"}))

    with pytest.raises(ConfigError) as caught:
        runlifecycle.start_detached_run(config, ".agentic-hil/testconfig.yaml", wait_s=0.0)

    assert caught.value.error_type == "run_state_invalid"
    assert caught.value.details["record_error"] == "Test run state is not a record this version can read."
    assert "backend_error" not in caught.value.details
    assert isinstance(caught.value.__cause__, ConfigError)
    # Another version's record is a state on the first look, so the start spends
    # no more looks on it than the reader does.
    assert reads.record_reads == 1
