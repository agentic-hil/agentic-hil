# Copyright 2026 Hannes Pauli
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import pytest

from aihil.__main__ import build_parser, doctor, init_config, mcp_config


def test_init_config_writes_starter_config(tmp_path) -> None:
    config_path = tmp_path / ".aihil" / "config.yaml"

    result = init_config(str(config_path))

    assert result["ok"] is True
    assert config_path.exists()
    assert "server:" in config_path.read_text(encoding="utf-8")


def test_init_config_does_not_overwrite_without_force(tmp_path) -> None:
    config_path = tmp_path / ".aihil" / "config.yaml"
    config_path.parent.mkdir()
    config_path.write_text("existing: true\n", encoding="utf-8")

    result = init_config(str(config_path))

    assert result["ok"] is False
    assert result["error_type"] == "config_exists"
    assert config_path.read_text(encoding="utf-8") == "existing: true\n"


def test_mcp_config_uses_configured_server(tmp_path) -> None:
    config_path = tmp_path / ".aihil" / "config.yaml"
    config_path.parent.mkdir()
    config_path.write_text(
        """
server:
  listen: "127.0.0.1:9999"
""",
        encoding="utf-8",
    )

    result = mcp_config(str(config_path))

    assert result["mcpServers"]["aihil"]["url"] == "http://127.0.0.1:9999/mcp"


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
    assert result["mcp_endpoint"] == "http://127.0.0.1:8732/mcp"
    assert result["debugger"]["tool"] == "aihil_debugger_info"


def test_cli_requires_explicit_subcommand() -> None:
    parser = build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args([])
