"""The COM tools, `com-stdio` and the UART plan steps over a real terminal device.

Every COM test under tests/ runs against a fake serial handle, so pyserial's
own behaviour on POSIX is never exercised: the exclusive `flock` that makes a
second opener busy, the termios the open applies, the read that returns nothing
while the far end is quiet, the errno an absent or an unreadable device answers
with. And the report a UART-only plan writes has never been fed to
`run-evidence` in a test, which is how an empty `Elapsed` column (#455), a
doubled digest prefix (#466) and a red run headed `Refused:` (#447) reached a
bench before anything here went red.

The device is a pseudo-terminal pair `socat` makes inside the container. The
configuration names one end as its COM port, written by the test as an
operator writes one (the link path, a baudrate, `identity_source: device`, no
USB identity); the other end is held by `tests/container/pty_responder.py`, a
scripted peer whose answers are the test's own input, listed on the command
line that starts it. The peer is a byte peer for the transport and nothing
else: it runs no firmware, models nothing electrical, and no test here claims
anything about a target.

Everything is driven the way an agent or an operator drives it: `tools/call`
over a live `agentic-hil mcp-stdio`, and the console script through a pipe.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from .conftest import (
    CONTAINER_ONLY,
    LiveServer,
    PtyPair,
    fixture_configuration,
    json_document,
    run_cli,
    start_responder,
)

pytestmark = [pytest.mark.container, CONTAINER_ONLY]

PORT = "dut"
WRITE_PERMISSION = "com_ports.dut.permissions.allow_write"
CONFIGURED_BAUDRATE = "115200"
# What a pseudo-terminal slave reads under `stty` before anybody has opened it:
# the kernel's default termios. The open is what moves it to the configured
# rate, so the two readings are the proof that the product opened this device
# and applied its configuration to it.
UNOPENED_BAUDRATE = "38400"

# The peer's answer table, spelled the way the responder reads it. Escapes are
# Python's, so `\\r\\n` here is the two bytes on the wire.
PING_PONG = "PING=PONG\\r\\n"
VERSION_LINE = "VERSION=v1.2.3\\r\\n"

READ_TIMEOUT_S = 10.0


def a_project(tmp_path: Path, pair: PtyPair) -> tuple[Path, Path, Path]:
    """A workspace, its configuration naming the pair's near end, and the state root."""
    project = tmp_path / "project"
    project.mkdir()
    state = tmp_path / "state"
    config = fixture_configuration(project, tmp_path / "config" / "config.yaml", state, com_port_device=str(pair.dut))
    return project, config, state


def stty_speed(device: Path) -> str:
    read = subprocess.run(["stty", "-F", str(device), "speed"], capture_output=True, text=True, timeout=30, check=False)
    assert read.returncode == 0, read.stderr
    return read.stdout.strip()


def read_until(server: LiveServer, expected: bytes, timeout_s: float = READ_TIMEOUT_S) -> tuple[bytes, list[dict]]:
    """`com_read` until ``expected`` has arrived, the way a caller polls a line.

    An answer can arrive in pieces, and every read is the product's own
    `com_read` with whatever is left of the deadline as its wait.
    """
    received = b""
    reads: list[dict] = []
    deadline = time.monotonic() + timeout_s
    while expected not in received:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        result = server.call("com_read", {"port_id": PORT, "wait_timeout_s": round(remaining, 3)})
        reads.append(result)
        assert result["ok"] is True, result
        received += bytes.fromhex(result["data"]["hex"])
    return received, reads


def event_logs(project: Path) -> list[Path]:
    return sorted((project / ".agentic-hil" / "logs").glob(f"com-*-{PORT}.jsonl"))


# ---------------------------------------------------------------------------
# The open, on the kernel's own evidence.


