#!/usr/bin/env python3
"""ST-LINK_gdbserver, replayed from what version 7.14.0 printed on the reference board.

The recording beside this file holds the version, the date, the STM32CubeCLT it
came from and the GDB that drove it. Nothing here is remembered output: every
line this server prints is a line of that recording, with the port the start
reserved, the serial it was given and the programmer directory it was pointed
at where the recording has its own. Where it refuses something the recording
does not cover, it says so in its own words, starting `fake ST-LINK_gdbserver:`.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import socket
import sys
import threading
import time
from pathlib import Path

RECORDING = Path(__file__).with_name("st_link_gdbserver_7_14_0_linux_recordings.json")
# A file this fake appends one JSON line to for every event: the start with its
# arguments, the port listening, every client it accepted and lost, and a
# SIGTERM it ran its own shutdown for.
EVENTS_VARIABLE = "FAKE_ST_LINK_GDBSERVER_EVENTS"
# The name of a recorded failure to replay instead of serving.
SCENARIO_VARIABLE = "FAKE_ST_LINK_GDBSERVER"
FAILURE_SCENARIOS = ("unknown_serial", "probe_already_held_by_another_server", "programmer_path_missing")
# The recorded run that served one session with `-g`, and the line in it that
# says the GDB port listens. The server prints the same line again after every
# client it accepts.
STARTUP_SCENARIO = "startup_attach"
READY_LINE = "Waiting for debugger connection..."
CONNECTED_LINE = "Debugger connected"
# What the recorded server printed when it ended on its own: when its client
# left (scenario `session_ended_by_detach`) and when it was sent SIGTERM
# (scenario `session_ended_by_terminating_the_server`). Both resumed the core.
SHUTDOWN_LINES = ("Shutting down...", "Exit.")
PORT_WORDS = "Listen Port Number         : "
# The serial and the programmer directory the recorded refusals were given.
RECORDED_UNKNOWN_SERIAL = "AGENTICHILNOSUCHPROBE0"
RECORDED_MISSING_PROGRAMMER = "[redacted]/no-such-programmer"
# Options this fake refuses, because a session must not use them. `-e` and
# `--persistent` keep the server alive after its client leaves (the recorded
# help); a session ends the server itself.
PERSISTENT_OPTIONS = ("-e", "--persistent")
# `-t` is the shared mode: the server reaches the probe through stlink-server
# on its port (7184) instead of opening the probe's USB itself. The recorded
# shared cycles printed the same startup lines as a server that opened the USB
# itself (st_link_gdbserver_7_14_0_linux_ends_recordings.json). This fake
# connects to the port this variable names, standing in for 7184, and holds the
# connection for as long as it runs. A shared start with nothing listening was
# not recorded, so this fake refuses one in its own words.
SHARED_OPTION = "-t"
SHARED_PORT_VARIABLE = "FAKE_ST_LINK_SHARED_PORT"
# The probe's USB refusing the server, after a killed server had held it
# itself: the lines of the first start the direct stop round had refused
# (st_link_gdbserver_7_14_0_linux_session_stops_recordings.json). That round
# kept the server's lines and not its exit status, so this fake exits 1, the
# status of the recorded refusal by a probe another server held; the product
# reads a refusal from its lines.
STOPS_RECORDING = Path(__file__).with_name("st_link_gdbserver_7_14_0_linux_session_stops_recordings.json")
USB_REFUSAL_SCENARIO = "start_refused_over_usb_after_a_killed_server"
# A server that is still running when the start's wait runs out: set this and the
# fake prints the recorded startup up to the ready line, then neither listens nor
# says it does, and keeps running until it is ended. No recorded start hung, so
# this prints no line the recording has not got: it stops where the product's
# wait begins, which is the one case a running server's end has to be read in.
NEVER_READY_VARIABLE = "FAKE_ST_LINK_GDBSERVER_NEVER_READY"
# A shared start stlink-server could not open the probe for: the GDB server's
# lines and exit status of the first such start in the restart round, where
# stlink-server's own log said `TCPCMD OPEN_DEV FAIL`. In this scenario the
# fake asks the fake stlink-server for the probe by sending it a line, waits
# for that one to refuse and close, and then says what the recorded server said.
RESTARTS_RECORDING = Path(__file__).with_name("st_link_gdbserver_7_14_0_linux_restarts_recordings.json")
OPEN_REFUSAL_SCENARIO = "start_refused_by_stlink_server"
output_lock = threading.Lock()
shared_connections: list[socket.socket] = []


def recording() -> dict:
    return json.loads(RECORDING.read_text(encoding="utf-8"))


def event(name: str, **fields: object) -> None:
    path = os.environ.get(EVENTS_VARIABLE)
    if not path:
        return
    with output_lock, open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps({"event": name, "pid": os.getpid(), **fields}) + "\n")


def say(line: str, stream: str = "stdout") -> None:
    with output_lock, contextlib.suppress(OSError):
        target = sys.stdout if stream == "stdout" else sys.stderr
        target.write(line + "\n")
        target.flush()


def option_value(args: list[str], option: str) -> str | None:
    if option not in args:
        return None
    index = args.index(option)
    return args[index + 1] if index + 1 < len(args) else None


def replay(lines: list[dict], port: str, recorded_port: str, substitutions: dict[str, str] | None = None) -> None:
    for entry in lines:
        line = str(entry["line"])
        if PORT_WORDS in line:
            line = line.replace(f"{PORT_WORDS}{recorded_port}", f"{PORT_WORDS}{port}")
        for recorded, given in (substitutions or {}).items():
            line = line.replace(recorded, given)
        say(line, str(entry["stream"]))


def recorded_port(scenario: dict) -> str:
    return str(scenario["argv_tail"][scenario["argv_tail"].index("-p") + 1])


def recorded_usb_refusal() -> list[str]:
    """The server's lines for the first refused start of the direct stop round."""
    stops = json.loads(STOPS_RECORDING.read_text(encoding="utf-8"))["rounds"]["direct"]["scenarios"]["session_stops"]
    for cycle in stops:
        for step in cycle.values():
            if isinstance(step, dict) and isinstance(step.get("refused"), dict):
                return [str(line) for line in step["refused"]["server_stdout"]]
    raise SystemExit("fake ST-LINK_gdbserver: the direct stop round holds no refused start")


