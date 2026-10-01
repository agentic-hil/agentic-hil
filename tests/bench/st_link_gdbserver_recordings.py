"""Opt-in recording of ST-LINK_gdbserver against the bench's board (#624).

Typed debug sessions on the STM32CubeProgrammer backend run ST-LINK_gdbserver,
the GDB server STM32CubeCLT ships beside STM32_Programmer_CLI, and every fake
the deterministic tests use for it comes from what this module records: the
lines a starting server prints, the ports it listens on, its failures, the
answers to the monitor commands a session could send, and what each way of
ending a session leaves the core doing. Nothing here goes through the
product's session layer, because recording what that layer has to be built
against is the point; the server and GDB are driven directly, under the bench
lock and the run lock, and nothing is written to the board's flash: no
`load`, no `--erase-all`, no external loader.

Selected explicitly, like the other recorders: the file is not named `test_*`.
`AGENTIC_HIL_BENCH_CUBECLT` names the root of an STM32CubeCLT tree (the
directory holding `STLink-gdb-server` and `STM32CubeProgrammer`). With
`AGENTIC_HIL_RECORDING_OUT` set to a directory, the recording is written there
as `st-link-gdbserver-recording.json`; it is always attached to the test report
as a property as well.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import tempfile
import time
from pathlib import Path

import pytest
from support import scaled_time_bound

from agentic_hil.gdbmi import GdbMiClient

from .conftest import BENCH_ONLY, Bench, put_on_board
from .pyocd_gdbserver_recordings import (
    COMMAND_TIMEOUT_S,
    COUNTER,
    EXIT_WAIT_S,
    HANDLER,
    OUTPUT_DIRECTORY_ENV,
    RUN_BEFORE_INTERRUPT_S,
    SETTLE_S,
    STOP_TIMEOUT_S,
    GdbServer,
    Recorder,
    free_port,
    gdb_executable,
    gdb_version,
    listening_addresses,
    listening_ports,
)
from .pyocd_recordings import redact_values

pytestmark = [pytest.mark.bench, BENCH_ONLY]

CUBECLT_ENV = "AGENTIC_HIL_BENCH_CUBECLT"
RECORDING_SCHEMA = "agentic-hil.st-link-gdbserver-recording/v1"
OUTPUT_NAME = "st-link-gdbserver-recording.json"
STARTUP_TIMEOUT_S = 30.0
# A serial no probe carries, for the refusal a wrong probe_id gets.
UNKNOWN_SERIAL = "AGENTICHILNOSUCHPROBE0"
# How many times a session is ended by terminating the server with the core
# halted, each followed by a fresh server that has to open the probe again.
TERMINATE_HALTED_CYCLES = 3
STARTS_AFTER_TERMINATE_RUNNING = 3
NEXT_CONNECTS = 3
# What the CubeCLT tree is called in the committed recording.
CUBECLT_PLACEHOLDER = "<cubeclt>"


def cubeclt_root() -> Path:
    value = os.environ.get(CUBECLT_ENV)
    if not value or not Path(value).is_dir():
        pytest.fail(f"{CUBECLT_ENV} does not name an STM32CubeCLT tree on this bench", pytrace=False)
    return Path(value)


def directory_entries(path: Path) -> list[str]:
    try:
        return sorted(item.name for item in path.iterdir())
    except OSError as error:
        return [f"<unreadable: {type(error).__name__}>"]


class StLinkRecorder(Recorder):
    """The pyOCD recorder's GDB side, driving ST-LINK_gdbserver's command line instead."""

    def __init__(self, bench: Bench, executable: str, programmer_bin: str, gdb: str, image: Path, serial: str, environment: dict[str, str]) -> None:
        super().__init__(bench, executable, gdb, image, serial, environment)
        self.programmer_bin = programmer_bin

    def server_argv(self, port: int, *, serial: str | None = None, extra: list[str] | None = None, swd: bool = True, programmer_bin: str | None = None) -> list[str]:  # type: ignore[override]
        argv = [self.executable, "-p", str(port)]
        if swd:
            argv.append("-d")
        argv.extend(["-cp", programmer_bin or self.programmer_bin, "-i", serial or self.uid, *(extra or [])])
        return argv

    def wait_until_listening(self, server: GdbServer, port: int, timeout_s: float) -> dict | None:
        """The moment the server listens on the GDB port, read from /proc, with the lines printed by then."""
        deadline = time.monotonic() + scaled_time_bound(timeout_s)
        while time.monotonic() < deadline:
            if server.process.poll() is not None:
                return None
            try:
                ports = listening_ports(server.process.pid)
            except OSError:
                ports = []
            if port in ports:
                time.sleep(0.3)
                return {"at_s": round(time.monotonic() - server.started, 2), "lines_by_then": server.output()}
            time.sleep(0.05)
        return None

    def started(self, extra: list[str] | None = None, *, swd: bool = True) -> tuple[GdbServer, dict | None]:  # type: ignore[override]
        port = free_port()
        server = self.start(self.server_argv(port, extra=extra, swd=swd))
        ready = self.wait_until_listening(server, port, STARTUP_TIMEOUT_S)
        return server, ready

    @staticmethod
    def port_of(server: GdbServer) -> int:
        return int(server.argv[server.argv.index("-p") + 1])

    def monitor(self, client: GdbMiClient, words: str) -> dict:
        return self.command(client, f'-interpreter-exec console "monitor {words}"')

    def where(self, client: GdbMiClient) -> dict:
        counter_answer, counter = self.value(client, COUNTER)
        pc_answer, pc = self.value(client, "$pc")
        return {COUNTER: counter, "pc": pc, "answers": [counter_answer, pc_answer]}

    def counter_after_a_fresh_connect(self) -> dict:  # type: ignore[override]
        """The demo's counter and PC as a new attaching server and GDB find them, ended with the server terminated first."""
        server, ready = self.started(["-g"])
        record: dict = {"argv_tail": server.argv[1:], "ready": ready}
        if ready is None:
            record.update(self.finish(server))
            return record
        client, connect = self.connect(self.port_of(server))
        record["connect"] = connect
        record.update(self.where(client))
        record["server_terminated_first"] = server.terminate()
        self.live.remove(server)
        record["gdb_closed"] = self.close_client(client)
        record["output"] = server.output()
        return record

    # Scenarios -----------------------------------------------------------

    def startup(self, extra: list[str], label: str, *, swd: bool = True) -> dict:  # type: ignore[override]
        """A server started and torn down by the product's own teardown with no GDB ever connected."""
        temporary = Path(tempfile.gettempdir())
        cwd_before, tmp_before = directory_entries(Path(self.cwd)), directory_entries(temporary)
        server, ready = self.started(extra, swd=swd)
        record: dict = {"scenario": label, "argv_tail": server.argv[1:], "ready": ready, "gdb_port": self.port_of(server)}
        if ready is not None and server.process.poll() is None:
            record["listening_ports"] = listening_ports(server.process.pid)
            record["listening_addresses"] = listening_addresses(server.process.pid)
            record["new_in_cwd_while_running"] = sorted(set(directory_entries(Path(self.cwd))) - set(cwd_before))
            record["new_in_tmp_while_running"] = sorted(set(directory_entries(temporary)) - set(tmp_before))
        if ready is None:
            record.update(self.finish(server))
        else:
            record["terminated"] = server.terminate()
            self.live.remove(server)
            record["output"] = server.output()
        record["left_in_cwd"] = sorted(set(directory_entries(Path(self.cwd))) - set(cwd_before))
        record["left_in_tmp"] = sorted(set(directory_entries(temporary)) - set(tmp_before))
        return record

    def tcp_probe(self) -> dict:  # type: ignore[override]
        """What the server does when something connects to its GDB port and closes without speaking GDB's protocol."""
        server, ready = self.started()
        record: dict = {"scenario": "connect_and_close_without_gdb", "argv_tail": server.argv[1:], "ready": ready}
        if ready is not None:
            with socket.create_connection(("127.0.0.1", self.port_of(server)), timeout=5.0):
                time.sleep(0.5)
        record.update(self.finish(server))
        return record

    def session(self, label: str, extra: list[str]) -> dict:
        """A session through the commands the product sends, ended the way OpenOCD sessions are: GDB exits first.

        Every monitor command a reset could be built on is asked, each followed
        by where the core is, so the recording says which of them reset, which
        halt, and which print what."""
        server, ready = self.started(extra)
        record: dict = {"scenario": label, "argv_tail": server.argv[1:], "ready": ready}
        if ready is None:
            record.update(self.finish(server))
            return record
        client, connect = self.connect(self.port_of(server))
        record["connect"] = connect
        record["after_connect"] = self.where(client)
        record["listening_ports_with_gdb_connected"] = listening_ports(server.process.pid)
        record["monitor_help"] = self.monitor(client, "help")
        for words in ("reset halt", "reset", "halt", "reset init"):
            key = words.replace(" ", "_")
            record[f"monitor_{key}"] = self.monitor(client, words)
            record[f"monitor_{key}_stop_poll"] = self.stop(client, 1.0)
            record[f"after_monitor_{key}"] = self.where(client)
        record["run_and_interrupt"] = [self.command(client, "-exec-continue")]
        time.sleep(0.5)
        record["run_and_interrupt"].append(self.command(client, "-exec-interrupt --all"))
        record["run_and_interrupt_stop"] = self.stop(client)
        record["interrupt_when_halted"] = self.command(client, "-exec-interrupt --all")
        record["interrupt_when_halted_stop_poll"] = self.stop(client, 1.0)
        record["at_handler"] = self.halted_at_handler(client)
        record["unknown_monitor_command"] = self.monitor(client, "agentic_hil_no_such_command")
        record["gdb_exit"] = self.close_client(client)
        record["server_after_gdb_exit"] = self.finish(server)
        time.sleep(SETTLE_S)
        record["settle_s"] = SETTLE_S
        record["fresh_connect"] = self.counter_after_a_fresh_connect()
        return record

    def session_ended_by_terminating_the_server(self, cycle: int) -> dict:  # type: ignore[override]
        """With the core halted at the handler, the server is terminated while GDB is still connected."""
        server, ready = self.started(["-g"])
        record: dict = {"scenario": "session_ended_by_terminating_the_server", "cycle": cycle, "argv_tail": server.argv[1:], "ready": ready}
        if ready is None:
            record.update(self.finish(server))
            return record
        client, connect = self.connect(self.port_of(server))
        record["connect"] = connect
        record["at_handler"] = self.halted_at_handler(client)
        record["server_terminated"] = server.terminate()
        self.live.remove(server)
        record["server_output"] = server.output()
        record["gdb_exit"] = self.close_client(client)
        time.sleep(SETTLE_S)
        record["settle_s"] = SETTLE_S
        record["fresh_connect"] = self.counter_after_a_fresh_connect()
        record["fresh_connect_again"] = self.counter_after_a_fresh_connect()
        return record

    def session_ended_by_detach(self) -> dict:
        """With the core halted at the handler, GDB detaches and the server is left to end on its own."""
        server, ready = self.started(["-g"])
        record: dict = {"scenario": "session_ended_by_detach", "argv_tail": server.argv[1:], "ready": ready}
        if ready is None:
            record.update(self.finish(server))
            return record
        client, connect = self.connect(self.port_of(server))
        record["connect"] = connect
        record["at_handler"] = self.halted_at_handler(client)
        record["detach"] = self.command(client, "-target-detach")
        record["gdb_exit"] = self.close_client(client)
        record["server_after_detach"] = self.finish(server)
        time.sleep(SETTLE_S)
        record["settle_s"] = SETTLE_S
        record["fresh_connect"] = self.counter_after_a_fresh_connect()
        return record

    def server_terminated_while_the_core_runs(self) -> dict:  # type: ignore[override]
        server, ready = self.started(["-g"])
        record: dict = {"scenario": "server_terminated_while_the_core_runs", "argv_tail": server.argv[1:], "ready": ready}
        if ready is None:
            record.update(self.finish(server))
            return record
        client, connect = self.connect(self.port_of(server))
        record["connect"] = connect
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
            if attempt.get("ready") is not None:
                break
            time.sleep(SETTLE_S)
        return record

    def next_connects(self, label: str, halt) -> dict:  # type: ignore[override]
        """A session halted one way and ended by terminating its server under GDB, then attaching servers one after another."""
        server, ready = self.started(["-g"])
        record: dict = {"scenario": label, "argv_tail": server.argv[1:], "ready": ready}
        if ready is None:
            record.update(self.finish(server))
            return record
        client, connect = self.connect(self.port_of(server))
        record["connect"] = connect
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

    def failure(self, label: str, argv: list[str]) -> dict:  # type: ignore[override]
        server = self.start(argv)
        record: dict = {"scenario": label, "argv_tail": argv[1:]}
        record.update(self.finish(server))
        return record

    def busy_probe(self) -> dict:  # type: ignore[override]
        holder, ready = self.started(["-g"])
        record = self.failure("probe_already_held_by_another_server", self.server_argv(free_port(), extra=["-g"]))
        record["holder_ready"] = ready
        record["holder_terminated"] = holder.terminate()
        if holder in self.live:
            self.live.remove(holder)
        record["holder_output"] = holder.output()
        return record


