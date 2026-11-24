"""Plan assertions observe the real simulated COM session and its lifetime."""

from __future__ import annotations

import json
import os
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import write_config
from defmt_fixtures import missing_toolchain_prerequisite
from support import scaled_time_bound
from test_flash_capture import Board, BoardBackend

from agentic_hil.comports import ComPortSession
from agentic_hil.config import load_config
from agentic_hil.test_reactor import TestReactor, build_test_step
from agentic_hil.tools import AgenticHILToolService

PORT_ID = "defmt_plan_runtime_uart"
FIXTURES = Path(__file__).parent / "fixtures" / "defmt"
PRIMING_FAILURE = "defmt_expect must match the priming frame before queue invalidation is tested."


class Runtime:
    def __init__(self, workspace: Path, board: Board, service: AgenticHILToolService) -> None:
        self.workspace = workspace
        self.board = board
        self.service = service
        self.reactor = TestReactor(service.config, service)
        self.observed: list[tuple[str, dict, dict]] = []
        self.stream = (FIXTURES / "stream-rzcobs.bin").read_bytes()
        self.oracle = [json.loads(line) for line in (FIXTURES / "expected-rzcobs.jsonl").read_text(encoding="utf-8").splitlines()]

    def step(self, action: str, **arguments: object) -> dict:
        return self.reactor.execute_step(build_test_step({"action": action, "port_id": PORT_ID, **arguments}))

    def expect(self, text: str) -> dict:
        return self.step("defmt_expect", text=text, timeout_s=scaled_time_bound(0.1))

    def reads(self) -> list[dict]:
        return [result for name, _, result in self.observed if name == "com_read"]

    def prime(self) -> str:
        assert len(self.oracle) == 46
        first, stale = self.oracle[0]["message"], self.oracle[1]["message"]
        assert sum(stale in record["message"] for record in self.oracle) == 1
        self.board.transmit(self.stream)
        result = self.expect(first)
        frame = result.get("frame")
        if result.get("ok") is not True or not isinstance(frame, dict) or frame.get("message") != first:
            raise AssertionError(PRIMING_FAILURE)
        reads = self.reads()
        assert len(reads) == 1
        assert len(reads[0]["frames"]) == len(self.oracle)
        return stale

    def require_fresh_timeout(self, text: str) -> None:
        before = len(self.reads())
        result = self.expect(text)
        new_reads = len(self.reads()) - before
        assert result.get("ok") is False, result
        assert result.get("error_type") == "defmt_expect_timeout", result
        assert new_reads >= 1, result
        assert result.get("reads") == new_reads, result


@pytest.fixture
def runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    defmt_images: dict[str, Path],
) -> Iterator[Runtime]:
    board = Board()
    monkeypatch.setitem(sys.modules, "serial", SimpleNamespace(Serial=board.construct))
    workspace = tmp_path / "workspace"
    config_path = write_config(
        workspace,
        com_ports_yaml=f'com_ports:\n  {PORT_ID}:\n    device: "COM_DEFMTPRUNTIME"\n',
    )
    image = workspace / "build" / "app.elf"
    image.parent.mkdir(parents=True, exist_ok=True)
    image.write_bytes(defmt_images["rzcobs"].read_bytes())
    (workspace / "build" / "other.elf").write_bytes(image.read_bytes())
    config = load_config(str(config_path))
    service = AgenticHILToolService(config, backend=BoardBackend(board, banner=b""))
    board.watch = service.coordinator.bench.held_resources
    scenario = Runtime(workspace, board, service)
    original_call = service.call

    def observe(name: str, arguments: dict | None = None) -> dict:
        supplied = dict(arguments or {})
        result = original_call(name, supplied)
        scenario.observed.append((name, supplied, result))
        return result

    monkeypatch.setattr(service, "call", observe)
    try:
        assert scenario.step("uart_open", clear_buffer=False, defmt={"image_path": "build/app.elf"}).get("ok") is True
        yield scenario
    finally:
        try:
            if PORT_ID in service.com_ports.sessions:
                closed = scenario.step("uart_close")
                assert closed.get("ok") is True, closed
        finally:
            try:
                scenario.reactor.close()
            finally:
                try:
                    service.close()
                finally:
                    board.finish()


