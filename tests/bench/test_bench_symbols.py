"""Reading the board through its own symbols, and the four gates a read passes.

A symbol read is the one debug tool that carries target memory out of the bench,
so it is the one that has four separate things to be right about at once: the
resolution (where the object is and how wide), the allowlist (whether this
project lets that name leave at all), the size cap (how many bytes may leave in
one call), and the output path (where the bytes are allowed to land). Every one
of them is a property of a real ELF, a real GDB and a real target, and three of
the four are decided against numbers that only the board can supply, so a fake
can say nothing about any of them.

What is driven here, and only this: the MCP stdio server, spoken to over its own
protocol, and `agentic-hil lease-status` and `agentic-hil recover` at the command
line for the bench's ownership state. No openocd, no gdb, no device node. The
server is started per test and ends per test, because one probe and one board
mean one holder at a time and a session that outlived its test would meet the
next one on the same hardware.

Three things about the shape of this file:

* The symbol is the starter firmware's own. `uptime_ms` is declared in the
  demo's `Src/main.c` as `volatile uint32_t uptime_ms`, at file scope and with a
  complete type, which is what lets both halves of a resolution be answered and
  what makes the width below a fact about the source rather than a guess. Its
  address is never asserted against a number: the address is read out of the
  product's own answer and checked for the properties a `uint32_t` object must
  have, which is a claim that holds on any build of this firmware.

* Policy is varied by running a server against a copy of this session's own
  configuration with named keys changed, never by editing the configuration the
  session was set up with. A copy is written outside the workspace, the copy's
  absolute path is handed to the child in `AGENTIC_HIL_CONFIG`, and the original
  is not touched, so nothing here can leave the tier's configuration different
  from how it found it. The copies are per test and stable across reruns.

* Sessions and servers are closed in a fixture teardown that runs on a failing
  test as well as a passing one, and the same teardown reads the bench's
  ownership state afterwards and clears a quarantine through `agentic-hil
  recover` where one stands. A quarantine is recorded under the configuration
  the incident happened on, and the project key a lease is filed under includes
  the configuration's own path, so recovery is run once per configuration this
  file started a server against, never only against the authoritative one.

The board is left running the firmware it was carrying: a debug session ends
with the target halted and pinned so that detaching does not resume it, which is
correct for a session and wrong for a bench the next test inherits, so the
teardown resets the target through the product once every server that opened a
session.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from collections.abc import Iterator
from pathlib import Path
from threading import Thread
from typing import IO, Any

import pytest
import yaml

from .conftest import BENCH_ONLY, COMMAND_TIMEOUT_S, Bench, child_command, isolated_environment

pytestmark = [pytest.mark.bench, BENCH_ONLY]

# The symbol every read below asks for, and what the starter firmware says about
# it. From the demo this tier copies and builds, `Src/main.c`:
#
#     volatile uint32_t uptime_ms = 0U;
#
# File scope and a complete type, so a resolution has both an address and a
# `sizeof` to answer with, and the ELF symbol table carries the same object for
# the route that reads the table instead of asking the debugger.
COUNTER_SYMBOL = "uptime_ms"
# `uint32_t`, so four bytes, and four-byte aligned on this architecture. Both are
# properties of the declaration, not of a particular build or a particular board.
COUNTER_SIZE_BYTES = 4
COUNTER_ALIGNMENT_BYTES = 4

# The second name the allowlist tests hold constant, from the same file: the
# starter's SysTick handler. It is also the one function this firmware reaches
# every millisecond once it is running, which is what makes it a breakpoint a
# resume actually arrives at rather than one a test waits out.
TICK_HANDLER_SYMBOL = "SysTick_Handler"

# A valid C identifier this firmware does not define. Valid on purpose: the
# argument check has to pass so that what refuses the call is the lookup and not
# the identifier pattern in front of it.
ABSENT_SYMBOL = "no_such_object_in_this_firmware"

# What the MCP client half of this file says it speaks. The server negotiates
# the version and answers with the one it settled on; nothing here asserts which
# one that is, only that a server answered as this product.
CLIENT_PROTOCOL_VERSION = "2025-06-18"
CLIENT_INFO = {"name": "agentic-hil-bench-symbols", "version": "0"}

# How long the server may take to answer, before that is a failure rather than a
# slow machine. Read under a bound because no timeout plugin is configured in
# this repository, so an unbounded read of a live but silent server would run the
# job to its ceiling instead of failing with what did not answer.
INITIALIZE_TIMEOUT_S = 60.0
# A session start spawns a debug server, connects GDB and can download an image.
CALL_TIMEOUT_S = 300.0
# How long a resume may run before the product's own containment takes over. Long
# enough for a target that reaches a millisecond interrupt, short enough that a
# target that never reaches it fails the test rather than holding the bench.
RESUME_TIMEOUT_S = 10.0
SERVER_EXIT_TIMEOUT_S = 120.0
SERVER_KILL_TIMEOUT_S = 30.0

# The Intel HEX end-of-file record, which is one fixed line in the format and is
# therefore written out rather than derived.
INTEL_HEX_EOF_RECORD = ":00000001FF"
# Records that may appear in a dump of one symbol: an extended linear address, a
# data record, and the end record.
INTEL_HEX_EXTENDED_LINEAR_ADDRESS = 0x04
INTEL_HEX_DATA = 0x00
INTEL_HEX_END_OF_FILE = 0x01

# Where the dumps that are meant to land go. Under the workspace, and under a
# directory of this file's own so a rerun overwrites its own files and no other
# author's.
DUMP_DIRECTORY = "artifacts/bench-symbol-dumps"
BUILD_DUMP_DIRECTORY = "build/bench-symbol-dumps"


def a_line_within(stream: IO[str], seconds: float) -> str | None:
    """One line off a pipe, or None when nothing arrived inside the bound.

    A server that dies closes its stdout and the read returns at once; only a
    live but silent one needs this.
    """
    answered: list[str] = []
    reader = Thread(target=lambda: answered.append(stream.readline()), daemon=True)
    reader.start()
    reader.join(seconds)
    return answered[0] if answered else None


def intel_hex_payload(text: str) -> tuple[int, bytes]:
    """The address a dump starts at and the bytes it records, parsed here.

    Written out rather than taken from the product's own Intel HEX reader,
    deliberately: the claim under test is that what the dump tool wrote is Intel
    HEX, and parsing it with the writer's sibling would be asking the code under
    test to grade its own output. Every record is checked against the format's
    own rules (a record checksum that sums to zero, a length that matches the
    count byte, a contiguous address range, an end record last), so a file that
    is only nearly Intel HEX fails here instead of round-tripping.
    """
    lines = [line for line in text.splitlines() if line]
    assert lines, "the dump wrote a file with no records in it"
    assert lines[-1] == INTEL_HEX_EOF_RECORD, f"the dump does not end with the Intel HEX end record: {lines[-1]!r}"
    upper_address = 0
    base_address: int | None = None
    next_address: int | None = None
    payload = bytearray()
    for number, line in enumerate(lines, start=1):
        assert line.startswith(":"), f"record {number} is not an Intel HEX record: {line!r}"
        body = bytes.fromhex(line[1:])
        assert len(body) >= 5, f"record {number} is too short to be an Intel HEX record: {line!r}"
        assert sum(body) & 0xFF == 0, f"record {number} fails its own checksum: {line!r}"
        count, high, low, record_type = body[0], body[1], body[2], body[3]
        content = body[4:-1]
        assert len(content) == count, f"record {number} carries {len(content)} bytes and declares {count}: {line!r}"
        if record_type == INTEL_HEX_EXTENDED_LINEAR_ADDRESS:
            assert count == 2, f"record {number} is an extended linear address of the wrong width: {line!r}"
            upper_address = int.from_bytes(content, "big")
        elif record_type == INTEL_HEX_DATA:
            address = (upper_address << 16) | (high << 8) | low
            if base_address is None:
                base_address, next_address = address, address
            assert address == next_address, f"record {number} leaves a hole in the dumped range: {line!r}"
            payload.extend(content)
            next_address = address + count
        elif record_type == INTEL_HEX_END_OF_FILE:
            assert number == len(lines), f"the end record is not the last line: record {number}"
        else:
            raise AssertionError(f"record {number} carries a record type a symbol dump never writes: {line!r}")
    assert base_address is not None, "the dump wrote no data record at all"
    return base_address, bytes(payload)


def cli(bench: Bench, config: Path, *arguments: str) -> tuple[int, dict]:
    """One command's machine document, under a named configuration.

    ``Bench.document`` always runs against the configuration ``init`` selected.
    A server started against a copy files its ownership state under a different
    project key, because the key is derived from the configuration's own path, so
    the command that reads or clears that state has to be pointed at the same
    copy.
    """
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


def configuration_copy(bench: Bench, name: str, changes: dict[str, dict[str, Any]]) -> Path:
    """This session's configuration with named keys changed, written outside the workspace.

    The authoritative file is read and never written. The copy has to live
    outside ``workspace_root`` because the product refuses a configuration stored
    inside the workspace it governs, and it is handed over as an absolute path in
    ``AGENTIC_HIL_CONFIG``, which is the documented override.
    """
    document = bench.configuration()
    for section, fields in changes.items():
        document[section] = {**(document.get(section) or {}), **fields}
    directory = bench.config_root / "symbol-configuration-copies"
    directory.mkdir(parents=True, exist_ok=True)
    written = directory / f"{name}.yaml"
    written.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return written


class Server:
    """One `agentic-hil mcp-stdio` process, spoken to over its own protocol."""

    def __init__(self, bench: Bench, config: Path) -> None:
        self.config = config
        self._next_id = 0
        # Latched the moment a read runs out of time. The reader thread that
        # timed out is still parked on this pipe, so the answer it is waiting for
        # would reach nobody and the line after it would answer the wrong
        # request. Every later call on this server refuses instead, which is what
        # keeps a teardown running after a timeout from reporting a mismatch it
        # caused itself.
        self._unresponsive = False
        # Its lifetime is the child's, not a block's, and `close` releases it. A
        # pipe would do instead and must not: nothing drains it while the server
        # runs, so a server that wrote enough to fill it would block on its own
        # error stream rather than answer. Kept rather than dropped because a
        # server that would not start says why here and nowhere else.
        self._stderr = tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace")  # noqa: SIM115 - see above
        self.process = subprocess.Popen(
            child_command("mcp-stdio"),
            cwd=str(bench.project),
            env=isolated_environment(bench.config_root, bench.state_root, AGENTIC_HIL_CONFIG=str(config)),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self._stderr,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
        opened = self.request(
            "initialize",
            {"protocolVersion": CLIENT_PROTOCOL_VERSION, "capabilities": {}, "clientInfo": CLIENT_INFO},
            INITIALIZE_TIMEOUT_S,
        )
        assert opened["serverInfo"]["name"] == "agentic-hil", opened
        assert isinstance(opened.get("protocolVersion"), str) and opened["protocolVersion"], opened
        self.notify("notifications/initialized")

    def request(self, method: str, params: dict, timeout_s: float = CALL_TIMEOUT_S) -> dict:
        assert self.process.stdin is not None and self.process.stdout is not None
        assert not self._unresponsive, f"{method} was not sent: this server already failed to answer inside its bound"
        self._next_id += 1
        request_id = self._next_id
        self.process.stdin.write(json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}) + "\n")
        self.process.stdin.flush()
        line = a_line_within(self.process.stdout, timeout_s)
        if line is None:
            self._unresponsive = True
            raise AssertionError(f"the server did not answer {method} within {timeout_s:.0f}s{self._diagnosis()}")
        assert line.strip(), f"the server answered nothing to {method}{self._diagnosis()}"
        answered = json.loads(line)
        assert "error" not in answered, answered
        assert answered["id"] == request_id, answered
        return answered["result"]

    def notify(self, method: str) -> None:
        assert self.process.stdin is not None
        self.process.stdin.write(json.dumps({"jsonrpc": "2.0", "method": method}) + "\n")
        self.process.stdin.flush()

    def call(self, name: str, arguments: dict | None = None, timeout_s: float = CALL_TIMEOUT_S) -> dict:
        """One tool result, taken off the structured half and checked against the other two.

        A host reads whichever of the three the specification lets it read, so a
        result whose text block disagrees with its structured content, or whose
        `isError` disagrees with a refusal, is a defect in itself and is caught
        here rather than in every test.
        """
        result = self.request("tools/call", {"name": name, "arguments": arguments or {}}, timeout_s)
        structured = result["structuredContent"]
        assert json.loads(result["content"][0]["text"]) == structured, result
        if structured.get("ok") is not True:
            assert result["isError"] is True, result
        return structured

    def close(self) -> None:
        if self.process.poll() is None:
            assert self.process.stdin is not None
            self.process.stdin.close()
            # A server that already missed one bound is not waited on for a
            # second: closing its stdin is the polite ask, and the probe it may
            # still be holding is owed to the next test rather than to this one's
            # patience.
            try:
                self.process.wait(SERVER_KILL_TIMEOUT_S if self._unresponsive else SERVER_EXIT_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(SERVER_KILL_TIMEOUT_S)
        if self.process.stdout is not None:
            self.process.stdout.close()
        self._stderr.close()

    def _diagnosis(self) -> str:
        """Whatever the server wrote to its error stream, for a message that would
        otherwise say only that nothing arrived."""
        self._stderr.seek(0)
        written = self._stderr.read()
        self._stderr.seek(0, 2)
        return f"\nserver stderr:\n{written}" if written.strip() else ""


class BenchSymbols:
    """The servers and sessions one test opened, and the teardown that closes them."""

    def __init__(self, bench: Bench, image_path: str) -> None:
        self.bench = bench
        self.image_path = image_path
        self._servers: list[Server] = []
        self._sessions: list[Server] = []

    def server(self, config: Path | None = None) -> Server:
        started = Server(self.bench, config or self.bench.config)
        self._servers.append(started)
        return started

    def session(self, server: Server, mode: str = "attach") -> dict:
        """A debug session on the built firmware, registered so teardown ends it."""
        started = server.call("debug_start_session", {"image_path": self.image_path, "mode": mode})
        if started.get("ok") is True:
            self._sessions.append(server)
        assert started["ok"] is True, started
        assert started["session"]["status"] == "halted", started
        return started

    def close(self) -> None:
        """Stop every session, leave the board running, close every server, clear
        any quarantine, and report everything that did not go to plan.

        Collected rather than raised at the first problem: a session that will
        not stop must not stop the server from being closed, and a server left
        running would hold the probe against every test after this one.
        """
        problems: list[str] = []
        for server in self._sessions:
            try:
                # A breakpoint is something a test opened too, and it is cleared
                # through the product rather than left for the teardown of the
                # session to take down with the connection.
                cleared = server.call("debug_clear_breakpoints")
                if cleared.get("ok") is not True:
                    problems.append(f"debug_clear_breakpoints did not clear the session's breakpoints: {cleared}")
                stopped = server.call("debug_stop_session")
                if stopped.get("ok") is not True:
                    problems.append(f"debug_stop_session did not end the session: {stopped}")
                # The session is over and the target is halted and pinned against
                # a resume on detach, which is right for a session and wrong for
                # the next test. Put back through the product's own reset.
                reset = server.call("reset_target", {"mode": "run"})
                if reset.get("ok") is not True:
                    problems.append(f"the target was not reset after the session: {reset}")
            except Exception as error:
                problems.append(f"a session could not be ended: {type(error).__name__}: {error}")
        for server in self._servers:
            try:
                server.close()
            except Exception as error:
                problems.append(f"a server could not be closed: {type(error).__name__}: {error}")
        for config in {server.config for server in self._servers}:
            problems.extend(self._clear_quarantine(config))
        assert not problems, "\n".join(problems)

    def _clear_quarantine(self, config: Path) -> list[str]:
        """Whatever `agentic-hil recover` could not settle under one configuration.

        The bench is left as it was found or the reason it was not is reported.
        A recovery run against a configuration copy meets its own record, so the
        digest of the file the incident was filed under is the digest of the file
        recovering it; the second call covers the case where it is not, which is
        the operator's own documented override and is what the refusal asks for.
        """
        _, status = cli(self.bench, config, "lease-status")
        # Both questions, because they are different ones since the quarantine
        # narrowed: a bench can hold a standing incident without being blocked,
        # and a teardown that asked only whether the bench was blocked would hand
        # that incident to the next test in this file, where it reads as that
        # test's own failure.
        if not status.get("blocked") and not status.get("incident_stands"):
            return []
        quarantine_id = status.get("quarantine_id")
        if not isinstance(quarantine_id, str) or not quarantine_id:
            return [f"the bench is not free and names no quarantine id to clear: {status.get('cleanup_reasons')}"]
        _, recovered = cli(self.bench, config, "recover", "--confirm-safe-state", "--quarantine-id", quarantine_id)
        if recovered.get("error_type") == "config_changed":
            _, recovered = cli(
                self.bench, config, "recover", "--confirm-safe-state", "--quarantine-id", quarantine_id, "--accept-config-change"
            )
        if recovered.get("ok") is not True:
            return [f"a quarantine this file raised could not be cleared: {recovered}"]
        return []


@pytest.fixture
def symbols(bench: Bench, firmware: Path) -> Iterator[BenchSymbols]:
    """One test's servers and sessions, closed whether the test passed or failed."""
    manager = BenchSymbols(bench, firmware.relative_to(bench.project).as_posix())
    try:
        yield manager
    finally:
        manager.close()


