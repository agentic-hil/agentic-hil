# Copyright 2026 Hannes Pauli
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
import io
import json
import sys
import threading
import time
import types
from pathlib import Path

from aihil.config import load_config
from aihil.mcp import handle_mcp_message
from aihil.stdio import run_stdio_server
from aihil.tools import AIHILToolService


ROOT = Path(__file__).resolve().parents[1]


def write_config(tmp_path: Path) -> Path:
    fake = (ROOT / "tests" / "fixtures" / "fake_openocd.py").as_posix()
    path = tmp_path / ".aihil" / "config.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"""
target:
  name: "example-target"
  controller: "stm32f4"
debugger:
  type: "openocd"
  executable: "{fake}"
  interface_cfg: "interface/stlink.cfg"
  target_cfg: "target/stm32f4x.cfg"
  timeout_s: 5
artifacts:
  allowed_roots: ["build"]
  allowed_extensions: [".elf", ".hex", ".bin"]
reports:
  directory: ".aihil/reports"
logs:
  directory: ".aihil/logs"
com_ports:
  dut_uart:
    device: "loop://"
    baudrate: 115200
    timeout_s: 0.01
    write_timeout_s: 0.5
    max_write_bytes: 64
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


def install_fake_serial(monkeypatch) -> None:
    module = types.ModuleType("serial")

    def serial_for_url(device: str, baudrate: int, timeout: float, write_timeout: float) -> FakeLoopSerial:
        return FakeLoopSerial(device, baudrate, timeout, write_timeout)

    module.serial_for_url = serial_for_url
    monkeypatch.setitem(sys.modules, "serial", module)


@contextmanager
def tool_service(tmp_path: Path) -> Iterator[AIHILToolService]:
    service = AIHILToolService(load_config(write_config(tmp_path), work_dir=tmp_path))
    try:
        yield service
    finally:
        service.close()


def mcp_tool_call(service: AIHILToolService, name: str, arguments: dict | None = None) -> dict:
    response = handle_mcp_message(
        {
            "jsonrpc": "2.0",
            "id": name,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments or {}},
        },
        service,
    )
    assert response is not None
    return response["result"]["structuredContent"]


def test_mcp_initialize(tmp_path: Path) -> None:
    with tool_service(tmp_path) as service:
        response = handle_mcp_message(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "pytest", "version": "0"},
                },
            },
            service,
        )

    assert response is not None
    assert response["result"]["serverInfo"]["name"] == "aihil"
    assert "tools" in response["result"]["capabilities"]


def test_mcp_tools_list(tmp_path: Path) -> None:
    with tool_service(tmp_path) as service:
        response = handle_mcp_message({"jsonrpc": "2.0", "id": "tools", "method": "tools/list"}, service)

    assert response is not None
    tools = {tool["name"]: tool for tool in response["result"]["tools"]}
    assert "aihil_probe_target" in tools
    assert "aihil_flash_firmware" in tools
    assert "aihil_com_session_start" in tools
    assert "aihil_com_write" in tools
    assert "aihil_com_read" in tools
    assert tools["aihil_flash_firmware"]["inputSchema"]["additionalProperties"] is False
    assert tools["aihil_com_write"]["inputSchema"]["additionalProperties"] is False


def test_mcp_tools_call_debugger_info(tmp_path: Path) -> None:
    with tool_service(tmp_path) as service:
        result = mcp_tool_call(service, "aihil_debugger_info")

    assert result["ok"] is True
    assert result["backend"] == "openocd"


def test_mcp_tools_call_probe(tmp_path: Path) -> None:
    with tool_service(tmp_path) as service:
        response = handle_mcp_message(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "aihil_probe_target", "arguments": {}},
            },
            service,
        )

    assert response is not None
    result = response["result"]
    assert result["isError"] is False
    assert result["structuredContent"]["ok"] is True
    assert result["structuredContent"]["tool"] == "aihil_probe_target"
    assert result["content"][0]["type"] == "text"


def test_mcp_tools_call_flash_validates_artifact(tmp_path: Path) -> None:
    firmware = tmp_path / "build" / "firmware.elf"
    firmware.parent.mkdir()
    firmware.write_bytes(b"\x7fELFfake")

    with tool_service(tmp_path) as service:
        response = handle_mcp_message(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "aihil_flash_firmware",
                    "arguments": {"image_path": "build/firmware.elf"},
                },
            },
            service,
        )

    assert response is not None
    result = response["result"]
    assert result["isError"] is False
    assert result["structuredContent"]["ok"] is True
    assert result["structuredContent"]["artifact"]["sha256"]


