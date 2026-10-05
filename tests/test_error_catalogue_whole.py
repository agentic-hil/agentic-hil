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
subscript assignment and `setdefault`, a (key, value) pair handed to `dict` or
`update`, the first argument of an exception whose constructor takes the type,
and the argument of every helper that writes its own parameter into the field,
followed to each caller in the package under whatever name the caller imports
it by. Each type is held together with the scope its advice is looked up under
at that site, read the way the debug guard reads it; a site whose scope cannot
be read is held to the bare key, which every lookup falls back to. A type field
named in any other form, and a followed helper named other than as the function
a call calls, is a form the scan does not read, and it fails the scan.

Every pair resolves through the real MCP resource read, the scoped key first
and the bare key after it, exactly as `knowledge.lookup_remedy` does, or sits in
`EXCLUDED` with a reason a test reads off the code. The number of modules, of
types and of producers (a type with a function that writes it) is pinned, so
the scan cannot shrink unnoticed, and planted refusals prove each shape reaches
the inventory and fails the guard.
"""

from __future__ import annotations

import ast
import importlib
import importlib.util
import inspect
import json
import re
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from types import MappingProxyType, ModuleType

import pytest
from conftest import write_config
from test_error_catalogue_can import NOT_RETURNED_BY_A_TOOL
from test_error_catalogue_ec1_debug import (
    ERROR_TYPE_FIELDS,
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
from test_error_catalogue_ec2_artifacts_reports import clauses
from test_error_catalogue_ec3_run_coordination import CONSUMERS, NEGATION
from test_error_catalogue_ec3_run_coordination import EXCLUDED as RUN_EXCLUDED
from test_error_catalogue_ec3_run_coordination import PINNED_DYNAMIC as RUN_DYNAMIC_SITES

import agentic_hil
from agentic_hil import humanize
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
    # Where a type is written in a form the scan does not read.
    unsupported: frozenset[DynamicSite]

    @property
    def types(self) -> frozenset[str]:
        return frozenset(error_type for error_type, _scope in self.pairs)

    @property
    def producers(self) -> frozenset[tuple[str, str, str]]:
        """Each type with a function that writes it, as (type, module, function)."""
        return frozenset((error_type, module, function) for (error_type, _scope), sites in self.pairs.items() for module, function, _line in sites)

    def sites(self, pair: Pair) -> frozenset[tuple[str, str]]:
        return frozenset((module, function) for module, function, _line in self.pairs.get(pair, ()))


def _is_consumer_keyword(source: Source, node: ast.expr) -> bool:
    parent = source.parents.get(node)
    if not isinstance(parent, ast.keyword):
        return False
    call = source.parents.get(parent)
    return isinstance(call, ast.Call) and _callee(call) in CONSUMERS


def imported(source: Source) -> Mapping[str, object]:
    """What each name `source` imports from the package stands for, under the name it is bound to.

    An import inside a function counts for the whole module: a name that means
    a helper anywhere in it is read as that helper everywhere in it.
    """
    bound: dict[str, object] = {}
    for node in ast.walk(source.tree):
        if isinstance(node, ast.ImportFrom):
            base = importlib.util.resolve_name("." * node.level + (node.module or ""), source.module.__package__)
            if base.split(".")[0] != "agentic_hil":
                continue
            module = importlib.import_module(base)
            for alias in node.names:
                if alias.name == "*":
                    bound.update((name, getattr(module, name)) for name in getattr(module, "__all__", [name for name in vars(module) if not name.startswith("_")]))
                else:
                    bound[alias.asname or alias.name] = getattr(module, alias.name) if hasattr(module, alias.name) else importlib.import_module(f"{base}.{alias.name}")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] == "agentic_hil":
                    bound[alias.asname or "agentic_hil"] = importlib.import_module(alias.name if alias.asname else "agentic_hil")
    return MappingProxyType(bound)


def resolved(node: ast.expr, source: Source, bound: Mapping[str, object]) -> object | None:
    """The object of the package a name, or an attribute of a module, stands for in `source`, under whatever name it was imported."""
    if isinstance(node, ast.Name):
        return bound[node.id] if node.id in bound else vars(source.module).get(node.id)
    if isinstance(node, ast.Attribute):
        owner = resolved(node.value, source, bound)
        return getattr(owner, node.attr, None) if isinstance(owner, ModuleType) else None
    return None


# The calls that build a document from (key, value) pairs: a pair keyed by a
# type field in the list or tuple one of them is handed writes the type.
PAIR_TAKERS = frozenset({"dict", "update"})


def _sites(source: Source, bound: Mapping[str, object]) -> Iterator[ast.expr]:
    """Every expression `source` writes as an error type."""
    producers = producer_classes()
    for node in error_type_expressions(source):
        if not _is_consumer_keyword(source, node):
            yield node
    for node in ast.walk(source.tree):
        if not isinstance(node, ast.Call):
            continue
        constructed = resolved(node.func, source, bound)
        callee = constructed.__name__ if isinstance(constructed, type) and constructed.__name__ in producers else _callee(node)
        if callee in producers and len(node.args) > producers[callee]:
            yield node.args[producers[callee]]
        elif callee == "__setitem__" and len(node.args) == 2 and isinstance(node.args[0], ast.Constant) and node.args[0].value == "error_type":
            yield node.args[1]
        elif callee in PAIR_TAKERS and node.args and isinstance(node.args[0], (ast.List, ast.Tuple)):
            for pair in node.args[0].elts:
                if isinstance(pair, ast.Tuple) and len(pair.elts) == 2 and isinstance(pair.elts[0], ast.Constant) and pair.elts[0].value in ERROR_TYPE_FIELDS:
                    yield pair.elts[1]


# Where a (field, value) row is printed as a line of text rather than written
# into a document: a test below holds that it returns lines.
RENDERERS = (humanize._fields,)
# The calls that read a field of a document, or an attribute of an error, by its name.
FIELD_READERS = frozenset({"get", "getattr"})


def _written_value(source: Source, name: ast.Constant) -> ast.AST | None:
    """What a literal type field writes the type from, where it stands to write one.

    That is the value beside it in a dict, an assignment or a `setdefault`, or
    in a (field, value) pair; the write itself where there is no single value.
    """
    parent = source.parents.get(name)
    if isinstance(parent, ast.Dict) and any(key is name for key in parent.keys):
        return next(value for key, value in zip(parent.keys, parent.values, strict=True) if key is name)
    if isinstance(parent, ast.Subscript) and parent.slice is name and isinstance(parent.ctx, ast.Store):
        statement = source.parents.get(parent)
        return statement.value if isinstance(statement, (ast.Assign, ast.AnnAssign)) and statement.value is not None else parent
    if isinstance(parent, ast.Call) and parent.args and parent.args[0] is name and _callee(parent) in {"setdefault", "__setitem__"}:
        return parent.args[1] if len(parent.args) == 2 else parent
    if isinstance(parent, ast.Tuple) and len(parent.elts) == 2 and parent.elts[0] is name:
        return parent.elts[1]
    return None


def _is_rendered(source: Source, bound: Mapping[str, object], pair: ast.AST) -> bool:
    rows = source.parents.get(pair)
    call = source.parents.get(rows) if rows is not None else None
    return (
        isinstance(rows, (ast.List, ast.Tuple))
        and isinstance(call, ast.Call)
        and bool(call.args)
        and call.args[0] is rows
        and any(resolved(call.func, source, bound) is renderer for renderer in RENDERERS)
    )


def _is_read(parent: ast.AST | None, name: ast.Constant) -> bool:
    """Whether a literal type field only reads: a subscript load, a `.get` or `getattr`, a comparison, or a set of names to match."""
    if isinstance(parent, ast.Subscript):
        return parent.slice is name and not isinstance(parent.ctx, ast.Store)
    if isinstance(parent, ast.Call):
        return _callee(parent) in FIELD_READERS and any(argument is name for argument in parent.args)
    return isinstance(parent, (ast.Compare, ast.Set))


def fields_unread(source: Source, bound: Mapping[str, object], read: Iterable[ast.expr]) -> Iterator[DynamicSite]:
    """Every place `source` names a type field the scan neither reads the value of nor knows to only read or print it."""
    values = {id(node) for node in read}
    for node in ast.walk(source.tree):
        if not (isinstance(node, ast.Constant) and node.value in ERROR_TYPE_FIELDS):
            continue
        parent = source.parents.get(node)
        value = _written_value(source, node)
        if (value is not None and (id(value) in values or _is_rendered(source, bound, parent))) or (value is None and _is_read(parent, node)):
            continue
        yield short_name(source.module), written_in(source, node), ast.unparse(parent if parent is not None else node)


def helper_references_unread(sources: Mapping[str, Source], bound: Mapping[str, Mapping[str, object]], helpers: list[object]) -> Iterator[DynamicSite]:
    """Every place a helper the scan follows to its callers is named other than as the function a call calls.

    The scan follows a helper through its calls; a helper stored, passed on or
    renamed is called where no call names it.
    """
    for module_name, source in sources.items():
        for node in ast.walk(source.tree):
            if isinstance(node, (ast.Name, ast.Attribute)) and isinstance(node.ctx, ast.Load):
                parent = source.parents.get(node)
                if not (isinstance(parent, ast.Call) and parent.func is node) and any(resolved(node, source, bound[module_name]) is helper for helper in helpers):
                    yield short_name(source.module), written_in(source, node), ast.unparse(parent if parent is not None else node)


def function_at(source: Source, line: int) -> ast.FunctionDef:
    (function,) = [function for functions in source.functions.values() for function in functions if function.lineno == line]
    return function


def module_helper(sources: Mapping[str, Source], home: str, line: int) -> object | None:
    """The module function whose `def` stands at `line` of `home`, or None for a method or a nested function."""
    function = function_at(sources[home], line)
    helper = vars(importlib.import_module(home)).get(function.name)
    return helper if inspect.isfunction(helper) and helper.__qualname__ == function.name and sources[home].enclosing_class(function) is None else None


def calls_from_other_modules(sources: Mapping[str, Source], bound: Mapping[str, Mapping[str, object]], home: str, helper: object) -> Iterator[tuple[Source, ast.Call]]:
    """Calls to the module function `helper` of `home` from every other module, under any name it is imported by."""
    for module_name, source in sources.items():
        if module_name == home:
            continue
        for node in ast.walk(source.tree):
            if isinstance(node, ast.Call) and resolved(node.func, source, bound[module_name]) is helper:
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

    bound = {name: imported(source) for name, source in sources.items()}
    unsupported: set[DynamicSite] = set()
    for name, source in sources.items():
        written = list(_sites(source, bound[name]))
        unsupported.update(fields_unread(source, bound[name], written))
        for node in written:
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
                helper = module_helper(sources, home, line)
                if helper is None:
                    continue
                for caller, call in calls_from_other_modules(sources, bound, home, helper):
                    argument = _argument(call, function_at(sources[home], line), parameter)
                    if argument is not None:
                        forwarded = read(caller, argument)
                        if forwarded is not None:
                            add(caller, argument, forwarded, scopes)
            followed |= reader.followed
    dynamic |= reader.unread
    helpers = [helper for home, line, _parameter in followed if (helper := module_helper(sources, home, line)) is not None]
    unsupported.update(helper_references_unread(sources, bound, helpers))
    return Inventory(
        tuple(names),
        MappingProxyType({pair: frozenset(sites) for pair, sites in pairs.items()}),
        frozenset(dynamic),
        frozenset(followed),
        frozenset(reader.passed_over),
        frozenset(unsupported),
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

SCANNED_MODULE_COUNT = 48
# `can_broker_disconnected`, a broker connection that ended (#664), is the 219th.
COLLECTED_TYPE_COUNT = 219
# A producer is a type together with a function that writes it. A type many
# functions write, such as `invalid_argument`, keeps its place among the types
# when one of those functions drops out of the scan, so the type count alone
# misses that; this one moves. A new function that writes a type, or a
# function that writes one it did not write before, adds one: raise the number
# in the same change. The newest is `report.audit_error_detail`, which names a
# filesystem fault an audit write met `report_write_failed` (#675); that is the
# one new type. The integrated debugger fix also adds the `debugger_error`
# producer in `GdbDebugSessions.continue_execution`; the joint host scan finds
# two additional producers, with every previous producer still present.
# `CanBusService.session_stop` writes `can_participant_not_configured` (#632).
# The broker's two `permission_denied` refusals are written by one function,
# `CanBroker._permission_refusal`, which names the key (#657).
# `canbroker.broker_request_failure` writes `can_broker_disconnected` (#664).
PRODUCER_COUNT = 645


def pin_problems(inventory: Inventory) -> list[str]:
    problems = []
    if len(inventory.modules) != SCANNED_MODULE_COUNT:
        problems.append(f"the scan read {len(inventory.modules)} modules, and the package has {SCANNED_MODULE_COUNT}")
    if len(inventory.types) != COLLECTED_TYPE_COUNT:
        problems.append(f"the scan collected {len(inventory.types)} types, and {COLLECTED_TYPE_COUNT} are pinned")
    if len(inventory.producers) != PRODUCER_COUNT:
        problems.append(f"the scan found {len(inventory.producers)} producers (a type and a function that writes it), and {PRODUCER_COUNT} are pinned")
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


# The last statement of `run_plan`'s `with registration:` block, which is what
# decides the terminal record a run leaves. Since #666 and #667 it is the
# wrapper around the run rather than a bare `finish`, and it is pinned whole
# because each part of it carries the exclusion below: the normal path finishes
# from the run's own result, which names its type; an exception finishes from
# the report the run recorded on it wherever there is one, so an interrupted or
# a crashed run is recorded under the type that report names instead of falling
# to the `__exit__` fallback; and only an `Exception` carrying such a report is
# answered as a result, so `KeyboardInterrupt` and `SystemExit` still leave by
# `raise`, with their record already written.
RUN_PLAN_REGISTERED_BLOCK = '''try:
    result = run_registered_plan(config, test_config, wait_s=wait_s, registration=registration)
except BaseException as error:
    written = getattr(error, "agentic_hil_report", None)
    if isinstance(written, dict):
        registration.finish(written)
    if return_failed_report and isinstance(error, Exception) and isinstance(written, dict):
        result = written
    else:
        raise
else:
    registration.finish(result)
'''


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
    ended = registered.body[-1]
    assert isinstance(ended, ast.Try), ast.unparse(ended)
    # The run that returned still has its record finished from its own result.
    assert [ast.unparse(statement) for statement in ended.orelse] == ["registration.finish(result)"]
    # And the wrapper around it is the one above, to the statement.
    assert ast.dump(ended) == ast.dump(ast.parse(RUN_PLAN_REGISTERED_BLOCK).body[0]), ast.unparse(ended)
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
        frozenset(
            {
                ("backends.gdbdebug", "GdbDebugSessions._read_memory_bytes"),
                ("backends.stlink", "STLinkBackend._failure_result"),
                ("backends.stlink", "STLinkBackend.debug_symbol_value"),
            }
        ),
        check_silent(("memory_read_failed", "stlink")),
    ),
    ("verify_failed", "openocd"): Exclusion(
        SILENT_REASON,
        frozenset({("backends.gdbdebug", "GdbDebugSessions._start_failure"), ("backends.openocd", "OpenOCDBackend._failure_result"), ("backends.openocd", "OpenOCDBackend.info")}),
        check_silent(("verify_failed", "openocd")),
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

    assert len(problems) == 3, problems
    assert f"read {SCANNED_MODULE_COUNT - 1} modules" in problems[0]
    assert "types" in problems[1]
    assert "producers" in problems[2]


def test_a_producer_the_scan_stops_reading_fails_the_pins() -> None:
    """One of the functions that write `unsupported_agent` stops writing it: every module and every type is still found."""
    source = Path(str(importlib.import_module("agentic_hil.cli").__file__)).read_text(encoding="utf-8")
    written = '"error_type": "unsupported_agent", '
    refusal = f'{written}"summary": summary, "agent": normalize_agent(agent)'
    assert source.count(refusal) == 1
    dropped = ("unsupported_agent", "cli", "_unsupported_agent")
    assert dropped in scanned().producers
    assert len({producer for producer in scanned().producers if producer[0] == dropped[0]}) > 1

    inventory = scan(planted=MappingProxyType({"agentic_hil.cli": source.replace(refusal, refusal.removeprefix(written))}))
    problems = pin_problems(inventory)

    assert dropped not in inventory.producers
    assert len(problems) == 1, problems
    assert f"found {PRODUCER_COUNT - 1} producers" in problems[0]


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
        "from agentic_hil.config import reject_nonfinite_numbers\n\n\n"
        'def planted_forwarded_refusal(document):\n    reject_nonfinite_numbers(document, "planted_forwarded_refusal")\n',
        ("planted_forwarded_refusal", None),
        ("runevidence", "planted_forwarded_refusal"),
    ),
    (
        "agentic_hil.junit",
        "from agentic_hil.config import reject_nonfinite_numbers as planted_alias\n\n\n"
        'def planted_aliased_refusal(document):\n    planted_alias(document, "planted_aliased_refusal")\n',
        ("planted_aliased_refusal", None),
        ("junit", "planted_aliased_refusal"),
    ),
    (
        "agentic_hil.upgrade",
        "from agentic_hil.config import ConfigError as PlantedRefusal\n\n\n"
        'def planted_aliased_exception_refusal():\n    raise PlantedRefusal("planted_aliased_exception_refusal", "planted")\n',
        ("planted_aliased_exception_refusal", None),
        ("upgrade", "planted_aliased_exception_refusal"),
    ),
    (
        "agentic_hil.report",
        'def planted_pairs_refusal():\n    return dict([("ok", False), ("error_type", "planted_pairs_refusal")])\n',
        ("planted_pairs_refusal", None),
        ("report", "planted_pairs_refusal"),
    ),
    (
        "agentic_hil.cli",
        'def planted_update_refusal(result):\n    result.update((("error_type", "planted_update_refusal"),))\n    return result\n',
        ("planted_update_refusal", None),
        ("cli", "planted_update_refusal"),
    ),
)

# Refusals written in a form the scan does not read, each with where it is
# recorded and the expression it is recorded under. A type written so reaches
# no guard, so the form fails the scan instead of passing it unread.
PLANTED_UNREAD: tuple[tuple[str, str, DynamicSite], ...] = (
    (
        "agentic_hil.test_reactor",
        'def planted_renamed_helper(document):\n    check = reject_nonfinite_numbers\n    check(document, "planted_renamed_helper")\n',
        ("test_reactor", "planted_renamed_helper", "check = reject_nonfinite_numbers"),
    ),
    (
        "agentic_hil.junit",
        'def planted_zipped_refusal():\n    return dict(zip(("ok", "error_type"), (False, "planted_zipped_refusal")))\n',
        ("junit", "planted_zipped_refusal", "('ok', 'error_type')"),
    ),
    (
        "agentic_hil.upgrade",
        'def planted_listed_refusal(result):\n    pairs = [("error_type", "planted_listed_refusal")]\n    result.update(pairs)\n    return result\n',
        ("upgrade", "planted_listed_refusal", "('error_type', 'planted_listed_refusal')"),
    ),
)


@cache
def planted_scan() -> Inventory:
    texts: dict[str, str] = {}
    for module, text in [(module, text) for module, text, _pair, _site in PLANTED] + [(module, text) for module, text, _site in PLANTED_UNREAD]:
        source = texts.get(module) or Path(str(importlib.import_module(module).__file__)).read_text(encoding="utf-8")
        texts[module] = f"{source}\n\n{text}"
    return scan(planted=MappingProxyType(texts))


@pytest.mark.parametrize(("module", "text", "pair", "site"), PLANTED, ids=[pair[0] for _module, _text, pair, _site in PLANTED])
def test_a_planted_refusal_fails_the_guard(module: str, text: str, pair: Pair, site: tuple[str, str], reference: AgenticHILToolService) -> None:
    inventory = planted_scan()

    assert site in inventory.sites(pair)
    assert pair in set(unresolved(inventory, reference)) - set(unresolved(scanned(), reference))


@pytest.mark.parametrize(("module", "text", "site"), PLANTED_UNREAD, ids=[site[1] for _module, _text, site in PLANTED_UNREAD])
def test_a_planted_refusal_the_scan_cannot_read_fails_it(module: str, text: str, site: DynamicSite) -> None:
    assert site in planted_scan().unsupported


def test_the_package_writes_every_type_in_a_form_the_scan_reads() -> None:
    unsupported = sorted(scanned().unsupported)

    assert unsupported == [], "write the type as a dict value, a keyword, a subscript or `setdefault`, and call a helper that writes it by a name it is imported under"


def test_a_type_handed_to_the_renderer_becomes_a_line_of_text() -> None:
    """A (field, value) row `humanize._fields` takes is printed, not written into a document, so the scan passes it."""
    lines = humanize._fields([("error_type", "planted_rendered_refusal")])

    assert lines and all(isinstance(line, str) for line in lines) and "planted_rendered_refusal" in "".join(lines), lines


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


# What a piece of an entry says the check does with a lease state it names:
# lets it through (it passes, it is no reason) or holds it against the record
# (it fails, it is a reason). A piece that says neither is a condition the
# record fails on, and names a state the check lets through only to exclude it.
PASSES = re.compile(r"\bpass(?:es|ed)?\b", re.IGNORECASE)
HOLDS = re.compile(r"\bfail(?:s|ed)?\b|\breasons?\b", re.IGNORECASE)
VERDICT = re.compile(f"{PASSES.pattern}|{HOLDS.pattern}", re.IGNORECASE)


def negated_before(text: str) -> bool:
    """Whether `text` ends in a negation of what follows it: `does not`, `doesn't`, `is no`, `is not a`, `neither`.

    Only the word right before, or the one before that, so a negation in a
    condition earlier in the sentence, or a qualifier after the word, such as
    `without exception`, is not read as turning it.
    """
    return re.search(rf"(?:{NEGATION.pattern})\s+(?:\w+\s+)?$", text, re.IGNORECASE) is not None


# A subject said away up to the state named next: `neither a`, `nor a`, `no`,
# `none of the`, and across the states it joins (`no missing or null one`).
NO_SUBJECT = re.compile(rf"\b(?:neither|nor|no|none of)\s+(?:(?:a|an|the|or|and|{'|'.join(LEASE_STATES)})\s+)*$", re.IGNORECASE)


def outside_parentheses(text: str) -> tuple[list[str], list[list[str]]]:
    """`text` cut at each comma outside parentheses, and each piece's asides: (pieces, asides by piece)."""
    pieces, depth, start = [], 0, 0
    for index, character in enumerate(text):
        depth += {"(": 1, ")": -1}.get(character, 0)
        if character == "," and depth == 0:
            pieces.append(text[start:index])
            start = index + 1
    pieces.append(text[start:])
    return pieces, [re.findall(r"\(([^()]*)\)", piece) for piece in pieces]