def test_symbol_info_answers_the_counters_address_and_the_width_its_declaration_gives_it(symbols: BenchSymbols) -> None:
    """Where the starter's counter lives, read out of the image the session loaded.

    Catches a resolution that answers with something other than the object asked
    for: an address of zero or one that is not aligned for a `uint32_t` is not
    this symbol, and a size other than four is not this declaration. Both halves
    have to come from one lookup, and the answer has to say which route produced
    it, because a caller comparing an address against a firmware map needs to
    know whether the debugger or the symbol table answered.
    """
    server = symbols.server()
    symbols.session(server)

    resolved = server.call("debug_symbol_info", {"symbol": COUNTER_SYMBOL})

    assert resolved["ok"] is True, resolved
    assert resolved["symbol"] == COUNTER_SYMBOL, resolved
    assert resolved["size_bytes"] == COUNTER_SIZE_BYTES, resolved
    assert isinstance(resolved["address"], str) and resolved["address"].startswith("0x"), resolved
    address = int(resolved["address"], 16)
    assert address != 0, resolved
    assert address % COUNTER_ALIGNMENT_BYTES == 0, resolved
    assert resolved["resolved_from"] in {"debug_info", "elf_symbol_table"}, resolved
    assert resolved["session"]["status"] == "halted", resolved
    assert resolved["summary"] == "Symbol resolved.", resolved


