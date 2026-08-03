from __future__ import annotations

import json
import re
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator, SchemaError

from agentic_hil.config import (
    ConfigError,
    UniqueKeyLoader,
    absolute_without_symlinks,
    bind_debugger,
    is_path_within_frozen,
    reject_nonfinite_numbers,
    safe_read_text,
)
from agentic_hil.contracts import validate_tool_arguments
from agentic_hil.devices import Device, DeviceSet, debugger_device, uart_device
from agentic_hil.report import audit_errors, overall_success
from agentic_hil.tools import AgenticHILToolService
from agentic_hil.types import AgenticHILConfig, DebuggerConfig, JsonObject

DEFAULT_TEST_CONFIG_PATH = ".agentic-hil/testconfig.yaml"
TEST_CONFIG_SCHEMA_RESOURCE = "schemas/testconfig.schema.json"
ACTION_SCHEMAS = {
    "flash": "flash",
    "uart_open": "uartOpen",
    "uart_close": "uartClose",
    "debug_start": "debugStart",
    "run_until_breakpoint": "runUntilBreakpoint",
    "dump_memory": "dumpMemory",
    "debug_stop": "debugStop",
}
DEBUG_ACTIONS = {"debug_start", "run_until_breakpoint", "dump_memory", "debug_stop"}
# Routing is a two-way split, so it has exactly one authority: an action is a
# UART action or it runs on a debugger. A second "which actions need a probe"
# constant would go stale silently the first time an action is added.
UART_ACTIONS = {"uart_open", "uart_close"}


def result_failed(result: JsonObject) -> bool:
    return not overall_success(result)


def result_error_type(result: JsonObject) -> str:
    if result.get("error_type"):
        return str(result["error_type"])
    if result.get("target_ok") is False:
        return str(result.get("target_error_type") or "target_failed")
    if result.get("audit_ok") is False:
        return "audit_failed"
    return "step_failed"


def propagate_result_status(aggregate: JsonObject, sources: list[JsonObject]) -> None:
    audit_failures = [source for source in sources if source.get("audit_ok") is False]
    if audit_failures:
        aggregate["audit_ok"] = False
        flattened: list[JsonObject] = []
        for source in audit_failures:
            for error in audit_errors(source):
                if error not in flattened:
                    flattened.append(error)
        if flattened:
            aggregate["audit_error"] = flattened[0]
        aggregate["audit_errors"] = flattened
        aggregate["retry_safe"] = audit_failures[0].get("retry_safe", False)
    target_failures = [source for source in sources if source.get("target_ok") is False]
    if target_failures:
        aggregate["target_ok"] = False
        if "target_error_type" in target_failures[0]:
            aggregate["target_error_type"] = target_failures[0]["target_error_type"]


def merge_result_status(result: JsonObject, *sources: JsonObject) -> JsonObject:
    merged = dict(result)
    propagate_result_status(merged, list(sources))
    retry_values = [source["retry_safe"] for source in sources if "retry_safe" in source]
    if any(source.get("audit_ok") is False for source in sources):
        merged["retry_safe"] = False
    elif retry_values:
        merged["retry_safe"] = all(value is not False for value in retry_values)
    if any(source.get("audit_ok") is True for source in sources) and "audit_ok" not in merged:
        merged["audit_ok"] = True
    if any(source.get("target_ok") is True for source in sources) and "target_ok" not in merged:
        merged["target_ok"] = True
    return merged


@dataclass(frozen=True)
class TestStep:
    action: str
    arguments: JsonObject
    # Routing keys, straight from the authoritative config's own names: a
    # debugger action names a `debuggers` entry, a UART action names a
    # `com_ports` entry. `debugger` may be None only while the project
    # configures exactly one probe.
    debugger: str | None = None
    port_id: str | None = None

    @property
    def route(self) -> str:
        return self.debugger or self.port_id or "-"


@dataclass(frozen=True)
class TestConfig:
    __test__ = False

    path: str
    name: str
    steps: list[TestStep]


