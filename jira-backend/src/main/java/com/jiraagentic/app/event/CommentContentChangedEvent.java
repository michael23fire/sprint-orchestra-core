package com.jiraagentic.app.event;

import java.time.Instant;

/**
 * Published after a comment is created or edited, on the same transactional boundary. The
 * vectorization service upserts a single vector keyed by comment id (see the ingestion service's
 * {@code comment:{id}} chunk key), so repeated edits overwrite rather than accumulate stale copies.
 */
public record CommentContentChangedEvent(
        long commentId,
        long issueId,
        String issueKey,
        long spaceId,
        String content,
        Instant occurredAt
) {}
