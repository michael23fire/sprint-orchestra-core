-- V19 can only see issues that still reference a completed sprint. When every
-- surviving row is Done, carried-over work may already have moved elsewhere,
-- so the original denominator is unknowable. Do not present a fabricated 100%.
UPDATE sprints s
SET initial_committed_points = NULL,
    initial_completed_points = NULL,
    final_scope_points = NULL,
    completed_points = NULL,
    initial_issue_count = NULL,
    completed_issue_count = NULL,
    final_issue_count = NULL,
    unestimated_issue_count = NULL
WHERE s.status = 'completed'
  AND EXISTS (
      SELECT 1 FROM sprint_issue_history h WHERE h.sprint_id = s.id
  )
  AND NOT EXISTS (
      SELECT 1
      FROM sprint_issue_history h
      WHERE h.sprint_id = s.id
        AND h.outcome <> 'completed'
  );

-- Counts describe all participating work items. Point totals and estimate
-- coverage continue to use only Story/Task/Bug to avoid Epic/Subtask double-counting.
UPDATE sprints s
SET initial_issue_count = metrics.initial_issues,
    completed_issue_count = metrics.completed_issues,
    final_issue_count = metrics.final_issues
FROM (
    SELECT sprint_id,
           COUNT(*) FILTER (WHERE initial_scope)::INTEGER AS initial_issues,
           COUNT(*) FILTER (WHERE outcome = 'completed')::INTEGER AS completed_issues,
           COUNT(*) FILTER (WHERE outcome <> 'removed')::INTEGER AS final_issues
    FROM sprint_issue_history
    GROUP BY sprint_id
) metrics
WHERE s.id = metrics.sprint_id
  AND s.status = 'completed'
  AND s.initial_committed_points IS NOT NULL;
