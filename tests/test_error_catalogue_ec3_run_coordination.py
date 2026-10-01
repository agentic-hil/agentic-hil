"""Every refusal a run, the coordinator, a recovery, a JUnit write or the test
reactor hands back has an entry in the error catalogue, and the entry arrives.

A refusal with no entry leaves its reader with a summary and a guess. The guess
for these refusals is expensive: a run that cannot be found gets restarted on a
bench another run still holds, a recovery that could not write its ledger gets
retried around the ledger, a quarantine whose markers changed gets cleared by
hand. So the inventory of what these modules can refuse with is derived from
their source, pinned so the scan cannot shrink without anybody noticing, and
held against the catalogue three ways: the entry exists and is complete, the
reference resource serves it, and a real refusal through the real code path
carries its fix.

Values the scan finds that never reach a caller as a refusal of their own are
listed with the reason they do not, so excluding one is a decision somebody
wrote down rather than a gap.
"""
from __future__ import annotations

import ast
import importlib
import json
import subprocess
import sys
from functools import lru_cache
from pathlib import Path

import pytest
from conftest import write_authoritative_config, write_config
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

from agentic_hil import runlifecycle
from agentic_hil.cli import build_parser, dispatch, entrypoint
from agentic_hil.config import load_authoritative_config, load_config
from agentic_hil.coordination import DEBUGGER_DISCOVERY_RESOURCE, LEASE_RELEASE_RETRY_REASON, HardwareCoordinator
from agentic_hil.knowledge import ERROR_CATALOGUE, ERROR_URI_PREFIX, lookup_remedy, remediation_fields
from agentic_hil.mcp import handle_mcp_message
from agentic_hil.test_reactor import TestReactor, load_test_config
from agentic_hil.tools import AgenticHILToolService

# ---------------------------------------------------------------------------
# The inventory.

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
    }
)

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
    "audit_failed",
    "step_failed",
)
REQUIRED_TYPES = frozenset(key.partition(":")[0] for key in REQUIRED_KEYS)

