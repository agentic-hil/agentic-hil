#!/usr/bin/env python3
"""A pyOCD that flashes, then answers a memory read without leaving the bytes.

The symbol source is the ELF a *successful* flash put on the target, so a pyOCD
that fails the flash never gets as far as a read; this one flashes for real and
then answers `savemem` the way a read that died between opening the probe and
writing its output looks: the commander's own echo, exit 0, and no file. That is
the one case the exit code alone would report as success, which is why the
backend settles a read on the window it was asked for rather than on the code.
"""

from __future__ import annotations

import json
import sys


def main() -> int:
    args = sys.argv[1:]
    if "--version" in args:
        print("0.36.0")
        return 0
    if args and args[0] == "json":
        print(json.dumps({"status": 0, "boards": [{"unique_id": "PYOCD123"}, {"unique_id": "PYOCD456"}]}))
        return 0
    print(" ".join(args))
    if args and args[0] == "flash":
        print("[==================================] 100%")
        print("Programmed 8192 bytes @ 0x08000000")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
