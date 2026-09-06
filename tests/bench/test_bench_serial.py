"""The configured serial line on the real bench: opened, written to, read, and closed.

Everything here goes through one of the product's two surfaces and through
nothing else. The session tools live only on the MCP stdio server, so that is
what these tests drive: one `agentic-hil mcp-stdio` child per test, spoken to in
JSON-RPC over its own stdin and stdout, exactly as an agent host speaks to it.
The listing and the plan half go through the command line the same way the rest
of this tier does. No test here opens a serial device, and none could: the port
is held exclusively by whichever session the product opened, which is itself one
of the things asserted below.

Why a server per test rather than one for the file. A COM session is owned by
the process that opened it, and the machine-wide device lock it takes is
released when that process ends. A test that failed half way through a session
would otherwise hand the next test a bench it still holds, and the failure a
reader then sees would be the second test's. One child per test, closed in the
fixture's teardown whatever the test did, makes every test here runnable twice
in a row and in any order.

What the board is. The starter's firmware, built before the run, whose whole
published protocol is one line: `printf("Hello World\\n")` at boot, asserted by
the demo's own plan as `comparator: {equals: "Hello World"}` in
`examples/nucleo-f446re_demo/testconfig.yaml`. It accepts no commands, so the
stimulus these tests send is the one the published plan sends, `text: "capture\\n"`
in `examples/testconfig.example.yaml`, and what is asserted about it is the
write's own contract: how many bytes reached the line, what the session logged,
and that the line still answers afterwards. The answer that is read back is the
banner, and it is provoked the way the plan provokes it, with a reset through
the product.

What a test here can leave standing, and who clears it. A write whose effect on
the line cannot be confirmed, and a close that does not confirm either, hold
this bench until an operator signs for it. Every test in this file is followed
by `bench_is_left_clear`, which reads `lease-status` and clears exactly what it
names with `agentic-hil recover --confirm-safe-state --quarantine-id`, so a run
of this file ends with the bench in the state it started in or fails saying it
could not. The same clearing runs before a permission is put back, because a
quarantine is itself an open hold and a permission does not move under one.

Nothing here asserts a value that identifies hardware. The device name, the
serial number and the log path are read out of the product's own answers and
compared with each other; what is asserted about them is that they are present,
non-empty and consistent across two surfaces, never what they say.
"""

from __future__ import annotations

import json
import queue
import subprocess
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import suppress
from pathlib import Path

import pytest

from .conftest import BENCH_ONLY, Bench, child_command

pytestmark = [pytest.mark.bench, BENCH_ONLY]

# The stimulus the published expanded plan sends on a serial line:
# `- {device: dut_uart, action: uart_write, text: "capture\n"}` in
# examples/testconfig.example.yaml. The starter firmware reads nothing, so this
# is a command in the plan vocabulary's sense and not in the board's: what is
# claimed about it below is what the product did with it, never what the board
# made of it.
STIMULUS = "capture\n"

# What the starter does say, from the comparator of the demo plan's own read
# step in examples/nucleo-f446re_demo/testconfig.yaml.
BANNER = "Hello World"

# Long enough for a reset and a boot banner on a slow link.
BANNER_TIMEOUT_S = 15.0

# One `com_read` pass inside the accumulating reads below. Short, because the
# accumulation is what waits.
READ_SLICE_S = 0.5

# How long one JSON-RPC request may take to be answered. A flash or a reset
# through OpenOCD is the slow case; a wedged server has to fail the test rather
# than hold the session.
REPLY_TIMEOUT_S = 300.0

# How long a server gets to close its sessions and exit after its input closes.
SHUTDOWN_TIMEOUT_S = 60.0

# How many empty-handed passes it takes to call a line quiet.
DRAIN_PASSES = 6

MCP_PROTOCOL_VERSION = "2025-06-18"


