"""Temporary, payload-free pytest profiling plugin for #536 investigation.

Load with ``-p ci_debug_phase_profile`` and set
``AGENTIC_HIL_PHASE_PROFILE_ROOT`` to a writable artifact directory. Each xdist
worker writes its own JSONL file. The plugin records durations and bounded
operation labels only: no commands, arguments, paths, process ids or env values.
"""

from __future__ import annotations

import inspect
import json
import os
import platform
import shutil
import time
from contextvars import ContextVar
from pathlib import Path
from typing import Any

import pytest

_ITEM: str | None = None
_EVENTS: list[dict[str, Any]] = []
_COUNTERS: dict[tuple[int, str], int] = {}
_SPAN_SEQUENCE = 0
_SPAN_STACK: ContextVar[tuple[tuple[int, str], ...]] = ContextVar("phase_profile_span_stack", default=())


def _record(
    phase: str,
    started: float,
    *,
    timeout_s: float | None = None,
    ordinal: int | None = None,
    span_id: int | None = None,
    parent_span: tuple[int, str] | None = None,
) -> None:
    if _ITEM is None:
        return
    event: dict[str, Any] = {"phase": phase, "duration_s": time.perf_counter() - started}
    if timeout_s is not None:
        event["timeout_budget_s"] = float(timeout_s)
    if ordinal is not None:
        event["ordinal"] = ordinal
    if span_id is not None:
        event["span_id"] = span_id
    if parent_span is None:
        stack = _SPAN_STACK.get()
        parent_span = stack[-1] if stack else None
    if parent_span is not None:
        event["parent_span_id"] = parent_span[0]
        event["parent_phase"] = parent_span[1]
    _EVENTS.append(event)


