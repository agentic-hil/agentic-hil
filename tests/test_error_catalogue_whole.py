"""Every error_type the package can hand a caller resolves at its reference URI.

AGENTS.md promises that `agentic-hil://reference/errors` holds every
`error_type` with its meaning, its ordered fix and the wrong fix. Five guards
hold that promise area by area, each over the modules it names. None of them
holds it for the code base: a refusal written in a module no area claims, the
command line's own refusals among them, reached nobody's guard and resolved to
nothing.

This guard reads every module of the package, the debugger backends included,
with the debug guard's reader, and collects every literal a caller can read as
an `error_type`: a dict value under the key, an `error_type=` keyword, a
subscript assignment and `setdefault`, the first argument of an exception
whose constructor takes the type, and the argument of every helper that writes
its own parameter into the field, followed to each caller in the package. Each
type is held together with the scope its advice is looked up under at that
site, read the way the debug guard reads it; a site whose scope cannot be read
is held to the bare key, which every lookup falls back to.

Every pair resolves through the real MCP resource read, the scoped key first
and the bare key after it, exactly as `knowledge.lookup_remedy` does, or sits in
`EXCLUDED` with a reason a test reads off the code. The number of modules and
of types is pinned, so the scan cannot shrink unnoticed, and planted refusals
prove each shape reaches the inventory and fails the guard.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import json
import re
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from types import MappingProxyType, ModuleType

import pytest
from conftest import write_config
from test_error_catalogue_can import NOT_RETURNED_BY_A_TOOL
from test_error_catalogue_ec1_debug import (
    PINNED_EXPRESSIONS,
    SILENT,
    Reader,
    Scope,
    Source,
    UnreadableValue,
    _argument,
    _assigned,
    _calls_to,
    _merge_beside,
    _merges_in_function,
    _parameters,
    attach_scopes,
    closed,
    error_type_expressions,
)
from test_error_catalogue_ec2_artifacts_reports import DYNAMIC_SITES as ARTIFACT_DYNAMIC_SITES
from test_error_catalogue_ec2_artifacts_reports import EXCLUDED_SITES as ARTIFACT_EXCLUDED_SITES
from test_error_catalogue_ec2_artifacts_reports import Facts, clauses, fact_problems
from test_error_catalogue_ec3_run_coordination import CONSUMERS
from test_error_catalogue_ec3_run_coordination import EXCLUDED as RUN_EXCLUDED
from test_error_catalogue_ec3_run_coordination import PINNED_DYNAMIC as RUN_DYNAMIC_SITES

import agentic_hil
from agentic_hil.backends import common, gdbdebug, openocd, pyocd, stlink
from agentic_hil.config import load_config
from agentic_hil.knowledge import ERROR_URI_PREFIX, catalogue_entry, lookup_remedy
from agentic_hil.mcp import MCP_RESOURCE_NOT_FOUND, handle_mcp_message
from agentic_hil.report import SUCCESS_CHECKS, classify_failure_report, write_report
from agentic_hil.tools import AgenticHILToolService

PACKAGE = Path(str(agentic_hil.__file__)).parent
Pair = tuple[str, str | None]
# (module, function, expression), the key the run and artifact guards pin
# their computed sites under; the module is named relative to the package.
DynamicSite = tuple[str, str, str]

# ---------------------------------------------------------------------------
# Reading the package.


def module_names() -> tuple[str, ...]:
    """Every module of the package, by its import name."""
    names = (".".join(("agentic_hil", *path.relative_to(PACKAGE).with_suffix("").parts)).removesuffix(".__init__") for path in PACKAGE.rglob("*.py"))
    return tuple(sorted(names))


def short_name(module: ModuleType) -> str:
    return module.__name__.removeprefix("agentic_hil.") if module.__name__ != "agentic_hil" else "__init__"


def qualname(source: Source, node: ast.AST | None) -> str:
    """The dotted name of the functions and classes `node` is written in, itself included."""
    names = []
    while node is not None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.append(node.name)
        node = source.parents.get(node)
    return ".".join(reversed(names)) or "<module>"


def _callee(call: ast.Call) -> str | None:
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return None


@cache
def producer_classes() -> Mapping[str, int]:
    """Classes of the package whose constructor takes the error type, and at which position."""
    found: dict[str, int] = {}
    for name in module_names():
        module = importlib.import_module(name)
        for value in vars(module).values():
            if isinstance(value, type) and value.__module__ == name and "__init__" in vars(value):
                parameters = list(inspect.signature(vars(value)["__init__"]).parameters)
                if "error_type" in parameters:
                    found[value.__name__] = parameters.index("error_type") - 1
    return MappingProxyType(found)


# A function the reader followed a parameter of: (module, line of its `def`, parameter).
Followed = tuple[str, int, str]


def package_classes(cls: type) -> Iterator[type]:
    """`cls` and every class of the package below it. Classes a test defines are not the product's."""
    yield cls
    for subclass in cls.__subclasses__():
        if subclass.__module__.split(".")[0] == "agentic_hil":
            yield from package_classes(subclass)


Part = tuple[str, Callable[[], frozenset]]


