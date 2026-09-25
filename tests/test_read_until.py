"""`com_read` waits for a pattern, and `can_read` waits for a frame id.

`com_read` ends its wait on the first byte that arrives, and `can_read` ends its
wait on the first frame. A boot banner or a test verdict arrives over many
reader chunks, so a caller waiting for `PASS` called `com_read` again and again,
and every one of those calls was a full request. `until` and `until_id` let the
server wait for what the caller is actually waiting for, so the polling series
becomes one call.

Everything here runs against the suite's own stand-ins: a pyserial handle that
hands out scripted chunks, a python-can bus that hands out scripted frames, and
the protocol 2 bridge in `fixtures/fake_can_bridge.py` behind the real
`adapter: process` path. No port, adapter or board is touched.
"""

from __future__ import annotations

import json
import queue
import re
import sys
import threading
import time
from collections import deque
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import write_config
from support import scaled_time_bound
from test_can_frame_and_routing import fake_can_module

from agentic_hil.config import load_config
from agentic_hil.contracts import MCP_TOOLS
from agentic_hil.mcp import handle_mcp_message
from agentic_hil.report import read_last_report
from agentic_hil.tools import AgenticHILToolService

PORT_ID = "read_until_uart"
ASCII_PORT_ID = "read_until_ascii"
LATIN1_PORT_ID = "read_until_latin1"
COM_PORTS_YAML = (
    "com_ports:\n"
    f"  {PORT_ID}:\n"
    '    device: "/dev/ttyREADUNTIL0"\n'
    f"  {ASCII_PORT_ID}:\n"
    '    device: "/dev/ttyREADUNTIL1"\n'
    '    encoding: "ascii"\n'
    f"  {LATIN1_PORT_ID}:\n"
    '    device: "/dev/ttyREADUNTIL2"\n'
    '    encoding: "latin-1"\n'
)

BUS_ID = "read_until_bus"
BRIDGE_BUS_ID = "read_until_bridge"
FAKE_CAN_BRIDGE = Path(__file__).parent / "fixtures" / "fake_can_bridge.py"
ADAPTER_KINDS = [pytest.param("python-can", id="python-can"), pytest.param("bridge", id="bridge")]

# What the two reads answer today, key for key: their own fields, and the
# fields the service adds to every device tool's answer (the lease, the first
# contact, the configuration in force, the report). A call without the new
# arguments keeps answering exactly this.
ENVELOPE_KEYS = {
    "audit_ok",
    "cleanup_reasons",
    "cleanup_required",
    "config_in_force",
    "first_contact_at",
    "first_contact_by",
    "lease_id",
    "lease_state",
    "processes_reaped",
    "quarantine_id",
    "quarantined",
    "report_path",
    "resources",
    "safe_state_confirmed",
}
TODAY_COM_READ_KEYS = {"ok", "tool", "port_id", "bytes_read", "buffer_remaining_bytes", "overflow_bytes", "data", "log_path", "summary"} | ENVELOPE_KEYS
TODAY_CAN_READ_KEYS = {"ok", "tool", "bus_id", "adapter", "frames_read", "frames", "adapter_result", "log_path", "summary"} | ENVELOPE_KEYS
TODAY_DESCRIPTIONS = {
    "com_read": "Read buffered feedback from an active COM port session. Use this instead of screen, minicom, or picocom.",
    "can_read": "Read CAN frames from an active configured CAN bus session. Use this instead of candump.",
}


# ---------------------------------------------------------------------------
# Stand-ins and helpers.

# A fed chunk that fails the read instead, the way an unplugged device does.
DIE = object()


class ScriptedSerialHandle:
    """A pyserial handle that hands out the chunks a test feeds it, one per read.

    The session's reader thread is the only caller of `read`, and each call
    hands out at most one fed chunk, so a line fed as two chunks reaches the
    session buffer in two reader passes, the way a slow link delivers it.
    `drained` is set by a read that finds nothing left to hand out. The reader
    only gets there after it has buffered and audited every earlier chunk, so a
    test waits on it before calling instead of sleeping.
    """

    def __init__(self) -> None:
        self.port = ""
        self.baudrate = 0
        self.timeout: float | None = None
        self.write_timeout: float | None = None
        self.dtr: bool | None = None
        self.rts: bool | None = None
        self.exclusive: bool | None = None
        self.is_open = False
        self.chunks: deque[object] = deque()
        self.lock = threading.Lock()
        self.drained = threading.Event()

    @property
    def in_waiting(self) -> int:
        with self.lock:
            head = self.chunks[0] if self.chunks else b""
        return len(head) if isinstance(head, bytes) else 0

    def open(self) -> None:
        self.is_open = True

    def read(self, size: int) -> bytes:
        with self.lock:
            if not self.chunks:
                self.drained.set()
                return b""
            head = self.chunks.popleft()
            if head is DIE:
                raise OSError("device disconnected mid-read")
            chunk = bytes(head)
            if len(chunk) > size:
                self.chunks.appendleft(chunk[size:])
            return chunk[:size]

    def feed(self, *chunks: object) -> None:
        with self.lock:
            self.drained.clear()
            self.chunks.extend(chunks)

    def deliver(self, *chunks: bytes) -> None:
        """Feed `chunks` and return once the session has buffered every one of them."""
        self.feed(*chunks)
        assert self.drained.wait(scaled_time_bound(5.0)), "the session reader never took the fed chunks"

    def write(self, data: bytes) -> int:
        return len(data)

    def flush(self) -> None:
        return None

    def reset_input_buffer(self) -> None:
        with self.lock:
            self.chunks.clear()

    def cancel_read(self) -> None:
        return None

    def close(self) -> None:
        self.is_open = False


# A scripted adapter read that answers nothing, the way a quiet bus answers a
# read that ran out its timeout.
QUIET = object()
# A scripted adapter read that fails, the way a controller error does.
FAIL = object()


