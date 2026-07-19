from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from importlib import resources
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator, SchemaError

from agentic_hil.config import ConfigError, UniqueKeyLoader, load_config
from agentic_hil.hardware_lock import HardwareLockError, HardwareQuarantinedError, ProjectHardwareLock
from agentic_hil.report import write_report
from agentic_hil.tools import AgenticHILToolService, cleanup_error_is_audit_only
from agentic_hil.types import AgenticHILConfig, DeviceConfig, JsonObject

TEST_CONFIG_SCHEMA_RESOURCE = "schemas/testconfig.schema.json"
DEBUG_ACTIONS = {"debug_start", "run_until_breakpoint", "dump_memory", "debug_stop"}
ProjectTestLock = ProjectHardwareLock


@dataclass(frozen=True)
class TestStep:
    action: str
    arguments: JsonObject


@dataclass(frozen=True)
class TestCase:
    name: str
    device: str
    steps: list[TestStep]


@dataclass(frozen=True)
class TestPlan:
    path: str
    tests: list[TestCase]


def test_config_schema_text() -> str:
    return resources.files("agentic_hil").joinpath(TEST_CONFIG_SCHEMA_RESOURCE).read_text(encoding="utf-8")


def load_test_config(test_config_path: str, work_dir: str | None = None) -> TestPlan:
    base = Path(work_dir or Path.cwd()).resolve()
    requested = Path(test_config_path).expanduser()
    path = (requested if requested.is_absolute() else base / requested).resolve()
    if not path.is_file():
        raise ConfigError("test_config_not_found", "Test reactor configuration file could not be found.", {"path": str(path)})
    try:
        loaded = yaml.load(path.read_text(encoding="utf-8"), Loader=UniqueKeyLoader)
    except OSError as error:
        raise ConfigError("test_config_unreadable", "Test reactor configuration file could not be read.", {"path": str(path), "backend_error": str(error)}) from error
    except UnicodeDecodeError as error:
        raise ConfigError("test_config_invalid", "Test reactor configuration file is not valid UTF-8 text.", {"path": str(path)}) from error
    except yaml.YAMLError as error:
        details: JsonObject = {"path": str(path), "backend_error": str(error)}
        mark = getattr(error, "problem_mark", None)
        if mark is not None:
            details.update({"line": mark.line + 1, "column": mark.column + 1})
        raise ConfigError("test_config_invalid", "Test reactor configuration file is not valid YAML or JSON.", details) from error

    raw: Any = loaded or {}
    if not isinstance(raw, dict):
        raise ConfigError("test_config_invalid", "Test reactor configuration root must be a mapping.", {"path": str(path)})
    validate_test_config_schema(raw, str(path))
    tests = [
        TestCase(
            name=str(test["name"]),
            device=str(test["device"]),
            steps=[
                TestStep(
                    action=str(step["action"]),
                    arguments={key: value for key, value in step.items() if key != "action"},
                )
                for step in test["steps"]
            ],
        )
        for test in raw["tests"]
    ]
    names = [test.name for test in tests]
    duplicate_names = sorted({name for name in names if names.count(name) > 1})
    if duplicate_names:
        raise ConfigError(
            "test_config_invalid",
            "Test names must be unique within a test configuration.",
            {"path": str(path), "duplicate_test_names": duplicate_names},
        )
    return TestPlan(path=str(path), tests=tests)


def validate_test_config_schema(raw: JsonObject, path: str | None = None) -> None:
    schema = json.loads(test_config_schema_text())
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as error:
        raise ConfigError("test_config_schema_invalid", "Bundled test reactor schema is invalid.", {"schema_error": str(error)}) from error
    errors = sorted(Draft202012Validator(schema).iter_errors(raw), key=lambda item: list(item.absolute_path))
    if errors:
        error = deepest_validation_error(errors[0])
        parts = [str(part) for part in error.absolute_path]
        if error.validator == "additionalProperties":
            match = re.search(r"'([^']+)' was unexpected", error.message)
            if match:
                parts.append(match.group(1))
        raise ConfigError(
            "test_config_invalid",
            "Test reactor configuration failed schema validation.",
            {"path": path, "field": format_field(parts), "schema_error": error.message, "value": error.instance},
        ) from error


