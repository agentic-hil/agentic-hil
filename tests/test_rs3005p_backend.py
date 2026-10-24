"""The RS-3005P backend (#735) against a fake transport, and nothing else.

The fake answers what issue #730 recorded (2026-10-22, firmware V6.9, queries
only), read from `fixtures/rs3005p_v6_9_issue_transcription.json`: every
newline-terminated command gets its reply with a trailing newline, and a command
written before the previous reply was read is not answered, as the recording
found for two queries sent back to back. Output switching answers as #736
recorded it (2026-10-24, `fixtures/rs3005p_v6_9_output_recording.json`): `OUT0`
and `OUT1` answer nothing and turn the STATUS byte to what was read after them.
What nobody recorded (the setter reply, any other bit of the STATUS byte and the
protections) the fake does not invent either: the backend has to fail closed on
it, and these tests hold it to that.

No test here opens a port. The factory's own test replaces pyserial's `Serial`
before anything is built, and names a device that does not exist.
"""

from __future__ import annotations

import json
import uuid
from contextlib import suppress
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agentic_hil.types import ChannelStatus, PowerSupplyFault

FIXTURE = Path(__file__).parent / "fixtures" / "rs3005p_v6_9_issue_transcription.json"
OUTPUT_FIXTURE = Path(__file__).parent / "fixtures" / "rs3005p_v6_9_output_recording.json"
AT = "2026-10-22T12:00:00.000Z"
# What one read of the fake takes: the recording's first reply byte came 13 to
# 44 ms after the write.
READ_TAKES_S = 0.013


def transcription() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def exchanges() -> dict[str, dict[str, Any]]:
    return {exchange["command"]: exchange for exchange in transcription()["exchanges"]}


def output_recording() -> dict[str, Any]:
    return json.loads(OUTPUT_FIXTURE.read_text(encoding="utf-8"))


def switches() -> dict[bytes, dict[str, Any]]:
    return {switch["command"].encode(): switch for switch in output_recording()["switches"]}


def synthetic_serial() -> str:
    return uuid.uuid4().hex[:8].upper()


class FakeSupply:
    """A transport that answers as the transcription does, and records every byte it is handed.

    Setters are taken silently: what the supply answers to one is not recorded,
    so the fake answers nothing, and the next query reads the value back as it
    was written. That read-back format is the fake's assumption, not a fact."""

    def __init__(
        self,
        *,
        idn: bytes | None = None,
        status: bytes = b"\x53\n",
        silent: tuple[bytes, ...] = (),
        cut_short: dict[bytes, bytes] | None = None,
        write_fails: dict[bytes, BaseException] | None = None,
        read_fails: dict[bytes, BaseException] | None = None,
    ) -> None:
        self.answers: dict[bytes, bytes] = {name.encode(): exchange["reply"].encode() + b"\n" for name, exchange in exchanges().items() if "reply" in exchange}
        self.answers[b"STATUS?"] = status
        if idn is not None:
            self.answers[b"*IDN?"] = idn
        self.answers.update(cut_short or {})
        self.silent = set(silent)
        # The serial line failing as pyserial fails: on writing a command, or on
        # reading the reply to the command written last.
        self.write_fails = dict(write_fails or {})
        self.read_fails = dict(read_fails or {})
        self.last_command = b""
        self.events: list[tuple[str, Any]] = []
        self.pending = bytearray()
        self.unanswered: list[bytes] = []
        self.now = 0.0
        self.closed = False

    def write(self, data: bytes) -> int:
        self.events.append(("write", bytes(data)))
        self.last_command = bytes(data).removesuffix(b"\n")
        if self.last_command in self.write_fails:
            raise self.write_fails[self.last_command]
        if self.pending:
            self.unanswered.append(bytes(data))
            return len(data)
        command = bytes(data).removesuffix(b"\n")
        recorded = switches().get(command)
        if recorded is not None:
            self.answers[b"STATUS?"] = bytes.fromhex(recorded["after"]["STATUS?"]) + b"\n"
            return len(data)
        for setter, query in ((b"VSET1:", b"VSET1?"), (b"ISET1:", b"ISET1?")):
            if command.startswith(setter):
                self.answers[query] = command[len(setter) :] + b"\n"
                return len(data)
        if command not in self.silent:
            self.pending += self.answers.get(command, b"")
        return len(data)

    def read_until(self, terminator: bytes = b"\n") -> bytes:
        self.now += READ_TAKES_S
        if self.last_command in self.read_fails:
            raise self.read_fails[self.last_command]
        end = self.pending.find(terminator)
        size = len(self.pending) if end < 0 else end + len(terminator)
        reply = bytes(self.pending[:size])
        del self.pending[:size]
        self.events.append(("read", reply))
        return reply

    def read(self, size: int = 1) -> bytes:
        self.now += READ_TAKES_S
        if self.last_command in self.read_fails:
            raise self.read_fails[self.last_command]
        reply = bytes(self.pending[:size])
        del self.pending[:size]
        self.events.append(("read", reply))
        return reply

    def close(self) -> None:
        self.events.append(("close", None))
        self.closed = True

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.events.append(("sleep", seconds))
        self.now += seconds


