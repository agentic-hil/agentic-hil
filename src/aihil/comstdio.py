# Copyright 2026 Hannes Pauli
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
import threading
import time
from typing import Any, TextIO

from .comports import ComPortService
from .config import AIHILConfig


def run_com_stdio(
    config: AIHILConfig,
    port_id: str,
    stdin: TextIO,
    stdout: TextIO,
    stderr: TextIO,
    *,
    max_read_bytes: int | None = None,
    read_wait_timeout_s: float = 0.05,
    eof_idle_timeout_s: float = 0.5,
) -> int:
    service = ComPortService(config)
    stop_event = threading.Event()
    stdin_done = threading.Event()
    failed = threading.Event()
    started_ok = False

    try:
        started = service.session_start(port_id, clear_buffer=True)
        if not started.get("ok"):
            _write_error(stderr, started)
            return 1
        started_ok = True

        port = config.com_ports[port_id]
        read_size = max_read_bytes or port.max_buffer_bytes

        def write_stdin_to_com() -> None:
            try:
                while not stop_event.is_set():
                    text = stdin.read(1)
                    if text == "":
                        stdin_done.set()
                        return
                    try:
                        data = text.encode(port.encoding)
                    except LookupError:
                        failed.set()
                        _write_error(
                            stderr,
                            {
                                "ok": False,
                                "tool": "aihil_com_stdio",
                                "port_id": port_id,
                                "error_type": "config_invalid",
                                "summary": "COM port encoding is not supported by Python.",
                                "encoding": port.encoding,
                            },
                        )
                        return
                    result = service.write_bytes(port_id, data, "aihil_com_stdio_write")
                    if not result.get("ok"):
                        failed.set()
                        _write_error(stderr, result)
                        return
            finally:
                stdin_done.set()

        writer = threading.Thread(target=write_stdin_to_com, name=f"aihil-com-stdio-{port_id}-stdin", daemon=True)
        writer.start()

        last_data_at = time.monotonic()
        while not stop_event.is_set():
            result = service.read_bytes(port_id, read_size, read_wait_timeout_s, "aihil_com_stdio_read")
            if not result.get("ok"):
                failed.set()
                _write_error(stderr, result)
                break
            if result.get("bytes_read", 0):
                stdout.write(str(result.get("data", {}).get("text", "")))
                stdout.flush()
                last_data_at = time.monotonic()
                continue
            if failed.is_set():
                break
            if stdin_done.is_set() and time.monotonic() - last_data_at >= eof_idle_timeout_s:
                break

        stop_event.set()
        writer.join(timeout=1.0)
        return 1 if failed.is_set() else 0
    finally:
        stop_event.set()
        if started_ok:
            service.session_stop(port_id)
        service.close()


def _write_error(stderr: TextIO, result: dict[str, Any]) -> None:
    stderr.write(json.dumps(result, ensure_ascii=False, sort_keys=True))
    stderr.write("\n")
    stderr.flush()