class PackageReader(Reader):
    """The debug guard's reader, widened to what the rest of the package writes.

    It reads an f-string as every combination of its parts, and `self.<name>`
    as the class constant of each class an instance can be: the package's
    classes below the one the method is written in that nothing subclasses. A
    class passed over for that is recorded, and a test holds that nothing
    constructs one. Of an expression with branches, a name with several
    assignments, or a `.get` with a default, it keeps every part it can read
    and records each part it cannot, so a literal behind a computed value
    still reaches the inventory. It records each parameter it follows to the
    callers of its function.
    """

    def __init__(self) -> None:
        super().__init__()
        self.unread: set[DynamicSite] = set()
        self.followed: set[Followed] = set()
        self.passed_over: set[type] = set()

    def values(self, node: ast.expr, scope: Scope) -> frozenset[str | None]:
        if scope.function is not None and (scope.source.name, scope.function.name, ast.unparse(node)) in PINNED_EXPRESSIONS:
            return super().values(node, scope)
        if isinstance(node, ast.JoinedStr):
            combinations = frozenset({""})
            for part in node.values:
                if isinstance(part, ast.FormattedValue):
                    pieces = [piece for piece in self.values(part.value, scope) if piece is not None]
                    combinations = frozenset(combination + piece for combination in combinations for piece in pieces)
                else:
                    combinations = frozenset(combination + str(part.value) for combination in combinations)  # type: ignore[attr-defined]
            return frozenset(combinations)
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "self":
            return self._class_constant(node, scope)
        if isinstance(node, ast.IfExp):
            return self._some(scope, [self._part(branch, scope) for branch in (node.body, node.orelse)])
        if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or):
            return self._some(scope, [self._part(value, scope) for value in node.values])
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "get" and not isinstance(node.func.value, ast.Dict) and len(node.args) == 2:
            # What the mapping holds is somebody else's value; the default is written here.
            self._record(scope, ast.unparse(node))
            return self.values(node.args[1], scope)
        return super().values(node, scope)

    def _part(self, node: ast.expr, scope: Scope) -> Part:
        return ast.unparse(node), lambda: self.values(node, scope)

    def _record(self, scope: Scope, text: str) -> None:
        function = qualname(scope.source, scope.function) if scope.function is not None else "<module>"
        self.unread.add((short_name(scope.source.module), function, text))

    def _some(self, scope: Scope, parts: list[Part]) -> frozenset[str | None]:
        """Every part that can be read. The others are recorded; only all of them unread is unreadable."""
        values: frozenset[str | None] = frozenset()
        unread = []
        for text, read in parts:
            try:
                values |= read()
            except UnreadableValue:
                unread.append(text)
        if len(unread) == len(parts):
            raise UnreadableValue(f"{scope.source.name}: {unread}")
        for text in unread:
            self._record(scope, text)
        return values

    def _name(self, name: str, scope: Scope) -> frozenset[str | None]:
        if name in dict(scope.bindings) or scope.function is None:
            return super()._name(name, scope)
        assigned = _assigned(scope.function, name)
        is_parameter = name in {argument.arg for argument in _parameters(scope.function)}
        if not (assigned or is_parameter):
            return super()._name(name, scope)
        parts = [self._part(value, scope) for value in assigned]
        if is_parameter:
            parts.append((name, lambda: self._parameter(name, scope)))
        return self._some(scope, parts)

    def _parameter(self, name: str, scope: Scope) -> frozenset[str | None]:
        assert scope.function is not None
        self.followed.add((scope.source.module.__name__, scope.function.lineno, name))
        if scope.source.enclosing_class(scope.function) is None and not _calls_to(scope.source, scope.function.name):
            # A module function only other modules call: `scan` reads those calls.
            return frozenset()
        return super()._parameter(name, scope)

    def _returns(self, source: Source, function: ast.FunctionDef, bindings: tuple[tuple[str, frozenset[str | None]], ...]) -> frozenset[str | None]:
        key = (source.name, function.lineno, "<return>")
        if key in self._active:
            return frozenset()
        self._active.add(key)
        try:
            callee = Scope(source, function, bindings)
            parts = [self._part(node.value, callee) for node in ast.walk(function) if isinstance(node, ast.Return) and node.value is not None]
            return self._some(callee, parts) if parts else frozenset()
        finally:
            self._active.discard(key)

    def _class_constant(self, node: ast.Attribute, scope: Scope) -> frozenset[str | None]:
        owner_name = scope.source.enclosing_class(scope.function) if scope.function is not None else None
        owner = getattr(scope.source.module, owner_name, None) if owner_name else None
        if not isinstance(owner, type):
            raise UnreadableValue(f"{scope.source.name}:{node.lineno}: {ast.unparse(node)}")
        classes = list(package_classes(owner))
        leaves = [cls for cls in classes if not any(other is not cls and cls in other.__mro__ for other in classes)]
        values = [getattr(cls, node.attr, None) for cls in leaves]
        if not all(isinstance(value, str) for value in values):
            raise UnreadableValue(f"{scope.source.name}:{node.lineno}: {ast.unparse(node)}")
        self.passed_over.update(cls for cls in classes if cls not in leaves and isinstance(vars(cls).get(node.attr), str))
        return frozenset(values)


# The modules whose refusals are filled, or merged, under the backend that
# answers: the debug guard's rule for them (`attach_scopes`).
BACKEND_SCOPED_MODULES = frozenset({gdbdebug.__name__, openocd.__name__, pyocd.__name__, stlink.__name__, common.__name__})


def site_scopes(source: Source, node: ast.expr, values: frozenset[str | None]) -> tuple[str | None, ...]:
    """The scopes a refusal written at `node` is looked up under.

    A merge beside the type, or one in the same function that covers it, names
    its scope; a debugger backend's refusal without one is filled under the
    backend. Anywhere else the bare key is the one held: a lookup under any
    scope falls back to it, so requiring it never passes a pair the lookup
    misses. A merge whose scope the reader cannot evaluate is held the same way.
    """
    function = source.enclosing_function(node)
    scope = Scope(source, function)

    def unmerged() -> tuple[str | None, ...]:
        if source.module.__name__ in BACKEND_SCOPED_MODULES:
            return attach_scopes(source, node, function.name if function is not None else "<module>")
        return (None,)

    try:
        beside = _merge_beside(source, node, scope)
    except UnreadableValue:
        return (None,)
    if beside is not None:
        types, scopes = beside
        assert types == values, f"{source.name}:{node.lineno}: merges the remediation of {sorted(types, key=str)} beside {sorted(values, key=str)}"
        return scopes if scopes is not None else unmerged()
    try:
        merges = _merges_in_function(function, scope)
    except UnreadableValue:
        return (None,)
    matching = [scopes for types, scopes in merges if values <= types]
    if matching:
        return tuple(sorted({one for scopes in matching for one in (scopes if scopes is not None else unmerged())}, key=str))
    return unmerged()


@dataclass(frozen=True)
class Inventory:
    """What the scan read: the modules, each (type, scope) pair with its sites, and the sites it could not read."""

    modules: tuple[str, ...]
    pairs: Mapping[Pair, frozenset[tuple[str, str, int]]]
    dynamic: frozenset[DynamicSite]
    followed: frozenset[Followed]
    passed_over: frozenset[type]

    @property
    def types(self) -> frozenset[str]:
        return frozenset(error_type for error_type, _scope in self.pairs)

    def sites(self, pair: Pair) -> frozenset[tuple[str, str]]:
        return frozenset((module, function) for module, function, _line in self.pairs.get(pair, ()))


def _is_consumer_keyword(source: Source, node: ast.expr) -> bool:
    parent = source.parents.get(node)
    if not isinstance(parent, ast.keyword):
        return False
    call = source.parents.get(parent)
    return isinstance(call, ast.Call) and _callee(call) in CONSUMERS


def _sites(source: Source) -> Iterator[ast.expr]:
    """Every expression `source` writes as an error type."""
    producers = producer_classes()
    for node in error_type_expressions(source):
        if not _is_consumer_keyword(source, node):
            yield node
    for node in ast.walk(source.tree):
        if not isinstance(node, ast.Call):
            continue
        callee = _callee(node)
        if callee in producers and len(node.args) > producers[callee]:
            yield node.args[producers[callee]]
        elif callee == "__setitem__" and len(node.args) == 2 and isinstance(node.args[0], ast.Constant) and node.args[0].value == "error_type":
            yield node.args[1]


def function_at(source: Source, line: int) -> ast.FunctionDef:
    (function,) = [function for functions in source.functions.values() for function in functions if function.lineno == line]
    return function


def calls_from_other_modules(sources: Mapping[str, Source], home: str, name: str) -> Iterator[tuple[Source, ast.Call]]:
    """Calls to the module function `name` of `home` from every other module, by its name or through its module."""
    for module_name, source in sources.items():
        if module_name == home:
            continue
        for node in ast.walk(source.tree):
            if isinstance(node, ast.Call) and ((isinstance(node.func, ast.Name) and node.func.id == name) or (isinstance(node.func, ast.Attribute) and node.func.attr == name and isinstance(node.func.value, ast.Name))):
                yield source, node


