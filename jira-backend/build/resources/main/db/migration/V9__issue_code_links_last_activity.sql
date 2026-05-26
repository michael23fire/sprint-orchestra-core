-- Last meaningful GitHub activity (PR updated_at, commit date, repo pushed_at, etc.)
ALTER TABLE issue_code_links ADD COLUMN last_activity_at TIMESTAMPTZ NULL;
