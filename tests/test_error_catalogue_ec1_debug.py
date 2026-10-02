"""Every refusal a debug tool or a debugger backend answers with has a catalogue entry (#644).

AGENTS.md describes `agentic-hil://reference/errors` as listing every
`error_type` with its meaning, its ordered fix and the wrong fix. Much of what
the debug session tools and the three debugger backends refuse with had no
entry there: the reference resolved to nothing, and the refusal carried no
standing fix.

The fix a refusal needs can turn on the backend that answered it, and the
catalogue already says so with keys of the form `<error_type>:<backend>`. So
the inventory here is a set of pairs, the error type and the scope its
remediation is looked up under, and an entry is the scoped key where there is
one and the bare key otherwise. The scope is read off the source as well: a
site that merges `remediation_fields(<type>, <scope>)` itself is looked up
under that scope, and every other refusal a debugger-backed tool returns is
filled where the result leaves the tool service, under the backend the result
names.

Five things are held here. The inventory is read off the source, so a refusal
added later without an entry fails the guard, and it is pinned as well, so the
scan cannot shrink unnoticed; planted refusals prove the scan reads each shape
it claims to. Every pair in it resolves at its URI, and the bare entries other
producers fall back to resolve at their own. Each entry written here says what
its producers' code makes true, clause by clause. The service fills a refusal
that carries no advice of its own, and leaves alone what it must not touch.
And every tool path, driven through the tool service against the suite's fake
debugger, fake GDB and recorded transcripts, hands the entry's steps out in
the refusal itself. No probe, board or debugger process is touched.
"""

from __future__ import annotations

import ast
import json
import re
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from types import MappingProxyType, ModuleType

import pytest
from conftest import DEFAULT_TEST_PERMISSIONS, FAKE_OPENOCD, FAKE_OPENOCD_ACCESS_DENIED, write_config
from test_debug_backend_refusals import (
    FAKE_BY_TYPE,
    FAKE_PYOCD_RESET_REFUSED,
    FAKE_TRANSCRIPT,
    SILENT_PAIRS,
    call,
    config_for,
    play_recording,
    play_transcript,
)
from test_debug_backend_refusals import debug_service as server_service
from test_debug_session_run_state import ABSENT_SYMBOL, MI_ASYNC_UNSUPPORTED, seeded_service, symbol_arguments
from test_debug_sessions import (
    START_TIMEOUT_S,
    flash_symbol_source,
    latch_audit_break,
    pyocd_read_service,
    stlink_dump_service,
)
from test_debug_sessions import debug_service as session_service
from test_gdbserver_sessions import pyocd_session_service, st_link_session_service
from test_openocd_access_denied import HostOs

from agentic_hil import debugger, elfsymbols, gdbmi, tools
from agentic_hil.backends import common, gdbdebug, openocd, pyocd, stlink
from agentic_hil.config import load_config
from agentic_hil.gdbmi import GdbMiCommandResult
from agentic_hil.knowledge import (
    ERROR_CATALOGUE,
    ERROR_URI_PREFIX,
    EXCLUSIVE_PERMISSION_SCOPE,
    ErrorRemedy,
    catalogue_entry,
    lookup_remedy,
    remediation_fields,
)
from agentic_hil.mcp import handle_mcp_message
from agentic_hil.tools import AgenticHILToolService

BACKENDS = ("openocd", "pyocd", "stlink")
Pair = tuple[str, str | None]

# ---------------------------------------------------------------------------
# Where the debug refusals are built, and whose remediation each one is.

# Each backend module answers under its own name: that is the `backend` its
# results carry, and the scope the service fills a refusal under.
BACKEND_MODULES: dict[str, ModuleType] = {"openocd": openocd, "pyocd": pyocd, "stlink": stlink}

# `GdbDebugSessions` runs the typed debug sessions, and every backend constructs
# it under its own name (#624, pinned below), so a refusal its methods build is
# answered under the backend whose session it is. The scan reads each such site
# once per backend, because what a site can answer can turn on the backend that
# constructed the session (`PINNED_EXPRESSIONS`). The module's own functions are
# shared, and named one by one with the backends whose calls reach them (also
# pinned below).
GDBDEBUG_SESSION_CLASS = "GdbDebugSessions"
GDBDEBUG_SESSION_SCOPES: tuple[str, ...] = BACKENDS
GDBDEBUG_FUNCTION_SCOPES: dict[str, tuple[str, ...]] = {
    # The offline symbol read of the backends that read memory without a session.
    "validate_debug_symbol": ("pyocd", "stlink"),
    "resolve_symbol_offline": ("pyocd", "stlink"),
    # Only `GdbDebugSessions.set_breakpoint` normalizes a location.
    "normalize_breakpoint_location": GDBDEBUG_SESSION_SCOPES,
    "normalize_symbol_location": GDBDEBUG_SESSION_SCOPES,
    # Only the typed sessions report where their target stopped.
    "target_stop_fields": GDBDEBUG_SESSION_SCOPES,
}
# The module's constants that hold error types, each with the backends whose
# sessions answer with them.
GDBDEBUG_CONSTANT_SCOPES: dict[str, tuple[str, ...]] = {
    # The proofs a stop takes; `GdbDebugSessions.stop_session` answers with the
    # first one it missed (`PINNED_EXPRESSIONS`).
    "_TEARDOWN_PROOFS": GDBDEBUG_SESSION_SCOPES,
}
# Refusal builders in common.py that take the calling backend's name and
# answer under it.
COMMON_FUNCTION_SCOPES: dict[str, tuple[str, ...]] = {
    # The one backend that opens no session where no GDB server is found.
    "debug_session_unsupported": ("stlink",),
    "reset_init_unsupported": ("pyocd", "stlink"),
}

# The debug paths of the tool service. Each answers before any backend is
# asked, and its refusal names no backend, so the bare key is the one a
# refusal of theirs is served by.
TOOLS_DEBUG_FUNCTIONS = frozenset(
    {
        "debugger_info",
        "debugger_probes_list",
        "probe_target",
        "reset_target",
        "debug_start_session",
        "debug_stop_session",
        "debug_get_session_status",
        "debug_set_breakpoint",
        "debug_list_breakpoints",
        "debug_clear_breakpoints",
        "debug_continue",
        "debug_halt",
        "debug_get_stop_reason",
        "debug_symbol_info",
        "debug_symbol_value",
        "debug_dump_symbol_ihex",
        "_coordinated_debug_call",
        "_debug_permission_failure",
        "unbound_debugger_error",
        "unnamed_probe_error",
    }
)
# Every other function of the tool service that writes an error type, with the
# change whose area it is. A function in neither set fails the scan: a new
# helper is classified once, by whoever adds it, instead of passing unread.
TOOLS_OTHER_AREAS: dict[str, str] = {
    **dict.fromkeys(
        (
            "call",
            "_call_unlocked",
            "_dispatch_tool",
            "tool_error",
            "_flash_capturing",
            "flash_firmware",
            "_config_write_denied",
            "_create_in_open_run_refusal",
            "unprovisioned_tool_error",
        ),
        "#645: the dispatcher, flash, artifacts and configuration writes",
    ),
    "_capture_result": "#635: the COM capture a flash opens",
    **dict.fromkeys(("hardware_recover", "test_reactor_run", "_lock_cleanup_refusal"), "#646: runs, coordination and recovery"),
}

# Expressions the scan cannot evaluate from the source alone, each pinned by
# its exact text where it stands, with the values it can take and why. A
# change to the expression is a change to this table. Each is handed the
# backend whose session the site is read for, or None for a read that is not
# one backend's, which answers what any of them can.
PINNED_EXPRESSIONS: dict[tuple[str, str, str], Callable[[str | None], frozenset[str]]] = {
    # Reached only under `if not ok`, and `ok` is `stop_reason not in
    # ABNORMAL_STOP_REASONS`, so the argument is one of those reasons.
    ("gdbdebug.py", "_stopped_result", "stop_error_type(stop_reason)"): lambda _backend: frozenset(gdbdebug.stop_error_type(reason) for reason in gdbdebug.ABNORMAL_STOP_REASONS),
    # The same reasons, set as `target_error_type` under `if stop_reason in
    # ABNORMAL_STOP_REASONS`.
    ("gdbdebug.py", "target_stop_fields", "stop_error_type(stop_reason)"): lambda _backend: frozenset(gdbdebug.stop_error_type(reason) for reason in gdbdebug.ABNORMAL_STOP_REASONS),
    # A debug server that stopped before its GDB port was ready: the session
    # hands its output to the classifier it was constructed with (pinned
    # below), and the result carries what that answers, with `debugger_error`
    # for a classification that matched nothing. On OpenOCD that is
    # `_classify_output` without a tool. pyOCD's and stlink's readings of the
    # same output always name an error type of their own, merged over this one
    # (pinned below), so on those two this value never reaches a result.
    ("gdbdebug.py", "_start_failure", "backend_error_type if backend_error_type != 'unknown_debugger_error' else 'debugger_error'"): lambda backend: (
        (classifier_returns_without_a_tool(openocd) - {"unknown_debugger_error"}) | {"debugger_error"} if backend in {None, "openocd"} else frozenset()
    ),
    # A stop that did not get every teardown proof answers with the first one
    # it missed, in the order `_TEARDOWN_PROOFS` takes them. On a server with a
    # command that keeps the core halted when GDB detaches, that detach takes
    # the breakpoints off, and the removal is proven without a command of its
    # own (pinned below), so there the first proof missed is never that one.
    ("gdbdebug.py", "stop_session", "unconfirmed[0].error_type"): lambda backend: frozenset(
        proof.error_type for proof in gdbdebug._TEARDOWN_PROOFS if proof.error_type != "breakpoints_not_removed" or backend is None or server_steps_of(backend).detach_guard_command is None
    ),
    # ST-LINK_gdbserver's output is read against a table of its recorded
    # refusals; the loop answers the name of the first one that matches.
    ("stlink.py", "classify_gdb_server_output", "backend_error_type"): lambda _backend: frozenset(name for name, _pattern in stlink.ST_LINK_GDB_SERVER_REFUSALS),
}


def classifier_returns_without_a_tool(module: ModuleType) -> frozenset[str]:
    """What `module`'s `_classify_output` can answer when it is handed no tool.

    The classifier is a ladder of `if <words>: return "<bucket>"`. A rung whose
    test names `tool` cannot fire without one; any other shape fails the scan.
    """
    (function,) = source_of(module).functions["_classify_output"]
    values: set[str] = set()
    for statement in function.body:
        if isinstance(statement, ast.Return) and isinstance(statement.value, ast.Constant):
            values.add(statement.value.value)
        elif isinstance(statement, ast.If) and not statement.orelse and len(statement.body) == 1 and isinstance(statement.body[0], ast.Return):
            returned = statement.body[0].value
            if not (isinstance(returned, ast.Constant) and isinstance(returned.value, str)):
                raise UnreadableValue(f"{module.__name__}._classify_output:{statement.lineno}: {ast.unparse(statement)}")
            if not any(isinstance(node, ast.Name) and node.id == "tool" for node in ast.walk(statement.test)):
                values.add(returned.value)
        elif any(isinstance(node, ast.Return) for node in ast.walk(statement)):
            raise UnreadableValue(f"{module.__name__}._classify_output:{statement.lineno}: {ast.unparse(statement)}")
    return frozenset(values)


# What each backend publishes for what it classified: its own table, applied by
# `_public_error_type` (whose body is pinned below).
MAPPERS = frozenset({"_public_error_type"})


# ---------------------------------------------------------------------------
# Reading the inventory off the source.


class UnreadableValue(Exception):
    pass


@dataclass
class Source:
    module: ModuleType
    name: str
    tree: ast.Module
    parents: dict[ast.AST, ast.AST] = field(default_factory=dict)
    functions: dict[str, list[ast.FunctionDef]] = field(default_factory=dict)

    @classmethod
    def of(cls, module: ModuleType, text: str | None = None) -> Source:
        """`module`'s source, or `text` read as if it were that module's."""
        path = Path(str(module.__file__))
        source = cls(module, path.name, ast.parse(path.read_text(encoding="utf-8") if text is None else text))
        for node in ast.walk(source.tree):
            for child in ast.iter_child_nodes(node):
                source.parents[child] = node
            if isinstance(node, ast.FunctionDef):
                source.functions.setdefault(node.name, []).append(node)
        return source

    def enclosing_function(self, node: ast.AST) -> ast.FunctionDef | None:
        while node in self.parents:
            node = self.parents[node]
            if isinstance(node, ast.FunctionDef):
                return node
        return None

    def enclosing_class(self, node: ast.AST) -> str | None:
        while node in self.parents:
            node = self.parents[node]
            if isinstance(node, ast.ClassDef):
                return node.name
        return None


