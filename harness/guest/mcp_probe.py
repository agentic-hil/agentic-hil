#!/usr/bin/env python3
"""Minimal MCP stdio probe: assert the installed server exposes exactly the
expected tool surface (install-integrity check). Lease-free -- it only does
`initialize` + `tools/list`, no hardware tool calls. The server discovers its
policy from AGENTIC_HIL_CONFIG (inherited from the environment); v0.3.0 forbids
passing --config, so none is given.

Usage: mcp_probe.py [FIXTURE_DIR]
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

FIXTURE = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.home() / "fixture"
HARNESS = Path(__file__).resolve().parent


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

    def send(obj):
        proc.stdin.write(json.dumps(obj) + "\n")
        proc.stdin.flush()

    def recv():
        while True:
            line = proc.stdout.readline()
            if not line:
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
        try:
            proc.stdin.close()
        except Exception:
            pass
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()

    for c in checks:
        print(("PASS" if c["ok"] else "FAIL") + f": {c['name']} ({c['detail']})")
    ok = all(c["ok"] for c in checks)
    (HARNESS / "mcp_probe.json").write_text(json.dumps({"overall": "PASS" if ok else "FAIL", "ok": ok, "checks": checks}, indent=2))
    print("MCP PROBE:", "ALL PASS" if ok else "FAILURES PRESENT")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
