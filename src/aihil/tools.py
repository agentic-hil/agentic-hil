# Copyright 2026 Hannes Pauli
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from typing import Any

from .artifacts import ArtifactManager
from .config import AIHILConfig
from .debugger import DebuggerBackend, create_debugger_backend
from .report import read_last_report


class AIHILToolService:
    def __init__(
        self,
        config: AIHILConfig,
        backend: DebuggerBackend | None = None,
        artifacts: ArtifactManager | None = None,
    ) -> None:
        self.config = config
        self.backend = backend or create_debugger_backend(config)
        self.artifacts = artifacts or ArtifactManager(config)

    def debugger_info(self) -> dict[str, Any]:
        return self.backend.info()

    def probe_target(self) -> dict[str, Any]:
        return self.backend.probe_target()

    def flash_firmware(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = payload or {}
        image_path = payload.get("image_path")
        artifact_id = payload.get("artifact_id")
        if bool(image_path) == bool(artifact_id):
            return {
                "ok": False,
                "tool": "aihil_flash_firmware",
                "error_type": "invalid_argument",
                "summary": "Provide exactly one of image_path or artifact_id.",
            }
        if image_path:
            validation = self.artifacts.validate_local_path(str(image_path))
        else:
            validation = self.artifacts.resolve_artifact_id(str(artifact_id))
        if not validation["ok"]:
            return validation
        return self.backend.flash_firmware(validation["artifact"])

    def reset_target(self, mode: str = "run") -> dict[str, Any]:
        return self.backend.reset_target(mode)

    def get_last_report(self) -> dict[str, Any]:
        report = read_last_report(self.config)
        if not report.get("ok") and report.get("error_type") in {"report_not_found", "config_invalid"}:
            return report
        return {
            "ok": True,
            "tool": "aihil_get_last_report",
            "report": report,
        }

    def classify_last_error(self) -> dict[str, Any]:
        return self.backend.classify_last_error()

    def call(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        arguments = arguments or {}
        if name == "aihil_debugger_info":
            return self.debugger_info()
        if name == "aihil_probe_target":
            return self.probe_target()
        if name == "aihil_flash_firmware":
            return self.flash_firmware(arguments)
        if name == "aihil_reset_target":
            return self.reset_target(str(arguments.get("mode", "run")))
        if name == "aihil_get_last_report":
            return self.get_last_report()
        if name == "aihil_classify_last_error":
            return self.classify_last_error()
        return {
            "ok": False,
            "tool": name,
            "error_type": "unknown_tool",
            "summary": "Unknown AI-HIL tool.",
        }
