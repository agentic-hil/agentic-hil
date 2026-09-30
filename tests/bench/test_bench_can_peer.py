"""The board as a CAN peer: the CAN half of the bench's peer image, driven over its serial control line.

`firmware/peer.c` is the serial counterparty of `test_bench_serial_peer.py`,
and it carries a CAN half as well: bxCAN CAN1 on PB8 (RX) and PB9 (TX) at 500
kbit/s, answering frames out of a table the way the container tier's CAN peer
(`tests/container/can_peer.py`) answers them on a virtual interface. A rule is
written exactly as that peer reads one, `ID/DATA=ID/DATA`, and means exactly
what it means there: a standard data frame with the rule's identifier and
payload is answered with the rule's standard data frame, and every other frame,
extended and remote frames included, is heard and left unanswered. The frames a
rule stands for are read here by that peer's own parser, so the two tables
cannot come to mean different things.

Silent loopback. The controller is put in its loopback and silent modes at
once: it hears every frame it sends, drives nothing on the pin, and needs
neither a transceiver nor a second node. So the peer is asked to send frames
its own rules answer, it hears them and its own answers, and its statistics
say what happened on its side of the controller: frames sent, received and
answered, frames lost, frames that never left, and a digest of every frame
received, which the test computes from the frames it asked for. Normal mode,
the one a transceiver on those pins needs, is proven only as far as a board
with nothing on its CAN pins allows: the switch into it and back.

Everything goes through the product, through the same `Peer` the serial module
drives: every control line is a `com_write` over a session of its own and its
answer a `com_read`. The image goes on the board once for this module, and the
demo goes back after its last test, pass or fail.
"""

from __future__ import annotations

import re
import time
import zlib
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from container.can_peer import parse_rule

from .conftest import BENCH_ONLY, Bench, BoardImages
from .test_bench_serial_peer import Peer, Servers, Tally, quarantine_left

pytestmark = [pytest.mark.bench, BENCH_ONLY]

# SocketCAN's flags in a frame's identifier word, which the peer's digest keeps
# in the same places: bit 31 for an extended identifier, bit 30 for a remote frame.
EXTENDED_FLAG = 0x80000000
REMOTE_FLAG = 0x40000000

CAN_STATS = re.compile(
    r"@peer ok can stats mode=(?P<mode>\w+) queued=(?P<queued>\d+) sent=(?P<sent>\d+) received=(?P<received>\d+)"
    r" answered=(?P<answered>\d+) digest=(?P<digest>[0-9a-f]{8}) lost=(?P<lost>\d+) unsent=(?P<unsent>\d+)"
    r" tec=(?P<tec>\d+) rec=(?P<rec>\d+) state=(?P<state>\w+) fd=(?P<fd>\w+)"
)
TEXT_FIELDS = frozenset({"mode", "digest", "state", "fd"})

# How long frames asked for may take to leave the controller and be heard back.
SETTLE_TIMEOUT_S = 10.0

# The rules of the first proof, spelled the way `can_peer.py --reply` takes them.
RULE_TEXTS = (
    # The container tier's own plan rule.
    "0x123/01=0x124/02",
    # An empty payload, written as can_peer.py documents it.
    "0x200/=0x201/ff",
    # The highest and the lowest standard identifier and eight bytes each way,
    # without the prefix and in capitals, as int(x, 16) and bytes.fromhex read them.
    "7FF/0001020304050607=0/F8F9FAFBFCFDFEFF",
)

# Control lines the peer refuses, each with the one line it answers; none of
# them changes anything.
REFUSALS = (
    ("can", "@peer error can syntax"),
    ("can listen", "@peer error can syntax"),
    ("can mode", "@peer error can mode syntax"),
    ("can mode fd", "@peer error can mode syntax"),
    ("can rule 0x123/01", "@peer error can rule syntax"),
    ("can rule 0x123/0=0x124/02", "@peer error can rule syntax"),
    ("can rule 0x123/01=0x124/0g", "@peer error can rule syntax"),
    ("can rule extended 0x123/01=0x124/02", "@peer error can rule syntax"),
    # can_peer.py's frames are all standard, so a rule's identifiers end at 0x7ff.
    ("can rule 0x800/01=0x124/02", "@peer error can rule range"),
    ("can rule 0x123/01=0x800/02", "@peer error can rule range"),
    # Classic CAN carries eight bytes at most, and bxCAN has no CAN FD.
    ("can rule 0x123/010203040506070809=0x124/02", "@peer error can rule long"),
    ("can unrule 0x123/01", "@peer error can unrule absent"),
    ("can clear now", "@peer error can clear syntax"),
    ("can send 0x800/01", "@peer error can send range"),
    ("can send extended 0x20000000/01", "@peer error can send range"),
    ("can send 0x123/R9", "@peer error can send range"),
    ("can send 0x123/Rx", "@peer error can send syntax"),
    ("can send 0x123/01 0", "@peer error can send range"),
    ("can send 0x123/01 100001", "@peer error can send range"),
    ("can send 0x123/010203040506070809", "@peer error can send long"),
    ("can filter 0x123", "@peer error can filter syntax"),
    ("can filter 0x800 0x7ff", "@peer error can filter range"),
    ("can filter extended 0x20000000 0x1fffffff", "@peer error can filter range"),
    ("can last now", "@peer error can last syntax"),
    ("can stats now", "@peer error can stats syntax"),
)
RULES_AT_MOST = 8
FILTERS_AT_MOST = 14


