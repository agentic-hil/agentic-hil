"""A debug start takes OpenOCD's GDB port as ready from OpenOCD's own line.

OpenOCD logs `Listening on port <port> for gdb connections` once its GDB port
listens (openocd/src/server/server.c:286 and :297 in v0.12.0). A start that
decides the port is ready by connecting to it and closing the socket at once
hands OpenOCD a connection that never speaks: OpenOCD accepts it once its
startup commands are done, sends it the `+` it sends GDB, clears the target's
breakpoints and watchpoints for it, fails to read the acknowledgement GDB would
have sent, and closes it with `Error: attempted 'gdb' connection rejected`
(gdb_server.c:1012 to :1030, server.c:93 and :94). Every session log on the
bench carried that line ahead of GDB's own accept.

The fake OpenOCD prints the recorded session, answers each connection the way
OpenOCD does, and records every connection it accepted with the bytes it
received; the fake GDB opens the connection `-target-select` names. What a
server saw is read back as a timeline: when its port listened, when it printed
its listening line, and each connection by what it sent. GDB's sends `+`, the
acknowledgement OpenOCD waits for; a readiness check's sends nothing.

A server that exits before the line is pinned where the other startup refusals
are, by `test_a_server_that_dies_at_startup_is_classified_from_its_output` in
tests/test_debug_backend_refusals.py, which plays recorded OpenOCD refusals and
asserts today's classification and today's prompt answer.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from conftest import FAKE_GDB, FAKE_OPENOCD, write_config
from fixtures.fake_gdb import CONNECT_DROPPED_MESSAGE, CONNECT_DROPPED_ONCE, CONNECTS_TO_SERVER
from fixtures.fake_openocd import (
    DECOY_LINES_VARIABLE,
    FLOOD_LINES_VARIABLE,
    LINE_AFTER_VARIABLE,
    LISTEN_AFTER_VARIABLE,
    NEVER,
    RECORD_VARIABLE,
    RECORDED_ACCEPTING_LINE,
    RECORDED_LISTENING_LINE,
    RECORDED_REJECTED_LINE,
    VERSION_SWALLOWED_VARIABLE,
)
from support import scaled_time_bound

from agentic_hil.backends import gdbdebug
from agentic_hil.backends.gdbdebug import GdbDebugSessions
from agentic_hil.config import load_config
from agentic_hil.tools import AgenticHILToolService

# A start's deadline is also the time the fake OpenOCD has to come up and hold
# back what a test tells it to, so both deadlines below take the suite's time
# scale, and so does the configured budget that caps them.
START_TIMEOUT_S = 10.0
# How long the fake holds back the step a test delays. A start that acts on
# anything the fake did before that step has long connected by the time it comes.
HELD_BACK_S = "1.0"
# The deadline of a start that must never see its line. The fake has printed
# every other line long before it runs out, so a start that took one of them for
# the line would already have connected.
LINELESS_START_TIMEOUT_S = 2.0
# A tail far narrower than the product's own, and a flood of recorded lines
# written in the same write as the listening line and many times the tail's size.
# The line is gone from the tail within that one read, long before any wait that
# searched the tail could look. With the product's own cap the flood would have
# to outgrow 64 KiB, and reading it takes the product long enough that such a
# wait could still catch the line in passing.
NARROW_TAIL_CHARS = 1024
FLOOD_LINES = 64
# A `probe_id` for the one case that needs the probe selection to run at all:
# without one nothing is selected and no release is read. The value only has to
# reach the server's command line, which is why it is a plausible ST-Link serial
# and nothing more.
A_PROBE_SERIAL = "066BFF505050505050505050"

PORT_LISTENING = "port listening"
LINE_PRINTED = "listening line printed"
CHECK_CONNECTED = "connection sent ''"
GDB_CONNECTED = "connection sent '+'"
# What the recorded session should have been: the port listens, OpenOCD says so,
# and the one connection it accepts is GDB's.
ONE_SESSION = [PORT_LISTENING, LINE_PRINTED, GDB_CONNECTED]
TIMEOUT_FAILURE = {
    "ok": False,
    "error_type": "timeout",
    "backend_error_type": "gdb_server_not_ready",
    "cleanup_confirmed": True,
    "side_effect_status": "not_started",
    "retry_safe": True,
}
# The line a start on OpenOCD waits for, which its timeout summary names: the
# recorded listening line without the level OpenOCD puts in front of it.
AWAITED_LINE = RECORDED_LISTENING_LINE.removeprefix("Info : ")


def drive_fake_openocd(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, listen_after: str = "0", line_after: str = "0", decoys: bool = False, flood: int = 0) -> Path:
    """Tell the fake OpenOCD when to listen and when to say so; returns its record."""
    record = tmp_path / "gdb-port-record.jsonl"
    monkeypatch.setenv(RECORD_VARIABLE, str(record))
    monkeypatch.setenv(LISTEN_AFTER_VARIABLE, listen_after)
    monkeypatch.setenv(LINE_AFTER_VARIABLE, line_after)
    monkeypatch.setenv(DECOY_LINES_VARIABLE, "1" if decoys else "0")
    monkeypatch.setenv(FLOOD_LINES_VARIABLE, str(flood))
    return record


def image_bytes(fake_gdb_behavior: str) -> bytes:
    return b"\x7fELF" + b"\x00" * 12 + f"\nFAKE_GDB_BEHAVIOR={fake_gdb_behavior}\n".encode()


def write_image(tmp_path: Path, fake_gdb_behavior: str) -> Path:
    image = tmp_path / "build" / "app.elf"
    image.parent.mkdir(parents=True, exist_ok=True)
    image.write_bytes(image_bytes(fake_gdb_behavior))
    return image


def debug_service(tmp_path: Path, fake_gdb_behavior: str = CONNECTS_TO_SERVER, probe_id: str | None = None) -> AgenticHILToolService:
    config_path = write_config(tmp_path, gdb_executable=FAKE_GDB, timeout_s=scaled_time_bound(START_TIMEOUT_S), probe_id=probe_id)
    write_image(tmp_path, fake_gdb_behavior)
    return AgenticHILToolService(load_config(str(config_path)))


def start_attach(service: AgenticHILToolService, timeout_s: float = START_TIMEOUT_S) -> dict:
    return service.call("debug_start_session", {"image_path": "build/app.elf", "mode": "attach", "timeout_s": scaled_time_bound(timeout_s)})


def close(service: AgenticHILToolService) -> None:
    """Close the service, and end its device hold even when the close refuses."""
    try:
        service.close()
    except BaseException:
        service.coordinator.bench.release_all()
        raise


def served_port(record: Path) -> int:
    """The GDB port the fake OpenOCD was told to serve, which is the port the start reserved."""
    ports = {json.loads(line)["port"] for line in record.read_text(encoding="utf-8").splitlines()}
    assert len(ports) == 1, ports
    return ports.pop()


def timelines(record: Path) -> list[list[str]]:
    """What each debug server saw, in order, one list per server in the order they started.

    A connection is named by what it sent, so GDB's reads `connection sent '+'`
    and a connection that only checked the port reads `connection sent ''`."""
    if not record.exists():
        return []
    events = [json.loads(line) for line in record.read_text(encoding="utf-8").splitlines()]
    sent: dict[tuple[int, int], str] = {}
    for event in events:
        if event["event"] == "received":
            key = (event["pid"], event["connection"])
            sent[key] = sent.get(key, "") + event["data"]
    seen: dict[int, list[str]] = {}
    for event in events:
        entries = seen.setdefault(event["pid"], [])
        if event["event"] == "listening":
            entries.append(PORT_LISTENING)
        elif event["event"] == "printed" and RECORDED_LISTENING_LINE.format(port=event["port"]) in event["lines"]:
            entries.append(LINE_PRINTED)
        elif event["event"] == "accepted":
            entries.append(f"connection sent {sent.get((event['pid'], event['connection']), '')!r}")
    return list(seen.values())


def test_the_session_log_has_one_accept_and_no_rejection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The recorded session, started once and stopped: OpenOCD accepted one
    connection, GDB's, and rejected none."""
    drive_fake_openocd(monkeypatch, tmp_path)
    service = debug_service(tmp_path)
    try:
        started = start_attach(service)
        assert started["ok"] is True, started
        stopped = service.call("debug_stop_session")
        assert stopped["ok"] is True, stopped
    finally:
        close(service)

    log = json.loads((Path(service.config.work_dir) / started["log_path"]).read_text(encoding="utf-8"))
    lines = log["server_stderr_tail"].splitlines()
    accepts = [line for line in lines if line.startswith("Info : accepting ")]
    rejections = [line for line in lines if line == RECORDED_REJECTED_LINE]
    assert (accepts, rejections) == ([RECORDED_ACCEPTING_LINE.format(port=started["gdb_port"])], [])


