"""Every refusal a debug tool or a debugger backend answers with has a catalogue entry (#644).

AGENTS.md describes `agentic-hil://reference/errors` as listing every
`error_type` with its meaning, its ordered fix and the wrong fix. Much of what
the debug session tools and the three debugger backends refuse with had no
entry there: the reference resolved to nothing, and the refusal carried no
standing fix.

The fix a refusal needs can turn on the backend that answered it, and the
catalogue already says so with keys of the form `<error_type>:<backend>`. So
the inventory here is a set of pairs, the error type and the backend whose
remediation a refusal of that type is looked up under, and an entry is the
scoped key where there is one and the bare key otherwise.

Three things are held here. The inventory is read off the source, so a refusal
added later without an entry fails the guard, and it is pinned as well, so the
scan cannot shrink unnoticed. Every pair in it resolves at its URI. And every
tool path, driven through the tool service against the suite's fake debugger,
fake GDB and recorded transcripts, hands the entry's steps out in the refusal
itself. No probe, board or debugger process is touched.
"""

from __future__ import annotations

import ast
import json
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from types import ModuleType

import pytest
from conftest import FAKE_OPENOCD, FAKE_OPENOCD_ACCESS_DENIED, write_config
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
from test_openocd_access_denied import HostOs

from agentic_hil import debugger, elfsymbols, gdbmi, tools
from agentic_hil.backends import common, gdbdebug, openocd, pyocd, stlink
from agentic_hil.config import load_config
from agentic_hil.gdbmi import GdbMiCommandResult
from agentic_hil.knowledge import ERROR_CATALOGUE, ERROR_URI_PREFIX, catalogue_entry, lookup_remedy, remediation_fields
from agentic_hil.mcp import handle_mcp_message
from agentic_hil.tools import AgenticHILToolService

BACKENDS = ("openocd", "pyocd", "stlink")

# ---------------------------------------------------------------------------
# Where the debug refusals are built, and whose remediation each one is.

# Each backend module answers under its own name; that is the scope
# `_failure_result` and every other merge in it passes.
BACKEND_MODULES: dict[str, ModuleType] = {"openocd": openocd, "pyocd": pyocd, "stlink": stlink}

# Overrides inside a backend module: the site merges under a scope of its own.
SITE_SCOPES: dict[tuple[str, str], tuple[str, ...]] = {
    ("openocd.py", "_probe_selection_refusal"): ("openocd_probe_selection",),
}

# `GdbDebugSessions` runs the typed debug sessions, and only the OpenOCD backend
# constructs it (pinned below), so every refusal its methods build is an
# OpenOCD refusal. The module's own functions are shared, and named one by one.
GDBDEBUG_SESSION_CLASS = "GdbDebugSessions"
GDBDEBUG_FUNCTION_SCOPES: dict[str, tuple[str, ...]] = {
    # A missing GDB, met by the OpenOCD session start and by the offline symbol
    # read of the two sessionless backends alike. Each of the three merges
    # under a scope of its own, the GDB's state, not the backend's.
    "configured_gdb_missing": (None,),
    "no_gdb_on_this_bench": (gdbdebug.GDB_NOT_CONFIGURED_SCOPE,),
    "autodetected_gdb_missing": (gdbdebug.GDB_AUTODETECTED_MISSING_SCOPE,),
    # The offline symbol read of the backends without a typed session.
    "validate_debug_symbol": ("pyocd", "stlink"),
    "resolve_symbol_offline": ("pyocd", "stlink"),
    # Only `GdbDebugSessions.set_breakpoint` normalizes a location.
    "normalize_breakpoint_location": ("openocd",),
    "normalize_symbol_location": ("openocd",),
    # Only the typed sessions report where their target stopped.
    "target_stop_fields": ("openocd",),
}
COMMON_FUNCTION_SCOPES: dict[str, tuple[str, ...]] = {
    "debug_session_unsupported": ("pyocd", "stlink"),
    "reset_init_unsupported": ("pyocd", "stlink"),
    "not_executable_refusal": BACKENDS,
}