@cache
def source_of(module: ModuleType) -> Source:
    return Source.of(module)


@dataclass(frozen=True)
class Scope:
    """Where an expression is read: a function, and what its parameters are bound to at one call."""

    source: Source
    function: ast.FunctionDef | None
    bindings: tuple[tuple[str, frozenset[str | None]], ...] = ()

    def __hash__(self) -> int:
        return hash((self.source.name, id(self.function), self.bindings))


def _parameters(function: ast.FunctionDef) -> list[ast.arg]:
    return [*function.args.posonlyargs, *function.args.args, *function.args.kwonlyargs]


def _assigned(function: ast.FunctionDef, name: str) -> list[ast.expr]:
    """Every expression `function` assigns to `name`, tuple unpacking included."""
    values: list[ast.expr] = []
    for node in ast.walk(function):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    values.append(node.value)
                elif isinstance(target, ast.Tuple) and isinstance(node.value, ast.Tuple):
                    values.extend(value for element, value in zip(target.elts, node.value.elts, strict=True) if isinstance(element, ast.Name) and element.id == name)
                elif isinstance(target, ast.Tuple) and any(isinstance(element, ast.Name) and element.id == name for element in target.elts):
                    raise UnreadableValue(f"{name} unpacked from {ast.unparse(node.value)}")
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == name and node.value is not None:
            values.append(node.value)
    return values


def _calls_to(source: Source, name: str) -> list[ast.Call]:
    calls = []
    for node in ast.walk(source.tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if (isinstance(func, ast.Name) and func.id == name) or (isinstance(func, ast.Attribute) and func.attr == name and isinstance(func.value, ast.Name) and func.value.id == "self"):
            calls.append(node)
    return calls


def _argument(call: ast.Call, function: ast.FunctionDef, parameter: str) -> ast.expr | None:
    names = [argument.arg for argument in _parameters(function)]
    is_method = bool(names) and names[0] == "self"
    for keyword in call.keywords:
        if keyword.arg == parameter:
            return keyword.value
    positional = [argument.arg for argument in [*function.args.posonlyargs, *function.args.args]]
    index = positional.index(parameter) if parameter in positional else None
    if index is not None:
        index -= 1 if is_method else 0
        if 0 <= index < len(call.args):
            return call.args[index]
    defaults = dict(zip(reversed(positional), reversed(function.args.defaults), strict=False))
    defaults.update({argument.arg: default for argument, default in zip(function.args.kwonlyargs, function.args.kw_defaults, strict=True) if default is not None})
    return defaults.get(parameter)


class Reader:
    """Evaluates an `error_type` expression to every string it can take.

    With `keep_none` a literal None is one of the values, which is what a scope
    argument means by it: the bare key.
    """

    def __init__(self, *, keep_none: bool = False, backend: str | None = None) -> None:
        self._active: set[tuple[str, int, str]] = set()
        self._keep_none = keep_none
        self._backend = backend

    def values(self, node: ast.expr, scope: Scope) -> frozenset[str | None]:
        if scope.function is not None:
            pinned = PINNED_EXPRESSIONS.get((scope.source.name, scope.function.name, ast.unparse(node)))
            if pinned is not None:
                return frozenset(pinned(self._backend))
        if isinstance(node, ast.Constant):
            if node.value is None:
                return frozenset({None}) if self._keep_none else frozenset()
            if isinstance(node.value, str):
                return frozenset({node.value})
        elif isinstance(node, ast.IfExp):
            return self.values(node.body, scope) | self.values(node.orelse, scope)
        elif isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or):
            return frozenset().union(*(self.values(value, scope) for value in node.values))
        elif isinstance(node, ast.Name):
            return self._name(node.id, scope)
        elif isinstance(node, ast.Call):
            return self._call(node, scope)
        raise UnreadableValue(f"{scope.source.name}:{node.lineno}: {ast.unparse(node)}")

    def _name(self, name: str, scope: Scope) -> frozenset[str | None]:
        bound = dict(scope.bindings)
        if name in bound:
            return bound[name]
        if scope.function is not None:
            assigned = _assigned(scope.function, name)
            is_parameter = name in {argument.arg for argument in _parameters(scope.function)}
            if assigned or is_parameter:
                values = frozenset().union(*(self.values(value, scope) for value in assigned))
                return values | (self._parameter(name, scope) if is_parameter else frozenset())
        named = getattr(scope.source.module, name, None)
        if isinstance(named, str):
            return frozenset({named})
        raise UnreadableValue(f"{scope.source.name}: {name}")

    def _parameter(self, name: str, scope: Scope) -> frozenset[str | None]:
        """A parameter no call-site binding fixes: what every caller in the module passes."""
        assert scope.function is not None
        key = (scope.source.name, scope.function.lineno, name)
        if key in self._active:
            return frozenset()
        self._active.add(key)
        try:
            values: frozenset[str | None] = frozenset()
            calls = _calls_to(scope.source, scope.function.name)
            if not calls:
                raise UnreadableValue(f"{scope.source.name}:{scope.function.name}: parameter {name} has no caller in the module")
            for call in calls:
                argument = _argument(call, scope.function, name)
                if argument is None:
                    raise UnreadableValue(f"{scope.source.name}:{call.lineno}: no {name} passed to {scope.function.name}")
                values |= self.values(argument, Scope(scope.source, scope.source.enclosing_function(call)))
            return values
        finally:
            self._active.discard(key)

    def _call(self, node: ast.Call, scope: Scope) -> frozenset[str | None]:
        func = node.func
        # `{...}.get(key, default)`: any value of the literal, or the default.
        if isinstance(func, ast.Attribute) and func.attr == "get" and isinstance(func.value, ast.Dict):
            values = frozenset().union(*(self.values(value, scope) for value in func.value.values))
            return values | (self.values(node.args[1], scope) if len(node.args) > 1 else frozenset())
        if isinstance(func, ast.Name) and func.id == "str" and len(node.args) == 1:
            return self.values(node.args[0], scope)
        is_self_call = isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) and func.value.id == "self"
        if not (isinstance(func, ast.Name) or is_self_call):
            raise UnreadableValue(f"{scope.source.name}:{node.lineno}: {ast.unparse(node)}")
        name = func.id if isinstance(func, ast.Name) else func.attr  # type: ignore[union-attr]
        if name in MAPPERS:
            table = scope.source.module.BACKEND_ERROR_TO_PUBLIC_ERROR
            return frozenset(table.get(value, value) for value in self.values(node.args[0], scope))
        definitions = scope.source.functions.get(name)
        if not definitions or len(definitions) != 1:
            raise UnreadableValue(f"{scope.source.name}:{node.lineno}: {ast.unparse(node)}")
        function = definitions[0]
        bindings = []
        for parameter in _parameters(function):
            if parameter.arg == "self":
                continue
            argument = _argument(node, function, parameter.arg)
            if argument is None:
                continue
            try:
                bindings.append((parameter.arg, self.values(argument, scope)))
            except UnreadableValue:
                # A parameter the callee never returns is free to be unreadable.
                continue
        return self._returns(scope.source, function, tuple(bindings))

    def _returns(self, source: Source, function: ast.FunctionDef, bindings: tuple[tuple[str, frozenset[str | None]], ...]) -> frozenset[str | None]:
        key = (source.name, function.lineno, "<return>")
        if key in self._active:
            return frozenset()
        self._active.add(key)
        try:
            callee = Scope(source, function, bindings)
            values: frozenset[str | None] = frozenset()
            for node in ast.walk(function):
                if isinstance(node, ast.Return) and node.value is not None:
                    values |= self.values(node.value, callee)
            return values
        finally:
            self._active.discard(key)


# The fields a debug refusal's type is read from. `target_error_type` is what a
# session that started over a target stopped abnormally reports, and the test
# reactor publishes it as its own step's `error_type` (test_reactor.py), so the
# entry for it is read on a `test_reactor_run` result as well.
ERROR_TYPE_FIELDS = frozenset({"error_type", "target_error_type"})


def _names_an_error_type_field(target: ast.expr) -> bool:
    return isinstance(target, ast.Subscript) and isinstance(target.slice, ast.Constant) and target.slice.value in ERROR_TYPE_FIELDS


def _is_tool_error_call(node: ast.Call) -> bool:
    return isinstance(node.func, ast.Name) and node.func.id == "tool_error" and len(node.args) > 1


def _is_error_type_setdefault(node: ast.Call) -> bool:
    return isinstance(node.func, ast.Attribute) and node.func.attr == "setdefault" and len(node.args) == 2 and isinstance(node.args[0], ast.Constant) and node.args[0].value in ERROR_TYPE_FIELDS


def error_type_expressions(source: Source) -> Iterator[ast.expr]:
    """Every expression `source` writes as an error type: a dict value, a keyword,
    a subscript assignment with or without an annotation, a `setdefault`, and
    the error type `tool_error` is called with."""
    for node in ast.walk(source.tree):
        if isinstance(node, ast.Dict):
            yield from (value for key, value in zip(node.keys, node.values, strict=True) if isinstance(key, ast.Constant) and key.value in ERROR_TYPE_FIELDS)
        elif isinstance(node, ast.keyword) and node.arg in ERROR_TYPE_FIELDS:
            yield node.value
        elif isinstance(node, ast.Assign):
            yield from (node.value for target in node.targets if _names_an_error_type_field(target))
        elif isinstance(node, ast.AnnAssign) and node.value is not None and _names_an_error_type_field(node.target):
            yield node.value
        elif isinstance(node, ast.Call) and (_is_tool_error_call(node) or _is_error_type_setdefault(node)):
            yield node.args[1]


# The scope argument that names the backend answering. A merge under it is the
# same lookup the service's fill performs, so it is read the same way.
BACKEND_NAME_EXPRESSIONS = frozenset({"self.backend_name", "backend_name"})


def _merge(node: ast.expr, scope: Scope, backend: str | None = None) -> tuple[frozenset[str | None], tuple[str | None, ...] | None] | None:
    """The types and scopes a `**remediation_fields(...)`-like call merges, or None when `node` is no such call.

    The scopes are None where the merge is under the backend's own name, which
    the site's place decides (`attach_scopes`).
    """
    if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
        return None
    if node.func.id == "exclusive_permission_fields":
        return frozenset({"permission_denied"}), (EXCLUSIVE_PERMISSION_SCOPE,)
    if node.func.id != "remediation_fields":
        return None
    types = Reader(backend=backend).values(node.args[0], scope)
    scope_node = node.args[1] if len(node.args) > 1 else next((keyword.value for keyword in node.keywords if keyword.arg == "scope"), None)
    if scope_node is None:
        return types, (None,)
    if ast.unparse(scope_node) in BACKEND_NAME_EXPRESSIONS:
        return types, None
    return types, tuple(sorted(Reader(keep_none=True, backend=backend).values(scope_node, scope), key=str))


def _merge_beside(source: Source, node: ast.expr, scope: Scope, backend: str | None = None) -> tuple[frozenset[str | None], tuple[str | None, ...] | None] | None:
    """A merge in the same dict literal, or the same call, that writes the site."""
    parent = source.parents.get(node)
    if isinstance(parent, ast.keyword):
        parent = source.parents.get(parent)
    if isinstance(parent, ast.Dict):
        spread = [value for key, value in zip(parent.keys, parent.values, strict=True) if key is None]
    elif isinstance(parent, ast.Call):
        spread = [keyword.value for keyword in parent.keywords if keyword.arg is None]
    else:
        return None
    merges = [merge for merge in (_merge(value, scope, backend) for value in spread) if merge is not None]
    if not merges:
        return None
    assert len(merges) == 1, f"{source.name}:{node.lineno}: more than one remediation merged beside one error type"
    return merges[0]


