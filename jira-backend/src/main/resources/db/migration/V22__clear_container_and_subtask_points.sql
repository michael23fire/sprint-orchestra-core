-- Sprint estimates belong to top-level Story/Task/Bug work. Keeping points on
-- epics or subtasks would double-count commitment and historical velocity.
UPDATE issues
SET story_points = NULL
WHERE LOWER(issue_type) IN ('epic', 'subtask')
  AND story_points IS NOT NULL;

UPDATE sprint_issue_history
SET points_at_start = NULL,
    points_at_end = NULL
WHERE LOWER(issue_type) IN ('epic', 'subtask')
  AND (points_at_start IS NOT NULL OR points_at_end IS NOT NULL);
