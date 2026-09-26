"""The bench's own firmware, built here and put on the board through the product.

The demo prints one line and ignores everything sent to it. That proves a
flash, a reset and a banner, and nothing past them: every behaviour whose other
party is the firmware (a board that answers, one that faults, one that floods
its port, one that never speaks) needs firmware that does exactly that. Each
such image lives beside this tier as one C file under ``firmware/``, built as
the demo's ``main.c`` with the demo's startup code, linker script and toolchain
file, and goes onto the board through the same plan runner an operator uses.

What this module holds is the part every such test stands on: that an image
from here really is what the board runs afterwards, and that the demo really is
back once the test is over, because every other module assumes it is.
"""

from __future__ import annotations

import pytest

from .conftest import BENCH_ONLY, Bench, BoardImages

pytestmark = [pytest.mark.bench, BENCH_ONLY]


def banner_plan(bench: Bench, name: str) -> str:
    """The demo's own claim, as a plan of its own: open the port clean, reset, read the banner."""
    plan = bench.project / f"{name}.yaml"
    plan.write_text(
        f"""version: 3
name: {name}
steps:
  - device: {bench.com_port_name()}
    action: uart_open
    clear_buffer: true
  - device: {bench.debugger_name()}
    action: reset
    mode: run
  - device: {bench.com_port_name()}
    action: uart_read
    comparator:
      equals: "Hello World"
    timeout_s: 5
""",
        encoding="utf-8",
    )
    return plan.name


def test_an_image_from_this_tier_is_what_the_board_runs_until_the_demo_goes_back(bench: Bench, board_images: BoardImages) -> None:
    """A silent image on the board fails the demo's claim, and the demo back on it passes it again.

    Both halves read the board through its own port rather than trusting the
    flash step's word: the silent image never prints the banner, so the claim
    the demo always meets goes unmet, headed as the board's answer and not as a
    setup error. Put the demo back and the same claim holds, which is what the
    rest of this tier relies on after any test that borrowed the board.
    """
    put = board_images.put("silent")
    assert put.get("ok") is True, put

    status, report = bench.document("test-reactor", "--test-config", banner_plan(bench, "banner-over-a-silent-board"))
    assert status == 1, report
    assert report["ok"] is False, report
    assert report["error_type"] == "comparator_unmet", report
    read = report["steps"][-1]
    assert read["action"] == "uart_read", report["steps"]
    assert read["result"]["error_type"] == "comparator_unmet", read

    board_images.restore()

    status, report = bench.document("test-reactor", "--test-config", banner_plan(bench, "banner-after-the-demo-went-back"))
    assert status == 0, report
    assert report["ok"] is True, report
