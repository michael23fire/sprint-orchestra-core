package com.jiraagentic.app.dto;

import com.jiraagentic.app.entity.IssueHistory;
import lombok.Data;

import java.time.Instant;

@Data
public class IssueHistoryDto {
    private Long id;
    private Long issueId;
    private Long actorId;
    private String actorName;
    private String eventType;
    private String fieldName;
    private String fromValue;
    private String toValue;
    private String description;
    private Instant createdAt;

    public static IssueHistoryDto from(IssueHistory h) {
        IssueHistoryDto dto = new IssueHistoryDto();
        dto.setId(h.getId());
        dto.setIssueId(h.getIssue().getId());
        if (h.getActor() != null) {
            dto.setActorId(h.getActor().getId());
            dto.setActorName(h.getActor().getName());
        }
        dto.setEventType(h.getEventType());
        dto.setFieldName(h.getFieldName());
        dto.setFromValue(h.getFromValue());
        dto.setToValue(h.getToValue());
        dto.setDescription(h.getDescription());
        dto.setCreatedAt(h.getCreatedAt());
        return dto;
    }
}
