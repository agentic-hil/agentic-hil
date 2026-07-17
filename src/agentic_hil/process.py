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
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(child.pid), "/T", "/F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    else:
        try:
            os.killpg(os.getpgid(child.pid), signal.SIGTERM)
        except ProcessLookupError:
            return
    try:
        child.wait(timeout=max(0.1, timeout_s))
    except subprocess.TimeoutExpired:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(child.pid), "/T", "/F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        else:
            with suppress(ProcessLookupError):
                os.killpg(os.getpgid(child.pid), signal.SIGKILL)
        child.wait(timeout=max(0.1, timeout_s))
