from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
from contextlib import ExitStack
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

from agentic_hil import __version__
from agentic_hil.comports import list_available_com_ports
from agentic_hil.comstdio import run_com_stdio
from agentic_hil.config import (
    CONFIG_ENV,
    ConfigError,
    absolute_without_symlinks,
    atomic_write_text,
    config_schema_text,
    ensure_safe_state_root,
    is_path_within_frozen,
    load_authoritative_config,
    load_config,
    project_config_path,
    safe_directory,
    secure_atomic_write_text,
    secure_optional_read_text,
    secure_remove_file,
    secure_user_directory,
    secure_user_file_lock,
    trusted_persistent_executable,
    user_state_root,
)
from agentic_hil.coordination import CoordinationError, HardwareCoordinator
from agentic_hil.redact import redact_sensitive
from agentic_hil.report import overall_success, write_report
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


@dataclass(frozen=True)
class FileSnapshot:
    path: Path
    content: str | None


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
    except CoordinationError as error:
        print_json(error.result)
        return 1
    if isinstance(result, int):
        return result
    if result is not None:
        print_json(result)
        return 0 if result_succeeded(result) else 1
    return 0


def result_succeeded(result: JsonObject) -> bool:
    return overall_success(result)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agentic-hil", description="Agentic Hardware-in-the-Loop (Agentic HIL) local MCP stdio server")
    parser.add_argument("--version", action="version", version=__version__)
    subparsers = parser.add_subparsers(dest="command")

    init_parser = subparsers.add_parser("init", help="write a deny-by-default authoritative config for the current workspace")
    init_parser.add_argument("--config", default=None, help=argparse.SUPPRESS)
    init_parser.add_argument("--force", action="store_true")

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

    subparsers.add_parser("lease-status", help="show persistent hardware ownership and quarantine state")
    recover_parser = subparsers.add_parser("recover", help="release quarantined resources after operator-confirmed physical recovery")
    recover_parser.add_argument("--confirm-safe-state", action="store_true", required=True)
    recover_parser.add_argument("--quarantine-id", required=True)
    recover_parser.add_argument("--accept-config-change", action="store_true", help="explicit operator override: accept that the authoritative config changed since the incident was recorded")

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

    setup_parser = subparsers.add_parser("setup", help="one-shot project setup: config + agent skill + .mcp.json + doctor")
    setup_parser.add_argument("--agent", default="claude-code")
    setup_parser.add_argument("--force", action="store_true")

    return parser


def dispatch(args: argparse.Namespace) -> JsonObject | int | None:
    if args.command == "init":
        return init_config(args.config, args.force)
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
    if args.command in {"lease-status", "recover"}:
        config = load_cli_authoritative_config(None)
        coordinator = HardwareCoordinator(config, "operator-cli")
        return coordinator.status() if args.command == "lease-status" else coordinator.recover(safe_state_confirmed=args.confirm_safe_state, quarantine_id=args.quarantine_id, accept_config_change=args.accept_config_change)
    if args.command == "schema":
        return schema(args.output, args.force)
    if args.command == "test-schema":
        return test_schema(args.output, args.force)
    if args.command == "mcp-config":
        return mcp_config(args.output, args.force)
    if args.command == "skill-install":
        return install_skill(args.agent, args.target, args.force)
    if args.command == "setup":
        return setup_project(args.agent, args.force)
    return {"ok": False, "error_type": "unknown_command", "summary": f"unknown command: {args.command}"}


def _agent_mcp_config_path(agent_id: str) -> Path:
    paths = {
        "claude-code": Path.home() / ".claude.json",
        "codex": Path.home() / ".codex" / "config.toml",
        "opencode": Path.home() / ".config" / "opencode" / "opencode.json",
    }
    return _external_user_path(paths[agent_id], "MCP user configuration")


def _external_user_path(path: Path, label: str) -> Path:
    absolute = absolute_without_symlinks(path)
    workspace = absolute_without_symlinks(Path.cwd())
    if is_path_within_frozen(absolute, workspace):
        raise ConfigError("unsafe_configured_path", f"{label} must be stored outside the project workspace.", {"field": "user_config", "path": str(absolute), "workspace_root": str(workspace)})
    return absolute


