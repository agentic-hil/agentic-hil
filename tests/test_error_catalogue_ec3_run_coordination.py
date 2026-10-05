"""Every refusal a run, the coordinator, a recovery, a JUnit write or the test
reactor hands back has an entry in the error catalogue, the entry says what the
code does, and the entry arrives.

A refusal with no entry leaves its reader with a summary and a guess. The guess
for these refusals is expensive: a run that cannot be found gets restarted on a
bench another run still holds, a recovery that could not write its ledger gets
retried around the ledger, a quarantine whose markers changed gets cleared by
hand. So the inventory of what these modules can refuse with is derived from
their source, site by site, and pinned, so a site added or lost fails here
before anybody has to notice. The inventory is held against the catalogue four
ways: the entry exists, its text makes the claims the code backs and none of
the claims it does not, the reference resource serves it, and a real refusal
through the real code path carries its fix, nested refusals included.

Values the scan finds that never reach a caller as a refusal of their own are
listed with the reason they do not, so excluding one is a decision somebody
wrote down rather than a gap. Two entries are reachable only by a caller this
package does not ship and are held to their entry and resource alone.

The run result follows one rule for its advice: it looks up the error_type it
publishes, scoped to the test reactor with the bare entry behind it, whatever
that type is, including a type a debug step passed up. A result that already
carries advice keeps it, and a run whose cleanup failed carries the scoped
`cleanup_failed:test_reactor` entry, not the debug session's. Since #694 a run
that publishes its failing step's own type takes that step's `remediation` and
`do_not` with it where the step had them, because the catalogue holds those per
backend and the run's own lookup cannot see which backend answered; the record
keeps what the result carried, so every later answer about the run says the
same thing.
"""
from __future__ import annotations

import argparse
import ast
import importlib
import json
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import pytest
from conftest import write_authoritative_config, write_config
from test_debug_sessions import debug_service, start_debug_session
from test_recover_tool import config_for
from test_run_lifecycle import bench_workspace
from test_test_reactor import (
    RecordingService,
    SymbolService,
    can_config,
    run_symbol_plan,
    symbol_plan,
    uart_config,
    write_test_config,
)

from agentic_hil import knowledge, reactorrun, runlifecycle
from agentic_hil.backends.gdbdebug import INTEGER_VALUE_WIDTHS
from agentic_hil.bench import BenchMutex
from agentic_hil.cli import build_parser, dispatch, entrypoint
from agentic_hil.config import load_authoritative_config, load_config
from agentic_hil.coordination import (
    DEBUGGER_DISCOVERY_RESOURCE,
    LEASE_RELEASE_RETRY_REASON,
    CoordinationError,
    HardwareCoordinator,
)
from agentic_hil.knowledge import (
    DEFAULT_TEST_CONFIG_PATH,
    ERROR_CATALOGUE,
    ERROR_URI_PREFIX,
    ErrorRemedy,
    catalogue_entry,
    lookup_remedy,
    remediation_fields,
)
from agentic_hil.mcp import handle_mcp_message
from agentic_hil.test_reactor import TestReactor, declared_devices, load_test_config
from agentic_hil.tools import AgenticHILToolService

# The scope the reactor's run result looks its advice up under. Read here as a
# literal rather than imported, so a missing constant fails its own test below
# instead of the whole module's collection.
REACTOR_SCOPE = "test_reactor"

# ---------------------------------------------------------------------------
# The scan.

SCANNED_MODULES = ("runlifecycle", "bench", "coordination", "junit", "reactorrun", "test_reactor")

# Calls that build a refusal from an error_type argument, and which argument it is.
PRODUCERS = {"ConfigError": 0, "exception_result": 1, "reject_nonfinite_numbers": 1, "tool_error": 1}

# Calls that take an error_type to look something up about it, not to refuse with it.
CONSUMERS = frozenset(
    {
        "remediation_fields",
        "lookup_remedy",
        "command_line_remediation",
        "_verdict_case",
        "likely_causes",
        "can_likely_causes",
        "can_failure_causes",
        "_likely_causes",
        "_summary_for_error",
        "_failure_summary",
        "_gdb_server_summary",
        "_gdb_server_likely_causes",
    }
)

MODULE_SCOPE = "<module>"
_SCOPE_NODES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)


@dataclass(eq=False)
class _Scope:
    """One function, class or module body, with what each local name is bound to.

    A binding of None is one the scan cannot follow (a parameter, a loop or
    tuple target, an import); anything else is the expression assigned."""

    name: str
    node: ast.AST
    parent: _Scope | None
    bindings: dict[str, list[ast.expr | None]] = field(default_factory=dict)

    @property
    def is_class(self) -> bool:
        return isinstance(self.node, ast.ClassDef)

    @property
    def is_module(self) -> bool:
        return self.parent is None


def _own_nodes(node: ast.AST):
    """Every node of a scope's own body, not of the scopes nested in it."""
    for child in ast.iter_child_nodes(node):
        if isinstance(child, _SCOPE_NODES):
            continue
        yield child
        yield from _own_nodes(child)


def _target_names(target: ast.expr) -> list[str]:
    return [node.id for node in ast.walk(target) if isinstance(node, ast.Name)]


def _bindings(node: ast.AST) -> dict[str, list[ast.expr | None]]:
    bindings: dict[str, list[ast.expr | None]] = {}

    def bind(name: str, value: ast.expr | None) -> None:
        bindings.setdefault(name, []).append(value)

    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
        arguments = node.args
        for argument in (*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs):
            bind(argument.arg, None)
        for argument in (arguments.vararg, arguments.kwarg):
            if argument is not None:
                bind(argument.arg, None)
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bind(child.name, None)
    for child in _own_nodes(node):
        if isinstance(child, ast.Assign):
            for target in child.targets:
                if isinstance(target, ast.Name):
                    bind(target.id, child.value)
                else:
                    for name in _target_names(target) if isinstance(target, (ast.Tuple, ast.List, ast.Starred)) else ():
                        bind(name, None)
        elif isinstance(child, ast.AnnAssign) and isinstance(child.target, ast.Name):
            if child.value is not None:
                bind(child.target.id, child.value)
        elif isinstance(child, ast.NamedExpr):
            bind(child.target.id, child.value)
        elif isinstance(child, ast.AugAssign) and isinstance(child.target, ast.Name):
            bind(child.target.id, None)
        elif isinstance(child, (ast.For, ast.AsyncFor, ast.comprehension)):
            for name in _target_names(child.target):
                bind(name, None)
        elif isinstance(child, ast.withitem) and child.optional_vars is not None:
            for name in _target_names(child.optional_vars):
                bind(name, None)
        elif isinstance(child, ast.ExceptHandler) and child.name:
            bind(child.name, None)
        elif isinstance(child, (ast.Import, ast.ImportFrom)):
            for alias in child.names:
                bind((alias.asname or alias.name).partition(".")[0], None)
        elif isinstance(child, (ast.Global, ast.Nonlocal)):
            for name in child.names:
                bind(name, None)
    return bindings


def _callee(call: ast.Call) -> str | None:
    function = call.func
    if isinstance(function, ast.Name):
        return function.id
    if isinstance(function, ast.Attribute):
        return function.attr
    return None


Values = tuple[set[str], set[tuple[str, str]]]


def _resolve(name: str, scope: _Scope) -> _Scope:
    """The scope a name used in `scope` is bound in, by Python's own rule."""
    current: _Scope | None = scope
    while current is not None:
        if current.is_module:
            return current
        if (current is scope or not current.is_class) and name in current.bindings:
            return current
        current = current.parent
    raise AssertionError("every scope chain ends at the module")


def _values(node: ast.expr, scope: _Scope, module: object, seen: set[tuple[int, str]]) -> Values:
    """The literal error_types an expression can be, and each part it leaves unresolved.

    An unresolved part is answered as (the function it is written in, its text)."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return {node.value}, set()
    if isinstance(node, ast.Name):
        home = _resolve(node.id, scope)
        if home.is_module:
            value = getattr(module, node.id, None)
            if isinstance(value, str):
                return {value}, set()
            return set(), {(home.name, node.id)}
        key = (id(home.node), node.id)
        if key in seen:
            return set(), set()
        seen = seen | {key}
        constants: set[str] = set()
        dynamic: set[tuple[str, str]] = set()
        for bound in home.bindings[node.id]:
            if bound is None:
                dynamic.add((home.name, node.id))
                continue
            found = _values(bound, home, module, seen)
            constants |= found[0]
            dynamic |= found[1]
        return constants, dynamic
    if isinstance(node, ast.IfExp):
        body, orelse = _values(node.body, scope, module, seen), _values(node.orelse, scope, module, seen)
        return body[0] | orelse[0], body[1] | orelse[1]
    if isinstance(node, ast.BoolOp):
        constants = set()
        dynamic = set()
        for value in node.values:
            found = _values(value, scope, module, seen)
            constants |= found[0]
            dynamic |= found[1]
        return constants, dynamic
    if isinstance(node, ast.Call) and _callee(node) == "str" and len(node.args) == 1 and isinstance(node.func, ast.Name):
        return _values(node.args[0], scope, module, seen)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "get" and len(node.args) == 2:
        return _values(node.args[1], scope, module, seen)[0], {(scope.name, ast.unparse(node))}
    return set(), {(scope.name, ast.unparse(node))}


@dataclass(frozen=True)
class Inventory:
    """What the scanned modules can refuse with.

    `constants` maps each literal error_type to the (module, function) sites
    that set it; `dynamic` is every (module, function, expression) the scan
    could not resolve to literals; `unclassified` is every (module, function,
    callee) call to something taking an `error_type` that is neither a known
    producer nor a known consumer."""

    constants: dict[str, frozenset[tuple[str, str]]]
    dynamic: frozenset[tuple[str, str, str]]
    unclassified: frozenset[tuple[str, str, str]]


def _functions_taking_an_error_type() -> frozenset[str]:
    package = Path(importlib.import_module("agentic_hil").__file__).parent
    names: set[str] = set()
    for path in package.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ClassDef):
                for item in node.body:
                    if isinstance(item, ast.FunctionDef) and item.name == "__init__" and "error_type" in [arg.arg for arg in item.args.args]:
                        names.add(node.name)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name != "__init__":
                arguments = node.args.posonlyargs + node.args.args + node.args.kwonlyargs
                if "error_type" in [arg.arg for arg in arguments]:
                    names.add(node.name)
    return frozenset(names)


def scan_module(
    name: str,
    source: str,
    module: object,
    takes_an_error_type: frozenset[str],
) -> tuple[dict[str, set[tuple[str, str]]], set[tuple[str, str, str]], set[tuple[str, str, str]]]:
    """One module's error_types, by the function that sets each."""
    constants: dict[str, set[tuple[str, str]]] = {}
    dynamic: set[tuple[str, str, str]] = set()
    unclassified: set[tuple[str, str, str]] = set()

    def add(node: ast.expr, scope: _Scope) -> None:
        found, unresolved = _values(node, scope, module, set())
        for value in found:
            constants.setdefault(value, set()).add((name, scope.name))
        for where, expression in unresolved:
            dynamic.add((name, where, expression))

    def inspect(node: ast.AST, scope: _Scope) -> None:
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values, strict=True):
                if isinstance(key, ast.Constant) and key.value == "error_type":
                    add(value, scope)
        elif isinstance(node, ast.Call):
            callee = _callee(node)
            if callee in takes_an_error_type and callee not in PRODUCERS and callee not in CONSUMERS:
                unclassified.add((name, scope.name, callee))
            if callee not in CONSUMERS:
                for keyword in node.keywords:
                    if keyword.arg == "error_type":
                        add(keyword.value, scope)
            if callee in PRODUCERS and len(node.args) > PRODUCERS[callee]:
                add(node.args[PRODUCERS[callee]], scope)
            if (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == "setdefault"
                and len(node.args) == 2
                and isinstance(node.args[0], ast.Constant)
                and node.args[0].value == "error_type"
            ):
                add(node.args[1], scope)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Subscript) and isinstance(target.slice, ast.Constant) and target.slice.value == "error_type":
                    add(node.value, scope)
        elif isinstance(node, ast.Return) and node.value is not None and scope.name.rpartition(".")[2].endswith("error_type"):
            add(node.value, scope)

    def visit(node: ast.AST, scope: _Scope) -> None:
        if isinstance(node, ast.ClassDef):
            # Class constants a shared method refuses with, such as the session
            # kinds' `not_owned_error`.
            for item in node.body:
                if isinstance(item, ast.AnnAssign):
                    target, value = item.target, item.value
                elif isinstance(item, ast.Assign) and len(item.targets) == 1:
                    target, value = item.targets[0], item.value
                else:
                    continue
                if isinstance(target, ast.Name) and target.id.endswith("_error") and isinstance(value, ast.Constant) and isinstance(value.value, str) and value.value:
                    qualified = node.name if scope.is_module else f"{scope.name}.{node.name}"
                    constants.setdefault(value.value, set()).add((name, qualified))
        if isinstance(node, _SCOPE_NODES):
            label = "<lambda>" if isinstance(node, ast.Lambda) else node.name
            qualified = label if scope.is_module else f"{scope.name}.{label}"
            scope = _Scope(qualified, node, scope, _bindings(node))
        for child in ast.iter_child_nodes(node):
            inspect(child, scope)
            visit(child, scope)

    tree = ast.parse(source)
    visit(tree, _Scope(MODULE_SCOPE, tree, None, _bindings(tree)))
    return constants, dynamic, unclassified


