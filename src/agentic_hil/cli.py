from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from contextlib import suppress
from copy import deepcopy
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

import yaml

from agentic_hil import __version__
from agentic_hil.comports import list_available_com_ports
from agentic_hil.comstdio import run_com_stdio
from agentic_hil.config import (
    CONFIG_ENV,
    ConfigError,
    UniqueKeyLoader,
    absolute_without_symlinks,
    config_schema_text,
    is_path_within_frozen,
    load_authoritative_config,
    load_config,
    pin_configured_executables,
    pin_configured_paths,
    project_config_path,
    safe_read_text,
    validate_config_schema,
)
from agentic_hil.debugger import create_debugger_backend
from agentic_hil.report import write_report
from agentic_hil.stdio import run_stdio_server
from agentic_hil.test_reactor import DEFAULT_TEST_CONFIG_PATH, TestReactor, load_test_config
from agentic_hil.tools import AgenticHILToolService
from agentic_hil.types import AgenticHILConfig, JsonObject

DEFAULT_CONFIG_TEMPLATE = """target:
  name: "example-target"
  controller: "unknown-controller"

devices:
  dut:
    debugger: true
    uart: null

debugger:
  type: "openocd"
  executable: null
  probe_id: null
  target_type: null
  interface_cfg: "interface/stlink.cfg"
  target_cfg: "target/stm32f4x.cfg"
  timeout_s: 60

debug:
  gdb_executable: null
  allowed_symbols: []
  allow_all_symbols: false
  max_dump_size_bytes: 1048576

artifacts:
  allowed_roots:
    - "build"
  upload_directory: ".agentic-hil/artifacts"
  allowed_extensions:
    - ".elf"
    - ".hex"
    - ".bin"
  max_upload_size_mb: 64
  allow_upload: false

com_ports: {}

can_buses: {}

adapters: {}

validation:
  require_existing_file: true
  require_allowed_root: true
  require_allowed_extension: true
  compute_sha256: true
  inspect_known_formats: true

permissions:
  allow_probe: false
  allow_flash: false
  allow_reset: false
  allow_com_read: false
  allow_com_write: false
  allow_can_read: false
  allow_can_write: false
  allow_adapter_read: false
  allow_adapter_write: false
  allow_raw_debugger_commands: false
  allow_mass_erase: false

reports:
  directory: ".agentic-hil/reports"

logs:
  directory: ".agentic-hil/logs"
"""

SKILL_NAME = "agentic-hil-config-setup"
SKILL_FILE = "SKILL.md"
AGENTIC_HIL_REGISTRATION_START = "<!-- Agentic HIL skill registration start -->"
AGENTIC_HIL_REGISTRATION_END = "<!-- Agentic HIL skill registration end -->"


@dataclass(frozen=True)
class SkillAgent:
    id: str
    display_name: str
    aliases: tuple[str, ...]
    default_target_path: str
    registration: str


def skill_agents() -> list[SkillAgent]:
    home = Path.home()
    return [
        SkillAgent("opencode", "opencode", ("opencode", "open-code"), str(home / ".config" / "opencode" / "skills" / SKILL_NAME / SKILL_FILE), "skills-directory"),
        SkillAgent("claude-code", "Claude Code", ("claude-code", "claude", "claude_code"), str(home / ".claude" / "skills" / SKILL_NAME / SKILL_FILE), "skills-directory"),
        SkillAgent("codex", "Codex", ("codex", "codex-cli", "openai-codex"), str(home / ".codex" / "skills" / SKILL_NAME / SKILL_FILE), "agents-md"),
    ]


