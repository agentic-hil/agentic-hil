"""The bench as a newcomer's Linux account meets it: the probe attached, and closed to this user.

The account is not in the group the probe's udev rule gives its USB device to,
or no rule is installed, and the serial port belongs to a group the account has
not joined either. The probe is plugged in and the configuration is right;
`init` and `doctor` say so, because they read sysfs and open nothing. The first
call that opens a device is refused by the device node's mode, and what the
product says then decides whether the newcomer changes their account or goes
looking for a probe that is already there.

This stage runs only in `tools/bench_in_container.py --without-device-group`,
which hands the container the probe's nodes and withholds every group they are
opened through, and says so in `AGENTIC_HIL_BENCH_DEVICE_GROUPS=withheld`. The
tier's conftest runs this module alone there and leaves it out of every other
run, so a full tier never meets it and it never meets a full tier.

Before any call the stage checks that the withholding holds. A node this process
may open for reading and writing would make every assertion below a claim about
a refusal that never happened, so that is a failure and never a skip. Nothing
here opens a device: the check asks the kernel whether it would, and the calls
are the product's own, through the MCP server an agent reaches them through.

OpenOCD's transcript is compared whole against the recording the unit tier's
fake prints (tests/fixtures/fake_openocd_access_denied.py), the way the
container tier holds the recording of a missing probe, so the fake cannot drift
from the binary this image installs.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
from collections.abc import Iterator
from contextlib import suppress
from pathlib import Path

import pytest
from fixtures.fake_openocd_access_denied import RECORDED_ACCESS_DENIED_RETURNCODE, RECORDED_ACCESS_DENIED_STDERR
from support import scaled_time_bound

from .conftest import BENCH_ONLY, WITHOUT_DEVICE_GROUP, Bench, child_command, refuse

pytestmark = [pytest.mark.bench, getattr(pytest.mark, WITHOUT_DEVICE_GROUP), BENCH_ONLY]

CLIENT_PROTOCOL_VERSION = "2025-06-18"
# Long enough for OpenOCD to start and be refused on a slow link, short enough
# that a wedged server fails this file rather than holding the session.
RESPONSE_TIMEOUT_S = 300.0
SHUTDOWN_TIMEOUT_S = scaled_time_bound(60.0)
STDERR_TAIL_LINES = 40

# Where the kernel puts the nodes libusb opens. In the container the runner
# builds, the probe's is the only one there.
USB_NODES = Path("/dev/bus/usb")

STALE_RECORDING = (
    "the recording in tests/fixtures/fake_openocd_access_denied.py is stale: take it again from this image with the "
    "probe's group withheld, and note the version and the date"
)


def usb_nodes() -> list[Path]:
    return sorted(node for bus in USB_NODES.iterdir() if bus.is_dir() for node in bus.iterdir())


@pytest.fixture(scope="module", autouse=True)
def nothing_here_may_be_opened(bench: Bench) -> None:
    """The premise: this process may open neither the probe nor its serial port.

    `os.access` answers for this process's user and groups, and opens nothing.
    The tier's own setup reached this far without opening a device, so a node
    that answers yes here is a run in which nothing was withheld, and the stage
    has nothing to measure.
    """
    if not USB_NODES.is_dir() or not usb_nodes():
        refuse(f"{USB_NODES} holds no node, so this run was handed no probe and there is no refusal to measure")
    ports = bench.configuration().get("com_ports") or {}
    if not ports:
        refuse("`init` bound no serial port on this bench, and this stage measures the refusal of one")
    devices = [*usb_nodes(), *(Path(entry["device"]) for entry in ports.values())]
    opened = [str(node) for node in devices if os.access(node, os.R_OK | os.W_OK)]
    if opened:
        refuse(
            f"this process may open {', '.join(opened)} for reading and writing, so nothing was withheld and this stage "
            "has nothing to measure. It runs in tools/bench_in_container.py --without-device-group, on a machine where "
            "the probe is opened through a group and not through an ACL or a mode that admits everyone"
        )


def read_line(stream, timeout_s: float, method: str) -> str:
    box: queue.Queue[str] = queue.Queue(maxsize=1)

    def read_one() -> None:
        try:
            box.put(stream.readline())
        except (OSError, ValueError):
            box.put("")

    threading.Thread(target=read_one, daemon=True).start()
    try:
        return box.get(timeout=timeout_s)
    except queue.Empty:
        raise AssertionError(f"the MCP server did not answer `{method}` within {timeout_s:.0f}s") from None


class Server:
    """One live `agentic-hil mcp-stdio`, spoken to in JSON-RPC lines the way an agent host speaks to it."""

    def __init__(self, bench: Bench) -> None:
        self.process = subprocess.Popen(
            child_command("mcp-stdio"),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
            cwd=str(bench.project),
            env=bench.environment(),
        )
        self._next_id = 0
        self._errors: list[str] = []
        threading.Thread(target=self._drain_errors, daemon=True).start()
        handshake = self.request(
            "initialize",
            {"protocolVersion": CLIENT_PROTOCOL_VERSION, "capabilities": {}, "clientInfo": {"name": "bench-without-device-group", "version": "0"}},
        )
        assert handshake["serverInfo"]["name"] == "agentic-hil", handshake

    def _drain_errors(self) -> None:
        with suppress(OSError, ValueError):
            for line in self.process.stderr:
                self._errors.append(line)

    def errors(self) -> str:
        return "".join(self._errors[-STDERR_TAIL_LINES:])

    def request(self, method: str, params: dict) -> dict:
        if self.process.poll() is not None:
            raise AssertionError(f"the MCP server exited before `{method}` with status {self.process.returncode}:\n{self.errors()}")
        self._next_id += 1
        self.process.stdin.write(json.dumps({"jsonrpc": "2.0", "id": self._next_id, "method": method, "params": params}) + "\n")
        self.process.stdin.flush()
        line = read_line(self.process.stdout, RESPONSE_TIMEOUT_S, method)
        assert line.strip(), f"the MCP server closed its output during `{method}`:\n{self.errors()}"
        message = json.loads(line)
        assert "error" not in message, message
        assert message.get("id") == self._next_id, message
        return message["result"]

    def call(self, tool: str, arguments: dict | None = None) -> tuple[bool, dict]:
        answered = self.request("tools/call", {"name": tool, "arguments": arguments or {}})
        return bool(answered["isError"]), answered["structuredContent"]

    def close(self) -> None:
        process = self.process
        try:
            if process.poll() is None:
                with suppress(OSError, ValueError):
                    process.stdin.close()
                try:
                    process.wait(timeout=SHUTDOWN_TIMEOUT_S)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=SHUTDOWN_TIMEOUT_S)
        finally:
            for stream in (process.stdin, process.stdout, process.stderr):
                with suppress(OSError, ValueError):
                    stream.close()


@pytest.fixture()
def server(bench: Bench) -> Iterator[Server]:
    started = Server(bench)
    try:
        yield started
    finally:
        started.close()


def test_probe_target_says_the_probe_is_attached_and_this_user_may_not_open_it(server: Server) -> None:
    """Refused before `init`, named as a permission, and the board left alone.

    The public name stays `adapter_not_found`, whose meaning is "could not be
    found or opened"; the backend's type, the summary and the causes say it was
    the opening, and none of them sends the reader to connect a probe.
    """
    failed, result = server.call("probe_target")

    assert failed is True, result
    assert result["error_type"] == "adapter_not_found", result
    assert result["backend_error_type"] == "adapter_access_denied", result
    assert "may not open" in result["summary"], result["summary"]
    assert "group" in result["likely_causes"][0], result["likely_causes"]
    assert not any("not connected" in cause for cause in result["likely_causes"]), result["likely_causes"]
    assert any("udev rule" in step for step in result["remediation"]), result["remediation"]
    assert result["target_contacted"] is False, result
    assert result["retry_safe"] is True, result
    assert result["hardware_state"] == "unchanged", result
    assert result.get("quarantine_id") is None, result
    assert result["programmer_output"]["returncode"] == RECORDED_ACCESS_DENIED_RETURNCODE, result["programmer_output"]
    assert result["programmer_output"]["stderr"] == RECORDED_ACCESS_DENIED_STDERR, STALE_RECORDING


def test_a_serial_session_names_the_group_the_port_was_refused_for(bench: Bench, server: Server) -> None:
    """The serial port's half of the same first run: the OS's own refusal, and the group to join."""
    failed, result = server.call("com_session_start", {"port_id": bench.com_port_name(), "clear_buffer": True})

    assert failed is True, result
    assert result["error_type"] == "com_port_open_failed", result
    assert "Errno 13" in result["backend_error"], result
    assert "group" in result["likely_causes"][0], result["likely_causes"]
    assert not any("another program" in cause for cause in result["likely_causes"]), result["likely_causes"]