def _merges_in_function(function: ast.FunctionDef | None, scope: Scope, backend: str | None = None) -> list[tuple[frozenset[str | None], tuple[str | None, ...] | None]]:
    """Every `<result>.update(remediation_fields(...))` the function makes."""
    if function is None:
        return []
    merges = []
    for node in ast.walk(function):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "update" and len(node.args) == 1:
            merge = _merge(node.args[0], scope, backend)
            if merge is not None:
                merges.append(merge)
    return merges


def attach_scopes(source: Source, node: ast.expr, function_name: str, backend: str | None = None) -> tuple[str | None, ...]:
    """The scope a refusal that merges nothing is filled under: the backend its result names.

    `backend` is the one whose session a `GdbDebugSessions` site is read for;
    a site read for no one backend is every session backend's.
    """
    if source.module is tools:
        # The service's own refusals name no backend; the bare key serves them.
        return (None,)
    for name, module in BACKEND_MODULES.items():
        if source.module is module:
            return (name,)
    if source.module is gdbdebug:
        if source.enclosing_class(node) == GDBDEBUG_SESSION_CLASS:
            return (backend,) if backend is not None else GDBDEBUG_SESSION_SCOPES
        if function_name in GDBDEBUG_FUNCTION_SCOPES:
            return GDBDEBUG_FUNCTION_SCOPES[function_name]
        if function_name == "<module>" and module_constant(source, node) in GDBDEBUG_CONSTANT_SCOPES:
            return GDBDEBUG_CONSTANT_SCOPES[module_constant(source, node)]  # type: ignore[index]
    if source.module is common and function_name in COMMON_FUNCTION_SCOPES:
        return COMMON_FUNCTION_SCOPES[function_name]
    raise AssertionError(f"{source.name}:{node.lineno}: an error_type in {function_name}, which no scope is pinned for")


def module_constant(source: Source, node: ast.AST) -> str | None:
    """The name of the module-level assignment `node` is written in, or None."""
    while node in source.parents:
        node = source.parents[node]
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            return None
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and source.parents.get(node) is source.tree:
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names = [target.id for target in targets if isinstance(target, ast.Name)]
            return names[0] if len(names) == 1 == len(targets) else None
    return None


def is_a_debug_site(source: Source, node: ast.expr) -> bool:
    """Whether a refusal built at `node` is this area's. Only the tool service holds other areas' sites."""
    if source.module is not tools:
        return True
    function = source.enclosing_function(node)
    function_name = function.name if function is not None else "<module>"
    if function_name in TOOLS_OTHER_AREAS:
        return False
    if function_name not in TOOLS_DEBUG_FUNCTIONS:
        raise AssertionError(f"tools.py:{node.lineno}: an error_type in {function_name}, which is neither a debug path nor named for another area")
    return True


def scopes_of_site(source: Source, node: ast.expr, values: frozenset[str | None], backend: str | None = None) -> tuple[str | None, ...]:
    """Whose remediation a refusal built at `node`, for `backend`'s session where it is a session's, is looked up under."""
    function = source.enclosing_function(node)
    function_name = function.name if function is not None else "<module>"
    scope = Scope(source, function)
    beside = _merge_beside(source, node, scope, backend)
    if beside is not None:
        types, scopes = beside
        assert types == values, f"{source.name}:{node.lineno}: merges the remediation of {sorted(types, key=str)} beside {sorted(values, key=str)}"
        return scopes if scopes is not None else attach_scopes(source, node, function_name, backend)
    matching = [scopes for types, scopes in _merges_in_function(function, scope, backend) if values <= types]
    if matching:
        return tuple(sorted({one for scopes in matching for one in (scopes if scopes is not None else attach_scopes(source, node, function_name, backend))}, key=str))
    return attach_scopes(source, node, function_name, backend)


# The debugger front end, the GDB/MI client and the ELF reader build no refusal
# today; scanning them makes one that appears later fail `attach_scopes`.
SCANNED_MODULES: tuple[ModuleType, ...] = (gdbdebug, openocd, pyocd, stlink, common, tools, debugger, gdbmi, elfsymbols)


def scan(planted: Mapping[ModuleType, str] = MappingProxyType({})) -> dict[Pair, tuple[str, ...]]:
    """Every (error_type, scope) pair the debug paths can answer, with the `file:line` sites.

    `planted` replaces a module's source with the text given, which is how the
    tests below prove that each shape of refusal reaches the inventory.
    """
    readers = {backend: Reader(backend=backend) for backend in (None, *GDBDEBUG_SESSION_SCOPES)}
    sites: dict[Pair, list[str]] = {}
    unreadable: list[str] = []
    for module in SCANNED_MODULES:
        source = Source.of(module, planted[module]) if module in planted else source_of(module)
        for node in error_type_expressions(source):
            if not is_a_debug_site(source, node):
                continue
            # A session's site is read once for each backend that opens sessions.
            in_a_session = source.module is gdbdebug and source.enclosing_class(node) == GDBDEBUG_SESSION_CLASS
            for backend in GDBDEBUG_SESSION_SCOPES if in_a_session else (None,):
                try:
                    values = readers[backend].values(node, Scope(source, source.enclosing_function(node)))
                except UnreadableValue as error:
                    unreadable.append(f"{source.name}:{node.lineno}: {ast.unparse(node)} ({error})")
                    break
                scopes = scopes_of_site(source, node, values, backend)
                for value in values:
                    assert value is not None
                    for scope in scopes:
                        sites.setdefault((value, scope), []).append(f"{source.name}:{node.lineno}")
    assert not unreadable, f"error_type values the scan cannot read: {unreadable}"
    return {pair: tuple(sorted(set(lines))) for pair, lines in sites.items()}


@cache
def scanned_debug_pairs() -> dict[Pair, tuple[str, ...]]:
    return scan()


# ---------------------------------------------------------------------------
# The inventory, pinned, and what is left out of it with the reason.

# Every pair the scan reads, so a scan that silently stops reading a site, or a
# refusal added to the source, is a change to this table.
INVENTORY_BY_TYPE: dict[str, tuple[str | None, ...]] = {
    "adapter_access_denied": ("openocd",),
    "adapter_not_found": BACKENDS,
    # ST-LINK_gdbserver's own refusals at a session start (stlink.py).
    "adapter_usb_error": ("stlink",),
    "artifact_validation_failed": (None,),
    "audit_broken": BACKENDS,
    "audit_unavailable": ("pyocd",),
    "breakpoint_reconciliation_failed": BACKENDS,
    "breakpoints_not_removed": ("pyocd", "stlink"),
    "cleanup_failed": BACKENDS,
    "cleanup_required": (None,),
    "config_file_not_found": ("openocd", "stlink"),
    "debug_session_setup_failed": BACKENDS,
    "debugger_command_rejected": ("openocd",),
    "debugger_config_not_found": ("openocd",),
    "debugger_error": BACKENDS,
    # common.py merges the bare entry beside it, for every backend alike.
    "debugger_not_executable": (None,),
    "debugger_not_found": BACKENDS,
    "detach_resume_not_confirmed": BACKENDS,
    "flash_erase_failed": BACKENDS,
    "flash_failed": BACKENDS,
    "gdb_async_unsupported": BACKENDS,
    # A missing GDB merges under the GDB's state, not the backend's (gdbdebug.py).
    "gdb_not_found": (None, gdbdebug.GDB_AUTODETECTED_MISSING_SCOPE, gdbdebug.GDB_NOT_CONFIGURED_SCOPE),
    "gdb_start_failed": BACKENDS,
    "halt_not_confirmed": BACKENDS,
    "interface_config_not_found": ("openocd",),
    "invalid_argument": (None, *BACKENDS),
    "memory_read_failed": BACKENDS,
    # The tool service merges a scope of its own for a call no probe can be
    # routed to: no debugger bound, or no probe named among several.
    "not_supported": ("openocd", "openocd_probe_selection", "pyocd", "stlink", "unbound_debugger", "unnamed_probe"),
    "output_write_failed": BACKENDS,
    # The permission helpers merge the bare entry, or a scope of their own: the
    # execution grant a debug_continue needs, and a granted key that blocks.
    "permission_denied": (None, "allow_debug_execution", EXCLUSIVE_PERMISSION_SCOPE, *BACKENDS),
    "probe_discovery_failed": BACKENDS,
    "probe_server_open_failed": ("stlink",),
    "reset_failed": BACKENDS,
    "resource_busy": (None,),
    "resource_quarantined": BACKENDS,
    "session_already_active": BACKENDS,
    # gdbdebug.py merges the bare entry beside it, true for every kind of session.
    "session_not_active": (None,),
    "stop_reason_not_available": BACKENDS,
    "symbol_ambiguous": BACKENDS,
    "symbol_not_found": BACKENDS,
    "symbol_resolution_failed": BACKENDS,
    "symbol_source_changed": ("pyocd", "stlink"),
    "symbol_source_not_available": ("pyocd", "stlink"),
    "target_config_not_found": ("openocd",),
    "target_exception": BACKENDS,
    "target_not_detected": BACKENDS,
    "target_state_unconfirmed": ("openocd", "stlink"),
    "target_type_invalid": ("pyocd",),
    "timeout": BACKENDS,
    "unexpected_breakpoint": BACKENDS,
    "verify_failed": BACKENDS,
}
INVENTORY = frozenset((error_type, scope) for error_type, scopes in INVENTORY_BY_TYPE.items() for scope in scopes)

# Pairs whose entry another change writes. The guard leaves them to it.
OWNED_ELSEWHERE: dict[Pair, str] = {
    ("session_not_active", None): "#635 writes one bare entry, true for the COM, CAN and debug sessions alike",
    ("resource_busy", None): "#646 writes the coordination refusals; `_coordinated_debug_call` answers with the lease's",
    ("cleanup_required", None): "#645 writes the dispatcher's refusals, and this one is the dispatcher's",
    ("artifact_validation_failed", None): "#645 writes the artifact refusals; `debug_start_session` refuses with the validator's word",
    ("audit_unavailable", "pyocd"): "#645 writes one bare entry for every module that loses its audit trail",
    ("not_supported", "unbound_debugger"): "#645 writes the scope the tool service merges for a call no debugger is bound to",
    ("not_supported", "unnamed_probe"): "#645 writes the scope the tool service merges for a call that names no probe among several",
}

# Pairs #516 decided stay silent until somebody writes that tool's own steps,
# pinned silent by test_debug_backend_refusals.py, which also pins that none
# of the three types grows a bare entry the service's fill could fall back to.
SILENT: frozenset[Pair] = frozenset((bucket, backend) for bucket, backend in SILENT_PAIRS)

# Values the scan reads that never reach a tool result. None does: every site
# above builds the top-level `error_type` of a result some tool returns.
NEVER_REACHED: dict[Pair, str] = {}

EXCLUDED: frozenset[Pair] = frozenset(OWNED_ELSEWHERE) | SILENT | frozenset(NEVER_REACHED)

# Pairs the bare key cannot answer, each with why: the scoped key has to exist.
OWN_KEY_REQUIRED: dict[tuple[str, str], str] = {
    ("config_file_not_found", "openocd"): "the bare entry is about the Agentic HIL configuration file, and this is an OpenOCD script the debug server could not find",
    ("not_supported", "openocd"): "no bare `not_supported` is true for every refusal of that name, and the other backends' keys are about debug sessions",
    **{("audit_broken", backend): "the bare key is the coordination ledger's, which #646 writes; this is the debug session's own evidence" for backend in BACKENDS},
    **{("debugger_not_found", backend): "the executable that is missing, and where it comes from, is each backend's own" for backend in BACKENDS},
    **{("timeout", backend): "a GDB/MI session that stopped answering and a command-line tool that ran out of time are read differently" for backend in BACKENDS},
}

