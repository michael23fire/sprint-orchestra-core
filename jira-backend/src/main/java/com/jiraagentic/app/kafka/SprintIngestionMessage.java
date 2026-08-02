package com.jiraagentic.app.kafka;

import com.jiraagentic.app.event.SprintContentChangedEvent;
import com.jiraagentic.app.event.SprintDeletedEvent;

import java.time.Instant;
import java.time.LocalDate;
import java.util.UUID;

/**
 * Kafka payload for sprint-level RAG ingestion — same shape/discriminator convention as
 * {@link IssueIngestionMessage}: one record for both upsert and delete, {@code eventType} tells the
 * consumer which. On delete, everything but the id/key/space is {@code null}.
 */
public record SprintIngestionMessage(
        String eventId,
        String eventType,
        Instant emittedAt,
        long sprintId,
        String sprintName,
        long spaceId,
        String goal,
        LocalDate startDate,
        LocalDate endDate,
        String status,
        Integer initialCommittedPoints,
        Integer initialCompletedPoints,
        Integer finalScopePoints,
        Integer completedPoints,
        Integer initialIssueCount,
        Integer completedIssueCount,
        Integer finalIssueCount,
        Integer unestimatedIssueCount
) {
    public static SprintIngestionMessage upserted(SprintContentChangedEvent e, UUID eventId) {
        return new SprintIngestionMessage(
                eventId.toString(),
                "sprint_upserted",
                e.occurredAt(),
                e.sprintId(),
                e.sprintName(),
                e.spaceId(),
                e.goal(),
                e.startDate(),
                e.endDate(),
                e.status(),
                e.initialCommittedPoints(),
                e.initialCompletedPoints(),
                e.finalScopePoints(),
                e.completedPoints(),
                e.initialIssueCount(),
                e.completedIssueCount(),
                e.finalIssueCount(),
                e.unestimatedIssueCount());
    }

    public static SprintIngestionMessage deleted(SprintDeletedEvent e, UUID eventId) {
        return new SprintIngestionMessage(
                eventId.toString(),
                "sprint_deleted",
                e.occurredAt(),
                e.sprintId(),
                e.sprintName(),
                e.spaceId(),
                null, null, null, null, null, null, null, null, null, null, null, null);
    }
}
