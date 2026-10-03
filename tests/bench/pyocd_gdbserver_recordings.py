"""Opt-in recording of pyOCD's GDB server against the bench's board (#624).

Typed debug sessions on the pyOCD backend run `pyocd gdbserver`, and every fake
the deterministic tests use for it comes from what this module records: the
lines a starting server prints, the ports it listens on, its failures, the
answer to the monitor commands a session sends, and what each way of ending a
session leaves the core doing. Nothing here goes through the product's session
layer, because recording what that layer has to be built against is the point;
the server and GDB are driven directly, under the bench lock and the run lock,
and nothing is written to the board's flash.

Selected explicitly, like the other recorders: the file is not named `test_*`.
With `AGENTIC_HIL_RECORDING_OUT` set to a directory, the recording is written
there as `pyocd-gdbserver-recording.json`; it is always attached to the test
report as a property as well. A second, smaller recording,
`pyocd-gdbserver-next-connect-recording.json`, follows a core a session left
halted through the servers that connect after it.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

import pytest
from support import scaled_time_bound

from agentic_hil.backends.gdbdebug import reserve_tcp_port
from agentic_hil.config import GDB_AUTODETECT_CANDIDATES
from agentic_hil.gdbmi import GdbMiClient, mi_field, mi_string
from agentic_hil.process import spawn_managed_process, terminate_process_tree

from .conftest import BENCH_ONLY, Bench, put_on_board
from .pyocd_recordings import PACK_ID, TARGET_TYPE, pyocd_bench_environment, pyocd_provenance, redact_values

pytestmark = [pytest.mark.bench, BENCH_ONLY]

RECORDING_SCHEMA = "agentic-hil.pyocd-gdbserver-recording/v1"
OUTPUT_DIRECTORY_ENV = "AGENTIC_HIL_RECORDING_OUT"
OUTPUT_NAME = "pyocd-gdbserver-recording.json"
STARTUP_TIMEOUT_S = 45.0
COMMAND_TIMEOUT_S = 10.0
STOP_TIMEOUT_S = 10.0
EXIT_WAIT_S = 10.0
# Long enough that a core left running moves the demo's millisecond counter by
# thousands, short enough to keep the recorder quick.
SETTLE_S = 2.0
UNKNOWN_UID = "AGENTICHILNOSUCHPROBE0"
UNKNOWN_TARGET = "agentic_hil_no_such_target"
COUNTER = "uptime_ms"
HANDLER = "SysTick_Handler"
# pyOCD 0.45.1 starts a STDIO (semihosting console) server on the fixed port
# 4444 unless told otherwise. `stdio_mode=off` reads as YAML, where `off` is a
# boolean, so pyOCD warns and ignores it; the plain string option it falls back
# to is what turns the console server off. Both are recorded.
STDIO_MODE_OFF = ["-O", "stdio_mode=off"]
CONSOLE_OFF = ["-O", "semihost_console_type=off"]
# How many times a session is ended by terminating the server with the core
# halted, each followed by a fresh server that has to open the probe again.
TERMINATE_HALTED_CYCLES = 3
# How many fresh starts are tried after the server was terminated while the
# core ran, to see whether and when the probe answers again.
STARTS_AFTER_TERMINATE_RUNNING = 3
# The second recording: what the next server to connect does to a core that a
# session left halted, one connect after another.
NEXT_CONNECT_SCHEMA = "agentic-hil.pyocd-gdbserver-next-connect-recording/v1"
NEXT_CONNECT_OUTPUT_NAME = "pyocd-gdbserver-next-connect-recording.json"
NEXT_CONNECTS = 3
# How long a resume runs before it is interrupted: past the demo's start, so the
# counter is moving and the core stops wherever it is, on no breakpoint.
RUN_BEFORE_INTERRUPT_S = 1.0
# The core debug register a CMSIS-Pack's DebugCoreStart sequence writes.
DHCSR_ADDRESS = "0xE000EDF0"


class GdbServer:
    """One `pyocd gdbserver` process and everything it printed, with when."""

    def __init__(self, argv: list[str], environment: dict[str, str], cwd: str) -> None:
        self.argv = argv
        self.started = time.monotonic()
        self.lines: list[dict] = []
        self.lock = threading.Lock()
        self.process = spawn_managed_process(
            argv,
            cwd=cwd,
            env=environment,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.readers = [
            threading.Thread(target=self._read, args=(self.process.stdout, "stdout"), daemon=True),
            threading.Thread(target=self._read, args=(self.process.stderr, "stderr"), daemon=True),
        ]
        for reader in self.readers:
            reader.start()

    def _read(self, stream, name: str) -> None:
        for line in stream:
            with self.lock:
                self.lines.append({"stream": name, "at_s": round(time.monotonic() - self.started, 2), "line": line.rstrip("\r\n")})

    def output(self) -> list[dict]:
        with self.lock:
            return [dict(line) for line in self.lines]

    def wait_for_line(self, pattern: re.Pattern[str], timeout_s: float) -> dict | None:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            for line in self.output():
                if pattern.search(line["line"]):
                    return line
            if self.process.poll() is not None:
                break
            time.sleep(0.05)
        return None

    def wait_for_exit(self, timeout_s: float) -> int | None:
        try:
            return self.process.wait(timeout=scaled_time_bound(timeout_s))
        except subprocess.TimeoutExpired:
            return None

    def terminate(self) -> dict:
        """The product's own teardown of a server process: SIGTERM to its group, then SIGKILL."""
        running = self.process.poll() is None
        terminate_process_tree(self.process, 5.0)
        for reader in self.readers:
            reader.join(timeout=5.0)
        return {"was_running": running, "returncode": self.process.returncode}


