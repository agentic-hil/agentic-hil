# Copyright 2026 Hannes Pauli
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from aihil.config import DEFAULT_CONFIG_PATH, ConfigError, config_schema, load_config, resolve_config_path
from aihil.debugger import create_debugger_backend
from aihil.debuggers.openocd import OpenOCDBackend


def write_config(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def base_config(executable: str | None = None, debugger_type: str = "openocd") -> str:
    executable_text = "null" if executable is None else f'"{executable}"'
    return f"""
debugger:
  type: "{debugger_type}"
  executable: {executable_text}
  interface_cfg: "interface/stlink.cfg"
  target_cfg: "target/stm32f4x.cfg"
  timeout_s: 5
artifacts:
  allowed_roots: ["build"]
"""


def test_default_config_path_is_dot_aihil_config_yaml() -> None:
    assert resolve_config_path() == DEFAULT_CONFIG_PATH


def test_config_schema_exposes_config_yaml_shape() -> None:
    schema = config_schema()

    assert schema["properties"]["debugger"]["properties"]["type"]["enum"] == ["openocd"]
    assert "server" not in schema["properties"]
    assert schema["properties"]["permissions"]["properties"]["allow_flash"]["type"] == "boolean"


def test_config_argument_overrides_default_path(tmp_path: Path) -> None:
    config_path = write_config(tmp_path / "custom.yaml", base_config())
    config = load_config(config_path, work_dir=tmp_path)
    assert config.config_path == config_path


def test_config_root_must_be_mapping(tmp_path: Path) -> None:
    config_path = write_config(tmp_path / ".aihil" / "config.yaml", "false\n")

    with pytest.raises(ConfigError) as exc:
        load_config(config_path, work_dir=tmp_path)

    assert exc.value.error_type == "config_invalid"


def test_debugger_executable_from_config_is_used(tmp_path: Path) -> None:
    fake = tmp_path / "fake_openocd.py"
    fake.write_text("", encoding="utf-8")
    config_path = write_config(tmp_path / ".aihil" / "config.yaml", base_config(fake.as_posix()))
    backend = OpenOCDBackend(load_config(config_path, work_dir=tmp_path))
    resolved = backend._resolve_executable()
    assert resolved["ok"] is True
    assert resolved["executable_path"] == str(fake)


def test_debugger_executable_uses_path_when_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_dir = tmp_path / "bin"
    fake_dir.mkdir()
    executable = fake_dir / ("openocd.bat" if os.name == "nt" else "openocd")
    executable.write_text("@echo off\n" if os.name == "nt" else "#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setenv("PATH", str(fake_dir))
    config_path = write_config(tmp_path / ".aihil" / "config.yaml", base_config(None))
    backend = OpenOCDBackend(load_config(config_path, work_dir=tmp_path))
    resolved = backend._resolve_executable()
    assert resolved["ok"] is True
    assert Path(resolved["executable_path"]).name.lower() in {"openocd", "openocd.bat"}


def test_debugger_not_found_when_no_config_or_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", "")
    config_path = write_config(tmp_path / ".aihil" / "config.yaml", base_config(None))
    backend = OpenOCDBackend(load_config(config_path, work_dir=tmp_path))
    result = backend.info()
    assert result["ok"] is False
    assert result["error_type"] == "debugger_not_found"
    assert result["backend_error_type"] == "openocd_not_found"


def test_openocd_debugger_type_creates_backend(tmp_path: Path) -> None:
    config_path = write_config(tmp_path / ".aihil" / "config.yaml", base_config())
    backend = create_debugger_backend(load_config(config_path, work_dir=tmp_path))
    assert isinstance(backend, OpenOCDBackend)


def test_unknown_debugger_type_is_rejected(tmp_path: Path) -> None:
    config_path = write_config(tmp_path / ".aihil" / "config.yaml", base_config(debugger_type="probe-rs"))
    with pytest.raises(ConfigError) as exc:
        create_debugger_backend(load_config(config_path, work_dir=tmp_path))
    assert exc.value.error_type == "config_invalid"
    assert exc.value.details["field"] == "debugger.type"


def test_unknown_config_field_is_rejected(tmp_path: Path) -> None:
    config_path = write_config(
        tmp_path / ".aihil" / "config.yaml",
        """
debugger:
  type: "openocd"
  unknown_field: true
""",
    )

    with pytest.raises(ConfigError) as exc:
        load_config(config_path, work_dir=tmp_path)

    assert exc.value.error_type == "config_invalid"
    assert exc.value.details["field"] == "debugger.unknown_field"


def test_config_boolean_must_be_boolean(tmp_path: Path) -> None:
    config_path = write_config(
        tmp_path / ".aihil" / "config.yaml",
        """
permissions:
  allow_flash: "false"
""",
    )

    with pytest.raises(ConfigError) as exc:
        load_config(config_path, work_dir=tmp_path)

    assert exc.value.error_type == "config_invalid"
    assert exc.value.details["field"] == "permissions.allow_flash"
