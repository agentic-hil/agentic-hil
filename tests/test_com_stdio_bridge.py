"""`agentic-hil com-stdio` in the direction nothing had driven: bytes in.

The bridge is bidirectional. Whatever arrives on stdin goes to the configured
port through `write_bytes` under the tool name `com_stdio_write`, and whatever
the port answers goes to stdout. Every test the suite had used a stdin that
blocked, failed or was at EOF, so the write branch had never been taken (#487).

The refusals have one shape. A port that cannot be opened, a write the service
refuses and a stop that fails each print the redacted refusal document on
stderr, one JSON line, leave stdout untouched, and exit 1. stdout is the port's
output and nothing else: a caller piping it into a log must never find a
refusal in it.

And the shutdown must not wait on the operator. When the serial session ends
by itself, a read failure, the bridge stops its stdin reader. On POSIX that
reader polls, so it notices the stop within milliseconds. On Windows it sat in
a bare `os.read`, and closing the descriptor under it waits for that read to
return, so the command hung until somebody pressed Enter or closed the pipe.
Measured on this branch's reference host: `stop_stdin_reader(reader, 0.5)`
returned after 6.00 s, when the pipe's write end was closed, and reported no
error.
"""

from __future__ import annotations

import io
import json
import os
import threading
import time
from contextlib import suppress
from io import StringIO
from pathlib import Path

import pytest
from conftest import write_config
from support import scaled_time_bound

from agentic_hil import comstdio
from agentic_hil.comstdio import run_com_stdio
from agentic_hil.config import load_config
from agentic_hil.redact import redact_sensitive

COM_PORT_YAML = 'com_ports:\n  dut:\n    device: "/dev/ttyAGENTIC_HILTEST"\n'
WAIT_TIMEOUT_S = 5.0
# What the bridge waits after stdin closes before it decides the port has said
# everything: kept short so the write-direction tests end promptly.
EOF_IDLE_S = 0.2
# A shutdown bound that is generous against a poll interval of 10 ms and far
# under the 5 s the pipe stays open in the Windows test.
SHUTDOWN_CEILING_S = 1.5
WINDOWS = os.name == "nt"

# A refusal carrying a secret-named field, which is the one redaction rule that
# applies to every document whatever its shape, so the test can see the document
# went through the rules rather than straight to the stream.
SECRET = "hunter2-9f3a"


def load_com_config(directory: Path):
    return load_config(str(write_config(directory, com_ports_yaml=COM_PORT_YAML)))


class RecordingComPortService:
    """The fake COM service: records every call and answers what it is told to.

    Silence on read by default (the port never answers), success everywhere
    else. A test that wants a refusal sets the class attribute it needs.
    """

    open_refusal: dict | None = None
    write_refusal: dict | None = None
    stop_refusal: dict | None = None
    read_refusal: dict | None = None
    banner: str | None = None

    def __init__(self, config) -> None:
        self.events: list[tuple] = []
        self.banner_sent = False

    def session_start(self, port_id: str, clear_buffer: bool) -> dict:
        self.events.append(("session_start", port_id, clear_buffer))
        return dict(self.open_refusal) if self.open_refusal else {"ok": True, "tool": "com_session_start", "port_id": port_id}

    def write_bytes(self, port_id: str, data: bytes, tool: str) -> dict:
        self.events.append(("write_bytes", port_id, bytes(data), tool))
        return dict(self.write_refusal) if self.write_refusal else {"ok": True, "tool": tool, "bytes_written": len(data)}

    def read_bytes(self, port_id: str, max_bytes: int, wait_timeout_s: float, tool: str) -> dict:
        if self.read_refusal:
            return dict(self.read_refusal)
        if self.banner and not self.banner_sent:
            self.banner_sent = True
            return {"ok": True, "bytes_read": len(self.banner), "data": {"text": self.banner}}
        return {"ok": True, "bytes_read": 0, "data": {"text": ""}}

    def session_stop(self, port_id: str) -> dict:
        self.events.append(("session_stop", port_id))
        return dict(self.stop_refusal) if self.stop_refusal else {"ok": True, "tool": "com_session_stop", "port_id": port_id}

    def close(self) -> None:
        self.events.append(("close",))


