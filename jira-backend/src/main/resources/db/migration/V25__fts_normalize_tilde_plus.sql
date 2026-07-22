-- Same root cause as V24, different trigger characters: a leading "~" or "+" directly
-- against a digit (e.g. "~2", "+2") tokenizes as one glued lexeme ('~2', '+2') when the
-- parser sees ONLY that fragment (an isolated search query), but as a plain numeral ('2')
-- when the same fragment sits inside a full sentence (stored issue/comment text). Confirmed
-- by testing all other punctuation (#, @, %, &, *, !, $, quotes) — only "~" and "+" show
-- this isolated-vs-embedded divergence. Add them to the normalized separator set from V24.

DROP INDEX IF EXISTS idx_issue_search_vector;
ALTER TABLE issues DROP COLUMN search_vector;
ALTER TABLE issues
    ADD COLUMN search_vector tsvector
    GENERATED ALWAYS AS (
        setweight(to_tsvector('english', regexp_replace(coalesce(title, ''), '[-_/.~+]+', ' ', 'g')), 'A') ||
        setweight(to_tsvector('english', regexp_replace(coalesce(description, ''), '[-_/.~+]+', ' ', 'g')), 'B')
    ) STORED;
CREATE INDEX idx_issue_search_vector ON issues USING GIN (search_vector);

DROP INDEX IF EXISTS idx_comment_search_vector;
ALTER TABLE comments DROP COLUMN search_vector;
ALTER TABLE comments
    ADD COLUMN search_vector tsvector
    GENERATED ALWAYS AS (
        to_tsvector('english', regexp_replace(coalesce(content, ''), '[-_/.~+]+', ' ', 'g'))
    ) STORED;
CREATE INDEX idx_comment_search_vector ON comments USING GIN (search_vector);
