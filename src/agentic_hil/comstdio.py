from __future__ import annotations

import json
import os
import queue
import select
import sys
import threading
import time
from contextlib import suppress
from dataclasses import dataclass
from typing import BinaryIO, TextIO

from agentic_hil.comports import ComPortService
from agentic_hil.config import ConfigError
from agentic_hil.redact import redact_sensitive
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
    owns_fd: bool


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
        # An interrupt from the reader teardown must not skip the session/service
        # cleanup below; record it and re-raise only after everything has run.
        try:
            cleanup_errors.extend(stop_stdin_reader(stdin_reader, max(0.1, eof_idle_timeout_s)))
        except BaseException as error:
            cleanup_errors.append(error)
            if primary_error is None and isinstance(error, (KeyboardInterrupt, SystemExit)):
                primary_error = error
    if started_ok:
        try:
            stopped = service.session_stop(port_id)
            if not overall_success(stopped):
                failed = True
                write_error(error_stream, stopped)
        except BaseException as error:
            cleanup_errors.append(error)
            if primary_error is None and isinstance(error, (KeyboardInterrupt, SystemExit)):
                primary_error = error
    try:
        service.close()
    except BaseException as error:
        cleanup_errors.append(error)
        if primary_error is None and isinstance(error, (KeyboardInterrupt, SystemExit)):
            primary_error = error
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
    owns_fd = owned_fd[0] is not None

    def pump() -> None:
        try:
            while not stop.is_set():
                with fd_lock:
                    descriptor = owned_fd[0]
                if descriptor is not None and os.name != "nt":
                    # Poll with a short timeout so a shutdown observed via `stop`
                    # unblocks the reader instead of leaving it parked in a
                    # blocking read that closing the fd does not interrupt.
                    ready, _, _ = select.select([descriptor], [], [], STDIN_POLL_TIMEOUT_S)
                    if not ready:
                        continue
                    data = os.read(descriptor, STDIN_CHUNK_BYTES)
                elif descriptor is not None:
                    # The same poll on Windows, where `select` takes sockets
                    # only. The read below is entered when the poll says a
                    # byte or an EOF is there to collect, so a shutdown is
                    # observed within the poll interval rather than when the
                    # operator presses Enter or the pipe closes (#487).
                    if not windows_stdin_ready(descriptor, STDIN_POLL_TIMEOUT_S):
                        continue
                    data = os.read(descriptor, STDIN_CHUNK_BYTES)
                else:
                    data = input_stream.read1(STDIN_CHUNK_BYTES) if hasattr(input_stream, "read1") else input_stream.read(STDIN_CHUNK_BYTES)
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
    try:
        thread.start()
    except BaseException:
        # A failed start must not leak the thread or its dup fd: signal stop so a
        # pathologically-started thread self-terminates, and close the fd here in
        # case the thread never ran its own finally.
        stop.set()
        close_owned_stdin_fd(owned_fd, fd_lock)
        raise
    return StdinReader(messages, thread, stop, input_stream, owned_fd, fd_lock, owns_fd)


def stop_stdin_reader(reader: StdinReader, timeout_s: float) -> list[BaseException]:
    errors: list[BaseException] = []
    reader.stop.set()
    reader.thread.join(timeout=min(0.05, timeout_s))
    if reader.thread.is_alive():
        try:
            if reader.owns_fd and os.name == "nt":
                # Not the close. On Windows the C runtime serialises every call
                # on a descriptor, so closing one under a read waits for that
                # read to return, and the command that came here to end hung
                # until the operator pressed Enter or the pipe closed (#487).
                # The reader polls, so it is in a read only while something is
                # there to collect or a console line is being typed; that read
                # is cancelled here, and the reader closes its own descriptor
                # as it unwinds. A read that cannot be cancelled is reported
                # below as a thread that remained, never waited on.
                cancel_windows_synchronous_io(reader.thread)
            elif reader.owns_fd:
                # The reader closes its own dup'd descriptor as it unwinds, so an
                # already emptied slot here means the read is ending on its own,
                # not that the stream was borrowed without a way to cancel it.
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


# GetFileType's answers for the handles a stdin can be, and the two answers
# WaitForSingleObject gives that mean "not yet".
_FILE_TYPE_CHAR = 0x0002
_FILE_TYPE_PIPE = 0x0003
_WAIT_TIMEOUT = 0x00000102
_THREAD_TERMINATE = 0x0001


def windows_stdin_ready(descriptor: int, timeout_s: float) -> bool:
    """Whether a read on `descriptor` returns now, having waited at most `timeout_s`.

    The Windows half of the reader's poll. POSIX asks `select`, which answers
    for every kind of descriptor; Windows answers per kind. A pipe, which is
    what a parent process or a shell redirection hands the bridge, is asked
    with `PeekNamedPipe` how many bytes wait in it: nothing yet is a short
    sleep and "not ready", a broken pipe is "ready" because the read that
    follows returns the EOF the closed writer means. A character device, the
    console, is waited on with `WaitForSingleObject`, which the console signals
    when input records are there to read. Anything else is a file, and a read
    of a file never blocks.
    """
    import ctypes
    import msvcrt
    from ctypes import wintypes

    handle = wintypes.HANDLE(msvcrt.get_osfhandle(descriptor))
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetFileType.argtypes = [wintypes.HANDLE]
    kernel32.GetFileType.restype = wintypes.DWORD
    file_type = kernel32.GetFileType(handle)
    if file_type == _FILE_TYPE_PIPE:
        kernel32.PeekNamedPipe.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD)]
        kernel32.PeekNamedPipe.restype = wintypes.BOOL
        available = wintypes.DWORD(0)
        if not kernel32.PeekNamedPipe(handle, None, 0, None, ctypes.byref(available), None) or available.value:
            return True
        time.sleep(timeout_s)
        return False
    if file_type == _FILE_TYPE_CHAR:
        kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        return kernel32.WaitForSingleObject(handle, int(timeout_s * 1000)) != _WAIT_TIMEOUT
    return True


def cancel_windows_synchronous_io(thread: threading.Thread) -> None:
    """End the read `thread` is in, if it is in one, without touching its descriptor.

    `CancelSynchronousIo` marks the pending synchronous I/O of one thread as
    cancelled, and the read returns to the reader as an error it treats as its
    end. Nothing is reported from here: a thread that was not in a read has
    nothing to cancel and ends on the next poll, and one whose read could not
    be cancelled is the thread the caller goes on to wait for and to report.
    """
    import ctypes
    from ctypes import wintypes

    native_id = thread.native_id
    if native_id is None:
        return
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenThread.restype = wintypes.HANDLE
    kernel32.CancelSynchronousIo.argtypes = [wintypes.HANDLE]
    kernel32.CancelSynchronousIo.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenThread(_THREAD_TERMINATE, False, native_id)
    if not handle:
        return
    try:
        kernel32.CancelSynchronousIo(handle)
    finally:
        kernel32.CloseHandle(handle)


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
    output.write(json.dumps(redact_sensitive(result)) + "\n")
    output.flush()
