"""The CAN tools and CAN plans over a real SocketCAN interface.

Every CAN test under tests/ replaces python-can with a namespace object and
`ip` with a recorded envelope, so nothing in the suite has ever bound a CAN_RAW
socket. What the kernel does with one is decided here: a bind to an interface
that is not there, a bind to one that is there and down, a send on a link that
went down under a session, a read on a link that was deleted under one, and a
frame that has to reach a second socket on the same interface as the frame the
plan wrote.

The interface is a `vcan` netdev the test creates inside the container, which
needs the host kernel's module and NET_ADMIN; where it cannot be created the
tests skip naming that, and the job that runs this tier reads the skip as a
failure. The far end is a second python-can socket in this process, or the
scripted peer in `can_peer.py` for the plan runs, whose reply table is written
beside the plan that reads it. Neither is a board and neither runs firmware:
they are the transport peer for a CAN_RAW socket, which is the thing under
test. `candump` reads the wire once as the observer with no stake in the answer.

Every product call goes through `agentic-hil mcp-stdio` over its own pipe or
through the `test-reactor` command, and what is asserted is the whole document
that came back, because the assertion gaps the bench found were in the outer
fields nothing had read (#443, #447).

The issue this file pins beside the transport is #501: a second session on a
channel a session of this very process already holds is refused as if another
process held it, with no holder named.

Recorded 2026-09-06 against python-can 4.6.1 and the iproute2 this image
installs; the python-can version is asserted so the record cannot go stale
silently.
"""

from __future__ import annotations

import itertools
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from agentic_hil.can import IP_COMMAND_PATHS

from .conftest import COMMAND_TIMEOUT_S, CONTAINER_ONLY, a_line_within, fixture_configuration

pytestmark = [pytest.mark.container, CONTAINER_ONLY]

PEER_SCRIPT = Path(__file__).with_name("can_peer.py")

# How long the live server may take to answer one call before that is a failure
# rather than a slow machine. A read that waits out its own timeout is bounded
# by BUS_TIMEOUT_S below, which is far inside this.
ANSWER_TIMEOUT_S = 60.0
# How long the far end waits for a frame the product said it sent.
WIRE_TIMEOUT_S = 5.0
# The bus entry's own `timeout_s`, which bounds every read's wait.
BUS_TIMEOUT_S = 2.0
# Slack for a wait that is measured against the clock: scheduling, the pipe,
# the JSON on either side. A read that waited its second and came back inside
# this is a read that waited its second.
WAIT_SLACK_S = 1.5

_interface_numbers = itertools.count()
# One token per worker process, drawn once. `can:socketcan:<channel>` is a
# machine-wide lock, so two workers on one name would meet as `device_busy`, and
# `pytest -n` runs this tier in several processes at once. A random token keeps
# two workers apart where `pid mod 1000` could collide two whose pids agree on
# the low three digits; the counter keeps two tests in one worker apart. Four
# digits, not hex: a `peak` bus routes to socketcan only for a channel matching
# `vcan\\d+`, so the whole name after `vcan` stays numeric.
_INTERFACE_TOKEN = f"{secrets.randbelow(10000):04d}"


def a_fresh_interface_name() -> str:
    """One vcan name per test, unique on this host.

    Ten characters (`vcan` + a four-digit per-process token + a two-digit
    counter), all digits after `vcan`, inside the kernel's fifteen-character
    limit on an interface name.
    """
    return f"vcan{_INTERFACE_TOKEN}{next(_interface_numbers):02d}"


def ip_command() -> str:
    """The `ip` the product itself would read a link with, or whatever is on PATH."""
    installed = next((path for path in IP_COMMAND_PATHS if Path(path).is_file()), None)
    if installed is None:
        installed = shutil.which("ip")
    assert installed is not None, "iproute2 is not in this image"
    return installed


