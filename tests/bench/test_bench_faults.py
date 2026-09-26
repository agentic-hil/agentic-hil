"""Real faults on the board, and what the product answers for each of them.

The demo only ever boots and prints one line, so every claim the product makes
about a board that went wrong was, until this module, a claim nobody had put a
wrong board under. The images here are the tier's own (``firmware/``), each one
a single fault and nothing else:

* ``undefined_instruction`` boots, says so once, and executes an instruction the
  core has no meaning for, which escalates to a HardFault whose handler is an
  endless loop. The probe still reaches that core, and the debugger has to name
  the fault.
* ``watchdog`` starts the independent watchdog and never feeds it, so the board
  restarts itself about four times a second and says, on every boot, how many
  boots it has counted and what started this one.
* ``flood`` never stops talking: an eight digit counter and a newline, over and
  over, so any stretch of the stream says where in the stream it was taken, and
  a reader that lost bytes can say exactly how many.
* ``wrong_banner`` is the demo with one character too many in its banner, which
  is the smallest board the declared plan has to call red.

Everything goes through the product's two surfaces: the plan runner puts an
image on the board and the MCP stdio server does the rest, one child per test,
spoken to in JSON-RPC exactly as an agent host speaks to it. The demo goes back
on the board after every test through the ``board_images`` fixture, which every
test here requests before ``servers`` so the servers (and the sessions and the
port they hold) are closed first.

What is read back is read off the board's own line. A reset is proved by the
boot line it provokes, a halt by a line that stays quiet, and a flash by the
demo's banner, so no claim here rests on the tool's word about itself alone.
Nothing asserted identifies hardware: device names, serials and paths are read
out of the product's answers and only checked for presence.
"""

from __future__ import annotations

import json
import os
import queue
import re
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import suppress
from pathlib import Path

import pytest
from result_text import assert_text_projects
from support import scaled_time_bound

from .conftest import BENCH_ONLY, DEMO_IMAGE, Bench, BoardImages, child_command

pytestmark = [pytest.mark.bench, BENCH_ONLY]

MCP_PROTOCOL_VERSION = "2025-06-18"

# How long one JSON-RPC request may take: a flash through OpenOCD is the slow
# case, and a wedged server has to fail the test rather than hold the session.
REPLY_TIMEOUT_S = 300.0
START_SESSION_TIMEOUT_S = 240.0
SHUTDOWN_TIMEOUT_S = scaled_time_bound(60.0)

# One `com_read` pass inside the accumulating reads below.
READ_SLICE_S = 0.5
# Long enough for a reset and a boot line on a slow link.
BOOT_TIMEOUT_S = 15.0
# How long a line has to say nothing to count as a core that is not running.
# The watchdog restarts its board about four times a second, so three seconds
# is a dozen boots that did not happen.
QUIET_S = 3.0
DRAIN_PASSES = 6

# A resume that has to reach a breakpoint, and one that can only time out.
REACHABLE_STOP_TIMEOUT_S = 15.0
UNREACHABLE_STOP_TIMEOUT_S = 3.0

# What the demo says, from the comparator of its own plan.
BANNER = b"Hello World"
# What each image says, from its own source under `firmware/`.
FAULT_BOOT_LINE = b"undefined instruction image booted\n"
WRONG_BANNER_TEXT = "Hello World!"
WATCHDOG_LINE = re.compile(rb"watchdog boot (\d+) cause (\w+)\n")
FLOOD_DIGITS = 8
FLOOD_LINE_BYTES = FLOOD_DIGITS + 1
FLOOD_WRAP = 100_000_000
# 115200 baud, ten bits a byte.
FLOOD_BYTES_PER_S = 11_520

# The symbols the fault image's stops are named by.
FAULT_HANDLER = "HardFault_Handler"
ENTRY_FUNCTION = "main"

# See the watchdog test: how many halt and run cycles it may take before one
# of them misses the watchdog's own reset.
WATCHDOG_RACE_ATTEMPTS = 3

SHA256 = re.compile(r"^[0-9a-f]{64}$")

# How the product tells OpenOCD what to write (a Tcl double-quoted word), and
# where a Linux host says which process has which file open.
PROGRAM_COMMAND = re.compile(r'program "((?:[^"\\]|\\.)*)"')
PROC = Path("/proc")
# The erase of the first sector alone takes a quarter of a second, and the
# image is open for all of it, so a poll this fine cannot miss the window.
KILL_POLL_S = 0.01
FLASH_UNCONFIRMED = "debugger_result_unconfirmed"


