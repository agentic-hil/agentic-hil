"""A shared CAN bus has one owner and named participants; a single-owner bus is untouched.

Two halves, and the first one is load-bearing for the second. Everything a
``can_buses`` entry without ``shares:`` says about itself is pinned here against
literals captured before the broker existed, because the whole argument for
adding participants is that they cost the existing model nothing. A byte that
moves in that status is the feature failing, whatever else passes.

The second half runs real brokers in real child processes over the real local IPC
transport, with the real ``adapter: process`` path behind them and a scripted
bridge child at the bottom (``tests/fixtures/fake_can_bridge.py``). No hardware
and no broker stub: the broker's whole job is being the one process on the bus,
and a broker that is not a process cannot be shown to be that.
"""
from __future__ import annotations

import dataclasses
import json
import os
import subprocess
import threading
import time
from collections.abc import Iterator
from multiprocessing.connection import AuthenticationError, Client, Listener
from pathlib import Path

import pytest
from conftest import write_authoritative_config, write_config

from agentic_hil import canbroker
from agentic_hil.can import CanBusService
from agentic_hil.canbroker import (
    PROTOCOL_DIGEST,
    PROTOCOL_VERSION,
    ParticipantError,
    attach_participant,
    authkey_path,
    bus_lock_key,
    descriptor_path,
    endpoint_address,
    endpoint_family,
    read_descriptor,
)
from agentic_hil.config import load_config
from agentic_hil.devices import can_device
from agentic_hil.redact import redact_sensitive

FIXTURES = Path(__file__).resolve().parent / "fixtures"
FAKE_CAN_BRIDGE = FIXTURES / "fake_can_bridge.py"


# ---------------------------------------------------------------------------
# The single-owner bus, pinned.

SINGLE_OWNER_BUS_YAML = (
    "can_buses:\n"
    "  bench:\n"
    '    adapter: "peak"\n'
    '    channel: "PCAN_USBBUS1"\n'
    "    bitrate: 500000\n"
    "    permissions:\n"
    "      allow_read: true\n"
    "      allow_write: true\n"
)

# Captured from the tree before `shares:` existed, by serializing
# `CanBusService.list_buses()["buses"]["bench"]` with sorted keys. This is the
# operator-facing description of a bus, and for an entry that declares no shares
# it must still be exactly these bytes: not a superset, not a reordering, not a
# new key that happens to be inert.
SINGLE_OWNER_STATUS_JSON = '{"adapter": "peak", "bitrate": 500000, "channel": "PCAN_USBBUS1", "fd": false, "listen_only": false, "listen_only_enforcement": "driver_verified", "max_buffer_frames": 1024, "max_frame_data_bytes": 8, "session_active": false}'

# The same capture for the parsed entry itself. Compared below against this dict
# plus *exactly* the two keys shares brought, so the assertion says what a
# share-less entry gained — two inert defaults — and would fail on anything else
# that moved.
SINGLE_OWNER_ENTRY_JSON = '{"adapter": "peak", "args": [], "bitrate": 500000, "channel": "PCAN_USBBUS1", "data_bitrate": null, "executable": null, "fd": false, "listen_only": false, "max_buffer_frames": 1024, "max_frame_data_bytes": 8, "pcanbasic_dll": null, "permissions": {"allow_read": true, "allow_write": true}, "poll_interval_ms": 10, "receive_own_messages": false, "resource_id": null, "timeout_s": 10.0}'


def single_owner_config(tmp_path: Path):
    return load_config(str(write_config(tmp_path, can_buses_yaml=SINGLE_OWNER_BUS_YAML)))


def test_single_owner_bus_status_is_byte_identical(tmp_path: Path) -> None:
    config = single_owner_config(tmp_path)
    service = CanBusService(config)
    try:
        listed = service.list_buses()
    finally:
        service.close()
    assert json.dumps(listed["buses"]["bench"], sort_keys=True) == SINGLE_OWNER_STATUS_JSON


def test_single_owner_entry_gains_only_the_two_inert_share_defaults(tmp_path: Path) -> None:
    config = single_owner_config(tmp_path)
    expected = {**json.loads(SINGLE_OWNER_ENTRY_JSON), "listen_only_enforcement": "controller", "shares": {}}
    parsed = dataclasses.asdict(config.can_buses["bench"])
    assert json.dumps(parsed, sort_keys=True, default=str) == json.dumps(expected, sort_keys=True, default=str)


