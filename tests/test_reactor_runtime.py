"""The reactor runtime where it fails: dead and unresponsive workers, a stop asked
of a run that has no step yet, records nobody prunes, and the refusals a plan
gets before it touches anything (#505).

Every test here is written from the issue text, before any change to the code,
so a test that is red today is red for the reason the issue names. The detached
paths are driven through a real worker process the way the existing detached
tests are: a worker that dies is a real interpreter dying, and a worker that
comes alive late is a real interpreter running the real plan late, so the poll
and record race the start command decides on is the real one.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from conftest import DEFAULT_TEST_PERMISSIONS, write_config
from test_run_lifecycle import (
    LONG_DELAY_PLAN,
    bench_workspace,
    detached_runs,  # noqa: F401
    wait_for_state,
    worker_log,
)
from test_test_reactor import RecordingService, write_test_config

import agentic_hil.runlifecycle as runlifecycle
from agentic_hil.config import load_authoritative_config, load_config
from agentic_hil.test_reactor import TestReactor, declared_devices, load_test_config

SHORT_DELAY_PLAN = "version: 4\nsteps:\n  - {device: dut, action: delay, duration_ms: 20}\n"


# --- a worker that dies before it publishes ----------------------------------


def a_child_that_cannot_import_the_package(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Arrange the next child interpreter so `-m agentic_hil` finds no package.

    What a venv without the package, a user-site shadow or a broken install look
    like to the worker: the same interpreter, the same command line, and the
    import fails. PYTHONPATH names a directory whose `sitecustomize` takes every
    route to the package off the child's import path (a source tree on the path,
    an editable install's finder, a site-packages copy) before `runpy` looks for
    it, so the child prints runpy's own refusal and exits the way it does on a
    machine where the package is really not there. The parent process is not
    touched: only the environment the worker is spawned with."""
    shadow = tmp_path / "no-package"
    shadow.mkdir()
    (shadow / "sitecustomize.py").write_text(
        "import os\n"
        "import sys\n"
        "sys.path[:] = [entry for entry in sys.path if not os.path.isdir(os.path.join(entry or os.curdir, 'agentic_hil'))]\n"
        "sys.meta_path[:] = [finder for finder in sys.meta_path if 'editable' not in (type(finder).__module__ + type(finder).__name__).lower()]\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("PYTHONPATH", str(shadow))
    return shadow


def test_a_worker_that_dies_before_publishing_is_reported_with_its_exit_code_and_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`run_worker_failed`: the interpreter cannot run `-m agentic_hil`.

    The start command waits `WORKER_EXIT_GRACE_S` after the exit for a record
    that never comes, then answers with the exit code and the tail of what the
    worker printed, which with no record and no report is the only account of
    why. Nothing is planted under the handle: a worker that has exited cannot
    come alive later, so there is nobody for a stop to reach."""
    workspace, plan = bench_workspace(tmp_path, monkeypatch, LONG_DELAY_PLAN)
    config = load_authoritative_config(workspace)
    a_child_that_cannot_import_the_package(tmp_path, monkeypatch)

    started = time.monotonic()
    result = runlifecycle.start_detached_run(config, str(plan), wait_s=0.0)
    elapsed_s = time.monotonic() - started

    assert result["ok"] is False, result
    assert result["error_type"] == "run_worker_failed", result
    assert result["exit_code"] == 1, result
    assert "No module named agentic_hil" in result["worker_output"], result["worker_output"]
    assert result["retry_safe"] is True, result
    assert result["side_effect_committed"] is False, result
    assert result["run"].startswith("run-")
    assert not runlifecycle.stop_path(config, result["run"]).exists()
    # Answered off the exit and the grace, not off the whole publication window.
    assert elapsed_s < runlifecycle.WORKER_PUBLISH_TIMEOUT_S, elapsed_s


def test_a_worker_that_dies_before_its_record_is_reported_with_its_log_tail(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A genuine dying process, not a stub: the poll()/record race is the real one.

    The worker is a child interpreter that writes one line to the log the
    spawner opened for it and exits 3. The start command reports that exit and
    that line, and leaves no stop under the handle."""
    workspace, plan = bench_workspace(tmp_path, monkeypatch, LONG_DELAY_PLAN)
    config = load_authoritative_config(workspace)

    def dying_worker(inner_config, handle: str, test_config_path: str, *, wait_s: float) -> subprocess.Popen:
        with open(runlifecycle.runs_directory(inner_config) / f"{handle}.log", "ab") as log:
            return subprocess.Popen(
                [sys.executable, "-c", "import sys; print('boom', file=sys.stderr); sys.exit(3)"],
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                cwd=str(inner_config.work_dir),
            )

    monkeypatch.setattr(runlifecycle, "spawn_run_worker", dying_worker)

    result = runlifecycle.start_detached_run(config, str(plan), wait_s=0.0)

    assert result["ok"] is False, result
    assert result["error_type"] == "run_worker_failed", result
    assert result["exit_code"] == 3, result
    assert "boom" in result["worker_output"], result
    assert result["retry_safe"] is True, result
    assert not runlifecycle.stop_path(config, result["run"]).exists()
    # The handle names a run this bench never heard from, and says so.
    assert runlifecycle.run_status(config, result["run"])["error_type"] == "run_not_found"


# --- a worker that publishes nothing inside the window -----------------------


class WorkerThatNeverPublishes:
    """A spawned worker that stays alive and never writes its record."""

    def poll(self) -> None:
        return None


def test_a_worker_that_never_publishes_gets_a_stop_planted_under_its_handle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`run_worker_unresponsive`, and what is left behind for a worker that wakes.

    The refusal is not retry-safe, because a live process may still be on its
    way to the board. A stop is planted under the handle so that process ends
    at its first step boundary, and until it publishes the handle is one this
    bench has no record of."""
    workspace, plan = bench_workspace(tmp_path, monkeypatch, LONG_DELAY_PLAN)
    config = load_authoritative_config(workspace)
    monkeypatch.setattr(runlifecycle, "WORKER_PUBLISH_TIMEOUT_S", 0.3)
    monkeypatch.setattr(runlifecycle, "spawn_run_worker", lambda *_, **__: WorkerThatNeverPublishes())

    result = runlifecycle.start_detached_run(config, str(plan), wait_s=0.0)

    assert result["ok"] is False, result
    assert result["error_type"] == "run_worker_unresponsive", result
    assert result["retry_safe"] is False, result
    assert result["side_effect_committed"] is False, result
    handle = result["run"]
    assert runlifecycle.stop_path(config, handle).exists()
    assert runlifecycle.run_status(config, handle)["error_type"] == "run_not_found"
    # A registration taken later under that handle, which is what the worker
    # does when it comes alive, is answered yes before its first step.
    registration = runlifecycle.RunRegistration.take(config, handle, name="late", test_config_path=str(plan), detached=True)
    with registration:
        assert registration.stop_requested() is True


def test_an_unresponsive_worker_gets_a_planted_stop_that_ends_it_when_it_wakes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, detached_runs) -> None:  # noqa: F811
    """The promise the planted stop makes, kept by a real worker that wakes late.

    The child sleeps past the publication window and then runs the real worker
    command in-process. The start answers unresponsive with the stop planted;
    the worker then takes the handle, reads the stop before its first step, and
    ends `stopped` having run nothing, instead of running the whole plan behind
    a caller who was told the start had failed."""
    from agentic_hil.process import spawn_detached_process

    workspace, plan = bench_workspace(tmp_path, monkeypatch, LONG_DELAY_PLAN)
    config = load_authoritative_config(workspace)
    monkeypatch.setattr(runlifecycle, "WORKER_PUBLISH_TIMEOUT_S", 0.5)

    def late_worker(inner_config, handle: str, test_config_path: str, *, wait_s: float) -> subprocess.Popen:
        arguments = ["test-reactor", "--test-config", test_config_path, "--wait-s", str(wait_s), "--run-handle", handle]
        script = (
            "import runpy, sys, time\n"
            "time.sleep(2.0)\n"
            f"sys.argv = ['agentic_hil', *{arguments!r}]\n"
            "runpy.run_module('agentic_hil', run_name='__main__', alter_sys=True)\n"
        )
        with open(runlifecycle.runs_directory(inner_config) / f"{handle}.log", "ab") as log:
            return spawn_detached_process([sys.executable, "-c", script], stdin=subprocess.DEVNULL, stdout=log, stderr=log, cwd=str(inner_config.work_dir))

    monkeypatch.setattr(runlifecycle, "spawn_run_worker", late_worker)

    result = runlifecycle.start_detached_run(config, str(plan), wait_s=0.0)

    handle = result["run"]
    detached_runs.append((config, handle))
    assert result["error_type"] == "run_worker_unresponsive", result
    assert result["retry_safe"] is False, result
    assert runlifecycle.stop_path(config, handle).exists()
    ended = wait_for_state(config, handle, {"finished", "stopped", "worker_gone"})
    assert ended["state"] == "stopped", f"{ended.get('summary')!r}; the worker printed:\n{worker_log(config, handle)}"
    assert ended["error_type"] == "run_stopped", ended
    assert ended["stopped_after_step"] == 0, ended
    report = json.loads((workspace / ".agentic-hil" / "reports" / "last-report.json").read_text(encoding="utf-8"))
    assert report["run"] == handle
    assert report["stopped"] is True
    assert report["steps"] == []


# --- a stop asked while the worker waits for a held device -------------------


def test_a_stop_asked_while_the_worker_waits_for_a_held_device_ends_the_wait(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A run in `starting` reads its stop inside the device wait, not after it.

    A stranger holds the plan's device, the run is registered and asked to
    stop, and then waits up to 3 s for the device. Today the wait runs to its
    end and the run ends `device_busy`; a stop asked of a run that holds
    nothing yet has to end the wait and the run, as `stopped`."""
    from agentic_hil.bench import BenchMutex
    from agentic_hil.reactorrun import run_registered_plan

    workspace, plan = bench_workspace(tmp_path, monkeypatch, LONG_DELAY_PLAN)
    config = load_authoritative_config(workspace)
    test_config = load_test_config(str(plan), config.work_dir)
    stranger = BenchMutex(frontend="stranger", label="other-bench-session")
    stranger.acquire(declared_devices(config, test_config))
    try:
        registration = runlifecycle.RunRegistration.take(config, runlifecycle.new_run_handle(), name=test_config.name, test_config_path=test_config.path, detached=True)
        with registration:
            requested = runlifecycle.request_run_stop(config, registration.handle)
            assert requested["ok"] is True, requested
            assert requested["stop_requested"] is True, requested
            started = time.monotonic()
            result = run_registered_plan(config, test_config, wait_s=3.0, registration=registration)
            elapsed_s = time.monotonic() - started
            registration.finish(result)
    finally:
        stranger.release_all()

    # Well under the 3 s the wait was granted: the stop ended it.
    assert elapsed_s < 2.0, (elapsed_s, result.get("error_type"), result.get("summary"))
    assert result["stopped"] is True, result
    assert result["error_type"] == "run_stopped", result
    assert result["steps"] == []
    assert runlifecycle.run_status(config, registration.handle)["state"] == "stopped"


def test_a_stop_asked_of_a_starting_run_does_not_promise_a_step_it_is_not_in(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The wording, for a run that holds nothing yet.

    `test-reactor-stop` on a `starting` record answered that the run finishes
    the step it is in, and `test-reactor-status` that it is on its first step.
    Neither is true of a run still waiting for its devices, and a reader who
    believes either goes away expecting a report the device wait may never let
    the run write."""
    workspace, plan = bench_workspace(tmp_path, monkeypatch, LONG_DELAY_PLAN)
    config = load_authoritative_config(workspace)
    registration = runlifecycle.RunRegistration.take(config, runlifecycle.new_run_handle(), name="waiting", test_config_path=str(plan), detached=True)
    with registration:
        status = runlifecycle.run_status(config, registration.handle)
        requested = runlifecycle.request_run_stop(config, registration.handle)
        registration.finish({"ok": False, "stopped": True, "error_type": "run_stopped"})

    assert status["state"] == "starting", status
    assert "its first step" not in status["summary"], status["summary"]
    assert requested["ok"] is True, requested
    assert requested["state"] == "starting", requested
    assert "the step it is in" not in requested["summary"], requested["summary"]


# --- the report a status names -----------------------------------------------


def test_status_of_an_earlier_run_names_that_runs_own_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """After a later run, the earlier run's status still points at its own report.

    The per-run copy under the state root is the canonical report and the run's
    own result named it; the status of the same handle sent a reader to the
    shared `last-report.json`, which the next run had already overwritten with
    its own report."""
    from agentic_hil.cli import run_test_reactor

    workspace, plan = bench_workspace(tmp_path, monkeypatch, SHORT_DELAY_PLAN)
    config = load_authoritative_config(workspace)
    first = run_test_reactor(str(plan))
    second = run_test_reactor(str(plan))
    assert first["ok"] is True and second["ok"] is True, (first, second)
    assert first["run"] != second["run"]

    status = runlifecycle.run_status(config, first["run"])

    assert status["state"] == "finished", status
    named = status.get("canonical_report_path") or status["report_path"]
    report_file = Path(named) if Path(named).is_absolute() else workspace / named
    assert report_file.is_file(), named
    assert json.loads(report_file.read_text(encoding="utf-8"))["run"] == first["run"], named
    assert str(named) in status["summary"], status["summary"]


# --- records of killed workers -----------------------------------------------


def dead_worker_records(config, count: int) -> list[str]:
    """`count` records in state `running` whose lock nobody holds, oldest last.

    What a runner reboot leaves behind: the record says running, the worker is
    gone, and the lock the operating system held for it went with it."""
    runlifecycle.runs_directory(config).mkdir(parents=True, exist_ok=True)
    handles = [f"run-{index:016x}" for index in range(1, count + 1)]
    for index, handle in enumerate(handles):
        runlifecycle.write_run_record(config, handle, {"version": runlifecycle.RUN_RECORD_VERSION, "state": "running", "run": handle, "run_ok": None})
        modified = 1_700_000_000 - index
        os.utime(runlifecycle.record_path(config, handle), (modified, modified))
    return handles


def test_dead_worker_records_past_the_kept_count_are_pruned(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The documented cap holds for records whose worker was killed.

    Only terminal records were candidates, so a `running` record with a free
    lock survived every prune and a bench whose runners get rebooted grew past
    `RUN_RECORDS_KEPT` without bound, and every listing paid one lock probe per
    such record."""
    workspace, _ = bench_workspace(tmp_path, monkeypatch, LONG_DELAY_PLAN)
    config = load_authoritative_config(workspace)
    monkeypatch.setattr(runlifecycle, "RUN_RECORDS_KEPT", 10)
    dead_worker_records(config, runlifecycle.RUN_RECORDS_KEPT + 20)

    runlifecycle.prune_run_records(config)

    remaining = list(runlifecycle.runs_directory(config).glob("run-*.json"))
    assert len(remaining) <= runlifecycle.RUN_RECORDS_KEPT, len(remaining)
    listed = runlifecycle.known_runs(config)
    assert listed["ok"] is True, listed
    assert len(listed["runs"]) <= runlifecycle.RUN_RECORDS_KEPT, len(listed["runs"])
    assert {item["state"] for item in listed["runs"]} <= {runlifecycle.RUN_WORKER_GONE}


def test_a_live_run_is_never_pruned_however_old_its_record_is(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The neighbour that must not move: a record whose lock is held is a run.

    Pruning dead-worker records must be decided by the lock and never by the
    state field, or a long run would lose its record from under a reader the
    moment enough newer runs had finished."""
    from agentic_hil.bench import _LifetimeLock

    workspace, _ = bench_workspace(tmp_path, monkeypatch, LONG_DELAY_PLAN)
    config = load_authoritative_config(workspace)
    monkeypatch.setattr(runlifecycle, "RUN_RECORDS_KEPT", 0)
    oldest = dead_worker_records(config, 3)[-1]
    lock = _LifetimeLock(runlifecycle.lock_path(config, oldest))
    lock.acquire()
    try:
        runlifecycle.prune_run_records(config)
        assert runlifecycle.record_path(config, oldest).exists()
        assert runlifecycle.run_status(config, oldest)["state"] == "running"
    finally:
        lock.release()


# --- debugger steps a permission refuses -------------------------------------

FLASH_STEP = "  - {debugger: dut, action: flash, image_path: build/app.elf}\n"
FLASH_WITH_RESET_STEP = "  - {debugger: dut, action: flash, image_path: build/app.elf, reset_after_flash: true}\n"
ATTACH_STEP = "  - {debugger: dut, action: debug_start, image_path: build/app.elf, mode: attach}\n"
RESET_HALT_STEP = "  - {debugger: dut, action: debug_start, image_path: build/app.elf, mode: reset_halt}\n"
LOAD_STEP = "  - {debugger: dut, action: debug_start, image_path: build/app.elf, mode: load}\n"
BREAKPOINT_STEP = "  - {debugger: dut, action: run_until_breakpoint, location: test_done, timeout_s: 5}\n"


@pytest.mark.parametrize(
    ("flag", "value", "steps", "field", "granted"),
    [
        pytest.param("allow_flash", False, FLASH_STEP, "steps[0].action", False, id="flash without allow_flash"),
        pytest.param("allow_reset", False, FLASH_WITH_RESET_STEP, "steps[0].reset_after_flash", False, id="reset_after_flash without allow_reset"),
        pytest.param("allow_probe", False, ATTACH_STEP, "steps[0].action", False, id="debug_start without allow_probe"),
        pytest.param("allow_reset", False, RESET_HALT_STEP, "steps[0].mode", False, id="debug_start reset_halt without allow_reset"),
        pytest.param("allow_flash", False, LOAD_STEP, "steps[0].mode", False, id="debug_start load without allow_flash"),
        pytest.param("allow_debug_execution", False, ATTACH_STEP + BREAKPOINT_STEP, "steps[1].action", False, id="run_until_breakpoint without allow_debug_execution"),
        pytest.param("allow_raw_debugger_commands", True, FLASH_STEP, "steps[0].action", True, id="flash while raw debugger commands are allowed"),
    ],
)
def test_debugger_step_permission_refusals_name_the_key_and_no_step_runs(tmp_path: Path, flag: str, value: bool, steps: str, field: str, granted: bool) -> None:
    """The #443/#444/#468 contract, for every debugger step kind a permission gates.

    Refused at preflight, before any device is touched: `permission_denied`
    naming the dotted key in `permission` and in the summary, the grant line in
    `next_step`, an empty step list and a tool log with no invocation. The
    exclusive flags are named as the key that must be closed, not opened."""
    config = load_config(str(write_config(tmp_path, permissions={**DEFAULT_TEST_PERMISSIONS, flag: value})))
    plan_path = write_test_config(tmp_path, "version: 2\nsteps:\n" + steps)
    (tmp_path / "build").mkdir(exist_ok=True)
    (tmp_path / "build" / "app.elf").write_bytes(b"\x7fELF" + b"\x00" * 12)
    service = RecordingService()

    result = TestReactor(config, service).run(load_test_config(str(plan_path), str(tmp_path)))  # type: ignore[arg-type]

    key = f"debuggers.dut.permissions.{flag}"
    assert result["ok"] is False, result
    assert result["error_type"] == "permission_denied", result
    assert result["step_error_type"] == "permission_denied", result
    assert result["permission"] == key, result
    assert result["validation_error"]["error_type"] == "permission_denied", result["validation_error"]
    assert result["validation_error"]["permission"] == key, result["validation_error"]
    assert result["validation_error"]["field"] == field, result["validation_error"]
    assert f"`{key}`" in result["validation_error"]["summary"], result["validation_error"]["summary"]
    assert f"`{key}`" in result["summary"], result["summary"]
    next_step = result["validation_error"]["next_step"]
    if granted:
        assert result["validation_error"]["permission_granted"] is True, result["validation_error"]
        assert f"agentic-hil revoke {key}" in next_step, next_step
        assert f"agentic-hil grant {key}" not in next_step, next_step
    else:
        assert "permission_granted" not in result["validation_error"], result["validation_error"]
        assert f"agentic-hil grant {key}" in next_step, next_step
    assert result["steps"] == []
    assert service.calls == []


def test_a_granted_debugger_plan_runs_every_step_the_refusals_above_gate(tmp_path: Path) -> None:
    """The neighbour: the same steps, granted, run and call the tools they name."""
    config = load_config(str(write_config(tmp_path)))
    plan_path = write_test_config(tmp_path, "version: 2\nsteps:\n" + FLASH_WITH_RESET_STEP + ATTACH_STEP + BREAKPOINT_STEP)
    (tmp_path / "build").mkdir(exist_ok=True)
    (tmp_path / "build" / "app.elf").write_bytes(b"\x7fELF" + b"\x00" * 12)
    service = RecordingService()

    result = TestReactor(config, service).run(load_test_config(str(plan_path), str(tmp_path)))  # type: ignore[arg-type]

    assert result["ok"] is True, result
    assert [step["action"] for step in result["steps"]] == ["flash", "debug_start", "run_until_breakpoint"]
    assert [name for name, _ in service.calls][:2] == ["flash_firmware", "debug_start_session"]


# --- a plan run over MCP inside the server's own bench run -------------------


def test_a_plan_run_inside_the_servers_own_bench_run_is_answered_as_its_own_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An agent holding the board through `bench_run_start` asks this server to run a plan.

    The plan needs the same board, and the refusal has to say that this session
    holds it and that `bench_run_stop` is the way out. Today the answer is
    `device_busy` naming this process's own pid as the holder, with the
    catalogue's advice to wait for another owner's run to end, which is a run
    that ends when this agent ends it."""
    from agentic_hil.tools import AgenticHILToolService

    workspace, plan = bench_workspace(tmp_path, monkeypatch, LONG_DELAY_PLAN)
    service = AgenticHILToolService(load_authoritative_config(workspace), frontend="mcp")
    try:
        opened = service.call("bench_run_start", {"devices": [{"kind": "debugger", "id": "dut"}], "label": "agent-run"})
        assert opened["ok"] is True, opened

        refused = service.call("test_reactor_run", {"test_config_path": str(plan)})

        assert refused["ok"] is False, refused
        assert refused["error_type"] == "run_already_active" or "bench_run_stop" in refused["summary"], refused
        assert refused.get("steps", []) == []
        # The agent's own run is untouched by the refusal and ends when it says so.
        assert service.call("bench_run_status", {})["run_label"] == "agent-run"
        assert service.call("bench_run_stop", {})["ok"] is True
    finally:
        service.close()


def test_a_plan_run_over_mcp_against_a_strangers_hold_is_still_device_busy_naming_them(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The neighbour: a board another process holds is still `device_busy` with that holder."""
    from agentic_hil.bench import BenchMutex
    from agentic_hil.tools import AgenticHILToolService

    workspace, plan = bench_workspace(tmp_path, monkeypatch, LONG_DELAY_PLAN)
    config = load_authoritative_config(workspace)
    stranger = BenchMutex(frontend="stranger", label="other-bench-session")
    stranger.acquire(declared_devices(config, load_test_config(str(plan), config.work_dir)))
    service = AgenticHILToolService(config, frontend="mcp")
    try:
        refused = service.call("test_reactor_run", {"test_config_path": str(plan)})
    finally:
        service.close()
        stranger.release_all()

    assert refused["ok"] is False, refused
    assert refused["error_type"] == "device_busy", refused
    assert refused["holder"]["label"] == "other-bench-session", refused
    assert refused["steps"] == []
