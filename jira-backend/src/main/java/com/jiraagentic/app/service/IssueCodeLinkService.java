package com.jiraagentic.app.service;

import com.jiraagentic.app.dto.CreateIssueCodeLinkRequest;
import com.jiraagentic.app.dto.IssueCodeLinkDto;
import com.jiraagentic.app.entity.Issue;
import com.jiraagentic.app.entity.IssueCodeLink;
import com.jiraagentic.app.entity.Space;
import com.jiraagentic.app.repository.IssueCodeLinkRepository;
import com.jiraagentic.app.repository.IssueRepository;
import com.jiraagentic.app.repository.UserRepository;
import lombok.RequiredArgsConstructor;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.net.URI;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;
import java.util.Objects;
import java.util.Optional;
import java.util.stream.Collectors;

@Service
@RequiredArgsConstructor
public class IssueCodeLinkService {

    private final IssueCodeLinkRepository codeLinkRepository;
    private final IssueRepository issueRepository;
    private final ActiveSpaceGuard activeSpaceGuard;
    private final UserRepository userRepository;
    private final IssueHistoryService issueHistoryService;
    private final GithubMetadataClient githubMetadataClient;

    public List<IssueCodeLinkDto> findByIssue(Long issueId) {
        Issue issue = issueRepository.findById(issueId)
                .orElseThrow(() -> new RuntimeException("Issue not found: " + issueId));
        activeSpaceGuard.requireActive(issue.getSpace());
        return codeLinkRepository.findByIssueIdOrderByActivityDesc(issueId).stream()
                .map(IssueCodeLinkDto::from)
                .collect(Collectors.toList());
    }

    public List<IssueCodeLinkDto> findBySpace(Long spaceId) {
        activeSpaceGuard.requireActive(spaceId);
        return codeLinkRepository.findByIssueSpaceIdOrderByActivityDesc(spaceId).stream()
                .map(IssueCodeLinkDto::from)
                .collect(Collectors.toList());
    }

    @Transactional
    public IssueCodeLinkDto create(Long issueId, CreateIssueCodeLinkRequest req, Long actorUserId) {
        if (req == null || req.getUrl() == null || req.getUrl().isBlank()) {
            throw new RuntimeException("URL is required");
        }
        String url = req.getUrl().trim();

        Issue issue = issueRepository.findById(issueId)
                .orElseThrow(() -> new RuntimeException("Issue not found: " + issueId));
        activeSpaceGuard.requireActive(issue.getSpace());

        Optional<IssueCodeLink> existing = codeLinkRepository.findByIssueIdAndUrl(issueId, url);
        if (existing.isPresent()) {
            throw new RuntimeException("This URL is already linked to the issue");
        }

        GithubUrlParser.ParsedLink parsed = GithubUrlParser.parse(url);

        IssueCodeLink link = new IssueCodeLink();
        link.setIssue(issue);
        link.setUrl(url);
        link.setProvider(parsed.provider());
        link.setKind(parsed.kind());
        link.setOwner(parsed.owner());
        link.setRepo(parsed.repo());
        link.setRefId(parsed.refId());
        link.setTitle(parsed.fallbackTitle());
        if (actorUserId != null) {
            link.setCreator(userRepository.findById(actorUserId).orElse(null));
        }

        // Best-effort enrichment; never block on network failures.
        String pat = trimToNull(req.getGithubToken());
        if (pat == null) {
            pat = trimToNull(issue.getSpace().getGithubPat());
        }
        githubMetadataClient.fetch(parsed, pat).ifPresent(meta -> {
            if (meta.title() != null) link.setTitle(meta.title());
            if (meta.state() != null) link.setState(meta.state());
            if (meta.authorLogin() != null) link.setAuthorLogin(meta.authorLogin());
            if (meta.lastActivityAt() != null) link.setLastActivityAt(meta.lastActivityAt());
        });

        IssueCodeLink saved = codeLinkRepository.save(link);

        String label = displayLabel(saved);
        issueHistoryService.recordEvent(issue, actorUserId, "code_link_added",
                "linked a " + humanKind(saved.getKind()));
        issueHistoryService.recordFieldChange(issue, actorUserId, "Development",
                "None", label);

        return IssueCodeLinkDto.from(saved);
    }

