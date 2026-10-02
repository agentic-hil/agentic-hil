"""The USB-UART adapter's own claims, on the board: its listing, DTR, and a write the line cannot carry in time.

The rest of the serial suite runs over this adapter too, as the second of the
two lines `OVER_BOTH_LINES` gives every body in `test_bench_serial_peer.py`
and `test_bench_device_access.py`. What is here is what only this line can
show, because only on it does the board see more of the host than its bytes:

* the listing. The product's inventory names the adapter by the USB identity
  its descriptor publishes, which this module reads back out of sysfs and
  compares, the serial number without ever printing it.
* DTR. The adapter's DTR# output is wired to PB12, which the peer image
  watches (`firmware/peer.c`, the modem line monitor): the pin's level, the
  edges counted, and the shortest and longest low pulse in microseconds. The
  monitor is read over the probe's own port, so reading it never touches the
  line under test, and DTR is read while the adapter's session is open and
  after it ended.
* a write longer than the line carries within `write_timeout_s`. The peer and
  the adapter are moved to 9600 and to 1200 baud, and what reached the peer is
  its own count and CRC-32.

Everything goes through the product, as in the rest of the tier: the adapter
is opened by nothing but a session of `agentic-hil mcp-stdio`. Each test keeps
what it measured on its JUnit record (`record_property`), whatever the outcome,
and never a serial number.
"""

from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Callable, Iterator
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

import pytest

from .conftest import (
    BENCH_ONLY,
    CHECKOUT_SOURCES,
    USB_UART_ENV,
    USB_UART_PRODUCT_IDS,
    USB_UART_VENDOR_ID,
    Bench,
    BoardImages,
)
from .test_bench_serial_peer import (
    ANSWER_TIMEOUT_S,
    PING_PONG,
    STATS,
    Peer,
    Server,
    Servers,
    Tally,
    quarantine_left,
    read_bytes_until,
)

pytestmark = [pytest.mark.bench, pytest.mark.usb_uart, BENCH_ONLY]

RecordProperty = Callable[[str, object], None]

# Where the kernel publishes a tty's device, and the directory of stable names
# the configured entry opens the adapter by.
SYSFS_TTY = Path("/sys/class/tty")
BY_ID = "/dev/serial/by-id/"

# The peer's answer to `@peer modem`, as `firmware/peer.c` documents it.
MODEM = re.compile(
    r"@peer ok modem level=(?P<level>low|high) falls=(?P<falls>\d+) rises=(?P<rises>\d+)"
    r" shortest=(?P<shortest>\d+|none) longest=(?P<longest>\d+|none)"
)
# How many sessions the released line is opened, used and closed for: enough
# opens for the spread of what each one does to the line to show.
DTR_CYCLES = 20

# The framing every entry opens with: pyserial's eight data bits, no parity and
# one stop bit, which the product never changes, and the peer's own.
BITS_PER_BYTE = 10
# A stats request led by a line ending, which finishes the payload's cut last
# line, so the request is a line of its own and those two bytes are payload.
STATS_LINE = "\r\n@peer stats\r\n"
BACKPRESSURE = [
    pytest.param(9600, None, id="9600-baud-default-cap"),
    pytest.param(9600, 16384, id="9600-baud-16384-cap"),
    pytest.param(1200, None, id="1200-baud-default-cap"),
    pytest.param(1200, 16384, id="1200-baud-16384-cap"),
]
SCHEMA = CHECKOUT_SOURCES / "agentic_hil" / "schemas" / "config.schema.json"


@pytest.fixture(scope="module")
def bench(usb_uart_bench: Bench) -> Bench:
    """The session's configuration with the adapter declared beside the probe's own port, driving the adapter."""
    return usb_uart_bench


# The fixtures of `test_bench_serial_peer.py`, copied as `test_bench_can_peer.py`
# copies them: a fixture follows the module it is defined in, and these follow
# this module's `bench`.


@pytest.fixture(autouse=True)
def bench_is_left_clear(bench: Bench) -> Iterator[None]:
    """Whatever a test here quarantined under the session's configuration, cleared through `recover` and failed for."""
    yield
    left = quarantine_left(bench, bench.config)
    assert left is None, left


@pytest.fixture
def port(bench: Bench) -> str:
    return bench.com_port_name()


