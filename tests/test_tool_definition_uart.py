"""What `com_session_start`, `com_session_stop` and `com_write` tell an agent.

The three definitions said what the tools are and little else: nothing about
which port a `port_id` names, what a repeated start does, what stop leaves
behind, whether a write adds a line ending, how large it may be, or which tool
reads the reply. These tests ask the definitions a host receives through
`tools/list` to say those things, and every claim they ask for is first shown
to be what the code does, through `tools/call`, against a recording stand-in
for pyserial. No port, adapter or board is touched.

The metadata tests check meaning, not wording: an input property describes
itself, a default or a limit is named with its value, a failure is named by the
`error_type` the result carries, and every identifier a definition names is one
this server lists, accepts, configures or answers with.
"""

from __future__ import annotations

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
from test_read_until import ScriptedSerialHandle, close, tools_call

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
}
# Every entry but the first three keeps the defaults the code applies, which
# are what the definitions are asked to name.
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
)
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
        self.refuse_close_once: set[str] = set()

    def handle(self, port_id: str) -> RecordingSerialHandle:
        """The handle most recently opened on `port_id`'s device."""
        opened = [handle for handle in self.handles if handle.port == DEVICES[port_id] and handle.opened]
        assert opened, f"no handle was opened on {port_id}"
        return opened[-1]

    def opened_on(self, port_id: str) -> int:
        return sum(1 for handle in self.handles if handle.port == DEVICES[port_id] and handle.opened)