def lets_through(piece: str, state: str) -> list[bool]:
    """What `piece` says the check does with each mention of `state`: True where it lets the record through.

    A mention belongs to the first verdict after it, its subject coming
    before its verb, or else to the last one before it (`and so does a null
    one`). A verdict is turned only by a negation right before its own word, so
    `a missing one does not fail this check but a null one fails` lets the
    missing one through and holds the null one. A negated subject turns it
    again for each state in that subject: `neither a missing nor a null one
    passes` holds both. With no verdict at all, the piece is a condition the
    record fails on, and a mention lets the state through only where it is
    excluded (`neither `active` nor `released``).
    """
    verdicts = [(found.start(), PASSES.fullmatch(found.group()) is not None, negated_before(piece[: found.start()])) for found in VERDICT.finditer(piece)]
    said = []
    for mention in re.finditer(state, piece):
        if not verdicts:
            said.append(negated_before(piece[: mention.start()]))
            continue
        _start, passes, negated = next((verdict for verdict in verdicts if verdict[0] > mention.start()), verdicts[-1])
        said.append((passes != negated) != (NO_SUBJECT.search(piece[: mention.start()]) is not None))
    return said


def lease_problems(entry: Mapping) -> list[str]:
    """Each statement that names `lease_state` among the checks lets through every state the check does.

    The statement is the comma-separated piece of a clause that names it: in
    the meaning, among the checks the record failed; in the remediation, among
    the conditions that send the caller to `hardware_lease_status`. Its asides
    in parentheses are read on their own, so `(a missing or null one passes
    this check)` lets those through and `(a missing or null one is also a
    reason)` holds them against the record.
    """
    check = dict(SUCCESS_CHECKS)["lease_state"]
    passing = [state for state, fields in LEASE_STATES.items() if check(fields)]
    assert passing and len(passing) < len(LEASE_STATES)
    problems = []
    for part, texts in (("meaning", [entry["meaning"]]), ("remediation", list(entry["remediation"]))):
        naming = []
        for clause in (clause for text in texts for clause in clauses(text)):
            pieces, asides = outside_parentheses(clause)
            naming.extend((piece, own) for piece, own in zip(pieces, asides, strict=True) if "`lease_state`" in piece)
        if not naming:
            problems.append(f"the {part} names no `lease_state`")
        for piece, own in naming:
            statements = [re.sub(r"\([^()]*\)", " ", piece), *own]
            for state in passing:
                said = [verdict for statement in statements for verdict in lets_through(statement, state)]
                if not said:
                    problems.append(f"the {part} names `lease_state` without {state}, which passes: {piece}")
                elif not all(said):
                    problems.append(f"the {part} holds {state} against the record, which the check lets through: {piece}")
    return problems


