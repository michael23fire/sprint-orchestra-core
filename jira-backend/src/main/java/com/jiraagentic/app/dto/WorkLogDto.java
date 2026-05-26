package com.jiraagentic.app.dto;

import com.jiraagentic.app.entity.WorkLog;
import lombok.Data;

import java.time.Instant;
import java.time.LocalDate;

@Data
public class WorkLogDto {
    private Long id;
    private Long issueId;
    private Long authorId;
    private String authorName;
    private Integer spentMinutes;
    private String note;
    private LocalDate logDate;
    private Instant createdAt;
    private Instant updatedAt;

    public static WorkLogDto from(WorkLog w) {
        WorkLogDto dto = new WorkLogDto();
        dto.setId(w.getId());
        dto.setIssueId(w.getIssue().getId());
        dto.setAuthorId(w.getAuthor().getId());
        dto.setAuthorName(w.getAuthor().getName());
        dto.setSpentMinutes(w.getSpentMinutes());
        dto.setNote(w.getNote());
        dto.setLogDate(w.getLogDate());
        dto.setCreatedAt(w.getCreatedAt());
        dto.setUpdatedAt(w.getUpdatedAt());
        return dto;
    }
}
