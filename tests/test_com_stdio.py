# Copyright 2026 Hannes Pauli
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import io
import json
import sys
import threading
import time
import types
from pathlib import Path

import pytest

from aihil.comstdio import run_com_stdio
from aihil.config import load_config


def write_config(tmp_path: Path) -> Path:
    path = tmp_path / ".aihil" / "config.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        """
com_ports:
  dut_uart:
    device: "loop://"
    baudrate: 115200
    timeout_s: 0.01
    write_timeout_s: 0.5
    encoding: "ascii"
    max_buffer_bytes: 65536
    max_write_bytes: 64
reports:
  directory: ".aihil/reports"
logs:
  directory: ".aihil/logs"
""",
        encoding="utf-8",
    )
    return path


class FakeLoopSerial:
    def __init__(self, device: str, baudrate: int, timeout: float, write_timeout: float) -> None:
        self.device = device
        self.baudrate = baudrate
        self.timeout = timeout
        self.write_timeout = write_timeout
        self.closed = False
        self._rx = bytearray()
        self._lock = threading.Lock()

    @property
    def in_waiting(self) -> int:
        with self._lock:
            return len(self._rx)

    def read(self, size: int = 1) -> bytes:
        deadline = time.monotonic() + self.timeout
        while True:
            with self._lock:
                if self._rx:
                    data = bytes(self._rx[:size])
                    del self._rx[:size]
                    return data
                if self.closed:
                    raise OSError("serial port closed")
            if time.monotonic() >= deadline:
                return b""
            time.sleep(0.001)

    def write(self, data: bytes) -> int:
        with self._lock:
            if self.closed:
                raise OSError("serial port closed")
            self._rx.extend(data)
            return len(data)

    def flush(self) -> None:
        return None

    def close(self) -> None:
        with self._lock:
            self.closed = True


def install_fake_serial(monkeypatch: pytest.MonkeyPatch) -> None:
    module = types.ModuleType("serial")

    def serial_for_url(device: str, baudrate: int, timeout: float, write_timeout: float) -> FakeLoopSerial:
        return FakeLoopSerial(device, baudrate, timeout, write_timeout)

    module.serial_for_url = serial_for_url
    monkeypatch.setitem(sys.modules, "serial", module)


def test_com_stdio_bridges_text_stdin_to_com_and_com_to_stdout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    install_fake_serial(monkeypatch)
    config = load_config(write_config(tmp_path), work_dir=tmp_path)
    stdin = io.StringIO("ping\n")
    stdout = io.StringIO()
    stderr = io.StringIO()

    result = run_com_stdio(
        config,
        "dut_uart",
        stdin,
        stdout,
        stderr,
        read_wait_timeout_s=0.01,
        eof_idle_timeout_s=0.2,
    )

    assert result == 0
    assert stdout.getvalue() == "ping\n"
    assert stderr.getvalue() == ""


def test_com_stdio_rejects_unconfigured_port(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    install_fake_serial(monkeypatch)
    config = load_config(write_config(tmp_path), work_dir=tmp_path)
    stdout = io.StringIO()
    stderr = io.StringIO()

    result = run_com_stdio(config, "missing", io.StringIO("ping\n"), stdout, stderr, eof_idle_timeout_s=0.01)

    assert result == 1
    assert stdout.getvalue() == ""
    assert "com_port_not_configured" in stderr.getvalue()
    report = json.loads((tmp_path / ".aihil" / "reports" / "last-report.json").read_text(encoding="utf-8"))
    assert report["error_type"] == "com_port_not_configured"
