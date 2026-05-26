-- Soft-delete for spaces: keep rows for audit/recovery; hide from normal queries via deleted_at IS NULL.

ALTER TABLE spaces
    ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMP(6) WITH TIME ZONE NULL;

CREATE INDEX IF NOT EXISTS idx_spaces_deleted_at ON spaces (deleted_at);

-- Allow re-using space_key after a space was soft-deleted (only non-deleted rows must be unique).
ALTER TABLE spaces DROP CONSTRAINT IF EXISTS uk_spaces_space_key;
ALTER TABLE spaces DROP CONSTRAINT IF EXISTS spaces_space_key_key;

DROP INDEX IF EXISTS uk_spaces_space_key;
DROP INDEX IF EXISTS spaces_space_key_key;
DROP INDEX IF EXISTS idx_spaces_space_key;

CREATE UNIQUE INDEX IF NOT EXISTS uk_spaces_space_key_active ON spaces (space_key) WHERE deleted_at IS NULL;
