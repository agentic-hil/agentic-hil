from __future__ import annotations

from decimal import Decimal
from typing import Protocol

from agentic_hil.config import ConfigError
from agentic_hil.types import AgenticHILConfig, ChannelStatus, JsonObject, SupplyIdentity


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

    def opens_debug_sessions(self) -> bool:
        """Whether this backend runs typed debug sessions through a GDB server.

        Asked of the backend rather than read off its type name, so the test
        reactor admits a plan's session steps on exactly the backends that open
        sessions, with nothing else to change when another one does."""
        ...

    def target_support(self) -> JsonObject: ...

    def classify_last_error(self) -> JsonObject: ...

    def close(self) -> None: ...


class PowerSupplyBackend(Protocol):
    """What the power supply class layer needs from one supply type.

    Signatures only, and device-neutral: no command string and no raw byte
    crosses this interface. Every rule (limits, permissions, leases, identity,
    protection, exact read-back) lives in the class layer, the same for every
    backend. A backend that cannot do what it is asked raises
    `PowerSupplyFault`; it never answers with a guess."""

    def identity(self) -> SupplyIdentity:
        """Ask the supply who it is. On an unknown identity this is the only command that was sent."""
        ...

    def capabilities(self) -> JsonObject:
        """The supply type's static hardware bounds, never limits, and without I/O.

        Channels, per-channel voltage and current maximum and resolution, what is
        measured, whether output state is readable, and per protection whether it
        is `threshold`, `at_setpoint` or `unsupported` and whether its armed
        threshold can be read back."""
        ...

    def channel_status(self, channel: int) -> ChannelStatus: ...

    def set_voltage(self, channel: int, volts: Decimal) -> None: ...

    def set_current(self, channel: int, amps: Decimal) -> None: ...

    def set_output(self, channel: int, on: bool) -> None: ...

    def arm_protection(self, channel: int, ovp_v: Decimal | None, ocp_a: Decimal | None) -> None:
        """Set each given threshold to exactly that value and enable it.

        `None` leaves that protection untouched. No set point and no output state
        changes. Raises `PowerSupplyFault` when it cannot do this."""
        ...

    def evidence(self) -> list[JsonObject]:
        """The transcript of this connection: command, reply as hex with any serial masked, latency and time."""
        ...

    def classify_last_error(self) -> JsonObject: ...

    def close(self) -> None: ...


class UnboundDebuggerBackend:
    """Stands in when no single probe is bound.

    With zero or several debuggers configured there is no board a bare call
    could mean. Refusing every call here keeps a default from silently picking
    a board: the failure mode this rework exists to remove."""

    def __init__(self, config: AgenticHILConfig):
        self.config = config

    def reconfigure(self, config: AgenticHILConfig) -> None:
        self.config = config

    def close(self) -> None:
        return None

    def sessionless_debug_tools(self) -> frozenset[str]:
        """No probe is bound, so no tool runs as a standalone debugger read here.

        Answered rather than left to ``__getattr__`` (which would turn a set
        membership test in the coordination layer into a refusal dict), so the
        one-shot classification reads the same empty answer it reads from a bound
        session backend."""
        return frozenset()

    def opens_debug_sessions(self) -> bool:
        """No probe is bound, so no session can be opened on one."""
        return False

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
    if config.debugger.type == "esptool":
        from agentic_hil.backends.esptool import EsptoolBackend

        return EsptoolBackend(config)
    raise ConfigError(
        "config_invalid",
        "Unsupported debugger.type.",
        {"field": f"debuggers.{config.debugger_id}.type", "value": config.debugger.type, "allowed_values": ["openocd", "stlink", "pyocd", "esptool"]},
    )