def test_the_open_applies_the_configured_baudrate_and_the_stop_releases_the_exclusive_hold(pty_pair: PtyPair, tmp_path: Path) -> None:
    """The two facts nothing but the device can give.

    Before the session the slave reads the kernel's default rate; after
    `com_session_start` it reads the configured one, which is pyserial having
    opened this device and applied the entry to it. After `com_session_stop`
    a second exclusive open succeeds, which is the flock having been released:
    socat's log and the process table say nothing about either.
    """
    import serial

    project, config, _state = a_project(tmp_path, pty_pair)
    assert stty_speed(pty_pair.dut) == UNOPENED_BAUDRATE

    with LiveServer(config, project) as server:
        server.initialize()
        started = server.call("com_session_start", {"port_id": PORT})
        assert started["ok"] is True, started
        assert stty_speed(pty_pair.dut) == CONFIGURED_BAUDRATE

        stopped = server.call("com_session_stop", {"port_id": PORT})
        assert stopped["ok"] is True, stopped
        assert stopped["was_active"] is True, stopped
        assert stopped["session"]["session_active"] is False, stopped

        second_holder = serial.Serial(str(pty_pair.dut), 115200, exclusive=True)
        second_holder.close()


# ---------------------------------------------------------------------------
# A session, end to end, with the peer answering.


def test_a_session_writes_a_stimulus_the_peer_receives_and_reads_its_answer(pty_pair: PtyPair, tmp_path: Path) -> None:
    """`com_session_start`, `com_write`, `com_read`, `com_session_stop`, as documents.

    The stimulus is asserted at both ends: what `com_write` says it wrote and
    what the peer's record says arrived on the wire. The answer is the peer's
    table entry for `PING`, read back through `com_read`.
    """
    project, config, _state = a_project(tmp_path, pty_pair)
    responder = start_responder(pty_pair, tmp_path, PING_PONG)
    try:
        with LiveServer(config, project) as server:
            server.initialize()
            started = server.call("com_session_start", {"port_id": PORT})
            assert started["ok"] is True, started
            assert started["already_active"] is False, started
            assert started["session"]["session_active"] is True, started
            assert started["identity"]["status"] == "not_declared", started["identity"]
            assert started["summary"] == "COM port session started.", started

            again = server.call("com_session_start", {"port_id": PORT})
            assert again["ok"] is True and again["already_active"] is True, again

            written = server.call("com_write", {"port_id": PORT, "text": "PING\r\n"})
            assert written["ok"] is True, written
            assert written["bytes_written"] == 6, written
            assert written["data"] == {"hex": b"PING\r\n".hex(), "text": "PING\r\n", "encoding": "utf-8"}, written
            assert responder.wait_for(b"PING\r\n") == b"PING\r\n"

            received, reads = read_until(server, b"PONG\r\n")
            assert received == b"PONG\r\n", (received, reads)
            for result in reads:
                assert result["data"]["encoding"] == "utf-8", result
                assert result["summary"] == "Feedback read from COM port.", result
            assert reads[-1]["buffer_remaining_bytes"] == 0, reads[-1]
            assert reads[-1]["overflow_bytes"] == 0, reads[-1]

            stopped = server.call("com_session_stop", {"port_id": PORT})
            assert stopped["ok"] is True and stopped["was_active"] is True, stopped

            after = server.call("com_read", {"port_id": PORT})
            assert after["ok"] is False, after
            assert after["error_type"] == "session_not_active", after
    finally:
        responder.stop()


def test_a_read_against_a_silent_peer_waits_its_timeout_and_returns_no_bytes(pty_pair: PtyPair, tmp_path: Path) -> None:
    """The peer records and never answers, so the read has nothing to return.

    The wait is measured from outside: a `com_read` with `wait_timeout_s` on a
    quiet line takes at least that long and comes back `ok` with no bytes,
    which is the quiet "no feedback" and not a failure. The stimulus did reach
    the wire, so the silence is the peer's and not a write that never went.
    """
    project, config, _state = a_project(tmp_path, pty_pair)
    responder = start_responder(pty_pair, tmp_path)
    try:
        with LiveServer(config, project) as server:
            server.initialize()
            assert server.call("com_session_start", {"port_id": PORT})["ok"] is True
            assert server.call("com_write", {"port_id": PORT, "text": "PING\r\n"})["ok"] is True
            assert responder.wait_for(b"PING\r\n") == b"PING\r\n"

            before = time.monotonic()
            read = server.call("com_read", {"port_id": PORT, "wait_timeout_s": 1.5})
            waited = time.monotonic() - before

            assert read["ok"] is True, read
            assert read["bytes_read"] == 0, read
            assert read["data"] == {"hex": "", "text": "", "encoding": "utf-8"}, read
            assert read["summary"] == "No COM port feedback was available.", read
            assert waited >= 1.5, f"the read came back after {waited:.2f}s on a wait of 1.5s"
            assert "reader_error" not in read, read
    finally:
        responder.stop()


