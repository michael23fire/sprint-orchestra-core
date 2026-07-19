package com.jiraagentic.app.service;

import com.jiraagentic.app.entity.Issue;
import com.jiraagentic.app.entity.Sprint;
import com.jiraagentic.app.entity.SprintIssueHistory;
import com.jiraagentic.app.repository.SprintIssueHistoryRepository;
import lombok.RequiredArgsConstructor;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.time.Instant;
import java.util.HashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.stream.Collectors;

@Service
@RequiredArgsConstructor
public class SprintHistoryService {

    private final SprintIssueHistoryRepository historyRepository;

    public static boolean requiresEstimate(Issue issue) {
        if (issue == null || issue.getIssueType() == null) return false;
        String type = issue.getIssueType().toLowerCase(Locale.ROOT);
        return "story".equals(type) || "task".equals(type) || "bug".equals(type);
    }

    public static void assertEstimatedForSprint(Issue issue) {
        if (requiresEstimate(issue) && (issue.getStoryPoints() == null || issue.getStoryPoints() <= 0)) {
            throw new IllegalArgumentException(
                    "Story points are required before adding " + issue.getIssueKey() + " to a sprint");
        }
    }

    public void assertSprintReady(List<Issue> issues) {
        List<String> missing = issues.stream()
                .filter(SprintHistoryService::requiresEstimate)
                .filter(issue -> issue.getStoryPoints() == null || issue.getStoryPoints() <= 0)
                .map(Issue::getIssueKey)
                .collect(Collectors.toList());
        if (!missing.isEmpty()) {
            throw new IllegalArgumentException(
                    "Story points are required before starting the sprint. Missing: " + String.join(", ", missing));
        }
    }

    @Transactional
    public void snapshotInitialScope(Sprint sprint, List<Issue> issues) {
        if (!historyRepository.findBySprint_Id(sprint.getId()).isEmpty()) return;
        List<SprintIssueHistory> rows = issues.stream()
                .map(issue -> newRow(sprint, issue, true))
                .collect(Collectors.toList());
        historyRepository.saveAll(rows);
    }

    @Transactional
    public void trackAdded(Sprint sprint, Issue issue) {
        if (sprint == null || !"active".equals(sprint.getStatus()) || issue.getId() == null) return;
        historyRepository.findBySprint_IdAndIssueId(sprint.getId(), issue.getId())
                .orElseGet(() -> historyRepository.save(newRow(sprint, issue, false)));
    }

    @Transactional
    public void trackRemoved(Sprint sprint, Issue issue) {
        if (sprint == null || !"active".equals(sprint.getStatus()) || issue.getId() == null) return;
        SprintIssueHistory row = historyRepository.findBySprint_IdAndIssueId(sprint.getId(), issue.getId())
                .orElseGet(() -> newRow(sprint, issue, false));
        row.setPointsAtEnd(issue.getStoryPoints());
        row.setStatusAtEnd(issue.getStatus());
        row.setOutcome("removed");
        row.setRemovedAt(Instant.now());
        historyRepository.save(row);
    }

    @Transactional
    public void finalizeSprint(Sprint sprint, List<Issue> currentIssues) {
        List<SprintIssueHistory> existing = historyRepository.findBySprint_Id(sprint.getId());
        if (existing.isEmpty()) {
            snapshotInitialScope(sprint, currentIssues);
            existing = historyRepository.findBySprint_Id(sprint.getId());
        }

        Map<Long, SprintIssueHistory> byIssueId = new HashMap<>();
        for (SprintIssueHistory row : existing) byIssueId.put(row.getIssueId(), row);
        for (Issue issue : currentIssues) {
            SprintIssueHistory row = byIssueId.get(issue.getId());
            if (row == null) {
                row = historyRepository.save(newRow(sprint, issue, false));
                existing.add(row);
                byIssueId.put(issue.getId(), row);
            }
            row.setPointsAtEnd(issue.getStoryPoints());
            row.setStatusAtEnd(issue.getStatus());
            row.setOutcome("done".equals(issue.getStatus()) ? "completed" : "carried_over");
            row.setRemovedAt(null);
        }
        historyRepository.saveAll(existing);
        applyMetrics(sprint, existing);
    }

    private static SprintIssueHistory newRow(Sprint sprint, Issue issue, boolean initialScope) {
        SprintIssueHistory row = new SprintIssueHistory();
        row.setSprint(sprint);
        row.setIssueId(issue.getId());
        row.setIssueKey(issue.getIssueKey());
        row.setIssueType(issue.getIssueType());
        row.setInitialScope(initialScope);
        row.setPointsAtStart(initialScope ? issue.getStoryPoints() : null);
        row.setAddedAt(Instant.now());
        return row;
    }

    private static void applyMetrics(Sprint sprint, List<SprintIssueHistory> rows) {
        List<SprintIssueHistory> estimatable = rows.stream()
                .filter(SprintHistoryService::requiresEstimate)
                .collect(Collectors.toList());
        List<SprintIssueHistory> initial = estimatable.stream()
                .filter(SprintIssueHistory::isInitialScope)
                .collect(Collectors.toList());
        List<SprintIssueHistory> finalScope = estimatable.stream()
                .filter(row -> !"removed".equals(row.getOutcome()))
                .collect(Collectors.toList());

        sprint.setInitialIssueCount((int) rows.stream()
                .filter(SprintIssueHistory::isInitialScope)
                .count());
        sprint.setInitialCommittedPoints(sum(initial, true));
        sprint.setInitialCompletedPoints(initial.stream()
                .filter(row -> "completed".equals(row.getOutcome()))
                .mapToInt(row -> value(row.getPointsAtStart()))
                .sum());
        sprint.setFinalIssueCount((int) rows.stream()
                .filter(row -> !"removed".equals(row.getOutcome()))
                .count());
        sprint.setFinalScopePoints(sum(finalScope, false));
        sprint.setCompletedIssueCount((int) rows.stream()
                .filter(row -> "completed".equals(row.getOutcome()))
                .count());
        sprint.setCompletedPoints(finalScope.stream()
                .filter(row -> "completed".equals(row.getOutcome()))
                .mapToInt(row -> value(row.getPointsAtEnd()))
                .sum());
        sprint.setUnestimatedIssueCount((int) finalScope.stream()
                .filter(row -> row.getPointsAtEnd() == null || row.getPointsAtEnd() <= 0)
                .count());
    }

    private static boolean requiresEstimate(SprintIssueHistory row) {
        String type = row.getIssueType() == null ? "" : row.getIssueType().toLowerCase(Locale.ROOT);
        return "story".equals(type) || "task".equals(type) || "bug".equals(type);
    }

    private static int sum(List<SprintIssueHistory> rows, boolean start) {
        return rows.stream().mapToInt(row -> value(start ? row.getPointsAtStart() : row.getPointsAtEnd())).sum();
    }

    private static int value(Integer value) {
        return value != null && value > 0 ? value : 0;
    }
}
