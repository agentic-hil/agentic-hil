# Copyright 2026 Hannes Pauli
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

from .comports import list_available_com_ports
from .comstdio import run_com_stdio
from .config import DEFAULT_CONFIG_PATH, ConfigError, config_schema_text, display_path, load_config
from .debugger import create_debugger_backend
from .stdio import run_stdio_server


DEFAULT_CONFIG_TEMPLATE = """target:
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

com_ports: {}

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
  allow_com_read: true
  allow_com_write: true
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
        description="AI-HIL MCP stdio server command. Install once per machine, then run per project with .aihil/config.yaml.",
    )

    command_parsers = parser.add_subparsers(dest="command", required=True)

    init_parser = command_parsers.add_parser("init", help="Create a project-local .aihil/config.yaml")
    init_parser.add_argument("--config", default=None, help="Project config path to write, defaults to .aihil/config.yaml")
    init_parser.add_argument("--force", action="store_true", help="Overwrite an existing config file")

    schema_parser = command_parsers.add_parser("schema", help="Export the bundled .aihil/config.yaml JSON schema")
    schema_parser.add_argument("--output", default=None, help="Path to write the schema JSON, defaults to stdout")
    schema_parser.add_argument("--force", action="store_true", help="Overwrite an existing output file")

    doctor_parser = command_parsers.add_parser("doctor", help="Validate local AI-HIL setup")
    doctor_parser.add_argument("--config", default=None, help="Path to .aihil/config.yaml")

    command_parsers.add_parser("com-ports", help="List host serial/COM ports available for config setup")

    mcp_config_parser = command_parsers.add_parser("mcp-config", help="Print MCP client configuration JSON")
    mcp_config_parser.add_argument("--config", default=None, help="Path to .aihil/config.yaml")

    stdio_parser = command_parsers.add_parser("mcp-stdio", help="Run the project-scoped MCP stdio server")
    stdio_parser.add_argument("--config", default=None, help="Path to .aihil/config.yaml")

    com_stdio_parser = command_parsers.add_parser("com-stdio", help="Bridge one configured COM port as a plain text stdio stream")
    com_stdio_parser.add_argument("--config", default=None, help="Path to .aihil/config.yaml")
    com_stdio_parser.add_argument("--port", required=True, help="Configured com_ports id, for example dut_uart")
    com_stdio_parser.add_argument("--max-read-bytes", type=int, default=None, help="Maximum COM bytes to read per stdout write")
    com_stdio_parser.add_argument("--read-wait-timeout-s", type=float, default=0.05, help="Seconds each COM read waits for data")
    com_stdio_parser.add_argument(
        "--eof-idle-timeout-s",
        type=float,
        default=0.5,
        help="After stdin closes, exit after this many idle seconds without COM data",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    command = args.command

    if command == "init":
        result = init_config(args.config, force=args.force)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0 if result["ok"] else 1

    if command == "schema":
        result = schema(args.output, force=args.force)
        if args.output is None:
            print(result["schema"], end="")
        else:
            print(json.dumps(result, indent=2, sort_keys=True))
        return 0 if result["ok"] else 1

    if command == "doctor":
        result = doctor(args.config)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0 if result["ok"] else 1

    if command == "com-ports":
        result = list_available_com_ports()
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0 if result["ok"] else 1

    if command == "mcp-config":
        print(json.dumps(mcp_config(args.config), indent=2, sort_keys=True))
        return 0

    if command == "mcp-stdio":
        return mcp_stdio(args.config)

    if command == "com-stdio":
        return com_stdio(
            args.config,
            args.port,
            args.max_read_bytes,
            args.read_wait_timeout_s,
            args.eof_idle_timeout_s,
        )

    parser.error(f"unknown command: {command}")


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
    try:
        load_config(path)
    except ConfigError as exc:
        result = exc.to_dict()
        result["summary"] = "AI-HIL starter configuration was written but failed validation."
        result["path"] = str(path)
        return result
    available_com_ports = list_available_com_ports()
    return {
        "ok": True,
        "summary": "AI-HIL starter configuration written.",
        "path": str(path),
        "available_com_ports": available_com_ports,
        "next_steps": _init_next_steps(available_com_ports),
    }


def _init_next_steps(available_com_ports: dict[str, Any]) -> list[str]:
    next_steps = [
        "Keep this .aihil/config.yaml with the firmware project; install aihil only once per machine.",
        "Edit target.name and target.controller for your board.",
        "Set debugger.interface_cfg and debugger.target_cfg for your OpenOCD setup.",
    ]
    if available_com_ports.get("ok"):
        ports = available_com_ports.get("ports", [])
        if ports:
            devices = ", ".join(str(port.get("device", "")) for port in ports[:5])
            suffix = "" if len(ports) <= 5 else f", and {len(ports) - 5} more"
            next_steps.append(
                f"Detected COM ports: {devices}{suffix}. Add the DUT UART under com_ports if serial feedback is needed."
            )
        else:
            next_steps.append("No host COM ports detected. Connect USB serial hardware and run: aihil com-ports")
    else:
        next_steps.append("COM port discovery failed. Run: aihil com-ports after checking the pyserial installation.")
    next_steps.extend(
        [
            "Run: aihil doctor",
            "Run: aihil mcp-config > .mcp.json",
        ]
    )
    return next_steps


def schema(output: str | None = None, force: bool = False) -> dict[str, Any]:
    schema_text = config_schema_text()
    if output is None:
        return {
            "ok": True,
            "schema": schema_text,
        }

    path = Path(output)
    if path.exists() and not force:
        return {
            "ok": False,
            "error_type": "schema_exists",
            "summary": "AI-HIL configuration schema already exists. Use --force to overwrite it.",
            "path": str(path),
        }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(schema_text, encoding="utf-8")
    return {
        "ok": True,
        "summary": "AI-HIL configuration schema written.",
        "path": str(path),
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
    config_display_path = display_path(config, config.config_path)
    return {
        "ok": debugger_info.get("ok") is True,
        "tool": "aihil_doctor",
        "summary": "AI-HIL configuration loaded and debugger checked."
        if debugger_info.get("ok")
        else "AI-HIL configuration loaded, but debugger check failed.",
        "config_path": str(config.config_path),
        "mcp": {
            "transport": "stdio",
            "command": "aihil",
            "args": ["mcp-stdio", "--config", config_display_path],
        },
        "target": {
            "name": config.target.name,
            "controller": config.target.controller,
        },
        "com_ports": {
            port_id: {
                "device": port.device,
                "baudrate": port.baudrate,
                "encoding": port.encoding,
            }
            for port_id, port in config.com_ports.items()
        },
        "debugger": debugger_info,
    }


def mcp_config(config_path: str | None = None) -> dict[str, Any]:
    config_arg = str(Path(config_path)) if config_path else DEFAULT_CONFIG_PATH.as_posix()
    return {
        "mcpServers": {
            "aihil": {
                "command": "aihil",
                "args": ["mcp-stdio", "--config", config_arg],
            }
        }
    }


def mcp_stdio(config_path: str | None = None) -> int:
    try:
        config = load_config(config_path)
    except ConfigError as exc:
        print(json.dumps(exc.to_dict(), indent=2, sort_keys=True), file=sys.stderr)
        return 2
    return run_stdio_server(config, sys.stdin, sys.stdout)


def com_stdio(
    config_path: str | None,
    port_id: str,
    max_read_bytes: int | None,
    read_wait_timeout_s: float,
    eof_idle_timeout_s: float,
) -> int:
    try:
        config = load_config(config_path)
    except ConfigError as exc:
        print(json.dumps(exc.to_dict(), indent=2, sort_keys=True), file=sys.stderr)
        return 2
    return run_com_stdio(
        config,
        port_id,
        sys.stdin,
        sys.stdout,
        sys.stderr,
        max_read_bytes=max_read_bytes,
        read_wait_timeout_s=read_wait_timeout_s,
        eof_idle_timeout_s=eof_idle_timeout_s,
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
