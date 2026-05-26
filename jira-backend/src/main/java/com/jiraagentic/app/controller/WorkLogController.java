package com.jiraagentic.app.controller;

import com.jiraagentic.app.dto.CreateWorkLogRequest;
import com.jiraagentic.app.dto.UpdateWorkLogRequest;
import com.jiraagentic.app.dto.WorkLogDto;
import com.jiraagentic.app.service.WorkLogService;
import lombok.RequiredArgsConstructor;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;

import java.util.List;

@RestController
@RequestMapping("/api/issues/{issueId}/worklogs")
@RequiredArgsConstructor
public class WorkLogController {

    private final WorkLogService workLogService;

    @GetMapping
    public List<WorkLogDto> getByIssue(@PathVariable Long issueId) {
        return workLogService.findByIssue(issueId);
    }

    @PostMapping
    public WorkLogDto create(@PathVariable Long issueId, @RequestBody CreateWorkLogRequest req) {
        return workLogService.create(issueId, req);
    }

    @PutMapping("/{workLogId}")
    public WorkLogDto update(@PathVariable Long workLogId, @RequestBody UpdateWorkLogRequest req) {
        return workLogService.update(workLogId, req);
    }

    @DeleteMapping("/{workLogId}")
    public ResponseEntity<Void> delete(@PathVariable Long workLogId) {
        workLogService.delete(workLogId);
        return ResponseEntity.noContent().build();
    }
}
