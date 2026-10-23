"""The RS-3005P backend: one bench power supply over its USB serial line.

The RS-3005P speaks the KA3005P command set at 9600 8N1 with no flow control.
What this module sends and how it reads the answers is what issue #730 recorded
on 2026-10-22 from a unit on firmware V6.9, with queries only:

* ``*IDN?`` answers ``RS-3005P V6.9 SN:<serial>``;
* ``VSET1?``, ``ISET1?``, ``VOUT1?`` and ``IOUT1?`` answer the set and the
  measured voltage and current as decimal text, ``24.00`` and ``2.000``;
* ``STATUS?`` answers one raw byte;
* a command terminated by a newline is answered with a trailing newline;
* two commands written back to back get only the first one answered, so a
  command is written only after the previous reply was read.

Everything else is unrecorded, and the module fails closed on it rather than
guess:

* The STATUS byte is kept as evidence and given no meaning. ``STATUS_TABLES``,
  keyed by (model, firmware), is filled only from recordings and is empty, so
  the output state, the regulation mode and a protection trip read as ``None``.
* Output switching is never sent: ``set_output`` refuses before contact, and the
  type record says the output state cannot be read, so the layer above answers
  ``not_supported`` before it would ever call it.
* No protection command is sent. The type record declares OVP and OCP
  unsupported, and ``arm_protection`` with a threshold raises
  ``power_supply_protection_not_armed`` before contact.
* The setter format (``VSET1:12.34``, ``ISET1:1.234``) is a draft, inferred
  from the query replies: as many places as the query answers carry. What the
  supply answers to a setter is not recorded, so no reply is read; the backend
  waits ``SETTER_GAP_S``, also a draft, and reads the set point back with its
  query. The layer above compares that read-back exactly before a write counts.
* How ``STATUS?`` is framed is not recorded. It is read as one byte and an
  optional newline; anything else after the byte is ``power_supply_no_reply``
  and ends the conversation, because it could otherwise be read as the answer
  to the next command.
* A call this backend refuses before sending anything (on a conversation that
  ended or was closed, before an accepted identity, or to switch the output) is
  ``RS3005PRefused``: ``power_supply_no_reply`` with the cause ``not_sent`` and
  nothing written.

A reply that does not come, or comes without its newline, is
``power_supply_no_reply``, and it too ends the conversation: a late reply must
never be read as the answer to a later command. So is a serial line that fails
under the conversation: a port that does not open, a write that times out, any
other error pyserial raises. The fault names that cause and says whether a set
point could have reached the supply, and the error itself is in its summary.

Every exchange is kept as evidence: the command, the reply as hex, its latency
and the time. The ``SN:`` span of an identity reply is masked before it is kept,
with its length preserved, and a reply that does not parse as an identity is
masked whole, since nothing says where a serial would sit in it.

The transport is a narrow interface (``write``, ``read_until``, ``read``,
``close``); ``rs3005p_factory`` builds the one over pyserial, which opens the
configured port at the first command and not before.
"""

from __future__ import annotations

import copy
import re
import time
from collections.abc import Callable
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, NoReturn, Protocol

from agentic_hil.types import ChannelStatus, JsonObject, PowerSupplyFault, SupplyIdentity

TYPE = "rs3005p"
CHANNEL = 1
BAUDRATE = 9600
TERMINATOR = b"\n"

# The identities this backend drives. Only a unit whose `*IDN?` reply has been
# recorded is accepted; another model or firmware is read and named, and not
# driven.
ACCEPTED_IDENTITIES = frozenset({("RS-3005P", "V6.9")})

# The meaning of the STATUS byte per (model, firmware), from recordings only.
# None exists yet, so no bit of it is read.
STATUS_TABLES: dict[tuple[str, str], dict[str, Any]] = {}

# Draft, not measured: how long a setter is given before its read-back query.
# The recording saw the first reply byte of a query 13 to 44 ms after the write;
# what a setter takes is not recorded.
SETTER_GAP_S = 0.1

# Draft, inferred from the query replies (`24.00`, `2.000`): the places a setter
# writes. They match the resolution of the type record.
VOLTAGE_PLACES = 2
CURRENT_PLACES = 3

SETTER_DRAFT = "draft: the setter format is inferred from the query replies, and the setter's own reply is not recorded, so none is read"

_IDENTITY = re.compile(rb"(\S+) (V\S+) SN:(.+)")
_NUMBER = re.compile(rb"[0-9]+(?:\.[0-9]+)?")


class Transport(Protocol):
    """The serial line, as narrow as this backend uses it."""

    def write(self, data: bytes) -> int | None: ...

    def read_until(self, expected: bytes = ...) -> bytes: ...

    def read(self, size: int = ...) -> bytes: ...

    def close(self) -> None: ...