def entrypoint(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help(sys.stderr)
        return 2
    try:
        result = dispatch(args)
    except ConfigError as error:
        print_json(error.to_dict())
        return 1
    if isinstance(result, int):
        return result
    if result is not None:
        print_json(result)
        return 0 if result_succeeded(result) else 1
    return 0


def result_succeeded(result: JsonObject) -> bool:
    return result.get("ok") is True and result.get("audit_ok") is not False and result.get("target_ok") is not False


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agentic-hil", description="Agentic Hardware-in-the-Loop (Agentic HIL) local MCP stdio server")
    parser.add_argument("--version", action="version", version=__version__)
    subparsers = parser.add_subparsers(dest="command")

    init_parser = subparsers.add_parser("init", help="write a deny-by-default authoritative config for the current workspace")
    init_parser.add_argument("--config", default=None, help=argparse.SUPPRESS)
    init_parser.add_argument("--force", action="store_true")

    migrate_parser = subparsers.add_parser("migrate-config", help="migrate a 0.2.3 workspace config into the authoritative policy location")
    migrate_parser.add_argument("--from", dest="source", default=".agentic-hil/config.yaml")

    doctor_parser = subparsers.add_parser("doctor", help="validate config and check debugger availability")
    doctor_parser.add_argument("--config", default=None, help=argparse.SUPPRESS)

    subparsers.add_parser("debugger-probes", help="list connected probe IDs for the configured debugger backend")

    subparsers.add_parser("com-ports", help="list host serial/COM ports")

    mcp_stdio_parser = subparsers.add_parser("mcp-stdio", help="run MCP over stdio")
    mcp_stdio_parser.add_argument("--config", default=None, help=argparse.SUPPRESS)

    com_stdio_parser = subparsers.add_parser("com-stdio", help="bind stdin/stdout to a configured COM port")
    com_stdio_parser.add_argument("--config", default=None, help=argparse.SUPPRESS)
    com_stdio_parser.add_argument("--port", required=True)
    com_stdio_parser.add_argument("--max-read-bytes", type=int, default=None)
    com_stdio_parser.add_argument("--read-wait-timeout-s", type=float, default=0.05)
    com_stdio_parser.add_argument("--eof-idle-timeout-s", type=float, default=0.5)

    reactor_parser = subparsers.add_parser("test-reactor", help="run a validated hardware test sequence")
    reactor_parser.add_argument("--test-config", default=DEFAULT_TEST_CONFIG_PATH)

    schema_parser = subparsers.add_parser("schema", help="print or write bundled config schema")
    schema_parser.add_argument("--output", default=None)
    schema_parser.add_argument("--force", action="store_true")

    mcp_config_parser = subparsers.add_parser("mcp-config", help="print or write project .mcp.json for MCP client discovery")
    mcp_config_parser.add_argument("--output", default=None)
    mcp_config_parser.add_argument("--force", action="store_true")

    skill_parser = subparsers.add_parser("skill-install", help="install/update the Agentic HIL agent setup skill")
    skill_parser.add_argument("--agent", default="opencode")
    skill_parser.add_argument("--target", default=None)
    skill_parser.add_argument("--force", action="store_true")

    return parser


def dispatch(args: argparse.Namespace) -> JsonObject | int | None:
    if args.command == "init":
        return init_config(args.config, args.force)
    if args.command == "migrate-config":
        return migrate_config(args.source)
    if args.command == "doctor":
        return doctor(args.config)
    if args.command == "debugger-probes":
        return debugger_probes()
    if args.command == "com-ports":
        return list_available_com_ports()
    if args.command == "mcp-stdio":
        return run_stdio_server(load_cli_authoritative_config(args.config))
    if args.command == "com-stdio":
        config = load_cli_authoritative_config(args.config)
        return run_com_stdio(config, args.port, max_read_bytes=args.max_read_bytes, read_wait_timeout_s=args.read_wait_timeout_s, eof_idle_timeout_s=args.eof_idle_timeout_s)
    if args.command == "test-reactor":
        return run_test_reactor(args.test_config)
    if args.command == "schema":
        return schema(args.output, args.force)
    if args.command == "mcp-config":
        return mcp_config(args.output, args.force)
    if args.command == "skill-install":
        return install_skill(args.agent, args.target, args.force)
    return {"ok": False, "error_type": "unknown_command", "summary": f"unknown command: {args.command}"}


def init_config(config_path: str | None = None, force: bool = False) -> JsonObject:
    workspace = Path.cwd().resolve()
    target_path = initialized_config_path(workspace)
    validate_legacy_config_selector(config_path, workspace, target_path)
    if target_path.exists() and not force:
        return {"ok": False, "error_type": "config_exists", "summary": "Agentic HIL configuration already exists. Use --force to overwrite it.", "path": str(target_path)}
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text(f"workspace_root: {json.dumps(str(workspace))}\n\n{DEFAULT_CONFIG_TEMPLATE}", encoding="utf-8")
    previous = os.environ.pop(CONFIG_ENV, None)
    try:
        load_authoritative_config(workspace)
    except ConfigError as error:
        result = error.to_dict()
        result["summary"] = "Agentic HIL starter configuration was written but failed validation."
        result["path"] = str(target_path)
        return result
    finally:
        if previous is not None:
            os.environ[CONFIG_ENV] = previous
    available_com_ports = list_available_com_ports()
    return {
        "ok": True,
        "summary": "Deny-by-default Agentic HIL project configuration written.",
        "path": str(target_path),
        "optional_override": f'{CONFIG_ENV}={target_path}',
        "available_com_ports": available_com_ports,
        "next_steps": init_next_steps(available_com_ports, target_path),
    }


def initialized_config_path(workspace: Path) -> Path:
    return project_config_path(workspace)


def migrate_config(source: str) -> JsonObject:
    workspace = Path.cwd().resolve()
    requested_source = Path(source).expanduser()
    source_path = absolute_without_symlinks(requested_source if requested_source.is_absolute() else workspace / requested_source)
    source_in_workspace = is_path_within_frozen(source_path, workspace)
    if not requested_source.is_absolute() and not source_in_workspace:
        raise ConfigError("config_migration_required", "Migration source must be inside the current workspace.", {"path": str(source_path), "workspace_root": str(workspace)})
    target_path = project_config_path(workspace)
    if target_path.exists():
        return {"ok": False, "error_type": "config_exists", "summary": "Authoritative Agentic HIL configuration already exists; migration will not overwrite it.", "path": str(target_path)}
    try:
        loaded = yaml.load(safe_read_text(source_path, workspace=workspace if source_in_workspace else None), Loader=UniqueKeyLoader)
    except FileNotFoundError as error:
        raise ConfigError("config_file_not_found", "Legacy Agentic HIL configuration could not be found.", {"path": str(source_path)}) from error
    except UnicodeDecodeError as error:
        raise ConfigError("config_invalid", "Legacy Agentic HIL configuration is not valid UTF-8 text.", {"path": str(source_path)}) from error
    except yaml.YAMLError as error:
        raise ConfigError("config_invalid", "Legacy Agentic HIL configuration is not valid YAML.", yaml_error_details(error, source_path)) from error
    if loaded is None:
        loaded = {}
    if not isinstance(loaded, dict):
        raise ConfigError("config_invalid", "Legacy Agentic HIL configuration root must be a mapping.", {"path": str(source_path)})
    legacy = deepcopy(loaded)
    strip_empty_legacy_args(legacy, str(source_path))
    base = yaml.safe_load(DEFAULT_CONFIG_TEMPLATE) or {}
    migrated = deep_merge(base, legacy)
    migrated["workspace_root"] = str(workspace)
    migrated.setdefault("debug", {})["allow_all_symbols"] = False
    migrated.setdefault("artifacts", {})["allow_upload"] = False
    migrated["permissions"] = deny_all_permissions()
    validate_config_schema(migrated, str(target_path))
    target_path.parent.mkdir(parents=True, exist_ok=True)
    text = yaml.safe_dump(migrated, sort_keys=False)
    validate_migrated_text(text, target_path, workspace)
    write_exclusive_text(target_path, text)
    previous = os.environ.pop(CONFIG_ENV, None)
    try:
        load_authoritative_config(workspace)
    except Exception:
        with suppress(FileNotFoundError):
            target_path.unlink()
        raise
    finally:
        if previous is not None:
            os.environ[CONFIG_ENV] = previous
    return {
        "ok": True,
        "summary": "Legacy Agentic HIL configuration migrated to the external authoritative policy location.",
        "source_path": str(source_path),
        "path": str(target_path),
        "warnings": [
            "All hardware permissions were set to false and require operator review.",
            "artifacts.allow_upload and debug.allow_all_symbols were set to false.",
            "Run agentic-hil doctor after reviewing and enabling only required capabilities.",
        ],
    }


def deep_merge(base: JsonObject, updates: JsonObject) -> JsonObject:
    result = deepcopy(base)
    for key, value in updates.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result


def strip_empty_legacy_args(raw: JsonObject, source_path: str) -> None:
    for section in ("can_buses", "adapters"):
        entries = raw.get(section)
        if not isinstance(entries, dict):
            continue
        for name, value in entries.items():
            if not isinstance(value, dict) or "args" not in value:
                continue
            args = value.get("args")
            if args:
                raise ConfigError(
                    "config_migration_required",
                    "Process bridge args cannot be migrated safely. Pin an operator-controlled wrapper directly as executable.",
                    {"path": source_path, "field": f"{section}.{name}.args"},
                )
            value.pop("args", None)


def deny_all_permissions() -> JsonObject:
    return {
        "allow_probe": False,
        "allow_flash": False,
        "allow_reset": False,
        "allow_com_read": False,
        "allow_com_write": False,
        "allow_can_read": False,
        "allow_can_write": False,
        "allow_adapter_read": False,
        "allow_adapter_write": False,
        "allow_raw_debugger_commands": False,
        "allow_mass_erase": False,
    }


def validate_migrated_text(text: str, target_path: Path, workspace: Path) -> None:
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=target_path.parent, delete=False) as handle:
        temporary_path = Path(handle.name)
        handle.write(text)
    try:
        pin_configured_executables(pin_configured_paths(load_config(str(temporary_path), str(workspace))))
    finally:
        with suppress(FileNotFoundError):
            temporary_path.unlink()


