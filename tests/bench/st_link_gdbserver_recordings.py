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
`st-link-gdbserver-restart-recording.json`, the fourth, the ends one, as
`st-link-gdbserver-ends-recording.json`, the fifth, the stops one, as
`st-link-session-stops-recording.json`, the sixth, the stlink-server one,
as `st-link-server-race-recording.json`, and the seventh, the restart one
through stlink-server, as `st-link-server-restart-recording.json`, and the
eighth, the sharing one, as `st-link-server-sharing-recording.json`, and the ninth, the ends of
stlink-server, as `st-link-server-ends-recording.json`; each is always attached to the test
report as a property as well, which is how a run in the bench image, whose
environment this module cannot set, hands its recording out. The fourth also makes calls through the product's
own MCP server, on a copy of the tier's configuration with the probe on `type:
stlink` and on the tier's own configuration, because what it asks is whether
the product's next call still reaches the probe. The fifth runs the product's
own sessions through that server and drives no server or GDB itself. The
eighth does both: the product's calls and sessions, and a server and GDB of
its own beside them. The ninth calls the product's OpenOCD probe_target after its
stlink-server ends.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import socket
import subprocess
import tempfile
import time
from contextlib import suppress
from pathlib import Path

import pytest
from support import scaled_time_bound

from agentic_hil.backends.stlink import STLINK_SERVER_LISTEN_TIMEOUT_S, stlink_server_listening
from agentic_hil.gdbmi import GdbMiClient, mi_field
from agentic_hil.process import terminate_process_tree

from . import test_bench_debug_sessions as debug_sessions
from . import test_bench_stlink_sessions as stlink_sessions
from .conftest import BENCH_ONLY, DEMO_IMAGE, Bench, built_where_it_stands, put_on_board
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
# The fifth round: the product's own session stops, through its MCP server.
STOPS_OUTPUT_NAME = "st-link-session-stops-recording.json"
STOP_CYCLES_ENV = "AGENTIC_HIL_RECORDING_STOP_CYCLES"
DEFAULT_STOP_CYCLES = 100
# The sixth round: a session start through stlink-server made the way the
# product makes one, over and over, in a few variants taken in turn. In the
# bench image the product's starts were refused now and then with "Failed to
# connect to device." while the stlink-server they reached printed `TCPCMD
# OPEN_DEV FAIL, internal assoc not key created`, and the GDB server's stderr
# carried a second stlink-server's "stlinkserver already running, exit": the
# USB driver library ST-LINK_gdbserver loads names `LaunchServer`, `fork` and
# `execvp` among its symbols. Each variant changes one thing about the start.
RACE_OUTPUT_NAME = "st-link-server-race-recording.json"
RACE_CYCLES_ENV = "AGENTIC_HIL_RECORDING_RACE_CYCLES"
DEFAULT_RACE_CYCLES = 80
RACE_VARIANTS: dict[str, dict] = {
    # The product's start: its own stlink-server, the port listening, then the
    # GDB server with `-t` in the product's own environment.
    "on_path": {"stlink_server_on_path": True, "wait_after_listening_s": 0.0},
    # The same with every PATH entry that holds an stlink-server left out of the
    # GDB server's environment, so its driver finds none to start.
    "off_path": {"stlink_server_on_path": False, "wait_after_listening_s": 0.0},
    # The product's start with a pause between the port listening and the GDB
    # server's start, for a server that listens before it can open the probe.
    "on_path_after_a_wait": {"stlink_server_on_path": True, "wait_after_listening_s": 1.0},
}
# Refused starts in a row after which the round stops starting servers at a probe that answers none.
RACE_REFUSALS_IN_A_ROW_LIMIT = 5
# The seventh round: the same starts with no pause before them. In the sixth
# round every start came up, with the tier's pause after each cycle; in the
# tier, the starts that were refused were the ones made with no pause after the
# session before them had ended. Each block takes one way for stlink-server to
# live: started for every session and ended with it in the product's order (the
# GDB server killed, stlink-server ended at once, then GDB closed), or started
# once and kept running across the block's sessions.
RESTART_OUTPUT_NAME = "st-link-server-restart-recording.json"
RESTART_CYCLES_ENV = "AGENTIC_HIL_RECORDING_RESTART_CYCLES"
DEFAULT_RESTART_CYCLES = 40
RESTART_BLOCKS: tuple[tuple[str, dict], ...] = (
    ("restarted_at_once", {"stlink_server": "per_session", "pause_before_start_s": 0.0}),
    ("kept_running", {"stlink_server": "kept_running", "pause_before_start_s": 0.0}),
    ("restarted_after_a_pause", {"stlink_server": "per_session", "pause_before_start_s": 2.0}),
    ("restarted_at_once_again", {"stlink_server": "per_session", "pause_before_start_s": 0.0}),
)
# The eighth round: stlink-server shared. Whether one left running with no
# client keeps the probe from the product's other openers (STM32_Programmer_CLI
# and OpenOCD), and what a second GDB server on the same probe, through the same
# stlink-server, can still do once the product's session that started that
# stlink-server has stopped.
SHARING_OUTPUT_NAME = "st-link-server-sharing-recording.json"
SHARING_CYCLES_ENV = "AGENTIC_HIL_RECORDING_SHARING_CYCLES"
DEFAULT_SHARING_CYCLES = 3
# The ninth round: how a session's stlink-server is ended. In the seventh,
# starts made right after an stlink-server was ended in the product's order
# (the GDB server killed, stlink-server terminated at once) were refused with
# `TCPCMD OPEN_DEV FAIL` in 36 of 120 cycles, a pause of 2 s before the start
# changing nothing; the sixth, which closed GDB before it ended stlink-server,
# had none in 240. Each variant waits for something else between the kill and
# stlink-server's SIGTERM: nothing, stlink-server's side of the GDB server's
# connection closed, its USB node released, or half a second. The variants are
# taken in turn and every start is made at once, so each start meets the end
# of the variant before it. The cycles after them each end their
# stlink-server one of two ways and are followed at once by the product's
# probe_target on the tier's own configuration (OpenOCD) instead.
SERVER_ENDS_OUTPUT_NAME = "st-link-server-ends-recording.json"
SERVER_END_CYCLES_ENV = "AGENTIC_HIL_RECORDING_SERVER_END_CYCLES"
DEFAULT_SERVER_END_CYCLES = 40
SERVER_END_VARIANTS = ("at_once", "after_its_client_closed", "after_the_usb_released", "after_half_a_second")
SERVER_END_WAIT_LIMIT_S = 3.0
SERVER_END_OPENOCD_VARIANTS = ("at_once", "after_its_client_closed")
SERVER_END_OPENOCD_CYCLES = 10
# /proc/net/tcp's state codes.
TCP_STATES = {"01": "ESTABLISHED", "02": "SYN_SENT", "03": "SYN_RECV", "04": "FIN_WAIT1", "05": "FIN_WAIT2", "06": "TIME_WAIT", "07": "CLOSE", "08": "CLOSE_WAIT", "09": "LAST_ACK", "0A": "LISTEN", "0B": "CLOSING"}


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

    def race_cycle(self, variant: str, cycle: int, stlink_server: str, *, whole: bool) -> dict:
        """One session start through stlink-server in `variant`, and the product's stop after it.

        stlink-server is started as the product starts it, with no options from
        its own directory, and asked whether it listens the way the product asks,
        with a connect every 50 ms. The GDB server is then started with `-t`; if
        it comes up, GDB attaches, the core's state is read, and the session is
        ended as the product ends one: the GDB server killed, GDB closed, then
        stlink-server terminated. `whole` keeps every line either server
        printed; the other cycles keep stlink-server's lines and the GDB
        server's stderr, where the second stlink-server prints."""
        settings = RACE_VARIANTS[variant]
        record: dict = {"variant": variant, "cycle": cycle}
        probe_server = GdbServer([stlink_server], self.environment, str(Path(stlink_server).parent))
        self.live.append(probe_server)
        deadline = probe_server.started + STLINK_SERVER_LISTEN_TIMEOUT_S
        listening = False
        while time.monotonic() < deadline and probe_server.process.poll() is None:
            if stlink_server_listening(STLINK_SERVER_PORT):
                listening = True
                break
            time.sleep(0.05)
        record["probe_server_listening_after_ms"] = int((time.monotonic() - probe_server.started) * 1000) if listening else None
        if listening:
            if settings["wait_after_listening_s"]:
                time.sleep(settings["wait_after_listening_s"])
            environment = dict(self.environment)
            if not settings["stlink_server_on_path"]:
                environment["PATH"] = path_without_stlink_server(environment.get("PATH", ""))
            port = free_port()
            server = GdbServer(self.server_argv(port, extra=["-g", "-t"]), environment, self.cwd)
            self.live.append(server)
            record["gdb_server_started_after_ms"] = int((server.started - probe_server.started) * 1000)
            if whole:
                record["argv_tail"] = server.argv[1:]
            ready = self.wait_until_listening(server, port, STARTUP_TIMEOUT_S)
            record["ready_at_s"] = ready["at_s"] if ready is not None else None
            if ready is None:
                record["refused"] = self.finish(server)
            else:
                record["processes_at_ready"] = processes_beside(probe_server.process.pid, server.process.pid)
                client, connect = self.connect(port)
                if whole:
                    record["connect"] = connect
                at_connect = self.core_state(client)
                record["at_connect"] = at_connect if whole else {key: at_connect[key] for key in ("s_halt", COUNTER, "pc")}
                record["end"] = self.kill(server)
                closed = self.close_client(client)
                record["gdb_exit"] = closed if whole else {"closed": closed["closed"], "error": closed.get("error")}
                output = server.output()
                record["gdb_server_output"] = output if whole else [line for line in output if line["stream"] == "stderr"]
        record["probe_server_end"] = probe_server.terminate()
        if probe_server in self.live:
            self.live.remove(probe_server)
        record["probe_server_output"] = probe_server.output()
        record["left_running"] = [process["name"] for process in all_processes() if process["name"] in ("stlink-server", "ST-LINK_gdbserver")]
        return record

    def probe_server_started(self, stlink_server: str) -> tuple[GdbServer, int | None]:
        """stlink-server started as the product starts it, and how long it took to listen, or None if it never did."""
        probe_server = GdbServer([stlink_server], self.environment, str(Path(stlink_server).parent))
        self.live.append(probe_server)
        deadline = probe_server.started + STLINK_SERVER_LISTEN_TIMEOUT_S
        while time.monotonic() < deadline and probe_server.process.poll() is None:
            if stlink_server_listening(STLINK_SERVER_PORT):
                return probe_server, int((time.monotonic() - probe_server.started) * 1000)
            time.sleep(0.05)
        return probe_server, None

    def probe_server_ended(self, probe_server: GdbServer) -> dict:
        ended = probe_server.terminate()
        if probe_server in self.live:
            self.live.remove(probe_server)
        return ended

    def restart_cycle(self, block: str, cycle: int, stlink_server: str, kept: GdbServer | None, ended_at: float | None, *, whole: bool) -> tuple[dict, float]:
        """One session start in `block` and the product's stop after it; returns the record and when the cycle's last end was.

        With `kept` the start reaches that stlink-server, which stays running;
        without, one is started for this session and ended with it, in the
        product's order. `ended_at` is when the cycle before ended, so the record
        says how long after it this start was made."""
        record: dict = {"block": block, "cycle": cycle}
        began = time.monotonic()
        if ended_at is not None:
            record["started_after_the_last_end_ms"] = int((began - ended_at) * 1000)
        if kept is None:
            probe_server, listening_after_ms = self.probe_server_started(stlink_server)
            record["probe_server_listening_after_ms"] = listening_after_ms
            lines_before = 0
        else:
            probe_server, listening_after_ms = kept, 0
            lines_before = len(kept.output())
        if listening_after_ms is not None:
            port = free_port()
            server = GdbServer(self.server_argv(port, extra=["-g", "-t"]), self.environment, self.cwd)
            self.live.append(server)
            if whole:
                record["argv_tail"] = server.argv[1:]
            ready = self.wait_until_listening(server, port, STARTUP_TIMEOUT_S)
            record["ready_at_s"] = ready["at_s"] if ready is not None else None
            if ready is None:
                record["refused"] = self.finish(server)
            else:
                client, connect = self.connect(port)
                if whole:
                    record["connect"] = connect
                at_connect = self.core_state(client)
                record["at_connect"] = at_connect if whole else {key: at_connect[key] for key in ("s_halt", COUNTER, "pc")}
                record["end"] = self.kill(server)
                if kept is None:
                    record["probe_server_end"] = self.probe_server_ended(probe_server)
                closed = self.close_client(client)
                record["gdb_exit"] = closed if whole else {"closed": closed["closed"], "error": closed.get("error")}
                output = server.output()
                record["gdb_server_output"] = output if whole else [line for line in output if line["stream"] == "stderr"]
        if kept is None and "probe_server_end" not in record:
            record["probe_server_end"] = self.probe_server_ended(probe_server)
        record["probe_server_output"] = probe_server.output()[lines_before:]
        record["left_running"] = sorted(process["name"] for process in all_processes() if process["name"] in ("stlink-server", "ST-LINK_gdbserver"))
        return record, time.monotonic()

    def server_end_cycle(self, variant: str, cycle: int, stlink_server: str, previous: str | None, *, whole: bool) -> dict:
        """One session through an stlink-server started for it, its GDB server killed, and that stlink-server ended as `variant` says.

        `previous` is how the cycle before ended its stlink-server, which is what
        this cycle's start meets. stlink-server's sockets on its port and the
        USB nodes it holds are read with a client, right after the kill and
        right before the SIGTERM."""
        record: dict = {"variant": variant, "cycle": cycle, "previous_end": previous}
        probe_server, record["probe_server_listening_after_ms"] = self.probe_server_started(stlink_server)
        pid = probe_server.process.pid
        if record["probe_server_listening_after_ms"] is not None:
            port = free_port()
            server = GdbServer(self.server_argv(port, extra=["-g", "-t"]), self.environment, self.cwd)
            self.live.append(server)
            ready = self.wait_until_listening(server, port, STARTUP_TIMEOUT_S)
            record["ready_at_s"] = ready["at_s"] if ready is not None else None
            if ready is None:
                record["refused"] = self.finish(server)
            else:
                client, _ = self.connect(port)
                record["at_connect"] = {key: value for key, value in self.core_state(client).items() if key in ("s_halt", COUNTER, "pc")}
                record["with_a_client"] = {"connections": server_side_states(STLINK_SERVER_PORT), "usb_handles": usb_handles(pid)}
                record["end"] = self.kill(server)
                killed = time.monotonic()
                record["killed_at_s"] = round(killed - probe_server.started, 3)
                record["at_the_kill"] = {"connections": server_side_states(STLINK_SERVER_PORT), "usb_handles": usb_handles(pid)}
                record["waited"] = wait_before_the_server_end(variant, pid, killed)
                if variant != "at_once":
                    record["before_the_end"] = {"connections": server_side_states(STLINK_SERVER_PORT), "usb_handles": usb_handles(pid)}
                if whole:
                    record["tcp_lines_before_the_end"] = tcp_lines(STLINK_SERVER_PORT)
                record["probe_server_end"] = self.probe_server_ended(probe_server)
                record["probe_server_ended_at_s"] = round(time.monotonic() - probe_server.started, 3)
                record["gdb_exit"] = {key: value for key, value in self.close_client(client).items() if key in ("closed", "error")}
                output = server.output()
                record["gdb_server_output"] = output if whole else [line for line in output if line["stream"] == "stderr"]
        if "probe_server_end" not in record:
            record["probe_server_end"] = self.probe_server_ended(probe_server)
        record["probe_server_output"] = probe_server.output()
        record["left_running"] = sorted(process["name"] for process in all_processes() if process["name"] in ("stlink-server", "ST-LINK_gdbserver"))
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


