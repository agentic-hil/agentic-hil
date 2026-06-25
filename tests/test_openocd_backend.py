# Copyright 2026 Hannes Pauli
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
from pathlib import Path

from aihil.artifacts import ArtifactManager
from aihil.config import load_config
from aihil.debuggers.openocd import OpenOCDBackend


ROOT = Path(__file__).resolve().parents[1]


def write_config(tmp_path: Path, timeout_s: float = 5) -> Path:
    fake = (ROOT / "tests" / "fixtures" / "fake_openocd.py").as_posix()
    path = tmp_path / ".aihil" / "config.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"""
debugger:
  type: "openocd"
  executable: "{fake}"
  interface_cfg: "interface/stlink.cfg"
  target_cfg: "target/stm32f4x.cfg"
  timeout_s: {timeout_s}
artifacts:
  allowed_roots: ["build"]
  allowed_extensions: [".elf", ".hex", ".bin"]
reports:
  directory: ".aihil/reports"
logs:
  directory: ".aihil/logs"
""",
        encoding="utf-8",
    )
    return path


def test_debugger_info_works_with_fake_openocd(tmp_path: Path) -> None:
    backend = OpenOCDBackend(load_config(write_config(tmp_path), work_dir=tmp_path))
    result = backend.info()
    assert result["ok"] is True
    assert result["tool"] == "aihil_debugger_info"
    assert "Open On-Chip Debugger" in result["version"]


def test_probe_target_calls_fake_openocd(tmp_path: Path, monkeypatch) -> None:
    record = tmp_path / "calls.json"
    monkeypatch.setenv("AIHIL_FAKE_OPENOCD_RECORD", str(record))
    backend = OpenOCDBackend(load_config(write_config(tmp_path), work_dir=tmp_path))

    result = backend.probe_target()

    assert result["ok"] is True
    assert result["target_detected"] is True
    calls = json.loads(record.read_text(encoding="utf-8"))
    assert any("init; targets" in item for item in calls[0])
    assert (tmp_path / ".aihil" / "reports" / "last-report.json").exists()


def test_openocd_actions_disable_tcp_server_ports(tmp_path: Path, monkeypatch) -> None:
    record = tmp_path / "calls.json"
    monkeypatch.setenv("AIHIL_FAKE_OPENOCD_RECORD", str(record))
    backend = OpenOCDBackend(load_config(write_config(tmp_path), work_dir=tmp_path))

    result = backend.probe_target()

    assert result["ok"] is True
    args = json.loads(record.read_text(encoding="utf-8"))[0]
    action_command_index = next(index for index, arg in enumerate(args) if "init; targets" in arg)
    for command in ["gdb_port disabled", "tcl_port disabled", "telnet_port disabled"]:
        assert command in args
        assert args.index(command) < action_command_index


def test_flash_firmware_accepts_image_path_and_computes_sha256(tmp_path: Path, monkeypatch) -> None:
    record = tmp_path / "calls.json"
    monkeypatch.setenv("AIHIL_FAKE_OPENOCD_RECORD", str(record))
    config = load_config(write_config(tmp_path), work_dir=tmp_path)
    firmware = tmp_path / "build" / "firmware.elf"
    firmware.parent.mkdir()
    firmware.write_bytes(b"\x7fELFfake")
    artifact = ArtifactManager(config).validate_local_path("build/firmware.elf")["artifact"]

    result = OpenOCDBackend(config).flash_firmware(artifact)

    assert result["ok"] is True
    assert result["artifact"]["path"] == "build/firmware.elf"
    assert result["artifact"]["sha256"]
    calls = json.loads(record.read_text(encoding="utf-8"))
    assert any("program" in item for item in calls[0])


def test_reset_target_accepts_only_known_modes(tmp_path: Path) -> None:
    backend = OpenOCDBackend(load_config(write_config(tmp_path), work_dir=tmp_path))

    assert backend.reset_target("run")["ok"] is True
    assert backend.reset_target("halt")["ok"] is True
    assert backend.reset_target("init")["ok"] is True
    invalid = backend.reset_target("bootloader")
    assert invalid["ok"] is False
    assert invalid["error_type"] == "invalid_argument"


def test_verify_failure_is_classified(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AIHIL_FAKE_OPENOCD_SCENARIO", "verify_failed")
    config = load_config(write_config(tmp_path), work_dir=tmp_path)
    firmware = tmp_path / "build" / "firmware.elf"
    firmware.parent.mkdir()
    firmware.write_bytes(b"\x7fELFfake")
    artifact = ArtifactManager(config).validate_local_path("build/firmware.elf")["artifact"]

    result = OpenOCDBackend(config).flash_firmware(artifact)

    assert result["ok"] is False
    assert result["error_type"] == "verify_failed"
    assert result["backend_error_type"] == "verify_failed"


def test_returncode_zero_verify_error_is_classified(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AIHIL_FAKE_OPENOCD_SCENARIO", "verify_failed_return_zero")
    config = load_config(write_config(tmp_path), work_dir=tmp_path)
    firmware = tmp_path / "build" / "firmware.elf"
    firmware.parent.mkdir()
    firmware.write_bytes(b"\x7fELFfake")
    artifact = ArtifactManager(config).validate_local_path("build/firmware.elf")["artifact"]

    result = OpenOCDBackend(config).flash_firmware(artifact)

    assert result["ok"] is False
    assert result["error_type"] == "verify_failed"
    assert result["backend_error_type"] == "verify_failed"


def test_missing_success_marker_is_not_success(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AIHIL_FAKE_OPENOCD_SCENARIO", "missing_success_marker")
    backend = OpenOCDBackend(load_config(write_config(tmp_path), work_dir=tmp_path))

    result = backend.probe_target()

    assert result["ok"] is False
    assert result["error_type"] == "target_not_detected"
    assert result["backend_error_type"] == "target_not_detected"


def test_backend_config_error_is_mapped_to_generic_error(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AIHIL_FAKE_OPENOCD_SCENARIO", "interface_config_not_found")
    backend = OpenOCDBackend(load_config(write_config(tmp_path), work_dir=tmp_path))

    result = backend.probe_target()

    assert result["ok"] is False
    assert result["error_type"] == "debugger_config_not_found"
    assert result["backend_error_type"] == "interface_config_not_found"


def test_adapter_open_failure_is_classified(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AIHIL_FAKE_OPENOCD_SCENARIO", "adapter_open_failed")
    backend = OpenOCDBackend(load_config(write_config(tmp_path), work_dir=tmp_path))

    result = backend.probe_target()

    assert result["ok"] is False
    assert result["error_type"] == "adapter_not_found"
    assert result["backend_error_type"] == "adapter_not_found"


def test_timeout_is_classified(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AIHIL_FAKE_OPENOCD_SCENARIO", "timeout")
    backend = OpenOCDBackend(load_config(write_config(tmp_path, timeout_s=0.1), work_dir=tmp_path))

    result = backend.probe_target()

    assert result["ok"] is False
    assert result["error_type"] == "timeout"
