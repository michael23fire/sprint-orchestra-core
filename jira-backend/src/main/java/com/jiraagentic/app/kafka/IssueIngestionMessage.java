package com.jiraagentic.app.kafka;

import com.jiraagentic.app.event.IssueContentChangedEvent;
import com.jiraagentic.app.event.IssueDeletedEvent;

import java.time.Instant;
import java.util.UUID;

/**
 * Kafka payload for issue-level RAG ingestion. One shape covers both upsert and delete; the
 * {@code eventType} discriminator tells the vectorization service whether to re-embed
 * ({@code issue_upserted}) or drop the issue's vectors ({@code issue_deleted}). On delete the text
 * and metadata fields are {@code null} — the consumer only needs the id to purge.
 *
 * <p>Upserts always carry the structured metadata ({@code issueType}, {@code status},
 * {@code sprintId}/{@code sprintName}, {@code createdAt}, {@code updatedAt}) alongside the embeddable
 * text: the consumer maintains an issue-level metadata table for exact counting/filtering queries, and
 * that table must never lag the text it sits next to. Field-level change history travels separately as
 * {@link IssueHistoryIngestionMessage} — this message is a snapshot of latest state, not a diff.
 *
 * <p>{@code parentId}/{@code parentKey}/{@code parentTitle} are null unless this issue is a subtask —
 * see {@link IssueContentChangedEvent}'s doc for why the consumer embeds this into the subtask's own
 * chunk text rather than treating it as pure metadata.
 */
public record IssueIngestionMessage(
        String eventId,
        String eventType,
        Instant emittedAt,
        long issueId,
        String issueKey,
        long spaceId,
        String title,
        String description,
        String issueType,
        String status,
        String priority,
        Long sprintId,
        String sprintName,
        Long parentId,
        String parentKey,
        String parentTitle,
        // Owner and size. Carried on the upsert (not left to the history stream) for the same reason
        // priority is: both are usually set once at creation, and a history-only sync never observes
        // that initial value. See vectorization-service migrations/012.
        Long assigneeId,
        String assigneeName,
        Integer storyPoints,
        Instant createdAt,
        Instant updatedAt
) {
    public static IssueIngestionMessage upserted(IssueContentChangedEvent e, UUID eventId) {
        return new IssueIngestionMessage(
                eventId.toString(),
                "issue_upserted",
                e.occurredAt(),
                e.issueId(),
                e.issueKey(),
                e.spaceId(),
                e.title(),
                e.description(),
                e.issueType(),
                e.status(),
                e.priority(),
                e.sprintId(),
                e.sprintName(),
                e.parentId(),
                e.parentKey(),
                e.parentTitle(),
                e.assigneeId(),
                e.assigneeName(),
                e.storyPoints(),
                e.createdAt(),
                e.updatedAt());
    }

    public static IssueIngestionMessage deleted(IssueDeletedEvent e, UUID eventId) {
        return new IssueIngestionMessage(
                eventId.toString(),
                "issue_deleted",
                e.occurredAt(),
                e.issueId(),
                e.issueKey(),
                e.spaceId(),
                null,
                null,
                null,
                null,
                null,
                null,
                null,
                null,
                null,
                null,
                null,
                null,
                null,
                null,
                null);
    }
}
