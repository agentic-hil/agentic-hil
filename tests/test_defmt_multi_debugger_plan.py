"""Exercise UART defmt provenance through a plan's per-debugger services."""

from __future__ import annotations

import json
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import write_config
from defmt_wire import build_elf, defmt_symbol, encode_frame, rzcobs_stream
from support import scaled_time_bound
from test_flash_capture import Board, BoardBackend

from agentic_hil import reactorrun, tools
from agentic_hil.config import load_config
from agentic_hil.debugger import UnboundDebuggerBackend
from agentic_hil.defmt import load_table
from agentic_hil.runlifecycle import RunRegistration, new_run_handle
from agentic_hil.test_reactor import TestReactor, build_test_step, load_test_config
from agentic_hil.types import AgenticHILConfig, JsonObject

IMAGE = "build/firmware.elf"
OTHER_IMAGE = "build/other.elf"
PORT_ID = "dut_uart"


@dataclass
class Bench:
    config: AgenticHILConfig
    board: Board
    backends: dict[str, BoardBackend]
    frame: bytes


def write_elf(path: Path, message: str) -> None:
    symbols = [
        ("_defmt_version_ = 4", 0, 0, None),
        ("_defmt_encoding_ = rzcobs", 0, 0, None),
        (defmt_symbol("defmt_info", message), 1, 1, ".defmt"),
    ]
    path.write_bytes(build_elf(symbols, {".defmt": bytes(2)}))


@pytest.fixture
def bench(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Bench]:
    workspace = tmp_path / "workspace"
    config = load_config(str(write_config(
        workspace,
        debugger_name="probe_a",
        debuggers_yaml="debuggers:\n  probe_b:\n    type: openocd\n",
        com_ports_yaml=f"com_ports:\n  {PORT_ID}:\n    device: COM_DEFMT_MULTI\n",
    )))
    assert config.debugger_id is None
    (workspace / "build").mkdir()
    write_elf(workspace / IMAGE, "boot")
    write_elf(workspace / OTHER_IMAGE, "other boot")
    table = load_table(workspace / IMAGE)
    frame = rzcobs_stream([encode_frame(table, "boot", [], tags=("defmt_info",))])
    board = Board()
    backends = {"probe_a": BoardBackend(board, banner=b""), "probe_b": BoardBackend(Board(), banner=b"")}

    def backend_for(bound: AgenticHILConfig) -> UnboundDebuggerBackend | BoardBackend:
        if bound.debugger_id is None:
            return UnboundDebuggerBackend(bound)
        return backends[bound.debugger_id]

    monkeypatch.setattr(tools, "create_debugger_backend", backend_for)
    monkeypatch.setitem(sys.modules, "serial", SimpleNamespace(Serial=board.construct))
    try:
        yield Bench(config, board, backends, frame)
    finally:
        for backend in backends.values():
            backend.board.finish()


def flash_step(probe: str, image: str) -> JsonObject:
    return {"device": probe, "action": "flash", "image_path": image, "reset_after_flash": True}


def uart_open_step(defmt: bool | JsonObject) -> JsonObject:
    return {"device": PORT_ID, "action": "uart_open", "defmt": defmt}


def run_plan(bench: Bench, monkeypatch: pytest.MonkeyPatch, steps: list[JsonObject]) -> JsonObject:
    path = Path(bench.config.work_dir) / "plan.json"
    path.write_text(json.dumps({"version": 7, "name": "defmt-multi-debugger", "steps": steps}), encoding="utf-8")
    plan = load_test_config(str(path), bench.config.work_dir)
    with RunRegistration.take(bench.config, new_run_handle(), name=plan.name, test_config_path=plan.path, detached=False) as registration:
        progress = registration.progress

        def feed_frame(update: JsonObject) -> None:
            progress(update)
            if update.get("action") == "defmt_expect":
                # uart_open has finished clearing input when this step starts.
                bench.board.transmit(bench.frame)

        monkeypatch.setattr(registration, "progress", feed_frame)
        result = reactorrun.run_registered_plan(bench.config, plan, wait_s=0.0, registration=registration)
        registration.finish(result)
        return result


