#!/usr/bin/env python3
"""An STM32CubeProgrammer that flashes, then reads memory without confirming it.

fake_stlink_unconfirmed.py confirms nothing at all, which cannot exercise this
branch: the symbol source is the ELF a *successful* flash put on the target, so
a CLI that fails the flash never gets as far as a read. This one flashes for
real and then answers `-r` the way a read that died between opening the probe
and finishing looks — connect banner, UPLOADING block, exit 0, and no
`Data read successfully` anywhere. No output file is written either, because a
read that never confirmed has nothing to promise about one.
"""

from __future__ import annotations

import sys


def main() -> int:
    args = sys.argv[1:]
    if "--version" in args:
        print("STM32CubeProgrammer version: 2.23.0")
        return 0
    if "-w" in args:
        print("Download verified successfully")
        return 0
    if "-r" in args:
        index = args.index("-r")
        address_text, size_text, file_text = args[index + 1 : index + 4]
        print("ST-LINK SN  : STLINK123")
        print("Device name : STM32F446RE")
        print()
        print("UPLOADING ...")
        print(f"  File          : {file_text}")
        print(f"  Size          : {size_text} Bytes")
        print(f"  Address:      : {address_text}")
        return 0
    print("ST-LINK SN  : STLINK123")
    print("Device name : STM32F446RE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
