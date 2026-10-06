"""The recovery's reset into halt, right after a flash was killed mid-write (#621).

Killing OpenOCD while it writes leaves the in-circuit debugger re-enumerating
for a moment, and a reset into halt sent into that moment is answered by one of
two lines out of OpenOCD's adapter driver, before any target is addressed:

* `Error: mode (transport) not supported by device`
* `Error: init mode failed (unable to connect to the target)`

Those two, and only those two, are retried, on a bounded backoff of about ten
seconds in all. Anything else fails at once, and whatever fails carries the
backend's own line and the number of attempts, so the result says why.

The OpenOCD answers are fed in at the spawn, so the real backend classifies them
and builds the result the recovery reads. Around the two lines, which are the
in-circuit debugger's own words on the bench, each transcript keeps the startup
format of the OpenOCD recording in tests/fixtures/fake_openocd.py.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from test_run_abort_recovery import RESET_REASON, FakeBackend, config_for, quarantine

from agentic_hil import tools as tools_module
from agentic_hil.backends import openocd as openocd_backend
from agentic_hil.backends.common import CompletedCommand
from agentic_hil.backends.openocd import OPENOCD_INIT_STAGE_MARKER, OPENOCD_SUCCESS_MARKERS
from agentic_hil.tools import AgenticHILToolService

TRANSPORT_NOT_SUPPORTED = "Error: mode (transport) not supported by device"
INIT_MODE_FAILED = "Error: init mode failed (unable to connect to the target)"
# A failure that is not the probe coming back: the target itself would not halt.
# Recorded on the bench right after a killed flash (2026-10-06), with the warning
# OpenOCD wrote ahead of it, which is not the line the reset failed on.
EXAMINATION_FAILED = "Warn : target stm32f4x.cpu examination failed"
OTHER_FAILURE = "Error: timed out while waiting for target halted"

STARTUP = (
    "Open On-Chip Debugger 0.11.0\n"
    "Licensed under GNU GPL v2\n"
    "For bug reports, read\n"
    "\thttp://openocd.org/doc/doxygen/bugs.html\n"
    'Info : auto-selecting first available session transport "hla_swd". To override use \'transport select <transport>\'.\n'
    "Info : The selected transport took over low-level target control. The results might differ compared to plain JTAG/SWD\n"
    "Info : clock speed 2000 kHz\n"
)


def adapter_refused(line: str) -> CompletedCommand:
    """`init` failing inside the adapter driver: the stage marker never printed."""
    return CompletedCommand(stdout="", stderr=f"{STARTUP}{line}\n", returncode=1, timed_out=False, not_found=False)


def target_would_not_halt() -> CompletedCommand:
    """The bench's recording: the warning, then the tail of the capture as it was logged."""
    stderr = "".join(
        f"{line}\n"
        for line in (
            EXAMINATION_FAILED,
            "Info : gdb port disabled",
            OPENOCD_INIT_STAGE_MARKER,
            "Info : Unable to match requested speed 2000 kHz, using 1800 kHz",
            "Info : Unable to match requested speed 2000 kHz, using 1800 kHz",
            "Info : [stm32f4x.cpu] Cortex-M4 r0p1 processor detected",
            "Info : [stm32f4x.cpu] target has 6 breakpoints, 4 watchpoints",
            OTHER_FAILURE,
            "TARGET: stm32f4x.cpu - Not halted",
        )
    )
    return CompletedCommand(stdout="", stderr=stderr, returncode=1, timed_out=False, not_found=False)


def confirmed(tool: str) -> CompletedCommand:
    stderr = (
        f"{STARTUP}Info : STLINK V2J30M19 (API v2) VID:PID 0483:374B\n"
        "Info : Target voltage: 3.264253\n"
        "Info : [stm32f4x.cpu] Cortex-M4 r0p1 processor detected\n"
        f"{OPENOCD_INIT_STAGE_MARKER}\n"
        f"{OPENOCD_SUCCESS_MARKERS[tool]}\n"
    )
    return CompletedCommand(stdout="", stderr=stderr, returncode=0, timed_out=False, not_found=False)


class ScriptedOpenOCD:
    """The OpenOCD runs of one recovery: each reset takes the next scripted answer."""

    def __init__(self, resets: list[CompletedCommand]) -> None:
        self.resets = list(resets)
        self.commands: list[str] = []

    def __call__(self, args: list[str], *_args, **_kwargs) -> CompletedCommand:
        command = args[-1]
        if "reset halt" in command:
            self.commands.append("reset halt")
            return self.resets.pop(0) if len(self.resets) > 1 else self.resets[0]
        if "targets" in command:
            self.commands.append("targets")
            return confirmed("probe_target")
        if "program" in command:
            self.commands.append("program")
        # The configuration-stage reads (`--version` and the like) are not the
        # recovery's business; an empty answer leaves the selector at its default.
        return CompletedCommand(stdout="", stderr="", returncode=0, timed_out=False, not_found=False)


