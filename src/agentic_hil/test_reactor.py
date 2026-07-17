from __future__ import annotations

import json
import re
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
    is_path_within_frozen,
    safe_read_text,
)
from agentic_hil.report import write_report
from agentic_hil.tools import AgenticHILToolService
from agentic_hil.types import AgenticHILConfig, DeviceConfig, JsonObject

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
DEBUGGER_DEVICE_ACTIONS = {"flash", *DEBUG_ACTIONS}


@dataclass(frozen=True)
class TestStep:
    device: str
    action: str
    arguments: JsonObject


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
    validate_test_config_schema(raw, str(path))
    steps = [
        TestStep(
            device=str(step["device"]),
            action=str(step["action"]),
            arguments={key: value for key, value in step.items() if key not in {"device", "action"}},
        )
        for step in raw["steps"]
    ]
    return TestConfig(path=str(path), name=str(raw.get("name") or path.stem), steps=steps)


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


class Device:
    def __init__(self, device_id: str, config: DeviceConfig, service: AgenticHILToolService):
        self.id = device_id
        self.config = config
        self.service = service
        self._owns_debug_session = False
        self._owns_uart_session = False

    def execute(self, action: str, arguments: JsonObject) -> JsonObject:
        if action in DEBUGGER_DEVICE_ACTIONS and not self.config.debugger:
            return self._capability_error(action, "debugger")
        if action in {"uart_open", "uart_close"} and self.config.uart is None:
            return self._capability_error(action, "uart")

        if action == "flash":
            return self.service.call("flash_firmware", arguments)
        if action == "uart_open":
            self._owns_uart_session = True
            result = self.service.call("com_session_start", {"port_id": self.config.uart, "clear_buffer": arguments.get("clear_buffer", True)})
            self._owns_uart_session = result.get("ok") is True and not result.get("already_active", False)
            return result
        if action == "uart_close":
            if not self._owns_uart_session:
                return {
                    "ok": False,
                    "tool": "test_reactor",
                    "error_type": "uart_session_not_owned",
                    "summary": "Device cannot close a UART session it did not open.",
                    "device": self.id,
                    "uart": self.config.uart,
                }
            result = self.service.call("com_session_stop", {"port_id": self.config.uart})
            if result.get("ok") is True:
                self._owns_uart_session = False
            return result
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
            "device": self.id,
            "action": action,
        }

    def cleanup(self) -> list[JsonObject]:
        results: list[JsonObject] = []
        if self._owns_debug_session:
            result = self._cleanup_call("debug_stop_session")
            results.append({"device": self.id, "action": "debug_stop", "result": result})
            if result.get("ok") is True:
                self._owns_debug_session = False
        if self._owns_uart_session and self.config.uart is not None:
            result = self._cleanup_call("com_session_stop", {"port_id": self.config.uart})
            results.append({"device": self.id, "action": "uart_close", "result": result})
            if result.get("ok") is True:
                self._owns_uart_session = False
        return results

    def _run_until_breakpoint(self, arguments: JsonObject) -> JsonObject:
        breakpoint_result = self.service.call("debug_set_breakpoint", {"location": arguments["location"]})
        if breakpoint_result.get("ok") is not True:
            return breakpoint_result
        continued = self.service.call("debug_continue", {"timeout_s": arguments.get("timeout_s")})
        cleared = self.service.call("debug_clear_breakpoints")
        if cleared.get("ok") is not True:
            if continued.get("ok") is not True:
                return {**continued, "breakpoint_cleanup": cleared}
            return {
                "ok": False,
                "tool": "test_reactor",
                "error_type": "breakpoint_cleanup_failed",
                "summary": "Target stopped, but the reactor breakpoint could not be removed.",
                "device": self.id,
                "breakpoint": breakpoint_result.get("breakpoint"),
                "breakpoint_cleanup": cleared,
            }
        if continued.get("ok") is not True:
            return continued
        expected_id = breakpoint_result.get("breakpoint", {}).get("id")
        actual_id = continued.get("stop", {}).get("breakpoint_id")
        if continued.get("stop_reason") != "breakpoint_hit" or actual_id != expected_id:
            return {
                "ok": False,
                "tool": "test_reactor",
                "error_type": "unexpected_stop",
                "summary": "Target did not stop at the breakpoint created by this test step.",
                "device": self.id,
                "expected_breakpoint_id": expected_id,
                "stop": continued.get("stop"),
            }
        return {
            "ok": True,
            "tool": "test_reactor",
            "summary": "Target stopped at the expected breakpoint.",
            "breakpoint": breakpoint_result["breakpoint"],
            "stop_reason": continued["stop_reason"],
            "stop": continued["stop"],
        }

    def _capability_error(self, action: str, capability: str) -> JsonObject:
        return {
            "ok": False,
            "tool": "test_reactor",
            "error_type": "device_capability_unavailable",
            "summary": f"Device does not configure the {capability} capability required by this action.",
            "device": self.id,
            "action": action,
            "capability": capability,
        }

    def _cleanup_call(self, tool: str, arguments: JsonObject | None = None) -> JsonObject:
        try:
            return self.service.call(tool, arguments)
        except Exception as error:
            return exception_result(tool, "cleanup_exception", "Cleanup action raised an exception.", error)


