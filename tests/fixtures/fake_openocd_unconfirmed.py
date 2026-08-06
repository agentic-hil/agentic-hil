#!/usr/bin/env python3
"""An OpenOCD that exits 0 having confirmed nothing.

fake_openocd.py executes the `-c` scripts it is given, so it prints the
init-stage marker and the success marker whenever the script reaches them, and
an error when it does not. This one prints neither and no failure text either —
the case where the process's own report is *missing* rather than negative, which
a real run reaches when the markers are lost, buffered away or omitted by a
build that echoes differently.

Nothing here says a target did not answer. It says nothing at all, which is why
the backend reports `probe_unconfirmed` rather than a classification, and why
the read cannot be released as never-contacted.
"""

from __future__ import annotations

import sys


def main() -> int:
    if "--version" in sys.argv[1:]:
        print("Open On-Chip Debugger 0.12.0")
        return 0
    print("Info : clock speed 2000 kHz")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
