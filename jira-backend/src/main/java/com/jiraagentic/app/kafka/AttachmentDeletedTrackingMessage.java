package com.jiraagentic.app.kafka;

import com.jiraagentic.app.event.AttachmentDeletedEvent;

import java.time.Instant;
import java.util.UUID;

/** Kafka payload when an attachment is deleted — for index removal / downstream sync. */
public record AttachmentDeletedTrackingMessage(
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
        String bucket,
        String storageKey,
        String storageUri
) {
    public static AttachmentDeletedTrackingMessage from(AttachmentDeletedEvent e, UUID eventId) {
        return new AttachmentDeletedTrackingMessage(
                eventId.toString(),
                "attachment_deleted",
                e.occurredAt(),
                e.attachmentId(),
                e.issueId(),
                e.issueKey(),
                e.spaceId(),
                e.originalFilename(),
                e.contentType(),
                e.sizeBytes(),
                e.storageBackend(),
                e.storageBucket(),
                e.storageKey(),
                AttachmentStorageUris.s3Uri(e.storageBackend(), e.storageBucket(), e.storageKey())
        );
    }
}
