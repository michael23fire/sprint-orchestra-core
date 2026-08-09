package com.jiraagentic.app.service;

import com.jiraagentic.app.dto.CommentDto;
import com.jiraagentic.app.dto.CreateIssueRequest;
import com.jiraagentic.app.dto.IssueDto;
import com.jiraagentic.app.dto.UpdateIssueRequest;
import com.jiraagentic.app.entity.Issue;
import com.jiraagentic.app.entity.Label;
import com.jiraagentic.app.entity.Space;
import com.jiraagentic.app.entity.Sprint;
import com.jiraagentic.app.event.IssueContentChangedEvent;
import com.jiraagentic.app.event.IssueDeletedEvent;
import com.jiraagentic.app.repository.*;
import lombok.RequiredArgsConstructor;
import org.springframework.context.ApplicationEventPublisher;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.time.Instant;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Objects;
import java.util.stream.Collectors;

@Service
@RequiredArgsConstructor
public class IssueService {

    private final IssueRepository issueRepository;
    private final SpaceRepository spaceRepository;
    private final SprintRepository sprintRepository;
    private final UserRepository userRepository;
    private final CommentRepository commentRepository;
    private final IssueHistoryService issueHistoryService;
    private final IssueLinkService issueLinkService;
    private final IssueLinkRepository issueLinkRepository;
    private final IssueAttachmentService issueAttachmentService;
    private final IssueCodeLinkService issueCodeLinkService;
    private final IssueCodeLinkRepository issueCodeLinkRepository;
    private final IssueHistoryRepository issueHistoryRepository;
    private final ActiveSpaceGuard activeSpaceGuard;
    private final LabelService labelService;
    private final SprintHistoryService sprintHistoryService;
    private final ApplicationEventPublisher applicationEventPublisher;

    public List<IssueDto> findBySpace(Long spaceId) {
        activeSpaceGuard.requireActive(spaceId);
        List<Issue> issues = issueRepository.findBySpaceIdOrderByIssueOrderAsc(spaceId);
        if (issues.isEmpty()) {
            return List.of();
        }
        List<Long> parentIds = issues.stream().map(Issue::getId).collect(Collectors.toList());
        Map<Long, List<String>> childKeysByParent = childKeysByParentId(parentIds);
        return issues.stream()
                .map(i -> toListDto(i, childKeysByParent.getOrDefault(i.getId(), List.of())))
                .collect(Collectors.toList());
    }

    public IssueDto findByKey(Long spaceId, String issueKey) {
        Issue issue = issueRepository.findByIssueKey(issueKey)
                .orElseThrow(() -> new RuntimeException("Issue not found: " + issueKey));
        assertIssueInSpace(issue, spaceId);
        return toDto(issue);
    }

