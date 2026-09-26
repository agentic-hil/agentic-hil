"""One serial port is one lock, whichever of its names a workspace wrote down.

Linux gives the bench's port two names that open the same device: the
`/dev/serial/by-id/` link `agentic-hil init` writes, and the kernel node the
link leads to. Two workspaces of one user that name the port by different
spellings still share one port, so a hold taken through either name refuses the
other `device_busy`, naming its holder, before anything opens the port (#584).

Each test builds two workspaces from the demo, one configured with the bench's
link and one with the node it resolves to, and drives each through an
`agentic-hil mcp-stdio` of its own. Their entries name the port by the device
alone (`identity_source: device`): an entry that names the adapter's serial is
locked by that serial whatever its spelling, and the spelling is the subject
here. Both directions run, so either workspace may be the holder.

Nothing here outlives the test. The workspaces and their state are the test's
own, and a device hold ends with the server that took it, which the fixture
closes whatever the test did.
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import suppress
from pathlib import Path

import pytest
import yaml
from support import scaled_time_bound

from .conftest import BENCH_ONLY, DEMO, Bench, child_command, isolated_environment

pytestmark = [pytest.mark.bench, BENCH_ONLY]

LINK_DIRECTORY = "/dev/serial/by-id/"
# The keys that tie an entry to one adapter, taken out so that each workspace's
# lock follows the device name it wrote and nothing else.
HARDWARE_IDENTITY = ("serial_number", "resource_id", "vid", "pid", "identity_source")
LABEL = "one-port-one-lock"
REPLY_TIMEOUT_S = scaled_time_bound(120.0)
SHUTDOWN_TIMEOUT_S = scaled_time_bound(60.0)
MCP_PROTOCOL_VERSION = "2025-06-18"


class Server:
    """One workspace's `agentic-hil mcp-stdio`, driven as an agent host drives it.

    The tier's client, cut to what these tests ask of it: answers are pulled off
    stdout by a thread and matched by id, so every request has a deadline, and
    stderr goes to a file that nothing can deadlock on.
    """

    def __init__(self, project: Path, environment: dict[str, str], stderr_path: Path) -> None:
        self.stderr_path = stderr_path
        self._stderr = stderr_path.open("w", encoding="utf-8")
        self.process = subprocess.Popen(
            child_command("mcp-stdio"),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self._stderr,
            text=True,
            encoding="utf-8",
            cwd=str(project),
            env=environment,
        )
        self._answers: queue.Queue[str | None] = queue.Queue()
        self._pump = threading.Thread(target=self._collect, daemon=True)
        self._pump.start()
        self._last_id = 0

    @property
    def pid(self) -> int:
        return self.process.pid

    def _collect(self) -> None:
        try:
            for line in self.process.stdout or ():
                self._answers.put(line)
        finally:
            self._answers.put(None)

    def _send(self, message: dict) -> None:
        assert self.process.stdin is not None
        self.process.stdin.write(json.dumps(message) + "\n")
        self.process.stdin.flush()

    def stderr_text(self) -> str:
        if not self._stderr.closed:
            self._stderr.flush()
        try:
            return self.stderr_path.read_text(encoding="utf-8").strip()
        except OSError:  # pragma: no cover - a stderr file this host cannot read is not the failure
            return ""

    def request(self, method: str, params: dict | None = None) -> dict:
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
        hello = self.request(
            "initialize",
            {"protocolVersion": MCP_PROTOCOL_VERSION, "capabilities": {}, "clientInfo": {"name": "agentic-hil-bench-tier", "version": "0"}},
        )
        assert hello["result"]["serverInfo"]["name"] == "agentic-hil", hello
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def call(self, name: str, arguments: dict | None = None) -> dict:
        """One tool call, answered with the tool's own document, held to the `isError` flag beside it."""
        answered = self.request("tools/call", {"name": name, "arguments": arguments or {}})
        assert "result" in answered, answered
        document = answered["result"]["structuredContent"]
        assert isinstance(document, dict), answered
        if document.get("ok") is not True:
            assert answered["result"]["isError"] is True, answered
        return document

    def close(self) -> None:
        """Close its input, which ends its session and its run and gives its holds back; kill it if it will not end."""
        if self.process.poll() is None:
            with suppress(OSError):  # a pipe already gone needs no closing
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