def test_single_owner_lock_key_is_unchanged(tmp_path: Path) -> None:
    config = single_owner_config(tmp_path)
    assert can_device(config, "bench").lock_key == "can:peak:pcan_usbbus1"
    assert bus_lock_key(config, "bench") == "can:peak:pcan_usbbus1"


def test_single_owner_bus_has_no_participants_and_starts_no_broker(tmp_path: Path) -> None:
    config = single_owner_config(tmp_path)
    with pytest.raises(ParticipantError) as refusal:
        attach_participant(config, "bench", "alpha")
    assert refusal.value.result["error_type"] == "can_bus_not_shared"
    assert not descriptor_path(bus_lock_key(config, "bench"), Path(os.path.expanduser("~")) / ".agentic-hil" / "device-locks").exists()


@pytest.mark.parametrize("share_name", ["permissions", "provenance", "allow_write", "allow_anything"])
def test_a_share_may_not_be_named_after_a_configuration_block_or_a_grant(tmp_path: Path, share_name: str) -> None:
    """`configwrite.permission_surface` is name-driven, and both its arms collide here.

    It reads a mapping called `permissions` as grants at any depth, and any key
    beginning with `allow_` as a grant unless it is one level below a named
    section — which a share name is not. Either spelling would have the walker
    reading a participant view as a permission."""
    from agentic_hil.config import ConfigError

    yaml_text = SINGLE_OWNER_BUS_YAML + f"    shares:\n      {share_name}:\n        max_frames: 4\n"
    with pytest.raises(ConfigError) as refusal:
        load_config(str(write_config(tmp_path, can_buses_yaml=yaml_text)))
    assert refusal.value.error_type == "config_invalid"


def test_a_participant_grant_is_governed_by_the_permissions_right(tmp_path: Path) -> None:
    """A share's `permissions:` are grants, and the ratchet has to see them as grants.

    A participant that may transmit is a bus that may be written, whatever it is
    called. If the permission walk missed a share's block, a run could be given
    the medium by a description-level edit — the exact thing the two-right split
    exists to stop."""
    from agentic_hil.configwrite import permission_surface

    document = {
        "can_buses": {
            "bench": {
                "adapter": "peak",
                "permissions": {"allow_read": True, "allow_write": False},
                "shares": {"alpha": {"max_frames": 16, "permissions": {"allow_read": True, "allow_write": True}}},
            }
        }
    }
    surface = permission_surface(document)

    assert surface["can_buses.bench.shares.alpha.permissions.allow_write"] is True
    assert surface["can_buses.bench.shares.alpha.permissions.allow_read"] is True
    # The bus's own block is still its own, so a participant grant cannot be
    # mistaken for the bus being writable or the other way round.
    assert surface["can_buses.bench.permissions.allow_write"] is False


# ---------------------------------------------------------------------------
# A brokered bus, end to end.


def shared_bus_yaml(*, listen_only: bool = False, enforcement: str = "controller", shares: str = "") -> str:
    return (
        "can_buses:\n"
        "  bench:\n"
        '    adapter: "process"\n'
        '    channel: "vcan0"\n'
        f'    executable: "{FAKE_CAN_BRIDGE.as_posix()}"\n'
        f"    listen_only: {str(listen_only).lower()}\n"
        f'    listen_only_enforcement: "{enforcement}"\n'
        "    timeout_s: 5.0\n"
        "    shares:\n" + (shares or DEFAULT_SHARES)
    )


DEFAULT_SHARES = (
    "      alpha:\n"
    "        max_frames: 16\n"
    "        permissions:\n"
    "          allow_read: true\n"
    "          allow_write: true\n"
    "      beta:\n"
    "        max_frames: 16\n"
    "        permissions:\n"
    "          allow_read: true\n"
    "          allow_write: true\n"
)


@pytest.fixture
def reaped_brokers(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[subprocess.Popen]]:
    """Never leave a broker behind, whatever a failing assertion skipped."""
    started: list[subprocess.Popen] = []
    original = canbroker._spawn_broker

    def spawn(*args, **kwargs):
        child = original(*args, **kwargs)
        started.append(child)
        return child

    monkeypatch.setattr(canbroker, "_spawn_broker", spawn)
    yield started
    for child in started:
        if child.poll() is None:
            child.terminate()
        try:
            child.wait(timeout=15)
        except subprocess.TimeoutExpired:
            child.kill()