def test_symbol_value_returns_the_counters_bytes_and_decodes_them_in_the_order_the_image_declares(
    symbols: BenchSymbols,
) -> None:
    """The bytes themselves, and the two readings of them, checked against each other.

    Catches a read that returns the address instead of the contents, one whose
    hex and `size_bytes` disagree, and a decode taken in the wrong byte order:
    the integers are recomputed here from the hex the same result carries, so a
    result that decoded big-endian on a little-endian image fails even though
    every field is present. The order is also asserted to have been read off the
    image rather than assumed, because that is the whole reason the field exists.
    """
    server = symbols.server()
    symbols.session(server)

    read = server.call("debug_symbol_value", {"symbol": COUNTER_SYMBOL})

    assert read["ok"] is True, read
    assert read["symbol"] == COUNTER_SYMBOL, read
    assert read["size_bytes"] == COUNTER_SIZE_BYTES, read
    raw = bytes.fromhex(read["hex"])
    assert len(raw) == COUNTER_SIZE_BYTES, read
    assert read["byte_order"] == "little", read
    assert read["byte_order_from"] == "elf_header", read
    assert read["value_unsigned"] == int.from_bytes(raw, "little", signed=False), read
    assert read["value_signed"] == int.from_bytes(raw, "little", signed=True), read

    where = server.call("debug_symbol_info", {"symbol": COUNTER_SYMBOL})
    assert read["address"] == where["address"], (read, where)


