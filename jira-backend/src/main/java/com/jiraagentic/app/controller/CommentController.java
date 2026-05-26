package com.jiraagentic.app.controller;

import com.jiraagentic.app.dto.CommentDto;
import com.jiraagentic.app.dto.CreateCommentRequest;
import com.jiraagentic.app.service.CommentService;
import lombok.RequiredArgsConstructor;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;

import java.util.List;
import java.util.Map;

@RestController
@RequestMapping("/api/issues/{issueId}/comments")
@RequiredArgsConstructor
public class CommentController {

    private final CommentService commentService;

    @GetMapping
    public List<CommentDto> getByIssue(@PathVariable Long issueId) {
        return commentService.findByIssue(issueId);
    }

    @PostMapping
    public CommentDto create(@PathVariable Long issueId, @RequestBody CreateCommentRequest req) {
        return commentService.create(issueId, req);
    }

    @PutMapping("/{commentId}")
    public CommentDto update(@PathVariable Long commentId, @RequestBody Map<String, String> body) {
        return commentService.update(commentId, body.get("content"));
    }

    @DeleteMapping("/{commentId}")
    public ResponseEntity<Void> delete(@PathVariable Long commentId) {
        commentService.delete(commentId);
        return ResponseEntity.noContent().build();
    }
}
