#!/usr/bin/env bash
# Deterministic post-run checks for the v0.3.0 harness (openocd + stlink).
# Exit code = verdict. Usage: assert.sh [FIXTURE_DIR]
set -uo pipefail

FIXTURE="${1:-$HOME/fixture}"
HARNESS="$(cd "$(dirname "$0")" && pwd)"
OPENOCD_CONFIG="$HOME/ahil/config.openocd.yaml"
export PATH="$HOME/.local/bin:$PATH"

fail=0
pass() { echo "PASS: $1"; }
bad()  { echo "FAIL: $1"; fail=1; }
note() { echo "NOTE: $1"; }

reactor_passed() {  # report-file
  [ -f "$1" ] && [ -s "$1" ] && grep -q '"ok": true' "$1" && ! grep -q '"ok": false' "$1"
}

# Classify a reactor report: PASS | PENDING:<action> | FAIL:<reason>.
# PENDING = the first failing step is a debug_* action that returned
# not_supported (functions not yet implemented on this backend). Any flash/uart
# failure is a real FAIL and is never masked.
classify_reactor() {  # report-file
python3 - "$1" <<'PY'
import json, sys
DEBUG = {"debug_start", "run_until_breakpoint", "dump_memory", "debug_stop"}
try:
    d = json.load(open(sys.argv[1]))
except Exception:
    print("FAIL:no-report"); sys.exit(0)
if d.get("ok") is True:
    print("PASS"); sys.exit(0)
fs = d.get("failed_step")
steps = d.get("steps", [])
if isinstance(fs, int) and 1 <= fs <= len(steps):
    step = steps[fs - 1]
    action = step.get("action")
    res = step.get("result", {}) or {}
    et = res.get("error_type") or res.get("step_error_type")
    if action in DEBUG and et == "not_supported":
        print("PENDING:%s" % action); sys.exit(0)
    print("FAIL:%s@%s" % (et, action)); sys.exit(0)
print("FAIL:%s" % (d.get("error_type") or d.get("step_error_type") or "unknown"))
PY
}

# --- install succeeded, user-local ---
if command -v agentic-hil >/dev/null; then pass "agentic-hil on PATH"; else bad "agentic-hil on PATH"; fi
VER="$(agentic-hil --version 2>/dev/null || true)"
if [ -n "$VER" ]; then pass "agentic-hil --version -> $VER"; else bad "agentic-hil --version"; fi

# --- agent skill installed and version-matched ---
SKILL="$HOME/.claude/skills/agentic-hil-config-setup/SKILL.md"
if [ -f "$SKILL" ]; then
  pass "claude-code skill installed"
  SKILL_VER="$(grep -oE 'agentic_hil_version: "[^"]+"' "$SKILL" | sed -E 's/.*"([^"]+)".*/\1/')"
  if [ "$SKILL_VER" = "$VER" ]; then pass "skill version matches CLI"; else bad "skill version ($SKILL_VER) != CLI ($VER)"; fi
else
  bad "claude-code skill installed"
fi

# --- v0.3.0 config model: authoritative config is EXTERNAL, not in the repo ---
if [ -f "$OPENOCD_CONFIG" ]; then pass "external authoritative config present"; else bad "external authoritative config present"; fi
if [ ! -f "$FIXTURE/.agentic-hil/config.yaml" ]; then pass "no repo-local config.yaml (external-config model honored)"; else bad "repo-local .agentic-hil/config.yaml exists (should be external)"; fi

# --- `agentic-hil init` works ---
I="$HARNESS/init_result.json"
if [ -f "$I" ] && grep -q '"ok": true' "$I"; then pass "agentic-hil init succeeded"; else bad "agentic-hil init succeeded"; fi

# --- project MCP discovery written ---
if [ -f "$FIXTURE/.mcp.json" ] && grep -q mcp-stdio "$FIXTURE/.mcp.json"; then pass ".mcp.json written and references mcp-stdio"; else bad ".mcp.json written and references mcp-stdio"; fi

# --- ground rules ---
T="$HARNESS/transcript.txt"
if [ -f "$T" ]; then
  if grep -qiE '(^|[^[:alnum:]])sudo ' "$T"; then bad "install used sudo"; else pass "install used no sudo"; fi
  if grep -q -- '--break-system-packages' "$T"; then bad "install used --break-system-packages"; else pass "install avoided --break-system-packages"; fi
else
  bad "transcript.txt present"
fi

# --- do not vendor agentic-hil source ---
if [ ! -d "$FIXTURE/src/agentic_hil" ]; then pass "no agentic-hil source vendored into fixture"; else bad "agentic-hil source vendored into fixture"; fi

# --- usage: OpenOCD reactor sequence (with delay breakpoint) ---
if reactor_passed "$HARNESS/reactor_report.openocd.json"; then pass "openocd test-reactor sequence passed"; else bad "openocd test-reactor sequence passed"; fi

# --- usage: stlink (STM32CubeProgrammer) reactor sequence, if installed ---
STATUS="$(cat "$HARNESS/stlink_status.txt" 2>/dev/null || echo unknown)"
if [ "$STATUS" = "present" ]; then
  if reactor_passed "$HARNESS/reactor_report.stlink.json"; then pass "stlink test-reactor sequence passed"; else bad "stlink test-reactor sequence passed"; fi
elif [ "$STATUS" = "skipped" ]; then
  note "stlink variant skipped (STM32CubeProgrammer CLI not installed) -- not a failure"
else
  bad "stlink status unknown (run-all.sh did not record it)"
fi

# --- install integrity: MCP tool surface snapshot ---
P="$HARNESS/mcp_probe.json"
if [ -f "$P" ] && grep -q '"overall": "PASS"' "$P"; then pass "MCP tool surface snapshot matched"; else bad "MCP tool surface snapshot matched"; fi

echo
if [ "$fail" -eq 0 ]; then echo "ASSERT: ALL PASS"; else echo "ASSERT: FAILURES PRESENT"; fi
exit "$fail"
