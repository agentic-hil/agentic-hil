#!/usr/bin/env python3
from __future__ import annotations

import re
import socket
import sys
import time


def serve_gdb_port(port: int) -> int:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", port))
    listener.listen(1)
    print(f"Info : Listening on port {port} for gdb connections", flush=True)
    try:
        while True:
            time.sleep(0.1)
    except KeyboardInterrupt:
        return 0
    finally:
        listener.close()


def main() -> int:
    args = sys.argv[1:]
    if "--version" in args:
        print("Open On-Chip Debugger 0.12.0")
        return 0
    text = " ".join(args)
    gdb_port_match = re.search(r"gdb_port (\d+)", text)
    if gdb_port_match:
        return serve_gdb_port(int(gdb_port_match.group(1)))
    if "adapter serial" in text:
        print(text)
    if "probe_target" in text:
        print("HARDCI_RESULT:probe_target:ok")
    if "flash_firmware" in text or "program" in text:
        print("HARDCI_RESULT:flash_firmware:ok")
    if "reset_target" in text or "reset" in text:
        print("HARDCI_RESULT:reset_target:ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