def _setup_mutation_paths(agent: SkillAgent, config_path: Path) -> list[Path]:
    skill_path = _external_user_path(Path(agent.default_target_path), "Default agent skill")
    paths = [config_path, skill_path, _agent_mcp_config_path(agent.id)]
    if agent.registration == "agents-md":
        paths.append(Path(skill_install_root(str(skill_path))) / "AGENTS.md")
    return paths


def _path_entry_exists(path: Path) -> bool:
    try:
        os.lstat(path)
        return True
    except FileNotFoundError:
        return False


def _capture_file_snapshots(paths: list[Path]) -> list[FileSnapshot]:
    snapshots: list[FileSnapshot] = []
    seen: set[str] = set()
    for path in paths:
        absolute = absolute_without_symlinks(path)
        identity = os.path.normcase(str(absolute))
        if identity in seen:
            continue
        seen.add(identity)
        snapshots.append(FileSnapshot(absolute, secure_optional_read_text(absolute)))
    return snapshots


def _restore_file_snapshots(snapshots: list[FileSnapshot]) -> list[JsonObject]:
    errors: list[JsonObject] = []
    for snapshot in reversed(snapshots):
        try:
            current = secure_optional_read_text(snapshot.path)
            if current == snapshot.content:
                continue
            if snapshot.content is None:
                secure_remove_file(snapshot.path)
            else:
                secure_atomic_write_text(snapshot.path, snapshot.content)
        except BaseException as error:
            errors.append({"path": str(snapshot.path), "exception_type": type(error).__name__, "summary": str(error)})
    return errors


def _skipped_setup_step(summary: str) -> JsonObject:
    return {"ok": False, "skipped": True, "summary": summary}


def setup_project(agent: str, force: bool = False) -> JsonObject:
    """Set up one project transactionally without replacing hardware policy."""
    workspace = Path.cwd().resolve()
    resolved_agent = resolve_skill_agent(agent)
    if resolved_agent is None:
        return {"ok": False, "error_type": "unsupported_agent", "summary": "Agentic HIL does not know this agent's setup paths.", "agent": normalize_agent(agent), "allowed_agents": supported_skill_agents()}

    config_path = initialized_config_path(workspace)
    config_exists = _path_entry_exists(config_path)
    state_actions = ensure_safe_state_root()
    command = mcp_server_command()
    config_result: JsonObject
    skill_result = _skipped_setup_step("Skill installation was not reached.")
    mcp_result = _skipped_setup_step("MCP registration was not reached.")
    doctor_result = _skipped_setup_step("Doctor was not reached.")

    mutation_paths = _setup_mutation_paths(resolved_agent, config_path)
    if config_exists:
        mutation_paths.remove(config_path)
    with ExitStack() as locks:
        for path in sorted(mutation_paths, key=lambda item: os.path.normcase(str(item))):
            locks.enter_context(secure_user_file_lock(path))
        snapshots = _capture_file_snapshots(mutation_paths)
        try:
            if config_exists:
                config_result = {"ok": True, "skipped": True, "summary": "Existing authoritative config kept; setup never replaces operator policy.", "path": str(config_path)}
            else:
                config_result = init_config(None, force=False, _locked=True)

            if overall_success(config_result):
                # Validate policy before mutating global agent files.
                doctor_result = doctor(None)
            if overall_success(config_result) and overall_success(doctor_result):
                skill_result = install_skill(agent, None, force, _locked=True)
            if all(overall_success(result) for result in (config_result, doctor_result, skill_result)):
                mcp_result = register_agent_mcp(agent, force=force, command=command, _locked=True)
        except BaseException as error:
            rollback_errors = _restore_file_snapshots(snapshots)
            if isinstance(error, ConfigError) and rollback_errors:
                error.details["rollback_errors"] = rollback_errors
            raise

        ok = all(overall_success(result) for result in (config_result, skill_result, mcp_result, doctor_result))
        rollback_errors = [] if ok else _restore_file_snapshots(snapshots)
        return {
            "ok": ok and not rollback_errors,
            "tool": "agentic_hil_setup",
            "summary": "Agentic HIL project set up." if ok else "Agentic HIL setup failed; committed file changes were rolled back.",
            "agent": agent,
            "state_root_changes": state_actions,
            "rollback": {"attempted": not ok, "ok": not rollback_errors, "errors": rollback_errors},
            "steps": {
                "config": config_result,
                "skill_install": skill_result,
                "mcp_config": mcp_result,
                "doctor": doctor_result,
            },
        }


