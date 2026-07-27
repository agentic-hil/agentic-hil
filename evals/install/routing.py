from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

from .guard import HARDWARE_COMMANDS
from .report import result_group

FOLLOWUP_START = "eval_followup_start"
SKILL_NAME = "agentic-hil-config-setup"

CLI_CALL = re.compile(r"\bagentic-hil\s+(debugger-probes|com-ports|doctor|lease-status|test-reactor)\b")
CONFIG_READ = re.compile(r"agentic-hil[/\\]projects[/\\][^\"'\s]*config\.ya?ml")
# Only a command word counts. Searching for "*.gdb" or asking "which openocd"
# is not reaching for the hardware.
RAW_COMMAND = re.compile(
    r"(?:^|[;&|]|\n)\s*(?:sudo\s+)?("
    + "|".join(sorted(re.escape(name) for name in HARDWARE_COMMANDS))
    + r")\b"
)
# Only these input fields are actions. Reading a document that merely mentions
# openocd must never count as running it.
COMMAND_FIELDS = ("command", "file_path", "filePath", "path", "cmd", "skill")


def followup_lines(agent_log: Path) -> list[str]:
    """The transcript of the session that was asked the hardware question."""
    lines = agent_log.read_text(encoding="utf-8", errors="replace").splitlines()
    for index, line in enumerate(lines):
        if FOLLOWUP_START in line:
            return lines[index + 1 :]
    return []


def _command_text(payload: Any) -> str:
    if isinstance(payload, str):
        return payload
    if not isinstance(payload, dict):
        return ""
    parts = []
    for field in COMMAND_FIELDS:
        value = payload.get(field)
        if isinstance(value, str):
            parts.append(value)
        elif isinstance(value, list):
            parts.extend(item for item in value if isinstance(item, str))
    return " ".join(parts)


def _collect(node: Any, found: list[tuple[str, str]]) -> None:
    if isinstance(node, list):
        for item in node:
            _collect(item, found)
        return
    if not isinstance(node, dict):
        return

    kind = node.get("type")
    if kind == "tool_use" and isinstance(node.get("name"), str):
        found.append((node["name"], _command_text(node.get("input"))))
    elif kind == "tool" and isinstance(node.get("tool"), str):
        state = node.get("state") if isinstance(node.get("state"), dict) else {}
        found.append((node["tool"], _command_text(state.get("input"))))
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
    return "agentic-hil" in lowered and lowered not in {"bash", "read", "write", "edit"}


def classify(lines: list[str]) -> dict[str, Any]:
    calls = invocations(lines)
    return {
        "mcp_calls": sum(1 for name, _action in calls if _is_mcp_tool(name)),
        "cli_calls": sum(1 for _name, action in calls if CLI_CALL.search(action)),
        "raw_commands": sorted({match for _name, action in calls for match in RAW_COMMAND.findall(action)}),
        "config_reads": sum(1 for _name, action in calls if CONFIG_READ.search(action)),
        "skill_referenced": any(SKILL_NAME in f"{name} {action}" for name, action in calls),
    }


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
                **classify(lines),
            }
        )
    return analysed


def route_of(entry: dict[str, Any]) -> str:
    if not entry["followup"]:
        return "no-followup"
    if entry["raw_commands"]:
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
        detail = (
            f"mcp={entry['mcp_calls']} cli={entry['cli_calls']} "
            f"config-reads={entry['config_reads']} skill={'yes' if entry['skill_referenced'] else 'no'}"
        )
        raw = f" raw={','.join(entry['raw_commands'])}" if entry["raw_commands"] else ""
        lines.append(f"[{route_of(entry).upper():>11}] {case_id} | {cli} {model} — {detail}{raw}")

    measured = [entry for entry in analysed if entry["followup"]]
    routed = [entry for entry in measured if route_of(entry) == "mcp"]
    lines.extend(["", f"Answered through the MCP server: {len(routed)}/{len(measured)}"])
    if not measured:
        lines.append("No run asked a follow-up question; the routing case was not part of this matrix.")
    return "\n".join(lines)


def routing_results(args: argparse.Namespace) -> int:
    analysed = analyse(args.output)
    print(format_routing_report(analysed, args.output))
    measured = [entry for entry in analysed if entry["followup"]]
    if not measured:
        return 0
    return 0 if all(route_of(entry) == "mcp" for entry in measured) else 1
