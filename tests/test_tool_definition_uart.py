"""What `com_session_start`, `com_session_stop` and `com_write` tell an agent.

The three definitions said what the tools are and little else: nothing about
which port a `port_id` names, what a repeated start does, what stop leaves
behind, whether a write adds a line ending, how large it may be, or which tool
reads the reply. These tests ask the definitions a host receives through
`tools/list` to say those things, and every claim they ask for is first shown
to be what the code does, through `tools/call`, against a recording stand-in
for pyserial. No port, adapter or board is touched.

The metadata tests check meaning, not wording. A claim is checked as a
relation inside one sentence or clause (the failure and the condition that
produces it, the limit and its default, the timeout and what it waits for), and
the inverted claim an agent could act on wrongly is rejected outright. Every
identifier a definition names is one the server lists, configures or can
answer with, and the start, write, read, stop example one of them carries is
run as written.
"""

from __future__ import annotations

import ast
import errno
import json
import re
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import write_config
from support import scaled_time_bound
from test_read_until import DIE, ScriptedSerialHandle, close, tools_call

import agentic_hil
from agentic_hil.config import load_config
from agentic_hil.mcp import handle_mcp_message
from agentic_hil.tools import AgenticHILToolService

UART_TOOLS = ("com_session_start", "com_session_stop", "com_write")
DESCRIPTION_LIMIT = 400
PROPERTY_DESCRIPTION_LIMIT = 200

# Port ids and devices of this module alone: device locks are machine-wide.
PORT_ID = "tooldef_uart"
SPARE_PORT_ID = "tooldef_spare"
LISTEN_PORT_ID = "tooldef_listen"
LOCKED_PORT_ID = "tooldef_locked"
QUIET_PORT_ID = "tooldef_quiet"
LATIN1_PORT_ID = "tooldef_latin1"
ASCII_PORT_ID = "tooldef_ascii"
ABSENT_PORT_ID = "tooldef_absent"
SLOW_PORT_ID = "tooldef_slow"
SMALL_PORT_ID = "tooldef_small"
UNKNOWN_PORT_ID = "tooldef_nowhere"
DEVICES = {
    PORT_ID: "/dev/ttyTOOLDEF0",
    SPARE_PORT_ID: "/dev/ttyTOOLDEF1",
    LISTEN_PORT_ID: "/dev/ttyTOOLDEF2",
    LOCKED_PORT_ID: "/dev/ttyTOOLDEF3",
    QUIET_PORT_ID: "/dev/ttyTOOLDEF4",
    LATIN1_PORT_ID: "/dev/ttyTOOLDEF5",
    ASCII_PORT_ID: "/dev/ttyTOOLDEF6",
    ABSENT_PORT_ID: "/dev/ttyTOOLDEF7",
    SLOW_PORT_ID: "/dev/ttyTOOLDEF8",
    SMALL_PORT_ID: "/dev/ttyTOOLDEF9",
}
# The device the example a definition carries is run on, under the port name
# the example itself gives.
EXAMPLE_DEVICE = "/dev/ttyTOOLDEF10"
SMALL_MAX_WRITE_BYTES = 8
# The first two entries keep every default the code applies, which are what the
# definitions are asked to name; each other entry changes one setting.
COM_PORTS_YAML = (
    "com_ports:\n"
    f"  {PORT_ID}:\n"
    f'    device: "{DEVICES[PORT_ID]}"\n'
    f"  {SPARE_PORT_ID}:\n"
    f'    device: "{DEVICES[SPARE_PORT_ID]}"\n'
    f"  {LISTEN_PORT_ID}:\n"
    f'    device: "{DEVICES[LISTEN_PORT_ID]}"\n'
    "    permissions: {allow_read: true, allow_write: false}\n"
    f"  {LOCKED_PORT_ID}:\n"
    f'    device: "{DEVICES[LOCKED_PORT_ID]}"\n'
    "    permissions: {allow_read: false, allow_write: false}\n"
    f"  {QUIET_PORT_ID}:\n"
    f'    device: "{DEVICES[QUIET_PORT_ID]}"\n'
    "    assert_dtr: false\n"
    "    assert_rts: false\n"
    f"  {LATIN1_PORT_ID}:\n"
    f'    device: "{DEVICES[LATIN1_PORT_ID]}"\n'
    '    encoding: "latin-1"\n'
    f"  {ASCII_PORT_ID}:\n"
    f'    device: "{DEVICES[ASCII_PORT_ID]}"\n'
    '    encoding: "ascii"\n'
    f"  {ABSENT_PORT_ID}:\n"
    f'    device: "{DEVICES[ABSENT_PORT_ID]}"\n'
    f"  {SLOW_PORT_ID}:\n"
    f'    device: "{DEVICES[SLOW_PORT_ID]}"\n'
    "    timeout_s: 1.5\n"
    f"  {SMALL_PORT_ID}:\n"
    f'    device: "{DEVICES[SMALL_PORT_ID]}"\n'
    f"    max_write_bytes: {SMALL_MAX_WRITE_BYTES}\n"
)
# Version 2 of the configuration: reading needs no grant, so the only
# permission a port carries is allow_write, here withheld.
V2_COM_PORTS_YAML = f'com_ports:\n  {LISTEN_PORT_ID}:\n    device: "{DEVICES[LISTEN_PORT_ID]}"\n    permissions: {{allow_write: false}}\n'
# The config defaults of a com_ports entry (src/agentic_hil/config.py).
DEFAULT_MAX_WRITE_BYTES = 4096
DEFAULT_TIMEOUT_S = 0.1


# ---------------------------------------------------------------------------
# The stand-in and the calls.


