from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import BinaryIO, TextIO

from agentic_hil.config import ConfigError, load_authoritative_config
from agentic_hil.mcp import handle_mcp_message, oversized_message_response, parse_error_response
from agentic_hil.tools import AgenticHILToolService, UnprovisionedToolService
from agentic_hil.types import AgenticHILConfig

DEFAULT_MAX_MESSAGE_CHARS = 10 * 1024 * 1024
MESSAGE_OVERHEAD_CHARS = 1024 * 1024
BASE64_EXPANSION_NUMERATOR = 4
BASE64_EXPANSION_DENOMINATOR = 3


def message_size_limit(config: AgenticHILConfig) -> int:
    """Largest accepted JSON-RPC line: leaves room for a max-size artifact upload as base64.

    The number is the same one it always was, and it bounds what the host sent:
    the bytes of the line off the transport, and the characters of the line off
    a stream a caller decoded itself. The upload it is derived from is base64,
    where the two are the same count."""
    upload_chars = max(0, config.artifacts.max_upload_size_mb) * 1024 * 1024 * BASE64_EXPANSION_NUMERATOR // BASE64_EXPANSION_DENOMINATOR
    return max(DEFAULT_MAX_MESSAGE_CHARS, upload_chars + MESSAGE_OVERHEAD_CHARS)


def run_stdio_server(
    config: AgenticHILConfig | None,
    input_stream: TextIO | BinaryIO | None = None,
    output_stream: TextIO | None = None,
    max_message_chars: int | None = None,
    tools: AgenticHILToolService | UnprovisionedToolService | None = None,
) -> int:
    """Serve MCP over stdio.

    ``config`` is None only for a workspace that has no configuration yet, and
    the caller then supplies the service that may generate one. The message limit
    is the fixed default in that case: there is no configured upload size to
    derive it from, and nothing to upload either.

    A caller that passes its own streams keeps them, decoded and encoded as it
    sees fit. A caller that passes none gets the transport MCP defines, which is
    UTF-8 in both directions and is not the host's code page."""
    input_stream = input_stream if input_stream is not None else utf8_request_stream()
    output_stream = output_stream if output_stream is not None else utf8_reply_stream()
    if tools is None:
        if config is None:
            raise ValueError("run_stdio_server needs either a configuration or a prepared tool service.")
        tools = AgenticHILToolService(config, frontend="mcp")
    limit = max_message_chars or (message_size_limit(config) if config is not None else DEFAULT_MAX_MESSAGE_CHARS)
    primary_error: BaseException | None = None
    try:
        while True:
            raw_line = input_stream.readline(limit)
            if not raw_line:
                break
            if len(raw_line) >= limit and not raw_line.endswith(line_terminator(raw_line)):
                drain_oversized_line(input_stream, limit)
                write_message(output_stream, oversized_message_response(limit))
                continue
            if isinstance(raw_line, bytes):
                try:
                    raw_line = raw_line.decode("utf-8")
                except UnicodeDecodeError:
                    # Bytes that are not UTF-8 are not a message. Answering the
                    # parse error the JSON-RPC specification has for exactly this
                    # keeps the session, and keeps the host told; substituting a
                    # character for the bad byte would hand the dispatcher a
                    # value nobody sent.
                    write_message(output_stream, parse_error_response())
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


def utf8_request_stream() -> TextIO | BinaryIO:
    """The request side of the transport: the bytes behind ``sys.stdin``.

    The text layer the interpreter builds over them decodes with the process
    code page, which on Windows is an ANSI code page, so the two bytes of an
    umlaut in a UTF-8 request become two characters before the dispatcher ever
    sees the value, and nothing refuses and nothing warns (#484).

    The bytes are taken rather than the same stream reconfigured to UTF-8
    because a text layer decodes a whole buffered chunk at a time: one byte that
    is not UTF-8 raises for the entire chunk, which takes the messages that
    happened to be buffered with it and ends the session. Decoding a line at a
    time costs a malformed line a parse error and costs the session nothing.
    """
    return getattr(sys.stdin, "buffer", sys.stdin)


def utf8_reply_stream() -> TextIO:
    """The reply side: ``sys.stdout`` with its codec pinned to UTF-8.

    Pinned by reconfiguring rather than by writing bytes, so everything else
    about the stream the interpreter built stays as it was, the line terminator
    it writes included, and an ASCII session reaches the host as the same bytes
    it always did. A stream that cannot be reconfigured is left alone: a caller
    in that position passes the stream it wants.
    """
    stdout = sys.stdout
    reconfigure = getattr(stdout, "reconfigure", None)
    if reconfigure is not None:
        try:
            reconfigure(encoding="utf-8")
        except (OSError, ValueError, TypeError):
            return stdout
    return stdout


def line_terminator(raw_line: str | bytes) -> str | bytes:
    """The newline of whichever stream the line came off."""
    return b"\n" if isinstance(raw_line, bytes) else "\n"


def drain_oversized_line(input_stream: TextIO | BinaryIO, limit: int) -> None:
    while True:
        chunk = input_stream.readline(limit)
        if not chunk or chunk.endswith(line_terminator(chunk)):
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
                    "config_invalid",
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
