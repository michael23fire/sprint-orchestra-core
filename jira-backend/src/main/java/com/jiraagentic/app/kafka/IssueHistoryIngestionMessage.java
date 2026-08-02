package com.jiraagentic.app.kafka;

import com.jiraagentic.app.event.IssueHistoryRecordedEvent;

import java.time.Instant;
import java.util.UUID;

/**
 * Kafka payload for one issue-history entry ({@code eventType=issue_history_added}), published on the
 * same content-ingestion topic and partitioned by {@code issueKey} like the other issue messages, so
 * per-issue ordering holds across snapshots and history.
 *
 * <p>{@code historyId} is the {@code issue_history} primary key from this service — the consumer
 * keys its own append-only {@code issue_changes} table on it, making redelivery/backfill idempotent.
 * {@code changeEventType} is the *history* row's kind ({@code field_change}, {@code issue_created},
 * {@code comment_created}, …) — named apart from the envelope's {@code eventType} discriminator on
 * purpose so the consumer's routing never confuses the two.
 */
public record IssueHistoryIngestionMessage(
        String eventId,
        String eventType,
        Instant emittedAt,
        long historyId,
        long issueId,
        String issueKey,
        long spaceId,
        String changeEventType,
        String fieldName,
        String fromValue,
        String toValue,
        String description,
        String actorName
) {
    public static IssueHistoryIngestionMessage from(IssueHistoryRecordedEvent e, UUID eventId) {
        return new IssueHistoryIngestionMessage(
                eventId.toString(),
                "issue_history_added",
                e.occurredAt(),
                e.historyId(),
                e.issueId(),
                e.issueKey(),
                e.spaceId(),
                e.eventType(),
                e.fieldName(),
                e.fromValue(),
                e.toValue(),
                e.description(),
                e.actorName());
    }
}
