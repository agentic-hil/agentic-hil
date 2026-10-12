#!/usr/bin/env python3
"""A stand-in for esptool 5.5.0 that never opens a serial port.

Every line it prints is esptool 5.5.0's own. The banner, the configuration line,
the connect and the chip report come from `esptool/__init__.py` and
`esptool/loader.py`; the flash-id read, the write, the verify and the reset come
from `esptool/cmds.py`; the error box click draws when it refuses an option, and
the port that will not open, were captured from the real esptool 5.5.0 run
against `--port=COM250`, a port that does not exist. Nothing here reads the
source at run time and nothing touches a device.

It also refuses what the backend must never send, with exit status 97 and a line
starting `fake esptool:` that no test expects to see:

* a command that reaches for a port without naming one with `--port=`, since
  esptool would then scan every serial port on the host and reset whatever
  answered on each of them;
* an erase of the whole chip or a write that skips its own checks
  (`erase-flash`, `--erase-all`, `--force`, `--encrypt`, `--encrypt-files`);
* any run without the private configuration file the backend names in
  `ESPTOOL_CFGFILE`, because without it esptool reads an operator's own
  `esptool.cfg` or `setup.cfg` from the directory it starts in.

An option esptool does not have is refused the way esptool refuses it, with
click's usage box and exit status 2.

Environment, read only here (the backend passes `AGENTIC_HIL_*` through and
strips esptool's own variables):

* `AGENTIC_HIL_FAKE_ESPTOOL_VERSION`: what `version` reports, 5.5.0 by default.
  A 4.x answers in esptool 4's words, `esptool.py v4.8.1`.
* `AGENTIC_HIL_FAKE_ESPTOOL_VERSION_HANG`: `1` makes `version` never answer.
* `AGENTIC_HIL_FAKE_ESPTOOL_CHIP`: the chip on the board, the way esptool names
  it (`ESP32`, `ESP32-S3`), ESP32 by default.
* `AGENTIC_HIL_FAKE_ESPTOOL_SCENARIO`: what a command that opens the port does,
  one of SCENARIOS, `ok` by default.
* `AGENTIC_HIL_FAKE_ESPTOOL_STDOUT`, `_STDERR` and `_EXIT`: a transcript such a
  command prints instead, for the phrases only a board can make esptool say. The
  exit status defaults to 1. The refusals above still come first.
* `AGENTIC_HIL_FAKE_ESPTOOL_LOG`: a file each run appends one JSON line to, with
  its arguments, its working directory, the environment esptool would read and
  the configuration file it was pointed at.
"""

from __future__ import annotations

import json
import os
import sys
import time
import zlib
from pathlib import Path

# `esptool --help` in 5.5.0, in its order.
CHIP_CHOICES = ["auto", "esp8266", "esp32", "esp32s2", "esp32s3", "esp32c3", "esp32c2", "esp32c6", "esp32c61", "esp32c5", "esp32e22", "esp32h2", "esp32h21", "esp32p4", "esp32h4", "esp32s31"]
GLOBAL_VALUE_OPTIONS = {"--chip", "--port", "--baud", "--before", "--after"}
GLOBAL_FLAGS = {"--no-stub"}
FORBIDDEN = {"--erase-all", "--force", "--encrypt", "--encrypt-files", "erase-flash", "erase-region"}
# The box click draws on a terminal 400 columns wide, which is what the backend
# sets COLUMNS to: every line padded to 399 characters, square corners because
# NO_COLOR is set.
BOX_WIDTH = 399
DESCRIPTIONS = {
    "ESP32": ("ESP32-D0WD-V3 (revision v3.1)", "Wi-Fi, BT, Dual Core + LP Core, 240MHz, Vref calibration in eFuse, Coding Scheme None"),
    "ESP32-S3": ("ESP32-S3 (QFN56) (revision v0.2)", "Wi-Fi, BT 5 (LE), Dual Core + LP Core, 240MHz, Embedded PSRAM 8MB (AP_3v3)"),
    "ESP32-C3": ("ESP32-C3 (QFN32) (revision v0.4)", "Wi-Fi, BT 5 (LE), Single Core, 160MHz, Embedded Flash 4MB (XMC)"),
}
# A documentation MAC (Espressif's OUI, a made-up device part), printed so the
# backend's redaction has something to take out.
MAC = "24:0a:c4:12:34:56"
TROUBLESHOOTING = "https://docs.espressif.com/projects/esptool/en/latest/troubleshooting.html"
REFUSED = 97
SCENARIOS = {
    "ok",
    # Stops answering after the connect line, as a board holding the line does.
    "hang",
    # The port open failures esptool reports for a port another program holds
    # and for one that is not there.
    "port_busy",
    "port_missing",
    # The bridge opened, and no bootloader answered behind it.
    "no_serial_data",
    # The write went through and the region read back differs.
    "verify_mismatch",
    # The write went through and the region read back erased.
    "write_empty",
    # The chip went quiet partway through the command.
    "stopped_responding",
    # Exits 0 having printed the banner and nothing else.
    "silent",
}


