"""Catalogue entries for artifact, report, adoption, configuration-write and dispatcher refusals (#645).

Four things are pinned here.

The inventory. Every `error_type` value the scanned modules can put into a
result is found by an AST scan of their source and compared with the inventory
written down below, so a refusal added to one of them cannot ship without being
classified, and the scan cannot quietly stop finding values either. The scan
reads every way this code writes the key (a dict literal, a keyword, a helper's
positional argument, a subscript assignment, a keyed setter such as
`setdefault`, a function default) and every way a value reaches it (a literal, a
constant, a conditional, a lookup table, a parameter). A value it cannot read is
pinned by the expression it comes from, one by one, so a new computed refusal in
a function that already had one is still caught. Mutation tests prove each of
those readings fails the guard.

The meaning. Each entry this issue adds says what its refusal means and what to
do next, checked by relation (a pattern per clause or step, the order of the
steps, and claims it must not make), never by whole sentences.

The resolution. `agentic-hil://reference/errors/<key>` answers for every key
this issue adds, with what `catalogue_entry` returns for it.

The delivery. A real refusal from each tool path carries the entry's
`remediation` and `do_not`, reached through the real tool function and the
suite's existing fakes.
"""

from __future__ import annotations

import ast
import errno
import importlib
import json
import re
import tempfile
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml
from conftest import write_config
from test_agent_provisioning import _regenerable_bench
from test_bootstrap import NUCLEO_VCP, STARTER_PROFILE, _fixed_stlink, _linux_openocd_host
from test_config_adopt import attached, document_of, placeholder_bench
from test_config_adopt import service as adopt_service
from test_coordination import failing_write_record
from test_debug_sessions import debug_service, start_debug_session
from test_flash_capture import PORT_ID, flash_args
from test_flash_capture import config_for as capture_config
from test_mcp_reference_resources import read_text

import agentic_hil
from agentic_hil import bootstrap
from agentic_hil.adopt import PROJECT_CONFIG_ADOPT, project_config_adopt_hardware
from agentic_hil.backends.common import CompletedCommand
from agentic_hil.backends.pyocd import PyOCDBackend
from agentic_hil.bootstrap import discover_attached_hardware
from agentic_hil.can import CanBusService
from agentic_hil.cli import init_config
from agentic_hil.comports import ComPortService
from agentic_hil.config import ConfigError, bind_debugger, load_authoritative_config, load_config
from agentic_hil.knowledge import (
    CONFIG_DESCRIPTION_RIGHT,
    ERROR_CATALOGUE,
    ERROR_URI_PREFIX,
    ErrorRemedy,
    catalogue_entry,
    remediation_fields,
)
from agentic_hil.report import canonical_run_report_path, ensure_audit_ready, report_state_path, write_report
from agentic_hil.tools import PROJECT_CONFIG_CREATE, AgenticHILToolService, UnprovisionedToolService

# ---------------------------------------------------------------------------
# The inventory.

SCANNED_MODULES = ("adopt", "artifacts", "bootstrap", "configwrite", "devices", "report", "tools")

# Callables that take the error type positionally, by the index of that argument.
POSITIONAL_ERROR_TYPE = {"ConfigError": 0, "tool_error": 1, "_validation_error": 2, "_discovery_failure": 0}
# Methods that write the key they are handed: `result.setdefault("error_type", ...)`.
KEYED_SETTERS = frozenset({"setdefault", "__setitem__"})
ERROR_TYPE_KEY = "error_type"

DYNAMIC = "<dynamic>"
FORWARDED = "<forwarded>"

# A value a site can write, and the expression it was read from.
Value = tuple[str, str]


@dataclass(frozen=True)
class Site:
    module: str
    function: str
    value: str
    line: int
    source: str


def _callee(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _parameters(function: ast.AST) -> set[str]:
    arguments = function.args  # type: ignore[attr-defined]
    names = {argument.arg for argument in (*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs)}
    names.update(argument.arg for argument in (arguments.vararg, arguments.kwarg) if argument is not None)
    return names


def _binds(node: ast.AST, name: str) -> bool:
    if isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)):
        targets = [node.target]
    else:
        targets = [item.optional_vars for item in node.items if item.optional_vars is not None]  # type: ignore[attr-defined]
    return any(isinstance(item, ast.Name) and item.id == name for target in targets for item in ast.walk(target))


def _bindings(scope: ast.AST, name: str, *, top_level_only: bool = False) -> list[ast.expr | None]:
    """What a name is bound to inside a scope; None for a binding no single value maps to."""
    nodes = list(scope.body) if top_level_only else list(ast.walk(scope))  # type: ignore[attr-defined]
    found: list[ast.expr | None] = []
    for node in nodes:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    found.append(node.value)
                elif isinstance(target, (ast.Tuple, ast.List)) and any(isinstance(item, ast.Name) and item.id == name for item in ast.walk(target)):
                    found.append(None)
        elif isinstance(node, (ast.AnnAssign, ast.NamedExpr)) and isinstance(node.target, ast.Name) and node.target.id == name and node.value is not None:
            found.append(node.value)
        elif (isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name) and node.target.id == name) or (
            isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension, ast.With, ast.AsyncWith)) and _binds(node, name)
        ):
            found.append(None)
    return found


class Scanner:
    """Every place one module writes an `error_type`, and the values it can write there."""

    def __init__(self, module_name: str, source: str | None = None) -> None:
        module = importlib.import_module(f"agentic_hil.{module_name}")
        self.module_name = module_name
        self.namespace = vars(module)
        self.tree = ast.parse(source if source is not None else Path(str(module.__file__)).read_text(encoding="utf-8"))
        self.parents = {child: parent for parent in ast.walk(self.tree) for child in ast.iter_child_nodes(parent)}
        self.sites: list[Site] = []

    def scopes(self, node: ast.AST) -> list[ast.AST]:
        chain = []
        while node in self.parents:
            node = self.parents[node]
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
                chain.append(node)
        return chain

    def qualname(self, node: ast.AST) -> str:
        return ".".join(reversed([getattr(scope, "name", "<lambda>") for scope in self.scopes(node)]))

    def name_bindings(self, node: ast.Name) -> tuple[str, list[ast.expr | None], object]:
        """Where a name is bound: a parameter, a local, a module assignment, or an imported value."""
        for scope in self.scopes(node):
            if isinstance(scope, ast.ClassDef):
                continue
            if node.id in _parameters(scope):
                return "parameter", [], scope
            if isinstance(scope, ast.Lambda):
                continue
            bound = _bindings(scope, node.id)
            if bound:
                return "local", bound, scope
        bound = _bindings(self.tree, node.id, top_level_only=True)
        if bound:
            return "module", bound, self.tree
        return ("namespace" if node.id in self.namespace else "unbound"), [], None

    def key_is_error_type(self, node: ast.expr | None) -> bool:
        if isinstance(node, ast.Constant):
            return node.value == ERROR_TYPE_KEY
        if isinstance(node, ast.Name):
            return {value for value, _ in self.resolve(node)} == {ERROR_TYPE_KEY}
        return False

    def table(self, node: ast.expr, key: ast.expr | None, seen: frozenset) -> set[Value] | None:
        """What a lookup in a mapping can produce, or None for no mapping this scan can read."""
        if isinstance(node, ast.Dict):
            pairs = list(zip(node.keys, node.values, strict=True))
            if isinstance(key, ast.Constant):
                hits = [value for item, value in pairs if isinstance(item, ast.Constant) and item.value == key.value]
                if hits:
                    return set().union(*(self.resolve(value, seen) for value in hits))
                return {(DYNAMIC, ast.unparse(node))} if None in node.keys else set()
            return set().union(set(), *(self.resolve(value, seen) if item is not None else {(DYNAMIC, ast.unparse(value))} for item, value in pairs))
        if isinstance(node, ast.Name):
            kind, bound, scope = self.name_bindings(node)
            if kind in {"local", "module"}:
                if (scope, node.id) in seen:
                    return set()
                tables = [self.table(value, key, seen | {(scope, node.id)}) if value is not None else None for value in bound]
                return set().union(*tables) if all(found is not None for found in tables) else None  # type: ignore[arg-type]
            mapping = self.namespace.get(node.id) if kind == "namespace" else None
            if isinstance(mapping, dict) and mapping and all(isinstance(value, str) for value in mapping.values()):
                if isinstance(key, ast.Constant) and key.value in mapping:
                    return {(mapping[key.value], f"{node.id}[{key.value!r}]")}
                return {(value, f"{node.id}[...]") for value in mapping.values()}
        return None

    def resolve(self, node: ast.expr | None, seen: frozenset = frozenset()) -> set[Value]:
        if node is None:
            return {(DYNAMIC, "<unpacked>")}
        if isinstance(node, ast.Constant):
            if node.value is None:
                return set()
            return {(node.value, repr(node.value))} if isinstance(node.value, str) else {(DYNAMIC, ast.unparse(node))}
        if isinstance(node, ast.IfExp):
            return self.resolve(node.body, seen) | self.resolve(node.orelse, seen)
        if isinstance(node, ast.BoolOp):
            return set().union(*(self.resolve(value, seen) for value in node.values))
        if isinstance(node, ast.NamedExpr):
            return self.resolve(node.value, seen)
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id == "str" and len(node.args) == 1:
                return self.resolve(node.args[0], seen)
            if isinstance(node.func, ast.Attribute) and node.func.attr == "get" and 1 <= len(node.args) <= 2 and not node.keywords:
                found = self.table(node.func.value, node.args[0], seen)
                default = self.resolve(node.args[1], seen) if len(node.args) == 2 else set()
                return (found if found is not None else {(DYNAMIC, ast.unparse(node))}) | default
            return {(DYNAMIC, ast.unparse(node))}
        if isinstance(node, ast.Subscript):
            found = self.table(node.value, node.slice, seen)
            return found if found is not None else {(DYNAMIC, ast.unparse(node))}
        if isinstance(node, ast.Name):
            kind, bound, scope = self.name_bindings(node)
            if kind == "parameter":
                return {(FORWARDED, node.id)}
            if kind in {"local", "module"}:
                if (scope, node.id) in seen:
                    return set()
                return set().union(*(self.resolve(value, seen | {(scope, node.id)}) for value in bound))
            value = self.namespace.get(node.id)
            return {(value, node.id)} if isinstance(value, str) else {(DYNAMIC, node.id)}
        return {(DYNAMIC, ast.unparse(node))}

    def record(self, node: ast.expr, function: str | None = None) -> None:
        where = self.qualname(node) if function is None else function
        for value, source in self.resolve(node):
            self.sites.append(Site(self.module_name, where, value, node.lineno, source))

    def scan(self) -> list[Site]:
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Dict):
                for key, value in zip(node.keys, node.values, strict=True):
                    if key is not None and self.key_is_error_type(key):
                        self.record(value)
            elif isinstance(node, ast.Call):
                for keyword in node.keywords:
                    if keyword.arg == ERROR_TYPE_KEY:
                        self.record(keyword.value)
                index = POSITIONAL_ERROR_TYPE.get(_callee(node.func) or "")
                if index is not None and len(node.args) > index and not any(isinstance(argument, ast.Starred) for argument in node.args[: index + 1]):
                    self.record(node.args[index])
                if isinstance(node.func, ast.Attribute) and node.func.attr in KEYED_SETTERS and len(node.args) == 2 and self.key_is_error_type(node.args[0]):
                    self.record(node.args[1])
            elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    if isinstance(target, ast.Subscript) and self.key_is_error_type(target.slice) and node.value is not None:
                        self.record(node.value)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                arguments = node.args
                positional = [*arguments.posonlyargs, *arguments.args]
                defaults = list(zip(positional[len(positional) - len(arguments.defaults) :], arguments.defaults, strict=True))
                defaults += [(argument, default) for argument, default in zip(arguments.kwonlyargs, arguments.kw_defaults, strict=True) if default is not None]
                for argument, default in defaults:
                    if argument.arg == ERROR_TYPE_KEY:
                        self.record(default, function=f"{self.qualname(node)}.{node.name}".lstrip("."))
        return self.sites


