"""A peer that fails the broker's handshake loses its own connection and nothing else.

Every connection to a CAN broker proves it holds the broker's key before its
first message is read: the broker sends a challenge, the peer answers it, and
the peer then challenges the broker. What a peer does inside that exchange is up
to the peer. It can leave after the challenge, answer with the wrong key, send
an attach without answering at all, or connect and say nothing. None of that may
end the broker, hold up a participant that does hold the key, or keep a stop
from reaching the broker, and a connection that never finishes the exchange is
dropped once the time the broker gives it has passed.

Each broker here runs in this process, so its serve loop can be watched and a
stop can be asked of it before that loop has started. The peers are
`multiprocessing.connection.Client` connections without a key, which connect
and then read and write the handshake's messages one at a time, the way a peer
that is not a participant would.
"""

from __future__ import annotations

import threading
import time
from multiprocessing.connection import AuthenticationError, Client, Connection
from pathlib import Path

import pytest
from support import scaled_time_bound
from test_can_broker import shared_config
from test_can_broker_owner import attach_outcome, serving

from agentic_hil import canbroker
from agentic_hil.canbroker import PROTOCOL_DIGEST, PROTOCOL_VERSION, CanBroker, read_descriptor

WRONG_KEY = b"not-the-brokers-key-at-all-32byt"


