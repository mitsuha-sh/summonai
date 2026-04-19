PRAGMA foreign_keys=OFF;

CREATE TABLE IF NOT EXISTS tasks_new (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    north_star TEXT,
    purpose TEXT NOT NULL,
    acceptance_criteria_json TEXT NOT NULL,
    project TEXT NOT NULL,
    priority TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN (
        'pending','assigned','in_progress','review','done','redo','cancelled'
    )),
    creator_role TEXT NOT NULL,
    assignee_id TEXT,
    assignee_role TEXT,
    assignment_role TEXT,
    parent_task_id TEXT,
    root_task_id TEXT,
    redo_of TEXT,
    batch1_qc_required INTEGER NOT NULL DEFAULT 0,
    destructive_safety_json TEXT,
    purpose_gap TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT,
    pane_id TEXT,
    FOREIGN KEY(parent_task_id) REFERENCES tasks(id),
    FOREIGN KEY(redo_of) REFERENCES tasks(id)
);

INSERT INTO tasks_new (
    id, title, north_star, purpose, acceptance_criteria_json, project, priority, status,
    creator_role, assignee_id, assignee_role, assignment_role, parent_task_id, root_task_id,
    redo_of, batch1_qc_required, destructive_safety_json, purpose_gap, metadata_json,
    created_at, updated_at, completed_at, pane_id
)
SELECT
    id, title, north_star, purpose, acceptance_criteria_json, project, priority, status,
    creator_role, assignee_id, assignee_role, assignment_role, parent_task_id, root_task_id,
    redo_of, batch1_qc_required, destructive_safety_json, purpose_gap, metadata_json,
    created_at, updated_at, completed_at, pane_id
FROM tasks;

DROP TABLE tasks;
ALTER TABLE tasks_new RENAME TO tasks;

CREATE INDEX IF NOT EXISTS idx_tasks_status_priority ON tasks(status, priority, updated_at);
CREATE INDEX IF NOT EXISTS idx_tasks_assignee ON tasks(assignee_id, status, updated_at);
CREATE INDEX IF NOT EXISTS idx_tasks_project ON tasks(project, status, updated_at);
CREATE INDEX IF NOT EXISTS idx_tasks_parent ON tasks(parent_task_id, status, updated_at);

PRAGMA foreign_keys=ON;
