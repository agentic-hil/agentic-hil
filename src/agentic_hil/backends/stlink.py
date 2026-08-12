from __future__ import annotations

import json
import re
import shutil
import tempfile
import time
from pathlib import Path
from typing import Literal

from agentic_hil.artifacts import looks_like_intel_hex, sha256_file
from agentic_hil.backends.common import (
    NOT_CONTACTED,
    READ_ONLY_TOOLS,
    command_for_log,
    contains_any,
    contains_failure_text,
    find_stm32_programmer_cli,
    invocation,
    reset_init_unsupported,
    spawn_command,
    which,
)
from agentic_hil.backends.gdbdebug import DEBUG_SYMBOL_PATTERN, resolve_gdb_executable
from agentic_hil.config import (
    ConfigError,
    display_path,
    resolve_work_path,
    safe_configured_directory,
    safe_write_text,
)
from agentic_hil.knowledge import exclusive_permission_summary, remediation_fields
from agentic_hil.report import (
    classify_failure_report,
    logs_directory,
    mark_audit_failure,
    mark_side_effect,
    timestamp_for_filename,
    utc_now_iso,
    write_report,
)
from agentic_hil.types import AgenticHILConfig, JsonObject

STLINK_NOT_FOUND: JsonObject = {
    "ok": False,
    "backend": "stlink",
    "error_type": "debugger_not_found",
    "backend_error_type": "stm32_programmer_cli_not_found",
    "summary": "STM32CubeProgrammer CLI executable could not be found.",
    "likely_causes": ["debuggers.<name>.executable is not configured", "STM32CubeProgrammer is not installed", "STM32_Programmer_CLI executable is not in PATH"],
    # No executable means no process, so this call is not an unconfirmed outcome
    # on the bench: nothing was ever started that could have touched it.
    **NOT_CONTACTED,
}

BACKEND_ERROR_TO_PUBLIC_ERROR = {
    "stm32_programmer_cli_not_found": "debugger_not_found",
    "probe_not_found": "adapter_not_found",
    # An exit of 0 whose output is missing at least one of the lines that
    # confirm the operation. Its own public error_type, for the reason the OpenOCD table
    # gives: `target_not_detected` is the CLI's positive report that a probe was
    # opened and nothing answered behind it, and the shipped catalogue entry for
    # it says so, so publishing it here would claim an abort point this branch
    # has no evidence for.
    "probe_unconfirmed": "target_state_unconfirmed",
    "flash_unconfirmed": "flash_failed",
    "reset_unconfirmed": "reset_failed",
    "memory_read_unconfirmed": "memory_read_failed",
}

# One entry per tool, and every entry is a line the operation itself prints.
# The connect banner is not in any of them: STM32CubeProgrammer prints the
# ST-Link serial and the device name whenever it opens a probe, so a reset that
# connected and then did nothing would confirm itself out of the banner alone.
# `reset_target` therefore waits for "MCU Reset", and a memory read waits for
# "Data read successfully" — measured against a NUCLEO-F446RE on
# STM32CubeProgrammer v2.23.0, which prints it after the UPLOADING block and
# before the elapsed-time line.
STLINK_SUCCESS_CONFIRMATION = {
    "probe_target": ["ST-LINK SN", "Device name"],
    "flash_firmware": ["Download verified successfully"],
    "debug_dump_symbol_ihex": ["Data read successfully"],
}
# One row per reset mode, holding the argument that goes on the wire and the
# lines STM32CubeProgrammer prints when that argument did what it says. The two
# belong to the same command and are kept together for that reason: `-rst` and
# `-halt` are different commands with different success lines, and a single
# marker set for `reset_target` carried the `-rst` lines only — mode `halt` did
# exactly what it was asked, printed neither of them, and could therefore never
# report anything but an unconfirmed outcome (#142).
#
# `-halt` measured on STM32CubeProgrammer v2.23.0 against a NUCLEO-F446RE: the
# NORMAL connect banner carries `Reset mode  : Software reset` and the run ends
# with `Core halted`. The banner settles what this mode does — a software reset
# and then a halt, the same operation OpenOCD's and pyOCD's `reset halt`
# perform — but it is not a second marker: connect prints it before the halt is
# attempted, so a run that connected and then failed to halt prints it
# unchanged. Only `Core halted` is the operation's own success line, and it is
# the only one of the two a failure withholds.
STLINK_RESET_MODES: dict[str, dict[str, list[str]]] = {
    "run": {"args": ["-rst"], "success_text": ["MCU Reset", "reset is performed"]},
    "halt": {"args": ["-halt"], "success_text": ["Core halted"]},
}
# What the offline symbol query prints, and what is read back out of it. Named
# markers rather than GDB's own `$1 = ...` value history: a command that failed
# prints an error and no marker at all, so a missing answer can never be
# mistaken for a parsed one, and the reply survives GDB versions that number or
# decorate the echo differently.
GDB_SYMBOL_ADDRESS_MARKER = "AGENTIC_HIL_SYMBOL_ADDRESS="
GDB_SYMBOL_SIZE_MARKER = "AGENTIC_HIL_SYMBOL_SIZE="
GDB_SYMBOL_QUERY_TIMEOUT_S = 30.0
GDB_SYMBOL_MISSING_MARKERS = ["no symbol", "not defined"]

