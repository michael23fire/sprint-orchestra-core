package com.jiraagentic.app.event;

import java.time.Instant;

/**
 * Published after a sprint is deleted (jira Cloud allows deleting from any status). Mirrors
 * {@link IssueDeletedEvent} — the vectorization service consumes this to drop the sprint's metadata
 * row and its goal chunk, both of which live in a separate store Postgres cascade deletes can't reach.
 */
public record SprintDeletedEvent(
        long sprintId,
        String sprintName,
        long spaceId,
        Instant occurredAt
) {}
