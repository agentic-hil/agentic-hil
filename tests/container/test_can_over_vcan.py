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
import shutil
import subprocess
import sys
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


def a_fresh_interface_name() -> str:
    """One vcan name per test, unique on this host.

    `can:socketcan:<channel>` is a machine-wide lock, so two tests on one name
    would meet each other as `device_busy`; the pid keeps two runs apart and the
    counter keeps two tests apart. Fifteen characters is the kernel's limit on
    an interface name.
    """
    return f"vcan{os.getpid() % 1000:03d}{next(_interface_numbers):02d}"


def ip_command() -> str:
    """The `ip` the product itself would read a link with, or whatever is on PATH."""
    installed = next((path for path in IP_COMMAND_PATHS if Path(path).is_file()), None)
    if installed is None:
        installed = shutil.which("ip")
    assert installed is not None, "iproute2 is not in this image"
    return installed


def ip_link(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([ip_command(), "link", *arguments], capture_output=True, text=True, timeout=30, check=False)


@contextmanager
def virtual_interface(*, up: bool = True) -> Iterator[str]:
    """A vcan netdev for one test, deleted afterwards.

    The one condition that is a skip rather than a failure: the interface could
    not be made, which is the host kernel's module or the container's
    capability. The reason is named so the job that reads this tier's report
    can print it.
    """
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


def bus_entry(bus_id: str, channel: str, *, allow_write: bool = True, listen_only: bool = False) -> str:
    """One `can_buses` entry on a SocketCAN channel, at configuration version 3.

    Reading needs no grant at this version; `allow_write` is the one permission
    the entry carries. `timeout_s` bounds every read's wait and every send.
    """
    return f"""  {bus_id}:
    adapter: socketcan
    channel: {channel!r}
    bitrate: 500000
    timeout_s: {BUS_TIMEOUT_S}
    max_buffer_frames: 8
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
def far_end(channel: str) -> Iterator[object]:
    """A second CAN_RAW socket on the interface, held by this test.

    A frame the product sends is what this receives, and a frame this sends is
    what the product reads. It is python-can's own socket and nothing of this
    project's.
    """
    import can

    bus = can.Bus(interface="socketcan", channel=channel)
    try:
        yield bus
    finally:
        bus.shutdown()


def send_from_far_end(bus: object, frame_id: int, data: bytes) -> None:
    import can

    bus.send(can.Message(arbitration_id=frame_id, data=data, is_extended_id=False), timeout=WIRE_TIMEOUT_S)  # type: ignore[attr-defined]


def a_frame_at_the_far_end(bus: object, timeout_s: float = WIRE_TIMEOUT_S) -> tuple[int, bytes] | None:
    message = bus.recv(timeout=timeout_s)  # type: ignore[attr-defined]
    if message is None:
        return None
    return int(message.arbitration_id), bytes(message.data)


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
            time.sleep(0.5)
            sent = server.call("can_send", {"bus_id": "bus", "frame_id": "0x7ab", "data_hex": "deadbeef"})
            assert sent["ok"] is True, sent
            stdout, stderr = observer.communicate(timeout=WIRE_TIMEOUT_S)
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
        assert 1.0 <= waited < 1.0 + WAIT_SLACK_S, f"a read asked to wait 1.0s waited {waited:.2f}s"

        began = time.monotonic()
        capped = server.call("can_read", {"bus_id": "bus", "wait_timeout_s": 30.0})
        waited = time.monotonic() - began
        assert capped["ok"] is True and capped["frames_read"] == 0, capped
        assert BUS_TIMEOUT_S <= waited < BUS_TIMEOUT_S + WAIT_SLACK_S, f"a read asked to wait 30s on a bus with timeout_s {BUS_TIMEOUT_S} waited {waited:.2f}s"


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
        assert "session" not in sent.get("error_type", ""), sent
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


def test_a_session_on_an_interface_that_is_down_is_refused_before_contact(tmp_path: Path, vcan_down: str) -> None:
    """An interface that exists and is down carries nothing, so a session on it is a refusal.

    The kernel lets a CAN_RAW socket bind a down interface and then answers
    every send with ENETDOWN and a receive with either nothing or the same. So
    a session opened on one is a session over a link that cannot carry a frame:
    with the default `clear_rx_queue` the drain happens to fail on the receive
    and the start is refused as a queue that could not be cleared, and with
    `clear_rx_queue: false` it reports "CAN bus session started." and the first
    send is answered as an unknown bus effect. Neither says what is wrong.
    Refused instead, both ways, before any socket exists, as a channel that is
    not there is refused: the channel named, no contact, no incident, and the
    operator told which `ip link` line brings it up.
    """
    project, config = can_project(tmp_path, bus_entry("bus", vcan_down))

    with live_server(project, config) as server:
        for arguments in ({"bus_id": "bus"}, {"bus_id": "bus", "clear_rx_queue": False}):
            refused = server.call("can_session_start", arguments)
            assert refused["ok"] is False, refused
            assert refused["error_type"] == "can_interface_down", refused
            assert refused["channel"] == vcan_down, refused
            assert refused["field"] == "can_buses.bus.channel", refused
            assert refused["target_contacted"] is False, refused
            assert refused["side_effect_committed"] is False, refused
            assert refused["retry_safe"] is True, refused
            assert refused["quarantined"] is False and refused["lease_state"] == "released", refused
            assert f"ip link set up {vcan_down}" in " ".join(refused["remediation"]), refused
            assert server.call("can_buses_list")["buses"]["bus"]["session_active"] is False

        # Brought up, the same entry opens.
        assert ip_link("set", "up", vcan_down).returncode == 0
        started = server.call("can_session_start", {"bus_id": "bus"})
        assert started["ok"] is True, started


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