def deepest_validation_error(error: Any) -> Any:
    candidates = [error, *(deepest_validation_error(child) for child in error.context)]
    return max(candidates, key=lambda item: (len(item.absolute_path), item.validator != "oneOf"))


def format_field(parts: list[str]) -> str:
    field = ""
    for part in parts:
        field = f"{field}[{part}]" if part.isdigit() else (f"{field}.{part}" if field else part)
    return field or "$"


class Device:
    def __init__(
        self,
        device_id: str,
        binding: DeviceConfig,
        root_config: AgenticHILConfig,
        service_factory: Callable[[AgenticHILConfig], AgenticHILToolService] | None = None,
        policy_guard: Callable[[], JsonObject | None] | None = None,
        hardware_owner: str | None = None,
    ):
        self.id = device_id
        self.binding = binding
        self.debugger = root_config.debugger if binding.debugger == "default" else root_config.debuggers[binding.debugger]
        self.config = replace(root_config, target=binding.target, debugger=self.debugger)
        self.service = service_factory(self.config) if service_factory else AgenticHILToolService(self.config, reload_config=False, hardware_owner=hardware_owner)
        self.policy_guard = policy_guard
        self._owns_debug = False
        self._owns_uart = False

    def execute(self, step: TestStep) -> JsonObject:
        action = step.action
        arguments = step.arguments
        if action == "flash":
            return self._call("flash_firmware", arguments)
        if action == "uart_open":
            if self.binding.uart is None:
                return capability_error(self.id, action, "uart")
            self._owns_uart = True
            result = self._call("com_session_start", {"port_id": self.binding.uart, "clear_buffer": arguments.get("clear_buffer", True)})
            self._owns_uart = result.get("ok") is True and not result.get("already_active", False)
            return result
        if action == "uart_close":
            if self.binding.uart is None:
                return capability_error(self.id, action, "uart")
            result = self._call("com_session_stop", {"port_id": self.binding.uart})
            if result.get("ok") is True:
                self._owns_uart = False
            return result
        if action == "debug_start":
            self._owns_debug = True
            result = self._call("debug_start_session", arguments)
            if result.get("ok") is not True:
                self._owns_debug = False
            return result
        if action == "run_until_breakpoint":
            return self._run_until_breakpoint(arguments)
        if action == "dump_memory":
            return self._call("debug_dump_symbol_ihex", arguments)
        if action == "debug_stop":
            result = self._call("debug_stop_session", arguments)
            if result.get("ok") is True:
                self._owns_debug = False
            return result
        return {"ok": False, "tool": "test_reactor", "error_type": "unknown_action", "summary": "Unknown test reactor action."}

    def cleanup(self) -> list[JsonObject]:
        results: list[JsonObject] = []
        if self._owns_debug:
            result = self._cleanup_call("debug_stop_session")
            results.append({"action": "debug_stop", "result": result})
            if result.get("ok") is True:
                self._owns_debug = False
        if self._owns_uart and self.binding.uart:
            result = self._cleanup_call("com_session_stop", {"port_id": self.binding.uart})
            results.append({"action": "uart_close", "result": result})
            if result.get("ok") is True:
                self._owns_uart = False
        return results

    def close(self) -> JsonObject:
        try:
            self.service.close()
        except Exception as error:
            result = exception_result("device_close_exception", "Device service cleanup raised an exception.", error)
            if cleanup_error_is_audit_only(error):
                result["error_type"] = "audit_write_failed"
                result["completion_confirmed"] = True
            return result
        return {"ok": True}

    def hardware_state(self) -> JsonObject:
        return self.service.hardware_state()

    def _run_until_breakpoint(self, arguments: JsonObject) -> JsonObject:
        breakpoint_result = self._call("debug_set_breakpoint", {"location": arguments["location"]})
        if breakpoint_result.get("ok") is not True:
            return breakpoint_result
        continued = self._call("debug_continue", {"timeout_s": arguments.get("timeout_s")})
        cleared = self._cleanup_call("debug_clear_breakpoints")
        if cleared.get("ok") is not True:
            unsafe = cleared.get("completion_unconfirmed") is True or cleared.get("hardware_state_unconfirmed") is True or cleared.get("error_type") in {"hardware_state_unconfirmed", "hardware_cleanup_failed"}
            return {"ok": False, "tool": "test_reactor", "error_type": "hardware_state_unconfirmed" if unsafe else "breakpoint_cleanup_failed", "step_error_type": cleared.get("error_type"), "completion_unconfirmed": unsafe, "hardware_state_unconfirmed": unsafe, "summary": "Reactor breakpoint could not be removed.", "breakpoint_cleanup": cleared}
        if continued.get("ok") is not True:
            return continued
        expected = breakpoint_result.get("breakpoint", {}).get("id")
        actual = continued.get("stop", {}).get("breakpoint_id")
        if continued.get("stop_reason") != "breakpoint_hit" or expected != actual:
            return {"ok": False, "tool": "test_reactor", "error_type": "unexpected_stop", "summary": "Target did not stop at the expected breakpoint.", "stop": continued.get("stop")}
        return {"ok": True, "tool": "test_reactor", "breakpoint": breakpoint_result["breakpoint"], "stop_reason": continued["stop_reason"], "stop": continued["stop"]}

    def _cleanup_call(self, tool: str, arguments: JsonObject | None = None) -> JsonObject:
        try:
            return self.service.call(tool, arguments)
        except Exception as error:
            return exception_result("cleanup_exception", "Cleanup action raised an exception.", error)

    def _call(self, tool: str, arguments: JsonObject | None = None) -> JsonObject:
        if self.policy_guard is not None:
            policy_error = self.policy_guard()
            if policy_error is not None:
                return policy_error
        return self.service.call(tool, arguments)