@dataclass(frozen=True)
class Frame:
    """One CAN frame, as `@peer can send` spells it and as the peer's digest counts it."""

    identifier: int
    data: bytes = b""
    extended: bool = False
    remote: bool = False
    # A remote frame carries a length and no data.
    remote_length: int = 0

    @property
    def length(self) -> int:
        return self.remote_length if self.remote else len(self.data)

    def spelled(self) -> str:
        """`[extended ]ID/DATA`, with a remote frame's DATA written `R` and its length."""
        payload = f"R{self.remote_length}" if self.remote else self.data.hex()
        return f"{'extended ' if self.extended else ''}0x{self.identifier:x}/{payload}"

    def canonical(self) -> bytes:
        """What the digest covers: the identifier word, most significant byte first, the length, the data."""
        word = self.identifier | (EXTENDED_FLAG if self.extended else 0) | (REMOTE_FLAG if self.remote else 0)
        return word.to_bytes(4, "big") + bytes([self.length]) + (b"" if self.remote else self.data)


def digest(frames: Iterable[Frame]) -> str:
    """The peer's digest of the frames it received: each frame's CRC-32, as `zlib.crc32` computes it, summed modulo 2**32.

    A sum rather than one CRC over the stream, because in loopback a frame and
    the answers to the frames before it meet on the bus in an order no test
    decides. The sum is the same in every order, and it changes for a frame
    lost, added or altered.
    """
    return format(sum(zlib.crc32(frame.canonical()) for frame in frames) % 2**32, "08x")


def ruled(text: str) -> tuple[Frame, Frame]:
    """The frame a rule answers and the frame it answers with, read by the container peer's own parser."""
    (heard_id, heard_data), (answer_id, answer_data) = parse_rule(text)
    return Frame(heard_id, heard_data), Frame(answer_id, answer_data)


@dataclass(frozen=True)
class CanStats:
    """One `@peer can stats` answer."""

    mode: str
    queued: int
    sent: int
    received: int
    answered: int
    digest: str
    lost: int
    unsent: int
    tec: int
    rec: int
    state: str
    fd: str


def can_stats(peer: Peer) -> CanStats:
    answers = peer.control("can stats")
    found = CAN_STATS.fullmatch(answers[0])
    assert found is not None, answers
    return CanStats(**{name: value if name in TEXT_FIELDS else int(value) for name, value in found.groupdict().items()})


def settled(peer: Peer, sent: int) -> CanStats:
    """The statistics once ``sent`` frames have left the controller and nothing waits to be sent, or at the bound.

    The peer answers a send as soon as the frames are queued and sends them
    after that, so a test asks until they are all out. Whatever the count then
    says, the test's own assertions judge it.
    """
    deadline = time.monotonic() + SETTLE_TIMEOUT_S
    while True:
        stats = can_stats(peer)
        if (stats.sent >= sent and stats.queued == 0) or time.monotonic() > deadline:
            return stats


def sends(peer: Peer, *bursts: tuple[Frame, int]) -> list[Frame]:
    """Each frame sent its number of times, every send accepted; the frames in the order they were asked for."""
    answers = peer.control(*(f"can send {frame.spelled()} {count}" for frame, count in bursts))
    assert answers == [f"@peer ok can send {count}" for _, count in bursts], answers
    return [frame for frame, count in bursts for _ in range(count)]


def add_rules(peer: Peer, *texts: str) -> dict[Frame, Frame]:
    """The rules given to the peer, every one accepted; what each answers, and with what."""
    answers = peer.control(*(f"can rule {text}" for text in texts))
    assert answers == ["@peer ok can rule"] * len(texts), answers
    return dict(ruled(text) for text in texts)


