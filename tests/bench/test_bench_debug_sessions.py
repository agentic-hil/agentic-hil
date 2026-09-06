"""The debug session lifecycle, driven over the MCP stdio server against the board.

A debug session is the one surface here that has no command line at all: there
is no `agentic-hil debug-start`, and there is not meant to be, because a session
is a thing an agent holds open across several calls and a shell invocation holds
nothing. So every test in this file speaks the protocol the agent speaks, to
`agentic-hil mcp-stdio`, started as a child of this test in this checkout,
against the configuration the bench fixture wrote. Nothing here opens a probe, a
serial device or a GDB of its own: what is being measured is what the product
answers, and a test that reached the board around it would be measuring
something else.

What a fake cannot establish, and why these live on a bench:

* whether an SWD attach really leaves the core halted, and whether the three
  session modes differ on the board the way the schema says they differ. A
  double answers `halted` because it was written to; a Cortex-M4 answers it
  because the probe stopped it.
* whether the session's own evidence file exists at the path the result names.
  The path is built from the configured log directory and reported through
  `display_path`, and only a run with a real configuration and a real write
  behind it can say that the two agree.
* whether a refusal really costs nothing. A start refused on its artifact, and a
  resume refused on its permission, both take and give back a hardware lease on
  a machine-wide lock. That the bench is free afterwards is a claim about locks
  and records on this host, not about a return value.
* whether a server that ends with a session open hands the board back. The
  session, the GDB client, the debug server child and the lease all have to come
  apart in order, and none of it is visible from inside one process.

Every session this file opens is closed by the fixture that opened it, on the
failing path too, and the fixture then asks the product whether the bench came
back free. If it did not, the quarantine is cleared here through
`agentic-hil recover --confirm-safe-state`, on this session's own configuration,
and the test is failed for having needed it, so a bench that has to be recovered
is never a bench that was quietly recovered.

Nothing here names a bench. The probe, the port, the session id, the GDB port
and the log path are read out of the product's own answers and asserted on their
shape; no value that identifies this machine is written down, because these
files are public and the machine is not.
"""

from __future__ import annotations

import contextlib
import json
import queue
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from .conftest import BENCH_ONLY, COMMAND_TIMEOUT_S, Bench, child_command

pytestmark = [pytest.mark.bench, BENCH_ONLY]

# The three modes `debug_start_session` publishes. Written out here rather than
# imported, so that a mode added to the product and not covered by this file
# fails the first test below instead of quietly running nowhere.
DEBUG_MODES = ["attach", "reset_halt", "load"]

# The MCP protocol version this client offers. Nothing here asserts which
# version came back: the server negotiates, and what this file measures is the
# debug session behind the tools, not the handshake.
CLIENT_PROTOCOL_VERSION = "2025-06-18"

# The operator's line, and the only line that opens a permission. Named here
# because the refusal under test has to carry it, and a test that asked the
# product what its own advice says would assert nothing.
GRANT_COMMAND = "agentic-hil grant"

# Long enough for a GDB attach, a reset into halt and a full image download over
# SWD; short enough that a wedged session fails this file rather than the night.
TOOL_TIMEOUT_S = COMMAND_TIMEOUT_S
# A server that has been told to end has nothing left to do but end.
SHUTDOWN_TIMEOUT_S = 120.0

# What the reader thread puts on the queue when the server's stdout reaches end
# of file, so a request waiting for an answer that will never come fails at once
# with the server's own last words instead of at the timeout.
_STREAM_CLOSED = object()


