-- V009: Remove goals table and goal_id link from memories

DROP INDEX IF EXISTS idx_memories_goal;
DROP INDEX IF EXISTS idx_goals_status;
DROP INDEX IF EXISTS idx_goals_parent;

ALTER TABLE memories DROP COLUMN goal_id;
DROP TABLE goals;