def scan_all(sources: dict[str, str] | None = None) -> list[Site]:
    """Every site in the scanned modules; `sources` replaces a module's text, for the mutation tests."""
    sources = sources or {}
    return [site for module in SCANNED_MODULES for site in Scanner(module, sources.get(module)).scan()]


def literal_values(sites: list[Site]) -> set[str]:
    return {site.value for site in sites if site.value not in {DYNAMIC, FORWARDED}}


# What the scan has to find, module by module. Pinned so that a scan that stops
# finding values (a renamed helper, a new way of building a refusal) fails here
# instead of passing with less to check.
EXPECTED_INVENTORY: dict[str, frozenset[str]] = {
    "adopt": frozenset(
        {
            "audit_failed_after_action",
            "config_file_not_found",
            "config_invalid",
            "config_write_in_open_run",
            "hardware_mismatch",
            "invalid_argument",
            "permission_denied",
            "resource_quarantined",
            "unknown_device",
        }
    ),
    "artifacts": frozenset(
        {
            "artifact_changed",
            "artifact_not_found",
            "artifact_staging_failed",
            "artifact_too_large",
            "artifact_validation_failed",
            "invalid_argument",
            "output_validation_failed",
            "permission_denied",
            "unsafe_configured_path",
        }
    ),
    "bootstrap": frozenset(
        {
            "adapter_not_found",
            "ambiguous_hardware",
            "config_invalid",
            "debugger_not_executable",
            "debugger_not_found",
            "invalid_argument",
            "probe_discovery_failed",
            "probe_inventory_incomplete",
            "target_not_detected",
            "timeout",
        }
    ),
    "configwrite": frozenset(
        {
            "config_changed_underneath",
            "config_file_not_found",
            "config_invalid",
            "config_unreadable",
            "config_write_in_open_run",
            "invalid_argument",
            "permission_change_in_open_run",
            "permission_denied",
            "permission_widening_denied",
        }
    ),
    "devices": frozenset(
        {
            "can_participant_not_configured",
            "can_participant_required",
            "coordination_state_invalid",
            "invalid_argument",
            "not_supported",
            "unknown_device",
        }
    ),
    "report": frozenset(
        {
            "audit_failed",
            "audit_unavailable",
            "canonical_write_pending",
            "config_invalid",
            "coordination_state_invalid",
            "report_not_found",
            "report_state_damaged",
            "report_unreadable",
            "report_write_failed",
            "unknown_debugger_error",
        }
    ),
    "tools": frozenset(
        {
            "artifact_validation_failed",
            "audit_failed_after_action",
            "audit_unavailable",
            "cleanup_required",
            "com_port_close_failed",
            "config_file_not_found",
            "config_invalid",
            "config_write_in_open_run",
            "hardware_action_exception",
            "invalid_argument",
            "not_supported",
            "permission_denied",
            "recovery_requires_physical_check",
            "resource_busy",
            "resource_quarantined",
            "run_already_active",
            "serial_read_failed",
            "service_cleanup_required",
            "service_closed",
            "unknown_tool",
        }
    ),
}

# The bare entries this issue adds.
NEW_ENTRIES = frozenset(
    {
        "ambiguous_hardware",
        "artifact_changed",
        "artifact_not_found",
        "artifact_staging_failed",
        "artifact_too_large",
        "artifact_validation_failed",
        "audit_failed_after_action",
        "audit_unavailable",
        # The nested `audit_error` of a report read back before its promotion
        # committed (report.staged_report_snapshot). `get_last_report` hands that
        # report to the caller, so the marker is advice a caller meets.
        "canonical_write_pending",
        "cleanup_required",
        "config_changed_underneath",
        "config_unreadable",
        "hardware_action_exception",
        "hardware_mismatch",
        "output_validation_failed",
        "report_not_found",
        # A report state that reads and holds something damaged (#689). It used
        # to answer `config_invalid` with configuration advice; the repair is
        # `agentic-hil report-state-repair`, not the configuration.
        "report_state_damaged",
        "report_unreadable",
        # The nested `audit_error` of a report or log write the filesystem
        # refused (`report.audit_error_detail`, #675). Both audit paths nest the
        # same shape, the refusal before an action and the failure after one, so
        # the type a caller looks up is a catalogue key rather than the Python
        # class name that used to stand there.
        "report_write_failed",
        "service_cleanup_required",
        "service_closed",
        "unknown_device",
        "unknown_tool",
    }
)

# `not_supported` is one word for refusals whose fixes have nothing in common, so
# no bare entry can be true for all of them. Each site that answers it at the top
# level gets its own scoped key, by (module, function) of the site.
SCOPED_SITES: dict[tuple[str, str], str] = {
    ("tools", "unbound_debugger_error"): "not_supported:unbound_debugger",
    ("tools", "unnamed_probe_error"): "not_supported:unnamed_probe",
}
SCOPED_VALUE = "not_supported"

# A not_supported site that never reaches a tool result.
EXCLUDED_SITES: dict[tuple[str, str], str] = {
    ("devices", "DebuggerDevice.routing_refusal"): "only `Device.execute` calls it, and nothing in src calls `Device.execute`",
}

# Values only attached-hardware discovery writes in these modules. Discovery has
# no configured debugger to scope by, and the per-backend entries these types
# already have describe a configured bench, so each gets a `:discovery` entry.
DISCOVERY_SCOPE = "discovery"
DISCOVERY_SCOPED = frozenset({"adapter_not_found", "debugger_not_executable", "target_not_detected"})

# Already in the catalogue as a bare entry; this issue relies on them unchanged.
COVERED = frozenset(
    {
        "config_file_not_found",
        "config_invalid",
        "config_write_in_open_run",
        "invalid_argument",
        "permission_change_in_open_run",
        "permission_denied",
        "permission_widening_denied",
        "probe_inventory_incomplete",
        "recovery_requires_physical_check",
        "resource_quarantined",
        "run_already_active",
        "unsafe_configured_path",
    }
)

# Values these modules write whose entries another issue adds.
OWNED_ELSEWHERE: dict[str, str] = {
    "can_participant_not_configured": "#635 (CAN half)",
    "can_participant_required": "#635 (CAN half)",
    "com_port_close_failed": "#635 (COM half)",
    "serial_read_failed": "#635 (COM half)",
    "audit_failed": "#646 (the reactor's refusal; classify_last_error reuses the word as a label)",
    "coordination_state_invalid": "#646",
    "resource_busy": "#646",
    "debugger_not_found": "#644",
    "probe_discovery_failed": "#644",
    "timeout": "#644",
    "unknown_debugger_error": "#644 (classify_last_error's top-level error_type for a failure that recorded none)",
}

