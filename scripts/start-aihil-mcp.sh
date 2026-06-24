#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
CONFIG_PATH="$REPO_ROOT/.aihil/config.yaml"

cd "$REPO_ROOT"
python -m pip install -e .

if [ ! -f "$CONFIG_PATH" ]; then
    aihil init --config "$CONFIG_PATH"
fi

exec aihil serve --config "$CONFIG_PATH"