class Server:
    """One `agentic-hil mcp-stdio` child, driven as an agent host drives it.

    Answers are pulled off stdout by a thread and handed over by id, so every
    request has a deadline. stderr goes to a file, because a pipe nobody drains
    is a way to deadlock on the one failure worth reading. Spawning and greeting
    are two steps so the fixture can register the child for teardown before a
    handshake that may fail.
    """

    def __init__(self, bench: Bench, stderr_path: Path) -> None:
        self.stderr_path = stderr_path
        self._stderr = stderr_path.open("w", encoding="utf-8")
        self.process = subprocess.Popen(
            child_command("mcp-stdio"),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self._stderr,
            text=True,
            encoding="utf-8",
            cwd=str(bench.project),
            env=bench.environment(),
        )
        self._answers: queue.Queue[str | None] = queue.Queue()
        self._pump = threading.Thread(target=self._collect, daemon=True)
        self._pump.start()
        self._last_id = 0
        self._closed = False
        self._session_opened = False

    @property
    def pid(self) -> int:
        return self.process.pid

    def greet(self) -> None:
        hello = self.request(
            "initialize",
            {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "agentic-hil-bench-tier", "version": "0"},
            },
        )
        assert hello["result"]["serverInfo"]["name"] == "agentic-hil", hello
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def _collect(self) -> None:
        try:
            for line in self.process.stdout or ():
                self._answers.put(line)
        finally:
            self._answers.put(None)

    def stderr_text(self) -> str:
        if not self._stderr.closed:
            self._stderr.flush()
        try:
            return self.stderr_path.read_text(encoding="utf-8").strip()
        except OSError:  # pragma: no cover - a stderr file this host cannot read is not the failure
            return ""

    def _send(self, message: dict) -> None:
        assert self.process.stdin is not None
        self.process.stdin.write(json.dumps(message) + "\n")
        self.process.stdin.flush()

    def request(self, method: str, params: dict | None = None, timeout_s: float = REPLY_TIMEOUT_S) -> dict:
        self._last_id += 1
        request_id = self._last_id
        self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}})
        deadline = time.monotonic() + timeout_s
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError(f"the MCP server did not answer `{method}` within {timeout_s}s. Its stderr said: {self.stderr_text()}")
            try:
                line = self._answers.get(timeout=remaining)
            except queue.Empty:  # pragma: no cover - the deadline above is what ends this loop
                continue
            if line is None:
                raise AssertionError(f"the MCP server ended before answering `{method}`. Its stderr said: {self.stderr_text()}")
            message = json.loads(line)
            if message.get("id") == request_id:
                return message

    def call(self, name: str, arguments: dict | None = None, timeout_s: float = REPLY_TIMEOUT_S) -> tuple[bool, dict]:
        """One tool call: whether the host is told it is an error, and the tool's document.

        The text a host reads has to be the compact projection of the document, and
        a document that is not `ok` has to be flagged, because a refusal whose
        envelope says the call went fine is a refusal a host acts on as success.
        """
        answered = self.request("tools/call", {"name": name, "arguments": arguments or {}}, timeout_s)
        assert "error" not in answered, f"{name} came back as a JSON-RPC error: {answered}"
        result = answered["result"]
        document = result.get("structuredContent")
        assert isinstance(document, dict), f"{name} answered no structuredContent: {result}"
        content = result.get("content")
        assert isinstance(content, list) and content and content[0].get("type") == "text", f"{name} answered no content block: {result}"
        assert_text_projects(result)
        errored = bool(result.get("isError"))
        if document.get("ok") is not True:
            assert errored is True, f"{name} refused and the host was told the call went fine: {result}"
        if name == "debug_start_session":
            self._session_opened = True
        return errored, document

    def try_call(self, name: str, arguments: dict | None = None) -> dict | None:
        """A teardown call: best effort, and never the thing that fails a test."""
        if self.process.poll() is not None:
            return None
        try:
            _, document = self.call(name, arguments)
        except (AssertionError, OSError, ValueError):
            return None
        return document

    def close(self) -> int | None:
        """Stop a debug session it may still hold, then end it the way a host does."""
        if self._closed:
            return self.process.returncode
        if self._session_opened:
            self.try_call("debug_stop_session")
        self._closed = True
        if self.process.poll() is None:
            with suppress(OSError):
                if self.process.stdin is not None:
                    self.process.stdin.close()
            try:
                self.process.wait(timeout=SHUTDOWN_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=SHUTDOWN_TIMEOUT_S)
        self._pump.join(timeout=SHUTDOWN_TIMEOUT_S)
        if self.process.stdout is not None:
            self.process.stdout.close()
        self._stderr.close()
        return self.process.returncode


def incident_left_standing(bench: Bench) -> str | None:
    """Why this test left the bench blocked, after clearing it through `recover`; None if it did not.

    Nothing a test here does may leave an incident standing: a failed call's
    incident is settled or stood down when the call ends. So one that stands is
    reported against the test that left it, and cleared, so the next test meets
    a bench in the state this one found.
    """
    _, status = bench.document("lease-status")
    if not status.get("blocked"):
        return None
    quarantine = str(status.get("quarantine_id") or "none")
    recovered = bench.run("recover", "--confirm-safe-state", "--quarantine-id", quarantine)
    if recovered.returncode != 0:
        recovered = bench.run("recover", "--confirm-safe-state", "--quarantine-id", quarantine, "--accept-config-change")
    return (
        f"this test left the bench blocked with cleanup reasons {status.get('cleanup_reasons')}; "
        f"recover exited {recovered.returncode}: {recovered.stdout}{recovered.stderr}"
    )


@pytest.fixture
def servers(bench: Bench, tmp_path: Path) -> Iterator[Callable[[], Server]]:
    """Start MCP stdio servers, close every one afterwards, and fail a test that left the bench blocked."""
    started: list[Server] = []

    def start() -> Server:
        server = Server(bench, tmp_path / f"mcp-stdio-{len(started)}.stderr")
        started.append(server)
        server.greet()
        return server

    yield start
    failures: list[Exception] = []
    for server in reversed(started):
        try:
            server.close()
        except Exception as error:
            failures.append(error)
    left = incident_left_standing(bench)
    if failures:
        raise failures[0]
    if left is not None:
        pytest.fail(left, pytrace=False)


def backend_type(bench: Bench) -> str:
    return str(bench.configuration()["debuggers"][bench.debugger_name()]["type"])


def require_debug_grants(bench: Bench) -> None:
    """The two grants a session here needs, which `agentic-hil init` writes open."""
    configuration = bench.configuration()
    if configuration.get("debug", {}).get("allow_all_symbols") is not True:
        pytest.fail("this session's configuration does not grant debug.allow_all_symbols, and the stops here are named by symbol", pytrace=False)
    permissions = configuration["debuggers"][bench.debugger_name()]["permissions"]
    if permissions.get("allow_debug_execution") is not True:
        pytest.fail("this session's configuration does not grant allow_debug_execution, and every resume here needs it", pytrace=False)


def put(board_images: BoardImages, name: str) -> str:
    """One of the tier's images on the board and started; its path as a tool argument."""
    report = board_images.put(name)
    assert report.get("ok") is True, report
    return board_images.image(name).relative_to(board_images.bench.project).as_posix()


# -- The serial line ----------------------------------------------------------


def open_port(server: Server, port: str) -> dict:
    errored, opened = server.call("com_session_start", {"port_id": port, "clear_buffer": True})
    assert errored is False, opened
    assert opened["ok"] is True, opened
    return opened


def read(server: Server, port: str, wait_timeout_s: float, max_bytes: int | None = None) -> dict:
    """One `com_read`, asserted to have succeeded."""
    arguments: dict = {"port_id": port, "wait_timeout_s": wait_timeout_s}
    if max_bytes is not None:
        arguments["max_bytes"] = max_bytes
    errored, answered = server.call("com_read", arguments)
    assert errored is False, answered
    assert answered["ok"] is True, answered
    return answered


def received(answered: dict) -> bytes:
    """The bytes one read handed over, taken from the hex rather than the decoded text."""
    data = bytes.fromhex(answered["data"]["hex"])
    assert len(data) == answered["bytes_read"], answered
    return data


