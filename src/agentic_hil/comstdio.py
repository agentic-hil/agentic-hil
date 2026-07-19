from __future__ import annotations

import json
import os
import queue
import sys
import threading
import time
from contextlib import suppress
from dataclasses import dataclass
from typing import BinaryIO, TextIO

from agentic_hil.comports import ComPortService
from agentic_hil.config import ConfigError
from agentic_hil.report import audit_unavailable, ensure_audit_ready, overall_success
from agentic_hil.types import AgenticHILConfig, JsonObject

STDIN_CHUNK_BYTES = 4096
STDIN_POLL_TIMEOUT_S = 0.01


@dataclass
class StdinReader:
    messages: queue.Queue[tuple[str, bytes | BaseException | None]]
    thread: threading.Thread
    stop: threading.Event
    input_stream: BinaryIO
    owned_fd: list[int | None]
    fd_lock: threading.Lock


def run_com_stdio(
    config: AgenticHILConfig,
    port_id: str,
    input_stream: BinaryIO | None = None,
    output_stream: TextIO | None = None,
    error_stream: TextIO | None = None,
    max_read_bytes: int | None = None,
    read_wait_timeout_s: float = 0.05,
    eof_idle_timeout_s: float = 0.5,
) -> int:
    input_stream = input_stream or sys.stdin.buffer
    output_stream = output_stream or sys.stdout
    error_stream = error_stream or sys.stderr
    service = ComPortService(config)
    started_ok = False
    failed = False
    primary_error: BaseException | None = None
    stdin_reader: StdinReader | None = None
    try:
        try:
            ensure_audit_ready(config)
        except (ConfigError, OSError) as error:
            started = audit_unavailable("com_session_start", error)
        else:
            started = service.session_start(port_id, True)
        if not overall_success(started):
            write_error(error_stream, started)
            failed = True
        else:
            started_ok = True
            port = config.com_ports[port_id]
            read_size = max_read_bytes or port.max_buffer_bytes
            last_data_at = time.monotonic()
            input_stream_closed = False
            stdin_reader = start_stdin_reader(input_stream)
            while not failed:
                kind, payload = next_stdin_chunk(stdin_reader)
                if kind == "error":
                    assert isinstance(payload, BaseException)
                    raise payload
                chunk = payload if kind == "data" else b"" if kind == "eof" else None
                if chunk:
                    written = service.write_bytes(port_id, chunk, "com_stdio_write")
                    if not overall_success(written):
                        failed = True
                        write_error(error_stream, written)
                elif chunk == b"":
                    input_stream_closed = True
                result = service.read_bytes(port_id, read_size, read_wait_timeout_s, "com_stdio_read")
                if not overall_success(result):
                    failed = True
                    write_error(error_stream, result)
                    break
                if int(result.get("bytes_read", 0)) > 0:
                    output_stream.write(str(result["data"].get("text", "")))
                    output_stream.flush()
                    last_data_at = time.monotonic()
                    continue
                if input_stream_closed and time.monotonic() - last_data_at >= eof_idle_timeout_s:
                    break
    except BaseException as error:
        primary_error = error
    cleanup_errors: list[BaseException] = []
    if stdin_reader is not None:
        cleanup_errors.extend(stop_stdin_reader(stdin_reader, max(0.1, eof_idle_timeout_s)))
    if started_ok:
        try:
            stopped = service.session_stop(port_id)
            if not overall_success(stopped):
                failed = True
                write_error(error_stream, stopped)
        except BaseException as error:
            cleanup_errors.append(error)
    try:
        service.close()
    except BaseException as error:
        cleanup_errors.append(error)
    if primary_error is not None:
        if cleanup_errors:
            primary_error.args = (*primary_error.args, "Cleanup errors: " + "; ".join(f"{type(error).__name__}: {error}" for error in cleanup_errors))
        raise primary_error
    if cleanup_errors:
        raise RuntimeError("COM stdio cleanup failed: " + "; ".join(f"{type(error).__name__}: {error}" for error in cleanup_errors)) from cleanup_errors[0]
    return 1 if failed else 0


def start_stdin_reader(input_stream: BinaryIO) -> StdinReader:
    """Read stdin on a daemon thread so a blocked terminal read cannot stall serial output relaying."""
    messages: queue.Queue[tuple[str, bytes | BaseException | None]] = queue.Queue()
    stop = threading.Event()
    owned_fd: list[int | None] = [None]
    fd_lock = threading.Lock()
    with suppress(AttributeError, OSError, TypeError, ValueError):
        owned_fd[0] = os.dup(input_stream.fileno())

    def pump() -> None:
        try:
            while not stop.is_set():
                with fd_lock:
                    descriptor = owned_fd[0]
                data = os.read(descriptor, STDIN_CHUNK_BYTES) if descriptor is not None else input_stream.read1(STDIN_CHUNK_BYTES) if hasattr(input_stream, "read1") else input_stream.read(STDIN_CHUNK_BYTES)
                if stop.is_set():
                    return
                if not data:
                    messages.put(("eof", None))
                    return
                messages.put(("data", bytes(data)))
        except BaseException as error:
            if not stop.is_set():
                messages.put(("error", error))
        finally:
            close_owned_stdin_fd(owned_fd, fd_lock)

    thread = threading.Thread(target=pump, daemon=True)
    thread.start()
    return StdinReader(messages, thread, stop, input_stream, owned_fd, fd_lock)


def stop_stdin_reader(reader: StdinReader, timeout_s: float) -> list[BaseException]:
    errors: list[BaseException] = []
    reader.stop.set()
    reader.thread.join(timeout=min(0.05, timeout_s))
    if reader.thread.is_alive():
        try:
            if reader.owned_fd[0] is not None:
                close_owned_stdin_fd(reader.owned_fd, reader.fd_lock)
            else:
                cancel_read = getattr(reader.input_stream, "cancel_read", None)
                if not callable(cancel_read):
                    raise RuntimeError("Borrowed stdin stream has no cancellable read interface.")
                cancel_read()
        except BaseException as error:
            errors.append(error)
        reader.thread.join(timeout=timeout_s)
    if reader.thread.is_alive():
        errors.append(RuntimeError("COM stdio stdin reader remained blocked during shutdown."))
    return errors


def close_owned_stdin_fd(owned_fd: list[int | None], lock: threading.Lock) -> None:
    with lock:
        descriptor = owned_fd[0]
        owned_fd[0] = None
    if descriptor is not None:
        os.close(descriptor)


def next_stdin_chunk(reader: StdinReader) -> tuple[str, bytes | BaseException | None]:
    try:
        return reader.messages.get(timeout=STDIN_POLL_TIMEOUT_S)
    except queue.Empty:
        return "pending", None


def write_error(output: TextIO, result: JsonObject) -> None:
    output.write(json.dumps(result) + "\n")
    output.flush()
