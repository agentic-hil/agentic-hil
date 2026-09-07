"""The runs directory after a worker that never came alive (#514).

A detached start that gives up on a worker which published nothing inside its
window reports `run_worker_unresponsive` and writes `<handle>.stop`, and
`spawn_run_worker` has already created `<handle>.log`. A worker that never comes
alive writes no `run-<handle>.json`, and `prune_run_records` lists its candidates
through `_records_newest_first`, which globs `run-*.json`, so that pair is never
a candidate and the directory grows by one pair per such handle for the life of
the bench.

The decided behaviour, and what the tests here encode, one per line:

* a `.stop` or `.log` whose handle has no record is removed once it is older
  than the window a detached start waits for its worker, which is the window
  `worker_publish_window_s` computes and no second number,
* that removal does not wait for the record cap to be exceeded: a bench whose
  starts all time out writes no records at all, which is the bench the issue
  reports, and it is the bench where nothing would ever be removed,
* the threshold is the widest window a start can wait in, because the prune
  cannot know which wait the start that left a file behind was handed,
* what is left behind by the sweep is nothing: the handle's lock goes with its
  stop and its log, or a directory that grew by two files per handle grows by
  one instead,
* a fresh orphan, inside that window, is left alone: a start command may still
  be waiting in it, and the worker it is waiting for owns that log,
* an orphan whose lock is held is a worker that is alive between taking its
  registration lock and writing its first record, and its files are its own,
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
SECOND_ORPHAN_HANDLE = "run-fffffffffffffffe"


LOG_TEXT = "the worker never got far enough to say anything\n"


def log_path(config, handle: str) -> Path:
    """Where `spawn_run_worker` opens the worker's output, asked of the module.

    Asked rather than spelled out again, so that a log name which moves in the
    code moves what these tests plant with it. The primary test reads the
    planted log back through `worker_output` as well, which is the reader the
    start command's refusal uses.
    """
    return runlifecycle.worker_log_path(config, handle)


def past_every_publish_window_s() -> float:
    """An age older than any window a detached start can wait for a worker in.

    Taken from the module rather than written down: the window is
    `worker_publish_window_s`, and its widest value is the one it computes for
    the largest device wait the bench admits. A test that hard coded thirty
    seconds would pass while the constant moved, and would also be a second
    number of exactly the kind the issue refuses.
    """
    return runlifecycle.worker_publish_window_s(MAX_WAIT_S) + 60.0


def plant_orphan_files(config, handle: str, *, age_s: float, stop_age_s: float | None = None, with_lock: bool = False) -> tuple[Path, ...]:
    """The files a give-up leaves behind: a planted stop and a worker log, aged.

    Written the way the code writes them, a stop carrying the fields
    `_plant_stop_after_unresponsive` puts in it and a log carrying whatever the
    worker managed to print, and with no record beside them, which is the case
    the issue is about.

    `with_lock` plants the third file of the set. A lock is left on disk by any
    reader that asked whether this handle's worker was gone, because `release`
    closes the descriptor and does not unlink, so a sweep that probes before it
    removes will find or create one. It is planted here so that a sweep which
    forgets it is caught leaving the directory growing by a file per handle.

    `stop_age_s` ages the stop apart from the log, for the case where the start
    command planted its stop only a moment ago over a log as old as the spawn.
    """
    directory = runlifecycle.runs_directory(config)
    directory.mkdir(parents=True, exist_ok=True)
    stop = runlifecycle.stop_path(config, handle)
    stop.write_text(
        json.dumps({"run": handle, "requested_at": runlifecycle.utc_now_iso(), "requested_by_pid": os.getpid()}, indent=2) + "\n",
        encoding="utf-8",
    )
    log = log_path(config, handle)
    log.write_text(LOG_TEXT, encoding="utf-8")
    planted = [stop, log]
    if with_lock:
        lock = runlifecycle.lock_path(config, handle)
        lock.write_bytes(b"0")
        planted.append(lock)
    now = time.time()
    for path in planted:
        age = stop_age_s if path == stop and stop_age_s is not None else age_s
        modified = now - age
        os.utime(path, (modified, modified))
    return tuple(planted)


def terminal_records(config, count: int) -> list[str]:
    """`count` finished records, newest first, with distinct modification times.

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
    """What is in the runs directory, for an assertion that has to say why.

    The assertions below are over the whole of this rather than over the two
    files a test planted, because the question the issue asks is whether the
    directory stops growing, and a sweep that trades a pair of files for a
    single leftover lock answers it no while satisfying every narrower check.
    """
    return sorted(path.name for path in runlifecycle.runs_directory(config).iterdir())


def record_names(handles) -> list[str]:
    """The file names those handles' records are stored under."""
    return sorted(f"{handle}.json" for handle in handles)