def written(fake: FakeSupply) -> list[bytes]:
    return [data for kind, data in fake.events if kind == "write"]


def backend(fake: FakeSupply):
    from agentic_hil.backends.rs3005p import RS3005PBackend

    return RS3005PBackend(fake, clock=lambda: AT, monotonic=fake.monotonic, sleep=fake.sleep)


def accepted(fake: FakeSupply):
    supply = backend(fake)
    assert supply.identity().accepted is True
    return supply


# -- identity ----------------------------------------------------------------


def test_the_identity_is_the_identity_query_alone_and_keeps_model_firmware_and_serial() -> None:
    serial = synthetic_serial()
    fake = FakeSupply(idn=f"RS-3005P V6.9 SN:{serial}\n".encode())

    identity = backend(fake).identity()

    assert written(fake) == [b"*IDN?\n"]
    assert (identity.model, identity.firmware, identity.accepted, identity.serial) == ("RS-3005P", "V6.9", True, serial)


def test_the_transcribed_identity_is_accepted_with_its_serial_redacted() -> None:
    assert exchanges()["*IDN?"]["reply"] == "RS-3005P V6.9 SN:[redacted]"

    identity = backend(FakeSupply()).identity()

    assert (identity.model, identity.firmware, identity.accepted, identity.serial) == ("RS-3005P", "V6.9", True, "[redacted]")


@pytest.mark.parametrize(
    ("reply", "model", "firmware"),
    [
        ("RS-3005P V7.0 SN:{serial}\n", "RS-3005P", "V7.0"),
        ("KA3005P V5.8 SN:{serial}\n", "KA3005P", "V5.8"),
        ("RS-3005P V6.9\n", None, None),
        ("{serial}\n", None, None),
        ("\n", None, None),
    ],
    ids=["other-firmware", "other-model", "no-serial-span", "unparsed", "empty"],
)
def test_an_identity_other_than_rs3005p_v6_9_is_not_accepted_and_nothing_else_is_written(reply: str, model: str | None, firmware: str | None) -> None:
    fake = FakeSupply(idn=reply.format(serial=synthetic_serial()).encode())
    supply = backend(fake)

    identity = supply.identity()

    assert (identity.model, identity.firmware, identity.accepted) == (model, firmware, False)
    for operation in (lambda: supply.channel_status(1), lambda: supply.set_voltage(1, Decimal("5.00")), lambda: supply.set_current(1, Decimal("0.100"))):
        with pytest.raises(RuntimeError):
            operation()
    assert written(fake) == [b"*IDN?\n"]


def test_nothing_is_sent_before_the_identity_was_read() -> None:
    fake = FakeSupply()
    supply = backend(fake)

    for operation in (lambda: supply.channel_status(1), lambda: supply.set_voltage(1, Decimal("5.00")), lambda: supply.set_current(1, Decimal("0.100"))):
        with pytest.raises(RuntimeError):
            operation()

    assert written(fake) == []


# -- queries -----------------------------------------------------------------