def path_without_stlink_server(value: str) -> str:
    """PATH with every entry that holds an stlink-server left out."""
    return os.pathsep.join(entry for entry in value.split(os.pathsep) if entry and not (Path(entry) / "stlink-server").is_file())


def all_processes() -> list[dict]:
    """Every process this PID namespace shows: its pid, its parent and the name of what it runs."""
    found = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            argv = (entry / "cmdline").read_bytes().split(b"\0")
            stat = (entry / "stat").read_text(encoding="ascii", errors="replace")
        except OSError:
            continue
        name = Path(argv[0].decode(errors="replace")).name if argv and argv[0] else ""
        found.append({"pid": int(entry.name), "ppid": int(stat.rsplit(")", 1)[1].split()[1]), "name": name})
    return found


def processes_beside(probe_server_pid: int, gdb_server_pid: int) -> list[dict]:
    """The stlink-server and ST-LINK_gdbserver processes running, and the GDB server's children, by how they relate to the two this cycle started."""
    related = []
    for process in all_processes():
        if process["pid"] in (probe_server_pid, gdb_server_pid):
            continue
        if process["ppid"] == gdb_server_pid or process["name"] in ("stlink-server", "ST-LINK_gdbserver"):
            relation = "child of the GDB server" if process["ppid"] == gdb_server_pid else "started by neither"
            related.append({"name": process["name"], "relation": relation})
    return related


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
    # The account's own home as well as the one the tier gives its runs: an
    # executable this round was pointed at from outside the tier (stlink-server)
    # prints its path under the account's home.
    import pwd

    try:
        account_home = pwd.getpwuid(os.getuid()).pw_dir
    except KeyError:
        # The bench image runs as an account its passwd file need not name.
        account_home = str(Path.home())
    private_values = (serial, str(bench.project), str(bench.config_root), str(bench.state_root), str(tmp_path), str(Path.home()), account_home, str(firmware.parent))
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
        assert restored.get("ok") is True, f"the demo could not be put back on the board: {restored.get('summary')}{chr(10)}{json.dumps(restored, indent=1, default=str)}"


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
        assert restored.get("ok") is True, f"the demo could not be put back on the board: {restored.get('summary')}{chr(10)}{json.dumps(restored, indent=1, default=str)}"


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
        assert restored.get("ok") is True, f"the demo could not be put back on the board: {restored.get('summary')}{chr(10)}{json.dumps(restored, indent=1, default=str)}"


