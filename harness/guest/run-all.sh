#!/usr/bin/env bash
# One-process eval for Agentic HIL v0.3.0: install -> configure -> run the usage
# sequence on BOTH debugger backends (OpenOCD and STM32CubeProgrammer CLI / stlink)
# -> MCP tool-surface probe. Single process so AGENTIC_HIL_CONFIG persists.
# Everything is logged to transcript.txt. Must run WITHOUT sudo and WITHOUT
# --break-system-packages (assert.sh enforces both against the transcript).
#
# Usage: run-all.sh [FIXTURE_DIR]
set -uo pipefail

FIXTURE="${1:-$HOME/fixture}"
HARNESS="$(cd "$(dirname "$0")" && pwd)"
AHIL_HOME="$HOME/ahil"                 # external, outside the workspace
STATE_ROOT="$AHIL_HOME/state"
OPENOCD_CONFIG="$AHIL_HOME/config.openocd.yaml"
STLINK_CONFIG="$AHIL_HOME/config.stlink.yaml"

exec > >(tee "$HARNESS/transcript.txt") 2>&1
echo "== agentic-hil v0.3.0 install + usage eval (openocd + stlink)"
uname -a

ensure_path() { export PATH="$HOME/.local/bin:$PATH"; }
step() { echo; echo "== $*"; }

# ---- 1. documented install fallback chain --------------------------------
step "python3 -m pip install --user agentic-hil (may fail on PEP-668; expected)"
python3 -m pip install --user agentic-hil || echo "pip --user path unavailable (expected on PEP-668)"
ensure_path

if ! command -v agentic-hil >/dev/null 2>&1; then
  if ! command -v uv >/dev/null 2>&1; then
    step "bootstrap uv (user-local, no admin rights)"
    curl -LsSf https://astral.sh/uv/install.sh | sh
    ensure_path
  fi
  step "uv tool install agentic-hil"
  uv tool install agentic-hil
  ensure_path
fi

step "agentic-hil --version"
if ! agentic-hil --version; then
  echo "FATAL: agentic-hil not installed after fallback chain"
  exit 1
fi

# ---- 2. agent skill ------------------------------------------------------
step "agentic-hil skill-install --agent claude-code"
agentic-hil skill-install --agent claude-code

# ---- 3. prepare external config root + detect ST tools -------------------
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
# One shared plan, run identically on both backends.
cp "$HARNESS/testconfig.yaml" "$FIXTURE/.agentic-hil/testconfig.yaml"

step "agentic-hil init (writes canonical deny-by-default config; must succeed)"
agentic-hil init --force > "$HARNESS/init_result.json" || true
cat "$HARNESS/init_result.json"

step "agentic-hil mcp-config --output .mcp.json"
agentic-hil mcp-config --output .mcp.json --force

# ---- 4. usage: run the reactor sequence on each available backend --------
run_variant() {  # name  template  testconfig  outconfig
  local name="$1" tmpl="$2" plan="$3" outcfg="$4"
  step "[$name] render config + doctor"
  render_config "$tmpl" "$outcfg"
  export AGENTIC_HIL_CONFIG="$outcfg"
  agentic-hil doctor || echo "[$name] doctor reported a failure (board / tool paths?)"
  step "[$name] test-reactor"
  agentic-hil test-reactor --test-config ".agentic-hil/$plan" > "$HARNESS/reactor_report.$name.json" || true
  cat "$HARNESS/reactor_report.$name.json"
}

run_variant openocd "$HARNESS/config.openocd.template.yaml" testconfig.yaml "$OPENOCD_CONFIG"

if [ -n "$CUBECLI" ]; then
  echo "present" > "$HARNESS/stlink_status.txt"
  run_variant stlink "$HARNESS/config.stlink.template.yaml" testconfig.yaml "$STLINK_CONFIG"
else
  echo "skipped" > "$HARNESS/stlink_status.txt"
  step "[stlink] SKIPPED -- STM32CubeProgrammer CLI not installed in this image"
  echo "Install STM32CubeProgrammer to enable the stlink backend variant (see provision-golden.sh)."
fi

# ---- 5. install integrity: MCP tool-surface snapshot (lease-free) --------
step "MCP tools/list snapshot probe (openocd config)"
export AGENTIC_HIL_CONFIG="$OPENOCD_CONFIG"
python3 "$HARNESS/mcp_probe.py" "$FIXTURE" || true

echo
echo "== eval done"
