#!/usr/bin/env python3
"""Minimal MCP stdio probe: assert the installed server exposes exactly the
expected tool surface (install-integrity check). Lease-free -- it only does
`initialize` + `tools/list`, no hardware tool calls. The server discovers its
policy from AGENTIC_HIL_CONFIG (inherited from the environment), so no
repository-controlled --config argument is given.

Usage: mcp_probe.py [FIXTURE_DIR]
"""
from __future__ import annotations

import contextlib
import json
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

FIXTURE = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.home() / "fixture"
HARNESS = Path(__file__).resolve().parent
RESPONSE_TIMEOUT_S = 10.0


def dig(obj, *path):
    for key in path:
        if not isinstance(obj, dict):
            return None
        obj = obj.get(key)
    return obj


def main() -> int:
    env = dict(os.environ)
    env["PATH"] = str(Path.home() / ".local" / "bin") + os.pathsep + env.get("PATH", "")
    proc = subprocess.Popen(
        ["agentic-hil", "mcp-stdio"],
        cwd=str(FIXTURE),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        bufsize=1,
        env=env,
    )
    response_lines: queue.Queue[str | None] = queue.Queue()

    def read_stdout() -> None:
        try:
            for line in proc.stdout:
                response_lines.put(line)
        finally:
            response_lines.put(None)

    stdout_thread = threading.Thread(target=read_stdout, name="mcp-probe-stdout", daemon=True)
    stdout_thread.start()

    def send(obj):
        proc.stdin.write(json.dumps(obj) + "\n")
        proc.stdin.flush()

    def recv():
        deadline = time.monotonic() + RESPONSE_TIMEOUT_S
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"MCP response timed out after {RESPONSE_TIMEOUT_S:g}s")
            try:
                line = response_lines.get(timeout=remaining)
            except queue.Empty as error:
                raise TimeoutError(f"MCP response timed out after {RESPONSE_TIMEOUT_S:g}s") from error
            if line is None:
                raise RuntimeError("mcp server closed stdout")
            line = line.strip()
            if line:
                return json.loads(line)

    checks: list[dict] = []
    try:
        send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
              "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "hil-probe", "version": "1"}}})
        init = recv()
        server = dig(init, "result", "serverInfo", "name")
        checks.append({"name": "initialize", "ok": server == "agentic-hil", "detail": str(dig(init, "result", "serverInfo", "version"))})
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})

        send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        listed = recv()
        names = {t["name"] for t in dig(listed, "result", "tools") or []}
        expected = {x.strip() for x in (HARNESS / "tools.list.expected").read_text().split() if x.strip()}
        missing, added = expected - names, names - expected
        checks.append({"name": "tool surface matches snapshot", "ok": not missing and not added,
                       "detail": f"count={len(names)} missing={sorted(missing)} added={sorted(added)}"})
    finally:
        with contextlib.suppress(Exception):
            proc.stdin.close()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        stdout_thread.join(timeout=1)

    for c in checks:
        print(("PASS" if c["ok"] else "FAIL") + f": {c['name']} ({c['detail']})")
    ok = all(c["ok"] for c in checks)
    (HARNESS / "mcp_probe.json").write_text(json.dumps({"overall": "PASS" if ok else "FAIL", "ok": ok, "checks": checks}, indent=2))
    print("MCP PROBE:", "ALL PASS" if ok else "FAILURES PRESENT")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
