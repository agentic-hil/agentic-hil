from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from agentic_hil.backends.common import (
    FAILURE_WORDS,
    NOT_CONTACTED,
    READ_ONLY_TOOLS,
    CompletedCommand,
    command_for_log,
    contains_any,
    contains_failure_text,
    failure_text_lines,
    invocation,
    not_executable_refusal,
    programmer_output_fields,
    reports_reset_failure,
    spawn_command,
    which,
)
from agentic_hil.backends.gdbdebug import GdbDebugSessions, GdbServerSteps
from agentic_hil.comports import (
    DISCOVERED_BY_USB_INVENTORY,
    list_available_com_ports,
    usb_stlink_ports,
    usb_stlink_probe_ids,
)
from agentic_hil.config import ConfigError, display_path, resolve_work_path, safe_write_text
from agentic_hil.knowledge import (
    exclusive_permission_fields,
    exclusive_permission_summary,
    permission_denied_fields,
    permission_denied_summary,
    permission_key,
    remediation_fields,
)
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
    # The probe is attached and this user may not open it. The public name stays
    # the one that means "could not be found or opened", and the backend's own
    # type, the summary and the causes say it was the opening (#506).
    "adapter_access_denied": "adapter_not_found",
    # An exit of 0 with the tool's success marker missing from the output. This
    # branch read nothing out of OpenOCD's words, so it gets a public error_type
    # of its own rather than the classification the words would have produced:
    # `target_not_detected` is OpenOCD's report that it reached the adapter and
    # nothing answered (which the shipped catalogue entry says in as many words),
    # and publishing that here would make the abort-point claim this branch
    # exists to withhold. See READ_ONLY_PRE_CONTACT_BACKEND_ERRORS.
    "probe_unconfirmed": "target_state_unconfirmed",
    "flash_unconfirmed": "flash_failed",
    "reset_unconfirmed": "reset_failed",
    # The debugger failed and its output says no more than that. `debugger_error`
    # is the name the debug-session path has always published for exactly that
    # (`_start_failure` in gdbdebug), and a caller that reads one word for it in
    # a failed session and another in a failed command has to learn two. The
    # backend's own `unknown_debugger_error` travels beside it in
    # `backend_error_type` for a reader at that layer (#506).
    "unknown_debugger_error": "debugger_error",
}

# OpenOCD's own words for an erase it could not carry out: `flash_erase_address`
# and the `flash erase_sector` handler both end in
# `failed erasing sectors %u to %u` (src/flash/nor/tcl.c), which is the phrase a
# refused or failed erase prints whichever of them the `program` proc reached.
# One measured phrase rather than a family of guessed ones, the way the ST-Link
# backend's marker list is: a marker nobody has seen a tool print would classify
# on a hope, and the broad flash bucket below is the honest answer until somebody
# measures the next wording.
OPENOCD_ERASE_FAILURE_MARKERS = ["failed erasing sectors"]

# Which configured field an unfindable script came from. Both buckets publish
# the same `debugger_config_not_found`, and the generic summary for it says only
# that a configuration file could not be found; an operator reading that for a
# missing target script goes and checks `interface_cfg`, which is the one key
# that is right. The classification already knows which field's value OpenOCD
# named, so the summary says it (#506).
OPENOCD_CONFIG_FIELD_BY_BACKEND_ERROR = {
    "interface_config_not_found": "interface_cfg",
    "target_config_not_found": "target_cfg",
}

# What OpenOCD 0.12.0 printed on the bench with the probe attached and the group
# that owns its USB device withheld from the user (2026-09-27):
# `Error: libusb_open() failed with LIBUSB_ERROR_ACCESS`, and then the same
# `Error: open failed` it prints with nothing on USB. libusb enumerated the probe
# and the device node's mode refused opening it. Read only off Windows: there
# libusb can give the same error for a device another program holds, which the
# causes `adapter_not_found` already carries cover, the way the serial path reads
# EACCES.
OPENOCD_ACCESS_REFUSED_MARKER = "libusb_error_access"

# Causes for a classification the public error_type is less specific than. The
# generic causes for `adapter_not_found` start with a probe that is not
# connected, which is the one thing this transcript rules out.
OPENOCD_CAUSES_BY_BACKEND_ERROR = {
    "adapter_access_denied": [
        "this user may not open the probe's USB device: on Linux add the user to the group the probe's udev rule gives it to (plugdev on Debian and Ubuntu) and log in again",
        "no udev rule for this probe is installed, or its rule or the device node's mode denies this user (ls -l /dev/bus/usb/<bus>/<device>, with the numbers lsusb prints for the probe, shows its owner and group)",
    ],
}

OPENOCD_DISABLE_TCP_SERVER_COMMANDS = ["gdb_port disabled", "tcl_port disabled", "telnet_port disabled"]
# What OpenOCD logs once a debug session's GDB port listens: `listen()` returns
# and this line follows, naming the port and the service
# (openocd/src/server/server.c:286 and :297 in v0.12.0; the same words since
# 0.11.0, which is why debug sessions need 0.11.0 or newer). A session start
# takes the port as ready on this line, for the port it reserved, and opens no
# connection of its own: OpenOCD runs its per-connection setup for every
# connection it accepts, and logs one that never sends GDB's acknowledgement as
# rejected (#586).
OPENOCD_GDB_LISTENING_LINE = "Listening on port {port} for gdb connections"
# OpenOCD's own answers to the session steps that are each server's (#624):
# the line above, `monitor reset halt` believed on its `^done`, and the guard
# that keeps the core halted once GDB lets go. OpenOCD's `gdb-detach` and
# `gdb-end` target events resume the core by default when the last GDB
# connection ends, so both are overridden to do nothing before GDB detaches
# (see `GdbDebugSessions._pin_no_resume_on_detach`). In a reset mode the server
# itself starts with `init; reset halt`.
OPENOCD_GDB_SERVER_STEPS = GdbServerSteps(
    ready_line=OPENOCD_GDB_LISTENING_LINE,
    detach_guard_command='-interpreter-exec console "monitor $_TARGETNAME configure -event gdb-detach {}; $_TARGETNAME configure -event gdb-end {}"',
    server_resets_at_start=True,
)
# Each of these is `echo`ed by the command string *after* the one command the
# tool exists for, and OpenOCD's interpreter stops evaluating a `-c` script at
# the first command that fails. So a marker in the output is OpenOCD's own
# statement that its operation returned success: `targets` after `init` opened
# the adapter and examined the core, `program ... verify` after the image was
# written and read back, `reset <mode>` after the core restarted. Nothing else
# is claimed by any of them, and none of them moves.
OPENOCD_SUCCESS_MARKERS = {
    "probe_target": "AGENTIC_HIL_RESULT:probe_target:ok",
    "flash_firmware": "AGENTIC_HIL_RESULT:flash_firmware:ok",
    "reset_target": "AGENTIC_HIL_RESULT:reset_target:ok",
}
# The classifications that outrank the marker, and the only ones (#425).
#
# `failed erasing sectors` is OpenOCD's own report that the operation it names
# did not happen, and what it leaves behind is a flash whose contents nobody can
# state. If a build ever logs it and still returns success out of `program`, a
# run reported as a success would be telling a caller the new image is on the
# board when it may not be, so this one keeps deciding. It costs nothing where it
# does not belong: it is one measured phrase rather than a word class, it cannot
# occur in a `reset_target` or `probe_target` transcript, and OpenOCD aborts the
# script when the erase really fails, so on the flash path it is a guard against
# a build nobody has measured rather than a rule that fires today.
#
# Nothing else is here, and the near miss says why. `verify_failed` matches the
# word "verify" anywhere in the transcript beside any failure word, and every
# verified flash prints it: OpenOCD's `program` proc announces the phase with
# `** Verify Started **`. Keeping that rule ahead of the marker would therefore
# read one incidental `Error:` line as a verify mismatch on every flash, which is
# #425 again one tool over. A verify that really fails raises out of `program`
# and takes the marker with it, while the CRC pass that logs `checksum mismatch -
# attempting binary compare` before a byte compare that then succeeds is exactly
# the line that has to stay a warning.
OPENOCD_BACKEND_ERRORS_OUTRANKING_THE_MARKER = frozenset({"flash_erase_failed"})
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
# OpenOCD 0.11's answer to a group asked for a subcommand it does not have, which
# names the words after the group: `invalid subcommand "serial <serial>"` for an
# `adapter serial <serial>`, recorded on Ubuntu 22.04's package
# (tests/fixtures/openocd_0_11_bench_recordings.json).
OPENOCD_UNKNOWN_SUBCOMMAND = re.compile(r'invalid subcommand "([^"]+)"', re.IGNORECASE)

