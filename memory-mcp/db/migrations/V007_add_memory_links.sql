-- V007: Add memory_links table for associative memory graph

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

CREATE INDEX IF NOT EXISTS idx_memory_links_source
ON memory_links(source_memory_id, strength DESC);

CREATE INDEX IF NOT EXISTS idx_memory_links_target
ON memory_links(target_memory_id);

CREATE INDEX IF NOT EXISTS idx_memory_links_relation
ON memory_links(relation_type, strength DESC);
