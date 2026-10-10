"""The esptool backend: ESP32-family chips flashed and reset through their ROM bootloader.

esptool reaches the chip over the serial line of the board's USB-UART bridge and
nothing else. It pulls the chip into its ROM bootloader through the bridge's DTR
and RTS lines (the auto-reset circuit every ESP32 development board carries),
talks to that bootloader, and lets the chip go again. So this backend has no
probe of its own: its identity, its lock and its device are the `com_ports`
entry `debuggers.<name>.com_port` names, and a debug interface to the CPU is not
something it can offer at all.

esptool is GPLv2+ and runs here as a subprocess only. Nothing in this module
imports it, so `agentic-hil` stays installable without it and its licence stays
the subprocess's.
"""

from __future__ import annotations

import dataclasses
import difflib
import json
import os
import re
import shutil
import tempfile
import time
from collections.abc import Mapping
from pathlib import Path

from agentic_hil.backends.common import (
    CONTACT_UNPROVEN,
    FAILURE_WORDS,
    NOT_CONTACTED,
    CompletedCommand,
    command_for_log,
    contains_any,
    find_esptool,
    invocation,
    not_executable_refusal,
    programmer_output_fields,
    reset_init_unsupported,
    spawn_command,
    which,
)
from agentic_hil.comports import com_port_unbound, verify_port_identity
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
from agentic_hil.types import AgenticHILConfig, JsonObject, com_port_is_unbound

ESPTOOL_NOT_FOUND: JsonObject = {
    "ok": False,
    "backend": "esptool",
    "error_type": "debugger_not_found",
    "backend_error_type": "esptool_not_found",
    "summary": "The esptool executable could not be found.",
    "likely_causes": [
        "esptool is not installed; `pip install agentic-hil[esptool]` installs it into the same environment as agentic-hil",
        "debuggers.<name>.executable names a file that does not exist",
        "esptool is installed in another environment and is not on PATH",
    ],
    # No executable means no process, so nothing was started that could have
    # touched the serial line, let alone the chip behind it.
    **NOT_CONTACTED,
}

# The major version this backend is written against and tested with (5.5.0). The
# command names it sends (`write-flash`, `flash-id`, `--before=default-reset`)
# are esptool 5's spelling; esptool 4 spelt them with underscores and printed
# other success lines, so a 4.x would not merely be untested but would be read
# against markers it never prints. A 6.x is refused for the same reason in the
# other direction: whatever it prints has not been read by anybody here.
ESPTOOL_SUPPORTED_MAJOR = 5
ESPTOOL_SUPPORTED_VERSIONS = "5.x (tested with 5.5.0)"
# `esptool version` prints `esptool v5.5.0` as its banner and the bare version on
# a line of its own; the banner is the one that says whose version it is.
ESPTOOL_VERSION_PATTERN = re.compile(r"esptool(?:\.py)? v(?P<version>(?P<major>\d+)\.\d+(?:\.\d+)?\S*)")
# The serial speed of the flash after the bootloader has synchronised at 115200.
# Fixed rather than configurable: 460800 is the rate esptool's own documentation
# uses for development boards, and the CH340 and CP210x bridges on them carry it.
# The probe and the resets send nothing worth speeding up and stay at 115200.
ESPTOOL_FLASH_BAUD = "460800"

# esptool prints the chip's factory MAC address on every connect, and the MAC is
# a hardware identifier this project keeps out of its reports, logs and bench
# artifacts. Six to eight colon-separated octets (EUI-48 and EUI-64), not inside
# a longer run of them.
MAC_PATTERN = re.compile(r"(?<![0-9A-Fa-f:])[0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5,7}(?![0-9A-Fa-f:])")
REDACTED_MAC = "<redacted-mac>"
# The private directory each run gets. esptool names its configuration file by
# absolute path in its banner, and that path is a host path.
REDACTED_WORKDIR = "<esptool-workdir>"
# Colour is switched off through the environment; this only keeps a stray escape
# sequence from splitting a marker in two when the output is read.
ANSI_PATTERN = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")

# Lines esptool prints only once it is talking to the chip, or is resetting it
# through the bridge's control lines in order to. Lower case, read against
# lower-cased output. One of them in a failed run's transcript means the run got
# past opening the port, so a failure that is otherwise a pre-contact one (a
# usage error, a port that would not open) cannot claim the chip was left alone.
ESPTOOL_CONTACT_MARKERS = [
    "connecting...",
    "connected to ",
    "chip type:",
    "hard resetting",
    "staying in bootloader",
    "changing baud rate",
    "uploading stub",
    "running stub",
    "writing at",
    "wrote ",
    "hash of data verified",
]
# What a failed flash or reset says about itself besides the generic words: a
# chip that went quiet mid-command and a packet that never came.
ESPTOOL_FAILURE_TEXT = [*FAILURE_WORDS, "stopped responding", "timed out"]

# The success lines, each printed by esptool 5.5.0 after the step it names:
# `Chip type:` and `Detected flash size:` by a connect and a flash-id read;
# `Hash of data verified.` after the MD5 of the written region matched the image;
# and one line per `--after` mode, printed only after a successful connect.
# `Hard resetting` covers both of esptool's spellings, "via RTS pin..." on the
# ESP32 and "with a watchdog..." on the chips whose USB interface needs one.
PROBE_CONFIRMATION = ["Chip type:", "Detected flash size:"]
AFTER_CONFIRMATION = {"hard-reset": "Hard resetting", "no-reset": "Staying in bootloader."}
FLASH_CONFIRMATION = "Hash of data verified."

CHIP_TYPE_PATTERN = re.compile(r"^\s*Chip type:\s*(.+?)\s*$", re.MULTILINE)
FLASH_SIZE_PATTERN = re.compile(r"^\s*Detected flash size:\s*(\S+)", re.MULTILINE)
# The choices click lists when `--chip` is refused: "'esp99' is not one of
# 'auto', 'esp8266', 'esp32', ...".
CHIP_CHOICES_PATTERN = re.compile(r"is not one of ((?:'[^']*'(?:,\s*)?)+)")

