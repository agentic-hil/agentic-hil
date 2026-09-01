from __future__ import annotations

import errno
import math
import os
import re
import threading
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from agentic_hil.config import ConfigError, display_path, safe_append_text
from agentic_hil.coordination import (
    CoordinationError,
    DetachedHardwareLease,
    HardwareCoordinator,
    HardwareLease,
)
from agentic_hil.devices import VOLATILE_SERIAL_DEVICE_WARNING, uart_device
from agentic_hil.knowledge import COM_PORT_BUSY_ERROR, remediation_fields
from agentic_hil.provisional import (
    cleanup_provisional_handles,
    discharge_provisional_handle,
    register_provisional_handle,
)
from agentic_hil.report import (
    ContactMarker,
    append_jsonl,
    append_jsonl_audited,
    audit_unavailable,
    logs_directory,
    mark_audit_failure,
    mark_side_effect,
    no_contact_refusal,
    overall_success,
    recommit_report_with_status,
    safe_filename,
    timestamp_for_filename,
    utc_now_iso,
    write_report,
)
from agentic_hil.types import (
    SERIAL_BY_ID_DIRECTORY,
    AgenticHILConfig,
    ComPortConfig,
    JsonObject,
    fold_device_path,
    fold_hardware_id,
    format_usb_id,
    is_stable_device_name,
    raised_errno,
)


def list_available_com_ports(tool: str = "com_ports_available") -> JsonObject:
    try:
        from serial.tools import list_ports
    except ImportError:
        return {
            "ok": False,
            "tool": tool,
            "error_type": "serial_backend_not_available",
            "summary": "pyserial is not installed or could not be imported.",
            "likely_causes": ["install Agentic HIL with its runtime dependencies", "pyserial installation is broken"],
        }
    try:
        # Read once for the whole enumeration rather than per port: it is a
        # directory listing whose answer is the same for every port in it.
        stable_names = serial_by_id_links()
        ports = [available_port_info(port, stable_names) for port in list_ports.comports()]
        return {"ok": True, "tool": tool, "ports": ports, "summary": f"{len(ports)} available COM port(s)."}
    except OSError as error:
        return {
            "ok": False,
            "tool": tool,
            "error_type": "com_port_discovery_failed",
            "summary": "Available COM ports could not be listed.",
            "backend_error": str(error),
            "likely_causes": ["serial backend reported an OS error", "USB serial driver state changed during discovery"],
        }


# ---------------------------------------------------------------------------
# Which board is behind this entry.
#
# `device` says how to reach a port, not which one it is. On both platforms
# that name is an enumeration order: plug a second ST-Link into a Linux host
# and the UART that was ttyACM0 can come up as ttyACM1, and the board that
# takes the vacated name is opened in its place with nothing in the result
# saying so. The entry is therefore asked what hardware it means, and the
# answer is compared against what is actually behind that name before the port
# is opened.

COM_PORT_IDENTITY_MISMATCH = "com_port_identity_mismatch"
# A declared identity that could not be confirmed before opening: the host has
# no serial backend, the port is not enumerated exactly once, the matched port
# reports no serial, or it reports no vendor and product ids while the entry
# names them. Distinct from a mismatch: the name may still lead to
# the right board, but nothing proved it does, and an entry that asked for the
# guarantee is not opened on a check that did not run.
COM_PORT_IDENTITY_UNVERIFIED = "com_port_identity_unverified"


def usb_id_phrase(vid: int | None, pid: int | None) -> str:
    """The vendor/product pair as a refusal says it out loud, or ``""``.

    Hexadecimal, because that is the spelling on the datasheet, in `lsusb` and
    in pyserial's own ``hwid``; the integers travel in the payload beside it."""
    if vid is not None and pid is not None:
        return f"type {format_usb_id(vid)}:{format_usb_id(pid)}"
    if vid is not None:
        return f"vendor {format_usb_id(vid)}"
    return f"product {format_usb_id(pid)}" if pid is not None else ""


def host_usb_id(value: object) -> int | None:
    """One USB id as the host inventory reports it, or nothing.

    pyserial hands out integers. Anything else is a host that did not answer the
    question, which is the same as not answering it, and an entry that named an
    id is then refused rather than opened, exactly as it is for a serial the host
    does not report."""
    return value if isinstance(value, int) and not isinstance(value, bool) else None


@dataclass(frozen=True)
class PortIdentity:
    """The hardware a configured COM port claims, and the fields that claim it.

    ``expected`` is the unit (a USB serial) and is None for an adapter that
    publishes none. ``vid``/``pid`` are the type, and either half may stand
    alone: they narrow a serial that matches (a USB serial is unique only within
    a vendor) and they are the whole claim when there is no serial to narrow."""

    expected: str | None
    field: str | None
    vid: int | None = None
    pid: int | None = None

    @property
    def claim(self) -> str:
        """What this entry says its port is, in one phrase a refusal can quote."""
        ids = usb_id_phrase(self.vid, self.pid)
        if self.expected is None:
            return f"a device of {ids}"
        return f"hardware '{self.expected}' of {ids}" if ids else f"hardware '{self.expected}'"

    @property
    def fields(self) -> JsonObject:
        """The claim as result keys: only what this entry actually names."""
        stated: JsonObject = {}
        if self.expected is not None:
            stated["expected_serial_number"] = self.expected
            stated["expected_from"] = self.field
        if self.vid is not None:
            stated["expected_vid"] = self.vid
        if self.pid is not None:
            stated["expected_pid"] = self.pid
        return stated


def expected_port_identity(config: AgenticHILConfig, port_id: str) -> PortIdentity | None:
    """The hardware this entry says its port belongs to, if it says any.

    Two sources for the serial, and no third. ``serial_number`` is the entry
    stating its own hardware. A ``resource_id`` shared with a configured debugger
    is the operator stating that this port and that probe are one unit (which
    they are on any Nucleo), so the probe's serial is the port's serial.

    ``vid``/``pid`` ride along with whichever of those answered, and stand on
    their own when neither did: an adapter that publishes no serial can still say
    what kind of adapter it is, and a check that refuses a CH340 answering to a
    name written for an ST-Link is worth more than no check at all.

    What is deliberately absent is a guess. A project with one debugger and one
    COM port is *probably* one board, and probably is exactly what must not
    decide which hardware a write reaches: a bench with a separate USB-serial
    adapter has the same shape and a different answer. An entry that names
    neither gets no expectation, no check, and the behaviour it has today,
    together with the warning that says why (``UartDevice.identity_warning``).

    Ambiguity is the same answer as silence: two debuggers sharing one
    ``resource_id`` under different probe serials say nothing about which of them
    this port is."""
    port = config.com_ports.get(port_id)
    if port is None:  # pragma: no cover - callers resolve the entry first
        return None
    if port.serial_number:
        return PortIdentity(port.serial_number, f"com_ports.{port_id}.serial_number", port.vid, port.pid)
    if port.resource_id:
        shared = fold_hardware_id(port.resource_id)
        named = sorted(
            (name, debugger.probe_id)
            for name, debugger in config.debuggers.items()
            if debugger.probe_id and debugger.resource_id and fold_hardware_id(debugger.resource_id) == shared
        )
        if len(named) == 1:
            debugger_id, probe_id = named[0]
            return PortIdentity(str(probe_id), f"debuggers.{debugger_id}.probe_id", port.vid, port.pid)
    if port.vid is None and port.pid is None:
        return None
    return PortIdentity(None, None, port.vid, port.pid)


def port_identity_fields(config: AgenticHILConfig, port_id: str) -> JsonObject:
    """How one configured entry is identified, without asking the hardware.

    A pure read of the configuration, so it answers on a host with nothing
    attached, which is where `doctor` and `com_ports_list` are usually run."""
    device = uart_device(config, port_id)
    fields: JsonObject = {"identity_source": device.identity_source}
    if device.port.serial_number:
        fields["serial_number"] = device.port.serial_number
    if device.port.vid is not None:
        fields["vid"] = device.port.vid
    if device.port.pid is not None:
        fields["pid"] = device.port.pid
    if device.port.resource_id:
        fields["resource_id"] = device.port.resource_id
    expectation = expected_port_identity(config, port_id)
    if expectation is not None:
        fields.update(expectation.fields)
    warning = device.identity_warning
    if warning:
        fields["identity_warning"] = warning
    return fields


