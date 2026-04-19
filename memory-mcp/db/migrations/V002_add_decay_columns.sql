-- V002: Add decay tracking columns for memories

ALTER TABLE memories ADD COLUMN last_decayed_at TEXT;
CREATE INDEX IF NOT EXISTS idx_memories_last_decayed ON memories(last_decayed_at);