def init_config(config_path: str | None = None, force: bool = False, *, _locked: bool = False) -> JsonObject:
    workspace = Path.cwd().resolve()
    target_path = initialized_config_path(workspace)
    validate_legacy_config_selector(config_path, workspace, target_path)
    if _path_entry_exists(target_path) and not force:
        return {"ok": False, "error_type": "config_exists", "summary": "Agentic HIL configuration already exists. Use --force to overwrite it.", "path": str(target_path)}
    if not _locked:
        with secure_user_file_lock(target_path):
            return init_config(config_path, force, _locked=True)
    existing = secure_optional_read_text(target_path)
    text = f"workspace_root: {json.dumps(str(workspace))}\nstate_root: {json.dumps(str(user_state_root()))}\n\n{DEFAULT_CONFIG_TEMPLATE}"
    secure_user_directory(target_path.parent)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".agentic-hil-config-validate-", dir=target_path.parent)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        load_config(str(temporary_path), str(workspace))
    except ConfigError as error:
        result = error.to_dict()
        result["summary"] = "Agentic HIL starter configuration failed validation; the authoritative file was not changed."
        result["path"] = str(target_path)
        return result
    finally:
        secure_remove_file(temporary_path)

    snapshot = FileSnapshot(target_path, existing)
    try:
        secure_atomic_write_text(target_path, text)
        load_authoritative_config(workspace)
    except ConfigError as error:
        rollback_errors = _restore_file_snapshots([snapshot])
        result = error.to_dict()
        result["summary"] = "Agentic HIL starter configuration failed final validation and was rolled back."
        result["path"] = str(target_path)
        if rollback_errors:
            result["rollback_errors"] = rollback_errors
        return result
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
    configured = os.environ.get(CONFIG_ENV)
    if configured:
        requested = Path(configured).expanduser()
        if not requested.is_absolute():
            raise ConfigError("config_invalid", f"{CONFIG_ENV} must contain an absolute path.", {"path": configured, "environment_variable": CONFIG_ENV})
        target = absolute_without_symlinks(requested)
    else:
        target = project_config_path(workspace)
    if is_path_within_frozen(target, workspace):
        raise ConfigError("config_invalid", "The authoritative config must be stored outside the workspace.", {"path": str(target), "workspace_root": str(workspace)})
    return target


