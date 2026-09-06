#!/usr/bin/env python3
"""A pyOCD whose flash succeeds and whose post-flash reset fails.

fake_pyocd.py answers `flash` and `commander --command reset` alike with
success, so the branch of `flash_firmware` that has to report a firmware that
was written and a target that then would not reset has never been driven: a
flash that stopped at that point leaves the board holding the new image and
not running it, which is neither a success nor a retry-safe refusal.

`flash` is answered exactly as fake_pyocd.py answers it. The reset line is
representative, not recorded: pyOCD reports a failed reset as an `E` log line
naming the reset (test_failure_classification.py carries the same shape as one
of its genuine reset failures), and no bench recording of a reset that failed
after a flash that succeeded exists yet. What this fixture pins is the
backend's reading of a reset the commander refused, whatever the wording.
"""

from __future__ import annotations

import json
import sys

RESET_FAILED = "0000684 E Error attempting to reset target: reset failed [board]"


def main() -> int:
    args = sys.argv[1:]
    if "--version" in args:
        print("0.45.1")
        return 0
    if args and args[0] == "json":
        if args[1:] == ["--targets", "--no-config"]:
            print(json.dumps({"pyocd_version": "0.45.1", "status": 0, "targets": [{"name": "stm32f446re", "vendor": "STMicroelectronics", "part_number": "STM32F446RE", "source": "pack"}]}))
            return 0
        print(json.dumps({"status": 0, "boards": [{"unique_id": "PYOCD123"}]}))
        return 0
    text = " ".join(args)
    print(text)
    if args and args[0] in {"commander", "cmd"} and "reset" in text:
        print(RESET_FAILED, file=sys.stderr)
        return 1
    if args and args[0] == "flash":
        print("[==================================] 100%")
        print("Programmed 8192 bytes @ 0x08000000")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
