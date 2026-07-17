package com.jiraagentic.app.security.oauth;

import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.security.oauth2.client.registration.ClientRegistration;
import org.springframework.security.oauth2.client.registration.ClientRegistrationRepository;

import static org.assertj.core.api.Assertions.assertThat;

@SpringBootTest(properties = {
        "app.oauth2.github.enabled=true",
        "app.oauth2.github.client-id=test-client-id",
        "app.oauth2.github.client-secret=test-client-secret",
        "management.health.db.enabled=false",
        "management.health.redis.enabled=false"
})
class OAuth2ConfigurationTests {

    @Autowired
    private OAuth2LoginSuccessHandler loginSuccessHandler;

    @Autowired
    private ClientRegistrationRepository clientRegistrationRepository;

    @Test
    void movedLoginSuccessHandlerAndGithubRegistrationAreLoadedWhenOauthIsEnabled() {
        ClientRegistration github = clientRegistrationRepository.findByRegistrationId("github");

        assertThat(loginSuccessHandler).isNotNull();
        assertThat(github).isNotNull();
        assertThat(github.getClientId()).isEqualTo("test-client-id");
        assertThat(github.getRedirectUri()).isEqualTo("http://localhost:8080/login/oauth2/code/github");
    }
}
