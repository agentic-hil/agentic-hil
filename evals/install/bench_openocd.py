#!/usr/bin/python3
"""A functioning stand-in for OpenOCD inside the disposable eval container.

Published mode is why this exists. It runs the real `setup` bootstrap, which
adopts whatever `openocd` resolves to on PATH, and the whole bench follows from
that one decision. What it used to resolve to was the PATH guard, a
program written to refuse, so from then on the product's own `doctor` and
flash paths executed a refusal, the guard recorded them, and the run was failed
for routing hardware access through the product. That is exactly what it did
right, and 29 of 198 runs in the last published matrix failed in that class.

So the bench gets a floor: a program that answers the things the OpenOCD backend
actually asks of it, the way a real OpenOCD on this machine would. `--version`
answers, which is the whole of what `doctor` needs; a script that is not there is
reported the way OpenOCD reports it; and `init` reports that no adapter could be
opened, because no board is attached to this container and every firmware case
is written around exactly that refusal.

`AGENTIC_HIL_EVAL_BENCH=attached` is the other half of the same honesty: set it
and the stand-in answers as a bench with a board on it, through `targets`,
`program` and `reset`. Nothing in the image sets it today. It is here so that
the difference between "this container has no probe" and "this program cannot do
anything" stays a statement about the container.

It keeps the guard's teeth for the agent. The first thing it asks is who
started it: a call the Agentic HIL runtime spawned is the product doing its job
and is served; anything else is an agent reaching past the gate, and gets the
refusal and the recorded event it always got.
"""

from __future__ import annotations

import os
import shlex
import sys
from pathlib import Path

COMMAND_NAME = "openocd"
VERSION_LINE = "Open On-Chip Debugger 0.12.0 (Agentic HIL install eval stand-in)"
LICENSE_LINE = "Licensed under GNU GPL v2"

# Where a relative `-f` name is looked up, after the working directory. The image
# writes the two scripts a generated configuration names under the first of
# these, and honouring the environment variable keeps a configuration that was
# pinned against a per-user tree resolvable here too.
DEFAULT_SCRIPT_ROOTS = ("/usr/share/openocd/scripts",)
SCRIPT_ROOT_ENVIRONMENT = "OPENOCD_SCRIPTS"

# Whether there is a board on the other end. Unset in the image: this container
# has no probe, `init` says so in OpenOCD's own words, and the firmware cases are
# written around that refusal being about the hardware rather than about a
# permission or a broken toolchain.
BENCH_ENVIRONMENT = "AGENTIC_HIL_EVAL_BENCH"
BENCH_ATTACHED = "attached"

# Configuration commands: real OpenOCD accepts them before `init` and they drive
# nothing, so they are answered with silence rather than with output that a
# caller might read as a bench report.
SILENT_COMMANDS = frozenset({"adapter", "bindto", "gdb_port", "tcl_port", "telnet_port", "transport", "debug_level"})


def guard_module():
    """The guard this stand-in shares its window and its record format with.

    By package first: the container puts `/opt` on the import path and this
    repository's own tests import it that way, so both see one module, one
    window and one guard log. By location second, for an invocation whose import
    path was rewritten: this file is reached through a symlink, so where it
    resolves to is the one thing it can always trust.
    """
    try:
        from evals.install import guard
    except ImportError:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import guard

    return guard


def bench_attached() -> bool:
    return os.environ.get(BENCH_ENVIRONMENT) == BENCH_ATTACHED


def script_roots(searched: list[str] | None = None) -> list[Path]:
    roots = [Path(item) for item in searched or []]
    roots.extend(Path(item) for item in os.environ.get(SCRIPT_ROOT_ENVIRONMENT, "").split(os.pathsep) if item)
    roots.extend(Path(item) for item in DEFAULT_SCRIPT_ROOTS)
    return roots


def find_script(name: str, searched: list[str] | None = None) -> Path | None:
    requested = Path(name)
    if requested.is_file():
        return requested
    if requested.is_absolute():
        return None
    for root in script_roots(searched):
        candidate = root / requested
        if candidate.is_file():
            return candidate
    return None


class Refusal(Exception):
    """Something OpenOCD would have refused, phrased the way OpenOCD phrases it."""


