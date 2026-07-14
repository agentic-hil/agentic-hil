from __future__ import annotations

import json
import time
from pathlib import Path

from agentic_hil.backends.common import (
    command_for_log,
    contains_any,
    contains_failure_text,
    find_stm32_programmer_cli,
    invocation,
    spawn_command,
    which,
)
from agentic_hil.config import display_path, resolve_work_path, safe_write_text
from agentic_hil.report import logs_directory, read_last_report, timestamp_for_filename, utc_now_iso, write_report
from agentic_hil.types import AgenticHILConfig, JsonObject

STLINK_NOT_FOUND: JsonObject = {
    "ok": False,
    "backend": "stlink",
    "error_type": "debugger_not_found",
    "backend_error_type": "stm32_programmer_cli_not_found",
    "summary": "STM32CubeProgrammer CLI executable could not be found.",
    "likely_causes": ["debugger.executable is not configured", "STM32CubeProgrammer is not installed", "STM32_Programmer_CLI executable is not in PATH"],
}

BACKEND_ERROR_TO_PUBLIC_ERROR = {
    "stm32_programmer_cli_not_found": "debugger_not_found",
    "probe_not_found": "adapter_not_found",
    "probe_unconfirmed": "target_not_detected",
    "flash_unconfirmed": "flash_failed",
    "reset_unconfirmed": "reset_failed",
}

STLINK_SUCCESS_CONFIRMATION = {
    "probe_target": ["ST-LINK SN", "Device name"],
    "flash_firmware": ["Download verified successfully"],
    "reset_target": ["MCU Reset", "reset is performed"],
}