def run_boot_plan(bench: Bench, monkeypatch: pytest.MonkeyPatch, defmt: bool | JsonObject) -> JsonObject:
    result = run_plan(bench, monkeypatch, [
        flash_step("probe_a", IMAGE),
        uart_open_step(defmt),
        {"device": PORT_ID, "action": "defmt_expect", "level": "info", "format": "boot", "timeout_s": scaled_time_bound(1.0)},
        {"device": PORT_ID, "action": "uart_close"},
    ])
    assert len(result["steps"]) >= 2, json.dumps(result)
    assert_confirmed_flash(bench, result["steps"][0]["result"], "probe_a")
    assert result["steps"][1]["action"] == "uart_open", result
    return result


def assert_confirmed_flash(bench: Bench, flash: JsonObject, probe: str) -> None:
    assert "flash_firmware" in bench.backends[probe].calls
    assert flash["ok"] is True, flash
    assert flash["success_confirmed"] is True, flash
    assert flash["side_effect_status"] == "committed", flash


def assert_boot_plan_passed(result: JsonObject) -> JsonObject:
    assert result["ok"] is True, result
    _, opened, matched, closed = [step["result"] for step in result["steps"]]
    assert opened["ok"] is True, opened
    assert matched["ok"] is True and matched["frame"]["message"] == "boot", matched
    assert closed["ok"] is True, closed
    return opened["defmt"]


def test_registered_plan_proves_implicit_flashed_elf(bench: Bench, monkeypatch: pytest.MonkeyPatch) -> None:
    result = run_boot_plan(bench, monkeypatch, True)
    if result["steps"][1]["result"].get("error_type") == "defmt_table_not_available":
        raise AssertionError("The plan UART refused the ELF confirmed by the named probe.")
    source = assert_boot_plan_passed(result)
    assert source["origin"] == "flashed", source
    assert source["proven"] is True, source


def test_registered_plan_proves_explicit_flashed_elf(bench: Bench, monkeypatch: pytest.MonkeyPatch) -> None:
    result = run_boot_plan(bench, monkeypatch, {"image_path": IMAGE})
    source = assert_boot_plan_passed(result)
    if source["proven"] is False:
        raise AssertionError("The plan UART reported the explicitly named flashed ELF as unproven.")
    assert source["proven"] is True, source


def test_unconfirmed_probe_flash_refuses_implicit_uart_before_open(bench: Bench) -> None:
    bench.backends["probe_a"].unconfirmed = True
    service, factory = reactorrun._plan_services(bench.config)
    reactor = TestReactor(service.config, service, service_factory=factory)
    try:
        flash = reactor.execute_step(build_test_step(flash_step("probe_a", IMAGE)))
        assert "flash_firmware" in bench.backends["probe_a"].calls
        assert flash["ok"] is False and flash["error_type"] == "flash_failed", flash
        opened = reactor.execute_step(build_test_step(uart_open_step(True)))
        assert opened["ok"] is False and opened["error_type"] == "defmt_table_not_available", opened
        assert bench.board.opens == 0
    finally:
        try:
            reactor.close()
        finally:
            service.close()


def test_distinct_probe_images_refuse_implicit_uart_before_open(bench: Bench, monkeypatch: pytest.MonkeyPatch) -> None:
    result = run_plan(bench, monkeypatch, [
        flash_step("probe_a", IMAGE),
        flash_step("probe_b", OTHER_IMAGE),
        uart_open_step(True),
    ])
    assert result["ok"] is False, result
    assert len(result["steps"]) == 3, json.dumps(result)
    first, second, opened = result["steps"]
    assert_confirmed_flash(bench, first["result"], "probe_a")
    assert_confirmed_flash(bench, second["result"], "probe_b")
    assert opened["action"] == "uart_open", opened
    assert opened["result"]["ok"] is False and opened["result"]["error_type"] == "defmt_table_not_available", opened
    assert bench.board.opens == 0
