package com.jiraagentic.app.service;

import com.jiraagentic.app.dto.CommentDto;
import com.jiraagentic.app.dto.CreateCommentRequest;
import com.jiraagentic.app.entity.Comment;
import com.jiraagentic.app.entity.Issue;
import com.jiraagentic.app.repository.CommentRepository;
import com.jiraagentic.app.repository.IssueRepository;
import com.jiraagentic.app.repository.UserRepository;
import lombok.RequiredArgsConstructor;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.util.List;
import java.util.stream.Collectors;

@Service
@RequiredArgsConstructor
public class CommentService {

    private final CommentRepository commentRepository;
    private final IssueRepository issueRepository;
    private final UserRepository userRepository;
    private final IssueHistoryService issueHistoryService;
    private final IssueAttachmentService issueAttachmentService;
    private final ActiveSpaceGuard activeSpaceGuard;

    public List<CommentDto> findByIssue(Long issueId) {
        Issue issue = issueRepository.findById(issueId)
                .orElseThrow(() -> new RuntimeException("Issue not found: " + issueId));
        activeSpaceGuard.requireActive(issue.getSpace());
        return commentRepository.findByIssueIdOrderByCreatedAtAsc(issueId).stream()
                .map(CommentDto::from)
                .collect(Collectors.toList());
    }

    @Transactional
    public CommentDto create(Long issueId, CreateCommentRequest req) {
        Issue issue = issueRepository.findById(issueId)
                .orElseThrow(() -> new RuntimeException("Issue not found: " + issueId));
        activeSpaceGuard.requireActive(issue.getSpace());

        Comment comment = new Comment();
        comment.setIssue(issue);
        comment.setAuthor(userRepository.findById(req.getAuthorId())
                .orElseThrow(() -> new RuntimeException("User not found: " + req.getAuthorId())));
        comment.setContent(req.getContent());
        Comment saved = commentRepository.save(comment);
        issueHistoryService.recordEvent(issue, req.getAuthorId(), "comment_created", "added a comment");
        return CommentDto.from(saved);
    }

    @Transactional
    public CommentDto update(Long commentId, String content) {
        Comment comment = commentRepository.findById(commentId)
                .orElseThrow(() -> new RuntimeException("Comment not found: " + commentId));
        activeSpaceGuard.requireActive(comment.getIssue().getSpace());
        String oldContent = comment.getContent();
        comment.setContent(content);
        Comment saved = commentRepository.save(comment);
        issueHistoryService.recordFieldChange(
                saved.getIssue(),
                saved.getAuthor() != null ? saved.getAuthor().getId() : null,
                "comment",
                oldContent,
                saved.getContent()
        );
        issueAttachmentService.reconcileEmbeddedAttachments(saved.getIssue().getId(),
                saved.getAuthor() != null ? saved.getAuthor().getId() : null);
        return CommentDto.from(saved);
    }

    @Transactional
    public void delete(Long commentId) {
        Comment comment = commentRepository.findById(commentId).orElse(null);
        if (comment == null) {
            throw new RuntimeException("Comment not found: " + commentId);
        }
        activeSpaceGuard.requireActive(comment.getIssue().getSpace());
        issueHistoryService.recordEvent(
                comment.getIssue(),
                comment.getAuthor() != null ? comment.getAuthor().getId() : null,
                "comment_deleted",
                "deleted a comment"
        );
        Long issueId = comment.getIssue().getId();
        Long actorId = comment.getAuthor() != null ? comment.getAuthor().getId() : null;
        commentRepository.deleteById(commentId);
        issueAttachmentService.reconcileEmbeddedAttachments(issueId, actorId);
    }
}