def test_the_unknown_debugger_error_entry_lets_through_every_lease_state_the_check_does() -> None:
    entry = catalogue_entry("unknown_debugger_error")
    assert entry is not None

    assert lease_problems(entry) == []


# The entry with one statement about a lease state turned around: (part, as the
# entry says it, turned around).
LEASE_TURNED: dict[str, tuple[str, str, str]] = {
    "meaning_fails_missing": ("meaning", "(a missing or null one passes this check)", "(a missing or null one fails this check)"),
    "meaning_does_not_pass_missing": ("meaning", "(a missing or null one passes this check)", "(a missing or null one does not pass this check)"),
    "meaning_fails_active": ("meaning", "is neither `active` nor `released`", "is `active` or `released`"),
    "meaning_leaves_out_missing": ("meaning", " (a missing or null one passes this check)", ""),
    "remediation_reason_missing": ("remediation", "(a missing or null one is no reason)", "(a missing or null one is also a reason)"),
    "remediation_reason_active": ("remediation", "is neither `active` nor `released`", "is `active` or `released`"),
    "remediation_leaves_out_missing": ("remediation", " (a missing or null one is no reason)", ""),
    "meaning_fails_null_only": ("meaning", "(a missing or null one passes this check)", "(a missing one does not fail this check but a null one fails)"),
    "meaning_fails_missing_only": ("meaning", "(a missing or null one passes this check)", "(a null one does not fail this check but a missing one fails)"),
    "remediation_reason_null_only": ("remediation", "(a missing or null one is no reason)", "(a missing one is no reason, but a null one is a reason)"),
    "remediation_reason_missing_only": ("remediation", "(a missing or null one is no reason)", "(a null one is no reason, but a missing one is a reason)"),
    "meaning_neither_passes": ("meaning", "(a missing or null one passes this check)", "(neither a missing nor a null one passes this check)"),
}

