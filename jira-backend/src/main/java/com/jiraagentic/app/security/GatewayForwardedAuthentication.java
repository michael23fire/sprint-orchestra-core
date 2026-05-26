package com.jiraagentic.app.security;

import org.springframework.security.authentication.AbstractAuthenticationToken;
import org.springframework.security.core.authority.SimpleGrantedAuthority;

import java.util.List;

/**
 * Identity propagated from the API gateway after JWT validation (production: only the gateway may reach this service).
 */
public class GatewayForwardedAuthentication extends AbstractAuthenticationToken {

    private final Long userId;
    private final String username;

    public GatewayForwardedAuthentication(Long userId, String username) {
        super(List.of(new SimpleGrantedAuthority("ROLE_USER")));
        this.userId = userId;
        this.username = username;
        setAuthenticated(true);
    }

    @Override
    public Object getCredentials() {
        return "";
    }

    @Override
    public Object getPrincipal() {
        return username;
    }

    public Long getUserId() {
        return userId;
    }
}
