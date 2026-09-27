"""A holder record its owner marked released names nobody as the holder of a held device.

A `device_busy` refusal says who holds the device from the holder record beside
its lock: `holder`, `held_since`, `heartbeat_at` and the heartbeat age are read
from it, and so is whether the holder is this process. The record is written by
whoever takes the lock and rewritten `released` by the same owner when it lets
go, so a device nobody has taken since carries its last owner's released
record. When the lock is held again while the record on disk is still that
released one, because the new holder has not written its own yet or because the
lock is held by something that writes none, the refusal names the previous
owner as the one holding the device, with its old timestamps, and a heartbeat
that stopped when that owner let go reads as a holder that hung.

Only a record in state `held` says who holds the device. A released one leaves
every holder field out, and the summary says the current holder is not known.

The previous owner here took the device two hours ago and gave it back, the way
every owner does. The holders after it are the product's own: in a second
interpreter, a bare lifetime lock, held the way `probe_bus_lock` holds a bus
lock for the length of its probe, and the device mutex caught between taking
the lock and writing its record; in this process, the device mutex whose record
could not be written, a hold `_take` keeps.

Every text that tells an agent or an operator that the refusal names its
holder says, in the same words, what to do when it names none.
"""

from __future__ import annotations

import errno
import os
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from support import PUBLISH_ATOMICALLY_SOURCE, publish_atomically, scaled_time_bound
from test_bench_mutex import child_environment, holder_pid, wait_for_file
from test_holder_is_this_process import BOARD, lock_directory, refusal_to_a_contender_here

from agentic_hil import bench as bench_module
from agentic_hil.bench import BenchMutex
from agentic_hil.knowledge import LEASE_LIFECYCLE_URI, read_resource, remediation_fields

# Every field of a refusal that says who holds the device and how that holder is
# doing. Only a record in state `held` may supply any of them.
HOLDER_FIELDS = ("holder", "held_since", "heartbeat_at", "heartbeat_age_s", "holder_heartbeat_stale", "holder_is_this_process")

PREVIOUS_LABEL = "earlier-run"

# The field a named holder arrives in, spelled once and compared against the
# refusal and the texts, so a rename on either side fails here. Every text that
# promises a named holder carries this clause for a refusal that names none.
HOLDER_FIELD = "holder"
NO_HOLDER_CLAUSE = f"carries no `{HOLDER_FIELD}`"

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
TEXTS_THAT_PROMISE_A_NAMED_HOLDER = ("AGENTS.md", "TROUBLESHOOTING.md", "src/agentic_hil/skills/agentic-hil/SKILL.md", "plugins/agentic-hil/skills/agentic-hil/SKILL.md")

# Holds BOARD with the product's lifetime lock and nothing else, the way
# `probe_bus_lock` holds a bus lock while it probes: no record is written, so
# the one on disk stays whatever the last owner left.
LOCK_ONLY_SCRIPT = (
    PUBLISH_ATOMICALLY_SOURCE
    + """
import os, sys, time
from pathlib import Path
from agentic_hil.bench import _LifetimeLock, resource_digest
lock = _LifetimeLock(Path(sys.argv[1]) / (resource_digest(sys.argv[2]) + '.lock'))
lock.acquire()
publish_atomically(sys.argv[3], str(os.getpid()))
while not Path(sys.argv[4]).exists():
    time.sleep(0.02)
lock.release()
"""
)

# Takes BOARD through the product's own mutex and stops inside its first record
# write: the lock is held, and the record on disk is still the last owner's.
# The write goes ahead once the test is done, so the hold ends the way every
# hold does.
NOT_WRITTEN_YET_SCRIPT = (
    PUBLISH_ATOMICALLY_SOURCE
    + """
import os, sys, time
from pathlib import Path
from agentic_hil import bench
write = bench.atomic_write_text
paused = []
def write_once_the_test_is_done(*args, **kwargs):
    if not paused:
        paused.append(True)
        publish_atomically(sys.argv[3], str(os.getpid()))
        while not Path(sys.argv[4]).exists():
            time.sleep(0.02)
    write(*args, **kwargs)
bench.atomic_write_text = write_once_the_test_is_done
mutex = bench.BenchMutex(frontend='mcp', root=Path(sys.argv[1]))
mutex.acquire([sys.argv[2]])
mutex.release_all()
"""
)


def released_by_a_previous_owner(root: Path, monkeypatch: pytest.MonkeyPatch) -> BenchMutex:
    """BOARD taken and given back two hours ago, by an owner of its own. Its released record stays on disk."""
    two_hours_ago = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    previous = BenchMutex(frontend="cli", label=PREVIOUS_LABEL, root=root)
    with monkeypatch.context() as clock:
        clock.setattr(bench_module, "utc_now_iso", lambda: two_hours_ago)
        previous.acquire([BOARD])
        previous.release_all()
    return previous