@pytest.fixture
def com_service(monkeypatch: pytest.MonkeyPatch) -> type[RecordingComPortService]:
    """A fresh service class per test, with its answers reset and its instances kept."""

    class Service(RecordingComPortService):
        instances: list[RecordingComPortService] = []

        def __init__(self, config) -> None:
            super().__init__(config)
            Service.instances.append(self)

    monkeypatch.setattr("agentic_hil.comstdio.ComPortService", Service)
    return Service


def run_bridge(config, stdin: bytes | io.RawIOBase, **kwargs) -> tuple[int, str, str]:
    """One bridge run with stdin carrying `stdin`; returns the exit code, stdout, stderr."""
    stdout, stderr = StringIO(), StringIO()
    input_stream = io.BytesIO(stdin) if isinstance(stdin, bytes) else stdin
    code = run_com_stdio(config, "dut", input_stream=input_stream, output_stream=stdout, error_stream=stderr, eof_idle_timeout_s=EOF_IDLE_S, **kwargs)
    return code, stdout.getvalue(), stderr.getvalue()


def the_one_document_on(stderr: str) -> dict:
    lines = stderr.splitlines()
    assert len(lines) == 1, f"stderr must carry exactly one refusal line: {stderr!r}"
    return json.loads(lines[0])


# ---------------------------------------------------------------------------
# The write direction.


def test_com_stdio_relays_stdin_bytes_to_the_port_and_exits_zero(tmp_path: Path, com_service) -> None:
    """Bytes piped into the bridge reach `write_bytes` under the bridge's own tool name."""
    config = load_com_config(tmp_path)

    code, stdout, stderr = run_bridge(config, b"AT\r\n")

    service = com_service.instances[0]
    writes = [event for event in service.events if event[0] == "write_bytes"]
    assert writes == [("write_bytes", "dut", b"AT\r\n", "com_stdio_write")], service.events
    assert code == 0
    assert stderr == ""
    assert stdout == ""
    # In session order: the port was open before the bytes went to it, and it
    # was stopped and the service closed after.
    kinds = [event[0] for event in service.events]
    assert kinds == ["session_start", "write_bytes", "session_stop", "close"], kinds


def test_com_stdio_relays_both_directions_in_one_run(tmp_path: Path, com_service) -> None:
    """The read direction is the neighbour: it keeps working while bytes go in."""
    com_service.banner = "ready>"
    config = load_com_config(tmp_path)

    code, stdout, stderr = run_bridge(config, b"version\n")

    assert code == 0
    assert stdout == "ready>"
    assert stderr == ""
    assert ("write_bytes", "dut", b"version\n", "com_stdio_write") in com_service.instances[0].events


def test_com_stdio_writes_every_chunk_stdin_delivers(tmp_path: Path, com_service) -> None:
    """A stream that hands its bytes over in pieces is written in those pieces, in order."""
    config = load_com_config(tmp_path)

    class ChunkedStdin(io.RawIOBase):
        def __init__(self) -> None:
            self.chunks = [b"AT", b"+RST\r\n"]

        def readinto(self, buffer) -> int:
            if not self.chunks:
                return 0
            chunk = self.chunks.pop(0)
            buffer[: len(chunk)] = chunk
            return len(chunk)

    code, _, stderr = run_bridge(config, ChunkedStdin())

    written = [event[2] for event in com_service.instances[0].events if event[0] == "write_bytes"]
    assert b"".join(written) == b"AT+RST\r\n", written
    assert len(written) == 2, written
    assert code == 0
    assert stderr == ""


# ---------------------------------------------------------------------------
# The refusal exits: stderr carries the redacted document, stdout is untouched, exit 1.


def test_a_refused_open_prints_the_redacted_refusal_on_stderr_and_exits_one(tmp_path: Path, com_service) -> None:
    com_service.open_refusal = {
        "ok": False,
        "tool": "com_session_start",
        "port_id": "dut",
        "error_type": "com_port_open_failed",
        "summary": "COM port could not be opened.",
        "backend_error": "could not open port",
        "access_token": SECRET,
    }
    config = load_com_config(tmp_path)

    code, stdout, stderr = run_bridge(config, b"AT\r\n")

    assert code == 1
    assert stdout == ""
    document = the_one_document_on(stderr)
    assert document["error_type"] == "com_port_open_failed"
    assert document == redact_sensitive(com_service.open_refusal)
    assert SECRET not in stderr
    service = com_service.instances[0]
    kinds = [event[0] for event in service.events]
    # Nothing was written to a port that never opened, and nothing was stopped.
    assert "write_bytes" not in kinds, kinds
    assert "session_stop" not in kinds, kinds
    assert kinds[-1] == "close", kinds


