#!/usr/bin/env python3
"""stlink-server, as version 2.1.1 behaved under ST-LINK_gdbserver's shared mode on the reference board.

What this stands in for is recorded in the shared-mode cycles of
st_link_gdbserver_7_14_0_linux_ends_recordings.json beside this file: the
server listens on TCP port 7184 (its own `default port : 7184` line), the GDB
server started with `-t` reaches the probe through it, and SIGTERM ends it at
once with nothing printed, exit status -15. This fake prints nothing, because
the session layer reads none of the server's words; it listens on the port the
test names instead of 7184, and appends one JSON line to a file for every
event: its start with its arguments, the port listening, every client it
accepted and lost, and the SIGTERM it died of.
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

# The port this fake listens on, standing in for 7184.
PORT_VARIABLE = "FAKE_STLINK_SERVER_PORT"
EVENTS_VARIABLE = "FAKE_STLINK_SERVER_EVENTS"
events_lock = threading.Lock()


def event(name: str, **fields: object) -> None:
    path = os.environ.get(EVENTS_VARIABLE)
    if not path:
        return
    with events_lock, open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps({"event": name, "pid": os.getpid(), "at": time.monotonic(), **fields}) + "\n")


def serve(connection: socket.socket) -> None:
    event("client_connected")
    with contextlib.suppress(OSError):
        while connection.recv(4096):
            pass
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
