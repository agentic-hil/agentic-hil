"""The board as the other end of the line: the product's serial tools against a peer that answers.

The demo prints one banner and reads nothing, so over the real line the rest of
this tier can prove a banner and nothing a caller sends. This module puts the
bench's own peer image on the board instead (`firmware/peer.c`): it reads what
the product writes, answers out of a table, and counts what arrived. Against it
run the container tier's serial scenarios, from
`tests/container/test_serial_over_pty.py` and
`tests/container/test_com_stdio_on_a_pty.py`: the same stimuli, the same plans
and the same assertions, with a board where the container has a pseudo-terminal
and a scripted process.

They are twins rather than one body run against two counterparties. The
container's tests build their project, their configuration and their peer
inside the body, around a pseudo-terminal pair, and one body for both would
mean restructuring that tier; its assertions stay exactly as they are.

The vocabulary is the container's. `peer.start_responder(*replies, delay_s=...,
announce=..., announce_every_s=...)` sets the answer table, spelled the way the
container's responder reads it, and `peer.received()` says what reached the
board. The container reads its peer's record file. A board keeps no file, so
`received()` is the peer's own count and CRC-32 of every payload byte since the
test began (a `Tally`), and the claim that the peer saw exactly `b"PING\\r\\n"`
is the claim that both agree with those six bytes.

A plan that fails is the exception. The product ends a failed run with a
recovery action, which under the default policy resets the target into halt
(`docs/test-plan-contract.md`, `TROUBLESHOOTING.md`), and any reset zeroes the
peer's count. So the twins of the failing plans take what the plan wrote from
the session log its open named, and what the board received from the answer
the report quotes, which the peer gives only to the exact line. The peer's
silence afterwards is the board's side of the report's reset into halt, and
the peer is then started again through `reset_target`.

Everything goes through the product, the peer's own settings included: a
control line is a `com_write` over a session that a separate
`agentic-hil mcp-stdio` opens for it, and its answer is a `com_read`. The line
is held exclusively, so the peer is asked what arrived only once the test's own
session has ended.

Where the image lives. The peer goes on the board once, for this module, and
the demo goes back after its last test through `BoardImages.restore`, so no
test outside this file meets it. Each test starts from a reset over the peer's
own control line; a peer that does not answer it (left at another rate by a
test that failed half way) is restarted through `reset_target` and asked again.
What a test quarantined is cleared through `agentic-hil recover` afterwards,
under every configuration the test ran against.

What is not here, and why:

* the scenarios whose fault only a pseudo-terminal can stage: a link that leads
  nowhere, a device this user cannot read, a logs directory the server cannot
  write, a session log that stops taking lines, a device that vanishes under a
  session, a line that stops draining, and a port another program holds
  exclusively. On a bench each would mean opening the device outside the
  product or pulling the board.
* the listing of an entry with no USB identity. This bench's entry names its
  hardware, and `tests/bench/test_bench_serial.py` already proves the real
  entry's listing, free and held, through the tools and through
  `agentic-hil com-ports`.
* a range claim without a pattern, which is refused before any port is opened.
* `port_not_enumerated`. Its bench counterpart is a device the inventory does
  show under another identity than the entry names, which is the mismatch
  test below.
* `com-stdio` on a device that is gone, and the pyserial version the container
  image records: neither involves a board.
"""

from __future__ import annotations

import json
import queue
import re
import shutil
import subprocess
import threading
import time
import zlib
from collections.abc import Iterator
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml

from .conftest import BENCH_ONLY, COMMAND_TIMEOUT_S, Bench, BoardImages, child_command, isolated_environment

pytestmark = [pytest.mark.bench, BENCH_ONLY]

# The answer tables, spelled the way both peers read them: the escapes are
# Python's, so `\\r\\n` here is the two bytes on the wire. The same strings the
# container tier hands its responder.
PING_PONG = "PING=PONG\\r\\n"
VERSION_LINE = "VERSION=v1.2.3\\r\\n"
COUNT_LINE = "COUNT=COUNT=42\\r\\n"
AT_OK = "AT=OK\\r\\n"
LATIN_RULE = "Gr\\xfc\\xdfe=\\xdcber\\r\\n"
# The request is the whole line as received, prefix bytes and all.
RAW_RULE = "\\xff\\xfe\\x00RAW=\\xff\\xfePONG\\r\\n"
# What a byte that is not UTF-8 reads as in a UTF-8 text.
REPLACED = "\N{REPLACEMENT CHARACTER}"

# The peer's answer to `FLOOD`: 202 bytes on a port whose buffer holds 64.
FLOOD_LINE = "0123456789" * 20
FLOOD_RULE = f"FLOOD={FLOOD_LINE}\\r\\n"
BUFFER_BYTES = 64

# The rate the baud test moves the peer and one configuration to.
OTHER_RATE = 57600

READ_TIMEOUT_S = 10.0
# How long one control line may take to be answered.
ANSWER_TIMEOUT_S = 5.0
# A reset through the probe and the peer's boot line after it.
BOOT_TIMEOUT_S = 15.0
REPLY_TIMEOUT_S = 300.0
SHUTDOWN_TIMEOUT_S = 60.0
# The bridge's idle window after stdin closes, as the container gives it.
EOF_IDLE_S = 3.0
MCP_PROTOCOL_VERSION = "2025-06-18"

# A reset, led by a line ending that finishes whatever partial line the peer
# was holding, so the reset is a line of its own whatever came before it.
RESET_LINE = "\r\n@peer reset\r\n"
READY = b"@peer ready\r\n"
STATS = re.compile(
    r"@peer ok stats bytes=(?P<bytes>\d+) crc32=(?P<crc32>[0-9a-f]{8}) lines=(?P<lines>\d+) overlong=(?P<overlong>\d+) lost=(?P<lost>\d+)"
)

REPORT = ".agentic-hil/reports/last-report.json"
EVIDENCE = "bench-peer-evidence"
# A serial number no board carries, for the entry that names the wrong one.
NOT_THIS_BOARD = "NOT-THIS-BOARD-0001"

# The container's plans, step for step, with the bench's port in every step.
GREEN_PLAN = [
    {"action": "uart_open"},
    {"action": "uart_write", "text": "PING\r\n"},
    {"action": "uart_expect", "text": "PONG", "timeout_s": 5},
    {"action": "uart_write", "text": "VERSION\r\n"},
    {"action": "uart_read", "comparator": {"pattern": "^v(\\d+)\\.2\\.3", "range": {"min": 1, "max": 9}}, "timeout_s": 5},
    {"action": "uart_read"},
    {"action": "uart_close"},
]
# The claim the peer does not meet: its table answers `VERSION` with v1.2.3.
FAILING_PLAN = [
    {"action": "uart_open"},
    {"action": "uart_write", "text": "VERSION\r\n"},
    {"action": "uart_read", "comparator": {"equals": "v9.9.9"}, "timeout_s": 2},
]
SHAPES_PLAN = [
    {"action": "uart_open"},
    {"action": "uart_write", "text": "VERSION\r\n"},
    {"action": "uart_read", "comparator": {"pattern": "^v\\d+\\.\\d+\\.\\d+"}, "timeout_s": 5},
    {"action": "uart_write", "text": "VERSION\r\n"},
    {"action": "uart_read", "comparator": {"equals": "v1.2.3"}, "timeout_s": 5},
    {"action": "uart_write", "text": "COUNT\r\n"},
    {"action": "uart_read", "comparator": {"pattern": "COUNT=(\\d+)", "range": {"min": 40, "max": 50}}, "timeout_s": 5},
    {"action": "uart_close"},
]
RANGE_UNMET_PLAN = [
    {"action": "uart_open"},
    {"action": "uart_write", "text": "COUNT\r\n"},
    {"action": "uart_read", "comparator": {"pattern": "COUNT=(\\d+)", "range": {"min": 100, "max": 200}}, "timeout_s": 2},
]
EXPECT_TIMEOUT_PLAN = [
    {"action": "uart_open"},
    {"action": "uart_write", "text": "PING\r\n"},
    {"action": "uart_expect", "text": "NEVER", "timeout_s": 1.5},
]
V2_EXPECT_PLAN = [
    {"action": "uart_open"},
    {"action": "uart_expect", "pattern": "^v(\\d+)\\.\\d+\\.\\d+", "timeout_s": 5},
    {"action": "uart_close"},
]
# The one claim a pseudo-terminal cannot be asked: a number the board measured.
TEMPERATURE_PATTERN = "TEMP=(-?\\d+\\.\\d) RAW=\\d+"
TEMPERATURE_PLAN = [
    {"action": "uart_open"},
    {"action": "uart_write", "text": "TEMP?\r\n"},
    {"action": "uart_read", "comparator": {"pattern": TEMPERATURE_PATTERN, "range": {"min": -40, "max": 125}}, "timeout_s": 5},
    {"action": "uart_close"},
]
STEP_ROW = r"^\| (\d+) \| {port} \| (uart_\w+) \| (pass|fail) \| (\d+) \|$"