def _argument_value(function: Any, name: str | None, args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
    if name is None:
        return None
    if name in kwargs:
        return kwargs[name]
    try:
        position = tuple(inspect.signature(function).parameters).index(name)
    except (TypeError, ValueError):
        return None
    return args[position] if position < len(args) else None


def _wrap_method(monkeypatch: pytest.MonkeyPatch, owner: Any, name: str, phase: str, *, budget_arg: str | None = None) -> None:
    original = getattr(owner, name)

    def timed(*args: Any, **kwargs: Any) -> Any:
        global _SPAN_SEQUENCE
        started = time.perf_counter()
        budget = _argument_value(original, budget_arg, args, kwargs)
        ordinal = None
        if phase in {"mi_command_wait", "service_call", "service_close"} and args:
            key = (id(args[0]), phase)
            ordinal = _COUNTERS.get(key, 0) + 1
            _COUNTERS[key] = ordinal
        stack = _SPAN_STACK.get()
        _SPAN_SEQUENCE += 1
        span_id = _SPAN_SEQUENCE
        token = _SPAN_STACK.set((*stack, (span_id, phase)))
        try:
            return original(*args, **kwargs)
        finally:
            _SPAN_STACK.reset(token)
            _record(
                phase,
                started,
                timeout_s=budget if isinstance(budget, (int, float)) else None,
                ordinal=ordinal,
                span_id=span_id,
                parent_span=stack[-1] if stack else None,
            )

    monkeypatch.setattr(owner, name, timed)


def _wrap_function(monkeypatch: pytest.MonkeyPatch, owner: Any, name: str, phase: str, *, budget_arg: str | None = None) -> None:
    original = getattr(owner, name)

    def timed(*args: Any, **kwargs: Any) -> Any:
        global _SPAN_SEQUENCE
        started = time.perf_counter()
        budget = _argument_value(original, budget_arg, args, kwargs)
        stack = _SPAN_STACK.get()
        _SPAN_SEQUENCE += 1
        span_id = _SPAN_SEQUENCE
        token = _SPAN_STACK.set((*stack, (span_id, phase)))
        try:
            return original(*args, **kwargs)
        finally:
            _SPAN_STACK.reset(token)
            _record(
                phase,
                started,
                timeout_s=budget if isinstance(budget, (int, float)) else None,
                span_id=span_id,
                parent_span=stack[-1] if stack else None,
            )

    monkeypatch.setattr(owner, name, timed)


def _install_timers(monkeypatch: pytest.MonkeyPatch) -> None:
    from agentic_hil import config, gdbmi
    from agentic_hil.backends import common, gdbdebug
    from agentic_hil.tools import AgenticHILToolService

    _wrap_method(monkeypatch, AgenticHILToolService, "call", "service_call")
    _wrap_method(monkeypatch, AgenticHILToolService, "close", "service_close")
    _wrap_method(monkeypatch, gdbmi.GdbMiClient, "__init__", "gdb_client_init")
    _wrap_method(monkeypatch, gdbmi.GdbMiClient, "command", "mi_command_wait", budget_arg="timeout_s")
    _wrap_method(monkeypatch, gdbmi.GdbMiClient, "wait_for_stop", "mi_stop_wait", budget_arg="timeout_s")
    _wrap_method(monkeypatch, gdbmi.GdbMiClient, "close", "gdb_client_close")
    _wrap_method(monkeypatch, gdbdebug.GdbDebugSessions, "_cleanup_session", "debug_session_cleanup", budget_arg="timeout_s")
    _wrap_function(monkeypatch, gdbdebug, "wait_for_ready_line", "debug_server_ready_wait", budget_arg="timeout_s")
    _wrap_function(monkeypatch, gdbdebug, "spawn_managed_process", "debug_server_spawn")
    _wrap_function(monkeypatch, gdbmi, "spawn_managed_process", "gdb_spawn")
    _wrap_function(monkeypatch, gdbdebug, "terminate_process_tree", "debug_server_tree_reap", budget_arg="timeout_s")
    _wrap_function(monkeypatch, gdbmi, "terminate_process_tree", "gdb_tree_reap", budget_arg="timeout_s")
    _wrap_function(monkeypatch, common, "spawn_managed_process", "command_process_spawn")
    _wrap_function(monkeypatch, common, "terminate_process_tree", "command_tree_reap", budget_arg="timeout_s")
    _wrap_function(monkeypatch, common, "spawn_command", "external_command_total", budget_arg="timeout_seconds")
    _wrap_function(monkeypatch, config, "atomic_write_bytes", "atomic_write_total")
    _wrap_function(monkeypatch, config, "_windows_hold_directory_chain", "windows_directory_chain_lock")
    _wrap_function(monkeypatch, config, "safe_file_path", "safe_file_path_check")

    # These broad filesystem operations are timed only during the selected test
    # body. Their arguments are never recorded, so neither paths nor file data
    # can enter the artifact.
    _wrap_function(monkeypatch, shutil, "rmtree", "shutil_rmtree")
    _wrap_function(monkeypatch, os, "fsync", "os_fsync")


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item: pytest.Item, nextitem: pytest.Item | None):
    global _ITEM, _EVENTS, _SPAN_SEQUENCE
    _ITEM = item.nodeid
    _EVENTS = []
    _SPAN_SEQUENCE = 0
    monkeypatch = pytest.MonkeyPatch()
    _install_timers(monkeypatch)
    started = time.perf_counter()
    stack = _SPAN_STACK.get()
    _SPAN_SEQUENCE += 1
    total_span_id = _SPAN_SEQUENCE
    token = _SPAN_STACK.set((*stack, (total_span_id, "test_total")))
    try:
        yield
    finally:
        _SPAN_STACK.reset(token)
        _record("test_total", started, span_id=total_span_id, parent_span=stack[-1] if stack else None)
        root = Path(os.environ.get("AGENTIC_HIL_PHASE_PROFILE_ROOT", ".testenv/phase-profile"))
        sample = os.environ.get("AGENTIC_HIL_PHASE_PROFILE_SAMPLE", "sample")
        worker = os.environ.get("PYTEST_XDIST_WORKER", "local")
        output = root / sample / f"{worker}.jsonl"
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps({"nodeid": _ITEM, "events": _EVENTS}, separators=(",", ":")) + "\n")
        monkeypatch.undo()
        _COUNTERS.clear()
        _ITEM = None


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_setup(item: pytest.Item):
    started = time.perf_counter()
    try:
        yield
    finally:
        _record("pytest_setup", started)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item: pytest.Item):
    started = time.perf_counter()
    try:
        yield
    finally:
        _record("pytest_call", started)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_teardown(item: pytest.Item, nextitem: pytest.Item | None):
    started = time.perf_counter()
    try:
        yield
    finally:
        _record("pytest_teardown", started)


def pytest_report_header(config: pytest.Config) -> str:
    workers = getattr(config.option, "numprocesses", None)
    sample = os.environ.get("AGENTIC_HIL_PHASE_PROFILE_SAMPLE", "sample")
    return (
        f"Phase profile {sample}: OS={platform.system()} {platform.release()}, Python={platform.python_version()}, "
        f"CPU={platform.processor() or 'unknown'} x{os.cpu_count() or 'unknown'}, xdist_workers={workers}; "
        "durations are wall-clock seconds; MI waits include write/flush and use the recorded timeout budget; "
        "tree-reap durations are inclusive cleanup-call time; nested spans carry parent ids and must not be added "
        "to their parents."
    )
