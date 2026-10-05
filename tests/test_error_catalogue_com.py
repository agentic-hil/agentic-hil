"""Every refusal a COM tool answers with has an entry in the error catalogue (#635).

AGENTS.md describes `agentic-hil://reference/errors` as listing every
`error_type` with its meaning, its ordered fix and the wrong fix. Most of what
`com_ports_list`, `com_session_start`, `com_session_stop`, `com_write` and
`com_read` refuse with had no entry there: the reference resolved to nothing,
and the refusal carried no standing fix.

Four things are held here. The inventory is read off the source, so a refusal
added later without an entry fails the guard, and it is pinned as well, so the
scan cannot shrink unnoticed; the scan itself is shown to see every way the
code writes an `error_type`, and to refuse the ways it cannot read. Every type
in it resolves at its URI. Every tool path, driven through `tools/call` against
a scripted stand-in for pyserial, hands the entry's steps out in the refusal
itself. And each new entry says what the code does, checked one sentence at a
time: a relation between two facts, never the mere presence of a word, which an
entry saying the opposite would also pass. No port, adapter or board is touched.

Three groups of types reach a COM caller besides the ones the COM modules
return at the top level. The reader's own failure is kept on the session and
handed out nested under `reader_error`. `com_session_start` forwards the
coordinator's refusal whole when it cannot take the port, so the types the
coordinator raises are derived from its source as well. And `session_not_active`
is one bare entry for three kinds of session (COM, CAN and the debug session),
so it is checked against every place that returns it. The debug session returns
it from two of those places, and one of them sends the caller to stop the
session rather than to start one, so the places are catalogued per site.
"""

from __future__ import annotations

import ast
import inspect
import json
import re
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
from conftest import FAKE_GDB, write_config
from support import scaled_time_bound
from test_read_until import DIE, ScriptedSerialHandle, close, tools_call

import agentic_hil
from agentic_hil import bench as bench_module
from agentic_hil import comports, coordination, readuntil
from agentic_hil.config import load_config
from agentic_hil.knowledge import (
    ERROR_CATALOGUE,
    ERROR_URI_PREFIX,
    QUARANTINE_REASON_GUIDES,
    catalogue_entry,
    remediation_fields,
)
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
        "session_lease_held",
        "session_not_active",
    }
)

# error_type values the scan finds that no COM tool answers with at the top
# level. Each is the reader's own failure, kept on the session and handed out
# under `reader_error`; the call that meets it answers with the type it nests
# in. A caller still reads them as an error_type, so each needs an entry.
COM_NESTED = {
    "serial_read_failed": "the reader died; nested in a session_not_active refusal, a com_read result and a port's status in com_ports_list",
    "audit_write_failed": "received bytes could not be logged; the session is quarantined and the tools answer resource_quarantined",
}

# error_type values `com_session_start` forwards from `HardwareCoordinator.acquire`
# when it cannot take the port, besides those owned elsewhere below. Each
# already had an entry; what is held here is that it resolves.
COM_FORWARDED = {
    "device_busy": "the device mutex: another owner holds the port's device",
    "undeclared_device": "an open run did not declare the port",
    "resource_quarantined": "this owner's incident stands",
}

# Shared types a COM tool can also answer with, owned by #645/#646: their
# entries are written there, and the integration adds one whole-catalogue
# check at the end. Kept out of the COM inventory and named here so the reason
# is visible.
OWNED_ELSEWHERE = {
    "#645": frozenset({"audit_unavailable", "service_closed", "service_cleanup_required", "hardware_action_exception", "audit_failed_after_action", "unknown_tool"}),
    "#646": frozenset({"resource_busy", "coordination_closed", "coordination_state_invalid"}),
}

# Every type this module requires an entry for.
COM_ERROR_TYPES = COM_INVENTORY | COM_NESTED.keys() | COM_FORWARDED.keys()

# The entries that existed before #635. Every other type above is new, and
# each new one carries the claims checked further down.
CATALOGUED_BEFORE = frozenset(
    {
        "com_port_busy",
        "com_port_identity_mismatch",
        "com_port_not_bound",
        "config_invalid",
        "device_busy",
        "invalid_argument",
        "permission_denied",
        "resource_quarantined",
        "undeclared_device",
    }
)
NEW_ENTRIES = COM_ERROR_TYPES - CATALOGUED_BEFORE

# The session tool each `session_not_active` summary names, by the file that
# returns it and once for every site in that file.
#
# Three kinds of session answer this type, and the debug session answers it
# from two sites that send a caller to different tools. A session that ended in
# an error is still registered, so the way on is `debug_stop_session`: nothing
# runs on it until it is stopped, and the start it would otherwise be told to
# make is the call that session refuses. A session that was never started, or
# that has been stopped, is told to start one. Both are genuine producers, so
# each is named here with the tool it directs the caller to, rather than one
# tuple standing for a whole file.
SESSION_NOT_ACTIVE_SITES = {
    "backends/gdbdebug.py": (("debug_stop_session",), ("debug_start_session",)),
    "can.py": (("can_session_start",),),
    "comports.py": (("com_session_start",),),
}
# Widened from the start tools alone for the error-ended debug session, whose
# summary names a stop. Still read off the summary the code writes, so a site
# that names no session tool, or another kind's, is a drift the catalogue below
# fails on.
SESSION_TOOL = re.compile(r"\b[a-z]+_(?:session_start|session_stop|start_session|stop_session)\b")

# Port ids and devices of this module alone: device locks are machine-wide.
PORT_ID = "catalogue_com"
DECLARED_PORT_ID = "catalogue_com_declared"
UNBOUND_PORT_ID = "catalogue_com_unbound"
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
    f"  {UNBOUND_PORT_ID}:\n"
    "    device: null\n"
)
CAN_BUS_ID = "catalogue_bus"
CAN_BUSES_YAML = f'can_buses:\n  {CAN_BUS_ID}:\n    adapter: "socketcan"\n    channel: "catalogue-com-635"\n'


# ---------------------------------------------------------------------------
# Reading the error types off the source.


@dataclass
class Scan:
    """Every `error_type` a piece of source writes, and every use of the key it could not read."""

    found: dict[str, list[str]] = field(default_factory=dict)
    unsupported: list[str] = field(default_factory=list)

    def merge(self, other: Scan) -> None:
        for error_type, sites in other.found.items():
            self.found.setdefault(error_type, []).extend(sites)
        self.unsupported.extend(other.unsupported)


def _resolve(namespace: object, value: ast.expr) -> str | None:
    """The string `value` evaluates to in `namespace`, or None when it is not one this can read."""
    if isinstance(value, ast.Constant) and isinstance(value.value, str):
        return value.value
    if isinstance(value, ast.Name):
        named = getattr(namespace, value.id, None)
        return named if isinstance(named, str) else None
    return None


