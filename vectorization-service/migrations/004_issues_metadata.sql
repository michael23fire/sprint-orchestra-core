-- Issue-level structured metadata, one row per issue — the "data-engineering" half of retrieval.
--
-- Why this exists alongside `chunks`: semantic search over `chunks` answers "what content is
-- relevant to this question", but it structurally CANNOT answer "how many bug issues are there",
-- "list every story", "which issue changed most recently", or "filter results to status=blocked".
-- Those are structured/aggregate queries over exact metadata, not top-K similarity over prose — a
-- top-K retriever returns its best guesses, never a complete, counted set. So the agent gets a second
-- tool backed by this table (see app/api/routes.py POST /issues/query) and routes count/filter/
-- recency questions here instead of pretending semantic search can count.
--
-- This is deliberately a separate table from `chunks`, not extra columns on it: an issue is one row
-- here regardless of how many chunks its text splits into (so COUNT is exact, not a COUNT(DISTINCT)
-- over duplicated metadata), and an issue with an empty description — which produces zero chunks —
-- still appears here and still gets counted. Same "vectorization-service owns its own Kafka-fed copy"
-- principle as `chunks`: this never reaches into jira-backend's tables.
CREATE TABLE IF NOT EXISTS issues (
    issue_id    BIGINT PRIMARY KEY,
    issue_key   TEXT NOT NULL,
    space_id    BIGINT NOT NULL,
    issue_type  TEXT,                           -- 'bug' | 'story' | 'task' | 'epic' | 'subtask' | ...
    status      TEXT,                           -- 'done' | 'in_progress' | 'planned' | 'blocked' | ...
    title       TEXT,
    -- The issue's OWN lifecycle timestamps (from jira-backend), NOT the ingestion time — these are
    -- what "created in the last 3 months" / "recently updated" questions filter and order on. Nullable
    -- because the current Kafka producer doesn't emit them yet (backfill supplies them from source);
    -- see IssueIngestionMessage in app/models.py.
    created_at  TIMESTAMPTZ,
    updated_at  TIMESTAMPTZ,
    ingested_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Every structured query is space-scoped first (permission boundary), then filtered/ordered by type,
-- status, or recency — index those access paths.
CREATE INDEX IF NOT EXISTS idx_issues_space ON issues (space_id);
CREATE INDEX IF NOT EXISTS idx_issues_space_type ON issues (space_id, issue_type);
CREATE INDEX IF NOT EXISTS idx_issues_space_status ON issues (space_id, status);
CREATE INDEX IF NOT EXISTS idx_issues_space_updated ON issues (space_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_issues_space_created ON issues (space_id, created_at DESC);