class Server:
    """One `agentic-hil mcp-stdio` child, driven as an agent host drives it.

    Answers are pulled off stdout by a thread and handed over by id, so a
    request can be given a deadline instead of blocking the suite on a server
    that stopped answering. stderr goes to a file rather than a pipe: the server
    writes there only when its configuration will not load, and a pipe nobody
    drains is a way to deadlock on exactly that failure.

    Starting and greeting are two steps on purpose. The constructor only spawns,
    so the fixture below can register the child for teardown *before* the
    handshake that may fail; a greeting that raised inside the constructor would
    leave a server nobody holds a reference to and nobody closes.
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

    def greet(self) -> None:
        """The MCP handshake, and the one claim about who answered it."""
        hello = self.request(
            "initialize",
            {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "agentic-hil-bench-tier", "version": "0"},
            },
        )
        assert hello["result"]["serverInfo"]["name"] == "agentic-hil", hello
        self.notify("notifications/initialized")

    def _collect(self) -> None:
        try:
            for line in self.process.stdout or ():
                self._answers.put(line)
        finally:
            self._answers.put(None)

    def stderr_text(self) -> str:
        """Whatever the server wrote beside its protocol, which is where a configuration it will not load lands.

        Readable after `close` as well as before it, because the thing most
        worth reading is why a server that was asked to end did not end well.
        """
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

    def notify(self, method: str) -> None:
        """A notification, which by the protocol is answered by nothing."""
        self._send({"jsonrpc": "2.0", "method": method})

    def request(self, method: str, params: dict | None = None, timeout_s: float = REPLY_TIMEOUT_S) -> dict:
        """One JSON-RPC request, and the message that carries its id back."""
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

    def call(self, name: str, arguments: dict | None = None, timeout_s: float = REPLY_TIMEOUT_S) -> dict:
        """One tool call, answered with the MCP result envelope around the tool's document."""
        answered = self.request("tools/call", {"name": name, "arguments": arguments or {}}, timeout_s)
        assert "result" in answered, answered
        return answered["result"]

    def close(self) -> int | None:
        """End it the way a host ends it: close its input and let it clean up.

        Closing stdin is the whole of it. The stdio loop reads to EOF and then
        closes its tool service, which is what stops every session this server
        opened and gives back the device locks it took. A server that will not
        end is killed rather than waited on, so a wedged child fails the test it
        belongs to instead of the run.
        """
        if self._closed:
            return self.process.returncode
        self._closed = True
        if self.process.poll() is None:
            with suppress(OSError):  # a pipe already gone needs no closing
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


def tool_document(envelope: dict) -> dict:
    """The tool's own document out of one MCP result, held to the flag beside it.

    `isError` is what an agent host reads before it reads anything else, so a
    document that refused and an envelope that says the call went fine is a
    refusal nobody sees. Checked in the one direction that is always true of
    this server: a result that is not `ok` is flagged.
    """
    structured = envelope["structuredContent"]
    assert isinstance(structured, dict), envelope
    if structured.get("ok") is not True:
        assert envelope["isError"] is True, envelope
    return structured


def read_once(server: Server, port_id: str, wait_timeout_s: float) -> dict:
    """One `com_read`, asserted to have succeeded, so callers can read its fields."""
    answered = tool_document(server.call("com_read", {"port_id": port_id, "wait_timeout_s": wait_timeout_s}))
    assert answered["ok"] is True, answered
    return answered


def read_until(server: Server, port_id: str, wanted: str, timeout_s: float = BANNER_TIMEOUT_S) -> str:
    """Everything the line said until `wanted` was among it, or the time ran out.

    A single read hands over whatever the session had buffered at that moment,
    which for a line arriving in pieces is a piece. Accumulating is what the
    plan's own read step does, and it is what a caller of the tool has to do.
    """
    received = ""
    deadline = time.monotonic() + timeout_s
    while True:
        received += read_once(server, port_id, READ_SLICE_S)["data"]["text"]
        if wanted in received or time.monotonic() >= deadline:
            return received


def drain(server: Server, port_id: str) -> None:
    """Take whatever the line already holds off it, so a later read is about what comes next.

    A line that is still handing bytes over after this many reads is not a line
    any claim below about a quiet one can be made against, so that says so here
    rather than in the middle of a test whose subject is something else.
    """
    for _ in range(DRAIN_PASSES):
        if read_once(server, port_id, READ_SLICE_S)["bytes_read"] == 0:
            return
    raise AssertionError(f"the line was still handing over bytes after {DRAIN_PASSES} reads, so nothing here can call it quiet")


def buffered_bytes(server: Server, port_id: str) -> int:
    """What the session is holding, asked through the listing so the asking consumes nothing."""
    listed = tool_document(server.call("com_ports_list"))
    assert listed["ok"] is True, listed
    return listed["ports"][port_id]["rx_buffer_bytes"]


def settled_buffer(server: Server, port_id: str, timeout_s: float = BANNER_TIMEOUT_S) -> int:
    """Wait until the line has said something and finished saying it.

    Two equal readings taken through `com_ports_list`, which reports the count
    without taking the bytes: a claim about a buffer that was cleared has to be
    made against a buffer that had settled, or it is a claim about a race.
    """
    deadline = time.monotonic() + timeout_s
    previous = -1
    while time.monotonic() < deadline:
        current = buffered_bytes(server, port_id)
        if current > 0 and current == previous:
            return current
        previous = current
        time.sleep(READ_SLICE_S)
    raise AssertionError(f"the line buffered nothing that settled within {timeout_s}s of a reset through the product")