class ScriptedBus:
    """A python-can bus stand-in whose `recv` hands out scripted frames in order.

    `QUIET` in the inbox answers one `recv` with nothing, and an exception in it
    is raised from `recv`. `asked_timeouts` is every timeout `recv` was called
    with, in order, which is how a test sees the way a wait was carried out.
    """

    def __init__(self) -> None:
        self.inbox: queue.Queue[object] = queue.Queue()
        self.asked_timeouts: list[float | None] = []
        self.sent: list[object] = []
        self.closed = False

    def recv(self, timeout: float | None = None) -> object:
        self.asked_timeouts.append(timeout)
        try:
            item = self.inbox.get(timeout=timeout) if timeout else self.inbox.get_nowait()
        except queue.Empty:
            return None
        if isinstance(item, BaseException):
            raise item
        return None if item is QUIET else item

    def send(self, message: object, timeout: float | None = None) -> None:
        self.sent.append(message)

    def shutdown(self) -> None:
        self.closed = True


def frame(frame_id: int, data_hex: str, *, extended: bool = False) -> dict:
    """One scripted frame, in the shape the fake bridge takes."""
    return {"id": frame_id, "data_hex": data_hex, "extended": extended}


def message(spec: dict) -> SimpleNamespace:
    """The same frame as the `can.Message` a python-can bus hands out."""
    data = bytes.fromhex(spec["data_hex"])
    return SimpleNamespace(arbitration_id=spec["id"], is_extended_id=spec["extended"], is_remote_frame=False, data=data, dlc=len(data))


A = frame(0x100, "a1")
B = frame(0x101, "b1")
C = frame(0x102, "c1")
D = frame(0x103, "d1")
MATCH_ID = 0x200
MATCH = frame(MATCH_ID, "0d")
UNSEEN_ID = 0x7FF


def frame_ids(result: dict) -> list[int]:
    return [received["id"] for received in result.get("frames", [])]


@contextmanager
def after(delay_s: float, action: Callable[..., object], *args: object) -> Iterator[None]:
    """Run `action(*args)` on a timer `delay_s` from now, and reap the timer on the way out."""
    timer = threading.Timer(delay_s, action, args=args)
    timer.start()
    try:
        yield
    finally:
        timer.cancel()
        timer.join()


def close(service: AgenticHILToolService) -> None:
    """Close the service, and end every device hold even when the close refuses."""
    try:
        service.close()
    finally:
        service.coordinator.bench.release_all()


def install_serial(monkeypatch: pytest.MonkeyPatch) -> ScriptedSerialHandle:
    handle = ScriptedSerialHandle()
    monkeypatch.setitem(sys.modules, "serial", SimpleNamespace(Serial=lambda *args, **kwargs: handle))
    return handle


@contextmanager
def com_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, port_id: str = PORT_ID) -> Iterator[tuple[AgenticHILToolService, ScriptedSerialHandle]]:
    """A service with a started session on `port_id`, whose line the test feeds."""
    handle = install_serial(monkeypatch)
    service = AgenticHILToolService(load_config(str(write_config(tmp_path, com_ports_yaml=COM_PORTS_YAML))), frontend="mcp")
    try:
        started = service.call("com_session_start", {"port_id": port_id})
        assert started["ok"] is True, started
        yield service, handle
    finally:
        close(service)


def com_read(service: AgenticHILToolService, port_id: str = PORT_ID, **arguments: object) -> dict:
    return service.call("com_read", {"port_id": port_id, **arguments})


def wait_for_reader_death(service: AgenticHILToolService, port_id: str = PORT_ID) -> None:
    """Return once the session's reader has recorded the error it died of."""
    session = service.com_ports.sessions[port_id]
    deadline = time.monotonic() + scaled_time_bound(5.0)
    while session.reader_error is None:
        assert time.monotonic() < deadline, "the session reader never died"
        time.sleep(0.01)


def can_buses_yaml(kind: str, timeout_s: float, poll_interval_ms: int | None = None) -> str:
    poll = "" if poll_interval_ms is None else f"    poll_interval_ms: {poll_interval_ms}\n"
    if kind == "python-can":
        return f'can_buses:\n  {BUS_ID}:\n    adapter: "socketcan"\n    channel: "vcan613"\n    timeout_s: {timeout_s}\n{poll}'
    return (
        f'can_buses:\n  {BRIDGE_BUS_ID}:\n    adapter: "process"\n    channel: "vcan614"\n'
        f'    executable: "{FAKE_CAN_BRIDGE.as_posix()}"\n    timeout_s: {timeout_s}\n{poll}'
    )


@contextmanager
def can_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str, script: list, *, timeout_s: float = 5.0, poll_interval_ms: int | None = None
) -> Iterator[SimpleNamespace]:
    """A service with a started session on a bus whose adapter reads answer `script`.

    Each entry of `script` is what one adapter read answers: a list of frames,
    possibly empty, or `FAIL`. The direct adapter gets it as frames in its bus
    with a `QUIET` after each read's frames, which ends that read the way an
    empty `recv` does; the bridge gets it as the fake bridge's own script.
    """
    bus = ScriptedBus()
    if kind == "python-can":
        for read in script:
            if read is FAIL:
                bus.inbox.put(OSError("staged receive failure"))
                continue
            for spec in read:
                bus.inbox.put(message(spec))
            bus.inbox.put(QUIET)
        monkeypatch.setitem(sys.modules, "can", fake_can_module(lambda **kwargs: bus))
        bus_id = BUS_ID
    else:
        failing = [index for index, read in enumerate(script) if read is FAIL]
        monkeypatch.setenv("FAKE_CAN_BRIDGE_RX", json.dumps(script[: failing[0]] if failing else script))
        if failing:
            monkeypatch.setenv("FAKE_CAN_BRIDGE_FAIL_READ", str(failing[0] + 1))
        else:
            monkeypatch.delenv("FAKE_CAN_BRIDGE_FAIL_READ", raising=False)
        bus_id = BRIDGE_BUS_ID
    service = AgenticHILToolService(load_config(str(write_config(tmp_path, can_buses_yaml=can_buses_yaml(kind, timeout_s, poll_interval_ms)))), frontend="mcp")
    try:
        started = service.call("can_session_start", {"bus_id": bus_id, "clear_rx_queue": False})
        assert started["ok"] is True, started
        yield SimpleNamespace(service=service, bus=bus, bus_id=bus_id)
    finally:
        close(service)


def can_read(rig: SimpleNamespace, **arguments: object) -> dict:
    return rig.service.call("can_read", {"bus_id": rig.bus_id, **arguments})


