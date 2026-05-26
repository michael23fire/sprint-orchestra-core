package com.jiraagentic.app.service;

import com.jiraagentic.app.dto.CreateSprintRequest;
import com.jiraagentic.app.dto.SprintDto;
import com.jiraagentic.app.entity.Issue;
import com.jiraagentic.app.entity.Space;
import com.jiraagentic.app.entity.Sprint;
import com.jiraagentic.app.repository.IssueRepository;
import com.jiraagentic.app.repository.SprintRepository;
import lombok.RequiredArgsConstructor;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.util.List;
import java.util.stream.Collectors;

@Service
@RequiredArgsConstructor
public class SprintService {

    private final SprintRepository sprintRepository;
    private final IssueRepository issueRepository;
    private final ActiveSpaceGuard activeSpaceGuard;

    public List<SprintDto> findBySpace(Long spaceId) {
        activeSpaceGuard.requireActive(spaceId);
        return sprintRepository.findBySpaceIdOrderByStartDateAsc(spaceId).stream()
                .map(SprintDto::from)
                .collect(Collectors.toList());
    }

    public SprintDto findById(Long id) {
        Sprint sprint = sprintRepository.findById(id)
                .orElseThrow(() -> new RuntimeException("Sprint not found: " + id));
        activeSpaceGuard.requireActive(sprint.getSpace());
        return SprintDto.from(sprint);
    }

    @Transactional
    public SprintDto create(Long spaceId, CreateSprintRequest req) {
        Space space = activeSpaceGuard.requireActive(spaceId);

        Sprint sprint = new Sprint();
        sprint.setSpace(space);
        sprint.setName(req.getName());
        sprint.setGoal(req.getGoal());
        sprint.setStartDate(req.getStartDate());
        sprint.setEndDate(req.getEndDate());
        sprint.setStatus(req.getStatus() != null ? req.getStatus() : "future");
        return SprintDto.from(sprintRepository.save(sprint));
    }

    @Transactional
    public SprintDto update(Long id, CreateSprintRequest req) {
        Sprint sprint = sprintRepository.findById(id)
                .orElseThrow(() -> new RuntimeException("Sprint not found: " + id));
        activeSpaceGuard.requireActive(sprint.getSpace());
        if (req.getName() != null) sprint.setName(req.getName());
        if (req.getGoal() != null) sprint.setGoal(req.getGoal());
        if (req.getStartDate() != null) sprint.setStartDate(req.getStartDate());
        if (req.getEndDate() != null) sprint.setEndDate(req.getEndDate());
        if (req.getStatus() != null) sprint.setStatus(req.getStatus());

        if ("completed".equals(req.getStatus())) {
            moveIncompleteIssuesToBacklog(id);
        }

        return SprintDto.from(sprintRepository.save(sprint));
    }

    /** Clear sprint assignment for issues not in Done when the sprint is completed (Jira-style backlog). */
    private void moveIncompleteIssuesToBacklog(Long sprintId) {
        List<Issue> inSprint = issueRepository.findBySprint_Id(sprintId);
        for (Issue issue : inSprint) {
            if (!"done".equals(issue.getStatus())) {
                issue.setSprint(null);
            }
        }
        issueRepository.saveAll(inSprint);
    }

    @Transactional
    public void delete(Long id) {
        Sprint sprint = sprintRepository.findById(id)
                .orElseThrow(() -> new RuntimeException("Sprint not found: " + id));
        activeSpaceGuard.requireActive(sprint.getSpace());
        sprintRepository.deleteById(id);
    }
}
