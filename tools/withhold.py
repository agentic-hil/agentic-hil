"""Withhold the probe's serial numbers and this machine's names from a workflow step.

A workflow's log and the artifacts it uploads can be read by anyone who can
read the repository, and the commands that drive the board name it: doctor
prints the probe's serial number and the serial port's link under
/dev/serial/by-id that carries it, the plan's report and the OpenOCD and port
logs repeat them, and paths under the home directory name the machine's user.
`bench_in_container.py` withholds all of that from everything the bench tier
prints and hands back, which covers the gate and the nightly's distributions.
This is the same withholding, its own `Redactor` over the same values, for a
step that drives the board with the installation on the machine instead:

    python3 tools/withhold.py run [--stdout-file FILE] -- COMMAND...
    python3 tools/withhold.py files PATH...

`run` runs the command and prints its standard output and standard error each
to its own stream, line by line, every value replaced by `[withheld]` and no
line removed, and exits with the command's status. `--stdout-file` writes the
standard output to FILE as the command wrote it instead, for a report a later
step reads by the paths in it; the files step covers it before it is uploaded.
`files` rewrites every regular file under each PATH in place where a value
stands in it, byte for byte elsewhere, without following a link, passes over a
PATH that does not exist, and names each file it changed.

What is withheld: the serial number of every in-circuit debugger or programmer
and of every USB-UART adapter attached to this machine, found through sysfs the
way the runner finds them, and this machine's host name, home directory and
user. The adapters count because the product's port inventory lists every
serial port on the machine, theirs among them. With no attached probe showing a
serial number, the one a configuration names is not known here, so both refuse
before they run or change anything: what followed would name the board in
public.

Exit statuses: 2 refused, 1 a file could not be read or written, 127 the
command could not be started, and otherwise the command's own, 128 plus the
signal's number for one a signal ended.
"""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import threading
from contextlib import suppress
from pathlib import Path
from typing import BinaryIO

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bench_in_container import Redactor, discover_probes, discover_usb_uarts, host_identities  # noqa: E402

EXIT_UNREADABLE = 1
EXIT_REFUSED = 2
EXIT_CANNOT_START = 127
# How long the relays get to drain after the command has exited. A process the
# command started and left running can hold its streams open; the step is not
# kept waiting on it.
READER_GRACE_S = 30
ENCODING = "utf-8"
# Bytes that are not UTF-8 go through as they came.
ERRORS = "surrogateescape"


def say(text: str, redact: Redactor) -> None:
    print(f"withhold: {redact(text)}", file=sys.stderr, flush=True)


def what_to_withhold() -> tuple[Redactor, Redactor | None]:
    """This machine's names alone, for this tool's own lines, and with every serial, or None without a probe's."""
    names = host_identities()
    serials = [serial for probe in discover_probes() for serial in probe.serial_numbers]
    adapters = [serial for adapter in discover_usb_uarts() for serial in adapter.serial_numbers]
    return Redactor(names), Redactor([*names, *serials, *adapters]) if serials else None


def refuse(redact: Redactor, outcome: str) -> int:
    say(
        "no in-circuit debugger or programmer attached to this machine shows a serial number, so the one a "
        f"configuration names cannot be withheld from what follows; {outcome}",
        redact,
    )
    return EXIT_REFUSED


def binary(stream: object) -> BinaryIO:
    return getattr(stream, "buffer", stream)  # type: ignore[return-value]


def relay(source: BinaryIO, sink: object, redact: Redactor) -> None:
    """Copy one stream line by line as it arrives, withheld.

    It never stops draining: a line that cannot be written is dropped from
    there, not from the pipe, so the command is never blocked on a full one.
    """
    for raw in iter(source.readline, b""):
        text = redact(raw.decode(ENCODING, ERRORS))
        with suppress(OSError, ValueError):
            sink.flush()  # type: ignore[attr-defined]
            out = binary(sink)
            out.write(text.encode(ENCODING, ERRORS))
            out.flush()


