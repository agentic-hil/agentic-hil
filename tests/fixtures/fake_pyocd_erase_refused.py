#!/usr/bin/env python3
"""A pyOCD whose flash stops at the erase.

`Failed to erase sector at 0x...` is the FlashEraseFailure pyOCD's flash sequencer
raises when the device refuses the erase, logged with the `[flash]` logger name
after it. It is the row #333 measured: with no erase classification of its own it
landed in the broad `flash_failed` bucket.

The `Resetting target` line above it is here on purpose and is not decoration.
pyOCD logs it as a matter of course while it does its work, so it sits in the
transcript of a great many runs in which no reset failed; put beside the `Failed`
in the erase line it is what made the old whole-transcript reset rule answer
`reset_failed` for a refused erase, and pyOCD is the backend likeliest of the
three to print it. A fixture that printed the erase line alone would pass without
ever reproducing that half of the bug.
"""

from __future__ import annotations

import json
import sys


def main() -> int:
    args = sys.argv[1:]
    if "--version" in args:
        print("0.36.0")
        return 0
    if args and args[0] == "json":
        if args[1:] == ["--targets", "--no-config"]:
            print(json.dumps({"pyocd_version": "0.36.0", "status": 0, "targets": [{"name": "stm32f446retx", "vendor": "STMicroelectronics", "part_number": "STM32F446RETx", "source": "pack"}]}))
            return 0
        print(json.dumps({"status": 0, "boards": [{"unique_id": "PYOCD123"}]}))
        return 0
    if args and args[0] == "flash":
        print("0000521 I Loading firmware.elf [load_cmd]")
        print("0000684 I Resetting target with default reset type [board]")
        print("0000902 E Failed to erase sector at 0x08000000 [flash]", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