    @Transactional
    public IssueDto create(Long spaceId, CreateIssueRequest req, Long creatorUserId) {
        Space space = activeSpaceGuard.requireActive(spaceId);

        int num = space.nextIssueNumber();
        String issueKey = space.getKey() + "-" + num;
        spaceRepository.save(space);

        Issue issue = new Issue();
        issue.setIssueKey(issueKey);
        issue.setSpace(space);
        issue.setTitle(req.getTitle());
        issue.setDescription(req.getDescription());
        issue.setIssueType(req.getIssueType() != null ? req.getIssueType() : "task");
        if (req.getParentId() != null) {
            Issue parent = resolveParent(spaceId, req.getParentId());
            issue.setParent(parent);
            if (req.getStatus() != null) {
                issue.setStatus(req.getStatus());
            } else if (parent != null) {
                issue.setStatus(parent.getStatus());
            } else {
                issue.setStatus("planned");
            }
        } else {
            issue.setStatus(req.getStatus() != null ? req.getStatus() : "planned");
        }
        issue.setPriority(req.getPriority());
        issue.setStoryPoints(req.getStoryPoints());
        issue.setStartDate(req.getStartDate());
        issue.setDueDate(req.getDueDate());
        issue.setLabels(labelService.resolveLabels(spaceId, req.getLabels()));

        Long reporterId = req.getReporterId() != null ? req.getReporterId() : creatorUserId;
        if (reporterId != null) {
            issue.setReporter(userRepository.findById(reporterId).orElse(null));
        }

        if (req.getAssigneeId() != null) {
            issue.setAssignee(userRepository.findById(req.getAssigneeId()).orElse(null));
        }
        if (req.getSprintId() != null && !isEpicType(issue.getIssueType())) {
            issue.setSprint(resolveSprint(spaceId, req.getSprintId()));
        }

        applyEpicInvariants(issue);
        assertValidSubtaskParent(issue);
        if (issue.getSprint() != null) SprintHistoryService.assertEstimatedForSprint(issue);

        Issue saved = issueRepository.save(issue);
        sprintHistoryService.trackAdded(saved.getSprint(), saved);
        issueHistoryService.recordEvent(saved, creatorUserId, "issue_created", "Issue created");
        applicationEventPublisher.publishEvent(new IssueContentChangedEvent(
                saved.getId(), saved.getIssueKey(), saved.getSpace().getId(),
                saved.getTitle(), saved.getDescription(),
                saved.getIssueType(), saved.getStatus(), saved.getPriority(),
                saved.getSprint() != null ? saved.getSprint().getId() : null,
                saved.getSprint() != null ? saved.getSprint().getName() : null,
                saved.getParent() != null ? saved.getParent().getId() : null,
                saved.getParent() != null ? saved.getParent().getIssueKey() : null,
                saved.getParent() != null ? saved.getParent().getTitle() : null,
                saved.getAssignee() != null ? saved.getAssignee().getId() : null,
                saved.getAssignee() != null ? saved.getAssignee().getName() : null,
                saved.getStoryPoints(),
                saved.getCreatedAt(), saved.getUpdatedAt(),
                Instant.now()));
        return toDto(saved);
    }

