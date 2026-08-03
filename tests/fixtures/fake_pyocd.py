#!/usr/bin/env python3
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
            # Same envelope pyOCD 0.45.1 emits, trimmed: one builtin and one
            # pack-provided target, so a test can tell the two apart.
            print(
                json.dumps(
                    {
                        "pyocd_version": "0.36.0",
                        "status": 0,
                        "targets": [
                            {"name": "cortex_m", "vendor": "Generic", "part_number": "CoreSightTarget", "source": "builtin"},
                            {"name": "stm32f446re", "vendor": "STMicroelectronics", "part_number": "STM32F446RE", "source": "pack"},
                            {"name": "stm32f446retx", "vendor": "STMicroelectronics", "part_number": "STM32F446RETx", "source": "pack"},
                        ],
                    }
                )
            )
            return 0
        if args[1:] != ["--probes", "--no-config"]:
            print("unsafe probe discovery arguments", file=sys.stderr)
            return 2
        print(
            json.dumps(
                {
                    "status": 0,
                    "boards": [
                        {"unique_id": "PYOCD123"},
                        {"unique_id": "PYOCD456"},
                    ],
                }
            )
        )
        return 0
    text = " ".join(args)
    print(text)
    if args and args[0] in {"commander", "cmd"}:
        if "status" in text:
            print("Target status: halted")
        if "reset" in text:
            print("Reset target executed")
    elif args and args[0] == "flash":
        print("[==================================] 100%")
        print("Programmed 8192 bytes @ 0x08000000")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