@pytest.fixture
def waits(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """The waits the recovery asks for between attempts, taken instead of slept."""
    taken: list[float] = []
    monkeypatch.setattr(tools_module, "recovery_reset_retry_sleep", taken.append, raising=False)
    return taken


def recover(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, resets: list[CompletedCommand]) -> tuple[dict, ScriptedOpenOCD]:
    openocd = ScriptedOpenOCD(resets)
    monkeypatch.setattr(openocd_backend, "spawn_command", openocd)
    config = config_for(tmp_path)
    service = AgenticHILToolService(config)
    try:
        quarantine(service, RESET_REASON)
        return service.recover_after_failed_run(["dut"]), openocd
    finally:
        service.close()


@pytest.mark.parametrize("line", [TRANSPORT_NOT_SUPPORTED, INIT_MODE_FAILED])
def test_a_reset_refused_while_the_probe_comes_back_is_tried_again(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, waits: list[float], line: str) -> None:
    recovery, openocd = recover(tmp_path, monkeypatch, [adapter_refused(line), adapter_refused(line), confirmed("reset_target")])

    assert recovery["outcome"] == "recovered", recovery
    assert recovery["actions"] == ["reap_processes", "reset_halt", "probe_target"], recovery
    assert recovery["reset_halt_attempts"] == 3, recovery
    assert recovery["reset_halt_retried_on"] == [line, line], recovery
    assert openocd.commands == ["reset halt", "reset halt", "reset halt", "targets"]
    assert waits == [0.5, 1.0]
    assert recovery["incident_resolved"] is True, recovery


def test_the_retry_gives_up_after_about_ten_seconds_and_names_the_line(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, waits: list[float]) -> None:
    recovery, openocd = recover(tmp_path, monkeypatch, [adapter_refused(INIT_MODE_FAILED)])

    assert recovery["outcome"] == "failed", recovery
    assert recovery["failed_action"] == "reset_halt", recovery
    assert recovery["reset_halt_attempts"] == 6, recovery
    assert recovery["backend_error_line"] == INIT_MODE_FAILED, recovery
    assert INIT_MODE_FAILED in recovery["summary"], recovery
    assert "6 attempts" in recovery["summary"], recovery
    assert sum(waits) == pytest.approx(10.0)
    assert openocd.commands == ["reset halt"] * 6
    assert "program" not in openocd.commands


def test_any_other_reset_failure_is_not_retried_and_carries_its_line(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, waits: list[float]) -> None:
    recovery, openocd = recover(tmp_path, monkeypatch, [target_would_not_halt(), confirmed("reset_target")])

    assert recovery["outcome"] == "failed", recovery
    assert recovery["failed_action"] == "reset_halt", recovery
    assert recovery["reset_halt_attempts"] == 1, recovery
    assert recovery["backend_error_line"] == OTHER_FAILURE, recovery
    assert OTHER_FAILURE in recovery["summary"], recovery
    assert waits == []
    assert openocd.commands == ["reset halt"]


def test_a_line_that_only_mentions_the_words_is_not_retried(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, waits: list[float]) -> None:
    quoted = f"Error: {INIT_MODE_FAILED} while reading the option bytes"
    recovery, openocd = recover(tmp_path, monkeypatch, [adapter_refused(quoted), confirmed("reset_target")])

    assert recovery["outcome"] == "failed", recovery
    assert recovery["reset_halt_attempts"] == 1, recovery
    assert waits == []
    assert openocd.commands == ["reset halt"]


def test_a_reset_that_works_at_once_says_it_took_one_attempt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, waits: list[float]) -> None:
    recovery, openocd = recover(tmp_path, monkeypatch, [confirmed("reset_target")])

    assert recovery["outcome"] == "recovered", recovery
    assert recovery["reset_halt_attempts"] == 1, recovery
    assert "reset_halt_retried_on" not in recovery, recovery
    assert waits == []
    assert openocd.commands == ["reset halt", "targets"]


class OtherBackendRefusing(FakeBackend):
    """Another backend whose reset failed with the same words in its output."""

    def reset_target(self, mode: str = "run") -> dict:
        self.calls.append(f"reset_target:{mode}")
        return {
            "ok": False,
            "tool": "reset_target",
            "backend": "pyocd",
            "mode": mode,
            "error_type": "reset_failed",
            "programmer_output": {"returncode": 1, "stdout": "", "stderr": f"{INIT_MODE_FAILED}\n"},
        }


def test_the_retry_is_for_openocd_alone(tmp_path: Path, waits: list[float]) -> None:
    config = config_for(tmp_path)
    backend = OtherBackendRefusing()
    service = AgenticHILToolService(config, backend=backend)
    try:
        recovery = service.recover_after_failed_run(["dut"])
    finally:
        service.close()

    assert recovery["outcome"] == "failed", recovery
    assert recovery["reset_halt_attempts"] == 1, recovery
    assert recovery["backend_error_line"] == INIT_MODE_FAILED, recovery
    assert backend.calls == ["reset_target:halt"]
    assert waits == []