def assert_refused_for(refusal: dict, argument: str) -> None:
    """An `invalid_argument` refusal of the value `argument` was given.

    The schema today refuses an argument it does not know with this same error
    type and `validator: additionalProperties`, which is a refusal of the name.
    What is pinned here is a refusal of the value: by the schema under the
    argument's own path, or by the tool's own check naming the argument.
    """
    assert refusal["ok"] is False, refusal
    assert refusal["error_type"] == "invalid_argument", refusal
    assert refusal.get("validator") != "additionalProperties", refusal
    assert str(refusal.get("field", "")).startswith(argument) or argument in str(refusal.get("summary", "")), refusal


def tools_call(request_id: int, name: str, arguments: dict) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "method": "tools/call", "params": {"name": name, "arguments": arguments}}


# ---------------------------------------------------------------------------
# com_read with `until`.


@pytest.mark.parametrize(
    ("first", "second", "until", "expected"),
    [
        pytest.param(b"boot ok\r\nPA", b"SS\r\nidle\r\n", "PASS", b"boot ok\r\nPASS", id="pattern-split-in-the-middle"),
        pytest.param(b"caf\xc3", b"\xa9 ok\r\n", "\u00e9", b"caf\xc3\xa9", id="character-split-between-its-bytes"),
    ],
)
def test_com_read_until_finds_a_match_split_across_two_reader_chunks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, first: bytes, second: bytes, until: str, expected: bytes
) -> None:
    """The match is looked for in the session buffer, as bytes, and not in each
    chunk as it arrives. The first chunk is buffered before the call and holds
    only the start of the match, so the call has to go on waiting for the
    second; a UTF-8 character split between its two bytes is found the same way."""
    with com_session(tmp_path, monkeypatch) as (service, handle):
        handle.deliver(first)
        with after(0.3, handle.feed, second):
            result = com_read(service, until=until, wait_timeout_s=5)

    assert result["ok"] is True, result
    assert result["until_matched"] is True, result
    assert result["matched"] == until, result
    assert result["data"]["hex"] == expected.hex(), result
    assert result["bytes_read"] == len(expected), result


@pytest.mark.parametrize(
    "until",
    [pytest.param(["ABCD", "BC"], id="longer-entry-listed-first"), pytest.param(["BC", "ABCD"], id="shorter-entry-listed-first")],
)
def test_com_read_until_stops_at_the_entry_whose_match_ends_first(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, until: list[str]) -> None:
    """`ABCD` starts first and `BC` ends first. The call hands out as little as
    answers it, so the match that ends earliest wins, whatever the list order."""
    with com_session(tmp_path, monkeypatch) as (service, handle):
        handle.deliver(b"xABCDy")
        result = com_read(service, until=until, wait_timeout_s=5)

    assert result["ok"] is True, result
    assert result["until_matched"] is True, result
    assert result["matched"] == "BC", result
    assert result["data"]["text"] == "xABC", result
    assert result["buffer_remaining_bytes"] == 2, result


@pytest.mark.parametrize(
    ("until", "matched"),
    [pytest.param(["BC", "ABC"], "BC", id="shorter-entry-listed-first"), pytest.param(["ABC", "BC"], "ABC", id="longer-entry-listed-first")],
)
def test_com_read_until_on_a_tie_names_the_entry_listed_first(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, until: list[str], matched: str) -> None:
    """`ABC` and `BC` end on the same byte, so both hand out the same bytes.
    `matched` names the one the caller listed first."""
    with com_session(tmp_path, monkeypatch) as (service, handle):
        handle.deliver(b"xABCy")
        result = com_read(service, until=until, wait_timeout_s=5)

    assert result["ok"] is True, result
    assert result["until_matched"] is True, result
    assert result["matched"] == matched, result
    assert result["data"]["text"] == "xABC", result
    assert result["buffer_remaining_bytes"] == 1, result


def test_com_read_until_leaves_everything_after_the_match_buffered(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """What came after the match is the next thing the caller reads, not lost."""
    with com_session(tmp_path, monkeypatch) as (service, handle):
        handle.deliver(b"boot ok\r\nPASS\r\nidle\r\n")
        matched = com_read(service, until="PASS", wait_timeout_s=5)
        rest = com_read(service)

    assert matched["ok"] is True, matched
    assert matched["until_matched"] is True, matched
    assert matched["data"]["text"] == "boot ok\r\nPASS", matched
    assert matched["buffer_remaining_bytes"] == len(b"\r\nidle\r\n"), matched
    assert rest["data"]["text"] == "\r\nidle\r\n", rest
    assert rest["buffer_remaining_bytes"] == 0, rest


@pytest.mark.parametrize(
    ("max_bytes", "expected", "until_matched"),
    [
        pytest.param(5, b"boot ", False, id="well-before-the-match"),
        pytest.param(12, b"boot ok\r\nPAS", False, id="one-byte-short-of-the-match"),
        pytest.param(13, b"boot ok\r\nPASS", True, id="exactly-at-the-end-of-the-match"),
    ],
)
def test_com_read_until_never_hands_out_more_than_max_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, max_bytes: int, expected: bytes, until_matched: bool
) -> None:
    """`max_bytes` still caps the answer. A match that ends past it is not
    reported as found, and the summary says the match lies beyond `max_bytes`,
    so the caller knows to read on rather than to wait again. The match is
    already buffered, so none of these calls has anything to wait for."""
    fed = b"boot ok\r\nPASS\r\n"
    with com_session(tmp_path, monkeypatch) as (service, handle):
        handle.deliver(fed)
        started = time.monotonic()
        result = com_read(service, until="PASS", max_bytes=max_bytes, wait_timeout_s=20)
        elapsed = time.monotonic() - started

    assert result["ok"] is True, result
    assert result["until_matched"] is until_matched, result
    assert result["data"]["hex"] == expected.hex(), result
    assert result["bytes_read"] == len(expected), result
    assert result["buffer_remaining_bytes"] == len(fed) - len(expected), result
    if until_matched:
        assert result["matched"] == "PASS", result
    else:
        assert "matched" not in result, result
        assert "max_bytes" in result["summary"], result
    assert elapsed < scaled_time_bound(5.0), "the match was already buffered, so the call had nothing to wait for"