class SerialLine:
    """Every handle the service opened, and what the devices are scripted to do."""

    def __init__(self) -> None:
        self.handles: list[RecordingSerialHandle] = []
        self.absent: set[str] = set()
        self.held: set[str] = set()
        self.refuse_close_once: set[str] = set()
        self.accepts_per_write: dict[str, int] = {}

    def handle(self, port_id: str) -> RecordingSerialHandle:
        """The handle most recently opened on `port_id`'s device."""
        return self.on_device(DEVICES[port_id])

    def on_device(self, device: str) -> RecordingSerialHandle:
        opened = [handle for handle in self.handles if handle.port == device and handle.opened]
        assert opened, f"no handle was opened on {device}"
        return opened[-1]

    def opened_on(self, port_id: str) -> int:
        return sum(1 for handle in self.handles if handle.port == DEVICES[port_id] and handle.opened)


class RecordingSerialHandle(ScriptedSerialHandle):
    """The scripted handle, recording the bytes written to it and its input resets.

    It also does what a test asks of one device: refuse the open because the
    device is absent, or because another program holds it (`EBUSY`, the number
    a POSIX driver answers with); accept only part of each write, the way
    pyserial reports a write the line did not take whole; refuse the first
    close; and stall the reader inside a read until the test lets it go, the
    way a driver that ignores `cancel_read` does.
    """

    def __init__(self, line: SerialLine) -> None:
        super().__init__()
        self.line = line
        self.opened = False
        self.written = bytearray()
        self.input_resets = 0
        self.stall = False
        self.stalled = threading.Event()
        self.unstall = threading.Event()
        line.handles.append(self)

    def open(self) -> None:
        if self.port in self.line.absent:
            raise OSError(f"could not open port {self.port}: no such device")
        if self.port in self.line.held:
            raise OSError(errno.EBUSY, f"could not open port {self.port}: device or resource busy")
        super().open()
        self.opened = True

    def read(self, size: int) -> bytes:
        if self.stall:
            self.stalled.set()
            self.unstall.wait(scaled_time_bound(30.0))
            return b""
        return super().read(size)

    def write(self, data: bytes) -> int:
        accepted = bytes(data)[: self.line.accepts_per_write.get(self.port, len(data))]
        self.written.extend(accepted)
        return len(accepted)

    def reset_input_buffer(self) -> None:
        self.input_resets += 1
        super().reset_input_buffer()

    def close(self) -> None:
        if self.port in self.line.refuse_close_once:
            self.line.refuse_close_once.discard(self.port)
            raise OSError("close refused by the driver")
        super().close()


def install_line(monkeypatch: pytest.MonkeyPatch) -> SerialLine:
    line = SerialLine()
    monkeypatch.setitem(sys.modules, "serial", SimpleNamespace(Serial=lambda *args, **kwargs: RecordingSerialHandle(line)))
    return line


def new_service(workspace: Path, com_ports_yaml: str = COM_PORTS_YAML, **kwargs: object) -> AgenticHILToolService:
    return AgenticHILToolService(load_config(str(write_config(workspace, com_ports_yaml=com_ports_yaml, **kwargs))), frontend="mcp")


