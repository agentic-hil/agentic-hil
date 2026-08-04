from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import sysconfig
import tempfile
from contextlib import ExitStack, suppress
from dataclasses import asdict, dataclass
from importlib import resources
from pathlib import Path, PurePath

import yaml

from agentic_hil import __version__
from agentic_hil.adopt import project_config_adopt_hardware
from agentic_hil.bootstrap import apply_discovery_to_template, discover_attached_hardware, load_project_profile
from agentic_hil.comports import list_available_com_ports
from agentic_hil.comstdio import run_com_stdio
from agentic_hil.config import (
    CONFIG_ENV,
    DEFAULT_CONFIG_TEMPLATE,
    ConfigError,
    absolute_without_symlinks,
    atomic_write_text,
    authoritative_config_target,
    bind_debugger,
    config_schema_text,
    debugger_access_enabled,
    ensure_safe_state_root,
    inspect_windows_path_trust,
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
    tighten_owned_writable_ancestors,
    trusted_persistent_executable,
    user_file_lock_path,
    user_state_root,
    windows_path_trust,
)
from agentic_hil.configstate import config_status, with_config_status
from agentic_hil.configwrite import ACTOR_HUMAN
from agentic_hil.coordination import CoordinationError, HardwareCoordinator
from agentic_hil.knowledge import RUNNING_SERVER_COMPARISON, remediation_fields
from agentic_hil.process import ProcessImage, snapshot_process_images
from agentic_hil.redact import redact_sensitive
from agentic_hil.report import overall_success, write_report
from agentic_hil.stdio import run_stdio_server
from agentic_hil.test_reactor import DEFAULT_TEST_CONFIG_PATH, TestReactor, load_test_config, plan_devices
from agentic_hil.tools import AgenticHILToolService, UnprovisionedToolService, unbound_debugger_error
from agentic_hil.types import AgenticHILConfig, JsonObject

SKILL_NAME = "agentic-hil"
# How far up the process tree the upgrade guard follows launchers before it
# stops looking, and how many holding processes a refusal names before the
# count carries the rest.
_ANCESTOR_WALK_LIMIT = 16
_REPORTED_HOLDER_LIMIT = 10
# Earlier releases installed the same skill under these names. Leaving one in
# place would offer the agent two skills for the same job.
LEGACY_SKILL_NAMES = ("agentic-hil-config-setup",)
SKILL_FILE = "SKILL.md"
AGENTIC_HIL_REGISTRATION_START = "<!-- Agentic HIL skill registration start -->"
AGENTIC_HIL_REGISTRATION_END = "<!-- Agentic HIL skill registration end -->"
# Measured: a refusal that only said an unmanaged entry exists was read as an
# obstacle to clear. Ten runs rewrote the operator's entry by hand and reran
# setup, and reported success. Say whose the entry is and that the refusal is
# the answer — and name no action that could be mistaken for a way through.
CONFLICT_NEXT_STEP = (
    "That entry belongs to the operator, and --force does not apply to a foreign entry. Editing the "
    "file yourself or removing the entry is not the resolution either: it hands the hardware gate to "
    "a program the operator did not choose. This conflict is the answer to the request. Report it, "
    "name the file, and stop."
)


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

    init_parser = subparsers.add_parser("init", help="project half: write this workspace's deny-by-default authoritative config and verify it with doctor")
    init_parser.add_argument("--config", default=None, help=argparse.SUPPRESS)
    init_parser.add_argument("--agent", default=None, help="also ask this agent to refuse its own write tools on the config and the state root")
    init_parser.add_argument("--force", action="store_true")

    adopt_parser = subparsers.add_parser(
        "adopt-hardware",
        help="carry an attached board's identity into an existing config: probe id, backend executable, controller, COM device. Fills only what is unset; never touches permissions",
    )
    adopt_parser.add_argument("--debugger", default=None, help="which configured debugger entry receives the values; only needed with more than one")
    adopt_parser.add_argument("--com-port", default=None, help="which com_ports entry receives the discovered device; created with every permission false if it does not exist")
    adopt_parser.add_argument("--probe-id", default=None, help="which attached probe this is about; only needed when more than one is attached")
    adopt_parser.add_argument("--dry-run", action="store_true", help="report what would be filled in and write nothing")

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
    reactor_parser.add_argument(
        "--wait-s",
        type=float,
        default=0.0,
        help="wait up to this many seconds for a device another run holds, instead of refusing immediately; bounded and never implicit",
    )

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

    agent_install_parser = subparsers.add_parser("agent-install", help="user half, once per user and agent: install the agent skill and register the MCP server at user level; needs no workspace and no config")
    agent_install_parser.add_argument("--agent", default="claude-code")
    agent_install_parser.add_argument("--force", action="store_true")

    setup_parser = subparsers.add_parser("setup", help="first run in one command: agent-install (user half) then init (project half)")
    setup_parser.add_argument("--agent", default="claude-code")
    setup_parser.add_argument("--force", action="store_true")

    upgrade_parser = subparsers.add_parser("upgrade", help="upgrade this Agentic HIL installation and refresh agent skills")
    upgrade_parser.add_argument("--agent", action="append", default=[], help="refresh this agent's skill after upgrading; repeat for multiple agents")

    return parser


def dispatch(args: argparse.Namespace) -> JsonObject | int | None:
    if args.command == "init":
        return init_project(args.config, args.agent, args.force)
    if args.command == "adopt-hardware":
        return adopt_hardware(debugger_id=args.debugger, com_port_id=args.com_port, probe_id=args.probe_id, dry_run=args.dry_run)
    if args.command == "doctor":
        return doctor(args.config)
    if args.command == "debugger-probes":
        return debugger_probes()
    if args.command == "com-ports":
        return list_available_com_ports()
    if args.command == "mcp-stdio":
        return run_mcp_stdio(args.config)
    if args.command == "com-stdio":
        config = load_cli_authoritative_config(args.config)
        return run_com_stdio(config, args.port, max_read_bytes=args.max_read_bytes, read_wait_timeout_s=args.read_wait_timeout_s, eof_idle_timeout_s=args.eof_idle_timeout_s)
    if args.command == "test-reactor":
        return run_test_reactor(args.test_config, wait_s=args.wait_s)
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
    if args.command == "agent-install":
        return install_agent(args.agent, args.force)
    if args.command == "setup":
        return setup_project(args.agent, args.force)
    if args.command == "upgrade":
        return upgrade_installation(args.agent)
    return {"ok": False, "error_type": "unknown_command", "summary": f"unknown command: {args.command}"}


def _distribution_installer() -> str | None:
    try:
        from importlib.metadata import distribution

        installer = distribution("agentic-hil").read_text("INSTALLER")
    except Exception:
        return None
    return installer.strip().lower() if installer else None


def _normalized_location(path: str | Path) -> str:
    return os.path.normcase(str(Path(path).resolve())).replace("\\", "/").casefold()


def _dedicated_environment_root() -> Path | None:
    """The environment this distribution owns alone, or None when it shares one.

    A uv tool environment and a pipx venv hold Agentic HIL and its dependencies
    and nothing else, so every executable under them belongs to this
    installation. A `pip install --user` prefix or a system Python holds every
    other Python program on the machine as well, and reading its executables as
    ours would refuse an upgrade because some unrelated script is running.
    """
    prefix = Path(sys.prefix)
    location = _normalized_location(prefix)
    if any(marker in location for marker in ("/uv/tools/agentic-hil", "/pipx/venvs/agentic-hil")):
        return prefix
    return None


def _installed_extras() -> tuple[str, ...]:
    """Declared extras whose every requirement is installed alongside this package.

    `uv tool upgrade` and `pipx upgrade` reinstall from the requirement their
    own receipt records, and that requirement already names the extras. The pip
    and `uv pip` paths take the requirement from the command line instead, so
    one that names the bare distribution re-resolves without the extras and
    leaves whatever they installed pinned at whatever version it already had.
    """
    from importlib.metadata import PackageNotFoundError, distribution, version

    try:
        metadata = distribution("agentic-hil").metadata
    except Exception:
        return ()
    declared = [name.strip() for name in metadata.get_all("Provides-Extra") or [] if isinstance(name, str) and name.strip()]
    requirements = [item for item in metadata.get_all("Requires-Dist") or [] if isinstance(item, str)]
    installed: list[str] = []
    for extra in declared:
        marker = re.compile(rf"""extra\s*==\s*['"]{re.escape(extra)}['"]""")
        required = [_requirement_name(item) for item in requirements if ";" in item and marker.search(item.split(";", 1)[1])]
        names = [name for name in required if name]
        if not names:
            continue
        for name in names:
            try:
                version(name)
            except PackageNotFoundError:
                break
        else:
            installed.append(extra)
    return tuple(sorted(installed))


def _requirement_name(requirement: str) -> str | None:
    match = re.match(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)", requirement)
    return match.group(1) if match else None


