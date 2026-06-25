# Copyright 2026 Hannes Pauli
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from typing import Any

from .artifacts import ArtifactManager
from .comports import ComPortService
from .config import AIHILConfig
from .debugger import DebuggerBackend, create_debugger_backend
from .report import read_last_report


class AIHILToolService:
    def __init__(
        self,
        config: AIHILConfig,
        backend: DebuggerBackend | None = None,
        artifacts: ArtifactManager | None = None,
        com_ports: ComPortService | None = None,
    ) -> None:
        self.config = config
        self.backend = backend or create_debugger_backend(config)
        self.artifacts = artifacts or ArtifactManager(config)
        self.com_ports = com_ports or ComPortService(config)

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

    def com_ports_list(self) -> dict[str, Any]:
        return self.com_ports.list_ports()

    def com_session_start(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = payload or {}
        return self.com_ports.session_start(str(payload.get("port_id", "")), bool(payload.get("clear_buffer", True)))

    def com_session_stop(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = payload or {}
        return self.com_ports.session_stop(str(payload.get("port_id", "")))

    def com_write(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = payload or {}
        port_id = str(payload.get("port_id", ""))
        write_payload = {key: payload[key] for key in ("text", "hex") if key in payload}
        return self.com_ports.write(port_id, write_payload)

    def com_read(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = payload or {}
        return self.com_ports.read(
            str(payload.get("port_id", "")),
            payload.get("max_bytes"),
            payload.get("wait_timeout_s", 0.0),
        )

    def close(self) -> None:
        self.com_ports.close()

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
        if name == "aihil_com_ports_list":
            return self.com_ports_list()
        if name == "aihil_com_session_start":
            return self.com_session_start(arguments)
        if name == "aihil_com_session_stop":
            return self.com_session_stop(arguments)
        if name == "aihil_com_write":
            return self.com_write(arguments)
        if name == "aihil_com_read":
            return self.com_read(arguments)
        return {
            "ok": False,
            "tool": name,
            "error_type": "unknown_tool",
            "summary": "Unknown AI-HIL tool.",
        }