def port_names_device(host_port: JsonObject, configured_device: str) -> bool:
    """Whether one enumerated host port is the device a configuration names.

    Either spelling counts, because both name it: a configuration written by
    adoption on Linux holds ``/dev/serial/by-id/...`` while the enumerator
    reports ``/dev/ttyACM0``, and the link between the two was already resolved
    when the inventory was taken. Case folds as a path, which is the host's rule
    and not ours. See ``types.fold_device_path``."""
    wanted = fold_device_path(configured_device)
    candidates = (str(host_port.get("device") or ""), str(host_port.get("stable_device") or ""))
    return any(name and fold_device_path(name) == wanted for name in candidates)


def _identity_unverified(tool: str, port_id: str, port: ComPortConfig, expectation: PortIdentity, identity: JsonObject) -> JsonObject:
    """Refuse a declared port whose identity could not be confirmed before opening.

    Distinct from a mismatch: a mismatch found the wrong board behind the name,
    this found no way to check which board is behind it. Both refuse before the
    port is opened and for the same reason: an entry that names hardware is
    opened only against a `confirmed` comparison, and a check that could not run
    is not one. ``identity['status']`` says which of the three ways the question
    had no answer, and ``identity['summary']`` says it in a sentence.

    Retry-safe and no-contact by construction: the port was never touched, so
    restoring the check (installing the serial backend, plugging the board in,
    a host that reports the adapter's serial) or running `adopt-hardware` to
    rewrite the entry, then calling again, is the whole repair."""
    return {
        "ok": False,
        "tool": tool,
        "port_id": port_id,
        "error_type": COM_PORT_IDENTITY_UNVERIFIED,
        "summary": (
            f"'{port.device}' names {expectation.claim}, but that could not be confirmed before opening, so "
            f"the port was not opened: {identity['summary']}"
        ),
        "identity": identity,
        "configured_device": port.device,
        **expectation.fields,
        "next_step": (
            "This entry names a board on purpose, so it is opened only once the host confirms the name still leads to it. "
            "Restore the check (install the serial backend, plug the board in, or use a host that reports the adapter's "
            "serial) or run `agentic-hil adopt-hardware` to rewrite the entry for the board that is attached. Drop the "
            "entry's `serial_number`/`vid`/`pid`/`resource_id` only if it genuinely names no fixed board."
        ),
        # Nothing was reached, so the bench stays in service and this is not an
        # incident: restore the check and call again.
        "side_effect_committed": False,
        "side_effect_status": "not_started",
        "hardware_state": "unchanged",
        "retry_safe": True,
    }


def _identity_mismatch(
    tool: str,
    port_id: str,
    port: ComPortConfig,
    expectation: PortIdentity,
    identity: JsonObject,
    *,
    summary: str,
    likely_causes: list[str],
) -> JsonObject:
    """Refuse a declared port that is currently other hardware than it names.

    One refusal with two ways in, because from the caller's side they are one
    thing: the name no longer leads to what the entry says. The serial says this
    is another *unit*, the vendor and product ids say it is another *kind* of
    device, and the second is not the weaker of the two, since a USB serial
    number is unique only within a vendor and a matching one across vendors
    proves nothing at all. Both sides of the comparison are in
    the payload, whichever ran: ``expected_*`` from the configuration,
    ``found_*`` from the host."""
    identity["status"] = "mismatch"
    return {
        "ok": False,
        "tool": tool,
        "port_id": port_id,
        "error_type": COM_PORT_IDENTITY_MISMATCH,
        "summary": summary,
        "identity": identity,
        "configured_device": port.device,
        **expectation.fields,
        **{name: identity[name] for name in ("found_serial_number", "found_vid", "found_pid") if name in identity},
        "likely_causes": likely_causes,
        # Nothing was reached, so the bench stays in service and this is not an
        # incident: fix the named cause (plug the board in, or let
        # `adopt-hardware` rewrite the entry) and call again.
        "side_effect_committed": False,
        "side_effect_status": "not_started",
        "hardware_state": "unchanged",
        "retry_safe": True,
    }


def verify_port_identity(config: AgenticHILConfig, port_id: str, tool: str) -> JsonObject:
    """Refuse a configured port that is currently a different board.

    ``{"ok": True, "identity": ...}`` when the port may be opened, and the
    identity block says on what grounds. ``{"ok": False, ...}`` is the refusal,
    and it is returned before anything is opened: connecting to find out which
    board a name now leads to is itself the failure.

    An entry that declares hardware is opened on one ground only: the attached
    device was `confirmed` to be the board it names. The four ways that question
    has no answer (the serial backend is missing (`backend_unavailable`), the
    device is not enumerated exactly once (`port_not_enumerated`), the matched
    port reports no serial (`serial_unknown`), or it reports no USB ids while the
    entry names them (`usb_ids_unknown`)) are refusals too, not opens with
    a note. The entry asked that this name be proved to still
    reach its board before use, and "the check could not run" does not prove it;
    reporting the gap in a result the caller reads *after* it has already written
    to whatever the name now reaches is no protection at all. Every one is
    retry-safe and no-contact: restore the check, or `adopt-hardware` the entry,
    and call again.

    An entry that declares no hardware costs nothing here: the host is not
    enumerated at all, so a configuration written before any of this existed
    behaves exactly as it did, `not_declared` and openable."""
    port = config.com_ports[port_id]
    expectation = expected_port_identity(config, port_id)
    identity: JsonObject = {"device": port.device}
    if expectation is None:
        identity["status"] = "not_declared"
        identity["summary"] = "This entry names no hardware, so which board it reaches was not verified."
        if not is_stable_device_name(port.device):
            identity["warning"] = VOLATILE_SERIAL_DEVICE_WARNING
        return {"ok": True, "identity": identity}

    identity.update(expectation.fields)
    available = list_available_com_ports("com_ports_available")
    if not available.get("ok"):
        # The inventory is how this question is answered at all. This entry named
        # a board so that the name would be proved to still reach it before use;
        # pyserial missing means it cannot be, and opening anyway is the wrong-
        # board write the identity exists to prevent. Nothing was contacted, so
        # the refusal is retry-safe: install the backend and call again.
        identity["status"] = "backend_unavailable"
        identity["summary"] = "Host serial ports could not be enumerated, so this port's identity was not verified."
        identity["backend_error"] = str(available.get("summary", ""))
        return _identity_unverified(tool, port_id, port, expectation, identity)

    ports = [entry for entry in available.get("ports", []) if isinstance(entry, dict)]
    matches = [entry for entry in ports if port_names_device(entry, port.device)]
    if len(matches) != 1:
        identity["status"] = "port_not_enumerated"
        identity["summary"] = (
            "The host's serial port inventory does not name this device exactly once, so its identity was not "
            "verified. A pseudo-terminal, a URL handler and a port that has gone away all look like this."
        )
        return _identity_unverified(tool, port_id, port, expectation, identity)

    found = str(matches[0].get("serial_number") or "")
    stable = matches[0].get("stable_device")
    if stable:
        identity["stable_device"] = stable
    found_vid = host_usb_id(matches[0].get("vid"))
    found_pid = host_usb_id(matches[0].get("pid"))
    if found:
        identity["found_serial_number"] = found
    # Reported when this entry names them, and only then. The host reports vid
    # and pid for every USB adapter, so recording them unconditionally would add
    # fields to the result of every configuration written before these keys
    # existed, and an entry that names no type has had no type compared.
    if expectation.vid is not None or expectation.pid is not None:
        if found_vid is not None:
            identity["found_vid"] = found_vid
        if found_pid is not None:
            identity["found_pid"] = found_pid

    if expectation.expected is not None:
        if not found:
            identity["status"] = "serial_unknown"
            identity["summary"] = "This host port reports no serial number, so it could not be compared with the hardware this entry names."
            return _identity_unverified(tool, port_id, port, expectation, identity)
        if fold_hardware_id(found) != fold_hardware_id(expectation.expected):
            elsewhere = next((entry for entry in ports if fold_hardware_id(str(entry.get("serial_number") or "")) == fold_hardware_id(expectation.expected)), None)
            result = _identity_mismatch(
                tool,
                port_id,
                port,
                expectation,
                identity,
                summary=(
                    f"'{port.device}' currently belongs to hardware '{found}', but this entry names '{expectation.expected}'. "
                    "The port was not opened: a device name is an enumeration order, so this is another board under the "
                    "name this entry used to have."
                ),
                likely_causes=[
                    "another serial adapter was attached, so the kernel handed this name to a different device",
                    "the boards were replugged in a different order, or the host was rebooted with both attached",
                    "this entry was written for a board that is no longer the one behind this name",
                    (
                        "this entry shares a resource_id with a probe whose virtual COM port it is not, so it is being "
                        "compared against that probe's serial; give the port its own serial_number if the two really are "
                        "separate devices"
                    ),
                ],
            )
            if isinstance(elsewhere, dict):
                # The board this entry means is still attached, under another
                # name. Say where, because that is the whole repair.
                moved = str(elsewhere.get("stable_device") or elsewhere.get("device") or "")
                result["expected_device"] = moved
                result["summary"] += f" The hardware it names is attached as '{moved}'."
            return result

    # The type check. It runs after the serial and never instead of it: a serial
    # that matches proves the unit only once the vendor agrees, because a USB
    # serial number is unique within a vendor and nowhere else. For an adapter
    # that publishes no serial this is the whole check, and it is honest about
    # being one: it separates a CH340 from an ST-Link, not one CH340 from
    # another.
    declared = [(claimed, seen) for claimed, seen in ((expectation.vid, found_vid), (expectation.pid, found_pid)) if claimed is not None]
    if any(seen is None for _, seen in declared):
        identity["status"] = "usb_ids_unknown"
        identity["summary"] = (
            "This host port reports no USB vendor and product id, so the device type this entry names could not be "
            "compared with what is behind the name."
        )
        return _identity_unverified(tool, port_id, port, expectation, identity)
    if any(claimed != seen for claimed, seen in declared):
        return _identity_mismatch(
            tool,
            port_id,
            port,
            expectation,
            identity,
            summary=(
                f"'{port.device}' is currently a device of {usb_id_phrase(found_vid, found_pid)}, but this entry names "
                f"{usb_id_phrase(expectation.vid, expectation.pid)}. The port was not opened: this is a different kind of "
                "device under the name this entry used to have"
                + (
                    ", and its serial number matching proves nothing, because a USB serial is unique only within a vendor."
                    if expectation.expected is not None
                    else "."
                )
            ),
            likely_causes=[
                "a different USB-serial adapter was attached and the kernel handed this name to it",
                "this entry was written for an adapter that is no longer the one behind this name",
                (
                    "the serial numbers agree by coincidence: a USB serial number is scoped to its vendor, and cheap "
                    "adapters ship duplicates of it, which is the case `vid`/`pid` exist to separate"
                ),
            ],
        )

    identity["status"] = "confirmed"
    identity["summary"] = (
        "The port behind this entry is the hardware it names."
        if expectation.expected is not None
        else (
            "The port behind this entry is a device of the type it names. This entry names no serial number, so that "
            "is a type check: it separates this kind of adapter from another kind, not one of them from an identical one."
        )
    )
    return {"ok": True, "identity": identity}


