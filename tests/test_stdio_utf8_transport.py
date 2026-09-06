"""The MCP stdio transport is UTF-8 in both directions, whatever the host's code page.

MCP over stdio carries JSON-RPC as UTF-8. A server that reads its requests
through the text layer the interpreter builds from the process code page reads
them through whatever table the host happens to be configured for, and on
Windows that is an ANSI code page: the two bytes of an umlaut arrive as two
characters, nothing refuses and nothing warns, and the corrupted value is
written into a configuration or matched against a path that does not exist.

The values here are spelled with escapes so this file stays ASCII: what is
under test is the encoding of a stream, and a test that proved it while
depending on how an editor saved this file would prove nothing.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import write_config

from agentic_hil.config import load_config
from agentic_hil.stdio import run_stdio_server

# The code page a German or Western European Windows host runs with, and the one
# the defect was measured on. Every character of it is a single byte, so a UTF-8
# request decoded through it is silently corrupted rather than refused.
ANSI_CODE_PAGE = "cp1252"

JSONRPC_PARSE_ERROR = -32700

# "Messgeraet" and "Pruefstand" spelled the way an operator spells them, in a
# resource URI, because a URI is echoed back verbatim in the reply and so proves
# both directions of the transport with one request.
NON_ASCII_URI = "agentic-hil://bench/Messger\u00e4t/Pr\u00fcfstand"
ASCII_URI = "agentic-hil://bench/plain"
NON_ASCII_SUMMARY = "Der Pr\u00fcfstand meldet: Messger\u00e4t nicht bereit."

# A line whose bytes are not valid UTF-8 at all. Both bytes are printable
# characters in cp1252, which is exactly why the wrong table is dangerous: the
# request parses and is answered instead of being refused.
INVALID_UTF8_LINE = b'{"jsonrpc": "2.0", "id": 2, "method": "resources/read", "params": {"uri": "\xff\xfe"}}\n'


def request_bytes(request_id: int, uri: str) -> bytes:
    """One JSON-RPC line the way a host writes it: UTF-8, unescaped, newline terminated."""
    body = {"jsonrpc": "2.0", "id": request_id, "method": "resources/read", "params": {"uri": uri}}
    return json.dumps(body, ensure_ascii=False).encode("utf-8") + b"\n"


def ansi_stdin(payload: bytes) -> io.TextIOWrapper:
    """A stdin like the one the interpreter builds on a host with an ANSI code page:
    a byte pipe behind a text layer whose codec is not UTF-8."""
    return io.TextIOWrapper(io.BytesIO(payload), encoding=ANSI_CODE_PAGE, errors="strict")


def ansi_stdout() -> tuple[io.TextIOWrapper, io.BytesIO]:
    """The same for stdout, with the bytes that reach the host kept for inspection.

    The wrapper is built the way the interpreter builds the real one, newline
    translation included, so the bytes this test reads are the bytes a host
    reads.
    """
    written = io.BytesIO()
    return io.TextIOWrapper(written, encoding=ANSI_CODE_PAGE, errors="strict"), written


def child_environment() -> dict[str, str]:
    """The interpreter's own stream defaults, with everything that would paper over them cleared.

    On Windows the default is the ANSI code page. On POSIX the C locale is asked
    for and its coercion switched off, so the default there is not UTF-8 either
    and the same test measures the same thing on both.
    """
    environment = {
        name: value
        for name, value in os.environ.items()
        if name not in ("PYTHONUTF8", "PYTHONIOENCODING", "PYTHONLEGACYWINDOWSSTDIO")
    }
    if os.name != "nt":
        environment["PYTHONCOERCECLOCALE"] = "0"
        environment["LC_ALL"] = "C"
        environment["LANG"] = "C"
    return environment


class RefusedStream:
    """A process stream that fails the test if the server reaches for it at all."""

    def __getattr__(self, name: str) -> object:
        raise AssertionError(f"the server touched the process stream ({name!r}) although the caller passed its own")


def test_a_non_ascii_argument_arrives_intact_on_a_host_whose_streams_are_the_ansi_code_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The read direction. A workspace path, a description or a firmware name can
    carry any character an operator types, and the transport is defined as UTF-8,
    so the value the dispatcher sees is the value the host sent."""
    config = load_config(str(write_config(tmp_path)))
    stdin = ansi_stdin(request_bytes(1, NON_ASCII_URI))
    stdout, written = ansi_stdout()
    monkeypatch.setattr(sys, "stdin", stdin)
    monkeypatch.setattr(sys, "stdout", stdout)

    exit_code = run_stdio_server(config)

    assert exit_code == 0
    reply = json.loads(written.getvalue().decode("utf-8").splitlines()[0])
    assert reply["error"]["data"]["uri"] == NON_ASCII_URI


