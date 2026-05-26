package com.jiraagentic.app.dto;

import lombok.Data;

@Data
public class CreateSpaceGithubRepoRequest {
    /** Accepts "owner/repo" or a full GitHub URL like https://github.com/owner/repo. */
    private String target;
}