def load_test_config(test_config_path: str | None = None, work_dir: str | None = None) -> TestConfig:
    base = Path(work_dir or Path.cwd()).resolve()
    requested = Path(test_config_path or DEFAULT_TEST_CONFIG_PATH)
    lexical_path = absolute_without_symlinks(requested if requested.is_absolute() else base / requested)
    path = lexical_path
    if not is_path_within_frozen(path, base):
        raise ConfigError(
            "test_config_invalid",
            "Test plan must remain inside the configured workspace.",
            {"path": str(path), "workspace_root": str(base)},
        )
    try:
        loaded = yaml.load(safe_read_text(path, workspace=base), Loader=UniqueKeyLoader)
    except FileNotFoundError as error:
        raise ConfigError("test_config_not_found", "Test reactor configuration file could not be found.", {"path": str(path)}) from error
    except OSError as error:
        raise ConfigError(
            "test_config_unreadable",
            "Test reactor configuration file could not be read.",
            {"path": str(path), "backend_error": str(error)},
        ) from error
    except UnicodeDecodeError as error:
        raise ConfigError(
            "test_config_invalid",
            "Test reactor configuration file is not valid UTF-8 text.",
            {"path": str(path)},
        ) from error
    except yaml.YAMLError as error:
        details: JsonObject = {"path": str(path), "backend_error": str(error)}
        mark = getattr(error, "problem_mark", None)
        if mark is not None:
            details.update({"line": mark.line + 1, "column": mark.column + 1})
        raise ConfigError(
            "test_config_invalid",
            "Test reactor configuration file is not valid YAML or JSON.",
            details,
        ) from error

    raw: Any = loaded or {}
    if not isinstance(raw, dict):
        raise ConfigError("test_config_invalid", "Test reactor configuration root must be a mapping.", {"path": str(path)})
    reject_nonfinite_numbers(raw, "test_config_invalid", str(path))
    reject_superseded_plan_version(raw, str(path))
    validate_test_config_schema(raw, str(path))
    steps = [
        TestStep(
            action=str(step["action"]),
            arguments={key: value for key, value in step.items() if key not in {"action", "debugger", "port_id"}},
            debugger=None if step.get("debugger") is None else str(step["debugger"]),
            port_id=None if step.get("port_id") is None else str(step["port_id"]),
        )
        for step in raw["steps"]
    ]
    return TestConfig(path=str(path), name=str(raw.get("name") or path.stem), steps=steps)


def reject_superseded_plan_version(raw: JsonObject, path: str | None = None) -> None:
    """Refuse a version 1 plan by name.

    Version 1 steps addressed a `device:` that the config model no longer has.
    Leaving the plan at version 1 would have made every old plan invalid with
    only a bare const mismatch to go on, so the break gets a version boundary
    and a message that says which key replaced which."""
    if raw.get("version") != 1:
        return
    raise ConfigError(
        "test_config_invalid",
        "This test plan is version 1, whose steps address a `device:` that no longer exists. Set `version: 2` and route each step by the name it drives.",
        {
            "path": path,
            "field": "version",
            "migration": {
                "device (flash, debug_start, run_until_breakpoint, dump_memory, debug_stop)": "debugger: <name of a debuggers entry>, or omit it while the project configures exactly one probe",
                "device (uart_open, uart_close)": "port_id: <name of a com_ports entry>",
            },
        },
    )


def validate_test_config_schema(raw: JsonObject, path: str | None = None) -> None:
    schema = json.loads(resources.files("agentic_hil").joinpath(TEST_CONFIG_SCHEMA_RESOURCE).read_text(encoding="utf-8"))
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as error:
        raise ConfigError(
            "test_config_schema_invalid",
            "Bundled test reactor configuration schema is invalid.",
            {"schema": TEST_CONFIG_SCHEMA_RESOURCE, "schema_error": str(error)},
        ) from error
    root_schema = deepcopy(schema)
    root_schema["properties"]["steps"]["items"] = {}
    errors = sorted(Draft202012Validator(root_schema).iter_errors(raw), key=lambda item: list(item.absolute_path))
    if errors:
        raise_test_config_validation_error(errors[0], path)

    for index, step in enumerate(raw["steps"]):
        if not isinstance(step, dict):
            raise ConfigError(
                "test_config_invalid",
                "Test reactor step must be a mapping.",
                {"path": path, "field": f"steps[{index}]", "value": step},
            )
        action = step.get("action")
        if action not in ACTION_SCHEMAS:
            raise ConfigError(
                "test_config_invalid",
                "Test reactor action has an unsupported value.",
                {
                    "path": path,
                    "field": f"steps[{index}].action",
                    "value": action,
                    "allowed_values": list(ACTION_SCHEMAS),
                },
            )
        step_schema = {**schema["$defs"][ACTION_SCHEMAS[action]], "$defs": schema["$defs"]}
        step_errors = sorted(Draft202012Validator(step_schema).iter_errors(step), key=lambda item: list(item.absolute_path))
        if step_errors:
            raise_test_config_validation_error(step_errors[0], path, ["steps", str(index)])


