#!/usr/bin/env python3
"""An OpenOCD whose `program` run stops at the erase.

`failed erasing sectors %u to %u` is what OpenOCD's flash layer prints when the
device refuses the erase the image needs (src/flash/nor/tcl.c), and it is the
row #333 measured: with no erase classification of its own it landed in the broad
`flash_failed` bucket, whose causes are about a wrong image or a wrong address.

The `Warn :` line above it is here on purpose and is not decoration. OpenOCD
prints it on nearly every reset of a Cortex-M core with no srst wired, so it is
in the transcript of a great many runs that have nothing to do with a reset
failing; put beside the `failed` in the erase line it is what made the old
whole-transcript reset rule answer `reset_failed` for a refused erase. A fixture
that printed the erase line alone would pass without ever reproducing that half
of the bug.

The stage echo before it prints because `init` completed: the adapter was opened
and the core examined, which is what makes this an unconfirmed flash rather than
a call that never reached the bench. The success echo after `program` never
prints, because the script stops where the erase did.
"""

from __future__ import annotations

import sys


def main() -> int:
    args = sys.argv[1:]
    if "--version" in args:
        print("Open On-Chip Debugger 0.12.0")
        return 0
    for index, argument in enumerate(args):
        if argument != "-c" or index + 1 >= len(args):
            continue
        for segment in args[index + 1].split(";"):
            command = segment.strip()
            verb, _, argument_text = command.partition(" ")
            if verb == "echo":
                print(argument_text.strip().strip('"'))
            if verb == "program":
                print("Info : device id = 0x10006421")
                print("Info : flash size = 512 KiB")
                print(
                    "Warn : Only resetting the Cortex-M core, use a reset-init event handler to reset any "
                    "peripherals or configure hardware srst support."
                )
                print("Error: failed erasing sectors 0 to 5", file=sys.stderr)
                return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
