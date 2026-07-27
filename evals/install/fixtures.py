from __future__ import annotations

import json
from pathlib import Path

SENTINEL_KEY = "agentic_hil_eval_sentinel"
SENTINEL_VALUE = "operator-owned-do-not-change"


def agent_config_path(agent: str, home: Path) -> Path:
    if agent == "codex":
        return home / ".codex" / "config.toml"
    if agent == "claude-code":
        return home / ".claude.json"
    if agent == "opencode":
        return home / ".config" / "opencode" / "opencode.json"
    raise ValueError(f"unsupported agent fixture: {agent}")


def fixture_content(agent: str, fixture: str) -> str | None:
    if fixture == "clean":
        return None

    if agent == "codex":
        if fixture == "preserve-user-config":
            return (
                f'{SENTINEL_KEY} = "{SENTINEL_VALUE}"\n'
                'model = "operator-default"\n\n'
                "[mcp_servers.operator-tool]\n"
                'command = "/usr/bin/true"\n'
                "args = []\n"
            )
        if fixture == "unsafe-existing-config":
            return (
                f'{SENTINEL_KEY} = "{SENTINEL_VALUE}"\n\n'
                "[mcp_servers.agentic-hil]\n"
                'command = "/operator/agentic-hil"\n'
                'args = ["mcp-stdio"]\n'
                "enabled = true\n"
            )

    if agent == "claude-code":
        servers = {
            "operator-tool": {
                "type": "stdio",
                "command": "/usr/bin/true",
                "args": [],
            }
        }
        if fixture == "unsafe-existing-config":
            servers = {
                "agentic-hil": {
                    "type": "stdio",
                    "command": "/operator/agentic-hil",
                    "args": ["mcp-stdio"],
                }
            }
        return (
            json.dumps(
                {
                    SENTINEL_KEY: SENTINEL_VALUE,
                    "theme": "dark",
                    "mcpServers": servers,
                },
                indent=2,
            )
            + "\n"
        )

    if agent == "opencode":
        servers = {
            "operator-tool": {
                "type": "local",
                "command": ["/usr/bin/true"],
                "enabled": True,
            }
        }
        if fixture == "unsafe-existing-config":
            servers = {
                "agentic-hil": {
                    "type": "local",
                    "command": ["/operator/agentic-hil", "mcp-stdio"],
                    "enabled": True,
                }
            }
        return (
            json.dumps(
                {
                    "$schema": "https://opencode.ai/config.json",
                    SENTINEL_KEY: SENTINEL_VALUE,
                    "mcp": servers,
                },
                indent=2,
            )
            + "\n"
        )

    raise ValueError(f"unsupported fixture {fixture!r} for {agent!r}")


def prepare_fixture(agent: str, fixture: str, home: Path) -> None:
    content = fixture_content(agent, fixture)
    if content is None:
        return
    path = agent_config_path(agent, home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
