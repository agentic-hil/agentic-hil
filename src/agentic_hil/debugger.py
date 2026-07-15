from __future__ import annotations

from typing import Protocol

from agentic_hil.config import ConfigError
from agentic_hil.types import AgenticHILConfig, JsonObject


class DebuggerBackend(Protocol):
    def reconfigure(self, config: AgenticHILConfig) -> None: ...

    def info(self) -> JsonObject: ...

    def list_probes(self) -> JsonObject: ...

    def probe_target(self) -> JsonObject: ...

    def flash_firmware(self, artifact: JsonObject, reset_after_flash: bool = False) -> JsonObject: ...

    def reset_target(self, mode: str = "run") -> JsonObject: ...

    def debug_start_session(self, artifact: JsonObject, mode: str = "attach", timeout_s: float | None = None) -> JsonObject: ...

    def debug_stop_session(self, timeout_s: float | None = None) -> JsonObject: ...

    def debug_get_session_status(self) -> JsonObject: ...

    def debug_set_breakpoint(self, location: JsonObject) -> JsonObject: ...

    def debug_list_breakpoints(self) -> JsonObject: ...

    def debug_clear_breakpoints(self) -> JsonObject: ...

    def debug_continue(self, timeout_s: float | None = None) -> JsonObject: ...

    def debug_halt(self, timeout_s: float | None = None) -> JsonObject: ...

    def debug_get_stop_reason(self) -> JsonObject: ...

    def debug_symbol_info(self, symbol: str) -> JsonObject: ...

    def debug_dump_symbol_ihex(self, symbol: str, output: JsonObject) -> JsonObject: ...

    def classify_last_error(self) -> JsonObject: ...

    def close(self) -> None: ...


def create_debugger_backend(config: AgenticHILConfig) -> DebuggerBackend:
    if config.debugger.type == "openocd":
        from agentic_hil.backends.openocd import OpenOCDBackend

        return OpenOCDBackend(config)
    if config.debugger.type == "stlink":
        from agentic_hil.backends.stlink import STLinkBackend

        return STLinkBackend(config)
    if config.debugger.type == "pyocd":
        from agentic_hil.backends.pyocd import PyOCDBackend

        return PyOCDBackend(config)
    raise ConfigError(
        "config_invalid",
        "Unsupported debugger.type.",
        {"field": "debugger.type", "value": config.debugger.type, "allowed_values": ["openocd", "stlink", "pyocd"]},
    )