def scan(*, modules: tuple[str, ...] | None = None, planted: Mapping[str, str] = MappingProxyType({})) -> Inventory:
    """The inventory of `modules`, every module of the package by default.

    `planted` replaces a module's source with the text given, which is how the
    tests below prove that each shape of refusal reaches the inventory.
    """
    names = module_names() if modules is None else modules
    sources = {name: Source.of(importlib.import_module(name), planted.get(name)) for name in names}
    reader = PackageReader()
    pairs: dict[Pair, set[tuple[str, str, int]]] = {}
    dynamic: set[DynamicSite] = set()
    followed: set[Followed] = set()

    def read(source: Source, node: ast.expr) -> frozenset[str | None] | None:
        function = source.enclosing_function(node)
        try:
            return reader.values(node, Scope(source, function)) - {None}
        except UnreadableValue:
            dynamic.add((short_name(source.module), qualname(source, function) if function is not None else "<module>", ast.unparse(node)))
            return None

    def add(source: Source, node: ast.expr, values: frozenset[str | None], scopes: tuple[str | None, ...]) -> None:
        site = (short_name(source.module), qualname(source, node), node.lineno)
        for value in values:
            for scope in scopes:
                pairs.setdefault((value, scope), set()).add(site)  # type: ignore[arg-type]

    for source in sources.values():
        for node in _sites(source):
            reader.followed.clear()
            values = read(source, node)
            if values is None:
                continue
            scopes = site_scopes(source, node, values)
            add(source, node, values, scopes)
            # A module function whose parameter carries the type is called from
            # other modules too, and each of those calls is a site of its own,
            # looked up the way the function's own site is.
            for home, line, parameter in sorted(reader.followed):
                function = function_at(sources[home], line)
                if sources[home].enclosing_class(function) is not None:
                    continue
                for caller, call in calls_from_other_modules(sources, home, function.name):
                    argument = _argument(call, function, parameter)
                    if argument is not None:
                        forwarded = read(caller, argument)
                        if forwarded is not None:
                            add(caller, argument, forwarded, scopes)
            followed |= reader.followed
    dynamic |= reader.unread
    return Inventory(
        tuple(names),
        MappingProxyType({pair: frozenset(sites) for pair, sites in pairs.items()}),
        frozenset(dynamic),
        frozenset(followed),
        frozenset(reader.passed_over),
    )


@cache
def scanned() -> Inventory:
    return scan()


@cache
def package_sources() -> Mapping[str, Source]:
    """Every module's source, by its name relative to the package."""
    return MappingProxyType({short_name(module): Source.of(module) for module in map(importlib.import_module, module_names())})


def written_in(source: Source, node: ast.AST) -> str:
    function = source.enclosing_function(node)
    return qualname(source, function) if function is not None else "<module>"


@cache
def package_calls() -> Mapping[str | None, tuple[tuple[str, Source, ast.Call], ...]]:
    """Every call the package writes, by the name it calls, with its module and source."""
    calls: dict[str | None, list[tuple[str, Source, ast.Call]]] = {}
    for module, source in package_sources().items():
        for node in ast.walk(source.tree):
            if isinstance(node, ast.Call):
                calls.setdefault(_callee(node), []).append((module, source, node))
    return MappingProxyType({name: tuple(found) for name, found in calls.items()})


def callers(name: str) -> frozenset[tuple[str, str]]:
    """Where the package calls a function or method named `name`, as (module, function)."""
    return frozenset((module, written_in(source, node)) for module, source, node in package_calls().get(name, ()))


def module_function_callers(module: str, name: str) -> frozenset[tuple[str, str]]:
    """Where the package calls the module function `name` of `module`: by name where it is in scope, or through its module."""
    last = module.rsplit(".", 1)[-1]
    found = set()
    for other, source in package_sources().items():
        imports = [alias for node in ast.walk(source.tree) if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[-1] == last for alias in node.names if alias.name == name]
        assert all(alias.asname is None for alias in imports), f"{other} imports {name} under another name"
        in_scope = other == module or bool(imports)
        for node in ast.walk(source.tree):
            if not isinstance(node, ast.Call):
                continue
            by_name = in_scope and isinstance(node.func, ast.Name) and node.func.id == name
            through_module = isinstance(node.func, ast.Attribute) and node.func.attr == name and isinstance(node.func.value, ast.Name) and node.func.value.id == last
            if by_name or through_module:
                found.add((other, written_in(source, node)))
    return frozenset(found)


def definition(module: str, name: str) -> ast.FunctionDef:
    """The function `name` (dotted through its classes) of `module`."""
    source = package_sources()[module]
    (function,) = [function for functions in source.functions.values() for function in functions if qualname(source, function) == name]
    return function


def same(node: ast.AST, text: str) -> bool:
    return ast.dump(node) == ast.dump(ast.parse(text, mode="eval").body)


def assigns_field(statements: list[ast.stmt], name: str) -> list[ast.Assign]:
    """The statements of one block that assign `<something>[name]`."""
    return [
        statement
        for statement in statements
        if isinstance(statement, ast.Assign)
        and any(isinstance(target, ast.Subscript) and isinstance(target.slice, ast.Constant) and target.slice.value == name for target in statement.targets)
    ]


def blocks(function: ast.FunctionDef) -> Iterator[list[ast.stmt]]:
    """Every list of statements in `function`, its own body included."""
    for node in ast.walk(function):
        for name in ("body", "orelse", "finalbody"):
            statements = getattr(node, name, None)
            if isinstance(statements, list) and statements and isinstance(statements[0], ast.stmt):
                yield statements


# ---------------------------------------------------------------------------
# What the pins hold.

SCANNED_MODULE_COUNT = 47
COLLECTED_TYPE_COUNT = 214


def pin_problems(inventory: Inventory) -> list[str]:
    problems = []
    if len(inventory.modules) != SCANNED_MODULE_COUNT:
        problems.append(f"the scan read {len(inventory.modules)} modules, and the package has {SCANNED_MODULE_COUNT}")
    if len(inventory.types) != COLLECTED_TYPE_COUNT:
        problems.append(f"the scan collected {len(inventory.types)} types, and {COLLECTED_TYPE_COUNT} are pinned")
    return problems