def test_unmet_defmt_expect_reports_timeout(runtime: Runtime) -> None:
    result = runtime.expect("no such defmt message in the committed burst")
    reads = runtime.reads()
    if (
        result.get("ok") is not False
        or result.get("error_type") != "defmt_expect_timeout"
        or not reads
        or result.get("reads") != len(reads)
    ):
        raise AssertionError("An unmet defmt_expect must read the live UART and report defmt_expect_timeout.")


def test_concurrent_clear_invalidates_a_just_read_batch(runtime: Runtime, monkeypatch: pytest.MonkeyPatch) -> None:
    original_call = runtime.service.call
    cleared = False

    def clear_after_read(name: str, arguments: dict | None = None) -> dict:
        nonlocal cleared
        result = original_call(name, arguments)
        if name == "com_read" and result.get("frames") and not cleared:
            cleared = True
            opened = original_call("com_session_start", {"port_id": PORT_ID, "clear_buffer": True})
            assert opened.get("ok") is True, opened
        return result

    monkeypatch.setattr(runtime.service, "call", clear_after_read)
    runtime.board.transmit(runtime.stream)
    runtime.require_fresh_timeout(runtime.oracle[0]["message"])
    assert cleared


@pytest.mark.parametrize("replacement", ["session", "source"], ids=["session", "source"])
def test_retained_frames_require_live_session_and_source(runtime: Runtime, replacement: str) -> None:
    stale = runtime.prime()
    old_session = runtime.service.com_ports.sessions[PORT_ID]
    assert old_session.defmt is not None
    old_identity = old_session.defmt.identity
    old_epoch = old_session.buffer_epoch

    if replacement == "session":
        stopped = runtime.service.call("com_session_stop", {"port_id": PORT_ID})
        assert stopped.get("ok") is True, stopped
        # Match the consumed epoch so this checks session identity independently.
        opened = runtime.service.call(
            "com_session_start", {"port_id": PORT_ID, "clear_buffer": True, "defmt": {"image_path": "build/app.elf"}}
        )
        assert opened.get("ok") is True, opened
        current = runtime.service.com_ports.sessions[PORT_ID]
        assert current is not old_session
        assert current.defmt is not None and current.defmt.identity == old_identity
    else:
        opened = runtime.service.call(
            "com_session_start", {"port_id": PORT_ID, "clear_buffer": False, "defmt": {"image_path": "build/other.elf"}}
        )
        assert opened.get("ok") is True and opened.get("already_active") is True, opened
        current = runtime.service.com_ports.sessions[PORT_ID]
        assert current is old_session
        assert current.defmt is not None and current.defmt.identity != old_identity

    assert current.buffer_epoch == old_epoch
    runtime.require_fresh_timeout(stale)


@pytest.fixture(scope="session")
def defmt_toolchain_environment() -> dict[str, str]:
    # Capture Rust's locations before the per-test application sandbox moves
    # HOME and USERPROFILE. Do not undo that sandbox to resolve the toolchain.
    toolchain_profile = Path.home()
    return {
        "CARGO_HOME": os.environ.get("CARGO_HOME", str(toolchain_profile / ".cargo")),
        "RUSTUP_HOME": os.environ.get("RUSTUP_HOME", str(toolchain_profile / ".rustup")),
    }


def _required_overflow_runtime(request: pytest.FixtureRequest, toolchain_environment: dict[str, str]) -> Runtime:
    with pytest.MonkeyPatch.context() as toolchain:
        for name, value in toolchain_environment.items():
            toolchain.setenv(name, value)
        missing = missing_toolchain_prerequisite()
        if missing is not None:
            pytest.fail(f"Required defmt toolchain unavailable: {missing}")
        return request.getfixturevalue("runtime")


def _wait_until_logged(session: ComPortSession) -> None:
    """Wait until the reader has written everything the drain received to the session log.

    `Board.transmit` returns once the drain has read the line, but the reader
    logs that input on its own thread, and a full buffer is one entry of hex and
    text ending in an fsync. `com_session_stop` joins the reader for
    max(1 s, timeout_s + 0.5 s), so a teardown close that starts while that
    entry is still being written can outlast the join on a loaded host and
    answer com_port_close_failed. `log_received` takes the queue and writes its
    entry under `audit_lock`, so an empty queue seen under that lock is input
    already on the log."""
    deadline = time.monotonic() + scaled_time_bound(5.0)
    while time.monotonic() < deadline:
        with session.audit_lock, session.lock:
            if not session.received:
                return
        time.sleep(0.005)
    raise AssertionError("The COM reader did not log the overflow batch.")


