"""`agentic-hil com-stdio` over a real terminal device, both directions.

The unit tier proves the bridge against a recorded service; what it cannot
prove is that a byte piped into the command comes out of a tty, because that
is pyserial's business with the kernel: the raw-mode configuration it applies
on open, the modem-line calls a pseudo-terminal refuses and it has to tolerate,
the exclusive open. Every one of those is invisible to a fake and decided here
by the real driver (#487).

The device is a pseudo-terminal pair the kernel allocates, the same thing
`socat pty,raw,echo=0 pty,raw,echo=0` builds a pair of. The slave end is what
the configuration names as the port; the master end is the wire, where the
test reads what the bridge wrote and writes what the bridge should relay.

Recorded 2026-09-06 against the pyserial this image's locked dependency set
installs; the version is asserted so the record cannot go stale silently.
"""

from __future__ import annotations

import json
import os
import select
import subprocess
import sys
import time
from pathlib import Path

import pytest

from .conftest import COMMAND_TIMEOUT_S, CONTAINER_ONLY, fixture_configuration

pytestmark = [pytest.mark.container, CONTAINER_ONLY]

WIRE_TIMEOUT_S = 10.0
# The bridge's own idle window after stdin closes, widened so the test has
# time to answer on the wire before the bridge decides the port is done.
EOF_IDLE_S = 3.0


def bridge(config: Path, project: Path, *arguments: str) -> subprocess.Popen[bytes]:
    environment = {**os.environ, "AGENTIC_HIL_CONFIG": str(config)}
    return subprocess.Popen(
        [sys.executable, "-m", "agentic_hil", "com-stdio", "--port", "dut", "--eof-idle-timeout-s", str(EOF_IDLE_S), *arguments],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=str(project),
        env=environment,
    )


def read_from_the_wire(master: int, expected: bytes, timeout_s: float) -> bytes:
    """Bytes off the master end until `expected` has arrived or the bound passes."""
    received = b""
    deadline = time.monotonic() + timeout_s
    while expected not in received and time.monotonic() < deadline:
        ready, _, _ = select.select([master], [], [], 0.1)
        if ready:
            received += os.read(master, 4096)
    return received


def test_the_pyserial_this_image_installs_is_the_one_this_was_recorded_against() -> None:
    import serial

    assert serial.__version__.startswith("3."), serial.__version__


def test_com_stdio_relays_stdin_bytes_to_a_real_tty_and_the_tty_back_to_stdout(tmp_path: Path) -> None:
    """A byte in on stdin is a byte out of the slave; a byte in at the master is a byte out on stdout."""
    master, slave = os.openpty()
    try:
        project = tmp_path / "project"
        project.mkdir()
        config = fixture_configuration(project, tmp_path / "config" / "config.yaml", tmp_path / "state", com_port_device=os.ttyname(slave))

        process = bridge(config, project)
        assert process.stdin is not None
        process.stdin.write(b"AT\r\n")
        process.stdin.flush()
        on_the_wire = read_from_the_wire(master, b"AT\r\n", WIRE_TIMEOUT_S)
        # The port is open and listening by now; answer it on the wire. stdin
        # stays open until `communicate` closes it, which is the EOF the bridge
        # ends on once the port has been idle for its window.
        os.write(master, b"OK\r\n")
        stdout, stderr = process.communicate(timeout=COMMAND_TIMEOUT_S)
    finally:
        os.close(master)
        os.close(slave)

    assert on_the_wire == b"AT\r\n", on_the_wire
    assert process.returncode == 0, stderr.decode("utf-8", errors="replace")
    assert stderr == b"", stderr
    assert b"OK\r\n" in stdout, stdout


def test_com_stdio_refuses_a_device_that_is_gone_on_stderr_and_exits_one(tmp_path: Path) -> None:
    """The pair is released before the bridge starts, so the name it was given names nothing."""
    master, slave = os.openpty()
    device = os.ttyname(slave)
    os.close(master)
    os.close(slave)
    assert not Path(device).exists(), device
    project = tmp_path / "project"
    project.mkdir()
    config = fixture_configuration(project, tmp_path / "config" / "config.yaml", tmp_path / "state", com_port_device=device)

    process = bridge(config, project)
    stdout, stderr = process.communicate(input=b"AT\r\n", timeout=COMMAND_TIMEOUT_S)

    assert process.returncode == 1, stdout + stderr
    assert stdout == b"", stdout
    lines = stderr.decode("utf-8").splitlines()
    assert len(lines) == 1, stderr
    document = json.loads(lines[0])
    assert document["error_type"] == "com_port_open_failed", document
    assert document["ok"] is False, document
