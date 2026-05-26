package com.jiraagentic.gateway;

import org.springframework.cloud.gateway.filter.GatewayFilterChain;
import org.springframework.cloud.gateway.filter.GlobalFilter;
import org.springframework.core.Ordered;
import org.springframework.http.HttpHeaders;
import org.springframework.http.server.reactive.ServerHttpRequest;
import org.springframework.security.core.context.ReactiveSecurityContextHolder;
import org.springframework.security.oauth2.server.resource.authentication.JwtAuthenticationToken;
import org.springframework.stereotype.Component;
import org.springframework.web.server.ServerWebExchange;
import reactor.core.publisher.Mono;

/**
 * After JWT authentication at the gateway, forwards uid/subject as headers to internal services
 * and strips the client {@code Authorization} header so backends rely on gateway trust + forwarded identity only.
 */
@Component
public class PropagateUserHeadersGatewayFilter implements GlobalFilter, Ordered {

    @Override
    public Mono<Void> filter(ServerWebExchange exchange, GatewayFilterChain chain) {
        return ReactiveSecurityContextHolder.getContext()
                .flatMap(ctx -> {
                    if (ctx.getAuthentication() instanceof JwtAuthenticationToken jwt) {
                        return chain.filter(mutateForJwt(exchange, jwt));
                    }
                    return chain.filter(stripAuthorizationOnly(exchange));
                })
                .switchIfEmpty(chain.filter(stripAuthorizationOnly(exchange)));
    }

    private static ServerWebExchange mutateForJwt(ServerWebExchange exchange, JwtAuthenticationToken jwt) {
        Object uidClaim = jwt.getToken().getClaim("uid");
        String uidStr = null;
        if (uidClaim instanceof Number n) {
            uidStr = String.valueOf(n.longValue());
        } else if (uidClaim != null) {
            uidStr = uidClaim.toString();
        }
        String subject = jwt.getToken().getSubject();
        ServerHttpRequest.Builder b = exchange.getRequest().mutate();
        if (uidStr != null) {
            b.header("X-User-Id", uidStr);
        }
        if (subject != null) {
            b.header("X-Username", subject);
        }
        b.headers(h -> h.remove(HttpHeaders.AUTHORIZATION));
        return exchange.mutate().request(b.build()).build();
    }

    private static ServerWebExchange stripAuthorizationOnly(ServerWebExchange exchange) {
        ServerHttpRequest req = exchange.getRequest().mutate()
                .headers(h -> h.remove(HttpHeaders.AUTHORIZATION))
                .build();
        return exchange.mutate().request(req).build();
    }

    @Override
    public int getOrder() {
        return Ordered.LOWEST_PRECEDENCE - 1;
    }
}