# esptool erases and writes whole 4 KiB sectors, so an address inside a sector
# erases the bytes in front of it too; esptool says so in a note and carries on.
FLASH_SECTOR_SIZE = 0x1000
# What esptool's `arg_auto_int` (Python's `int(value, 0)`) reads as an address:
# hexadecimal with its prefix, or a decimal without leading zeros.
FLASH_ADDRESS_PATTERN = re.compile(r"0[xX][0-9A-Fa-f]+|0|[1-9][0-9]*")
HEX_RECORD_PATTERN = re.compile(r":[0-9A-Fa-f]*")

BACKEND_ERROR_TO_PUBLIC_ERROR = {
    "esptool_not_found": "debugger_not_found",
    # An esptool whose major version is not the one these markers were read
    # off. The executable this backend needs is not there, which is what
    # `debugger_not_found` says.
    "esptool_version_unsupported": "debugger_not_found",
    # "Could not open <port>, the port is busy or doesn't exist." The serial line
    # is this backend's whole transport, and the COM port tools publish the same
    # failure under this name.
    "port_open_failed": "com_port_open_failed",
    # click refused `--chip` before esptool opened anything.
    "chip_argument_invalid": "target_type_invalid",
    # "This chip is ESP32-S3, not ESP32. Wrong chip argument?" The chip
    # answered, and it is not the family `target_type` names.
    "chip_mismatch": "target_type_invalid",
    # Any other usage error click reports: an argument this backend built that
    # esptool did not accept.
    "usage_error": "debugger_error",
    # Exit status 0 without every line that confirms the operation, published
    # as the stlink backend publishes its own: nothing in the output places
    # where esptool stopped.
    "probe_unconfirmed": "target_state_unconfirmed",
    "flash_unconfirmed": "flash_failed",
    "reset_unconfirmed": "reset_failed",
    "unknown_debugger_error": "debugger_error",
    "esptool_workdir_unavailable": "debugger_error",
}

# The failures esptool reports before it opens the port or while opening it, so
# before the bridge's control lines could have reset anything. Each is a claim
# about where the run stopped only when no contact marker contradicts it.
PRE_CONTACT_BACKEND_ERRORS = frozenset({"port_open_failed", "chip_argument_invalid", "usage_error"})

# The environment esptool and the libraries it is built on read: its own
# defaults for chip, port and baud rate (`ESPTOOL_PORT`, `ESPTOOL_CHIP`,
# `ESPTOOL_BAUD`, ...), its configuration file (`ESPTOOL_CFGFILE`), ESP-IDF's
# variables and the IDE hooks (`ESP_IDE_WS`, `ESPRESSIF_IDE_WS`), and the
# terminal and colour settings of the console library its help and errors go
# through. None of them may decide what a flash does: a port left in an
# operator's shell would otherwise pick the board.
ESPTOOL_ENV_PREFIXES = ("ESPTOOL", "ESP_", "ESPRESSIF_", "IDF_", "RICH_CLICK")
ESPTOOL_ENV_NAMES = frozenset({"FORCE_COLOR", "NO_COLOR", "TTY_COMPATIBLE", "TTY_INTERACTIVE", "TERMINAL_WIDTH", "COLUMNS", "LINES", "CLICOLOR", "CLICOLOR_FORCE"})


def esptool_environment(config_file: str, base: Mapping[str, str] | None = None) -> dict[str, str]:
    """The environment an esptool run gets: this one, without esptool's own settings.

    Every variable esptool or its console library reads is removed, compared
    case-insensitively because Windows is, and the run is pointed at a private,
    empty configuration file, so neither a variable nor an `esptool.cfg`,
    `setup.cfg` or `tox.ini` in some directory esptool searches can add an
    option to the command this backend built. Colour is off and the console is
    wide, so the output is the plain lines the markers are read against, and
    Python writes UTF-8 unbuffered, so a run killed at its deadline still leaves
    what it had printed in the log.
    """
    source = os.environ if base is None else base
    environment = {name: value for name, value in source.items() if not name.upper().startswith(ESPTOOL_ENV_PREFIXES) and name.upper() not in ESPTOOL_ENV_NAMES}
    environment.update(
        {
            "ESPTOOL_CFGFILE": config_file,
            "NO_COLOR": "1",
            "COLUMNS": "400",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
            "PYTHONUNBUFFERED": "1",
        }
    )
    return environment


def strip_ansi(text: str) -> str:
    return ANSI_PATTERN.sub("", text)


def redact_esptool_output(text: str, workdir: str) -> str:
    """`text` without the run's private directory and without any MAC address.

    The directory in each of the spellings it can come back in: as created, made
    absolute, and with its links resolved."""
    spellings = {workdir, os.path.abspath(workdir), os.path.realpath(workdir)}
    for spelling in sorted((value for value in spellings if value), key=len, reverse=True):
        text = text.replace(spelling, REDACTED_WORKDIR)
    return MAC_PATTERN.sub(REDACTED_MAC, text)


def esptool_chip_choices(output: str) -> list[str]:
    """The chip names click listed when it refused `--chip`, in its order."""
    match = CHIP_CHOICES_PATTERN.search(output)
    if match is None:
        return []
    return re.findall(r"'([^']*)'", match.group(1))


