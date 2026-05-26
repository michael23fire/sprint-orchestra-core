package com.jiraagentic.app.service;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Component;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Duration;
import java.time.Instant;
import java.time.format.DateTimeParseException;
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import java.util.Optional;

/**
 * Best-effort fetcher that enriches a parsed GitHub link with metadata
 * (title, state, author) using GitHub's public REST API.
 *
 * Works without authentication for public repos, subject to a 60 req/hour
 * rate limit. A token can be provided via the GITHUB_TOKEN env var to lift
 * that to 5000/hr and access private repos. All calls are wrapped in
 * try/catch — failure never blocks the create flow.
 */
@Component
public class GithubMetadataClient {

    private static final Logger log = LoggerFactory.getLogger(GithubMetadataClient.class);
    private static final String API_BASE = "https://api.github.com";

    private final HttpClient http = HttpClient.newBuilder()
            .connectTimeout(Duration.ofSeconds(3))
            .build();
    private final ObjectMapper mapper = new ObjectMapper();

    @Value("${app.github.token:${GITHUB_TOKEN:}}")
    private String token;

    /**
     * @param lastActivityAt GitHub-side timestamp when available (PR {@code updated_at}, commit date, etc.)
     */
    public record Metadata(String title, String state, String authorLogin, Instant lastActivityAt) {}

    public record PullSummary(int number, String title, String body, String state,
                              boolean merged, boolean draft, String authorLogin, String htmlUrl) {}

    public record CommitSummary(String sha, String message, String authorLogin, String htmlUrl) {}

    /** Owner + repo name as returned by GitHub list endpoints (may differ for forks). */
    public record GithubRepoRef(String owner, String name) {}

    /**
     * True if {@code login} is a valid GitHub user or organization handle.
     */
    public boolean accountExists(String login) {
        return accountExists(login, null);
    }

    /**
     * @param tokenOverride optional PAT for this call only; if blank, uses {@link #token}.
     */
    public boolean accountExists(String login, String tokenOverride) {
        if (login == null || login.isBlank()) return false;
        try {
            JsonNode n = get("/users/" + trimLogin(login), tokenOverride);
            return n != null && n.hasNonNull("id");
        } catch (Exception ex) {
            log.debug("GitHub accountExists failed for {}: {}", login, ex.getMessage());
            return false;
        }
    }

    /**
     * Lists every repository visible to the API token for a user or organization.
     * <ul>
     *   <li>Organizations: {@code /orgs/{login}/repos} — private repos when the token has org access.</li>
     *   <li>User <strong>other than</strong> the token owner: {@code /users/{login}/repos} — <em>public only</em> (GitHub API).</li>
     *   <li>User <strong>same as</strong> the token owner: {@code /user/repos} — includes that user's private repos (needs {@code repo} scope or fine-grained repo read).</li>
     * </ul>
     * Paginates until a short page or a safety cap (5000 repos).
     */
    public List<GithubRepoRef> listReposForAccount(String login) {
        return listReposForAccount(login, null);
    }

    /**
     * @param tokenOverride optional PAT for this call only (e.g. bulk import); if blank, uses configured token.
     */
    public List<GithubRepoRef> listReposForAccount(String login, String tokenOverride) {
        if (login == null || login.isBlank()) return Collections.emptyList();
        String sanitized = trimLogin(login);
        try {
            JsonNode meta = get("/users/" + sanitized, tokenOverride);
            if (meta == null || !meta.has("type")) {
                return Collections.emptyList();
            }
            String type = textOrNull(meta.path("type"));
            final String listBase;
            if ("Organization".equalsIgnoreCase(type)) {
                listBase = "/orgs/" + sanitized + "/repos";
            } else {
                String actor = tokenActorLogin(tokenOverride);
                if (actor != null && actor.equalsIgnoreCase(sanitized)) {
                    // /user/repos includes repositories the token can access (including collaborator repos).
                    // We only want repositories owned by the requested account in bulk import.
                    listBase = "/user/repos";
                } else {
                    listBase = "/users/" + sanitized + "/repos";
                }
            }
            List<GithubRepoRef> allVisible = paginateRepoList(listBase, tokenOverride);
            List<GithubRepoRef> ownedByAccount = new ArrayList<>();
            for (GithubRepoRef ref : allVisible) {
                if (ref.owner() != null && ref.owner().equalsIgnoreCase(sanitized)) {
                    ownedByAccount.add(ref);
                }
            }
            return ownedByAccount;
        } catch (Exception ex) {
            log.warn("GitHub listReposForAccount failed for {}: {}", login, ex.getMessage());
            return Collections.emptyList();
        }
    }

    /** Login for the authenticated API user, or null if no token or /user failed. */
    private String tokenActorLogin() {
        return tokenActorLogin(null);
    }

