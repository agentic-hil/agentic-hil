"""The recovery's reset into halt when the in-circuit debugger cannot reach the target yet (#621).

Recorded on the bench after a flash killed mid-write (2026-10-06, OpenOCD 0.12.0
and 0.11.0 with `debug_level 3`, high-level adapter): the next OpenOCD finds the
in-circuit debugger answering USB but not the target. Every debug-port access
fails and the identity code reads 0x00000000:

    Debug: stlink_usb_error_check(): STLINK_SWD_DP_ERROR
    Debug: stlink_usb_idcode(): IDCODE: 0x00000000
    Warn : target stm32f4x.cpu examination failed

Waiting does not clear it (five more examinations, 100 ms apart, failed the
same way). A `reset halt` sent into it writes its halt request and vector catch
through that broken connection, the writes fail without a word, and only then
does the adapter's own reset command run, which brings the connection back. The
core is running by then, with no vector catch set, and the reset ends in the
line #621 was filed on:

    Error: timed out while waiting for target halted

A reset run before it, in the same OpenOCD, is the one that brings the
connection back: on the bench the `reset halt` after it examined at once, set
its vector catch and halted. Whether `init` examined the target is no test for
the state: on 0.11.0 the target counted as examined while every access still
failed. So the recovery's reset command always runs one reset into halt ahead,
with its failure caught; on a healthy connection that one simply halts too.

The fake below answers the way the bench did: a reset into halt with no reset
ahead of it on the same command line gets the recording above, one with a reset
ahead of it halts.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from test_recovery_reset_retry import (  # noqa: F401
    EXAMINATION_FAILED,
    STARTUP,
    ScriptedOpenOCD,
    target_would_not_halt,
    waits,
)
from test_run_abort_recovery import RESET_REASON, FakeBackend, config_for, quarantine

from agentic_hil import tools as tools_module
from agentic_hil.backends import openocd as openocd_backend
from agentic_hil.backends.common import CompletedCommand
from agentic_hil.backends.openocd import OPENOCD_INIT_STAGE_MARKER, OPENOCD_SUCCESS_MARKERS, OpenOCDBackend
from agentic_hil.tools import AgenticHILToolService

# The reset ahead's own answer while the connection is broken.
TIMED_OUT = "Error: timed out while waiting for target halted"


def halted_after_the_reconnecting_reset() -> CompletedCommand:
    """The bench's runs, at the default debug level: the reset ahead times out, the one after it halts."""
    stderr = "".join(
        f"{line}\n"
        for line in (
            "Info : STLINK V2J30M19 (API v2) VID:PID 0483:374B",
            "Info : Target voltage: 3.268556",
            EXAMINATION_FAILED,
            "Info : gdb port disabled",
            OPENOCD_INIT_STAGE_MARKER,
            TIMED_OUT,
            "TARGET: stm32f4x.cpu - Not halted",
            "Info : Unable to match requested speed 2000 kHz, using 1800 kHz",
            "Info : Unable to match requested speed 2000 kHz, using 1800 kHz",
            "Info : [stm32f4x.cpu] Cortex-M4 r0p1 processor detected",
            "Info : [stm32f4x.cpu] target has 6 breakpoints, 4 watchpoints",
            OPENOCD_SUCCESS_MARKERS["reset_target"],
        )
    )
    return CompletedCommand(stdout="", stderr=f"{STARTUP}{stderr}", returncode=0, timed_out=False, not_found=False)


def ahead_of_the_last_reset_halt(command: str) -> str:
    return command[: command.rindex("reset halt")]


def a_reset_runs_ahead_of_the_halt(command: str) -> bool:
    ahead = ahead_of_the_last_reset_halt(command)
    return re.search(r"\breset\b", ahead) is not None


class UnreachableAfterTheKill(ScriptedOpenOCD):
    """An in-circuit debugger that reaches the target again only after a reset."""

    def __init__(self) -> None:
        super().__init__([])
        self.reset_commands: list[str] = []

    def __call__(self, args: list[str], *rest, **kwargs) -> CompletedCommand:
        command = args[-1]
        if "reset halt" in command:
            self.commands.append("reset halt")
            self.reset_commands.append(command)
            return halted_after_the_reconnecting_reset() if a_reset_runs_ahead_of_the_halt(command) else target_would_not_halt()
        return super().__call__(args, *rest, **kwargs)


def recover(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, openocd: UnreachableAfterTheKill) -> dict:
    monkeypatch.setattr(openocd_backend, "spawn_command", openocd)
    service = AgenticHILToolService(config_for(tmp_path))
    try:
        quarantine(service, RESET_REASON)
        return service.recover_after_failed_run(["dut"])
    finally:
        service.close()


def test_the_recovery_reconnects_before_it_resets_into_halt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, waits: list[float]) -> None:  # noqa: F811
    openocd = UnreachableAfterTheKill()
    recovery = recover(tmp_path, monkeypatch, openocd)

    assert recovery["outcome"] == "recovered", recovery
    assert recovery["reset_halt_attempts"] == 1, recovery
    assert "reset_halt_retried_on" not in recovery, recovery
    assert openocd.commands == ["reset halt", "targets"]
    assert waits == []


def test_the_reset_ahead_is_a_reset_into_halt_whose_failure_is_caught(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, waits: list[float]) -> None:  # noqa: F811
    openocd = UnreachableAfterTheKill()
    recover(tmp_path, monkeypatch, openocd)

    [command] = openocd.reset_commands
    # Into halt, so a healthy target never runs between the two resets; caught,
    # so its timeout is not the command's end.
    assert "catch {reset halt}" in ahead_of_the_last_reset_halt(command), command


def test_the_reset_tool_resets_into_halt_without_the_reset_ahead(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    openocd = UnreachableAfterTheKill()
    monkeypatch.setattr(openocd_backend, "spawn_command", openocd)
    backend = OpenOCDBackend(config_for(tmp_path))

    result = backend.reset_target("halt")

    assert result["ok"] is False, result
    [command] = openocd.reset_commands
    assert not a_reset_runs_ahead_of_the_halt(command), command


class RecordingBackend(FakeBackend):
    def reset_target(self, mode: str = "run") -> dict:
        self.calls.append(f"reset_target:{mode}")
        return {"ok": True, "tool": "reset_target", "mode": mode}


def test_another_backend_gets_its_plain_reset_into_halt() -> None:
    backend = RecordingBackend()

    tools_module.recovery_reset_halt(backend)

    assert backend.calls == ["reset_target:halt"]


def test_the_recovery_reset_on_openocd_runs_the_reset_ahead(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    openocd = UnreachableAfterTheKill()
    monkeypatch.setattr(openocd_backend, "spawn_command", openocd)
    backend = OpenOCDBackend(config_for(tmp_path))

    result = tools_module.recovery_reset_halt(backend)

    assert result["ok"] is True, result
    assert openocd.reset_commands and a_reset_runs_ahead_of_the_halt(openocd.reset_commands[0])