def run_text(argv: list[str], environment: dict[str, str], cwd: str) -> dict:
    answered = subprocess.run(argv, capture_output=True, text=True, env=environment, cwd=cwd, timeout=scaled_time_bound(60), check=False)
    return {"argv_tail": argv[1:], "returncode": answered.returncode, "stdout": answered.stdout.splitlines(), "stderr": answered.stderr.splitlines()}


def write_recording(recording: dict, root: Path, private_values: tuple[str, ...], record_property) -> None:
    text = json.dumps(recording, indent=2, sort_keys=True)
    # The tree first, so its paths read as the placeholder rather than as a
    # redacted home with the rest of the path after it.
    text = text.replace(json.dumps(str(root))[1:-1], CUBECLT_PLACEHOLDER)
    redacted = redact_values(json.loads(text), private_values)
    rendered = json.dumps(redacted, indent=2, sort_keys=True) + "\n"
    directory = os.environ.get(OUTPUT_DIRECTORY_ENV)
    if directory:
        Path(directory).mkdir(parents=True, exist_ok=True)
        (Path(directory) / OUTPUT_NAME).write_bytes(rendered.encode("utf-8"))
    record_property("st_link_gdbserver_recording_v1", json.dumps(redacted, sort_keys=True, separators=(",", ":")))


