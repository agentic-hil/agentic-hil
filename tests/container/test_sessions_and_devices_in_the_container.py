"""Sessions and devices against the real transport and the real absence of a library (#508).

The unit tier beside this drives the same behaviours through the fake serial
handle and a poisoned `sys.modules`. What a fake cannot say is what pyserial
puts on a terminal device for a hex stimulus, what the kernel answers a user
who may not open the device, and what a Python environment that never had
python-can installed answers when a CAN tool asks for it. Those are decided
here: the socat pseudo-terminal pair, the container's unprivileged user, and
a `uv tool install` of this checkout without the `[can]` extra.

Recorded 2026-09-06 in the image tools/container/Dockerfile builds (python
3.12-slim, socat from the distribution, uv 0.12.9); nothing here names a host,
a user, an address, a home path or a probe.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from .conftest import (
    ABOVE_EVERY_RELEASE,
    CONTAINER_ONLY,
    LiveServer,
    PtyPair,
    UvTool,
    Wheelhouse,
    fixture_configuration,
    start_responder,
    unprivileged_tree,
    unprivileged_user,
)

pytestmark = [pytest.mark.container, CONTAINER_ONLY]

PORT = "dut"
BUS = "bench"


def a_project(tmp_path: Path, pair: PtyPair) -> tuple[Path, Path]:
    project = tmp_path / "project"
    project.mkdir()
    config = fixture_configuration(project, tmp_path / "config" / "config.yaml", tmp_path / "state", com_port_device=str(pair.dut))
    return project, config


def can_project(tmp_path: Path, can_buses_yaml: str) -> tuple[Path, Path]:
    project = tmp_path / "project"
    project.mkdir(exist_ok=True)
    config = fixture_configuration(project, tmp_path / "config" / "config.yaml", tmp_path / "state", can_buses_yaml=can_buses_yaml)
    return project, config


def peak_bus(channel: str) -> str:
    return f"""can_buses:
  {BUS}:
    adapter: peak
    channel: {channel!r}
    bitrate: 500000
    timeout_s: 2.0
    permissions:
      allow_write: true