def test_the_transcribed_queries_decode_exactly_and_what_is_not_recorded_reads_none() -> None:
    from agentic_hil.backends.rs3005p import STATUS_TABLES

    assert transcription()["how"] == "transcribed from issue #730; raw bytes not available; not a recording made by this run"
    assert (transcription()["model"], transcription()["firmware"], transcription()["recorded_on"]) == ("RS-3005P", "V6.9", "2026-10-22")
    fake = FakeSupply()
    supply = accepted(fake)

    status = supply.channel_status(1)

    assert written(fake)[1:] == [b"VSET1?\n", b"ISET1?\n", b"VOUT1?\n", b"IOUT1?\n", b"STATUS?\n"]
    assert [str(value) for value in (status.set_voltage_v, status.set_current_a, status.measured_voltage_v, status.measured_current_a)] == ["24.00", "2.000", "24.00", "0.000"]
    assert status == ChannelStatus(
        set_voltage_v=Decimal("24.00"),
        set_current_a=Decimal("2.000"),
        measured_voltage_v=Decimal("24.00"),
        measured_current_a=Decimal("0.000"),
        output_on=True,
        regulation=None,
        protection_tripped=None,
        ovp_threshold_v=None,
        ocp_threshold_a=None,
        read_at=AT,
    )
    assert STATUS_TABLES == {("RS-3005P", "V6.9"): {"output_bit": 6}}


@pytest.mark.parametrize("framing", [b"\x53\n", b"\x53"], ids=["with-newline", "byte-alone"])
def test_the_status_byte_is_kept_as_evidence_and_only_its_output_bit_is_read(framing: bytes) -> None:
    status_exchange = exchanges()["STATUS?"]
    assert (status_exchange["reply_raw_hex"], status_exchange["note"]) == ("53", "state not recorded, no meaning derived")
    fake = FakeSupply(status=framing)
    supply = accepted(fake)

    status = supply.channel_status(1)
    supply.set_voltage(1, Decimal("5.00"))

    recorded = [entry for entry in supply.evidence() if entry["command"] == "STATUS?"]
    assert [entry["reply_hex"] for entry in recorded] == [framing.hex()]
    assert (status.output_on, status.regulation, status.protection_tripped) == (True, None, None)
    assert fake.unanswered == []


def test_a_status_reply_longer_than_one_byte_and_a_newline_ends_the_conversation() -> None:
    """Its framing is not recorded: whatever follows could be read as the next command's reply."""
    fake = FakeSupply(status=b"\x53\x41\n")
    supply = accepted(fake)

    # The read that met the framing fails too: a status taken from a
    # conversation that has just ended is not one the caller can rely on.
    with pytest.raises(PowerSupplyFault) as fault:
        supply.channel_status(1)
    sent = written(fake)
    with pytest.raises(RuntimeError):
        supply.set_voltage(1, Decimal("5.00"))

    assert (fault.value.error_type, fault.value.wrote) == ("power_supply_no_reply", False)
    assert "STATUS?" in fault.value.summary, fault.value.summary
    assert written(fake) == sent
    assert [entry["reply_hex"] for entry in supply.evidence() if entry["command"] == "STATUS?"] == ["5341"]


def _before_identity(fake: FakeSupply) -> Any:
    return backend(fake)


def _ended(fake: FakeSupply) -> Any:
    supply = accepted(fake)
    with suppress(PowerSupplyFault):
        supply.channel_status(1)
    return supply


def _closed(fake: FakeSupply) -> Any:
    supply = accepted(fake)
    supply.close()
    return supply


