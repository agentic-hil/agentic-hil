"""A session start that inherits a dead owner's incident names the reason it ended.

A server holding a configured COM port is killed. The next server opens the same
port, the open succeeds, and the incident the dead owner left does not stand: the
reason names no damaged evidence chain, and the next open is what settles the
serial handle either way. What the successful result has to say is what it
inherited, because the reason is the only field separating a host-side fault from
a board nobody has confirmed the state of.

The reason travels for the device the dead owner did not hold, because the lease
release on that path persists the project record as ``cleanup_required``, which is
the state the adopted reason is written onto. A session start never releases its
lease inside the call, so on the dead owner's own device the reason reaches
nothing: the result and the recovery ledger line both carry an empty list, and the
incident id leads to no record that names why it existed.

These tests hold both devices to the same answer, and hold unchanged the two
neighbours the change must not touch: a start with nothing to inherit invents no
reason, and a dead owner whose audit trail is damaged still holds the port.

They also hold the answer to the shape of the fix. The reason has to reach the
caller without the record lying about the bench, so the project record this
successful session writes is pinned as ``active``: a fix that persists the
session's own record as ``cleanup_required`` to carry the reason through would
leave every concurrent reader of that record told a held bench needs recovery.
And the successful result is pinned as still short, carrying no
``quarantine_guidance`` and no cleanup or quarantine flags, because whether a
success should also carry the guidance for what it inherited is the owner's open
question and not one an implementation gets to answer on the way past.

Issue #528.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import DEFAULT_TEST_PERMISSIONS, write_config

from agentic_hil.config import load_config
from agentic_hil.coordination import (
    ATTESTATION_NO_STANDING_STATE,
    HardwareCoordinator,
)
from agentic_hil.devices import can_device, uart_device
from agentic_hil.report import (
    CONTACT_MARKER_KEY,
    CONTACT_MARKER_SOURCE_KEY,
    write_report,
)
from agentic_hil.tools import AgenticHILToolService

# Machine-wide device locks contend across sibling clones, so every identity
# this module puts into a configuration is named for this file alone.
PORT_ID = "dut"
BUS_ID = "bench"
PORT_RESOURCE_ID = "inherited-incident-reason-uart"
BUS_RESOURCE_ID = "inherited-incident-reason-can"
OTHER_PORT_ID = "aux"
OTHER_PORT_RESOURCE_ID = "inherited-incident-reason-other-uart"
DEAD_OWNER_REASON = "owner_process_exited_without_release"
# A reason naming a ledger that could not be written. No reset writes a report
# that was never written, so this incident stands and no automatic route ends it.
AUDIT_BROKEN_REASON = "com_report_audit_broken"


class FakeBackend:
    """The debugger stand-in, for the neighbour that reaches past the port."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def probe_target(self) -> dict:
        self.calls.append("probe_target")
        return {"ok": True, "tool": "probe_target", "target_detected": True}

    def reset_target(self, mode: str = "run") -> dict:
        self.calls.append(f"reset_target:{mode}")
        return {"ok": True, "tool": "reset_target", "mode": mode}

    def close(self) -> None:
        return None

    def sessionless_debug_tools(self) -> frozenset[str]:
        return frozenset()


class WorkingHandle:
    """A serial handle that opens, the way the port answers after the dead
    owner's handle was closed by the operating system."""

    def __init__(self) -> None:
        self.is_open = False
        self.in_waiting = 0

    def open(self) -> None:
        self.is_open = True

    def read(self, size: int) -> bytes:
        return b""

    def write(self, data: bytes) -> int:
        return len(data)

    def flush(self) -> None:
        return None

    def reset_input_buffer(self) -> None:
        return None

    def cancel_read(self) -> None:
        return None

    def close(self) -> None:
        self.is_open = False


class RecordingBus:
    """A python-can bus stand-in that opens and records nothing else."""

    def __init__(self) -> None:
        self.sent: list[object] = []
        self.closed = False

    def send(self, message: object, timeout: float | None = None) -> None:
        self.sent.append(message)

    def shutdown(self) -> None:
        self.closed = True


def install_serial(monkeypatch: pytest.MonkeyPatch, handle: object) -> None:
    monkeypatch.setitem(sys.modules, "serial", SimpleNamespace(Serial=lambda *args, **kwargs: handle))


