"""Every refusal a COM tool answers with has an entry in the error catalogue (#635).

AGENTS.md describes `agentic-hil://reference/errors` as listing every
`error_type` with its meaning, its ordered fix and the wrong fix. Most of what
`com_ports_list`, `com_session_start`, `com_session_stop`, `com_write` and
`com_read` refuse with had no entry there: the reference resolved to nothing,
and the refusal carried no standing fix.

Three things are held here. The inventory is read off the source, so a refusal
added later without an entry fails the guard, and it is pinned as well, so the
scan cannot shrink unnoticed. Every type in it resolves at its URI. And every
tool path, driven through `tools/call` against a scripted stand-in for pyserial,
hands the entry's steps out in the refusal itself. No port, adapter or board is
touched.

`session_not_active` is one bare entry for three kinds of session (COM, CAN and
the debug session), so it is also checked against every place that returns it.
"""

from __future__ import annotations

import ast
import json
import re
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
from conftest import FAKE_GDB, write_config
from test_read_until import ScriptedSerialHandle, close, tools_call

import agentic_hil
from agentic_hil import comports, readuntil
from agentic_hil.config import load_config
from agentic_hil.knowledge import ERROR_CATALOGUE, ERROR_URI_PREFIX, catalogue_entry, remediation_fields
from agentic_hil.mcp import handle_mcp_message
from agentic_hil.tools import AgenticHILToolService

# The modules whose results the COM tools hand back. `readuntil` builds the
# refusals of `com_read`'s `until` argument.
COM_MODULES: tuple[ModuleType, ...] = (comports, readuntil)

# Every error_type a COM tool answers with at the top level of a result.
# `com_port_discovery_failed` is the top level of the host inventory, which
# `com_ports_list` carries whole under `available_com_ports` and
# `agentic-hil com-ports` returns as it is; `serial_backend_not_available` is
# both that and a refusal of `com_session_start`.
COM_INVENTORY = frozenset(
    {
        "com_buffer_clear_failed",
        "com_port_busy",
        "com_port_close_failed",
        "com_port_discovery_failed",
        "com_port_identity_mismatch",
        "com_port_identity_unverified",
        "com_port_not_bound",
        "com_port_not_configured",
        "com_port_open_failed",
        "com_reader_start_failed",
        "config_invalid",
        "invalid_argument",
        "permission_denied",
        "resource_quarantined",
        "serial_backend_not_available",
        "serial_write_failed",
        "serial_write_incomplete",
        "session_not_active",
    }
)

# error_type values the scan finds that no COM tool answers with at the top
# level. Each is the reader's own failure, kept on the session and handed out
# under `reader_error`; the call that meets it answers with the type it nests in.
NESTED_ONLY = {
    "serial_read_failed": "the reader died; nested in a session_not_active refusal, a com_read result and a port's status in com_ports_list",
    "audit_write_failed": "received bytes could not be logged; the session is quarantined and the tools answer resource_quarantined",
}

# The start tool each kind of session names in its own `session_not_active`
# summary, by the file that returns it.
SESSION_NOT_ACTIVE_SITES = {
    "backends/gdbdebug.py": ("debug_start_session",),
    "can.py": ("can_session_start",),
    "comports.py": ("com_session_start",),
}
START_TOOL = re.compile(r"\b[a-z]+_(?:session_start|start_session)\b")

# Port ids and devices of this module alone: device locks are machine-wide.
PORT_ID = "catalogue_com"
DECLARED_PORT_ID = "catalogue_com_declared"
UNKNOWN_PORT_ID = "catalogue_com_nowhere"
DEVICES = {PORT_ID: "/dev/ttyCATCOM0", DECLARED_PORT_ID: "/dev/ttyCATCOM1"}
# A serial no host in this test enumerates, so the declared port cannot be
# proved to reach its board.
DECLARED_SERIAL = "CATALOGUECOMTESTSERIAL"
COM_PORTS_YAML = (
    "com_ports:\n"
    f"  {PORT_ID}:\n"
    f'    device: "{DEVICES[PORT_ID]}"\n'
    f"  {DECLARED_PORT_ID}:\n"
    f'    device: "{DEVICES[DECLARED_PORT_ID]}"\n'
    f'    serial_number: "{DECLARED_SERIAL}"\n'
)
CAN_BUS_ID = "catalogue_bus"
CAN_BUSES_YAML = f'can_buses:\n  {CAN_BUS_ID}:\n    adapter: "socketcan"\n    channel: "catalogue-com-635"\n'