# The debug paths of the tool service. Each answers before any backend is
# asked, so nothing scopes it: the bare key is the one a refusal of theirs is
# served by. `flash_firmware`'s own argument checks are the flash path's.
# Two of them refuse with `not_supported` for a reason that is neither
# backend's, a probe the call cannot be routed to, and are named a scope of
# their own, so the catalogue can say what fixes each.
TOOLS_SITE_SCOPES: dict[str, tuple[str, ...]] = {
    "unbound_debugger_error": ("unbound_debugger",),
    "unnamed_probe_error": ("unnamed_probe",),
}
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
        "unbound_debugger_error",
        "unnamed_probe_error",
    }
)

# Expressions the scan cannot evaluate from the source alone, each pinned by
# its exact text where it stands, with the values it can take and why. A
# change to the expression is a change to this table.
PINNED_EXPRESSIONS: dict[tuple[str, str, str], Callable[[], frozenset[str]]] = {
    # Reached only under `if not ok`, and `ok` is `stop_reason not in
    # ABNORMAL_STOP_REASONS`, so the argument is one of those reasons.
    ("gdbdebug.py", "_stopped_result", "stop_error_type(stop_reason)"): lambda: frozenset(gdbdebug.stop_error_type(reason) for reason in gdbdebug.ABNORMAL_STOP_REASONS),
    # The same reasons, set as `target_error_type` under `if stop_reason in
    # ABNORMAL_STOP_REASONS`.
    ("gdbdebug.py", "target_stop_fields", "stop_error_type(stop_reason)"): lambda: frozenset(gdbdebug.stop_error_type(reason) for reason in gdbdebug.ABNORMAL_STOP_REASONS),
    # A debug server that stopped before its GDB port was ready: the session
    # hands its output to the classifier it was constructed with, which is
    # OpenOCD's (pinned below), without a tool, and publishes what that answers
    # as it is, with `debugger_error` for a classification that matched nothing.
    ("gdbdebug.py", "_start_failure", "backend_error_type if backend_error_type != 'unknown_debugger_error' else 'debugger_error'"): lambda: (
        classifier_returns_without_a_tool(openocd) - {"unknown_debugger_error"}
    )
    | {"debugger_error"},
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
    def of(cls, module: ModuleType) -> Source:
        path = Path(str(module.__file__))
        source = cls(module, path.name, ast.parse(path.read_text(encoding="utf-8")))
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
    bindings: tuple[tuple[str, frozenset[str]], ...] = ()

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
    """Evaluates an `error_type` expression to every string it can take."""

    def __init__(self) -> None:
        self._active: set[tuple[str, int, str]] = set()

    def values(self, node: ast.expr, scope: Scope) -> frozenset[str]:
        if scope.function is not None:
            pinned = PINNED_EXPRESSIONS.get((scope.source.name, scope.function.name, ast.unparse(node)))
            if pinned is not None:
                return pinned()
        if isinstance(node, ast.Constant):
            if node.value is None:
                return frozenset()
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

    def _name(self, name: str, scope: Scope) -> frozenset[str]:
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

    def _parameter(self, name: str, scope: Scope) -> frozenset[str]:
        """A parameter no call-site binding fixes: what every caller in the module passes."""
        assert scope.function is not None
        key = (scope.source.name, scope.function.lineno, name)
        if key in self._active:
            return frozenset()
        self._active.add(key)
        try:
            values: frozenset[str] = frozenset()
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

    def _call(self, node: ast.Call, scope: Scope) -> frozenset[str]:
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

    def _returns(self, source: Source, function: ast.FunctionDef, bindings: tuple[tuple[str, frozenset[str]], ...]) -> frozenset[str]:
        key = (source.name, function.lineno, "<return>")
        if key in self._active:
            return frozenset()
        self._active.add(key)
        try:
            callee = Scope(source, function, bindings)
            values: frozenset[str] = frozenset()
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


def error_type_expressions(source: Source) -> Iterator[ast.expr]:
    """Every expression `source` writes as an error type: a dict value, a keyword,
    a subscript assignment, and the error type `tool_error` is called with."""
    for node in ast.walk(source.tree):
        if isinstance(node, ast.Dict):
            yield from (value for key, value in zip(node.keys, node.values, strict=True) if isinstance(key, ast.Constant) and key.value in ERROR_TYPE_FIELDS)
        elif isinstance(node, ast.keyword) and node.arg in ERROR_TYPE_FIELDS:
            yield node.value
        elif isinstance(node, ast.Assign):
            yield from (node.value for target in node.targets if isinstance(target, ast.Subscript) and isinstance(target.slice, ast.Constant) and target.slice.value in ERROR_TYPE_FIELDS)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "tool_error" and len(node.args) > 1:
            yield node.args[1]


def scopes_of_site(source: Source, node: ast.expr) -> tuple[str | None, ...] | None:
    """Whose remediation a refusal built at `node` is looked up under, or None when the site is not a debug one."""
    function = source.enclosing_function(node)
    function_name = function.name if function is not None else "<module>"
    if source.module is tools:
        if function_name not in TOOLS_DEBUG_FUNCTIONS:
            return None
        return TOOLS_SITE_SCOPES.get(function_name, (None,))
    for backend, module in BACKEND_MODULES.items():
        if source.module is module:
            return SITE_SCOPES.get((source.name, function_name), (backend,))
    if source.module is gdbdebug:
        if source.enclosing_class(node) == GDBDEBUG_SESSION_CLASS:
            return ("openocd",)
        if function_name in GDBDEBUG_FUNCTION_SCOPES:
            return GDBDEBUG_FUNCTION_SCOPES[function_name]
    if source.module is common and function_name in COMMON_FUNCTION_SCOPES:
        return COMMON_FUNCTION_SCOPES[function_name]
    raise AssertionError(f"{source.name}:{node.lineno}: an error_type in {function_name}, which no scope is pinned for")


# The debugger front end, the GDB/MI client and the ELF reader build no refusal
# today; scanning them makes one that appears later fail `scopes_of_site`.
SCANNED_MODULES: tuple[ModuleType, ...] = (gdbdebug, openocd, pyocd, stlink, common, tools, debugger, gdbmi, elfsymbols)


@cache
def scanned_debug_pairs() -> dict[tuple[str, str | None], tuple[str, ...]]:
    """Every (error_type, scope) pair the debug paths can answer, with the `file:line` sites."""
    reader = Reader()
    sites: dict[tuple[str, str | None], list[str]] = {}
    unreadable: list[str] = []
    for module in SCANNED_MODULES:
        source = source_of(module)
        for node in error_type_expressions(source):
            scopes = scopes_of_site(source, node)
            if scopes is None:
                continue
            try:
                values = reader.values(node, Scope(source, source.enclosing_function(node)))
            except UnreadableValue as error:
                unreadable.append(f"{source.name}:{node.lineno}: {ast.unparse(node)} ({error})")
                continue
            for value in values:
                for scope in scopes:
                    sites.setdefault((value, scope), []).append(f"{source.name}:{node.lineno}")
    assert not unreadable, f"error_type values the scan cannot read: {unreadable}"
    return {pair: tuple(sorted(set(lines))) for pair, lines in sites.items()}


# ---------------------------------------------------------------------------
# The inventory, pinned, and what is left out of it with the reason.

# Every pair the scan reads, so a scan that silently stops reading a site, or a
# refusal added to the source, is a change to this table.
INVENTORY_BY_TYPE: dict[str, tuple[str | None, ...]] = {
    "adapter_access_denied": ("openocd",),
    "adapter_not_found": BACKENDS,
    "artifact_validation_failed": (None,),
    "audit_broken": ("openocd",),
    "audit_unavailable": ("pyocd",),
    "breakpoint_reconciliation_failed": ("openocd",),
    "cleanup_failed": ("openocd",),
    "cleanup_required": (None,),
    "config_file_not_found": ("openocd", "stlink"),
    "debug_session_setup_failed": ("openocd",),
    "debugger_command_rejected": ("openocd",),
    "debugger_config_not_found": ("openocd",),
    "debugger_error": BACKENDS,
    "debugger_not_executable": BACKENDS,
    "debugger_not_found": BACKENDS,
    "detach_resume_not_confirmed": ("openocd",),
    "flash_erase_failed": BACKENDS,
    "flash_failed": BACKENDS,
    "gdb_async_unsupported": ("openocd",),
    "gdb_not_found": (None, gdbdebug.GDB_AUTODETECTED_MISSING_SCOPE, gdbdebug.GDB_NOT_CONFIGURED_SCOPE),
    "gdb_start_failed": ("openocd",),
    "halt_not_confirmed": ("openocd",),
    "interface_config_not_found": ("openocd",),
    "invalid_argument": (None, *BACKENDS),
    "memory_read_failed": BACKENDS,
    "not_supported": ("openocd", "openocd_probe_selection", "pyocd", "stlink", "unbound_debugger", "unnamed_probe"),
    "output_write_failed": BACKENDS,
    "permission_denied": (None, *BACKENDS),
    "probe_discovery_failed": BACKENDS,
    "reset_failed": BACKENDS,
    "resource_busy": (None,),
    "resource_quarantined": ("openocd",),
    "session_already_active": ("openocd",),
    "session_not_active": ("openocd",),
    "stop_reason_not_available": ("openocd",),
    "symbol_ambiguous": ("openocd",),
    "symbol_not_found": BACKENDS,
    "symbol_resolution_failed": BACKENDS,
    "symbol_source_changed": ("pyocd", "stlink"),
    "symbol_source_not_available": ("pyocd", "stlink"),
    "target_config_not_found": ("openocd",),
    "target_exception": ("openocd",),
    "target_not_detected": BACKENDS,
    "target_state_unconfirmed": ("openocd", "stlink"),
    "target_type_invalid": ("pyocd",),
    "timeout": BACKENDS,
    "unexpected_breakpoint": ("openocd",),
    "verify_failed": BACKENDS,
}
INVENTORY = frozenset((error_type, scope) for error_type, scopes in INVENTORY_BY_TYPE.items() for scope in scopes)

Pair = tuple[str, str | None]

# Pairs whose entry another change writes. The guard leaves them to it.
OWNED_ELSEWHERE: dict[Pair, str] = {
    ("session_not_active", "openocd"): "#635 writes one bare entry, true for the COM, CAN and debug sessions alike",
    ("resource_busy", None): "#646 writes the coordination refusals; `_coordinated_debug_call` answers with the lease's",
    ("cleanup_required", None): "#645 writes the dispatcher's refusals, and this one is the dispatcher's",
    ("artifact_validation_failed", None): "#645 writes the artifact refusals; `debug_start_session` refuses with the validator's word",
    ("audit_unavailable", "pyocd"): "#645 writes one bare entry for every module that loses its audit trail",
    ("not_supported", "unbound_debugger"): "#645 writes the dispatcher's scoped `not_supported` keys",
    ("not_supported", "unnamed_probe"): "#645 writes the dispatcher's scoped `not_supported` keys",
}

# Pairs #516 decided stay silent until somebody writes that tool's own steps,
# pinned silent by test_debug_backend_refusals.py.
SILENT: frozenset[Pair] = frozenset((bucket, backend) for bucket, backend in SILENT_PAIRS)

# Values the scan reads that never reach a tool result. None does: every site
# above builds the top-level `error_type` of a result some tool returns.
NEVER_REACHED: dict[Pair, str] = {}

EXCLUDED: frozenset[Pair] = frozenset(OWNED_ELSEWHERE) | SILENT | frozenset(NEVER_REACHED)

# The pairs the catalogue answered nothing for when this was written, or
# answered with an entry about something else, and which this change writes.
WRITTEN_HERE: frozenset[Pair] = frozenset(
    {
        ("adapter_access_denied", "openocd"),
        ("audit_broken", "openocd"),
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
    }
)

# Pairs the bare key cannot answer, each with why: the scoped key has to exist.
OWN_KEY_REQUIRED: dict[tuple[str, str], str] = {
    ("config_file_not_found", "openocd"): "the bare entry is about the Agentic HIL configuration file, and this is an OpenOCD script the debug server could not find",
    ("not_supported", "openocd"): "no bare `not_supported` is true for every refusal of that name, and the other backends' keys are about debug sessions",
    **{("debugger_not_found", backend): "the executable that is missing, and where it comes from, is each backend's own" for backend in BACKENDS},
    **{("timeout", backend): "a GDB/MI session that stopped answering and a command-line tool that ran out of time are fixed differently" for backend in BACKENDS},
}

# Bare keys this area owns because other areas scope theirs under them.
BARE_KEY_REQUIRED: dict[str, str] = {
    "cleanup_failed": "the debug session's own; #646 adds only `cleanup_failed:test_reactor` beside it",
}


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
    for bare, _reason in BARE_KEY_REQUIRED.items():
        assert any(error_type == bare for error_type, _scope in WRITTEN_HERE), bare


def test_every_debug_refusal_has_a_catalogue_entry() -> None:
    assert missing_entries() == [], "debug refusals without an ERROR_CATALOGUE entry"


@pytest.mark.parametrize(("error_type", "scope"), sorted(OWN_KEY_REQUIRED), ids=[f"{error_type}:{scope}" for error_type, scope in sorted(OWN_KEY_REQUIRED)])
def test_a_refusal_the_bare_key_cannot_answer_has_its_own(error_type: str, scope: str) -> None:
    assert f"{error_type}:{scope}" in ERROR_CATALOGUE, OWN_KEY_REQUIRED[(error_type, scope)]


@pytest.mark.parametrize("error_type", sorted(BARE_KEY_REQUIRED))
def test_the_bare_entries_this_area_owns_exist(error_type: str) -> None:
    assert error_type in ERROR_CATALOGUE, BARE_KEY_REQUIRED[error_type]


# ---------------------------------------------------------------------------
# The facts the scan's scopes rest on.


def test_typed_debug_sessions_are_built_by_the_openocd_backend_alone() -> None:
    """Why every refusal `GdbDebugSessions` builds is looked up under `openocd`."""
    constructions = []
    for module in (*BACKEND_MODULES.values(), gdbdebug, common, tools, debugger):
        source = source_of(module)
        constructions.extend((source.name, node) for node in ast.walk(source.tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == GDBDEBUG_SESSION_CLASS)

    assert [name for name, _node in constructions] == ["openocd.py"]
    (_name, call) = constructions[0]
    keywords = {keyword.arg: ast.unparse(keyword.value) for keyword in call.keywords}
    assert keywords["backend_name"] == "self.backend_name", keywords
    assert keywords["classify_server_output"] == "self._classify_output", keywords


def backends_reaching(function_name: str, seen: frozenset[str] = frozenset()) -> frozenset[str]:
    """The backends whose calls reach the module function `function_name` of gdbdebug.py.

    A call in a backend module is that backend's; a call inside
    `GdbDebugSessions` is OpenOCD's; a call inside another module function of
    gdbdebug.py is whatever reaches that one. A call anywhere else fails.
    """
    reached: set[str] = set()
    for module in SCANNED_MODULES:
        source = source_of(module)
        for node in ast.walk(source.tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == function_name):
                continue
            caller = source.enclosing_function(node)
            if module in BACKEND_MODULES.values():
                reached.add(module.__name__.rpartition(".")[2])
            elif module is gdbdebug and source.enclosing_class(node) == GDBDEBUG_SESSION_CLASS:
                reached.add("openocd")
            elif module is gdbdebug and caller is not None and source.enclosing_class(node) is None:
                if caller.name not in seen:
                    reached |= backends_reaching(caller.name, seen | {function_name})
            else:
                raise AssertionError(f"{source.name}:{node.lineno}: {function_name} called where no backend is named")
    return frozenset(reached)


BACKEND_SCOPED_FUNCTIONS = sorted(name for name, scopes in GDBDEBUG_FUNCTION_SCOPES.items() if set(scopes) <= set(BACKENDS))


@pytest.mark.parametrize("function_name", BACKEND_SCOPED_FUNCTIONS)
def test_a_shared_gdb_function_is_reached_by_the_backends_named_for_it(function_name: str) -> None:
    """Why a refusal built in a module function of gdbdebug.py is looked up under the backends `GDBDEBUG_FUNCTION_SCOPES` names."""
    assert backends_reaching(function_name) == frozenset(GDBDEBUG_FUNCTION_SCOPES[function_name])


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


RESOLVED = sorted(INVENTORY - EXCLUDED, key=str)


@pytest.mark.parametrize(("error_type", "scope"), RESOLVED, ids=[f"{error_type}-{scope}" for error_type, scope in RESOLVED])
def test_the_reference_resolves_every_debug_refusal(reference: AgenticHILToolService, error_type: str, scope: str | None) -> None:
    key = catalogue_key(error_type, scope)
    uri = ERROR_URI_PREFIX + key
    response = handle_mcp_message({"jsonrpc": "2.0", "id": 1, "method": "resources/read", "params": {"uri": uri}}, reference)

    assert isinstance(response, dict), response
    assert "error" not in response, response
    contents = response["result"]["contents"]
    assert [content["uri"] for content in contents] == [uri]
    entry = json.loads(contents[0]["text"])
    assert entry == catalogue_entry(key)
    assert entry["error_type"] == error_type
    assert entry["meaning"].strip(), entry
    assert entry["remediation"], entry
    # The wrong fix is asked of the entries written here; the older debugger
    # entries are rewritten by a change of their own and left as they are.
    if (error_type, scope) in WRITTEN_HERE:
        assert entry.get("do_not"), entry


# ---------------------------------------------------------------------------
# What the entries may not say (#637).

NEGATION = re.compile(r"\b(not|cannot|never|no)\b|n't\b", re.IGNORECASE)
REPETITION = re.compile(r"\b(again|retry|retried|repeat|repeated)\b", re.IGNORECASE)


def promises_a_retried_stop(step: str) -> bool:
    """Whether a step offers another `debug_stop_session` as what ends the session."""
    return any("debug_stop_session" in sentence and REPETITION.search(sentence) and not NEGATION.search(sentence) for sentence in re.split(r"(?<=[.!?])\s+", step))


@pytest.mark.parametrize("error_type", ["halt_not_confirmed", "detach_resume_not_confirmed"])
def test_an_unsettled_stop_names_the_call_that_settles_it(error_type: str) -> None:
    """#637: a retried `debug_stop_session` cannot settle the session; `probe_target` or `debug_start_session` does."""
    advice = remediation_fields(error_type, "openocd")

    assert advice, f"no entry answers {error_type} under openocd"
    assert any("probe_target" in step or "debug_start_session" in step for step in advice["remediation"]), advice["remediation"]
    assert not [step for step in advice["remediation"] if promises_a_retried_stop(step)], advice["remediation"]


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
    # debug_stop_session
    Refusal("debug_stop_session", "cleanup_failed", "openocd", stopped_with_a_gdb_that_will_not_close),
    Refusal("debug_stop_session", "halt_not_confirmed", "openocd", stopped_without("_confirm_halted_before_end")),
    Refusal("debug_stop_session", "detach_resume_not_confirmed", "openocd", stopped_without("_pin_no_resume_on_detach")),
    # run control and state
    Refusal("debug_continue", "unexpected_breakpoint", "openocd", continued_into("unexpected_breakpoint")),
    Refusal("debug_continue", "target_exception", "openocd", continued_into("hardfault")),
    Refusal("debug_halt", "target_exception", "openocd", halted_after_a_fault),
    Refusal("debug_get_stop_reason", "stop_reason_not_available", "openocd", asked_for_a_stop_reason_before_any_stop),
    Refusal("debug_get_session_status", "audit_broken", "openocd", called_after_the_audit_broke("debug_get_session_status", {})),
    Refusal("debug_list_breakpoints", "audit_broken", "openocd", called_after_the_audit_broke("debug_list_breakpoints", {})),
    Refusal("debug_set_breakpoint", "audit_broken", "openocd", called_after_the_audit_broke("debug_set_breakpoint", {"location": {"symbol": "test_done"}})),
    Refusal("debug_clear_breakpoints", "breakpoint_reconciliation_failed", "openocd", cleared_with_an_unreadable_breakpoint_list),
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
    # pyOCD's post-flash reset relabels a refusal whose advice was merged for another type (pyocd.py).
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
    advice = remediation_fields(refusal.error_type, refusal.scope)
    assert advice, f"no catalogue entry answers {refusal.error_type} under {refusal.scope}"
    assert result.get("remediation") == advice["remediation"], result
    assert result.get("do_not") == advice.get("do_not"), result


DEBUG_TOOLS = frozenset(name for name in TOOLS_DEBUG_FUNCTIONS if not name.startswith("_") and not name.endswith("_error"))


def test_every_pair_written_here_and_every_debug_tool_is_driven() -> None:
    driven = {(refusal.error_type, refusal.scope) for refusal in REFUSALS}

    assert sorted(WRITTEN_HERE - driven, key=str) == []
    assert sorted(DEBUG_TOOLS - {refusal.tool for refusal in REFUSALS}) == []
