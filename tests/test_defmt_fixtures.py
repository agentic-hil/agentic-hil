"""Rust-free checks for the generated defmt test images."""

from __future__ import annotations

import errno
import multiprocessing
import os
import traceback
from multiprocessing.connection import Connection
from pathlib import Path

import conftest
import defmt_fixtures
import pytest
from support import scaled_time_bound


def test_fixture_skips_with_a_named_reason_when_cargo_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(defmt_fixtures.shutil, "which", lambda command: None if command == "cargo" else "rustc")

    with pytest.raises(pytest.skip.Exception, match="cargo not found on PATH"):
        conftest._require_defmt_toolchain()


def _contend_for_lock(path: Path, connection: Connection, timeout: float) -> None:
    import msvcrt

    locking = msvcrt.locking
    reported_contention = False

    def report_contention(descriptor: int, mode: int, length: int) -> None:
        nonlocal reported_contention
        try:
            locking(descriptor, mode, length)
        except OSError as error:
            if mode == msvcrt.LK_NBLCK and error.errno in {errno.EACCES, errno.EDEADLK} and not reported_contention:
                connection.send("contended")
                reported_contention = True
            raise

    with connection, pytest.MonkeyPatch.context() as patch:
        patch.setattr(msvcrt, "locking", report_contention)
        try:
            with defmt_fixtures.exclusive_file_lock(path):
                connection.send("acquired")
                assert connection.poll(timeout), "holder did not acknowledge the lock handover"
                assert connection.recv() == "release"
        except PermissionError as error:
            # Only the baseline pad flush has the declared failure signature.
            if error.errno != errno.EACCES or "lock_file.flush()" not in traceback.format_exc():
                raise
            connection.send("pad flush refused")


@pytest.mark.skipif(os.name != "nt", reason="requires Windows byte-range locking")
def test_exclusive_file_lock_waits_for_held_empty_file(tmp_path: Path) -> None:
    import msvcrt

    timeout = scaled_time_bound(30)
    path = tmp_path / "build.lock"
    context = multiprocessing.get_context("spawn")
    parent, contender = context.Pipe()
    child = context.Process(target=_contend_for_lock, args=(path, contender, timeout))
    with parent, contender, path.open("w+b") as holder:
        descriptor = holder.fileno()
        msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        child.start()
        contender.close()
        try:
            assert parent.poll(timeout), "contender did not report attempting the held lock"
            outcome = parent.recv()
            if outcome == "pad flush refused":
                raise AssertionError("The defmt lock contender could not flush the pad byte while byte zero was held.")
            assert outcome == "contended", "contender entered before the holder released the lock"
            msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)

            assert parent.poll(timeout), "contender did not acquire the released lock"
            assert parent.recv() == "acquired"
            with pytest.raises(OSError) as refusal:
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            assert refusal.value.errno in {errno.EACCES, errno.EDEADLK}
            parent.send("release")
            child.join(timeout)
            assert child.exitcode == 0, "contender did not exit cleanly"
        finally:
            if child.is_alive():
                child.kill()
                child.join(timeout)
            child.close()
