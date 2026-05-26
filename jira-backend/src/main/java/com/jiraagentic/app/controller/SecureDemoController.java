package com.jiraagentic.app.controller;

import io.swagger.v3.oas.annotations.Operation;
import io.swagger.v3.oas.annotations.security.SecurityRequirement;
import org.springframework.security.core.Authentication;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

import java.util.Map;

@RestController
@RequestMapping("/api/secure")
public class SecureDemoController {

    @GetMapping("/ping")
    @Operation(summary = "Secure ping", security = @SecurityRequirement(name = "bearerAuth"))
    public Map<String, Object> ping(Authentication authentication) {
        return Map.of(
                "ok", true,
                "principal", authentication != null ? authentication.getName() : "unknown"
        );
    }
}