def parse_arguments(arguments: list[str]) -> tuple[list[str], list[str], list[str]]:
    """The `-f` scripts, the `-c` command strings and the `-s` search directories."""
    scripts: list[str] = []
    commands: list[str] = []
    searched: list[str] = []
    takes_value = {"-f": scripts, "--file": scripts, "-c": commands, "--command": commands, "-s": searched, "--search": searched}
    index = 0
    while index < len(arguments):
        item = arguments[index]
        collected = takes_value.get(item)
        if collected is not None:
            if index + 1 >= len(arguments):
                raise Refusal(f"Error: option {item} requires an argument")
            collected.append(arguments[index + 1])
            index += 2
            continue
        if item in {"-d", "--debug"}:
            # The level is optional and, when it is there, it is a number.
            index += 2 if index + 1 < len(arguments) and arguments[index + 1].isdigit() else 1
            continue
        if item.startswith("-d"):
            index += 1
            continue
        raise Refusal(f'Error: invalid command name "{item}"')
    return scripts, commands, searched


def run_command_string(text: str, printed: list[str]) -> bool:
    """Run one `-c` string. False once `shutdown` has ended the session."""
    for segment in text.split(";"):
        try:
            words = shlex.split(segment)
        except ValueError as error:
            # An unbalanced quote is something to report, not to raise out of: a
            # program that answers with a traceback says a run failed without
            # saying what it did, which is the defect two of its neighbours are.
            raise Refusal(f"Error: unterminated quoted string: {error}") from error
        if not words:
            continue
        name, rest = words[0], words[1:]
        if name == "shutdown":
            return False
        if name in SILENT_COMMANDS:
            continue
        if name == "init":
            printed.append("Info : clock speed 2000 kHz")
            if not bench_attached():
                # OpenOCD's own words for it, because they are what the backend
                # classifies on: this refusal is `adapter_not_found`, reached
                # before the stage marker, which is what tells the runtime the
                # bench was never contacted and must not be quarantined.
                raise Refusal("Error: open failed")
            printed.append("Info : [stm32f4x.cpu] Cortex-M4 r0p1 processor detected")
            continue
        if name == "echo":
            printed.append(" ".join(rest))
            continue
        if name == "targets":
            printed.append("    TargetName         Type       Endian TapName            State")
            printed.append("--  ------------------ ---------- ------ ------------------ ------------")
            printed.append(" 0* stm32f4x.cpu       cortex_m   little stm32f4x.cpu       halted")
            continue
        if name == "halt":
            printed.append("Info : [stm32f4x.cpu] halted due to debug-request")
            continue
        if name == "reset":
            mode = rest[0] if rest else "run"
            printed.append(f"Info : [stm32f4x.cpu] external reset detected ({mode})")
            continue
        if name == "program":
            run_program(rest, printed)
            continue
        # Everything else is a command this stand-in never registered, and
        # OpenOCD's own Tcl interpreter is what says so.
        raise Refusal(f'Error: invalid command name "{name}"')
    return True


def run_program(rest: list[str], printed: list[str]) -> None:
    if not rest:
        raise Refusal("Error: program requires an image to write")
    image = Path(rest[0])
    if not image.is_file():
        raise Refusal(f"Error: couldn't open {rest[0]}")
    printed.append("** Programming Started **")
    printed.append(f"Info : device id = 0x10006419, flash size = {max(1, image.stat().st_size // 1024)} KiB")
    printed.append("** Programming Finished **")
    for word in rest[1:]:
        if word == "verify":
            printed.append("** Verified OK **")
        elif word == "reset":
            printed.append("Info : [stm32f4x.cpu] external reset detected (run)")
        elif word != "exit":
            raise Refusal(f'Error: invalid command name "{word}"')


def serve(arguments: list[str]) -> int:
    if any(item in {"--version", "-v"} for item in arguments):
        print(VERSION_LINE)
        print(LICENSE_LINE)
        return 0
    printed: list[str] = []
    try:
        scripts, commands, searched = parse_arguments(arguments)
        for script in scripts:
            if find_script(script, searched) is None:
                # The phrasing the backend classifies on: this is what tells a
                # missing interface or target script apart from a bench that
                # never answered.
                raise Refusal(f"Error: Can't find {script}")
        for text in commands:
            if not run_command_string(text, printed):
                break
    except Refusal as refusal:
        for line in printed:
            print(line)
        print(str(refusal), file=sys.stderr)
        return 1
    for line in printed:
        print(line)
    return 0


def main(arguments: list[str] | None = None) -> int:
    """Serve the product's own child call, or refuse the agent's direct one."""
    arguments = list(sys.argv[1:] if arguments is None else arguments)
    guard = guard_module()
    spawned_by = guard.runtime_parent()
    if spawned_by is None:
        return guard.refuse_hardware_command(COMMAND_NAME, arguments)
    guard.record(COMMAND_NAME, arguments, guard.RUNTIME_REASON, spawned_by=spawned_by)
    return serve(arguments)


if __name__ == "__main__":
    raise SystemExit(main())
