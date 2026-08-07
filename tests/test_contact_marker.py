"""Quarantine presupposes contact, and contact is recorded rather than inferred.

Three benches were held in one week by failures that never reached any hardware:
a socket bind, an interface that was not there, and a serial port another program
was already holding. Each was a gap in the same mechanism — a per-backend set of
exception classes that *prove* no contact, with everything outside the set
treated as an unknown board state. A set of known-innocent classes is only ever
as complete as the last incident, which is why there was a third one.

So the question is inverted. Each session records a contact marker at the one
point where contact becomes provable, and every failure handler that used to
quarantine on "markerless" asks the marker first. The tests here pin both halves,
and the second half is the load-bearing one:

* A failure before contact refuses, whatever ended it — including an exception
  class no classifier has ever heard of. That is the property a set of classes
  cannot have.
* A failure after contact quarantines exactly as it did before, and a failure
  that can neither show contact nor rule it out quarantines too. An unknown
  rounded down to safe is the failure quarantine exists to prevent.

The serial half also closes the case that produced the third incident: the port
is now opened exclusively, so a port somebody else is holding fails at the open —
a refusal — instead of succeeding into a shared line whose interleaved bytes
surface much later as an unconfirmed effect, which is a quarantine.
"""

from __future__ import annotations

import errno
import inspect
import json
import os
import sys
from contextlib import suppress
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import write_config

from agentic_hil.can import CanBusService
from agentic_hil.comports import PORT_BUSY_ERRNOS, ComPortService, serial_port_busy
from agentic_hil.config import load_config
from agentic_hil.knowledge import COM_PORT_BUSY_ERROR, catalogue_entry
from agentic_hil.report import (
    CONTACT_MARKER_KEY,
    CONTACT_MARKER_SOURCE_KEY,
    ContactMarker,
    last_report_path,
    no_contact_refusal,
)
from agentic_hil.tools import AgenticHILToolService

# Device locks are machine-wide, so a port id, a device name and a bus id shared
# with another checkout's tests would contend across clones. Everything here is
# named for this file alone.
PORT_ID = "contact_marker_uart"
DEVICE = "/dev/ttyCONTACTMARKER0"
BUS_ID = "contact_marker_bus"
CHANNEL = "vcan221marker"


def com_config(workspace: Path):
    yaml = f'com_ports:\n  {PORT_ID}:\n    device: "{DEVICE}"\n'
    return load_config(str(write_config(workspace, com_ports_yaml=yaml)))


def can_config(workspace: Path):
    yaml = f'can_buses:\n  {BUS_ID}:\n    adapter: "socketcan"\n    channel: "{CHANNEL}"\n'
    return load_config(str(write_config(workspace, can_buses_yaml=yaml)))


def blocking_record_states(config) -> set[str]:
    records = Path(config.state_root) / "coordination" / "records"
    if not records.is_dir():
        return set()
    states = {json.loads(path.read_text(encoding="utf-8")).get("state") for path in records.glob("*.json")}
    return states & {"cleanup_required", "quarantined", "recovery_pending"}


def assert_refused_without_quarantine(result: dict, config) -> None:
    assert result["ok"] is False, result
    assert result["retry_safe"] is True, result
    assert result.get("quarantined") is not True, result
    assert result.get("cleanup_required") is not True, result
    assert result["side_effect_status"] == "not_started", result
    assert not blocking_record_states(config)


class FakePort:
    """One serial device, and whether somebody else is holding it.

    ``open()`` answers a held port the way pyserial's POSIX backend does when its
    non-blocking exclusive lock is already taken: a ``SerialException``, which is
    an ``OSError`` carrying the number, built as ``SerialException(errno, text)``.
    Nothing here matches on the message, and neither does the code under test.
    """

    def __init__(self, held: bool = False) -> None:
        self.held = held
        self.opens = 0
        self.exclusive_at_open: object = "never opened"

    def handle(self) -> FakeHandle:
        return FakeHandle(self)


