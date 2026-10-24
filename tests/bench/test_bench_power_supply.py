"""A bench power supply driven through the product, the way an MCP host drives it.

Runs only on a bench (``AGENTIC_HIL_BENCH=1``) where
``AGENTIC_HIL_BENCH_POWER_SUPPLY`` names a bench workspace: a project whose
authoritative configuration declares a supply, its limits and the grants to set
it. That configuration is the owner's, and this module reads it
and never writes it. On a bench run without the variable the conftest
deselects this module rather than letting it skip, because a skip is no result:
nothing here counts as qualification unless it ran.

What it proves on each RS-3005P the workspace declares, on channel 1:

* ``power_supply_status`` answers;
* ``power_supply_set`` at half the owner's effective limit, rounded down to the
  type's resolution, is read back exactly;
* a request one resolution step above the effective limit is refused with
  ``power_supply_limit_exceeded``, and the set point stays where it was.

The set points the supply had before are written back at the end where they
were read and lie within the limits. The output is never switched.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import time
from contextlib import suppress
from decimal import ROUND_DOWN, Decimal
from pathlib import Path
from typing import Any

import pytest
from support import scaled_time_bound

from .conftest import BENCH_ONLY, POWER_SUPPLY, POWER_SUPPLY_ENV, child_command, import_path

# Read here, at collection, before the suite's own isolation moves the home and
# the configuration roots: the server this module starts has to find the
# owner's authoritative configuration and the device locks where they are.
OPERATOR_ENVIRONMENT = dict(os.environ)
_NAMED = OPERATOR_ENVIRONMENT.get(POWER_SUPPLY_ENV)

pytestmark = [
    pytest.mark.bench,
    BENCH_ONLY,
    getattr(pytest.mark, POWER_SUPPLY),
    pytest.mark.skipif(
        not (_NAMED and Path(_NAMED).is_dir()),
        reason=f"{POWER_SUPPLY_ENV} names no bench workspace with a power supply",
    ),
]

REPLY_TIMEOUT_S = scaled_time_bound(120.0)
SHUTDOWN_TIMEOUT_S = scaled_time_bound(60.0)
MCP_PROTOCOL_VERSION = "2025-06-18"
CHANNEL = 1

# Prints the type and the limits of every configured supply, and nothing else
# from the file: never a serial.
LIMITS = """
import json, sys
from agentic_hil.config import load_authoritative_config

def text(value):
    return None if value is None else str(value)

config = load_authoritative_config(sys.argv[1])
print(json.dumps({
    supply_id: {
        "type": supply.type,
        "voltage_limit_v": text(supply.voltage_limit_v),
        "current_limit_a": text(supply.current_limit_a),
        "channels": {
            str(number): {"voltage_limit_v": text(channel.voltage_limit_v), "current_limit_a": text(channel.current_limit_a)}
            for number, channel in supply.channels.items()
        },
    }
    for supply_id, supply in config.power_supplies.items()
}))
"""


def workspace() -> Path:
    return Path(OPERATOR_ENVIRONMENT[POWER_SUPPLY_ENV]).resolve()


def child_environment() -> dict[str, str]:
    return {**OPERATOR_ENVIRONMENT, "PYTHONPATH": import_path(OPERATOR_ENVIRONMENT.get("PYTHONPATH"))}


def configured_limits() -> dict[str, Any]:
    done = subprocess.run(
        [*child_command()[:2], "-c", LIMITS, str(workspace())],
        cwd=str(workspace()),
        env=child_environment(),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=REPLY_TIMEOUT_S,
        check=False,
    )
    assert done.returncode == 0, f"the bench workspace's authoritative configuration did not load: {done.stderr.strip()}"
    return json.loads(done.stdout)


def effective(limits: dict[str, Any], quantity: str) -> Decimal:
    """The lower of the supply's and the channel's limit; both are the owner's, and neither may be missing."""
    key = {"voltage": "voltage_limit_v", "current": "current_limit_a"}[quantity]
    supply_limit = limits.get(key)
    channel_limit = limits.get("channels", {}).get(str(CHANNEL), {}).get(key)
    missing = [name for name, value in ((key, supply_limit), (f"channels.{CHANNEL}.{key}", channel_limit)) if value is None]
    if missing:
        pytest.fail(f"the bench workspace sets no {', '.join(missing)}; only the owner writes these limits, so this run cannot pick a set point")
    return min(Decimal(supply_limit), Decimal(channel_limit))