def audit_entries(bench: Bench, log_path: str) -> list[dict]:
    """One session's log, as the entries it is made of.

    `log_path` is the display path the session reported, which is workspace
    relative, so this reads the file the product named rather than one this test
    went looking for.
    """
    lines = (bench.project / log_path).read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def clear_any_quarantine(bench: Bench) -> None:
    """Whatever this bench is holding an operator signature for, signed off through `recover`.

    Written as a function and not only as the fixture below because two
    teardowns need it and the order between them matters: a quarantine is an
    open hold, and `agentic-hil grant` is refused under one, so a permission is
    put back only after this has run. Doing it twice costs nothing: `recover`
    over a bench with nothing standing answers that it had nothing to do.

    The second attempt exists because a test that moved a permission has, by
    doing so, changed the configuration the incident was recorded under, and
    that is an override an operator is asked for by name.
    """
    _, status = bench.document("lease-status")
    if not status.get("blocked"):
        return
    quarantine = status.get("quarantine_id")
    assert isinstance(quarantine, str) and quarantine, f"this bench is blocked and names no quarantine to clear: {status}"
    recovered = bench.run("recover", "--confirm-safe-state", "--quarantine-id", quarantine)
    if recovered.returncode != 0:
        recovered = bench.run("recover", "--confirm-safe-state", "--quarantine-id", quarantine, "--accept-config-change")
    assert recovered.returncode == 0, recovered.stdout + recovered.stderr


@pytest.fixture(autouse=True)
def bench_is_left_clear(bench: Bench) -> Iterator[None]:
    """Whatever a test in this file quarantined, cleared through the operator's own command.

    A write whose effect could not be confirmed, or a close that did not, holds
    this bench until somebody signs for it, and a tier that left one standing
    would fail every test after it for a reason belonging to the first.
    """
    yield
    clear_any_quarantine(bench)


@pytest.fixture
def servers(bench: Bench, bench_is_left_clear: None, tmp_path: Path) -> Iterator[Callable[[], Server]]:
    """Start MCP stdio servers, and close every one of them afterwards.

    The teardown runs whether the test passed, failed or raised, and it runs
    before the quarantine check above, because a session still open is a device
    still held and the check would then be reading this test's own leftovers.
    Every child is closed even when one of them will not end: a first failure
    that skipped the rest would leave the sessions this file's next test needs.
    """
    started: list[Server] = []

    def start() -> Server:
        server = Server(bench, tmp_path / f"mcp-stdio-{len(started)}.stderr")
        # Registered before the handshake, so a server that will not greet is
        # still a server this teardown closes.
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
    if failures:
        raise failures[0]


@pytest.fixture
def port(bench: Bench) -> str:
    """The configured serial line this bench publishes, by the name a plan writes."""
    return bench.com_port_name()


@pytest.fixture
def open_session(servers: Callable[[], Server], port: str) -> Callable[[], tuple[Server, dict]]:
    """A server with the line open on it, and the document that opened it.

    Closing is the `servers` teardown's job and not a step a test has to
    remember: ending the server ends the session, which is the product's own
    contract and is asserted on its own below.
    """

    def start() -> tuple[Server, dict]:
        server = servers()
        opened = tool_document(server.call("com_session_start", {"port_id": port, "clear_buffer": True}))
        assert opened["ok"] is True, opened
        return server, opened

    return start


@pytest.fixture
def write_permission_restored(bench: Bench, port: str) -> Iterator[str]:
    """The dotted key for writing to this line, granted again whatever the test did to it.

    Every test that touches this permission takes this fixture, including the
    one that only asserts the permission may *not* move: were that assertion to
    fail, it would fail because the move went through, and the next test in the
    file would then meet a line it may no longer write to for a reason nothing
    in it names.

    Ask for it before `servers` in a test's signature. Fixtures are torn down in
    the reverse of the order they were set up, and a server still running is a
    session still holding the line, which is exactly what a permission change is
    refused under. A quarantine is the other kind of hold and is refused the same
    way, so this clears one before it grants.
    """
    key = f"com_ports.{port}.permissions.allow_write"
    yield key
    clear_any_quarantine(bench)
    granted = bench.run("grant", key)
    assert granted.returncode == 0, granted.stdout + granted.stderr


