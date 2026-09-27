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

Given `gdb_port <port>`, it is the GDB server of a debug session instead, and it
prints what OpenOCD 0.12.0 printed while serving one: the recorded startup, the
listening line once the port listens, and a line for every connection it
accepts, rejects or drops. It answers a connection the way OpenOCD's GDB server
does, so a connection that closes without sending GDB's initial acknowledgement
is rejected with the recorded `Error:` line, as the product's own readiness
check was on the bench.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import socket
import sys
import threading
import time

RUN_STAGE_COMMANDS = frozenset({"reset", "targets", "halt", "resume", "step", "poll", "mdw", "mww", "mdb", "mwb"})
INITIALIZING_COMMANDS = frozenset({"init", "program"})

# What OpenOCD 0.12.0 printed while it brought up the GDB port of one attach
# debug session, word for word from a recording on the reference board: the
# distribution package on Linux 6.8 (Ubuntu), an ST-Link V2-1 on a Nucleo-F446RE,
# 2026-09-26, read out of the session log's `server_stderr_tail` (OpenOCD logs to
# stderr). `{port}` stands where the recording has the port the session reserved.
RECORDED_STARTUP_LINES = (
    "Open On-Chip Debugger 0.12.0",
    "Licensed under GNU GPL v2",
    "For bug reports, read",
    "\thttp://openocd.org/doc/doxygen/bugs.html",
    "Info : auto-selecting first available session transport \"hla_swd\". To override use 'transport select <transport>'.",
    "Info : The selected transport took over low-level target control. The results might differ compared to plain JTAG/SWD",
    "Info : clock speed 2000 kHz",
    "Info : STLINK V2J30M19 (API v2) VID:PID 0483:374B",
    "Info : Target voltage: 3.264253",
    "Info : [stm32f4x.cpu] Cortex-M4 r0p1 processor detected",
    "Info : [stm32f4x.cpu] target has 6 breakpoints, 4 watchpoints",
    "Info : starting gdb server for stm32f4x.cpu on {port}",
)
# Logged once the port listens: OpenOCD calls listen() and then logs this line
# (openocd/src/server/server.c:286 and :297 in v0.12.0).
RECORDED_LISTENING_LINE = "Info : Listening on port {port} for gdb connections"
# What the recording has between the listening line and the first connection:
# the `halt` of the startup script, and the two servers the product turns off.
RECORDED_LINES_AFTER_LISTENING = (
    "[stm32f4x.cpu] halted due to breakpoint, current mode: Thread ",
    "xPSR: 0x01070000 pc: 0x0800208a msp: 0x2001ffe8",
    "Info : Unable to match requested speed 2000 kHz, using 1800 kHz",
    "Info : Unable to match requested speed 2000 kHz, using 1800 kHz",
    "[stm32f4x.cpu] halted due to debug-request, current mode: Thread ",
    "xPSR: 0x01000000 pc: 0x08002240 msp: 0x20020000",
    "Info : tcl server disabled",
    "Info : telnet server disabled",
)
# The recording's lines for a connection (server.c:90, :94 and :574). Its first
# accept was the product's readiness check, rejected because it closed without
# sending anything; the second was GDB's.
RECORDED_ACCEPTING_LINE = "Info : accepting 'gdb' connection on tcp/{port}"
RECORDED_REJECTED_LINE = "Error: attempted 'gdb' connection rejected"
RECORDED_DROPPED_LINE = "Info : dropped 'gdb' connection"
# One of the recording's own lines, for a test that buries the listening line.
RECORDED_FLOOD_LINE = "Info : Unable to match requested speed 2000 kHz, using 1800 kHz"
# Not recorded, because the product turns the tcl and telnet servers off and asks
# for one target: listening lines a session must not take for its own. They are
# the recorded line's format (server.c:297) with the names OpenOCD gives those two
# servers (tcl_server.c:269, telnet_server.c:934), and with the port OpenOCD gives
# a second target's GDB server, the next one up (gdb_server.c:3853). The tcl and
# telnet lines carry the session's own port, so only the service name tells them
# apart from the line that counts.
DECOY_LISTENING_LINES = (
    "Info : Listening on port {port} for tcl connections",
    "Info : Listening on port {port} for telnet connections",
    "Info : Listening on port {other_port} for gdb connections",
)
# How a test drives the GDB server, read from the environment the product hands
# the process it spawns. With none of them set, the recording is printed in order
# and the port listens at once.
#
# RECORD: a file this fake appends one JSON line to for every event: the port
#   listening, every write to stderr, and every connection accepted, every chunk
#   it sent and its close.
# LISTEN_AFTER_S: seconds between the startup lines and the port listening, or
#   `never`.
# LINE_AFTER_S: seconds between the port listening and the listening line, or
#   `never`.
# DECOY_LINES: `1` prints the decoy lines above as soon as the port listens.
# FLOOD_LINES: how many copies of the flood line follow the listening line in the
#   same write.
RECORD_VARIABLE = "FAKE_OPENOCD_RECORD"
LISTEN_AFTER_VARIABLE = "FAKE_OPENOCD_LISTEN_AFTER_S"
LINE_AFTER_VARIABLE = "FAKE_OPENOCD_LINE_AFTER_S"
DECOY_LINES_VARIABLE = "FAKE_OPENOCD_DECOY_LINES"
FLOOD_LINES_VARIABLE = "FAKE_OPENOCD_FLOOD_LINES"
NEVER = "never"

