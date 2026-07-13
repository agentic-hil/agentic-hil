from __future__ import annotations

import json
import time
from pathlib import Path

from agentic_hil.backends.common import (
    command_for_log,
    contains_any,
    contains_failure_text,
    invocation,
    spawn_command,
    which,
)
from agentic_hil.backends.gdbdebug import GdbDebugSessions
from agentic_hil.config import display_path, resolve_work_path
from agentic_hil.report import logs_directory, read_last_report, timestamp_for_filename, utc_now_iso, write_report
from agentic_hil.types import AgenticHILConfig, JsonObject

OPENOCD_NOT_FOUND: JsonObject = {
    "ok": False,
    "backend": "openocd",
    "error_type": "debugger_not_found",
    "backend_error_type": "openocd_not_found",
    "summary": "Debugger executable could not be found.",
    "likely_causes": ["debugger.executable is not configured", "debugger executable is not installed", "debugger executable is not in PATH"],
}

BACKEND_ERROR_TO_PUBLIC_ERROR = {
    "openocd_not_found": "debugger_not_found",
    "interface_config_not_found": "debugger_config_not_found",
    "target_config_not_found": "debugger_config_not_found",
    "config_file_not_found": "debugger_config_not_found",
}

OPENOCD_DISABLE_TCP_SERVER_COMMANDS = ["gdb_port disabled", "tcl_port disabled", "telnet_port disabled"]
OPENOCD_SUCCESS_MARKERS = {
    "probe_target": "AGENTIC_HIL_RESULT:probe_target:ok",
    "flash_firmware": "AGENTIC_HIL_RESULT:flash_firmware:ok",
    "reset_target": "AGENTIC_HIL_RESULT:reset_target:ok",
}


