-- Durable output cache for paid VLM attachment extraction.  The content hash includes the prompt
-- version, provider/model, token limit and MIME type, so changing extraction behavior creates a
-- new cache key without retaining attachment bytes or source identifiers in this table.
CREATE TABLE IF NOT EXISTS vlm_result_cache (
    cache_key TEXT PRIMARY KEY,
    namespace TEXT NOT NULL,
    mime_type TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
