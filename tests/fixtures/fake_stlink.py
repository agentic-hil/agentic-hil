#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

INTEL_HEX_BYTES_PER_RECORD = 32


def intel_hex_record(address16: int, record_type: int, payload: bytes) -> str:
    record = bytes([len(payload), (address16 >> 8) & 0xFF, address16 & 0xFF, record_type, *payload])
    return f":{record.hex().upper()}{(-sum(record)) & 0xFF:02X}"


def write_intel_hex(path: Path, start_address: int, data: bytes) -> None:
    lines: list[str] = []
    upper_address: int | None = None
    for offset in range(0, len(data), INTEL_HEX_BYTES_PER_RECORD):
        absolute = start_address + offset
        chunk_upper = (absolute >> 16) & 0xFFFF
        if chunk_upper != upper_address:
            lines.append(intel_hex_record(0, 0x04, bytes([(chunk_upper >> 8) & 0xFF, chunk_upper & 0xFF])))
            upper_address = chunk_upper
        lines.append(intel_hex_record(absolute & 0xFFFF, 0x00, data[offset : offset + INTEL_HEX_BYTES_PER_RECORD]))
    lines.append(":00000001FF")
    path.write_text("\n".join(lines) + "\n", encoding="ascii")


def read_memory(args: list[str]) -> int:
    """`-r <address> <size> <file>`, printed as STM32CubeProgrammer v2.23.0 prints it.

    Measured against a NUCLEO-F446RE: the CLI answers a read with its connect
    banner, an UPLOADING block naming the file, size and address, and then the
    line the backend actually keys on. The banner is deliberately reproduced
    here so a backend that confirmed itself out of it would pass this fixture
    and fail on the bench.
    """
    index = args.index("-r")
    address_text, size_text, file_text = args[index + 1 : index + 4]
    address = int(address_text, 16 if address_text.lower().startswith("0x") else 10)
    size = int(size_text)
    # Address-derived bytes, so a test can prove the file holds what was read
    # from the address that was asked for rather than merely holding something.
    write_intel_hex(Path(file_text), address, bytes((address + offset) & 0xFF for offset in range(size)))
    print("ST-LINK SN  : STLINK123")
    print("Board       : NUCLEO-F446RE")
    print("Device name : STM32F446RE")
    print()
    print("UPLOADING ...")
    print(f"  File          : {file_text}")
    print(f"  Size          : {size} Bytes")
    print(f"  Address:      : {hex(address)}")
    print()
    print("Data read successfully")
    print("Time elapsed during read operation is: 00:00:00.002")
    return 0

# The connect banner STM32CubeProgrammer prints before it runs the action it was
# given, transcribed from a v2.23.0 run against a NUCLEO-F446RE. `Reset mode  :
# Software reset` is what makes `-halt` a reset-then-halt rather than a bare
# halt, and it is printed by the connect, so it is here for every action that
# connects — including the ones that then fail, which is why the backend does
# not read it as confirmation of anything.
CONNECT_BANNER = """      -------------------------------------------------------------------
                       STM32CubeProgrammer v2.18.0
      -------------------------------------------------------------------

ST-LINK SN  : STLINK123
ST-LINK FW  : V2J30M19
Board       : NUCLEO-F446RE
Voltage     : 3.26V
SWD freq    : 4000 KHz
Connect mode: Normal
Reset mode  : Software reset
Device ID   : 0x421
Revision ID : Rev A
Device name : STM32F446RE
NVM size    : 512 KBytes
Device type : MCU
Device CPU  : Cortex-M4
BL Version  : 0x90
"""


def main() -> int:
    args = sys.argv[1:]
    if "--version" in args:
        print("STM32CubeProgrammer version: 2.18.0")
        return 0
    if "-l" in args:
        if "st-link-only" not in args:
            print("intrusive probe listing is not allowed", file=sys.stderr)
            return 2
        print("===== STLink Interface =====")
        print("ST-LINK SN  : STLINK123")
        print("ST-LINK SN  : STLINK456")
        return 0
    text = " ".join(args)
    print(text)
    if "-w" in args:
        print("Download verified successfully")
    elif "-r" in args:
        return read_memory(args)
    elif "-rst" in args:
        print("MCU Reset")
        print("reset is performed")
    elif "-halt" in args:
        # A different command from `-rst` with a success line of its own: the
        # CLI resets the part through the NORMAL connect and then reports the
        # core it stopped. Neither `-rst` line appears.
        print(CONNECT_BANNER)
        print("Core halted")
    else:
        print("ST-LINK SN  : STLINK123")
        print("Device name : STM32F446RE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
