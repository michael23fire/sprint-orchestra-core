-- Work types are stored in issues.issue_type and must not be duplicated as labels.
DELETE FROM issue_labels
WHERE LOWER(TRIM(label)) IN ('bug', 'story', 'epic');
