"""Catalogue entries for artifact, report, adoption, configuration-write and dispatcher refusals (#645).

Three things are pinned here.

The inventory. Every `error_type` value the scanned modules can put into a
result is found by an AST scan of their source and compared with the inventory
written down below, so a refusal added to one of them cannot ship without being
classified, and the scan cannot quietly stop finding values either. Every value
is in exactly one class: an entry this issue adds, an entry that already exists,
a value only a scoped key answers, a value another issue owns, or a value that
never reaches the top level of a tool result (each with its reason).

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
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml
from conftest import write_config
from test_agent_provisioning import _regenerable_bench
from test_bootstrap import NUCLEO_VCP, STARTER_PROFILE, _linux_openocd_host
from test_config_adopt import attached, document_of, placeholder_bench
from test_config_adopt import service as adopt_service
from test_coordination import failing_write_record
from test_debug_sessions import debug_service, start_debug_session
from test_flash_capture import PORT_ID, flash_args
from test_flash_capture import config_for as capture_config
from test_mcp_reference_resources import read_text

from agentic_hil.adopt import PROJECT_CONFIG_ADOPT, project_config_adopt_hardware
from agentic_hil.backends.common import CompletedCommand
from agentic_hil.backends.pyocd import PyOCDBackend
from agentic_hil.bootstrap import discover_attached_hardware
from agentic_hil.can import CanBusService
from agentic_hil.comports import ComPortService
from agentic_hil.config import ConfigError, bind_debugger, load_authoritative_config, load_config
from agentic_hil.knowledge import (
    CONFIG_DESCRIPTION_RIGHT,
    ERROR_CATALOGUE,
    ERROR_URI_PREFIX,
    catalogue_entry,
    remediation_fields,
)
from agentic_hil.report import report_state_path
from agentic_hil.tools import PROJECT_CONFIG_CREATE, AgenticHILToolService, UnprovisionedToolService

# ---------------------------------------------------------------------------
# The inventory.

SCANNED_MODULES = ("adopt", "artifacts", "bootstrap", "configwrite", "devices", "report", "tools")

# Callables that take the error type positionally, by the index of that argument.
POSITIONAL_ERROR_TYPE = {"ConfigError": 0, "tool_error": 1, "_validation_error": 2, "_discovery_failure": 0}

DYNAMIC = "<dynamic>"
FORWARDED = "<forwarded>"


@dataclass(frozen=True)
class Site:
    module: str
    function: str
    value: str
    line: int


def _callee(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _parameters(function: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    arguments = function.args
    names = {argument.arg for argument in (*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs)}
    names.update(argument.arg for argument in (arguments.vararg, arguments.kwarg) if argument is not None)
    return names


def _bindings(function: ast.FunctionDef | ast.AsyncFunctionDef, name: str) -> list[ast.expr | None]:
    """What a local name is bound to inside a function; None for a binding no value maps to."""
    found: list[ast.expr | None] = []
    for node in ast.walk(function):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    found.append(node.value)
                elif isinstance(target, (ast.Tuple, ast.List)) and any(isinstance(item, ast.Name) and item.id == name for item in ast.walk(target)):
                    found.append(None)
        elif isinstance(node, (ast.AnnAssign, ast.NamedExpr)) and isinstance(node.target, ast.Name) and node.target.id == name and node.value is not None:
            found.append(node.value)
        elif isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)) and any(isinstance(item, ast.Name) and item.id == name for item in ast.walk(node.target)):
            found.append(None)
    return found


class Scanner:
    """Every place one module writes an `error_type`, and the values it can write there."""

    def __init__(self, module_name: str) -> None:
        module = importlib.import_module(f"agentic_hil.{module_name}")
        self.module_name = module_name
        self.namespace = vars(module)
        self.tree = ast.parse(Path(str(module.__file__)).read_text(encoding="utf-8"))
        self.parents = {child: parent for parent in ast.walk(self.tree) for child in ast.iter_child_nodes(parent)}
        self.sites: list[Site] = []

    def scopes(self, node: ast.AST) -> list[ast.AST]:
        chain = []
        while node in self.parents:
            node = self.parents[node]
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                chain.append(node)
        return chain

    def qualname(self, node: ast.AST) -> str:
        return ".".join(reversed([scope.name for scope in self.scopes(node)]))  # type: ignore[attr-defined]

    def resolve(self, node: ast.expr | None, seen: frozenset = frozenset()) -> set[str]:
        if node is None:
            return {DYNAMIC}
        if isinstance(node, ast.Constant):
            if node.value is None:
                return set()
            return {node.value} if isinstance(node.value, str) else {DYNAMIC}
        if isinstance(node, ast.IfExp):
            return self.resolve(node.body, seen) | self.resolve(node.orelse, seen)
        if isinstance(node, ast.BoolOp):
            return set().union(*(self.resolve(value, seen) for value in node.values))
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id == "str" and len(node.args) == 1:
                return self.resolve(node.args[0], seen)
            if isinstance(node.func, ast.Attribute) and node.func.attr == "get" and len(node.args) == 2:
                return {DYNAMIC} | self.resolve(node.args[1], seen)
            return {DYNAMIC}
        if isinstance(node, ast.Name):
            for scope in self.scopes(node):
                if isinstance(scope, ast.ClassDef):
                    continue
                if node.id in _parameters(scope):  # type: ignore[arg-type]
                    return {FORWARDED}
                bound = _bindings(scope, node.id)  # type: ignore[arg-type]
                if bound:
                    if (scope, node.id) in seen:
                        return set()
                    return set().union(*(self.resolve(value, seen | {(scope, node.id)}) for value in bound))
            value = self.namespace.get(node.id)
            return {value} if isinstance(value, str) else {DYNAMIC}
        return {DYNAMIC}

    def record(self, node: ast.expr, function: str | None = None) -> None:
        where = self.qualname(node) if function is None else function
        for value in self.resolve(node):
            self.sites.append(Site(self.module_name, where, value, node.lineno))

    def scan(self) -> list[Site]:
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Dict):
                for key, value in zip(node.keys, node.values, strict=True):
                    if isinstance(key, ast.Constant) and key.value == "error_type":
                        self.record(value)
            elif isinstance(node, ast.Call):
                for keyword in node.keywords:
                    if keyword.arg == "error_type":
                        self.record(keyword.value)
                index = POSITIONAL_ERROR_TYPE.get(_callee(node.func) or "")
                if index is not None and len(node.args) > index and not any(isinstance(argument, ast.Starred) for argument in node.args[: index + 1]):
                    self.record(node.args[index])
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Subscript) and isinstance(target.slice, ast.Constant) and target.slice.value == "error_type":
                        self.record(node.value)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                arguments = node.args
                positional = [*arguments.posonlyargs, *arguments.args]
                defaults = list(zip(positional[len(positional) - len(arguments.defaults) :], arguments.defaults, strict=True))
                defaults += [(argument, default) for argument, default in zip(arguments.kwonlyargs, arguments.kw_defaults, strict=True) if default is not None]
                for argument, default in defaults:
                    if argument.arg == "error_type":
                        self.record(default, function=f"{self.qualname(node)}.{node.name}".lstrip("."))
        return self.sites


def scan_all() -> list[Site]:
    return [site for module in SCANNED_MODULES for site in Scanner(module).scan()]


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
            "report_unreadable",
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
        "cleanup_required",
        "config_changed_underneath",
        "config_unreadable",
        "hardware_action_exception",
        "hardware_mismatch",
        "output_validation_failed",
        "report_not_found",
        "report_unreadable",
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

# Already in the catalogue as a bare entry; this issue relies on them unchanged.
COVERED = frozenset(
    {
        "config_file_not_found",
        "config_invalid",
        "config_write_in_open_run",
        "debugger_not_executable",
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

# Answered by scoped keys only; the discovery sites name no scope today.
SCOPED_ONLY = frozenset({"adapter_not_found", "target_not_detected"})

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
}

# Values that never reach the top level of a tool result.
EXCLUDED_VALUES: dict[str, str] = {
    "canonical_write_pending": "the nested audit_error marker of a staged per-run report copy (report.staged_report_snapshot), never a result's own error_type",
    "unknown_debugger_error": "a classification label inside the ok:true answer of classify_last_error, not a refusal",
}

# Sites whose value is not a literal, each with why no entry follows from it.
DYNAMIC_SITES: dict[tuple[str, str], str] = {
    ("report", "classify_failure_report"): "hands back the error_type of the failure it classifies, which is some other tool's",
    ("report", "audit_unavailable"): "the nested audit_error names the exception class; the top level is the literal audit_unavailable",
    ("report", "mark_audit_failure"): "the nested audit_error names the exception class or its ConfigError type",
    ("tools", "AgenticHILToolService._capture_result"): "passes a COM session's own refusal through (#635)",
    ("tools", "AgenticHILToolService._dispatch_tool"): "the nested audit_error names the exception class; the top level is one of two literals",
}

# Helpers that forward an error_type parameter; their callers are scanned instead.
FORWARDING_SITES = frozenset(
    {
        ("artifacts", "ArtifactManager._validation_error"),
        ("bootstrap", "_discovery_failure"),
        ("tools", "tool_error"),
    }
)

NEW_KEYS = tuple(sorted(NEW_ENTRIES | set(SCOPED_SITES.values())))


@pytest.fixture(scope="module")
def sites() -> list[Site]:
    return scan_all()


def test_the_scan_finds_the_pinned_inventory(sites: list[Site]) -> None:
    found = {module: frozenset(site.value for site in sites if site.module == module and site.value not in {DYNAMIC, FORWARDED}) for module in SCANNED_MODULES}
    for module in SCANNED_MODULES:
        missing = sorted(EXPECTED_INVENTORY[module] - found[module])
        added = sorted(found[module] - EXPECTED_INVENTORY[module])
        assert not missing, f"{module}: the scan no longer finds {missing}"
        assert not added, f"{module}: new error_type values {added}; classify each below and give it an entry or a reason"


def test_every_value_is_classified_exactly_once(sites: list[Site]) -> None:
    classes = {
        "new": NEW_ENTRIES,
        "scoped by site": frozenset({SCOPED_VALUE}),
        "covered": COVERED,
        "scoped only": SCOPED_ONLY,
        "owned elsewhere": frozenset(OWNED_ELSEWHERE),
        "excluded": frozenset(EXCLUDED_VALUES),
    }
    for value in sorted(literal_values(sites)):
        holders = [name for name, members in classes.items() if value in members]
        assert len(holders) == 1, f"{value} is classified as {holders or 'nothing'}"
    everything = frozenset().union(*classes.values())
    assert everything <= literal_values(sites), f"classified but not in the code: {sorted(everything - literal_values(sites))}"
    for reason in (*EXCLUDED_VALUES.values(), *EXCLUDED_SITES.values(), *DYNAMIC_SITES.values()):
        assert reason.strip()


def test_every_not_supported_site_has_its_own_scope_or_a_reason(sites: list[Site]) -> None:
    found = {(site.module, site.function) for site in sites if site.value == SCOPED_VALUE}
    assert found == set(SCOPED_SITES) | set(EXCLUDED_SITES)


def test_every_non_literal_site_is_named(sites: list[Site]) -> None:
    assert {(site.module, site.function) for site in sites if site.value == DYNAMIC} == set(DYNAMIC_SITES)
    assert {(site.module, site.function) for site in sites if site.value == FORWARDED} == set(FORWARDING_SITES)


def test_the_entries_this_issue_relies_on_are_there() -> None:
    assert sorted(value for value in COVERED if value not in ERROR_CATALOGUE) == []
    for value in sorted(SCOPED_ONLY):
        assert any(key.startswith(f"{value}:") for key in ERROR_CATALOGUE), value


@pytest.mark.parametrize("key", NEW_KEYS)
def test_each_new_entry_is_complete(key: str) -> None:
    assert key in ERROR_CATALOGUE, f"no catalogue entry for {key}"
    entry = catalogue_entry(key)
    assert entry["meaning"].strip(), key
    assert entry["remediation"].strip(), key
    assert entry.get("do_not"), f"{key} names no wrong fix"
    assert all(item.strip() for item in entry["do_not"]), key


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
    when a scope is given. A site that passes its backend as the scope and is
    answered by the bare entry names the bare key here."""
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
    assert_carries_entry(tools.call("flash_firmware", {"image_path": "build/app.elf"}), "artifact_changed")


