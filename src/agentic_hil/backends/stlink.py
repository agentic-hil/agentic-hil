from __future__ import annotations

import json
import re
import shutil
import socket
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Literal

from agentic_hil.artifacts import looks_like_intel_hex
from agentic_hil.backends.common import (
    FAILURE_WORDS,
    NOT_CONTACTED,
    READ_ONLY_TOOLS,
    CompletedCommand,
    command_for_log,
    contains_any,
    contains_failure_text,
    debug_session_unsupported,
    find_stm32_programmer_cli,
    invocation,
    not_executable_refusal,
    programmer_output_fields,
    reports_reset_failure,
    reset_init_unsupported,
    spawn_command,
    which,
)
from agentic_hil.backends.gdbdebug import (
    GdbDebugSessions,
    GdbServerSteps,
    ServerCompanion,
    decode_symbol_value,
    offline_symbol_info,
    resolve_symbol_offline,
    validate_debug_symbol,
)
from agentic_hil.config import (
    ConfigError,
    display_path,
    resolve_work_path,
    safe_configured_directory,
    safe_write_text,
)
from agentic_hil.gdbmi import read_intel_hex_file
from agentic_hil.knowledge import (
    exclusive_permission_fields,
    exclusive_permission_summary,
    permission_denied_fields,
    permission_denied_summary,
    permission_key,
    remediation_fields,
)
from agentic_hil.process import spawn_managed_process, terminate_process_tree
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
    # The debugger failed and its output says no more than that. `debugger_error`
    # is the name the debug-session path has always published for exactly that
    # (`_start_failure` in gdbdebug), and a caller that reads one word for it in
    # a failed session and another in a failed command has to learn two. The
    # backend's own `unknown_debugger_error` travels beside it in
    # `backend_error_type` for a reader at that layer (#506).
    "unknown_debugger_error": "debugger_error",
    # The GDB server a debug session runs on, named by
    # `debuggers.<name>.gdb_server_executable` or found beside the CLI, is not
    # there any more. A program this backend starts that is missing, which is
    # what `debugger_not_found` says for the CLI itself.
    "gdb_server_not_found": "debugger_not_found",
}

# One entry per tool, and every entry is a line the operation itself prints.
# The connect banner is not in any of them: STM32CubeProgrammer prints the
# ST-Link serial and the device name whenever it opens a probe, so a reset that
# connected and then did nothing would confirm itself out of the banner alone.
# `reset_target` therefore waits for "MCU Reset", and a memory read waits for
# "Data read successfully", measured against a NUCLEO-F446RE on
# STM32CubeProgrammer v2.23.0, which prints it after the UPLOADING block and
# before the elapsed-time line.
#
# The two memory reads share that line because they are one command: both send
# `-r` and differ only in what becomes of the file it wrote. Held in one name so
# a measured marker is never transcribed twice and the two can never drift into
# confirming themselves differently.
STLINK_MEMORY_READ_CONFIRMATION = ["Data read successfully"]
STLINK_SUCCESS_CONFIRMATION = {
    "probe_target": ["ST-LINK SN", "Device name"],
    "flash_firmware": ["Download verified successfully"],
    "debug_dump_symbol_ihex": STLINK_MEMORY_READ_CONFIRMATION,
    "debug_symbol_value": STLINK_MEMORY_READ_CONFIRMATION,
}
# The typed-debug reads this backend answers with no session behind them, and so
# the ones the coordination layer must lease as one-shots rather than run on a
# session lease that does not exist here. The same `-r` command, so the same
# pair the confirmation table above carries.
SESSIONLESS_DEBUG_READS = frozenset({"debug_dump_symbol_ihex", "debug_symbol_value"})
# One row per reset mode, holding the argument that goes on the wire and the
# lines STM32CubeProgrammer prints when that argument did what it says. The two
# belong to the same command and are kept together for that reason: `-rst` and
# `-halt` are different commands with different success lines, and a single
# marker set for `reset_target` carried the `-rst` lines only: mode `halt` did
# exactly what it was asked, printed neither of them, and could therefore never
# report anything but an unconfirmed outcome (#142).
#
# `-halt` measured on STM32CubeProgrammer v2.23.0 against a NUCLEO-F446RE: the
# NORMAL connect banner carries `Reset mode  : Software reset` and the run ends
# with `Core halted`. The banner settles what this mode does (a software reset
# and then a halt, the same operation OpenOCD's and pyOCD's `reset halt`
# perform), but it is not a second marker: connect prints it before the halt is
# attempted, so a run that connected and then failed to halt prints it
# unchanged. Only `Core halted` is the operation's own success line, and it is
# the only one of the two a failure withholds.
STLINK_RESET_MODES: dict[str, dict[str, list[str]]] = {
    "run": {"args": ["-rst"], "success_text": ["MCU Reset", "reset is performed"]},
    "halt": {"args": ["-halt"], "success_text": ["Core halted"]},
}
# `debuggers.<name>.connect_mode` in the spelling STM32CubeProgrammer's own
# `--connect` usage prints: "[mode=<mode>] : Connection mode. Value in
# {UR/HOTPLUG/NORMAL/POWERDOWN/HWRSTPULSE}". Taken from the CLI's usage text
# rather than assumed, because `UR` is the one value here that could not be
# guessed from the word it abbreviates, and the same text is where its
# consequence is written down too: "Reset mode with UR connection mode is HWrst",
# so a bench that selects it needs the probe's reset line wired to NRST.
#
# Only the two modes the configuration can ask for are here. NORMAL is not one of
# them: it stays what `reset_target` connects with, chosen by the operation
# rather than by the file.
STLINK_CONNECT_MODES: dict[str, str] = {"hotplug": "HOTPLUG", "under_reset": "UR"}
# The connect the typed-debug reads use, and the whole reason they are allowed to
# exist on this backend: a read that resets the target destroys the RAM it is
# reading and reports the reset image's bytes as a measurement (#342). The reads
# used to send `mode=NORMAL`, which is the CLI's default and is a connect that
# resets: STM32CubeProgrammer's own `--connect` usage prints
#
#     [mode=<mode>]      : Connection mode. Value in {UR/HOTPLUG/NORMAL/POWERDOWN/HWRSTPULSE}
#                          default mode: NORMAL
#     [reset=<mode>]     : Reset modes: SWrst/HWrst/Crst. Default mode: SWrst
#
# and #329 measured what that costs on this interface: a SysTick counter read
# twice across two invocations jumped backwards, so the target had rebooted
# between them. A coverage dump taken that way is a dump of a freshly reset
# board.
#
# HOTPLUG is the CLI's own name for attaching to a target as it is. The evidence
# is the CLI's own words rather than an assumption about what "hot plug" ought to
# mean, in the two places it writes them down:
#
# * the message it prints after such a connect, "Device connected in HotPlug
#   mode, a Reset is needed to program the flash": a reset is still *needed*,
#   which is the programmer stating that its connect did not perform one; and
# * the advice it gives for the one command it ships that only reads, "The
#   suggested connect mode while using the -regdump command is hotplug."
#
# ST's UM2237 (*STM32CubeProgrammer software description*, the `-c/--connect`
# section) is where the modes are defined, and defines hot plug as the connect
# that reaches a running target without resetting or halting it, against normal,
# which resets first. The verbatim evidence kept here is the CLI's, because the
# CLI is what this backend executes and its strings can be re-read from the
# installed binary on any bench that doubts this comment.
#
# No `reset=` travels with it. The option's default is SWrst and it belongs to
# the connects that reset; sending one alongside HOTPLUG would ask the least
# intrusive connect for the thing it was chosen to avoid.
#
# Documented, and since 2026-08-30 also measured: issue #348 holds the bench
# evidence, five SysTick pairs on a NUCLEO-F446RE advancing by wall time
# through this connect, a deliberate reset control the counter detected, and
# ten read logs carrying this argv with mode=HOTPLUG and no reset= option.
# Nothing in a result claims the proof; what a result carries is the log,
# and the log carries this argv.
STLINK_READ_CONNECT_MODE: Literal["HOTPLUG"] = "HOTPLUG"

