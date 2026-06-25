# Copyright 2026 Hannes Pauli
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from ..config import AIHILConfig, display_path, resolve_work_path
from ..debugger import DebuggerBackend
from ..report import logs_directory, read_last_report, timestamp_for_filename, utc_now_iso, write_report


OPENOCD_NOT_FOUND = {
    "ok": False,
    "backend": "openocd",
    "error_type": "debugger_not_found",
    "backend_error_type": "openocd_not_found",
    "summary": "Debugger executable could not be found.",
    "likely_causes": [
        "debugger.executable is not configured",
        "debugger executable is not installed",
        "debugger executable is not in PATH",
    ],
}

BACKEND_ERROR_TO_PUBLIC_ERROR = {
    "openocd_not_found": "debugger_not_found",
    "interface_config_not_found": "debugger_config_not_found",
    "target_config_not_found": "debugger_config_not_found",
    "config_file_not_found": "debugger_config_not_found",
}

OPENOCD_DISABLE_TCP_SERVER_COMMANDS = [
    "gdb_port disabled",
    "tcl_port disabled",
    "telnet_port disabled",
]

OPENOCD_SUCCESS_MARKERS = {
    "aihil_probe_target": "AIHIL_RESULT:probe_target:ok",
    "aihil_flash_firmware": "AIHIL_RESULT:flash_firmware:ok",
    "aihil_reset_target": "AIHIL_RESULT:reset_target:ok",
}