def write_exclusive_text(path: Path, text: str) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    descriptor = -1
    created = False
    try:
        descriptor = os.open(path, flags, 0o600)
        created = True
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        if created:
            with suppress(FileNotFoundError):
                path.unlink()
        raise
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def yaml_error_details(error: yaml.YAMLError, path: Path) -> JsonObject:
    details: JsonObject = {"path": str(path)}
    mark = getattr(error, "problem_mark", None)
    if mark is not None:
        details.update({"line": mark.line + 1, "column": mark.column + 1})
    return details


def run_test_reactor(test_config_path: str | None = None) -> JsonObject:
    config = load_authoritative_config(Path.cwd())
    test_config = load_test_config(test_config_path, config.work_dir)
    service = AgenticHILToolService(config)
    try:
        result = TestReactor(service.config, service).run(test_config)
    except Exception:
        with suppress(Exception):
            service.close()
        raise
    try:
        service.close()
    except Exception as error:
        cleanup_error = {
            "device": "service",
            "action": "close",
            "result": {
                "ok": False,
                "tool": "test_reactor",
                "error_type": "cleanup_exception",
                "summary": "Agentic HIL service cleanup raised an exception.",
                "exception_type": type(error).__name__,
                "backend_error": str(error),
            },
        }
        result["ok"] = False
        result["cleanup_ok"] = False
        result.setdefault("cleanup", []).append(cleanup_error)
        result.setdefault("cleanup_errors", []).append(cleanup_error)
        result.setdefault("step_error_type", result.get("error_type"))
        result["error_type"] = "cleanup_failed"
        result["summary"] = "Test reactor sequence failed during cleanup."
    return write_report(config, result)


