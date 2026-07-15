from __future__ import annotations

import json
import queue
import sys
import threading
import time
from typing import BinaryIO, TextIO

from agentic_hil.comports import ComPortService
from agentic_hil.hardware_lock import HardwareLockError, ProjectHardwareLock
from agentic_hil.types import AgenticHILConfig, JsonObject

STDIN_CHUNK_BYTES = 4096
STDIN_POLL_TIMEOUT_S = 0.01


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
    hardware_lock = ProjectHardwareLock(config.config_path)
    try:
        acquired = hardware_lock.acquire()
    except HardwareLockError as error:
        write_error(error_stream, {"ok": False, "tool": "com_stdio", "error_type": "hardware_lock_failed", "summary": "Project hardware lease could not be acquired.", "backend_error": str(error)})
        return 1
    if not acquired:
        write_error(error_stream, {"ok": False, "tool": "com_stdio", "error_type": "hardware_busy", "summary": "Project hardware is in use by another Agentic HIL process."})
        return 1
    service: ComPortService | None = None
    started_ok = False
    failed = False
    try:
        service = ComPortService(config)
        started = service.session_start(port_id, True)
        if not started.get("ok"):
            write_error(error_stream, started)
            return 1
        started_ok = True
        port = config.com_ports[port_id]
        read_size = max_read_bytes or port.max_buffer_bytes
        last_data_at = time.monotonic()
        input_stream_closed = False
        stdin_chunks = start_stdin_reader(input_stream)
        while not failed:
            chunk = next_stdin_chunk(stdin_chunks)
            if chunk:
                written = service.write_bytes(port_id, chunk, "com_stdio_write")
                if not written.get("ok"):
                    failed = True
                    write_error(error_stream, written)
            elif chunk == b"":
                input_stream_closed = True
            result = service.read_bytes(port_id, read_size, read_wait_timeout_s, "com_stdio_read")
            if not result.get("ok"):
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
        return 1 if failed else 0
    finally:
        try:
            if service is not None:
                if started_ok:
                    service.session_stop(port_id)
                service.close()
        finally:
            hardware_lock.release()


def start_stdin_reader(input_stream: BinaryIO) -> queue.Queue[bytes]:
    """Read stdin on a daemon thread so a blocked terminal read cannot stall serial output relaying."""
    chunks: queue.Queue[bytes] = queue.Queue()

    def pump() -> None:
        while True:
            data = input_stream.read1(STDIN_CHUNK_BYTES) if hasattr(input_stream, "read1") else input_stream.read(STDIN_CHUNK_BYTES)
            chunks.put(bytes(data))
            if not data:
                return

    threading.Thread(target=pump, daemon=True).start()
    return chunks


def next_stdin_chunk(chunks: queue.Queue[bytes]) -> bytes | None:
    """Return the next stdin chunk, b"" on EOF, or None when nothing is pending."""
    try:
        return chunks.get(timeout=STDIN_POLL_TIMEOUT_S)
    except queue.Empty:
        return None


def write_error(output: TextIO, result: JsonObject) -> None:
    output.write(json.dumps(result) + "\n")
    output.flush()
