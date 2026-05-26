package com.jiraagentic.app.event;

import java.time.Instant;

/**
 * Published after an embedded or panel attachment row and binary are removed.
 * Kafka bridging listens after commit so consumers never observe stale attachment rows.
 */
public record AttachmentDeletedEvent(
        long attachmentId,
        long issueId,
        String issueKey,
        long spaceId,
        String storageBackend,
        String storageBucket,
        String storageKey,
        String originalFilename,
        String contentType,
        long sizeBytes,
        boolean listInAttachmentPanel,
        Instant occurredAt
) {}
