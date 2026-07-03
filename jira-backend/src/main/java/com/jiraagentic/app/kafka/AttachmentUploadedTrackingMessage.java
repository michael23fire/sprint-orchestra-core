package com.jiraagentic.app.kafka;

import com.jiraagentic.app.event.AttachmentUploadedEvent;

import java.time.Instant;
import java.util.UUID;

/**
 * Kafka payload when an attachment is uploaded — tracking / correlation for downstream ingestion.
 * {@code storageUri} is a stable object locator ({@code s3://bucket/key}) for vector / RAG workers
 * that fetch the binary with their own S3 credentials.
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
        String bucket,
        String storageKey,
        String storageUri
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
                e.storageBucket(),
                e.storageKey(),
                AttachmentStorageUris.s3Uri(e.storageBackend(), e.storageBucket(), e.storageKey())
        );
    }
}
