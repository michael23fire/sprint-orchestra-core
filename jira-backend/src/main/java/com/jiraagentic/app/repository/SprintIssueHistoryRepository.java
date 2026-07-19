package com.jiraagentic.app.repository;

import com.jiraagentic.app.entity.SprintIssueHistory;
import org.springframework.data.jpa.repository.JpaRepository;

import java.util.List;
import java.util.Optional;

public interface SprintIssueHistoryRepository extends JpaRepository<SprintIssueHistory, Long> {
    List<SprintIssueHistory> findBySprint_Id(Long sprintId);
    Optional<SprintIssueHistory> findBySprint_IdAndIssueId(Long sprintId, Long issueId);
    void deleteBySprint_Id(Long sprintId);
}
