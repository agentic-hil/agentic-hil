from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from contextlib import ExitStack, suppress
from dataclasses import asdict, dataclass
from importlib import resources
from pathlib import Path, PurePath

import yaml

from agentic_hil import __version__, upgrade
from agentic_hil.adopt import project_config_adopt_hardware
from agentic_hil.bench import BenchMutex, DeviceBusyError
from agentic_hil.bootstrap import (
    BOOTSTRAP_BACKEND,
    DEFAULT_PROJECT_PROFILE,
    apply_discovery_to_template,
    enumerate_attached_probes,
    load_project_profile,
)
from agentic_hil.comports import list_available_com_ports, port_identity_fields
from agentic_hil.comstdio import run_com_stdio
from agentic_hil.config import (
    CONFIG_ENV,
    DEFAULT_CONFIG_TEMPLATE,
    OPENOCD_SCRIPT_SEARCH_NAME,
    PROJECT_CONFIG_FILENAME,
    ConfigError,
    absolute_without_symlinks,
    atomic_write_text,
    authoritative_config_target,
    bind_debugger,
    config_schema_text,
    debugger_drives_hardware,
    ensure_safe_state_root,
    is_path_within_frozen,
    load_authoritative_config,
    load_config,
    openocd_script_kind,
    permission_summary,
    project_config_directory,
    project_config_path,
    safe_directory,
    secure_atomic_write_bytes,
    secure_atomic_write_text,
    secure_optional_read_bytes,
    secure_optional_read_text,
    secure_remove_file,
    secure_user_file_lock,
    tighten_owned_writable_ancestors,
    tool_owned_user_roots,
    trusted_persistent_executable,
    user_file_lock_path,
    user_state_root,
)
from agentic_hil.configreload import NOT_RELOADED_SECTIONS, PROJECT_CONFIG_RELOAD, RELOADED_SECTIONS, reload_description
from agentic_hil.configstate import config_status, with_config_status
from agentic_hil.configwrite import (
    ACTOR_HUMAN,
    NOT_STARTED,
    PERMISSION_COMMAND_VALUES,
    authoritative_write_target,
    load_config_document,
    permission_surface,
    set_permission,
)
from agentic_hil.coordination import CoordinationError, HardwareCoordinator, nothing_standing_result
from agentic_hil.devices import config_devices
from agentic_hil.knowledge import (
    CONFIG_GRANT_COMMAND,
    CONFIG_REOPEN_COMMAND,
    CONFIG_REVOKE_COMMAND,
    RUNNING_SERVER_COMPARISON,
    remediation_fields,
)
from agentic_hil.reactorrun import run_plan, start_plan_detached
from agentic_hil.redact import redact_sensitive
from agentic_hil.report import overall_success
from agentic_hil.runlifecycle import request_run_stop, run_status
from agentic_hil.stdio import run_stdio_server
from agentic_hil.test_reactor import DEFAULT_TEST_CONFIG_PATH
from agentic_hil.tools import (
    AgenticHILToolService,
    UnprovisionedToolService,
    discover_for_generation,
    narrowed_permissions,
    unbound_debugger_error,
)
from agentic_hil.types import AgenticHILConfig, DebuggerConfig, JsonObject
from agentic_hil.upgrade import CLI_UPGRADE_TOOL, missing_configured_extras, replace_installation

