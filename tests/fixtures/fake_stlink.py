#!/usr/bin/env python3
from __future__ import annotations

import sys


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
    else:
        print("ST-LINK SN  : STLINK123")
        print("Device name : STM32F446RE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
