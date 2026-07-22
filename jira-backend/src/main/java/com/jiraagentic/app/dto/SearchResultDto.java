package com.jiraagentic.app.dto;

import lombok.AllArgsConstructor;
import lombok.Data;

import java.time.Instant;

@Data
@AllArgsConstructor
public class SearchResultDto {
    /** "ISSUE" or "COMMENT". */
    private String type;
    /** "EXACT_KEY", "FULL_TEXT", or "FUZZY" — which search tier produced this hit. */
    private String matchType;
    private Long spaceId;
    private Long issueId;
    private String issueKey;
    private String title;
    private String snippet;
    private double rank;
    private Instant updatedAt;
}
