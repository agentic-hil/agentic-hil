# Copyright 2026 Hannes Pauli
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import hashlib
from pathlib import Path

from aihil.artifacts import ArtifactManager
from aihil.config import load_config


def write_config(tmp_path: Path) -> Path:
    path = tmp_path / ".aihil" / "config.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        """
debugger:
  type: "openocd"
  executable: "tests/fixtures/fake_openocd.py"
artifacts:
  allowed_roots: ["build"]
  allowed_extensions: [".elf", ".hex", ".bin"]
validation:
  inspect_known_formats: true
""",
        encoding="utf-8",
    )
    return path


def test_local_path_artifact_is_accepted_and_sha256_is_computed(tmp_path: Path) -> None:
    config = load_config(write_config(tmp_path), work_dir=tmp_path)
    firmware = tmp_path / "build" / "firmware.elf"
    firmware.parent.mkdir()
    data = b"\x7fELFfake"
    firmware.write_bytes(data)

    result = ArtifactManager(config).validate_local_path("build/firmware.elf")

    assert result["ok"] is True
    assert result["artifact"]["sha256"] == hashlib.sha256(data).hexdigest()
    assert result["validation"]["sha256_computed"] is True


def test_local_path_outside_allowed_roots_is_blocked(tmp_path: Path) -> None:
    config = load_config(write_config(tmp_path), work_dir=tmp_path)
    firmware = tmp_path / "other" / "firmware.elf"
    firmware.parent.mkdir()
    firmware.write_bytes(b"\x7fELFfake")

    result = ArtifactManager(config).validate_local_path("other/firmware.elf")

    assert result["ok"] is False
    assert result["error_type"] == "artifact_validation_failed"
    assert result["validation"]["allowed_root"] is False


def test_local_path_extension_must_be_allowed(tmp_path: Path) -> None:
    config = load_config(write_config(tmp_path), work_dir=tmp_path)
    firmware = tmp_path / "build" / "firmware.txt"
    firmware.parent.mkdir()
    firmware.write_text("not firmware", encoding="utf-8")

    result = ArtifactManager(config).validate_local_path("build/firmware.txt")

    assert result["ok"] is False
    assert result["error_type"] == "artifact_validation_failed"
    assert result["validation"]["allowed_extension"] is False