def shared_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **kwargs):
    from agentic_hil.config import load_authoritative_config

    workspace = tmp_path / "project"
    write_authoritative_config(workspace, monkeypatch, can_buses_yaml=shared_bus_yaml(**kwargs))
    return load_authoritative_config(workspace)


def broker_diagnostics(config, bus_key: str) -> str:
    from agentic_hil.bench import BenchMutex

    log = canbroker.broker_log_path(bus_key, BenchMutex().root)
    return log.read_text(encoding="utf-8", errors="replace") if log.exists() else "<no broker log>"


def test_broker_starts_on_first_attach_and_exits_on_last_detach(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reaped_brokers: list[subprocess.Popen]) -> None:
    config = shared_config(tmp_path, monkeypatch)
    bus_key = bus_lock_key(config, "bench")
    try:
        alpha = attach_participant(config, "bench", "alpha")
    except ParticipantError as error:  # pragma: no cover - only on a broken broker
        pytest.fail(f"{error.result}\n{broker_diagnostics(config, bus_key)}")
    assert alpha.broker_process is not None, "the first participant is the one that started the broker"
    assert alpha.broker_process.poll() is None, "and it is still running while the participant is attached"
    lock_root = alpha.mutex.root
    # Deliberately not `broker_pid == broker_process.pid`. A venv `python.exe` may
    # be a launcher trampoline that execs the real interpreter as a separate
    # process, so the pid the parent gets from `Popen` is the launcher's, not the
    # broker's. Every pid that matters is one the broker wrote about itself: the
    # descriptor and the bus-lock holder record both come from its `os.getpid()`,
    # and the probe compares those two. Agreement between them is the identity
    # this feature rests on, so that is what is checked.
    assert descriptor_path(bus_key, lock_root).exists()
    assert read_descriptor(bus_key, lock_root).pid == alpha.broker_pid
    holder = json.loads((lock_root / f"{canbroker.resource_digest(bus_key)}.holder.json").read_text(encoding="utf-8"))
    assert holder["owner"]["pid"] == alpha.broker_pid, "the process holding the bus lock is the process that answered"

    detached = alpha.detach()
    assert detached["ok"] is True
    # Not "eventually gone": reaped, with a status. A broker that is still alive
    # after the last participant left is the orphan the automatic lifecycle
    # exists to prevent, and wait() is what proves it is not one.
    assert alpha.broker_process.wait(timeout=30) == canbroker.BROKER_EXIT_OK
    assert not descriptor_path(bus_key, lock_root).exists()
    assert not authkey_path(bus_key, lock_root).exists()


def test_broker_holds_the_machine_wide_bus_lock_for_its_lifetime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reaped_brokers: list[subprocess.Popen]) -> None:
    from agentic_hil.bench import BenchMutex, DeviceBusyError

    config = shared_config(tmp_path, monkeypatch)
    bus_key = bus_lock_key(config, "bench")
    alpha = attach_participant(config, "bench", "alpha")
    try:
        contender = BenchMutex(frontend="single-owner")
        with pytest.raises(DeviceBusyError) as busy:
            contender.acquire_named(bus_key)
        assert busy.value.result["holder"]["pid"] == alpha.broker_pid
    finally:
        alpha.detach()
        alpha.broker_process.wait(timeout=30)
    # And the lock is genuinely gone once the broker is, so the next single-owner
    # run on this bench is not blocked by a bus nobody is on.
    freed = BenchMutex(frontend="single-owner")
    assert freed.acquire_named(bus_key) is True
    freed.release_all()


def test_counter_mismatch_is_refused_at_attach_naming_both_values(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reaped_brokers: list[subprocess.Popen]) -> None:
    config = shared_config(tmp_path, monkeypatch)
    bus_key = bus_lock_key(config, "bench")
    alpha = attach_participant(config, "bench", "alpha")
    try:
        lock_root = alpha.mutex.root
        descriptor = read_descriptor(bus_key, lock_root)
        key = canbroker._read_authkey(authkey_path(bus_key, lock_root))
        connection = Client(descriptor.endpoint, descriptor.family, authkey=key)
        try:
            connection.send(
                {
                    "message": "attach",
                    "protocol_version": PROTOCOL_VERSION,
                    "protocol_digest": PROTOCOL_DIGEST,
                    "expected_counter": descriptor.counter - 1,
                    "bus_key": bus_key,
                    "participant": "beta",
                    "requires_listen_only": False,
                    "client_pid": os.getpid(),
                }
            )
            assert connection.poll(30)
            refusal = connection.recv()
        finally:
            connection.close()
    finally:
        alpha.detach()
        alpha.broker_process.wait(timeout=30)
    assert refusal["ok"] is False
    assert refusal["error_type"] == "can_broker_counter_mismatch"
    assert refusal["broker_counter"] == descriptor.counter
    assert refusal["client_counter"] == descriptor.counter - 1
    assert refusal["broker_counter"] != refusal["client_counter"]