def _classify(key: ast.Constant, parents: dict[ast.AST, ast.AST]) -> tuple[str, ast.expr | str | None]:
    """What one `"error_type"` literal does: `write` with the value it writes,
    `read` for a lookup that writes nothing, or `unsupported` with the form.

    Every use is sorted, so a way of writing the key this does not know is a
    failure rather than a refusal the guard never saw."""
    parent = parents.get(key)
    if isinstance(parent, ast.Dict):
        for dict_key, dict_value in zip(parent.keys, parent.values, strict=True):
            if dict_key is key:
                return "write", dict_value
        return "unsupported", "the key as a dict value"
    if isinstance(parent, ast.Subscript) and parent.slice is key:
        if isinstance(parent.ctx, (ast.Load, ast.Del)):
            return "read", None
        statement = parents.get(parent)
        if isinstance(statement, ast.Assign) and parent in statement.targets:
            return "write", statement.value
        if isinstance(statement, ast.AnnAssign) and statement.target is parent and statement.value is not None:
            return "write", statement.value
        return "unsupported", "a subscript store with no value this scan reads"
    if isinstance(parent, ast.Tuple) and len(parent.elts) == 2 and parent.elts[0] is key:
        holder = parents.get(parent)
        if isinstance(holder, (ast.List, ast.Tuple, ast.Set)) or (isinstance(holder, ast.Call) and parent in holder.args):
            # A (key, value) pair, the shape `dict(...)` and `.update(...)` take.
            return "write", parent.elts[1]
    if isinstance(parent, ast.Call) and key in parent.args:
        method = parent.func.attr if isinstance(parent.func, ast.Attribute) else None
        if method in {"get", "pop"}:
            return "read", None
        if method == "setdefault" and parent.args[0] is key and len(parent.args) == 2:
            return "write", parent.args[1]
        return "unsupported", "the key handed to a call"
    if isinstance(parent, ast.Compare):
        return "read", None
    if isinstance(parent, (ast.Set, ast.List, ast.Tuple)):
        # A field name among others, as in `key in {"error_type", "summary"}`.
        return "read", None
    return "unsupported", "a use of the key this scan does not classify"


def scan_error_types(tree: ast.AST, namespace: object, label: str) -> Scan:
    """Every `error_type` value `tree` writes, read in `namespace`.

    A literal is read as written and a name through the namespace, the way
    `COM_PORT_BUSY_ERROR` reaches its result. A value that is neither cannot be
    checked against the catalogue, so it is reported instead of being skipped,
    and so is a use of the key in a form this does not read."""
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    scan = Scan()

    def write(value: ast.expr) -> None:
        resolved = _resolve(namespace, value)
        if resolved is None:
            scan.unsupported.append(f"{label}:{value.lineno}: unreadable value {ast.unparse(value)}")
        else:
            scan.found.setdefault(resolved, []).append(f"{label}:{value.lineno}")

    for node in ast.walk(tree):
        if isinstance(node, ast.keyword) and node.arg == "error_type":
            write(node.value)
        elif isinstance(node, ast.Constant) and node.value == "error_type":
            kind, detail = _classify(node, parents)
            if kind == "write":
                assert isinstance(detail, ast.expr)
                write(detail)
            elif kind == "unsupported":
                scan.unsupported.append(f"{label}:{node.lineno}: {detail}: {ast.unparse(parents.get(node, node))}")
    return scan


def com_scan(sources: dict[ModuleType, str] | None = None) -> Scan:
    """The COM modules' scan, with `sources` standing in for a module's file where given."""
    scan = Scan()
    for module in COM_MODULES:
        path = Path(str(module.__file__))
        source = (sources or {}).get(module) or path.read_text(encoding="utf-8")
        scan.merge(scan_error_types(ast.parse(source), module, path.name))
    return scan


def inventory_drift(scan: Scan) -> tuple[list[str], list[str]]:
    """The types the scan found that are not pinned, and the pinned ones it did not find."""
    pinned = COM_INVENTORY | COM_NESTED.keys()
    return sorted(set(scan.found) - pinned), sorted(pinned - set(scan.found))


def session_not_active_sites() -> list[tuple[str, int, tuple[str, ...]]]:
    """Every result in the package that answers `session_not_active`, with the session tool its summary names."""
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
            sites.append((path.relative_to(package).as_posix(), node.lineno, tuple(SESSION_TOOL.findall(text))))
    return sites


# ---------------------------------------------------------------------------
# The guard.


def test_the_scan_finds_exactly_the_pinned_com_inventory() -> None:
    """A type the code gained must be sorted into the inventory or the nested
    set by hand, and a type the scan stopped seeing must be taken out by hand,
    so neither the code nor the scan can move the inventory on its own."""
    scan = com_scan()
    unpinned, unseen = inventory_drift(scan)

    assert scan.unsupported == [], scan.unsupported
    assert not COM_INVENTORY & COM_NESTED.keys()
    assert unpinned == [], {error_type: scan.found[error_type] for error_type in unpinned}
    assert unseen == []


def test_the_types_owned_elsewhere_stay_out_of_the_com_inventory() -> None:
    owned = OWNED_ELSEWHERE["#645"] | OWNED_ELSEWHERE["#646"]

    assert not owned & COM_ERROR_TYPES
    assert not owned & set(com_scan().found)


@pytest.mark.parametrize("error_type", sorted(COM_ERROR_TYPES))
def test_every_com_error_type_has_a_catalogue_entry(error_type: str) -> None:
    assert error_type in ERROR_CATALOGUE, f"the catalogue has no entry for {error_type}"


# Each is appended to comports.py as it stands. A refusal written any of these
# ways is one more error_type, and the guard must see it.
WRITES = {
    "a pair list handed to update": 'result.update([("error_type", "catalogue_mutation")])',
    "a pair tuple handed to dict": 'refusal = dict((("error_type", "catalogue_mutation"),))',
    "an annotated subscript assignment": 'result["error_type"]: str = "catalogue_mutation"',
    "a subscript assignment": 'result["error_type"] = "catalogue_mutation"',
    "a dict display": 'refusal = {"error_type": "catalogue_mutation"}',
    "a keyword": 'refusal = dict(error_type="catalogue_mutation")',
    "setdefault": 'result.setdefault("error_type", "catalogue_mutation")',
}

# Each writes the key in a way the scan does not read, so it must say so
# rather than pass over it.
UNREADABLE_WRITES = {
    "__setitem__": 'result.__setitem__("error_type", "catalogue_mutation")',
    "an augmented assignment": 'result["error_type"] += "_mutation"',
    "an annotation without a value": 'result["error_type"]: str',
    "a loop target": 'for result["error_type"] in names:\n    pass',
    "the key as a dict value": 'fields = {"key": "error_type"}',
    "a value built at run time": 'refusal = {"error_type": "catalogue_" + suffix}',
}


def _appended(snippet: str) -> dict[ModuleType, str]:
    source = Path(str(comports.__file__)).read_text(encoding="utf-8")
    return {comports: f"{source}\n\n{snippet}\n"}


@pytest.mark.parametrize("snippet", list(WRITES.values()), ids=list(WRITES))
def test_the_guard_sees_a_refusal_added_in_any_form_it_reads(snippet: str) -> None:
    unpinned, unseen = inventory_drift(com_scan(_appended(snippet)))

    assert unpinned == ["catalogue_mutation"]
    assert unseen == []


@pytest.mark.parametrize("snippet", list(UNREADABLE_WRITES.values()), ids=list(UNREADABLE_WRITES))
def test_the_guard_refuses_a_form_it_cannot_read(snippet: str) -> None:
    scan = com_scan(_appended(snippet))
    appended_from = len(Path(str(comports.__file__)).read_text(encoding="utf-8").splitlines()) + 1

    assert len(scan.unsupported) == 1, scan.unsupported
    assert int(scan.unsupported[0].split(":")[1]) > appended_from, scan.unsupported


