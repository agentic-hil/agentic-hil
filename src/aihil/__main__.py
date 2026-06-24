# Copyright 2026 Hannes Pauli
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import uvicorn

from .config import DEFAULT_CONFIG_PATH, ConfigError, load_config
from .debugger import create_debugger_backend
from .server import create_app


DEFAULT_CONFIG_TEMPLATE = """server:
  listen: "127.0.0.1:8732"

target:
  name: "example-target"
  controller: "unknown-controller"

debugger:
  type: "openocd"
  executable: null
  interface_cfg: "interface/stlink.cfg"
  target_cfg: "target/stm32f4x.cfg"
  timeout_s: 60

artifacts:
  allowed_roots:
    - "build"
  upload_directory: ".aihil/artifacts"
  allowed_extensions:
    - ".elf"
    - ".hex"
    - ".bin"
  max_upload_size_mb: 64
  allow_upload: true

validation:
  require_existing_file: true
  require_allowed_root: true
  require_allowed_extension: true
  compute_sha256: true
  inspect_known_formats: true

permissions:
  allow_probe: true
  allow_flash: true
  allow_reset: true
  allow_raw_debugger_commands: false
  allow_mass_erase: false

reports:
  directory: ".aihil/reports"

logs:
  directory: ".aihil/logs"
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="aihil",
        description="AI-HIL MCP server command. Install once per machine, then run per project with .aihil/config.yaml.",
    )

    command_parsers = parser.add_subparsers(dest="command", required=True)

    init_parser = command_parsers.add_parser("init", help="Create a project-local .aihil/config.yaml")
    init_parser.add_argument("--config", default=None, help="Project config path to write, defaults to .aihil/config.yaml")
    init_parser.add_argument("--force", action="store_true", help="Overwrite an existing config file")

    doctor_parser = command_parsers.add_parser("doctor", help="Validate local AI-HIL setup")
    doctor_parser.add_argument("--config", default=None, help="Path to .aihil/config.yaml")

    mcp_config_parser = command_parsers.add_parser("mcp-config", help="Print MCP client configuration JSON")
    mcp_config_parser.add_argument("--config", default=None, help="Path to .aihil/config.yaml")

    serve_parser = command_parsers.add_parser("serve", help="Run the local HTTP MCP server")
    serve_parser.add_argument("--config", default=None, help="Path to .aihil/config.yaml")
    serve_parser.add_argument("--host", default=None, help="Override configured server host")
    serve_parser.add_argument("--port", type=int, default=None, help="Override configured server port")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    command = args.command

    if command == "init":
        result = init_config(args.config, force=args.force)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0 if result["ok"] else 1

    if command == "doctor":
        result = doctor(args.config)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0 if result["ok"] else 1

    if command == "mcp-config":
        print(json.dumps(mcp_config(args.config), indent=2, sort_keys=True))
        return 0

    return serve(args)


def init_config(config_path: str | None = None, force: bool = False) -> dict[str, Any]:
    path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
    if path.exists() and not force:
        return {
            "ok": False,
            "error_type": "config_exists",
            "summary": "AI-HIL configuration already exists. Use --force to overwrite it.",
            "path": str(path),
        }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(DEFAULT_CONFIG_TEMPLATE, encoding="utf-8")
    return {
        "ok": True,
        "summary": "AI-HIL starter configuration written.",
        "path": str(path),
        "next_steps": [
            "Keep this .aihil/config.yaml with the firmware project; install aihil only once per machine.",
            "Edit target.name and target.controller for your board.",
            "Set debugger.interface_cfg and debugger.target_cfg for your OpenOCD setup.",
            "Run: aihil doctor",
            "Run: aihil serve --config .aihil/config.yaml",
        ],
    }


def doctor(config_path: str | None = None) -> dict[str, Any]:
    try:
        config = load_config(config_path)
    except ConfigError as exc:
        result = exc.to_dict()
        result["tool"] = "aihil_doctor"
        return result

    backend = create_debugger_backend(config)
    debugger_info = backend.info()
    return {
        "ok": debugger_info.get("ok") is True,
        "tool": "aihil_doctor",
        "summary": "AI-HIL configuration loaded and debugger checked."
        if debugger_info.get("ok")
        else "AI-HIL configuration loaded, but debugger check failed.",
        "config_path": str(config.config_path),
        "mcp_endpoint": f"http://{config.server.host}:{config.server.port}/mcp",
        "server": {
            "listen": config.server.listen,
            "host": config.server.host,
            "port": config.server.port,
        },
        "target": {
            "name": config.target.name,
            "controller": config.target.controller,
        },
        "debugger": debugger_info,
    }


def mcp_config(config_path: str | None = None) -> dict[str, Any]:
    host = "127.0.0.1"
    port = 8732
    try:
        config = load_config(config_path)
        host = config.server.host
        port = config.server.port
    except ConfigError:
        pass
    return {
        "mcpServers": {
            "aihil": {
                "type": "http",
                "url": f"http://{host}:{port}/mcp",
            }
        }
    }


def serve(args: argparse.Namespace) -> int:
    try:
        config = load_config(args.config)
        app = create_app(config)
    except ConfigError as exc:
        print(json.dumps(exc.to_dict(), indent=2, sort_keys=True), file=sys.stderr)
        return 2
    host = getattr(args, "host", None) or config.server.host
    port = getattr(args, "port", None) or config.server.port
    if host == "0.0.0.0":
        print(
            "Warning: AI-HIL server is listening on 0.0.0.0 and may be reachable from the network.",
            file=sys.stderr,
        )
    uvicorn.run(app, host=host, port=port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
