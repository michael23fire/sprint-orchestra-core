package com.jiraagentic.app.dto;

import lombok.Data;

@Data
public class CreateIssueCodeLinkRequest {
    private String url;

    /**
     * Optional PAT for this request only (not persisted). If omitted, the space stored PAT
     * (from bulk import) or server {@code GITHUB_TOKEN} is used.
     */
    private String githubToken;
}
