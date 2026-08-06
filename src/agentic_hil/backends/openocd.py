from __future__ import annotations

import json
import re
import time
from pathlib import Path

from agentic_hil.backends.common import (
    NOT_CONTACTED,
    READ_ONLY_TOOLS,
    command_for_log,
    contains_any,
    contains_failure_text,
    invocation,
    spawn_command,
    which,
)
from agentic_hil.backends.gdbdebug import GdbDebugSessions
from agentic_hil.config import ConfigError, display_path, resolve_work_path, safe_write_text
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

OPENOCD_NOT_FOUND: JsonObject = {
    "ok": False,
    "backend": "openocd",
    "error_type": "debugger_not_found",
    "backend_error_type": "openocd_not_found",
    "summary": "Debugger executable could not be found.",
    "likely_causes": ["debuggers.<name>.executable is not configured", "debugger executable is not installed", "debugger executable is not in PATH"],
    # No executable means no process, so this call is not an unconfirmed outcome
    # on the bench: nothing was ever started that could have touched it.
    **NOT_CONTACTED,
}

BACKEND_ERROR_TO_PUBLIC_ERROR = {
    "openocd_not_found": "debugger_not_found",
    "interface_config_not_found": "debugger_config_not_found",
    "target_config_not_found": "debugger_config_not_found",
    "config_file_not_found": "debugger_config_not_found",
    "command_rejected_before_init": "debugger_command_rejected",
    # An exit of 0 with the tool's success marker missing from the output. This
    # branch read nothing out of OpenOCD's words, so it gets a public error_type
    # of its own rather than the classification the words would have produced:
    # `target_not_detected` is OpenOCD's report that it reached the adapter and
    # nothing answered — which the shipped catalogue entry says in as many words
    # — and publishing that here would make the abort-point claim this branch
    # exists to withhold. See READ_ONLY_PRE_CONTACT_BACKEND_ERRORS.
    "probe_unconfirmed": "target_state_unconfirmed",
    "flash_unconfirmed": "flash_failed",
    "reset_unconfirmed": "reset_failed",
}