def module_source(name: str) -> str:
    return Path(importlib.import_module(f"agentic_hil.{name}").__file__).read_text(encoding="utf-8")


def scan_sources(overrides: dict[str, str] | None = None) -> Inventory:
    """The scanned modules' inventory, with any module's source replaced by `overrides`."""
    takes = _functions_taking_an_error_type()
    constants: dict[str, set[tuple[str, str]]] = {}
    dynamic: set[tuple[str, str, str]] = set()
    unclassified: set[tuple[str, str, str]] = set()
    for name in SCANNED_MODULES:
        module = importlib.import_module(f"agentic_hil.{name}")
        source = (overrides or {}).get(name) or module_source(name)
        found = scan_module(name, source, module, takes)
        for value, sites in found[0].items():
            constants.setdefault(value, set()).update(sites)
        dynamic |= found[1]
        unclassified |= found[2]
    return Inventory({value: frozenset(sites) for value, sites in constants.items()}, frozenset(dynamic), frozenset(unclassified))


@lru_cache(maxsize=1)
def scan() -> Inventory:
    return scan_sources()


# ---------------------------------------------------------------------------
# The pinned inventory: every literal error_type with the functions that set it.


def _at(module: str, *functions: str) -> frozenset[tuple[str, str]]:
    return frozenset((module, function) for function in functions)


PINNED_SITES: dict[str, frozenset[tuple[str, str]]] = {
    "audit_failed": _at("test_reactor", "result_error_type"),
    "breakpoint_cleanup_failed": _at("test_reactor", "DebuggerRunner._run_until_breakpoint"),
    "can_listen_only_mode": _at("test_reactor", "CanRunner.permission_refusal"),
    "can_session_not_owned": _at("test_reactor", "CanRunner"),
    "cleanup_exception": _at("reactorrun", "run_registered_plan") | _at("test_reactor", "StepDevice.cleanup_call"),
    "cleanup_failed": _at("reactorrun", "run_registered_plan") | _at("test_reactor", "TestReactor.run"),
    "com_port_not_bound": _at("test_reactor", "UartRunner.unbound_refusal"),
    "comparator_unmet": _at("test_reactor", "CanRunner._compare", "DebuggerRunner._judge_symbol", "UartRunner._compare"),
    "config_changed": _at("coordination", "HardwareCoordinator.recover"),
    "coordination_closed": _at("coordination", "HardwareCoordinator._require_open"),
    "coordination_state_invalid": _at("coordination", "HardwareCoordinator._mark_incident_resources", "HardwareCoordinator.recover", "_read_record_at"),
    "device_busy": _at("bench", "BenchMutex.busy_result"),
    "interrupted": _at("reactorrun", "run_registered_plan"),
    "invalid_argument": (
        _at("bench", "validated_wait")
        | _at("coordination", "HardwareCoordinator.begin_run", "_bound_debugger_device")
        | _at("runlifecycle", "validated_run_handle")
        | _at("test_reactor", "CanRunner.preflight", "StepDevice.schema_refusal", "TestReactor.step_device", "pattern_refusal")
    ),
    "junit_xml_requires_synchronous_run": _at("junit", "detached_junit_refusal"),
    "junit_xml_write_failed": _at("junit", "result_with_junit_xml"),
    "not_supported": _at("test_reactor", "StepDevice.execute"),
    "operator_confirmation_required": _at("coordination", "HardwareCoordinator.recover"),
    "permission_denied": _at("test_reactor", "exclusive_permission_preflight_error", "participant_permission_preflight_error", "permission_preflight_error"),
    "preflight_exception": _at("test_reactor", "TestReactor.run"),
    "quarantine_changed": _at("coordination", "HardwareCoordinator.recover"),
    "quarantine_id_required": _at("coordination", "HardwareCoordinator.recover"),
    "reactor_exception": _at("reactorrun", "run_registered_plan") | _at("runlifecycle", "RunRegistration.__exit__"),
    "recovery_audit_failed": _at("coordination", "HardwareCoordinator.recover"),
    "recovery_persist_failed": _at("coordination", "HardwareCoordinator.recover"),
    "resource_busy": _at("coordination", "HardwareCoordinator._acquire_lock", "HardwareCoordinator.recover"),
    "resource_not_quarantined": _at("coordination", "HardwareCoordinator.recover"),
    "resource_quarantined": _at("coordination", "HardwareCoordinator._foreign_incident_refusal", "HardwareCoordinator._quarantined_result"),
    "run_already_active": _at("coordination", "HardwareCoordinator.begin_run") | _at("runlifecycle", "RunRegistration.take"),
    "run_not_found": _at("runlifecycle", "request_run_stop", "run_status"),
    "run_state_invalid": _at("runlifecycle", "_unreadable_record_refusal", "known_runs", "read_run_record"),
    "run_state_unwritable": _at("runlifecycle", "_runs_directory_unwritable"),
    "run_stopped": _at("bench", "BenchMutex._take") | _at("test_reactor", "TestReactor.execute_repeat", "TestReactor.run"),
    "run_worker_failed": _at("runlifecycle", "start_detached_run"),
    "run_worker_gone": _at("runlifecycle", "request_run_stop"),
    "run_worker_unresponsive": _at("runlifecycle", "start_detached_run"),
    "step_exception": _at("test_reactor", "TestReactor.execute_recorded_step"),
    "step_failed": _at("test_reactor", "result_error_type"),
    "symbol_size_mismatch": _at("test_reactor", "DebuggerRunner._judge_symbol"),
    "symbol_width_not_numeric": _at("test_reactor", "DebuggerRunner._judge_symbol"),
    "target_failed": _at("test_reactor", "result_error_type"),
    "target_stop": _at("test_reactor", "DebuggerRunner._debug_start"),
    "test_config_invalid": _at(
        "test_reactor",
        "TestReactor.run",
        "load_test_config",
        "raise_outdated_plan_version",
        "raise_test_config_validation_error",
        "reject_superseded_plan_version",
        "validate_test_steps_schema",
    ),
    "test_config_not_found": _at("test_reactor", "load_test_config"),
    "test_config_schema_invalid": _at("test_reactor", "validate_test_config_schema"),
    "test_config_unreadable": _at("test_reactor", "load_test_config"),
    "uart_expect_timeout": _at("test_reactor", "UartRunner._expect"),
    "uart_session_not_owned": _at("test_reactor", "UartRunner"),
    "undeclared_device": _at("coordination", "HardwareCoordinator.acquire"),
    "unexpected_stop": _at("test_reactor", "DebuggerRunner._run_until_breakpoint"),
    "unknown": _at("runlifecycle", "RunRegistration.__exit__", "_detached_terminal_result"),
    "unknown_action": _at("test_reactor", "TestReactor.step_device"),
}

# The sites whose error_type is not a literal, each with what it carries. Every
# one is a value that came from somewhere the scan already covers or names.
PINNED_DYNAMIC: dict[tuple[str, str, str], str] = {
    ("coordination", "HardwareCoordinator._quarantine_registered_lease", "type(error).__name__"): "a Python exception class name recorded beside a lease the coordinator could not release",
    ("coordination", "HardwareCoordinator.begin_run", "error.error_type"): "a ConfigError from resolving the plan's devices, passed on under its own type",
    ("coordination", "HardwareCoordinator.record_cleanup_event", "type(error).__name__"): "a Python exception class name in a cleanup event",
    ("coordination", "HardwareCoordinator.release_lease", "type(persist_error).__name__"): "a Python exception class name recorded for a release that could not be written",
    ("runlifecycle", "RunRegistration.finish", "result.get('error_type')"): "the run result's own type, copied into its record",
    ("runlifecycle", "_detached_terminal_result", "record.get('error_type')"): "the recorded type of a run that already ended, echoed back to its start",
    ("test_reactor", "DebuggerRunner._debug_start", "result.get('target_error_type', 'target_stop')"): "a debug session's target type, passed up from the step",
    ("test_reactor", "DebuggerRunner._run_until_breakpoint", "result_error_type(cleared)"): "the breakpoint clear's own type, kept inside `breakpoint_cleanup`",
    ("test_reactor", "SessionDevice.close_session", "self.not_owned_error"): "the session kinds' class constants, which the scan collects as constants",
    ("test_reactor", "TestReactor.execute_repeat", "result_error_type(failure or {})"): "a failed nested step's type, passed up to its repeat block",
    ("test_reactor", "TestReactor.run", "result_error_type(failure)"): "a failed step's type, passed up to the run",
    ("test_reactor", "TestReactor.run", "validation_error.get('error_type')"): "the preflight refusal's own type, found where the preflight sets it",
    ("test_reactor", "exception_result", "error_type"): "the helper's parameter, read at each call to it",
    ("test_reactor", "result_error_type", "result.get('target_error_type')"): "a debug session's target type, passed up from the step",
    ("test_reactor", "result_error_type", "result['error_type']"): "a step result's own type, passed up",
}


def inventory_drift(inventory: Inventory) -> dict[str, list]:
    """How an inventory differs from the pinned one, site by site."""
    found = {(value, site) for value, sites in inventory.constants.items() for site in sites}
    pinned = {(value, site) for value, sites in PINNED_SITES.items() for site in sites}
    return {
        "new_sites": sorted(found - pinned),
        "lost_sites": sorted(pinned - found),
        "new_dynamic": sorted(inventory.dynamic - set(PINNED_DYNAMIC)),
        "lost_dynamic": sorted(set(PINNED_DYNAMIC) - inventory.dynamic),
        "unclassified": sorted(inventory.unclassified),
    }


