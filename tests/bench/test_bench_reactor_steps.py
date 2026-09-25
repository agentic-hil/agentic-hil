"""The plan runner's own step kinds, each one driven on the board.

A plan is the product's primary path, and until this module only four of its
step kinds had ever run against hardware: `flash`, `reset`, `uart_open` and
`uart_read`. Everything a plan does with a debug session (start one, run to a
breakpoint, read a symbol and judge it, dump it, stop the session), everything
it does with time (`delay`, and `repeat` bounded by a count or by a duration),
and closing a serial line as a step of its own rather than as cleanup, was
proved against fakes only. Each of those has the board as its counterparty, so
each is driven here through `agentic-hil test-reactor`, the command an operator
runs, and read back off the report it prints.

Everything here runs against the demo this tier builds and flashes once per
session, and asserts only what that firmware makes true:

* `uptime_ms` is `volatile uint32_t uptime_ms = 0U;` in the demo's `main.c`,
  counted up once a millisecond by the SysTick handler that `main` starts. At a
  breakpoint on `main` after a reset into halt, startup has zeroed it and nothing
  has started counting it, so it holds exactly zero. Once the board has run for
  a while it holds the milliseconds since `main` started it, which is a range a
  claim can bound without naming a number the bench decides.
* an attach session halts the core, so every read inside one session sees the
  same word, which is what lets several claims about one value be checked
  against each other.
* the demo prints `Hello World` once after every reset, which is the banner the
  serial steps wait for.

No address, no probe and no port is named anywhere below: the debugger and the
port are the names this session's own configuration gives them, and the address
a read reports is only ever compared with the address another read of the same
run reports.

Every test leaves the board running the demo. A red plan's recovery resets the
target into halt, and an attach session leaves it halted too, so the autouse
fixture below resets it into run through a plan after every test, passed or
failed, having cleared any incident a failed test left standing first.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

from .conftest import BENCH_ONLY, Bench

pytestmark = [pytest.mark.bench, BENCH_ONLY]

# The demo's millisecond counter, and the width its declaration gives it.
COUNTER_SYMBOL = "uptime_ms"
COUNTER_SIZE_BYTES = 4
# The demo's entry, which is reached from a reset before SysTick is started.
ENTRY_FUNCTION = "main"
# How long a run to `main` may take from a reset into halt. The core is at the
# reset vector and `main` is the first thing startup calls, so this is a bound on
# a stuck probe rather than a guess at how long startup takes.
RESUME_TIMEOUT_S = 15
# How long the board runs between the two sessions. Long enough that the counter
# is far above the floor of the range claim below.
SETTLE_MS = 1500
# What a counter that has been counting since before the delay must hold at
# least, and what it cannot have reached: the floor is below the delay itself,
# because the counter only starts once `main` has initialised the UART, and the
# ceiling is ten minutes, which no step of this plan comes near.
COUNTING_RANGE = {"min": 1000, "max": 600_000}
# The upper half of the 32-bit word. The counter stays below 65536 for the first
# minute the board runs, so its upper half is still all zero when it is read.
UPPER_HALF_MASK = 0xFFFF0000
# Where the dump goes. Inside the workspace, which is the artifact root this
# session's generated configuration names, and under a directory of this
# module's own so a rerun overwrites only its own file.
DUMP_OUTPUT = "artifacts/reactor-steps/uptime-at-main.hex"

# The banner the demo prints after every reset, as the pattern a plan waits for.
# A pattern rather than the literal line, so the claim that is exercised is the
# regular expression search and not a substring test.
BANNER_PATTERN = r"Hello\s+World"
BANNER_TEXT = "Hello World"
BANNER_TIMEOUT_S = 5

# How long a duration-bounded block is asked to run. Long enough for more than
# one reset and read on this bench, short enough that the test is cheap.
REPEAT_DURATION_S = 3

# The Intel HEX end-of-file record, which is one fixed line in the format and is
# therefore written out rather than derived.
INTEL_HEX_EOF_RECORD = ":00000001FF"
# Records that may appear in a dump of one symbol: an extended linear address, a
# data record, and the end record.
INTEL_HEX_EXTENDED_LINEAR_ADDRESS = 0x04
INTEL_HEX_DATA = 0x00
INTEL_HEX_END_OF_FILE = 0x01


def write_plan(bench: Bench, name: str, version: int, steps: list[dict[str, Any]]) -> str:
    """One plan in the project directory, named after itself; the name to run it by.

    Written through the YAML dumper rather than as text, so a pattern with a
    backslash in it reaches the plan loader as the string it is here.
    """
    plan = bench.project / f"{name}.yaml"
    plan.write_text(yaml.safe_dump({"version": version, "name": name, "steps": steps}, sort_keys=False), encoding="utf-8")
    return plan.name


def intel_hex_payload(text: str) -> tuple[int, bytes]:
    """The address a dump starts at and the bytes it records, parsed here.

    Written out rather than taken from the product's own Intel HEX reader,
    deliberately: the claim under test is that what the dump step wrote is Intel
    HEX, and parsing it with the writer's sibling would be asking the code under
    test to grade its own output. Every record is checked against the format's
    own rules (a record checksum that sums to zero, a length that matches the
    count byte, a contiguous address range, an end record last).
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