class OpenOCDBackend(DebuggerBackend):
    backend_name = "openocd"

    def __init__(self, config: AIHILConfig) -> None:
        self.config = config

    def resolve_executable(self) -> dict[str, Any]:
        return self._resolve_executable()

    def info(self) -> dict[str, Any]:
        resolved = self._resolve_executable()
        if not resolved["ok"]:
            return {"tool": "aihil_debugger_info", **resolved}
        command = self._invocation(resolved["executable_path"]) + ["--version"]
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=min(self.config.debugger.timeout_s, 10),
                cwd=self.config.work_dir,
                check=False,
                **_hidden_subprocess_kwargs(),
            )
        except (FileNotFoundError, OSError):
            return {"tool": "aihil_debugger_info", **OPENOCD_NOT_FOUND}
        except subprocess.TimeoutExpired:
            return {
                "ok": False,
                "tool": "aihil_debugger_info",
                "backend": self.backend_name,
                "executable": resolved["executable"],
                "error_type": "timeout",
                "summary": "Debugger version check timed out.",
            }

        output = (completed.stdout + completed.stderr).strip()
        if completed.returncode != 0:
            backend_error_type = self._classify_output(output)
            error_type = self._public_error_type(backend_error_type)
            return {
                "ok": False,
                "tool": "aihil_debugger_info",
                "backend": self.backend_name,
                "executable": resolved["executable"],
                "error_type": error_type,
                "backend_error_type": backend_error_type,
                "summary": self._summary_for_error(error_type, backend_error_type),
            }
        version = output.splitlines()[0] if output else "OpenOCD version output was empty."
        return {
            "ok": True,
            "tool": "aihil_debugger_info",
            "backend": self.backend_name,
            "executable": resolved["executable"],
            "version": version,
            "summary": "OpenOCD is available.",
        }

    def probe_target(self) -> dict[str, Any]:
        if not self.config.permissions.allow_probe:
            return self._permission_denied("aihil_probe_target", "Probing is disabled by .aihil/config.yaml.")
        marker = OPENOCD_SUCCESS_MARKERS["aihil_probe_target"]
        result = self._run_openocd(
            "aihil_probe_target",
            f'init; targets; echo "{marker}"; shutdown',
            success_marker=marker,
        )
        if result["ok"]:
            result["target_detected"] = True
            result["summary"] = "Target detected through OpenOCD."
        return self._write_action_report(result)

    def flash_firmware(self, artifact: dict[str, Any]) -> dict[str, Any]:
        if not self.config.permissions.allow_flash:
            return self._permission_denied("aihil_flash_firmware", "Flashing is disabled by .aihil/config.yaml.")
        if self.config.permissions.allow_raw_debugger_commands:
            return self._permission_denied(
                "aihil_flash_firmware",
                "Flashing is disabled while raw debugger commands are allowed.",
            )
        if self.config.permissions.allow_mass_erase:
            return self._permission_denied(
                "aihil_flash_firmware",
                "Flashing is disabled while mass erase is allowed.",
            )

        path = str(artifact["resolved_path"])
        command_path = path.replace("\\", "/").replace('"', '\\"')
        marker = OPENOCD_SUCCESS_MARKERS["aihil_flash_firmware"]
        result = self._run_openocd(
            "aihil_flash_firmware",
            f'program "{command_path}" verify reset; echo "{marker}"; shutdown',
            success_marker=marker,
        )
        result["artifact"] = {
            "source": artifact.get("source", "path"),
            "path": artifact.get("path"),
            "sha256": artifact.get("sha256"),
        }
        result["verify"] = True
        result["reset_after_flash"] = True
        if result["ok"]:
            result["summary"] = "Firmware flashed, verified, and target reset."
        return self._write_action_report(result)

    def reset_target(self, mode: str = "run") -> dict[str, Any]:
        allowed_modes = ["run", "halt", "init"]
        if mode not in allowed_modes:
            return {
                "ok": False,
                "tool": "aihil_reset_target",
                "error_type": "invalid_argument",
                "summary": "Invalid reset mode.",
                "allowed_values": allowed_modes,
            }
        if not self.config.permissions.allow_reset:
            return self._permission_denied("aihil_reset_target", "Reset is disabled by .aihil/config.yaml.")
        marker = OPENOCD_SUCCESS_MARKERS["aihil_reset_target"]
        result = self._run_openocd(
            "aihil_reset_target",
            f'init; reset {mode}; echo "{marker}"; shutdown',
            success_marker=marker,
        )
        result["mode"] = mode
        if result["ok"]:
            result["summary"] = f"Target reset with mode '{mode}'."
        return self._write_action_report(result)

    def classify_last_error(self) -> dict[str, Any]:
        report = read_last_report(self.config)
        if not report.get("ok") and report.get("error_type") == "report_not_found":
            return {
                "ok": False,
                "tool": "aihil_classify_last_error",
                "error_type": "report_not_found",
                "summary": "No AI-HIL report has been written yet.",
            }
        if report.get("ok"):
            return {
                "ok": True,
                "tool": "aihil_classify_last_error",
                "error_type": None,
                "summary": "Last AI-HIL report did not contain an error.",
            }
        error_type = str(report.get("error_type", "unknown_debugger_error"))
        result = {
            "ok": True,
            "tool": "aihil_classify_last_error",
            "error_type": error_type,
            "summary": report.get("summary", "Last AI-HIL report contained an error."),
            "likely_causes": report.get("likely_causes", self._likely_causes(error_type)),
            "report_path": report.get("report_path"),
            "log_path": report.get("log_path"),
        }
        if report.get("backend_error_type"):
            result["backend_error_type"] = report["backend_error_type"]
        return result

    def _resolve_executable(self) -> dict[str, Any]:
        configured = self.config.debugger.executable
        if configured:
            candidate = Path(configured)
            has_path_separator = any(separator in configured for separator in ("/", "\\"))
            if candidate.is_absolute() or has_path_separator:
                resolved = resolve_work_path(self.config, candidate)
                if not resolved.is_file():
                    return dict(OPENOCD_NOT_FOUND)
                return {"ok": True, "executable": str(resolved), "executable_path": str(resolved)}
            found = shutil.which(configured)
            if not found:
                return dict(OPENOCD_NOT_FOUND)
            return {"ok": True, "executable": found, "executable_path": found}

        found = shutil.which("openocd")
        if not found:
            return dict(OPENOCD_NOT_FOUND)
        return {"ok": True, "executable": found, "executable_path": found}

    def _run_openocd(self, tool: str, openocd_command: str, success_marker: str | None = None) -> dict[str, Any]:
        started_at = utc_now_iso()
        start = time.perf_counter()
        resolved = self._resolve_executable()
        if not resolved["ok"]:
            result = {"tool": tool, "backend": self.backend_name, "started_at": started_at, **resolved}
            result["finished_at"] = utc_now_iso()
            result["elapsed_ms"] = int((time.perf_counter() - start) * 1000)
            return result

        args = (
            self._invocation(resolved["executable_path"])
            + [
                "-f",
                self.config.debugger.interface_cfg,
                "-f",
                self.config.debugger.target_cfg,
            ]
            + [arg for command in OPENOCD_DISABLE_TCP_SERVER_COMMANDS for arg in ("-c", command)]
            + [
                "-c",
                openocd_command,
            ]
        )
        log_path = logs_directory(self.config) / f"openocd-{timestamp_for_filename()}-{tool}.log"
        stdout = ""
        stderr = ""
        timed_out = False
        returncode: int | None = None
        try:
            completed = subprocess.run(
                args,
                capture_output=True,
                text=True,
                timeout=self.config.debugger.timeout_s,
                cwd=self.config.work_dir,
                check=False,
                **_hidden_subprocess_kwargs(),
            )
            stdout = completed.stdout
            stderr = completed.stderr
            returncode = completed.returncode
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            stdout = _decode_timeout_output(exc.stdout)
            stderr = _decode_timeout_output(exc.stderr)
        except (FileNotFoundError, OSError):
            result = {"tool": tool, "backend": self.backend_name, "started_at": started_at, **OPENOCD_NOT_FOUND}
            result["finished_at"] = utc_now_iso()
            result["elapsed_ms"] = int((time.perf_counter() - start) * 1000)
            return result

        finished_at = utc_now_iso()
        elapsed_ms = int((time.perf_counter() - start) * 1000)
        self._write_log(log_path, args, stdout, stderr, returncode, timed_out)
        if timed_out:
            return {
                "ok": False,
                "tool": tool,
                "backend": self.backend_name,
                "started_at": started_at,
                "finished_at": finished_at,
                "elapsed_ms": elapsed_ms,
                "error_type": "timeout",
                "summary": "Debugger command timed out.",
                "likely_causes": self._likely_causes("timeout"),
                "log_path": display_path(self.config, log_path),
            }

        output = stdout + stderr
        if returncode == 0:
            backend_error_type = self._backend_error_from_output(output, tool)
            if backend_error_type is not None:
                return self._openocd_failure_result(tool, started_at, finished_at, elapsed_ms, backend_error_type, log_path)
            if success_marker is not None and success_marker not in output:
                backend_error_type = self._unconfirmed_backend_error_type(tool)
                return self._openocd_failure_result(tool, started_at, finished_at, elapsed_ms, backend_error_type, log_path)
            result = {
                "ok": True,
                "tool": tool,
                "backend": self.backend_name,
                "started_at": started_at,
                "finished_at": finished_at,
                "elapsed_ms": elapsed_ms,
                "summary": "OpenOCD command completed successfully.",
                "log_path": display_path(self.config, log_path),
            }
            if success_marker is not None:
                result["success_confirmed"] = True
            return result

        backend_error_type = self._classify_output(output, tool=tool)
        return self._openocd_failure_result(tool, started_at, finished_at, elapsed_ms, backend_error_type, log_path)

    def _openocd_failure_result(
        self,
        tool: str,
        started_at: str,
        finished_at: str,
        elapsed_ms: int,
        backend_error_type: str,
        log_path: Path,
    ) -> dict[str, Any]:
        error_type = self._public_error_type(backend_error_type)
        return {
            "ok": False,
            "tool": tool,
            "backend": self.backend_name,
            "started_at": started_at,
            "finished_at": finished_at,
            "elapsed_ms": elapsed_ms,
            "error_type": error_type,
            "backend_error_type": backend_error_type,
            "summary": self._summary_for_error(error_type, backend_error_type),
            "likely_causes": self._likely_causes(error_type),
            "log_path": display_path(self.config, log_path),
        }

    def _backend_error_from_output(self, output: str, tool: str) -> str | None:
        backend_error_type = self._classify_output(output, tool=tool)
        if backend_error_type != "unknown_debugger_error":
            return backend_error_type
        if _contains_failure_text(output):
            return backend_error_type
        return None

    def _unconfirmed_backend_error_type(self, tool: str) -> str:
        return {
            "aihil_probe_target": "target_not_detected",
            "aihil_flash_firmware": "flash_failed",
            "aihil_reset_target": "reset_failed",
        }.get(tool, "unknown_debugger_error")

    def _write_action_report(self, result: dict[str, Any]) -> dict[str, Any]:
        return write_report(self.config, result)

    def _write_log(
        self,
        path: Path,
        args: list[str],
        stdout: str,
        stderr: str,
        returncode: int | None,
        timed_out: bool,
    ) -> None:
        content = {
            "command": _command_for_log(args),
            "returncode": returncode,
            "timed_out": timed_out,
            "stdout": stdout,
            "stderr": stderr,
        }
        path.write_text(json.dumps(content, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    def _permission_denied(self, tool: str, summary: str) -> dict[str, Any]:
        return {
            "ok": False,
            "tool": tool,
            "error_type": "permission_denied",
            "summary": summary,
        }

    def _invocation(self, executable_path: str) -> list[str]:
        if executable_path.endswith(".py"):
            return [sys.executable, executable_path]
        return [executable_path]

    def _classify_output(self, output: str, tool: str | None = None) -> str:
        lower = output.lower()
        interface = self.config.debugger.interface_cfg.lower()
        target = self.config.debugger.target_cfg.lower()
        if interface in lower and any(text in lower for text in ("not found", "can't find", "couldn't find", "couldn't open")):
            return "interface_config_not_found"
        if target in lower and any(text in lower for text in ("not found", "can't find", "couldn't find", "couldn't open")):
            return "target_config_not_found"
        if any(
            text in lower
            for text in (
                "adapter not found",
                "no adapter",
                "no device found",
                "unable to open",
                "open failed",
                "libusb_open",
            )
        ):
            return "adapter_not_found"
        if any(text in lower for text in ("target not examined", "target not detected", "unable to connect", "failed to read")):
            return "target_not_detected"
        if "verify" in lower and any(text in lower for text in ("failed", "mismatch", "error")):
            return "verify_failed"
        if "reset" in lower and any(text in lower for text in ("failed", "error")):
            return "reset_failed"
        if any(text in lower for text in ("can't find", "couldn't find", "couldn't open", "not found")):
            return "config_file_not_found"
        if tool == "aihil_flash_firmware" and any(text in lower for text in ("failed", "error")):
            return "flash_failed"
        return "unknown_debugger_error"

    def _public_error_type(self, backend_error_type: str) -> str:
        return BACKEND_ERROR_TO_PUBLIC_ERROR.get(backend_error_type, backend_error_type)

    def _summary_for_error(self, error_type: str, backend_error_type: str | None = None) -> str:
        summaries = {
            "debugger_not_found": "Debugger executable could not be found.",
            "debugger_config_not_found": "Debugger configuration file could not be found.",
            "adapter_not_found": "Debugger adapter could not be found or opened.",
            "target_not_detected": "Debugger could not detect the target.",
            "flash_failed": "Debugger failed to flash the firmware.",
            "verify_failed": "Debugger failed to verify the flashed firmware.",
            "reset_failed": "Debugger failed to reset the target.",
            "timeout": "Debugger command timed out.",
            "unknown_debugger_error": "Debugger failed with an unknown error.",
        }
        summary = summaries.get(error_type, "Debugger failed with an unknown error.")
        if backend_error_type in {"interface_config_not_found", "target_config_not_found"}:
            return f"{summary}"
        return summary

    def _likely_causes(self, error_type: str) -> list[str]:
        causes = {
            "target_not_detected": [
                "DUT is not powered",
                "wrong interface configuration",
                "SWD/JTAG wiring issue",
                "debug probe already in use",
            ],
            "adapter_not_found": [
                "debug probe is not connected",
                "debug probe driver is missing",
                "debug probe is already in use",
                "Windows USB driver is not bound to the ST-Link adapter",
            ],
            "verify_failed": [
                "flash write did not persist correctly",
                "wrong target configuration",
                "firmware image does not match target memory layout",
            ],
            "flash_failed": [
                "target flash is locked",
                "wrong target configuration",
                "firmware image is invalid for this target",
            ],
            "reset_failed": [
                "reset line wiring issue",
                "target is not responding",
                "wrong reset configuration",
            ],
            "timeout": [
                "debugger stopped responding",
                "debug probe or target is stuck",
                "timeout_s is too low for this operation",
            ],
            "debugger_not_found": [
                "debugger.executable is not configured",
                "debugger executable is not installed",
                "debugger executable is not in PATH",
            ],
            "debugger_config_not_found": [
                "debugger interface configuration is missing",
                "debugger target configuration is missing",
                "debugger search path is incomplete",
            ],
        }
        return causes.get(error_type, ["inspect the debugger log for details"])


def _decode_timeout_output(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return value


def _contains_failure_text(output: str) -> bool:
    lower = output.lower()
    return any(text in lower for text in ("error:", "failed", "failure", "mismatch"))


def _command_for_log(args: list[str]) -> str:
    if os.name == "nt":
        return subprocess.list2cmdline(args)
    return " ".join(shlex.quote(arg) for arg in args)


def _hidden_subprocess_kwargs() -> dict[str, int]:
    if os.name != "nt":
        return {}
    return {"creationflags": subprocess.CREATE_NO_WINDOW}