# The release an OpenOCD says it is, in the banner every build prints first:
# `Open On-Chip Debugger 0.11.0` from Ubuntu 22.04's package, on stderr, and a
# suffix after the numbers from a development build. Read for one decision only:
# how the probe a configuration names by serial is selected.
OPENOCD_VERSION_BANNER = re.compile(r"Open On-Chip Debugger\s+v?((\d+)\.(\d+)(?:\.(\d+))?\S*)")
# The release `adapter serial` came with, the one selector every adapter driver
# takes. Every release before it selects a probe by serial through a command of
# the adapter driver's own.
OPENOCD_ADAPTER_SERIAL_SINCE = (0, 12, 0)
# Which adapter driver an interface script loads, asked the way the bench asked
# OpenOCD 0.11: `adapter name` names the driver, or answers `undefined` when the
# script loaded none, and it is echoed behind a marker of this backend's own.
# The run loads the interface script, answers and shuts down at the
# configuration stage, so no adapter is opened for it.
OPENOCD_ADAPTER_DRIVER_MARKER = "AGENTIC_HIL_ADAPTER_DRIVER:"
OPENOCD_ADAPTER_DRIVER_QUERY = f'echo "{OPENOCD_ADAPTER_DRIVER_MARKER}[adapter name]"'
# The selector each adapter driver takes a probe's serial with before 0.12, as
# OpenOCD 0.11 took the recorded serial with each (the `selector_*` recordings):
# `hla_serial` for the hla driver `interface/stlink.cfg` loads, `st-link serial`
# for the st-link driver of `interface/stlink-dap.cfg`, `cmsis_dap_serial` for
# cmsis-dap. `jlink serial` is not here, because it refused the recorded serial as
# not a number, and the other drivers have no selector at all.
#
# Each is a filter and not a preference: given a serial no attached probe carries,
# the selector makes 0.11 open nothing rather than fall back to the first probe it
# finds. Measured for `hla_serial` alone, by the one recording that reaches `init`
# with a serial nothing matches (`probe_target_hla_serial_no_such_probe`, where
# 0.11 answered "No device matches the serial string"). The three `selector_*`
# recordings stop at the configuration stage by construction, so they show the
# form of each selector being accepted and can say nothing about what a non-match
# does. For `st-link serial` and `cmsis_dap_serial` the filter behaviour is read
# off the 0.11 driver sources (stlink_dap_usb_open and cmsis_dap_usb_open both
# compare the serial and return ERROR_FAIL on no match) and not measured here.
# Closing that gap means one `init`-stage no-match run per driver on a bench that
# has those probes, in the shape of the hla recording.
OPENOCD_0_11_SERIAL_SELECTORS = {"hla": "hla_serial", "st-link": "st-link serial", "cmsis-dap": "cmsis_dap_serial"}
# How long a configuration-stage read may take: it loads one or two scripts and
# exits, and the debugger's own timeout is sized for flashing a board.
OPENOCD_CONFIGURATION_READ_TIMEOUT_S = 10.0

OpenOCDRelease = tuple[str, tuple[int, int, int]]

# The adapter scripts whose probes this host can enumerate without OpenOCD's
# help. OpenOCD ships one family of them under that prefix (`stlink.cfg`,
# `stlink-dap.cfg`, and the older per-generation names), and an ST-Link is the
# one adapter whose USB identity `usb_stlink_probe_ids` reads. Matched on the
# script's own name rather than on a list of releases, because the name is what
# the operator wrote and what OpenOCD resolves; an entry naming any other
# adapter has no enumeration behind it and is told so instead of being answered
# with an empty listing that would read as "no probe attached".
OPENOCD_USB_ENUMERATED_INTERFACE = "stlink"


def openocd_interface_enumerates_by_usb(interface_cfg: str) -> bool:
    """Whether this entry's adapter is one the USB serial inventory enumerates.

    The script may be an OpenOCD search name (`interface/stlink.cfg`) or an
    absolute path to a file of the operator's own, so only the final component
    is read, without its extension and case-insensitively. Both separators are
    split on: a Windows configuration writes forward slashes by convention and
    backslashes by accident, and the answer must not depend on which."""
    name = re.split(r"[\\/]", str(interface_cfg or "").strip())[-1].lower()
    stem = name[: -len(".cfg")] if name.endswith(".cfg") else name
    return stem.startswith(OPENOCD_USB_ENUMERATED_INTERFACE)


def parse_openocd_version(output: str) -> OpenOCDRelease | None:
    """The release OpenOCD's banner names, as written and as numbers, or None without a banner."""
    match = OPENOCD_VERSION_BANNER.search(output)
    if match is None:
        return None
    return match.group(1), (int(match.group(2)), int(match.group(3)), int(match.group(4) or 0))


def _last_written_line(output: str) -> str:
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    return lines[-1] if lines else ""


# What the adapter driver answers while the in-circuit debugger is still
# re-enumerating, as it is for a moment after an OpenOCD was killed in the middle
# of a flash (#621). Both lines are written while `init` opens the probe, before
# any target is addressed, so a call refused with one of them never reached the
# board.
OPENOCD_PROBE_REENUMERATING_LINES = (
    "Error: mode (transport) not supported by device",
    "Error: init mode failed (unable to connect to the target)",
)


def openocd_probe_reenumerating_line(result: JsonObject) -> str | None:
    """The line of a failed OpenOCD call saying the probe was still coming back, or None.

    Only an OpenOCD result that did not succeed, and only one of the two lines
    above as a whole line of its own capture: a line that quotes the words inside
    another report is that other report."""
    if result.get("backend") != "openocd" or result.get("ok") is True:
        return None
    output = result.get("programmer_output")
    if not isinstance(output, dict):
        return None
    for line in f"{output.get('stdout') or ''}\n{output.get('stderr') or ''}".splitlines():
        if line.strip() in OPENOCD_PROBE_REENUMERATING_LINES:
            return line.strip()
    return None


def read_failure_reason(completed: CompletedCommand, what: str, timeout_s: float, expected: str) -> str:
    """Why one configuration-stage read of an OpenOCD answered nothing, in its own words where it wrote any.

    Five unlike outcomes collapsed to a bare `None` before this: not found, not
    executable, timed out, a non-zero exit, and an exit 0 that printed nothing the
    read was looking for. Only the last of those is anything like "this OpenOCD is
    simply older", and none of them was recorded anywhere, so the refusal a failed
    read causes could not be traced back to it. The repo's rule is that a failure
    surface shows the decisive line, redacted if it has to be, and here there is
    nothing to redact: OpenOCD's own words about its own binary.

    The path is not in the sentence. Every result that carries this also carries
    `executable`, so naming it twice adds nothing, and this line travels into
    `likely_causes`, which an agent reads back.
    """
    if completed.not_found:
        return f"the {what} could not be started: the file is not there"
    if completed.not_executable:
        return f"the {what} could not be started: the file is not executable"
    if completed.timed_out:
        return f"the {what} did not answer within {timeout_s:g}s"
    line = _last_written_line(f"{completed.stdout}{completed.stderr}")
    if completed.returncode != 0:
        return f"the {what} exited {completed.returncode}" + (f": {line}" if line else " and wrote nothing")
    return f"the {what} exited 0 without {expected}" + (f"; its last line was: {line}" if line else " and wrote nothing at all")


def openocd_version(executable_path: str, timeout_s: float) -> tuple[OpenOCDRelease | None, str | None]:
    """The release the OpenOCD at this path says it is, from its own `--version`, or why it did not say.

    Exactly one of the two is set. The second is the whole reason this returns a
    pair: the release read is the one that decides which selector a call gets, and
    a read that failed sends the call to `adapter serial` for a reason that has
    nothing to do with the release, which is a wrapper the repo explicitly
    supports (`debuggers.<name>.executable` may be one) swallowing `--version`."""
    completed = spawn_command([*invocation(executable_path), "--version"], str(Path(executable_path).parent), timeout_s)
    reason = "OpenOCD release read (`--version`)"
    if completed.not_found or completed.not_executable or completed.timed_out or completed.returncode != 0:
        return None, read_failure_reason(completed, reason, timeout_s, "naming a release")
    release = parse_openocd_version(f"{completed.stdout}{completed.stderr}")
    if release is None:
        return None, read_failure_reason(completed, reason, timeout_s, "printing a version banner")
    return release, None


