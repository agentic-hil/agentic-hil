#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
CONFIG_PATH="$REPO_ROOT/.aihil/config.yaml"

cd "$REPO_ROOT"

if ! command -v aihil >/dev/null 2>&1; then
    echo "aihil is not installed. Install it once on this machine, then rerun this script." >&2
    echo "Example from the AI-HIL repository: python -m pip install -e ." >&2
    exit 127
fi

if [ ! -f "$CONFIG_PATH" ]; then
    aihil init --config "$CONFIG_PATH"
fi

exec aihil serve --config "$CONFIG_PATH"
