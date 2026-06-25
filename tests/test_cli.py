# Copyright 2026 Hannes Pauli
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json

import pytest

from aihil.__main__ import build_parser, doctor, init_config, main, mcp_config, schema


def test_init_config_writes_starter_config(tmp_path) -> None:
    config_path = tmp_path / ".aihil" / "config.yaml"

    result = init_config(str(config_path))

    assert result["ok"] is True
    assert config_path.exists()
    assert "target:" in config_path.read_text(encoding="utf-8")
    assert "server:" not in config_path.read_text(encoding="utf-8")
    assert "config.schema.json" not in config_path.read_text(encoding="utf-8")


def test_init_config_reports_detected_com_ports(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_path = tmp_path / ".aihil" / "config.yaml"
    monkeypatch.setattr(
        "aihil.__main__.list_available_com_ports",
        lambda: {
            "ok": True,
            "tool": "aihil_com_ports_available",
            "ports": [{"device": "COM9"}],
            "summary": "1 available COM port(s).",
        },
    )

    result = init_config(str(config_path))

    assert result["ok"] is True
    assert result["available_com_ports"]["ports"] == [{"device": "COM9"}]
    assert any("COM9" in step for step in result["next_steps"])


def test_schema_command_writes_bundled_schema(tmp_path) -> None:
    schema_path = tmp_path / "config.schema.json"

    result = schema(str(schema_path))

    assert result["ok"] is True
    assert schema_path.exists()
    assert "AI-HIL project configuration" in schema_path.read_text(encoding="utf-8")


def test_init_config_does_not_overwrite_without_force(tmp_path) -> None:
    config_path = tmp_path / ".aihil" / "config.yaml"
    config_path.parent.mkdir()
    config_path.write_text("existing: true\n", encoding="utf-8")

    result = init_config(str(config_path))

    assert result["ok"] is False
    assert result["error_type"] == "config_exists"
    assert config_path.read_text(encoding="utf-8") == "existing: true\n"


def test_mcp_config_uses_stdio_command(tmp_path) -> None:
    config_path = tmp_path / ".aihil" / "config.yaml"

    result = mcp_config(str(config_path))

    assert result["mcpServers"]["aihil"] == {
        "command": "aihil",
        "args": ["mcp-stdio", "--config", str(config_path)],
    }


def test_doctor_reports_debugger_status(tmp_path) -> None:
    config_path = tmp_path / ".aihil" / "config.yaml"
    fake_openocd = tmp_path / "fake_openocd.py"
    fake_openocd.write_text("", encoding="utf-8")
    config_path.parent.mkdir()
    config_path.write_text(
        f"""
debugger:
  type: "openocd"
  executable: "{fake_openocd.as_posix()}"
""",
        encoding="utf-8",
    )

    result = doctor(str(config_path))

    assert result["tool"] == "aihil_doctor"
    assert result["mcp"]["transport"] == "stdio"
    assert result["mcp"]["command"] == "aihil"
    assert result["mcp"]["args"][-1] == str(config_path)
    assert result["debugger"]["tool"] == "aihil_debugger_info"


def test_cli_requires_explicit_subcommand() -> None:
    parser = build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args([])


def test_cli_exposes_stdio_subcommand() -> None:
    parser = build_parser()

    args = parser.parse_args(["mcp-stdio", "--config", "custom.yaml"])

    assert args.command == "mcp-stdio"
    assert args.config == "custom.yaml"


def test_cli_exposes_com_stdio_subcommand() -> None:
    parser = build_parser()

    args = parser.parse_args(["com-stdio", "--config", "custom.yaml", "--port", "dut_uart"])

    assert args.command == "com-stdio"
    assert args.config == "custom.yaml"
    assert args.port == "dut_uart"


def test_cli_exposes_com_ports_subcommand() -> None:
    parser = build_parser()

    args = parser.parse_args(["com-ports"])

    assert args.command == "com-ports"


def test_cli_com_ports_command_outputs_discovered_ports(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.setattr(
        "aihil.__main__.list_available_com_ports",
        lambda: {
            "ok": True,
            "tool": "aihil_com_ports_available",
            "ports": [{"device": "COM10"}],
            "summary": "1 available COM port(s).",
        },
    )

    status = main(["com-ports"])

    assert status == 0
    assert json.loads(capsys.readouterr().out)["ports"] == [{"device": "COM10"}]
