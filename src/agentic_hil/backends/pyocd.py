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
from agentic_hil.config import display_path, resolve_work_path
from agentic_hil.report import logs_directory, read_last_report, timestamp_for_filename, utc_now_iso, write_report
from agentic_hil.types import AgenticHILConfig, JsonObject

PYOCD_NOT_FOUND: JsonObject = {
    "ok": False,
    "backend": "pyocd",
    "error_type": "debugger_not_found",
    "backend_error_type": "pyocd_not_found",
    "summary": "pyOCD executable could not be found.",
    "likely_causes": [
        "debugger.executable is not configured",
        "pyOCD is not installed (install agentic-hil[pyocd] or pip install pyocd)",
        "pyocd is not in PATH",
    ],
}

BACKEND_ERROR_TO_PUBLIC_ERROR = {
    "pyocd_not_found": "debugger_not_found",
    "probe_not_found": "adapter_not_found",
}


class PyOCDBackend:
    backend_name = "pyocd"

    def __init__(self, config: AgenticHILConfig):
        self.config = config

    def reconfigure(self, config: AgenticHILConfig) -> None:
        self.config = config

    def info(self) -> JsonObject:
        resolved = self._resolve_executable()
        if not resolved["ok"]:
            return {"tool": "debugger_info", **resolved}
        command = [*invocation(str(resolved["executable_path"])), "--version"]
        completed = spawn_command(command, self.config.work_dir, min(self.config.debugger.timeout_s, 10))
        if completed.not_found:
            return {"tool": "debugger_info", **PYOCD_NOT_FOUND}
        if completed.timed_out:
            return {"ok": False, "tool": "debugger_info", "backend": self.backend_name, "executable": resolved["executable"], "error_type": "timeout", "summary": "Debugger version check timed out."}
        output = f"{completed.stdout}{completed.stderr}".strip()
        if completed.returncode != 0:
            backend_error_type = self._classify_output(output)
            error_type = self._public_error_type(backend_error_type)
            return {"ok": False, "tool": "debugger_info", "backend": self.backend_name, "executable": resolved["executable"], "error_type": error_type, "backend_error_type": backend_error_type, "summary": self._summary_for_error(error_type)}
        version = output.splitlines()[0] if output else "pyOCD version output was empty."
        return {"ok": True, "tool": "debugger_info", "backend": self.backend_name, "executable": resolved["executable"], "probe_id": self.config.debugger.probe_id, "target_type": self.config.debugger.target_type, "version": version, "summary": "pyOCD is available."}

    def list_probes(self) -> JsonObject:
        tool = "debugger_probes_list"
        if not self.config.permissions.allow_probe:
            return self._permission_denied(tool, "Debugger probe discovery is disabled by .agentic-hil/config.yaml.")
        resolved = self._resolve_executable()
        if not resolved["ok"]:
            return {"tool": tool, **resolved}
        command = [*invocation(str(resolved["executable_path"])), "json", "--probes", "--no-config"]
        completed = spawn_command(command, str(Path(str(resolved["executable_path"])).parent), self.config.debugger.timeout_s)
        if completed.not_found:
            return {"tool": tool, **PYOCD_NOT_FOUND}
        if completed.timed_out:
            return {"ok": False, "tool": tool, "backend": self.backend_name, "error_type": "timeout", "summary": "Debugger probe discovery timed out."}
        if completed.returncode != 0:
            return {"ok": False, "tool": tool, "backend": self.backend_name, "error_type": "probe_discovery_failed", "summary": "pyOCD probe discovery command failed."}
        parsed = parse_pyocd_probes(completed.stdout)
        if not parsed["ok"]:
            return {"tool": tool, "backend": self.backend_name, **parsed}
        probes = parsed["probes"]
        return {"ok": True, "tool": tool, "backend": self.backend_name, "probes": probes, "summary": f"{len(probes)} connected debugger probe(s) detected."}

    def probe_target(self) -> JsonObject:
        if not self.config.permissions.allow_probe:
            return self._permission_denied("probe_target", "Probing is disabled by .agentic-hil/config.yaml.")
        result = self._run_pyocd("probe_target", ["commander", "--command", "status", *self._connection_args()])
        if result.get("ok"):
            result["target_detected"] = True
            result["summary"] = "Target detected through pyOCD."
        return self._write_action_report(result)

    def flash_firmware(self, artifact: JsonObject, reset_after_flash: bool = False) -> JsonObject:
        if not self.config.permissions.allow_flash:
            return self._permission_denied("flash_firmware", "Flashing is disabled by .agentic-hil/config.yaml.")
        if self.config.permissions.allow_raw_debugger_commands:
            return self._permission_denied("flash_firmware", "Flashing is disabled while raw debugger commands are allowed.")
        if self.config.permissions.allow_mass_erase:
            return self._permission_denied("flash_firmware", "Flashing is disabled while mass erase is allowed.")

        artifact_path = str(artifact["resolved_path"])
        address_args: list[str] = []
        if Path(artifact_path).suffix.lower() == ".bin":
            if self.config.debugger.flash_address is None:
                return {"ok": False, "tool": "flash_firmware", "backend": self.backend_name, "error_type": "invalid_argument", "summary": "Flashing .bin artifacts with pyOCD requires debugger.flash_address.", "artifact": self._artifact_summary(artifact)}
            address_args = ["--base-address", self.config.debugger.flash_address]

        result = self._run_pyocd("flash_firmware", ["flash", *self._connection_args(), *address_args, artifact_path])
        result["artifact"] = self._artifact_summary(artifact)
        result["verify"] = True
        if not result.get("ok"):
            result["reset_after_flash"] = False
            return self._write_action_report(result)
        if not reset_after_flash:
            result["reset_after_flash"] = False
            result["summary"] = "Firmware flashed and verified. Target was not reset."
            return self._write_action_report(result)

        reset = self._run_pyocd("flash_firmware", ["commander", "--command", "reset", *self._connection_args()])
        if not reset.get("ok"):
            reset["artifact"] = self._artifact_summary(artifact)
            reset["verify"] = True
            reset["reset_after_flash"] = False
            reset["error_type"] = "reset_failed"
            reset["summary"] = "Firmware flashed, but the post-flash reset failed."
            return self._write_action_report(reset)
        result["reset_after_flash"] = reset_after_flash
        result["summary"] = "Firmware flashed, verified, and target reset."
        return self._write_action_report(result)

    def reset_target(self, mode: str = "run") -> JsonObject:
        allowed_modes = ["run", "halt", "init"]
        if mode not in allowed_modes:
            return {"ok": False, "tool": "reset_target", "error_type": "invalid_argument", "summary": "Invalid reset mode.", "allowed_values": allowed_modes}
        commander_command = "reset" if mode == "run" else "reset halt"
        result = self._run_pyocd("reset_target", ["commander", "--command", commander_command, *self._connection_args()])
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

    def close(self) -> None:
        return None

    def has_active_session(self) -> bool:
        return False

    def active_session_ids(self) -> list[str]:
        return []

    def _resolve_executable(self) -> JsonObject:
        configured = self.config.debugger.executable
        if configured:
            has_path_separator = "/" in configured or "\\" in configured
            if Path(configured).is_absolute() or has_path_separator:
                resolved = Path(resolve_work_path(self.config, configured))
                if not resolved.is_file():
                    return dict(PYOCD_NOT_FOUND)
                return {"ok": True, "executable": str(resolved), "executable_path": str(resolved)}
            found = which(configured)
            if found is None:
                return dict(PYOCD_NOT_FOUND)
            return {"ok": True, "executable": found, "executable_path": found}
        found = which("pyocd")
        if found is None:
            return dict(PYOCD_NOT_FOUND)
        return {"ok": True, "executable": found, "executable_path": found}

    def _run_pyocd(self, tool: str, action_args: list[str]) -> JsonObject:
        started_at = utc_now_iso()
        start = time.perf_counter()
        resolved = self._resolve_executable()
        if not resolved["ok"]:
            return {"tool": tool, "backend": self.backend_name, "started_at": started_at, **resolved, "finished_at": utc_now_iso(), "elapsed_ms": int((time.perf_counter() - start) * 1000)}
        args = [*invocation(str(resolved["executable_path"])), *action_args]
        log_path = str(Path(logs_directory(self.config)) / f"pyocd-{timestamp_for_filename()}-{tool}.log")
        completed = spawn_command(args, self.config.work_dir, self.config.debugger.timeout_s)
        finished_at = utc_now_iso()
        elapsed_ms = int((time.perf_counter() - start) * 1000)
        if completed.not_found:
            return {"tool": tool, "backend": self.backend_name, "started_at": started_at, **PYOCD_NOT_FOUND, "finished_at": finished_at, "elapsed_ms": elapsed_ms}
        self._write_log(log_path, args, completed.stdout, completed.stderr, completed.returncode, completed.timed_out)
        if completed.timed_out:
            return {"ok": False, "tool": tool, "backend": self.backend_name, "started_at": started_at, "finished_at": finished_at, "elapsed_ms": elapsed_ms, "error_type": "timeout", "summary": "Debugger command timed out.", "likely_causes": self._likely_causes("timeout"), "log_path": display_path(self.config, log_path)}
        output = f"{completed.stdout}{completed.stderr}"
        if completed.returncode == 0:
            backend_error_type = self._backend_error_from_output(output, tool)
            if backend_error_type is not None:
                return self._failure_result(tool, started_at, finished_at, elapsed_ms, backend_error_type, log_path)
            return {"ok": True, "tool": tool, "backend": self.backend_name, "started_at": started_at, "finished_at": finished_at, "elapsed_ms": elapsed_ms, "summary": "pyOCD command completed successfully.", "log_path": display_path(self.config, log_path)}
        return self._failure_result(tool, started_at, finished_at, elapsed_ms, self._classify_output(output, tool), log_path)

    def _connection_args(self) -> list[str]:
        args: list[str] = []
        if self.config.debugger.probe_id is not None:
            args.extend(["--uid", self.config.debugger.probe_id])
        if self.config.debugger.target_type is not None:
            args.extend(["--target", self.config.debugger.target_type])
        return args

    def _artifact_summary(self, artifact: JsonObject) -> JsonObject:
        return {"source": artifact.get("source", "path"), "path": artifact.get("path"), "sha256": artifact.get("sha256")}

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

    def _write_action_report(self, result: JsonObject) -> JsonObject:
        return write_report(self.config, result)

    def _write_log(self, log_path: str, args: list[str], stdout: str, stderr: str, returncode: int | None, timed_out: bool) -> None:
        Path(log_path).write_text(json.dumps({"command": command_for_log(args), "returncode": returncode, "timed_out": timed_out, "stdout": stdout, "stderr": stderr}, indent=2) + "\n", encoding="utf-8")

    def _permission_denied(self, tool: str, summary: str) -> JsonObject:
        return {"ok": False, "tool": tool, "error_type": "permission_denied", "summary": summary}

    def _unsupported_debug_tool(self, tool: str) -> JsonObject:
        return {"ok": False, "tool": tool, "backend": self.backend_name, "error_type": "not_supported", "summary": "Typed debug sessions require the OpenOCD backend."}

    def _classify_output(self, output: str, tool: str | None = None) -> str:
        lower = output.lower()
        if contains_any(lower, ["no available debug probes", "no debug probes are connected", "unable to open probe", "probe not found", "no probe with uid"]):
            return "probe_not_found"
        if contains_any(lower, ["unable to connect", "failed to connect", "target is not responding", "no ack received", "error connecting"]):
            return "target_not_detected"
        if contains_any(lower, ["unknown target type", "no target type", "target type is not"]):
            return "target_type_invalid"
        if "verify" in lower and contains_any(lower, ["failed", "mismatch", "error"]):
            return "verify_failed"
        if "reset" in lower and contains_any(lower, ["failed", "error"]):
            return "reset_failed"
        if tool == "flash_firmware" and contains_any(lower, ["failed", "error"]):
            return "flash_failed"
        return "unknown_debugger_error"

    def _public_error_type(self, backend_error_type: str) -> str:
        return BACKEND_ERROR_TO_PUBLIC_ERROR.get(backend_error_type, backend_error_type)

    def _summary_for_error(self, error_type: str) -> str:
        return {
            "debugger_not_found": "Debugger executable could not be found.",
            "adapter_not_found": "Debugger probe could not be found or opened.",
            "target_not_detected": "Debugger could not detect the target.",
            "target_type_invalid": "pyOCD does not know the configured debugger.target_type.",
            "flash_failed": "Debugger failed to flash the firmware.",
            "verify_failed": "Debugger failed to verify the flashed firmware.",
            "reset_failed": "Debugger failed to reset the target.",
            "timeout": "Debugger command timed out.",
            "unknown_debugger_error": "Debugger failed with an unknown error.",
        }.get(error_type, "Debugger failed with an unknown error.")

    def _likely_causes(self, error_type: str) -> list[str]:
        return {
            "target_not_detected": ["DUT is not powered", "SWD/JTAG wiring issue", "debug probe already in use", "wrong debugger.target_type for this device"],
            "adapter_not_found": ["debug probe is not connected", "debugger.probe_id does not match a connected probe", "probe driver or udev rule is missing", "debug probe is already in use"],
            "target_type_invalid": ["debugger.target_type is misspelled", "the target requires a CMSIS pack (pyocd pack install <type>)"],
            "verify_failed": ["flash write did not persist correctly", "firmware image does not match target memory layout"],
            "flash_failed": ["target flash is locked", "firmware image is invalid for this target", "debugger.flash_address is wrong"],
            "reset_failed": ["reset line wiring issue", "target is not responding"],
            "timeout": ["debugger stopped responding", "debug probe or target is stuck", "timeout_s is too low for this operation"],
            "debugger_not_found": ["debugger.executable is not configured", "pyOCD is not installed (install agentic-hil[pyocd] or pip install pyocd)", "pyocd is not in PATH"],
        }.get(error_type, ["inspect the debugger log for details"])


def parse_pyocd_probes(output: str) -> JsonObject:
    try:
        payload = json.loads(output)
    except (json.JSONDecodeError, TypeError):
        return {"ok": False, "error_type": "probe_discovery_failed", "summary": "pyOCD returned invalid probe-discovery JSON."}
    if not isinstance(payload, dict) or payload.get("status") != 0 or not isinstance(payload.get("boards"), list):
        return {"ok": False, "error_type": "probe_discovery_failed", "summary": "pyOCD reported a probe-discovery failure."}
    probe_ids: list[str] = []
    for board in payload["boards"]:
        if not isinstance(board, dict) or not isinstance(board.get("unique_id"), str) or not board["unique_id"]:
            return {"ok": False, "error_type": "probe_discovery_failed", "summary": "pyOCD returned an invalid probe record."}
        if board["unique_id"] not in probe_ids:
            probe_ids.append(board["unique_id"])
    return {"ok": True, "probes": [{"probe_id": probe_id} for probe_id in probe_ids]}