    /**
     * Used by the repo scanner. If a link with this exact URL already exists
     * on the issue, returns it untouched; otherwise creates a fresh one. This
     * is idempotent so the scan can safely be re-run.
     */
    @Transactional
    public Optional<IssueCodeLinkDto> linkIfMissing(Issue issue, String url, Long actorUserId) {
        if (url == null || url.isBlank()) return Optional.empty();
        Optional<IssueCodeLink> existing = codeLinkRepository.findByIssueIdAndUrl(issue.getId(), url);
        if (existing.isPresent()) return Optional.empty();
        CreateIssueCodeLinkRequest req = new CreateIssueCodeLinkRequest();
        req.setUrl(url);
        return Optional.of(create(issue.getId(), req, actorUserId));
    }

    /**
     * @param githubToken optional PAT for this refresh only (not stored).
     */
    @Transactional
    public RefreshResult refreshSpace(Long spaceId, Long actorUserId, String githubToken) {
        activeSpaceGuard.requireActive(spaceId);
        List<IssueCodeLink> links = codeLinkRepository.findByIssueSpaceIdOrderByActivityDesc(spaceId);
        String pat = resolvePatForSpace(spaceId, githubToken);
        return refreshAll(links, actorUserId, pat);
    }

    @Transactional
    public RefreshResult refreshIssue(Long issueId, Long actorUserId, String githubToken) {
        List<IssueCodeLink> links = codeLinkRepository.findByIssueIdOrderByActivityDesc(issueId);
        Issue issue = issueRepository.findById(issueId)
                .orElseThrow(() -> new RuntimeException("Issue not found: " + issueId));
        activeSpaceGuard.requireActive(issue.getSpace());
        String pat = coalescePat(githubToken, issue.getSpace());
        return refreshAll(links, actorUserId, pat);
    }

    @Transactional
    public IssueCodeLinkDto refreshOne(Long linkId, Long actorUserId, String githubToken) {
        IssueCodeLink link = codeLinkRepository.findById(linkId)
                .orElseThrow(() -> new RuntimeException("Code link not found: " + linkId));
        activeSpaceGuard.requireActive(link.getIssue().getSpace());
        String pat = coalescePat(githubToken, link.getIssue().getSpace());
        applyRefresh(link, actorUserId, pat);
        return IssueCodeLinkDto.from(link);
    }

    private String resolvePatForSpace(Long spaceId, String requestToken) {
        String fromReq = trimToNull(requestToken);
        if (fromReq != null) {
            return fromReq;
        }
        Space space = activeSpaceGuard.requireActive(spaceId);
        return trimToNull(space.getGithubPat());
    }

    private static String coalescePat(String requestToken, Space space) {
        String fromReq = trimToNull(requestToken);
        if (fromReq != null) {
            return fromReq;
        }
        return space == null ? null : trimToNull(space.getGithubPat());
    }

    public record RefreshResult(int checked, int updated) {}

    private RefreshResult refreshAll(List<IssueCodeLink> links, Long actorUserId, String githubToken) {
        int checked = 0;
        int updated = 0;
        for (IssueCodeLink link : links) {
            // Commits are no longer surfaced in the product UI; skip refreshing them.
            if ("commit".equals(link.getKind())) {
                continue;
            }
            checked++;
            if (applyRefresh(link, actorUserId, githubToken)) updated++;
        }
        return new RefreshResult(checked, updated);
    }

    /**
     * Mutates {@code link} with the freshest metadata from GitHub. Returns
     * true if anything changed, and logs a history row when the PR state
     * transitions (e.g. open → merged).
     */
    private boolean applyRefresh(IssueCodeLink link, Long actorUserId, String githubToken) {
        GithubUrlParser.ParsedLink parsed = new GithubUrlParser.ParsedLink(
                link.getProvider(), link.getKind(), link.getOwner(), link.getRepo(), link.getRefId(),
                link.getTitle() == null ? link.getUrl() : link.getTitle()
        );
        var metadata = githubMetadataClient.fetch(parsed, githubToken);
        if (metadata.isEmpty()) return false;
        var meta = metadata.get();
        boolean changed = false;
        String oldState = link.getState();
        if (meta.title() != null && !meta.title().equals(link.getTitle())) {
            link.setTitle(meta.title());
            changed = true;
        }
        if (meta.state() != null && !meta.state().equalsIgnoreCase(oldState)) {
            link.setState(meta.state());
            changed = true;
        }
        if (meta.authorLogin() != null && !meta.authorLogin().equals(link.getAuthorLogin())) {
            link.setAuthorLogin(meta.authorLogin());
            changed = true;
        }
        if (meta.lastActivityAt() != null
                && (link.getLastActivityAt() == null || !meta.lastActivityAt().equals(link.getLastActivityAt()))) {
            link.setLastActivityAt(meta.lastActivityAt());
            changed = true;
        }
        if (changed) {
            codeLinkRepository.save(link);
            if (meta.state() != null && !meta.state().equalsIgnoreCase(oldState)) {
                issueHistoryService.recordFieldChange(link.getIssue(), actorUserId,
                        "Development state", oldState == null ? "unknown" : oldState, meta.state());
            }
        }
        return changed;
    }