def product_answer(answer: dict) -> dict:
    """The fields of a product answer a stop round keeps for every call."""
    return {key: answer.get(key) for key in ("ok", "error_type", "backend_error_type", "backend_error", "summary", "safe_state_confirmed", "halt_not_confirmed", "elapsed_ms") if key in answer}


def refused_session_start(bench: Bench, answer: dict) -> dict:
    """A session start the product refused, with what the server printed before it exited, from the session's own log."""
    record = {"answer": answer}
    lines = product_log(bench, answer.get("log_path"))
    if lines is not None:
        with suppress(ValueError):
            logged = json.loads(chr(10).join(lines))
            record["server_command"] = logged.get("server_command")
            record["server_stdout"] = str(logged.get("server_stdout_tail") or "").splitlines()
            record["server_stderr"] = str(logged.get("server_stderr_tail") or "").splitlines()
    return record


START_KEYS = ("reset_halt_start", "attach_start", "second_attach_start")


def summarize_stops(entries: list[dict]) -> dict:
    """The stop round's counts: every stop, every opener after one, and what the core ran over the stops a start followed."""
    stops = [stop for entry in entries for stop in (entry.get("reset_halt_stop"), entry.get("attach_stop"), entry.get("second_attach_stop")) if stop is not None]
    starts = [entry[key] for entry in entries for key in START_KEYS if key in entry]
    clis = [entry["cli"] for entry in entries if "cli" in entry]
    return {
        "cycles": len(entries),
        "stops": len(stops),
        "stops_not_confirmed": len([stop for stop in stops if stop.get("ok") is not True or stop.get("safe_state_confirmed") is not True]),
        "starts": len(starts),
        "starts_refused_in_cycles": [entry["cycle"] for entry in entries for key in START_KEYS if key in entry and entry[key].get("ok") is not True],
        "cli_calls": len(clis),
        "cli_refused_in_cycles": [entry["cycle"] for entry in entries if "cli" in entry and entry["cli"].get("ok") is not True],
        "ran_ms_over_the_stop": [entry["ran_ms_over_the_stop"] for entry in entries if "ran_ms_over_the_stop" in entry],
        "ran_ms_over_the_attach_stop": [entry["ran_ms_over_the_attach_stop"] for entry in entries if "ran_ms_over_the_attach_stop" in entry],
        "detach_guards": sorted({json.dumps({key: value for key, value in (stop.get("detach_guard") or {}).items() if key != "server_returncode"}, sort_keys=True) for stop in stops}),
        # How each stopped session reached the probe: through stlink-server or itself.
        "probe_servers": sorted({str((stop.get("probe_server") or {}).get("mode")) for stop in stops}),
    }