def raise_test_config_validation_error(error: Any, path: str | None, prefix: list[str] | None = None) -> None:
    parts = [*(prefix or []), *(str(part) for part in error.absolute_path)]
    if error.validator == "required":
        missing = next((name for name in error.validator_value if name not in error.instance), None)
        if missing is not None:
            parts.append(str(missing))
    elif error.validator == "additionalProperties":
        match = re.search(r"'([^']+)' was unexpected", error.message)
        if match:
            parts.append(match.group(1))
    field = format_test_config_field(parts)
    details: JsonObject = {
        "path": path,
        "field": field,
        "schema_error": "Value does not satisfy the test configuration schema.",
        "validator": error.validator,
    }
    if isinstance(error.instance, (str, int, float, bool)) or error.instance is None:
        details["value"] = error.instance[:128] if isinstance(error.instance, str) else error.instance
    if error.validator in {"enum", "const"}:
        details["allowed_values"] = error.validator_value if error.validator == "enum" else [error.validator_value]
    raise ConfigError("test_config_invalid", "Test reactor configuration failed schema validation.", details) from error


def format_test_config_field(parts: list[str]) -> str:
    field = ""
    for part in parts:
        if part.isdigit():
            field = f"{field}[{part}]" if field else f"[{part}]"
        else:
            field = f"{field}.{part}" if field else part
    return field or "$"


class DebuggerRunner:
    """Executes one test plan's debugger steps against a single named probe and
    owns the debug session it opened, so cleanup can close exactly what this
    plan started on that board."""

    def __init__(self, debugger_id: str, debugger: DebuggerConfig, service: AgenticHILToolService):
        self.id = debugger_id
        self.debugger = debugger
        self.service = service
        self._owns_debug_session = False
        self.cleanup_interrupts: list[BaseException] = []

    def execute(self, action: str, arguments: JsonObject) -> JsonObject:
        if action == "flash":
            return self.service.call("flash_firmware", arguments)
        if action == "debug_start":
            self._owns_debug_session = True
            result = self.service.call("debug_start_session", arguments)
            if result.get("ok") is True:
                if result.get("target_ok") is False:
                    result = {
                        **result,
                        "ok": False,
                        "error_type": result.get("target_error_type", "target_stop"),
                        "summary": "Debug session started, but the target is stopped abnormally.",
                    }
            else:
                self._owns_debug_session = result.get("cleanup_required") is True
            return result
        if action == "run_until_breakpoint":
            return self._run_until_breakpoint(arguments)
        if action == "dump_memory":
            return self.service.call("debug_dump_symbol_ihex", arguments)
        if action == "debug_stop":
            result = self.service.call("debug_stop_session", arguments)
            if result.get("ok") is True:
                self._owns_debug_session = False
            return result
        return {
            "ok": False,
            "tool": "test_reactor",
            "error_type": "unknown_action",
            "summary": "Unknown test reactor action.",
            "debugger": self.id,
            "action": action,
        }

    def cleanup(self) -> list[JsonObject]:
        if not self._owns_debug_session:
            return []
        result = self._cleanup_call("debug_stop_session")
        if result.get("ok") is True:
            self._owns_debug_session = False
        return [{"debugger": self.id, "action": "debug_stop", "result": result}]

    def _run_until_breakpoint(self, arguments: JsonObject) -> JsonObject:
        breakpoint_result = self.service.call("debug_set_breakpoint", {"location": arguments["location"]})
        if result_failed(breakpoint_result):
            if breakpoint_result.get("side_effect_committed") is True or breakpoint_result.get("breakpoint"):
                cleared = self.service.call("debug_clear_breakpoints")
                return merge_result_status({**breakpoint_result, "breakpoint_cleanup": cleared}, breakpoint_result, cleared)
            return breakpoint_result
        continued = self.service.call("debug_continue", {} if arguments.get("timeout_s") is None else {"timeout_s": arguments["timeout_s"]})
        cleared = self.service.call("debug_clear_breakpoints")
        if result_failed(cleared):
            if result_failed(continued):
                return merge_result_status({**continued, "breakpoint_cleanup": cleared}, breakpoint_result, continued, cleared)
            return merge_result_status({
                "ok": False,
                "tool": "test_reactor",
                "error_type": result_error_type(cleared) if cleared.get("audit_ok") is False or cleared.get("target_ok") is False else "breakpoint_cleanup_failed",
                "summary": "Target stopped, but the reactor breakpoint could not be removed.",
                "debugger": self.id,
                "breakpoint": breakpoint_result.get("breakpoint"),
                "breakpoint_cleanup": cleared,
            }, breakpoint_result, continued, cleared)
        if result_failed(continued):
            return merge_result_status(continued, breakpoint_result, continued, cleared)
        expected_id = breakpoint_result.get("breakpoint", {}).get("id")
        actual_id = continued.get("stop", {}).get("breakpoint_id")
        if continued.get("stop_reason") != "breakpoint_hit" or actual_id != expected_id:
            return merge_result_status({
                "ok": False,
                "tool": "test_reactor",
                "error_type": "unexpected_stop",
                "summary": "Target did not stop at the breakpoint created by this test step.",
                "debugger": self.id,
                "expected_breakpoint_id": expected_id,
                "stop": continued.get("stop"),
            }, breakpoint_result, continued, cleared)
        return merge_result_status({
            "ok": True,
            "tool": "test_reactor",
            "summary": "Target stopped at the expected breakpoint.",
            "breakpoint": breakpoint_result["breakpoint"],
            "stop_reason": continued["stop_reason"],
            "stop": continued["stop"],
        }, breakpoint_result, continued, cleared)

    def _cleanup_call(self, tool: str, arguments: JsonObject | None = None) -> JsonObject:
        try:
            return self.service.call(tool, arguments)
        except BaseException as error:
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                self.cleanup_interrupts.append(error)
            return exception_result(tool, "cleanup_exception", "Cleanup action raised an exception.", error)


