from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import BinaryIO, TextIO

from agentic_hil.config import ConfigError, load_authoritative_config
from agentic_hil.mcp import handle_mcp_message, oversized_message_response, parse_error_response
from agentic_hil.tools import AgenticHILToolService, UnprovisionedToolService
from agentic_hil.types import AgenticHILConfig, JsonObject

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
    roots = HostRoots(tools)
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
            message, answered = roots.take_answers(message)
            if answered:
                # The folder may be bound now, and a start in that folder would
                # have sized the limit from its configuration.
                if max_message_chars is None and isinstance(tools, UnprovisionedToolService) and tools.config is not None:
                    limit = message_size_limit(tools.config)
                if not roots.asking():
                    for waiting in roots.release():
                        dispatch(waiting, tools, output_stream)
            if message is None:
                continue
            if roots.asking() and needs_folder(message):
                roots.hold(message)
                continue
            dispatch(message, tools, output_stream)
            question = roots.question_after(message)
            if question is not None:
                write_message(output_stream, question)
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


def dispatch(message: object, tools: AgenticHILToolService | UnprovisionedToolService, output_stream: TextIO) -> None:
    response = handle_mcp_message(message, tools)  # type: ignore[arg-type]
    if response is not None:
        write_message(output_stream, response)


def needs_folder(message: object) -> bool:
    """Whether answering this message depends on which folder the server serves.

    Only a tool call does: every list, prompt and resource is the same in every
    folder. A batch is held whole, so its replies keep their order."""
    if isinstance(message, list):
        return any(needs_folder(item) for item in message)
    return isinstance(message, dict) and message.get("method") == "tools/call" and "id" in message


class HostRoots:
    """Asks the host which folder it has open, while the server has no project.

    A server started where there is no configuration, the home directory a
    user-wide registration starts in above all, serves the folder the host names
    in its answer to `roots/list`. It asks only a host that declared `roots` at
    initialize, only once that host has sent `notifications/initialized` (the
    first moment MCP lets a server send a request), and again on
    `notifications/roots/list_changed`, but never once a configuration is bound.

    A tool call that arrives while a question is open is held until the answer,
    because run at once it would run against the directory the server started
    in. A host that hangs up with a call still held gets no answer to it and the
    call is not run: nobody is reading the result, and a hardware call nobody
    reads is the one thing this server must not start.
    """

    ID_PREFIX = "agentic-hil-roots-"

    def __init__(self, tools: AgenticHILToolService | UnprovisionedToolService):
        self._tools = tools if isinstance(tools, UnprovisionedToolService) else None
        self._open: set[str] = set()
        self._held: list[object] = []
        self._count = 0

    def asking(self) -> bool:
        return bool(self._open)

    def hold(self, message: object) -> None:
        self._held.append(message)

    def release(self) -> list[object]:
        held, self._held = self._held, []
        return held

    def question_after(self, message: object) -> JsonObject | None:
        """The `roots/list` request this message calls for, if any."""
        tools = self._tools
        if tools is None or not isinstance(message, dict) or "id" in message:
            return None
        if message.get("method") not in ("notifications/initialized", "notifications/roots/list_changed"):
            return None
        # `config` binds a configuration the working directory has, which ends
        # the question before it is asked.
        if not tools.host_names_folders or tools.config is not None or not tools.movable:
            return None
        self._count += 1
        request_id = f"{self.ID_PREFIX}{self._count}"
        self._open.add(request_id)
        return {"jsonrpc": "2.0", "id": request_id, "method": "roots/list"}

    def take_answers(self, message: object) -> tuple[object | None, bool]:
        """Take the host's answers to this server's questions out of a message.

        Returns what is left to dispatch, None when nothing is, and whether an
        answer was taken. A batch keeps the entries that are not answers."""
        if isinstance(message, list):
            rest = [item for item in message if not self._take_answer(item)]
            if len(rest) == len(message):
                return message, False
            return rest or None, True
        if self._take_answer(message):
            return None, True
        return message, False

    def _take_answer(self, message: object) -> bool:
        """Consume the host's answer to a question this server asked, and act on it.

        An answer is never answered. One that is an error, or that names no
        single folder, leaves the workspace where it was."""
        if self._tools is None or not isinstance(message, dict) or "method" in message:
            return False
        request_id = message.get("id")
        if not isinstance(request_id, str) or request_id not in self._open:
            return False
        self._open.discard(request_id)
        result = message.get("result")
        listed = result.get("roots") if isinstance(result, dict) else None
        if isinstance(listed, list):
            uris = [root["uri"] for root in listed if isinstance(root, dict) and isinstance(root.get("uri"), str)]
            self._tools.serve_host_roots(uris)
        return True


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
