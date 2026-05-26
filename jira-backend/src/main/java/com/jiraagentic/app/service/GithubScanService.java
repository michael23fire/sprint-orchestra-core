package com.jiraagentic.app.service;

import com.jiraagentic.app.dto.BulkImportGithubReposRequest;
import com.jiraagentic.app.dto.BulkImportGithubReposResult;
import com.jiraagentic.app.dto.CreateSpaceGithubRepoRequest;
import com.jiraagentic.app.dto.SpaceGithubRepoDto;
import com.jiraagentic.app.entity.Issue;
import com.jiraagentic.app.entity.Space;
import com.jiraagentic.app.entity.SpaceGithubRepo;
import com.jiraagentic.app.repository.IssueRepository;
import com.jiraagentic.app.repository.SpaceGithubRepoRepository;
import com.jiraagentic.app.repository.SpaceRepository;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Service;
import org.springframework.transaction.PlatformTransactionManager;
import org.springframework.transaction.annotation.Transactional;
import org.springframework.transaction.support.TransactionTemplate;

import java.net.URI;
import java.time.Instant;
import java.util.ArrayList;
import java.util.HashSet;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Objects;
import java.util.Optional;
import java.util.Set;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * Scans GitHub repositories connected to a space, looks for issue-key
 * references like "P1-12" in PR titles, PR bodies, and commit messages,
 * and auto-creates code links. Idempotent — re-running the scan never
 * creates duplicate links.
 */
@Service
public class GithubScanService {

    private static final Logger log = LoggerFactory.getLogger(GithubScanService.class);

    private static final int PR_PAGE_SIZE = 50;
    private static final int COMMIT_PAGE_SIZE = 50;

    private final SpaceGithubRepoRepository repoRepository;
    private final SpaceRepository spaceRepository;
    private final ActiveSpaceGuard activeSpaceGuard;
    private final IssueRepository issueRepository;
    private final IssueCodeLinkService codeLinkService;
    private final GithubMetadataClient githubMetadataClient;
    private final TransactionTemplate transactionTemplate;

    public GithubScanService(
            SpaceGithubRepoRepository repoRepository,
            SpaceRepository spaceRepository,
            ActiveSpaceGuard activeSpaceGuard,
            IssueRepository issueRepository,
            IssueCodeLinkService codeLinkService,
            GithubMetadataClient githubMetadataClient,
            PlatformTransactionManager transactionManager) {
        this.repoRepository = repoRepository;
        this.spaceRepository = spaceRepository;
        this.activeSpaceGuard = activeSpaceGuard;
        this.issueRepository = issueRepository;
        this.codeLinkService = codeLinkService;
        this.githubMetadataClient = githubMetadataClient;
        this.transactionTemplate = new TransactionTemplate(transactionManager);
    }

    // ── Repo connection CRUD ──────────────────────────────────────────────

    public List<SpaceGithubRepoDto> listRepos(Long spaceId) {
        activeSpaceGuard.requireActive(spaceId);
        return repoRepository.findBySpaceIdOrderByCreatedAtAsc(spaceId).stream()
                .map(SpaceGithubRepoDto::from)
                .toList();
    }

    @Transactional
    public SpaceGithubRepoDto addRepo(Long spaceId, CreateSpaceGithubRepoRequest req) {
        if (req == null || req.getTarget() == null || req.getTarget().isBlank()) {
            throw new RuntimeException("owner/repo or GitHub URL is required");
        }
        OwnerRepo or = parseOwnerRepo(req.getTarget().trim());
        if (or == null) throw new RuntimeException("Could not parse owner/repo from input");

        Space space = activeSpaceGuard.requireActive(spaceId);

        repoRepository.findBySpaceIdAndOwnerAndRepo(spaceId, or.owner(), or.repo())
                .ifPresent(existing -> { throw new RuntimeException("This repo is already connected"); });

        // Best-effort existence check. We don't hard fail on a 404 since the
        // user may have a valid private repo but no token configured.
        if (!githubMetadataClient.repoExists(or.owner(), or.repo())) {
            log.info("GitHub repo {}/{} not reachable via public API; adding anyway", or.owner(), or.repo());
        }

        SpaceGithubRepo repo = new SpaceGithubRepo();
        repo.setSpace(space);
        repo.setOwner(or.owner());
        repo.setRepo(or.repo());
        return SpaceGithubRepoDto.from(repoRepository.save(repo));
    }

