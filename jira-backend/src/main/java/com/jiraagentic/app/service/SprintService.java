package com.jiraagentic.app.service;

import com.jiraagentic.app.dto.CompleteSprintRequest;
import com.jiraagentic.app.dto.CreateSprintRequest;
import com.jiraagentic.app.dto.ReorderSprintRequest;
import com.jiraagentic.app.dto.SprintDto;
import com.jiraagentic.app.entity.Issue;
import com.jiraagentic.app.entity.Space;
import com.jiraagentic.app.entity.Sprint;
import com.jiraagentic.app.event.SprintContentChangedEvent;
import com.jiraagentic.app.event.SprintDeletedEvent;
import com.jiraagentic.app.repository.IssueRepository;
import com.jiraagentic.app.repository.SpaceRepository;
import com.jiraagentic.app.repository.SprintIssueHistoryRepository;
import com.jiraagentic.app.repository.SprintRepository;
import lombok.RequiredArgsConstructor;
import org.springframework.context.ApplicationEventPublisher;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.time.Instant;
import java.time.LocalDate;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.List;
import java.util.Locale;
import java.util.stream.Collectors;

@Service
@RequiredArgsConstructor
public class SprintService {

    private final SprintRepository sprintRepository;
    private final IssueRepository issueRepository;
    private final SpaceRepository spaceRepository;
    private final ActiveSpaceGuard activeSpaceGuard;
    private final SprintHistoryService sprintHistoryService;
    private final SprintIssueHistoryRepository sprintIssueHistoryRepository;
    private final ApplicationEventPublisher applicationEventPublisher;

    /** Bridges a saved Sprint to the RAG content-ingestion stream — see SprintContentChangedEvent's
     *  docstring for why the goal text specifically needs its own event to be searchable at all. */
    private void publishSprintChanged(Sprint saved) {
        applicationEventPublisher.publishEvent(new SprintContentChangedEvent(
                saved.getId(), saved.getName(), saved.getSpace().getId(), saved.getGoal(),
                saved.getStartDate(), saved.getEndDate(), saved.getStatus(),
                saved.getInitialCommittedPoints(), saved.getInitialCompletedPoints(),
                saved.getFinalScopePoints(), saved.getCompletedPoints(),
                saved.getInitialIssueCount(), saved.getCompletedIssueCount(),
                saved.getFinalIssueCount(), saved.getUnestimatedIssueCount(),
                Instant.now()));
    }

    /**
     * Jira backlog order: completed (closed) first → active → future (by sprint_order).
     * Completed are newest-closed first; future keep manual order.
     */
    public List<SprintDto> findBySpace(Long spaceId) {
        activeSpaceGuard.requireActive(spaceId);
        List<Sprint> all = sprintRepository.findBySpaceIdOrderByStartDateAsc(spaceId);

        List<Sprint> completed = all.stream()
                .filter(s -> "completed".equals(s.getStatus()))
                .sorted(Comparator
                        .comparing(Sprint::getUpdatedAt, Comparator.nullsLast(Comparator.reverseOrder()))
                        .thenComparing(Sprint::getId, Comparator.reverseOrder()))
                .collect(Collectors.toList());

        List<Sprint> active = all.stream()
                .filter(s -> "active".equals(s.getStatus()))
                .sorted(Comparator.comparing(Sprint::getId))
                .collect(Collectors.toList());

        List<Sprint> future = all.stream()
                .filter(s -> "future".equals(s.getStatus()))
                .sorted(Comparator
                        .comparing(Sprint::getSprintOrder, Comparator.nullsLast(Comparator.naturalOrder()))
                        .thenComparing(Sprint::getId))
                .collect(Collectors.toList());

        List<Sprint> ordered = new ArrayList<>(completed.size() + active.size() + future.size());
        ordered.addAll(completed);
        ordered.addAll(active);
        ordered.addAll(future);
        return ordered.stream().map(SprintDto::from).collect(Collectors.toList());
    }

    public SprintDto findById(Long spaceId, Long id) {
        Sprint sprint = sprintRepository.findById(id)
                .orElseThrow(() -> new RuntimeException("Sprint not found: " + id));
        assertSprintInSpace(sprint, spaceId);
        return SprintDto.from(sprint);
    }