NO_DRIFT = {"new_sites": [], "lost_sites": [], "new_dynamic": [], "lost_dynamic": [], "unclassified": []}


def test_the_scan_finds_exactly_the_pinned_inventory() -> None:
    """A site the scan stops finding, or a new one nobody classified, fails here.

    New refusals in these modules are the point: each has to be put in one of
    the classes below, which for a refusal that reaches a caller means writing
    its entry. The pin is per function, so a known type set in a new place is a
    new site too: it is a new path for the type's advice to be missing on."""
    assert inventory_drift(scan()) == NO_DRIFT


def test_every_non_literal_site_says_what_it_carries() -> None:
    for site, reason in PINNED_DYNAMIC.items():
        assert reason.strip(), site


INJECTED_REFUSAL = "\n\ndef _injected_refusal():\n    error_type = {value}\n    return {{'ok': False, 'error_type': error_type}}\n"


@pytest.mark.parametrize("value", ["new_refusal", "comparator_unmet"])
def test_the_scan_reports_a_site_added_through_a_local_name(value: str) -> None:
    """A refusal built from a local variable is a site, whether its type is new or known."""
    source = module_source("test_reactor") + INJECTED_REFUSAL.format(value=repr(value))

    drift = inventory_drift(scan_sources({"test_reactor": source}))

    assert drift == {**NO_DRIFT, "new_sites": [(value, ("test_reactor", "_injected_refusal"))]}


CLEANUP_PRODUCER = 'return exception_result(tool, "cleanup_exception", "Cleanup action raised an exception.", error)'


def test_the_scan_reports_a_site_that_was_removed() -> None:
    source = module_source("test_reactor")
    assert source.count(CLEANUP_PRODUCER) == 1

    drift = inventory_drift(scan_sources({"test_reactor": source.replace(CLEANUP_PRODUCER, "return {}")}))

    assert drift == {**NO_DRIFT, "lost_sites": [("cleanup_exception", ("test_reactor", "StepDevice.cleanup_call"))]}


# ---------------------------------------------------------------------------
# The classes every scanned type falls in.

# Every entry this area needs. A scoped key where the bare error_type is
# another area's refusal with another fix: `cleanup_failed` is also what a
# debug session answers when its own teardown fails, and the reactor's cleanup
# failure is a different case with a different first move.
REQUIRED_KEYS = (
    # Detached and synchronous runs (runlifecycle.py, reactorrun.py).
    "run_not_found",
    "run_state_invalid",
    "run_worker_failed",
    "run_worker_unresponsive",
    "run_worker_gone",
    "run_stopped",
    "reactor_exception",
    "interrupted",
    # Recovery and coordination (coordination.py).
    "quarantine_id_required",
    "resource_not_quarantined",
    "quarantine_changed",
    "recovery_audit_failed",
    "recovery_persist_failed",
    "coordination_state_invalid",
    "resource_busy",
    "operator_confirmation_required",
    "coordination_closed",
    # JUnit output (junit.py).
    "junit_xml_requires_synchronous_run",
    "junit_xml_write_failed",
    # The reactor's own refusals (test_reactor.py, reactorrun.py).
    "cleanup_exception",
    "cleanup_failed:test_reactor",
    "comparator_unmet",
    "symbol_size_mismatch",
    "symbol_width_not_numeric",
    "uart_expect_timeout",
    "unexpected_stop",
    "breakpoint_cleanup_failed",
    "uart_session_not_owned",
    "can_session_not_owned",
    "step_exception",
    "preflight_exception",
    "test_config_not_found",
    "test_config_unreadable",
    "test_config_schema_invalid",
    "audit_failed",
    "step_failed",
)
REQUIRED_TYPES = frozenset(key.partition(":")[0] for key in REQUIRED_KEYS)

# Required, but no caller this package ships reaches them, so they are held to
# their entry and the reference resource and not to a refusal of their own. A
# client of the coordinator outside this package can still meet either one.
RESOLUTION_ONLY: dict[str, str] = {
    "operator_confirmation_required": (
        "every caller of HardwareCoordinator.recover in this package passes safe_state_confirmed=True: the hardware_recover tool, "
        "the coordinator's own recovery action, and the recover command, whose parser requires --confirm-safe-state first"
    ),
    "coordination_closed": "only AgenticHILToolService.close closes the coordinator, and service.call answers service_closed before any tool reaches it",
}

# Catalogue keys a caller of this area meets that another area writes. The
# reactor passes these up from a debug step and its run result looks each one
# up by the rule above, so their entries arrive here once they exist.
OWNED_ELSEWHERE: dict[str, str] = {
    "cleanup_failed": "the bare key is a debug session's own teardown failure; the reactor's run answers under `cleanup_failed:test_reactor`",
    "target_exception": "a debug session's target type, passed up from a step's `target_error_type`",
    "unexpected_breakpoint": "a debug session's target type, passed up from a step's `target_error_type`",
    "debugger_error": "a debug session's own refusal, passed up from a debug step",
}

# Found by the scan, never a refusal a caller receives under that name.
EXCLUDED: dict[str, str] = {
    "unknown": (
        "the fallback of a terminal run record that names no error_type, and no run writes one: RunRegistration.finish copies the "
        "run result's own type, run_plan either finishes or leaves through RunRegistration.__exit__, which writes reactor_exception, "
        "every result with ok false that TestReactor.run or run_registered_plan builds names its type, and RunRegistration.take is "
        "used by run_plan alone, so only a record written by hand reads `unknown`"
    ),
    "target_stop": "fallback for a debug result with target_ok false and no target_error_type, and every debug result with target_ok false names its target_error_type",
    "target_failed": "the same fallback in result_error_type, unreachable for the same reason as target_stop",
    "not_supported": "a step action the device kind does not serve is refused by the plan loader as test_config_invalid before the reactor dispatches it",
    "unknown_action": "an action no device kind declares is refused by the plan loader as test_config_invalid before the reactor dispatches it",
}

# Found by the scan, and already catalogued by an earlier entry.
ALREADY_COVERED = frozenset(
    {
        "can_listen_only_mode",
        "com_port_not_bound",
        "config_changed",
        "device_busy",
        "invalid_argument",
        "permission_denied",
        "resource_quarantined",
        "run_already_active",
        "run_state_unwritable",
        "test_config_invalid",
        "undeclared_device",
    }
)


def test_the_classes_do_not_overlap() -> None:
    assert not REQUIRED_TYPES & set(EXCLUDED)
    assert not REQUIRED_TYPES & ALREADY_COVERED
    assert not set(EXCLUDED) & ALREADY_COVERED
    assert set(RESOLUTION_ONLY) <= set(REQUIRED_KEYS)
    assert not set(OWNED_ELSEWHERE) & set(REQUIRED_KEYS)
    assert not set(OWNED_ELSEWHERE) & (set(EXCLUDED) | ALREADY_COVERED)


def test_every_type_the_scan_finds_is_classified() -> None:
    assert set(PINNED_SITES) == REQUIRED_TYPES | set(EXCLUDED) | ALREADY_COVERED


def test_every_exclusion_and_handover_says_why() -> None:
    for error_type, reason in {**EXCLUDED, **RESOLUTION_ONLY, **OWNED_ELSEWHERE}.items():
        assert reason.strip(), error_type


def test_the_already_covered_types_resolve() -> None:
    for error_type in sorted(ALREADY_COVERED):
        assert lookup_remedy(error_type) is not None, error_type


def test_every_refusal_found_in_these_modules_has_an_entry() -> None:
    """The guard: a refusal these modules can return has a catalogue entry."""
    missing = {key: sorted(PINNED_SITES.get(key.partition(":")[0], ())) for key in REQUIRED_KEYS if key not in ERROR_CATALOGUE}

    assert missing == {}, f"refusals with no catalogue entry: {missing}"


def test_the_reactor_scope_is_published_beside_its_entry() -> None:
    scope = getattr(knowledge, "TEST_REACTOR_SCOPE", None)

    assert scope == REACTOR_SCOPE
    assert f"cleanup_failed:{scope}" in REQUIRED_KEYS


# ---------------------------------------------------------------------------
# What each entry says.
#
# A claim is a tuple of patterns that must all match inside one clause, so a
# claim holds only where the text says those things together. `first` is held
# against the first remediation step, `order` must find each pattern at a later
# step than the one before, `never` must not match a clause that is not itself
# a negation, and `only_with` says a step naming the first pattern names the
# condition the second pattern is, so advice for one case is never handed to
# another.

Claim = tuple[str, ...]

NEGATION = re.compile(r"\b(not|never|no|nothing|none|cannot|without|neither|nor|instead of)\b|n't", re.IGNORECASE)


@dataclass(frozen=True)
class Says:
    meaning: tuple[Claim, ...]
    first: Claim
    do_not: tuple[Claim, ...]
    order: tuple[str, ...] = ()
    never: tuple[str, ...] = ()
    only_with: tuple[tuple[str, str], ...] = ()


def clauses(text: str) -> list[str]:
    return [clause for clause in re.split(r"(?<=[.;?!])\s+", text) if clause.strip()]


def claim_holds(claim: Claim, texts: list[str]) -> bool:
    return any(all(re.search(pattern, clause, re.IGNORECASE) for pattern in claim) for text in texts for clause in clauses(text))


def spec_violations(key: str, entry: dict) -> list[str]:
    """Everything the entry says against what `SAYS[key]` holds it to."""
    says = SAYS[key]
    meaning = entry.get("meaning", "")
    remediation = list(entry.get("remediation") or [])
    do_not = list(entry.get("do_not") or [])
    problems: list[str] = []
    if not meaning.strip() or not remediation or not all(step.strip() for step in remediation):
        problems.append("no meaning or no remediation")
    if not do_not or not all(step.strip() for step in do_not):
        problems.append("no do_not")
    for claim in says.meaning:
        if not claim_holds(claim, [meaning]):
            problems.append(f"the meaning does not say {claim}")
    for pattern in says.first:
        if not remediation or not re.search(pattern, remediation[0], re.IGNORECASE):
            problems.append(f"the first step does not say {pattern!r}")
    previous = -1
    for pattern in says.order:
        index = next((i for i, step in enumerate(remediation) if re.search(pattern, step, re.IGNORECASE)), None)
        if index is None or index <= previous:
            problems.append(f"{pattern!r} is not in a step after the one before it")
        else:
            previous = index
    for claim in says.do_not:
        if not claim_holds(claim, do_not):
            problems.append(f"do_not does not say {claim}")
    for text in [meaning, *remediation]:
        for clause in clauses(text):
            if NEGATION.search(clause):
                continue
            for pattern in says.never:
                if re.search(pattern, clause, re.IGNORECASE):
                    problems.append(f"says {pattern!r} outright: {clause!r}")
    for named, condition in says.only_with:
        for step in remediation:
            if re.search(named, step, re.IGNORECASE) and not re.search(condition, step, re.IGNORECASE):
                problems.append(f"names {named!r} without {condition!r}: {step!r}")
    return problems