# ---------------------------------------------------------------------------
# The write permission, moved by the operator's commands.


def test_the_write_is_refused_after_revoke_and_accepted_again_after_grant(pty_pair: PtyPair, tmp_path: Path) -> None:
    """`agentic-hil revoke`, a server that meets the closed key, `agentic-hil grant`, a server that does not.

    A server parses permissions once at startup, and both commands say so,
    so each half is read off a server started after the write. The refusal
    names the key and the grant line (#443), and nothing reaches the wire
    while it stands.
    """
    project, config, _state = a_project(tmp_path, pty_pair)
    responder = start_responder(pty_pair, tmp_path, PING_PONG)
    try:
        revoked = run_cli(project, config, "revoke", "com_ports.dut.allow_write", "--json")
        assert revoked.returncode == 0, revoked.stderr
        revocation = json_document(revoked)
        assert revocation["ok"] is True, revocation
        assert revocation["changed"] == [{"key": WRITE_PERMISSION, "previous_value": True, "value": False}], revocation
        assert revocation["restart_required"] is True, revocation

        with LiveServer(config, project) as server:
            server.initialize()
            assert server.call("com_session_start", {"port_id": PORT})["ok"] is True
            refused = server.call("com_write", {"port_id": PORT, "text": "PING\r\n"})
            assert refused["ok"] is False, refused
            assert refused["error_type"] == "permission_denied", refused
            assert refused["permission"] == WRITE_PERMISSION, refused
            assert f"The permission is `{WRITE_PERMISSION}` and it is false." in refused["summary"], refused
            assert f"agentic-hil grant {WRITE_PERMISSION}" in refused["next_step"], refused
            # Refused before the line: the peer saw nothing.
            time.sleep(0.2)
            assert responder.received() == b"", responder.received()

        granted = run_cli(project, config, "grant", "com_ports.dut.allow_write", "--json")
        assert granted.returncode == 0, granted.stderr
        grant = json_document(granted)
        assert grant["ok"] is True, grant
        assert grant["changed"] == [{"key": WRITE_PERMISSION, "previous_value": False, "value": True}], grant

        with LiveServer(config, project) as server:
            server.initialize()
            assert server.call("com_session_start", {"port_id": PORT})["ok"] is True
            written = server.call("com_write", {"port_id": PORT, "text": "PING\r\n"})
            assert written["ok"] is True, written
            assert responder.wait_for(b"PING\r\n") == b"PING\r\n"
            received, _reads = read_until(server, b"PONG\r\n")
            assert received == b"PONG\r\n"
    finally:
        responder.stop()


# ---------------------------------------------------------------------------
# What the listings say about a port with no USB identity.