def test_a_symbol_this_firmware_does_not_carry_is_refused_without_touching_the_board(symbols: BenchSymbols) -> None:
    """A name nothing defines, refused as a lookup failure and not as a broken bench.

    Catches two opposite mistakes. A missing symbol reported as a generic
    debugger failure tells an agent to go looking at the probe for a typo in its
    own argument, so the error_type has to be the one the error catalogue keys
    that fix on. And a lookup that failed having committed nothing must not leave
    the bench owing anybody a recovery: an agent that reads `cleanup_required`
    after a typo stops working on a bench that is fine.

    Both routes are asserted to have been taken and to have failed: the debugger
    was asked and so was the image's own symbol table, which is what
    `symbol_table_lookup` records.
    """
    server = symbols.server()
    symbols.session(server)

    refused = server.call("debug_symbol_info", {"symbol": ABSENT_SYMBOL})

    assert refused["ok"] is False, refused
    assert refused["error_type"] == "symbol_not_found", refused
    assert refused["symbol"] == ABSENT_SYMBOL, refused
    assert refused["side_effect_committed"] is False, refused
    assert refused.get("cleanup_required") is not True, refused
    assert isinstance(refused.get("symbol_table_lookup"), str) and refused["symbol_table_lookup"], refused

    _, ownership = cli(symbols.bench, server.config, "lease-status")
    assert ownership["incident_stands"] is False, ownership


