r"""One serial port is one device lock, whatever the configuration calls it.

A COM port entry that names no hardware is locked machine-wide under the
spelling of its device name (`UartDevice.lock_key`), and one port answers to
more than one spelling:

* On POSIX, a udev link and the node it leads to. The stable-name rule steers a
  configuration to `/dev/serial/by-id/...`, a symlink to a kernel name such as
  `/dev/ttyACM0` that another configuration may still name directly.
* On Windows, `COM59` and its device namespace spelling `\\.\COM59`, which
  pyserial hands to CreateFile as one and the same name.

The device locks live in one directory per user and machine
(`bench.device_lock_root`), so every workspace of that user meets every other
one there, and two entries of one configuration meet there too: nothing at load
refuses two entries that name one port. Two spellings met there as two locks. A
second owner through the other spelling then reached a port somebody else held,
stopped at best by the port's own exclusive open, which names nobody, and not
stopped at all while the holder's run had the port closed between its steps.
test_devices.py pins that a board reachable under two entries of one spelling
is still one board; the tests here ask the same of two spellings.

Every workspace here has a state_root of its own, so that a second owner meets
the device lock rather than the per-configuration lock kept under the state
root. That is the layer where a second owner through the *same* spelling is
refused `device_busy`, naming its holder, and the same-spelling cases pin that
the harness reaches it.

A run declares what its configuration names, and the node a link leads to is
the host's to give: it can appear after the run began, when the board is plugged
in late. The last two tests ask that such a run's own step is neither refused
over that name nor let past somebody holding the port under it.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
import yaml
from conftest import write_config
from test_contact_marker import FakePort, install_fake_serial

from agentic_hil.bench import BenchMutex, DeviceBusyError
from agentic_hil.config import load_config
from agentic_hil.devices import UartDevice, uart_device
from agentic_hil.tools import AgenticHILToolService

PORT_ID = "one_port_uart"

# The POSIX pair is the recording's rather than typed: the kernel name a stock
# Ubuntu 24.04 host gave the on-board debugger's virtual COM port, and the by-id
# link udev published for it. What makes the two one port is the filesystem, so
# the pair is rebuilt as a real symlink under tmp_path, absolute the way
# test_serial_port_identity.py builds its links: the recording says where the
# link leads, not how udev spells the target, and a symlink resolves either way.
RECORDING_PATH = Path(__file__).resolve().parent / "fixtures" / "com_ports_ubuntu_24_04_recording.json"

# pyserial 3.5 opens both Windows spellings through one Win32 name. The port
# setter keeps the configured string as `name` (serial/serialutil.py:272-274),
# and `open` puts `\\.\` in front of a `COM` number above 8 and hands every
# other string to CreateFile unchanged (serial/serialwin32.py:47-55). `COM59`
# and `\\.\COM59` therefore reach CreateFile as the same name.
COM59_DEVICE_NAMESPACE = r"\\.\COM59"

POSIX_ONLY = pytest.mark.skipif(os.name == "nt", reason="a symlink and the node it leads to are two names a POSIX filesystem gives one device")
WINDOWS_ONLY = pytest.mark.skipif(os.name != "nt", reason="the device namespace spelling of a COM port is a Win32 name")

TWO_SPELLINGS = [
    pytest.param("by-id link", "kernel name", marks=POSIX_ONLY, id="by-id-link-then-kernel-name"),
    pytest.param("kernel name", "by-id link", marks=POSIX_ONLY, id="kernel-name-then-by-id-link"),
    pytest.param("COM59", "device namespace", marks=WINDOWS_ONLY, id="COM59-then-device-namespace"),
    pytest.param("device namespace", "COM59", marks=WINDOWS_ONLY, id="device-namespace-then-COM59"),
]

# The control: a second owner through the same spelling is refused today, so a
# harness that never reached the device lock would fail these as well.
ONE_SPELLING = [
    pytest.param("by-id link", "by-id link", marks=POSIX_ONLY, id="posix-same-spelling"),
    pytest.param("COM59", "COM59", marks=WINDOWS_ONLY, id="windows-same-spelling"),
]


def recorded_port(tmp_path: Path) -> tuple[Path, Path]:
    """Where the recorded kernel node and its by-id link sit, before either exists."""
    ports = json.loads(RECORDING_PATH.read_text(encoding="utf-8"))["recording"]["ports"]
    (recorded,) = [port for port in ports if port.get("stable_device")]
    return tmp_path / "host" / recorded["device"].lstrip("/"), tmp_path / "host" / recorded["stable_device"].lstrip("/")


def publish(name: Path, target: Path | None = None) -> None:
    """One name appearing as the board arrives: the node itself, or a link to it."""
    name.parent.mkdir(parents=True, exist_ok=True)
    if target is None:
        # A plain file stands in for the character device: the lock never opens
        # it, and a session's open goes to the fake serial backend.
        name.write_text("", encoding="utf-8")
    else:
        name.symlink_to(target)


@pytest.fixture
def spellings(tmp_path: Path) -> dict[str, str]:
    """Every name the one port under test answers to on this host."""
    if os.name == "nt":
        return {"COM59": "COM59", "device namespace": COM59_DEVICE_NAMESPACE}
    node, link = recorded_port(tmp_path)
    publish(node)
    publish(link, node)
    return {"kernel name": str(node), "by-id link": str(link)}


def workspace_config(tmp_path: Path, name: str, devices: dict[str, str]):
    """A workspace of its own, with a state_root of its own."""
    com_ports = yaml.safe_dump({"com_ports": {port_id: {"device": device} for port_id, device in devices.items()}})
    return load_config(str(write_config(tmp_path / f"workspace-{name}", state_root=tmp_path / f"state-{name}", com_ports_yaml=com_ports)))


def held_and_asked(tmp_path: Path, layout: str, held: str, asked: str) -> tuple[UartDevice, UartDevice]:
    if layout == "one configuration":
        config = workspace_config(tmp_path, "a", {"first_uart": held, "second_uart": asked})
        return uart_device(config, "first_uart"), uart_device(config, "second_uart")
    return uart_device(workspace_config(tmp_path, "a", {PORT_ID: held}), PORT_ID), uart_device(workspace_config(tmp_path, "b", {PORT_ID: asked}), PORT_ID)


@pytest.mark.parametrize("layout", ["two workspaces", "one configuration"])
@pytest.mark.parametrize(("held_as", "asked_as"), TWO_SPELLINGS + ONE_SPELLING)
def test_a_port_held_under_one_spelling_is_busy_under_the_other(spellings: dict[str, str], tmp_path: Path, layout: str, held_as: str, asked_as: str) -> None:
    """The lock itself, below every tool: one port, one holder, and the holder named.

    Two workspaces of one user are the common shape. Two entries of one
    configuration are the same question asked inside one file, and two owners
    of that file each take the entry they were told to."""
    held, asked = held_and_asked(tmp_path, layout, spellings[held_as], spellings[asked_as])
    holder = BenchMutex(frontend="mcp", label="holding-run")
    contender = BenchMutex(frontend="cli")
    try:
        assert held.acquire(holder) is True

        with pytest.raises(DeviceBusyError) as busy:
            asked.acquire(contender)

        assert busy.value.result["holder"]["owner_id"] == holder.owner.owner_id, busy.value.result
    finally:
        contender.release_all()
        holder.release_all()


@POSIX_ONLY
@pytest.mark.parametrize(
    ("first_as", "second_as"),
    [
        pytest.param("by-id link", "kernel name", id="by-id-link-then-kernel-name"),
        pytest.param("kernel name", "by-id link", id="kernel-name-then-by-id-link"),
        pytest.param("by-id link", "by-id link", id="same-spelling"),
    ],
)
def test_a_second_session_through_another_spelling_is_refused_naming_the_first(spellings: dict[str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch, first_as: str, second_as: str) -> None:
    """Refused by the lock that knows the holder, not by the open that does not.

    The first session's exclusive open does keep the second out on POSIX:
    pyserial takes a non-blocking flock on the descriptor inside `open()`
    (serial/serialposix.py:381-389), and the node is one whichever name reached
    it. That refusal is `com_port_busy`, which says another program holds the
    port and names none, while the program is a session on this machine whose
    holder record says who it is. A second session through the same spelling is
    told exactly that, and so is one through the other spelling."""
    port = FakePort()
    install_fake_serial(monkeypatch, port)
    first = AgenticHILToolService(workspace_config(tmp_path, "a", {PORT_ID: spellings[first_as]}))
    second = AgenticHILToolService(workspace_config(tmp_path, "b", {PORT_ID: spellings[second_as]}))
    try:
        started = first.call("com_session_start", {"port_id": PORT_ID})
        assert started["ok"] is True, started
        # The first open now holds the one node, as pyserial's flock does.
        port.held = True

        refused = second.call("com_session_start", {"port_id": PORT_ID})

        assert refused.get("error_type") == "device_busy", refused
        assert refused["holder"]["owner_id"] == first.coordinator.bench.owner.owner_id, refused
        # Refused before the port: the second session never reached the open.
        assert port.opens == 1
        # The port keeps the name its configuration gave it.
        assert started["identity"]["device"] == spellings[first_as], started
    finally:
        second.close()
        first.close()


@pytest.mark.parametrize(("held_as", "asked_as"), TWO_SPELLINGS + ONE_SPELLING)
def test_a_run_holding_the_port_keeps_out_a_session_through_another_spelling(spellings: dict[str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch, held_as: str, asked_as: str) -> None:
    """The case no exclusive open can cover.

    A run holds its devices from `bench_run_start` to `bench_run_stop`, and
    between the steps that talk to the port (while it flashes the board, say)
    the port is closed. The device lock is then all that keeps a session from
    another workspace off the board, and a lock keyed by spelling does not."""
    port = FakePort()
    install_fake_serial(monkeypatch, port)
    running = AgenticHILToolService(workspace_config(tmp_path, "a", {PORT_ID: spellings[held_as]}))
    stranger = AgenticHILToolService(workspace_config(tmp_path, "b", {PORT_ID: spellings[asked_as]}))
    try:
        run = running.call("bench_run_start", {"devices": [{"kind": "uart", "id": PORT_ID}], "label": "boot-smoke"})
        assert run["ok"] is True, run

        refused = stranger.call("com_session_start", {"port_id": PORT_ID})

        assert refused.get("error_type") == "device_busy", refused
        assert refused["holder"]["label"] == "boot-smoke", refused
        assert port.opens == 0
        # Ending the run gives back every name it held for the port.
        assert running.call("bench_run_stop")["ok"] is True
        reached = stranger.call("com_session_start", {"port_id": PORT_ID})
        assert reached.get("error_type") is None, reached
    finally:
        stranger.close()
        running.close()


@POSIX_ONLY
def test_a_run_declared_before_its_link_appeared_reaches_the_port_once_it_has(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A run's own step is not refused over a name the host gave its port later.

    The run is declared while the board is unplugged, so neither the kernel node
    nor the by-id link its configuration names exists yet. Then the board
    arrives. The run declared what its configuration names, and the node the
    link now leads to is not a device it failed to declare, so its session
    reaches the port. Once the session and the run are over, a second workspace
    through the kernel name gets the port: nothing stayed held under either
    name."""
    port = FakePort()
    install_fake_serial(monkeypatch, port)
    node, link = recorded_port(tmp_path)
    running = AgenticHILToolService(workspace_config(tmp_path, "a", {PORT_ID: str(link)}))
    other = AgenticHILToolService(workspace_config(tmp_path, "b", {PORT_ID: str(node)}))
    try:
        run = running.call("bench_run_start", {"devices": [{"kind": "uart", "id": PORT_ID}], "label": "boot-smoke"})
        assert run["ok"] is True, run
        publish(node)
        publish(link, node)

        started = running.call("com_session_start", {"port_id": PORT_ID})

        assert started.get("error_type") is None, started
        assert port.opens == 1
        assert running.call("com_session_stop", {"port_id": PORT_ID})["ok"] is True
        assert running.call("bench_run_stop")["ok"] is True
        reached = other.call("com_session_start", {"port_id": PORT_ID})
        assert reached.get("error_type") is None, reached
        assert port.opens == 2
    finally:
        other.close()
        running.close()