def _overflow_serial_buffer(runtime: Runtime) -> None:
    session = runtime.service.com_ports.sessions[PORT_ID]
    with session.lock:
        stream = session.defmt
        assert stream is not None and stream.identity is not None
        identity = stream.identity
        overflow_before = session.overflow_bytes
        capacity = session.port_config.max_buffer_bytes
    board = runtime.board
    with board.guard:
        handle = board.handle
        lifecycle = [event for event in board.events if event in {"open", "clear", "close"}]
    assert handle is not None and handle.is_open

    # Separators contain no target frame. The real drain must trim these bytes.
    board.transmit(b"\0" * (capacity + 1))
    _wait_until_logged(session)

    with board.guard:
        assert not board.line
        assert "reader_stalled" not in board.events
        assert board.handle is handle and handle.is_open
        assert [event for event in board.events if event in {"open", "clear", "close"}] == lifecycle
    assert runtime.service.com_ports.sessions[PORT_ID] is session
    with session.lock:
        assert session.active and session.reader_error is None
        assert session.defmt is stream and stream.identity == identity
        assert session.overflow_bytes > overflow_before
        assert len(session.buffer) <= capacity


def test_overflow_during_com_read_preserves_decoded_frames(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
    defmt_toolchain_environment: dict[str, str],
) -> None:
    scenario = _required_overflow_runtime(request, defmt_toolchain_environment)
    target = scenario.oracle[0]["message"]
    session = scenario.service.com_ports.sessions[PORT_ID]
    stream = session.defmt
    original_call = scenario.service.call
    injected_batch: dict | None = None

    def overflow_after_read(name: str, arguments: dict | None = None) -> dict:
        nonlocal injected_batch
        result = original_call(name, arguments)
        if (name == "com_read" and result.get("ok") is True and injected_batch is None
                and any(frame.get("message") == target and "error" not in frame for frame in result.get("frames", []))):
            injected_batch = result
            _overflow_serial_buffer(scenario)
        return result

    monkeypatch.setattr(scenario.service, "call", overflow_after_read)
    scenario.board.transmit(scenario.stream)
    result = scenario.expect(target)

    assert injected_batch is not None
    assert injected_batch is scenario.reads()[0]
    assert injected_batch["bytes_read"] > 0
    matching = [frame for frame in injected_batch["frames"] if frame.get("message") == target and "error" not in frame]
    assert len(matching) == 1
    assert scenario.service.com_ports.sessions[PORT_ID] is session and session.defmt is stream
    if result.get("ok") is not True:
        raise AssertionError("overflow during com_read discarded a valid decoded frame")
    assert result["frame"] == matching[0]
    assert result["reads"] == 1


def test_overflow_between_defmt_expect_calls_preserves_retained_frames(
    request: pytest.FixtureRequest, defmt_toolchain_environment: dict[str, str],
) -> None:
    scenario = _required_overflow_runtime(request, defmt_toolchain_environment)
    target = scenario.prime()
    matching = [frame for frame in scenario.reads()[0]["frames"] if frame.get("message") == target and "error" not in frame]
    assert len(matching) == 1
    _overflow_serial_buffer(scenario)
    reads_before = len(scenario.reads())

    result = scenario.expect(target)

    if result.get("ok") is not True:
        raise AssertionError("overflow between defmt_expect calls discarded a valid retained frame")
    assert result["frame"] == matching[0]
    assert result["reads"] == 0
    assert len(scenario.reads()) == reads_before


@pytest.mark.parametrize("clearer", ["uart_open", "com_session_start"], ids=["uart_open", "com_session_start"])
def test_retained_frames_require_live_buffer_epoch(runtime: Runtime, clearer: str) -> None:
    stale = runtime.prime()
    session = runtime.service.com_ports.sessions[PORT_ID]
    assert session.defmt is not None
    identity = session.defmt.identity
    epoch = session.buffer_epoch

    if clearer == "uart_open":
        opened = runtime.step("uart_open", clear_buffer=True)
    else:
        opened = runtime.service.call("com_session_start", {"port_id": PORT_ID, "clear_buffer": True})
    assert opened.get("ok") is True and opened.get("already_active") is True, opened
    current = runtime.service.com_ports.sessions[PORT_ID]
    assert current is session
    assert current.defmt is not None and current.defmt.identity == identity
    assert current.buffer_epoch > epoch

    runtime.require_fresh_timeout(stale)
