-- V006: Add memory_bucket and remove legacy Layer2 decay artifacts

ALTER TABLE memories
ADD COLUMN memory_bucket TEXT NOT NULL DEFAULT 'knowledge'
CHECK (memory_bucket IN ('code', 'knowledge', 'content'));

-- Promote obvious legacy tags into v3 buckets.
UPDATE memories
SET memory_bucket = 'code'
WHERE id IN (
    SELECT memory_id FROM tags
    WHERE tag = 'code'
       OR tag LIKE 'core:%'
       OR tag LIKE 'policy:%'
);

UPDATE memories
SET memory_bucket = 'content'
WHERE id IN (
    SELECT memory_id FROM tags
    WHERE tag = 'content'
       OR tag LIKE 'work:content%'
);

CREATE INDEX IF NOT EXISTS idx_memories_bucket ON memories(memory_bucket, importance DESC, created_at DESC);

DROP INDEX IF EXISTS idx_memories_last_decayed;
ALTER TABLE memories DROP COLUMN last_decayed_at;
DROP TABLE IF EXISTS decay_config;
