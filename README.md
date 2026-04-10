# summonai

AI assistant framework with persistent memory and task orchestration.

## Quick Start

```bash
git clone --recursive https://github.com/mitsuha-sh/summonai.git
cd summonai
make setup   # registers MCPs, installs deps, configures hooks
make start   # start/attach zellij session "summonai" and run claude in main pane
```

## What `make setup` Does

1. Initializes git submodules
2. Creates memory-mcp venv and installs dependencies (needed for hooks)
3. Registers MCP servers in `.mcp.json` (project scope)
4. Configures Claude Code hooks in `.claude/settings.json`:
   - **SessionStart**: injects persona (USER.md/SOUL.md), memory restore instructions
   - **Stop**: auto-saves conversation to memory DB
5. Copies persona templates if `USER.md`/`SOUL.md` don't exist yet

## Prerequisites

- `git`, `python3`, `uv`, `jq`
- `claude` CLI (Claude Code)

## Repository Layout

```
summonai/
├── CLAUDE.md                 # project-level execution rules
├── setup.sh                  # one-command setup
├── Makefile                  # make setup / make start
├── .claude/settings.json     # hooks config (git-tracked)
├── .mcp.json                 # MCP server registration (git-ignored, machine-specific)
├── config/
│   ├── task_runner.claude.json   # Claude Code as task runner
│   └── task_runner.codex.json    # Codex as task runner (demo)
├── memory-mcp/               # submodule: persistent memory server
│   ├── persona/              # USER.md, SOUL.md (your persona files)
│   └── scripts/              # session hooks
├── task-mcp/                 # submodule: task orchestration server
├── scripts/
│   └── demo_task_agent.py    # demo task runner
└── examples/
    └── hello-task.md         # sample task payload
```

## Persona

Edit `memory-mcp/persona/USER.md` and `memory-mcp/persona/SOUL.md` to define your profile and your AI assistant's personality. These are injected at every session start.

## Task Runner

Default runner is `config/task_runner.claude.json` (Claude Code). To change, update `SUMMONAI_TASK_RUNNER_CONFIG` in `.mcp.json`.

## Zellij Pane-Based Sub Agents

`summonai-task-mcp` can run sub agents through a zellij pane-based runner so each task executes in its own pane/session context.
This repository keeps the setup entrypoint and delegates operational details (runner modes, pane lifecycle, and configuration options) to the `summonai-task-mcp` README.

## Start Workflow

- `make start` uses `zellij attach --create summonai` behavior.
- If session `summonai` does not exist, it is created with layout `zellij/layouts/summonai-start.kdl` and the main pane starts `claude` in interactive mode.
- If session `summonai` already exists, `make start` attaches to that session (no duplicate session).

## Notes

- `.mcp.json` contains absolute paths and is git-ignored. Regenerate with `make setup`.
- `.claude/settings.json` contains hooks config and is git-tracked.
- Submodule repos: [summonai-memory-mcp](https://github.com/mitsuha-sh/summonai-memory-mcp), [summonai-task-mcp](https://github.com/mitsuha-sh/summonai-task-mcp)
