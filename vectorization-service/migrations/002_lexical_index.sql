-- Adds a lexical (BM25-style) retrieval path over the SAME chunk rows the vector index covers, so
-- hybrid search can fuse ranks from both signals over one candidate set (see app/db/vector_store.py
-- search_hybrid / Reciprocal Rank Fusion).
--
-- This is deliberately NOT a duplicate of jira-backend's FTS. jira-backend's tsvector lives on the
-- *original* issues/comments rows and answers "does this issue/comment match" at issue/comment
-- granularity. This tsvector lives on *chunks* (which also include attachment text that jira-backend
-- has no FTS over at all) and answers "does this specific chunk match" — the granularity RAG
-- retrieval actually needs, so a lexical hit and a vector hit can be fused rank-for-rank.

ALTER TABLE chunks
    ADD COLUMN IF NOT EXISTS search_vector tsvector
        GENERATED ALWAYS AS (to_tsvector('english', content)) STORED;

CREATE INDEX IF NOT EXISTS idx_chunks_search_vector ON chunks USING GIN (search_vector);
