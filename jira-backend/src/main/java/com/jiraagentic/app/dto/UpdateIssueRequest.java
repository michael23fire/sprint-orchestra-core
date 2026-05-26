package com.jiraagentic.app.dto;

import lombok.Data;
import java.time.LocalDate;
import java.util.List;

@Data
public class UpdateIssueRequest {
    private String title;
    private String description;
    private String issueType;
    private String status;
    private String priority;
    private Long assigneeId;
    private Long reporterId;
    /** When true, clears assignee even if assigneeId is null. */
    private Boolean clearAssignee;
    /** When true, clears reporter even if reporterId is null. */
    private Boolean clearReporter;
    private Long sprintId;
    private Boolean clearSprint;
    private Long parentId;
    /** When true, clears parent even if parentId is null. */
    private Boolean clearParent;
    private Integer storyPoints;
    private LocalDate startDate;
    private LocalDate dueDate;
    private Integer issueOrder;
    private List<String> labels;
}
