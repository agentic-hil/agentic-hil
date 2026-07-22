#!/usr/bin/env bash
# One-process eval: install an explicit immutable package spec, exercise setup
# transactionality, then run OpenOCD and STM32CubeProgrammer plans serially.
# Every result is gated with agentic_hil.report.overall_success before continuing.
#
# Usage: run-all.sh [FIXTURE_DIR] INSTALL_SPEC EXPECTED_VERSION
# Example INSTALL_SPEC:
#   git+https://github.com/agentic-hil/agentic-hil@<full-40-character-sha>
#   agentic-hil==0.4.0
set -euo pipefail

FIXTURE="${1:-$HOME/fixture}"
INSTALL_SPEC="${2:-${AGENTIC_HIL_INSTALL_SPEC:-}}"
EXPECTED_VERSION="${3:-${AGENTIC_HIL_EXPECTED_VERSION:-}}"
HARNESS="$(cd "$(dirname "$0")" && pwd)"
AHIL_HOME="$HOME/ahil"
STATE_ROOT="$AHIL_HOME/state"
OPENOCD_CONFIG="$AHIL_HOME/config.openocd.yaml"
STLINK_CONFIG="$AHIL_HOME/config.stlink.yaml"

exec > >(tee "$HARNESS/transcript.txt") 2>&1

if [[ ! "$INSTALL_SPEC" =~ ^agentic-hil==[0-9A-Za-z.+-]+$ ]] \
  && [[ ! "$INSTALL_SPEC" =~ ^git\+https://github\.com/agentic-hil/agentic-hil(\.git)?@[0-9a-fA-F]{40}$ ]]; then
  echo "FATAL: INSTALL_SPEC must be an exact version or the Agentic HIL repository at a full commit SHA."
  exit 2
fi
if [[ ! "$EXPECTED_VERSION" =~ ^[0-9A-Za-z.+-]+$ ]]; then
  echo "FATAL: EXPECTED_VERSION is required."
  exit 2
fi
printf '%s\n' "$INSTALL_SPEC" > "$HARNESS/install_spec.txt"
printf '%s\n' "$EXPECTED_VERSION" > "$HARNESS/expected_version.txt"

echo "== agentic-hil pinned install + setup + usage eval (openocd + stlink)"
echo "install spec: $INSTALL_SPEC"
echo "expected version: $EXPECTED_VERSION"
uname -a

ensure_path() { export PATH="$HOME/.local/bin:$PATH"; }
step() { echo; echo "== $*"; }
fatal() { echo "FATAL: $*"; exit 1; }

# ---- 1. documented install fallback chain, exact source ------------------
if command -v agentic-hil >/dev/null 2>&1; then
  fatal "golden image is not clean: agentic-hil already resolves before installation"
fi

step "python3 -m pip install --user $INSTALL_SPEC (may fail on PEP-668; expected)"
python3 -m pip install --user "$INSTALL_SPEC" || echo "pip --user path unavailable (expected on PEP-668)"
ensure_path

if ! command -v agentic-hil >/dev/null 2>&1; then
  if ! command -v uv >/dev/null 2>&1; then
    step "bootstrap uv (user-local, no admin rights)"
    curl -LsSf https://astral.sh/uv/install.sh | sh || fatal "uv bootstrap failed"
    ensure_path
  fi
  step "uv tool install --force $INSTALL_SPEC"
  uv tool install --force "$INSTALL_SPEC" || fatal "uv tool installation failed"
  ensure_path
fi

AHIL_BIN="$(command -v agentic-hil || true)"
[ -n "$AHIL_BIN" ] || fatal "agentic-hil not installed after fallback chain"
SHEBANG="$(head -n 1 "$AHIL_BIN")"
case "$SHEBANG" in
  '#!'*) AHIL_PY="${SHEBANG#\#!}" ;;
  *) fatal "cannot identify the Python interpreter behind $AHIL_BIN" ;;
esac
[ -x "$AHIL_PY" ] || fatal "agentic-hil interpreter is not executable: $AHIL_PY"
EXPECTED_MCP_COMMAND="$("$AHIL_PY" - "$AHIL_BIN" <<'PY'
import os
import sys
from pathlib import Path

