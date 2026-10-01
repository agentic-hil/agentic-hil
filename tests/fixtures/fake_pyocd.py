#!/usr/bin/env python3
from __future__ import annotations

import contextlib
import json
import os
import re
import socket
import sys
import threading
from pathlib import Path

# The one pyOCD connect mode that leaves a running core alone. Every other way
# of asking for one halts or resets the target the read is measuring, so this
# fixture refuses them the way it refuses an intrusive probe listing: a backend
# that regressed to one of them fails here rather than on somebody's bench.
NON_INTRUSIVE_CONNECT_MODE = "attach"
CONNECT_MODE_FLAGS = ("--connect", "-M")
HALT_ON_CONNECT_FLAGS = ("--halt", "-H")


def intrusive_connect_reason(args: list[str], commands: list[str], read_command: str) -> str | None:
    """Why this read would have touched the core, or None if it would not."""
    for flag in HALT_ON_CONNECT_FLAGS:
        if flag in args:
            return f"{flag} halts the core on connect"
    for index, item in enumerate(args):
        if item in CONNECT_MODE_FLAGS:
            mode = args[index + 1] if index + 1 < len(args) else ""
            if mode != NON_INTRUSIVE_CONNECT_MODE:
                return f"connect mode {mode!r} is not {NON_INTRUSIVE_CONNECT_MODE!r}"
        if item.startswith("connect_mode="):
            return f"session option {item!r} decides the connect behind the read"
    others = [command for command in commands if command != read_command]
    if others:
        return f"the read ran beside another commander command: {others!r}"
    return None


def unquote(token: str) -> str:
    """The single-quoted form the backend sends a file path in.

    pyOCD's own tokenizer treats a backslash as an escape and a space as a word
    break unless the word is quoted, and honours no escapes inside single
    quotes; this is the half of that behaviour the fixture needs.
    """
    if len(token) >= 2 and token[0] == "'" and token[-1] == "'":
        return token[1:-1]
    return token


def save_memory(command: str) -> int:
    """`savemem ADDR LEN FILE`, answered the way pyOCD answers it.

    A raw binary file of exactly the requested length, and the confirmation line
    pyOCD's SavememCommand prints. The bytes are derived from the address, so a
    test can prove the file holds what was read from the address that was asked
    for rather than merely holding something.
    """
    _, address_text, size_text, file_text = command.split(None, 3)
    address = int(address_text, 16 if address_text.lower().startswith("0x") else 10)
    size = int(size_text)
    path = Path(unquote(file_text.strip()))
    path.write_bytes(bytes((address + offset) & 0xFF for offset in range(size)))
    print(f"Saved {size} bytes to {path}")
    return 0


# `pyocd gdbserver`, replayed from what pyOCD 0.45.1 printed on the reference
# board (the recording beside this file: its version, date and the GDB that
# drove it are in the file). Nothing here is remembered output: every line this
# server prints is a line of that recording, with the port the start reserved
# where the recording has its own.
GDBSERVER_RECORDING = Path(__file__).with_name("pyocd_0_45_1_gdbserver_recordings.json")
# A file this fake appends one JSON line to for every event: the start with its
# arguments, the port listening, and every client it accepted and lost.
GDBSERVER_EVENTS_VARIABLE = "FAKE_PYOCD_GDBSERVER_EVENTS"
# The name of a recorded failure to replay instead of serving: one of
# `unknown_probe_uid`, `unknown_target_type` or
# `probe_already_held_by_another_server`.
GDBSERVER_SCENARIO_VARIABLE = "FAKE_PYOCD_GDBSERVER"
GDBSERVER_FAILURE_SCENARIOS = ("unknown_probe_uid", "unknown_target_type", "probe_already_held_by_another_server")
# The recorded run that served one session with the console server off, and
# the line in it that says the GDB port listens.
GDBSERVER_STARTUP_SCENARIO = "startup_console_off"
GDBSERVER_READY_WORDS = "GDB server listening on port "
# Only the session's own port may listen. Without this option the recorded
# server also listened on 4444, the semihosting console's fixed default
# (scenario `startup_default_options`), and `--persist` keeps a server alive
# after its client is gone (the recorded help). This fake refuses both in its
# own words, so a session that would have left either behind fails here.
CONSOLE_OFF_OPTION = "semihost_console_type=off"
gdbserver_output_lock = threading.Lock()


def gdbserver_recording() -> dict:
    return json.loads(GDBSERVER_RECORDING.read_text(encoding="utf-8"))


def gdbserver_event(event: str, **fields: object) -> None:
    path = os.environ.get(GDBSERVER_EVENTS_VARIABLE)
    if not path:
        return
    with gdbserver_output_lock, open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps({"event": event, "pid": os.getpid(), **fields}) + "\n")


def gdbserver_say(line: str, stream: str = "stderr") -> None:
    with gdbserver_output_lock, contextlib.suppress(OSError):
        target = sys.stdout if stream == "stdout" else sys.stderr
        target.write(line + "\n")
        target.flush()


def recorded_line(lines: list[dict], words: str) -> str:
    return next(entry["line"] for entry in lines if words in entry["line"])