@pytest.mark.parametrize(
    ("fake", "situation", "operation"),
    [
        (lambda: FakeSupply(), _before_identity, lambda supply: supply.set_voltage(1, Decimal("5.00"))),
        (lambda: FakeSupply(idn=b"KA3005P V5.8 SN:SYNTH\n"), lambda fake: (supply := backend(fake), supply.identity())[0], lambda supply: supply.set_current(1, Decimal("0.100"))),
        (lambda: FakeSupply(silent=(b"VOUT1?",)), _ended, lambda supply: supply.set_voltage(1, Decimal("5.00"))),
        (lambda: FakeSupply(), _closed, lambda supply: supply.set_voltage(1, Decimal("5.00"))),
        (lambda: FakeSupply(), _closed, lambda supply: supply.set_output(1, True)),
    ],
    ids=["before-identity", "identity-not-accepted", "conversation-ended", "closed", "output-switching-closed"],
)
def test_a_command_the_backend_refuses_is_a_fault_that_wrote_nothing(fake: Any, situation: Any, operation: Any) -> None:
    """The layer above learns from the fault itself that nothing reached the supply."""
    transport = fake()
    supply = situation(transport)
    sent = written(transport)

    with pytest.raises(PowerSupplyFault) as fault:
        operation(supply)

    assert isinstance(fault.value, RuntimeError)
    assert (fault.value.error_type, fault.value.wrote, fault.value.cause) == ("power_supply_no_reply", False, "not_sent")
    assert written(transport) == sent


def test_every_command_ends_in_a_newline_and_waits_for_the_previous_reply() -> None:
    fake = FakeSupply()
    supply = accepted(fake)

    supply.channel_status(1)
    supply.set_voltage(1, Decimal("12.34"))
    supply.set_current(1, Decimal("1.234"))
    supply.channel_status(1)

    assert fake.unanswered == []
    assert all(command.endswith(b"\n") and command.count(b"\n") == 1 for command in written(fake))


@pytest.mark.parametrize("channel", [0, 2])
def test_the_rs3005p_has_one_channel(channel: int) -> None:
    fake = FakeSupply()
    supply = accepted(fake)

    for operation in (lambda: supply.channel_status(channel), lambda: supply.set_voltage(channel, Decimal("5.00")), lambda: supply.set_current(channel, Decimal("0.100"))):
        with pytest.raises(ValueError):
            operation()

    assert written(fake) == [b"*IDN?\n"]


# -- setters -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("operation", "value", "setter", "query"),
    [
        ("set_voltage", "12.34", b"VSET1:12.34\n", b"VSET1?\n"),
        ("set_voltage", "5", b"VSET1:5.00\n", b"VSET1?\n"),
        ("set_current", "1.234", b"ISET1:1.234\n", b"ISET1?\n"),
        ("set_current", "0.5", b"ISET1:0.500\n", b"ISET1?\n"),
    ],
)
def test_a_setter_writes_the_exact_value_waits_the_draft_gap_and_reads_it_back(operation: str, value: str, setter: bytes, query: bytes) -> None:
    from agentic_hil.backends.rs3005p import SETTER_GAP_S

    fake = FakeSupply()
    supply = accepted(fake)
    fake.events.clear()

    getattr(supply, operation)(1, Decimal(value))

    assert fake.events[:3] == [("write", setter), ("sleep", SETTER_GAP_S), ("write", query)]
    entries = supply.evidence()[-2:]
    assert [entry["command"] for entry in entries] == [setter.decode().strip(), query.decode().strip()]
    assert [entry["operation"] for entry in entries] == [operation, operation]
    assert entries[0]["reply_hex"] == "" and entries[0]["draft"]
    assert entries[1]["reply_hex"] == (setter[len(b"VSET1:") :]).hex()


@pytest.mark.parametrize(
    ("operation", "value"),
    [("set_voltage", "12.345"), ("set_current", "1.2345"), ("set_voltage", "-1.00"), ("set_current", "-0.001"), ("set_voltage", "NaN"), ("set_current", "Infinity")],
)
def test_a_value_the_supply_cannot_take_exactly_is_refused_before_it_is_written(operation: str, value: str) -> None:
    fake = FakeSupply()
    supply = accepted(fake)

    with pytest.raises(ValueError):
        getattr(supply, operation)(1, Decimal(value))

    assert written(fake) == [b"*IDN?\n"]


