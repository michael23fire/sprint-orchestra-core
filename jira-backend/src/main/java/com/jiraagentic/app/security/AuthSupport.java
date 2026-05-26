package com.jiraagentic.app.security;

import org.springframework.security.core.Authentication;

public final class AuthSupport {

    private AuthSupport() {
    }

    public static Long extractUid(Authentication authentication) {
        if (authentication instanceof GatewayForwardedAuthentication g) {
            return g.getUserId();
        }
        throw new IllegalStateException("Expected gateway-forwarded identity");
    }

    public static Long extractUidOrNull(Authentication authentication) {
        if (authentication == null || !authentication.isAuthenticated()) {
            return null;
        }
        if (authentication instanceof GatewayForwardedAuthentication g) {
            return g.getUserId();
        }
        return null;
    }
}