# Every expression a type is computed from rather than written, with what it
# carries. Each is a value the scan reads where it is first written, or names
# below. The run and artifact guards pin most of them under the same key.
PINNED_DYNAMIC: dict[DynamicSite, str] = {
    **ARTIFACT_DYNAMIC_SITES,
    **{
        site: RUN_DYNAMIC_SITES[site]
        for site in (
            ("coordination", "HardwareCoordinator._quarantine_registered_lease", "type(error).__name__"),
            ("coordination", "HardwareCoordinator.begin_run", "error.error_type"),
            ("coordination", "HardwareCoordinator.record_cleanup_event", "type(error).__name__"),
            ("coordination", "HardwareCoordinator.release_lease", "type(persist_error).__name__"),
            ("runlifecycle", "RunRegistration.finish", "result.get('error_type')"),
            ("runlifecycle", "_detached_terminal_result", "record.get('error_type')"),
            ("test_reactor", "DebuggerRunner._debug_start", "result.get('target_error_type', 'target_stop')"),
            ("test_reactor", "TestReactor.run", "validation_error.get('error_type')"),
            ("test_reactor", "result_error_type", "result.get('target_error_type')"),
        )
    },
    ("test_reactor", "result_error_type", "str(result['error_type'])"): RUN_DYNAMIC_SITES[("test_reactor", "result_error_type", "result['error_type']")],
    ("test_reactor", "TestReactor.run", "aborted.failure"): "the failed step's own result, whose type result_error_type passes up to the run",
    ("test_reactor", "propagate_result_status", "target_failures[0]['target_error_type']"): "the first failed source's target type, copied to the aggregate",
    ("canbroker", "_explained_exit_refusal", "document.get('error_type')"): (
        "the refusal the broker's `main` printed to its log before it exited, passed on under its own type: a ConfigError, "
        "can_bus_not_shared, device_busy, or the adapter's own refusal, each read where it is written"
    ),
    ("canbroker", "main", "error.error_type"): "a ConfigError from loading the authoritative configuration, printed to the broker log under its own type",
    ("cli", "_init_open_run_unchecked", "refusal.error_type"): "a ConfigError from writable_stable_directory, kept under its own type in the init finding",
    ("cli", "_plan_check_configuration", "error.error_type"): "a ConfigError from loading the authoritative configuration, reported under its own type",
    ("cli", "check_plan", "detail.get('error_type')"): "a ConfigError from load_test_config, reported under its own type for the plan it refused",
    ("cli", "mcp_server_command", "error.error_type"): "a ConfigError one rejected candidate raised, nested under `rejected_candidates` of mcp_command_untrusted",
    ("config", "ConfigError.to_dict", "self.error_type"): "the constructor's argument, read at every construction of ConfigError in the package",
    ("knowledge", "catalogue_entry", "error_type"): "the catalogue key's own type, written back into the entry the reference serves for it",
    ("runevidence", "_run", "str(report['error_type'])"): "a run report's recorded type, copied into the run evidence",
}


# ---------------------------------------------------------------------------
# The exclusions, each with the code its reason rests on.


@dataclass(frozen=True)
class Exclusion:
    """A pair that needs no entry: why, where the scan finds it, and the check that reads the why off the code."""

    reason: str
    sites: frozenset[tuple[str, str]]
    check: Callable[[Path], None]


def check_silent(pair: Pair) -> Callable[[Path], None]:
    def check(_tmp_path: Path) -> None:
        assert pair in SILENT

    return check


def _participant_requests() -> frozenset[str]:
    """Participant's methods that put a request to the broker, `_request` itself included."""
    (participant,) = [node for node in package_sources()["canbroker"].tree.body if isinstance(node, ast.ClassDef) and node.name == "Participant"]
    return frozenset(
        method.name for method in participant.body if isinstance(method, ast.FunctionDef) and any(isinstance(node, ast.Call) and _callee(node) == "_request" for node in ast.walk(method))
    ) | {"_request"}


def module_functions_reached(module: str, start: str) -> dict[str, ast.FunctionDef]:
    """`start` and every module function of `module` it reaches by name."""
    top = {node.name: node for node in package_sources()[module].tree.body if isinstance(node, ast.FunctionDef)}
    reached: dict[str, ast.FunctionDef] = {}
    pending = [start]
    while pending:
        name = pending.pop()
        if name in reached:
            continue
        reached[name] = top[name]
        pending.extend(node.func.id for node in ast.walk(top[name]) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in top)
    return reached


def check_broker_timeout(_tmp_path: Path) -> None:
    """Only the attach is answered from a ParticipantError, and the attach puts no request."""
    catching = set()
    for module, source in package_sources().items():
        for node in ast.walk(source.tree):
            if isinstance(node, ast.Name) and node.id == "ParticipantError" and not any(isinstance(parent, ast.Raise) for parent in _ancestors(source, node)):
                catching.add((module, written_in(source, node)))
    assert catching == {("can", "CanBusService._participant_session_start"), ("canbroker", "Participant.detach")}
    function = definition("can", "CanBusService._participant_session_start")
    (attach,) = [node for node in ast.walk(function) if isinstance(node, ast.Try) and any("ParticipantError" in ast.unparse(handler) for handler in node.handlers)]
    assert {_callee(node) for statement in attach.body for node in ast.walk(statement) if isinstance(node, ast.Call)} == {"attach_participant"}
    # The attach puts no request: a Participant is built only to be returned,
    # what holds one only hands it on, and building one asks the broker nothing.
    source = package_sources()["canbroker"]
    requests = _participant_requests()
    assert requests >= {"_request", "send", "read", "status", "detach"}
    chain = module_functions_reached("canbroker", "attach_participant")
    assert "_attach_once" in chain
    for function in chain.values():
        held = {
            target.id
            for node in ast.walk(function)
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call) and _callee(node.value) in {"Participant", *chain}
            for target in node.targets
            if isinstance(target, ast.Name)
        }
        for node in ast.walk(function):
            if isinstance(node, ast.Call) and _callee(node) == "Participant":
                assert isinstance(source.parents[node], ast.Return), f"{function.name}: {ast.unparse(node)}"
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name) and node.func.value.id in held:
                assert node.func.attr not in requests, f"{function.name}: {ast.unparse(node)}"
    built = definition("canbroker", "Participant.__init__")
    assert not [ast.unparse(node) for node in ast.walk(built) if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and ast.unparse(node.func.value) == "self" and node.func.attr in requests]


def _ancestors(source: Source, node: ast.AST) -> Iterator[ast.AST]:
    while node in source.parents:
        node = source.parents[node]
        yield node


def check_broker_not_attached(_tmp_path: Path) -> None:
    """Every connection to a broker either sends the attach first or is closed unused."""
    assert callers("Client") == {("canbroker", "CanBroker._finish_stop"), ("canbroker", "_attach_once")}
    wake = definition("canbroker", "CanBroker._finish_stop")
    assert [ast.unparse(node) for node in ast.walk(wake) if isinstance(node, ast.Call) and _callee(node) == "Client"] == ["Client(self._endpoint, endpoint_family())"]
    assert any(
        isinstance(node, ast.Call) and _callee(node) == "close" and isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Call) and _callee(node.func.value) == "Client"
        for node in ast.walk(wake)
    )
    attach = definition("canbroker", "_attach_once")
    sends = sorted((node for node in ast.walk(attach) if isinstance(node, ast.Call) and _callee(node) == "send"), key=lambda node: (node.lineno, node.col_offset))
    first = sends[0].args[0]
    assert isinstance(first, ast.Dict)
    fields = {key.value: value for key, value in zip(first.keys, first.values, strict=True) if isinstance(key, ast.Constant)}
    assert isinstance(fields["message"], ast.Constant) and fields["message"].value == "attach"


