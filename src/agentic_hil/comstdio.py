from __future__ import annotations

import json
import queue
import sys
import threading
import time
from typing import BinaryIO, TextIO

from agentic_hil.comports import ComPortService
from agentic_hil.hardware_lock import HardwareLockError, HardwareQuarantinedError, ProjectHardwareLock
from agentic_hil.report import utc_now_iso
from agentic_hil.tools import is_confirmed_audit_error
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
    try:
        hardware_lock = ProjectHardwareLock(config.config_path)
        acquired = hardware_lock.acquire(source="com_stdio")
    except HardwareQuarantinedError as error:
        write_error(error_stream, {"ok": False, "tool": "com_stdio", "error_type": "hardware_state_unconfirmed", "summary": "Project hardware state is unconfirmed after an incomplete cleanup.", "quarantine": error.details})
        return 1
    except HardwareLockError as error:
        write_error(error_stream, {"ok": False, "tool": "com_stdio", "error_type": "hardware_lock_failed", "summary": "Project hardware lease could not be acquired.", "backend_error": str(error)})
        return 1
    if not acquired:
        write_error(error_stream, {"ok": False, "tool": "com_stdio", "error_type": "hardware_busy", "summary": "Project hardware is in use by another Agentic HIL process."})
        return 1
    service: ComPortService | None = None
    started_ok = False
    failed = False
    exit_code = 0
    error_result: JsonObject | None = None
    cleanup_error: Exception | None = None
    pending_base_exception: BaseException | None = None
    hardware_state: JsonObject = {"active": False, "active_resources": [], "inspection_errors": []}
    quarantine: JsonObject | None = None
    write_in_flight: JsonObject | None = None
    unconfirmed_operations: list[JsonObject] = []
    try:
        service = ComPortService(config)
        started = service.session_start(port_id, True)
        if not started.get("ok"):
            error_result = started
            exit_code = 1
        else:
            started_ok = True
            port = config.com_ports[port_id]
            read_size = max_read_bytes or port.max_buffer_bytes
            last_data_at = time.monotonic()
            input_stream_closed = False
            stdin_chunks = start_stdin_reader(input_stream)
            while not failed:
                chunk = next_stdin_chunk(stdin_chunks)
                if isinstance(chunk, StdinReadFailure):
                    failed = True
                    error_result = {
                        "ok": False,
                        "tool": "com_stdio",
                        "error_type": "stdin_read_failed",
                        "port_id": port_id,
                        "backend_error": str(chunk.error),
                        "summary": "COM stdio input stream failed; the session was stopped.",
                    }
                    break
                if chunk:
                    write_in_flight = {"type": "operation", "tool": "com_stdio_write", "port_id": port_id, "started_at": utc_now_iso()}
                    written = service.write_bytes(port_id, chunk, "com_stdio_write")
                    if not isinstance(written, dict):
                        raise TypeError("COM stdio write returned a non-object result.")
                    error_type = str(written.get("error_type", ""))
                    completion_unconfirmed = written.get("completion_unconfirmed") is True or written.get("hardware_state_unconfirmed") is True or error_type == "serial_write_failed"
                    if completion_unconfirmed:
                        unconfirmed_operations.append(
                            {
                                **write_in_flight,
                                "error_type": error_type or "completion_unconfirmed",
                                "summary": "COM stdio write completion was not confirmed.",
                            }
                        )
                        failed = True
                        error_result = written
                    write_in_flight = None
                    if not written.get("ok"):
                        failed = True
                        error_result = written
                elif chunk == b"":
                    input_stream_closed = True
                result = service.read_bytes(port_id, read_size, read_wait_timeout_s, "com_stdio_read")
                if not result.get("ok"):
                    failed = True
                    error_result = result
                    break
                if int(result.get("bytes_read", 0)) > 0:
                    output_stream.write(str(result["data"].get("text", "")))
                    output_stream.flush()
                    last_data_at = time.monotonic()
                    continue
                if input_stream_closed and time.monotonic() - last_data_at >= eof_idle_timeout_s:
                    break
            exit_code = 1 if failed else 0
    except BaseException as error:
        if write_in_flight is not None:
            if is_confirmed_audit_error(error):
                write_in_flight = None
            else:
                unconfirmed_operations.append(
                    {
                        **write_in_flight,
                        "error_type": type(error).__name__,
                        "error": str(error),
                        "summary": "COM stdio write completion was not confirmed.",
                    }
                )
                write_in_flight = None
        pending_base_exception = error
    finally:
        if service is not None:
            try:
                if started_ok:
                    service.session_stop(port_id)
            except BaseException as error:
                if isinstance(error, Exception):
                    cleanup_error = error
                elif pending_base_exception is None:
                    pending_base_exception = error
            try:
                service.close()
            except BaseException as error:
                if isinstance(error, Exception) and cleanup_error is None:
                    cleanup_error = error
                elif not isinstance(error, Exception) and pending_base_exception is None:
                    pending_base_exception = error
            try:
                active_resources = [{"type": "com", "id": resource_id} for resource_id in service.active_session_ids()]
                hardware_state = {"active": bool(active_resources), "active_resources": active_resources, "inspection_errors": []}
            except BaseException as error:
                hardware_state = {"active": True, "active_resources": [], "inspection_errors": [{"type": "com", "error": str(error)}]}
                if not isinstance(error, Exception) and pending_base_exception is None:
                    pending_base_exception = error
        inspection_errors = [*hardware_state["inspection_errors"], *unconfirmed_operations]
        if write_in_flight is not None:
            inspection_errors.append({**write_in_flight, "summary": "COM stdio write was still in flight during cleanup."})
        hardware_state = {
            "active": bool(hardware_state["active_resources"] or inspection_errors),
            "active_resources": hardware_state["active_resources"],
            "inspection_errors": inspection_errors,
        }
        if hardware_state["active"]:
            try:
                quarantine = hardware_lock.quarantine_and_release(
                    reason="hardware_cleanup_failed",
                    source="com_stdio",
                    active_resources=hardware_state["active_resources"],
                    inspection_errors=hardware_state["inspection_errors"],
                )
            except HardwareLockError as error:
                quarantine = {"reason": "quarantine_failed", "source": "com_stdio", "backend_error": str(error)}
                hardware_lock.release_os_lock()
        else:
            try:
                hardware_lock.confirm_safe_and_release()
            except HardwareLockError as error:
                if cleanup_error is None:
                    cleanup_error = error
                hardware_state = {
                    "active": True,
                    "active_resources": hardware_state["active_resources"],
                    "inspection_errors": [*hardware_state["inspection_errors"], {"type": "hardware_state", "error": str(error)}],
                }
                quarantine = hardware_lock.quarantine_info()
        if cleanup_error is not None or hardware_state["active"]:
            result: JsonObject = {
                "ok": False,
                "tool": "com_stdio",
                "error_type": "hardware_cleanup_failed",
                "hardware_state_unconfirmed": bool(hardware_state["active"]),
                "port_id": port_id,
                "active_resources": hardware_state["active_resources"],
                "inspection_errors": hardware_state["inspection_errors"],
                "summary": "COM stdio cleanup could not confirm a safe hardware state.",
            }
            if cleanup_error is not None:
                result["backend_error"] = str(cleanup_error)
            if quarantine is not None:
                result["quarantine"] = quarantine
            write_error(error_stream, result)
        elif error_result is not None:
            write_error(error_stream, error_result)
    if pending_base_exception is not None:
        raise pending_base_exception
    if cleanup_error is not None or hardware_state["active"]:
        return 1
    return exit_code


