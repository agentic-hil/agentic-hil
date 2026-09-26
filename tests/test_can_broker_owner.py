"""The endpoint a CAN participant attaches to is the broker that holds the bus, or it is refused.

Before a participant is seated, `probe_bus_lock` asks whether the endpoint the
broker descriptor names really owns the bus. The bus lock says whether anybody
holds the bus, and the holder record beside it says who: the endpoint is
accepted when the record's pid is the pid the descriptor claims and the
record's host is this host. A pid names a process only inside one PID namespace
and one boot. Two containers that share the lock directory and the host name
(host networking hands every container the host's name) can each have a
process under the same small number, so a record one broker wrote vouches for
an endpoint that is not that broker.

The bus here is held by a real broker in a second interpreter, through the
broker's own `take_bus`, and the one thing that process is handed is the
number: its `os.getpid()` answers this process's pid, which is what a second
PID namespace can do and a test on one host otherwise cannot. The endpoint is a
real broker in this process that never took the bus. It publishes its
descriptor and answers the handshake the way a broker does, and the one thing
wrong with it is that it is not the process that wrote the record.

Beside it, the broker that does hold the bus is still accepted: the one a
participant starts, by a second participant that finds it running, and a broker
whose heartbeat has rewritten its record since it published. A broker that names
itself by pid alone, the way release 0.21.5 does, is refused even while it holds
the bus: nothing it publishes tells it apart from a stranger under the same pid,
so a client meeting one fails closed.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from support import PUBLISH_ATOMICALLY_SOURCE, publish_atomically, scaled_time_bound
from test_bench_mutex import child_environment, holder_pid, wait_for_file
from test_can_broker import broker_diagnostics, reaped_brokers, shared_config  # noqa: F401  (fixture)

from agentic_hil.canbroker import (
    CanBroker,
    ParticipantError,
    attach_participant,
    bus_lock_key,
    descriptor_path,
    read_descriptor,
)
from agentic_hil.config import atomic_write_text

# A broker descriptor exactly as release 0.21.5 writes it: these keys and no
# others, its descriptor and protocol versions, and the digest of its message
# surface. The one thing in it that names the broker is `pid`.
PID_ONLY_DESCRIPTOR_KEYS = ("version", "bus_key", "endpoint", "family", "pid", "protocol_version", "protocol_digest", "counter", "started_at")
PID_ONLY_RELEASE = {"version": 1, "protocol_version": 1, "protocol_digest": "112df16f33592fe0"}

# Holds the bus the way a broker does, through `CanBroker.take_bus`, and does
# nothing else: it publishes no descriptor and answers no handshake, so the one
# trace it leaves is the holder record its own mutex writes. `os.getpid`
# answers the number it is handed before anything is imported, the way it would
# inside a second PID namespace; the real pid is what it publishes.
BUS_HOLDER_SCRIPT = (
    PUBLISH_ATOMICALLY_SOURCE
    + """
import os, sys, time
from pathlib import Path
real_pid = os.getpid()
namespace_pid = int(sys.argv[1])
os.getpid = lambda: namespace_pid
from agentic_hil.canbroker import CanBroker
from agentic_hil.config import load_authoritative_config
broker = CanBroker(load_authoritative_config(sys.argv[2]), sys.argv[3])
broker.take_bus()
publish_atomically(sys.argv[4], str(real_pid))
while not Path(sys.argv[5]).exists():
    time.sleep(0.02)
broker.mutex.release_all()
"""
)


@contextmanager
def bus_held_by_another_broker(tmp_path: Path, config, *, pid: int) -> Iterator[int]:
    """The bus held by a broker in a second process whose pid reads as `pid`. Yields that process's real pid."""
    ready = tmp_path / "bus-holder-ready"
    stop = tmp_path / "bus-holder-stop"
    errors = tmp_path / "bus-holder-stderr.txt"
    with errors.open("w", encoding="utf-8") as stderr:
        child = subprocess.Popen(
            [sys.executable, "-c", BUS_HOLDER_SCRIPT, str(pid), str(config.work_dir), "bench", str(ready), str(stop)],
            env=child_environment(tmp_path / "bus-holder-state"),
            stderr=stderr,
        )
        try:
            assert wait_for_file(ready, child, scaled_time_bound(20)), f"the other broker never took the bus: {errors.read_text(encoding='utf-8')}"
            yield holder_pid(ready)
        finally:
            publish_atomically(str(stop), "stop")
            try:
                child.wait(timeout=scaled_time_bound(20))
            finally:
                if child.poll() is None:
                    child.kill()
                    child.wait(timeout=scaled_time_bound(20))


@contextmanager
def serving(broker: CanBroker) -> Iterator[CanBroker]:
    """`broker` published and answering on its endpoint until the block ends."""
    broker.publish()
    server = threading.Thread(target=broker.serve, daemon=True)
    server.start()
    try:
        yield broker
    finally:
        broker.stop("test_finished")
        server.join(timeout=scaled_time_bound(10))
        broker.shutdown()


