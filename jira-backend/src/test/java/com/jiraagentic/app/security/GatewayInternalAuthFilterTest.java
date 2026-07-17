package com.jiraagentic.app.security;

import org.junit.jupiter.api.Test;
import org.springframework.mock.web.MockFilterChain;
import org.springframework.mock.web.MockHttpServletRequest;
import org.springframework.mock.web.MockHttpServletResponse;

import static org.assertj.core.api.Assertions.assertThat;

class GatewayInternalAuthFilterTest {

    private final GatewayInternalAuthFilter filter = new GatewayInternalAuthFilter("test-gateway-token");

    @Test
    void allowsOnlyConfiguredManagementEndpointsWithoutGatewayHeader() throws Exception {
        assertRequestAllowed("/actuator/health");
        assertRequestAllowed("/actuator/health/liveness");
        assertRequestAllowed("/actuator/health/readiness");
        assertRequestAllowed("/actuator/prometheus");
    }

    @Test
    void stillProtectsOtherActuatorEndpoints() throws Exception {
        MockHttpServletRequest request = new MockHttpServletRequest("GET", "/actuator/env");
        MockHttpServletResponse response = new MockHttpServletResponse();
        MockFilterChain chain = new MockFilterChain();

        filter.doFilter(request, response, chain);

        assertThat(response.getStatus()).isEqualTo(403);
        assertThat(chain.getRequest()).isNull();
    }

    private void assertRequestAllowed(String path) throws Exception {
        MockHttpServletRequest request = new MockHttpServletRequest("GET", path);
        MockHttpServletResponse response = new MockHttpServletResponse();
        MockFilterChain chain = new MockFilterChain();

        filter.doFilter(request, response, chain);

        assertThat(response.getStatus()).isEqualTo(200);
        assertThat(chain.getRequest()).isSameAs(request);
    }
}
