from __future__ import annotations

import io
import json
import sys
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import FAKE_GDB, write_config
from fixtures import fake_gdb

from agentic_hil.backends.gdbdebug import RESET_HANDLER, GdbDebugSession, exception_type_from_handler, handler_name
from agentic_hil.backends.openocd import OpenOCDBackend
from agentic_hil.config import load_config
from agentic_hil.gdbmi import mi_field, stop_result_from_line
from tests.bench.test_bench_faults import sanitize_gdb_mi_source_paths

FIXTURE = Path(__file__).parent / "fixtures" / "gdb_mi_stop_recordings.json"
# The literal stops the fake answers, by the names it defines them under, in the
# order GDB_MANUAL_SHAPED_STOPS lists them. None of them is a recording.
MANUAL_SHAPED_STOP_NAMES = (
    "INTERRUPT_STOP",
    "EXPECTED_BREAKPOINT_STOP",
    "UNEXPECTED_BREAKPOINT_STOP",
    "EXITED_NORMALLY_STOP",
    "SIGTRAP_STOP",
    "SIGSEGV_STOP",
    "SIGUSR1_STOP",
    "END_STEPPING_RANGE_STOP",
)


def recorded_stop(scenario: str, index: int) -> str:
    """One `*stopped` record of the bench recording, exactly as GDB printed it."""
    recordings = json.loads(FIXTURE.read_text(encoding="utf-8"))["recordings"]
    return next(case for case in recordings if case["scenario"] == scenario)["records"][index]


def strings_in(value: object) -> Iterator[str]:
    """Every string a decoded recording holds, whatever its structure."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from strings_in(item)
    elif isinstance(value, list):
        for item in value:
            yield from strings_in(item)


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


def test_fake_gdb_continue_under_stop_in_reset_handler_answers_the_recorded_reset_stop(monkeypatch: pytest.MonkeyPatch) -> None:
    """The stop the fake answers a continue with under `stop_in_reset_handler` is the bench's reset stop, byte for byte.

    The OpenOCD / Debian GDB 16.3 record of the same bench run as both fault
    stops: no reason field, Reset_Handler at 0x08000454, line 56, and the
    fixture's path redactions."""
    monkeypatch.setattr(fake_gdb, "behavior_override", "stop_in_reset_handler")

    if fake_gdb.continue_stop_line() != recorded_stop("reset_halt_to_hardfault", 0):
        raise AssertionError("fake_gdb's continue under stop_in_reset_handler is not reset_halt_to_hardfault records[0] of gdb_mi_stop_recordings.json")


def test_the_invented_reset_stop_is_gone_from_the_fake() -> None:
    """The invented SIGINT stop in Reset_Handler at 0x080001c0 is no longer something the fake can answer."""
    if hasattr(fake_gdb, "RESET_HANDLER_STOP") or fake_gdb.STOP_IN_RESET_HANDLER in fake_gdb.STOP_LINES_BY_BEHAVIOR:
        raise AssertionError("fake_gdb still defines the invented RESET_HANDLER_STOP or answers stop_in_reset_handler from STOP_LINES_BY_BEHAVIOR")


def test_gdb_manual_shaped_stops_are_every_literal_stop_the_fake_answers() -> None:
    """The label is a tuple naming each literal stop the fake still answers, and nothing else."""
    expected = tuple(getattr(fake_gdb, name) for name in MANUAL_SHAPED_STOP_NAMES)

    if getattr(fake_gdb, "GDB_MANUAL_SHAPED_STOPS", None) != expected:
        raise AssertionError("fake_gdb.GDB_MANUAL_SHAPED_STOPS is not the tuple of the eight literal stops the fake answers")


def test_every_stop_line_by_behavior_is_labelled_manual_shaped() -> None:
    """Each stop a behaviour opts into by name is one of the labelled manual-shaped stops."""
    shaped = getattr(fake_gdb, "GDB_MANUAL_SHAPED_STOPS", ())

    if any(line not in shaped for line in fake_gdb.STOP_LINES_BY_BEHAVIOR.values()):
        raise AssertionError("a STOP_LINES_BY_BEHAVIOR value of fake_gdb is not in GDB_MANUAL_SHAPED_STOPS")


def test_no_manual_shaped_stop_is_a_recording_or_carries_a_handler_frame() -> None:
    """A manual-shaped stop passes for neither a recording nor a reset or fault stop.

    The recordings are every fixture recording beside the fake, read as data
    and walked whatever their structure. A handler is what the product's own
    classifier reads as a fault or a reset, so a renamed or lower-case handler
    counts as well as Reset_Handler and HardFault_Handler."""
    shaped = getattr(fake_gdb, "GDB_MANUAL_SHAPED_STOPS", None)
    if not shaped:
        raise AssertionError("fake_gdb has no GDB_MANUAL_SHAPED_STOPS to check")
    recorded = [text for path in sorted(FIXTURE.parent.glob("*recording*.json")) for text in strings_in(json.loads(path.read_text(encoding="utf-8")))]

    for line in shaped:
        if any(line in text for text in recorded):
            raise AssertionError(f"a manual-shaped stop of fake_gdb is held by a fixture recording: {line}")
        handler = handler_name(mi_field(line, "func"))
        if exception_type_from_handler(handler) is not None or handler.startswith(RESET_HANDLER):
            raise AssertionError(f"a manual-shaped stop of fake_gdb carries a reset or fault handler frame: {line}")