OPENOCD_DISABLE_TCP_SERVER_COMMANDS = ["gdb_port disabled", "tcl_port disabled", "telnet_port disabled"]
OPENOCD_SUCCESS_MARKERS = {
    "probe_target": "AGENTIC_HIL_RESULT:probe_target:ok",
    "flash_firmware": "AGENTIC_HIL_RESULT:flash_firmware:ok",
    "reset_target": "AGENTIC_HIL_RESULT:reset_target:ok",
}
# OpenOCD has no `reset`, `targets` or `halt` until `init` has run: those live in
# target_exec_command_handlers, which target_init registers while `init` executes,
# so before that the Tcl interpreter answers `invalid command name "reset"` and
# nothing reaches the adapter. `init` is safe to name explicitly - it neither
# resets nor halts anything (openocd.c handle_init_command examines targets and
# starts the servers), and it carries its own guard against running twice. The
# echo behind it is this backend's evidence that the run stage was reached; a
# failure without it never got that far.
OPENOCD_INIT_STAGE_MARKER = "AGENTIC_HIL_STAGE:init:ok"
OPENOCD_INIT_PREFIX = f'init; echo "{OPENOCD_INIT_STAGE_MARKER}"; '
# Jim rejects a command OpenOCD has not registered yet; OpenOCD itself rejects one
# that exists but belongs to the other stage. Both verdicts are reached inside the
# interpreter, before handle_init_command opens the probe.
OPENOCD_UNREGISTERED_COMMAND = re.compile(r'invalid command name "([^"]+)"', re.IGNORECASE)
OPENOCD_WRONG_STAGE_COMMAND = re.compile(r"the '([^']+)' command must be used (?:after|before) 'init'", re.IGNORECASE)


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
        # `allow_raw_debugger_commands` is read the way `_debug_permission_failure`
        # reads it: a debug session is refused *while* raw commands are allowed,
        # so a config that turns that grant on is one an open session may not
        # outlive. An unbound config has no session to authorize at all — the
        # service replaces this object rather than reconfiguring it in that case,
        # and reading a permission off `None` here would raise before it could.
        debug_permission_revoked = not config.probe_allowed() or config.debugger is None or config.debugger.permissions.allow_raw_debugger_commands
        if debugger_changed or debug_permission_revoked:
            self._debug.close()
        self.config = config
        self._debug.config = config

    def info(self) -> JsonObject:
        resolved = self._resolve_executable()
        if not resolved["ok"]:
            return {"tool": "debugger_info", **resolved}
        command = [*invocation(str(resolved["executable_path"])), "--version"]
        completed = spawn_command(command, str(Path(str(resolved["executable_path"])).parent), min(self.config.debugger.timeout_s, 10))
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

    def list_probes(self) -> JsonObject:
        if not self.config.probe_allowed():
            return self._permission_denied("debugger_probes_list", "Debugger probe discovery is disabled by the authoritative config.")
        return {
            "ok": False,
            "tool": "debugger_probes_list",
            "backend": self.backend_name,
            "error_type": "not_supported",
            "summary": "OpenOCD cannot enumerate all connected probe IDs through a backend-independent command.",
        }

    def probe_target(self) -> JsonObject:
        if not self.config.probe_allowed():
            return self._permission_denied("probe_target", "Probing is disabled by the authoritative config.")
        marker = OPENOCD_SUCCESS_MARKERS["probe_target"]
        result = self._run_openocd("probe_target", f'{OPENOCD_INIT_PREFIX}targets; echo "{marker}"; shutdown', marker)
        if result.get("ok"):
            result["target_detected"] = True
            result["summary"] = "Target detected through OpenOCD."
        return self._write_action_report(result)

    def flash_firmware(self, artifact: JsonObject, reset_after_flash: bool = False) -> JsonObject:
        if not self.config.debugger.permissions.allow_flash:
            return self._permission_denied("flash_firmware", "Flashing is disabled by the authoritative config.")
        if self.config.debugger.permissions.allow_raw_debugger_commands:
            return self._permission_denied("flash_firmware", exclusive_permission_summary("Flashing", "allow_raw_debugger_commands", self.config.debugger_id))
        if self.config.debugger.permissions.allow_mass_erase:
            return self._permission_denied("flash_firmware", exclusive_permission_summary("Flashing", "allow_mass_erase", self.config.debugger_id))

        command_path = escape_tcl_double_quoted_word(openocd_path_for_command(str(artifact["resolved_path"])))
        marker = OPENOCD_SUCCESS_MARKERS["flash_firmware"]
        reset_command = " reset" if reset_after_flash else ""
        # `program` runs `init` itself, so the explicit prefix changes nothing
        # about the flash — `init` guards against running twice. What it adds is
        # the stage echo: a failure whose output lacks the init marker provably
        # stopped before adapter_init opened the probe, which is what lets a
        # missing config script or an absent adapter refuse instead of
        # quarantining the bench (see _failure_result).
        result = self._run_openocd("flash_firmware", f'{OPENOCD_INIT_PREFIX}program "{command_path}" verify{reset_command}; echo "{marker}"; shutdown', marker)
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
        result = self._run_openocd("reset_target", openocd_reset_command(mode, marker), marker)
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

    def target_support(self) -> JsonObject:
        """OpenOCD has no target type to check.

        It selects the target with `target_cfg`, a script config load already
        resolved against an existing file, so there is no separate catalogue
        that could be missing. Answered rather than omitted, so the field means
        the same thing on every backend.
        """
        return {
            "ok": True,
            "tool": "debugger_target_support",
            "backend": self.backend_name,
            "status": "not_applicable",
            "target_cfg": self.config.debugger.target_cfg if self.config.debugger else None,
            "summary": "OpenOCD selects the target through debuggers.<name>.target_cfg, a bundled script, not through a target_type a CMSIS pack has to provide.",
        }

    def classify_last_error(self) -> JsonObject:
        return classify_failure_report(self.config, self._likely_causes)

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
        completed = spawn_command(args, str(Path(str(resolved["executable_path"])).parent), self.config.debugger.timeout_s)
        finished_at = utc_now_iso()
        elapsed_ms = int((time.perf_counter() - start) * 1000)
        if completed.not_found:
            return {"tool": tool, "backend": self.backend_name, "started_at": started_at, **OPENOCD_NOT_FOUND, "finished_at": finished_at, "elapsed_ms": elapsed_ms}

        audit_error = self._write_log(log_path, args, completed.stdout, completed.stderr, completed.returncode, completed.timed_out)
        if completed.timed_out:
            return self._finish_log_audit({"ok": False, "tool": tool, "backend": self.backend_name, "started_at": started_at, "finished_at": finished_at, "elapsed_ms": elapsed_ms, "error_type": "timeout", "summary": "Debugger command timed out.", "likely_causes": self._likely_causes("timeout"), "log_path": display_path(self.config, log_path)}, audit_error)

        output = f"{completed.stdout}{completed.stderr}"
        rejected = rejected_openocd_commands(openocd_command, output)
        init_reached = OPENOCD_INIT_STAGE_MARKER in output
        if completed.returncode == 0:
            backend_error_type = self._backend_error_from_output(output, tool)
            if backend_error_type is not None:
                return self._finish_log_audit(self._failure_result(tool, started_at, finished_at, elapsed_ms, backend_error_type, log_path, rejected, init_reached=init_reached), audit_error)
            if success_marker is not None and success_marker not in output:
                return self._finish_log_audit(self._failure_result(tool, started_at, finished_at, elapsed_ms, self._unconfirmed_backend_error_type(tool), log_path, rejected, init_reached=init_reached), audit_error)
            result: JsonObject = {"ok": True, "tool": tool, "backend": self.backend_name, "started_at": started_at, "finished_at": finished_at, "elapsed_ms": elapsed_ms, "summary": "OpenOCD command completed successfully.", "log_path": display_path(self.config, log_path)}
            if success_marker is not None:
                result["success_confirmed"] = True
            return self._finish_log_audit(result, audit_error)
        return self._finish_log_audit(self._failure_result(tool, started_at, finished_at, elapsed_ms, self._classify_output(output, tool), log_path, rejected, init_reached=init_reached), audit_error)

    # Failures whose classification already names the phase before the adapter
    # opens. Config scripts load at the configuration stage, and the adapter is
    # first opened by adapter_init inside `init`; an adapter that could not be
    # opened is the other face of the same boundary. Each of these may be read
    # as "never reached the bench" ONLY together with the absent init-stage
    # marker (see _failure_result) — the classification alone is string
    # matching, and the marker is what makes it a proof. Nothing here covers the
    # timeout, which is the deadline killing the process before it could say
    # where it stopped.
    PRE_CONTACT_BACKEND_ERRORS = frozenset(
        {"interface_config_not_found", "target_config_not_found", "config_file_not_found", "adapter_not_found"}
    )
    # One classification further out, and only for the two tools whose command
    # string drives nothing: OpenOCD's own report that nothing answered on the
    # selected transport, which is what the error catalogue's
    # `target_not_detected:openocd` entry means by it. A target that never
    # answered was never brought under debug control, and with the init-stage
    # marker absent `init` is the only thing that could have addressed it at all
    # — `targets` lists and `shutdown` ends. `flash_firmware` and `reset_target`
    # are deliberately not here: their command strings drive the target
    # themselves, so the same words also fit a target that stopped answering
    # while it was being driven, and the safe reading of an ambiguity is the one
    # that keeps the bench contained.
    #
    # Only a `target_not_detected` the classifier read out of OpenOCD's own
    # output qualifies. `probe_unconfirmed` — an exit of 0 with the success
    # marker missing — stays out, because it is the absence of a report rather
    # than a report that nothing answered: OpenOCD may have completed `init`,
    # halted the core and then lost both markers, and that board's run state is
    # unknown. It carries `target_state_unconfirmed` to the caller for the same
    # reason, so the public error_type and its catalogue entry withhold the
    # abort-point claim this set is about.
    READ_ONLY_PRE_CONTACT_BACKEND_ERRORS = frozenset({"target_not_detected"})

    def _proves_no_contact(self, tool: str, backend_error_type: str) -> bool:
        if backend_error_type in self.PRE_CONTACT_BACKEND_ERRORS:
            return True
        return tool in READ_ONLY_TOOLS and backend_error_type in self.READ_ONLY_PRE_CONTACT_BACKEND_ERRORS

    def _failure_result(self, tool: str, started_at: str, finished_at: str, elapsed_ms: int, backend_error_type: str, log_path: str, rejected_commands: list[str] | None = None, *, init_reached: bool = True) -> JsonObject:
        # likely_causes says what may be wrong; remediation says what to check
        # next, scoped to this backend, because the checks differ per tool: an
        # OpenOCD target is selected by target_cfg, a pyOCD one by target_type.
        # Same catalogue the MCP reference serves, so the two cannot diverge.
        if rejected_commands:
            backend_error_type = "command_rejected_before_init"
        error_type = self._public_error_type(backend_error_type)
        result = {"ok": False, "tool": tool, "backend": self.backend_name, "started_at": started_at, "finished_at": finished_at, "elapsed_ms": elapsed_ms, "error_type": error_type, "backend_error_type": backend_error_type, "summary": self._summary_for_error(error_type), "likely_causes": self._likely_causes(error_type), **remediation_fields(error_type, self.backend_name), "log_path": display_path(self.config, log_path)}
        if rejected_commands:
            # OpenOCD stopped inside its own interpreter, before it opened the
            # probe: this call never reached the bench. That is a failed call,
            # not an unconfirmed target, so it must not take the bench out of
            # service - the board is exactly as the last call that did reach it
            # left it.
            result.update({"rejected_commands": rejected_commands, **NOT_CONTACTED})
        elif self._proves_no_contact(tool, backend_error_type) and not init_reached:
            # Same test as the rejected-commands branch, met by other evidence:
            # OpenOCD named a failure that left the target untouched (a config
            # script it could not load, an adapter it could not open, a target
            # that did not answer), and the init-stage marker never printed, so
            # `init` never completed — adapter_init never had a probe to drive,
            # or target examine never brought a core under debug control. The
            # board is exactly as the last call that did reach it left it, and a
            # quarantine here would demand a physical inspection of hardware
            # this run provably never touched.
            result.update(NOT_CONTACTED)
        return result

    def _backend_error_from_output(self, output: str, tool: str) -> str | None:
        backend_error_type = self._classify_output(output, tool)
        if backend_error_type != "unknown_debugger_error":
            return backend_error_type
        if contains_failure_text(output):
            return backend_error_type
        return None

    def _unconfirmed_backend_error_type(self, tool: str) -> str:
        return {"probe_target": "probe_unconfirmed", "flash_firmware": "flash_unconfirmed", "reset_target": "reset_unconfirmed"}.get(tool, "unknown_debugger_error")

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
            "target_state_unconfirmed": "OpenOCD exited without reporting the outcome, so the target's state is unknown.",
            "flash_failed": "Debugger failed to flash the firmware.",
            "verify_failed": "Debugger failed to verify the flashed firmware.",
            "reset_failed": "Debugger failed to reset the target.",
            "timeout": "Debugger command timed out.",
            "debugger_command_rejected": "OpenOCD refused the command before it opened the debug probe, so the target was not touched.",
            "unknown_debugger_error": "Debugger failed with an unknown error.",
        }.get(error_type, "Debugger failed with an unknown error.")

    def _likely_causes(self, error_type: str) -> list[str]:
        return {
            "target_not_detected": ["DUT is not powered", "wrong interface configuration", "SWD/JTAG wiring issue", "debug probe already in use"],
            "target_state_unconfirmed": ["OpenOCD exited successfully without printing either of the markers this backend echoes", "debuggers.<name>.executable is a wrapper that discards OpenOCD's output", "OpenOCD's output was redirected away from the process it was started as"],
            "adapter_not_found": ["debug probe is not connected", "debug probe driver is missing", "debug probe is already in use", "Windows USB driver is not bound to the ST-Link adapter"],
            "verify_failed": ["flash write did not persist correctly", "wrong target configuration", "firmware image does not match target memory layout"],
            "flash_failed": ["target flash is locked", "wrong target configuration", "firmware image is invalid for this target"],
            "reset_failed": ["reset line wiring issue", "target is not responding", "wrong reset configuration"],
            "timeout": ["debugger stopped responding", "debug probe or target is stuck", "timeout_s is too low for this operation"],
            "debugger_not_found": ["debuggers.<name>.executable is not configured", "debugger executable is not installed", "debugger executable is not in PATH"],
            "debugger_config_not_found": ["debugger interface configuration is missing", "debugger target configuration is missing", "debugger search path is incomplete"],
            "debugger_command_rejected": ["this OpenOCD build does not know the command that was sent", "a configuration script used a run-stage command before 'init'", "the installed OpenOCD is older or newer than the one the command was written for"],
        }.get(error_type, ["inspect the debugger log for details"])