@pytest.fixture
def bench(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[SimpleNamespace]:
    """A server on this module's ports, and the line its sessions open."""
    line = install_line(monkeypatch)
    service = new_service(tmp_path / "workspace")
    try:
        yield SimpleNamespace(service=service, line=line)
    finally:
        for handle in line.handles:
            handle.unstall.set()
        close(service)


def call(service: AgenticHILToolService, name: str, arguments: dict) -> dict:
    """One `tools/call`, answered with the structured result an agent acts on."""
    response = handle_mcp_message(tools_call(1, name, arguments), service)
    assert isinstance(response, dict) and "result" in response, response
    answer = response["result"]
    structured = answer["structuredContent"]
    assert answer["isError"] is (structured.get("ok") is not True), answer
    return structured


def start(service: AgenticHILToolService, port_id: str = PORT_ID, **arguments: object) -> dict:
    return call(service, "com_session_start", {"port_id": port_id, **arguments})


def stop(service: AgenticHILToolService, port_id: str = PORT_ID) -> dict:
    return call(service, "com_session_stop", {"port_id": port_id})


def write(service: AgenticHILToolService, port_id: str = PORT_ID, **payload: object) -> dict:
    return call(service, "com_write", {"port_id": port_id, **payload})


def read(service: AgenticHILToolService, port_id: str = PORT_ID, **arguments: object) -> dict:
    return call(service, "com_read", {"port_id": port_id, **arguments})


def started(service: AgenticHILToolService, port_id: str = PORT_ID, **arguments: object) -> dict:
    result = start(service, port_id, **arguments)
    assert result["ok"] is True, result
    return result


def logged_rx(service: AgenticHILToolService, log_path: str) -> bytes:
    """Every received byte the session log at `log_path` records, in order."""
    path = Path(log_path)
    if not path.is_absolute():
        path = Path(service.config.work_dir) / path
    entries = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return b"".join(bytes.fromhex(entry["hex"]) for entry in entries if entry.get("direction") == "rx")


# ---------------------------------------------------------------------------
# What the definitions say, read the way a host reads them.


def listed_tools(workspace: Path) -> dict[str, dict]:
    service = new_service(workspace)
    try:
        response = handle_mcp_message({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, service)
    finally:
        close(service)
    assert isinstance(response, dict), response
    return {str(tool["name"]): tool for tool in response["result"]["tools"]}


@pytest.fixture
def listed(tmp_path: Path) -> dict[str, dict]:
    return listed_tools(tmp_path / "listed")


def property_text(tool: dict, name: str) -> str:
    return str(tool["inputSchema"]["properties"].get(name, {}).get("description") or "")


def definition_text(tool: dict) -> str:
    """The description and every property description: what an agent reads."""
    properties = tool["inputSchema"]["properties"]
    return " ".join([str(tool.get("description") or ""), *(property_text(tool, name) for name in properties)])


def names(text: str, *words: str) -> list[str]:
    """The words of `words` that `text` does not name as whole identifiers."""
    return [word for word in words if not re.search(rf"(?<![A-Za-z0-9_]){re.escape(word)}(?![A-Za-z0-9_])", text)]


def sentences(text: str) -> list[str]:
    """The sentences of `text`. A decimal point, as in `0.5 s`, ends none."""
    return [part for part in re.split(r"(?<=[.!?])\s+", text) if part.strip()]


def clauses(text: str) -> list[str]:
    """The sentences of `text`, each split once more at its semicolons."""
    return [part for sentence in sentences(text) for part in re.split(r";\s*", sentence) if part.strip()]


def one_of(units: list[str], *patterns: str) -> bool:
    """Whether a single sentence or clause of `units` matches every pattern."""
    return any(all(re.search(pattern, unit, re.IGNORECASE) for pattern in patterns) for unit in units)


def claims(text: str, pattern: str) -> bool:
    return re.search(pattern, text, re.IGNORECASE) is not None


# A negation and a line ending in one clause, in either order: "adds no line
# ending", "without a newline", "a line ending is not added", or nothing at all
# appended.
NO_LINE_ENDING = re.compile(
    r"\b(?:no|not|never|without)\b[^.;]*\b(?:line ending|line endings|newline|terminator|CR|LF)\b"
    r"|\b(?:line ending|newline|terminator)\b[^.;]*\b(?:not|never)\b"
    r"|\bnothing\b[^.;]*\b(?:appended|added)\b",
    re.IGNORECASE,
)
NEGATION = r"\b(?:no|not|never|nothing|without)\b"
ADDS_LINE_ENDING = r"\b(?:adds?|appends?|added|appended)\b[^.;]*\b(?:line ending|newline|terminator)\b"
DEFAULT_TRUE = re.compile(r"\bdefaults?\b[^.;]*\btrue\b|\btrue\b[^.;]*\bdefault\b", re.IGNORECASE)
SECONDS = re.compile(r"\b(\d+(?:\.\d+)?)\s?s\b")
# max(1 s, timeout_s + 0.5 s), with or without the units: the join bound in
# comports.ComPortService._stop_session.
READER_JOIN_BOUND = r"\bmax\(\s*1(?:\.0)?\s*s?\s*,\s*timeout_s\s*\+\s*0?\.5\s*s?\s*\)"
SNAKE_CASE = re.compile(r"(?<![A-Za-z0-9_])[a-z][a-z0-9]*(?:_[a-z0-9]+)+(?![A-Za-z0-9_])")
# A quoted literal as an agent would pass it in JSON: example data, not a name.
QUOTED = r'"((?:[^"\\]|\\.)*)"'


def test_the_three_tools_are_listed_with_a_description_within_the_budget(listed: dict[str, dict]) -> None:
    for name in UART_TOOLS:
        assert name in listed, sorted(listed)
        description = listed[name].get("description")
        assert isinstance(description, str) and description.strip(), name
        assert len(description) <= DESCRIPTION_LIMIT, (name, len(description))


@pytest.mark.parametrize("name", UART_TOOLS)
def test_every_input_property_describes_itself(listed: dict[str, dict], name: str) -> None:
    properties = listed[name]["inputSchema"]["properties"]
    undescribed = sorted(key for key in properties if not property_text(listed[name], key).strip())
    assert not undescribed, f"{name}: input properties without a description: {undescribed}"
    oversized = sorted(key for key in properties if len(property_text(listed[name], key)) > PROPERTY_DESCRIPTION_LIMIT)
    assert not oversized, f"{name}: property descriptions over {PROPERTY_DESCRIPTION_LIMIT} characters: {oversized}"


@pytest.mark.parametrize("name", UART_TOOLS)
def test_port_id_is_described_as_a_configured_entry_and_not_a_device(listed: dict[str, dict], name: str) -> None:
    """The port is chosen by the name of its `com_ports` entry, which
    `com_ports_list` lists; a device path is not a `port_id`, and a name the
    configuration does not carry is refused as `com_port_not_configured`."""
    described = property_text(listed[name], "port_id")
    assert not names(described, "com_ports", "com_ports_list"), described
    assert claims(described, r"\bnot\b[^.;]*\bdevice\b"), described
    assert not names(definition_text(listed[name]), "com_port_not_configured"), definition_text(listed[name])


def test_clear_buffer_names_its_default_what_it_discards_and_that_it_applies_to_an_active_session(listed: dict[str, dict]) -> None:
    described = property_text(listed["com_session_start"], "clear_buffer")
    parts = clauses(described)
    assert DEFAULT_TRUE.search(described), described
    assert one_of(parts, r"discard|drop|clear|purge|empt", r"receiv|unread"), described
    assert claims(described, r"already[ _]active|active session|repeat"), described
    assert one_of(parts, r"\bfalse\b", r"\bkeeps?\b|\bkept\b|\bpreserv|\bretain"), described
    # Not the inverted switch: false never discards.
    assert not claims(described, r"\bfalse\b[^.;]*\b(?:discard|drop|clear|purge|empt)"), described


def test_text_names_its_encoding_and_that_no_line_ending_is_added(listed: dict[str, dict]) -> None:
    """Encoded with the port's configured encoding, utf-8 unless configured
    otherwise, and sent as given."""
    described = property_text(listed["com_write"], "text")
    parts = clauses(described)
    assert one_of(parts, r"\bencod", r"\bconfigured\b|\bport'?s?\b", r"\butf-?8\b", r"\bdefault\b"), described
    assert not claims(described, r"\b(?:always|only)\b[^.;]*\butf-?8\b|\butf-?8\b[^.;]*\b(?:always|only)\b"), described
    assert NO_LINE_ENDING.search(described), described


def test_hex_names_its_digit_pairs_and_that_whitespace_is_ignored(listed: dict[str, dict]) -> None:
    described = property_text(listed["com_write"], "hex")
    assert claims(
        described,
        r"\btwo\b[^.;]*\bdigits?\b|\bdigit pairs?\b|\bbyte pairs?\b|\bpairs? of\b[^.;]*\bdigits\b|\bhex(?:adecimal)? pairs?\b",
    ), described
    assert one_of(clauses(described), r"\bwhitespace\b|\bspaces?\b", r"\bignored\b|\bremoved\b|\bskipped\b|\bstripped\b|\ballowed\b"), described
    assert not claims(described, r"\b(?:whitespace|spaces?)\b[^.;]*\b(?:invalid|not allowed|refused|rejected|forbidden)\b"), described


def test_com_session_start_says_what_it_opens_holds_and_how_it_fails(listed: dict[str, dict]) -> None:
    """What the session is (the port held with its lock until stop, a reader
    buffering for com_read), what keys it, what a repeated start does, what
    opening does to DTR and RTS, and each refusal with its condition."""
    text = definition_text(listed["com_session_start"])
    units = sentences(text)
    missing = names(
        text,
        "com_session_stop",
        "com_read",
        "port_id",
        "already_active",
        "com_port_not_configured",
        "permission_denied",
        "com_port_open_failed",
        "device_busy",
        "resource_busy",
        "com_port_busy",
        "DTR",
        "RTS",
    )
    assert not missing, (missing, text)
    assert one_of(units, r"\block\b", r"\bcom_session_stop\b", r"\b(?:hold|holds|held|keeps?)\b"), text
    assert one_of(units, r"\bbuffer", r"\bcom_read\b"), text
    assert one_of(units, r"\bDTR\b", r"\bRTS\b", r"\breset"), text
    # One session per port: a repeated start keeps the live session and
    # replaces one that failed.
    assert one_of(units, r"\bport_id\b", r"\bsame\b|\bper\b|\bone session\b|\bkeyed\b|\bidentif"), text
    assert one_of(clauses(text), r"\balready_active\b", r"\bkeeps?\b|\bkept\b|\breuses?\b|\bpreserv"), text
    assert one_of(units, r"\breplac|\breopen", r"\bfail|\bdead\b|\binactive\b|\bended\b|\bno longer active\b"), text
    assert not claims(text, r"\balready_active\b[^.;]*\b(?:error|refus|reject)|\b(?:error|refus|reject)\w*\b[^.;]*\balready_active\b"), text
    # All three busy answers, together with what they mean: somebody else has the port.
    assert one_of(units, r"\bdevice_busy\b", r"\bresource_busy\b", r"\bcom_port_busy\b", r"\banother\b|\bheld\b|\bholds?\b|\bother\b"), text
    # The start needs reading or writing allowed, not allow_write in particular.
    assert one_of(units, r"\bpermission_denied\b", r"\bneither\b|\bnor\b", r"\bread", r"\bwrit"), text
    assert not claims(text, r"\bpermission_denied\b[^.;]*\ballow_write\b|\ballow_write\b[^.;]*\bpermission_denied\b"), text


def test_com_session_stop_says_what_it_releases_what_it_leaves_and_how_long_it_waits(listed: dict[str, dict]) -> None:
    """Stop releases the lock, the log keeps what nobody read, no session is a
    success with was_active false, and the only wait is the reader join, after
    which the session stays registered for another stop."""
    text = definition_text(listed["com_session_stop"])
    units = sentences(text)
    parts = clauses(text)
    missing = names(text, "com_session_start", "was_active", "log_path", "com_port_close_failed", "timeout_s")
    assert not missing, (missing, text)
    assert one_of(parts, r"\breleas", r"\block\b"), text
    assert not one_of(parts, NEGATION, r"\breleas"), text
    assert one_of(units, r"\bunread\b|\bnot (?:yet )?read\b", r"\blog\b", r"\bkeeps?\b|\bkept\b|\bretains?\b|\brecords?\b|\bholds?\b"), text
    assert not one_of(parts, r"\blog\b", r"\b(?:delet|remov|discard|lost|lose|clear)"), text
    assert one_of(parts, r"\bwas_active\b", r"\bfalse\b", r"\bwithout\b|\bno session\b|\bnot active\b|\bnone\b", r"\bok\b|\bsucceed"), text
    assert not one_of(parts, r"\bwas_active\b", r"\bfail|\berror\b|\brefus"), text
    assert one_of(units, READER_JOIN_BOUND, r"\breader\b"), text
    assert {float(value) for value in SECONDS.findall(text)} <= {1.0, 0.5}, text
    assert one_of(units, r"\bcom_port_close_failed\b", r"\bagain\b|\bretry", r"\bregistered\b|\bkeeps?\b|\bremains?\b|\bstays?\b"), text


def test_com_write_says_what_it_needs_what_it_sends_and_what_it_returns(listed: dict[str, dict]) -> None:
    """An active session and allow_write, each with its refusal; exactly one
    payload, sent as given; the configured limit in encoded bytes and its
    default; bytes_written as what reached the line; com_read for the reply."""
    text = definition_text(listed["com_write"])
    units = sentences(text)
    parts = clauses(text)
    missing = names(
        text,
        "com_session_start",
        "session_not_active",
        "allow_write",
        "permission_denied",
        "max_write_bytes",
        str(DEFAULT_MAX_WRITE_BYTES),
        "bytes_written",
        "serial_write_incomplete",
        "com_read",
    )
    assert not missing, (missing, text)
    assert one_of(units, r"\bcom_session_start\b", r"\b(?:else|otherwise|without|unless|fails?|returns?|gives?)\b[^.;]*\bsession_not_active\b"), text
    assert not claims(text, r"\bnever\b[^.;]*\bsession_not_active\b|\bsession_not_active\b[^.;]*\bnever\b"), text
    assert one_of(units, r"\b(?:needs?|requires?)\b[^.;]*\ballow_write\b", r"\b(?:else|otherwise|without|fails?|returns?|gives?)\b[^.;]*\bpermission_denied\b"), text
    assert one_of(units, r"\btext\b", r"\bhex\b", r"\bnot both\b|\bexactly one\b|\bone of\b|\beither\b"), text
    assert NO_LINE_ENDING.search(text), text
    # A sentence that speaks of adding a line ending says that none is added.
    adding = [unit for unit in units if claims(unit, ADDS_LINE_ENDING)]
    assert all(claims(unit, NEGATION) for unit in adding), adding
    # The configured limit, counted after encoding, with 4096 as its default only.
    assert one_of(units, r"\bmax_write_bytes\b", r"\bencod"), text
    assert one_of(parts, rf"\b{DEFAULT_MAX_WRITE_BYTES}\b", r"\bdefault\b"), text
    assert not claims(text, rf"\b(?:at most|up to|maximum(?: of)?|limit(?:ed)? (?:of|to))\s+{DEFAULT_MAX_WRITE_BYTES}\b"), text
    # bytes_written is what reached the line, and a short write says so.
    assert one_of(units, r"\bbytes_written\b", r"\bsent\b|\breached\b|\bactually\b|\bwritten to\b"), text
    assert not claims(text, r"\bbytes_written\b[^.;]*\b(?:requested|asked for)\b"), text
    assert one_of(units, r"\bserial_write_incomplete\b", r"\bshort\b|\bfewer\b|\bpartial"), text


def word(name: str) -> str:
    return rf"(?<![A-Za-z0-9_]){name}(?![A-Za-z0-9_])"


# The four calls in order. The start is the one nearest the write: a sentence
# that names com_session_start before the example begins is not part of it.
EXAMPLE = re.compile(
    word("com_session_start") + r"(?:(?!" + word("com_session_start") + r").)*?" + word("com_write") + r"(?P<write>.*?)" + word("com_read") + r"(?P<read>.*?)" + word("com_session_stop"),
    re.DOTALL,
)


class Example:
    """A start, write, read, stop example as a definition writes it."""

    def __init__(self, port_id: str, field: str, payload: str, until: str | None) -> None:
        self.port_id, self.field, self.payload, self.until = port_id, field, payload, until

    @staticmethod
    def literal(raw: str) -> str:
        """The value an agent passes for a quoted literal, read as JSON."""
        return str(json.loads(f'"{raw}"'))

    @classmethod
    def find(cls, description: str) -> Example | None:
        found = EXAMPLE.search(description)
        if found is None:
            return None
        span = found.group(0)
        ports = {cls.literal(raw) for raw in re.findall(rf"\bport_id\b\s*[=:]?\s*{QUOTED}", span)}
        payloads = re.findall(rf"\b(text|hex)\b\s*[=:]?\s*{QUOTED}", found.group("write"))
        untils = re.findall(rf"\buntil\b\s*[=:]?\s*{QUOTED}", found.group("read"))
        assert len(ports) == 1, ("the example names one port, by a port_id value", span)
        assert len(payloads) == 1, ("the example's com_write passes exactly one of text or hex", span)
        assert len(untils) <= 1, span
        return cls(ports.pop(), payloads[0][0], cls.literal(payloads[0][1]), cls.literal(untils[0]) if untils else None)


def test_one_definition_carries_a_valid_start_write_read_stop_example(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, listed: dict[str, dict]) -> None:
    """The four calls in the order an agent makes them, on one port, with
    exactly one payload. The example is then run as written, on a port named as
    the example names it: the board's answer is on the line before the read, so
    a read with `until` returns at once, well inside its 10 s default wait, and
    a read without it returns what is buffered."""
    examples = [example for example in (Example.find(str(listed[name].get("description") or "")) for name in UART_TOOLS) if example is not None]
    assert examples, {name: listed[name].get("description") for name in UART_TOOLS}
    example = examples[0]

    line = install_line(monkeypatch)
    service = new_service(tmp_path / "example", com_ports_yaml=f'com_ports:\n  {example.port_id}:\n    device: "{EXAMPLE_DEVICE}"\n')
    try:
        assert started(service, example.port_id)["already_active"] is False
        written = write(service, example.port_id, **{example.field: example.payload})
        assert written["ok"] is True, written
        sent = example.payload.encode("utf-8") if example.field == "text" else bytes.fromhex(re.sub(r"\s+", "", example.payload))
        assert bytes(line.on_device(EXAMPLE_DEVICE).written) == sent

        answer = f"answer {example.until}" if example.until is not None else "answer\r\n"
        line.on_device(EXAMPLE_DEVICE).deliver(answer.encode("utf-8"))
        received = read(service, example.port_id, **({"until": example.until} if example.until is not None else {}))
        assert received["ok"] is True, received
        assert received["data"]["text"] == answer, received
        if example.until is not None:
            assert received["until_matched"] is True, received

        assert stop(service, example.port_id)["was_active"] is True
    finally:
        close(service)


def test_the_annotations_agree_with_what_the_definitions_describe(listed: dict[str, dict]) -> None:
    """Start can reset a board through DTR or RTS and discards received bytes;
    stop changes nothing a second time; a write is a stimulus the board acts on."""
    assert listed["com_session_start"]["annotations"]["destructiveHint"] is True
    assert listed["com_session_start"]["annotations"]["idempotentHint"] is False
    assert listed["com_session_stop"]["annotations"]["destructiveHint"] is False
    assert listed["com_session_stop"]["annotations"]["idempotentHint"] is True
    assert listed["com_write"]["annotations"]["destructiveHint"] is True
    assert listed["com_write"]["annotations"]["idempotentHint"] is False
    for name in UART_TOOLS:
        assert listed[name]["annotations"]["readOnlyHint"] is False, name


# ---------------------------------------------------------------------------
# Every identifier a definition names is one the server really has.


def config_vocabulary() -> set[str]:
    """The keys an operator writes for a port: the entry's own and its permissions."""
    schema_path = Path(agentic_hil.__file__).resolve().parent / "schemas" / "config.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    entry = schema["properties"]["com_ports"]["additionalProperties"]
    return {"com_ports", *entry["properties"], *schema["$defs"]["io_permissions"]["properties"]}


def answer_vocabulary() -> set[str]:
    """Every snake_case string the package's source spells out whole.

    Each `error_type` and each result field the server can answer with is such
    a string, whether or not a test happens to reach it: the complete set, not
    the outcomes one scenario produced. Words inside a sentence, a docstring or
    a message are not whole strings and do not count."""
    package = Path(agentic_hil.__file__).resolve().parent
    found: set[str] = set()
    for path in package.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and SNAKE_CASE.fullmatch(node.value):
                found.add(node.value)
    return found


def test_every_identifier_the_definitions_name_is_real(listed: dict[str, dict]) -> None:
    """A snake_case word in any of the three definitions is a listed tool, an
    input property of a COM tool, a configuration key of a port, or a string the
    server answers with. Quoted literals are example data and are not read."""
    com_properties = {key for name, tool in listed.items() if name.startswith("com_") for key in tool["inputSchema"]["properties"]}
    vocabulary = set(listed) | com_properties | config_vocabulary() | answer_vocabulary()
    assert "com_port_busy" in vocabulary and "serial_write_incomplete" in vocabulary, "the source vocabulary is read whole"
    assert "com_port_unbound" not in vocabulary, "a near miss is still refused"

    unknown = {name: sorted(set(SNAKE_CASE.findall(re.sub(QUOTED, " ", definition_text(listed[name])))) - vocabulary) for name in UART_TOOLS}
    assert not any(unknown.values()), unknown


# ---------------------------------------------------------------------------
# The claims, as the code answers them today.


def test_com_session_start_opens_the_named_port_buffers_for_com_read_and_holds_its_lock(bench: SimpleNamespace) -> None:
    service, line = bench.service, bench.line
    assert not service.coordinator.leases

    result = started(service)

    assert result["already_active"] is False, result
    assert result["port_id"] == PORT_ID, result
    assert result["session"]["session_active"] is True, result
    assert line.handle(PORT_ID).port == DEVICES[PORT_ID]
    assert service.coordinator.leases, "a session holds its port's lock"
    line.handle(PORT_ID).deliver(b"banner\r\n")
    received = read(service)
    assert received["ok"] is True, received
    assert received["data"]["text"] == "banner\r\n", received


@pytest.mark.parametrize(("port_id", "asserted"), [(PORT_ID, True), (QUIET_PORT_ID, False)])
def test_com_session_start_drives_dtr_and_rts_as_configured(bench: SimpleNamespace, port_id: str, asserted: bool) -> None:
    """Asserted by default, so a board that wires DTR or RTS to reset restarts
    on the open; `assert_dtr: false` and `assert_rts: false` leave both low."""
    started(bench.service, port_id)

    handle = bench.line.handle(port_id)
    assert (handle.dtr, handle.rts) == (asserted, asserted)


def test_a_repeated_start_answers_already_active_and_clears_unless_told_not_to(bench: SimpleNamespace) -> None:
    service, line = bench.service, bench.line
    started(service)
    line.handle(PORT_ID).deliver(b"stale\r\n")

    again = started(service)

    assert again["already_active"] is True, again
    assert line.opened_on(PORT_ID) == 1, "the active session is kept, not reopened"
    assert line.handle(PORT_ID).input_resets == 2, "the default clear_buffer resets the driver input at each start"
    assert read(service)["bytes_read"] == 0, "the bytes received before the repeated start were discarded"

    line.handle(PORT_ID).deliver(b"kept\r\n")
    kept = started(service, clear_buffer=False)

    assert kept["already_active"] is True, kept
    assert read(service)["data"]["text"] == "kept\r\n"


def test_a_repeated_start_replaces_a_session_whose_reader_failed(bench: SimpleNamespace) -> None:
    """The session is keyed by port_id, and only a live one is kept: once the
    reader has failed, the session is no longer active, and the next start
    closes it and opens the port again."""
    service, line = bench.service, bench.line
    started(service)
    failed = line.handle(PORT_ID)
    failed.feed(DIE)
    deadline = time.monotonic() + scaled_time_bound(5.0)
    while service.com_ports.sessions[PORT_ID].reader_error is None:
        assert time.monotonic() < deadline, "the reader never failed"
        time.sleep(0.01)
    assert write(service, text="x")["error_type"] == "session_not_active"
    assert failed.written == bytearray(), "a write on the failed session sent nothing"

    again = started(service)

    assert again["already_active"] is False, again
    assert line.opened_on(PORT_ID) == 2, "the failed session was replaced by a new open"
    assert failed.is_open is False, "the failed session's handle was closed"
    assert write(service, text="PING")["ok"] is True
    assert bytes(line.handle(PORT_ID).written) == b"PING"


def test_an_unknown_port_id_is_refused_as_not_configured_by_all_three(bench: SimpleNamespace) -> None:
    for result in (start(bench.service, UNKNOWN_PORT_ID), write(bench.service, UNKNOWN_PORT_ID, text="x"), stop(bench.service, UNKNOWN_PORT_ID)):
        assert result["error_type"] == "com_port_not_configured", result
        assert PORT_ID in result["configured_ports"], result
    assert bench.line.handles == []


@pytest.mark.parametrize(
    ("config_version", "com_ports_yaml", "port_id", "starts"),
    [
        pytest.param(None, COM_PORTS_YAML, LOCKED_PORT_ID, False, id="v1-neither-read-nor-write-granted"),
        pytest.param(None, COM_PORTS_YAML, LISTEN_PORT_ID, True, id="v1-read-granted-write-not"),
        pytest.param(2, V2_COM_PORTS_YAML, LISTEN_PORT_ID, True, id="v2-reading-needs-no-grant"),
    ],
)
def test_com_session_start_needs_reading_or_writing_allowed_and_com_write_needs_allow_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, config_version: int | None, com_ports_yaml: str, port_id: str, starts: bool
) -> None:
    """The start is refused only where the port may be neither read nor
    written: under version 1 that is both grants withheld, and from version 2
    on reading needs no grant, so the start is never refused for permission.
    allow_write is what the write needs, not the start."""
    line = install_line(monkeypatch)
    service = new_service(tmp_path / "workspace", com_ports_yaml=com_ports_yaml, config_version=config_version)
    try:
        result = start(service, port_id)
        if not starts:
            assert result["error_type"] == "permission_denied", result
            assert line.handles == []
            return
        assert result["ok"] is True, result
        line.handle(port_id).deliver(b"banner\r\n")
        assert read(service, port_id)["data"]["text"] == "banner\r\n"
        refused = write(service, port_id, text="PING")
        assert refused["error_type"] == "permission_denied", refused
        assert "allow_write" in refused["summary"], refused
        assert bytes(line.handle(port_id).written) == b""
    finally:
        close(service)


def test_com_session_start_on_a_port_that_will_not_open_fails_open(bench: SimpleNamespace) -> None:
    bench.line.absent.add(DEVICES[ABSENT_PORT_ID])

    refused = start(bench.service, ABSENT_PORT_ID)

    assert refused["error_type"] == "com_port_open_failed", refused
    assert not bench.service.coordinator.leases, "a refused open holds nothing"


def test_a_port_another_server_holds_is_busy_until_its_session_stops(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    install_line(monkeypatch)
    first = new_service(tmp_path / "workspace-a", state_root=tmp_path / "state-a")
    second = new_service(tmp_path / "workspace-b", state_root=tmp_path / "state-b")
    try:
        started(first)

        refused = start(second)
        assert refused["error_type"] == "device_busy", refused

        assert stop(first)["was_active"] is True
        assert started(second)["already_active"] is False
    finally:
        close(second)
        close(first)


def test_a_port_another_process_of_the_same_configuration_holds_is_resource_busy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Two servers on one project configuration meet at its own lock first."""
    line = install_line(monkeypatch)
    first = new_service(tmp_path / "workspace")
    second = new_service(tmp_path / "workspace")
    try:
        started(first)

        refused = start(second)
        assert refused["error_type"] == "resource_busy", refused
        assert line.opened_on(PORT_ID) == 1, "the refused start opened nothing"

        assert stop(first)["was_active"] is True
        assert started(second)["already_active"] is False
    finally:
        close(second)
        close(first)


def test_a_device_another_program_holds_is_com_port_busy(bench: SimpleNamespace) -> None:
    """The open itself is refused by the operating system: another program,
    not another Agentic HIL server, has the device. Nothing stays held, and the
    start succeeds once the device is free."""
    service, line = bench.service, bench.line
    line.held.add(DEVICES[PORT_ID])

    refused = start(service)

    assert refused["error_type"] == "com_port_busy", refused
    assert not service.coordinator.leases, "a refused open holds nothing"

    line.held.discard(DEVICES[PORT_ID])
    assert started(service)["already_active"] is False


def test_com_session_stop_releases_the_port_and_leaves_unread_bytes_in_the_log(bench: SimpleNamespace) -> None:
    service, line = bench.service, bench.line
    started(service)
    line.handle(PORT_ID).deliver(b"never read\r\n")

    stopped = stop(service)

    assert stopped["ok"] is True, stopped
    assert stopped["was_active"] is True, stopped
    assert not service.coordinator.leases, "stop released the port's lock"
    assert line.handle(PORT_ID).is_open is False
    assert b"never read\r\n" in logged_rx(service, stopped["session"]["log_path"])
    assert read(service)["error_type"] == "session_not_active"


def test_com_session_stop_without_a_session_answers_was_active_false(bench: SimpleNamespace) -> None:
    first = stop(bench.service)
    started(bench.service)
    stop(bench.service)
    again = stop(bench.service)

    for result in (first, again):
        assert result["ok"] is True, result
        assert result["was_active"] is False, result


@pytest.mark.parametrize(
    ("port_id", "timeout_s"),
    [pytest.param(PORT_ID, DEFAULT_TIMEOUT_S, id="default-timeout"), pytest.param(SLOW_PORT_ID, 1.5, id="timeout-1.5s")],
)
def test_com_session_stop_waits_a_bounded_time_for_the_reader_and_can_be_retried(bench: SimpleNamespace, port_id: str, timeout_s: float) -> None:
    """A reader that does not end is waited for max(1 s, timeout_s + 0.5 s),
    then the stop answers `com_port_close_failed` and keeps the session, and a
    second stop after the reader ended closes it."""
    service, line = bench.service, bench.line
    started(service, port_id)
    handle = line.handle(port_id)
    handle.stall = True
    assert handle.stalled.wait(scaled_time_bound(5.0)), "the reader never entered the stalled read"
    bound = max(1.0, timeout_s + 0.5)

    began = time.monotonic()
    refused = stop(service, port_id)
    elapsed = time.monotonic() - began

    assert refused["error_type"] == "com_port_close_failed", refused
    assert "reader" in refused["backend_error"], refused
    assert elapsed >= bound - 0.05, elapsed
    assert elapsed < scaled_time_bound(bound + 5.0), elapsed
    assert port_id in service.com_ports.sessions, "the session stays registered for a retry"

    handle.unstall.set()
    reader = service.com_ports.sessions[port_id].reader
    assert reader is not None
    reader.join(scaled_time_bound(5.0))
    retried = stop(service, port_id)

    assert retried["ok"] is True, retried
    assert retried["was_active"] is True, retried
    assert not service.coordinator.leases


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        pytest.param({"text": "PING"}, b"PING", id="text-without-line-ending"),
        pytest.param({"text": "PING\r\n"}, b"PING\r\n", id="text-with-its-own-line-ending"),
        pytest.param({"hex": "50 49\n4e\t47 0d0a"}, b"PING\r\n", id="hex-with-whitespace"),
        pytest.param({"text": "café"}, b"caf\xc3\xa9", id="text-in-utf-8-by-default"),
    ],
)
def test_com_write_sends_exactly_the_given_bytes_and_reports_them(bench: SimpleNamespace, payload: dict, expected: bytes) -> None:
    service, line = bench.service, bench.line
    started(service)

    written = write(service, **payload)

    assert written["ok"] is True, written
    assert bytes(line.handle(PORT_ID).written) == expected
    assert written["bytes_written"] == len(expected), written
    assert written["data"]["hex"] == expected.hex(), written
    assert written["data"]["encoding"] == "utf-8", written
    assert written["log_path"], written


def test_com_write_encodes_text_with_the_ports_own_encoding(bench: SimpleNamespace) -> None:
    service, line = bench.service, bench.line
    started(service, LATIN1_PORT_ID)
    started(service, ASCII_PORT_ID)

    latin1 = write(service, LATIN1_PORT_ID, text="café")
    refused = write(service, ASCII_PORT_ID, text="café")

    assert latin1["ok"] is True, latin1
    assert bytes(line.handle(LATIN1_PORT_ID).written) == b"caf\xe9"
    assert refused["error_type"] == "invalid_argument", refused
    assert refused["encoding"] == "ascii", refused
    assert bytes(line.handle(ASCII_PORT_ID).written) == b""


@pytest.mark.parametrize("hex_payload", ["abc", "0g", "0x41"])
def test_com_write_refuses_hex_that_is_not_whole_hex_bytes(bench: SimpleNamespace, hex_payload: str) -> None:
    started(bench.service)

    refused = write(bench.service, hex=hex_payload)

    assert refused["error_type"] == "invalid_argument", refused
    assert bytes(bench.line.handle(PORT_ID).written) == b""


@pytest.mark.parametrize(
    ("port_id", "limit"),
    [pytest.param(PORT_ID, DEFAULT_MAX_WRITE_BYTES, id="4096-by-default"), pytest.param(SMALL_PORT_ID, SMALL_MAX_WRITE_BYTES, id="as-configured")],
)
def test_com_write_refuses_more_than_the_ports_max_write_bytes(bench: SimpleNamespace, port_id: str, limit: int) -> None:
    service, line = bench.service, bench.line
    started(service, port_id)

    at_limit = write(service, port_id, hex="41" * limit)
    over = write(service, port_id, hex="41" * (limit + 1))

    assert at_limit["bytes_written"] == limit, at_limit
    assert over["error_type"] == "invalid_argument", over
    assert over["max_write_bytes"] == limit, over
    assert over["bytes_requested"] == limit + 1, over
    assert len(line.handle(port_id).written) == limit


def test_max_write_bytes_counts_the_encoded_bytes_not_the_characters(bench: SimpleNamespace) -> None:
    """é is two bytes in utf-8: one character under the limit can be one byte over it."""
    service, line = bench.service, bench.line
    started(service)

    fits = write(service, text="A" * (DEFAULT_MAX_WRITE_BYTES - 2) + "é")
    over = write(service, text="A" * (DEFAULT_MAX_WRITE_BYTES - 1) + "é")

    assert fits["bytes_written"] == DEFAULT_MAX_WRITE_BYTES, fits
    assert over["error_type"] == "invalid_argument", over
    assert over["bytes_requested"] == DEFAULT_MAX_WRITE_BYTES + 1, over
    assert len(line.handle(PORT_ID).written) == DEFAULT_MAX_WRITE_BYTES


@pytest.mark.parametrize("payload", [pytest.param({"text": "PING", "hex": "50"}, id="both"), pytest.param({}, id="neither")])
def test_com_write_takes_exactly_one_of_text_or_hex(bench: SimpleNamespace, payload: dict) -> None:
    started(bench.service)

    refused = write(bench.service, **payload)

    assert refused["error_type"] == "invalid_argument", refused
    assert bytes(bench.line.handle(PORT_ID).written) == b""


@pytest.mark.parametrize(("accepted_per_write", "sent"), [pytest.param(2, 8, id="two-bytes-per-attempt"), pytest.param(0, 0, id="nothing-taken")])
def test_a_short_write_reports_the_bytes_that_reached_the_line(bench: SimpleNamespace, accepted_per_write: int, sent: int) -> None:
    """bytes_written is what the driver took, after a bounded retry of the
    remainder, not what was asked for; a write that stays short is
    serial_write_incomplete."""
    service, line = bench.service, bench.line
    started(service)
    line.accepts_per_write[DEVICES[PORT_ID]] = accepted_per_write

    short = write(service, text="0123456789")

    assert short["error_type"] == "serial_write_incomplete", short
    assert short["bytes_written"] == sent, short
    assert short["bytes_requested"] == 10, short
    assert bytes(line.handle(PORT_ID).written) == b"0123456789"[:sent]


def test_com_write_needs_a_session_from_com_session_start(bench: SimpleNamespace) -> None:
    refused = write(bench.service, SPARE_PORT_ID, text="PING\r\n")

    assert refused["error_type"] == "session_not_active", refused
    assert "com_session_start" in refused["summary"], refused
    assert bench.line.handles == [], "a write never opens the port itself"


def test_a_reply_that_arrives_before_com_read_is_buffered_for_it(bench: SimpleNamespace) -> None:
    """Start, write, read with `until`, stop. The reply reaches the session
    before `com_read` is called and is still handed out, through the match,
    by that call; what follows the match stays buffered."""
    service, line = bench.service, bench.line
    started(service)

    assert write(service, text="PING\r\n")["ok"] is True
    line.handle(PORT_ID).deliver(b"PONG\r\nidle\r\n")
    received = read(service, until="PONG", wait_timeout_s=0)
    rest = read(service)
    stopped = stop(service)

    assert received["ok"] is True, received
    assert received["until_matched"] is True, received
    assert received["data"]["text"] == "PONG", received
    assert rest["data"]["text"] == "\r\nidle\r\n", rest
    assert stopped["was_active"] is True, stopped
