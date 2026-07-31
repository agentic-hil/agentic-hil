from __future__ import annotations

from dataclasses import dataclass

# The intersection of what all three CLIs understand. Codex additionally accepts
# "ultra" and opencode "none"; admitting either would hand the other two a silent
# default and make a cross-agent comparison meaningless.
REASONING_EFFORTS = ("low", "medium", "high", "xhigh", "max")
# How each CLI is told, and the token that proves the pinned build still has it.
EFFORT_FLAGS = {"codex": "--config", "claude-code": "--effort", "opencode": "--variant"}


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


def build_agent_command(
    agent: str,
    model: str,
    prompt: str,
    *,
    isolated: bool = True,
    reasoning_effort: str | None = None,
) -> list[str]:
    """Build a non-interactive command while keeping agent and model separate.

    An isolated session ignores the agent CLI's user configuration, which keeps
    the installation phase hermetic. A session that has to exercise what setup
    registered — the MCP server and the skill — must read that configuration,
    so it runs without the isolating flags.

    The reasoning effort is deliberately outside those conditionals: both
    sessions must run at the same effort, or the measured session answers as a
    different model than the one that installed.
    """
    adapter = adapter_for(agent)
    if not model.strip():
        raise ValueError("model must not be empty")
    if reasoning_effort is not None and reasoning_effort not in REASONING_EFFORTS:
        allowed = ", ".join(REASONING_EFFORTS)
        raise ValueError(f"unsupported reasoning_effort {reasoning_effort!r}; expected one of: {allowed}")

    if adapter.id == "codex":
        return [
            "codex",
            "exec",
            "--dangerously-bypass-approvals-and-sandbox",
            "--skip-git-repo-check",
            "--ephemeral",
            *(["--ignore-user-config"] if isolated else []),
            # An override, not a config file: it applies whether or not
            # --ignore-user-config suppressed config.toml, which a file could
            # never do for the isolated session.
            *(["--config", f"model_reasoning_effort={reasoning_effort}"] if reasoning_effort else []),
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
            *(["--effort", reasoning_effort] if reasoning_effort else []),
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
        # A flag, not the OPENCODE_CONFIG file the entrypoint owns: the measured
        # session drops that file so it can see the registered MCP server, so a
        # config-file mechanism would reach only one of the two sessions.
        *(["--variant", reasoning_effort] if reasoning_effort else []),
        "--model",
        model,
        prompt,
    ]
