package com.jiraagentic.app.kafka;

/**
 * Builds stable object locators for attachment Kafka payloads.
 * Downstream workers use their own S3/MinIO client config to resolve these URIs.
 */
final class AttachmentStorageUris {

    private AttachmentStorageUris() {}

    /**
     * @return {@code s3://bucket/key} when backend is S3 and bucket/key are present; otherwise empty
     */
    static String s3Uri(String storageBackend, String bucket, String storageKey) {
        if (storageBackend == null || !"s3".equalsIgnoreCase(storageBackend.trim())) {
            return "";
        }
        if (bucket == null || bucket.isBlank() || storageKey == null || storageKey.isBlank()) {
            return "";
        }
        return "s3://" + bucket.trim() + "/" + storageKey.trim();
    }
}