def actions(plan: list[dict]) -> list[str]:
    return [step["action"] for step in plan]


class Server:
    """One `agentic-hil mcp-stdio` child under a named configuration, driven as an agent host drives it.

    The same client as the one in `test_bench_serial.py`, with the
    configuration as a parameter: a server started against a copy reads that
    copy and nothing else, which is how a test gives the line another rate, a
    smaller buffer, another encoding or another identity without touching the
    file `init` wrote. Answers are pulled off stdout by a thread and handed
    over by id, so every request has a deadline; stderr goes to a file, which
    nothing can deadlock on.
    """

    def __init__(self, bench: Bench, config: Path, stderr_path: Path) -> None:
        self.config = config
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
            env=isolated_environment(bench.config_root, bench.state_root, AGENTIC_HIL_CONFIG=str(config)),
        )
        self._answers: queue.Queue[str | None] = queue.Queue()
        self._pump = threading.Thread(target=self._collect, daemon=True)
        self._pump.start()
        self._last_id = 0
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

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
        self._send({"jsonrpc": "2.0", "method": method})

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

    def call(self, name: str, arguments: dict | None = None, timeout_s: float = REPLY_TIMEOUT_S) -> dict:
        answered = self.request("tools/call", {"name": name, "arguments": arguments or {}}, timeout_s)
        assert "result" in answered, answered
        return answered["result"]

    def tool(self, name: str, **arguments: object) -> dict:
        """One tool call, answered with the tool's own document."""
        return tool_document(self.call(name, dict(arguments)))

    def close(self) -> int | None:
        """Close its input and let it end its sessions, or kill it when it will not."""
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
    """The tool's own document out of one MCP result, held to the `isError` flag beside it."""
    structured = envelope["structuredContent"]
    assert isinstance(structured, dict), envelope
    if structured.get("ok") is not True:
        assert envelope["isError"] is True, envelope
    return structured


def read_bytes_until(server: Server, port: str, wanted: bytes, timeout_s: float = READ_TIMEOUT_S) -> tuple[bytes, list[dict]]:
    """`com_read` until ``wanted`` has arrived, the way the container's twin polls its line.

    Every read is the product's own, with whatever is left of the deadline as
    its wait, and every read has to succeed.
    """
    received = b""
    reads: list[dict] = []
    deadline = time.monotonic() + timeout_s
    while wanted not in received:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        result = server.tool("com_read", port_id=port, wait_timeout_s=round(remaining, 3))
        reads.append(result)
        assert result["ok"] is True, result
        received += bytes.fromhex(result["data"]["hex"])
    return received, reads


def cli(bench: Bench, config: Path, *arguments: str) -> tuple[int, dict]:
    """One command's machine document, under a named configuration."""
    answered = subprocess.run(
        child_command(*arguments, "--json"),
        capture_output=True,
        text=True,
        cwd=str(bench.project),
        env=isolated_environment(bench.config_root, bench.state_root, AGENTIC_HIL_CONFIG=str(config)),
        timeout=COMMAND_TIMEOUT_S,
        check=False,
    )
    assert answered.stdout.strip(), f"{arguments} printed no document (exit {answered.returncode}):\n{answered.stderr}"
    return answered.returncode, json.loads(answered.stdout)


def quarantine_left(bench: Bench, config: Path) -> str | None:
    """Whatever the bench holds under one configuration, signed off through `recover`; why not, or None.

    Per configuration because a server started against a copy files its
    ownership state under a project key of its own, derived from the copy's
    path. Both questions are asked, `blocked` and `incident_stands`, because a
    bench can hold a standing incident without being blocked.
    """
    _, status = cli(bench, config, "lease-status")
    if not status.get("blocked") and not status.get("incident_stands"):
        return None
    quarantine = status.get("quarantine_id")
    if not isinstance(quarantine, str) or not quarantine:
        return f"the bench is not free and names no quarantine to clear: {status.get('cleanup_reasons')}"
    _, recovered = cli(bench, config, "recover", "--confirm-safe-state", "--quarantine-id", quarantine)
    if recovered.get("error_type") == "config_changed":
        _, recovered = cli(bench, config, "recover", "--confirm-safe-state", "--quarantine-id", quarantine, "--accept-config-change")
    if recovered.get("ok") is not True:
        return f"a quarantine this module raised could not be cleared: {recovered}"
    return None


@dataclass(frozen=True)
class Tally:
    """What reached the peer: how many payload bytes, and their CRC-32 as `zlib.crc32` spells it."""

    count: int
    crc32: str

    @classmethod
    def of(cls, data: bytes) -> Tally:
        return cls(len(data), format(zlib.crc32(data), "08x"))


class PeerSilent(AssertionError):
    """The peer did not answer a control line in time."""


class Servers:
    """MCP stdio servers for one test, the configurations they ran against, and the closing of both."""

    def __init__(self, bench: Bench, port: str, directory: Path) -> None:
        self.bench = bench
        self.port = port
        self.directory = directory
        self.started: list[Server] = []
        self.variants: list[Path] = []

    def __call__(self, config: Path | None = None) -> Server:
        server = Server(self.bench, config or self.bench.config, self.directory / f"mcp-stdio-{len(self.started)}.stderr")
        # Registered before the handshake, so a server that will not greet is
        # still a server the teardown closes.
        self.started.append(server)
        server.greet()
        return server

    def variant(self, name: str, **fields: object) -> Path:
        """This session's configuration with the port's entry changed, written outside the workspace.

        The authoritative file is read and never written. The copy lives under
        the session's configuration root because the product refuses a
        configuration stored inside the workspace it governs, and it reaches a
        server as an absolute `AGENTIC_HIL_CONFIG`, the documented override.
        """
        document = self.bench.configuration()
        document["com_ports"][self.port] = {**document["com_ports"][self.port], **fields}
        directory = self.bench.config_root / "serial-peer-configurations"
        directory.mkdir(parents=True, exist_ok=True)
        written = directory / f"{name}.yaml"
        written.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
        self.variants.append(written)
        return written

    def close_all(self) -> list[str]:
        """Close every server, the ones that will not end included; what went wrong."""
        problems: list[str] = []
        for server in reversed(self.started):
            try:
                server.close()
            except Exception as error:
                problems.append(f"an MCP server would not close: {error}")
        return problems