STLINK_SERIAL_PATTERN = re.compile(r"^\s*ST-?LINK\s+SN\s*:\s*(\S+)\s*$", re.IGNORECASE | re.MULTILINE)
STLINK_DEVICE_PATTERN = re.compile(r"^\s*Device\s+name\s*:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)
STLINK_EMPTY_MARKERS = ["no st-link detected", "no stlink detected", "0 st-link detected", "0 stlink detected"]
# STM32CubeProgrammer's own words for a flash erase the device would not carry
# out. Measured on a NUCLEO-F446RE against STM32CubeProgrammer v2.22.0, where a
# first flash after power-up ends:
#
#     Erasing memory corresponding to segment 0:
#     Erasing internal memory sectors [0 5]
#     Error: failed to erase memory
#
# One phrase, because one phrase is what that bench proved. The catalogue used
# to read this transcript as `reset_failed`: the connect banner every action
# prints carries `Reset mode  : Software reset`, the refusal carries `failed`,
# and the reset rule below matches any output holding both, so a programmer that
# said "erase" sent the operator to the reset line (#327). A phrase nobody has
# seen a programmer print does not belong here; adding one on a guess would
# claim an erase refusal for a transcript that never refused an erase.
STLINK_ERASE_REFUSAL_MARKERS = ["failed to erase memory"]
# The lines STM32CubeProgrammer prints once it has begun changing what is in
# flash, and therefore the lines that separate the `flash_change_underway`
# reading from the weaker one (#328). Each one is printed after the operation it
# names has started, which is the whole property that makes it positive evidence
# a phase ran:
#
# * `Download in Progress` opens the write phase for a segment, and the erase for
#   that segment is behind it.
# * `File download complete` and `Download verified successfully` close it.
# * `Mass erase successfully achieved` is an erase that finished.
# * A progress bar belongs to whichever phase is running, so one anywhere in the
#   transcript says a phase ran rather than being announced.
#
# `Erasing memory corresponding to segment 0:` and `Erasing internal memory
# sectors [0 5]` are deliberately not in this set: the programmer prints both
# before it asks the device for anything, so they name what it intends to erase
# rather than what it erased. Their absence from the change set is what makes a
# refusal reached with only those lines the weaker `erase_refused_effect_unconfirmed`
# reading -- but the *absence* of a change marker is not itself proof no sector
# was erased (these lines are not guaranteed across versions, quiet or piped
# output and abort paths), so that reading is unconfirmed and quarantined, not a
# claim that flash is untouched.
STLINK_FLASH_CHANGE_MARKERS = [
    "download in progress",
    "file download complete",
    "download verified successfully",
    "mass erase successfully achieved",
    "[=",
]
# The three readings `erase_abort_point` distinguishes. None of them relaxes the
# quarantine: a refused erase leaves the flash contents unconfirmed whichever
# reading fits, because the transcript can place *a* line of the failure but
# cannot prove that no sector was erased before the refusal (#328 originally read
# the absence of a progress or download marker as that proof, which it is not:
# STM32CubeProgrammer's progress lines are not guaranteed across versions, quiet
# or piped output and abort paths, and `failed to erase memory` also covers a
# protection refusal that can strike after unprotected sectors in the same range
# have already been erased). The readings differ only in how far the transcript
# accounts for the failure, which is what an operator reads to choose a next
# step, not a safety verdict this layer trusts.
ERASE_REFUSED_EFFECT_UNCONFIRMED = "erase_refused_effect_unconfirmed"
FLASH_CHANGE_UNDERWAY = "flash_change_underway"
ERASE_ABORT_POINT_UNREADABLE = "abort_point_unreadable"

# Typed debug sessions on this backend run ST-LINK_gdbserver, the GDB server
# STM32CubeCLT installs beside STM32_Programmer_CLI (#624). What the session
# layer has to know of it, each from version 7.14.0 recorded on the reference
# board (tests/fixtures/st_link_gdbserver_7_14_0_linux_recordings.json and
# st_link_gdbserver_7_14_0_linux_teardown_recordings.json):
#
# * Ready: `Waiting for debugger connection...`, a line of its own that names no
#   port; the server prints it again after each client it accepts.
# * It serves one client and exits when that client leaves, and a bare TCP
#   connect and close is a client. So readiness is the line, never a look at the
#   port, which would end the server it looked at.
# * Started with `-g`, GDB's connect halts the core where it runs and GDB
#   reports that stop. Without `-g` the connect resets the core first.
# * Reset: `monitor reset` answers `^done` after `STM32 Successfully completed
#   reset operation (System reset)` and leaves the core halted at the reset
#   vector with no stop record of its own. `monitor reset halt`, `reset 0` to
#   `reset 2` and `reset init` are each refused as an unknown reset option.
# * Detach: GDB exiting, GDB detaching and the server's own shutdown on SIGTERM
#   each resumed the core, and the server has no command that stops that. A
#   killed server left the core halted where it was, three times out of three,
#   read back by servers killed the same way. So the server is killed, with no
#   SIGTERM first, before GDB detaches.
# * Probe: killed while it held the probe's USB itself, the server left the
#   probe refusing the next opener after some stops (the direct stop round of
#   st_link_gdbserver_7_14_0_linux_session_stops_recordings.json). Started with
#   `-t`, it reaches the probe through stlink-server instead, and killed the same
#   way it never did (the shared round). So a session runs it with `-t` whenever
#   an stlink-server is there or can be started (`_start_probe_server`).
ST_LINK_GDB_SERVER_STEPS = GdbServerSteps(
    ready_line="Waiting for debugger connection...",
    ready_line_ends_the_line=True,
    reset_halt_command='-interpreter-exec console "monitor reset"',
    reset_halt_confirmation="Successfully completed reset operation",
    detach_guard_command=None,
    server_resets_at_start=False,
    graceful_server_shutdown=False,
)
# How ST-LINK_gdbserver says it could not start, in the `Reason:` line it ends
# with, recorded on Linux with the reference board and on Windows with nothing
# on USB (st_link_gdbserver_7_14_0_windows_recordings.json). Its words are not
# STM32_Programmer_CLI's, and the CLI's reading does not fit them: an unknown
# serial is `ST-LINK: <serial> not found`, which that reading takes for a
# missing input file. First match wins, in this order.
ST_LINK_GDB_SERVER_REFUSALS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # `No ST-LINK found. Please check ST-LINK USB cable.` with nothing on USB,
    # and `ST-LINK: <serial> not found.` for a serial no connected probe has.
    ("probe_not_found", re.compile(r"no st-link found|st-link: \S+ not found", re.IGNORECASE)),
    # `Target USB comms error`, then `Error in initializing ST-LINK device.` and
    # `Reason: USB communication error. Please reconnect the ST-LINK USB cable
    # and try again.`, from a probe a killed server had held (the direct stop
    # round of st_link_gdbserver_7_14_0_linux_session_stops_recordings.json).
    ("adapter_usb_error", re.compile(r"target usb comms error|usb communication error", re.IGNORECASE)),
    # `Failed to connect to device. Please check power and cabling to target.`
    # for a probe another server holds, and `Unknown MCU found on target.` for a
    # JTAG connect to the reference board's SWD-only wiring. A target that is
    # off is the other cause the server's words name, so neither line is read as
    # proof that nothing behind the probe was reached.
    ("target_not_detected", re.compile(r"failed to connect to device|unknown mcu found on target", re.IGNORECASE)),
    # `Couldn't locate STM32CubeProgrammer in '<-cp>', use -cp <path>`.
    ("stm32_programmer_cli_not_found", re.compile(r"couldn't locate stm32cubeprogrammer", re.IGNORECASE)),
)
# The refusals that end the server before any probe carried anything.
ST_LINK_GDB_SERVER_PRE_CONTACT = frozenset({"probe_not_found", "stm32_programmer_cli_not_found"})