@contextmanager
def held_by_another_process(tmp_path: Path, root: Path, script: str) -> Iterator[int]:
    """BOARD held by a second process running `script`. Yields that process's pid."""
    ready = tmp_path / "holder-ready"
    stop = tmp_path / "holder-stop"
    errors = tmp_path / "holder-stderr.txt"
    with errors.open("w", encoding="utf-8") as stderr:
        child = subprocess.Popen(
            [sys.executable, "-c", script, str(root), BOARD, str(ready), str(stop)],
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


def no_space_left(*_args: object, **_kwargs: object) -> None:
    raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))


def assert_names_nobody_as_the_holder(refusal: dict) -> None:
    named = {field: refusal[field] for field in HOLDER_FIELDS if refusal.get(field) is not None}
    assert not named, f"a released record was reported as the holder: {named}"
    assert PREVIOUS_LABEL not in refusal["summary"], refusal["summary"]
    assert "not known" in refusal["summary"], refusal["summary"]


@pytest.mark.parametrize("script", [LOCK_ONLY_SCRIPT, NOT_WRITTEN_YET_SCRIPT], ids=["held_by_a_lock_that_writes_no_record", "held_by_a_mutex_that_has_not_written_its_record"])
def test_a_released_record_under_a_lock_held_elsewhere_names_nobody_as_the_holder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, script: str) -> None:
    root = lock_directory(tmp_path)
    previous = released_by_a_previous_owner(root, monkeypatch)
    with held_by_another_process(tmp_path, root, script):
        record = previous.holder(BOARD)
        refusal = refusal_to_a_contender_here(root)

    # The premise: the device is held, and the record on disk is the one its
    # previous owner marked released.
    assert refusal["error_type"] == "device_busy", refusal
    assert record is not None and record["state"] == "released", record
    assert record["owner"] == previous.owner.as_json(), record
    assert_names_nobody_as_the_holder(refusal)


def test_a_released_record_under_a_lock_held_here_names_nobody_as_the_holder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The previous owner was this process, and this process holds the device
    again through a mutex whose record could not be written. The pid in the
    released record is this pid and the lock is in this process's lock table,
    which is everything `holder_is_this_process` asks, and the record still
    names an owner that let go two hours ago."""
    root = lock_directory(tmp_path)
    previous = released_by_a_previous_owner(root, monkeypatch)
    holder = BenchMutex(frontend="mcp", root=root)
    try:
        with monkeypatch.context() as disk:
            disk.setattr(bench_module, "atomic_write_text", no_space_left)
            holder.acquire([BOARD])
            held_here = holder.holds(BOARD)
            record = previous.holder(BOARD)
            refusal = refusal_to_a_contender_here(root)
    finally:
        holder.release_all()

    # The premise: this process holds the device, and the record on disk is the
    # one its previous owner, also this process, marked released.
    assert held_here
    assert refusal["error_type"] == "device_busy", refusal
    assert record is not None and record["state"] == "released", record
    assert record["owner"] == previous.owner.as_json(), record
    assert record["owner"]["pid"] == os.getpid(), record
    assert_names_nobody_as_the_holder(refusal)


def rest_of_the_sentence(text: str, clause: str) -> str | None:
    """The sentence in `text` from `clause` on, with the line breaks of a wrapped text undone."""
    flat = " ".join(text.split())
    start = flat.find(clause)
    if start < 0:
        return None
    end = flat.find(". ", start)
    return flat[start:] if end < 0 else flat[start : end + 1]


def test_every_text_that_promises_a_named_holder_says_what_to_do_when_the_refusal_carries_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = lock_directory(tmp_path)
    released_by_a_previous_owner(root, monkeypatch)
    with held_by_another_process(tmp_path, root, LOCK_ONLY_SCRIPT):
        refusal = refusal_to_a_contender_here(root)

    # The premise: the product's own refusal for a device that is held carries
    # no holder, and is as safe to retry as one that names it.
    assert refusal["error_type"] == "device_busy", refusal
    assert HOLDER_FIELD not in refusal, refusal
    assert refusal["retry_safe"] is True, refusal

    texts = {
        "the device_busy remediation": " ".join(remediation_fields(refusal["error_type"])["remediation"]),
        LEASE_LIFECYCLE_URI: read_resource(LEASE_LIFECYCLE_URI)["text"],
        **{name: (REPOSITORY_ROOT / name).read_text(encoding="utf-8") for name in TEXTS_THAT_PROMISE_A_NAMED_HOLDER},
    }
    sentences = {name: rest_of_the_sentence(text, NO_HOLDER_CLAUSE) for name, text in texts.items()}
    silent = [name for name, sentence in sentences.items() if sentence is None]
    assert not silent, f"these texts promise a named holder and say nothing about a refusal that {NO_HOLDER_CLAUSE}: {silent}"
    # And each says what to do then: wait for it, as for a holder that is named.
    no_advice = {name: sentence for name, sentence in sentences.items() if "wait" not in str(sentence).lower()}
    assert not no_advice, no_advice
    # The two copies of the skill say it in the same words.
    assert sentences["src/agentic_hil/skills/agentic-hil/SKILL.md"] == sentences["plugins/agentic-hil/skills/agentic-hil/SKILL.md"]