def install_can(monkeypatch: pytest.MonkeyPatch, bus: object) -> None:
    module = SimpleNamespace(
        Bus=lambda **kwargs: bus,
        BusState=SimpleNamespace(ACTIVE="active", PASSIVE="passive"),
        Message=lambda **kwargs: SimpleNamespace(**kwargs),
        CanInitializationError=type("CanInitializationError", (Exception,), {}),
    )
    monkeypatch.setitem(sys.modules, "can", module)


COM_PORTS_YAML = (
    "com_ports:\n"
    f"  {PORT_ID}:\n"
    '    device: "COM_INHERIT_TEST"\n'
    f'    resource_id: "{PORT_RESOURCE_ID}"\n'
    f"  {OTHER_PORT_ID}:\n"
    '    device: "COM_INHERIT_OTHER"\n'
    f'    resource_id: "{OTHER_PORT_RESOURCE_ID}"\n'
)

CAN_BUSES_YAML = (
    "can_buses:\n"
    f"  {BUS_ID}:\n"
    '    adapter: "socketcan"\n'
    '    channel: "can0"\n'
    "    fd: false\n"
    "    bitrate: 500000\n"
    f'    resource_id: "{BUS_RESOURCE_ID}"\n'
)


def config_for(workspace: Path, *, auto_recover: str | None = None):
    written = write_config(
        workspace,
        permissions={**DEFAULT_TEST_PERMISSIONS, "allow_probe": True, "allow_flash": True, "allow_reset": True},
        auto_recover=auto_recover,
        com_ports_yaml=COM_PORTS_YAML,
        can_buses_yaml=CAN_BUSES_YAML,
    )
    written.write_text("permissions:\n  allow_recover: true\n" + written.read_text(encoding="utf-8"), encoding="utf-8")
    return load_config(str(written))


def leave_dead_owner(
    config,
    resources: list[str],
    *,
    tool: str,
    contact_source: str,
    audit_ok: bool = True,
    reason: str | None = None,
) -> None:
    """The project record, resource markers and last report a killed server leaves.

    The contact marker is what stops the no-contact release taking this owner as
    provably innocent, and the issue is explicit that treating this owner as
    having reached its device is right: the serial open recorded its marker
    before the process died.
    """
    setup = HardwareCoordinator(config, "dead-owner-setup")
    lease = {
        "lease_id": "dead-owner-lease",
        "resources": list(resources),
        "lease_state": "active",
        "safe_state_confirmed": False,
        "processes_reaped": False,
        "audit_ok": audit_ok,
        "cleanup_required": False,
        "quarantined": False,
        "cleanup_reasons": [reason] if reason else [],
        "quarantine_id": None,
    }
    record = setup._base_record("active", list(resources))
    record["leases"] = [lease]
    if reason:
        record["reason"] = reason
    write_report(
        config,
        {
            "ok": True,
            "tool": tool,
            "summary": "Session opened.",
            "side_effect_committed": True,
            "side_effect_status": "committed",
            "retry_safe": False,
            "audit_ok": audit_ok,
            "lease_id": "dead-owner-lease",
            "resources": list(resources),
            "lease_state": "active",
            CONTACT_MARKER_KEY: "2026-09-01T09:00:00Z",
            CONTACT_MARKER_SOURCE_KEY: contact_source,
        },
    )
    for resource in resources:
        marker = setup._base_record("active", list(resources))
        marker["lease_id"] = "dead-owner-lease"
        setup._write_record(resource, marker)
    setup._write_record(setup.project_key, record)


def ledger(config) -> list[dict]:
    path = Path(HardwareCoordinator(config, "ledger-reader").root) / "recovery.jsonl"
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def stand_down_lines(config) -> list[dict]:
    return [line for line in ledger(config) if line.get("recovery") == "incident_stood_down"]


def assert_record_is_honest(service) -> None:
    """The record this held session wrote says what the bench actually is.

    The reason has to reach the caller without the project record claiming a
    held bench needs recovery. `cleanup_required` and `quarantined` are the two
    states `_persist_project` writes the adopted reason onto, so they are the
    tempting route and the wrong one: the record's state is read as the truth
    about the bench by every other reader, and this session is running normally.
    The reason may be written onto an honest `active` record, so only the state
    is pinned here.
    """
    coordinator = service.coordinator
    record = coordinator._read_record(coordinator.project_key)
    assert record is not None, "the held session wrote no project record"
    assert record["state"] == "active", record