def test_the_guard_notices_a_refusal_that_is_no_longer_written() -> None:
    source = Path(str(comports.__file__)).read_text(encoding="utf-8")
    written = '"error_type": "com_reader_start_failed", '
    assert source.count(written) == 1

    unpinned, unseen = inventory_drift(com_scan({comports: source.replace(written, "")}))

    assert unpinned == []
    assert unseen == ["com_reader_start_failed"]


# ---------------------------------------------------------------------------
# What `com_session_start` forwards from the coordinator.


@dataclass(frozen=True)
class Function:
    module: ModuleType
    owner: str | None
    node: ast.FunctionDef

    @property
    def label(self) -> str:
        name = f"{self.owner}.{self.node.name}" if self.owner else self.node.name
        return f"{Path(str(self.module.__file__)).name}:{name}"


def _definitions(modules: tuple[ModuleType, ...]) -> dict[tuple[str, str | None, str], Function]:
    found: dict[tuple[str, str | None, str], Function] = {}
    for module in modules:
        for node in ast.parse(Path(str(module.__file__)).read_text(encoding="utf-8")).body:
            if isinstance(node, ast.FunctionDef):
                found[(module.__name__, None, node.name)] = Function(module, None, node)
            elif isinstance(node, ast.ClassDef):
                for item in node.body:
                    if isinstance(item, ast.FunctionDef):
                        found[(module.__name__, node.name, item.name)] = Function(module, node.name, item)
    return found


def _is_self(node: ast.AST) -> bool:
    return isinstance(node, ast.Name) and node.id == "self"


def _is_self_bench(node: ast.AST) -> bool:
    return isinstance(node, ast.Attribute) and node.attr == "bench" and _is_self(node.value)


def coordinator_closure(start: str = "acquire") -> list[Function]:
    """Every function `HardwareCoordinator.<start>` can reach in coordination.py and bench.py.

    Followed: a method of the same class through `self`, a `BenchMutex` method
    through `self.bench`, and a module-level function of either file by name.
    A call on any other object is not followed; the coordinator reaches the
    device mutex only through `self.bench`."""
    modules = (coordination, bench_module)
    definitions = _definitions(modules)
    names = {module.__name__ for module in modules}
    pending = [definitions[(coordination.__name__, "HardwareCoordinator", start)]]
    reached: dict[str, Function] = {}
    while pending:
        function = pending.pop()
        if function.label in reached:
            continue
        reached[function.label] = function
        for node in ast.walk(function.node):
            key: tuple[str, str | None, str] | None = None
            if isinstance(node, ast.Attribute) and _is_self(node.value) and function.owner is not None:
                key = (function.module.__name__, function.owner, node.attr)
            elif isinstance(node, ast.Attribute) and _is_self_bench(node.value):
                key = (bench_module.__name__, "BenchMutex", node.attr)
            elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                target = getattr(function.module, node.id, None)
                if inspect.isfunction(target) and target.__module__ in names:
                    key = (target.__module__, None, target.__name__)
            if key is not None and key in definitions:
                pending.append(definitions[key])
    return list(reached.values())


def coordinator_refusals() -> Scan:
    scan = Scan()
    for function in coordinator_closure():
        scan.merge(scan_error_types(function.node, function.module, function.label))
    return scan


def test_the_coordinator_refusals_a_com_start_forwards_are_pinned() -> None:
    """Everything `HardwareCoordinator.acquire` can raise reaches the caller of
    `com_session_start` as it is, so each type it raises is either pinned here
    or owned elsewhere. `run_stopped` is raised on the same path only for a
    caller that passes a stop request, which the next test shows this one
    never does."""
    scan = coordinator_refusals()

    assert scan.unsupported == [], scan.unsupported
    assert set(scan.found) == COM_FORWARDED.keys() | OWNED_ELSEWHERE["#646"] | {"run_stopped"}, scan.found


def test_the_coordinator_never_asks_the_device_mutex_to_watch_for_a_stop() -> None:
    """`run_stopped` is written once, in `BenchMutex._take`, under a
    `stop_requested` its caller handed it. `BenchMutex.acquire` takes that
    argument by keyword only, defaulting to None, and no call the coordinator
    makes on `self.bench` passes it."""
    closure = coordinator_closure()
    stops = [site for function in closure for site in scan_error_types(function.node, function.module, function.label).found.get("run_stopped", [])]
    assert stops and all(site.startswith("bench.py:BenchMutex._take:") for site in stops), stops

    acquire = _definitions((bench_module,))[(bench_module.__name__, "BenchMutex", "acquire")].node
    keyword_only = {argument.arg: default for argument, default in zip(acquire.args.kwonlyargs, acquire.args.kw_defaults, strict=True)}
    assert "stop_requested" in keyword_only
    default = keyword_only["stop_requested"]
    assert isinstance(default, ast.Constant) and default.value is None

    asked = [
        f"{function.label}:{node.lineno}"
        for function in closure
        if function.owner != "BenchMutex"
        for node in ast.walk(function.node)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and _is_self_bench(node.func.value) and any(keyword.arg in {"stop_requested", None} for keyword in node.keywords)
    ]
    assert asked == []


def test_com_session_start_forwards_the_coordinator_refusal_whole() -> None:
    """The refusal of `self.coordinator.acquire` is returned with its own
    fields, `error_type` among them, so the types above reach the caller."""
    tree = ast.parse(Path(str(comports.__file__)).read_text(encoding="utf-8"))
    service = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "ComPortService")
    start = next(node for node in service.body if isinstance(node, ast.FunctionDef) and node.name == "session_start")

    forwarding = []
    for node in ast.walk(start):
        if not isinstance(node, ast.Try):
            continue
        calls = [call for statement in node.body for call in ast.walk(statement) if isinstance(call, ast.Call)]
        if not any(ast.unparse(call.func) == "self.coordinator.acquire" for call in calls):
            continue
        for handler in node.handlers:
            if not (isinstance(handler.type, ast.Name) and handler.type.id == "CoordinationError" and handler.name):
                continue
            returned = [statement for statement in handler.body if isinstance(statement, ast.Return)]
            spreads = [
                item
                for statement in returned
                for item in ast.walk(statement)
                if isinstance(item, ast.Dict)
                and any(key is None and ast.unparse(value) == f"{handler.name}.result" for key, value in zip(item.keys, item.values, strict=True))
            ]
            forwarding.extend(spreads)
    assert len(forwarding) == 1, "com_session_start no longer returns the coordinator's refusal whole"
    # Spread last, so no field this call sets can overwrite the refusal's own.
    spread = forwarding[0]
    assert spread.keys[-1] is None


# ---------------------------------------------------------------------------
# Resolution at the URI.


@pytest.fixture
def reference(tmp_path: Path) -> Iterator[AgenticHILToolService]:
    service = AgenticHILToolService(load_config(str(write_config(tmp_path / "reference"))), frontend="mcp")
    try:
        yield service
    finally:
        close(service)


def resolved(service: AgenticHILToolService, error_type: str) -> dict:
    """The entry `resources/read` answers for one error_type's URI."""
    uri = ERROR_URI_PREFIX + error_type
    response = handle_mcp_message({"jsonrpc": "2.0", "id": 1, "method": "resources/read", "params": {"uri": uri}}, service)
    assert isinstance(response, dict), response
    assert "error" not in response, response
    contents = response["result"]["contents"]
    assert [content["uri"] for content in contents] == [uri]
    return json.loads(contents[0]["text"])