@pytest.fixture
def revoked_write_permission(bench: Bench, write_permission_restored: str) -> str:
    """`allow_write` on the configured line, closed before the test starts.

    Revoked before any session exists, deliberately: a permission may not move
    under a held bench, so the change has to happen while nothing holds the
    line. Putting it back is `write_permission_restored`'s job, which is set up
    before this and therefore torn down after it.
    """
    revoked = bench.run("revoke", write_permission_restored)
    assert revoked.returncode == 0, revoked.stdout + revoked.stderr
    return write_permission_restored


def test_a_session_opens_sends_the_plans_stimulus_reads_the_starters_banner_and_closes(
    bench: Bench, open_session: Callable[[], tuple[Server, dict]], port: str
) -> None:
    """The whole serial loop through the tools, and the close that really closes.

    Catches a `com_session_stop` that answers `ok` while leaving the line open:
    the read after it has to be refused as having no session, and a stop that
    only forgot its own bookkeeping would answer that read from a session that
    is still running.

    The stimulus is the published plan's `text: "capture\\n"`; the starter reads
    nothing, so what is claimed about the write is the write's own contract. The
    answer is the starter's boot banner, provoked with a reset through the
    product, which is how the demo's own plan provokes it.
    """
    server, opened = open_session()
    session = opened["session"]

    assert opened["tool"] == "com_session_start", opened
    assert opened["port_id"] == port, opened
    assert opened["already_active"] is False, opened
    assert session["session_active"] is True, session
    assert isinstance(session["log_path"], str) and session["log_path"], session
    assert isinstance(session["started_at"], str) and session["started_at"], session
    assert isinstance(opened["identity"]["device"], str) and opened["identity"]["device"], opened["identity"]

    written = tool_document(server.call("com_write", {"port_id": port, "text": STIMULUS}))
    assert written["ok"] is True, written
    assert written["tool"] == "com_write", written
    assert written["bytes_written"] == len(STIMULUS.encode(written["data"]["encoding"])), written
    assert written["data"]["text"] == STIMULUS, written
    assert written["data"]["hex"] == STIMULUS.encode(written["data"]["encoding"]).hex(), written

    reset = tool_document(server.call("reset_target", {"mode": "run"}))
    assert reset["ok"] is True, reset

    received = read_until(server, port, BANNER)
    assert BANNER in received, received[-400:]

    stopped = tool_document(server.call("com_session_stop", {"port_id": port}))
    assert stopped["ok"] is True, stopped
    assert stopped["was_active"] is True, stopped
    assert stopped["session"]["session_active"] is False, stopped

    after = tool_document(server.call("com_read", {"port_id": port, "wait_timeout_s": 0.0}))
    assert after["ok"] is False, after
    assert after["error_type"] == "session_not_active", after


def test_opening_the_same_line_twice_answers_already_active_and_clears_what_the_line_had_said(
    open_session: Callable[[], tuple[Server, dict]], port: str
) -> None:
    """A second open is the same session said twice, and its `clear_buffer` is a clear.

    Two failures. An open that replaced a live session instead of recognising it
    would report a new `started_at` and a new log file, and the bytes the first
    session had already recorded would be in a log nothing points at any more.
    And a second open that took `clear_buffer` as a no-op because the session was
    already there would leave the previous boot's banner sitting in the buffer,
    where the next read matches it and calls it this boot's.

    The buffer is filled first, with a reset through the product, so the claim
    about the clear is made against a line that really had something to clear.
    """
    server, opened = open_session()
    assert tool_document(server.call("reset_target", {"mode": "run"}))["ok"] is True
    assert settled_buffer(server, port) > 0

    again = tool_document(server.call("com_session_start", {"port_id": port, "clear_buffer": True}))

    assert again["ok"] is True, again
    assert again["already_active"] is True, again
    assert again["session"]["started_at"] == opened["session"]["started_at"], (again, opened)
    assert again["session"]["log_path"] == opened["session"]["log_path"], (again, opened)
    assert again["session"]["session_active"] is True, again
    assert again["session"]["rx_buffer_bytes"] == 0, again
    # And the line the second open reported on is still the line: it reads.
    assert read_once(server, port, 0.2)["ok"] is True


def test_a_read_with_no_session_is_refused_by_name_and_names_the_call_that_opens_one(
    servers: Callable[[], Server], port: str
) -> None:
    """The refusal a caller that skipped the open has to read.

    Catches a read that answers an empty success when no session exists: an
    agent would report a silent board where the truth is that nothing was ever
    listening, and the two have opposite remedies.
    """
    server = servers()

    refused = tool_document(server.call("com_read", {"port_id": port, "wait_timeout_s": 0.0}))

    assert refused["ok"] is False, refused
    assert refused["tool"] == "com_read", refused
    assert refused["port_id"] == port, refused
    assert refused["error_type"] == "session_not_active", refused
    assert "com_session_start" in refused["summary"], refused["summary"]
    assert "bytes_read" not in refused, refused


