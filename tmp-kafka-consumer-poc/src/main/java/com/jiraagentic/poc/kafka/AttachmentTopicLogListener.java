package com.jiraagentic.poc.kafka;

import org.apache.kafka.clients.consumer.ConsumerRecord;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.kafka.annotation.KafkaListener;
import org.springframework.stereotype.Component;

/**
 * Temporary POC consumer: logs raw JSON so you can confirm jira-backend publishes after uploads.
 */
@Component
public class AttachmentTopicLogListener {

    private static final Logger log = LoggerFactory.getLogger(AttachmentTopicLogListener.class);

    @KafkaListener(
            topics = "${app.kafka.attachment-ingestion.topic}",
            groupId = "${spring.kafka.consumer.group-id}")
    public void onMessage(ConsumerRecord<String, String> record) {
        String line = String.format(
                "[tmp-kafka-consumer-poc] partition=%s offset=%s key=%s value=%s",
                record.partition(),
                record.offset(),
                record.key(),
                record.value());
        System.out.println(line);
        log.info(line);
    }
}