def _upgrade_requirement() -> str:
    extras = _installed_extras()
    return f"agentic-hil[{','.join(extras)}]" if extras else "agentic-hil"


def _upgrade_command() -> tuple[str, list[str]]:
    """Select the manager that owns the running installation, never another PATH copy."""
    prefix = _normalized_location(sys.prefix)
    installer = _distribution_installer()
    if "/uv/tools/agentic-hil" in prefix:
        uv = shutil.which("uv")
        if uv is None:
            raise ConfigError("upgrade_manager_not_found", "This installation is managed by uv, but uv is not on PATH.", {"manager": "uv", "python": sys.executable})
        return "uv", [uv, "tool", "upgrade", "agentic-hil"]
    if "/pipx/venvs/agentic-hil" in prefix:
        pipx = shutil.which("pipx")
        if pipx is None:
            raise ConfigError("upgrade_manager_not_found", "This installation is managed by pipx, but pipx is not on PATH.", {"manager": "pipx", "python": sys.executable})
        return "pipx", [pipx, "upgrade", "agentic-hil"]
    if installer == "uv":
        uv = shutil.which("uv")
        if uv is None:
            raise ConfigError("upgrade_manager_not_found", "This installation is managed by uv, but uv is not on PATH.", {"manager": "uv", "python": sys.executable})
        return "uv", [uv, "pip", "install", "--python", sys.executable, "--upgrade", _upgrade_requirement()]
    return "pip", [sys.executable, "-m", "pip", "install", "--upgrade", _upgrade_requirement()]


def _installation_console_scripts() -> tuple[str, ...]:
    """Where this distribution's own console script can sit for this interpreter.

    A virtual environment puts it beside the interpreter; a system or `--user`
    installation puts it in that scheme's script directory instead. Only these
    exact files are attributable to this distribution, which is what a shared
    prefix needs: the interpreter there runs every other Python program on the
    machine too.
    """
    name = "agentic-hil.exe" if os.name == "nt" else "agentic-hil"
    directories = [Path(sys.executable).parent, Path(sys.prefix) / ("Scripts" if os.name == "nt" else "bin")]
    for scheme in (None, f"{os.name}_user"):
        with suppress(KeyError, OSError):
            directories.append(Path(sysconfig.get_path("scripts") if scheme is None else sysconfig.get_path("scripts", scheme)))
    return tuple(dict.fromkeys(_normalized_location(directory / name) for directory in directories))


def _belongs_to_installation(image: str, owned_prefix: str | None, scripts: tuple[str, ...]) -> bool:
    location = _normalized_location(image)
    if owned_prefix is not None and location.startswith(owned_prefix):
        return True
    return location in scripts


def _upgrading_process_and_its_launchers(by_pid: dict[int, ProcessImage], owned_prefix: str | None, scripts: tuple[str, ...]) -> set[int]:
    """This process, plus the parents inside the installation that started it.

    `agentic-hil upgrade` runs out of the very installation it replaces, so it
    would otherwise report itself and refuse every upgrade there is. Measured on
    Windows: the interpreter executing this code has its image at the base
    Python outside the environment, and the process that actually holds
    `Scripts\\python.exe` open is its parent -- a launcher the environment
    installs, with the console script another level above it. Excluding only
    `os.getpid()` would therefore still find a holder every single time.

    Walking up is what makes this safe rather than merely permissive. A second
    Agentic HIL -- the MCP server the agent host started, which is the process
    this check exists to find -- is a sibling of this one, never one of its
    parents, so no ancestor walk can reach it.
    """
    excluded = {os.getpid()}
    current = by_pid.get(os.getpid())
    for _ in range(_ANCESTOR_WALK_LIMIT):
        if current is None:
            break
        parent = by_pid.get(current.parent_pid)
        if parent is None or parent.pid in excluded:
            break
        if not _belongs_to_installation(parent.image, owned_prefix, scripts):
            break
        if parent.created_ns > current.created_ns:
            # A pid is reused once its process exits. Nothing can be younger
            # than the child it started, so this entry is an unrelated process
            # that inherited the number, not the launcher we came through.
            break
        excluded.add(parent.pid)
        current = parent
    return excluded


def _processes_holding_installation() -> list[JsonObject]:
    """Processes running out of the installation an upgrade is about to replace.

    Empty on every platform but Windows, and deliberately so. Elsewhere a
    package manager unlinks the old files while the processes using them keep
    reading their own copies, so the upgrade completes and nothing is lost.
    Windows refuses to delete a file that is mapped as a running image, and a
    manager that removes the environment before it rebuilds it then leaves
    neither the old installation nor the new one.
    """
    snapshot = snapshot_process_images()
    if snapshot is None:
        return []
    owned_root = _dedicated_environment_root()
    owned_prefix = _normalized_location(owned_root).rstrip("/") + "/" if owned_root is not None else None
    scripts = _installation_console_scripts()
    excluded = _upgrading_process_and_its_launchers({entry.pid: entry for entry in snapshot}, owned_prefix, scripts)
    holders = [
        {"pid": entry.pid, "image": entry.image}
        for entry in snapshot
        if entry.pid not in excluded and _belongs_to_installation(entry.image, owned_prefix, scripts)
    ]
    return sorted(holders, key=lambda holder: holder["pid"])


def _run_upgrade_process(command: list[str], *, cwd: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=cwd, capture_output=True, text=True, timeout=600, check=False)


def _process_result(completed: subprocess.CompletedProcess[str]) -> JsonObject:
    result: JsonObject = {"returncode": completed.returncode}
    if completed.stdout.strip():
        result["stdout"] = completed.stdout.strip()
    if completed.stderr.strip():
        result["stderr"] = completed.stderr.strip()
    return result


def upgrade_installation(agents: list[str] | None = None) -> JsonObject:
    """Upgrade the package owning this process, then run maintenance from new code."""
    requested_agents = agents or []
    invalid = [agent for agent in requested_agents if resolve_skill_agent(agent) is None]
    if invalid:
        return {"ok": False, "error_type": "unsupported_agent", "summary": "Agentic HIL does not know one or more requested agents.", "agents": invalid, "allowed_agents": supported_skill_agents()}

    holders = _processes_holding_installation()
    if holders:
        return {
            "ok": False,
            "error_type": "installation_in_use",
            "summary": "Another process is running out of this installation, so upgrading it now would leave no working installation at all. Nothing was changed.",
            "python": sys.executable,
            "installation_root": str(Path(sys.prefix)),
            "held_by": holders[:_REPORTED_HOLDER_LIMIT],
            "held_by_count": len(holders),
            "installed_version": __version__,
            **remediation_fields("installation_in_use"),
        }

    manager, command = _upgrade_command()
    previous_version = __version__
    try:
        installed = _run_upgrade_process(command)
    except (OSError, subprocess.TimeoutExpired) as error:
        return {"ok": False, "error_type": "upgrade_failed", "summary": "Agentic HIL package upgrade could not run.", "manager": manager, "command": command, "previous_version": previous_version, "exception_type": type(error).__name__, "detail": str(error)}
    install_result = _process_result(installed)
    if installed.returncode != 0:
        return {"ok": False, "error_type": "upgrade_failed", "summary": "Agentic HIL package manager reported an upgrade failure.", "manager": manager, "command": command, "previous_version": previous_version, "install": install_result}

    version_command = [sys.executable, "-m", "agentic_hil", "--version"]
    verified = _run_upgrade_process(version_command)
    verification = _process_result(verified)
    current_version = verified.stdout.strip() if verified.returncode == 0 else None
    if verified.returncode != 0 or not current_version:
        return {"ok": False, "error_type": "upgrade_verification_failed", "summary": "Package manager completed, but updated Agentic HIL could not be loaded by this installation's Python.", "manager": manager, "command": command, "previous_version": previous_version, "install": install_result, "verification": verification}

    skill_results: JsonObject = {}
    with tempfile.TemporaryDirectory(prefix="agentic-hil-upgrade-") as maintenance_cwd:
        for agent in requested_agents:
            skill_command = [sys.executable, "-m", "agentic_hil", "skill-install", "--agent", agent]
            refreshed = _run_upgrade_process(skill_command, cwd=maintenance_cwd)
            child = _process_result(refreshed)
            if refreshed.stdout.strip():
                with suppress(json.JSONDecodeError):
                    child["result"] = json.loads(refreshed.stdout)
            skill_results[agent] = child

    skills_ok = all(result.get("returncode") == 0 for result in skill_results.values())
    return {
        "ok": skills_ok,
        "tool": "agentic_hil_upgrade",
        "summary": "Agentic HIL upgraded; restart agent hosts to load the new MCP server." if skills_ok else "Agentic HIL package upgraded, but one or more agent skills could not be refreshed.",
        "manager": manager,
        "command": command,
        "python": sys.executable,
        "previous_version": previous_version,
        "version": current_version,
        "install": install_result,
        "skills": skill_results,
        "restart_required": True,
    }


