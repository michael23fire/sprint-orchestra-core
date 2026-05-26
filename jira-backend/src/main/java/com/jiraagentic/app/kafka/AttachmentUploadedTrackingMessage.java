package com.jiraagentic.app.kafka;

import com.jiraagentic.app.event.AttachmentUploadedEvent;

import java.time.Instant;
import java.util.UUID;

/**
 * Kafka payload when an attachment is uploaded — tracking / correlation only (POC-friendly).
 */
public record AttachmentUploadedTrackingMessage(
        String eventId,
        String eventType,
        Instant emittedAt,
        long attachmentId,
        long issueId,
        String issueKey,
        long spaceId,
        String filename,
        String mimeType,
        long byteSize,
        String storageBackend,
        String storageKey
) {
    public static AttachmentUploadedTrackingMessage from(AttachmentUploadedEvent e, UUID eventId) {
        return new AttachmentUploadedTrackingMessage(
                eventId.toString(),
                "attachment_uploaded",
                e.occurredAt(),
                e.attachmentId(),
                e.issueId(),
                e.issueKey(),
                e.spaceId(),
                e.originalFilename(),
                e.contentType(),
                e.sizeBytes(),
                e.storageBackend(),
                e.storageKey()
        );
    }
}
