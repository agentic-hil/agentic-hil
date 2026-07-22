#!/usr/bin/env bash
# First test / doc-optimization loop: fire the canonical install prompt at an
# agent and capture what it does following AI_AGENT_QUICKSTART.md. Read the
# transcript, improve the install instructions, re-run.
#
# Isolation trick: all user/config/state/cache roots and Python installer roots
# are redirected to a throwaway dir. PATH is rebuilt without the operator's
# user bins, so an existing agentic-hil cannot satisfy the install accidentally.
# The selected agent executable is resolved before isolation; pass its auth via
# env (see below), because file-based auth is intentionally unavailable.
#
#   Claude Code : export CLAUDE_CODE_OAUTH_TOKEN=...   (claude setup-token)
#   Codex       : export CODEX_API_KEY=...
#   opencode    : export ANTHROPIC_API_KEY=... (or the key for OPENCODE_MODEL)
#
# The prompt points at the explicitly selected BRANCH so the agent installs that
# ref's code AND reads that ref's AI_AGENT_QUICKSTART.md. The ref must be pushed
# to the remote first (see README).
#
# Usage: run-install-prompt.sh claude|codex|opencode [prompt]
#   BRANCH is required; override the repository with REPO=... if needed.
set -euo pipefail

AGENT="${1:?usage: run-install-prompt.sh claude|codex|opencode [prompt]}"
REPO="${REPO:-https://github.com/agentic-hil/agentic-hil}"
BRANCH="${BRANCH:?set BRANCH to the remote branch or ref under test}"
PROMPT="${2:-Install agentic-hil from the '$BRANCH' branch of $REPO (use git+$REPO@$BRANCH as the package source) and set it up for this project.}"

case "$AGENT" in
  claude) AGENT_COMMAND="claude" ;;
  codex) AGENT_COMMAND="codex" ;;
  opencode) AGENT_COMMAND="opencode" ;;
  *) echo "unknown agent: $AGENT (use claude|codex|opencode)"; exit 2 ;;
esac
AGENT_BIN="$(command -v "$AGENT_COMMAND" || true)"
if [ -z "$AGENT_BIN" ] || [ ! -x "$AGENT_BIN" ]; then
  echo "agent executable not found: $AGENT_COMMAND"
  exit 2
fi

TRANSCRIPTS="$(cd "$(dirname "$0")" && pwd)/transcripts"
mkdir -p "$TRANSCRIPTS"
STAMP="$(date +%Y%m%d-%H%M%S)"
LOG="$TRANSCRIPTS/install-$AGENT-$STAMP.log"

TH="$(mktemp -d)"
PROJ="$TH/fw-project"
mkdir -p "$PROJ" "$TH/.config" "$TH/.local/state" "$TH/.local/share" "$TH/.cache" "$TH/runtime" "$TH/tmp"
chmod 700 "$TH/runtime" "$TH/tmp"
export HOME="$TH" USERPROFILE="$TH"
export XDG_CONFIG_HOME="$TH/.config"
export XDG_STATE_HOME="$TH/.local/state"
export XDG_DATA_HOME="$TH/.local/share"
export XDG_CACHE_HOME="$TH/.cache"
export XDG_RUNTIME_DIR="$TH/runtime"
export TMPDIR="$TH/tmp"
export PYTHONUSERBASE="$TH/.local"
export PIP_CONFIG_FILE="/dev/null"
export PIP_CACHE_DIR="$TH/.cache/pip"
export PIPX_HOME="$TH/.local/share/pipx"
export PIPX_BIN_DIR="$TH/.local/bin"
export UV_CACHE_DIR="$TH/.cache/uv"
export UV_TOOL_DIR="$TH/.local/share/uv/tools"
export UV_TOOL_BIN_DIR="$TH/.local/bin"
export NPM_CONFIG_USERCONFIG="$TH/.npmrc"
export GIT_CONFIG_GLOBAL="/dev/null"
unset AGENTIC_HIL_CONFIG PYTHONHOME PYTHONPATH VIRTUAL_ENV CONDA_PREFIX CONDA_DEFAULT_ENV
unset PYTHONSTARTUP PYTHONBREAKPOINT PYTHONINSPECT PIP_REQUIRE_VIRTUALENV PIP_TARGET PIP_PREFIX PIP_USER
unset UV_CONFIG_FILE UV_PROJECT_ENVIRONMENT UV_PYTHON CODEX_HOME CLAUDE_CONFIG_DIR
unset OPENCODE_CONFIG OPENCODE_CONFIG_DIR SSH_AUTH_SOCK GPG_AGENT_INFO
export PATH="$TH/.local/bin:$TH/.opencode/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
cd "$PROJ"

{
  echo "agent=$AGENT  target=$REPO@$BRANCH"
  echo "home=$TH  workdir=$PROJ"
  echo "transcript=$LOG"
  echo "prompt: $PROMPT"
  echo "----------------------------------------------------------------------"
} | tee "$LOG"

case "$AGENT" in
  claude)
    if "$AGENT_BIN" -p "$PROMPT" --dangerously-skip-permissions 2>&1 | tee -a "$LOG"; then
      AGENT_STATUS=0
    else
      AGENT_STATUS=$?
    fi
    ;;
  codex)
    # default codex sandbox is read-only with no network; installing needs both.
    if "$AGENT_BIN" exec --dangerously-bypass-approvals-and-sandbox "$PROMPT" 2>&1 | tee -a "$LOG"; then
      AGENT_STATUS=0
    else
      AGENT_STATUS=$?
    fi
    ;;
  opencode)
    if "$AGENT_BIN" run --model "${OPENCODE_MODEL:-anthropic/claude-sonnet-4-5}" "$PROMPT" 2>&1 | tee -a "$LOG"; then
      AGENT_STATUS=0
    else
      AGENT_STATUS=$?
    fi
    ;;
esac

{
  echo "----------------------------------------------------------------------"
  echo "done (agent exit $AGENT_STATUS). transcript: $LOG"
  echo "throwaway HOME kept for inspection: $TH   (rm -rf \"$TH\" when finished)"
} | tee -a "$LOG"
exit "$AGENT_STATUS"
