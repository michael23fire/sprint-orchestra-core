package com.jiraagentic.gateway;

import org.springframework.cloud.gateway.filter.ratelimit.KeyResolver;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.security.core.Authentication;
import org.springframework.security.oauth2.server.resource.authentication.JwtAuthenticationToken;
import org.springframework.web.server.ServerWebExchange;
import reactor.core.publisher.Mono;

@Configuration
public class GatewayRateLimitConfig {

    @Bean
    public KeyResolver userOrIpKeyResolver() {
        return exchange -> exchange.getPrincipal()
                .cast(Authentication.class)
                .filter(Authentication::isAuthenticated)
                .map(auth -> resolveAuthKey(exchange, auth))
                .switchIfEmpty(Mono.fromSupplier(() -> resolveIpKey(exchange)));
    }

    private static String resolveAuthKey(ServerWebExchange exchange, Authentication authentication) {
        if (authentication instanceof JwtAuthenticationToken jwtAuthentication) {
            Object uid = jwtAuthentication.getToken().getClaim("uid");
            if (uid != null) {
                return "uid:" + uid;
            }
        }
        String principalName = authentication.getName();
        if (principalName != null && !principalName.isBlank()) {
            return "user:" + principalName;
        }
        return resolveIpKey(exchange);
    }

    private static String resolveIpKey(ServerWebExchange exchange) {
        if (exchange.getRequest().getRemoteAddress() != null
                && exchange.getRequest().getRemoteAddress().getAddress() != null) {
            return "ip:" + exchange.getRequest().getRemoteAddress().getAddress().getHostAddress();
        }
        return "ip:unknown";
    }
}