# The OS numbers that mean "another program is holding this port", and nothing
# else. `EWOULDBLOCK`/`EAGAIN` is what a non-blocking exclusive lock answers when
# the lock is already taken: one number on Linux, not necessarily one number
# everywhere, so both are named. `EBUSY` is what a driver answers when the kernel
# is holding the line exclusively on somebody else's behalf.
#
# `EACCES` is deliberately absent. On a serial device it is far more often a
# missing group membership than a second holder, and answering "busy" to a
# permissions problem sends the reader hunting for a process that does not exist.
PORT_BUSY_ERRNOS = (errno.EWOULDBLOCK, errno.EAGAIN, errno.EBUSY)


def serial_port_busy(error: BaseException, port_id: str, port_config: ComPortConfig) -> JsonObject | None:
    """The refusal for a port another program already holds, or ``None``.

    Open time only and by OS number only, and both halves of that are the
    contract: the same shape the SocketCAN missing-interface refusal is cut to,
    for the same reason. A refused open leaves no handle behind: the port keeps
    doing whatever its holder is doing with it, this session put nothing on the
    line, and the failure is about which process owns a device name rather than
    about a board whose state is in doubt.

    Reachable at all only because the handle is opened exclusively. Without that
    the second open succeeds on POSIX, two processes interleave bytes on one
    line, and the failure surfaces much later as a response that does not match
    the stimulus: an unknown board state, which is a quarantine. The whole point
    of asking for exclusivity is to move that failure to here, where it is a
    refusal nobody has to walk to the bench for.

    ``None`` on Windows even for a busy port, and that is a limit rather than an
    oversight: the Win32 open refusal arrives as a message-only exception with no
    number anywhere in its chain, and classifying on the message would answer
    differently under a translated system. That case keeps
    `com_port_open_failed`, whose likely causes already name a second program
    holding the port, and loses only the specific name for it.
    """
    if not any(raised_errno(error, number) for number in PORT_BUSY_ERRNOS):
        return None
    return {
        "ok": False,
        "tool": "com_session_start",
        "port_id": port_id,
        "error_type": COM_PORT_BUSY_ERROR,
        # `configured_device`, and deliberately no `field`. A `field` names the
        # configuration key that is wrong, and here nothing in the configuration
        # is: the entry reaches exactly the port it means, and somebody else has
        # it. Naming a key invites the one repair the remediation warns against:
        # pointing the entry at whichever device is free, which is another board.
        "configured_device": port_config.device,
        "summary": (
            f"COM port {port_config.device} is held by another program, so the session was refused at the open: no "
            "handle was created and nothing was written to the line."
        ),
        "backend_error": str(error),
        "target_contacted": False,
        "side_effect_committed": False,
        "side_effect_status": "not_started",
        "retry_safe": True,
        **remediation_fields(COM_PORT_BUSY_ERROR),
    }


class ComPortSession:
    def __init__(self, port_id: str, port_config: ComPortConfig, serial_handle: object, log_path: str, lease: HardwareLease | None = None, *, start_reader: bool = True, contact: ContactMarker | None = None):
        self.port_id = port_id
        self.port_config = port_config
        self.serial_handle = serial_handle
        # A supplied marker is authoritative and is never written over here: the
        # open that produced it is the evidence, and a constructor is not. Only
        # the marker-less case is filled in, which is the direct construction in
        # tests: those describe an open port, so that is what their marker says.
        self.contact = contact if contact is not None else ContactMarker()
        if contact is None:
            self.contact.record("serial_open")
        self.log_path = log_path
        self.started_at = utc_now_iso()
        self.active = True
        self.buffer = bytearray()
        self.overflow_bytes = 0
        self.reader_error: JsonObject | None = None
        self.lock = threading.Lock()
        self.io_lock = threading.Lock()
        self.reader: threading.Thread | None = None
        self.audit_broken = False
        self.lease = lease or DetachedHardwareLease()
        if start_reader:
            self.start_reader()

    def start_reader(self) -> None:
        if self.reader is not None:
            return
        reader = threading.Thread(target=self._reader_loop, daemon=True)
        self.reader = reader
        try:
            reader.start()
        except BaseException:
            if not reader.is_alive():
                self.reader = None
            raise

    def _reader_loop(self) -> None:
        while self.active:
            try:
                with self.io_lock:
                    waiting = int(getattr(self.serial_handle, "in_waiting", 0) or 0)
                    read_size = min(max(waiting, 1), self.port_config.max_buffer_bytes, 4096)
                    data = self.serial_handle.read(read_size)
                    if data:
                        chunk = bytes(data)
                        with self.lock:
                            self.buffer.extend(chunk)
                            overflow = len(self.buffer) - self.port_config.max_buffer_bytes
                            if overflow > 0:
                                del self.buffer[:overflow]
                                self.overflow_bytes += overflow
                if not data:
                    time.sleep(0.01)
                    continue
                audit_error = append_jsonl(self.log_path, {"direction": "rx", "bytes": len(chunk), "hex": chunk.hex(), "text": decode_bytes(chunk, self.port_config.encoding)})
                if audit_error is not None:
                    self.reader_error = {"error_type": "audit_write_failed", "summary": "COM port feedback could not be audited.", "backend_error": str(audit_error)}
                    self.audit_broken = True
                    self.lease.quarantine("com_reader_audit_broken", audit_error, audit_broken=True)
                    self.active = False
                    break
            except Exception as error:  # serial backends raise implementation-specific exception classes
                if self.active:
                    self.reader_error = {
                        "error_type": "serial_read_failed",
                        "summary": "COM port reader failed.",
                        "backend_error": str(error),
                        "likely_causes": likely_causes("serial_read_failed"),
                    }
                    append_jsonl(self.log_path, {"event": "error", **self.reader_error})
                break


