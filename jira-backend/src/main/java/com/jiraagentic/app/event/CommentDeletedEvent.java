package com.jiraagentic.app.event;

import java.time.Instant;

/**
 * Published after a comment is deleted, on the same transactional boundary. The vectorization
 * service removes the comment's vector; without this event a deleted comment would linger in the
 * vector store and keep surfacing in semantic search (stale-index problem) because the vector store
 * is a separate copy that Postgres does not cascade-delete.
 */
public record CommentDeletedEvent(
        long commentId,
        long issueId,
        String issueKey,
        long spaceId,
        Instant occurredAt
) {}
