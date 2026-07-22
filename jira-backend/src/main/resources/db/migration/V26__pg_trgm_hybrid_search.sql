-- Hybrid search, tiered (not a linear-weighted blend — different scoring scales don't
-- combine meaningfully):
--   1. Exact/prefix issue-key match   — always highest priority
--   2. Full-text search (V23-V25)     — stemmed, ranked word matches
--   3. pg_trgm fuzzy fallback         — typo tolerance / partial word, only used when
--                                       tiers 1-2 don't fill a page of results
--
-- Tier 3 uses word_similarity()/"<%" rather than similarity()/"%": plain similarity()
-- compares two WHOLE strings, so a one-word typo query against a full sentence or
-- multi-word title gets diluted below the match threshold (verified: "webbhook" vs a
-- 48-char title scores 0.16 with similarity(), well under the 0.3 default — but 0.7
-- with word_similarity(), which finds the best-matching substring instead of comparing
-- string-to-string).

CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- Tier 1: case-insensitive prefix lookup on issue_key ("SP1-3" -> SP1-3, SP1-30..39).
CREATE INDEX idx_issue_key_lower_pattern ON issues (lower(issue_key) text_pattern_ops);

-- Tier 3: trigram indexes backing the "<%" word-similarity operator.
CREATE INDEX idx_issue_title_trgm ON issues USING GIN (lower(title) gin_trgm_ops);
CREATE INDEX idx_issue_description_trgm ON issues USING GIN (lower(coalesce(description, '')) gin_trgm_ops);
CREATE INDEX idx_comment_content_trgm ON comments USING GIN (lower(content) gin_trgm_ops);
