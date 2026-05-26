package com.jiraagentic.app.dto;

import com.jiraagentic.app.entity.Comment;
import lombok.Data;
import java.time.Instant;

@Data
public class CommentDto {
    private Long id;
    private Long issueId;
    private Long authorId;
    private String authorName;
    private String content;
    private Instant createdAt;

    public static CommentDto from(Comment c) {
        CommentDto dto = new CommentDto();
        dto.setId(c.getId());
        dto.setIssueId(c.getIssue().getId());
        dto.setAuthorId(c.getAuthor().getId());
        dto.setAuthorName(c.getAuthor().getName());
        dto.setContent(c.getContent());
        dto.setCreatedAt(c.getCreatedAt());
        return dto;
    }
}
