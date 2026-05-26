package com.jiraagentic.app.dto;

import lombok.Data;

@Data
public class BulkImportGithubReposRequest {
    /**
     * GitHub user or organization login (e.g. {@code octocat} or {@code mycompany}).
     * Leading {@code @} is optional.
     */
    private String account;

    /**
     * Optional PAT for listing repos to import. If provided, it is also <strong>stored on the space</strong>
     * (DB column {@code spaces.github_pat}) for later Scan / Refresh / Development metadata — not returned by the Space API.
     * Without it, GitHub only returns <em>public</em> repos for the account. Classic: {@code repo}; fine-grained: repo read.
     */
    private String githubToken;
}