def test_an_orphaned_stop_and_log_older_than_the_publish_window_are_pruned(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]) -> None:
    """The whole of #514: the files of a handle that never got a record go.

    A worker that hung before it could take its registration writes no record,
    so the handle is not among the candidates the prune globs and its planted
    stop and its worker log survive every prune for the life of the bench. Past
    the window the start command waited in, nothing is coming for either file:
    the handle is not a run, it will never be one, and the two files are the
    only thing left saying it was ever attempted.

    The cap is asserted in the same test because the two must hold together:
    removing orphans is not licence to keep fewer, or more, records than
    `RUN_RECORDS_KEPT`. The assertion is over the whole directory, so a lock
    left behind for the swept handle fails it as loudly as a stop would.
    """
    workspace, _ = bench_workspace(tmp_path, monkeypatch, LONG_DELAY_PLAN)
    config = load_authoritative_config(workspace)
    monkeypatch.setattr(runlifecycle, "RUN_RECORDS_KEPT", 10)
    plant_orphan_files(config, ORPHAN_HANDLE, age_s=past_every_publish_window_s(), with_lock=True)
    handles = terminal_records(config, runlifecycle.RUN_RECORDS_KEPT + 5)
    # Read through the module's own reader, so that the file this test plants is
    # the file the code means by this handle's log and not a name that drifted.
    assert runlifecycle.worker_output(config, ORPHAN_HANDLE) == LOG_TEXT
    capfd.readouterr()

    assert runlifecycle.prune_run_records(config) is None

    assert directory_listing(config) == record_names(handles[: runlifecycle.RUN_RECORDS_KEPT])
    assert runlifecycle.worker_output(config, ORPHAN_HANDLE) == ""
    # Silent, the way it is today: housekeeping in the middle of a run's
    # registration does not narrate itself onto a caller's output. Read at the
    # file descriptors rather than through `sys.stdout`, so a narration written
    # to a stream captured before this call is caught too.
    printed = capfd.readouterr()
    assert printed.out == ""
    assert printed.err == ""


def test_an_orphan_is_pruned_with_the_runs_directory_under_the_record_cap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The bench the issue reports has no records at all, and must still be swept.

    A bench whose starts keep timing out writes a stop and a log per attempt and
    never a record, so the count of records stays at zero and the cap is never
    reached. A sweep that runs only once there are more records than the bench
    keeps would leave exactly that bench growing without bound, which is the
    report. So the sweep is not a consequence of the cap being exceeded: it is
    what every prune does, and here it is asked of a directory holding nothing
    but three attempts that never became runs.
    """
    workspace, _ = bench_workspace(tmp_path, monkeypatch, LONG_DELAY_PLAN)
    config = load_authoritative_config(workspace)
    monkeypatch.setattr(runlifecycle, "RUN_RECORDS_KEPT", 100)
    # One of them with the lock file a probe of that handle leaves on disk and
    # two without, because the sweep has to end with neither kind still there.
    for handle, planted_lock in ((ORPHAN_HANDLE, True), (SECOND_ORPHAN_HANDLE, False), ("run-fffffffffffffffd", False)):
        plant_orphan_files(config, handle, age_s=past_every_publish_window_s(), with_lock=planted_lock)

    runlifecycle.prune_run_records(config)

    assert directory_listing(config) == []


def test_the_orphan_threshold_follows_the_publish_window_constant(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The age is read from the start command's own constant, not written down.

    Both directions, because either one alone is satisfied by a number that
    happens to sit on the right side of the ages this file plants. With the
    window widened past any age here, a file the unpatched module would remove
    stays; with the window narrowed, a file the unpatched module would keep
    goes. Only a threshold computed from `worker_publish_window_s`, and so from
    `WORKER_PUBLISH_TIMEOUT_S`, satisfies both.
    """
    workspace, _ = bench_workspace(tmp_path, monkeypatch, LONG_DELAY_PLAN)
    config = load_authoritative_config(workspace)
    monkeypatch.setattr(runlifecycle, "RUN_RECORDS_KEPT", 0)
    aged = past_every_publish_window_s()

    monkeypatch.setattr(runlifecycle, "WORKER_PUBLISH_TIMEOUT_S", 10_000.0)
    widened = plant_orphan_files(config, ORPHAN_HANDLE, age_s=aged)

    runlifecycle.prune_run_records(config)

    for path in widened:
        assert path.exists(), directory_listing(config)

    monkeypatch.setattr(runlifecycle, "WORKER_PUBLISH_TIMEOUT_S", 1.0)
    monkeypatch.setattr(runlifecycle, "MAX_WAIT_S", 1.0)

    runlifecycle.prune_run_records(config)

    assert directory_listing(config) == []