def settle_and_discard(server: Server, port: str) -> None:
    """Let a freshly opened line hand over what it had queued, and throw that away.

    The probe's virtual COM port can pass on bytes it held from before the open,
    and a claim about the stream has to be made against bytes the board sent
    while somebody was listening.
    """
    time.sleep(READ_SLICE_S)
    read(server, port, 0.0)


def drain(server: Server, port: str) -> None:
    """Take whatever the line already holds, including the byte a reset can leave on it."""
    for _ in range(DRAIN_PASSES):
        if read(server, port, READ_SLICE_S)["bytes_read"] == 0:
            return
    raise AssertionError(f"the line was still handing over bytes after {DRAIN_PASSES} reads, so nothing here can call it quiet")


def assert_quiet(server: Server, port: str, seconds: float = QUIET_S) -> None:
    """A line that says nothing for this long, which from these images means a core that is not running."""
    answered = read(server, port, seconds)
    assert answered["bytes_read"] == 0, f"the line was meant to be quiet and said {received(answered)[:200]!r}"


def read_until(server: Server, port: str, wanted: bytes, timeout_s: float = BOOT_TIMEOUT_S) -> bytes:
    """Everything the line said up to and including `wanted`, or a failure saying what it said instead."""
    got = b""
    deadline = time.monotonic() + timeout_s
    while wanted not in got:
        if time.monotonic() >= deadline:
            raise AssertionError(f"the line did not say {wanted!r} within {timeout_s}s; it said {got[-400:]!r}")
        got += received(read(server, port, READ_SLICE_S))
    return got


def port_status(server: Server, port: str) -> dict:
    """The port's entry in the listing, which reports the buffer without taking from it."""
    errored, listed = server.call("com_ports_list")
    assert errored is False, listed
    assert listed["ok"] is True, listed
    return listed["ports"][port]


def wait_for_buffered(server: Server, port: str, at_least: int, timeout_s: float = BOOT_TIMEOUT_S) -> dict:
    deadline = time.monotonic() + timeout_s
    while True:
        status = port_status(server, port)
        if status["rx_buffer_bytes"] >= at_least:
            return status
        if time.monotonic() >= deadline:
            raise AssertionError(f"the session buffered {status['rx_buffer_bytes']} bytes in {timeout_s}s, not the {at_least} this needs: {status}")
        time.sleep(0.1)


def watchdog_boots(server: Server, port: str, at_least: int, timeout_s: float = BOOT_TIMEOUT_S) -> list[tuple[int, str]]:
    """The boot lines the watchdog image printed from here on, as (count, cause), once there are enough.

    Only whole lines count: a line the previous read cut in half does not start
    with the image's own words and is skipped rather than misread.
    """
    got = b""
    deadline = time.monotonic() + timeout_s
    while True:
        boots = [(int(match.group(1)), match.group(2).decode("ascii")) for match in WATCHDOG_LINE.finditer(got)]
        if len(boots) >= at_least:
            return boots
        if time.monotonic() >= deadline:
            raise AssertionError(f"the watchdog image printed {len(boots)} boot lines in {timeout_s}s, not {at_least}; the line said {got[-400:]!r}")
        got += received(read(server, port, READ_SLICE_S))


def flood_bytes(start: int, length: int) -> bytes:
    """What the flood image sends from byte `start` of a boot's stream, `length` bytes of it."""
    first = start // FLOOD_LINE_BYTES
    last = (start + length) // FLOOD_LINE_BYTES + 1
    text = b"".join(b"%08d\n" % (line % FLOOD_WRAP) for line in range(first, last + 1))
    offset = start - first * FLOOD_LINE_BYTES
    return text[offset : offset + length]


def flood_position(chunk: bytes) -> int:
    """Where in the flood image's stream this chunk was taken, proved by the whole chunk matching.

    The first whole line inside the chunk names its own position, and every
    other byte of the chunk then has exactly one value it may have. A chunk
    that lost, gained or reordered a single byte does not match.
    """
    newline = chunk.find(b"\n")
    assert newline >= 0 and len(chunk) >= newline + 1 + FLOOD_LINE_BYTES, f"too little of the stream to place: {chunk[:64]!r}"
    digits = chunk[newline + 1 : newline + 1 + FLOOD_DIGITS]
    terminator = chunk[newline + FLOOD_LINE_BYTES : newline + FLOOD_LINE_BYTES + 1]
    assert digits.isdigit() and terminator == b"\n", f"this is not the flood image's stream: {chunk[:64]!r}"
    start = int(digits) * FLOOD_LINE_BYTES - (newline + 1)
    assert start >= 0, f"a chunk that starts before its boot's first byte: {chunk[:64]!r}"
    expected = flood_bytes(start, len(chunk))
    if chunk != expected:
        where = next(index for index, (got, wanted) in enumerate(zip(chunk, expected, strict=True)) if got != wanted)
        raise AssertionError(
            f"the stream is not contiguous: byte {where} of a {len(chunk)} byte read is {chunk[where : where + 20]!r}, "
            f"where the stream placed at {start} has {expected[where : where + 20]!r}"
        )
    return start


# -- The debugger -------------------------------------------------------------


def assert_reset(server: Server, mode: str) -> dict:
    errored, result = server.call("reset_target", {"mode": mode})
    assert errored is False, result
    assert result["ok"] is True, result
    assert result["tool"] == "reset_target", result
    assert result["mode"] == mode, result
    assert str(result["summary"]).startswith(f"Target reset with mode '{mode}'."), result
    assert "error_type" not in result, result
    assert isinstance(result["elapsed_ms"], int) and result["elapsed_ms"] > 0, result
    assert result["log_path"], result
    return result


def reset_into_init(server: Server, bench: Bench) -> bool:
    """`reset_target` in `init` mode; True when this backend has one and the core went through it."""
    if backend_type(bench) == "openocd":
        assert_reset(server, "init")
        return True
    errored, refused = server.call("reset_target", {"mode": "init"})
    assert errored is True, refused
    assert refused["ok"] is False, refused
    assert refused["error_type"] == "not_supported", refused
    assert refused["mode"] == "init", refused
    assert refused["supported_modes"] == ["run", "halt"], refused
    assert "log_path" not in refused, refused
    return False


