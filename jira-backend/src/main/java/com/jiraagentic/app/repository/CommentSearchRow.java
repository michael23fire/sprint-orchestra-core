package com.jiraagentic.app.repository;

import java.time.Instant;

/** Projection for full-text search hits on {@code comments}. */
public interface CommentSearchRow {
    Long getId();
    Long getIssueId();
    String getIssueKey();
    Long getSpaceId();
    String getIssueTitle();
    String getSnippet();
    Double getRank();
    Instant getUpdatedAt();
}
