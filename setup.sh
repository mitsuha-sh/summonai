#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
DATA_DIR="$ROOT_DIR/.data"
MEMORY_DB="$DATA_DIR/summonai_memory.db"
TASK_DB="$DATA_DIR/summonai_task.db"
RUNNER_CONFIG="$ROOT_DIR/config/task_runner.claude.json"
SETTINGS_PATH="$ROOT_DIR/.claude/settings.json"

echo "=== summonai setup ==="

if ! command -v zellij &> /dev/null; then
  echo "Warning: zellij is not installed. task-mcp runner requires zellij."
  echo "Install: cargo install zellij  or  https://zellij.dev/documentation/installation"
fi

# ── 1. Data directory ──
mkdir -p "$DATA_DIR"

# ── 2. Submodule init ──
if [ ! -f "$ROOT_DIR/memory-mcp/server.py" ] || [ ! -f "$ROOT_DIR/task-mcp/pyproject.toml" ]; then
  echo "Initializing submodules..."
  git -C "$ROOT_DIR" submodule update --init
fi

# ── 3. memory-mcp venv (required for hooks) ──
MEMORY_MCP_DIR="$ROOT_DIR/memory-mcp"
VENV_DIR="$MEMORY_MCP_DIR/.venv"
if [ ! -d "$VENV_DIR" ]; then
  echo "Creating memory-mcp venv..."
  python3 -m venv "$VENV_DIR"
fi
echo "Installing memory-mcp dependencies..."
"$VENV_DIR/bin/pip" install -q -r "$MEMORY_MCP_DIR/requirements.txt"

# ── 4. Register MCP servers (project scope) ──
echo "Registering MCP servers..."
claude mcp remove summonai-memory-mcp 2>/dev/null || true
claude mcp remove summonai-task-mcp 2>/dev/null || true

claude mcp add -s project \
  summonai-memory-mcp \
  -e SUMMONAI_MEMORY_DB="$MEMORY_DB" \
  -- "$VENV_DIR/bin/python" "$MEMORY_MCP_DIR/server.py"

claude mcp add -s project \
  summonai-task-mcp \
  -e SUMMONAI_TASK_DB="$TASK_DB" \
  -e SUMMONAI_TASK_RUNNER_CONFIG="$RUNNER_CONFIG" \
  -- uv run --directory "$ROOT_DIR/task-mcp" python -m summonai_task.server

# ── 5. Task runner config ──
# config/task_runner.claude.json is committed to git with relative project_dir.
# Do not overwrite it with absolute paths (OSS policy).
# Runtime paths are passed via SUMMONAI_TASK_RUNNER_CONFIG env var instead.
if [ ! -f "$RUNNER_CONFIG" ]; then
  cat > "$RUNNER_CONFIG" <<RUNNER_EOF
{
  "enabled": true,
  "project_dir": ".",
  "runner": "claude"
}
RUNNER_EOF
  echo "Generated: $RUNNER_CONFIG"
else
  echo "Skipped (exists): $RUNNER_CONFIG"
fi

# ── 6. Hooks (SessionStart + Stop) ──
mkdir -p "$ROOT_DIR/.claude"
if [ ! -f "$SETTINGS_PATH" ]; then
  printf '{}\n' > "$SETTINGS_PATH"
fi

TMP_FILE="$(mktemp)"
jq '
  .hooks = {
    "SessionStart": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "SUMMONAI_AGENT_ID=summonai bash memory-mcp/scripts/session_start_memory_context.sh",
            "timeout": 15
          }
        ]
      }
    ],
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "SUMMONAI_AGENT_ID=summonai bash memory-mcp/scripts/stop_hook_conversation_save.sh",
            "timeout": 15
          }
        ]
      }
    ]
  }
' "$SETTINGS_PATH" > "$TMP_FILE"
mv "$TMP_FILE" "$SETTINGS_PATH"

# ── 7. Persona files ──
PERSONA_DIR="$MEMORY_MCP_DIR/persona"
for f in USER.md SOUL.md; do
  if [ ! -f "$PERSONA_DIR/$f" ] && [ -f "$PERSONA_DIR/$f.example" ]; then
    cp "$PERSONA_DIR/$f.example" "$PERSONA_DIR/$f"
    echo "Created $PERSONA_DIR/$f from example (edit to customize)"
  fi
done

# ── Done ──
echo ""
echo "Setup complete."
echo "- MCP servers: .mcp.json"
echo "- Hooks: .claude/settings.json (SessionStart + Stop)"
echo "- Memory venv: memory-mcp/.venv"
echo "- Task runner: $RUNNER_CONFIG"
echo ""
echo "Start Claude Code:"
echo "  cd $ROOT_DIR && claude"