class Peer:
    """The board's peer image, set up and asked through the product's own serial tools.

    Every control exchange is one session on a server of its own (the control
    server, started when first needed): open, one `com_write` per control line,
    `com_read` until that line's answer, close. Nothing is left open, so the
    test's own sessions and plans find the line free. The control server itself
    is closed once the peer is set up, because a plan, a bridge or a permission
    change is a separate process that should meet no other server on the
    project; `received()` starts a new one.
    """

    def __init__(self, servers: Servers, port: str) -> None:
        self.servers = servers
        self.port = port
        self._control: Server | None = None

    def _server(self) -> Server:
        if self._control is None or self._control.closed:
            self._control = self.servers()
        return self._control

    def let_go(self) -> None:
        """Close the control server, so nothing but the test is on the project."""
        if self._control is not None:
            self._control.close()
            self._control = None

    def exchange(self, pairs: list[tuple[str, str]], server: Server | None = None) -> list[str]:
        """Each ``(wire, command)`` written in one session, and the answer line each got."""
        server = server or self._server()
        opened = server.tool("com_session_start", port_id=self.port, clear_buffer=True)
        assert opened["ok"] is True, opened
        answers: list[str] = []
        try:
            pending = b""
            for wire, command in pairs:
                written = server.tool("com_write", port_id=self.port, text=wire)
                assert written["ok"] is True, written
                answer, pending = self._answer(server, command, pending)
                answers.append(answer)
        except BaseException:
            with suppress(Exception):
                server.call("com_session_stop", {"port_id": self.port})
            raise
        stopped = server.tool("com_session_stop", port_id=self.port)
        assert stopped["ok"] is True, stopped
        return answers

    def _answer(self, server: Server, command: str, pending: bytes) -> tuple[str, bytes]:
        """The one line that answers ``command``, and what the line said after it.

        Searched for rather than expected first: a line that was quiet while
        closed may hand over what the peer said before the open, and an
        announcing peer interleaves its announcements. Decoded as Latin-1, which
        maps every byte to one character, so a match's offsets are byte offsets.
        """
        pattern = re.compile(rf"@peer (?:ok|error) {re.escape(command)}(?: [^\r\n]*)?\r\n|@peer error (?:unknown|overlong)\r\n")
        deadline = time.monotonic() + ANSWER_TIMEOUT_S
        while True:
            found = pattern.search(pending.decode("latin-1"))
            if found is not None:
                return found.group(0).rstrip("\r\n"), pending[found.end() :]
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise PeerSilent(f"the peer did not answer `@peer {command}` within {ANSWER_TIMEOUT_S}s; the line said {pending[-400:]!r}")
            read = server.tool("com_read", port_id=self.port, wait_timeout_s=round(remaining, 3))
            assert read["ok"] is True, read
            pending += bytes.fromhex(read["data"]["hex"])

    def control(self, *lines: str, server: Server | None = None) -> list[str]:
        return self.exchange([(f"@peer {line}\r\n", line.split(" ", 1)[0]) for line in lines], server)

    def configure(self, *lines: str) -> None:
        """Control lines the peer has to accept, and then out of the test's way."""
        answers = self.control(*lines)
        for line, answer in zip(lines, answers, strict=True):
            assert answer.startswith(f"@peer ok {line.split(' ', 1)[0]}"), (line, answer)
        self.let_go()

    def reset(self, server: Server | None = None) -> None:
        """The peer back at its boot defaults, with its statistics zeroed.

        Through the control server, at the boot rate. A peer that does not
        answer there is restarted through the probe and asked again; a caller
        that names its own server is speaking the peer's current rate, and
        silence there is the failure.
        """
        try:
            answers = self.exchange([(RESET_LINE, "reset")], server)
        except PeerSilent:
            if server is not None:
                raise
            self.restart()
            answers = self.exchange([(RESET_LINE, "reset")])
        assert answers == ["@peer ok reset"], answers

    def restart(self) -> None:
        """The board reset into run through the probe, and the peer's boot line read after it."""
        server = self._server()
        opened = server.tool("com_session_start", port_id=self.port, clear_buffer=True)
        assert opened["ok"] is True, opened
        try:
            restarted = server.tool("reset_target", mode="run")
            assert restarted["ok"] is True, restarted
            said, _reads = read_bytes_until(server, self.port, READY, BOOT_TIMEOUT_S)
        except BaseException:
            with suppress(Exception):
                server.call("com_session_stop", {"port_id": self.port})
            raise
        stopped = server.tool("com_session_stop", port_id=self.port)
        assert stopped["ok"] is True, stopped
        assert READY in said, f"the peer did not say it was ready within {BOOT_TIMEOUT_S}s of a reset through the probe: {said[-400:]!r}"

    def start_responder(self, *replies: str, delay_s: float = 0.0, announce: str | None = None, announce_every_s: float = 0.0) -> None:
        """The container's `start_responder`, on the board.

        Each reply is ``REQUEST=RESPONSE`` under Python's escape rules. No
        replies at all is a peer that listens, counts and never answers.
        ``announce`` is a line the peer writes on its own every
        ``announce_every_s``, and ``delay_s`` is the wait before each answer.
        """
        lines = [f"rule {reply}" for reply in replies]
        if delay_s:
            lines.append(f"delay {round(delay_s * 1000)}")
        if announce is not None:
            lines.append(f"announce {round(announce_every_s * 1000)} {announce}")
        if lines:
            self.configure(*lines)
        self.let_go()

    def received(self, server: Server | None = None) -> Tally:
        """What reached the peer since the test's reset, as the peer counted it.

        Asked only once the test's own session has ended, because the line is
        held exclusively. A peer that lost bytes to an overrun has no complete
        count to give, and says so here rather than in a mismatch.
        """
        answers = self.control("stats", server=server)
        found = STATS.fullmatch(answers[0])
        assert found is not None, answers
        assert int(found["lost"]) == 0, answers[0]
        return Tally(int(found["bytes"]), found["crc32"])


def run_plan(bench: Bench, port: str, name: str, steps: list[dict], *arguments: str, version: int = 3) -> subprocess.CompletedProcess[str]:
    """A plan written into the project under its own name, run by `test-reactor`, and removed again."""
    plan = bench.project / f"{name}.yaml"
    document = {"version": version, "name": name, "steps": [{"port_id": port, **step} for step in steps]}
    plan.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    try:
        return bench.run("test-reactor", "--test-config", plan.name, *arguments)
    finally:
        plan.unlink(missing_ok=True)


def last_report(bench: Bench) -> dict:
    path = bench.project / REPORT
    assert path.is_file(), f"no report at {path}"
    return json.loads(path.read_text(encoding="utf-8"))