class TestReactor:
    __test__ = False

    def __init__(self, config: AgenticHILConfig, service: AgenticHILToolService, service_factory: Callable[[AgenticHILConfig], AgenticHILToolService] | None = None):
        self.config = config
        self.service = service
        self._service_factory = service_factory
        # Services built for a probe other than the base service's bound one are
        # owned here and closed by close(); the base service is the caller's.
        self._owned_services: list[AgenticHILToolService] = []
        self.runners: dict[str, DebuggerRunner] = {}
        # port_id -> True while this plan holds the session it opened.
        self._owned_uarts: dict[str, bool] = {}
        self.cleanup_interrupts: list[BaseException] = []

    def runner(self, debugger_id: str) -> DebuggerRunner:
        """The runner for one named probe, built on first use.

        A probe other than the base service's bound one needs its own service:
        without a factory the step would silently execute on whatever probe the
        base service holds, which is the wrong board."""
        existing = self.runners.get(debugger_id)
        if existing is not None:
            return existing
        debugger = self.config.debuggers[debugger_id]
        if debugger_id == self.config.debugger_id or self._service_factory is None:
            service = self.service
        else:
            bound = bind_debugger(self.config, debugger_id)
            # bind_debugger() already applies this probe's target override.
            service = self._service_factory(bound)
            self._owned_services.append(service)
        built = DebuggerRunner(debugger_id, debugger, service)
        self.runners[debugger_id] = built
        return built

    def _close_owned_services(self) -> list[BaseException]:
        errors: list[BaseException] = []
        for built in self._owned_services:
            try:
                built.close()
            except BaseException as error:  # noqa: BLE001 - aggregate per-probe close errors
                errors.append(error)
        self._owned_services = []
        return errors

    def close(self) -> None:
        errors = self._close_owned_services()
        if not errors:
            return
        interrupts = [error for error in errors if isinstance(error, (KeyboardInterrupt, SystemExit))]
        if interrupts:
            raise interrupts[0]
        detail = "; ".join(f"{type(error).__name__}: {error}" for error in errors)
        raise RuntimeError(f"Per-debugger service cleanup failed: {detail}") from errors[0]

    def execute_step(self, step: TestStep) -> JsonObject:
        if step.action in UART_ACTIONS:
            return self._execute_uart(step)
        return self.runner(str(self.step_debugger_id(step))).execute(step.action, step.arguments)

    def _execute_uart(self, step: TestStep) -> JsonObject:
        port_id = str(step.port_id)
        if step.action == "uart_open":
            result = self.service.call("com_session_start", {"port_id": port_id, "clear_buffer": step.arguments.get("clear_buffer", True)})
            self._owned_uarts[port_id] = result.get("ok") is True and not result.get("already_active", False)
            return result
        if not self._owned_uarts.get(port_id):
            return {
                "ok": False,
                "tool": "test_reactor",
                "error_type": "uart_session_not_owned",
                "summary": "A test plan cannot close a UART session it did not open.",
                "port_id": port_id,
            }
        result = self.service.call("com_session_stop", {"port_id": port_id})
        if result.get("ok") is True:
            self._owned_uarts[port_id] = False
        return result

    def _cleanup_uarts(self) -> list[JsonObject]:
        results: list[JsonObject] = []
        for port_id, owned in list(self._owned_uarts.items()):
            if not owned:
                continue
            try:
                result = self.service.call("com_session_stop", {"port_id": port_id})
            except BaseException as error:
                if isinstance(error, (KeyboardInterrupt, SystemExit)):
                    self.cleanup_interrupts.append(error)
                result = exception_result("com_session_stop", "cleanup_exception", "Cleanup action raised an exception.", error)
            results.append({"port_id": port_id, "action": "uart_close", "result": result})
            if result.get("ok") is True:
                self._owned_uarts[port_id] = False
        return results

    def run(self, test_config: TestConfig) -> JsonObject:
        try:
            validation_error = self.preflight(test_config)
        except Exception as error:
            validation_error = {
                "field": "$",
                "summary": "Test reactor preflight raised an exception.",
                **exception_result("test_reactor", "preflight_exception", "Test reactor preflight raised an exception.", error),
            }
        if validation_error is not None:
            result: JsonObject = {
                "ok": False,
                "tool": "test_reactor",
                "name": test_config.name,
                "test_config_path": test_config.path,
                "error_type": validation_error.get("error_type", "test_config_invalid"),
                "validation_error": validation_error,
                "steps": [],
                "cleanup": [],
                "cleanup_ok": True,
                "summary": "Test reactor configuration failed semantic validation; no steps were executed.",
            }
            if "step" in validation_error:
                result["failed_step"] = validation_error["step"]
            return result

        completed: list[JsonObject] = []
        failed_step: int | None = None
        primary_error: BaseException | None = None
        try:
            for index, step in enumerate(test_config.steps, start=1):
                try:
                    result: JsonObject = self.execute_step(step)
                except Exception as error:
                    result = exception_result(
                        "test_reactor",
                        "step_exception",
                        "Test reactor step raised an exception.",
                        error,
                    )
                completed.append({"index": index, "route": step.route, "action": step.action, "result": result})
                if result_failed(result):
                    failed_step = index
                    break
        except BaseException as error:
            primary_error = error
        finally:
            # Debug sessions close before UARTs: a still-attached debugger can
            # keep writing to the serial line this plan is about to close.
            cleanup = [*(result for runner in reversed(list(self.runners.values())) for result in runner.cleanup()), *self._cleanup_uarts()]

        cleanup_interrupts = [*self.cleanup_interrupts, *(error for runner in self.runners.values() for error in runner.cleanup_interrupts)]
        if primary_error is not None:
            primary_error.agentic_hil_cleanup = cleanup
            if cleanup_interrupts:
                primary_error.args = (*primary_error.args, "Cleanup interrupts: " + "; ".join(f"{type(error).__name__}: {error}" for error in cleanup_interrupts))
            raise primary_error
        if cleanup_interrupts:
            interrupt = cleanup_interrupts[0]
            interrupt.agentic_hil_cleanup = cleanup
            raise interrupt

        cleanup_errors = [item for item in cleanup if result_failed(item["result"])]
        cleanup_ok = not cleanup_errors
        ok = failed_step is None and cleanup_ok
        result: JsonObject = {
            "ok": ok,
            "tool": "test_reactor",
            "name": test_config.name,
            "test_config_path": test_config.path,
            "steps": completed,
            "cleanup": cleanup,
            "cleanup_ok": cleanup_ok,
            "cleanup_errors": cleanup_errors,
            "summary": "Test reactor sequence completed." if ok else "Test reactor sequence failed.",
        }
        propagate_result_status(result, [step["result"] for step in completed] + [item["result"] for item in cleanup])
        if failed_step is not None:
            result["failed_step"] = failed_step
            step_error_type = result_error_type(completed[-1]["result"])
            result["step_error_type"] = step_error_type
            result["error_type"] = "cleanup_failed" if not cleanup_ok else step_error_type
        elif not cleanup_ok:
            result["error_type"] = "cleanup_failed"
        return result

    def preflight(self, test_config: TestConfig) -> JsonObject | None:
        debug_active: str | None = None
        active_uarts: set[str] = set()

        for index, step in enumerate(test_config.steps, start=1):
            contract_error = self._preflight_tool_contract(index, step)
            if contract_error is not None:
                return contract_error

            if step.action in UART_ACTIONS:
                port_id = str(step.port_id)
                port = self.config.com_ports.get(port_id)
                if port is None:
                    return preflight_error(index, step, "port_id", "Test step references a COM port that is not in the authoritative config.", {"configured_com_ports": sorted(self.config.com_ports)})
                if step.action == "uart_open":
                    if not self.config.com_read_allowed(port):
                        return preflight_error(index, step, "action", "Reading this COM port is disabled by the authoritative config.")
                    if port_id in active_uarts:
                        return preflight_error(index, step, "action", "UART session is already open in this test plan.")
                    active_uarts.add(port_id)
                elif port_id not in active_uarts:
                    return preflight_error(index, step, "action", "UART session must be opened before it can be closed.")
                else:
                    active_uarts.discard(port_id)
                continue

            debugger_error = self._preflight_debugger_name(index, step)
            if debugger_error is not None:
                return debugger_error
            debugger_id = str(self.step_debugger_id(step))
            debugger = self.config.debuggers[debugger_id]
            permissions = debugger.permissions

            if step.action == "flash":
                if debug_active is not None:
                    return preflight_error(index, step, "action", "Firmware cannot be flashed while a debug session is active.", {"debug_session_debugger": debug_active})
                if not permissions.allow_flash:
                    return preflight_error(index, step, "action", "Flashing is disabled for this debugger by the authoritative config.")
                if step.arguments.get("reset_after_flash", False) and not permissions.allow_reset:
                    return preflight_error(index, step, "reset_after_flash", "Post-flash reset is disabled for this debugger by the authoritative config.")
                if permissions.allow_raw_debugger_commands:
                    return preflight_error(index, step, "action", "Flashing is disabled while this debugger allows raw debugger commands.", {"permission": "allow_raw_debugger_commands"})
                if permissions.allow_mass_erase:
                    return preflight_error(index, step, "action", "Flashing is disabled while this debugger allows mass erase.", {"permission": "allow_mass_erase"})
                artifact_error = self._preflight_artifact(index, step, require_elf=False)
                if artifact_error is not None:
                    return artifact_error
                continue

            if step.action in DEBUG_ACTIONS and debugger.type != "openocd":
                return preflight_error(
                    index,
                    step,
                    "action",
                    "Typed debug actions currently require a debugger of type 'openocd'.",
                    {"debugger_type": debugger.type},
                )
            if step.action == "debug_start":
                if debug_active is not None:
                    return preflight_error(index, step, "action", "A debug session is already active in this test plan.", {"debug_session_debugger": debug_active})
                mode = str(step.arguments.get("mode", "attach"))
                # Preflight is the only diagnosis the operator gets: it runs
                # before any tool call, so the backend's per-flag messages are
                # never reached. Name the flag that actually fired rather than
                # pointing at a permission the config plainly shows as false.
                if not self.config.probe_allowed(debugger):
                    return preflight_error(index, step, "action", "Debug sessions require allow_probe on this debugger.", {"permission": "allow_probe"})
                if permissions.allow_raw_debugger_commands:
                    return preflight_error(index, step, "action", "Debug sessions are disabled while this debugger allows raw debugger commands.", {"permission": "allow_raw_debugger_commands"})
                if mode != "attach" and not permissions.allow_reset:
                    return preflight_error(index, step, "mode", f"Debug mode '{mode}' requires allow_reset on this debugger.", {"permission": "allow_reset"})
                if mode == "load" and not permissions.allow_flash:
                    return preflight_error(index, step, "mode", "Debug load mode requires allow_flash on this debugger.", {"permission": "allow_flash"})
                if mode == "load" and permissions.allow_mass_erase:
                    return preflight_error(index, step, "mode", "Debug load mode is disabled while this debugger allows mass erase.", {"permission": "allow_mass_erase"})
                artifact_error = self._preflight_artifact(index, step, require_elf=True)
                if artifact_error is not None:
                    return artifact_error
                debug_active = debugger_id
            elif step.action == "debug_stop":
                if debug_active is None:
                    return preflight_error(index, step, "action", "A debug session must be started before it can be stopped.")
                if debug_active != debugger_id:
                    return preflight_error(index, step, "debugger", "A debug session may only be stopped on the debugger that started it.", {"debug_session_debugger": debug_active})
                debug_active = None
            elif step.action in {"run_until_breakpoint", "dump_memory"}:
                if debug_active is None:
                    return preflight_error(index, step, "action", "A debug session must be started before this action.")
                if debug_active != debugger_id:
                    return preflight_error(index, step, "debugger", "This action must run on the debugger that started the debug session.", {"debug_session_debugger": debug_active})
                symbol = breakpoint_symbol(step.arguments.get("location")) if step.action == "run_until_breakpoint" else step.arguments.get("symbol")
                if symbol is None and not self.config.debug.allow_all_symbols:
                    return preflight_error(index, step, "location", "File/line breakpoints require debug.allow_all_symbols.")
                if symbol is not None and not symbol_allowed(self.config, str(symbol)):
                    field = "location" if step.action == "run_until_breakpoint" else "symbol"
                    return preflight_error(index, step, field, "Symbol is not allowed by the authoritative debug config.", {"symbol": symbol})
                if step.action == "dump_memory" and hasattr(self.service, "artifacts"):
                    output = self.service.artifacts.validate_output_path(str(step.arguments["output_path"]), "debug_dump_symbol_ihex")
                    if output.get("ok") is not True:
                        return preflight_error(
                            index,
                            step,
                            "output_path",
                            str(output.get("summary", "Memory dump output path is invalid.")),
                            {"validation": output},
                        )
        return None

    def _preflight_debugger_name(self, index: int, step: TestStep) -> JsonObject | None:
        """Resolve which probe a debugger step runs on, or refuse the plan.

        A plan that omits the name while several probes are configured is not
        defaulted to one: picking a board for the author is how the wrong board
        gets flashed. Steps carry the resolved name from here on."""
        debugger_id = self.step_debugger_id(step)
        if debugger_id is None:
            if not self.config.debuggers:
                return preflight_error(index, step, "debugger", "The authoritative config declares no debuggers, so this step cannot run.")
            return preflight_error(index, step, "debugger", "Name the debugger this step runs on; the authoritative config declares several.", {"configured_debuggers": sorted(self.config.debuggers)})
        if debugger_id not in self.config.debuggers:
            return preflight_error(index, step, "debugger", "Test step references a debugger that is not in the authoritative config.", {"configured_debuggers": sorted(self.config.debuggers)})
        if debugger_id != self.config.debugger_id and self._service_factory is None:
            return preflight_error(index, step, "debugger", "Running a step on another debugger needs a service factory to build that probe's service; the reactor would otherwise execute it on the bound probe.", {"bound_debugger": self.config.debugger_id})
        return None

    def step_debugger_id(self, step: TestStep) -> str | None:
        return step_debugger_id(self.config, step)

    def _preflight_tool_contract(self, index: int, step: TestStep) -> JsonObject | None:
        # Validate each step's plan-supplied tool arguments against the exact MCP
        # contract validators, so a step the reactor schema accepted but the tool
        # contract rejects (e.g. a traversal path) fails before any backend call
        # builds hardware or process state. (uart_open/uart_close are checked
        # against the config's own com_ports names instead, above.)
        for tool, arguments in step_tool_arguments(step):
            error = validate_tool_arguments(tool, arguments)
            if error is not None:
                return preflight_error(index, step, error.get("field", "arguments"), f"Step arguments are rejected by the {tool} contract: {error.get('summary', 'invalid argument')}", {"tool": tool, "validation": error})
        return None

    def _preflight_artifact(self, index: int, step: TestStep, require_elf: bool) -> JsonObject | None:
        if not hasattr(self.service, "artifacts"):
            return None
        image_path = str(step.arguments["image_path"])
        validation = self.service.artifacts.validate_local_path(image_path)
        if validation.get("ok") is not True:
            return preflight_error(
                index,
                step,
                "image_path",
                str(validation.get("summary", "Firmware artifact is invalid.")),
                {"validation": validation},
            )
        if require_elf and Path(image_path).suffix.lower() != ".elf":
            return preflight_error(index, step, "image_path", "Debug sessions require an ELF artifact with debug symbols.")
        return None


