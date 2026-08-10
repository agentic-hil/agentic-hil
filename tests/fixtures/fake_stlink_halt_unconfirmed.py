#!/usr/bin/env python3
"""An STM32_Programmer_CLI whose `-halt` connects and never reports the halt.

The failure shape of the mode `halt` confirmation, and the reason the connect
banner is not part of it. The banner is printed in full here — `Reset mode  :
Software reset` included — because connect happens before the halt is
attempted, so a run that connected and then failed to stop the core prints
exactly this. Everything the banner asserts is true and the outcome is still
unstated, which is what the backend has to answer `reset_unconfirmed` to: the
core may be halted, may be running, and nothing on the host knows which.

Exits 0 with no failure text on purpose. A CLI that says `Error:` is classified
from its own words and is a different result; this is the case where there are
no words at all — a wrapper that swallowed the last line, or a version that
words it differently.
"""

from __future__ import annotations

import sys


def main() -> int:
    args = sys.argv[1:]
    if "--version" in args:
        print("STM32CubeProgrammer version: 2.18.0")
        return 0
    print(" ".join(args))
    print("ST-LINK SN  : STLINK123")
    print("Board       : NUCLEO-F446RE")
    print("Connect mode: Normal")
    print("Reset mode  : Software reset")
    print("Device name : STM32F446RE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