class RecordingSerialHandle(ScriptedSerialHandle):
    """The scripted handle, recording the bytes written to it and its input resets.

    It also does three things a test asks of one device: refuse the open (the
    device is absent), refuse the first close, and stall the reader inside a
    read until the test lets it go, the way a driver that ignores
    `cancel_read` does.
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
        super().open()
        self.opened = True

    def read(self, size: int) -> bytes:
        if self.stall:
            self.stalled.set()
            self.unstall.wait(scaled_time_bound(30.0))
            return b""
        return super().read(size)

    def write(self, data: bytes) -> int:
        self.written.extend(data)
        return len(data)

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


# A negation and a line ending in one clause, in either order: "adds no line
# ending", "without a newline", "a line ending is not added".
NO_LINE_ENDING = re.compile(
    r"\b(?:no|not|never|without)\b[^.;]*\b(?:line ending|line endings|newline|terminator|CR|LF)\b"
    r"|\b(?:line ending|newline|terminator)\b[^.;]*\b(?:not|never)\b",
    re.IGNORECASE,
)
DEFAULT_TRUE = re.compile(r"\bdefaults?\b[^.;]*\btrue\b|\btrue\b[^.;]*\bdefault\b", re.IGNORECASE)
SECONDS = re.compile(r"\b\d+(?:\.\d+)?\s?s\b")
SNAKE_CASE = re.compile(r"(?<![A-Za-z0-9_])[a-z][a-z0-9]*(?:_[a-z0-9]+)+(?![A-Za-z0-9_])")


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
    assert re.search(r"\bnot\b[^.;]*\bdevice\b", described, re.IGNORECASE), described
    assert not names(definition_text(listed[name]), "com_port_not_configured"), definition_text(listed[name])


def test_clear_buffer_names_its_default_what_it_discards_and_that_it_applies_to_an_active_session(listed: dict[str, dict]) -> None:
    described = property_text(listed["com_session_start"], "clear_buffer")
    assert DEFAULT_TRUE.search(described), described
    assert re.search(r"discard|drop|clear|purge|empt", described, re.IGNORECASE), described
    assert re.search(r"receiv|unread", described, re.IGNORECASE), described
    assert re.search(r"already[ _]active|active session|existing session", described, re.IGNORECASE), described


def test_text_names_its_encoding_and_that_no_line_ending_is_added(listed: dict[str, dict]) -> None:
    described = property_text(listed["com_write"], "text")
    assert re.search(r"encod", described, re.IGNORECASE), described
    assert re.search(r"utf-?8", described, re.IGNORECASE), described
    assert NO_LINE_ENDING.search(described), described


def test_hex_names_its_digit_pairs_and_that_whitespace_is_ignored(listed: dict[str, dict]) -> None:
    described = property_text(listed["com_write"], "hex")
    assert re.search(r"\btwo\b[^.;]*\bdigits?\b|\bdigit pairs?\b|\bpairs? of\b[^.;]*\bdigits\b", described, re.IGNORECASE), described
    assert re.search(r"whitespace|spaces", described, re.IGNORECASE), described


def test_com_session_start_says_what_it_opens_holds_and_how_it_fails(listed: dict[str, dict]) -> None:
    text = definition_text(listed["com_session_start"])
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
        "DTR",
        "RTS",
    )
    assert not missing, (missing, text)
    assert re.search(r"\bbuffer", text, re.IGNORECASE), text
    assert re.search(r"\block\b", text, re.IGNORECASE), text
    assert re.search(r"\breset", text, re.IGNORECASE), text


def test_com_session_stop_says_what_it_releases_what_it_leaves_and_how_long_it_waits(listed: dict[str, dict]) -> None:
    text = definition_text(listed["com_session_stop"])
    missing = names(text, "com_session_start", "was_active", "log_path", "com_port_close_failed", "timeout_s")
    assert not missing, (missing, text)
    assert re.search(r"\breleas", text, re.IGNORECASE) and re.search(r"\block\b", text, re.IGNORECASE), text
    assert re.search(r"\bunread\b|\bnot (?:yet )?read\b", text, re.IGNORECASE), text
    assert SECONDS.search(text), text
    assert re.search(r"\bagain\b|\bretry", text, re.IGNORECASE), text


def test_com_write_says_what_it_needs_what_it_sends_and_what_it_returns(listed: dict[str, dict]) -> None:
    text = definition_text(listed["com_write"])
    missing = names(
        text,
        "com_session_start",
        "session_not_active",
        "allow_write",
        "permission_denied",
        "max_write_bytes",
        str(DEFAULT_MAX_WRITE_BYTES),
        "bytes_written",
        "com_read",
        "until",
    )
    assert not missing, (missing, text)
    assert NO_LINE_ENDING.search(text), text


def test_one_definition_carries_a_start_write_read_stop_sequence(listed: dict[str, dict]) -> None:
    """The four calls in the order an agent makes them, the read waiting with
    `until`, built from tool names alone and no device command."""

    def in_order(text: str) -> bool:
        position = 0
        for word in ("com_session_start", "com_write", "com_read", "until", "com_session_stop"):
            found = re.compile(rf"(?<![A-Za-z0-9_]){word}(?![A-Za-z0-9_])").search(text, position)
            if found is None:
                return False
            position = found.end()
        return True

    carriers = [name for name in UART_TOOLS if in_order(str(listed[name].get("description") or ""))]
    assert carriers, {name: listed[name].get("description") for name in UART_TOOLS}


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


def collected_names(value: object, into: set[str]) -> None:
    """Every key, and every `error_type` and `tool` value, anywhere in a result."""
    if isinstance(value, dict):
        for key, item in value.items():
            into.add(str(key))
            if key in {"error_type", "tool"} and isinstance(item, str):
                into.add(item)
            collected_names(item, into)
    elif isinstance(value, list):
        for item in value:
            collected_names(item, into)


def config_vocabulary() -> set[str]:
    """The keys an operator writes for a port: the entry's own and its permissions."""
    schema_path = Path(__file__).resolve().parents[1] / "src" / "agentic_hil" / "schemas" / "config.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    entry = schema["properties"]["com_ports"]["additionalProperties"]
    return {"com_ports", *entry["properties"], *schema["$defs"]["io_permissions"]["properties"]}


