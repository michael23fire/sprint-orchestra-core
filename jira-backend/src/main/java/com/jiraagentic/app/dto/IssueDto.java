package com.jiraagentic.app.dto;

import com.jiraagentic.app.entity.Issue;
import lombok.Data;
import java.time.Instant;
import java.time.LocalDate;
import java.util.List;

@Data
public class IssueDto {
    private Long id;
    private String issueKey;
    private Long spaceId;
    private Long sprintId;
    private String sprintName;
    private Long parentId;
    private String parentKey;
    private String title;
    private String description;
    private String issueType;
    private String status;
    private String priority;
    private Long assigneeId;
    private String assigneeName;
    private Long reporterId;
    private String reporterName;
    private Integer storyPoints;
    private LocalDate startDate;
    private LocalDate dueDate;
    private Integer issueOrder;
    private List<String> labels;
    private List<CommentDto> comments;
    private List<String> childKeys;
    private List<IssueLinkDto> linkedIssues;
    private List<IssueAttachmentDto> attachments;
    private List<IssueCodeLinkDto> codeLinks;
    /** When the issue row was created — used for status lifecycle / time-in-stage. */
    private Instant createdAt;

    public static IssueDto from(Issue i) {
        IssueDto dto = new IssueDto();
        dto.setId(i.getId());
        dto.setIssueKey(i.getIssueKey());
        dto.setSpaceId(i.getSpace().getId());
        if (i.getSprint() != null) {
            dto.setSprintId(i.getSprint().getId());
            dto.setSprintName(i.getSprint().getName());
        }
        if (i.getParent() != null) {
            dto.setParentId(i.getParent().getId());
            dto.setParentKey(i.getParent().getIssueKey());
        }
        dto.setTitle(i.getTitle());
        dto.setDescription(i.getDescription());
        dto.setIssueType(i.getIssueType());
        dto.setStatus(i.getStatus());
        dto.setPriority(i.getPriority());
        if (i.getAssignee() != null) {
            dto.setAssigneeId(i.getAssignee().getId());
            dto.setAssigneeName(i.getAssignee().getName());
        }
        if (i.getReporter() != null) {
            dto.setReporterId(i.getReporter().getId());
            dto.setReporterName(i.getReporter().getName());
        }
        dto.setStoryPoints(i.getStoryPoints());
        dto.setStartDate(i.getStartDate());
        dto.setDueDate(i.getDueDate());
        dto.setIssueOrder(i.getIssueOrder());
        dto.setLabels(i.getLabels());
        dto.setCreatedAt(i.getCreatedAt());
        return dto;
    }
}
