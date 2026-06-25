# Copyright 2026 Hannes Pauli
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
from typing import Any, TextIO

from .config import AIHILConfig
from .mcp import handle_mcp_message, parse_error_response
from .tools import AIHILToolService


def run_stdio_server(config: AIHILConfig, stdin: TextIO, stdout: TextIO) -> int:
    tools = AIHILToolService(config)
    try:
        for raw_line in stdin:
            line = raw_line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
                _write_message(stdout, parse_error_response())
                continue

            response = handle_mcp_message(message, tools)
            if response is not None:
                _write_message(stdout, response)
    finally:
        tools.close()
    return 0


def _write_message(stdout: TextIO, message: Any) -> None:
    stdout.write(json.dumps(message, ensure_ascii=False, separators=(",", ":")))
    stdout.write("\n")
    stdout.flush()
