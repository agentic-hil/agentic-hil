from __future__ import annotations

from collections.abc import Callable
from contextlib import suppress
from dataclasses import replace
from pathlib import Path

from agentic_hil.adapters import AdapterService
from agentic_hil.artifacts import ArtifactManager
from agentic_hil.can import CanBusService
from agentic_hil.comports import ComPortService
from agentic_hil.config import ConfigError, load_config
from agentic_hil.debugger import DebuggerBackend, create_debugger_backend
from agentic_hil.hardware_lock import HardwareLockError, ProjectHardwareLock
from agentic_hil.report import read_last_report
from agentic_hil.types import AgenticHILConfig, JsonObject

HARDWARE_TOOLS = {
    "debugger_probes_list",
    "probe_target",
    "flash_firmware",
    "reset_target",
    "debug_start_session",
    "debug_stop_session",
    "debug_get_session_status",
    "debug_set_breakpoint",
    "debug_list_breakpoints",
    "debug_clear_breakpoints",
    "debug_continue",
    "debug_halt",
    "debug_get_stop_reason",
    "debug_symbol_info",
    "debug_dump_symbol_ihex",
    "com_session_start",
    "com_session_stop",
    "com_write",
    "com_read",
    "can_session_start",
    "can_session_stop",
    "can_send",
    "can_read",
    "adapter_session_start",
    "adapter_session_stop",
    "adapter_set_value",
    "adapter_inject_fault",
    "adapter_clear_fault",
    "adapter_measure",
}


class HardwareCleanupError(RuntimeError):
    def __init__(self, errors: list[tuple[str, Exception]]):
        self.errors = errors
        details = "; ".join(f"{name}: {error}" for name, error in errors)
        super().__init__(f"Hardware cleanup failed ({details}).")


