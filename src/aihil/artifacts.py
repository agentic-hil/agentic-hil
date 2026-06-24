# Copyright 2026 Hannes Pauli
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from .config import AIHILConfig, display_path, resolve_work_path


class ArtifactManager:
    def __init__(self, config: AIHILConfig) -> None:
        self.config = config

    def validate_local_path(self, image_path: str) -> dict[str, Any]:
        requested = Path(image_path)
        resolved = resolve_work_path(self.config, requested)
        validation: dict[str, Any] = {
            "path_traversal_safe": ".." not in requested.parts,
            "exists": resolved.exists(),
            "allowed_root": self._is_under_allowed_roots(resolved),
            "allowed_extension": resolved.suffix.lower() in self.config.artifacts.allowed_extensions,
            "sha256_computed": False,
        }
        validation["require_allowed_root"] = validation["allowed_root"]

        if not validation["path_traversal_safe"]:
            return self._validation_error(
                "Firmware artifact path contains traversal segments.",
                validation,
            )
        if self.config.validation.require_existing_file and not validation["exists"]:
            return self._validation_error("Firmware artifact does not exist.", validation, "artifact_not_found")
        if self.config.validation.require_allowed_root and not validation["allowed_root"]:
            return self._validation_error("Firmware artifact is outside allowed artifact roots.", validation)
        if self.config.validation.require_allowed_extension and not validation["allowed_extension"]:
            return self._validation_error("Firmware artifact extension is not allowed.", validation)

        sha256 = None
        size_bytes = None
        if validation["exists"]:
            size_bytes = resolved.stat().st_size
            if self.config.validation.compute_sha256:
                sha256 = _sha256(resolved)
                validation["sha256_computed"] = True
            if self.config.validation.inspect_known_formats:
                validation.update(self._inspect_format(resolved))

        failed_plausibility = [
            key for key in ("elf_header", "hex_parseable", "bin_size_plausible") if validation.get(key) is False
        ]
        if failed_plausibility:
            return self._validation_error(
                "Firmware artifact failed basic format plausibility checks.",
                validation,
            )

        return {
            "ok": True,
            "artifact": {
                "source": "path",
                "path": display_path(self.config, requested),
                "resolved_path": str(resolved),
                "sha256": sha256,
                "size_bytes": size_bytes,
                "validation": validation,
            },
            "validation": validation,
        }

    def resolve_artifact_id(self, artifact_id: str) -> dict[str, Any]:
        return {
            "ok": False,
            "tool": "aihil_flash_firmware",
            "error_type": "artifact_not_found",
            "summary": "Uploaded artifact could not be found.",
            "artifact_id": artifact_id,
        }

    def _validation_error(
        self,
        summary: str,
        validation: dict[str, Any],
        error_type: str = "artifact_validation_failed",
    ) -> dict[str, Any]:
        return {
            "ok": False,
            "tool": "aihil_flash_firmware",
            "error_type": error_type,
            "summary": summary,
            "validation": validation,
        }

    def _is_under_allowed_roots(self, path: Path) -> bool:
        for root in self.config.artifacts.allowed_roots:
            root_path = resolve_work_path(self.config, root)
            if _is_relative_to(path, root_path):
                return True
        return False

    def _inspect_format(self, path: Path) -> dict[str, Any]:
        suffix = path.suffix.lower()
        if suffix == ".elf":
            try:
                with path.open("rb") as handle:
                    return {"elf_header": handle.read(4) == b"\x7fELF"}
            except OSError:
                return {"elf_header": False}
        if suffix == ".hex":
            return {"hex_parseable": _looks_like_intel_hex(path)}
        if suffix == ".bin":
            try:
                return {"bin_size_plausible": path.stat().st_size > 0}
            except OSError:
                return {"bin_size_plausible": False}
        return {}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _looks_like_intel_hex(path: Path) -> bool:
    try:
        lines = [line.strip() for line in path.read_text(encoding="ascii").splitlines() if line.strip()]
    except (OSError, UnicodeDecodeError):
        return False
    if not lines:
        return False
    for line in lines:
        if not line.startswith(":"):
            return False
        payload = line[1:]
        if len(payload) < 10 or len(payload) % 2 != 0:
            return False
        try:
            data = bytes.fromhex(payload)
        except ValueError:
            return False
        byte_count = data[0]
        if len(data) != byte_count + 5:
            return False
        if sum(data) & 0xFF:
            return False
    return True


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False
