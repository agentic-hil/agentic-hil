from __future__ import annotations

import argparse
import json
import re
import shlex
from pathlib import Path, PurePosixPath
from typing import Any

from .guard import HARDWARE_COMMANDS
from .report import result_group

FOLLOWUP_START = "eval_followup_start"
SKILL_NAME = "agentic-hil"
# The control arm of the A/B measurement: the skill is uninstalled before the
# question is asked, so its runs report what the MCP tools achieve alone. They
# are measured, never gated.
CONTROL_SUFFIX = "-without-skill"

CLI_CALL = re.compile(r"\bagentic-hil\s+(debugger-probes|com-ports|doctor|lease-status|test-reactor)\b")
CONFIG_READ = re.compile(r"agentic-hil[/\\]projects[/\\][^\"'\s]*config\.ya?ml")
# Agent CLIs wrap their work in a shell, so a hardware command usually sits
# inside the quoted argument of one. Only these get looked into; quoting a name
# for anything else — an rg pattern, a grep alternation — is not running it.
SHELLS = frozenset({"bash", "sh", "dash", "zsh", "ksh"})
SEPARATORS = ";&|()\n"
ENVIRONMENT_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
# Only these input fields are actions. Reading a document that merely mentions
# openocd must never count as running it.
COMMAND_FIELDS = ("command", "file_path", "filePath", "path", "cmd", "skill")
# opencode's skill tool names what it loads in "name". That field is too generic
# to read everywhere, so it counts only for the tool whose argument it is.
SKILL_TOOL_FIELDS = (*COMMAND_FIELDS, "name")
SKILL_TOOLS = frozenset({"skill", "skills"})
# The skill shares its name with the MCP server, so a substring search would
# call every mcp__agentic-hil__* invocation a skill load. Only loading it counts:
# through the CLI's own skill tool, or by reading the installed file.
SKILL_PATH = re.compile(rf"skills[/\\]{re.escape(SKILL_NAME)}[/\\]SKILL\.md")


