package com.jiraagentic.app.config;

import jakarta.annotation.PostConstruct;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.security.oauth2.client.registration.ClientRegistration;
import org.springframework.security.oauth2.client.registration.ClientRegistrationRepository;
import org.springframework.security.oauth2.client.registration.InMemoryClientRegistrationRepository;
import org.springframework.security.oauth2.core.AuthorizationGrantType;
import org.springframework.security.oauth2.core.ClientAuthenticationMethod;
import org.springframework.util.StringUtils;

@Configuration
@ConditionalOnProperty(name = "app.oauth2.github.enabled", havingValue = "true")
public class GithubOAuthClientConfig {
    @Value("${app.oauth2.github.client-id}")
    private String clientId;

    @Value("${app.oauth2.github.client-secret}")
    private String clientSecret;

    @Value("${app.oauth2.github.redirect-uri:http://localhost:8080/login/oauth2/code/github}")
    private String redirectUri;

    @PostConstruct
    void validate() {
        if (!StringUtils.hasText(clientId) || !StringUtils.hasText(clientSecret)) {
            throw new IllegalStateException("app.oauth2.github.enabled=true but GITHUB_CLIENT_ID / GITHUB_CLIENT_SECRET are missing");
        }
    }

    @Bean
    ClientRegistrationRepository clientRegistrationRepository() {
        ClientRegistration github = ClientRegistration.withRegistrationId("github")
                .clientId(clientId)
                .clientSecret(clientSecret)
                .clientAuthenticationMethod(ClientAuthenticationMethod.CLIENT_SECRET_BASIC)
                .authorizationGrantType(AuthorizationGrantType.AUTHORIZATION_CODE)
                .redirectUri(redirectUri)
                .scope("read:user", "user:email")
                .authorizationUri("https://github.com/login/oauth/authorize")
                .tokenUri("https://github.com/login/oauth/access_token")
                .userInfoUri("https://api.github.com/user")
                .userNameAttributeName("id")
                .clientName("GitHub")
                .build();
        return new InMemoryClientRegistrationRepository(github);
    }
}
