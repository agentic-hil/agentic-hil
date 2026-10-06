"""What the killed-flash bench test says when it fails, kept apart so it is tested offline.

The test in test_bench_faults.py kills OpenOCD while it writes, and two things
it has seen fail once and never again (#620, #621) left no line saying why. So
a failure there now carries:

* the whole recovery block the aborted flash returned, as JSON, rather than
  the repr pytest cuts off after a few fields, and every debugger log written
  since the flash started, with its exit status and the lines a failure is
  read out of: the reset's own capture holds the backend's error line, which
  the recovery block does not;
* a serial port the kill made the in-circuit debugger remove and create again,
  named for what it is. A container started with fixed `--device` bindings
  keeps the node it was given, so a port that came back under another device
  number is out of its reach, and the product's `com_port_open_failed` that
  follows is about the binding, not about the product.

Nothing here opens a device: the port is looked at through sysfs and `stat`.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

# The fields that change when the kernel removes a tty and creates it again: its
# sysfs entry is a new one, and so is the node, or it carries another number.
IDENTITY_FIELDS = ("name", "sysfs_inode", "sysfs_link_inode", "node_inode", "node_rdev")
STDERR_TAIL_LINES = 8


def debugger_logs_since(directory: Path, since: float) -> list[dict[str, Any]]:
    """Every debugger log in `directory` written at or after `since`, oldest name first.

    The product names each log by its start time and the tool, so the name order
    is the order the calls ran in. A log that cannot be read is listed with the
    reason rather than left out.
    """
    try:
        candidates = sorted(directory.glob("*.log"))
    except OSError:
        return []
    logs: list[dict[str, Any]] = []
    for path in candidates:
        try:
            if path.stat().st_mtime < since:
                continue
            recorded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            logs.append({"name": path.name, "unreadable": f"{type(error).__name__}: {error}"})
            continue
        if not isinstance(recorded, dict):
            logs.append({"name": path.name, "unreadable": "the log is not a JSON object"})
            continue
        logs.append(
            {
                "name": path.name,
                "returncode": recorded.get("returncode"),
                "timed_out": recorded.get("timed_out"),
                "stdout": str(recorded.get("stdout") or ""),
                "stderr": str(recorded.get("stderr") or ""),
            }
        )
    return logs


def decisive_lines(capture: str) -> list[str]:
    """The lines of a debugger capture a failure is read out of."""
    return [line for line in capture.splitlines() if "error" in line.lower() or "fail" in line.lower()]


def recovery_report(recovery: dict[str, Any], logs: list[dict[str, Any]]) -> str:
    """The recovery block whole, then each debugger log since the flash started."""
    lines = ["The recovery the aborted flash ran, whole:", json.dumps(recovery, indent=1, sort_keys=True, default=str)]
    if not logs:
        lines.append("There is no debugger log from this flash or its recovery.")
    for log in logs:
        if "unreadable" in log:
            lines.append(f"{log['name']}: unreadable, {log['unreadable']}")
            continue
        capture = f"{log['stdout']}\n{log['stderr']}"
        lines.append(f"{log['name']}: returncode {log['returncode']}, timed out {log['timed_out']}")
        lines.extend(f"  decisive: {line}" for line in decisive_lines(capture))
        tail = [line for line in log["stderr"].splitlines() if line.strip()][-STDERR_TAIL_LINES:]
        lines.extend(f"  stderr: {line}" for line in tail)
    return "\n".join(lines)


def reenumerated_fields(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    """Which of the port's identity facts changed between two snapshots; empty for the same port.

    A sysfs entry that cannot be read any more counts as changed: the tty it
    named is gone.
    """
    changed = [field for field in IDENTITY_FIELDS if before.get(field) != after.get(field)]
    if after.get("sysfs_stat_error") is not None and "sysfs_inode" not in changed:
        changed.append("sysfs_inode")
    return changed


def reaches_its_tty(snapshot: dict[str, Any], sysfs_root: Path) -> bool:
    """Whether the node the port's name leads to carries the number sysfs gives that tty now."""
    name = snapshot.get("name")
    if not isinstance(name, str) or snapshot.get("node_rdev") is None or snapshot.get("sysfs_inode") is None:
        return False
    try:
        number = (sysfs_root / name / "dev").read_text(encoding="ascii").strip()
    except OSError:
        return False
    return number == f"{snapshot.get('major')}:{snapshot.get('minor')}"


def wait_for_the_serial_port(
    before: dict[str, Any],
    snapshot: Callable[[], dict[str, Any]],
    sysfs_root: Path,
    *,
    timeout_s: float = 12.0,
    poll_interval_s: float = 0.25,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> str | None:
    """None when the port is the one the run started with, or one it still reaches; else why not.

    A port that was not re-enumerated is answered at once. One that was is
    waited on for the same bound the USB reset stage gives a re-enumerated node
    (`usb_reset_support.wait_for_usb_device`): under a live device tree the
    stable name leads to the new node once udev has made it, and the test goes
    on. Under fixed bindings it never does, and the answer names that.
    """
    if timeout_s <= 0 or poll_interval_s <= 0:
        raise ValueError("the serial port wait bounds must be positive")
    deadline = clock() + timeout_s
    while True:
        after = snapshot()
        changed = reenumerated_fields(before, after)
        if not changed or reaches_its_tty(after, sysfs_root):
            return None
        remaining = deadline - clock()
        if remaining <= 0:
            return (
                f"the killed flash left the in-circuit debugger's serial port {before.get('name')} re-enumerated, "
                f"removed and created again ({', '.join(changed)} changed), and the node this run reaches the port through "
                f"(now {after.get('name')}) is still not the new one after {timeout_s:g}s. A container started with fixed "
                "--device bindings keeps the node it was given, so every serial call after this would fail on the binding, "
                "not on the product; a run with --live-device-tree follows the new node."
            )
        sleep(min(poll_interval_s, remaining))
