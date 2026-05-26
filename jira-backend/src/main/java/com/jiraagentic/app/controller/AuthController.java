package com.jiraagentic.app.controller;

import com.jiraagentic.app.dto.AuthTokenResponse;
import com.jiraagentic.app.dto.UserDto;
import com.jiraagentic.app.entity.User;
import com.jiraagentic.app.service.JwtTokenService;
import com.jiraagentic.app.service.UserService;
import io.swagger.v3.oas.annotations.Operation;
import lombok.RequiredArgsConstructor;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;

import java.util.Map;

@RestController
@RequestMapping("/api/auth")
@RequiredArgsConstructor
public class AuthController {

    private final UserService userService;
    private final JwtTokenService jwtTokenService;

    @Value("${app.oauth2.github.enabled:false}")
    private boolean githubOAuthEnabled;

    @GetMapping("/config")
    @Operation(summary = "Public auth options for the login UI")
    public Map<String, Boolean> authConfig() {
        return Map.of(
                "githubOAuthEnabled", githubOAuthEnabled
        );
    }

    @PostMapping("/token")
    @Operation(summary = "Username/password login and issue bearer JWT")
    public ResponseEntity<?> issueToken(@RequestBody Map<String, String> body) {
        String username = body.get("username");
        String password = body.get("password");
        if (username == null || password == null) {
            return ResponseEntity.badRequest().body(Map.of("message", "Username and password are required"));
        }
        try {
            User user = userService.authenticate(username, password);
            String token = jwtTokenService.issueToken(user);
            return ResponseEntity.ok(new AuthTokenResponse(
                    token,
                    "Bearer",
                    jwtTokenService.getExpiresMinutes(),
                    UserDto.from(user)
            ));
        } catch (RuntimeException e) {
            return ResponseEntity.status(401).body(Map.of("message", e.getMessage()));
        }
    }
}