def serve_gdb_client(listener: socket.socket, port: int, connected_line: str, disconnected_line: str) -> None:
    """One client, then exit: what the recorded server did with no `--persist`.

    A client that closes is the end of the server, whether it was GDB or a bare
    TCP connect (scenario `connect_and_close_without_gdb`, exit status 0), and
    the recorded GDB that exited left the core running (scenario
    `session_ended_by_gdb_exit`), which is what the event says."""
    try:
        connection, peer = listener.accept()
    except OSError:
        os._exit(0)
    gdbserver_event("client_connected", port=port)
    gdbserver_say(re.sub(r"localhost:\d+", f"localhost:{peer[1]}", connected_line))
    with contextlib.suppress(OSError):
        while True:
            received = connection.recv(4096)
            if not received:
                break
            if received.startswith(b"+"):
                connection.sendall(b"+")
    gdbserver_event("client_disconnected", port=port, resumes_core=True)
    gdbserver_say(disconnected_line)
    os._exit(0)


def gdbserver(args: list[str]) -> int:
    recording = gdbserver_recording()
    scenarios = recording["scenarios"]
    gdbserver_event("started", argv=args)
    scenario = os.environ.get(GDBSERVER_SCENARIO_VARIABLE, "")
    if scenario in GDBSERVER_FAILURE_SCENARIOS:
        failure = scenarios[scenario]
        for entry in failure["output"]:
            gdbserver_say(entry["line"], entry["stream"])
        return int(failure["returncode"])
    if CONSOLE_OFF_OPTION not in args or "--persist" in args:
        print(f"fake pyocd: a session server needs -O {CONSOLE_OFF_OPTION} and no --persist: {args!r}", file=sys.stderr)
        return 2
    port = int(args[args.index("--port") + 1])
    session = scenarios["session_ended_by_gdb_exit"]
    served_lines = session["server_after_gdb_exit"]["output"]
    recorded_port = str(session["ready_line"]["line"]).split(GDBSERVER_READY_WORDS, 1)[1].split()[0]
    connected_line = recorded_line(served_lines, "connected on port").replace(recorded_port, str(port))
    disconnected_line = recorded_line(served_lines, "disconnected from port").replace(recorded_port, str(port))
    startup = scenarios[GDBSERVER_STARTUP_SCENARIO]
    startup_port = str(startup["gdb_port"])
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    for entry in startup["output"]:
        line = str(entry["line"])
        if GDBSERVER_READY_WORDS in line:
            # Listening before the line says so, as the real server does.
            listener.bind(("127.0.0.1", port))
            listener.listen(1)
            gdbserver_event("listening", port=port)
            line = line.replace(f"{GDBSERVER_READY_WORDS}{startup_port}", f"{GDBSERVER_READY_WORDS}{port}")
        gdbserver_say(line, entry["stream"])
    threading.Thread(target=serve_gdb_client, args=(listener, port, connected_line, disconnected_line), daemon=True).start()
    threading.Event().wait()
    return 0


def main() -> int:
    args = sys.argv[1:]
    if args and args[0] == "gdbserver":
        return gdbserver(args)
    if "--version" in args:
        print("0.36.0")
        return 0
    if args and args[0] == "json":
        if args[1:] == ["--targets", "--no-config"]:
            # Same envelope pyOCD 0.45.1 emits, trimmed: one builtin and one
            # pack-provided target, so a test can tell the two apart.
            print(
                json.dumps(
                    {
                        "pyocd_version": "0.36.0",
                        "status": 0,
                        "targets": [
                            {"name": "cortex_m", "vendor": "Generic", "part_number": "CoreSightTarget", "source": "builtin"},
                            {"name": "stm32f446re", "vendor": "STMicroelectronics", "part_number": "STM32F446RE", "source": "pack"},
                            {"name": "stm32f446retx", "vendor": "STMicroelectronics", "part_number": "STM32F446RETx", "source": "pack"},
                        ],
                    }
                )
            )
            return 0
        if args[1:] != ["--probes", "--no-config"]:
            print("unsafe probe discovery arguments", file=sys.stderr)
            return 2
        print(
            json.dumps(
                {
                    "status": 0,
                    "boards": [
                        {"unique_id": "PYOCD123"},
                        {"unique_id": "PYOCD456"},
                    ],
                }
            )
        )
        return 0
    text = " ".join(args)
    print(text)
    if args and args[0] in {"commander", "cmd"}:
        commands = [args[index + 1] for index, item in enumerate(args) if item in {"-c", "--command"} and index + 1 < len(args)]
        savemem = next((command for command in commands if command.startswith("savemem")), None)
        if savemem is not None:
            reason = intrusive_connect_reason(args, commands, savemem)
            if reason is not None:
                print(f"a memory read must not disturb the core: {reason}", file=sys.stderr)
                return 2
            return save_memory(savemem)
        if "status" in text:
            print("Target status: halted")
        if "reset" in text:
            print("Reset target executed")
    elif args and args[0] == "flash":
        print("[==================================] 100%")
        print("Programmed 8192 bytes @ 0x08000000")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