# Every expression a value is computed from rather than written, each pinned on
# its own with why no entry follows from it. Keyed by the expression, so a second
# computed value in a function that already has one is a new key here.
#
# Three nested `audit_error` expressions used to sit here, one per path that met
# a failed write: `audit_unavailable`, `mark_audit_failure` and the dispatcher.
# All three now nest `report.audit_error_detail`, which writes the
# `report_write_failed` literal above for a filesystem fault and hands a
# configuration refusal its own `to_dict()`, so none of them computes a type any
# more and the nested error is classified like every other refusal (#675).
DYNAMIC_SITES: dict[tuple[str, str, str], str] = {
    ("report", "classify_failure_report", "report.get('error_type')"): "classify_last_error answers ok:true with the recorded failure's own type as a label; that type's entry is the advice",
    ("report", "classify_failure_report", "report.get('target_error_type')"): "the same label, taken from the target's own failure type",
    ("tools", "AgenticHILToolService._capture_result", "failure['error_type']"): "copies the capture failure built just above it, whose two sources are pinned on their own",
    ("tools", "AgenticHILToolService._capture_result", "reader_error.get('error_type', 'serial_read_failed')"): "the COM reader's own refusal, owned by #635 (COM half)",
    ("tools", "AgenticHILToolService._capture_result", "stop.get('error_type', 'com_port_close_failed')"): "the COM session stop's own refusal, owned by #635 (COM half)",
}

# Helpers that forward an error_type parameter; their callers are scanned instead.
FORWARDING_SITES: dict[tuple[str, str, str], str] = {
    ("artifacts", "ArtifactManager._validation_error", ERROR_TYPE_KEY): "every caller passes a literal or the default",
    ("bootstrap", "_discovery_failure", ERROR_TYPE_KEY): "every caller passes a literal",
    ("tools", "tool_error", ERROR_TYPE_KEY): "every caller passes a literal",
}

# Functions whose sites are left out of the delivery, each with why.
DEAD_SITES: dict[tuple[str, str], str] = {
    ("report", "read_report_file"): "nothing in src calls it; the report readers go through read_report_state_entry",
}

NEW_KEYS = tuple(
    sorted(NEW_ENTRIES | set(SCOPED_SITES.values()) | {f"{value}:{DISCOVERY_SCOPE}" for value in DISCOVERY_SCOPED})
)


def inventory_problems(sites: list[Site]) -> list[str]:
    problems = []
    for module in SCANNED_MODULES:
        found = literal_values([site for site in sites if site.module == module])
        if missing := sorted(EXPECTED_INVENTORY[module] - found):
            problems.append(f"{module}: the scan no longer finds {missing}")
        if added := sorted(found - EXPECTED_INVENTORY[module]):
            problems.append(f"{module}: new error_type values {added}; classify each and give it an entry or a reason")
    return problems


def classification_problems(sites: list[Site]) -> list[str]:
    classes = {
        "new": NEW_ENTRIES,
        "scoped by site": frozenset({SCOPED_VALUE}),
        "scoped for discovery": DISCOVERY_SCOPED,
        "covered": COVERED,
        "owned elsewhere": frozenset(OWNED_ELSEWHERE),
    }
    found = literal_values(sites)
    problems = []
    for value in sorted(found):
        holders = [name for name, members in classes.items() if value in members]
        if len(holders) != 1:
            problems.append(f"{value} is classified as {holders or 'nothing'}")
    everything = frozenset().union(*classes.values())
    if stale := sorted(everything - found):
        problems.append(f"classified but not in the code: {stale}")
    for value in sorted(DISCOVERY_SCOPED & found):
        if outside := sorted({(site.module, site.function) for site in sites if site.value == value and site.module != "bootstrap"}):
            problems.append(f"{value} is scoped for discovery and also written outside it: {outside}")
    return problems


def non_literal_problems(sites: list[Site]) -> list[str]:
    problems = []
    for marker, pinned in ((DYNAMIC, DYNAMIC_SITES), (FORWARDED, FORWARDING_SITES)):
        found = Counter((site.module, site.function, site.source) for site in sites if site.value == marker)
        for key in sorted(set(found) | set(pinned)):
            if key not in pinned:
                problems.append(f"{marker} value from {key} at line(s) {sorted(site.line for site in sites if (site.module, site.function, site.source) == key)} is not pinned")
            elif found[key] != 1:
                problems.append(f"{marker} value from {key} is written {found[key]} times, pinned once")
    return problems


def scoped_site_problems(sites: list[Site]) -> list[str]:
    found = {(site.module, site.function) for site in sites if site.value == SCOPED_VALUE}
    expected = set(SCOPED_SITES) | set(EXCLUDED_SITES)
    return [f"{SCOPED_VALUE} written at {sorted(found ^ expected)} without its own scope or reason"] if found != expected else []


def all_problems(sites: list[Site]) -> list[str]:
    return [*inventory_problems(sites), *classification_problems(sites), *non_literal_problems(sites), *scoped_site_problems(sites)]


@pytest.fixture(scope="module")
def sites() -> list[Site]:
    return scan_all()


def test_the_scan_finds_the_pinned_inventory(sites: list[Site]) -> None:
    assert inventory_problems(sites) == []


def test_every_value_is_classified_exactly_once(sites: list[Site]) -> None:
    assert classification_problems(sites) == []
    for reason in (*OWNED_ELSEWHERE.values(), *EXCLUDED_SITES.values(), *DYNAMIC_SITES.values(), *FORWARDING_SITES.values(), *DEAD_SITES.values()):
        assert reason.strip()


def test_every_not_supported_site_has_its_own_scope_or_a_reason(sites: list[Site]) -> None:
    assert scoped_site_problems(sites) == []


def test_every_non_literal_expression_is_pinned_on_its_own(sites: list[Site]) -> None:
    assert non_literal_problems(sites) == []


def report_source() -> str:
    return Path(str(importlib.import_module("agentic_hil.report").__file__)).read_text(encoding="utf-8")


# Each a way of adding a refusal the guards above have to catch, written into
# classify_failure_report, a function that already writes computed values.
ANCHOR = "    return attach_canonical_audit_evidence(config, result)\n"
MADE_UP_TABLE = '\n_MADE_UP_REFUSALS = {"classify_last_error": "made_up_refusal"}\n'
MUTATIONS: dict[str, tuple[str, str, str]] = {
    "a keyed setter": (ANCHOR, '    result.setdefault("error_type", "made_up_refusal")\n' + ANCHOR, ""),
    "a keyed dunder setter": (ANCHOR, '    result.__setitem__("error_type", "made_up_refusal")\n' + ANCHOR, ""),
    "a module table subscript": (ANCHOR, '    result["error_type"] = _MADE_UP_REFUSALS[str(result["tool"])]\n' + ANCHOR, MADE_UP_TABLE),
    "a module table lookup": (ANCHOR, '    result["error_type"] = _MADE_UP_REFUSALS.get(str(result["tool"]))\n' + ANCHOR, MADE_UP_TABLE),
    "a lookup default": (ANCHOR, '    result["error_type"] = report.get("step_error_type", "made_up_default")\n' + ANCHOR, ""),
    "a local table": (ANCHOR, '    refusals = {"classify_last_error": "made_up_refusal"}\n    result["error_type"] = refusals[str(result["tool"])]\n' + ANCHOR, ""),
    "a key spelled through a constant": (ANCHOR, '    result[_MADE_UP_KEY] = "made_up_refusal"\n' + ANCHOR, '\n_MADE_UP_KEY = "error_type"\n'),
    "a second copy of a pinned computed value": (ANCHOR, '    result["error_type"] = report.get("error_type")\n' + ANCHOR, ""),
    "a replaced operand of a pinned BoolOp": ('report.get("error_type") or (', 'report.get("failure_kind") or (', ""),
}


def mutated_report(name: str) -> str:
    old, new, appended = MUTATIONS[name]
    source = report_source()
    assert source.count(old) == 1, f"the anchor for {name} is gone from report.py"
    return source.replace(old, new) + appended


@pytest.mark.parametrize("name", sorted(MUTATIONS))
def test_the_guards_fail_a_refusal_added_through(name: str) -> None:
    problems = all_problems(scan_all({"report": mutated_report(name)}))
    assert problems, f"{name} passed every guard"


def test_the_guards_pass_the_unmutated_source_read_the_same_way() -> None:
    assert all_problems(scan_all({"report": report_source()})) == []


def test_the_entries_this_issue_relies_on_are_there() -> None:
    assert sorted(value for value in COVERED if value not in ERROR_CATALOGUE) == []


@pytest.mark.parametrize("key", NEW_KEYS)
def test_each_new_entry_is_complete(key: str) -> None:
    assert key in ERROR_CATALOGUE, f"no catalogue entry for {key}"
    entry = catalogue_entry(key)
    assert isinstance(entry["meaning"], str) and entry["meaning"].strip(), key
    for field in ("remediation", "do_not"):
        steps = entry.get(field)
        assert isinstance(steps, list) and steps, f"{key} has no {field}"
        assert all(isinstance(step, str) and step.strip() for step in steps), (key, field)


def test_the_report_reader_left_out_has_no_caller() -> None:
    source_root = Path(agentic_hil.__file__).parent
    for module, function in DEAD_SITES:
        uses = [
            (path.relative_to(source_root).as_posix(), line.strip())
            for path in sorted(source_root.rglob("*.py"))
            for line in path.read_text(encoding="utf-8").splitlines()
            if re.search(rf"\b{function}\b", line)
        ]
        assert len(uses) == 1, uses
        assert uses[0][0] == f"{module}.py" and uses[0][1].startswith(f"def {function}("), uses


# ---------------------------------------------------------------------------
# The meaning.

CLAUSE = re.compile(r"(?<=[.;!?])\s+")
FLAGS = re.IGNORECASE | re.DOTALL


@dataclass(frozen=True)
class Facts:
    """What an entry has to say, by relation.

    `says`: each matches one clause of the meaning. `steps`: each matches a
    remediation step, and the first step each matches comes in this order.
    `mentions`: each appears somewhere in the meaning or the remediation.
    `do_not`: each matches one wrong fix. `never`: no clause of the meaning or
    the remediation makes this claim."""

    says: tuple[str, ...] = ()
    steps: tuple[str, ...] = ()
    mentions: tuple[str, ...] = ()
    do_not: tuple[str, ...] = ()
    never: tuple[str, ...] = ()


