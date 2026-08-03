#!/usr/bin/env python3
"""An STM32_Programmer_CLI that finds no ST-Link to open at all.

fake_stlink_no_target.py one step earlier in the chain: nothing enumerates, so
STM32CubeProgrammer never reaches a target. The backend classifies this as
`probe_not_found` and reports it as the public `adapter_not_found`, which is a
second remediation entry and a second wiring site in `_failure_result`.
"""

from __future__ import annotations

import sys


def main() -> int:
    if "--version" in sys.argv[1:]:
        print("STM32CubeProgrammer version: 2.18.0")
        return 0
    print("Error: No ST-LINK detected!", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
