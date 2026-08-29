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

    def debug_symbol_info(self, symbol: str, symbol_elf: JsonObject | None = None) -> JsonObject: ...

    def debug_symbol_value(self, symbol: str, symbol_elf: JsonObject | None = None) -> JsonObject: ...

    def debug_dump_symbol_ihex(self, symbol: str, output: JsonObject, symbol_elf: JsonObject | None = None) -> JsonObject: ...

    def sessionless_debug_tools(self) -> frozenset[str]: ...

    def target_support(self) -> JsonObject: ...

    def classify_last_error(self) -> JsonObject: ...

    def close(self) -> None: ...


class UnboundDebuggerBackend:
    """Stands in when no single probe is bound.

    With zero or several debuggers configured there is no board a bare call
    could mean. Refusing every call here keeps a default from silently picking
    a board — the failure mode this rework exists to remove."""

    def __init__(self, config: AgenticHILConfig):
        self.config = config

    def reconfigure(self, config: AgenticHILConfig) -> None:
        self.config = config

    def close(self) -> None:
        return None

    def sessionless_debug_tools(self) -> frozenset[str]:
        """No probe is bound, so no tool runs as a standalone debugger read here.

        Answered rather than left to ``__getattr__`` — which would turn a set
        membership test in the coordination layer into a refusal dict — so the
        one-shot classification reads the same empty answer it reads from a bound
        session backend."""
        return frozenset()

    def target_support(self) -> JsonObject:
        """Undetermined rather than refused: with no probe bound there is no
        backend to ask, and that is a fact about the binding, not a fault in the
        configuration. Refusing here would make `doctor` red on every project
        that configures no debugger at all."""
        return {
            "ok": True,
            "tool": "debugger_target_support",
            "status": "undetermined",
            "undetermined_reason": "no debugger is bound, so no backend can be asked which target types it resolves.",
            "summary": "Target support was not checked: no debugger is bound.",
        }

    def classify_last_error(self) -> JsonObject:
        """Classify the last recorded failure even with no probe bound.

        The record is written by whichever tool failed, so a UART-only or
        multi-board project must still be able to ask what went wrong. Only the
        probe-specific likely_causes lookup is unavailable here."""
        from agentic_hil.report import classify_failure_report

        return classify_failure_report(self.config, lambda error_type: ["inspect the report and log for details"])

    def _refuse(self, tool: str) -> JsonObject:
        from agentic_hil.tools import unbound_debugger_error

        return unbound_debugger_error(tool, self.config)

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)
        return lambda *args, **kwargs: self._refuse(name)


def create_debugger_backend(config: AgenticHILConfig) -> DebuggerBackend:
    if config.debugger is None:
        return UnboundDebuggerBackend(config)
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
        {"field": f"debuggers.{config.debugger_id}.type", "value": config.debugger.type, "allowed_values": ["openocd", "stlink", "pyocd"]},
    )
