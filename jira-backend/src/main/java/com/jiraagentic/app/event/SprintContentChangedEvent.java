package com.jiraagentic.app.event;

import java.time.Instant;
import java.time.LocalDate;

/**
 * Published after a sprint is created, edited, or completed, on the same transactional boundary as
 * {@link IssueContentChangedEvent} (AFTER_COMMIT bridging — see ContentIngestionKafkaPublisher).
 *
 * <p>Unlike an issue, a sprint's {@code goal} is real embeddable prose ("Prove the storefront is
 * accessible, observable, and ready for a larger public beta") that never appears verbatim on any
 * issue — without this event, the vectorization service has no way to answer "which sprint's goal was
 * about accessibility" except by coincidentally matching similarly-worded issue text, which is a
 * citation to the wrong source even when the wording happens to line up.
 *
 * <p>Carries the velocity/point snapshot fields too (committed/completed points, issue counts) so
 * structured "how is this sprint tracking" queries don't need a second round-trip.
 */
public record SprintContentChangedEvent(
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
        Integer unestimatedIssueCount,
        Instant occurredAt
) {}