def assert_probe_finds_the_target(server: Server, bench: Bench) -> dict:
    errored, result = server.call("probe_target")
    assert errored is False, result
    assert result["ok"] is True, result
    assert result["tool"] == "probe_target", result
    assert result["backend"] == backend_type(bench), result
    assert result["target_detected"] is True, result
    assert result["success_confirmed"] is True, result
    assert "error_type" not in result, result
    assert result.get("quarantined") is not True, result
    assert result.get("audit_ok") is not False, result
    assert isinstance(result["elapsed_ms"], int), result
    for key in ("started_at", "finished_at", "log_path"):
        assert isinstance(result[key], str) and result[key].strip(), (key, result)
    return result


def assert_flash_holds(result: dict) -> None:
    assert result["ok"] is True, result
    assert result["tool"] == "flash_firmware", result
    assert result["verify"] is True, result
    assert "error_type" not in result, result
    assert SHA256.match(str(result["artifact"]["sha256"])), result
    assert isinstance(result["elapsed_ms"], int) and result["elapsed_ms"] > 0, result
    assert result["log_path"], result


def flash_the_demo_over(server: Server, port: str) -> bytes:
    """The demo flashed over whatever the board runs, through the MCP tool, proved by its banner."""
    errored, flashed = server.call("flash_firmware", {"image_path": DEMO_IMAGE.as_posix(), "reset_after_flash": True})
    assert errored is False, flashed
    assert_flash_holds(flashed)
    assert str(flashed["summary"]).startswith("Firmware flashed, verified, and target reset."), flashed
    # The whole line, so a quiet line afterwards is not broken by its own newline.
    return read_until(server, port, BANNER + b"\n")


def start_session(server: Server, image: str, mode: str) -> dict:
    errored, started = server.call("debug_start_session", {"image_path": image, "mode": mode}, START_SESSION_TIMEOUT_S)
    assert started["ok"] is True, started
    assert started["mode"] == mode, started
    summary = "Debug session started and target is halted."
    if started.get("retried_connects"):
        # An attach whose connect the debug server dropped connected again (#575).
        assert mode == "attach", started
        assert str(started["summary"]).startswith(f"{summary} The debug server dropped the first"), started
    else:
        assert started["summary"] == summary, started
    assert started["session"]["status"] == "halted", started["session"]
    assert started["session"]["firmware_load_status"] == "not_started", started["session"]
    assert started["quarantined"] is False, started
    return started


def assert_session_stops_halted(server: Server) -> dict:
    errored, stopped = server.call("debug_stop_session")
    assert errored is False, stopped
    assert stopped["ok"] is True, stopped
    assert stopped["active"] is False, stopped
    assert stopped["status"] == "stopped", stopped
    assert stopped["safe_state_confirmed"] is True, stopped
    assert stopped["halt_not_confirmed"] is False, stopped
    assert stopped["detach_resume_guard_confirmed"] is True, stopped
    assert stopped["summary"] == "Debug session stopped with the target confirmed halted.", stopped
    return stopped


def assert_stopped_in_the_fault(document: dict) -> None:
    """A stop the debugger read as the image's HardFault, with the handler as its frame."""
    stop = document["stop"]
    assert stop["stop_reason"] == "exception", stop
    assert stop["exception_type"] == "hardfault", stop
    assert stop["frame"]["function"] == FAULT_HANDLER, stop


# -- The faulted board --------------------------------------------------------


def test_a_faulted_board_is_probed_reset_in_every_mode_and_flashed_back_to_the_demo(bench: Bench, board_images: BoardImages, servers) -> None:
    """A core sitting in a HardFault is still a core the product can reach and bring back.

    The fault handler leaves the debug port alone, so the probe has to find the
    target, each reset mode has to do what it says (a run boots the image again,
    which the line proves by printing the boot line again; a halt and an init
    leave a core that says nothing), and a flash has to replace the image,
    which the demo's banner proves. Catches a probe that reads a faulted core
    as absent, a reset that reports success over a core it never restarted,
    and a halt that lets the core run on.
    """
    put(board_images, "undefined_instruction")
    port = bench.com_port_name()
    server = servers()
    open_port(server, port)
    settle_and_discard(server, port)

    assert_reset(server, "run")
    read_until(server, port, FAULT_BOOT_LINE)

    assert_probe_finds_the_target(server, bench)

    assert_reset(server, "halt")
    drain(server, port)
    assert_quiet(server, port)

    if reset_into_init(server, bench):
        drain(server, port)
        assert_quiet(server, port)

    assert_reset(server, "run")
    read_until(server, port, FAULT_BOOT_LINE)
    drain(server, port)

    flash_the_demo_over(server, port)


def test_debug_session_attach_on_a_faulted_board_names_the_hardfault_and_stops_clean(bench: Bench, gdb: None, board_images: BoardImages, servers) -> None:
    """Attach to a core that is already in its fault handler: the session has to say so.

    The core was running the fault image, so it sits in the HardFault handler
    when the session attaches. Whether the session reads that stop at start or
    at the first resume is the backend's business; what is claimed is that the
    fault is named as a fault (`exception`, a HardFault, the handler as the
    frame) wherever it is read, that neither a halt nor a resume pretends the
    core is fine, that none of it quarantines the bench, and that the session
    still ends with the core confirmed halted.
    """
    require_debug_grants(bench)
    image = put(board_images, "undefined_instruction")
    server = servers()

    started = start_session(server, image, "attach")
    assert started["session"]["load_phase"] == "target_connected", started["session"]

    if "target_stop_reason" in started:
        # Read at start: the stop the attach found is the fault.
        assert started["target_stop_reason"] == "exception", started
        assert started["target_error_type"] == "target_exception", started
        assert started["target_ok"] is False, started

        errored, reason = server.call("debug_get_stop_reason")
        assert reason["ok"] is True, reason
        assert reason["stop_reason"] == "exception", reason
        assert_stopped_in_the_fault(reason)
        assert reason["suggested_actions"], reason

        errored, halted = server.call("debug_halt")
        assert errored is True, halted
        assert str(halted["summary"]).startswith("Target was already stopped"), halted
        assert halted["error_type"] == "target_exception", halted
        assert halted.get("quarantined") is not True, halted

        errored, resumed = server.call("debug_continue", {"timeout_s": UNREACHABLE_STOP_TIMEOUT_S})
        assert errored is True, resumed
        assert resumed["summary"] == "Target is already stopped: exception.", resumed
        assert resumed["error_type"] == "target_exception", resumed
        assert resumed.get("quarantined") is not True, resumed
    else:
        # Nothing read at start: the first resume runs into the fault loop and
        # the stop it confirms after its timeout is the fault.
        errored, reason = server.call("debug_get_stop_reason")
        assert errored is True, reason
        assert reason["error_type"] == "stop_reason_not_available", reason

        errored, resumed = server.call("debug_continue", {"timeout_s": UNREACHABLE_STOP_TIMEOUT_S})
        assert errored is True, resumed
        assert resumed["error_type"] == "timeout", resumed
        assert resumed["halt_confirmed"] is True, resumed
        assert resumed["target_state"] == "halted", resumed
        assert resumed["target_stop_reason"] == "exception", resumed
        assert resumed["target_error_type"] == "target_exception", resumed
        assert_stopped_in_the_fault(resumed)
        assert resumed.get("quarantined") is not True, resumed
        assert resumed.get("cleanup_required") is not True, resumed

        errored, reason = server.call("debug_get_stop_reason")
        assert reason["ok"] is True, reason
        assert reason["stop_reason"] == "exception", reason
        assert_stopped_in_the_fault(reason)

    assert_session_stops_halted(server)


