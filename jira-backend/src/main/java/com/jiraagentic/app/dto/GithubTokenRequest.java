package com.jiraagentic.app.dto;

import lombok.Data;

/**
 * Optional GitHub PAT for a single HTTP request (not persisted).
 * Used for scan, refresh, etc. when the server token is missing or insufficient.
 */
@Data
public class GithubTokenRequest {
    private String githubToken;
}