def step_debugger_id(config: AgenticHILConfig, step: TestStep) -> str | None:
    """The probe a debugger step runs on: the name the plan gave, or the only
    configured one. None means the plan must name it — with several probes
    configured, picking one for the author is how the wrong board gets
    flashed."""
    if step.debugger is not None:
        return step.debugger
    return next(iter(config.debuggers)) if len(config.debuggers) == 1 else None


def plan_devices(config: AgenticHILConfig, test_config: TestConfig) -> DeviceSet:
    """Every device this plan says it touches, in acquisition order.

    This is what the run locks before its first step, and what every call inside
    it is checked against, so the plan stays the honest record of what a test
    reaches for. A name the config does not know is left out on purpose:
    preflight refuses that step with a message about the name, which is a better
    answer than a lock failure about a device nobody configured.

    Two steps naming one physical unit — a probe and its virtual COM port under
    one resource_id, the same port under two config entries — yield one device,
    because DeviceSet keys on the hardware and not on what the plan called it."""
    devices: list[Device] = []
    for step in test_config.steps:
        if step.action in UART_ACTIONS:
            if step.port_id in config.com_ports:
                devices.append(uart_device(config, str(step.port_id)))
            continue
        debugger_id = step_debugger_id(config, step)
        if debugger_id in config.debuggers:
            devices.append(debugger_device(bind_debugger(config, str(debugger_id)), str(debugger_id)))
    return DeviceSet.of(devices)