@POSIX_ONLY
def test_a_run_declared_before_its_link_appeared_is_kept_off_a_port_held_under_the_kernel_name(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The same run, when the port it reaches is somebody else's by then.

    Declared while the board is unplugged, as above. The board arrives, and a
    second workspace naming the kernel node opens a session on it before the
    link appears. The run's session through the link is refused `device_busy`,
    naming that workspace, and never reaches the open. The refusal keeps
    nothing either: once the run ends, its owner holds no device."""
    port = FakePort()
    install_fake_serial(monkeypatch, port)
    node, link = recorded_port(tmp_path)
    running = AgenticHILToolService(workspace_config(tmp_path, "a", {PORT_ID: str(link)}))
    holder = AgenticHILToolService(workspace_config(tmp_path, "b", {PORT_ID: str(node)}))
    try:
        run = running.call("bench_run_start", {"devices": [{"kind": "uart", "id": PORT_ID}], "label": "boot-smoke"})
        assert run["ok"] is True, run
        publish(node)
        held = holder.call("com_session_start", {"port_id": PORT_ID})
        assert held["ok"] is True, held
        # The holder's open now has the node, as pyserial's flock does.
        port.held = True
        publish(link, node)

        refused = running.call("com_session_start", {"port_id": PORT_ID})

        assert refused.get("error_type") == "device_busy", refused
        assert refused["holder"]["owner_id"] == holder.coordinator.bench.owner.owner_id, refused
        assert port.opens == 1
        # The refused call counted the run's own hold once more before it met
        # the busy node; that count goes back with the refusal.
        assert running.call("bench_run_stop")["ok"] is True
        assert running.call("bench_run_status")["held_devices"] == []
    finally:
        holder.close()
        running.close()