def test_a_read_naming_a_line_this_configuration_does_not_declare_is_refused_as_unconfigured(
    servers: Callable[[], Server], port: str
) -> None:
    """A name that is not a port is answered about the name, not about a session.

    Catches the two being collapsed into one refusal: "you did not open this"
    sends a caller to open a port that does not exist, and the answer it needs
    is the list of the ports that do.
    """
    server = servers()

    refused = tool_document(server.call("com_read", {"port_id": "a_line_this_bench_does_not_declare", "wait_timeout_s": 0.0}))

    assert refused["ok"] is False, refused
    assert refused["error_type"] == "com_port_not_configured", refused
    assert port in refused["configured_ports"], refused


def test_stopping_a_line_that_was_never_opened_is_a_success_that_says_it_was_not_active(
    servers: Callable[[], Server], port: str
) -> None:
    """Closing what is already closed is an answer, not a failure.

    Catches a stop that refuses when there is nothing to stop: cleanup paths
    close every line they might have opened, and a refusal there turns a tidy
    exit into a reported error about a port that was fine.
    """
    server = servers()

    stopped = tool_document(server.call("com_session_stop", {"port_id": port}))

    assert stopped["ok"] is True, stopped
    assert stopped["was_active"] is False, stopped
    assert "session" not in stopped, stopped


def test_a_read_that_finds_nothing_waits_out_its_timeout_and_answers_an_empty_success(
    open_session: Callable[[], tuple[Server, dict]], port: str
) -> None:
    """A quiet line is a successful read of nothing, and it takes the time it was given.

    Two failures at once. A read that returned the moment it found an empty
    buffer would answer before the board it is waiting on had a chance to speak,
    which is the whole of what `wait_timeout_s` is for; and a read that reported
    an error for having received nothing would make every poll of an idle line
    look like a broken one.

    The starter prints its banner at boot and nothing afterwards, so the line is
    drained first and the measured read is then about a line that is genuinely
    quiet.
    """
    server, _ = open_session()
    drain(server, port)

    started = time.monotonic()
    answered = read_once(server, port, 2.0)
    elapsed = time.monotonic() - started

    assert answered["bytes_read"] == 0, answered
    assert answered["buffer_remaining_bytes"] == 0, answered
    assert answered["data"]["text"] == "", answered
    assert "error_type" not in answered, answered
    assert elapsed >= 1.75, f"the read answered after {elapsed:.2f}s of a 2s wait"


def test_a_capped_read_hands_over_the_cap_and_leaves_the_rest_where_it_was(
    open_session: Callable[[], tuple[Server, dict]], port: str
) -> None:
    """`max_bytes` bounds one answer, it does not discard what did not fit.

    Catches a capped read that drops the remainder of the buffer: the banner
    would arrive one byte at a time and the rest of it would be gone, and a plan
    reading a long line in slices would never see the end of it.
    """
    server, _ = open_session()
    drain(server, port)
    reset = tool_document(server.call("reset_target", {"mode": "run"}))
    assert reset["ok"] is True, reset

    first = tool_document(server.call("com_read", {"port_id": port, "max_bytes": 1, "wait_timeout_s": BANNER_TIMEOUT_S}))
    assert first["ok"] is True, first
    assert first["bytes_read"] == 1, first
    assert len(first["data"]["text"]) == 1, first

    rest = first["data"]["text"] + read_until(server, port, BANNER)
    assert BANNER in rest, rest[-400:]


def test_a_write_naming_both_text_and_hex_is_refused_before_the_line_and_leaves_the_session_usable(
    bench: Bench, open_session: Callable[[], tuple[Server, dict]], port: str
) -> None:
    """A payload that says two things is refused, and nothing goes out while it is decided.

    Catches a refusal raised after the bytes were already sent: the log is what
    says whether the line was touched, and a stimulus recorded there for a call
    that answered `invalid_argument` is a board that moved on an argument the
    product had refused.
    """
    server, opened = open_session()

    refused = tool_document(server.call("com_write", {"port_id": port, "text": STIMULUS, "hex": "00"}))

    assert refused["ok"] is False, refused
    assert refused["error_type"] == "invalid_argument", refused
    assert "bytes_written" not in refused, refused

    written = tool_document(server.call("com_write", {"port_id": port, "text": STIMULUS}))
    assert written["ok"] is True, written

    sent = [entry for entry in audit_entries(bench, opened["session"]["log_path"]) if entry.get("direction") == "tx"]
    assert len(sent) == 1, sent
    assert sent[0]["text"] == STIMULUS, sent[0]


