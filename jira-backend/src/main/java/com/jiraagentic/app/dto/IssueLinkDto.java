package com.jiraagentic.app.dto;

import lombok.Data;

import java.time.Instant;

@Data
public class IssueLinkDto {
    private Long id;
    private String relation;
    private Long linkedIssueId;
    private String linkedIssueKey;
    private String linkedIssueTitle;
    private Instant createdAt;
}