# -- no reply ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("silent", "steps", "wrote", "last_command"),
    [
        (b"*IDN?", (), False, b"*IDN?\n"),
        (b"VOUT1?", (lambda supply: supply.channel_status(1),), False, b"VOUT1?\n"),
        (b"STATUS?", (lambda supply: supply.channel_status(1),), False, b"STATUS?\n"),
        (b"VSET1?", (lambda supply: supply.set_voltage(1, Decimal("5.00")),), True, b"VSET1?\n"),
        (b"ISET1?", (lambda supply: supply.set_current(1, Decimal("0.100")),), True, b"ISET1?\n"),
    ],
    ids=["identity", "measured-voltage", "status", "voltage-read-back", "current-read-back"],
)
def test_a_reply_that_never_comes_is_no_reply_and_says_whether_anything_was_written(silent: bytes, steps: tuple, wrote: bool, last_command: bytes) -> None:
    fake = FakeSupply(silent=(silent,), status=b"" if silent == b"STATUS?" else b"\x53\n")
    supply = backend(fake)

    with pytest.raises(PowerSupplyFault) as fault:
        supply.identity()
        for step in steps:
            step(supply)

    assert (fault.value.error_type, fault.value.wrote) == ("power_supply_no_reply", wrote)
    assert written(fake)[-1] == last_command
    assert supply.classify_last_error()["last_fault"] == {"error_type": "power_supply_no_reply", "summary": fault.value.summary, "wrote": wrote}


@pytest.mark.parametrize("command", [b"*IDN?", b"ISET1?"])
def test_a_reply_cut_short_of_its_newline_is_no_reply(command: bytes) -> None:
    fake = FakeSupply(cut_short={command: b"RS-3005P V6" if command == b"*IDN?" else b"2.0"})
    supply = backend(fake)

    with pytest.raises(PowerSupplyFault) as fault:
        supply.identity()
        supply.channel_status(1)

    assert (fault.value.error_type, fault.value.wrote) == ("power_supply_no_reply", False)
    assert written(fake)[-1] == command + b"\n"


def test_no_fault_is_classified_before_one_was_raised() -> None:
    supply = accepted(FakeSupply())

    supply.channel_status(1)

    assert supply.classify_last_error()["last_fault"] is None


def _write_timeout() -> BaseException:
    import serial

    return serial.SerialTimeoutException("Write timeout")


def _line_lost() -> BaseException:
    import serial

    return serial.SerialException("device reports readiness to read but returned no data (device disconnected or multiple access on port?)")


@pytest.mark.parametrize(
    ("failure", "step", "cause", "wrote"),
    [
        (lambda: {"write_fails": {b"VOUT1?": _write_timeout()}}, lambda supply: supply.channel_status(1), "write_timeout", False),
        (lambda: {"write_fails": {b"VSET1:5.00": _write_timeout()}}, lambda supply: supply.set_voltage(1, Decimal("5.00")), "write_timeout", True),
        (lambda: {"read_fails": {b"IOUT1?": _line_lost()}}, lambda supply: supply.channel_status(1), "serial_error", False),
        (lambda: {"read_fails": {b"ISET1?": _line_lost()}}, lambda supply: supply.set_current(1, Decimal("0.100")), "serial_error", True),
    ],
    ids=["query-write-timeout", "setter-write-timeout", "query-read-fails", "read-back-read-fails"],
)
def test_a_serial_line_that_fails_is_no_reply_naming_its_cause_and_whether_it_wrote(failure: Any, step: Any, cause: str, wrote: bool) -> None:
    """pyserial's own errors reach the class layer as the catalogue fault, never raw."""
    failures = failure()
    (error,) = [*failures.get("write_fails", {}).values(), *failures.get("read_fails", {}).values()]
    fake = FakeSupply(**failures)
    supply = accepted(fake)

    with pytest.raises(PowerSupplyFault) as fault:
        step(supply)

    assert (fault.value.error_type, fault.value.cause, fault.value.wrote) == ("power_supply_no_reply", cause, wrote)
    assert type(error).__name__ in fault.value.summary and str(error) in fault.value.summary, fault.value.summary
    assert supply.classify_last_error()["last_fault"]["error_type"] == "power_supply_no_reply"
    sent = written(fake)
    with pytest.raises(RuntimeError):
        supply.channel_status(1)
    assert written(fake) == sent