STLINK_SERIAL_PATTERN = re.compile(r"^\s*ST-?LINK\s+SN\s*:\s*(\S+)\s*$", re.IGNORECASE | re.MULTILINE)
STLINK_DEVICE_PATTERN = re.compile(r"^\s*Device\s+name\s*:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)
STLINK_EMPTY_MARKERS = ["no st-link detected", "no stlink detected", "0 st-link detected", "0 stlink detected"]


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

    def list_probes(self) -> JsonObject:
        tool = "debugger_probes_list"
        if not self.config.probe_allowed():
            return self._permission_denied(tool, "Debugger probe discovery is disabled by the authoritative config.")
        resolved = self._resolve_executable()
        if not resolved["ok"]:
            return {"tool": tool, **resolved}
        # `-l st-link-only` enumerates connected probes and never connects to a
        # target, so every failure of it — including the timeout, whose child
        # spawn_command reaped before returning — leaves the bench untouched
        # and carries the markers that say so.
        command = [*invocation(str(resolved["executable_path"])), "-q", "-l", "st-link-only"]
        completed = spawn_command(command, str(Path(str(resolved["executable_path"])).parent), self.config.debugger.timeout_s)
        if completed.not_found:
            return {"tool": tool, **STLINK_NOT_FOUND}
        if completed.timed_out:
            return {"ok": False, "tool": tool, "backend": self.backend_name, "error_type": "timeout", "summary": "Debugger probe discovery timed out.", **NOT_CONTACTED}
        output = f"{completed.stdout}{completed.stderr}"
        probe_ids = stlink_probe_ids(output)
        if completed.returncode == 0 and (probe_ids or stlink_empty_result(output)):
            probes = [{"probe_id": probe_id} for probe_id in probe_ids]
            return {
                "ok": True,
                "tool": tool,
                "backend": self.backend_name,
                "probes": probes,
                "summary": f"{len(probes)} connected debugger probe(s) detected.",
            }
        return {
            "ok": False,
            "tool": tool,
            "backend": self.backend_name,
            "error_type": "probe_discovery_failed",
            "summary": "STM32CubeProgrammer did not return a recognized non-intrusive probe listing.",
            **NOT_CONTACTED,
        }

    def probe_target(self) -> JsonObject:
        if not self.config.probe_allowed():
            return self._permission_denied("probe_target", "Probing is disabled by the authoritative config.")
        result = self._run_stlink("probe_target", self._connection_args("HOTPLUG"))
        if result.get("ok"):
            result["target_detected"] = True
            result["summary"] = "Target detected through ST-Link."
        return self._write_action_report(result)

    def flash_firmware(self, artifact: JsonObject, reset_after_flash: bool = False) -> JsonObject:
        if not self.config.debugger.permissions.allow_flash:
            return self._permission_denied("flash_firmware", "Flashing is disabled by the authoritative config.")
        if self.config.debugger.permissions.allow_raw_debugger_commands:
            return self._permission_denied("flash_firmware", exclusive_permission_summary("Flashing", "allow_raw_debugger_commands", self.config.debugger_id))
        if self.config.debugger.permissions.allow_mass_erase:
            return self._permission_denied("flash_firmware", exclusive_permission_summary("Flashing", "allow_mass_erase", self.config.debugger_id))

        artifact_path = str(artifact["resolved_path"])
        write_args = ["-w", artifact_path]
        if Path(artifact_path).suffix.lower() == ".bin":
            if self.config.debugger.flash_address is None:
                return {"ok": False, "tool": "flash_firmware", "backend": self.backend_name, "error_type": "invalid_argument", "summary": "Flashing .bin artifacts with ST-Link requires debuggers.<name>.flash_address.", "artifact": {"source": artifact.get("source", "path"), "path": artifact.get("path"), "sha256": artifact.get("sha256")}}
            write_args.append(self.config.debugger.flash_address)
        reset_args = ["-rst"] if reset_after_flash else []
        result = self._run_stlink("flash_firmware", [*self._connection_args("HOTPLUG"), *write_args, "-v", *reset_args])
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
        if mode == "init":
            return reset_init_unsupported(self.backend_name, "STM32CubeProgrammer's CLI has no equivalent")
        selected = STLINK_RESET_MODES[mode]
        result = self._run_stlink("reset_target", [*self._connection_args("NORMAL"), *selected["args"]], selected["success_text"])
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

    def debug_dump_symbol_ihex(self, symbol: str = "", output: JsonObject | None = None, symbol_elf: JsonObject | None = None) -> JsonObject:
        """Read one allowed symbol out of target memory and write Intel HEX.

        The only typed-debug tool this backend serves, because it is the only
        one STM32CubeProgrammer can do without a GDB session: `-r <address>
        <size> <file.hex>` uploads device memory and picks Intel HEX off the
        file extension. Everything a caller can observe is the OpenOCD path's —
        the same arguments, the same allowlist and `debug.max_dump_size_bytes`
        refusals word for word, the same success fields — so a plan that dumps a
        symbol does not have to know which probe it is running on. What differs
        is where the symbol table comes from, and that difference is stated
        rather than hidden: with no session there is no loaded image to ask, so
        the answer is resolved offline from the ELF this service flashed, whose
        sha256 travels in the result.
        """
        tool = "debug_dump_symbol_ihex"
        validated = self._validate_symbol(tool, symbol)
        if not validated["ok"]:
            return validated
        resolved = self._resolve_symbol_offline(tool, symbol, symbol_elf)
        if not resolved["ok"]:
            return resolved
        size_bytes = int(resolved["size_bytes"])
        if size_bytes > self.config.debug.max_dump_size_bytes:
            return {"ok": False, "tool": tool, "backend": self.backend_name, "error_type": "permission_denied", "summary": "Symbol dump exceeds debug.max_dump_size_bytes.", "symbol": symbol, "size_bytes": size_bytes, "max_dump_size_bytes": self.config.debug.max_dump_size_bytes}
        if output is None:
            # The service validates the output path before it gets here, so this
            # is unreachable through a tool call. It is a refusal rather than an
            # assert because an assert is stripped under `python -O`, and what
            # would follow it is a probe opened for a read with nowhere to put
            # what it read.
            return {"ok": False, "tool": tool, "backend": self.backend_name, "error_type": "invalid_argument", "summary": "output_path must be a non-empty string.", **NOT_CONTACTED}
        output_path = Path(str(output["resolved_path"]))
        try:
            # The CLI writes the file itself and will not create a missing
            # parent, so the directory is made here — through the same guarded
            # helper the OpenOCD path uses, so a dump cannot mkdir its way out
            # of the workspace.
            safe_configured_directory(self.config, str(output_path.parent), f"{tool}.output_path")
        except (ConfigError, OSError) as error:
            return {"ok": False, "tool": tool, "backend": self.backend_name, "error_type": "output_write_failed", "summary": "Intel HEX output directory could not be prepared.", "backend_error": str(error), "symbol": symbol, **NOT_CONTACTED}
        address_value = int(resolved["address_value"])
        result = self._run_stlink(tool, [*self._connection_args("NORMAL"), "-r", hex(address_value), str(size_bytes), str(output_path)])
        result.update({"symbol": symbol, "address": resolved["address"], "size_bytes": size_bytes, "output": output, "symbol_source": resolved["symbol_source"]})
        if not result.get("ok") and "side_effect_status" not in result:
            # The reset family's answer to the same question. A read drives no
            # write, but it is still a probe attached to a live core: an
            # STM32CubeProgrammer that exited without printing "Data read
            # successfully" leaves no evidence of where it stopped, and a
            # backend that called that harmless would be inventing the one
            # claim it does not have. Branches that *did* prove their abort
            # point — "no ST-LINK detected" — arrive carrying not_started and
            # keep it.
            result.update({"side_effect_status": "unknown", "retry_safe": False})
        if result.get("ok"):
            # The CLI confirmed the read, so the bytes left the target; whether
            # what landed on disk is the Intel HEX a caller was promised is a
            # separate question, and it is asked with the same parser artifact
            # validation uses on an uploaded .hex.
            if not output_path.is_file() or not looks_like_intel_hex(output_path):
                result.update({"ok": False, "error_type": "output_write_failed", "summary": "STM32CubeProgrammer confirmed the read but did not leave a parseable Intel HEX file.", "target_contacted": True})
                result.pop("success_confirmed", None)
                return self._write_action_report(result)
            result["summary"] = "Symbol memory dumped as Intel HEX."
        return self._write_action_report(result)

    def close(self) -> None:
        return None

    def target_support(self) -> JsonObject:
        """STM32CubeProgrammer identifies the part itself.

        There is no target_type field for this backend and no catalogue to
        install, so nothing here can be missing. Answered rather than omitted,
        so the field means the same thing on every backend.
        """
        return {
            "ok": True,
            "tool": "debugger_target_support",
            "backend": self.backend_name,
            "status": "not_applicable",
            "summary": "STM32CubeProgrammer identifies the connected part itself; debuggers.<name>.target_type is not read by this backend.",
        }

    def classify_last_error(self) -> JsonObject:
        return classify_failure_report(self.config, self._likely_causes)

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

    def _run_stlink(self, tool: str, action_args: list[str], success_text: list[str] | None = None) -> JsonObject:
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
        audit_error = self._write_log(log_path, args, completed.stdout, completed.stderr, completed.returncode, completed.timed_out)
        if completed.timed_out:
            return self._finish_log_audit({"ok": False, "tool": tool, "backend": self.backend_name, "started_at": started_at, "finished_at": finished_at, "elapsed_ms": elapsed_ms, "error_type": "timeout", "summary": "Debugger command timed out.", "likely_causes": self._likely_causes("timeout"), "log_path": display_path(self.config, log_path)}, audit_error)
        output = f"{completed.stdout}{completed.stderr}"
        if completed.returncode == 0:
            backend_error_type = self._backend_error_from_output(output, tool)
            if backend_error_type is not None:
                return self._finish_log_audit(self._failure_result(tool, started_at, finished_at, elapsed_ms, backend_error_type, log_path), audit_error)
            confirmation = self._confirm_operation_success(output, STLINK_SUCCESS_CONFIRMATION.get(tool, []) if success_text is None else success_text)
            if not confirmation["confirmed"]:
                # Confirmation is all-or-nothing here, so this branch is also
                # reached with some of the expected lines present — a read that
                # printed the ST-Link serial and no device name. The lines that
                # did match travel with the ones that were looked for, so the
                # result says how far the CLI got rather than leaving a caller
                # to read the worst case out of the error_type.
                return self._finish_log_audit(self._failure_result(tool, started_at, finished_at, elapsed_ms, self._unconfirmed_backend_error_type(tool), log_path, {"confirmed": False, "expected_success_text": confirmation["expected"], "matched_success_text": confirmation["matched"]}), audit_error)
            return self._finish_log_audit({"ok": True, "tool": tool, "backend": self.backend_name, "started_at": started_at, "finished_at": finished_at, "elapsed_ms": elapsed_ms, "success_confirmed": True, "operation_result": {"confirmed": True, "matched_success_text": confirmation["matched"]}, "summary": "STM32CubeProgrammer CLI command completed successfully.", "log_path": display_path(self.config, log_path)}, audit_error)
        return self._finish_log_audit(self._failure_result(tool, started_at, finished_at, elapsed_ms, self._classify_output(output, tool), log_path), audit_error)

    def _connection_args(self, mode: Literal["HOTPLUG", "NORMAL"]) -> list[str]:
        args = ["-c", f"port={self.config.debugger.interface}", f"mode={mode}"]
        if self.config.debugger.probe_id is not None:
            args.append(f"sn={self.config.debugger.probe_id}")
        return args

    # STM32CubeProgrammer's own report that the transport never existed, for any
    # tool, plus — for the reads, whose command drives nothing of its own — its
    # report that a probe was opened and nothing answered behind it. Both are
    # read off the CLI's output, so each is a positive statement rather than the
    # absence of one; `probe_unconfirmed` is not, and stays out.
    PRE_CONTACT_BACKEND_ERRORS = frozenset({"probe_not_found"})
    READ_ONLY_PRE_CONTACT_BACKEND_ERRORS = frozenset({"target_not_detected"})

    def _proves_no_contact(self, tool: str, backend_error_type: str) -> bool:
        if backend_error_type in self.PRE_CONTACT_BACKEND_ERRORS:
            return True
        return tool in READ_ONLY_TOOLS and backend_error_type in self.READ_ONLY_PRE_CONTACT_BACKEND_ERRORS

    def _failure_result(self, tool: str, started_at: str, finished_at: str, elapsed_ms: int, backend_error_type: str, log_path: str, operation_result: JsonObject | None = None) -> JsonObject:
        # likely_causes says what may be wrong; remediation says what to check
        # next, scoped to this backend, because the checks differ per tool: an
        # ST-Link transport is chosen with `interface`, an OpenOCD one with
        # interface_cfg. Same catalogue the MCP reference serves.
        error_type = self._public_error_type(backend_error_type)
        result = {"ok": False, "tool": tool, "backend": self.backend_name, "started_at": started_at, "finished_at": finished_at, "elapsed_ms": elapsed_ms, "error_type": error_type, "backend_error_type": backend_error_type, "summary": self._summary_for_error(error_type), "likely_causes": self._likely_causes(error_type), **remediation_fields(error_type, self.backend_name), "log_path": display_path(self.config, log_path)}
        if operation_result is not None:
            result["operation_result"] = operation_result
        if self._proves_no_contact(tool, backend_error_type):
            # The ST-Link probe is the only transport STM32CubeProgrammer has
            # to the target, and "no ST-LINK detected" is its report that the
            # transport never existed for this run; "No STM32 target found" and
            # its siblings are the same report one step further out, with the
            # probe named and nothing behind it. A failed call over a channel
            # that never carried anything must refuse, not quarantine. Both are
            # the CLI's own words, read out of its output by
            # `_classify_output`; `probe_unconfirmed` — an exit status of 0
            # whose output is missing at least one line that confirms the
            # connection — deliberately is not, because it names no abort point
            # at all, whether it printed some of them or none.
            result.update(NOT_CONTACTED)
        return result

    def _backend_error_from_output(self, output: str, tool: str) -> str | None:
        backend_error_type = self._classify_output(output, tool)
        if backend_error_type != "unknown_debugger_error":
            return backend_error_type
        if contains_failure_text(output):
            return backend_error_type
        return None

    def _confirm_operation_success(self, output: str, expected: list[str]) -> JsonObject:
        # All-or-nothing over whichever set the caller selected: a command is
        # confirmed by every line it prints on success, and by nothing less.
        if not expected:
            return {"confirmed": True, "matched": None, "expected": expected}
        lower = output.lower()
        matched = [marker for marker in expected if marker.lower() in lower]
        return {"confirmed": len(matched) == len(expected), "matched": matched, "expected": expected}

    def _unconfirmed_backend_error_type(self, tool: str) -> str:
        return {"probe_target": "probe_unconfirmed", "flash_firmware": "flash_unconfirmed", "reset_target": "reset_unconfirmed", "debug_dump_symbol_ihex": "memory_read_unconfirmed"}.get(tool, "unknown_debugger_error")

    def _write_action_report(self, result: JsonObject) -> JsonObject:
        return write_report(self.config, mark_side_effect(result))

    def _write_log(self, log_path: str, args: list[str], stdout: str, stderr: str, returncode: int | None, timed_out: bool) -> Exception | None:
        try:
            safe_write_text(self.config, log_path, json.dumps({"command": command_for_log(args), "returncode": returncode, "timed_out": timed_out, "stdout": stdout, "stderr": stderr}, indent=2) + "\n")
        except (ConfigError, OSError) as error:
            return error
        return None

    def _finish_log_audit(self, result: JsonObject, error: Exception | None) -> JsonObject:
        return mark_audit_failure(result, error) if error is not None else result

    def _permission_denied(self, tool: str, summary: str) -> JsonObject:
        return {"ok": False, "tool": tool, "error_type": "permission_denied", "summary": summary}

    def _unsupported_debug_tool(self, tool: str) -> JsonObject:
        return {"ok": False, "tool": tool, "backend": self.backend_name, "error_type": "not_supported", "summary": "Typed debug sessions require the OpenOCD backend."}

    def _validate_symbol(self, tool: str, symbol: str) -> JsonObject:
        """The OpenOCD path's two refusals, word for word.

        Copied deliberately rather than approximated: `debug.allowed_symbols` is
        the operator's statement about what may leave this bench at all, and a
        caller that reads the refusal must not be able to tell which backend
        wrote it, or the allowlist would look like a property of the debugger
        instead of a property of the project.
        """
        if not isinstance(symbol, str) or DEBUG_SYMBOL_PATTERN.match(symbol) is None:
            return {"ok": False, "tool": tool, "backend": self.backend_name, "error_type": "invalid_argument", "summary": "symbol must be a valid C/C++ identifier."}
        if not self.config.debug.allow_all_symbols and symbol not in self.config.debug.allowed_symbols:
            return {"ok": False, "tool": tool, "backend": self.backend_name, "error_type": "permission_denied", "summary": "Symbol is not allowed by debug.allowed_symbols.", "symbol": symbol}
        return {"ok": True}

    def _resolve_symbol_offline(self, tool: str, symbol: str, symbol_elf: JsonObject | None) -> JsonObject:
        """Address and size for one symbol, from an ELF and nothing else.

        A debug session resolves a symbol by asking the GDB that already has the
        image loaded and the target attached. There is no session here, so the
        same GDB is asked in batch mode against the ELF alone: no `target`
        command is issued, nothing is attached, and the probe is not opened
        until the read itself. That keeps the query harmless on a running board
        and means a bad symbol name costs a refusal rather than a hardware
        contact.

        The ELF is the one this service flashed, so the symbol table provably
        describes what is on the target — the guarantee a session gets from
        having loaded the image. Nothing else is accepted: resolving against
        some other build would answer with an address that is right about the
        file and wrong about the board.
        """
        if symbol_elf is None:
            return {
                "ok": False,
                "tool": tool,
                "backend": self.backend_name,
                "error_type": "symbol_source_not_available",
                "summary": "No ELF with debug symbols has been flashed through this service, so no symbol table describes what is on the target. Flash the ELF with flash_firmware first.",
                "symbol": symbol,
                "likely_causes": ["no flash_firmware call has succeeded in this session", "the firmware that was flashed was a .hex or .bin, which carries no symbols", "the firmware on the target was flashed outside Agentic HIL"],
                **NOT_CONTACTED,
            }
        # The remembered path is the caller's own workspace file, which a
        # rebuild is free to overwrite at any moment — including in the window
        # between hashing it and handing the same path to a separately spawned
        # GDB. A replacement landing there makes GDB answer from bytes the digest
        # never covered while the result still carries the flashed image's digest,
        # so an address that is right about the new build and wrong about the
        # board would travel out wearing the old image's provenance. So the bytes
        # are copied into a private file first; the digest that gates the query
        # and the bytes GDB reads are then the same immutable file, and a later
        # replacement of the caller's path cannot reach it. A copy whose digest no
        # longer matches the flashed image is refused, exactly as a changed source
        # on the original path was.
        elf_path = str(symbol_elf["resolved_path"])
        recorded_digest = symbol_elf.get("integrity_sha256") or symbol_elf.get("sha256")
        staging_dir: Path | None = None
        try:
            try:
                staging_dir = Path(tempfile.mkdtemp(prefix="agentic-hil-symbols-"))
                staged_elf = staging_dir / Path(elf_path).name
                shutil.copyfile(elf_path, staged_elf)
                staged_digest = sha256_file(staged_elf)
            except OSError as error:
                return {"ok": False, "tool": tool, "backend": self.backend_name, "error_type": "symbol_source_changed", "summary": "The ELF flashed through this service can no longer be read, so no symbol table is proven to describe what is on the target. Flash it again with flash_firmware.", "symbol": symbol, "backend_error": str(error), **NOT_CONTACTED}
            if not isinstance(recorded_digest, str) or staged_digest != recorded_digest:
                return {"ok": False, "tool": tool, "backend": self.backend_name, "error_type": "symbol_source_changed", "summary": "The ELF on disk no longer matches the image flashed through this service, so its symbol table is not proven to describe what is on the target. Flash the current build with flash_firmware first.", "symbol": symbol, "likely_causes": ["the ELF was rebuilt or replaced after it was flashed", "a different file now occupies the flashed artifact's path"], **NOT_CONTACTED}
            gdb = resolve_gdb_executable(self.config, self.backend_name)
            if not gdb["ok"]:
                return {**gdb, "tool": tool, "symbol": symbol}
            args = gdb_symbol_query_args(str(gdb["executable"]), str(staged_elf), symbol)
            completed = spawn_command(args, str(staging_dir), min(self.config.debugger.timeout_s, GDB_SYMBOL_QUERY_TIMEOUT_S))
            if completed.not_found:
                return {"ok": False, "tool": tool, "backend": self.backend_name, "error_type": "gdb_not_found", "summary": "Configured debug.gdb_executable could not be found.", "likely_causes": ["debug.gdb_executable points to a missing file", "GDB is not installed"], "symbol": symbol, **NOT_CONTACTED}
            if completed.timed_out:
                return {"ok": False, "tool": tool, "backend": self.backend_name, "error_type": "timeout", "summary": "Symbol resolution timed out.", "symbol": symbol, **NOT_CONTACTED}
            output = f"{completed.stdout}{completed.stderr}"
            address_value = gdb_symbol_marker_value(output, GDB_SYMBOL_ADDRESS_MARKER)
            size_value = gdb_symbol_marker_value(output, GDB_SYMBOL_SIZE_MARKER)
            if address_value is None or size_value is None:
                lower = output.lower()
                error_type = "symbol_not_found" if contains_any(lower, GDB_SYMBOL_MISSING_MARKERS) else "symbol_resolution_failed"
                summary = "Symbol was not found in the flashed ELF." if error_type == "symbol_not_found" else "GDB returned no usable symbol address or size for the flashed ELF."
                return {"ok": False, "tool": tool, "backend": self.backend_name, "error_type": error_type, "summary": summary, "symbol": symbol, **NOT_CONTACTED}
            return {
                "ok": True,
                "symbol": symbol,
                "address": hex(address_value),
                "address_value": address_value,
                "size_bytes": size_value,
                "symbol_source": {"path": symbol_elf.get("path"), "sha256": symbol_elf.get("sha256")},
            }
        finally:
            if staging_dir is not None:
                shutil.rmtree(staging_dir, ignore_errors=True)

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
        return {"debugger_not_found": "Debugger executable could not be found.", "adapter_not_found": "Debugger adapter could not be found or opened.", "target_not_detected": "Debugger could not detect the target.", "target_state_unconfirmed": "STM32CubeProgrammer exited without confirming the operation, so the target's state is unknown.", "flash_failed": "Debugger failed to flash the firmware.", "verify_failed": "Debugger failed to verify the flashed firmware.", "reset_failed": "Debugger failed to reset the target.", "memory_read_failed": "Debugger failed to read the requested target memory.", "timeout": "Debugger command timed out.", "config_file_not_found": "Debugger input file could not be found.", "unknown_debugger_error": "Debugger failed with an unknown error."}.get(error_type, "Debugger failed with an unknown error.")

    def _likely_causes(self, error_type: str) -> list[str]:
        return {"target_not_detected": ["DUT is not powered", "wrong SWD/JTAG interface selection", "SWD/JTAG wiring issue", "debug probe already in use"], "target_state_unconfirmed": ["STM32CubeProgrammer exited successfully without printing every line that confirms the operation; operation_result names which of them did print","debuggers.<name>.executable is a wrapper that discards the CLI's output", "this STM32CubeProgrammer version words its confirmation differently"], "adapter_not_found": ["debug probe is not connected", "debuggers.<name>.probe_id does not match a connected ST-Link serial number", "debug probe driver is missing", "debug probe is already in use"], "verify_failed": ["flash write did not persist correctly", "firmware image does not match target memory layout"], "flash_failed": ["target flash is locked", "firmware image is invalid for this target", "debuggers.<name>.flash_address is wrong"], "reset_failed": ["reset line wiring issue", "target is not responding"], "memory_read_failed": ["STM32CubeProgrammer exited without printing 'Data read successfully', so the read is unconfirmed", "the symbol's address is not readable memory on this target", "debug probe or target stopped responding mid-read"], "timeout": ["debugger stopped responding", "debug probe or target is stuck", "timeout_s is too low for this operation"], "debugger_not_found": ["debuggers.<name>.executable is not configured", "STM32CubeProgrammer is not installed", "STM32_Programmer_CLI executable is not in PATH"], "config_file_not_found": ["firmware artifact path is missing", "STM32CubeProgrammer CLI path is incomplete"]}.get(error_type, ["inspect the debugger log for details"])


def gdb_symbol_query_args(gdb_executable: str, elf_path: str, symbol: str) -> list[str]:
    """A GDB that reads one symbol out of an ELF and attaches to nothing.

    `--batch` runs the two commands and exits; `-nx` keeps a developer's
    `.gdbinit` — which can define, alias or hook anything — out of a bench
    answer; `-q` drops the banner. No `target`, `attach` or `monitor` command
    appears, and none can: this is the whole command line, so the query cannot
    reach a probe even if one is connected.

    `printf` rather than `print` because it emits exactly the marker line and
    nothing on failure. The casts to `unsigned long` are what make the two
    values plain decimal integers regardless of the symbol's own type.
    """
    return [
        *invocation(gdb_executable),
        "--batch",
        "-nx",
        "-q",
        "-ex",
        f'printf "{GDB_SYMBOL_ADDRESS_MARKER}%lu\\n", (unsigned long)&{symbol}',
        "-ex",
        f'printf "{GDB_SYMBOL_SIZE_MARKER}%lu\\n", (unsigned long)sizeof({symbol})',
        elf_path,
    ]


def gdb_symbol_marker_value(output: str, marker: str) -> int | None:
    for line in output.splitlines():
        stripped = line.strip()
        if stripped.startswith(marker):
            digits = stripped[len(marker) :].strip()
            if digits.isdigit():
                return int(digits)
    return None


def version_line(output: str) -> str:
    for line in output.splitlines():
        if "STM32CubeProgrammer version:" in line:
            return f"STM32CubeProgrammer {line.split(':', 1)[1].strip()}"
    return next((line.strip() for line in output.splitlines() if line.strip()), "STM32CubeProgrammer version output was empty.")


def stlink_probe_ids(output: str) -> list[str]:
    return list(dict.fromkeys(STLINK_SERIAL_PATTERN.findall(output)))


def stlink_target_info(output: str) -> JsonObject | None:
    serials = stlink_probe_ids(output)
    devices = list(dict.fromkeys(value.strip() for value in STLINK_DEVICE_PATTERN.findall(output) if value.strip()))
    if len(serials) != 1 or len(devices) != 1:
        return None
    return {"probe_id": serials[0], "controller": devices[0]}


def stlink_empty_result(output: str) -> bool:
    lower = output.lower()
    return any(marker in lower for marker in STLINK_EMPTY_MARKERS)