# Bare keys this area writes. Each is what every producer of the type falls
# back to when it names no backend: probe discovery in bootstrap.py, a test
# reactor step result, the result text's advice line. So each is required at
# its own URI, apart from any scoped key beside it.
BARE_KEY_REQUIRED: dict[str, str] = {
    "cleanup_failed": "the debug session's and the runs' alike; #646 adds only `cleanup_failed:test_reactor` beside it",
    "debugger_error": "a test reactor step passes the backend's type up without the backend",
    "reset_failed": "a test reactor step passes the backend's type up without the backend",
    "probe_discovery_failed": "probe discovery in bootstrap.py answers it with no backend and attaches the bare advice",
    "output_write_failed": "a test reactor step passes the backend's type up without the backend",
    "debugger_not_found": "probe discovery in bootstrap.py answers it with no backend and attaches the bare advice",
    "target_exception": "the test reactor publishes a session's target_error_type as its step's error_type",
    "unexpected_breakpoint": "the test reactor publishes a session's target_error_type as its step's error_type",
    "symbol_not_found": "a test reactor step passes the backend's type up without the backend",
    "symbol_resolution_failed": "a test reactor step passes the backend's type up without the backend",
    "symbol_ambiguous": "a test reactor step passes the backend's type up without the backend",
    "symbol_source_changed": "a test reactor step passes the backend's type up without the backend",
    "symbol_source_not_available": "a test reactor step passes the backend's type up without the backend",
}

# Every key this change writes. A pair written here resolves to one of them.
WRITTEN_KEYS: frozenset[str] = frozenset(
    {
        *(f"{error_type}:{backend}" for error_type in ("timeout", "debugger_not_found") for backend in BACKENDS),
        "config_file_not_found:openocd",
        "not_supported:openocd",
        *(f"audit_broken:{backend}" for backend in BACKENDS),
        *BARE_KEY_REQUIRED,
        "adapter_access_denied",
        "breakpoint_reconciliation_failed",
        "breakpoints_not_removed",
        "debug_session_setup_failed",
        "detach_resume_not_confirmed",
        "gdb_async_unsupported",
        "gdb_start_failed",
        "halt_not_confirmed",
        "interface_config_not_found",
        "session_already_active",
        "stop_reason_not_available",
        "target_config_not_found",
    }
)

# The pairs the catalogue answered nothing for when this was written, or
# answered with an entry about something else, and which this change writes.
WRITTEN_HERE: frozenset[Pair] = frozenset(
    {
        ("adapter_access_denied", "openocd"),
        ("breakpoint_reconciliation_failed", "openocd"),
        ("cleanup_failed", "openocd"),
        ("config_file_not_found", "openocd"),
        ("debug_session_setup_failed", "openocd"),
        ("detach_resume_not_confirmed", "openocd"),
        ("gdb_async_unsupported", "openocd"),
        ("gdb_start_failed", "openocd"),
        ("halt_not_confirmed", "openocd"),
        ("interface_config_not_found", "openocd"),
        ("not_supported", "openocd"),
        ("session_already_active", "openocd"),
        ("stop_reason_not_available", "openocd"),
        ("symbol_ambiguous", "openocd"),
        ("target_config_not_found", "openocd"),
        ("target_exception", "openocd"),
        ("unexpected_breakpoint", "openocd"),
        *((error_type, backend) for backend in BACKENDS for error_type in ("debugger_error", "debugger_not_found", "output_write_failed", "probe_discovery_failed", "reset_failed", "symbol_not_found", "symbol_resolution_failed", "timeout")),
        *((error_type, backend) for backend in ("pyocd", "stlink") for error_type in ("symbol_source_changed", "symbol_source_not_available")),
        # The debug sessions those two open (#624).
        *(("breakpoints_not_removed", backend) for backend in ("pyocd", "stlink")),
        *(("audit_broken", backend) for backend in BACKENDS),
    }
)


def missing_entries() -> list[str]:
    sites = scanned_debug_pairs()
    return [f"{error_type} under {scope}: {' '.join(sites.get((error_type, scope), ()))}" for error_type, scope in sorted(INVENTORY - EXCLUDED, key=str) if lookup_remedy(error_type, scope) is None]


def catalogue_key(error_type: str, scope: str | None) -> str:
    """The key a refusal of `error_type` under `scope` is answered from: the scoped one where it exists."""
    return f"{error_type}:{scope}" if scope and f"{error_type}:{scope}" in ERROR_CATALOGUE else error_type


# ---------------------------------------------------------------------------
# The guard.


def test_the_scan_reads_the_pinned_inventory() -> None:
    scanned = scanned_debug_pairs()

    assert sorted(set(scanned) - INVENTORY, key=str) == [], {pair: scanned[pair] for pair in set(scanned) - INVENTORY}
    assert sorted(INVENTORY - set(scanned), key=str) == []


def test_every_left_out_pair_is_one_the_debug_paths_answer() -> None:
    assert EXCLUDED <= INVENTORY, sorted(EXCLUDED - INVENTORY, key=str)
    assert WRITTEN_HERE <= INVENTORY, sorted(WRITTEN_HERE - INVENTORY, key=str)
    assert frozenset(OWN_KEY_REQUIRED) <= WRITTEN_HERE, sorted(frozenset(OWN_KEY_REQUIRED) - WRITTEN_HERE, key=str)
    assert not (WRITTEN_HERE & EXCLUDED), sorted(WRITTEN_HERE & EXCLUDED, key=str)
    for bare in BARE_KEY_REQUIRED:
        assert any(error_type == bare for error_type, _scope in WRITTEN_HERE), bare


def test_every_debug_refusal_has_a_catalogue_entry() -> None:
    assert missing_entries() == [], "debug refusals without an ERROR_CATALOGUE entry"


def test_every_pair_written_here_resolves_to_a_key_written_here() -> None:
    assert sorted((pair, catalogue_key(*pair)) for pair in WRITTEN_HERE if catalogue_key(*pair) not in WRITTEN_KEYS) == []


@pytest.mark.parametrize(("error_type", "scope"), sorted(OWN_KEY_REQUIRED), ids=[f"{error_type}:{scope}" for error_type, scope in sorted(OWN_KEY_REQUIRED)])
def test_a_refusal_the_bare_key_cannot_answer_has_its_own(error_type: str, scope: str) -> None:
    assert f"{error_type}:{scope}" in ERROR_CATALOGUE, OWN_KEY_REQUIRED[(error_type, scope)]


# ---------------------------------------------------------------------------
# The scan reads every shape it claims to, proven on planted source.


def planted(module: ModuleType, anchor: str, insertion: str) -> dict[ModuleType, str]:
    """`module`'s source with `insertion` after the one occurrence of `anchor`."""
    text = Path(str(module.__file__)).read_text(encoding="utf-8")
    assert text.count(anchor) == 1, anchor
    return {module: text.replace(anchor, anchor + insertion)}


def appended(module: ModuleType, insertion: str) -> dict[ModuleType, str]:
    return {module: Path(str(module.__file__)).read_text(encoding="utf-8") + insertion}


PLANTED = "planted_refusal"


def test_nothing_is_planted_in_the_source() -> None:
    assert [pair for pair in scanned_debug_pairs() if pair[0] == PLANTED] == []


def test_a_refusal_planted_in_the_debug_permission_gate_reaches_the_inventory() -> None:
    """`_dispatch_tool` returns this helper's answer before anything else is asked (tools.py)."""
    pairs = scan(
        planted(
            tools,
            "    def _debug_permission_failure(self, name: str, args: JsonObject) -> JsonObject | None:\n",
            f'        if args.get("planted"):\n            return {{"ok": False, "tool": name, "error_type": "{PLANTED}"}}\n',
        )
    )

    assert (PLANTED, None) in pairs
    assert (PLANTED, None) not in INVENTORY


def test_a_tool_service_function_nobody_classified_fails_the_scan() -> None:
    with pytest.raises(AssertionError, match="planted_helper"):
        scan(appended(tools, f'\n\ndef planted_helper(tool):\n    return {{"ok": False, "tool": tool, "error_type": "{PLANTED}"}}\n'))


def test_an_annotated_subscript_assignment_reaches_the_inventory() -> None:
    pairs = scan(
        planted(
            gdbdebug,
            '    def start_session(self, artifact: JsonObject, mode: str = "attach", timeout_s: float | None = None) -> JsonObject:\n',
            f'        planted: JsonObject = {{}}\n        planted["error_type"]: str = "{PLANTED}"\n',
        )
    )

    assert (PLANTED, "openocd") in pairs


def test_a_setdefault_reaches_the_inventory_under_its_backend() -> None:
    pairs = scan(appended(stlink, f'\n\ndef planted_helper(result):\n    result.setdefault("error_type", "{PLANTED}")\n    return result\n'))

    assert (PLANTED, "stlink") in pairs


def test_a_merge_under_a_scope_of_its_own_is_read_under_that_scope() -> None:
    pairs = scan(appended(openocd, f'\n\ndef planted_helper(tool):\n    return {{"ok": False, "tool": tool, "error_type": "{PLANTED}", **remediation_fields("{PLANTED}", "planted_scope")}}\n'))

    assert (PLANTED, "planted_scope") in pairs
    assert (PLANTED, "openocd") not in pairs


def test_a_merge_of_another_types_advice_beside_a_refusal_fails_the_scan() -> None:
    with pytest.raises(AssertionError, match="merges the remediation of"):
        scan(appended(openocd, f'\n\ndef planted_helper(tool):\n    return {{"ok": False, "tool": tool, "error_type": "{PLANTED}", **remediation_fields("timeout", "planted_scope")}}\n'))


# ---------------------------------------------------------------------------
# The facts the scan's scopes rest on.


# The classifier each backend constructs its sessions with, which is what
# `_start_failure` reads a dead server's output with.
SESSION_CLASSIFIERS = {
    "openocd.py": "self._classify_output",
    "pyocd.py": "lambda output: self._classify_output(output, 'debug_start_session')",
    "stlink.py": "classify_gdb_server_output",
}
# The steps each backend constructs its sessions with: its own server's.
SESSION_SERVER_STEPS = {"openocd.py": "OPENOCD_GDB_SERVER_STEPS", "pyocd.py": "PYOCD_GDB_SERVER_STEPS", "stlink.py": "ST_LINK_GDB_SERVER_STEPS"}


def server_steps_of(backend: str) -> gdbdebug.GdbServerSteps:
    """The `GdbServerSteps` `backend`'s sessions run on (pinned below)."""
    return getattr(BACKEND_MODULES[backend], SESSION_SERVER_STEPS[f"{backend}.py"])


