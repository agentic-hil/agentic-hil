from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, replace
from importlib import resources
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator, SchemaError
from yaml.constructor import ConstructorError
from yaml.resolver import BaseResolver

from agentic_hil.config import ConfigError
from agentic_hil.tools import AgenticHILToolService
from agentic_hil.types import AgenticHILConfig, DeviceConfig, JsonObject

TEST_CONFIG_SCHEMA_RESOURCE = "schemas/testconfig.schema.json"
DEBUG_ACTIONS = {"debug_start", "run_until_breakpoint", "dump_memory", "debug_stop"}


class UniqueKeyLoader(yaml.SafeLoader):
    pass


def construct_unique_mapping(loader: UniqueKeyLoader, node: yaml.MappingNode, deep: bool = False) -> JsonObject:
    loader.flatten_mapping(node)
    mapping: JsonObject = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in mapping
        except TypeError as error:
            raise ConstructorError("while constructing a mapping", node.start_mark, "found an unhashable key", key_node.start_mark) from error
        if duplicate:
            raise ConstructorError("while constructing a mapping", node.start_mark, f"found duplicate key {key!r}", key_node.start_mark)
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


UniqueKeyLoader.add_constructor(BaseResolver.DEFAULT_MAPPING_TAG, construct_unique_mapping)


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


class ProjectTestLock:
    def __init__(self, project_root: str):
        project_key = hashlib.sha256(os.path.normcase(str(Path(project_root).resolve())).encode("utf-8")).hexdigest()
        self.path = Path(tempfile.gettempdir()) / "agentic-hil" / f"test-reactor-{project_key}.lock"
        self.handle: Any = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt

                self.handle.seek(0)
                if self.handle.read(1) == b"":
                    self.handle.seek(0)
                    self.handle.write(b"0")
                    self.handle.flush()
                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.handle.close()
            self.handle = None
            return False
        return True

    def release(self) -> None:
        if self.handle is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        finally:
            self.handle.close()
        self.handle = None


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
    ):
        self.id = device_id
        self.binding = binding
        self.debugger = root_config.debugger if binding.debugger == "default" else root_config.debuggers[binding.debugger]
        self.config = replace(root_config, target=binding.target, debugger=self.debugger)
        self.service = service_factory(self.config) if service_factory else AgenticHILToolService(self.config, reload_config=False)
        self._owns_debug = False
        self._owns_uart = False

    def execute(self, step: TestStep) -> JsonObject:
        action = step.action
        arguments = step.arguments
        if action == "flash":
            return self.service.call("flash_firmware", arguments)
        if action == "uart_open":
            if self.binding.uart is None:
                return capability_error(self.id, action, "uart")
            self._owns_uart = True
            result = self.service.call("com_session_start", {"port_id": self.binding.uart, "clear_buffer": arguments.get("clear_buffer", True)})
            self._owns_uart = result.get("ok") is True and not result.get("already_active", False)
            return result
        if action == "uart_close":
            if self.binding.uart is None:
                return capability_error(self.id, action, "uart")
            result = self.service.call("com_session_stop", {"port_id": self.binding.uart})
            if result.get("ok") is True:
                self._owns_uart = False
            return result
        if action == "debug_start":
            self._owns_debug = True
            result = self.service.call("debug_start_session", arguments)
            if result.get("ok") is not True:
                self._owns_debug = False
            return result
        if action == "run_until_breakpoint":
            return self._run_until_breakpoint(arguments)
        if action == "dump_memory":
            return self.service.call("debug_dump_symbol_ihex", arguments)
        if action == "debug_stop":
            result = self.service.call("debug_stop_session", arguments)
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
            return exception_result("device_close_exception", "Device service cleanup raised an exception.", error)
        return {"ok": True}

    def _run_until_breakpoint(self, arguments: JsonObject) -> JsonObject:
        breakpoint_result = self.service.call("debug_set_breakpoint", {"location": arguments["location"]})
        if breakpoint_result.get("ok") is not True:
            return breakpoint_result
        continued = self.service.call("debug_continue", {"timeout_s": arguments.get("timeout_s")})
        cleared = self.service.call("debug_clear_breakpoints")
        if cleared.get("ok") is not True:
            return {"ok": False, "tool": "test_reactor", "error_type": "breakpoint_cleanup_failed", "summary": "Reactor breakpoint could not be removed.", "breakpoint_cleanup": cleared}
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


class TestReactor:
    def __init__(
        self,
        config: AgenticHILConfig,
        service_factory: Callable[[AgenticHILConfig], AgenticHILToolService] | None = None,
    ):
        self.config = config
        self.devices = {
            device_id: Device(device_id, binding, config, service_factory)
            for device_id, binding in config.devices.items()
        }

    def run(self, plan: TestPlan) -> JsonObject:
        project_lock = ProjectTestLock(self.config.config_path)
        if not project_lock.acquire():
            close_results = self.close()
            return {
                "ok": False,
                "tool": "test_reactor",
                "error_type": "test_reactor_busy",
                "test_config_path": plan.path,
                "tests": [],
                "cleanup": close_results,
                "summary": "Another test reactor run is already active for this project.",
            }
        try:
            return self._run_plan(plan)
        finally:
            project_lock.release()

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
                if result.get("error_type") in {"cleanup_failed", "test_exception"}:
                    unsafe_state = True
                    aborted_tests = [remaining.name for remaining in plan.tests[test_index + 1 :]]
                    break
        finally:
            close_results = self.close()
        cleanup_ok = all(item["result"].get("ok") is True for item in close_results)
        ok = len(results) == len(plan.tests) and all(result.get("ok") is True for result in results) and cleanup_ok
        response: JsonObject = {
            "ok": ok,
            "tool": "test_reactor",
            "test_config_path": plan.path,
            "tests": results,
            "aborted_tests": aborted_tests,
            "cleanup": close_results,
            "summary": "All test reactor tests completed successfully." if ok else "One or more test reactor tests failed.",
        }
        if unsafe_state:
            response["error_type"] = "unsafe_test_state"
            response["summary"] = "Test reactor stopped because cleanup could not establish a safe state."
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
                try:
                    result = device.execute(step)
                except Exception as error:
                    result = exception_result("step_exception", "Test step raised an exception.", error)
                steps.append({"index": index, "action": step.action, "result": result})
                if result.get("ok") is not True:
                    failed_step = index
                    break
        finally:
            cleanup = device.cleanup()
        cleanup_ok = all(item["result"].get("ok") is True for item in cleanup)
        ok = failed_step is None and cleanup_ok
        result: JsonObject = {"ok": ok, "name": test.name, "device": test.device, "steps": steps, "cleanup": cleanup}
        if not cleanup_ok:
            result["error_type"] = "cleanup_failed"
            if failed_step is not None:
                result["failed_step"] = failed_step
                result["step_error_type"] = steps[-1]["result"].get("error_type", "step_failed")
        elif failed_step is not None:
            result["failed_step"] = failed_step
            result["error_type"] = steps[-1]["result"].get("error_type", "step_failed")
        return result

    def close(self) -> list[JsonObject]:
        return [
            {"device": device.id, "result": device.close()}
            for device in self.devices.values()
        ]


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


def capability_error(device: str, action: str, capability: str) -> JsonObject:
    return {"ok": False, "tool": "test_reactor", "error_type": "device_capability_unavailable", "summary": f"Device lacks {capability} capability.", "device": device, "action": action}


def exception_result(error_type: str, summary: str, error: Exception) -> JsonObject:
    return {"ok": False, "tool": "test_reactor", "error_type": error_type, "summary": summary, "exception_type": type(error).__name__, "backend_error": str(error)}