    /**
     * GitHub HTTP runs <em>outside</em> a DB transaction so we do not hold a
     * connection open across slow / paginated API calls (which previously led
     * to pool timeouts and opaque {@code ServletException} → “Unexpected error”).
     */
    public BulkImportGithubReposResult bulkAddReposFromAccount(Long spaceId, BulkImportGithubReposRequest req) {
        if (req == null || req.getAccount() == null || req.getAccount().isBlank()) {
            throw new RuntimeException("GitHub account (user or organization) is required");
        }
        String account = GithubMetadataClient.normalizeGithubAccountInput(req.getAccount());
        if (account.isBlank()) {
            throw new RuntimeException("GitHub account (user or organization) is required (use a login or a profile URL like https://github.com/acme)");
        }

        if (!spaceRepository.existsByIdAndDeletedAtIsNull(spaceId)) {
            throw new RuntimeException("Space not found: " + spaceId);
        }

        String importToken = req.getGithubToken();

        if (!githubMetadataClient.accountExists(account, importToken)) {
            throw new RuntimeException("GitHub user or organization not found: " + account);
        }

        List<GithubMetadataClient.GithubRepoRef> discovered = githubMetadataClient.listReposForAccount(account, importToken);
        final String patToStore = trimToNull(importToken);

        return transactionTemplate.execute(status -> {
            Space space = activeSpaceGuard.requireActive(spaceId);
            if (patToStore != null) {
                space.setGithubPat(patToStore);
                spaceRepository.save(space);
            }
            if (discovered.isEmpty()) {
                return new BulkImportGithubReposResult(0, 0, 0);
            }
            int added = 0;
            int skipped = 0;
            for (GithubMetadataClient.GithubRepoRef ref : discovered) {
                if (repoRepository.findBySpaceIdAndOwnerAndRepo(spaceId, ref.owner(), ref.name()).isPresent()) {
                    skipped++;
                    continue;
                }
                SpaceGithubRepo row = new SpaceGithubRepo();
                row.setSpace(space);
                row.setOwner(ref.owner());
                row.setRepo(ref.name());
                repoRepository.save(row);
                added++;
            }
            return new BulkImportGithubReposResult(discovered.size(), added, skipped);
        });
    }

    @Transactional
    public void removeRepo(Long spaceId, Long repoId, Long actorUserId) {
        activeSpaceGuard.requireActive(spaceId);
        SpaceGithubRepo repo = repoRepository.findById(repoId)
                .orElseThrow(() -> new RuntimeException("Repo connection not found"));
        if (!Objects.equals(repo.getSpace().getId(), spaceId)) {
            throw new RuntimeException("Repo connection does not belong to this space");
        }
        codeLinkService.removeLinksForGithubRepoInSpace(spaceId, repo.getOwner(), repo.getRepo(), actorUserId);
        repoRepository.delete(repo);
    }

    // ── The actual scan ───────────────────────────────────────────────────

    /**
     * Per-repo stats so the UI can show exactly which repo contributed
     * what (and which one failed). {@code warning} is non-null when that
     * repo's scan hit an error.
     */
    public record RepoScanStats(
            Long repoId,
            String owner,
            String repo,
            int prsInspected,
            int commitsInspected,
            int linksCreated,
            String warning
    ) {}

    public record ScanResult(
            int reposScanned,
            int reposRemoved,
            int prsInspected,
            int commitsInspected,
            int linksCreated,
            List<RepoScanStats> perRepo,
            List<String> warnings
    ) {}

