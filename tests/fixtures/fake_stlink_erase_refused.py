#!/usr/bin/env python3
"""An STM32CubeProgrammer whose first flash is refused at the erase.

Transcribed from the bench report behind #327: ST-LINK V3SET on a NUCLEO-F446RE,
STM32CubeProgrammer v2.22.0, ten consecutive attempts over four hours where every
first flash ended in these lines after a constant ~310 ms and every immediate
retry programmed and verified.

The connect banner is here on purpose and is not decoration. It carries
`Reset mode  : Software reset`, which is the only occurrence of the word "reset"
in the whole run; put beside the `failed` in the last line it is what made the
classifier answer `reset_failed` and send the operator to the reset line, while
the programmer had said it could not erase. A fixture that printed only the three
erase lines would pass without ever reproducing the bug.
"""

from __future__ import annotations

import sys

CONNECT_BANNER = """      -------------------------------------------------------------------
                       STM32CubeProgrammer v2.22.0
      -------------------------------------------------------------------

ST-LINK SN  : 002E00073431511834333935
ST-LINK FW  : V3J15M6
Board       : NUCLEO-F446RE
Voltage     : 3.27V
SWD freq    : 8000 KHz
Connect mode: Hot Plug
Reset mode  : Software reset
Device ID   : 0x421
Revision ID : Rev A
Device name : STM32F446
Flash size  : 512 KBytes
Device type : MCU
Device CPU  : Cortex-M4
BL Version  : 0xD1
"""


def main() -> int:
    args = sys.argv[1:]
    if "--version" in args:
        print("STM32CubeProgrammer version: 2.22.0")
        return 0
    print(CONNECT_BANNER)
    if "-w" not in args:
        # Everything that is not a flash connects and answers normally, so a
        # test can prove the bench is otherwise healthy: the probe is there, the
        # part is identified, and only the erase is refused.
        print("ST-LINK SN  : 002E00073431511834333935")
        print("Device name : STM32F446")
        return 0
    print("Memory Programming ...")
    print("Opening and parsing file: firmware.elf")
    print("  File          : firmware.elf")
    print("  Size          : 22.55 KB")
    print("  Address       : 0x08000000")
    print()
    print("Erasing memory corresponding to segment 0:")
    print("Erasing internal memory sectors [0 5]")
    print("Error: failed to erase memory", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
