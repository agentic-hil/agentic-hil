#!/usr/bin/env bash
# Deterministic post-run checks. Every Agentic HIL JSON result is evaluated with
# the installed package's public agentic_hil.report.overall_success helper.
# Exit code = verdict. Usage: assert.sh [FIXTURE_DIR]
set -uo pipefail

FIXTURE="${1:-$HOME/fixture}"
HARNESS="$(cd "$(dirname "$0")" && pwd)"
OPENOCD_CONFIG="$HOME/ahil/config.openocd.yaml"
STLINK_CONFIG="$HOME/ahil/config.stlink.yaml"
export PATH="$HOME/.local/bin:$PATH"

fail=0
pass() { echo "PASS: $1"; }
bad()  { echo "FAIL: $1"; fail=1; }
note() { echo "NOTE: $1"; }

AHIL_BIN="$(command -v agentic-hil 2>/dev/null || true)"
AHIL_PY=""
if [ -n "$AHIL_BIN" ]; then
  SHEBANG="$(head -n 1 "$AHIL_BIN" 2>/dev/null || true)"
  case "$SHEBANG" in
    '#!'*) AHIL_PY="${SHEBANG#\#!}" ;;
  esac
fi

report_passed() {  # JSON report
  [ -n "$AHIL_PY" ] && [ -x "$AHIL_PY" ] && [ -f "$1" ] && [ -s "$1" ] || return 1
  "$AHIL_PY" - "$1" <<'PY'
import json
import sys

from agentic_hil.report import overall_success

try:
    result = json.load(open(sys.argv[1], encoding="utf-8"))
except (OSError, ValueError, TypeError):
    raise SystemExit(1)
raise SystemExit(0 if isinstance(result, dict) and overall_success(result) else 1)
PY
}

report_failed_cleanly() {  # expected setup failure report
  [ -n "$AHIL_PY" ] && [ -x "$AHIL_PY" ] && [ -f "$1" ] && [ -s "$1" ] || return 1
  "$AHIL_PY" - "$1" <<'PY'
import json
import sys

from agentic_hil.report import overall_success

try:
    result = json.load(open(sys.argv[1], encoding="utf-8"))
except (OSError, ValueError, TypeError):
    raise SystemExit(1)
valid_failure = isinstance(result, dict) and result.get("ok") is False and not overall_success(result)
raise SystemExit(0 if valid_failure else 1)
PY
}

# --- exact install source and version ---
if [ -n "$AHIL_BIN" ]; then pass "agentic-hil on PATH ($AHIL_BIN)"; else bad "agentic-hil on PATH"; fi
if [ -n "$AHIL_PY" ] && [ -x "$AHIL_PY" ]; then pass "agentic-hil interpreter resolved"; else bad "agentic-hil interpreter resolved"; fi
VER="$(agentic-hil --version 2>/dev/null || true)"
EXPECTED_VERSION="$(cat "$HARNESS/expected_version.txt" 2>/dev/null || true)"
INSTALL_SPEC="$(cat "$HARNESS/install_spec.txt" 2>/dev/null || true)"
if [ -n "$INSTALL_SPEC" ]; then pass "explicit install spec recorded: $INSTALL_SPEC"; else bad "explicit install spec recorded"; fi
if [ -n "$EXPECTED_VERSION" ] && [ "$VER" = "$EXPECTED_VERSION" ]; then
  pass "agentic-hil version matches expected $EXPECTED_VERSION"
else
  bad "agentic-hil version ($VER) matches expected ($EXPECTED_VERSION)"
fi