def leave_the_board_running(bench: Bench) -> None:
    """The bench handed on free, with the demo executing.

    A red plan's recovery resets the target into halt, which is right for the
    evidence and wrong for the next test, and a test that failed halfway may
    have left an incident standing. Both are settled through the product: the
    quarantine through `agentic-hil recover` with the id `lease-status` names,
    the halt through a plan that resets the board into run.
    """
    _, status = bench.document("lease-status")
    if status.get("blocked") or status.get("incident_stands"):
        quarantine_id = status.get("quarantine_id")
        assert isinstance(quarantine_id, str) and quarantine_id, f"the bench is not free and names no quarantine id to clear: {status.get('cleanup_reasons')}"
        _, recovered = bench.document("recover", "--confirm-safe-state", "--quarantine-id", quarantine_id)
        assert recovered.get("ok") is True, recovered
    plan = write_plan(bench, "reactor-steps-leave-the-board-running", 3, [{"device": bench.debugger_name(), "action": "reset", "mode": "run"}])
    status_code, report = bench.document("test-reactor", "--test-config", plan)
    assert status_code == 0, report


@pytest.fixture(autouse=True)
def board_left_running(bench: Bench, firmware: Path) -> Iterator[None]:
    """The demo on the board before each test, and running after it.

    `firmware` puts the demo on the board once per session, and no test here
    flashes anything else. What it does not do is start a core that a test left
    halted, which is this fixture's half.
    """
    yield
    leave_the_board_running(bench)


def results(report: dict) -> list[dict]:
    """Every step's result, in plan order."""
    return [record["result"] for record in report["steps"]]


