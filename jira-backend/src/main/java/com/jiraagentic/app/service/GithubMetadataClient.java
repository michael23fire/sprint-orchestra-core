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
     * @throws RuntimeException when a <em>user-supplied</em> token is rejected (401/403) — callers must not
     *         treat that as “account not found”. A bad server {@code GITHUB_TOKEN} is ignored for public lookups.
     */
    public boolean accountExists(String login, String tokenOverride) {
        if (login == null || login.isBlank()) return false;
        boolean userSuppliedPat = tokenOverride != null && !tokenOverride.isBlank();
        try {
            HttpResponse<String> res = rawGet("/users/" + trimLogin(login), tokenOverride, userSuppliedPat);
            if (res == null) return false;
            int code = res.statusCode();
            if (code == 404) return false;
            if (code == 401 || code == 403) {
                if (userSuppliedPat) {
                    throw new RuntimeException(
                            "GitHub rejected the token (HTTP " + code + "). Check that the PAT is valid and has not expired.");
                }
                // Configured server token may be invalid — retry anonymously for public account lookup.
                res = rawGet("/users/" + trimLogin(login), null, false);
                if (res == null) return false;
                code = res.statusCode();
                if (code == 404 || code == 401 || code == 403 || code / 100 != 2) return false;
                JsonNode n = mapper.readTree(res.body());
                return n != null && n.hasNonNull("id");
            }
            if (code / 100 != 2) return false;
            JsonNode n = mapper.readTree(res.body());
            return n != null && n.hasNonNull("id");
        } catch (RuntimeException ex) {
            throw ex;
        } catch (Exception ex) {
            log.debug("GitHub accountExists failed for {}: {}", login, ex.getMessage());
            return false;
        }
    }

    /**
     * Validates a user-supplied PAT via {@code GET /user}.
     * Call this before import when the user pasted a token so failures are not
     * misreported as “account not found”.
     *
     * @return the GitHub login that owns the token
     */
    public String requireValidTokenActor(String tokenOverride) {
        return requireTokenActorLogin(tokenOverride);
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
     * @param tokenOverride optional <em>user-supplied</em> PAT for this call (e.g. bulk import).
     *                      When blank, only <strong>public</strong> owned repos are listed — the server
     *                      {@code GITHUB_TOKEN} is not used to force a private listing (that previously
     *                      caused “PAT” errors on public-only imports).
     */
    public List<GithubRepoRef> listReposForAccount(String login, String tokenOverride) {
        if (login == null || login.isBlank()) return Collections.emptyList();
        String sanitized = trimLogin(login);
        boolean userSuppliedPat = tokenOverride != null && !tokenOverride.isBlank();
        try {
            // Public import must not depend on a possibly-invalid server GITHUB_TOKEN.
            JsonNode meta = get("/users/" + sanitized, tokenOverride, userSuppliedPat);
            if (meta == null || !meta.has("type")) {
                if (!userSuppliedPat) {
                    meta = get("/users/" + sanitized, null, false);
                }
            }
            if (meta == null || !meta.has("type")) {
                return Collections.emptyList();
            }
            String type = textOrNull(meta.path("type"));
            final String listBase;
            final String listQuery;
            // Only a pasted PAT opts into private listing. Server GITHUB_TOKEN alone must not.
            if ("Organization".equalsIgnoreCase(type)) {
                listBase = "/orgs/" + sanitized + "/repos";
                listQuery = userSuppliedPat
                        ? "type=all&sort=full_name&direction=asc"
                        : "type=public&sort=full_name&direction=asc";
            } else if (userSuppliedPat) {
                // /users/{login}/repos is public-only even with a PAT. Private user repos require
                // /user/repos, and the PAT must belong to that same login.
                String actor = requireTokenActorLogin(tokenOverride);
                if (!actor.equalsIgnoreCase(sanitized)) {
                    throw new RuntimeException(
                            "GitHub token belongs to @" + actor + ", but import account is @" + sanitized
                                    + ". Use a PAT for @" + sanitized + ", or leave the PAT empty to import public repos only.");
                }
                listBase = "/user/repos";
                listQuery = "affiliation=owner&visibility=all&sort=full_name&direction=asc";
            } else {
                listBase = "/users/" + sanitized + "/repos";
                listQuery = "type=owner&sort=full_name&direction=asc";
            }
            List<GithubRepoRef> allVisible = paginateRepoList(listBase, listQuery, tokenOverride, userSuppliedPat);
            List<GithubRepoRef> ownedByAccount = new ArrayList<>();
            for (GithubRepoRef ref : allVisible) {
                if (ref.owner() != null && ref.owner().equalsIgnoreCase(sanitized)) {
                    ownedByAccount.add(ref);
                }
            }
            return ownedByAccount;
        } catch (RuntimeException ex) {
            throw ex;
        } catch (Exception ex) {
            log.warn("GitHub listReposForAccount failed for {}: {}", login, ex.getMessage());
            return Collections.emptyList();
        }
    }

    /**
     * Login for the authenticated API user. Throws when a token is present but {@code /user} fails
     * (invalid PAT, missing profile read, etc.) so callers do not silently fall back to public-only lists.
     */
    private String requireTokenActorLogin(String tokenOverride) {
        if (tokenOverride == null || tokenOverride.isBlank()) {
            throw new RuntimeException("GitHub token is required to list private repositories");
        }
        try {
            HttpResponse<String> res = rawGet("/user", tokenOverride, true);
            if (res == null) {
                throw new RuntimeException("Could not reach GitHub to validate the token");
            }
            int code = res.statusCode();
            if (code == 401 || code == 403) {
                throw new RuntimeException(
                        "GitHub rejected the PAT (HTTP " + code + "). It may be invalid, expired, or missing required scopes (classic: repo; fine-grained: repository read).");
            }
            if (code / 100 != 2) {
                throw new RuntimeException("GitHub could not validate the PAT (HTTP " + code + ")");
            }
            JsonNode n = mapper.readTree(res.body());
            String actor = textOrNull(n.path("login"));
            if (actor == null || actor.isBlank()) {
                throw new RuntimeException("GitHub PAT did not return a user login");
            }
            return actor;
        } catch (RuntimeException ex) {
            throw ex;
        } catch (Exception ex) {
            throw new RuntimeException("Failed to validate GitHub PAT: " + ex.getMessage(), ex);
        }
    }

    private List<GithubRepoRef> paginateRepoList(String pathBase, String query, String tokenOverride, boolean allowConfiguredFallback) {
        List<GithubRepoRef> out = new ArrayList<>();
        final int perPage = 100;
        final int maxPages = 50;
        String q = (query == null || query.isBlank()) ? "sort=full_name&direction=asc" : query;
        for (int page = 1; page <= maxPages; page++) {
            String path = pathBase + "?per_page=" + perPage + "&page=" + page + "&" + q;
            JsonNode arr;
            try {
                arr = get(path, tokenOverride, allowConfiguredFallback);
                // Public listing: if a bad server token caused 401, retry with no auth.
                if (arr == null && !allowConfiguredFallback) {
                    arr = get(path, null, false);
                }
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
     * @param tokenOverride if non-blank after trim, used as Bearer; otherwise the configured {@link #token}
     *                      when {@code allowConfiguredFallback} is true.
     */
    private HttpResponse<String> rawGet(String path, String tokenOverride) throws Exception {
        return rawGet(path, tokenOverride, true);
    }

    private HttpResponse<String> rawGet(String path, String tokenOverride, boolean allowConfiguredFallback) throws Exception {
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
        String bearer = effectiveBearer(tokenOverride, token, allowConfiguredFallback);
        if (bearer != null) {
            b.header("Authorization", "Bearer " + bearer);
        }
        return http.send(b.build(), HttpResponse.BodyHandlers.ofString());
    }

    private JsonNode get(String path) throws Exception {
        return get(path, null, true);
    }

    private JsonNode get(String path, String tokenOverride) throws Exception {
        return get(path, tokenOverride, true);
    }

    private JsonNode get(String path, String tokenOverride, boolean allowConfiguredFallback) throws Exception {
        HttpResponse<String> res = rawGet(path, tokenOverride, allowConfiguredFallback);
        if (res == null) return null;
        if (res.statusCode() / 100 != 2) return null;
        String body = res.body();
        if (body == null || body.isBlank()) return null;
        return mapper.readTree(body);
    }

    /**
     * Prefer trimmed {@code tokenOverride} when non-empty; else configured token when allowed.
     */
    private static String effectiveBearer(String tokenOverride, String configuredToken, boolean allowConfiguredFallback) {
        if (tokenOverride != null) {
            String t = tokenOverride.trim();
            if (!t.isEmpty()) {
                return t;
            }
        }
        if (allowConfiguredFallback && configuredToken != null && !configuredToken.isBlank()) {
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