SAYS: dict[str, Says] = {
    "resource_busy": Says(
        meaning=((r"nothing was driven", r"nothing changed"), (r"carries `resources`", r"another Agentic HIL process"), (r"no `resources`", r"this server")),
        first=(r"carries `resources`", r"hardware_lease_status", r"owner_active", r"device_holds"),
        order=(r"hardware_lease_status", r"debug_stop_session", r"bench_run_stop"),
        do_not=((r"do not delete lock files",), (r"do not retry in a tight loop",)),
        never=(r"\b(delete|remove)\b.*\block",),
        only_with=((r"debug_stop_session", r"no `resources`"), (r"bench_run_stop", r"no `resources`"), (r"hardware_lease_status", r"carries `resources`")),
    ),
    "coordination_closed": Says(
        meaning=((r"closed", r"shuts down"), (r"nothing was locked or driven", r"nothing changed")),
        first=(r"start the server or the command again", r"retry is safe"),
        do_not=((r"do not keep calling",),),
    ),
    "operator_confirmation_required": Says(
        meaning=((r"without the operator's confirmation", r"nothing was cleared", r"quarantine stands"), (r"only a person at the bench",)),
        first=(r"--confirm-safe-state", r"--quarantine-id", r"lease-status"),
        order=(r"--confirm-safe-state", r"operator_statement"),
        do_not=((r"do not confirm a safe state nobody has looked at",),),
        never=(r"safe_state_confirmed",),
    ),
    "coordination_state_invalid": Says(
        meaning=(
            (r"not one it can trust", r"stopped"),
            (r"`resource`", r"error_class", r"errno"),
            (r"unlockable_lock_keys",),
            (r"audit_error", r"canonical audit ledger"),
            (r"audit_ok", r"false"),
        ),
        first=(r"`resource`", r"error_class", r"errno", r"backend_error"),
        order=(r"permission|full disk", r"operator's to judge", r"unlockable_lock_keys"),
        do_not=((r"do not delete or hand-edit", r"coordination records"), (r"do not move `state_root`",)),
        never=(r"\b(delete|rewrite|hand-edit)\b",),
    ),
    "quarantine_id_required": Says(
        meaning=((r"no quarantine id", r"nothing was cleared"),),
        first=(r"lease-status", r"--quarantine-id"),
        do_not=((r"do not guess",),),
    ),
    "resource_not_quarantined": Says(
        meaning=((r"no quarantined incident", r"nothing changed"),),
        first=(r"hardware_lease_status",),
        order=(r"hardware_lease_status", r"standing_incidents"),
        do_not=((r"do not sign again",), (r"delete coordination records",)),
        never=(r"--confirm-safe-state",),
    ),
    "quarantine_changed": Says(
        meaning=((r"nothing was cleared", r"quarantine stands"), (r"with no `resource`", r"another id|newer incident"), (r"with `resource`", r"marker")),
        first=(r"lease-status", r"check the board", r"quarantine_id"),
        order=(r"lease-status", r"`resource`", r"hand the operator"),
        do_not=((r"do not edit or delete the marker",), (r"do not sign for the new id without looking at the board",)),
        never=(r"\bold id\b",),
    ),
    "recovery_audit_failed": Says(
        meaning=((r"nothing was cleared",), (r"ledger line is written before any marker is released",), (r"quarantine stands", r"same `quarantine_id`")),
        first=(r"backend_error", r"same `quarantine_id`"),
        do_not=((r"do not clear the quarantine some other way",), (r"do not move `state_root`",)),
        never=(r"\b(was|were|is) (cleared|released)\b",),
    ),
    "recovery_persist_failed": Says(
        meaning=((r"in the ledger",), (r"quarantine stands",), (r"recovery_pending",)),
        first=(r"backend_error", r"same `quarantine_id`", r"retry is safe", r"`resumed`"),
        do_not=((r"do not treat the bench as recovered",), (r"do not delete markers",)),
        never=(r"\bbench (is|was) recovered\b",),
    ),
    "run_not_found": Says(
        meaning=((r"no record", r"`run`"), (r"newest ended runs",)),
        first=(r"test_reactor_status", r"no `run`"),
        do_not=((r"do not take a missing record as proof",),),
        never=(r"start the plan again",),
    ),
    "run_state_invalid": Says(
        meaning=((r"could not be read", r"cannot say"), (r"another version", r"no `backend_error`"), (r"stop by name is refused",)),
        first=(r"backend_error|record_error", r"retry is safe"),
        order=(r"backend_error", r"another version", r"hardware_lease_status"),
        do_not=((r"do not delete or rewrite the record",), (r"do not start the plan again",)),
        never=(r"\bthe run (has )?ended\b",),
    ),
    "run_worker_failed": Says(
        meaning=((r"ended before it published a record",), (r"no run exists", r"nothing was locked or driven"), (r"exit_code",), (r"worker_output",)),
        first=(r"worker_output", r"exit_code"),
        order=(r"worker_output", r"without detaching"),
        do_not=((r"do not restart the detached run unchanged",), (r"do not delete the runs directory",)),
    ),
    "run_worker_unresponsive": Says(
        meaning=((r"may still be alive",), (r"stop was planted",), (r"retry_safe", r"false")),
        first=(r"test_reactor_status", r"later"),
        order=(r"test_reactor_status", r"hardware_lease_status", r"worker_output"),
        do_not=((r"do not start the plan again at once",), (r"do not delete the planted stop",)),
        never=(r"retry is safe", r"\b(at once|immediately|right away)\b"),
    ),
    "run_worker_gone": Says(
        meaning=((r"gone", r"no orderly end"), (r"no report and no verdict",), (r"dead-owner",)),
        first=(r"hardware_lease_status", r"quarantine_id"),
        order=(r"hardware_lease_status", r"resource_quarantined"),
        do_not=((r"do not read the missing report as a pass or a failure",), (r"do not delete the run record or the lock files",), (r"do not send another stop",)),
        never=(r"test_reactor_stop",),
    ),
    "run_stopped": Says(
        meaning=(
            (r"stop request",),
            (r"neither a pass nor a failure",),
            (r"stopped_after_step",),
            (r"waiting for a device", r"`resource`", r"waited_s"),
            (r"no recovery ran",),
        ),
        first=(r"stopped_after_step", r"steps"),
        order=(r"stopped_after_step", r"start the plan again", r"planted"),
        do_not=((r"do not count the steps that never ran as passed",), (r"do not report the plan as failed",)),
        never=(r"\brecovery (ran|was attempted)\b",),
    ),
    # Since #666 the run's own call answers with the report it wrote instead of
    # letting the exception out, so the entry may no longer send a reader to a
    # protocol failure or a traceback for it, and since #667 the handle behind
    # that run names the same type the report does.
    "reactor_exception": Says(
        meaning=((r"defect",), (r"exception_type",), (r"report was written",), (r"answers with this failed report", r"handle", r"same error type")),
        first=(r"get_last_report", r"cleanup_ok"),
        order=(r"get_last_report", r"hardware_lease_status", r"report the defect"),
        do_not=((r"do not rerun the plan in a loop",), (r"do not trust an older report",)),
        never=(r"internal error", r"traceback"),
    ),
    # #667: the record is the report's own type, so the entry says what the
    # handle names instead of stating the disagreement it used to.
    "interrupted": Says(
        meaning=((r"interrupted",), (r"report was written",), (r"record", r"handle", r"interrupted")),
        first=(r"get_last_report", r"hardware_lease_status"),
        order=(r"get_last_report", r"start the plan again"),
        do_not=((r"do not read the steps that did not run as passed",), (r"do not delete lock files",)),
        never=(r"reactor_exception",),
    ),
    "junit_xml_requires_synchronous_run": Says(
        meaning=((r"detached start",), (r"no run began",)),
        first=(r"without `--detach`", r"test_reactor_status"),
        do_not=((r"do not expect the detached worker to write the file later",),),
    ),
    "junit_xml_write_failed": Says(
        meaning=((r"JSON report stands",), (r"keeps its own `error_type`", r"junit_xml_error")),
        first=(r"junit_xml_error", r"backend_error", r"--junit-xml"),
        do_not=((r"do not read the missing file as a test failure",),),
    ),
    "cleanup_exception": Says(
        meaning=((r"raised instead of answering",), (r"exception_type", r"backend_error"), (r"unconfirmed",)),
        first=(r"hardware_lease_status", r"incident_stands"),
        order=(r"hardware_lease_status", r"report the defect"),
        do_not=((r"do not delete lock files",), (r"do not rerun the plan at once",)),
        never=(r"debug_stop_session", r"com_session_stop", r"can_session_stop"),
    ),
    "cleanup_failed:test_reactor": Says(
        meaning=((r"cleanup_errors",), (r"step_error_type", r"failed_step"), (r"`recovery`",)),
        first=(r"cleanup_errors",),
        order=(r"cleanup_errors", r"hardware_lease_status", r"step_error_type"),
        do_not=((r"do not call `debug_stop_session`",), (r"do not delete coordination records or lock files",)),
        never=(r"debug_stop_session", r"com_session_stop", r"can_session_stop", r"\bretry\b"),
    ),
    "comparator_unmet": Says(
        meaning=((r"did not satisfy the comparator",), (r"not a bench fault",), (r"received_tail",), (r"frames_tail",), (r"captured_value",)),
        first=(r"comparator",),
        order=(r"comparator", r"fix the firmware or the plan"),
        do_not=((r"do not loosen the comparator",), (r"do not call it a bench fault",)),
        never=(r"\bbench fault\b",),
    ),
    "symbol_size_mismatch": Says(
        meaning=((r"different size",), (r"expected_size_bytes", r"size_bytes"), (r"not compared",)),
        first=(r"expected_size_bytes", r"size_bytes"),
        do_not=((r"do not drop `size_bytes`",),),
    ),
    "symbol_width_not_numeric": Says(
        meaning=((r"integer_widths",), (r"nothing was judged",)),
        first=(r"integer_widths",),
        do_not=((r"do not treat this as a failed assertion",),),
    ),
    "uart_expect_timeout": Says(
        meaning=((r"expected_text|expected_pattern", r"timeout_s"), (r"received_tail",), (r"bytes_received", r"reads")),
        first=(r"received_tail",),
        order=(r"received_tail", r"bytes_received"),
        do_not=((r"do not raise `timeout_s`",),),
    ),
    "unexpected_stop": Says(
        meaning=((r"not at the breakpoint",), (r"`stop`", r"expected_breakpoint_id")),
        first=(r"`stop`",),
        do_not=((r"do not add breakpoints or widen timeouts",),),
    ),
    "breakpoint_cleanup_failed": Says(
        meaning=((r"could not be cleared",), (r"not a pass",), (r"breakpoint_cleanup",)),
        first=(r"breakpoint_cleanup", r"hardware_lease_status"),
        do_not=((r"do not read the step as passed",),),
    ),
    "uart_session_not_owned": Says(
        meaning=((r"already closed, or never opened",), (r"nothing was sent",)),
        first=(r"order", r"repeat"),
        do_not=((r"do not read this as a fault of the port or the board",),),
        never=(r"com_session_stop",),
    ),
    "can_session_not_owned": Says(
        meaning=((r"already closed, or never opened",), (r"nothing was sent",)),
        first=(r"order", r"repeat"),
        do_not=((r"do not read this as a fault of the adapter",),),
        never=(r"can_session_stop",),
    ),
    "step_exception": Says(
        meaning=((r"defect",), (r"unknown",), (r"`recovery`",), (r"exception_type", r"backend_error")),
        first=(r"hardware_lease_status",),
        order=(r"hardware_lease_status", r"report the defect"),
        do_not=((r"do not count it as a firmware verdict",), (r"do not rerun the plan in a loop",)),
    ),
    "preflight_exception": Says(
        meaning=((r"before the first step", r"raised"), (r"no step ran", r"nothing was driven"), (r"validation_error",), (r"`field`", r"`\$`")),
        first=(r"report the defect",),
        do_not=((r"do not rewrite the plan",),),
    ),
    "test_config_not_found": Says(
        meaning=((r"no plan file at `path`",), (r"workspace_root",)),
        first=(r"relative to the workspace root",),
        do_not=((r"do not repoint `workspace_root`",),),
    ),
    "test_config_unreadable": Says(
        meaning=((r"exists but could not be read",), (r"backend_error",)),
        first=(r"backend_error",),
        do_not=((r"do not widen the permissions",),),
    ),
    "test_config_schema_invalid": Says(
        meaning=((r"schema bundled", r"could not be used"), (r"`schema`", r"schema_error"), (r"the installation is",)),
        first=(r"agentic-hil upgrade", r"--version"),
        do_not=((r"do not edit or loosen the plan",), (r"do not edit the schema",)),
    ),
    "audit_failed": Says(
        meaning=((r"action ran", r"evidence could not be written"), (r"audit_ok", r"false"), (r"audit_error",), (r"classify_last_error",)),
        first=(r"audit_error",),
        order=(r"audit_error", r"hardware_lease_status"),
        do_not=((r"do not run the step again",),),
        never=(r"\b(run|repeat) the step again\b",),
    ),
    "step_failed": Says(
        meaning=((r"without naming an error type",), (r"lease_state", r"cleanup_required", r"quarantined"), (r"side_effect_status", r"hardware_state"), (r"`recovery`",)),
        first=(r"failed_step",),
        order=(r"failed_step", r"hardware_lease_status"),
        do_not=((r"do not read a step's `ok: true` as a pass",),),
    ),
}