def test_com_read_until_answers_at_once_when_max_bytes_are_buffered_without_a_match(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing that arrives later can end a match inside the first `max_bytes`
    bytes, so waiting on would only delay the answer: the call hands those
    bytes out at once, unmatched, and says the match lies beyond `max_bytes`."""
    with com_session(tmp_path, monkeypatch) as (service, handle):
        handle.deliver(b"boot ok\r\n")
        started = time.monotonic()
        result = com_read(service, until="PASS", max_bytes=5, wait_timeout_s=20)
        elapsed = time.monotonic() - started

    assert result["ok"] is True, result
    assert result["until_matched"] is False, result
    assert "matched" not in result, result
    assert result["data"]["text"] == "boot ", result
    assert result["buffer_remaining_bytes"] == 4, result
    assert "max_bytes" in result["summary"], result
    assert elapsed < scaled_time_bound(5.0), "max_bytes bytes were buffered from the start, so the call had nothing to wait for"


def test_com_read_until_without_a_match_by_the_deadline_is_ok_and_names_the_wait(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A pattern that did not appear is feedback about the target, not a failed
    read. The call answers what was buffered, and its summary says the pattern
    was not seen and how long it was waited for."""
    with com_session(tmp_path, monkeypatch) as (service, handle):
        handle.deliver(b"boot\r\n")
        started = time.monotonic()
        result = com_read(service, until="PASS", wait_timeout_s=0.3)
        elapsed = time.monotonic() - started

    assert result["ok"] is True, result
    assert result["until_matched"] is False, result
    assert "matched" not in result, result
    assert result["data"]["text"] == "boot\r\n", result
    assert result["bytes_read"] == 6, result
    assert result["buffer_remaining_bytes"] == 0, result
    assert result["summary"] not in {"Feedback read from COM port.", "No COM port feedback was available."}, result
    assert "0.3" in result["summary"] or "300" in result["summary"], result
    assert elapsed >= 0.3 - 0.05, "bytes were buffered from the start, and the call still waited out its deadline for the pattern"


def test_com_read_until_matches_in_the_ports_own_encoding(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """On a latin-1 port `\u00e9` is the single byte e9. Encoded as UTF-8 it
    would be c3 a9, which this line never carries."""
    with com_session(tmp_path, monkeypatch, LATIN1_PORT_ID) as (service, handle):
        handle.deliver(b"caf\xe9 ok\r\n")
        result = com_read(service, LATIN1_PORT_ID, until="\u00e9", wait_timeout_s=5)

    assert result["ok"] is True, result
    assert result["until_matched"] is True, result
    assert result["matched"] == "\u00e9", result
    assert result["data"]["hex"] == b"caf\xe9".hex(), result


def test_com_read_until_without_wait_timeout_waits_by_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`until` alone waits (ten seconds), where a read without it answers at
    once. The line arrives half a second in, so the default is shown to be a
    wait without this test sitting through it."""
    with com_session(tmp_path, monkeypatch) as (service, handle):
        started = time.monotonic()
        with after(0.5, handle.feed, b"PASS\r\n"):
            result = com_read(service, until="PASS")
        elapsed = time.monotonic() - started

    assert result["ok"] is True, result
    assert result["until_matched"] is True, result
    assert result["data"]["text"] == "PASS", result
    assert elapsed < scaled_time_bound(5.0), "the match arrived half a second in and should have ended the wait"


def test_com_read_until_ends_its_wait_when_the_reader_dies_with_nothing_buffered(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A dead reader can deliver no match. The wait ends with it, and the call
    answers as a read against a dead reader answers today: the
    `session_not_active` refusal with the reader's error, whose backend line
    is the decisive one."""
    with com_session(tmp_path, monkeypatch) as (service, handle):
        started = time.monotonic()
        with after(0.3, handle.feed, DIE):
            result = com_read(service, until="PASS", wait_timeout_s=20)
        elapsed = time.monotonic() - started

    assert result["ok"] is False, result
    assert result["error_type"] == "session_not_active", result
    assert result["reader_error"]["error_type"] == "serial_read_failed", result
    assert result["reader_error"]["backend_error"] == "device disconnected mid-read", result
    assert elapsed < scaled_time_bound(5.0), "the reader died 0.3 s in and should have ended the wait"


def test_com_read_until_ends_its_wait_when_the_reader_dies_and_hands_out_the_buffer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The bytes the reader took off the line before it died are feedback the
    bench really produced, and they are handed out with the reader's error, as
    a plain read hands them out today."""
    with com_session(tmp_path, monkeypatch) as (service, handle):
        handle.deliver(b"boot\r\n")
        started = time.monotonic()
        with after(0.3, handle.feed, DIE):
            result = com_read(service, until="PASS", wait_timeout_s=20)
        elapsed = time.monotonic() - started

    assert result["ok"] is True, result
    assert result["until_matched"] is False, result
    assert "matched" not in result, result
    assert result["data"]["text"] == "boot\r\n", result
    assert result["reader_error"]["error_type"] == "serial_read_failed", result
    assert result["reader_error"]["backend_error"] == "device disconnected mid-read", result
    assert elapsed < scaled_time_bound(5.0), "the reader died 0.3 s in and should have ended the wait"


def test_com_read_until_hands_out_a_match_the_reader_buffered_before_it_died(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The reader took the match off the line and then died. The match is still
    the answer: the bytes through it are handed out as matched, with the
    reader's error, and what came after it stays buffered."""
    with com_session(tmp_path, monkeypatch) as (service, handle):
        handle.feed(b"boot\r\nPASS\r\nidle", DIE)
        wait_for_reader_death(service)
        started = time.monotonic()
        result = com_read(service, until="PASS", wait_timeout_s=20)
        elapsed = time.monotonic() - started

    assert result["ok"] is True, result
    assert result["until_matched"] is True, result
    assert result["matched"] == "PASS", result
    assert result["data"]["text"] == "boot\r\nPASS", result
    assert result["buffer_remaining_bytes"] == len(b"\r\nidle"), result
    assert result["reader_error"]["backend_error"] == "device disconnected mid-read", result
    assert elapsed < scaled_time_bound(5.0), "the reader was already dead, so the call had nothing to wait for"


@pytest.mark.parametrize(
    ("fed", "until", "wait_timeout_s", "until_matched", "matched"),
    [
        pytest.param(b"result: FAIL\r\n", ["PASS", "FAIL"], 5, True, "FAIL", id="matched"),
        pytest.param(b"result: pending\r\n", ["PASS"], 0.2, False, None, id="not-matched"),
    ],
)
def test_com_read_report_records_the_until_entries_and_the_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fed: bytes, until: list[str], wait_timeout_s: float, until_matched: bool, matched: str | None
) -> None:
    """The report says what the call waited for and whether it came, beside
    everything it records today."""
    with com_session(tmp_path, monkeypatch) as (service, handle):
        handle.deliver(fed)
        result = com_read(service, until=until, wait_timeout_s=wait_timeout_s)
        report = read_last_report(service.config)

    assert result["ok"] is True, result
    assert report["tool"] == "com_read", report
    assert report["until"] == until, report
    assert report["until_matched"] is until_matched, report
    assert report.get("matched") == matched, report
    assert report["data"] == result["data"], report


@pytest.mark.parametrize(
    ("until", "arguments", "until_wait_s"),
    [
        pytest.param("PASS", {}, 10.0, id="a-string-and-the-default-wait"),
        pytest.param(["PASS"], {"wait_timeout_s": 600}, 60.0, id="a-list-and-a-wait-past-the-cap"),
        pytest.param("PASS", {"wait_timeout_s": 2.5}, 2.5, id="a-string-and-its-own-wait"),
    ],
)
def test_com_read_report_records_until_as_a_list_and_the_wait_in_force(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, until: object, arguments: dict, until_wait_s: float
) -> None:
    """The report records `until` as a list however the caller gave it, and the
    wait that was in force once the default and the cap applied. The answer
    echoes neither: the caller knows what it asked, and every field costs it."""
    with com_session(tmp_path, monkeypatch) as (service, handle):
        handle.deliver(b"PASS\r\n")
        result = com_read(service, until=until, **arguments)
        report = read_last_report(service.config)

    assert result["ok"] is True, result
    assert result["until_matched"] is True, result
    assert report["until"] == ["PASS"], report
    assert report["until_wait_s"] == until_wait_s, report
    assert "until" not in result, result
    assert "until_wait_s" not in result, result


def test_com_read_without_until_keeps_todays_wait_and_shape(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Without `until` nothing changes: the first bytes end the wait, and the
    answer carries exactly the keys it carries today."""
    with com_session(tmp_path, monkeypatch) as (service, handle):
        started = time.monotonic()
        with after(0.2, handle.feed, b"boot ok\r\n"):
            result = com_read(service, wait_timeout_s=30)
        elapsed = time.monotonic() - started

    assert result["ok"] is True, result
    assert set(result) == TODAY_COM_READ_KEYS, result
    assert result["summary"] == "Feedback read from COM port.", result
    assert result["bytes_read"] >= 1, result
    assert "boot ok\r\n".startswith(result["data"]["text"]), result
    assert elapsed < scaled_time_bound(5.0), "the first bytes arrived 0.2 s in and should have ended the wait"


REFUSED_UNTIL = [
    pytest.param("", id="empty-string"),
    pytest.param([], id="empty-list"),
    pytest.param([""], id="list-with-an-empty-entry"),
    pytest.param(["PASS", ""], id="empty-entry-after-a-good-one"),
    pytest.param([f"entry{index}" for index in range(9)], id="nine-entries"),
    pytest.param("x" * 257, id="string-of-257-characters"),
    pytest.param(["x" * 257], id="entry-of-257-characters"),
    pytest.param(5, id="number"),
    pytest.param(True, id="boolean"),
    pytest.param(None, id="null"),
    pytest.param({"text": "PASS"}, id="object"),
    pytest.param([5], id="list-with-a-number"),
    pytest.param([["PASS"]], id="nested-list"),
]


@pytest.mark.parametrize("until", REFUSED_UNTIL)
def test_com_read_refuses_every_other_until_shape_before_touching_the_port(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, until: object) -> None:
    """`until` is a non-empty string, or 1 to 8 of them, each at most 256
    characters. Anything else is refused before the port is read: the
    buffered line is still there for the next call."""
    with com_session(tmp_path, monkeypatch) as (service, handle):
        handle.deliver(b"PASS\r\n")
        refusal = com_read(service, until=until, wait_timeout_s=5)
        kept = com_read(service)

    assert_refused_for(refusal, "until")
    assert kept["data"]["text"] == "PASS\r\n", kept


ACCEPTED_UNTIL = [
    pytest.param("x" * 256, id="string-of-256-characters"),
    pytest.param(["x" * 256], id="entry-of-256-characters"),
    pytest.param([f"entry{index}" for index in range(8)], id="eight-entries"),
    pytest.param(["PASS"], id="list-of-one"),
]


@pytest.mark.parametrize("until", ACCEPTED_UNTIL)
def test_com_read_accepts_until_at_its_limits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, until: object) -> None:
    """The other side of each limit is taken."""
    with com_session(tmp_path, monkeypatch) as (service, handle):
        handle.deliver(b"boot\r\n")
        result = com_read(service, until=until, wait_timeout_s=0)

    assert result["ok"] is True, result
    assert result["until_matched"] is False, result
    assert result["data"]["text"] == "boot\r\n", result


@pytest.mark.parametrize(
    "until",
    [pytest.param("PASS \u2713", id="the-only-entry"), pytest.param(["PASS", "PASS \u2713"], id="the-second-entry")],
)
def test_com_read_refuses_an_entry_the_ports_encoding_cannot_encode_and_names_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, until: object) -> None:
    """An ascii port cannot carry a check mark, so the entry could never match.
    It is refused by name before the port is read, even when another entry
    would have matched what is buffered."""
    with com_session(tmp_path, monkeypatch, ASCII_PORT_ID) as (service, handle):
        handle.deliver(b"PASS\r\n")
        refusal = com_read(service, ASCII_PORT_ID, until=until, wait_timeout_s=5)
        kept = com_read(service, ASCII_PORT_ID)

    assert refusal["ok"] is False, refusal
    assert refusal["error_type"] == "invalid_argument", refusal
    assert refusal.get("validator") != "additionalProperties", refusal
    assert "PASS \u2713" in json.dumps(refusal, ensure_ascii=False), refusal
    assert kept["data"]["text"] == "PASS\r\n", kept


# ---------------------------------------------------------------------------
# can_read with `until_id`.


@pytest.mark.parametrize("kind", ADAPTER_KINDS)
def test_can_read_until_id_returns_the_frames_before_the_match_and_the_match_last(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str) -> None:
    """The call reads on across a quiet read until the frame it waits for, and
    hands out everything it took off the bus on the way, in arrival order."""
    with can_session(tmp_path, monkeypatch, kind, [[A], [], [B, MATCH]]) as rig:
        result = can_read(rig, until_id=MATCH_ID, wait_timeout_s=5)

    assert result["ok"] is True, result
    assert result["until_matched"] is True, result
    assert result["matched_id"] == MATCH_ID, result
    assert frame_ids(result) == [A["id"], B["id"], MATCH_ID], result
    assert result["frames_read"] == 3, result
    assert "until_id" not in result, result


@pytest.mark.parametrize("kind", ADAPTER_KINDS)
def test_can_read_until_id_stops_at_max_frames_without_a_match(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str) -> None:
    """`max_frames` still caps the answer; reaching it first is an answer
    without a match, not a failure."""
    with can_session(tmp_path, monkeypatch, kind, [[A], [B], [C, MATCH]]) as rig:
        result = can_read(rig, until_id=MATCH_ID, max_frames=2, wait_timeout_s=5)

    assert result["ok"] is True, result
    assert result["until_matched"] is False, result
    assert "matched_id" not in result, result
    assert frame_ids(result) == [A["id"], B["id"]], result


@pytest.mark.parametrize("kind", ADAPTER_KINDS)
def test_can_read_until_id_ignores_the_extended_flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str) -> None:
    """Only the numeric id is compared: an extended frame with id 0x123 answers
    `until_id` 0x123 as a standard one would, here given as one of a list."""
    with can_session(tmp_path, monkeypatch, kind, [[A, frame(0x123, "5a", extended=True)]]) as rig:
        result = can_read(rig, until_id=[UNSEEN_ID, 0x123], wait_timeout_s=5)

    assert result["ok"] is True, result
    assert result["until_matched"] is True, result
    assert result["matched_id"] == 0x123, result
    assert frame_ids(result) == [A["id"], 0x123], result
    assert result["frames"][-1]["extended"] is True, result


@pytest.mark.parametrize("kind", ADAPTER_KINDS)
def test_can_read_until_id_without_a_match_by_the_deadline_is_ok(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str) -> None:
    """A frame that did not come is feedback about the target, not a failed
    read: the frames that did come are handed out, and `ok` stays true."""
    with can_session(tmp_path, monkeypatch, kind, [[A]]) as rig:
        started = time.monotonic()
        result = can_read(rig, until_id=UNSEEN_ID, wait_timeout_s=0.3)
        elapsed = time.monotonic() - started

    assert result["ok"] is True, result
    assert result["until_matched"] is False, result
    assert "matched_id" not in result, result
    assert frame_ids(result) == [A["id"]], result
    assert elapsed >= 0.3 - 0.05, "a frame came at once, and the call still waited out its deadline for the id"


@pytest.mark.parametrize("kind", ADAPTER_KINDS)
def test_can_read_until_id_keeps_the_frames_of_earlier_reads_when_a_later_read_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str) -> None:
    """The failure is reported as today, with the adapter's own error line, and
    the frames an earlier read of the same call took off the bus stay in the
    answer: they are feedback the bench really produced."""
    decisive = {"python-can": "staged receive failure", "bridge": "Fake CAN bridge controller error."}[kind]
    with can_session(tmp_path, monkeypatch, kind, [[A], FAIL]) as rig:
        result = can_read(rig, until_id=UNSEEN_ID, wait_timeout_s=5)

    assert result["ok"] is False, result
    assert result["error_type"] == "can_read_failed", result
    assert decisive in json.dumps(result), result
    assert frame_ids(result) == [A["id"]], result
    assert result["until_matched"] is False, result
    if kind == "python-can":
        assert result["side_effect_committed"] is False, result


@pytest.mark.parametrize("kind", ADAPTER_KINDS)
def test_can_read_until_id_without_wait_timeout_reads_on_past_quiet_reads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str) -> None:
    """`until_id` alone waits, where a read without it answers at once: two
    quiet reads do not end the call."""
    with can_session(tmp_path, monkeypatch, kind, [[], [], [MATCH]]) as rig:
        result = can_read(rig, until_id=MATCH_ID)

    assert result["ok"] is True, result
    assert result["until_matched"] is True, result
    assert frame_ids(result) == [MATCH_ID], result


@pytest.mark.parametrize("kind", ADAPTER_KINDS)
def test_can_read_until_id_without_a_wait_still_reads_the_frames_already_queued(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str) -> None:
    """A deadline ends the waiting, not the reading of what is already there:
    with no wait, the call still goes through the queued frames to the match,
    as a read without `until_id` drains the queue. The fake bridge answers each
    read with one script entry, so there each queued frame is an entry of its own."""
    queued = {"python-can": [[A, B, MATCH, C]], "bridge": [[A], [B], [MATCH], [C]]}[kind]
    with can_session(tmp_path, monkeypatch, kind, queued) as rig:
        result = can_read(rig, until_id=MATCH_ID, wait_timeout_s=0)

    assert result["ok"] is True, result
    assert result["until_matched"] is True, result
    assert frame_ids(result) == [A["id"], B["id"], MATCH_ID], result


@pytest.mark.parametrize("kind", ADAPTER_KINDS)
def test_can_read_without_until_id_keeps_todays_single_read_and_shape(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str) -> None:
    """Without `until_id` nothing changes: one adapter read answers the call,
    with exactly the keys it carries today, and the next read's frames wait
    for the next call."""
    with can_session(tmp_path, monkeypatch, kind, [[A], [B]]) as rig:
        first = can_read(rig, wait_timeout_s=5)
        second = can_read(rig, wait_timeout_s=5)

    assert first["ok"] is True, first
    assert set(first) == TODAY_CAN_READ_KEYS, first
    assert first["summary"] == "CAN frame(s) read.", first
    assert frame_ids(first) == [A["id"]], first
    assert frame_ids(second) == [B["id"]], second


def test_can_read_until_id_without_wait_timeout_waits_for_a_late_frame(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The default wait (ten seconds) covers a frame that comes half a second
    in, without this test sitting through the default."""
    with can_session(tmp_path, monkeypatch, "python-can", []) as rig:
        started = time.monotonic()
        with after(0.5, rig.bus.inbox.put, message(MATCH)):
            result = can_read(rig, until_id=MATCH_ID)
        elapsed = time.monotonic() - started

    assert result["ok"] is True, result
    assert result["until_matched"] is True, result
    assert frame_ids(result) == [MATCH_ID], result
    assert elapsed < scaled_time_bound(5.0), "the frame arrived half a second in and should have ended the wait"


def test_can_read_until_id_waits_as_reads_each_within_the_bus_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The wait is repeated adapter reads, each held to the per-read cap the
    call applies today (the bus's `timeout_s` here), so neither adapter nor the
    bridge protocol is asked for a longer read than before."""
    with can_session(tmp_path, monkeypatch, "python-can", [], timeout_s=0.2) as rig:
        with after(0.7, rig.bus.inbox.put, message(MATCH)):
            result = can_read(rig, until_id=MATCH_ID, wait_timeout_s=5)
        asked = [timeout for timeout in rig.bus.asked_timeouts if timeout]

    assert result["ok"] is True, result
    assert result["until_matched"] is True, result
    assert frame_ids(result) == [MATCH_ID], result
    assert len(asked) >= 2, rig.bus.asked_timeouts
    assert max(asked) <= 0.2, rig.bus.asked_timeouts


@pytest.mark.parametrize(("poll_interval_ms", "most_reads"), [pytest.param(None, 60, id="default-poll-interval"), pytest.param(50, 12, id="configured-poll-interval")])
def test_can_read_until_id_pauses_after_a_read_that_answers_nothing_early(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, poll_interval_ms: int | None, most_reads: int
) -> None:
    """Every adapter read here answers nothing at once, well before its wait is
    over. Read back to back, that is a busy loop of thousands of reads; the
    call pauses for the bus's poll interval (10 ms unless configured) before
    the next read, so a 0.3 s wait stays a few dozen reads."""
    with can_session(tmp_path, monkeypatch, "python-can", [[]] * 5000, poll_interval_ms=poll_interval_ms) as rig:
        result = can_read(rig, until_id=UNSEEN_ID, wait_timeout_s=0.3)
        reads = len(rig.bus.asked_timeouts)

    assert result["ok"] is True, result
    assert result["until_matched"] is False, result
    assert 2 <= reads <= most_reads, reads


def test_can_read_until_id_leaves_the_frames_after_the_match_for_the_next_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The matching frame is the last one handed out, and the frames already on
    the bus behind it are not lost: the next read hands them out."""
    with can_session(tmp_path, monkeypatch, "python-can", [[A, MATCH, C, D]]) as rig:
        matched = can_read(rig, until_id=MATCH_ID, wait_timeout_s=5)
        rest = can_read(rig, wait_timeout_s=5)

    assert matched["ok"] is True, matched
    assert matched["until_matched"] is True, matched
    assert frame_ids(matched) == [A["id"], MATCH_ID], matched
    assert frame_ids(rest) == [C["id"], D["id"]], rest


REFUSED_UNTIL_ID = [
    pytest.param(-1, id="negative"),
    pytest.param(0x20000000, id="past-29-bits"),
    pytest.param([], id="empty-list"),
    pytest.param(list(range(0x100, 0x109)), id="nine-ids"),
    pytest.param("0x123", id="string"),
    pytest.param(1.5, id="fraction"),
    pytest.param(True, id="boolean"),
    pytest.param(None, id="null"),
    pytest.param({"id": 0x123}, id="object"),
    pytest.param([0x100, -1], id="negative-in-a-list"),
    pytest.param([0x100, 0x20000000], id="past-29-bits-in-a-list"),
    pytest.param([0x100, "0x200"], id="string-in-a-list"),
    pytest.param([[0x100]], id="nested-list"),
]


@pytest.mark.parametrize("until_id", REFUSED_UNTIL_ID)
def test_can_read_refuses_every_other_until_id_shape_before_touching_the_bus(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, until_id: object) -> None:
    """`until_id` is an arbitration id from 0 to 0x1FFFFFFF, or 1 to 8 of them.
    Anything else is refused before the bus is read: the adapter was never
    asked, and the waiting frame is still there for the next call."""
    with can_session(tmp_path, monkeypatch, "python-can", [[A]]) as rig:
        refusal = can_read(rig, until_id=until_id, wait_timeout_s=5)
        asked_before_the_next_call = list(rig.bus.asked_timeouts)
        kept = can_read(rig, wait_timeout_s=5)

    assert_refused_for(refusal, "until_id")
    assert asked_before_the_next_call == [], asked_before_the_next_call
    assert frame_ids(kept) == [A["id"]], kept


ACCEPTED_UNTIL_ID = [
    pytest.param(0, id="zero"),
    pytest.param(0x1FFFFFFF, id="largest-extended-id"),
    pytest.param(list(range(0x100, 0x108)), id="eight-ids"),
    pytest.param([UNSEEN_ID], id="list-of-one"),
]


@pytest.mark.parametrize("until_id", ACCEPTED_UNTIL_ID)
def test_can_read_accepts_until_id_at_its_limits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, until_id: object) -> None:
    """The other side of each limit is taken."""
    with can_session(tmp_path, monkeypatch, "python-can", [[frame(0x050, "01")]]) as rig:
        result = can_read(rig, until_id=until_id, wait_timeout_s=0)

    assert result["ok"] is True, result
    assert result["until_matched"] is False, result


# ---------------------------------------------------------------------------
# The MCP surface and the descriptions.


def test_tools_call_takes_until_and_until_id_and_refuses_a_bad_value(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Through `tools/call`, the path an agent actually takes: the published
    input schemas accept both arguments, and a value outside them is refused as
    a bad value of that argument rather than as an argument nobody knows."""
    install_serial(monkeypatch)
    monkeypatch.setitem(sys.modules, "can", fake_can_module(lambda **kwargs: ScriptedBus()))
    config = load_config(str(write_config(tmp_path, com_ports_yaml=COM_PORTS_YAML, can_buses_yaml=can_buses_yaml("python-can", 5.0))))
    service = AgenticHILToolService(config, frontend="mcp")
    try:
        assert service.call("com_session_start", {"port_id": PORT_ID})["ok"] is True
        assert service.call("can_session_start", {"bus_id": BUS_ID, "clear_rx_queue": False})["ok"] is True
        accepted_com = handle_mcp_message(tools_call(1, "com_read", {"port_id": PORT_ID, "until": "PASS", "wait_timeout_s": 0}), service)["result"]
        refused_com = handle_mcp_message(tools_call(2, "com_read", {"port_id": PORT_ID, "until": ""}), service)["result"]
        accepted_can = handle_mcp_message(tools_call(3, "can_read", {"bus_id": BUS_ID, "until_id": 0x123, "wait_timeout_s": 0}), service)["result"]
        refused_can = handle_mcp_message(tools_call(4, "can_read", {"bus_id": BUS_ID, "until_id": -1}), service)["result"]
    finally:
        close(service)

    assert accepted_com["isError"] is False, accepted_com
    assert accepted_com["structuredContent"]["until_matched"] is False, accepted_com
    assert refused_com["isError"] is True, refused_com
    assert_refused_for(refused_com["structuredContent"], "until")
    assert accepted_can["isError"] is False, accepted_can
    assert accepted_can["structuredContent"]["until_matched"] is False, accepted_can
    assert refused_can["isError"] is True, refused_can
    assert_refused_for(refused_can["structuredContent"], "until_id")


SENTENCE_BREAK = re.compile(r"(?<=[.!?])\s+")


@pytest.mark.parametrize(("tool", "argument"), [("com_read", "until"), ("can_read", "until_id")])
def test_the_read_descriptions_gain_one_sentence_and_the_arguments_describe_themselves(tool: str, argument: str) -> None:
    """Each description keeps what it says today and gains one sentence saying
    the call can wait instead of being polled. The new argument describes
    itself in under 200 characters, and `until_id` says the extended flag is
    not compared."""
    entry = next(item for item in MCP_TOOLS if item["name"] == tool)
    today = SENTENCE_BREAK.split(TODAY_DESCRIPTIONS[tool])
    sentences = SENTENCE_BREAK.split(entry["description"].strip())
    added = [sentence for sentence in sentences if sentence not in today]

    assert all(sentence in sentences for sentence in today), entry["description"]
    assert len(added) == 1, added
    assert re.search(r"wait|until", added[0], re.IGNORECASE), added
    described = entry["inputSchema"]["properties"].get(argument, {}).get("description")
    assert isinstance(described, str) and described, entry["inputSchema"]
    assert len(described) < 200, described
    if argument == "until_id":
        assert "extended" in described.lower(), described


# ---------------------------------------------------------------------------
# The pieces another caller reuses: the wait, the `until` checks and the matcher.
# Each test imports them itself, so the rest of this module runs without them.


@pytest.mark.parametrize(
    ("wait_timeout_s", "expected"),
    [
        pytest.param(None, 10.0, id="none-given"),
        pytest.param(0, 0.0, id="zero"),
        pytest.param(0.3, 0.3, id="under-the-cap"),
        pytest.param(60, 60.0, id="at-the-cap"),
        pytest.param(61, 60.0, id="just-past-the-cap"),
        pytest.param(3600, 60.0, id="far-past-the-cap"),
    ],
)
def test_until_wait_is_ten_seconds_unless_given_and_sixty_at_most(wait_timeout_s: float | None, expected: float) -> None:
    """Every wait for `until` or `until_id` takes its length from one helper:
    ten seconds when the caller gave none, and never more than sixty."""
    from agentic_hil.readuntil import until_wait_s

    assert until_wait_s(wait_timeout_s) == expected


def test_until_patterns_encodes_each_entry_with_the_encoding_it_is_given() -> None:
    """The entries come back as a list whatever shape they were given in, beside
    the bytes each one is looked for as."""
    from agentic_hil.readuntil import until_patterns

    e_acute = chr(0xE9)
    assert until_patterns("PASS", "utf-8") == {"ok": True, "entries": ["PASS"], "patterns": [b"PASS"]}
    assert until_patterns(["PASS", e_acute], "latin-1") == {"ok": True, "entries": ["PASS", e_acute], "patterns": [b"PASS", b"\xe9"]}


@pytest.mark.parametrize("until", REFUSED_UNTIL)
def test_until_patterns_refuses_every_other_shape_under_the_callers_own_names(until: object) -> None:
    """Another caller gets the same checks, refused under its own tool and
    field: here `flash_firmware` and its `capture.until`."""
    from agentic_hil.readuntil import until_patterns

    refusal = until_patterns(until, "utf-8", tool="flash_firmware", field="capture.until")

    assert refusal["ok"] is False, refusal
    assert refusal["tool"] == "flash_firmware", refusal
    assert refusal["error_type"] == "invalid_argument", refusal
    assert str(refusal["field"]).startswith("capture.until"), refusal


def test_until_patterns_names_the_entry_its_encoding_cannot_carry() -> None:
    from agentic_hil.readuntil import until_patterns

    entry = "PASS " + chr(0x2713)
    refusal = until_patterns(["PASS", entry], "ascii")

    assert refusal["ok"] is False, refusal
    assert refusal["error_type"] == "invalid_argument", refusal
    assert refusal["field"] == "until[1]", refusal
    assert entry in refusal["summary"], refusal


@pytest.mark.parametrize(
    ("buffer", "patterns", "expected"),
    [
        pytest.param(b"xABCDy", [b"ABCD", b"BC"], (4, 1), id="the-match-that-ends-first"),
        pytest.param(b"xABCy", [b"BC", b"ABC"], (4, 0), id="a-tie-goes-to-the-first-listed-shorter-entry"),
        pytest.param(b"xABCy", [b"ABC", b"BC"], (4, 0), id="a-tie-goes-to-the-first-listed-longer-entry"),
        pytest.param(b"boot PA", [b"PASS"], None, id="the-start-of-a-match-is-no-match"),
        pytest.param(b"PASS PASS", [b"PASS"], (4, 0), id="the-first-occurrence"),
    ],
)
def test_find_until_answers_where_the_first_match_ends_and_whose_it_is(buffer: bytes, patterns: list[bytes], expected: tuple[int, int] | None) -> None:
    """The matcher takes any bytes buffer and answers the end offset of the
    match that ends first with the index of its entry, or None."""
    from agentic_hil.readuntil import find_until

    assert find_until(buffer, patterns) == expected
    assert find_until(bytearray(buffer), patterns) == expected