# The entry with one statement about a lease state said another true way: (part,
# as the entry says it, said again).
LEASE_RESTATED: dict[str, tuple[str, str, str]] = {
    "meaning_without_exception": ("meaning", "(a missing or null one passes this check)", "(a missing or null one passes this check without exception)"),
    "meaning_each_does_not_fail": ("meaning", "(a missing or null one passes this check)", "(a missing one does not fail this check, and a null one does not fail it either)"),
    "meaning_so_does_null": ("meaning", "(a missing or null one passes this check)", "(a missing one passes this check, and so does a null one)"),
    "remediation_without_exception": ("remediation", "(a missing or null one is no reason)", "(a missing or null one is no reason, without exception)"),
    "remediation_neither_is_null": ("remediation", "(a missing or null one is no reason)", "(a missing one is no reason, and neither is a null one)"),
    "meaning_neither_fails": ("meaning", "(a missing or null one passes this check)", "(neither a missing nor a null one fails this check)"),
}


def unknown_debugger_error_with(part: str, said: str, instead: str) -> dict:
    """The entry with the one place `part` says `said` saying `instead`."""
    entry = dict(catalogue_entry("unknown_debugger_error") or {})
    texts = [entry["meaning"]] if part == "meaning" else list(entry["remediation"])
    assert sum(text.count(said) for text in texts) == 1, said
    texts = [text.replace(said, instead) for text in texts]
    entry[part] = texts[0] if part == "meaning" else texts
    return entry