    @Transactional
    public IssueDto update(Long spaceId, String issueKey, UpdateIssueRequest req, Long actorUserId) {
        Issue issue = issueRepository.findByIssueKey(issueKey)
                .orElseThrow(() -> new RuntimeException("Issue not found: " + issueKey));
        assertIssueInSpace(issue, spaceId);

        String oldTitle = issue.getTitle();
        String oldDescription = issue.getDescription();
        String oldIssueType = issue.getIssueType();
        String oldStatus = issue.getStatus();
        String oldPriority = issue.getPriority();
        String oldAssignee = issue.getAssignee() != null ? issue.getAssignee().getName() : null;
        String oldReporter = issue.getReporter() != null ? issue.getReporter().getName() : null;
        Sprint oldSprintEntity = issue.getSprint();
        String oldSprint = issue.getSprint() != null ? issue.getSprint().getName() : null;
        String oldParent = issue.getParent() != null ? issue.getParent().getIssueKey() : null;
        Integer oldStoryPoints = issue.getStoryPoints();
        String oldStartDate = issue.getStartDate() != null ? issue.getStartDate().toString() : null;
        String oldDueDate = issue.getDueDate() != null ? issue.getDueDate().toString() : null;
        String oldLabels = labelNames(issue);
        Integer oldIssueOrder = issue.getIssueOrder();

        if (req.getTitle() != null) issue.setTitle(req.getTitle());
        if (req.getDescription() != null) issue.setDescription(req.getDescription());
        if (req.getIssueType() != null) {
            boolean wasSubtask = isSubtaskType(oldIssueType);
            boolean willBeSubtask = isSubtaskType(req.getIssueType());
            if (!wasSubtask && willBeSubtask) {
                throw new IllegalArgumentException(
                        "An existing issue cannot be converted to a subtask; create it from a parent issue.");
            }
            if (wasSubtask && !willBeSubtask && issue.getParent() != null) {
                throw new IllegalArgumentException(
                        "Subtask issue type cannot be changed once the issue is a subtask.");
            }
            issue.setIssueType(req.getIssueType());
        }
        if (req.getStatus() != null) issue.setStatus(req.getStatus());
        if (req.getPriority() != null) issue.setPriority(req.getPriority());
        if (req.getStoryPoints() != null) issue.setStoryPoints(req.getStoryPoints());
        if (req.getStartDate() != null) issue.setStartDate(req.getStartDate());
        if (req.getDueDate() != null) issue.setDueDate(req.getDueDate());
        if (req.getIssueOrder() != null) issue.setIssueOrder(req.getIssueOrder());
        if (req.getLabels() != null) {
            issue.getLabels().clear();
            issue.getLabels().addAll(labelService.resolveLabels(issue.getSpace().getId(), req.getLabels()));
        }

        if (Boolean.TRUE.equals(req.getClearAssignee())) {
            issue.setAssignee(null);
        } else if (req.getAssigneeId() != null) {
            issue.setAssignee(userRepository.findById(req.getAssigneeId()).orElse(null));
        }
        if (Boolean.TRUE.equals(req.getClearReporter())) {
            issue.setReporter(null);
        } else if (req.getReporterId() != null) {
            issue.setReporter(userRepository.findById(req.getReporterId()).orElse(null));
        }
        if (Boolean.TRUE.equals(req.getClearSprint())) {
            issue.setSprint(null);
        } else if (req.getSprintId() != null) {
            issue.setSprint(resolveSprint(issue.getSpace().getId(), req.getSprintId()));
        }
        if (Boolean.TRUE.equals(req.getClearParent())) {
            issue.setParent(null);
        } else if (req.getParentId() != null) {
            issue.setParent(resolveParent(issue.getSpace().getId(), req.getParentId()));
        }

        applyEpicInvariants(issue);
        assertValidSubtaskParent(issue);
        boolean newlyAssignedToSprint = !sameSprint(oldSprintEntity, issue.getSprint()) && issue.getSprint() != null;
        boolean becameEstimatableInSprint = req.getIssueType() != null && issue.getSprint() != null;
        if (newlyAssignedToSprint || becameEstimatableInSprint) {
            SprintHistoryService.assertEstimatedForSprint(issue);
        }

        issueHistoryService.recordFieldChange(issue, actorUserId, "title", oldTitle, issue.getTitle());
        issueHistoryService.recordFieldChange(issue, actorUserId, "description", oldDescription, issue.getDescription());
        issueHistoryService.recordFieldChange(issue, actorUserId, "issueType", oldIssueType, issue.getIssueType());
        issueHistoryService.recordFieldChange(issue, actorUserId, "status", oldStatus, issue.getStatus());
        issueHistoryService.recordFieldChange(issue, actorUserId, "priority", oldPriority, issue.getPriority());
        issueHistoryService.recordFieldChange(issue, actorUserId, "assignee", oldAssignee, issue.getAssignee() != null ? issue.getAssignee().getName() : null);
        issueHistoryService.recordFieldChange(issue, actorUserId, "reporter", oldReporter, issue.getReporter() != null ? issue.getReporter().getName() : null);
        issueHistoryService.recordFieldChange(issue, actorUserId, "sprint", oldSprint, issue.getSprint() != null ? issue.getSprint().getName() : null);
        issueHistoryService.recordFieldChange(issue, actorUserId, "parent", oldParent, issue.getParent() != null ? issue.getParent().getIssueKey() : null);
        issueHistoryService.recordFieldChange(issue, actorUserId, "storyPoints", oldStoryPoints, issue.getStoryPoints());
        issueHistoryService.recordFieldChange(issue, actorUserId, "startDate", oldStartDate, issue.getStartDate() != null ? issue.getStartDate().toString() : null);
        issueHistoryService.recordFieldChange(issue, actorUserId, "dueDate", oldDueDate, issue.getDueDate() != null ? issue.getDueDate().toString() : null);
        issueHistoryService.recordFieldChange(issue, actorUserId, "labels", oldLabels, labelNames(issue));
        issueHistoryService.recordFieldChange(issue, actorUserId, "issueOrder", oldIssueOrder, issue.getIssueOrder());

        Issue saved = issueRepository.save(issue);
        if (!sameSprint(oldSprintEntity, saved.getSprint())) {
            sprintHistoryService.trackRemoved(oldSprintEntity, saved);
            sprintHistoryService.trackAdded(saved.getSprint(), saved);
        }
        if (req.getDescription() != null && !Objects.equals(oldDescription, saved.getDescription())) {
            issueAttachmentService.reconcileEmbeddedAttachments(saved.getId(), actorUserId);
        }
        // Publish whenever anything IssueIngestionMessage carries actually changed — not just the
        // embeddable text. **Found live, not hypothetically**: this condition used to only check
        // title/description/parent, on the reasoning that "plain status/priority/sprint edits don't
        // affect the vector" — true for the *embedding*, but the same event also carries status,
        // priority, and sprint_id/sprint_name as structured metadata that vectorization-service's
        // `handle_issue` unconditionally upserts (independent of whether it re-embeds — see that
        // method's own comment on why metadata sync must not be gated on "is there text to embed").
        // With the old condition, every `move_out_of_sprint`/`change_priority` recovery action (and
        // any plain status drag-and-drop) silently never reached vectorization-service's index at
        // all: jira-backend's own database was correct, but every downstream read (risk-signal
        // detection, `/issues/query` sprint filters, evidence retrieval) kept seeing the pre-change
        // state indefinitely, indistinguishable from a real regression. A manual backfill script was
        // the only way to catch it up — this fix makes that unnecessary going forward.
        String newParentKey = saved.getParent() != null ? saved.getParent().getIssueKey() : null;
        String newAssignee = saved.getAssignee() != null ? saved.getAssignee().getName() : null;
        if (!Objects.equals(oldTitle, saved.getTitle()) || !Objects.equals(oldDescription, saved.getDescription())
                || !Objects.equals(oldParent, newParentKey) || !Objects.equals(oldStatus, saved.getStatus())
                || !Objects.equals(oldPriority, saved.getPriority())
                || !sameSprint(oldSprintEntity, saved.getSprint())
                || !Objects.equals(oldAssignee, newAssignee)
                || !Objects.equals(oldStoryPoints, saved.getStoryPoints())) {
            applicationEventPublisher.publishEvent(new IssueContentChangedEvent(
                    saved.getId(), saved.getIssueKey(), saved.getSpace().getId(),
                    saved.getTitle(), saved.getDescription(),
                    saved.getIssueType(), saved.getStatus(), saved.getPriority(),
                    saved.getSprint() != null ? saved.getSprint().getId() : null,
                    saved.getSprint() != null ? saved.getSprint().getName() : null,
                    saved.getParent() != null ? saved.getParent().getId() : null,
                    newParentKey,
                    saved.getParent() != null ? saved.getParent().getTitle() : null,
                    saved.getAssignee() != null ? saved.getAssignee().getId() : null,
                    saved.getAssignee() != null ? saved.getAssignee().getName() : null,
                    saved.getStoryPoints(),
                    saved.getCreatedAt(), saved.getUpdatedAt(),
                    Instant.now()));
        }
        boolean sprintChanged = req.getSprintId() != null || Boolean.TRUE.equals(req.getClearSprint());
        if (sprintChanged) {
            cascadeSprintToChildren(saved.getId(), saved.getSprint());
        }
        return toDto(issueRepository.findById(saved.getId()).orElse(saved));
    }

