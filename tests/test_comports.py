# Copyright 2026 Hannes Pauli
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import sys
import threading
import time
import types
from pathlib import Path

import pytest

from aihil.comports import ComPortService, list_available_com_ports
from aihil.config import load_config


def write_config(tmp_path: Path, extra: str = "") -> Path:
    path = tmp_path / ".aihil" / "config.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"""
com_ports:
  dut_uart:
    device: "loop://"
    baudrate: 115200
    timeout_s: 0.01
    write_timeout_s: 0.5
    encoding: "utf-8"
    max_buffer_bytes: 65536
    max_write_bytes: 64
reports:
  directory: ".aihil/reports"
logs:
  directory: ".aihil/logs"
{extra}
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


def install_fake_serial(monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    module = types.ModuleType("serial")
    module.instances = []

    def serial_for_url(device: str, baudrate: int, timeout: float, write_timeout: float) -> FakeLoopSerial:
        instance = FakeLoopSerial(device, baudrate, timeout, write_timeout)
        module.instances.append(instance)
        return instance

    module.serial_for_url = serial_for_url
    monkeypatch.setitem(sys.modules, "serial", module)
    return module


def install_fake_list_ports(monkeypatch: pytest.MonkeyPatch, ports: list[types.SimpleNamespace]) -> None:
    serial_module = types.ModuleType("serial")
    tools_module = types.ModuleType("serial.tools")
    list_ports_module = types.ModuleType("serial.tools.list_ports")
    list_ports_module.comports = lambda: ports
    serial_module.tools = tools_module
    tools_module.list_ports = list_ports_module
    monkeypatch.setitem(sys.modules, "serial", serial_module)
    monkeypatch.setitem(sys.modules, "serial.tools", tools_module)
    monkeypatch.setitem(sys.modules, "serial.tools.list_ports", list_ports_module)


def test_list_available_com_ports_returns_pyserial_ports(monkeypatch: pytest.MonkeyPatch) -> None:
    install_fake_list_ports(
        monkeypatch,
        [
            types.SimpleNamespace(
                device="COM5",
                name="COM5",
                description="USB Serial Port",
                hwid="USB VID:PID=1234:5678",
                manufacturer="Acme",
                product="Debug UART",
                interface=None,
                location=None,
                serial_number="ABC123",
                vid=0x1234,
                pid=0x5678,
            )
        ],
    )

    result = list_available_com_ports()

    assert result["ok"] is True
    assert result["ports"] == [
        {
            "device": "COM5",
            "name": "COM5",
            "description": "USB Serial Port",
            "hwid": "USB VID:PID=1234:5678",
            "manufacturer": "Acme",
            "product": "Debug UART",
            "serial_number": "ABC123",
            "vid": 0x1234,
            "pid": 0x5678,
        }
    ]


def test_list_available_com_ports_handles_missing_pyserial(monkeypatch: pytest.MonkeyPatch) -> None:
    def raise_import_error(_name: str):
        raise ImportError("missing pyserial")

    monkeypatch.setattr("aihil.comports.importlib.import_module", raise_import_error)

    result = list_available_com_ports()

    assert result["ok"] is False
    assert result["error_type"] == "serial_backend_not_available"


def test_com_ports_list_includes_configured_and_available(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "aihil.comports.list_available_com_ports",
        lambda: {
            "ok": True,
            "tool": "aihil_com_ports_available",
            "ports": [{"device": "COM7"}],
            "summary": "1 available COM port(s).",
        },
    )
    service = ComPortService(load_config(write_config(tmp_path), work_dir=tmp_path))

    result = service.list_ports()

    assert result["ok"] is True
    assert result["ports"]["dut_uart"]["device"] == "loop://"
    assert result["available_com_ports"]["ports"] == [{"device": "COM7"}]


def test_com_port_session_roundtrip_uses_background_reader(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    install_fake_serial(monkeypatch)
    service = ComPortService(load_config(write_config(tmp_path), work_dir=tmp_path))

    start = service.session_start("dut_uart")
    write = service.write("dut_uart", {"text": "ping\n"})
    read = service.read("dut_uart", wait_timeout_s=1.0)
    stop = service.session_stop("dut_uart")

    assert start["ok"] is True
    assert write["ok"] is True
    assert write["bytes_written"] == 5
    assert read["ok"] is True
    assert read["data"]["text"] == "ping\n"
    assert read["data"]["hex"] == "70696e670a"
    assert stop["ok"] is True
    assert (tmp_path / ".aihil" / "reports" / "last-report.json").exists()
    assert (tmp_path / read["log_path"]).exists()


def test_com_write_requires_configured_write_permission(tmp_path: Path) -> None:
    config = load_config(
        write_config(
            tmp_path,
            """
permissions:
  allow_com_write: false
""",
        ),
        work_dir=tmp_path,
    )

    result = ComPortService(config).write("dut_uart", {"text": "x"})

    assert result["ok"] is False
    assert result["error_type"] == "permission_denied"


def test_com_session_start_requires_configured_port(tmp_path: Path) -> None:
    service = ComPortService(load_config(write_config(tmp_path), work_dir=tmp_path))

    result = service.session_start("missing")

    assert result["ok"] is False
    assert result["error_type"] == "com_port_not_configured"
    assert result["configured_ports"] == ["dut_uart"]
