package com.jiraagentic.app.controller;

import com.jiraagentic.app.dto.CreateIssueRequest;
import com.jiraagentic.app.dto.IssueDto;
import com.jiraagentic.app.dto.UpdateIssueRequest;
import com.jiraagentic.app.security.AuthSupport;
import com.jiraagentic.app.service.IssueService;
import lombok.RequiredArgsConstructor;
import org.springframework.http.ResponseEntity;
import org.springframework.security.core.Authentication;
import org.springframework.web.bind.annotation.*;

import java.util.List;

@RestController
@RequestMapping("/api/spaces/{spaceId}/issues")
@RequiredArgsConstructor
public class IssueController {

    private final IssueService issueService;

    @GetMapping
    public List<IssueDto> getBySpace(@PathVariable Long spaceId) {
        return issueService.findBySpace(spaceId);
    }

    @GetMapping("/{issueKey}")
    public IssueDto getByKey(@PathVariable Long spaceId, @PathVariable String issueKey) {
        return issueService.findByKey(spaceId, issueKey);
    }

    @PostMapping
    public IssueDto create(
            @PathVariable Long spaceId,
            @RequestBody CreateIssueRequest req,
            Authentication authentication) {
        Long creatorId = AuthSupport.extractUidOrNull(authentication);
        return issueService.create(spaceId, req, creatorId);
    }

    @PutMapping("/{issueKey}")
    public IssueDto update(
            @PathVariable Long spaceId,
            @PathVariable String issueKey,
            @RequestBody UpdateIssueRequest req,
            Authentication authentication) {
        return issueService.update(spaceId, issueKey, req, AuthSupport.extractUid(authentication));
    }

    @DeleteMapping("/{issueKey}")
    public ResponseEntity<Void> delete(
            @PathVariable Long spaceId,
            @PathVariable String issueKey,
            Authentication authentication) {
        issueService.delete(spaceId, issueKey, AuthSupport.extractUid(authentication));
        return ResponseEntity.noContent().build();
    }
}