def test_the_only_connection_to_the_gdb_port_is_gdbs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing connects to the GDB port before GDB does."""
    record = drive_fake_openocd(monkeypatch, tmp_path)
    service = debug_service(tmp_path)
    try:
        started = start_attach(service)
        assert started["ok"] is True, started
        assert service.call("debug_stop_session")["ok"] is True
    finally:
        close(service)

    assert timelines(record) == [ONE_SESSION]


@pytest.mark.parametrize(
    ("listen_after", "line_after"),
    [("0", HELD_BACK_S), (HELD_BACK_S, "0")],
    ids=["port-listens-before-the-line", "startup-names-the-port-before-it-listens"],
)
def test_gdb_connects_after_the_listening_line_and_not_before(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, listen_after: str, line_after: str) -> None:
    """The port counts as ready when OpenOCD says it listens, not when it does.

    In the first case the port listens a second before OpenOCD says so, which
    is when a start that connects to find out would connect. In the second,
    OpenOCD's `starting gdb server for ... on <port>` line names the port a
    second before the port listens, and GDB sent there on the strength of it
    would find nothing to connect to."""
    record = drive_fake_openocd(monkeypatch, tmp_path, listen_after=listen_after, line_after=line_after)
    service = debug_service(tmp_path)
    try:
        started = start_attach(service)
        assert started["ok"] is True, started
        assert service.call("debug_stop_session")["ok"] is True
    finally:
        close(service)

    assert timelines(record) == [ONE_SESSION]


def test_other_listening_lines_do_not_count(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Only `gdb` and the reserved port count.

    A `tcl` and a `telnet` listening line for the session's own port and a
    `gdb` listening line for the next port up are printed as soon as the port
    listens, a second before the line that counts. GDB connects after that line."""
    record = drive_fake_openocd(monkeypatch, tmp_path, line_after=HELD_BACK_S, decoys=True)
    service = debug_service(tmp_path)
    try:
        started = start_attach(service)
        assert started["ok"] is True, started
        assert service.call("debug_stop_session")["ok"] is True
    finally:
        close(service)

    assert timelines(record) == [ONE_SESSION]