print(Path(os.path.abspath(Path(sys.argv[1]).expanduser())))
PY
)" || fatal "cannot normalize the persistent agentic-hil launcher path"

overall_success_file() {  # JSON report
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

step "agentic-hil --version"
VERSION="$("$AHIL_BIN" --version)" || fatal "agentic-hil --version failed"
[ "$VERSION" = "$EXPECTED_VERSION" ] || fatal "installed version $VERSION != expected $EXPECTED_VERSION"

# ---- 2. prepare plans and external hardware config root ------------------
step "prepare external config root (outside workspace, user-owned)"
mkdir -p "$STATE_ROOT"
chmod 700 "$AHIL_HOME" "$STATE_ROOT"

CUBECLI="$(command -v STM32_Programmer_CLI 2>/dev/null || true)"
if [ -z "$CUBECLI" ] && [ -x "$HOME/STMicroelectronics/STM32Cube/STM32CubeProgrammer/bin/STM32_Programmer_CLI" ]; then
  CUBECLI="$HOME/STMicroelectronics/STM32Cube/STM32CubeProgrammer/bin/STM32_Programmer_CLI"
fi
echo "STM32CubeProgrammer CLI: ${CUBECLI:-<not found>}"

render_config() {  # template -> output
  sed -e "s|__WORKSPACE__|$FIXTURE|g" -e "s|__STATE__|$STATE_ROOT|g" -e "s|__CUBECLI__|${CUBECLI:-/nonexistent}|g" "$1" > "$2"
}

cd "$FIXTURE"
mkdir -p "$FIXTURE/.agentic-hil"
cp "$HARNESS/testconfig.openocd.yaml" "$FIXTURE/.agentic-hil/testconfig.openocd.yaml"
cp "$HARNESS/testconfig.stlink.yaml" "$FIXTURE/.agentic-hil/testconfig.stlink.yaml"

# ---- 3. setup primary path: success, preservation, idempotency ------------
unset AGENTIC_HIL_CONFIG
cat > "$HOME/.claude.json" <<'JSON'
{
  "harnessSentinel": {"preserve": true, "label": "unrelated-user-config"},
  "mcpServers": {
    "unrelated": {"type": "stdio", "command": "/bin/true", "args": []}
  }
}
JSON
step "agentic-hil setup --agent claude-code (first run)"
if ! "$AHIL_BIN" setup --agent claude-code > "$HARNESS/setup_result.first.json"; then
  cat "$HARNESS/setup_result.first.json"
  fatal "first setup failed"
fi
cat "$HARNESS/setup_result.first.json"
overall_success_file "$HARNESS/setup_result.first.json" || fatal "first setup did not satisfy overall_success"
"$AHIL_PY" - "$HOME/.claude.json" "$EXPECTED_MCP_COMMAND" "$FIXTURE" <<'PY' \
  || fatal "setup did not safely merge the persistent MCP command"
import json
import os
import sys
import tempfile
from pathlib import Path

path = Path(sys.argv[1])
expected = Path(sys.argv[2])
fixture = Path(sys.argv[3]).resolve(strict=True)
temp_root = Path(tempfile.gettempdir()).resolve(strict=True)
data = json.loads(path.read_text(encoding="utf-8"))
if data.get("harnessSentinel") != {"preserve": True, "label": "unrelated-user-config"}:
    raise SystemExit(1)
servers = data.get("mcpServers")
if not isinstance(servers, dict) or servers.get("unrelated") != {
    "type": "stdio",
    "command": "/bin/true",
    "args": [],
}:
    raise SystemExit(1)
entry = servers.get("agentic-hil")
command = entry.get("command") if isinstance(entry, dict) else None
if not isinstance(command, str) or not os.path.isabs(command):
    raise SystemExit(1)
stored = Path(command)
if not expected.is_absolute() or stored != expected:
    raise SystemExit(1)
try:
    resolved = stored.resolve(strict=True)
except OSError:
    raise SystemExit(1)
if not resolved.is_file() or not os.access(resolved, os.X_OK):
    raise SystemExit(1)
if any(
    candidate.is_relative_to(root)
    for candidate in (stored, resolved)
    for root in (fixture, temp_root)
):
    raise SystemExit(1)
PY
printf 'pass\n' > "$HARNESS/mcp_registration_status.txt"

SETUP_CONFIG="$("$AHIL_PY" - "$HARNESS/setup_result.first.json" <<'PY'
import json
import sys

result = json.load(open(sys.argv[1], encoding="utf-8"))
path = result.get("steps", {}).get("config", {}).get("path")
if not isinstance(path, str) or not path:
    raise SystemExit(1)
print(path)
PY
)" || fatal "setup result did not identify its authoritative config"
[ -f "$SETUP_CONFIG" ] || fatal "setup authoritative config is missing"
printf '\n# harness-preservation-sentinel\n' >> "$SETUP_CONFIG"