@pytest.mark.parametrize("turned", sorted(LEASE_TURNED))
def test_an_entry_that_holds_a_lease_state_against_a_record_the_check_lets_through_fails(turned: str) -> None:
    assert lease_problems(unknown_debugger_error_with(*LEASE_TURNED[turned])) != []


@pytest.mark.parametrize("restated", sorted(LEASE_RESTATED))
def test_an_entry_that_lets_each_passing_lease_state_through_another_way_passes(restated: str) -> None:
    assert lease_problems(unknown_debugger_error_with(*LEASE_RESTATED[restated])) == []


# The fields a refusal written as a dict carries for every type, and the advice
# merged into it: neither tells one refusal of a type from another.
COMMON_FIELDS = frozenset({"ok", "tool", "error_type", "summary"})


def sentence(node: ast.expr | None) -> str:
    """The sentence a refusal is raised with, as written: a template keeps its placeholders."""
    if node is None:
        return ""
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else ast.unparse(node)


def refusals(error_type: str) -> list[tuple[str, str, frozenset[str]]]:
    """Each refusal of `error_type`, read at every site the scan found it written: (where, its sentence, the fields it carries beside its type)."""
    producers = producer_classes()
    found = []
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
                summary = parent.args[1] if len(parent.args) > 1 else next((keyword.value for keyword in parent.keywords if keyword.arg == "summary"), None)
                found.append((f"{module}:{line}", sentence(summary), frozenset(key.value for key in details.keys) if details is not None else frozenset()))  # type: ignore[union-attr]
            elif isinstance(parent, ast.Dict):
                keys = [key for key in parent.keys if key is not None]
                spread = [value for key, value in zip(parent.keys, parent.values, strict=True) if key is None]
                assert all(isinstance(key, ast.Constant) for key in keys) and all(isinstance(value, ast.Call) and _callee(value) == "remediation_fields" for value in spread), f"{module}:{line}: {ast.unparse(parent)}"
                summary = next((value for key, value in zip(parent.keys, parent.values, strict=True) if isinstance(key, ast.Constant) and key.value == "summary"), None)
                found.append((f"{module}:{line}", sentence(summary), frozenset(key.value for key in keys) - COMMON_FIELDS))  # type: ignore[attr-defined]
            else:
                raise AssertionError(f"{module}:{line}: the fields of {ast.unparse(parent)} are not read")
    return found


