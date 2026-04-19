CREATE TABLE IF NOT EXISTS schema_versions (
    version INTEGER PRIMARY KEY,
    filename TEXT NOT NULL,
    applied_at TEXT NOT NULL,
    checksum TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    north_star TEXT,
    purpose TEXT NOT NULL,
    acceptance_criteria_json TEXT NOT NULL,
    project TEXT NOT NULL,
    priority TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN (
        'pending','assigned','in_progress','review','done','redo'
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
    FOREIGN KEY(parent_task_id) REFERENCES tasks(id),
    FOREIGN KEY(redo_of) REFERENCES tasks(id)
);

CREATE TABLE IF NOT EXISTS task_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(task_id) REFERENCES tasks(id)
);

CREATE TABLE IF NOT EXISTS task_reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    reviewer_id TEXT NOT NULL,
    decision TEXT NOT NULL CHECK(decision IN ('done','redo')),
    acceptance_results_json TEXT NOT NULL,
    notes TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY(task_id) REFERENCES tasks(id)
);

CREATE INDEX IF NOT EXISTS idx_tasks_status_priority ON tasks(status, priority, updated_at);
CREATE INDEX IF NOT EXISTS idx_tasks_assignee ON tasks(assignee_id, status, updated_at);
CREATE INDEX IF NOT EXISTS idx_tasks_project ON tasks(project, status, updated_at);
CREATE INDEX IF NOT EXISTS idx_tasks_parent ON tasks(parent_task_id, status, updated_at);
CREATE INDEX IF NOT EXISTS idx_task_events_task ON task_events(task_id, created_at);