@pytest.fixture
def servers(bench: Bench, bench_is_left_clear: None, port: str, tmp_path: Path) -> Iterator[Servers]:
    """Servers for one test, closed afterwards, and the configurations they ran against cleared and removed."""
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
    """The peer, reset to its boot defaults before the test, over the adapter's line."""
    answering = Peer(servers, port)
    answering.reset()
    return answering


@pytest.fixture
def probe(configured_bench: Bench, servers: Servers) -> Peer:
    """The peer's other line, the probe's own port, which the modem line monitor is read over."""
    return Peer(servers, configured_bench.com_port_name())


@dataclass(frozen=True)
class UsbDescriptor:
    """What the kernel read out of a USB device's descriptor."""

    vendor_id: int
    product_id: int
    serial_number: str | None


def descriptor_behind(node: str) -> UsbDescriptor:
    """The USB device a tty node belongs to, as sysfs publishes it: the first directory above the tty with an `idVendor`."""
    device = (SYSFS_TTY / Path(os.path.realpath(node)).name / "device").resolve()
    for directory in (device, *device.parents):
        if (directory / "idVendor").is_file():
            serial = directory / "serial"
            return UsbDescriptor(
                int((directory / "idVendor").read_text(encoding="ascii").strip(), 16),
                int((directory / "idProduct").read_text(encoding="ascii").strip(), 16),
                serial.read_text(encoding="utf-8").strip() if serial.is_file() else None,
            )
    pytest.fail(f"sysfs names no USB device behind {node}", pytrace=False)


@dataclass(frozen=True)
class Monitor:
    """One reading of the peer's modem line monitor: PB12, which the adapter's DTR# drives, low while DTR is asserted."""

    level: str
    falls: int
    rises: int
    shortest_us: int | None
    longest_us: int | None

    @property
    def asserted(self) -> bool:
        return self.level == "low"

    def evidence(self) -> dict[str, object]:
        return {"level": self.level, "falls": self.falls, "rises": self.rises, "shortest_us": self.shortest_us, "longest_us": self.longest_us}


def width(said: str) -> int | None:
    return None if said == "none" else int(said)


def monitor(probe: Peer, server: Server) -> Monitor:
    """The monitor as the peer reports it over the probe's own port."""
    answers = probe.control("modem", server=server)
    found = MODEM.fullmatch(answers[0])
    assert found is not None, answers
    return Monitor(found["level"], int(found["falls"]), int(found["rises"]), width(found["shortest"]), width(found["longest"]))


def cleared(probe: Peer, server: Server) -> None:
    answers = probe.control("modem clear", server=server)
    assert answers == ["@peer ok modem clear"], answers


def com_port_default(name: str) -> object:
    """One field's default for a `com_ports` entry, as the product's schema states it."""
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    return schema["properties"]["com_ports"]["additionalProperties"]["properties"][name]["default"]


