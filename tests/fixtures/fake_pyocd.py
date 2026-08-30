#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from pathlib import Path

# The one pyOCD connect mode that leaves a running core alone. Every other way
# of asking for one halts or resets the target the read is measuring, so this
# fixture refuses them the way it refuses an intrusive probe listing: a backend
# that regressed to one of them fails here rather than on somebody's bench.
NON_INTRUSIVE_CONNECT_MODE = "attach"
CONNECT_MODE_FLAGS = ("--connect", "-M")
HALT_ON_CONNECT_FLAGS = ("--halt", "-H")


def intrusive_connect_reason(args: list[str], commands: list[str], read_command: str) -> str | None:
    """Why this read would have touched the core, or None if it would not."""
    for flag in HALT_ON_CONNECT_FLAGS:
        if flag in args:
            return f"{flag} halts the core on connect"
    for index, item in enumerate(args):
        if item in CONNECT_MODE_FLAGS:
            mode = args[index + 1] if index + 1 < len(args) else ""
            if mode != NON_INTRUSIVE_CONNECT_MODE:
                return f"connect mode {mode!r} is not {NON_INTRUSIVE_CONNECT_MODE!r}"
        if item.startswith("connect_mode="):
            return f"session option {item!r} decides the connect behind the read"
    others = [command for command in commands if command != read_command]
    if others:
        return f"the read ran beside another commander command: {others!r}"
    return None


def unquote(token: str) -> str:
    """The single-quoted form the backend sends a file path in.

    pyOCD's own tokenizer treats a backslash as an escape and a space as a word
    break unless the word is quoted, and honours no escapes inside single
    quotes; this is the half of that behaviour the fixture needs.
    """
    if len(token) >= 2 and token[0] == "'" and token[-1] == "'":
        return token[1:-1]
    return token


def save_memory(command: str) -> int:
    """`savemem ADDR LEN FILE`, answered the way pyOCD answers it.

    A raw binary file of exactly the requested length, and the confirmation line
    pyOCD's SavememCommand prints. The bytes are derived from the address, so a
    test can prove the file holds what was read from the address that was asked
    for rather than merely holding something.
    """
    _, address_text, size_text, file_text = command.split(None, 3)
    address = int(address_text, 16 if address_text.lower().startswith("0x") else 10)
    size = int(size_text)
    path = Path(unquote(file_text.strip()))
    path.write_bytes(bytes((address + offset) & 0xFF for offset in range(size)))
    print(f"Saved {size} bytes to {path}")
    return 0


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
        commands = [args[index + 1] for index, item in enumerate(args) if item in {"-c", "--command"} and index + 1 < len(args)]
        savemem = next((command for command in commands if command.startswith("savemem")), None)
        if savemem is not None:
            reason = intrusive_connect_reason(args, commands, savemem)
            if reason is not None:
                print(f"a memory read must not disturb the core: {reason}", file=sys.stderr)
                return 2
            return save_memory(savemem)
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