def free_port() -> int:
    reservation = reserve_tcp_port()
    port = reservation.port
    reservation.release()
    return port


def listening_ports(pid: int) -> list[int]:
    """The TCP ports a process listens on, read from /proc: the socket inodes it holds, matched to LISTEN rows."""
    inodes = set()
    fd_root = Path(f"/proc/{pid}/fd")
    for fd in fd_root.iterdir():
        try:
            target = os.readlink(fd)
        except OSError:
            continue
        match = re.fullmatch(r"socket:\[(\d+)\]", target)
        if match:
            inodes.add(match.group(1))
    ports = set()
    for table in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            rows = Path(table).read_text(encoding="ascii").splitlines()[1:]
        except OSError:
            continue
        for row in rows:
            fields = row.split()
            if len(fields) > 9 and fields[3] == "0A" and fields[9] in inodes:
                ports.add(int(fields[1].rsplit(":", 1)[1], 16))
    return sorted(ports)


def listening_addresses(pid: int) -> list[str]:
    """The local addresses of the same LISTEN rows, so a recording shows whether a port is loopback only."""
    inodes = set()
    for fd in Path(f"/proc/{pid}/fd").iterdir():
        try:
            match = re.fullmatch(r"socket:\[(\d+)\]", os.readlink(fd))
        except OSError:
            continue
        if match:
            inodes.add(match.group(1))
    addresses = []
    for table in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            rows = Path(table).read_text(encoding="ascii").splitlines()[1:]
        except OSError:
            continue
        for row in rows:
            fields = row.split()
            if len(fields) > 9 and fields[3] == "0A" and fields[9] in inodes:
                address_hex, port_hex = fields[1].rsplit(":", 1)
                if len(address_hex) == 8:
                    address = ".".join(str(int(address_hex[index : index + 2], 16)) for index in (6, 4, 2, 0))
                else:
                    address = "ipv6:" + address_hex
                addresses.append(f"{address}:{int(port_hex, 16)}")
    return sorted(addresses)