def run(command: list[str], stdout_file: str | None) -> int:
    """The command's status, with what it printed withheld, or a refusal before it ran."""
    names, redact = what_to_withhold()
    if redact is None:
        return refuse(names, "nothing was run")
    child: subprocess.Popen[bytes] | None = None
    pending: list[int] = []

    def keep_waiting(signum: int, frame: object) -> None:
        return None

    def pass_on(signum: int, frame: object) -> None:
        if child is None:
            pending.append(signum)
            return
        with suppress(OSError):
            child.send_signal(signum)

    # As `run_lock.py run` does: an interrupt from a terminal reaches the
    # command as part of the foreground process group, and a termination sent
    # to this process alone is passed on, so the command can close what it
    # opened on the board while this keeps relaying what it says. Handlers, not
    # SIG_IGN, so the command does not inherit them.
    previous = {signal.SIGINT: signal.signal(signal.SIGINT, keep_waiting)}
    if os.name != "nt":
        previous[signal.SIGTERM] = signal.signal(signal.SIGTERM, pass_on)
    report: BinaryIO | None = None
    try:
        if stdout_file is not None:
            try:
                report = open(stdout_file, "wb")  # noqa: SIM115 - closed below, after the command
            except OSError as error:
                say(f"{stdout_file} could not be written, so nothing was run: {error}", redact)
                return EXIT_UNREADABLE
        try:
            child = subprocess.Popen(command, stdout=report or subprocess.PIPE, stderr=subprocess.PIPE)
        except OSError as error:
            say(f"{command[0]} could not be started: {error}", redact)
            return EXIT_CANNOT_START
        for signum in pending:
            with suppress(OSError):
                child.send_signal(signum)
        relays = [threading.Thread(target=relay, args=(child.stderr, sys.stderr, redact), daemon=True)]
        if report is None:
            relays.append(threading.Thread(target=relay, args=(child.stdout, sys.stdout, redact), daemon=True))
        for thread in relays:
            thread.start()
        returncode = child.wait()
        for thread in relays:
            thread.join(READER_GRACE_S)
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
        if report is not None:
            report.close()
    return returncode if returncode >= 0 else 128 - returncode


def regular_files(path: Path) -> list[Path]:
    """Every regular file at or under `path`, no link followed, none for a path that is not there."""
    if path.is_symlink():
        return []
    if path.is_file():
        return [path]
    found: list[Path] = []
    for directory, _subdirectories, names in os.walk(path, followlinks=False):
        for name in sorted(names):
            candidate = Path(directory) / name
            if not candidate.is_symlink() and candidate.is_file():
                found.append(candidate)
    return found


def files(paths: list[str]) -> int:
    """Withhold in place under each path; 0 when every file was read and written."""
    names, redact = what_to_withhold()
    if redact is None:
        return refuse(names, "nothing was changed, and what is under those paths is not fit to upload")
    status = 0
    changed = 0
    for named in paths:
        for path in regular_files(Path(named)):
            try:
                written = path.read_bytes()
                text = written.decode(ENCODING, ERRORS)
                withheld = redact(text)
                if withheld == text:
                    continue
                path.write_bytes(withheld.encode(ENCODING, ERRORS))
            except OSError as error:
                say(f"{path.as_posix()} could not be withheld: {error}", redact)
                status = EXIT_UNREADABLE
                continue
            changed += 1
            say(f"withheld what names this machine or its probe in {path.as_posix()}", redact)
    say(f"{changed} file{'' if changed == 1 else 's'} changed", redact)
    return status


def parse_options(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="withhold.py",
        description="Withhold the probe's serial numbers and this machine's names from a command's output or from files.",
    )
    actions = parser.add_subparsers(dest="action", required=True)

    running = actions.add_parser("run", help="Run a command and print what it prints, withheld.")
    running.add_argument("--stdout-file", help="Write the command's standard output here as it wrote it.")
    running.add_argument("command", nargs=argparse.REMAINDER, help="After --: the command to run.")

    rewriting = actions.add_parser("files", help="Withhold in place in every regular file under each path.")
    rewriting.add_argument("paths", nargs="+", help="Files or directories; one that does not exist is passed over.")

    options = parser.parse_args(argv)
    if options.action == "run":
        if options.command[:1] == ["--"]:
            options.command = options.command[1:]
        if not options.command:
            running.error("name the command to run after --")
    return options


def main(argv: list[str] | None = None) -> int:
    options = parse_options(argv)
    if options.action == "run":
        return run(options.command, options.stdout_file)
    return files(options.paths)


if __name__ == "__main__":
    sys.exit(main())
