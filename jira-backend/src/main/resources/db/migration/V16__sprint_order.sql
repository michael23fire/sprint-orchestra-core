-- Jira-style backlog ordering: future sprints are manually reorderable via sprint_order.
-- Completed/active use status grouping; sprint_order still backfilled for consistency.

ALTER TABLE sprints
    ADD COLUMN sprint_order INTEGER NOT NULL DEFAULT 0;

-- Assign per-space order by start_date (nulls last), then id — preserves current relative order.
WITH ranked AS (
    SELECT
        id,
        ROW_NUMBER() OVER (
            PARTITION BY space_id
            ORDER BY start_date ASC NULLS LAST, id ASC
        ) - 1 AS ord
    FROM sprints
)
UPDATE sprints s
SET sprint_order = ranked.ord
FROM ranked
WHERE s.id = ranked.id;