def check_every_step_runs_on_a_kind_that_serves_it(_tmp_path: Path) -> None:
    from agentic_hil.test_reactor import STEP_DEVICE_CLASSES, STEP_DEVICE_CLASSES_BY_ACTION

    for action, kinds in STEP_DEVICE_CLASSES_BY_ACTION.items():
        assert all(action in kind.step_action_specs for kind in kinds), action
    assert len({kind.kind for kind in STEP_DEVICE_CLASSES}) == len(STEP_DEVICE_CLASSES)
    chooser = definition("test_reactor", "step_device_class")
    assert any(isinstance(node, ast.Assign) and same(node.value, "step_device_classes(step.action)") and ast.unparse(node.targets[0]) == "candidates" for node in ast.walk(chooser))
    assert {ast.unparse(node.value) for node in ast.walk(chooser) if isinstance(node, ast.Return) and node.value is not None} == {
        "candidates[0] if candidates else None",
        "pinned[0]",
        "named[0] if len(named) == 1 else None",
    }
    assert {ast.unparse(node.iter) for node in ast.walk(chooser) if isinstance(node, ast.comprehension)} == {"candidates"}
    assert [ast.unparse(node.value) for node in ast.walk(definition("test_reactor", "TestReactor.step_device")) if isinstance(node, ast.Return)] == ["self.device_for(device_class, name)"]
    builder = definition("test_reactor", "TestReactor.device_for")
    assert any(isinstance(node, ast.Assign) and same(node.value, "(device_class.kind, config_id)") for node in ast.walk(builder))
    assert any(isinstance(node, ast.Call) and _callee(node) == "device_class" for node in ast.walk(builder))
    assert callers("execute") == {("test_reactor", "TestReactor.execute_step")}
    assert callers("routing_refusal") == {("devices", "Device.execute")}


def check_preflight_refuses_a_step_no_kind_serves(tmp_path: Path) -> None:
    from agentic_hil.config import ConfigError
    from agentic_hil.test_reactor import load_test_config

    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({"version": 2, "steps": [{"debugger": "dut", "action": "shell"}]}), encoding="utf-8")
    with pytest.raises(ConfigError) as refused:
        load_test_config(str(plan), str(tmp_path))
    assert refused.value.error_type == "test_config_invalid"
    assert callers("step_device") == {("test_reactor", "TestReactor.execute_step")}
    assert callers("execute_step") == {("test_reactor", "TestReactor.execute_recorded_step")}
    assert callers("execute_recorded_step") == {("test_reactor", "TestReactor.execute_steps")}
    assert callers("execute_steps") == {("test_reactor", "TestReactor.run"), ("test_reactor", "TestReactor.execute_repeat")}
    assert callers("execute_repeat") <= {("test_reactor", "TestReactor.execute_steps")}
    run = definition("test_reactor", "TestReactor.run")
    (refused_plan,) = [node for node in ast.walk(run) if isinstance(node, ast.If) and same(node.test, "validation_error is not None")]
    assert isinstance(refused_plan.body[-1], ast.Return)
    (steps,) = [node for node in ast.walk(run) if isinstance(node, ast.Call) and _callee(node) == "execute_steps"]
    assert refused_plan.lineno < steps.lineno
    step = definition("test_reactor", "TestReactor._preflight_step")
    for test in ("not candidates", "device_class is None"):
        (branch,) = [node for node in ast.walk(step) if isinstance(node, ast.If) and same(node.test, test)]
        assert isinstance(branch.body[-1], ast.Return), test
    assert ("test_reactor", "TestReactor._preflight_repeat") in callers("_preflight_steps")


def check_every_failed_target_names_its_type(_tmp_path: Path) -> None:
    from agentic_hil.test_reactor import propagate_result_status

    # Every place that can write the target's failure: the stop fields, and
    # the aggregate that copies them. A constant `True`, and a table that
    # describes the field, write no failure.
    writes = set()
    for module, source in package_sources().items():
        for node in ast.walk(source.tree):
            values: list[ast.expr] = []
            if isinstance(node, ast.Dict):
                values = [value for key, value in zip(node.keys, node.values, strict=True) if isinstance(key, ast.Constant) and key.value == "target_ok"]
            elif isinstance(node, ast.Assign):
                values = [node.value for target in node.targets if isinstance(target, ast.Subscript) and isinstance(target.slice, ast.Constant) and target.slice.value == "target_ok"]
            elif isinstance(node, ast.keyword) and node.arg == "target_ok":
                values = [node.value]
            if any(not (isinstance(value, ast.Constant) and (value.value is True or isinstance(value.value, str))) for value in values):
                writes.add((module, written_in(source, node)))
    assert writes == {("backends.gdbdebug", "target_stop_fields"), ("test_reactor", "propagate_result_status")}
    for reason in gdbdebug.ABNORMAL_STOP_REASONS:
        fields = gdbdebug.target_stop_fields({"stop_reason": reason})
        assert fields["target_ok"] is False and fields["target_error_type"], reason
    aggregate: dict = {}
    propagate_result_status(aggregate, [{"target_ok": False, "target_error_type": "target_exception"}])
    assert aggregate == {"target_ok": False, "target_error_type": "target_exception"}


def check_every_ended_run_names_its_type(_tmp_path: Path) -> None:
    terminal = set()
    for module, source in package_sources().items():
        for node in ast.walk(source.tree):
            if isinstance(node, ast.Call) and _callee(node) == "_write" and node.args and {"RUN_FINISHED", "RUN_STOPPED"} & {name.id for name in ast.walk(node.args[0]) if isinstance(name, ast.Name)}:
                terminal.add((module, written_in(source, node)))
    assert terminal == {("runlifecycle", "RunRegistration.__exit__"), ("runlifecycle", "RunRegistration.finish")}
    assert callers("RunRegistration") == frozenset()
    taken = {
        (module, written_in(source, node))
        for module, source in package_sources().items()
        for node in ast.walk(source.tree)
        if isinstance(node, ast.Call) and ast.unparse(node.func) == "RunRegistration.take"
    }
    assert taken == {("reactorrun", "run_plan")}
    (registered,) = [node for node in ast.walk(definition("reactorrun", "run_plan")) if isinstance(node, ast.With) and [ast.unparse(item.context_expr) for item in node.items] == ["registration"]]
    assert ast.unparse(registered.body[-1]) == "registration.finish(result)"
    assert not any(isinstance(node, (ast.Return, ast.Break, ast.Continue)) for node in ast.walk(registered))
    # Every failed run result names its type: the reactor's own, and the
    # refusals and cleanup failures run_registered_plan builds around it.
    run = definition("test_reactor", "TestReactor.run")
    assert any(isinstance(node, ast.Assign) and ast.unparse(node.targets[0]) == "ok" and same(node.value, "failure is None and cleanup_ok and not stopped") for node in ast.walk(run))
    (branch,) = [node for node in ast.walk(run) if isinstance(node, ast.If) and same(node.test, "failure is not None") and node.orelse]
    tests = []
    while isinstance(branch, ast.If):
        tests.append(ast.unparse(branch.test))
        assert assigns_field(branch.body, "error_type"), tests[-1]
        branch = branch.orelse[0] if len(branch.orelse) == 1 else None  # type: ignore[assignment]
    assert tests == ["failure is not None", "stopped", "not cleanup_ok"]
    plan = definition("reactorrun", "run_registered_plan")
    for node in ast.walk(plan):
        if isinstance(node, ast.Dict):
            fields = {key.value: value for key, value in zip(node.keys, node.values, strict=True) if isinstance(key, ast.Constant)}
            if isinstance(fields.get("ok"), ast.Constant) and fields["ok"].value is False:  # type: ignore[union-attr]
                assert "error_type" in fields or None in node.keys, ast.unparse(node)
    for statements in blocks(plan):
        if any(isinstance(statement.value, ast.Constant) and statement.value.value is False for statement in assigns_field(statements, "ok")):
            assert assigns_field(statements, "error_type")


