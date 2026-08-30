#!/usr/bin/env python3
"""A pyOCD that flashes but finds no target when a read tries to connect.

fake_pyocd.py answers `savemem` with the bytes the address describes. This twin
flashes the same way, a sessionless symbol read resolves its symbol against the
ELF a confirmed flash put on the board, so the read needs one to have happened,
but its `commander savemem` connect sequence fails before it reads anything, in
pyOCD's own words.

Every classified pyOCD failure is derived from pyOCD's own output, so a
`target_not_detected` that came out of `_classify_output` needs a process that
actually says it. "unable to connect to the target" is that line, and a read
whose command drives nothing of its own reporting it is the proof of no contact
that `_proves_no_contact` turns into the NOT_CONTACTED fields, so this fixture
is what pins that a read which reached no core is released as a retry-safe
refusal rather than quarantined as an unknown effect.

The file `savemem` would have written is deliberately never created, so a test
cannot mistake a stale artifact for a read that reached the target.
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
            print(
                json.dumps(
                    {
                        "pyocd_version": "0.36.0",
                        "status": 0,
                        "targets": [
                            {"name": "stm32f446re", "vendor": "STMicroelectronics", "part_number": "STM32F446RE", "source": "pack"},
                        ],
                    }
                )
            )
            return 0
        print(json.dumps({"status": 0, "boards": [{"unique_id": "PYOCD123"}]}))
        return 0
    text = " ".join(args)
    print(text)
    if args and args[0] in {"commander", "cmd"}:
        commands = [args[index + 1] for index, item in enumerate(args) if item in {"-c", "--command"} and index + 1 < len(args)]
        savemem = next((command for command in commands if command.startswith("savemem")), None)
        if savemem is not None:
            # The read's own connect sequence, and pyOCD's own words for a target
            # that never answered it: a probe was opened, nothing behind it
            # acknowledged, and no `savemem` ran. Non-zero exit, no file written.
            print("0000000:ERROR:Error attempting to connect to target", file=sys.stderr)
            print("Error: unable to connect to the target", file=sys.stderr)
            return 2
    elif args and args[0] == "flash":
        print("[==================================] 100%")
        print("Programmed 8192 bytes @ 0x08000000")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