def test_a_port_that_will_not_open_is_no_reply_and_nothing_was_written(monkeypatch: pytest.MonkeyPatch) -> None:
    import serial

    from agentic_hil.backends import rs3005p

    refused = "could not open port 'psu': PermissionError(13, 'Access is denied.', None, 5)"

    class RefusingSerial(FakeSerial):
        def open(self) -> None:
            raise serial.SerialException(refused)

    monkeypatch.setattr(FakeSerial, "built", [])
    monkeypatch.setattr(serial, "Serial", RefusingSerial)
    supply = rs3005p.rs3005p_factory(a_config(f"psu-{uuid.uuid4().hex[:8]}"), "bench")

    with pytest.raises(PowerSupplyFault) as fault:
        supply.identity()

    assert (fault.value.error_type, fault.value.cause, fault.value.wrote) == ("power_supply_no_reply", "port_open_failed", False)
    assert refused in fault.value.summary, fault.value.summary
    (handle,) = FakeSerial.built
    assert handle.sent == [] and handle.is_open is False
    with pytest.raises(RuntimeError):
        supply.channel_status(1)
    supply.close()
    assert handle.sent == []


# -- what is never sent ------------------------------------------------------


@pytest.mark.parametrize("command", [b"OUT0", b"OUT1"])
def test_output_switching_sends_the_recorded_command_and_reads_the_output_bit_back(command: bytes) -> None:
    switch = switches()[command]
    fake = FakeSupply(status=bytes.fromhex(switch["before"]["STATUS?"]) + b"\n")
    supply = accepted(fake)
    on = command == b"OUT1"

    assert supply.channel_status(1).output_on is (not on)
    supply.set_output(1, on)
    status = supply.channel_status(1)

    assert written(fake)[6] == command + b"\n"
    assert fake.events[fake.events.index(("write", command + b"\n")) + 1] == ("sleep", 0.1)
    assert (status.output_on, status.regulation) == (on, None)
    entry = next(entry for entry in supply.evidence() if entry["command"] == command.decode())
    assert (entry["operation"], entry["reply_hex"], entry["latency_ms"], entry["gap_s"]) == ("set_output", "", None, 0.1)
    assert fake.unanswered == []


@pytest.mark.parametrize("on", [True, False])
def test_output_switching_is_not_sent_to_a_firmware_without_a_status_table(on: bool, monkeypatch: pytest.MonkeyPatch) -> None:
    from agentic_hil.backends import rs3005p

    monkeypatch.setattr(rs3005p, "STATUS_TABLES", {})
    fake = FakeSupply()
    supply = accepted(fake)

    with pytest.raises(PowerSupplyFault) as fault:
        supply.set_output(1, on)

    assert (fault.value.error_type, fault.value.wrote, fault.value.cause) == ("power_supply_no_reply", False, "not_sent")
    assert supply.channel_status(1).output_on is None
    assert b"OUT0\n" not in written(fake) and b"OUT1\n" not in written(fake)


def test_arming_nothing_sends_nothing_and_arming_a_threshold_fails_before_contact() -> None:
    fake = FakeSupply()
    supply = accepted(fake)

    supply.arm_protection(1, None, None)
    faults = []
    for ovp, ocp in ((Decimal("12.00"), None), (None, Decimal("1.000")), (Decimal("12.00"), Decimal("1.000"))):
        with pytest.raises(PowerSupplyFault) as fault:
            supply.arm_protection(1, ovp, ocp)
        faults.append((fault.value.error_type, fault.value.wrote))

    assert faults == [("power_supply_protection_not_armed", False)] * 3
    assert written(fake) == [b"*IDN?\n"]


def test_no_protection_command_is_ever_sent_and_the_output_only_as_out0_or_out1() -> None:
    fake = FakeSupply()
    supply = accepted(fake)

    supply.channel_status(1)
    supply.set_voltage(1, Decimal("5.00"))
    supply.set_current(1, Decimal("0.500"))
    supply.arm_protection(1, None, None)
    for attempt in (lambda: supply.arm_protection(1, Decimal("5.00"), Decimal("0.500")), lambda: supply.set_output(1, True), lambda: supply.set_output(1, False)):
        with suppress(PowerSupplyFault, RuntimeError):
            attempt()
    supply.channel_status(1)

    sent = [command.upper() for command in written(fake)]
    assert not [command for command in sent if b"OVP" in command or b"OCP" in command]
    assert [command for command in sent if b"OUT" in command and command not in {b"VOUT1?\n", b"IOUT1?\n"}] == [b"OUT1\n", b"OUT0\n"]


