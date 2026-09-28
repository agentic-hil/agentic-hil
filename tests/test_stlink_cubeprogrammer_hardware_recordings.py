"""Replay genuine STM32CubeProgrammer flash results captured on the bench."""

from __future__ import annotations

import json
from pathlib import Path

from conftest import FAKE_STLINK, write_config

from agentic_hil.backends import stlink
from agentic_hil.backends.common import CompletedCommand
from agentic_hil.backends.stlink import STLinkBackend
from agentic_hil.config import load_config

FIXTURE = Path(__file__).parent / "fixtures" / "stm32cubeprogrammer_2_23_0_nucleo_f446re_flash_recordings.json"
RECORDINGS = json.loads(FIXTURE.read_text(encoding="utf-8"))["recordings"]


def _backend(tmp_path: Path, monkeypatch, recording_name: str) -> tuple[STLinkBackend, Path]:
    recording = RECORDINGS[recording_name]
    config_path = write_config(tmp_path, debugger_type="stlink", debugger_executable=FAKE_STLINK, probe_id="recorded-probe")
    backend = STLinkBackend(load_config(str(config_path)))
    artifact_path = tmp_path / "build" / "nucleo-f446re_demo.elf"
    artifact_path.parent.mkdir(parents=True)
    artifact_path.write_bytes(b"recorded demo image")

    def replay(command: list[str], cwd: str, timeout_seconds: float) -> CompletedCommand:
        assert "mode=HOTPLUG" in command
        assert command[command.index("-w") + 1] == str(artifact_path)
        return CompletedCommand(
            stdout=recording["stdout"],
            stderr=recording["stderr"],
            returncode=recording["returncode"],
            timed_out=False,
            not_found=False,
        )

    monkeypatch.setattr(stlink, "spawn_command", replay)
    return backend, artifact_path


def test_real_cubeprogrammer_sector_erase_refusal_is_classified_and_preserved(tmp_path: Path, monkeypatch) -> None:
    recording = RECORDINGS["flash_erase_refused"]
    backend, artifact_path = _backend(tmp_path, monkeypatch, "flash_erase_refused")

    result = backend.flash_firmware({"resolved_path": str(artifact_path), "path": "build/nucleo-f446re_demo.elf"}, reset_after_flash=True)

    assert result["ok"] is False, result
    assert result["error_type"] == "flash_erase_failed", result
    assert result["backend_error_type"] == "flash_erase_failed", result
    assert result["summary"] == recording["summary"], result
    assert result["programmer_output"] == {
        "returncode": recording["returncode"],
        "stdout": recording["stdout"],
        "stderr": recording["stderr"],
    }
    assert result["erase_abort_point"]["reading"] == "erase_refused_effect_unconfirmed"
    assert result["verify"] is True
    assert result["reset_after_flash"] is True


def test_real_cubeprogrammer_recovery_retry_requires_verified_flash_and_reset(tmp_path: Path, monkeypatch) -> None:
    recording = RECORDINGS["flash_recovery_succeeded"]
    backend, artifact_path = _backend(tmp_path, monkeypatch, "flash_recovery_succeeded")

    result = backend.flash_firmware({"resolved_path": str(artifact_path), "path": "build/nucleo-f446re_demo.elf"}, reset_after_flash=True)

    assert result["ok"] is True, result
    assert result["success_confirmed"] is True, result
    assert result["operation_result"]["confirmed"] is True
    assert "Download verified successfully" in result["operation_result"]["matched_success_text"]
    assert result["summary"] == recording["summary"]
    assert result["verify"] is True
    assert result["reset_after_flash"] is True
    assert recording["cleanup_required"] is False
    assert recording["quarantined"] is False
    assert recording["side_effect_status"] == "committed"
