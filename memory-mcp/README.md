# summonai-memory-mcp

📖 [日本語版はこちら](README_ja.md)

SQLite + FTS5 + sqlite-vec hybrid search Memory MCP server for open-source agents.
It delivers fast full-text search, vector similarity, and configurable ranking from a single SQLite
backend.

## Features
1. **FTS5 full-text search** over memory content, categories, and source context (trigram tokenizer).
2. **Vector similarity search** backed by `sqlite-vec` using `cl-nagoya/ruri-v3-130m` embeddings.
3. **RRF hybrid ranking** that blends FTS hits, vector distances, and effective importance scoring.
4. **Tag/importance filtering** so callers can restrict results by tags, importance thresholds, and bi-temporal windows.
5. **Bi-temporal management** (valid_from/valid_until) for historical reasoning.
6. **Automatic embedding generation** when new memories are saved, with graceful degradation if vector storage is unavailable.

## Prerequisites
- Python 3.10 or newer
- `pip` (for installing Python dependencies)
- Claude Code CLI (`claude`) in PATH

## Quick Start
Fastest path (recommended) using `setup.sh`:

```bash
git clone https://github.com/mitsuha-sh/summonai-memory-mcp.git
cd summonai-memory-mcp
bash setup.sh
```

`setup.sh` does all of the following:
- Creates `.venv` when missing
- Installs dependencies from `requirements.txt`
- Registers the MCP server globally (`claude mcp add -s user`)

Manual path (if you do not use `setup.sh`):

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
claude mcp add summonai-memory-mcp -- \
  "$(pwd)/.venv/bin/python" "$(pwd)/server.py"
```

Quick verification:

```bash
claude mcp list | rg summonai-memory-mcp
```

Initialize persona templates:

```bash
cd persona
cp USER.md.example USER.md
cp SOUL.md.example SOUL.md
# Edit to customize
```

## Global Registration
If you manage MCP servers in `~/.claude.json`, add an entry like this:

```json
{
  "mcpServers": {
    "summonai-memory-mcp": {
      "command": "/path/to/summonai-memory-mcp/.venv/bin/python",
      "args": [
        "/path/to/summonai-memory-mcp/server.py"
      ],
      "env": {
        "SUMMONAI_MEMORY_DB": "/path/to/summonai_memory.db"
      }
    }
  }
}
```

Use absolute paths and restart Claude Code after editing the config.

## Project-scoped Usage
`setup.sh` accepts an optional project directory argument:

```bash
# user-global registration (shared memory across projects)
bash setup.sh

