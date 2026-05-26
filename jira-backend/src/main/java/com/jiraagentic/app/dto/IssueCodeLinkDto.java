package com.jiraagentic.app.dto;

import com.jiraagentic.app.entity.IssueCodeLink;
import lombok.Data;

import java.time.Instant;

@Data
public class IssueCodeLinkDto {
    private Long id;
    private Long issueId;
    private String issueKey;
    private String url;
    private String kind;
    private String provider;
    private String owner;
    private String repo;
    private String refId;
    private String title;
    private String state;
    private String authorLogin;
    private String creatorName;
    private Instant createdAt;
    /** GitHub activity timestamp when known; falls back to {@code createdAt} for ordering. */
    private Instant lastActivityAt;

    public static IssueCodeLinkDto from(IssueCodeLink e) {
        IssueCodeLinkDto dto = new IssueCodeLinkDto();
        dto.setId(e.getId());
        if (e.getIssue() != null) {
            dto.setIssueId(e.getIssue().getId());
            dto.setIssueKey(e.getIssue().getIssueKey());
        }
        dto.setUrl(e.getUrl());
        dto.setKind(e.getKind());
        dto.setProvider(e.getProvider());
        dto.setOwner(e.getOwner());
        dto.setRepo(e.getRepo());
        dto.setRefId(e.getRefId());
        dto.setTitle(e.getTitle());
        dto.setState(e.getState());
        dto.setAuthorLogin(e.getAuthorLogin());
        if (e.getCreator() != null) dto.setCreatorName(e.getCreator().getName());
        dto.setCreatedAt(e.getCreatedAt());
        dto.setLastActivityAt(e.getLastActivityAt());
        return dto;
    }
}