def _agent_mcp_config_path(agent_id: str) -> Path:
    paths = {
        "claude-code": Path.home() / ".claude.json",
        "codex": Path.home() / ".codex" / "config.toml",
        "opencode": Path.home() / ".config" / "opencode" / "opencode.json",
    }
    return _external_user_path(paths[agent_id], "MCP user configuration")


def _agent_permission_config_path(agent_id: str) -> Path | None:
    """Where an agent CLI keeps the rules it applies to its own tools.

    Codex is absent on purpose: it sandboxes model-generated shell commands and
    leaves MCP servers outside that sandbox, so its default policy already
    refuses a write here while the server keeps reading the config.
    """
    paths = {
        "claude-code": Path.home() / ".claude" / "settings.json",
        "opencode": Path.home() / ".config" / "opencode" / "opencode.json",
    }
    if agent_id not in paths:
        return None
    return _external_user_path(paths[agent_id], "Agent permission configuration")


def _external_user_path(path: Path, label: str) -> Path:
    absolute = absolute_without_symlinks(path)
    workspace = absolute_without_symlinks(Path.cwd())
    if is_path_within_frozen(absolute, workspace):
        raise ConfigError("unsafe_configured_path", f"{label} must be stored outside the project workspace.", {"field": "user_config", "path": str(absolute), "workspace_root": str(workspace)})
    return absolute


def _unique_paths(paths: list[Path]) -> list[Path]:
    """One entry per real path.

    opencode keeps its MCP entry and its permissions in one file. Locking a path
    twice deadlocks on its own sidecar, so each one appears once.
    """
    unique: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        identity = os.path.normcase(str(absolute_without_symlinks(path)))
        if identity not in seen:
            seen.add(identity)
            unique.append(path)
    return unique


def _agent_skill_target(agent: SkillAgent) -> Path:
    return _external_user_path(Path(agent.default_target_path), "Default agent skill")


def _user_mutation_paths(agent: SkillAgent) -> list[Path]:
    """Everything the user-wide half may write.

    The agent's skill, its user-level MCP registration, and — for an agents-md
    agent — the file that registers the skill. No project path appears here, so
    a project step that fails can never roll one of these back.
    """
    skill_path = _agent_skill_target(agent)
    paths = [skill_path, _agent_mcp_config_path(agent.id)]
    if agent.registration == "agents-md":
        paths.append(Path(skill_install_root(str(skill_path))) / "AGENTS.md")
    return _unique_paths(paths)


def _project_mutation_paths(agent: SkillAgent | None, config_path: Path) -> list[Path]:
    """Everything the project half may write.

    This workspace's authoritative config, and the agent's own write-permission
    rules — which name this project's config directory and state root, so they
    are project content even though the file is user-level.

    For opencode that permission file is also the file the user-wide half
    registered the MCP server in. Snapshots are taken when this half starts,
    after the user-wide half has finished, so restoring it restores the
    registration rather than removing it.
    """
    paths = [config_path]
    if agent is not None:
        permission_path = _agent_permission_config_path(agent.id)
        if permission_path is not None:
            paths.append(permission_path)
    return _unique_paths(paths)


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


def _smooth_permissions(targets: list[Path]) -> list[str]:
    """Silently tighten the current user's own group/other-writable directories
    along these chains so the fail-closed trust validators accept a umask-002 /
    private-group home without the operator hand-fixing permissions. Only
    user-owned components are ever changed. POSIX only; on Windows this is a
    no-op."""
    actions: list[str] = []
    seen: set[str] = set()
    for target in targets:
        key = os.path.normcase(str(target))
        if key in seen:
            continue
        seen.add(key)
        actions.extend(tighten_owned_writable_ancestors(target))
    return actions


def _smooth_user_permissions(mutation_paths: list[Path]) -> list[str]:
    """Smooth only what the user-wide half needs: the agent skill, the
    user-level MCP config, and the launcher chains the executable trust check
    walks. It never touches the authoritative config or the state root, so a
    profile that refuses those is not this half's problem."""
    targets: list[Path] = [*mutation_paths]
    for candidate in _mcp_command_candidates():
        targets.append(Path(candidate))
        with suppress(OSError):
            targets.append(Path(os.path.realpath(candidate)))
    return _smooth_permissions(targets)


def _smooth_project_permissions(config_path: Path, mutation_paths: list[Path]) -> list[str]:
    """Smooth only what the project half needs: the state root, this workspace's
    authoritative config, and the agent's permission file."""
    return _smooth_permissions([user_state_root(), config_path, *mutation_paths])


def _unsupported_agent(agent: str, summary: str) -> JsonObject:
    return {"ok": False, "error_type": "unsupported_agent", "summary": summary, "agent": normalize_agent(agent), "allowed_agents": supported_skill_agents()}


def install_agent(agent: str, force: bool = False) -> JsonObject:
    """Install what is user-wide, once per user and agent.

    Everything here lands under the invoking user's home directory, so the scope
    is per user, per machine: every process of this OS user on this host, and no
    other OS user on it.

    The agent's skill, its user-level MCP registration, and the check that there
    is a persistent trusted executable to register. None of that is project
    state, so this runs with no workspace and no readable authoritative config —
    and it has to. On a stock Windows profile the configuration location fails
    the ancestor trust check, and while both halves shared one transaction that
    refusal took the skill and the MCP entry down with it, leaving the operator
    with nothing rather than with a working agent.

    Rollback covers these paths and no others.
    """
    resolved_agent = resolve_skill_agent(agent)
    if resolved_agent is None:
        return _unsupported_agent(agent, "Agentic HIL does not know this agent's setup paths.")

    mutation_paths = _user_mutation_paths(resolved_agent)
    # Smooth the common umask-002 friction before the fail-closed validators run,
    # tightening only the operator's own writable dirs.
    permission_changes = _smooth_user_permissions(mutation_paths)
    command = mcp_server_command()
    skill_result = _skipped_setup_step("Skill installation was not reached.")
    mcp_result = _skipped_setup_step("MCP registration was not reached.")

    with ExitStack() as locks:
        for path in sorted(mutation_paths, key=lambda item: os.path.normcase(str(item))):
            locks.enter_context(secure_user_file_lock(path))
        # Same as install_skill: rollback covers the superseded skill, the lock
        # does not, so removing it can take its empty directory too.
        legacy_paths = legacy_skill_paths(_agent_skill_target(resolved_agent))
        snapshots = _capture_file_snapshots([*mutation_paths, *legacy_paths])
        try:
            skill_result = install_skill(agent, None, force, _locked=True)
            if overall_success(skill_result):
                mcp_result = register_agent_mcp(agent, force=force, command=command, _locked=True)
        except BaseException as error:
            rollback_errors = _restore_file_snapshots(snapshots)
            if isinstance(error, ConfigError) and rollback_errors:
                error.details["rollback_errors"] = rollback_errors
            raise

        ok = all(overall_success(result) for result in (skill_result, mcp_result))
        rollback_errors = [] if ok else _restore_file_snapshots(snapshots)
        result: JsonObject = {
            "ok": ok and not rollback_errors,
            "tool": "agentic_hil_agent_install",
            "scope": "user",
            "summary": "Agentic HIL agent integration installed for this user account." if ok else "Agentic HIL agent installation failed; committed file changes were rolled back.",
            "agent": agent,
            "command": command,
            "permission_changes": permission_changes,
            "rollback": {"attempted": not ok, "ok": not rollback_errors, "errors": rollback_errors},
            "steps": {"skill_install": skill_result, "mcp_config": mcp_result},
            "next_step": "Bind a project to it with `agentic-hil init` from that project root. This half never has to run again for this user on this machine.",
        }
        if not result["ok"]:
            surviving = _skill_rollback_did_not_own(snapshots, _agent_skill_target(resolved_agent))
            if surviving is not None:
                result["left_behind"] = surviving
        return result