SKILL="$HOME/.claude/skills/agentic-hil-config-setup/SKILL.md"
[ -f "$SKILL" ] || fatal "setup did not install the Claude Code skill"
CONFIG_HASH_BEFORE="$(sha256sum "$SETUP_CONFIG" | awk '{print $1}')"
SKILL_HASH_BEFORE="$(sha256sum "$SKILL" | awk '{print $1}')"
CLAUDE_CONFIG="$HOME/.claude.json"
CLAUDE_HASH_BEFORE=""
if [ -f "$CLAUDE_CONFIG" ]; then
  CLAUDE_HASH_BEFORE="$(sha256sum "$CLAUDE_CONFIG" | awk '{print $1}')"
fi

step "agentic-hil setup --agent claude-code (idempotent rerun)"
if ! "$AHIL_BIN" setup --agent claude-code > "$HARNESS/setup_result.second.json"; then
  cat "$HARNESS/setup_result.second.json"
  fatal "second setup failed"
fi
cat "$HARNESS/setup_result.second.json"
overall_success_file "$HARNESS/setup_result.second.json" || fatal "second setup did not satisfy overall_success"
"$AHIL_PY" - "$HARNESS/setup_result.second.json" <<'PY' || fatal "second setup did not report idempotent skips"
import json
import sys

steps = json.load(open(sys.argv[1], encoding="utf-8")).get("steps", {})
if steps.get("config", {}).get("skipped") is not True:
    raise SystemExit(1)
if steps.get("mcp_config", {}).get("skipped") is not True:
    raise SystemExit(1)
skill = steps.get("skill_install", {})
if skill.get("installed") is not False or skill.get("updated") is not False:
    raise SystemExit(1)
PY

CONFIG_HASH_AFTER="$(sha256sum "$SETUP_CONFIG" | awk '{print $1}')"
SKILL_HASH_AFTER="$(sha256sum "$SKILL" | awk '{print $1}')"
[ "$CONFIG_HASH_AFTER" = "$CONFIG_HASH_BEFORE" ] || fatal "second setup overwrote the existing authoritative config"
[ "$SKILL_HASH_AFTER" = "$SKILL_HASH_BEFORE" ] || fatal "second setup changed an already-current skill"
grep -q '^# harness-preservation-sentinel$' "$SETUP_CONFIG" || fatal "setup did not preserve user config content"
if [ -n "$CLAUDE_HASH_BEFORE" ]; then
  CLAUDE_HASH_AFTER="$(sha256sum "$CLAUDE_CONFIG" | awk '{print $1}')"
  [ "$CLAUDE_HASH_AFTER" = "$CLAUDE_HASH_BEFORE" ] || fatal "second setup rewrote the existing Claude MCP config"
fi
printf 'pass\n' > "$HARNESS/preservation_status.txt"

