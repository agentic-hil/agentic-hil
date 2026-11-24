"""A runtime and a surviving container for the bench-stage lock tests."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path


def main() -> int:
    directory = Path(sys.argv[1])
    command, *args = sys.argv[2:]
    if command in {"podman", "docker"}:
        command, *args = args
    if command == "worker":
        (directory / "worker-ready").touch()
        deadline = time.monotonic() + 120
        while not (directory / "finish-worker").exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        return 0

    marker = directory / f"runtime-{os.getpid()}"
    marker.touch()
    try:
        return run_command(directory, command, args)
    finally:
        marker.unlink(missing_ok=True)


def run_command(directory: Path, command: str, args: list[str]) -> int:
    container = directory / "container"
    name = container.read_text(encoding="utf-8") if container.exists() else ""
    if command == "container" and args[0] == "inspect":
        if (directory / "unresponsive").exists():
            (directory / "inspected-unresponsive").touch()
            print("runtime socket not found", file=sys.stderr)
            return 125
        if name == args[-1] and (directory / "visible").exists():
            (directory / "inspected-running").touch()
            if "RUNNER_TRACKING_ID" not in os.environ:
                (directory / "inspected-by-holder").touch()
            print("running")
            return 0
        print(f"Error: no such container {args[-1]}", file=sys.stderr)
        return 125
    if command == "ps":
        if name and (directory / "visible").exists():
            print(name)
        return 0
    if command == "stop":
        (directory / "stop-requested").touch()
        return 0
    if command == "rm":
        if (directory / "allow-removal").exists():
            (directory / "visible").unlink(missing_ok=True)
            (directory / "finish-worker").touch()
            (directory / "removed").touch()
        return 0
    raise ValueError(f"Unexpected runtime command {command!r}")


if __name__ == "__main__":
    raise SystemExit(main())
