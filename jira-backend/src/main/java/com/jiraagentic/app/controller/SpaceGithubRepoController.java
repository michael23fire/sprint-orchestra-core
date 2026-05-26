package com.jiraagentic.app.controller;

import com.jiraagentic.app.dto.BulkImportGithubReposRequest;
import com.jiraagentic.app.dto.BulkImportGithubReposResult;
import com.jiraagentic.app.dto.CreateSpaceGithubRepoRequest;
import com.jiraagentic.app.dto.GithubTokenRequest;
import com.jiraagentic.app.dto.SpaceGithubRepoDto;
import com.jiraagentic.app.security.AuthSupport;
import com.jiraagentic.app.service.GithubScanService;
import lombok.RequiredArgsConstructor;
import org.springframework.http.ResponseEntity;
import org.springframework.security.core.Authentication;
import org.springframework.web.bind.annotation.*;

import java.util.List;

@RestController
@RequestMapping("/api/spaces/{spaceId}/github-repos")
@RequiredArgsConstructor
public class SpaceGithubRepoController {

    private final GithubScanService scanService;

    @GetMapping
    public List<SpaceGithubRepoDto> list(@PathVariable Long spaceId) {
        return scanService.listRepos(spaceId);
    }

    @PostMapping
    public SpaceGithubRepoDto add(
            @PathVariable Long spaceId,
            @RequestBody CreateSpaceGithubRepoRequest req) {
        return scanService.addRepo(spaceId, req);
    }

    @PostMapping("/bulk")
    public BulkImportGithubReposResult bulkImport(
            @PathVariable Long spaceId,
            @RequestBody BulkImportGithubReposRequest req) {
        return scanService.bulkAddReposFromAccount(spaceId, req);
    }

    @DeleteMapping("/{repoId}")
    public ResponseEntity<Void> remove(
            @PathVariable Long spaceId,
            @PathVariable Long repoId,
            Authentication authentication) {
        scanService.removeRepo(spaceId, repoId, AuthSupport.extractUid(authentication));
        return ResponseEntity.noContent().build();
    }

    @PostMapping("/scan")
    public GithubScanService.ScanResult scan(
            @PathVariable Long spaceId,
            @RequestBody(required = false) GithubTokenRequest body,
            Authentication authentication) {
        String token = body != null ? body.getGithubToken() : null;
        return scanService.scanSpace(spaceId, AuthSupport.extractUid(authentication), token);
    }
}