    @Transactional
    public SprintDto create(Long spaceId, CreateSprintRequest req) {
        Space space = lockActiveSpace(spaceId);

        Sprint sprint = new Sprint();
        sprint.setSpace(space);
        sprint.setName(req.getName());
        sprint.setGoal(req.getGoal());
        sprint.setStartDate(req.getStartDate());
        sprint.setEndDate(req.getEndDate());
        String requestedStatus = req.getStatus() != null ? req.getStatus() : "future";
        if (!"future".equals(requestedStatus) && !"active".equals(requestedStatus)) {
            throw new IllegalArgumentException("A sprint must be created as future or active");
        }
        sprint.setStatus(requestedStatus);
        sprint.setSprintOrder(nextSprintOrder(spaceId, sprint.getStatus()));

        assertDatesValid(sprint.getStartDate(), sprint.getEndDate());
        assertNoDateOverlap(spaceId, null, sprint.getStartDate(), sprint.getEndDate());
        if ("active".equals(sprint.getStatus())) {
            assertSingleActive(spaceId, null);
        }

        Sprint saved = sprintRepository.save(sprint);
        if ("active".equals(saved.getStatus())) {
            List<Issue> issues = issueRepository.findBySprint_Id(saved.getId());
            sprintHistoryService.assertSprintReady(issues);
            sprintHistoryService.snapshotInitialScope(saved, issues);
        }
        publishSprintChanged(saved);
        return SprintDto.from(saved);
    }

    @Transactional
    public SprintDto update(Long spaceId, Long id, CreateSprintRequest req) {
        lockActiveSpace(spaceId);
        Sprint sprint = sprintRepository.findById(id)
                .orElseThrow(() -> new RuntimeException("Sprint not found: " + id));
        Space space = sprint.getSpace();
        String oldStatus = sprint.getStatus();
        assertSprintInSpace(sprint, spaceId);
        if (req.getStatus() != null) {
            if (!"future".equals(req.getStatus())
                    && !"active".equals(req.getStatus())
                    && !"completed".equals(req.getStatus())) {
                throw new IllegalArgumentException("Invalid sprint status: " + req.getStatus());
            }
            if ("completed".equals(req.getStatus()) && !"completed".equals(oldStatus)) {
                throw new IllegalArgumentException("Use the complete sprint action to close a sprint");
            }
            if ("completed".equals(oldStatus) && !"completed".equals(req.getStatus())) {
                throw new IllegalArgumentException("A completed sprint cannot be reopened");
            }
            if ("active".equals(oldStatus) && "future".equals(req.getStatus())) {
                throw new IllegalArgumentException("An active sprint cannot be moved back to future");
            }
        }
        if (req.getName() != null) sprint.setName(req.getName());
        if (req.getGoal() != null) sprint.setGoal(req.getGoal());
        if (req.getStartDate() != null) sprint.setStartDate(req.getStartDate());
        if (req.getEndDate() != null) sprint.setEndDate(req.getEndDate());
        if (req.getStatus() != null) sprint.setStatus(req.getStatus());

        assertDatesValid(sprint.getStartDate(), sprint.getEndDate());
        assertNoDateOverlap(space.getId(), sprint.getId(), sprint.getStartDate(), sprint.getEndDate());
        if ("active".equals(sprint.getStatus())) {
            assertSingleActive(space.getId(), sprint.getId());
        }

        boolean starting = !"active".equals(oldStatus) && "active".equals(sprint.getStatus());
        List<Issue> issues = starting ? issueRepository.findBySprint_Id(sprint.getId()) : List.of();
        if (starting) sprintHistoryService.assertSprintReady(issues);
        Sprint saved = sprintRepository.save(sprint);
        if (starting) sprintHistoryService.snapshotInitialScope(saved, issues);
        publishSprintChanged(saved);
        return SprintDto.from(saved);
    }

    /**
     * Reorder a future sprint among other future sprints only.
     * Active and completed sprints cannot be moved.
     */
    @Transactional
    public List<SprintDto> reorder(Long spaceId, Long sprintId, ReorderSprintRequest req) {
        lockActiveSpace(spaceId);
        if (req == null || req.getAction() == null || req.getAction().isBlank()) {
            throw new IllegalArgumentException("action is required");
        }
        String action = req.getAction().trim().toLowerCase(Locale.ROOT);

        Sprint sprint = sprintRepository.findById(sprintId)
                .orElseThrow(() -> new RuntimeException("Sprint not found: " + sprintId));
        if (!sprint.getSpace().getId().equals(spaceId)) {
            throw new IllegalArgumentException("Sprint does not belong to space: " + spaceId);
        }
        if (!"future".equals(sprint.getStatus())) {
            throw new IllegalArgumentException("Only future sprints can be reordered");
        }

        List<Sprint> future = new ArrayList<>(
                sprintRepository.findBySpaceIdAndStatusOrderBySprintOrderAscIdAsc(spaceId, "future"));
        int index = -1;
        for (int i = 0; i < future.size(); i++) {
            if (future.get(i).getId().equals(sprintId)) {
                index = i;
                break;
            }
        }
        if (index < 0) {
            throw new IllegalArgumentException("Sprint not found among future sprints: " + sprintId);
        }

        int targetIndex = switch (action) {
            case "move_up" -> Math.max(0, index - 1);
            case "move_down" -> Math.min(future.size() - 1, index + 1);
            case "move_to_top" -> 0;
            case "move_to_bottom" -> future.size() - 1;
            default -> throw new IllegalArgumentException(
                    "Invalid action: " + action + " (expected move_up, move_down, move_to_top, move_to_bottom)");
        };

        if (targetIndex != index) {
            Sprint moving = future.remove(index);
            future.add(targetIndex, moving);
            for (int i = 0; i < future.size(); i++) {
                future.get(i).setSprintOrder(i);
            }
            sprintRepository.saveAll(future);
        }

        return findBySpace(spaceId);
    }

