"""What `com_session_start`, `com_session_stop`, `com_write` and `com_read` tell an agent.

The definitions said what the tools are and little else: nothing about
which port a `port_id` names, what a repeated start does, what stop leaves
behind, whether a write adds a line ending, how large it may be, or which tool
reads the reply; and nothing about how long a read waits, how much it returns,
or what it leaves for the next one. These tests ask the definitions a host receives through
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
from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import write_config
from support import scaled_time_bound
from test_read_until import DIE, ScriptedSerialHandle, close, tools_call

import agentic_hil
from agentic_hil import comports
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
# A word that denies what follows it. `cannot` and the n't contractions (can't,
# won't, doesn't, isn't) deny as plainly as `not`; the whole is one group, so it
# can sit inside a longer pattern without its alternation leaking out.
NEGATION = r"(?:\b(?:no|not|never|nothing|without|cannot)\b|n't\b)"
# How far back a negation reaches a predicate: to the start of its phrase, which
# a full stop that closes a sentence, a semicolon, a comma or a colon ends (a
# full stop inside "0.1" or "debug.allowed_symbols" ends nothing), and within the
# phrase to the last `and`, `but` or `then`, which opens a predicate of its own:
# "It doesn't change the stop reason and resumes the core" resumes it. `or` and
# `nor` open none: "never halts or resumes" denies both.
PHRASE_END = r"[;,:]|\.(?=\s|$)"
PREDICATE_START = r"\b(?:and|but|then)\b"
# A phrase opened by a negated subject denies all it says: "No damaged report
# state answers `config_invalid`".
NEGATED_SUBJECT = r"\s*(?:no|none|nothing|neither|never)\b"
# A comparative or time qualifier bounds a predicate and denies nothing:
# "resumed no later than one second after the call" resumes it.
QUALIFIER = r"\bno\s+(?:later|more|sooner|longer|less)\s+than\b|\bnot\s+(?:before|after)\b"
# An object that denies the predicate before it: "lifts nothing", "lifts no hold".
NEGATED_OBJECT = rf"\s+(?!{QUALIFIER})(?:nothing|none|no)\b"


def denies(found: re.Match[str], negation: str = NEGATION) -> bool:
    """Whether the predicate `found` matched is denied in the text it was found
    in: a negation within its own predicate before it, a negated subject that
    opens its phrase, or a negated object right after it. A negation elsewhere
    in the sentence governs another predicate and denies nothing here."""
    text = found.string
    phrase = re.split(PHRASE_END, text[: found.start()])[-1]
    predicate = re.split(PREDICATE_START, phrase, flags=re.IGNORECASE)[-1]
    return bool(
        re.search(negation, predicate, re.IGNORECASE)
        or re.match(NEGATED_SUBJECT, phrase, re.IGNORECASE)
        or re.match(NEGATED_OBJECT, text[found.end() :], re.IGNORECASE)
    )


def stated(pattern: str, text: str, negation: str = NEGATION, flags: int = re.IGNORECASE) -> bool:
    """Whether some match of `pattern` in `text` is stated, not denied."""
    return any(not denies(found, negation) for found in re.finditer(pattern, text, flags))


def denied(pattern: str, text: str, negation: str = NEGATION, flags: int = re.IGNORECASE) -> bool:
    """Whether some match of `pattern` in `text` is denied."""
    return any(denies(found, negation) for found in re.finditer(pattern, text, flags))


# A verb that adds, followed in its sentence by the line ending it would add.
ADDS_LINE_ENDING = r"\b(?:adds?|appends?|added|appended)\b(?=[^.;]*\b(?:line ending|newline|terminator)\b)"
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
    # A sentence that speaks of adding a line ending says that none is added:
    # the adding itself is denied ("adds no line ending"), not another verb in
    # the sentence ("adds a newline and doesn't pad it").
    adding = [found for unit in units for found in re.finditer(ADDS_LINE_ENDING, unit, re.IGNORECASE)]
    assert not [found.string for found in adding if not denies(found)], text
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


# ---------------------------------------------------------------------------
# com_read: what it needs, how long it waits, how much it returns, what it
# leaves for the next read.

READ_TOOL = "com_read"
READ_PROPERTIES = ("port_id", "max_bytes", "wait_timeout_s", "until")
# The config default of a com_ports entry's max_buffer_bytes (src/agentic_hil/config.py).
DEFAULT_MAX_BUFFER_BYTES = 65536
# The waits com_read applies: none by default without until, ten seconds by
# default with it, and never more than sixty either way (src/agentic_hil/comports.py,
# src/agentic_hil/readuntil.py).
UNTIL_DEFAULT_WAIT_S = 10.0
WAIT_CAP_S = 60.0
# What until takes: one text, or a list of 1 to 8, each at most 256 characters
# (src/agentic_hil/readuntil.py).
UNTIL_MAX_ENTRIES = 8
UNTIL_MAX_CHARACTERS = 256
# Ports of the com_read tests that need a setting COM_PORTS_YAML does not carry.
BUFFER_PORT_ID = "tooldef_buffer"
WRITE_ONLY_PORT_ID = "tooldef_write_only"
READ_DEVICES = {BUFFER_PORT_ID: "/dev/ttyTOOLDEF11", WRITE_ONLY_PORT_ID: "/dev/ttyTOOLDEF12"}
SMALL_MAX_BUFFER_BYTES = 64

# until left out, in the words a definition may use for that.
WITHOUT_UNTIL = (
    r"\b(?:without|no)\s+(?:an?\s+)?until\b"
    r"|\buntil\b\s+(?:is\s+)?(?:absent|omitted|unset|missing|not (?:given|set|passed))\b"
    r"|\b(?:absent|omitted|unset)\s+until\b"
)
# An until entry that was not seen before the wait ended.
MISS = (
    r"\bno match\b|\bnot (?:seen|found|matched)\b|\bwithout (?:a )?match\b|\bunmatched\b|\bnothing matches\b"
    r"|\bnever (?:seen|appears|arrives|matches)\b|\bmiss(?:es|ed)?\b|\btime[sd]? out\b|\btimeout\b|\bwait (?:ends|runs out|expires)\b"
)
# A negation denies what follows it up to the end of its own phrase: the next
# comma, colon, bracket or joining word, so "not discarded but stays buffered"
# keeps the bytes. A condition worded with a negation ("without until", "if
# until is not seen") says when, and denies nothing.
NEGATED = r"\b(?:no|not|never|nothing|none|neither|nor|without|cannot)\b|n't\b"
CONDITION = rf"{WITHOUT_UNTIL}|{MISS}"
PHRASE_BREAK = re.compile(r"[,:()]|\s(?:and|but|while|whereas)\s", re.IGNORECASE)


def phrases(clause: str) -> list[str]:
    return [part for part in PHRASE_BREAK.split(clause) if part.strip()]


def affirms(unit: str, pattern: str) -> bool:
    """Whether `unit` states `pattern` with no negation before it in its own phrase."""
    for found in re.finditer(pattern, unit, re.IGNORECASE):
        lead = PHRASE_BREAK.split(unit[: found.start()])[-1]
        if not claims(re.sub(CONDITION, " ", lead, flags=re.IGNORECASE), NEGATED):
            return True
    return False


def with_until(phrase: str) -> bool:
    return claims(phrase, r"\buntil\b") and not claims(phrase, WITHOUT_UNTIL)


# The relations a com_read definition could state the wrong way round. Each is
# a named check over a text, so the controls below can show it accepts the true
# statement and refuses the inverted one.

ZERO = r"\b0(?:\.0)?\s?s?\b|\bzero\b|\bno wait\b|\bdoes not wait\b|\bat once\b|\bimmediately\b"
ZERO_VALUE = r"\b0(?:\.0)?\s?s?\b|\bzero\b"
TEN = r"\b10(?:\.0)?\s?(?:s|seconds?)?\b|\bten seconds\b"
SIXTY = r"\b60(?:\.0)?\s?(?:s|seconds?)?\b|\bsixty seconds\b"
CAPPED = r"\bcap(?:s|ped)?\b|\bclamp\w*|\bcut\b|\bat most\b|\bup to\b|\bno more than\b|\bmax(?:imum)?\b|\blimit(?:s|ed)?\b|\bceiling\b"
REFUSED = r"\brefus\w*|\breject\w*|\binvalid\w*|\berror\b"


def wait_defaults_follow_until(text: str) -> bool:
    """comports.py:1265 and readuntil.py:34: without until a read waits 0 s
    unless asked, with until 10 s. Each default is read in the phrase that
    names its condition, so the two cannot trade places."""
    zero = ten = False
    for clause in clauses(text):
        defaults = claims(clause, r"\bdefault")
        for phrase in phrases(clause):
            if claims(phrase, WITHOUT_UNTIL):
                if claims(phrase, TEN):
                    return False
                zero = zero or (defaults and affirms(phrase, ZERO))
            elif with_until(phrase):
                if claims(phrase, ZERO_VALUE):
                    return False
                ten = ten or (defaults and affirms(phrase, TEN))
    return zero and ten


def waits_are_capped_at_sixty(text: str) -> bool:
    """comports.py:1277 and readuntil.py:34: a longer wait is cut to 60 s, not refused."""
    units = clauses(text)
    capped = any(claims(unit, SIXTY) and affirms(unit, CAPPED) for unit in units)
    refused = any(claims(unit, SIXTY) and affirms(unit, REFUSED) for unit in units)
    return capped and not refused


# A wait that ends on the first bytes to arrive, whatever they are.
FIRST_BYTES = (
    r"\bfirst\b[^.;]*\b(?:bytes?|data|feedback|output|chunk)\b"
    r"|\bas soon as\b[^.;]*\b(?:bytes?|data|feedback|output|anything)\b"
    r"|\bany\b[^.;]*\b(?:bytes?|data|feedback|output)\b[^.;]*\barriv"
)


def a_plain_wait_ends_at_the_first_bytes(text: str) -> bool:
    """comports.py:1277-1284: without until, the first bytes buffered end a
    wait, even part of a line. Said of a read without until, not of until."""
    return any(
        claims(clause, WITHOUT_UNTIL) and claims(clause, r"\bwait") and any(affirms(phrase, FIRST_BYTES) and not with_until(phrase) for phrase in phrases(clause))
        for clause in clauses(text)
    )


def max_bytes_is_the_most_bytes_returned(text: str) -> bool:
    """comports.py:1268 and 1286: a count of bytes, at least 1, not of characters or lines."""
    units = clauses(text)
    meant = any(affirms(unit, r"\b(?:most|max(?:imum)?|at most|up to|limit)\b[^.;]*\bbytes\b") for unit in units)
    other_unit = any(affirms(unit, r"\b(?:characters?|chars|lines?)\b") for unit in units)
    other_minimum = claims(text, r"\b(?:at least|minimum(?: of)?|min\.?)\s+(?!1\b)\d+")
    return meant and not other_unit and not other_minimum


def max_bytes_defaults_to_max_buffer_bytes(text: str) -> bool:
    """comports.py:1264 and config.py:3600: unset, it is the port's
    max_buffer_bytes, 65536 unless the entry sets another. 65536 is that
    default, so a clause naming it says so; it is not a limit of max_bytes."""
    units = clauses(text)
    named = one_of(units, r"\bmax_buffer_bytes\b", r"\bdefault")
    unconditional = any(claims(unit, rf"\b{DEFAULT_MAX_BUFFER_BYTES}\b") and not claims(unit, r"\bdefault|\bunless\b|\bconfigured\b") for unit in units)
    return named and not unconditional


REST = r"\brest\b|\bremainder\b|\bremaining\b|\bexcess\b|\bwhat (?:is left|remains|follows)\b"
KEPT = r"\bbuffer(?:ed|s)?\b|\bnext\b|\blater\b|\bkept\b|\bstays?\b|\bremains?\b"
LOST = r"\bdiscard\w*|\bdrop\w*|\blost\b|\blose\b|\bdelet\w*|\btruncat\w*|\bgone\b"


def the_rest_stays_buffered(text: str) -> bool:
    """comports.py:1286-1287 and 1329-1331: what a read does not return stays for the next one."""
    units = clauses(text)
    kept = any(claims(unit, REST) and affirms(unit, KEPT) for unit in units)
    lost = any(claims(unit, REST) and affirms(unit, LOST) for unit in units)
    return kept and not lost


THROUGH = r"\bthrough\b|\bincluding\b|\bends? (?:with|at)\b|\bending (?:with|at)\b"


def the_answer_runs_through_the_first_match(text: str) -> bool:
    """comports.py:1326-1331: the bytes returned end with the earliest-ending match."""
    return any(claims(unit, r"\bfirst\b") and claims(unit, r"\bmatch") and affirms(unit, THROUGH) for unit in clauses(text))


LITERAL = r"\bliteral(?:ly)?\b|\bexact(?:ly)?\b|\bplain\b|\bverbatim\b|\bas is\b|\bcase-sensitive\b"
PATTERN_LANGUAGE = r"\breg(?:ular expression|ex(?:es)?)\b|\bwildcards?\b|\bglob\b"


def until_is_literal_text(text: str) -> bool:
    """readuntil.py:87: bytes.find, so literal text, case counting, no pattern language."""
    units = clauses(text)
    return any(affirms(unit, LITERAL) for unit in units) and not any(affirms(unit, PATTERN_LANGUAGE) for unit in units)


def until_limits_are_true(text: str) -> bool:
    """readuntil.py:54-62: one text or a list of 1 to 8, each at most 256 characters."""
    unlimited = any(affirms(unit, r"\bany (?:number|length|size)\b|\bunlimited\b|\bno (?:limit|maximum)\b") for unit in clauses(text))
    counts = {int(count) for count in re.findall(r"\b(\d+)\s+(?:texts|entries|strings|patterns)\b", text, re.IGNORECASE)}
    lengths = {int(length) for length in re.findall(r"\b(\d+)\s+(?:characters|chars)\b", text, re.IGNORECASE)}
    return counts == {UNTIL_MAX_ENTRIES} and lengths <= {UNTIL_MAX_CHARACTERS} and not unlimited


ENDS = r"\bends?\b|\bstops?\b|\bfinish\w*|\breturns?\b|\banswers?\b"


def max_bytes_also_ends_an_until_wait(text: str) -> bool:
    """comports.py:1321: with until, max_bytes buffered without a match ends the wait as well."""
    units = clauses(text)
    ends = any(claims(unit, r"\bmax_bytes\b") and claims(unit, r"\buntil\b|\bmatch") and affirms(unit, ENDS) for unit in units)
    only_time = any(affirms(unit, r"\bonly\b[^.;]*\b(?:timeout|time|deadline|wait_timeout_s|seconds?)\b") for unit in units)
    return ends and not only_time


RETURNED = r"\breturn\w*|\bhand(?:s|ed)? out\b|\bgives?\b|\bgiven\b|\bwith\b"
RECEIVED_BYTES = r"\b(?:bytes|data|feedback|output)\b|\barrived\b|\bbuffered\b|\breceived\b|\bso far\b"


def a_missed_until_is_an_ok_answer(text: str) -> bool:
    """comports.py:1341-1355: no match by the end of the wait answers ok, with
    until_matched false and the bytes that did arrive."""
    missed = [unit for unit in clauses(text) if claims(unit, MISS)]
    false = any(claims(unit, r"\buntil_matched\b[^.;]{0,12}\bfalse\b") for unit in missed)
    true = any(claims(unit, r"\buntil_matched\b[^.;]{0,12}\btrue\b") for unit in missed)
    ok = any(affirms(unit, r"\bok\b|\bsucce\w*|\bstill (?:returns|answers)\b") or claims(unit, r"\bnot an? (?:error|failure|refusal)\b") for unit in missed)
    failed = any(affirms(unit, r"\berror\b|\bfail\w*|\brefus\w*") for unit in missed)
    returned = any(claims(unit, RETURNED) and affirms(unit, RECEIVED_BYTES) for unit in missed)
    return false and not true and ok and not failed and returned


def a_read_needs_a_session_from_com_session_start(text: str) -> bool:
    """comports.py:1613-1615: no session answers session_not_active, naming com_session_start."""
    tied = one_of(sentences(text), r"\bcom_session_start\b", r"\b(?:else|otherwise|without|unless|fails?|returns?|gives?|answers?)\b[^.;]*\bsession_not_active\b")
    never = claims(text, r"\bnever\b[^.;]*\bsession_not_active\b|\bsession_not_active\b[^.;]*\bnever\b")
    return tied and not never


READING_ALLOWED = (
    r"\ballow_read\b"
    r"|\bread(?:ing)?\b[^.;]*\b(?:allow\w*|permit\w*|disabled|grant\w*|off|denied)\b"
    r"|\b(?:allow\w*|permit\w*|disabled|grant\w*)\b[^.;]*\bread(?:ing)?\b"
)
VERSION_1 = r"\bversion 1\b(?!\s*(?:and|or)\s+(?:later|newer|above|up))|\bv1\b"


def reading_needs_allow_read_under_version_1_only(text: str) -> bool:
    """types.py:591 and 605-606, comports.py:1239-1240: only a version 1
    configuration can withhold reading, through allow_read; from version 2 on
    reading needs no grant. Never a matter of allow_write."""
    units = sentences(text)
    tied = any(claims(unit, r"\bpermission_denied\b") and claims(unit, READING_ALLOWED) and claims(unit, VERSION_1) for unit in units)
    unscoped = any(claims(unit, r"\ballow_read\b|\bpermission_denied\b") and not claims(unit, VERSION_1) for unit in units)
    write = any(claims(unit, r"\bpermission_denied\b") and claims(unit, r"\ballow_write\b") for unit in units)
    return tied and not unscoped and not write


# Each byte is handed out once: a read takes what it returns off the buffer.
CONSUMES = (
    r"\b(?:returned|read|handed out|given)\s+(?:only\s+)?once\b|\bremov\w*|\bconsum\w*|\bdelet\w*|\bdrain\w*"
    r"|\btakes?\b[^.;]*\b(?:off|out of)\b[^.;]*\bbuffer"
)


def each_byte_is_returned_once(text: str) -> bool:
    """comports.py:1286-1287: a read takes what it returns out of the buffer."""
    return any(claims(unit, r"\bbytes?\b|\bbuffer\w*|\bfeedback\b|\bdata\b") and affirms(unit, CONSUMES) for unit in clauses(text))


READ_RELATIONS: list[tuple[Callable[[str], bool], str, str]] = [
    (wait_defaults_follow_until, "Without until the default is 0 and a wait ends at the first bytes; with until the default is 10 s.", "Without until default 10 s, with until default 0 s; capped at 60 s."),
    (wait_defaults_follow_until, "The default is 0 without until and 10 s with until.", "With until the default is 0; without until the default is 10 s."),
    (waits_are_capped_at_sixty, "Waits are capped at 60 s.", "Never capped at 60 s."),
    (waits_are_capped_at_sixty, "A longer wait is cut to 60 s, not refused.", "A wait over 60 s is refused as invalid."),
    (a_plain_wait_ends_at_the_first_bytes, "A positive wait ends at the first bytes if until is absent.", "A wait without until never ends at the first bytes."),
    (a_plain_wait_ends_at_the_first_bytes, "Without until, a wait ends as soon as any bytes are buffered.", "With until a wait ends at the first bytes, without until at the deadline."),
    (max_bytes_is_the_most_bytes_returned, "Most bytes to return.", "Most characters to return."),
    (max_bytes_is_the_most_bytes_returned, "The most bytes one read returns, at least 1.", "Most bytes to return, at least 2."),
    (max_bytes_defaults_to_max_buffer_bytes, "Returns up to 65536 bytes by default unless max_buffer_bytes is configured.", "Defaults to max_buffer_bytes; at most 65536."),
    (max_bytes_defaults_to_max_buffer_bytes, "Default: the port's max_buffer_bytes (65536 unless configured).", "At most 65536 bytes."),
    (the_rest_stays_buffered, "The remainder is not discarded but stays buffered.", "The rest is dropped."),
    (the_rest_stays_buffered, "Returns through the first match; the rest stays buffered for the next read.", "Never returns through the first match; the rest is never buffered."),
    (the_answer_runs_through_the_first_match, "Returns the feedback through the first match.", "Never returns through the first match; the rest is never buffered."),
    (until_is_literal_text, "Matched literally as bytes, not as a regex.", "Not a literal text: a regex."),
    (until_is_literal_text, "Exact text, case-sensitive.", "A regex, not plain text."),
    (until_limits_are_true, "Text, or up to 8 texts of at most 256 characters.", "Any number of texts of any length."),
    (until_limits_are_true, "Text, or up to 8 texts.", "Text, or up to 16 texts."),
    (max_bytes_also_ends_an_until_wait, "With until, the wait also ends once max_bytes are buffered without a match.", "With no match, only timeout can end the wait."),
    (max_bytes_also_ends_an_until_wait, "A wait for until ends at a match or once max_bytes are buffered.", "max_bytes buffered never ends a wait for until."),
    (
        a_missed_until_is_an_ok_answer,
        "If until is not seen in time, the answer is still ok, with until_matched false and the bytes so far.",
        "No match at timeout: not ok, until_matched false, returns no received bytes.",
    ),
    (
        a_missed_until_is_an_ok_answer,
        "No match by the deadline is not an error: ok, until_matched false, and the bytes received are returned.",
        "If until is not seen in time, the answer is ok with until_matched true and the bytes so far.",
    ),
    (a_read_needs_a_session_from_com_session_start, "Needs a session from com_session_start, else session_not_active.", "Needs a session from com_session_start; never session_not_active."),
    (reading_needs_allow_read_under_version_1_only, "Under config version 1, permission_denied unless allow_read is true.", "Needs allow_read (else permission_denied)."),
    (reading_needs_allow_read_under_version_1_only, "Config version 1 only: reading off gives permission_denied.", "Under config version 1, permission_denied unless allow_write."),
    (each_byte_is_returned_once, "Each byte is returned once, as text and hex.", "Bytes are never removed from the buffer by a read."),
]


@pytest.mark.parametrize(("check", "true_statement", "inverted"), READ_RELATIONS, ids=[f"{check.__name__}-{index}" for index, (check, _, _) in enumerate(READ_RELATIONS)])
def test_each_com_read_relation_check_refuses_its_inverted_statement(check: Callable[[str], bool], true_statement: str, inverted: str) -> None:
    assert check(true_statement), true_statement
    assert not check(inverted), inverted


def test_com_read_is_listed_and_every_input_describes_itself_within_the_budget(listed: dict[str, dict]) -> None:
    tool = listed[READ_TOOL]
    description = tool.get("description")
    assert isinstance(description, str) and description.strip(), tool
    assert len(description) <= DESCRIPTION_LIMIT, len(description)
    properties = tool["inputSchema"]["properties"]
    assert set(READ_PROPERTIES) <= set(properties), sorted(properties)
    undescribed = sorted(key for key in properties if not property_text(tool, key).strip())
    assert not undescribed, f"com_read: input properties without a description: {undescribed}"
    oversized = {key: len(property_text(tool, key)) for key in properties if len(property_text(tool, key)) > PROPERTY_DESCRIPTION_LIMIT}
    assert not oversized, f"com_read: property descriptions over {PROPERTY_DESCRIPTION_LIMIT} characters: {oversized}"


def test_com_read_port_id_is_a_configured_entry_and_not_a_device(listed: dict[str, dict]) -> None:
    """The same name com_session_start took, chosen from com_ports; an unknown
    name is refused as com_port_not_configured."""
    described = property_text(listed[READ_TOOL], "port_id")
    assert not names(described, "com_ports", "com_ports_list"), described
    assert claims(described, r"\bnot\b[^.;]*\bdevice\b"), described
    assert not names(definition_text(listed[READ_TOOL]), "com_port_not_configured"), definition_text(listed[READ_TOOL])


def test_max_bytes_names_its_meaning_its_default_and_that_the_rest_stays_buffered(listed: dict[str, dict]) -> None:
    """The most bytes one read returns, 1 or more as the schema says; without
    it, the port's max_buffer_bytes, which is everything the buffer can hold;
    with a smaller value the bytes past it stay for the next read rather than
    being lost."""
    tool = listed[READ_TOOL]
    described = property_text(tool, "max_bytes")
    assert tool["inputSchema"]["properties"]["max_bytes"].get("minimum") == 1, tool["inputSchema"]["properties"]["max_bytes"]
    assert max_bytes_is_the_most_bytes_returned(described), described
    assert max_bytes_defaults_to_max_buffer_bytes(described), described
    assert the_rest_stays_buffered(described), described


def test_wait_timeout_s_names_both_defaults_the_cap_and_its_unit(listed: dict[str, dict]) -> None:
    """The issue in one property: without until a read does not wait unless
    told to, with until it waits ten seconds unless told otherwise, and no
    wait is longer than sixty. The two defaults are tied to their condition,
    and neither is stated the other way round."""
    described = property_text(listed[READ_TOOL], "wait_timeout_s")
    assert claims(described, r"\bseconds?\b|\b\d+(?:\.\d+)?\s?s\b"), described
    assert wait_defaults_follow_until(described), described
    assert waits_are_capped_at_sixty(described), described


def test_a_wait_without_until_is_said_to_end_at_the_first_bytes(listed: dict[str, dict]) -> None:
    """A plain read with a wait returns as soon as anything is buffered, which
    may be part of a line; only until waits on for more."""
    text = definition_text(listed[READ_TOOL])
    assert a_plain_wait_ends_at_the_first_bytes(text), text


def test_until_names_how_it_matches_its_limits_and_what_it_leaves(listed: dict[str, dict]) -> None:
    """Literal text, compared as the bytes the port's encoding gives it, one or
    up to 8 of them as the schema bounds them; the answer runs through the
    first match and what follows stays buffered."""
    tool = listed[READ_TOOL]
    described = property_text(tool, "until")
    schema = tool["inputSchema"]["properties"]["until"]
    shapes = {shape["type"]: shape for shape in schema["oneOf"]}
    assert shapes["string"]["maxLength"] == UNTIL_MAX_CHARACTERS, schema
    assert (shapes["array"]["minItems"], shapes["array"]["maxItems"], shapes["array"]["items"]["maxLength"]) == (1, UNTIL_MAX_ENTRIES, UNTIL_MAX_CHARACTERS), schema
    assert one_of(clauses(described), r"\bbytes?\b", r"\bencod\w*", r"\bport'?s?\b|\bconfigured\b"), described
    assert until_is_literal_text(described), described
    assert until_limits_are_true(described), described
    assert the_answer_runs_through_the_first_match(described), described
    assert the_rest_stays_buffered(described), described


def test_a_full_max_bytes_is_said_to_end_a_wait_for_until(listed: dict[str, dict]) -> None:
    """A wait for until ends at a match or once max_bytes are buffered, which
    is all the answer could hold; the time is not the only way out."""
    text = definition_text(listed[READ_TOOL])
    assert max_bytes_also_ends_an_until_wait(text), text


def test_a_missed_until_is_said_to_be_an_ok_answer_with_until_matched_false(listed: dict[str, dict]) -> None:
    """No match before the wait ends is feedback about the board, not a failed
    call: the answer is ok, until_matched is false, and the bytes that did
    arrive are returned."""
    text = definition_text(listed[READ_TOOL])
    assert not names(text, "until_matched"), text
    assert a_missed_until_is_an_ok_answer(text), text


def test_com_read_says_what_it_needs_what_it_takes_and_what_it_returns(listed: dict[str, dict]) -> None:
    """The session com_session_start opened (else session_not_active), reading
    allowed where a version 1 configuration can withhold it (else
    permission_denied, never a matter of allow_write), each byte returned once,
    as text and as hex, with what is left counted."""
    text = definition_text(listed[READ_TOOL])
    missing = names(text, "com_session_start", "session_not_active", "permission_denied", "buffer_remaining_bytes")
    assert not missing, (missing, text)
    assert a_read_needs_a_session_from_com_session_start(text), text
    assert reading_needs_allow_read_under_version_1_only(text), text
    assert each_byte_is_returned_once(text), text
    assert one_of(clauses(text), r"\btext\b", r"\bhex\b"), text
    # A read: nothing it does reaches the board.
    assert not claims(text, r"\b(?:writes?|sends?|transmits?)\b[^.;]*\bto the (?:board|target|port|device)\b"), text
    assert listed[READ_TOOL]["annotations"]["readOnlyHint"] is True
    assert listed[READ_TOOL]["annotations"]["openWorldHint"] is False


def test_every_identifier_the_com_read_definition_names_is_real(listed: dict[str, dict]) -> None:
    com_properties = {key for name, tool in listed.items() if name.startswith("com_") for key in tool["inputSchema"]["properties"]}
    vocabulary = set(listed) | com_properties | config_vocabulary() | answer_vocabulary()
    assert {"until_matched", "buffer_remaining_bytes", "max_buffer_bytes"} <= vocabulary, "the source vocabulary is read whole"

    unknown = sorted(set(SNAKE_CASE.findall(re.sub(QUOTED, " ", definition_text(listed[READ_TOOL])))) - vocabulary)
    assert not unknown, unknown


# ---------------------------------------------------------------------------
# What com_read does, as the code answers it today.


class SimulatedWait:
    """`time` as comports.py sees it, with the calling thread's waits simulated.

    The test thread's sleeps advance `now` instead of passing, so a read that
    waits a minute is measured in a moment, and `now` is how long it waited.
    Every other thread, the session's reader among them, keeps real time.
    """

    def __init__(self) -> None:
        self.thread = threading.current_thread()
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now if threading.current_thread() is self.thread else time.monotonic()

    def sleep(self, seconds: float) -> None:
        if threading.current_thread() is self.thread:
            self.now += seconds
        else:
            time.sleep(seconds)

    def __getattr__(self, name: str) -> object:
        return getattr(time, name)


def test_com_read_needs_a_session_on_a_configured_port_and_opens_nothing(bench: SimpleNamespace) -> None:
    service, line = bench.service, bench.line

    without_session = read(service, SPARE_PORT_ID)
    unknown = read(service, UNKNOWN_PORT_ID)

    assert without_session["error_type"] == "session_not_active", without_session
    assert "com_session_start" in without_session["summary"], without_session
    assert unknown["error_type"] == "com_port_not_configured", unknown
    assert PORT_ID in unknown["configured_ports"], unknown
    assert line.handles == [], "a read never opens the port itself"


@pytest.mark.parametrize(
    ("config_version", "permissions", "reads"),
    [
        pytest.param(None, "{allow_read: false, allow_write: true}", False, id="v1-read-withheld"),
        pytest.param(None, "{allow_read: true, allow_write: true}", True, id="v1-read-granted"),
        pytest.param(2, "{allow_write: true}", True, id="v2-reading-needs-no-grant"),
    ],
)
def test_com_read_needs_reading_allowed_not_allow_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, config_version: int | None, permissions: str, reads: bool) -> None:
    """A session a write grant opened is still refused a read where reading is
    withheld, which only a version 1 configuration can do, and the refusal
    names allow_read. Such a session runs no reader, so there is nothing for
    it to buffer."""
    line = install_line(monkeypatch)
    device = READ_DEVICES[WRITE_ONLY_PORT_ID]
    com_ports_yaml = f'com_ports:\n  {WRITE_ONLY_PORT_ID}:\n    device: "{device}"\n    permissions: {permissions}\n'
    service = new_service(tmp_path / "workspace", com_ports_yaml=com_ports_yaml, config_version=config_version)
    try:
        started(service, WRITE_ONLY_PORT_ID)
        if reads:
            line.on_device(device).deliver(b"banner\r\n")

        result = read(service, WRITE_ONLY_PORT_ID)

        if reads:
            assert result["ok"] is True, result
            assert result["data"]["text"] == "banner\r\n", result
            return
        assert result["error_type"] == "permission_denied", result
        assert "allow_read" in result["summary"], result
        assert "allow_write" not in result["summary"], result
        assert write(service, WRITE_ONLY_PORT_ID, text="PING")["ok"] is True, "the session itself is usable for writing"
    finally:
        close(service)


@pytest.mark.parametrize(
    "max_buffer_bytes",
    [pytest.param(None, id="65536-by-default"), pytest.param(SMALL_MAX_BUFFER_BYTES, id="as-configured")],
)
def test_without_max_bytes_a_read_returns_up_to_the_ports_max_buffer_bytes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, max_buffer_bytes: int | None) -> None:
    """A full buffer is returned whole by one read without max_bytes. The
    buffer itself holds max_buffer_bytes at most: older bytes past that are
    dropped and counted in overflow_bytes, and the newest are what a read
    returns."""
    line = install_line(monkeypatch)
    limit = DEFAULT_MAX_BUFFER_BYTES if max_buffer_bytes is None else max_buffer_bytes
    device = READ_DEVICES[BUFFER_PORT_ID]
    setting = "" if max_buffer_bytes is None else f"    max_buffer_bytes: {max_buffer_bytes}\n"
    service = new_service(tmp_path / "workspace", com_ports_yaml=f'com_ports:\n  {BUFFER_PORT_ID}:\n    device: "{device}"\n{setting}')
    full = (b"0123456789abcdef" * (limit // 16 + 1))[:limit]
    try:
        started(service, BUFFER_PORT_ID)
        handle = line.on_device(device)

        handle.deliver(full)
        whole = read(service, BUFFER_PORT_ID)

        assert whole["bytes_read"] == limit, whole["bytes_read"]
        assert whole["data"]["hex"] == full.hex()
        assert whole["buffer_remaining_bytes"] == 0, whole["buffer_remaining_bytes"]
        assert whole["overflow_bytes"] == 0, whole["overflow_bytes"]

        handle.deliver(b"OLDER" + full)
        newest = read(service, BUFFER_PORT_ID)

        assert newest["bytes_read"] == limit, newest["bytes_read"]
        assert newest["data"]["hex"] == full.hex()
        assert newest["overflow_bytes"] == len(b"OLDER"), newest["overflow_bytes"]
    finally:
        close(service)


def test_a_read_returns_each_byte_once_oldest_first_and_max_bytes_leaves_the_rest(bench: SimpleNamespace) -> None:
    service, line = bench.service, bench.line
    started(service)
    line.handle(PORT_ID).deliver(b"0123456789")

    first = read(service, max_bytes=4)
    second = read(service)
    third = read(service)

    assert (first["data"]["text"], first["bytes_read"], first["buffer_remaining_bytes"]) == ("0123", 4, 6), first
    assert (second["data"]["text"], second["bytes_read"], second["buffer_remaining_bytes"]) == ("456789", 6, 0), second
    assert third["ok"] is True, third
    assert (third["data"]["text"], third["bytes_read"]) == ("", 0), third


@pytest.mark.parametrize(
    ("port_id", "line_bytes", "text"),
    [
        pytest.param(LATIN1_PORT_ID, b"caf\xe9\r\n", "café\r\n", id="text-in-the-ports-encoding"),
        pytest.param(PORT_ID, b"ok\xff\r\n", "ok�\r\n", id="undecodable-byte-replaced-in-text-kept-in-hex"),
    ],
)
def test_a_read_returns_the_bytes_as_hex_and_as_text_in_the_ports_encoding(bench: SimpleNamespace, port_id: str, line_bytes: bytes, text: str) -> None:
    service, line = bench.service, bench.line
    started(service, port_id)
    line.handle(port_id).deliver(line_bytes)

    result = read(service, port_id)

    encoding = "latin-1" if port_id == LATIN1_PORT_ID else "utf-8"
    assert result["data"] == {"hex": line_bytes.hex(), "text": text, "encoding": encoding}, result


@pytest.mark.parametrize(
    ("arguments", "wait_in_force_s"),
    [
        pytest.param({}, 0.0, id="no-until-returns-at-once-by-default"),
        pytest.param({"wait_timeout_s": 2.5}, 2.5, id="no-until-waits-as-asked"),
        pytest.param({"wait_timeout_s": 600}, WAIT_CAP_S, id="no-until-wait-capped-at-60s"),
        pytest.param({"until": "PASS"}, UNTIL_DEFAULT_WAIT_S, id="until-waits-10s-by-default"),
        pytest.param({"until": "PASS", "wait_timeout_s": 2.5}, 2.5, id="until-waits-as-asked"),
        pytest.param({"until": "PASS", "wait_timeout_s": 0}, 0.0, id="until-with-no-wait-returns-at-once"),
        pytest.param({"until": "PASS", "wait_timeout_s": 600}, WAIT_CAP_S, id="until-wait-capped-at-60s"),
    ],
)
def test_how_long_a_read_waits_on_a_quiet_line(bench: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, arguments: dict, wait_in_force_s: float) -> None:
    """Nothing arrives, so every read waits out the wait in force: none without
    until unless asked, ten seconds with until unless asked, and a wait asked
    past sixty seconds is cut to sixty rather than refused. The answer is an
    ok read of nothing either way."""
    service = bench.service
    started(service)
    clock = SimulatedWait()

    with monkeypatch.context() as patch:
        patch.setattr(comports, "time", clock)
        result = read(service, **arguments)

    assert result["ok"] is True, result
    assert result["bytes_read"] == 0, result
    assert wait_in_force_s <= clock.now <= wait_in_force_s + 0.02, clock.now
    if "until" in arguments:
        assert result["until_matched"] is False, result


def test_a_wait_without_until_ends_at_the_first_bytes_and_until_waits_on_for_its_match(bench: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    """The same buffered bytes end a plain read's 30 s wait at once, and do not
    end a read with until: it waits the 30 s out for a match that never comes,
    then answers ok with until_matched false and the bytes it has, which are
    gone from the buffer like any other bytes a read returned."""
    service, line = bench.service, bench.line
    started(service)
    clock = SimulatedWait()

    with monkeypatch.context() as patch:
        patch.setattr(comports, "time", clock)
        line.handle(PORT_ID).deliver(b"boot\r\n")
        plain = read(service, wait_timeout_s=30)
        plain_waited = clock.now
        line.handle(PORT_ID).deliver(b"boot\r\n")
        missed = read(service, until="PASS", wait_timeout_s=30)
        missed_waited = clock.now - plain_waited
    after_miss = read(service)

    assert plain["data"]["text"] == "boot\r\n", plain
    assert plain_waited == 0.0, plain_waited
    assert 30.0 <= missed_waited <= 30.02, missed_waited
    assert missed["ok"] is True, missed
    assert missed["until_matched"] is False, missed
    assert "matched" not in missed, missed
    assert missed["data"]["text"] == "boot\r\n", missed
    assert missed["buffer_remaining_bytes"] == 0, missed
    assert after_miss["bytes_read"] == 0, after_miss


@pytest.mark.parametrize(
    ("until", "until_matched"),
    [
        pytest.param("PASS", True, id="the-text-itself"),
        pytest.param("P.SS", False, id="a-dot-is-a-dot"),
        pytest.param("PAS+", False, id="a-plus-is-a-plus"),
        pytest.param("pass", False, id="case-counts"),
    ],
)
def test_until_is_matched_as_literal_text(bench: SimpleNamespace, until: str, until_matched: bool) -> None:
    service, line = bench.service, bench.line
    started(service)
    line.handle(PORT_ID).deliver(b"result: PASS\r\n")

    result = read(service, until=until, wait_timeout_s=0)

    assert result["ok"] is True, result
    assert result["until_matched"] is until_matched, result


def test_a_full_max_bytes_ends_a_wait_for_until_at_once(bench: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    """max_bytes buffered with no match among them ends a wait for until on the
    spot: the answer could hold no more, so waiting on could not change it. It
    is ok with until_matched false, and the bytes past max_bytes stay buffered."""
    service, line = bench.service, bench.line
    started(service)
    clock = SimulatedWait()

    with monkeypatch.context() as patch:
        patch.setattr(comports, "time", clock)
        line.handle(PORT_ID).deliver(b"0123456789")
        full = read(service, until="PASS", max_bytes=4, wait_timeout_s=30)

    assert clock.now == 0.0, clock.now
    assert full["ok"] is True, full
    assert full["until_matched"] is False, full
    assert (full["data"]["text"], full["buffer_remaining_bytes"]) == ("0123", 6), full


@pytest.mark.parametrize(("max_bytes", "refused"), [pytest.param(1, False, id="one-is-taken"), pytest.param(0, True, id="zero-is-refused")])
def test_max_bytes_is_one_or_more(bench: SimpleNamespace, max_bytes: int, refused: bool) -> None:
    service, line = bench.service, bench.line
    started(service)
    line.handle(PORT_ID).deliver(b"ok\r\n")

    result = read(service, max_bytes=max_bytes)

    if refused:
        assert (result["error_type"], result["field"]) == ("invalid_argument", "max_bytes"), result
        return
    assert (result["data"]["text"], result["buffer_remaining_bytes"]) == ("o", 3), result
