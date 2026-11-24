"""Serial-pinned Raspberry Pi USB bootloader operations."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import NamedTuple
from uuid import uuid4

from agentic_hil.backends.common import (
    CONTACT_UNPROVEN,
    NOT_CONTACTED,
    CompletedCommand,
    invocation,
    reset_init_unsupported,
    spawn_command,
)
from agentic_hil.bench import MAX_WAIT_S, BenchMutex, DeviceBusyError
from agentic_hil.config import (
    AgenticHILConfig,
    bind_debugger,
    display_path,
    host_debugger_executable,
)
from agentic_hil.knowledge import (
    exclusive_permission_fields,
    exclusive_permission_summary,
    permission_denied_fields,
    permission_denied_summary,
    permission_key,
    remediation_fields,
)
from agentic_hil.redact import redact_stream_text
from agentic_hil.report import (
    classify_failure_report,
    logs_directory,
    mark_audit_failure,
    write_audit_log,
)
from agentic_hil.types import JsonObject

PICOTOOL_LISTING_NOTICE = "Only configured picotool boards are listed. Use com_ports_list to find the USB serial of a board running an application with USB stdio."
_PROCESS_LOCK = "picotool:all"
_VERSION = re.compile(r"(?:picotool v)?(2\.2\.0-a4|2\.\d+\.\d+)(?:[ \t]+\([^\r\n]*\))?")
_LEGACY_INFO_VERSIONS = {"2.0.0", "2.1.0", "2.1.1", "2.2.0", "2.2.0-a4"}
_DEVICE_HEADER = "Device Information"
_CHIP = re.compile(r"\s*type:\s*(RP2040|RP2350)\s*")
_RUNNING_HINT = re.compile(r"(RP2040|RP2350) device at bus \d+, address \d+ appears to have a USB serial connection")
_GENERIC_RUNNING_HINT = re.compile(
    r"Device at bus \d+, address \d+ appears to be a RP2040 device with a USB serial connection"
)
_UNIX_HINT_END = ", so consider -f (or -F) to force reboot in order to run the command."
_WINDOWS_HINT_END = ", not in BOOTSEL mode. You can force reboot into BOOTSEL mode via 'picotool reboot -f -u' first."
_WRITE = re.compile(r"Loading into Flash:[ \t]+\[={30}\]  100%")
_VERIFY = re.compile(r"Verifying Flash:[ \t]+\[={30}\]  100%")
_START = "The device was rebooted to start the application."
_RETURN = "The device was asked to reboot back into application mode."
_HOLD = (
    "The device has been left accessible, but without the drive mounted; "
    "use 'picotool reboot' to reboot into regular BOOTSEL mode or application mode."
)
_TRACKING_SERIAL = re.compile(r"(Tracking device serial number )[^\r\n]+?( for reboot)", re.IGNORECASE)
_HOST_PATH = re.compile(r"""(?<![\w:/])(?:[a-zA-Z]:[\\/]|\\\\|/(?!/))[^ \t\r\n"'<>]+""")


class _Command(NamedTuple):
    status: int | None
    lines: list[str]
    result: JsonObject


def _signed_status(status: int | None) -> int | None:
    # On POSIX, these tool-defined negative exits occupy the high byte
    # values; negative subprocess statuses themselves denote signals.
    if status is not None:
        if 0x80000000 <= status <= 0xFFFFFFFF:
            return status - 0x100000000
        if 157 <= status <= 255:
            return status - 256
        if status < 0 and os.name != "nt":
            return None
    return status


def _failure(tool: str, error: str, summary: str, *, contact: JsonObject = NOT_CONTACTED, **details: object) -> JsonObject:
    return {
        "ok": False, "tool": tool, "backend": "picotool", "error_type": error,
        "summary": summary, **contact, **details, **remediation_fields(error, "picotool"),
    }


def _redact(text: str, serial: str | None, paths: list[str]) -> str:
    if serial:
        text = re.sub(re.escape(serial), "[redacted]", text, flags=re.IGNORECASE)
    for spelling in sorted({spelling for path in paths for spelling in (path, path.replace("\\", "/"))}, key=len, reverse=True):
        if spelling:
            text = text.replace(spelling, "[redacted]")
    text = _TRACKING_SERIAL.sub(r"\1[redacted]\2", text)
    return redact_stream_text(_HOST_PATH.sub("[redacted]", text))