# Found by the scan, never a refusal a caller receives under that name.
EXCLUDED: dict[str, str] = {
    "operator_confirmation_required": "every caller of HardwareCoordinator.recover passes safe_state_confirmed=True, and the recover command's parser requires --confirm-safe-state before the coordinator is reached",
    "coordination_closed": "only AgenticHILToolService.close closes the coordinator, and service.call answers service_closed before any tool reaches it",
    "unknown": "the run record's fallback for a run that never finished, but run_plan always calls finish and every failing reactor result names its own error_type",
    "target_stop": "fallback for a debug result with target_ok false and no target_error_type, and every debug result with target_ok false names its target_error_type",
    "target_failed": "the same fallback in result_error_type, unreachable for the same reason as target_stop",
    "not_supported": "a step action the device kind does not serve is refused by the plan loader as test_config_invalid before the reactor dispatches it",
    "unknown_action": "an action no device kind declares is refused by the plan loader as test_config_invalid before the reactor dispatches it",
    "test_config_schema_invalid": "the bundled plan schema failing to load is a broken installation, like config_schema_invalid, not a refusal a plan or a caller can cause",
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

# The sites whose error_type is not a literal, as (module, expression). Each is
# a value that came from somewhere the scan already covers or names: a
# ConfigError re-raised under its own type, a Python exception class name in a
# log line, a finished run's own error_type echoed back, the reactor's step
# result passed up, or a class constant the scan collects separately.
PINNED_DYNAMIC_SITES = frozenset(
    {
        ("coordination", "error.error_type"),
        ("coordination", "type(error).__name__"),
        ("coordination", "type(persist_error).__name__"),
        ("runlifecycle", "record.get('error_type')"),
        ("runlifecycle", "result.get('error_type')"),
        ("test_reactor", "error_type"),
        ("test_reactor", "refused_as"),
        ("test_reactor", "result.get('target_error_type')"),
        ("test_reactor", "result.get('target_error_type', 'target_stop')"),
        ("test_reactor", "result['error_type']"),
        ("test_reactor", "result_error_type(cleared)"),
        ("test_reactor", "result_error_type(failure or {})"),
        ("test_reactor", "self.not_owned_error"),
        ("test_reactor", "step_error_type"),
    }
)


def _callee(call: ast.Call) -> str | None:
    function = call.func
    if isinstance(function, ast.Name):
        return function.id
    if isinstance(function, ast.Attribute):
        return function.attr
    return None


def _values(node: ast.expr, module: object) -> tuple[set[str], set[str]]:
    """The literal error_types an expression can be, and what it leaves unresolved."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return {node.value}, set()
    if isinstance(node, ast.Name):
        value = getattr(module, node.id, None)
        if isinstance(value, str):
            return {value}, set()
        return set(), {ast.unparse(node)}
    if isinstance(node, ast.IfExp):
        body, orelse = _values(node.body, module), _values(node.orelse, module)
        return body[0] | orelse[0], body[1] | orelse[1]
    if isinstance(node, ast.BoolOp):
        constants: set[str] = set()
        dynamic: set[str] = set()
        for value in node.values:
            found = _values(value, module)
            constants |= found[0]
            dynamic |= found[1]
        return constants, dynamic
    if isinstance(node, ast.Call) and _callee(node) == "str" and len(node.args) == 1:
        return _values(node.args[0], module)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "get" and len(node.args) == 2:
        return _values(node.args[1], module)[0], {ast.unparse(node)}
    return set(), {ast.unparse(node)}


def _functions_taking_an_error_type() -> set[str]:
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
    return names


@lru_cache(maxsize=1)
def scan() -> tuple[dict[str, frozenset[str]], frozenset[tuple[str, str]], frozenset[tuple[str, str, int]]]:
    """Every error_type the six modules can set, from their source.

    Answers the literal values with the sites that set them, the expressions
    that are not literals, and any call to a function taking an `error_type`
    that is neither a known producer nor a known consumer, which is a refusal
    the scan would otherwise miss."""
    takes_an_error_type = _functions_taking_an_error_type()
    constants: dict[str, set[str]] = {}
    dynamic: set[tuple[str, str]] = set()
    unclassified: set[tuple[str, str, int]] = set()
    for name in SCANNED_MODULES:
        module = importlib.import_module(f"agentic_hil.{name}")
        tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))

        def add(node: ast.expr, line: int, *, name: str = name, module: object = module) -> None:
            found, unresolved = _values(node, module)
            for value in found:
                constants.setdefault(value, set()).add(f"{name}.py:{line}")
            for expression in unresolved:
                dynamic.add((name, expression))

        for node in ast.walk(tree):
            if isinstance(node, ast.Dict):
                for key, value in zip(node.keys, node.values, strict=True):
                    if isinstance(key, ast.Constant) and key.value == "error_type":
                        add(value, value.lineno)
            elif isinstance(node, ast.Call):
                callee = _callee(node)
                if callee in takes_an_error_type and callee not in PRODUCERS and callee not in CONSUMERS:
                    unclassified.add((name, callee, node.lineno))
                if callee not in CONSUMERS:
                    for keyword in node.keywords:
                        if keyword.arg == "error_type":
                            add(keyword.value, keyword.value.lineno)
                if callee in PRODUCERS and len(node.args) > PRODUCERS[callee]:
                    add(node.args[PRODUCERS[callee]], node.lineno)
                if (
                    isinstance(node.func, ast.Attribute)
                    and node.func.attr == "setdefault"
                    and len(node.args) == 2
                    and isinstance(node.args[0], ast.Constant)
                    and node.args[0].value == "error_type"
                ):
                    add(node.args[1], node.lineno)
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Subscript) and isinstance(target.slice, ast.Constant) and target.slice.value == "error_type":
                        add(node.value, node.lineno)
            if isinstance(node, ast.ClassDef):
                # Class constants a shared method refuses with, such as the
                # session kinds' `not_owned_error`.
                for item in node.body:
                    if isinstance(item, ast.AnnAssign):
                        target, value = item.target, item.value
                    elif isinstance(item, ast.Assign) and len(item.targets) == 1:
                        target, value = item.targets[0], item.value
                    else:
                        continue
                    if isinstance(target, ast.Name) and target.id.endswith("_error") and isinstance(value, ast.Constant) and isinstance(value.value, str) and value.value:
                        constants.setdefault(value.value, set()).add(f"{name}.py:{item.lineno}")
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.endswith("error_type"):
                for inner in ast.walk(node):
                    if isinstance(inner, ast.Return) and inner.value is not None:
                        add(inner.value, inner.lineno)
    return {value: frozenset(sites) for value, sites in constants.items()}, frozenset(dynamic), frozenset(unclassified)


def test_the_three_classes_do_not_overlap() -> None:
    assert not REQUIRED_TYPES & set(EXCLUDED)
    assert not REQUIRED_TYPES & ALREADY_COVERED
    assert not set(EXCLUDED) & ALREADY_COVERED


def test_the_scan_finds_exactly_the_pinned_inventory() -> None:
    """A value the scan stops finding, or a new one nobody classified, fails here.

    New refusals in these modules are the point: each has to be put in one of
    the three classes, which for a refusal that reaches a caller means writing
    its entry."""
    constants, _, _ = scan()
    found = set(constants)
    expected = REQUIRED_TYPES | set(EXCLUDED) | ALREADY_COVERED

    assert sorted(found - expected) == [], {value: sorted(constants[value]) for value in found - expected}
    assert sorted(expected - found) == []


def test_every_non_literal_error_type_site_is_one_somebody_looked_at() -> None:
    _, dynamic, _ = scan()

    assert sorted(dynamic - PINNED_DYNAMIC_SITES) == []
    assert sorted(PINNED_DYNAMIC_SITES - dynamic) == []


def test_no_call_takes_an_error_type_the_scan_does_not_read() -> None:
    _, _, unclassified = scan()

    assert sorted(unclassified) == []


def test_every_exclusion_says_why() -> None:
    for error_type, reason in EXCLUDED.items():
        assert reason.strip(), error_type


def test_the_already_covered_types_resolve() -> None:
    for error_type in sorted(ALREADY_COVERED):
        assert lookup_remedy(error_type) is not None, error_type


def test_every_refusal_found_in_these_modules_has_an_entry() -> None:
    """The guard: a refusal these modules can return has a catalogue entry."""
    constants, _, _ = scan()
    missing = {}
    for key in REQUIRED_KEYS:
        error_type = key.partition(":")[0]
        if key not in ERROR_CATALOGUE:
            missing[key] = sorted(constants.get(error_type, ()))

    assert missing == {}, f"refusals with no catalogue entry: {missing}"


@pytest.mark.parametrize("key", REQUIRED_KEYS)
def test_each_entry_says_what_happened_what_to_do_and_what_not_to(key: str) -> None:
    remedy = ERROR_CATALOGUE.get(key)

    assert remedy is not None, f"{key} has no catalogue entry"
    assert remedy.meaning.strip()
    assert remedy.remediation and all(step.strip() for step in remedy.remediation)
    assert remedy.do_not and all(step.strip() for step in remedy.do_not)


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


def assert_refusal_carries_its_entry(payload: dict, error_type: str, scope: str | None = None) -> None:
    """The refusal is the one named, its entry exists, and the entry's fix is on it."""
    assert payload.get("ok") is False, payload
    assert payload.get("error_type") == error_type, payload
    key = f"{error_type}:{scope}" if scope else error_type
    catalogued = key in ERROR_CATALOGUE
    assert catalogued, f"{key} reached a caller with no catalogue entry: {payload.get('summary')!r}"
    expected = remediation_fields(error_type, scope)
    assert payload.get("remediation") == expected.get("remediation"), payload
    assert payload.get("do_not") == expected.get("do_not"), payload


# --- test_reactor_status and test_reactor_stop over MCP ---------------------

UNKNOWN_HANDLE = "run-00000000000000ff"


def test_status_of_a_handle_the_bench_never_saw(tmp_path: Path) -> None:
    service = AgenticHILToolService(load_config(str(write_config(tmp_path))))
    try:
        result = service.call("test_reactor_status", {"run": UNKNOWN_HANDLE})
    finally:
        service.close()

    assert_refusal_carries_its_entry(result, "run_not_found")


def test_status_of_a_record_this_version_cannot_read(tmp_path: Path) -> None:
    config = load_config(str(write_config(tmp_path)))
    handle = runlifecycle.new_run_handle()
    path = runlifecycle.record_path(config, handle)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"version": 999}\n', encoding="utf-8")
    service = AgenticHILToolService(config)
    try:
        result = service.call("test_reactor_status", {"run": handle})
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

    assert_refusal_carries_its_entry(result, "run_worker_failed")


