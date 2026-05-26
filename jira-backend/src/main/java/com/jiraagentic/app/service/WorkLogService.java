package com.jiraagentic.app.service;

import com.jiraagentic.app.dto.CreateWorkLogRequest;
import com.jiraagentic.app.dto.UpdateWorkLogRequest;
import com.jiraagentic.app.dto.WorkLogDto;
import com.jiraagentic.app.entity.Issue;
import com.jiraagentic.app.entity.WorkLog;
import com.jiraagentic.app.repository.IssueRepository;
import com.jiraagentic.app.repository.UserRepository;
import com.jiraagentic.app.repository.WorkLogRepository;
import lombok.RequiredArgsConstructor;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.time.LocalDate;
import java.util.List;
import java.util.stream.Collectors;

@Service
@RequiredArgsConstructor
public class WorkLogService {

    private final WorkLogRepository workLogRepository;
    private final IssueRepository issueRepository;
    private final UserRepository userRepository;
    private final IssueHistoryService issueHistoryService;
    private final ActiveSpaceGuard activeSpaceGuard;

    public List<WorkLogDto> findByIssue(Long issueId) {
        Issue issue = issueRepository.findById(issueId)
                .orElseThrow(() -> new RuntimeException("Issue not found: " + issueId));
        activeSpaceGuard.requireActive(issue.getSpace());
        return workLogRepository.findByIssueIdOrderByLogDateDescCreatedAtDesc(issueId).stream()
                .map(WorkLogDto::from)
                .collect(Collectors.toList());
    }

    @Transactional
    public WorkLogDto create(Long issueId, CreateWorkLogRequest req) {
        Issue issue = issueRepository.findById(issueId)
                .orElseThrow(() -> new RuntimeException("Issue not found: " + issueId));
        activeSpaceGuard.requireActive(issue.getSpace());
        WorkLog w = new WorkLog();
        w.setIssue(issue);
        w.setAuthor(userRepository.findById(req.getAuthorId())
                .orElseThrow(() -> new RuntimeException("User not found: " + req.getAuthorId())));
        w.setSpentMinutes(req.getSpentMinutes() == null ? 0 : req.getSpentMinutes());
        w.setNote(req.getNote());
        w.setLogDate(req.getLogDate() == null ? LocalDate.now() : req.getLogDate());
        WorkLog saved = workLogRepository.save(w);
        issueHistoryService.recordEvent(issue, req.getAuthorId(), "worklog_created", "logged work");
        return WorkLogDto.from(saved);
    }

    @Transactional
    public WorkLogDto update(Long id, UpdateWorkLogRequest req) {
        WorkLog w = workLogRepository.findById(id)
                .orElseThrow(() -> new RuntimeException("Work log not found: " + id));
        activeSpaceGuard.requireActive(w.getIssue().getSpace());
        Integer oldSpentMinutes = w.getSpentMinutes();
        String oldNote = w.getNote();
        String oldLogDate = w.getLogDate() != null ? w.getLogDate().toString() : null;
        if (req.getSpentMinutes() != null) w.setSpentMinutes(req.getSpentMinutes());
        if (req.getNote() != null) w.setNote(req.getNote());
        if (req.getLogDate() != null) w.setLogDate(req.getLogDate());
        WorkLog saved = workLogRepository.save(w);
        Long actorId = saved.getAuthor() != null ? saved.getAuthor().getId() : null;
        issueHistoryService.recordFieldChange(saved.getIssue(), actorId, "worklogMinutes", oldSpentMinutes, saved.getSpentMinutes());
        issueHistoryService.recordFieldChange(saved.getIssue(), actorId, "worklogNote", oldNote, saved.getNote());
        issueHistoryService.recordFieldChange(saved.getIssue(), actorId, "worklogDate", oldLogDate, saved.getLogDate() != null ? saved.getLogDate().toString() : null);
        return WorkLogDto.from(saved);
    }

    @Transactional
    public void delete(Long id) {
        WorkLog workLog = workLogRepository.findById(id).orElse(null);
        if (workLog == null) {
            throw new RuntimeException("Work log not found: " + id);
        }
        activeSpaceGuard.requireActive(workLog.getIssue().getSpace());
        issueHistoryService.recordEvent(
                workLog.getIssue(),
                workLog.getAuthor() != null ? workLog.getAuthor().getId() : null,
                "worklog_deleted",
                "deleted a work log"
        );
        workLogRepository.deleteById(id);
    }
}
