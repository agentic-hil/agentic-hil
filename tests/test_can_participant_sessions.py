"""Public CAN participant sessions and run declarations (#500)."""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import write_config

from agentic_hil.canbroker import bus_lock_key, participant_lock_key
from agentic_hil.config import ConfigError, load_config
from agentic_hil.contracts import MCP_TOOLS
from agentic_hil.devices import DeviceError, resolve_devices
from agentic_hil.test_reactor import load_test_config, plan_devices
from agentic_hil.tools import AgenticHILToolService

BUS = "shared_can"
SHARES_YAML = f'''can_buses:
  {BUS}:
    adapter: process
    channel: fake-can
    executable: fake-bridge
    shares:
      ecu_a:
        permissions:
          allow_read: true
          allow_write: true
      ecu_b:
        permissions:
          allow_read: true
          allow_write: true
'''


class SharedFakeParticipant:
    """Small broker-shaped test double; both views see the same transmitted frames."""

    frames: list[dict] = []

    def __init__(self, name: str):
        self.name = name
        self.detached = False

    def send(self, frame_id: int, data: bytes, *, extended: bool = False, rtr: bool = False) -> dict:
        frame = {"id": frame_id, "data": bytes(data), "extended": extended, "rtr": rtr, "sender": self.name}
        self.frames.append(frame)
        return {"ok": True, "frame_seq": len(self.frames), "frame": {"id": frame_id, "data_hex": bytes(data).hex(), "extended": extended, "rtr": rtr}}

    def read(self, max_frames: int, wait_timeout_s: float) -> dict:
        visible = [{**frame, "data_hex": frame["data"].hex()} for frame in self.frames if frame["sender"] != self.name][:max_frames]
        return {"ok": True, "frames": visible}

    def status(self) -> dict:
        return {"ok": True, "participant": self.name, "attached_participants": ["ecu_a", "ecu_b"]}

    def detach(self) -> dict:
        self.detached = True
        return {"ok": True, "participant": self.name}


def config_for(tmp_path: Path):
    return load_config(str(write_config(tmp_path, can_buses_yaml=SHARES_YAML)))


def test_can_session_contract_accepts_named_participants():
    schema = next(tool["inputSchema"] for tool in MCP_TOOLS if tool["name"] == "can_session_start")
    assert "participant" in schema["properties"]
    assert schema["properties"]["participant"]["type"] == "string"


def test_public_frame_normalization_preserves_broker_delivery_metadata():
    from agentic_hil.can import normalize_received_frames

    frames = normalize_received_frames([{
        "id": 0x101,
        "extended": False,
        "rtr": False,
        "data_hex": "aa",
        "frame_seq": 7,
        "origin": "participant_tx",
        "delivery_status": "adapter_accepted",
    }])

    assert frames == [{
        "id": 0x101,
        "id_hex": "0x101",
        "extended": False,
        "rtr": False,
        "data_hex": "aa",
        "dlc": 1,
        "frame_seq": 7,
        "origin": "participant_tx",
        "delivery_status": "adapter_accepted",
    }]


def test_broker_participant_close_proves_only_its_connection_and_keeps_unconfirmed_detach_failed():
    from agentic_hil.can import BrokerCanAdapterSession

    class DetachPeer:
        def __init__(self, *results):
            self.results = iter(results)

        def detach(self):
            return next(self.results)

        def status(self):
            raise AssertionError("detached broker connection must not be queried")

    confirmed_session = BrokerCanAdapterSession(DetachPeer({"ok": True, "message": "detached"}))
    confirmed = confirmed_session.close()
    assert confirmed["safe_state_confirmed"] is True
    assert confirmed["process_reaped"] is True
    assert confirmed_session.status()["active"] is False

    uncertain_session = BrokerCanAdapterSession(DetachPeer(
        {"ok": True, "broker_unreachable": True},
        {"ok": True, "already_detached": True},
    ))
    uncertain = uncertain_session.close()
    retry = uncertain_session.close()
    assert uncertain["safe_state_confirmed"] is False
    assert uncertain["process_reaped"] is True
    assert retry["safe_state_confirmed"] is False
    assert uncertain_session.status()["cleanup_required"] is True