def test_debug_session_reset_halt_on_a_faulted_board_runs_from_main_into_the_named_fault(bench: Bench, gdb: None, board_images: BoardImages, servers) -> None:
    """From reset, through `main`, into the fault: each stop named for what it is.

    A reset into halt clears the fault, so the session's own start must not
    report one. A breakpoint on `main` is then reached as a breakpoint, and the
    next resume runs the image to its undefined instruction: the line proves
    the core got there (the boot line comes out between the two stops), and the
    stop the debugger confirms after its timeout is the HardFault, named by its
    handler. A resume over a core already stopped in a fault answers that
    instead of resuming it.
    """
    require_debug_grants(bench)
    image = put(board_images, "undefined_instruction")
    port = bench.com_port_name()
    server = servers()
    open_port(server, port)

    started = start_session(server, image, "reset_halt")
    assert started["session"]["load_phase"] == "pre_load_reset_confirmed", started["session"]
    # The reset cleared the fault the core was sitting in before the session.
    assert started.get("target_ok") is not False, started
    drain(server, port)

    errored, placed = server.call("debug_set_breakpoint", {"location": ENTRY_FUNCTION})
    assert errored is False, placed
    assert placed["ok"] is True, placed

    errored, at_main = server.call("debug_continue", {"timeout_s": REACHABLE_STOP_TIMEOUT_S})
    assert errored is False, at_main
    assert at_main["ok"] is True, at_main
    assert at_main["stop_reason"] == "breakpoint_hit", at_main
    assert at_main["target_ok"] is True, at_main
    assert at_main["stop"]["breakpoint_expected"] is True, at_main
    assert at_main["stop"]["frame"]["function"] == ENTRY_FUNCTION, at_main

    errored, faulted = server.call("debug_continue", {"timeout_s": UNREACHABLE_STOP_TIMEOUT_S})
    assert errored is True, faulted
    assert faulted["error_type"] == "timeout", faulted
    assert faulted["halt_confirmed"] is True, faulted
    assert faulted["target_state"] == "halted", faulted
    assert faulted["target_stop_reason"] == "exception", faulted
    assert faulted["target_error_type"] == "target_exception", faulted
    assert_stopped_in_the_fault(faulted)
    assert faulted.get("quarantined") is not True, faulted
    assert faulted.get("cleanup_required") is not True, faulted
    read_until(server, port, FAULT_BOOT_LINE)

    errored, reason = server.call("debug_get_stop_reason")
    assert reason["ok"] is True, reason
    assert reason["stop_reason"] == "exception", reason
    assert_stopped_in_the_fault(reason)

    errored, again = server.call("debug_continue", {"timeout_s": UNREACHABLE_STOP_TIMEOUT_S})
    assert errored is True, again
    assert again["summary"] == "Target is already stopped: exception.", again
    assert again["error_type"] == "target_exception", again
    assert again.get("quarantined") is not True, again

    assert_session_stops_halted(server)


# -- The board the watchdog keeps restarting ----------------------------------


def test_a_board_the_watchdog_keeps_restarting_is_held_by_a_halt_restarted_by_a_run_and_flashed_back(bench: Bench, board_images: BoardImages, servers) -> None:
    """Every reset mode over a board that resets itself, read off the board's own boot count.

    The image counts its boots in RAM and names what started each one, so the
    line says exactly what the product did: a halt and an init stop the
    restarting (a reset stops a watchdog software started, and a core held
    before `main` never starts it again), a run restarts the board from the
    debugger rather than from the watchdog, after which the watchdog takes over
    again, and a flash puts the demo back with the restarting gone for good.
    """
    put(board_images, "watchdog")
    port = bench.com_port_name()
    server = servers()
    open_port(server, port)
    settle_and_discard(server, port)

    boots = watchdog_boots(server, port, 3)
    assert all(cause == "iwdg" for _, cause in boots), boots
    counts = [count for count, _ in boots]
    assert counts == list(range(counts[0], counts[0] + len(counts))), boots
    seen = max(counts)

    # The watchdog can fire in the few milliseconds between the debugger arming
    # its catch on the reset vector and resetting the core, and then the core is
    # caught on the watchdog's reset with the watchdog's flag set, which the
    # next boot then reports. That is the board telling the truth about a real
    # reset, not the product misbehaving, so the cycle is repeated a bounded
    # number of times; every repetition still has to hold every other claim.
    first_cause = "iwdg"
    for _ in range(WATCHDOG_RACE_ATTEMPTS):
        assert_reset(server, "halt")
        drain(server, port)
        assert_quiet(server, port)

        if reset_into_init(server, bench):
            drain(server, port)
            assert_quiet(server, port)

        assert_reset(server, "run")
        after = watchdog_boots(server, port, 2)
        (first_count, first_cause), (second_count, second_cause) = after[0], after[1]
        # The count survived: a restart, not a board that lost its RAM.
        assert first_count > seen, (seen, after)
        # And the watchdog took over again after the debugger's boot.
        assert (second_count, second_cause) == (first_count + 1, "iwdg"), after
        seen = max(count for count, _ in after)
        if first_cause != "iwdg":
            break
    assert first_cause != "iwdg", f"every boot after a reset from the debugger named the watchdog as its cause, {WATCHDOG_RACE_ATTEMPTS} times"

    flash_the_demo_over(server, port)
    assert_quiet(server, port)


