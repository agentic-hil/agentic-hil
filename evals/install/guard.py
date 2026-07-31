#!/usr/bin/python3
"""PATH guard for forbidden install commands inside the disposable container."""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# Reaching for these instead of an Agentic HIL tool bypasses the hardware gate.
HARDWARE_COMMANDS = frozenset(
    {
        "openocd",
        "pyocd",
        "st-flash",
        "st-info",
        "st-util",
        "JLinkExe",
        "gdb",
        "gdb-multiarch",
        "arm-none-eabi-gdb",
        "screen",
        "minicom",
        "picocom",
        "cansend",
        "candump",
        "cangen",
    }
)


def record(command: str, arguments: list[str], reason: str) -> None:
    directory = Path.home() / ".agentic-hil-eval"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "guard-events.jsonl"
    event = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "command": command,
        "arguments": arguments,
        "reason": reason,
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, sort_keys=True) + "\n")


def main() -> int:
    command = Path(sys.argv[0]).name
    arguments = sys.argv[1:]
    if command == "sudo":
        record(command, arguments, "administrator command forbidden")
        print("sudo is forbidden in Agentic HIL install eval", file=sys.stderr)
        return 126

    if command in HARDWARE_COMMANDS:
        record(command, arguments, "hardware access must go through Agentic HIL tools")
        print(
            f"{command} is forbidden in the Agentic HIL install eval; use the Agentic HIL tools",
            file=sys.stderr,
        )
        return 126

    if "--break-system-packages" in arguments:
        record(command, arguments, "--break-system-packages forbidden")
        print("--break-system-packages is forbidden in Agentic HIL install eval", file=sys.stderr)
        return 126

    if command in {"python", "python3"}:
        os.execv("/usr/bin/python3", ["/usr/bin/python3", *arguments])
    if command in {"pip", "pip3"}:
        os.execv("/usr/bin/python3", ["/usr/bin/python3", "-m", "pip", *arguments])
    raise RuntimeError(f"guard invoked under unsupported name: {command}")


if __name__ == "__main__":
    raise SystemExit(main())
