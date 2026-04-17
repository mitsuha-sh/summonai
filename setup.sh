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

EXECUTORS_CONFIG="$ROOT_DIR/.summonai/executors.toml"

claude mcp add -s project \
  summonai-task-mcp \
  -e SUMMONAI_TASK_DB="$TASK_DB" \
  -e SUMMONAI_TASK_RUNNER_CONFIG="$RUNNER_CONFIG" \
  -e SUMMONAI_EXECUTORS_CONFIG="$EXECUTORS_CONFIG" \
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

# ── 6. Project-local memory hook config and executors config ──
mkdir -p "$ROOT_DIR/.summonai" "$ROOT_DIR/personas/default"
MEMORY_CONFIG="$ROOT_DIR/.summonai/memory.toml"
PERSONA_DIR="$ROOT_DIR/personas/default"
if [ ! -f "$MEMORY_CONFIG" ]; then
  cat > "$MEMORY_CONFIG" <<EOF
agent_id = "default"
project = "summonai"
scope_type = "project"
scope_id = "summonai"
persona_dir = "$PERSONA_DIR"
EOF
  echo "Created $MEMORY_CONFIG"
fi

if [ ! -f "$EXECUTORS_CONFIG" ]; then
  cp "$ROOT_DIR/config/executors.toml.example" "$EXECUTORS_CONFIG"
  echo "Created $EXECUTORS_CONFIG (edit to customize capability tiers)"
else
  echo "Skipped (exists): $EXECUTORS_CONFIG"
fi

# ── 7. Hooks (SessionStart + Stop) ──
mkdir -p "$ROOT_DIR/.claude"
if [ ! -f "$SETTINGS_PATH" ]; then
  printf '{}\n' > "$SETTINGS_PATH"
fi

TMP_FILE="$(mktemp)"
jq --arg root "$ROOT_DIR" '
  .hooks = {
    "SessionStart": [
      {
        "hooks": [
          {
            "type": "command",
            "command": ("SUMMONAI_DIR=" + $root + " SUMMONAI_MEMORY_CONFIG=" + $root + "/.summonai/memory.toml bash " + $root + "/memory-mcp/scripts/session_start_memory_context.sh"),
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
            "command": ("SUMMONAI_DIR=" + $root + " SUMMONAI_MEMORY_CONFIG=" + $root + "/.summonai/memory.toml bash " + $root + "/memory-mcp/scripts/stop_hook_conversation_save.sh"),
            "timeout": 15
          }
        ]
      }
    ]
  }
' "$SETTINGS_PATH" > "$TMP_FILE"
mv "$TMP_FILE" "$SETTINGS_PATH"

# ── 8. Persona files ──
for f in USER.md SOUL.md; do
  if [ ! -f "$PERSONA_DIR/$f" ] && [ -f "$MEMORY_MCP_DIR/persona/$f.example" ]; then
    cp "$MEMORY_MCP_DIR/persona/$f.example" "$PERSONA_DIR/$f"
    echo "Created $PERSONA_DIR/$f from example (edit to customize)"
  fi
done

# ── Done ──
echo ""
echo "Setup complete."
echo "- MCP servers: .mcp.json"
echo "- Hooks: .claude/settings.json (SessionStart + Stop)"
echo "- Memory hook config: .summonai/memory.toml"
echo "- Persona source: $PERSONA_DIR"
echo "- Memory venv: memory-mcp/.venv"
echo "- Task runner: $RUNNER_CONFIG"
echo "- Executor tiers: $EXECUTORS_CONFIG"
echo ""
echo "Start Claude Code:"
echo "  cd $ROOT_DIR && claude"
