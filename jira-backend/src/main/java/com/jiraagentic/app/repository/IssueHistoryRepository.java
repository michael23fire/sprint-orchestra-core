package com.jiraagentic.app.repository;

import com.jiraagentic.app.entity.IssueHistory;
import org.springframework.data.jpa.repository.JpaRepository;

import java.util.List;

public interface IssueHistoryRepository extends JpaRepository<IssueHistory, Long> {
    List<IssueHistory> findByIssueIdOrderByCreatedAtDesc(Long issueId);

    void deleteByIssue_Id(Long issueId);
}