def test_record_st_link_session_stops(
    bench: Bench, firmware: Path, gdb: None, tmp_path: Path, record_property, stlink_bench: Bench, stlink_servers, mcp_servers
) -> None:
    """The fifth round: the product's own session stops, cycle after cycle, and whether the probe answers the next opener.

    The direct ends of the fourth round never had a start refused, where the
    product's own reset-halt sessions had one in about twenty refused right
    after a stop. So this round goes through the product's MCP server as a
    client does. Each cycle is a reset-halt session that a resume nothing stops
    runs out on, a stop, the pause, an attach session (the next server start,
    which reads what the core ran over the stop), a stop, the pause, a second
    attach session (which reads what the core ran over an attach session's
    stop), a stop, the pause and a `probe_target` (the next CLI call). A
    refused start or CLI call is recorded
    with the server's or the CLI's own lines, and then what gives the probe
    back is tried in turn and recorded: the same call again, an OpenOCD open,
    a second OpenOCD open, an OpenOCD reset, and the demo put back on the
    board."""
    root, _, private_values, recording = recording_for(bench, firmware, tmp_path)
    cycles = int(os.environ.get(STOP_CYCLES_ENV) or DEFAULT_STOP_CYCLES)
    recording["cycles_asked"] = cycles
    recording["stop_round"] = {"continue_timeout_s": stlink_sessions.UNREACHABLE_STOP_TIMEOUT_S, "settle_s": stlink_sessions.SETTLE_S}
    product = stlink_servers()
    image = debug_sessions.workspace_image(stlink_bench, firmware)
    entries: list[dict] = []

    def cli() -> dict:
        answer = product.tool("probe_target")
        entry = product_answer(answer)
        if answer.get("ok") is not True:
            entry["log"] = product_log(bench, answer.get("log_path"))
        return entry

    def start(mode: str) -> tuple[dict, bool]:
        answer = product.tool("debug_start_session", {"image_path": image, "mode": mode})
        if answer.get("ok") is True:
            return product_answer(answer), True
        return {**product_answer(answer), "refused": refused_session_start(bench, answer)}, False

    def stop() -> dict:
        answer = product.tool("debug_stop_session")
        entry = product_answer(answer)
        lines = product_log(bench, answer.get("log_path"))
        if lines is not None:
            with suppress(ValueError):
                logged = json.loads(chr(10).join(lines))
                entry["detach_guard"] = logged.get("detach_guard")
                entry["probe_server"] = logged.get("probe_server")
        return entry

    def give_back() -> dict:
        """What gives a refusing probe back, tried in turn until a start comes up."""
        record: dict = {"cli": cli()}
        again, ready = start("attach")
        record["start_again"] = again
        if ready:
            record["stop_again"] = stop()
            return record
        openocd = mcp_servers()
        record["openocd_probe_target"] = product_answer(openocd.tool("probe_target"))
        record["start_after_openocd_open"], ready = start("attach")
        if ready:
            record["stop_after_openocd_open"] = stop()
            openocd.shut_down()
            return record
        record["cli_after_openocd_open"] = cli()
        # A second open apart from the reset: in the first product stop cycles
        # on the bench, the first open never gave the probe back and a reset
        # after it always did, with its own examination failing, so the second
        # open alone is tried first.
        record["openocd_probe_target_again"] = product_answer(openocd.tool("probe_target"))
        record["cli_after_second_openocd_open"] = cli()
        record["start_after_second_openocd_open"], ready = start("attach")
        if ready:
            record["stop_after_second_openocd_open"] = stop()
            openocd.shut_down()
            return record
        record["openocd_reset_target"] = product_answer(openocd.tool("reset_target", {"mode": "run"}))
        openocd.shut_down()
        record["cli_after_openocd_reset"] = cli()
        record["start_after_openocd_reset"], ready = start("attach")
        if ready:
            record["stop_after_openocd_reset"] = stop()
            return record
        record["put_on_board"] = product_answer(put_on_board(bench, firmware))
        record["start_after_put_on_board"], ready = start("attach")
        if ready:
            record["stop_after_put_on_board"] = stop()
        return record

    try:
        for cycle in range(1, cycles + 1):
            entry: dict = {"cycle": cycle}
            entries.append(entry)
            entry["reset_halt_start"], ready = start("reset_halt")
            if not ready:
                entry["given_back"] = give_back()
                continue
            entry["continue"] = product_answer(product.tool("debug_continue", {"timeout_s": stlink_sessions.UNREACHABLE_STOP_TIMEOUT_S}))
            before = product.tool("debug_symbol_value", {"symbol": COUNTER})
            entry["reset_halt_stop"] = stop()
            time.sleep(stlink_sessions.SETTLE_S)
            entry["attach_start"], ready = start("attach")
            if not ready:
                entry["given_back"] = give_back()
                continue
            after = product.tool("debug_symbol_value", {"symbol": COUNTER})
            if isinstance(before.get("value_unsigned"), int) and isinstance(after.get("value_unsigned"), int):
                entry["ran_ms_over_the_stop"] = after["value_unsigned"] - before["value_unsigned"]
            entry["attach_stop"] = stop()
            time.sleep(stlink_sessions.SETTLE_S)
            entry["second_attach_start"], ready = start("attach")
            if not ready:
                entry["given_back"] = give_back()
                continue
            again = product.tool("debug_symbol_value", {"symbol": COUNTER})
            if isinstance(after.get("value_unsigned"), int) and isinstance(again.get("value_unsigned"), int):
                entry["ran_ms_over_the_attach_stop"] = again["value_unsigned"] - after["value_unsigned"]
            entry["second_attach_stop"] = stop()
            time.sleep(stlink_sessions.SETTLE_S)
            entry["cli"] = cli()
            if entry["cli"].get("ok") is not True:
                entry["given_back"] = give_back()
    finally:
        recording["scenarios"]["session_stops"] = entries
        recording["summary"] = summarize_stops(entries)
        write_recording(recording, root, private_values, record_property, STOPS_OUTPUT_NAME)
        restored = put_on_board(bench, firmware)
        assert restored.get("ok") is True, f"the demo could not be put back on the board: {restored.get('summary')}{chr(10)}{json.dumps(restored, indent=1, default=str)}"