def refusal_shapes(error_type: str) -> frozenset[frozenset[str]]:
    """The fields each refusal of `error_type` carries beside its type."""
    return frozenset(fields for _where, _sentence, fields in refusals(error_type))


PATH_ONLY = frozenset({"path"})
# Each refusal of `mcp_command_untrusted`, told apart by a part of the
# sentence the package raises it with: (that part, what sets it off as the
# entry says it, the fields it carries beside its type). The candidate walk in
# `cli.mcp_server_command` raises the first; `trusted_persistent_executable`
# in `config` raises the rest.
MCP_REFUSALS: tuple[tuple[str, str, frozenset[str]], ...] = (
    ("No stable trusted Agentic HIL executable was found", r"\btried\b", frozenset({"rejected_candidates"})),
    ("must resolve to an absolute path", r"\babsolute\b", PATH_ONLY),
    ("must not come from the workspace or a temporary/cache directory", r"\b(?:project|workspace|temporary|cache)\b", PATH_ONLY),
    ("has a parent directory that is", r"\bparent directory\b[^.;]*\b(?:belongs? to|owned by)\b", frozenset({"path", "directory", "mode", "uid"})),
    ("must be trusted, executable, and writable by nobody but its owner", r"^(?=.*\bowner\b)(?=.*\bwrit(?:e|able)\b)(?=.*\bexecute bit\b)", frozenset({"path", "mode", "uid", "gid", "untrusted_because"})),
    # The symlink's own owner or link count: not that of its parent directory
    # or its target, which `trusted_parent_chain` and the file checks refuse.
    ("symlink must have a trusted owner and one link", r"\bsymlink\b(?:(?!\b(?:parent|directory|target)\b)[^.;])*\b(?:owner|owned by|belongs? to|links?)\b", PATH_ONLY),
    ("changed during validation", r"\bchange[sd]?\b", PATH_ONLY),
    ("symlink target does not exist", r"\btarget\b[^.;]*\b(?:does not exist|missing)\b", PATH_ONLY),
    ("must not contain another symlink", r"\banother symlink\b", frozenset({"path", "target"})),
    ("must be a regular executable", r"\bregular file\b", PATH_ONLY),
)


