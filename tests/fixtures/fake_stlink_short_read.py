#!/usr/bin/env python3
"""An STM32CubeProgrammer whose confirmed read leaves half the bytes on disk.

The confirmation line says the transfer finished; it says nothing about what
landed in the file. This one prints every line a healthy read prints, exit code
included, and writes an Intel HEX covering the first half of the requested
window, which is what a truncated write or a wrong file looks like from the
outside. A backend that trusted the line alone would decode whatever it found
and call it the symbol's value.
"""

from __future__ import annotations

import sys
from pathlib import Path


def intel_hex_record(address16: int, record_type: int, payload: bytes) -> str:
    record = bytes([len(payload), (address16 >> 8) & 0xFF, address16 & 0xFF, record_type, *payload])
    return f":{record.hex().upper()}{(-sum(record)) & 0xFF:02X}"


def write_short_intel_hex(path: Path, start_address: int, size: int) -> None:
    data = bytes((start_address + offset) & 0xFF for offset in range(max(1, size // 2)))
    upper = (start_address >> 16) & 0xFFFF
    lines = [
        intel_hex_record(0, 0x04, bytes([(upper >> 8) & 0xFF, upper & 0xFF])),
        intel_hex_record(start_address & 0xFFFF, 0x00, data),
        ":00000001FF",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="ascii")


def main() -> int:
    args = sys.argv[1:]
    if "--version" in args:
        print("STM32CubeProgrammer version: 2.23.0")
        return 0
    if "-w" in args:
        print("Download verified successfully")
        return 0
    if "-r" in args:
        index = args.index("-r")
        address_text, size_text, file_text = args[index + 1 : index + 4]
        address = int(address_text, 16 if address_text.lower().startswith("0x") else 10)
        write_short_intel_hex(Path(file_text), address, int(size_text))
        print("ST-LINK SN  : STLINK123")
        print("Device name : STM32F446RE")
        print()
        print("UPLOADING ...")
        print(f"  File          : {file_text}")
        print(f"  Size          : {size_text} Bytes")
        print(f"  Address:      : {address_text}")
        print()
        print("Data read successfully")
        return 0
    print("ST-LINK SN  : STLINK123")
    print("Device name : STM32F446RE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
