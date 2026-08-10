#!/usr/bin/env python3
from __future__ import annotations

import sys

# The connect banner STM32CubeProgrammer prints before it runs the action it was
# given, transcribed from a v2.23.0 run against a NUCLEO-F446RE. `Reset mode  :
# Software reset` is what makes `-halt` a reset-then-halt rather than a bare
# halt, and it is printed by the connect, so it is here for every action that
# connects — including the ones that then fail, which is why the backend does
# not read it as confirmation of anything.
CONNECT_BANNER = """      -------------------------------------------------------------------
                       STM32CubeProgrammer v2.18.0
      -------------------------------------------------------------------

ST-LINK SN  : STLINK123
ST-LINK FW  : V2J30M19
Board       : NUCLEO-F446RE
Voltage     : 3.26V
SWD freq    : 4000 KHz
Connect mode: Normal
Reset mode  : Software reset
Device ID   : 0x421
Revision ID : Rev A
Device name : STM32F446RE
NVM size    : 512 KBytes
Device type : MCU
Device CPU  : Cortex-M4
BL Version  : 0x90
"""


def main() -> int:
    args = sys.argv[1:]
    if "--version" in args:
        print("STM32CubeProgrammer version: 2.18.0")
        return 0
    if "-l" in args:
        if "st-link-only" not in args:
            print("intrusive probe listing is not allowed", file=sys.stderr)
            return 2
        print("===== STLink Interface =====")
        print("ST-LINK SN  : STLINK123")
        print("ST-LINK SN  : STLINK456")
        return 0
    text = " ".join(args)
    print(text)
    if "-w" in args:
        print("Download verified successfully")
    elif "-rst" in args:
        print("MCU Reset")
        print("reset is performed")
    elif "-halt" in args:
        # A different command from `-rst` with a success line of its own: the
        # CLI resets the part through the NORMAL connect and then reports the
        # core it stopped. Neither `-rst` line appears.
        print(CONNECT_BANNER)
        print("Core halted")
    else:
        print("ST-LINK SN  : STLINK123")
        print("Device name : STM32F446RE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
