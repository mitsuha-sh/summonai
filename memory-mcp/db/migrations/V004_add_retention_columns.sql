-- V004: Add retention scoring columns for conversation chunks

ALTER TABLE conversation_chunks ADD COLUMN retention_score REAL NOT NULL DEFAULT 1.0;
ALTER TABLE conversation_chunks ADD COLUMN recall_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE conversation_chunks ADD COLUMN consolidated_at TEXT;

CREATE INDEX IF NOT EXISTS idx_conv_chunks_retention
ON conversation_chunks(retention_score DESC, ended_at DESC);