class RS3005PRefused(PowerSupplyFault, RuntimeError):
    """A call this backend will not make: nothing was sent for it.

    The fault says so itself, with ``wrote`` false and the cause ``not_sent``,
    so the layer above leaves the supply's state confirmed. It is still a
    RuntimeError, as it always was."""

    def __init__(self, error_type: str, summary: str) -> None:
        super().__init__(error_type, summary, wrote=False, cause="not_sent")


class PortOpenFailed(OSError):
    """The supply's port could not be opened, so nothing was sent on it."""


# How each failure of the serial line itself is said, by its fault cause.
_LINE_FAILURES = {
    "port_open_failed": "The port of the RS-3005P did not open",
    "write_timeout": "A write to the RS-3005P timed out",
    "serial_error": "The serial line to the RS-3005P failed",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def mask_identity_reply(reply: bytes) -> bytes:
    """The reply with its serial masked by `*`, its length and its terminator kept.

    The `SN:` span is masked where the reply parses as an identity; a reply that
    does not is masked whole, except its terminator.
    """
    body = reply.removesuffix(TERMINATOR)
    tail = reply[len(body) :]
    match = _IDENTITY.fullmatch(body)
    start = match.start(3) if match is not None else 0
    return body[:start] + b"*" * (len(body) - start) + tail


def parse_identity(reply: bytes) -> SupplyIdentity:
    match = _IDENTITY.fullmatch(reply.removesuffix(TERMINATOR))
    if match is None:
        return SupplyIdentity(model=None, firmware=None, accepted=False)
    try:
        model, firmware, serial = (part.decode("ascii") for part in match.groups())
    except UnicodeDecodeError:
        return SupplyIdentity(model=None, firmware=None, accepted=False)
    return SupplyIdentity(model=model, firmware=firmware, accepted=(model, firmware) in ACCEPTED_IDENTITIES, serial=serial)


class RS3005PBackend:
    """One conversation with one RS-3005P: an identity, then the operations of one call, then close.

    Nothing but `*IDN?` is sent before an identity this backend accepts was
    read, and nothing at all after the conversation ended (a reply that did not
    come, a STATUS reply that did not frame) or was closed.
    """

    def __init__(
        self,
        transport: Transport,
        *,
        clock: Callable[[], str] = utc_now,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._transport = transport
        self._clock = clock
        self._monotonic = monotonic
        self._sleep = sleep
        self._identity: SupplyIdentity | None = None
        self._ended: str | None = None
        self._closed = False
        self._evidence: list[JsonObject] = []
        self._last_fault: PowerSupplyFault | None = None

    # -- the interface ---------------------------------------------------------

    def identity(self) -> SupplyIdentity:
        self._open_conversation()
        self._identity = None
        reply = self._ask("identity", "*IDN?", wrote=False, masked=True)
        identity = parse_identity(reply)
        self._identity = identity
        return identity

    def capabilities(self) -> JsonObject:
        from agentic_hil.power_supply import POWER_SUPPLY_TYPES

        return copy.deepcopy(POWER_SUPPLY_TYPES[TYPE])

    def channel_status(self, channel: int) -> ChannelStatus:
        self._ready(channel)
        set_voltage, set_current, measured_voltage, measured_current = (
            self._number(self._ask("channel_status", command, wrote=False), command, wrote=False)
            for command in ("VSET1?", "ISET1?", "VOUT1?", "IOUT1?")
        )
        self._status_byte("channel_status")
        return ChannelStatus(
            set_voltage_v=set_voltage,
            set_current_a=set_current,
            measured_voltage_v=measured_voltage,
            measured_current_a=measured_current,
            output_on=None,
            regulation=None,
            protection_tripped=None,
            ovp_threshold_v=None,
            ocp_threshold_a=None,
            read_at=self._clock(),
        )

    def set_voltage(self, channel: int, volts: Decimal) -> None:
        self._set("set_voltage", channel, "VSET1", volts, VOLTAGE_PLACES)

    def set_current(self, channel: int, amps: Decimal) -> None:
        self._set("set_current", channel, "ISET1", amps, CURRENT_PLACES)

    def set_output(self, channel: int, on: bool) -> None:
        self._refuse("Output switching on the RS-3005P is not sent: its OUT commands and the STATUS byte that would confirm them are not recorded.")

    def arm_protection(self, channel: int, ovp_v: Decimal | None, ocp_a: Decimal | None) -> None:
        if ovp_v is None and ocp_a is None:
            return
        fault = PowerSupplyFault(
            "power_supply_protection_not_armed",
            "The RS-3005P backend arms no protection: how its OVP and OCP behave is not recorded, so no protection command is sent.",
            wrote=False,
        )
        self._last_fault = fault
        raise fault

    def evidence(self) -> list[JsonObject]:
        return copy.deepcopy(self._evidence)

    def classify_last_error(self) -> JsonObject:
        fault = self._last_fault
        if fault is None:
            return {"last_fault": None}
        return {"last_fault": {"error_type": fault.error_type, "summary": fault.summary, "wrote": fault.wrote}}

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._transport.close()

    # -- the conversation ------------------------------------------------------

    def _open_conversation(self) -> None:
        if self._closed:
            self._refuse("This RS-3005P conversation is closed; nothing more is sent on it.")
        if self._ended is not None:
            self._refuse(f"This RS-3005P conversation ended ({self._ended}); nothing more is sent on it.")

    def _ready(self, channel: int) -> None:
        self._open_conversation()
        if self._identity is None:
            self._refuse("Nothing is sent to the supply before its identity was read.")
        if not self._identity.accepted:
            self._refuse(
                f"The supply identified as {self._identity.model or 'an unparsed reply'} {self._identity.firmware or ''}".rstrip()
                + "; this backend drives only RS-3005P V6.9, so nothing else is sent."
            )
        if type(channel) is not int or channel != CHANNEL:
            raise ValueError(f"The RS-3005P has one channel, 1; got {channel!r}.")

    def _set(self, operation: str, channel: int, setter: str, value: Decimal, places: int) -> None:
        self._ready(channel)
        command = f"{setter}:{setter_text(value, places)}"
        started = self._monotonic()
        self._exchange(lambda: self._transport.write(command.encode("ascii") + TERMINATOR), command=command, wrote=True)
        self._record(operation, command, b"", started, latency=False, draft=SETTER_DRAFT, gap_s=SETTER_GAP_S)
        self._sleep(SETTER_GAP_S)
        query = f"{setter}?"
        self._number(self._ask(operation, query, wrote=True), query, wrote=True)

    def _ask(self, operation: str, command: str, *, wrote: bool, masked: bool = False) -> bytes:
        """One query and its newline-terminated reply; anything short of that is no reply."""
        started = self._monotonic()
        self._exchange(lambda: self._transport.write(command.encode("ascii") + TERMINATOR), command=command, wrote=wrote)
        reply = self._exchange(lambda: self._transport.read_until(TERMINATOR), command=command, wrote=wrote)
        self._record(operation, command, mask_identity_reply(reply) if masked else reply, started)
        if not reply.endswith(TERMINATOR):
            self._no_reply(command, wrote=wrote, why=f"answered {len(reply)} bytes and no newline" if reply else "did not answer")
        return reply

    def _status_byte(self, operation: str) -> bytes:
        """`STATUS?`: one raw byte and an optional newline, kept and not read."""
        started = self._monotonic()
        self._exchange(lambda: self._transport.write(b"STATUS?" + TERMINATOR), command="STATUS?", wrote=False)
        first = self._exchange(lambda: self._transport.read(1), command="STATUS?", wrote=False)
        second = self._exchange(lambda: self._transport.read(1), command="STATUS?", wrote=False) if first else b""
        reply = first + second
        self._record(operation, "STATUS?", reply, started)
        if not first:
            self._no_reply("STATUS?", wrote=False, why="did not answer")
        if second not in (b"", TERMINATOR):
            # Whatever follows could be read as the next command's reply, so
            # the conversation ends here, and so does the read that met it.
            self._no_reply("STATUS?", wrote=False, why="answered more than one byte and a newline, a framing nobody recorded,")
        return reply

    def _number(self, reply: bytes, command: str, *, wrote: bool) -> Decimal:
        text = reply.removesuffix(TERMINATOR)
        if _NUMBER.fullmatch(text) is None:
            self._no_reply(command, wrote=wrote, why=f"answered {len(text)} bytes that are not a decimal number")
        return Decimal(text.decode("ascii"))

    def _refuse(self, why: str) -> NoReturn:
        """Refuse a call before anything is sent for it."""
        refused = RS3005PRefused("power_supply_no_reply", why)
        self._last_fault = refused
        raise refused

    def _no_reply(self, command: str, *, wrote: bool, why: str, cause: str = "no_reply") -> NoReturn:
        """End the conversation with `power_supply_no_reply`.

        For the cause ``no_reply`` ``why`` says what the supply answered; for a
        failure of the serial line itself it is the transport's error, named by
        its type."""
        if cause == "no_reply":
            self._ended = f"the supply {why} to {command}"
            summary = f"The RS-3005P {why} to {command}"
        else:
            self._ended = f"{_LINE_FAILURES[cause].lower()} at {command}: {why}"
            summary = f"{_LINE_FAILURES[cause]} at {command} ({why})"
        if wrote:
            summary += ", after a set point was written"
        summary += ". The conversation is ended, so a late reply cannot be read as the answer to another command."
        fault = PowerSupplyFault("power_supply_no_reply", summary, wrote=wrote, cause=cause)
        self._last_fault = fault
        raise fault

    def _exchange(self, step: Callable[[], Any], *, command: str, wrote: bool) -> Any:
        """One transport call for `command`; a transport that fails ends the conversation.

        A failure of the serial line (pyserial raises an OSError for every one)
        is `power_supply_no_reply` naming its cause. ``wrote`` is whether this
        step could have changed the supply; a port that did not open sent
        nothing. Any other error is passed on unchanged."""
        try:
            return step()
        except OSError as error:
            from serial import SerialTimeoutException  # type: ignore[import-untyped]

            if isinstance(error, PortOpenFailed):
                cause, wrote = "port_open_failed", False
            elif isinstance(error, SerialTimeoutException):
                cause = "write_timeout"
            else:
                cause = "serial_error"
            reported = error.__cause__ if isinstance(error, PortOpenFailed) and error.__cause__ is not None else error
            self._no_reply(command, wrote=wrote, why=f"{type(reported).__name__}: {reported}", cause=cause)
        except BaseException as error:
            self._ended = f"the serial line failed: {type(error).__name__}"
            raise

    def _record(self, operation: str, command: str, reply: bytes, started: float, *, latency: bool = True, **extra: Any) -> None:
        self._evidence.append(
            {
                "operation": operation,
                "command": command,
                "reply_hex": reply.hex(),
                "latency_ms": round((self._monotonic() - started) * 1000, 3) if latency else None,
                "at": self._clock(),
                **extra,
            }
        )


def setter_text(value: Decimal, places: int) -> str:
    """`value` written with exactly `places` places, or ValueError where that would change it."""
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError(f"A set point is a finite Decimal; got {value!r}.")
    if value < 0:
        raise ValueError(f"A set point is not negative; got {value}.")
    step = Decimal(1).scaleb(-places)
    try:
        exact = value.quantize(step)
    except InvalidOperation as error:
        raise ValueError(f"{value} cannot be written with {places} places.") from error
    if exact != value:
        raise ValueError(f"{value} is not a multiple of {step}; it is refused rather than rounded.")
    return format(abs(exact), "f")


class SerialTransport:
    """The supply's port over pyserial, opened at the first command and closed with the backend."""

    def __init__(self, *, device: str, timeout_s: float, write_timeout_s: float | None, dtr: bool, rts: bool) -> None:
        self._device = device
        self._timeout_s = timeout_s
        self._write_timeout_s = write_timeout_s
        self._dtr = dtr
        self._rts = rts
        self._handle: Any = None

    def _open(self) -> Any:
        if self._handle is None:
            import serial

            # Built unopened so every setting, the modem lines among them, is
            # decided before the port is opened; exclusively, so no other
            # program interleaves its bytes with this conversation. Whatever
            # fails here, a setting pyserial refuses or the open itself, sent
            # nothing, and says so as `PortOpenFailed`.
            try:
                handle = serial.Serial()
                handle.port = self._device
                handle.baudrate = BAUDRATE
                handle.bytesize = serial.EIGHTBITS
                handle.parity = serial.PARITY_NONE
                handle.stopbits = serial.STOPBITS_ONE
                handle.xonxoff = False
                handle.rtscts = False
                handle.dsrdtr = False
                handle.timeout = self._timeout_s
                handle.write_timeout = self._write_timeout_s
                handle.dtr = self._dtr
                handle.rts = self._rts
                handle.exclusive = True
                handle.open()
            except (OSError, ValueError) as error:
                raise PortOpenFailed(f"{type(error).__name__}: {error}") from error
            self._handle = handle
        return self._handle

    def write(self, data: bytes) -> int | None:
        written: int | None = self._open().write(data)
        return written

    def read_until(self, expected: bytes = TERMINATOR) -> bytes:
        return bytes(self._open().read_until(expected))

    def read(self, size: int = 1) -> bytes:
        return bytes(self._open().read(size))

    def close(self) -> None:
        handle, self._handle = self._handle, None
        if handle is not None:
            handle.close()


def rs3005p_factory(config: Any, supply_id: str) -> RS3005PBackend:
    """The backend for `power_supplies.<supply_id>`, over the `com_ports` entry it names; nothing is opened yet."""
    supply = config.power_supplies[supply_id]
    port = config.com_ports[supply.com_port]
    transport = SerialTransport(
        device=port.device,
        timeout_s=supply.timeout_s,
        write_timeout_s=port.write_timeout_s,
        dtr=port.assert_dtr,
        rts=port.assert_rts,
    )
    return RS3005PBackend(transport)