def chip_name(choice: str) -> str:
    """`esp32s3` as esptool's CHIP_NAME spells it, `ESP32-S3`."""
    upper = choice.upper()
    if upper.startswith("ESP32") and len(upper) > len("ESP32"):
        return f"ESP32-{upper[len('ESP32'):]}"
    return upper


def usage_box(message: str) -> str:
    lines = [
        "",
        " Usage: esptool [OPTIONS] COMMAND [ARGS]...",
        "",
        " Try 'esptool -h' for help",
    ]
    padded = [line.ljust(BOX_WIDTH) for line in lines]
    top = "┌─ Error "
    padded.append(top + "─" * (BOX_WIDTH - len(top) - 1) + "┐")
    padded.append("│ " + message.ljust(BOX_WIDTH - 4) + " │")
    padded.append("└" + "─" * (BOX_WIDTH - 2) + "┘")
    padded.append("".ljust(BOX_WIDTH))
    return "\n".join(padded) + "\n"


def refuse(reason: str) -> int:
    sys.stderr.write(f"fake esptool: {reason}\n")
    return REFUSED


def fatal(message: str) -> int:
    # `_main`: a blank line on stdout, then the message on stderr, exit status 2.
    sys.stdout.write("\n")
    sys.stdout.flush()
    sys.stderr.write(f"ERROR: A fatal error occurred: {message}\n")
    return 2


def record(argv: list[str]) -> None:
    path = os.environ.get("AGENTIC_HIL_FAKE_ESPTOOL_LOG")
    if not path:
        return
    names = {"FORCE_COLOR", "NO_COLOR", "TTY_COMPATIBLE", "TTY_INTERACTIVE", "TERMINAL_WIDTH", "COLUMNS", "LINES", "CLICOLOR", "CLICOLOR_FORCE", "PYTHONIOENCODING", "PYTHONUTF8", "PYTHONUNBUFFERED"}
    prefixes = ("ESPTOOL", "ESP_", "ESPRESSIF_", "IDF_", "RICH_CLICK")
    environment = {name: value for name, value in os.environ.items() if name.upper().startswith(prefixes) or name.upper() in names}
    config_file = os.environ.get("ESPTOOL_CFGFILE")
    config_text = None
    if config_file and Path(config_file).is_file():
        config_text = Path(config_file).read_text(encoding="utf-8")
    entry = {"argv": argv, "cwd": os.getcwd(), "environment": environment, "config_file": config_file, "config_text": config_text}
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry) + "\n")


def banner(version: str) -> str:
    major = version.split(".", 1)[0]
    if major.isdigit() and int(major) < 5:
        return f"esptool.py v{version}\n"
    return f"esptool v{version}\nLoaded custom configuration from {os.environ['ESPTOOL_CFGFILE']} (set with ESPTOOL_CFGFILE)\n"


def parse(argv: list[str]) -> tuple[dict[str, str], set[str], list[str]] | str:
    """The global options, the flags, and the command with its own arguments;
    or the option esptool would refuse, as the box's message."""
    values: dict[str, str] = {}
    flags: set[str] = set()
    index = 0
    while index < len(argv) and argv[index].startswith("-"):
        name, _, value = argv[index].partition("=")
        if name in GLOBAL_VALUE_OPTIONS and value:
            values[name] = value
        elif name in GLOBAL_FLAGS and not value:
            flags.add(name)
        elif name in FORBIDDEN:
            return f"forbidden:{name}"
        else:
            return f"No such option '{name}'."
        index += 1
    return values, flags, argv[index:]