# A short write is retried against exactly its own remainder, and only a few
# times. The common cause is a full OS send buffer draining the moment it is
# asked again, which one or two more calls already answers; a handle that
# keeps confirming zero further bytes on every attempt stops this before the
# budget does, and the budget itself exists so a link that is genuinely this
# slow fails the call instead of blocking it for an unbounded number of
# round trips at the configured write_timeout_s each.
SHORT_WRITE_RETRY_LIMIT = 3


def _write_with_bounded_retry(serial_handle: object, data: bytes) -> int:
    """Write ``data``, retrying a short return against its own remainder.

    Returns the number of bytes pyserial's own return value confirms were
    queued: never assumed, and never ``len(data)`` by default the way a
    return value nobody read used to mean. A handle that answers ``None``
    is trusted for the call it made, because some fakes and older backends
    report nothing on a completed write; that trust is per call, not
    inherited by the next one, so a real short return is still caught.

    Stops the moment a call makes no progress at all, so a port that is
    genuinely stuck is answered in one extra round trip rather than by
    exhausting the retry budget doing nothing every time.
    """
    sent = 0
    attempt = 0
    while True:
        remaining = data[sent:]
        written = serial_handle.write(remaining)
        chunk = len(remaining) if written is None else max(0, min(int(written), len(remaining)))
        sent += chunk
        attempt += 1
        if sent >= len(data) or chunk == 0 or attempt > SHORT_WRITE_RETRY_LIMIT:
            return sent


