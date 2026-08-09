-- Assignee and story points on the structured issue metadata table.
--
-- Both were already in jira-backend (Issue.assignee, Issue.storyPoints) and both were already visible
-- in the product UI, but neither ever reached this service — so anything reading the structured index
-- was blind to *who owns a ticket* and *how big it is*. Two consequences that mattered in practice:
--
--   1. `sprint_recovery`'s risk signals could say "ATC-170 has been blocked for 5 days" but never
--      "...and Maya Chen owns it", so its generated Jira comments had to ask a human to supply the
--      owner the system could have known. `RiskSignal.signal_type` even declares `owner_overloaded`,
--      which was impossible to compute without this column.
--   2. Sprint completion was measured purely by issue COUNT. In real agile practice a sprint is
--      committed and tracked in story points; 8 small tickets done out of 10 reads as "80% complete"
--      even when the 2 that remain carry most of the sprint's weight.
--
-- Nullable throughout: an unassigned or unestimated issue is a normal, meaningful state (and
-- `unestimated_issue_count` on `sprints` already tracks the latter at sprint level), not missing data.
ALTER TABLE issues ADD COLUMN IF NOT EXISTS assignee_id BIGINT;
ALTER TABLE issues ADD COLUMN IF NOT EXISTS assignee_name TEXT;
ALTER TABLE issues ADD COLUMN IF NOT EXISTS story_points INTEGER;

-- Supports "what else is this person carrying in this sprint" — the read behind the owner_overloaded
-- signal, which scans one sprint's issues grouped by assignee.
CREATE INDEX IF NOT EXISTS idx_issues_space_assignee ON issues (space_id, assignee_id);
