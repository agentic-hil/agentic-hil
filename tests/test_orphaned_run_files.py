"""The runs directory after a worker that never came alive (#514).

A detached start that gives up on a worker which published nothing inside its
window reports `run_worker_unresponsive` and writes `<handle>.stop`, and
`spawn_run_worker` has already created `<handle>.log`. A worker that never comes
alive writes no `run-<handle>.json`, and `prune_run_records` lists its candidates
through `_records_newest_first`, which globs `run-*.json`, so that pair is never
a candidate and the directory grows by one pair per such handle for the life of
the bench.

The decided behaviour, and what the tests here encode, one per line:

* a `.stop` or `.log` whose handle has no record is removed once that file is
  older than the window a detached start waits for its worker, which is the
  window `worker_publish_window_s` computes and no second number,
* a fresh orphan, inside that window, is left alone: a start command may still
  be waiting in it, and the worker it is waiting for owns that log,
* a stop belonging to a handle whose record exists stays with that record's own
  lifecycle: it goes when the record goes and never before,
* a record whose lock is held is a live run and neither it nor its files are
  touched however old they are,
* the prune stays silent about what it removed, the way it is today,
* and the orphan handle is not a run either side of the prune: status answers
  `run_not_found` before and after.

Everything here is in process and over the coordination state files only. No
worker is spawned: what the module is asked is what it does with files that are
already there, and planting them is the whole of the arrangement.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest
from test_run_lifecycle import LONG_DELAY_PLAN, bench_workspace

import agentic_hil.runlifecycle as runlifecycle
from agentic_hil.bench import MAX_WAIT_S, _LifetimeLock
from agentic_hil.config import load_authoritative_config

# A handle no generated record collides with: the record handles below are
# minted from a counter, so the top of the space is free for the orphan.
ORPHAN_HANDLE = "run-ffffffffffffffff"


def log_path(config, handle: str) -> Path:
    """Where `spawn_run_worker` opens the worker's output, named the same way."""
    return runlifecycle.runs_directory(config) / f"{runlifecycle.validated_run_handle(handle)}.log"


def past_every_publish_window_s() -> float:
    """An age older than any window a detached start can wait for a worker in.

    Taken from the module rather than written down: the window is
    `worker_publish_window_s`, and its widest value is the one it computes for
    the largest device wait the bench admits. A test that hard coded thirty
    seconds would pass while the constant moved, and would also be a second
    number of exactly the kind the issue refuses.
    """
    return runlifecycle.worker_publish_window_s(MAX_WAIT_S) + 60.0


def plant_orphan_files(config, handle: str, *, age_s: float) -> tuple[Path, Path]:
    """The pair a give-up leaves behind: a planted stop and a worker log, aged.

    Written the way the code writes them, a stop carrying the fields
    `_plant_stop_after_unresponsive` puts in it and a log carrying whatever the
    worker managed to print, and with no record beside them, which is the case
    the issue is about.
    """
    directory = runlifecycle.runs_directory(config)
    directory.mkdir(parents=True, exist_ok=True)
    stop = runlifecycle.stop_path(config, handle)
    stop.write_text(
        json.dumps({"run": handle, "requested_at": runlifecycle.utc_now_iso(), "requested_by_pid": os.getpid()}, indent=2) + "\n",
        encoding="utf-8",
    )
    log = log_path(config, handle)
    log.write_text("the worker never got far enough to say anything\n", encoding="utf-8")
    modified = time.time() - age_s
    for path in (stop, log):
        os.utime(path, (modified, modified))
    return stop, log


def terminal_records(config, count: int) -> list[str]:
    """`count` finished records, oldest last, with distinct modification times.

    Distinct because the prune orders by modification time and several records
    written in one loop share a clock tick on Windows, which would leave which
    of them is oldest to the stability of the sort.
    """
    runlifecycle.runs_directory(config).mkdir(parents=True, exist_ok=True)
    handles = [f"run-{index:016x}" for index in range(1, count + 1)]
    for index, handle in enumerate(handles):
        runlifecycle.write_run_record(config, handle, {"version": runlifecycle.RUN_RECORD_VERSION, "state": "finished", "run": handle, "run_ok": True})
        modified = 1_700_000_000 - index
        os.utime(runlifecycle.record_path(config, handle), (modified, modified))
    return handles