def ip_link(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([ip_command(), "link", *arguments], capture_output=True, text=True, timeout=30, check=False)


def observer_is_bound(channel: str, seconds: float) -> bool:
    """Whether a receiver for this interface is listed in the kernel's CAN rcvlist.

    A bound `CAN_RAW` socket (candump's, here) shows up as a row for its
    interface in `/proc/net/can/rcvlist_all`. Polling that is how a test waits
    for the observer to be listening before it sends, instead of a fixed sleep
    that races a loaded runner.
    """
    rcvlist = Path("/proc/net/can/rcvlist_all")
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            listed = rcvlist.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return False
        if any(line.split()[:1] == [channel] for line in listed.splitlines()):
            return True
        time.sleep(0.02)
    return False


def a_can_raw_socket_binds_here() -> str | None:
    """Why a CAN_RAW socket cannot be opened on this host, or None where it can.

    `ip link add ... type vcan` needs only the `vcan` module, and it succeeds on
    a kernel that has `vcan` but not `can_raw`. The product's transport binds a
    `CAN_RAW` socket, which needs `can_raw`, and without it the bind answers
    EAFNOSUPPORT and the classifier reports `can_adapter_open_failed` with a
    quarantine: every test here would then fail as if the product were wrong.
    Probed once, before an interface is made, so that gap is a named skip rather
    than a wall of red. The plan asks for exactly this probe.
    """
    family = getattr(socket, "AF_CAN", None)
    raw = getattr(socket, "CAN_RAW", None)
    if family is None or raw is None:
        return "this Python has no AF_CAN/CAN_RAW, so the CAN sub-tier cannot bind the socket the product binds"
    try:
        probe = socket.socket(family, socket.SOCK_RAW, raw)
    except OSError as error:
        return f"a CAN_RAW socket could not be opened here ({error}): the CAN sub-tier needs the host kernel's can_raw module, not only vcan"
    probe.close()
    return None


@contextmanager
def virtual_interface(*, up: bool = True) -> Iterator[str]:
    """A vcan netdev for one test, deleted afterwards.

    The one condition that is a skip rather than a failure: the interface, or a
    CAN_RAW socket on it, could not be made, which is the host kernel's modules
    or the container's capability. The reason is named so the job that reads
    this tier's report can print it.
    """
    no_socket = a_can_raw_socket_binds_here()
    if no_socket is not None:
        pytest.skip(f"{no_socket}, and CAP_NET_ADMIN on the container")
    name = a_fresh_interface_name()
    added = ip_link("add", "dev", name, "type", "vcan")
    if added.returncode != 0:
        detail = added.stderr.strip() or f"ip link add exited {added.returncode}"
        pytest.skip(f"a vcan interface could not be created here ({detail}): the CAN sub-tier needs the host kernel's vcan module and CAP_NET_ADMIN on the container")
    try:
        if up:
            brought_up = ip_link("set", "up", name)
            assert brought_up.returncode == 0, brought_up.stderr
        yield name
    finally:
        ip_link("del", name)


@pytest.fixture
def vcan() -> Iterator[str]:
    with virtual_interface() as name:
        yield name


@pytest.fixture
def vcan_down() -> Iterator[str]:
    """A vcan that exists and was never brought up."""
    with virtual_interface(up=False) as name:
        yield name


@pytest.fixture
def vcan_fd() -> Iterator[str]:
    """A vcan whose MTU carries a CAN FD frame (72 bytes: the 64-byte payload plus the header).

    The MTU is set while the interface is down: the kernel refuses an MTU change
    on an up vcan, so the interface is created down, given its MTU, then brought
    up.
    """
    with virtual_interface(up=False) as name:
        assert ip_link("set", name, "mtu", "72").returncode == 0, "the kernel refused mtu 72 on the vcan"
        assert ip_link("set", "up", name).returncode == 0, "the vcan could not be brought up after its mtu was set"
        yield name


def bus_entry(
    bus_id: str,
    channel: str,
    *,
    adapter: str = "socketcan",
    allow_write: bool = True,
    listen_only: bool = False,
    fd: bool = False,
    receive_own_messages: bool = False,
    max_buffer_frames: int = 8,
) -> str:
    """One `can_buses` entry on a SocketCAN channel, at configuration version 3.

    Reading needs no grant at this version; `allow_write` is the one permission
    the entry carries. `timeout_s` bounds every read's wait and every send.
    `adapter` is the logical adapter written into the config verbatim, so a test
    can name `peak` on a netdev-shaped channel and watch it route to socketcan.
    """
    return f"""  {bus_id}:
    adapter: {adapter}
    channel: {channel!r}
    bitrate: 500000
    fd: {str(fd).lower()}
    receive_own_messages: {str(receive_own_messages).lower()}
    timeout_s: {BUS_TIMEOUT_S}
    max_buffer_frames: {max_buffer_frames}
    listen_only: {str(listen_only).lower()}
    permissions:
      allow_write: {str(allow_write).lower()}
"""


def can_project(tmp_path: Path, *entries: str) -> tuple[Path, Path]:
    """A project and a configuration declaring these buses, and nothing that reaches hardware."""
    project = tmp_path / "project"
    project.mkdir(exist_ok=True)
    config = fixture_configuration(project, tmp_path / "config" / "config.yaml", tmp_path / "state", can_buses_yaml="can_buses:\n" + "".join(entries))
    return project, config


class LiveServer:
    """`agentic-hil mcp-stdio` over its own pipe, spoken to as an agent host speaks to it."""

    def __init__(self, project: Path, config: Path, *, client: str = "can-over-vcan") -> None:
        self.process = subprocess.Popen(
            [sys.executable, "-m", "agentic_hil", "mcp-stdio"],
            cwd=str(project),
            env={**os.environ, "AGENTIC_HIL_CONFIG": str(config)},
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self._next_id = itertools.count(1)
        initialized = self.request("initialize", {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": client, "version": "0"}})
        assert initialized["result"]["protocolVersion"], initialized

    @property
    def pid(self) -> int:
        return self.process.pid

    def request(self, method: str, params: dict) -> dict:
        assert self.process.stdin is not None and self.process.stdout is not None
        request_id = next(self._next_id)
        self.process.stdin.write(json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}) + "\n")
        self.process.stdin.flush()
        answered = a_line_within(self.process.stdout, ANSWER_TIMEOUT_S)
        assert answered is not None, f"the server did not answer {method} within {ANSWER_TIMEOUT_S:.0f}s"
        assert answered.strip(), f"the server answered nothing to {method}: {self.stderr_so_far()}"
        document = json.loads(answered)
        assert document.get("id") == request_id, document
        return document

    def call(self, tool: str, arguments: dict | None = None) -> dict:
        """One `tools/call`, answered with the tool's own document."""
        document = self.request("tools/call", {"name": tool, "arguments": arguments or {}})
        assert "result" in document, document
        return document["result"]["structuredContent"]

    def stderr_so_far(self) -> str:
        if self.process.poll() is None:
            return "it is still running"
        assert self.process.stderr is not None
        return self.process.stderr.read()

    def close(self) -> None:
        """EOF on stdin, which is how a lost client ends a server and releases what it held."""
        if self.process.poll() is not None:
            return
        assert self.process.stdin is not None
        self.process.stdin.close()
        try:
            self.process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=30)


@contextmanager
def live_server(project: Path, config: Path, **kwargs: str) -> Iterator[LiveServer]:
    server = LiveServer(project, config, **kwargs)
    try:
        yield server
    finally:
        server.close()


@contextmanager
def far_end(channel: str, *, fd: bool = False) -> Iterator[object]:
    """A second CAN_RAW socket on the interface, held by this test.

    A frame the product sends is what this receives, and a frame this sends is
    what the product reads. It is python-can's own socket and nothing of this
    project's. `fd` opens it CAN FD-capable, which a socket has to be to receive
    an FD frame off the wire.
    """
    import can

    bus = can.Bus(interface="socketcan", channel=channel, fd=fd)
    try:
        yield bus
    finally:
        bus.shutdown()


def send_from_far_end(bus: object, frame_id: int, data: bytes, *, extended: bool = False, rtr: bool = False, fd: bool = False) -> None:
    import can

    bus.send(  # type: ignore[attr-defined]
        can.Message(arbitration_id=frame_id, data=data, is_extended_id=extended, is_remote_frame=rtr, is_fd=fd),
        timeout=WIRE_TIMEOUT_S,
    )


def a_frame_at_the_far_end(bus: object, timeout_s: float = WIRE_TIMEOUT_S) -> tuple[int, bytes] | None:
    message = bus.recv(timeout=timeout_s)  # type: ignore[attr-defined]
    if message is None:
        return None
    return int(message.arbitration_id), bytes(message.data)


@contextmanager
def flooding(bus: object, frames: list[tuple[int, bytes, dict]], *, gap_s: float = 0.001) -> Iterator[None]:
    """Keep sending frames from the far end for as long as the block runs.

    A background sender, so a read-until-match or a drain that cannot keep up has
    a bus that keeps carrying traffic under it. Each entry is `(id, data, kwargs)`
    where kwargs is forwarded to `send_from_far_end` (for example `extended`).
    """
    stop = threading.Event()

    def pump() -> None:
        while not stop.is_set():
            for frame_id, data, kwargs in frames:
                if stop.is_set():
                    break
                try:
                    send_from_far_end(bus, frame_id, data, **kwargs)
                except Exception:
                    return
                if gap_s:
                    time.sleep(gap_s)

    sender = threading.Thread(target=pump, daemon=True)
    sender.start()
    try:
        yield
    finally:
        stop.set()
        sender.join(timeout=5)


@contextmanager
def scripted_peer(channel: str, ready: Path, *replies: str) -> Iterator[subprocess.Popen[str]]:
    """`can_peer.py` on the far end, answering from the table given here."""
    command = [sys.executable, str(PEER_SCRIPT), "--channel", channel, "--ready", str(ready)]
    for reply in replies:
        command += ["--reply", reply]
    peer = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + WIRE_TIMEOUT_S
        while not ready.exists() and time.monotonic() < deadline and peer.poll() is None:
            time.sleep(0.02)
        assert ready.exists(), f"the peer did not bind {channel}: {peer.stderr.read() if peer.poll() is not None else 'still starting'}"
        yield peer
    finally:
        if peer.poll() is None:
            peer.terminate()
        try:
            peer.wait(timeout=10)
        except subprocess.TimeoutExpired:
            peer.kill()
            peer.wait(timeout=10)


def reactor(project: Path, config: Path, plan: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    """`agentic-hil test-reactor` on this plan, in this project."""
    return subprocess.run(
        [sys.executable, "-m", "agentic_hil", "test-reactor", "--test-config", str(plan), *arguments],
        cwd=str(project),
        env={**os.environ, "AGENTIC_HIL_CONFIG": str(config)},
        capture_output=True,
        text=True,
        timeout=COMMAND_TIMEOUT_S,
        check=False,
    )


def write_plan(project: Path, name: str, read_claim: str) -> Path:
    """A version 3 plan that sends one frame and waits for the peer's answer.

    The peer's table answers `0x123/01` with `0x124/02`, so a claim of `02`
    is met and any other claim is not; both plans are otherwise the same
    four steps.
    """
    plan = project / f"{name}.yaml"
    plan.write_text(
        f"""version: 3
name: {name}
steps:
  - {{device: bus, action: can_open}}
  - {{device: bus, action: can_send, frame_id: "0x123", data_hex: "01"}}
  - {{device: bus, action: can_read, comparator: {{id: "0x124", equals: "{read_claim}"}}, timeout_s: 3}}
  - {{device: bus, action: can_close}}
""",
        encoding="utf-8",
    )
    return plan


# ---------------------------------------------------------------------------
# What this was recorded against.


def test_the_python_can_this_image_installs_is_the_one_this_was_recorded_against() -> None:
    import can

    assert can.__version__ == "4.6.1", can.__version__


# ---------------------------------------------------------------------------
# The listing, a session, and frames both ways.


def test_can_buses_list_describes_a_socketcan_bus_before_any_session(tmp_path: Path, vcan: str) -> None:
    """The listing an agent reads before it opens anything, off the live server.

    `link_verified` is what listen-only would rest on for this adapter, and the
    bitrate is said to be configuration and not a measurement, because
    python-can's SocketCAN backend takes no bitrate at all.
    """
    project, config = can_project(tmp_path, bus_entry("bus", vcan))

    with live_server(project, config) as server:
        listed = server.call("can_buses_list")

    assert listed["ok"] is True, listed
    assert listed["tool"] == "can_buses_list", listed
    assert "socketcan" in listed["supported_adapters"], listed
    assert list(listed["buses"]) == ["bus"], listed
    bus = listed["buses"]["bus"]
    assert bus["adapter"] == "socketcan", bus
    assert bus["channel"] == vcan, bus
    assert bus["listen_only"] is False, bus
    assert bus["listen_only_enforcement"] == "link_verified", bus
    assert bus["session_active"] is False, bus
    assert bus["bitrate"] == 500000, bus
    assert bus["bitrate_verified"] is False, bus
    assert "ip link set" in bus["bitrate_note"], bus


def test_a_session_binds_the_interface_and_frames_cross_it_in_both_directions(tmp_path: Path, vcan: str) -> None:
    """Open, read what the far end sent, send what the far end then reads, close.

    The frame is read back field by field: the identifier in both spellings,
    the payload as hexadecimal, the DLC, and that it is neither extended nor
    remote, because the reactor's comparator and every agent read exactly
    these. The stop is proved by the lease: a second start afterwards is a
    fresh session and not `already_active`.
    """
    project, config = can_project(tmp_path, bus_entry("bus", vcan))

    with live_server(project, config) as server, far_end(vcan) as peer:
        started = server.call("can_session_start", {"bus_id": "bus"})
        assert started["ok"] is True, started
        assert started["tool"] == "can_session_start", started
        assert started["already_active"] is False, started
        assert started["adapter"] == "socketcan", started
        assert started["frames_drained"] == 0, started
        assert started["session"]["session_active"] is True, started
        assert started["bitrate_verified"] is False, started
        assert started["summary"] == "CAN bus session started.", started

        # A payload with letters in it, so the hexadecimal's case is part of
        # what is read back: the comparator normalises a plan's spelling to
        # lower case and compares against exactly this field.
        send_from_far_end(peer, 0x123, b"\x0a\xfe")
        read = server.call("can_read", {"bus_id": "bus", "wait_timeout_s": 1.0})
        assert read["ok"] is True, read
        assert read["tool"] == "can_read", read
        assert read["frames_read"] == 1, read
        assert read["summary"] == "CAN frame(s) read.", read
        frame = read["frames"][0]
        assert frame["id"] == 0x123, frame
        assert frame["id_hex"] == "0x123", frame
        assert frame["data_hex"] == "0afe", frame
        assert frame["dlc"] == 2, frame
        assert frame["extended"] is False and frame["rtr"] is False, frame

        sent = server.call("can_send", {"bus_id": "bus", "frame_id": "0x321", "data_hex": "aa bb"})
        assert sent["ok"] is True, sent
        assert sent["tool"] == "can_send", sent
        assert sent["summary"] == "CAN frame sent.", sent
        assert sent["frame"]["id_hex"] == "0x321", sent
        assert a_frame_at_the_far_end(peer) == (0x321, b"\xaa\xbb")

        stopped = server.call("can_session_stop", {"bus_id": "bus"})
        assert stopped["ok"] is True, stopped
        assert stopped["was_active"] is True, stopped
        assert stopped["session"]["session_active"] is False, stopped
        assert stopped["quarantined"] is False, stopped
        assert server.call("can_buses_list")["buses"]["bus"]["session_active"] is False

        again = server.call("can_session_stop", {"bus_id": "bus"})
        assert again["ok"] is True and again["was_active"] is False, again
        restarted = server.call("can_session_start", {"bus_id": "bus"})
        assert restarted["ok"] is True and restarted["already_active"] is False, restarted


def test_a_frame_the_product_sent_is_what_candump_reads_off_the_interface(tmp_path: Path, vcan: str) -> None:
    """The observer with no stake: can-utils reads the wire, not python-can.

    `candump -L` prints one frame per line as `(time) <if> <id>#<data>`, and
    `-n 1` ends it after that frame.
    """
    project, config = can_project(tmp_path, bus_entry("bus", vcan))
    candump = shutil.which("candump")
    assert candump is not None, "can-utils is not in this image"

    with live_server(project, config) as server:
        assert server.call("can_session_start", {"bus_id": "bus"})["ok"] is True
        observer = subprocess.Popen([candump, "-L", "-n", "1", vcan], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            # Wait for candump to be bound before sending, not a fixed sleep: the
            # kernel lists a socket's receiver for the interface in
            # /proc/net/can/rcvlist_all the moment it binds, so a frame sent
            # after that line appears is a frame candump will see. A fixed sleep
            # loses the frame on a loaded runner and turns this into a timeout
            # about the observer rather than an assertion about the product.
            assert observer_is_bound(vcan, WIRE_TIMEOUT_S), f"candump did not bind {vcan} within {WIRE_TIMEOUT_S:.0f}s: {observer.stderr.read() if observer.poll() is not None else 'still starting'}"
            sent = server.call("can_send", {"bus_id": "bus", "frame_id": "0x7ab", "data_hex": "deadbeef"})
            assert sent["ok"] is True, sent
            try:
                stdout, stderr = observer.communicate(timeout=WIRE_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                observer.kill()
                stdout, stderr = observer.communicate()
                raise AssertionError(f"candump read no frame off {vcan} after the product reported it sent 0x7ab: {stdout!r} {stderr!r}") from None
        finally:
            if observer.poll() is None:
                observer.kill()
                observer.wait(timeout=10)

    assert observer.returncode == 0, stderr
    lines = stdout.strip().splitlines()
    assert len(lines) == 1, stdout
    assert lines[0].split()[1:] == [vcan, "7AB#DEADBEEF"], stdout


# ---------------------------------------------------------------------------
# The read timeout.


def test_a_read_on_a_quiet_bus_waits_the_asked_time_and_no_longer_than_the_bus_timeout(tmp_path: Path, vcan: str) -> None:
    """`wait_timeout_s` is honoured up to the entry's `timeout_s`, which caps it.

    Two reads on a bus nobody writes: one asks for a second and gets it, one
    asks for far longer than the entry's `timeout_s` and is held to that. Both
    come back `ok` with no frames, because an empty read is an answer and not a
    failure.
    """
    project, config = can_project(tmp_path, bus_entry("bus", vcan))

    with live_server(project, config) as server:
        assert server.call("can_session_start", {"bus_id": "bus"})["ok"] is True

        began = time.monotonic()
        read = server.call("can_read", {"bus_id": "bus", "wait_timeout_s": 1.0})
        waited = time.monotonic() - began
        assert read["ok"] is True, read
        assert read["frames_read"] == 0 and read["frames"] == [], read
        assert read["summary"] == "No CAN frames were available.", read
        # The lower bound is the claim: the read really did wait its second. The
        # upper bound only guards against it running to the bus timeout, so it is
        # loose enough not to flake on a loaded runner where a pipe round trip
        # and two JSON encodings sit on top of the wait.
        assert waited >= 1.0, f"a read asked to wait 1.0s came back after only {waited:.2f}s"
        assert waited < BUS_TIMEOUT_S + WAIT_SLACK_S, f"a read asked to wait 1.0s waited {waited:.2f}s, past the bus timeout"

        began = time.monotonic()
        capped = server.call("can_read", {"bus_id": "bus", "wait_timeout_s": 30.0})
        waited = time.monotonic() - began
        assert capped["ok"] is True and capped["frames_read"] == 0, capped
        # The claim is that the entry's timeout_s capped the 30s ask: it waited at
        # least that long and nothing like the 30s it was told. The upper bound is
        # generous for the same reason as above, not a second product property.
        assert waited >= BUS_TIMEOUT_S, f"a read asked to wait 30s on a bus with timeout_s {BUS_TIMEOUT_S} came back after only {waited:.2f}s"
        assert waited < BUS_TIMEOUT_S + WAIT_SLACK_S + 2.0, f"a read asked to wait 30s on a bus with timeout_s {BUS_TIMEOUT_S} waited {waited:.2f}s, nowhere near capped"


# ---------------------------------------------------------------------------
# The gates: permission, listen-only.


def test_a_send_on_a_bus_that_may_not_be_written_is_refused_naming_the_key_and_nothing_reaches_the_wire(tmp_path: Path, vcan: str) -> None:
    """`allow_write: false` refuses the send with the dotted key an operator grants (#443)."""
    project, config = can_project(tmp_path, bus_entry("bus", vcan, allow_write=False))

    with live_server(project, config) as server, far_end(vcan) as peer:
        started = server.call("can_session_start", {"bus_id": "bus"})
        assert started["ok"] is True, started

        refused = server.call("can_send", {"bus_id": "bus", "frame_id": "0x100", "data_hex": "01"})
        assert refused["ok"] is False, refused
        assert refused["tool"] == "can_send", refused
        assert refused["error_type"] == "permission_denied", refused
        assert refused["permission"] == "can_buses.bus.permissions.allow_write", refused
        assert "can_buses.bus.permissions.allow_write" in refused["summary"], refused
        assert "grant" in refused["next_step"], refused
        assert refused["side_effect_committed"] is False, refused
        assert a_frame_at_the_far_end(peer, timeout_s=1.0) is None, "a refused send put a frame on the interface"

        # The refusal cost the session nothing: a read still works.
        assert server.call("can_read", {"bus_id": "bus", "wait_timeout_s": 0.0})["ok"] is True


def test_listen_only_is_refused_on_a_virtual_interface_and_the_send_gate_answers_before_any_session(tmp_path: Path, vcan: str) -> None:
    """Two refusals off the real `ip -details -json link show`.

    A vcan has no controller and so no listen-only mode to be in, so a
    `listen_only: true` entry on it never opens; and a send on that entry is
    refused for the mode before the session question is ever asked, whatever
    `allow_write` says. Nothing reaches the far end in either case.
    """
    project, config = can_project(tmp_path, bus_entry("bus", vcan, listen_only=True))

    with live_server(project, config) as server, far_end(vcan) as peer:
        started = server.call("can_session_start", {"bus_id": "bus"})
        assert started["ok"] is False, started
        assert started["error_type"] == "can_listen_only_unsupported", started
        assert started["field"] == "can_buses.bus.listen_only", started
        assert started["listen_only_requested"] is True and started["listen_only_confirmed"] is False, started
        assert started["link_state"] == f"{vcan} is a vcan interface, which has no CAN controller and therefore no listen-only mode to be in.", started
        assert started["side_effect_committed"] is False, started
        assert started["retry_safe"] is True, started
        assert started["quarantined"] is False, started
        assert server.call("can_buses_list")["buses"]["bus"]["session_active"] is False

        sent = server.call("can_send", {"bus_id": "bus", "frame_id": "0x100", "data_hex": "01"})
        assert sent["ok"] is False, sent
        assert sent["error_type"] == "can_listen_only_mode", sent
        assert sent["field"] == "can_buses.bus.listen_only", sent
        assert sent["listen_only"] is True, sent
        assert sent["listen_only_enforcement"] == "link_verified", sent
        assert sent["side_effect_committed"] is False and sent["retry_safe"] is False, sent
        # Over the real kernel too, the mode refusal names the bus and the entry
        # that declared it rather than a serial or debugger log (#523).
        mode_causes = sent["likely_causes"]
        assert mode_causes and not any("COM port" in cause or "debugger" in cause for cause in mode_causes), sent
        assert any("listen_only" in cause for cause in mode_causes), sent
        assert a_frame_at_the_far_end(peer, timeout_s=1.0) is None, "a refused send put a frame on the interface"


# ---------------------------------------------------------------------------
# An interface that is absent, one that is down, and links that change under a session.


def test_a_channel_that_names_no_interface_is_refused_as_not_found_and_leaves_the_bench_free(tmp_path: Path, vcan: str) -> None:
    """ENODEV from the real bind is the one open failure that is a refusal, not an incident.

    The bus that is there is started afterwards on the same server to prove
    the refusal quarantined nothing.
    """
    absent = f"{vcan}x"
    project, config = can_project(tmp_path, bus_entry("gone", absent), bus_entry("bus", vcan))

    with live_server(project, config) as server:
        refused = server.call("can_session_start", {"bus_id": "gone"})
        assert refused["ok"] is False, refused
        assert refused["error_type"] == "can_interface_not_found", refused
        assert refused["channel"] == absent, refused
        assert refused["field"] == "can_buses.gone.channel", refused
        assert refused["target_contacted"] is False, refused
        assert refused["side_effect_committed"] is False, refused
        assert refused["retry_safe"] is True, refused
        assert refused["quarantined"] is False and refused["lease_state"] == "released", refused
        assert "No such device" in refused["backend_error"], refused
        assert any("ip link" in step for step in refused["remediation"]), refused

        started = server.call("can_session_start", {"bus_id": "bus"})
        assert started["ok"] is True, started


def assert_refused_as_down(refused: dict, *, bus_id: str, channel: str, adapter: str) -> None:
    """Every field the decided refusal carries (#511)."""
    assert refused["ok"] is False, refused
    assert refused["error_type"] == "can_interface_down", refused
    assert refused["tool"] == "can_session_start", refused
    assert refused["bus_id"] == bus_id, refused
    assert refused["adapter"] == adapter, refused
    assert refused["field"] == f"can_buses.{bus_id}.channel", refused
    assert refused["channel"] == channel, refused
    assert refused["interface_state"] == "down", refused
    assert channel in refused["summary"] and "down" in refused["summary"], refused
    assert refused["target_contacted"] is False, refused
    assert refused["side_effect_committed"] is False, refused
    assert refused["side_effect_status"] == "not_started", refused
    assert refused["retry_safe"] is True, refused
    assert refused["quarantined"] is False, refused
    assert refused.get("cleanup_required") is not True, refused
    assert "quarantine_guidance" not in refused, refused
    assert refused["lease_state"] == "released", refused
    assert re.search(r"sudo ip link set \S+ up", refused["remediation"][0]), refused["remediation"]
    assert f"ip link set {channel} up" in json.dumps(refused), refused
    assert any("recover --confirm-safe-state" in step for step in refused["do_not"]), refused


def test_a_session_on_an_interface_that_is_down_is_refused_before_contact_for_both_clear_rx_queue_values(tmp_path: Path, vcan_down: str) -> None:
    """A link that exists and is down is refused as `can_interface_down` before the socket is opened (#511).

    The kernel lets a CAN_RAW socket bind an interface that is administratively
    down and answers a receive and a send with ENETDOWN, so the two answers this
    replaces were `can_queue_clear_failed` naming the drain (default
    `clear_rx_queue`) and `ok` over a link that carries nothing
    (`clear_rx_queue: false`); neither carried a `channel`, and neither told the
    operator to bring the link up. Now the state is read before the socket is
    opened (the IFF_UP flag; `operstate` reads `unknown` on an up vcan and is
    not the signal), both values of `clear_rx_queue` get the one refusal, the
    listing shows no session behind it, and once `ip link set up` brought the
    interface up the same entry opens the ordinary way on the same server.
    """
    project, config = can_project(tmp_path, bus_entry("bus", vcan_down))

    with live_server(project, config) as server:
        for arguments in ({"bus_id": "bus"}, {"bus_id": "bus", "clear_rx_queue": False}):
            refused = server.call("can_session_start", arguments)
            assert_refused_as_down(refused, bus_id="bus", channel=vcan_down, adapter="socketcan")
            assert "Network is down" not in json.dumps(refused), "the refusal was decided by the drain, not by the link state"
            assert server.call("can_buses_list")["buses"]["bus"]["session_active"] is False

        # Brought up, the same entry opens the ordinary way, with no restart.
        assert ip_link("set", "up", vcan_down).returncode == 0
        started = server.call("can_session_start", {"bus_id": "bus"})
        assert started["ok"] is True, started
        assert started["summary"] == "CAN bus session started.", started
        assert server.call("can_buses_list")["buses"]["bus"]["session_active"] is True
        assert server.call("can_session_stop", {"bus_id": "bus"})["ok"] is True


def test_a_peak_bus_on_a_down_netdev_meets_the_same_refusal_under_its_own_adapter_name(tmp_path: Path, vcan_down: str) -> None:
    """The `peak` netdev channels route through socketcan and reach the state read too."""
    project, config = can_project(tmp_path, bus_entry("bus", vcan_down, adapter="peak"))

    with live_server(project, config) as server:
        refused = server.call("can_session_start", {"bus_id": "bus"})
        assert_refused_as_down(refused, bus_id="bus", channel=vcan_down, adapter="peak")

        assert ip_link("set", "up", vcan_down).returncode == 0
        started = server.call("can_session_start", {"bus_id": "bus"})
        assert started["ok"] is True, started
        assert server.call("can_session_stop", {"bus_id": "bus"})["ok"] is True


def test_the_interface_state_the_product_reads_is_the_iff_up_flag_and_not_operstate(vcan_down: str) -> None:
    """The state read, against the real kernel's sysfs.

    Recorded in this image: a down vcan's `flags` reads `0x80` and its
    `operstate` `down`; the same vcan after `ip link set up` reads `0x81` and
    `operstate` `unknown`. So `operstate` cannot be the signal, and the read
    answers `up` where it says `unknown`. A name with no netdev has no sysfs
    entry and reads no state at all: only a proven down link refuses.

    What the kernel publishes is read and asserted first, before the product's
    seam is imported at all, so that this test states the recording the unit
    tier's fake answers with even on the run where the seam does not exist yet.
    """
    flags = Path("/sys/class/net") / vcan_down / "flags"
    operstate = Path("/sys/class/net") / vcan_down / "operstate"
    down_flags, down_operstate = flags.read_text(encoding="utf-8"), operstate.read_text(encoding="utf-8")
    assert ip_link("set", "up", vcan_down).returncode == 0
    up_flags, up_operstate = flags.read_text(encoding="utf-8"), operstate.read_text(encoding="utf-8")
    assert (down_flags, down_operstate) == ("0x80\n", "down\n"), (down_flags, down_operstate)
    assert (up_flags, up_operstate) == ("0x81\n", "unknown\n"), (up_flags, up_operstate)
    assert not (Path("/sys/class/net") / f"{vcan_down}x").exists()

    from agentic_hil.can import socketcan_interface_state

    assert socketcan_interface_state(vcan_down) == "up"
    assert ip_link("set", "down", vcan_down).returncode == 0
    assert socketcan_interface_state(vcan_down) == "down"
    assert socketcan_interface_state(f"{vcan_down}x") is None


def test_a_link_taken_down_under_a_session_fails_the_send_as_an_unknown_effect_that_ends_with_the_call(tmp_path: Path, vcan: str) -> None:
    """ENETDOWN on a bound socket is the failure the code keeps as an unknown effect.

    The interface existed and the session was on it; what the controller did
    with the frame before the link went down is not this call's to know, so the
    send is `can_send_failed` with an unknown effect, `cleanup_required`, and
    the reason recorded. And, as the workflow prompt says, a failed call is not
    a held bench: the incident stands down when the call ends, the result says
    so, and the session still reads and closes afterwards.
    """
    project, config = can_project(tmp_path, bus_entry("bus", vcan))

    with live_server(project, config) as server:
        assert server.call("can_session_start", {"bus_id": "bus"})["ok"] is True
        assert ip_link("set", "down", vcan).returncode == 0

        failed = server.call("can_send", {"bus_id": "bus", "frame_id": "0x100", "data_hex": "01"})
        assert failed["ok"] is False, failed
        assert failed["error_type"] == "can_send_failed", failed
        assert failed["backend_error"] == "Failed to transmit: Network is down [Error Code 100]", failed
        assert failed["side_effect_status"] == "unknown", failed
        assert failed["cleanup_required"] is True, failed
        assert failed["cleanup_reasons"] == ["can_effect_unconfirmed"], failed
        assert failed["incident_stood_down"]["stood_down"] is True, failed
        assert failed["quarantined"] is False, failed
        # Over the real kernel too, the send failure names causes about the bus
        # rather than falling through to a serial or debugger table (#517).
        causes = failed["likely_causes"]
        assert causes and not any("COM port" in cause or "debugger" in cause for cause in causes), failed
        assert any(("bus" in cause or "adapter" in cause or "interface" in cause or "node" in cause) for cause in causes), failed

        read = server.call("can_read", {"bus_id": "bus", "wait_timeout_s": 0.0})
        assert read["ok"] is True and read["lease_state"] == "active", read
        stopped = server.call("can_session_stop", {"bus_id": "bus"})
        assert stopped["ok"] is True and stopped["was_active"] is True and stopped["quarantined"] is False, stopped


def test_an_interface_deleted_under_a_session_fails_the_read_and_quarantines_nothing(tmp_path: Path, vcan: str) -> None:
    """A read transmits nothing, so a read that failed is a refusal with a retry, not an incident.

    `ip link del` unregisters the netdev under the bound socket; the kernel
    fails the next receive once. That failure names the read, is retry-safe,
    leaves the lease active, and the session still closes cleanly afterwards.
    """
    project, config = can_project(tmp_path, bus_entry("bus", vcan))

    with live_server(project, config) as server:
        assert server.call("can_session_start", {"bus_id": "bus"})["ok"] is True
        assert ip_link("del", vcan).returncode == 0

        failed = server.call("can_read", {"bus_id": "bus", "wait_timeout_s": 1.0})
        assert failed["ok"] is False, failed
        assert failed["error_type"] == "can_read_failed", failed
        assert failed["side_effect_committed"] is False, failed
        assert failed["retry_safe"] is True, failed
        assert failed["quarantined"] is False, failed
        assert failed["lease_state"] == "active", failed
        # The failed read carries causes about the interface it was reading, on
        # the real kernel as in the fakes (#523).
        read_causes = failed["likely_causes"]
        assert read_causes and not any("COM port" in cause or "debugger" in cause for cause in read_causes), failed
        assert any(("interface" in cause or "link" in cause or "adapter" in cause or "bus" in cause) for cause in read_causes), failed

        stopped = server.call("can_session_stop", {"bus_id": "bus"})
        assert stopped["ok"] is True and stopped["was_active"] is True, stopped
        assert stopped["quarantined"] is False, stopped


# ---------------------------------------------------------------------------
# Who holds the bus (#501).


def test_a_second_session_on_a_channel_this_process_holds_names_this_process(tmp_path: Path, vcan: str) -> None:
    """Two entries on one channel, one server: the second start is refused, and the refusal says by whom.

    The pattern is the one the listen-only refusal itself recommends: a second
    `can_buses` entry for the same channel. The lock is per channel, so the
    second session is refused, and the refusal has to name the holder the way
    the device mutex names one, with `holder_is_this_process` true, because
    the holder is the process that was asked. What #501 observed instead was
    `resource_busy` blaming "another Agentic HIL process" with no holder at
    all.
    """
    project, config = can_project(tmp_path, bus_entry("bus_a", vcan), bus_entry("bus_b", vcan))

    with live_server(project, config) as server:
        first = server.call("can_session_start", {"bus_id": "bus_a"})
        assert first["ok"] is True, first

        refused = server.call("can_session_start", {"bus_id": "bus_b"})
        assert refused["ok"] is False, refused
        assert refused["tool"] == "can_session_start", refused
        assert refused["bus_id"] == "bus_b", refused
        # The device mutex's own answer, chosen so the same channel-busy condition
        # reads the same whether the holder is this process or another: device_busy
        # with a holder, not the anonymous resource_busy the project lock gave.
        assert refused["error_type"] == "device_busy", refused
        assert refused["retry_safe"] is True, refused
        assert refused["side_effect_committed"] is False, refused
        assert "another" not in refused["summary"], refused
        holder = refused.get("holder")
        assert isinstance(holder, dict), refused
        assert holder["pid"] == server.pid, refused
        assert refused["holder_is_this_process"] is True, refused

        # The session that holds the channel is untouched by the refusal.
        listed = server.call("can_buses_list")["buses"]
        assert listed["bus_a"]["session_active"] is True and listed["bus_b"]["session_active"] is False, listed
        assert server.call("can_read", {"bus_id": "bus_a", "wait_timeout_s": 0.0})["ok"] is True


def test_a_second_process_meeting_a_declared_run_is_told_which_process_holds_the_bus(tmp_path: Path, vcan: str) -> None:
    """The neighbour #501 leaves alone: across processes the holder is named already.

    A run declared in one server holds the channel through the device mutex,
    and a session start in a second server meets that mutex, which knows the
    holder: its pid is the first server's, and `holder_is_this_process` is
    absent because it is not.
    """
    project, config = can_project(tmp_path, bus_entry("bus", vcan))

    with live_server(project, config, client="holder") as holder, live_server(project, config, client="contender") as contender:
        run = holder.call("bench_run_start", {"devices": [{"kind": "can", "id": "bus"}], "label": "holding-the-bus"})
        assert run["ok"] is True, run
        try:
            refused = contender.call("can_session_start", {"bus_id": "bus"})
        finally:
            assert holder.call("bench_run_stop")["ok"] is True

    assert refused["ok"] is False, refused
    assert refused["error_type"] == "device_busy", refused
    assert refused["retry_safe"] is True and refused["side_effect_committed"] is False, refused
    assert refused["holder"]["pid"] == holder.pid, refused
    assert refused["holder"]["label"] == "holding-the-bus", refused
    assert "holder_is_this_process" not in refused, refused
    assert str(holder.pid) in refused["summary"], refused


def test_a_second_process_with_no_declared_run_still_meets_the_anonymous_project_lock(tmp_path: Path, vcan: str) -> None:
    """The path #501's fix leaves untouched: cross-process, no run, no holder named.

    Two servers on one configuration with neither declaring a run meet the
    project's own coordination lock first, before any device mutex, and that lock
    cannot name a holder. The answer is `resource_busy` with no `holder` and no
    `holder_is_this_process`, exactly as before the #501 fix: that fix only names
    a holder where the holder is this very process, which this cross-process case
    is not. Pinned so the fix's blast radius is visible and bounded.
    """
    project, config = can_project(tmp_path, bus_entry("bus", vcan))

    with live_server(project, config, client="holder") as holder, live_server(project, config, client="contender") as contender:
        assert holder.call("can_session_start", {"bus_id": "bus"})["ok"] is True
        refused = contender.call("can_session_start", {"bus_id": "bus"})

    assert refused["ok"] is False, refused
    assert refused["error_type"] == "resource_busy", refused
    assert refused["retry_safe"] is True and refused["side_effect_committed"] is False, refused
    assert "holder" not in refused, refused
    assert "holder_is_this_process" not in refused, refused


# ---------------------------------------------------------------------------
# Plans through the real `test-reactor`, against the scripted peer.


def test_a_can_plan_runs_green_against_the_scripted_peer(tmp_path: Path, vcan: str) -> None:
    """Open, send, read-until-match, close, through the command a CI job runs.

    The peer's table (`0x123/01` answered with `0x124/02`) is the test's own
    input; the plan's claim is met by exactly that answer.
    """
    project, config = can_project(tmp_path, bus_entry("bus", vcan))
    plan = write_plan(project, "green", "02")

    with scripted_peer(vcan, tmp_path / "peer-ready", "0x123/01=0x124/02"):
        ran = reactor(project, config, plan, "--json")

    assert ran.returncode == 0, ran.stdout + ran.stderr
    result = json.loads(ran.stdout)
    assert result["ok"] is True, result
    assert result["tool"] == "test_reactor", result
    assert [step["action"] for step in result["steps"]] == ["can_open", "can_send", "can_read", "can_close"], result
    assert all(step["result"]["ok"] is True for step in result["steps"]), result
    answered = result["steps"][2]["result"]
    assert answered["frame"]["id_hex"] == "0x124" and answered["frame"]["data_hex"] == "02", answered
    assert answered["comparator"] == {"id": "0x124", "equals": "02"}, answered
    assert result["cleanup"] == [], result


def test_a_can_plan_whose_claim_is_unmet_is_headed_failed_and_carries_the_frames_the_bus_did_carry(tmp_path: Path, vcan: str) -> None:
    """The red run a plan produces when the peer answers something else (#447).

    Rendered, the first line is `Failed: comparator_unmet`, not `Refused:`,
    because the bus was opened and driven. As a document, the top-level
    `error_type` is the comparator's, and the step's tail shows the `0x124/02`
    frame the bus did carry, which is how a wrong claim and a silent bus read
    differently. Exit 1 either way, and the session the plan opened is closed
    by the run's cleanup.
    """
    project, config = can_project(tmp_path, bus_entry("bus", vcan))
    plan = write_plan(project, "red", "ff")

    with scripted_peer(vcan, tmp_path / "peer-ready", "0x123/01=0x124/02"):
        rendered = reactor(project, config, plan)
        documented = reactor(project, config, plan, "--json")

    assert rendered.returncode == 1, rendered.stdout + rendered.stderr
    assert rendered.stdout.splitlines()[0] == "Failed: comparator_unmet", rendered.stdout

    assert documented.returncode == 1, documented.stdout + documented.stderr
    result = json.loads(documented.stdout)
    assert result["ok"] is False, result
    assert result["error_type"] == "comparator_unmet", result
    assert [step["action"] for step in result["steps"]] == ["can_open", "can_send", "can_read"], result
    read = result["steps"][2]["result"]
    assert read["error_type"] == "comparator_unmet", read
    assert read["comparator"] == {"id": "0x124", "equals": "ff"}, read
    assert {"id_hex": "0x124", "data_hex": "02", "extended": False} in read["frames_tail"], read
    assert result["cleanup_ok"] is True, result
    assert [entry["action"] for entry in result["cleanup"]] == ["can_close"], result


# ---------------------------------------------------------------------------
# Frame shapes, own messages, FD, and a queue that will not drain.


def test_a_second_start_of_the_same_entry_is_already_active_and_drains_the_queue(tmp_path: Path, vcan: str) -> None:
    """The same entry started twice is one session: `already_active`, not a second lease.

    Unlike two entries on one channel (the #501 shape below), starting the same
    entry again is not a lock collision at all: the session exists, so the second
    start reports it as already active and, with the default `clear_rx_queue`,
    drains whatever the far end put on the bus since. A frame sent between the two
    starts is drained by the second and is gone from a following read.
    """
    project, config = can_project(tmp_path, bus_entry("bus", vcan))

    with live_server(project, config) as server, far_end(vcan) as peer:
        assert server.call("can_session_start", {"bus_id": "bus"})["ok"] is True
        send_from_far_end(peer, 0x111, b"\x01")
        time.sleep(0.1)

        again = server.call("can_session_start", {"bus_id": "bus"})
        assert again["ok"] is True, again
        assert again["already_active"] is True, again
        assert again["frames_drained"] == 1, again
        assert again["session"]["session_active"] is True, again

        # The drained frame is not read again: the queue the second start cleared is empty.
        read = server.call("can_read", {"bus_id": "bus", "wait_timeout_s": 0.2})
        assert read["ok"] is True and read["frames_read"] == 0, read


def test_extended_and_remote_frames_cross_the_read_path_with_their_shape_intact(tmp_path: Path, vcan: str) -> None:
    """A 29-bit identifier and a remote frame are read back as what they are.

    The read path reports `extended` and `rtr` per frame, and the comparator's
    frame selection reads exactly those. A standard 0x123 and an extended 0x123
    are different frames, so the flags are part of the frame, not a decoration:
    both are sent from the far end and both come back distinct.
    """
    project, config = can_project(tmp_path, bus_entry("bus", vcan))

    with live_server(project, config) as server, far_end(vcan) as peer:
        assert server.call("can_session_start", {"bus_id": "bus"})["ok"] is True

        send_from_far_end(peer, 0x1ABCDEF, b"\x11\x22", extended=True)
        send_from_far_end(peer, 0x123, b"", rtr=True)
        time.sleep(0.1)

        read = server.call("can_read", {"bus_id": "bus", "wait_timeout_s": 1.0, "max_frames": 2})
        assert read["ok"] is True and read["frames_read"] == 2, read
        by_id = {frame["id"]: frame for frame in read["frames"]}

        extended = by_id[0x1ABCDEF]
        assert extended["extended"] is True and extended["rtr"] is False, extended
        assert extended["id_hex"] == "0x1abcdef" and extended["data_hex"] == "1122", extended

        remote = by_id[0x123]
        assert remote["rtr"] is True and remote["extended"] is False, remote


def test_a_comparator_only_matches_the_frame_type_it_asked_for(tmp_path: Path, vcan: str) -> None:
    """The reactor's `can_read` comparator selects on the identifier and the frame type.

    A plan asking for the extended 0x123 is not met by a standard 0x123 carrying
    the same payload: the two are different frames on the wire. The far end floods
    both while the plan reads until its claim is met, and the run is green only
    because the extended frame arrived and matched.
    """
    project, config = can_project(tmp_path, bus_entry("bus", vcan))
    plan = project / "extended.yaml"
    plan.write_text(
        """version: 3
name: extended
steps:
  - {device: bus, action: can_open}
  - {device: bus, action: can_read, comparator: {id: "0x123", extended: true, equals: "5a"}, timeout_s: 4}
  - {device: bus, action: can_close}
""",
        encoding="utf-8",
    )

    with far_end(vcan) as peer, flooding(peer, [(0x123, b"\x5a", {}), (0x123, b"\x5a", {"extended": True})]):
        ran = reactor(project, config, plan, "--json")

    assert ran.returncode == 0, ran.stdout + ran.stderr
    result = json.loads(ran.stdout)
    assert result["ok"] is True, result
    matched = result["steps"][1]["result"]
    assert matched["frame"]["extended"] is True, matched
    assert matched["frame"]["id_hex"] == "0x123" and matched["frame"]["data_hex"] == "5a", matched


def test_receive_own_messages_reads_back_the_frame_the_same_session_sent(tmp_path: Path, vcan: str) -> None:
    """`receive_own_messages: true` puts a session's own send on its own read path.

    The default is off, and a session does not read its own traffic; on, the
    kernel loops a sent frame back to the sending socket, so the same session
    reads the frame it just sent. Proved on the real socket, since the flag is a
    `CAN_RAW` socket option nothing in the fakes could exercise.
    """
    project, config = can_project(tmp_path, bus_entry("bus", vcan, receive_own_messages=True))

    with live_server(project, config) as server:
        assert server.call("can_session_start", {"bus_id": "bus"})["ok"] is True
        assert server.call("can_send", {"bus_id": "bus", "frame_id": "0x2aa", "data_hex": "c0ffee"})["ok"] is True

        read = server.call("can_read", {"bus_id": "bus", "wait_timeout_s": 1.0})
        assert read["ok"] is True and read["frames_read"] == 1, read
        assert read["frames"][0]["id_hex"] == "0x2aa" and read["frames"][0]["data_hex"] == "c0ffee", read


def test_a_can_fd_frame_crosses_a_vcan_whose_mtu_carries_it(tmp_path: Path, vcan_fd: str) -> None:
    """An FD bus on an mtu-72 vcan sends a payload longer than a classic frame.

    Classic CAN caps a frame at 8 data bytes; CAN FD carries up to 64, and the
    interface has to have the MTU for it. A 16-byte payload the product sends is
    read off the far end whole, which a classic frame could not carry.
    """
    project, config = can_project(tmp_path, bus_entry("bus", vcan_fd, fd=True))
    payload = "00112233445566778899aabbccddeeff"

    with live_server(project, config) as server, far_end(vcan_fd, fd=True) as peer:
        assert server.call("can_session_start", {"bus_id": "bus"})["ok"] is True
        sent = server.call("can_send", {"bus_id": "bus", "frame_id": "0x321", "data_hex": payload})
        assert sent["ok"] is True, sent
        assert a_frame_at_the_far_end(peer) == (0x321, bytes.fromhex(payload))


def test_a_receive_queue_that_never_empties_is_the_clear_limit_and_the_lease_is_released(tmp_path: Path, vcan: str) -> None:
    """A flood the drain cannot outrun stops the start as `can_queue_clear_limit`.

    `max_buffer_frames: 2` and a far end that keeps sending mean the pre-session
    drain reads its bounded batches and the queue is still not empty, so the
    start is refused as the clear limit rather than opening a session onto a bus
    it cannot get ahead of. The socket is closed and the lease released: a refused
    start holds no bench.
    """
    project, config = can_project(tmp_path, bus_entry("bus", vcan, max_buffer_frames=2))

    with far_end(vcan) as peer, flooding(peer, [(0x100, b"\x01\x02", {})], gap_s=0.0), live_server(project, config) as server:
        refused = server.call("can_session_start", {"bus_id": "bus"})
        assert refused["ok"] is False, refused
        assert refused["error_type"] == "can_queue_clear_limit", refused
        assert refused["cleanup_confirmed"] is True, refused
        assert refused["lease_state"] == "released", refused
        assert refused["quarantined"] is False, refused
        # The clear limit is a fact about the traffic on this bus, and says so
        # rather than sending the reader to another transport's log (#523).
        limit_causes = refused["likely_causes"]
        assert limit_causes and not any("COM port" in cause or "debugger" in cause for cause in limit_causes), refused
        assert any(("bus" in cause or "queue" in cause or "frames" in cause or "traffic" in cause) for cause in limit_causes), refused
        assert server.call("can_buses_list")["buses"]["bus"]["session_active"] is False


def test_a_peak_bus_on_a_netdev_channel_opens_through_socketcan_and_frames_cross(tmp_path: Path, vcan: str) -> None:
    """A `peak` bus whose channel names a Linux netdev routes to the socketcan backend.

    PCANBasic cannot open a kernel netdev, so a `peak` bus configured on a `vcan`
    channel opens through socketcan instead, keeps its logical adapter name in the
    result, and carries frames like any socketcan bus. Proved on the real kernel:
    the routing decision is invisible to a fake that never binds a socket.
    """
    project, config = can_project(tmp_path, bus_entry("bus", vcan, adapter="peak"))

    with live_server(project, config) as server, far_end(vcan) as peer:
        started = server.call("can_session_start", {"bus_id": "bus"})
        assert started["ok"] is True, started
        assert started["adapter"] == "peak", started
        assert started["session"]["session_active"] is True, started

        sent = server.call("can_send", {"bus_id": "bus", "frame_id": "0x201", "data_hex": "42"})
        assert sent["ok"] is True, sent
        assert a_frame_at_the_far_end(peer) == (0x201, b"\x42")


# ---------------------------------------------------------------------------
# The CI reader over a real CAN report, and doctor with the extra installed.


def test_run_evidence_reads_the_report_a_green_can_plan_wrote(tmp_path: Path, vcan: str) -> None:
    """`agentic-hil run-evidence` turns a CAN run's report into CI evidence.

    The report a plan writes is fed to the evidence command the same workflow
    runs after a run, and the run summary it emits reports the run as passed and
    names the CAN steps. The command loads no configuration and touches no
    hardware; it reads the report and the workspace only.
    """
    project, config = can_project(tmp_path, bus_entry("bus", vcan))
    plan = write_plan(project, "evidence", "02")

    with scripted_peer(vcan, tmp_path / "peer-ready", "0x123/01=0x124/02"):
        ran = reactor(project, config, plan, "--json")
    assert ran.returncode == 0, ran.stdout + ran.stderr
    report = project / "can-report.json"
    report.write_text(ran.stdout, encoding="utf-8")

    evidence = subprocess.run(
        [sys.executable, "-m", "agentic_hil", "run-evidence", "--report", str(report), "--out", "evidence", "--json"],
        cwd=str(project),
        env={**os.environ, "AGENTIC_HIL_CONFIG": str(config)},
        capture_output=True,
        text=True,
        timeout=COMMAND_TIMEOUT_S,
        check=False,
    )
    assert evidence.returncode == 0, evidence.stdout + evidence.stderr
    result = json.loads(evidence.stdout)
    assert result["ok"] is True, result
    summary = json.loads((project / "evidence" / "run-summary.json").read_text(encoding="utf-8"))
    assert summary["outcome"] == "success", summary
    assert (project / "evidence" / "job-summary.md").is_file()


def test_doctor_with_the_can_extra_installed_names_no_missing_python_can(tmp_path: Path, vcan: str) -> None:
    """`agentic-hil doctor` over a CAN config does not warn that python-can is missing.

    The image installs `agentic-hil[can]`, so the extra a CAN configuration needs
    is present, and doctor's missing-extra warning (the one a bench installed
    without the extra would carry) is absent. Run over the real installation, the
    only place that check reads a true answer.
    """
    project, config = can_project(tmp_path, bus_entry("bus", vcan))

    checked = subprocess.run(
        [sys.executable, "-m", "agentic_hil", "doctor", "--json"],
        cwd=str(project),
        env={**os.environ, "AGENTIC_HIL_CONFIG": str(config)},
        capture_output=True,
        text=True,
        timeout=COMMAND_TIMEOUT_S,
        check=False,
    )
    result = json.loads(checked.stdout)
    assert "python-can" not in json.dumps(result.get("warnings", [])), result