def test_every_required_entry_has_a_spec() -> None:
    assert set(SAYS) == set(REQUIRED_KEYS)


@pytest.mark.parametrize("key", REQUIRED_KEYS)
def test_each_entry_says_what_the_code_does(key: str) -> None:
    entry = catalogue_entry(key)

    assert entry is not None, f"{key} has no catalogue entry"
    assert spec_violations(key, entry) == []


def test_a_spec_refuses_generic_or_wrong_advice() -> None:
    """The specs have teeth: advice that would pass a non-empty check fails them."""
    generic = {"meaning": "The worker failed.", "remediation": ["Retry the run immediately; the retry is safe."], "do_not": ["Do not panic."]}
    settles_by_stopping = {
        **catalogue_entry_or_empty("cleanup_failed:test_reactor"),
        "remediation": ["Read `cleanup_errors`.", "Call `debug_stop_session` again until it answers; that settles the cleanup."],
    }

    assert any("outright" in problem for problem in spec_violations("run_worker_unresponsive", generic))
    assert any("debug_stop_session" in problem and "outright" in problem for problem in spec_violations("cleanup_failed:test_reactor", settles_by_stopping))


def catalogue_entry_or_empty(key: str) -> dict:
    return catalogue_entry(key) or {}


# --- what the text says about the code, checked against the code ---------------


def test_the_record_retention_the_entry_names_is_the_one_kept() -> None:
    entry = catalogue_entry_or_empty("run_not_found")

    assert f"{runlifecycle.RUN_RECORDS_KEPT} newest ended runs" in entry.get("meaning", "")


def test_the_default_plan_path_the_entry_names_is_the_default() -> None:
    entry = catalogue_entry_or_empty("test_config_not_found")

    assert f"`{DEFAULT_TEST_CONFIG_PATH}`" in " ".join([entry.get("meaning", ""), *entry.get("remediation", [])])


def test_the_integer_widths_the_entry_names_are_the_ones_compared() -> None:
    widths = sorted(INTEGER_VALUE_WIDTHS)
    spoken = ", ".join(str(width) for width in widths[:-1]) + f" or {widths[-1]} byte"

    assert spoken in catalogue_entry_or_empty("symbol_width_not_numeric").get("meaning", "")


def subcommands() -> frozenset[str]:
    parser = build_parser()
    return frozenset(name for action in parser._actions if isinstance(action, argparse._SubParsersAction) for name in action.choices)


def test_every_command_line_the_entries_print_parses() -> None:
    """A full command line parses; a bare `agentic-hil <command>` names a command that exists."""
    commands = sorted(
        {
            command
            for key in REQUIRED_KEYS
            for text in [catalogue_entry_or_empty(key).get("meaning", ""), *catalogue_entry_or_empty(key).get("remediation", [])]
            for command in re.findall(r"`(agentic-hil [^`]+)`", text)
        }
    )
    assert commands, "the entries name no command line"
    known = subcommands()
    refused = {}
    for command in commands:
        argv = [re.sub(r"^<[^>]+>$", "placeholder", token) for token in shlex.split(command)[1:]]
        if len(argv) == 1 and not argv[0].startswith("-"):
            if argv[0] not in known:
                refused[command] = "no such command"
            continue
        try:
            build_parser().parse_args(argv)
        except SystemExit as exit_:
            if exit_.code not in (0, None):
                refused[command] = exit_.code

    assert refused == {}


def test_the_run_options_the_entries_name_exist() -> None:
    build_parser().parse_args(["test-reactor", "--detach"])
    build_parser().parse_args(["test-reactor", "--junit-xml", "report.xml"])


def test_the_lease_status_fields_the_entries_send_a_reader_to_exist(tmp_path: Path) -> None:
    service = AgenticHILToolService(load_config(str(write_config(tmp_path))))
    try:
        status = service.call("hardware_lease_status")
    finally:
        service.close()

    assert {"owner_active", "device_holds", "incident_stands", "standing_incidents"} <= set(status), sorted(status)


def test_the_recovery_tool_takes_the_operators_statement(tmp_path: Path) -> None:
    service = AgenticHILToolService(load_config(str(write_config(tmp_path))))
    try:
        listed = handle_mcp_message({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, service)
    finally:
        service.close()

    assert isinstance(listed, dict)
    tools = {tool["name"]: tool for tool in listed["result"]["tools"]}
    assert "operator_statement" in tools["hardware_recover"]["inputSchema"]["properties"]


# ---------------------------------------------------------------------------
# Resolution over the reference resource.


@pytest.fixture
def reference_service(tmp_path: Path):
    service = AgenticHILToolService(load_config(str(write_config(tmp_path))))
    try:
        yield service
    finally:
        service.close()


@pytest.mark.parametrize("key", REQUIRED_KEYS)
def test_the_reference_resource_serves_each_entry(reference_service: AgenticHILToolService, key: str) -> None:
    uri = ERROR_URI_PREFIX + key
    response = handle_mcp_message({"jsonrpc": "2.0", "id": key, "method": "resources/read", "params": {"uri": uri}}, reference_service)

    assert isinstance(response, dict)
    assert "error" not in response, f"{uri} does not resolve: {response.get('error')}"
    contents = response["result"]["contents"]
    assert len(contents) == 1 and contents[0]["uri"] == uri
    entry = json.loads(contents[0]["text"])
    error_type, _, scope = key.partition(":")
    assert entry["error_type"] == error_type
    assert entry.get("scope", "") == scope
    assert entry["meaning"].strip()
    assert entry["remediation"] and entry["do_not"]
    # The fields a refusal carries are the entry's own steps, not a second text.
    assert remediation_fields(error_type, scope or None) == {"remediation": entry["remediation"], "do_not": entry["do_not"]}


# ---------------------------------------------------------------------------
# Real refusals carry their entry.


def assert_carries_advice(payload: dict, key: str) -> None:
    """The payload names the key's type, the key has an entry, and the entry's fix is on it."""
    error_type, _, scope = key.partition(":")
    assert payload.get("error_type") == error_type, payload
    catalogued = key in ERROR_CATALOGUE
    assert catalogued, f"{key} reached a caller with no catalogue entry: {payload.get('summary')!r}"
    expected = remediation_fields(error_type, scope or None)
    assert expected.get("remediation"), key
    assert payload.get("remediation") == expected["remediation"], payload
    assert payload.get("do_not") == expected.get("do_not"), payload


def assert_refusal_carries_its_entry(payload: dict, key: str) -> None:
    assert payload.get("ok") is False, payload
    assert_carries_advice(payload, key)


def nested_with_type(payload: object, error_type: str, *, top: bool = True) -> list[dict]:
    """Every dictionary inside the payload, not the payload itself, naming that type."""
    found: list[dict] = []
    if isinstance(payload, dict):
        if not top and payload.get("error_type") == error_type:
            found.append(payload)
        for value in payload.values():
            found.extend(nested_with_type(value, error_type, top=False))
    elif isinstance(payload, list):
        for value in payload:
            found.extend(nested_with_type(value, error_type, top=False))
    return found


# --- test_reactor_status and test_reactor_stop over MCP ---------------------

UNKNOWN_HANDLE = "run-00000000000000ff"


def test_status_of_a_handle_the_bench_never_saw(tmp_path: Path) -> None:
    service = AgenticHILToolService(load_config(str(write_config(tmp_path))))
    try:
        result = service.call("test_reactor_status", {"run": UNKNOWN_HANDLE})
    finally:
        service.close()

    assert result.get("run") == UNKNOWN_HANDLE, result
    assert_refusal_carries_its_entry(result, "run_not_found")


def unreadable_record(tmp_path: Path):
    config = load_config(str(write_config(tmp_path)))
    handle = runlifecycle.new_run_handle()
    path = runlifecycle.record_path(config, handle)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"version": 999}\n', encoding="utf-8")
    return config, handle


def test_status_of_a_record_this_version_cannot_read(tmp_path: Path) -> None:
    config, handle = unreadable_record(tmp_path)
    service = AgenticHILToolService(config)
    try:
        result = service.call("test_reactor_status", {"run": handle})
    finally:
        service.close()

    assert_refusal_carries_its_entry(result, "run_state_invalid")


def test_stop_of_a_record_this_version_cannot_read(tmp_path: Path) -> None:
    config, handle = unreadable_record(tmp_path)
    service = AgenticHILToolService(config)
    try:
        result = service.call("test_reactor_stop", {"run": handle})
    finally:
        service.close()

    assert_refusal_carries_its_entry(result, "run_state_invalid")