def summarize_race(entries: list[dict]) -> dict:
    """Per variant: the starts, the refused ones with the decisive lines, and how often a second stlink-server spoke."""
    summary: dict = {}
    for variant in RACE_VARIANTS:
        cycles = [entry for entry in entries if entry["variant"] == variant]
        refused = [entry for entry in cycles if entry.get("ready_at_s") is None]

        def lines(entry: dict, key: str) -> list[str]:
            return [line["line"] for line in entry.get(key) or [] if isinstance(line, dict)]

        def gdb_server_lines(entry: dict) -> list[str]:
            return lines(entry, "gdb_server_output") + lines(entry.get("refused") or {}, "output")

        summary[variant] = {
            "cycles": len(cycles),
            "refused_in_cycles": [entry["cycle"] for entry in refused],
            "refused_reasons": sorted({line for entry in refused for line in gdb_server_lines(entry) if line.startswith("Reason:")}),
            "probe_server_open_dev_fail_in_cycles": [entry["cycle"] for entry in cycles if any("OPEN_DEV FAIL" in line for line in lines(entry, "probe_server_output"))],
            "second_stlink_server_said_already_running_in": len([entry for entry in cycles if any("already running" in line for line in gdb_server_lines(entry))]),
            "left_running_after_a_cycle": sorted({name for entry in cycles for name in entry.get("left_running") or []}),
            "processes_at_ready": sorted({json.dumps(process, sort_keys=True) for entry in cycles for process in entry.get("processes_at_ready") or []}),
            "not_halted_at_connect_in_cycles": [entry["cycle"] for entry in cycles if "at_connect" in entry and entry["at_connect"].get("s_halt") is not True],
        }
    return summary


def test_record_st_link_server_race(bench: Bench, firmware: Path, gdb: None, tmp_path: Path, record_property) -> None:
    """The sixth round: session starts through stlink-server, variant after variant, and which of them the probe refused.

    The variants are taken in turn, one cycle each, so whatever drifts over the
    round drifts under all of them alike; a pause of the tier's own length
    follows every cycle, as it follows a stop in the tier. stlink-server is the
    one this run's PATH names (the bench image puts the one it extracted from
    STM32CubeCLT there), or the one `AGENTIC_HIL_RECORDING_STLINK_SERVER`
    names. A start refused that many times in a row ends the round early."""
    root, recorder, private_values, recording = recording_for(bench, firmware, tmp_path)
    stlink_server = os.environ.get(STLINK_SERVER_ENV) or shutil.which("stlink-server")
    if not stlink_server or not Path(stlink_server).is_file():
        pytest.fail(f"no stlink-server on PATH and none named by {STLINK_SERVER_ENV}", pytrace=False)
    cycles = int(os.environ.get(RACE_CYCLES_ENV) or DEFAULT_RACE_CYCLES)
    recording["race"] = {
        "variants": RACE_VARIANTS,
        "cycles_per_variant": cycles,
        "pause_after_each_cycle_s": stlink_sessions.SETTLE_S,
        "stlink_server_sha256": hashlib.sha256(Path(stlink_server).read_bytes()).hexdigest(),
        "stlink_server_on_path": shutil.which("stlink-server") == stlink_server,
        "in_a_container": {"dockerenv": Path("/.dockerenv").exists(), "containerenv": Path("/run/.containerenv").exists()},
    }
    entries: list[dict] = []
    refusals_in_a_row = 0
    try:
        for cycle in range(1, cycles + 1):
            for variant in RACE_VARIANTS:
                entry = recorder.race_cycle(variant, cycle, stlink_server, whole=cycle == 1)
                entries.append(entry)
                refusals_in_a_row = refusals_in_a_row + 1 if entry.get("ready_at_s") is None else 0
                time.sleep(stlink_sessions.SETTLE_S)
                if refusals_in_a_row >= RACE_REFUSALS_IN_A_ROW_LIMIT:
                    break
            if refusals_in_a_row >= RACE_REFUSALS_IN_A_ROW_LIMIT:
                recording["race"]["ended_early"] = f"{refusals_in_a_row} starts in a row were refused"
                break
    finally:
        recorder.cleanup()
        recording["scenarios"]["race"] = entries
        recording["summary"] = summarize_race(entries)
        write_recording(recording, root, private_values, record_property, RACE_OUTPUT_NAME)
        restored = put_on_board(bench, firmware)
        assert restored.get("ok") is True, f"the demo could not be put back on the board: {restored.get('summary')}{chr(10)}{json.dumps(restored, indent=1, default=str)}"


def summarize_restarts(entries: list[dict]) -> dict:
    """Per block: the starts, the refused ones with the decisive lines of both servers, and whether the next start came up."""
    summary: dict = {}
    for block, _ in RESTART_BLOCKS:
        cycles = [entry for entry in entries if entry["block"] == block]
        refused = [entry for entry in cycles if entry.get("ready_at_s") is None]

        def lines(entry: dict, key: str) -> list[str]:
            return [line["line"] for line in entry.get(key) or [] if isinstance(line, dict)]

        following = []
        for entry in refused:
            index = entries.index(entry)
            if index + 1 < len(entries):
                following.append({"cycle": entry["cycle"], "next_block": entries[index + 1]["block"], "next_came_up": entries[index + 1].get("ready_at_s") is not None})
        summary[block] = {
            "cycles": len(cycles),
            "refused_in_cycles": [entry["cycle"] for entry in refused],
            "refused_reasons": sorted({line for entry in refused for line in lines(entry.get("refused") or {}, "output") if line.startswith("Reason:")}),
            "probe_server_lines_at_refusals": sorted({line for entry in refused for line in lines(entry, "probe_server_output") if line.startswith("Error:") and "recv returned 0" not in line}),
            "probe_server_open_dev_fail_in_cycles": [entry["cycle"] for entry in cycles if any("OPEN_DEV FAIL" in line for line in lines(entry, "probe_server_output"))],
            "after_each_refusal": following,
            "started_after_the_last_end_ms": sorted(entry["started_after_the_last_end_ms"] for entry in cycles if "started_after_the_last_end_ms" in entry),
            "left_running_after_a_cycle": sorted({name for entry in cycles for name in entry.get("left_running") or []}),
            "not_halted_at_connect_in_cycles": [entry["cycle"] for entry in cycles if "at_connect" in entry and entry["at_connect"].get("s_halt") is not True],
        }
    return summary


