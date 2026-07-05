from __future__ import annotations

import json
import sys
from typing import TextIO

from hardci.config import ConfigError, load_config
from hardci.mcp import handle_mcp_message, oversized_message_response, parse_error_response
from hardci.tools import HardCIToolService
from hardci.types import HardCIConfig

DEFAULT_MAX_MESSAGE_CHARS = 10 * 1024 * 1024
MESSAGE_OVERHEAD_CHARS = 1024 * 1024
BASE64_EXPANSION_NUMERATOR = 4
BASE64_EXPANSION_DENOMINATOR = 3


def message_size_limit(config: HardCIConfig) -> int:
    """Largest accepted JSON-RPC line: leaves room for a max-size artifact upload as base64."""
    upload_chars = max(0, config.artifacts.max_upload_size_mb) * 1024 * 1024 * BASE64_EXPANSION_NUMERATOR // BASE64_EXPANSION_DENOMINATOR
    return max(DEFAULT_MAX_MESSAGE_CHARS, upload_chars + MESSAGE_OVERHEAD_CHARS)


def run_stdio_server(
    config: HardCIConfig,
    input_stream: TextIO | None = None,
    output_stream: TextIO | None = None,
    max_message_chars: int | None = None,
) -> int:
    input_stream = input_stream or sys.stdin
    output_stream = output_stream or sys.stdout
    limit = max_message_chars or message_size_limit(config)
    tools = HardCIToolService(config)
    try:
        while True:
            raw_line = input_stream.readline(limit)
            if not raw_line:
                break
            if len(raw_line) >= limit and not raw_line.endswith("\n"):
                drain_oversized_line(input_stream, limit)
                write_message(output_stream, oversized_message_response(limit))
                continue
            line = raw_line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                write_message(output_stream, parse_error_response())
                continue
            response = handle_mcp_message(message, tools)
            if response is not None:
                write_message(output_stream, response)
    finally:
        tools.close()
    return 0


def drain_oversized_line(input_stream: TextIO, limit: int) -> None:
    while True:
        chunk = input_stream.readline(limit)
        if not chunk or chunk.endswith("\n"):
            return


def mcp_stdio(config_path: str | None = None) -> int:
    try:
        return run_stdio_server(load_config(config_path))
    except ConfigError as error:
        sys.stderr.write(json.dumps(error.to_dict(), indent=2) + "\n")
        return 2


def write_message(output_stream: TextIO, message: object) -> None:
    output_stream.write(json.dumps(message) + "\n")
    output_stream.flush()