class ComPortService:
    def __init__(self, config: AgenticHILConfig, coordinator: HardwareCoordinator | None = None):
        self.config = config
        self.coordinator = coordinator or HardwareCoordinator(config, "com-service")
        self._owns_coordinator = coordinator is None
        self.sessions: dict[str, ComPortSession] = {}

    def reconfigure(self, config: AgenticHILConfig) -> None:
        # The port config carries its own permissions, so an inequality here also
        # covers a revoked grant: a session may not outlive the permission that
        # authorized it.
        for port_id, session in list(self.sessions.items()):
            if config.com_ports.get(port_id) != session.port_config:
                self._stop_session(session, "config_reloaded")
                self.sessions.pop(port_id, None)
        self.config = config

    def list_ports(self) -> JsonObject:
        ports: JsonObject = {}
        for port_id, port_config in self.config.com_ports.items():
            ports[port_id] = self._port_status(port_id, port_config, self.sessions.get(port_id))
        # Host discovery is not scoped to one configured port, so it needs at
        # least one port the operator allowed reading from.
        if any(self.config.com_read_allowed(port_config) for port_config in self.config.com_ports.values()):
            available = list_available_com_ports()
        else:
            available = {
                "ok": False,
                "tool": "com_ports_available",
                "error_type": "permission_denied",
                "summary": "Host COM port discovery is disabled by the authoritative config; no configured COM port has permissions.allow_read.",
            }
        available_count = len(available.get("ports", [])) if available.get("ok") else 0
        return {
            "ok": True,
            "tool": "com_ports_list",
            "ports": ports,
            "available_com_ports": available,
            "summary": f"{len(ports)} configured COM port(s), {available_count} available host COM port(s).",
        }

    def session_start(self, port_id: str, clear_buffer: bool = True) -> JsonObject:
        if not isinstance(port_id, str) or not isinstance(clear_buffer, bool):
            return self._write_report({"ok": False, "tool": "com_session_start", "error_type": "invalid_argument", "summary": "port_id must be a string and clear_buffer must be a boolean.", "side_effect_committed": False})
        port = self._configured_port(port_id, "com_session_start")
        if not port["ok"]:
            return self._write_report(port)
        # A write-only line is a legitimate configuration, and the session is the
        # only way to reach the port at all: gating the open on allow_read alone
        # made allow_write without allow_read a grant that could never be used.
        port_permissions = port["port_config"].permissions
        if not self.config.com_read_allowed(port["port_config"]) and not port_permissions.allow_write:
            return self._write_report(self._permission_denied("com_session_start", "Reading and writing this COM port are disabled by the authoritative config.", port_id))
        if clear_buffer and not self.config.com_read_allowed(port["port_config"]):
            # Clearing drains the receive buffer, which is a read.
            clear_buffer = False

        # Before the log file, before the lease, before the handle: a mismatch
        # means this name leads to a board nobody meant, and every one of those
        # steps is already a step taken on its behalf. Enumerating the host to
        # ask which board a configured entry reaches is part of opening it
        # safely, not a read of the target, so it is not gated on a permission,
        # and it names only the port being opened, never the inventory.
        identity = verify_port_identity(self.config, port_id, "com_session_start")
        if not identity["ok"]:
            return self._write_report(identity)
        identity_status = identity["identity"]

        existing = self.sessions.get(port_id)
        if existing and self._session_is_active(existing):
            if clear_buffer:
                cleared = self._clear_buffers(existing)
                if not cleared["ok"]:
                    return self._write_report(cleared)
            return self._write_report({"ok": True, "tool": "com_session_start", "port_id": port_id, "already_active": True, "session": self._session_status(existing), "identity": identity_status, "summary": "COM port session is already active."})
        if existing:
            try:
                self._stop_session(existing, "replaced")
            except Exception as error:
                return self._write_report(self._close_failure("com_session_start", port_id, error))
            self.sessions.pop(port_id, None)

        try:
            log_path = str(Path(logs_directory(self.config)) / f"com-{timestamp_for_filename()}-{safe_filename(port_id, 'port')}.jsonl")
            safe_append_text(log_path, "")
        except (ConfigError, OSError) as error:
            return audit_unavailable("com_session_start", error)
        try:
            lease = self.coordinator.acquire(uart_device(self.config, port_id))
        except CoordinationError as error:
            return self._write_report({"tool": "com_session_start", "port_id": port_id, "side_effect_committed": False, **error.result})
        contact = ContactMarker()
        try:
            opened = self._open_serial(port_id, port["port_config"], log_path, lease, contact)
        except BaseException as error:
            # An interrupt that lands before the open has nothing to contain: the
            # port was never reached, so the lease goes back instead of holding a
            # bench for a physical inspection of a board this call never touched.
            # A release that will not confirm is itself an unknown and keeps the
            # quarantine, as does an interrupt after contact.
            if not contact.proves_no_contact or not lease.release():
                lease.quarantine("com_open_interrupted", error)
            raise
        if not opened["ok"]:
            # The marker is consulted before the backend's own claim about its
            # side effect, and it can only ever widen the refusal: an open that
            # never reached the port is a refusal whatever exception class ended
            # it, including one nothing here has ever seen. Where contact was
            # made, or cannot be ruled out, the fields the backend wrote decide
            # exactly as they did before.
            opened = no_contact_refusal({**opened, **contact.report_fields()}, contact)
            safe_to_release = opened.get("side_effect_committed") is False or opened.get("cleanup_confirmed") is True
            if not safe_to_release:
                # The open failed and the teardown of whatever handle it got as
                # far as could not be confirmed. That is recorded, in the same
                # detail and under the same reason, and it travels into this
                # refusal so the caller reads what is unconfirmed and what to
                # check. What it no longer does is hold the port: a handle that
                # is really still open makes the next `com_session_start` fail
                # through the operating system, and one that is not makes it
                # succeed, which is the proof this reason was waiting for.
                lease.record_cleanup_event("com_open_cleanup_unconfirmed", opened.get("backend_error"))
            return self._write_unattached_lease_report(opened, lease, release_if_safe=True)
        session = opened["session"]
        self.sessions[port_id] = session
        audit_error = append_jsonl_audited(self.config, session.log_path, {"event": "start", "port_id": port_id, "device": session.port_config.device})
        if audit_error is not None:
            session.audit_broken = True
            with suppress(BaseException):
                self._stop_session(session, "audit_failed")
            result = {"ok": True, "tool": "com_session_start", "port_id": port_id, "already_active": False, "session": self._session_status(session), "identity": identity_status, "cleanup_required": True, "summary": "COM port opened, but audit initialization failed; resource is quarantined."}
            return self._write_report(mark_audit_failure(result, audit_error))
        if clear_buffer:
            cleared = self._clear_buffers(session)
            if not cleared["ok"]:
                try:
                    self._stop_session(session, "buffer_clear_failed", defer_release=True)
                except BaseException as close_error:
                    return self._write_report(self._close_failure("com_session_start", port_id, close_error))
                written = self._write_report({**cleared, "cleanup_confirmed": True})
                if written.get("audit_ok") is not False:
                    session.lease.resolve_retryable_cleanup("com_buffer_clear_unconfirmed")
                    if not session.lease.release():
                        return recommit_report_with_status(self.config, written, session.lease.status())
                    self.sessions.pop(port_id, None)
                return recommit_report_with_status(self.config, written, session.lease.status())
        try:
            # The background reader buffers incoming bytes, so it only runs for
            # a port the operator allowed reading from.
            if self.config.com_read_allowed(port["port_config"]):
                session.start_reader()
        except BaseException as error:
            try:
                self._stop_session(session, "start_failed", defer_release=True)
            except BaseException as close_error:
                written = self._write_report(self._close_failure("com_session_start", port_id, close_error))
                if isinstance(error, (KeyboardInterrupt, SystemExit)):
                    error.args = (*error.args, f"Cleanup error: {close_error}")
                    raise error from close_error
                if isinstance(close_error, (KeyboardInterrupt, SystemExit)):
                    raise
                return written
            failure = {"ok": False, "tool": "com_session_start", "port_id": port_id, "error_type": "com_reader_start_failed", "summary": "COM port reader could not be started; the port was closed.", "backend_error": str(error), "cleanup_confirmed": True, "side_effect_committed": False}
            written = self._write_report(failure)
            if written.get("audit_ok") is False:
                return written
            if not session.lease.release():
                return self._write_report(self._close_failure("com_session_start", port_id, RuntimeError("Lease release remained unconfirmed.")))
            self.sessions.pop(port_id, None)
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise error
            return recommit_report_with_status(self.config, written, session.lease.status())
        result = {"ok": True, "tool": "com_session_start", "port_id": port_id, "already_active": False, "session": self._session_status(session), "identity": identity_status, "summary": "COM port session started."}
        return self._write_report(mark_audit_failure(result, audit_error) if audit_error is not None else result)

    def session_stop(self, port_id: str) -> JsonObject:
        port = self._configured_port(port_id, "com_session_stop")
        if not port["ok"]:
            return self._write_report(port)
        session = self.sessions.get(port_id)
        if session is None:
            return self._write_report({"ok": True, "tool": "com_session_stop", "port_id": port_id, "was_active": False, "summary": "COM port session was not active."})
        try:
            audit_error = self._stop_session(session, "requested", defer_release=True)
        except Exception as error:
            return self._write_report(self._close_failure("com_session_stop", port_id, error))
        result = {"ok": True, "tool": "com_session_stop", "port_id": port_id, "was_active": True, "session": self._session_status(session), "summary": "COM port session stopped."}
        written = self._write_report(mark_audit_failure(result, audit_error) if audit_error is not None else result)
        if written.get("audit_ok") is False:
            return {**written, "cleanup_required": True, "quarantined": True}
        if not session.lease.release():
            return self._write_report(self._close_failure("com_session_stop", port_id, RuntimeError("Lease release remained unconfirmed.")))
        self.sessions.pop(port_id, None)
        return recommit_report_with_status(self.config, written, session.lease.status())

    def write(self, port_id: str, payload: JsonObject) -> JsonObject:
        port = self._configured_port(port_id, "com_write")
        if not port["ok"]:
            return self._write_report(port)
        encoded = payload_bytes(port["port_config"], payload)
        if not encoded["ok"]:
            encoded["port_id"] = port_id
            return self._write_report(encoded)
        return self._write_report(self.write_bytes(port_id, encoded["data"], "com_write"))

    def write_bytes(self, port_id: str, data: bytes, tool: str = "com_write") -> JsonObject:
        port = self._configured_port(port_id, tool)
        if not port["ok"]:
            return port
        if not port["port_config"].permissions.allow_write:
            return self._permission_denied(tool, "Writing to this COM port is disabled by the authoritative config.", port_id)
        session_result = self._active_session(port_id, tool)
        if not session_result["ok"]:
            return session_result
        session = session_result["session"]
        if len(data) > session.port_config.max_write_bytes:
            return {"ok": False, "tool": tool, "port_id": port_id, "error_type": "invalid_argument", "summary": "COM port write exceeds configured max_write_bytes.", "bytes_requested": len(data), "max_write_bytes": session.port_config.max_write_bytes}
        try:
            sent = _write_with_bounded_retry(session.serial_handle, data)
            flush = getattr(session.serial_handle, "flush", None)
            if callable(flush):
                flush()
        except BaseException as error:
            result = {"ok": False, "tool": tool, "port_id": port_id, "error_type": "serial_write_failed", "summary": "COM port write failed.", "backend_error": str(error), "likely_causes": likely_causes("serial_write_failed"), "log_path": display_path(self.config, session.log_path)}
            session.lease.quarantine("com_write_effect_unconfirmed", error)
            result.update({"side_effect_status": "unknown", "retry_safe": False, "cleanup_required": True, "quarantined": True})
            audit_error = append_jsonl_audited(self.config, session.log_path, {"event": "error", **result})
            if audit_error is not None:
                session.audit_broken = True
                session.lease.quarantine("com_write_audit_broken", audit_error, audit_broken=True)
                return mark_audit_failure(result, audit_error)
            if not isinstance(error, Exception):
                raise
            return result
        sent_data = data[:sent]
        audit_error = append_jsonl_audited(self.config, session.log_path, {"direction": "tx", "bytes": len(sent_data), "hex": sent_data.hex(), "text": decode_bytes(sent_data, session.port_config.encoding)})
        if sent < len(data):
            # Confirmed short, not unknown: every attempt returned normally,
            # nothing raised, and pyserial's own return values, read this
            # time instead of discarded, say fewer bytes reached the line
            # than were asked for even after `_write_with_bounded_retry`'s
            # own retry of the remainder. There is no missing proof here for
            # a later call to supply, so this does not hold the lease the
            # way an exception mid-write does: it records what happened,
            # by its own reason, and leaves the session usable.
            result = {
                "ok": False,
                "tool": tool,
                "port_id": port_id,
                "error_type": "serial_write_incomplete",
                "summary": f"COM port write was short: {sent} of {len(data)} byte(s) reached the line, even after a bounded retry of the remainder.",
                "bytes_written": sent,
                "bytes_requested": len(data),
                "data": data_result(sent_data, session.port_config.encoding),
                "log_path": display_path(self.config, session.log_path),
                "side_effect_committed": True,
                "side_effect_status": "committed",
                "retry_safe": False,
                "likely_causes": likely_causes("serial_write_incomplete"),
            }
            session.lease.record_cleanup_event("serial_write_incomplete", RuntimeError(result["summary"]))
            if audit_error is not None:
                session.audit_broken = True
                session.lease.quarantine("com_write_audit_broken", audit_error, audit_broken=True)
                return mark_audit_failure(result, audit_error)
            return result
        result = {"ok": True, "tool": tool, "port_id": port_id, "bytes_written": sent, "data": data_result(sent_data, session.port_config.encoding), "log_path": display_path(self.config, session.log_path), "summary": "Stimulus written to COM port."}
        if audit_error is not None:
            session.audit_broken = True
            session.lease.quarantine("com_write_audit_broken", audit_error, audit_broken=True)
            return mark_audit_failure(result, audit_error)
        return result

    def read(self, port_id: str, max_bytes: object | None = None, wait_timeout_s: object = 0.0) -> JsonObject:
        return self._write_report(self.read_bytes(port_id, max_bytes, wait_timeout_s, "com_read"))

    def read_bytes(self, port_id: str, max_bytes: object | None = None, wait_timeout_s: object = 0.0, tool: str = "com_read") -> JsonObject:
        port = self._configured_port(port_id, tool)
        if not port["ok"]:
            return port
        if not self.config.com_read_allowed(port["port_config"]):
            return self._permission_denied(tool, "Reading this COM port is disabled by the authoritative config.", port_id)
        session_result = self._active_session(port_id, tool)
        if not session_result["ok"]:
            return session_result
        session = session_result["session"]
        try:
            parsed_max_bytes = session.port_config.max_buffer_bytes if max_bytes is None else int(max_bytes)
            parsed_wait_timeout_s = float(wait_timeout_s)
        except (TypeError, ValueError):
            return {"ok": False, "tool": tool, "port_id": port_id, "error_type": "invalid_argument", "summary": "max_bytes must be an integer and wait_timeout_s must be a number."}
        if parsed_max_bytes < 1:
            return {"ok": False, "tool": tool, "port_id": port_id, "error_type": "invalid_argument", "summary": "max_bytes must be at least 1."}
        if not math.isfinite(parsed_wait_timeout_s):
            return {"ok": False, "tool": tool, "port_id": port_id, "error_type": "invalid_argument", "summary": "wait_timeout_s must be finite."}
        deadline = time.monotonic() + max(0.0, min(parsed_wait_timeout_s, 60.0))
        while self._session_is_active(session):
            with session.lock:
                if session.buffer:
                    break
            if time.monotonic() >= deadline:
                break
            time.sleep(0.01)
        with session.lock:
            data = bytes(session.buffer[:parsed_max_bytes])
            del session.buffer[:parsed_max_bytes]
            remaining = len(session.buffer)
        if not data and session.reader_error is not None:
            # The wait loop above exited because the reader died while this
            # call was waiting on it, not because the session was fine and
            # the deadline simply passed: `_active_session` already refuses
            # a read against a session in this state before the wait even
            # starts, and a reader that dies mid-wait deserves exactly that
            # refusal rather than a quiet "no feedback" once it is too late
            # to ask before the fact. Re-checking now is the same question,
            # asked one call later, and it reuses the same answer rather than
            # inventing a second way to say a session is not active.
            failure = self._active_session(port_id, tool)
            if not failure["ok"]:
                return failure
        result: JsonObject = {"ok": True, "tool": tool, "port_id": port_id, "bytes_read": len(data), "buffer_remaining_bytes": remaining, "overflow_bytes": session.overflow_bytes, "data": data_result(data, session.port_config.encoding), "log_path": display_path(self.config, session.log_path), "summary": "Feedback read from COM port." if data else "No COM port feedback was available."}
        if session.reader_error:
            result["reader_error"] = session.reader_error
        return result

    def close(self) -> None:
        errors: list[tuple[str, BaseException]] = []
        interrupt: BaseException | None = None
        for port_id in list(self.sessions):
            try:
                result = self.session_stop(port_id)
                if not overall_success(result):
                    raise RuntimeError(str(result.get("summary", "COM port cleanup failed.")))
            except BaseException as error:
                errors.append((port_id, error))
                if interrupt is None and isinstance(error, (KeyboardInterrupt, SystemExit)):
                    interrupt = error
        for provisional_error in cleanup_provisional_handles(self.coordinator.owner_marker):
            errors.append(("provisional_handle", RuntimeError(provisional_error)))
        if self._owns_coordinator and not errors:
            self.coordinator.close()
        if interrupt is not None:
            interrupt.args = (*interrupt.args, "Cleanup errors: " + "; ".join(f"{port_id}: {type(error).__name__}: {error}" for port_id, error in errors))
            raise interrupt
        if errors:
            details = "; ".join(f"{port_id}: {type(error).__name__}: {error}" for port_id, error in errors)
            raise RuntimeError(f"COM port cleanup failed: {details}") from errors[0][1]

    def _open_serial(self, port_id: str, port_config: ComPortConfig, log_path: str, lease: HardwareLease, contact: ContactMarker) -> JsonObject:
        """Open the port, recording contact the moment it becomes provable.

        ``contact`` is created by the caller rather than here, because the caller
        also has to be able to read it when this method does not return at all: a
        `KeyboardInterrupt` between the constructor and the open leaves a marker
        that says, truthfully, that nothing was reached.

        What the marker means on this backend: the successful return of
        ``open()``. pyserial applies the configured DTR and RTS states inside
        ``open()``, so by the time it returns the modem lines have been driven and
        a board that wires DTR to reset has been reset. The marker is exactly the
        line between "that may have happened" and "it cannot have".

        A failed ``open()`` stays a refusal, and deliberately, even though on
        POSIX the descriptor is created before the parts of the open that can
        fail. That reading has not changed: a failed open does at most what a
        normal open/close cycle does, and that cycle has never needed a physical
        inspection. The marker draws its line where the *session* begins, not
        where the first syscall does.
        """
        try:
            import serial
        except ImportError:
            return {"ok": False, "tool": "com_session_start", "port_id": port_id, "error_type": "serial_backend_not_available", "summary": "pyserial is not installed or could not be imported.", "likely_causes": ["install Agentic HIL with its runtime dependencies", "pyserial installation is broken"], "side_effect_committed": False}

        def open_failure(error: BaseException) -> JsonObject:
            return {"ok": False, "tool": "com_session_start", "port_id": port_id, "error_type": "com_port_open_failed", "summary": "COM port could not be opened.", "backend_error": str(error), "likely_causes": likely_causes("com_port_open_failed")}

        try:
            # Built unopened so the modem lines are decided BEFORE the port is
            # opened. Passing the device to the constructor opens it immediately
            # with pyserial's own defaults, which raise DTR and RTS, and on a
            # board that wires DTR to reset, listening to a target restarts it.
            # pyserial applies the requested states as part of open(); a driver
            # that pulses a line during the open itself is beyond what any host
            # can suppress, which is why listen_only and this pair are described
            # as evidence of intent, not as a hardware guarantee.
            serial_handle = serial.Serial()
            serial_handle.port = port_config.device
            serial_handle.baudrate = port_config.baudrate
            serial_handle.timeout = port_config.timeout_s
            serial_handle.write_timeout = port_config.write_timeout_s
            serial_handle.dtr = port_config.assert_dtr
            serial_handle.rts = port_config.assert_rts
            # Asked for exclusively, because sharing a line was never a mode this
            # could work in. Two processes on one port interleave their bytes,
            # and what a caller then sees is a reply that does not answer the
            # stimulus: an unknown board state, which is a quarantine, arriving
            # mid-session and blaming the wrong thing. The machine-wide device
            # lock never covered this: it binds the runs that take it, and a
            # foreign test runner or serial monitor does not.
            #
            # Windows opens serial devices exclusively whatever this says (the
            # handle is created with a share mode of zero), so there the flag is
            # a declaration of what is already true. On POSIX it is the whole
            # difference: without it a second open succeeds and nothing refuses
            # anything. A handle that will not accept the flag is not opened, and
            # falls to the pre-open refusal below with the rest of the
            # configuration failures.
            serial_handle.exclusive = True
        except Exception as error:
            # No OS handle exists before open(): configuring the unopened
            # object cannot touch the device, so this failure proves the port
            # was never reached and must refuse rather than quarantine.
            return {**open_failure(error), "side_effect_committed": False, "side_effect_status": "not_started", "retry_safe": True}
        try:
            serial_handle.open()
        except Exception as error:
            # The dominant failures (device absent, port held by another
            # program, adapter unplugged) end here. pyserial's contract for a
            # failed open() is that no open handle remains (it closes any
            # partially created descriptor on its own error path and leaves
            # is_open False); that contract is verified rather than assumed,
            # and a handle that contradicts it is closed here. Only a close
            # that then fails leaves the unknown state that still quarantines.
            # The port never carried a byte of this session either way: a
            # failed open does at most what a normal open/close cycle does, and
            # that cycle never needed a physical inspection.
            busy = serial_port_busy(error, port_id, port_config)
            failure = busy if busy is not None else open_failure(error)
            if not getattr(serial_handle, "is_open", False):
                return {**failure, "cleanup_confirmed": True, "side_effect_committed": False, "side_effect_status": "not_started", "retry_safe": True}
            # A handle that says it is open after the open that failed
            # contradicts the contract, and a descriptor on the device is the
            # thing that contradiction would mean. That is not proof of contact
            # (a flag left standing by an object that mismanaged its own state is
            # not evidence of a syscall), but it is squarely not proof of the
            # absence of it either, so the marker withholds both answers and the
            # handling below decides on the close exactly as it always has.
            contact.record_unproven("serial_open_left_a_handle")
            try:
                serial_handle.close()
            except Exception as close_error:
                # A handle that contradicts the contract and then will not close
                # is the one unknown here, and it outranks any classification of
                # why the open failed.
                return {**open_failure(error), "cleanup_error": str(close_error)}
            return {**failure, "cleanup_confirmed": True, "side_effect_committed": False, "side_effect_status": "not_started", "retry_safe": True}
        contact.record("serial_open")
        try:
            provisional = register_provisional_handle(self.coordinator.owner_marker, f"com:{port_id}", serial_handle.close)
        except BaseException as register_error:
            # The open handle could not be registered for retried cleanup, so
            # it is closed right here; a close failure leaves the markerless
            # result whose quarantine is exactly the unconfirmed handle.
            try:
                serial_handle.close()
            except BaseException as cleanup_error:
                if not isinstance(register_error, Exception):
                    register_error.args = (*register_error.args, f"Cleanup error: {cleanup_error}")
                    raise
                return {**open_failure(register_error), "cleanup_error": str(cleanup_error)}
            if not isinstance(register_error, Exception):
                raise
            return {**open_failure(register_error), "cleanup_confirmed": True}
        try:
            session = ComPortSession(port_id, port_config, serial_handle, log_path, lease, start_reader=False, contact=contact)
        except BaseException as primary_error:
            try:
                serial_handle.close()
            except BaseException as cleanup_error:
                # The raw handle stays registered so service.close() can retry
                # closing it; do not discharge the provisional owner here. The
                # unconfirmed handle is exactly the unknown that justifies the
                # quarantine the caller raises over this markerless result.
                if not isinstance(primary_error, Exception):
                    primary_error.args = (*primary_error.args, f"Cleanup error: {cleanup_error}")
                    raise
                return {**open_failure(primary_error), "cleanup_error": str(cleanup_error)}
            discharge_provisional_handle(provisional)
            if not isinstance(primary_error, Exception):
                raise
            # The raw handle was confirmed closed, so the port ends where a
            # normal open/close cycle ends it.
            return {**open_failure(primary_error), "cleanup_confirmed": True, "side_effect_committed": False, "side_effect_status": "not_started", "retry_safe": True}
        discharge_provisional_handle(provisional)
        return {"ok": True, "session": session}

    def _configured_port(self, port_id: str, tool: str) -> JsonObject:
        if not port_id:
            return {"ok": False, "tool": tool, "error_type": "invalid_argument", "summary": "port_id is required."}
        port_config = self.config.com_ports.get(port_id)
        if port_config is None:
            return {"ok": False, "tool": tool, "port_id": port_id, "error_type": "com_port_not_configured", "summary": "COM port is not available in the authoritative config.", "configured_ports": sorted(self.config.com_ports.keys())}
        return {"ok": True, "port_config": port_config}

    def _active_session(self, port_id: str, tool: str) -> JsonObject:
        port = self._configured_port(port_id, tool)
        if not port["ok"]:
            return port
        session = self.sessions.get(port_id)
        if session is None or self.coordinator.incident_stands or session.audit_broken or session.lease.state != "active" or not self._session_is_active(session):
            result: JsonObject = {"ok": False, "tool": tool, "port_id": port_id, "error_type": "session_not_active", "summary": "COM port session is not active. Start it with com_session_start first."}
            if session is not None and (self.coordinator.incident_stands or session.audit_broken or session.lease.state != "active"):
                result.update({"error_type": "resource_quarantined", "summary": "COM port requires cleanup or audit recovery before further actions.", "cleanup_required": True, "quarantined": True})
            if session is not None and session.reader_error:
                result["reader_error"] = session.reader_error
                result["summary"] = "COM port session failed and is no longer active. Start it again with com_session_start."
            return result
        return {"ok": True, "session": session}

    def _port_status(self, port_id: str, port_config: ComPortConfig, session: ComPortSession | None) -> JsonObject:
        # assert_dtr/assert_rts are reported because they decide whether merely
        # opening this port touches the target; a reader judging whether an
        # observation was passive needs to see them without opening the config.
        result: JsonObject = {"device": port_config.device, "baudrate": port_config.baudrate, "encoding": port_config.encoding, "max_buffer_bytes": port_config.max_buffer_bytes, "max_write_bytes": port_config.max_write_bytes, "assert_dtr": port_config.assert_dtr, "assert_rts": port_config.assert_rts, "session_active": False}
        # Which hardware this entry names, next to the name it is reached by. A
        # reader deciding whether a port is safely addressed needs both.
        result.update(port_identity_fields(self.config, port_id))
        if session is not None:
            result.update(self._session_status(session))
        return result

    def _session_status(self, session: ComPortSession) -> JsonObject:
        result: JsonObject = {"session_active": self._session_is_active(session), "started_at": session.started_at, "rx_buffer_bytes": len(session.buffer), "overflow_bytes": session.overflow_bytes, "log_path": display_path(self.config, session.log_path)}
        if session.reader_error:
            result["reader_error"] = session.reader_error
        return result

    def _session_is_active(self, session: ComPortSession) -> bool:
        if session.reader_error is not None:
            return False
        return session.active and bool(getattr(session.serial_handle, "is_open", True))

    def _clear_buffers(self, session: ComPortSession) -> JsonObject:
        reset_started = False
        try:
            with session.io_lock:
                reset = getattr(session.serial_handle, "reset_input_buffer", None)
                if callable(reset):
                    reset_started = True
                    reset()
                with session.lock:
                    session.buffer.clear()
                    session.overflow_bytes = 0
        except BaseException as error:
            result: JsonObject = {"ok": False, "tool": "com_session_start", "port_id": session.port_id, "error_type": "com_buffer_clear_failed", "summary": "COM input buffers could not be cleared.", "backend_error": str(error), "side_effect_committed": reset_started, "side_effect_status": "unknown" if reset_started else "not_started", "retry_safe": not reset_started}
            if reset_started:
                session.lease.quarantine("com_buffer_clear_unconfirmed", error)
                result.update({"cleanup_required": True, "quarantined": True})
            if not isinstance(error, Exception):
                raise
            return result
        return {"ok": True}

    def _stop_session(self, session: ComPortSession, reason: str, *, defer_release: bool = False) -> Exception | None:
        session.active = False
        errors: list[tuple[str, BaseException]] = []
        interrupt: BaseException | None = None
        cancel_read = getattr(session.serial_handle, "cancel_read", None)
        if callable(cancel_read):
            try:
                cancel_read()
            except BaseException as error:
                errors.append(("cancel_read", error))
                if isinstance(error, (KeyboardInterrupt, SystemExit)):
                    interrupt = error
        try:
            session.serial_handle.close()
        except BaseException as error:
            errors.append(("close", error))
            if interrupt is None and isinstance(error, (KeyboardInterrupt, SystemExit)):
                interrupt = error
        if session.reader is not None and session.reader is not threading.current_thread():
            try:
                session.reader.join(timeout=max(1.0, session.port_config.timeout_s + 0.5))
                if session.reader.is_alive():
                    raise RuntimeError("COM port reader remained active after close.")
            except BaseException as error:
                errors.append(("reader", error))
                if interrupt is None and isinstance(error, (KeyboardInterrupt, SystemExit)):
                    interrupt = error
        audit_error = append_jsonl_audited(self.config, session.log_path, {"event": "stop", "reason": reason})
        if audit_error is not None or session.audit_broken:
            session.audit_broken = True
            session.lease.quarantine("com_audit_broken", audit_error, audit_broken=True)
            errors.append(("audit", audit_error or RuntimeError("COM audit state was already broken.")))
        if errors:
            if session.audit_broken:
                # The audit is the one thing no later call re-establishes, so a
                # cleanup that failed beside a broken ledger keeps the incident
                # it always had.
                session.lease.quarantine("com_cleanup_unconfirmed", errors[0][1], audit_broken=True)
            else:
                # Recorded and released. The close, the reader join, or both did
                # not confirm, and the failure below still says so with the same
                # reason and the same remediation; the port is not held for it,
                # because the next open is what settles a handle either way.
                session.lease.record_cleanup_event("com_cleanup_unconfirmed", errors[0][1])
                if not defer_release:
                    session.lease.release()
            details = "; ".join(f"{name}: {type(error).__name__}: {error}" for name, error in errors)
            if interrupt is not None:
                interrupt.args = (*interrupt.args, f"Cleanup errors: {details}")
                raise interrupt
            raise RuntimeError(details) from errors[0][1]
        if not defer_release and not session.lease.release():
            raise RuntimeError("COM resource release remains unconfirmed.")
        return audit_error

    def _close_failure(self, tool: str, port_id: str, error: Exception) -> JsonObject:
        return {
            "ok": False,
            "tool": tool,
            "port_id": port_id,
            "error_type": "com_port_close_failed",
            "summary": "COM port session could not be closed and remains registered for cleanup retry.",
            "backend_error": str(error),
        }

    def _write_report(self, result: JsonObject) -> JsonObject:
        prepared = mark_side_effect(result)
        port_id = prepared.get("port_id")
        session = self.sessions.get(port_id) if isinstance(port_id, str) else None
        unsafe_effect = prepared.get("side_effect_status") in {"unknown", "partial"}
        if session is not None and unsafe_effect:
            # Nothing consults the marker here, and that is the intended
            # asymmetry: a registered session exists only downstream of a
            # successful open, so every failure that reaches this line is an
            # after-contact failure and keeps precisely the quarantine it has
            # always had.
            session.lease.quarantine("com_effect_unconfirmed")
        if session is not None:
            # Published on every report this session writes, not only on the one
            # that opened it, because the reader that needs it (the release of a
            # dead owner's devices) sees whichever report was committed last.
            prepared = {**prepared, **session.contact.report_fields(), **session.lease.status()}
        written = write_report(self.config, prepared)
        if session is not None and written.get("audit_ok") is False:
            session.audit_broken = True
            session.lease.quarantine("com_report_audit_broken", audit_broken=True)
            written = write_report(self.config, {**written, **session.lease.status()})
        return written

    def _write_unattached_lease_report(self, result: JsonObject, lease: HardwareLease, *, release_if_safe: bool) -> JsonObject:
        written = write_report(self.config, {**mark_side_effect(result), **lease.status()})
        if written.get("audit_ok") is False:
            lease.quarantine("com_report_audit_broken", audit_broken=True)
            return write_report(self.config, {**written, **lease.status()})
        if release_if_safe and not lease.release():
            return write_report(self.config, {**written, **lease.status(), "ok": False, "cleanup_required": True, "summary": "COM lease release remained unconfirmed."})
        return recommit_report_with_status(self.config, written, lease.status())

    def _permission_denied(self, tool: str, summary: str, port_id: str | None = None) -> JsonObject:
        result: JsonObject = {"ok": False, "tool": tool, "error_type": "permission_denied", "summary": summary}
        if port_id:
            result["port_id"] = port_id
        return result


