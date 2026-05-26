package com.jiraagentic.app.service;

import com.jiraagentic.app.dto.CreateIssueLinkRequest;
import com.jiraagentic.app.dto.IssueLinkDto;
import com.jiraagentic.app.entity.Issue;
import com.jiraagentic.app.entity.IssueLink;
import com.jiraagentic.app.repository.IssueLinkRepository;
import com.jiraagentic.app.repository.IssueRepository;
import lombok.RequiredArgsConstructor;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.util.List;
import java.util.Map;
import java.util.Objects;
import java.util.stream.Collectors;

@Service
@RequiredArgsConstructor
public class IssueLinkService {

    private static final Map<String, String> INWARD_TO_TYPE = Map.of(
            "is blocked by", "blocks",
            "is cloned by", "clones",
            "is duplicated by", "duplicates",
            "relates to", "relates to"
    );

    private final IssueLinkRepository issueLinkRepository;
    private final IssueRepository issueRepository;
    private final IssueHistoryService issueHistoryService;
    private final ActiveSpaceGuard activeSpaceGuard;

    public List<IssueLinkDto> findByIssue(Long issueId) {
        Issue viewer = issueRepository.findById(issueId)
                .orElseThrow(() -> new RuntimeException("Issue not found: " + issueId));
        activeSpaceGuard.requireActive(viewer.getSpace());
        return issueLinkRepository.findBySourceIssueIdOrTargetIssueId(issueId, issueId).stream()
                .map(link -> toDto(link, issueId))
                .sorted((a, b) -> b.getCreatedAt().compareTo(a.getCreatedAt()))
                .collect(Collectors.toList());
    }

    @Transactional
    public IssueLinkDto create(Long issueId, CreateIssueLinkRequest req, Long actorUserId) {
        String relation = normalizeRelation(req.getRelation());
        if (relation == null) throw new RuntimeException("Unsupported relation type");
        if (req.getTargetIssueKey() == null || req.getTargetIssueKey().isBlank()) {
            throw new RuntimeException("Target issue key is required");
        }

        Issue current = issueRepository.findById(issueId)
                .orElseThrow(() -> new RuntimeException("Issue not found: " + issueId));
        Issue target = issueRepository.findByIssueKey(req.getTargetIssueKey().trim())
                .orElseThrow(() -> new RuntimeException("Target issue not found: " + req.getTargetIssueKey()));
        activeSpaceGuard.requireActive(current.getSpace());
        activeSpaceGuard.requireActive(target.getSpace());
        if (Objects.equals(current.getId(), target.getId())) {
            throw new RuntimeException("Cannot link issue to itself");
        }

        LinkShape shape = toCanonicalLink(current, target, relation);
        issueLinkRepository.findBySourceIssueIdAndTargetIssueIdAndLinkType(
                shape.source().getId(),
                shape.target().getId(),
                shape.linkType()
        ).ifPresent(existing -> {
            throw new RuntimeException("Link already exists");
        });

        IssueLink link = new IssueLink();
        link.setSourceIssue(shape.source());
        link.setTargetIssue(shape.target());
        link.setLinkType(shape.linkType());
        IssueLink saved = issueLinkRepository.save(link);

        issueHistoryService.recordEvent(current, actorUserId, "link_created",
                "updated the Link");
        issueHistoryService.recordFieldChange(current, actorUserId, "Link",
                "None", relation + " " + target.getIssueKey());
        issueHistoryService.recordEvent(target, actorUserId, "link_created",
                "updated the Link");

        return toDto(saved, issueId);
    }

    @Transactional
    public void delete(Long issueId, Long linkId, Long actorUserId) {
        IssueLink link = issueLinkRepository.findById(linkId)
                .orElseThrow(() -> new RuntimeException("Issue link not found: " + linkId));
        boolean belongsToIssue = Objects.equals(link.getSourceIssue().getId(), issueId)
                || Objects.equals(link.getTargetIssue().getId(), issueId);
        if (!belongsToIssue) throw new RuntimeException("Link does not belong to issue");

        activeSpaceGuard.requireActive(link.getSourceIssue().getSpace());
        activeSpaceGuard.requireActive(link.getTargetIssue().getSpace());

        Issue owner = Objects.equals(link.getSourceIssue().getId(), issueId) ? link.getSourceIssue() : link.getTargetIssue();
        Issue other = Objects.equals(link.getSourceIssue().getId(), issueId) ? link.getTargetIssue() : link.getSourceIssue();
        String relation = relationFromPerspective(link, issueId);
        issueHistoryService.recordEvent(owner, actorUserId, "link_deleted", "updated the Link");
        issueHistoryService.recordFieldChange(owner, actorUserId, "Link",
                relation + " " + other.getIssueKey(), "None");
        issueLinkRepository.delete(link);
    }

    private IssueLinkDto toDto(IssueLink link, Long viewerIssueId) {
        boolean viewerIsSource = Objects.equals(link.getSourceIssue().getId(), viewerIssueId);
        Issue linked = viewerIsSource ? link.getTargetIssue() : link.getSourceIssue();
        IssueLinkDto dto = new IssueLinkDto();
        dto.setId(link.getId());
        dto.setRelation(relationFromPerspective(link, viewerIssueId));
        dto.setLinkedIssueId(linked.getId());
        dto.setLinkedIssueKey(linked.getIssueKey());
        dto.setLinkedIssueTitle(linked.getTitle());
        dto.setCreatedAt(link.getCreatedAt());
        return dto;
    }

    private String relationFromPerspective(IssueLink link, Long viewerIssueId) {
        boolean viewerIsSource = Objects.equals(link.getSourceIssue().getId(), viewerIssueId);
        return switch (link.getLinkType()) {
            case "blocks" -> viewerIsSource ? "blocks" : "is blocked by";
            case "clones" -> viewerIsSource ? "clones" : "is cloned by";
            case "duplicates" -> viewerIsSource ? "duplicates" : "is duplicated by";
            default -> "relates to";
        };
    }

    private String normalizeRelation(String relation) {
        if (relation == null) return null;
        String r = relation.trim().toLowerCase();
        if (INWARD_TO_TYPE.containsKey(r)) return r;
        if (INWARD_TO_TYPE.containsValue(r)) return r;
        return null;
    }

    private LinkShape toCanonicalLink(Issue current, Issue target, String relation) {
        if ("is blocked by".equals(relation)) return new LinkShape(target, current, "blocks");
        if ("is cloned by".equals(relation)) return new LinkShape(target, current, "clones");
        if ("is duplicated by".equals(relation)) return new LinkShape(target, current, "duplicates");
        String type = INWARD_TO_TYPE.getOrDefault(relation, relation);
        return new LinkShape(current, target, type);
    }

    private record LinkShape(Issue source, Issue target, String linkType) {}
}