# stlink-server, which STM32CubeCLT ships to share one probe between its tools,
# and the port its clients reach it on (ST-LINK_gdbserver `-t`). It needs no
# options and listens on loopback; version 2.1.1 refused `--auto-exit` and ended
# at once on SIGTERM, with exit status -15 (recorded).
STLINK_SERVER_PORT = 7184
STLINK_SERVER_LISTEN_TIMEOUT_S = 10.0
STLINK_SERVER_OUTPUT_TAIL_LINES = 20
# What a session stop risks when the GDB server holds the probe's USB itself:
# the direct stop round (st_link_gdbserver_7_14_0_linux_session_stops_recordings.json).
DIRECT_PROBE_STOP_RISK = (
    "A session stop kills ST-LINK_gdbserver while it holds the probe's USB itself. In the recorded direct stop round "
    "on the reference board, 8 of 99 such stops left the probe refusing its next opener: 4 server starts with "
    "`Target USB comms error` and 4 STM32_Programmer_CLI calls with `ST-LINK error (DEV_USB_COMM_ERR)`. "
    "Every stop still confirmed the core halted. Put stlink-server on PATH so sessions reach the probe through it."
)


def find_stlink_server() -> str | None:
    """The stlink-server on this host's PATH, as `shutil.which` names it."""
    return shutil.which("stlink-server")


def stlink_server_listening(port: int) -> bool:
    """Whether something accepts connections on `port` on loopback."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.5)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def output_tail(path: Path) -> list[str]:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as error:
        return [f"{path.name} could not be read: {error}"]
    return lines[-STLINK_SERVER_OUTPUT_TAIL_LINES:]


def classify_gdb_server_output(output: str) -> str:
    """The backend error ST-LINK_gdbserver's output names, or `unknown_debugger_error`."""
    for backend_error_type, pattern in ST_LINK_GDB_SERVER_REFUSALS:
        if pattern.search(output):
            return backend_error_type
    return "unknown_debugger_error"


def gdb_server_decisive_line(output: str, classified: str) -> str | None:
    """The line a start failure is reported by.

    The first line that alone classifies as the whole output did. For output
    nothing classifies, the last line that reports a failure, because the server
    ends a failed start with `Shutting down...` and `Exit.` (a taken GDB port,
    recorded), and only then the last line."""
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    if not lines:
        return None
    if classified != "unknown_debugger_error":
        for line in lines:
            if classify_gdb_server_output(line) == classified:
                return line
    failures = [line for line in lines if contains_any(line.lower(), ["fail", "error"])]
    return failures[-1] if failures else lines[-1]


