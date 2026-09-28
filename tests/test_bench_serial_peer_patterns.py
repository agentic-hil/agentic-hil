"""Software-only checks for serial plans exercised against a real board."""

from __future__ import annotations

import re

from tests.bench.test_bench_serial_peer import V2_EXPECT_PLAN


def test_version_expectation_matches_a_line_after_a_partial_previous_line() -> None:
    """A version line can follow a partial byte fragment already in the RX stream.

    The hardware run captured `.3\r\nv1.2.3\r\n...`: anchoring at the start of the
    entire stream misses every complete version line that follows that fragment.
    """
    captured_stream_prefix = ".3\r\nv1.2.3\r\n"
    pattern = V2_EXPECT_PLAN[1]["pattern"]

    assert re.search(pattern, captured_stream_prefix), (pattern, captured_stream_prefix)
