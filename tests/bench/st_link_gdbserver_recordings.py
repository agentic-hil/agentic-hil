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
as `st-link-gdbserver-recording.json`, the second round, the teardown one, as
`st-link-gdbserver-teardown-recording.json`, the third, the restart one, as
`st-link-gdbserver-restart-recording.json`, and the fourth, the ends one, as
`st-link-gdbserver-ends-recording.json`; each is always attached to the test
report as a property as well. The fourth also makes calls through the product's
own MCP server, on a copy of the tier's configuration with the probe on `type:
stlink` and on the tier's own configuration, because what it asks is whether
the product's next call still reaches the probe.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import tempfile
import time
from contextlib import suppress
from pathlib import Path

import pytest
from support import scaled_time_bound

from agentic_hil.gdbmi import GdbMiClient, mi_field
from agentic_hil.process import terminate_process_tree

from . import test_bench_debug_sessions as debug_sessions
from . import test_bench_stlink_sessions as stlink_sessions
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
TEARDOWN_OUTPUT_NAME = "st-link-gdbserver-teardown-recording.json"
RESTART_OUTPUT_NAME = "st-link-gdbserver-restart-recording.json"
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
# How many times a session is ended by killing the server with the core
# halted, each read back by servers that are killed the same way.
KILL_HALTED_CYCLES = 3
# The Cortex-M debug halting control and status register: bit 17 (S_HALT)
# reads 1 while the core is halted, whatever GDB has cached.
DHCSR = "0xE000EDF0"
# The arguments `monitor reset` is asked with: plain `reset` printed "System
# reset", `reset halt` and `reset init` were refused as an unknown reset option,
# so the numbered forms are asked for what they print and do.
RESET_VARIANTS = ("reset", "reset 0", "reset 1", "reset 2")
# The third round. A session ended by killing its server, the next server
# started at once, the way a stop followed by a start reaches the probe: the
# bench tier found such a start refused with "USB communication error". How
# many times that is tried, and the waits between the kill and the next start
# it is tried with as well, each that many times.
RESTART_CYCLES = 8
RESTART_DELAYS_S = (0.25, 0.5, 1.0, 2.0)
RESTART_CYCLES_PER_DELAY = 4
# The fourth round: each way a session's server could be ended, many times in a
# row, with the core halted before the end. Which ways and how many times are
# chosen when the round is run, and written into the recording.
ENDS_OUTPUT_NAME = "st-link-gdbserver-ends-recording.json"
ENDS_ENV = "AGENTIC_HIL_RECORDING_ENDS"
END_CYCLES_ENV = "AGENTIC_HIL_RECORDING_END_CYCLES"
DEFAULT_END_CYCLES = 10
# How long each cycle lets the core run before it is interrupted, so the counter
# moves between the connect and the end.
END_RUN_S = 0.3
# Each way of ending: the server options it is started with, and the end itself,
# either a GDB command sent while the server runs or a signal to its group.
# `kill` is GDB's own end of the inferior (the `k` or `vKill` packet),
# `-target-disconnect` closes the connection without detaching, and the signals
# are the server's own shutdown (SIGINT, SIGTERM) or none of it (SIGKILL).
# A GDB command and then a signal sends the signal to a server that is still
# running `GDB_THEN_SIGNAL_WAIT_S` after the command was answered.
# `-r` is the server's "Minimum delay in seconds for hardware status refresh".
GDB_KILL = '-interpreter-exec console "kill"'
GDB_THEN_SIGNAL_WAIT_S = 1.0
END_CANDIDATES: dict[str, tuple[tuple[str, ...], str, object]] = {
    "gdb_kill": ((), "gdb", GDB_KILL),
    "gdb_kill_sigterm": ((), "gdb_then_signal", (GDB_KILL, signal.SIGTERM)),
    "gdb_kill_sigint": ((), "gdb_then_signal", (GDB_KILL, signal.SIGINT)),
    "gdb_kill_sigkill": ((), "gdb_then_signal", (GDB_KILL, signal.SIGKILL)),
    "gdb_kill_sigterm_verbose": (("-v",), "gdb_then_signal", (GDB_KILL, signal.SIGTERM)),
    "gdb_disconnect": ((), "gdb", "-target-disconnect"),
    "sigint": ((), "signal", signal.SIGINT),
    "sigterm": ((), "signal", signal.SIGTERM),
    "sigkill": ((), "signal", signal.SIGKILL),
    "sigkill_slow_refresh": (("-r", "3600"), "signal", signal.SIGKILL),
    "shared_sigkill": (("-t",), "signal", signal.SIGKILL),
    "shared_gdb_kill_sigkill": (("-t",), "gdb_then_signal", (GDB_KILL, signal.SIGKILL)),
    "shared_sigterm": (("-t",), "signal", signal.SIGTERM),
    "reset_sigkill": ((), "signal", signal.SIGKILL),
    "reset_gdb_kill_sigkill": ((), "gdb_then_signal", (GDB_KILL, signal.SIGKILL)),
    "reset_sigterm": ((), "signal", signal.SIGTERM),
    "reset_shared_sigkill": (("-t",), "signal", signal.SIGKILL),
}
# The ends whose cycle is the product's reset-halt session rather than an
# attach: `monitor reset` right after the connect, then a resume nothing stops
# for as long as the tier lets one run before it interrupts it. The product's
# own cycles of that shape had a start refused with "USB communication error"
# after one kill in about twenty, where attach cycles killed the same way had
# none.
RESET_FIRST = frozenset({"reset_sigkill", "reset_gdb_kill_sigkill", "reset_sigterm", "reset_shared_sigkill"})
RESET_COMMAND = '-interpreter-exec console "monitor reset"'
RESET_RUN_S = 3.0
# `-t` is the server's shared mode: it reaches the probe through stlink-server
# instead of opening its USB itself. ST's USB driver library connects to it on
# this port (its own strings carry the number), so each shared cycle starts the
# stlink-server this variable names with `--auto-exit`, which exits once its last
# client has gone, and waits for it to listen before the GDB server is started.
STLINK_SERVER_ENV = "AGENTIC_HIL_RECORDING_STLINK_SERVER"
STLINK_SERVER_PORT = 7184
STLINK_SERVER_EXIT_WAIT_S = 5.0


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

    def kill(self, server: GdbServer) -> dict:
        """SIGKILL to the server's process group and nothing before it, so the server runs none of its own shutdown."""
        running = server.process.poll() is None
        group = getattr(server.process, "_agentic_hil_pgid", None) or server.process.pid
        if running:
            with suppress(ProcessLookupError):
                os.killpg(group, signal.SIGKILL)
        returncode = server.wait_for_exit(5.0)
        # The product's teardown on a group that is gone: it only forgets the process.
        terminate_process_tree(server.process, 5.0)
        for reader in server.readers:
            reader.join(timeout=5.0)
        if server in self.live:
            self.live.remove(server)
        return {"was_running": running, "returncode": returncode}

    def core_state(self, client: GdbMiClient) -> dict:
        """The core as it is rather than as GDB cached it: the register cache dropped, then DHCSR, the PC and the counter."""
        flush = self.command(client, '-interpreter-exec console "maintenance flush register-cache"')
        dhcsr = self.command(client, f"-data-read-memory-bytes {DHCSR} 4")
        contents = mi_field(dhcsr["line"], "contents") if dhcsr["result_class"] == "done" else None
        halted = None
        if contents is not None and len(contents) == 8:
            halted = bool(int.from_bytes(bytes.fromhex(contents), "little") & (1 << 17))
        return {"flush": flush, "dhcsr": dhcsr, "dhcsr_contents": contents, "s_halt": halted, **self.where(client)}

    def counter_after_a_killed_connect(self) -> dict:
        """The core as a new attaching server and GDB find it, ended by killing the server so it resumes nothing."""
        server, ready = self.started(["-g"])
        record: dict = {"argv_tail": server.argv[1:], "ready": ready}
        if ready is None:
            record.update(self.finish(server))
            return record
        client, connect = self.connect(self.port_of(server))
        record["connect"] = connect
        record.update(self.core_state(client))
        record["server_killed"] = self.kill(server)
        record["gdb_closed"] = self.close_client(client)
        record["output"] = server.output()
        return record

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

    def session_ended_by_killing_the_server(self, cycle: int) -> dict:
        """With the core halted at the handler, the server is killed while GDB is still connected."""
        server, ready = self.started(["-g"])
        record: dict = {"scenario": "session_ended_by_killing_the_server", "cycle": cycle, "argv_tail": server.argv[1:], "ready": ready}
        if ready is None:
            record.update(self.finish(server))
            return record
        client, connect = self.connect(self.port_of(server))
        record["connect"] = connect
        record["at_handler"] = self.halted_at_handler(client)
        record["before_kill"] = self.core_state(client)
        record["server_killed"] = self.kill(server)
        record["server_output"] = server.output()
        record["gdb_exit"] = self.close_client(client)
        time.sleep(SETTLE_S)
        record["settle_s"] = SETTLE_S
        record["killed_connect"] = self.counter_after_a_killed_connect()
        time.sleep(SETTLE_S)
        record["killed_connect_again"] = self.counter_after_a_killed_connect()
        return record

    def start_once(self) -> dict:
        """One attaching server started: whether it came up, and what it printed if it did not."""
        server, ready = self.started(["-g"])
        record: dict = {"argv_tail": server.argv[1:], "ready": ready}
        if ready is None:
            record.update(self.finish(server))
        else:
            record["server_killed"] = self.kill(server)
            record["output"] = server.output()
        return record

    def restart_after_a_kill(self, cycle: int, delay_s: float) -> dict:
        """A session ended by killing its server, then the next server started after `delay_s`; once more at once if that one did not come up."""
        server, ready = self.started(["-g"])
        record: dict = {"scenario": "restart_after_a_kill", "cycle": cycle, "delay_s": delay_s, "argv_tail": server.argv[1:], "ready": ready}
        if ready is None:
            record.update(self.finish(server))
            return record
        client, connect = self.connect(self.port_of(server))
        record["connect"] = connect
        record["before_kill"] = self.where(client)
        record["server_killed"] = self.kill(server)
        killed_at = time.monotonic()
        record["server_output"] = server.output()
        record["gdb_exit"] = self.close_client(client)
        if delay_s:
            time.sleep(delay_s)
        record["gap_s"] = round(time.monotonic() - killed_at, 3)
        record["next_start"] = self.start_once()
        if record["next_start"]["ready"] is None:
            record["start_again_at_once"] = self.start_once()
        return record

    def signal_group(self, server: GdbServer, number: int) -> None:
        group = getattr(server.process, "_agentic_hil_pgid", None) or server.process.pid
        with suppress(ProcessLookupError):
            os.killpg(group, number)

    def end_session(self, server: GdbServer, client: GdbMiClient, how: str, *, whole: bool) -> dict:
        """One candidate end, with how long the server took to exit after it and what it exited with.

        A server still running `EXIT_WAIT_S` after the end is killed, and the
        record says so. `whole` keeps every GDB record, with GDB's remote packet
        log turned on for a GDB command; the other cycles keep the result only."""
        _, kind, what = END_CANDIDATES[how]
        record: dict = {"how": how}
        if kind in ("gdb", "gdb_then_signal") and whole:
            record["debug_remote"] = self.command(client, "-gdb-set debug remote 1")
        began = time.monotonic()
        if kind in ("gdb", "gdb_then_signal"):
            command, number = (what, None) if kind == "gdb" else what  # type: ignore[misc]
            answer = self.command(client, str(command))
            record["answer"] = answer if whole else {key: answer[key] for key in ("result_class", "line", "timed_out", "error")}
            if number is not None:
                record["exited_before_the_signal"] = server.wait_for_exit(GDB_THEN_SIGNAL_WAIT_S)
                if record["exited_before_the_signal"] is None:
                    began = time.monotonic()
                    self.signal_group(server, int(number))
        else:
            self.signal_group(server, int(what))  # type: ignore[call-overload]
        returncode = server.wait_for_exit(EXIT_WAIT_S)
        record["returncode"] = returncode
        record["exited_after_s"] = round(time.monotonic() - began, 3) if returncode is not None else None
        if returncode is None:
            record["killed_after_waiting"] = self.kill(server)
        else:
            for reader in server.readers:
                reader.join(timeout=5.0)
            if server in self.live:
                self.live.remove(server)
        closed = self.close_client(client)
        record["gdb_exit"] = closed if whole else {"closed": closed["closed"], "error": closed.get("error")}
        return record

    def shared_server_started(self) -> tuple[GdbServer, dict]:
        """stlink-server for one shared cycle, listening on the port the driver connects to, or the record of why not."""
        path = os.environ.get(STLINK_SERVER_ENV)
        if not path or not Path(path).is_file():
            pytest.fail(f"{STLINK_SERVER_ENV} does not name an stlink-server executable on this bench", pytrace=False)
        server = self.start([path, "--auto-exit", "--debug", "1"])
        deadline = time.monotonic() + 10.0
        record: dict = {"listening_at_s": None}
        while time.monotonic() < deadline and server.process.poll() is None:
            with suppress(OSError):
                if STLINK_SERVER_PORT in listening_ports(server.process.pid):
                    record["listening_at_s"] = round(time.monotonic() - server.started, 2)
                    break
            time.sleep(0.05)
        return server, record

    def shared_server_end(self, server: GdbServer, *, whole: bool) -> dict:
        """How long stlink-server took to exit on its own after its client went, and what it printed."""
        began = time.monotonic()
        returncode = server.wait_for_exit(STLINK_SERVER_EXIT_WAIT_S)
        record: dict = {"returncode": returncode, "exited_after_s": round(time.monotonic() - began, 3) if returncode is not None else None}
        if returncode is None:
            record["terminated_after_waiting"] = server.terminate()
        else:
            for reader in server.readers:
                reader.join(timeout=5.0)
        if server in self.live:
            self.live.remove(server)
        output = server.output()
        record["output"] = output if whole else [entry["line"] for entry in output][-6:]
        return record

    def end_cycle(self, how: str, cycle: int, cli_first: bool, cli) -> dict:
        """A session halted, then ended by `how`; the counter before the end and, at the next cycle's connect, after it.

        With `-g` the connect halts the core where it runs, so the counter read
        at a connect minus the one read before the previous end is what the core
        ran in between. `cli_first` puts a call of the product's own (the CLI,
        through `probe_target`) between this end and the next start, so every
        other end is followed first by the CLI and every other by the server."""
        extra, _, _ = END_CANDIDATES[how]
        whole = cycle == 1
        shared = None
        record: dict = {"cycle": cycle, "first_opener_after_the_end": "cli" if cli_first else "server"}
        if "-t" in extra:
            shared, record["shared_server"] = self.shared_server_started()
        server, ready = self.started(["-g", *extra])
        record["argv_tail"] = server.argv[1:] if whole else None
        record["ready_at_s"] = ready["at_s"] if ready is not None else None
        if ready is None:
            record["refused"] = self.finish(server)
            if shared is not None:
                record["shared_server_end"] = self.shared_server_end(shared, whole=True)
            return record
        client, connect = self.connect(self.port_of(server))
        if whole:
            record["connect"] = connect
        at_connect = self.core_state(client)
        record["at_connect"] = at_connect if whole else {key: at_connect[key] for key in ("s_halt", COUNTER, "pc")}
        if how in RESET_FIRST:
            reset = self.command(client, RESET_COMMAND)
            record["reset"] = reset if whole else {key: reset[key] for key in ("result_class", "timed_out", "error")}
        run = [self.command(client, "-exec-continue")]
        time.sleep(RESET_RUN_S if how in RESET_FIRST else END_RUN_S)
        run.append(self.command(client, "-exec-interrupt --all"))
        stop = self.stop(client)
        record["run"] = {"steps": run, "stop": stop} if whole else {"stop_reason": stop["reason"], "timed_out": stop["timed_out"]}
        before = self.core_state(client)
        record["before_end"] = before if whole else {key: before[key] for key in ("s_halt", COUNTER, "pc")}
        lines_before_end = len(server.output())
        record["end"] = self.end_session(server, client, how, whole=whole)
        output = server.output()
        record["output"] = output if whole else [entry["line"] for entry in output[lines_before_end:]]
        if shared is not None:
            record["shared_server_end"] = self.shared_server_end(shared, whole=whole)
        time.sleep(SETTLE_S)
        if cli_first:
            record["cli"] = cli()
        return record

    def verbose_idle(self, log_file: Path) -> dict:
        """What the server prints, at full logging, while the core sits halted with GDB idle, then runs, then is halted again.

        Ended by SIGTERM, which is the server's own shutdown."""
        server, ready = self.started(["-g", "-v", "-l", "31", "-f", str(log_file)])
        record: dict = {"scenario": "verbose_idle", "argv_tail": server.argv[1:], "ready_at_s": ready["at_s"] if ready else None}
        if ready is None:
            record.update(self.finish(server))
            return record
        client, connect = self.connect(self.port_of(server))
        record["connect"] = connect
        marks = {"connected": round(time.monotonic() - server.started, 2)}
        time.sleep(3.0)
        marks["idle_halted_until"] = round(time.monotonic() - server.started, 2)
        record["continue"] = self.command(client, "-exec-continue")
        time.sleep(1.0)
        marks["running_until"] = round(time.monotonic() - server.started, 2)
        record["interrupt"] = self.command(client, "-exec-interrupt --all")
        record["interrupt_stop"] = self.stop(client)
        marks["halted_again"] = round(time.monotonic() - server.started, 2)
        time.sleep(2.0)
        marks["idle_halted_again_until"] = round(time.monotonic() - server.started, 2)
        record["marks"] = marks
        record["server_terminated"] = server.terminate()
        self.live.remove(server)
        record["gdb_exit"] = self.close_client(client)
        record["output"] = server.output()
        return record

    def monitor_resets(self) -> dict:
        """Each reset form from a core halted at the handler, read back past GDB's cache, then run and interrupted."""
        server, ready = self.started(["-g"])
        record: dict = {"scenario": "monitor_resets", "argv_tail": server.argv[1:], "ready": ready, "resets": []}
        if ready is None:
            record.update(self.finish(server))
            return record
        client, connect = self.connect(self.port_of(server))
        record["connect"] = connect
        record["after_connect"] = self.core_state(client)
        for words in RESET_VARIANTS:
            entry: dict = {"words": words, "at_handler": self.halted_at_handler(client)}
            entry["monitor"] = self.monitor(client, words)
            entry["stop_poll"] = self.stop(client, 1.0)
            entry["after"] = self.core_state(client)
            time.sleep(0.5)
            entry["half_a_second_later"] = self.core_state(client)
            entry["run_and_interrupt"] = self.halted_by_interrupt(client)
            record["resets"].append(entry)
        record["server_killed"] = self.kill(server)
        record["server_output"] = server.output()
        record["gdb_exit"] = self.close_client(client)
        return record

    def persistent_session_detach(self) -> dict:
        """`-e` keeps the server up after its client leaves: whether the core is resumed when GDB detaches from it."""
        server, ready = self.started(["-g", "-e"])
        record: dict = {"scenario": "persistent_session_detach", "argv_tail": server.argv[1:], "ready": ready}
        if ready is None:
            record.update(self.finish(server))
            return record
        client, connect = self.connect(self.port_of(server))
        record["connect"] = connect
        record["at_handler"] = self.halted_at_handler(client)
        record["detach"] = self.command(client, "-target-detach")
        record["gdb_exit"] = self.close_client(client)
        time.sleep(SETTLE_S)
        record["settle_s"] = SETTLE_S
        record["server_running_after_detach"] = server.process.poll() is None
        if record["server_running_after_detach"]:
            record["listening_ports_after_detach"] = listening_ports(server.process.pid)
            again, again_connect = self.connect(self.port_of(server))
            record["second_client_connect"] = again_connect
            record["second_client_reads"] = self.core_state(again)
            record["server_killed"] = self.kill(server)
            record["second_client_exit"] = self.close_client(again)
        else:
            record.update(self.finish(server))
        record["server_output"] = server.output()
        time.sleep(SETTLE_S)
        record["killed_connect"] = self.counter_after_a_killed_connect()
        return record

    def ports(self) -> dict:
        """What the second listening port is, and what the server does when either port is taken."""
        record: dict = {}
        port, console = free_port(), free_port()
        server = self.start(self.server_argv(port, extra=["-g", "--semihost-console-port", str(console)]))
        ready = self.wait_until_listening(server, port, STARTUP_TIMEOUT_S)
        moved: dict = {"argv_tail": server.argv[1:], "ready": ready, "gdb_port": port, "semihost_console_port": console}
        if ready is not None and server.process.poll() is None:
            moved["listening_ports"] = listening_ports(server.process.pid)
            moved["listening_addresses"] = listening_addresses(server.process.pid)
        moved["server_killed"] = self.kill(server)
        moved["output"] = server.output()
        record["semihost_console_port_named"] = moved
        for label, offset in (("gdb_port_taken", 0), ("port_after_the_gdb_port_taken", 1)):
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as holder:
                holder.bind(("0.0.0.0", 0))
                taken = holder.getsockname()[1]
                holder.listen(1)
                gdb_port = taken - offset
                server = self.start(self.server_argv(gdb_port, extra=["-g"]))
                exited = server.wait_for_exit(EXIT_WAIT_S)
                entry: dict = {"argv_tail": server.argv[1:], "gdb_port": gdb_port, "taken_port": taken, "exited_on_its_own": exited is not None, "returncode": exited}
                if exited is None:
                    entry["listening_ports"] = listening_ports(server.process.pid)
                    entry["server_killed"] = self.kill(server)
                else:
                    for reader in server.readers:
                        reader.join(timeout=5.0)
                    if server in self.live:
                        self.live.remove(server)
                entry["output"] = server.output()
                record[label] = entry
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


def write_recording(recording: dict, root: Path, private_values: tuple[str, ...], record_property, name: str = OUTPUT_NAME) -> None:
    text = json.dumps(recording, indent=2, sort_keys=True)
    # The tree first, so its paths read as the placeholder rather than as a
    # redacted home with the rest of the path after it.
    text = text.replace(json.dumps(str(root))[1:-1], CUBECLT_PLACEHOLDER)
    redacted = redact_values(json.loads(text), private_values)
    rendered = json.dumps(redacted, indent=2, sort_keys=True) + "\n"
    directory = os.environ.get(OUTPUT_DIRECTORY_ENV)
    if directory:
        Path(directory).mkdir(parents=True, exist_ok=True)
        (Path(directory) / name).write_bytes(rendered.encode("utf-8"))
    record_property(name, json.dumps(redacted, sort_keys=True, separators=(",", ":")))


def recording_for(bench: Bench, firmware: Path, tmp_path: Path) -> tuple[Path, StLinkRecorder, tuple[str, ...], dict]:
    """The recorder for this bench's probe, the values a committed recording must not carry, and the recording's header."""
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
        "version": run_text([executable, "--version"], environment, recorder.cwd),
        "scenarios": {},
    }
    return root, recorder, private_values, recording


def test_record_st_link_gdbserver(bench: Bench, firmware: Path, gdb: None, tmp_path: Path, record_property) -> None:
    root, recorder, private_values, recording = recording_for(bench, firmware, tmp_path)
    recording["help"] = run_text([recorder.executable, "-h"], recorder.environment, recorder.cwd)
    recording["cwd_entries"] = directory_entries(Path(recorder.cwd))
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


def test_record_st_link_gdbserver_teardown(bench: Bench, firmware: Path, gdb: None, tmp_path: Path, record_property) -> None:
    """The second round: a server killed rather than terminated, the reset forms read past GDB's cache, `-e`, and the ports.

    The first round found every ending it tried resuming the core: GDB exiting,
    GDB detaching, and the server terminated under GDB, which prints "Shutting
    down..." first. This one asks whether a server that runs none of its own
    shutdown leaves the core where it was, and whether the probe opens again
    after it."""
    root, recorder, private_values, recording = recording_for(bench, firmware, tmp_path)
    scenarios = recording["scenarios"]
    try:
        scenarios["session_ended_by_killing_the_server"] = [recorder.session_ended_by_killing_the_server(cycle) for cycle in range(1, KILL_HALTED_CYCLES + 1)]
        scenarios["monitor_resets"] = recorder.monitor_resets()
        scenarios["persistent_session_detach"] = recorder.persistent_session_detach()
        scenarios["ports"] = recorder.ports()
    finally:
        recorder.cleanup()
        write_recording(recording, root, private_values, record_property, TEARDOWN_OUTPUT_NAME)
        restored = put_on_board(bench, firmware)
        assert restored.get("ok") is True, f"the demo could not be put back on the board: {restored.get('summary')}"


def product_log(bench: Bench, log_path: object) -> list[str] | None:
    """The lines of a log the product named in an answer, for a call that failed."""
    if not isinstance(log_path, str) or not log_path:
        return None
    path = bench.project / log_path
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as error:
        return [f"<unreadable: {type(error).__name__}>"]


def summarize_ends(entries: list[dict]) -> dict:
    """Per candidate: the starts and CLI calls refused, how each end exited, and the counter run over each end.

    The run over an end is only taken between two cycles with nothing in between
    but the pause and, every other cycle, the CLI call; a cycle that needed a
    heal is left out, because what heals the probe may move the core too."""
    refused_starts = [entry["cycle"] for entry in entries if entry.get("refused") is not None]
    cli_calls = [entry for entry in entries if "cli" in entry]
    refused_cli = [entry["cycle"] for entry in cli_calls if entry["cli"].get("ok") is not True]
    ran: dict[str, list[int]] = {"server": [], "cli": []}
    for previous, following in zip(entries, entries[1:], strict=False):
        if "heal" in previous or "before_end" not in previous or "at_connect" not in following:
            continue
        before, after = previous["before_end"].get(COUNTER), following["at_connect"].get(COUNTER)
        if before is None or after is None:
            continue
        ran[previous["first_opener_after_the_end"]].append(int(after) - int(before))
    returncodes: dict[str, int] = {}
    exited_after = []
    for entry in entries:
        if "end" not in entry:
            continue
        key = str(entry["end"]["returncode"])
        returncodes[key] = returncodes.get(key, 0) + 1
        if entry["end"]["exited_after_s"] is not None:
            exited_after.append(entry["end"]["exited_after_s"])
    return {
        "cycles": len(entries),
        "starts_refused_in_cycles": refused_starts,
        "cli_calls": len(cli_calls),
        "cli_refused_in_cycles": refused_cli,
        "ran_ms_over_the_end_next_opener_server": ran["server"],
        "ran_ms_over_the_end_next_opener_cli": ran["cli"],
        "end_returncodes": returncodes,
        "exited_after_s_max": max(exited_after) if exited_after else None,
        "not_halted_before_the_end_in_cycles": [entry["cycle"] for entry in entries if "before_end" in entry and entry["before_end"].get("s_halt") is not True],
    }


# The product's own MCP servers for the fourth round: on the stlink copy of the
# tier's configuration for the CLI call, and on the tier's own OpenOCD one for
# the open that was measured to give a refusing probe back.
stlink_bench = stlink_sessions.stlink_bench
stlink_servers = stlink_sessions.stlink_servers
mcp_servers = debug_sessions.mcp_servers


def test_record_st_link_gdbserver_ends(
    bench: Bench, firmware: Path, gdb: None, tmp_path: Path, record_property, stlink_servers, mcp_servers
) -> None:
    """The fourth round: each way of ending a session's server, cycle after cycle, and what the probe and the core do after it.

    The product ends a session's server with SIGKILL because every end the
    server runs itself resumed the core, and its bench tier then had one start
    in four refused with "USB communication error" until an OpenOCD open. So
    each candidate end is run many times in a row with the core halted before
    it: what the core ran over it, read from the counter at the next connect;
    whether the next start comes up; and, every other cycle, whether the CLI
    answers first. A refused start or CLI call is recorded whole, then tried
    again, and then the probe is opened once through OpenOCD by the product and
    tried once more, so the recording says what gave it back."""
    root, recorder, private_values, recording = recording_for(bench, firmware, tmp_path)
    asked = [name.strip() for name in os.environ.get(ENDS_ENV, ",".join(END_CANDIDATES)).split(",") if name.strip()]
    unknown = [name for name in asked if name not in END_CANDIDATES and name != "verbose_idle"]
    assert not unknown, f"{ENDS_ENV} names ends this round does not know: {unknown}"
    cycles = int(os.environ.get(END_CYCLES_ENV) or DEFAULT_END_CYCLES)
    recording["ends_asked"] = asked
    recording["cycles_per_end"] = cycles
    recording["end_run_s"] = END_RUN_S
    recording["summary"] = {}
    scenarios = recording["scenarios"]
    product = stlink_servers()

    def cli(*, whole: bool = False) -> dict:
        answer = product.tool("probe_target")
        entry = {key: answer.get(key) for key in ("ok", "error_type", "backend_error_type", "summary")}
        if whole or answer.get("ok") is not True:
            entry["answer"] = answer
            entry["log"] = product_log(bench, answer.get("log_path"))
        return entry

    def heal() -> dict:
        record: dict = {"start_again": recorder.startup(["-g"], "start_again"), "cli_again": cli(whole=True)}
        if record["start_again"]["ready"] is not None and record["cli_again"]["ok"] is True:
            return record
        openocd = mcp_servers()
        opened = openocd.tool("probe_target")
        record["openocd_probe_target"] = {key: opened.get(key) for key in ("ok", "error_type", "summary", "log_path")}
        openocd.shut_down()
        record["start_after_openocd"] = recorder.startup(["-g"], "start_after_openocd")
        record["cli_after_openocd"] = cli(whole=True)
        return record

    try:
        for how in asked:
            if how == "verbose_idle":
                scenarios["verbose_idle"] = recorder.verbose_idle(tmp_path / "st-link-gdbserver-verbose.log")
                continue
            entries: list[dict] = []
            for cycle in range(1, cycles + 1):
                entry = recorder.end_cycle(how, cycle, cycle % 2 == 0, cli)
                if entry.get("refused") is not None or ("cli" in entry and entry["cli"].get("ok") is not True):
                    entry["heal"] = heal()
                entries.append(entry)
            scenarios[f"ends_{how}"] = entries
            recording["summary"][how] = summarize_ends(entries)
    finally:
        recorder.cleanup()
        write_recording(recording, root, private_values, record_property, ENDS_OUTPUT_NAME)
        restored = put_on_board(bench, firmware)
        assert restored.get("ok") is True, f"the demo could not be put back on the board: {restored.get('summary')}"


def test_record_st_link_gdbserver_restart(bench: Bench, firmware: Path, gdb: None, tmp_path: Path, record_property) -> None:
    """The third round: the next server started right after a session's server was killed, and after waits.

    The product ends a session's server with SIGKILL, and its bench tier had a
    session start, right after another session ended that way, refused before
    the GDB port opened: "Target USB comms error", then "USB communication
    error. Please reconnect the ST-LINK USB cable and try again." This round
    asks how often that happens at once, whether a wait after the kill avoids
    it, and whether a start tried again at once after it comes up."""
    root, recorder, private_values, recording = recording_for(bench, firmware, tmp_path)
    scenarios = recording["scenarios"]
    try:
        scenarios["restart_at_once"] = []
        for cycle in range(1, RESTART_CYCLES + 1):
            scenarios["restart_at_once"].append(recorder.restart_after_a_kill(cycle, 0.0))
            time.sleep(SETTLE_S)
        scenarios["restart_after_a_wait"] = []
        for delay_s in RESTART_DELAYS_S:
            for cycle in range(1, RESTART_CYCLES_PER_DELAY + 1):
                scenarios["restart_after_a_wait"].append(recorder.restart_after_a_kill(cycle, delay_s))
                time.sleep(SETTLE_S)
    finally:
        recorder.cleanup()
        write_recording(recording, root, private_values, record_property, RESTART_OUTPUT_NAME)
        restored = put_on_board(bench, firmware)
        assert restored.get("ok") is True, f"the demo could not be put back on the board: {restored.get('summary')}"
