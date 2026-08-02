#!/usr/bin/env python3
"""An OpenOCD that opens the adapter and finds no target on it.

fake_openocd.py always succeeds, and every failure classification is derived
from the debugger's own output, so a target_not_detected result cannot be
produced without a process that actually prints one.
"""

from __future__ import annotations

import sys


def main() -> int:
    if "--version" in sys.argv[1:]:
        print("Open On-Chip Debugger 0.12.0")
        return 0
    print("Error: unable to connect to the target", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