def test_counter_increments_on_every_attach(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reaped_brokers: list[subprocess.Popen]) -> None:
    config = shared_config(tmp_path, monkeypatch)
    alpha = attach_participant(config, "bench", "alpha")
    try:
        beta = attach_participant(config, "bench", "beta")
        try:
            assert beta.counter == alpha.counter + 1
            assert read_descriptor(bus_lock_key(config, "bench"), alpha.mutex.root).counter == beta.counter
        finally:
            beta.detach()
    finally:
        alpha.detach()
        alpha.broker_process.wait(timeout=30)


def test_protocol_digest_mismatch_refuses_across_releases(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reaped_brokers: list[subprocess.Popen]) -> None:
    config = shared_config(tmp_path, monkeypatch)
    alpha = attach_participant(config, "bench", "alpha")
    try:
        monkeypatch.setattr(canbroker, "PROTOCOL_DIGEST", "0" * 16)
        with pytest.raises(ParticipantError) as refusal:
            attach_participant(config, "bench", "beta", allow_start=False, start_timeout_s=5.0)
    finally:
        alpha.detach()
        alpha.broker_process.wait(timeout=30)
    assert refusal.value.result["error_type"] == "can_broker_protocol_mismatch"
    assert refusal.value.result["broker_protocol_digest"] != refusal.value.result["client_protocol_digest"]


def test_authkey_handshake_refuses_a_client_with_the_wrong_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reaped_brokers: list[subprocess.Popen]) -> None:
    config = shared_config(tmp_path, monkeypatch)
    bus_key = bus_lock_key(config, "bench")
    alpha = attach_participant(config, "bench", "alpha")
    try:
        descriptor = read_descriptor(bus_key, alpha.mutex.root)
        with pytest.raises(AuthenticationError):
            Client(descriptor.endpoint, descriptor.family, authkey=b"not-the-brokers-key-at-all-32byt")
        # And the client path maps that to a refusal rather than an exception
        # nobody catches: the run has to be told which of its assumptions failed.
        monkeypatch.setattr(canbroker, "_read_authkey", lambda _path: b"not-the-brokers-key-at-all-32byt")
        with pytest.raises(ParticipantError) as refusal:
            attach_participant(config, "bench", "beta", allow_start=False, start_timeout_s=5.0)
        assert refusal.value.result["error_type"] == "can_broker_authentication_failed"
        # The broker survived the failed handshake and is still serving.
        assert alpha.status()["ok"] is True
    finally:
        alpha.detach()
        alpha.broker_process.wait(timeout=30)


class _ImpostorListener:
    """An endpoint that speaks the protocol perfectly and holds no bus lock."""

    def __init__(self, address: str, authkey: bytes):
        self.listener = Listener(address, endpoint_family(), authkey=authkey)
        self.accepted: list[object] = []
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self) -> None:
        while True:
            try:
                connection = self.listener.accept()
            except BaseException:
                return
            try:
                self.accepted.append(connection.recv())
                connection.send({"ok": True, "message": "attached", "counter": 1, "broker_pid": os.getpid(), "bus_key": "", "participant": "alpha", "view": {}, "bus_frame_log": "", "listen_only": False, "listen_only_enforcement": "controller", "listen_only_proof": "controller", "attached_participants": ["alpha"]})
            except BaseException:
                pass

    def close(self) -> None:
        with __import__("contextlib").suppress(BaseException):
            self.listener.close()


