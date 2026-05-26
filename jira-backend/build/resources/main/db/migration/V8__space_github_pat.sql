-- Optional GitHub PAT for this space (set when bulk-importing with a token).
-- Used for repo scan, code-link refresh, and metadata fetch for private repos.
-- Never returned by the Space API.
ALTER TABLE spaces ADD COLUMN github_pat TEXT NULL;
