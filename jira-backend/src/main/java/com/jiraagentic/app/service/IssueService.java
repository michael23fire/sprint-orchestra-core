package com.jiraagentic.app.service;

import com.jiraagentic.app.dto.CommentDto;
import com.jiraagentic.app.dto.CreateIssueRequest;
import com.jiraagentic.app.dto.IssueDto;
import com.jiraagentic.app.dto.UpdateIssueRequest;
import com.jiraagentic.app.entity.Issue;
import com.jiraagentic.app.entity.Space;
import com.jiraagentic.app.entity.Sprint;
import com.jiraagentic.app.repository.*;
import lombok.RequiredArgsConstructor;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

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
    private final WorkLogRepository workLogRepository;
    private final IssueHistoryRepository issueHistoryRepository;
    private final ActiveSpaceGuard activeSpaceGuard;

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

    public IssueDto findByKey(String issueKey) {
        Issue issue = issueRepository.findByIssueKey(issueKey)
                .orElseThrow(() -> new RuntimeException("Issue not found: " + issueKey));
        activeSpaceGuard.requireActive(issue.getSpace());
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
            Issue parent = issueRepository.findById(req.getParentId()).orElse(null);
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
        issue.setLabels(req.getLabels() != null ? req.getLabels() : List.of());

        Long reporterId = req.getReporterId() != null ? req.getReporterId() : creatorUserId;
        if (reporterId != null) {
            issue.setReporter(userRepository.findById(reporterId).orElse(null));
        }

        if (req.getAssigneeId() != null) {
            issue.setAssignee(userRepository.findById(req.getAssigneeId()).orElse(null));
        }
        if (req.getSprintId() != null && !isEpicType(issue.getIssueType())) {
            issue.setSprint(sprintRepository.findById(req.getSprintId()).orElse(null));
        }

        applyEpicInvariants(issue);
        assertSubtaskNotParentedOnEpic(issue);

        Issue saved = issueRepository.save(issue);
        issueHistoryService.recordEvent(saved, creatorUserId, "issue_created", "Issue created");
        return toDto(saved);
    }

    @Transactional
    public IssueDto update(String issueKey, UpdateIssueRequest req, Long actorUserId) {
        Issue issue = issueRepository.findByIssueKey(issueKey)
                .orElseThrow(() -> new RuntimeException("Issue not found: " + issueKey));
        activeSpaceGuard.requireActive(issue.getSpace());

        String oldTitle = issue.getTitle();
        String oldDescription = issue.getDescription();
        String oldIssueType = issue.getIssueType();
        String oldStatus = issue.getStatus();
        String oldPriority = issue.getPriority();
        String oldAssignee = issue.getAssignee() != null ? issue.getAssignee().getName() : null;
        String oldReporter = issue.getReporter() != null ? issue.getReporter().getName() : null;
        String oldSprint = issue.getSprint() != null ? issue.getSprint().getName() : null;
        String oldParent = issue.getParent() != null ? issue.getParent().getIssueKey() : null;
        Integer oldStoryPoints = issue.getStoryPoints();
        String oldStartDate = issue.getStartDate() != null ? issue.getStartDate().toString() : null;
        String oldDueDate = issue.getDueDate() != null ? issue.getDueDate().toString() : null;
        String oldLabels = issue.getLabels() != null ? String.join(", ", issue.getLabels()) : null;
        Integer oldIssueOrder = issue.getIssueOrder();

        if (req.getTitle() != null) issue.setTitle(req.getTitle());
        if (req.getDescription() != null) issue.setDescription(req.getDescription());
        if (req.getIssueType() != null) issue.setIssueType(req.getIssueType());
        if (req.getStatus() != null) issue.setStatus(req.getStatus());
        if (req.getPriority() != null) issue.setPriority(req.getPriority());
        if (req.getStoryPoints() != null) issue.setStoryPoints(req.getStoryPoints());
        if (req.getStartDate() != null) issue.setStartDate(req.getStartDate());
        if (req.getDueDate() != null) issue.setDueDate(req.getDueDate());
        if (req.getIssueOrder() != null) issue.setIssueOrder(req.getIssueOrder());
        if (req.getLabels() != null) issue.setLabels(req.getLabels());

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
            issue.setSprint(sprintRepository.findById(req.getSprintId()).orElse(null));
        }
        if (Boolean.TRUE.equals(req.getClearParent())) {
            issue.setParent(null);
        } else if (req.getParentId() != null) {
            issue.setParent(issueRepository.findById(req.getParentId()).orElse(null));
        }

        applyEpicInvariants(issue);
        assertSubtaskNotParentedOnEpic(issue);

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
        issueHistoryService.recordFieldChange(issue, actorUserId, "labels", oldLabels, issue.getLabels() != null ? String.join(", ", issue.getLabels()) : null);
        issueHistoryService.recordFieldChange(issue, actorUserId, "issueOrder", oldIssueOrder, issue.getIssueOrder());

        Issue saved = issueRepository.save(issue);
        if (req.getDescription() != null && !Objects.equals(oldDescription, saved.getDescription())) {
            issueAttachmentService.reconcileEmbeddedAttachments(saved.getId(), actorUserId);
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
            child.setSprint(sprintOrNull);
            issueRepository.save(child);
            cascadeSprintToChildren(child.getId(), sprintOrNull);
        }
    }

    private static boolean isEpicType(String issueType) {
        return issueType != null && "epic".equalsIgnoreCase(issueType);
    }

    private static boolean isSubtaskType(String issueType) {
        return issueType != null && "subtask".equalsIgnoreCase(issueType);
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
    }

    private void assertSubtaskNotParentedOnEpic(Issue issue) {
        if (!isSubtaskType(issue.getIssueType()) || issue.getParent() == null) {
            return;
        }
        if (isEpicType(issue.getParent().getIssueType())) {
            throw new IllegalArgumentException(
                    "Subtasks cannot be attached to an epic; attach them to a story or task under the epic.");
        }
    }

    @Transactional
    public void delete(String issueKey, Long actorUserId) {
        Issue issue = issueRepository.findByIssueKey(issueKey)
                .orElseThrow(() -> new RuntimeException("Issue not found: " + issueKey));
        activeSpaceGuard.requireActive(issue.getSpace());
        List<Long> postOrder = new ArrayList<>();
        collectSubtreePostOrder(issue.getId(), postOrder);
        for (Long issueId : postOrder) {
            purgeIssueDependencies(issueId);
            Issue toDelete = issueRepository.findById(issueId)
                    .orElseThrow(() -> new RuntimeException("Issue not found: id=" + issueId));
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
        workLogRepository.deleteByIssue_Id(issueId);
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