def test_lock_probe_refuses_an_impostor_that_does_not_hold_the_bus(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A peer's claim to own the bus is not evidence; the free lock is.

    The impostor here is not a broken endpoint. It authenticates, it answers a
    well-formed `attached`, it publishes a descriptor naming itself — and it
    forges the holder record too, because that record is advisory: any process
    can write the file that says who owns the lock. Every question that can be
    answered by asking, it answers correctly, and any handshake that only asked
    would seat a participant on a bus this process does not own.

    What it cannot do is hold `can:process:vcan0`. The probe takes the lock, and
    taking it is the proof nobody else has it. The assertion that the impostor
    was never spoken to is the other half: the probe has to run *before* the peer
    is trusted, not after."""
    import socket as socket_module

    from agentic_hil.bench import BenchMutex

    config = shared_config(tmp_path, monkeypatch)
    bus_key = bus_lock_key(config, "bench")
    lock_root = BenchMutex().root
    key = b"an-impostor-key-of-thirty-2bytes"
    address = endpoint_address(bus_key + "#impostor", lock_root)
    impostor = _ImpostorListener(address, key)
    try:
        canbroker._write_authkey(authkey_path(bus_key, lock_root), key)
        # The advisory record, forged to name this process as the lock holder.
        (lock_root / f"{canbroker.resource_digest(bus_key)}.holder.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "state": "held",
                    "resource": bus_key,
                    "owner": {"pid": os.getpid(), "host": socket_module.gethostname(), "frontend": "can-broker"},
                    "acquired_at": "2026-08-09T00:00:00.000Z",
                    "heartbeat_at": "2026-08-09T00:00:00.000Z",
                }
            ),
            encoding="utf-8",
        )
        descriptor_path(bus_key, lock_root).write_text(
            json.dumps(
                {
                    "version": canbroker.DESCRIPTOR_VERSION,
                    "bus_key": bus_key,
                    "endpoint": address,
                    "family": endpoint_family(),
                    "pid": os.getpid(),
                    "protocol_version": PROTOCOL_VERSION,
                    "protocol_digest": PROTOCOL_DIGEST,
                    "counter": 1,
                    "started_at": "2026-08-09T00:00:00.000Z",
                }
            ),
            encoding="utf-8",
        )
        with pytest.raises(ParticipantError) as refusal:
            attach_participant(config, "bench", "alpha", allow_start=False, start_timeout_s=5.0)
    finally:
        impostor.close()
        # These three were planted in the user's real lock root, not in tmp_path,
        # because that is where an attach looks. Sweep them up rather than
        # leaving a forged bus owner behind for the next test to read.
        for planted in (
            descriptor_path(bus_key, lock_root),
            authkey_path(bus_key, lock_root),
            lock_root / f"{canbroker.resource_digest(bus_key)}.holder.json",
        ):
            planted.unlink(missing_ok=True)
    assert refusal.value.result["error_type"] == "can_broker_not_bus_owner"
    assert refusal.value.result["bus_lock_held"] is False
    assert impostor.accepted == [], "the impostor must never be reached, let alone believed"


def test_participant_name_collision_is_refused_and_distinct_names_run_in_parallel(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reaped_brokers: list[subprocess.Popen]) -> None:
    config = shared_config(tmp_path, monkeypatch)
    alpha = attach_participant(config, "bench", "alpha")
    try:
        with pytest.raises(ParticipantError) as refusal:
            attach_participant(config, "bench", "alpha", start_timeout_s=5.0)
        assert refusal.value.result["error_type"] == "device_busy"
        assert refusal.value.result["participant"] == "alpha"
        assert refusal.value.result["participant_lock"].endswith("#alpha")

        beta = attach_participant(config, "bench", "beta")
        try:
            assert sorted(alpha.status()["attached_participants"]) == ["alpha", "beta"]
            assert alpha.broker_pid == beta.broker_pid, "distinct participants share one broker, not one each"
            # And one leaving does not end the bus for the other.
            beta.detach()
            assert alpha.status()["attached_participants"] == ["alpha"]
            assert alpha.broker_process.poll() is None
        finally:
            beta.detach()
    finally:
        alpha.detach()
        alpha.broker_process.wait(timeout=30)


LISTEN_ONLY_SHARES = (
    "      writer:\n"
    "        permissions:\n"
    "          allow_read: true\n"
    "          allow_write: true\n"
    "      sniffer:\n"
    "        requires_listen_only: true\n"
    "        permissions:\n"
    "          allow_read: true\n"
    "          allow_write: false\n"
)


def test_listen_only_and_transmitting_participants_cannot_coexist(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reaped_brokers: list[subprocess.Popen]) -> None:
    config = shared_config(tmp_path, monkeypatch, enforcement="service", shares=LISTEN_ONLY_SHARES)
    writer = attach_participant(config, "bench", "writer")
    try:
        with pytest.raises(ParticipantError) as refusal:
            attach_participant(config, "bench", "sniffer", start_timeout_s=5.0)
        result = refusal.value.result
        assert result["error_type"] == "can_listen_only_conflict"
        assert result["conflicting_participants"] == ["writer"]
        # The level is named, and a service-level claim is software filtering.
        assert result["listen_only_enforcement"] == "service"
        assert result["listen_only_proof"] == "software_filter"
        assert result["listen_only_proof"] != "controller"
    finally:
        writer.detach()
        writer.broker_process.wait(timeout=30)


def test_a_transmitting_participant_cannot_join_a_listen_only_sniffer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reaped_brokers: list[subprocess.Popen]) -> None:
    config = shared_config(tmp_path, monkeypatch, enforcement="controller", shares=LISTEN_ONLY_SHARES)
    sniffer = attach_participant(config, "bench", "sniffer")
    try:
        assert sniffer.attached["listen_only_enforcement"] == "controller"
        assert sniffer.attached["listen_only_proof"] == "controller"
        with pytest.raises(ParticipantError) as refusal:
            attach_participant(config, "bench", "writer", start_timeout_s=5.0)
        assert refusal.value.result["error_type"] == "can_listen_only_conflict"
        assert refusal.value.result["conflicting_participants"] == ["sniffer"]
    finally:
        sniffer.detach()
        sniffer.broker_process.wait(timeout=30)


def test_a_listen_only_bus_refuses_a_participant_that_may_transmit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reaped_brokers: list[subprocess.Popen]) -> None:
    config = shared_config(tmp_path, monkeypatch, listen_only=True, enforcement="controller", shares=LISTEN_ONLY_SHARES)
    sniffer = attach_participant(config, "bench", "sniffer")
    try:
        with pytest.raises(ParticipantError) as refusal:
            attach_participant(config, "bench", "writer", start_timeout_s=5.0)
        assert refusal.value.result["error_type"] == "can_listen_only_conflict"
        assert refusal.value.result["listen_only"] is True
    finally:
        sniffer.detach()
        sniffer.broker_process.wait(timeout=30)


def test_a_bus_incident_aborts_every_participant_and_gates_the_bus(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reaped_brokers: list[subprocess.Popen]) -> None:
    """The adapter fails, not a participant, so the whole bus goes."""
    monkeypatch.setenv("FAKE_CAN_BRIDGE_FAIL_READ", "1")
    config = shared_config(tmp_path, monkeypatch)
    alpha = attach_participant(config, "bench", "alpha")
    beta = attach_participant(config, "bench", "beta")
    try:
        incident = alpha.read(max_frames=1)
        assert incident["ok"] is False
        assert incident["error_type"] == "can_bus_incident"
        assert incident["bus_gated"] is True
        assert sorted(incident["aborted_participants"]) == ["alpha", "beta"]
        assert incident["abort"]["recovery_action"] == config.recovery.auto_recover

        # Every participant's run, not only the one that touched the adapter.
        assert beta.poll_incident()["aborted"] is True
        assert beta.send(0x101, b"\x01")["error_type"] == "can_bus_incident"
        # And the gate holds against a participant that was not even there.
        with pytest.raises(ParticipantError) as gated:
            attach_participant(config, "bench", "gamma", allow_start=False, start_timeout_s=5.0)
        assert gated.value.result["error_type"] in {"can_bus_gated", "can_participant_not_configured"}
    finally:
        beta.detach()
        alpha.detach()
        alpha.broker_process.wait(timeout=30)


def test_a_participant_scoped_failure_aborts_only_that_participant(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reaped_brokers: list[subprocess.Popen]) -> None:
    """A participant spends its own frame budget; the bus and its neighbour do not care."""
    shares = (
        "      alpha:\n"
        "        max_frames: 1\n"
        "        permissions:\n"
        "          allow_read: true\n"
        "          allow_write: true\n"
        "      beta:\n"
        "        max_frames: 16\n"
        "        permissions:\n"
        "          allow_read: true\n"
        "          allow_write: true\n"
    )
    config = shared_config(tmp_path, monkeypatch, shares=shares)
    alpha = attach_participant(config, "bench", "alpha")
    beta = attach_participant(config, "bench", "beta")
    try:
        assert alpha.send(0x101, b"\x01")["ok"] is True
        exhausted = alpha.send(0x101, b"\x02")
        assert exhausted["ok"] is False
        assert exhausted["error_type"] == "can_participant_frame_budget_exhausted"
        assert exhausted["abort"]["scope"] == "participant"

        assert alpha.status()["bus_gated"] is False
        assert beta.send(0x202, b"\x03")["ok"] is True
        assert beta.poll_incident()["aborted"] is False
        assert alpha.send(0x101, b"\x04")["error_type"] == "can_participant_incident"
    finally:
        beta.detach()
        alpha.detach()
        alpha.broker_process.wait(timeout=30)


def test_a_sent_frame_is_attributable_in_both_logs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reaped_brokers: list[subprocess.Popen]) -> None:
    tx_log = tmp_path / "medium.jsonl"
    monkeypatch.setenv("FAKE_CAN_BRIDGE_TX_LOG", str(tx_log))
    monkeypatch.setenv("FAKE_CAN_BRIDGE_RX", json.dumps([[{"id": 0x201, "data_hex": "aabb"}]]))
    config = shared_config(tmp_path, monkeypatch)
    alpha = attach_participant(config, "bench", "alpha")
    beta = attach_participant(config, "bench", "beta")
    try:
        sent = alpha.send(0x123, b"\xde\xad")
        assert sent["ok"] is True
        sequence = sent["frame_seq"]
        received = beta.read(max_frames=1, wait_timeout_s=0.2)
        assert received["frames_read"] == 1
        bus_lines = [json.loads(line) for line in (Path(config.work_dir) / alpha.bus_frame_log).read_text(encoding="utf-8").splitlines() if line.strip()]
        own_lines = [json.loads(line) for line in Path(alpha.log_path).read_text(encoding="utf-8").splitlines() if line.strip()]
    finally:
        beta.detach()
        alpha.detach()
        alpha.broker_process.wait(timeout=30)

    bus_tx = [line for line in bus_lines if line.get("seq") == sequence]
    assert len(bus_tx) == 1
    assert bus_tx[0]["direction"] == "tx"
    assert bus_tx[0]["participant"] == "alpha"
    assert bus_tx[0]["frame"]["data_hex"] == "dead"
    own_tx = [line for line in own_lines if line.get("frame_seq") == sequence]
    assert len(own_tx) == 1
    assert own_tx[0]["direction"] == "tx"
    # The whole-bus log is the broker's, so the received frame is in it too, with
    # the participants it reached named.
    bus_rx = [line for line in bus_lines if line.get("direction") == "rx"]
    assert bus_rx and sorted(bus_rx[0]["delivered_to"]) == ["alpha", "beta"]
    # And the frame really left through the adapter rather than only being logged.
    assert json.loads(tx_log.read_text(encoding="utf-8").splitlines()[0])["data_hex"] == "dead"


def test_a_participant_may_not_send_outside_its_own_view(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reaped_brokers: list[subprocess.Popen]) -> None:
    shares = (
        "      alpha:\n"
        "        filter:\n"
        "          - {id: 256, mask: 2047}\n"
        "        permissions:\n"
        "          allow_read: true\n"
        "          allow_write: true\n"
    )
    config = shared_config(tmp_path, monkeypatch, shares=shares)
    alpha = attach_participant(config, "bench", "alpha")
    try:
        assert alpha.send(0x100, b"\x01")["ok"] is True
        refused = alpha.send(0x200, b"\x01")
        assert refused["ok"] is False
        assert refused["error_type"] == "can_participant_filter_violation"
    finally:
        alpha.detach()
        alpha.broker_process.wait(timeout=30)


def test_the_authkey_is_absent_from_every_result_report_and_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reaped_brokers: list[subprocess.Popen]) -> None:
    """Grep the whole serialized surface for the real key, not for a field name.

    Two measures are being checked at once: the key is kept out of every payload
    by construction, and `redact_sensitive` grew an `authkey` arm for the field
    somebody adds later without reading that argument."""
    monkeypatch.setenv("FAKE_CAN_BRIDGE_RX", json.dumps([[{"id": 0x201, "data_hex": "aabb"}]]))
    config = shared_config(tmp_path, monkeypatch)
    bus_key = bus_lock_key(config, "bench")
    alpha = attach_participant(config, "bench", "alpha")
    lock_root = alpha.mutex.root
    key = canbroker._read_authkey(authkey_path(bus_key, lock_root))
    assert key is not None and len(key) == canbroker.AUTHKEY_BYTES
    results = [
        alpha.attached,
        alpha.status(),
        alpha.send(0x123, b"\xde\xad"),
        alpha.read(max_frames=1, wait_timeout_s=0.2),
        alpha.report_incident("participant", "test_scope"),
        alpha.poll_incident(),
    ]
    bus_log = (Path(config.work_dir) / alpha.bus_frame_log).read_text(encoding="utf-8")
    own_log = Path(alpha.log_path).read_text(encoding="utf-8")
    descriptor_text = descriptor_path(bus_key, lock_root).read_text(encoding="utf-8")
    results.append(alpha.detach())
    alpha.broker_process.wait(timeout=30)

    serialized = json.dumps(results, default=str) + bus_log + own_log + descriptor_text
    assert key.hex() not in serialized.lower()
    assert key.hex().upper() not in serialized
    for encoding in ("utf-8", "latin-1"):
        with __import__("contextlib").suppress(UnicodeDecodeError):
            assert key.decode(encoding) not in serialized
    assert "authkey" not in serialized.lower()
    assert "authkey" not in json.dumps(canbroker.MESSAGE_SURFACE)


def test_redact_sensitive_covers_an_authkey_field() -> None:
    payload = {"authkey": "s3cret", "auth_key": "s3cret", "nested": [{"broker_authkey": "s3cret"}], "resource_key": "kept"}
    redacted = redact_sensitive(payload)
    assert redacted == {"authkey": "[redacted]", "auth_key": "[redacted]", "nested": [{"broker_authkey": "[redacted]"}], "resource_key": "kept"}
    assert "s3cret" not in json.dumps(redacted)


def test_a_killed_broker_leaves_a_corpse_that_the_next_attach_replaces(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reaped_brokers: list[subprocess.Popen]) -> None:
    """One hard-killed broker must not brick the bus until somebody deletes a file.

    A broker that is killed never runs its cleanup, so its descriptor and authkey
    outlive it. The lock probe correctly refuses that endpoint — it owns nothing —
    and if the refusal were the end of the story the bus would stay unusable. It
    is not: a descriptor whose bus lock is free is provably a corpse, because a
    live broker holds that lock from before it publishes until after it
    unpublishes, so the next attach clears it and starts a broker of its own."""
    import signal

    config = shared_config(tmp_path, monkeypatch)
    bus_key = bus_lock_key(config, "bench")
    alpha = attach_participant(config, "bench", "alpha")
    lock_root = alpha.mutex.root
    dead_pid = alpha.broker_pid
    os.kill(dead_pid, signal.SIGTERM)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and canbroker.probe_bus_lock(bus_key, dead_pid, lock_root)["ok"]:
        time.sleep(0.05)
    alpha.detach()

    # The corpse is really there, and it really names the dead broker.
    assert descriptor_path(bus_key, lock_root).exists(), "a killed broker cannot clean up after itself"
    assert read_descriptor(bus_key, lock_root).pid == dead_pid
    assert canbroker.probe_bus_lock(bus_key, dead_pid, lock_root)["bus_lock_held"] is False

    # Starting a broker is what makes replacing it safe, so without that licence
    # the corpse is still refused rather than quietly cleared.
    with pytest.raises(ParticipantError) as refusal:
        attach_participant(config, "bench", "beta", allow_start=False, start_timeout_s=5.0)
    assert refusal.value.result["error_type"] == "can_broker_not_bus_owner"

    beta = attach_participant(config, "bench", "beta")
    try:
        assert beta.broker_pid != dead_pid, "the corpse was replaced, not reused"
        assert read_descriptor(bus_key, lock_root).pid == beta.broker_pid
        assert beta.status()["ok"] is True
    finally:
        beta.detach()
        beta.broker_process.wait(timeout=30)


def test_a_broker_that_nobody_attaches_to_does_not_sit_on_the_bus(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reaped_brokers: list[subprocess.Popen]) -> None:
    """The other orphan: a client that spawned a broker and then died."""
    from agentic_hil.bench import BenchMutex

    monkeypatch.setattr(canbroker, "BROKER_FIRST_ATTACH_TIMEOUT_S", 1.0)
    config = shared_config(tmp_path, monkeypatch)
    bus_key = bus_lock_key(config, "bench")
    lock_root = BenchMutex().root
    child = canbroker._spawn_broker(config, "bench", bus_key, lock_root)
    deadline = time.monotonic() + 40
    while time.monotonic() < deadline and child.poll() is None:
        time.sleep(0.05)
    assert child.poll() == canbroker.BROKER_EXIT_OK, broker_diagnostics(config, bus_key)
    assert not descriptor_path(bus_key, lock_root).exists()