def parse_openocd_adapter_driver(output: str) -> str | None:
    """The adapter driver `adapter name` named behind this backend's marker, word for word.

    That includes OpenOCD's own `undefined` for a script that loaded none. None
    when no line, or more than one answer, carries the marker: a run that stopped
    before the echo, such as on an interface script OpenOCD could not find."""
    answers = {line.strip()[len(OPENOCD_ADAPTER_DRIVER_MARKER) :].strip() for line in output.splitlines() if line.strip().startswith(OPENOCD_ADAPTER_DRIVER_MARKER)}
    if len(answers) != 1:
        return None
    return answers.pop() or None


def openocd_adapter_driver(executable_path: str, interface_cfg: str, timeout_s: float) -> tuple[str | None, str | None]:
    """The adapter driver this interface script loads on the OpenOCD at this path, or why it could not be read.

    OpenOCD loads the script, echoes `adapter name` and shuts down, all at its
    configuration stage: `init`, which opens the adapter, is never reached. The
    second half of the pair is why nothing was read, for the reason
    `openocd_version` returns one: a failed read here sends the call to `adapter
    serial` too, and on 0.11 that is refused."""
    completed = spawn_command(
        [*invocation(executable_path), "-f", interface_cfg, "-c", OPENOCD_ADAPTER_DRIVER_QUERY, "-c", "shutdown"],
        str(Path(executable_path).parent),
        timeout_s,
    )
    reason = f"adapter driver read of `{interface_cfg}`"
    if completed.not_found or completed.not_executable or completed.timed_out or completed.returncode != 0:
        return None, read_failure_reason(completed, reason, timeout_s, "naming an adapter driver")
    driver = parse_openocd_adapter_driver(f"{completed.stdout}{completed.stderr}")
    if driver is None:
        return None, read_failure_reason(completed, reason, timeout_s, f"echoing `{OPENOCD_ADAPTER_DRIVER_MARKER}` once")
    return driver, None


@dataclass(frozen=True)
class OpenOCDProbeSelection:
    """The `-c` arguments that select the configured probe, and what they were chosen from.

    `supported` is False when this OpenOCD has no selector for the probe's
    adapter driver, and then `commands` is empty: such a call is refused, never
    sent without a selector.

    `read_failure` is set only where one of the two configuration-stage reads
    answered nothing, and is then the decisive line it answered nothing with. It
    is what tells the two `adapter serial` fallbacks apart: the release really is
    0.12.0 or newer, or nothing here could find out. Three surfaces carry it, and
    they are every refusal this selection shapes: the refusal of a selection this
    OpenOCD has no selector for, every failure of a tool call the selection was
    sent with, and every start that never reached its GDB port, whether its
    server stopped or hung. A call that succeeded carries nothing, having nothing
    to explain. All of them, because a refusal caused by a failed read used to
    name only `adapter serial` and the three generic causes, with nothing
    pointing at the repair."""

    commands: tuple[str, ...]
    version: str | None
    adapter_driver: str | None
    supported: bool
    read_failure: str | None = None


def openocd_probe_selection(
    executable_path: str,
    interface_cfg: str,
    probe_id: str | None,
    timeout_s: float,
    *,
    read_release: Callable[[str, float], tuple[OpenOCDRelease | None, str | None]] = openocd_version,
) -> OpenOCDProbeSelection:
    """How the OpenOCD at this path is told which probe to open.

    Without a `probe_id` nothing is selected, as always. With one, the choice
    turns on the release. OpenOCD 0.12.0 and newer take `adapter serial` for
    every adapter driver, and so does an OpenOCD whose release cannot be read:
    a release without the command refuses it before `init`, so no probe is
    opened either way. Before 0.12 the selector is the adapter driver's own,
    and the driver is asked of the interface script at the configuration stage.
    A driver with none this backend can give the serial to is not supported,
    and neither is a script that loads no driver; a driver that cannot be read
    is given `adapter serial`, which that release refuses before `init`.

    The one thing never returned for a `probe_id` is an empty selection that is
    supported: OpenOCD would open whichever probe it found first.

    Either fallback to `adapter serial` taken because a read failed carries that
    read's decisive line on `read_failure`, so a call refused afterwards can be
    traced to it. Nothing about the choice changes: a release without the command
    refuses it before `init`, and that is still the safe answer.

    `read_release` reads the release; the backend passes one that remembers it."""
    if probe_id is None:
        return OpenOCDProbeSelection((), None, None, True)
    generic = ("-c", f"adapter serial {probe_id}")
    release, release_failure = read_release(executable_path, timeout_s)
    if release is None:
        return OpenOCDProbeSelection(generic, None, None, True, release_failure)
    version, numbers = release
    if numbers >= OPENOCD_ADAPTER_SERIAL_SINCE:
        return OpenOCDProbeSelection(generic, version, None, True)
    driver, driver_failure = openocd_adapter_driver(executable_path, interface_cfg, timeout_s)
    if driver is None:
        return OpenOCDProbeSelection(generic, version, None, True, driver_failure)
    selector = OPENOCD_0_11_SERIAL_SELECTORS.get(driver)
    if selector is None:
        return OpenOCDProbeSelection((), version, driver, False)
    return OpenOCDProbeSelection(("-c", f"{selector} {probe_id}"), version, driver, True)


def probe_selection_unsupported_reason(selection: OpenOCDProbeSelection, interface_cfg: str) -> str:
    """Why this OpenOCD cannot select the configured probe, naming the release and the driver."""
    if selection.adapter_driver == "undefined":
        driver = f"the interface script `{interface_cfg}` loads no adapter driver"
    else:
        driver = f"the `{selection.adapter_driver}` adapter driver the interface script `{interface_cfg}` loads has none that takes this probe's serial"
    return f"OpenOCD {selection.version} selects a probe by serial only through the adapter driver's own command, and {driver}"


