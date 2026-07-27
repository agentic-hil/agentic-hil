from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AgentAdapter:
    id: str
    aliases: tuple[str, ...]
    auth_environment: tuple[str, ...]


_ADAPTERS = (
    AgentAdapter(
        id="codex",
        aliases=("codex", "codex-cli", "openai-codex"),
        auth_environment=("CODEX_ACCESS_TOKEN", "CODEX_API_KEY", "OPENAI_API_KEY"),
    ),
    AgentAdapter(
        id="claude-code",
        aliases=("claude-code", "claude", "claude_code"),
        auth_environment=("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"),
    ),
    AgentAdapter(
        id="opencode",
        aliases=("opencode", "open-code"),
        auth_environment=(
            "ANTHROPIC_API_KEY",
            "OPENAI_API_KEY",
            "GOOGLE_GENERATIVE_AI_API_KEY",
            "GROQ_API_KEY",
            "OPENROUTER_API_KEY",
            "AWS_ACCESS_KEY_ID",
            "AWS_SECRET_ACCESS_KEY",
            "AWS_SESSION_TOKEN",
            "AWS_REGION",
        ),
    ),
)


def adapter_for(name: str) -> AgentAdapter:
    normalized = name.strip().lower()
    for adapter in _ADAPTERS:
        if normalized in adapter.aliases:
            return adapter
    allowed = ", ".join(adapter.id for adapter in _ADAPTERS)
    raise ValueError(f"unsupported agent {name!r}; expected one of: {allowed}")


def build_agent_command(agent: str, model: str, prompt: str, *, isolated: bool = True) -> list[str]:
    """Build a non-interactive command while keeping agent and model separate.

    An isolated session ignores the agent CLI's user configuration, which keeps
    the installation phase hermetic. A session that has to exercise what setup
    registered — the MCP server and the skill — must read that configuration,
    so it runs without the isolating flags.
    """
    adapter = adapter_for(agent)
    if not model.strip():
        raise ValueError("model must not be empty")

    if adapter.id == "codex":
        return [
            "codex",
            "exec",
            "--dangerously-bypass-approvals-and-sandbox",
            "--skip-git-repo-check",
            "--ephemeral",
            *(["--ignore-user-config"] if isolated else []),
            "--json",
            "--model",
            model,
            prompt,
        ]
    if adapter.id == "claude-code":
        return [
            "claude",
            "--print",
            "--dangerously-skip-permissions",
            "--no-session-persistence",
            *(["--strict-mcp-config"] if isolated else []),
            "--output-format",
            "stream-json",
            # Claude Code rejects --print with stream-json unless it is verbose.
            "--verbose",
            "--model",
            model,
            prompt,
        ]
    return [
        "opencode",
        "run",
        "--auto",
        "--format",
        "json",
        "--model",
        model,
        prompt,
    ]
