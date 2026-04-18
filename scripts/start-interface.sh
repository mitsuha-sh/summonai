#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TOML="$SCRIPT_DIR/../.summonai/interface.toml"

model=$(awk '/^\[interface\]/{found=1} found && /^model/{gsub(/.*= *"|"$/, ""); print; exit}' "$TOML")

if [[ -z "$model" ]]; then
  echo "error: [interface].model not found in $TOML" >&2
  exit 1
fi

exec claude --model "$model" --dangerously-skip-permissions
