ALTER TABLE users ADD COLUMN github_id VARCHAR(255);

CREATE UNIQUE INDEX uk_users_github_id ON users (github_id) WHERE github_id IS NOT NULL;
