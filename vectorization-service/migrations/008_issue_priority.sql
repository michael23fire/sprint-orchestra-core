-- Adds `priority` to the structured `issues` snapshot (migrations/004), self-healed the same way as
-- `status`/`issueType`/`sprint` (migrations/004, 006): jira-backend already records a "priority"
-- field_change history row on every priority edit (IssueService.java's `update()` calls
-- `recordFieldChange(issue, actorUserId, "priority", oldPriority, issue.getPriority())`
-- unconditionally), but a priority-only edit never fires IssueContentChangedEvent (no re-embed
-- needed — priority isn't part of the embedded text), so the history stream is the only path that
-- keeps this column current. See VectorStore.append_issue_change's new `priority` branch.
ALTER TABLE issues ADD COLUMN IF NOT EXISTS priority TEXT;

CREATE INDEX IF NOT EXISTS idx_issues_space_priority ON issues (space_id, priority);