@pytest.mark.parametrize(
    ("listen_after", "decoys", "expected"),
    [
        ("0", False, [PORT_LISTENING]),
        ("0", True, [PORT_LISTENING]),
        (NEVER, False, []),
    ],
    ids=["listening-without-the-line", "only-other-listening-lines", "not-listening"],
)
def test_a_start_that_never_sees_the_line_times_out_naming_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, listen_after: str, decoys: bool, expected: list[str]) -> None:
    """No line before the deadline is today's timeout failure, `gdb_server_not_ready`,
    and nothing connects to the port, whether it listens or not.

    The summary names the line the start waited for, word for word, so an
    OpenOCD that never prints it (a release older than 0.11.0, or one whose
    configuration lowers `debug_level` or logs to a file) can be told from the
    result alone."""
    record = drive_fake_openocd(monkeypatch, tmp_path, listen_after=listen_after, line_after=NEVER, decoys=decoys)
    service = debug_service(tmp_path)
    try:
        started = start_attach(service, LINELESS_START_TIMEOUT_S)
    finally:
        close(service)

    assert {key: started.get(key) for key in TIMEOUT_FAILURE} == TIMEOUT_FAILURE
    assert started["summary"] == f'Debug server did not print "{AWAITED_LINE.format(port=served_port(record))}" before the timeout.', started
    assert timelines(record) == [expected]