@pytest.fixture
def spellings(bench: Bench) -> tuple[str, dict[str, str]]:
    """The port's name in a plan, and the two device names it answers to on this host."""
    port = bench.com_port_name()
    link = str(bench.configuration()["com_ports"][port].get("device") or "")
    if not link.startswith(LINK_DIRECTORY):
        pytest.skip(f"this bench configures its serial port as {link!r}, not as a {LINK_DIRECTORY} link, so the port has no second spelling to test")
    node = os.path.realpath(link)
    if node == link or not os.path.exists(node):
        pytest.skip(f"{link} leads to no device node on this host")
    return port, {"link": link, "node": node}


@pytest.fixture
def workspace(bench: Bench, spellings: tuple[str, dict[str, str]], tmp_path: Path) -> Iterator[Callable[[str], Server]]:
    """Start a workspace of this user that names the bench's port by one spelling, served over MCP.

    A copy of the demo, with its configuration beside it rather than inside it,
    since the product refuses a configuration stored in the workspace it
    governs. The configuration is the bench's own apart from the port's device
    name, the keys that name the adapter, and the two roots. The state root is
    the workspace's own: two workspaces sharing one would meet at its
    per-resource locks, which name no holder, before they reach the device lock
    this is about.
    """
    port, devices = spellings
    started: list[Server] = []

    def start(spelling: str) -> Server:
        name = f"{spelling}-{len(started)}"
        project = tmp_path / f"workspace-{name}"
        shutil.copytree(DEMO, project, ignore=shutil.ignore_patterns("build", ".agentic-hil"))
        document = bench.configuration()
        entry = {key: value for key, value in document["com_ports"][port].items() if key not in HARDWARE_IDENTITY}
        document["com_ports"][port] = {**entry, "device": devices[spelling], "identity_source": "device"}
        document["workspace_root"] = str(project.resolve())
        document["state_root"] = str((tmp_path / f"state-{name}").resolve())
        config = tmp_path / f"config-{name}.yaml"
        config.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
        environment = isolated_environment(bench.config_root, bench.state_root, AGENTIC_HIL_CONFIG=str(config))
        server = Server(project, environment, tmp_path / f"mcp-stdio-{name}.stderr")
        # Registered before the handshake, so a server that will not greet is
        # still one the teardown closes.
        started.append(server)
        server.greet()
        return server

    yield start
    failures: list[Exception] = []
    for server in reversed(started):
        try:
            server.close()
        except Exception as error:
            failures.append(error)
    if failures:
        raise failures[0]


@pytest.mark.parametrize(("held_by", "asked_by"), [("link", "node"), ("node", "link")])
def test_a_run_holding_the_port_by_one_name_refuses_a_session_by_the_other_until_it_stops(
    spellings: tuple[str, dict[str, str]], workspace: Callable[[str], Server], held_by: str, asked_by: str
) -> None:
    port, _ = spellings
    holder = workspace(held_by)
    contender = workspace(asked_by)
    started = holder.call("bench_run_start", {"devices": [{"kind": "uart", "id": port}], "label": LABEL})
    assert started["ok"] is True, started

    refused = contender.call("com_session_start", {"port_id": port})
    assert refused["ok"] is False, refused
    assert refused["error_type"] == "device_busy", refused
    assert refused["holder"]["pid"] == holder.pid, refused
    assert refused["holder"]["label"] == LABEL, refused

    assert holder.call("bench_run_stop")["ok"] is True
    opened = contender.call("com_session_start", {"port_id": port})
    assert opened["ok"] is True, opened
    assert contender.call("com_session_stop", {"port_id": port})["ok"] is True


@pytest.mark.parametrize(("held_by", "asked_by"), [("link", "node"), ("node", "link")])
def test_a_session_holding_the_port_by_one_name_refuses_a_session_by_the_other(
    spellings: tuple[str, dict[str, str]], workspace: Callable[[str], Server], held_by: str, asked_by: str
) -> None:
    port, _ = spellings
    holder = workspace(held_by)
    contender = workspace(asked_by)
    opened = holder.call("com_session_start", {"port_id": port})
    assert opened["ok"] is True, opened

    refused = contender.call("com_session_start", {"port_id": port})
    assert refused["ok"] is False, refused
    assert refused["error_type"] == "device_busy", refused
    assert refused["holder"]["pid"] == holder.pid, refused

    assert holder.call("com_session_stop", {"port_id": port})["ok"] is True