def init_next_steps(available_com_ports: JsonObject, config_path: Path) -> list[str]:
    next_steps = [
        f"Review the deny-by-default config at {config_path}. Set {CONFIG_ENV} only when an explicit absolute-path override is needed.",
        "Edit target.name and target.controller for your board.",
        "Set debugger.interface_cfg and debugger.target_cfg for your OpenOCD setup.",
        "Configure devices with the debugger and optional UART used by test-reactor sequences.",
        "If multiple debug probes are connected, set debugger.probe_id to the intended probe serial number.",
    ]
    if available_com_ports.get("ok"):
        ports = available_com_ports.get("ports", [])
        if ports:
            devices = ", ".join(str(port.get("device", "")) for port in ports[:5])
            suffix = "" if len(ports) <= 5 else f", and {len(ports) - 5} more"
            next_steps.append(f"Detected COM ports: {devices}{suffix}. Add the DUT UART under com_ports if serial feedback is needed.")
        else:
            next_steps.append("No host COM ports detected. Connect USB serial hardware and run: agentic-hil com-ports")
    else:
        next_steps.append("COM port discovery failed. Run: agentic-hil com-ports after checking the pyserial installation.")
    next_steps.extend(
        [
            "For CAN access, add a named bus under can_buses.",
            "Run: agentic-hil doctor",
            "Create or update .mcp.json if your MCP client needs project discovery.",
        ]
    )
    return next_steps