def esptool_hex_problem(path: str | Path) -> str | None:
    """Why esptool would not write this Intel HEX file record by record, or None.

    esptool decides what a file is by its first byte, not by its name: a file
    that starts with ':' goes to the intelhex library and is written at the
    addresses its records carry, anything else is written as raw bytes at the
    address it was given. And when the library refuses the file, esptool does
    not refuse it: it falls back to the raw write, so a damaged HEX file would be
    written verbatim, colons and all, at the address of the pair (esptool
    5.5.0, `bin_image.intel_hex_to_bin`). So this file is held to the rules that
    library applies, record by record, before esptool is allowed near it, and to
    a few stricter ones where the library is lenient in a way that would make
    the file mean something else:

    * ASCII only, lines split exactly as Python's universal newlines split them
      (the library reads the file in text mode);
    * every non-empty line is one record: ':', an even number of hex digits and
      nothing else, a byte count that matches, a known record type and a
      checksum that adds up;
    * address records have the shapes the library requires, and there is at
      most one start address record;
    * an end-of-file record is required and nothing may follow it (the library
      stops reading there, so anything after it would be silently dropped);
    * at least one data byte, no byte written twice, and no run of data that
      starts inside a 4 KiB sector: esptool writes each run as an address/file
      pair of its own and erases from the start of the sector, so the bytes in
      front of the run would go. That also keeps two runs out of one sector,
      the overlap esptool itself refuses.
    """
    try:
        data = Path(path).read_bytes()
    except OSError as error:
        return f"the file could not be read ({error.strerror or error})"
    if not data.startswith(b":"):
        return "it does not start with ':', so esptool would write it as raw bytes rather than as the records it holds"
    try:
        text = data.decode("ascii")
    except UnicodeDecodeError:
        return "it contains bytes that are not ASCII"
    spans: list[tuple[int, int]] = []
    offset = 0
    ended = False
    start_records = 0
    for number, line in enumerate(text.replace("\r\n", "\n").replace("\r", "\n").split("\n"), start=1):
        if not line:
            continue
        if ended:
            return f"line {number} follows the end-of-file record"
        if HEX_RECORD_PATTERN.fullmatch(line) is None or (len(line) - 1) % 2:
            return f"line {number} is not an Intel HEX record (':' followed by pairs of hex digits and nothing else)"
        record = bytes.fromhex(line[1:])
        if len(record) < 5 or len(record) != 5 + record[0]:
            return f"line {number} has a byte count that does not match its length"
        count = record[0]
        address = int.from_bytes(record[1:3], "big")
        record_type = record[3]
        payload = record[4:-1]
        if record_type > 5:
            return f"line {number} has the unknown record type {record_type:02X}"
        if sum(record) & 0xFF:
            return f"line {number} has a wrong checksum"
        if record_type == 0:
            if count:
                spans.append((offset + address, offset + address + count - 1))
        elif record_type == 1:
            if count:
                return f"line {number} is an end-of-file record that carries data"
            ended = True
        elif record_type in (2, 4):
            if count != 2 or address:
                return f"line {number} is an extended address record of the wrong shape"
            offset = int.from_bytes(payload, "big") * (16 if record_type == 2 else 65536)
        else:
            if count != 4 or address:
                return f"line {number} is a start address record of the wrong shape"
            start_records += 1
            if start_records > 1:
                return f"line {number} is a second start address record"
    if not ended:
        return "it has no end-of-file record, so it may have been cut short"
    if not spans:
        return "it holds no data"
    runs: list[list[int]] = []
    for start, end in sorted(spans):
        if runs and start <= runs[-1][1]:
            return f"it writes the byte at {start:#010x} more than once"
        if runs and start == runs[-1][1] + 1:
            runs[-1][1] = end
        else:
            runs.append([start, end])
    for start, _end in runs:
        if start % FLASH_SECTOR_SIZE:
            return f"its data at {start:#010x} starts inside a 4 KiB flash sector, and esptool would erase the bytes in front of it"
    return None


def starts_like_intel_hex(path: str | Path) -> bool:
    """Whether esptool would take this file for Intel HEX: its first byte is ':'."""
    try:
        with open(path, "rb") as handle:
            return handle.read(1) == b":"
    except OSError:
        return False