FACTS: dict[str, Facts] = {
    "artifact_not_found": Facts(
        says=(r"\bnothing was\b.*\bflashed\b", r"`image_path`.*resolved against the workspace root", r"`artifact_id`.*names no upload"),
        steps=(r"workspace root", r"build it first", r"upload the image again", r"backend_error"),
        do_not=(r"placeholder", r"another build"),
        never=(r"resolved against the directory the server",),
    ),
    "artifact_changed": Facts(
        says=(r"nothing was sent to the board", r"retry_safe: true", r"calling again validates and stages"),
        steps=(r"build .*finish", r"call again once the file is stable", r"build it first", r"backend_error"),
        do_not=(r"raw programmer", r"tight loop"),
        never=(r"retry_safe: false",),
    ),
    "artifact_staging_failed": Facts(
        says=(r"passed validation", r"temporary directory", r"neither in the workspace nor under `state_root`", r"not_started"),
        steps=(r"backend_error", r"free space|write access", r"restart the MCP server"),
        do_not=(r"incident to recover or sign", r"toolchain yourself"),
        never=(r"(free|make) space (in|under|on) the state root", r"\b(is|was) quarantined\b"),
    ),
    "artifact_too_large": Facts(
        says=(r"`bytes`.*`max_bytes`", r"max_upload_size_mb.*MiB", r"before anything is sent to the board"),
        steps=(r"compare `bytes`", r"\.hex.*\.bin", r"operator's to raise"),
        mentions=(r"applies when the server restarts",),
        do_not=(r"truncate or split", r"raw programmer"),
        never=(r"project_config_set", r"\bstrip\b"),
    ),
    "artifact_validation_failed": Facts(
        says=(r"nothing was sent to the board", r"within_workspace.*nothing relaxes", r"not an `\.elf`"),
        steps=(r"read `validation`", r"format check", r"debug session.*\.elf", r"inside the\s+workspace", r"operator"),
        do_not=(r"rename", r"allowed_roots"),
        never=(r"project_config_set",),
    ),
    "output_validation_failed": Facts(
        says=(r"before anything was read", r"`\.hex` or `\.ihex`", r"inside the workspace \(nothing relaxes"),
        steps=(r"read `validation`", r"build/<symbol>\.hex", r"call again"),
        do_not=(r"another tool", r"allowed_roots"),
        never=(r"project_config_set", r"\.elf\b"),
    ),
    # A destination the profile refuses is a configuration refusal nested as
    # `audit_error`, with its own entry; naming that fix here as well prints it
    # twice under one refusal.
    "audit_unavailable": Facts(
        says=(r"refused before it started", r"nothing was flashed, reset or written"),
        steps=(r"audit_error", r"agentic-hil doctor", r"call again"),
        mentions=(r"`audit_error` that names it carries its own remediation",),
        do_not=(r"by hand", r"delete report state"),
        never=(r"quarantin", r"recover --confirm", r"init --force"),
    ),
    "audit_failed_after_action": Facts(
        says=(
            r"action ran and the record of it could not be written",
            r"neither wrote anything to the configuration",
            r"quarantined under an audit-broken reason",
            r"no automatic recovery clears",
        ),
        steps=(r"agentic-hil doctor", r"get_last_report", r"agentic-hil recover --confirm-safe-state --quarantine-id", r"hardware_lease_status`? again"),
        do_not=(r"repeat the action before the incident is settled", r"delete"),
        never=(r"(?<!no )automatic recovery clears", r"nothing was (flashed|started)"),
    ),
    "hardware_action_exception": Facts(
        says=(r"side_effect_status: unknown", r"retry_safe: false", r"settles the lock and says nothing about the board"),
        steps=(r"backend_error", r"quarantined.*hardware_lease_status", r"before repeating", r"defect"),
        do_not=(r"loop", r"by hand"),
        never=(r"leaves? the (target|board) (unchanged|untouched|as it was)", r"nothing was (sent|started)"),
    ),
    "service_closed": Facts(
        says=(r"only while the server process is stopping", r"nothing was started"),
        steps=(r"restart or reconnect", r"hardware_lease_status"),
        do_not=(r"loop",),
        never=(r"agentic-hil recover",),
    ),
    "service_cleanup_required": Facts(
        says=(r"shutdown failed part-way", r"stays recorded as held", r"next server finds it as an incident"),
        steps=(r"restart the MCP server", r"hardware_lease_status", r"agentic-hil recover --confirm-safe-state"),
        do_not=(r"coordination records|lock files", r"keep calling"),
        never=(r"(released|freed) (everything|all)",),
    ),
    "unknown_tool": Facts(
        says=(r"nothing was started", r"config_file_not_found"),
        steps=(r"tools/list", r"agentic-hil doctor.*not a tool", r"serverInfo"),
        do_not=(r"raw", r"guess"),
        never=(r"debugger_info",),
    ),
    "report_not_found": Facts(
        says=(r"nothing to read yet", r"includes every recorded call having succeeded", r"per project"),
        steps=(r"make the hardware call", r"same server, configuration and\s+workspace"),
        do_not=(r"pass or a failure", r"state_root"),
        never=(r"\bmeans? (the|a|that the) (call|run) (failed|passed)",),
    ),
    "report_unreadable": Facts(
        says=(r"exists and reading it failed", r"path is\s+withheld", r"damaged answers `report_state_damaged`"),
        steps=(r"error_class", r"state_root", r"audit_unavailable"),
        do_not=(r"delete or recreate", r"empty record"),
        never=(r"\bdelete\b",),
    ),
    # Damaged content, not a failed read: its own repair command, the damaged
    # copy kept, and never the configuration as the cause (#689).
    "report_state_damaged": Facts(
        says=(r"exists and reads", r"not JSON", r"configuration is not involved", r"withheld", r"audit_unavailable"),
        steps=(r"agentic-hil report-state-repair", r"every byte kept", r"Call again once it is repaired"),
        do_not=(r"delete or edit", r"configuration"),
        never=(r"config_invalid", r"init --force"),
    ),
    # The write side of the same fault, nested as `audit_error` by both audit
    # paths. It may never read as a record that was written, nor offer a retry
    # before the destination is repaired: an action whose audit failed may have
    # reached the board.
    "report_write_failed": Facts(
        says=(r"report or audit record failed", r"`error_class` and `errno`.*filesystem fault", r"without exposing the state-root path"),
        steps=(r"`error_class` and `errno`", r"restore write access or free space", r"[Rr]etry only after the report destination is writable"),
        mentions=(r"incident resolved",),
        do_not=(r"delete or recreate report state", r"repeat an action whose audit failed"),
        never=(r"\breport (was|is) written\b", r"retry is safe"),
    ),
    "config_unreadable": Facts(
        says=(r"exists and cannot be read", r"nothing was decided from it and nothing was written"),
        steps=(r"`path` and `backend_error`", r"operator fix", r"call again"),
        do_not=(r"agentic-hil init --force.*resets every permission", r"AGENTIC_HIL_CONFIG"),
        never=(r"\brun `agentic-hil init --force`",),
    ),
    "config_changed_underneath": Facts(
        says=(r"another process wrote", r"nothing was written", r"retry_safe: true"),
        steps=(r"re-read .*project_config_describe", r"plan again", r"operator"),
        mentions=(r"project_config_adopt_hardware` again",),
        do_not=(r"project_config_set", r"same plan again"),
        never=(r"retry_safe: false", r"(resend|send) the same plan"),
    ),
    "unknown_device": Facts(
        says=(r"does not declare", r"looked up in the configuration, not on the bench", r"nothing was held or started"),
        steps=(r"configured_debuggers|configured_devices", r"exactly", r"project_config_describe"),
        mentions=(r"allow_config_description_write", r"adoption never adds"),
        do_not=(r"create an entry yourself", r"substitute another"),
    ),
    "hardware_mismatch": Facts(
        says=(r"`configured_probe_id` against `discovered_probe_id`", r"nothing was written"),
        steps=(r"ask the operator", r"attach it and call again", r"debugger_id", r"debuggers\.<name>\.probe_id"),
        do_not=(r"clear or overwrite", r"another entry"),
    ),
    "ambiguous_hardware": Facts(
        says=(r"more than one in-circuit debugger or programmer", r"will not choose", r"nothing was read from a board and nothing was written"),
        steps=(r"ask the operator", r"project_config_adopt_hardware.*probe_id", r"leave only that one connected"),
        do_not=(r"pick", r"retry"),
    ),
    "not_supported:unbound_debugger": Facts(
        says=(r"binds none", r"declares no debugger at all, or it declares several", r"retry_safe: false"),
        steps=(r"configured_debuggers", r"empty.*project_config_set.*project_config_reload_description.*project_config_create", r"several.*test_reactor_run"),
        mentions=(r"agentic-hil init --force", r"agentic-hil://reference/test-plan", r"allow_config_write", r"allow_config_description_write", r"every grant closed"),
        do_not=(r"other arguments", r"delete or hand-edit"),
        never=(r"debugger_id", r"only the operator can add"),
    ),
    "not_supported:unnamed_probe": Facts(
        says=(r"names no `probe_id` while other entries exist", r"nothing was started"),
        steps=(r"debugger_probes_list", r"project_config_adopt_hardware.*debugger_id.*probe_id", r"every other entry", r"call the tool again"),
        mentions=(r"OpenOCD cannot enumerate", r"allow_config_description_write"),
        do_not=(r"remove the other entries", r"guess a serial"),
    ),
    "adapter_not_found:discovery": Facts(
        says=(r"found no in-circuit debugger or programmer", r"requested_probe_id", r"nothing was read from a board and nothing was written"),
        steps=(r"requested_probe_id", r"STM32CubeProgrammer", r"attach the probe", r"project_config_adopt_hardware"),
        do_not=(r"probe_id",),
        never=(r"(selection|discovery) (adds|will add)",),
    ),
    "target_not_detected:discovery": Facts(
        says=(r"answered", r"found no\s+target", r"nothing was reset or written"),
        steps=(r"powered", r"SWD", r"debug pins.*operator", r"same discovery again"),
        do_not=(r"another `probe_id`", r"under reset"),
        never=(r"(?<!never )connects under reset",),
    ),
    "debugger_not_executable:discovery": Facts(
        says=(r"present on this host and will not run", r"rather than\s+falling back to OpenOCD", r"reads no configured\s+`executable`", r"nothing was said to the board"),
        steps=(r"not_executable_reason", r"permission_denied.*chmod \+x", r"not_an_executable_image", r"same discovery again"),
        do_not=(r"debuggers\.<name>\.executable", r"workspace"),
        never=(r"project_config_set", r"(correct|set) `debuggers\.<name>\.executable`"),
    ),
    "canonical_write_pending": Facts(
        says=(r"staged copy", r"neither a confirmed success nor a confirmed failure"),
        steps=(r"unconfirmed", r"hardware_lease_status", r"state root", r"again once records commit"),
        do_not=(r"passed", r"edit or delete"),
        never=(r"\bis a confirmed success",),
    ),
    "cleanup_required": Facts(
        says=(r"stopped the session's processes", r"could not be handed back", r"debug session is over"),
        steps=(r"cleanup_reasons", r"lease_release_unconfirmed.*debug_stop_session` again.*no session is active", r"audit_broken.*same refusal", r"hardware_lease_status"),
        mentions=(r"without touching the board", r"agentic-hil recover --confirm-safe-state"),
        do_not=(r"coordination records", r"processes by hand"),
        never=(r"target state remains unconfirmed", r"debug_start_session", r"audit_broken.*call `debug_stop_session` again"),
    ),
}