def attach_outcome(config, participant: str) -> dict:
    """What an attach that may not start a broker makes of the endpoint the descriptor names.

    The broker's `attached` answer when the participant is seated, detached again
    at once so nothing it holds outlives the question, or the refusal."""
    try:
        seated = attach_participant(config, "bench", participant, allow_start=False, start_timeout_s=5.0)
    except ParticipantError as refused:
        return refused.result
    seated.detach()
    return seated.attached


def published_as_the_pid_only_release(broker: CanBroker) -> None:
    """Rewrite `broker`'s descriptor into the one release 0.21.5 publishes for the same broker."""
    path = descriptor_path(broker.bus_key, broker.lock_root)
    published = json.loads(path.read_text(encoding="utf-8"))
    pid_only = {key: published[key] for key in PID_ONLY_DESCRIPTOR_KEYS}
    pid_only.update(PID_ONLY_RELEASE)
    atomic_write_text(path, json.dumps(pid_only, indent=2) + "\n")


def test_an_endpoint_under_the_pid_of_the_bus_holder_is_not_the_bus_owner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = shared_config(tmp_path, monkeypatch)
    bus_key = bus_lock_key(config, "bench")
    with bus_held_by_another_broker(tmp_path, config, pid=os.getpid()) as holder:
        endpoint = CanBroker(config, "bench")
        with serving(endpoint):
            record = endpoint.mutex.holder(bus_key)
            claimed = read_descriptor(bus_key, endpoint.lock_root)
            outcome = attach_outcome(config, "alpha")

    # The premise: another process holds the bus and wrote the record as a
    # broker, and the record names the pid the endpoint claims, on this host.
    assert holder != os.getpid()
    assert record is not None and record["state"] == "held", record
    assert record["owner"]["frontend"] == "can-broker", record
    assert claimed is not None and claimed.pid == os.getpid(), claimed
    assert record["owner"]["pid"] == claimed.pid, record
    assert record["owner"]["host"] == socket.gethostname(), record
    # The endpoint is not the process that wrote that record, so it is not the
    # bus owner, however well it answers.
    assert outcome.get("error_type") == "can_broker_not_bus_owner", outcome
    assert outcome.get("bus_lock_held") is True, outcome


def test_the_broker_a_participant_starts_is_accepted_as_the_bus_owner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reaped_brokers: list[subprocess.Popen]) -> None:  # noqa: F811
    """Both attaches pass the owner check: the first against the broker it
    started, once that broker has published, and the second against the broker
    it finds running, whose descriptor the first attach has rewritten."""
    config = shared_config(tmp_path, monkeypatch)
    bus_key = bus_lock_key(config, "bench")
    try:
        alpha = attach_participant(config, "bench", "alpha")
    except ParticipantError as error:  # pragma: no cover (only on a broken broker)
        pytest.fail(f"{error.result}\n{broker_diagnostics(config, bus_key)}")
    try:
        beta = attach_outcome(config, "beta")
    finally:
        alpha.detach()
        alpha.broker_process.wait(timeout=scaled_time_bound(30))
    assert beta.get("ok") is True, f"{beta}\n{broker_diagnostics(config, bus_key)}"
    assert beta["broker_pid"] == alpha.broker_pid, beta


def test_the_broker_is_still_the_bus_owner_after_its_heartbeat_rewrote_the_record(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A broker's mutex rewrites its holder record on every heartbeat for as
    long as it holds the bus, so whatever the record says about the broker has
    to survive that rewrite, or a participant that arrives after the first
    interval finds the broker that holds the bus refused as a stranger. The
    broker is in this process so its heartbeat can be driven rather than waited
    out."""
    config = shared_config(tmp_path, monkeypatch)
    broker = CanBroker(config, "bench")
    broker.take_bus()
    try:
        with serving(broker):
            broker.mutex.heartbeat()
            outcome = attach_outcome(config, "alpha")
    finally:
        broker.shutdown()
    assert outcome.get("ok") is True, outcome
    assert outcome["broker_pid"] == os.getpid(), outcome


def test_a_broker_that_names_itself_by_pid_alone_is_refused_even_while_it_holds_the_bus(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A broker of release 0.21.5 names itself by pid alone, in its descriptor
    and in its handshake. A client cannot tell it from an endpoint that only
    shares the bus holder's pid, so it does not accept it on the pid: the pair
    fails closed. The broker here does hold the bus; only its descriptor is the
    one that release publishes."""
    config = shared_config(tmp_path, monkeypatch)
    broker = CanBroker(config, "bench")
    broker.take_bus()
    try:
        with serving(broker):
            published_as_the_pid_only_release(broker)
            outcome = attach_outcome(config, "alpha")
    finally:
        broker.shutdown()
    assert outcome.get("error_type") in {"can_broker_not_bus_owner", "can_broker_protocol_mismatch"}, outcome
