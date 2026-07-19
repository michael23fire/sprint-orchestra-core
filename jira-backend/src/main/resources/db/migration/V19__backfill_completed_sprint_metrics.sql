-- Best-effort backfill for already closed sprints. Older data cannot reveal
-- work that was moved away at close time, but current membership is still more
-- useful than showing no historical metric at all.
INSERT INTO sprint_issue_history (
    sprint_id, issue_id, issue_key, issue_type, initial_scope,
    points_at_start, points_at_end, status_at_end, outcome, added_at)
SELECT i.sprint_id,
       i.id,
       i.issue_key,
       i.issue_type,
       TRUE,
       i.story_points,
       i.story_points,
       i.status,
       CASE WHEN i.status = 'done' THEN 'completed' ELSE 'carried_over' END,
       COALESCE(s.created_at, CURRENT_TIMESTAMP)
FROM issues i
JOIN sprints s ON s.id = i.sprint_id
WHERE s.status = 'completed'
ON CONFLICT (sprint_id, issue_id) DO NOTHING;

UPDATE sprints
SET initial_committed_points = 0,
    initial_completed_points = 0,
    final_scope_points = 0,
    completed_points = 0,
    initial_issue_count = 0,
    completed_issue_count = 0,
    final_issue_count = 0,
    unestimated_issue_count = 0
WHERE status = 'completed'
  AND initial_issue_count IS NULL;

UPDATE sprints s
SET initial_committed_points = metrics.total_points,
    initial_completed_points = metrics.done_points,
    final_scope_points = metrics.total_points,
    completed_points = metrics.done_points,
    initial_issue_count = metrics.total_issues,
    completed_issue_count = metrics.done_issues,
    final_issue_count = metrics.total_issues,
    unestimated_issue_count = metrics.unestimated_issues
FROM (
    SELECT sprint_id,
           COALESCE(SUM(CASE
               WHEN LOWER(issue_type) IN ('story', 'task', 'bug') THEN COALESCE(points_at_end, 0)
               ELSE 0 END), 0)::INTEGER AS total_points,
           COALESCE(SUM(CASE
               WHEN LOWER(issue_type) IN ('story', 'task', 'bug') AND outcome = 'completed'
                   THEN COALESCE(points_at_end, 0)
               ELSE 0 END), 0)::INTEGER AS done_points,
           COUNT(*) FILTER (
               WHERE LOWER(issue_type) IN ('story', 'task', 'bug'))::INTEGER AS total_issues,
           COUNT(*) FILTER (
               WHERE LOWER(issue_type) IN ('story', 'task', 'bug') AND outcome = 'completed')::INTEGER AS done_issues,
           COUNT(*) FILTER (
               WHERE LOWER(issue_type) IN ('story', 'task', 'bug')
                 AND (points_at_end IS NULL OR points_at_end <= 0))::INTEGER AS unestimated_issues
    FROM sprint_issue_history
    GROUP BY sprint_id
) metrics
WHERE s.id = metrics.sprint_id
  AND s.status = 'completed'
  AND s.initial_issue_count = 0;
