from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import TextIO

from agentic_hil.config import ConfigError, load_authoritative_config
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
    max_message_chars: int | None = None,
) -> int:
    input_stream = input_stream or sys.stdin
    output_stream = output_stream or sys.stdout
    tools = AgenticHILToolService(config, frontend="mcp")
    limit = max_message_chars or message_size_limit(tools.config)
    primary_error: BaseException | None = None
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
                message = json.loads(line, parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
            except (json.JSONDecodeError, ValueError):
                write_message(output_stream, parse_error_response())
                continue
            response = handle_mcp_message(message, tools)
            if response is not None:
                write_message(output_stream, response)
    except BaseException as error:
        primary_error = error
    cleanup_error: BaseException | None = None
    try:
        tools.close()
    except BaseException as error:
        cleanup_error = error
    if primary_error is not None:
        if cleanup_error is not None:
            primary_error.args = (*primary_error.args, f"Cleanup error: {type(cleanup_error).__name__}: {cleanup_error}")
        raise primary_error
    if cleanup_error is not None:
        raise cleanup_error
    return 0


def drain_oversized_line(input_stream: TextIO, limit: int) -> None:
    while True:
        chunk = input_stream.readline(limit)
        if not chunk or chunk.endswith("\n"):
            return


def mcp_stdio(config_path: str | None = None) -> int:
    try:
        workspace = Path.cwd().resolve()
        config = load_authoritative_config(workspace)
        if config_path is not None:
            requested = Path(config_path).expanduser()
            requested = (requested if requested.is_absolute() else workspace / requested).resolve()
            if requested != Path(config.config_path):
                raise ConfigError(
                    "config_migration_required",
                    "Explicit config paths cannot override the authoritative Agentic HIL policy. Use AGENTIC_HIL_CONFIG with an absolute external path.",
                    {"selected_path": str(requested), "authoritative_path": config.config_path},
                )
        return run_stdio_server(config)
    except ConfigError as error:
        sys.stderr.write(json.dumps(error.to_dict(), indent=2) + "\n")
        return 2


def write_message(output_stream: TextIO, message: object) -> None:
    output_stream.write(json.dumps(message) + "\n")
    output_stream.flush()