def check_every_command_is_dispatched(_tmp_path: Path) -> None:
    import argparse

    from agentic_hil import cli

    (commands,) = [action for action in cli.build_parser()._actions if isinstance(action, argparse._SubParsersAction)]
    handled: set[str] = set()
    dispatch = definition("cli", "dispatch")
    for node in ast.walk(dispatch):
        if isinstance(node, ast.Compare) and ast.unparse(node.left) == "args.command":
            (operator,), (right,) = node.ops, node.comparators
            if isinstance(operator, ast.Eq) and isinstance(right, ast.Constant):
                handled.add(right.value)
            elif isinstance(operator, ast.In) and isinstance(right, ast.Set):
                handled.update(element.value for element in right.elts if isinstance(element, ast.Constant))
            elif isinstance(operator, ast.In) and isinstance(right, ast.Name):
                handled.update(getattr(cli, right.id))
    assert set(commands.choices) <= handled, sorted(set(commands.choices) - handled)
    assert module_function_callers("cli", "dispatch") == {("cli", "entrypoint")}
    entry = definition("cli", "entrypoint")
    (no_command,) = [node for node in ast.walk(entry) if isinstance(node, ast.If) and same(node.test, "not command")]
    assert isinstance(no_command.body[-1], ast.Return)
    assert no_command.lineno < min(node.lineno for node in ast.walk(entry) if isinstance(node, ast.Call) and _callee(node) == "dispatch")


FIELD_DESCRIPTION = "stable machine-readable identifier; branch on this"


def check_field_description(_tmp_path: Path) -> None:
    from agentic_hil.knowledge import errors_document

    assert errors_document()["result_fields"]["error_type"] == FIELD_DESCRIPTION


SILENT_REASON = (
    "#516 keeps this backend's advice for the type silent until somebody writes that tool's own steps; "
    "test_debug_backend_refusals.py pins it silent and holds that the type grows no bare entry the service's fill could fall back to"
)

EXCLUDED: dict[Pair, Exclusion] = {
    ("flash_failed", "stlink"): Exclusion(
        SILENT_REASON, frozenset({("backends.stlink", "STLinkBackend._failure_result"), ("backends.stlink", "STLinkBackend.info")}), check_silent(("flash_failed", "stlink"))
    ),
    ("memory_read_failed", "openocd"): Exclusion(
        SILENT_REASON, frozenset({("backends.gdbdebug", "GdbDebugSessions._read_memory_bytes")}), check_silent(("memory_read_failed", "openocd"))
    ),
    ("memory_read_failed", "stlink"): Exclusion(
        SILENT_REASON,
        frozenset({("backends.stlink", "STLinkBackend._failure_result"), ("backends.stlink", "STLinkBackend.debug_symbol_value")}),
        check_silent(("memory_read_failed", "stlink")),
    ),
    ("verify_failed", "openocd"): Exclusion(
        SILENT_REASON,
        frozenset({("backends.gdbdebug", "GdbDebugSessions._start_failure"), ("backends.openocd", "OpenOCDBackend._failure_result"), ("backends.openocd", "OpenOCDBackend.info")}),
        check_silent(("verify_failed", "openocd")),
    ),
    ("can_broker_timeout", None): Exclusion(
        NOT_RETURNED_BY_A_TOOL["can_broker_timeout"], frozenset({("canbroker", "Participant._request")}), check_broker_timeout
    ),
    ("can_broker_not_attached", None): Exclusion(
        NOT_RETURNED_BY_A_TOOL["can_broker_not_attached"], frozenset({("canbroker", "CanBroker._serve_connection")}), check_broker_not_attached
    ),
    ("not_supported", None): Exclusion(
        "a step runs only on a device of a kind STEP_DEVICE_CLASSES_BY_ACTION lists for its action, and every kind listed serves it, "
        "so StepDevice.execute never meets an action its kind does not serve; and DebuggerDevice.routing_refusal: "
        + ARTIFACT_EXCLUDED_SITES[("devices", "DebuggerDevice.routing_refusal")],
        frozenset({("devices", "DebuggerDevice.routing_refusal"), ("test_reactor", "StepDevice.execute")}),
        check_every_step_runs_on_a_kind_that_serves_it,
    ),
    ("unknown_action", None): Exclusion(
        RUN_EXCLUDED["unknown_action"]
        + ", and TestReactor.run refuses in preflight, before any step runs, a step no kind serves and a step whose `device:` names no single configured entry",
        frozenset({("test_reactor", "TestReactor.step_device")}),
        check_preflight_refuses_a_step_no_kind_serves,
    ),
    ("target_stop", None): Exclusion(RUN_EXCLUDED["target_stop"], frozenset({("test_reactor", "DebuggerRunner._debug_start")}), check_every_failed_target_names_its_type),
    ("target_failed", None): Exclusion(
        RUN_EXCLUDED["target_failed"],
        frozenset({("test_reactor", "DebuggerRunner._run_until_breakpoint"), ("test_reactor", "TestReactor.execute_repeat"), ("test_reactor", "TestReactor.run")}),
        check_every_failed_target_names_its_type,
    ),
    ("unknown", None): Exclusion(
        RUN_EXCLUDED["unknown"],
        frozenset({("runlifecycle", "RunRegistration.__exit__"), ("runlifecycle", "_detached_terminal_result")}),
        check_every_ended_run_names_its_type,
    ),
    ("unknown_command", None): Exclusion(
        "dispatch answers it for a command the parser does not offer; entrypoint calls dispatch only after the parser accepted a subcommand, and dispatch handles every one",
        frozenset({("cli", "dispatch")}),
        check_every_command_is_dispatched,
    ),
    (FIELD_DESCRIPTION, None): Exclusion(
        "not a type: the errors reference document's description of the `error_type` field",
        frozenset({("knowledge", "errors_document")}),
        check_field_description,
    ),
}


# ---------------------------------------------------------------------------
# The reference, read the way a client reads it.

Entry = tuple[str, dict]


@pytest.fixture(scope="module")
def reference(tmp_path_factory: pytest.TempPathFactory) -> Iterator[AgenticHILToolService]:
    service = AgenticHILToolService(load_config(str(write_config(tmp_path_factory.mktemp("reference")))), frontend="mcp")
    try:
        yield service
    finally:
        closed(service)


def read_entry(service: AgenticHILToolService, key: str) -> dict | None:
    """What `resources/read` serves under one key, or None when it has no such entry."""
    uri = ERROR_URI_PREFIX + key
    response = handle_mcp_message({"jsonrpc": "2.0", "id": 1, "method": "resources/read", "params": {"uri": uri}}, service)
    assert isinstance(response, dict), response
    if "error" in response:
        assert response["error"]["code"] == MCP_RESOURCE_NOT_FOUND, response
        return None
    contents = response["result"]["contents"]
    assert [content["uri"] for content in contents] == [uri]
    return json.loads(contents[0]["text"])


