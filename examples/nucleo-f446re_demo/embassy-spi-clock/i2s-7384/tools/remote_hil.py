"""Minimal MCP stdio client for the Agentic HIL server on the bench host.

ssh runs the remote `agentic-hil mcp-stdio` in the bench workspace; requests are
newline-delimited JSON-RPC. Every request and response is saved as a receipt before and
after the call.
"""

import base64
import hashlib
import json
import os
import queue
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

# A JSON file naming ssh_executable, ssh_arguments and remote_mcp_command for the bench host.
PROFILE = Path(os.environ["AGENTIC_HIL_BENCH_PROFILE"])


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def structured(result: dict) -> dict:
    if "structuredContent" in result:
        return dict(result["structuredContent"])
    for block in result.get("content", []):
        if block.get("type") == "text":
            try:
                value = json.loads(block["text"])
            except (ValueError, TypeError):
                continue
            if isinstance(value, dict):
                return value
    return {}


class Remote:
    def __init__(self, audit: Path, timeout_s: float = 600) -> None:
        profile = json.loads(PROFILE.read_text(encoding="utf-8"))
        self.audit = audit
        audit.mkdir(parents=True, exist_ok=True)
        self.timeout_s = timeout_s
        self.seq = 0
        self.lines: "queue.Queue[str | None]" = queue.Queue()
        self.stderr = (audit / "transport.stderr.log").open("ab")
        self.proc = subprocess.Popen(
            [profile["ssh_executable"], *profile["ssh_arguments"], profile["remote_mcp_command"]],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.stderr,
            text=True, encoding="utf-8",
        )
        threading.Thread(target=self._pump, daemon=True).start()
        self.request("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                                    "clientInfo": {"name": "i2s-7384-evidence", "version": "1"}})
        self.proc.stdin.write('{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
        self.proc.stdin.flush()

    def _pump(self) -> None:
        for line in self.proc.stdout:
            self.lines.put(line)
        self.lines.put(None)

    def request(self, method: str, params: dict, label: str | None = None) -> dict:
        self.seq += 1
        req = {"jsonrpc": "2.0", "id": self.seq, "method": method, "params": params}
        name = label or (params.get("name") if method == "tools/call" else method.replace("/", "-"))
        receipt = self.audit / f"{self.seq:03d}-{name}.json"
        shown = json.loads(json.dumps(req))
        args = shown["params"].get("arguments", {})
        if "data_base64" in args:  # keep receipts small; the digest identifies the upload
            data = base64.b64decode(args["data_base64"])
            args["data_base64"] = f"<{len(data)} bytes, sha256 {hashlib.sha256(data).hexdigest()}>"
        record = {"started_at": now(), "request": shown}
        receipt.write_text(json.dumps(record, indent=1), encoding="utf-8")
        self.proc.stdin.write(json.dumps(req) + "\n")
        self.proc.stdin.flush()
        while True:
            line = self.lines.get(timeout=self.timeout_s)
            if line is None:
                record.update(error="transport closed", outcome="unconfirmed")
                receipt.write_text(json.dumps(record, indent=1), encoding="utf-8")
                raise ConnectionError("Agentic HIL MCP transport closed")
            resp = json.loads(line)
            if resp.get("id") == self.seq:
                break
        record.update(completed_at=now(), response=resp)
        receipt.write_text(json.dumps(record, indent=1), encoding="utf-8")
        if "error" in resp:
            raise ValueError(f"MCP error: {resp['error']}")
        return resp["result"]

    def call(self, name: str, arguments: dict | None = None) -> dict:
        return structured(self.request("tools/call", {"name": name, "arguments": arguments or {}}))

    def upload(self, path: Path) -> dict:
        data = path.read_bytes()
        return self.call("artifact_upload", {"filename": path.name,
                                             "data_base64": base64.b64encode(data).decode()})

    def close(self) -> None:
        self.proc.stdin.close()  # EOF makes the server release its leases normally
        try:
            self.proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self.proc.terminate()
            self.proc.wait(timeout=5)
        self.stderr.close()


if __name__ == "__main__":
    # Read-only survey: tools, configuration, ports, leases.
    remote = Remote(Path(sys.argv[1]), timeout_s=60)
    try:
        tools = remote.request("tools/list", {})
        print("tools:", ", ".join(t["name"] for t in tools["tools"]))
        for name in ("project_config_describe", "com_ports_list", "bench_run_status",
                     "hardware_lease_status", "debugger_info"):
            try:
                value = remote.call(name)
            except Exception as error:  # noqa: BLE001 - survey keeps going
                print(name, "failed:", error)
                continue
            print(f"== {name}: ok={value.get('ok')} error={value.get('error')}")
    finally:
        remote.close()