def test_debug_session_attach_on_a_self_resetting_board_holds_it_halted_and_leaves_it_halted(bench: Bench, gdb: None, board_images: BoardImages, servers) -> None:
    """A debug session over a board that restarts itself keeps it still, and so does its end.

    Attaching halts the core, and the watchdog is frozen while the core is
    halted under a debugger, so the boot lines stop for as long as the session
    holds the board. The board's own reset can land while the debugger
    connects, and the start connects again when it does (#575). The stop says
    the target is confirmed halted and that the detach will not resume it; the
    line is how that is checked, because a core resumed at the detach meets a
    watchdog that is still running and the board starts printing boot lines
    again.
    """
    require_debug_grants(bench)
    image = put(board_images, "watchdog")
    port = bench.com_port_name()
    server = servers()
    open_port(server, port)
    settle_and_discard(server, port)

    started = start_session(server, image, "attach")
    assert started["session"]["load_phase"] == "target_connected", started["session"]
    drain(server, port)
    assert_quiet(server, port)

    errored, status = server.call("debug_get_session_status")
    assert status["ok"] is True, status
    assert status["active"] is True, status
    assert status["status"] == "halted", status

    errored, halted = server.call("debug_halt")
    assert str(halted["summary"]).startswith("Target was already stopped"), halted
    assert halted.get("quarantined") is not True, halted

    assert_session_stops_halted(server)
    assert_quiet(server, port)


# -- The board that never stops talking ---------------------------------------


def test_a_flooded_line_hands_over_exactly_the_cap_in_stream_order_and_survives_a_stop_and_restart(bench: Bench, board_images: BoardImages, servers) -> None:
    """A capped read under a flood takes the cap, from the front, and leaves the rest in order.

    The flood image's stream says where every byte belongs, so a read is
    checked byte for byte: exactly the cap handed over, the next read starting
    at the byte the first one ended on, and a session stopped under the flood
    refusing reads and starting again onto the same stream further on.
    """
    put(board_images, "flood")
    port = bench.com_port_name()
    server = servers()
    open_port(server, port)
    settle_and_discard(server, port)

    wait_for_buffered(server, port, 1000)
    first = read(server, port, 0.0, max_bytes=100)
    assert first["bytes_read"] == 100, first
    assert first["buffer_remaining_bytes"] > 0, first
    assert first["overflow_bytes"] == 0, first
    start = flood_position(received(first))
    end = start + 100

    second = read(server, port, 0.0, max_bytes=1000)
    assert second["bytes_read"] > FLOOD_LINE_BYTES * 2, second
    assert second["overflow_bytes"] == 0, second
    assert flood_position(received(second)) == end, (end, second["bytes_read"])
    end += second["bytes_read"]

    errored, stopped = server.call("com_session_stop", {"port_id": port})
    assert errored is False, stopped
    assert stopped["ok"] is True, stopped
    assert stopped["was_active"] is True, stopped

    errored, refused = server.call("com_read", {"port_id": port, "wait_timeout_s": 0.0})
    assert errored is True, refused
    assert refused["error_type"] == "session_not_active", refused

    errored, reopened = server.call("com_session_start", {"port_id": port, "clear_buffer": True})
    assert errored is False, reopened
    assert reopened["ok"] is True, reopened
    assert reopened["already_active"] is False, reopened
    settle_and_discard(server, port)
    wait_for_buffered(server, port, 1000)
    third = read(server, port, 0.0, max_bytes=1000)
    assert flood_position(received(third)) > end, (end, third["bytes_read"])


def test_a_flooded_line_fills_to_its_limit_counts_what_it_dropped_and_clears_only_when_asked(bench: Bench, board_images: BoardImages, servers) -> None:
    """The buffer limit, the overflow count and `clear_buffer`, each checked against the stream itself.

    Left unread, the session fills to its configured limit and then drops the
    oldest bytes, counting them. The count is checked rather than read: the
    first byte the next read hands over has to be exactly as far past the last
    byte read before as the count says was dropped. A second start without
    `clear_buffer` has to keep the buffer and the count, so the next read
    carries on where the last one ended; a start with it has to empty both, so
    the next read begins past everything that was buffered.
    """
    put(board_images, "flood")
    port = bench.com_port_name()
    server = servers()
    open_port(server, port)
    settle_and_discard(server, port)
    limit = port_status(server, port)["max_buffer_bytes"]
    assert isinstance(limit, int) and limit > 0, limit

    wait_for_buffered(server, port, 1000)
    before = read(server, port, 0.0, max_bytes=1000)
    end = flood_position(received(before)) + before["bytes_read"]
    dropped = before["overflow_bytes"]

    deadline = time.monotonic() + limit / FLOOD_BYTES_PER_S * 2 + 10
    while True:
        status = port_status(server, port)
        if status["overflow_bytes"] > dropped and status["rx_buffer_bytes"] == limit:
            break
        assert time.monotonic() < deadline, f"the session never filled to its limit of {limit} and dropped bytes: {status}"
        time.sleep(READ_SLICE_S)

    full = read(server, port, 0.0)
    assert full["bytes_read"] == limit, (limit, full["bytes_read"])
    assert full["overflow_bytes"] > dropped, full["overflow_bytes"]
    assert flood_position(received(full)) == end + (full["overflow_bytes"] - dropped), (end, dropped, full["overflow_bytes"])
    end += full["overflow_bytes"] - dropped + limit
    dropped = full["overflow_bytes"]

    wait_for_buffered(server, port, 2000)
    errored, kept = server.call("com_session_start", {"port_id": port, "clear_buffer": False})
    assert errored is False, kept
    assert kept["ok"] is True, kept
    assert kept["already_active"] is True, kept
    assert kept["session"]["overflow_bytes"] == dropped, kept["session"]
    assert kept["session"]["rx_buffer_bytes"] >= 2000, kept["session"]
    carried = read(server, port, 0.0, max_bytes=1000)
    assert carried["overflow_bytes"] == dropped, carried["overflow_bytes"]
    assert flood_position(received(carried)) == end, (end, carried["bytes_read"])
    end += carried["bytes_read"]

    wait_for_buffered(server, port, 2000)
    errored, cleared = server.call("com_session_start", {"port_id": port, "clear_buffer": True})
    assert errored is False, cleared
    assert cleared["ok"] is True, cleared
    assert cleared["already_active"] is True, cleared
    assert cleared["session"]["overflow_bytes"] == 0, cleared["session"]
    wait_for_buffered(server, port, 1000)
    fresh = read(server, port, 0.0, max_bytes=1000)
    assert fresh["overflow_bytes"] == 0, fresh["overflow_bytes"]
    assert flood_position(received(fresh)) >= end + 2000, (end, fresh["bytes_read"])