class Recorder:
    def __init__(self, bench: Bench, executable: str, gdb: str, image: Path, uid: str, environment: dict[str, str]) -> None:
        self.bench = bench
        self.executable = executable
        self.gdb = gdb
        self.image = image
        self.uid = uid
        self.environment = environment
        self.cwd = str(Path(executable).parent)
        self.live: list[GdbServer] = []
        self.clients: list[GdbMiClient] = []

    def server_argv(self, port: int, *, uid: str | None = None, target: str = TARGET_TYPE, extra: list[str] | None = None) -> list[str]:
        return [self.executable, "gdbserver", "--port", str(port), *(extra or []), "--uid", uid or self.uid, "--target", target, "-W"]

    def start(self, argv: list[str]) -> GdbServer:
        server = GdbServer(argv, self.environment, self.cwd)
        self.live.append(server)
        return server

    def started(self, extra: list[str] | None = None) -> tuple[GdbServer, dict | None]:
        port = free_port()
        server = self.start(self.server_argv(port, extra=extra))
        ready = server.wait_for_line(re.compile(rf"\blistening on port {port}\b", re.IGNORECASE), STARTUP_TIMEOUT_S)
        return server, ready

    def connect(self, port: int) -> tuple[GdbMiClient, list[dict]]:
        """The product's MI prologue, then the stop GDB reports for the connect itself, so a later stop poll waits for a new one."""
        client = GdbMiClient(self.gdb, str(self.bench.project))
        self.clients.append(client)
        transcript = []
        for command in ("-gdb-set pagination off", "-gdb-set confirm off", "-gdb-set mi-async on", f"-file-exec-and-symbols {mi_string(str(self.image))}", f"-target-select extended-remote localhost:{port}"):
            transcript.append(self.command(client, command))
        transcript.append({"stop_after_connect": self.stop(client, 1.0)})
        return client, transcript

    def command(self, client: GdbMiClient, command: str, timeout_s: float = COMMAND_TIMEOUT_S) -> dict:
        result = client.command(command, timeout_s)
        return {"command": command, "result_class": result.result_class, "line": result.line, "records": list(result.records), "timed_out": result.timed_out, "error": result.error_message}

    def stop(self, client: GdbMiClient, timeout_s: float = STOP_TIMEOUT_S) -> dict:
        stop = client.wait_for_stop(timeout_s)
        return {"line": stop.line, "reason": stop.reason, "timed_out": stop.timed_out, "error": stop.error_message}

    def value(self, client: GdbMiClient, expression: str) -> tuple[dict, str | None]:
        answer = self.command(client, f"-data-evaluate-expression {expression}")
        return answer, mi_field(answer["line"], "value") if answer["result_class"] == "done" else None

    def close_client(self, client: GdbMiClient) -> dict:
        history_before = len(client.history())
        try:
            client.close(5.0)
            closed = {"closed": True}
        except Exception as error:  # recorded, not raised: the recording is what was asked for
            closed = {"closed": False, "error": f"{type(error).__name__}: {error}"}
        closed["commands"] = client.history()[history_before:]
        if client in self.clients:
            self.clients.remove(client)
        return closed

    def finish(self, server: GdbServer) -> dict:
        exited = server.wait_for_exit(EXIT_WAIT_S)
        result = {"exited_on_its_own": exited is not None, "returncode": exited}
        if exited is None:
            result["terminated"] = server.terminate()
        else:
            for reader in server.readers:
                reader.join(timeout=5.0)
        if server in self.live:
            self.live.remove(server)
        result["output"] = server.output()
        return result

    def cleanup(self) -> None:
        for client in list(self.clients):
            self.close_client(client)
        for server in list(self.live):
            server.terminate()
            self.live.remove(server)

    # Scenarios -----------------------------------------------------------

    def startup(self, extra: list[str], label: str) -> dict:
        """A server started and torn down by the product's own teardown with no GDB ever connected."""
        server, ready = self.started(extra)
        record: dict = {"scenario": label, "argv_tail": server.argv[1:], "ready_line": ready}
        if ready is not None and server.process.poll() is None:
            record["listening_ports"] = listening_ports(server.process.pid)
            record["listening_addresses"] = listening_addresses(server.process.pid)
            record["gdb_port"] = int(server.argv[server.argv.index("--port") + 1])
        record["terminated"] = server.terminate()
        self.live.remove(server)
        record["output"] = server.output()
        return record

    def tcp_probe(self) -> dict:
        """What the server does when something connects to its GDB port and closes without speaking GDB's protocol."""
        server, ready = self.started(CONSOLE_OFF)
        port = int(server.argv[server.argv.index("--port") + 1])
        record: dict = {"scenario": "connect_and_close_without_gdb", "argv_tail": server.argv[1:], "ready_line": ready}
        with socket.create_connection(("127.0.0.1", port), timeout=5.0):
            time.sleep(0.5)
        record.update(self.finish(server))
        return record

    def counter_after_a_fresh_connect(self) -> dict:
        """The demo's counter and PC as a new server and GDB find them, ended so the core stays where it is."""
        server, ready = self.started(CONSOLE_OFF)
        port = int(server.argv[server.argv.index("--port") + 1])
        client, connect = self.connect(port)
        counter_answer, counter = self.value(client, COUNTER)
        pc_answer, pc = self.value(client, "$pc")
        terminated = server.terminate()
        self.live.remove(server)
        closed = self.close_client(client)
        return {"ready_line": ready, "connect": connect, COUNTER: counter, "pc": pc, "answers": [counter_answer, pc_answer], "server_terminated_first": terminated, "gdb_closed": closed, "output": server.output()}

    def halted_at_handler(self, client: GdbMiClient) -> dict:
        """Run to the SysTick handler and leave the core stopped there with no breakpoint left."""
        steps = [self.command(client, f"-break-insert {HANDLER}"), self.command(client, "-exec-continue")]
        stop = self.stop(client)
        steps.append(self.command(client, "-break-delete"))
        counter_answer, counter = self.value(client, COUNTER)
        pc_answer, pc = self.value(client, "$pc")
        steps.extend([counter_answer, pc_answer])
        return {"steps": steps, "stop": stop, COUNTER: counter, "pc": pc}

    def halted_by_interrupt(self, client: GdbMiClient) -> dict:
        """Resume with no breakpoint set and interrupt it, so the core stops wherever it was."""
        steps = [self.command(client, "-exec-continue")]
        time.sleep(RUN_BEFORE_INTERRUPT_S)
        steps.append(self.command(client, "-exec-interrupt --all"))
        stop = self.stop(client)
        counter_answer, counter = self.value(client, COUNTER)
        pc_answer, pc = self.value(client, "$pc")
        steps.extend([counter_answer, pc_answer])
        return {"steps": steps, "stop": stop, COUNTER: counter, "pc": pc}

    def next_connects(self, label: str, halt) -> dict:
        """A session halted one way and ended by terminating its server under GDB, then servers connecting one after another.

        Each later server is started, connected to and read the way
        `counter_after_a_fresh_connect` does, after a pause in which a core
        left running would move the counter by thousands."""
        server, ready = self.started(CONSOLE_OFF)
        port = int(server.argv[server.argv.index("--port") + 1])
        client, connect = self.connect(port)
        record: dict = {"scenario": label, "argv_tail": server.argv[1:], "ready_line": ready, "connect": connect}
        record["halted"] = halt(client)
        record["server_terminated"] = server.terminate()
        self.live.remove(server)
        record["server_output"] = server.output()
        record["gdb_exit"] = self.close_client(client)
        record["settle_s"] = SETTLE_S
        record["next_connects"] = []
        for _ in range(NEXT_CONNECTS):
            time.sleep(SETTLE_S)
            record["next_connects"].append(self.counter_after_a_fresh_connect())
        return record

    def session_ended_by_gdb_exit(self) -> dict:
        """A session through the commands the product sends, ended the way OpenOCD sessions are: GDB exits first."""
        server, ready = self.started(CONSOLE_OFF)
        port = int(server.argv[server.argv.index("--port") + 1])
        client, connect = self.connect(port)
        record: dict = {"scenario": "session_ended_by_gdb_exit", "argv_tail": server.argv[1:], "ready_line": ready, "connect": connect}
        record["listening_ports_with_gdb_connected"] = listening_ports(server.process.pid)
        record["reset_halt"] = self.command(client, '-interpreter-exec console "monitor reset halt"')
        record["reset_halt_stop_poll"] = self.stop(client, 1.0)
        record["after_reset"] = {"counter_answer": self.value(client, COUNTER)[0], "pc_answer": self.value(client, "$pc")[0]}
        record["run_and_interrupt"] = [self.command(client, "-exec-continue")]
        time.sleep(0.5)
        record["run_and_interrupt"].append(self.command(client, "-exec-interrupt --all"))
        record["run_and_interrupt_stop"] = self.stop(client)
        record["interrupt_when_halted"] = self.command(client, "-exec-interrupt --all")
        record["interrupt_when_halted_stop_poll"] = self.stop(client, 1.0)
        record["at_handler"] = self.halted_at_handler(client)
        record["unknown_monitor_command"] = self.command(client, '-interpreter-exec console "monitor agentic_hil_no_such_command"')
        record["gdb_exit"] = self.close_client(client)
        record["server_after_gdb_exit"] = self.finish(server)
        time.sleep(SETTLE_S)
        record["settle_s"] = SETTLE_S
        record["fresh_connect"] = self.counter_after_a_fresh_connect()
        return record

    def session_ended_by_terminating_the_server(self, cycle: int) -> dict:
        """The same session ended the other way round: with the core halted, the server is terminated while GDB is still connected."""
        server, ready = self.started(CONSOLE_OFF)
        port = int(server.argv[server.argv.index("--port") + 1])
        client, connect = self.connect(port)
        record: dict = {"scenario": "session_ended_by_terminating_the_server", "cycle": cycle, "argv_tail": server.argv[1:], "ready_line": ready, "connect": connect}
        record["at_handler"] = self.halted_at_handler(client)
        record["server_terminated"] = server.terminate()
        self.live.remove(server)
        record["server_output"] = server.output()
        record["gdb_exit"] = self.close_client(client)
        time.sleep(SETTLE_S)
        record["settle_s"] = SETTLE_S
        record["fresh_connect"] = self.counter_after_a_fresh_connect()
        return record

    def server_terminated_while_the_core_runs(self) -> dict:
        """The server terminated while GDB has the core running, then fresh servers started until one answers or the tries run out."""
        server, ready = self.started(CONSOLE_OFF)
        port = int(server.argv[server.argv.index("--port") + 1])
        client, connect = self.connect(port)
        record: dict = {"scenario": "server_terminated_while_the_core_runs", "argv_tail": server.argv[1:], "ready_line": ready, "connect": connect}
        record["continue"] = self.command(client, "-exec-continue")
        time.sleep(0.5)
        record["server_terminated"] = server.terminate()
        self.live.remove(server)
        record["server_output"] = server.output()
        record["gdb_exit"] = self.close_client(client)
        time.sleep(SETTLE_S)
        record["settle_s"] = SETTLE_S
        record["starts_after"] = []
        for _ in range(STARTS_AFTER_TERMINATE_RUNNING):
            attempt = self.counter_after_a_fresh_connect()
            record["starts_after"].append(attempt)
            if attempt["ready_line"] is not None:
                break
            time.sleep(SETTLE_S)
        return record

    def failure(self, label: str, argv: list[str]) -> dict:
        server = self.start(argv)
        record: dict = {"scenario": label, "argv_tail": argv[1:]}
        record.update(self.finish(server))
        return record

    def busy_probe(self) -> dict:
        holder, ready = self.started(CONSOLE_OFF)
        port = free_port()
        record = self.failure("probe_already_held_by_another_server", self.server_argv(port, extra=CONSOLE_OFF))
        record["holder_ready_line"] = ready
        record["holder_terminated"] = holder.terminate()
        self.live.remove(holder)
        record["holder_output"] = holder.output()
        return record


