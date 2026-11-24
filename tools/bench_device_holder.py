"""Own a bench stage's host device locks until its container is gone."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from contextlib import ExitStack, suppress
from datetime import datetime, timezone
from pathlib import Path
from queue import Empty, Queue
from typing import TextIO

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentic_hil import device_lock_protocol  # noqa: E402

POLL_S = 0.25
INSPECT_TIMEOUT_S = 2


def read_control(stream: TextIO, messages: Queue[dict | None]) -> None:
    """Read lines into a queue so the owner can service deadlines and detect EOF."""
    try:
        with stream:
            for line in stream:
                messages.put(json.loads(line))
    finally:
        messages.put(None)


def send_control(stream: TextIO, **message: object) -> None:
    print(json.dumps(message), file=stream, flush=True)


def inspect_container(
    argv: list[str], name: str, run: Callable[[list[str]], subprocess.CompletedProcess[str]],
) -> str | None:
    """Return None only for an explicit missing-container answer for this name."""
    try:
        result = run([*argv, "container", "inspect", "--format", "{{.State.Status}}", name])
    except (OSError, subprocess.SubprocessError) as error:
        return f"unknown: {error}"
    if result.returncode == 0:
        return result.stdout.strip() or "present"

    lines = (result.stderr.strip() or result.stdout.strip()).splitlines()
    answer = lines[-1].strip() if lines else "no inspection result"
    # Older Podman and Docker versions wrap the missing-container diagnostic.
    prefix = r"(?:error:\s*(?:(?:error )?inspecting object:\s*)?|error response from daemon:\s*)?"
    identifier = rf"""(?P<quote>["']?)(?-i:{re.escape(name)})(?P=quote)"""
    missing = (
        rf"{prefix}no such container:?\s+{identifier}",
        rf"(?:error:\s*)?no container with name or id {identifier} found(?:: no such container)?",
        rf"(?:error:\s*)?container {identifier} (?:not found|does not exist)",
    )
    if any(re.fullmatch(pattern, answer, re.IGNORECASE) for pattern in missing):
        return None
    return f"unknown: {answer}"


def release_holder_record(path: Path, record: dict[str, object], *, shared: bool) -> None:
    # Update while the file lock is still held. A failed record write still
    # allows the descriptor to close.
    with suppress(OSError):
        device_lock_protocol.write_holder(path, {**record, "state": "released"}, shared=shared)


def acquire_devices(locks: ExitStack, root: Path, keys: list[str], label: str, *, shared: bool) -> dict[str, str]:
    paths = {key: device_lock_protocol.lock_path(root, key) for key in keys}
    with ExitStack() as attempt:
        for path in paths.values():
            try:
                descriptor = device_lock_protocol.take_lock(path, shared=shared)
            except device_lock_protocol.LockOccupied:
                return {"busy": device_lock_protocol.holder_label(path)}
            except OSError as error:
                return {"error": f"could not reserve the stage's devices: {error}"}
            attempt.callback(os.close, descriptor)
        acquired_at = datetime.now(timezone.utc).isoformat()
        owner = {"pid": os.getpid(), "label": label}
        for key, path in paths.items():
            record = {"version": 1, "state": "held", "acquired_at": acquired_at, "owner": owner, "resource": key}
            try:
                device_lock_protocol.write_holder(path, record, shared=shared)
            except OSError as error:
                return {"error": f"could not publish the stage's device hold: {error}"}
            attempt.callback(release_holder_record, path, record, shared=shared)
        locks.enter_context(attempt.pop_all())
    return {"status": "ready"}


def run_inspect(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command, stdin=subprocess.DEVNULL, capture_output=True, encoding="utf-8",
        errors="replace", timeout=INSPECT_TIMEOUT_S, check=False,
    )


def watch_container(messages: Queue[dict | None], argv: list[str], name: str, launch_grace_s: float) -> bool:
    """Return whether the stage requested release, or wait for an orphan to go."""
    seen = False
    while True:
        try:
            # Once the runtime has shown the container, the stage handles
            # removal while attached. Resume inspection on control-channel EOF.
            message = messages.get(timeout=None if seen else POLL_S)
        except Empty:
            state = inspect_container(argv, name, run_inspect)
            seen = state is not None and not state.startswith("unknown:")
            continue
        if message is None:
            break
        return message["action"] == "release"

    sys.stdout.close()
    deadline = time.monotonic() + launch_grace_s
    while True:
        can_release = seen or time.monotonic() >= deadline
        state = inspect_container(argv, name, run_inspect)
        if state is None and can_release:
            return False
        seen = seen or (state is not None and not state.startswith("unknown:"))
        time.sleep(POLL_S)


def main() -> None:
    messages: Queue[dict | None] = Queue()
    threading.Thread(target=read_control, args=(sys.stdin, messages), daemon=True).start()
    release = False
    with ExitStack() as locks:
        while (request := messages.get()) is not None:
            if request["action"] == "acquire":
                send_control(sys.stdout, **acquire_devices(
                    locks, Path(request["root"]), request["keys"], request["label"], shared=request["shared"],
                ))
            elif request["action"] == "launch":
                # A broken reply pipe means the stage never received permission
                # to submit the launch.
                send_control(sys.stdout, status="launch")
                release = watch_container(messages, request["argv"], request["name"], request["launch_grace_s"])
                break
    if release:
        send_control(sys.stdout, status="released")


if __name__ == "__main__":
    with suppress(BrokenPipeError):
        main()