# -- The board with the wrong banner ------------------------------------------


def test_the_declared_plan_over_a_board_with_the_wrong_banner_is_red_on_every_surface(bench: Bench, board_images: BoardImages, servers) -> None:
    """The demo's own plan, pointed at an image whose banner is one character off, fails and says why.

    `equals` is the whole line, so `Hello World!` is not `Hello World`, and the
    run has to be red for the board's answer rather than refused for a setup
    error: headed `Failed: comparator_unmet` for a person, the same error at
    step four in the report, with what the board did say in the read's tail.
    The agent's two readers of the last run (`get_last_report` and
    `classify_last_error`) and the evidence a reviewer reads (`run-evidence`)
    have to tell the same story.
    """
    image = board_images.image("wrong_banner")
    # The plan's own first step flashes it, so the demo has to go back after.
    board_images.displaced = True
    declared = (bench.project / "testconfig.yaml").read_text(encoding="utf-8")
    demo_line = f"image_path: {DEMO_IMAGE.as_posix()}"
    name_line = "name: nucleo-f446re-hello-world"
    assert declared.count(demo_line) == 1, declared
    assert declared.count(name_line) == 1, declared
    plan_name = "wrong-banner-under-the-declared-plan"
    plan = bench.project / f"{plan_name}.yaml"
    plan.write_text(
        declared.replace(demo_line, f"image_path: {image.relative_to(bench.project).as_posix()}").replace(name_line, f"name: {plan_name}"),
        encoding="utf-8",
    )

    rendered = bench.run("test-reactor", "--test-config", plan.name)
    assert rendered.returncode == 1, rendered.stdout + rendered.stderr
    assert rendered.stdout.startswith("Failed: comparator_unmet"), rendered.stdout[:400]
    assert "Refused:" not in rendered.stdout, rendered.stdout[:400]

    status, report = bench.document("test-reactor", "--test-config", plan.name)
    report_path = Path("artifacts") / "reports" / f"{plan_name}.json"
    (bench.project / report_path).parent.mkdir(parents=True, exist_ok=True)
    (bench.project / report_path).write_text(json.dumps(report), encoding="utf-8")
    assert status == 1, report
    assert report["ok"] is False, report
    assert report["error_type"] == "comparator_unmet", report
    assert report["failed_step"] == 4, report
    assert report["step_error_type"] == "comparator_unmet", report
    read_step = report["steps"][3]
    assert read_step["action"] == "uart_read", report["steps"]
    assert read_step["result"]["error_type"] == "comparator_unmet", read_step
    assert WRONG_BANNER_TEXT in read_step["result"]["received_tail"]["text"], read_step

    server = servers()
    errored, last = server.call("get_last_report")
    assert errored is False, last
    assert last["ok"] is True, last
    assert last["tool"] == "get_last_report", last
    assert last["report"]["tool"] == "test_reactor", last["report"]
    assert last["report"]["error_type"] == "comparator_unmet", last["report"]
    assert last["report"]["failed_step"] == 4, last["report"]

    errored, classified = server.call("classify_last_error")
    assert classified["ok"] is True, classified
    assert classified["error_type"] == "comparator_unmet", classified
    assert classified["source_tool"] == "test_reactor", classified
    assert classified["failed_step"] == 4, classified
    assert classified["step_error_type"] == "comparator_unmet", classified
    assert isinstance(classified["likely_causes"], list) and classified["likely_causes"], classified

    out = Path("artifacts") / "evidence-wrong-banner"
    built = bench.run("run-evidence", "--report", report_path.as_posix(), "--out", out.as_posix())
    assert built.returncode == 0, built.stdout + built.stderr
    summary = json.loads((bench.project / out / "run-summary.json").read_text(encoding="utf-8"))
    assert summary["outcome"] == "failure", summary
    assert summary["run"]["failed_step"] == 4, summary["run"]
    assert summary["run"]["error_type"] == "comparator_unmet", summary["run"]
    job = (bench.project / out / "job-summary.md").read_text(encoding="utf-8")
    assert f"## Agentic HIL: {plan_name}" in job, job
    assert "**Outcome:** failure" in job, job
    assert "### Step 4 failed: `comparator_unmet`" in job, job


# -- The flash that never finished --------------------------------------------


def process_stat(pid: int) -> tuple[int, int] | None:
    """(parent, start time) of one process, from /proc; None once it is gone."""
    try:
        stat = (PROC / str(pid) / "stat").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    # The command name sits in parentheses and may itself hold spaces and parentheses.
    fields = stat[stat.rindex(")") + 2 :].split()
    return int(fields[1]), int(fields[19])


def descendants(root: int) -> set[int]:
    """Every process below `root`: the product's own children, and theirs."""
    parents: dict[int, int] = {}
    for entry in PROC.iterdir():
        if entry.name.isdigit():
            stat = process_stat(int(entry.name))
            if stat is not None:
                parents[int(entry.name)] = stat[0]
    found: set[int] = set()
    frontier = {root}
    while frontier:
        frontier = {pid for pid, parent in parents.items() if parent in frontier} - found
        found |= frontier
    return found


def programmed_image(pid: int) -> str | None:
    """The image a debugger process was told to program, read off its own command line."""
    try:
        arguments = (PROC / str(pid) / "cmdline").read_bytes().split(b"\0")
    except OSError:
        return None
    for argument in arguments:
        match = PROGRAM_COMMAND.search(argument.decode("utf-8", errors="replace"))
        if match is not None:
            return re.sub(r"\\(.)", r"\1", match.group(1))
    return None