def openocd_reset_command(mode: str, marker: str) -> str:
    """The one command line `reset_target` sends, for each of the three modes.

    `run`, `halt` and `init` are OpenOCD's own reset modes: let the target run,
    halt it immediately, or halt it and then run the target's reset-init event
    script. `init; reset init` is therefore not the same word twice - the first
    is the server leaving its configuration stage, the second is the reset mode -
    and it is exactly the pair OpenOCD's own `program` proc issues before it
    writes flash (src/flash/startup.tcl), which is why flashing works today
    without a prefix and a bare `reset` does not."""
    return f'{OPENOCD_INIT_PREFIX}reset {mode}; echo "{marker}"; shutdown'


def rejected_openocd_commands(openocd_command: str, output: str) -> list[str]:
    """The commands OpenOCD refused to evaluate, when it refused before `init`.

    An empty list means: assume nothing. Two conditions have to hold together
    before a failure may be read as one that never reached the target.

    OpenOCD has to name a command, and the name has to be one this backend put on
    the command line. `reset` does not exist in the interpreter until `init`
    registers it and can never stop existing afterwards, so being told that
    `reset` is not a command is proof that `init` had not completed - and a run
    where `init` did not complete is a run where adapter_init never opened the
    probe. A name we did not send is somebody else's script failing, and says
    nothing about ours.

    And our post-init marker has to be absent, so an evaluation error raised by a
    configuration script after the adapter was already open cannot be read as an
    untouched target.

    Everything else stays unconfirmed and keeps quarantining: an adapter that
    would not open, a target that would not answer, a reset that was issued and
    not confirmed, a timeout. None of those can prove where they stopped."""
    if OPENOCD_INIT_STAGE_MARKER in output:
        return []
    sent = {segment.strip().split(" ", 1)[0].strip() for segment in openocd_command.split(";")}
    named = {match.group(1) for pattern in (OPENOCD_UNREGISTERED_COMMAND, OPENOCD_WRONG_STAGE_COMMAND) for match in pattern.finditer(output)}
    return sorted(named & sent)


def openocd_path_for_command(value: str) -> str:
    return value.replace("\\", "/") if Path(value).anchor.startswith("\\") else value


def escape_tcl_double_quoted_word(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$").replace("[", "\\[").replace("]", "\\]")