def gdb_executable(environment: dict[str, str]) -> str:
    for candidate in GDB_AUTODETECT_CANDIDATES:
        found = shutil.which(candidate, path=environment.get("PATH"))
        if found:
            return found
    pytest.fail("no GDB candidate is on the bench's PATH", pytrace=False)


def gdb_version(gdb: str) -> str:
    answered = subprocess.run([gdb, "--version"], capture_output=True, text=True, timeout=scaled_time_bound(30), check=False)
    return answered.stdout.splitlines()[0] if answered.stdout else ""


def debug_core_start_dhcsr_writes(environment: dict[str, str], pack_version: str) -> list[str]:
    """The lines of the pack's DebugCoreStart sequence that write DHCSR, read out of the installed pack description.

    pyOCD runs that sequence every time it connects to a part the pack
    describes, in place of its own write that keeps the halt bit."""
    description = Path(environment["XDG_DATA_HOME"]) / "cmsis-pack-manager" / f"{PACK_ID}.{pack_version}.pdsc"
    text = description.read_text(encoding="utf-8")
    sequence = text.split('<sequence name="DebugCoreStart">', 1)[1].split("</sequence>", 1)[0]
    return [line.strip() for line in sequence.splitlines() if DHCSR_ADDRESS in line]


def write_recording(recording: dict, private_values: tuple[str, ...], output_name: str, property_name: str, record_property) -> None:
    redacted = redact_values(recording, private_values)
    text = json.dumps(redacted, indent=2, sort_keys=True) + "\n"
    directory = os.environ.get(OUTPUT_DIRECTORY_ENV)
    if directory:
        Path(directory).mkdir(parents=True, exist_ok=True)
        (Path(directory) / output_name).write_text(text, encoding="utf-8")
    record_property(property_name, json.dumps(redacted, sort_keys=True, separators=(",", ":")))