def _diagnostic_paragraphs(lines: list[str]) -> list[str]:
    paragraphs = re.split(r"\n(?:[ \t]*\n)+", "\n".join(lines))
    return [paragraph for paragraph in paragraphs if paragraph.strip()]


def _running_board_hint(paragraphs: list[str], version: str) -> re.Match[str] | None:
    # Notices can share a paragraph. Wrapped hint continuations are indented.
    messages = (
        " ".join(message.split())
        for paragraph in paragraphs
        for message in re.split(r"\n(?=\S)", paragraph)
    )
    if next(messages, None) != "but:":
        return None
    pattern = _GENERIC_RUNNING_HINT if version == "2.0.0" else _RUNNING_HINT
    endings = (_UNIX_HINT_END, _WINDOWS_HINT_END) if version in _LEGACY_INFO_VERSIONS else (_UNIX_HINT_END,)
    for message in messages:
        for ending in endings:
            if message.endswith(ending) and (match := pattern.fullmatch(message.removesuffix(ending))):
                return match
    return None


def _flash_confirmed(lines: list[str], final_lines: tuple[str, ...]) -> bool:
    write = next((i for i, line in enumerate(lines) if _WRITE.fullmatch(line)), None)
    if write is None:
        return False
    verify = next((i for i in range(write + 1, len(lines) - 1) if _VERIFY.fullmatch(lines[i])), None)
    if verify is None or lines[verify + 1] != "  OK":
        return False
    if not final_lines:
        return True
    tail = lines[verify + 2:]
    paragraphs = (" ".join(paragraph.split()) for paragraph in _diagnostic_paragraphs(tail))
    return any(message in final_lines for message in (*tail, *paragraphs))


