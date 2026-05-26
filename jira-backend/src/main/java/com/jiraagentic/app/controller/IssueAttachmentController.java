package com.jiraagentic.app.controller;

import com.jiraagentic.app.dto.IssueAttachmentDto;
import com.jiraagentic.app.security.AuthSupport;
import com.jiraagentic.app.service.IssueAttachmentService;
import lombok.RequiredArgsConstructor;
import org.springframework.core.io.Resource;
import org.springframework.http.ResponseEntity;
import org.springframework.security.core.Authentication;
import org.springframework.web.bind.annotation.*;
import org.springframework.web.multipart.MultipartFile;

import java.util.List;

@RestController
@RequestMapping("/api/issues/{issueId}/attachments")
@RequiredArgsConstructor
public class IssueAttachmentController {

    private final IssueAttachmentService issueAttachmentService;

    @GetMapping
    public List<IssueAttachmentDto> getByIssue(@PathVariable Long issueId) {
        return issueAttachmentService.findByIssue(issueId);
    }

    @PostMapping(consumes = "multipart/form-data")
    public IssueAttachmentDto upload(
            @PathVariable Long issueId,
            @RequestPart("file") MultipartFile file,
            @RequestParam(value = "embedded", defaultValue = "false") boolean embedded,
            Authentication authentication) {
        return issueAttachmentService.upload(issueId, file, AuthSupport.extractUid(authentication), !embedded);
    }

    @GetMapping("/{attachmentId}/download")
    public ResponseEntity<Resource> download(@PathVariable Long issueId, @PathVariable Long attachmentId) {
        return issueAttachmentService.download(issueId, attachmentId);
    }

    @DeleteMapping("/{attachmentId}")
    public ResponseEntity<Void> delete(
            @PathVariable Long issueId,
            @PathVariable Long attachmentId,
            Authentication authentication) {
        issueAttachmentService.delete(issueId, attachmentId, AuthSupport.extractUid(authentication));
        return ResponseEntity.noContent().build();
    }
}
