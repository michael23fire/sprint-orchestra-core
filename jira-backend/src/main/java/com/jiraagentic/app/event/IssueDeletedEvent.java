package com.jiraagentic.app.event;

import java.time.Instant;

/**
 * Published after an issue is deleted (once per issue in a deleted subtree), on the same
 * transactional boundary. The vectorization service consumes this to remove <em>all</em> vectors it
 * holds for the issue — the issue chunk, every comment chunk, and every attachment chunk — because
 * those live in a separate vector store that Postgres cascade deletes do not reach.
 *
 * <p>No per-comment delete event is emitted on issue deletion: the consumer deletes by issue id, so
 * one event covers the whole issue's footprint.
 */
public record IssueDeletedEvent(
        long issueId,
        String issueKey,
        long spaceId,
        Instant occurredAt
) {}
