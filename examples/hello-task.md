# Hello Task Example

Use this example payload when creating a first task in `summonai-task-mcp`.

```json
{
  "title": "Hello from summonai",
  "north_star": "Validate end-to-end task lifecycle",
  "purpose": "Confirm task_create triggers runner and reaches completion handoff state",
  "acceptance_criteria": [
    "runner_started is true",
    "task status reaches review after runner execution"
  ],
  "project": "summonai",
  "priority": "medium",
  "creator_role": "human",
  "assignee_id": "worker-demo",
  "assignee_role": "worker"
}
```

Expected flow: `task_create -> runner start -> task_update(in_progress) -> task_complete(review)`.
