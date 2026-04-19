-- V001: Initial core schema

CREATE TABLE IF NOT EXISTS memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_type TEXT NOT NULL CHECK(memory_type IN ('episodic', 'semantic', 'procedural', 'idea')),
    category TEXT,
    content TEXT NOT NULL,
    source_context TEXT,
    source_agent TEXT,
    source_cmd TEXT,
    importance INTEGER NOT NULL DEFAULT 5 CHECK(importance BETWEEN 1 AND 10),
    emotional_impact REAL DEFAULT 0.0 CHECK(emotional_impact BETWEEN -10.0 AND 10.0),
    confidence REAL DEFAULT 1.0 CHECK(confidence BETWEEN 0.0 AND 1.0),
    access_count INTEGER DEFAULT 0,
    last_accessed_at TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime')),
    valid_from TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime')),
    valid_until TEXT,
    goal_id INTEGER REFERENCES goals(id)
);

CREATE TABLE IF NOT EXISTS goals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    description TEXT,
    priority INTEGER NOT NULL DEFAULT 5 CHECK(priority BETWEEN 1 AND 10),
    progress REAL DEFAULT 0.0 CHECK(progress BETWEEN 0.0 AND 1.0),
    deadline TEXT,
    parent_id INTEGER REFERENCES goals(id),
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active', 'completed', 'paused', 'abandoned')),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS tags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_id INTEGER NOT NULL REFERENCES memories(id),
    tag TEXT NOT NULL,
    UNIQUE(memory_id, tag)
);

CREATE TABLE IF NOT EXISTS action_triggers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    description TEXT,
    condition_type TEXT NOT NULL CHECK(condition_type IN ('pattern_count', 'goal_stale', 'schedule', 'memory_match')),
    condition_params TEXT NOT NULL,
    action_type TEXT NOT NULL CHECK(action_type IN ('suggest_cmd', 'auto_investigate', 'alert', 'dashboard_update')),
    action_params TEXT NOT NULL,
    cooldown_hours INTEGER DEFAULT 24,
    last_fired_at TEXT,
    enabled INTEGER DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS decision_patterns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    situation TEXT NOT NULL,
    decision TEXT NOT NULL,
    reasoning TEXT,
    confidence REAL DEFAULT 0.5,
    observation_count INTEGER DEFAULT 1,
    last_observed_at TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime'))
);

CREATE INDEX IF NOT EXISTS idx_memories_type ON memories(memory_type);
CREATE INDEX IF NOT EXISTS idx_memories_importance ON memories(importance DESC);
CREATE INDEX IF NOT EXISTS idx_memories_created ON memories(created_at);
CREATE INDEX IF NOT EXISTS idx_memories_valid ON memories(valid_from, valid_until);
CREATE INDEX IF NOT EXISTS idx_memories_goal ON memories(goal_id);
CREATE INDEX IF NOT EXISTS idx_memories_category ON memories(category);
CREATE INDEX IF NOT EXISTS idx_memories_source_agent ON memories(source_agent);
CREATE INDEX IF NOT EXISTS idx_tags_tag ON tags(tag);
CREATE INDEX IF NOT EXISTS idx_tags_memory ON tags(memory_id);
CREATE INDEX IF NOT EXISTS idx_goals_status ON goals(status);
CREATE INDEX IF NOT EXISTS idx_goals_parent ON goals(parent_id);
CREATE INDEX IF NOT EXISTS idx_triggers_enabled ON action_triggers(enabled);

CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
    content,
    source_context,
    category,
    content='memories',
    content_rowid='id',
    tokenize='trigram'
);

CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
    INSERT INTO memories_fts(rowid, content, source_context, category)
    VALUES (new.id, new.content, new.source_context, new.category);
END;

CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, content, source_context, category)
    VALUES ('delete', old.id, old.content, old.source_context, old.category);
END;

CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, content, source_context, category)
    VALUES ('delete', old.id, old.content, old.source_context, old.category);
    INSERT INTO memories_fts(rowid, content, source_context, category)
    VALUES (new.id, new.content, new.source_context, new.category);
END;

CREATE VIRTUAL TABLE IF NOT EXISTS memories_vec USING vec0(
    memory_id INTEGER PRIMARY KEY,
    embedding float[512]
);
