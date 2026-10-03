"""The pyOCD probe call preserves its operation and failure semantics with traceback diagnostics."""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import write_config

from agentic_hil.backends.common import CompletedCommand
from agentic_hil.backends.pyocd import PyOCDBackend
from agentic_hil.config import load_config


def test_probe_target_enables_tracebacks_without_retrying_or_changing_generic_usb_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = write_config(
        tmp_path,
        debugger_type="pyocd",
        probe_id="PYOCD123",
        target_type="stm32f446re",
        timeout_s=17,
    )
    backend = PyOCDBackend(load_config(str(config_path)))
    executable = tmp_path / "pyocd"
    monkeypatch.setattr(backend, "_resolve_executable", lambda: {"ok": True, "executable_path": str(executable), "executable": str(executable)})
    monkeypatch.setattr(backend, "_resolve_probe_selector", lambda tool: {"ok": True})
    monkeypatch.setattr(backend, "_confirm_target_support", lambda error_type: error_type)
    monkeypatch.setattr(backend, "_write_log", lambda *args, **kwargs: None)

    calls: list[tuple[list[str], float]] = []
    native_stderr = "0001439 C USB Error: [Errno 110] Operation timed out [__main__]\n"

    def run(command: list[str], cwd: str, timeout: float) -> CompletedCommand:
        calls.append((command, timeout))
        return CompletedCommand(stdout="", stderr=native_stderr, returncode=1, timed_out=False, not_found=False)

    monkeypatch.setattr("agentic_hil.backends.pyocd.spawn_command", run)

    result = backend.probe_target()

    commander_calls = [(command, timeout) for command, timeout in calls if "commander" in command]
    assert len(commander_calls) == 1, "traceback diagnostics must not add a commander retry"
    command, timeout = commander_calls[0]
    commander = command.index("commander")
    assert command[commander + 1 : commander + 3] == ["--command", "status"]
    assert [command[index + 1] for index, value in enumerate(command[:-1]) if value == "-O"] == ["debug.traceback=true", "pack.debug_sequences.disabled_sequences=DebugCoreStart"]
    assert command.count("-O") == 2
    assert command.count("--command") == 1
    assert command.count("-W") == 1
    assert "--connect" not in command and "-M" not in command
    assert timeout == 17, "the diagnostic option must not change the configured process deadline"

    # A pyOCD USB exception is the observed native failure, not evidence of a
    # completed NoACK or proof that the target was never contacted.
    assert result["ok"] is False
    assert result["error_type"] == "debugger_error"
    assert result["backend_error_type"] == "unknown_debugger_error"
    assert result["programmer_output"]["stderr"] == native_stderr
    assert result.get("target_contacted") is not False
    assert result.get("retry_safe") is not True
