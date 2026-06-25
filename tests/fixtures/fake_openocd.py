# Copyright 2026 Hannes Pauli
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path


def main() -> int:
    args = sys.argv[1:]
    record_path = os.environ.get("AIHIL_FAKE_OPENOCD_RECORD")
    if record_path:
        path = Path(record_path)
        records = []
        if path.exists():
            records = json.loads(path.read_text(encoding="utf-8"))
        records.append(args)
        path.write_text(json.dumps(records, indent=2) + "\n", encoding="utf-8")

    scenario = os.environ.get("AIHIL_FAKE_OPENOCD_SCENARIO", "success")
    if "--version" in args:
        print("Open On-Chip Debugger 0.12.0 fake")
        return 0
    if scenario == "timeout":
        time.sleep(30)
        return 0
    if scenario == "target_not_detected":
        print("Error: target not examined", file=sys.stderr)
        return 1
    if scenario == "interface_config_not_found":
        print("Error: can't find interface/stlink.cfg", file=sys.stderr)
        return 1
    if scenario == "adapter_open_failed":
        print("Error: open failed", file=sys.stderr)
        return 1
    if scenario == "verify_failed":
        print("Error: checksum mismatch - verify failed", file=sys.stderr)
        return 1
    if scenario == "verify_failed_return_zero":
        print("Error: checksum mismatch - verify failed", file=sys.stderr)
        return 0
    if scenario == "missing_success_marker":
        print("OpenOCD fake command succeeded")
        return 0
    print("OpenOCD fake command succeeded")
    commands = [args[index + 1] for index, arg in enumerate(args[:-1]) if arg == "-c"]
    if commands:
        if any("targets" in command for command in commands):
            print("target 0: fake.cpu")
        if any("program" in command for command in commands):
            print("verified")
        for command in commands:
            for part in command.split(";"):
                part = part.strip()
                if part.startswith("echo "):
                    print(part[5:].strip().strip('"'))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