class AgenticHILToolService:
    def __init__(
        self,
        config: AgenticHILConfig,
        backend: DebuggerBackend | None = None,
        artifacts: ArtifactManager | None = None,
        com_ports: ComPortService | None = None,
        can_buses: CanBusService | None = None,
        adapters: AdapterService | None = None,
        reload_config: bool = True,
        hardware_owner: str | None = None,
    ):
        self.config = config
        self.reload_config = reload_config
        self.hardware_owner = hardware_owner
        self._hardware_lock = ProjectHardwareLock(config.config_path)
        self.backend = backend or create_debugger_backend(config)
        self.artifacts = artifacts or ArtifactManager(config)
        self.com_ports = com_ports or ComPortService(config)
        self.can_buses = can_buses or CanBusService(config)
        self.adapters = adapters or AdapterService(config)

    def debugger_info(self) -> JsonObject:
        return self.backend.info()

    def debugger_probes_list(self, debugger_name: str = "default") -> JsonObject:
        if not self.config.permissions.allow_probe:
            return tool_error("debugger_probes_list", "permission_denied", "Debugger probe discovery is disabled by .agentic-hil/config.yaml.")
        if debugger_name == "default":
            return self.backend.list_probes()
        debugger = self.config.debuggers.get(debugger_name)
        if debugger is None:
            result = tool_error("debugger_probes_list", "invalid_argument", "Unknown configured debugger name.")
            result["debugger"] = debugger_name
            return result
        backend = create_debugger_backend(replace(self.config, debugger=debugger))
        try:
            result = backend.list_probes()
            result["debugger"] = debugger_name
            return result
        finally:
            backend.close()

    def probe_target(self) -> JsonObject:
        return self.backend.probe_target()

    def flash_firmware(self, payload: JsonObject | None = None) -> JsonObject:
        payload = payload or {}
        if not self.config.permissions.allow_flash:
            return tool_error("flash_firmware", "permission_denied", "Flashing is disabled by .agentic-hil/config.yaml.")
        image_path = payload.get("image_path")
        artifact_id = payload.get("artifact_id")
        if bool(image_path) == bool(artifact_id):
            return tool_error("flash_firmware", "invalid_argument", "Provide exactly one of image_path or artifact_id.")
        reset_after_flash = payload.get("reset_after_flash", False)
        if not isinstance(reset_after_flash, bool):
            return tool_error("flash_firmware", "invalid_argument", "reset_after_flash must be a boolean.")
        validation = self.artifacts.validate_local_path(str(image_path)) if image_path else self.artifacts.resolve_artifact_id(str(artifact_id))
        if not validation["ok"]:
            return validation
        return self.backend.flash_firmware(validation["artifact"], reset_after_flash)

    def artifact_upload(self, payload: JsonObject | None = None) -> JsonObject:
        return self.artifacts.upload(payload)

    def reset_target(self, mode: str = "run") -> JsonObject:
        return self.backend.reset_target(mode)

    def debug_start_session(self, payload: JsonObject | None = None) -> JsonObject:
        payload = payload or {}
        image_path = payload.get("image_path")
        artifact_id = payload.get("artifact_id")
        if bool(image_path) == bool(artifact_id):
            return tool_error("debug_start_session", "invalid_argument", "Provide exactly one of image_path or artifact_id.")
        validation = self.artifacts.validate_local_path(str(image_path)) if image_path else self.artifacts.resolve_artifact_id(str(artifact_id), "debug_start_session")
        if not validation["ok"]:
            validation["tool"] = "debug_start_session"
            return validation
        artifact = validation["artifact"]
        if Path(str(artifact["resolved_path"])).suffix.lower() != ".elf":
            return tool_error("debug_start_session", "artifact_validation_failed", "Debug sessions require an ELF artifact with debug symbols.")
        return self.backend.debug_start_session(artifact, str(payload.get("mode", "attach")), number_argument(payload.get("timeout_s")))

    def debug_stop_session(self, payload: JsonObject | None = None) -> JsonObject:
        return self.backend.debug_stop_session(number_argument((payload or {}).get("timeout_s")))

    def debug_get_session_status(self) -> JsonObject:
        return self.backend.debug_get_session_status()

    def debug_set_breakpoint(self, payload: JsonObject | None = None) -> JsonObject:
        location = (payload or {}).get("location")
        has_symbol_location = isinstance(location, str) and bool(location.strip())
        has_typed_location = isinstance(location, dict) and bool(location)
        if not has_symbol_location and not has_typed_location:
            return tool_error("debug_set_breakpoint", "invalid_argument", "location must be a non-empty string or object.")
        return self.backend.debug_set_breakpoint({"location": location})

    def debug_list_breakpoints(self) -> JsonObject:
        return self.backend.debug_list_breakpoints()

    def debug_clear_breakpoints(self) -> JsonObject:
        return self.backend.debug_clear_breakpoints()

    def debug_continue(self, payload: JsonObject | None = None) -> JsonObject:
        return self.backend.debug_continue(number_argument((payload or {}).get("timeout_s")))

    def debug_halt(self, payload: JsonObject | None = None) -> JsonObject:
        return self.backend.debug_halt(number_argument((payload or {}).get("timeout_s")))

    def debug_get_stop_reason(self) -> JsonObject:
        return self.backend.debug_get_stop_reason()

    def debug_symbol_info(self, payload: JsonObject | None = None) -> JsonObject:
        symbol = (payload or {}).get("symbol")
        if not isinstance(symbol, str) or not symbol.strip():
            return tool_error("debug_symbol_info", "invalid_argument", "symbol must be a non-empty string.")
        return self.backend.debug_symbol_info(symbol.strip())

    def debug_dump_symbol_ihex(self, payload: JsonObject | None = None) -> JsonObject:
        payload = payload or {}
        symbol = payload.get("symbol")
        output_path = payload.get("output_path")
        if not isinstance(symbol, str) or not symbol.strip():
            return tool_error("debug_dump_symbol_ihex", "invalid_argument", "symbol must be a non-empty string.")
        if not isinstance(output_path, str) or not output_path.strip():
            return tool_error("debug_dump_symbol_ihex", "invalid_argument", "output_path must be a non-empty string.")
        output = self.artifacts.validate_output_path(output_path, "debug_dump_symbol_ihex")
        if not output["ok"]:
            return output
        return self.backend.debug_dump_symbol_ihex(symbol.strip(), output["output"])

    def get_last_report(self) -> JsonObject:
        report = read_last_report(self.config)
        if not report.get("ok") and report.get("error_type") in {"report_not_found", "config_invalid"}:
            return report
        return {"ok": True, "tool": "get_last_report", "report": report}

    def classify_last_error(self) -> JsonObject:
        return self.backend.classify_last_error()

    def call(self, name: str, arguments: JsonObject | None = None) -> JsonObject:
        args = arguments or {}
        hardware_error = self._acquire_hardware(name)
        if hardware_error is not None:
            return hardware_error
        try:
            reload_error = self._reload_config(name)
            if reload_error is not None:
                return reload_error
            return self._dispatch(name, args)
        except Exception:
            if name == "debug_start_session":
                with suppress(Exception):
                    self.backend.close()
            raise
        finally:
            self._reconcile_hardware_lease()

    def _dispatch(self, name: str, args: JsonObject) -> JsonObject:
        dispatch = {
            "debugger_info": lambda: self.debugger_info(),
            "debugger_probes_list": lambda: self.debugger_probes_list(str(args.get("debugger", "default"))),
            "probe_target": lambda: self.probe_target(),
            "flash_firmware": lambda: self.flash_firmware(args),
            "artifact_upload": lambda: self.artifact_upload(args),
            "reset_target": lambda: self.reset_target(str(args.get("mode", "run"))),
            "debug_start_session": lambda: self.debug_start_session(args),
            "debug_stop_session": lambda: self.debug_stop_session(args),
            "debug_get_session_status": lambda: self.debug_get_session_status(),
            "debug_set_breakpoint": lambda: self.debug_set_breakpoint(args),
            "debug_list_breakpoints": lambda: self.debug_list_breakpoints(),
            "debug_clear_breakpoints": lambda: self.debug_clear_breakpoints(),
            "debug_continue": lambda: self.debug_continue(args),
            "debug_halt": lambda: self.debug_halt(args),
            "debug_get_stop_reason": lambda: self.debug_get_stop_reason(),
            "debug_symbol_info": lambda: self.debug_symbol_info(args),
            "debug_dump_symbol_ihex": lambda: self.debug_dump_symbol_ihex(args),
            "get_last_report": lambda: self.get_last_report(),
            "classify_last_error": lambda: self.classify_last_error(),
            "com_ports_list": lambda: self.com_ports.list_ports(),
            "com_session_start": lambda: self.com_ports.session_start(str(args.get("port_id", "")), bool(args.get("clear_buffer", True))),
            "com_session_stop": lambda: self.com_ports.session_stop(str(args.get("port_id", ""))),
            "com_write": lambda: self.com_ports.write(str(args.get("port_id", "")), {key: value for key, value in args.items() if key in {"text", "hex"}}),
            "com_read": lambda: self.com_ports.read(str(args.get("port_id", "")), args.get("max_bytes"), args.get("wait_timeout_s", 0.0)),
            "can_buses_list": lambda: self.can_buses.list_buses(),
            "can_session_start": lambda: self.can_buses.session_start(str(args.get("bus_id", "")), bool(args.get("clear_rx_queue", True))),
            "can_session_stop": lambda: self.can_buses.session_stop(str(args.get("bus_id", ""))),
            "can_send": lambda: self.can_buses.send(str(args.get("bus_id", "")), {key: value for key, value in args.items() if key != "bus_id"}),
            "can_read": lambda: self.can_buses.read(str(args.get("bus_id", "")), args.get("max_frames"), args.get("wait_timeout_s", 0.0)),
            "adapters_list": lambda: self.adapters.list_adapters(),
            "adapter_session_start": lambda: self.adapters.session_start(str(args.get("adapter_id", ""))),
            "adapter_session_stop": lambda: self.adapters.session_stop(str(args.get("adapter_id", ""))),
            "adapter_set_value": lambda: self.adapters.set_value(str(args.get("adapter_id", "")), adapter_payload(args)),
            "adapter_inject_fault": lambda: self.adapters.inject_fault(str(args.get("adapter_id", "")), adapter_payload(args)),
            "adapter_clear_fault": lambda: self.adapters.clear_fault(str(args.get("adapter_id", "")), adapter_payload(args)),
            "adapter_measure": lambda: self.adapters.measure(str(args.get("adapter_id", "")), adapter_payload(args)),
        }
        if name in dispatch:
            return dispatch[name]()
        return {"ok": False, "tool": name, "error_type": "unknown_tool", "summary": "Unknown Agentic HIL tool."}

    def _acquire_hardware(self, tool: str) -> JsonObject | None:
        if tool not in HARDWARE_TOOLS:
            return None
        if self.hardware_owner is not None:
            if ProjectHardwareLock.owner_is_active(self.config.config_path, self.hardware_owner):
                return None
            return tool_error(tool, "hardware_owner_invalid", "Hardware lease owner is no longer active.")
        if self._hardware_lock.handle is not None:
            return None
        try:
            acquired = self._hardware_lock.acquire()
        except HardwareLockError as error:
            result = tool_error(tool, "hardware_lock_failed", "Project hardware lease could not be acquired.")
            result["backend_error"] = str(error)
            return result
        if acquired:
            return None
        return tool_error(tool, "hardware_busy", "Project hardware is in use by another Agentic HIL process.")

    def _reconcile_hardware_lease(self) -> None:
        if self.hardware_owner is not None:
            return
        active = (
            self.backend.has_active_session()
            or self.com_ports.has_active_sessions()
            or self.can_buses.has_active_sessions()
            or self.adapters.has_active_sessions()
        )
        if not active:
            self._hardware_lock.release()
            return
        if self._hardware_lock.handle is None and not self._hardware_lock.acquire():
            raise HardwareLockError("Active hardware session exists without ownership of the project hardware lease.")

    def _reload_config(self, tool: str) -> JsonObject | None:
        if not self.reload_config:
            return None
        try:
            config = load_config(self.config.config_path, self.config.work_dir)
        except ConfigError as error:
            return {"tool": tool, **error.to_dict()}
        if config == self.config:
            return None

        if config.debugger.type == self.config.debugger.type:
            self.backend.reconfigure(config)
        else:
            backend = create_debugger_backend(config)
            self.backend.close()
            self.backend = backend
        self.artifacts.reconfigure(config)
        self.com_ports.reconfigure(config)
        self.can_buses.reconfigure(config)
        self.adapters.reconfigure(config)
        self.config = config
        return None

    def cleanup_test_sessions(self) -> None:
        self._close_subsystems(
            (
                ("adapters", self.adapters.close),
                ("com", self.com_ports.close),
                ("can", self.can_buses.close),
            )
        )

    def close(self) -> None:
        self._close_subsystems(
            (
                ("debugger", self.backend.close),
                ("com", self.com_ports.close),
                ("can", self.can_buses.close),
                ("adapters", self.adapters.close),
            )
        )

    def _close_subsystems(self, actions: tuple[tuple[str, Callable[[], None]], ...]) -> None:
        errors: list[tuple[str, Exception]] = []
        for name, close_action in actions:
            try:
                close_action()
            except Exception as error:
                errors.append((name, error))
        try:
            self._reconcile_hardware_lease()
        except Exception as error:
            errors.append(("hardware_lease", error))
        if errors:
            raise HardwareCleanupError(errors)


def adapter_payload(args: JsonObject) -> JsonObject:
    return {key: value for key, value in args.items() if key != "adapter_id"}


def number_argument(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def tool_error(tool: str, error_type: str, summary: str) -> JsonObject:
    return {"ok": False, "tool": tool, "error_type": error_type, "summary": summary}
