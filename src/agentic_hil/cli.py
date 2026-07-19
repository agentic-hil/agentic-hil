from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

from agentic_hil import __version__
from agentic_hil.comports import list_available_com_ports
from agentic_hil.comstdio import run_com_stdio
from agentic_hil.config import DEFAULT_CONFIG_PATH, ConfigError, config_schema_text, display_path, load_config
from agentic_hil.debugger import create_debugger_backend
from agentic_hil.hardware_lock import HardwareLockError, ProjectHardwareLock, marker_owner_is_alive
from agentic_hil.report import write_report
from agentic_hil.stdio import run_stdio_server
from agentic_hil.test_reactor import TestReactor, load_test_config, test_config_schema_text
from agentic_hil.tools import AgenticHILToolService
from agentic_hil.types import AgenticHILConfig, JsonObject

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

debuggers: {}

devices:
  dut:
    debugger: "default"
    uart: null

debug:
  gdb_executable: null
  allowed_symbols: []
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
    except HardwareLockError as error:
        print_json({"ok": False, "error_type": "hardware_lock_failed", "backend_error": str(error), "summary": "Project hardware state storage is unavailable."})
        return 1
    except KeyboardInterrupt:
        return 130
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

    debugger_probes_parser = subparsers.add_parser("debugger-probes", help="list connected probe IDs for the configured debugger backend")
    debugger_probes_parser.add_argument("--config", default=None)
    debugger_probes_parser.add_argument("--debugger", default="default", help="named debugger from config, or default")

    subparsers.add_parser("com-ports", help="list host serial/COM ports")

    mcp_parser = subparsers.add_parser("mcp-stdio", help="run MCP over stdio")
    mcp_parser.add_argument("--config", default=None)

    com_stdio_parser = subparsers.add_parser("com-stdio", help="bind stdin/stdout to a configured COM port")
    com_stdio_parser.add_argument("--config", default=None)
    com_stdio_parser.add_argument("--port", required=True)
    com_stdio_parser.add_argument("--max-read-bytes", type=int, default=None)
    com_stdio_parser.add_argument("--read-wait-timeout-s", type=float, default=0.05)
    com_stdio_parser.add_argument("--eof-idle-timeout-s", type=float, default=0.5)

    reactor_parser = subparsers.add_parser("test-reactor", help="run validated hardware tests across configured devices")
    reactor_parser.add_argument("--config", default=None)
    reactor_parser.add_argument("--test-config", required=True, help="explicit test configuration path; ~ expands to the user home directory")

    hardware_status_parser = subparsers.add_parser("hardware-status", help="show project hardware lock and quarantine status")
    hardware_status_parser.add_argument("--config", default=None)

    hardware_recover_parser = subparsers.add_parser("hardware-recover", help="clear project hardware quarantine after operator inspection")
    hardware_recover_parser.add_argument("--config", default=None)
    hardware_recover_parser.add_argument("--quarantine-id", default=None)
    hardware_recover_parser.add_argument("--acknowledge-hardware-checked", action="store_true")
    hardware_recover_parser.add_argument("--force-live-owner", action="store_true", help="emergency override after independently stopping or isolating the source process")

    schema_parser = subparsers.add_parser("schema", help="print or write bundled config schema")
    schema_parser.add_argument("--output", default=None)
    schema_parser.add_argument("--force", action="store_true")

    test_schema_parser = subparsers.add_parser("test-schema", help="print or write bundled test configuration schema")
    test_schema_parser.add_argument("--output", default=None)
    test_schema_parser.add_argument("--force", action="store_true")

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
    if args.command == "debugger-probes":
        return debugger_probes(args.config, args.debugger)
    if args.command == "com-ports":
        return list_available_com_ports()
    if args.command == "mcp-stdio":
        config = load_config(args.config)
        return run_stdio_server(config)
    if args.command == "com-stdio":
        config = load_config(args.config)
        return run_com_stdio(config, args.port, max_read_bytes=args.max_read_bytes, read_wait_timeout_s=args.read_wait_timeout_s, eof_idle_timeout_s=args.eof_idle_timeout_s)
    if args.command == "test-reactor":
        return run_test_reactor(args.config, args.test_config)
    if args.command == "hardware-status":
        return hardware_status(args.config)
    if args.command == "hardware-recover":
        return hardware_recover(args.config, args.acknowledge_hardware_checked, args.quarantine_id, args.force_live_owner)
    if args.command == "schema":
        return schema(args.output, args.force)
    if args.command == "test-schema":
        return test_schema(args.output, args.force)
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