class TestReactor:
    def __init__(
        self,
        config: AgenticHILConfig,
        service_factory: Callable[[AgenticHILConfig], AgenticHILToolService] | None = None,
    ):
        self.config = config
        self.service_factory = service_factory
        self.devices: dict[str, Device] = {}
        self._loaded_policy_digest = read_policy_digest(config.config_path)
        self._policy_digest: str | None = None
        self._step_in_flight: JsonObject | None = None
        self._execution_inspection_errors: list[JsonObject] = []
        self._project_lock: ProjectHardwareLock | None = None
        self._has_run = False

    def run(self, plan: TestPlan) -> JsonObject:
        if self._has_run:
            raise RuntimeError("TestReactor instances are single-use.")
        self._has_run = True
        try:
            project_lock = ProjectTestLock(self.config.config_path)
            acquired = project_lock.acquire(source="test_reactor")
        except HardwareQuarantinedError as error:
            return {
                "ok": False,
                "tool": "test_reactor",
                "error_type": "hardware_state_unconfirmed",
                "test_config_path": plan.path,
                "tests": [],
                "cleanup": [],
                "quarantine": error.details,
                "summary": "Project hardware state is unconfirmed after an incomplete cleanup.",
            }
        except HardwareLockError as error:
            return {
                "ok": False,
                "tool": "test_reactor",
                "error_type": "test_reactor_lock_failed",
                "test_config_path": plan.path,
                "tests": [],
                "cleanup": [],
                "backend_error": str(error),
                "summary": "Project hardware lease could not be acquired.",
            }
        if not acquired:
            return {
                "ok": False,
                "tool": "test_reactor",
                "error_type": "test_reactor_busy",
                "test_config_path": plan.path,
                "tests": [],
                "cleanup": [],
                "summary": "Project hardware is in use by another Agentic HIL process.",
            }
        self._project_lock = project_lock
        response: JsonObject | None = None
        pending_base_exception: BaseException | None = None
        try:
            policy_error = self._establish_policy_baseline(plan.path)
            if policy_error is not None:
                response = policy_error
            else:
                initialization_error = self._initialize_devices(plan.path, project_lock.owner_token)
                response = initialization_error if initialization_error is not None else self._run_plan(plan)
        except Exception as error:
            try:
                cleanup = self.close()
            except BaseException as cleanup_error:
                cleanup = [{"device": "all", "result": exception_result("device_close_exception", "Device service cleanup raised an exception.", cleanup_error)}]
                if not isinstance(cleanup_error, Exception):
                    pending_base_exception = cleanup_error
            response = {
                "ok": False,
                "tool": "test_reactor",
                "error_type": "test_reactor_exception",
                "test_config_path": plan.path,
                "tests": [],
                "cleanup": cleanup,
                "exception": exception_result("test_reactor_exception", "Test reactor raised an exception.", error),
                "summary": "Test reactor failed unexpectedly.",
            }
        except BaseException as error:
            pending_base_exception = error
            try:
                cleanup = self.close()
            except BaseException as cleanup_error:
                cleanup = [{"device": "all", "result": exception_result("device_close_exception", "Device service cleanup raised an exception.", cleanup_error)}]
            response = {
                "ok": False,
                "tool": "test_reactor",
                "error_type": "test_reactor_interrupted",
                "test_config_path": plan.path,
                "tests": [],
                "cleanup": cleanup,
                "exception_type": type(error).__name__,
                "summary": "Test reactor was interrupted.",
            }
        finally:
            hardware_state = self._collect_hardware_state()
            if hardware_state["active"]:
                if response is None:
                    response = {
                        "ok": False,
                        "tool": "test_reactor",
                        "error_type": "test_reactor_interrupted",
                        "test_config_path": plan.path,
                        "tests": [],
                        "cleanup": [],
                        "summary": "Test reactor did not complete before cleanup state was evaluated.",
                    }
                underlying_error_type = response.get("error_type")
                try:
                    quarantine = project_lock.mark_quarantined(
                        reason="hardware_cleanup_failed",
                        source="test_reactor",
                        active_resources=hardware_state["active_resources"],
                        inspection_errors=hardware_state["inspection_errors"],
                    )
                except HardwareLockError as error:
                    quarantine = None
                    response["quarantine_error"] = str(error)
                response.update(
                    {
                        "ok": False,
                        "error_type": "unsafe_test_state",
                        "hardware_state_unconfirmed": True,
                        "active_resources": hardware_state["active_resources"],
                        "inspection_errors": hardware_state["inspection_errors"],
                        "summary": "Test reactor stopped because cleanup could not establish a safe hardware state.",
                    }
                )
                if underlying_error_type and underlying_error_type != "unsafe_test_state":
                    response["underlying_error_type"] = underlying_error_type
                if quarantine is not None:
                    response["quarantine"] = quarantine
                try:
                    response = self._finalize(response)
                finally:
                    project_lock.release_os_lock()
            else:
                try:
                    response = self._finalize(response)
                    project_lock.confirm_safe()
                except HardwareLockError as error:
                    underlying_error_type = response.get("error_type")
                    response.update(
                        {
                            "ok": False,
                            "error_type": "unsafe_test_state",
                            "underlying_error_type": underlying_error_type,
                            "hardware_state_unconfirmed": True,
                            "state_error": str(error),
                            "summary": "Test reactor cleanup completed, but safe lease release could not be persisted.",
                        }
                    )
                    response = self._finalize(response)
                finally:
                    project_lock.release_os_lock()
            self._project_lock = None
        assert response is not None
        if pending_base_exception is not None:
            raise pending_base_exception
        return response

    def _initialize_devices(self, test_config_path: str, hardware_owner: str) -> JsonObject | None:
        try:
            for device_id, binding in self.config.devices.items():
                self.devices[device_id] = Device(
                    device_id,
                    binding,
                    self.config,
                    self.service_factory,
                    policy_guard=self._check_policy,
                    hardware_owner=hardware_owner,
                )
        except Exception as error:
            close_results = self.close()
            return {
                "ok": False,
                "tool": "test_reactor",
                "error_type": "device_initialization_failed",
                "test_config_path": test_config_path,
                "tests": [],
                "cleanup": close_results,
                "exception": exception_result("device_initialization_failed", "Device service initialization raised an exception.", error),
                "summary": "Test reactor device initialization failed; no tests were executed.",
            }
        return None

    def _collect_hardware_state(self) -> JsonObject:
        active_resources: list[JsonObject] = []
        inspection_errors: list[JsonObject] = [*self._execution_inspection_errors]
        if self._step_in_flight is not None:
            inspection_errors.append({**self._step_in_flight, "summary": "Test reactor step completion was not confirmed."})
        for device in self.devices.values():
            try:
                state = device.hardware_state()
            except Exception as error:
                inspection_errors.append({"device": device.id, "type": "device", "error": str(error)})
                continue
            for resource in state.get("active_resources", []):
                active_resources.append({"device": device.id, **resource})
            for error in state.get("inspection_errors", []):
                inspection_errors.append({"device": device.id, **error})
        unique_errors: list[JsonObject] = []
        seen: set[tuple[object, ...]] = set()
        for item in inspection_errors:
            key = tuple(item.get(field) for field in ("type", "device", "test", "step", "action", "tool", "error_type"))
            if key not in seen:
                seen.add(key)
                unique_errors.append(item)
        return {"active": bool(active_resources or unique_errors), "state_confirmed": not unique_errors, "active_resources": active_resources, "inspection_errors": unique_errors}

    def _establish_policy_baseline(self, test_config_path: str) -> JsonObject | None:
        current_digest = read_policy_digest(self.config.config_path)
        try:
            current_config = load_config(self.config.config_path, self.config.work_dir)
        except ConfigError as error:
            return policy_changed_result(test_config_path, "Project policy became invalid before test execution.", error.to_dict())
        verified_digest = read_policy_digest(self.config.config_path)
        if current_digest is None or verified_digest is None or current_digest != verified_digest:
            return policy_changed_result(test_config_path, "Project policy could not be read consistently before test execution.")
        if current_config != self.config or (self._loaded_policy_digest is not None and verified_digest != self._loaded_policy_digest):
            return policy_changed_result(test_config_path, "Project policy changed before test execution.")
        self._policy_digest = verified_digest
        return None

    def _check_policy(self) -> JsonObject | None:
        current_digest = read_policy_digest(self.config.config_path)
        if current_digest is not None and current_digest == self._policy_digest:
            return None
        return {
            "ok": False,
            "tool": "test_reactor",
            "error_type": "policy_changed",
            "summary": "Project policy changed during the test reactor run; remaining hardware actions were blocked.",
        }

    def _finalize(self, response: JsonObject) -> JsonObject:
        try:
            return write_report(self.config, response)
        except Exception as error:
            failed = dict(response)
            if "error_type" in failed:
                failed["reactor_error_type"] = failed["error_type"]
            failed.update(
                {
                    "ok": False,
                    "error_type": "test_reactor_report_failed",
                    "report_error": exception_result("test_reactor_report_failed", "Test reactor result report could not be written.", error),
                    "summary": "Test reactor completed, but its authoritative result report could not be written.",
                }
            )
            return failed

    def _run_plan(self, plan: TestPlan) -> JsonObject:
        try:
            validation_errors = [error for test in plan.tests if (error := self.preflight(test)) is not None]
        except Exception as error:
            close_results = self.close()
            return {
                "ok": False,
                "tool": "test_reactor",
                "error_type": "preflight_exception",
                "test_config_path": plan.path,
                "tests": [],
                "cleanup": close_results,
                "exception": exception_result("preflight_exception", "Test reactor preflight raised an exception.", error),
                "summary": "Test reactor preflight failed unexpectedly; no tests were executed.",
            }
        if validation_errors:
            close_results = self.close()
            return {
                "ok": False,
                "tool": "test_reactor",
                "error_type": "test_config_invalid",
                "test_config_path": plan.path,
                "validation_errors": validation_errors,
                "tests": [],
                "cleanup": close_results,
                "summary": "Test reactor preflight failed; no tests were executed.",
            }

        results: list[JsonObject] = []
        aborted_tests: list[str] = []
        unsafe_state = False
        policy_changed = False
        try:
            for test_index, test in enumerate(plan.tests):
                device = self.devices[test.device]
                try:
                    result = self._run_test(test, device)
                except Exception as error:
                    result = {
                        "ok": False,
                        "name": test.name,
                        "device": test.device,
                        "error_type": "test_exception",
                        "summary": "Test execution raised an exception.",
                        "exception": exception_result("test_exception", "Test execution raised an exception.", error),
                    }
                results.append(result)
                error_type = result.get("error_type")
                if error_type in {"cleanup_failed", "test_exception", "hardware_state_unconfirmed"} or (error_type == "step_exception" and result.get("completion_confirmed") is not True):
                    unsafe_state = True
                    policy_changed = result.get("step_error_type") == "policy_changed"
                    aborted_tests = [remaining.name for remaining in plan.tests[test_index + 1 :]]
                    break
                if result.get("error_type") == "policy_changed":
                    policy_changed = True
                    aborted_tests = [remaining.name for remaining in plan.tests[test_index + 1 :]]
                    break
        finally:
            close_results = self.close()
        cleanup_ok = all(item["result"].get("ok") is True for item in close_results)
        cleanup_safe = all(cleanup_resource_safe(item["result"]) for item in close_results)
        if not cleanup_safe:
            unsafe_state = True
        ok = len(results) == len(plan.tests) and all(result.get("ok") is True for result in results) and cleanup_ok
        failed_tests = [str(result["name"]) for result in results if result.get("ok") is not True]
        response: JsonObject = {
            "ok": ok,
            "tool": "test_reactor",
            "test_config_path": plan.path,
            "tests": results,
            "failed_tests": failed_tests,
            "aborted_tests": aborted_tests,
            "cleanup": close_results,
            "summary": "All test reactor tests completed successfully." if ok else "One or more test reactor tests failed.",
        }
        if unsafe_state:
            response["error_type"] = "unsafe_test_state"
            response["summary"] = "Test reactor stopped because cleanup could not establish a safe state."
            if not cleanup_safe:
                response["cleanup_error_type"] = "device_close_failed"
        elif policy_changed:
            response["error_type"] = "policy_changed"
            response["summary"] = "Test reactor stopped because project policy changed during execution."
        elif not cleanup_ok:
            response["error_type"] = "audit_write_failed"
            response["hardware_state_unconfirmed"] = False
            response["summary"] = "Test reactor cleanup confirmed a safe hardware state, but one or more audit records could not be written."
        elif not ok:
            response["error_type"] = "test_failed"
        return response

    def preflight(self, test: TestCase) -> JsonObject | None:
        device = self.devices.get(test.device)
        if device is None:
            return validation_error(test, None, "device", "Test references an unknown device.")
        debug_active = False
        uart_active = False
        permissions = self.config.permissions
        for index, step in enumerate(test.steps):
            if step.action in DEBUG_ACTIONS and device.debugger.type != "openocd":
                return validation_error(test, index, "action", "Typed debug actions currently require an OpenOCD debugger.")
            if step.action in DEBUG_ACTIONS and not permissions.allow_probe:
                return validation_error(test, index, "action", "Debug actions require permissions.allow_probe.")
            if step.action in DEBUG_ACTIONS and permissions.allow_raw_debugger_commands:
                return validation_error(test, index, "action", "Typed debug actions are disabled while raw debugger commands are allowed.")
            if step.action == "uart_open":
                if device.binding.uart is None:
                    return validation_error(test, index, "action", "Device has no configured UART.")
                if not permissions.allow_com_read:
                    return validation_error(test, index, "action", "UART sessions require permissions.allow_com_read.")
                if uart_active:
                    return validation_error(test, index, "action", "UART session is already open.")
                uart_active = True
            elif step.action == "uart_close":
                if not uart_active:
                    return validation_error(test, index, "action", "UART must be opened before it can be closed.")
                uart_active = False
            elif step.action == "debug_start":
                if debug_active:
                    return validation_error(test, index, "action", "Debug session is already active.")
                artifact_error, _ = validate_artifact(device, test, index, step, require_elf=True)
                if artifact_error:
                    return artifact_error
                if step.arguments.get("mode", "attach") == "load":
                    if not permissions.allow_flash:
                        return validation_error(test, index, "mode", "Debug mode 'load' requires permissions.allow_flash.")
                    if permissions.allow_mass_erase:
                        return validation_error(test, index, "mode", "Debug mode 'load' is disabled while mass erase is allowed.")
                debug_active = True
            elif step.action == "debug_stop":
                if not debug_active:
                    return validation_error(test, index, "action", "Debug session must be started before it can be stopped.")
                debug_active = False
            elif step.action == "run_until_breakpoint":
                if not debug_active:
                    return validation_error(test, index, "action", "Debug session must be started before continuing.")
                location = step.arguments["location"]
                symbol = location if isinstance(location, str) else location.get("symbol")
                if symbol and self.config.debug.allowed_symbols and symbol not in self.config.debug.allowed_symbols:
                    return validation_error(test, index, "location", "Breakpoint symbol is not allowed by debug.allowed_symbols.")
            elif step.action == "dump_memory":
                if not debug_active:
                    return validation_error(test, index, "action", "Debug session must be started before dumping memory.")
                symbol = str(step.arguments["symbol"])
                if self.config.debug.allowed_symbols and symbol not in self.config.debug.allowed_symbols:
                    return validation_error(test, index, "symbol", "Symbol is not allowed by debug.allowed_symbols.")
                output = device.service.artifacts.validate_output_path(str(step.arguments["output_path"]), "debug_dump_symbol_ihex")
                if output.get("ok") is not True:
                    return validation_error(test, index, "output_path", str(output.get("summary", "Invalid output path.")))
            elif step.action == "flash":
                if debug_active:
                    return validation_error(test, index, "action", "Firmware cannot be flashed while a debug session is active.")
                if not permissions.allow_flash:
                    return validation_error(test, index, "action", "Flashing requires permissions.allow_flash.")
                if permissions.allow_raw_debugger_commands or permissions.allow_mass_erase:
                    return validation_error(test, index, "action", "Validated flashing is disabled by the active debugger interlock policy.")
                artifact_error, artifact_path = validate_artifact(device, test, index, step, require_elf=False)
                if artifact_error:
                    return artifact_error
                if (
                    artifact_path is not None
                    and artifact_path.suffix.lower() == ".bin"
                    and device.debugger.flash_address is None
                ):
                    return validation_error(test, index, "image_path", f"{device.debugger.type} requires debugger.flash_address for .bin artifacts.")
        return None

    def _run_test(self, test: TestCase, device: Device) -> JsonObject:
        steps: list[JsonObject] = []
        failed_step: int | None = None
        try:
            for index, step in enumerate(test.steps, start=1):
                self._step_in_flight = {"type": "operation", "test": test.name, "device": test.device, "step": index, "action": step.action}
                try:
                    if self._project_lock is not None:
                        state = self._collect_hardware_state()
                        self._project_lock.update_active_state(source=f"test_reactor:{test.name}:{index}", active_resources=state["active_resources"], operation=self._step_in_flight)
                    result = device.execute(step)
                except Exception as error:
                    completion_confirmed = getattr(error, "_agentic_hil_completion_confirmed", False) is True
                    if not completion_confirmed:
                        self._execution_inspection_errors.append({**self._step_in_flight, "error_type": type(error).__name__, "error": str(error), "summary": "Test reactor step completion was not confirmed."})
                    self._step_in_flight = None
                    result = exception_result("step_exception", "Test step raised an exception.", error)
                    if completion_confirmed:
                        result["completion_confirmed"] = True
                except BaseException as error:
                    if getattr(error, "_agentic_hil_completion_confirmed", False) is not True:
                        self._execution_inspection_errors.append({**self._step_in_flight, "error_type": type(error).__name__, "error": str(error), "summary": "Test reactor step completion was not confirmed."})
                    self._step_in_flight = None
                    raise
                else:
                    if not isinstance(result, dict):
                        error = TypeError("Test reactor device step returned a non-object result.")
                        self._execution_inspection_errors.append({**self._step_in_flight, "error_type": type(error).__name__, "error": str(error), "summary": "Test reactor step completion was not confirmed."})
                        self._step_in_flight = None
                        raise error
                    step_error_type = str(result.get("error_type", ""))
                    if result.get("completion_unconfirmed") is True or result.get("hardware_state_unconfirmed") is True or step_error_type in {"hardware_state_unconfirmed", "hardware_cleanup_failed"}:
                        self._execution_inspection_errors.append({**self._step_in_flight, "error_type": step_error_type or "completion_unconfirmed", "summary": "Test reactor step completion was not confirmed."})
                        result = {**result, "ok": False, "error_type": step_error_type or "hardware_state_unconfirmed", "hardware_state_unconfirmed": True}
                    if self._project_lock is not None:
                        state = self._collect_hardware_state()
                        self._project_lock.update_active_state(source=f"test_reactor:{test.name}:{index}", active_resources=state["active_resources"], operation=None)
                    self._step_in_flight = None
                steps.append({"index": index, "action": step.action, "result": result})
                if result.get("ok") is not True:
                    failed_step = index
                    break
        finally:
            cleanup = device.cleanup()
        cleanup_ok = all(item["result"].get("ok") is True for item in cleanup)
        cleanup_safe = all(cleanup_resource_safe(item["result"]) for item in cleanup)
        ok = failed_step is None and cleanup_ok
        result: JsonObject = {"ok": ok, "name": test.name, "device": test.device, "steps": steps, "cleanup": cleanup}
        if not cleanup_ok:
            result["error_type"] = "cleanup_failed" if not cleanup_safe else "audit_write_failed"
            if failed_step is not None:
                result["failed_step"] = failed_step
                result["step_error_type"] = steps[-1]["result"].get("error_type", "step_failed")
        elif failed_step is not None:
            result["failed_step"] = failed_step
            step_result = steps[-1]["result"]
            step_error_type = step_result.get("error_type", "step_failed")
            if step_result.get("completion_unconfirmed") is True or step_result.get("hardware_state_unconfirmed") is True or step_error_type in {"hardware_state_unconfirmed", "hardware_cleanup_failed"}:
                result["error_type"] = "hardware_state_unconfirmed"
                result["step_error_type"] = step_error_type
            else:
                result["error_type"] = step_error_type
                if step_result.get("completion_confirmed") is True:
                    result["completion_confirmed"] = True
        return result

    def close(self) -> list[JsonObject]:
        results: list[JsonObject] = []
        pending_base_exception: BaseException | None = None
        for device in self.devices.values():
            try:
                result = device.close()
            except BaseException as error:
                result = exception_result("device_close_exception", "Device service cleanup raised an exception.", error)
                if not isinstance(error, Exception) and pending_base_exception is None:
                    pending_base_exception = error
            results.append({"device": device.id, "result": result})
        if pending_base_exception is not None:
            raise pending_base_exception
        return results