    /** Enforces Jira's default "no parallel sprints" rule: only one active sprint per space. */
    private void assertSingleActive(Long spaceId, Long excludeSprintId) {
        List<Sprint> activeSprints = sprintRepository.findBySpaceIdAndStatusOrderByIdAsc(spaceId, "active");
        boolean otherActiveExists = activeSprints.stream()
                .anyMatch(s -> excludeSprintId == null || !s.getId().equals(excludeSprintId));
        if (otherActiveExists) {
            throw new IllegalArgumentException(
                    "There can only be one active sprint. Complete the current active sprint first.");
        }
    }

    private void assertDatesValid(LocalDate start, LocalDate end) {
        if (start != null && end != null && end.isBefore(start)) {
            throw new IllegalArgumentException("Sprint end date must be on or after the start date.");
        }
    }

    /** Enforces Jira's default "no parallel sprints" rule: active/future sprints in a space must not overlap in date range. */
    private void assertNoDateOverlap(Long spaceId, Long excludeSprintId, LocalDate start, LocalDate end) {
        if (start == null || end == null) return;
        List<Sprint> others = sprintRepository.findBySpaceIdOrderByStartDateAsc(spaceId);
        for (Sprint other : others) {
            if (excludeSprintId != null && other.getId().equals(excludeSprintId)) continue;
            String st = other.getStatus();
            if ("completed".equals(st)) continue;
            LocalDate os = other.getStartDate();
            LocalDate oe = other.getEndDate();
            if (os == null || oe == null) continue;
            if (!start.isAfter(oe) && !os.isAfter(end)) {
                throw new IllegalArgumentException(
                        "Sprint dates overlap with \"" + other.getName() + "\" (" + os + " \u2013 " + oe + ").");
            }
        }
    }

    /**
     * Jira-style "Complete Sprint": issues already Done stay on the completed sprint; incomplete
     * issues move to the caller-chosen destination (backlog, an existing future sprint, or a
     * newly created future sprint).
     */
    @Transactional
    public SprintDto complete(Long spaceId, Long id, CompleteSprintRequest req) {
        lockActiveSpace(spaceId);
        Sprint sprint = sprintRepository.findById(id)
                .orElseThrow(() -> new RuntimeException("Sprint not found: " + id));
        Space space = sprint.getSpace();
        assertSprintInSpace(sprint, spaceId);

        if (!"active".equals(sprint.getStatus())) {
            throw new IllegalArgumentException("Only an active sprint can be completed: " + id);
        }

        List<Issue> issuesInSprint = issueRepository.findBySprint_Id(id);
        sprintHistoryService.assertSprintReady(issuesInSprint);
        sprintHistoryService.finalizeSprint(sprint, issuesInSprint);
        List<Issue> incompleteIssues = issuesInSprint.stream()
                .filter(issue -> !"done".equals(issue.getStatus()))
                .collect(Collectors.toList());

        if (!incompleteIssues.isEmpty()) {
            String destination = req.getIncompleteDestination();
            if (destination == null || destination.isBlank()) {
                throw new IllegalArgumentException("incompleteDestination is required when the sprint has incomplete issues");
            }

            Sprint destinationSprint = resolveDestinationSprint(space, sprint, destination, req);
            for (Issue issue : incompleteIssues) {
                issue.setSprint(destinationSprint);
            }
            issueRepository.saveAll(incompleteIssues);
        }

        sprint.setStatus("completed");
        Sprint saved = sprintRepository.save(sprint);
        publishSprintChanged(saved);
        // The incomplete issues just moved to destinationSprint above bypass IssueService.update(),
        // so no per-issue IssueHistoryRecordedEvent/'sprint' field_change fires for them — same
        // documented gap as the identical bulk-reassignment path in delete() below. Their vecdb
        // sprint_id/sprint_name snapshot goes stale until the next edit or backfill touches them.
        return SprintDto.from(saved);
    }

