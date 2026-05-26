package com.jiraagentic.app.dto;

import lombok.Data;

@Data
public class CreateIssueLinkRequest {
    private String relation;
    private String targetIssueKey;
}
