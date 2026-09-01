#!/usr/bin/env python3
"""An OpenOCD whose `-f` script cannot be found.

Real OpenOCD loads every `-f` file during its configuration stage and exits
before `init` when one is missing, so the adapter is never opened and the
init-stage echo never prints. This fake reproduces exactly that: it checks each
`-f` argument against the filesystem, reports the first missing one the way
OpenOCD 0.12 words it, and executes nothing else, in particular none of the
`-c` scripts, so no stage marker and no result marker reach the output.
"""

from __future__ import annotations

import sys
from pathlib import Path


def main() -> int:
    args = sys.argv[1:]
    if "--version" in args:
        print("Open On-Chip Debugger 0.12.0")
        return 0
    for index, argument in enumerate(args):
        if argument == "-f" and index + 1 < len(args):
            script = args[index + 1]
            if not Path(script).is_file():
                print(f"Can't find {script}", file=sys.stderr)
                print(f"embedded:startup.tcl:28: Error: Can't find {script}", file=sys.stderr)
                print("in procedure 'script'", file=sys.stderr)
                return 1
    print("Error: fake_openocd_missing_cfg expected a missing -f script", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