def init_project(config_path: str | None = None, agent: str | None = None, force: bool = False) -> JsonObject:
    """Set up one project: its authoritative config, verified by doctor.

    Everything here is project state, so it reuses `init_config` rather than
    repeating it, and its rollback covers this workspace's config plus the
    agent's write-permission rules — never the user-wide skill or MCP
    registration. Naming an agent is optional: without one the config is still
    written and verified, and only the step that asks that agent to refuse its
    own write tools is left out.
    """
    workspace = Path.cwd().resolve()
    resolved_agent: SkillAgent | None = None
    if agent is not None:
        resolved_agent = resolve_skill_agent(agent)
        if resolved_agent is None:
            return _unsupported_agent(agent, "Agentic HIL does not know this agent's setup paths.")

    target_path = initialized_config_path(workspace)
    validate_legacy_config_selector(config_path, workspace, target_path)
    config_exists = _path_entry_exists(target_path)
    mutation_paths = _project_mutation_paths(resolved_agent, target_path)
    permission_changes = _smooth_project_permissions(target_path, mutation_paths)
    state_actions = ensure_safe_state_root()
    config_result: JsonObject
    doctor_result = _skipped_setup_step("Doctor was not reached.")
    permission_result: JsonObject
    if resolved_agent is None:
        permission_result = {"ok": True, "skipped": True, "summary": "No agent was named, so no agent write restriction was applied. Pass --agent to have that agent refuse its own write tools on the policy files."}
    else:
        permission_result = _skipped_setup_step("Agent write restriction was not reached.")

    # An existing config that is being kept is not this run's to write, so it
    # stays out of the rollback set: an operator's read-only policy file must
    # neither be locked nor restored by a later step that fails.
    keep_existing = config_exists and not force
    if keep_existing:
        mutation_paths.remove(target_path)
    with ExitStack() as locks:
        for path in sorted(mutation_paths, key=lambda item: os.path.normcase(str(item))):
            locks.enter_context(secure_user_file_lock(path))
        snapshots = _capture_file_snapshots(mutation_paths)
        try:
            if keep_existing:
                config_result = {"ok": True, "skipped": True, "summary": "Existing authoritative config kept; this never replaces operator policy.", "path": str(target_path)}
            else:
                config_result = init_config(config_path, force=force, _locked=True)

            if overall_success(config_result):
                doctor_result = doctor(config_path)
            if resolved_agent is not None and all(overall_success(result) for result in (config_result, doctor_result)):
                # Last, because it forbids writing the very file the steps above
                # had to create.
                permission_result = restrict_agent_write_access(
                    resolved_agent.id, target_path, Path(load_config(str(target_path)).state_root)
                )
        except BaseException as error:
            rollback_errors = _restore_file_snapshots(snapshots)
            if isinstance(error, ConfigError) and rollback_errors:
                error.details["rollback_errors"] = rollback_errors
            raise

        ok = all(overall_success(result) for result in (config_result, doctor_result, permission_result))
        rollback_errors = [] if ok else _restore_file_snapshots(snapshots)
        return {
            "ok": ok and not rollback_errors,
            "tool": "agentic_hil_init",
            "scope": "project",
            "summary": "Agentic HIL project configured." if ok else "Agentic HIL project setup failed; its own committed file changes were rolled back.",
            "agent": agent,
            "config_path": str(target_path),
            "state_root_changes": state_actions,
            "permission_changes": permission_changes,
            "rollback": {"attempted": not ok, "ok": not rollback_errors, "errors": rollback_errors},
            "steps": {
                "config": config_result,
                "doctor": doctor_result,
                "agent_write_restriction": permission_result,
            },
        }


def _unreached_project_scope(summary: str) -> JsonObject:
    return {
        "ok": False,
        "skipped": True,
        "tool": "agentic_hil_init",
        "scope": "project",
        "summary": summary,
        "state_root_changes": [],
        "permission_changes": [],
        "rollback": {"attempted": False, "ok": True, "errors": []},
        "steps": {
            "config": _skipped_setup_step("Authoritative config was not reached."),
            "doctor": _skipped_setup_step("Doctor was not reached."),
            "agent_write_restriction": _skipped_setup_step("Agent write restriction was not reached."),
        },
    }


def _refused_project_scope(error: ConfigError) -> JsonObject:
    """Report a project half that was refused before it changed anything.

    Raising here would leave the operator reading a configuration refusal with
    no word about the user-wide half that just succeeded — which is the whole
    thing they get to keep on a profile that refuses the configuration location.
    """
    refusal = error.to_dict()
    scope = _unreached_project_scope(str(refusal.get("summary") or "The project half was refused."))
    scope["steps"]["config"] = refusal
    return scope


def setup_project(agent: str, force: bool = False) -> JsonObject:
    """First run in one command: the user-wide half, then the project half.

    A composition of two independently runnable commands, not one transaction
    across both. Each half owns its rollback set, so a project step that fails
    leaves the installed skill and the MCP registration standing — which is the
    point where the configuration location is refused and the user-wide half
    has nothing to do with it. The second project for this user needs
    `agentic-hil init` alone.

    `--force` reaches the user-wide half only. It repairs a managed skill or
    MCP entry; it never rewrites an authoritative config, which is operator
    policy and only `agentic-hil init --force` touches.
    """
    user_result = install_agent(agent, force)
    if "steps" not in user_result:
        return user_result
    if overall_success(user_result):
        try:
            project_result = init_project(agent=agent)
        except ConfigError as error:
            project_result = _refused_project_scope(error)
    else:
        project_result = _unreached_project_scope("Project setup was not reached; the user-wide half did not complete, and nothing project-local was changed.")

    user_ok = overall_success(user_result)
    ok = user_ok and overall_success(project_result)
    if ok:
        summary = "Agentic HIL project set up."
    elif user_ok:
        summary = "Agentic HIL is installed for this agent under this user account; the project half failed and only its own file changes were rolled back."
    else:
        summary = "Agentic HIL user-wide agent installation failed and was rolled back; nothing project-local was changed."
    rollback_errors = [*user_result["rollback"]["errors"], *project_result["rollback"]["errors"]]
    result: JsonObject = {
        "ok": ok,
        "tool": "agentic_hil_setup",
        "summary": summary,
        "agent": agent,
        "state_root_changes": project_result["state_root_changes"],
        "permission_changes": [*user_result["permission_changes"], *project_result["permission_changes"]],
        "rollback": {
            "attempted": user_result["rollback"]["attempted"] or project_result["rollback"]["attempted"],
            "ok": not rollback_errors,
            "errors": rollback_errors,
        },
        "scopes": {"user": user_result, "project": project_result},
        "steps": {
            "config": project_result["steps"]["config"],
            "skill_install": user_result["steps"]["skill_install"],
            "mcp_config": user_result["steps"]["mcp_config"],
            "doctor": project_result["steps"]["doctor"],
            "agent_write_restriction": project_result["steps"]["agent_write_restriction"],
        },
    }
    if user_ok and not ok:
        result["next_step"] = (
            "The agent is installed for this user and stays installed; only the project half was rolled back. "
            "Fix what its `config` or `doctor` step reports, then run "
            f"`agentic-hil init --agent {agent}` from this project root. "
            "`agentic-hil agent-install` does not have to run again."
        )
    if "left_behind" in user_result:
        result["left_behind"] = user_result["left_behind"]
    return result


def _skill_rollback_did_not_own(snapshots: list[FileSnapshot], skill_target: Path) -> JsonObject | None:
    """Name a skill a failed installation restored rather than removed.

    Rollback puts every file back the way this half found it, so a skill an
    earlier `skill-install` had already written survives on purpose: it only
    takes back what it wrote. That leaves the operator with a skill that routes
    hardware work to tools no longer registered, which is inert but easy to
    miss, so the refusal says so instead of leaving it to be discovered.
    """
    identity = os.path.normcase(str(absolute_without_symlinks(skill_target)))
    for snapshot in snapshots:
        if os.path.normcase(str(snapshot.path)) != identity or snapshot.content is None:
            continue
        if not _path_entry_exists(snapshot.path):
            return None
        return {
            "skill": str(snapshot.path),
            "summary": "A skill installed before this setup is still there; setup only takes back what setup wrote.",
            "next_step": (
                f"That skill points hardware work at Agentic HIL tools this setup did not register. "
                f"Either resolve the conflict this refusal names and run setup again, or delete "
                f"{snapshot.path} if you no longer want it."
            ),
        }
    return None


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
    profile = load_project_profile(workspace)
    discovery = discover_attached_hardware() if profile is not None else None
    if profile is not None and discovery is not None and overall_success(discovery):
        template = yaml.safe_load(DEFAULT_CONFIG_TEMPLATE)
        assert isinstance(template, dict)
        configured = apply_discovery_to_template(template, profile, discovery)
        document = {"workspace_root": str(workspace), "state_root": str(user_state_root()), **configured}
        text = yaml.safe_dump(document, sort_keys=False, allow_unicode=False)
    else:
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
        "summary": "Attached hardware was discovered and configured." if discovery is not None and overall_success(discovery) else "Deny-by-default Agentic HIL project configuration written.",
        "path": str(target_path),
        "optional_override": f'{CONFIG_ENV}={target_path}',
        "available_com_ports": available_com_ports,
        "hardware_discovery": discovery,
        "next_steps": init_next_steps(available_com_ports, target_path),
    }