def resolve(service: AgenticHILToolService, pair: Pair) -> Entry | None:
    """The entry a lookup of `pair` lands on: the scoped key first, then the bare one."""
    error_type, scope = pair
    for key in ([f"{error_type}:{scope}"] if scope is not None else []) + [error_type]:
        entry = read_entry(service, key)
        if entry is not None:
            return key, entry
    return None


def unresolved(inventory: Inventory, service: AgenticHILToolService) -> dict[Pair, frozenset[tuple[str, str, int]]]:
    return {pair: sites for pair, sites in sorted(inventory.pairs.items(), key=str) if pair not in EXCLUDED and resolve(service, pair) is None}


def described(missing: Mapping[Pair, frozenset[tuple[str, str, int]]]) -> str:
    return "\n".join(f"{error_type}:{scope} written at {sorted(sites)}" if scope else f"{error_type} written at {sorted(sites)}" for (error_type, scope), sites in missing.items())


# ---------------------------------------------------------------------------
# The guard.


def test_every_type_the_package_writes_resolves_at_its_reference_uri(reference: AgenticHILToolService) -> None:
    missing = unresolved(scanned(), reference)

    assert not missing, "no entry, scoped or bare, for:\n" + described(missing)


def test_the_reference_answers_every_pair_as_the_lookup_does(reference: AgenticHILToolService) -> None:
    for pair in scanned().pairs:
        resolved = resolve(reference, pair)
        remedy = lookup_remedy(*pair)
        assert (resolved is None) == (remedy is None), pair
        if resolved is not None:
            key, entry = resolved
            assert entry == catalogue_entry(key), pair


@pytest.mark.parametrize("pair", sorted(EXCLUDED, key=str), ids=[f"{error_type}-{scope}" for error_type, scope in sorted(EXCLUDED, key=str)])
def test_each_exclusion_is_true(pair: Pair, reference: AgenticHILToolService, tmp_path: Path) -> None:
    """Found where it is pinned, and nowhere new; its reason read off the code; and still without an entry."""
    exclusion = EXCLUDED[pair]
    assert exclusion.reason.strip()
    assert scanned().sites(pair) == exclusion.sites

    exclusion.check(tmp_path)

    assert resolve(reference, pair) is None, "the pair has an entry now; drop the exclusion"


def test_every_computed_type_is_pinned_with_what_it_carries() -> None:
    assert scanned().dynamic == set(PINNED_DYNAMIC)
    for site, reason in PINNED_DYNAMIC.items():
        assert reason.strip(), site


def test_the_scan_reads_every_module_and_the_pinned_number_of_types() -> None:
    assert scanned().modules == module_names()
    assert pin_problems(scanned()) == []


def test_a_module_left_out_of_the_scan_fails_the_pins() -> None:
    inventory = scan(modules=tuple(name for name in module_names() if name != "agentic_hil.cli"))

    problems = pin_problems(inventory)

    assert len(problems) == 2, problems
    assert f"read {SCANNED_MODULE_COUNT - 1} modules" in problems[0]
    assert "types" in problems[1]


def test_every_function_that_takes_an_error_type_is_read() -> None:
    """A helper that writes its parameter as the type is followed to its callers; anything else only looks a type up."""
    followed = {(module, line) for module, line, parameter in scanned().followed if parameter == "error_type"}
    unread = []
    for name in module_names():
        source = package_sources()[short_name(importlib.import_module(name))]
        for functions in source.functions.values():
            for function in functions:
                if "error_type" not in {argument.arg for argument in _parameters(function)} or function.name in CONSUMERS:
                    continue
                if function.name == "__init__" and source.enclosing_class(function) in producer_classes():
                    continue
                if (name, function.lineno) not in followed:
                    unread.append(f"{name}:{function.lineno} {function.name}")
    assert unread == []


def test_every_call_to_a_followed_method_is_read() -> None:
    """The reader follows a method's parameter to the calls in its own module.

    A call written in another module reaches the method only as `super()` from
    an override that forwards its own parameter; a `self.` call there resolves
    to a method of that module's own class.
    """
    for module, line, _parameter in sorted(scanned().followed):
        home = package_sources()[module.removeprefix("agentic_hil.")]
        function = function_at(home, line)
        home_class = home.enclosing_class(function)
        if home_class is None:
            continue
        owner = getattr(home.module, home_class)
        for other, source, node in package_calls().get(function.name, ()):
            if source is not home:
                enclosing = source.enclosing_function(node)
                receiver = ast.unparse(node.func.value) if isinstance(node.func, ast.Attribute) else None
                where = f"{other}: {ast.unparse(node)}"
                assert enclosing is not None and receiver in {"self", "super()"}, where
                if receiver == "super()":
                    assert enclosing.name == function.name, where
                else:
                    cls = getattr(source.module, str(source.enclosing_class(enclosing)))
                    assert next(klass for klass in cls.__mro__ if function.name in vars(klass)) is not owner, where


def test_no_class_the_reader_passed_over_is_built() -> None:
    """`self.<constant>` is read on the classes nothing subclasses; a class above them is never an instance of its own."""
    passed_over = scanned().passed_over
    assert passed_over
    for cls in passed_over:
        assert callers(cls.__name__) == frozenset(), cls
        for name in module_names():
            for value in vars(importlib.import_module(name)).values():
                if isinstance(value, (tuple, list, set, frozenset)):
                    assert not any(item is cls for item in value), f"{name} lists {cls.__name__}"


# ---------------------------------------------------------------------------
# Planted refusals: each shape reaches the inventory and fails the guard.

PLANTED: tuple[tuple[str, str, Pair, tuple[str, str]], ...] = (
    (
        "agentic_hil.cli",
        'def planted_dict_refusal():\n    return {"ok": False, "error_type": "planted_dict_refusal", "summary": "planted"}\n',
        ("planted_dict_refusal", None),
        ("cli", "planted_dict_refusal"),
    ),
    (
        "agentic_hil.report",
        'def planted_subscript_refusal(result):\n    result["error_type"] = "planted_subscript_refusal"\n    return result\n',
        ("planted_subscript_refusal", None),
        ("report", "planted_subscript_refusal"),
    ),
    (
        "agentic_hil.upgrade",
        'def planted_keyword_refusal():\n    return dict(ok=False, error_type="planted_keyword_refusal")\n',
        ("planted_keyword_refusal", None),
        ("upgrade", "planted_keyword_refusal"),
    ),
    (
        "agentic_hil.junit",
        'def planted_setdefault_refusal(result):\n    result.setdefault("error_type", "planted_setdefault_refusal")\n    return result\n',
        ("planted_setdefault_refusal", None),
        ("junit", "planted_setdefault_refusal"),
    ),
    (
        "agentic_hil.runevidence",
        'def planted_exception_refusal():\n    raise ConfigError("planted_exception_refusal", "planted")\n',
        ("planted_exception_refusal", None),
        ("runevidence", "planted_exception_refusal"),
    ),
    (
        "agentic_hil.bootstrap",
        'def planted_helper_refusal():\n    return _discovery_failure("planted_helper_refusal", "planted")\n',
        ("planted_helper_refusal", "discovery"),
        ("bootstrap", "_discovery_failure"),
    ),
    (
        "agentic_hil.runevidence",
        'def planted_forwarded_refusal(document):\n    reject_nonfinite_numbers(document, "planted_forwarded_refusal")\n',
        ("planted_forwarded_refusal", None),
        ("runevidence", "planted_forwarded_refusal"),
    ),
)


