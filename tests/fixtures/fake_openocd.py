#!/usr/bin/env python3
"""An OpenOCD that keeps OpenOCD's two stages.

No hardware is faked here. What is faked is the one rule that took the bench out
of service in 0.7.0: OpenOCD registers `reset`, `targets`, `halt` and the rest of
the target commands while `init` runs, so a script that reaches one of them
earlier is refused by the Tcl interpreter with `invalid command name "<command>"`
and the adapter is never opened (seen on OpenOCD 0.12). `program`
needs no `init` because OpenOCD's own proc runs `init` and `reset init` itself.

A fake that answers every command line the same way is why a command line nothing
could execute shipped, so this one executes what it is given: the success markers
come out of the script's own `echo` commands, not out of a substring of the
arguments, and a script that never reaches its `echo` prints nothing.
"""

from __future__ import annotations

import re
import socket
import sys
import time

RUN_STAGE_COMMANDS = frozenset({"reset", "targets", "halt", "resume", "step", "poll", "mdw", "mww", "mdb", "mwb"})
INITIALIZING_COMMANDS = frozenset({"init", "program"})


def serve_gdb_port(port: int) -> int:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", port))
    listener.listen(1)
    print(f"Info : Listening on port {port} for gdb connections", flush=True)
    try:
        while True:
            time.sleep(0.1)
    except KeyboardInterrupt:
        return 0
    finally:
        listener.close()


def evaluate(script: str, initialized: bool) -> tuple[list[str], bool, int | None]:
    """Run one -c script the way OpenOCD's interpreter would.

    Returns the lines it printed, whether the run stage has been reached, and an
    exit code once the script stopped - on `shutdown`, or on the first command
    the interpreter refuses, which aborts the rest of the script."""
    printed: list[str] = []
    for segment in script.split(";"):
        command = segment.strip()
        if not command:
            continue
        verb, _, argument = command.partition(" ")
        if verb in RUN_STAGE_COMMANDS and not initialized:
            printed.append(f'Error: invalid command name "{verb}"')
            return printed, initialized, 1
        if verb in INITIALIZING_COMMANDS:
            initialized = True
        if verb == "echo":
            printed.append(argument.strip().strip('"'))
        if verb == "shutdown":
            return printed, initialized, 0
    return printed, initialized, None


def main() -> int:
    args = sys.argv[1:]
    if "--version" in args:
        print("Open On-Chip Debugger 0.12.0")
        return 0
    gdb_port_match = re.search(r"gdb_port (\d+)", " ".join(args))
    if gdb_port_match:
        return serve_gdb_port(int(gdb_port_match.group(1)))
    initialized = False
    for index, argument in enumerate(args):
        if argument != "-c" or index + 1 >= len(args):
            continue
        printed, initialized, exit_code = evaluate(args[index + 1], initialized)
        for line in printed:
            print(line, file=sys.stderr if line.startswith("Error:") else sys.stdout)
        if exit_code is not None:
            return exit_code
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
