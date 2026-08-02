package com.jiraagentic.app.service;

import com.jiraagentic.app.dto.IssueHistoryDto;
import com.jiraagentic.app.entity.Issue;
import com.jiraagentic.app.entity.IssueHistory;
import com.jiraagentic.app.entity.User;
import com.jiraagentic.app.event.IssueHistoryRecordedEvent;
import com.jiraagentic.app.repository.IssueHistoryRepository;
import com.jiraagentic.app.repository.IssueRepository;
import com.jiraagentic.app.repository.UserRepository;
import lombok.RequiredArgsConstructor;
import org.springframework.context.ApplicationEventPublisher;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.time.Instant;
import java.util.List;
import java.util.Objects;
import java.util.stream.Collectors;

@Service
@RequiredArgsConstructor
public class IssueHistoryService {

    private final IssueHistoryRepository issueHistoryRepository;
    private final IssueRepository issueRepository;
    private final UserRepository userRepository;
    private final ActiveSpaceGuard activeSpaceGuard;
    private final ApplicationEventPublisher applicationEventPublisher;

    public List<IssueHistoryDto> findByIssue(Long issueId) {
        Issue issue = issueRepository.findById(issueId)
                .orElseThrow(() -> new RuntimeException("Issue not found: " + issueId));
        activeSpaceGuard.requireActive(issue.getSpace());
        return issueHistoryRepository.findByIssueIdOrderByCreatedAtDesc(issueId).stream()
                .map(IssueHistoryDto::from)
                .collect(Collectors.toList());
    }

    @Transactional
    public void recordFieldChange(Issue issue, Long actorId, String fieldName, Object fromValue, Object toValue) {
        if (Objects.equals(fromValue, toValue)) return;
        IssueHistory h = new IssueHistory();
        h.setIssue(issue);
        h.setActor(resolveActor(actorId));
        h.setEventType("field_change");
        h.setFieldName(fieldName);
        h.setFromValue(fromValue == null ? null : String.valueOf(fromValue));
        h.setToValue(toValue == null ? null : String.valueOf(toValue));
        h.setDescription("Updated " + fieldName);
        issueHistoryRepository.save(h);
        publishRecorded(h, issue);
    }

    @Transactional
    public void recordEvent(Issue issue, Long actorId, String eventType, String description) {
        IssueHistory h = new IssueHistory();
        h.setIssue(issue);
        h.setActor(resolveActor(actorId));
        h.setEventType(eventType);
        h.setDescription(description);
        issueHistoryRepository.save(h);
        publishRecorded(h, issue);
    }

    /**
     * Bridge every recorded history row to the Kafka content-ingestion stream (AFTER_COMMIT listener
     * in ContentIngestionKafkaPublisher) so the vectorization service keeps an append-only copy of
     * issue changes for the AI agent's history questions ("which issues were reopened", "what did I
     * just change"). Published inside the same transaction as the save — IDENTITY generation means
     * the row id is already assigned — and only ever delivered after commit.
     */
    private void publishRecorded(IssueHistory h, Issue issue) {
        applicationEventPublisher.publishEvent(new IssueHistoryRecordedEvent(
                h.getId(),
                issue.getId(),
                issue.getIssueKey(),
                issue.getSpace().getId(),
                h.getEventType(),
                h.getFieldName(),
                h.getFromValue(),
                h.getToValue(),
                h.getDescription(),
                h.getActor() != null ? h.getActor().getName() : null,
                h.getCreatedAt() != null ? h.getCreatedAt() : Instant.now()));
    }

    private User resolveActor(Long actorId) {
        if (actorId == null) return null;
        return userRepository.findById(actorId).orElse(null);
    }
}