def clauses(text: str) -> list[str]:
    return [clause for clause in CLAUSE.split(text) if clause.strip()]


def fact_problems(key: str, facts: Facts) -> list[str]:
    entry = catalogue_entry(key)
    if entry is None:
        return [f"no catalogue entry for {key}"]
    meaning = clauses(entry["meaning"])
    steps = list(entry["remediation"])
    wrong_fixes = list(entry.get("do_not", []))
    advice = [*meaning, *(clause for step in steps for clause in clauses(step))]
    problems = []
    for pattern in facts.says:
        if not any(re.search(pattern, clause, FLAGS) for clause in meaning):
            problems.append(f"the meaning does not say {pattern!r}")
    reached = -1
    for pattern in facts.steps:
        hits = [index for index, step in enumerate(steps) if re.search(pattern, step, FLAGS)]
        if not hits:
            problems.append(f"no remediation step matches {pattern!r}")
        elif hits[0] < reached:
            problems.append(f"{pattern!r} first comes at step {hits[0]}, before step {reached}")
        else:
            reached = hits[0]
    for pattern in facts.mentions:
        if not re.search(pattern, "\n".join([entry["meaning"], *steps]), FLAGS):
            problems.append(f"neither the meaning nor the remediation mentions {pattern!r}")
    for pattern in facts.do_not:
        if not any(re.search(pattern, item, FLAGS) for item in wrong_fixes):
            problems.append(f"no do_not item matches {pattern!r}")
    for pattern in facts.never:
        claimed = [clause for clause in advice if re.search(pattern, clause, FLAGS)]
        if claimed:
            problems.append(f"{pattern!r} is claimed in {claimed}")
    return problems


def test_every_new_key_has_its_facts() -> None:
    assert set(FACTS) == set(NEW_KEYS)


@pytest.mark.parametrize("key", NEW_KEYS)
def test_each_new_entry_says_what_its_refusal_means_and_what_to_do(key: str) -> None:
    assert fact_problems(key, FACTS[key]) == []


@pytest.mark.parametrize(
    ("key", "inverted"),
    [
        (
            "artifact_staging_failed",
            ErrorRemedy(
                meaning="The image could not be staged. The bench is quarantined until an operator signs.",
                remediation=("Free space under the state root.", "Read backend_error.", "Restart the MCP server."),
                do_not=("Do not hand the image to the toolchain yourself.",),
            ),
        ),
        (
            "config_changed_underneath",
            ErrorRemedy(
                meaning="Another process wrote the file, so nothing was written. retry_safe: false.",
                remediation=("Send the same plan again.", "Ask the operator."),
                do_not=("Do not use project_config_set.",),
            ),
        ),
    ],
)
def test_the_meaning_check_refuses_an_inverted_entry(monkeypatch: pytest.MonkeyPatch, key: str, inverted: ErrorRemedy) -> None:
    monkeypatch.setitem(ERROR_CATALOGUE, key, inverted)
    assert fact_problems(key, FACTS[key])


# ---------------------------------------------------------------------------
# The resolution.


@pytest.fixture
def tools(tmp_path: Path) -> Iterator[AgenticHILToolService]:
    service = AgenticHILToolService(load_config(str(write_config(tmp_path))))
    try:
        yield service
    finally:
        service.close()


@pytest.mark.parametrize("key", NEW_KEYS)
def test_each_new_entry_resolves_as_a_reference_resource(tools: AgenticHILToolService, key: str) -> None:
    assert key in ERROR_CATALOGUE, f"no catalogue entry for {key}"
    assert json.loads(read_text(tools, ERROR_URI_PREFIX + key)) == catalogue_entry(key)


# ---------------------------------------------------------------------------
# The delivery.


def assert_carries_entry(result: dict, error_type: str, scope: str | None = None, *, key: str | None = None) -> None:
    """The refusal is the one expected and carries its entry's fields.

    `key` is the catalogue key that has to exist; it defaults to the scoped key
    when a scope is given. A site that passes a scope and is answered by the
    bare entry names the bare key here."""
    assert result.get("ok") is False, result
    assert result.get("error_type") == error_type, result
    key = key or (f"{error_type}:{scope}" if scope else error_type)
    assert key in ERROR_CATALOGUE, f"no catalogue entry for {key}"
    expected = remediation_fields(error_type, scope)
    assert expected, f"the entry for {key} needs a permission this refusal does not name"
    assert result.get("remediation") == expected["remediation"], result
    assert result.get("do_not") == expected.get("do_not"), result


def firmware(workspace: Path, name: str = "app.elf", content: bytes = b"\x7fELF" + b"\x00" * 12) -> Path:
    image = workspace / "build" / name
    image.parent.mkdir(parents=True, exist_ok=True)
    image.write_bytes(content)
    return image


TOO_LARGE = b"x" * (1024 * 1024 + 1)


# flash_firmware and artifact_upload: artifacts.py.


def test_flash_firmware_of_a_missing_image(tmp_path: Path, tools: AgenticHILToolService) -> None:
    (tmp_path / "build").mkdir()
    assert_carries_entry(tools.call("flash_firmware", {"image_path": "build/missing.elf"}), "artifact_not_found")


def test_flash_firmware_of_an_unknown_artifact_id(tools: AgenticHILToolService) -> None:
    assert_carries_entry(tools.call("flash_firmware", {"artifact_id": "0" * 64 + ".elf"}), "artifact_not_found")


def test_flash_firmware_of_a_file_that_is_not_firmware(tmp_path: Path, tools: AgenticHILToolService) -> None:
    firmware(tmp_path, "notes.txt", b"not an image")
    assert_carries_entry(tools.call("flash_firmware", {"image_path": "build/notes.txt"}), "artifact_validation_failed")


def test_flash_firmware_of_an_image_above_the_limit(tmp_path: Path, tools: AgenticHILToolService) -> None:
    firmware(tmp_path, "big.bin", TOO_LARGE)
    assert_carries_entry(tools.call("flash_firmware", {"image_path": "build/big.bin"}), "artifact_too_large")


def test_flash_firmware_of_an_image_rebuilt_after_validation(tmp_path: Path, tools: AgenticHILToolService, monkeypatch: pytest.MonkeyPatch) -> None:
    image = firmware(tmp_path)
    validate = tools.artifacts.validate_local_path

    def then_rebuilt(path: str) -> dict:
        validated = validate(path)
        image.write_bytes(b"\x7fELF" + b"\x01" * 12)
        return validated

    monkeypatch.setattr(tools.artifacts, "validate_local_path", then_rebuilt)
    result = tools.call("flash_firmware", {"image_path": "build/app.elf"})
    assert result.get("retry_safe") is True, result
    assert_carries_entry(result, "artifact_changed")


def test_flash_firmware_when_the_image_cannot_be_staged(tmp_path: Path, tools: AgenticHILToolService, monkeypatch: pytest.MonkeyPatch) -> None:
    firmware(tmp_path)

    def no_space(*args: object, **kwargs: object) -> str:
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(
        "agentic_hil.artifacts.tempfile",
        SimpleNamespace(mkdtemp=no_space, NamedTemporaryFile=tempfile.NamedTemporaryFile, TemporaryDirectory=tempfile.TemporaryDirectory, gettempdir=tempfile.gettempdir),
    )
    result = tools.call("flash_firmware", {"image_path": "build/app.elf"})
    assert result.get("side_effect_status") == "not_started", result
    assert_carries_entry(result, "artifact_staging_failed")