def test_record_pyocd_gdbserver(bench: Bench, firmware: Path, gdb: None, tmp_path: Path, record_property) -> None:
    environment = pyocd_bench_environment(bench)
    executable, pyocd_version, pack_version = pyocd_provenance(environment)
    gdb_path = gdb_executable(environment)
    debugger = bench.configuration()["debuggers"][bench.debugger_name()]
    uid = str(debugger.get("probe_id") or "")
    assert uid, "the bench configuration names no probe"
    recorder = Recorder(bench, executable, gdb_path, firmware, uid, environment)
    private_values = (uid, str(bench.project), str(bench.config_root), str(bench.state_root), str(tmp_path), str(Path.home()), str(firmware.parent))
    recording: dict = {
        "schema": RECORDING_SCHEMA,
        "recorded_on": time.strftime("%Y-%m-%d", time.gmtime()),
        "source_commit": os.environ.get("AGENTIC_HIL_BENCH_COMMIT"),
        "pyocd_version": pyocd_version,
        "cmsis_pack": {"id": PACK_ID, "version": pack_version, "target_type": TARGET_TYPE},
        "gdb_version": gdb_version(gdb_path),
        "probe": "the bench's in-circuit debugger, its unique ID redacted",
        "image": "the demo firmware the tier builds",
        "scenarios": {},
    }
    help_text = subprocess.run([executable, "gdbserver", "--help"], capture_output=True, text=True, env=environment, timeout=scaled_time_bound(60), check=False)
    recording["gdbserver_help"] = {"returncode": help_text.returncode, "stdout": help_text.stdout.splitlines()}
    scenarios = recording["scenarios"]
    try:
        scenarios["startup_default_options"] = recorder.startup([], "startup_default_options")
        scenarios["startup_stdio_mode_off"] = recorder.startup(STDIO_MODE_OFF, "startup_stdio_mode_off")
        scenarios["startup_console_off"] = recorder.startup(CONSOLE_OFF, "startup_console_off")
        scenarios["connect_and_close_without_gdb"] = recorder.tcp_probe()
        scenarios["session_ended_by_gdb_exit"] = recorder.session_ended_by_gdb_exit()
        scenarios["session_ended_by_terminating_the_server"] = [recorder.session_ended_by_terminating_the_server(cycle) for cycle in range(1, TERMINATE_HALTED_CYCLES + 1)]
        scenarios["server_terminated_while_the_core_runs"] = recorder.server_terminated_while_the_core_runs()
        scenarios["unknown_probe_uid"] = recorder.failure("unknown_probe_uid", recorder.server_argv(free_port(), uid=UNKNOWN_UID, extra=CONSOLE_OFF))
        scenarios["unknown_target_type"] = recorder.failure("unknown_target_type", recorder.server_argv(free_port(), target=UNKNOWN_TARGET, extra=CONSOLE_OFF))
        scenarios["probe_already_held_by_another_server"] = recorder.busy_probe()
    finally:
        recorder.cleanup()
        write_recording(recording, private_values, OUTPUT_NAME, "pyocd_gdbserver_recording_v1", record_property)
        restored = put_on_board(bench, firmware)
        assert restored.get("ok") is True, f"the demo could not be put back on the board: {restored.get('summary')}"