def declared_devices(config: AgenticHILConfig, test_config: TestConfig) -> list[str]:
    """The lock keys of `plan_devices`, which is what the mutex takes."""
    return plan_devices(config, test_config).lock_keys


def step_tool_arguments(step: TestStep) -> list[tuple[str, JsonObject]]:
    """The plan-supplied tool calls a step will make, as (tool, arguments), so
    preflight can validate them against the same contracts the dispatcher
    enforces. uart_open/uart_close are omitted: preflight checks their port_id
    against the authoritative config's own com_ports names instead."""
    action = step.action
    arguments = step.arguments
    if action == "flash":
        return [("flash_firmware", arguments)]
    if action == "debug_start":
        return [("debug_start_session", arguments)]
    if action == "run_until_breakpoint":
        calls: list[tuple[str, JsonObject]] = [("debug_set_breakpoint", {"location": arguments.get("location")})]
        if arguments.get("timeout_s") is not None:
            calls.append(("debug_continue", {"timeout_s": arguments.get("timeout_s")}))
        return calls
    if action == "dump_memory":
        return [("debug_dump_symbol_ihex", arguments)]
    if action == "debug_stop":
        return [("debug_stop_session", arguments)]
    return []


def breakpoint_symbol(location: object) -> str | None:
    if isinstance(location, str):
        return location
    if isinstance(location, dict):
        symbol = location.get("symbol", location.get("function"))
        return symbol if isinstance(symbol, str) else None
    return None


def symbol_allowed(config: AgenticHILConfig, symbol: str) -> bool:
    return config.debug.allow_all_symbols or symbol in config.debug.allowed_symbols


def preflight_error(index: int, step: TestStep, field: str, summary: str, details: JsonObject | None = None) -> JsonObject:
    return {
        "step": index,
        "field": f"steps[{index - 1}].{field}",
        "route": step.route,
        "action": step.action,
        "summary": summary,
        **(details or {}),
    }


def exception_result(tool: str, error_type: str, summary: str, error: BaseException) -> JsonObject:
    return {
        "ok": False,
        "tool": tool,
        "error_type": error_type,
        "summary": summary,
        "exception_type": type(error).__name__,
        "backend_error": str(error),
    }