def numbered_lines(size: int) -> bytes:
    """``size`` bytes of numbered 64-byte lines, the last one cut where the size ends."""
    lines = b"".join(f"{number:06d} {'.' * 55}\r\n".encode("ascii") for number in range(size // 64 + 1))
    return lines[:size]


def stats_within(server: Server, port: str, timeout_s: float) -> tuple[re.Match[str] | None, bytes, list[str]]:
    """The peer's statistics answer as `com_read` brings it in within ``timeout_s``, on an open session.

    Never raises, so a test can keep what it saw before it fails: the answer
    or None, everything the line said, and every read that was refused.
    """
    said = b""
    refused: list[str] = []
    deadline = time.monotonic() + timeout_s
    while True:
        found = STATS.search(said.decode("latin-1"))
        remaining = deadline - time.monotonic()
        if found is not None or remaining <= 0:
            return found, said, refused
        read = server.tool("com_read", port_id=port, wait_timeout_s=round(remaining, 3))
        if read.get("ok") is not True:
            refused.append(f"{read.get('error_type')}: {read.get('summary')}")
            return None, said, refused
        said += bytes.fromhex(read["data"]["hex"])


# ---------------------------------------------------------------------------
# The listing.


def test_the_inventory_lists_the_adapter_by_its_usb_identity(servers: Servers, port: str, record_property: RecordProperty) -> None:
    """`com_ports_list` names the adapter by the identity its USB descriptor publishes.

    The tool's contract: "List configured named COM ports and detected host
    serial ports." The knowledge text on `com_ports`: "`vid`/`pid` name which
    kind of adapter it is, which is what makes a serial mean a unit at all and
    is the only identity an adapter that publishes no serial can have." So the
    detected port at the adapter's node carries FTDI's vendor id, the product
    id of its part and its serial number, each the one the kernel read out of
    the adapter's descriptor, and its `/dev/serial/by-id` name, which the
    configured entry opens it by, leads to that node. Values that carry the
    serial number are compared and never printed.
    """
    node = os.environ[USB_UART_ENV]
    published = descriptor_behind(node)
    server = servers()

    listed = server.tool("com_ports_list")
    available = listed["available_com_ports"]
    assert available.get("ok") is True, {key: available.get(key) for key in ("error_type", "summary", "backend_error")}
    at_the_node = [entry for entry in available["ports"] if entry.get("device") == node]
    assert len(at_the_node) == 1, f"{len(at_the_node)} detected port(s) at {node}"
    entry = at_the_node[0]
    vendor_id, product_id = entry.get("vid"), entry.get("pid")
    record_property("usb_identity", json.dumps({"device": node, "vid": vendor_id, "pid": product_id}))

    assert vendor_id == published.vendor_id == USB_UART_VENDOR_ID
    assert product_id == published.product_id
    assert product_id in USB_UART_PRODUCT_IDS
    publishes_a_serial = bool(published.serial_number)
    assert publishes_a_serial, "the adapter's descriptor publishes no serial number, and this bench binds its entry by one"
    same_serial = entry.get("serial_number") == published.serial_number
    assert same_serial, "the inventory's serial number for the adapter is not the one its descriptor publishes"
    stable = entry.get("stable_device")
    by_id = isinstance(stable, str) and stable.startswith(BY_ID)
    assert by_id, f"the inventory gives the adapter no name under {BY_ID}"
    leads_to_the_node = os.path.realpath(stable) == os.path.realpath(node)
    assert leads_to_the_node, f"the adapter's name under {BY_ID} does not lead to {node}"
    opened_by_it = listed["ports"][port].get("device") == stable
    assert opened_by_it, f"the configured entry `{port}` does not open the adapter by its name under {BY_ID}"


# ---------------------------------------------------------------------------
# DTR, on PB12.


def test_with_assert_dtr_true_dtr_is_asserted_for_as_long_as_the_session_is_open(
    servers: Servers, peer: Peer, probe: Peer, port: str, record_property: RecordProperty
) -> None:
    """DTR asserted by the open and held through the session's writes and reads.

    The schema's `assert_dtr`: "Whether DTR is asserted while the port is
    open." Its default is true, which the adapter's entry leaves in place, for
    when "the peer needs DTR to talk". So from `com_session_start` to
    `com_session_stop` PB12 is low and has no edge, across a stimulus and the
    read of its answer. What DTR does before the open and after the stop the
    product does not say, and those readings are kept as evidence only.
    """
    peer.start_responder(PING_PONG)
    server = servers()
    assert server.tool("com_ports_list")["ports"][port]["assert_dtr"] is True
    cleared(probe, server)
    before = monitor(probe, server)

    started = server.tool("com_session_start", port_id=port)
    assert started["ok"] is True, started
    opened = monitor(probe, server)
    assert server.tool("com_write", port_id=port, text="PING\r\n")["ok"] is True
    answered, reads = read_bytes_until(server, port, b"PONG\r\n")
    assert answered == b"PONG\r\n", (answered, reads)
    talked = monitor(probe, server)
    stopped = server.tool("com_session_stop", port_id=port)
    assert stopped["ok"] is True, stopped
    ended = monitor(probe, server)

    evidence = json.dumps({
        "before_the_open": before.evidence(),
        "after_the_open": opened.evidence(),
        "after_a_stimulus_and_its_answer": talked.evidence(),
        "after_the_stop": ended.evidence(),
    })
    record_property("dtr_asserted", evidence)
    assert opened.asserted and talked.asserted, evidence
    assert (talked.falls, talked.rises) == (opened.falls, opened.rises), evidence
    assert peer.received(server=server) == Tally.of(b"PING\r\n")


def test_with_assert_dtr_false_the_open_pulses_dtr_at_most_once_and_the_session_leaves_it_released(
    servers: Servers, peer: Peer, probe: Peer, port: str, record_property: RecordProperty
) -> None:
    """Twenty sessions with DTR released: at most one pulse, during the open, and none after it.

    The schema's `assert_dtr`: "Set false to keep DTR released for the
    session", and "False is not a proof that the open left DTR alone: on
    Linux, measured with an FT232R over 65 opens, the open itself asserted DTR
    once for 239 to 943 microseconds before it was released for the rest of
    the session and at close". So with `assert_dtr: false` each open moves
    PB12 at most once, a fall and its rise, and the line reads released (PB12
    high) once the open has answered; a stimulus, the read of its answer and
    the stop move it no further, twenty times over. The schema states no bound
    on the pulse's width beyond what it measured, so the width is kept and not
    asserted. The monitor is cleared before each open, so every reading is one
    session's own, and every reading is kept: the edges counted, and the
    shortest and longest low pulse the peer timed.
    """
    peer.start_responder(PING_PONG)
    server = servers(servers.variant("dtr-released", assert_dtr=False))
    assert server.tool("com_ports_list")["ports"][port]["assert_dtr"] is False
    cleared(probe, server)

    readings: list[dict[str, object]] = []
    broken: list[dict[str, object]] = []

    def read(at: str, cycle: int | None, opened: Monitor | None = None) -> Monitor:
        reading = monitor(probe, server)
        readings.append({"at": at, "cycle": cycle, **reading.evidence()})
        if cycle is None:
            return reading
        # After the open: one pulse at most, and released. Later in the same
        # session: what the open left, so nothing moved since.
        held = (reading.falls == reading.rises <= 1 and not reading.asserted) if opened is None else reading == opened
        if not held:
            broken.append(readings[-1])
        return reading

    read("before the first open", None)
    for cycle in range(DTR_CYCLES):
        cleared(probe, server)
        started = server.tool("com_session_start", port_id=port)
        assert started["ok"] is True, started
        opened = read("after the open", cycle)
        assert server.tool("com_write", port_id=port, text="PING\r\n")["ok"] is True
        answered, reads = read_bytes_until(server, port, b"PONG\r\n")
        assert answered == b"PONG\r\n", (answered, reads)
        read("after a stimulus and its answer", cycle, opened)
        stopped = server.tool("com_session_stop", port_id=port)
        assert stopped["ok"] is True, stopped
        read("after the stop", cycle, opened)

    record_property("dtr_released", json.dumps({"cycles": DTR_CYCLES, "readings": readings}))
    assert not broken, json.dumps({"cycles": DTR_CYCLES, "first_broken": broken[0], "last": readings[-1]})
    assert peer.received(server=server) == Tally.of(b"PING\r\n" * DTR_CYCLES)


# ---------------------------------------------------------------------------
# A write the line cannot carry within write_timeout_s.


@pytest.mark.parametrize(("rate", "cap"), BACKPRESSURE)
def test_a_write_the_line_cannot_carry_in_time_is_short_by_exactly_what_never_reached_the_peer(
    bench: Bench, servers: Servers, peer: Peer, probe: Peer, port: str, rate: int, cap: int | None, record_property: RecordProperty
) -> None:
    """`serial_write_incomplete`, with the count the peer received, and the session still usable.

    The product's own wording for the answer: "COM port write was short: N of
    M byte(s) reached the line", and its first likely cause, "configured
    write_timeout_s is too short for this payload size and baudrate". The
    changelog, for #275: "a write still short after the retry fails as
    `serial_write_incomplete` carrying both counts, and because the outcome is
    confirmed rather than unknown, the lease is recorded rather than
    quarantined".

    The peer and the entry are moved to a rate at which a payload as long as
    the entry allows needs several times `write_timeout_s` (the schema's
    default) on the wire, once under the default `max_write_bytes` and once
    under a larger one. Then the answer is `serial_write_incomplete`; its
    `bytes_written` and `data` are exactly what the peer counted and
    checksummed once the line drained, asked over the same session, which is
    therefore still usable; and nothing is left quarantined. How long the
    `com_write` took is kept as evidence and not asserted: the product states
    no bound for it.
    """
    peer.configure(f"baud {rate}")
    changes: dict[str, object] = {"baudrate": rate}
    if cap is not None:
        changes["max_write_bytes"] = cap
    variant = servers.variant(f"backpressure-{rate}-{cap or 'default'}", **changes)
    server = servers(variant)
    entry = server.tool("com_ports_list")["ports"][port]
    limit = entry["max_write_bytes"]
    assert (entry["baudrate"], limit) == (rate, cap or com_port_default("max_write_bytes")), entry
    timeout_s = float(com_port_default("write_timeout_s"))
    payload = numbered_lines(limit)
    on_the_wire_s = len(payload) * BITS_PER_BYTE / rate
    assert on_the_wire_s > 2 * timeout_s, (on_the_wire_s, timeout_s)
    drained_s = on_the_wire_s + ANSWER_TIMEOUT_S

    opened = server.tool("com_session_start", port_id=port)
    assert opened["ok"] is True, opened
    began = time.monotonic()
    written = server.tool("com_write", port_id=port, text=payload.decode("ascii"))
    took_s = time.monotonic() - began
    active_after = server.tool("com_ports_list")["ports"][port].get("session_active")
    evidence: dict[str, object] = {
        "baudrate": rate,
        "write_timeout_s": timeout_s,
        "max_write_bytes": limit,
        "bytes_requested": len(payload),
        "on_the_wire_s": round(on_the_wire_s, 3),
        "com_write_s": round(took_s, 3),
        "answer": {
            key: written.get(key)
            for key in ("ok", "error_type", "summary", "bytes_written", "bytes_requested", "backend_error", "side_effect_status", "retry_safe", "quarantined")
        },
        "session_active_after_the_write": active_after,
    }
    # Kept at once, so a run that fails further down still says what the write answered.
    record_property("short_write_answer", json.dumps(evidence))

    # What reached the peer, asked over the same session: the request written
    # after the payload, and its answer read back once the line has drained.
    asked = server.tool("com_write", port_id=port, text=STATS_LINE)
    counted, said, refused = stats_within(server, port, drained_s) if asked.get("ok") is True else (None, b"", [])
    stopped = server.tool("com_session_stop", port_id=port)
    evidence["over_the_same_session"] = {
        "request": {key: asked.get(key) for key in ("ok", "error_type", "summary")},
        "answer": counted.group(0) if counted is not None else None,
        "line_said": None if counted is not None else said[-200:].decode("latin-1"),
        "reads_refused": refused,
        "stop": {key: stopped.get(key) for key in ("ok", "error_type", "summary")},
    }
    if counted is None:
        # Whether the peer still answers on the probe's own line, at its own rate.
        try:
            evidence["probe_line"] = probe.control("stats", server=server)[0]
        except AssertionError as error:
            evidence["probe_line"] = str(error).splitlines()[0][:300] if str(error) else type(error).__name__
    server.close()
    left = quarantine_left(bench, variant)
    evidence["left"] = left
    afterwards = servers(variant)
    if counted is None:
        # What reached the peer, asked from a new server once the bench is clear.
        try:
            evidence["from_a_new_server"] = peer.exchange([(STATS_LINE, "stats")], server=afterwards, answer_timeout_s=drained_s)[0]
        except AssertionError as error:
            evidence["from_a_new_server"] = str(error).splitlines()[0][:300] if str(error) else type(error).__name__
    with suppress(AssertionError):
        peer.reset(server=afterwards)
    evidence["all_of_it_would_be"] = {"bytes": len(payload) + 2, "crc32": Tally.of(payload + b"\r\n").crc32}
    report = json.dumps(evidence)
    record_property("short_write", report)

    assert written.get("error_type") == "serial_write_incomplete", report
    sent = written["bytes_written"]
    assert isinstance(sent, int) and 0 < sent < len(payload), report
    assert written["bytes_requested"] == len(payload), report
    assert written["summary"].startswith(f"COM port write was short: {sent} of {len(payload)} byte(s) reached the line"), report
    assert written["likely_causes"][0] == "configured write_timeout_s is too short for this payload size and baudrate", report
    assert bytes.fromhex(written["data"]["hex"]) == payload[:sent], report
    assert (written["side_effect_committed"], written["side_effect_status"]) == (True, "committed"), report
    assert written.get("quarantined") is not True, report
    assert active_after is True and asked.get("ok") is True, report
    assert counted is not None, report
    assert Tally(int(counted["bytes"]), counted["crc32"]) == Tally.of(payload[:sent] + b"\r\n"), report
    assert int(counted["lost"]) == 0, report
    assert stopped.get("ok") is True, report
    assert left is None, report