def payload_bytes(port_config: ComPortConfig, payload: JsonObject) -> JsonObject:
    if set(payload) - {"text", "hex"}:
        return {"ok": False, "tool": "com_write", "error_type": "invalid_argument", "summary": "COM write payload contains unsupported fields."}
    has_text = payload.get("text") is not None
    has_hex = payload.get("hex") is not None
    if has_text == has_hex:
        return {"ok": False, "tool": "com_write", "error_type": "invalid_argument", "summary": "Provide exactly one of text or hex."}
    if has_text:
        if not isinstance(payload.get("text"), str):
            return {"ok": False, "tool": "com_write", "error_type": "invalid_argument", "summary": "text must be a string."}
        try:
            return {"ok": True, "data": payload["text"].encode(port_config.encoding)}
        except LookupError:
            return {"ok": False, "tool": "com_write", "error_type": "config_invalid", "summary": "COM port encoding is not supported by Python.", "encoding": port_config.encoding}
        except UnicodeEncodeError:
            return {"ok": False, "tool": "com_write", "error_type": "invalid_argument", "summary": "text cannot be encoded with the configured COM port encoding.", "encoding": port_config.encoding}
    if not isinstance(payload.get("hex"), str):
        return {"ok": False, "tool": "com_write", "error_type": "invalid_argument", "summary": "hex must be a string."}
    cleaned = re.sub(r"\s+", "", payload["hex"])
    if len(cleaned) % 2 != 0 or re.fullmatch(r"[0-9a-fA-F]*", cleaned) is None:
        return {"ok": False, "tool": "com_write", "error_type": "invalid_argument", "summary": "hex must contain valid hexadecimal bytes."}
    return {"ok": True, "data": bytes.fromhex(cleaned)}