# ---------------------------------------------------------------------------
# Reading the inventory off the source.


def _resolve(module: ModuleType, value: ast.expr) -> str | None:
    """The string `value` evaluates to in `module`, or None when it is not one this can read."""
    if isinstance(value, ast.Constant) and isinstance(value.value, str):
        return value.value
    if isinstance(value, ast.Name):
        named = getattr(module, value.id, None)
        return named if isinstance(named, str) else None
    return None


def error_type_values(module: ModuleType) -> dict[str, list[int]]:
    """Every `error_type` value `module` writes, with the lines it writes it on.

    A literal is read as written and a name through the module, the way
    `COM_PORT_BUSY_ERROR` reaches its result. A value that is neither cannot be
    checked against the catalogue, so it fails the scan instead of being skipped.
    """
    path = Path(str(module.__file__))
    found: dict[str, list[int]] = {}
    unreadable: list[str] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        values: list[ast.expr] = []
        if isinstance(node, ast.Dict):
            values = [value for key, value in zip(node.keys, node.values, strict=True) if isinstance(key, ast.Constant) and key.value == "error_type"]
        elif isinstance(node, ast.keyword) and node.arg == "error_type":
            values = [node.value]
        elif isinstance(node, ast.Assign):
            values = [node.value for target in node.targets if isinstance(target, ast.Subscript) and isinstance(target.slice, ast.Constant) and target.slice.value == "error_type"]
        for value in values:
            resolved = _resolve(module, value)
            if resolved is None:
                unreadable.append(f"{path.name}:{value.lineno}: {ast.unparse(value)}")
            else:
                found.setdefault(resolved, []).append(value.lineno)
    assert not unreadable, f"error_type values the scan cannot read: {unreadable}"
    return found


def scanned_com_error_types() -> dict[str, list[str]]:
    sites: dict[str, list[str]] = {}
    for module in COM_MODULES:
        name = Path(str(module.__file__)).name
        for error_type, lines in error_type_values(module).items():
            sites.setdefault(error_type, []).extend(f"{name}:{line}" for line in lines)
    return sites


def session_not_active_sites() -> list[tuple[str, int, tuple[str, ...]]]:
    """Every result in the package that answers `session_not_active`, with the start tool its summary names."""
    package = Path(str(agentic_hil.__file__)).parent
    sites: list[tuple[str, int, tuple[str, ...]]] = []
    for path in sorted(package.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Dict):
                continue
            fields = {key.value: value for key, value in zip(node.keys, node.values, strict=True) if isinstance(key, ast.Constant)}
            error_type = fields.get("error_type")
            if not (isinstance(error_type, ast.Constant) and error_type.value == "session_not_active"):
                continue
            summary = fields.get("summary")
            text = summary.value if isinstance(summary, ast.Constant) and isinstance(summary.value, str) else ""
            sites.append((path.relative_to(package).as_posix(), node.lineno, tuple(START_TOOL.findall(text))))
    return sites


# ---------------------------------------------------------------------------
# The guard.


def test_the_scan_finds_exactly_the_pinned_com_inventory() -> None:
    """A type the code gained must be sorted into the inventory or the nested
    set by hand, and a type the scan stopped seeing must be taken out by hand,
    so neither the code nor the scan can move the inventory on its own."""
    scanned = scanned_com_error_types()
    pinned = COM_INVENTORY | NESTED_ONLY.keys()

    assert not COM_INVENTORY & NESTED_ONLY.keys()
    assert sorted(set(scanned) - pinned) == [], {error_type: scanned[error_type] for error_type in set(scanned) - pinned}
    assert sorted(pinned - set(scanned)) == []


def test_every_com_error_type_has_a_catalogue_entry() -> None:
    missing = sorted(error_type for error_type in COM_INVENTORY if error_type not in ERROR_CATALOGUE)

    assert missing == [], f"COM error types without an ERROR_CATALOGUE entry: {missing}"


