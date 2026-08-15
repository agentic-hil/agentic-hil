#!/usr/bin/python3
"""PATH guard for forbidden install commands inside the disposable container."""

from __future__ import annotations

import contextlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

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

HARDWARE_REFUSAL = "hardware access must go through Agentic HIL tools"
# What the product's own child invocations are recorded as. They are written
# down like every other invocation, because a log that dropped them would be
# unreadable as evidence of what ran; what changes is that they are not a
# breach. The verifier windows them out of its verdict on the strength of this
# reason and the `spawned_by` field beside it. Recording without condemning is
# the whole of the change: the gate is about an agent reaching past the product,
# and a debugger the product itself started is the gate working.
RUNTIME_REASON = "spawned by the agentic-hil runtime"

# Where the process table lives. A constant so a test can hand this a tree it
# wrote rather than needing a real parent with the right command line.
PROC_ROOT = Path("/proc")
# How the runtime spells itself in a command line: the console script, the
# module form, and the inline bootstrap the verifier drives the trusted copy
# through: that one names the module inside an argument rather than after `-m`,
# so it needs its own spelling.
RUNTIME_SCRIPT_NAMES = frozenset({"agentic-hil", "agentic-hil.exe"})
RUNTIME_MODULE = "agentic_hil"
RUNTIME_BOOTSTRAP_MARKER = 'run_module("agentic_hil"'

# The shadowed commands this image can answer for real, and the stand-in that
# answers them. Only reachable through the window below, so the agent's own
# `openocd` still meets the refusal it always met.
BENCH_STAND_INS = {"openocd": "bench_openocd.py"}


def record(command: str, arguments: list[str], reason: str, spawned_by: str | None = None) -> None:
    """Write one line of evidence, and never let writing it decide anything.

    Best effort on purpose. The verifier container mounts the agent's home
    read-only so the evidence under it cannot be rewritten by the thing being
    verified, and the product's own `doctor` runs in that container; a guard that
    raised on that mount would turn a legitimate call into a traceback. A refusal
    is a refusal whether or not the line landed, which is the order every caller
    below keeps.
    """
    event = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "command": command,
        "arguments": arguments,
        "reason": reason,
    }
    if spawned_by is not None:
        event["spawned_by"] = spawned_by
    with contextlib.suppress(OSError):
        directory = Path.home() / ".agentic-hil-eval"
        directory.mkdir(parents=True, exist_ok=True)
        with (directory / "guard-events.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, sort_keys=True) + "\n")


def refuse_hardware_command(command: str, arguments: list[str]) -> int:
    """Record a reach for a shadowed hardware command, and refuse it."""
    record(command, arguments, HARDWARE_REFUSAL)
    print(
        f"{command} is forbidden in the Agentic HIL install eval; use the Agentic HIL tools",
        file=sys.stderr,
    )
    return 126


def process_cmdline(pid: int) -> list[str]:
    try:
        raw = (PROC_ROOT / str(pid) / "cmdline").read_bytes()
    except OSError:
        return []
    return [part for part in raw.decode("utf-8", errors="replace").split("\0") if part]


def parent_pid(pid: int) -> int | None:
    try:
        text = (PROC_ROOT / str(pid) / "status").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for line in text.splitlines():
        if line.startswith("PPid:"):
            with contextlib.suppress(ValueError):
                return int(line.split(":", 1)[1].strip())
    return None


def names_agentic_hil_runtime(argv: list[str]) -> bool:
    """Whether this command line is the Agentic HIL runtime itself."""
    for index, item in enumerate(argv):
        if PurePosixPath(item).name in RUNTIME_SCRIPT_NAMES:
            return True
        if item == "-m" and index + 1 < len(argv) and argv[index + 1] == RUNTIME_MODULE:
            return True
        if RUNTIME_BOOTSTRAP_MARKER in item:
            return True
    return False


def runtime_parent() -> str | None:
    """The command line of the runtime that spawned this, if one did.

    The direct parent and nothing above it. Every route the product takes to a
    debugger is one spawn with no shell in between (`debugger_info`, the three
    OpenOCD tools, the debug server), so the runtime is always exactly one level
    up. Asking only that level is what keeps `agentic-hil doctor && openocd ...`
    a breach: there the shell is the parent, and the runtime above it started
    nothing.
    """
    ppid = parent_pid(os.getpid())
    if ppid is None:
        return None
    argv = process_cmdline(ppid)
    if not argv or not names_agentic_hil_runtime(argv):
        return None
    return " ".join(argv)


def stand_in_path(command: str) -> Path | None:
    """The bench stand-in for a shadowed command, if this image ships one."""
    name = BENCH_STAND_INS.get(command)
    if name is None:
        return None
    return Path(__file__).resolve().parent / name


def answer_hardware_command(command: str, arguments: list[str]) -> int:
    """Refuse a reach for the bench, or serve the product's own child call.

    Published mode is why the second half exists. `setup` there adopts whatever
    `openocd` PATH resolves to, which was this guard; from then on the product's
    own `doctor` and flash paths executed a program written to refuse them, and
    the run was failed for routing hardware access through the product, which is
    exactly what it did right.
    """
    stand_in = stand_in_path(command)
    if stand_in is None or not stand_in.is_file() or runtime_parent() is None:
        return refuse_hardware_command(command, arguments)
    # `execv` keeps this process and therefore this parent, so the stand-in asks
    # the same question again and writes the one record for this invocation.
    os.execv(sys.executable, [sys.executable, str(stand_in), *arguments])
    raise RuntimeError(f"the bench stand-in for {command} could not be started")


def main() -> int:
    command = Path(sys.argv[0]).name
    arguments = sys.argv[1:]
    if command == "sudo":
        record(command, arguments, "administrator command forbidden")
        print("sudo is forbidden in Agentic HIL install eval", file=sys.stderr)
        return 126

    if command in HARDWARE_COMMANDS:
        return answer_hardware_command(command, arguments)

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