def test_a_write_longer_than_the_configured_maximum_is_refused_naming_both_numbers(
    open_session: Callable[[], tuple[Server, dict]], port: str
) -> None:
    """The bound is read off the configuration and the refusal states both sides of it.

    Catches a bound that is enforced without being reported: a caller told only
    that its write was invalid cannot tell whether to split the payload or to
    fix it, and the two numbers are what decides that.

    The maximum is read from the product's own listing rather than written
    here, so this is a claim about whatever this bench configured.
    """
    server, _ = open_session()
    listed = tool_document(server.call("com_ports_list"))
    assert listed["ok"] is True, listed
    maximum = listed["ports"][port]["max_write_bytes"]
    assert isinstance(maximum, int) and maximum >= 1, listed["ports"][port]
    payload = "a" * (maximum + 1)

    refused = tool_document(server.call("com_write", {"port_id": port, "text": payload}))

    assert refused["ok"] is False, refused
    assert refused["error_type"] == "invalid_argument", refused
    assert refused["bytes_requested"] == len(payload.encode(listed["ports"][port]["encoding"])), refused
    assert refused["max_write_bytes"] == maximum, refused
    assert "bytes_written" not in refused, refused
    # Refused before the line, so the line is still there.
    assert read_once(server, port, 0.2)["ok"] is True


def test_a_write_is_refused_by_its_permission_key_while_it_is_revoked_and_sends_once_it_is_granted_back(
    bench: Bench, port: str, revoked_write_permission: str, servers: Callable[[], Server]
) -> None:
    """The refusal an agent relays, and the grant that is the only way past it.

    Three things have to hold at once and each has failed on its own. The
    refusal is `permission_denied` rather than a fault in the call. It names the
    dotted key the file uses and `agentic-hil grant` takes, in the sentence a
    caller reads out as well as in a field. And it is a refusal and not a
    failure of the session: the line stays open and readable, so a plan that met
    it can report and carry on rather than treat the bench as lost.

    The permission is closed and opened again on this tier's own configuration.
    No configuration the operator owns is reachable from here. Both moves happen
    while nothing holds the line, because a permission may not move under a held
    bench.
    """
    key = revoked_write_permission
    narrowed = servers()
    opened = tool_document(narrowed.call("com_session_start", {"port_id": port, "clear_buffer": True}))
    assert opened["ok"] is True, opened

    envelope = narrowed.call("com_write", {"port_id": port, "text": STIMULUS})
    refused = tool_document(envelope)

    assert envelope["isError"] is True, envelope
    assert refused["ok"] is False, refused
    assert refused["error_type"] == "permission_denied", refused
    assert refused["permission"] == key, refused
    assert key in refused["summary"], refused["summary"]
    assert f"agentic-hil grant {key}" in refused["next_step"], refused["next_step"]
    assert "bytes_written" not in refused, refused
    assert read_once(narrowed, port, 0.2)["ok"] is True
    assert tool_document(narrowed.call("com_session_stop", {"port_id": port}))["ok"] is True
    narrowed.close()

    granted = bench.run("grant", key)
    assert granted.returncode == 0, granted.stdout + granted.stderr

    widened = servers()
    reopened = tool_document(widened.call("com_session_start", {"port_id": port, "clear_buffer": True}))
    assert reopened["ok"] is True, reopened
    written = tool_document(widened.call("com_write", {"port_id": port, "text": STIMULUS}))
    assert written["ok"] is True, written
    assert written["bytes_written"] == len(STIMULUS.encode(written["data"]["encoding"])), written
    # Closed here rather than left to the teardown: this test's own fixture puts
    # the permission back, and a permission may not move while a session holds
    # the line, so the line has to be free before this test returns.
    assert tool_document(widened.call("com_session_stop", {"port_id": port}))["ok"] is True
    widened.close()


def test_moving_a_permission_is_refused_while_a_session_holds_the_line(
    bench: Bench, write_permission_restored: str, open_session: Callable[[], tuple[Server, dict]]
) -> None:
    """A held line is a line whose rules stay where they were.

    Catches a grant or revoke that writes while a session is open: the session
    took the line under the permissions in the file, and moving one underneath
    it changes the rules of a run already under way, which no result afterwards
    could honestly describe.
    """
    open_session()
    key = write_permission_restored

    status, refused = bench.document("revoke", key)

    assert status != 0, refused
    assert refused["ok"] is False, refused
    assert refused["error_type"] == "permission_change_in_open_run", refused
    assert refused["open_holds"], refused
    assert "nothing was written" in refused["summary"], refused["summary"]