def test_record_st_link_server_restarts(bench: Bench, firmware: Path, gdb: None, tmp_path: Path, record_property) -> None:
    """The seventh round: session starts through stlink-server with no pause before them, block after block.

    The blocks run in the order `RESTART_BLOCKS` lists them, the same number of
    cycles each; the one that keeps stlink-server running starts it once,
    before its first cycle, and ends it after its last. A start refused that
    many times in a row ends the block early, not the round."""
    root, recorder, private_values, recording = recording_for(bench, firmware, tmp_path)
    stlink_server = os.environ.get(STLINK_SERVER_ENV) or shutil.which("stlink-server")
    if not stlink_server or not Path(stlink_server).is_file():
        pytest.fail(f"no stlink-server on PATH and none named by {STLINK_SERVER_ENV}", pytrace=False)
    cycles = int(os.environ.get(RESTART_CYCLES_ENV) or DEFAULT_RESTART_CYCLES)
    recording["restarts"] = {
        "blocks": [{"block": block, **settings} for block, settings in RESTART_BLOCKS],
        "cycles_per_block": cycles,
        "stlink_server_sha256": hashlib.sha256(Path(stlink_server).read_bytes()).hexdigest(),
        "stlink_server_on_path": shutil.which("stlink-server") == stlink_server,
        "in_a_container": {"dockerenv": Path("/.dockerenv").exists(), "containerenv": Path("/run/.containerenv").exists()},
    }
    entries: list[dict] = []
    ended_at: float | None = None
    try:
        for block, settings in RESTART_BLOCKS:
            kept: GdbServer | None = None
            if settings["stlink_server"] == "kept_running":
                kept, listening_after_ms = recorder.probe_server_started(stlink_server)
                recording["restarts"][f"{block}_listening_after_ms"] = listening_after_ms
                if listening_after_ms is None:
                    recording["restarts"][f"{block}_not_listening"] = recorder.probe_server_ended(kept)
                    continue
            refusals_in_a_row = 0
            for cycle in range(1, cycles + 1):
                if settings["pause_before_start_s"]:
                    time.sleep(settings["pause_before_start_s"])
                entry, ended_at = recorder.restart_cycle(block, cycle, stlink_server, kept, ended_at, whole=cycle == 1)
                entries.append(entry)
                refusals_in_a_row = refusals_in_a_row + 1 if entry.get("ready_at_s") is None else 0
                if refusals_in_a_row >= RACE_REFUSALS_IN_A_ROW_LIMIT:
                    recording["restarts"][f"{block}_ended_early"] = f"{refusals_in_a_row} starts in a row were refused"
                    break
            if kept is not None:
                recording["restarts"][f"{block}_probe_server_end"] = recorder.probe_server_ended(kept)
                ended_at = time.monotonic()
    finally:
        recorder.cleanup()
        recording["scenarios"]["restarts"] = entries
        recording["summary"] = summarize_restarts(entries)
        write_recording(recording, root, private_values, record_property, RESTART_OUTPUT_NAME)
        restored = put_on_board(bench, firmware)
        assert restored.get("ok") is True, f"the demo could not be put back on the board: {restored.get('summary')}{chr(10)}{json.dumps(restored, indent=1, default=str)}"


def usb_handles(pid: int) -> int | None:
    """How many USB device nodes a process holds open, read from its /proc fd links; None where they cannot be read."""
    try:
        return len([fd for fd in Path(f"/proc/{pid}/fd").iterdir() if os.readlink(fd).startswith("/dev/bus/usb/")])
    except OSError:
        return None


def stlink_servers_running() -> list[int]:
    return sorted(process["pid"] for process in all_processes() if process["name"] == "stlink-server")


def end_stlink_servers_left_running() -> list[dict]:
    """End every stlink-server still running in this PID namespace, whoever started it, and say how each ended."""
    ended = []
    for pid in stlink_servers_running():
        with suppress(ProcessLookupError):
            os.kill(pid, signal.SIGTERM)
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and pid in stlink_servers_running():
            time.sleep(0.05)
        gone = pid not in stlink_servers_running()
        if not gone:
            with suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)
        ended.append({"ended_by_sigterm": gone})
    return ended


def tcp_lines(port: int) -> list[str]:
    """The lines of /proc/net/tcp and /proc/net/tcp6 with `port` at either end, as the kernel prints them."""
    wanted = f":{port:04X}"
    lines = []
    for table in ("/proc/net/tcp", "/proc/net/tcp6"):
        with suppress(OSError):
            for line in Path(table).read_text(encoding="ascii").splitlines()[1:]:
                fields = line.split()
                if len(fields) > 3 and (fields[1].endswith(wanted) or fields[2].endswith(wanted)):
                    lines.append(line.strip())
    return lines


def server_side_states(port: int) -> list[str]:
    """The state of every socket whose own end is `port`, the listening one included."""
    wanted = f":{port:04X}"
    fields = [line.split() for line in tcp_lines(port)]
    return sorted(TCP_STATES.get(entry[3], entry[3]) for entry in fields if entry[1].endswith(wanted))


def wait_before_the_server_end(variant: str, pid: int, killed: float) -> dict:
    """Wait as `variant` says; and when, in ms after the kill, stlink-server's side of the connection was closed and its USB node released.

    Closed is no socket of its own on its port left established or waiting for
    it to close; released is no /dev/bus/usb node among its descriptors. Both
    are looked for every 5 ms for as long as the variant waits."""
    waited: dict = {"client_closed_after_ms": None, "usb_released_after_ms": None}
    if variant == "at_once":
        return waited
    until = killed + (0.5 if variant == "after_half_a_second" else SERVER_END_WAIT_LIMIT_S)
    while True:
        now = time.monotonic()
        if waited["client_closed_after_ms"] is None and not {"ESTABLISHED", "CLOSE_WAIT"} & set(server_side_states(STLINK_SERVER_PORT)):
            waited["client_closed_after_ms"] = int((now - killed) * 1000)
        if waited["usb_released_after_ms"] is None and usb_handles(pid) == 0:
            waited["usb_released_after_ms"] = int((now - killed) * 1000)
        if variant == "after_its_client_closed" and waited["client_closed_after_ms"] is not None:
            break
        if variant == "after_the_usb_released" and waited["usb_released_after_ms"] is not None:
            break
        if now >= until:
            waited["waited_out"] = variant != "after_half_a_second"
            break
        time.sleep(0.005)
    return waited

