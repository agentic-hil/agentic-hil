#!/usr/bin/env python3
"""An STM32_Programmer_CLI that opens the ST-Link and finds no target behind it.

fake_stlink.py always succeeds, and fake_stlink_unconfirmed.py exits 0 with
output that confirms nothing, which the backend reports as `probe_unconfirmed`
without ever consulting the classifier. Every classified failure is derived from
STM32CubeProgrammer's own words, so a `target_not_detected` that came out of
`_classify_output` needs a process that actually says them.

The probe line is printed on purpose: the real CLI names the ST-Link it opened
before it gives up on the target, and the classification has to stay
`target_not_detected` rather than `probe_not_found` in its presence.
"""

from __future__ import annotations

import sys


def main() -> int:
    if "--version" in sys.argv[1:]:
        print("STM32CubeProgrammer version: 2.18.0")
        return 0
    print("ST-LINK SN  : STLINK123")
    print("Error: No STM32 target found!", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
