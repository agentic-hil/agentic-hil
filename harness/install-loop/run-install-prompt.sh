#!/usr/bin/env bash
# First test / doc-optimization loop: fire the canonical install prompt at an
# agent and capture what it does following AI_AGENT_QUICKSTART.md. Read the
# transcript, improve the install instructions, re-run.
#
# Isolation trick: HOME is redirected to a throwaway dir, so the agent installs
# agentic-hil "from nothing" every run (no reset needed) and your real machine
# is untouched. The agent CLI therefore cannot read its file-based auth -- pass
# auth via env instead (see below), which survives the HOME override.
#
#   Claude Code : export CLAUDE_CODE_OAUTH_TOKEN=...   (claude setup-token)
#   Codex       : export CODEX_API_KEY=...
#   opencode    : export ANTHROPIC_API_KEY=... (or the key for OPENCODE_MODEL)
#
# The prompt points at the work BRANCH (not master) so the agent installs the
# branch's code AND reads the branch's AI_AGENT_QUICKSTART.md -- that is what
# lets you optimize the install docs and see the effect on the next run. The
# branch must be pushed to the remote first (see README).
#
# Usage: run-install-prompt.sh claude|codex|opencode [prompt]
#   Override target with REPO=... BRANCH=... in the environment.
set -uo pipefail

AGENT="${1:?usage: run-install-prompt.sh claude|codex|opencode [prompt]}"
REPO="${REPO:-https://github.com/agentic-hil/agentic-hil}"
BRANCH="${BRANCH:-feature/smooth-installation}"
PROMPT="${2:-Install agentic-hil from the '$BRANCH' branch of $REPO (use git+$REPO@$BRANCH as the package source) and set it up for this project.}"

TRANSCRIPTS="$(cd "$(dirname "$0")" && pwd)/transcripts"
mkdir -p "$TRANSCRIPTS"
STAMP="$(date +%Y%m%d-%H%M%S)"
LOG="$TRANSCRIPTS/install-$AGENT-$STAMP.log"

TH="$(mktemp -d)"
PROJ="$TH/fw-project"
mkdir -p "$PROJ"
export HOME="$TH" USERPROFILE="$TH"      # fresh, isolated environment per run
cd "$PROJ"

echo "agent=$AGENT  target=$REPO@$BRANCH"
echo "home=$TH  workdir=$PROJ"
echo "transcript=$LOG"
echo "prompt: $PROMPT"
echo "----------------------------------------------------------------------"

case "$AGENT" in
  claude)
    claude -p "$PROMPT" --dangerously-skip-permissions 2>&1 | tee "$LOG"
    ;;
  codex)
    # default codex sandbox is read-only with no network; installing needs both.
    codex exec --dangerously-bypass-approvals-and-sandbox "$PROMPT" 2>&1 | tee "$LOG"
    ;;
  opencode)
    opencode run --model "${OPENCODE_MODEL:-anthropic/claude-sonnet-4-5}" "$PROMPT" 2>&1 | tee "$LOG"
    ;;
  *)
    echo "unknown agent: $AGENT (use claude|codex|opencode)"; exit 2 ;;
esac

echo "----------------------------------------------------------------------"
echo "done. transcript: $LOG"
echo "throwaway HOME kept for inspection: $TH   (rm -rf \"$TH\" when finished)"