    /** Resolves the destination for incomplete issues: null for backlog, or a future sprint (existing or newly created). */
    private Sprint resolveDestinationSprint(Space space, Sprint completingSprint, String destination, CompleteSprintRequest req) {
        switch (destination) {
            case "backlog":
                return null;
            case "future_sprint": {
                Long moveToSprintId = req.getMoveToSprintId();
                if (moveToSprintId == null) {
                    throw new IllegalArgumentException("moveToSprintId is required when incompleteDestination = future_sprint");
                }
                Sprint target = sprintRepository.findById(moveToSprintId)
                        .orElseThrow(() -> new IllegalArgumentException("Sprint not found: " + moveToSprintId));
                if (!target.getSpace().getId().equals(space.getId())) {
                    throw new IllegalArgumentException("Destination sprint must be in the same space: " + moveToSprintId);
                }
                if (target.getId().equals(completingSprint.getId())) {
                    throw new IllegalArgumentException("Destination sprint must not be the sprint being completed: " + moveToSprintId);
                }
                if (!"future".equals(target.getStatus())) {
                    throw new IllegalArgumentException("Destination sprint must be a future sprint: " + moveToSprintId);
                }
                return target;
            }
            case "new_sprint": {
                Sprint newSprint = new Sprint();
                newSprint.setSpace(space);
                String name = req.getNewSprintName();
                newSprint.setName(name != null && !name.isBlank() ? name : nextDefaultSprintName(space.getId()));
                newSprint.setStatus("future");
                newSprint.setSprintOrder(nextSprintOrder(space.getId(), "future"));
                return sprintRepository.save(newSprint);
            }
            default:
                throw new IllegalArgumentException("Invalid incompleteDestination: " + destination);
        }
    }

    private int nextSprintOrder(Long spaceId, String status) {
        String orderStatus = status != null ? status : "future";
        return sprintRepository.findMaxSprintOrderBySpaceIdAndStatus(spaceId, orderStatus).orElse(-1) + 1;
    }

    private String nextDefaultSprintName(Long spaceId) {
        int count = sprintRepository.findBySpaceIdOrderByStartDateAsc(spaceId).size();
        return "Sprint " + (count + 1);
    }

    @Transactional
    public void delete(Long spaceId, Long id) {
        lockActiveSpace(spaceId);
        Sprint sprint = sprintRepository.findById(id)
                .orElseThrow(() -> new RuntimeException("Sprint not found: " + id));
        assertSprintInSpace(sprint, spaceId);

        // Jira Cloud allows deleting a sprint from any state (planned/active/completed).
        // Issues on the deleted sprint move to the next future sprint, or back to the backlog.
        Sprint nextSprint = sprintRepository
                .findBySpaceIdAndStatusOrderByIdAsc(sprint.getSpace().getId(), "future")
                .stream()
                .filter(s -> !s.getId().equals(id))
                .findFirst()
                .orElse(null);

        List<Issue> issuesOnSprint = issueRepository.findBySprint_Id(id);
        if (!issuesOnSprint.isEmpty()) {
            for (Issue issue : issuesOnSprint) {
                if (nextSprint != null) {
                    SprintHistoryService.assertEstimatedForSprint(issue);
                }
                issue.setSprint(nextSprint);
            }
            issueRepository.saveAll(issuesOnSprint);
        }

        sprintIssueHistoryRepository.deleteBySprint_Id(id);
        sprintRepository.deleteById(id);
        // Same bulk-reassignment caveat as complete() above: issues moved off this sprint just now
        // bypass IssueService.update(), so their vecdb sprint snapshot doesn't update until their
        // next real edit or the next backfill — documented, not silently dropped.
        applicationEventPublisher.publishEvent(
                new SprintDeletedEvent(sprint.getId(), sprint.getName(), sprint.getSpace().getId(), Instant.now()));
    }

    private void assertSprintInSpace(Sprint sprint, Long spaceId) {
        activeSpaceGuard.requireActive(sprint.getSpace());
        if (!sprint.getSpace().getId().equals(spaceId)) {
            throw new IllegalArgumentException("Sprint does not belong to space: " + spaceId);
        }
    }

    private Space lockActiveSpace(Long spaceId) {
        return spaceRepository.findActiveByIdForUpdate(spaceId)
                .orElseThrow(() -> new IllegalArgumentException("Space not found or deleted: " + spaceId));
    }
}