def test_record_st_link_server_sharing(
    bench: Bench, firmware: Path, gdb: None, tmp_path: Path, record_property, stlink_bench: Bench, stlink_servers, mcp_servers
) -> None:
    """The eighth round: stlink-server left running with no client, and a second GDB server sharing it.

    The idle cycles run a session through an stlink-server started here, kill
    its GDB server, and then, with that stlink-server still running and no
    client on it, call the product's `probe_target` on the stlink
    configuration (STM32_Programmer_CLI) and on the tier's own (OpenOCD); then
    the same two once it has ended. The overlap cycles start the product's own
    attach session, which starts its stlink-server, attach a second GDB server
    with `-t` and GDB to the same probe, stop the product's session, and then
    have the second halt the core, run it, halt it again and end. Whatever
    stlink-server is left running at the end of a cycle is ended here."""
    root, recorder, private_values, recording = recording_for(bench, firmware, tmp_path)
    stlink_server = os.environ.get(STLINK_SERVER_ENV) or shutil.which("stlink-server")
    if not stlink_server or not Path(stlink_server).is_file():
        pytest.fail(f"no stlink-server on PATH and none named by {STLINK_SERVER_ENV}", pytrace=False)
    cycles = int(os.environ.get(SHARING_CYCLES_ENV) or DEFAULT_SHARING_CYCLES)
    recording["sharing"] = {"cycles": cycles, "settle_s": SETTLE_S, "run_before_interrupt_s": RUN_BEFORE_INTERRUPT_S}
    product = stlink_servers()
    image = debug_sessions.workspace_image(stlink_bench, firmware)
    idle: list[dict] = []
    overlap: list[dict] = []

    def openocd_probe_target() -> dict:
        openocd = mcp_servers()
        try:
            return product_answer(openocd.tool("probe_target"))
        finally:
            openocd.shut_down()

    def cli_probe_target() -> dict:
        answer = product.tool("probe_target")
        entry = product_answer(answer)
        if answer.get("ok") is not True:
            entry["log"] = product_log(bench, answer.get("log_path"))
        return entry

    try:
        for cycle in range(1, cycles + 1):
            whole = cycle == 1
            entry: dict = {"cycle": cycle}
            idle.append(entry)
            probe_server, entry["probe_server_listening_after_ms"] = recorder.probe_server_started(stlink_server)
            port = free_port()
            server = GdbServer(recorder.server_argv(port, extra=["-g", "-t"]), recorder.environment, recorder.cwd)
            recorder.live.append(server)
            ready = recorder.wait_until_listening(server, port, STARTUP_TIMEOUT_S)
            entry["ready_at_s"] = ready["at_s"] if ready is not None else None
            if ready is None:
                entry["refused"] = recorder.finish(server)
            else:
                client, _ = recorder.connect(port)
                entry["at_connect"] = {key: value for key, value in recorder.core_state(client).items() if key in ("s_halt", COUNTER, "pc")}
                entry["usb_handles_with_a_client"] = usb_handles(probe_server.process.pid)
                entry["end"] = recorder.kill(server)
                entry["gdb_exit"] = {key: value for key, value in recorder.close_client(client).items() if key in ("closed", "error")}
            time.sleep(SETTLE_S)
            entry["usb_handles_while_idle"] = usb_handles(probe_server.process.pid)
            entry["probe_server_running_while_idle"] = probe_server.process.poll() is None
            entry["cli_probe_target_while_idle"] = cli_probe_target()
            entry["openocd_probe_target_while_idle"] = openocd_probe_target()
            entry["usb_handles_after_the_other_openers"] = usb_handles(probe_server.process.pid)
            entry["probe_server_end"] = recorder.probe_server_ended(probe_server)
            entry["probe_server_output"] = probe_server.output()
            time.sleep(SETTLE_S)
            entry["cli_probe_target_after_its_end"] = cli_probe_target()
            entry["openocd_probe_target_after_its_end"] = openocd_probe_target()
            if whole:
                entry["gdb_server_output"] = server.output()
        for cycle in range(1, cycles + 1):
            whole = cycle == 1
            entry = {"cycle": cycle}
            overlap.append(entry)
            started = product.tool("debug_start_session", {"image_path": image, "mode": "attach"})
            entry["first_started"] = {**product_answer(started), "probe_server": (started.get("session") or {}).get("probe_server")}
            if started.get("ok") is not True:
                entry["first_refused"] = refused_session_start(bench, started)
                continue
            port = free_port()
            second = GdbServer(recorder.server_argv(port, extra=["-g", "-t"]), recorder.environment, recorder.cwd)
            recorder.live.append(second)
            ready = recorder.wait_until_listening(second, port, STARTUP_TIMEOUT_S)
            entry["second_ready_at_s"] = ready["at_s"] if ready is not None else None
            client = None
            if ready is None:
                entry["second_refused"] = recorder.finish(second)
            else:
                client, _ = recorder.connect(port)
                entry["second_at_connect"] = {key: value for key, value in recorder.core_state(client).items() if key in ("s_halt", COUNTER, "pc", "dhcsr_contents")}
            stopped = product.tool("debug_stop_session")
            entry["first_stopped"] = {**product_answer(stopped), "probe_server": (stopped.get("session") or {}).get("probe_server")}
            entry["stlink_servers_running_after_the_first_stop"] = len(stlink_servers_running())
            if client is not None:
                time.sleep(SETTLE_S)
                entry["second_interrupt"] = recorder.command(client, "-exec-interrupt --all")
                entry["second_interrupt_stop"] = recorder.stop(client, 2.0)
                entry["second_halted"] = {key: value for key, value in recorder.core_state(client).items() if key in ("s_halt", COUNTER, "pc", "dhcsr_contents")}
                entry["second_continue"] = recorder.command(client, "-exec-continue")
                time.sleep(RUN_BEFORE_INTERRUPT_S)
                entry["second_interrupt_after_the_run"] = recorder.command(client, "-exec-interrupt --all")
                entry["second_stop_after_the_run"] = recorder.stop(client)
                entry["second_after_the_run"] = {key: value for key, value in recorder.core_state(client).items() if key in ("s_halt", COUNTER, "pc", "dhcsr_contents")}
                entry["second_end"] = recorder.kill(second)
                entry["second_gdb_exit"] = {key: value for key, value in recorder.close_client(client).items() if key in ("closed", "error")}
            output = second.output()
            entry["second_server_output"] = output if whole else [line for line in output if line["stream"] == "stderr" or "rror" in line["line"]]
            entry["stlink_servers_ended_here"] = end_stlink_servers_left_running()
            time.sleep(SETTLE_S)
    finally:
        recorder.cleanup()
        left = end_stlink_servers_left_running()
        recording["scenarios"]["idle_server_and_other_openers"] = idle
        recording["scenarios"]["overlapping_sessions"] = overlap
        recording["stlink_servers_ended_at_the_end"] = left
        write_recording(recording, root, private_values, record_property, SHARING_OUTPUT_NAME)
        restored = put_on_board(bench, firmware)
        assert restored.get("ok") is True, f"the demo could not be put back on the board: {restored.get('summary')}{chr(10)}{json.dumps(restored, indent=1, default=str)}"