def test_each_mcp_command_untrusted_refusal_carries_the_fields_its_trigger_is_held_to() -> None:
    """Every site the scan finds is told apart by one sentence, and carries the fields set beside it."""
    told = set()
    for where, said, carried in refusals("mcp_command_untrusted"):
        rows = [row for row in MCP_REFUSALS if row[0] in said]
        assert len(rows) == 1, f"{where}: {said}"
        assert carried == rows[0][2], f"{where} carries {sorted(carried)}, held to {sorted(rows[0][2])}: {said}"
        told.add(rows[0][0])

    assert told == {row[0] for row in MCP_REFUSALS}


# Words that say a refusal has a field, and words that say it leaves one out.
HAS = re.compile(r"\b(?:carr(?:y|ies)|adds?|includes?|names?|naming|with)\b", re.IGNORECASE)
LEAVES_OUT = re.compile(r"\b(?:omits?|lacks?|drops?|leaves? out|without)\b", re.IGNORECASE)
PRESENCE = re.compile(f"{HAS.pattern}|{LEAVES_OUT.pattern}", re.IGNORECASE)
# The same said of a field after it, its subject: `is included`, `is never
# carried`, `isn't added`, `are not set`.
HAS_AFTER = re.compile(r"(?:\s*(?:,|\band\b|\bor\b)\s*`\w+`)*\s+(?:is|are)(?:n't)?\s+(?:(?:not|never)\s+)?(?P<word>included|carried|added|named|given|set)\b", re.IGNORECASE)


def presence(clause: str, fields: frozenset[str]) -> tuple[frozenset[str], frozenset[str]]:
    """The fields among `fields` that `clause` says the refusal has, and those it says it leaves out: (has, leaves out).

    A field followed by a passive predicate (`is included`, `is not carried`)
    belongs to that. Any other belongs to the nearest word before it that says
    one or the other, or with none before, to the first after it, as a subject
    does to its verb.
    That word is turned by a negation right before it (`never adds`, `does not
    omit`), and the field by one between that word and the field (`adds no`). A
    negation elsewhere in the clause, as in what sets the refusal off (`is not
    owned by`), turns neither.
    """
    words = [(found.start(), found.end(), HAS.fullmatch(found.group()) is not None, negated_before(clause[: found.start()])) for found in PRESENCE.finditer(clause)]
    has, leaves_out = set(), set()
    for mention in re.finditer(r"`(\w+)`", clause):
        if mention.group(1) not in fields:
            continue
        passive = HAS_AFTER.match(clause, mention.end())
        if passive:
            (leaves_out if negated_before(clause[: passive.start("word")]) else has).add(mention.group(1))
            continue
        before = [word for word in words if word[1] <= mention.start()]
        _start, end, says_has, negated = before[-1] if before else next(iter(words), (0, 0, True, False))
        between = clause[end if before else 0 : mention.start()]
        (has if (says_has != negated) != negated_before(between) else leaves_out).add(mention.group(1))
    return frozenset(has), frozenset(leaves_out)


def mcp_field_problems(meaning: str) -> list[str]:
    """Every refusal of the type, the one the candidate walk ends in and each one of a single path, read where it is raised.

    `path` stands in nearly every one of them, so a clause may name it once for
    all. Every other field a refusal carries is said to be there in one clause
    with the rest of that refusal's fields and with what sets that refusal off,
    and no clause says together fields are there that no one refusal carries,
    such as a file's `gid` beside a parent's `directory`. A clause that says
    what sets a refusal off says no field is there that the refusal does not
    carry, and leaves out none that it does.
    """
    shapes = refusal_shapes("mcp_command_untrusted")
    fields = frozenset().union(*shapes)
    assert {"rejected_candidates", "directory", "gid"} <= fields
    said = clauses(meaning)
    read = [presence(clause, fields) for clause in said]
    named = [has for has, _leaves_out in read]
    problems = []
    if not fields <= frozenset().union(*named):
        problems.append(f"says no refusal has {sorted(fields - frozenset().union(*named))}")
    for shape in shapes:
        if not any(shape - {"path"} <= clause for clause in named):
            problems.append(f"no clause says {sorted(shape - {'path'})} are there together")
    for clause, (has, leaves_out) in zip(said, read, strict=True):
        if not any(has <= shape for shape in shapes):
            problems.append(f"says together {sorted(has)} are there, which no one refusal carries: {clause}")
        for raised, sets_off, carries in MCP_REFUSALS:
            if re.search(sets_off, clause, re.IGNORECASE) and not has <= carries:
                problems.append(f"gives the refusal raised as {raised!r} {sorted(has - carries)}, which it does not carry: {clause}")
            if re.search(sets_off, clause, re.IGNORECASE) and leaves_out & carries:
                problems.append(f"leaves {sorted(leaves_out & carries)} out of the refusal raised as {raised!r}, which carries them: {clause}")
    for raised, sets_off, carries in MCP_REFUSALS:
        own = carries - {"path"}
        if own and not any(re.search(sets_off, clause, re.IGNORECASE) and own <= has for clause, has in zip(said, named, strict=True)):
            problems.append(f"no clause says what sets off the refusal raised as {raised!r} and that it adds {sorted(own)}")
    return problems


