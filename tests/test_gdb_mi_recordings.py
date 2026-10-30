from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import FAKE_GDB, write_config
from fixtures import fake_gdb

from agentic_hil.backends.gdbdebug import GdbDebugSession
from agentic_hil.backends.openocd import OpenOCDBackend
from agentic_hil.config import load_config
from agentic_hil.gdbmi import stop_result_from_line
from tests.bench.test_bench_faults import sanitize_gdb_mi_source_paths

FIXTURE = Path(__file__).parent / "fixtures" / "gdb_mi_stop_recordings.json"


def recorded_stop(scenario: str, index: int) -> str:
    """One `*stopped` record of the bench recording, exactly as GDB printed it."""
    recordings = json.loads(FIXTURE.read_text(encoding="utf-8"))["recordings"]
    return next(case for case in recordings if case["scenario"] == scenario)["records"][index]


def test_source_path_sanitizer_redacts_only_absolute_file_and_fullname_values() -> None:
    line = '*stopped,reason="signal-received",signal-name="SIGINT",frame={addr="0x08000438",func="HardFault_Handler",file="/tmp/build path/main.c",fullname="/tmp/build path/main.c",line="87"}'

    sanitized = sanitize_gdb_mi_source_paths(line)

    assert sanitized == '*stopped,reason="signal-received",signal-name="SIGINT",frame={addr="0x08000438",func="HardFault_Handler",file="<path redacted>",fullname="<path redacted>",line="87"}'


def test_source_path_sanitizer_handles_escaped_absolute_paths_and_preserves_relative_paths() -> None:
    escaped = '*stopped,frame={file="/tmp/build\\"quoted/main.c",fullname="/tmp/build\\"quoted/main.c",func="main"}'
    relative = '*stopped,frame={file="Src/main.c",fullname="Src/main.c",func="main"}'

    assert sanitize_gdb_mi_source_paths(escaped) == '*stopped,frame={file="<path redacted>",fullname="<path redacted>",func="main"}'
    assert sanitize_gdb_mi_source_paths(relative) == relative


def test_real_gdb_stop_recordings_replay_through_the_stop_classifier(tmp_path: Path) -> None:
    recording = json.loads(FIXTURE.read_text(encoding="utf-8"))
    provenance = recording["provenance"]
    assert provenance["source_commit"] == "1b4a9c8116a433366c9d1fda0ceceae4f0378556"
    assert provenance["run_id"] == "36368702404-1"
    assert provenance["gdb_version"] == "GNU gdb (Debian 16.3-1) 16.3"
    assert provenance["backend"] == "openocd"
    assert provenance["board"] == "ST Nucleo-F446RE (STM32F446RE)"
    assert provenance["firmware_source"] == "tests/bench/firmware/undefined_instruction.c"
    assert provenance["workflow_url"] == "https://github.com/agentic-hil/agentic-hil/actions/runs/36368702404"
    redaction = "absolute source path replaced with <path redacted>; relative source names retained"
    assert provenance["redacted_fields"] == {"frame.file": redaction, "frame.fullname": redaction}

    config = load_config(str(write_config(tmp_path, gdb_executable=FAKE_GDB)))
    backend = OpenOCDBackend(config)
    cases = {case["scenario"]: case for case in recording["recordings"]}
    assert len(cases["attach_hardfault"]["records"]) == 1
    assert len(cases["reset_halt_to_hardfault"]["records"]) == 3
    assert "reason=" not in cases["attach_hardfault"]["records"][0]
    assert "reason=" not in cases["reset_halt_to_hardfault"]["records"][0]

    attach = GdbDebugSession("attach", {}, "attach", 0, SimpleNamespace(poll=lambda: None), [], "attach.json")
    attach_result = backend._debug._stop_reason_from_gdb(attach, stop_result_from_line(cases["attach_hardfault"]["records"][0]))
    assert attach_result["stop_reason"] == "exception"
    assert attach_result["exception_type"] == "hardfault"
    assert cases["attach_hardfault"]["expected_stop_reasons"] == [f'{attach_result["stop_reason"]}:{attach_result["exception_type"]}']

    reset = GdbDebugSession("reset", {}, "reset_halt", 0, SimpleNamespace(poll=lambda: None), [], "reset.json")
    reset.breakpoints = [{"backend_id": "1", "id": "bp-main"}]
    reset_results = [backend._debug._stop_reason_from_gdb(reset, stop_result_from_line(line)) for line in cases["reset_halt_to_hardfault"]["records"]]
    assert reset_results[0]["stop_reason"] == "reset"
    assert reset_results[1]["stop_reason"] == "breakpoint_hit"
    assert reset_results[1]["breakpoint_expected"] is True
    assert reset_results[2]["stop_reason"] == "exception"
    assert reset_results[2]["exception_type"] == "hardfault"
    actual_reset_reasons = []
    for result in reset_results:
        if result.get("exception_type"):
            actual_reset_reasons.append(f'{result["stop_reason"]}:{result["exception_type"]}')
        elif result.get("breakpoint_expected"):
            actual_reset_reasons.append(f'{result["stop_reason"]}:expected')
        else:
            actual_reset_reasons.append(result["stop_reason"])
    assert cases["reset_halt_to_hardfault"]["expected_stop_reasons"] == actual_reset_reasons


def test_fake_gdb_continue_under_hardfault_answers_the_recorded_fault_stop(monkeypatch: pytest.MonkeyPatch) -> None:
    """The stop the fake answers a continue with under `hardfault` is the bench's fault stop, byte for byte.

    The line is read where every continue reads it, before the fake schedules its
    delivery, so the claim is about the bytes and not about the delay."""
    monkeypatch.setattr(fake_gdb, "behavior_override", "hardfault")

    if fake_gdb.continue_stop_line() != recorded_stop("reset_halt_to_hardfault", 2):
        raise AssertionError("fake_gdb's continue under hardfault is not reset_halt_to_hardfault records[2] of gdb_mi_stop_recordings.json")


def test_fake_gdb_attach_under_stopped_on_attach_hardfault_prints_the_recorded_attach_stop(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """A connect under `stopped_on_attach_hardfault` is followed by the bench's attach stop and by no other.

    The fake's own main() reads the commands, so the stop is the one a session
    started over this fake is handed."""
    monkeypatch.setattr(fake_gdb, "behavior_override", "stopped_on_attach_hardfault")
    monkeypatch.setattr(sys, "argv", [str(FAKE_GDB)])
    monkeypatch.setattr(sys, "stdin", io.StringIO("1-target-select extended-remote localhost:3333\n2-gdb-exit\n"))

    assert fake_gdb.main() == 0
    stops = [line for line in capsys.readouterr().out.splitlines() if line.startswith("*stopped")]

    if stops != [recorded_stop("attach_hardfault", 0)]:
        raise AssertionError("fake_gdb's main() under stopped_on_attach_hardfault does not print attach_hardfault records[0] as its only stop")
