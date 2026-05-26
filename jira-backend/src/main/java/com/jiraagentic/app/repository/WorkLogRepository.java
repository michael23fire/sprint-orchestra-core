package com.jiraagentic.app.repository;

import com.jiraagentic.app.entity.WorkLog;
import org.springframework.data.jpa.repository.JpaRepository;

import java.util.List;

public interface WorkLogRepository extends JpaRepository<WorkLog, Long> {
    List<WorkLog> findByIssueIdOrderByLogDateDescCreatedAtDesc(Long issueId);

    void deleteByIssue_Id(Long issueId);
}