def schema(output: str | None = None, force: bool = False) -> JsonObject:
    text = config_schema_text()
    if output is None:
        sys.stdout.write(text)
        return {"ok": True}
    output_path = Path(output)
    if output_path.exists() and not force:
        return {"ok": False, "error_type": "schema_exists", "summary": "Agentic HIL configuration schema already exists. Use --force to overwrite it.", "path": output}
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(text, encoding="utf-8")
    return {"ok": True, "summary": "Agentic HIL configuration schema written.", "path": output}


def mcp_config_text() -> str:
    return resources.files("agentic_hil").joinpath("templates", "mcp.json").read_text(encoding="utf-8")


def mcp_config(output: str | None = None, force: bool = False) -> JsonObject:
    text = mcp_config_text()
    if output is None:
        sys.stdout.write(text)
        return {"ok": True}
    output_path = Path(output)
    if output_path.exists() and not force:
        return {"ok": False, "error_type": "mcp_config_exists", "summary": "MCP configuration already exists. Use --force to overwrite it.", "path": output}
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(text, encoding="utf-8")
    return {"ok": True, "summary": "Agentic HIL MCP configuration written.", "path": output}


def doctor(config_path: str | None = None) -> JsonObject:
    try:
        config = load_cli_authoritative_config(config_path)
    except ConfigError as error:
        result = error.to_dict()
        result["tool"] = "agentic_hil_doctor"
        return result
    if config.permissions.allow_probe:
        backend = create_debugger_backend(config)
        try:
            debugger_info = backend.info()
        finally:
            backend.close()
    else:
        debugger_info = {
            "ok": True,
            "tool": "debugger_info",
            "skipped": True,
            "summary": "Debugger check skipped because allow_probe is disabled by the authoritative config.",
        }
    return {
        "ok": debugger_info.get("ok") is True,
        "tool": "agentic_hil_doctor",
        "summary": "Agentic HIL authoritative configuration loaded; debugger check skipped." if debugger_info.get("skipped") else ("Agentic HIL configuration loaded and debugger checked." if debugger_info.get("ok") else "Agentic HIL configuration loaded, but debugger check failed."),
        "config_path": config.config_path,
        "mcp": {"transport": "stdio", "command": "agentic-hil", "args": ["mcp-stdio"]},
        "target": {"name": config.target.name, "controller": config.target.controller},
        "devices": {device_id: {"debugger": device.debugger, "uart": device.uart} for device_id, device in config.devices.items()},
        "com_ports": {port_id: {"device": port.device, "baudrate": port.baudrate, "encoding": port.encoding} for port_id, port in config.com_ports.items()},
        "can_buses": {bus_id: {"adapter": bus.adapter, "channel": bus.channel, "bitrate": bus.bitrate, "fd": bus.fd} for bus_id, bus in config.can_buses.items()},
        "debugger": debugger_info,
    }