def test_the_session_log_records_the_open_the_stimulus_the_feedback_and_the_close_in_that_order(
    bench: Bench, open_session: Callable[[], tuple[Server, dict]], port: str
) -> None:
    """The file a reviewer reads instead of the bench, in the order things happened.

    Catches a log whose lines are in the order two threads reached the file
    rather than the order of the events: the reader appends what arrives the
    moment it has it, so without the write and its own entry being one section
    the banner provoked by the reset lands ahead of the stimulus sent before it,
    and the log reads as a board answering before it was asked.
    """
    server, opened = open_session()
    log_path = opened["session"]["log_path"]
    drain(server, port)

    written = tool_document(server.call("com_write", {"port_id": port, "text": STIMULUS}))
    assert written["ok"] is True, written
    assert tool_document(server.call("reset_target", {"mode": "run"}))["ok"] is True
    assert BANNER in read_until(server, port, BANNER)
    stopped = tool_document(server.call("com_session_stop", {"port_id": port}))
    assert stopped["ok"] is True, stopped

    entries = audit_entries(bench, log_path)
    assert entries, f"the session reported {log_path} and wrote nothing into it"
    assert entries[0]["event"] == "start", entries[0]
    assert entries[0]["port_id"] == port, entries[0]
    assert isinstance(entries[0]["device"], str) and entries[0]["device"], entries[0]
    assert entries[-1]["event"] == "stop", entries[-1]
    assert entries[-1]["reason"] == "requested", entries[-1]

    stimulus = [index for index, entry in enumerate(entries) if entry.get("direction") == "tx"]
    assert len(stimulus) == 1, [entries[index] for index in stimulus]
    assert entries[stimulus[0]]["text"] == STIMULUS, entries[stimulus[0]]
    assert entries[stimulus[0]]["bytes"] == written["bytes_written"], (entries[stimulus[0]], written)
    assert entries[stimulus[0]]["hex"] == written["data"]["hex"], (entries[stimulus[0]], written)

    feedback = [index for index, entry in enumerate(entries) if entry.get("direction") == "rx"]
    assert feedback, "the banner was read through the tool and no rx entry recorded it"
    # The claim the ordering exists for, stated as one line: the banner was
    # provoked after the stimulus was sent, so the entries it was recorded in
    # are after the entry the stimulus was recorded in.
    answered = [index for index in feedback if index > stimulus[0]]
    assert BANNER in "".join(entries[index]["text"] for index in answered), entries

    stamps = [entry["time"] for entry in entries]
    assert stamps == sorted(stamps), stamps


def test_the_port_listing_names_the_open_session_and_still_enumerates_the_host(
    open_session: Callable[[], tuple[Server, dict]], port: str
) -> None:
    """Asking what the bench has is answerable while a line is held, and says it is held.

    Two failures. A listing that opened each port to describe it would fail on
    the very port this session holds exclusively, so the one line an operator is
    asking about would be the one missing from the answer; and a listing that
    did not report the session would leave `bench_run_status` and this tool
    disagreeing about whether the bench is busy.
    """
    server, opened = open_session()

    listed = tool_document(server.call("com_ports_list"))

    assert listed["ok"] is True, listed
    entry = listed["ports"][port]
    assert entry["session_active"] is True, entry
    assert entry["log_path"] == opened["session"]["log_path"], (entry, opened)
    assert entry["started_at"] == opened["session"]["started_at"], (entry, opened)
    assert isinstance(entry["device"], str) and entry["device"], entry
    assert listed["available_com_ports"]["ok"] is True, listed["available_com_ports"]
    assert listed["available_com_ports"]["ports"], "the host inventory came back empty while a session held one of its ports"
    # The listing was a read of the host and not a touch of the line.
    assert read_once(server, port, 0.2)["ok"] is True


def test_the_command_line_port_listing_answers_while_a_session_holds_the_line(
    bench: Bench, open_session: Callable[[], tuple[Server, dict]], port: str
) -> None:
    """The operator's own listing, run from a second process against a held bench.

    Catches a listing that takes the device lock or opens the device: either one
    turns `agentic-hil com-ports`, which is what an operator runs when something
    is wrong, into a command that fails exactly while a session is running.
    """
    server, _ = open_session()

    status, listed = bench.document("com-ports")

    assert status == 0, listed
    assert listed["ok"] is True, listed
    assert listed["ports"], listed
    for described in listed["ports"]:
        assert isinstance(described["device"], str) and described["device"], described
    # The second process asked and did not take: the session still reads.
    assert read_once(server, port, 0.2)["ok"] is True