# ---------------------------------------------------------------------------
# Resolution at the URI.


@pytest.fixture
def reference(tmp_path: Path) -> Iterator[AgenticHILToolService]:
    service = AgenticHILToolService(load_config(str(write_config(tmp_path / "reference"))), frontend="mcp")
    try:
        yield service
    finally:
        close(service)


@pytest.mark.parametrize("error_type", sorted(COM_INVENTORY))
def test_the_reference_resolves_every_com_error_type(reference: AgenticHILToolService, error_type: str) -> None:
    uri = ERROR_URI_PREFIX + error_type
    response = handle_mcp_message({"jsonrpc": "2.0", "id": 1, "method": "resources/read", "params": {"uri": uri}}, reference)

    assert isinstance(response, dict), response
    assert "error" not in response, response
    contents = response["result"]["contents"]
    assert [content["uri"] for content in contents] == [uri]
    entry = json.loads(contents[0]["text"])
    assert entry == catalogue_entry(error_type)
    assert entry["error_type"] == error_type
    assert entry["meaning"].strip(), entry
    assert entry["remediation"], entry
    assert entry.get("do_not"), entry


# ---------------------------------------------------------------------------
# The same entry, in the refusal a tool hands out.


class Line:
    """What each device is scripted to do, for the handles opened on it."""

    def __init__(self) -> None:
        self.absent: set[str] = set()
        self.refuse_close_once: set[str] = set()
        self.refuse_input_reset: set[str] = set()
        self.write_dies: set[str] = set()
        # How many bytes in all a device takes before every further write
        # confirms none, the way pyserial reports a write the line did not take.
        self.write_budget: dict[str, int] = {}


class LineHandle(ScriptedSerialHandle):
    def __init__(self, line: Line) -> None:
        super().__init__()
        self.line = line

    def open(self) -> None:
        if self.port in self.line.absent:
            raise OSError(f"could not open port {self.port}: no such device")
        super().open()

    def write(self, data: bytes) -> int:
        if self.port in self.line.write_dies:
            raise OSError("write died mid-line")
        if self.port not in self.line.write_budget:
            return len(data)
        accepted = min(len(data), self.line.write_budget[self.port])
        self.line.write_budget[self.port] -= accepted
        return accepted

    def reset_input_buffer(self) -> None:
        if self.port in self.line.refuse_input_reset:
            raise OSError("input reset refused by the driver")
        super().reset_input_buffer()

    def close(self) -> None:
        if self.port in self.line.refuse_close_once:
            self.line.refuse_close_once.discard(self.port)
            raise OSError("close refused by the driver")
        super().close()