@pytest.mark.parametrize("error_type", sorted(COM_ERROR_TYPES))
def test_the_reference_resolves_every_com_error_type(reference: AgenticHILToolService, error_type: str) -> None:
    entry = resolved(reference, error_type)

    assert entry == catalogue_entry(error_type)
    assert entry["error_type"] == error_type
    assert entry["meaning"].strip(), entry
    assert entry["remediation"], entry
    assert all(isinstance(step, str) and step.strip() for step in entry["remediation"]), entry
    assert entry.get("do_not"), entry
    assert all(isinstance(step, str) and step.strip() for step in entry["do_not"]), entry


# ---------------------------------------------------------------------------
# What each new entry says, one sentence at a time.


def sentences(text: str) -> list[str]:
    return [part for part in re.split(r"(?<=[.;!?])\s+|\n+", text) if part.strip()]


def says(texts: str | list[str], *patterns: str) -> bool:
    """Whether one sentence of `texts` matches every pattern, case aside."""
    pieces = [texts] if isinstance(texts, str) else texts
    return any(all(re.search(pattern, sentence, re.IGNORECASE) for pattern in patterns) for text in pieces for sentence in sentences(text))


def step_index(steps: list[str], *patterns: str) -> int:
    """The first step with a sentence matching every pattern, or -1."""
    return next((index for index, step in enumerate(steps) if says(step, *patterns)), -1)


def entry_of(error_type: str) -> dict:
    entry = catalogue_entry(error_type)
    assert entry is not None, f"the catalogue has no entry for {error_type}"
    return entry


# The statuses `verify_port_identity` refuses as unverified, read off the
# source: each is assigned in the block that returns `_identity_unverified`.
def unverified_statuses() -> set[str]:
    tree = ast.parse(Path(str(comports.__file__)).read_text(encoding="utf-8"))
    statuses: set[str] = set()
    for node in ast.walk(tree):
        for block in (getattr(node, "body", None), getattr(node, "orelse", None)):
            if not isinstance(block, list):
                continue
            refuses = any(isinstance(statement, ast.Return) and isinstance(statement.value, ast.Call) and ast.unparse(statement.value.func) == "_identity_unverified" for statement in block)
            if not refuses:
                continue
            for statement in block:
                if isinstance(statement, ast.Assign) and any(isinstance(target, ast.Subscript) and isinstance(target.slice, ast.Constant) and target.slice.value == "status" for target in statement.targets):
                    assert isinstance(statement.value, ast.Constant), ast.unparse(statement)
                    statuses.add(statement.value.value)
    return statuses


# What each new entry must say, as (where, patterns) with every pattern in one
# sentence. `meaning` is the meaning, `first` the first step, `steps` any step
# and `do_not` any of the wrong fixes.
CLAIMS: dict[str, list[tuple[str, tuple[str, ...]]]] = {
    "com_port_not_configured": [
        ("meaning", (r"`port_id`", r"`com_ports`", r"\bno\b")),
        ("meaning", (r"\bnothing\b", r"\bopen")),
        ("meaning", (r"`com_port_not_bound`",)),
        ("first", (r"`configured_ports`",)),
        ("do_not", (r"\bdevice\b", r"`port_id`")),
    ],
    "com_port_open_failed": [
        ("meaning", (r"(refused|failed)", r"\bopen")),
        ("meaning", (r"\bno session\b",)),
        ("meaning", (r"`retry_safe`",)),
        ("first", (r"`backend_error`", r"`likely_causes`")),
        ("steps", (r"\b(dialout|uucp)\b",)),
        ("steps", (r"`cleanup_error`", r"`com_session_start`")),
        ("do_not", (r"\b(another|other|different)\b", r"\bdevice\b")),
    ],
    "serial_backend_not_available": [
        ("meaning", (r"pyserial", r"\bimport")),
        ("meaning", (r"`com_port_identity_unverified`", r"`backend_unavailable`")),
        ("first", (r"\binstall", r"\b(environment|interpreter)\b")),
        ("steps", (r"\brestart", r"\bserver\b")),
        ("do_not", (r"\b(another|other|different|second)\b", r"\b(python|interpreter|environment)\b")),
    ],
    "com_port_discovery_failed": [
        ("meaning", (r"\benumerat", r"\b(raised|failed|refused)\b")),
        ("meaning", (r"\bnot\b", r"\bno (port|board|adapter)\b")),
        ("meaning", (r"`com_port_identity_unverified`",)),
        ("first", (r"(`com_ports_list`|`agentic-hil com-ports`)", r"\bagain\b")),
        ("do_not", (r"\b(remove|delete)\b", r"`(serial_number|vid|pid)`")),
    ],
    "com_port_identity_unverified": [
        ("meaning", (r"\bnot\b", r"\bmismatch\b")),
        ("meaning", (r"\bnot opened\b|\bwas not opened\b|\bnothing was\b",)),
        ("first", (r"`identity\.status`",)),
        ("steps", (r"`com_session_start`",)),
        ("do_not", (r"\b(remove|delete|drop)\b", r"`(serial_number|vid|pid)`")),
    ],
    "com_reader_start_failed": [
        ("meaning", (r"\breader\b", r"\bstart")),
        ("meaning", (r"\bclosed\b", r"`cleanup_confirmed`")),
        ("meaning", (r"\bnothing\b", r"\bwritten\b")),
        ("first", (r"`com_session_start`",)),
        ("steps", (r"\brestart", r"\bserver\b")),
        ("do_not", (r"\b(another|other)\b", r"\b(serial|terminal|program|tool)\b")),
    ],
    "com_port_close_failed": [
        ("meaning", (r"`backend_error`",)),
        ("meaning", (r"\bregistered\b",)),
        ("meaning", (r"`session_not_active`",)),
        ("meaning", (r"`quarantined`", r"\baudit\b")),
        ("first", (r"`com_session_stop`", r"\b(same|again)\b")),
        ("steps", (r"`quarantine_guidance`",)),
        ("do_not", (r"confirm-safe-state", r"`quarantined`")),
    ],
    "serial_write_failed": [
        ("meaning", (r"\bunknown\b",)),
        ("meaning", (r"`retry_safe`", r"\bfalse\b")),
        ("meaning", (r"\b(part|some)\b", r"\b(arrived|reached)\b")),
        ("first", (r"`com_read`",)),
        ("steps", (r"\bknown state\b",)),
        ("do_not", (r"\b(resend|send|repeat)\b", r"\bnothing\b")),
    ],
    "serial_write_incomplete": [
        ("meaning", (r"`bytes_written`", r"`bytes_requested`")),
        ("meaning", (r"\b(rest|remainder)\b", r"\bnever\b")),
        ("meaning", (r"\bsession\b", r"\b(open|usable)\b")),
        # Two writes end short, and their summaries say which: on Linux the
        # line is handed what it carries within `write_timeout_s` and the rest
        # is not sent, and only the other write retries the remainder first.
        ("meaning", (r"\bLinux\b", r"`write_timeout_s`", r"\b(not|never)\b[^.;]*\bsen[dt]\b")),
        ("meaning", (r"\bretr(y|ied)\b", r"\b(otherwise|elsewhere)\b")),
        ("first", (r"`com_read`",)),
        ("steps", (r"`bytes_written`", r"\b(missing|rest|remainder|from)\b")),
        ("do_not", (r"\b(repeat|resend|replay|send)\b", r"\b(whole|entire|full|same)\b")),
    ],
    "com_buffer_clear_failed": [
        ("meaning", (r"\balready active\b", r"\b(stays|kept|remains)\b", r"\b(open|usable)\b")),
        ("meaning", (r"`cleanup_confirmed`", r"\bclosed\b")),
        ("first", (r"`cleanup_confirmed`",)),
        ("steps", (r"`com_read`", r"\bdiscard")),
        ("do_not", (r"\b(fresh|stale|old)\b",)),
    ],
    "session_lease_held": [
        ("meaning", (r"\bclosed\b", r"\blease\b")),
        ("meaning", (r"`cleanup_confirmed`", r"\bnever\b")),
        ("meaning", (r"`next_step`",)),
        ("meaning", (r"`participant`",)),
        ("first", (r"`next_step`", r"`bench_run_stop`")),
        ("steps", (r"\bstop\b", r"\bagain\b")),
        ("do_not", (r"\bloop\b",)),
    ],
    "session_not_active": [
        ("meaning", (r"`port_id`",)),
        ("meaning", (r"`bus_id`",)),
        ("meaning", (r"\bdebug\b",)),
        ("meaning", (r"`reader_error`",)),
        ("steps", (r"\bCOM\b", r"`com_session_start`", r"`port_id`")),
        ("steps", (r"\bCAN\b", r"`can_session_start`", r"`bus_id`")),
        ("steps", (r"\bdebug\b", r"`debug_start_session`")),
        ("steps", (r"`debug_stop_session`", r"\b(error|errored|exited|GDB)\b")),
        ("steps", (r"`reader_error`", r"`com_session_start`", r"\b(again|replac)")),
        ("do_not", (r"\bloop\b",)),
    ],
    "serial_read_failed": [
        ("meaning", (r"`reader_error`",)),
        ("meaning", (r"\b(inactive|not active|no longer active|ended)\b",)),
        ("meaning", (r"`com_read`", r"\bbuffer")),
        ("first", (r"`likely_causes`|`backend_error`",)),
        ("steps", (r"`com_session_start`",)),
        ("do_not", (r"`com_read`|`com_write`",)),
    ],
    "audit_write_failed": [
        ("meaning", (r"\b(log|logged|audit)\b", r"\b(could not|cannot|failed)\b")),
        ("meaning", (r"`resource_quarantined`", r"`com_reader_audit_broken`")),
        ("meaning", (r"`com_ports_list`", r"`reader_error`")),
        ("first", (r"`backend_error`|`log_path`",)),
        ("steps", (r"confirm-safe-state", r"quarantine-id")),
        ("do_not", (r"\b(delete|truncate|remove)\b", r"\blog\b")),
    ],
}