def is_quiet(stats: CanStats) -> None:
    """Nothing lost, nothing left unsent, nothing waiting, and a controller without errors, as loopback always is."""
    assert (stats.lost, stats.unsent, stats.queued) == (0, 0, 0), stats
    assert (stats.tec, stats.rec, stats.state) == (0, 0, "active"), stats


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
    """Servers for one test, closed afterwards, since a session still open is a device still held."""
    started = Servers(bench, port, tmp_path)
    yield started
    problems = started.close_all()
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
    """The peer, reset to its boot defaults before the test, its CAN half included."""
    answering = Peer(servers, port)
    answering.reset()
    return answering


def test_frames_its_own_rules_answer_are_sent_heard_and_answered_in_silent_loopback_with_none_lost(peer: Peer) -> None:
    """The proof the image carries a working CAN peer: its rules answer every frame they name, and nothing is lost.

    Six hundred frames the rules name, in three bursts, each answered by its
    rule: twelve hundred frames on the bus, every one sent, heard back and
    counted, the digest of what arrived equal to the digest of what was asked
    for and answered. The statistics say that bxCAN has no CAN FD, rather than
    leaving it to be found out. And the CAN control lines, being control lines,
    left the serial statistics untouched.
    """
    assert peer.control("can mode loopback") == ["@peer ok can mode loopback"]
    rules = add_rules(peer, *RULE_TEXTS)

    heard = sends(peer, *zip(rules, (400, 100, 100), strict=True))
    answered = [rules[frame] for frame in heard]
    stats = settled(peer, len(heard) + len(answered))

    assert stats.mode == "loopback", stats
    assert (stats.sent, stats.received, stats.answered) == (len(heard) + len(answered), len(heard) + len(answered), len(answered)), stats
    assert stats.digest == digest(heard + answered), stats
    is_quiet(stats)
    assert stats.fd == "unsupported", stats
    assert peer.received() == Tally.of(b"")


def test_extended_and_remote_frames_are_heard_intact_and_never_answered(peer: Peer) -> None:
    """can_peer.py skips every extended and every remote frame, and so does this peer, whatever they carry.

    Frames of every kind the rule does not name: the rule's identifier and
    payload in an extended frame, the rule's identifier asked for in remote
    frames of two lengths, the container tier's extended frame, the highest and
    the lowest extended identifier, an extended remote frame, and standard
    frames that differ from the rule in one respect each. All of them are sent,
    heard and left unanswered, and each arrives as it was sent: the digest
    covers every identifier, flag, length and byte.
    """
    rule = "0x123/01=0x124/02"
    heard, answer = ruled(rule)
    frames = [
        Frame(heard.identifier, heard.data, extended=True),
        Frame(heard.identifier, remote=True),
        Frame(heard.identifier, remote=True, remote_length=len(heard.data)),
        Frame(0x1ABCDEF, b"\x11\x22", extended=True),
        Frame(0x1FFFFFFF, bytes(range(0xA0, 0xA8)), extended=True),
        Frame(0x0, extended=True),
        Frame(0x1ABCDEF, extended=True, remote=True, remote_length=8),
        Frame(heard.identifier, heard.data + b"\x00"),
        Frame(heard.identifier, b"\x02"),
        Frame(answer.identifier, heard.data),
    ]
    add_rules(peer, rule)

    sent = sends(peer, *((frame, 5) for frame in frames))
    stats = settled(peer, len(sent))

    assert (stats.sent, stats.received, stats.answered) == (len(sent), len(sent), 0), stats
    assert stats.digest == digest(sent), stats
    is_quiet(stats)
    assert peer.control("can last") == [f"@peer ok can last {frames[-1].spelled()}"]


def test_frames_the_acceptance_filters_reject_are_sent_and_never_heard_or_answered(peer: Peer) -> None:
    """A standard and an extended acceptance filter, and the frames each lets in and keeps out.

    The standard filter takes 0x120 to 0x12f and the extended one 0x1abcd00 to
    0x1abcdff. A frame a rule names but no filter takes is sent and never
    heard, so never answered, and an extended frame carrying the standard
    filter's identifier is kept out too, since a standard filter takes only
    standard frames. With the filters dropped, the frame kept out is heard and
    answered again.
    """
    rules = add_rules(peer, "0x123/01=0x124/02", "0x321/01=0x322/02")
    assert peer.control("can filter 0x120 0x7f0", "can filter extended 0x1abcd00 0x1ffff00") == ["@peer ok can filter"] * 2
    taken, kept_out = Frame(0x123, b"\x01"), Frame(0x321, b"\x01")
    extended_taken, extended_kept_out = Frame(0x1ABCDEF, b"\x11\x22", extended=True), Frame(0x123, b"\x01", extended=True)
    remote_taken = Frame(0x125, remote=True, remote_length=2)

    sent = sends(peer, (taken, 50), (kept_out, 50), (extended_taken, 5), (extended_kept_out, 5), (remote_taken, 5))
    heard = [frame for frame in sent if frame in {taken, extended_taken, remote_taken}] + [rules[taken]] * 50
    stats = settled(peer, len(sent) + 50)

    assert (stats.sent, stats.received, stats.answered) == (len(sent) + 50, len(heard), 50), stats
    assert stats.digest == digest(heard), stats
    is_quiet(stats)

    assert peer.control("can filter off") == ["@peer ok can filter off"]
    again = sends(peer, (kept_out, 10))
    heard += again + [rules[kept_out]] * 10
    stats = settled(peer, len(sent) + 50 + 20)

    assert (stats.sent, stats.received, stats.answered) == (len(sent) + 50 + 20, len(heard), 60), stats
    assert stats.digest == digest(heard), stats
    is_quiet(stats)