def test_a_listing_of_runs_that_could_not_be_taken(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def unlistable(_directory):
        raise PermissionError("runs directory denied")

    monkeypatch.setattr(runlifecycle, "_records_newest_first", unlistable)
    service = AgenticHILToolService(load_config(str(write_config(tmp_path))))
    try:
        result = service.call("test_reactor_status", {})
    finally:
        service.close()

    assert "backend_error" in result, result
    assert_refusal_carries_its_entry(result, "run_state_invalid")


def test_stop_of_a_handle_the_bench_never_saw(tmp_path: Path) -> None:
    service = AgenticHILToolService(load_config(str(write_config(tmp_path))))
    try:
        result = service.call("test_reactor_stop", {"run": UNKNOWN_HANDLE})
    finally:
        service.close()

    assert_refusal_carries_its_entry(result, "run_not_found")


def test_stop_of_a_run_whose_worker_is_gone(tmp_path: Path) -> None:
    # A running record nobody holds the lock of: the worker ended without
    # finishing, which is what a killed worker leaves.
    config = load_config(str(write_config(tmp_path)))
    handle = runlifecycle.new_run_handle()
    runlifecycle.write_run_record(config, handle, {"version": runlifecycle.RUN_RECORD_VERSION, "state": runlifecycle.RUN_RUNNING, "run": handle, "run_ok": None})
    service = AgenticHILToolService(config)
    try:
        result = service.call("test_reactor_stop", {"run": handle})
    finally:
        service.close()

    assert_refusal_carries_its_entry(result, "run_worker_gone")


# --- test_reactor_run over MCP ------------------------------------------------

RESET_PLAN = "version: 4\nsteps:\n  - {device: dut, action: reset}\n"


def test_run_of_a_plan_that_is_not_there(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace, _ = bench_workspace(tmp_path, monkeypatch, RESET_PLAN)
    service = AgenticHILToolService(load_authoritative_config(workspace))
    try:
        result = service.call("test_reactor_run", {"test_config_path": "plans/absent.yaml"})
    finally:
        service.close()

    assert_refusal_carries_its_entry(result, "test_config_not_found")


def test_run_of_a_plan_that_cannot_be_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace, plan = bench_workspace(tmp_path, monkeypatch, RESET_PLAN)

    def unreadable(*_args, **_kwargs):
        raise PermissionError("plan denied")

    monkeypatch.setattr("agentic_hil.test_reactor.safe_read_bytes", unreadable)
    service = AgenticHILToolService(load_authoritative_config(workspace))
    try:
        result = service.call("test_reactor_run", {"test_config_path": str(plan)})
    finally:
        service.close()

    assert_refusal_carries_its_entry(result, "test_config_unreadable")


def test_run_against_a_bundled_schema_that_will_not_load(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace, plan = bench_workspace(tmp_path, monkeypatch, RESET_PLAN)
    # A schema whose own `type` is not a type name, which the metaschema refuses.
    monkeypatch.setattr("agentic_hil.test_reactor.test_config_schema", lambda: {"type": 12})
    service = AgenticHILToolService(load_authoritative_config(workspace))
    try:
        result = service.call("test_reactor_run", {"test_config_path": str(plan)})
    finally:
        service.close()

    assert result.get("schema") and result.get("schema_error"), result
    assert_refusal_carries_its_entry(result, "test_config_schema_invalid")


def test_detached_start_whose_worker_dies_first(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace, plan = bench_workspace(tmp_path, monkeypatch, RESET_PLAN)

    def dying_worker(inner_config, handle: str, test_config_path: str, *, wait_s: float) -> subprocess.Popen:
        with open(runlifecycle.runs_directory(inner_config) / f"{handle}.log", "ab") as log:
            return subprocess.Popen(
                [sys.executable, "-c", "import sys; print('boom', file=sys.stderr); sys.exit(3)"],
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                cwd=str(inner_config.work_dir),
            )

    monkeypatch.setattr(runlifecycle, "spawn_run_worker", dying_worker)
    service = AgenticHILToolService(load_authoritative_config(workspace))
    try:
        result = service.call("test_reactor_run", {"test_config_path": str(plan), "detach": True})
    finally:
        service.close()

    assert result.get("exit_code") == 3 and "boom" in str(result.get("worker_output")), result
    assert_refusal_carries_its_entry(result, "run_worker_failed")


class WorkerThatNeverPublishes:
    """A spawned worker that stays alive and never writes its record."""

    def poll(self) -> None:
        return None


def test_detached_start_whose_worker_never_says_anything(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace, plan = bench_workspace(tmp_path, monkeypatch, RESET_PLAN)
    monkeypatch.setattr(runlifecycle, "WORKER_PUBLISH_TIMEOUT_S", 0.3)
    monkeypatch.setattr(runlifecycle, "spawn_run_worker", lambda *_args, **_kwargs: WorkerThatNeverPublishes())
    config = load_authoritative_config(workspace)
    service = AgenticHILToolService(config)
    try:
        result = service.call("test_reactor_run", {"test_config_path": str(plan), "detach": True})
    finally:
        service.close()

    # What the entry tells a reader not to undo, and why it says no retry is safe.
    assert result.get("retry_safe") is False, result
    assert runlifecycle.stop_path(config, str(result.get("run"))).exists(), result
    assert_refusal_carries_its_entry(result, "run_worker_unresponsive")


# --- a detached run that ended before its start returned ----------------------


class EndedWorker:
    """A worker that already ran its plan to the end, in this process."""

    def poll(self) -> int:
        return 0


def worker_run_in_this_process(*, stop_first: bool):
    def spawn(inner_config, handle: str, test_config_path: str, *, wait_s: float) -> EndedWorker:
        if stop_first:
            # The stop a start plants under a handle before its worker registers,
            # read by the run before it takes anything.
            runlifecycle._plant_stop_after_unresponsive(inner_config, handle)
        reactorrun.run_plan(inner_config, test_config_path, wait_s=wait_s, run_handle=handle)
        return EndedWorker()

    return spawn


def start_status_and_stop(service: AgenticHILToolService, plan: Path) -> tuple[dict, dict, dict]:
    started = service.call("test_reactor_run", {"test_config_path": str(plan), "detach": True})
    handle = started.get("run")
    assert isinstance(handle, str), started
    return started, service.call("test_reactor_status", {"run": handle}), service.call("test_reactor_stop", {"run": handle})


def test_a_detached_run_refused_the_bench_answers_with_its_advice_everywhere(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace, plan = bench_workspace(tmp_path, monkeypatch, RESET_PLAN)
    config = load_authoritative_config(workspace)
    monkeypatch.setattr(runlifecycle, "spawn_run_worker", worker_run_in_this_process(stop_first=False))
    stranger = BenchMutex(frontend="stranger", label="other-bench-session")
    stranger.acquire(declared_devices(config, load_test_config(str(plan), config.work_dir)))
    service = AgenticHILToolService(config)
    try:
        started, status, stop = start_status_and_stop(service, plan)
    finally:
        service.close()
        stranger.release_all()

    report = json.loads((workspace / ".agentic-hil" / "reports" / "last-report.json").read_text(encoding="utf-8"))
    assert_refusal_carries_its_entry(report, "device_busy")
    assert started.get("state") == "finished", started
    assert_refusal_carries_its_entry(started, "device_busy")
    # Status and stop answer the question they were asked, so `ok` is theirs;
    # the run's failure and its advice travel beside it.
    assert status.get("ok") is True and status.get("run_ok") is False, status
    assert_carries_advice(status, "device_busy")
    assert stop.get("ok") is True and stop.get("stop_requested") is False, stop
    assert_carries_advice(stop, "device_busy")


def test_a_detached_run_stopped_before_its_first_step_answers_with_its_advice_everywhere(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace, plan = bench_workspace(tmp_path, monkeypatch, RESET_PLAN)
    config = load_authoritative_config(workspace)
    monkeypatch.setattr(runlifecycle, "spawn_run_worker", worker_run_in_this_process(stop_first=True))
    service = AgenticHILToolService(config)
    try:
        started, status, stop = start_status_and_stop(service, plan)
    finally:
        service.close()

    assert started.get("state") == "stopped" and started.get("stopped_after_step") == 0, started
    assert_refusal_carries_its_entry(started, "run_stopped")
    assert status.get("ok") is True and status.get("state") == "stopped", status
    assert_carries_advice(status, "run_stopped")
    assert stop.get("ok") is True and stop.get("stop_requested") is False, stop
    assert_carries_advice(stop, "run_stopped")


# --- a run whose reactor raised -----------------------------------------------


def last_report(workspace: Path) -> dict:
    return json.loads((workspace / ".agentic-hil" / "reports" / "last-report.json").read_text(encoding="utf-8"))


def test_a_run_whose_reactor_raised_leaves_its_advice_in_the_report_and_the_status(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace, plan = bench_workspace(tmp_path, monkeypatch, RESET_PLAN)
    config = load_authoritative_config(workspace)

    def raising_run(self, test_config):
        raise RuntimeError("reactor broke")

    monkeypatch.setattr(TestReactor, "run", raising_run)
    handle = runlifecycle.new_run_handle()
    with pytest.raises(RuntimeError, match="reactor broke"):
        reactorrun.run_plan(config, str(plan), run_handle=handle)
    service = AgenticHILToolService(config)
    try:
        status = service.call("test_reactor_status", {"run": handle})
    finally:
        service.close()

    assert_refusal_carries_its_entry(last_report(workspace), "reactor_exception")
    assert status.get("ok") is True and status.get("run_ok") is False, status
    assert_carries_advice(status, "reactor_exception")


def test_an_interrupted_run_leaves_its_advice_in_the_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace, plan = bench_workspace(tmp_path, monkeypatch, RESET_PLAN)
    config = load_authoritative_config(workspace)

    def interrupted_run(self, test_config):
        raise KeyboardInterrupt

    monkeypatch.setattr(TestReactor, "run", interrupted_run)
    handle = runlifecycle.new_run_handle()
    with pytest.raises(KeyboardInterrupt):
        reactorrun.run_plan(config, str(plan), run_handle=handle)

    assert_refusal_carries_its_entry(last_report(workspace), "interrupted")
    # What the entry tells a reader about the record behind the handle: since
    # #667 the record is written from the report, so an interrupted run is
    # `interrupted` in both and the status carries that entry's advice rather
    # than the one that calls a Ctrl+C a defect in Agentic HIL.
    status = runlifecycle.run_status(config, handle)
    assert status.get("error_type") == "interrupted", status
    assert_carries_advice(status, "interrupted")


def test_run_whose_reactor_close_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace, plan = bench_workspace(tmp_path, monkeypatch, RESET_PLAN)

    class ReactorThatWillNotClose(TestReactor):
        def close(self) -> None:
            super().close()
            raise OSError("per-probe service would not close")

    monkeypatch.setattr("agentic_hil.reactorrun.TestReactor", ReactorThatWillNotClose)
    service = AgenticHILToolService(load_authoritative_config(workspace))
    try:
        result = service.call("test_reactor_run", {"test_config_path": str(plan)})
    finally:
        service.close()

    closes = [item["result"] for item in result.get("cleanup_errors", [])]
    assert closes and closes[-1].get("error_type") == "cleanup_exception", result
    for close in closes:
        assert_refusal_carries_its_entry(close, "cleanup_exception")
    assert_refusal_carries_its_entry(result, "cleanup_failed:test_reactor")


# --- the rule a run result looks its advice up by -----------------------------

PASSED_UP = "passed_up_by_a_step"


@pytest.fixture
def passed_up_entry(monkeypatch: pytest.MonkeyPatch) -> ErrorRemedy:
    """An entry for a type this area does not own, as a debug session would write one."""
    remedy = ErrorRemedy(meaning="A type a step passed up.", remediation=("Read the step that passed it up.",), do_not=("Do not guess at it.",))
    monkeypatch.setitem(ERROR_CATALOGUE, PASSED_UP, remedy)
    return remedy


DEBUG_START_PLAN = "version: 2\nsteps:\n  - {debugger: dut, action: debug_start, image_path: build/app.elf}\n"


class StartsOnAStoppedTarget(RecordingService):
    def call(self, name: str, arguments: dict | None = None) -> dict:
        result = super().call(name, arguments)
        if name == "debug_start_session":
            return {"ok": True, "tool": name, "target_ok": False, "target_error_type": PASSED_UP}
        return result


@pytest.mark.parametrize(
    ("plan", "service"),
    [
        pytest.param(DEBUG_START_PLAN, StartsOnAStoppedTarget, id="debug-start-target-type"),
        pytest.param(RESET_PLAN, lambda: RecordingService(reset_result={"ok": True, "tool": "reset_target", "target_ok": False, "target_error_type": PASSED_UP}), id="step-target-type"),
        pytest.param(RESET_PLAN, lambda: RecordingService(reset_result={"ok": False, "tool": "reset_target", "error_type": PASSED_UP}), id="step-own-type"),
    ],
)
def test_a_failed_run_carries_the_advice_of_whatever_type_it_names(tmp_path: Path, passed_up_entry: ErrorRemedy, plan: str, service) -> None:
    result = run_plan_text(load_config(str(write_config(tmp_path))), tmp_path, plan, service())

    assert result.get("ok") is False, result
    assert result.get("error_type") == PASSED_UP, result
    assert result.get("remediation") == list(passed_up_entry.remediation), result
    assert result.get("do_not") == list(passed_up_entry.do_not), result


def test_a_run_refused_with_scoped_advice_keeps_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace, plan = bench_workspace(tmp_path, monkeypatch, RESET_PLAN)
    config = load_authoritative_config(workspace)
    scoped = remediation_fields("resource_quarantined", "foreign_project")
    assert scoped and scoped != remediation_fields("resource_quarantined")

    def refused(self, *_args, **_kwargs):
        raise CoordinationError({"ok": False, "error_type": "resource_quarantined", "summary": "Another project's incident stands on this device.", **scoped})

    monkeypatch.setattr(HardwareCoordinator, "begin_run", refused)
    result = reactorrun.run_plan(config, str(plan))

    assert result.get("error_type") == "resource_quarantined", result
    assert {key: result.get(key) for key in scoped} == scoped


# --- hardware_recover over MCP -------------------------------------------------


def standing_incident(config, resource: str) -> str:
    """A standing incident left behind by an owner that is gone.

    The resource name is this module's own: the device locks are machine-wide,
    and a name a sibling checkout's suite also uses would make the two wait on
    each other."""
    owner = HardwareCoordinator(config, "ec3-incident-setup")
    try:
        lease = owner.acquire(resource)
        lease.quarantine(LEASE_RELEASE_RETRY_REASON, audit_broken=True)
        incident = owner.quarantine_id
    finally:
        owner.close()
    assert isinstance(incident, str)
    return incident


def test_recover_whose_ledger_line_cannot_be_written(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from agentic_hil import coordination

    config = config_for(tmp_path)
    incident = standing_incident(config, "physical:ec3-catalogue-audit")
    original = coordination.safe_append_text

    def ledger_refuses_the_recovery(path, text, *args, **kwargs):
        if "mcp:hardware_recover" in str(text):
            raise OSError("ledger denied")
        return original(path, text, *args, **kwargs)

    monkeypatch.setattr("agentic_hil.coordination.safe_append_text", ledger_refuses_the_recovery)
    service = AgenticHILToolService(config)
    try:
        result = service.call("hardware_recover", {})
        after = service.call("hardware_lease_status")
    finally:
        service.close()

    assert "backend_error" in result, result
    assert_refusal_carries_its_entry(result, "recovery_audit_failed")
    # What the entry promises: nothing was cleared, the same incident stands.
    assert after.get("incident_stands") is True and after.get("quarantine_id") == incident, after


def test_recover_whose_released_markers_cannot_be_written(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = config_for(tmp_path)
    standing_incident(config, "physical:ec3-catalogue-persist")
    service = AgenticHILToolService(config)
    try:
        original = service.coordinator._write_record

        def refuse_the_pending_record(resource: str, record: dict) -> None:
            if record.get("state") == "recovery_pending":
                raise OSError("injected write fault")
            original(resource, record)

        monkeypatch.setattr(service.coordinator, "_write_record", refuse_the_pending_record)
        result = service.call("hardware_recover", {})
    finally:
        service.close()

    assert "backend_error" in result, result
    assert_refusal_carries_its_entry(result, "recovery_persist_failed")


def test_recover_whose_resource_marker_changed(tmp_path: Path) -> None:
    config = config_for(tmp_path)
    resource = "physical:ec3-catalogue-marker"
    standing_incident(config, resource)
    doctor = HardwareCoordinator(config, "ec3-marker-doctor")
    try:
        marker = doctor._read_record(resource)
        assert marker is not None
        doctor._write_record(resource, {**marker, "resources": []})
    finally:
        doctor.close()
    service = AgenticHILToolService(config)
    try:
        result = service.call("hardware_recover", {})
    finally:
        service.close()

    assert result.get("resource") == resource, result
    assert_refusal_carries_its_entry(result, "quarantine_changed")


# --- resource_busy, both holders ----------------------------------------------


def test_probe_while_another_owner_holds_the_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = load_config(str(write_config(tmp_path)))
    owner = HardwareCoordinator(config, "ec3-discovery-owner")
    service = AgenticHILToolService(config)
    try:
        gate = owner.acquire(DEBUGGER_DISCOVERY_RESOURCE)
        monkeypatch.setattr(service.backend, "probe_target", lambda: {"ok": True})
        result = service.call("probe_target")
        gate.release()
    finally:
        service.close()
        owner.close()

    # Another owner's lock: the refusal says what it asked for.
    assert "resources" in result, result
    assert_refusal_carries_its_entry(result, "resource_busy")


def test_a_debugger_tool_while_this_servers_own_session_holds_the_debugger(tmp_path: Path) -> None:
    service = debug_service(tmp_path)
    try:
        assert start_debug_session(service, mode="attach")["ok"] is True
        result = service.call("reset_target", {"mode": "halt"})
        assert service.call("debug_stop_session")["ok"] is True
    finally:
        service.close()

    # This server's own session: no `resources`, which is the case the entry's
    # `debug_stop_session` step is for.
    assert "resources" not in result, result
    assert_refusal_carries_its_entry(result, "resource_busy")


def test_recover_on_a_bench_with_nothing_quarantined(tmp_path: Path) -> None:
    # The tool and the command ask lease-status first and answer
    # nothing_to_recover; this is the coordinator's answer to the recovery that
    # loses the race to a parallel one between that read and its own.
    coordinator = HardwareCoordinator(load_config(str(write_config(tmp_path))), "ec3-recovery")
    try:
        result = coordinator.recover(safe_state_confirmed=True, quarantine_id="incident")
    finally:
        coordinator.close()

    assert_refusal_carries_its_entry(result, "resource_not_quarantined")


# --- the command line -------------------------------------------------------------


def cli_recover(argv: list[str]) -> dict:
    result = dispatch(build_parser().parse_args(["recover", *argv]))
    assert isinstance(result, dict), result
    return result


def command_line_bench(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    workspace = tmp_path / "project"
    write_authoritative_config(workspace, monkeypatch)
    monkeypatch.chdir(workspace)
    return load_authoritative_config(workspace)


def test_recover_command_with_an_empty_incident_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = command_line_bench(tmp_path, monkeypatch)
    standing_incident(config, "physical:ec3-catalogue-cli-id")

    result = cli_recover(["--confirm-safe-state", "--quarantine-id", ""])

    assert_refusal_carries_its_entry(result, "quarantine_id_required")


def test_recover_command_naming_another_incident(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = command_line_bench(tmp_path, monkeypatch)
    standing_incident(config, "physical:ec3-catalogue-cli-wrong")

    result = cli_recover(["--confirm-safe-state", "--quarantine-id", "not-the-incident"])

    assert "resource" not in result, result
    assert_refusal_carries_its_entry(result, "quarantine_changed")


def test_recover_command_while_a_live_owner_holds_the_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = command_line_bench(tmp_path, monkeypatch)
    owner = HardwareCoordinator(config, "ec3-live-owner")
    try:
        lease = owner.acquire("physical:ec3-catalogue-cli-busy")
        lease.quarantine(LEASE_RELEASE_RETRY_REASON, audit_broken=True)
        incident = str(owner.status()["quarantine_id"])
        result = cli_recover(["--confirm-safe-state", "--quarantine-id", incident])
    finally:
        owner.close()

    assert "resources" in result, result
    assert_refusal_carries_its_entry(result, "resource_busy")


def test_recover_command_over_inconsistent_markers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = command_line_bench(tmp_path, monkeypatch)
    resource = "physical:ec3-catalogue-cli-markers"
    incident = standing_incident(config, resource)
    doctor = HardwareCoordinator(config, "ec3-record-doctor")
    try:
        record = doctor._read_record(doctor.project_key)
        assert record is not None
        doctor._write_record(doctor.project_key, {**record, "resources": [resource, resource]})
    finally:
        doctor.close()

    result = cli_recover(["--confirm-safe-state", "--quarantine-id", incident])

    assert_refusal_carries_its_entry(result, "coordination_state_invalid")


def test_lease_status_command_over_a_record_it_cannot_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = load_config(str(write_config(tmp_path)))
    coordinator = HardwareCoordinator(config, "ec3-setup")
    try:
        coordinator._record_path(coordinator.project_key).write_text('{"version": 999}\n', encoding="utf-8")
    finally:
        coordinator.close()
    monkeypatch.setattr("agentic_hil.cli.load_cli_authoritative_config", lambda path: config)

    exit_code = entrypoint(["lease-status", "--json"])

    assert exit_code == 1
    result = json.loads(capsys.readouterr().out)
    assert result.get("resource"), result
    assert_refusal_carries_its_entry(result, "coordination_state_invalid")


def damaged_project_record(tmp_path: Path):
    config = load_config(str(write_config(tmp_path)))
    coordinator = HardwareCoordinator(config, "setup")
    try:
        coordinator._record_path(coordinator.project_key).write_text('{"version": 999}\n', encoding="utf-8")
    finally:
        coordinator.close()
    return config


@pytest.mark.parametrize("tool", ["hardware_lease_status", "hardware_recover"])
def test_the_lease_tools_answer_a_damaged_record_with_its_refusal(tmp_path: Path, tool: str) -> None:
    """The two tools that are the way out of a broken bench answer the record's
    own refusal, not a protocol error that cannot be told from a broken server."""
    service = AgenticHILToolService(damaged_project_record(tmp_path))
    try:
        response = handle_mcp_message({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool, "arguments": {}}}, service)
    finally:
        service.close()

    assert isinstance(response, dict)
    assert "error" not in response, response
    result = response["result"]["structuredContent"]
    assert result.get("resource"), result
    assert_refusal_carries_its_entry(result, "coordination_state_invalid")


def test_recover_whose_resource_lock_cannot_be_taken(tmp_path: Path) -> None:
    """A resource lock recovery cannot take is answered the way the project lock
    a few lines above it is: as the lock's own refusal, and nothing cleared."""
    config = config_for(tmp_path)
    resource = "physical:ec3-catalogue-resource-lock"
    incident = standing_incident(config, resource)
    service = AgenticHILToolService(config)
    try:
        original = service.coordinator._acquire_lock

        def resource_lock_held_elsewhere(name: str, requested: list[str]):
            if name == resource:
                raise CoordinationError({"ok": False, "error_type": "resource_busy", "summary": "held", "resources": requested, "retry_safe": True, **remediation_fields("resource_busy")})
            return original(name, requested)

        service.coordinator._acquire_lock = resource_lock_held_elsewhere
        response = handle_mcp_message({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "hardware_recover", "arguments": {}}}, service)
        service.coordinator._acquire_lock = original
        after = service.call("hardware_lease_status")
    finally:
        service.close()

    assert isinstance(response, dict)
    assert "error" not in response, response
    result = response["result"]["structuredContent"]
    assert result.get("resources"), result
    assert_refusal_carries_its_entry(result, "resource_busy")
    assert after.get("incident_stands") is True and after.get("quarantine_id") == incident, after


def write_plan(workspace: Path) -> Path:
    path = workspace / ".agentic-hil" / "testconfig.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("version: 2\nsteps:\n  - {debugger: dut, action: reset}\n", encoding="utf-8")
    return path


def test_junit_beside_a_detached_start(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    write_authoritative_config(tmp_path, monkeypatch)
    plan = write_plan(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("agentic_hil.cli.start_plan_detached", lambda *_args, **_kwargs: {"ok": True, "run": "run-1234"})

    exit_code = entrypoint(["test-reactor", "--detach", "--test-config", str(plan), "--junit-xml", str(tmp_path / "junit.xml"), "--json"])

    assert exit_code == 1
    assert_refusal_carries_its_entry(json.loads(capsys.readouterr().out), "junit_xml_requires_synchronous_run")


def junit_run_with(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], run_result: dict) -> dict:
    write_authoritative_config(tmp_path, monkeypatch)
    plan = write_plan(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("agentic_hil.reactorrun.run_registered_plan", lambda *_args, **_kwargs: run_result)

    def refusing_write(*_args, **_kwargs) -> str:
        raise OSError("read-only file system")

    monkeypatch.setattr("agentic_hil.junit.write_junit_xml", refusing_write)

    exit_code = entrypoint(["test-reactor", "--test-config", str(plan), "--junit-xml", str(tmp_path / "junit.xml"), "--json"])

    assert exit_code == 1
    return json.loads(capsys.readouterr().out)


def test_junit_file_that_cannot_be_written(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    passed = {"ok": True, "tool": "test_reactor", "name": "testconfig", "cleanup_ok": True, "steps": []}

    result = junit_run_with(tmp_path, monkeypatch, capsys, passed)

    assert_refusal_carries_its_entry(result, "junit_xml_write_failed")
    assert_carries_advice(result.get("junit_xml_error") or {}, "junit_xml_write_failed")


def test_junit_file_that_cannot_be_written_after_a_failed_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    verdict = tmp_path / "verdict"
    verdict.mkdir()
    failed = run_plan_text(
        uart_config(verdict),
        verdict,
        'version: 3\nsteps:\n  - {device: dut_uart, action: uart_open}\n  - {device: dut_uart, action: uart_read, comparator: {equals: "Hello World"}, timeout_s: 0.05}\n',
        RecordingService(uart_reads=[b"Hello World and then some\r\n"]),
    )

    result = junit_run_with(tmp_path / "bench", monkeypatch, capsys, failed)

    # The run's own failure keeps its type and its own advice; the write
    # failure is beside it with its own.
    assert_refusal_carries_its_entry(result, "comparator_unmet")
    assert_carries_advice(result.get("junit_xml_error") or {}, "junit_xml_write_failed")


# --- the reactor's own refusals ---------------------------------------------------


def run_plan_text(config, tmp_path: Path, text: str, service, **reactor_kwargs) -> dict:
    path = write_test_config(tmp_path, text)
    return TestReactor(config, service, **reactor_kwargs).run(load_test_config(str(path), str(tmp_path)))  # type: ignore[arg-type]


def test_a_uart_read_whose_comparator_never_held(tmp_path: Path) -> None:
    result = run_plan_text(
        uart_config(tmp_path),
        tmp_path,
        'version: 3\nsteps:\n  - {device: dut_uart, action: uart_open}\n  - {device: dut_uart, action: uart_read, comparator: {equals: "Hello World"}, timeout_s: 0.05}\n',
        RecordingService(uart_reads=[b"Hello World and then some\r\n"]),
    )

    assert_refusal_carries_its_entry(result, "comparator_unmet")


def test_a_uart_expect_that_timed_out(tmp_path: Path) -> None:
    result = run_plan_text(
        load_config(str(write_config(tmp_path, com_ports_yaml='com_ports:\n  dut_uart:\n    device: "COM_TEST"\n'))),
        tmp_path,
        'version: 2\nsteps:\n  - {port_id: dut_uart, action: uart_open}\n  - {port_id: dut_uart, action: uart_expect, text: "Hello World", timeout_s: 0.2}\n  - {port_id: dut_uart, action: uart_close}\n',
        RecordingService(uart_reads=[b"boot: stage 1\r\n", b"HardFault at 0x08000abc\r\n"]),
    )

    assert_refusal_carries_its_entry(result, "uart_expect_timeout")


def test_a_symbol_read_at_the_wrong_size(tmp_path: Path) -> None:
    path = symbol_plan(tmp_path, "{device: dut, action: read_symbol, symbol: boot_counter, size_bytes: 4, comparator: {equals: 7}}")

    result = run_symbol_plan(tmp_path, path, SymbolService(b"\x07\x00"))

    assert_refusal_carries_its_entry(result, "symbol_size_mismatch")


def test_a_symbol_too_wide_to_compare_as_a_number(tmp_path: Path) -> None:
    path = symbol_plan(tmp_path, "{device: dut, action: read_symbol, symbol: CTC_array, comparator: {equals: 1}}")

    result = run_symbol_plan(tmp_path, path, SymbolService(bytes(408)))

    assert_refusal_carries_its_entry(result, "symbol_width_not_numeric")


BREAKPOINT_PLAN = (
    "version: 2\nsteps:\n"
    "  - {debugger: dut, action: debug_start, image_path: build/app.elf}\n"
    "  - {debugger: dut, action: run_until_breakpoint, location: test_done}\n"
)


def test_a_target_that_stopped_somewhere_else(tmp_path: Path) -> None:
    class StopsElsewhere(RecordingService):
        def call(self, name: str, arguments: dict | None = None) -> dict:
            result = super().call(name, arguments)
            if name == "debug_continue":
                return {"ok": True, "stop_reason": "halted", "stop": {}}
            return result

    result = run_plan_text(load_config(str(write_config(tmp_path))), tmp_path, BREAKPOINT_PLAN, StopsElsewhere())

    assert_refusal_carries_its_entry(result, "unexpected_stop")


def test_a_breakpoint_that_would_not_come_out(tmp_path: Path) -> None:
    class KeepsItsBreakpoint(RecordingService):
        def call(self, name: str, arguments: dict | None = None) -> dict:
            result = super().call(name, arguments)
            if name == "debug_clear_breakpoints":
                return {"ok": False, "tool": name}
            return result

    result = run_plan_text(load_config(str(write_config(tmp_path))), tmp_path, BREAKPOINT_PLAN, KeepsItsBreakpoint())

    assert_refusal_carries_its_entry(result, "breakpoint_cleanup_failed")


def test_a_plan_closing_a_uart_session_it_already_closed(tmp_path: Path) -> None:
    result = run_plan_text(
        uart_config(tmp_path),
        tmp_path,
        "version: 4\nsteps:\n  - {device: dut_uart, action: uart_open}\n  - {action: repeat, count: 2, steps: [{device: dut_uart, action: uart_close}]}\n",
        RecordingService(),
    )

    assert_refusal_carries_its_entry(result, "uart_session_not_owned")


def test_a_plan_closing_a_can_session_it_already_closed(tmp_path: Path) -> None:
    result = run_plan_text(
        can_config(tmp_path),
        tmp_path,
        "version: 4\nsteps:\n  - {device: dut_can, action: can_open}\n  - {action: repeat, count: 2, steps: [{device: dut_can, action: can_close}]}\n",
        RecordingService(),
    )

    assert_refusal_carries_its_entry(result, "can_session_not_owned")


def test_a_step_that_raised(tmp_path: Path) -> None:
    result = run_plan_text(
        load_config(str(write_config(tmp_path))),
        tmp_path,
        "version: 2\nsteps:\n  - {debugger: dut, action: flash, image_path: build/app.elf}\n",
        RecordingService(raise_flash=True),
    )

    assert_refusal_carries_its_entry(result, "step_exception")
    nested = nested_with_type(result, "step_exception")
    assert nested, result
    for step in nested:
        assert_refusal_carries_its_entry(step, "step_exception")


def test_a_preflight_that_raised(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def raising_preflight(self, test_config):
        raise RuntimeError("preflight broke")

    monkeypatch.setattr(TestReactor, "preflight", raising_preflight)

    result = run_plan_text(load_config(str(write_config(tmp_path))), tmp_path, RESET_PLAN, RecordingService())

    assert_refusal_carries_its_entry(result, "preflight_exception")
    assert_refusal_carries_its_entry(result.get("validation_error") or {}, "preflight_exception")


def test_a_run_asked_to_stop(tmp_path: Path) -> None:
    result = run_plan_text(load_config(str(write_config(tmp_path))), tmp_path, RESET_PLAN, RecordingService(), stop_requested=lambda: True)

    assert result.get("stopped") is True and "stopped_after_step" in result, result
    assert "recovery" not in result, result
    assert_refusal_carries_its_entry(result, "run_stopped")


def test_a_step_whose_evidence_could_not_be_written(tmp_path: Path) -> None:
    result = run_plan_text(
        load_config(str(write_config(tmp_path))),
        tmp_path,
        "version: 2\nsteps:\n  - {debugger: dut, action: flash, image_path: build/app.elf}\n",
        RecordingService(audit_flash_failure=True),
    )

    assert_refusal_carries_its_entry(result, "audit_failed")


class ResettingBackend:
    """A probe that answers a reset and nothing more."""

    def probe_target(self) -> dict:
        return {"ok": True, "tool": "probe_target", "target_detected": True}

    def reset_target(self, mode: str = "run") -> dict:
        return {"ok": True, "tool": "reset_target", "mode": mode}

    def close(self) -> None:
        return None

    def sessionless_debug_tools(self) -> frozenset:
        return frozenset()


def test_a_step_that_left_its_lease_needing_cleanup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The real service, whose reset worked and whose lease could not be given
    # back: `ok` true, the lease's own state merged in, and no error_type of its
    # own, which the run names step_failed.
    config = load_config(str(write_config(tmp_path)))
    service = AgenticHILToolService(config, backend=ResettingBackend())  # type: ignore[arg-type]
    original = service.coordinator._persist_lease

    def release_cannot_be_written(lease, state=None, incident_override=None):
        if state == "released":
            raise OSError("injected release fault")
        return original(lease, state=state, incident_override=incident_override)

    monkeypatch.setattr(service.coordinator, "_persist_lease", release_cannot_be_written)
    try:
        result = run_plan_text(config, tmp_path, RESET_PLAN, service)
    finally:
        service.close()

    reset = result["steps"][0]["result"]
    assert reset.get("ok") is True and reset.get("cleanup_required") is True and "error_type" not in reset, reset
    assert_refusal_carries_its_entry(result, "step_failed")


def test_a_run_whose_device_cleanup_failed(tmp_path: Path) -> None:
    result = run_plan_text(
        load_config(str(write_config(tmp_path))),
        tmp_path,
        "version: 2\nsteps:\n  - {debugger: dut, action: debug_start, image_path: build/app.elf}\n",
        RecordingService(fail_cleanup=True),
    )

    closes = [item["result"] for item in result.get("cleanup_errors", [])]
    assert closes and closes[0].get("error_type") == "cleanup_exception", result
    for close in closes:
        assert_refusal_carries_its_entry(close, "cleanup_exception")
    assert "recovery" in result, result
    assert_refusal_carries_its_entry(result, "cleanup_failed:test_reactor")