class FakeHandle:
    def __init__(self, port: FakePort) -> None:
        self.port_device = port
        self.is_open = False
        self.in_waiting = 0
        self.exclusive = None

    def open(self) -> None:
        self.port_device.opens += 1
        self.port_device.exclusive_at_open = self.exclusive
        if self.port_device.held:
            raise OSError(errno.EWOULDBLOCK, f"Could not exclusively lock port {DEVICE}: [Errno 11] Resource temporarily unavailable")
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


def install_fake_serial(monkeypatch: pytest.MonkeyPatch, port: FakePort) -> None:
    monkeypatch.setitem(sys.modules, "serial", SimpleNamespace(Serial=lambda *args, **kwargs: port.handle()))


# ---------------------------------------------------------------------------
# What pyserial actually promises, per platform. Read from the installed
# package rather than assumed, because the whole of part one rests on it.


def test_pyserial_exclusivity_is_a_setting_this_platform_accepts() -> None:
    """`exclusive = True` is a real setting on the platform pyserial builds for.

    Asserted against the installed pyserial and not a stub: the flag is set on
    every open now, so a pyserial that rejected it would refuse every session
    rather than share a port, and that is worth finding here.
    """
    import serial

    handle = serial.Serial()
    handle.exclusive = True

    assert handle.exclusive is True


@pytest.mark.skipif(os.name == "nt", reason="POSIX is where a second open otherwise succeeds")
def test_on_posix_the_exclusivity_is_an_advisory_lock_taken_inside_open() -> None:
    """What the flag buys on POSIX, stated exactly.

    pyserial 3.5 does not use `TIOCEXCL`. It takes a non-blocking `flock` on the
    descriptor from inside `_reconfigure_port`, which `open()` calls, so a held
    port fails during `open()` and `open()` unwinds its own descriptor — which is
    what puts the failure in the refusal path rather than the quarantine path.

    Advisory has a consequence worth writing down rather than discovering: the
    lock binds holders that take one too. It stops a second pyserial user and
    anything else that flocks the tty (ModemManager does), and it does not stop a
    program that simply opens the device. This is a large improvement over
    nothing and it is not mandatory locking.
    """
    from serial import serialposix

    source = inspect.getsource(serialposix.Serial._reconfigure_port)

    assert "flock" in source
    assert "LOCK_EX" in source and "LOCK_NB" in source
    assert "TIOCEXCL" not in inspect.getsource(serialposix)
    assert "_reconfigure_port" in inspect.getsource(serialposix.Serial.open)


@pytest.mark.skipif(os.name != "nt", reason="the Win32 handle is where this is decided")
def test_on_windows_the_open_is_exclusive_whether_or_not_it_is_asked_for() -> None:
    """Windows has no shared mode to ask for, and pyserial says so by refusing
    the opposite: the handle is created with a share mode of zero, and setting
    `exclusive = False` raises rather than pretending. The flag is therefore a
    declaration of what is already true there, and the whole of the difference on
    POSIX."""
    from serial import serialwin32

    with pytest.raises(ValueError):
        serialwin32.Serial().exclusive = False

    assert "0,  # exclusive access" in inspect.getsource(serialwin32.Serial.open)


# ---------------------------------------------------------------------------
# Part one: the port somebody else is holding.


