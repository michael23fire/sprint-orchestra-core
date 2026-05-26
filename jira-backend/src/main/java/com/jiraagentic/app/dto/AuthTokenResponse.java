package com.jiraagentic.app.dto;

import lombok.AllArgsConstructor;
import lombok.Data;

@Data
@AllArgsConstructor
public class AuthTokenResponse {
    private String accessToken;
    private String tokenType;
    private long expiresInMinutes;
    private UserDto user;
}