@pytest.fixture
def bench(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[SimpleNamespace]:
    """A server on this module's ports. pyserial is left as it is until a case asks for the line."""
    service = AgenticHILToolService(load_config(str(write_config(tmp_path / "workspace", com_ports_yaml=COM_PORTS_YAML))), frontend="mcp")
    try:
        yield SimpleNamespace(service=service, monkeypatch=monkeypatch, line=Line())
    finally:
        close(service)


def call(service: AgenticHILToolService, name: str, arguments: dict) -> dict:
    """One `tools/call`, answered with the structured result an agent acts on."""
    response = handle_mcp_message(tools_call(1, name, arguments), service)
    assert isinstance(response, dict) and "result" in response, response
    structured = response["result"]["structuredContent"]
    assert response["result"]["isError"] is (structured.get("ok") is not True), response
    return structured


def on_the_line(bench: SimpleNamespace) -> Line:
    bench.monkeypatch.setitem(sys.modules, "serial", SimpleNamespace(Serial=lambda *args, **kwargs: LineHandle(bench.line)))
    return bench.line


def without_pyserial(bench: SimpleNamespace) -> None:
    for name in ("serial", "serial.tools", "serial.tools.list_ports"):
        bench.monkeypatch.setitem(sys.modules, name, None)


def started(bench: SimpleNamespace) -> None:
    on_the_line(bench)
    result = call(bench.service, "com_session_start", {"port_id": PORT_ID})
    assert result["ok"] is True, result


def listing_whose_enumeration_fails(bench: SimpleNamespace) -> dict:
    def enumeration_fails() -> list[object]:
        raise OSError("the host refused the port enumeration")

    bench.monkeypatch.setattr("serial.tools.list_ports.comports", enumeration_fails)
    listed = call(bench.service, "com_ports_list", {})
    assert listed["ok"] is True, listed
    return listed["available_com_ports"]


def listing_without_pyserial(bench: SimpleNamespace) -> dict:
    without_pyserial(bench)
    listed = call(bench.service, "com_ports_list", {})
    assert listed["ok"] is True, listed
    return listed["available_com_ports"]


def start_of_an_unconfigured_port(bench: SimpleNamespace) -> dict:
    return call(bench.service, "com_session_start", {"port_id": UNKNOWN_PORT_ID})


def start_without_pyserial(bench: SimpleNamespace) -> dict:
    without_pyserial(bench)
    return call(bench.service, "com_session_start", {"port_id": PORT_ID})


def start_of_an_absent_device(bench: SimpleNamespace) -> dict:
    on_the_line(bench).absent.add(DEVICES[PORT_ID])
    return call(bench.service, "com_session_start", {"port_id": PORT_ID})


def start_of_a_declared_port_the_host_does_not_list(bench: SimpleNamespace) -> dict:
    bench.monkeypatch.setattr("serial.tools.list_ports.comports", lambda: [])
    return call(bench.service, "com_session_start", {"port_id": DECLARED_PORT_ID})


def start_whose_reader_will_not_start(bench: SimpleNamespace) -> dict:
    def reader_refuses(self: comports.ComPortSession) -> None:
        raise RuntimeError("the reader thread could not be started")

    on_the_line(bench)
    bench.monkeypatch.setattr(comports.ComPortSession, "start_reader", reader_refuses)
    return call(bench.service, "com_session_start", {"port_id": PORT_ID})


def start_whose_input_reset_fails(bench: SimpleNamespace) -> dict:
    on_the_line(bench).refuse_input_reset.add(DEVICES[PORT_ID])
    return call(bench.service, "com_session_start", {"port_id": PORT_ID, "clear_buffer": True})


def stop_whose_close_is_refused(bench: SimpleNamespace) -> dict:
    started(bench)
    bench.line.refuse_close_once.add(DEVICES[PORT_ID])
    return call(bench.service, "com_session_stop", {"port_id": PORT_ID})


def write_without_a_session(bench: SimpleNamespace) -> dict:
    return call(bench.service, "com_write", {"port_id": PORT_ID, "text": "ping\n"})


def write_that_dies_on_the_line(bench: SimpleNamespace) -> dict:
    started(bench)
    bench.line.write_dies.add(DEVICES[PORT_ID])
    return call(bench.service, "com_write", {"port_id": PORT_ID, "text": "ping\n"})


def write_the_line_takes_only_part_of(bench: SimpleNamespace) -> dict:
    started(bench)
    bench.line.write_budget[DEVICES[PORT_ID]] = 2
    return call(bench.service, "com_write", {"port_id": PORT_ID, "text": "ping\n"})


def read_without_a_session(bench: SimpleNamespace) -> dict:
    return call(bench.service, "com_read", {"port_id": PORT_ID})


REFUSALS = [
    pytest.param(listing_whose_enumeration_fails, "com_port_discovery_failed", id="com_ports_list-com_port_discovery_failed"),
    pytest.param(listing_without_pyserial, "serial_backend_not_available", id="com_ports_list-serial_backend_not_available"),
    pytest.param(start_of_an_unconfigured_port, "com_port_not_configured", id="com_session_start-com_port_not_configured"),
    pytest.param(start_without_pyserial, "serial_backend_not_available", id="com_session_start-serial_backend_not_available"),
    pytest.param(start_of_an_absent_device, "com_port_open_failed", id="com_session_start-com_port_open_failed"),
    pytest.param(start_of_a_declared_port_the_host_does_not_list, "com_port_identity_unverified", id="com_session_start-com_port_identity_unverified"),
    pytest.param(start_whose_reader_will_not_start, "com_reader_start_failed", id="com_session_start-com_reader_start_failed"),
    pytest.param(start_whose_input_reset_fails, "com_buffer_clear_failed", id="com_session_start-com_buffer_clear_failed"),
    pytest.param(stop_whose_close_is_refused, "com_port_close_failed", id="com_session_stop-com_port_close_failed"),
    pytest.param(write_without_a_session, "session_not_active", id="com_write-session_not_active"),
    pytest.param(write_that_dies_on_the_line, "serial_write_failed", id="com_write-serial_write_failed"),
    pytest.param(write_the_line_takes_only_part_of, "serial_write_incomplete", id="com_write-serial_write_incomplete"),
    pytest.param(read_without_a_session, "session_not_active", id="com_read-session_not_active"),
]


@pytest.mark.parametrize(("provoke", "error_type"), REFUSALS)
def test_a_com_refusal_carries_its_catalogue_entry(bench: SimpleNamespace, provoke, error_type: str) -> None:
    """`com_ports_list` itself never refuses: the host inventory it carries is
    where its refusals are, so that is the result checked for it."""
    refusal = provoke(bench)

    assert refusal["ok"] is False, refusal
    assert refusal["error_type"] == error_type, refusal
    advice = remediation_fields(error_type)
    assert advice, f"the catalogue has no entry for {error_type}"
    assert refusal.get("remediation") == advice["remediation"], refusal
    assert refusal.get("do_not") == advice.get("do_not"), refusal


def test_the_refusals_reach_every_com_type_but_those_catalogued_before() -> None:
    """The refusals above reach every COM type that had no entry before #635,
    so each of those entries is shown arriving in a payload, not only in the
    reference."""
    provoked = {param.values[1] for param in REFUSALS}

    assert sorted(COM_INVENTORY - provoked) == sorted(
        {
            "com_port_busy",
            "com_port_identity_mismatch",
            "com_port_not_bound",
            "config_invalid",
            "invalid_argument",
            "permission_denied",
            "resource_quarantined",
        }
    )


# ---------------------------------------------------------------------------
# session_not_active: one entry for every kind of session.


def test_session_not_active_is_answered_by_the_three_kinds_of_session() -> None:
    sites = session_not_active_sites()

    assert {path: tools for path, _, tools in sites} == SESSION_NOT_ACTIVE_SITES, sites
    assert len(sites) == len(SESSION_NOT_ACTIVE_SITES), sites


def test_the_session_not_active_entry_names_each_kind_of_session_its_own_start_tool() -> None:
    entry = catalogue_entry("session_not_active")

    assert entry is not None, "the catalogue has no entry for session_not_active"
    steps = " ".join(entry["remediation"])
    for tools in SESSION_NOT_ACTIVE_SITES.values():
        for tool in tools:
            assert re.search(rf"(?<![A-Za-z0-9_]){tool}(?![A-Za-z0-9_])", steps), (tool, entry["remediation"])


def test_a_debug_tool_without_a_session_carries_the_session_not_active_entry(tmp_path: Path) -> None:
    service = AgenticHILToolService(load_config(str(write_config(tmp_path / "debug", gdb_executable=FAKE_GDB))), frontend="mcp")
    try:
        refusal = call(service, "debug_get_stop_reason", {})
    finally:
        close(service)

    assert refusal["error_type"] == "session_not_active", refusal
    advice = remediation_fields("session_not_active")
    assert advice, "the catalogue has no entry for session_not_active"
    assert refusal.get("remediation") == advice["remediation"], refusal
    assert refusal.get("do_not") == advice.get("do_not"), refusal


def test_a_can_tool_without_a_session_resolves_to_the_session_not_active_entry(tmp_path: Path) -> None:
    """The CAN refusal resolves to the same entry. Whether can.py hands the
    entry out in the payload is the CAN tools' own refusal set."""
    service = AgenticHILToolService(load_config(str(write_config(tmp_path / "can", can_buses_yaml=CAN_BUSES_YAML))), frontend="mcp")
    try:
        refusal = call(service, "can_read", {"bus_id": CAN_BUS_ID})
    finally:
        close(service)

    assert refusal["error_type"] == "session_not_active", refusal
    advice = remediation_fields(refusal["error_type"])
    assert advice, "the catalogue has no entry for session_not_active"
    assert "can_session_start" in " ".join(advice["remediation"]), advice