def test_the_orphan_threshold_is_the_widest_window_a_start_can_wait_in(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A start still inside its own wait keeps its log, whatever wait it was handed.

    `worker_publish_window_s` extends the startup window by the device wait the
    worker was granted, and the prune is called from another run's registration
    and cannot know which wait that was. A threshold built from the startup
    window alone would delete the log of a start that is still legitimately
    waiting for a held bench, and `worker_output` reads that log to say why the
    start failed. So the threshold is the window computed for the largest wait
    the bench admits, and a file younger than that is never an orphan.
    """
    workspace, _ = bench_workspace(tmp_path, monkeypatch, LONG_DELAY_PLAN)
    config = load_authoritative_config(workspace)
    monkeypatch.setattr(runlifecycle, "RUN_RECORDS_KEPT", 0)
    monkeypatch.setattr(runlifecycle, "WORKER_PUBLISH_TIMEOUT_S", 1.0)
    monkeypatch.setattr(runlifecycle, "MAX_WAIT_S", 1000.0)
    still_waiting = plant_orphan_files(config, ORPHAN_HANDLE, age_s=500.0)

    runlifecycle.prune_run_records(config)

    for path in still_waiting:
        assert path.exists(), directory_listing(config)


def test_a_fresh_orphaned_stop_and_log_inside_the_publish_window_are_left_alone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The neighbour the age is there for: inside the window, a worker is still expected.

    The log is opened before the worker exists and the stop is planted while the
    start command is still deciding, so a pair younger than the window belongs to
    a handle whose worker may yet publish its first record. Removing it would
    take a live worker's output out from under it and drop a stop request the
    worker is about to read.

    Two ages, both inside every window the module can compute: one just planted,
    and one older than that but still a small fraction of the base startup
    window, so that the record writes in between cannot age it over any line.
    """
    workspace, _ = bench_workspace(tmp_path, monkeypatch, LONG_DELAY_PLAN)
    config = load_authoritative_config(workspace)
    monkeypatch.setattr(runlifecycle, "RUN_RECORDS_KEPT", 0)
    just_planted = plant_orphan_files(config, ORPHAN_HANDLE, age_s=0.0)
    still_starting = plant_orphan_files(config, SECOND_ORPHAN_HANDLE, age_s=runlifecycle.WORKER_PUBLISH_TIMEOUT_S / 10)
    terminal_records(config, 3)

    runlifecycle.prune_run_records(config)

    assert directory_listing(config) == sorted(path.name for path in (*just_planted, *still_starting))


def test_an_orphan_whose_lock_is_held_is_left_alone_however_old_it_is(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A held lock with no record beside it is a worker alive and not yet registered.

    `RunRegistration.take` acquires the lock before it writes the first record,
    so a worker that is slow between those two steps holds `<handle>.lock` with
    no `run-<handle>.json` anywhere. Its files look exactly like the orphan this
    sweep is for, and the stop among them is the one thing that will end it at
    its first step boundary if it does reach the board. Age decides nothing
    here: the lock does, the same way it decides for a record.

    What the files still being there proves depends on the platform, and this
    has to hold on both. Windows refuses to unlink a file whose lock is held, so
    a sweep that never asked about the lock leaves the same directory behind
    here as one that asked; POSIX lets that unlink through and the worker's stop
    is gone. So the removal is asserted at the attempt: the files of a handle
    whose lock is held are not offered for removal at all.
    """
    workspace, _ = bench_workspace(tmp_path, monkeypatch, LONG_DELAY_PLAN)
    config = load_authoritative_config(workspace)
    monkeypatch.setattr(runlifecycle, "RUN_RECORDS_KEPT", 0)
    # The lock is planted with the byte the acquire would have written and aged
    # with the rest, so that taking it leaves the age of the set where it is:
    # otherwise a fresh lock file would keep this set through the age rule and
    # say nothing about whether the lock itself was consulted.
    stop, log, _ = plant_orphan_files(config, ORPHAN_HANDLE, age_s=past_every_publish_window_s(), with_lock=True)
    attempted: list[str] = []
    unlink = Path.unlink

    def spying_unlink(self: Path, *args: object, **kwargs: object) -> None:
        attempted.append(self.name)
        return unlink(self, *args, **kwargs)

    lock = _LifetimeLock(runlifecycle.lock_path(config, ORPHAN_HANDLE))
    lock.acquire()
    try:
        monkeypatch.setattr(Path, "unlink", spying_unlink)

        runlifecycle.prune_run_records(config)

        assert attempted == [], attempted
        assert stop.exists(), directory_listing(config)
        assert log.exists(), directory_listing(config)
        assert runlifecycle.lock_path(config, ORPHAN_HANDLE).exists(), directory_listing(config)
    finally:
        monkeypatch.setattr(Path, "unlink", unlink)
        lock.release()


def test_an_orphan_whose_stop_was_just_planted_keeps_the_older_log_beside_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The files of one handle are one thing, and the newest of them dates the set.

    The log is as old as the spawn and the stop is written at the end of the
    window, so the two are never the same age. A sweep that dated each file on
    its own would take the log of a handle whose stop was planted a moment ago,
    and the log is what `worker_output` reads for the refusal the caller is
    about to be handed.
    """
    workspace, _ = bench_workspace(tmp_path, monkeypatch, LONG_DELAY_PLAN)
    config = load_authoritative_config(workspace)
    monkeypatch.setattr(runlifecycle, "RUN_RECORDS_KEPT", 0)
    stop, log = plant_orphan_files(config, ORPHAN_HANDLE, age_s=past_every_publish_window_s(), stop_age_s=0.0)

    runlifecycle.prune_run_records(config)

    assert stop.exists(), directory_listing(config)
    assert log.exists(), directory_listing(config)


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