def broker_holding_the_bus(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[object, CanBroker]:
    """A config with a shared bus, and a broker in this process that holds that bus."""
    config = shared_config(tmp_path, monkeypatch)
    broker = CanBroker(config, "bench")
    broker.take_bus()
    return config, broker


def connect_without_a_key(broker: CanBroker) -> Connection:
    descriptor = read_descriptor(broker.bus_key, broker.lock_root)
    assert descriptor is not None
    return Client(descriptor.endpoint, descriptor.family)


def attach_within(config, participant: str, seconds: float) -> dict | None:
    """What `attach_outcome` makes of the attach, or None when it has not finished within `seconds`."""
    outcome: list[dict] = []
    attacher = threading.Thread(target=lambda: outcome.append(attach_outcome(config, participant)), daemon=True)
    attacher.start()
    attacher.join(timeout=seconds)
    return outcome[0] if outcome else None


def next_message(connection: Connection, seconds: float) -> bytes | None:
    """The next message on `connection` as raw bytes, or None when nothing arrives within `seconds`.

    Raises EOFError or OSError when the other end has closed. The wait is a poll,
    never a read that blocks, so a broker that never answers costs the test the
    wait and no thread."""
    if not connection.poll(seconds):
        return None
    return connection.recv_bytes()


def ended_within(connection: Connection, seconds: float) -> bool:
    """Whether the other end of `connection` closes it within `seconds`, reading past anything it still sends."""
    deadline = time.monotonic() + seconds
    while True:
        try:
            if next_message(connection, max(0.0, deadline - time.monotonic())) is None:
                return False
        except (EOFError, OSError):
            return True


def test_a_peer_that_leaves_after_the_challenge_does_not_end_the_broker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A peer that reads the challenge and closes its end: on the serve loop's
    thread that read its answer as the end of the broker, and every participant
    lost the bus with it. A participant that attaches afterwards is seated."""
    config, broker = broker_holding_the_bus(tmp_path, monkeypatch)
    try:
        with serving(broker):
            peer = connect_without_a_key(broker)
            try:
                challenge = next_message(peer, scaled_time_bound(5))
            finally:
                peer.close()
            outcome = attach_within(config, "alpha", scaled_time_bound(10))
    finally:
        broker.shutdown()
    assert challenge is not None and challenge.startswith(b"#CHALLENGE#"), challenge
    assert outcome is not None, "the attach after a peer left in the middle of the handshake never finished"
    assert outcome.get("ok") is True, outcome
    assert outcome["attached_participants"] == ["alpha"], outcome


def test_a_silent_peer_does_not_hold_up_an_attach(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A peer that connects and never answers the challenge keeps its own
    connection waiting and nobody else's: a participant is seated while that
    peer is still connected and still owes its answer. The time the broker
    gives a handshake is raised far above the attach's, so the attach cannot be
    waiting that time out."""
    monkeypatch.setattr(canbroker, "BROKER_HANDSHAKE_TIMEOUT_S", 600.0)
    config, broker = broker_holding_the_bus(tmp_path, monkeypatch)
    try:
        with serving(broker):
            silent = connect_without_a_key(broker)
            try:
                outcome = attach_within(config, "alpha", scaled_time_bound(10))
                challenge = next_message(silent, scaled_time_bound(5))
                still_waiting = next_message(silent, 0.2) is None
            finally:
                silent.close()
    finally:
        broker.shutdown()
    assert outcome is not None, "the attach waited for a peer that never answered the challenge"
    assert outcome.get("ok") is True, outcome
    assert challenge is not None and challenge.startswith(b"#CHALLENGE#"), challenge
    assert still_waiting, "the silent peer's connection had ended before the attach was seated"


def test_a_silent_peer_is_dropped_once_its_handshake_time_has_passed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The broker gives a handshake a bounded time: a peer that has not finished
    it by then has its connection closed, so a connection that says nothing does
    not hold a thread of the broker for the broker's whole life. The broker
    closes it no sooner than that time, and within a few seconds of it."""
    bound_s = 0.5
    monkeypatch.setattr(canbroker, "BROKER_HANDSHAKE_TIMEOUT_S", bound_s)
    config, broker = broker_holding_the_bus(tmp_path, monkeypatch)
    try:
        with serving(broker):
            started = time.monotonic()
            silent = connect_without_a_key(broker)
            try:
                ended = ended_within(silent, scaled_time_bound(bound_s + 5))
                took = time.monotonic() - started
            finally:
                silent.close()
            outcome = attach_within(config, "alpha", scaled_time_bound(10))
    finally:
        broker.shutdown()
    assert ended, "the broker kept a connection that never answered the challenge past the handshake's time"
    # The clock here reads in steps of up to a Windows tick, so the floor allows one.
    assert took >= bound_s - 0.05, took
    assert outcome is not None and outcome.get("ok") is True, outcome


def test_a_stop_reaches_a_broker_whose_serve_loop_has_not_looked_at_the_flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`stop` wakes the serve loop with a connection of its own. That connection
    has nothing to prove, so the stop returns at once even while nothing is
    accepting on the endpoint yet, and the loop, when it starts, finds the flag
    set and leaves."""
    _config, broker = broker_holding_the_bus(tmp_path, monkeypatch)
    stopper = threading.Thread(target=broker.stop, args=("test_finished",), daemon=True)
    server = threading.Thread(target=broker.serve, daemon=True)
    try:
        broker.publish()
        started = time.monotonic()
        stopper.start()
        stopper.join(timeout=scaled_time_bound(5))
        stop_took = time.monotonic() - started
        stop_returned = not stopper.is_alive()
        server.start()
        server.join(timeout=scaled_time_bound(5))
    finally:
        broker.shutdown()
        stopper.join(timeout=scaled_time_bound(10))
        server.join(timeout=scaled_time_bound(10))
    assert stop_returned, "the stop waited for a serve loop that was not accepting"
    assert stop_took < scaled_time_bound(2.0), stop_took
    assert not server.is_alive(), "the serve loop did not leave on a stop asked before it started"


def test_a_peer_without_the_key_is_never_seated_and_the_broker_goes_on(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A peer with the wrong key is refused by the handshake, and a peer that
    sends an attach without answering the challenge has that attach taken for a
    wrong answer: neither is ever read as a message, neither is seated, and a
    participant with the key is seated afterwards beside nobody."""
    config, broker = broker_holding_the_bus(tmp_path, monkeypatch)
    try:
        with serving(broker):
            descriptor = read_descriptor(broker.bus_key, broker.lock_root)
            assert descriptor is not None
            with pytest.raises(AuthenticationError):
                Client(descriptor.endpoint, descriptor.family, authkey=WRONG_KEY)
            intruder = connect_without_a_key(broker)
            try:
                intruder.send(
                    {
                        "message": "attach",
                        "protocol_version": PROTOCOL_VERSION,
                        "protocol_digest": PROTOCOL_DIGEST,
                        "expected_counter": descriptor.counter,
                        "bus_key": broker.bus_key,
                        "participant": "beta",
                        "requires_listen_only": False,
                        "client_pid": 1,
                    }
                )
                answers: list[bytes] = []
                deadline = time.monotonic() + scaled_time_bound(10)
                intruder_closed = False
                while not intruder_closed:
                    try:
                        answer = next_message(intruder, max(0.0, deadline - time.monotonic()))
                    except (EOFError, OSError):
                        intruder_closed = True
                        continue
                    if answer is None:
                        break
                    answers.append(answer)
            finally:
                intruder.close()
            outcome = attach_within(config, "alpha", scaled_time_bound(10))
    finally:
        broker.shutdown()
    assert intruder_closed, answers
    # The challenge, and at most the handshake's own refusal: never a reply to the attach.
    assert answers and answers[0].startswith(b"#CHALLENGE#"), answers
    assert all(answer.startswith((b"#CHALLENGE#", b"#FAILURE#")) for answer in answers), answers
    assert outcome is not None and outcome.get("ok") is True, outcome
    assert outcome["attached_participants"] == ["alpha"], outcome