def adopt_hardware(*, debugger_id: str | None = None, com_port_id: str | None = None, probe_id: str | None = None, dry_run: bool = False) -> JsonObject:
    """Carry the attached board into this project's configuration, as the operator.

    The same computation and the same write path the MCP tool uses, run under the
    authority `agentic-hil init` already runs under: a person at a terminal, in
    their own project. So the description grant is not consulted here — a
    placeholder configuration has it false, and requiring it would mean editing
    the YAML this command exists to stop anyone editing.

    That is not a hole an agent with a shell can widen. This command takes no
    value from its caller: the probe serial, the toolchain path, the controller
    and the COM device all come from what is attached to this machine. It fills
    only what is unset, so it cannot repoint a bench somebody configured. And it
    names no permission, which the before/after comparison inside the write path
    enforces from the document rather than from the request. The same shell
    already has `agentic-hil init --force`, which rewrites the whole file and
    resets every grant in it to false — strictly more destructive, and of no use
    to anyone trying to gain something.

    The grant is what differs, and only the grant. Reading the probe goes through
    the same coordinator every other hardware call on this machine goes through,
    so this command waits for nothing, quarantines nothing and reads nothing that
    an MCP server or another terminal is holding — a person's authority over
    their own configuration is not authority over somebody else's running bench.
    """
    config = load_cli_authoritative_config(None)
    return project_config_adopt_hardware(
        Path(config.work_dir),
        config,
        {"apply": not dry_run, "debugger_id": debugger_id, "com_port_id": com_port_id, "probe_id": probe_id},
        frontend="operator-cli",
        actor=ACTOR_HUMAN,
        via="cli:adopt-hardware",
    )


def initialized_config_path(workspace: Path) -> Path:
    """Where `init` writes. The same rule the agent provisioning path follows, so
    the two cannot disagree about which file is authoritative."""
    return authoritative_config_target(workspace)


def run_test_reactor(test_config_path: str | None = None, *, wait_s: float = 0.0) -> JsonObject:
    config = load_authoritative_config(Path.cwd())
    test_config = load_test_config(test_config_path, config.work_dir)
    service = AgenticHILToolService(config, frontend="reactor")
    # The plan's devices are held from before its first step to after its last,
    # not around each call: between two steps there would otherwise be no lock at
    # all, and an observation from outside could reach the board exactly where
    # the plan assumes nothing moved. The same declaration fixes what this run
    # may touch, so a step reaching past the plan is refused rather than
    # silently widening what the plan says it does.
    plan = plan_devices(config, test_config)
    devices = plan.lock_keys
    if devices:
        try:
            service.coordinator.begin_run(plan, label=test_config.name, wait_s=wait_s)
        except CoordinationError as error:
            service.close()
            return write_report(
                config,
                {
                    "tool": "test_reactor",
                    "name": test_config.name,
                    "test_config_path": test_config.path,
                    "steps": [],
                    "cleanup": [],
                    "cleanup_ok": True,
                    "declared_devices": devices,
                    **error.result,
                    "summary": str(error.result.get("summary", "A device this plan declares is unavailable.")) + " No step ran.",
                },
            )
    # A step naming another probe gets its own service driving that debugger,
    # sharing the base coordinator so the whole project stays one owner.
    def debugger_service_factory(bound_config: AgenticHILConfig) -> AgenticHILToolService:
        return AgenticHILToolService(bound_config, coordinator=service.coordinator, frontend="reactor")

    # Construction happens inside the guarded block: the factory builds real
    # per-probe services while the plan runs, so a failure there must still
    # produce a JSON error result and fall through to service.close() below.
    reactor: TestReactor | None = None
    primary_error: BaseException | None = None
    try:
        reactor = TestReactor(service.config, service, service_factory=debugger_service_factory)
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
        f"Review the config at {config_path}. Set {CONFIG_ENV} only when an explicit absolute-path override is needed.",
        "Edit target.name and target.controller for your board.",
        "Set interface_cfg and target_cfg on your debuggers entry for your OpenOCD setup.",
        "Reading needs nothing granted: probing, serial reads and CAN reads work as written. Grant only what writes or changes state under debuggers.<name>.permissions; those flags stay deny-by-default.",
        "If multiple debug probes are connected, give each debuggers entry the full unique id of its own probe; run `agentic-hil debugger-probes` to list them (OpenOCD cannot enumerate — read the serial off the probe). Test-reactor plan steps then address a board by its name; the MCP tools require exactly one configured probe.",
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


def _mcp_command_candidates() -> list[str]:
    """The console-script locations we consider for the pinned MCP launcher."""
    candidates: list[str] = []
    if found := shutil.which("agentic-hil"):
        candidates.append(found)
    script = Path(sys.executable).parent / ("agentic-hil.exe" if os.name == "nt" else "agentic-hil")
    candidates.append(str(script))
    return list(dict.fromkeys(candidates))


def mcp_server_command() -> str:
    """Return one pinned, persistent console-script path or fail closed."""

    rejected: list[JsonObject] = []
    for candidate in _mcp_command_candidates():
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


def _protected_write_globs(config_path: Path, state_root: Path) -> list[str]:
    """The two trees an agent must not write once setup has created them."""
    return [f"{config_path.parent.as_posix()}/**", f"{state_root.as_posix()}/**"]


def _claude_code_deny_patterns(config_path: Path, state_root: Path) -> list[str]:
    """The same two trees, written the way Claude Code actually resolves a path.

    Two things are observed rather than assumed here, both from
    https://code.claude.com/docs/en/permissions:

    `Edit` is the only file form that is consulted. "Claude Code checks file
    permissions against `Edit(path)` and `Read(path)` rules only"; a `Write(...)`
    path rule "is accepted but never consulted", and warned about at every start.
    One `Edit` rule covers every file-editing tool, so it needs no twin.

    A pattern needs two leading slashes to mean an absolute path. One leading
    slash "anchors at the settings source, not the filesystem root" — and these
    rules go into user settings, where that source is `~/.claude`. Windows paths
    are normalised to POSIX form before matching, so `C:\\Users\\alice` matches
    as `/c/Users/alice`.
    """
    return [f"/{_posix_filesystem_path(path)}/**" for path in (config_path.parent, state_root)]


def _posix_filesystem_path(path: PurePath) -> str:
    """An absolute path in the POSIX form Claude Code normalises to: a Windows
    drive letter becomes a lowercase leading segment, `C:/Users` -> `/c/Users`."""
    posix = path.as_posix()
    drive, colon, rest = posix.partition(":")
    if colon and len(drive) == 1 and drive.isalpha():
        return f"/{drive.lower()}{rest}"
    return posix


def _stale_claude_code_deny_rules(config_path: Path, state_root: Path) -> set[str]:
    """What earlier releases wrote here and this one has to take back.

    Earlier releases built both rules straight from the absolute path: a
    `Write(...)` that Claude Code never consults, and an `Edit(...)` whose single
    leading slash anchored it under `~/.claude` instead of at the filesystem root.
    The first is loud — a yellow warning at every start (hardci-hq#81) — and the
    second is silent, which is worse: it reads as protection and is not.

    Identifying them needs no heuristic. The globs are derived from this
    project's config path and state root, so a rule is ours exactly when its text
    is one of these few strings. An operator's own `Write(...)` or `Edit(...)`
    rule names some other path and is therefore never one of them.
    """
    return {f"{form}({glob})" for form in ("Edit", "Write") for glob in _protected_write_globs(config_path, state_root)}


_OPENCODE_UNRESTRICTED = "Agentic HIL writes no write restriction for opencode; whether its file tools may reach the authoritative config and state root is the operator's to set, in ~/.config/opencode/opencode.json."


def _stale_opencode_deny_patterns(config_path: Path, state_root: Path) -> set[str]:
    """The absolute `permission.edit` patterns earlier releases wrote.

    They match nothing at all. opencode compares that key against the
    worktree-relative path, established from its source rather than from its
    documentation, which does not say (hardci-hq#84): `write.ts`, `edit.ts` and
    `apply_patch.ts` ask with `patterns: [path.relative(instance.worktree,
    filepath)]`, `evaluate` in `permission/index.ts` matches the configured
    pattern against that subject, and `Wildcard.match` anchors it `^...$`. Both
    trees are required to live outside the workspace, so the subject always reads
    `../../.config/agentic-hil/...` and nothing absolute can equal it.

    They are taken back rather than corrected, because Agentic HIL no longer
    writes a restriction for this host at all — see `restrict_agent_write_access`.
    A rule that reads as protection and is not is worse than none, which is the
    silent half of hardci-hq#81 over again.

    Identifying them needs the same argument as `_stale_claude_code_deny_rules`:
    the text is derived from this project's own paths, so an operator's own
    pattern is never one of them, and one of ours they have since set to
    something other than `deny` is theirs now and stays.
    """
    return set(_protected_write_globs(config_path, state_root))