def test_the_three_reads_refuse_a_symbol_the_allowlist_does_not_carry_in_the_same_words(symbols: BenchSymbols) -> None:
    """One allowed name and one that is not, under a configuration that lists them.

    The allowlist is the operator's statement about what may leave this bench at
    all, so it has to bite the same way on all three reads and it has to name
    itself: an agent that reports a refusal reads the summary out, and a summary
    that did not name `debug.allowed_symbols` sent an operator looking through
    permission blocks for a key that is not one. The refusal is asserted to be
    identical across the three tools for the same reason.

    Two further claims that a shallower test would miss: the refused dump wrote
    no file, and the listed symbol still answers, so what is being measured is
    the list and not a configuration that broke every read.
    """
    config = configuration_copy(
        symbols.bench,
        "counter-only",
        {"debug": {"allow_all_symbols": False, "allowed_symbols": [COUNTER_SYMBOL]}},
    )
    server = symbols.server(config)
    symbols.session(server)
    output = f"{DUMP_DIRECTORY}/refused-by-the-allowlist.hex"

    refusals = [
        server.call("debug_symbol_info", {"symbol": TICK_HANDLER_SYMBOL}),
        server.call("debug_symbol_value", {"symbol": TICK_HANDLER_SYMBOL}),
        server.call("debug_dump_symbol_ihex", {"symbol": TICK_HANDLER_SYMBOL, "output_path": output}),
    ]

    for refused in refusals:
        assert refused["ok"] is False, refused
        assert refused["error_type"] == "permission_denied", refused
        assert refused["symbol"] == TICK_HANDLER_SYMBOL, refused
        assert "debug.allowed_symbols" in refused["summary"], refused["summary"]
    assert len({refused["summary"] for refused in refusals}) == 1, refusals
    assert not (symbols.bench.project / output).exists(), "a refused dump created its output file anyway"

    allowed = server.call("debug_symbol_info", {"symbol": COUNTER_SYMBOL})
    assert allowed["ok"] is True, allowed
    assert allowed["size_bytes"] == COUNTER_SIZE_BYTES, allowed