class StdinReadFailure:
    """Terminal queue event: the stdin pump thread died with an error."""

    def __init__(self, error: BaseException):
        self.error = error


def start_stdin_reader(input_stream: BinaryIO) -> queue.Queue[object]:
    """Read stdin on a daemon thread so a blocked terminal read cannot stall serial output relaying.

    The pump always ends with a terminal event (EOF bytes or StdinReadFailure) so
    the main loop can never wait forever on a dead reader.
    """
    chunks: queue.Queue[object] = queue.Queue()

    def pump() -> None:
        try:
            while True:
                data = input_stream.read1(STDIN_CHUNK_BYTES) if hasattr(input_stream, "read1") else input_stream.read(STDIN_CHUNK_BYTES)
                chunks.put(bytes(data))
                if not data:
                    return
        except BaseException as error:
            chunks.put(StdinReadFailure(error))

    threading.Thread(target=pump, daemon=True).start()
    return chunks


def next_stdin_chunk(chunks: queue.Queue[object]) -> bytes | StdinReadFailure | None:
    """Return the next stdin chunk, b"" on EOF, a StdinReadFailure on reader death, or None when nothing is pending."""
    try:
        item = chunks.get(timeout=STDIN_POLL_TIMEOUT_S)
    except queue.Empty:
        return None
    if isinstance(item, StdinReadFailure):
        return item
    return item if isinstance(item, bytes) else bytes(item)


def write_error(output: TextIO, result: JsonObject) -> None:
    output.write(json.dumps(result) + "\n")
    output.flush()
