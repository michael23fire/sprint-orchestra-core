package com.jiraagentic.app.dto;

import lombok.Data;
import java.time.LocalDate;
import java.util.List;

@Data
public class CreateIssueRequest {
    private String title;
    private String description;
    private String issueType;
    private String status;
    private String priority;
    private Long assigneeId;
    private Long reporterId;
    private Long sprintId;
    private Long parentId;
    private Integer storyPoints;
    private LocalDate startDate;
    private LocalDate dueDate;
    private List<String> labels;
}
