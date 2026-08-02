-- Fixes a real gap found via live testing (not a theoretical one): migration 002's search_vector was
-- generated from `content` only. `content` is the chunk's TEXT (title+description, comment body,
-- attachment text) — it does NOT contain the issue's own key string unless that key happens to be
-- mentioned in the prose. So a lexical query for an exact key like "ATLAS-6" matched ZERO rows, even
-- though README.md and ARCHITECTURE.md both describe the lexical tier as the answer to "opaque
-- identifiers embeddings can't match." That claim was only true in intent, not in the actual indexed
-- column, until this migration.
--
-- A generated column's expression can't be ALTERed in place in Postgres — drop and recreate (which
-- also drops the GIN index built on it; recreated below).

ALTER TABLE chunks DROP COLUMN IF EXISTS search_vector;

ALTER TABLE chunks
    ADD COLUMN search_vector tsvector
        GENERATED ALWAYS AS (to_tsvector('english', issue_key || ' ' || content)) STORED;

CREATE INDEX IF NOT EXISTS idx_chunks_search_vector ON chunks USING GIN (search_vector);