# -- capabilities -------------------------------------------------------------


def test_the_capabilities_are_the_type_record_and_touch_nothing() -> None:
    from agentic_hil.backends.rs3005p import STATUS_TABLES
    from agentic_hil.power_supply import POWER_SUPPLY_TYPES

    fake = FakeSupply()
    supply = backend(fake)

    capabilities = supply.capabilities()
    capabilities["channels"] = 7

    assert supply.capabilities() == POWER_SUPPLY_TYPES["rs3005p"]
    assert POWER_SUPPLY_TYPES["rs3005p"]["channels"] == 1
    assert supply.capabilities()["protection"] == {"ovp": "unsupported", "ocp": "unsupported"}
    assert supply.capabilities()["protection_readable"] == {"ovp": False, "ocp": False}
    assert supply.capabilities()["output_readable"] is True and set(STATUS_TABLES) == {("RS-3005P", "V6.9")}
    assert fake.events == []


def test_the_setter_format_carries_exactly_the_type_resolution() -> None:
    """The draft setter format writes as many places as the record's resolution has, and no more."""
    from agentic_hil.backends.rs3005p import CURRENT_PLACES, VOLTAGE_PLACES
    from agentic_hil.power_supply import POWER_SUPPLY_TYPES

    ranges = POWER_SUPPLY_TYPES["rs3005p"]["ranges"]["1"]

    assert -Decimal(ranges["voltage"]["resolution"]).as_tuple().exponent == VOLTAGE_PLACES
    assert -Decimal(ranges["current"]["resolution"]).as_tuple().exponent == CURRENT_PLACES


# -- evidence ----------------------------------------------------------------


@pytest.mark.parametrize("reply", ["RS-3005P V6.9 SN:{serial}\n", "KA3005P V5.8 SN:{serial}\n", "RS-3005P V6.9 SN:[redacted]\n"], ids=["accepted", "unknown-model", "transcribed"])
def test_the_evidence_masks_the_serial_span_and_keeps_the_reply_length(reply: str) -> None:
    serial = synthetic_serial()
    raw = reply.format(serial=serial).encode()
    supply = backend(FakeSupply(idn=raw))

    supply.identity()

    (entry,) = supply.evidence()
    masked = bytes.fromhex(entry["reply_hex"])
    span = raw.index(b"SN:") + len(b"SN:")
    assert (entry["operation"], entry["command"]) == ("identity", "*IDN?")
    assert len(masked) == len(raw)
    assert masked[:span] == raw[:span] and masked.endswith(b"\n")
    assert set(masked[span:-1]) == {ord("*")}
    assert serial not in json.dumps(supply.evidence()) and serial.encode().hex() not in entry["reply_hex"].lower()


def test_an_identity_reply_that_does_not_parse_is_masked_whole() -> None:
    """Nothing says where a serial sits in a reply nobody recorded, so none of it is kept."""
    serial = synthetic_serial()
    raw = f"PSU-9 {serial} 1.0\n".encode()
    supply = backend(FakeSupply(idn=raw))

    supply.identity()

    masked = bytes.fromhex(supply.evidence()[0]["reply_hex"])
    assert len(masked) == len(raw) and masked == b"*" * (len(raw) - 1) + b"\n"


def test_each_evidence_entry_carries_operation_command_reply_latency_and_time() -> None:
    fake = FakeSupply()
    supply = accepted(fake)

    supply.channel_status(1)

    entries = supply.evidence()
    assert [entry["command"] for entry in entries] == ["*IDN?", "VSET1?", "ISET1?", "VOUT1?", "IOUT1?", "STATUS?"]
    assert all({"operation", "command", "reply_hex", "latency_ms", "at"} <= entry.keys() for entry in entries)
    assert all(entry["at"] == AT for entry in entries)
    assert entries[1]["reply_hex"] == b"24.00\n".hex()
    assert entries[0]["latency_ms"] == pytest.approx(READ_TAKES_S * 1000)
    assert entries[-1]["latency_ms"] == pytest.approx(2 * READ_TAKES_S * 1000)


