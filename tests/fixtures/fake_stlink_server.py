#!/usr/bin/env python3
"""stlink-server, as version 2.1.1 behaved under ST-LINK_gdbserver's shared mode on the reference board.

What this stands in for is recorded in the shared-mode cycles of
st_link_gdbserver_7_14_0_linux_ends_recordings.json beside this file: the
server listens on TCP port 7184 (its own `default port : 7184` line), the GDB
server started with `-t` reaches the probe through it, and SIGTERM ends it at
once with nothing printed, exit status -15. This fake listens on the port the
test names instead of 7184, and appends one JSON line to a file for every
event: its start with its arguments, the port listening, every client it
accepted and lost, and the SIGTERM it died of.

It prints nothing, except with `FAKE_STLINK_SERVER_SCENARIO=open_dev_refused`:
then, for a client that sends anything, it prints the line the recorded server
printed when it could not open the probe for a GDB server, taken from the first
such refusal in st_link_gdbserver_7_14_0_linux_restarts_recordings.json, and
closes that client's connection.
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

# The port this fake listens on, standing in for 7184.
PORT_VARIABLE = "FAKE_STLINK_SERVER_PORT"
EVENTS_VARIABLE = "FAKE_STLINK_SERVER_EVENTS"
SCENARIO_VARIABLE = "FAKE_STLINK_SERVER_SCENARIO"
OPEN_DEV_REFUSED = "open_dev_refused"
RESTARTS_RECORDING = Path(__file__).with_name("st_link_gdbserver_7_14_0_linux_restarts_recordings.json")
events_lock = threading.Lock()


def event(name: str, **fields: object) -> None:
    path = os.environ.get(EVENTS_VARIABLE)
    if not path:
        return
    with events_lock, open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps({"event": name, "pid": os.getpid(), "at": time.monotonic(), **fields}) + "\n")


def recorded_open_refusal() -> list[str]:
    """stlink-server's own lines about the first start it could not open the probe for, in the restart round."""
    for cycle in json.loads(RESTARTS_RECORDING.read_text(encoding="utf-8"))["scenarios"]["restarts"]:
        lines = [str(line["line"]) for line in cycle.get("probe_server_output") or [] if "OPEN_DEV FAIL" in str(line["line"])]
        if cycle.get("ready_at_s") is None and lines:
            return lines
    raise SystemExit("fake stlink-server: the restart round holds no refusal to open the probe")


def serve(connection: socket.socket) -> None:
    event("client_connected")
    refusing = os.environ.get(SCENARIO_VARIABLE) == OPEN_DEV_REFUSED
    with contextlib.suppress(OSError):
        while connection.recv(4096):
            if refusing:
                for line in recorded_open_refusal():
                    print(line, file=sys.stderr, flush=True)
                event("open_refused")
                connection.close()
                break
    event("client_disconnected")


def main() -> int:
    event("started", argv=sys.argv[1:])
    port_text = os.environ.get(PORT_VARIABLE)
    if not port_text:
        print(f"fake stlink-server: {PORT_VARIABLE} names no port", file=sys.stderr)
        return 2

    def terminated(signum: int, frame: object) -> None:
        # The recorded server did not run a shutdown of its own on SIGTERM: it
        # died of the signal. So this one records the signal and dies of it too.
        event("terminated")
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        os.kill(os.getpid(), signal.SIGTERM)

    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, terminated)
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", int(port_text)))
    listener.listen(8)
    event("listening", port=int(port_text))
    while True:
        connection, _ = listener.accept()
        threading.Thread(target=serve, args=(connection,), daemon=True).start()


if __name__ == "__main__":
    raise SystemExit(main())