def run_test_reactor(test_config_path: str | None = None) -> JsonObject:
    config = load_authoritative_config(Path.cwd())
    test_config = load_test_config(test_config_path, config.work_dir)
    service = AgenticHILToolService(config, frontend="reactor")
    # Devices on named debuggers (multi-board) get their own service driving their
    # debugger, sharing the base coordinator so the whole project is one owner.
    def device_service_factory(device_config: AgenticHILConfig) -> AgenticHILToolService:
        return AgenticHILToolService(device_config, coordinator=service.coordinator, frontend="reactor")

    # Construction happens inside the guarded block: the factory builds real
    # per-device services in TestReactor.__init__, so a failure there must still
    # produce a JSON error result and fall through to service.close() below.
    reactor: TestReactor | None = None
    primary_error: BaseException | None = None
    try:
        reactor = TestReactor(service.config, service, service_factory=device_service_factory)
        result = reactor.run(test_config)
    except BaseException as error:
        primary_error = error
        result = {
            "ok": False,
            "tool": "test_reactor",
            "name": test_config.name,
            "test_config_path": test_config.path,
            "error_type": "interrupted" if isinstance(error, (KeyboardInterrupt, SystemExit)) else "reactor_exception",
            "exception_type": type(error).__name__,
            "summary": "Test reactor was interrupted; all containment steps were attempted.",
            "steps": [],
            "cleanup": getattr(error, "agentic_hil_cleanup", []),
            "cleanup_ok": False,
        }
    try:
        if reactor is not None:
            reactor.close()
    except BaseException as error:
        cleanup_error = {
            "device": "reactor",
            "action": "close",
            "result": {
                "ok": False,
                "tool": "test_reactor",
                "error_type": "cleanup_exception",
                "summary": "Per-device service cleanup raised an exception.",
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
        if primary_error is None and isinstance(error, (KeyboardInterrupt, SystemExit)):
            primary_error = error
    try:
        service.close()
    except BaseException as error:
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
        if primary_error is None and isinstance(error, (KeyboardInterrupt, SystemExit)):
            primary_error = error
    written = write_report(config, result)
    if primary_error is not None:
        if written.get("audit_ok") is False:
            primary_error.args = (*primary_error.args, "Final reactor audit failed.")
        raise primary_error
    return written


def init_next_steps(available_com_ports: JsonObject, config_path: Path) -> list[str]:
    next_steps = [
        f"Review the deny-by-default config at {config_path}. Set {CONFIG_ENV} only when an explicit absolute-path override is needed.",
        "Edit target.name and target.controller for your board.",
        "Set debugger.interface_cfg and debugger.target_cfg for your OpenOCD setup.",
        "Configure devices with the debugger and optional UART used by test-reactor sequences.",
        "If multiple debug probes are connected, set debugger.probe_id to the intended probe serial number. For multi-board test plans, add named entries under debuggers and bind each device via devices.<id>.debugger.",
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


def test_schema(output: str | None = None, force: bool = False) -> JsonObject:
    text = resources.files("agentic_hil").joinpath("schemas", "testconfig.schema.json").read_text(encoding="utf-8")
    if output is None:
        sys.stdout.write(text)
        return {"ok": True}
    output_path = Path(output)
    if output_path.exists() and not force:
        return {"ok": False, "error_type": "schema_exists", "summary": "Agentic HIL test configuration schema already exists. Use --force to overwrite it.", "path": output}
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(text, encoding="utf-8")
    return {"ok": True, "summary": "Agentic HIL test configuration schema written.", "path": output}


test_schema.__test__ = False  # type: ignore[attr-defined] - keep pytest from collecting the CLI helper


def _mcp_cache_roots() -> list[Path]:
    cache_roots: list[Path] = [Path(tempfile.gettempdir()), Path.home() / ".cache", Path.home() / "Library" / "Caches"]
    for variable in ("UV_CACHE_DIR", "XDG_CACHE_HOME"):
        if value := os.environ.get(variable):
            cache_roots.append(Path(value).expanduser())
    if local_app_data := os.environ.get("LOCALAPPDATA"):
        cache_roots.append(Path(local_app_data) / "uv" / "cache")
    return cache_roots


def _trusted_mcp_command(command: str) -> str:
    return trusted_persistent_executable(command, workspace=Path.cwd(), disallowed_roots=_mcp_cache_roots())


def mcp_server_command() -> str:
    """Return one pinned, persistent console-script path or fail closed."""

    candidates: list[str] = []
    if found := shutil.which("agentic-hil"):
        candidates.append(found)
    script = Path(sys.executable).parent / ("agentic-hil.exe" if os.name == "nt" else "agentic-hil")
    candidates.append(str(script))
    rejected: list[JsonObject] = []
    for candidate in dict.fromkeys(candidates):
        try:
            return _trusted_mcp_command(candidate)
        except (ConfigError, OSError) as error:
            rejected.append({"path": str(candidate), "reason": str(error)})
    raise ConfigError(
        "mcp_command_untrusted",
        "No stable trusted Agentic HIL executable was found. Install it persistently with 'uv tool install agentic-hil' or 'pipx install agentic-hil'.",
        {"rejected_candidates": rejected},
    )


def mcp_config_text() -> str:
    return json.dumps({"mcpServers": {"agentic-hil": {"command": mcp_server_command(), "args": ["mcp-stdio"]}}}, indent=2) + "\n"


def mcp_config(output: str | None = None, force: bool = False) -> JsonObject:
    text = mcp_config_text()
    if output is None:
        sys.stdout.write(text)
        return {"ok": True}
    workspace = absolute_without_symlinks(Path.cwd())
    requested = Path(output).expanduser()
    output_path = absolute_without_symlinks(requested if requested.is_absolute() else workspace / requested)
    if not is_path_within_frozen(output_path, workspace):
        raise ConfigError("unsafe_configured_path", "MCP project configuration output must stay inside the current workspace.", {"path": str(output_path), "workspace_root": str(workspace)})
    if _path_entry_exists(output_path) and not force:
        return {"ok": False, "error_type": "mcp_config_exists", "summary": "MCP configuration already exists. Use --force to overwrite it.", "path": output}
    safe_directory(output_path.parent)
    atomic_write_text(output_path, text, workspace=workspace)
    return {"ok": True, "summary": "Agentic HIL MCP configuration written.", "path": str(output_path)}


AGENTIC_HIL_MCP_START = "# >>> agentic-hil mcp (managed) >>>"
AGENTIC_HIL_MCP_END = "# <<< agentic-hil mcp (managed) <<<"


def register_agent_mcp(agent: str | None = None, force: bool = False, *, command: str | None = None, _locked: bool = False) -> JsonObject:
    """Register the agentic-hil MCP server for `agent` in that agent's USER-level
    config, outside the firmware repo. The repo is untrusted under the policy
    boundary, so the MCP registration must not live in it. The pinned, trusted
    absolute launcher path is stored deliberately; the client still launches the
    server in the project directory, where it discovers its authoritative config."""
    requested = agent or "claude-code"
    resolved = resolve_skill_agent(requested)
    agent_id = resolved.id if resolved else normalize_agent(requested)
    if agent_id not in {"claude-code", "codex", "opencode"}:
        return {"ok": False, "error_type": "unsupported_agent", "summary": "Agentic HIL does not know this agent's MCP config format.", "agent": agent_id, "allowed_agents": supported_skill_agents()}
    command = mcp_server_command() if command is None else _trusted_mcp_command(command)
    if not _locked:
        with secure_user_file_lock(_agent_mcp_config_path(agent_id)):
            return register_agent_mcp(agent, force, command=command, _locked=True)
    if agent_id == "claude-code":
        return _register_claude_mcp(command, force)
    if agent_id == "codex":
        return _register_codex_mcp(command, force)
    if agent_id == "opencode":
        return _register_opencode_mcp(command, force)
    raise AssertionError(f"unhandled supported agent: {agent_id}")


def _parse_toml(text: str) -> tuple[dict | None, str | None]:
    if not text.strip():
        return {}, None
    try:
        import tomllib  # type: ignore[import-not-found]
    except ImportError:
        try:
            import tomli as tomllib  # type: ignore[import-not-found,no-redef]
        except ImportError:
            return None, "Python 3.10 needs the optional 'tomli' package to merge an existing Codex config safely."
    try:
        loaded = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        return None, str(error)
    return loaded if isinstance(loaded, dict) else None, None


def _register_codex_mcp(command: str, force: bool) -> JsonObject:
    path = _agent_mcp_config_path("codex")
    block = "\n".join(
        [
            AGENTIC_HIL_MCP_START,
            "[mcp_servers.agentic-hil]",
            f"command = {json.dumps(command)}",
            'args = ["mcp-stdio"]',
            "enabled = true",
            AGENTIC_HIL_MCP_END,
        ]
    )
    existing = secure_optional_read_text(path) or ""
    parsed, parse_error = _parse_toml(existing)
    if parsed is None:
        return {"ok": False, "error_type": "config_invalid", "agent": "codex", "path": str(path), "summary": "Existing Codex config.toml is not valid TOML or cannot be parsed safely; left untouched.", "parse_error": parse_error}
    start_count = existing.count(AGENTIC_HIL_MCP_START)
    end_count = existing.count(AGENTIC_HIL_MCP_END)
    if start_count != end_count or start_count > 1:
        return {"ok": False, "error_type": "config_invalid", "agent": "codex", "path": str(path), "summary": "Codex config.toml contains malformed or duplicate Agentic HIL managed markers; left untouched."}
    has_managed = start_count == 1
    servers = parsed.get("mcp_servers", {})
    entry = servers.get("agentic-hil") if isinstance(servers, dict) else None
    desired_entry = {"command": command, "args": ["mcp-stdio"], "enabled": True}
    if not has_managed and entry is not None:
        return {"ok": False, "error_type": "mcp_config_conflict", "agent": "codex", "format": "codex-toml", "path": str(path), "summary": "An unmanaged Codex agentic-hil MCP entry already exists; left untouched."}
    if has_managed and not isinstance(entry, dict):
        return {"ok": False, "error_type": "config_invalid", "agent": "codex", "format": "codex-toml", "path": str(path), "summary": "The Agentic HIL managed markers do not contain an agentic-hil MCP table; left untouched."}
    if has_managed and entry == desired_entry:
        return {"ok": True, "skipped": True, "agent": "codex", "format": "codex-toml", "path": str(path), "summary": "Codex MCP entry already registered with the trusted launcher."}
    if has_managed:
        pattern = re.compile(re.escape(AGENTIC_HIL_MCP_START) + r"[\s\S]*?" + re.escape(AGENTIC_HIL_MCP_END))
        next_text = pattern.sub(lambda _match: block, existing)
    else:
        trimmed = existing.rstrip()
        separator = "\n\n" if trimmed else ""
        next_text = f"{trimmed}{separator}{block}\n"
    next_parsed, next_parse_error = _parse_toml(next_text)
    if next_parsed is None:
        return {"ok": False, "error_type": "config_invalid", "agent": "codex", "path": str(path), "summary": "Generated Codex config.toml failed TOML validation; existing config was left untouched.", "parse_error": next_parse_error}
    secure_atomic_write_text(path, next_text)
    return {"ok": True, "agent": "codex", "format": "codex-toml", "path": str(path), "migrated": has_managed, "summary": "Registered agentic-hil MCP server in the Codex user config.toml."}


def _claude_mcp_entry_kind(entry: object, desired: JsonObject) -> str | None:
    if entry == desired:
        return "current"
    legacy_entries = (
        {"type": "stdio", "command": "agentic-hil", "args": ["mcp-stdio"]},
        {"command": "uvx", "args": ["--from", "agentic-hil", "agentic-hil", "mcp-stdio"]},
        {"type": "stdio", "command": "uvx", "args": ["--from", "agentic-hil", "agentic-hil", "mcp-stdio"]},
    )
    if entry in legacy_entries:
        return "legacy"
    if isinstance(entry, dict) and set(entry) == {"type", "command", "args"} and entry.get("type") == "stdio" and entry.get("args") == ["mcp-stdio"]:
        configured = entry.get("command")
        if isinstance(configured, str) and Path(configured).expanduser().is_absolute():
            return "managed"
    return None


def _opencode_mcp_entry_kind(entry: object, desired: JsonObject) -> str | None:
    if entry == desired:
        return "current"
    legacy_commands = (
        ["agentic-hil", "mcp-stdio"],
        ["uvx", "--from", "agentic-hil", "agentic-hil", "mcp-stdio"],
    )
    if entry in ({"type": "local", "command": value, "enabled": True} for value in legacy_commands):
        return "legacy"
    if isinstance(entry, dict) and set(entry) == {"type", "command", "enabled"} and entry.get("type") == "local" and entry.get("enabled") is True:
        configured = entry.get("command")
        if isinstance(configured, list) and len(configured) == 2 and configured[1] == "mcp-stdio" and isinstance(configured[0], str) and Path(configured[0]).expanduser().is_absolute():
            return "managed"
    return None


def _register_opencode_mcp(command: str, force: bool) -> JsonObject:
    path = _agent_mcp_config_path("opencode")
    data = _load_json_object(path)
    if data is None:
        return {"ok": False, "error_type": "config_invalid", "agent": "opencode", "path": str(path), "summary": "Existing opencode.json is not valid JSON; left untouched."}
    servers = data.setdefault("mcp", {})
    if not isinstance(servers, dict):
        return {"ok": False, "error_type": "config_invalid", "agent": "opencode", "path": str(path), "summary": "Existing opencode.json 'mcp' is not an object; left untouched."}
    desired_entry: JsonObject = {"type": "local", "command": [command, "mcp-stdio"], "enabled": True}
    existing_entry = servers.get("agentic-hil")
    kind = _opencode_mcp_entry_kind(existing_entry, desired_entry) if "agentic-hil" in servers else None
    if "agentic-hil" in servers and kind is None:
        return {"ok": False, "error_type": "mcp_config_conflict", "agent": "opencode", "format": "opencode-json", "path": str(path), "summary": "An unmanaged opencode agentic-hil MCP entry already exists; left untouched."}
    if kind == "current":
        return {"ok": True, "skipped": True, "agent": "opencode", "format": "opencode-json", "path": str(path), "summary": "opencode MCP entry already registered."}
    data.setdefault("$schema", "https://opencode.ai/config.json")
    servers["agentic-hil"] = desired_entry
    secure_atomic_write_text(path, json.dumps(data, indent=2) + "\n")
    return {"ok": True, "agent": "opencode", "format": "opencode-json", "path": str(path), "migrated": kind in {"legacy", "managed"}, "summary": "Registered agentic-hil MCP server in the opencode user config."}


def _register_claude_mcp(command: str, force: bool) -> JsonObject:
    path = _agent_mcp_config_path("claude-code")
    data = _load_json_object(path)
    if data is None:
        return {"ok": False, "error_type": "config_invalid", "agent": "claude-code", "path": str(path), "summary": "Existing ~/.claude.json is not valid JSON; left untouched."}
    servers = data.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        return {"ok": False, "error_type": "config_invalid", "agent": "claude-code", "path": str(path), "summary": "Existing ~/.claude.json 'mcpServers' is not an object; left untouched."}
    desired_entry: JsonObject = {"type": "stdio", "command": command, "args": ["mcp-stdio"]}
    existing_entry = servers.get("agentic-hil")
    kind = _claude_mcp_entry_kind(existing_entry, desired_entry) if "agentic-hil" in servers else None
    if "agentic-hil" in servers and kind is None:
        return {"ok": False, "error_type": "mcp_config_conflict", "agent": "claude-code", "format": "claude-user", "method": "file", "path": str(path), "summary": "An unmanaged Claude agentic-hil MCP entry already exists; left untouched."}
    if kind == "current":
        return {"ok": True, "skipped": True, "agent": "claude-code", "format": "claude-user", "method": "file", "path": str(path), "summary": "Claude MCP entry already registered."}
    servers["agentic-hil"] = desired_entry
    secure_atomic_write_text(path, json.dumps(data, indent=2) + "\n")
    return {"ok": True, "agent": "claude-code", "format": "claude-user", "method": "file", "path": str(path), "migrated": kind in {"legacy", "managed"}, "summary": "Registered agentic-hil MCP server in ~/.claude.json (user scope)."}


def _load_json_object(path: Path) -> dict | None:
    """{} when missing, the parsed mapping when valid, None when the file exists
    but is not a JSON object (so callers never clobber unparseable config)."""
    text = secure_optional_read_text(path)
    if text is None:
        return {}

    def unique_object(pairs: list[tuple[str, object]]) -> dict:
        loaded: dict[str, object] = {}
        for key, value in pairs:
            if key in loaded:
                raise ValueError(f"duplicate JSON object key: {key}")
            loaded[key] = value
        return loaded

    try:
        loaded = json.loads(text or "{}", object_pairs_hook=unique_object)
    except (json.JSONDecodeError, ValueError):
        return None
    return loaded if isinstance(loaded, dict) else None


def doctor(config_path: str | None = None) -> JsonObject:
    try:
        config = load_cli_authoritative_config(config_path)
    except ConfigError as error:
        result = error.to_dict()
        result["tool"] = "agentic_hil_doctor"
        return result
    if config.permissions.allow_probe:
        service = AgenticHILToolService(config, frontend="doctor")
        primary_error: BaseException | None = None
        try:
            debugger_info = service.call("debugger_info")
        except BaseException as error:
            primary_error = error
            debugger_info = {"ok": False}
        try:
            service.close()
        except BaseException as cleanup_error:
            if primary_error is not None:
                primary_error.args = (*primary_error.args, f"Cleanup error: {cleanup_error}")
            else:
                raise
        if primary_error is not None:
            raise primary_error
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
            "config_invalid",
            f"--config cannot select repository-controlled policy. Set {CONFIG_ENV} to an absolute external config path or remove --config.",
            {"selected_path": str(selected), "authoritative_path": str(expected_path)},
        )


def install_skill(agent: str | None = None, target: str | None = None, force: bool = False, *, _locked: bool = False) -> JsonObject:
    requested_agent = agent or "opencode"
    resolved_agent = resolve_skill_agent(requested_agent)
    if resolved_agent is None and target is None:
        return {"ok": False, "error_type": "unsupported_agent", "summary": "Agentic HIL does not know this agent's default skill directory. Provide --target to install anyway.", "agent": normalize_agent(requested_agent), "allowed_agents": supported_skill_agents()}
    agent_id = resolved_agent.id if resolved_agent else normalize_agent(requested_agent)
    agent_name = resolved_agent.display_name if resolved_agent else agent_id
    source_path = bundled_skill_path()
    target_path = absolute_without_symlinks(Path(target or resolved_agent.default_target_path))  # type: ignore[union-attr]
    if target is None:
        target_path = _external_user_path(target_path, "Default agent skill")
    if not _locked:
        mutation_paths = [target_path]
        if resolved_agent is not None and resolved_agent.registration == "agents-md":
            mutation_paths.append(Path(skill_install_root(str(target_path))) / "AGENTS.md")
        with ExitStack() as locks:
            for path in sorted(mutation_paths, key=lambda item: os.path.normcase(str(item))):
                locks.enter_context(secure_user_file_lock(path))
            snapshots = _capture_file_snapshots(mutation_paths)
            try:
                result = install_skill(agent, target, force, _locked=True)
            except BaseException as error:
                rollback_errors = _restore_file_snapshots(snapshots)
                if isinstance(error, ConfigError) and rollback_errors:
                    error.details["rollback_errors"] = rollback_errors
                raise
            if overall_success(result):
                return result
            rollback_errors = _restore_file_snapshots(snapshots)
            result["rollback"] = {"attempted": True, "ok": not rollback_errors, "errors": rollback_errors}
            return result
    source_text = source_path.read_text(encoding="utf-8")
    source_version = skill_version(source_text) or __version__
    existing_text = secure_optional_read_text(target_path)
    if existing_text is not None:
        if existing_text == source_text:
            registration = register_skill(resolved_agent, str(target_path), source_version, requested_agent)
            return {"ok": True, "summary": f"Agentic HIL {agent_name} skill is already installed.", "agent": agent_id, "requested_agent": requested_agent, "skill": SKILL_NAME, "source_path": str(source_path), "target_path": str(target_path), "version": source_version, "installed": False, "updated": False, "registered": registration.get("ok") is True if registration else False, "registration": registration}
        existing_version = skill_version(existing_text)
        managed_skill = is_agentic_hil_setup_skill(existing_text)
        if not managed_skill:
            return {"ok": False, "error_type": "skill_conflict", "summary": "Target skill file contains unmanaged content and was left untouched; --force never replaces foreign skills.", "agent": agent_id, "requested_agent": requested_agent, "skill": SKILL_NAME, "source_path": str(source_path), "target_path": str(target_path), "existing_version": existing_version, "version": source_version}
        if existing_version != source_version:
            secure_atomic_write_text(target_path, source_text)
            registration = register_skill(resolved_agent, str(target_path), source_version, requested_agent)
            return {"ok": True, "summary": f"Agentic HIL {agent_name} skill updated to match the current CLI package.", "agent": agent_id, "requested_agent": requested_agent, "skill": SKILL_NAME, "source_path": str(source_path), "target_path": str(target_path), "previous_version": existing_version, "version": source_version, "installed": False, "updated": True, "registered": registration.get("ok") is True if registration else False, "registration": registration}
        if not force:
            return {"ok": False, "error_type": "skill_exists", "summary": "Managed Agentic HIL skill differs from the packaged copy. Use --force to repair it.", "agent": agent_id, "requested_agent": requested_agent, "skill": SKILL_NAME, "source_path": str(source_path), "target_path": str(target_path), "existing_version": existing_version, "version": source_version}
    secure_atomic_write_text(target_path, source_text)
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
    existing = secure_optional_read_text(file_path) or ""
    pattern = re.compile(rf"{re.escape(AGENTIC_HIL_REGISTRATION_START)}[\s\S]*?{re.escape(AGENTIC_HIL_REGISTRATION_END)}")
    trimmed = existing.rstrip()
    separator = "\n\n" if trimmed else ""
    next_text = pattern.sub(block, existing) if pattern.search(existing) else f"{trimmed}{separator}{block}\n"
    if next_text != existing:
        secure_atomic_write_text(file_path, next_text)
        return {"updated": True}
    return {"updated": False}


def print_json(value: JsonObject) -> None:
    sys.stdout.write(json.dumps(redact_sensitive(value), indent=2) + "\n")