def validate_artifact(device: Device, test: TestCase, index: int, step: TestStep, require_elf: bool) -> tuple[JsonObject | None, Path | None]:
    image_path = str(step.arguments["image_path"])
    validation = device.service.artifacts.validate_local_path(image_path)
    if validation.get("ok") is not True:
        return validation_error(test, index, "image_path", str(validation.get("summary", "Invalid firmware artifact."))), None
    artifact_path = Path(str(validation["artifact"]["resolved_path"]))
    if require_elf and artifact_path.suffix.lower() != ".elf":
        return validation_error(test, index, "image_path", "Debug sessions require an ELF artifact."), None
    return None, artifact_path


def validation_error(test: TestCase, index: int | None, field: str, summary: str) -> JsonObject:
    prefix = f"tests.{test.name}"
    location = f"{prefix}.steps[{index}].{field}" if index is not None else f"{prefix}.{field}"
    return {"test": test.name, "device": test.device, "field": location, "summary": summary}


def cleanup_resource_safe(result: JsonObject) -> bool:
    """True when a cleanup result proves the physical resource is safe, even if its audit record failed."""
    if result.get("ok") is True:
        return True
    return (
        result.get("completion_confirmed") is True
        and result.get("hardware_state_unconfirmed") is not True
        and result.get("completion_unconfirmed") is not True
    )


def capability_error(device: str, action: str, capability: str) -> JsonObject:
    return {"ok": False, "tool": "test_reactor", "error_type": "device_capability_unavailable", "summary": f"Device lacks {capability} capability.", "device": device, "action": action}


def exception_result(error_type: str, summary: str, error: BaseException) -> JsonObject:
    return {"ok": False, "tool": "test_reactor", "error_type": error_type, "summary": summary, "exception_type": type(error).__name__, "backend_error": str(error)}


def read_policy_digest(config_path: str) -> str | None:
    try:
        return hashlib.sha256(Path(config_path).read_bytes()).hexdigest()
    except OSError:
        return None


def policy_changed_result(test_config_path: str, summary: str, policy_error: JsonObject | None = None) -> JsonObject:
    result: JsonObject = {
        "ok": False,
        "tool": "test_reactor",
        "error_type": "policy_changed",
        "test_config_path": test_config_path,
        "tests": [],
        "cleanup": [],
        "summary": summary,
    }
    if policy_error is not None:
        result["policy_error"] = policy_error
    return result
