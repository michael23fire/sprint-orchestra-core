package com.jiraagentic.app.security;

import jakarta.servlet.FilterChain;
import jakarta.servlet.ServletException;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.security.core.context.SecurityContextHolder;
import org.springframework.stereotype.Component;
import org.springframework.util.StringUtils;
import org.springframework.web.filter.OncePerRequestFilter;

import java.io.IOException;

/**
 * Production-style trust boundary: only requests that include the shared secret header
 * from the API gateway are accepted. User identity comes from gateway-forwarded headers
 * after JWT validation at the edge.
 */
@Component
public class GatewayInternalAuthFilter extends OncePerRequestFilter {

    public static final String GATEWAY_INTERNAL_HEADER = "X-Gateway-Internal";
    public static final String USER_ID_HEADER = "X-User-Id";
    public static final String USERNAME_HEADER = "X-Username";

    private final String expectedGatewayToken;

    public GatewayInternalAuthFilter(@Value("${app.security.internal-gateway-token}") String expectedGatewayToken) {
        this.expectedGatewayToken = expectedGatewayToken;
    }

    @Override
    protected boolean shouldNotFilter(HttpServletRequest request) {
        String path = request.getRequestURI();
        return path.startsWith("/error");
    }

    @Override
    protected void doFilterInternal(
            HttpServletRequest request,
            HttpServletResponse response,
            FilterChain filterChain
    ) throws ServletException, IOException {
        String presented = request.getHeader(GATEWAY_INTERNAL_HEADER);
        if (!StringUtils.hasText(presented) || !expectedGatewayToken.equals(presented)) {
            response.sendError(HttpServletResponse.SC_FORBIDDEN, "Missing or invalid gateway trust header");
            return;
        }

        if (requiresUserIdentity(request.getRequestURI())) {
            String uidHeader = request.getHeader(USER_ID_HEADER);
            String username = request.getHeader(USERNAME_HEADER);
            if (!StringUtils.hasText(uidHeader) || !StringUtils.hasText(username)) {
                response.sendError(HttpServletResponse.SC_UNAUTHORIZED, "Missing gateway user headers");
                return;
            }
            try {
                long uid = Long.parseLong(uidHeader.trim());
                SecurityContextHolder.getContext().setAuthentication(new GatewayForwardedAuthentication(uid, username));
            } catch (NumberFormatException e) {
                response.sendError(HttpServletResponse.SC_UNAUTHORIZED, "Invalid user id header");
                return;
            }
        } else {
            SecurityContextHolder.clearContext();
        }

        filterChain.doFilter(request, response);
    }

    private boolean requiresUserIdentity(String uri) {
        if (!uri.startsWith("/api/")) {
            return false;
        }
        // Match collection root GET /api/users and POST /api/users (no trailing slash) as well as sub-paths.
        if (uri.startsWith("/api/auth/") || "/api/auth".equals(uri)) {
            return false;
        }
        if (uri.startsWith("/api/users/") || "/api/users".equals(uri)) {
            return false;
        }
        return true;
    }
}