class WorkerThatNeverPublishes:
    """A spawned worker that stays alive and never writes its record."""

    def poll(self) -> None:
        return None


def test_detached_start_whose_worker_never_says_anything(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace, plan = bench_workspace(tmp_path, monkeypatch, RESET_PLAN)
    monkeypatch.setattr(runlifecycle, "WORKER_PUBLISH_TIMEOUT_S", 0.3)
    monkeypatch.setattr(runlifecycle, "spawn_run_worker", lambda *_args, **_kwargs: WorkerThatNeverPublishes())
    service = AgenticHILToolService(load_authoritative_config(workspace))
    try:
        result = service.call("test_reactor_run", {"test_config_path": str(plan), "detach": True})
    finally:
        service.close()

    assert_refusal_carries_its_entry(result, "run_worker_unresponsive")


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

    assert result["cleanup_errors"][-1]["result"]["error_type"] == "cleanup_exception", result
    assert_refusal_carries_its_entry(result, "cleanup_failed", "test_reactor")


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
    standing_incident(config, "physical:ec3-catalogue-audit")
    original = coordination.safe_append_text

    def ledger_refuses_the_recovery(path, text, *args, **kwargs):
        if "mcp:hardware_recover" in str(text):
            raise OSError("ledger denied")
        return original(path, text, *args, **kwargs)

    monkeypatch.setattr("agentic_hil.coordination.safe_append_text", ledger_refuses_the_recovery)
    service = AgenticHILToolService(config)
    try:
        result = service.call("hardware_recover", {})
    finally:
        service.close()

    assert_refusal_carries_its_entry(result, "recovery_audit_failed")


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


# --- a coordinator refusal through a hardware tool ------------------------------


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
    assert_refusal_carries_its_entry(json.loads(capsys.readouterr().out), "coordination_state_invalid")


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


def test_junit_file_that_cannot_be_written(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    write_authoritative_config(tmp_path, monkeypatch)
    plan = write_plan(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "agentic_hil.reactorrun.run_registered_plan",
        lambda *_args, **_kwargs: {"ok": True, "tool": "test_reactor", "name": "testconfig", "cleanup_ok": True, "steps": []},
    )

    def refusing_write(*_args, **_kwargs) -> str:
        raise OSError("read-only file system")

    monkeypatch.setattr("agentic_hil.junit.write_junit_xml", refusing_write)

    exit_code = entrypoint(["test-reactor", "--test-config", str(plan), "--junit-xml", str(tmp_path / "junit.xml"), "--json"])

    assert exit_code == 1
    assert_refusal_carries_its_entry(json.loads(capsys.readouterr().out), "junit_xml_write_failed")


# --- the reactor's own refusals ---------------------------------------------------


def run_plan_text(config, tmp_path: Path, text: str, service: RecordingService, **reactor_kwargs) -> dict:
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


def test_a_preflight_that_raised(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def raising_preflight(self, test_config):
        raise RuntimeError("preflight broke")

    monkeypatch.setattr(TestReactor, "preflight", raising_preflight)

    result = run_plan_text(load_config(str(write_config(tmp_path))), tmp_path, RESET_PLAN, RecordingService())

    assert_refusal_carries_its_entry(result, "preflight_exception")


def test_a_run_asked_to_stop(tmp_path: Path) -> None:
    result = run_plan_text(load_config(str(write_config(tmp_path))), tmp_path, RESET_PLAN, RecordingService(), stop_requested=lambda: True)

    assert result.get("stopped") is True, result
    assert_refusal_carries_its_entry(result, "run_stopped")


def test_a_step_whose_evidence_could_not_be_written(tmp_path: Path) -> None:
    result = run_plan_text(
        load_config(str(write_config(tmp_path))),
        tmp_path,
        "version: 2\nsteps:\n  - {debugger: dut, action: flash, image_path: build/app.elf}\n",
        RecordingService(audit_flash_failure=True),
    )

    assert_refusal_carries_its_entry(result, "audit_failed")


def test_a_step_that_left_its_lease_needing_cleanup(tmp_path: Path) -> None:
    # The shape a hardware tool answers with when its call worked and giving
    # the lease back did not: `ok` true, the lease's own status merged in, and no
    # error_type of its own.
    reset = {"ok": True, "tool": "reset_target", "lease_state": "cleanup_required", "cleanup_required": True, "quarantined": True}

    result = run_plan_text(load_config(str(write_config(tmp_path))), tmp_path, RESET_PLAN, RecordingService(reset_result=reset))

    assert_refusal_carries_its_entry(result, "step_failed")


def test_a_run_whose_device_cleanup_failed(tmp_path: Path) -> None:
    result = run_plan_text(
        load_config(str(write_config(tmp_path))),
        tmp_path,
        "version: 2\nsteps:\n  - {debugger: dut, action: debug_start, image_path: build/app.elf}\n",
        RecordingService(fail_cleanup=True),
    )

    assert result["cleanup_errors"][0]["result"]["error_type"] == "cleanup_exception", result
    assert_refusal_carries_its_entry(result, "cleanup_failed", "test_reactor")