def test_mcp_flash_blocks_outside_allowed_root(tmp_path: Path) -> None:
    firmware = tmp_path / "other" / "firmware.elf"
    firmware.parent.mkdir()
    firmware.write_bytes(b"\x7fELFfake")

    with tool_service(tmp_path) as service:
        result = mcp_tool_call(service, "aihil_flash_firmware", {"image_path": "other/firmware.elf"})

    assert result["ok"] is False
    assert result["error_type"] == "artifact_validation_failed"
    assert result["validation"]["allowed_root"] is False


def test_mcp_flash_blocks_disallowed_extension(tmp_path: Path) -> None:
    firmware = tmp_path / "build" / "firmware.txt"
    firmware.parent.mkdir()
    firmware.write_text("not firmware", encoding="utf-8")

    with tool_service(tmp_path) as service:
        result = mcp_tool_call(service, "aihil_flash_firmware", {"image_path": "build/firmware.txt"})

    assert result["ok"] is False
    assert result["validation"]["allowed_extension"] is False


def test_mcp_reset_rejects_invalid_mode(tmp_path: Path) -> None:
    with tool_service(tmp_path) as service:
        result = mcp_tool_call(service, "aihil_reset_target", {"mode": "bad"})

    assert result["ok"] is False
    assert result["error_type"] == "invalid_argument"


def test_mcp_get_last_report(tmp_path: Path) -> None:
    with tool_service(tmp_path) as service:
        mcp_tool_call(service, "aihil_probe_target")
        result = mcp_tool_call(service, "aihil_get_last_report")

    assert result["ok"] is True
    assert result["report"]["tool"] == "aihil_probe_target"


def test_mcp_com_session_write_read(tmp_path: Path, monkeypatch) -> None:
    install_fake_serial(monkeypatch)

    with tool_service(tmp_path) as service:
        start = mcp_tool_call(service, "aihil_com_session_start", {"port_id": "dut_uart"})
        write = mcp_tool_call(service, "aihil_com_write", {"port_id": "dut_uart", "text": "status?\n"})
        chunks: list[str] = []
        deadline = time.monotonic() + 1.0
        while "".join(chunks) != "status?\n" and time.monotonic() < deadline:
            read = mcp_tool_call(service, "aihil_com_read", {"port_id": "dut_uart", "wait_timeout_s": 0.1})
            chunks.append(read["data"]["text"])
        stop = mcp_tool_call(service, "aihil_com_session_stop", {"port_id": "dut_uart"})

    assert start["ok"] is True
    assert write["ok"] is True
    assert read["ok"] is True
    assert "".join(chunks) == "status?\n"
    assert stop["ok"] is True


def test_mcp_invalid_params_returns_jsonrpc_error(tmp_path: Path) -> None:
    with tool_service(tmp_path) as service:
        response = handle_mcp_message({"jsonrpc": "2.0", "id": 4, "method": "initialize", "params": []}, service)

    assert response is not None
    assert response["error"]["code"] == -32602


def test_mcp_initialized_notification_returns_none(tmp_path: Path) -> None:
    with tool_service(tmp_path) as service:
        response = handle_mcp_message({"jsonrpc": "2.0", "method": "notifications/initialized"}, service)

    assert response is None


def test_stdio_server_handles_line_delimited_messages(tmp_path: Path) -> None:
    config = load_config(write_config(tmp_path), work_dir=tmp_path)
    stdin = io.StringIO(json.dumps({"jsonrpc": "2.0", "id": "tools", "method": "tools/list"}) + "\n")
    stdout = io.StringIO()

    result = run_stdio_server(config, stdin, stdout)

    assert result == 0
    lines = stdout.getvalue().splitlines()
    assert len(lines) == 1
    response = json.loads(lines[0])
    assert response["id"] == "tools"
    assert response["result"]["tools"]


def test_stdio_server_returns_parse_error(tmp_path: Path) -> None:
    config = load_config(write_config(tmp_path), work_dir=tmp_path)
    stdin = io.StringIO("{bad json\n")
    stdout = io.StringIO()

    result = run_stdio_server(config, stdin, stdout)

    assert result == 0
    response = json.loads(stdout.getvalue())
    assert response["error"]["code"] == -32700