    private void cascadeSprintToChildren(Long parentId, Sprint sprintOrNull) {
        for (Issue child : issueRepository.findByParentId(parentId)) {
            if (!isSubtaskType(child.getIssueType())) {
                continue;
            }
            Sprint oldSprint = child.getSprint();
            child.setSprint(sprintOrNull);
            Issue savedChild = issueRepository.save(child);
            if (!sameSprint(oldSprint, sprintOrNull)) {
                sprintHistoryService.trackRemoved(oldSprint, savedChild);
                sprintHistoryService.trackAdded(sprintOrNull, savedChild);
                // Same fix as update()'s own publish condition, applied here: a cascaded sprint move
                // changes savedChild's sprint_id in jira-backend's database but, before this, never
                // told vectorization-service — the cascade path saves the child directly and never
                // went through update()'s (now-fixed) publish condition at all.
                String childParentKey = savedChild.getParent() != null ? savedChild.getParent().getIssueKey() : null;
                applicationEventPublisher.publishEvent(new IssueContentChangedEvent(
                        savedChild.getId(), savedChild.getIssueKey(), savedChild.getSpace().getId(),
                        savedChild.getTitle(), savedChild.getDescription(),
                        savedChild.getIssueType(), savedChild.getStatus(), savedChild.getPriority(),
                        savedChild.getSprint() != null ? savedChild.getSprint().getId() : null,
                        savedChild.getSprint() != null ? savedChild.getSprint().getName() : null,
                        savedChild.getParent() != null ? savedChild.getParent().getId() : null,
                        childParentKey,
                        savedChild.getParent() != null ? savedChild.getParent().getTitle() : null,
                        savedChild.getAssignee() != null ? savedChild.getAssignee().getId() : null,
                        savedChild.getAssignee() != null ? savedChild.getAssignee().getName() : null,
                        savedChild.getStoryPoints(),
                        savedChild.getCreatedAt(), savedChild.getUpdatedAt(),
                        Instant.now()));
            }
            cascadeSprintToChildren(savedChild.getId(), sprintOrNull);
        }
    }