# Steps whose order is the point: each pattern set must be met in a later step
# than the one before it.
ORDER: dict[str, list[tuple[str, ...]]] = {
    "serial_write_incomplete": [(r"`com_read`",), (r"`bytes_written`",)],
    "serial_write_failed": [(r"`com_read`",), (r"\bknown state\b",)],
    "serial_read_failed": [(r"`com_read`",), (r"`com_session_start`",)],
    "audit_write_failed": [(r"`backend_error`|`log_path`",), (r"confirm-safe-state",)],
    "com_port_close_failed": [(r"`com_session_stop`",), (r"`quarantine_guidance`",)],
    "com_buffer_clear_failed": [(r"`cleanup_confirmed`",), (r"`com_read`",)],
    "session_lease_held": [(r"`bench_run_stop`",), (r"\bagain\b",)],
}


def test_every_new_entry_has_its_claims() -> None:
    assert set(CLAIMS) == NEW_ENTRIES
    assert set(ORDER) <= NEW_ENTRIES


@pytest.mark.parametrize("error_type", sorted(CLAIMS))
def test_a_new_entry_says_what_the_code_does(error_type: str) -> None:
    entry = entry_of(error_type)
    where = {"meaning": [entry["meaning"]], "first": entry["remediation"][:1], "steps": entry["remediation"], "do_not": entry.get("do_not", [])}

    unmet = [(place, patterns) for place, patterns in CLAIMS[error_type] if not says(where[place], *patterns)]
    assert unmet == [], (unmet, entry)


@pytest.mark.parametrize("error_type", sorted(ORDER))
def test_a_new_entry_orders_its_steps_the_way_the_state_needs(error_type: str) -> None:
    steps = entry_of(error_type)["remediation"]
    indexes = [step_index(steps, *patterns) for patterns in ORDER[error_type]]

    assert -1 not in indexes, (indexes, steps)
    assert indexes == sorted(indexes) and len(set(indexes)) == len(indexes), (indexes, steps)


def test_the_unverified_entry_names_every_status_it_is_refused_with() -> None:
    statuses = unverified_statuses()
    assert statuses == {"backend_unavailable", "port_not_enumerated", "serial_unknown", "usb_ids_unknown"}
    entry = entry_of("com_port_identity_unverified")

    unnamed = sorted(status for status in statuses if not says(entry["meaning"], rf"`{status}`"))
    assert unnamed == [], entry["meaning"]


def test_the_unverified_entry_does_not_promise_adoption_rewrites_a_declared_identity() -> None:
    """Adoption fills keys that are empty (AGENTS.md); a declared identity is
    not one. An entry that sent the operator there to replace it would be
    advice the tool does not carry out."""
    entry = entry_of("com_port_identity_unverified")
    text = [entry["meaning"], *entry["remediation"], *entry["do_not"]]

    assert not says(text, r"adopt", r"\b(rewrit|overwrit|replac)")


def test_the_incomplete_write_entry_never_advises_sending_the_whole_payload_again() -> None:
    """Part of the payload is on the line (`bytes_written`), so sending it all
    again hands the target those bytes twice."""
    steps = entry_of("serial_write_incomplete")["remediation"]

    assert not says(steps, r"\b(resend|replay|repeat|retry)\w*", r"\b(whole|entire|full|same|all)\b"), steps


def test_the_incomplete_write_entry_names_the_retry_only_with_the_write_that_makes_it() -> None:
    """A Linux write paced to `write_timeout_s` ends short with no retry at all:
    its summary ends "within write_timeout_s (N s); the rest was not sent." The
    entry is read for both writes, so a sentence that names the retry says which
    write makes it, where "reached it, even after a bounded retry of the
    remainder" claimed one for every short write."""
    meaning = entry_of("serial_write_incomplete")["meaning"]
    retrying = [sentence for sentence in sentences(meaning) if re.search(r"\bretr(y|ied|ies)\b", sentence, re.IGNORECASE)]

    assert retrying, meaning
    unconditioned = [sentence for sentence in retrying if not re.search(r"\b(otherwise|elsewhere|Windows|macOS)\b", sentence, re.IGNORECASE)]
    assert unconditioned == [], unconditioned


# Which kind of session each word or start tool belongs to. Case matters: the
# family names are written in capitals, and "can" is also a verb.
FAMILIES = {
    "COM": (re.compile(r"\bCOM\b|`port_id`"), "com_session_start"),
    "CAN": (re.compile(r"\bCAN\b|`bus_id`"), "can_session_start"),
    "debug": (re.compile(r"\b[Dd]ebug\b(?!_)|\bGDB\b"), "debug_start_session"),
}