def test_the_allowlist_is_checked_before_the_lookup_so_the_two_refusals_stay_apart(symbols: BenchSymbols) -> None:
    """A list carrying only a name the firmware has not got, which separates the gates.

    Same two symbols as the test above and the opposite list, so what changed is
    the configuration and not the firmware. That makes the ordering visible: the
    symbol the board really carries is refused as a permission, because the list
    is consulted first, and the symbol the list carries is refused as a lookup,
    because the list let it through to a debugger that could not find it.

    Catches a gate that ran the other way round. An allowlist applied after the
    resolution would answer `symbol_not_found` for a name it was never going to
    allow, which tells an agent the firmware is wrong when the policy is what
    refused it, and would have asked the board about a name policy had already
    denied.
    """
    config = configuration_copy(
        symbols.bench,
        "absent-name-only",
        {"debug": {"allow_all_symbols": False, "allowed_symbols": [ABSENT_SYMBOL]}},
    )
    server = symbols.server(config)
    symbols.session(server)

    denied = server.call("debug_symbol_info", {"symbol": COUNTER_SYMBOL})
    missing = server.call("debug_symbol_info", {"symbol": ABSENT_SYMBOL})

    assert denied["ok"] is False, denied
    assert denied["error_type"] == "permission_denied", denied
    assert "debug.allowed_symbols" in denied["summary"], denied["summary"]
    assert missing["ok"] is False, missing
    assert missing["error_type"] == "symbol_not_found", missing


def test_a_dump_inside_the_workspace_is_intel_hex_over_the_symbols_own_address_range(symbols: BenchSymbols) -> None:
    """The file a dump leaves behind, read back as the format it claims to be.

    Catches a dump that reports success and writes something no Intel HEX reader
    accepts, one that writes the right bytes at the wrong address, and one that
    writes a different region than the symbol occupies. The parser here checks
    each record against the format's own rules rather than looking for a
    recognisable first line, and the payload is compared with the bytes
    `debug_symbol_value` reads out of the same address.

    The two value reads bracket the dump. The target is halted for the whole
    session, so a counter in RAM cannot advance between them: their agreement is
    a claim about the halt as much as it is the licence to compare the dump
    against them, and if the board were running underneath a session that
    reports it halted, this is where that shows.
    """
    server = symbols.server()
    symbols.session(server)
    output = f"{DUMP_DIRECTORY}/counter.hex"
    written = symbols.bench.project / output
    # Removed before the dump so that the file found afterwards is this run's.
    # Without it the same assertion passes on a file an earlier run left behind
    # and a dump that wrote nothing at all would read as a dump that worked.
    written.unlink(missing_ok=True)

    before = server.call("debug_symbol_value", {"symbol": COUNTER_SYMBOL})
    dumped = server.call("debug_dump_symbol_ihex", {"symbol": COUNTER_SYMBOL, "output_path": output})
    after = server.call("debug_symbol_value", {"symbol": COUNTER_SYMBOL})

    assert dumped["ok"] is True, dumped
    assert dumped["symbol"] == COUNTER_SYMBOL, dumped
    assert dumped["size_bytes"] == COUNTER_SIZE_BYTES, dumped
    assert dumped["address"] == before["address"], (dumped, before)
    # Where the result says the file went. The workspace-relative path is the
    # half a caller can act on and is asserted whole; the resolved one is this
    # bench's own and only its last component is looked at, because a test that
    # compared it against anything would be a test about one machine's layout.
    assert dumped["output"]["path"] == output, dumped["output"]
    assert Path(dumped["output"]["resolved_path"]).name == Path(output).name, dumped["output"]

    assert written.is_file(), f"the dump reported success and wrote no file at {output}"
    base_address, payload = intel_hex_payload(written.read_text(encoding="ascii"))
    assert base_address == int(dumped["address"], 16), (base_address, dumped["address"])
    assert len(payload) == COUNTER_SIZE_BYTES, payload.hex()

    assert before["hex"] == after["hex"], (before, after)
    assert payload.hex() == before["hex"], (payload.hex(), before["hex"])