def debugger_probes() -> JsonObject:
    service = AgenticHILToolService(load_authoritative_config(Path.cwd()))
    try:
        return service.call("debugger_probes_list")
    finally:
        service.close()


def load_cli_authoritative_config(config_path: str | None = None) -> AgenticHILConfig:
    workspace = Path.cwd().resolve()
    expected_path = Path(os.environ.get(CONFIG_ENV) or project_config_path(workspace)).expanduser().resolve()
    validate_legacy_config_selector(config_path, workspace, expected_path)
    return load_authoritative_config(workspace)


def validate_legacy_config_selector(config_path: str | None, workspace: Path, expected_path: Path) -> None:
    if config_path is None:
        return
    selected = Path(config_path).expanduser()
    selected = (selected if selected.is_absolute() else workspace / selected).resolve()
    if selected != expected_path:
        raise ConfigError(
            "config_migration_required",
            f"--config can no longer select repository-controlled policy. Set {CONFIG_ENV} to an absolute external config path or remove --config.",
            {"selected_path": str(selected), "authoritative_path": str(expected_path)},
        )


def install_skill(agent: str | None = None, target: str | None = None, force: bool = False) -> JsonObject:
    requested_agent = agent or "opencode"
    resolved_agent = resolve_skill_agent(requested_agent)
    if resolved_agent is None and target is None:
        return {"ok": False, "error_type": "unsupported_agent", "summary": "Agentic HIL does not know this agent's default skill directory. Provide --target to install anyway.", "agent": normalize_agent(requested_agent), "allowed_agents": supported_skill_agents()}
    agent_id = resolved_agent.id if resolved_agent else normalize_agent(requested_agent)
    agent_name = resolved_agent.display_name if resolved_agent else agent_id
    source_path = bundled_skill_path()
    target_path = Path(target or resolved_agent.default_target_path)  # type: ignore[union-attr]
    source_text = source_path.read_text(encoding="utf-8")
    source_version = skill_version(source_text) or __version__
    if target_path.exists():
        existing_text = target_path.read_text(encoding="utf-8")
        if existing_text == source_text:
            registration = register_skill(resolved_agent, str(target_path), source_version, requested_agent)
            return {"ok": True, "summary": f"Agentic HIL {agent_name} skill is already installed.", "agent": agent_id, "requested_agent": requested_agent, "skill": SKILL_NAME, "source_path": str(source_path), "target_path": str(target_path), "version": source_version, "installed": False, "updated": False, "registered": registration.get("ok") is True if registration else False, "registration": registration}
        existing_version = skill_version(existing_text)
        if is_agentic_hil_setup_skill(existing_text) and existing_version != source_version:
            target_path.parent.mkdir(parents=True, exist_ok=True)
            target_path.write_text(source_text, encoding="utf-8")
            registration = register_skill(resolved_agent, str(target_path), source_version, requested_agent)
            return {"ok": True, "summary": f"Agentic HIL {agent_name} skill updated to match the current CLI package.", "agent": agent_id, "requested_agent": requested_agent, "skill": SKILL_NAME, "source_path": str(source_path), "target_path": str(target_path), "previous_version": existing_version, "version": source_version, "installed": False, "updated": True, "registered": registration.get("ok") is True if registration else False, "registration": registration}
        if not force:
            return {"ok": False, "error_type": "skill_exists", "summary": "Target skill file already exists with different content and no CLI-version drift. Use --force to overwrite it.", "agent": agent_id, "requested_agent": requested_agent, "skill": SKILL_NAME, "source_path": str(source_path), "target_path": str(target_path), "existing_version": existing_version, "version": source_version}
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text(source_text, encoding="utf-8")
    registration = register_skill(resolved_agent, str(target_path), source_version, requested_agent)
    return {"ok": True, "summary": f"Agentic HIL {agent_name} skill installed.", "agent": agent_id, "requested_agent": requested_agent, "skill": SKILL_NAME, "source_path": str(source_path), "target_path": str(target_path), "version": source_version, "installed": True, "updated": False, "registered": registration.get("ok") is True if registration else False, "registration": registration}


