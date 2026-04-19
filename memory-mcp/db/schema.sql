-- ============================================================
-- SummonAI Memory DB Schema
-- ============================================================

-- ============================================================
-- Core: memories
-- ============================================================

CREATE TABLE IF NOT EXISTS memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,

    -- Classification
    memory_type TEXT NOT NULL CHECK(memory_type IN (
        'episodic',
        'semantic',
        'procedural',
        'idea'
    )),
    memory_bucket TEXT NOT NULL DEFAULT 'knowledge' CHECK(memory_bucket IN (
        'code',
        'knowledge',
        'content'
    )),
    category TEXT,

    -- Content
    content TEXT NOT NULL,
    source_context TEXT,
    source_agent TEXT,
    source_cmd TEXT,

    -- Weighting
    importance INTEGER NOT NULL DEFAULT 5 CHECK(importance BETWEEN 1 AND 10),
    emotional_impact REAL DEFAULT 0.0 CHECK(emotional_impact BETWEEN -10.0 AND 10.0),
    confidence REAL DEFAULT 1.0 CHECK(confidence BETWEEN 0.0 AND 1.0),

    -- Access tracking
    access_count INTEGER DEFAULT 0,
    last_accessed_at TEXT,

    -- Bi-temporal
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime')),
    valid_from TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime')),
    valid_until TEXT
);

-- ============================================================
-- Tags
-- ============================================================

CREATE TABLE IF NOT EXISTS tags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_id INTEGER NOT NULL REFERENCES memories(id),
    tag TEXT NOT NULL,
    UNIQUE(memory_id, tag)
);

-- ============================================================
-- Action Triggers
-- ============================================================

CREATE TABLE IF NOT EXISTS action_triggers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    description TEXT,

    condition_type TEXT NOT NULL CHECK(condition_type IN (
        'pattern_count',
        'goal_stale',
        'schedule',
        'memory_match'
    )),
    condition_params TEXT NOT NULL,

    action_type TEXT NOT NULL CHECK(action_type IN (
        'suggest_cmd',
        'auto_investigate',
        'alert',
        'dashboard_update'
    )),
    action_params TEXT NOT NULL,

    cooldown_hours INTEGER DEFAULT 24,
    last_fired_at TEXT,
    enabled INTEGER DEFAULT 1,

    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime'))
);

-- ============================================================
-- Decision Patterns
-- ============================================================

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

-- ============================================================
-- Layer2 Decay (removed in memory v3)
-- ============================================================
-- Structured memories (Layer2) are no longer auto-decayed.
-- Recency control is handled by conversation_load_recent for Layer1.

-- ============================================================
-- Memory links (associative graph)
-- ============================================================

CREATE TABLE IF NOT EXISTS memory_links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_memory_id INTEGER NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    target_memory_id INTEGER NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    relation_type TEXT NOT NULL CHECK (relation_type IN (
        'derived_from',
        'semantic_sim',
        'supports',
        'contradicts',
        'temporal_next'
    )),
    strength REAL NOT NULL DEFAULT 0.8 CHECK (strength >= 0.0 AND strength <= 1.0),
    source TEXT NOT NULL DEFAULT 'manual',
    note TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime')),
    UNIQUE(source_memory_id, target_memory_id, relation_type)
);

-- ============================================================
-- Indexes
-- ============================================================

CREATE INDEX IF NOT EXISTS idx_memories_type ON memories(memory_type);
CREATE INDEX IF NOT EXISTS idx_memories_importance ON memories(importance DESC);
CREATE INDEX IF NOT EXISTS idx_memories_created ON memories(created_at);
CREATE INDEX IF NOT EXISTS idx_memories_valid ON memories(valid_from, valid_until);
CREATE INDEX IF NOT EXISTS idx_memories_category ON memories(category);
CREATE INDEX IF NOT EXISTS idx_memories_source_agent ON memories(source_agent);
CREATE INDEX IF NOT EXISTS idx_memories_bucket ON memories(memory_bucket, importance DESC, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_tags_tag ON tags(tag);
CREATE INDEX IF NOT EXISTS idx_tags_memory ON tags(memory_id);
CREATE INDEX IF NOT EXISTS idx_triggers_enabled ON action_triggers(enabled);
CREATE INDEX IF NOT EXISTS idx_memory_links_source ON memory_links(source_memory_id, strength DESC);
CREATE INDEX IF NOT EXISTS idx_memory_links_target ON memory_links(target_memory_id);
CREATE INDEX IF NOT EXISTS idx_memory_links_relation ON memory_links(relation_type, strength DESC);

-- ============================================================
-- FTS5: Full-text search
-- ============================================================

CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
    content,
    source_context,
    category,
    content='memories',
    content_rowid='id',
    tokenize='trigram'
);