def test_a_plan_is_refused_by_name_while_a_session_holds_the_line_and_no_step_runs(
    bench: Bench, open_session: Callable[[], tuple[Server, dict]], port: str
) -> None:
    """The plan surface, met by a line somebody else is already holding.

    Catches a plan that opens a line a live session holds. The lock is
    machine-wide and it is the whole of what keeps two runs off one board, so a
    plan that got past it would interleave its bytes with the session's and both
    would read answers that did not belong to their own stimulus. The refusal
    has to come before the first step, and the report has to say so.
    """
    open_session()
    plan = bench.project / "serial-line-that-is-already-held.yaml"
    plan.write_text(
        f"""version: 3
name: serial-line-that-is-already-held
steps:
  - device: {port}
    action: uart_open
    clear_buffer: true
  - device: {port}
    action: uart_read
    timeout_s: 1
""",
        encoding="utf-8",
    )

    status, report = bench.document("test-reactor", "--test-config", "serial-line-that-is-already-held.yaml")
    rendered = bench.run("test-reactor", "--test-config", "serial-line-that-is-already-held.yaml")

    assert status == 1, report
    assert report["ok"] is False, report
    assert report["error_type"] == "device_busy", report
    assert report["steps"] == [], report
    assert report["declared_devices"], report
    assert "No step ran." in report["summary"], report["summary"]
    assert rendered.returncode == 1, rendered.stdout
    assert rendered.stdout.startswith("Refused: device_busy"), rendered.stdout[:400]


def test_a_plan_opens_and_closes_its_own_session_and_gives_the_line_back(
    bench: Bench, servers: Callable[[], Server], port: str
) -> None:
    """A plan with no close step closes anyway, and the proof is the next open.

    Catches end-of-run cleanup that reports a close it did not perform. The
    report's own `cleanup_ok` is written by the same code that would be wrong,
    so it is not the evidence here: the evidence is that a second process can
    open the line afterwards, which an operating system will not allow while the
    first handle is still there.
    """
    plan = bench.project / "serial-session-through-a-plan.yaml"
    plan.write_text(
        f"""version: 3
name: serial-session-through-a-plan
steps:
  - device: {port}
    action: uart_open
    clear_buffer: true
  - device: {port}
    action: uart_read
    timeout_s: 2
""",
        encoding="utf-8",
    )

    status, report = bench.document("test-reactor", "--test-config", "serial-session-through-a-plan.yaml")

    assert status == 0, report
    assert report["ok"] is True, report
    assert [step["action"] for step in report["steps"]] == ["uart_open", "uart_read"], report["steps"]
    assert [step["result"]["tool"] for step in report["steps"]] == ["com_session_start", "com_read"], report["steps"]
    for step in report["steps"]:
        assert step["result"]["ok"] is True, step
    read = report["steps"][1]["result"]
    assert isinstance(read["bytes_read"], int), read
    assert read["data"]["encoding"], read

    assert report["cleanup_ok"] is True, report
    closes = [entry for entry in report["cleanup"] if entry["action"] == "uart_close"]
    assert len(closes) == 1, report["cleanup"]
    assert closes[0]["result"]["tool"] == "com_session_stop", closes[0]
    assert closes[0]["result"]["ok"] is True, closes[0]
    assert closes[0]["result"]["was_active"] is True, closes[0]

    _, free = bench.document("lease-status")
    assert free["bench_held"] is False, free
    assert free["held_devices"] == [], free

    server = servers()
    reopened = tool_document(server.call("com_session_start", {"port_id": port, "clear_buffer": True}))
    assert reopened["ok"] is True, reopened
    assert reopened["already_active"] is False, reopened


def test_a_server_that_ends_closes_the_session_it_opened_and_gives_the_line_back(
    bench: Bench, servers: Callable[[], Server], port: str
) -> None:
    """A host that goes away does not leave the bench held.

    Catches a shutdown that exits without closing its sessions: the device lock
    would outlive the process that took it, the next run would be refused as
    busy by a holder that no longer exists, and the only way back would be an
    operator at the bench.
    """
    server = servers()
    opened = tool_document(server.call("com_session_start", {"port_id": port, "clear_buffer": True}))
    assert opened["ok"] is True, opened

    ended = server.close()

    assert ended == 0, f"the MCP server exited {ended} after its input closed; its stderr said: {server.stderr_text()}"
    _, free = bench.document("lease-status")
    assert free["bench_held"] is False, free
    assert free["held_devices"] == [], free
    assert free["blocked"] is False, free

    successor = servers()
    reopened = tool_document(successor.call("com_session_start", {"port_id": port, "clear_buffer": True}))
    assert reopened["ok"] is True, reopened
    assert reopened["session"]["log_path"] != opened["session"]["log_path"], (reopened, opened)