    private String tokenActorLogin(String tokenOverride) {
        if (effectiveBearer(tokenOverride, token) == null) {
            return null;
        }
        try {
            JsonNode n = get("/user", tokenOverride);
            if (n == null) {
                return null;
            }
            return textOrNull(n.path("login"));
        } catch (Exception ex) {
            log.debug("GitHub tokenActorLogin failed: {}", ex.getMessage());
            return null;
        }
    }

    private List<GithubRepoRef> paginateRepoList(String pathBase) {
        return paginateRepoList(pathBase, null);
    }

    private List<GithubRepoRef> paginateRepoList(String pathBase, String tokenOverride) {
        List<GithubRepoRef> out = new ArrayList<>();
        final int perPage = 100;
        final int maxPages = 50;
        for (int page = 1; page <= maxPages; page++) {
            String path = pathBase + "?per_page=" + perPage + "&page=" + page + "&sort=full_name&direction=asc";
            JsonNode arr;
            try {
                arr = get(path, tokenOverride);
            } catch (Exception ex) {
                log.debug("GitHub repo page {} failed for {}: {}", page, pathBase, ex.getMessage());
                break;
            }
            if (arr == null || !arr.isArray() || arr.isEmpty()) {
                break;
            }
            for (JsonNode n : arr) {
                String name = textOrNull(n.path("name"));
                String ownerLogin = textOrNull(n.path("owner").path("login"));
                if (name != null && ownerLogin != null) {
                    out.add(new GithubRepoRef(ownerLogin, name));
                }
            }
            if (arr.size() < perPage) {
                break;
            }
        }
        return out;
    }

    /**
     * Accepts a bare login, {@code @login}, {@code github.com/name}, or
     * {@code https://github.com/name} (and {@code /orgs/x}, {@code /users/x} paths).
     */
    public static String normalizeGithubAccountInput(String raw) {
        if (raw == null) return "";
        String s = raw.trim();
        // Users may paste text with decorative prefixes like "🔗https://github.com/foo".
        // Strip leading non-account chars before parsing.
        s = s.replaceFirst("^[^A-Za-z0-9@hH]+", "");
        if (s.isEmpty()) return "";
        if (s.startsWith("@")) {
            s = s.substring(1).trim();
        }
        if (s.toLowerCase().startsWith("github.com/")) {
            s = "https://" + s;
        }
        if (s.startsWith("http://") || s.startsWith("https://")) {
            try {
                URI u = new URI(s);
                String host = u.getHost();
                if (host != null) {
                    String h = host.toLowerCase();
                    if (h.equals("github.com") || h.equals("www.github.com")) {
                        String path = u.getPath();
                        if (path == null || path.isBlank() || "/".equals(path)) {
                            return "";
                        }
                        List<String> segs = new ArrayList<>();
                        for (String p : path.split("/")) {
                            if (!p.isBlank()) segs.add(p);
                        }
                        if (segs.isEmpty()) return "";
                        String head = segs.get(0);
                        if (head.equalsIgnoreCase("orgs") && segs.size() >= 2) {
                            return segs.get(1);
                        }
                        if (head.equalsIgnoreCase("users") && segs.size() >= 2) {
                            return segs.get(1);
                        }
                        if (head.equalsIgnoreCase("settings")
                                || head.equalsIgnoreCase("pulls")
                                || head.equalsIgnoreCase("issues")) {
                            return "";
                        }
                        return head;
                    }
                }
            } catch (Exception ignored) {
                // fall through: treat whole string as login (may still fail API-side)
            }
        }
        return s;
    }

    private static String trimLogin(String login) {
        return normalizeGithubAccountInput(login);
    }

    /**
     * Returns the most recent PRs (any state) for a repo — newest first.
     * Empty list on error. Caps at 50 per call.
     */
    public List<PullSummary> listRecentPulls(String owner, String repo, int limit) {
        return listRecentPulls(owner, repo, limit, null);
    }

    /**
     * @param tokenOverride optional PAT for this call; if blank, uses configured token.
     */
    public List<PullSummary> listRecentPulls(String owner, String repo, int limit, String tokenOverride) {
        int cap = Math.min(Math.max(limit, 1), 50);
        try {
            JsonNode arr = get("/repos/" + owner + "/" + repo + "/pulls?state=all&per_page=" + cap + "&sort=updated&direction=desc", tokenOverride);
            if (arr == null || !arr.isArray()) return Collections.emptyList();
            List<PullSummary> out = new ArrayList<>();
            for (JsonNode n : arr) {
                int number = n.path("number").asInt();
                String title = textOrNull(n.path("title"));
                String body = textOrNull(n.path("body"));
                String state = textOrNull(n.path("state"));
                boolean merged = n.path("merged_at").isTextual();
                boolean draft = n.path("draft").asBoolean(false);
                String author = textOrNull(n.path("user").path("login"));
                String url = textOrNull(n.path("html_url"));
                if (merged) state = "merged";
                else if (draft) state = "draft";
                out.add(new PullSummary(number, title, body, state, merged, draft, author, url));
            }
            return out;
        } catch (Exception ex) {
            log.debug("GitHub listRecentPulls failed for {}/{}: {}", owner, repo, ex.getMessage());
            return Collections.emptyList();
        }
    }