def bundled_skill_path() -> Path:
    return resources.files("agentic_hil").joinpath("skills", SKILL_NAME, SKILL_FILE)


def skill_version(text: str) -> str | None:
    match = re.search(r'^  agentic_hil_version: "([^"]+)"$', text, re.MULTILINE)
    return match.group(1) if match else None


def is_agentic_hil_setup_skill(text: str) -> bool:
    return re.search(rf"^name: {re.escape(SKILL_NAME)}$", text, re.MULTILINE) is not None and re.search(r"^  origin: Agentic HIL$", text, re.MULTILINE) is not None


def normalize_agent(agent: str) -> str:
    return agent.strip().lower().replace("_", "-")


def resolve_skill_agent(agent: str) -> SkillAgent | None:
    normalized = normalize_agent(agent)
    return next((candidate for candidate in skill_agents() if normalized in {normalize_agent(alias) for alias in candidate.aliases}), None)


def supported_skill_agents() -> list[str]:
    return [agent.id for agent in skill_agents()]


def register_skill(agent: SkillAgent | None, target_path: str, version: str, requested_agent: str) -> JsonObject | None:
    if agent is None:
        return {"ok": False, "mode": "explicit-target", "summary": "No automatic agent registration is known for this agent. The skill was written to the explicit target path."}
    if agent.registration == "skills-directory":
        return {"ok": True, "mode": "skills-directory", "summary": f"{agent.display_name} discovers installed skills from its skills directory.", "path": str(Path(target_path).parent)}
    registration_path = Path(skill_install_root(target_path)) / "AGENTS.md"
    result = upsert_marked_block(registration_path, codex_registration_block(target_path, version, requested_agent))
    return {"ok": True, "mode": "agents-md", "summary": f"{agent.display_name} registration written to AGENTS.md.", "path": str(registration_path), "updated": result["updated"]}


def skill_install_root(target_path: str) -> str:
    path = Path(target_path)
    if path.name == SKILL_FILE and path.parent.name == SKILL_NAME and path.parent.parent.name == "skills":
        return str(path.parent.parent.parent)
    return str(path.parent)


def codex_registration_block(target_path: str, version: str, requested_agent: str) -> str:
    return f"""{AGENTIC_HIL_REGISTRATION_START}
## Agentic HIL Skill

- Skill path: `{target_path}`
- Agentic HIL version: `{version}`
- Agentic HIL is for embedded firmware development with local hardware-in-the-loop targets.
- For Agentic HIL setup, configuration, MCP, or embedded hardware workflows, read and follow this skill before acting.
- If this version differs from `agentic-hil --version`, run `agentic-hil skill-install --agent {requested_agent}`.
{AGENTIC_HIL_REGISTRATION_END}"""


def upsert_marked_block(file_path: Path, block: str) -> JsonObject:
    file_path.parent.mkdir(parents=True, exist_ok=True)
    existing = file_path.read_text(encoding="utf-8") if file_path.exists() else ""
    pattern = re.compile(rf"{re.escape(AGENTIC_HIL_REGISTRATION_START)}[\s\S]*?{re.escape(AGENTIC_HIL_REGISTRATION_END)}")
    trimmed = existing.rstrip()
    separator = "\n\n" if trimmed else ""
    next_text = pattern.sub(block, existing) if pattern.search(existing) else f"{trimmed}{separator}{block}\n"
    if next_text != existing:
        file_path.write_text(next_text, encoding="utf-8")
        return {"updated": True}
    return {"updated": False}


def print_json(value: JsonObject) -> None:
    sys.stdout.write(json.dumps(value, indent=2) + "\n")
