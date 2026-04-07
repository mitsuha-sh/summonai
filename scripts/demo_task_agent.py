#!/usr/bin/env python3
"""Minimal demo runner for summonai-task-mcp.

This worker simulates an agent lifecycle:
- fetch task
- move assigned -> in_progress
- complete via task_complete (status becomes review)
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

from summonai_task.server import task_complete, task_get, task_update


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: demo_task_agent.py <task_id>", file=sys.stderr)
        return 2

    task_id = sys.argv[1]
    log_path = Path(__file__).resolve().parent.parent / "reports" / "demo_runner.log"

    task = task_get(task_id, include_history=False)["task"]
    task_update(
        task_id=task_id,
        status="in_progress",
        progress_note="demo runner started",
        actor_id="demo-runner",
    )

    task_complete(
        task_id=task_id,
        summary="Demo runner completed task execution.",
        artifact_paths=["examples/hello-task.md"],
        verification="Automated via scripts/demo_task_agent.py",
        actor_id="demo-runner",
    )

    with log_path.open("a", encoding="utf-8") as f:
        f.write(f"[{_now()}] task_id={task_id} title={task['title']} completed_to=review\n")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