def test_a_plan_stops_at_main_reads_the_counter_before_it_starts_and_reads_it_counting_after_a_delay(bench: Bench, firmware: Path) -> None:
    """Every debug step kind and every claim `read_symbol` allows, in one plan.

    The plan resets the board into halt, runs it to `main`, and makes the two
    claims that hold there (the counter is zero, read unsigned and read signed),
    dumps the counter, and stops the session. Then it lets the board run for a
    delay, attaches, and makes the three claims that hold on a counter that has
    been counting: inside a range, inside the same range read signed, and zero in
    its upper half under a mask, followed by a plain read that claims nothing.

    Catches a runner that judges a claim on the wrong reading, a range or a mask
    applied to something other than the value read, a read without a claim that
    is reshaped instead of answered as the tool answers it, a dump that writes
    other bytes than the read reports, and a delay that does not wait. The dump
    is parsed here as Intel HEX and compared with the read at the same address,
    and every read of the attached session has to agree on one value, because
    the core is halted for all of them.
    """
    debugger = bench.debugger_name()
    image = firmware.relative_to(bench.project).as_posix()
    dumped_file = bench.project / DUMP_OUTPUT
    dumped_file.parent.mkdir(parents=True, exist_ok=True)
    # Removed first, so the file read below is this run's and not one an
    # earlier run left behind.
    dumped_file.unlink(missing_ok=True)
    plan = write_plan(
        bench,
        "reactor-steps-counter",
        5,
        [
            {"device": debugger, "action": "debug_start", "image_path": image, "mode": "reset_halt"},
            {"device": debugger, "action": "run_until_breakpoint", "location": {"function": ENTRY_FUNCTION}, "timeout_s": RESUME_TIMEOUT_S},
            {"device": debugger, "action": "read_symbol", "symbol": COUNTER_SYMBOL, "size_bytes": COUNTER_SIZE_BYTES, "comparator": {"equals": 0}},
            {"device": debugger, "action": "read_symbol", "symbol": COUNTER_SYMBOL, "comparator": {"equals": 0, "signed": True}},
            {"device": debugger, "action": "dump_memory", "symbol": COUNTER_SYMBOL, "output_path": DUMP_OUTPUT},
            {"device": debugger, "action": "debug_stop"},
            {"device": debugger, "action": "reset", "mode": "run"},
            {"device": debugger, "action": "delay", "duration_ms": SETTLE_MS},
            {"device": debugger, "action": "debug_start", "image_path": image, "mode": "attach"},
            {"device": debugger, "action": "read_symbol", "symbol": COUNTER_SYMBOL, "size_bytes": COUNTER_SIZE_BYTES, "comparator": {"range": COUNTING_RANGE}},
            {"device": debugger, "action": "read_symbol", "symbol": COUNTER_SYMBOL, "comparator": {"range": COUNTING_RANGE, "signed": True}},
            {"device": debugger, "action": "read_symbol", "symbol": COUNTER_SYMBOL, "comparator": {"mask": UPPER_HALF_MASK, "equals": 0}},
            {"device": debugger, "action": "read_symbol", "symbol": COUNTER_SYMBOL},
            {"device": debugger, "action": "debug_stop"},
            {"device": debugger, "action": "reset", "mode": "run"},
        ],
    )

    status, report = bench.document("test-reactor", "--test-config", plan)

    assert status == 0, report
    assert report["ok"] is True, report
    assert [record["action"] for record in report["steps"]] == [
        "debug_start",
        "run_until_breakpoint",
        "read_symbol",
        "read_symbol",
        "dump_memory",
        "debug_stop",
        "reset",
        "delay",
        "debug_start",
        "read_symbol",
        "read_symbol",
        "read_symbol",
        "read_symbol",
        "debug_stop",
        "reset",
    ], report["steps"]
    for record in report["steps"]:
        assert record["result"]["ok"] is True, record
    # Both sessions were stopped by the plan's own steps, so the run owns
    # nothing at the end and cleanup has nothing to close.
    assert report["cleanup"] == [], report["cleanup"]
    assert report["cleanup_ok"] is True, report

    _, reached, at_main, at_main_signed, dumped, _, _, waited, _, counting, counting_signed, masked, plain, _, _ = results(report)

    assert reached["summary"] == "Target stopped at the expected breakpoint.", reached
    assert reached["stop_reason"] == "breakpoint_hit", reached
    assert reached["stop"]["frame"]["function"] == ENTRY_FUNCTION, reached["stop"]

    for claim in (at_main, at_main_signed):
        assert claim["tool"] == "test_reactor", claim
        assert claim["summary"] == "The symbol held the expected value.", claim
        assert claim["symbol"] == COUNTER_SYMBOL, claim
        assert claim["size_bytes"] == COUNTER_SIZE_BYTES, claim
        assert claim["captured_value"] == 0, claim
        assert bytes.fromhex(claim["hex"]) == bytes(COUNTER_SIZE_BYTES), claim
    assert at_main["reading"] == "value_unsigned", at_main
    assert at_main_signed["reading"] == "value_signed", at_main_signed
    assert at_main["address"] == at_main_signed["address"], (at_main, at_main_signed)

    assert dumped["symbol"] == COUNTER_SYMBOL, dumped
    assert dumped["size_bytes"] == COUNTER_SIZE_BYTES, dumped
    assert dumped["address"] == at_main["address"], (dumped, at_main)
    assert dumped["output"]["path"] == DUMP_OUTPUT, dumped["output"]
    assert dumped_file.is_file(), f"the dump step reported success and wrote no file at {DUMP_OUTPUT}"
    base_address, payload = intel_hex_payload(dumped_file.read_text(encoding="ascii"))
    assert base_address == int(at_main["address"], 16), (hex(base_address), at_main["address"])
    assert payload == bytes(COUNTER_SIZE_BYTES), payload.hex()

    assert waited["summary"] == "Test plan waited.", waited
    assert waited["debugger"] == debugger, waited
    assert waited["duration_ms"] == SETTLE_MS, waited
    assert "stop_requested" not in waited, waited
    assert report["steps"][7]["elapsed_ms"] >= SETTLE_MS, report["steps"][7]

    for claim in (counting, counting_signed):
        assert claim["summary"] == "The symbol's value fell inside the expected range.", claim
        assert COUNTING_RANGE["min"] <= claim["captured_value"] <= COUNTING_RANGE["max"], claim
    assert counting["reading"] == "value_unsigned", counting
    assert counting_signed["reading"] == "value_signed", counting_signed
    assert masked["summary"] == "The symbol's masked bits held the expected value.", masked
    assert masked["reading"] == "value_unsigned", masked
    assert masked["masked_value"] == 0, masked

    # A read with no claim is the tool's own answer, unchanged.
    assert plain["tool"] == "debug_symbol_value", plain
    assert "comparator" not in plain, plain
    assert plain["symbol"] == COUNTER_SYMBOL, plain

    # One halted core, one word: every read of the attached session saw the same
    # value at the same address the reads at `main` reported.
    held = {counting["captured_value"], counting_signed["captured_value"], masked["captured_value"], plain["value_unsigned"]}
    assert len(held) == 1, (counting, counting_signed, masked, plain)
    assert {counting["address"], counting_signed["address"], masked["address"], plain["address"]} == {at_main["address"]}, (at_main, plain)


