#!/usr/bin/env python3
"""An OpenOCD that reached the run stage and then confirmed nothing further.

fake_openocd_unconfirmed.py loses both echoes; this one loses only the second.
It prints the init-stage marker — so `init` completed, the adapter was opened
and the core was examined — and then exits 0 without the tool's success marker.
That is the branch the catalogue entry has to describe truthfully: the run
reached the board and still reported no outcome, so the state is unknown for a
different reason than a run that said nothing at all.

The marker is spelled here rather than imported because this file is executed as
a standalone program by the backend under test.
"""

from __future__ import annotations

import sys


def main() -> int:
    if "--version" in sys.argv[1:]:
        print("Open On-Chip Debugger 0.12.0")
        return 0
    print("Info : clock speed 2000 kHz")
    print("AGENTIC_HIL_STAGE:init:ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
