"""Scripted picotool subprocess for the #917 acceptance tests."""

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

PREFIX = "AGENTIC_HIL_FAKE_PICOTOOL_"
COMMANDS = {"info", "load", "reboot"}
FORBIDDEN = {
    "--bus", "--address", "--vid", "--pid", "--ignore-partitions",
    "erase", "otp", "config", "partition", "seal", "encrypt",
}
# picotool 2.3.1 transcripts supplied with the test specification.
DEVICE_INFORMATION = {
    "RP2040": (
        "Device Information\n"
        " type:        RP2040\n"
        " flash size:  2048K\n"
        " flash id:    0xE6614103E7A1B2C3\n"
    ),
    "RP2350": (
        "Device Information\n"
        " type:                 RP2350\n"
        " package:              QFN60\n"
        " chipid:               0x8c2d5e1f4a3b6971\n"
        " flash devinfo:        0x0c00\n"
        " current cpu:          ARM\n"
        " available cpus:       ARM, RISC-V\n"
        " default cpu:          ARM\n"
        " secure boot:          0\n"
        " debug enable:         1\n"
        " secure debug enable:  1\n"
        " flash size:           4096K\n"
    ),
}
WRITE_COMPLETE = (
    "Loading into Flash:   [                              ]  0%\r"
    "Loading into Flash:   [==============================]  100%\r\n"
)
VERIFY_COMPLETE = (
    "Verifying Flash:      [                              ]  0%\r"
    "Verifying Flash:      [==============================]  100%\r\n"
    "  OK\n"
)
FORCE_ENTRY = "The device was asked to reboot into BOOTSEL mode so the command can be executed."
FLASH_START = "The device was rebooted to start the application.\n"
FORCED_RETURN = "The device was asked to reboot back into application mode.\n"
LEFT_ACCESSIBLE = (
    "The device has been left accessible, but without the drive mounted; use 'picotool reboot' "
    "to reboot into regular BOOTSEL mode or application mode.\n"
)


def setting(name, default=""):
    return os.environ.get(PREFIX + name, default)


def refuse(reason):
    print(f"fake picotool: {reason}", file=sys.stderr)
    return 97


def command_and_serial(argv):
    if argv == ["version"] or argv == ["version", "-s"]:
        return "version", None
    if argv.count("--ser") != 1:
        return None, None
    index = argv.index("--ser")
    if index + 1 >= len(argv):
        return None, None
    serial = argv[index + 1]
    if not serial or serial.startswith(("-", "@")):
        return None, None
    remaining = argv[:index] + argv[index + 2:]
    commands = [item for item in remaining if item in COMMANDS]
    if len(commands) != 1 or any(item.split("=", 1)[0] in FORBIDDEN for item in remaining):
        return None, None
    command = commands[0]
    prohibited = {"info": {"-f", "-F"}, "load": {"-n", "-N", "-u"}, "reboot": {"-c", "-g"}}
    if prohibited[command].intersection(remaining):
        return None, None
    return command, serial


def image_path(argv):
    supported = {".uf2", ".elf", ".bin"}
    return next((Path(arg) for arg in argv if Path(arg).suffix.lower() in supported), None)


def record(argv, command, serial, child_pid=None):
    log = setting("LOG")
    if not log:
        return
    entry = {"argv": argv, "command": command, "serial": serial, "pid": os.getpid(), "child_pid": child_pid}
    if serial is not None and serial == setting("WATCH_SERIAL"):
        from support import process_is_running

        entry["live_predecessors"] = [pid for pid in json.loads(setting("WATCH_PIDS", "[]")) if process_is_running(pid)]
    if command == "load":
        path = image_path(argv)
        if path is not None and path.is_file():
            entry.update(artifact=str(path), sha256=hashlib.sha256(path.read_bytes()).hexdigest())
    with Path(log).open("ab", buffering=0) as stream:
        stream.write((json.dumps(entry) + "\n").encode("utf-8"))