class Server:
    """One `agentic-hil mcp-stdio` child in the bench workspace, answered by id with a deadline per request."""

    def __init__(self, stderr_path: Path) -> None:
        self._stderr = stderr_path.open("w", encoding="utf-8")
        self.stderr_path = stderr_path
        self.process = subprocess.Popen(
            child_command("mcp-stdio"),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self._stderr,
            text=True,
            encoding="utf-8",
            cwd=str(workspace()),
            env=child_environment(),
        )
        self._answers: queue.Queue[str | None] = queue.Queue()
        self._pump = threading.Thread(target=self._collect, daemon=True)
        self._pump.start()
        self._last_id = 0

    def _collect(self) -> None:
        try:
            for line in self.process.stdout or ():
                self._answers.put(line)
        finally:
            self._answers.put(None)

    def stderr_text(self) -> str:
        self._stderr.flush()
        try:
            return self.stderr_path.read_text(encoding="utf-8").strip()
        except OSError:  # pragma: no cover - a stderr file this host cannot read is not the failure
            return ""

    def _send(self, message: dict[str, Any]) -> None:
        assert self.process.stdin is not None
        self.process.stdin.write(json.dumps(message) + "\n")
        self.process.stdin.flush()

    def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._last_id += 1
        request_id = self._last_id
        self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}})
        deadline = time.monotonic() + REPLY_TIMEOUT_S
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError(f"the MCP server did not answer `{method}` within {REPLY_TIMEOUT_S}s. Its stderr said: {self.stderr_text()}")
            try:
                line = self._answers.get(timeout=remaining)
            except queue.Empty:  # pragma: no cover - the deadline above is what ends this loop
                continue
            if line is None:
                raise AssertionError(f"the MCP server ended before answering `{method}`. Its stderr said: {self.stderr_text()}")
            message = json.loads(line)
            if message.get("id") == request_id:
                return message

    def greet(self) -> None:
        hello = self.request("initialize", {"protocolVersion": MCP_PROTOCOL_VERSION, "capabilities": {}, "clientInfo": {"name": "agentic-hil-bench-tier", "version": "0"}})
        assert hello["result"]["serverInfo"]["name"] == "agentic-hil", hello
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def tool(self, name: str, **arguments: object) -> dict[str, Any]:
        answered = self.request("tools/call", {"name": name, "arguments": dict(arguments)})
        assert "result" in answered, answered
        envelope = answered["result"]
        document = envelope["structuredContent"]
        assert isinstance(document, dict), envelope
        assert envelope.get("isError", False) is (document.get("ok") is not True), envelope
        return document

    def close(self) -> None:
        if self.process.poll() is None:
            with suppress(OSError):
                if self.process.stdin is not None:
                    self.process.stdin.close()
            try:
                self.process.wait(timeout=SHUTDOWN_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=SHUTDOWN_TIMEOUT_S)
        self._pump.join(timeout=SHUTDOWN_TIMEOUT_S)
        if self.process.stdout is not None:
            self.process.stdout.close()
        self._stderr.close()


def reading(status: dict[str, Any], quantity: str) -> Decimal | None:
    """Channel 1's set point for `quantity` out of a `power_supply_status` document."""
    channels = status["channels"]
    entry = channels[str(CHANNEL)] if isinstance(channels, dict) else next(item for item in channels if item.get("channel") == CHANNEL)
    value = entry.get(f"set_{quantity}")
    return None if value is None else Decimal(str(value))


def test_a_set_point(tmp_path: Path) -> None:
    """Half the effective limit reads back exactly; one resolution step above it is refused and changes nothing."""
    from agentic_hil.power_supply import POWER_SUPPLY_TYPES

    supplies = {supply_id: limits for supply_id, limits in configured_limits().items() if limits["type"] == "rs3005p"}
    assert supplies, "the bench workspace declares no rs3005p power supply"
    server = Server(tmp_path / "mcp-stdio.stderr")
    try:
        server.greet()
        for supply_id, limits in sorted(supplies.items()):
            ranges = POWER_SUPPLY_TYPES[limits["type"]]["ranges"][str(CHANNEL)]
            step_v = Decimal(ranges["voltage"]["resolution"])
            step_a = Decimal(ranges["current"]["resolution"])
            limit_v, limit_a = effective(limits, "voltage"), effective(limits, "current")
            half_v = (limit_v / 2).quantize(step_v, rounding=ROUND_DOWN)
            half_a = (limit_a / 2).quantize(step_a, rounding=ROUND_DOWN)
            assert half_v > 0 and half_a > 0, (supply_id, limit_v, limit_a)

            before = server.tool("power_supply_status", supply_id=supply_id)
            assert before["ok"] is True, before
            previous = (reading(before, "voltage_v"), reading(before, "current_a"))
            try:
                written = server.tool("power_supply_set", supply_id=supply_id, channel=CHANNEL, voltage_v=float(half_v), current_a=float(half_a))
                assert written["ok"] is True, written
                after = server.tool("power_supply_status", supply_id=supply_id)
                assert (reading(after, "voltage_v"), reading(after, "current_a")) == (half_v, half_a), after

                refused = server.tool("power_supply_set", supply_id=supply_id, channel=CHANNEL, voltage_v=float(limit_v + step_v))
                assert (refused["ok"], refused["error_type"]) == (False, "power_supply_limit_exceeded"), refused
                unchanged = server.tool("power_supply_status", supply_id=supply_id)
                assert reading(unchanged, "voltage_v") == half_v, unchanged
            finally:
                if None not in previous and previous[0] <= limit_v and previous[1] <= limit_a:
                    server.tool("power_supply_set", supply_id=supply_id, channel=CHANNEL, voltage_v=float(previous[0]), current_a=float(previous[1]))
    finally:
        server.close()