def test_a_dump_aimed_out_of_the_workspace_is_refused_and_says_which_rule_refused_it(symbols: BenchSymbols) -> None:
    """Three output paths that must never be written, each refused on its own rule.

    An absolute path outside the workspace, the same escape spelled as a
    traversal, and a path inside the workspace with an extension this dump does
    not write. All three are the one write primitive an agent aims wherever it
    likes, so each has to be refused before any memory is read, and the finding
    has to say which rule refused it: the validation flags are what an operator
    reads to tell a containment refusal from an extension one.

    Catches a check that collapses the three into one verdict, a traversal that
    resolves into the workspace and is then accepted, and any of them creating
    the file it was refused. The session is open while this runs, so a refusal
    here is the path rule and not a missing session.
    """
    server = symbols.server()
    symbols.session(server)
    outside = symbols.bench.config_root / "symbol-dumps-outside-the-workspace" / "counter.hex"
    traversal = "../bench-symbol-dump-escaped.hex"
    wrong_extension = f"{DUMP_DIRECTORY}/counter.txt"

    escaped = server.call("debug_dump_symbol_ihex", {"symbol": COUNTER_SYMBOL, "output_path": str(outside)})
    climbed = server.call("debug_dump_symbol_ihex", {"symbol": COUNTER_SYMBOL, "output_path": traversal})
    misnamed = server.call("debug_dump_symbol_ihex", {"symbol": COUNTER_SYMBOL, "output_path": wrong_extension})

    for refused in (escaped, climbed, misnamed):
        assert refused["ok"] is False, refused
        assert refused["error_type"] == "output_validation_failed", refused
        assert refused["tool"] == "debug_dump_symbol_ihex", refused
    assert escaped["validation"]["within_workspace"] is False, escaped
    assert escaped["validation"]["path_traversal_safe"] is True, escaped
    assert climbed["validation"]["path_traversal_safe"] is False, climbed
    assert misnamed["validation"]["within_workspace"] is True, misnamed
    assert misnamed["validation"]["allowed_extension"] is False, misnamed

    assert not outside.exists(), "a dump refused for leaving the workspace wrote its file anyway"
    assert not (symbols.bench.project.parent / Path(traversal).name).exists(), "a traversal dump wrote outside the workspace"
    assert not (symbols.bench.project / wrong_extension).exists(), "a dump refused for its extension wrote its file anyway"


def test_a_dump_outside_the_configured_artifact_roots_is_refused_and_one_inside_them_is_written(
    symbols: BenchSymbols,
) -> None:
    """Containment and the artifact roots as two separate answers, under a narrowed list.

    A configuration whose `artifacts.allowed_roots` names one directory. A dump
    into that directory is written; a dump into a sibling directory of the same
    workspace is refused, with the flags saying that it stayed inside the
    workspace and left the roots.

    Catches a roots check that is really a containment check. With the generated
    default of the whole workspace the two are indistinguishable, so a
    `allowed_root` computed from containment would pass every test that ran on a
    generated configuration and would let an operator's narrowing do nothing at
    all.
    """
    config = configuration_copy(symbols.bench, "build-root-only", {"artifacts": {"allowed_roots": ["build"]}})
    server = symbols.server(config)
    symbols.session(server)
    inside_the_root = f"{BUILD_DUMP_DIRECTORY}/counter.hex"
    outside_the_root = f"{DUMP_DIRECTORY}/outside-the-root.hex"
    written = symbols.bench.project / inside_the_root
    # Same reason as the dump test above: the file below has to be this run's.
    written.unlink(missing_ok=True)

    accepted = server.call("debug_dump_symbol_ihex", {"symbol": COUNTER_SYMBOL, "output_path": inside_the_root})
    refused = server.call("debug_dump_symbol_ihex", {"symbol": COUNTER_SYMBOL, "output_path": outside_the_root})

    assert accepted["ok"] is True, accepted
    assert accepted["output"]["path"] == inside_the_root, accepted["output"]
    assert written.is_file(), f"the dump reported success and wrote no file at {inside_the_root}"
    base_address, payload = intel_hex_payload(written.read_text(encoding="ascii"))
    assert base_address == int(accepted["address"], 16), (base_address, accepted["address"])
    assert len(payload) == COUNTER_SIZE_BYTES, payload.hex()

    assert refused["ok"] is False, refused
    assert refused["error_type"] == "output_validation_failed", refused
    assert refused["validation"]["within_workspace"] is True, refused
    assert refused["validation"]["allowed_root"] is False, refused
    assert not (symbols.bench.project / outside_the_root).exists(), "a dump outside the artifact roots wrote its file anyway"