    /**
     * Returns every commit belonging to a specific pull request — including
     * commits that have not been merged to the default branch yet. This is
     * the GitHub equivalent of "list commits on a PR's head branch" and is
     * what lets us scan open PRs. Empty list on error. Caps at 50 per call.
     */
    public List<CommitSummary> listPullCommits(String owner, String repo, int prNumber, int limit) {
        return listPullCommits(owner, repo, prNumber, limit, null);
    }

    public List<CommitSummary> listPullCommits(String owner, String repo, int prNumber, int limit, String tokenOverride) {
        int cap = Math.min(Math.max(limit, 1), 50);
        try {
            JsonNode arr = get("/repos/" + owner + "/" + repo + "/pulls/" + prNumber + "/commits?per_page=" + cap, tokenOverride);
            if (arr == null || !arr.isArray()) return Collections.emptyList();
            List<CommitSummary> out = new ArrayList<>();
            for (JsonNode n : arr) {
                String sha = textOrNull(n.path("sha"));
                String message = textOrNull(n.path("commit").path("message"));
                String author = textOrNull(n.path("author").path("login"));
                if (author == null) author = textOrNull(n.path("commit").path("author").path("name"));
                String url = textOrNull(n.path("html_url"));
                out.add(new CommitSummary(sha, message, author, url));
            }
            return out;
        } catch (Exception ex) {
            log.debug("GitHub listPullCommits failed for {}/{}#{}: {}", owner, repo, prNumber, ex.getMessage());
            return Collections.emptyList();
        }
    }

    /**
     * Returns the most recent commits on the repo's default branch, newest first.
     */
    public List<CommitSummary> listRecentCommits(String owner, String repo, int limit) {
        return listRecentCommits(owner, repo, limit, null);
    }

    public List<CommitSummary> listRecentCommits(String owner, String repo, int limit, String tokenOverride) {
        int cap = Math.min(Math.max(limit, 1), 50);
        try {
            JsonNode arr = get("/repos/" + owner + "/" + repo + "/commits?per_page=" + cap, tokenOverride);
            if (arr == null || !arr.isArray()) return Collections.emptyList();
            List<CommitSummary> out = new ArrayList<>();
            for (JsonNode n : arr) {
                String sha = textOrNull(n.path("sha"));
                String message = textOrNull(n.path("commit").path("message"));
                String author = textOrNull(n.path("author").path("login"));
                if (author == null) author = textOrNull(n.path("commit").path("author").path("name"));
                String url = textOrNull(n.path("html_url"));
                out.add(new CommitSummary(sha, message, author, url));
            }
            return out;
        } catch (Exception ex) {
            log.debug("GitHub listRecentCommits failed for {}/{}: {}", owner, repo, ex.getMessage());
            return Collections.emptyList();
        }
    }

    /** Quick sanity check that owner/repo exists and is reachable. */
    public boolean repoExists(String owner, String repo) {
        try {
            JsonNode n = get("/repos/" + owner + "/" + repo);
            return n != null && n.hasNonNull("id");
        } catch (Exception ex) {
            return false;
        }
    }

    public Optional<Metadata> fetch(GithubUrlParser.ParsedLink link) {
        return fetch(link, null);
    }

    /**
     * @param tokenOverride optional PAT for this call; if blank, uses configured token.
     */
    public Optional<Metadata> fetch(GithubUrlParser.ParsedLink link, String tokenOverride) {
        if (link == null || !"github".equals(link.provider())) return Optional.empty();
        try {
            return switch (link.kind()) {
                case "pull_request" -> fetchPull(link.owner(), link.repo(), link.refId(), tokenOverride);
                case "commit" -> fetchCommit(link.owner(), link.repo(), link.refId(), tokenOverride);
                case "branch" -> fetchBranch(link.owner(), link.repo(), link.refId(), tokenOverride);
                case "repo" -> fetchRepo(link.owner(), link.repo(), tokenOverride);
                default -> Optional.empty();
            };
        } catch (Exception ex) {
            log.debug("GitHub metadata fetch failed for {} {}/{}: {}", link.kind(), link.owner(), link.repo(), ex.getMessage());
            return Optional.empty();
        }
    }

