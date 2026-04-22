#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_ROOT="$(cd "$PROJECT_ROOT/.." && pwd)"
PYTHON_BIN="$PROJECT_ROOT/.venv/bin/python"
if [[ ! -x "$PYTHON_BIN" ]]; then
  PYTHON_BIN="python3"
fi
export SUMMONAI_MEMORY_DB="${SUMMONAI_MEMORY_DB:-$REPO_ROOT/.data/summonai_memory.db}"
exec "$PYTHON_BIN" "$SCRIPT_DIR/passive_recall_hook.py"
