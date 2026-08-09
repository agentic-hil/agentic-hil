#!/usr/bin/env python3
"""Fake MCP stdio server for tests: answers `initialize` and `tools/list`, nothing else.

The tool surface is read from the JSON file named as the first argument, so a
test decides what the client sees. That is all `evals/hil/mcp_probe.py` asks a
server for, which is what lets the probe be run to completion on a machine with
no board attached and no server installed.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

SERVER_VERSION = "0.0.0-fake"


def main() -> int:
    tools = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    server_name = sys.argv[2] if len(sys.argv) > 2 else "agentic-hil"
    while True:
        line = sys.stdin.readline()
        if not line:
            return 0
        line = line.strip()
        if not line:
            continue
        message = json.loads(line)
        method = message.get("method")
        if method == "initialize":
            result = {"serverInfo": {"name": server_name, "version": SERVER_VERSION}, "capabilities": {}}
        elif method == "tools/list":
            result = {"tools": tools}
        else:
            # Notifications carry no id and expect no reply.
            continue
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    raise SystemExit(main())