    private static boolean isEpicType(String issueType) {
        return issueType != null && "epic".equalsIgnoreCase(issueType);
    }

    private static boolean isSubtaskType(String issueType) {
        return issueType != null && "subtask".equalsIgnoreCase(issueType);
    }

    private Sprint resolveSprint(Long spaceId, Long sprintId) {
        Sprint sprint = sprintRepository.findById(sprintId)
                .orElseThrow(() -> new IllegalArgumentException("Sprint not found: " + sprintId));
        if (!sprint.getSpace().getId().equals(spaceId)) {
            throw new IllegalArgumentException("Sprint does not belong to space: " + spaceId);
        }
        if ("completed".equals(sprint.getStatus())) {
            throw new IllegalArgumentException("Issues cannot be added to a completed sprint");
        }
        return sprint;
    }

    private Issue resolveParent(Long spaceId, Long parentId) {
        Issue parent = issueRepository.findById(parentId)
                .orElseThrow(() -> new IllegalArgumentException("Parent issue not found: " + parentId));
        if (!parent.getSpace().getId().equals(spaceId)) {
            throw new IllegalArgumentException("Parent issue does not belong to space: " + spaceId);
        }
        return parent;
    }

    private static boolean sameSprint(Sprint left, Sprint right) {
        if (left == null || right == null) return left == right;
        return Objects.equals(left.getId(), right.getId());
    }

    private static String labelNames(Issue issue) {
        if (issue.getLabels() == null || issue.getLabels().isEmpty()) return "";
        return issue.getLabels().stream().map(Label::getName).collect(Collectors.joining(", "));
    }

    /**
     * Jira-style: epics are backlog-level containers — no parent, never on a sprint.
     */
    private void applyEpicInvariants(Issue issue) {
        if (!isEpicType(issue.getIssueType())) {
            return;
        }
        issue.setParent(null);
        issue.setSprint(null);
        issue.setStoryPoints(null);
    }

    private void assertValidSubtaskParent(Issue issue) {
        if (!isSubtaskType(issue.getIssueType())) {
            return;
        }
        // Parent work owns the estimate; pointing both parent and subtask would
        // double-count sprint commitment and velocity.
        issue.setStoryPoints(null);
        if (issue.getParent() == null) {
            throw new IllegalArgumentException(
                    "A subtask must have a parent issue; use Create child issue on the parent.");
        }
        if (isEpicType(issue.getParent().getIssueType())) {
            throw new IllegalArgumentException(
                    "Subtasks cannot be attached to an epic; attach them to a story or task under the epic.");
        }
    }