def test_the_evidence_is_a_copy() -> None:
    supply = accepted(FakeSupply())

    supply.evidence().clear()
    supply.evidence()[0]["reply_hex"] = "00"

    assert len(supply.evidence()) == 1 and supply.evidence()[0]["reply_hex"] != "00"


# -- closing -----------------------------------------------------------------


def test_close_closes_the_transport_and_ends_the_conversation() -> None:
    fake = FakeSupply()
    supply = accepted(fake)

    supply.close()
    supply.close()
    with pytest.raises(RuntimeError):
        supply.channel_status(1)

    assert fake.closed is True
    assert written(fake) == [b"*IDN?\n"]


# -- the factory ---------------------------------------------------------------


class FakeSerial:
    """pyserial's `Serial` as the factory's transport builds it: unopened, configured, then opened."""

    built: list[FakeSerial] = []

    def __init__(self) -> None:
        self.is_open = False
        self.opened_with: dict[str, Any] | None = None
        self.sent: list[bytes] = []
        FakeSerial.built.append(self)

    def open(self) -> None:
        self.opened_with = {name: value for name, value in vars(self).items() if name not in {"is_open", "opened_with", "sent"}}
        self.is_open = True

    def write(self, data: bytes) -> int:
        self.sent.append(bytes(data))
        return len(data)

    def read_until(self, expected: bytes = b"\n", size: int | None = None) -> bytes:
        return b"RS-3005P V6.9 SN:[redacted]\n"

    def read(self, size: int = 1) -> bytes:
        return b""

    def close(self) -> None:
        self.is_open = False


def a_config(device: str) -> SimpleNamespace:
    port = SimpleNamespace(device=device, baudrate=115200, timeout_s=9.0, write_timeout_s=0.4, assert_dtr=False, assert_rts=True)
    supply = SimpleNamespace(type="rs3005p", com_port="psu", timeout_s=0.75)
    return SimpleNamespace(power_supplies={"bench": supply}, com_ports={"psu": port})


def test_the_factory_opens_the_configured_port_at_9600_8n1_only_at_the_first_command(monkeypatch: pytest.MonkeyPatch) -> None:
    import serial

    from agentic_hil.backends import rs3005p

    monkeypatch.setattr(FakeSerial, "built", [])
    monkeypatch.setattr(serial, "Serial", FakeSerial)
    device = f"psu-{uuid.uuid4().hex[:8]}"

    supply = rs3005p.rs3005p_factory(a_config(device), "bench")
    built_before_contact = list(FakeSerial.built)
    identity = supply.identity()

    assert built_before_contact == []
    assert identity.accepted is True
    (handle,) = FakeSerial.built
    assert handle.opened_with == {
        "port": device,
        "baudrate": 9600,
        "bytesize": serial.EIGHTBITS,
        "parity": serial.PARITY_NONE,
        "stopbits": serial.STOPBITS_ONE,
        "xonxoff": False,
        "rtscts": False,
        "dsrdtr": False,
        "timeout": 0.75,
        "write_timeout": 0.4,
        "dtr": False,
        "rts": True,
        "exclusive": True,
    }
    assert handle.sent == [b"*IDN?\n"]
    supply.close()
    assert handle.is_open is False


def test_a_factory_backend_closed_before_any_command_never_opens_the_port(monkeypatch: pytest.MonkeyPatch) -> None:
    import serial

    from agentic_hil.backends import rs3005p

    monkeypatch.setattr(FakeSerial, "built", [])
    monkeypatch.setattr(serial, "Serial", FakeSerial)

    rs3005p.rs3005p_factory(a_config(f"psu-{uuid.uuid4().hex[:8]}"), "bench").close()

    assert FakeSerial.built == []