class McpServer:
    """One `agentic-hil mcp-stdio` child, driven as a JSON-RPC client.

    Deliberately small. It writes one message per line, reads the answers off a
    background thread so a dead server is an immediate failure rather than a
    timeout, and matches responses by id. Everything else a test needs, it
    asserts for itself out of the documents this returns.
    """

    def __init__(self, bench: Bench) -> None:
        self.bench = bench
        self.process = subprocess.Popen(
            child_command("mcp-stdio"),
            cwd=str(bench.project),
            env=bench.environment(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        self._incoming: queue.Queue[Any] = queue.Queue()
        self._errors: list[str] = []
        self._next_id = 0
        self._readers = [
            threading.Thread(target=self._read_stdout, daemon=True),
            threading.Thread(target=self._read_stderr, daemon=True),
        ]
        for reader in self._readers:
            reader.start()
        try:
            self.server_info = self._handshake()
        except BaseException:
            # The child is already running by here, and a caller that never
            # received this object cannot hand it to the fixture that closes
            # servers. A handshake that fails takes its own process with it, or
            # the next test meets a probe this one is still holding.
            self.shut_down(stop_session=False)
            raise

    def _read_stdout(self) -> None:
        stream = self.process.stdout
        if stream is not None:
            for line in stream:
                if line.strip():
                    self._incoming.put(line)
        self._incoming.put(_STREAM_CLOSED)

    def _read_stderr(self) -> None:
        stream = self.process.stderr
        if stream is not None:
            for line in stream:
                self._errors.append(line)

    def diagnosis(self) -> str:
        """What the server said on the way down, for a failure message."""
        tail = "".join(self._errors[-40:])
        return f"exit={self.process.poll()}\n{tail}"

    def _handshake(self) -> dict:
        answered = self.request(
            "initialize",
            {
                "protocolVersion": CLIENT_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "agentic-hil-bench", "version": "0"},
            },
        )
        self.notify("notifications/initialized")
        return answered["result"]

    def _write(self, message: dict) -> None:
        stream = self.process.stdin
        assert stream is not None, self.diagnosis()
        stream.write(json.dumps(message) + "\n")
        stream.flush()

    def notify(self, method: str, params: dict | None = None) -> None:
        message: dict = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        self._write(message)

    def request(self, method: str, params: dict | None = None, timeout_s: float = TOOL_TIMEOUT_S) -> dict:
        self._next_id += 1
        request_id = self._next_id
        message: dict = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            message["params"] = params
        self._write(message)
        deadline = time.monotonic() + timeout_s
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError(f"the MCP server did not answer {method} within {timeout_s}s\n{self.diagnosis()}")
            try:
                line = self._incoming.get(timeout=remaining)
            except queue.Empty:
                raise AssertionError(f"the MCP server did not answer {method} within {timeout_s}s\n{self.diagnosis()}") from None
            if line is _STREAM_CLOSED:
                raise AssertionError(f"the MCP server ended before answering {method}\n{self.diagnosis()}")
            answered = json.loads(line)
            if answered.get("id") == request_id:
                assert "error" not in answered, (method, answered["error"])
                return answered

    def envelope(self, tool: str, arguments: dict | None = None, timeout_s: float = TOOL_TIMEOUT_S) -> dict:
        """One `tools/call` result, whole: content, structuredContent and isError."""
        answered = self.request("tools/call", {"name": tool, "arguments": arguments or {}}, timeout_s=timeout_s)
        return answered["result"]

    def tool(self, tool: str, arguments: dict | None = None, timeout_s: float = TOOL_TIMEOUT_S) -> dict:
        """One tool's machine document, which is what every assertion here reads."""
        return self.envelope(tool, arguments, timeout_s=timeout_s)["structuredContent"]

    def shut_down(self, *, stop_session: bool = True, timeout_s: float = SHUTDOWN_TIMEOUT_S) -> int:
        """End this server, closing whatever it still holds, and report its status.

        Idempotent, and safe on a server that has already gone: a test that ends
        its own server calls this again through the fixture's teardown.

        ``stop_session`` is the containment, not a convenience. It is asked for
        first so a session an assertion abandoned is torn down through the tool
        that tears sessions down; only then is stdin closed, which is what ends
        the read loop and runs the server's own close. A server that will not
        end on its own is killed, and on a host where the debug server survives
        that, the next test's start says so rather than this one guessing.
        """
        if self.process.poll() is None and stop_session:
            # Containment must not raise over its own best effort: the process
            # teardown below is what actually frees the bench, and the fixture
            # checks afterwards whether it did.
            with contextlib.suppress(AssertionError, KeyError, OSError, ValueError):
                self.tool("debug_stop_session", {}, timeout_s=SHUTDOWN_TIMEOUT_S)
        if self.process.stdin is not None and not self.process.stdin.closed:
            with contextlib.suppress(OSError):
                self.process.stdin.close()
        try:
            return self.process.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            self.process.kill()
            return self.process.wait(timeout=timeout_s)


def workspace_image(bench: Bench, firmware: Path) -> str:
    """The built demo ELF, named the way a caller inside the workspace names it."""
    return firmware.relative_to(bench.project).as_posix()


def blocking_incident(bench: Bench) -> dict | None:
    """This bench's own answer about whether it is blocked, or None where it is free.

    Read through `agentic-hil lease-status`, which is the product's own answer
    about its own locks and records; nothing here inspects a state file.

    The exit status is deliberately not asserted. A blocked bench is reported
    with `cleanup_required`, and that is exactly what makes the command exit
    non-zero, so a check written against exit 0 would raise on the one case this
    exists to catch and would never reach the recovery below.
    """
    _, report = bench.document("lease-status")
    return report if report.get("blocked") else None


@pytest.fixture
def mcp_servers(bench: Bench):
    """Start MCP servers for one test, and give the bench back afterwards.

    Every server this hands out is shut down here, in reverse order, on the
    passing and the failing path alike, and each one is waited for before the
    next test may take the probe: one probe, one board, one process at a time.

    A shutdown that raises is reported rather than allowed to end the loop: the
    server after it in the list is the one still holding the probe, and a
    teardown that stopped at the first failure would hand the next test a bench
    somebody else's process is on.

    Then the bench is asked whether it came back free. A test in this file that
    leaves a quarantine behind has gone wrong, so the quarantine is cleared here
    through `agentic-hil recover --confirm-safe-state` on this session's own
    configuration (no configuration the operator owns is reachable from this
    tier) and the test is failed for having needed it. Clearing without failing
    would hand the next test a clean bench and hide the reason it was not.
    """
    started: list[McpServer] = []

    def start() -> McpServer:
        server = McpServer(bench)
        started.append(server)
        return server

    try:
        yield start
    finally:
        left_behind: list[str] = []
        for server in reversed(started):
            try:
                server.shut_down()
            except Exception as error:
                left_behind.append(f"a server did not shut down: {type(error).__name__}: {error}\n{server.diagnosis()}")
        incident = blocking_incident(bench)
        if incident is not None:
            # `--quarantine-id` is mandatory on this command, so the id the
            # bench just reported is passed even when it reported none: an
            # answer about the empty one is the product's, and an argparse
            # usage error would be this file's.
            recovered = bench.run("recover", "--confirm-safe-state", "--quarantine-id", str(incident.get("quarantine_id") or ""))
            left_behind.append(
                "this test left the bench blocked; recovery was run here so the next test starts clean.\n"
                f"cleanup_reasons: {incident.get('cleanup_reasons')}\n"
                f"incident_stands: {incident.get('incident_stands')}\n"
                f"recover exited {recovered.returncode}: {recovered.stdout}{recovered.stderr}"
            )
        if left_behind:
            pytest.fail("\n".join(left_behind), pytrace=False)


def test_the_schema_offers_exactly_the_modes_this_file_drives_on_the_board(mcp_servers) -> None:
    """A session mode nobody drives on hardware is a mode nobody has tested.

    The three below are each started against the board further down. If a fourth
    is ever published, this fails, and the file that has to grow is named by the
    failure rather than discovered a release later on somebody's bench.
    """
    server = mcp_servers()

    listed = server.request("tools/list")["result"]["tools"]

    start = next(tool for tool in listed if tool["name"] == "debug_start_session")
    assert start["inputSchema"]["properties"]["mode"]["enum"] == DEBUG_MODES, start["inputSchema"]
    assert start["inputSchema"]["properties"]["mode"]["default"] == "attach", start["inputSchema"]


def test_an_attach_session_halts_the_core_and_writes_no_flash(mcp_servers, bench: Bench, firmware: Path) -> None:
    """Attach is the mode that must not touch the board's flash.

    It connects to a running target and the SWD attach stops the core, and that
    is all it may do. The claim under test is the pair: the session reports the
    core halted, and its load phase and firmware load status say the image was
    never downloaded. A mode that quietly reset or reloaded the target to reach
    a halt would pass on the first half and fail here on the second.
    """
    server = mcp_servers()

    started = server.tool("debug_start_session", {"image_path": workspace_image(bench, firmware), "mode": "attach"})

    assert started["ok"] is True, started
    assert started["mode"] == "attach", started
    assert started["summary"] == "Debug session started and target is halted.", started["summary"]
    session = started["session"]
    assert session["status"] == "halted", session
    assert session["mode"] == "attach", session
    assert session["load_phase"] == "target_connected", session
    assert session["firmware_load_status"] == "not_started", session
    assert isinstance(session["session_id"], str) and session["session_id"].strip(), session
    assert isinstance(session["gdb_port"], int) and session["gdb_port"] > 0, session
    assert session["breakpoints"] == [], session
    assert started["quarantined"] is False, started

    stopped = server.tool("debug_stop_session")
    assert stopped["ok"] is True, stopped


def test_a_reset_halt_session_resets_the_target_before_it_halts_and_still_writes_no_flash(
    mcp_servers, bench: Bench, firmware: Path
) -> None:
    """reset_halt has to be a different thing from attach on the real board.

    Its whole point is a target taken into a defined state from reset rather
    than caught wherever it happened to be, and the load phase is where the
    backend says it did that. A reset_halt that degraded to a plain attach, on
    a board whose OpenOCD would not drive `reset halt`, would still report a
    halted core; it would not report the confirmed pre-load reset asserted here.
    The permission it costs, allow_reset, is the one attach does not need.
    """
    server = mcp_servers()

    started = server.tool("debug_start_session", {"image_path": workspace_image(bench, firmware), "mode": "reset_halt"})

    assert started["ok"] is True, started
    assert started["mode"] == "reset_halt", started
    session = started["session"]
    assert session["status"] == "halted", session
    assert session["mode"] == "reset_halt", session
    assert session["load_phase"] == "pre_load_reset_confirmed", session
    assert session["firmware_load_status"] == "not_started", session

    stopped = server.tool("debug_stop_session")
    assert stopped["ok"] is True, stopped


def test_a_load_session_downloads_the_built_image_and_says_the_load_committed(
    mcp_servers, bench: Bench, firmware: Path
) -> None:
    """The one mode that writes the board, and the field that says whether it did.

    The image is the demo's own ELF, built for this run by the `firmware`
    fixture, so the board ends holding what it started with; nothing here erases
    anything and this mode is refused outright while allow_mass_erase is true.

    `firmware_load_status` is the claim. A load mode that connected, reset and
    reported success without ever running the download would look identical in
    every other field and would leave that one at `not_started`, which is
    exactly the failure an agent would then blame on the firmware it thought it
    had just written. If the download fails half way the bench is quarantined,
    and the fixture clears that incident through `agentic-hil recover` and fails
    the test for it.
    """
    server = mcp_servers()

    started = server.tool("debug_start_session", {"image_path": workspace_image(bench, firmware), "mode": "load"})

    assert started["ok"] is True, started
    assert started["mode"] == "load", started
    session = started["session"]
    assert session["status"] == "halted", session
    assert session["mode"] == "load", session
    assert session["firmware_load_status"] == "committed", session
    assert session["load_phase"] == "post_load_reset_confirmed", session

    stopped = server.tool("debug_stop_session")
    assert stopped["ok"] is True, stopped


def test_a_second_start_while_a_session_is_open_is_refused_and_leaves_the_first_running(
    mcp_servers, bench: Bench, firmware: Path
) -> None:
    """One probe, one board, one session, and the refusal must not cost the first one.

    The dangerous failure is not the refusal, it is a second start that tears
    the live session down on its way to reporting that a session was live. So
    the refusal is asserted to name the session that is already open, and the
    session is then read again and asserted to be the same one, still halted,
    and to stop cleanly afterwards.
    """
    server = mcp_servers()
    image = workspace_image(bench, firmware)
    first = server.tool("debug_start_session", {"image_path": image, "mode": "attach"})
    assert first["ok"] is True, first
    session_id = first["session"]["session_id"]

    refused = server.envelope("debug_start_session", {"image_path": image, "mode": "attach"})
    document = refused["structuredContent"]

    assert refused["isError"] is True, refused
    assert document["ok"] is False, document
    assert document["error_type"] == "session_already_active", document
    assert document["session"]["session_id"] == session_id, document["session"]
    assert document["quarantined"] is False, document

    still_open = server.tool("debug_get_session_status")
    assert still_open["active"] is True, still_open
    assert still_open["session"]["session_id"] == session_id, still_open
    assert still_open["status"] == "halted", still_open

    stopped = server.tool("debug_stop_session")
    assert stopped["ok"] is True, stopped
    assert stopped["session"]["session_id"] == session_id, stopped


def test_the_status_and_the_stop_reason_of_a_server_that_never_started_a_session(mcp_servers, bench: Bench) -> None:
    """Two reads on a fresh server, and neither may touch the bench to answer.

    The status is a success that reports nothing active; the stop reason is a
    refusal that names the tool to call first. The failure this catches is a
    status read that answers out of a stale session object, or one that takes a
    hardware lease in order to say there is nothing to report: this server has
    started no session, so `lease-status` must still see a free bench while it
    is running.
    """
    server = mcp_servers()

    status = server.envelope("debug_get_session_status")
    document = status["structuredContent"]

    assert status["isError"] is False, status
    assert document["ok"] is True, document
    assert document["active"] is False, document
    assert document["status"] == "stopped", document
    assert document["session"] is None, document

    reason = server.tool("debug_get_stop_reason")
    assert reason["ok"] is False, reason
    assert reason["error_type"] == "session_not_active", reason

    # One read, because both halves of "the bench was never touched" are in the
    # same document and two reads would be two different moments.
    free = bench.document("lease-status")[1]
    assert free["blocked"] is False, free
    assert free["bench_held"] is False, free
    assert free["owner_active"] is False, free


def test_stopping_a_session_twice_is_a_success_both_times_and_the_second_holds_nothing(
    mcp_servers, bench: Bench, firmware: Path
) -> None:
    """Containment has to be free to repeat, and the repeat must claim nothing.

    An agent whose call was interrupted, and the teardown of every fixture in
    this file, both call stop on a session that may already be gone. The first
    stop states the two proofs a session end is made of: the target was
    confirmed halted and the backend was told not to resume it when the
    connection went away. The second must be an ordinary success carrying no
    session at all. A second stop reported as `session_not_active` would make
    containment unsafe to retry; one that carried the first session's proofs
    again would be claiming a halt nobody re-established.
    """
    server = mcp_servers()
    started = server.tool("debug_start_session", {"image_path": workspace_image(bench, firmware), "mode": "attach"})
    assert started["ok"] is True, started

    first = server.tool("debug_stop_session")
    second_envelope = server.envelope("debug_stop_session")
    second = second_envelope["structuredContent"]

    assert first["ok"] is True, first
    assert first["active"] is False, first
    assert first["status"] == "stopped", first
    assert first["safe_state_confirmed"] is True, first
    assert first["halt_not_confirmed"] is False, first
    assert first["detach_resume_guard_confirmed"] is True, first
    assert first["session"]["status"] == "stopped", first["session"]
    assert first["summary"] == "Debug session stopped with the target confirmed halted.", first["summary"]

    assert second_envelope["isError"] is False, second_envelope
    assert second["ok"] is True, second
    assert second["active"] is False, second
    assert second["status"] == "stopped", second
    assert second["summary"] == "No debug session is active.", second["summary"]
    assert second.get("session") is None, second


def test_a_start_from_an_image_path_that_does_not_exist_is_refused_and_gives_the_bench_straight_back(
    mcp_servers, bench: Bench
) -> None:
    """A missing artifact is a refusal, never an incident.

    Two halves, and the second is the one that used to bite. The refusal has to
    be `artifact_not_found` on `debug_start_session` and not a generic
    validation failure, so a caller knows to build rather than to re-validate.
    And the lease the start took before it looked at the file has to come back
    released: a refusal that quarantined the bench over a firmware file that was
    never opened would put a working board behind an operator's signature.
    """
    server = mcp_servers()

    refused = server.tool("debug_start_session", {"image_path": "build/Debug/no-such-image.elf", "mode": "attach"})

    assert refused["ok"] is False, refused
    assert refused["tool"] == "debug_start_session", refused
    assert refused["error_type"] == "artifact_not_found", refused
    assert refused["validation"]["exists"] is False, refused["validation"]
    assert refused["lease_state"] == "released", refused
    assert refused["quarantined"] is False, refused
    assert refused["cleanup_required"] is False, refused
    assert blocking_incident(bench) is None


def test_a_start_from_an_image_outside_the_workspace_is_refused_on_containment(
    mcp_servers, bench: Bench, firmware: Path, tmp_path: Path
) -> None:
    """The same ELF, from outside the project, is not the same artifact.

    The image is a byte-for-byte copy of the one the test above starts a session
    from, placed outside `workspace_root`. Only its location differs, so a
    refusal here is a refusal about containment and nothing else, and a start
    that accepted it would let a session be opened on any file on the host that
    happened to end in `.elf`. Both containment findings are asserted, because
    the two roots are configured separately and a bench that widened one of them
    must still be refused by the other.
    """
    server = mcp_servers()
    outside = tmp_path / "outside-the-workspace.elf"
    outside.write_bytes(firmware.read_bytes())

    refused = server.tool("debug_start_session", {"image_path": str(outside), "mode": "attach"})

    assert refused["ok"] is False, refused
    assert refused["tool"] == "debug_start_session", refused
    assert refused["error_type"] == "artifact_validation_failed", refused
    assert refused["validation"]["within_workspace"] is False, refused["validation"]
    assert refused["validation"]["allowed_root"] is False, refused["validation"]
    assert refused["quarantined"] is False, refused
    assert blocking_incident(bench) is None


def test_a_start_from_a_flashable_artifact_that_is_not_an_elf_is_refused_for_wanting_symbols(
    mcp_servers, bench: Bench, firmware: Path
) -> None:
    """A `.bin` is a perfectly good flash artifact and a useless debug artifact.

    It passes every artifact check the flash path applies, which is why this
    refusal cannot be left to those checks: a session has to read symbols out of
    the image, and a raw binary carries none. The refusal has to say that in the
    sentence a caller relays, or the agent retries the same file forever because
    nothing it was told is false of it.
    """
    server = mcp_servers()
    binary = bench.project / "debug-session-raw-image.bin"
    binary.write_bytes(firmware.read_bytes())

    refused = server.tool("debug_start_session", {"image_path": binary.name, "mode": "attach"})

    assert refused["ok"] is False, refused
    assert refused["tool"] == "debug_start_session", refused
    assert refused["error_type"] == "artifact_validation_failed", refused
    assert refused["summary"] == "Debug sessions require an ELF artifact with debug symbols.", refused["summary"]
    assert refused["quarantined"] is False, refused
    assert blocking_incident(bench) is None


def test_a_start_from_a_file_named_elf_that_is_not_one_is_refused_on_its_contents(
    mcp_servers, bench: Bench
) -> None:
    """The extension is a claim about the file, and the file is asked to back it.

    The other half of the pair above. This file is named the way a debug image
    is named and is not one, so nothing but reading its first bytes can refuse
    it. A validation that trusted the suffix would hand GDB a text file and turn
    a typo into a debug server that starts and then fails to load symbols with
    the board already attached.
    """
    server = mcp_servers()
    pretender = bench.project / "debug-session-not-an-image.elf"
    pretender.write_bytes(b"this file is named like a debug image and is not one\n")

    refused = server.tool("debug_start_session", {"image_path": pretender.name, "mode": "attach"})

    assert refused["ok"] is False, refused
    assert refused["tool"] == "debug_start_session", refused
    assert refused["error_type"] == "artifact_validation_failed", refused
    assert refused["validation"]["elf_header"] is False, refused["validation"]
    assert refused["quarantined"] is False, refused
    assert blocking_incident(bench) is None


def test_the_session_log_is_reported_inside_the_workspace_and_the_file_is_there(
    mcp_servers, bench: Bench, firmware: Path
) -> None:
    """The evidence a reviewer with no access to this bench opens afterwards.

    Three things at once, and each has failed on its own elsewhere: the path is
    reported relative to the workspace, so no host layout is published in a
    result an agent may relay; the file is actually at that path, so a report
    naming a log is not a report naming nothing; and the log is about this
    session, which is what the session id and the recorded GDB commands say. A
    log path built from one directory and written to another looks correct in
    every result and resolves to nothing on disk.
    """
    server = mcp_servers()
    started = server.tool("debug_start_session", {"image_path": workspace_image(bench, firmware), "mode": "attach"})
    assert started["ok"] is True, started

    log_path = started["log_path"]
    assert not Path(log_path).is_absolute(), log_path
    recorded = bench.project / log_path
    assert recorded.is_file(), f"the result named a session log that is not there: {log_path}"
    payload = json.loads(recorded.read_text(encoding="utf-8"))

    assert payload["session_id"] == started["session"]["session_id"], payload["session_id"]
    assert payload["mode"] == "attach", payload["mode"]
    assert payload["gdb_commands"], "the session log recorded no GDB command, so it is evidence of nothing"

    stopped = server.tool("debug_stop_session")
    assert stopped["ok"] is True, stopped
    assert stopped["log_path"] == log_path, stopped["log_path"]


def test_the_stop_reason_read_agrees_with_what_the_session_start_reported(
    mcp_servers, bench: Bench, firmware: Path
) -> None:
    """Two surfaces, one fact, before anything has been resumed or halted.

    Nothing in this test asks the target to stop: the only stop that exists is
    whatever the attach itself produced, and the session start has already
    published it. So the read has exactly two honest answers, and both are
    asserted here. Where the start recorded a stop, the read must return that
    same one; where it recorded none, the read must refuse with
    `stop_reason_not_available` and name the two tools that produce one. What
    neither may do is invent a reason, or lose one the start already reported,
    and an assertion written for only one of the two branches would let the
    other through.
    """
    server = mcp_servers()
    started = server.tool("debug_start_session", {"image_path": workspace_image(bench, firmware), "mode": "attach"})
    assert started["ok"] is True, started

    read = server.tool("debug_get_stop_reason")

    if "target_stop_reason" in started:
        assert read["ok"] is True, read
        assert read["stop_reason"] == started["target_stop_reason"], (read, started["target_stop_reason"])
        assert read["stop"] == started["stop"], (read["stop"], started["stop"])
        assert read["session"]["session_id"] == started["session"]["session_id"], read["session"]
    else:
        assert read["ok"] is False, read
        assert read["error_type"] == "stop_reason_not_available", read
        assert "debug_continue" in read["summary"], read["summary"]
        assert "debug_halt" in read["summary"], read["summary"]

    stopped = server.tool("debug_stop_session")
    assert stopped["ok"] is True, stopped


def test_resuming_is_refused_while_allow_debug_execution_is_closed_and_the_grant_line_reopens_it(
    mcp_servers, bench: Bench, firmware: Path
) -> None:
    """The one permission that lifts the core off a halt, and the refusal an agent relays.

    Opening a session is not gated on it: an attach halts the core, and reading
    a halted target perturbs nothing that exclusivity does not already answer
    for. Resuming it is, and that is the seam this holds. Four things have to be
    true at once and each has been wrong: the refusal is `permission_denied` and
    not a debugger fault; it names the dotted key the file uses and
    `agentic-hil grant` takes, as a field and inside the sentence that gets read
    out; it offers that exact line as the operator's next step; and it costs the
    bench nothing, so the session it refused is still open and still stops
    cleanly.

    The permission is revoked and granted back inside this test, on this tier's
    own configuration. No configuration the operator owns is reachable from
    here. Both edits are made with no server running, because a bench that is
    held refuses a permission write, which is also why the server is shut down
    before the grant rather than after it.
    """
    key = f"debuggers.{bench.debugger_name()}.permissions.allow_debug_execution"
    revoked = bench.run("revoke", key)
    server: McpServer | None = None
    try:
        # Inside the block that grants it back, deliberately. A revoke that
        # reported failure may still have written, and a check that stood
        # outside would leave this bench unable to resume a target for every
        # later run in this session.
        assert revoked.returncode == 0, revoked.stdout + revoked.stderr
        server = mcp_servers()
        started = server.tool("debug_start_session", {"image_path": workspace_image(bench, firmware), "mode": "attach"})
        assert started["ok"] is True, started

        refused_envelope = server.envelope("debug_continue", {"timeout_s": 5})
        refused = refused_envelope["structuredContent"]

        assert refused_envelope["isError"] is True, refused_envelope
        assert refused["ok"] is False, refused
        assert refused["tool"] == "debug_continue", refused
        assert refused["error_type"] == "permission_denied", refused
        assert refused["permission"] == key, refused
        # The outermost sentence, which is the one a caller relays.
        assert key in refused["summary"], refused["summary"]
        assert f"{GRANT_COMMAND} {key}" in refused["next_step"], refused["next_step"]
        # A refusal is an answer, not an incident: the session it refused is
        # still the agent's, and the bench is still this server's to give back.
        assert refused["quarantined"] is False, refused
        assert refused["lease_state"] == "active", refused

        still_open = server.tool("debug_get_session_status")
        assert still_open["active"] is True, still_open
        assert still_open["session"]["session_id"] == started["session"]["session_id"], still_open

        stopped = server.tool("debug_stop_session")
        assert stopped["ok"] is True, stopped
    finally:
        # The server goes first and unconditionally: a permission write is
        # refused while this bench is held, so a failed assertion above would
        # otherwise leave the key closed for every later run.
        if server is not None:
            server.shut_down()
        granted_status, granted = bench.document("grant", key)

    assert granted_status == 0, granted
    assert granted["ok"] is True, granted
    reopened = [item for item in (granted.get("changed") or []) + (granted.get("unchanged") or []) if item["key"] == key]
    assert reopened, granted
    assert all(item["value"] is True for item in reopened), reopened


def test_a_server_that_ends_with_a_session_open_hands_the_board_to_the_next_one(
    mcp_servers, bench: Bench, firmware: Path
) -> None:
    """An agent that walks away mid session must not take the bench with it.

    The session is opened and deliberately never stopped: the transport is
    closed under it, which is what happens when an agent host ends the server it
    started. From the board's side that is a process that owned a debug session
    going away, and the server's own shutdown is the only thing standing between
    it and a probe nobody can take again.

    Three claims, in the order they fail. The server ends cleanly rather than
    dying on its own teardown. The bench reads free afterwards, through
    `agentic-hil lease-status`: not blocked, not held, no owner. And the next
    server starts a session on the same board, which is the claim the other two
    are only evidence for. A shutdown that left the lease registered would show
    up here as a start refused `resource_busy` or `resource_quarantined`, and a
    session object that outlived its process would show up as
    `session_already_active` on a server that has never started one.
    """
    abandoned = mcp_servers()
    started = abandoned.tool("debug_start_session", {"image_path": workspace_image(bench, firmware), "mode": "attach"})
    assert started["ok"] is True, started
    first_session_id = started["session"]["session_id"]

    assert abandoned.shut_down(stop_session=False) == 0, abandoned.diagnosis()

    handed_back = bench.document("lease-status")[1]
    assert handed_back["blocked"] is False, handed_back
    assert handed_back["bench_held"] is False, handed_back
    assert handed_back["owner_active"] is False, handed_back

    successor = mcp_servers()
    reopened = successor.tool("debug_start_session", {"image_path": workspace_image(bench, firmware), "mode": "attach"})

    assert reopened["ok"] is True, reopened
    assert reopened["session"]["status"] == "halted", reopened["session"]
    assert reopened["session"]["session_id"] != first_session_id, reopened["session"]

    stopped = successor.tool("debug_stop_session")
    assert stopped["ok"] is True, stopped
