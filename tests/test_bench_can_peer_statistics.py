"""Software-only checks for what the peer firmware's CAN statistics claim.

The peer image is built by the bench tier against a board, with an ARM toolchain
no developer `pytest` has, so nothing here runs the firmware. What can be held on
any host is the shape of two claims the firmware's own header comment makes about
`lost`, each of which was wrong in a way no current test could fail on, because no
test switches CAN modes with traffic in flight and no assertion reads `lost`
without `received` coming up short first.

Read off the source, which is weak and is the only software-only reading
available; the bench tier is where the behaviour itself is exercised.
"""

from __future__ import annotations

import re
from pathlib import Path

PEER = Path(__file__).resolve().parent / "bench" / "firmware" / "peer.c"
SOURCE = PEER.read_text(encoding="utf-8")


def body_of(function: str) -> str:
    """One function's body, from its opening brace to the first closing brace in column one."""
    start = SOURCE.index(f"\n{function}\n{{\n") if f"\n{function}\n{{\n" in SOURCE else SOURCE.index(function)
    end = SOURCE.index("\n}\n", start)
    return SOURCE[start:end]


def test_the_mode_switch_counts_every_frame_the_flush_discards() -> None:
    """A frame heard and then flushed has to land in `lost`, which is where it belongs.

    `can_start` clears the receive ring. On the `can_reset` path the statistics are
    zeroed afterwards, so the flush is harmless; `can_switch` calls
    `can_take_received()` and *then* `can_start()`, and a frame still in FIFO 0
    when `can_wait(&CAN1_TSR, CAN_TSR_TME_ALL, ...)` returned reaches the ring
    through `CAN1_RX0_IRQHandler` a few microseconds later, after that call. Such
    a frame was discarded without being counted in `received`, `lost` or the
    digest, so `@peer can send 0x123/01 1000` followed by `@peer can mode normal`
    reported `received` short of what the controller heard with `lost=0`, against
    the header comment's own contract.

    Counted inside the same interrupts-off region as the flush, so no frame can
    arrive between the count and the discard, which a `can_take_received()` moved
    one line later would still leave open.
    """
    start = body_of("static int can_start(uint32_t mode)")
    guarded = start[start.index("interrupts_off();") : start.index("interrupts_on();")]

    assert "can_lost +=" in guarded, guarded
    assert "can_rx_head - can_rx_tail" in guarded, guarded
    assert "can_rx_tail = can_rx_head;" in guarded, guarded
    # The count is first, so the difference is read before it is destroyed.
    assert guarded.index("can_lost +=") < guarded.index("can_rx_tail = can_rx_head;"), guarded


def test_the_fifo_overrun_flag_is_not_presented_as_a_frame_count() -> None:
    """One FOVR0 flag stands for however many frames were dropped, and says so.

    The ring-full branch counts per frame; the overrun branch counts one flag, so
    the two halves of the same statistic are not the same kind of number and
    "the frames heard and not kept" holds for only one of them. This cannot make a
    test pass wrongly, since `received` would come up short and the `received ==`
    assertions fail loudly, but the statistic an operator reads after a red run
    understates the loss, and a number whose meaning is not written down is the
    thing to fix.
    """
    handler = body_of("void CAN1_RX0_IRQHandler(void)")
    overrun = handler[handler.index("CAN_RF0R_FOVR0") :]

    assert "however many frames" in overrun, overrun
    assert "lower bound" in overrun, overrun
    # And the header comment, which is what a reader of the report has.
    header = SOURCE[: SOURCE.index("#include")]
    claim = header[header.index("`lost`") : header.index("`unsent`")]
    assert "lower bound" in claim, claim
    assert "one flag" in claim, claim
    assert re.search(r"ring-full case counts\s+\*?\s*every frame", claim), claim
    # The mode switch is named as a way a heard frame is not kept, beside the two
    # the comment always carried.
    assert "mode switch" in claim, claim