class PicotoolBackend:
    backend_name = "picotool"

    def __init__(self, config: AgenticHILConfig):
        self.config = config

    def reconfigure(self, config: AgenticHILConfig) -> None:
        self.config = config

    def close(self) -> None:
        pass

    def opens_debug_sessions(self) -> bool:
        return False

    def sessionless_debug_tools(self) -> frozenset[str]:
        return frozenset()

    def info(self) -> JsonObject:
        _, result = self._version("debugger_info")
        return result

    def target_support(self) -> JsonObject:
        family = self.config.debugger.target_type
        tool = "debugger_target_support"
        if family is not None and family not in {"rp2040", "rp2350"}:
            return _failure(
                tool, "target_type_invalid", "Picotool target_type must be rp2040 or rp2350.",
                contact={}, target_type=family, status="unsupported",
            )
        return {
            "ok": True,
            "tool": tool,
            "backend": self.backend_name,
            "target_type": family,
            "status": "not_configured" if family is None else "supported",
            "summary": "Picotool supports RP2040 and RP2350. Flashing checks the configured family against the selected board.",
        }

    def classify_last_error(self) -> JsonObject:
        return classify_failure_report(self.config, lambda error: [])

    def _run(self, tool: str, executable: str, args: list[str], *, device: bool = True) -> _Command:
        config = self.config
        serial = config.debugger.probe_id if device else None
        if device and (not serial or not serial.strip() or serial.startswith(("-", "@"))):
            return _Command(None, [], _failure(tool, "invalid_argument", "Picotool requires a valid configured USB serial."))
        command = [*invocation(executable), *args]
        if serial is not None:
            command.extend(("--ser", serial))
        lock = BenchMutex(label="picotool invocation")
        timeout = config.debugger.timeout_s
        try:
            lock.acquire_named(_PROCESS_LOCK, wait_s=min(timeout, MAX_WAIT_S), use_windows_pipe=True)
        except DeviceBusyError:
            return _Command(None, [], _failure(tool, "device_busy", "Another Agentic HIL call is running picotool."))
        try:
            completed = spawn_command(command, config.work_dir, timeout)
        finally:
            lock.release_all()
        return self._command_result(tool, command, completed, device=device)

    def _command_result(self, tool: str, command: list[str], completed: CompletedCommand, *, device: bool) -> _Command:
        config = self.config
        paths = [config.work_dir, *(arg for arg in command if Path(arg).is_absolute())]
        serial = config.debugger.probe_id
        stdout, stderr = (_redact(stream, serial, paths) for stream in (completed.stdout, completed.stderr))
        log_path = str(Path(logs_directory(config)) / f"picotool-{uuid4().hex}.log")
        log = {
            "command": [_redact(arg, serial, paths) for arg in command],
            "returncode": completed.returncode, "timed_out": completed.timed_out,
            "stdout": stdout, "stderr": stderr,
        }
        audit_error = write_audit_log(config, log_path, json.dumps(log, indent=2) + "\n")
        if completed.not_found or completed.not_executable:
            result = _failure(tool, "debugger_not_found", "The configured picotool executable could not be started.", backend_error_type="picotool_not_found")
        elif completed.timed_out and device:
            result = _failure(tool, "timeout", "Picotool exceeded the configured timeout.", contact=CONTACT_UNPROVEN)
        else:
            result = {"ok": True, "tool": tool, "backend": self.backend_name}
        result["log_path"] = display_path(config, log_path)
        result["backend_output"] = (stdout + "\n" + stderr).strip()
        if audit_error is not None:
            result = mark_audit_failure(result, audit_error)
        # Windows text output adds CRLF after a progress bar's own CR.
        lines = completed.stdout.replace("\r\r\n", "\r\n").splitlines()
        return _Command(_signed_status(completed.returncode), lines, result)

    def _version(self, tool: str) -> tuple[str | None, JsonObject]:
        executable = self.config.debugger.executable or host_debugger_executable(self.backend_name)
        if executable is None or (Path(executable).is_absolute() and not Path(executable).is_file()):
            return None, _failure(tool, "debugger_not_found", "Picotool was not found on PATH.", backend_error_type="picotool_not_found")
        run = self._run(tool, executable, ["version", "-s"], device=False)
        if not run.result["ok"] or run.result.get("audit_ok") is False:
            return None, run.result
        version = _VERSION.fullmatch(run.lines[0]) if len(run.lines) == 1 else None
        if run.status != 0 or version is None:
            return None, {**run.result, **_failure(
                tool, "debugger_not_found", "Picotool must report a readable 2.x version before device access.",
                backend_error_type="picotool_version_unsupported",
            )}
        return executable, {**run.result, "version": version[1], "summary": "Picotool 2.x is available."}

    def _detect(self, tool: str) -> tuple[str | None, JsonObject]:
        executable, version_result = self._version(tool)
        if executable is None:
            return None, version_result
        version = str(version_result["version"])
        run = self._run(tool, executable, ["info", "-d"])
        if not run.result["ok"] or run.result.get("audit_ok") is False:
            return None, run.result
        if run.status == 0 and _DEVICE_HEADER in run.lines:
            chip = next((match[1] for line in run.lines if (match := _CHIP.fullmatch(line))), None)
            if chip is not None:
                return executable, {**run.result, "present": True, "target_state": "bootsel", "chip": chip, "summary": "The selected board is in BOOTSEL."}
        family = "RP2040/RP2350" if version == "2.0.0" else "RP-series"
        found = "found found" if version in _LEGACY_INFO_VERSIONS else "found"
        absent = f"No accessible {family} devices in BOOTSEL mode were {found} with serial number {self.config.debugger.probe_id}."
        paragraphs = _diagnostic_paragraphs(run.lines) if run.status == -7 else []
        # Fold line breaks without collapsing spaces inside a USB serial.
        first_message = " ".join(line.strip() for line in paragraphs[0].splitlines()) if paragraphs else ""
        if first_message == absent:
            hint = _running_board_hint(paragraphs[1:], version)
            if hint is not None:
                # 2.0.0 calls either running family an RP2040 device.
                chip = None if version == "2.0.0" else hint[1]
                return executable, {**run.result, "present": True, "target_state": "application", "chip": chip, "summary": "The application is running and exposes the USB reset interface."}
            return None, {**run.result, **_failure(tool, "target_not_detected", "The configured board was not detected.", present=False, chip=None)}
        # A completed, unforced info command can read the board without changing it.
        return None, {**run.result, **_failure(
            tool, "probe_failed", f"Picotool {version} returned an unrecognized reply to info -d.",
            contact={"side_effect_committed": False, "side_effect_status": "not_started", "hardware_state": "unchanged"},
            version=version,
        )}

    def list_probes(self) -> JsonObject:
        tool = "debugger_probes_list"
        rows: list[JsonObject] = []
        failure: JsonObject = {}
        for name, entry in self.config.debuggers.items():
            if entry.type != "picotool":
                continue
            if not self.config.probe_allowed(entry):
                rows.append({"debugger_id": name, "status": "not_listed"})
                continue
            if failure.get("cleanup_required") or failure.get("audit_ok") is False:
                rows.append({"debugger_id": name, "status": "unknown"})
                continue
            backend = self if name == self.config.debugger_id else PicotoolBackend(bind_debugger(self.config, name))
            _, result = backend._detect(tool)
            failed = result.get("audit_ok") is False or (not result["ok"] and result["error_type"] != "target_not_detected")
            if failed:
                # Give a failure that stops enumeration precedence over a settled one.
                if not failure or result.get("cleanup_required") or result.get("audit_ok") is False:
                    failure = result
                rows.append({**result, "debugger_id": name, "ok": False})
                continue
            rows.append({
                "debugger_id": name, "backend": self.backend_name, "ok": True,
                **{key: result[key] for key in ("present", "target_state", "chip", "log_path") if key in result},
            })
        return {
            "ok": True, "tool": tool, "backend": self.backend_name, "summary": PICOTOOL_LISTING_NOTICE,
            **failure, "probes": rows,
        }

    def probe_target(self) -> JsonObject:
        if not self.config.probe_allowed():
            return self._permission_denied("probe_target", "Probing is disabled by the authoritative config.", "allow_probe")
        return self._detect("probe_target")[1]

    def flash_firmware(self, artifact: JsonObject, reset_after_flash: bool = False) -> JsonObject:
        tool = "flash_firmware"
        permissions = self.config.debugger.permissions
        if not permissions.allow_flash:
            return self._permission_denied(tool, "Flashing is disabled by the authoritative config.", "allow_flash")
        for blocking in ("allow_raw_debugger_commands", "allow_mass_erase"):
            if getattr(permissions, blocking):
                debugger_id = self.config.debugger_id
                return _failure(tool, "permission_denied", exclusive_permission_summary("Flashing", blocking, debugger_id),
                                **exclusive_permission_fields(blocking, debugger_id))
        if reset_after_flash and not permissions.allow_reset:
            return self._permission_denied(tool, "Starting the application after flashing is disabled by the authoritative config.", "allow_reset")
        path = Path(str(artifact["resolved_path"]))
        image_format = path.suffix.lower().removeprefix(".")
        if image_format not in {"uf2", "elf", "bin"}:
            return _failure(tool, "invalid_argument", "Picotool flashes UF2, ELF or BIN files.")
        args = ["load", str(path), "-v", "-t", image_format]
        if image_format == "bin":
            try:
                address = int(self.config.debugger.flash_address or "", 0)
            except ValueError:
                address = -1
            if not 0x10000000 <= address < 0x11000000 or address % 4096:
                return _failure(tool, "invalid_argument", "BIN flashing requires an explicit 4 KiB-aligned flash_address in the first XIP window.")
            args.extend(("-o", hex(address)))
        executable, detected = self._detect(tool)
        if executable is None:
            return detected
        family = self.config.debugger.target_type
        chip = detected["chip"]
        if family is not None and family.upper() != chip:
            summary = (
                "Picotool 2.0.0 cannot identify the running board's chip family for the target_type check."
                if chip is None else "The detected chip family does not match target_type."
            )
            return {**detected, **_failure(tool, "target_type_invalid", summary)}
        running = detected["target_state"] == "application"
        if reset_after_flash:
            args.append("-x")
            final_lines = (_START, _RETURN) if running else (_START,)
        elif running:
            final_lines = (_HOLD,)
        else:
            final_lines = ()
        if running:
            args.append("-f" if reset_after_flash else "-F")
        run = self._run(tool, executable, args)
        if not run.result["ok"] or run.result.get("audit_ok") is False:
            return run.result
        if run.status != 0 or not _flash_confirmed(run.lines, final_lines):
            mismatch = run.status == -11 or "ERROR: The device contents did not match the file" in run.lines
            error = "verify_failed" if mismatch else "flash_failed"
            summary = "Picotool did not confirm the write, verification and requested final state."
            if run.status == -8 and any("is too big to fit in flash size" in line for line in run.lines):
                summary = "Picotool found that the image was too large for the selected board's flash."
            return {**run.result, **_failure(tool, error, summary, contact=CONTACT_UNPROVEN)}
        return {
            **run.result, "success_confirmed": True, "verified": True,
            "target_state": "application" if reset_after_flash else "bootsel",
            "held_in_rom_bootloader": not reset_after_flash,
            "summary": "Image written and verified. " + ("The application was started." if reset_after_flash else "The board is held in BOOTSEL."),
        }

    def reset_target(self, mode: str = "run") -> JsonObject:
        tool = "reset_target"
        if not self.config.debugger.permissions.allow_reset:
            return self._permission_denied(tool, "Reset is disabled by the authoritative config.", "allow_reset")
        if mode == "init":
            return {**reset_init_unsupported(self.backend_name, "picotool provides USB reboot only"), **remediation_fields("not_supported", self.backend_name)}
        if mode not in {"run", "halt"}:
            return _failure(tool, "invalid_argument", "Picotool reset accepts run or halt.")
        executable, detected = self._detect(tool)
        if executable is None:
            return detected
        start_application = mode == "run"
        args = ["reboot", "-a" if start_application else "-u"]
        running = detected["target_state"] == "application"
        if running:
            args.append("-f")
        verb = "was asked to reboot" if running else "was rebooted"
        destination = "application" if start_application else "BOOTSEL"
        run = self._run(tool, executable, args)
        if not run.result["ok"] or run.result.get("audit_ok") is False:
            return run.result
        if run.status != 0 or f"The device {verb} into {destination} mode." not in run.lines:
            return {**run.result, **_failure(tool, "reset_failed", "Picotool did not confirm the requested reboot.", contact=CONTACT_UNPROVEN)}
        return {
            **run.result, "mode": mode, "success_confirmed": True, "target_state": destination.lower(),
            "summary": "The board rebooted into the application." if start_application else "The board rebooted into BOOTSEL. The application is stopped.",
        }

    def _permission_denied(self, tool: str, summary: str, key: str) -> JsonObject:
        permission = permission_key("debuggers", self.config.debugger_id, key)
        result = _failure(tool, "permission_denied", permission_denied_summary(summary, permission), **permission_denied_fields(permission))
        return {**result, **remediation_fields("permission_denied", self.backend_name, permission=permission)}

    def _debug_unsupported(self, tool: str) -> JsonObject:
        return _failure(tool, "not_supported", "Picotool provides USB bootloader operations and has no CPU debug interface.")

    def debug_start_session(self, artifact: JsonObject, mode: str = "attach", timeout_s: float | None = None) -> JsonObject:
        return self._debug_unsupported("debug_start_session")

    def debug_stop_session(self, timeout_s: float | None = None) -> JsonObject:
        return self._debug_unsupported("debug_stop_session")

    def debug_get_session_status(self) -> JsonObject:
        return self._debug_unsupported("debug_get_session_status")

    def debug_set_breakpoint(self, location: JsonObject) -> JsonObject:
        return self._debug_unsupported("debug_set_breakpoint")

    def debug_list_breakpoints(self) -> JsonObject:
        return self._debug_unsupported("debug_list_breakpoints")

    def debug_clear_breakpoints(self) -> JsonObject:
        return self._debug_unsupported("debug_clear_breakpoints")

    def debug_continue(self, timeout_s: float | None = None) -> JsonObject:
        return self._debug_unsupported("debug_continue")

    def debug_halt(self, timeout_s: float | None = None) -> JsonObject:
        return self._debug_unsupported("debug_halt")

    def debug_get_stop_reason(self) -> JsonObject:
        return self._debug_unsupported("debug_get_stop_reason")

    def debug_symbol_info(self, symbol: str, symbol_elf: JsonObject | None = None) -> JsonObject:
        return self._debug_unsupported("debug_symbol_info")

    def debug_symbol_value(self, symbol: str, symbol_elf: JsonObject | None = None) -> JsonObject:
        return self._debug_unsupported("debug_symbol_value")

    def debug_dump_symbol_ihex(self, symbol: str, output: JsonObject, symbol_elf: JsonObject | None = None) -> JsonObject:
        return self._debug_unsupported("debug_dump_symbol_ihex")
