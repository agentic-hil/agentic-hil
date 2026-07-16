from __future__ import annotations

import math
from pathlib import Path

from agentic_hil.adapters import AdapterService
from agentic_hil.artifacts import ArtifactManager
from agentic_hil.can import CanBusService
from agentic_hil.comports import ComPortService
from agentic_hil.config import ConfigError
from agentic_hil.debugger import DebuggerBackend, create_debugger_backend
from agentic_hil.report import ensure_audit_ready, read_last_report
from agentic_hil.types import AgenticHILConfig, JsonObject


class AgenticHILToolService:
    def __init__(
        self,
        config: AgenticHILConfig,
        backend: DebuggerBackend | None = None,
        artifacts: ArtifactManager | None = None,
        com_ports: ComPortService | None = None,
        can_buses: CanBusService | None = None,
        adapters: AdapterService | None = None,
    ):
        self.config = config
        self.backend = backend or create_debugger_backend(self.config)
        self.artifacts = artifacts or ArtifactManager(self.config)
        self.com_ports = com_ports or ComPortService(self.config)
        self.can_buses = can_buses or CanBusService(self.config)
        self.adapters = adapters or AdapterService(self.config)
        self._debug_artifact: JsonObject | None = None

    def debugger_info(self) -> JsonObject:
        if not self.config.permissions.allow_probe:
            return tool_error("debugger_info", "permission_denied", "Debugger execution is disabled by the authoritative config.")
        return self.backend.info()

    def debugger_probes_list(self) -> JsonObject:
        if not self.config.permissions.allow_probe:
            return tool_error("debugger_probes_list", "permission_denied", "Debugger probe discovery is disabled by the authoritative config.")
        return self.backend.list_probes()

    def probe_target(self) -> JsonObject:
        return self.backend.probe_target()

    def flash_firmware(self, payload: JsonObject | None = None) -> JsonObject:
        payload = payload or {}
        if not self.config.permissions.allow_flash:
            return tool_error("flash_firmware", "permission_denied", "Flashing is disabled by the authoritative config.")
        image_path = payload.get("image_path")
        artifact_id = payload.get("artifact_id")
        if bool(image_path) == bool(artifact_id):
            return tool_error("flash_firmware", "invalid_argument", "Provide exactly one of image_path or artifact_id.")
        reset_after_flash = payload.get("reset_after_flash", False)
        if not isinstance(reset_after_flash, bool):
            return tool_error("flash_firmware", "invalid_argument", "reset_after_flash must be a boolean.")
        if reset_after_flash and not self.config.permissions.allow_reset:
            return tool_error("flash_firmware", "permission_denied", "Post-flash reset is disabled by the authoritative config.")
        validation = self.artifacts.validate_local_path(str(image_path)) if image_path else self.artifacts.resolve_artifact_id(str(artifact_id))
        if not validation["ok"]:
            return validation
        staged = self.artifacts.stage_for_backend(validation["artifact"], "flash_firmware")
        if not staged["ok"]:
            return staged
        try:
            return self.backend.flash_firmware(staged["artifact"], reset_after_flash)
        finally:
            self.artifacts.release_stage(staged["artifact"])

    def artifact_upload(self, payload: JsonObject | None = None) -> JsonObject:
        return self.artifacts.upload(payload)

    def reset_target(self, mode: str = "run") -> JsonObject:
        if not self.config.permissions.allow_reset:
            return tool_error("reset_target", "permission_denied", "Target reset is disabled by the authoritative config.")
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
        staged = self.artifacts.stage_for_backend(artifact, "debug_start_session")
        if not staged["ok"]:
            return staged
        try:
            result = self.backend.debug_start_session(staged["artifact"], str(payload.get("mode", "attach")), number_argument(payload.get("timeout_s")))
        except Exception:
            self.artifacts.release_stage(staged["artifact"])
            raise
        if result.get("ok") or result.get("cleanup_required"):
            self._debug_artifact = staged["artifact"]
        else:
            self.artifacts.release_stage(staged["artifact"])
        return result

    def debug_stop_session(self, payload: JsonObject | None = None) -> JsonObject:
        result = self.backend.debug_stop_session(number_argument((payload or {}).get("timeout_s")))
        if result.get("ok") and self._debug_artifact is not None:
            self.artifacts.release_stage(self._debug_artifact)
            self._debug_artifact = None
        return result

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
        dispatch = {
            "debugger_info": lambda: self.debugger_info(),
            "debugger_probes_list": lambda: self.debugger_probes_list(),
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
            if name in audited_hardware_tools():
                try:
                    ensure_audit_ready(self.config)
                except (ConfigError, OSError) as error:
                    return {
                        "ok": False,
                        "tool": name,
                        "error_type": "audit_unavailable",
                        "summary": "Hardware action was not started because audit output is unavailable.",
                        "side_effect_committed": False,
                        "audit_ok": False,
                        "audit_error": error.to_dict() if isinstance(error, ConfigError) else {"error_type": type(error).__name__, "backend_error": str(error)},
                    }
            try:
                return dispatch[name]()
            except (ConfigError, OSError) as error:
                if name in audited_hardware_tools() or name in containment_tools():
                    return {
                        "ok": False,
                        "tool": name,
                        "error_type": "audit_failed_after_action",
                        "summary": "Hardware action ran, but its audit result could not be completed.",
                        "side_effect_status": "unknown",
                        "retry_safe": False,
                        "audit_ok": False,
                        "audit_error": error.to_dict() if isinstance(error, ConfigError) else {"error_type": type(error).__name__, "backend_error": str(error)},
                    }
                if isinstance(error, ConfigError):
                    return {"tool": name, **error.to_dict()}
                raise
        return {"ok": False, "tool": name, "error_type": "unknown_tool", "summary": "Unknown Agentic HIL tool."}

    def close(self) -> None:
        errors: list[tuple[str, Exception]] = []
        for name, resource in [
            ("backend", self.backend),
            ("artifacts", self.artifacts),
            ("com_ports", self.com_ports),
            ("can_buses", self.can_buses),
            ("adapters", self.adapters),
        ]:
            try:
                resource.close()
            except Exception as error:
                errors.append((name, error))
        if errors:
            details = "; ".join(f"{name}: {type(error).__name__}: {error}" for name, error in errors)
            raise RuntimeError(f"Agentic HIL service cleanup failed: {details}") from errors[0][1]


def number_argument(value: object) -> float | None:
    if value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def adapter_payload(args: JsonObject) -> JsonObject:
    return {key: value for key, value in args.items() if key != "adapter_id"}


def tool_error(tool: str, error_type: str, summary: str) -> JsonObject:
    return {"ok": False, "tool": tool, "error_type": error_type, "summary": summary}


def audited_hardware_tools() -> set[str]:
    return {
        "probe_target", "flash_firmware", "reset_target", "debug_start_session",
        "debug_set_breakpoint", "debug_continue",
        "com_session_start", "com_write", "can_session_start", "can_send",
        "adapter_session_start", "adapter_set_value", "adapter_inject_fault", "adapter_measure",
    }


def containment_tools() -> set[str]:
    return {
        "debug_stop_session", "debug_clear_breakpoints", "debug_halt",
        "com_session_stop", "can_session_stop", "adapter_session_stop", "adapter_clear_fault",
    }