def log_entries(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def written(bench: Bench, report: dict) -> bytes:
    """Every byte the plan's session wrote, from the session log its `uart_open` named."""
    entries = log_entries(bench.project / report["steps"][0]["result"]["session"]["log_path"])
    return b"".join(bytes.fromhex(entry["hex"]) for entry in entries if entry.get("direction") == "tx")


def halted_by_recovery(report: dict, port: str, peer: Peer) -> None:
    """The failed run's recovery as the report states it and as the board shows it, then the peer running again.

    A failed run ends in a recovery action: under the default policy, which
    the tier's configuration leaves in place, a reset into halt and a re-read
    of the probe. A halted peer answers nothing, and that silence is the
    board's side of the report's claim. The peer is then reset into run
    through the probe, so the next test meets it running.
    """
    recovery = report["recovery"]
    assert recovery["attempted"] is True, recovery
    assert recovery["outcome"] == "recovered", recovery
    assert recovery["devices"] == [port], recovery
    assert recovery["auto_recover_policy"] == "reset_halt", recovery
    assert "reset_halt" in recovery["actions"], recovery
    with pytest.raises(PeerSilent):
        peer.received()
    peer.restart()


def settled_at(server: Server, port: str, buffered: int, overflow: int) -> dict:
    """The port's status once the buffer holds ``buffered`` bytes over ``overflow`` dropped ones, or at the bound."""
    deadline = time.monotonic() + READ_TIMEOUT_S
    while True:
        listed = server.tool("com_ports_list")
        assert listed["ok"] is True, listed
        status = listed["ports"][port]
        if (status["rx_buffer_bytes"], status["overflow_bytes"]) == (buffered, overflow) or time.monotonic() > deadline:
            return status
        time.sleep(0.05)


@pytest.fixture(autouse=True)
def bench_is_left_clear(bench: Bench) -> Iterator[None]:
    """Whatever a test here quarantined under the session's configuration, cleared through `recover`."""
    yield
    left = quarantine_left(bench, bench.config)
    assert left is None, left


@pytest.fixture
def port(bench: Bench) -> str:
    return bench.com_port_name()


@pytest.fixture
def servers(bench: Bench, bench_is_left_clear: None, port: str, tmp_path: Path) -> Iterator[Servers]:
    """Servers for one test, closed afterwards, and the configurations they ran against cleared and removed.

    Every server is closed before anything is cleared, because a session still
    open is a device still held. A copy's quarantine is cleared under the copy,
    before the copy is removed.
    """
    started = Servers(bench, port, tmp_path)
    yield started
    problems = started.close_all()
    for variant in started.variants:
        left = quarantine_left(bench, variant)
        if left is not None:
            problems.append(left)
        variant.unlink(missing_ok=True)
    if problems:
        raise AssertionError(problems[0])


@pytest.fixture(scope="module")
def peer_image(board_image_builds: BoardImages) -> Iterator[None]:
    """The peer on the board for this module, and the demo back on it after the last test, pass or fail."""
    try:
        report = board_image_builds.put("peer")
        if report.get("ok") is not True:
            pytest.fail(f"the peer image could not be put on the board: {report.get('summary')}", pytrace=False)
        yield
    finally:
        board_image_builds.restore()


@pytest.fixture(autouse=True)
def peer(peer_image: None, servers: Servers, port: str) -> Peer:
    """The peer, reset to its boot defaults before the test, over its own control line."""
    answering = Peer(servers, port)
    answering.reset()
    return answering


@pytest.fixture
def write_permission_restored(bench: Bench, servers: Servers, port: str) -> Iterator[str]:
    """The dotted key for writing to the line, granted again whatever the test did to it.

    A permission does not move under a held bench, so every server is closed
    and any quarantine cleared before the grant; the grant runs even when
    either of those fails.
    """
    key = f"com_ports.{port}.permissions.allow_write"
    yield key
    problems = servers.close_all()
    left = quarantine_left(bench, bench.config)
    if left is not None:
        problems.append(left)
    granted = bench.run("grant", key)
    assert granted.returncode == 0, granted.stdout + granted.stderr
    if problems:
        raise AssertionError(problems[0])


# ---------------------------------------------------------------------------
# The open, on the board's own evidence.


def test_the_open_applies_the_configured_baudrate_and_the_stop_releases_the_exclusive_hold(servers: Servers, peer: Peer, port: str) -> None:
    """The two facts the container reads off the kernel, read off the peer instead.

    The peer is moved to 57600 baud, and a configuration that names 57600
    gets its answer, which it can only get if the open applied that rate to
    the line; the same stimulus at the boot rate is noise to it. After
    `com_session_stop` another process opens the line at once, while the
    first is still running, which is the exclusive hold having been released
    by the stop and not by the process ending.
    """
    peer.configure(f"rule {PING_PONG}", f"baud {OTHER_RATE}")
    config = servers.variant("baud-57600", baudrate=OTHER_RATE)

    first = servers(config)
    assert first.tool("com_ports_list")["ports"][port]["baudrate"] == OTHER_RATE
    started = first.tool("com_session_start", port_id=port)
    assert started["ok"] is True, started
    assert first.tool("com_write", port_id=port, text="PING\r\n")["ok"] is True
    answered, reads = read_bytes_until(first, port, b"PONG\r\n")
    assert answered == b"PONG\r\n", (answered, reads)

    stopped = first.tool("com_session_stop", port_id=port)
    assert stopped["ok"] is True, stopped
    assert stopped["was_active"] is True, stopped
    assert stopped["session"]["session_active"] is False, stopped

    second = servers(config)
    assert peer.received(server=second) == Tally.of(b"PING\r\n")

    at_the_boot_rate = servers()
    assert at_the_boot_rate.tool("com_session_start", port_id=port)["ok"] is True
    assert at_the_boot_rate.tool("com_write", port_id=port, text="PING\r\n")["ok"] is True
    heard, _reads = read_bytes_until(at_the_boot_rate, port, b"PONG", timeout_s=1.5)
    assert b"PONG" not in heard, heard
    assert at_the_boot_rate.tool("com_session_stop", port_id=port)["ok"] is True

    # Back to the boot rate, over the line that speaks the peer's current one.
    peer.reset(server=second)
    assert peer.received() == Tally.of(b"")


# ---------------------------------------------------------------------------
# A session, end to end, with the board answering.


def test_a_session_writes_a_stimulus_the_peer_receives_and_reads_its_answer(servers: Servers, peer: Peer, port: str) -> None:
    """`com_session_start`, `com_write`, `com_read`, `com_session_stop`, as documents.

    The stimulus is asserted at both ends: what `com_write` says it wrote and
    what the peer counted. The one difference from the container is the
    identity: a pseudo-terminal declares no hardware and opens `not_declared`,
    while this bench's entry names its board and opens only `confirmed`.
    """
    peer.start_responder(PING_PONG)
    server = servers()

    started = server.tool("com_session_start", port_id=port)
    assert started["ok"] is True, started
    assert started["already_active"] is False, started
    assert started["session"]["session_active"] is True, started
    assert started["identity"]["status"] == "confirmed", started["identity"]
    assert started["summary"] == "COM port session started.", started

    again = server.tool("com_session_start", port_id=port)
    assert again["ok"] is True and again["already_active"] is True, again

    written = server.tool("com_write", port_id=port, text="PING\r\n")
    assert written["ok"] is True, written
    assert written["bytes_written"] == 6, written
    assert written["data"] == {"hex": b"PING\r\n".hex(), "text": "PING\r\n", "encoding": "utf-8"}, written

    received, reads = read_bytes_until(server, port, b"PONG\r\n")
    assert received == b"PONG\r\n", (received, reads)
    for result in reads:
        assert result["data"]["encoding"] == "utf-8", result
        assert result["summary"] == "Feedback read from COM port.", result
    assert reads[-1]["buffer_remaining_bytes"] == 0, reads[-1]
    assert reads[-1]["overflow_bytes"] == 0, reads[-1]

    stopped = server.tool("com_session_stop", port_id=port)
    assert stopped["ok"] is True and stopped["was_active"] is True, stopped

    after = server.tool("com_read", port_id=port)
    assert after["ok"] is False, after
    assert after["error_type"] == "session_not_active", after

    assert peer.received() == Tally.of(b"PING\r\n")


def test_a_read_against_a_silent_peer_waits_its_timeout_and_returns_no_bytes(servers: Servers, peer: Peer, port: str) -> None:
    """The peer counts and never answers, so the read has nothing to return.

    A `com_read` with `wait_timeout_s` on a quiet line takes at least that
    long and comes back `ok` with no bytes, the quiet "no feedback" and not a
    failure. The stimulus did reach the board, so the silence is the peer's
    and not a write that never went.
    """
    peer.start_responder()
    server = servers()
    assert server.tool("com_session_start", port_id=port)["ok"] is True
    assert server.tool("com_write", port_id=port, text="PING\r\n")["ok"] is True

    before = time.monotonic()
    read = server.tool("com_read", port_id=port, wait_timeout_s=1.5)
    waited = time.monotonic() - before

    assert read["ok"] is True, read
    assert read["bytes_read"] == 0, read
    assert read["data"] == {"hex": "", "text": "", "encoding": "utf-8"}, read
    assert read["summary"] == "No COM port feedback was available.", read
    assert waited >= 1.5, f"the read came back after {waited:.2f}s on a wait of 1.5s"
    assert "reader_error" not in read, read

    assert server.tool("com_session_stop", port_id=port)["ok"] is True
    assert peer.received() == Tally.of(b"PING\r\n")


# ---------------------------------------------------------------------------
# The write permission, moved by the operator's commands.


def test_the_write_is_refused_after_revoke_and_accepted_again_after_grant(bench: Bench, write_permission_restored: str, servers: Servers, peer: Peer, port: str) -> None:
    """`agentic-hil revoke`, a server that meets the closed key, `agentic-hil grant`, a server that does not.

    `test_bench_serial.py` proves the refusal's shape against the demo, which
    reads nothing; what the board adds is the wire. Nothing reached it while
    the refusal stood, and the stimulus after the grant is answered.
    """
    key = write_permission_restored
    short = f"com_ports.{port}.allow_write"
    peer.start_responder(PING_PONG)

    code, revocation = bench.document("revoke", short)
    assert code == 0, revocation
    assert revocation["ok"] is True, revocation
    assert revocation["changed"] == [{"key": key, "previous_value": True, "value": False}], revocation
    assert revocation["restart_required"] is True, revocation

    server = servers()
    assert server.tool("com_session_start", port_id=port)["ok"] is True
    refused = server.tool("com_write", port_id=port, text="PING\r\n")
    assert refused["ok"] is False, refused
    assert refused["error_type"] == "permission_denied", refused
    assert refused["permission"] == key, refused
    assert f"The permission is `{key}` and it is false." in refused["summary"], refused
    assert f"agentic-hil grant {key}" in refused["next_step"], refused
    assert server.tool("com_session_stop", port_id=port)["ok"] is True
    server.close()

    code, grant = bench.document("grant", short)
    assert code == 0, grant
    assert grant["ok"] is True, grant
    assert grant["changed"] == [{"key": key, "previous_value": False, "value": True}], grant

    # Refused before the line: the board saw nothing.
    assert peer.received() == Tally.of(b"")

    server = servers()
    assert server.tool("com_session_start", port_id=port)["ok"] is True
    written = server.tool("com_write", port_id=port, text="PING\r\n")
    assert written["ok"] is True, written
    received, _reads = read_bytes_until(server, port, b"PONG\r\n")
    assert received == b"PONG\r\n", received
    assert server.tool("com_session_stop", port_id=port)["ok"] is True
    assert peer.received() == Tally.of(b"PING\r\n")


# ---------------------------------------------------------------------------
# The stdio bridge.


def test_com_stdio_relays_stdin_to_the_port_and_the_peers_answer_to_stdout(bench: Bench, peer: Peer, port: str) -> None:
    """A line in on stdin reaches the board; the board's answer comes out on stdout, and nothing on stderr.

    The twin of `tests/container/test_com_stdio_on_a_pty.py`, whose wire
    answers `AT` with `OK`, and of the relay in
    `tests/container/test_serial_over_pty.py`. The far end is a board here,
    and what reached it is the peer's own count, taken once the bridge has
    let the line go.
    """
    peer.start_responder(AT_OK)

    bridged = subprocess.run(
        child_command("com-stdio", "--port", port, "--eof-idle-timeout-s", str(EOF_IDLE_S)),
        input=b"AT\r\n",
        capture_output=True,
        cwd=str(bench.project),
        env=bench.environment(),
        timeout=COMMAND_TIMEOUT_S,
        check=False,
    )

    assert bridged.returncode == 0, bridged.stderr.decode("utf-8", errors="replace")
    assert bridged.stderr == b"", bridged.stderr
    assert b"OK\r\n" in bridged.stdout, bridged.stdout
    assert peer.received() == Tally.of(b"AT\r\n")


# ---------------------------------------------------------------------------
# The refusals a board can stage.


def test_a_second_session_on_the_same_configuration_is_refused_while_the_first_holds_the_port(servers: Servers, peer: Peer, port: str) -> None:
    """Two servers on one configuration: the second `com_session_start` is refused and the first keeps the line.

    The refusal is the coordinator's `resource_busy`, the first session is
    untouched by it, and the board saw only the first session's bytes.
    """
    peer.start_responder(PING_PONG)
    first = servers()
    second = servers()
    assert first.tool("com_session_start", port_id=port)["ok"] is True

    refused = second.tool("com_session_start", port_id=port)
    assert refused["ok"] is False, refused
    assert refused["error_type"] == "resource_busy", refused
    assert refused.get("side_effect_committed") is not True, refused

    assert first.tool("com_write", port_id=port, text="PING\r\n")["ok"] is True
    received, _reads = read_bytes_until(first, port, b"PONG\r\n")
    assert received == b"PONG\r\n", received
    assert first.tool("com_session_stop", port_id=port)["was_active"] is True
    assert peer.received() == Tally.of(b"PING\r\n")


@pytest.mark.parametrize("claim", ["serial_number", "vid_pid"])
def test_an_entry_that_names_other_hardware_than_the_real_port_is_refused_before_the_open(servers: Servers, peer: Peer, port: str, claim: str) -> None:
    """`com_port_identity_mismatch` at the real device, the way TROUBLESHOOTING section 11a and the skill describe it.

    The entry names the bench's own device, and either a serial number no
    board carries or vendor and product ids the adapter does not report. The
    refusal carries both sides of the comparison, `expected_*` from the
    configuration and `found_*` from the host, the latter equal to what the
    same device reported when it was confirmed a moment earlier. The named
    board is attached nowhere, so there is no `expected_device`. Nothing was
    opened and nothing was written: the board counted no byte.

    What is asserted is the shape and the agreement between the product's own
    answers, never a serial or an id this bench happens to have.
    """
    peer.start_responder(PING_PONG)
    base = servers()
    opened = base.tool("com_session_start", port_id=port)
    assert opened["ok"] is True, opened
    confirmed = opened["identity"]
    assert confirmed["status"] == "confirmed", confirmed
    for field in ("device", "expected_serial_number", "expected_from", "found_serial_number"):
        assert isinstance(confirmed.get(field), str) and confirmed[field], (field, confirmed)
    for field in ("expected_vid", "expected_pid", "found_vid", "found_pid"):
        assert isinstance(confirmed.get(field), int), (field, confirmed)
    assert base.tool("com_session_stop", port_id=port)["ok"] is True
    base.close()

    if claim == "serial_number":
        changes: dict[str, object] = {"serial_number": NOT_THIS_BOARD}
        expected = {
            "expected_serial_number": NOT_THIS_BOARD,
            "expected_from": f"com_ports.{port}.serial_number",
            "expected_vid": confirmed["expected_vid"],
            "expected_pid": confirmed["expected_pid"],
        }
    else:
        changes = {"vid": (confirmed["found_vid"] + 1) % 0x10000, "pid": (confirmed["found_pid"] + 1) % 0x10000}
        expected = {
            "expected_serial_number": confirmed["expected_serial_number"],
            "expected_from": confirmed["expected_from"],
            "expected_vid": changes["vid"],
            "expected_pid": changes["pid"],
        }
    found = {field: confirmed[field] for field in ("found_serial_number", "found_vid", "found_pid")}

    server = servers(servers.variant(f"identity-{claim}", **changes))
    entry = server.tool("com_ports_list")["ports"][port]
    for field in changes:
        assert field in entry, entry

    refused = server.tool("com_session_start", port_id=port)
    assert refused["ok"] is False, refused
    assert refused["error_type"] == "com_port_identity_mismatch", refused
    assert refused["identity"]["status"] == "mismatch", refused
    assert refused["identity"]["device"] == refused["configured_device"] == entry["device"] == confirmed["device"], refused
    for field, value in {**expected, **found}.items():
        assert refused[field] == value, (field, refused)
        assert refused["identity"][field] == value, (field, refused)
    assert refused["identity"].get("stable_device") == confirmed.get("stable_device"), refused
    assert "expected_device" not in refused, refused
    assert isinstance(refused["summary"], str) and refused["summary"], refused
    assert isinstance(refused["likely_causes"], list) and refused["likely_causes"], refused
    assert refused["side_effect_committed"] is False, refused
    assert refused["side_effect_status"] == "not_started", refused
    assert refused["hardware_state"] == "unchanged", refused
    assert refused["retry_safe"] is True, refused

    written = server.tool("com_write", port_id=port, text="PING\r\n")
    assert written["ok"] is False and written["error_type"] == "session_not_active", written
    assert server.tool("com_ports_list")["ports"][port]["session_active"] is False
    assert peer.received() == Tally.of(b"")


def test_a_port_the_configuration_does_not_declare_is_refused_by_every_tool_with_the_declared_ones_named(bench: Bench, servers: Servers, peer: Peer, port: str) -> None:
    """`com_port_not_configured`, from `com_session_start`, `com_write`, `com_read` and `com_session_stop` alike.

    `test_bench_serial.py` asks `com_read` alone; this asks all four, and the
    board says the write that named no port put nothing on the line.
    """
    server = servers()
    for tool, arguments in (
        ("com_session_start", {"port_id": "ghost"}),
        ("com_write", {"port_id": "ghost", "text": "PING\r\n"}),
        ("com_read", {"port_id": "ghost"}),
        ("com_session_stop", {"port_id": "ghost"}),
    ):
        refused = server.tool(tool, **arguments)
        assert refused["ok"] is False, (tool, refused)
        assert refused["error_type"] == "com_port_not_configured", (tool, refused)
        assert refused["port_id"] == "ghost", (tool, refused)
        assert refused["configured_ports"] == sorted(bench.configuration()["com_ports"]), (tool, refused)
        assert refused.get("side_effect_committed") is not True, (tool, refused)
    assert peer.received() == Tally.of(b"")


# ---------------------------------------------------------------------------
# A plan through the real test-reactor, and the evidence over its report.


def test_a_uart_plan_runs_green_against_the_peer_and_run_evidence_reads_its_report(bench: Bench, peer: Peer, port: str) -> None:
    """`uart_open`, `uart_write`, `uart_expect`, `uart_read` with and without a claim, `uart_close`, then `run-evidence`.

    Every step row of the evidence carries its elapsed time, the configuration
    digest is spelled with its prefix exactly once, the summary names the
    plan's one port, and the session log the evidence copied is the one the
    plan's open named. The report is read before the peer is asked anything,
    because every COM call writes the last report.
    """
    peer.start_responder(PING_PONG, VERSION_LINE)

    ran = run_plan(bench, port, "peer-round-trip", GREEN_PLAN, "--json")

    assert ran.returncode == 0, ran.stdout + ran.stderr
    result = json.loads(ran.stdout)
    assert result["ok"] is True, result
    assert [step["action"] for step in result["steps"]] == actions(GREEN_PLAN), result["steps"]
    for step in result["steps"]:
        assert step["result"]["ok"] is True, step
        assert isinstance(step["elapsed_ms"], int) and step["elapsed_ms"] >= 0, step
    expect = result["steps"][2]["result"]
    assert expect["expected_text"] == "PONG" and expect["bytes_received"] >= len(b"PONG\r\n"), expect
    claim = result["steps"][4]["result"]
    assert claim["ok"] is True and claim["bytes_received"] >= len(b"v1.2.3\r\n"), claim
    log_path = result["steps"][0]["result"]["session"]["log_path"]

    report = last_report(bench)
    assert report["ok"] is True and report["name"] == "peer-round-trip", report
    assert [step["action"] for step in report["steps"]] == actions(GREEN_PLAN)

    out = bench.project / EVIDENCE
    shutil.rmtree(out, ignore_errors=True)
    try:
        code, evidence = bench.document("run-evidence", "--report", REPORT, "--out", EVIDENCE)
        assert code == 0, evidence
        assert evidence["ok"] is True, evidence

        summary = json.loads((out / "run-summary.json").read_text(encoding="utf-8"))
        assert summary["outcome"] == "success", summary
        assert summary["plan"]["name"] == "peer-round-trip", summary
        assert summary["bench"]["devices"]["com_ports"] == [port], summary
        assert summary["bench"]["config_digest"].startswith("sha256:"), summary

        document = (out / "job-summary.md").read_text(encoding="utf-8")
        assert "sha256sha256" not in document
        digest_rows = [line for line in document.splitlines() if line.startswith("| Configuration digest |")]
        assert len(digest_rows) == 1, document
        assert digest_rows[0].count("sha256:") == 1, digest_rows[0]
        assert f"| `{summary['bench']['config_digest']}` |" in digest_rows[0], digest_rows[0]
        step_row = re.compile(STEP_ROW.format(port=re.escape(port)))
        rows = [match for match in (step_row.match(line) for line in document.splitlines()) if match is not None]
        assert [match.group(2) for match in rows] == actions(GREEN_PLAN), document
        assert all(match.group(3) == "pass" for match in rows), document
        assert "|  |" not in document, document

        copied_logs = list(out.rglob(f"com-*-{port}.jsonl"))
        assert len(copied_logs) == 1, sorted(str(path) for path in out.rglob("*"))
        assert copied_logs[0].read_bytes() == (bench.project / log_path).read_bytes()
    finally:
        shutil.rmtree(out, ignore_errors=True)

    assert peer.received() == Tally.of(b"PING\r\nVERSION\r\n")


def test_a_plan_whose_claim_the_peer_does_not_meet_is_headed_failed(bench: Bench, peer: Peer, port: str) -> None:
    """The red run a bench exists to produce, headed by its outcome.

    The port answered, the answer was not the claimed one, and the rendering's
    first line says `Failed:` and the comparator's own error type, never the
    word reserved for a call that never happened. What the port did say is in
    the rendering. The run's recovery halted the board, as a failed run's does.
    """
    peer.start_responder(VERSION_LINE)

    ran = run_plan(bench, port, "peer-wrong-version", FAILING_PLAN)

    rendered = ran.stdout
    assert ran.returncode == 1, rendered + ran.stderr
    first_line = rendered.splitlines()[0] if rendered else ""
    assert first_line.startswith("Failed: comparator_unmet"), rendered
    assert not rendered.startswith("Refused"), rendered
    assert "v1.2.3" in rendered, rendered

    report = last_report(bench)
    assert report["ok"] is False, report
    assert [step["action"] for step in report["steps"]] == actions(FAILING_PLAN), report["steps"]
    failed = report["steps"][2]["result"]
    assert failed["error_type"] == "comparator_unmet", failed
    assert "v1.2.3" in json.dumps(failed), failed

    assert written(bench, report) == b"VERSION\r\n"
    halted_by_recovery(report, port, peer)


# ---------------------------------------------------------------------------
# The event log the session writes.


def test_the_session_writes_an_event_log_in_the_order_things_happened(bench: Bench, servers: Servers, peer: Peer, port: str) -> None:
    """One file per session under the workspace logs: start, tx, rx, stop, each stamped.

    The stimulus entry is ahead of the answer it provoked, and on this line
    the answer really was provoked by it. The file `com_session_start` named
    is the file, and the agent-initiated lines are mirrored into the trusted
    ledger under the state root. The workspace holds other sessions' logs by
    now (the peer's own control sessions among them), so the file is the one
    the session named rather than the only one there.
    """
    peer.start_responder(PING_PONG)
    server = servers()
    device = server.tool("com_ports_list")["ports"][port]["device"]

    started = server.tool("com_session_start", port_id=port)
    assert started["ok"] is True, started
    named = started["session"]["log_path"]
    assert server.tool("com_write", port_id=port, text="PING\r\n")["ok"] is True
    received, _reads = read_bytes_until(server, port, b"PONG\r\n")
    assert received == b"PONG\r\n", received
    assert server.tool("com_session_stop", port_id=port)["ok"] is True

    log = bench.project / named
    assert log.parent.resolve() == (bench.project / ".agentic-hil" / "logs").resolve(), named
    assert log.name.startswith("com-") and log.name.endswith(f"-{port}.jsonl"), named

    entries = log_entries(log)
    assert all("time" in entry for entry in entries), entries
    assert entries[0]["event"] == "start", entries[0]
    assert entries[0]["port_id"] == port and entries[0]["device"] == device, entries[0]
    assert entries[-1] == {**entries[-1], "event": "stop", "reason": "requested"}, entries[-1]
    kinds = [entry.get("event") or entry.get("direction") for entry in entries]
    assert kinds[0] == "start" and kinds[-1] == "stop", kinds
    assert "tx" in kinds and "rx" in kinds, kinds
    assert kinds.index("tx") < kinds.index("rx"), kinds
    sent = [entry for entry in entries if entry.get("direction") == "tx"]
    assert [entry["hex"] for entry in sent] == [b"PING\r\n".hex()], sent
    assert sent[0]["text"] == "PING\r\n" and sent[0]["bytes"] == 6, sent[0]
    answered = b"".join(bytes.fromhex(entry["hex"]) for entry in entries if entry.get("direction") == "rx")
    assert answered == b"PONG\r\n", answered

    mirrored = list(bench.state_root.rglob(f"audit-logs/{log.name}"))
    assert len(mirrored) == 1, mirrored
    ledger_kinds = [entry.get("event") or entry.get("direction") for entry in log_entries(mirrored[0])]
    assert ledger_kinds[0] == "start" and "tx" in ledger_kinds and ledger_kinds[-1] == "stop", ledger_kinds

    assert peer.received() == Tally.of(b"PING\r\n")


# ---------------------------------------------------------------------------
# The receive buffer, and the encodings.


def test_the_receive_buffer_keeps_the_newest_bytes_up_to_its_size_and_a_second_start_clears_it_unless_told_not_to(servers: Servers, peer: Peer, port: str) -> None:
    """`max_buffer_bytes`, `overflow_bytes`, `buffer_remaining_bytes`, and `clear_buffer` on an already active session.

    The board answers with more than the buffer holds, so the oldest bytes are
    dropped and counted as overflow and what is kept is the newest. A read of
    part of it leaves the rest, counted. A second `com_session_start` on the
    active session clears both the buffer and the count by default and keeps
    both when told `clear_buffer: false`.
    """
    peer.start_responder(FLOOD_RULE)
    config = servers.variant("buffer-64", max_buffer_bytes=BUFFER_BYTES)
    flood = (FLOOD_LINE + "\r\n").encode("ascii")
    kept = flood[-BUFFER_BYTES:]
    overflow = len(flood) - BUFFER_BYTES

    server = servers(config)
    assert server.tool("com_ports_list")["ports"][port]["max_buffer_bytes"] == BUFFER_BYTES
    assert server.tool("com_session_start", port_id=port)["ok"] is True
    assert server.tool("com_write", port_id=port, text="FLOOD\r\n")["ok"] is True
    status = settled_at(server, port, BUFFER_BYTES, overflow)
    assert (status["rx_buffer_bytes"], status["overflow_bytes"]) == (BUFFER_BYTES, overflow), status

    part = server.tool("com_read", port_id=port, max_bytes=16)
    assert part["ok"] is True, part
    assert part["bytes_read"] == 16, part
    assert bytes.fromhex(part["data"]["hex"]) == kept[:16], part
    assert part["buffer_remaining_bytes"] == BUFFER_BYTES - 16, part
    assert part["overflow_bytes"] == overflow, part

    cleared = server.tool("com_session_start", port_id=port)
    assert cleared["ok"] is True and cleared["already_active"] is True, cleared
    assert cleared["session"]["rx_buffer_bytes"] == 0, cleared
    assert cleared["session"]["overflow_bytes"] == 0, cleared
    empty = server.tool("com_read", port_id=port)
    assert (empty["bytes_read"], empty["buffer_remaining_bytes"], empty["overflow_bytes"]) == (0, 0, 0), empty

    assert server.tool("com_write", port_id=port, text="FLOOD\r\n")["ok"] is True
    status = settled_at(server, port, BUFFER_BYTES, overflow)
    assert (status["rx_buffer_bytes"], status["overflow_bytes"]) == (BUFFER_BYTES, overflow), status
    untouched = server.tool("com_session_start", port_id=port, clear_buffer=False)
    assert untouched["ok"] is True and untouched["already_active"] is True, untouched
    assert untouched["session"]["rx_buffer_bytes"] == BUFFER_BYTES, untouched
    assert untouched["session"]["overflow_bytes"] == overflow, untouched
    whole = server.tool("com_read", port_id=port)
    assert whole["bytes_read"] == BUFFER_BYTES and whole["buffer_remaining_bytes"] == 0, whole
    assert bytes.fromhex(whole["data"]["hex"]) == kept, whole

    assert server.tool("com_session_stop", port_id=port)["ok"] is True
    assert peer.received() == Tally.of(b"FLOOD\r\nFLOOD\r\n")


def test_a_configured_encoding_is_applied_to_what_is_written_and_to_what_is_read(bench: Bench, servers: Servers, peer: Peer, port: str) -> None:
    """`encoding: latin-1` on the entry decides the bytes of a text write and the text of a read.

    The stimulus's u-umlaut and sharp s go out as one byte each and the board
    counted exactly those bytes; its answer carries a byte that is a capital
    U-umlaut in Latin-1 and nothing in UTF-8, and the read decodes it under
    the configured encoding. A text the encoding cannot carry (the euro sign)
    is refused before the line, with the encoding named and nothing on the
    wire.
    """
    peer.start_responder(LATIN_RULE)
    config = servers.variant("latin-1", encoding="latin-1")
    stimulus = "Gr\xfc\xdfe\r\n"
    on_the_wire = stimulus.encode("latin-1")

    server = servers(config)
    assert server.tool("com_ports_list")["ports"][port]["encoding"] == "latin-1"
    started = server.tool("com_session_start", port_id=port)
    assert started["ok"] is True, started

    written = server.tool("com_write", port_id=port, text=stimulus)
    assert written["ok"] is True, written
    assert written["bytes_written"] == len(on_the_wire) == 7, written
    assert written["data"] == {"hex": on_the_wire.hex(), "text": stimulus, "encoding": "latin-1"}, written

    received, reads = read_bytes_until(server, port, b"\xdcber\r\n")
    assert received == b"\xdcber\r\n", (received, reads)
    assert "".join(read["data"]["text"] for read in reads) == "\xdcber\r\n", reads
    assert all(read["data"]["encoding"] == "latin-1" for read in reads), reads

    refused = server.tool("com_write", port_id=port, text="\N{EURO SIGN}\r\n")
    assert refused["ok"] is False, refused
    assert refused["error_type"] == "invalid_argument", refused
    assert refused["encoding"] == "latin-1", refused
    assert "cannot be encoded" in refused["summary"], refused
    assert server.tool("com_session_stop", port_id=port)["ok"] is True

    assert peer.received() == Tally.of(on_the_wire)
    entries = log_entries(bench.project / started["session"]["log_path"])
    assert [entry["text"] for entry in entries if entry.get("direction") == "tx"] == [stimulus], entries
    assert "".join(entry["text"] for entry in entries if entry.get("direction") == "rx") == "\xdcber\r\n", entries


def test_bytes_that_do_not_decode_are_reported_as_hex_with_replacement_characters_in_the_text(servers: Servers, peer: Peer, port: str) -> None:
    """A `hex` write and an answer that is not UTF-8, on a UTF-8 port.

    The bytes go out exactly as given and come back exactly as sent: `hex` is
    the wire, and `text` is a best-effort decoding in which each byte that is
    not UTF-8 is a replacement character rather than an exception or a
    dropped byte.
    """
    peer.start_responder(RAW_RULE)
    server = servers()
    assert server.tool("com_session_start", port_id=port)["ok"] is True

    written = server.tool("com_write", port_id=port, hex="ff fe 00 52 41 57 0d 0a")
    assert written["ok"] is True, written
    assert written["bytes_written"] == 8, written
    assert written["data"] == {"hex": "fffe005241570d0a", "text": REPLACED * 2 + "\x00RAW\r\n", "encoding": "utf-8"}, written

    received, reads = read_bytes_until(server, port, b"PONG\r\n")
    assert received == b"\xff\xfePONG\r\n", (received, reads)
    assert "".join(read["data"]["text"] for read in reads) == REPLACED * 2 + "PONG\r\n", reads
    assert all(read["data"]["encoding"] == "utf-8" for read in reads), reads

    assert server.tool("com_session_stop", port_id=port)["ok"] is True
    assert peer.received() == Tally.of(bytes.fromhex("fffe005241570d0a"))


# ---------------------------------------------------------------------------
# The other shapes a plan's claim takes, and the ones it does not meet.


def test_a_pattern_an_equals_and_a_range_claim_are_each_met_by_the_line_they_describe(bench: Bench, peer: Peer, port: str) -> None:
    """The three claim shapes, green, each reporting the text that met it."""
    peer.start_responder(VERSION_LINE, COUNT_LINE)

    ran = run_plan(bench, port, "peer-claim-shapes", SHAPES_PLAN, "--json")

    assert ran.returncode == 0, ran.stdout + ran.stderr
    result = json.loads(ran.stdout)
    assert result["ok"] is True, result
    assert [step["action"] for step in result["steps"]] == actions(SHAPES_PLAN), result["steps"]
    assert all(step["result"]["ok"] is True for step in result["steps"]), result["steps"]

    by_pattern = result["steps"][2]["result"]
    assert by_pattern["comparator"] == {"pattern": "^v\\d+\\.\\d+\\.\\d+"}, by_pattern
    assert by_pattern["matched_text"]["text"] == "v1.2.3", by_pattern
    assert by_pattern["summary"] == "Expected pattern matched the COM port output.", by_pattern

    by_equals = result["steps"][4]["result"]
    assert by_equals["comparator"] == {"equals": "v1.2.3"}, by_equals
    assert by_equals["matched_text"]["text"].strip() == "v1.2.3", by_equals
    assert by_equals["summary"] == "The COM port output equalled the expected value.", by_equals

    by_range = result["steps"][6]["result"]
    assert by_range["comparator"] == {"pattern": "COUNT=(\\d+)", "range": {"min": 40, "max": 50}}, by_range
    assert by_range["captured_text"] == "42" and by_range["captured_value"] == 42.0, by_range
    assert by_range["matched_text"]["text"] == "COUNT=42", by_range
    assert by_range["summary"] == "A value captured from the COM port output fell inside the expected range.", by_range

    assert peer.received() == Tally.of(b"VERSION\r\nVERSION\r\nCOUNT\r\n")


def test_a_range_claim_the_captured_value_falls_outside_is_headed_failed_with_the_value_it_did_capture(bench: Bench, peer: Peer, port: str) -> None:
    """`comparator_unmet` on a range: the number was read and was out of bounds, and the report says which number."""
    peer.start_responder(COUNT_LINE)

    ran = run_plan(bench, port, "peer-range-unmet", RANGE_UNMET_PLAN)

    rendered = ran.stdout
    assert ran.returncode == 1, rendered + ran.stderr
    assert rendered.splitlines()[0].startswith("Failed: comparator_unmet"), rendered
    assert "42" in rendered, rendered

    report = last_report(bench)
    assert report["ok"] is False, report
    failed = report["steps"][2]["result"]
    assert failed["error_type"] == "comparator_unmet", failed
    assert failed["captured_text"] == "42" and failed["captured_value"] == 42.0, failed
    assert failed["received_tail"]["text"].endswith("COUNT=42\r\n"), failed
    assert failed["summary"] == "No value captured from the COM port output fell inside the expected range before this step's timeout.", failed

    assert written(bench, report) == b"COUNT\r\n"
    halted_by_recovery(report, port, peer)


def test_an_expectation_the_line_never_meets_is_a_failed_step_that_waited_its_timeout_and_quotes_the_line(bench: Bench, peer: Peer, port: str) -> None:
    """`uart_expect_timeout`: the board answered something else, the step failed after its timeout, and the answer is in the report."""
    peer.start_responder(PING_PONG)

    ran = run_plan(bench, port, "peer-expect-timeout", EXPECT_TIMEOUT_PLAN)

    rendered = ran.stdout
    assert ran.returncode == 1, rendered + ran.stderr
    assert rendered.splitlines()[0].startswith("Failed: uart_expect_timeout"), rendered
    assert "PONG" in rendered, rendered

    report = last_report(bench)
    assert report["ok"] is False, report
    assert [step["action"] for step in report["steps"]] == actions(EXPECT_TIMEOUT_PLAN), report["steps"]
    step = report["steps"][2]
    assert step["elapsed_ms"] >= 1500, step
    failed = step["result"]
    assert failed["error_type"] == "uart_expect_timeout", failed
    assert failed["expected_text"] == "NEVER" and failed["timeout_s"] == 1.5, failed
    assert failed["received_tail"]["text"] == "PONG\r\n", failed
    assert failed["received_tail_truncated"] is False, failed
    assert failed["summary"] == "Expected text did not appear on the COM port before this step's timeout.", failed

    assert written(bench, report) == b"PING\r\n"
    halted_by_recovery(report, port, peer)


def test_a_version_2_plan_waits_for_a_pattern_the_line_says_on_its_own(bench: Bench, peer: Peer, port: str) -> None:
    """The version 2 `uart_expect` with `pattern`, against a board that talks unprompted.

    Version 2 has no write step, so the peer announces its line on its own
    every so often, and the plan waits for the next announcement. The plan
    wrote nothing, and the board counted nothing.
    """
    peer.start_responder(announce="v1.2.3\\r\\n", announce_every_s=0.2)

    ran = run_plan(bench, port, "peer-v2-expect", V2_EXPECT_PLAN, "--json", version=2)

    assert ran.returncode == 0, ran.stdout + ran.stderr
    result = json.loads(ran.stdout)
    assert result["ok"] is True, result
    assert [step["action"] for step in result["steps"]] == actions(V2_EXPECT_PLAN), result["steps"]
    expect = result["steps"][1]["result"]
    assert expect["ok"] is True, expect
    assert expect["expected_pattern"] == "^v(\\d+)\\.\\d+\\.\\d+", expect
    assert expect["bytes_received"] >= len(b"v1.2.3\r\n"), expect
    assert expect["summary"] == "Expected pattern matched the COM port output.", expect

    assert peer.received() == Tally.of(b"")


def test_a_range_claim_holds_the_boards_own_temperature_reading_to_its_bounds(bench: Bench, peer: Peer, port: str) -> None:
    """A range claim over a number the board measured, which no pseudo-terminal can be asked.

    The peer answers `TEMP?` with its die temperature and the ADC reading it
    came from. The claim is met, the captured value is the number in the line,
    and that number is the reference manual's formula applied to the reading
    the same line carries, to the tenth the peer rounds to.
    """
    peer.configure("temp TEMP?")

    ran = run_plan(bench, port, "peer-temperature", TEMPERATURE_PLAN, "--json")

    assert ran.returncode == 0, ran.stdout + ran.stderr
    result = json.loads(ran.stdout)
    assert result["ok"] is True, result
    claim = result["steps"][2]["result"]
    assert claim["comparator"] == {"pattern": TEMPERATURE_PATTERN, "range": {"min": -40, "max": 125}}, claim
    line = re.fullmatch(r"TEMP=(-?\d+\.\d) RAW=(\d+)", claim["matched_text"]["text"])
    assert line is not None and line.group(1) == claim["captured_text"], claim
    assert claim["captured_value"] == float(claim["captured_text"]), claim
    formula = (int(line.group(2)) * 3.3 / 4095 - 0.76) / 0.0025 + 25
    assert abs(claim["captured_value"] - formula) <= 0.06, (claim, formula)

    assert peer.received() == Tally.of(b"TEMP?\r\n")
