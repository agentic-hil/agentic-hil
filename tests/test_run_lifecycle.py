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

from agentic_hil.config import load_authoritative_config, load_config
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
