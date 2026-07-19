package com.jiraagentic.app.dto;

import com.jiraagentic.app.entity.Sprint;
import lombok.Data;
import java.time.LocalDate;

@Data
public class SprintDto {
    private Long id;
    private Long spaceId;
    private String name;
    private String goal;
    private LocalDate startDate;
    private LocalDate endDate;
    private String status;
    private Integer sprintOrder;
    private Integer initialCommittedPoints;
    private Integer initialCompletedPoints;
    private Integer finalScopePoints;
    private Integer completedPoints;
    private Integer initialIssueCount;
    private Integer completedIssueCount;
    private Integer finalIssueCount;
    private Integer unestimatedIssueCount;
    private Integer commitmentCompletionPercent;
    private Integer finalScopeCompletionPercent;

    public static SprintDto from(Sprint s) {
        SprintDto dto = new SprintDto();
        dto.setId(s.getId());
        dto.setSpaceId(s.getSpace().getId());
        dto.setName(s.getName());
        dto.setGoal(s.getGoal());
        dto.setStartDate(s.getStartDate());
        dto.setEndDate(s.getEndDate());
        dto.setStatus(s.getStatus());
        dto.setSprintOrder(s.getSprintOrder());
        dto.setInitialCommittedPoints(s.getInitialCommittedPoints());
        dto.setInitialCompletedPoints(s.getInitialCompletedPoints());
        dto.setFinalScopePoints(s.getFinalScopePoints());
        dto.setCompletedPoints(s.getCompletedPoints());
        dto.setInitialIssueCount(s.getInitialIssueCount());
        dto.setCompletedIssueCount(s.getCompletedIssueCount());
        dto.setFinalIssueCount(s.getFinalIssueCount());
        dto.setUnestimatedIssueCount(s.getUnestimatedIssueCount());
        dto.setCommitmentCompletionPercent(percent(s.getInitialCompletedPoints(), s.getInitialCommittedPoints()));
        dto.setFinalScopeCompletionPercent(percent(s.getCompletedPoints(), s.getFinalScopePoints()));
        return dto;
    }

    private static Integer percent(Integer completed, Integer total) {
        if (completed == null || total == null || total <= 0) return null;
        return (int) Math.round((completed * 100.0) / total);
    }
}
