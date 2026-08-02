package com.jiraagentic.app.event;

import java.time.Instant;

/**
 * Published whenever an {@link com.jiraagentic.app.entity.IssueHistory} row is recorded — one event
 * per field change ({@code eventType=field_change}, with {@code fieldName}/{@code fromValue}/
 * {@code toValue}) or lifecycle event ({@code issue_created}, {@code comment_created}, …).
 *
 * <p>This is the append-only change stream the vectorization service ingests into its own
 * {@code issue_changes} table, so the AI agent can answer history questions the latest-state snapshot
 * cannot: "which issues were reopened", "what did I change in ATC-77 just now". The snapshot table
 * being an upsert (latest state only) is exactly why this stream exists — transitions would otherwise
 * be overwritten and lost downstream.
 *
 * <p>{@code historyId} is this row's primary key in {@code issue_history}; the consumer uses it as
 * its own primary key, which makes at-least-once Kafka delivery (and backfill overlap) naturally
 * idempotent — a redelivered event is an {@code ON CONFLICT DO NOTHING}.
 */
public record IssueHistoryRecordedEvent(
        long historyId,
        long issueId,
        String issueKey,
        long spaceId,
        String eventType,
        String fieldName,
        String fromValue,
        String toValue,
        String description,
        String actorName,
        Instant occurredAt
) {}
