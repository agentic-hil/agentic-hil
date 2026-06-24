# Copyright 2026 Hannes Pauli
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from typing import Any

from .config import AIHILConfig, ConfigError


class DebuggerBackend:
    def info(self) -> dict[str, Any]:
        raise NotImplementedError

    def probe_target(self) -> dict[str, Any]:
        raise NotImplementedError

    def flash_firmware(self, artifact: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    def reset_target(self, mode: str = "run") -> dict[str, Any]:
        raise NotImplementedError

    def classify_last_error(self) -> dict[str, Any]:
        raise NotImplementedError


def create_debugger_backend(config: AIHILConfig) -> DebuggerBackend:
    if config.debugger.type == "openocd":
        from .debuggers.openocd import OpenOCDBackend

        return OpenOCDBackend(config)
    raise ConfigError(
        "config_invalid",
        "Unsupported debugger.type.",
        field="debugger.type",
        value=config.debugger.type,
        allowed_values=["openocd"],
    )
