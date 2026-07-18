from __future__ import annotations

import json
import sys
from typing import TextIO

from agentic_hil.config import ConfigError, load_config
from agentic_hil.mcp import handle_mcp_message, oversized_message_response, parse_error_response
from agentic_hil.tools import AgenticHILToolService
from agentic_hil.types import AgenticHILConfig

DEFAULT_MAX_MESSAGE_CHARS = 10 * 1024 * 1024
MESSAGE_OVERHEAD_CHARS = 1024 * 1024
BASE64_EXPANSION_NUMERATOR = 4
BASE64_EXPANSION_DENOMINATOR = 3


def message_size_limit(config: AgenticHILConfig) -> int:
    """Largest accepted JSON-RPC line: leaves room for a max-size artifact upload as base64."""
    upload_chars = max(0, config.artifacts.max_upload_size_mb) * 1024 * 1024 * BASE64_EXPANSION_NUMERATOR // BASE64_EXPANSION_DENOMINATOR
    return max(DEFAULT_MAX_MESSAGE_CHARS, upload_chars + MESSAGE_OVERHEAD_CHARS)


def run_stdio_server(
    config: AgenticHILConfig,
    input_stream: TextIO | None = None,
    output_stream: TextIO | None = None,
    error_stream: TextIO | None = None,
    max_message_chars: int | None = None,
) -> int:
    input_stream = input_stream or sys.stdin
    output_stream = output_stream or sys.stdout
    error_stream = error_stream or sys.stderr
    limit = max_message_chars or message_size_limit(config)
    tools = AgenticHILToolService(config)
    pending_base_exception: BaseException | None = None
    cleanup_error: BaseException | None = None
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
    except BaseException as error:
        pending_base_exception = error
    finally:
        try:
            tools.close()
        except BaseException as error:
            cleanup_error = error
    if cleanup_error is not None:
        error_stream.write(
            json.dumps(
                {
                    "ok": False,
                    "tool": "mcp_stdio",
                    "error_type": "hardware_cleanup_failed",
                    "hardware_state_unconfirmed": True,
                    "exception_type": type(cleanup_error).__name__,
                    "backend_error": str(cleanup_error),
                    "summary": "MCP server shutdown could not confirm a safe hardware state.",
                }
            )
            + "\n"
        )
        error_stream.flush()
    if pending_base_exception is not None:
        raise pending_base_exception
    if cleanup_error is not None:
        if not isinstance(cleanup_error, Exception):
            raise cleanup_error
        return 1
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