def data_result(data: bytes, encoding: str) -> JsonObject:
    return {"hex": data.hex(), "text": decode_bytes(data, encoding), "encoding": encoding}


def decode_bytes(data: bytes, encoding: str) -> str:
    try:
        return data.decode(encoding, errors="replace")
    except LookupError:
        return data.decode("utf-8", errors="replace")


def serial_by_id_links(directory: str = SERIAL_BY_ID_DIRECTORY) -> dict[str, str]:
    """Every stable serial-port name this host publishes, keyed by what it resolves to.

    Linux's udev maintains ``/dev/serial/by-id/``: one symlink per port, named
    after the vendor, the product and the device's own serial number, and
    therefore unmoved by the enumeration order that decides ``ttyACM0`` from
    ``ttyACM1``. That name is what a configuration should hold.

    Windows publishes no counterpart that can be *opened*. It knows the identity
    perfectly well (the device instance path carries the same USB serial), but
    the only openable name is ``COMn``, and ``COMn`` moves. There the stable half
    of this fix is ``com_ports.<name>.serial_number`` plus the check at open
    time, and this returns nothing rather than pretending otherwise.

    Never raises: an unreadable ``/dev`` is a host without stable names, which is
    the same answer as a host that has none. Windows is short-circuited rather
    than left to fail the listing, because there a POSIX path is resolved against
    the current drive and probing ``C:\\dev`` once per enumeration would be a
    filesystem call whose answer is known."""
    if os.name == "nt":
        return {}
    links: dict[str, str] = {}
    try:
        entries = sorted(Path(directory).iterdir())
    except OSError:
        return {}
    for entry in entries:
        try:
            resolved = os.path.realpath(str(entry))
        except OSError:  # pragma: no cover - realpath does not raise for a plain string on POSIX
            continue
        # First spelling wins, and the listing above is sorted, so a port that
        # udev gave two links keeps the same one across runs. Both name one
        # device, so there is no wrong answer here, only an unstable one, and an
        # unstable answer would make adoption rewrite the file on every call.
        links.setdefault(resolved, str(entry))
    return links