def recorded_open_refusal() -> dict:
    """The first start of the restart round that stlink-server could not open the probe for."""
    for cycle in json.loads(RESTARTS_RECORDING.read_text(encoding="utf-8"))["scenarios"]["restarts"]:
        if cycle.get("ready_at_s") is None and any("OPEN_DEV FAIL" in str(line["line"]) for line in cycle.get("probe_server_output") or []):
            return cycle["refused"]
    raise SystemExit("fake ST-LINK_gdbserver: the restart round holds no start stlink-server could not open the probe for")


def shut_down(reason: str, **fields: object) -> None:
    event(reason, **fields)
    for line in SHUTDOWN_LINES:
        say(line)
    os._exit(0)


def serve_one_client(listener: socket.socket, port: int) -> None:
    """One client, then exit: what the recorded server did without `-e`.

    A client that leaves ends the server, whether it was GDB or a bare TCP
    connect (scenario `connect_and_close_without_gdb`, exit status 0), and the
    recorded GDB that detached left the core running (scenario
    `session_ended_by_detach`), which is what the event says."""
    try:
        connection, _ = listener.accept()
    except OSError:
        os._exit(0)
    event("client_connected", port=port)
    say(CONNECTED_LINE)
    say(READY_LINE)
    with contextlib.suppress(OSError):
        while True:
            received = connection.recv(4096)
            if not received:
                break
            if received.startswith(b"+"):
                connection.sendall(b"+")
    shut_down("client_disconnected", port=port, resumes_core=True)


