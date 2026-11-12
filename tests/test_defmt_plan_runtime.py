"""Plan assertions observe the real simulated COM session and its lifetime."""

from __future__ import annotations

import json
import sys
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import write_config
from support import scaled_time_bound
from test_flash_capture import Board, BoardBackend

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
def runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, defmt_images: dict[str, Path]) -> Iterator[Runtime]:
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
        opened = runtime.service.call(
            "com_session_start", {"port_id": PORT_ID, "clear_buffer": False, "defmt": {"image_path": "build/app.elf"}}
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