def test_com_ports_list_reports_the_entry_as_the_configuration_names_it_and_the_inventory_does_not_hold_the_pty(pty_pair: PtyPair, tmp_path: Path) -> None:
    """The configured entry, its identity fields, and the host inventory beside it.

    A pseudo-terminal publishes no USB identity, so the entry is identified by
    its device name alone and says so: `identity_source: device`, no serial
    number, no vendor or product id, and the warning that a kernel name is an
    enumeration order rather than hardware. pyserial's inventory enumerates
    the host's serial ports and never a pseudo-terminal, so the same document
    lists the entry under `ports` and not under `available_com_ports`, and so
    does `agentic-hil com-ports`.
    """
    project, config, _state = a_project(tmp_path, pty_pair)
    names_the_pty = {str(pty_pair.dut), str(pty_pair.dut_slave)}

    with LiveServer(config, project) as server:
        server.initialize()
        listed = server.call("com_ports_list")
        assert listed["ok"] is True, listed
        entry = listed["ports"][PORT]
        assert entry["device"] == str(pty_pair.dut), entry
        assert entry["baudrate"] == 115200, entry
        assert entry["encoding"] == "utf-8", entry
        assert entry["session_active"] is False, entry
        assert entry["identity_source"] == "device", entry
        for absent in ("serial_number", "vid", "pid", "resource_id"):
            assert absent not in entry, entry
        assert "serial_number" in entry["identity_warning"], entry
        inventory = listed["available_com_ports"]
        assert inventory["ok"] is True, inventory
        for host_port in inventory["ports"]:
            assert host_port.get("device") not in names_the_pty, host_port
            assert host_port.get("stable_device") not in names_the_pty, host_port

        assert server.call("com_session_start", {"port_id": PORT})["ok"] is True
        during = server.call("com_ports_list")["ports"][PORT]
        assert during["session_active"] is True, during
        assert during["log_path"], during

    machine = run_cli(project, None, "com-ports", "--json")
    assert machine.returncode == 0, machine.stderr
    document = json_document(machine)
    assert document["ok"] is True, document
    assert document["summary"] == f"{len(document['ports'])} available COM port(s).", document
    for host_port in document["ports"]:
        assert host_port.get("device") not in names_the_pty, host_port

    rendered = run_cli(project, None, "com-ports")
    assert rendered.returncode == 0, rendered.stderr
    assert f"{len(document['ports'])} available COM port(s)" in rendered.stdout.decode("utf-8"), rendered.stdout
    assert str(pty_pair.dut_slave) not in rendered.stdout.decode("utf-8"), rendered.stdout


# ---------------------------------------------------------------------------
# The stdio bridge over the pair.


def test_com_stdio_relays_stdin_to_the_port_and_the_peers_answer_to_stdout(pty_pair: PtyPair, tmp_path: Path) -> None:
    """A line in on stdin reaches the peer; the peer's answer comes out on stdout, and nothing on stderr.

    tests/container/test_com_stdio_on_a_pty.py proves the same two directions
    over a pair this process allocates with `os.openpty()`, holding the master
    itself and naming the slave's `/dev/pts/N` as the port. What this adds is
    the arrangement an operator has: the configured device is a link path to
    a slave a separate program owns, the other end is a second process and
    not a descriptor in the test, and what reached the wire is read off that
    process's record rather than off a master the test holds.
    """
    project, config, _state = a_project(tmp_path, pty_pair)
    responder = start_responder(pty_pair, tmp_path, PING_PONG)
    try:
        bridged = run_cli(project, config, "com-stdio", "--port", PORT, "--eof-idle-timeout-s", "3", stdin=b"PING\r\n")
    finally:
        received = responder.stop()

    assert bridged.returncode == 0, bridged.stderr
    assert bridged.stderr == b"", bridged.stderr
    assert received == b"PING\r\n", received
    assert b"PONG\r\n" in bridged.stdout, bridged.stdout


# ---------------------------------------------------------------------------
# The refusals: busy, absent, unreadable.


def test_a_second_session_on_the_same_configuration_is_refused_while_the_first_holds_the_port(pty_pair: PtyPair, tmp_path: Path) -> None:
    """Two servers on one configuration: the second `com_session_start` is refused and the first keeps the line.

    The two meet on the project's own lock before either reaches the device,
    so the refusal is the coordinator's `resource_busy`; the first session is
    untouched by it, and the peer saw only the first session's bytes.
    """
    project, config, _state = a_project(tmp_path, pty_pair)
    responder = start_responder(pty_pair, tmp_path, PING_PONG)
    try:
        with LiveServer(config, project) as first, LiveServer(config, project) as second:
            first.initialize()
            second.initialize()
            assert first.call("com_session_start", {"port_id": PORT})["ok"] is True

            refused = second.call("com_session_start", {"port_id": PORT})
            assert refused["ok"] is False, refused
            assert refused["error_type"] == "resource_busy", refused
            assert refused.get("side_effect_committed") is not True, refused

            assert first.call("com_write", {"port_id": PORT, "text": "PING\r\n"})["ok"] is True
            received, _reads = read_until(first, b"PONG\r\n")
            assert received == b"PONG\r\n"
            assert first.call("com_session_stop", {"port_id": PORT})["was_active"] is True
    finally:
        received = responder.stop()
    assert received == b"PING\r\n", received


