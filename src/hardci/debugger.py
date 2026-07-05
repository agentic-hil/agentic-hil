from __future__ import annotations

from typing import Protocol

from hardci.config import ConfigError
from hardci.types import HardCIConfig, JsonObject


class DebuggerBackend(Protocol):
    def info(self) -> JsonObject: ...

    def probe_target(self) -> JsonObject: ...

    def flash_firmware(self, artifact: JsonObject) -> JsonObject: ...

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


def create_debugger_backend(config: HardCIConfig) -> DebuggerBackend:
    if config.debugger.type == "openocd":
        from hardci.backends.openocd import OpenOCDBackend

        return OpenOCDBackend(config)
    if config.debugger.type == "stlink":
        from hardci.backends.stlink import STLinkBackend

        return STLinkBackend(config)
    if config.debugger.type == "pyocd":
        from hardci.backends.pyocd import PyOCDBackend

        return PyOCDBackend(config)
    raise ConfigError(
        "config_invalid",
        "Unsupported debugger.type.",
        {"field": "debugger.type", "value": config.debugger.type, "allowed_values": ["openocd", "stlink", "pyocd"]},
    )