def test_the_dump_size_cap_stops_both_reads_and_leaves_the_address_answerable(symbols: BenchSymbols) -> None:
    """A cap below the symbol's own width, which separates a read from a resolution.

    `debug.max_dump_size_bytes` is a limit on how many bytes may leave the bench
    in one call. Under a cap of one byte the four-byte counter cannot be read or
    dumped, and both refusals have to name the key and carry the two numbers a
    caller needs to see: what was asked for and what the ceiling is.

    What the same cap must not touch is `debug_symbol_info`. An address and a
    size are properties of the image and no target memory leaves the bench to
    answer them, so a cap applied there would refuse a question that reads
    nothing, which is exactly the mistake this catches. The refused dump is also
    asserted to have written no file, because a cap checked after the read would
    still have produced one.
    """
    config = configuration_copy(symbols.bench, "one-byte-cap", {"debug": {"max_dump_size_bytes": 1}})
    server = symbols.server(config)
    symbols.session(server)
    output = f"{DUMP_DIRECTORY}/over-the-cap.hex"

    resolved = server.call("debug_symbol_info", {"symbol": COUNTER_SYMBOL})
    read = server.call("debug_symbol_value", {"symbol": COUNTER_SYMBOL})
    dumped = server.call("debug_dump_symbol_ihex", {"symbol": COUNTER_SYMBOL, "output_path": output})

    assert resolved["ok"] is True, resolved
    assert resolved["size_bytes"] == COUNTER_SIZE_BYTES, resolved

    for refused in (read, dumped):
        assert refused["ok"] is False, refused
        assert refused["error_type"] == "permission_denied", refused
        assert refused["symbol"] == COUNTER_SYMBOL, refused
        assert refused["size_bytes"] == COUNTER_SIZE_BYTES, refused
        assert refused["max_dump_size_bytes"] == 1, refused
        assert "debug.max_dump_size_bytes" in refused["summary"], refused["summary"]
    assert not (symbols.bench.project / output).exists(), "a dump over the cap wrote its file anyway"


def test_every_symbol_read_refuses_without_a_session_rather_than_opening_the_probe(symbols: BenchSymbols) -> None:
    """The three reads with nothing started, on a backend that answers them out of a session.

    This backend resolves and reads through the session a caller opened, so the
    three reads have to say that no session is active rather than starting one of
    their own. Catches a read that quietly opens a probe nobody declared, which
    on a bench with one board is a read that takes hardware another run is
    holding.

    No session is opened here at all, so nothing is left to close beyond the
    server itself.
    """
    server = symbols.server()

    refusals = [
        server.call("debug_symbol_info", {"symbol": COUNTER_SYMBOL}),
        server.call("debug_symbol_value", {"symbol": COUNTER_SYMBOL}),
        server.call("debug_dump_symbol_ihex", {"symbol": COUNTER_SYMBOL, "output_path": f"{DUMP_DIRECTORY}/no-session.hex"}),
    ]

    for refused in refusals:
        assert refused["ok"] is False, refused
        assert refused["error_type"] == "session_not_active", refused
    assert not (symbols.bench.project / DUMP_DIRECTORY / "no-session.hex").exists(), "a refused dump wrote its file anyway"

    _, ownership = cli(symbols.bench, server.config, "lease-status")
    assert ownership["incident_stands"] is False, ownership


def test_a_failed_lookup_leaves_the_session_able_to_resume(symbols: BenchSymbols) -> None:
    """A lookup that found nothing, and the resume after it, which was #493.

    Resolving a symbol does not move the target, so it must not change what the
    session records about why the target is stopped. A name the debugger could
    not resolve used to be recorded as a debugger error against the session,
    and the next `debug_continue` short-circuited on that recorded state and
    refused to resume a target that was sitting exactly where the caller left
    it. An agent that mistyped a symbol name lost the session it was debugging
    in.

    The first resume is the control: it runs before the failed lookup and proves
    this session, this breakpoint and this target can do the thing the second
    resume is refused. So what separates the two is the lookup between them and
    nothing else.

    The session is started in `load` mode, which puts the firmware that was built
    from this repository's own demo onto the board and nothing else, because a
    resume that has to arrive at a breakpoint needs the code on the target to be
    the code the symbols describe. The breakpoint is the starter's millisecond
    interrupt handler, so an arriving resume arrives at once.

    Nothing here can leave the bench quarantined: both resumes stop at a
    breakpoint or are refused before the target moves, and the fixture's teardown
    stops the session, resets the target and clears a quarantine through
    `agentic-hil recover` if one stands anyway.
    """
    server = symbols.server()
    symbols.session(server, mode="load")
    marked = server.call("debug_set_breakpoint", {"location": {"symbol": TICK_HANDLER_SYMBOL}})
    assert marked["ok"] is True, marked

    reached = server.call("debug_continue", {"timeout_s": RESUME_TIMEOUT_S})
    assert reached["ok"] is True, reached
    assert reached["stop_reason"] == "breakpoint_hit", reached

    missed = server.call("debug_symbol_info", {"symbol": ABSENT_SYMBOL})
    assert missed["error_type"] == "symbol_not_found", missed

    resumed = server.call("debug_continue", {"timeout_s": RESUME_TIMEOUT_S})

    assert resumed["ok"] is True, resumed
    assert resumed["stop_reason"] == "breakpoint_hit", resumed