def test_record_st_link_gdbserver(bench: Bench, firmware: Path, gdb: None, tmp_path: Path, record_property) -> None:
    root = cubeclt_root()
    executable = str(root / "STLink-gdb-server" / "bin" / "ST-LINK_gdbserver")
    programmer_bin = str(root / "STM32CubeProgrammer" / "bin")
    environment = dict(os.environ)
    gdb_path = gdb_executable(environment)
    debugger = bench.configuration()["debuggers"][bench.debugger_name()]
    serial = str(debugger.get("probe_id") or "")
    assert serial, "the bench configuration names no probe"
    recorder = StLinkRecorder(bench, executable, programmer_bin, gdb_path, firmware, serial, environment)
    private_values = (serial, str(bench.project), str(bench.config_root), str(bench.state_root), str(tmp_path), str(Path.home()), str(firmware.parent))
    version = run_text([executable, "--version"], environment, recorder.cwd)
    recording: dict = {
        "schema": RECORDING_SCHEMA,
        "recorded_on": time.strftime("%Y-%m-%d", time.gmtime()),
        "source_commit": os.environ.get("AGENTIC_HIL_BENCH_COMMIT"),
        "bundle": "STM32CubeCLT 1.22.0 for Linux, extracted from its installer without running it",
        "stm32cubeprogrammer_version": (root / "STM32CubeProgrammer" / "bin" / "version").read_text(encoding="utf-8").strip(),
        "gdb_version": gdb_version(gdb_path),
        "probe": "the bench's in-circuit debugger, its serial number redacted",
        "image": "the demo firmware the tier builds",
        "timings": {"settle_s": SETTLE_S, "run_before_interrupt_s": RUN_BEFORE_INTERRUPT_S, "stop_timeout_s": STOP_TIMEOUT_S, "command_timeout_s": COMMAND_TIMEOUT_S, "exit_wait_s": EXIT_WAIT_S},
        "handler": HANDLER,
        "counter": COUNTER,
        "version": version,
        "help": run_text([executable, "-h"], environment, recorder.cwd),
        "cwd_entries": directory_entries(Path(recorder.cwd)),
        "scenarios": {},
    }
    scenarios = recording["scenarios"]
    try:
        scenarios["startup_default"] = recorder.startup([], "startup_default")
        scenarios["startup_attach"] = recorder.startup(["-g"], "startup_attach")
        scenarios["startup_without_swd"] = recorder.startup(["-g"], "startup_without_swd", swd=False)
        scenarios["connect_and_close_without_gdb"] = recorder.tcp_probe()
        scenarios["session_default_connect"] = recorder.session("session_default_connect", [])
        scenarios["session_attach_connect"] = recorder.session("session_attach_connect", ["-g"])
        scenarios["session_halt_option"] = recorder.session("session_halt_option", ["-g", "--halt"])
        scenarios["session_ended_by_terminating_the_server"] = [recorder.session_ended_by_terminating_the_server(cycle) for cycle in range(1, TERMINATE_HALTED_CYCLES + 1)]
        scenarios["session_ended_by_detach"] = recorder.session_ended_by_detach()
        scenarios["server_terminated_while_the_core_runs"] = recorder.server_terminated_while_the_core_runs()
        scenarios["next_connects_after_halted_by_interrupt"] = recorder.next_connects("halted_by_interrupt", recorder.halted_by_interrupt)
        scenarios["unknown_serial"] = recorder.failure("unknown_serial", recorder.server_argv(free_port(), serial=UNKNOWN_SERIAL, extra=["-g"]))
        scenarios["programmer_path_missing"] = recorder.failure("programmer_path_missing", recorder.server_argv(free_port(), extra=["-g"], programmer_bin=str(tmp_path / "no-such-programmer")))
        scenarios["probe_already_held_by_another_server"] = recorder.busy_probe()
    finally:
        recorder.cleanup()
        write_recording(recording, root, private_values, record_property)
        restored = put_on_board(bench, firmware)
        assert restored.get("ok") is True, f"the demo could not be put back on the board: {restored.get('summary')}"