def init_next_steps(available_com_ports: JsonObject) -> list[str]:
    next_steps = [
        "Keep this .agentic-hil/config.yaml with the firmware project; install Agentic HIL once with pipx or python -m pip --user.",
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


def debugger_probes(config_path: str | None = None, debugger_name: str = "default") -> JsonObject:
    service = AgenticHILToolService(load_config(config_path))
    try:
        return service.call("debugger_probes_list", {"debugger": debugger_name})
    finally:
        service.close()


def run_test_reactor(config_path: str | None, test_config_path: str) -> JsonObject:
    config = load_config(config_path)
    plan = load_test_config(test_config_path, config.work_dir)
    return TestReactor(config).run(plan)


def resolve_hardware_config_path(config_path: str | None) -> str:
    """Resolve the lock/marker identity exactly like load_config, but without parsing the policy file.

    Incident inspection and recovery must keep working when the project config is
    invalid, unreadable, or deleted.
    """
    from agentic_hil.config import resolve_config_path

    requested = Path(resolve_config_path(config_path)).expanduser()
    resolved = requested if requested.is_absolute() else Path.cwd().resolve() / requested
    return str(resolved.resolve())


def hardware_status(config_path: str | None = None) -> JsonObject:
    try:
        hardware_lock = ProjectHardwareLock(resolve_hardware_config_path(config_path))
        status = hardware_lock.status()
    except HardwareLockError as error:
        return {"ok": False, "tool": "hardware_status", "error_type": "hardware_status_failed", "backend_error": str(error), "summary": "Project hardware lock state could not be inspected."}
    state = status.get("state")
    quarantine_id = state.get("quarantine_id") if isinstance(state, dict) else None
    return {"tool": "hardware_status", **status, "quarantine_id": quarantine_id, "quarantine": state if status.get("quarantined") else None}


def hardware_recover(config_path: str | None = None, acknowledge_hardware_checked: bool = False, quarantine_id: str | None = None, force_live_owner: bool = False) -> JsonObject:
    try:
        config = load_config(config_path)
    except ConfigError:
        config = None
    if not acknowledge_hardware_checked:
        return {"ok": False, "tool": "hardware_recover", "error_type": "acknowledgement_required", "summary": "Use --acknowledge-hardware-checked after confirming hardware is safe."}
    if not quarantine_id:
        return {"ok": False, "tool": "hardware_recover", "error_type": "quarantine_id_required", "summary": "Use --quarantine-id from hardware-status after confirming the exact hardware state is safe."}
    try:
        hardware_lock = ProjectHardwareLock(resolve_hardware_config_path(config_path))
        acquired = hardware_lock.acquire(recovery=True, source="hardware_recover")
    except HardwareLockError as error:
        result = {"ok": False, "tool": "hardware_recover", "error_type": "hardware_lock_failed", "summary": "Project hardware lease could not be acquired for recovery.", "backend_error": str(error)}
        return write_hardware_recovery_report(config, result)
    if not acquired:
        result = {"ok": False, "tool": "hardware_recover", "error_type": "hardware_busy", "summary": "Project hardware is in use by another Agentic HIL process."}
        return write_hardware_recovery_report(config, result)
    try:
        previous = hardware_lock.quarantine_info()
        if previous is not None and previous.get("recovery_blocked") is True:
            result = {"ok": False, "tool": "hardware_recover", "error_type": "hardware_state_unreadable", "summary": "Hardware state marker is unreadable; repair state-file access before recovery.", "state": previous}
            return write_hardware_recovery_report(config, result)
        if previous is None or previous.get("quarantine_id", previous.get("lease_id")) != quarantine_id:
            result = {"ok": False, "tool": "hardware_recover", "error_type": "quarantine_changed", "summary": "Hardware state marker changed after operator inspection.", "state": previous}
            return write_hardware_recovery_report(config, result)
        if not force_live_owner and marker_owner_is_alive(previous):
            result = {"ok": False, "tool": "hardware_recover", "error_type": "owner_process_still_running", "summary": "Stop the original Agentic HIL process before recovery.", "state": previous}
            return write_hardware_recovery_report(config, result)
        hardware_lock.clear_quarantine(quarantine_id)
        result = {"ok": True, "tool": "hardware_recover", "recovered": True, "restart_required": True, "state": previous, "summary": "Project hardware state marker cleared after operator acknowledgement. Restart any existing Agentic HIL service process before further hardware access."}
        return write_hardware_recovery_report(config, result)
    except HardwareLockError as error:
        result = {"ok": False, "tool": "hardware_recover", "error_type": "hardware_recovery_failed", "summary": "Project hardware state marker could not be cleared.", "backend_error": str(error)}
        return write_hardware_recovery_report(config, result)
    finally:
        hardware_lock.release_os_lock()


def write_hardware_recovery_report(config: AgenticHILConfig | None, result: JsonObject) -> JsonObject:
    if config is None:
        return write_hardware_recovery_state_audit(result)
    try:
        return write_report(config, result)
    except Exception as error:
        return {"ok": False, "tool": "hardware_recover", "error_type": "audit_write_failed", "operation_result": result, "backend_error": str(error), "summary": "Hardware recovery result could not be written."}


def write_hardware_recovery_state_audit(result: JsonObject) -> JsonObject:
    """Project config is unusable: audit the recovery into the user state directory instead."""
    from agentic_hil.hardware_lock import hardware_state_directory
    from agentic_hil.report import atomic_write_text, timestamp_for_filename

    try:
        audit_path = hardware_state_directory() / f"recovery-{timestamp_for_filename()}.json"
        enriched = {**result, "report_path": str(audit_path), "report_note": "Project config was not loadable; recovery was audited into the user state directory."}
        atomic_write_text(audit_path, json.dumps(enriched, indent=2) + "\n")
        return enriched
    except Exception as error:
        return {"ok": False, "tool": "hardware_recover", "error_type": "audit_write_failed", "operation_result": result, "backend_error": str(error), "summary": "Hardware recovery result could not be written."}


def schema(output: str | None = None, force: bool = False) -> JsonObject | None:
    text = config_schema_text()
    if output is None:
        sys.stdout.write(text)
        return None
    output_path = Path(output)
    if output_path.exists() and not force:
        return {"ok": False, "error_type": "schema_exists", "summary": "Agentic HIL configuration schema already exists. Use --force to overwrite it.", "path": output}
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(text, encoding="utf-8")
    return {"ok": True, "summary": "Agentic HIL configuration schema written.", "path": output}


def test_schema(output: str | None = None, force: bool = False) -> JsonObject | None:
    text = test_config_schema_text()
    if output is None:
        sys.stdout.write(text)
        return None
    output_path = Path(output).expanduser()
    if output_path.exists() and not force:
        return {"ok": False, "error_type": "schema_exists", "summary": "Agentic HIL test configuration schema already exists. Use --force to overwrite it.", "path": output}
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(text, encoding="utf-8")
    return {"ok": True, "summary": "Agentic HIL test configuration schema written.", "path": str(output_path)}


def mcp_config_text() -> str:
    return resources.files("agentic_hil").joinpath("templates", "mcp.json").read_text(encoding="utf-8")


def mcp_config(output: str | None = None, force: bool = False) -> JsonObject | None:
    text = mcp_config_text()
    if output is None:
        sys.stdout.write(text)
        return None
    output_path = Path(output)
    if output_path.exists() and not force:
        return {"ok": False, "error_type": "mcp_config_exists", "summary": "MCP configuration already exists. Use --force to overwrite it.", "path": output}
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(text, encoding="utf-8")
    return {"ok": True, "summary": "Agentic HIL MCP configuration written.", "path": output}


def doctor(config_path: str | None = None) -> JsonObject:
    try:
        config = load_config(config_path)
    except ConfigError as error:
        result = error.to_dict()
        result["tool"] = "agentic_hil_doctor"
        return result
    backend = create_debugger_backend(config)
    try:
        debugger_info = backend.info()
    finally:
        backend.close()
    config_display_path = display_path(config, config.config_path)
    return {
        "ok": debugger_info.get("ok") is True,
        "tool": "agentic_hil_doctor",
        "summary": "Agentic HIL configuration loaded and debugger checked." if debugger_info.get("ok") else "Agentic HIL configuration loaded, but debugger check failed.",
        "config_path": config.config_path,
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