def test_a_port_another_program_holds_exclusively_is_refused_as_busy_and_opens_once_it_is_released(pty_pair: PtyPair, tmp_path: Path) -> None:
    """A foreign exclusive holder, met at the open by pyserial's flock.

    The holder is this test, holding the device the way a serial monitor
    would. The refusal names the device and says no handle was created;
    once the holder lets go, the same call succeeds.
    """
    import serial

    project, config, _state = a_project(tmp_path, pty_pair)
    holder = serial.Serial(str(pty_pair.dut), 115200, exclusive=True)
    try:
        with LiveServer(config, project) as server:
            server.initialize()
            refused = server.call("com_session_start", {"port_id": PORT})
            assert refused["ok"] is False, refused
            assert refused["error_type"] == "com_port_busy", refused
            assert refused["configured_device"] == str(pty_pair.dut), refused
            assert refused["side_effect_committed"] is False, refused
            assert refused["retry_safe"] is True, refused
            assert refused["cleanup_confirmed"] is True, refused
            assert "held by another program" in refused["summary"], refused

            holder.close()
            started = server.call("com_session_start", {"port_id": PORT})
            assert started["ok"] is True, started
    finally:
        if holder.is_open:
            holder.close()


def test_a_device_whose_link_leads_nowhere_is_refused_as_open_failed(pty_pair: PtyPair, tmp_path: Path) -> None:
    """socat is gone, the slaves with it, and the configured link dangles.

    The open fails in the operating system with no handle behind it, which is
    `com_port_open_failed` and not busy: nobody holds a device that is not
    there.
    """
    project, config, _state = a_project(tmp_path, pty_pair)
    pty_pair.stop()
    deadline = time.monotonic() + 10
    while pty_pair.dut.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert os.path.lexists(pty_pair.dut) and not pty_pair.dut.exists(), "the link should dangle once socat is gone"

    with LiveServer(config, project) as server:
        server.initialize()
        refused = server.call("com_session_start", {"port_id": PORT})

    assert refused["ok"] is False, refused
    assert refused["error_type"] == "com_port_open_failed", refused
    assert "[Errno 2]" in refused["backend_error"], refused
    assert refused["side_effect_committed"] is False, refused
    assert refused["retry_safe"] is True, refused
    assert refused["cleanup_confirmed"] is True, refused


