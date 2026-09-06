#!/usr/bin/env python3
"""An OpenOCD whose `-f` script cannot be found.

Real OpenOCD loads every `-f` file during its configuration stage and exits
before `init` when one is missing, so the adapter is never opened and the
init-stage echo never prints. This fake reproduces exactly that: it checks each
`-f` argument against the filesystem and against the script tree it is told it
has, reports the first missing one the way OpenOCD 0.12 words it, and executes
nothing else, in particular none of the `-c` scripts, so no stage marker and no
result marker reach the output.

The script tree is `AGENTIC_HIL_FAKE_OPENOCD_SCRIPTS`, search names joined by
the platform's path separator, and it is empty by default. Real OpenOCD
resolves `interface/stlink.cfg` against the tree its package installs, so a
configuration whose interface script is a search name and whose target script
is not there fails on the target; with no tree at all this fake would report
the interface script first and the target case could never be reached (#506).

The refusal text is the recording in debugger_refusal_recordings.json (OpenOCD
0.12.0, Debian package 0.12.0-3+b2, 2026-09-06): the version banner, then the
`embedded:startup.tcl` error line and the Jim traceback that follows it, all
on stderr, exit status 1.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

VERSION_BANNER = "Open On-Chip Debugger 0.12.0\nLicensed under GNU GPL v2\nFor bug reports, read\n\thttp://openocd.org/doc/doxygen/bugs.html\n"


def known_scripts() -> set[str]:
    return {name for name in os.environ.get("AGENTIC_HIL_FAKE_OPENOCD_SCRIPTS", "").split(os.pathsep) if name}


def main() -> int:
    args = sys.argv[1:]
    if "--version" in args:
        print("Open On-Chip Debugger 0.12.0")
        return 0
    sys.stderr.reconfigure(newline="\n")
    sys.stderr.write(VERSION_BANNER)
    for index, argument in enumerate(args):
        if argument == "-f" and index + 1 < len(args):
            script = args[index + 1]
            if not Path(script).is_file() and script not in known_scripts():
                sys.stderr.write(f"embedded:startup.tcl:28: Error: Can't find {script}\n")
                sys.stderr.write("Traceback (most recent call last):\n")
                sys.stderr.write('  File "embedded:startup.tcl", line 28, in script\n')
                sys.stderr.write(f"    find {script}\n")
                sys.stderr.flush()
                return 1
    print("Error: fake_openocd_missing_cfg expected a missing -f script", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