def test_a_refused_write_prints_the_redacted_refusal_on_stderr_and_exits_one(tmp_path: Path, com_service) -> None:
    com_service.write_refusal = {
        "ok": False,
        "tool": "com_stdio_write",
        "port_id": "dut",
        "error_type": "serial_write_failed",
        "summary": "COM port write failed.",
        "backend_error": "write timeout",
        "access_token": SECRET,
    }
    config = load_com_config(tmp_path)

    code, stdout, stderr = run_bridge(config, b"AT\r\n")

    assert code == 1
    assert stdout == ""
    document = the_one_document_on(stderr)
    assert document["error_type"] == "serial_write_failed"
    assert document == redact_sensitive(com_service.write_refusal)
    assert SECRET not in stderr
    # The session the write failed in is still stopped and the service closed.
    kinds = [event[0] for event in com_service.instances[0].events]
    assert kinds[-2:] == ["session_stop", "close"], kinds


def test_a_failed_stop_prints_the_redacted_refusal_on_stderr_and_exits_one(tmp_path: Path, com_service) -> None:
    com_service.stop_refusal = {
        "ok": False,
        "tool": "com_session_stop",
        "port_id": "dut",
        "error_type": "com_session_stop_failed",
        "summary": "COM session could not be stopped.",
        "backend_error": "close failed",
        "access_token": SECRET,
    }
    config = load_com_config(tmp_path)

    code, stdout, stderr = run_bridge(config, b"")

    assert code == 1
    assert stdout == ""
    document = the_one_document_on(stderr)
    assert document["error_type"] == "com_session_stop_failed"
    assert document == redact_sensitive(com_service.stop_refusal)
    assert SECRET not in stderr
    assert com_service.instances[0].events[-1] == ("close",)


def test_a_clean_run_writes_nothing_on_stderr(tmp_path: Path, com_service) -> None:
    """The neighbour of every refusal test: silence on stderr when nothing refused."""
    config = load_com_config(tmp_path)

    code, _, stderr = run_bridge(config, b"")

    assert code == 0
    assert stderr == ""


# ---------------------------------------------------------------------------
# The shutdown does not wait on stdin.


class PipeStdin:
    """A real descriptor for the reader to dup, the way `sys.stdin.buffer` is one."""

    def __init__(self, read_fd: int) -> None:
        self.read_fd = read_fd

    def fileno(self) -> int:
        return self.read_fd

    def read(self, size: int) -> bytes:  # pragma: no cover - the reader takes the descriptor path
        return os.read(self.read_fd, size)


@pytest.mark.skipif(not WINDOWS, reason="the Windows reader branch; the POSIX poll is pinned in test_hardening")
def test_stdin_reader_stops_without_external_input_on_windows() -> None:
    """The stop must not wait for the pipe to produce a byte or close.

    Nothing is written to the pipe, and its write end is only closed 5 s later
    by a watchdog: a stop that returns inside 1.5 s returned because the reader
    let go, and one that took 5 s returned because the watchdog freed it. The
    reader thread may still be winding down when the call returns, as long as
    the call says so; what it may not do is block the caller.
    """
    read_fd, write_fd = os.pipe()
    release = threading.Event()

    def close_the_write_end_late() -> None:
        release.wait(5.0)
        with suppress(OSError):
            os.close(write_fd)

    watchdog = threading.Thread(target=close_the_write_end_late, daemon=True)
    watchdog.start()
    reader = comstdio.start_stdin_reader(PipeStdin(read_fd))
    try:
        started = time.monotonic()
        errors = comstdio.stop_stdin_reader(reader, 0.5)
        elapsed = time.monotonic() - started

        assert elapsed < scaled_time_bound(SHUTDOWN_CEILING_S), f"stop_stdin_reader blocked for {elapsed:.2f} s waiting on stdin"
        assert not reader.thread.is_alive() or any("remained blocked" in str(error) for error in errors), errors
    finally:
        release.set()
        watchdog.join(timeout=WAIT_TIMEOUT_S)
        reader.thread.join(timeout=WAIT_TIMEOUT_S)
        with suppress(OSError):
            os.close(read_fd)