def test_typed_debug_sessions_are_built_by_every_backend_under_its_own_name() -> None:
    """Why a refusal `GdbDebugSessions` builds is looked up under the backend whose session it is (#624)."""
    constructions = []
    for module in (*BACKEND_MODULES.values(), gdbdebug, common, tools, debugger):
        source = source_of(module)
        constructions.extend((source.name, node) for node in ast.walk(source.tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == GDBDEBUG_SESSION_CLASS)

    assert sorted(name for name, _node in constructions) == sorted(f"{backend}.py" for backend in GDBDEBUG_SESSION_SCOPES)
    for name, construction in constructions:
        keywords = {keyword.arg: ast.unparse(keyword.value) for keyword in construction.keywords}
        assert keywords["backend_name"] == "self.backend_name", (name, keywords)
        assert keywords["classify_server_output"] == SESSION_CLASSIFIERS[name], (name, keywords)
        assert keywords["server_steps"] == SESSION_SERVER_STEPS[name], (name, keywords)
        # pyOCD and stlink read a dead server's output themselves (below);
        # OpenOCD may answer None, leaving its classification standing.
        assert keywords["read_start_failure"] == "self._debug_start_failure", (name, keywords)


@pytest.mark.parametrize("backend", ["pyocd", "stlink"])
def test_a_start_failure_on_pyocd_and_stlink_is_named_by_the_backends_own_reading(backend: str) -> None:
    """Why `_start_failure`'s own classification reaches no result on these two
    (`PINNED_EXPRESSIONS`): their reading returns one dict, which always names
    an error type, and the session merges it over the classification."""
    (function,) = source_of(BACKEND_MODULES[backend]).functions["_debug_start_failure"]
    returns = [node for node in ast.walk(function) if isinstance(node, ast.Return)]
    built = [node for node in ast.walk(function) if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == "result"]

    assert [ast.unparse(node) for node in returns] == ["return result"]
    assert len(built) == 1 and isinstance(built[0].value, ast.Dict), [ast.unparse(node) for node in built]
    assert "error_type" in [key.value for key in built[0].value.keys if isinstance(key, ast.Constant)]
    assert not [node for node in ast.walk(function) if isinstance(node, ast.Delete)]
    (start_failure,) = source_of(gdbdebug).functions["_start_failure"]
    assert "result.update(read)" in [ast.unparse(node) for node in ast.walk(start_failure) if isinstance(node, ast.Call)]


def test_where_gdbs_detach_keeps_the_core_halted_the_breakpoint_removal_needs_no_command() -> None:
    """Why a stop on OpenOCD never answers `breakpoints_not_removed` (`PINNED_EXPRESSIONS`).

    On a server with a detach guard command, GDB's detach carries the removal,
    so the removal before the end answers proven before it sends anything. Of
    the three servers, only OpenOCD's has that command."""
    (function,) = source_of(gdbdebug).functions["_remove_breakpoints_before_end"]
    docstring, guard, *_rest = function.body

    assert isinstance(docstring, ast.Expr) and isinstance(docstring.value, ast.Constant), ast.unparse(docstring)
    assert isinstance(guard, ast.If) and not guard.orelse, ast.unparse(guard)
    assert ast.unparse(guard.test) == "self._server_steps.detach_guard_command is not None or not session.breakpoints"
    assert [ast.unparse(statement) for statement in guard.body] == ["return True"]
    assert [backend for backend in BACKENDS if server_steps_of(backend).detach_guard_command is not None] == ["openocd"]


def calls_reaching(function_name: str, seen: frozenset[str] = frozenset()) -> Iterator[tuple[str, ast.Call]]:
    """Each call that reaches the module function `function_name`, with the backend it is made for.

    A call in a backend module is that backend's; a call inside
    `GdbDebugSessions` is every backend's that opens sessions; a call inside
    another module function of gdbdebug.py is whatever reaches that one. A call
    anywhere else fails.
    """
    for module in SCANNED_MODULES:
        source = source_of(module)
        for node in ast.walk(source.tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == function_name):
                continue
            caller = source.enclosing_function(node)
            if module in BACKEND_MODULES.values():
                yield module.__name__.rpartition(".")[2], node
            elif module is gdbdebug and source.enclosing_class(node) == GDBDEBUG_SESSION_CLASS:
                yield from ((backend, node) for backend in GDBDEBUG_SESSION_SCOPES)
            elif module is gdbdebug and caller is not None and source.enclosing_class(node) is None:
                if caller.name not in seen:
                    yield from ((backend, node) for backend, _call in calls_reaching(caller.name, seen | {function_name}))
            else:
                raise AssertionError(f"{source.name}:{node.lineno}: {function_name} called where no backend is named")


def backends_reaching(function_name: str) -> frozenset[str]:
    return frozenset(backend for backend, _call in calls_reaching(function_name))


@pytest.mark.parametrize("function_name", sorted(GDBDEBUG_FUNCTION_SCOPES))
def test_a_shared_gdb_function_is_reached_by_the_backends_named_for_it(function_name: str) -> None:
    """Why a refusal built in a module function of gdbdebug.py is looked up under the backends `GDBDEBUG_FUNCTION_SCOPES` names."""
    assert backends_reaching(function_name) == frozenset(GDBDEBUG_FUNCTION_SCOPES[function_name])


@pytest.mark.parametrize("function_name", sorted(COMMON_FUNCTION_SCOPES))
def test_a_shared_refusal_builder_answers_under_the_backend_that_calls_it(function_name: str) -> None:
    """Why a refusal common.py builds is looked up under the backends `COMMON_FUNCTION_SCOPES` names:
    those call it, and each hands it its own name, which is the `backend` the result carries."""
    calls = list(calls_reaching(function_name))

    assert frozenset(backend for backend, _call in calls) == frozenset(COMMON_FUNCTION_SCOPES[function_name])
    assert sorted({ast.unparse(call.args[0]) for _backend, call in calls}) == ["self.backend_name"]
    (function,) = source_of(common).functions[function_name]
    assert _parameters(function)[0].arg == "backend_name"


@pytest.mark.parametrize("backend", BACKENDS)
def test_each_backend_publishes_what_its_own_table_maps(backend: str) -> None:
    """What `MAPPERS` reads into the scan: the backend's table, applied and nothing else."""
    (function,) = source_of(BACKEND_MODULES[backend]).functions["_public_error_type"]

    assert [ast.unparse(statement) for statement in function.body] == ["return BACKEND_ERROR_TO_PUBLIC_ERROR.get(backend_error_type, backend_error_type)"]


# ---------------------------------------------------------------------------
# Resolution at the URI.


@pytest.fixture(scope="module")
def reference(tmp_path_factory: pytest.TempPathFactory) -> Iterator[AgenticHILToolService]:
    service = AgenticHILToolService(load_config(str(write_config(tmp_path_factory.mktemp("reference")))), frontend="mcp")
    try:
        yield service
    finally:
        closed(service)


def read_resource(service: AgenticHILToolService, key: str) -> dict:
    uri = ERROR_URI_PREFIX + key
    response = handle_mcp_message({"jsonrpc": "2.0", "id": 1, "method": "resources/read", "params": {"uri": uri}}, service)

    assert isinstance(response, dict), response
    assert "error" not in response, response
    contents = response["result"]["contents"]
    assert [content["uri"] for content in contents] == [uri]
    return json.loads(contents[0]["text"])


RESOLVED = sorted(INVENTORY - EXCLUDED, key=str)


@pytest.mark.parametrize(("error_type", "scope"), RESOLVED, ids=[f"{error_type}-{scope}" for error_type, scope in RESOLVED])
def test_the_reference_resolves_every_debug_refusal(reference: AgenticHILToolService, error_type: str, scope: str | None) -> None:
    key = catalogue_key(error_type, scope)
    entry = read_resource(reference, key)

    assert entry == catalogue_entry(key)
    assert entry["error_type"] == error_type
    assert entry["meaning"].strip(), entry
    assert [step for step in entry["remediation"] if step.strip()], entry


@pytest.mark.parametrize("error_type", sorted(BARE_KEY_REQUIRED))
def test_the_bare_entries_this_area_writes_resolve_at_their_own_uri(reference: AgenticHILToolService, error_type: str) -> None:
    """The exact bare key, not the scoped lookup's fallback: resources/read takes the key as it is given."""
    assert error_type in ERROR_CATALOGUE, BARE_KEY_REQUIRED[error_type]
    entry = read_resource(reference, error_type)

    assert entry == catalogue_entry(error_type)
    assert "scope" not in entry, entry


# ---------------------------------------------------------------------------
# What each entry written here says, clause by clause.

NEGATION = re.compile(r"\b(not|cannot|never|no|nothing|without)\b|n't\b", re.IGNORECASE)
REPETITION = re.compile(r"\b(again|retry|retried|retrying|repeat|repeated|repeating|rerun)\b", re.IGNORECASE)
CLAUSE_BREAK = re.compile(r"(?<=[.!?;:])\s+|,\s+(?=(?:and|but|then|so|or|not|because|which|while|until)\b)|\s+but\s+", re.IGNORECASE)
RETRY = REPETITION.pattern


def clauses(text: str) -> list[str]:
    return [clause for clause in CLAUSE_BREAK.split(text) if clause and clause.strip()]


def affirmative(clause: str) -> bool:
    return NEGATION.search(clause) is None


def affirmative_clauses(text: str) -> list[str]:
    return [clause for clause in clauses(text) if affirmative(clause)]


def first_affirmative_match(steps: list[str], pattern: str) -> tuple[int, int] | None:
    """Where `pattern` is first said affirmatively: (step, clause)."""
    for step_index, step in enumerate(steps):
        for clause_index, clause in enumerate(clauses(step)):
            if affirmative(clause) and re.search(pattern, clause, re.IGNORECASE):
                return step_index, clause_index
    return None


@dataclass(frozen=True)
class Contract:
    """What an entry has to say, read off the code that produces its refusals.

    `means`: patterns the meaning matches. `steps`: patterns an affirmative
    clause of the remediation matches, one each. `order`: patterns whose first
    affirmative mention comes in this order. `beside`: (a, b) where a step that
    says `a` affirmatively also names `b`, the condition it holds under.
    `avoid`: patterns a `do_not` element matches, one each. `never`: patterns
    no affirmative remediation clause may match.
    """

    means: tuple[str, ...] = ()
    steps: tuple[str, ...] = ()
    order: tuple[str, ...] = ()
    beside: tuple[tuple[str, str], ...] = ()
    avoid: tuple[str, ...] = ()
    never: tuple[str, ...] = ()


STATE_FIELDS = r"target_state|side_effect_status|target_contacted|halt_confirmed|cleanup_required|quarantined"
TIMEOUT_CEILING = r"debuggers\.\S+\.timeout_s"
# A clause naming debug_stop_session and a word that repeats it, in either order.
RETRIED_STOP = rf"(?=.*debug_stop_session)(?=.*(?:{RETRY}))"
# What a stop reports each teardown proof by (gdbdebug.py `stop_session`).
TEARDOWN_FIELDS = (r"halt_not_confirmed", r"breakpoints_removed_confirmed", r"detach_resume_guard_confirmed")

CONTRACTS: dict[str, Contract] = {
    # gdbdebug.py 640-649, 696, 1033, 1207, 1241, 1387; openocd.py 499, 982.
    # Every wait is `min(debuggers.<id>.timeout_s, cap)` and a call's own
    # timeout_s only shortens it (gdbdebug.py 240, 419-421, 684-686).
    "timeout:openocd": Contract(means=(r"\bOpenOCD\b", r"\bGDB\b"), steps=(STATE_FIELDS, TIMEOUT_CEILING, r"debug_halt"), order=(STATE_FIELDS, TIMEOUT_CEILING), avoid=(RETRY,)),
    # pyocd.py's command-line waits, the offline symbol read (gdbdebug.py), and
    # since #624 the debug session's: the server's ready line and every GDB/MI
    # command (gdbdebug.py, the same waits as on OpenOCD).
    "timeout:pyocd": Contract(means=(r"\bpyOCD\b", r"\bGDB\b", r"gdb_server_not_ready"), steps=(STATE_FIELDS, TIMEOUT_CEILING, r"debug_halt"), order=(STATE_FIELDS, TIMEOUT_CEILING), avoid=(RETRY,)),
    # stlink.py's command-line waits, the offline symbol read, and the session
    # on ST-LINK_gdbserver (#624).
    "timeout:stlink": Contract(means=(r"STM32CubeProgrammer", r"ST-LINK_gdbserver", r"gdb_server_not_ready"), steps=(STATE_FIELDS, TIMEOUT_CEILING, r"debug_halt"), order=(STATE_FIELDS, TIMEOUT_CEILING), avoid=(RETRY,)),
    # openocd.py OPENOCD_NOT_FOUND and the debug server spawn (gdbdebug.py 306).
    "debugger_not_found:openocd": Contract(means=(r"\bOpenOCD\b",), steps=(r"\bexecutable\b", r"\bPATH\b|install")),
    # The session start spawns `pyocd gdbserver` (gdbdebug.py, the debug server spawn).
    "debugger_not_found:pyocd": Contract(means=(r"\bpyOCD\b", r"debug server"), steps=(r"\bexecutable\b", r"\bPATH\b|install")),
    # stlink.py: no ST-LINK_gdbserver where a session start looks for one
    # (`backend_error_type` `gdb_server_not_found`), and the debug server spawn.
    "debugger_not_found:stlink": Contract(
        means=(r"STM32CubeProgrammer|STM32_Programmer_CLI", r"ST-LINK_gdbserver", r"gdb_server_not_found"), steps=(r"\bexecutable\b", r"\bPATH\b|install", r"gdb_server_executable")
    ),
    # The backends' tables and bootstrap.py 230, 331, 500, 793 (probe discovery).
    "debugger_not_found": Contract(steps=(r"\bexecutable\b", r"\bPATH\b|install")),
    # The raw name the session start publishes (decision: no behaviour change).
    "config_file_not_found:openocd": Contract(means=(r"OpenOCD",), steps=(r"interface_cfg|target_cfg|\.cfg", r"log_path|stderr|output"), never=(r"project_config_create",)),
    # openocd.py list_probes: an adapter with no USB identity to list.
    "not_supported:openocd": Contract(means=(r"OpenOCD",), steps=(r"probe_id",)),
    # gdbdebug.py 232: the session evidence latch; quarantined, refused until
    # resolved. The sessions on every backend share it (#624).
    **{f"audit_broken:{backend}": Contract(means=(r"audit|evidence",), steps=(r"operator",), avoid=(RETRY,)) for backend in BACKENDS},
    # The libusb refusal OpenOCD prints off Windows (openocd.py).
    "adapter_access_denied": Contract(means=(r"USB|libusb|adapter",), steps=(r"udev|group",), avoid=(r"\bsudo\b|\broot\b|administrator",)),
    # gdbdebug.py 566/575: cleanup_required, side_effect_status unknown; a
    # reconciled clear resolves the incident (tools.py, the debug quarantine).
    "breakpoint_reconciliation_failed": Contract(steps=(r"backend_reconciled",), beside=((r"debug_clear_breakpoints", RETRY),), avoid=(r"debug_continue",)),
    # gdbdebug.py 325: cleanup_confirmed (with the startup effect fields) or
    # cleanup_required with cleanup_error.
    "debug_session_setup_failed": Contract(beside=((r"debug_start_session", r"cleanup_confirmed|retry_safe"),), steps=(r"cleanup_required|cleanup_error",)),
    "gdb_start_failed": Contract(steps=(r"gdb_executable", r"cleanup_required|cleanup_error"), beside=((r"debug_start_session", r"cleanup_confirmed|retry_safe"),)),
    # gdbdebug.py 920: GDB refused mi-async before the target was contacted.
    "gdb_async_unsupported": Contract(means=(r"async",), steps=(r"gdb_executable",), avoid=(RETRY,)),
    "interface_config_not_found": Contract(steps=(r"interface_cfg",), never=(r"project_config_create",)),
    "target_config_not_found": Contract(steps=(r"target_cfg",), never=(r"project_config_create",)),
    # gdbdebug.py 227.
    "session_already_active": Contract(order=(r"debug_stop_session", r"debug_start_session")),
    # gdbdebug.py 711.
    "stop_reason_not_available": Contract(order=(r"debug_continue|debug_halt", r"debug_get_stop_reason")),
    # suggested_actions_for_stop, exception or fault (gdbdebug.py).
    "target_exception": Contract(
        order=(r"frame|exception_type", r"debug_symbol_value|debug_dump_symbol_ihex|memory", r"reset_target|debug_start_session"),
        avoid=(r"debug_continue",),
    ),
    # suggested_actions_for_stop, unexpected_breakpoint.
    "unexpected_breakpoint": Contract(steps=(r"debug_list_breakpoints|frame", r"debug_clear_breakpoints"), avoid=(r"debug_continue",)),
    # suggested_actions_for_stop, debugger_error: log_path and classify_last_error.
    "debugger_error": Contract(steps=(r"log_path|programmer_output", r"classify_last_error"), avoid=(RETRY,)),
    # The backends' reset classification and pyocd.py 414 (a confirmed flash
    # whose reset failed).
    "reset_failed": Contract(steps=(STATE_FIELDS, r"probe_target"), avoid=(r"flash_firmware|reset_target",)),
    # openocd.py 584, pyocd.py 324/1330-1336, stlink.py 324, bootstrap.py 242/345.
    "probe_discovery_failed": Contract(steps=(r"programmer_output|backend_error|summary", r"debugger_probes_list"), avoid=(r"probe_id",)),
    # gdbdebug.py 792, pyocd.py 556, stlink.py 540/560.
    "output_write_failed": Contract(steps=(r"output_path", r"backend_error", r"target_contacted")),
    "symbol_not_found": Contract(steps=(r"spell|name", r"ELF|build"), avoid=(r"allowed_symbols",)),
    "symbol_resolution_failed": Contract(steps=(r"symbol_table_lookup|backend_error|summary", r"debug_symbol_info"), avoid=(RETRY,)),
    "symbol_ambiguous": Contract(steps=(r"unique|rename|qualif|static",)),
    # gdbdebug.py 1759/1761: the flashed ELF changed or cannot be read.
    "symbol_source_changed": Contract(means=(r"digest|rebuilt|changed|replaced",), order=(r"flash_firmware", r"debug_symbol_info|debug_symbol_value|debug_dump_symbol_ihex")),
    # gdbdebug.py 1727/1733: only an ELF flashed through this service is read.
    "symbol_source_not_available": Contract(means=(r"ELF",), order=(r"flash_firmware", r"debug_symbol_info|debug_symbol_value|debug_dump_symbol_ihex"), steps=(r"\bELF\b",), avoid=(r"\.hex|\.bin",)),
    # gdbdebug.py 414-460: a retry repeats only the process cleanup, and only
    # after a cleanup-only failure (443-450); otherwise the proofs stay false.
    "cleanup_failed": Contract(
        means=TEARDOWN_FIELDS,
        steps=(r"cleanup_errors?", r"probe_target"),
        beside=tuple((r"debug_stop_session", field) for field in TEARDOWN_FIELDS),
        avoid=(r"debug_stop_session",),
    ),
    # gdbdebug.py 465-484 (#637): a retried stop cannot settle these.
    "halt_not_confirmed": Contract(steps=(r"probe_target",), avoid=(r"debug_stop_session",), never=(RETRIED_STOP,)),
    "detach_resume_not_confirmed": Contract(steps=(r"probe_target",), avoid=(r"debug_stop_session",), never=(RETRIED_STOP,)),
    # gdbdebug.py `_remove_breakpoints_before_end` (#624): on a server ended
    # before GDB detaches, the deletes and the list read back before the end;
    # the stop then holds the session like a halt it could not confirm.
    "breakpoints_not_removed": Contract(means=(r"breakpoints_removed_confirmed",), steps=(r"probe_target",), avoid=(r"debug_stop_session",), never=(RETRIED_STOP,)),
}

FORBIDDEN_CHARACTERS = tuple(chr(code) for code in (0x2013, 0x2014, 0x2192))


def test_every_key_written_here_has_a_contract() -> None:
    assert sorted(WRITTEN_KEYS ^ frozenset(CONTRACTS)) == []


@pytest.mark.parametrize("key", sorted(CONTRACTS))
def test_the_entry_says_what_its_producers_make_true(key: str) -> None:
    contract = CONTRACTS[key]
    entry = catalogue_entry(key)
    assert entry is not None, f"no entry under {key}"
    meaning: str = entry["meaning"]
    steps: list[str] = entry["remediation"]
    do_not: list[str] = entry.get("do_not", [])

    assert meaning.strip(), entry
    assert steps and all(step.strip() for step in steps), entry
    assert do_not and all(step.strip() for step in do_not), entry
    for text in (meaning, *steps, *do_not):
        assert not [character for character in FORBIDDEN_CHARACTERS if character in text], text
    for pattern in contract.means:
        assert re.search(pattern, meaning, re.IGNORECASE), (pattern, meaning)
    for pattern in contract.steps:
        assert first_affirmative_match(steps, pattern) is not None, (pattern, steps)
    positions = [first_affirmative_match(steps, pattern) for pattern in contract.order]
    assert None not in positions, (contract.order, steps)
    assert positions == sorted(positions), (contract.order, positions, steps)  # type: ignore[type-var]
    for said, condition in contract.beside:
        holding = [step for step in steps if any(re.search(said, clause, re.IGNORECASE) for clause in affirmative_clauses(step))]
        assert holding, (said, steps)
        assert all(re.search(condition, step, re.IGNORECASE) for step in holding), (said, condition, holding)
    for pattern in contract.avoid:
        assert any(re.search(pattern, step, re.IGNORECASE) for step in do_not), (pattern, do_not)
    for pattern in contract.never:
        assert first_affirmative_match(steps, pattern) is None, (pattern, steps)


# ---------------------------------------------------------------------------
# What settles a stop that could not confirm the target (#637).


def settles(step: str) -> bool:
    """Whether a step tells the reader, affirmatively, to call probe_target."""
    return any("probe_target" in clause for clause in affirmative_clauses(step))


def offers_a_retried_stop(step: str) -> bool:
    """Whether a step offers another `debug_stop_session`, affirmatively."""
    return any("debug_stop_session" in clause and REPETITION.search(clause) for clause in affirmative_clauses(step))


@pytest.mark.parametrize(
    ("step", "expected_settles", "expected_retry"),
    [
        ("Do not use probe_target. Retry debug_stop_session; it does not reset the target.", False, True),
        ("Call probe_target. Do not call debug_stop_session again; it cannot settle this.", True, False),
        ("Call probe_target; nothing else is needed.", True, False),
        ("Retry debug_stop_session; no reset is needed.", False, True),
        ("Never call probe_target.", False, False),
        ("Call debug_stop_session again, but not before probe_target.", False, True),
    ],
)
def test_the_settle_detector_reads_each_clause_on_its_own(step: str, expected_settles: bool, expected_retry: bool) -> None:
    assert settles(step) is expected_settles
    assert offers_a_retried_stop(step) is expected_retry


UNSETTLED_STOPS = [(proof.error_type, backend) for proof in gdbdebug._TEARDOWN_PROOFS for backend in INVENTORY_BY_TYPE[proof.error_type]]


@pytest.mark.parametrize(("error_type", "backend"), UNSETTLED_STOPS, ids=[f"{error_type}-{backend}" for error_type, backend in UNSETTLED_STOPS])
def test_an_unsettled_stop_names_the_call_that_settles_it(error_type: str, backend: str) -> None:
    """#637: a retried `debug_stop_session` forces every proof false and settles nothing
    (gdbdebug.py `stop_session`); the recovery probe_target runs first does."""
    advice = remediation_fields(error_type, backend)

    assert advice, f"no entry answers {error_type} under {backend}"
    assert any(settles(step) for step in advice["remediation"]), advice["remediation"]
    assert not [step for step in advice["remediation"] if offers_a_retried_stop(step)], advice["remediation"]


# How many proofs a stop takes, in words: `_TEARDOWN_PROOFS` holds three since #624.
PROOF_COUNTS = {2: r"\b(both|two)\s+(teardown\s+)?proofs\b", 3: r"\b(all three|three)\s+(teardown\s+)?proofs\b"}


@pytest.mark.parametrize("key", ["cleanup_failed", *(proof.error_type for proof in gdbdebug._TEARDOWN_PROOFS)])
def test_a_teardown_entry_counts_the_proofs_a_stop_takes(key: str) -> None:
    entry = catalogue_entry(key)
    assert entry is not None, key
    text = " ".join([entry["meaning"], *entry["remediation"], *entry.get("do_not", [])])

    for count, pattern in PROOF_COUNTS.items():
        if count != len(gdbdebug._TEARDOWN_PROOFS):
            assert not re.search(pattern, text, re.IGNORECASE), (pattern, text)


# ---------------------------------------------------------------------------
# The fill where a debugger-backed tool's result leaves the service.


def planted_through_the_service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backend: str, answer: dict) -> dict:
    """`debugger_info`'s answer when the backend returns `answer`, through the whole service call."""
    service = AgenticHILToolService(config_for(tmp_path, backend, FAKE_BY_TYPE[backend]))
    try:
        monkeypatch.setattr(service.backend, "info", lambda: json.loads(json.dumps(answer)))
        return service.call("debugger_info")
    finally:
        closed(service)


