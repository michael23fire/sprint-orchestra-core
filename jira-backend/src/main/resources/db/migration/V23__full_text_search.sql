-- Full-text search for issues (title + description) and comments.
-- Generated STORED tsvector columns keep the index in sync with every write
-- inside the same transaction, so no Kafka/async reindex step is needed for v1.

ALTER TABLE issues
    ADD COLUMN search_vector tsvector
    GENERATED ALWAYS AS (
        setweight(to_tsvector('english', coalesce(title, '')), 'A') ||
        setweight(to_tsvector('english', coalesce(description, '')), 'B')
    ) STORED;

CREATE INDEX idx_issue_search_vector ON issues USING GIN (search_vector);

ALTER TABLE comments
    ADD COLUMN search_vector tsvector
    GENERATED ALWAYS AS (
        to_tsvector('english', coalesce(content, ''))
    ) STORED;

CREATE INDEX idx_comment_search_vector ON comments USING GIN (search_vector);
