package com.jiraagentic.app.service;

import java.net.URI;
import java.net.URISyntaxException;
import java.util.Optional;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * Parses a raw URL pasted by a user into a structured shape we can render and
 * optionally enrich via the GitHub REST API.
 *
 * Supported (github.com) shapes:
 *   - https://github.com/{owner}/{repo}                            → repo
 *   - https://github.com/{owner}/{repo}/pull/{number}              → pull_request
 *   - https://github.com/{owner}/{repo}/commit/{sha}               → commit
 *   - https://github.com/{owner}/{repo}/tree/{branch}              → branch
 *
 * Unknown URLs fall back to {@code kind = "other"}.
 */
public final class GithubUrlParser {

    public record ParsedLink(
            String provider,
            String kind,
            String owner,
            String repo,
            String refId,
            String fallbackTitle
    ) {}

    private static final Pattern PR_P = Pattern.compile("^/([^/]+)/([^/]+)/pull/(\\d+)(?:/.*)?$");
    private static final Pattern COMMIT_P = Pattern.compile("^/([^/]+)/([^/]+)/commit/([0-9a-fA-F]{7,40})(?:/.*)?$");
    private static final Pattern BRANCH_P = Pattern.compile("^/([^/]+)/([^/]+)/tree/([^/]+)(?:/.*)?$");
    private static final Pattern REPO_P = Pattern.compile("^/([^/]+)/([^/]+)/?$");

    private GithubUrlParser() {}

    public static ParsedLink parse(String rawUrl) {
        if (rawUrl == null || rawUrl.isBlank()) {
            return new ParsedLink("other", "other", null, null, null, rawUrl);
        }
        String url = rawUrl.trim();
        URI uri;
        try {
            uri = new URI(url);
        } catch (URISyntaxException ex) {
            return new ParsedLink("other", "other", null, null, null, url);
        }

        String host = uri.getHost() == null ? "" : uri.getHost().toLowerCase();
        String path = uri.getPath() == null ? "" : uri.getPath();

        boolean isGithub = host.endsWith("github.com");
        if (!isGithub) {
            // Not GitHub — we still want to store the link so users can paste
            // GitLab, Bitbucket, etc. We just mark it as "other" without parsing.
            return new ParsedLink(providerFromHost(host), "other", null, null, null, url);
        }

        Matcher m;
        if ((m = PR_P.matcher(path)).matches()) {
            String owner = m.group(1);
            String repo = m.group(2);
            String number = m.group(3);
            return new ParsedLink("github", "pull_request", owner, repo, number,
                    owner + "/" + repo + " PR #" + number);
        }
        if ((m = COMMIT_P.matcher(path)).matches()) {
            String owner = m.group(1);
            String repo = m.group(2);
            String sha = m.group(3);
            String shortSha = sha.substring(0, Math.min(sha.length(), 7));
            return new ParsedLink("github", "commit", owner, repo, sha,
                    owner + "/" + repo + "@" + shortSha);
        }
        if ((m = BRANCH_P.matcher(path)).matches()) {
            String owner = m.group(1);
            String repo = m.group(2);
            String branch = m.group(3);
            return new ParsedLink("github", "branch", owner, repo, branch,
                    owner + "/" + repo + " (" + branch + ")");
        }
        if ((m = REPO_P.matcher(path)).matches()) {
            String owner = m.group(1);
            String repo = m.group(2);
            return new ParsedLink("github", "repo", owner, repo, null,
                    owner + "/" + repo);
        }
        return new ParsedLink("github", "other", null, null, null, url);
    }

    private static String providerFromHost(String host) {
        if (host.contains("gitlab")) return "gitlab";
        if (host.contains("bitbucket")) return "bitbucket";
        if (host.isEmpty()) return "other";
        return host;
    }

    public static Optional<String> shortenSha(String sha) {
        if (sha == null) return Optional.empty();
        return Optional.of(sha.substring(0, Math.min(sha.length(), 7)));
    }
}