def restrict_agent_write_access(agent_id: str, config_path: Path, state_root: Path) -> JsonObject:
    """Ask the agent CLI to refuse its own write tools on the policy files.

    Installation and use need opposite things: setup must be able to create the
    authoritative config, and afterwards nothing but the operator may change it.
    So this runs last, and it constrains the agent's file tools rather than a
    path — a path rule would also cover `agentic-hil mcp-stdio`, which has to
    keep reading the very file being protected.

    It is a lock on the front door, not a wall. A shell can still write the file,
    which is why SECURITY.md asks for a separate identity where that matters.

    Which rule form a host actually evaluates is read out of that host's own
    documentation rather than expected — see `_claude_code_deny_patterns`.

    Only claude-code gets a rule written. On opencode the operator decides for
    themselves, and nothing is written on their behalf: its permission model
    hands the decision to whoever answers the prompt anyway — one "always" adds a
    session `allow` for pattern `*`, which `evaluate` reads after the configured
    ruleset and which therefore outranks any denial written here. A rule that a
    single click removes is not a lock, and offering it as one would say
    something about this bench that is not true. Codex needs nothing.
    """
    path = _agent_permission_config_path(agent_id)
    if path is None:
        return {
            "ok": True,
            "mode": "sandboxed-by-the-agent",
            "summary": "Codex sandboxes model-generated shell commands and leaves MCP servers outside that sandbox, so its own policy already refuses this write.",
        }
    document = _load_json_object(path)
    if document is None:
        return {"ok": False, "error_type": "agent_permissions_unreadable", "summary": f"{path} is not a JSON object; left untouched.", "path": str(path)}

    removed: list[str] = []
    if agent_id == "claude-code":
        # Documented to merge across scopes rather than override, so adding
        # rules never removes the operator's own.
        permissions = document.setdefault("permissions", {})
        if not isinstance(permissions, dict):
            return {"ok": False, "error_type": "agent_permissions_unreadable", "summary": f"{path} has a non-object permissions entry; left untouched.", "path": str(path)}
        deny = permissions.setdefault("deny", [])
        if not isinstance(deny, list):
            return {"ok": False, "error_type": "agent_permissions_unreadable", "summary": f"{path} has a non-list deny entry; left untouched.", "path": str(path)}
        stale = _stale_claude_code_deny_rules(config_path, state_root)
        removed = [rule for rule in deny if rule in stale]
        if removed:
            deny = permissions["deny"] = [rule for rule in deny if rule not in stale]
        # `Edit`, `//`-anchored, and nothing else. See _claude_code_deny_patterns.
        added = [f"Edit({pattern})" for pattern in _claude_code_deny_patterns(config_path, state_root) if f"Edit({pattern})" not in deny]
        deny.extend(added)
    else:
        # opencode gets no restriction written for it, on purpose. What it does
        # get is the removal of the absolute patterns earlier releases left,
        # which protect nothing — see `_stale_opencode_deny_patterns`.
        added = []
        permission = document.get("permission")
        existing = permission.get("edit") if isinstance(permission, dict) else None
        if isinstance(existing, dict):
            stale = _stale_opencode_deny_patterns(config_path, state_root)
            removed = [pattern for pattern, action in existing.items() if pattern in stale and action == "deny"]
            for pattern in removed:
                del existing[pattern]
    mode = "tool-permissions" if agent_id == "claude-code" else "operator-managed"
    settled = "The agent already refuses to write the policy files." if agent_id == "claude-code" else _OPENCODE_UNRESTRICTED
    if not added and not removed:
        return {"ok": True, "mode": mode, "summary": settled, "path": str(path), "added": [], "removed": []}
    # Not sorted, so an operator's own file comes back in the order they wrote it
    # — which for opencode is also the order its rules are evaluated in.
    secure_atomic_write_text(path, json.dumps(document, indent=2) + "\n")
    if added:
        summary = f"{agent_id} will refuse its own write tools on the authoritative config and state root."
        if removed:
            summary += " The inert deny rules an earlier setup wrote were dropped."
    else:
        summary = f"{_OPENCODE_UNRESTRICTED} The inert deny patterns an earlier setup wrote were dropped."
    return {"ok": True, "mode": mode, "summary": summary, "path": str(path), "added": added, "removed": removed}


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
        return {"ok": False, "error_type": "mcp_config_conflict", "agent": "codex", "format": "codex-toml", "path": str(path), "summary": "An unmanaged Codex agentic-hil MCP entry already exists; left untouched.", "next_step": CONFLICT_NEXT_STEP}
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


def _replaceable_agentic_hil_command(configured: object) -> bool:
    """Whether a stale absolute command can be attributed to this installation.

    A JSON agent config carries no managed marker, so shape alone cannot tell our
    own earlier entry from one an operator wrote. A workspace-local command is
    replaceable by definition, and anything else must pass the same trust check
    as a new command. An operator's own absolute path satisfies neither and is
    reported as a conflict instead of being silently repointed.
    """
    if not isinstance(configured, str):
        return False
    path = Path(configured).expanduser()
    if not path.is_absolute():
        return False
    with suppress(OSError, ValueError):
        if is_path_within_frozen(path, Path.cwd().resolve()):
            return True
    try:
        _trusted_mcp_command(str(path))
    except (ConfigError, OSError, ValueError):
        return False
    return True


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
    if (
        isinstance(entry, dict)
        and set(entry) == {"type", "command", "args"}
        and entry.get("type") == "stdio"
        and entry.get("args") == ["mcp-stdio"]
        and _replaceable_agentic_hil_command(entry.get("command"))
    ):
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
        if isinstance(configured, list) and len(configured) == 2 and configured[1] == "mcp-stdio" and _replaceable_agentic_hil_command(configured[0]):
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
        return {"ok": False, "error_type": "mcp_config_conflict", "agent": "opencode", "format": "opencode-json", "path": str(path), "summary": "An unmanaged opencode agentic-hil MCP entry already exists; left untouched.", "next_step": CONFLICT_NEXT_STEP}
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
        return {"ok": False, "error_type": "mcp_config_conflict", "agent": "claude-code", "format": "claude-user", "method": "file", "path": str(path), "summary": "An unmanaged Claude agentic-hil MCP entry already exists; left untouched.", "next_step": CONFLICT_NEXT_STEP}
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


def _doctor_probe_check(config: AgenticHILConfig, debugger_id: str) -> tuple[JsonObject, JsonObject]:
    """Run debugger_info and the target-support check against one named probe.

    Both use the one service, so the toolchain is resolved once and neither
    check touches the hardware: target support is a question about what this
    host's toolchain resolves, not about what is plugged in.
    """
    service = AgenticHILToolService(bind_debugger(config, debugger_id), frontend="doctor")
    primary_error: BaseException | None = None
    support: JsonObject = {"ok": True, "tool": "debugger_target_support", "status": "undetermined", "undetermined_reason": "the debugger check did not complete."}
    try:
        result = service.call("debugger_info")
        support = _doctor_target_support(service)
    except BaseException as error:
        primary_error = error
        result = {"ok": False, "tool": "debugger_info", "summary": "Debugger check raised an exception.", "backend_error": str(error)}
    try:
        service.close()
    except BaseException as cleanup_error:
        if primary_error is None:
            raise
        primary_error.args = (*primary_error.args, f"Cleanup error: {cleanup_error}")
    if primary_error is not None:
        raise primary_error
    return result, support


def _doctor_target_support(service: AgenticHILToolService) -> JsonObject:
    """Ask the bound backend whether it resolves the configured target type.

    A backend that raises here has told us nothing, so the answer is
    "undetermined" and not a failure. `doctor` going red decides whether
    `setup` rolls back, and a host that simply has no CMSIS pack cache yet
    must not look like a broken configuration.
    """
    try:
        # Broad on purpose: a diagnostic must never become the failure it reports.
        support = service.backend.target_support()
    except Exception as error:
        return {"ok": True, "tool": "debugger_target_support", "status": "undetermined", "undetermined_reason": f"the target-support check raised {type(error).__name__}: {error}"}
    return support if isinstance(support, dict) else {"ok": True, "tool": "debugger_target_support", "status": "undetermined", "undetermined_reason": "the backend returned no target-support report."}


