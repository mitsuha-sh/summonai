#!/usr/bin/env bash
# Codex CLI SessionStart hook wrapper.
# Runs session_start_memory_context.py and wraps the plain-text output in
# the JSON format that the Codex hook engine expects (additionalContext).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Capture the plain-text output from the existing hook script.
# Pass stdin (Codex JSON payload) through to the underlying script.
context="$(cat - | bash "$SCRIPT_DIR/session_start_memory_context.sh")" || {
  # If the underlying script fails, emit a minimal valid JSON so Codex
  # does not mark the hook as failed.
  printf '{"continue":true,"hookSpecificOutput":{"hookEventName":"SessionStart","additionalContext":"[session_start_memory_context] hook script returned non-zero exit code"}}'
  exit 0
}

if [[ -z "$context" ]]; then
  # Empty output: no context to inject, just continue.
  printf '{"continue":true}'
  exit 0
fi

# Escape the context string for JSON embedding.
escaped="$(printf '%s' "$context" | python3 -c "
import sys, json
print(json.dumps(sys.stdin.read()), end='')
")"

printf '{"continue":true,"hookSpecificOutput":{"hookEventName":"SessionStart","additionalContext":%s}}' "$escaped"