def test_flash_firmware_whose_capture_session_cannot_be_audited(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service = AgenticHILToolService(capture_config(tmp_path))
    monkeypatch.setattr(service.com_ports, "session_start", lambda port_id, *args, **kwargs: {"ok": True, "tool": "com_session_start", "port_id": port_id, "audit_ok": False})
    try:
        result = service.call("flash_firmware", flash_args(tmp_path))
    finally:
        service.close()
    assert result.get("port_id") == PORT_ID, result
    assert_carries_entry(result, "audit_unavailable")


def test_artifact_upload_of_an_image_above_the_limit(tmp_path: Path, tools: AgenticHILToolService) -> None:
    firmware(tmp_path, "big.bin", TOO_LARGE)
    assert_carries_entry(tools.call("artifact_upload", {"image_path": "build/big.bin"}), "artifact_too_large")


def test_artifact_upload_of_a_missing_image(tmp_path: Path, tools: AgenticHILToolService) -> None:
    (tmp_path / "build").mkdir()
    assert_carries_entry(tools.call("artifact_upload", {"image_path": "build/missing.bin"}), "artifact_not_found")


# debug_start_session and debug_dump_symbol_ihex.


def test_debug_start_session_of_an_image_that_is_not_an_elf(tmp_path: Path) -> None:
    service = debug_service(tmp_path)
    firmware(tmp_path, "app.bin", b"\x00\x01\x02\x03")
    try:
        result = service.call("debug_start_session", {"image_path": "build/app.bin", "mode": "load", "timeout_s": 10.0})
    finally:
        service.close()
    assert_carries_entry(result, "artifact_validation_failed")


def test_debug_dump_symbol_ihex_outside_the_allowed_roots(tmp_path: Path) -> None:
    service = debug_service(tmp_path)
    try:
        assert start_debug_session(service)["ok"] is True
        result = service.call("debug_dump_symbol_ihex", {"symbol": "CTC_array", "output_path": "outside/memory.hex"})
    finally:
        service.close()
    assert_carries_entry(result, "output_validation_failed")


# get_last_report and classify_last_error: report.py.


@pytest.mark.parametrize("tool", ["get_last_report", "classify_last_error"])
def test_a_report_tool_before_any_report_exists(tools: AgenticHILToolService, tool: str) -> None:
    assert_carries_entry(tools.call(tool), "report_not_found")


@pytest.mark.parametrize("tool", ["get_last_report", "classify_last_error"])
def test_a_report_tool_whose_state_holds_no_report_yet(tools: AgenticHILToolService, tool: str) -> None:
    """The report state exists and holds no entry: the other way to have nothing to read."""
    ensure_audit_ready(tools.config)
    assert Path(report_state_path(tools.config)).is_file()
    assert_carries_entry(tools.call(tool), "report_not_found")


@pytest.mark.parametrize("tool", ["get_last_report", "classify_last_error"])
def test_a_report_tool_whose_state_the_os_refuses_to_read(tools: AgenticHILToolService, monkeypatch: pytest.MonkeyPatch, tool: str) -> None:
    state_file = Path(report_state_path(tools.config))
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text("{}", encoding="utf-8")

    def refused(path: object, *args: object, **kwargs: object) -> str:
        raise PermissionError(errno.EACCES, "Permission denied", str(path))

    monkeypatch.setattr("agentic_hil.report.safe_read_text", refused)
    assert_carries_entry(tools.call(tool), "report_unreadable")


def test_a_report_read_back_before_its_promotion_committed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The staged readback a failed promotion leaves, as `get_last_report` hands it over.

    The per-run copy's promotion and the corrective state rewrite both fail, so
    the trusted readback is the staged copy with its pending marker. That marker
    is the `audit_error` a caller reads, and it carries the entry's advice."""
    from agentic_hil import report as report_module

    config = load_config(str(write_config(tmp_path)))
    canonical = canonical_run_report_path(config, "run-promotion-fails")
    original_atomic = report_module.atomic_write_text
    original_state = report_module.write_report_state
    canonical_writes = {"count": 0}

    def flaky_atomic(path: object, text: str, **kwargs: object) -> object:
        if Path(str(path)) == canonical:
            canonical_writes["count"] += 1
            if canonical_writes["count"] >= 2:
                raise OSError(errno.EROFS, "Read-only file system")
        return original_atomic(path, text, **kwargs)

    def flaky_state(cfg: object, state: dict) -> object:
        last = state.get("last_report")
        if isinstance(last, dict) and last.get("audit_ok") is False and not last.get("canonical_write_pending"):
            raise OSError(errno.EROFS, "Read-only file system")
        return original_state(cfg, state)

    monkeypatch.setattr(report_module, "atomic_write_text", flaky_atomic)
    monkeypatch.setattr(report_module, "write_report_state", flaky_state)
    assert write_report(config, {"ok": True, "tool": "probe_target", "run": "run-promotion-fails"})["audit_ok"] is False
    monkeypatch.setattr(report_module, "write_report_state", original_state)
    monkeypatch.setattr(report_module, "atomic_write_text", original_atomic)

    service = AgenticHILToolService(config)
    try:
        result = service.call("get_last_report")
    finally:
        service.close()
    report = result["report"]
    assert report.get("canonical_write_pending") is True, report
    for marker in (report["audit_error"], *report["audit_errors"]):
        assert marker["error_type"] == "canonical_write_pending", marker
        expected = remediation_fields("canonical_write_pending")
        assert expected, "no catalogue entry for canonical_write_pending"
        assert marker.get("remediation") == expected["remediation"], marker
        assert marker.get("do_not") == expected.get("do_not"), marker


def test_a_report_write_the_filesystem_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The nested `audit_error` a failed report write leaves (#675).

    The type is a catalogue key rather than the Python class name that used to
    stand there, `error_class` and `errno` say which filesystem fault it was,
    and the entry's own advice travels with the marker, as it does for every
    other nested marker a caller reads."""
    config = load_config(str(write_config(tmp_path)))

    def no_space(*_args: object, **_kwargs: object) -> None:
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr("agentic_hil.report.safe_write_text", no_space)

    written = write_report(config, {"ok": True, "tool": "probe_target", "summary": "Target answered."})

    assert written["audit_ok"] is False, written
    expected = remediation_fields("report_write_failed")
    assert expected, "no catalogue entry for report_write_failed"
    for marker in (written["audit_error"], *written["audit_errors"]):
        assert marker["error_type"] == "report_write_failed", marker
        assert marker["error_class"] == "OSError", marker
        assert marker["errno"] == errno.ENOSPC, marker
        assert marker.get("remediation") == expected["remediation"], marker
        assert marker.get("do_not") == expected.get("do_not"), marker


def test_a_configuration_refusal_keeps_its_advice_on_both_audit_paths(tmp_path: Path) -> None:
    """One nested shape for both audit paths, as #675 asks.

    The refusal met before an action and the one met after a write that failed
    are the same configuration refusal, so they nest the same document:
    its own `error_type`, its details and its own remediation. `path` is the
    open question in the issue and is left out of the comparison."""
    from agentic_hil import report as report_module

    refusal = ConfigError(
        "unsafe_configured_path",
        "Output file must be a single-link regular file without symlinked parents.",
        {"path": str(tmp_path / "last-report.json"), "resolved_parent": str(tmp_path)},
    )

    before = report_module.audit_unavailable("probe_target", refusal)["audit_error"]
    after = report_module.mark_audit_failure({"ok": True, "tool": "probe_target"}, refusal)["audit_error"]

    assert before.get("remediation") == remediation_fields("unsafe_configured_path")["remediation"], before
    assert after["error_type"] == "unsafe_configured_path", after
    assert after["summary"] == refusal.summary, after
    assert after["resolved_parent"] == str(tmp_path), after
    dropped = {key: value for key, value in before.items() if key != "path" and after.get(key) != value}
    assert dropped == {}, f"the refusal met after the write dropped {sorted(dropped)}: {after}"


# project_config_adopt_hardware and project_config_create: adopt.py, configwrite.py, bootstrap.py.


def adopt(workspace: Path, arguments: dict | None = None) -> dict:
    service = adopt_service(workspace)
    try:
        return service.call(PROJECT_CONFIG_ADOPT, {"apply": True, **(arguments or {})})
    finally:
        service.close()


@pytest.fixture
def adoptable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    return placeholder_bench(tmp_path, monkeypatch, **{CONFIG_DESCRIPTION_RIGHT: True})


def test_adopting_into_an_undeclared_debugger_entry(adoptable: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    workspace, _ = adoptable
    attached(monkeypatch)
    assert_carries_entry(adopt(workspace, {"debugger_id": "second_board"}), "unknown_device")


def test_adopting_a_board_other_than_the_configured_one(adoptable: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    workspace, path = adoptable
    document = document_of(path)
    document["debuggers"]["dut"]["probe_id"] = "PROBE-OTHER-0001"
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    attached(monkeypatch)
    assert_carries_entry(adopt(workspace), "hardware_mismatch")


def _written_during_the_read(monkeypatch: pytest.MonkeyPatch, path: Path, change: Any) -> None:
    real = project_config_adopt_hardware.__globals__["discover_attached_hardware"]

    def slow(timeout_s: float = 10.0, *, probe_id: str | None = None, probe_named_by: str = "caller", before_connect: Any = None, profile: Any = None) -> dict:
        meanwhile = document_of(path)
        change(meanwhile)
        path.write_text(yaml.safe_dump(meanwhile, sort_keys=False), encoding="utf-8")
        return real(timeout_s, probe_id=probe_id, before_connect=before_connect)

    monkeypatch.setattr("agentic_hil.adopt.discover_attached_hardware", slow)


def test_adopting_while_a_carried_key_is_written_elsewhere(adoptable: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    workspace, path = adoptable
    attached(monkeypatch)
    _written_during_the_read(monkeypatch, path, lambda document: document["target"].update(controller="stm32f411re"))
    result = adopt(workspace)
    assert result.get("stale_keys"), result
    assert_carries_entry(result, "config_changed_underneath")
    assert_carries_entry(result["write"], "config_changed_underneath")


def test_adopting_while_the_document_is_changed_elsewhere(adoptable: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    workspace, path = adoptable
    attached(monkeypatch)
    _written_during_the_read(monkeypatch, path, lambda document: document["debuggers"]["dut"].update(type="pyocd"))
    result = adopt(workspace)
    assert result.get("document_changed") is True, result
    assert_carries_entry(result, "config_changed_underneath")
    assert_carries_entry(result["write"], "config_changed_underneath")


def test_adopting_into_a_configuration_that_is_not_utf8(adoptable: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    workspace, path = adoptable
    attached(monkeypatch)
    service = adopt_service(workspace)
    try:
        path.write_bytes(b"\xff\xfeversion: 2\n")
        result = service.call(PROJECT_CONFIG_ADOPT, {"apply": True})
    finally:
        service.close()
    assert_carries_entry(result, "config_unreadable")


def test_loading_a_configuration_that_is_a_directory(tmp_path: Path) -> None:
    """The loader's own refusal, as every CLI and tool path serializes it."""
    folder = tmp_path / "config.yaml"
    folder.mkdir()
    with pytest.raises(ConfigError) as refused:
        load_config(str(folder))
    assert_carries_entry(refused.value.to_dict(), "config_unreadable")


def test_adopting_when_the_record_of_the_read_cannot_be_written(adoptable: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    workspace, _ = adoptable
    attached(monkeypatch)
    monkeypatch.setattr("agentic_hil.adopt.write_report", lambda config, report: {**report, "audit_ok": False})
    assert_carries_entry(adopt(workspace), "audit_failed_after_action")


def test_adopting_when_the_audit_trail_is_unavailable(adoptable: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    workspace, _ = adoptable
    attached(monkeypatch)

    def unavailable(config: object) -> None:
        raise OSError(errno.EACCES, "Permission denied")

    monkeypatch.setattr("agentic_hil.adopt.ensure_audit_ready", unavailable)
    assert_carries_entry(adopt(workspace), "audit_unavailable")


TWO_PROBES = [
    {**NUCLEO_VCP, "serial_number": "PROBE-OTHER-0001", "hwid": "USB VID:PID=0483:374B SER=PROBE-OTHER-0001", "stable_device": None},
    {**NUCLEO_VCP, "device": "/dev/ttyACM1", "name": "ttyACM1", "serial_number": "PROBE-OTHER-0002", "hwid": "USB VID:PID=0483:374B SER=PROBE-OTHER-0002", "stable_device": None},
]


def test_adopting_with_two_probes_attached(adoptable: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    workspace, _ = adoptable
    _linux_openocd_host(monkeypatch, ports=TWO_PROBES)
    result = adopt(workspace)
    assert sorted(entry["probe_id"] for entry in result.get("probes", [])) == ["PROBE-OTHER-0001", "PROBE-OTHER-0002"], result
    assert_carries_entry(result, "ambiguous_hardware", DISCOVERY_SCOPE, key="ambiguous_hardware")


def test_discovery_with_two_probes_attached(monkeypatch: pytest.MonkeyPatch) -> None:
    _linux_openocd_host(monkeypatch, ports=TWO_PROBES)
    assert_carries_entry(discover_attached_hardware(profile=STARTER_PROFILE), "ambiguous_hardware", DISCOVERY_SCOPE, key="ambiguous_hardware")


def test_init_with_no_probe_attached(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    _fixed_stlink(monkeypatch, "No ST-LINK detected\n")
    result = init_config()
    assert result["ok"] is True, result
    assert_carries_entry(result["hardware_discovery"], "adapter_not_found", DISCOVERY_SCOPE)


def test_adopting_a_serial_that_is_not_attached(adoptable: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    workspace, _ = adoptable
    _linux_openocd_host(monkeypatch, ports=[NUCLEO_VCP])
    result = adopt(workspace, {"probe_id": "PROBE-OTHER-0009"})
    assert result.get("requested_probe_id") == "PROBE-OTHER-0009", result
    assert_carries_entry(result, "adapter_not_found", DISCOVERY_SCOPE)


def _cube_cli_answers(monkeypatch: pytest.MonkeyPatch, *answers: CompletedCommand) -> None:
    """A host with the STM32CubeProgrammer CLI, whose processes answer in turn."""
    responses = iter(answers)
    monkeypatch.setattr("agentic_hil.bootstrap.find_stm32_programmer_cli", lambda: str(Path("C:/ST/STM32_Programmer_CLI.exe")))
    monkeypatch.setattr("agentic_hil.bootstrap.spawn_command", lambda *args: next(responses))
    monkeypatch.setattr("agentic_hil.bootstrap.list_available_com_ports", lambda tool: {"ok": True, "ports": []})


def test_adopting_a_probe_with_no_target_behind_it(adoptable: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    workspace, _ = adoptable
    _cube_cli_answers(
        monkeypatch,
        CompletedCommand("ST-LINK SN : PROBE-OTHER-0003\n", "", 0, False, False),
        CompletedCommand("", "Error: No target found", 1, False, False),
    )
    result = adopt(workspace)
    assert result.get("probe_id") == "PROBE-OTHER-0003", result
    assert_carries_entry(result, "target_not_detected", DISCOVERY_SCOPE)


def test_adopting_with_a_programmer_that_will_not_run(adoptable: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    workspace, _ = adoptable
    _cube_cli_answers(monkeypatch, CompletedCommand("", "", None, False, False, not_executable_reason="permission_denied", spawn_error="Permission denied"))
    result = adopt(workspace)
    assert result.get("not_executable_reason") == "permission_denied", result
    assert_carries_entry(result, "debugger_not_executable", DISCOVERY_SCOPE)


def test_every_discovery_refusal_is_looked_up_by_its_own_type(monkeypatch: pytest.MonkeyPatch) -> None:
    """The attach rule: one lookup by the refusal's own error_type under the discovery scope.

    A type with only a bare entry is answered by it, and the moment a
    `:discovery` entry exists for it, that one answers instead. No list of types
    stands between a discovery refusal and its advice."""
    bare = ErrorRemedy(meaning="A made-up refusal.", remediation=("Do the bare thing.",), do_not=("Do not do the other thing.",))
    scoped = ErrorRemedy(meaning="A made-up discovery refusal.", remediation=("Do the discovery thing.",), do_not=("Do not do the other thing.",))
    monkeypatch.setitem(ERROR_CATALOGUE, "made_up_discovery_refusal", bare)
    refusal = bootstrap._discovery_failure("made_up_discovery_refusal", "Made up.")
    assert refusal.get("remediation") == ["Do the bare thing."], refusal
    monkeypatch.setitem(ERROR_CATALOGUE, f"made_up_discovery_refusal:{DISCOVERY_SCOPE}", scoped)
    refusal = bootstrap._discovery_failure("made_up_discovery_refusal", "Made up.")
    assert refusal.get("remediation") == ["Do the discovery thing."], refusal


def test_regenerating_when_the_record_of_the_read_cannot_be_written(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace, _ = _regenerable_bench(tmp_path, monkeypatch)
    monkeypatch.setattr("agentic_hil.adopt.write_report", lambda config, report: {**report, "audit_ok": False})
    service = AgenticHILToolService(load_authoritative_config(workspace), frontend="mcp")
    try:
        result = service.call(PROJECT_CONFIG_CREATE)
    finally:
        service.close()
    assert_carries_entry(result, "audit_failed_after_action")


def test_regenerating_when_the_terminal_record_cannot_be_written(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace, _ = _regenerable_bench(tmp_path, monkeypatch)
    monkeypatch.setattr("agentic_hil.report.write_report", lambda config, report: {**report, "audit_ok": False})
    service = AgenticHILToolService(load_authoritative_config(workspace), frontend="mcp")
    try:
        result = service.call(PROJECT_CONFIG_CREATE)
    finally:
        service.close()
    assert result.get("quarantined") is True, result
    assert_carries_entry(result, "audit_failed_after_action")


# bench_run_start: devices.py.

COM_PORT_YAML = 'com_ports:\n  dut:\n    device: "/dev/ttyAGENTIC_HILTEST"\n'
CAN_BUS_YAML = 'can_buses:\n  bench:\n    adapter: "process"\n    channel: "vcan0"\n    executable: "python"\n'


@pytest.mark.parametrize("kind", ["uart", "debugger"])
def test_a_run_declaring_a_device_the_config_does_not_have(tmp_path: Path, kind: str) -> None:
    service = AgenticHILToolService(load_config(str(write_config(tmp_path, com_ports_yaml=COM_PORT_YAML))))
    try:
        result = service.call("bench_run_start", {"devices": [{"kind": kind, "id": "ghost"}]})
    finally:
        service.close()
    assert_carries_entry(result, "unknown_device")


# The dispatcher: tools.py.


def test_a_tool_name_the_server_does_not_have(tools: AgenticHILToolService) -> None:
    assert_carries_entry(tools.call("setup"), "unknown_tool")


def test_a_tool_name_the_unprovisioned_server_does_not_have(tmp_path: Path) -> None:
    service = UnprovisionedToolService(tmp_path / "unprovisioned")
    try:
        result = service.call("setup")
    finally:
        service.close()
    assert_carries_entry(result, "unknown_tool")


def test_a_call_after_the_service_closed(tmp_path: Path) -> None:
    """Called on the Python service, because no MCP request reaches it.

    The stdio server closes its service only while the process shuts down, after
    the last request has been answered, so a client never sees this refusal over
    the wire. The service answers it all the same to any caller holding it."""
    service = AgenticHILToolService(load_config(str(write_config(tmp_path))))
    service.close()
    assert_carries_entry(service.call("probe_target"), "service_closed")


def test_a_call_after_the_service_failed_to_close(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Called on the Python service, for the same reason as the closed one.

    A failed shutdown happens only on the way out of the process, so no MCP
    request arrives after it; the refusal is reachable from Python alone."""
    service = AgenticHILToolService(load_config(str(write_config(tmp_path))))
    original = service.artifacts.close

    def stuck() -> None:
        raise RuntimeError("staging directory could not be removed")

    monkeypatch.setattr(service.artifacts, "close", stuck)
    try:
        with pytest.raises(RuntimeError):
            service.close()
        result = service.call("probe_target")
    finally:
        monkeypatch.setattr(service.artifacts, "close", original)
        service.close()
    assert_carries_entry(result, "service_cleanup_required")


@pytest.mark.parametrize(
    ("raised", "error_type", "quarantined"),
    [
        (RuntimeError("backend fell over"), "hardware_action_exception", False),
        (OSError(errno.EIO, "Input/output error"), "audit_failed_after_action", True),
    ],
)
def test_a_hardware_action_that_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, raised: Exception, error_type: str, quarantined: bool) -> None:
    service = AgenticHILToolService(load_config(str(write_config(tmp_path, auto_recover="off"))))

    def probe_target(*args: object, **kwargs: object) -> dict:
        raise raised

    monkeypatch.setattr(service.backend, "probe_target", probe_target)
    try:
        result = service.call("probe_target")
    finally:
        service.close()
    assert result.get("side_effect_status") == "unknown", result
    assert result.get("quarantined") is quarantined, result
    assert_carries_entry(result, error_type)


@pytest.mark.parametrize("tool", ["debugger_probes_list", "probe_target"])
def test_a_hardware_tool_when_the_audit_trail_is_unavailable(tools: AgenticHILToolService, monkeypatch: pytest.MonkeyPatch, tool: str) -> None:
    def unavailable(config: object) -> None:
        raise OSError(errno.EACCES, "Permission denied")

    monkeypatch.setattr("agentic_hil.tools.ensure_audit_ready", unavailable)
    assert_carries_entry(tools.call(tool), "audit_unavailable")


SPARE_DEBUGGER_YAML = 'debuggers:\n  spare:\n    type: "openocd"\n'


def test_a_debugger_tool_with_no_debugger_bound(tmp_path: Path) -> None:
    config = load_config(str(write_config(tmp_path, debuggers_yaml=SPARE_DEBUGGER_YAML)))
    assert config.debugger is None
    service = AgenticHILToolService(config)
    try:
        result = service.call("probe_target")
    finally:
        service.close()
    assert_carries_entry(result, "not_supported", "unbound_debugger")


def test_a_probe_tool_whose_bound_debugger_names_no_probe(tmp_path: Path) -> None:
    written = write_config(tmp_path, auto_probe_ids=False, debuggers_yaml=SPARE_DEBUGGER_YAML.replace('"openocd"\n', '"openocd"\n    probe_id: "PROBE-OTHER-0001"\n'))
    service = AgenticHILToolService(bind_debugger(load_config(str(written)), "dut"))
    try:
        result = service.call("reset_target", {"mode": "halt"})
    finally:
        service.close()
    assert_carries_entry(result, "not_supported", "unnamed_probe")


def test_a_debug_stop_whose_release_cannot_be_recorded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The release record fails once; the stop the remediation names settles it."""
    service = debug_service(tmp_path)
    coordinator = service.coordinator
    try:
        assert start_debug_session(service, mode="attach")["ok"] is True
        monkeypatch.setattr(coordinator, "_write_record", failing_write_record(coordinator, lambda resource, record: record.get("state") == "released"))
        result = service.call("debug_stop_session")
        monkeypatch.setattr(coordinator, "_write_record", type(coordinator)._write_record.__get__(coordinator))
        again = service.call("debug_stop_session")
    finally:
        monkeypatch.setattr(coordinator, "_write_record", type(coordinator)._write_record.__get__(coordinator))
        service.close()
    assert result.get("cleanup_reasons") == ["lease_release_unconfirmed"], result
    assert_carries_entry(result, "cleanup_required")
    assert again.get("ok") is True, again
    assert re.search(r"no debug session is active", str(again.get("summary")), re.IGNORECASE), again


def test_a_debug_stop_whose_release_cannot_be_recorded_says_what_is_unsettled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """#676: the session ended with its target state confirmed, and only the lease record is unsettled."""
    service = debug_service(tmp_path)
    coordinator = service.coordinator
    try:
        assert start_debug_session(service, mode="attach")["ok"] is True
        monkeypatch.setattr(coordinator, "_write_record", failing_write_record(coordinator, lambda resource, record: record.get("state") == "released"))
        result = service.call("debug_stop_session")
    finally:
        monkeypatch.setattr(coordinator, "_write_record", type(coordinator)._write_record.__get__(coordinator))
        service.close()
    summary = str(result.get("summary"))
    assert result.get("error_type") == "cleanup_required", result
    assert "target state remains unconfirmed" not in summary, summary
    assert re.search(r"debug session is over", summary, re.IGNORECASE), summary
    assert re.search(r"release could not be recorded", summary), summary


def test_a_probe_after_a_debugger_call_raised_does_not_blame_another_process(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """#677: an OSError in a debugger call leaves an incident this process holds; the next probe answers that."""
    service = AgenticHILToolService(load_config(str(write_config(tmp_path, auto_recover="off"))))
    original = service.backend.probe_target

    def raising(*args: object, **kwargs: object) -> dict:
        raise OSError(errno.EIO, "Input/output error")

    try:
        monkeypatch.setattr(service.backend, "probe_target", raising)
        first = service.call("probe_target")
        monkeypatch.setattr(service.backend, "probe_target", original)
        again = service.call("probe_target")
        status = service.call("hardware_lease_status")
    finally:
        monkeypatch.setattr(service.backend, "probe_target", original)
        try:
            service.close()
        finally:
            service.coordinator.close()
    assert first.get("error_type") == "audit_failed_after_action", first
    assert first.get("quarantined") is True, first
    assert status.get("incident_stands") is True, status
    assert "another Agentic HIL process" not in str(again.get("summary")), again
    assert again.get("error_type") != "resource_busy", again
    if again.get("ok") is not True:
        assert again.get("quarantine_id") == status.get("quarantine_id"), (again, status)


def test_a_debug_shutdown_whose_release_cannot_be_recorded_says_what_is_unsettled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """#676: shutdown still fails closed, and its words name the lease record rather than the target."""
    service = debug_service(tmp_path)
    coordinator = service.coordinator
    assert start_debug_session(service, mode="attach")["ok"] is True
    monkeypatch.setattr(coordinator, "_write_record", failing_write_record(coordinator, lambda resource, record: record.get("state") == "released"))
    try:
        with pytest.raises(RuntimeError) as raised:
            service.close()
    finally:
        monkeypatch.setattr(coordinator, "_write_record", type(coordinator)._write_record.__get__(coordinator))
        coordinator.close()
    message = str(raised.value)
    assert "target state remains unconfirmed" not in message, message
    assert re.search(r"release could not be recorded", message), message


# audit_unavailable from the other modules that build it.


def test_a_pyocd_command_whose_probe_listing_cannot_be_logged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    backend = PyOCDBackend(load_config(str(write_config(tmp_path, debugger_type="pyocd", probe_id="PYOCD123", target_type="stm32f446re"))))
    executable = tmp_path / "pyocd.exe"
    listing = '{"status": 0, "boards": [{"unique_id": "PYOCD123"}]}\n'
    monkeypatch.setattr(backend, "_resolve_executable", lambda: {"ok": True, "executable_path": str(executable), "executable": str(executable)})
    monkeypatch.setattr(
        "agentic_hil.backends.pyocd.spawn_command",
        lambda command, *args, **kwargs: CompletedCommand(stdout=listing if "json" in command else "", stderr="", returncode=0, timed_out=False, not_found=False),
    )
    monkeypatch.setattr(backend, "_write_log", lambda *args, **kwargs: OSError("action log could not be written"))
    # The site names its backend as the scope; the bare entry answers it.
    assert_carries_entry(backend.probe_target(), "audit_unavailable", "pyocd", key="audit_unavailable")


def _log_directory_refused(config: object) -> str:
    raise ConfigError("unsafe_configured_path", "The log directory is not a safe configured path.")


def test_a_com_session_whose_log_cannot_be_created(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("agentic_hil.comports.logs_directory", _log_directory_refused)
    service = ComPortService(load_config(str(write_config(tmp_path, com_ports_yaml=COM_PORT_YAML))))
    try:
        result = service.session_start("dut")
    finally:
        service.close()
    assert_carries_entry(result, "audit_unavailable")


def test_a_can_session_whose_log_cannot_be_created(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("agentic_hil.can.logs_directory", _log_directory_refused)
    service = CanBusService(load_config(str(write_config(tmp_path, can_buses_yaml=CAN_BUS_YAML))))
    try:
        result = service.session_start("bench")
    finally:
        service.close()
    assert_carries_entry(result, "audit_unavailable")