def doctor(config_path: str | None = None) -> JsonObject:
    try:
        config = load_cli_authoritative_config(config_path)
    except ConfigError as error:
        result = error.to_dict()
        result["tool"] = "agentic_hil_doctor"
        return result
    # Check every probe whose toolchain this configuration required to resolve,
    # not only a bound one. Reporting a green doctor because nothing was bound
    # would hide an unplugged board, a wrong probe_id, or a broken toolchain on
    # exactly the multi-board bench that most needs the check.
    #
    # Reading needs no permission any more, so "may be probed" no longer says
    # anything about what this bench is meant to drive; a config that granted
    # nothing would otherwise demand a debugger toolchain from every operator
    # the moment `init` wrote it. What the config did pin is the honest signal,
    # and it is the same set config load already insisted on resolving.
    probed = {name: _doctor_probe_check(config, name) for name, entry in config.debuggers.items() if debugger_access_enabled(entry)}
    checks = {name: result for name, (result, _) in probed.items()}
    target_support = {name: support for name, (_, support) in probed.items()}
    checked = [result for result in checks.values() if result.get("skipped") is not True]
    debugger_info = next(iter(checks.values()), None) or {
        "ok": True,
        "tool": "debugger_info",
        "skipped": True,
        "summary": "Debugger check skipped: no configured debugger pins a toolchain, so there is nothing to check yet. Reading a probe needs no permission; granting allow_flash or allow_reset is what makes a board one this bench drives.",
    }
    # Only a definite negative is a failure. A target-support check that could
    # not run said nothing about this configuration, and reporting it as broken
    # would make `doctor` red — and `setup` roll back — on any host that has no
    # debugger toolchain installed yet.
    unsupported = sorted(name for name, support in target_support.items() if support.get("ok") is not True)
    undetermined = sorted(name for name, support in target_support.items() if support.get("status") == "undetermined")
    all_ok = all(result.get("ok") is True for result in checks.values()) and not unsupported
    if not checked:
        summary = "Agentic HIL authoritative configuration loaded; debugger check skipped."
    elif all_ok:
        summary = f"Agentic HIL configuration loaded and {len(checked)} debugger(s) checked."
    else:
        failed = sorted(name for name, result in checks.items() if result.get("ok") is not True)
        parts = []
        if failed:
            parts.append(f"the debugger check failed for: {', '.join(failed)}")
        if unsupported:
            parts.append(f"the configured target_type is not resolvable for: {', '.join(unsupported)}")
        summary = f"Agentic HIL configuration loaded, but {'; and '.join(parts)}."
    if undetermined:
        summary += f" Target support could not be determined here for: {', '.join(undetermined)}; that is unknown, not broken."
    # Checked at the end, against the configuration this run was decided by. The
    # probe checks spawn debugger processes and take seconds, so the file can
    # move inside a single `doctor`; a report printed out of a document that has
    # since been replaced is the same defect at a smaller scale. The block also
    # publishes the digest that makes a running server's staleness diagnosable
    # from here, which is the disagreement that produced this: `doctor` said
    # pyocd and `debugger_info` said stlink in the same minute, and nothing named
    # the difference.
    status = {**config_status(config), "compare_with_running_server": RUNNING_SERVER_COMPARISON}
    report = with_config_status(
        {
            "ok": all_ok,
            "tool": "agentic_hil_doctor",
            "summary": summary,
        },
        status,
        prominent=True,
    )
    return {
        **report,
        "config_path": config.config_path,
        "installation": _doctor_installation_report(),
        **({"path_trust": _doctor_path_trust_report(config)} if os.name == "nt" else {}),
        "mcp": _doctor_mcp_report(),
        "target": {"name": config.target.name, "controller": config.target.controller},
        "debuggers": {
            name: {
                "type": entry.type,
                "probe_id": entry.probe_id,
                "bound": name == config.debugger_id,
                "permissions": asdict(entry.permissions),
                **({"check": checks[name]} if name in checks else {}),
                **({"target_support": target_support[name]} if name in target_support else {}),
            }
            for name, entry in config.debuggers.items()
        },
        "com_ports": {port_id: {"device": port.device, "baudrate": port.baudrate, "encoding": port.encoding, "permissions": asdict(port.permissions)} for port_id, port in config.com_ports.items()},
        "can_buses": {bus_id: {"adapter": bus.adapter, "channel": bus.channel, "bitrate": bus.bitrate, "fd": bus.fd, "permissions": asdict(bus.permissions)} for bus_id, bus in config.can_buses.items()},
        "debugger": debugger_info,
    }


def _doctor_path_trust_report(config: AgenticHILConfig) -> JsonObject:
    """What the Windows path trust check saw on this configuration's own paths
    and did not refuse.

    A finding that does not refuse still has to be visible. Otherwise a
    restricted environment whose ACLs cannot be read looks exactly like a healthy
    one, and a tolerated grant is a decision nobody was told about. Reported
    rather than refused, like the editable-installation report above.
    """
    from agentic_hil.windows_principals import describe_principals, principal_label

    mode = windows_path_trust()
    findings: list[JsonObject] = []
    for field, path in (("user_config", Path(config.config_path).parent), ("state_root", Path(config.state_root))):
        try:
            inspected = inspect_windows_path_trust(path)
        except OSError as error:
            inspected = [{"finding": "acl_unreadable", "scope": "object", "path": str(path), "backend_error": str(error)}]
        findings.extend({"field": field, **finding} for finding in inspected)
    report: JsonObject = {"mode": mode, "ok": not findings, "findings": findings}
    if not findings:
        report["summary"] = f"Every path this configuration names passes the Windows trust check under windows_path_trust: {mode}."
        return report
    sids = [str(sid) for finding in findings for sid in finding.get("sids", []) if sid]
    principals = describe_principals(sids)
    holders = ", ".join(principal_label(entry) for entry in principals)
    unreadable = sum(1 for finding in findings if finding["finding"] == "acl_unreadable")
    parts = [f"windows_path_trust: {mode} accepted {len(findings)} finding(s) that windows_path_trust: strict would refuse"]
    if holders:
        parts.append(f"rights are held by {holders}")
    if unreadable:
        parts.append(f"{unreadable} ACL(s) could not be read, which is unknown rather than unsafe")
    report["summary"] = ". ".join(parts) + ". Nothing was changed; see the platform-paths reference for what each finding means."
    if principals:
        report["principals"] = principals
    return report


def _doctor_installation_report() -> JsonObject:
    """Where the code behind the hardware gate is actually loaded from.

    An editable installation copies nothing: it points at a source tree, so the
    code enforcing the policy can change without anyone reinstalling, and the
    operator has no version to hold on to. That is normal while developing this
    package and wrong for a bench, so it is reported rather than refused.
    """
    package_root = str(Path(__file__).resolve().parent)
    report: JsonObject = {"version": __version__, "package_path": package_root, "editable": False}
    try:
        from importlib.metadata import distribution

        origin = distribution("agentic-hil").read_text("direct_url.json")
    except Exception:
        return report
    if not origin:
        return report
    with suppress(ValueError):
        report["editable"] = bool(json.loads(origin).get("dir_info", {}).get("editable"))
    if report["editable"]:
        report["summary"] = (
            f"Agentic HIL runs editable from {package_root}. The code behind the hardware gate is whatever "
            "that directory holds at the time, so reinstall it from a release, a tag or a copied directory "
            "before anyone relies on this bench."
        )
    return report


def _doctor_mcp_report() -> JsonObject:
    """What to register as the MCP server, or why nothing may be registered yet.

    Reporting the bare name would name the one form registration refuses. `PATH`
    decides it afresh every session, so the program behind the hardware gate
    could change without anyone editing the registration. An operator who copies
    what doctor prints must get something registration accepts.
    """
    report: JsonObject = {"transport": "stdio", "args": ["mcp-stdio"]}
    try:
        command = mcp_server_command()
    except (ConfigError, OSError) as error:
        summary = error.summary if isinstance(error, ConfigError) else str(error)
        return {
            **report,
            "command": None,
            "persistent": False,
            "summary": f"No trusted persistent executable to register yet: {summary}",
        }
    return {
        **report,
        "command": command,
        "persistent": True,
        "summary": "Register this exact path with 'agentic-hil setup --agent <agent>'; a transient runner or a bare PATH name is not a registration.",
    }


def debugger_probes() -> JsonObject:
    """Enumerate connected probes for every configured debugger that may probe.

    Probe discovery is how an operator finds the serial numbers a multi-board
    config needs, so it has to work in exactly the multi-probe project where no
    single debugger is bound. Each backend enumerates all attached probes, so
    binding one entry at a time is only about which toolchain to invoke."""
    config = load_authoritative_config(Path.cwd())
    if config.debugger is not None:
        service = AgenticHILToolService(config)
        try:
            return service.call("debugger_probes_list")
        finally:
            service.close()
    if not config.debuggers:
        # Nothing is switched off here — nothing exists. Answer exactly as the
        # MCP surface does for the same config, so the two do not send an
        # operator hunting for a flag that has no entry to live on.
        return unbound_debugger_error("debugger_probes_list", config)
    granted = [name for name, entry in config.debuggers.items() if config.probe_allowed(entry)]
    if not granted:
        return {
            "ok": False,
            "tool": "debugger_probes_list",
            "error_type": "permission_denied",
            "summary": "No configured debugger grants allow_probe, so probe discovery is disabled by the authoritative config.",
            "configured_debuggers": sorted(config.debuggers),
        }
    results: JsonObject = {}
    for name in granted:
        service = AgenticHILToolService(bind_debugger(config, name))
        try:
            results[name] = service.call("debugger_probes_list")
        finally:
            service.close()
    failed = sorted(name for name, result in results.items() if not overall_success(result))
    aggregate: JsonObject = {
        # `all`, not `any`: one probe answering must not report the run as
        # healthy while another failed, and the CLI exit code is derived from
        # this verdict alone.
        "ok": not failed,
        "tool": "debugger_probes_list",
        "debuggers": results,
        "summary": (
            f"Probe discovery ran for {len(results)} configured debugger(s)."
            if not failed
            else f"Probe discovery failed for: {', '.join(failed)}."
        ),
    }
    # A containment marker on any probe has to reach the top level, or
    # overall_success() reads clean over a nested quarantine.
    for marker, unsafe in (("cleanup_required", True), ("quarantined", True), ("audit_ok", False)):
        if any(result.get(marker) is unsafe for result in results.values()):
            aggregate[marker] = unsafe
    unsafe_effect = next((result.get("side_effect_status") for result in results.values() if result.get("side_effect_status") in {"unknown", "partial"}), None)
    if unsafe_effect is not None:
        aggregate["side_effect_status"] = unsafe_effect
    quarantine_id = next((result.get("quarantine_id") for result in results.values() if result.get("quarantine_id")), None)
    if quarantine_id is not None:
        aggregate["quarantine_id"] = quarantine_id
    return aggregate


