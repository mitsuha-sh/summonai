#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

export SUMMONAI_DIR="${SUMMONAI_DIR:-$ROOT_DIR}"
export SUMMONAI_MEMORY_CONFIG="${SUMMONAI_MEMORY_CONFIG:-$ROOT_DIR/.summonai/memory.toml}"

exec "$ROOT_DIR/memory-mcp/scripts/session_start_memory_context.sh"
