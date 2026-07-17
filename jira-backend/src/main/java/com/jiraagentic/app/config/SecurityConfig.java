package com.jiraagentic.app.config;

import com.jiraagentic.app.security.GatewayInternalAuthFilter;
import com.jiraagentic.app.security.oauth.OAuth2LoginSuccessHandler;
import org.springframework.beans.factory.ObjectProvider;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.security.config.annotation.web.builders.HttpSecurity;
import org.springframework.security.config.annotation.web.configurers.AbstractHttpConfigurer;
import org.springframework.security.oauth2.client.registration.ClientRegistrationRepository;
import org.springframework.security.oauth2.client.web.DefaultOAuth2AuthorizationRequestResolver;
import org.springframework.security.web.SecurityFilterChain;
import org.springframework.security.web.authentication.AnonymousAuthenticationFilter;

@Configuration
public class SecurityConfig {

    @Bean
    public SecurityFilterChain securityFilterChain(
            HttpSecurity http,
            ObjectProvider<ClientRegistrationRepository> clientRegistrationRepositoryProvider,
            ObjectProvider<OAuth2LoginSuccessHandler> oauth2LoginSuccessHandler,
            GatewayInternalAuthFilter gatewayInternalAuthFilter
    ) throws Exception {
        http
                .csrf(AbstractHttpConfigurer::disable)
                .addFilterBefore(gatewayInternalAuthFilter, AnonymousAuthenticationFilter.class)
                .authorizeHttpRequests(auth -> auth
                        .requestMatchers(
                                "/swagger-ui/**",
                                "/swagger-ui.html",
                                "/v3/api-docs/**",
                                "/api/auth/**",
                                "/api/users/**",
                                "/oauth2/**",
                                "/login/**",
                                "/actuator/health/**",
                                "/actuator/prometheus",
                                "/error"
                        ).permitAll()
                        .requestMatchers("/api/**").authenticated()
                        .anyRequest().permitAll()
                )
                .logout(logout -> logout
                        .logoutUrl("/logout")
                        .logoutSuccessUrl("/swagger-ui/index.html")
                        .permitAll()
                );

        if (clientRegistrationRepositoryProvider.getIfAvailable() != null) {
            ClientRegistrationRepository registrations = clientRegistrationRepositoryProvider.getIfAvailable();
            DefaultOAuth2AuthorizationRequestResolver authorizationRequestResolver =
                    new DefaultOAuth2AuthorizationRequestResolver(registrations, "/oauth2/authorization");
            authorizationRequestResolver.setAuthorizationRequestCustomizer(builder ->
                    builder.additionalParameters(parameters -> parameters.put("prompt", "select_account")));

            http.oauth2Login(oauth2 -> {
                oauth2.authorizationEndpoint(endpoint ->
                        endpoint.authorizationRequestResolver(authorizationRequestResolver));
                oauth2LoginSuccessHandler.ifAvailable(oauth2::successHandler);
            });
        }

        return http.build();
    }
}