"""


# ---------------------------------------------------------------------------
# com_write with a hex payload, on the line.


def test_com_write_hex_puts_exactly_those_bytes_on_the_line(pty_pair: PtyPair, tmp_path: Path) -> None:
    """`hex: "48 65\\n6c"` is the three bytes `Hel` at the peer, and an odd payload puts nothing there."""
    project, config = a_project(tmp_path, pty_pair)
    peer = start_responder(pty_pair, tmp_path)
    try:
        with LiveServer(config, project) as server:
            server.initialize()
            started = server.call("com_session_start", {"port_id": PORT})
            assert started["ok"] is True, started
            written = server.call("com_write", {"port_id": PORT, "hex": "48 65\n6c"})
            assert written["ok"] is True, written
            assert written["bytes_written"] == 3, written
            received = peer.wait_for(b"Hel")
            assert received == b"Hel", received

            refused = server.call("com_write", {"port_id": PORT, "hex": "abc"})
            assert refused["ok"] is False, refused
            assert refused["error_type"] == "invalid_argument", refused
            assert refused["summary"] == "hex must contain valid hexadecimal bytes.", refused
            stopped = server.call("com_session_stop", {"port_id": PORT})
            assert stopped["ok"] is True, stopped
    finally:
        peer.stop()
    assert peer.received() == b"Hel", peer.received()


# ---------------------------------------------------------------------------
# A device this user may not open.


def test_a_serial_device_this_user_may_not_open_names_the_permission_among_its_causes(pty_pair: PtyPair) -> None:
    """EACCES from the kernel on a root-owned slave: the causes name the group to join, not a second holder.

    The arrangement is the one `test_serial_over_pty` uses for the same
    refusal: the server runs as `nobody` under `setpriv` against the slave's
    own `/dev/pts/N`, which is the device's own permission and not a directory
    on the way to it. What is read here is the advice: on a Linux bench the
    first-run failure is a user outside `dialout` or `uucp`, and a reader sent
    hunting for another program holding the port finds none.
    """
    user = unprivileged_user()
    os.chmod(pty_pair.dut_slave, 0o600)
    assert os.stat(pty_pair.dut_slave).st_uid == 0

    tree = unprivileged_tree(user, str(pty_pair.dut_slave))
    try:
        with tree.server() as server:
            server.initialize()
            refused = server.call("com_session_start", {"port_id": PORT})
    finally:
        tree.remove()

    assert refused["ok"] is False, refused
    assert refused["error_type"] == "com_port_open_failed", refused
    assert "[Errno 13]" in refused["backend_error"], refused
    assert refused["side_effect_committed"] is False, refused
    assert refused["retry_safe"] is True, refused
    causes = " ".join(refused["likely_causes"]).lower()
    assert "dialout" in causes or "uucp" in causes or "group" in causes, refused["likely_causes"]
    assert "another program" not in causes, refused["likely_causes"]


# ---------------------------------------------------------------------------
# A CAN tool naming an undeclared bus.


def test_a_can_tool_naming_an_undeclared_bus_is_refused_before_any_socket_is_bound(tmp_path: Path) -> None:
    """`can_bus_not_configured`, from every CAN tool, with the declared buses named and no interface touched."""
    project, config = can_project(tmp_path, peak_bus("can0"))
    with LiveServer(config, project) as server:
        server.initialize()
        for tool, arguments in (
            ("can_session_start", {"bus_id": "ghost"}),
            ("can_read", {"bus_id": "ghost"}),
            ("can_send", {"bus_id": "ghost", "frame_id": 0x123, "data_hex": "01"}),
            ("can_session_stop", {"bus_id": "ghost"}),
        ):
            refused = server.call(tool, arguments)
            assert refused["ok"] is False, (tool, refused)
            assert refused["error_type"] == "can_bus_not_configured", (tool, refused)
            assert refused["summary"] == "CAN bus is not available in the authoritative config.", (tool, refused)
            assert refused["bus_id"] == "ghost", (tool, refused)
            assert refused["configured_buses"] == [BUS], (tool, refused)
            assert refused.get("side_effect_committed") is not True, (tool, refused)
        listed = server.call("can_buses_list")
        assert listed["buses"][BUS]["session_active"] is False, listed


# ---------------------------------------------------------------------------
# The real absence of python-can, and the Linux channel rule with it present.


def test_a_server_installed_without_the_can_extra_answers_can_backend_not_available(tmp_path: Path, uv_tool: UvTool, wheelhouse: Wheelhouse) -> None:
    """A `uv tool install` of this checkout with no `[can]` extra: `can_session_start` refuses by name, not with an ImportError.

    The environment is the real absence: uv resolved this distribution's own
    dependency set and nothing more, so `import can` fails in the installed
    interpreter the way it fails on a bench that skipped the extra. The server
    is that installation's own console script.
    """
    uv_tool.install("--find-links", str(wheelhouse.only(ABOVE_EVERY_RELEASE)), f"agentic-hil>={ABOVE_EVERY_RELEASE}")
    absent = subprocess.run([str(uv_tool.interpreter), "-c", "import can"], capture_output=True, text=True, timeout=60, check=False)
    assert absent.returncode != 0, "python-can is installed in an environment that asked for no CAN extra"
    assert "ModuleNotFoundError" in absent.stderr, absent.stderr

    project, config = can_project(tmp_path, peak_bus("can0"))
    with LiveServer(config, project, command=[str(uv_tool.launcher), "mcp-stdio"], environment=uv_tool.environment()) as server:
        server.initialize()
        refused = server.call("can_session_start", {"bus_id": BUS})

    assert refused["ok"] is False, refused
    assert refused["error_type"] == "can_backend_not_available", refused
    assert refused["summary"] == "python-can is not installed. Install agentic-hil[can] to use direct CAN adapters.", refused
    assert refused["side_effect_committed"] is False, refused
    assert refused["adapter"] == "peak", refused
    assert refused["bus_id"] == BUS, refused


def test_a_peak_bus_whose_channel_is_neither_a_pcan_handle_nor_a_netdev_is_refused_naming_the_field(tmp_path: Path) -> None:
    """With python-can installed, `usb1` on a Linux host is `config_invalid` at `can_buses.<id>.channel`.

    The rule reads the channel's shape and nothing else: a kernel netdev name
    opens through SocketCAN, a PCANBasic handle opens through `libpcanbasic`,
    and a channel of neither shape is refused before python-can is asked.
    """
    assert sys.platform.startswith("linux")
    import can  # noqa: F401  (the image installs the extra; the rule is read with it present)

    project, config = can_project(tmp_path, peak_bus("usb1"))
    with LiveServer(config, project) as server:
        server.initialize()
        refused = server.call("can_session_start", {"bus_id": BUS})

    assert refused["ok"] is False, json.dumps(refused)
    assert refused["error_type"] == "config_invalid", refused
    assert refused["field"] == f"can_buses.{BUS}.channel", refused
    assert refused["summary"] == "PEAK adapter on Linux expects a SocketCAN-style interface name such as can0.", refused
    assert refused["side_effect_committed"] is False, refused
    assert refused["retry_safe"] is True, refused


def test_a_peak_bus_naming_a_pcan_handle_on_linux_passes_the_channel_rule_and_meets_the_library(tmp_path: Path) -> None:
    """The neighbour: `PCAN_USBBUS1` is admitted on Linux and reaches python-can, which names the missing PCAN-Basic API.

    Recorded 2026-09-06 in the image: python-can 4.6.1 with no `libpcanbasic`
    installed answers `pcanbasic library not found.` from the `pcan` interface,
    and the product classifies it as `can_adapter_library_missing`, retry-safe
    and with nothing opened. The channel rule never fired: the refusal names
    the library, not the field.
    """
    assert sys.platform.startswith("linux")
    import can  # noqa: F401

    project, config = can_project(tmp_path, peak_bus("PCAN_USBBUS1"))
    with LiveServer(config, project) as server:
        server.initialize()
        refused = server.call("can_session_start", {"bus_id": BUS})

    assert refused["ok"] is False, json.dumps(refused)
    assert refused["error_type"] == "can_adapter_library_missing", refused
    assert "field" not in refused, refused
    assert refused["channel"] == "PCAN_USBBUS1", refused
    assert refused["missing_library"] == "PCAN-Basic API", refused
    assert refused["backend_error"] == "pcanbasic library not found.", refused
    assert refused["side_effect_committed"] is False, refused
    assert refused["retry_safe"] is True, refused
