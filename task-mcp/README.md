# summonai-task-mcp

`summonai-task-mcp` is a task orchestration MCP server for SummonAI workflows.

## Features (P0)

- SQLite-backed task store (`tasks`, `task_events`, `task_reviews`)
- Auto-migration on startup
- WAL mode enabled
- MCP tools:
  - `task_create`
  - `task_get`
  - `task_list`
  - `task_update`
  - `task_complete`
- Status transition validation
- Event logging for `create`, `update`, `complete`

## Requirements

- Python 3.11+
- `uv`

## Setup

```bash
uv sync --dev
```

Run server (stdio MCP):

```bash
SUMMONAI_TASK_DB=/absolute/path/to/summonai_task.db uv run python -m summonai_task.server
```

Run tests:

```bash
uv run pytest -q
```

## Configuration

Environment variables:

- `SUMMONAI_TASK_DB`: absolute path to SQLite DB file (default: `src/summonai_task/db/summonai_task.db`)
- `SUMMONAI_TASK_RUNNER_CONFIG`: optional JSON file path for worker-runner settings
- `SUMMONAI_TASK_RUNNER`: runner preset (`claude`, `codex`, `opencode`)
- `SUMMONAI_TASK_RUNNER_COMMAND`: optional shell command used as `sh -lc "<value>"`
- `SUMMONAI_TASK_RUNNER_PROJECT_DIR`: working directory for spawned runner process
- `SUMMONAI_TASK_RUNNER_ENABLED`: force enable/disable (`1/0`, `true/false`)
- `ZELLIJ_SESSION_NAME`: zellij session name for pane-based runner (used when config JSON has no `zellij_session`)

### Worker runner config JSON

When `SUMMONAI_TASK_RUNNER_CONFIG` is set, pass a JSON object like this:

```json
{
  "enabled": true,
  "runner": "codex",
  "project_dir": "/absolute/path/to/project",
  "command": ["codex", "-p", "{prompt}"],
  "env": {
    "EXTRA_FLAG": "value"
  }
}
```

`{task_id}` and `{prompt}` placeholders are supported in `command` arguments.

## Zellij Pane Runner

When runner mode is enabled with `runner: "zellij"`, `task_create` can open a dedicated zellij pane per task and send the worker bootstrap prompt automatically.

### How `zellij_session` is resolved

Set session name by either:

1. `SUMMONAI_TASK_RUNNER_CONFIG` JSON key: `zellij_session`
2. Environment variable: `ZELLIJ_SESSION_NAME`

If neither is set while runner is enabled, task creation returns `runner_started: false` with an error.

### Ready check behavior

After pane creation, the server performs readiness checks before sending work:

- waits up to 30 seconds for prompt readiness
- sends `claude`, then waits up to 30 seconds again
- only then sends task instruction prompt

If readiness times out or any startup step fails, the created pane is closed and `task_create` returns `runner_started: false` with `runner_error`.

## Additional MCP Tools

### `task_message`

Send an interactive message to a running task pane.

- Arguments:
  - `task_id` (str): target task id (must be `in_progress` and have `pane_id`)
  - `message` (str): message text to send (must be non-empty)
  - `actor_id` (str, optional): event actor id (default: `"system"`)
- Returns:
  - `task` (dict): updated task payload
  - `sent` (bool): always `true` on success
  - `pane_id` (str): target pane id

### `task_peek`

Read recent pane output for a task.

- Arguments:
  - `task_id` (str): target task id (must have `pane_id`)
  - `lines` (int, optional): number of trailing lines to return (default: `100`)
- Returns:
  - `task_id` (str)
  - `status` (str)
  - `pane_id` (str)
  - `lines` (int): normalized line count
  - `output` (str): captured pane output text

### `task_cancel`

Cancel a task and attempt to close its pane.

- Arguments:
  - `task_id` (str): target task id
  - `reason` (str, optional): cancellation reason
  - `actor_id` (str, optional): event actor id (default: `"system"`)
- Returns:
  - `task` (dict): updated task payload (`status: "cancelled"`)
  - `cancelled` (bool): `true` on success
  - `pane_closed` (bool): whether pane close succeeded
  - `pane_close_error` (str | null): close error detail when close failed

### `task_resume`

Resume an `assigned` task when its executor pane is gone.

- Arguments:
  - `task_id` (str): target task id (must be `assigned`)
  - `actor_id` (str, optional): event actor id (default: `"system"`)
- Behavior:
  - If current `pane_id` is still active, resume is skipped (no double-spawn)
  - If pane is missing, creates a new pane, boots Claude, and sends resume prompt
- Returns:
  - `task` (dict): updated task payload
  - `resumed` (bool): `true` when a new pane was created
  - `skipped` (bool): `true` when active pane already exists
  - `reason` (str | null): skip reason (e.g. `pane_already_active`)
  - `pane_id` (str | null): active pane id after the call

## Claude Code config example

```json
{
  "mcpServers": {
    "summonai-task-mcp": {
      "command": "uv",
      "args": ["run", "python", "-m", "summonai_task.server"],
      "env": {
        "SUMMONAI_TASK_DB": "/absolute/path/to/summonai_task.db"
      }
    }
  }
}
```

## Codex CLI config example

```toml
[mcp_servers.summonai-task-mcp]
command = "uv"
args = ["run", "python", "-m", "summonai_task.server"]

[mcp_servers.summonai-task-mcp.env]
SUMMONAI_TASK_DB = "/absolute/path/to/summonai_task.db"
```

## Bloom Routing

`task_create` accepts two routing parameters:

- `bloom_level` (int, 1–6): Bloom's Taxonomy level of the task.  
  1=Remember, 2=Understand, 3=Apply (default), 4=Analyze, 5=Evaluate, 6=Create
- `executor` (str | null): CLI tool name to use (e.g. `"claude"`, `"codex"`).  
  `null` means select the cheapest tier across all executors.

### `executors.toml` configuration

Create `.summonai/executors.toml` (git-ignored) to configure capability tiers:

```toml
# executor = CLI tool name (claude / codex / opencode)
# Multiple tiers with the same executor are ordered by max_bloom (cheapest first).

[[capability_tiers]]
executor = "claude"
model = "claude-haiku-4-5-20251001"
max_bloom = 3
cost_group = "low"

[[capability_tiers]]
executor = "claude"
model = "claude-sonnet-4-6"
max_bloom = 5
cost_group = "medium"

[[capability_tiers]]
executor = "claude"
model = "claude-opus-4-7"
max_bloom = 6
cost_group = "high"

[runners.claude]
template = "claude --model {model} --dangerously-skip-permissions"

# [defaults] applies when task_create is called without explicit bloom_level / executor.
[defaults]
bloom_level = 3
# executor = "claude"
```

### Selection rules

- `executor` specified: pick the cheapest tier for that executor with `max_bloom >= bloom_level`.
- `executor` unspecified: pick the globally cheapest tier with `max_bloom >= bloom_level`.
- Coverage gap: fall back to the highest `max_bloom` tier and emit a `WARN` to stderr.

### Unknown executor rejection

When `executors.toml` is present, passing an `executor` value that does not appear in any
`[[capability_tiers]]` entry raises a `ValueError` listing the available executor names.
Environments without a config file accept any executor string (legacy / no-config mode).

## Notes

- `task_id` is UUID v4 first 8 chars.
- Timestamps are stored in ISO 8601 UTC (`...Z`).
