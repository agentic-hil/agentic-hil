"""Windows pipe ownership with independent file roots and real kernel peers.

These are local mechanism proofs. They do not stand in for two Windows
accounts, a service, an MSIX host, a remote machine or an attached probe.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

import pytest
from support import scaled_time_bound
from windows_lock_support import (
    FILE_ALL_ACCESS,
    FILE_CREATE_PIPE_INSTANCE,
    FILE_GENERIC_READ,
    GENERIC_READ,
    READ_CONTROL,
    NativePipe,
    peer,
)

from agentic_hil import windows_device_lock
from agentic_hil.bench import BenchMutex, DeviceBusyError, _LifetimeLock, fold_resource_name, resource_digest

ON_WINDOWS = pytest.mark.skipif(os.name != "nt", reason="native device-lock pipes require Windows")
BOARD = "physical:pipe-test-board"


def lock_root(tmp_path: Path, name: str = "contender-locks") -> Path:
    root = tmp_path / name
    root.mkdir()
    return root


def busy(owner: BenchMutex, resource: str = BOARD) -> dict:
    with pytest.raises(DeviceBusyError) as refused:
        owner.acquire([resource], wait_s=0)
    assert refused.value.result["error_type"] == "device_busy"
    assert owner.held_resources() == frozenset()
    return refused.value.result


@ON_WINDOWS
def test_pipe_only_holder_refuses_and_returns_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = lock_root(tmp_path)
    endpoint = windows_device_lock.pipe_name(resource_digest(BOARD), root)
    monkeypatch.setattr(windows_device_lock, "ANSWER_TIMEOUT_S", scaled_time_bound(20))
    owner = BenchMutex(root=root)
    try:
        with peer(tmp_path / "raw", "raw", endpoint=endpoint, resource=BOARD, answer=True) as holder:
            holder.wait_for_marker("held")
            result = busy(owner)
            assert result["holder"]["pid"] == holder.pid, result
            assert result["holder"]["pid"] != -885
            with peer(tmp_path / "file-probe", "file", path=str(root / f"{resource_digest(BOARD)}.lock")) as probe:
                assert probe.wait_for_marker("result") == {"acquired": True}
                probe.reap()
            holder.send("release")
            holder.wait_for_marker("released")
            holder.reap()
    finally:
        owner.release_all()


@ON_WINDOWS
@pytest.mark.parametrize("exit_mode", ["release", "death"], ids=["release", "death"])
def test_next_acquire_after_holder_exit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, exit_mode: str) -> None:
    root = lock_root(tmp_path)
    endpoint = windows_device_lock.pipe_name(resource_digest(BOARD), root)
    monkeypatch.setattr(windows_device_lock, "ANSWER_TIMEOUT_S", scaled_time_bound(20))
    owner = BenchMutex(root=root)
    try:
        with peer(tmp_path / "holder", "product", endpoint=endpoint, resource=BOARD,
                  root=str(tmp_path / "holder-locks")) as holder:
            holder.wait_for_marker("held")
            assert busy(owner)["holder"]["pid"] == holder.pid
            if exit_mode == "release":
                holder.send("release")
                holder.wait_for_marker("released")
                holder.reap()
            else:
                holder.kill()
            assert owner.acquire([BOARD], wait_s=0) == [BOARD]
            assert owner.holds(BOARD)
    finally:
        owner.release_all()


@ON_WINDOWS
def test_reader_does_not_keep_released_name(tmp_path: Path) -> None:
    root = lock_root(tmp_path)
    endpoint = windows_device_lock.pipe_name(resource_digest(BOARD), root)
    native = NativePipe()
    owner = BenchMutex(root=root)
    try:
        with peer(tmp_path / "holder", "product", endpoint=endpoint, resource=BOARD,
                  root=str(tmp_path / "holder-locks")) as holder:
            holder.wait_for_marker("held")
            reader = native.open_reader(endpoint)
            try:
                # Only a successful native open permits the release request.
                holder.send("release")
                holder.wait_for_marker("released")
                assert owner.acquire([BOARD], wait_s=0) == [BOARD]
            finally:
                native.close(reader)
            holder.reap()
    finally:
        owner.release_all()


@ON_WINDOWS
def test_file_only_contender_is_refused(tmp_path: Path) -> None:
    root = lock_root(tmp_path)
    owner = BenchMutex(root=root)
    try:
        owner.acquire([BOARD])
        with peer(tmp_path / "old-peer", "file", path=str(root / f"{resource_digest(BOARD)}.lock")) as old:
            assert old.wait_for_marker("result") == {"acquired": False}
            old.reap()
    finally:
        owner.release_all()


@ON_WINDOWS
def test_pipe_dacl_grants_only_holder_and_authenticated_read(tmp_path: Path) -> None:
    root = lock_root(tmp_path)
    endpoint = windows_device_lock.pipe_name(resource_digest(BOARD), root)
    native = NativePipe()
    owner = BenchMutex(root=root)
    try:
        owner.acquire([BOARD])
        reader = native.open_reader(endpoint, access=GENERIC_READ | READ_CONTROL)
        try:
            entries = native.dacl(reader)
            sid = native.token_user_sid()
            assert len(entries) == 2, entries
            assert set(entries) == {(0, 0, FILE_ALL_ACCESS, sid), (0, 0, FILE_GENERIC_READ, "S-1-5-11")}, entries
            authenticated = next(mask for _, _, mask, trustee in entries if trustee == "S-1-5-11")
            assert not authenticated & FILE_CREATE_PIPE_INSTANCE
        finally:
            native.close(reader)
    finally:
        owner.release_all()


@ON_WINDOWS
def test_record_answers_while_guard_is_held(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = lock_root(tmp_path)
    endpoint = windows_device_lock.pipe_name(resource_digest(BOARD), root)
    monkeypatch.setattr(windows_device_lock, "ANSWER_TIMEOUT_S", scaled_time_bound(20))
    native = NativePipe()
    owner = BenchMutex(root=root)
    try:
        with peer(tmp_path / "holder", "product", endpoint=endpoint, resource=BOARD,
                  root=str(tmp_path / "holder-locks"), guard=True) as holder:
            original = holder.wait_for_marker("held")
            holder.wait_for_marker("guard-held")
            result = busy(owner)
            assert result["holder"]["pid"] == holder.pid, result
            assert not result.get("holder_heartbeat_stale"), result
            before = datetime.now(timezone.utc)
            reader = native.open_reader(endpoint)
            try:
                record = native.read_record(reader)
            finally:
                native.close(reader)
            after = datetime.now(timezone.utc)
            assert set(record) == set(original)
            assert {key: value for key, value in record.items() if key != "heartbeat_at"} == {
                key: value for key, value in original.items() if key != "heartbeat_at"
            }
            heartbeat = datetime.fromisoformat(record["heartbeat_at"].replace("Z", "+00:00"))
            # Millisecond serialization rounds down within the opening millisecond.
            assert int(before.timestamp() * 1000) <= int(heartbeat.timestamp() * 1000) <= int(after.timestamp() * 1000)
            assert record["heartbeat_at"] != original["heartbeat_at"]
            # No release signal was sent while either answer was being observed.
            holder.send("release")
            holder.wait_for_marker("released")
            holder.reap()
    finally:
        owner.release_all()


@ON_WINDOWS
def test_connected_nonanswering_holder_is_stale(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = lock_root(tmp_path)
    endpoint = windows_device_lock.pipe_name(resource_digest(BOARD), root)
    monkeypatch.setattr(windows_device_lock, "ANSWER_TIMEOUT_S", scaled_time_bound(0.25))
    owner = BenchMutex(root=root)
    try:
        with peer(tmp_path / "silent", "raw", endpoint=endpoint, resource=BOARD, answer=False) as holder:
            holder.wait_for_marker("held")
            result = busy(owner)
            assert result["holder_heartbeat_stale"] is True, result
            assert result["holder"]["pid"] == holder.pid, result
            holder.send("release")
            holder.wait_for_marker("released")
            holder.reap()
    finally:
        owner.release_all()


@ON_WINDOWS
def test_rolled_back_tentative_pipe_is_not_stale(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    first, later = "com:a", "com:b"
    root = lock_root(tmp_path)
    endpoint = windows_device_lock.pipe_name(resource_digest(first), root)
    monkeypatch.setattr(windows_device_lock, "ANSWER_TIMEOUT_S", scaled_time_bound(20))
    requester_root = lock_root(tmp_path, "requester-locks")
    blocker = _LifetimeLock(requester_root / f"{resource_digest(later)}.lock")
    owner = BenchMutex(root=root)
    try:
        blocker.acquire()
        with peer(tmp_path / "requester", "tentative", endpoint=endpoint, resource=first,
                  later=later, root=str(requester_root)) as requester:
            requester.wait_for_marker("tentative")
            open_reader = windows_device_lock._open_reader

            def connected(name: str) -> int:
                handle = open_reader(name)
                requester.send("reader-connected")
                return handle

            monkeypatch.setattr(windows_device_lock, "_open_reader", connected)
            result = busy(owner, first)
            rolled_back = requester.wait_for_marker("rolled-back")
            assert rolled_back["held"] == [], rolled_back
            assert rolled_back["result"]["error_type"] == "device_busy", rolled_back
            assert not result.get("holder"), result
            assert not result.get("holder_heartbeat_stale"), result
            requester.reap()
    finally:
        owner.release_all()
        blocker.release()


def test_pipe_name_respects_profile_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    canonical = tmp_path / "token-profile" / ".agentic-hil" / "device-locks"
    monkeypatch.setattr(windows_device_lock, "profile_device_lock_root", lambda: canonical)
    digest = resource_digest(fold_resource_name("physical:PIPE-TEST-BOARD"))
    production = windows_device_lock.pipe_name(digest, canonical)
    assert production == rf"\\.\pipe\agentic-hil-{digest}"
    alternative = tmp_path / "redirected-profile" / "device-locks"
    scoped = windows_device_lock.pipe_name(digest, alternative)
    assert scoped != production
    assert scoped == windows_device_lock.pipe_name(digest, alternative)