def connect(port: str, wanted: str, attached: str) -> str | None:
    """The connect esptool prints, or the chip it found instead."""
    sys.stdout.write(f"Serial port {port}:\nConnecting....\n")
    if wanted == "auto":
        sys.stdout.write(f"Detecting chip type... {attached}\n")
    elif chip_name(wanted) != attached:
        return f"This chip is {attached}, not {chip_name(wanted)}. Wrong chip argument?"
    description, features = DESCRIPTIONS.get(attached, (f"{attached} (revision v0.0)", "Wi-Fi"))
    sys.stdout.write(f"Connected to {attached} on {port}:\n")
    sys.stdout.write(f"{'Chip type:':<20}{description}\n")
    sys.stdout.write(f"{'Features:':<20}{features}\n")
    sys.stdout.write(f"{'Crystal frequency:':<20}40MHz\n")
    sys.stdout.write(f"{'MAC:':<20}{MAC}\n\n")
    return None


def after_line(after: str) -> str:
    return {"hard-reset": "Hard resetting via RTS pin...\n", "no-reset": "Staying in bootloader.\n"}[after]


def flash_id(after: str) -> int:
    sys.stdout.write("\nFlash Memory Information:\n=========================\nManufacturer: 20\nDevice: 4016\nDetected flash size: 4MB\nFlash voltage set by a strapping pin: 3.3V\n")
    sys.stdout.write("\n" + after_line(after))
    return 0


