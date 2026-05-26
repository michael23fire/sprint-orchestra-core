package com.jiraagentic.app.controller;

import com.jiraagentic.app.dto.IssueHistoryDto;
import com.jiraagentic.app.service.IssueHistoryService;
import lombok.RequiredArgsConstructor;
import org.springframework.web.bind.annotation.*;

import java.util.List;

@RestController
@RequestMapping("/api/issues/{issueId}/history")
@RequiredArgsConstructor
public class IssueHistoryController {

    private final IssueHistoryService issueHistoryService;

    @GetMapping
    public List<IssueHistoryDto> getByIssue(@PathVariable Long issueId) {
        return issueHistoryService.findByIssue(issueId);
    }
}
