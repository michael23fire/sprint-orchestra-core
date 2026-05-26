package com.jiraagentic.app.controller;

import com.jiraagentic.app.dto.CreateIssueCodeLinkRequest;
import com.jiraagentic.app.dto.GithubTokenRequest;
import com.jiraagentic.app.dto.IssueCodeLinkDto;
import com.jiraagentic.app.security.AuthSupport;
import com.jiraagentic.app.service.IssueCodeLinkService;
import lombok.RequiredArgsConstructor;
import org.springframework.http.ResponseEntity;
import org.springframework.security.core.Authentication;
import org.springframework.web.bind.annotation.*;

import java.util.List;

@RestController
@RequiredArgsConstructor
public class IssueCodeLinkController {

    private final IssueCodeLinkService codeLinkService;

    @GetMapping("/api/issues/{issueId}/code-links")
    public List<IssueCodeLinkDto> getByIssue(@PathVariable Long issueId) {
        return codeLinkService.findByIssue(issueId);
    }

    @PostMapping("/api/issues/{issueId}/code-links")
    public IssueCodeLinkDto create(
            @PathVariable Long issueId,
            @RequestBody CreateIssueCodeLinkRequest req,
            Authentication authentication) {
        return codeLinkService.create(issueId, req, AuthSupport.extractUid(authentication));
    }

    @DeleteMapping("/api/issues/{issueId}/code-links/{linkId}")
    public ResponseEntity<Void> delete(
            @PathVariable Long issueId,
            @PathVariable Long linkId,
            Authentication authentication) {
        codeLinkService.delete(issueId, linkId, AuthSupport.extractUid(authentication));
        return ResponseEntity.noContent().build();
    }

    @GetMapping("/api/spaces/{spaceId}/code-links")
    public List<IssueCodeLinkDto> getBySpace(@PathVariable Long spaceId) {
        return codeLinkService.findBySpace(spaceId);
    }

    @PostMapping("/api/spaces/{spaceId}/code-links/refresh")
    public IssueCodeLinkService.RefreshResult refreshSpace(
            @PathVariable Long spaceId,
            @RequestBody(required = false) GithubTokenRequest body,
            Authentication authentication) {
        String token = body != null ? body.getGithubToken() : null;
        return codeLinkService.refreshSpace(spaceId, AuthSupport.extractUid(authentication), token);
    }

    @PostMapping("/api/issues/{issueId}/code-links/refresh")
    public IssueCodeLinkService.RefreshResult refreshIssue(
            @PathVariable Long issueId,
            @RequestBody(required = false) GithubTokenRequest body,
            Authentication authentication) {
        String token = body != null ? body.getGithubToken() : null;
        return codeLinkService.refreshIssue(issueId, AuthSupport.extractUid(authentication), token);
    }

    @PostMapping("/api/code-links/{linkId}/refresh")
    public IssueCodeLinkDto refreshOne(
            @PathVariable Long linkId,
            @RequestBody(required = false) GithubTokenRequest body,
            Authentication authentication) {
        String token = body != null ? body.getGithubToken() : null;
        return codeLinkService.refreshOne(linkId, AuthSupport.extractUid(authentication), token);
    }
}