def test_the_mcp_command_untrusted_entry_names_the_fields_of_each_refusal_together() -> None:
    entry = catalogue_entry("mcp_command_untrusted")
    assert entry is not None

    assert mcp_field_problems(entry["meaning"]) == []


# The meaning with the fields of one refusal said away, named apart from what
# sets that refusal off, or handed to a refusal that does not carry them: (as
# the meaning says it, turned around).
MCP_TURNED: dict[str, tuple[str, str]] = {
    "never_adds_directory": ("or root adds `directory`", "or root never adds `directory`"),
    "does_not_add_untrusted_because": ("execute bit adds `untrusted_because`", "execute bit does not add `untrusted_because`"),
    "cannot_add_target": ("another symlink adds `target`", "another symlink cannot add `target`"),
    "doesnt_add_directory": ("or root adds `directory`", "or root doesn't add `directory`"),
    "adds_no_directory": ("or root adds `directory`", "or root adds no `directory`"),
    "never_carries_path": ("A refusal of one path carries `path`", "A refusal of one path never carries `path`"),
    "symlink_owner_adds_directory": ("a launcher whose parent directory belongs to", "a launcher symlink that belongs to"),
    "symlink_owner_too_adds_directory": (
        "a launcher whose parent directory belongs to an account other than this one or root adds",
        "a launcher symlink owned by another account, or a launcher whose parent directory belongs to an account other than this one or root, adds",
    ),
    "missing_target_adds_target": ("whose target resolves through another symlink", "whose target is missing"),
    "parent_directory_adds_file_fields": ("an executable refused for its owner, its write access or a missing execute bit adds", "a launcher whose parent directory belongs to another account adds"),
    "directory_fields_without_their_trigger": ("a launcher whose parent directory belongs to an account other than this one or root adds", "a launcher refused for its directory adds"),
    "file_fields_for_the_owner_only": ("an executable refused for its owner, its write access or a missing execute bit adds", "an executable refused for its owner adds"),
    "omits_path": ("A refusal of one path carries `path`", "A refusal of one path omits `path`"),
    "lacks_directory": ("or root adds `directory`", "or root lacks `directory`"),
    "drops_target": ("another symlink adds `target`", "another symlink drops `target`"),
    "leaves_out_untrusted_because": ("execute bit adds `untrusted_because`", "execute bit leaves out `untrusted_because`"),
    "without_mode_and_uid": ("with its `mode` and `uid`", "without its `mode` and `uid`"),
    "target_refusal_omits_path": ("another symlink adds `target`", "another symlink adds `target` but omits `path`"),
    "path_is_not_included": ("A refusal of one path carries `path`", "`path` is not included in a refusal of one path"),
}


@pytest.mark.parametrize("turned", sorted(MCP_TURNED))
def test_a_meaning_that_says_a_refusal_carries_fields_it_does_not_fails(turned: str) -> None:
    said, wrong = MCP_TURNED[turned]
    meaning = (catalogue_entry("mcp_command_untrusted") or {})["meaning"]
    assert meaning.count(said) == 1, said

    assert mcp_field_problems(meaning.replace(said, wrong)) != []


# The meaning with what sets one refusal off, or the fields it carries, said
# another true way: (as the meaning says it, said again).
MCP_RESTATED: dict[str, tuple[str, str]] = {
    "parent_directory_not_owned": ("whose parent directory belongs to an account other than this one or root adds", "whose parent directory is not owned by this account or root adds"),
    "file_refused_in_negations": (
        "an executable refused for its owner, its write access or a missing execute bit adds",
        "an executable whose owner is not this account or root, which another account can write, or which has no execute bit adds",
    ),
    "never_omits_path": ("A refusal of one path carries `path`", "A refusal of one path never omits `path`"),
    "target_without_exception": ("another symlink adds `target`", "another symlink adds `target` without exception"),
    "walk_without_path": ("names every launcher that was tried and why each failed", "names every launcher that was tried and why each failed, without `path`"),
    "path_is_included": ("A refusal of one path carries `path`", "`path` is included in a refusal of one path"),
    "symlink_parent_directory": (
        "a launcher whose parent directory belongs to an account other than this one or root adds `directory`, naming that parent directory, with its `mode` and `uid`",
        "a launcher symlink whose parent directory belongs to another account adds `directory`, `mode` and `uid`",
    ),
}


@pytest.mark.parametrize("restated", sorted(MCP_RESTATED))
def test_a_meaning_that_says_each_refusal_carries_its_fields_another_way_passes(restated: str) -> None:
    said, again = MCP_RESTATED[restated]
    meaning = (catalogue_entry("mcp_command_untrusted") or {})["meaning"]
    assert meaning.count(said) == 1, said

    assert mcp_field_problems(meaning.replace(said, again)) == []
