package com.jiraagentic.app.repository;

import com.jiraagentic.app.entity.IssueLink;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Modifying;
import org.springframework.data.jpa.repository.Query;
import org.springframework.data.repository.query.Param;

import java.util.List;
import java.util.Optional;

public interface IssueLinkRepository extends JpaRepository<IssueLink, Long> {
    List<IssueLink> findBySourceIssueIdOrTargetIssueId(Long sourceIssueId, Long targetIssueId);
    Optional<IssueLink> findBySourceIssueIdAndTargetIssueIdAndLinkType(Long sourceIssueId, Long targetIssueId, String linkType);

    @Modifying
    @Query("DELETE FROM IssueLink l WHERE l.sourceIssue.id = :issueId OR l.targetIssue.id = :issueId")
    void deleteAllInvolvingIssue(@Param("issueId") Long issueId);
}