def holds_open(pid: int, path: str) -> bool:
    try:
        descriptors = list((PROC / str(pid) / "fd").iterdir())
    except OSError:
        return False
    for descriptor in descriptors:
        with suppress(OSError):
            if os.readlink(descriptor) == path:
                return True
    return False


def kill_the_flash_while_it_writes(server: Server, image: Path, finished: threading.Event) -> None:
    """SIGKILL the debugger process this test's own server started, while it has the image open.

    The process is looked for only among the server's descendants and only by
    the image it was told to program. That is the product's private staged
    copy of the image, which keeps the image's file name, so the name is what
    is matched and the path the process itself was given is what is watched.
    It is killed only while it holds that file open, which OpenOCD does from
    before the erase to the end of the write: so the flash dies with the old
    image gone and the new one not complete. No other process on the host is
    signalled.
    """
    seen: set[str] = set()
    while not finished.is_set():
        for pid in descendants(server.pid):
            named = programmed_image(pid)
            if named is None or Path(named).name != image.name:
                continue
            programmed = os.path.realpath(named)
            seen.add(programmed)
            identity = process_stat(pid)
            while identity is not None and not finished.is_set():
                # Still the same process, and still this server's, at the moment of the kill.
                if holds_open(pid, programmed) and process_stat(pid) == identity and pid in descendants(server.pid):
                    os.kill(pid, signal.SIGKILL)
                    return
                time.sleep(KILL_POLL_S)
                if process_stat(pid) != identity:
                    identity = None
        time.sleep(KILL_POLL_S)
    raise AssertionError(
        "the flash finished before its debugger process was seen holding the image open, so nothing was aborted; "
        + (f"a process below the server was told to program {len(seen)} such file(s) and never held one open" if seen else "no process below the server was seen programming it")
    )


def test_a_flash_killed_mid_write_is_recovered_by_the_product_and_a_second_flash_brings_the_demo_back(
    bench: Bench, board_images: BoardImages, servers
) -> None:
    """The debugger dies with the flash half written, and the product's own answer brings the board back.

    Last in this module on purpose: it is the one test here that leaves the
    board without a whole image, for as long as the recovery and the second
    flash take. The kill is the only thing done outside the product, and it
    lands on the product's own child (see `kill_the_flash_while_it_writes`).

    What is asserted is the documented answer, in order. The call fails, as the
    single-action run it is, and that run aborts into the recovery action:
    reap, reset into halt where the policy allows it, re-read the probe. The
    flash is left unconfirmed and the result carries that reason, but the bench
    is not held for it: `quarantined: true` means a broken evidence chain and
    nothing else, and a bare call's incident is over when its call is. So the
    lease status reads a free bench, both recovery routes answer that there is
    nothing to recover, `classify_last_error` names this flash, and a second
    flash, accepted without any recovery step, puts the demo back, which its
    banner proves.
    """
    if backend_type(bench) != "openocd":
        pytest.skip("the kill is aimed at OpenOCD's own `program` command, and this bench's debugger is not OpenOCD")
    if not (PROC / "self" / "fd").is_dir():
        pytest.skip("finding the product's own debugger process needs /proc")
    # From the erase on, the board holds no whole image until the demo is back.
    board_images.displaced = True
    server = servers()

    answers: dict = {}
    finished = threading.Event()

    def flash() -> None:
        try:
            answers["flash"] = server.call("flash_firmware", {"image_path": DEMO_IMAGE.as_posix(), "reset_after_flash": True})
        except BaseException as error:  # handed to the test's own thread below
            answers["error"] = error
        finally:
            finished.set()

    worker = threading.Thread(target=flash, daemon=True)
    worker.start()
    try:
        kill_the_flash_while_it_writes(server, bench.project / DEMO_IMAGE, finished)
    finally:
        worker.join(REPLY_TIMEOUT_S)
    assert not worker.is_alive(), f"flash_firmware did not answer within {REPLY_TIMEOUT_S}s of its debugger being killed"
    if "error" in answers:
        raise answers["error"]
    errored, aborted = answers["flash"]

    assert errored is True, aborted
    assert aborted["ok"] is False, aborted
    assert aborted["tool"] == "flash_firmware", aborted
    assert isinstance(aborted.get("error_type"), str) and aborted["error_type"], aborted
    # Killed, not timed out: the answer has to be about the process that died.
    assert aborted["error_type"] != "timeout", aborted
    assert FLASH_UNCONFIRMED in aborted["cleanup_reasons"], aborted
    run = aborted["run"]
    assert run["implicit"] is True, run
    assert run["aborted"] is True, run
    recovery = aborted["recovery"]
    assert recovery["attempted"] is True, recovery
    if recovery["auto_recover_policy"] == "reset_halt":
        assert recovery["actions"] == ["reap_processes", "reset_halt", "probe_target"], recovery
        assert recovery["safe_state_predicate"] == "reset_halt", recovery
        assert recovery["outcome"] == "recovered", recovery
        assert recovery["incident_resolved"] is True, recovery
        assert recovery["resolved_reason"] == FLASH_UNCONFIRMED, recovery
    assert aborted["quarantined"] is False, aborted

    errored, classified = server.call("classify_last_error")
    assert classified["ok"] is True, classified
    assert classified["source_tool"] == "flash_firmware", classified
    assert classified["error_type"] == (aborted.get("target_error_type") or aborted["error_type"]), classified

    _, status = bench.document("lease-status")
    assert status["blocked"] is False, status
    assert status["incident_stands"] is False, status
    assert status["auto_recoverable"] is False, status
    assert status["bench_held"] is False, status

    quarantine = str(recovery.get("resolved_quarantine_id") or "none")
    code, recovered = bench.document("recover", "--confirm-safe-state", "--quarantine-id", quarantine)
    assert code == 0, recovered
    assert recovered["ok"] is True, recovered
    assert recovered["nothing_to_recover"] is True, recovered
    assert recovered["was_quarantined"] is False, recovered

    errored, answered = server.call("hardware_recover")
    assert errored is False, answered
    assert answered["ok"] is True, answered
    assert answered["nothing_to_recover"] is True, answered
    assert answered["was_quarantined"] is False, answered

    port = bench.com_port_name()
    open_port(server, port)
    settle_and_discard(server, port)
    flash_the_demo_over(server, port)
