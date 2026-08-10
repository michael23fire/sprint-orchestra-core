-- Adds the subtask -> parent link to the structured `issues` table, as a plain denormalized column
-- (not a FK to issue_id, since the parent might not have its own row yet at ingestion time and this
-- is a read-optimized copy, not the source of truth for referential integrity — jira-backend's
-- Issue.parent FK already owns that).
--
-- Why this is needed: threading parent_key/parent_title through the Kafka message and into a
-- subtask's own embedded chunk text stopped short of persisting it here — leaving no STRUCTURED way
-- to ask "is X actually the parent of these subtasks" without guessing from prose. The
-- post-generation subtask-relationship verifier needs exactly that: given a group of co-mentioned
-- issues where some are real subtasks, the reliable way to exclude the PARENT from being flagged as
-- a miscategorized sibling is checking `parent_key`, not a heuristic guess about which key the model
-- "already investigated."
ALTER TABLE issues ADD COLUMN IF NOT EXISTS parent_key TEXT;

CREATE INDEX IF NOT EXISTS idx_issues_parent_key ON issues (parent_key);