PLANTED_ENTRY = ErrorRemedy(meaning="A planted refusal.", remediation=("The planted step.",), do_not=("The planted wrong fix.",))
PLANTED_PERMISSION_ENTRY = ErrorRemedy(meaning="A planted permission refusal.", remediation=("Ask the operator to open {permission}.",), do_not=("Do not open {permission} yourself.",))


def refusal(backend: str | None, error_type: str = PLANTED, **fields: object) -> dict:
    answer: dict = {"ok": False, "tool": "debugger_info", "error_type": error_type, "summary": "Planted.", **fields}
    if backend is not None:
        answer["backend"] = backend
    return answer


@pytest.mark.parametrize("backend", BACKENDS)
def test_a_refusal_without_advice_is_filled_under_its_backend(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backend: str) -> None:
    monkeypatch.setitem(ERROR_CATALOGUE, f"{PLANTED}:{backend}", PLANTED_ENTRY)

    result = planted_through_the_service(tmp_path, monkeypatch, backend, refusal(backend))

    assert result.get("remediation") == list(PLANTED_ENTRY.remediation), result
    assert result.get("do_not") == list(PLANTED_ENTRY.do_not), result


def test_the_fill_substitutes_the_permission_the_refusal_names(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(ERROR_CATALOGUE, f"{PLANTED}:openocd", PLANTED_PERMISSION_ENTRY)

    result = planted_through_the_service(tmp_path, monkeypatch, "openocd", refusal("openocd", permission="debuggers.planted.permissions.allow_flash"))

    assert result.get("remediation") == ["Ask the operator to open debuggers.planted.permissions.allow_flash."], result


@pytest.mark.parametrize(("bucket", "backend"), sorted(SILENT), ids=[f"{bucket}-{backend}" for bucket, backend in sorted(SILENT)])
def test_a_pair_516_keeps_silent_stays_silent_through_the_fill(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bucket: str, backend: str) -> None:
    """The fill looks up the scoped key and falls back to the bare one, and neither exists for these."""
    result = planted_through_the_service(tmp_path, monkeypatch, backend, refusal(backend, bucket))

    assert result["error_type"] == bucket, result
    assert "remediation" not in result, result
    assert "do_not" not in result, result


def test_a_refusal_with_advice_of_its_own_keeps_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(ERROR_CATALOGUE, f"{PLANTED}:openocd", PLANTED_ENTRY)

    result = planted_through_the_service(tmp_path, monkeypatch, "openocd", refusal("openocd", remediation=["The producer's own step."]))

    assert result.get("remediation") == ["The producer's own step."], result
    assert "do_not" not in result, result


def test_a_refusal_naming_no_backend_is_not_filled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(ERROR_CATALOGUE, PLANTED, PLANTED_ENTRY)

    result = planted_through_the_service(tmp_path, monkeypatch, "openocd", refusal(None))

    assert "remediation" not in result, result


def test_a_nested_refusal_is_not_filled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(ERROR_CATALOGUE, f"{PLANTED}:openocd", PLANTED_ENTRY)
    answer = {"ok": False, "tool": "debugger_info", "backend": "openocd", "summary": "Planted.", "result": {"ok": False, "backend": "openocd", "error_type": PLANTED}}

    result = planted_through_the_service(tmp_path, monkeypatch, "openocd", answer)

    assert "remediation" not in result, result
    assert "remediation" not in result["result"], result


def test_a_success_that_names_a_target_fault_is_not_filled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(ERROR_CATALOGUE, f"{PLANTED}:openocd", PLANTED_ENTRY)
    answer = {"ok": True, "tool": "debugger_info", "backend": "openocd", "target_ok": False, "target_error_type": PLANTED, "summary": "Planted."}

    result = planted_through_the_service(tmp_path, monkeypatch, "openocd", answer)

    assert "remediation" not in result, result


# ---------------------------------------------------------------------------
# A session over a faulted target answers ok, and names the fault.


@pytest.mark.parametrize("tool", ["debug_start_session", "debug_get_session_status", "debug_get_stop_reason"])
def test_a_session_over_a_faulted_target_names_the_fault_and_its_entry(tmp_path: Path, tool: str) -> None:
    """gdbdebug.py 411, 494 and 713: `ok: true`, `target_ok: false` and the fault's
    type, with the session's own suggested_actions. The bare entry is what a
    reader of `target_error_type` is pointed to, and the test reactor publishes
    the same type as its step's error_type; the two have to agree that the
    target is not to be resumed."""
    service = session_service(tmp_path, fake_gdb_behavior="stopped_on_attach_hardfault")
    try:
        started = service.call(*START)
        result = started if tool == "debug_start_session" else service.call(tool, {})
    finally:
        closed(service)

    assert result["ok"] is True, result
    assert result["target_ok"] is False, result
    assert result["target_error_type"] == "target_exception", result
    assert [step for step in result["suggested_actions"] if step.strip()], result
    assert any(re.search(r"do not continue", step, re.IGNORECASE) for step in result["suggested_actions"]), result
    advice = remediation_fields(result["target_error_type"])
    assert advice, "no bare entry answers target_exception"
    assert any("debug_continue" in step for step in advice.get("do_not", [])), advice


# ---------------------------------------------------------------------------
# The entry arrives on the refusal itself, one tool path at a time.


def closed(service: AgenticHILToolService) -> None:
    """Close the service; where the refusal under test left a session the close may not settle, end the bench hold instead."""
    try:
        service.close()
    except RuntimeError:
        service.coordinator.close()


def refused_by(service: AgenticHILToolService, *calls: tuple[str, dict]) -> dict:
    """The answer to the last of `calls`, each earlier one asserted to have succeeded."""
    try:
        for tool, arguments in calls[:-1]:
            answer = service.call(tool, arguments)
            assert answer["ok"] is True, answer
        tool, arguments = calls[-1]
        return service.call(tool, arguments)
    finally:
        closed(service)


START = ("debug_start_session", {"image_path": "build/app.elf", "mode": "attach", "timeout_s": START_TIMEOUT_S})


def raises(error: BaseException) -> Callable[..., object]:
    def refuse(*_args: object, **_kwargs: object) -> object:
        raise error

    return refuse


# -- debug_start_session


def started_twice(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    return refused_by(session_service(tmp_path), START, START)


def started_on_a_gdb_without_mi_async(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    return refused_by(seeded_service(tmp_path, monkeypatch, MI_ASYNC_UNSUPPORTED), START)


def started_with_a_gdb_that_will_not_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    monkeypatch.setattr(gdbdebug, "GdbMiClient", raises(OSError("injected: GDB does not start")))
    return refused_by(session_service(tmp_path), START)


def started_without_output_readers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    service = session_service(tmp_path)
    monkeypatch.setattr(service.backend._debug, "_start_output_readers", raises(RuntimeError("injected: no reader thread")))
    return refused_by(service, START)


def started_with_a_server_that_will_not_spawn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    monkeypatch.setattr(gdbdebug, "spawn_managed_process", raises(FileNotFoundError("injected: no such file")))
    return refused_by(session_service(tmp_path), START)


def started_with_a_server_that_never_gets_ready(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    monkeypatch.setattr(gdbdebug, "wait_for_ready_line", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(gdbdebug, "wait_for_tcp_port", lambda *_args, **_kwargs: False)
    return refused_by(session_service(tmp_path), ("debug_start_session", {**START[1], "timeout_s": 0.5}))


def started_on_a_server_that_printed(stderr: str) -> Callable[[Path, pytest.MonkeyPatch], dict]:
    def provoke(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
        play_transcript(monkeypatch, stderr=stderr, returncode=1)
        return refused_by(server_service(tmp_path, server=FAKE_TRANSCRIPT), START)

    return provoke


def started_on_a_server_with_no_probe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    play_recording(monkeypatch, "openocd_server_stlink_no_probe")
    return refused_by(server_service(tmp_path, server=FAKE_TRANSCRIPT), START)


def started_on_a_probe_this_user_may_not_open(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    # The libusb refusal is read as one only off Windows (openocd.py), so the
    # backend is shown a POSIX host, as test_openocd_access_denied.py does.
    monkeypatch.setattr(openocd, "os", HostOs("posix"), raising=False)
    return refused_by(server_service(tmp_path, server=FAKE_OPENOCD_ACCESS_DENIED), START)


def started_while_raw_commands_are_granted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    return refused_by(session_service(tmp_path, permissions={**DEFAULT_TEST_PERMISSIONS, "allow_raw_debugger_commands": True}), START)


# -- debug_stop_session


def stopped_with_a_gdb_that_will_not_close(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    service = session_service(tmp_path)
    try:
        assert service.call(*START)["ok"] is True
        session = service.backend._debug.session
        original = session.gdb.close
        monkeypatch.setattr(session.gdb, "close", raises(RuntimeError("injected: close failed")))
        try:
            return service.call("debug_stop_session", {})
        finally:
            monkeypatch.setattr(session.gdb, "close", original)
    finally:
        closed(service)


def stopped_without(proof: str) -> Callable[[Path, pytest.MonkeyPatch], dict]:
    def provoke(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
        service = session_service(tmp_path)
        try:
            assert service.call(*START)["ok"] is True
            monkeypatch.setattr(service.backend._debug, proof, lambda *_args, **_kwargs: False)
            return service.call("debug_stop_session", {})
        finally:
            closed(service)

    return provoke


# -- the session's run control and state


def continued_into(behavior: str) -> Callable[[Path, pytest.MonkeyPatch], dict]:
    def provoke(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
        return refused_by(session_service(tmp_path, fake_gdb_behavior=behavior), START, ("debug_set_breakpoint", {"location": {"symbol": "test_done"}}), ("debug_continue", {"timeout_s": 5}))

    return provoke


def continued_without_the_grant(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    return refused_by(session_service(tmp_path, permissions={**DEFAULT_TEST_PERMISSIONS, "allow_debug_execution": False}), START, ("debug_continue", {"timeout_s": 5}))


def halted_after_a_fault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    service = session_service(tmp_path, fake_gdb_behavior="hardfault")
    try:
        assert service.call(*START)["ok"] is True
        assert service.call("debug_set_breakpoint", {"location": {"symbol": "test_done"}})["ok"] is True
        assert service.call("debug_continue", {"timeout_s": 5})["error_type"] == "target_exception"
        return service.call("debug_halt", {"timeout_s": 1.0})
    finally:
        closed(service)


def asked_for_a_stop_reason_before_any_stop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    return refused_by(session_service(tmp_path), START, ("debug_get_stop_reason", {}))


def called_after_the_audit_broke(tool: str, arguments: dict) -> Callable[[Path, pytest.MonkeyPatch], dict]:
    def provoke(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
        service = session_service(tmp_path)
        try:
            assert service.call(*START)["ok"] is True
            latch_audit_break(service)
            return service.call(tool, arguments)
        finally:
            closed(service)

    return provoke


def cleared_with_an_unreadable_breakpoint_list(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    service = session_service(tmp_path)
    try:
        assert service.call(*START)["ok"] is True
        monkeypatch.setattr(service.backend._debug, "_backend_breakpoint_numbers", lambda *_args, **_kwargs: None)
        return service.call("debug_clear_breakpoints", {})
    finally:
        closed(service)


# -- the sessions on the servers ended before GDB detaches (#624)

GDB_SERVER_SESSION_SERVICE: dict[str, Callable[[Path, pytest.MonkeyPatch], tuple[AgenticHILToolService, Path]]] = {"pyocd": pyocd_session_service, "stlink": st_link_session_service}


def stopped_on_a_gdb_server_with_an_unreadable_breakpoint_list(backend: str) -> Callable[[Path, pytest.MonkeyPatch], dict]:
    """A stop over a breakpoint whose removal before the server's end cannot read the backend's list."""

    def provoke(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
        service, _events = GDB_SERVER_SESSION_SERVICE[backend](tmp_path, monkeypatch)
        try:
            assert service.call(*START)["ok"] is True
            assert service.call("debug_set_breakpoint", {"location": {"symbol": "test_done"}})["ok"] is True
            monkeypatch.setattr(service.backend._debug, "_backend_breakpoint_numbers", lambda *_args, **_kwargs: None)
            return service.call("debug_stop_session", {})
        finally:
            closed(service)

    return provoke


def called_on_a_gdb_server_after_the_audit_broke(backend: str, tool: str, arguments: dict) -> Callable[[Path, pytest.MonkeyPatch], dict]:
    def provoke(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
        service, _events = GDB_SERVER_SESSION_SERVICE[backend](tmp_path, monkeypatch)
        try:
            assert service.call(*START)["ok"] is True
            latch_audit_break(service)
            return service.call(tool, arguments)
        finally:
            closed(service)

    return provoke


# -- the symbol tools over a session


def symbol_over_a_session(tool: str, symbol: str, gdb_answer: GdbMiCommandResult | None = None) -> Callable[[Path, pytest.MonkeyPatch], dict]:
    """`tool` for `symbol`, with GDB's answer to every expression replaced by `gdb_answer` when one is given."""

    def provoke(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
        service = session_service(tmp_path)
        try:
            assert service.call(*START)["ok"] is True
            if gdb_answer is not None:
                debug = service.backend._debug
                original = debug._gdb_command

                def answer(session: object, command: str, timeout_s: float | None = None, **kwargs: object) -> object:
                    if command.startswith("-data-evaluate-expression"):
                        return gdb_answer
                    return original(session, command, timeout_s, **kwargs)

                monkeypatch.setattr(debug, "_gdb_command", answer)
            return service.call(tool, symbol_arguments(tool, symbol))
        finally:
            closed(service)

    return provoke


def dumped_where_nothing_can_be_written(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    monkeypatch.setattr(gdbdebug, "write_intel_hex_file", raises(OSError("injected: disk full")))
    return refused_by(session_service(tmp_path), START, ("debug_dump_symbol_ihex", symbol_arguments("debug_dump_symbol_ihex", "CTC_array")))


# -- the symbol tools of the backends without a session


SESSIONLESS_SERVICE: dict[str, Callable[[Path], AgenticHILToolService]] = {"stlink": stlink_dump_service, "pyocd": pyocd_read_service}


def offline_symbol(backend: str, tool: str, symbol: str, *, flashed: bool = True, after_flash: Callable[[Path, pytest.MonkeyPatch], None] | None = None) -> Callable[[Path, pytest.MonkeyPatch], dict]:
    def provoke(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
        service = SESSIONLESS_SERVICE[backend](tmp_path)
        try:
            if flashed:
                assert flash_symbol_source(service)["ok"] is True
            if after_flash is not None:
                after_flash(tmp_path, monkeypatch)
            return service.call(tool, symbol_arguments(tool, symbol))
        finally:
            closed(service)

    return provoke


def rebuild_the_image(tmp_path: Path, _monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "build" / "app.elf").write_bytes(b"\x7fELF" + b"\x01" * 64)


def gdb_that_times_out(_tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gdbdebug, "spawn_command", lambda *_args, **_kwargs: common.CompletedCommand("", "", None, True, False))


def unwritable(module: ModuleType, name: str) -> Callable[[Path, pytest.MonkeyPatch], None]:
    def patch(_tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(module, name, raises(OSError("injected: disk full")))

    return patch


# -- the backend tools


def backend_call(backend: str, executable: Path, tool: str, arguments: dict | None = None, *, stderr: str | None = None, stdout: str = "", returncode: int = 1) -> Callable[[Path, pytest.MonkeyPatch], dict]:
    def provoke(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
        if stderr is not None:
            play_transcript(monkeypatch, stdout=stdout, stderr=stderr, returncode=returncode)
        return call(config_for(tmp_path, backend, executable), tool, arguments or {})

    return provoke


def missing_executable(backend: str) -> Callable[[Path, pytest.MonkeyPatch], dict]:
    def provoke(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
        return call(config_for(tmp_path, backend, tmp_path / "not-installed" / "debugger.exe"), "debugger_info")

    return provoke


def listed_without_a_usb_inventory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    monkeypatch.setattr(openocd, "list_available_com_ports", lambda *_args, **_kwargs: {"ok": False, "summary": "injected: the serial backend did not answer"})
    return call(config_for(tmp_path, "openocd", FAKE_OPENOCD), "debugger_probes_list")


def listed_on_an_adapter_without_usb_identity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    return call(config_for(tmp_path, "openocd", FAKE_OPENOCD, interface_cfg="interface/cmsis-dap.cfg"), "debugger_probes_list")


GAVE_UP = "Error: the tool gave up and said nothing about why\n"
RESET_REFUSED = "Error: failed to reset the target\n"


@dataclass(frozen=True)
class Refusal:
    tool: str
    error_type: str
    scope: str
    provoke: Callable[[Path, pytest.MonkeyPatch], dict]


REFUSALS = [
    # debug_start_session
    Refusal("debug_start_session", "session_already_active", "openocd", started_twice),
    Refusal("debug_start_session", "gdb_async_unsupported", "openocd", started_on_a_gdb_without_mi_async),
    Refusal("debug_start_session", "gdb_start_failed", "openocd", started_with_a_gdb_that_will_not_run),
    Refusal("debug_start_session", "debug_session_setup_failed", "openocd", started_without_output_readers),
    Refusal("debug_start_session", "debugger_not_found", "openocd", started_with_a_server_that_will_not_spawn),
    Refusal("debug_start_session", "timeout", "openocd", started_with_a_server_that_never_gets_ready),
    Refusal("debug_start_session", "target_config_not_found", "openocd", started_on_a_server_that_printed("Error: Can't find target/stm32f4x.cfg\n")),
    Refusal("debug_start_session", "interface_config_not_found", "openocd", started_on_a_server_that_printed("Error: Can't find interface/stlink.cfg\n")),
    Refusal("debug_start_session", "config_file_not_found", "openocd", started_on_a_server_that_printed("Error: Can't find board/other.cfg\n")),
    Refusal("debug_start_session", "debugger_error", "openocd", started_on_a_server_that_printed(GAVE_UP)),
    Refusal("debug_start_session", "adapter_access_denied", "openocd", started_on_a_probe_this_user_may_not_open),
    # Catalogued already; the start's result is built without the merge.
    Refusal("debug_start_session", "adapter_not_found", "openocd", started_on_a_server_with_no_probe),
    # A granted key that blocks the start: the exclusive scope, carried on the result.
    Refusal("debug_start_session", "permission_denied", EXCLUSIVE_PERMISSION_SCOPE, started_while_raw_commands_are_granted),
    # debug_stop_session
    Refusal("debug_stop_session", "cleanup_failed", "openocd", stopped_with_a_gdb_that_will_not_close),
    Refusal("debug_stop_session", "halt_not_confirmed", "openocd", stopped_without("_confirm_halted_before_end")),
    Refusal("debug_stop_session", "detach_resume_not_confirmed", "openocd", stopped_without("_pin_no_resume_on_detach")),
    # run control and state
    Refusal("debug_continue", "unexpected_breakpoint", "openocd", continued_into("unexpected_breakpoint")),
    Refusal("debug_continue", "target_exception", "openocd", continued_into("hardfault")),
    Refusal("debug_continue", "permission_denied", "allow_debug_execution", continued_without_the_grant),
    Refusal("debug_halt", "target_exception", "openocd", halted_after_a_fault),
    Refusal("debug_get_stop_reason", "stop_reason_not_available", "openocd", asked_for_a_stop_reason_before_any_stop),
    Refusal("debug_get_session_status", "audit_broken", "openocd", called_after_the_audit_broke("debug_get_session_status", {})),
    Refusal("debug_list_breakpoints", "audit_broken", "openocd", called_after_the_audit_broke("debug_list_breakpoints", {})),
    Refusal("debug_set_breakpoint", "audit_broken", "openocd", called_after_the_audit_broke("debug_set_breakpoint", {"location": {"symbol": "test_done"}})),
    Refusal("debug_clear_breakpoints", "breakpoint_reconciliation_failed", "openocd", cleared_with_an_unreadable_breakpoint_list),
    # the sessions on the servers ended before GDB detaches
    *(
        refusal
        for backend in ("pyocd", "stlink")
        for refusal in (
            Refusal("debug_stop_session", "breakpoints_not_removed", backend, stopped_on_a_gdb_server_with_an_unreadable_breakpoint_list(backend)),
            Refusal("debug_get_session_status", "audit_broken", backend, called_on_a_gdb_server_after_the_audit_broke(backend, "debug_get_session_status", {})),
        )
    ),
    # the symbol tools over a session
    Refusal("debug_symbol_info", "symbol_not_found", "openocd", symbol_over_a_session("debug_symbol_info", ABSENT_SYMBOL)),
    Refusal("debug_symbol_info", "symbol_resolution_failed", "openocd", symbol_over_a_session("debug_symbol_info", "g_pfnVectors")),
    Refusal("debug_symbol_info", "symbol_ambiguous", "openocd", symbol_over_a_session("debug_symbol_info", "boot_counter", GdbMiCommandResult(result_class="error", line="", error_message='"boot_counter" is ambiguous.'))),
    Refusal("debug_symbol_value", "timeout", "openocd", symbol_over_a_session("debug_symbol_value", "boot_counter", GdbMiCommandResult(result_class="error", line="", timed_out=True))),
    Refusal("debug_dump_symbol_ihex", "output_write_failed", "openocd", dumped_where_nothing_can_be_written),
    # the symbol tools without a session
    *(
        refusal
        for backend in ("pyocd", "stlink")
        for refusal in (
            Refusal("debug_symbol_info", "symbol_source_not_available", backend, offline_symbol(backend, "debug_symbol_info", "CTC_array", flashed=False)),
            Refusal("debug_symbol_info", "symbol_source_changed", backend, offline_symbol(backend, "debug_symbol_info", "CTC_array", after_flash=rebuild_the_image)),
            Refusal("debug_symbol_info", "symbol_not_found", backend, offline_symbol(backend, "debug_symbol_info", ABSENT_SYMBOL)),
            Refusal("debug_symbol_info", "symbol_resolution_failed", backend, offline_symbol(backend, "debug_symbol_info", "g_pfnVectors")),
            Refusal("debug_symbol_value", "timeout", backend, offline_symbol(backend, "debug_symbol_value", "CTC_array", after_flash=gdb_that_times_out)),
        )
    ),
    Refusal("debug_dump_symbol_ihex", "output_write_failed", "stlink", offline_symbol("stlink", "debug_dump_symbol_ihex", "CTC_array", after_flash=unwritable(stlink, "safe_configured_directory"))),
    Refusal("debug_dump_symbol_ihex", "output_write_failed", "pyocd", offline_symbol("pyocd", "debug_dump_symbol_ihex", "CTC_array", after_flash=unwritable(pyocd, "write_intel_hex_file"))),
    # the backend tools
    *(Refusal("debugger_info", "debugger_not_found", backend, missing_executable(backend)) for backend in BACKENDS),
    *(Refusal("probe_target", "debugger_error", backend, backend_call(backend, FAKE_TRANSCRIPT, "probe_target", stderr=GAVE_UP)) for backend in BACKENDS),
    *(Refusal("reset_target", "reset_failed", backend, backend_call(backend, FAKE_TRANSCRIPT, "reset_target", {"mode": "halt"}, stderr=RESET_REFUSED)) for backend in BACKENDS),
    # pyOCD's post-flash reset relabels a refusal whose advice was merged for
    # the type its classifier answered (pyocd.py); here both are reset_failed.
    Refusal("flash_firmware", "reset_failed", "pyocd", backend_call("pyocd", FAKE_PYOCD_RESET_REFUSED, "flash_firmware", {"image_path": "build/firmware.elf", "reset_after_flash": True})),
    # Catalogued already, under the backend's own key; the refusal is built without the merge.
    *(Refusal("reset_target", "not_supported", backend, backend_call(backend, FAKE_BY_TYPE[backend], "reset_target", {"mode": "init"})) for backend in ("pyocd", "stlink")),
    Refusal("debugger_probes_list", "probe_discovery_failed", "openocd", listed_without_a_usb_inventory),
    Refusal("debugger_probes_list", "probe_discovery_failed", "pyocd", backend_call("pyocd", FAKE_TRANSCRIPT, "debugger_probes_list", stderr="boom\n")),
    Refusal("debugger_probes_list", "probe_discovery_failed", "stlink", backend_call("stlink", FAKE_TRANSCRIPT, "debugger_probes_list", stdout="nothing this listing knows\n", stderr="")),
    Refusal("debugger_probes_list", "not_supported", "openocd", listed_on_an_adapter_without_usb_identity),
]


@pytest.mark.parametrize("refusal", REFUSALS, ids=[f"{refusal.tool}-{refusal.error_type}-{refusal.scope}" for refusal in REFUSALS])
def test_the_refusal_carries_its_entry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, refusal: Refusal) -> None:
    result = refusal.provoke(tmp_path, monkeypatch)

    assert result["ok"] is False, result
    assert result["error_type"] == refusal.error_type, result
    permission = result.get("permission")
    advice = remediation_fields(refusal.error_type, refusal.scope, permission=permission if isinstance(permission, str) else None)
    assert advice, f"no catalogue entry answers {refusal.error_type} under {refusal.scope}"
    assert result.get("remediation") == advice["remediation"], result
    assert result.get("do_not") == advice.get("do_not"), result


DEBUG_TOOLS = frozenset(name for name in TOOLS_DEBUG_FUNCTIONS if not name.startswith("_") and not name.endswith("_error"))


def test_every_pair_written_here_and_every_debug_tool_is_driven() -> None:
    driven = {(refusal.error_type, refusal.scope) for refusal in REFUSALS}

    assert sorted(WRITTEN_HERE - driven, key=str) == []
    assert sorted(DEBUG_TOOLS - {refusal.tool for refusal in REFUSALS}) == []
