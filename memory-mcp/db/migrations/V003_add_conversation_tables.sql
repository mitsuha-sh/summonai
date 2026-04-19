-- V003: Add conversation session/chunk persistence

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
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime')),
    promoted_to_memory_id INTEGER REFERENCES memories(id),
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
