package com.jiraagentic.app.dto;

import lombok.Data;

@Data
public class CreateCommentRequest {
    private Long authorId;
    private String content;
}
