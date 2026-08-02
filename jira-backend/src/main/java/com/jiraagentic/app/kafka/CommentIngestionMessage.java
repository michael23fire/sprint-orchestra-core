package com.jiraagentic.app.kafka;

import com.jiraagentic.app.event.CommentContentChangedEvent;
import com.jiraagentic.app.event.CommentDeletedEvent;

import java.time.Instant;
import java.util.UUID;

/**
 * Kafka payload for comment-level RAG ingestion. {@code eventType} is {@code comment_upserted} or
 * {@code comment_deleted}; {@code content} is {@code null} on delete. The {@code issueId} lets the
 * vectorization service attach the comment's chunk to its parent issue in vector metadata so
 * semantic-search hits can be linked back to the right issue.
 */
public record CommentIngestionMessage(
        String eventId,
        String eventType,
        Instant emittedAt,
        long commentId,
        long issueId,
        String issueKey,
        long spaceId,
        String content
) {
    public static CommentIngestionMessage upserted(CommentContentChangedEvent e, UUID eventId) {
        return new CommentIngestionMessage(
                eventId.toString(),
                "comment_upserted",
                e.occurredAt(),
                e.commentId(),
                e.issueId(),
                e.issueKey(),
                e.spaceId(),
                e.content());
    }

    public static CommentIngestionMessage deleted(CommentDeletedEvent e, UUID eventId) {
        return new CommentIngestionMessage(
                eventId.toString(),
                "comment_deleted",
                e.occurredAt(),
                e.commentId(),
                e.issueId(),
                e.issueKey(),
                e.spaceId(),
                null);
    }
}
