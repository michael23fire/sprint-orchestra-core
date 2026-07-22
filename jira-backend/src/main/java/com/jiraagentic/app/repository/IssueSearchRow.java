package com.jiraagentic.app.repository;

import java.time.Instant;

/** Projection for full-text search hits on {@code issues}. */
public interface IssueSearchRow {
    Long getId();
    String getIssueKey();
    Long getSpaceId();
    String getTitle();
    String getSnippet();
    Double getRank();
    Instant getUpdatedAt();
}