    @Transactional
    public void delete(Long spaceId, String issueKey, Long actorUserId) {
        Issue issue = issueRepository.findByIssueKey(issueKey)
                .orElseThrow(() -> new RuntimeException("Issue not found: " + issueKey));
        assertIssueInSpace(issue, spaceId);
        List<Long> postOrder = new ArrayList<>();
        collectSubtreePostOrder(issue.getId(), postOrder);
        for (Long issueId : postOrder) {
            purgeIssueDependencies(issueId);
            Issue toDelete = issueRepository.findById(issueId)
                    .orElseThrow(() -> new RuntimeException("Issue not found: id=" + issueId));
            // Emit before delete so the key/space are still loadable; the AFTER_COMMIT listener
            // fires only once the whole transaction commits. One event purges the issue's issue,
            // comment, and attachment vectors in the separate vector store.
            applicationEventPublisher.publishEvent(new IssueDeletedEvent(
                    toDelete.getId(), toDelete.getIssueKey(), toDelete.getSpace().getId(), Instant.now()));
            issueRepository.delete(toDelete);
        }
    }

    /** Depth-first post-order: children (any issue type) before parent. */
    private void collectSubtreePostOrder(Long parentId, List<Long> out) {
        for (Issue child : issueRepository.findByParentId(parentId)) {
            collectSubtreePostOrder(child.getId(), out);
        }
        out.add(parentId);
    }

    private void purgeIssueDependencies(Long issueId) {
        issueLinkRepository.deleteAllInvolvingIssue(issueId);
        commentRepository.deleteByIssue_Id(issueId);
        issueHistoryRepository.deleteByIssue_Id(issueId);
        issueCodeLinkRepository.deleteByIssue_Id(issueId);
        issueAttachmentService.purgeAttachmentsForIssue(issueId);
    }

    private Map<Long, List<String>> childKeysByParentId(List<Long> parentIds) {
        if (parentIds == null || parentIds.isEmpty()) {
            return Map.of();
        }
        return issueRepository.findByParent_IdIn(parentIds).stream()
                .collect(Collectors.groupingBy(
                        i -> i.getParent().getId(),
                        LinkedHashMap::new,
                        Collectors.mapping(Issue::getIssueKey, Collectors.toList())));
    }

    private void assertIssueInSpace(Issue issue, Long spaceId) {
        activeSpaceGuard.requireActive(issue.getSpace());
        if (!issue.getSpace().getId().equals(spaceId)) {
            throw new IllegalArgumentException("Issue does not belong to space: " + spaceId);
        }
    }

    /** Board/list: core issue row + subtask keys only; comments/links/attachments loaded via {@link #findByKey}. */
    private IssueDto toListDto(Issue issue, List<String> childKeys) {
        IssueDto dto = IssueDto.from(issue);
        dto.setChildKeys(childKeys);
        dto.setComments(List.of());
        dto.setLinkedIssues(List.of());
        dto.setAttachments(List.of());
        dto.setCodeLinks(List.of());
        return dto;
    }

    private IssueDto toDto(Issue issue) {
        IssueDto dto = IssueDto.from(issue);

        List<String> childKeys = issueRepository.findByParentId(issue.getId()).stream()
                .map(Issue::getIssueKey)
                .collect(Collectors.toList());
        dto.setChildKeys(childKeys);

        List<CommentDto> comments = commentRepository.findByIssueIdOrderByCreatedAtAsc(issue.getId()).stream()
                .map(CommentDto::from)
                .collect(Collectors.toList());
        dto.setComments(comments);
        dto.setLinkedIssues(issueLinkService.findByIssue(issue.getId()));
        dto.setAttachments(issueAttachmentService.findByIssue(issue.getId()));
        dto.setCodeLinks(issueCodeLinkService.findByIssue(issue.getId()));

        return dto;
    }
}
