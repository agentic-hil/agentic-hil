from __future__ import annotations

import argparse
import json
import re
import shlex
from pathlib import Path, PurePosixPath
from typing import Any

from .fixtures import HARDWARE_MAKE_TARGETS
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
# for anything else (an rg pattern, a grep alternation) is not running it.
SHELLS = frozenset({"bash", "sh", "dash", "zsh", "ksh"})
MAKE = "make"
# Newline is not here: lines are split before tokenising, because shlex treats a
# newline as whitespace and would never emit it as a separator token.
SEPARATORS = ";&|()"
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
    commands: list[list[str]] = []
    # shlex treats a newline as whitespace and never emits it as a separator, so
    # a script's second line would otherwise be folded into its first command.
    for line in action.splitlines() or [action]:
        lexer = shlex.shlex(line, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        current: list[str] = []
        try:
            for token in lexer:
                if token and all(character in SEPARATORS for character in token):
                    commands.append(current)
                    current = []
                else:
                    current.append(token)
        except ValueError:
            # An unbalanced quote means the line was cut off, or it continues on
            # the next one. What was read before that still counts.
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
        elif name == MAKE:
            # A firmware repository's own way to the bench. The build target is
            # not one: only the targets the fixture's Makefile drives hardware
            # from count, and they are named where that Makefile is written.
            found |= {f"{MAKE} {word}" for word in words[1:] if word in HARDWARE_MAKE_TARGETS}
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
        # A question with several parts needs several tools. Counting distinct
        # ones separates following a route from calling the same tool twice.
        "mcp_tools": sorted({name for name, _action in calls if _is_mcp_tool(name)}),
        "cli_calls": sum(1 for _name, action in calls if CLI_CALL.search(action)),
        "raw_commands": sorted({name for _tool, action in calls for name in raw_commands(action)}),
        "config_reads": sum(1 for _name, action in calls if CONFIG_READ.search(action)),
        "skill_referenced": any(_loads_skill(name, action) for name, action in calls),
    }


GUARD_CHECK = "forbidden PATH guard not triggered"
# The same guard log, read only over the session that was asked the hardware
# question. The whole-run check keeps its own meaning and its own name; this one
# is what a routing verdict may be built on, because a raw command reached for
# while installing says nothing about how the question was answered.
FOLLOWUP_GUARD_CHECK = "hardware answered without raw commands"
# Whether the run even had the server it is being asked about. A run whose
# registration never landed cannot route through MCP, and counting it as a run
# that chose not to is where the headline rate came from.
LAUNCHER_CHECK = "MCP registration uses trusted launcher"


def _check_passed(result: dict[str, Any], name: str) -> bool | None:
    for check in result.get("checks") or []:
        if isinstance(check, dict) and check.get("name") == name:
            return check.get("ok") is True
    return None


def guard_triggered(result: dict[str, Any]) -> bool | None:
    """Whether a shadowed hardware command ran in the measured session.

    The transcript only shows what the agent typed. The PATH guard records what
    the kernel was asked to execute, which is the evidence that decides. None
    means the run carries no guard verdict for that session: absent evidence,
    which must not be read as evidence of absence.
    """
    passed = _check_passed(result, FOLLOWUP_GUARD_CHECK)
    return None if passed is None else not passed


def mcp_available(result: dict[str, Any]) -> bool | None:
    """Whether this run ended with the MCP server registered to be reached."""
    return _check_passed(result, LAUNCHER_CHECK)


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
                "mcp_available": mcp_available(result),
                **classify(lines),
            }
        )
    return analysed


