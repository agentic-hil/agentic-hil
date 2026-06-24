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
    if scenario == "verify_failed":
        print("Error: checksum mismatch - verify failed", file=sys.stderr)
        return 1
    print("OpenOCD fake command succeeded")
    if "-c" in args:
        command = args[args.index("-c") + 1]
        if "targets" in command:
            print("target 0: fake.cpu")
        if "program" in command:
            print("verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