class TestReactor:
    __test__ = False

    def __init__(self, config: AgenticHILConfig, service: AgenticHILToolService):
        self.config = config
        self.service = service
        self.devices = {device_id: Device(device_id, device, service) for device_id, device in config.devices.items()}

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
            return write_report(self.config, result)

        completed: list[JsonObject] = []
        failed_step: int | None = None
        try:
            for index, step in enumerate(test_config.steps, start=1):
                device = self.devices.get(step.device)
                if device is None:
                    result: JsonObject = {
                        "ok": False,
                        "tool": "test_reactor",
                        "error_type": "unknown_device",
                        "summary": "Test step references a device that is not configured in devices.",
                        "device": step.device,
                    }
                else:
                    try:
                        result = device.execute(step.action, step.arguments)
                    except Exception as error:
                        result = exception_result(
                            "test_reactor",
                            "step_exception",
                            "Test reactor step raised an exception.",
                            error,
                        )
                completed.append({"index": index, "device": step.device, "action": step.action, "result": result})
                if result.get("ok") is not True:
                    failed_step = index
                    break
        finally:
            cleanup = [result for device in reversed(list(self.devices.values())) for result in device.cleanup()]

        cleanup_errors = [item for item in cleanup if item["result"].get("ok") is not True]
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
        if failed_step is not None:
            result["failed_step"] = failed_step
            step_error_type = completed[-1]["result"].get("error_type", "step_failed")
            result["step_error_type"] = step_error_type
            result["error_type"] = "cleanup_failed" if not cleanup_ok else step_error_type
        elif not cleanup_ok:
            result["error_type"] = "cleanup_failed"
        return write_report(self.config, result)

    def preflight(self, test_config: TestConfig) -> JsonObject | None:
        debug_active = False
        active_uarts: dict[str, str] = {}
        permissions = self.config.permissions

        for index, step in enumerate(test_config.steps, start=1):
            device = self.devices.get(step.device)
            if device is None:
                return preflight_error(index, step, "device", "Test step references an unknown device.")
            if step.action in DEBUGGER_DEVICE_ACTIONS and not device.config.debugger:
                return preflight_error(index, step, "device", "Device does not configure the debugger capability.")
            if step.action in {"uart_open", "uart_close"}:
                uart = device.config.uart
                if uart is None:
                    return preflight_error(index, step, "device", "Device does not configure a UART capability.")
                if uart not in self.config.com_ports:
                    return preflight_error(
                        index,
                        step,
                        "device",
                        "Device UART is not available in the authoritative config.",
                        {"uart": uart},
                    )
                if step.action == "uart_open":
                    if not permissions.allow_com_read:
                        return preflight_error(index, step, "action", "UART opening is disabled by the authoritative config.")
                    if uart in active_uarts:
                        return preflight_error(index, step, "action", "UART session is already open in this test plan.")
                    active_uarts[uart] = step.device
                else:
                    if uart not in active_uarts:
                        return preflight_error(index, step, "action", "UART session must be opened before it can be closed.")
                    if active_uarts[uart] != step.device:
                        return preflight_error(
                            index,
                            step,
                            "action",
                            "UART session may only be closed by the device that opened it.",
                            {"uart": uart, "owner_device": active_uarts[uart]},
                        )
                    active_uarts.pop(uart)
                continue

            if step.action == "flash":
                if debug_active:
                    return preflight_error(index, step, "action", "Firmware cannot be flashed while a debug session is active.")
                if not permissions.allow_flash:
                    return preflight_error(index, step, "action", "Flashing is disabled by the authoritative config.")
                if step.arguments.get("reset_after_flash", False) and not permissions.allow_reset:
                    return preflight_error(index, step, "reset_after_flash", "Post-flash reset is disabled by the authoritative config.")
                if permissions.allow_raw_debugger_commands or permissions.allow_mass_erase:
                    return preflight_error(index, step, "action", "Flashing conflicts with the authoritative raw-debugger permission.")
                artifact_error = self._preflight_artifact(index, step, require_elf=False)
                if artifact_error is not None:
                    return artifact_error
                continue

            if step.action in DEBUG_ACTIONS and self.config.debugger.type != "openocd":
                return preflight_error(
                    index,
                    step,
                    "action",
                    "Typed debug actions currently require debugger.type 'openocd'.",
                    {"debugger_type": self.config.debugger.type},
                )
            if step.action == "debug_start":
                if debug_active:
                    return preflight_error(index, step, "action", "A debug session is already active in this test plan.")
                mode = str(step.arguments.get("mode", "attach"))
                if not permissions.allow_probe or permissions.allow_raw_debugger_commands:
                    return preflight_error(index, step, "action", "Debug sessions are disabled by the authoritative config.")
                if mode != "attach" and not permissions.allow_reset:
                    return preflight_error(index, step, "mode", f"Debug mode '{mode}' requires reset permission.")
                if mode == "load" and (not permissions.allow_flash or permissions.allow_mass_erase):
                        return preflight_error(index, step, "mode", "Debug load mode is disabled by the authoritative config.")
                artifact_error = self._preflight_artifact(index, step, require_elf=True)
                if artifact_error is not None:
                    return artifact_error
                debug_active = True
            elif step.action == "debug_stop":
                if not debug_active:
                    return preflight_error(index, step, "action", "A debug session must be started before it can be stopped.")
                debug_active = False
            elif step.action in {"run_until_breakpoint", "dump_memory"}:
                if not debug_active:
                    return preflight_error(index, step, "action", "A debug session must be started before this action.")
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
        "device": step.device,
        "action": step.action,
        "summary": summary,
        **(details or {}),
    }


def exception_result(tool: str, error_type: str, summary: str, error: Exception) -> JsonObject:
    return {
        "ok": False,
        "tool": tool,
        "error_type": error_type,
        "summary": summary,
        "exception_type": type(error).__name__,
        "backend_error": str(error),
    }