def test_a_device_this_user_cannot_read_is_refused_as_open_failed_and_not_as_busy(pty_pair: PtyPair, tmp_path: Path) -> None:
    """EACCES on a root-owned slave, met by a server running as an unprivileged user.

    Root opens everything, so the server is started under `setpriv` as the
    `nobody` user with a tree that user owns. On a serial device a permission
    error is far more often a missing group membership than a second holder,
    and the product answers it as an open failure rather than sending the
    reader after a process that does not exist.

    The configured device is the slave's own `/dev/pts/N` and not the link
    under this test's temporary directory: pytest creates that tree with mode
    0o700, so a link there is refused in the path lookup by a user who cannot
    traverse it, and the slave's mode would then decide nothing. `/dev/pts`
    is traversable by everyone, so the EACCES the server meets is the
    device's own, which is the case this test exists for.
    """
    if os.geteuid() != 0:
        pytest.skip("needs root: the device has to be one the server's user cannot open, and only root can arrange that here")
    setpriv = shutil.which("setpriv")
    if setpriv is None:
        pytest.skip("setpriv is not on PATH, and it is what drops the server to an unprivileged user")
    import pwd

    try:
        unprivileged = pwd.getpwnam("nobody")
    except KeyError:
        pytest.skip("this image has no `nobody` user to drop the server to")

    os.chmod(pty_pair.dut_slave, 0o600)
    assert os.stat(pty_pair.dut_slave).st_uid == 0

    # Directly under /tmp and not under this test's own temporary directory:
    # that one sits inside the root-owned pytest tree, which the unprivileged
    # user cannot traverse, and a configuration it cannot read is refused
    # long before the port is reached.
    tree = Path(tempfile.mkdtemp(prefix="agentic-hil-unprivileged-", dir="/tmp"))
    try:
        project = tree / "project"
        project.mkdir()
        home = tree / "home"
        home.mkdir()
        temporary = tree / "tmp"
        temporary.mkdir()
        config = fixture_configuration(project, tree / "config" / "config.yaml", tree / "state", com_port_device=str(pty_pair.dut_slave))
        for path in [tree, *tree.rglob("*")]:
            os.chown(path, unprivileged.pw_uid, unprivileged.pw_gid)

        environment = {
            **os.environ,
            "HOME": str(home),
            "XDG_CONFIG_HOME": str(home / ".config"),
            "XDG_CACHE_HOME": str(home / ".cache"),
            "XDG_DATA_HOME": str(home / ".local" / "share"),
            "XDG_STATE_HOME": str(home / ".local" / "state"),
            "TMPDIR": str(temporary),
            "TEMP": str(temporary),
            "TMP": str(temporary),
            "AGENTIC_HIL_CONFIG": str(config),
        }
        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "container-tier", "version": "1"}}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "com_session_start", "arguments": {"port_id": PORT}}},
        ]
        server = subprocess.run(
            [setpriv, f"--reuid={unprivileged.pw_uid}", f"--regid={unprivileged.pw_gid}", "--clear-groups", sys.executable, "-m", "agentic_hil", "mcp-stdio"],
            cwd=str(project),
            env=environment,
            input="".join(json.dumps(request) + "\n" for request in requests),
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    finally:
        shutil.rmtree(tree, ignore_errors=True)

    answers = {json.loads(line)["id"]: json.loads(line) for line in server.stdout.splitlines() if line.strip()}
    assert 2 in answers, f"no answer to com_session_start (exit {server.returncode}):\n{server.stdout}\n{server.stderr}"
    refused = answers[2]["result"]["structuredContent"]
    assert refused["ok"] is False, refused
    assert refused["error_type"] == "com_port_open_failed", refused
    assert "[Errno 13]" in refused["backend_error"], refused
    # pyserial names the device it could not open, so the errno is the slave's.
    assert str(pty_pair.dut_slave) in refused["backend_error"], refused
    assert refused["side_effect_committed"] is False, refused
    assert refused["retry_safe"] is True, refused


# ---------------------------------------------------------------------------
# A plan through the real test-reactor, and the evidence over its report.

GREEN_PLAN = """version: 3
name: pty-round-trip
steps:
  - {port_id: dut, action: uart_open}
  - {port_id: dut, action: uart_write, text: "PING\\r\\n"}
  - {port_id: dut, action: uart_expect, text: "PONG", timeout_s: 5}
  - {port_id: dut, action: uart_write, text: "VERSION\\r\\n"}
  - {port_id: dut, action: uart_read, comparator: {pattern: "^v(\\\\d+)\\\\.2\\\\.3", range: {min: 1, max: 9}}, timeout_s: 5}
  - {port_id: dut, action: uart_read}
  - {port_id: dut, action: uart_close}
"""
GREEN_ACTIONS = ["uart_open", "uart_write", "uart_expect", "uart_write", "uart_read", "uart_read", "uart_close"]

# The claim the peer does not meet: its table answers `VERSION` with v1.2.3.
FAILING_PLAN = """version: 3
name: pty-wrong-version
steps:
  - {port_id: dut, action: uart_open}
  - {port_id: dut, action: uart_write, text: "VERSION\\r\\n"}
  - {port_id: dut, action: uart_read, comparator: {equals: "v9.9.9"}, timeout_s: 2}
"""

STEP_ROW = re.compile(r"^\| (\d+) \| dut \| (uart_\w+) \| (pass|fail) \| (\d+) \|$")


def last_report(project: Path) -> dict:
    path = project / ".agentic-hil" / "reports" / "last-report.json"
    assert path.is_file(), f"no report at {path}"
    return json.loads(path.read_text(encoding="utf-8"))


def test_a_uart_plan_runs_green_against_the_peer_and_run_evidence_reads_its_report(pty_pair: PtyPair, tmp_path: Path) -> None:
    """`uart_open`, `uart_write`, `uart_expect`, `uart_read` with and without a claim, `uart_close`, then `run-evidence`.

    The report is the one the run wrote, and the evidence is read off the
    files the command wrote out of it: every step row carries its elapsed time
    (#455), the configuration digest is spelled with its prefix exactly once
    (#466), and the summary names the plan's one device.
    """
    project, config, _state = a_project(tmp_path, pty_pair)
    (project / "testconfig.yaml").write_text(GREEN_PLAN, encoding="utf-8")
    responder = start_responder(pty_pair, tmp_path, PING_PONG, VERSION_LINE)
    try:
        ran = run_cli(project, config, "test-reactor", "--test-config", "testconfig.yaml", "--json")
    finally:
        received = responder.stop()

    assert ran.returncode == 0, ran.stdout.decode("utf-8", errors="replace") + ran.stderr.decode("utf-8", errors="replace")
    result = json_document(ran)
    assert result["ok"] is True, result
    assert received == b"PING\r\nVERSION\r\n", received
    assert [step["action"] for step in result["steps"]] == GREEN_ACTIONS, result["steps"]
    for step in result["steps"]:
        assert step["result"]["ok"] is True, step
        assert isinstance(step["elapsed_ms"], int) and step["elapsed_ms"] >= 0, step
    expect = result["steps"][2]["result"]
    assert expect["expected_text"] == "PONG" and expect["bytes_received"] >= len(b"PONG\r\n"), expect
    claim = result["steps"][4]["result"]
    assert claim["ok"] is True and claim["bytes_received"] >= len(b"v1.2.3\r\n"), claim

    report = last_report(project)
    assert report["ok"] is True and report["name"] == "pty-round-trip", report
    assert [step["action"] for step in report["steps"]] == GREEN_ACTIONS

    evidence = run_cli(project, None, "run-evidence", "--report", ".agentic-hil/reports/last-report.json", "--out", "evidence", "--json")
    assert evidence.returncode == 0, evidence.stdout.decode("utf-8", errors="replace") + evidence.stderr.decode("utf-8", errors="replace")
    assert json_document(evidence)["ok"] is True

    out = project / "evidence"
    summary = json.loads((out / "run-summary.json").read_text(encoding="utf-8"))
    assert summary["outcome"] == "success", summary
    assert summary["plan"]["name"] == "pty-round-trip", summary
    assert summary["bench"]["devices"]["com_ports"] == [PORT], summary
    assert summary["bench"]["config_digest"].startswith("sha256:"), summary

    document = (out / "job-summary.md").read_text(encoding="utf-8")
    assert "sha256sha256" not in document
    digest_rows = [line for line in document.splitlines() if line.startswith("| Configuration digest |")]
    assert len(digest_rows) == 1, document
    assert digest_rows[0].count("sha256:") == 1, digest_rows[0]
    assert f"| `{summary['bench']['config_digest']}` |" in digest_rows[0], digest_rows[0]
    rows = [STEP_ROW.match(line) for line in document.splitlines()]
    rows = [match for match in rows if match is not None]
    assert [match.group(2) for match in rows] == GREEN_ACTIONS, document
    assert all(match.group(3) == "pass" for match in rows), document
    assert "|  |" not in document, document

    copied_logs = list(out.rglob(f"com-*-{PORT}.jsonl"))
    assert len(copied_logs) == 1, sorted(out.rglob("*"))
    assert copied_logs[0].read_bytes() == event_logs(project)[-1].read_bytes()


def test_a_plan_whose_claim_the_peer_does_not_meet_is_headed_failed(pty_pair: PtyPair, tmp_path: Path) -> None:
    """The red run a bench exists to produce, headed by its outcome (#447).

    The port answered, the answer was not the claimed one, and the rendering's
    first line says `Failed:` and the comparator's own error type, never the
    word reserved for a call that never happened. The tail of what the port
    did say is in the rendering, so the red is readable without the bench.
    """
    project, config, _state = a_project(tmp_path, pty_pair)
    (project / "testconfig.yaml").write_text(FAILING_PLAN, encoding="utf-8")
    responder = start_responder(pty_pair, tmp_path, VERSION_LINE)
    try:
        ran = run_cli(project, config, "test-reactor", "--test-config", "testconfig.yaml")
    finally:
        received = responder.stop()

    rendered = ran.stdout.decode("utf-8")
    assert ran.returncode == 1, rendered + ran.stderr.decode("utf-8", errors="replace")
    assert received == b"VERSION\r\n", received
    first_line = rendered.splitlines()[0] if rendered else ""
    assert first_line.startswith("Failed: comparator_unmet"), rendered
    assert not rendered.startswith("Refused"), rendered
    assert "v1.2.3" in rendered, rendered

    report = last_report(project)
    assert report["ok"] is False, report
    assert [step["action"] for step in report["steps"]] == ["uart_open", "uart_write", "uart_read"], report["steps"]
    failed = report["steps"][2]["result"]
    assert failed["error_type"] == "comparator_unmet", failed
    assert "v1.2.3" in json.dumps(failed), failed


# ---------------------------------------------------------------------------
# The event log the session writes.


def test_the_session_writes_an_event_log_in_the_order_things_happened(pty_pair: PtyPair, tmp_path: Path) -> None:
    """One file per session under the workspace logs: start, tx, rx, stop, each stamped.

    The stimulus entry is ahead of the answer it provoked, which is the order
    a reader follows, and the file `com_session_start` named as `log_path` is
    this file. The agent-initiated lines (start, tx, stop) are mirrored into
    the trusted ledger under the state root.
    """
    project, config, state = a_project(tmp_path, pty_pair)
    responder = start_responder(pty_pair, tmp_path, PING_PONG)
    try:
        with LiveServer(config, project) as server:
            server.initialize()
            started = server.call("com_session_start", {"port_id": PORT})
            assert started["ok"] is True, started
            named = started["session"]["log_path"]
            assert server.call("com_write", {"port_id": PORT, "text": "PING\r\n"})["ok"] is True
            received, _reads = read_until(server, b"PONG\r\n")
            assert received == b"PONG\r\n"
            assert server.call("com_session_stop", {"port_id": PORT})["ok"] is True
    finally:
        responder.stop()

    logs = event_logs(project)
    assert len(logs) == 1, logs
    log = logs[0]
    assert Path(named if os.path.isabs(named) else project / named).resolve() == log.resolve(), (named, log)

    entries = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert all("time" in entry for entry in entries), entries
    assert entries[0]["event"] == "start", entries[0]
    assert entries[0]["port_id"] == PORT and entries[0]["device"] == str(pty_pair.dut), entries[0]
    assert entries[-1] == {**entries[-1], "event": "stop", "reason": "requested"}, entries[-1]
    kinds = [entry.get("event") or entry.get("direction") for entry in entries]
    assert kinds[0] == "start" and kinds[-1] == "stop", kinds
    assert "tx" in kinds and "rx" in kinds, kinds
    assert kinds.index("tx") < kinds.index("rx"), kinds
    sent = [entry for entry in entries if entry.get("direction") == "tx"]
    assert [entry["hex"] for entry in sent] == [b"PING\r\n".hex()], sent
    assert sent[0]["text"] == "PING\r\n" and sent[0]["bytes"] == 6, sent[0]
    answered = b"".join(bytes.fromhex(entry["hex"]) for entry in entries if entry.get("direction") == "rx")
    assert answered == b"PONG\r\n", answered

    mirrored = list(state.rglob(f"audit-logs/com-*-{PORT}.jsonl"))
    assert len(mirrored) == 1, sorted(state.rglob("*"))
    ledger = [json.loads(line) for line in mirrored[0].read_text(encoding="utf-8").splitlines() if line.strip()]
    ledger_kinds = [entry.get("event") or entry.get("direction") for entry in ledger]
    assert ledger_kinds[0] == "start" and "tx" in ledger_kinds and ledger_kinds[-1] == "stop", ledger_kinds
