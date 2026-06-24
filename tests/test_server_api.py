# Copyright 2026 Hannes Pauli
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from aihil.config import load_config
from aihil.server import create_app


ROOT = Path(__file__).resolve().parents[1]


def write_config(tmp_path: Path) -> Path:
    fake = (ROOT / "tests" / "fixtures" / "fake_openocd.py").as_posix()
    path = tmp_path / ".aihil" / "config.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"""
server:
  listen: "127.0.0.1:8732"
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
""",
        encoding="utf-8",
    )
    return path


def client(tmp_path: Path) -> TestClient:
    config = load_config(write_config(tmp_path), work_dir=tmp_path)
    return TestClient(create_app(config))


def test_health_endpoint(tmp_path: Path) -> None:
    response = client(tmp_path).get("/health")
    assert response.status_code == 200
    assert response.json()["ok"] is True


def test_legacy_tool_routes_are_not_exposed(tmp_path: Path) -> None:
    response = client(tmp_path).post("/tools/aihil_probe_target")
    assert response.status_code == 404


def mcp_tool_call(api: TestClient, name: str, arguments: dict | None = None) -> dict:
    response = api.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": name,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments or {}},
        },
    )
    return response.json()["result"]["structuredContent"]


def test_mcp_initialize(tmp_path: Path) -> None:
    response = client(tmp_path).post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "pytest", "version": "0"},
            },
        },
    )
    data = response.json()
    assert response.status_code == 200
    assert response.headers["MCP-Protocol-Version"] == "2025-06-18"
    assert data["result"]["serverInfo"]["name"] == "aihil"
    assert "tools" in data["result"]["capabilities"]


def test_mcp_tools_list(tmp_path: Path) -> None:
    response = client(tmp_path).post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": "tools", "method": "tools/list"},
    )
    data = response.json()
    tools = {tool["name"]: tool for tool in data["result"]["tools"]}
    assert "aihil_probe_target" in tools
    assert "aihil_flash_firmware" in tools
    assert tools["aihil_flash_firmware"]["inputSchema"]["additionalProperties"] is False


def test_mcp_tools_call_debugger_info(tmp_path: Path) -> None:
    result = mcp_tool_call(client(tmp_path), "aihil_debugger_info")
    assert result["ok"] is True
    assert result["backend"] == "openocd"


def test_mcp_tools_call_probe(tmp_path: Path) -> None:
    response = client(tmp_path).post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "aihil_probe_target", "arguments": {}},
        },
    )
    data = response.json()
    result = data["result"]
    assert result["isError"] is False
    assert result["structuredContent"]["ok"] is True
    assert result["structuredContent"]["tool"] == "aihil_probe_target"
    assert result["content"][0]["type"] == "text"


def test_mcp_tools_call_flash_validates_artifact(tmp_path: Path) -> None:
    firmware = tmp_path / "build" / "firmware.elf"
    firmware.parent.mkdir()
    firmware.write_bytes(b"\x7fELFfake")

    response = client(tmp_path).post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "aihil_flash_firmware",
                "arguments": {"image_path": "build/firmware.elf"},
            },
        },
    )
    data = response.json()
    result = data["result"]
    assert result["isError"] is False
    assert result["structuredContent"]["ok"] is True
    assert result["structuredContent"]["artifact"]["sha256"]


def test_mcp_flash_blocks_outside_allowed_root(tmp_path: Path) -> None:
    firmware = tmp_path / "other" / "firmware.elf"
    firmware.parent.mkdir()
    firmware.write_bytes(b"\x7fELFfake")

    result = mcp_tool_call(
        client(tmp_path),
        "aihil_flash_firmware",
        {"image_path": "other/firmware.elf"},
    )
    assert result["ok"] is False
    assert result["error_type"] == "artifact_validation_failed"
    assert result["validation"]["allowed_root"] is False


def test_mcp_flash_blocks_disallowed_extension(tmp_path: Path) -> None:
    firmware = tmp_path / "build" / "firmware.txt"
    firmware.parent.mkdir()
    firmware.write_text("not firmware", encoding="utf-8")

    result = mcp_tool_call(
        client(tmp_path),
        "aihil_flash_firmware",
        {"image_path": "build/firmware.txt"},
    )
    assert result["ok"] is False
    assert result["validation"]["allowed_extension"] is False


def test_mcp_reset_rejects_invalid_mode(tmp_path: Path) -> None:
    result = mcp_tool_call(client(tmp_path), "aihil_reset_target", {"mode": "bad"})
    assert result["ok"] is False
    assert result["error_type"] == "invalid_argument"


def test_mcp_get_last_report(tmp_path: Path) -> None:
    api = client(tmp_path)
    mcp_tool_call(api, "aihil_probe_target")
    result = mcp_tool_call(api, "aihil_get_last_report")
    assert result["ok"] is True
    assert result["report"]["tool"] == "aihil_probe_target"


def test_mcp_invalid_params_returns_jsonrpc_error(tmp_path: Path) -> None:
    response = client(tmp_path).post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 4, "method": "initialize", "params": []},
    )
    data = response.json()
    assert data["error"]["code"] == -32602


def test_mcp_initialized_notification_returns_accepted(tmp_path: Path) -> None:
    response = client(tmp_path).post(
        "/mcp",
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
    )
    assert response.status_code == 202
    assert response.content == b""
