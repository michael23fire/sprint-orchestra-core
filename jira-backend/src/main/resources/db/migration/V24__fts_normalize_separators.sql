-- V23's tsvector fed raw title/description/content straight into to_tsvector(). Postgres's
-- default parser treats runs of "-", "_", "/", "." as hyphenated/URL-like compound tokens
-- (tokentypes hword/file), which turns a hyphen- or slash-heavy query (e.g. a filename like
-- "metrics-wire-dlq-depth-and-webhook-failure-rate-.csv") into a strict phrase/proximity match
-- instead of "these words appear". Worse, the compound "whole word" lexeme it derives depends on
-- surrounding punctuation, so the same string tokenized in isolation (the user's query) and
-- embedded in a sentence (the stored content) can produce two different lexemes that never match.
--
-- Fix: normalize those separators to spaces before tokenizing, both when generating the indexed
-- tsvector and when building the search tsquery (see IssueRepository/CommentRepository). This
-- turns compound identifiers into plain word sequences on both sides, so word-order/adjacency no
-- longer matters and the tokenization is stable regardless of context.

DROP INDEX IF EXISTS idx_issue_search_vector;
ALTER TABLE issues DROP COLUMN search_vector;
ALTER TABLE issues
    ADD COLUMN search_vector tsvector
    GENERATED ALWAYS AS (
        setweight(to_tsvector('english', regexp_replace(coalesce(title, ''), '[-_/.]+', ' ', 'g')), 'A') ||
        setweight(to_tsvector('english', regexp_replace(coalesce(description, ''), '[-_/.]+', ' ', 'g')), 'B')
    ) STORED;
CREATE INDEX idx_issue_search_vector ON issues USING GIN (search_vector);

DROP INDEX IF EXISTS idx_comment_search_vector;
ALTER TABLE comments DROP COLUMN search_vector;
ALTER TABLE comments
    ADD COLUMN search_vector tsvector
    GENERATED ALWAYS AS (
        to_tsvector('english', regexp_replace(coalesce(content, ''), '[-_/.]+', ' ', 'g'))
    ) STORED;
CREATE INDEX idx_comment_search_vector ON comments USING GIN (search_vector);