def summarize_server_ends(entries: list[dict], openocd: list[dict]) -> dict:
    """Per variant: its ends, what was seen at them, and the starts and OpenOCD calls made right after them."""

    def lines(entry: dict, key: str) -> list[str]:
        return [line["line"] for line in entry.get(key) or [] if isinstance(line, dict)]

    summary: dict = {}
    for variant in SERVER_END_VARIANTS:
        ended = [entry for entry in entries if entry["variant"] == variant and "killed_at_s" in entry]
        met = [entry for entry in entries if entry.get("previous_end") == variant]
        refused = [entry for entry in met if entry.get("ready_at_s") is None]

        def waited(key: str, ended: list[dict] = ended) -> list[int]:
            return sorted(entry["waited"][key] for entry in ended if entry["waited"].get(key) is not None)

        summary[variant] = {
            "ends": len(ended),
            "next_starts": len(met),
            "next_starts_refused_in_cycles": [entry["cycle"] for entry in refused],
            "refusal_reasons": sorted({line for entry in refused for line in lines(entry.get("refused") or {}, "output") if line.startswith("Reason:")}),
            "probe_server_lines_at_refusals": sorted({line for entry in refused for line in lines(entry, "probe_server_output") if line.startswith("Error:") and "recv returned 0" not in line}),
            "client_closed_after_ms": waited("client_closed_after_ms"),
            "usb_released_after_ms": waited("usb_released_after_ms"),
            "waited_out": len([entry for entry in ended if entry["waited"].get("waited_out")]),
            "usb_handles_with_a_client": sorted({entry["with_a_client"]["usb_handles"] for entry in ended}, key=str),
            "usb_handles_at_the_kill": sorted({entry["at_the_kill"]["usb_handles"] for entry in ended}, key=str),
            "connections_at_the_kill": sorted({" ".join(entry["at_the_kill"]["connections"]) for entry in ended}),
            "connections_before_the_end": sorted({" ".join(entry["before_the_end"]["connections"]) for entry in ended if "before_the_end" in entry}),
            "left_running_after_a_cycle": sorted({name for entry in ended for name in entry.get("left_running") or []}),
        }
    for variant in SERVER_END_OPENOCD_VARIANTS:
        calls = [entry for entry in openocd if entry["variant"] == variant]
        answers = [entry.get("openocd_probe_target_next") or {} for entry in calls]
        summary[f"openocd_after_{variant}"] = {
            "calls": len([entry for entry in calls if "openocd_probe_target_next" in entry]),
            "refused_in_cycles": [entry["cycle"] for entry, answer in zip(calls, answers, strict=True) if answer.get("ok") is not True],
            "refusals": sorted({json.dumps({key: answer.get(key) for key in ("error_type", "summary", "backend_error")}, sort_keys=True) for answer in answers if answer.get("ok") is not True}),
            "session_starts_refused_in_cycles": [entry["cycle"] for entry in calls if "ready_at_s" in entry and entry["ready_at_s"] is None],
        }
    return summary


@pytest.fixture
def demo_built(bench: Bench) -> Path:
    """The demo's ELF built here, with nothing put on the board.

    The ninth round's own cycles are the first to open the probe in its run,
    so a probe the run before left refusing OpenOCD is met by them first; the
    round puts the demo on the board at its end."""
    failure = built_where_it_stands(bench.project)
    if failure is not None:
        pytest.fail(f"the demo firmware did not build here: {failure}", pytrace=False)
    image = bench.project / DEMO_IMAGE
    assert image.is_file(), f"the build left no ELF at {image}"
    return image


def test_record_st_link_server_ends(bench: Bench, demo_built: Path, gdb: None, tmp_path: Path, record_property, mcp_servers) -> None:
    """The ninth round: each way of ending a session's stlink-server, and what the next start, or OpenOCD, meets after it.

    The variants take turns cycle by cycle, `SERVER_END_VARIANTS` times the
    cycle count, every start made at once; then `SERVER_END_OPENOCD_CYCLES`
    cycles end theirs one of `SERVER_END_OPENOCD_VARIANTS` ways in turn, each
    followed at once by the product's probe_target on the tier's own
    configuration."""
    firmware = demo_built
    root, recorder, private_values, recording = recording_for(bench, firmware, tmp_path)
    stlink_server = os.environ.get(STLINK_SERVER_ENV) or shutil.which("stlink-server")
    if not stlink_server or not Path(stlink_server).is_file():
        pytest.fail(f"no stlink-server on PATH and none named by {STLINK_SERVER_ENV}", pytrace=False)
    cycles = int(os.environ.get(SERVER_END_CYCLES_ENV) or DEFAULT_SERVER_END_CYCLES)
    recording["server_ends"] = {
        "variants": list(SERVER_END_VARIANTS),
        "cycles_per_variant": cycles,
        "wait_limit_s": SERVER_END_WAIT_LIMIT_S,
        "openocd_variants": list(SERVER_END_OPENOCD_VARIANTS),
        "openocd_cycles": SERVER_END_OPENOCD_CYCLES,
        "stlink_server_sha256": hashlib.sha256(Path(stlink_server).read_bytes()).hexdigest(),
        "stlink_server_on_path": shutil.which("stlink-server") == stlink_server,
        "in_a_container": {"dockerenv": Path("/.dockerenv").exists(), "containerenv": Path("/run/.containerenv").exists()},
    }
    entries: list[dict] = []
    openocd: list[dict] = []

    def openocd_probe_target() -> dict:
        server = mcp_servers()
        try:
            return product_answer(server.tool("probe_target"))
        finally:
            server.shut_down()

    try:
        previous: str | None = None
        refusals_in_a_row = 0
        for index in range(cycles * len(SERVER_END_VARIANTS)):
            variant = SERVER_END_VARIANTS[index % len(SERVER_END_VARIANTS)]
            entry = recorder.server_end_cycle(variant, index + 1, stlink_server, previous, whole=index < len(SERVER_END_VARIANTS))
            entries.append(entry)
            previous = variant
            refusals_in_a_row = refusals_in_a_row + 1 if entry.get("ready_at_s") is None else 0
            if refusals_in_a_row >= RACE_REFUSALS_IN_A_ROW_LIMIT:
                recording["server_ends"]["ended_early"] = f"{refusals_in_a_row} starts in a row were refused"
                break
        for index in range(SERVER_END_OPENOCD_CYCLES if refusals_in_a_row < RACE_REFUSALS_IN_A_ROW_LIMIT else 0):
            variant = SERVER_END_OPENOCD_VARIANTS[index % len(SERVER_END_OPENOCD_VARIANTS)]
            entry = recorder.server_end_cycle(variant, index + 1, stlink_server, None, whole=False)
            entry["openocd_probe_target_next"] = openocd_probe_target()
            openocd.append(entry)
    finally:
        recorder.cleanup()
        left = end_stlink_servers_left_running()
        recording["scenarios"]["server_ends"] = entries
        recording["scenarios"]["openocd_after_the_end"] = openocd
        recording["stlink_servers_ended_at_the_end"] = left
        recording["summary"] = summarize_server_ends(entries, openocd)
        write_recording(recording, root, private_values, record_property, SERVER_ENDS_OUTPUT_NAME)
        restored = put_on_board(bench, firmware)
        assert restored.get("ok") is True, f"the demo could not be put back on the board: {restored.get('summary')}{chr(10)}{json.dumps(restored, indent=1, default=str)}"

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
        assert restored.get("ok") is True, f"the demo could not be put back on the board: {restored.get('summary')}{chr(10)}{json.dumps(restored, indent=1, default=str)}"