def test_the_session_not_active_entry_never_pairs_a_kind_of_session_with_another_kinds_start_tool() -> None:
    entry = entry_of("session_not_active")
    mixed = []
    for text in (entry["meaning"], *entry["remediation"], *entry["do_not"]):
        for sentence in sentences(text):
            for family, (_, tool) in FAMILIES.items():
                if tool not in sentence:
                    continue
                others = [other for other, (words, other_tool) in FAMILIES.items() if other != family and (words.search(sentence) or other_tool in sentence)]
                if others:
                    mixed.append((tool, others, sentence))
    assert mixed == []


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
        # Every payload a handle was asked to write, in order.
        self.writes: list[bytes] = []
        # The handle last opened on each device, to feed its reader.
        self.handles: dict[str, LineHandle] = {}


class LineHandle(ScriptedSerialHandle):
    def __init__(self, line: Line) -> None:
        super().__init__()
        self.line = line

    def open(self) -> None:
        if self.port in self.line.absent:
            raise OSError(f"could not open port {self.port}: no such device")
        super().open()
        self.line.handles[self.port] = self

    def write(self, data: bytes) -> int:
        self.line.writes.append(bytes(data))
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
        yield SimpleNamespace(service=service, monkeypatch=monkeypatch, line=Line(), tmp_path=tmp_path)
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


def wait_for(condition: Callable[[], bool], what: str) -> None:
    deadline = time.monotonic() + scaled_time_bound(5.0)
    while not condition():
        assert time.monotonic() < deadline, f"timed out waiting for {what}"
        time.sleep(0.01)


def carries_its_entry(result: dict, error_type: str) -> None:
    assert result["error_type"] == error_type, result
    advice = remediation_fields(error_type)
    assert advice, f"the catalogue has no entry for {error_type}"
    assert result.get("remediation") == advice["remediation"], result
    assert result.get("do_not") == advice.get("do_not"), result


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


def start_of_an_unbound_port(bench: SimpleNamespace) -> dict:
    return call(bench.service, "com_session_start", {"port_id": UNBOUND_PORT_ID})


def start_without_pyserial(bench: SimpleNamespace) -> dict:
    without_pyserial(bench)
    return call(bench.service, "com_session_start", {"port_id": PORT_ID})


def start_of_an_absent_device(bench: SimpleNamespace) -> dict:
    on_the_line(bench).absent.add(DEVICES[PORT_ID])
    return call(bench.service, "com_session_start", {"port_id": PORT_ID})


def start_of_a_declared_port_the_host_does_not_list(bench: SimpleNamespace) -> dict:
    bench.monkeypatch.setattr("serial.tools.list_ports.comports", lambda: [])
    return call(bench.service, "com_session_start", {"port_id": DECLARED_PORT_ID})


def start_of_a_declared_port_another_board_answers(bench: SimpleNamespace) -> dict:
    other = SimpleNamespace(device=DEVICES[DECLARED_PORT_ID], serial_number="CATALOGUEOTHERSERIAL", vid=None, pid=None)
    bench.monkeypatch.setattr("serial.tools.list_ports.comports", lambda: [other])
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


def restart_whose_input_reset_fails(bench: SimpleNamespace) -> dict:
    started(bench)
    bench.line.refuse_input_reset.add(DEVICES[PORT_ID])
    return call(bench.service, "com_session_start", {"port_id": PORT_ID, "clear_buffer": True})


def stop_whose_close_is_refused(bench: SimpleNamespace) -> dict:
    started(bench)
    bench.line.refuse_close_once.add(DEVICES[PORT_ID])
    return call(bench.service, "com_session_stop", {"port_id": PORT_ID})


def stop_held_by_a_run(bench: SimpleNamespace) -> dict:
    """A write of unknown effect inside a run: the stop closes the handle and
    the run's incident keeps the lease. The run is ended afterwards, so the
    port is given back before the server closes."""
    run = call(bench.service, "bench_run_start", {"devices": [{"kind": "uart", "id": PORT_ID}], "label": "catalogue"})
    assert run["ok"] is True, run
    write_that_dies_on_the_line(bench)
    refusal = call(bench.service, "com_session_stop", {"port_id": PORT_ID})
    assert call(bench.service, "bench_run_stop", {})["ok"] is True
    assert call(bench.service, "com_session_stop", {"port_id": PORT_ID})["ok"] is True
    return refusal


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
    pytest.param(start_of_an_unbound_port, "com_port_not_bound", id="com_session_start-com_port_not_bound"),
    pytest.param(start_without_pyserial, "serial_backend_not_available", id="com_session_start-serial_backend_not_available"),
    pytest.param(start_of_an_absent_device, "com_port_open_failed", id="com_session_start-com_port_open_failed"),
    pytest.param(start_of_a_declared_port_the_host_does_not_list, "com_port_identity_unverified", id="com_session_start-com_port_identity_unverified"),
    pytest.param(start_of_a_declared_port_another_board_answers, "com_port_identity_mismatch", id="com_session_start-com_port_identity_mismatch"),
    pytest.param(start_whose_reader_will_not_start, "com_reader_start_failed", id="com_session_start-com_reader_start_failed"),
    pytest.param(start_whose_input_reset_fails, "com_buffer_clear_failed", id="com_session_start-com_buffer_clear_failed-new-session"),
    pytest.param(restart_whose_input_reset_fails, "com_buffer_clear_failed", id="com_session_start-com_buffer_clear_failed-active-session"),
    pytest.param(stop_whose_close_is_refused, "com_port_close_failed", id="com_session_stop-com_port_close_failed"),
    pytest.param(stop_held_by_a_run, "session_lease_held", id="com_session_stop-session_lease_held"),
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
    carries_its_entry(refusal, error_type)


# Types no refusal above provokes, each with the reason.
PAYLOAD_EXEMPT = {
    "com_port_busy": "its entry was attached to the refusal before #635",
    "permission_denied": "its entry was attached to the refusal before #635",
    "config_invalid": "catalogued and attached before #635",
    "invalid_argument": "catalogued and attached before #635",
    "resource_quarantined": "catalogued before #635; reached by the audit-break test below",
    "device_busy": "forwarded from the coordinator; resolved, and reached by the forwarding test below",
    "undeclared_device": "forwarded from the coordinator; resolved, and reached by the forwarding test below",
    "serial_read_failed": "nested under reader_error; the reader tests below",
    "audit_write_failed": "nested under reader_error; the reader tests below",
}


def test_the_refusals_reach_every_com_type_but_the_exempt_ones() -> None:
    """Each entry new in #635, and each one attached to a payload in #635, is
    shown arriving in a payload, not only in the reference."""
    provoked = {param.values[1] for param in REFUSALS}

    assert sorted(COM_ERROR_TYPES - provoked) == sorted(PAYLOAD_EXEMPT)
    assert not provoked & PAYLOAD_EXEMPT.keys()


def test_a_session_that_was_already_active_stays_open_when_its_buffer_cannot_be_cleared(bench: SimpleNamespace) -> None:
    """comports.py `session_start`, the `existing` branch: the refusal comes
    from a session that stays registered and running, so it carries no
    `cleanup_confirmed`, and the next read is answered from it."""
    refusal = restart_whose_input_reset_fails(bench)

    carries_its_entry(refusal, "com_buffer_clear_failed")
    assert "cleanup_confirmed" not in refusal, refusal
    bench.line.refuse_input_reset.clear()
    read = call(bench.service, "com_read", {"port_id": PORT_ID})
    assert read["ok"] is True, read


