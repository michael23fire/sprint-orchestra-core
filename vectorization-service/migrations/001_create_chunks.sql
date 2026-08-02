-- Vector store schema for the vectorization-service.
-- Apply once against this service's OWN database (see docker-compose `vecdb`), which must be a
-- Postgres with the pgvector extension available (image: pgvector/pgvector).

CREATE EXTENSION IF NOT EXISTS vector;

-- One row per embedded chunk. `id` is a deterministic key (issue:{id}, comment:{id},
-- attachment:{id}#{index}) so upserts overwrite in place. `embedding` participates in similarity
-- search; every other column is metadata used for permission filtering (space_id) and for linking a
-- search hit back to its source (chunk_type + source_id + issue_key).
CREATE TABLE IF NOT EXISTS chunks (
    id          TEXT PRIMARY KEY,
    embedding   vector(1024) NOT NULL,          -- must match VEC_EMBEDDING_DIM (voyage-3 = 1024)
    chunk_type  TEXT NOT NULL,                  -- 'issue' | 'comment' | 'attachment'
    issue_id    BIGINT NOT NULL,
    issue_key   TEXT NOT NULL,
    space_id    BIGINT NOT NULL,
    source_id   BIGINT NOT NULL,                -- issue_id / comment_id / attachment_id
    chunk_index INT NOT NULL DEFAULT 0,
    content     TEXT NOT NULL,                  -- original text, for display / citation snippets
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Delete-by-issue (issue deletion cascade) and delete/replace-by-source (re-embed) are the hot
-- write paths — index them.
CREATE INDEX IF NOT EXISTS idx_chunks_issue_id ON chunks (issue_id);
CREATE INDEX IF NOT EXISTS idx_chunks_source ON chunks (chunk_type, source_id);

-- Approximate-nearest-neighbour index for cosine similarity, used by the AI service at query time.
-- HNSW gives good recall/latency without needing to pick a list count up front (unlike IVFFlat).
CREATE INDEX IF NOT EXISTS idx_chunks_embedding
    ON chunks USING hnsw (embedding vector_cosine_ops);
