-- V008: Add recall_count to memories for TRI ranking R-axis

ALTER TABLE memories
ADD COLUMN recall_count INTEGER NOT NULL DEFAULT 0;

CREATE INDEX IF NOT EXISTS idx_memories_recall_count
ON memories(recall_count DESC, access_count DESC);