def test_record_what_the_next_pyocd_connect_does_to_a_halted_core(bench: Bench, firmware: Path, gdb: None, tmp_path: Path, record_property) -> None:
    """A core left halted by a session, as each later `pyocd gdbserver` finds it.

    Two ways a session leaves the core halted: interrupted wherever it was, and
    stopped on a breakpoint GDB then deleted. Each session is ended the way the
    product ends one, its server terminated under a connected GDB, and three
    servers then connect one after another, each after a pause, and read the
    demo's counter and the PC. The pack's DHCSR write is recorded beside them."""
    environment = pyocd_bench_environment(bench)
    executable, pyocd_version, pack_version = pyocd_provenance(environment)
    gdb_path = gdb_executable(environment)
    debugger = bench.configuration()["debuggers"][bench.debugger_name()]
    uid = str(debugger.get("probe_id") or "")
    assert uid, "the bench configuration names no probe"
    recorder = Recorder(bench, executable, gdb_path, firmware, uid, environment)
    private_values = (uid, str(bench.project), str(bench.config_root), str(bench.state_root), str(tmp_path), str(Path.home()), str(firmware.parent))
    recording: dict = {
        "schema": NEXT_CONNECT_SCHEMA,
        "recorded_on": time.strftime("%Y-%m-%d", time.gmtime()),
        "source_commit": os.environ.get("AGENTIC_HIL_BENCH_COMMIT"),
        "pyocd_version": pyocd_version,
        "cmsis_pack": {"id": PACK_ID, "version": pack_version, "target_type": TARGET_TYPE, "debug_core_start_dhcsr_writes": debug_core_start_dhcsr_writes(environment, pack_version)},
        "gdb_version": gdb_version(gdb_path),
        "probe": "the bench's in-circuit debugger, its unique ID redacted",
        "image": "the demo firmware the tier builds",
        "scenarios": {},
    }
    scenarios = recording["scenarios"]
    try:
        scenarios["halted_by_interrupt"] = recorder.next_connects("halted_by_interrupt", recorder.halted_by_interrupt)
        scenarios["halted_on_a_deleted_breakpoint"] = recorder.next_connects("halted_on_a_deleted_breakpoint", recorder.halted_at_handler)
    finally:
        recorder.cleanup()
        write_recording(recording, private_values, NEXT_CONNECT_OUTPUT_NAME, "pyocd_gdbserver_next_connect_recording_v1", record_property)
        restored = put_on_board(bench, firmware)
        assert restored.get("ok") is True, f"the demo could not be put back on the board: {restored.get('summary')}"