def test_every_identifier_the_definitions_name_is_real(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A snake_case word in any of the three definitions is a listed tool, an
    input property of a COM tool, a configuration key of a port, or a field or
    `error_type` that one of the calls below really answered with."""
    tools = listed_tools(tmp_path / "listed")
    line = install_line(monkeypatch)
    service = new_service(tmp_path / "workspace-a", state_root=tmp_path / "state-a")
    other = new_service(tmp_path / "workspace-b", state_root=tmp_path / "state-b")
    results: list[dict] = []
    try:
        line.absent.add(DEVICES[ABSENT_PORT_ID])
        results += [start(service, UNKNOWN_PORT_ID), start(service, LOCKED_PORT_ID), start(service, ABSENT_PORT_ID)]
        results += [start(service), start(service), start(other)]
        line.handle(PORT_ID).deliver(b"PONG\r\n")
        results += [write(service, text="PING\r\n"), write(service, hex="00" * (DEFAULT_MAX_WRITE_BYTES + 1))]
        results += [write(service, SPARE_PORT_ID, text="x"), started(service, LISTEN_PORT_ID), write(service, LISTEN_PORT_ID, text="x")]
        results += [read(service, until="PONG", wait_timeout_s=0)]
        line.refuse_close_once.add(DEVICES[PORT_ID])
        results += [stop(service), stop(service), stop(service), stop(service, UNKNOWN_PORT_ID)]
    finally:
        close(other)
        close(service)

    observed: set[str] = set()
    for result in results:
        collected_names(result, observed)
    error_types = {result.get("error_type") for result in results}
    # The scenario reached every outcome it is here to supply.
    for expected in ("com_port_not_configured", "permission_denied", "com_port_open_failed", "device_busy", "session_not_active", "invalid_argument", "com_port_close_failed"):
        assert expected in error_types, (expected, sorted(str(item) for item in error_types))
    com_properties = {key for name, tool in tools.items() if name.startswith("com_") for key in tool["inputSchema"]["properties"]}
    vocabulary = set(tools) | com_properties | config_vocabulary() | observed

    unknown = {name: sorted(set(SNAKE_CASE.findall(definition_text(tools[name]))) - vocabulary) for name in UART_TOOLS}
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


def test_an_unknown_port_id_is_refused_as_not_configured_by_all_three(bench: SimpleNamespace) -> None:
    for result in (start(bench.service, UNKNOWN_PORT_ID), write(bench.service, UNKNOWN_PORT_ID, text="x"), stop(bench.service, UNKNOWN_PORT_ID)):
        assert result["error_type"] == "com_port_not_configured", result
        assert PORT_ID in result["configured_ports"], result
    assert bench.line.handles == []


def test_com_session_start_without_read_or_write_permission_is_refused(bench: SimpleNamespace) -> None:
    refused = start(bench.service, LOCKED_PORT_ID)

    assert refused["error_type"] == "permission_denied", refused
    assert bench.line.handles == []


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
    assert elapsed < bound + scaled_time_bound(5.0), elapsed
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


def test_com_write_refuses_more_than_max_write_bytes_which_is_4096_by_default(bench: SimpleNamespace) -> None:
    service, line = bench.service, bench.line
    started(service)

    at_limit = write(service, hex="41" * DEFAULT_MAX_WRITE_BYTES)
    over = write(service, hex="41" * (DEFAULT_MAX_WRITE_BYTES + 1))

    assert at_limit["bytes_written"] == DEFAULT_MAX_WRITE_BYTES, at_limit
    assert over["error_type"] == "invalid_argument", over
    assert over["max_write_bytes"] == DEFAULT_MAX_WRITE_BYTES, over
    assert over["bytes_requested"] == DEFAULT_MAX_WRITE_BYTES + 1, over
    assert len(line.handle(PORT_ID).written) == DEFAULT_MAX_WRITE_BYTES


def test_com_write_needs_a_session_from_com_session_start(bench: SimpleNamespace) -> None:
    refused = write(bench.service, SPARE_PORT_ID, text="PING\r\n")

    assert refused["error_type"] == "session_not_active", refused
    assert "com_session_start" in refused["summary"], refused
    assert bench.line.handles == [], "a write never opens the port itself"


def test_com_write_needs_allow_write_even_with_a_session(bench: SimpleNamespace) -> None:
    service, line = bench.service, bench.line
    started(service, LISTEN_PORT_ID)

    refused = write(service, LISTEN_PORT_ID, text="PING\r\n")

    assert refused["error_type"] == "permission_denied", refused
    assert "allow_write" in refused["summary"], refused
    assert bytes(line.handle(LISTEN_PORT_ID).written) == b""


def test_a_reply_that_arrives_before_com_read_is_buffered_for_it(bench: SimpleNamespace) -> None:
    """The documented sequence, end to end: start, write, read with `until`,
    stop. The reply reaches the session before `com_read` is called and is
    still handed out, through the match, by that call."""
    service, line = bench.service, bench.line
    started(service)

    assert write(service, text="PING\r\n")["ok"] is True
    line.handle(PORT_ID).deliver(b"PONG\r\nidle\r\n")
    received = read(service, until="PONG", wait_timeout_s=0)
    stopped = stop(service)

    assert received["ok"] is True, received
    assert received["until_matched"] is True, received
    assert received["data"]["text"] == "PONG", received
    assert stopped["was_active"] is True, stopped
