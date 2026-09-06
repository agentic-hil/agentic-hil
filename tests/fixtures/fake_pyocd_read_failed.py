#!/usr/bin/env python3
"""A pyOCD that flashes, connects, and then fails the memory read it was asked for.

fake_pyocd_no_target.py is the read whose connect never reached a core, which
the backend releases as a refusal. This is the other read failure: the probe
opened, the target answered the connect, and `savemem` itself reported an
error, so the bytes that were asked for never reached the file. The
classifier's bucket for that is `memory_read_failed`, anchored on the
operation rather than on a phrase, and nothing in the suite had driven it
through the tool result.

The error line is representative, not recorded: it carries the `E` log prefix
pyOCD 0.45.1 uses and a failure word, and no bench recording of a `savemem`
that failed after a successful connect exists yet. The file `savemem` would
have written is deliberately never created.
"""

from __future__ import annotations

import json
import sys

READ_FAILED = "0000817 E Transfer error while reading 4 bytes @ 0x20000080 [savemem]"


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
    if args and args[0] in {"commander", "cmd"} and "savemem" in text:
        print(READ_FAILED, file=sys.stderr)
        return 1
    if args and args[0] == "flash":
        print("[==================================] 100%")
        print("Programmed 8192 bytes @ 0x08000000")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