SKILL_NAME = "agentic-hil"
# What `agentic-hil init`'s read of the board is called in the audit record it
# leaves and in any incident it files. Deliberately not `project_config_create`,
# though the two write the same file from the same read: a record that could not
# say which of them touched the bench is the wrong record to hand an operator.
CLI_INIT = "cli_init"
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
# What `agentic-hil uninstall` does, and the whole of the argument for the two
# flags it does not offer. It is the parser's own `description`, so it is what
# `agentic-hil uninstall --help` prints, and it is written once here rather than
# split between the help text and a comment nobody reading the help can see.
UNINSTALL_DESCRIPTION = """Take back the user-wide half of this installation, for every agent it set up or for
the ones --agent names: the agent's skill file, its user-level MCP registration,
and, for Claude Code, the write-refusal rules `init --agent` added. Each half is
taken back only where it is recognisably this tool's own, by the same checks that
refuse to overwrite an operator's file. Everything those checks do not claim is
named on the result and left exactly where it was found.

Two trees are left standing on purpose, and no flag purges them.

A state root is the evidence. It holds every hardware action this bench
performed and any incident record saying a board is in an unknown physical
state until an operator signs for its recovery, and removing a package does not
put that board back. Agentic HIL quarantines a whole bench when a single audit
record cannot be written, so a flag that deleted all of them would contradict
the rule this tool enforces at every hardware call.

A project configuration is operator policy: which permissions are open on which
board. An agent may narrow a permission and may never widen one, which is why
`grant` lives at the operator's shell. A purge followed by a reinstall would put
every permission back to the template's, and that is the one move that turns
that ratchet the wrong way, out of a command an agent can be asked to run.

Both are named on the result, with their paths. Removing them is `rm -rf` at
your own shell, after reading what is in them.

The package is not removed here either, and cannot be: a process cannot delete
the files it is executing out of. The result ends on the removal line for the
manager that owns this installation. Run that line last."""
# Why each of those trees stays, on the result itself, for a reader who never
# opened the help.
_KEPT_TOOL_TREE = (
    "Agentic HIL creates this tree for itself, and what is in it outlives an installation: the audit trail of "
    "every hardware action, any incident still standing over a board that removing a package does not put back, "
    "the project configurations, and the machine-wide device locks. Read it before deleting it, and delete it yourself."
)
_KEPT_CONFIGURATION = (
    "A project configuration is operator policy, and its permissions only ever narrow. Deleting it would "
    "put every permission back to the template's on the next install. Delete it yourself if that is what you want."
)
_LEFT_FOREIGN_ENTRY = (
    "Agentic HIL did not write this, so it stays. Once the package is gone it will name a command that is "
    "gone with it, and whether to edit the file is the operator's decision."
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
    """The exact prior state of one file, for transactional rollback.

    Bytes, and existence tracked as its own fact (``raw is None`` ⟺ the path did
    not exist; ``raw == b""`` is an empty file that did). A rollback has to put
    back what was there, and a file that is not UTF-8 — a UTF-16 config, a
    truncated write, something binary dropped on the path — is still something a
    forced regeneration must restore rather than delete. Snapshotting it as
    decoded text mapped those bytes to ``None``, which reads as "absent", and a
    failed final validation then removed the original instead of restoring it."""

    path: Path
    raw: bytes | None

    @property
    def existed(self) -> bool:
        """Whether the file was there when the snapshot was taken."""
        return self.raw is not None

    @property
    def content(self) -> str | None:
        """The snapshot decoded as UTF-8, or ``None`` when it was absent or its
        bytes are not text. For callers that reason about text — the skill-install
        ownership check — never for restoring, which is byte-exact."""
        if self.raw is None:
            return None
        try:
            return self.raw.decode("utf-8")
        except UnicodeDecodeError:
            return None


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

    init_parser = subparsers.add_parser("init", help="project half: write this workspace's authoritative config with every permission granted but the two flashing is interlocked against, and verify it with doctor. A config that is already there is kept, unchanged, and only the steps that do not touch it run")
    init_parser.add_argument("--config", default=None, help=argparse.SUPPRESS)
    init_parser.add_argument("--agent", default=None, help="also ask this agent to refuse its own write tools on the config and the state root; on a config that is already there, adding or refreshing those rules is the whole of what this command does")
    init_parser.add_argument("--force", action="store_true")

    adopt_parser = subparsers.add_parser(
        "adopt-hardware",
        help="carry an attached board's identity into an existing config: probe id, backend executable, controller, COM device. Fills only what is unset; never touches permissions",
    )
    adopt_parser.add_argument("--debugger", default=None, help="which configured debugger entry receives the values; only needed with more than one")
    adopt_parser.add_argument("--com-port", default=None, help="which com_ports entry receives the discovered device; created with every permission false if it does not exist")
    adopt_parser.add_argument("--probe-id", default=None, help="which attached probe this is about; only needed when more than one is attached")
    adopt_parser.add_argument("--dry-run", action="store_true", help="report what would be filled in and write nothing")

    # The gate on the one-way street. Deliberately here and in no
    # tool list: an agent narrows a permission over MCP and never widens one, and
    # opening one is the operator's, at the operator's shell — the same shell
    # that already holds `agentic-hil init --force`, which rewrites every
    # permission in the file at once. These two move one named permission and
    # leave the rest of the file alone.
    grant_parser = subparsers.add_parser(
        "grant",
        help="open one named permission in the config that is already there, leaving everything else in it exactly as it is; never reachable over MCP",
    )
    grant_parser.add_argument(
        "keys",
        nargs="+",
        metavar="KEY",
        help="e.g. can_buses.dut.allow_write, or the long form can_buses.dut.permissions.allow_write; several may be named and are applied together or not at all",
    )

    revoke_parser = subparsers.add_parser(
        "revoke",
        help="close one named permission in the config that is already there; the counterpart of grant, so this command line is not a one-way street either",
    )
    revoke_parser.add_argument(
        "keys",
        nargs="+",
        metavar="KEY",
        help="e.g. debuggers.dut.allow_mass_erase; several may be named and are applied together or not at all",
    )

    doctor_parser = subparsers.add_parser("doctor", help="validate config and check debugger availability")
    doctor_parser.add_argument("--config", default=None, help=argparse.SUPPRESS)

    config_reload_parser = subparsers.add_parser(
        "config-reload",
        help="check what a running MCP server's project_config_reload_description would take from this file: which device description it adopts and what still needs a restart",
    )
    config_reload_parser.add_argument("--config", default=None, help=argparse.SUPPRESS)

    subparsers.add_parser("debugger-probes", help="list connected probe IDs for the configured debugger backend; on a project that has no configuration yet, through the same read-only discovery setup's bootstrap runs, labelled source: bootstrap")

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
    reactor_parser.add_argument(
        "--detach",
        action="store_true",
        help="run the plan in its own process and return at once with a run handle and the report path, instead of holding this terminal until the report exists",
    )
    # The handle a detached worker was started under. Not an operator's
    # argument: the start command mints the handle, registers nothing itself and
    # hands it to the worker it spawned, so a value supplied by hand here would
    # be a second process claiming a run it did not start.
    reactor_parser.add_argument("--run-handle", default=None, help=argparse.SUPPRESS)

    reactor_status_parser = subparsers.add_parser("test-reactor-status", help="say what a test run handle is doing: running (which step), finished, stopped, or its worker gone")
    reactor_status_parser.add_argument("--run", default=None, help="the handle the run was started under; without it, list the runs this bench still has records of")

    reactor_stop_parser = subparsers.add_parser("test-reactor-stop", help="ask a test run to end after the step it is in, close its devices and write its report")
    reactor_stop_parser.add_argument("--run", required=True, help="the handle the run was started under")

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

    upgrade_parser = subparsers.add_parser("upgrade", help="upgrade this Agentic HIL installation and refresh the agent skills and MCP registrations it wrote")
    upgrade_parser.add_argument("--agent", action="append", default=[], help="refresh only this agent, instead of every agent this installation had already set up; repeat for multiple agents. An agent that has neither a skill nor a registration is never installed for.")

    uninstall_parser = subparsers.add_parser(
        "uninstall",
        help="the user-wide half in reverse: take back the agent skills, MCP registrations and write-refusal rules this installation wrote, then name the line that removes the package. Project configurations and state roots are left standing, and there is no flag that purges them",
        description=UNINSTALL_DESCRIPTION,
        # The description is five paragraphs and the argument for the flags this
        # command refuses to offer; reflowed into one block it is unreadable, and
        # a reader who cannot get through it is a reader who deletes the state
        # root by hand instead.
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    uninstall_parser.add_argument("--agent", action="append", default=[], help="take back only this agent's half, instead of every agent this installation set up; repeat for multiple agents. An agent that has nothing installed is reported and left alone.")

    return parser


def dispatch(args: argparse.Namespace) -> JsonObject | int | None:
    if args.command == "init":
        return init_project(args.config, args.agent, args.force)
    if args.command == "adopt-hardware":
        return adopt_hardware(debugger_id=args.debugger, com_port_id=args.com_port, probe_id=args.probe_id, dry_run=args.dry_run)
    if args.command in PERMISSION_COMMAND_VALUES:
        return change_permission(args.command, args.keys)
    if args.command == "doctor":
        return doctor(args.config)
    if args.command == "config-reload":
        return config_reload(args.config)
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
        if args.detach:
            return start_detached_test_reactor(args.test_config, wait_s=args.wait_s)
        return run_test_reactor(args.test_config, wait_s=args.wait_s, run_handle=args.run_handle)
    if args.command == "test-reactor-status":
        return run_status(load_cli_authoritative_config(None), args.run)
    if args.command == "test-reactor-stop":
        return request_run_stop(load_cli_authoritative_config(None), args.run)
    if args.command in {"lease-status", "recover"}:
        config = load_cli_authoritative_config(None)
        coordinator = HardwareCoordinator(config, "operator-cli")
        status = coordinator.status()
        if args.command == "lease-status":
            return status
        # The signature is owed for one thing: an evidence chain that cannot be
        # rebuilt. Everything else ended with the call that raised it, or is an
        # incident waiting for the next hardware call to answer it, and neither
        # is a thing an operator can sign for. So somebody who typed this over a
        # bench with nothing standing is told exactly that, rather than handed an
        # error about a bench that is fine.
        if not status.get("incident_stands"):
            return nothing_standing_result(status)
        return coordinator.recover(safe_state_confirmed=args.confirm_safe_state, quarantine_id=args.quarantine_id, accept_config_change=args.accept_config_change)
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
    if args.command == "uninstall":
        return uninstall_agent_integration(args.agent)
    return {"ok": False, "error_type": "unknown_command", "summary": f"unknown command: {args.command}"}


def upgrade_installation(agents: list[str] | None = None) -> JsonObject:
    """Upgrade the package owning this process, then run maintenance from new code.

    The manager half is `upgrade.replace_installation`, shared verbatim with the
    `server_upgrade` MCP tool. What is this command's alone is
    what follows a swap that actually happened: a shell can refresh the agent
    skills out of the new package, and an MCP server cannot — it is still the old
    code until somebody restarts it.
    """
    requested_agents = agents or []
    invalid = [agent for agent in requested_agents if resolve_skill_agent(agent) is None]
    if invalid:
        return {"ok": False, "error_type": "unsupported_agent", "summary": "Agentic HIL does not know one or more requested agents.", "agents": invalid, "allowed_agents": supported_skill_agents()}

    result = replace_installation(tool=CLI_UPGRADE_TOOL)
    if not result.get("upgraded_on_disk"):
        # Nothing was replaced, so refreshing anything out of it would be work
        # with no effect, and reporting it would put a list of things that
        # happened under a result whose whole content is that nothing did. This
        # covers the already-current answer as well, which now comes back
        # without a package manager having run at all.
        return result

    previous_version = str(result["previous_version"])
    current_version = str(result["version"])
    refreshed = _refresh_agent_integrations(requested_agents)
    rewritten = [entry["agent"] for entry in refreshed if entry["registration_rewritten"]]
    failed = [entry["agent"] for entry in refreshed if not entry["ok"]]
    # One more way an installation stops matching its own configuration, on the
    # path the release notes actually send an operator down: an upgrade that
    # came back without the extra this bench's configuration needs. Best effort
    # by design, since `upgrade` has to work on a machine that has no project
    # configured yet, so a configuration that will not load is a reason to say
    # nothing here rather than to fail the upgrade that just succeeded.
    extras_warning = None
    with suppress(ConfigError, OSError):
        extras_warning = missing_configured_extras(load_cli_authoritative_config(None))
    summary = f"Agentic HIL upgraded from {previous_version} to {current_version}; restart agent hosts to load the new MCP server."
    if rewritten:
        # An agent host reads its MCP registration at startup, so the agent
        # whose entry moved is the one that has to be restarted before it uses
        # the launcher this upgrade resolved.
        hosts = _named_agents(rewritten)
        summary += f" The MCP registration was rewritten for {hosts}, so restart {hosts} to load it."
    if failed:
        # Not a failed upgrade. The package moved; what did not is a file this
        # command maintains for somebody else's program, and each entry carries
        # the one line that finishes it by hand.
        summary += f" The agent integration could not be refreshed for {_named_agents(failed)}; `refreshed` names the command that does it."
    return {
        **result,
        **({"extras_warning": extras_warning} if extras_warning is not None else {}),
        "summary": summary,
        "refreshed": refreshed,
    }


def _named_agents(agent_ids: list[str]) -> str:
    display = {agent.id: agent.display_name for agent in skill_agents()}
    return ", ".join(display.get(agent_id, agent_id) for agent_id in agent_ids)


def _installed_agent_integration(agent: SkillAgent) -> tuple[bool, bool]:
    """What this installation has already written for one agent: skill, registration.

    Read only, and forgiving of everything: a home directory that refuses the
    ancestor trust check, a configuration file that is no longer JSON, a path
    component that is not a directory any more. None of those is a reason to
    fail an upgrade that has already succeeded, and each of them answers the
    question the same way, which is that there is nothing here to refresh.
    """
    skill = False
    with suppress(ConfigError, OSError, ValueError):
        skill = _path_entry_exists(_agent_skill_target(agent))
    registration = False
    with suppress(ConfigError, OSError, ValueError):
        path = _agent_mcp_config_path(agent.id)
        if agent.id == "codex":
            registration = AGENTIC_HIL_MCP_START in (secure_optional_read_text(path) or "")
        else:
            data = _load_json_object(path) or {}
            servers = data.get("mcpServers" if agent.id == "claude-code" else "mcp")
            registration = isinstance(servers, dict) and "agentic-hil" in servers
    return skill, registration


def _refresh_agent_integrations(requested_agents: list[str]) -> list[JsonObject]:
    """Rewrite, out of the new package, the two things the old one had written.

    The skill file carries the release's own text, and the MCP registration
    names the launcher this installation resolves, which an upgrade is entitled
    to move. Neither is inside the package, so replacing the package leaves both
    behind, describing a release that is no longer installed.

    Only for an agent that already has one of the two. An upgrade that
    registered an agent nobody had set up would be adding an MCP server to a
    machine on the strength of a maintenance command, and `--agent` narrows this
    set rather than widening it: naming an agent that has nothing still installs
    nothing, and says so.

    Through a subprocess and never in process. This interpreter imported the
    release that was just replaced and goes on executing it, so a refresh run
    here would write the old package's skill text and resolve the old launcher,
    which is the exact opposite of the point.
    """
    wanted = {resolved.id for name in requested_agents if (resolved := resolve_skill_agent(name)) is not None}
    outcomes: list[JsonObject] = []
    with tempfile.TemporaryDirectory(prefix="agentic-hil-upgrade-") as maintenance_cwd:
        for agent in skill_agents():
            if wanted and agent.id not in wanted:
                continue
            skill, registration = _installed_agent_integration(agent)
            if not (skill or registration):
                outcomes.append(
                    {
                        "agent": agent.id,
                        "ok": True,
                        "skill": False,
                        "registration": False,
                        "registration_rewritten": False,
                        "summary": f"Nothing to refresh for {agent.display_name}: this installation had written neither its skill nor its MCP registration.",
                    }
                )
                continue
            outcomes.append(_refresh_one_agent(agent, maintenance_cwd))
    return outcomes


def _refresh_one_agent(agent: SkillAgent, maintenance_cwd: str) -> JsonObject:
    """One `agent-install --force`, run out of the new package, reported per half.

    `--force` because that is the documented repair route for a managed skill or
    MCP entry: it rewrites what this program wrote and still refuses an entry an
    operator wrote, which is the distinction that has to survive an upgrade.

    A failure here is reported and never raised. The package moved, which is
    what `agentic-hil upgrade` promises; a skill file that could not be written
    is one command away and that command is on the result.
    """
    # Through the module rather than a name imported above: `upgrade` is the one
    # place a test replaces the subprocess runner, and a second binding here
    # would be the copy that kept running the real thing.
    command = [sys.executable, "-m", "agentic_hil", "agent-install", "--agent", agent.id, "--force"]
    outcome: JsonObject = {"agent": agent.id, "command": command}
    try:
        completed = upgrade._run_upgrade_process(command, cwd=maintenance_cwd)
    except (OSError, subprocess.TimeoutExpired) as error:
        return {
            **outcome,
            "ok": False,
            "skill": False,
            "registration": False,
            "registration_rewritten": False,
            "exception_type": type(error).__name__,
            "detail": str(error),
            "summary": f"The agent integration for {agent.display_name} could not be refreshed; run `command` by hand to finish it.",
        }
    child = upgrade._process_result(completed)
    if completed.stdout.strip():
        with suppress(json.JSONDecodeError):
            child["result"] = json.loads(completed.stdout)
    reported = child.get("result")
    steps = reported.get("steps") if isinstance(reported, dict) else None
    steps = steps if isinstance(steps, dict) else {}
    mcp_step = steps.get("mcp_config")
    skill = completed.returncode == 0 and _step_succeeded(steps.get("skill_install"))
    registration = completed.returncode == 0 and _step_succeeded(mcp_step)
    ok = skill and registration
    return {
        **outcome,
        "ok": ok,
        "skill": skill,
        "registration": registration,
        # An entry that already named this launcher is in place and current, so
        # it counts as refreshed. It was not rewritten, and only a rewrite is
        # something an agent host has to be restarted to pick up.
        "registration_rewritten": registration and not (isinstance(mcp_step, dict) and mcp_step.get("skipped") is True),
        "install": child,
        "summary": (
            f"Agentic HIL's skill and MCP registration for {agent.display_name} were refreshed from the new release."
            if ok
            else f"The agent integration for {agent.display_name} was not fully refreshed; run `command` by hand to finish it."
        ),
    }


def _step_succeeded(step: object) -> bool:
    return isinstance(step, dict) and overall_success(step)


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


def _workspace_boundary() -> Path | None:
    """The project directory the user-wide half must stay out of, or None.

    The boundary is the working directory, and what it protects is a project: a
    repository is untrusted content, so the files that govern the agent may
    neither be written inside one nor launched out of one. The home directory is
    not such a project. Run from home itself, or from a directory above it (the
    filesystem root is the everyday one, and an operator typing the install
    one-liner is standing in one or the other), a boundary drawn at the working
    directory swallows the home directory whole: every user-level target and
    every uv- or pipx-installed launcher then reads as "inside the workspace",
    and a half whose entire output is user-level files has nothing left it may
    write. There is no workspace to keep out of there, so this reports none, and
    every directory that is not home or above it keeps the strict boundary. A
    host that cannot name a home directory keeps it too.
    """
    workspace = absolute_without_symlinks(Path.cwd())
    with suppress(RuntimeError):
        if is_path_within_frozen(absolute_without_symlinks(Path.home()), workspace):
            return None
    return workspace


def _external_user_path(path: Path, label: str) -> Path:
    absolute = absolute_without_symlinks(path)
    workspace = _workspace_boundary()
    if workspace is not None and is_path_within_frozen(absolute, workspace):
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
        snapshots.append(FileSnapshot(absolute, secure_optional_read_bytes(absolute)))
    return snapshots


def _restore_file_snapshots(snapshots: list[FileSnapshot]) -> list[JsonObject]:
    errors: list[JsonObject] = []
    for snapshot in reversed(snapshots):
        try:
            current = secure_optional_read_bytes(snapshot.path)
            if current == snapshot.raw:
                continue
            if snapshot.raw is None:
                secure_remove_file(snapshot.path)
            else:
                secure_atomic_write_bytes(snapshot.path, snapshot.raw)
        except BaseException as error:
            errors.append({"path": str(snapshot.path), "exception_type": type(error).__name__, "summary": str(error)})
    return errors


def _skipped_setup_step(summary: str) -> JsonObject:
    return {"ok": False, "skipped": True, "summary": summary}


def _smooth_permissions(targets: list[Path]) -> list[str]:
    """Silently tighten the current user's own group/other-writable components
    along these chains so the fail-closed trust validator accepts a launcher an
    installer wrote under a umask-002 / private-group home without the operator
    hand-fixing permissions. Only user-owned components are ever changed. POSIX
    only; on Windows this is a no-op."""
    actions: list[str] = []
    seen: set[str] = set()
    for target in targets:
        key = os.path.normcase(str(target))
        if key in seen:
            continue
        seen.add(key)
        actions.extend(tighten_owned_writable_ancestors(target))
    return actions


def _smooth_user_permissions() -> list[str]:
    """Smooth the one chain a fail-closed validator still walks: the launcher
    the MCP command will be registered as.

    ``trusted_persistent_executable`` is the last check that refuses a path for
    its POSIX mode, and since #143 that mode is the launcher file's own: its
    ancestors are no longer refused for who else may write them, so a umask-002
    home no longer has to be tightened for a registration to be possible.
    Smoothing was once wider still because configured paths were mode-checked
    too; that check was removed whole, and chmod-ing a tree
    nothing validates any more would be this policy's remnant mutating an
    operator's filesystem for no refusal it can prevent. The skill and the
    user-level MCP config are therefore no longer touched, and neither is
    anything project-side.
    """
    targets: list[Path] = []
    for candidate in _mcp_command_candidates():
        targets.append(Path(candidate))
        with suppress(OSError):
            targets.append(Path(os.path.realpath(candidate)))
    return _smooth_permissions(targets)


def _unsupported_agent(agent: str, summary: str) -> JsonObject:
    return {"ok": False, "error_type": "unsupported_agent", "summary": summary, "agent": normalize_agent(agent), "allowed_agents": supported_skill_agents()}


def _registration_restart(agent: SkillAgent, mcp_result: JsonObject) -> JsonObject:
    """Whether a registration was just written, and the sentence that says so.

    A host reads its MCP registrations once, when the session starts, so the
    session that writes one cannot see the tools it registered however long it
    waits for them. That news belongs on this result and nowhere else: the
    session that ran the command reads this and reports it in the same breath as
    `ok: true`, instead of both sides discovering it by waiting.

    It is claimed only for a registration that was actually written. A second
    `agent-install` that finds its own entry already there reports `skipped`, and
    a conflict or a failure wrote nothing at all; asking for a restart on any of
    those would be asking for one that reloads what is already loaded.
    """
    if mcp_result.get("ok") is not True or mcp_result.get("skipped") is True:
        return {"restart_required": False}
    return {
        "restart_required": True,
        "restart_notice": (
            f"The {agent.display_name} session that ran this must be restarted before the agentic-hil MCP tools "
            "appear; it read its registrations when it started and this one is newer. `agentic-hil doctor` at a "
            "shell works now and needs no restart."
        ),
    }


def _with_restart_notice(result: JsonObject) -> JsonObject:
    """Put the restart sentence where a person reading the terminal sees it.

    The CLI prints one JSON document and that is its human output, so a field
    nobody reads first is a field a person scrolls past. `summary` is the line
    both a person and an agent read, so the sentence goes there as well as into
    its own key."""
    notice = result.get("restart_notice")
    if isinstance(notice, str) and isinstance(result.get("summary"), str):
        result["summary"] = f"{result['summary']} {notice}"
    return result


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
    # Smooth the common umask-002 friction on the launcher chain before the
    # executable trust check reads it, tightening only the operator's own
    # writable dirs. Nothing else here is mode-checked, so nothing else is
    # touched.
    permission_changes = _smooth_user_permissions()
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
            **_registration_restart(resolved_agent, mcp_result),
            "command": command,
            "permission_changes": permission_changes,
            "rollback": {"attempted": not ok, "ok": not rollback_errors, "errors": rollback_errors},
            "steps": {"skill_install": skill_result, "mcp_config": mcp_result},
            # `--agent`, not a bare `init`: this half is where the split first
            # run hands back, and naming the agent is what carries its own
            # write-permission step into the project, on a config that is
            # already there as much as on one this creates.
            "next_step": f"Bind a project to it with `agentic-hil init --agent {resolved_agent.id}` from that project root. Naming the agent there settles that agent's own write permissions on the project's config and state root, whether the config is written by that run or already there. This half never has to run again for this user on this machine.",
        }
        if not result["ok"]:
            surviving = _skill_rollback_did_not_own(snapshots, _agent_skill_target(resolved_agent))
            if surviving is not None:
                result["left_behind"] = surviving
        return _with_restart_notice(result)


def uninstall_agent_integration(agents: list[str] | None = None) -> JsonObject:
    """Take back the user-wide half, and name the line that removes the package.

    The reverse of `install_agent`, and of the one step of `init --agent` that
    writes into a user-level file. What it takes back is only what is
    recognisably this installation's own, through the same checks that refuse to
    overwrite an operator's file rather than through a second set: the skill by
    `is_agentic_hil_setup_skill`, the JSON registrations by
    `_claude_mcp_entry_kind` and `_opencode_mcp_entry_kind`, the Codex ones by
    the managed markers, and the write refusals by `_tool_written_deny_tree` and
    the exact text this tool derives from a project's own paths. Whatever those
    do not claim is reported under `left_alone` and stays.

    The order inside one agent is the reverse of the order that wrote it, and it
    is load-bearing rather than tidy. The registration is what lets a host launch
    the server; the deny rules are what stop that host editing the configuration
    the server reads. Taking the rules back first would open a window with the
    lock already off and the door still there.

    Nothing is rolled back, and this half needs no transaction. Every step here
    removes something or does not, so a run that stops part way leaves no file
    half-written and is finished by running it again. `install_agent` rolls back
    because a half-installed integration is worse than none; a half-removed one
    is just less removed.

    It writes nothing project-side and needs no configuration, so it runs from
    anywhere `agent-install` runs, the home directory and the filesystem root
    included: every path it touches goes through the same `_external_user_path`
    that collapses the workspace boundary there (#235).

    The package itself is last and is not this command's to remove. A process
    cannot delete the files it is executing out of, so what comes back is the
    line for the operator's shell, from the manager that owns the installation.
    """
    requested = agents or []
    invalid = [agent for agent in requested if resolve_skill_agent(agent) is None]
    if invalid:
        return {"ok": False, "error_type": "unsupported_agent", "summary": "Agentic HIL does not know one or more requested agents.", "agents": invalid, "allowed_agents": supported_skill_agents()}

    wanted = {resolved.id for name in requested if (resolved := resolve_skill_agent(name)) is not None}
    reports = [_uninstall_one_agent(agent) for agent in skill_agents() if not wanted or agent.id in wanted]
    removed = [item for report in reports for item in report["removed"]]
    left_alone = [item for report in reports for item in report["left_alone"]]
    kept = _uninstall_kept_trees()
    failed = [report["agent"] for report in reports if not report["ok"]]

    command = upgrade.removal_command()
    package_directory = upgrade.installed_package_directory()
    summary = f"Agentic HIL's user-wide half was taken back for {_named_agents([report['agent'] for report in reports])}: {len(removed)} item(s) removed."
    if left_alone:
        summary += f" {len(left_alone)} item(s) this installation did not write were left where they were; `left_alone` names each one."
    if failed:
        summary += f" It could not be finished for {_named_agents(failed)}; that agent's step says why."
    summary += f" The package is still installed and this command cannot remove it: run `{command}`."
    return {
        "ok": not failed,
        "tool": "agentic_hil_uninstall",
        "scope": "user",
        "summary": summary,
        "agents": reports,
        "removed": removed,
        "left_alone": left_alone,
        "kept": kept,
        "package_removal": {"manager": upgrade.owning_manager(), "command": command, **({"package_directory": package_directory} if package_directory else {})},
        "next_step": (
            f"Run `{command}` at your shell; nothing running out of the installation can remove it. "
            + (
                f"If `import agentic_hil` still succeeds after that, a `__pycache__` left under {package_directory} is why; "
                f"remove only `{package_directory}/__pycache__`, never the rest of {package_directory}. "
                f"Python still imports the now-empty `{package_directory}` itself as a namespace package after that; "
                f"finish with a non-recursive empty-directory removal such as `rmdir {package_directory}`, which "
                f"succeeds only once nothing else is left there. "
                if package_directory
                else ""
            )
            + "`kept` names the trees this command deliberately did not touch and why."
        ),
    }


def _uninstall_one_agent(agent: SkillAgent) -> JsonObject:
    """One agent's half, taken back in the reverse of the order that wrote it."""
    steps = {
        "mcp_config": _remove_agent_mcp(agent),
        "skill": _remove_agent_skill(agent),
        "agent_write_restriction": _remove_agent_deny_rules(agent),
    }
    removed = [item for step in steps.values() for item in step["removed"]]
    left_alone = [item for step in steps.values() for item in step["left_alone"]]
    ok = all(step["ok"] for step in steps.values())
    if not ok:
        summary = f"Agentic HIL's half for {agent.display_name} was not fully taken back; `steps` says which part and why."
    elif removed:
        summary = f"Agentic HIL's half for {agent.display_name} was taken back."
    else:
        summary = f"Nothing to take back for {agent.display_name}: this installation had written nothing of its own here."
    return {"agent": agent.id, "ok": ok, "summary": summary, "removed": removed, "left_alone": left_alone, "steps": steps}


def _uninstall_step(summary: str, *, ok: bool = True, removed: list[JsonObject] | None = None, left_alone: list[JsonObject] | None = None, **fields: object) -> JsonObject:
    """One step's answer in the shape every step of this command answers in."""
    return {"ok": ok, "summary": summary, "removed": removed or [], "left_alone": left_alone or [], **fields}


def _removed_entry(what: str, path: Path | str) -> JsonObject:
    return {"what": what, "path": str(path)}


def _left_entry(what: str, path: Path | str, reason: str) -> JsonObject:
    return {"what": what, "path": str(path), "reason": reason}


def _remove_agent_mcp(agent: SkillAgent) -> JsonObject:
    """Take back the user-level MCP registration, where this installation wrote it.

    The file is another program's, and an absent one is answered without opening
    it: every guarded read and every lock in this package builds the directory
    chain on the way in, so probing a registration that was never written would
    plant `~/.codex` or `~/.config/opencode` on a machine that has neither.
    """
    path = _agent_mcp_config_path(agent.id)
    if not _path_entry_exists(path):
        return _uninstall_step(f"{agent.display_name} has no user-level MCP configuration, so there is no registration to take back.", path=str(path))
    with secure_user_file_lock(path):
        result = _remove_codex_mcp(agent, path) if agent.id == "codex" else _remove_json_mcp(agent, path)
    _release_lock_sidecar(path)
    return result


def _release_lock_sidecar(path: Path) -> None:
    """Remove the lock sidecar a transaction of this tool's leaves beside a file.

    `user_file_lock_path` states that the sidecar outlives its transaction, so
    anything cleaning up after this tool has to know the name. Without this an
    uninstall leaves a `.agentic-hil.lock` in `~/.claude`, in `~/.codex`, in
    `~/.config/opencode` and in the home directory itself, each one a file this
    tool put in another program's directory and named after itself: exactly the
    leftover this command exists to take back.

    Only after the lock has been released, and never insisted on. On Windows the
    sidecar cannot be unlinked while it is held, and a lock some other process is
    holding right now is one this run has no business taking away.
    """
    with suppress(OSError):
        user_file_lock_path(path).unlink()


def _remove_json_mcp(agent: SkillAgent, path: Path) -> JsonObject:
    """Claude Code and opencode: one named entry out of one JSON object.

    Which entry is ours is `_claude_mcp_entry_kind` / `_opencode_mcp_entry_kind`,
    the same three answers `register_agent_mcp` uses to tell its own earlier
    entry from an operator's. They are asked with the entry this run would write
    where the launcher still resolves and with nothing where it does not: a
    launcher that has stopped passing the trust check is a reason to be careful
    about writing an entry, and no reason to disown one.

    The containing object stays whatever it is left holding. `~/.claude.json` is
    the whole of that host's state and `opencode.json` carries the operator's own
    permissions; neither is this command's to tidy or to delete.
    """
    section = "mcpServers" if agent.id == "claude-code" else "mcp"
    document = _load_json_object(path)
    if document is None:
        return _uninstall_step(f"{path} is not a JSON object; left untouched.", ok=False, error_type="config_invalid", path=str(path))
    servers = document.get(section)
    if not isinstance(servers, dict) or "agentic-hil" not in servers:
        return _uninstall_step(f"{agent.display_name} has no agentic-hil MCP entry to take back.", path=str(path))
    if not _tool_written_mcp_entry(agent.id, servers["agentic-hil"]):
        return _uninstall_step(
            f"The agentic-hil MCP entry in {path} is not one Agentic HIL wrote; left untouched.",
            path=str(path),
            left_alone=[_left_entry("MCP registration", f"{path} :: {section}.agentic-hil", _LEFT_FOREIGN_ENTRY)],
        )
    del servers["agentic-hil"]
    secure_atomic_write_text(path, json.dumps(document, indent=2) + "\n")
    return _uninstall_step(
        f"The agentic-hil MCP entry was removed from {path}.",
        path=str(path),
        removed=[_removed_entry("MCP registration", f"{path} :: {section}.agentic-hil")],
    )


def _tool_written_mcp_entry(agent_id: str, entry: object) -> bool:
    """Whether one JSON MCP entry is this tool's own rather than the operator's."""
    desired: JsonObject = {}
    with suppress(ConfigError, OSError, ValueError):
        command = mcp_server_command()
        desired = {"type": "stdio", "command": command, "args": ["mcp-stdio"]} if agent_id == "claude-code" else {"type": "local", "command": [command, "mcp-stdio"], "enabled": True}
    kind = _claude_mcp_entry_kind(entry, desired) if agent_id == "claude-code" else _opencode_mcp_entry_kind(entry, desired)
    return kind is not None


def _remove_codex_mcp(agent: SkillAgent, path: Path) -> JsonObject:
    """Codex: the managed block, which is the only provenance that file carries.

    A TOML table without the markers around it was written by somebody else, and
    an `mcp_servers.agentic-hil` outside them is exactly what `_register_codex_mcp`
    refuses to touch on the way in.
    """
    try:
        existing = secure_optional_read_text(path)
    except UnicodeDecodeError:
        return _uninstall_step(f"{path} is not UTF-8 text, so it holds no marked block of this tool's; left untouched.", ok=False, error_type="config_invalid", path=str(path))
    if existing is None:
        return _uninstall_step("Codex has no user config.toml, so there is no registration to take back.", path=str(path))
    if existing.count(AGENTIC_HIL_MCP_START) != existing.count(AGENTIC_HIL_MCP_END) or existing.count(AGENTIC_HIL_MCP_START) > 1:
        return _uninstall_step(f"{path} contains malformed or duplicate Agentic HIL managed markers; left untouched.", ok=False, error_type="config_invalid", path=str(path))
    remainder = _text_without_marked_block(existing, AGENTIC_HIL_MCP_START, AGENTIC_HIL_MCP_END)
    if remainder is None:
        parsed, _parse_error = _parse_toml(existing)
        servers = parsed.get("mcp_servers") if isinstance(parsed, dict) else None
        if isinstance(servers, dict) and "agentic-hil" in servers:
            return _uninstall_step(
                f"The agentic-hil MCP table in {path} carries no Agentic HIL markers, so it is not one this tool wrote; left untouched.",
                path=str(path),
                left_alone=[_left_entry("MCP registration", f"{path} :: mcp_servers.agentic-hil", _LEFT_FOREIGN_ENTRY)],
            )
        return _uninstall_step(f"{agent.display_name} has no agentic-hil MCP entry to take back.", path=str(path))
    _write_or_remove(path, remainder)
    return _uninstall_step(
        f"The managed agentic-hil MCP block was removed from {path}.",
        path=str(path),
        removed=[_removed_entry("MCP registration", f"{path} :: mcp_servers.agentic-hil")],
    )


def _text_without_marked_block(existing: str, start: str, end: str) -> str | None:
    """`existing` with its one marked block gone, or None when it holds no block.

    The counterpart of `upsert_marked_block`, and it has to hand a file back to
    the program that owns it in a readable state: the blank line that separated
    the block from what came before goes with the block, and a remainder that is
    nothing but whitespace means the file held this tool's block and nothing
    else. The caller decides what an empty remainder is worth.
    """
    if start not in existing:
        return None
    pattern = re.compile(rf"\n*{re.escape(start)}[\s\S]*?{re.escape(end)}\n*")
    remainder = pattern.sub("\n\n", existing).strip()
    return f"{remainder}\n" if remainder else ""


def _write_or_remove(path: Path, text: str) -> None:
    """Write what is left of a file this tool appended to, or remove an empty one.

    A file whose entire content was this tool's block is this tool's file, and
    leaving a zero-byte `config.toml` or `AGENTS.md` behind is leaving a trace of
    an installation that is being removed. One created empty by somebody else and
    then appended to comes back to empty either way, so nothing anybody wrote is
    lost in the one case that cannot be told apart.
    """
    if text:
        secure_atomic_write_text(path, text)
        return
    secure_remove_file(path)


def _remove_agent_skill(agent: SkillAgent) -> JsonObject:
    """Take back the skill file, its directory, and any registration pointing at it.

    Ownership is `is_agentic_hil_setup_skill`, the frontmatter check
    `skill-install` refuses a foreign file by, so a skill somebody else put at
    this path is reported and left. Bytes that are not UTF-8 are not this tool's
    skill either, and answering that with a decode error out of an uninstall
    would be an internal error where a sentence belongs.

    The lock sidecar and the directory go after the transaction rather than
    inside it: the sidecar outlives the lock by design and on Windows is held
    open for as long as it is taken, so removing it from inside would fail and
    leave a directory that can never be empty.
    """
    target = _agent_skill_target(agent)
    removed: list[JsonObject] = []
    left_alone: list[JsonObject] = []
    ours = True
    if _path_entry_exists(target):
        with secure_user_file_lock(target):
            try:
                existing = secure_optional_read_text(target)
            except UnicodeDecodeError:
                existing, ours = None, False
            if existing is not None and not is_agentic_hil_setup_skill(existing):
                ours = False
            if ours and existing is not None:
                secure_remove_file(target)
                removed.append(_removed_entry("agent skill", target))
        _release_lock_sidecar(target)
        if ours:
            _remove_skill_directory(target)
        else:
            left_alone.append(_left_entry("agent skill", target, "This file is not the Agentic HIL setup skill, and --force never replaces a foreign skill either."))
    # Independent of whether the current target exists: a user who still has
    # only an older managed skill (`agentic-hil-config-setup/SKILL.md` and
    # nothing at the current name) must have it taken back too, not silently
    # left discoverable because there was no current file to gate this on.
    # `remove_legacy_skills` stats each legacy path itself before touching it,
    # so nothing here plants the current target's directory just to look.
    removed.extend(_removed_entry("superseded agent skill", path) for path in remove_legacy_skills(target))
    registration = _remove_skill_registration(agent, target)
    removed.extend(registration["removed"])
    left_alone.extend(registration["left_alone"])
    if not registration["ok"]:
        return _uninstall_step(registration["summary"], ok=False, removed=removed, left_alone=left_alone, path=str(target), registration=registration)
    if removed:
        summary = f"The Agentic HIL skill for {agent.display_name} was removed."
    elif left_alone:
        summary = f"The file at the {agent.display_name} skill path is not Agentic HIL's; left untouched."
    else:
        summary = f"{agent.display_name} has no Agentic HIL skill installed."
    return _uninstall_step(summary, removed=removed, left_alone=left_alone, path=str(target))


def _remove_skill_directory(target: Path) -> None:
    """Remove the directory this tool created to hold the skill.

    Only the directory named for the skill; anything else at that path was
    chosen by somebody else. `rmdir` and not a tree removal, so a directory
    holding a file this command did not put there survives with that file in it,
    and the `skills` directory above it is the host's own convention and stays
    whether or not it is left empty.
    """
    if target.parent.name != SKILL_NAME:
        return
    with suppress(OSError):
        target.parent.rmdir()


def _remove_skill_registration(agent: SkillAgent, target: Path) -> JsonObject:
    """Take back the AGENTS.md block that told an agents-md host the skill is there.

    Only Codex has one; the other two read their skills directory, so removing
    the file is the whole of the removal for them.
    """
    if agent.registration != "agents-md":
        return _uninstall_step(f"{agent.display_name} discovers skills from its skills directory, so nothing registered it.")
    path = _external_user_path(Path(skill_install_root(str(target))) / "AGENTS.md", "Agent skill registration")
    if not _path_entry_exists(path):
        return _uninstall_step(f"{agent.display_name} has no AGENTS.md, so nothing registered the skill.", path=str(path))
    with secure_user_file_lock(path):
        result = _remove_registration_block(agent, path)
    _release_lock_sidecar(path)
    return result


def _remove_registration_block(agent: SkillAgent, path: Path) -> JsonObject:
    try:
        existing = secure_optional_read_text(path)
    except UnicodeDecodeError:
        return _uninstall_step(f"{path} is not UTF-8 text; left untouched.", ok=False, error_type="config_invalid", path=str(path))
    if existing is None:
        return _uninstall_step(f"{agent.display_name} has no AGENTS.md, so nothing registered the skill.", path=str(path))
    if existing.count(AGENTIC_HIL_REGISTRATION_START) != existing.count(AGENTIC_HIL_REGISTRATION_END) or existing.count(AGENTIC_HIL_REGISTRATION_START) > 1:
        return _uninstall_step(f"{path} contains malformed or duplicate Agentic HIL registration markers; left untouched.", ok=False, error_type="config_invalid", path=str(path))
    remainder = _text_without_marked_block(existing, AGENTIC_HIL_REGISTRATION_START, AGENTIC_HIL_REGISTRATION_END)
    if remainder is None:
        return _uninstall_step(f"{path} carries no Agentic HIL skill registration.", path=str(path))
    _write_or_remove(path, remainder)
    return _uninstall_step(
        f"The Agentic HIL skill registration was removed from {path}.",
        path=str(path),
        removed=[_removed_entry("skill registration", path)],
    )


def _remove_agent_deny_rules(agent: SkillAgent) -> JsonObject:
    """Take back the write refusals `init --agent` wrote into an agent's settings.

    Only Claude Code ever received one. Codex sandboxes model-generated shell
    commands and was given nothing; opencode is left to the operator on purpose,
    and the inert absolute patterns earlier releases wrote there deny nothing
    whatever tree they name, so reaching into a file this tool no longer writes
    to would buy that nothing.

    Which rules are this tool's own is not decided here. It is
    `_tool_written_deny_tree`, the namespace claim #206 established, widened by
    exactly what `_superseded_claude_code_deny_rules` already recognises: the
    literal text this tool derives from a project's own paths, for every project
    configuration still on disk. That second half is what reaches a `state_root`
    an operator put outside this tool's own roots, which the namespace claim
    cannot reach and deliberately never claims.

    What #206 weighs and this does not is whether a tree is still *wanted*. There
    it decides everything, because a refresh runs while other projects still want
    their protection. Here the installation that wrote every one of these rules
    is going away, so nothing wants any of them, and a rule left behind is the
    exact leftover #206 was reported from.
    """
    path = _agent_permission_config_path(agent.id)
    if path is None:
        return _uninstall_step("Codex was never given a write refusal, so there is none to take back.", mode="sandboxed-by-the-agent")
    if agent.id != "claude-code":
        return _uninstall_step(f"{_OPENCODE_UNRESTRICTED} Nothing was written there, so nothing is taken back.", mode="operator-managed", path=str(path))
    if not _path_entry_exists(path):
        return _uninstall_step(f"{agent.display_name} has no settings file, so there are no write refusals to take back.", path=str(path))
    with secure_user_file_lock(path):
        result = _remove_claude_code_deny_rules(agent, path)
    _release_lock_sidecar(path)
    return result


def _remove_claude_code_deny_rules(agent: SkillAgent, path: Path) -> JsonObject:
    document = _load_json_object(path)
    if document is None:
        return _uninstall_step(f"{path} is not a JSON object; left untouched.", ok=False, error_type="agent_permissions_unreadable", path=str(path))
    permissions = document.get("permissions")
    deny = permissions.get("deny") if isinstance(permissions, dict) else None
    if not isinstance(deny, list):
        return _uninstall_step(f"{agent.display_name} has no deny rules, so there are none to take back.", path=str(path))
    written = _project_written_deny_rules()
    # `isinstance` first, for the same reason the writer needs it: the list
    # belongs to another program and an entry that is not a string is not a rule
    # anybody wrote here.
    removed = [rule for rule in deny if isinstance(rule, str) and (rule in written or _tool_written_deny_tree(rule) is not None)]
    if not removed:
        return _uninstall_step(f"{agent.display_name} carries no write refusal this installation wrote.", path=str(path))
    taken_back = set(removed)
    permissions["deny"] = [rule for rule in deny if not (isinstance(rule, str) and rule in taken_back)]
    secure_atomic_write_text(path, json.dumps(document, indent=2) + "\n")
    return _uninstall_step(
        f"The write refusals Agentic HIL wrote were removed from {path}; every other rule in it is the operator's and stayed.",
        path=str(path),
        removed=[_removed_entry("write refusal", f"{path} :: {rule}") for rule in removed],
    )


def _project_written_deny_rules() -> set[str]:
    """Every deny rule this tool wrote for a project configuration still on disk.

    The namespace claim in `_tool_written_deny_tree` covers a tree at or under
    one of this tool's own per-user roots and stops there, so a `state_root` an
    operator put on another volume is outside it. Every configuration this user
    has names its own state root, and the rules were derived from that name, so
    for a configuration that can still be read the text is recoverable exactly:
    the same argument `_superseded_claude_code_deny_rules` makes, over the same
    strings, and never over a shape.

    A configuration that cannot be read contributes nothing rather than stopping
    the removal. `_protected_trees_still_wanted` answers `None` there because an
    incomplete answer could take protection off a bench that still wants it; the
    incompleteness costs the opposite here, which is one rule left standing.
    """
    rules: set[str] = set()
    for config_path, state_root in _visible_project_configurations():
        if state_root is None:
            continue
        rules |= _superseded_claude_code_deny_rules(config_path, state_root)
        rules |= {f"Edit({pattern})" for pattern in _claude_code_deny_patterns(config_path, state_root)}
    return rules


def _visible_project_configurations() -> list[tuple[Path, Path | None]]:
    """Every project configuration under the config root, with the state root it names.

    `None` for one that cannot be read. A configuration bound by
    `AGENTIC_HIL_CONFIG` is invisible here and nothing on disk records where it
    is, which is the same blind spot `_protected_trees_still_wanted` has and for
    the same reason.
    """
    found: list[tuple[Path, Path | None]] = []
    directory = project_config_directory()
    with suppress(OSError):
        if directory.is_dir():
            for entry in sorted(directory.iterdir()):
                candidate = entry / PROJECT_CONFIG_FILENAME
                if candidate.is_file():
                    found.append((candidate, _configured_state_root(candidate)))
    return found


def _uninstall_kept_trees() -> list[JsonObject]:
    """The trees this command names and does not remove, with the reason on each.

    Named rather than counted, because "state root" is an abstraction and a path
    is something an operator can look inside before deciding. The per-user roots
    are the ones this tool creates for itself, reported only where they are
    actually there; a `state_root` a configuration points somewhere else is
    added from the configuration that names it.
    """
    configurations = _visible_project_configurations()
    kept: list[JsonObject] = [
        {
            "what": "project configurations",
            "path": str(project_config_directory()),
            "count": len(configurations),
            "reason": _KEPT_CONFIGURATION,
        }
    ]
    trees: dict[str, Path] = {}
    for path in [*tool_owned_user_roots(), *(state_root for _config, state_root in configurations if state_root is not None)]:
        if _path_entry_exists(path):
            trees.setdefault(os.path.normcase(str(path)), path)
    kept.extend({"what": "tree Agentic HIL created for itself", "path": str(path), "reason": _KEPT_TOOL_TREE} for path in trees.values())
    return kept


_NO_AGENT_NAMED = "No agent was named, so no agent write restriction was applied. Pass --agent to have that agent refuse its own write tools on the policy files."
_NO_AGENT_ON_EXISTING_CONFIG = (
    "No agent was named and the config was already there, so nothing was written at all. "
    "Run `agentic-hil init --agent <agent>` to add or refresh that agent's refusal of its own write tools on the config and the state root; "
    "it leaves the config exactly as it is."
)


def init_project(config_path: str | None = None, agent: str | None = None, force: bool = False) -> JsonObject:
    """Set up one project: its authoritative config, verified by doctor.

    Everything here is project state, so it reuses `init_config` rather than
    repeating it, and its rollback covers this workspace's config plus the
    agent's write-permission rules — never the user-wide skill or MCP
    registration. Naming an agent is optional: without one the config is still
    written and verified, and only the step that asks that agent to refuse its
    own write tools is left out.

    On a config that is already there, this completes the permission half and
    nothing else: the file stays byte for byte as the operator left it, doctor
    still runs, and the agent's rules for this workspace's config directory and
    state root are added or refreshed. That is the only route to the write
    refusal for a bench whose first run was split (a plain `init` by the agent,
    `agent-install` by the operator) or whose config predates the rules; the
    alternative would be `init --force`, which rewrites operator policy and is
    not a remedy.
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
    # An existing config that is being kept is not this run's to write, so it
    # stays out of the rollback set: an operator's read-only policy file must
    # neither be locked nor restored by a later step that fails.
    keep_existing = config_exists and not force
    mutation_paths = _project_mutation_paths(resolved_agent, target_path)
    if keep_existing:
        mutation_paths.remove(target_path)
    # Nothing project-side is smoothed. `state_root`, the authoritative config
    # and the agent's policy file were chmod-ed here to satisfy the configured
    # path trust check, which is gone; nothing left refuses any
    # of them for its mode, so an operator's group-writable tree keeps the modes
    # it was given. The field stays because callers read it.
    permission_changes: list[str] = []
    state_actions = ensure_safe_state_root()
    config_result: JsonObject
    doctor_result = _skipped_setup_step("Doctor was not reached.")
    permission_result: JsonObject
    if resolved_agent is None:
        # On an existing config this is the whole of what the run could still
        # have done, so the report names the command that does it rather than
        # the flag alone.
        permission_result = {"ok": True, "skipped": True, "summary": _NO_AGENT_ON_EXISTING_CONFIG if keep_existing else _NO_AGENT_NAMED}
    else:
        permission_result = _skipped_setup_step("Agent write restriction was not reached.")

    with ExitStack() as locks:
        for path in sorted(mutation_paths, key=lambda item: os.path.normcase(str(item))):
            locks.enter_context(secure_user_file_lock(path))
        snapshots = _capture_file_snapshots(mutation_paths)
        try:
            if keep_existing:
                config_result = {"ok": True, "skipped": True, "existing": True, "summary": "Existing authoritative config, unchanged: not a byte of it was written, and this never replaces operator policy.", "path": str(target_path)}
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
        # Read off the user-wide half rather than decided again: that half is the
        # one that writes the registration, and a second reading of the same
        # step is a second answer waiting to differ from the first.
        **{key: user_result[key] for key in ("restart_required", "restart_notice") if key in user_result},
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
    return _with_restart_notice(result)


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
        # `existed`, not the decoded text: a skill that was already there is one
        # rollback restored, and whether its bytes happen to be UTF-8 does not
        # change that it survived.
        if os.path.normcase(str(snapshot.path)) != identity or not snapshot.existed:
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


def _existing_config_text(target_path: Path) -> tuple[bytes | None, str | None, str | None]:
    """The file `--force` is about to overwrite: its exact bytes, its decoded
    text, and why the text could not be read.

    `--force` is the operator's reset, so it has to work on exactly the file most
    in need of one. Bytes that are not UTF-8 — a UTF-16 save, a truncated write,
    something binary dropped on the path — once left this command with an
    unhandled ``UnicodeDecodeError`` and no way to regenerate at all.

    Three returns because a rollback and a permission-narrowing analysis want
    different reads of one file. The bytes are what a failed regeneration
    restores — the original, exactly, whether or not it is UTF-8 — so a non-UTF-8
    file is put back rather than deleted; before this, the
    snapshot was taken from the decoded text, and text that would not decode was
    recorded as absence, so rollback removed the original it claimed to restore.
    The decoded text is for the narrowing comparison, which is over UTF-8 YAML
    and has no answer for bytes that are not text; there the reason takes its
    place.

    The decode is caught and nothing else. A ``ConfigError`` from the guarded
    read — the path is a symlink, a hardlink, or sits under a directory this user
    does not solely own — must keep coming out of it, because it says the object
    about to be overwritten is not the one that was named. Widening this to
    swallow it would turn a fail-closed refusal into a silent write over the
    wrong file."""
    raw = secure_optional_read_bytes(target_path)
    if raw is None:
        return None, None, None
    try:
        return raw, raw.decode("utf-8"), None
    except UnicodeDecodeError:
        return raw, None, "the file it replaced is not UTF-8 text, so nothing in it could be read"


def _discarded_narrowings(previous_text: str | None, written: JsonObject) -> tuple[list[str], str | None]:
    """Which permissions the replaced file had closed and the new one grants.

    `--force` is the full regeneration and stays that way: it
    already discards the baudrate, the `resource_id`, the `state_root` and the
    artifact roots, so a version that rescued permissions and nothing else would
    be harder to predict rather than easier. `carry_over_permissions` is the MCP
    path's answer and is deliberately not wired in here; `agentic-hil
    adopt-hardware` is the command for refreshing hardware and keeping the rest.

    What the reset owes its operator is the list. Somebody narrows a bench, runs
    `--force` weeks later for an unrelated reason, and without this nothing in
    the result says the bench is open again — the same silent reopening the
    one-way street on `project_config_set` exists to prevent.

    Returns ``(paths, unreadable_reason)``. A file that is not there yields
    neither: there was nothing to discard and an empty list would be noise. A
    file that cannot be read as a configuration yields the reason instead of an
    empty list, because "nothing was lost" and "this could not be checked" are
    different answers and only one of them is safe to act on."""
    if previous_text is None:
        return [], None
    try:
        previous = yaml.safe_load(previous_text)
    except yaml.YAMLError as error:
        detail = getattr(error, "problem", None) or str(error).splitlines()[0]
        return [], f"the file it replaced was not parseable YAML: {detail}"
    if not isinstance(previous, dict):
        return [], "the file it replaced held no YAML mapping, so it named no permissions to compare"
    granted = permission_surface(written)
    closed = (path for path, permitted in permission_surface(previous).items() if not permitted)
    return sorted(path for path in closed if granted.get(path)), None

def _init_bench_read(workspace: Path) -> tuple[JsonObject, JsonObject | None, str | None]:
    """Read the attached board for `init`, holding what a probe read holds.

    `init` called `discover_attached_hardware` directly: no `before_connect`, no
    coordinator, no record. Enumerating probes and connecting in HOTPLUG mode is
    a hardware read either way, so that reached a board another run was holding —
    where `project_config_create`, which writes the same file out of the same
    read, answers `device_busy` — and it did so leaving nothing in the audit
    trail. Since agentic-hil/agentic-hil#112 made `init` look
    unconditionally that applied to every `init` rather than only to a workspace
    carrying a profile.

    That the CLI is the operator's authority does not carry the exception, and
    `adopt_hardware` is the proof: it makes the same argument about the *grant*,
    waives that, and leases anyway. Authority over your own configuration is not
    authority over somebody else's running bench — and `--force` rewriting the
    file in the middle of a stranger's run is not something the operator wants
    either. So this goes through `discover_for_generation`, which is the function
    `project_config_create` uses, under this command's own coordinator.

    The open-run refusal comes first, before a word is said to any board. Every
    other write of this file has one — `project_config_create`,
    `project_config_adopt_hardware` and `agentic-hil grant`/`revoke` all refuse
    while a run, a lease or another terminal holds the bench — and `init --force`
    had none. What stopped it was incidental: a run that declared the probe owns
    the lock the read takes, so the read answered `device_busy`. A run that
    declared only a COM port or only a CAN bus holds no probe, and that
    regeneration went through and replaced the permissions, the baudrate and the
    device bindings underneath live measurements. `bench_open_holds` already
    answers the question the other write sites ask, so this asks it too.

    Returns the discovery, the refusal that is the whole answer when the read did
    not happen or did not end cleanly, and — when the board was read without a
    lease — the reason it could not be leased.
    """
    try:
        current: AgenticHILConfig | None = load_authoritative_config(workspace)
    except ConfigError as error:
        # No configuration to lease against, so no `state_root` to record a lease
        # under and no policy to audit against — the machinery does not exist
        # rather than being skipped. Both cases that reach here are that: the
        # first `init` of a workspace, and an `init --force` over a file that
        # cannot be loaded, which is the one command that repairs it. Refusing
        # either would leave no way to reach a configured bench at all, which is
        # worse than what this fixes. Named in the result instead of inferred
        # from a record that is not there.
        #
        # It is also why the holds question below is asked only of a
        # configuration that loaded: the devices a hold would be on are the ones
        # this file names, and a file that does not load names none.
        current, unleased = None, error.error_type
    else:
        unleased = None
        holds = bench_open_holds(current)
        if holds is not None:
            return {}, _init_open_run_refusal(current, holds), unleased
    discovery, refusal = discover_for_generation(
        current,
        None,
        tool=CLI_INIT,
        reason_prefix=CLI_INIT,
        frontend="operator-cli",
    )
    return discovery, refusal, unleased


def _init_open_run_refusal(existing: AgenticHILConfig, open_holds: JsonObject) -> JsonObject:
    """Somebody holds this bench, so `init --force` does not regenerate it.

    The same rule and the same error type as `project_config_create`, which
    writes this file from the same read over MCP: what is held was taken under
    the policy this file states, and a regeneration replaces every permission,
    every baudrate and every device binding in it. The holder here is usually
    another process — an MCP server, another terminal — so the next step names
    `agentic-hil lease-status`, which says whose bench it is, rather than a run
    this caller could close.

    Nothing was read either. The refusal is raised before the board is touched,
    so a `--force` typed by accident during somebody's run costs neither a
    HOTPLUG connect nor a line in the audit trail.
    """
    return {
        "ok": False,
        "tool": CLI_INIT,
        "error_type": "config_write_in_open_run",
        "summary": (
            "Something on this machine is holding this bench's hardware right now, and those holds were taken under "
            "the policy this configuration states. Regenerating reads the attached probe and replaces that policy, so "
            "nothing was read and nothing was written."
        ),
        "open_holds": open_holds,
        "path": existing.config_path,
        "workspace_root": existing.workspace_root,
        **remediation_fields("config_write_in_open_run"),
        "next_step": (
            "`agentic-hil lease-status` names the holder — which devices are held, which frontend took them and under "
            "which process — and `open_holds` here carries the same. Let the run finish or ask whoever holds it to "
            f"close it, then run `{CONFIG_REOPEN_COMMAND}` again. `agentic-hil adopt-hardware` is the command that "
            "refreshes the hardware and leaves the rest of the file standing."
        ),
        **NOT_STARTED,
        "retry_safe": True,
    }


def _init_lease_note(unleased: str | None) -> JsonObject:
    """Whether the board `init` read was read under a lease, and if not, why not."""
    if unleased is None:
        return {
            "leased": True,
            "tool": CLI_INIT,
            "summary": "The attached probe was read under a hardware lease, and that read is in the audit trail.",
        }
    return {
        "leased": False,
        "reason": unleased,
        "summary": (
            "This workspace had no loadable configuration to lease or audit against, so the attached probe was read "
            "directly. That is the one case where nothing on this machine can be holding a board on this project's "
            "behalf; every read after this one goes through the lease."
        ),
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
    original_bytes, existing, unreadable_existing = _existing_config_text(target_path)
    # Look first, and let the profile decide only what is written down. A
    # workspace profile says how to name and narrow a bench that was found; it
    # cannot say whether looking is allowed, and gating the read on it
    # meant a fresh installation with no `agentic-hil.config.example.yaml` got a
    # file full of placeholders on a machine with the board plugged in — while the
    # MCP server, which reads unconditionally, found that same board. The two
    # answers differed in the one thing neither of them decides: what is attached.
    # Without a profile the fixed default fills the template, which is the profile
    # `project_config_create` has always used, so both paths now write one file for
    # one machine.
    profile = load_project_profile(workspace)
    discovery, refusal, unleased = _init_bench_read(workspace)
    if refusal is not None:
        # Either the read was refused or it was never reached, and both leave
        # nothing to write a file from. Nothing was written; the refusal is the
        # whole answer.
        return {**refusal, "path": str(target_path)}
    discovered = overall_success(discovery)
    if discovered:
        template = yaml.safe_load(DEFAULT_CONFIG_TEMPLATE)
        assert isinstance(template, dict)
        try:
            # The profile is a hand-written file in the repository and the only
            # untrusted input this template filling has. A value it cannot use is
            # a refusal that names the key, not a traceback out of `init` after
            # the board was already read; nothing has been written at this point.
            configured = apply_discovery_to_template(template, profile if profile is not None else DEFAULT_PROJECT_PROFILE, discovery)
        except ConfigError as error:
            return {**error.to_dict(), "summary": f"{error.summary} No configuration was written.", "path": str(target_path)}
        document = {"workspace_root": str(workspace), "state_root": str(user_state_root()), **configured}
        text = yaml.safe_dump(document, sort_keys=False, allow_unicode=False)
    else:
        text = f"workspace_root: {json.dumps(str(workspace))}\nstate_root: {json.dumps(str(user_state_root()))}\n\n{DEFAULT_CONFIG_TEMPLATE}"
    safe_directory(target_path.parent)
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

    # Snapshotted as the exact bytes that were there, not as `existing` (the
    # decoded text, which is None for a non-UTF-8 file): a failed final
    # validation below has to put the original back byte-for-byte rather than
    # delete a file it could not decode.
    snapshot = FileSnapshot(target_path, original_bytes)
    try:
        secure_atomic_write_text(target_path, text)
        written = load_authoritative_config(workspace)
    except ConfigError as error:
        rollback_errors = _restore_file_snapshots([snapshot])
        result = error.to_dict()
        result["summary"] = "Agentic HIL starter configuration failed final validation and was rolled back."
        result["path"] = str(target_path)
        if rollback_errors:
            result["rollback_errors"] = rollback_errors
        return result
    available_com_ports = list_available_com_ports()
    # Read off the file that was written rather than asserted. The skeleton path
    # grants everything it can, but a project's own
    # `agentic-hil.config.example.yaml` may set a flag false and that is honoured
    # — an operator who wrote `allow_mass_erase: false` into their profile meant
    # it. A fixed sentence here told them the opposite about their own bench.
    #
    # `narrowed_permissions` rather than a walk of the whole surface, so that the
    # two flags the skeleton itself writes false are not reported as the
    # profile's doing. They are the same two on every bench,
    # profile or no profile, and are stated once here instead.
    written_document = yaml.safe_load(text) or {}
    narrowed = narrowed_permissions(written_document)
    granted_clause = (
        "with every permission granted except the two that are false so that flashing works"
        if not narrowed
        else f"with every permission granted except the two that are false so that flashing works, and {len(narrowed)} the project profile set to false"
    )
    discarded, unreadable_previous = _discarded_narrowings(existing, written_document)
    unreadable_previous = unreadable_existing or unreadable_previous
    next_steps = init_next_steps(
        available_com_ports,
        target_path,
        narrowed=sorted(narrowed),
        drives_hardware=any(debugger_drives_hardware(written, entry) for entry in written.debuggers.values()),
    )
    if not discovered:
        # The placeholders are a finding, not a default. An operator who is not
        # told that discovery ran and came back empty reads the same file as
        # "detection is broken" — which is exactly how this was reported.
        next_steps.insert(
            0,
            "This file describes no board yet, because hardware discovery ran and found none: "
            f"{discovery.get('summary') or 'no attached bench was identified'} "
            "(the whole result is under `hardware_discovery`). Attach the bench and run "
            "`agentic-hil adopt-hardware`, which fills in the probe id, the toolchain executable, the detected "
            "controller and the probe's own COM port without anything being retyped.",
        )
    # First, and before anything about COM ports or OpenOCD scripts: a bench that
    # was narrowed and is open again is the one thing here that changes what this
    # machine may be told to do.
    if discarded:
        next_steps.insert(
            0,
            f"`{CONFIG_REOPEN_COMMAND}` replaced a file that had {len(discarded)} "
            f"{'permission' if len(discarded) == 1 else 'permissions'} set to false, and this one grants "
            f"{'it' if len(discarded) == 1 else 'them'}: {', '.join(discarded)}. Close each one again with "
            f"`{CONFIG_REVOKE_COMMAND} <key>`, which moves one permission and leaves every other setting in the file "
            f"alone (`{CONFIG_GRANT_COMMAND} <key>` is the inverse) — or leave them open if the reset is what you "
            "wanted. The rest of the old file is gone the same way: `--force` starts over, and `agentic-hil "
            "adopt-hardware` is the command that refreshes the hardware and keeps everything else.",
        )
    elif unreadable_previous:
        next_steps.insert(
            0,
            f"`{CONFIG_REOPEN_COMMAND}` replaced a file it could not read as a configuration — {unreadable_previous} — "
            "so it cannot say which permissions that file had set to false, and this one grants every permission the "
            "profile did not narrow. Read the `permissions` blocks in the new file and close what this bench should "
            f"not have with `{CONFIG_REVOKE_COMMAND} <key>`.",
        )
    return {
        "ok": True,
        "summary": (
            (
                f"Attached hardware was discovered and configured, {granted_clause}."
                if discovered
                else f"No attached bench was found, so the placeholder Agentic HIL project configuration was written, {granted_clause}."
            )
            + (
                f" The file it replaced had {len(discarded)} {'permission' if len(discarded) == 1 else 'permissions'} "
                f"set to false that this one grants, so {'it is' if len(discarded) == 1 else 'they are'} open again: "
                f"{', '.join(discarded)}."
                if discarded
                else f" What the file it replaced had narrowed is not known, because {unreadable_previous}."
                if unreadable_previous
                else ""
            )
        ),
        "path": str(target_path),
        "optional_override": f'{CONFIG_ENV}={target_path}',
        "permissions": permission_summary(written),
        "narrowed_permissions": sorted(narrowed),
        **({"discarded_narrowings": discarded} if discarded else {}),
        **({"discarded_narrowings_unreadable": unreadable_previous} if unreadable_previous else {}),
        "available_com_ports": available_com_ports,
        "hardware_discovery": discovery,
        "hardware_lease": _init_lease_note(unleased),
        "next_steps": next_steps,
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
    enforces from the document rather than from the request. Nothing here needs
    to be argued as the lesser evil either: the same shell already has
    `agentic-hil init --force`, which since 0.8.0 rewrites the whole file
    at the generated defaults. Whoever has that shell has the operator's
    authority over this configuration outright, and the ratchet was never a
    promise about them — it holds on the MCP write path, which is where an agent
    that has only the MCP tools lives.

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


def change_permission(command: str, keys: list[str]) -> JsonObject:
    """`agentic-hil grant` and `agentic-hil revoke`, the operator's half of the ratchet.

    The ratchet leaves an agent able to write `false` into a permission and
    nothing else, and answers the other direction only for a file that does not
    exist yet: a generation opens everything it can. For a bench that has been
    running a while there was no answer that did not cost the rest of the file:
    `init --force` does come back open, and so does deleting the configuration,
    both at the cost of the baudrate, the `resource_id`, the `state_root` and
    every artifact root somebody set. That is the bill these two commands
    settle, and this is the gate. `carry_over_permissions`, the regeneration
    that does keep a narrowing, is `project_config_create`'s and belongs to no
    command here.

    It is a command and not a tool, and that is the whole security argument: an
    agent holding only the MCP tools cannot reach it, so the ratchet on that
    surface is untouched. Whoever has this shell already has `agentic-hil init
    --force`, which rewrites every permission in the file at once without
    consulting a grant — so a command that moves one named permission is strictly
    narrower than the authority already standing here, not a new one.

    The check and the write are one transaction against a run starting. Reading
    the holds and then writing left a gap: a run that began inside it took the
    bench under the old policy while the write moved the policy underneath —
    the open-run refusal, defeated by timing rather than argued away.
    So when the bench reads free, every configured device is held for the length
    of the check-and-write, on the same machine-wide locks a run or a session
    takes; one that begins now collides with those instead of slipping between the
    read and the write, and the outcome is that either it is refused or this write
    is. The holds are given back the moment the write returns.
    """
    config = load_cli_authoritative_config(None)
    return _change_permission_holding_the_bench(command, keys, config)


def _change_permission_holding_the_bench(command: str, keys: list[str], config: AgenticHILConfig) -> JsonObject:
    workspace = Path(config.work_dir)
    # The holder that is already there: a live session or a quarantine on the
    # project lock, or a run's device holds. Answered first, so a busy bench is a
    # refusal that still validates the key names before it (set_permission decides
    # that order), and so the one holder a device-lock hold cannot see — a project
    # lock with no device under it — is still caught.
    holds = bench_open_holds(config)
    if holds is not None:
        return set_permission(workspace, config, keys, command=command, open_holds=holds)
    # The devices to hold and the document to write against come from one read
    # taken here, after the (slow) holds probe above rather than from the config
    # loaded before it. The keys and the description then describe the same file
    # state, so a device repointed in the gap — `COM9` to `COM10`, a `resource_id`
    # moved from one board to another — cannot leave this command holding the old
    # key while a run takes the new one. `expect_document` carries that same
    # description into the write, which refuses if it has moved by the time the
    # write lock is held: either the run collides with the keys held here, or the
    # write is refused, never a policy landing over a bench held under keys this
    # call never saw.
    try:
        fresh, document = _config_and_document_for_hold(workspace, config)
    except ConfigError:
        # The file stopped loading between the CLI's own load and here. Let
        # set_permission read it again and return the structured refusal it owns.
        return set_permission(workspace, config, keys, command=command)
    device_keys = config_devices(fresh).lock_keys
    if not device_keys:
        # No physical device to hold, and so no run to race: the file is the whole
        # of what changes, and its own write lock makes that atomic already. The
        # description CAS still guards against an entry appearing in the gap.
        return set_permission(workspace, fresh, keys, command=command, expect_document=document)
    # Hold every configured device across the check-and-write. `begin_run` and a
    # lease both take these machine-wide device locks, so a run or session that
    # begins now waits or is refused here rather than starting under the old
    # policy — and a run that got in first makes this acquisition fail, which is
    # the "the write is refused" side of the same guarantee.
    bench = BenchMutex(frontend="operator-cli")
    try:
        try:
            bench.acquire(device_keys, wait_s=0)
        except DeviceBusyError as collision:
            return set_permission(workspace, fresh, keys, command=command, open_holds=_holds_from_collision(collision.result), expect_document=document)
        return set_permission(workspace, fresh, keys, command=command, expect_document=document)
    finally:
        bench.release_all()


def _config_and_document_for_hold(workspace: Path, config: AgenticHILConfig) -> tuple[AgenticHILConfig, JsonObject]:
    """A fresh pinned config and the document it was read from, for one hold.

    Two adjacent reads of the one authoritative file: the config gives the lock
    keys a run would take (pinned, so an executable-identified debugger's key
    matches what a run holds), and the document is what the write refuses a
    description move of. Reading both here rather than reusing the config the CLI
    loaded before the holds probe is what ties the keys acquired to the state the
    write is checked against."""
    target_path = authoritative_write_target(workspace, config)
    _, document = load_config_document(target_path)
    return load_authoritative_config(workspace), document


def _holds_from_collision(busy: JsonObject) -> JsonObject:
    """A `BenchMutex` device-busy result, shaped as the open-holds a refusal names.

    `bench_open_holds` describes the bench from outside its holder; a collision
    the writer hit while taking the bench for its own write describes the same
    fact from the other side — the device it could not take, and who holds it.
    Marked `owner_active: False` because what refused here was a device lock, not
    the project lock a live session holds, and `raced_a_run` so the reason the
    write stopped is legible rather than looking like a bench that was busy all
    along."""
    resource = busy.get("resource")
    holds: JsonObject = {"owner_active": False, "raced_a_run": True}
    if isinstance(resource, str):
        holds["held_devices"] = [resource]
        holds["busy_devices"] = [resource]
    holder = busy.get("holder")
    if isinstance(holder, dict):
        holds["holder"] = holder
    for field in ("held_since", "heartbeat_age_s"):
        if busy.get(field) is not None:
            holds[field] = busy[field]
    return holds


def bench_open_holds(config: AgenticHILConfig) -> JsonObject | None:
    """What is holding this bench right now, seen from outside whoever holds it.

    The MCP service asks its own coordinator and gets an exact answer, because
    the holder is itself. A command line is a different process and has to ask
    the locks, and both of them have to be asked: a lease takes the project lock
    and holds it for as long as the session lives, while a declared run takes the
    machine-wide device locks and *not* the project lock, so a run between two of
    its own calls is invisible to the first question.

    Asking is `HardwareCoordinator.status()`'s job, not this one's. The device
    half used to live here and helped no other caller — the
    same status read a second terminal makes still reported a run's held bench as
    free. This reshapes the one answer for the refusal; it does not go back to
    the locks a second time. Why the holder records are not that answer, and why
    each device is taken and released on its own, is in `device_holds`.

    `held_devices` here is deliberately wider than the field of that name in
    `status()`: the locks say which devices are held, and the project record
    additionally names what the live owner declared. A refusal that says what a
    permission change would move underneath wants both.

    From out here a declared run and a session that outlived one are the same
    fact, so neither is claimed: what is reported is that the bench is held and
    by whom. A hold is a hold, and that is what the refusal turns on — those
    devices were taken under the permissions in this file, and moving the rules
    underneath them is what the open-run refusal rules out.
    """
    coordinator = HardwareCoordinator(config, "operator-cli")
    try:
        status = coordinator.status()
    finally:
        coordinator.close()
    if not status.get("bench_held"):
        return None
    record = status.get("record") or {}
    busy = list(status.get("device_holds") or [])
    holds: JsonObject = {
        "owner_active": bool(status.get("owner_active")),
        "held_devices": sorted({*(str(item) for item in (record.get("resources") or []) if isinstance(item, str)), *(str(item) for item in (status.get("held_devices") or []) if isinstance(item, str))}),
        "busy_devices": busy,
        # The project record is advisory while somebody holds the lock, so say so
        # rather than presenting a read that could have straddled their write.
        "snapshot_atomic": bool(status.get("snapshot_atomic")),
    }
    for field in ("frontend", "owner_pid", "owner_started_at", "updated_at"):
        if record.get(field) is not None:
            holds[field] = record[field]
    return holds


def initialized_config_path(workspace: Path) -> Path:
    """Where `init` writes. The same rule the agent provisioning path follows, so
    the two cannot disagree about which file is authoritative."""
    return authoritative_config_target(workspace)


def start_detached_test_reactor(test_config_path: str | None = None, *, wait_s: float = 0.0) -> JsonObject:
    """Start a run in its own process and answer at once, for this working directory."""
    return start_plan_detached(load_authoritative_config(Path.cwd()), test_config_path, wait_s=wait_s)


def run_test_reactor(test_config_path: str | None = None, *, wait_s: float = 0.0, run_handle: str | None = None) -> JsonObject:
    """Run a plan to its end and answer with the report, for this working directory.

    The command's whole contribution is which configuration the run is bound to:
    an operator at a shell means the project they are standing in, and the MCP
    tools mean the one their server was started on. What a run then is lives in
    `reactorrun.py`, where both frontends read it from.
    """
    return run_plan(load_authoritative_config(Path.cwd()), test_config_path, wait_s=wait_s, run_handle=run_handle)


def init_next_steps(available_com_ports: JsonObject, config_path: Path, *, narrowed: list[str] | None = None, drives_hardware: bool = True) -> list[str]:
    granted_step = (
        "Every permission in this file is true — probing, flashing, resetting, and serial and CAN writes — except "
        "allow_raw_debugger_commands and allow_mass_erase, which are false so that flashing works. Read the "
        "permissions blocks and decide which of the rest this bench should not have."
        if not narrowed
        else "Every permission in this file is true — probing, flashing, resetting, and serial and CAN writes — except "
        "allow_raw_debugger_commands and allow_mass_erase, which are false so that flashing works, and the ones your "
        "project profile set to false (" + ", ".join(narrowed) + "). Read the permissions blocks and decide which of "
        "the rest this bench should not have."
    )
    next_steps = [
        f"Review the config at {config_path}. Set {CONFIG_ENV} only when an explicit absolute-path override is needed.",
        "Edit target.name and target.controller for your board.",
    ]
    if drives_hardware:
        next_steps.append("Check interface_cfg and target_cfg on your debuggers entry against your OpenOCD installation.")
    else:
        # The permissions are open and the entry still drives nothing, and those
        # are two different facts. The starter entry names no toolchain, and
        # nothing resolves one for it: an entry nobody has configured stays inert
        # rather than picking up whatever `openocd` happens to be on PATH.
        next_steps.append(
            "Your debuggers entry names no toolchain yet, so it drives no board however its permissions read. Set "
            "`executable` to the absolute path of your OpenOCD, STM32CubeProgrammer or pyOCD binary; for OpenOCD keep "
            "interface_cfg and target_cfg as OpenOCD script names such as `interface/stlink.cfg`, or give them "
            "absolute paths to scripts outside the workspace. `agentic-hil doctor` checks the entry from the moment it "
            "names one."
        )
    next_steps.append(granted_step)
    next_steps.extend([
        "You can also just tell the agent: over MCP it may write false into any permission here and can write no other "
        f"value, so nothing it does through `project_config_set` widens this file. `{CONFIG_REOPEN_COMMAND}` is yours "
        "and regenerates the file with the same defaults again.",
        "Leave allow_raw_debugger_commands and allow_mass_erase alone unless something outside these tools needs the "
        "probe that way: validated flashing and unrestricted debugger access are mutually exclusive policies, so while "
        "either is true on a probe, flash_firmware on that probe is refused. Neither has a tool behind it here, so "
        "turning one on costs you flashing and buys nothing.",
        "If multiple debug probes are connected, give each debuggers entry the full unique id of its own probe; run `agentic-hil debugger-probes` to list them (OpenOCD cannot enumerate — read the serial off the probe). Test-reactor plan steps then address a board by its name; the MCP tools require exactly one configured probe.",
    ])
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
    # The same boundary the user-level files are checked against: from home or
    # above it the launcher a package manager just installed under `~/.local/bin`
    # would otherwise be refused for coming "from the workspace", which is the
    # one thing agent-install is there to register.
    return trusted_persistent_executable(command, workspace=_workspace_boundary(), disallowed_roots=_mcp_cache_roots())


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
            # Whatever the refusal knew about this candidate travels with it: the
            # component that stopped the walk, its mode and its owner. A caller
            # holding a path and a sentence has to go and stat the chain itself
            # to find out which directory to fix.
            entry: JsonObject = {"path": str(candidate), "reason": str(error)}
            if isinstance(error, ConfigError):
                entry["error_type"] = error.error_type
                entry.update({key: value for key, value in error.details.items() if key not in entry})
            rejected.append(entry)
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


def _superseded_claude_code_deny_rules(config_path: Path, state_root: Path) -> set[str]:
    """What earlier releases wrote for *these* trees and this one has to take back.

    Earlier releases built both rules straight from the absolute path: a
    `Write(...)` that Claude Code never consults, and an `Edit(...)` whose single
    leading slash anchored it under `~/.claude` instead of at the filesystem root.
    The first is loud — a yellow warning at every start — and the
    second is silent, which is worse: it reads as protection and is not.

    Identifying them needs no heuristic. The globs are derived from this
    project's config path and state root, so a rule is ours exactly when its text
    is one of these few strings. An operator's own `Write(...)` or `Edit(...)`
    rule names some other path and is therefore never one of them.

    Both are replaced in the same run, which is what makes taking them back safe
    here and what `_abandoned_claude_code_deny_rule` cannot assume: it answers
    for trees no configuration names any more, where there is nothing to write
    back.
    """
    return {f"{form}({glob})" for form in ("Edit", "Write") for glob in _protected_write_globs(config_path, state_root)}


_DENIED_TREE = re.compile(r"^(?:Edit|Write)\((?P<tree>.+)/\*\*\)$")


def _deny_pattern_spellings(tree: PurePath) -> tuple[str, ...]:
    """Every text this tool has written for one directory.

    The plain POSIX path is what releases up to 0.7.0 wrote; the `//`-anchored
    form is what the host actually resolves against the filesystem root, and what
    is written now. A rule in either spelling is this tool's, and has to stay
    recognisable as this tool's after the tree it names is gone.
    """
    return (tree.as_posix(), f"/{_posix_filesystem_path(tree)}")


def _deny_tree_key(tree: str) -> str:
    """Compare two deny patterns the way the filesystem underneath them compares.

    `os.path.normcase` is the usual spelling of this and would also rewrite the
    separators; these are POSIX texts on both platforms, so only its case half
    applies.
    """
    return tree.lower() if os.name == "nt" else tree


def _tool_written_deny_tree(rule: str) -> str | None:
    """The tree a deny rule protects, when the rule is one this tool wrote.

    Provenance without a marker. A Claude Code deny rule is a bare string in a
    file another program owns, with nowhere to carry a comment or a version, so
    what identifies ours is where it points: `Edit(<tree>/**)` (or the superseded
    `Write` form) for a `<tree>` at or under one of the per-user roots this tool
    creates for itself. Nothing but Agentic HIL puts a directory there, so a rule
    about one is Agentic HIL's own, whichever release wrote it and whichever
    `state_root` the configuration named at the time. That is the whole of the
    claim: an operator's rule about their own repository, their own home, or a
    directory that merely reads like ours (`~/work/agentic-hil`) is not under any
    of those roots and is not ours.

    The shape has to match too. `/**` is the only suffix this tool has ever
    written, so `Edit(<state_root>)` and `Write(<config directory>/*.yaml)` stay
    the operator's even inside our own roots, which is what #201's pinned test
    asks for. A pattern that navigates back out of the tree it starts in is not
    one of ours either, whatever it starts with.

    A tree outside those roots (a config location `AGENTIC_HIL_CONFIG` names, a
    `state_root` an operator put on another volume) is never claimed. That half
    still needs the exact text of the current configuration, and a rule for a
    location like that which the configuration has since left stays behind: it
    cannot be told from the operator's own rule for the same directory, and the
    invariant that this never removes an operator's rule outranks the tidying.
    """
    match = _DENIED_TREE.match(rule)
    if match is None or ".." in match.group("tree").split("/"):
        return None
    tree = _deny_tree_key(match.group("tree"))
    owned = (_deny_tree_key(spelling) for root in tool_owned_user_roots() for spelling in _deny_pattern_spellings(root))
    return tree if any(tree == root or tree.startswith(f"{root}/") for root in owned) else None


def _protected_trees_still_wanted(config_path: Path, state_root: Path) -> set[str] | None:
    """Every tree a configuration of this user's still asks to have denied.

    The pair this run was given is only part of the answer. `init --agent` runs
    per project and writes a rule for that project's own configuration directory,
    so the rules of every other project this user set up sit in the same deny
    list; reading the set as "this run's two trees" would make each project's
    setup take back the previous project's protection.

    `None` means the answer could not be established, and then nothing is taken
    back at all. One unreadable project configuration is not a reason to drop
    another project's rule.

    What it cannot see is a project bound by `AGENTIC_HIL_CONFIG`: that
    configuration is wherever the operator put it and nothing on disk records
    where. Its configuration directory is outside these roots and so is never
    claimed either way, but a `state_root` of its own that does lie inside them
    is kept only while some visible project names that tree as well. It is the
    one case where a rule can be taken back from a bench that still wants it, and
    that project's next `init --agent` writes it again.
    """
    wanted = {config_path.parent, state_root}
    directory = project_config_directory()
    if directory.is_dir():
        try:
            entries = list(directory.iterdir())
        except OSError:
            return None
        for entry in entries:
            candidate = entry / PROJECT_CONFIG_FILENAME
            if not candidate.is_file():
                continue  # a leftover directory with no configuration in it asks for nothing
            configured = _configured_state_root(candidate)
            if configured is None:
                return None
            wanted |= {entry, configured}
    return {_deny_tree_key(spelling) for tree in wanted for spelling in _deny_pattern_spellings(tree)}


def _configured_state_root(config_path: Path) -> Path | None:
    """The `state_root` one project's configuration names, or None if unreadable.

    A plain read rather than `load_config`: this needs one string out of a file
    that belongs to another project, and a full load would refuse that file for
    reasons which have nothing to do with the question (a probe that is not
    attached, a workspace that has moved, a permission shape from another
    release). Normalised the way the loader normalises it, so the answer is the
    text the rule for that project was written from.

    Nothing is trusted out of this file. Its only effect is to *keep* a deny
    rule, so a document that lies about its state root buys whoever wrote it a
    rule that stays.
    """
    try:
        document = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return None
    value = document.get("state_root") if isinstance(document, dict) else None
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        requested = Path(value).expanduser()
    except (OSError, ValueError, RuntimeError):
        return None
    # The loader refuses a relative one, so a document carrying one names no tree
    # that could have produced a rule, and resolving it here against this
    # process's working directory would invent one.
    return absolute_without_symlinks(requested) if requested.is_absolute() else None


def _abandoned_claude_code_deny_rule(rule: str, wanted: set[str] | None) -> bool:
    """A rule this tool wrote for a tree no configuration of this user's names.

    It denies writes on a tree the bench has left, which is inert where the tree
    is merely unused and confusing where it has been deleted; #206 was reported
    from an uninstall that left two of them pointing at directories that were
    gone. Removing one takes protection off nothing: what it covered is either
    not this bench's any more, or not there.
    """
    if wanted is None:
        return False
    tree = _tool_written_deny_tree(rule)
    return tree is not None and tree not in wanted


_OPENCODE_UNRESTRICTED = "Agentic HIL writes no write restriction for opencode; whether its file tools may reach the authoritative config and state root is the operator's to set, in ~/.config/opencode/opencode.json."


def _stale_opencode_deny_patterns(config_path: Path, state_root: Path) -> set[str]:
    """The absolute `permission.edit` patterns earlier releases wrote.

    They match nothing at all. opencode compares that key against the
    worktree-relative path, established from its source rather than from its
    documentation, which does not say: `write.ts`, `edit.ts` and
    `apply_patch.ts` ask with `patterns: [path.relative(instance.worktree,
    filepath)]`, `evaluate` in `permission/index.ts` matches the configured
    pattern against that subject, and `Wildcard.match` anchors it `^...$`. Both
    trees are required to live outside the workspace, so the subject always reads
    `../../.config/agentic-hil/...` and nothing absolute can equal it.

    They are taken back rather than corrected, because Agentic HIL no longer
    writes a restriction for this host at all — see `restrict_agent_write_access`.
    A rule that reads as protection and is not is worse than none, which is the
    same silent failure over again.

    Identifying them needs the same argument as
    `_superseded_claude_code_deny_rules`: the text is derived from this project's
    own paths, so an operator's own pattern is never one of them, and one of ours
    they have since set to something other than `deny` is theirs now and stays.

    The namespace recognition #206 added for Claude Code is deliberately not
    repeated here. There it takes back a live denial on a tree the bench has
    left; here every absolute pattern matches nothing whatever tree it names, so
    one for an abandoned tree denies exactly as little as one for the current
    tree, and reaching further into a file this tool no longer writes to would
    buy that nothing.
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

    It refreshes rather than only adds. Every run takes back the rules this tool
    wrote that no configuration of this user's still asks for, so a `state_root`
    an operator moved away from, and a project that was uninstalled, stop leaving
    a denial behind on a tree nothing uses. Which rules are this tool's own is
    `_tool_written_deny_tree`, and which are still wanted is
    `_protected_trees_still_wanted`; between them the operator's own rules are
    never touched, and neither is another project's.

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
        superseded = _superseded_claude_code_deny_rules(config_path, state_root)
        wanted = _protected_trees_still_wanted(config_path, state_root)
        # `isinstance` first: the list belongs to another program, and an entry
        # that is not a string is not a rule anybody wrote here. Membership of a
        # set is also where an unhashable one raised `TypeError` out of a
        # hand-edited file.
        removed = [rule for rule in deny if isinstance(rule, str) and (rule in superseded or _abandoned_claude_code_deny_rule(rule, wanted))]
        if removed:
            taken_back = set(removed)
            deny = permissions["deny"] = [rule for rule in deny if rule not in taken_back]
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
    # On the agent rather than on `added`: since #206 a claude-code run that takes
    # back a rule for a tree no configuration names any more has nothing left to
    # add, and reading that as the opencode case reported the opposite of what it
    # had just done.
    if agent_id == "claude-code":
        summary = f"{agent_id} will refuse its own write tools on the authoritative config and state root."
        if removed:
            summary += " Deny rules an earlier setup wrote that this one replaces or no longer needs were dropped."
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

    Run from home or above it there is no workspace (`_workspace_boundary`), so
    nothing is workspace-local and the trust check decides alone. Reading every
    path under home as workspace-local there would hand an entry an operator
    installed system-wide to whatever this run found.
    """
    if not isinstance(configured, str):
        return False
    path = Path(configured).expanduser()
    if not path.is_absolute():
        return False
    with suppress(OSError, ValueError):
        workspace = _workspace_boundary()
        if workspace is not None and is_path_within_frozen(path, workspace):
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
    but is not a JSON object (so callers never clobber unparseable config).

    Bytes that are not UTF-8 are the same "not a JSON object" as bytes that
    parse to something else: no caller here can act on either, and every caller
    already treats None as a `config_invalid`/`agent_permissions_unreadable`
    refusal that leaves the file untouched. Caught once, here, rather than
    separately in each caller that reaches this: a caller that forgot to catch
    it would otherwise let the decode error out as an internal error instead of
    that structured refusal.
    """
    try:
        text = secure_optional_read_text(path)
    except UnicodeDecodeError:
        return None
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


def config_reload(config_path: str | None = None) -> JsonObject:
    """What a running MCP server's description reload would take from this file.

    The operator's half of `project_config_reload_description`, and it is a
    check rather than an act, because there is nothing here to act on: a command
    line loads this file fresh every time it runs and has never had a stale
    description to replace. The server that has one is the agent host's, in
    another process, and no command can reach into it.

    What it is for is the moment before that call. An operator who has just
    written a board into the file wants to know that the file loads, that the
    entry is where the reload will look for it, and which of their edits the
    reload will *not* take — and every one of those questions is answered by
    running the real merge here, against the same load the server performs. The
    same refusals come out of it too: a file that will not load is refused here
    with the message the server would give, before an agent is asked to make a
    call that cannot succeed.
    """
    try:
        config = load_cli_authoritative_config(config_path)
    except ConfigError as error:
        return {"tool": PROJECT_CONFIG_RELOAD, **error.to_dict(), "scope": "preview"}
    # Both sides of the merge are this file, because that is what a command line
    # has: the reload runs, finds the description it already holds, and reports
    # the split. It is the split that is being asked for.
    _, result = reload_description(config)
    if result.get("ok") is not True:
        return {**result, "scope": "preview"}
    return {
        **result,
        "scope": "preview",
        "summary": (
            "This configuration loads. A running MCP server calling `project_config_reload_description` would take the "
            f"{len(RELOADED_SECTIONS)} device sections below from it and would leave every other section, and every "
            "permission in it, for a restart. Nothing was changed here: a command line reads this file fresh on every "
            "run and has no loaded description to replace."
        ),
        "would_reload": {section: _section_preview(config, section) for section in RELOADED_SECTIONS},
        "next_steps": [
            "Ask the agent to call `project_config_reload_description` if a device below is the one you just wrote.",
            "A permission you changed is not in that call's scope. Restart the MCP server for it — the agent host's "
            "server for this workspace, not this command line.",
            f"Sections a reload leaves alone: {', '.join(section for section, _ in NOT_RELOADED_SECTIONS)}.",
        ],
    }


def _doctor_debugger_scripts(entry: DebuggerConfig) -> JsonObject:
    """Say what each OpenOCD script field is, rather than what it is not.

    `interface/stlink.cfg` names no file on this host and is not meant to: it is
    the name OpenOCD resolves against its own script tree. Reported as a missing
    file it reads as a broken bench, and the first answer an install found for
    that reading was to fabricate the file under `/tmp` and point the
    authoritative configuration there. So the report labels the kind, and only a
    value that claims to be a path on this host is answered with whether it is
    one.
    """
    report: JsonObject = {}
    for field, value in (("interface_cfg", entry.interface_cfg), ("target_cfg", entry.target_cfg)):
        kind = openocd_script_kind(value)
        detail: JsonObject = {"value": value, "kind": kind}
        if kind == OPENOCD_SCRIPT_SEARCH_NAME:
            detail["resolved_by"] = "openocd"
            detail["note"] = "An OpenOCD search name, resolved against OpenOCD's own script path when it runs. It does not name a file on this host and does not have to exist here."
        else:
            detail["exists"] = Path(value).is_file()
        report[field] = detail
    return report


def _section_preview(config: AgenticHILConfig, section: str) -> object:
    if section == "target":
        return asdict(config.target)
    return sorted(getattr(config, section))


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
    # and `debugger_drives_hardware` is literally the set config load validated —
    # which since 0.8.0 excludes the starter entry `init` writes with
    # every permission granted and no toolchain named, and includes every entry
    # that has one, whatever its scripts looked like before it did. It takes the
    # configuration too, because under version 2 an entry with no mutation grant
    # at all is still reachable — read-free — and an entry `doctor` skips is an
    # entry nothing validated.
    probed = {name: _doctor_probe_check(config, name) for name, entry in config.debuggers.items() if debugger_drives_hardware(config, entry)}
    checks = {name: result for name, (result, _) in probed.items()}
    target_support = {name: support for name, (_, support) in probed.items()}
    checked = [result for result in checks.values() if result.get("skipped") is not True]
    debugger_info = next(iter(checks.values()), None) or {
        "ok": True,
        "tool": "debugger_info",
        "skipped": True,
        "summary": "Debugger check skipped: no configured debugger pins a toolchain, so there is nothing to check yet. A generated configuration already grants every permission it can, so what is missing is the toolchain, not a grant: set `debuggers.<name>.executable` to your OpenOCD, STM32CubeProgrammer or pyOCD binary. For OpenOCD its two scripts are either OpenOCD's own script names, which it resolves itself, or absolute paths outside the workspace.",
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
    # Not a failure. A configuration whose devices are identified by a name the
    # host can reassign still works, and saying so is exactly why it is named
    # here rather than refused at load: `doctor` is where an operator looks
    # before a bench misbehaves, and the warning says what to run to fix it.
    identity_warnings = config_devices(config).warnings()
    # The other thing that is already wrong before anything misbehaves, and which
    # nothing said until now: an installation that lost the extra its
    # own configuration needs. `uv tool install --upgrade` replaces the recorded
    # requirement, so a bench installed as `agentic-hil[can]` came back without
    # python-can, and the first `can_session_start` — hours later, in a result
    # about a bus — was what found out. Named here with the exact reinstall line,
    # for the same reason the identity warning is: this is where an operator
    # looks first, and a warning that does not carry the command is a search.
    missing_extras = missing_configured_extras(config)
    if missing_extras is not None:
        identity_warnings = [*identity_warnings, f"{missing_extras['summary']} Reinstall with: {missing_extras['reinstall_command']}"]
    if identity_warnings:
        report["warnings"] = identity_warnings
    return {
        **report,
        "config_path": config.config_path,
        **({"missing_extras": missing_extras} if missing_extras is not None else {}),
        "installation": _doctor_installation_report(),
        "mcp": _doctor_mcp_report(),
        "target": {"name": config.target.name, "controller": config.target.controller},
        "debuggers": {
            name: {
                "type": entry.type,
                "probe_id": entry.probe_id,
                "bound": name == config.debugger_id,
                "permissions": asdict(entry.permissions),
                **({"scripts": _doctor_debugger_scripts(entry)} if entry.type == "openocd" else {}),
                **({"check": checks[name]} if name in checks else {}),
                **({"target_support": target_support[name]} if name in target_support else {}),
            }
            for name, entry in config.debuggers.items()
        },
        "com_ports": {port_id: {"device": port.device, "baudrate": port.baudrate, "encoding": port.encoding, **port_identity_fields(config, port_id), "permissions": asdict(port.permissions)} for port_id, port in config.com_ports.items()},
        "can_buses": {bus_id: {"adapter": bus.adapter, "channel": bus.channel, "bitrate": bus.bitrate, "fd": bus.fd, "permissions": asdict(bus.permissions)} for bus_id, bus in config.can_buses.items()},
        "debugger": debugger_info,
    }


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
        # The refusal already knows which candidates it looked at, where they
        # were, and what was wrong with each. Passing on the summary alone left
        # an operator with "no trusted executable was found" and no path to act
        # on — a refusal handed over without its reasons, which is the one thing
        # this project's error line does not do.
        details = error.details if isinstance(error, ConfigError) else {}
        rejected = details.get("rejected_candidates")
        return {
            **report,
            "command": None,
            "persistent": False,
            **({"rejected_candidates": rejected} if isinstance(rejected, list) else {}),
            "summary": f"No trusted persistent executable to register yet: {summary}",
        }
    return {
        **report,
        "command": command,
        "persistent": True,
        "summary": "Register this exact path with 'agentic-hil setup --agent <agent>'; a transient runner or a bare PATH name is not a registration.",
    }


def bootstrap_probe_listing() -> JsonObject:
    """The probe listing a project with no configuration can still be given.

    `setup`'s bootstrap already enumerates probes before any configuration
    exists, with the fixed read-only command package code owns, so this reuses
    that discovery rather than growing a second one beside it. The answer says
    what it is: `source: bootstrap` and the backend that ran, so nobody reads it
    as the configured bench speaking.

    Refusing here was the wrong answer to the right question. The one moment an
    operator genuinely wants a probe listing with nothing else in place is right
    before the first `setup`: is the board visible, is there one of it, which
    serial. `config_file_not_found` withheld an answer this tool could already
    give, for a configuration the question does not need."""
    listed = enumerate_attached_probes()
    result: JsonObject = {
        "ok": listed["ok"],
        "tool": "debugger_probes_list",
        "source": "bootstrap",
        "backend": BOOTSTRAP_BACKEND,
        **{key: value for key, value in listed.items() if key not in {"ok", "tool", "backend"}},
    }
    if listed["ok"]:
        result["summary"] = (
            f"{len(listed['probes'])} connected debugger probe(s) detected by bootstrap discovery. This project has no "
            "authoritative configuration yet, so the fixed read-only setup commands answered and no configured "
            "debugger backend was involved."
        )
    result["next_step"] = (
        "Write this project's configuration with `agentic-hil setup --agent <agent>` from its root. After that this "
        "command answers through the configured backend instead."
    )
    return result


def debugger_probes() -> JsonObject:
    """Enumerate connected probes for every configured debugger that may probe.

    Probe discovery is how an operator finds the serial numbers a multi-board
    config needs, so it has to work in exactly the multi-probe project where no
    single debugger is bound. Each backend enumerates all attached probes, so
    binding one entry at a time is only about which toolchain to invoke.

    With no configuration at all, the answer comes from `setup`'s own bootstrap
    instead of a refusal; see `bootstrap_probe_listing`. Only the missing file
    takes that route. A configuration that is there and will not load is a
    different fact about a bench somebody has already set up, and it still
    refuses with what is wrong with it."""
    try:
        config = load_authoritative_config(Path.cwd())
    except ConfigError as error:
        if error.error_type != "config_file_not_found":
            raise
        return bootstrap_probe_listing()
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
        try:
            text = secure_optional_read_text(path)
        except UnicodeDecodeError:
            # Not text this project wrote, so not ours to manage: leave a
            # foreign legacy-named file untouched and keep uninstalling.
            continue
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
- Read and follow this skill when a request operates the connected target board through the configured bench - flashing, resetting, probing, debugging, UART or CAN traffic, firmware artifacts, hardware test runs - or sets up Agentic HIL itself: configuration, skill installation, MCP registration.
- Not for designing hardware (PCB layout, schematic capture, EDA, mechanical design) and not for firmware authoring that never touches a board.
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
