"""A holder record naming this pid is not proof that this process holds the device.

A `device_busy` refusal carries `holder_is_this_process: true` when the holder
record of the busy device names this process's pid. The record is written by
whichever process holds the device, into the lock directory every process on
the machine reads, and a pid names a process only inside one PID namespace and
one boot. A server in a second PID namespace that shares the lock directory (a
container with the same home directory mounted, where the first process is pid 1
in every one of them) holds a device under a number this process also has. A
contender here is then told the holder is itself: it looks for a session or a
run of its own to close, finds none, and never looks for the process that
really holds the device.

The other process here is a second interpreter that holds the device through
the product's own mutex and writes its own record. The one thing it is handed
is the number: its `os.getpid()` answers this process's pid, which is what a
second PID namespace can do and a test on one host otherwise cannot.

The positive case stays where it is pinned: a second session of this process on
a channel this process holds is told the holder is this process
(tests/test_can_session_holder.py, #501), and so is a second mutex in this
process meeting the first (tests/test_sessions_devices_coordination.py).
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from support import PUBLISH_ATOMICALLY_SOURCE, publish_atomically, scaled_time_bound
from test_bench_mutex import child_environment, holder_pid, wait_for_file

from agentic_hil.bench import BenchMutex, DeviceBusyError, resource_digest

BOARD = "physical:bench-board"

# The keys of a holder record as released versions write it (`_write_holder`
# and `BenchOwner.as_json`), where nothing but the pid, and the host beside it,
# says which process wrote it. A server that has not been upgraded still writes
# exactly these into the same directory.
PID_ONLY_RECORD_KEYS = frozenset({"version", "state", "resource", "owner", "acquired_at", "heartbeat_at", "heartbeat_interval_s", "released_at", "reclaimed_from"})
PID_ONLY_OWNER_KEYS = frozenset({"owner_id", "pid", "host", "frontend", "label"})

# Holds BOARD through the product's own mutex, under the frontend the contender
# here uses as well, so the pid is not the only thing the two have in common.
# `os.getpid` answers the number it is handed before anything is imported, the
# way it would inside a second PID namespace; the real pid is what it publishes.
HOLDER_SCRIPT = (
    PUBLISH_ATOMICALLY_SOURCE
    + """
import os, sys, time
from pathlib import Path
real_pid = os.getpid()
namespace_pid = int(sys.argv[1])
os.getpid = lambda: namespace_pid
from agentic_hil.bench import BenchMutex
mutex = BenchMutex(frontend='mcp', root=Path(sys.argv[2]))
mutex.acquire([sys.argv[3]])
publish_atomically(sys.argv[4], str(real_pid))
while not Path(sys.argv[5]).exists():
    time.sleep(0.02)
mutex.release_all()
"""
)


def lock_directory(tmp_path: Path) -> Path:
    """A lock directory of this test's own, so the only holder it can meet is the one it starts."""
    root = tmp_path / "device-locks"
    root.mkdir()
    return root


@contextmanager
def held_by_another_process(tmp_path: Path, root: Path, *, pid: int) -> Iterator[int]:
    """BOARD held by a second process whose pid reads as `pid`. Yields that process's real pid."""
    ready = tmp_path / "holder-ready"
    stop = tmp_path / "holder-stop"
    errors = tmp_path / "holder-stderr.txt"
    with errors.open("w", encoding="utf-8") as stderr:
        child = subprocess.Popen(
            [sys.executable, "-c", HOLDER_SCRIPT, str(pid), str(root), BOARD, str(ready), str(stop)],
            env=child_environment(tmp_path / "holder-state"),
            stderr=stderr,
        )
        try:
            assert wait_for_file(ready, child, scaled_time_bound(20)), f"the other process never took {BOARD}: {errors.read_text(encoding='utf-8')}"
            yield holder_pid(ready)
        finally:
            publish_atomically(str(stop), "stop")
            try:
                child.wait(timeout=scaled_time_bound(20))
            finally:
                if child.poll() is None:
                    child.kill()
                    child.wait(timeout=scaled_time_bound(20))


def refusal_to_a_contender_here(root: Path) -> dict:
    contender = BenchMutex(frontend="mcp", root=root)
    with pytest.raises(DeviceBusyError) as busy:
        contender.acquire([BOARD])
    return busy.value.result


def test_a_holder_in_another_process_under_this_pid_is_not_this_process(tmp_path: Path) -> None:
    root = lock_directory(tmp_path)
    with held_by_another_process(tmp_path, root, pid=os.getpid()) as holder:
        refusal = refusal_to_a_contender_here(root)

    # The premise: another process holds the device, and its record names this pid.
    assert holder != os.getpid()
    assert refusal["error_type"] == "device_busy", refusal
    assert refusal["holder"]["pid"] == os.getpid(), refusal
    # The holder is not this process, so the refusal must not say it is.
    assert "holder_is_this_process" not in refusal, refusal


def test_a_record_that_names_its_writer_only_by_pid_and_host_is_not_this_process(tmp_path: Path) -> None:
    """A record in the shape released versions write, from another process with this pid.

    Every field in it that this process can check agrees with this process: the
    pid, the host and the frontend. The one that does not is the owner id the
    other process minted for itself, and that is enough: a record that carries
    nothing to tell its writer from this process must not read as this process
    on the pid (and the host) alone."""
    root = lock_directory(tmp_path)
    with held_by_another_process(tmp_path, root, pid=os.getpid()):
        record_path = root / f"{resource_digest(BOARD)}.holder.json"
        record = json.loads(record_path.read_text(encoding="utf-8"))
        pid_only = {key: value for key, value in record.items() if key in PID_ONLY_RECORD_KEYS}
        pid_only["owner"] = {key: value for key, value in record["owner"].items() if key in PID_ONLY_OWNER_KEYS}
        record_path.write_text(json.dumps(pid_only, indent=2) + "\n", encoding="utf-8")
        refusal = refusal_to_a_contender_here(root)

    # The premise: the refusal was read from that record, and it names this
    # process's pid, host and frontend.
    assert refusal["holder"] == pid_only["owner"], refusal
    assert refusal["holder"]["pid"] == os.getpid(), refusal
    assert refusal["holder"]["host"] == socket.gethostname(), refusal
    assert refusal["holder"]["frontend"] == "mcp", refusal
    assert "holder_is_this_process" not in refusal, refusal