def test_the_open_asks_for_the_port_exclusively(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Set before the open, because after it the second holder is already there."""
    config = com_config(tmp_path)
    service = AgenticHILToolService(config)
    port = FakePort()
    install_fake_serial(monkeypatch, port)
    try:
        started = service.call("com_session_start", {"port_id": PORT_ID})

        assert started["ok"] is True, started
        assert port.exclusive_at_open is True
    finally:
        service.close()


def test_a_port_another_program_holds_refuses_instead_of_quarantining(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The incident, in one call.

    A test runner outside this project had the port. Because the open was not
    exclusive it succeeded, two processes wrote down one line, and what the
    caller eventually saw was a reply that did not answer its stimulus — an
    unconfirmed effect, which quarantined the bench and named the wrong cause.
    Exclusive opens move that to here, where it is a refusal that names the
    actual situation and leaves nothing to recover.
    """
    config = com_config(tmp_path)
    service = AgenticHILToolService(config)
    port = FakePort(held=True)
    install_fake_serial(monkeypatch, port)
    try:
        result = service.call("com_session_start", {"port_id": PORT_ID})

        assert result["error_type"] == COM_PORT_BUSY_ERROR, result
        assert result["configured_device"] == DEVICE
        assert result["target_contacted"] is False
        assert result["lease_state"] == "released"
        assert_refused_without_quarantine(result, config)
        assert service.coordinator.blocked is False
        # The refusal has to say what to do about it, and the one thing not to
        # do is sign a physical safe state for a bench nobody disturbed.
        assert result["remediation"], result
        assert any("recover" in line for line in result["do_not"]), result
    finally:
        service.close()


def test_the_port_opens_once_the_other_program_lets_go(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`retry_safe: true` has to be true. A refusal that left the bench blocked
    would make the second attempt fail on the incident rather than on the port."""
    config = com_config(tmp_path)
    service = AgenticHILToolService(config)
    port = FakePort(held=True)
    install_fake_serial(monkeypatch, port)
    try:
        refused = service.call("com_session_start", {"port_id": PORT_ID})
        assert refused["error_type"] == COM_PORT_BUSY_ERROR, refused

        port.held = False
        started = service.call("com_session_start", {"port_id": PORT_ID})

        assert started["ok"] is True, started
        assert port.opens == 2
        assert not blocking_record_states(config)
    finally:
        service.close()


def test_the_busy_refusal_is_classified_by_number_and_not_by_message() -> None:
    """A message-matched classifier answers differently under a translated libc
    and identically for a device whose name contains the words. Every number that
    means "held" is recognised; a number that means something else is not, and
    keeps the undifferentiated open failure."""
    class Entry:
        device = DEVICE

    for number in PORT_BUSY_ERRNOS:
        assert serial_port_busy(OSError(number, "held"), PORT_ID, Entry()) is not None

    # Not a holder: a device that is not there, and a permission problem that is
    # far more often a missing group membership than a second program.
    assert serial_port_busy(OSError(errno.ENOENT, "no such device"), PORT_ID, Entry()) is None
    assert serial_port_busy(OSError(errno.EACCES, "permission denied"), PORT_ID, Entry()) is None
    # Worded like a lock and carrying no number at all: still not classified.
    assert serial_port_busy(RuntimeError("Could not exclusively lock port"), PORT_ID, Entry()) is None


def test_the_busy_error_type_is_in_the_one_catalogue() -> None:
    """A refusal a caller cannot act on is a refusal that gets worked around."""
    entry = catalogue_entry(COM_PORT_BUSY_ERROR)

    assert entry is not None
    assert entry["remediation"]
    assert entry["do_not"]


# ---------------------------------------------------------------------------
# Part two: the marker, and the exception class nobody enumerated.


class NeverSeenBefore(BaseException):
    """Deliberately outside `Exception`, and outside every classifier.

    The point is not that this particular class occurs. It is that no set of
    known-innocent classes can contain it, so a failure handler that decides on
    the class has to treat it as an unknown board state — and one that decides on
    the marker does not have to know it exists at all.
    """


def test_an_exotic_failure_before_the_port_is_open_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The class-killer, on the serial adapter.

    Nothing here classifies the exception, and nothing could: it is raised where
    the handle is being configured, before any syscall touches the device, and it
    is not an `Exception`, so every `except` clause on the path steps aside for
    it. The lease goes back because the marker says the port was never opened,
    and for no other reason.

    Asked of the adapter rather than through the tool dispatch on purpose. The
    dispatch keeps a last-resort net that contains *any* exception escaping any
    hardware tool, and that net has no marker to consult because the exception
    left the adapter behind — a separate layer answering a separate question, and
    not the one this pins.
    """
    config = com_config(tmp_path)
    service = ComPortService(config)
    monkeypatch.setitem(sys.modules, "serial", SimpleNamespace(Serial=lambda *args, **kwargs: (_ for _ in ()).throw(NeverSeenBefore("nobody enumerated this"))))
    try:
        with pytest.raises(NeverSeenBefore):
            service.session_start(PORT_ID)

        assert not blocking_record_states(config)
        assert service.coordinator.blocked is False
        assert service.coordinator.leases == {}
    finally:
        service.close()


def test_an_exotic_failure_before_the_bus_is_open_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The same proof on the CAN adapter, whose classifier gap opened the week.
    The open never returned, so nothing joined a bus, so there is nothing for an
    operator to walk to a bench and inspect."""
    config = can_config(tmp_path)
    service = CanBusService(config)

    def never_opens(*args, **kwargs):
        raise NeverSeenBefore("nobody enumerated this either")

    monkeypatch.setattr("agentic_hil.can.open_adapter", never_opens)
    try:
        with pytest.raises(NeverSeenBefore):
            service.session_start(BUS_ID)

        assert not blocking_record_states(config)
        assert service.coordinator.blocked is False
        assert service.coordinator.leases == {}
    finally:
        service.close()


def test_a_failure_after_contact_still_quarantines(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The half that must not move.

    A write that failed on an open port is the case quarantine is for: the bytes
    may be on the line, the board may have acted on them, and nothing on this
    host can say. The marker changes nothing here and this test exists to prove
    it — it passes identically before and after the inversion.
    """
    config = com_config(tmp_path)
    service = AgenticHILToolService(config)
    port = FakePort()
    install_fake_serial(monkeypatch, port)
    try:
        started = service.call("com_session_start", {"port_id": PORT_ID})
        assert started["ok"] is True, started
        session = service.com_ports.sessions[PORT_ID]
        monkeypatch.setattr(session.serial_handle, "write", lambda data: (_ for _ in ()).throw(OSError("write died mid-line")))

        result = service.call("com_write", {"port_id": PORT_ID, "text": "ping\n"})

        assert result["error_type"] == "serial_write_failed", result
        assert result["quarantined"] is True
        assert result["side_effect_status"] == "unknown"
        assert result["retry_safe"] is False
        assert "com_write_effect_unconfirmed" in result["cleanup_reasons"], result
        # The marker is set — the port was open — and it is published, so the
        # release of a dead owner's devices can see that this report's
        # `not_started` would have been about a call and not about the session.
        assert result[CONTACT_MARKER_KEY], result
        assert service.coordinator.blocked is True
    finally:
        # A quarantined session stays registered for a cleanup retry, so shutting
        # the service down over it is refused as well. That is the containment
        # working, not a teardown defect.
        with suppress(RuntimeError):
            service.close()


# ---------------------------------------------------------------------------
# The marker in the report.


def test_the_report_carries_the_moment_of_contact_once_the_port_is_open(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = com_config(tmp_path)
    service = AgenticHILToolService(config)
    install_fake_serial(monkeypatch, FakePort())
    try:
        started = service.call("com_session_start", {"port_id": PORT_ID})

        assert started["ok"] is True, started
        assert started[CONTACT_MARKER_KEY], started
        # Naming the set point, because "when" without "on what evidence" is not
        # something a later reader can check.
        assert started[CONTACT_MARKER_SOURCE_KEY] == "serial_open"
        # And it survives into the committed report, which is where the release
        # of a dead owner's devices reads it.
        persisted = json.loads(Path(last_report_path(config)).read_text(encoding="utf-8"))
        assert persisted[CONTACT_MARKER_KEY] == started[CONTACT_MARKER_KEY]
    finally:
        service.close()


def test_a_refused_open_publishes_no_moment_of_contact(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Absent rather than null, and asserted against its own positive control.

    A reader has to be able to tell a session that never reached its hardware
    from a report written before the marker existed, and only presence carries
    that difference. Absence alone would be satisfied by a marker that is never
    published at all, so the same port is refused and then opened, and only the
    second one says anything.
    """
    config = com_config(tmp_path)
    service = AgenticHILToolService(config)
    port = FakePort(held=True)
    install_fake_serial(monkeypatch, port)
    try:
        refused = service.call("com_session_start", {"port_id": PORT_ID})

        assert refused["ok"] is False
        assert CONTACT_MARKER_KEY not in refused, refused
        assert CONTACT_MARKER_SOURCE_KEY not in refused, refused

        port.held = False
        started = service.call("com_session_start", {"port_id": PORT_ID})

        assert started["ok"] is True, started
        assert CONTACT_MARKER_KEY in started, started
    finally:
        service.close()


# ---------------------------------------------------------------------------
# The marker itself, including the state that is neither answer.


def test_the_marker_is_monotonic_and_names_what_set_it() -> None:
    marker = ContactMarker()
    assert marker.proves_no_contact is True
    assert marker.made is False
    assert marker.report_fields() == {}

    marker.record("first_proof")
    first = marker.at
    marker.record("a_later_proof")

    assert marker.made is True
    assert marker.proves_no_contact is False
    assert marker.at == first
    assert marker.by == "first_proof"
    assert marker.report_fields() == {CONTACT_MARKER_KEY: first, CONTACT_MARKER_SOURCE_KEY: "first_proof"}


def test_contact_that_cannot_be_ruled_out_is_not_read_as_innocence() -> None:
    """The third state, and the reason the marker is not a boolean.

    A bridge that was handed an open request and never answered leaves its own
    side to itself. Reading that silence as "never on the bus" is exactly the
    reasoning quarantine exists to refuse, so the marker withholds both answers
    and the failure keeps its containment.
    """
    marker = ContactMarker()
    marker.record_unproven("open_request_unanswered")

    assert marker.made is False
    assert marker.proves_no_contact is False
    assert marker.report_fields() == {}
    assert no_contact_refusal({"ok": False, "error_type": "whatever"}, marker) == {"ok": False, "error_type": "whatever"}

    # And it never overwrites a contact that was proven.
    marker.record("bus_constructed")
    marker.record_unproven("too_late")
    assert marker.made is True


def test_a_broken_ledger_quarantines_whatever_the_marker_says() -> None:
    """The one rule the marker has no standing over. A bench whose audit trail
    cannot be written proves nothing about anything, including about itself, and
    that predates the marker and outlives it. The same holds for a result that
    already carries `cleanup_required`: that is usually about something still
    running on this host, and a live process can act whatever the bus state was
    when it started."""
    marker = ContactMarker()
    assert marker.proves_no_contact is True

    audit_broken = {"ok": False, "error_type": "com_port_open_failed", "audit_ok": False}
    still_live = {"ok": False, "error_type": "can_adapter_open_failed", "cleanup_required": True}

    assert no_contact_refusal(audit_broken, marker) == audit_broken
    assert no_contact_refusal(still_live, marker) == still_live

    plain = no_contact_refusal({"ok": False, "error_type": "com_port_open_failed"}, marker)
    assert plain["target_contacted"] is False
    assert plain["side_effect_status"] == "not_started"
    assert plain["retry_safe"] is True


def test_an_explicit_retry_claim_from_a_backend_survives_the_refusal() -> None:
    """A backend that said a retry cannot settle its own ambiguity is making a
    claim this layer has no evidence against, so the refusal defaults the field
    rather than overwriting it."""
    refusal = no_contact_refusal({"ok": False, "error_type": "com_port_open_failed", "retry_safe": False}, ContactMarker())

    assert refusal["retry_safe"] is False
    assert refusal["side_effect_status"] == "not_started"