class STLinkBackend:
    backend_name = "stlink"

    def __init__(self, config: AgenticHILConfig):
        self.config = config

    def reconfigure(self, config: AgenticHILConfig) -> None:
        self.config = config

    def info(self) -> JsonObject:
        resolved = self._resolve_executable()
        if not resolved["ok"]:
            return {"tool": "debugger_info", **resolved}
        command = [*invocation(str(resolved["executable_path"])), "--version"]
        completed = spawn_command(command, str(Path(str(resolved["executable_path"])).parent), min(self.config.debugger.timeout_s, 10))
        if completed.not_found:
            return {"tool": "debugger_info", **STLINK_NOT_FOUND}
        if completed.timed_out:
            return {"ok": False, "tool": "debugger_info", "backend": self.backend_name, "executable": resolved["executable"], "error_type": "timeout", "summary": "Debugger version check timed out."}
        output = f"{completed.stdout}{completed.stderr}".strip()
        if completed.returncode != 0:
            backend_error_type = self._classify_output(output)
            error_type = self._public_error_type(backend_error_type)
            return {"ok": False, "tool": "debugger_info", "backend": self.backend_name, "executable": resolved["executable"], "error_type": error_type, "backend_error_type": backend_error_type, "summary": self._summary_for_error(error_type)}
        return {"ok": True, "tool": "debugger_info", "backend": self.backend_name, "executable": resolved["executable"], "probe_id": self.config.debugger.probe_id, "interface": self.config.debugger.interface, "version": version_line(output), "summary": "STM32CubeProgrammer CLI is available."}

    def probe_target(self) -> JsonObject:
        if not self.config.permissions.allow_probe:
            return self._permission_denied("probe_target", "Probing is disabled by the effective policy.")
        result = self._run_stlink("probe_target", self._connection_args())
        if result.get("ok"):
            result["target_detected"] = True
            result["summary"] = "Target detected through ST-Link."
        return self._write_action_report(result)

    def flash_firmware(self, artifact: JsonObject, reset_after_flash: bool = False) -> JsonObject:
        if not self.config.permissions.allow_flash:
            return self._permission_denied("flash_firmware", "Flashing is disabled by the effective policy.")
        if self.config.permissions.allow_raw_debugger_commands:
            return self._permission_denied("flash_firmware", "Flashing is disabled while raw debugger commands are allowed.")
        if self.config.permissions.allow_mass_erase:
            return self._permission_denied("flash_firmware", "Flashing is disabled while mass erase is allowed.")

        artifact_path = str(artifact["resolved_path"])
        write_args = ["-w", artifact_path]
        if Path(artifact_path).suffix.lower() == ".bin":
            if self.config.debugger.flash_address is None:
                return {"ok": False, "tool": "flash_firmware", "backend": self.backend_name, "error_type": "invalid_argument", "summary": "Flashing .bin artifacts with ST-Link requires debugger.flash_address.", "artifact": {"source": artifact.get("source", "path"), "path": artifact.get("path"), "sha256": artifact.get("sha256")}}
            write_args.append(self.config.debugger.flash_address)
        reset_args = ["-rst"] if reset_after_flash else []
        result = self._run_stlink("flash_firmware", [*self._connection_args(), *write_args, "-v", *reset_args])
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
        mode_args = {"run": ["-rst"], "halt": ["-halt"], "init": ["-halt"]}
        result = self._run_stlink("reset_target", [*self._connection_args(), *mode_args[mode]])
        result["mode"] = mode
        if result.get("ok"):
            result["summary"] = f"Target reset with mode '{mode}'."
        return self._write_action_report(result)

    def debug_start_session(self, artifact: JsonObject | None = None, mode: str = "attach", timeout_s: float | None = None) -> JsonObject:
        return self._unsupported_debug_tool("debug_start_session")

    def debug_stop_session(self, timeout_s: float | None = None) -> JsonObject:
        return self._unsupported_debug_tool("debug_stop_session")

    def debug_get_session_status(self) -> JsonObject:
        return self._unsupported_debug_tool("debug_get_session_status")

    def debug_set_breakpoint(self, location: JsonObject | None = None) -> JsonObject:
        return self._unsupported_debug_tool("debug_set_breakpoint")

    def debug_list_breakpoints(self) -> JsonObject:
        return self._unsupported_debug_tool("debug_list_breakpoints")

    def debug_clear_breakpoints(self) -> JsonObject:
        return self._unsupported_debug_tool("debug_clear_breakpoints")

    def debug_continue(self, timeout_s: float | None = None) -> JsonObject:
        return self._unsupported_debug_tool("debug_continue")

    def debug_halt(self, timeout_s: float | None = None) -> JsonObject:
        return self._unsupported_debug_tool("debug_halt")

    def debug_get_stop_reason(self) -> JsonObject:
        return self._unsupported_debug_tool("debug_get_stop_reason")

    def debug_symbol_info(self, symbol: str = "") -> JsonObject:
        return self._unsupported_debug_tool("debug_symbol_info")

    def debug_dump_symbol_ihex(self, symbol: str = "", output: JsonObject | None = None) -> JsonObject:
        return self._unsupported_debug_tool("debug_dump_symbol_ihex")

    def close(self) -> None:
        return None

    def classify_last_error(self) -> JsonObject:
        report = read_last_report(self.config)
        if not report.get("ok") and report.get("error_type") == "report_not_found":
            return {"ok": False, "tool": "classify_last_error", "error_type": "report_not_found", "summary": "No Agentic HIL report has been written yet."}
        if report.get("ok"):
            return {"ok": True, "tool": "classify_last_error", "error_type": None, "summary": "Last Agentic HIL report did not contain an error."}
        error_type = str(report.get("error_type", "unknown_debugger_error"))
        result = {"ok": True, "tool": "classify_last_error", "error_type": error_type, "summary": report.get("summary", "Last Agentic HIL report contained an error."), "likely_causes": report.get("likely_causes", self._likely_causes(error_type)), "report_path": report.get("report_path"), "log_path": report.get("log_path")}
        if "backend_error_type" in report:
            result["backend_error_type"] = report["backend_error_type"]
        return result

    def _resolve_executable(self) -> JsonObject:
        configured = self.config.debugger.executable
        if configured:
            has_path_separator = "/" in configured or "\\" in configured
            if Path(configured).is_absolute() or has_path_separator:
                resolved = Path(resolve_work_path(self.config, configured))
                if not resolved.is_file():
                    return dict(STLINK_NOT_FOUND)
                return {"ok": True, "executable": str(resolved), "executable_path": str(resolved)}
            found = which(configured)
            if found is None:
                return dict(STLINK_NOT_FOUND)
            return {"ok": True, "executable": found, "executable_path": found}
        found = find_stm32_programmer_cli()
        if found is None:
            return dict(STLINK_NOT_FOUND)
        return {"ok": True, "executable": found, "executable_path": found}

    def _run_stlink(self, tool: str, action_args: list[str]) -> JsonObject:
        started_at = utc_now_iso()
        start = time.perf_counter()
        resolved = self._resolve_executable()
        if not resolved["ok"]:
            return {"tool": tool, "backend": self.backend_name, "started_at": started_at, **resolved, "finished_at": utc_now_iso(), "elapsed_ms": int((time.perf_counter() - start) * 1000)}
        args = [*invocation(str(resolved["executable_path"])), "-q", *action_args]
        log_path = str(Path(logs_directory(self.config)) / f"stlink-{timestamp_for_filename()}-{tool}.log")
        completed = spawn_command(args, str(Path(str(resolved["executable_path"])).parent), self.config.debugger.timeout_s)
        finished_at = utc_now_iso()
        elapsed_ms = int((time.perf_counter() - start) * 1000)
        if completed.not_found:
            return {"tool": tool, "backend": self.backend_name, "started_at": started_at, **STLINK_NOT_FOUND, "finished_at": finished_at, "elapsed_ms": elapsed_ms}
        self._write_log(log_path, args, completed.stdout, completed.stderr, completed.returncode, completed.timed_out)
        if completed.timed_out:
            return {"ok": False, "tool": tool, "backend": self.backend_name, "started_at": started_at, "finished_at": finished_at, "elapsed_ms": elapsed_ms, "error_type": "timeout", "summary": "Debugger command timed out.", "likely_causes": self._likely_causes("timeout"), "log_path": display_path(self.config, log_path)}
        output = f"{completed.stdout}{completed.stderr}"
        if completed.returncode == 0:
            backend_error_type = self._backend_error_from_output(output, tool)
            if backend_error_type is not None:
                return self._failure_result(tool, started_at, finished_at, elapsed_ms, backend_error_type, log_path)
            confirmation = self._confirm_operation_success(output, tool)
            if not confirmation["confirmed"]:
                return self._failure_result(tool, started_at, finished_at, elapsed_ms, self._unconfirmed_backend_error_type(tool), log_path, {"confirmed": False, "expected_success_text": confirmation["expected"]})
            return {"ok": True, "tool": tool, "backend": self.backend_name, "started_at": started_at, "finished_at": finished_at, "elapsed_ms": elapsed_ms, "success_confirmed": True, "operation_result": {"confirmed": True, "matched_success_text": confirmation["matched"]}, "summary": "STM32CubeProgrammer CLI command completed successfully.", "log_path": display_path(self.config, log_path)}
        return self._failure_result(tool, started_at, finished_at, elapsed_ms, self._classify_output(output, tool), log_path)

    def _connection_args(self) -> list[str]:
        args = ["-c", f"port={self.config.debugger.interface}"]
        if self.config.debugger.probe_id is not None:
            args.append(f"sn={self.config.debugger.probe_id}")
        return args

    def _failure_result(self, tool: str, started_at: str, finished_at: str, elapsed_ms: int, backend_error_type: str, log_path: str, operation_result: JsonObject | None = None) -> JsonObject:
        error_type = self._public_error_type(backend_error_type)
        result = {"ok": False, "tool": tool, "backend": self.backend_name, "started_at": started_at, "finished_at": finished_at, "elapsed_ms": elapsed_ms, "error_type": error_type, "backend_error_type": backend_error_type, "summary": self._summary_for_error(error_type), "likely_causes": self._likely_causes(error_type), "log_path": display_path(self.config, log_path)}
        if operation_result is not None:
            result["operation_result"] = operation_result
        return result

    def _backend_error_from_output(self, output: str, tool: str) -> str | None:
        backend_error_type = self._classify_output(output, tool)
        if backend_error_type != "unknown_debugger_error":
            return backend_error_type
        if contains_failure_text(output):
            return backend_error_type
        return None

    def _confirm_operation_success(self, output: str, tool: str) -> JsonObject:
        expected = STLINK_SUCCESS_CONFIRMATION.get(tool, [])
        if not expected:
            return {"confirmed": True, "matched": None, "expected": expected}
        lower = output.lower()
        matched = [marker for marker in expected if marker.lower() in lower]
        return {"confirmed": len(matched) == len(expected), "matched": matched, "expected": expected}

    def _unconfirmed_backend_error_type(self, tool: str) -> str:
        return {"probe_target": "probe_unconfirmed", "flash_firmware": "flash_unconfirmed", "reset_target": "reset_unconfirmed"}.get(tool, "unknown_debugger_error")

    def _write_action_report(self, result: JsonObject) -> JsonObject:
        return write_report(self.config, result)

    def _write_log(self, log_path: str, args: list[str], stdout: str, stderr: str, returncode: int | None, timed_out: bool) -> None:
        safe_write_text(self.config, log_path, json.dumps({"command": command_for_log(args), "returncode": returncode, "timed_out": timed_out, "stdout": stdout, "stderr": stderr}, indent=2) + "\n")

    def _permission_denied(self, tool: str, summary: str) -> JsonObject:
        return {"ok": False, "tool": tool, "error_type": "permission_denied", "summary": summary}

    def _unsupported_debug_tool(self, tool: str) -> JsonObject:
        return {"ok": False, "tool": tool, "backend": self.backend_name, "error_type": "not_supported", "summary": "Typed debug sessions require the OpenOCD backend."}

    def _classify_output(self, output: str, tool: str | None = None) -> str:
        lower = output.lower()
        if contains_any(lower, ["no st-link", "no stlink", "st-link not found", "stlink not found", "no debug probe"]):
            return "probe_not_found"
        if contains_any(lower, ["no stm32 target found", "cannot connect to target", "can not connect to target", "failed to connect"]):
            return "target_not_detected"
        if contains_any(lower, ["no device found", "device not found", "unable to connect"]):
            return "target_not_detected"
        if "verify" in lower and contains_any(lower, ["failed", "mismatch", "error"]):
            return "verify_failed"
        if "reset" in lower and contains_any(lower, ["failed", "error"]):
            return "reset_failed"
        if tool == "flash_firmware" and contains_any(lower, ["download failed", "write failed", "failed to download"]):
            return "flash_failed"
        if contains_any(lower, ["can't find", "couldn't find", "couldn't open", "not found"]):
            return "config_file_not_found"
        if tool == "flash_firmware" and contains_any(lower, ["failed", "error"]):
            return "flash_failed"
        return "unknown_debugger_error"

    def _public_error_type(self, backend_error_type: str) -> str:
        return BACKEND_ERROR_TO_PUBLIC_ERROR.get(backend_error_type, backend_error_type)

    def _summary_for_error(self, error_type: str) -> str:
        return {"debugger_not_found": "Debugger executable could not be found.", "adapter_not_found": "Debugger adapter could not be found or opened.", "target_not_detected": "Debugger could not detect the target.", "flash_failed": "Debugger failed to flash the firmware.", "verify_failed": "Debugger failed to verify the flashed firmware.", "reset_failed": "Debugger failed to reset the target.", "timeout": "Debugger command timed out.", "config_file_not_found": "Debugger input file could not be found.", "unknown_debugger_error": "Debugger failed with an unknown error."}.get(error_type, "Debugger failed with an unknown error.")

    def _likely_causes(self, error_type: str) -> list[str]:
        return {"target_not_detected": ["DUT is not powered", "wrong SWD/JTAG interface selection", "SWD/JTAG wiring issue", "debug probe already in use"], "adapter_not_found": ["debug probe is not connected", "debugger.probe_id does not match a connected ST-Link serial number", "debug probe driver is missing", "debug probe is already in use"], "verify_failed": ["flash write did not persist correctly", "firmware image does not match target memory layout"], "flash_failed": ["target flash is locked", "firmware image is invalid for this target", "debugger.flash_address is wrong"], "reset_failed": ["reset line wiring issue", "target is not responding"], "timeout": ["debugger stopped responding", "debug probe or target is stuck", "timeout_s is too low for this operation"], "debugger_not_found": ["debugger.executable is not configured", "STM32CubeProgrammer is not installed", "STM32_Programmer_CLI executable is not in PATH"], "config_file_not_found": ["firmware artifact path is missing", "STM32CubeProgrammer CLI path is incomplete"]}.get(error_type, ["inspect the debugger log for details"])


def version_line(output: str) -> str:
    for line in output.splitlines():
        if "STM32CubeProgrammer version:" in line:
            return f"STM32CubeProgrammer {line.split(':', 1)[1].strip()}"
    return next((line.strip() for line in output.splitlines() if line.strip()), "STM32CubeProgrammer version output was empty.")
