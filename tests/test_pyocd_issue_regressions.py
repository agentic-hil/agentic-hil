"""Regression coverage for pyOCD issue reports #601-#603 and #561."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import write_config

from agentic_hil.backends.pyocd import PyOCDBackend
from agentic_hil.config import load_config
from agentic_hil.report import overall_success


@pytest.fixture
def backend(tmp_path: Path) -> PyOCDBackend:
    config_path = write_config(tmp_path, debugger_type="pyocd", probe_id="PYOCD123", target_type="stm32f446re")
    return PyOCDBackend(load_config(str(config_path)))


@pytest.mark.parametrize(
    "message",
    [
        "attempt to program invalid flash address",
        "flash uninit",
        "target was not halted as expected",
        "flash algorithm overflowed stack",
        "program page sequence not available",
        "delegate is not available",
    ],
)
def test_pyocd_flash_failure_phrases_have_a_flash_remedy(backend: PyOCDBackend, message: str) -> None:
    assert backend._classify_output(message, "flash_firmware") == "flash_failed"


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("flash program page failure (address 0x08000000; result code 0x1)", "flash_failed"),
        ("flash erase sector failure (address 0x08004000; result code 0x1)", "flash_erase_failed"),
        ("SWD/JTAG communication failure (No ACK)", "target_not_detected"),
    ],
)
def test_pyocd_flash_error_shapes_are_classified(backend: PyOCDBackend, message: str, expected: str) -> None:
    assert backend._classify_output(message, "flash_firmware") == expected


def test_failed_post_flash_reset_keeps_the_successful_flash_contact(backend: PyOCDBackend, monkeypatch: pytest.MonkeyPatch) -> None:
    responses = iter(
        [
            {"ok": True, "tool": "flash_firmware", "target_contacted": True, "hardware_state": "changed", "side_effect_status": "complete", "side_effect_committed": True},
            {"ok": False, "tool": "flash_firmware", "error_type": "reset_failed", "target_contacted": False, "hardware_state": "unchanged", "side_effect_status": "not_started", "retry_safe": True},
        ]
    )
    monkeypatch.setattr(backend, "_run_pyocd", lambda *args, **kwargs: next(responses))
    result = backend.flash_firmware({"resolved_path": str(Path(backend.config.workspace_root) / "firmware.elf")}, reset_after_flash=True)

    assert result["ok"] is False
    assert result["target_contacted"] is True
    assert result["hardware_state"] == "changed"
    assert result["side_effect_status"] == "partial"
    assert result["retry_safe"] is False
    assert result["verify"] is False
    assert "verified" not in result["summary"].lower()


@pytest.mark.parametrize("error_type", ["flash_failed", "flash_erase_failed", "verify_failed"])
def test_pyocd_flash_remedy_does_not_claim_an_unperformed_readback(error_type: str) -> None:
    from agentic_hil.knowledge import catalogue_entry

    remedy = catalogue_entry(f"{error_type}:pyocd")
    assert remedy is not None
    wording = " ".join([remedy["meaning"], *remedy["remediation"], *remedy.get("do_not", [])]).lower()

    assert "flash programs and verifies" not in wording


def test_failed_flash_does_not_claim_readback_verification(backend: PyOCDBackend, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(backend, "_resolve_probe_selector", lambda tool: {"ok": True})
    monkeypatch.setattr(backend, "_run_pyocd", lambda *args, **kwargs: {"ok": False, "tool": "flash_firmware", "error_type": "flash_failed", "summary": "programming failed"})

    result = backend.flash_firmware({"resolved_path": str(Path(backend.config.workspace_root) / "firmware.elf")})

    assert result["ok"] is False
    assert result["verify"] is False


@pytest.mark.parametrize(
    ("reset_after_flash", "expected_summary"),
    [(False, "Firmware flashed. Target was not reset."), (True, "Firmware flashed and target reset.")],
)
def test_successful_pyocd_flash_never_claims_readback_verification(backend: PyOCDBackend, monkeypatch: pytest.MonkeyPatch, reset_after_flash: bool, expected_summary: str) -> None:
    monkeypatch.setattr(backend, "_resolve_probe_selector", lambda tool: {"ok": True})
    monkeypatch.setattr(backend, "_run_pyocd", lambda *args, **kwargs: {"ok": True, "tool": "flash_firmware", "target_contacted": True, "hardware_state": "changed", "side_effect_status": "committed", "side_effect_committed": True})

    result = backend.flash_firmware({"resolved_path": str(Path(backend.config.workspace_root) / "firmware.elf")}, reset_after_flash=reset_after_flash)

    assert result["ok"] is True
    assert result["verify"] is False
    assert result["summary"] == expected_summary


@pytest.mark.parametrize(
    ("stdout", "stderr", "expected_diagnostic"),
    [
        ("pyOCD JSON error follows\n{\"status\": 1, \"error\": \"Could not open USB probe\"}\n", "", "Could not open USB probe"),
        ("pyOCD warning\nnot valid JSON\n", "Traceback: USB setup failed\nOSError: cannot open USB probe\n", "OSError: cannot open USB probe"),
    ],
)
def test_probe_enumeration_keeps_final_json_or_stderr_error_in_real_subprocess(tmp_path: Path, stdout: str, stderr: str, expected_diagnostic: str) -> None:
    fake = tmp_path / "fake_pyocd_error.py"
    fake.write_text(
        "import sys\n"
        f"sys.stdout.write({stdout!r})\n"
        f"sys.stderr.write({stderr!r})\n"
        "raise SystemExit(1)\n",
        encoding="utf-8",
    )
    config_path = write_config(tmp_path, debugger_type="pyocd", debugger_executable=fake, probe_id="PYOCD123", target_type="stm32f446re")
    backend = PyOCDBackend(load_config(str(config_path)))

    result = backend._enumerate_probes("debugger_probes_list")

    assert result["ok"] is False
    assert expected_diagnostic in result["summary"]
    assert result["programmer_output"]["stdout"].replace("\r\n", "\n") == stdout
    assert result["programmer_output"]["stderr"].replace("\r\n", "\n") == stderr
    assert result["programmer_output"]["returncode"] == 1


def test_probe_enumeration_extracts_final_error_line_from_pretty_json_without_stderr(tmp_path: Path) -> None:
    stdout = (
        "pyOCD probe discovery failed:\n"
        "{\n"
        '  "status": 1,\n'
        '  "error": "Traceback (most recent call last):\\n  File \\\"usb.py\\\", line 42\\nOSError: cannot open USB probe"\n'
        "}\n"
    )
    fake = tmp_path / "fake_pyocd_pretty_error.py"
    fake.write_text(
        "import sys\n"
        f"sys.stdout.write({stdout!r})\n"
        "raise SystemExit(1)\n",
        encoding="utf-8",
    )
    config_path = write_config(tmp_path, debugger_type="pyocd", debugger_executable=fake, probe_id="PYOCD123", target_type="stm32f446re")
    backend = PyOCDBackend(load_config(str(config_path)))

    result = backend._enumerate_probes("debugger_probes_list")

    assert result["ok"] is False
    assert result["summary"] == "pyOCD probe discovery command failed: OSError: cannot open USB probe"
    assert result["programmer_output"]["stdout"].replace("\r\n", "\n") == stdout
    assert result["programmer_output"]["stderr"] == ""
    assert result["programmer_output"]["returncode"] == 1


@pytest.mark.parametrize(
    ("payload", "diagnostic"),
    [
        ({"status": 1, "error": "USB probe could not be opened"}, "USB probe could not be opened"),
        ({"status": 1, "error": 12}, ""),
        ({"status": 1, "error": {"detail": "bad USB handle"}}, "bad USB handle"),
    ],
)
def test_probe_enumeration_failure_preserves_json_and_stderr_diagnostics(backend: PyOCDBackend, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, payload: dict, diagnostic: str) -> None:
    from agentic_hil.backends.common import CompletedCommand

    executable = tmp_path / "pyocd.exe"
    monkeypatch.setattr(backend, "_resolve_executable", lambda: {"ok": True, "executable_path": str(executable), "executable": str(executable)})
    raw_json = json.dumps(payload)
    monkeypatch.setattr(
        "agentic_hil.backends.pyocd.spawn_command",
        lambda *args, **kwargs: CompletedCommand(stdout=raw_json, stderr="Traceback: cannot open USB probe", returncode=1, timed_out=False, not_found=False),
    )

    result = backend._enumerate_probes("debugger_probes_list")
    assert result["ok"] is False
    if diagnostic:
        assert result["summary"] == f"pyOCD probe discovery command failed: {diagnostic}"
    else:
        assert result["summary"] == "pyOCD probe discovery command failed: Traceback: cannot open USB probe"
    assert result["programmer_output"]["stdout"] == raw_json
    assert result["programmer_output"]["stderr"].endswith("cannot open USB probe")
    log_path = Path(backend.config.workspace_root) / result["log_path"]
    assert json.loads(log_path.read_text(encoding="utf-8"))["stdout"] == raw_json


@pytest.mark.parametrize(
    ("stdout", "summary"),
    [
        ("pyOCD JSON warning\nnot-json\n", "pyOCD returned invalid probe-discovery JSON."),
        ('{"status": 0, "boards": "not-a-list"}', "pyOCD reported a probe-discovery failure."),
    ],
)
def test_zero_exit_probe_discovery_parse_refusal_keeps_output_and_action_log(
    backend: PyOCDBackend,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    stdout: str,
    summary: str,
) -> None:
    from agentic_hil.backends.common import CompletedCommand

    executable = tmp_path / "pyocd.exe"
    stderr = "warning: probe enumeration returned an unusable response"
    monkeypatch.setattr(backend, "_resolve_executable", lambda: {"ok": True, "executable_path": str(executable), "executable": str(executable)})
    monkeypatch.setattr(
        "agentic_hil.backends.pyocd.spawn_command",
        lambda *args, **kwargs: CompletedCommand(stdout=stdout, stderr=stderr, returncode=0, timed_out=False, not_found=False),
    )

    result = backend._enumerate_probes("debugger_probes_list")

    assert result["ok"] is False
    assert result["error_type"] == "probe_discovery_failed"
    assert result["summary"] == summary
    assert result["target_contacted"] is False
    assert result["side_effect_status"] == "not_started"
    assert result["hardware_state"] == "unchanged"
    assert result["retry_safe"] is True
    assert result["programmer_output"] == {"stdout": stdout, "stderr": stderr, "returncode": 0}
    log_path = Path(backend.config.workspace_root) / result["log_path"]
    action_log = json.loads(log_path.read_text(encoding="utf-8"))
    assert action_log["command"].endswith("json --probes --no-config")
    assert action_log["returncode"] == 0
    assert action_log["timed_out"] is False
    assert action_log["stdout"] == stdout
    assert action_log["stderr"] == stderr


def test_zero_exit_probe_discovery_parse_refusal_still_propagates_log_audit_failure(
    backend: PyOCDBackend,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from agentic_hil.backends.common import CompletedCommand

    executable = tmp_path / "pyocd.exe"
    stdout = "not-json"
    stderr = "diagnostic stderr"
    audit_error = OSError("could not persist pyOCD action log")
    monkeypatch.setattr(backend, "_resolve_executable", lambda: {"ok": True, "executable_path": str(executable), "executable": str(executable)})
    monkeypatch.setattr(
        "agentic_hil.backends.pyocd.spawn_command",
        lambda *args, **kwargs: CompletedCommand(stdout=stdout, stderr=stderr, returncode=0, timed_out=False, not_found=False),
    )
    monkeypatch.setattr(backend, "_write_log", lambda *args, **kwargs: audit_error)

    result = backend._enumerate_probes("debugger_probes_list")

    assert result["ok"] is False
    assert result["error_type"] == "probe_discovery_failed"
    assert result["programmer_output"] == {"stdout": stdout, "stderr": stderr, "returncode": 0}
    assert result["audit_ok"] is False
    assert result["target_contacted"] is False
    assert result["side_effect_status"] == "not_started"
    assert result["retry_safe"] is True
    assert "could not persist pyOCD action log" in result["audit_error"]["backend_error"]
    assert overall_success(result) is False


def test_recorded_stlink_usb_timeout_replays_as_not_contacted_probe_discovery_failure(
    backend: PyOCDBackend,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Replay the actual pyOCD USB-timeout transcript from the hosted VM capture."""
    from agentic_hil.backends.common import CompletedCommand

    fixture_path = Path(__file__).parent / "fixtures" / "pyocd_probe_discovery_usb_timeout_recording.json"
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    recording = fixture["recording"]
    assert recording["command"].endswith("pyocd json --probes --no-config")
    assert recording["returncode"] == 1
    executable = tmp_path / "pyocd.exe"
    monkeypatch.setattr(backend, "_resolve_executable", lambda: {"ok": True, "executable_path": str(executable), "executable": str(executable)})
    monkeypatch.setattr(
        "agentic_hil.backends.pyocd.spawn_command",
        lambda *args, **kwargs: CompletedCommand(
            stdout=recording["stdout"],
            stderr=recording["stderr"],
            returncode=recording["returncode"],
            timed_out=recording["timed_out"],
            not_found=False,
        ),
    )

    result = backend._enumerate_probes("debugger_probes_list")

    assert result["ok"] is False
    assert result["error_type"] == recording["result"]["error_type"]
    assert result["summary"] == recording["result"]["summary"]
    assert result["target_contacted"] is False
    assert result["side_effect_status"] == "not_started"
    assert result["hardware_state"] == "unchanged"
    assert result["retry_safe"] is True
    assert result["programmer_output"] == {
        "stdout": recording["stdout"],
        "stderr": recording["stderr"],
        "returncode": recording["returncode"],
    }
    action_log = json.loads((Path(backend.config.workspace_root) / result["log_path"]).read_text(encoding="utf-8"))
    assert action_log["command"].endswith("json --probes --no-config")
    assert action_log["returncode"] == recording["returncode"]
    assert action_log["timed_out"] is recording["timed_out"]
    assert action_log["stdout"] == recording["stdout"]
    assert action_log["stderr"] == recording["stderr"]