def test_a_later_rule_for_the_same_frame_replaces_the_earlier_and_a_rule_taken_back_answers_no_more(peer: Peer) -> None:
    """The table as can_peer.py keeps it: built with `dict()`, so the later rule for a frame is the one that answers.

    A rule taken back with `unrule`, and then every rule with `clear`, leaves
    the frames it named heard and unanswered.
    """
    rules = add_rules(peer, "0x123/01=0x124/02", "0x123/01=0x125/03", "0x200/=0x201/ff")
    first, second = Frame(0x123, b"\x01"), Frame(0x200)
    assert rules[first] == Frame(0x125, b"\x03")

    heard = sends(peer, (first, 3))
    heard += [rules[first]] * 3
    assert settled(peer, len(heard)).answered == 3

    assert peer.control("can unrule 0x200/") == ["@peer ok can unrule"]
    heard += sends(peer, (second, 3))
    assert settled(peer, len(heard)).answered == 3

    assert peer.control("can clear") == ["@peer ok can clear"]
    heard += sends(peer, (first, 3))
    stats = settled(peer, len(heard))

    assert (stats.sent, stats.received, stats.answered) == (len(heard), len(heard), 3), stats
    assert stats.digest == digest(heard), stats
    is_quiet(stats)


def test_the_mode_is_switched_by_control_line_between_silent_loopback_and_normal(peer: Peer) -> None:
    """Silent loopback from a reset, normal mode on request, and silent loopback again, by a line or by a reset.

    Nothing is sent in normal mode: with nothing on the pins no frame would be
    acknowledged, and with a bus on them the frame would reach it, so neither
    says anything this test can hold. What normal mode shows here is that the
    controller took it, which needs the receive pin to read recessive, and that
    the peer hears its own frames again once back in loopback.
    """
    assert can_stats(peer).mode == "loopback"
    assert peer.control("can mode normal") == ["@peer ok can mode normal"]
    assert can_stats(peer).mode == "normal"
    peer.reset()
    assert can_stats(peer).mode == "loopback"

    assert peer.control("can mode normal", "can mode loopback") == ["@peer ok can mode normal", "@peer ok can mode loopback"]
    rules = add_rules(peer, "0x123/01=0x124/02")
    heard = sends(peer, (Frame(0x123, b"\x01"), 1))
    heard += [rules[heard[0]]]
    stats = settled(peer, len(heard))

    assert (stats.mode, stats.sent, stats.received, stats.answered) == ("loopback", 2, 2, 1), stats
    assert stats.digest == digest(heard), stats
    is_quiet(stats)


def test_a_malformed_or_out_of_bounds_can_line_is_refused_with_its_reason_and_changes_nothing(peer: Peer) -> None:
    """Every refusal names the subcommand and why, and the tables are bounded: eight rules, fourteen filters.

    A rule beyond the eighth and a filter beyond the fourteenth are refused as
    `full`, and after every refusal the peer has sent and heard nothing.
    """
    lines = [line for line, _ in REFUSALS]
    assert peer.control(*lines) == [answer for _, answer in REFUSALS]

    rules = [f"can rule 0x{0x100 + index:x}/=0x{0x180 + index:x}/" for index in range(RULES_AT_MOST + 1)]
    assert peer.control(*rules) == ["@peer ok can rule"] * RULES_AT_MOST + ["@peer error can rule full"]
    filters = [f"can filter 0x{0x100 + index:x} 0x7ff" for index in range(FILTERS_AT_MOST + 1)]
    assert peer.control(*filters) == ["@peer ok can filter"] * FILTERS_AT_MOST + ["@peer error can filter full"]
    assert peer.control("can last") == ["@peer ok can last none"]

    stats = can_stats(peer)
    assert (stats.mode, stats.sent, stats.received, stats.answered) == ("loopback", 0, 0, 0), stats
    assert stats.digest == digest([]), stats
    is_quiet(stats)