def route_of(entry: dict[str, Any]) -> str:
    if not entry["followup"]:
        return "no-followup"
    # A transcript can only show intent. The guard records what the container
    # was asked to execute, so it decides in both directions: it clears a
    # hardware name that was only quoted into a search pattern, and it condemns
    # a run that reached the hardware by a route the transcript did not spell
    # out. Absent evidence decides nothing.
    guard = entry.get("guard_triggered")
    if guard:
        return "raw"
    if entry["raw_commands"] and guard is None:
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
        case_id, cli, model, effort = entry["group"]
        # In the control arm the skill is verified absent, so a reference to it
        # is the agent going to look, not the agent loading it.
        skill = "no" if not entry["skill_referenced"] else "looked-for" if is_control(entry) else "yes"
        detail = (
            f"mcp={entry['mcp_calls']} tools={len(entry.get('mcp_tools', []))} "
            f"cli={entry['cli_calls']} config-reads={entry['config_reads']} skill={skill}"
        )
        raw = f" raw={','.join(entry['raw_commands'])}" if entry["raw_commands"] else ""
        if raw and entry.get("guard_triggered") is False:
            raw += " (named, never executed)"
        if not raw and entry.get("guard_triggered"):
            raw = " raw=recorded by the PATH guard, absent from the transcript"
        label = f"{cli} {model}" if effort == "default" else f"{cli} {model} ({effort})"
        lines.append(f"[{route_of(entry).upper():>11}] {case_id} | {label}: {detail}{raw}")

    measured = [entry for entry in analysed if entry["followup"]]
    # A run whose MCP registration never landed had no server to route through,
    # so counting it as a run that chose another way measures the install, not
    # the routing. Reported on its own line rather than dropped: how often the
    # install left no server is a result too.
    comparable = [entry for entry in measured if entry.get("mcp_available") is True]
    unregistered = [entry for entry in measured if entry.get("mcp_available") is not True]
    gated = [entry for entry in comparable if not is_control(entry)]
    control = [entry for entry in comparable if is_control(entry)]
    lines.extend(["", f"Answered through the MCP server: {_rate(gated)} with the skill installed"])
    if control:
        lines.append(f"{' ' * 33}{_rate(control)} with the skill uninstalled")
        lines.append(
            f"Distinct Agentic HIL tools per MCP-routed run: {_mean_tools(gated)} with, {_mean_tools(control)} without"
        )
        lines.append("The difference is what the skill adds over the tool descriptions alone.")
    if unregistered:
        lines.append(
            f"Left out of that comparison, no MCP registration to route through: {len(unregistered)} of "
            f"{len(measured)} measured runs; they answered {_rate(unregistered)} through MCP anyway"
        )
    if not measured:
        lines.append("No run asked a follow-up question; the routing case was not part of this matrix.")
    return "\n".join(lines)


def is_control(entry: dict[str, Any]) -> bool:
    return str(entry["group"][0]).endswith(CONTROL_SUFFIX)


def _mean_tools(entries: list[dict[str, Any]]) -> str:
    """How many distinct tools an MCP-routed answer took, and over how many runs.

    Averaged over every measured run instead, this mixes "how many tools does an
    answer need" with "how many answers used none at all", and reads as the first
    while measuring the second. The 2.7-against-3.5 claim was that mixture.
    """
    routed = [entry for entry in entries if route_of(entry) == "mcp"]
    if not routed:
        return "n/a (n=0)"
    mean = sum(len(entry.get("mcp_tools", [])) for entry in routed) / len(routed)
    return f"{mean:.1f} (n={len(routed)})"


def _rate(entries: list[dict[str, Any]]) -> str:
    routed = [entry for entry in entries if route_of(entry) == "mcp"]
    return f"{len(routed)}/{len(entries)}"


def routing_results(args: argparse.Namespace, analysed: list[dict[str, Any]] | None = None) -> int:
    analysed = analyse(args.output) if analysed is None else analysed
    print(format_routing_report(analysed, args.output))
    # Only the arm that has the skill is a gate. The control arm is the thing
    # being measured; a run of it that skips MCP is a result, not a regression.
    # A run with no MCP registration is not gated either: it had no server to
    # route through, which its own failed install check already reports.
    gated = [
        entry
        for entry in analysed
        if entry["followup"] and not is_control(entry) and entry.get("mcp_available") is True
    ]
    if not gated:
        return 0
    return 0 if all(route_of(entry) == "mcp" for entry in gated) else 1
