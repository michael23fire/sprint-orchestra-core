package com.jiraagentic.app.event;

import java.time.Instant;

/**
 * Published after an issue attachment is persisted and its binary stored (same transactional boundary).
 * Kafka bridging listens {@linkplain org.springframework.transaction.event.TransactionalEventListener
 * after commit} so consumers never observe the attachment row before commit.
 */
public record AttachmentUploadedEvent(
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