    @Transactional
    public void delete(Long issueId, Long linkId, Long actorUserId) {
        IssueCodeLink link = codeLinkRepository.findById(linkId)
                .orElseThrow(() -> new RuntimeException("Code link not found: " + linkId));
        if (!Objects.equals(link.getIssue().getId(), issueId)) {
            throw new RuntimeException("Code link does not belong to issue");
        }
        activeSpaceGuard.requireActive(link.getIssue().getSpace());
        String label = displayLabel(link);
        Issue owner = link.getIssue();
        codeLinkRepository.delete(link);
        issueHistoryService.recordEvent(owner, actorUserId, "code_link_removed",
                "removed a " + humanKind(link.getKind()));
        issueHistoryService.recordFieldChange(owner, actorUserId, "Development", label, "None");
    }

    /**
     * Removes every GitHub Development link in the space for {@code owner/repo}
     * (matches stored owner/repo or parses {@code github.com/owner/repo/...} from the URL).
     */
    @Transactional
    public int removeLinksForGithubRepoInSpace(Long spaceId, String owner, String repo, Long actorUserId) {
        if (owner == null || repo == null || owner.isBlank() || repo.isBlank()) {
            return 0;
        }
        String o = owner.trim();
        String r = repo.trim();
        List<IssueCodeLink> all = codeLinkRepository.findByIssueSpaceIdOrderByActivityDesc(spaceId);
        List<IssueCodeLink> targets = new ArrayList<>();
        for (IssueCodeLink l : all) {
            if (!"github".equalsIgnoreCase(l.getProvider())) {
                continue;
            }
            if (linkMatchesGithubRepo(l, o, r)) {
                targets.add(l);
            }
        }
        int n = 0;
        for (IssueCodeLink l : targets) {
            delete(l.getIssue().getId(), l.getId(), actorUserId);
            n++;
        }
        return n;
    }

    private static boolean linkMatchesGithubRepo(IssueCodeLink l, String owner, String repo) {
        if (l.getOwner() != null && l.getRepo() != null
                && l.getOwner().equalsIgnoreCase(owner)
                && l.getRepo().equalsIgnoreCase(repo)) {
            return true;
        }
        return githubUrlMatchesRepo(l.getUrl(), owner, repo);
    }

    private static boolean githubUrlMatchesRepo(String url, String owner, String repo) {
        if (url == null || url.isBlank()) {
            return false;
        }
        try {
            URI uri = URI.create(url.trim());
            String host = uri.getHost();
            if (host == null) {
                return false;
            }
            String h = host.toLowerCase();
            if (!"github.com".equals(h) && !"www.github.com".equals(h)) {
                return false;
            }
            String path = uri.getPath();
            if (path == null) {
                return false;
            }
            List<String> segs = Arrays.stream(path.split("/")).filter(s -> !s.isEmpty()).toList();
            if (segs.size() < 2) {
                return false;
            }
            return segs.get(0).equalsIgnoreCase(owner) && segs.get(1).equalsIgnoreCase(repo);
        } catch (IllegalArgumentException e) {
            return false;
        }
    }

    private static String displayLabel(IssueCodeLink link) {
        if (link.getTitle() != null && !link.getTitle().isBlank()) return link.getTitle();
        return link.getUrl();
    }

    private static String humanKind(String kind) {
        return switch (kind == null ? "" : kind) {
            case "pull_request" -> "pull request";
            case "commit" -> "commit";
            case "branch" -> "branch";
            case "repo" -> "repository";
            default -> "code link";
        };
    }

    private static String trimToNull(String s) {
        if (s == null) {
            return null;
        }
        String t = s.trim();
        return t.isEmpty() ? null : t;
    }
}