def load_cli_authoritative_config(config_path: str | None = None) -> AgenticHILConfig:
    workspace = Path.cwd().resolve()
    expected_path = Path(os.environ.get(CONFIG_ENV) or project_config_path(workspace)).expanduser().resolve()
    validate_legacy_config_selector(config_path, workspace, expected_path)
    return load_authoritative_config(workspace)


def run_mcp_stdio(config_path: str | None = None) -> int:
    """Serve MCP for the current workspace, configured or not.

    A missing configuration used to end the server before it started, which left
    an agent with `config_file_not_found` and no way forward that did not involve
    a person at a terminal. It now starts bound to the workspace instead, with
    every tool refusing except the one that generates a configuration once.

    Only an absent file takes that route. A configuration that exists and does
    not load — invalid, or in a location the trust check rejects — is still a
    hard stop: reading "broken" as "absent" would let a damaged file be replaced
    by a generated one, silently discarding the policy an operator wrote."""
    try:
        config = load_cli_authoritative_config(config_path)
    except ConfigError as error:
        if error.error_type != "config_file_not_found":
            raise
        return run_stdio_server(None, tools=UnprovisionedToolService(Path.cwd().resolve()))
    return run_stdio_server(config)


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
            # The superseded skill is snapshotted for rollback but never locked:
            # its sidecar lock would keep the directory alive after the file goes.
            snapshots = _capture_file_snapshots([*mutation_paths, *legacy_skill_paths(target_path)])
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
    legacy_note: JsonObject = {}
    removed_legacy = remove_legacy_skills(target_path)
    if removed_legacy:
        legacy_note = {"legacy_skills_removed": removed_legacy}
    existing_text = secure_optional_read_text(target_path)
    if existing_text is not None:
        if existing_text == source_text:
            registration = register_skill(resolved_agent, str(target_path), source_version, requested_agent)
            return {"ok": True, **legacy_note, "summary": f"Agentic HIL {agent_name} skill is already installed.", "agent": agent_id, "requested_agent": requested_agent, "skill": SKILL_NAME, "source_path": str(source_path), "target_path": str(target_path), "version": source_version, "installed": False, "updated": False, "registered": registration.get("ok") is True if registration else False, "registration": registration}
        existing_version = skill_version(existing_text)
        managed_skill = is_agentic_hil_setup_skill(existing_text)
        if not managed_skill:
            return {"ok": False, "error_type": "skill_conflict", "summary": "Target skill file contains unmanaged content and was left untouched; --force never replaces foreign skills.", "next_step": CONFLICT_NEXT_STEP, "agent": agent_id, "requested_agent": requested_agent, "skill": SKILL_NAME, "source_path": str(source_path), "target_path": str(target_path), "existing_version": existing_version, "version": source_version}
        if existing_version != source_version:
            secure_atomic_write_text(target_path, source_text)
            registration = register_skill(resolved_agent, str(target_path), source_version, requested_agent)
            return {"ok": True, **legacy_note, "summary": f"Agentic HIL {agent_name} skill updated to match the current CLI package.", "agent": agent_id, "requested_agent": requested_agent, "skill": SKILL_NAME, "source_path": str(source_path), "target_path": str(target_path), "previous_version": existing_version, "version": source_version, "installed": False, "updated": True, "registered": registration.get("ok") is True if registration else False, "registration": registration}
        if not force:
            return {"ok": False, "error_type": "skill_exists", "summary": "Managed Agentic HIL skill differs from the packaged copy. Use --force to repair it.", "agent": agent_id, "requested_agent": requested_agent, "skill": SKILL_NAME, "source_path": str(source_path), "target_path": str(target_path), "existing_version": existing_version, "version": source_version}
    secure_atomic_write_text(target_path, source_text)
    registration = register_skill(resolved_agent, str(target_path), source_version, requested_agent)
    return {"ok": True, **legacy_note, "summary": f"Agentic HIL {agent_name} skill installed.", "agent": agent_id, "requested_agent": requested_agent, "skill": SKILL_NAME, "source_path": str(source_path), "target_path": str(target_path), "version": source_version, "installed": True, "updated": False, "registered": registration.get("ok") is True if registration else False, "registration": registration}


def bundled_skill_path() -> Path:
    return resources.files("agentic_hil").joinpath("skills", SKILL_NAME, SKILL_FILE)


def skill_version(text: str) -> str | None:
    match = re.search(r'^  agentic_hil_version: "([^"]+)"\r?$', text, re.MULTILINE)
    return match.group(1) if match else None


def is_agentic_hil_setup_skill(text: str, name: str = SKILL_NAME) -> bool:
    # A Windows checkout converts the packaged skill to CRLF, and refusing to
    # recognise it would report this project's own skill as somebody else's.
    return re.search(rf"^name: {re.escape(name)}\r?$", text, re.MULTILINE) is not None and re.search(r"^  origin: Agentic HIL\r?$", text, re.MULTILINE) is not None


def legacy_skill_paths(target_path: Path) -> list[Path]:
    """Where an earlier release of this skill would sit beside the new one.

    Only paths that are there now. Snapshotting or probing an absent one would
    create its directory chain and make whatever occupies that name a
    precondition of installing under the current one.
    """
    if not (target_path.name == SKILL_FILE and target_path.parent.name == SKILL_NAME):
        return []
    candidates = (target_path.parent.parent / name / SKILL_FILE for name in LEGACY_SKILL_NAMES)
    return [path for path in candidates if path.is_file()]


def remove_legacy_skills(target_path: Path) -> list[str]:
    """Delete skills this project installed under an earlier name.

    Only a file carrying that name's managed frontmatter is removed, so a
    directory the user owns is never touched. The directory goes with it, along
    with the lock sidecar an earlier release left beside the skill.
    """
    removed = []
    for path in legacy_skill_paths(target_path):
        # Plain stat first: reading through the validated path would create the
        # directory chain, which would plant the very directory being removed.
        if not path.is_file():
            continue
        text = secure_optional_read_text(path)
        if text is None or not is_agentic_hil_setup_skill(text, path.parent.name):
            continue
        path.unlink()
        with suppress(OSError):
            # An earlier release locked this file and the sidecar outlives the
            # transaction, so without it the directory can never be empty.
            user_file_lock_path(path).unlink()
        with suppress(OSError):
            path.parent.rmdir()
        removed.append(str(path))
    return removed


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
- Read and follow this skill before acting on any firmware or hardware request: flashing, resetting, probing, debugging, UART or CAN traffic, firmware artifacts, and hardware test runs, as well as Agentic HIL setup, configuration, and MCP registration.
- Do not invoke a debugger, serial device, or CAN adapter directly when an Agentic HIL tool covers the request.
- If this version differs from `agentic-hil --version`, run `agentic-hil skill-install --agent {requested_agent}`.
{AGENTIC_HIL_REGISTRATION_END}"""


def upsert_marked_block(file_path: Path, block: str) -> JsonObject:
    existing = secure_optional_read_text(file_path) or ""
    pattern = re.compile(rf"{re.escape(AGENTIC_HIL_REGISTRATION_START)}[\s\S]*?{re.escape(AGENTIC_HIL_REGISTRATION_END)}")
    trimmed = existing.rstrip()
    separator = "\n\n" if trimmed else ""
    # A function replacement, because the block carries the skill's absolute
    # path and re.sub reads backslash escapes in a replacement string: on
    # Windows a second write of a block naming C:\Users\... died on "bad escape
    # \U". Only the second write takes this branch, so skill-install followed by
    # setup hit it and a plain setup did not.
    next_text = pattern.sub(lambda _: block, existing) if pattern.search(existing) else f"{trimmed}{separator}{block}\n"
    if next_text != existing:
        secure_atomic_write_text(file_path, next_text)
        return {"updated": True}
    return {"updated": False}


def print_json(value: JsonObject) -> None:
    sys.stdout.write(json.dumps(redact_sensitive(value), indent=2) + "\n")