# project-scoped registration (isolated memory per project)
bash setup.sh /abs/path/to/your-project
```

Scope guidelines:
- `scope_type=user`: share conversation history across projects for the same user.
- `scope_type=project`: isolate history per project to avoid cross-project contamination.
- `scope_id`: use a stable project identifier (for example absolute project path or slug).

When calling `conversation_save` / `conversation_load_recent`, pass matching `scope_type` and `scope_id` values.

## Configuration
Set `SUMMONAI_MEMORY_DB` to point at the SQLite file you want to use. If unset, the server creates `db/summonai_memory.db`
next to the code.

Example `settings.json` snippet for `claude mcp` registration:

```json
{
  "mcpServers": {
    "summonai-memory-mcp": {
      "command": "/path/to/.venv/bin/python",
      "args": [
        "/path/to/summonai-memory-mcp/server.py"
      ],
      "env": {
        "SUMMONAI_MEMORY_DB": "/path/to/summonai_memory.db"
      }
    }
  }
}
```

Adjust `/path/to/*` placeholders to your own workspace layout.

## SessionStart Hook Setup
Use `scripts/session_start_memory_context.sh` to auto-load recent memory context when Claude Code starts:

```json
{
  "hooks": {
    "SessionStart": [
      {
        "type": "command",
        "command": "bash /abs/path/to/summonai-memory-mcp/scripts/session_start_memory_context.sh"
      }
    ]
  }
}
```

The script uses `scripts/session_start_memory_context.py` internally and resolves runtime identity in this order:
`environment > .summonai/memory.toml > tmux option/payload/cwd defaults`.

Project-local config is optional. Copy `.summonai/memory.toml.example` to `.summonai/memory.toml` in each project:

```toml
agent_id = "default"
project = "example-project"
scope_type = "project"
scope_id = "example-project"
persona_dir = "/absolute/path/to/personas/default"
```

Supported environment overrides:
- `SUMMONAI_MEMORY_CONFIG`: explicit config file path
- `SUMMONAI_AGENT_ID`: overrides `agent_id`
- `SUMMONAI_PROJECT`: overrides `project`
- `SUMMONAI_SCOPE_TYPE` / `SUMMONAI_SCOPE_ID`: override scope filtering metadata
- `SUMMONAI_PERSONA_DIR`: overrides `persona_dir`

For shared persona, keep `USER.md` and `SOUL.md` in one directory and point each project's `persona_dir` at that source.

## Stop Hook Setup
Use `scripts/stop_hook_conversation_save.sh` to auto-save session transcripts to `conversation_save` at session end:

```json
{
  "hooks": {
    "Stop": [
      {
        "type": "command",
        "command": "bash /abs/path/to/summonai-memory-mcp/scripts/stop_hook_conversation_save.sh"
      }
    ]
  }
}
```

You can also run the Python bridge directly:

```bash
cat stop_event.json | python scripts/stop_hook_conversation_save.py
```

Expected JSON fields:
- `session_id` (or `conversation_id`)
- `agent_id` (or `agent`)
- `project` (optional)
- `task_id` or `cmd_id` (optional)
- `ended_at` or `timestamp` (optional; falls back to now)
- `transcript` (string) or `messages` (array)
- `transcript_path` or `transcriptPath` (optional JSONL transcript path)

Transcript extraction priority:
`transcript > messages > transcript_path > last_assistant_message`

## Available Tools
The server exposes these tools (via `mcp.tool()`):
- `memory_search(query, memory_type, tags, top_k, min_importance, include_invalid, after, before)` - hybrid retrieval (FTS5 + vector reranking + filters).
- `memory_save(content, memory_type, category, importance, emotional_impact, source_context, source_agent, source_cmd, tags_csv)` - save memory and generate embeddings automatically.
- `memory_invalidate(memory_id, reason)` - expire a memory record and append reason metadata.
- `memory_stats()` - return JSON stats (counts, tags, averages).
- `memory_load(min_importance, tags, memory_type, after, before)` - fetch startup memory context as plain text.
- `conversation_save(session_id, agent_id, project, task_id, transcript, ended_at, scope_type, scope_id)` - save session transcript chunks with dedupe and scope metadata.
- `conversation_load_recent(agent_id, project, scope_type, scope_id, limit_chunks, since_days)` - load recent chunks for session continuity. `agent_id` is required; `project` is optional. When `project` is omitted and no scope filter is provided, chunks are loaded across projects for the same agent.

## CLAUDE.md Guide
Document the prompts or persona cues you expect callers to store in memory by following `docs/CLAUDE_TEMPLATE.md`.
That template explains how to call `memory_save`, how tagging should work, how to reload memories during session start, and why the tag taxonomy is user-defined.

## Schema
- `memories`: core table (type, category, content, source metadata, importance, emotional_impact, bi-temporal timestamps, confidence).
- `tags`: many-to-many tags per memory.
- `action_triggers`: rule definitions that can alert, suggest commands, or update dashboards.
- `decision_patterns`: store reasoning snippets for observed situations.
- `memories_fts`: FTS5 virtual table synced by triggers for fast text search.
- `memories_vec`: `sqlite-vec` table storing 512-d embeddings for vector retrieval.
- `conversation_sessions`: session metadata for stop-hook auto-saved conversations.
- `conversation_chunks`: chunked transcript records keyed by `(session_id, chunk_index)` and `(session_id, chunk_hash)` for idempotency, with `scope_type/scope_id` for isolation.
- `conversation_chunks_fts`: FTS5 trigram index synced by triggers for conversation retrieval.

## License
MIT
