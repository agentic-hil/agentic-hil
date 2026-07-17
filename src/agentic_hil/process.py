from __future__ import annotations

import os
import signal
import subprocess
from contextlib import suppress

CHILD_REAP_TIMEOUT_S = 5.0


def process_group_kwargs() -> dict[str, object]:
    if os.name == "nt":
        return {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
    return {"start_new_session": True}


def terminate_process_tree(child: subprocess.Popen, timeout_s: float) -> None:
    if child.poll() is not None:
        return
    kill_group = False
    child_pgid = None
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(child.pid), "/T", "/F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    else:
        try:
            child_pgid = os.getpgid(child.pid)
        except ProcessLookupError:
            return
        kill_group = child_pgid != os.getpgrp()
        if kill_group:
            os.killpg(child_pgid, signal.SIGTERM)
        else:
            # Defensive fallback for callers/tests that did not isolate the child.
            with suppress(ProcessLookupError):
                child.terminate()
    try:
        child.wait(timeout=max(0.1, timeout_s))
    except subprocess.TimeoutExpired:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(child.pid), "/T", "/F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        elif kill_group and child_pgid is not None:
            with suppress(ProcessLookupError):
                os.killpg(child_pgid, signal.SIGKILL)
        else:
            with suppress(ProcessLookupError):
                child.kill()
        child.wait(timeout=max(0.1, timeout_s))
