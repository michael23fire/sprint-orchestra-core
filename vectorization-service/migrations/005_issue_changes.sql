-- Append-only issue change history, fed by jira-backend's `issue_history_added` Kafka events.
--
-- Why a third table: `chunks` answers "what content is relevant" (semantic), `issues` answers "how
-- many / which ones right now" (latest-state snapshot, upserted in place). Neither can answer
-- "which issues were REOPENED" or "what did I just change in ATC-77" — those are questions about
-- *transitions*, and the snapshot's upsert semantics are precisely what destroys transitions. So
-- changes get their own append-only stream: jira-backend already records every field change
-- (old value -> new value, including title/description edits) in its `issue_history` table, and each
-- recorded row is bridged here as an event. This service never diffs anything itself — the system of
-- record for "what changed" is upstream, and duplicating diff logic downstream would just let the two
-- disagree.
--
-- `id` is jira-backend's issue_history primary key, NOT a local sequence: at-least-once Kafka
-- delivery and backfill overlap both resolve to ON CONFLICT DO NOTHING instead of duplicate rows.
CREATE TABLE IF NOT EXISTS issue_changes (
    id          BIGINT PRIMARY KEY,             -- jira-backend issue_history.id (idempotency key)
    issue_id    BIGINT NOT NULL,
    issue_key   TEXT NOT NULL,
    space_id    BIGINT NOT NULL,
    event_type  TEXT NOT NULL,                  -- 'field_change' | 'issue_created' | 'comment_created' | ...
    field_name  TEXT,                           -- for field_change: 'status' | 'description' | 'title' | ...
    from_value  TEXT,
    to_value    TEXT,
    description TEXT,                           -- human-readable summary jira-backend already writes
    actor_name  TEXT,                           -- who made the change (display name; null for system)
    changed_at  TIMESTAMPTZ NOT NULL,
    ingested_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Query paths: always space-scoped, then "recent changes" (changed_at DESC), "changes to this issue",
-- and "status transitions" (the reopen question filters field_name='status').
CREATE INDEX IF NOT EXISTS idx_issue_changes_space_time ON issue_changes (space_id, changed_at DESC);
CREATE INDEX IF NOT EXISTS idx_issue_changes_issue ON issue_changes (issue_id, changed_at DESC);
CREATE INDEX IF NOT EXISTS idx_issue_changes_space_field ON issue_changes (space_id, field_name);
