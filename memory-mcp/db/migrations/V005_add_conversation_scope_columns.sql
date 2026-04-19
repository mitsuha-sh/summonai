-- V005: Add scope columns for user/project isolation on conversation chunks

ALTER TABLE conversation_chunks
ADD COLUMN scope_type TEXT NOT NULL DEFAULT 'project';

ALTER TABLE conversation_chunks
ADD COLUMN scope_id TEXT NOT NULL DEFAULT 'global';

-- Backfill existing rows: keep project conversations isolated per project name.
UPDATE conversation_chunks
SET scope_id = COALESCE(NULLIF(project, ''), 'global')
WHERE scope_id = 'global';

-- Rows without project become user-scoped global memory.
UPDATE conversation_chunks
SET scope_type = 'user'
WHERE scope_id = 'global';

CREATE INDEX IF NOT EXISTS idx_conv_chunks_scope_ended
ON conversation_chunks(scope_type, scope_id, ended_at DESC);