    /**
     * @param githubToken optional PAT for this request only (overrides space-stored and server token for GitHub calls).
     */
    @Transactional
    public ScanResult scanSpace(Long spaceId, Long actorUserId, String githubToken) {
        Space space = activeSpaceGuard.requireActive(spaceId);
        String tokenForGithub = trimToNull(githubToken);
        if (tokenForGithub == null) {
            tokenForGithub = trimToNull(space.getGithubPat());
        }
        String spaceKey = space.getKey();
        if (spaceKey == null || spaceKey.isBlank()) {
            throw new RuntimeException("Space has no key; cannot scan for issue references");
        }
        Pattern issueKeyPattern = Pattern.compile("\\b" + Pattern.quote(spaceKey) + "-(\\d+)\\b",
                Pattern.CASE_INSENSITIVE);

        List<SpaceGithubRepo> repos = repoRepository.findBySpaceIdOrderByCreatedAtAsc(spaceId);
        int prsInspected = 0;
        int commitsInspected = 0;
        int linksCreated = 0;
        int reposRemoved = 0;
        int reposScanned = 0;
        List<String> warnings = new ArrayList<>();
        List<RepoScanStats> perRepo = new ArrayList<>();

        // Do not auto-disconnect on GET /repos 404: GitHub returns 404 for private repos the token
        // cannot see as well as for deleted repos, so removal would drop valid connections.

        for (SpaceGithubRepo repo : repos) {
            reposScanned++;
            int[] prForThisRepo = {0};
            int[] commitsForThisRepo = {0};
            int[] linksForThisRepo = {0};
            // Avoid double-counting a commit that is both on a PR head-branch
            // AND on the default branch (which happens after PR merge). Keyed
            // on the commit's html_url because SHAs are unique per repo.
            Set<String> seenCommitUrls = new HashSet<>();
            String repoWarning = null;
            try {
                // ── Step 1: Pull requests (title + body) ────────────────
                List<GithubMetadataClient.PullSummary> pulls =
                        githubMetadataClient.listRecentPulls(repo.getOwner(), repo.getRepo(), PR_PAGE_SIZE, tokenForGithub);
                prForThisRepo[0] = pulls.size();
                for (var pr : pulls) {
                    String blob = (pr.title() == null ? "" : pr.title()) + "\n" + (pr.body() == null ? "" : pr.body());
                    Set<String> keys = extractKeys(blob, issueKeyPattern, spaceKey);
                    for (String key : keys) {
                        Optional<Issue> issue = issueRepository.findByIssueKey(key);
                        if (issue.isEmpty()) continue;
                        if (pr.htmlUrl() == null) continue;
                        boolean created = codeLinkService
                                .linkIfMissing(issue.get(), pr.htmlUrl(), actorUserId)
                                .isPresent();
                        if (created) linksForThisRepo[0]++;
                    }

                    // ── Step 2: Commits living on this PR's branch ──────
                    //
                    // Key reason for calling /pulls/{n}/commits in addition to
                    // /repos/{o}/{r}/commits: GitHub's default-branch listing
                    // *does not* surface commits that only exist on a PR head
                    // branch (i.e. unmerged PRs). That single fact means the
                    // scanner was effectively blind until merge — which is the
                    // opposite of what a real team wants. We want to see the
                    // ticket ↔ commit relationship the moment a dev pushes.
                    List<GithubMetadataClient.CommitSummary> prCommits =
                            githubMetadataClient.listPullCommits(repo.getOwner(), repo.getRepo(), pr.number(), COMMIT_PAGE_SIZE, tokenForGithub);
                    for (var c : prCommits) {
                        if (c.htmlUrl() == null) continue;
                        if (!seenCommitUrls.add(c.htmlUrl())) continue;
                        commitsForThisRepo[0]++;
                        scanCommit(c, issueKeyPattern, spaceKey, actorUserId, linksForThisRepo);
                    }
                }

                // ── Step 3: Default-branch commits ─────────────────────
                //
                // Still needed for commits that went in without a PR
                // (e.g. admin pushes to main directly). We dedup against
                // PR commits already seen in Step 2, so merging a PR
                // doesn't inflate the commit count on the next scan.
                List<GithubMetadataClient.CommitSummary> commits =
                        githubMetadataClient.listRecentCommits(repo.getOwner(), repo.getRepo(), COMMIT_PAGE_SIZE, tokenForGithub);
                for (var c : commits) {
                    if (c.htmlUrl() == null) continue;
                    if (!seenCommitUrls.add(c.htmlUrl())) continue;
                    commitsForThisRepo[0]++;
                    scanCommit(c, issueKeyPattern, spaceKey, actorUserId, linksForThisRepo);
                }

                repo.setLastScannedAt(Instant.now());
                repoRepository.save(repo);
            } catch (Exception ex) {
                log.warn("Failed scanning {}/{}: {}", repo.getOwner(), repo.getRepo(), ex.getMessage());
                repoWarning = ex.getMessage() == null ? "unknown error" : ex.getMessage();
                warnings.add(repo.getOwner() + "/" + repo.getRepo() + ": " + repoWarning);
            }
            prsInspected += prForThisRepo[0];
            commitsInspected += commitsForThisRepo[0];
            linksCreated += linksForThisRepo[0];
            perRepo.add(new RepoScanStats(
                    repo.getId(), repo.getOwner(), repo.getRepo(),
                    prForThisRepo[0], commitsForThisRepo[0], linksForThisRepo[0], repoWarning));
        }
        return new ScanResult(reposScanned, reposRemoved, prsInspected, commitsInspected, linksCreated, perRepo, warnings);
    }