def test_a_new_session_whose_buffer_cannot_be_cleared_is_closed_again(bench: SimpleNamespace) -> None:
    """comports.py `session_start`, the new-session branch: the port is closed
    again, the refusal says so in `cleanup_confirmed`, and a later start opens
    a fresh session rather than finding one already active."""
    refusal = start_whose_input_reset_fails(bench)

    carries_its_entry(refusal, "com_buffer_clear_failed")
    assert refusal["cleanup_confirmed"] is True, refusal
    bench.line.refuse_input_reset.clear()
    again = call(bench.service, "com_session_start", {"port_id": PORT_ID, "clear_buffer": True})
    assert again["ok"] is True and again["already_active"] is False, again


@pytest.mark.parametrize("payload", [{"text": ""}, {"hex": ""}, {"hex": "  "}, {"hex": " \n\t"}], ids=["empty-text", "empty-hex", "blank-hex", "whitespace-hex"])
def test_an_empty_payload_is_refused_before_anything_is_written_or_logged(bench: SimpleNamespace, payload: dict) -> None:
    """#634: a payload that carries no byte is a caller's mistake, not a
    stimulus. It is refused as `invalid_argument`, the line is never asked to
    write, and the session log records no transmission."""
    started(bench)
    session = bench.service.com_ports.sessions[PORT_ID]

    refused = call(bench.service, "com_write", {"port_id": PORT_ID, **payload})

    assert refused["ok"] is False and refused["error_type"] == "invalid_argument", refused
    assert "bytes_written" not in refused, refused
    assert bench.line.writes == [], bench.line.writes
    logged = [json.loads(line) for line in Path(session.log_path).read_text(encoding="utf-8").splitlines() if line.strip()]
    assert [entry for entry in logged if entry.get("direction") == "tx"] == [], logged


def mismatch_whose_board_moved(bench: SimpleNamespace, moved_to: str) -> dict:
    """The declared board is attached under `moved_to`; another board holds the configured name."""
    other = SimpleNamespace(device=DEVICES[DECLARED_PORT_ID], serial_number="CATALOGUEOTHERSERIAL", vid=None, pid=None)
    named = SimpleNamespace(device=moved_to, serial_number=DECLARED_SERIAL, vid=None, pid=None)
    bench.monkeypatch.setattr("serial.tools.list_ports.comports", lambda: [other, named])
    return call(bench.service, "com_session_start", {"port_id": DECLARED_PORT_ID})


def test_a_mismatch_whose_board_moved_names_the_device_key_and_the_reload(bench: SimpleNamespace) -> None:
    """#658: adoption keeps a `device` that is already set, so advice that sends
    the caller there changes nothing and the next start is refused the same
    way. The repair is the key itself: `device` set to `expected_device`, then a
    reload."""
    moved_to = "/dev/ttyCATCOM9"
    refusal = mismatch_whose_board_moved(bench, moved_to)

    carries_its_entry(refusal, "com_port_identity_mismatch")
    assert refusal["expected_device"] == moved_to, refusal
    assert says([refusal.get("next_step", "")], rf"`com_ports\.{DECLARED_PORT_ID}\.device`", re.escape(moved_to)), refusal
    advice = [refusal.get("next_step", ""), *refusal["remediation"]]
    assert says(advice, r"`expected_device`", r"`device`", r"`project_config_set`"), advice
    assert says(advice, r"`project_config_reload_description`"), advice
    assert not says([*advice, *refusal["do_not"]], r"adopt"), advice


def test_following_the_mismatch_advice_opens_the_port(bench: SimpleNamespace) -> None:
    """The advice, carried out: `device` set to `expected_device` by an edit of
    the file, and a server on that file opens the port the refusal named. The
    reload itself needs the discovered configuration, which this test does not
    install, so the edited file is loaded the way a restart would load it."""
    moved_to = "/dev/ttyCATCOM9"
    refusal = mismatch_whose_board_moved(bench, moved_to)
    assert refusal["error_type"] == "com_port_identity_mismatch", refusal

    config_path = bench.tmp_path / "workspace" / ".agentic-hil" / "config.yaml"
    text = config_path.read_text(encoding="utf-8")
    configured = f"device: {DEVICES[DECLARED_PORT_ID]}\n"
    assert text.count(configured) == 1, text
    config_path.write_text(text.replace(configured, f"device: {refusal['expected_device']}\n"), encoding="utf-8")

    on_the_line(bench)
    edited = AgenticHILToolService(load_config(str(config_path)), frontend="mcp")
    try:
        again = call(edited, "com_session_start", {"port_id": DECLARED_PORT_ID})
        assert again["ok"] is True, again
        assert again["identity"]["device"] == moved_to, again
    finally:
        close(edited)


def test_an_unverified_identity_never_sends_the_caller_to_adoption(bench: SimpleNamespace) -> None:
    """#658: the entry's `device` is set whenever this refusal can happen
    (an unset one is `com_port_not_bound`), and adoption keeps a set `device`.
    The way out is restoring the check or setting `device` explicitly."""
    refusal = start_of_a_declared_port_the_host_does_not_list(bench)

    assert refusal["error_type"] == "com_port_identity_unverified", refusal
    assert not says([refusal["next_step"]], r"adopt"), refusal["next_step"]
    assert says([refusal["next_step"]], r"\bset\b", r"`device`"), refusal["next_step"]


def comports_reason_literals() -> set[str]:
    """Every reason comports.py hands `record_cleanup_event` or `quarantine` as a literal."""
    reasons: set[str] = set()
    for node in ast.walk(ast.parse(inspect.getsource(comports))):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in {"record_cleanup_event", "quarantine"}):
            continue
        if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
            reasons.add(node.args[0].value)
    return reasons


def test_every_reason_comports_records_has_a_guide() -> None:
    """#659: a reason without a guide is answered with the fallback for a
    reason from another version, which tells the caller to treat the device
    state as unknown and sign for it."""
    reasons = comports_reason_literals()

    assert "serial_write_incomplete" in reasons and "com_write_effect_unconfirmed" in reasons, reasons
    assert sorted(reasons - QUARANTINE_REASON_GUIDES.keys()) == []


def test_a_short_write_and_the_stop_after_it_carry_the_short_write_guide(bench: SimpleNamespace) -> None:
    """#659: the short write is confirmed, nothing is quarantined, and the
    guidance says what was attempted, what reached the line, what is unknown,
    and that no signature is owed."""
    refusal = write_the_line_takes_only_part_of(bench)
    stopped = call(bench.service, "com_session_stop", {"port_id": PORT_ID})

    for result in (refusal, stopped):
        guides = [guide for guide in result.get("quarantine_guidance", []) if guide["reason"] == "serial_write_incomplete"]
        assert len(guides) == 1, result
        guide = guides[0]
        assert "different Agentic HIL version" not in guide["attempted"], guide
        assert "treat the device state as unknown" not in guide["unknown"], guide
        assert says([guide["attempted"]], r"`bytes_requested`"), guide
        assert says([guide["confirmed"]], r"`bytes_written`"), guide
        assert says([guide["unknown"]], r"\bpartial\b"), guide
        assert says([guide["physical_check"]], r"\bno\b", r"\bsign"), guide
    assert stopped["ok"] is True, stopped