def board_state(serial):
    boards = json.loads(setting("BOARDS", "{}"))
    if boards:
        board = boards.get(serial, {"state": "absent"})
    elif serial == setting("SERIAL", "PicoTestAbCd1234"):
        board = {"state": setting("STATE", "bootsel"), "chip": setting("CHIP", "RP2350")}
    else:
        board = {"state": "absent"}
    state_file = setting("STATE_FILE")
    states = json.loads(Path(state_file).read_text(encoding="utf-8")) if state_file and Path(state_file).is_file() else {}
    return states.get(serial, board["state"]), board.get("chip", "RP2350"), states


def remember(states, serial, state):
    if state_file := setting("STATE_FILE"):
        states[serial] = state
        Path(state_file).write_text(json.dumps(states), encoding="utf-8")


def not_found(serial, chip, running, command):
    print(f"No accessible RP-series devices in BOOTSEL mode were found with serial number {serial}.")
    if running:
        suffix = "-f to force the reboot." if command == "reboot" else "-f (or -F) to force reboot in order to run the command."
        print(f"\nbut:\n\n{chip} device at bus 1, address 2 appears to have a USB serial connection, so consider {suffix}")
    return -7


def flash(argv, chip, from_application):
    if from_application:
        sys.stdout.write(FORCE_ENTRY)
        retries = int(setting("WAIT_RETRIES", "0"))
        if retries >= 1:
            sys.stdout.write("\nWaiting for device to reboot" + "..." * (retries - 1))
        sys.stdout.write("\n\n")
    if chip == "RP2350" and (family := setting("ABSOLUTE_FAMILY")):
        sys.stdout.write(f"Family ID {family} can be downloaded in absolute space:\n  00000000->00400000\n")
    sys.stdout.write(WRITE_COMPLETE)
    if "-v" in argv:
        sys.stdout.write(VERIFY_COMPLETE)
    if "-x" in argv:
        sys.stdout.write("\n" + FLASH_START)
        return "application"
    if from_application and "-F" in argv:
        sys.stdout.write("\n" + LEFT_ACCESSIBLE)
        return "bootsel"
    if from_application:
        sys.stdout.write("\n" + FORCED_RETURN)
        return "application"
    return "bootsel"


def main(argv):
    command, serial = command_and_serial(argv)
    hangs = serial is not None and command == setting("HANG_COMMAND") and serial == setting("HANG_SERIAL", serial)
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(3600)"]) if hangs else None
    record(argv, command, serial, child.pid if child else None)
    if child:
        release = setting("HANG_RELEASE")
        if release:
            while not Path(release).is_file():
                time.sleep(0.02)
            child.terminate()
            child.wait()
        else:
            child.wait()
    if command is None:
        return refuse("disallowed command, selector or option")
    if command == "load":
        # Preserve lone CR frames and the final CRLF on Windows too.
        sys.stdout.reconfigure(encoding="utf-8", newline="\n")
    if command == setting("RAW_COMMAND"):
        artifact = str(image_path(argv) or "") if command == "load" else ""
        sys.stdout.write(setting("STDOUT").replace("{artifact}", artifact))
        sys.stderr.write(setting("STDERR").replace("{artifact}", artifact))
        return int(setting("EXIT", "0"))
    if command == "version":
        version = setting("VERSION", "2.3.1")
        print(version if "-s" in argv else f"picotool v{version} (Windows, GNU-14.2.0, Release)")
        return 0

    state, chip, states = board_state(serial)
    forced = "-f" in argv or "-F" in argv
    if state in {"absent", "no-reset-interface"} or (state == "application" and not forced):
        return not_found(serial, chip, state == "application", command)
    if command == "info":
        sys.stdout.write(DEVICE_INFORMATION[chip])
    elif command == "load":
        after = flash(argv, chip, state == "application")
        if after != state:
            remember(states, serial, after)
    else:
        into_bootsel = "-u" in argv
        mode = "BOOTSEL" if into_bootsel else "application"
        verb = "was asked to reboot" if state == "application" else "was rebooted"
        print(f"The device {verb} into {mode} mode.")
        remember(states, serial, "bootsel" if into_bootsel else "application")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
