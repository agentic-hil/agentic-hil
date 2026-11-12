"""A plan keeps decoded frames that one COM read returned past its match."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from conftest import write_config
from support import scaled_time_bound
from test_flash_capture import Board, BoardBackend

from agentic_hil.config import load_config
from agentic_hil.test_reactor import TestReactor, load_test_config
from agentic_hil.tools import AgenticHILToolService

PORT_ID = "defmt_plan_queue_uart"
PORT_DEVICE = "COM_DEFMTPQUEUE"
FIXTURES = Path(__file__).parent / "fixtures" / "defmt"


def _reference(record: dict) -> dict:
    if "error" in record:
        return {"error": "malformed"}
    return {
        "index": record["index"],
        "level": record["level"],
        "timestamp": record.get("timestamp"),
        "message": record["message"],
        "line": record["text"],
    }


def test_committed_burst_survives_nonclearing_repeat_opens(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    defmt_images: dict[str, Path],
) -> None:
    """One reset feeds all 46 frames; repeat opens do not discard queued records."""
    board = Board()
    monkeypatch.setitem(sys.modules, "serial", SimpleNamespace(Serial=board.construct))
    workspace = tmp_path / "workspace"
    config_path = write_config(
        workspace,
        com_ports_yaml=f'com_ports:\n  {PORT_ID}:\n    device: "{PORT_DEVICE}"\n',
    )
    image = workspace / "build" / "app.elf"
    image.parent.mkdir(parents=True, exist_ok=True)
    image.write_bytes(defmt_images["rzcobs"].read_bytes())
    stream = (FIXTURES / "stream-rzcobs.bin").read_bytes()
    oracle = [json.loads(line) for line in (FIXTURES / "expected-rzcobs.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(oracle) == 46

    config = load_config(str(config_path))
    backend = BoardBackend(board, banner=b"")
    service = AgenticHILToolService(config, backend=backend)
    board.watch = service.coordinator.bench.held_resources
    original_call = service.call
    observed: list[tuple[str, dict, dict]] = []
    burst_deliveries = 0

    def observe_call(name: str, arguments: dict | None = None) -> dict:
        nonlocal burst_deliveries
        supplied = dict(arguments or {})
        result = original_call(name, supplied)
        observed.append((name, supplied, result))
        if name == "com_session_start" and result.get("ok") is True and burst_deliveries == 0:
            # Reset only after the real UART open has succeeded. The backend
            # does not reset as part of flash, so this is the burst's one reset.
            reset_args = {"mode": "run"}
            reset = original_call("reset_target", reset_args)
            observed.append(("reset_target", reset_args, reset))
            assert reset.get("ok") is True, reset
            burst_deliveries += 1
            board.transmit(stream)
        return result

    monkeypatch.setattr(service, "call", observe_call)
    steps: list[dict] = [
        {
            "action": "flash",
            "debugger": "dut",
            "image_path": "build/app.elf",
            "reset_after_flash": False,
        }
    ]

    def expect_step(record: dict) -> dict:
        selectors = {
            "action": "defmt_expect",
            "port_id": PORT_ID,
            "timeout_s": scaled_time_bound(0.1),
            "text": record["message"],
        }
        if record["level"] is not None:
            selectors["level"] = record["level"]
        return selectors

    repeat_steps = [
        {"action": "uart_open", "port_id": PORT_ID, "clear_buffer": False, "defmt": True},
        {"action": "defmt_expect", "port_id": PORT_ID, "timeout_s": scaled_time_bound(0.1), "pattern": "."},
    ]
    steps.append({"action": "repeat", "count": 2, "steps": repeat_steps})
    steps.extend(expect_step(record) for record in oracle[2:])
    plan_path = workspace / ".agentic-hil" / "testconfig.yaml"
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(yaml.safe_dump({"version": 7, "name": "defmt-queued-burst", "steps": steps}, sort_keys=False), encoding="utf-8")

    reactor = TestReactor(config, service)
    try:
        plan = load_test_config(str(plan_path), str(workspace))
        assert reactor.preflight(plan) is None
        result = reactor.run(plan)
    finally:
        reactor.close()
        service.close()
        board.finish()

    repeat = result["steps"][1]
    assert repeat["result"]["iterations_run"] == 2
    iterations = repeat["iterations"]
    assert len(iterations) == 2
    opens = [iteration["steps"][0]["result"] for iteration in iterations]
    assert [opened.get("ok") for opened in opens] == [True, True]
    assert [opened.get("already_active") for opened in opens] == [False, True]
    assert result["ok"] is True, (result.get("error_type"), result.get("summary"), result.get("steps"), result.get("cleanup"))
    repeated_assertions = [iteration["steps"][1]["result"] for iteration in iterations]
    assert [assertion["reads"] for assertion in repeated_assertions] == [1, 0]
    remaining_assertions = [step["result"] for step in result["steps"][2:]]
    assert all(assertion["reads"] == 0 for assertion in remaining_assertions)
    matched = [assertion["frame"] for assertion in [*repeated_assertions, *remaining_assertions]]
    assert [_reference(frame) for frame in matched] == oracle

    assert burst_deliveries == 1
    assert board.opens == 1
    assert backend.calls.count("flash_firmware") == 1
    assert backend.calls.count("reset_target:run") == 1
    assert sum(name == "reset_target" for name, _, _ in observed) == 1
    assert sum(name == "com_read" for name, _, _ in observed) == 1
    assert len(matched) == 46
    assert result["cleanup_ok"] is True
    assert [item["action"] for item in result["cleanup"]] == ["uart_close"]
    assert sum(name == "com_session_stop" for name, _, _ in observed) == 1
    assert service.com_ports.sessions == {}