# ---------------------------------------------------------------------------
# The reader's own failures, nested under `reader_error`.


def test_a_reader_that_died_is_answered_with_its_error_nested(bench: SimpleNamespace) -> None:
    """What the entries for `serial_read_failed` and `session_not_active` say
    about a dead reader: the bytes it buffered are still handed out, every
    later call is refused with the reader's error nested, and starting the
    port again replaces the failed session."""
    started(bench)
    session = bench.service.com_ports.sessions[PORT_ID]
    last_words = b"last words\r\n"
    bench.line.handles[DEVICES[PORT_ID]].feed(last_words, DIE)
    wait_for(lambda: session.reader_error is not None, "the reader to record its failure")

    buffered = call(bench.service, "com_read", {"port_id": PORT_ID})
    assert buffered["ok"] is True and buffered["bytes_read"] == len(last_words), buffered
    assert buffered["reader_error"]["error_type"] == "serial_read_failed", buffered

    for tool, arguments in (("com_write", {"port_id": PORT_ID, "text": "ping\n"}), ("com_read", {"port_id": PORT_ID})):
        refusal = call(bench.service, tool, arguments)
        carries_its_entry(refusal, "session_not_active")
        assert refusal["reader_error"]["error_type"] == "serial_read_failed", refusal

    listed = call(bench.service, "com_ports_list", {})["ports"][PORT_ID]
    assert listed["reader_error"]["error_type"] == "serial_read_failed", listed
    entry = resolved(bench.service, listed["reader_error"]["error_type"])
    assert entry == entry_of("serial_read_failed")

    again = call(bench.service, "com_session_start", {"port_id": PORT_ID})
    assert again["ok"] is True and again["already_active"] is False, again


def test_a_reader_whose_log_broke_is_answered_with_its_error_nested(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The hardware calls meet the standing incident first and answer
    `resource_quarantined` under the reader's reason; the reader's own error
    is shown in the port's status in `com_ports_list`.

    The quarantine this leaves is this test's own: its state root is its own
    as well, so the incident outlives nothing but this test."""
    config = write_config(tmp_path / "workspace", com_ports_yaml=COM_PORTS_YAML, state_root=tmp_path / "state")
    service = AgenticHILToolService(load_config(str(config)), frontend="mcp")
    bench = SimpleNamespace(service=service, monkeypatch=monkeypatch, line=Line(), tmp_path=tmp_path)
    original = comports.ComPortSession.append_audit

    def received_bytes_cannot_be_logged(self: comports.ComPortSession, event: dict, config: object = None) -> Exception | None:
        if event.get("direction") == "rx":
            return OSError(28, "No space left on device")
        return original(self, event, config)

    try:
        started(bench)
        session = service.com_ports.sessions[PORT_ID]
        monkeypatch.setattr(comports.ComPortSession, "append_audit", received_bytes_cannot_be_logged)
        bench.line.handles[DEVICES[PORT_ID]].feed(b"boot ok\r\n")
        wait_for(lambda: session.reader_error is not None, "the reader to record its failure")

        for tool, arguments in (("com_read", {"port_id": PORT_ID}), ("com_write", {"port_id": PORT_ID, "text": "ping\n"})):
            refusal = call(service, tool, arguments)
            assert refusal["error_type"] == "resource_quarantined", refusal
            assert "com_reader_audit_broken" in refusal["cleanup_reasons"], refusal

        listed = call(service, "com_ports_list", {})["ports"][PORT_ID]
        assert listed["reader_error"]["error_type"] == "audit_write_failed", listed
        entry = resolved(service, listed["reader_error"]["error_type"])
        assert entry == entry_of("audit_write_failed")
    finally:
        with suppress(RuntimeError):
            close(service)


# ---------------------------------------------------------------------------
# The coordinator's refusals, as `com_session_start` forwards them.


def test_a_port_another_owner_holds_is_refused_with_a_type_that_resolves(bench: SimpleNamespace) -> None:
    started(bench)
    other_config = write_config(bench.tmp_path / "other", com_ports_yaml=COM_PORTS_YAML, state_root=bench.tmp_path / "other-state")
    other = AgenticHILToolService(load_config(str(other_config)), frontend="mcp")
    try:
        refusal = call(other, "com_session_start", {"port_id": PORT_ID})
    finally:
        close(other)

    assert refusal["ok"] is False, refusal
    assert refusal["error_type"] == "device_busy", refusal
    assert resolved(bench.service, refusal["error_type"]) == entry_of("device_busy")
    assert remediation_fields(refusal["error_type"]), refusal


def test_a_port_the_open_run_did_not_declare_is_refused_with_a_type_that_resolves(bench: SimpleNamespace) -> None:
    on_the_line(bench)
    run = call(bench.service, "bench_run_start", {"devices": [{"kind": "uart", "id": DECLARED_PORT_ID}], "label": "catalogue-com"})
    assert run["ok"] is True, run
    try:
        refusal = call(bench.service, "com_session_start", {"port_id": PORT_ID})
    finally:
        call(bench.service, "bench_run_stop", {})

    assert refusal["ok"] is False, refusal
    assert refusal["error_type"] == "undeclared_device", refusal
    assert resolved(bench.service, refusal["error_type"]) == entry_of("undeclared_device")
    assert remediation_fields(refusal["error_type"]), refusal


# ---------------------------------------------------------------------------
# session_not_active: one entry for every kind of session.


def test_session_not_active_is_answered_by_the_three_kinds_of_session() -> None:
    """Three kinds of session, four sites, and each site's own session tool.

    The debug session answers from two of them, and a catalogue holding one
    tuple per file could not tell them apart: it would keep whichever the scan
    walked last and still count three. So the sites are compared as pairs, one
    per producer, which pins the multiplicity as well as what each one says.
    Sorted rather than taken in scan order, because which of two returns inside
    one function `ast.walk` reaches first is not a claim about the product.
    """
    sites = session_not_active_sites()
    catalogued = sorted((path, tools) for path, per_file in SESSION_NOT_ACTIVE_SITES.items() for tools in per_file)

    assert sorted((path, tools) for path, _, tools in sites) == catalogued, sites
    assert len(sites) == len(catalogued), sites


def test_a_debug_tool_without_a_session_carries_the_session_not_active_entry(tmp_path: Path) -> None:
    service = AgenticHILToolService(load_config(str(write_config(tmp_path / "debug", gdb_executable=FAKE_GDB))), frontend="mcp")
    try:
        refusal = call(service, "debug_get_stop_reason", {})
    finally:
        close(service)

    carries_its_entry(refusal, "session_not_active")


def test_a_can_tool_without_a_session_resolves_to_the_session_not_active_entry(tmp_path: Path) -> None:
    """The CAN refusal resolves to the same entry, and the entry's CAN step is
    the one that names the CAN start tool. Whether can.py hands the entry out
    in the payload is the CAN tools' own refusal set."""
    service = AgenticHILToolService(load_config(str(write_config(tmp_path / "can", can_buses_yaml=CAN_BUSES_YAML))), frontend="mcp")
    try:
        refusal = call(service, "can_read", {"bus_id": CAN_BUS_ID})
        entry = resolved(service, refusal["error_type"])
    finally:
        close(service)

    assert refusal["error_type"] == "session_not_active", refusal
    assert says(entry["remediation"], r"\bCAN\b", r"`can_session_start`", r"`bus_id`"), entry