class EsptoolBackend:
    backend_name = "esptool"

    def __init__(self, config: AgenticHILConfig):
        self.config = config
        # Whether the last call that reached the chip left it in its ROM
        # bootloader on purpose: a confirmed `reset_target(mode="halt")`, or a
        # confirmed flash without a reset after it. A probe then stays out of
        # the application too, rather than hard-resetting into it and undoing
        # the halt a caller asked for. Known to this process only: a server
        # started fresh has not seen the halt, so its first probe resets the
        # chip into the application (docs/installation.md says so).
        self._held_in_rom_loader = False
        # `esptool version` per executable, keyed so that a replaced file is
        # asked again. Successes only: a failed check is asked again next time.
        self._version_cache: dict[tuple[str, int, int], JsonObject] = {}

    def reconfigure(self, config: AgenticHILConfig) -> None:
        # The held state belongs to the chip behind the entry it was observed
        # through. Another port, another chip family or another entry may be
        # another board, whose state this process has never seen.
        if config.debugger != self.config.debugger or config.target != self.config.target:
            self._held_in_rom_loader = False
        self.config = config

    def info(self) -> JsonObject:
        resolved = self._resolve_executable()
        if not resolved["ok"]:
            return {"tool": "debugger_info", **resolved}
        version = self._check_version(str(resolved["executable_path"]))
        if not version["ok"]:
            return {"tool": "debugger_info", **version}
        return {
            "ok": True,
            "tool": "debugger_info",
            "backend": self.backend_name,
            "executable": resolved["executable"],
            "version": version["version"],
            "com_port": self.config.debugger.com_port,
            "chip": self.config.debugger.target_type or "auto",
            "summary": "esptool is available.",
        }

    def list_probes(self) -> JsonObject:
        tool = "debugger_probes_list"
        if not self.config.probe_allowed():
            return self._permission_denied(tool, "Debugger probe discovery is disabled by the authoritative config.", self._permission_key("allow_probe"))
        # There is nothing to list. esptool reaches the chip through the
        # board's USB-UART bridge, which is a serial port like any other and is
        # listed by com_ports_list, and probing every port to find a bootloader
        # would reset every board it found.
        return {
            "ok": False,
            "tool": tool,
            "backend": self.backend_name,
            "error_type": "not_supported",
            "summary": (
                "esptool has no debug probe to list: it reaches the ESP32 through the board's USB-UART bridge, the "
                "serial port debuggers.<name>.com_port names. com_ports_list lists this host's serial ports."
            ),
            **remediation_fields("not_supported", self.backend_name),
            **NOT_CONTACTED,
        }

    def probe_target(self) -> JsonObject:
        if not self.config.probe_allowed():
            return self._permission_denied("probe_target", "Probing is disabled by the authoritative config.", self._permission_key("allow_probe"))
        if not self.config.debugger.permissions.allow_reset:
            return self._permission_denied(
                "probe_target",
                "Probing an ESP32 through esptool resets the chip into its ROM bootloader, so it needs allow_reset as well as allow_probe.",
                self._permission_key("allow_reset"),
            )
        # Into the bootloader and out again, unless the chip was left in it on
        # purpose: then it stays there, so a probe never starts an application
        # a caller has stopped. `--no-stub` keeps the probe to the ROM's own
        # commands; nothing is loaded into the chip's RAM for it.
        after = "no-reset" if self._held_in_rom_loader else "hard-reset"
        result, output = self._run_esptool("probe_target", ["--before=default-reset", f"--after={after}", "--no-stub", "flash-id"], [*PROBE_CONFIRMATION, AFTER_CONFIRMATION[after]])
        self._settle_held_state(result, held_on_success=after == "no-reset")
        if result.get("ok"):
            chip_type = CHIP_TYPE_PATTERN.search(output)
            flash_size = FLASH_SIZE_PATTERN.search(output)
            result["target_detected"] = True
            result["chip_type"] = chip_type.group(1) if chip_type else None
            result["flash_size"] = flash_size.group(1) if flash_size else None
            result["held_in_rom_bootloader"] = after == "no-reset"
            result["summary"] = (
                "ESP32 detected through its ROM bootloader and left there, where the last reset or flash had put it."
                if after == "no-reset"
                else "ESP32 detected through its ROM bootloader and reset back into its application."
            )
        return self._write_action_report(result)

    def flash_firmware(self, artifact: JsonObject, reset_after_flash: bool = False) -> JsonObject:
        tool = "flash_firmware"
        if not self.config.debugger.permissions.allow_flash:
            return self._permission_denied(tool, "Flashing is disabled by the authoritative config.", self._permission_key("allow_flash"))
        if self.config.debugger.permissions.allow_raw_debugger_commands:
            return self._exclusive_permission_denied(tool, "Flashing", "allow_raw_debugger_commands")
        if self.config.debugger.permissions.allow_mass_erase:
            return self._exclusive_permission_denied(tool, "Flashing", "allow_mass_erase")

        # Absolute, because esptool runs in a private directory of its own.
        artifact_path = Path(str(artifact["resolved_path"])).absolute()
        artifact_fields = {"source": artifact.get("source", "path"), "path": artifact.get("path"), "sha256": artifact.get("sha256")}
        suffix = artifact_path.suffix.lower()
        if suffix == ".bin":
            address = self.config.debugger.flash_address
            field = f"debuggers.{self.config.debugger_id or '<name>'}.flash_address"
            if address is None:
                return {
                    "ok": False,
                    "tool": tool,
                    "backend": self.backend_name,
                    "error_type": "invalid_argument",
                    "summary": f"Flashing a .bin artifact with esptool requires {field}, the flash offset the image belongs at (an ESP32 application usually at 0x10000).",
                    "field": field,
                    "artifact": artifact_fields,
                    **NOT_CONTACTED,
                }
            if FLASH_ADDRESS_PATTERN.fullmatch(address) is None or int(address, 0) % FLASH_SECTOR_SIZE:
                return {
                    "ok": False,
                    "tool": tool,
                    "backend": self.backend_name,
                    "error_type": "invalid_argument",
                    "summary": (
                        f"{field} is '{address}', and esptool needs a flash offset that starts a 4 KiB sector, written as "
                        "0x-prefixed hexadecimal or plain decimal: esptool erases whole sectors, so an offset inside one "
                        "erases the bytes in front of it."
                    ),
                    "field": field,
                    "artifact": artifact_fields,
                    **NOT_CONTACTED,
                }
            if starts_like_intel_hex(artifact_path):
                return {
                    "ok": False,
                    "tool": tool,
                    "backend": self.backend_name,
                    "error_type": "invalid_argument",
                    "summary": (
                        "This .bin artifact starts with ':', and esptool reads such a file as Intel HEX whatever it is "
                        "called, so it would be written at addresses of its own rather than at the flash offset. Give an "
                        "Intel HEX file the .hex extension, or check that this is the image the build produced."
                    ),
                    "artifact": artifact_fields,
                    **NOT_CONTACTED,
                }
        elif suffix == ".hex":
            problem = esptool_hex_problem(artifact_path)
            if problem is not None:
                return {
                    "ok": False,
                    "tool": tool,
                    "backend": self.backend_name,
                    "error_type": "invalid_argument",
                    "summary": f"This Intel HEX artifact cannot be flashed with esptool: {problem}.",
                    "artifact": artifact_fields,
                    **NOT_CONTACTED,
                }
            # The records carry their own addresses and esptool writes them
            # there; the pair still needs an address, and it is not used.
            address = "0x0"
        else:
            return {
                "ok": False,
                "tool": tool,
                "backend": self.backend_name,
                "error_type": "invalid_argument",
                "summary": (
                    "esptool writes binary images: a .bin at debuggers.<name>.flash_address, or a .hex whose records "
                    "carry their own addresses. Convert an ELF with `esptool elf2image`, and combine bootloader, "
                    "partition table and application into one image with `esptool merge-bin`."
                ),
                "artifact": artifact_fields,
                **NOT_CONTACTED,
            }
        # `--after=no-reset` leaves the chip in its ROM bootloader: esptool
        # prints "Staying in bootloader." and, when the flasher stub was running,
        # soft-resets back into the ROM loader. Never --erase-all, --encrypt,
        # --force or a skip/diff mode: the write is exactly the image, verified.
        after = "hard-reset" if reset_after_flash else "no-reset"
        result, _output = self._run_esptool(
            tool,
            [f"--baud={ESPTOOL_FLASH_BAUD}", "--before=default-reset", f"--after={after}", "write-flash", "--no-progress", address, str(artifact_path)],
            [FLASH_CONFIRMATION, AFTER_CONFIRMATION[after]],
        )
        self._settle_held_state(result, held_on_success=not reset_after_flash)
        result["artifact"] = artifact_fields
        result["verify"] = True
        result["reset_after_flash"] = reset_after_flash
        if result.get("ok"):
            result["held_in_rom_bootloader"] = not reset_after_flash
            result["summary"] = (
                "Firmware flashed, verified, and target reset."
                if reset_after_flash
                else "Firmware flashed and verified. The ESP32 was left in its ROM bootloader, so the application is not running until reset_target runs it."
            )
        return self._write_action_report(result)

    def reset_target(self, mode: str = "run") -> JsonObject:
        allowed_modes = ["run", "halt", "init"]
        if mode not in allowed_modes:
            return {"ok": False, "tool": "reset_target", "error_type": "invalid_argument", "summary": "Invalid reset mode.", "allowed_values": allowed_modes}
        if mode == "init":
            return reset_init_unsupported(self.backend_name, "esptool has no reset-init event script")
        # Both modes are a reset into the ROM bootloader through the auto-reset
        # circuit, confirmed by a flash-id read the bootloader answers, and differ
        # in how the chip is let go: `run` hard-resets it into the application,
        # `halt` leaves it in the bootloader, which is the nearest thing to a
        # halted core this interface has. The application is not running there,
        # and nothing of it can be inspected either.
        after = "no-reset" if mode == "halt" else "hard-reset"
        result, _output = self._run_esptool("reset_target", ["--before=default-reset", f"--after={after}", "--no-stub", "flash-id"], [AFTER_CONFIRMATION[after]])
        self._settle_held_state(result, held_on_success=mode == "halt")
        result["mode"] = mode
        if result.get("ok"):
            result["held_in_rom_bootloader"] = mode == "halt"
            result["summary"] = (
                "ESP32 reset into its ROM bootloader and held there; the application is not running."
                if mode == "halt"
                else "Target reset with mode 'run'; the ESP32 is running its application."
            )
        return self._write_action_report(result)

    # Every typed-debug tool refuses, and says why: there is no CPU debug
    # interface on this transport at all.
    def debug_start_session(self, artifact: JsonObject, mode: str = "attach", timeout_s: float | None = None) -> JsonObject:
        return self._unsupported_debug_tool("debug_start_session")

    def debug_stop_session(self, timeout_s: float | None = None) -> JsonObject:
        return self._unsupported_debug_tool("debug_stop_session")

    def debug_get_session_status(self) -> JsonObject:
        return self._unsupported_debug_tool("debug_get_session_status")

    def debug_set_breakpoint(self, location: JsonObject) -> JsonObject:
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

    def debug_symbol_info(self, symbol: str = "", symbol_elf: JsonObject | None = None) -> JsonObject:
        # Not the offline answer the other backends give either: that one is
        # read out of the ELF this service flashed, and esptool flashes binary
        # images, so there is never an ELF on file to read it from.
        return self._unsupported_debug_tool("debug_symbol_info")

    def debug_symbol_value(self, symbol: str = "", symbol_elf: JsonObject | None = None) -> JsonObject:
        return self._unsupported_debug_tool("debug_symbol_value")

    def debug_dump_symbol_ihex(self, symbol: str = "", output: JsonObject | None = None, symbol_elf: JsonObject | None = None) -> JsonObject:
        return self._unsupported_debug_tool("debug_dump_symbol_ihex")

    def close(self) -> None:
        return None

    def sessionless_debug_tools(self) -> frozenset[str]:
        """None: every typed-debug tool refuses here before anything is opened."""
        return frozenset()

    def opens_debug_sessions(self) -> bool:
        """Never: esptool has no debug interface to the CPU."""
        return False

    def target_support(self) -> JsonObject:
        """Whether this esptool knows the chip family `target_type` names.

        Unset is a configuration of its own here, not a gap: esptool then asks
        the ROM bootloader which chip it is (`--chip auto`). Set, the name is
        put to esptool itself, as `--chip=<name> version`, which click checks
        against the chip list before any command runs and which opens no port.
        A host that cannot answer (no esptool, an unsupported version, a run
        that did not finish) is `undetermined`, which says nothing about the
        configuration and leaves `doctor` green.
        """
        report: JsonObject = {"ok": True, "tool": "debugger_target_support", "backend": self.backend_name}
        chip = self.config.debugger.target_type if self.config.debugger else None
        if not chip:
            return {
                **report,
                "status": "not_configured",
                "target_type": None,
                "summary": (
                    "No debuggers.<name>.target_type is configured, so esptool asks the ROM bootloader which chip it is "
                    "(--chip auto). Set it to the chip family (esp32, esp32s3, esp32c3, ...) to have esptool refuse a "
                    "different chip."
                ),
            }
        report["target_type"] = chip
        resolved = self._resolve_executable()
        if not resolved["ok"]:
            return self._target_support_undetermined(report, chip, "esptool is not installed on this host.")
        executable_path = str(resolved["executable_path"])
        version = self._check_version(executable_path)
        if not version["ok"]:
            return self._target_support_undetermined(report, chip, str(version.get("summary", "esptool's version could not be read.")))
        completed = self._spawn_esptool([*invocation(executable_path), f"--chip={chip}", "version"], min(self.config.debugger.timeout_s, 10))
        if isinstance(completed, dict) or completed.not_found or completed.not_executable or completed.timed_out:
            return self._target_support_undetermined(report, chip, "esptool did not answer the chip check.")
        output = strip_ansi(f"{completed.stdout}{completed.stderr}")
        if completed.returncode == 0:
            return {**report, "status": "supported", "source": "esptool's --chip choices", "summary": f"esptool {version['version']} accepts --chip {chip}."}
        if "invalid value for '--chip'" in output.lower():
            choices = esptool_chip_choices(output)
            return {
                **report,
                "ok": False,
                "status": "unsupported",
                "error_type": "target_type_invalid",
                "summary": f"esptool {version['version']} does not know the chip family '{chip}'.",
                "likely_causes": self._likely_causes("target_type_invalid"),
                **remediation_fields("target_type_invalid", self.backend_name),
                "close_matches": difflib.get_close_matches(chip, choices, n=5, cutoff=0.6),
                "supported_chips": choices,
            }
        return self._target_support_undetermined(report, chip, "esptool's answer to the chip check was not readable.")

    def classify_last_error(self) -> JsonObject:
        return classify_failure_report(self.config, self._likely_causes)

    def _target_support_undetermined(self, report: JsonObject, chip: str, reason: str) -> JsonObject:
        return {
            **report,
            "status": "undetermined",
            "undetermined_reason": reason,
            "summary": f"Whether esptool knows the chip family '{chip}' could not be determined on this host: {reason} This is not a fault in the configuration.",
        }

    def _settle_held_state(self, result: JsonObject, held_on_success: bool) -> None:
        """What the chip's state is known to be after `result`.

        A confirmed call sets it. A failure that proves it never reached the
        chip leaves it as it was, and any other failure forgets it: the chip
        may have been reset into its application or left anywhere in between."""
        if result.get("ok"):
            self._held_in_rom_loader = held_on_success
        elif result.get("target_contacted") is not False:
            self._held_in_rom_loader = False

    def _resolve_executable(self) -> JsonObject:
        configured = self.config.debugger.executable
        if configured:
            has_path_separator = "/" in configured or "\\" in configured
            if Path(configured).is_absolute() or has_path_separator:
                resolved = Path(resolve_work_path(self.config, configured))
                if not resolved.is_file():
                    return dict(ESPTOOL_NOT_FOUND)
                return {"ok": True, "executable": str(resolved), "executable_path": str(resolved)}
            found = which(configured)
            if found is None:
                return dict(ESPTOOL_NOT_FOUND)
            return {"ok": True, "executable": found, "executable_path": found}
        found = find_esptool()
        if found is None:
            return dict(ESPTOOL_NOT_FOUND)
        return {"ok": True, "executable": found, "executable_path": found}

    def _check_version(self, executable_path: str) -> JsonObject:
        """`{"ok": True, "version": ...}` for a supported esptool, or the refusal."""
        try:
            status = os.stat(executable_path)
            cache_key: tuple[str, int, int] | None = (executable_path, status.st_mtime_ns, status.st_size)
        except OSError:
            cache_key = None
        if cache_key is not None and cache_key in self._version_cache:
            return dict(self._version_cache[cache_key])
        completed = self._spawn_esptool([*invocation(executable_path), "version"], min(self.config.debugger.timeout_s, 10))
        if isinstance(completed, dict):
            return completed
        if completed.not_found:
            return dict(ESPTOOL_NOT_FOUND)
        if completed.not_executable:
            return not_executable_refusal(self.backend_name, executable_path, completed)
        if completed.timed_out:
            # `version` opens no port, so a run that hung there reached nothing.
            return {"ok": False, "backend": self.backend_name, "executable": executable_path, "error_type": "timeout", "summary": "The esptool version check timed out.", **NOT_CONTACTED}
        match = ESPTOOL_VERSION_PATTERN.search(strip_ansi(f"{completed.stdout}{completed.stderr}"))
        if completed.returncode != 0 or match is None or int(match.group("major")) != ESPTOOL_SUPPORTED_MAJOR:
            found = match.group("version") if match is not None else None
            return {
                "ok": False,
                "backend": self.backend_name,
                "executable": executable_path,
                "error_type": "debugger_not_found",
                "backend_error_type": "esptool_version_unsupported",
                "summary": f"This backend needs esptool {ESPTOOL_SUPPORTED_VERSIONS}; the configured esptool reports {found or 'no readable version'}.",
                "version": found,
                "supported_versions": ESPTOOL_SUPPORTED_VERSIONS,
                "likely_causes": [
                    "an esptool from another major version is installed (esptool 4 spells its commands and success lines differently)",
                    "debuggers.<name>.executable names something that is not esptool",
                ],
                **NOT_CONTACTED,
            }
        checked: JsonObject = {"ok": True, "version": match.group("version")}
        if cache_key is not None:
            self._version_cache[cache_key] = checked
        return dict(checked)

    def _spawn_esptool(self, args: list[str], timeout_s: float) -> CompletedCommand | JsonObject:
        """Run esptool in a private directory with a private, empty configuration.

        The directory is the run's working directory and holds the file
        `ESPTOOL_CFGFILE` names, and it is removed when the run ends. What comes
        back has the directory's path and every MAC address taken out of it,
        before anything (the log included) sees it."""
        try:
            workdir = tempfile.mkdtemp(prefix="agentic-hil-esptool-")
        except OSError as error:
            return self._workdir_unavailable(error)
        try:
            config_file = Path(workdir) / "esptool.cfg"
            try:
                config_file.write_text("[esptool]\n", encoding="utf-8")
            except OSError as error:
                return self._workdir_unavailable(error)
            completed = spawn_command(args, workdir, timeout_s, esptool_environment(str(config_file)))
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
        return dataclasses.replace(completed, stdout=redact_esptool_output(completed.stdout, workdir), stderr=redact_esptool_output(completed.stderr, workdir))

    def _workdir_unavailable(self, error: OSError) -> JsonObject:
        return {
            "ok": False,
            "backend": self.backend_name,
            "error_type": "debugger_error",
            "backend_error_type": "esptool_workdir_unavailable",
            "summary": "The private directory an esptool run needs could not be created, so esptool was not started.",
            "backend_error": str(error),
            **remediation_fields("debugger_error", self.backend_name),
            **NOT_CONTACTED,
        }

    def _run_esptool(self, tool: str, action_args: list[str], success_text: list[str]) -> tuple[JsonObject, str]:
        """Run one esptool command against the configured port.

        The result, and esptool's output with its colour stripped (empty when
        nothing ran), for the caller that reads more than the markers out of it.
        """
        started_at = utc_now_iso()
        start = time.perf_counter()
        port_id = self.config.debugger.com_port
        port = self.config.debugger.com_port_config
        if port_id is None or port is None:
            # Unreachable through a validated configuration, which refuses an
            # esptool entry without a known com_port. A refusal rather than an
            # assert, because what would follow it is esptool scanning every
            # serial port on the host, resetting whatever it finds.
            return {
                "ok": False,
                "tool": tool,
                "backend": self.backend_name,
                "error_type": "com_port_not_configured",
                "summary": "This esptool entry names no com_ports entry, so there is no serial port to reach the ESP32 through.",
                **remediation_fields("com_port_not_configured", self.backend_name),
                **NOT_CONTACTED,
            }, ""
        if com_port_is_unbound(port):
            return {**com_port_unbound(tool, port_id), "backend": self.backend_name}, ""
        device = str(port.device)
        if device.startswith(("@", "-")):
            return {
                "ok": False,
                "tool": tool,
                "backend": self.backend_name,
                "error_type": "invalid_argument",
                "summary": f"com_ports.{port_id}.device starts with '{device[0]}', which esptool would read as something other than a port name.",
                "field": f"com_ports.{port_id}.device",
                **NOT_CONTACTED,
            }, ""
        resolved = self._resolve_executable()
        if not resolved["ok"]:
            return {"tool": tool, "backend": self.backend_name, "started_at": started_at, **resolved, "finished_at": utc_now_iso(), "elapsed_ms": int((time.perf_counter() - start) * 1000)}, ""
        executable_path = str(resolved["executable_path"])
        version = self._check_version(executable_path)
        if not version["ok"]:
            return {"tool": tool, "started_at": started_at, **version, "finished_at": utc_now_iso(), "elapsed_ms": int((time.perf_counter() - start) * 1000)}, ""
        # The same check com_session_start makes, for the same reason: a device
        # name is an enumeration order, and the board behind it is compared with
        # the one the entry names before its control lines are touched.
        identity = verify_port_identity(self.config, port_id, tool)
        if not identity.get("ok"):
            return {**identity, "backend": self.backend_name, **NOT_CONTACTED}, ""
        chip = self.config.debugger.target_type or "auto"
        args = [*invocation(executable_path), f"--chip={chip}", f"--port={device}", *action_args]
        log_path = str(Path(logs_directory(self.config)) / f"esptool-{timestamp_for_filename()}-{tool}.log")
        completed = self._spawn_esptool(args, self.config.debugger.timeout_s)
        finished_at = utc_now_iso()
        elapsed_ms = int((time.perf_counter() - start) * 1000)
        if isinstance(completed, dict):
            return {"tool": tool, "started_at": started_at, **completed, "finished_at": finished_at, "elapsed_ms": elapsed_ms}, ""
        if completed.not_found:
            return {"tool": tool, "backend": self.backend_name, "started_at": started_at, **ESPTOOL_NOT_FOUND, "finished_at": finished_at, "elapsed_ms": elapsed_ms}, ""
        if completed.not_executable:
            return {"tool": tool, "backend": self.backend_name, "started_at": started_at, **not_executable_refusal(self.backend_name, executable_path, completed), "finished_at": finished_at, "elapsed_ms": elapsed_ms}, ""
        audit_error = self._write_log(log_path, args, completed.stdout, completed.stderr, completed.returncode, completed.timed_out)
        port_fields = {"port_id": port_id, "port_identity": identity["identity"]}
        if completed.timed_out:
            # Killed at its deadline, so its last lines may never have been
            # written: no reading of the output can place where it stopped.
            result = {
                "ok": False,
                "tool": tool,
                "backend": self.backend_name,
                "started_at": started_at,
                "finished_at": finished_at,
                "elapsed_ms": elapsed_ms,
                "error_type": "timeout",
                "summary": "esptool did not finish within debuggers.<name>.timeout_s.",
                "likely_causes": self._likely_causes("timeout"),
                **remediation_fields("timeout", self.backend_name),
                "log_path": display_path(self.config, log_path),
                **CONTACT_UNPROVEN,
                **port_fields,
            }
            return self._finish_log_audit(result, audit_error), ""
        output = strip_ansi(f"{completed.stdout}{completed.stderr}")
        if completed.returncode == 0:
            # Read off the markers alone, all of them or nothing: esptool exits
            # non-zero for every failure it recognises, and its warnings use the
            # same words a failure would.
            confirmation = self._confirm_operation_success(output, success_text)
            if confirmation["confirmed"]:
                result = {
                    "ok": True,
                    "tool": tool,
                    "backend": self.backend_name,
                    "started_at": started_at,
                    "finished_at": finished_at,
                    "elapsed_ms": elapsed_ms,
                    "success_confirmed": True,
                    "operation_result": {"confirmed": True, "matched_success_text": confirmation["matched"]},
                    "summary": "esptool command completed successfully.",
                    "log_path": display_path(self.config, log_path),
                }
            else:
                result = self._failure_result(
                    tool,
                    started_at,
                    finished_at,
                    elapsed_ms,
                    self._unconfirmed_backend_error_type(tool),
                    log_path,
                    completed,
                    output,
                    {"confirmed": False, "expected_success_text": confirmation["expected"], "matched_success_text": confirmation["matched"]},
                )
        else:
            result = self._failure_result(tool, started_at, finished_at, elapsed_ms, self._classify_output(output, tool), log_path, completed, output)
        result.update(port_fields)
        return self._finish_log_audit(result, audit_error), output

    def _failure_result(
        self,
        tool: str,
        started_at: str,
        finished_at: str,
        elapsed_ms: int,
        backend_error_type: str,
        log_path: str,
        completed: CompletedCommand,
        output: str,
        operation_result: JsonObject | None = None,
    ) -> JsonObject:
        error_type = self._public_error_type(backend_error_type)
        result = {
            "ok": False,
            "tool": tool,
            "backend": self.backend_name,
            "started_at": started_at,
            "finished_at": finished_at,
            "elapsed_ms": elapsed_ms,
            "error_type": error_type,
            "backend_error_type": backend_error_type,
            "summary": self._summary_for_error(error_type),
            "likely_causes": self._likely_causes(error_type),
            **remediation_fields(error_type, self.backend_name),
            "log_path": display_path(self.config, log_path),
            **programmer_output_fields(completed),
        }
        if operation_result is not None:
            result["operation_result"] = operation_result
        # A failure esptool reports before or while opening the port proves the
        # chip was not reset, as long as nothing in the transcript says it got
        # further. Everything else reached the bridge's control lines, at least,
        # and may have left the chip anywhere between its application and its
        # bootloader, or a flash half written.
        if backend_error_type in PRE_CONTACT_BACKEND_ERRORS and not contains_any(output.lower(), ESPTOOL_CONTACT_MARKERS):
            result.update(NOT_CONTACTED)
        else:
            result.update(CONTACT_UNPROVEN)
        return result

    def _confirm_operation_success(self, output: str, expected: list[str]) -> JsonObject:
        lower = output.lower()
        matched = [marker for marker in expected if marker.lower() in lower]
        return {"confirmed": len(matched) == len(expected), "matched": matched, "expected": expected}

    def _unconfirmed_backend_error_type(self, tool: str) -> str:
        return {"probe_target": "probe_unconfirmed", "flash_firmware": "flash_unconfirmed", "reset_target": "reset_unconfirmed"}.get(tool, "unknown_debugger_error")

    def _classify_output(self, output: str, tool: str | None = None) -> str:
        """esptool's failure, from its own words, first match wins.

        The usage errors first, because click prints them before esptool does
        anything, and a refused `--chip` is a usage error that names its own
        cause. Then the connect and the chip, then the write."""
        lower = strip_ansi(output).lower()
        if "could not open" in lower and "port is busy or doesn't exist" in lower:
            return "port_open_failed"
        if "invalid value for '--chip'" in lower:
            return "chip_argument_invalid"
        if "usage: esptool" in lower:
            return "usage_error"
        if "wrong chip argument" in lower:
            return "chip_mismatch"
        if contains_any(lower, ["failed to connect to", "wrong boot mode detected", "no serial data received"]):
            return "target_not_detected"
        if "md5 of file does not match data in flash" in lower:
            return "verify_failed"
        if "write failed, the written flash region is empty" in lower:
            return "flash_failed"
        if tool == "flash_firmware" and contains_any(lower, ESPTOOL_FAILURE_TEXT):
            return "flash_failed"
        if tool == "reset_target" and contains_any(lower, ESPTOOL_FAILURE_TEXT):
            return "reset_failed"
        return "unknown_debugger_error"

    def _public_error_type(self, backend_error_type: str) -> str:
        return BACKEND_ERROR_TO_PUBLIC_ERROR.get(backend_error_type, backend_error_type)

    def _summary_for_error(self, error_type: str) -> str:
        return {
            "debugger_not_found": "The esptool executable could not be found.",
            "com_port_open_failed": "esptool could not open the serial port; it is busy, gone, or not accessible to this user.",
            "target_type_invalid": "The chip esptool found, or was told to expect, is not the family debuggers.<name>.target_type names.",
            "target_not_detected": "esptool opened the serial port but the ESP32's ROM bootloader did not answer.",
            "target_state_unconfirmed": "esptool exited without confirming the operation, so the chip's state is unknown.",
            "flash_failed": "esptool failed to flash the firmware.",
            "verify_failed": "esptool wrote the firmware but the flash contents did not match the image.",
            "reset_failed": "esptool failed to reset the chip.",
            "timeout": "esptool command timed out.",
            "debugger_error": "esptool failed with an unknown error.",
        }.get(error_type, "esptool failed with an unknown error.")

    def _likely_causes(self, error_type: str) -> list[str]:
        return {
            "debugger_not_found": ["esptool is not installed; `pip install agentic-hil[esptool]` installs it", "debuggers.<name>.executable names a file that does not exist"],
            "com_port_open_failed": [
                "a COM session or another program holds the port",
                "the board was unplugged or enumerated under another device name",
                "this user may not open the device (on Linux, membership of the group that owns it, usually dialout)",
            ],
            "target_type_invalid": ["debuggers.<name>.target_type names another chip family than the one on the board", "the name is not one of esptool's --chip choices"],
            "target_not_detected": [
                "the board's auto-reset circuit did not put the chip into its bootloader (some boards need BOOT held while EN is pressed)",
                "com_ports.<name>.device is another adapter's port than the ESP32 board's",
                "the board is not powered, or the USB cable carries power only",
            ],
            "target_state_unconfirmed": ["esptool exited successfully without printing every line that confirms the operation; operation_result names which of them did print", "debuggers.<name>.executable is a wrapper that discards esptool's output"],
            "flash_failed": ["the image does not fit the flash or does not belong at debuggers.<name>.flash_address", "the chip stopped responding during the write (power, cable, or a baud rate the bridge does not carry)", "flash encryption or secure boot is enabled on the chip"],
            "verify_failed": ["the flash chip did not keep what was written", "power or signal integrity failed during the write"],
            "reset_failed": ["the chip stopped responding", "the board's auto-reset circuit is not wired to DTR and RTS"],
            "timeout": ["esptool stopped responding", "the chip held the serial line without answering", "debuggers.<name>.timeout_s is too low for this image at the flash baud rate"],
            "debugger_error": ["inspect the esptool log for details"],
        }.get(error_type, ["inspect the esptool log for details"])

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
        result: JsonObject = {"ok": False, "tool": tool, "error_type": "permission_denied", "summary": summary}
        if permission:
            result["summary"] = permission_denied_summary(summary, permission)
            result.update(permission_denied_fields(permission))
            result.update(remediation_fields("permission_denied", permission=permission))
        return result

    def _permission_key(self, key: str) -> str:
        return permission_key("debuggers", self.config.debugger_id, key)

    def _exclusive_permission_denied(self, tool: str, action: str, blocking: str) -> JsonObject:
        return {
            "ok": False,
            "tool": tool,
            "error_type": "permission_denied",
            "summary": exclusive_permission_summary(action, blocking, self.config.debugger_id),
            **exclusive_permission_fields(blocking, self.config.debugger_id),
        }

    def _unsupported_debug_tool(self, tool: str) -> JsonObject:
        return {
            "ok": False,
            "tool": tool,
            "backend": self.backend_name,
            "error_type": "not_supported",
            "summary": (
                "esptool talks to the ESP32's ROM bootloader over the serial line and has no debug interface to the CPU, "
                "so this backend has no debug session, breakpoint, halt or symbol read to offer. Those need a JTAG "
                "connection to the chip, which this backend does not drive."
            ),
            **remediation_fields("not_supported", self.backend_name),
            **NOT_CONTACTED,
        }
