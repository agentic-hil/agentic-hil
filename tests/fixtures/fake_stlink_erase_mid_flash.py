#!/usr/bin/env python3
"""An STM32CubeProgrammer whose erase fails with one segment already written.

The other half of #328's distinction. Same refusal line as
`fake_stlink_erase_refused.py`, and a transcript that says something completely
different happened before it: segment 0 was erased, downloaded and completed, and
the failure hit while segment 1 was being erased. Flash on this board is now
neither the old image nor the new one, which is exactly what the
`debugger_result_unconfirmed` quarantine exists for.

A multi-segment image is the ordinary shape here rather than a contrived one: an
ELF with its vector table and code at 0x08000000 and a configuration block higher
up is programmed segment by segment, and the programmer erases each segment right
before it writes it.
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
    print("Erasing internal memory sectors [0 1]")
    print("Download in Progress:")
    print()
    print("File download complete")
    print("Time elapsed during download operation: 00:00:01.204")
    print()
    print("Erasing memory corresponding to segment 1:")
    print("Erasing internal memory sectors [2 5]")
    print("Error: failed to erase memory", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