def assert_success_stayed_short(result: dict) -> None:
    """A successful start does not read like a quarantine.

    Whether a success that inherited an incident should also carry
    `quarantine_guidance` for the reason is the open question on #528 and the
    owner's to answer. `AgenticHILToolService` attaches the guidance from the
    result's own cleanup fields, so a fix routing the reason through
    `cleanup_reasons` rather than through the stand-down block would answer that
    question by accident. Today's answer is pinned so the decision stays open.
    """
    assert "quarantine_guidance" not in result, result
    assert "cleanup_reasons" not in result, result
    assert result.get("cleanup_required") is not True, result
    assert result.get("quarantined") is not True, result


def assert_one_incident(result: dict, lines: list[dict]) -> str:
    """The ledger line and the returned block are the same incident.

    The issue's complaint is that the incident id leads to no record naming the
    reason, so naming the reason on two surfaces is only worth anything if the
    two surfaces are provably about one incident.
    """
    quarantine_id = result["incident_stood_down"]["quarantine_id"]
    assert isinstance(quarantine_id, str) and quarantine_id, result
    assert len(lines) == 1, lines
    assert lines[0]["quarantine_id"] == quarantine_id, (lines[0], result)
    return quarantine_id


# ---------------------------------------------------------------------------
# The gap: the device the dead owner itself held.