RED_CLAIMS = [
    pytest.param({"comparator": {"equals": 1}}, "comparator_unmet", "The symbol did not hold the expected value.", id="equals"),
    pytest.param(
        {"comparator": {"range": {"min": 1, "max": 0xFFFFFFFF}}},
        "comparator_unmet",
        "The symbol's value fell outside the expected range.",
        id="range",
    ),
    pytest.param({"comparator": {"mask": 1, "equals": 1}}, "comparator_unmet", "The symbol's masked bits did not hold the expected value.", id="mask"),
    pytest.param({"size_bytes": 2, "comparator": {"equals": 0}}, "symbol_size_mismatch", "The symbol is not the width this step declared.", id="width"),
]


@pytest.mark.parametrize(("claim", "error_type", "summary"), RED_CLAIMS)
def test_a_claim_the_counter_does_not_meet_fails_the_run_at_that_step_and_the_session_is_still_closed(
    bench: Bench, firmware: Path, claim: dict, error_type: str, summary: str
) -> None:
    """Each way a `read_symbol` step can be red, against a value the board fixes at zero.

    At `main` after a reset into halt the counter is zero, so each claim here is
    one the board contradicts: a different value, a range that leaves zero out,
    a mask whose bit is clear, and a width the declaration does not have. Each
    has to fail the run at the read, as the board's answer rather than as a
    refusal, and carry the value it judged, so the red result says what the
    board held without anybody reproducing it by hand.

    Catches a failed claim that leaves the session open: the plan's own
    `debug_stop` never runs, so the run's cleanup has to close the session it
    opened, and the failure has to trigger the recovery the configuration names.
    """
    debugger = bench.debugger_name()
    plan = write_plan(
        bench,
        f"reactor-steps-red-{error_type}-{'-'.join(sorted(claim.get('comparator', {})))}",
        5,
        [
            {"device": debugger, "action": "debug_start", "image_path": firmware.relative_to(bench.project).as_posix(), "mode": "reset_halt"},
            {"device": debugger, "action": "run_until_breakpoint", "location": ENTRY_FUNCTION, "timeout_s": RESUME_TIMEOUT_S},
            {"device": debugger, "action": "read_symbol", "symbol": COUNTER_SYMBOL, **claim},
            {"device": debugger, "action": "debug_stop"},
            {"device": debugger, "action": "reset", "mode": "run"},
        ],
    )

    status, report = bench.document("test-reactor", "--test-config", plan)

    assert status == 1, report
    assert report["ok"] is False, report
    assert report["summary"].startswith("Test reactor sequence failed."), report["summary"]
    assert report["failed_step"] == 3, report
    assert report["step_error_type"] == error_type, report
    assert report["error_type"] == error_type, report
    assert [record["action"] for record in report["steps"]] == ["debug_start", "run_until_breakpoint", "read_symbol"], report["steps"]
    read = report["steps"][2]["result"]
    assert read["ok"] is False, read
    assert read["error_type"] == error_type, read
    assert read["summary"] == summary, read
    assert read["symbol"] == COUNTER_SYMBOL, read
    assert read["value_unsigned"] == 0, read
    if error_type == "symbol_size_mismatch":
        assert read["expected_size_bytes"] == claim["size_bytes"], read
        assert read["size_bytes"] == COUNTER_SIZE_BYTES, read
    else:
        assert read["captured_value"] == 0, read
    if "mask" in claim.get("comparator", {}):
        assert read["masked_value"] == 0, read

    # The session the plan opened and never reached its own stop for.
    assert [(entry.get("debugger"), entry["action"]) for entry in report["cleanup"]] == [(debugger, "debug_stop")], report["cleanup"]
    assert report["cleanup"][0]["result"]["ok"] is True, report["cleanup"]
    assert report["cleanup_ok"] is True, report
    recovery = report["recovery"]
    assert recovery["attempted"] is True, recovery
    assert recovery["outcome"] == "recovered", recovery