# A malformed pre-existing opencode config forces a late setup failure. The
# transaction must preserve that file and remove newly-created config/skill.
step "setup rollback after isolated MCP-registration failure"
ROLLBACK_ROOT="$(mktemp -d "$HOME/agentic-hil-rollback.XXXXXX")"
ROLLBACK_HOME="$ROLLBACK_ROOT/home"
ROLLBACK_WORKSPACE="$ROLLBACK_ROOT/workspace"
mkdir -p "$ROLLBACK_HOME/.config/opencode" "$ROLLBACK_WORKSPACE"
printf '{ invalid-json\n' > "$ROLLBACK_HOME/.config/opencode/opencode.json"
ROLLBACK_USER_HASH="$(sha256sum "$ROLLBACK_HOME/.config/opencode/opencode.json" | awk '{print $1}')"
if ! (
  export HOME="$ROLLBACK_HOME" USERPROFILE="$ROLLBACK_HOME"
  export XDG_CONFIG_HOME="$ROLLBACK_ROOT/config"
  export XDG_STATE_HOME="$ROLLBACK_ROOT/state"
  export XDG_CACHE_HOME="$ROLLBACK_ROOT/cache"
  unset AGENTIC_HIL_CONFIG
  cd "$ROLLBACK_WORKSPACE"
  ROLLBACK_CONFIG="$("$AHIL_PY" -c 'from pathlib import Path; from agentic_hil.config import project_config_path; print(project_config_path(Path.cwd()))')"
  if "$AHIL_BIN" setup --agent opencode > "$HARNESS/setup_result.rollback.json"; then
    cat "$HARNESS/setup_result.rollback.json"
    fatal "rollback probe unexpectedly succeeded"
  fi
  cat "$HARNESS/setup_result.rollback.json"
  if overall_success_file "$HARNESS/setup_result.rollback.json"; then
    fatal "failed setup unexpectedly satisfied overall_success"
  fi
  [ ! -e "$ROLLBACK_CONFIG" ] || fatal "failed setup left a new authoritative config"
  [ ! -e "$ROLLBACK_HOME/.config/opencode/skills/agentic-hil-config-setup/SKILL.md" ] \
    || fatal "failed setup left a newly-installed skill"
  ROLLBACK_USER_HASH_AFTER="$(sha256sum "$ROLLBACK_HOME/.config/opencode/opencode.json" | awk '{print $1}')"
  [ "$ROLLBACK_USER_HASH_AFTER" = "$ROLLBACK_USER_HASH" ] || fatal "failed setup changed pre-existing user config"
); then
  fatal "setup rollback verification failed"
fi
printf 'pass\n' > "$HARNESS/rollback_status.txt"

# ---- 4. hardware usage, one backend at a time ----------------------------
run_variant() {  # name template testconfig outconfig
  local name="$1" tmpl="$2" plan="$3" outcfg="$4"
  step "[$name] render config + doctor"
  render_config "$tmpl" "$outcfg"
  export AGENTIC_HIL_CONFIG="$outcfg"
  if ! "$AHIL_BIN" doctor > "$HARNESS/doctor_report.$name.json"; then
    cat "$HARNESS/doctor_report.$name.json"
    fatal "[$name] doctor failed"
  fi
  cat "$HARNESS/doctor_report.$name.json"
  overall_success_file "$HARNESS/doctor_report.$name.json" || fatal "[$name] doctor did not satisfy overall_success"

  step "[$name] test-reactor"
  if ! "$AHIL_BIN" test-reactor --test-config ".agentic-hil/$plan" > "$HARNESS/reactor_report.$name.json"; then
    cat "$HARNESS/reactor_report.$name.json"
    fatal "[$name] test-reactor failed; no further hardware effects will run"
  fi
  cat "$HARNESS/reactor_report.$name.json"
  overall_success_file "$HARNESS/reactor_report.$name.json" \
    || fatal "[$name] test-reactor did not satisfy overall_success; no further hardware effects will run"
}

run_variant openocd "$HARNESS/config.openocd.template.yaml" testconfig.openocd.yaml "$OPENOCD_CONFIG"

if [ -n "$CUBECLI" ]; then
  printf 'present\n' > "$HARNESS/stlink_status.txt"
  run_variant stlink "$HARNESS/config.stlink.template.yaml" testconfig.stlink.yaml "$STLINK_CONFIG"
else
  printf 'skipped\n' > "$HARNESS/stlink_status.txt"
  step "[stlink] SKIPPED -- STM32CubeProgrammer CLI not installed in this image"
  echo "Install STM32CubeProgrammer to enable the stlink backend variant (see provision-golden.sh)."
fi

# ---- 5. install integrity: MCP tool-surface snapshot (lease-free) --------
step "MCP tools/list snapshot probe (openocd config)"
export AGENTIC_HIL_CONFIG="$OPENOCD_CONFIG"
python3 "$HARNESS/mcp_probe.py" "$FIXTURE" || fatal "MCP tool-surface probe failed"

echo
echo "== eval done"