def test_two_named_sessions_share_frames_and_stop_independently(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    SharedFakeParticipant.frames = []
    attached: list[SharedFakeParticipant] = []

    def attach(config, bus_id, participant, **kwargs):
        assert bus_id == BUS
        peer = SharedFakeParticipant(participant)
        attached.append(peer)
        return peer

    import agentic_hil.canbroker as broker_module
    monkeypatch.setattr(broker_module, "attach_participant", attach)
    service = AgenticHILToolService(config_for(tmp_path))
    try:
        first = service.call("can_session_start", {"bus_id": BUS, "participant": "ecu_a", "clear_rx_queue": False})
        second = service.call("can_session_start", {"bus_id": BUS, "participant": "ecu_b", "clear_rx_queue": False})
        assert first["ok"] is True, first
        assert second["ok"] is True, second
        assert service.call("can_buses_list", {})["buses"][BUS]["active_participants"] == ["ecu_a", "ecu_b"]
        sent = service.call("can_send", {"bus_id": BUS, "participant": "ecu_a", "frame_id": 0x123, "data_hex": "CA FE"})
        read = service.call("can_read", {"bus_id": BUS, "participant": "ecu_b", "max_frames": 4})
        assert sent["ok"] is True, sent
        assert read["ok"] is True, read
        assert read["frames"][0]["id"] == 0x123, read
        assert service.call("can_session_stop", {"bus_id": BUS, "participant": "ecu_a"})["ok"] is True
        assert service.call("can_session_stop", {"bus_id": BUS, "participant": "ecu_b"})["ok"] is True
    finally:
        service.close()


def test_run_selector_resolves_to_configured_participant_lock(tmp_path: Path):
    config = config_for(tmp_path)
    device = resolve_devices(config, [{"kind": "can", "id": BUS, "participant": "ecu_b"}]).devices[0]
    assert device.lock_key == participant_lock_key(bus_lock_key(config, BUS), "ecu_b")
    assert device.declared_keys == (device.lock_key,)


def test_shared_run_selector_cannot_declare_the_whole_bus(tmp_path: Path):
    config = config_for(tmp_path)
    with pytest.raises(DeviceError) as refusal:
        resolve_devices(config, [{"kind": "can", "id": BUS}])
    assert refusal.value.result["error_type"] == "can_participant_required"


def test_declared_run_holds_the_named_participant_and_refuses_a_different_one(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import agentic_hil.canbroker as broker_module
    monkeypatch.setattr(broker_module, "attach_participant", lambda config, bus_id, participant, **kwargs: SharedFakeParticipant(participant))
    service = AgenticHILToolService(config_for(tmp_path))
    try:
        run = service.call("bench_run_start", {"devices": [{"kind": "can", "id": BUS, "participant": "ecu_a"}]})
        assert run["ok"] is True, run
        assert run["declared_devices"] == [participant_lock_key(bus_lock_key(service.config, BUS), "ecu_a")], run
        started = service.call("can_session_start", {"bus_id": BUS, "participant": "ecu_a", "clear_rx_queue": False})
        assert started["ok"] is True, started
        wrong_participant = service.call("can_session_start", {"bus_id": BUS, "participant": "ecu_b", "clear_rx_queue": False})
        assert wrong_participant["error_type"] == "undeclared_device", wrong_participant
    finally:
        service.call("can_session_stop", {"bus_id": BUS, "participant": "ecu_a"})
        service.call("bench_run_stop", {})
        service.close()


def test_reactor_plan_declares_the_named_participant_and_gates_new_plan_syntax(tmp_path: Path):
    config = config_for(tmp_path)
    workspace = Path(config.workspace_root)
    workspace.mkdir(parents=True, exist_ok=True)
    plan_path = workspace / "shared-can.yaml"
    plan_path.write_text("version: 6\nsteps:\n  - {action: can_open, bus_id: shared_can, participant: ecu_a, clear_rx_queue: false}\n  - {action: can_close, bus_id: shared_can, participant: ecu_a}\n", encoding="utf-8")
    plan = load_test_config(str(plan_path), str(workspace))
    devices = plan_devices(config, plan)
    assert len(devices.devices) == 1
    assert devices.devices[0].lock_key == participant_lock_key(bus_lock_key(config, BUS), "ecu_a")

    plan_path.write_text("version: 5\nsteps:\n  - {action: can_open, bus_id: shared_can, participant: ecu_a, clear_rx_queue: false}\n", encoding="utf-8")
    with pytest.raises(ConfigError) as outdated:
        load_test_config(str(plan_path), str(workspace))
    assert outdated.value.details["plan_version"] == 5
    assert outdated.value.details["requires_plan_version"] == 6

    plan_path.write_text("version: 7\nsteps:\n  - {device: dut, action: delay, duration_ms: 1}\n", encoding="utf-8")
    with pytest.raises(ConfigError) as unsupported:
        load_test_config(str(plan_path), str(workspace))
    assert "version" in unsupported.value.details["field"]


def test_reactor_run_opens_two_declared_participants_and_routes_frames_between_them(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import agentic_hil.canbroker as broker_module

    SharedFakeParticipant.frames = []
    monkeypatch.setattr(broker_module, "attach_participant", lambda config, bus_id, participant, **kwargs: SharedFakeParticipant(participant))
    config = config_for(tmp_path)
    workspace = Path(config.workspace_root)
    plan_path = workspace / ".agentic-hil" / "testconfig.yaml"
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(
        "version: 6\nsteps:\n"
        "  - {action: can_open, bus_id: shared_can, participant: ecu_a, clear_rx_queue: false}\n"
        "  - {action: can_open, bus_id: shared_can, participant: ecu_b, clear_rx_queue: false}\n"
        "  - {action: can_send, bus_id: shared_can, participant: ecu_a, frame_id: 291, data_hex: 'CA FE'}\n"
        "  - {action: can_read, bus_id: shared_can, participant: ecu_b, max_frames: 4}\n"
        "  - {action: can_close, bus_id: shared_can, participant: ecu_a}\n"
        "  - {action: can_close, bus_id: shared_can, participant: ecu_b}\n",
        encoding="utf-8",
    )
    service = AgenticHILToolService(config)
    try:
        result = service.call("test_reactor_run", {"test_config_path": ".agentic-hil/testconfig.yaml"})
    finally:
        service.close()
    assert result["ok"] is True, result
    assert [step["action"] for step in result["steps"]] == ["can_open", "can_open", "can_send", "can_read", "can_close", "can_close"], result
    assert all(step["result"]["ok"] is True for step in result["steps"]), result
    assert result["steps"][3]["result"]["frames"][0]["data_hex"] == "cafe", result


def test_bus_scoped_broker_incident_reaches_coordinator(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    class FailedBusParticipant(SharedFakeParticipant):
        bus_gate = {"gated": False}

        def send(self, frame_id: int, data: bytes, *, extended: bool = False, rtr: bool = False) -> dict:
            self.bus_gate["gated"] = True
            return {"ok": False, "error_type": "can_bus_incident", "incident_scope": "bus", "abort": {"scope": "bus"}, "bus_gated": True, "summary": "adapter failed", "side_effect_status": "unknown"}

        def status(self) -> dict:
            gated = self.bus_gate["gated"]
            return {"ok": True, "participant": self.name, "abort": {"scope": "bus"} if gated else None, "bus_gated": gated}

    import agentic_hil.canbroker as broker_module
    monkeypatch.setattr(broker_module, "attach_participant", lambda config, bus_id, participant, **kwargs: FailedBusParticipant(participant))
    service = AgenticHILToolService(config_for(tmp_path))
    try:
        for participant in ("ecu_a", "ecu_b"):
            started = service.call("can_session_start", {"bus_id": BUS, "participant": participant, "clear_rx_queue": False})
            assert started["ok"] is True, started
        failed = service.call("can_send", {"bus_id": BUS, "participant": "ecu_a", "frame_id": 0x123, "data_hex": "01"})
        assert failed["error_type"] == "can_bus_incident", failed
        assert failed["bus_gated"] is True, failed
        assert failed["abort"]["scope"] == "bus", failed
        assert "can_send_effect_unconfirmed" in failed["cleanup_reasons"], failed
        peer_send = service.call("can_send", {"bus_id": BUS, "participant": "ecu_b", "frame_id": 0x124, "data_hex": "02"})
        assert peer_send["error_type"] == "session_not_active", peer_send
        peer_status = service.can_buses.sessions[(BUS, "ecu_b")].adapter_session.status()
        assert peer_status["bus_gated"] is True, peer_status
    finally:
        service.close()


def test_participant_scoped_failure_does_not_quarantine_peer_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    class FailedParticipant(SharedFakeParticipant):
        def __init__(self, name: str):
            super().__init__(name)
            self.failed = False

        def send(self, frame_id: int, data: bytes, *, extended: bool = False, rtr: bool = False) -> dict:
            self.failed = True
            return {"ok": False, "error_type": "can_participant_incident", "abort": {"scope": "participant"}, "side_effect_committed": False, "summary": "participant-local refusal"}

        def status(self) -> dict:
            return {"ok": True, "participant": self.name, "abort": {"scope": "participant"} if self.failed else None, "attached_participants": ["ecu_a", "ecu_b"]}

    import agentic_hil.canbroker as broker_module
    monkeypatch.setattr(
        broker_module,
        "attach_participant",
        lambda config, bus_id, participant, **kwargs: FailedParticipant(participant) if participant == "ecu_a" else SharedFakeParticipant(participant),
    )
    service = AgenticHILToolService(config_for(tmp_path))
    try:
        for participant in ("ecu_a", "ecu_b"):
            opened = service.call("can_session_start", {"bus_id": BUS, "participant": participant, "clear_rx_queue": False})
            assert opened["ok"] is True, opened
        failed = service.call("can_send", {"bus_id": BUS, "participant": "ecu_a", "frame_id": 0x123, "data_hex": "01"})
        peer_send = service.call("can_send", {"bus_id": BUS, "participant": "ecu_b", "frame_id": 0x124, "data_hex": "02"})
        assert failed["error_type"] == "can_participant_incident", failed
        assert peer_send["ok"] is True, peer_send
        assert service.coordinator.blocked is False
    finally:
        service.close()


UNSHARED_YAML = f'''can_buses:
  {BUS}:
    adapter: process
    channel: exclusive
    executable: fake-bridge
    permissions:
      allow_read: true
      allow_write: true
'''


class ExclusiveFakeAdapter:
    """A single-owner adapter session that opens, idles and closes."""

    adapter_name = "process"

    def __init__(self):
        self.closed = False

    def send(self, frame) -> dict:
        return {"ok": True}

    def read(self, max_frames: int, wait_timeout_s: float) -> dict:
        return {"ok": True, "frames": []}

    def status(self) -> dict:
        return {"ok": True, "active": not self.closed}

    def close(self) -> dict:
        self.closed = True
        return {"ok": True, "safe_state_confirmed": True, "process_reaped": True}


def test_stop_refuses_a_participant_the_shared_bus_does_not_declare(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A misspelled participant is refused, not told the bus is idle (#632)."""
    import agentic_hil.canbroker as broker_module

    monkeypatch.setattr(broker_module, "attach_participant", lambda config, bus_id, participant, **kwargs: SharedFakeParticipant(participant))
    service = AgenticHILToolService(config_for(tmp_path))
    try:
        started = service.call("can_session_start", {"bus_id": BUS, "participant": "ecu_a", "clear_rx_queue": False})
        assert started["ok"] is True, started
        stopped = service.call("can_session_stop", {"bus_id": BUS, "participant": "ecu_x"})
        assert stopped["ok"] is False, stopped
        assert stopped["error_type"] == "can_participant_not_configured", stopped
        assert stopped["participant"] == "ecu_x", stopped
        assert stopped["configured_participants"] == ["ecu_a", "ecu_b"], stopped
        assert stopped["side_effect_committed"] is False, stopped
        assert stopped["remediation"], stopped
        assert "was_active" not in stopped, stopped
        assert (BUS, "ecu_a") in service.can_buses.sessions
        assert service.call("can_session_stop", {"bus_id": BUS, "participant": "ecu_a"})["was_active"] is True
    finally:
        service.close()


def test_stop_refuses_a_participant_on_a_bus_without_shares(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The open session is not hidden behind a "was not active" (#632)."""
    adapter = ExclusiveFakeAdapter()
    monkeypatch.setattr("agentic_hil.can.open_adapter", lambda config, bus_id, bus_config, clear_rx_queue, contact=None: {"ok": True, "session": adapter})
    service = AgenticHILToolService(load_config(str(write_config(tmp_path, can_buses_yaml=UNSHARED_YAML))))
    try:
        started = service.call("can_session_start", {"bus_id": BUS, "clear_rx_queue": False})
        assert started["ok"] is True, started
        stopped = service.call("can_session_stop", {"bus_id": BUS, "participant": "nobody"})
        assert stopped["ok"] is False, stopped
        assert stopped["error_type"] == "can_participant_not_configured", stopped
        assert stopped["configured_participants"] == [], stopped
        assert adapter.closed is False
        plain = service.call("can_session_stop", {"bus_id": BUS})
        assert plain["was_active"] is True, plain
    finally:
        service.close()


def test_a_broker_permission_refusal_carries_the_key_and_its_remediation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The broker's own refusal reaches the caller with the key it names (#657).

    The server's grants say yes, the broker's say no: the broker loaded the file
    after the server did."""
    from agentic_hil.knowledge import remediation_fields

    write_key = f"can_buses.{BUS}.shares.ecu_a.permissions.allow_write"
    read_key = f"can_buses.{BUS}.shares.ecu_a.permissions.allow_read"

    class NarrowedParticipant(SharedFakeParticipant):
        def send(self, frame_id: int, data: bytes, *, extended: bool = False, rtr: bool = False) -> dict:
            return {"ok": False, "error_type": "permission_denied", "summary": f"Writing is disabled. The permission is `{write_key}` and it is false.", "bus_id": BUS, "participant": self.name, "permission": write_key, "retry_safe": False, "side_effect_committed": False}

        def read(self, max_frames: int, wait_timeout_s: float) -> dict:
            return {"ok": False, "error_type": "permission_denied", "summary": f"Reading is disabled. The permission is `{read_key}` and it is false.", "bus_id": BUS, "participant": self.name, "permission": read_key, "retry_safe": False, "side_effect_committed": False}

    import agentic_hil.canbroker as broker_module

    monkeypatch.setattr(broker_module, "attach_participant", lambda config, bus_id, participant, **kwargs: NarrowedParticipant(participant))
    service = AgenticHILToolService(config_for(tmp_path))
    try:
        assert service.call("can_session_start", {"bus_id": BUS, "participant": "ecu_a", "clear_rx_queue": False})["ok"] is True
        sent = service.call("can_send", {"bus_id": BUS, "participant": "ecu_a", "frame_id": 0x123, "data_hex": "01"})
        read = service.call("can_read", {"bus_id": BUS, "participant": "ecu_a"})
        until = service.call("can_read", {"bus_id": BUS, "participant": "ecu_a", "until_id": 0x123, "wait_timeout_s": 0})
    finally:
        service.close()
    assert sent["error_type"] == "permission_denied", sent
    assert sent["permission"] == write_key, sent
    assert sent["remediation"] == remediation_fields("permission_denied", permission=write_key)["remediation"], sent
    for answer in (read, until):
        assert answer["error_type"] == "permission_denied", answer
        assert answer["permission"] == read_key, answer
        assert answer["remediation"] == remediation_fields("permission_denied", permission=read_key)["remediation"], answer


def broker_failure(kind: str, name: str) -> BaseException:
    from agentic_hil.canbroker import ParticipantError

    if kind == "timeout":
        return ParticipantError({"ok": False, "error_type": "can_broker_timeout", "summary": "The CAN broker did not answer within the request timeout.", "bus_id": BUS, "participant": name, "side_effect_status": "unknown"})
    if kind == "invalid_message":
        return ParticipantError({"ok": False, "error_type": "can_broker_invalid_message", "summary": "The CAN broker answered with a message this client cannot read.", "bus_id": BUS, "participant": name})
    return BrokenPipeError(32, "The pipe has been ended")


BROKER_FAILURE_TYPES = {"timeout": "can_broker_timeout", "invalid_message": "can_broker_invalid_message", "gone": "can_broker_disconnected"}


@pytest.mark.parametrize("kind", sorted(BROKER_FAILURE_TYPES))
@pytest.mark.parametrize("tool", ["can_send", "can_read"])
def test_a_broker_failure_is_a_can_refusal_and_never_an_audit_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tool: str, kind: str):
    """A slow, garbled or ended broker is a connection failure with its own type (#664).

    Not `hardware_action_exception`, not `audit_failed_after_action`, and no
    audit-broken incident waiting for an operator's signature. A broker that has
    ended fails every request, status included; a slow or garbled one fails the
    request in hand."""
    from agentic_hil.knowledge import ERROR_CATALOGUE

    class FailingParticipant(SharedFakeParticipant):
        ended = False

        def send(self, frame_id: int, data: bytes, *, extended: bool = False, rtr: bool = False) -> dict:
            self.ended = kind == "gone"
            raise broker_failure(kind, self.name)

        def read(self, max_frames: int, wait_timeout_s: float) -> dict:
            self.ended = kind == "gone"
            raise broker_failure(kind, self.name)

        def status(self) -> dict:
            if self.ended:
                raise broker_failure(kind, self.name)
            return super().status()

    import agentic_hil.canbroker as broker_module

    monkeypatch.setattr(broker_module, "attach_participant", lambda config, bus_id, participant, **kwargs: FailingParticipant(participant))
    service = AgenticHILToolService(config_for(tmp_path))
    try:
        assert service.call("can_session_start", {"bus_id": BUS, "participant": "ecu_a", "clear_rx_queue": False})["ok"] is True
        if tool == "can_send":
            answer = service.call("can_send", {"bus_id": BUS, "participant": "ecu_a", "frame_id": 0x123, "data_hex": "01"})
        else:
            answer = service.call("can_read", {"bus_id": BUS, "participant": "ecu_a"})
        status = service.hardware_lease_status()
    finally:
        service.close()
    error_type = BROKER_FAILURE_TYPES[kind]
    assert answer["ok"] is False, answer
    assert answer["error_type"] == error_type, answer
    assert error_type in ERROR_CATALOGUE, error_type
    assert answer["remediation"], answer
    assert answer.get("audit_ok") is not False, answer
    assert answer["participant"] == "ecu_a", answer
    if tool == "can_send":
        assert answer["side_effect_status"] == "unknown", answer
    else:
        assert answer["side_effect_status"] == "not_started", answer
        assert answer["side_effect_committed"] is False, answer
    assert status["incident_stands"] is False, status
    assert not [reason for reason in status["cleanup_reasons"] if "audit_broken" in str(reason) or "hardware_exception" in str(reason)], status


def test_unshared_bus_keeps_exclusive_session_semantics(tmp_path: Path):
    config = load_config(str(write_config(tmp_path, can_buses_yaml=f'''can_buses:\n  {BUS}:\n    adapter: process\n    channel: exclusive\n    executable: fake-bridge\n''')))
    device = resolve_devices(config, [{"kind": "can", "id": BUS}]).devices[0]
    assert device.config_id == BUS
    assert device.bus.channel == "exclusive"
