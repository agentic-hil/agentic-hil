from __future__ import annotations

from pathlib import Path

from hardci.artifacts import ArtifactManager
from hardci.can import CanBusService
from hardci.comports import ComPortService
from hardci.debugger import DebuggerBackend, create_debugger_backend
from hardci.report import read_last_report
from hardci.types import HardCIConfig, JsonObject


class HardCIToolService:
    def __init__(
        self,
        config: HardCIConfig,
        backend: DebuggerBackend | None = None,
        artifacts: ArtifactManager | None = None,
        com_ports: ComPortService | None = None,
        can_buses: CanBusService | None = None,
    ):
        self.config = config
        self.backend = backend or create_debugger_backend(config)
        self.artifacts = artifacts or ArtifactManager(config)
        self.com_ports = com_ports or ComPortService(config)
        self.can_buses = can_buses or CanBusService(config)

    def debugger_info(self) -> JsonObject:
        return self.backend.info()

    def probe_target(self) -> JsonObject:
        return self.backend.probe_target()

    def flash_firmware(self, payload: JsonObject | None = None) -> JsonObject:
        payload = payload or {}
        image_path = payload.get("image_path")
        artifact_id = payload.get("artifact_id")
        if bool(image_path) == bool(artifact_id):
            return tool_error("hardci_flash_firmware", "invalid_argument", "Provide exactly one of image_path or artifact_id.")
        validation = self.artifacts.validate_local_path(str(image_path)) if image_path else self.artifacts.resolve_artifact_id(str(artifact_id))
        if not validation["ok"]:
            return validation
        return self.backend.flash_firmware(validation["artifact"])

    def artifact_upload(self, payload: JsonObject | None = None) -> JsonObject:
        return self.artifacts.upload(payload)

    def reset_target(self, mode: str = "run") -> JsonObject:
        return self.backend.reset_target(mode)

    def debug_start_session(self, payload: JsonObject | None = None) -> JsonObject:
        payload = payload or {}
        image_path = payload.get("image_path")
        artifact_id = payload.get("artifact_id")
        if bool(image_path) == bool(artifact_id):
            return tool_error("hardci_debug_start_session", "invalid_argument", "Provide exactly one of image_path or artifact_id.")
        validation = self.artifacts.validate_local_path(str(image_path)) if image_path else self.artifacts.resolve_artifact_id(str(artifact_id), "hardci_debug_start_session")
        if not validation["ok"]:
            validation["tool"] = "hardci_debug_start_session"
            return validation
        artifact = validation["artifact"]
        if Path(str(artifact["resolved_path"])).suffix.lower() != ".elf":
            return tool_error("hardci_debug_start_session", "artifact_validation_failed", "Debug sessions require an ELF artifact with debug symbols.")
        return self.backend.debug_start_session(artifact, str(payload.get("mode", "attach")), number_argument(payload.get("timeout_s")))

    def debug_stop_session(self, payload: JsonObject | None = None) -> JsonObject:
        return self.backend.debug_stop_session(number_argument((payload or {}).get("timeout_s")))

    def debug_get_session_status(self) -> JsonObject:
        return self.backend.debug_get_session_status()

    def debug_set_breakpoint(self, payload: JsonObject | None = None) -> JsonObject:
        payload = payload or {}
        return self.backend.debug_set_breakpoint({"location": payload.get("location", payload)})

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
            return tool_error("hardci_debug_symbol_info", "invalid_argument", "symbol must be a non-empty string.")
        return self.backend.debug_symbol_info(symbol.strip())

    def debug_dump_symbol_ihex(self, payload: JsonObject | None = None) -> JsonObject:
        payload = payload or {}
        symbol = payload.get("symbol")
        output_path = payload.get("output_path")
        if not isinstance(symbol, str) or not symbol.strip():
            return tool_error("hardci_debug_dump_symbol_ihex", "invalid_argument", "symbol must be a non-empty string.")
        if not isinstance(output_path, str) or not output_path.strip():
            return tool_error("hardci_debug_dump_symbol_ihex", "invalid_argument", "output_path must be a non-empty string.")
        output = self.artifacts.validate_output_path(output_path, "hardci_debug_dump_symbol_ihex")
        if not output["ok"]:
            return output
        return self.backend.debug_dump_symbol_ihex(symbol.strip(), output["output"])

    def get_last_report(self) -> JsonObject:
        report = read_last_report(self.config)
        if not report.get("ok") and report.get("error_type") in {"report_not_found", "config_invalid"}:
            return report
        return {"ok": True, "tool": "hardci_get_last_report", "report": report}

    def classify_last_error(self) -> JsonObject:
        return self.backend.classify_last_error()

    def call(self, name: str, arguments: JsonObject | None = None) -> JsonObject:
        args = arguments or {}
        dispatch = {
            "hardci_debugger_info": lambda: self.debugger_info(),
            "hardci_probe_target": lambda: self.probe_target(),
            "hardci_flash_firmware": lambda: self.flash_firmware(args),
            "hardci_artifact_upload": lambda: self.artifact_upload(args),
            "hardci_reset_target": lambda: self.reset_target(str(args.get("mode", "run"))),
            "hardci_debug_start_session": lambda: self.debug_start_session(args),
            "hardci_debug_stop_session": lambda: self.debug_stop_session(args),
            "hardci_debug_get_session_status": lambda: self.debug_get_session_status(),
            "hardci_debug_set_breakpoint": lambda: self.debug_set_breakpoint(args),
            "hardci_debug_list_breakpoints": lambda: self.debug_list_breakpoints(),
            "hardci_debug_clear_breakpoints": lambda: self.debug_clear_breakpoints(),
            "hardci_debug_continue": lambda: self.debug_continue(args),
            "hardci_debug_halt": lambda: self.debug_halt(args),
            "hardci_debug_get_stop_reason": lambda: self.debug_get_stop_reason(),
            "hardci_debug_symbol_info": lambda: self.debug_symbol_info(args),
            "hardci_debug_dump_symbol_ihex": lambda: self.debug_dump_symbol_ihex(args),
            "hardci_get_last_report": lambda: self.get_last_report(),
            "hardci_classify_last_error": lambda: self.classify_last_error(),
            "hardci_com_ports_list": lambda: self.com_ports.list_ports(),
            "hardci_com_session_start": lambda: self.com_ports.session_start(str(args.get("port_id", "")), bool(args.get("clear_buffer", True))),
            "hardci_com_session_stop": lambda: self.com_ports.session_stop(str(args.get("port_id", ""))),
            "hardci_com_write": lambda: self.com_ports.write(str(args.get("port_id", "")), {key: value for key, value in args.items() if key in {"text", "hex"}}),
            "hardci_com_read": lambda: self.com_ports.read(str(args.get("port_id", "")), args.get("max_bytes"), args.get("wait_timeout_s", 0.0)),
            "hardci_can_buses_list": lambda: self.can_buses.list_buses(),
            "hardci_can_session_start": lambda: self.can_buses.session_start(str(args.get("bus_id", "")), bool(args.get("clear_rx_queue", True))),
            "hardci_can_session_stop": lambda: self.can_buses.session_stop(str(args.get("bus_id", ""))),
            "hardci_can_send": lambda: self.can_buses.send(str(args.get("bus_id", "")), {key: value for key, value in args.items() if key != "bus_id"}),
            "hardci_can_read": lambda: self.can_buses.read(str(args.get("bus_id", "")), args.get("max_frames"), args.get("wait_timeout_s", 0.0)),
        }
        if name in dispatch:
            return dispatch[name]()
        return {"ok": False, "tool": name, "error_type": "unknown_tool", "summary": "Unknown HardCI tool."}

    def close(self) -> None:
        self.backend.close()
        self.com_ports.close()
        self.can_buses.close()


def number_argument(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def tool_error(tool: str, error_type: str, summary: str) -> JsonObject:
    return {"ok": False, "tool": tool, "error_type": error_type, "summary": summary}