class OpenOCDBackend:
    backend_name = "openocd"

    def __init__(self, config: AgenticHILConfig):
        self.config = config
        # Set while the recovery resets into halt (`recovery_reset`), and only then.
        self._recovery_reset = False
        # The release each OpenOCD file this backend has run says it is, by path,
        # modification time and size, so a file replaced in place is asked again.
        # Only answers are kept: an OpenOCD that did not say is asked next time.
        self._releases: dict[tuple[str, int, int], OpenOCDRelease] = {}
        # The selection a session start's resolve chose, for the server it then
        # starts, with the executable, script and serial it was chosen for.
        self._debug_probe_selection: tuple[tuple[str, str, str | None], OpenOCDProbeSelection] | None = None
        # The configuration-stage read that answered nothing for the call being
        # run, so its decisive line reaches that call's own result. Set at the top
        # of every `_run_openocd` and read by it alone.
        self._selection_read_failure: str | None = None
        self._debug = GdbDebugSessions(
            config,
            backend_name=self.backend_name,
            resolve_server=self._resolve_debug_server,
            build_server_args=self._debug_server_args,
            classify_server_output=self._classify_output,
            server_steps=OPENOCD_GDB_SERVER_STEPS,
            read_start_failure=self._debug_start_failure,
            read_start_context=self._debug_start_context,
        )

    def reconfigure(self, config: AgenticHILConfig) -> None:
        debugger_changed = config.debugger != self.config.debugger or config.target != self.config.target
        # `allow_raw_debugger_commands` is read the way `_debug_permission_failure`
        # reads it: a debug session is refused *while* raw commands are allowed,
        # so a config that turns that grant on is one an open session may not
        # outlive. An unbound config has no session to authorize at all: the
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
        if completed.not_executable:
            return {"tool": "debugger_info", **not_executable_refusal(self.backend_name, str(resolved["executable_path"]), completed)}
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
        """Enumerate the probes attached to this host, or say why there is no way to.

        OpenOCD still has no command that lists connected probes: it is told
        which adapter to open and opens it. What it has, on an ST-Link bench, is
        a host that already knows. The probe publishes its serial in the USB
        descriptor of the virtual COM port it exposes, that string is exactly
        what OpenOCD selects the probe by (`adapter serial` from 0.12, the
        adapter driver's own selector before), and bootstrap discovery has read probes out
        of it since #423. The configured bench simply did not use it, so
        `agentic-hil debugger-probes` refused `not_supported` on the very bench
        TROUBLESHOOTING.md sends an OpenOCD reader to it from, while the serial
        they were after was already in `agentic-hil com-ports`.

        `not_supported` survives where it is still true: an entry whose
        `interface_cfg` names an adapter that publishes no USB serial identity
        this host can read has no enumeration behind it, and inventing one out of
        the vendor id alone would be the wrong-board guess this project's
        identity rules exist to refuse. The result says which enumeration
        answered under `discovered_by`, so a caller never has to infer it.

        The inventory reaches an ST-Link only through the virtual COM port a V2-1
        or a V3 publishes, so a standalone ST-LINK/V2 and any probe with no VCP
        are outside what it can see. That blind spot does not close once one
        VCP-backed probe is found: a second, VCP-less probe can sit beside the
        one that was seen, and this enumeration would never observe it. Every
        listing off this inventory is therefore reported `complete: false` -- the
        empty reading because "nothing on the serial bus" is not "no probe
        attached", and a nonempty one because what it lists is not necessarily
        every probe attached. A caller that needs an authoritative count reads
        the ids off the probes or the adapter vendor's own tool.

        Nothing is contacted either way: this reads a USB descriptor listing and
        says nothing to a board."""
        tool = "debugger_probes_list"
        if not self.config.probe_allowed():
            return self._permission_denied(tool, "Debugger probe discovery is disabled by the authoritative config.", self._permission_key("allow_probe"))
        interface_cfg = self.config.debugger.interface_cfg
        if not openocd_interface_enumerates_by_usb(interface_cfg):
            return {
                "ok": False,
                "tool": tool,
                "backend": self.backend_name,
                "error_type": "not_supported",
                "summary": (
                    "OpenOCD has no command that enumerates connected probe IDs, and this entry's adapter is not one "
                    f"this host can enumerate from its USB serial inventory either: `interface_cfg` is `{interface_cfg}`, "
                    "which names neither an ST-Link nor any other adapter whose USB identity is read here. Read the id "
                    "off the probe, or off the adapter vendor's own tool."
                ),
                "interface_cfg": interface_cfg,
                **NOT_CONTACTED,
            }
        inventory = list_available_com_ports(tool)
        if inventory.get("ok") is not True:
            return {
                "ok": False,
                "tool": tool,
                "backend": self.backend_name,
                "error_type": "probe_discovery_failed",
                "discovered_by": DISCOVERED_BY_USB_INVENTORY,
                "summary": (
                    "OpenOCD enumerates ST-Link probes from this host's USB serial inventory, and that inventory could "
                    f"not be read: {inventory.get('summary', 'the serial backend did not answer')}"
                ),
                **({"backend_error": inventory["backend_error"]} if inventory.get("backend_error") else {}),
                "interface_cfg": interface_cfg,
                **NOT_CONTACTED,
            }
        probe_ids = usb_stlink_probe_ids(inventory)
        stlink_ports = usb_stlink_ports(inventory)
        if not probe_ids and not stlink_ports:
            # No ST-Link is on the serial bus at all: not one that published a
            # serial to name it, and not one that published a VCP without a
            # serial. This enumeration reaches an ST-Link only through the virtual
            # COM port a V2-1 or a V3 exposes, so a standalone ST-LINK/V2, and any
            # probe OpenOCD drives that publishes no VCP, never enter it. An empty
            # reading here is therefore not proof no probe is attached, and
            # answering `probes: []` as a finished count would say "no probe"
            # about hardware this method cannot observe -- the false negative that
            # made this worse than the old `not_supported` (round 0, finding 2).
            # The reading itself succeeded, so `ok` stays true, but `complete` is
            # false and the summary names the blind spot so a caller never reads
            # it as an empty bench.
            return {
                "ok": True,
                "tool": tool,
                "backend": self.backend_name,
                "discovered_by": DISCOVERED_BY_USB_INVENTORY,
                "probes": [],
                "stlink_ports": [],
                "complete": False,
                "interface_cfg": interface_cfg,
                "summary": (
                    "No ST-Link with a virtual COM port is on this host's USB serial inventory, and that inventory is "
                    "the only enumeration behind this listing. It reaches an ST-Link only through the VCP a V2-1 or a "
                    "V3 publishes, so a standalone ST-LINK/V2 -- or any probe OpenOCD drives that exposes no virtual "
                    "COM port -- would not appear here even if it is attached: this is not proof no probe is "
                    "connected. Read the id off the probe or the adapter vendor's own tool, or check "
                    "`agentic-hil com-ports`."
                ),
                **NOT_CONTACTED,
            }
        return {
            "ok": True,
            "tool": tool,
            "backend": self.backend_name,
            "discovered_by": DISCOVERED_BY_USB_INVENTORY,
            "probes": [{"probe_id": probe_id} for probe_id in probe_ids],
            # The ports the enumeration was read out of, including any ST-Link
            # that published no serial. An empty `probes` beside a listed port is
            # a probe that is there and cannot be named, which is a different
            # fact from no probe at all.
            "stlink_ports": stlink_ports,
            # At least one ST-Link is on the serial bus, so this is not the blind
            # empty reading above -- but it is still not an authoritative count.
            # The enumeration sees an ST-Link only through the VCP it publishes,
            # so a standalone ST-LINK/V2 or any VCP-less probe beside the ones
            # listed here would never appear. `complete` stays false so a caller
            # cannot read this len() as "every probe attached".
            "complete": False,
            "interface_cfg": interface_cfg,
            "summary": (
                f"{len(probe_ids)} connected debugger probe(s) read from this host's USB serial inventory. OpenOCD has "
                "no probe listing of its own, so the ids come from the USB descriptors the probes published and nothing "
                "was said to a board. The inventory sees an ST-Link only through the virtual COM port it publishes, so a "
                "standalone ST-LINK/V2 -- or any probe OpenOCD drives that exposes no virtual COM port -- would not "
                "appear here even if attached: this is not necessarily every probe connected. Read the ids off the "
                "probes or the adapter vendor's own tool for an authoritative count."
            ),
            **NOT_CONTACTED,
        }

    def probe_target(self) -> JsonObject:
        if not self.config.probe_allowed():
            return self._permission_denied("probe_target", "Probing is disabled by the authoritative config.", self._permission_key("allow_probe"))
        marker = OPENOCD_SUCCESS_MARKERS["probe_target"]
        result = self._run_openocd("probe_target", f'{OPENOCD_INIT_PREFIX}targets; echo "{marker}"; shutdown', marker)
        if result.get("ok"):
            result["target_detected"] = True
            result["summary"] = summary_with_carried_warnings(result, "Target detected through OpenOCD.")
        return self._write_action_report(result)

    def flash_firmware(self, artifact: JsonObject, reset_after_flash: bool = False) -> JsonObject:
        if not self.config.debugger.permissions.allow_flash:
            return self._permission_denied("flash_firmware", "Flashing is disabled by the authoritative config.", self._permission_key("allow_flash"))
        if self.config.debugger.permissions.allow_raw_debugger_commands:
            return self._exclusive_permission_denied("flash_firmware", "Flashing", "allow_raw_debugger_commands")
        if self.config.debugger.permissions.allow_mass_erase:
            return self._exclusive_permission_denied("flash_firmware", "Flashing", "allow_mass_erase")

        # A raw binary carries no load address, and `program` writes one from
        # address 0 unless it is given the address as its offset argument, which
        # `help program` lists right after the file. An ELF or a HEX file names
        # its own addresses, and an offset would move them, so the field is read
        # for a .bin alone, and a .bin without it is refused before OpenOCD runs,
        # as pyOCD and STM32CubeProgrammer refuse it. The config schema holds the
        # field to a hex or decimal number, so it is a safe Tcl word as it is.
        offset = ""
        if Path(str(artifact["resolved_path"])).suffix.lower() == ".bin":
            if self.config.debugger.flash_address is None:
                return {"ok": False, "tool": "flash_firmware", "backend": self.backend_name, "error_type": "invalid_argument", "summary": "Flashing .bin artifacts with OpenOCD requires debuggers.<name>.flash_address.", "artifact": {"source": artifact.get("source", "path"), "path": artifact.get("path"), "sha256": artifact.get("sha256")}}
            offset = f" {self.config.debugger.flash_address}"
        command_path = escape_tcl_double_quoted_word(openocd_path_for_command(str(artifact["resolved_path"])))
        marker = OPENOCD_SUCCESS_MARKERS["flash_firmware"]
        reset_command = " reset" if reset_after_flash else ""
        # `program` runs `init` itself, so the explicit prefix changes nothing
        # about the flash: `init` guards against running twice. What it adds is
        # the stage echo: a failure whose output lacks the init marker provably
        # stopped before adapter_init opened the probe, which is what lets a
        # missing config script or an absent adapter refuse instead of
        # quarantining the bench (see _failure_result).
        result = self._run_openocd("flash_firmware", f'{OPENOCD_INIT_PREFIX}program "{command_path}"{offset} verify{reset_command}; echo "{marker}"; shutdown', marker)
        result["artifact"] = {"source": artifact.get("source", "path"), "path": artifact.get("path"), "sha256": artifact.get("sha256")}
        result["verify"] = True
        result["reset_after_flash"] = reset_after_flash
        if result.get("ok"):
            result["summary"] = summary_with_carried_warnings(result, "Firmware flashed, verified, and target reset." if reset_after_flash else "Firmware flashed and verified. Target was not reset.")
        return self._write_action_report(result)

    @contextmanager
    def recovery_reset(self) -> Iterator[None]:
        """Inside, `reset_target("halt")` is the recovery's: it runs the reconnecting reset ahead of the halt (#621)."""
        self._recovery_reset = True
        try:
            yield
        finally:
            self._recovery_reset = False

    def reset_target(self, mode: str = "run") -> JsonObject:
        allowed_modes = ["run", "halt", "init"]
        if mode not in allowed_modes:
            return {"ok": False, "tool": "reset_target", "error_type": "invalid_argument", "summary": "Invalid reset mode.", "allowed_values": allowed_modes}
        marker = OPENOCD_SUCCESS_MARKERS["reset_target"]
        command = openocd_recovery_reset_halt_command(marker) if mode == "halt" and self._recovery_reset else openocd_reset_command(mode, marker)
        result = self._run_openocd("reset_target", command, marker)
        result["mode"] = mode
        if result.get("ok"):
            result["summary"] = summary_with_carried_warnings(result, f"Target reset with mode '{mode}'.")
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

    def debug_symbol_info(self, symbol: str, symbol_elf: JsonObject | None = None) -> JsonObject:
        # Ignored here for the reason the two reads below ignore it: the session
        # loaded an image, and that image is the one the target is running.
        return self._debug.symbol_info(symbol)

    def debug_symbol_value(self, symbol: str, symbol_elf: JsonObject | None = None) -> JsonObject:
        # `symbol_elf` is ignored here for the reason it is ignored by the dump
        # below: this backend has a session, and the image that session loaded is
        # the one the target is running.
        return self._debug.symbol_value(symbol)

    def debug_dump_symbol_ihex(self, symbol: str, output: JsonObject, symbol_elf: JsonObject | None = None) -> JsonObject:
        # `symbol_elf` is the service's offer of an ELF to resolve a symbol
        # against, for a backend with no loaded image to ask. This one has one:
        # the session holds the artifact it started with, and answering out of
        # any other file could describe a build the target is not running.
        return self._debug.dump_symbol_ihex(symbol, output)

    def sessionless_debug_tools(self) -> frozenset[str]:
        """None: every typed-debug read here runs through the session lease.

        This backend answers `debug_symbol_value` and `debug_dump_symbol_ihex`
        out of the session a caller opened, so the coordination layer must keep
        treating them as session-scoped and never take a one-shot lease for
        them."""
        return frozenset()

    def opens_debug_sessions(self) -> bool:
        """Yes: through OpenOCD's own GDB server."""
        return True

    def target_support(self) -> JsonObject:
        """OpenOCD has no target type to check.

        It selects the target with `target_cfg`, a script this OpenOCD reads
        directly: either a file config load resolved here, or a search name
        OpenOCD resolves against its own script path. So there is no separate
        catalogue that could be missing. Answered rather than omitted, so the
        field means the same thing on every backend.
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
        key = (executable_path, self.config.debugger.interface_cfg, self.config.debugger.probe_id)
        stored = self._debug_probe_selection
        selection = stored[1] if stored is not None and stored[0] == key else self._probe_selection(executable_path)
        # A session start refuses an unsupported selection in its resolve, before
        # this runs. Reached without one, the server is still never started
        # without a selector: it gets `adapter serial`, which a release without
        # it refuses before `init`.
        probe_selection = list(selection.commands) if selection.supported else self._adapter_serial_selection()
        return [
            *invocation(executable_path),
            "-f",
            self.config.debugger.interface_cfg,
            *probe_selection,
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

    def _debug_start_failure(self, output: str, server_args: list[str]) -> JsonObject | None:
        """What a dead debug server's own output says about where it stopped, or None.

        The server's command line carries the same probe-selection `-c` values the
        tool path puts on its own, including the documented `adapter serial`
        fallback taken when the release could not be read, and OpenOCD 0.11
        refuses a selector its loaded adapter driver does not register from inside
        the interpreter, before `init`. `rejected_openocd_commands` is what the
        tool path reads that with; the start classified from the output's words
        alone and answered `debugger_error` with "Debug server exited before the
        GDB port became ready." for a server that provably stopped at its first
        `-c`. The decisive line was in `server_stderr_tail` throughout and the
        markers were already right, so this is the classification catching up with
        what the transcript says.

        The server's last `-c` is its startup script (`init; halt`), the same
        place the tool path's own command sits, and every `-c` ahead of it is a
        configuration command. Split that way so this asks exactly the question
        the tool path asks.

        A start that timed out does not reach here at all: the server is still
        running, and `gdbdebug` asks this only of one that stopped. The read that
        shaped the command line travels on either, from `_debug_start_context`,
        because it is not a reading of the output.
        """
        values = [server_args[index + 1] for index, item in enumerate(server_args) if item == "-c" and index + 1 < len(server_args)]
        rejected = rejected_openocd_commands(values[-1], output, tuple(values[:-1])) if values else []
        if not rejected:
            return self._debug_start_public_error(output)
        backend_error_type = "command_rejected_before_init"
        error_type = self._public_error_type(backend_error_type)
        return {
            "error_type": error_type,
            "backend_error_type": backend_error_type,
            "summary": f"Debug server exited before the GDB port became ready: {self._failure_summary(backend_error_type, error_type)}",
            "rejected_commands": rejected,
        }

    def _debug_start_public_error(self, output: str) -> JsonObject | None:
        """The public name for what a dead debug server's output classifies as, where it has one, or None.

        `gdbdebug` publishes the classifier's own word as `error_type`, and the
        command path publishes `BACKEND_ERROR_TO_PUBLIC_ERROR`'s: a missing
        `target_cfg` was `target_config_not_found` from debug_start_session and
        `debugger_config_not_found` from probe_target. A caller that branches on
        `error_type` had two words for one missing script, so the start maps
        through the same table the command path does, keeps the classifier's
        word in `backend_error_type`, and says what the command path says about
        it, which names the configured field the script came from (#654).
        `unknown_debugger_error` is left to `gdbdebug`, which already publishes
        it as `debugger_error` with its own sentence."""
        backend_error_type = self._classify_output(output)
        error_type = self._public_error_type(backend_error_type)
        if error_type == backend_error_type or backend_error_type == "unknown_debugger_error":
            return None
        return {
            "error_type": error_type,
            "backend_error_type": backend_error_type,
            "summary": f"Debug server exited before the GDB port became ready: {self._failure_summary(backend_error_type, error_type)}",
        }

    def _debug_start_context(self, classified: JsonObject) -> JsonObject | None:
        """The configuration-stage read that shaped this start's command line, or None.

        Where one of the two reads answered nothing, the server was started with
        `adapter serial` for that reason, and the decisive line it answered
        nothing with is the repair: an `executable` that is a wrapper swallowing
        `--version` is an explicitly supported configuration, and nothing else on
        a start's surface names it. The line is about the command line the server
        was built with, known before it ran, so it rides out on a start that hung
        as well as on one that stopped, and says nothing about where either got
        to.

        It leads `likely_causes` rather than replacing it, exactly as
        `_run_openocd` leads the tool path's: the backend's own causes for the
        error this start was classified as follow, from the same two tables that
        path reads. Assigned instead, the list was this one line whatever the
        start stopped on, so a server refused by `LIBUSB_ERROR_ACCESS` offered
        the wrapper and not the group its udev rule names.
        """
        stored = self._debug_probe_selection
        read_failure = stored[1].read_failure if stored is not None else None
        if not read_failure:
            return None
        backend_error_type = str(classified.get("backend_error_type") or "")
        error_type = str(classified.get("error_type") or "")
        causes = OPENOCD_CAUSES_BY_BACKEND_ERROR.get(backend_error_type) or self._likely_causes(error_type)
        return {"probe_selection_read_failure": read_failure, "likely_causes": [read_failure, *causes]}

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

    def _resolve_debug_server(self) -> JsonObject:
        """The OpenOCD a debug session starts, or the refusal that starts none.

        The server selects the probe the way every other call does, and the reads
        that decide how run here, before a port is reserved or a server started:
        an OpenOCD with no way to select the configured probe refuses the session
        with nothing started for it."""
        resolved = self._resolve_executable()
        if not resolved["ok"]:
            return resolved
        executable_path = str(resolved["executable_path"])
        selection = self._probe_selection(executable_path)
        if not selection.supported:
            return self._probe_selection_refusal(selection)
        self._debug_probe_selection = ((executable_path, self.config.debugger.interface_cfg, self.config.debugger.probe_id), selection)
        return resolved

    def _run_openocd(self, tool: str, openocd_command: str, success_marker: str | None = None) -> JsonObject:
        """One OpenOCD call, carrying a configuration-stage read that failed on the way to it.

        The two reads that decide which selector this call gets run before it and
        answer nothing on a host whose OpenOCD is behind a wrapper that swallows
        `--version`, which this repo explicitly supports as an `executable`. The
        call is then made with `adapter serial`, which a 0.11 refuses before
        `init`, and the refusal named that command and three generic causes with no
        `openocd_version` field and nothing at all pointing at the wrapper. So the
        line the read failed with rides out on every failure of the call it shaped:
        it is not this call's error, and it is why this call has one.
        """
        self._selection_read_failure = None
        result = self._run_openocd_call(tool, openocd_command, success_marker)
        failure = self._selection_read_failure
        if failure and result.get("ok") is not True:
            result["probe_selection_read_failure"] = failure
            result["likely_causes"] = [failure, *(result.get("likely_causes") or [])]
        return result

    def _run_openocd_call(self, tool: str, openocd_command: str, success_marker: str | None = None) -> JsonObject:
        started_at = utc_now_iso()
        start = time.perf_counter()
        resolved = self._resolve_executable()
        if not resolved["ok"]:
            return {"tool": tool, "backend": self.backend_name, "started_at": started_at, **resolved, "finished_at": utc_now_iso(), "elapsed_ms": int((time.perf_counter() - start) * 1000)}

        selection = self._probe_selection(str(resolved["executable_path"]))
        # Kept for the wrapper above, which puts it on whatever this returns. The
        # refusal branch below publishes it itself, from the selection.
        self._selection_read_failure = selection.read_failure
        if not selection.supported:
            # Before the call's own run and before its log: OpenOCD was started
            # only for the two configuration-stage reads, which open no adapter.
            return {"tool": tool, "started_at": started_at, **self._probe_selection_refusal(selection), "finished_at": utc_now_iso(), "elapsed_ms": int((time.perf_counter() - start) * 1000)}
        probe_selection = list(selection.commands)
        args = [
            *invocation(str(resolved["executable_path"])),
            "-f",
            self.config.debugger.interface_cfg,
            *probe_selection,
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
        if completed.not_executable:
            # Before the log is written, exactly where the missing-file branch
            # returns: no process ran, so there is no transcript to record and
            # nothing was said to the board.
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
        # The probe selection's own `-c` values, which OpenOCD evaluates before
        # the target script and before `openocd_command`.
        rejected = rejected_openocd_commands(openocd_command, output, tuple(probe_selection[1::2]))
        init_reached = OPENOCD_INIT_STAGE_MARKER in output
        if completed.returncode == 0:
            marker_printed = success_marker is not None and success_marker in output
            backend_error_type = self._backend_error_from_output(output, tool, marker_printed=marker_printed)
            if backend_error_type is not None:
                return self._finish_log_audit(self._failure_result(tool, started_at, finished_at, elapsed_ms, backend_error_type, log_path, completed, rejected, init_reached=init_reached), audit_error)
            if success_marker is not None and not marker_printed:
                # Only the success marker decides this branch, so the init-stage
                # marker may well be in the output beside it: a run that
                # completed `init`, examined the core and then lost the second
                # echo. The result carries which of the two were printed so a
                # caller reads this run's evidence rather than inferring the
                # worst case from the error_type, and so the shipped catalogue
                # entry can describe a partial confirmation without claiming
                # anything about a particular run.
                return self._finish_log_audit(
                    self._failure_result(
                        tool,
                        started_at,
                        finished_at,
                        elapsed_ms,
                        self._unconfirmed_backend_error_type(tool),
                        log_path,
                        completed,
                        rejected,
                        init_reached=init_reached,
                        operation_result=self._marker_evidence(output, success_marker),
                    ),
                    audit_error,
                )
            result: JsonObject = {"ok": True, "tool": tool, "backend": self.backend_name, "started_at": started_at, "finished_at": finished_at, "elapsed_ms": elapsed_ms, "summary": "OpenOCD command completed successfully.", "log_path": display_path(self.config, log_path)}
            if success_marker is not None:
                result["success_confirmed"] = True
                # Whatever OpenOCD said on the way to a marker it did print is
                # evidence about how the operation went, and never the verdict on
                # whether it happened: the marker already answered that. Carried
                # rather than dropped, because the line is real and a reader
                # chasing a slow or noisy bench needs it (`Error setting register
                # pc` on OpenOCD 0.12 over hla_swd is the measured one, #425), and
                # the log at `log_path` keeps holding the whole capture besides.
                warnings = failure_text_lines(output)
                if warnings:
                    result["backend_warnings"] = warnings
                    result["summary"] = summary_with_carried_warnings(result, result["summary"])
            return self._finish_log_audit(result, audit_error)
        return self._finish_log_audit(self._failure_result(tool, started_at, finished_at, elapsed_ms, self._classify_output(output, tool), log_path, completed, rejected, init_reached=init_reached), audit_error)

    # Failures whose classification already names the phase before the adapter
    # opens. Config scripts load at the configuration stage, and the adapter is
    # first opened by adapter_init inside `init`; an adapter that could not be
    # opened is the other face of the same boundary. Each of these may be read
    # as "never reached the bench" ONLY together with the absent init-stage
    # marker (see _failure_result): the classification alone is string
    # matching, and the marker is what makes it a proof. Nothing here covers the
    # timeout, which is the deadline killing the process before it could say
    # where it stopped.
    PRE_CONTACT_BACKEND_ERRORS = frozenset(
        {"interface_config_not_found", "target_config_not_found", "config_file_not_found", "adapter_not_found", "adapter_access_denied"}
    )
    # One classification further out, and only for the two tools whose command
    # string drives nothing: OpenOCD's own report that nothing answered on the
    # selected transport, which is what the error catalogue's
    # `target_not_detected:openocd` entry means by it. A target that never
    # answered was never brought under debug control, and with the init-stage
    # marker absent `init` is the only thing that could have addressed it at all:
    # `targets` lists and `shutdown` ends. `flash_firmware` and `reset_target`
    # are deliberately not here: their command strings drive the target
    # themselves, so the same words also fit a target that stopped answering
    # while it was being driven, and the safe reading of an ambiguity is the one
    # that keeps the bench contained.
    #
    # Only a `target_not_detected` the classifier read out of OpenOCD's own
    # output qualifies. `probe_unconfirmed` (an exit of 0 with the success
    # marker missing) stays out, because it is the absence of a report rather
    # than a report that nothing answered: OpenOCD may have completed `init`,
    # halted the core and then lost the success marker, with or without the
    # stage marker beside it, and that board's run state is unknown either way. It carries `target_state_unconfirmed` to the caller for the same
    # reason, so the public error_type and its catalogue entry withhold the
    # abort-point claim this set is about.
    READ_ONLY_PRE_CONTACT_BACKEND_ERRORS = frozenset({"target_not_detected"})

    def _proves_no_contact(self, tool: str, backend_error_type: str) -> bool:
        if backend_error_type in self.PRE_CONTACT_BACKEND_ERRORS:
            return True
        return tool in READ_ONLY_TOOLS and backend_error_type in self.READ_ONLY_PRE_CONTACT_BACKEND_ERRORS

    def _marker_evidence(self, output: str, success_marker: str) -> JsonObject:
        """Which of the two echoes this backend asks for reached the output.

        Both are `echo`ed by the command string, so each is evidence about a
        stage of this run and neither is a verdict from OpenOCD about the
        target. Reported in the same shape the ST-Link backend uses for its
        confirmation lines, so a caller reads one field for both."""
        expected = [OPENOCD_INIT_STAGE_MARKER, success_marker]
        return {"confirmed": False, "expected_success_text": expected, "matched_success_text": [marker for marker in expected if marker in output]}

    def _failure_result(self, tool: str, started_at: str, finished_at: str, elapsed_ms: int, backend_error_type: str, log_path: str, completed: CompletedCommand, rejected_commands: list[str] | None = None, *, init_reached: bool = True, operation_result: JsonObject | None = None) -> JsonObject:
        # likely_causes says what may be wrong; remediation says what to check
        # next, scoped to this backend, because the checks differ per tool: an
        # OpenOCD target is selected by target_cfg, a pyOCD one by target_type.
        # Same catalogue the MCP reference serves, so the two cannot diverge.
        if rejected_commands:
            backend_error_type = "command_rejected_before_init"
        error_type = self._public_error_type(backend_error_type)
        # `programmer_output` on every classified failure (#334). OpenOCD wrote a
        # line about whatever this run stopped at, including the `invalid command
        # name` its own interpreter answered with, and that line is what the
        # classification, the summary and the causes were all read out of.
        result = {"ok": False, "tool": tool, "backend": self.backend_name, "started_at": started_at, "finished_at": finished_at, "elapsed_ms": elapsed_ms, "error_type": error_type, "backend_error_type": backend_error_type, "summary": self._failure_summary(backend_error_type, error_type), "likely_causes": OPENOCD_CAUSES_BY_BACKEND_ERROR.get(backend_error_type) or self._likely_causes(error_type), **remediation_fields(error_type, self.backend_name), "log_path": display_path(self.config, log_path), **programmer_output_fields(completed)}
        if operation_result is not None:
            result["operation_result"] = operation_result
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
            # `init` never completed: adapter_init never had a probe to drive,
            # or target examine never brought a core under debug control. The
            # board is exactly as the last call that did reach it left it, and a
            # quarantine here would demand a physical inspection of hardware
            # this run provably never touched.
            result.update(NOT_CONTACTED)
        return result

    def _backend_error_from_output(self, output: str, tool: str, *, marker_printed: bool = False) -> str | None:
        """The failure this exit-0 run reported, or None when it reported none.

        With the tool's success marker in the output there is almost nothing to
        report: OpenOCD stops evaluating a `-c` script at the first command that
        fails, so an `echo` placed after the operation could not have run unless
        the operation's command returned success, and every failure word ahead of
        it is a line OpenOCD wrote while succeeding. Reading those words as the
        verdict is what refused a reset the board had already performed and the
        same OpenOCD had already confirmed (#425): the Ubuntu 24.04 build writes
        `Error: Error setting register pc` on `reset run` over hla_swd after the
        core has restarted, and one incidental `Error:` line is all the
        operation-anchored bucket at the bottom of `_classify_output` needs.

        `OPENOCD_BACKEND_ERRORS_OUTRANKING_THE_MARKER` names what still decides,
        and why. Without the marker nothing has changed: the classification
        answers as it always has, and a run whose words say nothing at all is the
        caller's `*_unconfirmed` branch rather than this one.
        """
        backend_error_type = self._classify_output(output, tool)
        if marker_printed:
            return backend_error_type if backend_error_type in OPENOCD_BACKEND_ERRORS_OUTRANKING_THE_MARKER else None
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

    def _probe_selection(self, executable_path: str) -> OpenOCDProbeSelection:
        """How the OpenOCD at this path selects the configured probe (see `openocd_probe_selection`)."""
        return openocd_probe_selection(
            executable_path,
            self.config.debugger.interface_cfg,
            self.config.debugger.probe_id,
            min(self.config.debugger.timeout_s, OPENOCD_CONFIGURATION_READ_TIMEOUT_S),
            read_release=self._release,
        )

    def _release(self, executable_path: str, timeout_s: float) -> tuple[OpenOCDRelease | None, str | None]:
        """The release the OpenOCD at this path says it is, asked once per file, or why it did not say.

        Only answers are remembered, deliberately: an OpenOCD that did not answer
        is asked again, so a wrapper repaired between two calls is believed at
        once and a read that failed once cannot pin a host to `adapter serial` for
        the life of the process. The cost is one extra spawn per call on a host
        whose read keeps failing, and the reason it keeps failing now travels with
        every result, which is what turns that into something an operator can fix
        rather than a silence."""
        try:
            status = os.stat(executable_path)
        except OSError:
            return openocd_version(executable_path, timeout_s)
        key = (executable_path, status.st_mtime_ns, status.st_size)
        release = self._releases.get(key)
        if release is not None:
            return release, None
        release, failure = openocd_version(executable_path, timeout_s)
        if release is not None:
            self._releases[key] = release
        return release, failure

    def _adapter_serial_selection(self) -> list[str]:
        return [] if self.config.debugger.probe_id is None else ["-c", f"adapter serial {self.config.debugger.probe_id}"]

    def _probe_selection_refusal(self, selection: OpenOCDProbeSelection) -> JsonObject:
        """A call refused because this OpenOCD cannot select the configured probe by its serial.

        Refused rather than sent without a selector, which would let OpenOCD open
        whichever probe it found first. The caller adds its tool and times."""
        interface_cfg = self.config.debugger.interface_cfg
        return {
            "ok": False,
            "backend": self.backend_name,
            "error_type": "not_supported",
            "backend_error_type": "probe_selection_not_supported",
            "summary": (
                f"{probe_selection_unsupported_reason(selection, interface_cfg)}, so the probe `probe_id` names cannot "
                "be selected. The call was refused before OpenOCD was started for it, and nothing was sent to the "
                "board. OpenOCD 0.12.0 and newer select every adapter driver's probe with `adapter serial`."
            ),
            "openocd_version": selection.version,
            "adapter_driver": selection.adapter_driver,
            "interface_cfg": interface_cfg,
            **({"probe_selection_read_failure": selection.read_failure} if selection.read_failure else {}),
            "likely_causes": [
                # The read that failed first, where one did, because then it is
                # the repair: the release and the driver below are what could not
                # be read, so the three generic causes point at nothing.
                *([selection.read_failure] if selection.read_failure else []),
                f"the installed OpenOCD is {selection.version}, older than 0.12.0, which added `adapter serial` for every adapter driver",
                "the interface script loads an adapter driver with no serial selector of its own on this release, or one whose selector takes serials in another form (`jlink serial` takes numbers only), or loads no adapter driver at all",
            ],
            **remediation_fields("not_supported", "openocd_probe_selection"),
            **NOT_CONTACTED,
        }

    def _classify_output(self, output: str, tool: str | None = None) -> str:
        lower = output.lower()
        interface_config = self.config.debugger.interface_cfg.lower()
        target_config = self.config.debugger.target_cfg.lower()
        if interface_config in lower and contains_any(lower, ["not found", "can't find", "couldn't find", "couldn't open"]):
            return "interface_config_not_found"
        if target_config in lower and contains_any(lower, ["not found", "can't find", "couldn't find", "couldn't open"]):
            return "target_config_not_found"
        # Ahead of the adapter rule, which the same transcript also matches: its
        # `open failed` line is the whole of what OpenOCD prints with no probe on
        # USB, so only the libusb line says the probe was there.
        if OPENOCD_ACCESS_REFUSED_MARKER in lower and os.name != "nt":
            return "adapter_access_denied"
        if contains_any(lower, ["adapter not found", "no adapter", "no device found", "unable to open", "open failed", "libusb_open"]):
            return "adapter_not_found"
        if contains_any(lower, ["target not examined", "target not detected", "unable to connect", "failed to read"]):
            return "target_not_detected"
        # Ahead of the verify and reset rules and of the broad flash bucket at the
        # bottom, because it is more specific than any of them: this is the line
        # OpenOCD wrote about the operation that actually stopped. Without it an
        # erase OpenOCD refused was `flash_failed`, whose causes and remedy are
        # about a wrong image or a wrong address, and one incidental reset word
        # anywhere in the transcript turned it into `reset_failed` (#333).
        if contains_any(lower, OPENOCD_ERASE_FAILURE_MARKERS):
            return "flash_erase_failed"
        if "verify" in lower and contains_any(lower, ["failed", "mismatch", "error"]):
            return "verify_failed"
        if reports_reset_failure(output):
            return "reset_failed"
        if contains_any(lower, ["can't find", "couldn't find", "couldn't open", "not found"]):
            return "config_file_not_found"
        if tool == "flash_firmware" and contains_any(lower, FAILURE_WORDS):
            return "flash_failed"
        # The twin of the flash bucket above, anchored on the operation rather
        # than on a word: when the tool is `reset_target`, the operation that
        # reported a failure is a reset, whatever OpenOCD wrote about it, and
        # OpenOCD is the backend likeliest to report one across two lines with
        # the reset named in a Jim traceback rather than in the error itself
        # (#333).
        if tool == "reset_target" and contains_any(lower, FAILURE_WORDS):
            return "reset_failed"
        return "unknown_debugger_error"

    def _public_error_type(self, backend_error_type: str) -> str:
        return BACKEND_ERROR_TO_PUBLIC_ERROR.get(backend_error_type, backend_error_type)

    def _failure_summary(self, backend_error_type: str, error_type: str) -> str:
        """The sentence for this failure, with the configured field named where one is known.

        The two script buckets are the one case where the public error_type is
        less specific than the classification behind it: both are
        `debugger_config_not_found`, and the generic sentence for it sends a
        reader to check "the configuration file" when this backend takes two.
        The field whose value OpenOCD said it could not find is the first thing
        the operator has to look at, so it is in the sentence rather than only in
        `backend_error_type` (#506). A probe this user may not open is the other
        such case: `adapter_not_found` covers it, and the sentence for it would
        leave the reader looking for a probe that is plugged in."""
        if backend_error_type == "adapter_access_denied":
            return "OpenOCD was refused permission to open the debug probe (LIBUSB_ERROR_ACCESS): the probe is attached, and this user may not open its USB device."
        field = OPENOCD_CONFIG_FIELD_BY_BACKEND_ERROR.get(backend_error_type)
        if field is None:
            return self._summary_for_error(error_type)
        return f"OpenOCD could not find the script `{field}` names ({getattr(self.config.debugger, field)})."

    def _summary_for_error(self, error_type: str) -> str:
        return {
            "debugger_not_found": "Debugger executable could not be found.",
            "debugger_config_not_found": "Debugger configuration file could not be found.",
            "adapter_not_found": "Debugger adapter could not be found or opened.",
            "target_not_detected": "Debugger could not detect the target.",
            "target_state_unconfirmed": "OpenOCD exited without reporting the outcome, so the target's state is unknown.",
            "flash_failed": "Debugger failed to flash the firmware.",
            "flash_erase_failed": "OpenOCD could not erase the flash sectors this image covers, so the flash contents are unconfirmed.",
            "verify_failed": "Debugger failed to verify the flashed firmware.",
            "reset_failed": "Debugger failed to reset the target.",
            "timeout": "Debugger command timed out.",
            "debugger_command_rejected": "OpenOCD refused the command before it opened the debug probe, so the target was not touched.",
            "debugger_error": "Debugger failed with an unknown error.",
        }.get(error_type, "Debugger failed with an unknown error.")

    def _likely_causes(self, error_type: str) -> list[str]:
        return {
            "target_not_detected": ["DUT is not powered", "wrong interface configuration", "SWD/JTAG wiring issue", "debug probe already in use"],
            "target_state_unconfirmed": ["OpenOCD exited successfully without the success marker this backend echoes at the end of the command; operation_result names which markers did print", "debuggers.<name>.executable is a wrapper that discards OpenOCD's output", "OpenOCD's output was redirected away from the process it was started as"],
            "adapter_not_found": ["debug probe is not connected", "debug probe driver is missing", "debug probe is already in use", "Windows USB driver is not bound to the ST-Link adapter"],
            "verify_failed": ["flash write did not persist correctly", "wrong target configuration", "firmware image does not match target memory layout"],
            "flash_failed": ["target flash is locked", "wrong target configuration", "firmware image is invalid for this target"],
            # The first two are the refused-erase causes #327 measured on the
            # ST-Link path and they carry over unchanged, because they are
            # properties of the device and not of the tool talking to it. The
            # third is OpenOCD's own: the flash bank a target_cfg declares is
            # what OpenOCD erases by, and a bank whose sectors do not describe
            # this part fails at the erase rather than at the connect.
            "flash_erase_failed": ["the sectors this image covers are protected (write protection, PCROP, or a read-out protection level that refuses the erase)", "the core was still executing from flash when the erase was issued", "the flash bank in debuggers.<name>.target_cfg does not match this device's sector layout"],
            "reset_failed": ["reset line wiring issue", "target is not responding", "wrong reset configuration"],
            "timeout": ["debugger stopped responding", "debug probe or target is stuck", "timeout_s is too low for this operation"],
            "debugger_not_found": ["debuggers.<name>.executable is not configured", "debugger executable is not installed", "debugger executable is not in PATH"],
            "debugger_config_not_found": ["debugger interface configuration is missing", "debugger target configuration is missing", "debugger search path is incomplete"],
            "debugger_command_rejected": ["this OpenOCD build does not know the command that was sent", "a configuration script used a run-stage command before 'init'", "the installed OpenOCD is older or newer than the one the command was written for"],
        }.get(error_type, ["inspect the debugger log for details"])


def summary_with_carried_warnings(result: JsonObject, summary: str) -> str:
    """The tool's own summary, saying that failure-worded lines came with it.

    The summary is the one line a person reads, so a run that succeeded while
    OpenOCD printed something alarming has to say so there rather than only in a
    field somebody may not open. It says how many and where they are; the lines
    themselves stay verbatim in `backend_warnings`, and nothing is judged for the
    reader beyond the outcome the marker already settled.
    """
    warnings = result.get("backend_warnings") or []
    if not warnings:
        return summary
    lines = "line" if len(warnings) == 1 else "lines"
    return f"{summary} OpenOCD printed {len(warnings)} failure-worded {lines} in a run its own success marker confirmed; they are carried verbatim in backend_warnings."


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


# Run ahead of the recovery's `reset halt`, with its failure caught (#621).
# Recorded on the bench after a flash killed mid-write: the next OpenOCD finds
# the in-circuit debugger answering USB but every debug-port access failing, and
# waiting does not clear it. A `reset halt` sent then writes its halt request and
# vector catch through that connection, the writes fail silently, and the
# adapter's own reset that follows brings the connection back with the core
# running, so the reset times out waiting for a halt. The reset run first is the
# one that brings the connection back, and the `reset halt` after it sets its
# vector catch and halts. It runs every time, because whether `init` examined
# the target does not tell the state apart: OpenOCD 0.12.0 examined nothing,
# 0.11.0 counted the target as examined while every access still failed. On a
# healthy connection it is one more reset into halt, so the target never runs
# between the two, and nothing in it assumes a reset line is wired: it is the
# reset the configuration already does.
OPENOCD_RECONNECTING_RESET = "catch {reset halt}; "


def openocd_recovery_reset_halt_command(marker: str) -> str:
    """The recovery's reset into halt: `openocd_reset_command("halt")` with the reconnecting reset ahead of it."""
    return f'{OPENOCD_INIT_PREFIX}{OPENOCD_RECONNECTING_RESET}reset halt; echo "{marker}"; shutdown'


def rejected_openocd_commands(openocd_command: str, output: str, configuration_commands: tuple[str, ...] = ()) -> list[str]:
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

    `configuration_commands` are the `-c` values this backend puts on the
    command line ahead of `openocd_command`, the probe selection. OpenOCD
    evaluates its arguments in order and stops at the first that fails, so one
    of them refused is proof that the `init` in `openocd_command` never ran.
    Their names count as sent: OpenOCD 0.11 answers a selector the loaded
    adapter driver does not register, such as `hla_serial` beside the jlink
    driver, with `invalid command name`. And it refuses `adapter serial` naming
    only the words after the group, so that command is the one whose words
    after its first are exactly the words OpenOCD quoted, named by its group and
    subcommand.

    Everything else stays unconfirmed and keeps quarantining: an adapter that
    would not open, a target that would not answer, a reset that was issued and
    not confirmed, a timeout. None of those can prove where they stopped."""
    if OPENOCD_INIT_STAGE_MARKER in output:
        return []
    sent = {segment.strip().split(" ", 1)[0].strip() for segment in (*openocd_command.split(";"), *configuration_commands)}
    named = {match.group(1) for pattern in (OPENOCD_UNREGISTERED_COMMAND, OPENOCD_WRONG_STAGE_COMMAND) for match in pattern.finditer(output)}
    quoted = {match.group(1) for match in OPENOCD_UNKNOWN_SUBCOMMAND.finditer(output)}
    subcommands = {f"{group} {words.split(' ', 1)[0]}" for group, _, words in (command.strip().partition(" ") for command in configuration_commands) if words in quoted}
    return sorted((named & sent) | subcommands)


def openocd_path_for_command(value: str) -> str:
    return value.replace("\\", "/") if Path(value).anchor.startswith("\\") else value


def escape_tcl_double_quoted_word(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$").replace("[", "\\[").replace("]", "\\]")