def test_the_reply_stream_carries_a_character_the_host_code_page_cannot_encode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The write direction. A summary or a path with a non-ASCII character meets a
    stdout that cannot encode it and the server raises where it should answer, so
    the transport pins the stream it writes through to UTF-8 as well."""
    config = load_config(str(write_config(tmp_path)))
    stdout, written = ansi_stdout()
    monkeypatch.setattr(sys, "stdin", ansi_stdin(b""))
    monkeypatch.setattr(sys, "stdout", stdout)

    exit_code = run_stdio_server(config)

    assert exit_code == 0
    bound = sys.stdout
    assert bound.encoding.lower().replace("-", "") == "utf8", f"the transport writes through {bound.encoding}"
    bound.write(NON_ASCII_SUMMARY + "\n")
    bound.flush()
    assert written.getvalue().decode("utf-8").splitlines()[-1] == NON_ASCII_SUMMARY


def test_a_line_that_is_not_utf8_is_answered_as_a_parse_error_and_the_session_survives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A byte sequence that is not UTF-8 is a protocol error, answered as one. It is
    never a substituted character, and it never costs the messages around it: the
    request before it and the request after it are answered."""
    config = load_config(str(write_config(tmp_path)))
    stdin = ansi_stdin(request_bytes(1, ASCII_URI) + INVALID_UTF8_LINE + request_bytes(3, ASCII_URI))
    stdout, written = ansi_stdout()
    monkeypatch.setattr(sys, "stdin", stdin)
    monkeypatch.setattr(sys, "stdout", stdout)

    exit_code = run_stdio_server(config)

    assert exit_code == 0
    replies = [json.loads(line) for line in written.getvalue().decode("utf-8").splitlines()]
    assert [reply.get("id") for reply in replies] == [1, None, 3]
    assert replies[1]["error"]["code"] == JSONRPC_PARSE_ERROR


def test_an_ascii_session_reaches_the_host_as_the_same_bytes_it_reached_it_as_before(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The neighbour every host in the field is already running on. UTF-8 and the
    ANSI code pages agree on every ASCII character, and pinning the codec is not
    licence to change the framing: the line terminator a host reads is the one the
    interpreter's own stdout writes."""
    config = load_config(str(write_config(tmp_path)))
    stdout, written = ansi_stdout()
    monkeypatch.setattr(sys, "stdin", ansi_stdin(b'{"jsonrpc": "2.0", "id": 1, "method": "ping"}\n'))
    monkeypatch.setattr(sys, "stdout", stdout)

    exit_code = run_stdio_server(config)

    assert exit_code == 0
    expected = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {}}).encode("ascii") + os.linesep.encode("ascii")
    assert written.getvalue() == expected


def test_a_caller_that_passes_its_own_streams_keeps_them(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The other neighbour: the embedded caller. A caller that hands the server two
    streams has decoded and will encode them itself, and the server takes them as
    they are instead of reaching for the process streams behind them."""
    config = load_config(str(write_config(tmp_path)))
    monkeypatch.setattr(sys, "stdin", RefusedStream())
    monkeypatch.setattr(sys, "stdout", RefusedStream())
    output = io.StringIO()

    exit_code = run_stdio_server(
        config,
        input_stream=io.StringIO(request_bytes(1, NON_ASCII_URI).decode("utf-8")),
        output_stream=output,
    )

    assert exit_code == 0
    assert json.loads(output.getvalue())["error"]["data"]["uri"] == NON_ASCII_URI


def test_the_shipped_server_round_trips_a_non_ascii_value_through_real_byte_pipes(tmp_path: Path) -> None:
    """The whole thing as a host runs it: the installed entry point, two real byte
    pipes, and no encoding variable set to rescue it. Nothing but JSON-RPC frames
    reaches stdout, which is the channel the host parses."""
    finished = subprocess.run(
        [sys.executable, "-m", "agentic_hil", "mcp-stdio"],
        input=request_bytes(1, NON_ASCII_URI),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=tmp_path,
        env=child_environment(),
        timeout=180,
        check=False,
    )

    assert finished.returncode == 0, finished.stderr.decode("utf-8", "replace")
    assert finished.stderr == b"", finished.stderr.decode("utf-8", "replace")
    reply = json.loads(finished.stdout.decode("utf-8").splitlines()[0])
    assert reply["error"]["data"]["uri"] == NON_ASCII_URI