output_lock = threading.Lock()
served_port: int | None = None


def append_to_record(event: str, fields: dict[str, object]) -> None:
    path = os.environ.get(RECORD_VARIABLE)
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps({"event": event, "pid": os.getpid(), "port": served_port, **fields}) + "\n")


def record(event: str, **fields: object) -> None:
    with output_lock:
        append_to_record(event, fields)


def say(*lines: str) -> None:
    """Write lines to stderr, where OpenOCD logs, in one write.

    The write is recorded before it is made, so anything it causes is recorded
    after it."""
    with output_lock:
        append_to_record("printed", {"lines": list(lines)})
        with contextlib.suppress(OSError):
            sys.stderr.buffer.write("".join(f"{line}\n" for line in lines).encode("utf-8"))
            sys.stderr.buffer.flush()


def delay(variable: str) -> float | None:
    """Seconds to wait before a step, from the environment: None for never."""
    value = os.environ.get(variable, "").strip()
    if value == NEVER:
        return None
    return float(value) if value else 0.0


def receive(connection: socket.socket) -> bytes:
    try:
        return connection.recv(4096)
    except OSError:
        return b""


def answer_connection(connection: socket.socket, number: int) -> None:
    """Take one connection the way OpenOCD's GDB server does.

    OpenOCD logs the accept (server.c:90), sends its `+` and reads the
    acknowledgement GDB sends when it connects (gdb_server.c:1012 and :1030). A
    connection that closes without sending one fails that read, and OpenOCD
    closes it and logs the rejection (server.c:93 and :94). A connection that
    sent one is served until it closes, and the close is logged as dropped
    (server.c:574)."""
    record("accepted", connection=number)
    say(RECORDED_ACCEPTING_LINE.format(port=served_port))
    with contextlib.suppress(OSError):
        connection.sendall(b"+")
    received = receive(connection)
    if not received:
        record("closed", connection=number)
        connection.close()
        say(RECORDED_REJECTED_LINE)
        return
    record("received", connection=number, data=received.decode("latin-1"))
    threading.Thread(target=serve_connection, args=(connection, number), daemon=True).start()


def serve_connection(connection: socket.socket, number: int) -> None:
    while True:
        received = receive(connection)
        if not received:
            break
        record("received", connection=number, data=received.decode("latin-1"))
    record("closed", connection=number)
    connection.close()
    say(RECORDED_DROPPED_LINE)


def accept_connections(listener: socket.socket) -> None:
    number = 0
    while True:
        try:
            connection, _ = listener.accept()
        except OSError:
            return
        number += 1
        answer_connection(connection, number)


def run_until_stopped() -> int:
    try:
        while True:
            time.sleep(0.1)
    except KeyboardInterrupt:
        return 0


def serve_gdb_port(port: int) -> int:
    global served_port
    served_port = port
    say(*(line.format(port=port) for line in RECORDED_STARTUP_LINES))
    listen_after = delay(LISTEN_AFTER_VARIABLE)
    if listen_after is None:
        return run_until_stopped()
    time.sleep(listen_after)
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", port))
        listener.listen(1)
        record("listening")
        threading.Thread(target=accept_connections, args=(listener,), daemon=True).start()
        if os.environ.get(DECOY_LINES_VARIABLE) == "1":
            other_port = port + 1 if port < 65535 else port - 1
            say(*(line.format(port=port, other_port=other_port) for line in DECOY_LISTENING_LINES))
        line_after = delay(LINE_AFTER_VARIABLE)
        if line_after is not None:
            time.sleep(line_after)
            flood = int(os.environ.get(FLOOD_LINES_VARIABLE) or 0)
            say(RECORDED_LISTENING_LINE.format(port=port), *([RECORDED_FLOOD_LINE] * flood))
        say(*RECORDED_LINES_AFTER_LISTENING)
        return run_until_stopped()
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
