package com.jiraagentic.app.kafka;

import com.jiraagentic.app.event.AttachmentDeletedEvent;
import com.jiraagentic.app.event.AttachmentUploadedEvent;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.kafka.core.KafkaTemplate;
import org.springframework.stereotype.Component;
import org.springframework.transaction.event.TransactionPhase;
import org.springframework.transaction.event.TransactionalEventListener;

import java.util.UUID;

@Component
@ConditionalOnProperty(prefix = "app.kafka.attachment-ingestion", name = "enabled", havingValue = "true")
@RequiredArgsConstructor
@Slf4j
public class AttachmentIngestionKafkaPublisher {

    private final KafkaTemplate<String, Object> kafkaTemplate;

    @Value("${app.kafka.attachment-ingestion.topic}")
    private String topic;

    @TransactionalEventListener(phase = TransactionPhase.AFTER_COMMIT)
    public void onAttachmentUploaded(AttachmentUploadedEvent event) {
        publish(event.issueKey(), AttachmentUploadedTrackingMessage.from(event, UUID.randomUUID()), event.attachmentId());
    }

    @TransactionalEventListener(phase = TransactionPhase.AFTER_COMMIT)
    public void onAttachmentDeleted(AttachmentDeletedEvent event) {
        publish(event.issueKey(), AttachmentDeletedTrackingMessage.from(event, UUID.randomUUID()), event.attachmentId());
    }

    private void publish(String partitionKey, Object message, long attachmentId) {
        try {
            kafkaTemplate.send(topic, partitionKey, message)
                    .whenComplete((result, ex) -> {
                        if (ex != null) {
                            log.warn("Kafka publish failed for attachment {}: {}", attachmentId, ex.getMessage());
                        }
                    });
        } catch (Exception e) {
            log.warn("Kafka publish failed for attachment {}: {}", attachmentId, e.getMessage());
        }
    }
}
