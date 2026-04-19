#!/bin/bash
# summonai-memory-mcp setup: venv + Claude Code MCP registration
#
# NOTE: When used as part of the summonai repository, run the root setup.sh
# instead. This script is for standalone use of memory-mcp only.
set -e

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$REPO_DIR"

# 1. venv
if [ ! -d .venv ]; then
  echo "Creating venv..."
  python3 -m venv .venv
fi
echo "Installing dependencies..."
.venv/bin/pip install -q -r requirements.txt

# 2. Resolve DB path
# When memory-mcp is a submodule of summonai, default to the parent repo's
# .data/summonai_memory.db so both hooks and MCP server share the same DB.
# Override by setting SUMMONAI_MEMORY_DB in the environment before running.
PARENT_DIR="$(cd "$REPO_DIR/.." && pwd)"
SUMMONAI_MEMORY_DB="${SUMMONAI_MEMORY_DB:-$PARENT_DIR/.data/summonai_memory.db}"

# 3. Register MCP server
TARGET_PROJECT="${1:-}"
if [ -n "$TARGET_PROJECT" ]; then
  SCOPE_ARGS="-s project -d $TARGET_PROJECT"
else
  SCOPE_ARGS="-s user"
fi

echo "Registering MCP server (${SCOPE_ARGS})..."
echo "  SUMMONAI_MEMORY_DB=$SUMMONAI_MEMORY_DB"
claude mcp remove summonai-memory-mcp 2>/dev/null || true
claude mcp add summonai-memory-mcp $SCOPE_ARGS \
  -e SUMMONAI_MEMORY_DB="$SUMMONAI_MEMORY_DB" -- \
  "$REPO_DIR/.venv/bin/python" "$REPO_DIR/server.py"

echo "Done. Restart Claude Code to connect."
