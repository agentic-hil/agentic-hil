"""Software-only checks for the serial plans the two hardware tiers run.

Neither tier runs on a developer's `pytest`: the bench plans need the board
behind a probe and the container plans need the pseudo-terminal the image makes,
so a plan pattern that does not say what it means survives here until a leg goes
red. What a pattern says is settled by `re.search` over the window one step has
read, and that window starts where the step before it left off rather than where
a line does, so a claim about the start of a line writes `(?m)^` and a bare `^`
is a claim about the window. Both tiers' version claims are held to that below,
on any host.
"""

from __future__ import annotations

import re

import pytest
import yaml

from tests.bench.test_bench_serial_peer import GREEN_PLAN, SHAPES_PLAN, V2_EXPECT_PLAN
from tests.container import test_serial_over_pty as pty_tier

# What the hardware run captured: a complete version line behind the tail of the
# line before it. A plan reaches this shape without anything going wrong, because
# a read returns the moment the session has bytes: the `uart_expect` for `PONG`
# can be satisfied by a poll that saw `PONG` and not the `\r\n` behind it, and
# that line ending is then the first thing the next step's window holds.
AFTER_A_FRAGMENT = ".3\r\nv1.2.3\r\n"
# The same version text where no line begins, which is what a claim about the
# start of a line must not be met by. A pattern that answers the window by
# dropping its anchor rather than by moving it meets everything above.
WHERE_NO_LINE_BEGINS = "peer says v1.2.3\r\n"


def container_claim(plan: str, step: int) -> str:
    """The pattern one container plan step carries, as the runner reads it.

    Those plans are the YAML a project writes, so the claim is read back through
    the parser instead of off the source line: the escaping in the file is the
    file's own, and what the step means is what the loader hands over. The
    `comparator` is where a v3 `uart_read` keeps it and the step itself is where
    the v2 `uart_expect` does.
    """
    body = yaml.safe_load(plan)["steps"][step]
    return (body.get("comparator") or body)["pattern"]


# Every claim either tier makes about a version line, by the plan it lives in.
# The two tiers' plans are twins step for step, so a pattern moved in one and
# left in the other is a leg that still goes red.
VERSION_CLAIMS = [
    pytest.param(GREEN_PLAN[4]["comparator"]["pattern"], id="bench-green"),
    pytest.param(SHAPES_PLAN[2]["comparator"]["pattern"], id="bench-shapes"),
    pytest.param(V2_EXPECT_PLAN[1]["pattern"], id="bench-v2-expect"),
    pytest.param(container_claim(pty_tier.GREEN_PLAN, 4), id="pty-green"),
    pytest.param(container_claim(pty_tier.SHAPES_PLAN, 2), id="pty-shapes"),
    pytest.param(container_claim(pty_tier.V2_EXPECT_PLAN, 1), id="pty-v2-expect"),
]


@pytest.mark.parametrize("pattern", VERSION_CLAIMS)
def test_version_expectation_matches_a_line_after_a_partial_previous_line(pattern: str) -> None:
    """A version line can follow a partial byte fragment already in the RX stream.

    The hardware run captured `.3\r\nv1.2.3\r\n`: anchoring at the start of the
    window misses every complete version line that follows such a fragment, and
    the window only slides once 512 bytes have arrived, so the fragment is still
    in front of the line when the step's timeout passes.
    """
    assert re.search(pattern, AFTER_A_FRAGMENT), (pattern, AFTER_A_FRAGMENT)


@pytest.mark.parametrize("pattern", VERSION_CLAIMS)
def test_version_expectation_is_not_met_where_no_line_begins(pattern: str) -> None:
    """The anchor is moved to every line, not removed.

    A pattern with no anchor at all matches the version text wherever it appears,
    including inside a line the peer wrote about something else, and every plan
    here means the line the version begins.
    """
    assert re.search(pattern, WHERE_NO_LINE_BEGINS) is None, (pattern, WHERE_NO_LINE_BEGINS)