def directory_listing(config) -> list[str]:
    """What is in the runs directory, for an assertion that has to say why."""
    return sorted(path.name for path in runlifecycle.runs_directory(config).iterdir())


def test_an_orphaned_stop_and_log_older_than_the_publish_window_are_pruned(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """The whole of #514: the pair of a handle that never got a record goes.

    A worker that hung before it could take its registration writes no record,
    so the handle is not among the candidates the prune globs and its planted
    stop and its worker log survive every prune for the life of the bench. Past
    the window the start command waited in, nothing is coming for either file:
    the handle is not a run, it will never be one, and the two files are the
    only thing left saying it was ever attempted.

    The cap is asserted in the same test because the two must hold together:
    removing orphans is not licence to keep fewer, or more, records than
    `RUN_RECORDS_KEPT`.
    """
    workspace, _ = bench_workspace(tmp_path, monkeypatch, LONG_DELAY_PLAN)
    config = load_authoritative_config(workspace)
    monkeypatch.setattr(runlifecycle, "RUN_RECORDS_KEPT", 10)
    stop, log = plant_orphan_files(config, ORPHAN_HANDLE, age_s=past_every_publish_window_s())
    terminal_records(config, runlifecycle.RUN_RECORDS_KEPT + 5)
    capsys.readouterr()

    assert runlifecycle.prune_run_records(config) is None

    assert [path.name for path in (stop, log) if path.exists()] == [], directory_listing(config)
    remaining = list(runlifecycle.runs_directory(config).glob("run-*.json"))
    assert len(remaining) == runlifecycle.RUN_RECORDS_KEPT, directory_listing(config)
    # Silent, the way it is today: housekeeping in the middle of a run's
    # registration does not narrate itself onto a caller's output.
    printed = capsys.readouterr()
    assert printed.out == ""
    assert printed.err == ""


def test_status_answers_run_not_found_for_the_orphan_handle_before_and_after_the_prune(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The neighbour that must not move: the orphan handle is not a run either way.

    A stop and a log are not a run to any reader, and the prune removing them
    changes nothing a caller can see. Before, the answer is `run_not_found`
    because there is no record; after, for the same reason, and the refusal
    keeps its fields.
    """
    workspace, _ = bench_workspace(tmp_path, monkeypatch, LONG_DELAY_PLAN)
    config = load_authoritative_config(workspace)
    monkeypatch.setattr(runlifecycle, "RUN_RECORDS_KEPT", 1)
    plant_orphan_files(config, ORPHAN_HANDLE, age_s=past_every_publish_window_s())
    terminal_records(config, 3)

    before = runlifecycle.run_status(config, ORPHAN_HANDLE)
    runlifecycle.prune_run_records(config)
    after = runlifecycle.run_status(config, ORPHAN_HANDLE)

    for answer in (before, after):
        assert answer["ok"] is False, answer
        assert answer["error_type"] == "run_not_found", answer
        assert answer["run"] == ORPHAN_HANDLE, answer
        assert answer["side_effect_committed"] is False, answer


def test_a_fresh_orphaned_stop_and_log_inside_the_publish_window_are_left_alone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The neighbour the age is there for: inside the window, a worker is still expected.

    The log is opened before the worker exists and the stop is planted while the
    start command is still deciding, so a pair younger than the window belongs to
    a handle whose worker may yet publish its first record. Removing it would
    take a live worker's output out from under it and drop a stop request the
    worker is about to read.

    Two ages, both inside every window the module can compute: one just planted,
    and one older than that but still short of the base startup window.
    """
    workspace, _ = bench_workspace(tmp_path, monkeypatch, LONG_DELAY_PLAN)
    config = load_authoritative_config(workspace)
    monkeypatch.setattr(runlifecycle, "RUN_RECORDS_KEPT", 0)
    just_planted = plant_orphan_files(config, ORPHAN_HANDLE, age_s=0.0)
    still_starting = plant_orphan_files(config, "run-fffffffffffffffe", age_s=runlifecycle.WORKER_PUBLISH_TIMEOUT_S / 2)
    terminal_records(config, 3)

    runlifecycle.prune_run_records(config)

    for path in (*just_planted, *still_starting):
        assert path.exists(), directory_listing(config)


def test_a_stop_and_a_log_for_a_live_run_are_left_alone_however_old_they_are(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The neighbour with the hardest edge: a held lock is a run, and its files are its own.

    A long run asked to stop early has a stop file as old as the request and a
    log as old as the spawn, and it is still running: the record is there and
    the lock behind it cannot be taken. Nothing about that pair may be decided
    by its age.
    """
    workspace, _ = bench_workspace(tmp_path, monkeypatch, LONG_DELAY_PLAN)
    config = load_authoritative_config(workspace)
    monkeypatch.setattr(runlifecycle, "RUN_RECORDS_KEPT", 0)
    live = "run-00000000000000ff"
    runlifecycle.write_run_record(config, live, {"version": runlifecycle.RUN_RECORD_VERSION, "state": "running", "run": live, "run_ok": None})
    os.utime(runlifecycle.record_path(config, live), (1_700_000_000, 1_700_000_000))
    stop, log = plant_orphan_files(config, live, age_s=past_every_publish_window_s())
    terminal_records(config, 3)
    lock = _LifetimeLock(runlifecycle.lock_path(config, live))
    lock.acquire()
    try:
        runlifecycle.prune_run_records(config)

        assert runlifecycle.record_path(config, live).exists(), directory_listing(config)
        assert stop.exists(), directory_listing(config)
        assert log.exists(), directory_listing(config)
        answer = runlifecycle.run_status(config, live)
        assert answer["state"] == "running", answer
        assert answer["stop_requested_at"] is not None, answer
    finally:
        lock.release()


def test_the_stop_and_log_of_a_kept_record_go_with_the_record_and_not_before(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The neighbour an age rule would break: a record's files follow the record.

    A finished run inside the cap keeps its record, and its stop and its log are
    part of what that record is: a reader asking what happened to a run that was
    stopped reads both. A prune that removed either because it was older than
    the publish window would leave a record whose account of itself is gone. So
    the pair survives while the record does, and goes in the same pass the
    record goes in, which is the pass that takes it past the cap.
    """
    workspace, _ = bench_workspace(tmp_path, monkeypatch, LONG_DELAY_PLAN)
    config = load_authoritative_config(workspace)
    kept = "run-00000000000000ff"
    runlifecycle.write_run_record(config, kept, {"version": runlifecycle.RUN_RECORD_VERSION, "state": "stopped", "run": kept, "run_ok": False})
    os.utime(runlifecycle.record_path(config, kept), (1_700_000_000, 1_700_000_000))
    stop, log = plant_orphan_files(config, kept, age_s=past_every_publish_window_s())
    terminal_records(config, 3)

    monkeypatch.setattr(runlifecycle, "RUN_RECORDS_KEPT", 10)
    runlifecycle.prune_run_records(config)

    assert runlifecycle.record_path(config, kept).exists(), directory_listing(config)
    assert stop.exists(), directory_listing(config)
    assert log.exists(), directory_listing(config)

    monkeypatch.setattr(runlifecycle, "RUN_RECORDS_KEPT", 0)
    runlifecycle.prune_run_records(config)

    assert not runlifecycle.record_path(config, kept).exists(), directory_listing(config)
    assert not stop.exists(), directory_listing(config)
    assert not log.exists(), directory_listing(config)
