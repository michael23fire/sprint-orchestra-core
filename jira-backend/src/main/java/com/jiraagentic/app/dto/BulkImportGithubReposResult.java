package com.jiraagentic.app.dto;

import lombok.AllArgsConstructor;
import lombok.Data;
import lombok.NoArgsConstructor;

@Data
@NoArgsConstructor
@AllArgsConstructor
public class BulkImportGithubReposResult {
    /** Repos returned by GitHub for this account (before skipping duplicates). */
    private int discovered;
    private int added;
    /** Already linked to this space. */
    private int skipped;
}
