package com.jiraagentic.app.event;

import java.time.Instant;

/**
 * Published after an issue is created or its embeddable text (title / description) changes, on the
 * same transactional boundary. Kafka bridging listens
 * {@linkplain org.springframework.transaction.event.TransactionalEventListener after commit} so the
 * vectorization service never observes content before it is durably committed.
 *
 * <p>Carries the full text so the downstream RAG ingestion service can re-embed without reading this
 * service's database — the content is small (title + description HTML) and stays well under Kafka's
 * default max message size. Attachments are handled separately (see {@link AttachmentUploadedEvent})
 * because their binaries are large and fetched from object storage.
 *
 * <p>Also carries the issue's structured metadata ({@code issueType}, {@code status},
 * {@code priority}, lifecycle timestamps, and which sprint it's currently in): the vectorization
 * service keeps an issue-level metadata table alongside its vectors so the AI agent can answer exact
 * counting/filtering questions ("how many bugs?", "how many issues in the active sprint?") that top-K
 * semantic search structurally cannot. Metadata-only edits (e.g. a status or sprint change with no
 * text change) do NOT fire this event — they reach the consumer through
 * {@link IssueHistoryRecordedEvent} instead, which avoids spending an embedding call on a change that
 * doesn't affect the vector. {@code priority} is the one field carried in BOTH places: here, so the
 * metadata table gets the current value from the very first upsert (most issues have their priority
 * set once at creation and never changed again — a history-only sync would never see that initial
 * value); and self-healed via the history stream too (vectorization-service's
 * {@code append_issue_change}), for the less common case of a genuine later priority edit.
 *
 * <p>Also carries the PARENT issue's key/title, when this issue is a subtask (null otherwise): a
 * subtask's own title+description is often too terse to be findable or correctly characterized on its
 * own (e.g. "Reject repeated checkout requests" has no hint it belongs to a specific checkout
 * incident) — this lets the vectorization service embed that context directly into the subtask's own
 * chunk instead of relying on ranking to surface the parent separately. This is a denormalized
 * snapshot, same idiom as {@code sprintName}: if the PARENT's title later changes without this
 * subtask's own content changing, this subtask's already-embedded chunk keeps the stale parent title
 * until the subtask is next saved — accepted for the same reason sprint-name staleness already is.
 */
public record IssueContentChangedEvent(
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
        Instant createdAt,
        Instant updatedAt,
        Instant occurredAt
) {}