def main() -> int:
    args = sys.argv[1:]
    scenarios = recording()["scenarios"]
    event("started", argv=args)
    port_text = option_value(args, "-p")
    programmer = option_value(args, "-cp")
    if port_text is None or programmer is None:
        print(f"fake ST-LINK_gdbserver: a session server needs -p and -cp: {args!r}", file=sys.stderr)
        return 2
    if any(option in args for option in PERSISTENT_OPTIONS) or "-g" not in args:
        # Without `-g` the recorded server reset the core when GDB connected
        # (scenario `session_default_connect` stops at Reset_Handler), which an
        # attach must never do.
        print(f"fake ST-LINK_gdbserver: a session server needs -g and no -e: {args!r}", file=sys.stderr)
        return 2
    scenario_name = os.environ.get(SCENARIO_VARIABLE, "")
    if scenario_name in FAILURE_SCENARIOS:
        failure = scenarios[scenario_name]
        substitutions = {RECORDED_UNKNOWN_SERIAL: option_value(args, "-i") or "", RECORDED_MISSING_PROGRAMMER: programmer}
        replay(failure["output"], port_text, recorded_port(failure), substitutions)
        return int(failure["returncode"])
    if scenario_name == USB_REFUSAL_SCENARIO:
        for line in recorded_usb_refusal():
            say(line.split(PORT_WORDS)[0] + PORT_WORDS + port_text if PORT_WORDS in line else line)
        return 1
    if "-d" not in args:
        # The recorded server started without `-d` found no MCU it knew on the
        # reference board's SWD-only wiring.
        failure = scenarios["startup_without_swd"]
        replay(failure["output"], port_text, recorded_port(failure))
        return int(failure["returncode"])
    port = int(port_text)

    if SHARED_OPTION in args:
        shared_port = int(os.environ.get(SHARED_PORT_VARIABLE) or "7184")
        try:
            shared_connections.append(socket.create_connection(("127.0.0.1", shared_port), timeout=5.0))
        except OSError as error:
            print(f"fake ST-LINK_gdbserver: shared mode found no stlink-server on port {shared_port}: {error}", file=sys.stderr)
            return 2
        event("shared_server_connected", port=shared_port)
        if scenario_name == OPEN_REFUSAL_SCENARIO:
            refusal = recorded_open_refusal()
            connection = shared_connections[-1]
            connection.sendall(b"open" + bytes([10]))
            with contextlib.suppress(OSError):
                while connection.recv(4096):
                    pass
            for entry in refusal["output"]:
                line = str(entry["line"])
                if PORT_WORDS in line:
                    line = line.split(PORT_WORDS)[0] + PORT_WORDS + port_text
                say(line, str(entry["stream"]))
            return int(refusal["returncode"])

    def terminated(signum: int, frame: object) -> None:
        # The recorded server's own shutdown on SIGTERM, which resumed the core.
        shut_down("terminated", port=port, resumes_core=True)

    signal.signal(signal.SIGTERM, terminated)
    startup = scenarios[STARTUP_SCENARIO]
    never_ready = os.environ.get(NEVER_READY_VARIABLE) == "1"
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    for entry in startup["output"]:
        line = str(entry["line"])
        if line in SHUTDOWN_LINES or (never_ready and line == READY_LINE):
            break
        if line == READY_LINE:
            # Listening before the line says so. The real server listens on
            # every address and on the port after this one as well (its SWV
            # port); this fake listens on loopback and the GDB port only.
            listener.bind(("127.0.0.1", port))
            listener.listen(1)
            event("listening", port=port)
        if PORT_WORDS in line:
            line = line.replace(f"{PORT_WORDS}{recorded_port(startup)}", f"{PORT_WORDS}{port}")
        say(line, str(entry["stream"]))
    if never_ready:
        # Nothing to serve: the port never listened, and this server is still
        # running when the start's wait runs out, which is the only way the end
        # of a running server gets read at all.
        event("never_listening", port=port)
    else:
        threading.Thread(target=serve_one_client, args=(listener, port), daemon=True).start()
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    raise SystemExit(main())