def test_a_com_start_on_the_dead_owners_own_port_names_the_reason_it_ended(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The defect, over `com_session_start`, on the port the dead owner held.

    The start succeeds and is meant to. What it must also say is that it took
    the port from an owner that died holding it, because that is what leaves the
    board possibly holding a partial flash, a halt or stale stimulus. A
    stand-down that ends the incident without naming it cannot be audited
    afterwards, and the incident id on the ledger line leads nowhere.
    """
    config = config_for(tmp_path)
    port_resource = uart_device(config, PORT_ID).lock_key
    leave_dead_owner(config, [port_resource], tool="com_session_start", contact_source="serial_open")
    install_serial(monkeypatch, WorkingHandle())
    service = AgenticHILToolService(config, backend=FakeBackend())
    try:
        result = service.call("com_session_start", {"port_id": PORT_ID})

        assert result["ok"] is True, result
        assert result["incident_stood_down"]["stood_down"] is True, result
        assert result["incident_stood_down"]["reasons"] == [DEAD_OWNER_REASON], result
        assert_success_stayed_short(result)
        assert_record_is_honest(service)
    finally:
        service.close()

    lines = stand_down_lines(config)
    assert_one_incident(result, lines)
    assert lines[0]["reasons"] == [DEAD_OWNER_REASON], lines[0]
    assert lines[0]["attestation"] == ATTESTATION_NO_STANDING_STATE, lines[0]


def test_a_can_start_on_the_dead_owners_own_bus_names_the_reason_it_ended(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The same over `can_session_start`, which reaches its own device the same
    way and never releases its lease inside the call either."""
    config = config_for(tmp_path)
    bus_resource = can_device(config, BUS_ID).lock_key
    leave_dead_owner(config, [bus_resource], tool="can_session_start", contact_source="can_adapter_opened")
    install_can(monkeypatch, RecordingBus())
    service = AgenticHILToolService(config, backend=FakeBackend())
    try:
        result = service.call("can_session_start", {"bus_id": BUS_ID, "clear_rx_queue": False})

        assert result["ok"] is True, result
        assert result["incident_stood_down"]["stood_down"] is True, result
        assert result["incident_stood_down"]["reasons"] == [DEAD_OWNER_REASON], result
        assert_success_stayed_short(result)
        assert_record_is_honest(service)
    finally:
        service.close()

    lines = stand_down_lines(config)
    assert_one_incident(result, lines)
    assert lines[0]["reasons"] == [DEAD_OWNER_REASON], lines[0]


# ---------------------------------------------------------------------------
# The neighbours that must not change.


def test_a_call_reaching_past_the_dead_owners_device_still_names_the_reason(tmp_path: Path) -> None:
    """The path that already works, pinned so the fix cannot be a rewrite of it.

    `probe_target` reaches for the debugger, which the dead owner never held, so
    its lease release persists the project record as `cleanup_required`, the
    state that carries the adopted reason, and the stand-down finds it there.
    """
    config = config_for(tmp_path, auto_recover="off")
    port_resource = uart_device(config, PORT_ID).lock_key
    leave_dead_owner(config, [port_resource], tool="com_session_start", contact_source="serial_open")
    service = AgenticHILToolService(config, backend=FakeBackend())
    try:
        result = service.call("probe_target", {})

        assert result["ok"] is True, result
        assert result["incident_stood_down"]["reasons"] == [DEAD_OWNER_REASON], result
        assert service.coordinator.blocked is False
    finally:
        service.close()

    lines = stand_down_lines(config)
    assert len(lines) == 1, lines
    assert lines[0]["reasons"] == [DEAD_OWNER_REASON], lines[0]


def test_a_start_with_nothing_to_inherit_invents_no_reason(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A clean bench. There is no incident, so there is no stand-down, no reason
    and no ledger line: the reason a start reports is the one it actually
    inherited, never one the code supplies because a field wanted filling."""
    config = config_for(tmp_path)
    install_serial(monkeypatch, WorkingHandle())
    service = AgenticHILToolService(config, backend=FakeBackend())
    try:
        result = service.call("com_session_start", {"port_id": PORT_ID})

        assert result["ok"] is True, result
        assert "incident_stood_down" not in result, result
        assert service.coordinator.blocked is False
        assert service.coordinator.adopted_reason is None
    finally:
        service.close()

    assert stand_down_lines(config) == []


def test_a_dead_owner_with_a_damaged_audit_trail_still_holds_the_port(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The refusal that must survive untouched. A reason naming a ledger that
    could not be written is the one family no next contact re-establishes, so
    the incident stands, the start is refused, and nothing stands anything down.

    This one asks the coordinator for its status first, which adopts the
    incident and rewrites the record before the tool call, so the refusal it
    pins is the one a server already holding the incident gives. The fresh
    server's own route is the test below.
    """
    config = config_for(tmp_path)
    port_resource = uart_device(config, PORT_ID).lock_key
    leave_dead_owner(
        config,
        [port_resource],
        tool="com_session_start",
        contact_source="serial_open",
        audit_ok=False,
        reason=AUDIT_BROKEN_REASON,
    )
    install_serial(monkeypatch, WorkingHandle())
    service = AgenticHILToolService(config, backend=FakeBackend())
    try:
        assert service.coordinator.status()["incident_stands"] is True

        refused = service.call("com_session_start", {"port_id": PORT_ID})

        assert refused["ok"] is False, refused
        assert refused["error_type"] == "resource_quarantined", refused
        assert service.coordinator.incident_stands is True
        assert service.coordinator.stand_down() is None
    finally:
        service.close()

    assert stand_down_lines(config) == []


def test_a_fresh_server_refuses_the_damaged_audit_trail_and_names_what_it_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The refusal the issue is actually about, on a server asked nothing first.

    Nothing has adopted the incident when the tool call arrives, so the refusal
    is raised where the acquire meets the record the dead owner left. The
    refusal has to name what it refused for: the reason is what keys the signer
    guidance and the operator's choice between retrying and walking to the
    bench, and a refusal that names nothing leaves the same hole the successful
    start does.
    """
    config = config_for(tmp_path)
    port_resource = uart_device(config, PORT_ID).lock_key
    leave_dead_owner(
        config,
        [port_resource],
        tool="com_session_start",
        contact_source="serial_open",
        audit_ok=False,
        reason=AUDIT_BROKEN_REASON,
    )
    install_serial(monkeypatch, WorkingHandle())
    service = AgenticHILToolService(config, backend=FakeBackend())
    try:
        refused = service.call("com_session_start", {"port_id": PORT_ID})

        assert refused["ok"] is False, refused
        assert refused["error_type"] == "resource_quarantined", refused
        assert refused["quarantined"] is True, refused
        assert refused["cleanup_required"] is True, refused
        assert refused["cleanup_reasons"] == [DEAD_OWNER_REASON], refused
        assert service.coordinator.incident_stands is True
        assert service.coordinator.stand_down() is None
    finally:
        service.close()

    assert stand_down_lines(config) == []