@pytest.mark.skipif(not WINDOWS, reason="the Windows reader branch; the POSIX poll is pinned in test_hardening")
def test_stdin_reader_stop_ends_a_read_already_in_flight_on_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    """A reader caught inside the read, not at the poll, is cancelled rather than waited for.

    The poll keeps the reader out of `os.read` while nothing is there, so the
    stop normally finds it between polls. The console can put it inside one
    anyway: a key pressed without Enter signals the handle, the read starts
    and returns only with the line. That read is forced here by a poll that
    always says "ready" on a pipe nobody writes to, and the stop has to end it
    without closing the descriptor under it, which is the wait #487 measured.
    """
    monkeypatch.setattr(comstdio, "windows_stdin_ready", lambda descriptor, timeout_s: True)
    read_fd, write_fd = os.pipe()
    reader = comstdio.start_stdin_reader(PipeStdin(read_fd))
    try:
        # Give the reader time to take its poll's word and enter the read.
        time.sleep(0.2)
        assert reader.thread.is_alive()
        started = time.monotonic()
        errors = comstdio.stop_stdin_reader(reader, 0.5)
        elapsed = time.monotonic() - started

        assert errors == [], errors
        assert not reader.thread.is_alive()
        assert elapsed < scaled_time_bound(SHUTDOWN_CEILING_S), f"stop_stdin_reader waited {elapsed:.2f} s on a read in flight"
        assert reader.owned_fd[0] is None, "the reader did not close its own descriptor on the way out"
    finally:
        with suppress(OSError):
            os.close(write_fd)
        reader.thread.join(timeout=WAIT_TIMEOUT_S)
        with suppress(OSError):
            os.close(read_fd)


def test_com_stdio_ends_a_failed_session_without_waiting_on_stdin(tmp_path: Path, com_service) -> None:
    """The operator-visible rule on both platforms: a session that dies ends the command.

    stdin is a real pipe nobody writes to, which is what a terminal or an idle
    parent process is. The port read fails at once, so the bridge must print
    the refusal and exit 1 within its idle timeout, not when the operator
    presses Enter. Green on POSIX before any change; the Windows reader is what
    #487 measured at 6 s.

    This test is stricter than the reader test above it, and on purpose. That
    one accepts a stop that lets go of the read and names the thread it left
    behind ("remained blocked"); this one requires `run_com_stdio` to return 1,
    and `run_com_stdio` turns every error the stop reports into a
    `RuntimeError("COM stdio cleanup failed: ...")`. So the Windows stop has to
    end the reader, or stop reporting as a cleanup failure a thread it let go
    of on purpose: either way the operator gets exit 1 inside the idle window,
    not a traceback and not a wait.
    """
    com_service.read_refusal = {"ok": False, "tool": "com_stdio_read", "port_id": "dut", "error_type": "serial_read_failed", "summary": "COM port read failed."}
    config = load_com_config(tmp_path)
    read_fd, write_fd = os.pipe()
    release = threading.Event()

    def close_the_write_end_late() -> None:
        release.wait(5.0)
        with suppress(OSError):
            os.close(write_fd)

    watchdog = threading.Thread(target=close_the_write_end_late, daemon=True)
    watchdog.start()
    try:
        started = time.monotonic()
        try:
            code, stdout, stderr = run_bridge(config, PipeStdin(read_fd))
        except RuntimeError as error:
            pytest.fail(f"run_com_stdio raised instead of exiting 1: {error}")
        elapsed = time.monotonic() - started
    finally:
        release.set()
        watchdog.join(timeout=WAIT_TIMEOUT_S)
        with suppress(OSError):
            os.close(read_fd)

    assert code == 1
    assert stdout == ""
    assert the_one_document_on(stderr)["error_type"] == "serial_read_failed"
    assert elapsed < scaled_time_bound(SHUTDOWN_CEILING_S), f"com-stdio waited {elapsed:.2f} s on stdin after its session had failed"
