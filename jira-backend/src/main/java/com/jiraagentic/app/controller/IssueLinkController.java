package com.jiraagentic.app.controller;

import com.jiraagentic.app.dto.CreateIssueLinkRequest;
import com.jiraagentic.app.dto.IssueLinkDto;
import com.jiraagentic.app.security.AuthSupport;
import com.jiraagentic.app.service.IssueLinkService;
import lombok.RequiredArgsConstructor;
import org.springframework.http.ResponseEntity;
import org.springframework.security.core.Authentication;
import org.springframework.web.bind.annotation.*;

import java.util.List;

@RestController
@RequestMapping("/api/issues/{issueId}/links")
@RequiredArgsConstructor
public class IssueLinkController {

    private final IssueLinkService issueLinkService;

    @GetMapping
    public List<IssueLinkDto> getByIssue(@PathVariable Long issueId) {
        return issueLinkService.findByIssue(issueId);
    }

    @PostMapping
    public IssueLinkDto create(
            @PathVariable Long issueId,
            @RequestBody CreateIssueLinkRequest req,
            Authentication authentication) {
        return issueLinkService.create(issueId, req, AuthSupport.extractUid(authentication));
    }

    @DeleteMapping("/{linkId}")
    public ResponseEntity<Void> delete(
            @PathVariable Long issueId,
            @PathVariable Long linkId,
            Authentication authentication) {
        issueLinkService.delete(issueId, linkId, AuthSupport.extractUid(authentication));
        return ResponseEntity.noContent().build();
    }
}