@cache
def planted_scan() -> Inventory:
    texts: dict[str, str] = {}
    for module, text, _pair, _site in PLANTED:
        source = texts.get(module) or Path(str(importlib.import_module(module).__file__)).read_text(encoding="utf-8")
        texts[module] = f"{source}\n\n{text}"
    return scan(planted=MappingProxyType(texts))


@pytest.mark.parametrize(("module", "text", "pair", "site"), PLANTED, ids=[pair[0] for _module, _text, pair, _site in PLANTED])
def test_a_planted_refusal_fails_the_guard(module: str, text: str, pair: Pair, site: tuple[str, str], reference: AgenticHILToolService) -> None:
    inventory = planted_scan()

    assert site in inventory.sites(pair)
    assert pair in set(unresolved(inventory, reference)) - set(unresolved(scanned(), reference))


# ---------------------------------------------------------------------------
# Entries read against the refusals that carry them.

# A record's lease state, each written as the entry names it: none at all, a
# null, the two states the success check names, and the state the package
# writes for a lease it could not hand back.
LEASE_STATES: dict[str, dict] = {
    r"\bmissing\b": {},
    r"\bnull\b": {"lease_state": None},
    r"`active`": {"lease_state": "active"},
    r"`released`": {"lease_state": "released"},
    r"`quarantined`": {"lease_state": "quarantined"},
}


@pytest.mark.parametrize(
    ("state", "fails"),
    [(r"\bmissing\b", False), (r"\bnull\b", False), (r"`active`", False), (r"`released`", False), (r"`quarantined`", True)],
    ids=["missing", "null", "active", "released", "quarantined"],
)
def test_a_record_fails_on_its_lease_state_only_for_a_state_outside_the_two_the_check_names(state: str, fails: bool, tmp_path: Path) -> None:
    """A record that fails nothing else is a failure for its lease state only when one is set to neither of the two."""
    config = load_config(str(write_config(tmp_path)))
    write_report(config, {"ok": True, "tool": "probe_target", "summary": "A record that fails no other check.", **LEASE_STATES[state]})

    classified = classify_failure_report(config, lambda _error_type: [])

    assert classified["error_type"] == ("unknown_debugger_error" if fails else "report_not_found"), classified


def test_the_unknown_debugger_error_entry_lets_through_every_lease_state_the_check_does() -> None:
    """Each clause that names `lease_state` among the checks names every state the check lets through."""
    check = dict(SUCCESS_CHECKS)["lease_state"]
    passing = [state for state, fields in LEASE_STATES.items() if check(fields)]
    assert passing and len(passing) < len(LEASE_STATES)
    entry = catalogue_entry("unknown_debugger_error")
    assert entry is not None
    for part, texts in (("meaning", clauses(entry["meaning"])), ("remediation", list(entry["remediation"]))):
        naming = [text for text in texts if "`lease_state`" in text]
        assert naming, f"the {part} names no `lease_state`"
        for text in naming:
            unnamed = [state for state in passing if not re.search(state, text)]
            assert not unnamed, f"the {part} names `lease_state` without the states that pass {unnamed}: {text}"


# The fields a refusal written as a dict carries for every type, and the advice
# merged into it: neither tells one refusal of a type from another.
COMMON_FIELDS = frozenset({"ok", "tool", "error_type", "summary"})


def refusal_shapes(error_type: str) -> frozenset[frozenset[str]]:
    """The fields each refusal of `error_type` carries beside its type, read at every site the scan found it written."""
    producers = producer_classes()
    shapes = set()
    sites = {site for (found, _scope), written in scanned().pairs.items() if found == error_type for site in written}
    assert sites, error_type
    for module, function, line in sorted(sites):
        source = package_sources()[module]
        literals = [node for node in ast.walk(definition(module, function)) if isinstance(node, ast.Constant) and node.value == error_type and node.lineno == line]
        assert literals, f"{module}:{line}"
        for literal in literals:
            parent = source.parents[literal]
            if isinstance(parent, ast.Call) and _callee(parent) in producers and parent.args and parent.args[0] is literal:
                details = parent.args[2] if len(parent.args) > 2 else next((keyword.value for keyword in parent.keywords if keyword.arg == "details"), None)
                assert details is None or (isinstance(details, ast.Dict) and all(isinstance(key, ast.Constant) for key in details.keys)), f"{module}:{line}: {ast.unparse(parent)}"
                shapes.add(frozenset(key.value for key in details.keys) if details is not None else frozenset())  # type: ignore[union-attr]
            elif isinstance(parent, ast.Dict):
                keys = [key for key in parent.keys if key is not None]
                spread = [value for key, value in zip(parent.keys, parent.values, strict=True) if key is None]
                assert all(isinstance(key, ast.Constant) for key in keys) and all(isinstance(value, ast.Call) and _callee(value) == "remediation_fields" for value in spread), f"{module}:{line}: {ast.unparse(parent)}"
                shapes.add(frozenset(key.value for key in keys) - COMMON_FIELDS)  # type: ignore[attr-defined]
            else:
                raise AssertionError(f"{module}:{line}: the fields of {ast.unparse(parent)} are not read")
    return frozenset(shapes)


def test_the_mcp_command_untrusted_entry_names_the_fields_of_each_refusal_together() -> None:
    """Every refusal of the type, the one the candidate walk ends in and each one of a single path, read where it is raised.

    `path` stands in nearly every one of them, so a clause may name it once for
    all. Every other field a refusal carries is named in one clause with the
    rest of that refusal's fields, and no clause names together fields that no
    one refusal carries, such as a file's `gid` beside a parent's `directory`.
    """
    shapes = refusal_shapes("mcp_command_untrusted")
    fields = frozenset().union(*shapes)
    assert {"rejected_candidates", "directory", "gid"} <= fields
    entry = catalogue_entry("mcp_command_untrusted")
    assert entry is not None
    meaning = clauses(entry["meaning"])
    named = [frozenset(re.findall(r"`(\w+)`", clause)) & fields for clause in meaning]

    assert fields <= frozenset().union(*named), sorted(fields - frozenset().union(*named))
    for shape in shapes:
        assert any(shape - {"path"} <= clause for clause in named), f"no clause names {sorted(shape - {'path'})} together"
    for clause, carried in zip(meaning, named, strict=True):
        assert any(carried <= shape for shape in shapes), f"names together {sorted(carried)}, which no one refusal carries: {clause}"
    assert fact_problems("mcp_command_untrusted", Facts(says=(r"`directory`[^.;]*\bparent directory\b|\bparent directory\b[^.;]*`directory`",))) == []
