package com.jiraagentic.gateway;

import org.junit.jupiter.api.Test;
import org.springframework.cloud.gateway.filter.GatewayFilterChain;
import org.springframework.http.HttpHeaders;
import org.springframework.mock.http.server.reactive.MockServerHttpRequest;
import org.springframework.mock.web.server.MockServerWebExchange;
import org.springframework.security.oauth2.jwt.Jwt;
import org.springframework.security.oauth2.server.resource.authentication.JwtAuthenticationToken;
import org.springframework.security.core.context.ReactiveSecurityContextHolder;
import org.springframework.web.server.ServerWebExchange;
import reactor.core.publisher.Mono;

import java.time.Instant;
import java.util.concurrent.atomic.AtomicReference;

import static org.assertj.core.api.Assertions.assertThat;

class PropagateUserHeadersGatewayFilterTest {

    private final PropagateUserHeadersGatewayFilter filter = new PropagateUserHeadersGatewayFilter();

    @Test
    void replacesClientSuppliedIdentityHeadersWithValidatedJwtClaims() {
        MockServerWebExchange exchange = MockServerWebExchange.from(
                MockServerHttpRequest.get("/api/spaces")
                        .header(HttpHeaders.AUTHORIZATION, "Bearer public-token")
                        .header(PropagateUserHeadersGatewayFilter.USER_ID_HEADER, "999")
                        .header(PropagateUserHeadersGatewayFilter.USERNAME_HEADER, "mallory")
        );
        AtomicReference<ServerWebExchange> forwarded = new AtomicReference<>();
        GatewayFilterChain chain = e -> {
            forwarded.set(e);
            return Mono.empty();
        };

        Jwt jwt = Jwt.withTokenValue("validated-token")
                .header("alg", "none")
                .subject("alice")
                .claim("uid", 42L)
                .issuedAt(Instant.now())
                .expiresAt(Instant.now().plusSeconds(300))
                .build();

        filter.filter(exchange, chain)
                .contextWrite(ReactiveSecurityContextHolder.withAuthentication(new JwtAuthenticationToken(jwt)))
                .block();

        HttpHeaders headers = forwarded.get().getRequest().getHeaders();
        assertThat(headers.get(PropagateUserHeadersGatewayFilter.USER_ID_HEADER)).containsExactly("42");
        assertThat(headers.get(PropagateUserHeadersGatewayFilter.USERNAME_HEADER)).containsExactly("alice");
        assertThat(headers.containsKey(HttpHeaders.AUTHORIZATION)).isFalse();
    }

    @Test
    void stripsClientIdentityHeadersWhenThereIsNoAuthenticatedJwt() {
        MockServerWebExchange exchange = MockServerWebExchange.from(
                MockServerHttpRequest.get("/api/auth/config")
                        .header(PropagateUserHeadersGatewayFilter.USER_ID_HEADER, "999")
                        .header(PropagateUserHeadersGatewayFilter.USERNAME_HEADER, "mallory")
        );
        AtomicReference<ServerWebExchange> forwarded = new AtomicReference<>();

        filter.filter(exchange, e -> {
            forwarded.set(e);
            return Mono.empty();
        }).block();

        HttpHeaders headers = forwarded.get().getRequest().getHeaders();
        assertThat(headers.containsKey(PropagateUserHeadersGatewayFilter.USER_ID_HEADER)).isFalse();
        assertThat(headers.containsKey(PropagateUserHeadersGatewayFilter.USERNAME_HEADER)).isFalse();
    }
}