    private Optional<Metadata> fetchPull(String owner, String repo, String number, String tokenOverride) throws Exception {
        JsonNode n = get("/repos/" + owner + "/" + repo + "/pulls/" + number, tokenOverride);
        if (n == null) return Optional.empty();
        String title = textOrNull(n.path("title"));
        // GitHub PR state is "open" or "closed"; merged PRs are closed with merged=true.
        String state = textOrNull(n.path("state"));
        if (n.path("merged").asBoolean(false)) state = "merged";
        else if (n.path("draft").asBoolean(false)) state = "draft";
        String author = textOrNull(n.path("user").path("login"));
        Instant lastAt = instantOrNull(n.get("updated_at"));
        return Optional.of(new Metadata(title, state, author, lastAt));
    }

    private Optional<Metadata> fetchCommit(String owner, String repo, String sha, String tokenOverride) throws Exception {
        JsonNode n = get("/repos/" + owner + "/" + repo + "/commits/" + sha, tokenOverride);
        if (n == null) return Optional.empty();
        String message = textOrNull(n.path("commit").path("message"));
        String title = message == null ? null : message.split("\n", 2)[0];
        String author = textOrNull(n.path("author").path("login"));
        if (author == null) author = textOrNull(n.path("commit").path("author").path("name"));
        Instant lastAt = instantOrNull(n.path("commit").path("committer").path("date"));
        if (lastAt == null) {
            lastAt = instantOrNull(n.path("commit").path("author").path("date"));
        }
        return Optional.of(new Metadata(title, null, author, lastAt));
    }

    private Optional<Metadata> fetchBranch(String owner, String repo, String branch, String tokenOverride) throws Exception {
        JsonNode n = get("/repos/" + owner + "/" + repo + "/branches/" + branch, tokenOverride);
        if (n == null) return Optional.empty();
        String title = owner + "/" + repo + " · " + branch;
        Instant lastAt = instantOrNull(n.path("commit").path("commit").path("committer").path("date"));
        return Optional.of(new Metadata(title, null, null, lastAt));
    }

    private Optional<Metadata> fetchRepo(String owner, String repo, String tokenOverride) throws Exception {
        JsonNode n = get("/repos/" + owner + "/" + repo, tokenOverride);
        if (n == null) return Optional.empty();
        String desc = textOrNull(n.path("description"));
        String title = desc != null && !desc.isBlank() ? desc : owner + "/" + repo;
        String author = textOrNull(n.path("owner").path("login"));
        Instant lastAt = instantOrNull(n.get("pushed_at"));
        if (lastAt == null) {
            lastAt = instantOrNull(n.get("updated_at"));
        }
        return Optional.of(new Metadata(title, null, author, lastAt));
    }

    private HttpResponse<String> rawGet(String path) throws Exception {
        return rawGet(path, null);
    }

    /**
     * @param tokenOverride if non-blank after trim, used as Bearer; otherwise the configured {@link #token}.
     */
    private HttpResponse<String> rawGet(String path, String tokenOverride) throws Exception {
        final URI uri;
        try {
            uri = URI.create(API_BASE + path);
        } catch (IllegalArgumentException ex) {
            log.debug("Invalid GitHub API URI for path {}: {}", path, ex.getMessage());
            return null;
        }
        HttpRequest.Builder b = HttpRequest.newBuilder()
                .uri(uri)
                .timeout(Duration.ofSeconds(4))
                .header("Accept", "application/vnd.github+json")
                .header("X-GitHub-Api-Version", "2022-11-28")
                .GET();
        String bearer = effectiveBearer(tokenOverride, token);
        if (bearer != null) {
            b.header("Authorization", "Bearer " + bearer);
        }
        return http.send(b.build(), HttpResponse.BodyHandlers.ofString());
    }

    private JsonNode get(String path) throws Exception {
        return get(path, null);
    }

    private JsonNode get(String path, String tokenOverride) throws Exception {
        HttpResponse<String> res = rawGet(path, tokenOverride);
        if (res == null) return null;
        if (res.statusCode() / 100 != 2) return null;
        String body = res.body();
        if (body == null || body.isBlank()) return null;
        return mapper.readTree(body);
    }

    /** Prefer trimmed {@code tokenOverride} when non-empty; else configured token. */
    private static String effectiveBearer(String tokenOverride, String configuredToken) {
        if (tokenOverride != null) {
            String t = tokenOverride.trim();
            if (!t.isEmpty()) {
                return t;
            }
        }
        if (configuredToken != null && !configuredToken.isBlank()) {
            return configuredToken.trim();
        }
        return null;
    }

    private static String textOrNull(JsonNode n) {
        if (n == null || n.isMissingNode() || n.isNull()) return null;
        String s = n.asText(null);
        return (s == null || s.isBlank()) ? null : s;
    }

    private static Instant instantOrNull(JsonNode n) {
        if (n == null || n.isMissingNode() || !n.isTextual()) return null;
        try {
            return Instant.parse(n.asText());
        } catch (DateTimeParseException ex) {
            return null;
        }
    }
}
