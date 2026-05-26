ALTER TABLE users ADD COLUMN google_sub VARCHAR(255);

CREATE UNIQUE INDEX uk_users_google_sub ON users (google_sub) WHERE google_sub IS NOT NULL;