class OpenOCDBackend:
    backend_name = "openocd"

    def __init__(self, config: AgenticHILConfig):
        self.config = config
        self._debug = GdbDebugSessions(
            config,
            backend_name=self.backend_name,
            resolve_server=self._resolve_executable,
            build_server_args=self._debug_server_args,
            classify_server_output=self._classify_output,
        )

    def reconfigure(self, config: AgenticHILConfig) -> None:
        debugger_changed = config.debugger != self.config.debugger or config.target != self.config.target
        debug_permission_revoked = not config.permissions.allow_probe or config.permissions.allow_raw_debugger_commands
        if debugger_changed or debug_permission_revoked:
            self._debug.close()
        self.config = config
        self._debug.config = config

    def info(self) -> JsonObject:
        resolved = self._resolve_executable()
        if not resolved["ok"]:
            return {"tool": "debugger_info", **resolved}
        command = [*invocation(str(resolved["executable_path"])), "--version"]
        completed = spawn_command(command, self.config.work_dir, min(self.config.debugger.timeout_s, 10))
        if completed.not_found:
            return {"tool": "debugger_info", **OPENOCD_NOT_FOUND}
        if completed.timed_out:
            return {
                "ok": False,
                "tool": "debugger_info",
                "backend": self.backend_name,
                "executable": resolved["executable"],
                "error_type": "timeout",
                "summary": "Debugger version check timed out.",
            }
        output = f"{completed.stdout}{completed.stderr}".strip()
        if completed.returncode != 0:
            backend_error_type = self._classify_output(output)
            error_type = self._public_error_type(backend_error_type)
            return {
                "ok": False,
                "tool": "debugger_info",
                "backend": self.backend_name,
                "executable": resolved["executable"],
                "error_type": error_type,
                "backend_error_type": backend_error_type,
                "summary": self._summary_for_error(error_type),
            }
        return {
            "ok": True,
            "tool": "debugger_info",
            "backend": self.backend_name,
            "executable": resolved["executable"],
            "probe_id": self.config.debugger.probe_id,
            "version": output.splitlines()[0] if output else "OpenOCD version output was empty.",
            "summary": "OpenOCD is available.",
        }

    def probe_target(self) -> JsonObject:
        if not self.config.permissions.allow_probe:
            return self._permission_denied("probe_target", "Probing is disabled by .agentic-hil/config.yaml.")
        marker = OPENOCD_SUCCESS_MARKERS["probe_target"]
        result = self._run_openocd("probe_target", f'init; targets; echo "{marker}"; shutdown', marker)
        if result.get("ok"):
            result["target_detected"] = True
            result["summary"] = "Target detected through OpenOCD."
        return self._write_action_report(result)

    def flash_firmware(self, artifact: JsonObject, reset_after_flash: bool = False) -> JsonObject:
        if not self.config.permissions.allow_flash:
            return self._permission_denied("flash_firmware", "Flashing is disabled by .agentic-hil/config.yaml.")
        if self.config.permissions.allow_raw_debugger_commands:
            return self._permission_denied("flash_firmware", "Flashing is disabled while raw debugger commands are allowed.")
        if self.config.permissions.allow_mass_erase:
            return self._permission_denied("flash_firmware", "Flashing is disabled while mass erase is allowed.")

        command_path = escape_tcl_double_quoted_word(openocd_path_for_command(str(artifact["resolved_path"])))
        marker = OPENOCD_SUCCESS_MARKERS["flash_firmware"]
        reset_command = " reset" if reset_after_flash else ""
        result = self._run_openocd("flash_firmware", f'program "{command_path}" verify{reset_command}; echo "{marker}"; shutdown', marker)
        result["artifact"] = {"source": artifact.get("source", "path"), "path": artifact.get("path"), "sha256": artifact.get("sha256")}
        result["verify"] = True
        result["reset_after_flash"] = reset_after_flash
        if result.get("ok"):
            result["summary"] = "Firmware flashed, verified, and target reset." if reset_after_flash else "Firmware flashed and verified. Target was not reset."
        return self._write_action_report(result)

    def reset_target(self, mode: str = "run") -> JsonObject:
        allowed_modes = ["run", "halt", "init"]
        if mode not in allowed_modes:
            return {"ok": False, "tool": "reset_target", "error_type": "invalid_argument", "summary": "Invalid reset mode.", "allowed_values": allowed_modes}
        marker = OPENOCD_SUCCESS_MARKERS["reset_target"]
        result = self._run_openocd("reset_target", f'reset {mode}; echo "{marker}"; shutdown', marker)
        result["mode"] = mode
        if result.get("ok"):
            result["summary"] = f"Target reset with mode '{mode}'."
        return self._write_action_report(result)

    def debug_start_session(self, artifact: JsonObject, mode: str = "attach", timeout_s: float | None = None) -> JsonObject:
        return self._debug.start_session(artifact, mode, timeout_s)

    def debug_stop_session(self, timeout_s: float | None = None) -> JsonObject:
        return self._debug.stop_session(timeout_s)

    def debug_get_session_status(self) -> JsonObject:
        return self._debug.get_session_status()

    def debug_set_breakpoint(self, location: JsonObject) -> JsonObject:
        return self._debug.set_breakpoint(location.get("location", ""))

    def debug_list_breakpoints(self) -> JsonObject:
        return self._debug.list_breakpoints()

    def debug_clear_breakpoints(self) -> JsonObject:
        return self._debug.clear_breakpoints()

    def debug_continue(self, timeout_s: float | None = None) -> JsonObject:
        return self._debug.continue_execution(timeout_s)

    def debug_halt(self, timeout_s: float | None = None) -> JsonObject:
        return self._debug.halt(timeout_s)

    def debug_get_stop_reason(self) -> JsonObject:
        return self._debug.get_stop_reason()

    def debug_symbol_info(self, symbol: str) -> JsonObject:
        return self._debug.symbol_info(symbol)

    def debug_dump_symbol_ihex(self, symbol: str, output: JsonObject) -> JsonObject:
        return self._debug.dump_symbol_ihex(symbol, output)

    def classify_last_error(self) -> JsonObject:
        report = read_last_report(self.config)
        if not report.get("ok") and report.get("error_type") == "report_not_found":
            return {"ok": False, "tool": "classify_last_error", "error_type": "report_not_found", "summary": "No Agentic HIL report has been written yet."}
        if report.get("ok"):
            return {"ok": True, "tool": "classify_last_error", "error_type": None, "summary": "Last Agentic HIL report did not contain an error."}
        error_type = str(report.get("error_type", "unknown_debugger_error"))
        result = {
            "ok": True,
            "tool": "classify_last_error",
            "error_type": error_type,
            "summary": report.get("summary", "Last Agentic HIL report contained an error."),
            "likely_causes": report.get("likely_causes", self._likely_causes(error_type)),
            "report_path": report.get("report_path"),
            "log_path": report.get("log_path"),
        }
        if "backend_error_type" in report:
            result["backend_error_type"] = report["backend_error_type"]
        return result

    def close(self) -> None:
        self._debug.close()

    def _debug_server_args(self, executable_path: str, gdb_port: int, reset: bool) -> list[str]:
        startup = "init; reset halt" if reset else "init; halt"
        return [
            *invocation(executable_path),
            "-f",
            self.config.debugger.interface_cfg,
            *self._probe_selection_commands(),
            "-f",
            self.config.debugger.target_cfg,
            "-c",
            "bindto 127.0.0.1",
            "-c",
            f"gdb_port {gdb_port}",
            "-c",
            "tcl_port disabled",
            "-c",
            "telnet_port disabled",
            "-c",
            startup,
        ]

    def _resolve_executable(self) -> JsonObject:
        configured = self.config.debugger.executable
        if configured:
            has_path_separator = "/" in configured or "\\" in configured
            if Path(configured).is_absolute() or has_path_separator:
                resolved = Path(resolve_work_path(self.config, configured))
                if not resolved.is_file():
                    return dict(OPENOCD_NOT_FOUND)
                return {"ok": True, "executable": str(resolved), "executable_path": str(resolved)}
            found = which(configured)
            if found is None:
                return dict(OPENOCD_NOT_FOUND)
            return {"ok": True, "executable": found, "executable_path": found}
        found = which("openocd")
        if found is None:
            return dict(OPENOCD_NOT_FOUND)
        return {"ok": True, "executable": found, "executable_path": found}

    def _run_openocd(self, tool: str, openocd_command: str, success_marker: str | None = None) -> JsonObject:
        started_at = utc_now_iso()
        start = time.perf_counter()
        resolved = self._resolve_executable()
        if not resolved["ok"]:
            return {"tool": tool, "backend": self.backend_name, "started_at": started_at, **resolved, "finished_at": utc_now_iso(), "elapsed_ms": int((time.perf_counter() - start) * 1000)}

        args = [
            *invocation(str(resolved["executable_path"])),
            "-f",
            self.config.debugger.interface_cfg,
            *self._probe_selection_commands(),
            "-f",
            self.config.debugger.target_cfg,
            *[item for command in OPENOCD_DISABLE_TCP_SERVER_COMMANDS for item in ["-c", command]],
            "-c",
            openocd_command,
        ]
        log_path = str(Path(logs_directory(self.config)) / f"openocd-{timestamp_for_filename()}-{tool}.log")
        completed = spawn_command(args, self.config.work_dir, self.config.debugger.timeout_s)
        finished_at = utc_now_iso()
        elapsed_ms = int((time.perf_counter() - start) * 1000)
        if completed.not_found:
            return {"tool": tool, "backend": self.backend_name, "started_at": started_at, **OPENOCD_NOT_FOUND, "finished_at": finished_at, "elapsed_ms": elapsed_ms}

        self._write_log(log_path, args, completed.stdout, completed.stderr, completed.returncode, completed.timed_out)
        if completed.timed_out:
            return {"ok": False, "tool": tool, "backend": self.backend_name, "started_at": started_at, "finished_at": finished_at, "elapsed_ms": elapsed_ms, "error_type": "timeout", "summary": "Debugger command timed out.", "likely_causes": self._likely_causes("timeout"), "log_path": display_path(self.config, log_path)}

        output = f"{completed.stdout}{completed.stderr}"
        if completed.returncode == 0:
            backend_error_type = self._backend_error_from_output(output, tool)
            if backend_error_type is not None:
                return self._failure_result(tool, started_at, finished_at, elapsed_ms, backend_error_type, log_path)
            if success_marker is not None and success_marker not in output:
                return self._failure_result(tool, started_at, finished_at, elapsed_ms, self._unconfirmed_backend_error_type(tool), log_path)
            result: JsonObject = {"ok": True, "tool": tool, "backend": self.backend_name, "started_at": started_at, "finished_at": finished_at, "elapsed_ms": elapsed_ms, "summary": "OpenOCD command completed successfully.", "log_path": display_path(self.config, log_path)}
            if success_marker is not None:
                result["success_confirmed"] = True
            return result
        return self._failure_result(tool, started_at, finished_at, elapsed_ms, self._classify_output(output, tool), log_path)

    def _failure_result(self, tool: str, started_at: str, finished_at: str, elapsed_ms: int, backend_error_type: str, log_path: str) -> JsonObject:
        error_type = self._public_error_type(backend_error_type)
        return {"ok": False, "tool": tool, "backend": self.backend_name, "started_at": started_at, "finished_at": finished_at, "elapsed_ms": elapsed_ms, "error_type": error_type, "backend_error_type": backend_error_type, "summary": self._summary_for_error(error_type), "likely_causes": self._likely_causes(error_type), "log_path": display_path(self.config, log_path)}

    def _backend_error_from_output(self, output: str, tool: str) -> str | None:
        backend_error_type = self._classify_output(output, tool)
        if backend_error_type != "unknown_debugger_error":
            return backend_error_type
        if contains_failure_text(output):
            return backend_error_type
        return None

    def _unconfirmed_backend_error_type(self, tool: str) -> str:
        return {"probe_target": "target_not_detected", "flash_firmware": "flash_failed", "reset_target": "reset_failed"}.get(tool, "unknown_debugger_error")

    def _write_action_report(self, result: JsonObject) -> JsonObject:
        return write_report(self.config, result)

    def _write_log(self, log_path: str, args: list[str], stdout: str, stderr: str, returncode: int | None, timed_out: bool) -> None:
        Path(log_path).write_text(json.dumps({"command": command_for_log(args), "returncode": returncode, "timed_out": timed_out, "stdout": stdout, "stderr": stderr}, indent=2) + "\n", encoding="utf-8")

    def _permission_denied(self, tool: str, summary: str) -> JsonObject:
        return {"ok": False, "tool": tool, "error_type": "permission_denied", "summary": summary}

    def _probe_selection_commands(self) -> list[str]:
        return [] if self.config.debugger.probe_id is None else ["-c", f"adapter serial {self.config.debugger.probe_id}"]

    def _classify_output(self, output: str, tool: str | None = None) -> str:
        lower = output.lower()
        interface_config = self.config.debugger.interface_cfg.lower()
        target_config = self.config.debugger.target_cfg.lower()
        if interface_config in lower and contains_any(lower, ["not found", "can't find", "couldn't find", "couldn't open"]):
            return "interface_config_not_found"
        if target_config in lower and contains_any(lower, ["not found", "can't find", "couldn't find", "couldn't open"]):
            return "target_config_not_found"
        if contains_any(lower, ["adapter not found", "no adapter", "no device found", "unable to open", "open failed", "libusb_open"]):
            return "adapter_not_found"
        if contains_any(lower, ["target not examined", "target not detected", "unable to connect", "failed to read"]):
            return "target_not_detected"
        if "verify" in lower and contains_any(lower, ["failed", "mismatch", "error"]):
            return "verify_failed"
        if "reset" in lower and contains_any(lower, ["failed", "error"]):
            return "reset_failed"
        if contains_any(lower, ["can't find", "couldn't find", "couldn't open", "not found"]):
            return "config_file_not_found"
        if tool == "flash_firmware" and contains_any(lower, ["failed", "error"]):
            return "flash_failed"
        return "unknown_debugger_error"

    def _public_error_type(self, backend_error_type: str) -> str:
        return BACKEND_ERROR_TO_PUBLIC_ERROR.get(backend_error_type, backend_error_type)

    def _summary_for_error(self, error_type: str) -> str:
        return {
            "debugger_not_found": "Debugger executable could not be found.",
            "debugger_config_not_found": "Debugger configuration file could not be found.",
            "adapter_not_found": "Debugger adapter could not be found or opened.",
            "target_not_detected": "Debugger could not detect the target.",
            "flash_failed": "Debugger failed to flash the firmware.",
            "verify_failed": "Debugger failed to verify the flashed firmware.",
            "reset_failed": "Debugger failed to reset the target.",
            "timeout": "Debugger command timed out.",
            "unknown_debugger_error": "Debugger failed with an unknown error.",
        }.get(error_type, "Debugger failed with an unknown error.")

    def _likely_causes(self, error_type: str) -> list[str]:
        return {
            "target_not_detected": ["DUT is not powered", "wrong interface configuration", "SWD/JTAG wiring issue", "debug probe already in use"],
            "adapter_not_found": ["debug probe is not connected", "debug probe driver is missing", "debug probe is already in use", "Windows USB driver is not bound to the ST-Link adapter"],
            "verify_failed": ["flash write did not persist correctly", "wrong target configuration", "firmware image does not match target memory layout"],
            "flash_failed": ["target flash is locked", "wrong target configuration", "firmware image is invalid for this target"],
            "reset_failed": ["reset line wiring issue", "target is not responding", "wrong reset configuration"],
            "timeout": ["debugger stopped responding", "debug probe or target is stuck", "timeout_s is too low for this operation"],
            "debugger_not_found": ["debugger.executable is not configured", "debugger executable is not installed", "debugger executable is not in PATH"],
            "debugger_config_not_found": ["debugger interface configuration is missing", "debugger target configuration is missing", "debugger search path is incomplete"],
        }.get(error_type, ["inspect the debugger log for details"])


def openocd_path_for_command(value: str) -> str:
    return value.replace("\\", "/") if Path(value).anchor.startswith("\\") else value


def escape_tcl_double_quoted_word(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$").replace("[", "\\[").replace("]", "\\]")
