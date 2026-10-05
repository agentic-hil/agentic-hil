"""What the killed-flash bench test says when it fails, kept apart so it is tested offline.

The test in test_bench_faults.py kills OpenOCD while it writes, and two things
it has seen fail once and never again (#620, #621) left no line saying why. So
a failure there now carries:

* the whole recovery block the aborted flash returned, as JSON, rather than
  the repr pytest cuts off after a few fields, and every debugger log written
  since the flash started, with its exit status and the lines a failure is
  read out of: the reset's own capture holds the backend's error line, which
  the recovery block does not.

Nothing here opens a device: the port is looked at through sysfs and `stat`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

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