class STLinkBackend:
    backend_name = "stlink"

    def __init__(self, config: AgenticHILConfig):
        self.config = config
        # The directory of the STM32_Programmer_CLI the session being started
        # resolved, which its server is given as `-cp`.
        self._programmer_directory: str | None = None
        # Whether the session being started reaches the probe through
        # stlink-server, which its server is then told with `-t`.
        self._shared_probe = False
        self._debug = GdbDebugSessions(
            config,
            backend_name=self.backend_name,
            resolve_server=self._resolve_debug_server,
            build_server_args=self._debug_server_args,
            classify_server_output=classify_gdb_server_output,
            server_steps=ST_LINK_GDB_SERVER_STEPS,
            read_start_failure=self._debug_start_failure,
            start_server_companion=self._start_probe_server,
        )

    def reconfigure(self, config: AgenticHILConfig) -> None:
        # An open session may not outlive a change to the probe it runs on, to
        # the server it runs (the entry's `gdb_server_executable` is part of
        # it), or a grant that refuses sessions, read exactly as the OpenOCD
        # and pyOCD backends read them.
        debugger_changed = config.debugger != self.config.debugger or config.target != self.config.target
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
            return {"tool": "debugger_info", **STLINK_NOT_FOUND}
        if completed.not_executable:
            return {"tool": "debugger_info", **not_executable_refusal(self.backend_name, str(resolved["executable_path"]), completed)}
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
            return self._permission_denied(tool, "Debugger probe discovery is disabled by the authoritative config.", self._permission_key("allow_probe"))
        resolved = self._resolve_executable()
        if not resolved["ok"]:
            return {"tool": tool, **resolved}
        # `-l st-link-only` enumerates connected probes and never connects to a
        # target, so every failure of it (including the timeout, whose child
        # spawn_command reaped before returning) leaves the bench untouched
        # and carries the markers that say so.
        command = [*invocation(str(resolved["executable_path"])), "-q", "-l", "st-link-only"]
        completed = spawn_command(command, str(Path(str(resolved["executable_path"])).parent), self.config.debugger.timeout_s)
        if completed.not_found:
            return {"tool": tool, **STLINK_NOT_FOUND}
        if completed.not_executable:
            return {"tool": tool, **not_executable_refusal(self.backend_name, str(resolved["executable_path"]), completed)}
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
            return self._permission_denied("probe_target", "Probing is disabled by the authoritative config.", self._permission_key("allow_probe"))
        result = self._run_stlink("probe_target", self._connection_args("HOTPLUG"))
        if result.get("ok"):
            result["target_detected"] = True
            result["summary"] = "Target detected through ST-Link."
        return self._write_action_report(result)

    def flash_firmware(self, artifact: JsonObject, reset_after_flash: bool = False) -> JsonObject:
        if not self.config.debugger.permissions.allow_flash:
            return self._permission_denied("flash_firmware", "Flashing is disabled by the authoritative config.", self._permission_key("allow_flash"))
        if self.config.debugger.permissions.allow_raw_debugger_commands:
            return self._exclusive_permission_denied("flash_firmware", "Flashing", "allow_raw_debugger_commands")
        if self.config.debugger.permissions.allow_mass_erase:
            return self._exclusive_permission_denied("flash_firmware", "Flashing", "allow_mass_erase")

        artifact_path = str(artifact["resolved_path"])
        write_args = ["-w", artifact_path]
        if Path(artifact_path).suffix.lower() == ".bin":
            if self.config.debugger.flash_address is None:
                return {"ok": False, "tool": "flash_firmware", "backend": self.backend_name, "error_type": "invalid_argument", "summary": "Flashing .bin artifacts with ST-Link requires debuggers.<name>.flash_address.", "artifact": {"source": artifact.get("source", "path"), "path": artifact.get("path"), "sha256": artifact.get("sha256")}}
            write_args.append(self.config.debugger.flash_address)
        reset_args = ["-rst"] if reset_after_flash else []
        result = self._run_stlink("flash_firmware", [*self._connection_args(self._flash_connect_mode()), *write_args, "-v", *reset_args])
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

    # The session tools run on ST-LINK_gdbserver when the entry has one, and
    # refuse naming the ways to get one when it has none. A session already on
    # file is answered by the session layer whatever the entry says now.
    def debug_start_session(self, artifact: JsonObject, mode: str = "attach", timeout_s: float | None = None) -> JsonObject:
        if not self.opens_debug_sessions():
            return self._unsupported_debug_tool("debug_start_session")
        return self._debug.start_session(artifact, mode, timeout_s)

    def debug_stop_session(self, timeout_s: float | None = None) -> JsonObject:
        if not self._serves_session_tools():
            return self._unsupported_debug_tool("debug_stop_session")
        return self._debug.stop_session(timeout_s)

    def debug_get_session_status(self) -> JsonObject:
        if not self._serves_session_tools():
            return self._unsupported_debug_tool("debug_get_session_status")
        return self._debug.get_session_status()

    def debug_set_breakpoint(self, location: JsonObject) -> JsonObject:
        if not self._serves_session_tools():
            return self._unsupported_debug_tool("debug_set_breakpoint")
        return self._debug.set_breakpoint(location.get("location", ""))

    def debug_list_breakpoints(self) -> JsonObject:
        if not self._serves_session_tools():
            return self._unsupported_debug_tool("debug_list_breakpoints")
        return self._debug.list_breakpoints()

    def debug_clear_breakpoints(self) -> JsonObject:
        if not self._serves_session_tools():
            return self._unsupported_debug_tool("debug_clear_breakpoints")
        return self._debug.clear_breakpoints()

    def debug_continue(self, timeout_s: float | None = None) -> JsonObject:
        if not self._serves_session_tools():
            return self._unsupported_debug_tool("debug_continue")
        return self._debug.continue_execution(timeout_s)

    def debug_halt(self, timeout_s: float | None = None) -> JsonObject:
        if not self._serves_session_tools():
            return self._unsupported_debug_tool("debug_halt")
        return self._debug.halt(timeout_s)

    def debug_get_stop_reason(self) -> JsonObject:
        if not self._serves_session_tools():
            return self._unsupported_debug_tool("debug_get_stop_reason")
        return self._debug.get_stop_reason()

    def _serves_session_tools(self) -> bool:
        return self.opens_debug_sessions() or self._session_open()

    def _session_open(self) -> bool:
        """Whether a debug session is on file, settled or not.

        While one is, its server holds the probe, and STM32_Programmer_CLI
        opening the same probe is the busy-probe refusal recorded for a second
        server, so every read goes to the session, which also answers for one
        left unsettled."""
        return self._debug.session is not None

    def debug_symbol_info(self, symbol: str = "", symbol_elf: JsonObject | None = None) -> JsonObject:
        """The shared offline resolution, and nothing of this backend's own.

        The third read this backend serves, and the one that never opens a probe
        at all: an address and a size are properties of the image, and the image
        is on disk. It refused until #342 only because it was filed with the
        session family, which made a bench with an ST-Link ask the hardware for a
        fact the hardware was never going to be asked for.

        Delegated whole because there is nothing here for STM32CubeProgrammer to
        do: `offline_symbol_info` runs a GDB against the flashed ELF, and the
        pyOCD backend answers the same question through the same call (#344).

        Inside a debug session the session's GDB answers instead, against the
        image the session started with, as on OpenOCD and pyOCD (#624).
        """
        if self._session_open():
            return self._debug.symbol_info(symbol)
        return offline_symbol_info(self.config, self.backend_name, symbol, symbol_elf)

    def debug_symbol_value(self, symbol: str = "", symbol_elf: JsonObject | None = None) -> JsonObject:
        """One allowed symbol's bytes, read the only way this backend reads memory.

        The second tool of the typed family this backend serves, and for the
        reason the first one is served: `-r` is a memory read, and what a caller
        asks of it here is the same read the dump makes. It used to refuse,
        because the file that read writes is not what this tool promises and
        writing one anyway looked like approximating the answer. The asymmetry
        was the worse half of that trade (#249): a plan that reads a counter had
        to know which probe it was running on, while the file the objection was
        about is this call's own business, created in a private directory,
        deleted before the result is built, and named nowhere in it.

        Everything a caller can observe is the OpenOCD path's. Same allowlist
        refusal word for word, same `debug.max_dump_size_bytes` cap on the
        resolved size before a probe is opened, same digest-verified offline
        resolution the dump uses, same `hex` and integer readings and summary.
        What differs is what has to: `symbol_source` names the flashed ELF the
        symbol was resolved against, because there is no session that loaded it.

        The parse-back is held to exactly what was asked for. Intel HEX carries
        the address of every record it holds, so the bytes are taken by address
        rather than by position, and a file that does not cover the whole window
        is a failed read rather than a short answer.

        Inside a debug session the read goes through the session (#624): the
        session's server holds the probe, so the CLI could not open it.
        """
        if self._session_open():
            return self._debug.symbol_value(symbol)
        tool = "debug_symbol_value"
        if not self.config.probe_allowed():
            return self._permission_denied(tool, "Symbol reads require allow_probe in the authoritative config.", self._permission_key("allow_probe"))
        validated = validate_debug_symbol(self.config, self.backend_name, tool, symbol)
        if not validated["ok"]:
            return validated
        resolved = resolve_symbol_offline(self.config, self.backend_name, tool, symbol, symbol_elf)
        if not resolved["ok"]:
            return resolved
        size_bytes = int(resolved["size_bytes"])
        if size_bytes > self.config.debug.max_dump_size_bytes:
            return {"ok": False, "tool": tool, "backend": self.backend_name, "error_type": "permission_denied", "summary": "Symbol read exceeds debug.max_dump_size_bytes.", "symbol": symbol, "size_bytes": size_bytes, "max_dump_size_bytes": self.config.debug.max_dump_size_bytes}
        address_value = int(resolved["address_value"])
        try:
            # Private, and outside the workspace on purpose: the caller asked for
            # bytes and named no path, so this file is not theirs to find, to
            # collide with or to have left behind. The same reasoning, and the
            # same removal, as the staged ELF the resolution above copies.
            staging_dir = Path(tempfile.mkdtemp(prefix="agentic-hil-symbol-value-"))
        except OSError as error:
            # Assembled here rather than classified out of a run, so the
            # catalogue is asked here too (#521): this refusal never reached the
            # CLI, and without the merge it would be the one shape of the bucket
            # an entry for this backend could not reach.
            return {"ok": False, "tool": tool, "backend": self.backend_name, "error_type": "memory_read_failed", "summary": "The private file this read needs could not be created.", "backend_error": str(error), "symbol": symbol, **NOT_CONTACTED, **remediation_fields("memory_read_failed", self.backend_name)}
        try:
            # `.hex` because the CLI picks Intel HEX off the extension, which is
            # the same thing that makes the dump's output what it promises.
            memory_path = staging_dir / "symbol-value.hex"
            result = self._run_stlink(tool, [*self._connection_args(STLINK_READ_CONNECT_MODE), "-r", hex(address_value), str(size_bytes), str(memory_path)])
            result.update({"symbol": symbol, "address": resolved["address"], "size_bytes": size_bytes, "resolved_from": resolved["resolved_from"], "symbol_source": resolved["symbol_source"]})
            if not result.get("ok") and "side_effect_status" not in result:
                # The dump's answer to the same question, for the same reason: a
                # read drives no write, but the probe was attached to a live core
                # and a CLI that exited without its confirmation line left no
                # evidence of where it stopped.
                result.update({"side_effect_status": "unknown", "retry_safe": False})
            if result.get("ok"):
                # The CLI confirmed the read, so the bytes left the target. What
                # landed in the file is the separate question, and it is asked
                # against the window that was requested rather than against the
                # file's own idea of what it holds.
                data = read_intel_hex_file(memory_path, address_value, size_bytes)
                if data is None:
                    # The run was a success until this line turned it into a
                    # failed read, so nothing classified it and no bucket was
                    # looked up for it. The catalogue is asked here, where the
                    # verdict is made (#521).
                    result.update({"ok": False, "error_type": "memory_read_failed", "summary": "STM32CubeProgrammer confirmed the read but left no parseable Intel HEX covering the requested bytes.", "target_contacted": True, **remediation_fields("memory_read_failed", self.backend_name)})
                    result.pop("success_confirmed", None)
                    return self._write_action_report(result)
                result.update(decode_symbol_value(data, resolved["image_byte_order"]))
                result["summary"] = "Symbol value read from target memory."
            return self._write_action_report(result)
        finally:
            shutil.rmtree(staging_dir, ignore_errors=True)

    def debug_dump_symbol_ihex(self, symbol: str = "", output: JsonObject | None = None, symbol_elf: JsonObject | None = None) -> JsonObject:
        """Read one allowed symbol out of target memory and write Intel HEX.

        The first typed-debug tool this backend served, because it is what
        STM32CubeProgrammer can do without a GDB session: `-r <address> <size>
        <file.hex>` uploads device memory and picks Intel HEX off the file
        extension. Everything a caller can observe is the OpenOCD path's:
        the same arguments, the same allowlist and `debug.max_dump_size_bytes`
        refusals word for word, the same success fields, so a plan that dumps a
        symbol does not have to know which probe it is running on. What differs
        is where the symbol table comes from, and that difference is stated
        rather than hidden: with no session there is no loaded image to ask, so
        the answer is resolved offline from the ELF this service flashed, whose
        sha256 travels in the result.

        Inside a debug session the dump goes through the session (#624), for
        the reason the value read does.
        """
        if self._session_open() and output is not None:
            return self._debug.dump_symbol_ihex(symbol, output)
        tool = "debug_dump_symbol_ihex"
        if not self.config.probe_allowed():
            return self._permission_denied(tool, "Symbol dumps require allow_probe in the authoritative config.", self._permission_key("allow_probe"))
        validated = validate_debug_symbol(self.config, self.backend_name, tool, symbol)
        if not validated["ok"]:
            return validated
        resolved = resolve_symbol_offline(self.config, self.backend_name, tool, symbol, symbol_elf)
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
            # parent, so the directory is made here, through the same guarded
            # helper the OpenOCD path uses, so a dump cannot mkdir its way out
            # of the workspace.
            safe_configured_directory(self.config, str(output_path.parent), f"{tool}.output_path")
        except (ConfigError, OSError) as error:
            return {"ok": False, "tool": tool, "backend": self.backend_name, "error_type": "output_write_failed", "summary": "Intel HEX output directory could not be prepared.", "backend_error": str(error), "symbol": symbol, **NOT_CONTACTED}
        address_value = int(resolved["address_value"])
        result = self._run_stlink(tool, [*self._connection_args(STLINK_READ_CONNECT_MODE), "-r", hex(address_value), str(size_bytes), str(output_path)])
        result.update({"symbol": symbol, "address": resolved["address"], "size_bytes": size_bytes, "resolved_from": resolved["resolved_from"], "output": output, "symbol_source": resolved["symbol_source"]})
        if not result.get("ok") and "side_effect_status" not in result:
            # The reset family's answer to the same question. A read drives no
            # write, but it is still a probe attached to a live core: an
            # STM32CubeProgrammer that exited without printing "Data read
            # successfully" leaves no evidence of where it stopped, and a
            # backend that called that harmless would be inventing the one
            # claim it does not have. Branches that *did* prove their abort
            # point ("no ST-LINK detected") arrive carrying not_started and
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
        self._debug.close()

    def sessionless_debug_tools(self) -> frozenset[str]:
        """The two reads this backend serves with no debug session behind them.

        `-r` attaches to a live core the same way a reset does, so a symbol read
        here is a standalone hardware interaction, not a call inside a session
        that already holds the lease. The coordination layer takes a one-shot
        debugger lease for exactly these (machine-wide ownership, run
        declaration and the incident path for an unconfirmed read), where a
        session backend's own lease would carry them instead.

        None while a session is open: the reads then run through it, on the
        lease it holds."""
        if self._session_open():
            return frozenset()
        return SESSIONLESS_DEBUG_READS

    def opens_debug_sessions(self) -> bool:
        """When the entry has ST-LINK_gdbserver to run them on (#624).

        `debuggers.<name>.gdb_server_executable`, as configured or as found
        beside the configured CLI when the configuration loaded. Without one
        the session tools refuse and name both ways to get one."""
        return self.config.debugger is not None and self.config.debugger.gdb_server_executable is not None

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
        if completed.not_executable:
            # Before the log is written, exactly where the missing-file branch
            # returns: no process ran, so there is no transcript to record.
            return {
                "tool": tool,
                "backend": self.backend_name,
                "started_at": started_at,
                **not_executable_refusal(self.backend_name, str(resolved["executable_path"]), completed),
                "finished_at": finished_at,
                "elapsed_ms": elapsed_ms,
            }
        audit_error = self._write_log(log_path, args, completed.stdout, completed.stderr, completed.returncode, completed.timed_out)
        if completed.timed_out:
            return self._finish_log_audit({"ok": False, "tool": tool, "backend": self.backend_name, "started_at": started_at, "finished_at": finished_at, "elapsed_ms": elapsed_ms, "error_type": "timeout", "summary": "Debugger command timed out.", "likely_causes": self._likely_causes("timeout"), "log_path": display_path(self.config, log_path)}, audit_error)
        output = f"{completed.stdout}{completed.stderr}"
        if completed.returncode == 0:
            backend_error_type = self._backend_error_from_output(output, tool)
            if backend_error_type is not None:
                return self._finish_log_audit(self._failure_result(tool, started_at, finished_at, elapsed_ms, backend_error_type, log_path, completed), audit_error)
            confirmation = self._confirm_operation_success(output, STLINK_SUCCESS_CONFIRMATION.get(tool, []) if success_text is None else success_text)
            if not confirmation["confirmed"]:
                # Confirmation is all-or-nothing here, so this branch is also
                # reached with some of the expected lines present: a read that
                # printed the ST-Link serial and no device name. The lines that
                # did match travel with the ones that were looked for, so the
                # result says how far the CLI got rather than leaving a caller
                # to read the worst case out of the error_type.
                return self._finish_log_audit(self._failure_result(tool, started_at, finished_at, elapsed_ms, self._unconfirmed_backend_error_type(tool), log_path, completed, {"confirmed": False, "expected_success_text": confirmation["expected"], "matched_success_text": confirmation["matched"]}), audit_error)
            return self._finish_log_audit({"ok": True, "tool": tool, "backend": self.backend_name, "started_at": started_at, "finished_at": finished_at, "elapsed_ms": elapsed_ms, "success_confirmed": True, "operation_result": {"confirmed": True, "matched_success_text": confirmation["matched"]}, "summary": "STM32CubeProgrammer CLI command completed successfully.", "log_path": display_path(self.config, log_path)}, audit_error)
        return self._finish_log_audit(self._failure_result(tool, started_at, finished_at, elapsed_ms, self._classify_output(output, tool), log_path, completed), audit_error)

    def _connection_args(self, mode: Literal["HOTPLUG", "NORMAL", "UR"]) -> list[str]:
        args = ["-c", f"port={self.config.debugger.interface}", f"mode={mode}"]
        if self.config.debugger.probe_id is not None:
            args.append(f"sn={self.config.debugger.probe_id}")
        return args

    def _flash_connect_mode(self) -> Literal["HOTPLUG", "UR"]:
        """The connect mode the flash command uses, from the configuration.

        The one call that reads `connect_mode`, and it reads it because it is the
        one whose failure the setting exists for: a core executing from flash can
        defeat the erase a hot-plug connect leaves it running through, and holding
        the target in reset for the connect is the documented remedy. Halting
        first is not one on this interface: the CLI reboots the target on every
        invocation, so a halt from a previous call has not survived into this one.

        `probe_target` deliberately keeps connecting hot plug whatever this says.
        It is the least intrusive call this backend makes and has to stay that
        way, and it erases nothing, so it has no failure to fix. `reset_target`
        keeps its own NORMAL connect and the memory reads their own HOTPLUG one,
        for the same kind of reason: what they do to the target is decided by the
        operation. A read in particular must never connect under reset, whatever
        the flash needs: `UR` holds the target in reset, and the RAM a read was
        asked for would be gone before it was read.
        """
        return STLINK_CONNECT_MODES[self.config.debugger.connect_mode]  # type: ignore[return-value]

    # STM32CubeProgrammer's own report that the transport never existed, for any
    # tool whose transcript does not also place a flash phase (the qualification
    # `_proves_no_contact` applies below), plus (for the reads, whose command
    # drives nothing of its own) its report that a probe was opened and nothing
    # answered behind it. Both are read off the CLI's output, so each is a
    # positive statement rather than the absence of one; `probe_unconfirmed` is
    # not, and stays out.
    PRE_CONTACT_BACKEND_ERRORS = frozenset({"probe_not_found"})
    READ_ONLY_PRE_CONTACT_BACKEND_ERRORS = frozenset({"target_not_detected"})

    def _proves_no_contact(self, tool: str, backend_error_type: str, output: str) -> bool:
        if backend_error_type in self.PRE_CONTACT_BACKEND_ERRORS:
            # The bucket alone is not a pre-contact claim for a tool whose own
            # command drives the target, because two different kinds of line land
            # in it. One is the prose the CLI prints while it is looking for a
            # probe ("No ST-Link detected!", "no debug probe"), measured with
            # nothing on USB, and a discovery failure by construction. The other
            # is `ST-LINK error (DEV_NO_STLINK)`, which is not prose about a
            # phase at all: it is the ST-Link driver library's code for "no
            # device behind this handle". Measured only in that same probe-free
            # run, where the handle never opened; whether the library also
            # returns it for a handle that opened and then lost its device is not
            # measured here, because no checked-in transcript carries it beside a
            # flash-phase line.
            #
            # So the decision is taken from the transcript, with
            # STLINK_FLASH_CHANGE_MARKERS, the set this file already uses for
            # exactly this question: those lines are printed only after the phase
            # they name has started, so one of them is positive evidence that a
            # probe was there and was writing. Declining the no-contact claim
            # when one is present is the conservative reading, and it is
            # deliberately the assumed half rather than the measured one, because
            # the two ways of being wrong are not the same size. Declining it for
            # a run whose probe really was absent all along costs a refusal that
            # is reported as an unconfirmed flash: an operator is sent to look at
            # a board that is fine, and the retry they wanted needs the incident
            # to stand down first. Granting it for a probe that dropped off USB
            # mid-download publishes `target_contacted: false`,
            # `side_effect_status: not_started`, `hardware_state: unchanged` and
            # `retry_safe: true`, and opens no incident, for a board holding
            # neither the old image nor the new one, and the next caller boots or
            # trusts that flash on this layer's word.
            #
            # The reads keep the claim unconditional, for the reason the branch
            # below exists: a `-r` read drives nothing of its own and cannot
            # leave flash half changed, so a progress bar printed by its own
            # upload must not be read as a flash change and quarantine a call
            # that provably altered nothing.
            if tool in (READ_ONLY_TOOLS | SESSIONLESS_DEBUG_READS):
                return True
            return not contains_any(output.lower(), STLINK_FLASH_CHANGE_MARKERS)
        # SESSIONLESS_DEBUG_READS beside the older READ_ONLY_TOOLS, for the
        # reason the pyOCD backend carries them: a `-r` read drives nothing of
        # its own, so "No STM32 target found" behind an opened probe is its
        # report that the transport reached no core, exactly as it is for a
        # probe listing. Without them here a read that connected to nothing
        # would be quarantined as an unknown effect rather than released as the
        # retry-safe refusal the CLI's own words prove it is.
        return tool in (READ_ONLY_TOOLS | SESSIONLESS_DEBUG_READS) and backend_error_type in self.READ_ONLY_PRE_CONTACT_BACKEND_ERRORS

    def _failure_result(self, tool: str, started_at: str, finished_at: str, elapsed_ms: int, backend_error_type: str, log_path: str, completed: CompletedCommand, operation_result: JsonObject | None = None) -> JsonObject:
        # likely_causes says what may be wrong; remediation says what to check
        # next, scoped to this backend, because the checks differ per tool: an
        # ST-Link transport is chosen with `interface`, an OpenOCD one with
        # interface_cfg. Same catalogue the MCP reference serves.
        error_type = self._public_error_type(backend_error_type)
        # The whole transcript, which two of the readings below take: the erase
        # abort point and the no-contact claim both answer their question out of
        # what the CLI printed, and both have to see every stream it printed on.
        output = f"{completed.stdout}{completed.stderr}"
        # `programmer_output` on every classified failure, not only on the erase
        # refusal that first carried it (#334). Whatever this run failed at, the
        # CLI wrote a line about it, and that line is what the classification,
        # the summary and the causes were all read out of.
        result = {"ok": False, "tool": tool, "backend": self.backend_name, "started_at": started_at, "finished_at": finished_at, "elapsed_ms": elapsed_ms, "error_type": error_type, "backend_error_type": backend_error_type, "summary": self._summary_for_error(error_type), "likely_causes": self._likely_causes(error_type), **remediation_fields(error_type, self.backend_name), "log_path": display_path(self.config, log_path), **programmer_output_fields(completed)}
        if operation_result is not None:
            result["operation_result"] = operation_result
        if backend_error_type == "flash_erase_failed":
            # The one failure that reads its own transcript for more than the
            # classification. `erase_abort_point` is carried for the operator and
            # not to relax the quarantine: a refused erase leaves flash
            # unconfirmed, so no reading attaches a `hardware_state`/`retry_safe`
            # verdict and the coordination layer quarantines the incident as it
            # does any other unconfirmed effect.
            abort_point = erase_abort_point(output)
            result["erase_abort_point"] = abort_point
            # The generic `_summary_for_error` string cannot say more than "could
            # not erase", but the transcript reading can, and one of its readings
            # is a segment that was already written -- a summary that still said
            # "the firmware was not written" there would contradict the result's
            # own `flash_change_underway` evidence.
            result["summary"] = self._erase_failure_summary(abort_point["reading"])
        if self._proves_no_contact(tool, backend_error_type, output):
            # The ST-Link probe is the only transport STM32CubeProgrammer has
            # to the target, and "no ST-LINK detected" is its report that the
            # transport never existed for this run; "No STM32 target found" and
            # its siblings are the same report one step further out, with the
            # probe named and nothing behind it. A failed call over a channel
            # that never carried anything must refuse, not quarantine. Both are
            # the CLI's own words, read out of its output by
            # `_classify_output`; `probe_unconfirmed` (an exit status of 0
            # whose output is missing at least one line that confirms the
            # connection) deliberately is not, because it names no abort point
            # at all, whether it printed some of them or none. The transcript
            # goes with the pair because neither report is a claim about *when*
            # it was printed: see `_proves_no_contact` for the flash phase that
            # takes the claim back.
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
        return {"probe_target": "probe_unconfirmed", "flash_firmware": "flash_unconfirmed", "reset_target": "reset_unconfirmed", "debug_dump_symbol_ihex": "memory_read_unconfirmed", "debug_symbol_value": "memory_read_unconfirmed"}.get(tool, "unknown_debugger_error")

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

    def _permission_denied(self, tool: str, summary: str, permission: str | None = None) -> JsonObject:
        """A refusal one permission caused, carrying which one and what to do.

        `permission` is the dotted key the file uses and `agentic-hil grant`
        takes, built off the bound entry's own name. Named in the summary as
        well as carried as a field: an agent asked to report the permission it
        was denied reads the summary out (#443)."""
        result: JsonObject = {"ok": False, "tool": tool, "error_type": "permission_denied", "summary": summary}
        if permission:
            result["summary"] = permission_denied_summary(summary, permission)
            result.update(permission_denied_fields(permission))
            result.update(remediation_fields("permission_denied", permission=permission))
        return result

    def _permission_key(self, key: str) -> str:
        return permission_key("debuggers", self.config.debugger_id, key)

    def _exclusive_permission_denied(self, tool: str, action: str, blocking: str) -> JsonObject:
        """The other direction: a permission that is granted and blocks this.

        Its own helper because the advice is the opposite one. The unscoped
        `permission_denied` entry says the operator opens the key; here the key
        is already open and opening it is what an operator must not be sent to
        do, so the scoped entry travels on the result rather than being looked
        up later off an `error_type` the two cases share."""
        return {
            "ok": False,
            "tool": tool,
            "error_type": "permission_denied",
            "summary": exclusive_permission_summary(action, blocking, self.config.debugger_id),
            **exclusive_permission_fields(blocking, self.config.debugger_id),
        }

    def _unsupported_debug_tool(self, tool: str) -> JsonObject:
        return debug_session_unsupported(self.backend_name, tool)

    def _gdb_server_field(self) -> str:
        return f"debuggers.{self.config.debugger_id or '<name>'}.gdb_server_executable"

    def _resolve_debug_server(self) -> JsonObject:
        """The ST-LINK_gdbserver a debug session starts, or the refusal that starts none.

        The CLI is resolved first, exactly as every other call resolves it,
        because the server is told its directory as `-cp` and refuses to start
        without an STM32CubeProgrammer there (recorded). The directory is the
        resolved file's, so a CLI reached through a link names the installation
        it is part of."""
        resolved = self._resolve_executable()
        if not resolved["ok"]:
            return resolved
        server = self.config.debugger.gdb_server_executable
        if server is None:
            return self._unsupported_debug_tool("debug_start_session")
        if not Path(server).is_file():
            field = self._gdb_server_field()
            return {
                "ok": False,
                "backend": self.backend_name,
                "error_type": "debugger_not_found",
                "backend_error_type": "gdb_server_not_found",
                "summary": f"ST-LINK_gdbserver could not be found at {server}, the path {field} resolved to when the configuration loaded.",
                "field": field,
                "likely_causes": ["STM32CubeCLT was removed or moved after the configuration was loaded", f"{field} names a file that has since been deleted"],
                **NOT_CONTACTED,
            }
        self._programmer_directory = str(Path(str(resolved["executable_path"])).resolve().parent)
        return {"ok": True, "executable": server, "executable_path": server}

    def _debug_server_args(self, executable_path: str, gdb_port: int, reset: bool) -> list[str]:
        """ST-LINK_gdbserver on the reserved port, the entry's probe, attached.

        The same for every mode: the reset into halt is a GDB command here
        (`ST_LINK_GDB_SERVER_STEPS`), so `-g` connects without one, as an
        attach must. `-d` for an SWD entry: without it the server connects over
        JTAG, which the reference board's SWD-only wiring refused as an unknown
        MCU. Never `-e`, which keeps the server up after its client leaves.
        `-t` last, where the session reaches the probe through stlink-server."""
        args = [*invocation(executable_path), "-p", str(gdb_port)]
        if self.config.debugger.interface.upper() == "SWD":
            args.append("-d")
        args.extend(["-cp", str(self._programmer_directory)])
        if self.config.debugger.probe_id is not None:
            args.extend(["-i", self.config.debugger.probe_id])
        args.append("-g")
        if self._shared_probe:
            args.append("-t")
        return args

    def _start_probe_server(self, timeout_s: float) -> ServerCompanion:
        """How the session's GDB server reaches the probe: through stlink-server where it can, itself otherwise.

        One already listening is shared and left running: another tool's session
        may be using it. Otherwise the stlink-server on PATH is started for this
        session, which ends it once the GDB server is gone. Without one, or when
        the one found ends before it listens, the GDB server opens the probe's
        USB itself, and the record says so with what that risks at the stop."""
        port = STLINK_SERVER_PORT
        self._shared_probe = False
        if stlink_server_listening(port):
            self._shared_probe = True
            return ServerCompanion(None, {"mode": "shared", "started_by_session": False, "port": port, "ended": False})
        executable = find_stlink_server()
        if executable is None:
            return ServerCompanion(None, {"mode": "direct", "reason": "No stlink-server was found on PATH, so ST-LINK_gdbserver opens the probe's USB itself.", "stop_risk": DIRECT_PROBE_STOP_RISK})
        log_path = Path(logs_directory(self.config)) / f"stlink-server-{timestamp_for_filename()}.log"
        began = time.monotonic()
        try:
            with open(log_path, "wb") as log:
                process = spawn_managed_process([*invocation(executable)], cwd=str(Path(executable).parent), stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
        except OSError as error:
            return ServerCompanion(None, {"mode": "direct", "reason": f"stlink-server could not be started ({type(error).__name__}: {error}), so ST-LINK_gdbserver opens the probe's USB itself.", "executable": executable, "stop_risk": DIRECT_PROBE_STOP_RISK})
        deadline = began + max(0.0, min(timeout_s, STLINK_SERVER_LISTEN_TIMEOUT_S))
        while True:
            if stlink_server_listening(port):
                self._shared_probe = True
                return ServerCompanion(
                    process,
                    {
                        "mode": "shared",
                        "started_by_session": True,
                        "port": port,
                        "executable": executable,
                        "listening_after_ms": int((time.monotonic() - began) * 1000),
                        "log_path": display_path(self.config, str(log_path)),
                        "ended": False,
                    },
                )
            if process.poll() is not None or time.monotonic() >= deadline:
                break
            time.sleep(0.05)
        exited = process.poll() is not None
        if not exited:
            terminate_process_tree(process, STLINK_SERVER_LISTEN_TIMEOUT_S)
        reason = (
            f"stlink-server exited with status {process.returncode} before it listened on port {port}"
            if exited
            else f"stlink-server did not listen on port {port} within {int(min(timeout_s, STLINK_SERVER_LISTEN_TIMEOUT_S))} s and was ended"
        )
        return ServerCompanion(
            None,
            {
                "mode": "direct",
                "reason": f"{reason}, so ST-LINK_gdbserver opens the probe's USB itself.",
                "executable": executable,
                "returncode": process.returncode,
                "output_tail": output_tail(log_path),
                "log_path": display_path(self.config, str(log_path)),
                "stop_risk": DIRECT_PROBE_STOP_RISK,
            },
        )

    def _debug_start_failure(self, output: str, server_args: list[str]) -> JsonObject | None:
        """What a server that exited at start said, in its own words.

        The line that decided it travels as `backend_error`: the server prints a
        banner and its options ahead of the reason. A missing probe and a
        missing STM32CubeProgrammer end it before any probe carried anything,
        which is what NOT_CONTACTED says; the others make no such claim."""
        backend_error_type = classify_gdb_server_output(output)
        error_type = self._public_error_type(backend_error_type)
        result: JsonObject = {
            "error_type": error_type,
            "backend_error_type": backend_error_type,
            "summary": f"Debug server exited before the GDB port became ready: {self._gdb_server_summary(backend_error_type, error_type)}",
            "likely_causes": self._gdb_server_likely_causes(backend_error_type, error_type),
            **remediation_fields(error_type, self.backend_name),
        }
        decisive = gdb_server_decisive_line(output, backend_error_type)
        if decisive is not None:
            result["backend_error"] = decisive
        if backend_error_type in ST_LINK_GDB_SERVER_PRE_CONTACT:
            result.update(NOT_CONTACTED)
        return result

    def _gdb_server_summary(self, backend_error_type: str, error_type: str) -> str:
        if backend_error_type == "stm32_programmer_cli_not_found":
            return "ST-LINK_gdbserver found no STM32CubeProgrammer in the directory of the configured STM32_Programmer_CLI."
        return self._summary_for_error(error_type)

    def _gdb_server_likely_causes(self, backend_error_type: str, error_type: str) -> list[str]:
        if backend_error_type == "stm32_programmer_cli_not_found":
            return [
                "debuggers.<name>.executable is a wrapper or a copy rather than the STM32_Programmer_CLI inside an STM32CubeProgrammer bin directory",
                "the STM32CubeProgrammer installation the CLI belongs to is incomplete",
            ]
        return self._likely_causes(error_type)

    def _classify_output(self, output: str, tool: str | None = None) -> str:
        lower = output.lower()
        if "st-link error (dev_no_stlink)" in lower:
            return "probe_not_found"
        # A probe that enumerates and whose USB link refused the CLI, recorded
        # after a killed ST-LINK_gdbserver had held it (the direct stop round of
        # st_link_gdbserver_7_14_0_linux_session_stops_recordings.json).
        if "st-link error (dev_usb_comm_err)" in lower:
            return "adapter_usb_error"
        if contains_any(lower, ["no st-link", "no stlink", "st-link not found", "stlink not found", "no debug probe"]):
            return "probe_not_found"
        if contains_any(lower, ["no stm32 target found", "cannot connect to target", "can not connect to target", "failed to connect"]):
            return "target_not_detected"
        if contains_any(lower, ["no device found", "device not found", "unable to connect"]):
            return "target_not_detected"
        # Ahead of the verify and reset rules, because it is more specific than
        # either and both would swallow it. The two below match on one word plus
        # any failure word anywhere in the transcript; this one matches the line
        # the programmer wrote about the operation that actually stopped, and a
        # refused erase is its own failure with its own causes and its own fix.
        if contains_any(lower, STLINK_ERASE_REFUSAL_MARKERS):
            return "flash_erase_failed"
        if "verify" in lower and contains_any(lower, ["failed", "mismatch", "error"]):
            return "verify_failed"
        # Above the reset rule, where it used to sit below it. These phrases name
        # the operation the programmer said failed; the reset rule below reads
        # only the line that reports a failure, so the connect banner's `Reset
        # mode  : Software reset` no longer answers for a failed download (#333).
        if tool == "flash_firmware" and contains_any(lower, ["download failed", "write failed", "failed to download"]):
            return "flash_failed"
        if reports_reset_failure(output):
            return "reset_failed"
        if contains_any(lower, ["can't find", "couldn't find", "couldn't open", "not found"]):
            return "config_file_not_found"
        if tool == "flash_firmware" and contains_any(lower, FAILURE_WORDS):
            return "flash_failed"
        # The twin of the flash bucket above, anchored on the operation rather
        # than on a word: when the tool is `reset_target`, the operation that
        # reported a failure is a reset, whatever words this CLI version used for
        # it. That is what keeps a genuine reset failure classified without the
        # rule above having to guess from a stray "reset" somewhere in a
        # transcript, which is what it used to do (#333).
        if tool == "reset_target" and contains_any(lower, FAILURE_WORDS):
            return "reset_failed"
        return "unknown_debugger_error"

    def _public_error_type(self, backend_error_type: str) -> str:
        return BACKEND_ERROR_TO_PUBLIC_ERROR.get(backend_error_type, backend_error_type)

    def _summary_for_error(self, error_type: str) -> str:
        return {"debugger_not_found": "Debugger executable could not be found.", "adapter_not_found": "Debugger adapter could not be found or opened.", "adapter_usb_error": "The in-circuit debugger or programmer enumerates, but its USB link refused communication.", "target_not_detected": "Debugger could not detect the target.", "target_state_unconfirmed": "STM32CubeProgrammer exited without confirming the operation, so the target's state is unknown.", "flash_failed": "Debugger failed to flash the firmware.", "flash_erase_failed": "STM32CubeProgrammer could not erase the target's flash, so its contents are unconfirmed.", "verify_failed": "Debugger failed to verify the flashed firmware.", "reset_failed": "Debugger failed to reset the target.", "memory_read_failed": "Debugger failed to read the requested target memory.", "timeout": "Debugger command timed out.", "config_file_not_found": "Debugger input file could not be found.", "debugger_error": "Debugger failed with an unknown error."}.get(error_type, "Debugger failed with an unknown error.")

    def _erase_failure_summary(self, reading: str) -> str:
        """The erase-failure summary the transcript reading actually supports.

        A single generic line for `flash_erase_failed` had to claim "the firmware
        was not written", which is false for a transcript that reports a segment
        downloaded before a later segment's erase was refused. Each reading says
        only what its own evidence carries, and none of them says the flash is
        unchanged, because a refused erase does not establish that.
        """
        return {
            FLASH_CHANGE_UNDERWAY: "STM32CubeProgrammer could not erase the target's flash after a write phase had begun, so the flash holds neither the old image nor the new one and its contents are unconfirmed.",
            ERASE_REFUSED_EFFECT_UNCONFIRMED: "STM32CubeProgrammer refused to erase the target's flash; its output reports no completed write, but whether any sector was erased before the refusal is not established, so the flash contents are unconfirmed.",
            ERASE_ABORT_POINT_UNREADABLE: "STM32CubeProgrammer could not erase the target's flash and its output does not establish how far it got, so the flash contents are unconfirmed.",
        }.get(reading, self._summary_for_error("flash_erase_failed"))

    def _likely_causes(self, error_type: str) -> list[str]:
        return {"target_not_detected": ["DUT is not powered", "wrong SWD/JTAG interface selection", "SWD/JTAG wiring issue", "debug probe already in use"], "target_state_unconfirmed": ["STM32CubeProgrammer exited successfully without printing every line that confirms the operation; operation_result names which of them did print","debuggers.<name>.executable is a wrapper that discards the CLI's output", "this STM32CubeProgrammer version words its confirmation differently"], "adapter_usb_error": ["an ST-LINK_gdbserver killed while it held the probe's USB itself left it refusing (recorded after session stops without stlink-server)", "the USB link to the probe failed"], "adapter_not_found": ["debug probe is not connected", "debuggers.<name>.probe_id does not match a connected ST-Link serial number", "debug probe driver is missing", "debug probe is already in use"], "verify_failed": ["flash write did not persist correctly", "firmware image does not match target memory layout"], "flash_failed": ["target flash is locked", "firmware image is invalid for this target", "debuggers.<name>.flash_address is wrong"], "flash_erase_failed": ["the core was running from flash when the programmer connected under hot plug, so it defeated the erase; an immediate retry usually succeeds", "the sectors this image covers are protected (write protection, PCROP, or a read-out protection level that refuses the erase)", "an earlier flash operation had not finished and left the flash controller busy"], "reset_failed": ["reset line wiring issue", "target is not responding"], "memory_read_failed": ["STM32CubeProgrammer exited without printing 'Data read successfully', so the read is unconfirmed", "the symbol's address is not readable memory on this target", "debug probe or target stopped responding mid-read"], "timeout": ["debugger stopped responding", "debug probe or target is stuck", "timeout_s is too low for this operation"], "debugger_not_found": ["debuggers.<name>.executable is not configured", "STM32CubeProgrammer is not installed", "STM32_Programmer_CLI executable is not in PATH"], "config_file_not_found": ["firmware artifact path is missing", "STM32CubeProgrammer CLI path is incomplete"]}.get(error_type, ["inspect the debugger log for details"])


def erase_abort_point(output: str) -> JsonObject:
    """How far a refused erase can be placed, read off the programmer's transcript.

    This reads the transcript for the operator, not to clear the incident. A
    refused erase leaves flash unconfirmed regardless of the reading: the
    transcript can show that a write phase had begun, but its silence about one
    is not proof that no sector was erased. STM32CubeProgrammer's progress and
    download lines are not guaranteed across versions, quiet or piped output and
    abort paths, and `failed to erase memory` also covers a protection refusal
    that can strike after unprotected sectors in the same range have already been
    erased. An earlier revision (#328) relaxed the quarantine on the refused
    reading; it no longer does, because "no progress line was captured" cannot
    carry `hardware_state: unchanged` and `retry_safe: true` for a downstream
    reader to trust.

    Three readings, all of which keep the quarantine and differ only in how far
    the transcript accounts for the failure:

    * ``erase_refused_effect_unconfirmed`` -- the erase refusal is in the
      transcript and no line in it reports a write phase completing. The
      programmer announced an erase and was refused, but whether any sector was
      erased before the refusal is not established, so the flash contents are
      unconfirmed.
    * ``flash_change_underway`` -- a line says a phase had started before the
      failure hit. Partially erased or partially written flash is precisely what
      the quarantine exists for.
    * ``abort_point_unreadable`` -- the transcript will not answer the question
      at all, not even to place the refusal line.

    Every reading names the line it read, so an operator can disagree with
    evidence rather than with a feeling. The unreadable one names the last line
    the programmer printed, because "this is as far as the transcript goes and
    it does not say" is the evidence in that case.
    """
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    change = next((line for line in reversed(lines) if contains_any(line.lower(), STLINK_FLASH_CHANGE_MARKERS)), None)
    if change is not None:
        return {
            "reading": FLASH_CHANGE_UNDERWAY,
            "evidence_line": change,
            "evidence_rule": "STM32CubeProgrammer prints this line only after the phase it names has started, so flash was being changed when the failure hit and what it holds now is unknown.",
        }
    refusal = next((line for line in reversed(lines) if contains_any(line.lower(), STLINK_ERASE_REFUSAL_MARKERS)), None)
    if refusal is None:
        return {
            "reading": ERASE_ABORT_POINT_UNREADABLE,
            "evidence_line": lines[-1] if lines else "",
            "evidence_rule": "The transcript carries no line placing the failure before or after the first flash operation, so where it stopped is not established and the quarantine stands.",
        }
    return {
        "reading": ERASE_REFUSED_EFFECT_UNCONFIRMED,
        "evidence_line": refusal,
        "evidence_rule": "The erase was refused and no line reports a write phase completing, but the transcript does not establish that no sector was erased before the refusal, so the flash contents are unconfirmed and the quarantine stands.",
    }


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