def test_a_repeat_bounded_by_its_count_resets_and_reads_the_banner_that_many_times_and_the_port_closes_and_reopens(bench: Bench) -> None:
    """`repeat` on its count, the `pattern` comparator, `delay` on a port, and `uart_close` as a step.

    Three resets inside one block, each followed by a read that waits for the
    banner as a pattern and a short wait routed to the port. Then the port is
    closed as a step, opened again and read once more, which is the case where a
    plan closes its own line: the reopen has to be a fresh session and the run's
    cleanup has to find nothing left to close.

    The duration bound is set far beyond what three iterations take, so the
    block has to end on its count and say so. Catches a block that runs one
    iteration too many or too few, an iteration that reads the previous one's
    banner rather than its own reset's, and a closed port the run still thinks
    it owns.
    """
    debugger = bench.debugger_name()
    port = bench.com_port_name()
    read_banner = {"device": port, "action": "uart_read", "comparator": {"pattern": BANNER_PATTERN}, "timeout_s": BANNER_TIMEOUT_S}
    plan = write_plan(
        bench,
        "reactor-steps-banner-by-count",
        4,
        [
            {"device": port, "action": "uart_open", "clear_buffer": True},
            {
                "action": "repeat",
                "count": 3,
                "duration_s": 600,
                "steps": [
                    {"device": debugger, "action": "reset", "mode": "run"},
                    read_banner,
                    {"device": port, "action": "delay", "duration_ms": 100},
                ],
            },
            {"device": port, "action": "uart_close"},
            {"device": port, "action": "uart_open", "clear_buffer": True},
            {"device": debugger, "action": "reset", "mode": "run"},
            read_banner,
            {"device": port, "action": "uart_close"},
        ],
    )

    status, report = bench.document("test-reactor", "--test-config", plan)

    assert status == 0, report
    assert report["ok"] is True, report
    assert [record["action"] for record in report["steps"]] == [
        "uart_open",
        "repeat",
        "uart_close",
        "uart_open",
        "reset",
        "uart_read",
        "uart_close",
    ], report["steps"]
    for record in report["steps"]:
        assert record["result"]["ok"] is True, record

    block = report["steps"][1]
    repeated = block["result"]
    assert repeated["summary"] == "Repeat block ran 3 iteration(s) and ended on its count bound.", repeated
    assert repeated["exit_reason"] == "count", repeated
    assert repeated["iterations_run"] == 3, repeated
    assert repeated["count"] == 3, repeated
    assert repeated["duration_s"] == 600, repeated
    assert [iteration["iteration"] for iteration in block["iterations"]] == [1, 2, 3], block["iterations"]
    for iteration in block["iterations"]:
        assert [nested["action"] for nested in iteration["steps"]] == ["reset", "uart_read", "delay"], iteration
        reset, banner, waited = (nested["result"] for nested in iteration["steps"])
        assert reset["ok"] is True, iteration
        assert banner["ok"] is True, banner
        assert banner["summary"] == "Expected pattern matched the COM port output.", banner
        assert banner["matched_text"]["text"] == BANNER_TEXT, banner
        assert waited["summary"] == "Test plan waited.", waited
        assert waited["port_id"] == port, waited
        assert waited["duration_ms"] == 100, waited

    reopened = report["steps"][3]["result"]
    assert reopened["already_active"] is False, reopened
    last_banner = report["steps"][5]["result"]
    assert last_banner["matched_text"]["text"] == BANNER_TEXT, last_banner

    assert report["cleanup"] == [], report["cleanup"]
    assert report["cleanup_ok"] is True, report


