-- Backs IdempotencyFilter: a caller that retries a mutating request after losing the response (not
-- after a real failure) replays the original response instead of re-executing the side effect.
-- Sequential-retry only, not a lock against true concurrent duplicates — see the filter's own comment.
CREATE TABLE idempotency_keys (
    idempotency_key VARCHAR(255) PRIMARY KEY,
    response_status INT NOT NULL,
    response_body TEXT,
    content_type VARCHAR(100),
    created_at TIMESTAMP NOT NULL DEFAULT now()
);
