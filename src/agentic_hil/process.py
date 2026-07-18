from __future__ import annotations

import os
import signal
import subprocess
import time
from contextlib import suppress

CHILD_REAP_TIMEOUT_S = 5.0


def process_group_kwargs() -> dict[str, object]:
    if os.name == "nt":
        return {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
    return {"start_new_session": True}


def register_process_group(child: subprocess.Popen) -> subprocess.Popen:
    if os.name != "nt":
        try:
            child_pgid = os.getpgid(child.pid)
        except ProcessLookupError:
            child_pgid = child.pid
        child._agentic_hil_pgid = child_pgid
    return child


def terminate_process_tree(child: subprocess.Popen, timeout_s: float) -> None:
    if os.name == "nt":
        if child.poll() is None:
            subprocess.run(["taskkill", "/PID", str(child.pid), "/T", "/F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        with suppress(subprocess.TimeoutExpired):
            child.wait(timeout=max(0.1, timeout_s))
        if child.poll() is None:
            subprocess.run(["taskkill", "/PID", str(child.pid), "/T", "/F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
            child.wait(timeout=max(0.1, timeout_s))
        return

    child_pgid = _child_process_group(child)
    if child_pgid is None or child_pgid == os.getpgrp():
        _terminate_single_child(child, timeout_s)
        return

    with suppress(ProcessLookupError):
        os.killpg(child_pgid, signal.SIGTERM)
    if _wait_for_process_group(child, child_pgid, timeout_s):
        return
    with suppress(ProcessLookupError):
        os.killpg(child_pgid, signal.SIGKILL)
    if not _wait_for_process_group(child, child_pgid, timeout_s):
        _terminate_single_child(child, timeout_s)


def _child_process_group(child: subprocess.Popen) -> int | None:
    stored = getattr(child, "_agentic_hil_pgid", None)
    if isinstance(stored, int):
        return stored
    if child.poll() is not None:
        return child.pid
    try:
        return os.getpgid(child.pid)
    except ProcessLookupError:
        return None


def _terminate_single_child(child: subprocess.Popen, timeout_s: float) -> None:
    if child.poll() is None:
        with suppress(ProcessLookupError):
            child.terminate()
    try:
        child.wait(timeout=max(0.1, timeout_s))
    except subprocess.TimeoutExpired:
        with suppress(ProcessLookupError):
            child.kill()
        child.wait(timeout=max(0.1, timeout_s))


def _wait_for_process_group(child: subprocess.Popen, pgid: int, timeout_s: float) -> bool:
    deadline = time.monotonic() + max(0.1, timeout_s)
    while True:
        with suppress(subprocess.TimeoutExpired):
            child.wait(timeout=0)
        if child.poll() is not None and not _process_group_exists(pgid):
            return True
        if time.monotonic() >= deadline:
            return child.poll() is not None and not _process_group_exists(pgid)
        time.sleep(0.02)


def _process_group_exists(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