-- FTS5 sync triggers
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

-- ============================================================
-- Phase 3: sqlite-vec vector search
-- ============================================================

CREATE VIRTUAL TABLE IF NOT EXISTS memories_vec USING vec0(
    memory_id INTEGER PRIMARY KEY,
    embedding float[512]
);

-- ============================================================
-- Phase 1: Conversation memory (auto-saved on stop hook)
-- ============================================================

CREATE TABLE IF NOT EXISTS conversation_sessions (
    session_id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    project TEXT,
    task_id TEXT,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    message_count INTEGER DEFAULT 0,
    token_estimate INTEGER DEFAULT 0,
    summary TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS conversation_chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES conversation_sessions(session_id),
    chunk_index INTEGER NOT NULL,
    chunk_hash TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    project TEXT,
    task_id TEXT,
    started_at TEXT NOT NULL,
    ended_at TEXT NOT NULL,
    message_count INTEGER NOT NULL DEFAULT 0,
    token_estimate INTEGER DEFAULT 0,
    summary TEXT,
    content TEXT NOT NULL,
    scope_type TEXT NOT NULL DEFAULT 'project',
    scope_id TEXT NOT NULL DEFAULT 'global',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime')),
    retention_score REAL NOT NULL DEFAULT 1.0,
    recall_count INTEGER NOT NULL DEFAULT 0,
    promoted_to_memory_id INTEGER REFERENCES memories(id),
    consolidated_at TEXT,
    archived_at TEXT,
    UNIQUE(session_id, chunk_index),
    UNIQUE(session_id, chunk_hash)
);

CREATE INDEX IF NOT EXISTS idx_conv_sessions_agent_project_ended
ON conversation_sessions(agent_id, project, ended_at DESC);
CREATE INDEX IF NOT EXISTS idx_conv_chunks_session_index
ON conversation_chunks(session_id, chunk_index);
CREATE INDEX IF NOT EXISTS idx_conv_chunks_agent_project_ended
ON conversation_chunks(agent_id, project, ended_at DESC);
CREATE INDEX IF NOT EXISTS idx_conv_chunks_created
ON conversation_chunks(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_conv_chunks_scope_ended
ON conversation_chunks(scope_type, scope_id, ended_at DESC);

CREATE VIRTUAL TABLE IF NOT EXISTS conversation_chunks_fts USING fts5(
    summary,
    content,
    project,
    task_id,
    content='conversation_chunks',
    content_rowid='id',
    tokenize='trigram'
);

CREATE TRIGGER IF NOT EXISTS conversation_chunks_ai AFTER INSERT ON conversation_chunks BEGIN
    INSERT INTO conversation_chunks_fts(rowid, summary, content, project, task_id)
    VALUES (new.id, new.summary, new.content, new.project, new.task_id);
END;

CREATE TRIGGER IF NOT EXISTS conversation_chunks_ad AFTER DELETE ON conversation_chunks BEGIN
    INSERT INTO conversation_chunks_fts(conversation_chunks_fts, rowid, summary, content, project, task_id)
    VALUES ('delete', old.id, old.summary, old.content, old.project, old.task_id);
END;

CREATE TRIGGER IF NOT EXISTS conversation_chunks_au AFTER UPDATE ON conversation_chunks BEGIN
    INSERT INTO conversation_chunks_fts(conversation_chunks_fts, rowid, summary, content, project, task_id)
    VALUES ('delete', old.id, old.summary, old.content, old.project, old.task_id);
    INSERT INTO conversation_chunks_fts(rowid, summary, content, project, task_id)
    VALUES (new.id, new.summary, new.content, new.project, new.task_id);
END;
