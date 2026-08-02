-- Sprint metadata + issue-to-sprint linkage — closes a real gap: without this, semantic search over
-- a sprint's own goal text is impossible (the goal never appears verbatim on any issue), and
-- structured queries like "how many issues are in the active sprint" have no sprint dimension to
-- filter on at all. See jira-backend's SprintContentChangedEvent for the full "why" on the goal text.
--
-- One row per sprint, same "own copy of the metadata, kept current via Kafka events" pattern as
-- migrations/004 (issues) and migrations/005 (issue_changes) — this service never reaches into
-- jira-backend's tables directly.
CREATE TABLE IF NOT EXISTS sprints (
    sprint_id                  BIGINT PRIMARY KEY,
    sprint_name                TEXT NOT NULL,
    space_id                   BIGINT NOT NULL,
    goal                       TEXT,
    start_date                 DATE,
    end_date                   DATE,
    status                     TEXT,                 -- 'future' | 'active' | 'completed'
    initial_committed_points   INT,
    initial_completed_points   INT,
    final_scope_points         INT,
    completed_points           INT,
    initial_issue_count        INT,
    completed_issue_count      INT,
    final_issue_count          INT,
    unestimated_issue_count    INT,
    ingested_at                TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_sprints_space ON sprints (space_id);
CREATE INDEX IF NOT EXISTS idx_sprints_space_status ON sprints (space_id, status);

-- Which sprint an issue is CURRENTLY in — a snapshot column on the issues table (migrations/004),
-- not a join through jira-backend's own sprint_id foreign key (this service's `issues` table has no
-- FK relationship to `sprints`; both are independently upserted copies fed by their own Kafka events,
-- kept consistent by the producer, not by a database constraint).
ALTER TABLE issues ADD COLUMN IF NOT EXISTS sprint_id BIGINT;
ALTER TABLE issues ADD COLUMN IF NOT EXISTS sprint_name TEXT;

CREATE INDEX IF NOT EXISTS idx_issues_space_sprint ON issues (space_id, sprint_id);
