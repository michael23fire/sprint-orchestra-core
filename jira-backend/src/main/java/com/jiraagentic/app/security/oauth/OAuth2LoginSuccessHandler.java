package com.jiraagentic.app.security.oauth;

import com.jiraagentic.app.entity.User;
import com.jiraagentic.app.service.JwtTokenService;
import com.jiraagentic.app.service.UserService;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import lombok.RequiredArgsConstructor;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.security.core.Authentication;
import org.springframework.security.oauth2.client.authentication.OAuth2AuthenticationToken;
import org.springframework.security.oauth2.core.user.OAuth2User;
import org.springframework.security.web.authentication.AuthenticationSuccessHandler;
import org.springframework.stereotype.Component;

import java.io.IOException;
import java.net.URLEncoder;
import java.nio.charset.StandardCharsets;

@Component
@RequiredArgsConstructor
@ConditionalOnProperty(name = "app.oauth2.github.enabled", havingValue = "true")
public class OAuth2LoginSuccessHandler implements AuthenticationSuccessHandler {

    private final UserService userService;
    private final JwtTokenService jwtTokenService;

    @Value("${app.frontend.base-url:http://localhost:5173}")
    private String frontendBaseUrl;

    @Override
    public void onAuthenticationSuccess(
            HttpServletRequest request,
            HttpServletResponse response,
            Authentication authentication
    ) throws IOException {
        if (!(authentication instanceof OAuth2AuthenticationToken oauth2Token)) {
            response.sendRedirect(frontendBaseUrl + "/login?error=oauth");
            return;
        }

        OAuth2User principal = oauth2Token.getPrincipal();
        Object idObj = principal.getAttribute("id");
        String githubId = idObj != null ? String.valueOf(idObj) : null;
        String login = principal.getAttribute("login");
        String email = principal.getAttribute("email");
        String name = principal.getAttribute("name");

        if (githubId == null || login == null) {
            response.sendRedirect(frontendBaseUrl + "/login?error=oauth_missing_claims");
            return;
        }

        if (email == null || email.isBlank()) {
            email = login + "@users.noreply.github.com";
        }
        if (name == null || name.isBlank()) {
            name = login;
        }

        User user = userService.upsertFromGithub(githubId, email, name);
        String jwt = jwtTokenService.issueToken(user);

        if (request.getSession(false) != null) {
            request.getSession().invalidate();
        }

        String tokenEnc = URLEncoder.encode(jwt, StandardCharsets.UTF_8);
        String target = frontendBaseUrl.replaceAll("/$", "")
                + "/oauth-callback?access_token=" + tokenEnc + "&token_type=Bearer";
        response.sendRedirect(target);
    }
}