# --- setup transaction and user-level registration ---
if report_passed "$HARNESS/setup_result.first.json"; then pass "first setup satisfied overall_success"; else bad "first setup satisfied overall_success"; fi
if report_passed "$HARNESS/setup_result.second.json"; then pass "idempotent setup satisfied overall_success"; else bad "idempotent setup satisfied overall_success"; fi
if report_failed_cleanly "$HARNESS/setup_result.rollback.json"; then pass "rollback probe failed cleanly"; else bad "rollback probe failed cleanly"; fi
if [ "$(cat "$HARNESS/preservation_status.txt" 2>/dev/null || true)" = "pass" ]; then pass "setup preserved config and was idempotent"; else bad "setup preserved config and was idempotent"; fi
if [ "$(cat "$HARNESS/rollback_status.txt" 2>/dev/null || true)" = "pass" ]; then pass "failed setup rolled back new files"; else bad "failed setup rolled back new files"; fi
if [ "$(cat "$HARNESS/mcp_registration_status.txt" 2>/dev/null || true)" = "pass" ]; then pass "setup preserved user config and pinned the persistent MCP command"; else bad "setup preserved user config and pinned the persistent MCP command"; fi

SKILL="$HOME/.claude/skills/agentic-hil-config-setup/SKILL.md"
if [ -f "$SKILL" ]; then
  pass "claude-code skill installed"
  SKILL_VER="$(grep -oE 'agentic_hil_version: "[^"]+"' "$SKILL" | sed -E 's/.*"([^"]+)".*/\1/')"
  if [ "$SKILL_VER" = "$VER" ]; then pass "skill version matches CLI"; else bad "skill version ($SKILL_VER) != CLI ($VER)"; fi
else
  bad "claude-code skill installed"
fi
if [ ! -f "$FIXTURE/.mcp.json" ]; then pass "setup kept MCP registration outside the repo"; else bad "setup wrote project .mcp.json"; fi

# --- external config model ---
if [ -f "$OPENOCD_CONFIG" ]; then pass "external OpenOCD config present"; else bad "external OpenOCD config present"; fi
if [ ! -f "$FIXTURE/.agentic-hil/config.yaml" ]; then pass "no repo-local authoritative config"; else bad "repo-local .agentic-hil/config.yaml exists"; fi

# --- ground rules ---
T="$HARNESS/transcript.txt"
if [ -f "$T" ]; then
  if grep -qiE '(^|[^[:alnum:]])sudo ' "$T"; then bad "install used sudo"; else pass "install used no sudo"; fi
  if grep -q -- '--break-system-packages' "$T"; then bad "install used --break-system-packages"; else pass "install avoided --break-system-packages"; fi
else
  bad "transcript.txt present"
fi
if [ ! -d "$FIXTURE/src/agentic_hil" ]; then pass "no agentic-hil source vendored into fixture"; else bad "agentic-hil source vendored into fixture"; fi

# --- hardware results: exact public success predicate ---
if report_passed "$HARNESS/doctor_report.openocd.json"; then pass "openocd doctor satisfied overall_success"; else bad "openocd doctor satisfied overall_success"; fi
if report_passed "$HARNESS/reactor_report.openocd.json"; then pass "openocd reactor satisfied overall_success"; else bad "openocd reactor satisfied overall_success"; fi

STATUS="$(cat "$HARNESS/stlink_status.txt" 2>/dev/null || echo unknown)"
if [ "$STATUS" = "present" ]; then
  if [ -f "$STLINK_CONFIG" ]; then pass "external stlink config present"; else bad "external stlink config present"; fi
  if report_passed "$HARNESS/doctor_report.stlink.json"; then pass "stlink doctor satisfied overall_success"; else bad "stlink doctor satisfied overall_success"; fi
  if report_passed "$HARNESS/reactor_report.stlink.json"; then pass "stlink reactor satisfied overall_success"; else bad "stlink reactor satisfied overall_success"; fi
elif [ "$STATUS" = "skipped" ]; then
  note "stlink variant skipped (STM32CubeProgrammer CLI not installed)"
else
  bad "stlink status known"
fi

# --- install integrity: MCP tool surface snapshot ---
P="$HARNESS/mcp_probe.json"
if [ -f "$P" ] && grep -q '"overall": "PASS"' "$P"; then pass "MCP tool surface snapshot matched"; else bad "MCP tool surface snapshot matched"; fi

echo
if [ "$fail" -eq 0 ]; then echo "ASSERT: ALL PASS"; else echo "ASSERT: FAILURES PRESENT"; fi
exit "$fail"
