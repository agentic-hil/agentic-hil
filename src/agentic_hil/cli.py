from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

from agentic_hil import __version__
from agentic_hil.comports import list_available_com_ports
from agentic_hil.comstdio import run_com_stdio
from agentic_hil.config import (
    DEFAULT_CONFIG_PATH,
    TRUSTED_POLICY_ENV,
    ConfigError,
    apply_trusted_policy,
    config_schema_text,
    display_path,
    is_path_within,
    load_config,
    load_trusted_policy,
)
from agentic_hil.debugger import create_debugger_backend
from agentic_hil.stdio import run_stdio_server
from agentic_hil.types import JsonObject

DEFAULT_CONFIG_TEMPLATE = """target:
  name: "example-target"
  controller: "unknown-controller"

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
  allow_all_symbols: true
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
  allow_upload: true

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
  allow_probe: true
  allow_flash: true
  allow_reset: true
  allow_com_read: true
  allow_com_write: true
  allow_can_read: true
  allow_can_write: true
  allow_adapter_read: true
  allow_adapter_write: true
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
        return 0 if result.get("ok") else 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agentic-hil", description="Agentic Hardware-in-the-Loop (Agentic HIL) local MCP stdio server")
    parser.add_argument("--version", action="version", version=__version__)
    subparsers = parser.add_subparsers(dest="command")

    init_parser = subparsers.add_parser("init", help="write starter .agentic-hil/config.yaml")
    init_parser.add_argument("--config", default=None)
    init_parser.add_argument("--force", action="store_true")

    doctor_parser = subparsers.add_parser("doctor", help="validate config and check debugger availability")
    doctor_parser.add_argument("--config", default=None)

    subparsers.add_parser("com-ports", help="list host serial/COM ports")

    mcp_parser = subparsers.add_parser("mcp-stdio", help="run MCP over stdio")
    mcp_parser.add_argument("--config", default=None)

    policy_parser = subparsers.add_parser("policy-init", help="write a host-managed trusted policy outside the workspace")
    policy_parser.add_argument("--output", required=True)
    policy_parser.add_argument("--force", action="store_true")

    com_stdio_parser = subparsers.add_parser("com-stdio", help="bind stdin/stdout to a configured COM port")
    com_stdio_parser.add_argument("--config", default=None)
    com_stdio_parser.add_argument("--port", required=True)
    com_stdio_parser.add_argument("--max-read-bytes", type=int, default=None)
    com_stdio_parser.add_argument("--read-wait-timeout-s", type=float, default=0.05)
    com_stdio_parser.add_argument("--eof-idle-timeout-s", type=float, default=0.5)

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
    if args.command == "doctor":
        return doctor(args.config)
    if args.command == "com-ports":
        return list_available_com_ports()
    if args.command == "mcp-stdio":
        config = load_config(args.config)
        policy = load_trusted_policy(os.environ.get(TRUSTED_POLICY_ENV), config.work_dir)
        return run_stdio_server(config, trusted_policy=policy)
    if args.command == "policy-init":
        return init_policy(args.output, args.force)
    if args.command == "com-stdio":
        config = load_config(args.config)
        return run_com_stdio(config, args.port, max_read_bytes=args.max_read_bytes, read_wait_timeout_s=args.read_wait_timeout_s, eof_idle_timeout_s=args.eof_idle_timeout_s)
    if args.command == "schema":
        return schema(args.output, args.force)
    if args.command == "mcp-config":
        return mcp_config(args.output, args.force)
    if args.command == "skill-install":
        return install_skill(args.agent, args.target, args.force)
    return {"ok": False, "error_type": "unknown_command", "summary": f"unknown command: {args.command}"}


def init_config(config_path: str | None = None, force: bool = False) -> JsonObject:
    target_path = Path(config_path or DEFAULT_CONFIG_PATH)
    if target_path.exists() and not force:
        return {"ok": False, "error_type": "config_exists", "summary": "Agentic HIL configuration already exists. Use --force to overwrite it.", "path": str(target_path)}
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text(DEFAULT_CONFIG_TEMPLATE, encoding="utf-8")
    try:
        load_config(str(target_path))
    except ConfigError as error:
        result = error.to_dict()
        result["summary"] = "Agentic HIL starter configuration was written but failed validation."
        result["path"] = str(target_path)
        return result
    available_com_ports = list_available_com_ports()
    return {"ok": True, "summary": "Agentic HIL starter configuration written.", "path": str(target_path), "available_com_ports": available_com_ports, "next_steps": init_next_steps(available_com_ports)}


def init_policy(output: str, force: bool = False) -> JsonObject:
    target_path = Path(output).expanduser()
    if not target_path.is_absolute():
        return {"ok": False, "error_type": "trusted_policy_invalid", "summary": "The trusted policy path must be absolute.", "path": output}
    target_path = target_path.resolve()
    if is_path_within(target_path, Path.cwd()):
        return {"ok": False, "error_type": "trusted_policy_invalid", "summary": "The trusted policy must be stored outside the current workspace.", "path": str(target_path)}
    if target_path.exists() and not force:
        return {"ok": False, "error_type": "policy_exists", "summary": "Trusted policy already exists. Use --force to overwrite it.", "path": str(target_path)}
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text(default_trusted_policy_template(), encoding="utf-8")
    return {
        "ok": True,
        "summary": "Host-managed trusted policy written. Review it, keep it outside the workspace, and set AGENTIC_HIL_POLICY in the MCP host environment.",
        "path": str(target_path),
    }


def default_trusted_policy_template() -> str:
    text = DEFAULT_CONFIG_TEMPLATE.replace("  allow_upload: true", "  allow_upload: false")
    text = text.replace("  allow_all_symbols: true", "  allow_all_symbols: false")
    for field in [
        "allow_probe",
        "allow_flash",
        "allow_reset",
        "allow_com_read",
        "allow_com_write",
        "allow_can_read",
        "allow_can_write",
        "allow_adapter_read",
        "allow_adapter_write",
    ]:
        text = text.replace(f"  {field}: true", f"  {field}: false")
    return text


def init_next_steps(available_com_ports: JsonObject) -> list[str]:
    next_steps = [
        "Keep this .agentic-hil/config.yaml with the firmware project; install Agentic HIL once with pipx or python -m pip --user.",
        "Ask the human operator to create and review a trusted policy outside the project with agentic-hil policy-init.",
        "Edit target.name and target.controller for your board.",
        "Set debugger.interface_cfg and debugger.target_cfg for your OpenOCD setup.",
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
            "For sensor/actuator/fault simulation, add a named test adapter under adapters.",
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
        config = load_config(config_path)
        policy = load_trusted_policy(os.environ.get(TRUSTED_POLICY_ENV), config.work_dir)
        config = apply_trusted_policy(config, policy)
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
            "summary": "Debugger check skipped because allow_probe is disabled by the effective policy.",
        }
    config_display_path = display_path(config, config.config_path)
    return {
        "ok": debugger_info.get("ok") is True,
        "tool": "agentic_hil_doctor",
        "summary": "Agentic HIL configuration and trusted policy loaded; debugger check skipped." if debugger_info.get("skipped") else ("Agentic HIL configuration loaded and debugger checked." if debugger_info.get("ok") else "Agentic HIL configuration loaded, but debugger check failed."),
        "config_path": config.config_path,
        "trusted_policy": "loaded",
        "mcp": {"transport": "stdio", "command": "agentic-hil", "args": ["mcp-stdio", "--config", config_display_path]},
        "target": {"name": config.target.name, "controller": config.target.controller},
        "com_ports": {port_id: {"device": port.device, "baudrate": port.baudrate, "encoding": port.encoding} for port_id, port in config.com_ports.items()},
        "can_buses": {bus_id: {"adapter": bus.adapter, "channel": bus.channel, "bitrate": bus.bitrate, "fd": bus.fd} for bus_id, bus in config.can_buses.items()},
        "adapters": {adapter_id: {"executable": adapter.executable, "channels": adapter.channels, "faults": adapter.faults} for adapter_id, adapter in config.adapters.items()},
        "debugger": debugger_info,
    }


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