def write_flash(arguments: list[str], baud: str, after: str, scenario: str) -> int:
    options = [argument for argument in arguments if argument.startswith("-")]
    for option in options:
        if option in FORBIDDEN:
            return refuse(f"{option} is never sent")
        if option != "--no-progress":
            sys.stderr.write(usage_box(f"No such option '{option}'."))
            return 2
    positional = [argument for argument in arguments if not argument.startswith("-")]
    if len(positional) != 2:
        return refuse(f"write-flash takes one address and one file here, not {positional!r}")
    address, file_name = int(positional[0], 0), Path(positional[1])
    if not file_name.is_file():
        return refuse(f"{file_name} is not a file")
    data = file_name.read_bytes()
    sys.stdout.write("Uploading stub flasher...\nRunning stub flasher...\nStub flasher running.\n")
    if int(baud) > 115200:
        sys.stdout.write(f"Changing baud rate to {baud}...\nChanged.\n")
    sys.stdout.write("\nConfiguring flash size...\n")
    if scenario == "stopped_responding":
        return fatal("The chip stopped responding.")
    start = address & ~0xFFF
    end = ((address + len(data) + 0xFFF) & ~0xFFF) - 1
    sys.stdout.write(f"Flash will be erased from {start:#010x} to {end:#010x}...\n")
    compressed = zlib.compress(data, 9)
    seconds = 0.1
    sys.stdout.write(f"Compressed {len(data)} bytes to {len(compressed)}...\n")
    sys.stdout.write(f"Wrote {len(data)} bytes ({len(compressed)} compressed) at {address:#010x} in {seconds:.1f} seconds ({len(data) / seconds * 8 / 1000:.1f} kbit/s).\n")
    sys.stdout.write("Verifying written data...\n")
    if scenario == "verify_mismatch":
        sys.stdout.write("Input MD5:  0123456789abcdef0123456789abcdef\nFlash MD5:  fedcba9876543210fedcba9876543210\n")
        return fatal("MD5 of file does not match data in flash!")
    if scenario == "write_empty":
        sys.stdout.write("Input MD5:  0123456789abcdef0123456789abcdef\nFlash MD5:  f1c9645dbc14efddc7d8a322685f26eb\n")
        return fatal("Write failed, the written flash region is empty.")
    sys.stdout.write("Hash of data verified.\n")
    sys.stdout.write("\n" + after_line(after))
    return 0


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", newline="\n")
    sys.stderr.reconfigure(encoding="utf-8", newline="\n")
    argv = sys.argv[1:]
    record(argv)
    config_file = os.environ.get("ESPTOOL_CFGFILE")
    if not config_file or not Path(config_file).is_file():
        return refuse("ESPTOOL_CFGFILE does not name the private configuration file")
    version = os.environ.get("AGENTIC_HIL_FAKE_ESPTOOL_VERSION", "5.5.0")
    parsed = parse(argv)
    if isinstance(parsed, str):
        if parsed.startswith("forbidden:"):
            return refuse(f"{parsed.split(':', 1)[1]} is never sent")
        sys.stderr.write(usage_box(parsed))
        return 2
    values, flags, command = parsed
    chip = values.get("--chip", "auto")
    if chip not in CHIP_CHOICES:
        choices = ", ".join(f"'{choice}'" for choice in CHIP_CHOICES)
        sys.stderr.write(usage_box(f"Invalid value for '--chip' / '-c': '{chip}' is not one of {choices}."))
        return 2
    if not command:
        return refuse("no command")
    if command[0] in FORBIDDEN:
        return refuse(f"{command[0]} is never sent")
    if command == ["version"]:
        if os.environ.get("AGENTIC_HIL_FAKE_ESPTOOL_VERSION_HANG") == "1":
            time.sleep(60)
        sys.stdout.write(f"{banner(version)}{version}\n")
        return 0
    if "--port" not in values:
        return refuse("a command that opens a port without --port scans every port on the host")
    port = values["--port"]
    after = values.get("--after", "hard-reset")
    if after not in ("hard-reset", "no-reset"):
        return refuse(f"--after={after} is never sent")
    scenario = os.environ.get("AGENTIC_HIL_FAKE_ESPTOOL_SCENARIO", "ok")
    if scenario not in SCENARIOS:
        return refuse(f"unknown scenario {scenario!r}")
    if command[0] not in ("flash-id", "write-flash"):
        return refuse(f"{command[0]} is not a command the backend sends")
    if command[0] == "flash-id" and (command[1:] or "--no-stub" not in flags):
        return refuse("flash-id is sent alone and with --no-stub")
    if command[0] == "write-flash" and "--no-stub" in flags:
        return refuse("write-flash is sent with the flasher stub")

    # The whole of what the run prints, banner included, so the transcript of a
    # failure click reports before esptool starts carries no banner either.
    transcript = os.environ.get("AGENTIC_HIL_FAKE_ESPTOOL_STDOUT"), os.environ.get("AGENTIC_HIL_FAKE_ESPTOOL_STDERR")
    if transcript != (None, None):
        sys.stdout.write(transcript[0] or "")
        sys.stdout.flush()
        sys.stderr.write(transcript[1] or "")
        return int(os.environ.get("AGENTIC_HIL_FAKE_ESPTOOL_EXIT", "1"))
    sys.stdout.write(banner(version))
    if scenario == "silent":
        return 0
    if scenario in ("port_busy", "port_missing"):
        sys.stdout.write(f"Serial port {port}:\n")
        cause = "PermissionError(13, 'Access is denied.', None, 5)" if scenario == "port_busy" else "FileNotFoundError(2, 'The system cannot find the file specified.', None, 2)"
        return fatal(f"Could not open {port}, the port is busy or doesn't exist.\n(could not open port '{port}': {cause})\n\nHint: Check if the port is correct and ESP connected\n")
    if scenario == "hang":
        sys.stdout.write(f"Serial port {port}:\nConnecting...")
        sys.stdout.flush()
        time.sleep(60)
        return 0
    attached = os.environ.get("AGENTIC_HIL_FAKE_ESPTOOL_CHIP", "ESP32")
    if scenario == "no_serial_data":
        sys.stdout.write(f"Serial port {port}:\nConnecting" + "." * 40 + "\n")
        device = "Espressif device" if chip == "auto" else chip_name(chip)
        return fatal(f"Failed to connect to {device}: No serial data received.\nFor troubleshooting steps visit: {TROUBLESHOOTING}")
    mismatch = connect(port, chip, attached)
    if mismatch is not None:
        return fatal(mismatch)
    if command[0] == "flash-id":
        if scenario == "stopped_responding":
            return fatal("The chip stopped responding.")
        return flash_id(after)
    return write_flash(command[1:], values.get("--baud", "115200"), after, scenario)


if __name__ == "__main__":
    sys.exit(main())
