package com.jiraagentic.app.dto;

import lombok.Data;

@Data
public class AddMemberRequest {
    private Long userId;
    private String role;
}