def _shell_commands(action: str) -> list[list[str]]:
    """Split a shell action into commands, with the shell's own quoting rules.

    A separator inside quotes is data, not a pipeline: "openocd|pyocd" is one
    ripgrep pattern, however much it looks like two commands.
    """
    lexer = shlex.shlex(action, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    commands: list[list[str]] = []
    current: list[str] = []
    try:
        for token in lexer:
            if token and all(character in SEPARATORS for character in token):
                commands.append(current)
                current = []
            else:
                current.append(token)
    except ValueError:
        # An unbalanced quote means the transcript cut the command off. What was
        # read before that still counts.
        pass
    commands.append(current)
    return [command for command in commands if command]


def raw_commands(action: str, depth: int = 0) -> set[str]:
    """The hardware commands this action runs, not the ones it merely names."""
    found: set[str] = set()
    for words in _shell_commands(action):
        while words and (words[0] == "sudo" or ENVIRONMENT_ASSIGNMENT.match(words[0])):
            words = words[1:]
        if not words:
            continue
        name = PurePosixPath(words[0].replace("\\", "/")).name
        if name in HARDWARE_COMMANDS:
            found.add(name)
        elif name in SHELLS and depth < 3:
            # `bash -lc "openocd -f board.cfg"` runs openocd: the first argument
            # that is not a flag is a command line of its own.
            for argument in words[1:]:
                if argument.startswith("-"):
                    continue
                found |= raw_commands(argument, depth + 1)
                break
    return found


def followup_lines(agent_log: Path) -> list[str]:
    """The transcript of the session that was asked the hardware question."""
    lines = agent_log.read_text(encoding="utf-8", errors="replace").splitlines()
    for index, line in enumerate(lines):
        if FOLLOWUP_START in line:
            return lines[index + 1 :]
    return []


def _command_text(payload: Any, fields: tuple[str, ...] = COMMAND_FIELDS) -> str:
    if isinstance(payload, str):
        return payload
    if not isinstance(payload, dict):
        return ""
    parts = []
    for field in fields:
        value = payload.get(field)
        if isinstance(value, str):
            parts.append(value)
        elif isinstance(value, list):
            parts.extend(item for item in value if isinstance(item, str))
    return " ".join(parts)


def _fields_for(tool_name: str) -> tuple[str, ...]:
    return SKILL_TOOL_FIELDS if tool_name.lower() in SKILL_TOOLS else COMMAND_FIELDS


def _collect(node: Any, found: list[tuple[str, str]]) -> None:
    if isinstance(node, list):
        for item in node:
            _collect(item, found)
        return
    if not isinstance(node, dict):
        return

    kind = node.get("type")
    if kind == "tool_use" and isinstance(node.get("name"), str):
        found.append((node["name"], _command_text(node.get("input"), _fields_for(node["name"]))))
    elif kind == "tool" and isinstance(node.get("tool"), str):
        state = node.get("state") if isinstance(node.get("state"), dict) else {}
        found.append((node["tool"], _command_text(state.get("input"), _fields_for(node["tool"]))))
    elif kind == "command_execution":
        found.append(("bash", _command_text(node)))
    elif kind == "mcp_tool_call":
        found.append((f"mcp__{node.get('server', '')}__{node.get('tool', '')}", ""))

    for value in node.values():
        _collect(value, found)


def invocations(lines: list[str]) -> list[tuple[str, str]]:
    """Every tool the agent invoked, as (tool name, action text) pairs."""
    found: list[tuple[str, str]] = []
    for line in lines:
        try:
            document = json.loads(line)
        except ValueError:
            continue
        _collect(document, found)
    # The same event is often echoed as started and completed.
    return list(dict.fromkeys(found))


def _is_mcp_tool(name: str) -> bool:
    lowered = name.lower()
    if lowered in SKILL_TOOLS or lowered in {"bash", "read", "write", "edit"}:
        return False
    return "agentic-hil" in lowered


def _loads_skill(name: str, action: str) -> bool:
    if name.lower() in SKILL_TOOLS and SKILL_NAME in action:
        return True
    return SKILL_PATH.search(action) is not None


def classify(lines: list[str]) -> dict[str, Any]:
    calls = invocations(lines)
    return {
        "mcp_calls": sum(1 for name, _action in calls if _is_mcp_tool(name)),
        "cli_calls": sum(1 for _name, action in calls if CLI_CALL.search(action)),
        "raw_commands": sorted({name for _tool, action in calls for name in raw_commands(action)}),
        "config_reads": sum(1 for _name, action in calls if CONFIG_READ.search(action)),
        "skill_referenced": any(_loads_skill(name, action) for name, action in calls),
    }


GUARD_CHECK = "forbidden PATH guard not triggered"


def guard_triggered(result: dict[str, Any]) -> bool:
    """Whether a shadowed hardware command actually ran, as the container saw it.

    The transcript only shows what the agent typed. The PATH guard records what
    the kernel was asked to execute, which is the evidence that decides.
    """
    for check in result.get("checks") or []:
        if isinstance(check, dict) and check.get("name") == GUARD_CHECK:
            return check.get("ok") is not True
    return False


def analyse(output_root: Path | str) -> list[dict[str, Any]]:
    """Describe how each run answered the hardware question."""
    root = Path(output_root)
    analysed = []
    for result_path in sorted(root.glob("*/result.json")):
        result = json.loads(result_path.read_text(encoding="utf-8-sig"))
        agent_log = result_path.parent / "agent.log"
        lines = followup_lines(agent_log) if agent_log.is_file() else []
        analysed.append(
            {
                "group": result_group(result),
                "status": str(result.get("status", "unknown")),
                "followup": bool(lines),
                "guard_triggered": guard_triggered(result),
                **classify(lines),
            }
        )
    return analysed


def route_of(entry: dict[str, Any]) -> str:
    if not entry["followup"]:
        return "no-followup"
    # A transcript can only show intent. Where the guard's verdict is available
    # it decides, so a hardware name quoted into a search pattern stays what it
    # is instead of being reported as a run command.
    if entry["raw_commands"] and entry.get("guard_triggered", True):
        return "raw"
    if entry["mcp_calls"]:
        return "mcp"
    if entry["cli_calls"]:
        return "cli"
    if entry["config_reads"]:
        return "config-file"
    return "none"


def format_routing_report(analysed: list[dict[str, Any]], output_root: Path | str) -> str:
    lines = [f"Firmware routing report: {Path(output_root)}", ""]
    for entry in analysed:
        case_id, cli, model = entry["group"]
        # In the control arm the skill is verified absent, so a reference to it
        # is the agent going to look, not the agent loading it.
        skill = "no" if not entry["skill_referenced"] else "looked-for" if is_control(entry) else "yes"
        detail = (
            f"mcp={entry['mcp_calls']} cli={entry['cli_calls']} "
            f"config-reads={entry['config_reads']} skill={skill}"
        )
        raw = f" raw={','.join(entry['raw_commands'])}" if entry["raw_commands"] else ""
        if raw and not entry.get("guard_triggered", True):
            raw += " (named, never executed)"
        lines.append(f"[{route_of(entry).upper():>11}] {case_id} | {cli} {model} — {detail}{raw}")

    measured = [entry for entry in analysed if entry["followup"]]
    gated = [entry for entry in measured if not is_control(entry)]
    control = [entry for entry in measured if is_control(entry)]
    lines.extend(["", f"Answered through the MCP server: {_rate(gated)} with the skill installed"])
    if control:
        lines.append(f"{' ' * 33}{_rate(control)} with the skill uninstalled")
        lines.append("The difference is what the skill adds over the tool descriptions alone.")
    if not measured:
        lines.append("No run asked a follow-up question; the routing case was not part of this matrix.")
    return "\n".join(lines)


def is_control(entry: dict[str, Any]) -> bool:
    return str(entry["group"][0]).endswith(CONTROL_SUFFIX)


def _rate(entries: list[dict[str, Any]]) -> str:
    routed = [entry for entry in entries if route_of(entry) == "mcp"]
    return f"{len(routed)}/{len(entries)}"


def routing_results(args: argparse.Namespace, analysed: list[dict[str, Any]] | None = None) -> int:
    analysed = analyse(args.output) if analysed is None else analysed
    print(format_routing_report(analysed, args.output))
    # Only the arm that has the skill is a gate. The control arm is the thing
    # being measured; a run of it that skips MCP is a result, not a regression.
    gated = [entry for entry in analysed if entry["followup"] and not is_control(entry)]
    if not gated:
        return 0
    return 0 if all(route_of(entry) == "mcp" for entry in gated) else 1