def test_a_repeat_bounded_by_a_duration_ends_between_iterations_once_the_duration_has_passed(bench: Bench) -> None:
    """`repeat` on its time bound, which is asked at the bottom of the loop and nowhere else.

    The block resets the board and reads its banner until the duration has
    passed. It must end on that bound and say so, it must have run at least as
    long as it was asked, and every iteration but the last must have ended
    before the bound, because the bound is only asked between iterations: a
    block that ended early, or one that went round again after its time was up,
    is caught by one of the two.
    """
    debugger = bench.debugger_name()
    port = bench.com_port_name()
    plan = write_plan(
        bench,
        "reactor-steps-banner-for-a-while",
        4,
        [
            {"device": port, "action": "uart_open", "clear_buffer": True},
            {
                "action": "repeat",
                "duration_s": REPEAT_DURATION_S,
                "steps": [
                    {"device": debugger, "action": "reset", "mode": "run"},
                    {"device": port, "action": "uart_read", "comparator": {"pattern": BANNER_PATTERN}, "timeout_s": BANNER_TIMEOUT_S},
                ],
            },
            {"device": port, "action": "uart_close"},
        ],
    )

    status, report = bench.document("test-reactor", "--test-config", plan)

    assert status == 0, report
    assert report["ok"] is True, report
    block = report["steps"][1]
    repeated = block["result"]
    iterations = block["iterations"]
    assert repeated["exit_reason"] == "duration_s", repeated
    assert "count" not in repeated, repeated
    assert repeated["duration_s"] == REPEAT_DURATION_S, repeated
    assert repeated["iterations_run"] == len(iterations) >= 1, (repeated, iterations)
    assert repeated["summary"] == f"Repeat block ran {len(iterations)} iteration(s) and ended on its duration_s bound.", repeated
    assert repeated["elapsed_s"] >= REPEAT_DURATION_S, repeated
    for iteration in iterations:
        assert [nested["action"] for nested in iteration["steps"]] == ["reset", "uart_read"], iteration
        for nested in iteration["steps"]:
            assert nested["result"]["ok"] is True, nested
    # The steps of every iteration before the last took less than the bound
    # between them. Each record's time is rounded to the millisecond, which is
    # the one allowance made.
    before_the_last = [nested["elapsed_ms"] for iteration in iterations[:-1] for nested in iteration["steps"]]
    assert sum(before_the_last) < REPEAT_DURATION_S * 1000 + len(before_the_last), iterations
    assert report["cleanup"] == [], report["cleanup"]
