package com.jiraagentic.app.service;

import com.jiraagentic.app.dto.SprintDto;
import com.jiraagentic.app.entity.Issue;
import com.jiraagentic.app.entity.Space;
import com.jiraagentic.app.entity.Sprint;
import com.jiraagentic.app.entity.SprintIssueHistory;
import com.jiraagentic.app.repository.SprintIssueHistoryRepository;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;

import java.util.ArrayList;
import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.mockito.Mockito.when;

@ExtendWith(MockitoExtension.class)
class SprintHistoryServiceTest {

    @Mock SprintIssueHistoryRepository historyRepository;

    @Test
    void rejectsStartingSprintWithUnestimatedWork() {
        Issue issue = issue(1L, "APP-1", "story", null, "planned");

        assertThatThrownBy(() -> service().assertSprintReady(List.of(issue)))
                .isInstanceOf(IllegalArgumentException.class)
                .hasMessageContaining("APP-1");
    }

    @Test
    void finalizesStableCommitmentAndFinalScopeMetrics() {
        Space space = new Space();
        space.setId(1L);
        Sprint sprint = new Sprint();
        sprint.setId(10L);
        sprint.setSpace(space);

        Issue doneInitial = issue(1L, "APP-1", "story", 5, "done");
        Issue carriedInitial = issue(2L, "APP-2", "task", 3, "in_progress");
        Issue doneAdded = issue(3L, "APP-3", "bug", 2, "done");
        Issue doneSubtask = issue(4L, "APP-4", "subtask", null, "done");

        List<SprintIssueHistory> rows = new ArrayList<>(List.of(
                row(sprint, doneInitial, true, 5),
                row(sprint, carriedInitial, true, 3),
                row(sprint, doneAdded, false, null),
                row(sprint, doneSubtask, true, null)));
        when(historyRepository.findBySprint_Id(10L)).thenReturn(rows);

        service().finalizeSprint(sprint, List.of(doneInitial, carriedInitial, doneAdded, doneSubtask));

        assertThat(sprint.getInitialCommittedPoints()).isEqualTo(8);
        assertThat(sprint.getInitialCompletedPoints()).isEqualTo(5);
        assertThat(sprint.getFinalScopePoints()).isEqualTo(10);
        assertThat(sprint.getCompletedPoints()).isEqualTo(7);
        assertThat(sprint.getInitialIssueCount()).isEqualTo(3);
        assertThat(sprint.getFinalIssueCount()).isEqualTo(4);
        assertThat(sprint.getCompletedIssueCount()).isEqualTo(3);
        assertThat(sprint.getUnestimatedIssueCount()).isZero();

        SprintDto dto = SprintDto.from(sprint);
        assertThat(dto.getCommitmentCompletionPercent()).isEqualTo(63);
        assertThat(dto.getFinalScopeCompletionPercent()).isEqualTo(70);
    }

    private SprintHistoryService service() {
        return new SprintHistoryService(historyRepository);
    }

    private static Issue issue(
            Long id,
            String key,
            String type,
            Integer points,
            String status) {
        Issue issue = new Issue();
        issue.setId(id);
        issue.setIssueKey(key);
        issue.setIssueType(type);
        issue.setStoryPoints(points);
        issue.setStatus(status);
        return issue;
    }

    private static SprintIssueHistory row(
            Sprint sprint,
            Issue issue,
            boolean initialScope,
            Integer startPoints) {
        SprintIssueHistory row = new SprintIssueHistory();
        row.setSprint(sprint);
        row.setIssueId(issue.getId());
        row.setIssueKey(issue.getIssueKey());
        row.setIssueType(issue.getIssueType());
        row.setInitialScope(initialScope);
        row.setPointsAtStart(startPoints);
        return row;
    }
}