    /**
     * Runs key extraction + link-if-missing on a single commit and bumps
     * the per-repo link counter. Extracted to keep {@link #scanSpace} flat.
     */
    private void scanCommit(GithubMetadataClient.CommitSummary c,
                             Pattern issueKeyPattern,
                             String spaceKey,
                             Long actorUserId,
                             int[] linkCounter) {
        String msg = c.message() == null ? "" : c.message();
        Set<String> keys = extractKeys(msg, issueKeyPattern, spaceKey);
        for (String key : keys) {
            Optional<Issue> issue = issueRepository.findByIssueKey(key);
            if (issue.isEmpty()) continue;
            boolean created = codeLinkService
                    .linkIfMissing(issue.get(), c.htmlUrl(), actorUserId)
                    .isPresent();
            if (created) linkCounter[0]++;
        }
    }

    private static String trimToNull(String s) {
        if (s == null) {
            return null;
        }
        String t = s.trim();
        return t.isEmpty() ? null : t;
    }

    private static Set<String> extractKeys(String text, Pattern pattern, String spaceKey) {
        if (text == null || text.isBlank()) return Set.of();
        Matcher m = pattern.matcher(text);
        Set<String> out = new LinkedHashSet<>();
        while (m.find()) {
            // Always normalise to the canonical upper-case space key so we can
            // match the issue row (keys in the DB are stored in canonical form).
            out.add(spaceKey + "-" + m.group(1));
        }
        return out;
    }

    private record OwnerRepo(String owner, String repo) {}

    private static OwnerRepo parseOwnerRepo(String raw) {
        String s = raw;
        // Accept full GitHub URL, trim trailing ".git" and query/hash.
        if (s.startsWith("http://") || s.startsWith("https://")) {
            try {
                URI u = new URI(s);
                s = u.getPath();
                if (s != null && s.startsWith("/")) s = s.substring(1);
            } catch (Exception ignore) {
                return null;
            }
        }
        if (s == null) return null;
        s = s.replace(".git", "");
        int hash = s.indexOf('#');
        if (hash >= 0) s = s.substring(0, hash);
        int q = s.indexOf('?');
        if (q >= 0) s = s.substring(0, q);
        String[] parts = s.split("/");
        // Filter empties from leading/trailing slashes.
        var nonEmpty = new ArrayList<String>();
        for (String p : parts) if (!p.isBlank()) nonEmpty.add(p);
        if (nonEmpty.size() < 2) return null;
        String owner = nonEmpty.get(0);
        String repo = nonEmpty.get(1);
        if (owner.isBlank() || repo.isBlank()) return null;
        return new OwnerRepo(owner, repo);
    }
}
