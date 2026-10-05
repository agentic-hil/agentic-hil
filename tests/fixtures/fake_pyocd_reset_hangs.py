#!/usr/bin/env python3
"""A pyOCD whose flash succeeds and whose post-flash reset never returns.

`flash` is answered exactly as fake_pyocd.py answers it. A `commander` run
carrying `reset` sleeps far past any `debuggers.<name>.timeout_s` a test sets,
so the backend stops it at the deadline: a firmware that was written and a reset
that timed out, the case #655 found reported as a refused reset.
"""

from __future__ import annotations

import json
import sys
import time


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
    print(text, flush=True)
    if args and args[0] in {"commander", "cmd"} and "reset" in text:
        time.sleep(120)
        return 0
    if args and args[0] == "flash":
        print("[==================================] 100%")
        print("Programmed 8192 bytes @ 0x08000000")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