def test_flash_firmware_when_the_image_cannot_be_staged(tmp_path: Path, tools: AgenticHILToolService, monkeypatch: pytest.MonkeyPatch) -> None:
    firmware(tmp_path)

    def no_space(*args: object, **kwargs: object) -> str:
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(
        "agentic_hil.artifacts.tempfile",
        SimpleNamespace(mkdtemp=no_space, NamedTemporaryFile=tempfile.NamedTemporaryFile, TemporaryDirectory=tempfile.TemporaryDirectory, gettempdir=tempfile.gettempdir),
    )
    assert_carries_entry(tools.call("flash_firmware", {"image_path": "build/app.elf"}), "artifact_staging_failed")


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
def test_a_report_tool_whose_state_the_os_refuses_to_read(tools: AgenticHILToolService, monkeypatch: pytest.MonkeyPatch, tool: str) -> None:
    state_file = Path(report_state_path(tools.config))
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text("{}", encoding="utf-8")

    def refused(path: object, *args: object, **kwargs: object) -> str:
        raise PermissionError(errno.EACCES, "Permission denied", str(path))

    monkeypatch.setattr("agentic_hil.report.safe_read_text", refused)
    assert_carries_entry(tools.call(tool), "report_unreadable")


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


def test_adopting_while_the_document_is_changed_elsewhere(adoptable: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    workspace, path = adoptable
    attached(monkeypatch)
    _written_during_the_read(monkeypatch, path, lambda document: document["debuggers"]["dut"].update(type="pyocd"))
    result = adopt(workspace)
    assert result.get("document_changed") is True, result
    assert_carries_entry(result, "config_changed_underneath")


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
    assert_carries_entry(result, "ambiguous_hardware")


def test_discovery_with_two_probes_attached(monkeypatch: pytest.MonkeyPatch) -> None:
    _linux_openocd_host(monkeypatch, ports=TWO_PROBES)
    assert_carries_entry(discover_attached_hardware(profile=STARTER_PROFILE), "ambiguous_hardware")


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
    service = AgenticHILToolService(load_config(str(write_config(tmp_path))))
    service.close()
    assert_carries_entry(service.call("probe_target"), "service_closed")


def test_a_call_after_the_service_failed_to_close(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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
    ("raised", "error_type"),
    [(RuntimeError("backend fell over"), "hardware_action_exception"), (OSError(errno.EIO, "Input/output error"), "audit_failed_after_action")],
)
def test_a_hardware_action_that_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, raised: Exception, error_type: str) -> None:
    service = AgenticHILToolService(load_config(str(write_config(tmp_path, auto_recover="off"))))

    def probe_target(*args: object, **kwargs: object) -> dict:
        raise raised

    monkeypatch.setattr(service.backend, "probe_target", probe_target)
    try:
        result = service.call("probe_target")
    finally:
        service.close()
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
    service = debug_service(tmp_path)
    coordinator = service.coordinator
    try:
        assert start_debug_session(service, mode="attach")["ok"] is True
        monkeypatch.setattr(coordinator, "_write_record", failing_write_record(coordinator, lambda resource, record: record.get("state") == "released"))
        result = service.call("debug_stop_session")
    finally:
        monkeypatch.setattr(coordinator, "_write_record", type(coordinator)._write_record.__get__(coordinator))
        service.close()
    assert_carries_entry(result, "cleanup_required")


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