def available_port_info(port_info: object, stable_names: dict[str, str] | None = None) -> JsonObject:
    result: JsonObject = {"device": str(getattr(port_info, "device", "") or getattr(port_info, "name", ""))}
    for attr, output_name in [("name", "name"), ("description", "description"), ("hwid", "hwid"), ("manufacturer", "manufacturer"), ("product", "product"), ("interface", "interface"), ("location", "location"), ("serial_number", "serial_number")]:
        value = getattr(port_info, attr, None)
        if value is not None:
            result[output_name] = str(value)
    for attr, output_name in [("vid", "vid"), ("pid", "pid")]:
        value = getattr(port_info, attr, None)
        if value is not None:
            result[output_name] = value
    stable = stable_device_name(result["device"], stable_names or {})
    if stable is not None:
        result["stable_device"] = stable
    return result


def stable_device_name(device: str, stable_names: dict[str, str]) -> str | None:
    """The replug-proof name for one enumerated port, if this host publishes one.

    A device that already *is* a stable name is returned unchanged, so a caller
    that hands this an entry read out of a configuration gets the same answer as
    one that hands it a kernel name.

    The listing is keyed by what udev's symlinks resolve to, which for the
    ordinary ``/dev/ttyACM0`` is that name itself, so the direct lookup answers
    the common case without a syscall, and ``realpath`` is the fallback for a
    device node that is itself a link."""
    if not device:
        return None
    if is_stable_device_name(device):
        return device
    if not stable_names:
        return None
    direct = stable_names.get(device)
    if direct is not None:
        return direct
    try:
        return stable_names.get(os.path.realpath(device))
    except OSError:  # pragma: no cover - realpath does not raise for a plain string on POSIX
        return None


def likely_causes(error_type: str) -> list[str]:
    return {
        "com_port_open_failed": ["configured COM port device does not exist", "COM port is already open in another program", "USB serial adapter is unplugged or driver is missing"],
        "serial_read_failed": ["COM port was disconnected", "serial driver reported an I/O error", "another process interfered with the port"],
        "serial_write_failed": ["COM port was disconnected", "serial driver write timed out", "target or USB serial adapter stopped responding"],
        "serial_write_incomplete": ["configured write_timeout_s is too short for this payload size and baudrate", "target or USB serial adapter is applying flow control", "COM port was disconnected partway through the write"],
    }.get(error_type, ["inspect the COM port log for details"])
