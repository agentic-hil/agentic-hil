#!/usr/bin/env python3
"""An STM32CubeProgrammer that confirms half of a read.

fake_stlink_unconfirmed.py prints none of the confirmation lines; this one
prints the ST-Link serial number and never the device name, which is what a CLI
that opened the probe and then failed to identify the part leaves behind.
Confirmation is all-or-nothing, so the backend still answers
`probe_unconfirmed`, and the result has to say which line arrived, because the
one that did proves the probe was opened.
"""

from __future__ import annotations


def main() -> int:
    print("STM32CubeProgrammer version: 2.15.0")
    print("ST-LINK SN  : STLINK123")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