def test_a_start_that_timed_out_still_names_the_read_that_sent_it_to_adapter_serial(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The timeout is the surface that failure is met on, and the read was named nowhere on it.

    With `probe_id` set and an `executable` that swallows `--version`, which this
    repo supports and which the fake answers the way such a wrapper does, the
    probe selection falls back to `adapter serial` and keeps the line the read
    failed with. A server that then hangs instead of exiting is the failure an
    operator meets most often, and the read was published only where the server
    had stopped: `probe_selection_read_failure` appeared on no timed-out start,
    and neither the result nor the session log named the read that chose the
    selector.

    The line is a fact about the command line the server was started with, known
    before it ran, so it is not a reclassification and the guard against those
    does not apply to it. What the timeout keeps is everything it says about where
    the server got to: the same `timeout` / `gdb_server_not_ready` and the same
    summary naming the line it never printed.
    """
    monkeypatch.setenv(VERSION_SWALLOWED_VARIABLE, "1")
    record = drive_fake_openocd(monkeypatch, tmp_path, line_after=NEVER)
    service = debug_service(tmp_path, probe_id=A_PROBE_SERIAL)
    try:
        started = start_attach(service, LINELESS_START_TIMEOUT_S)
    finally:
        close(service)

    assert {key: started.get(key) for key in TIMEOUT_FAILURE} == TIMEOUT_FAILURE
    assert started["summary"] == f'Debug server did not print "{AWAITED_LINE.format(port=served_port(record))}" before the timeout.', started
    # Nothing about the output was read: the server is still running, and no
    # command of ours was reported rejected.
    assert "rejected_commands" not in started, started
    read_failure = started["probe_selection_read_failure"]
    assert "OpenOCD release read" in read_failure, read_failure
    # First, because the release that could not be read is why the selector on
    # that command line is `adapter serial` at all, and ahead of the causes a
    # timeout carries rather than instead of them.
    assert started["likely_causes"][0] == read_failure, started["likely_causes"]
    assert "timeout_s is too low for this operation" in started["likely_causes"], started["likely_causes"]


def test_the_line_counts_even_when_later_output_pushes_it_out_of_the_tail(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The line is recognised as it arrives, not searched for in the capped tail
    afterwards: the recorded lines written with it push it out of the tail within
    the same read, and the start still finds it."""
    monkeypatch.setattr(gdbdebug, "OUTPUT_TAIL_CHARS", NARROW_TAIL_CHARS)
    record = drive_fake_openocd(monkeypatch, tmp_path, flood=FLOOD_LINES)
    service = debug_service(tmp_path)
    try:
        started = start_attach(service)
        assert started["ok"] is True, started
        assert service.call("debug_stop_session")["ok"] is True
    finally:
        close(service)

    assert timelines(record) == [ONE_SESSION]


def test_an_attach_whose_first_connect_is_dropped_connects_again_after_each_line(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The reconnect of an attach whose first connection is dropped (#575) is
    unchanged, and each of its two servers sees only GDB, after its own line."""
    record = drive_fake_openocd(monkeypatch, tmp_path)
    service = debug_service(tmp_path, f"{CONNECTS_TO_SERVER}+{CONNECT_DROPPED_ONCE}")
    try:
        started = start_attach(service)
        assert started["ok"] is True, started
        assert [entry["backend_error"] for entry in started["retried_connects"]] == [CONNECT_DROPPED_MESSAGE], started
        assert service.call("debug_stop_session")["ok"] is True
    finally:
        close(service)

    assert timelines(record) == [ONE_SESSION, ONE_SESSION]


@pytest.mark.parametrize("backend_name", ["pyocd", "stlink"])
def test_a_server_whose_line_has_no_recording_is_still_found_by_connecting(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backend_name: str) -> None:
    """pyOCD's gdbserver and ST-LINK_gdbserver keep the connect check.

    Neither backend runs a debug session yet, so the sessions are built the way
    such a backend would build them, with the fake OpenOCD standing in for a
    GDB server that listens and never prints OpenOCD's line. The start still
    finds the port, by the connection that checks it before GDB's."""
    record = drive_fake_openocd(monkeypatch, tmp_path, line_after=NEVER)
    config = load_config(str(write_config(tmp_path, debugger_type=backend_name, target_type="stm32f446re" if backend_name == "pyocd" else None, gdb_executable=FAKE_GDB, timeout_s=scaled_time_bound(START_TIMEOUT_S))))
    image = write_image(tmp_path, CONNECTS_TO_SERVER)
    sessions = GdbDebugSessions(
        config,
        backend_name=backend_name,
        resolve_server=lambda: {"ok": True, "executable_path": str(FAKE_OPENOCD)},
        build_server_args=lambda executable, port, reset: [sys.executable, executable, "-c", f"gdb_port {port}"],
        classify_server_output=lambda output: "unknown_debugger_error",
    )
    try:
        started = sessions.start_session({"source": "workspace", "path": "build/app.elf", "resolved_path": str(image)}, "attach", scaled_time_bound(START_TIMEOUT_S))
        assert started["ok"] is True, started
        assert sessions.stop_session()["ok"] is True
    finally:
        sessions.close()

    assert timelines(record) == [[PORT_LISTENING, CHECK_CONNECTED, GDB_CONNECTED]]
