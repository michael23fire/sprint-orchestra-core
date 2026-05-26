package com.jiraagentic.app.repository;

import com.jiraagentic.app.entity.IssueCodeLink;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Query;
import org.springframework.data.repository.query.Param;

import java.util.List;
import java.util.Optional;

public interface IssueCodeLinkRepository extends JpaRepository<IssueCodeLink, Long> {

    @Query("SELECT l FROM IssueCodeLink l WHERE l.issue.id = :issueId ORDER BY COALESCE(l.lastActivityAt, l.createdAt) DESC")
    List<IssueCodeLink> findByIssueIdOrderByActivityDesc(@Param("issueId") Long issueId);

    Optional<IssueCodeLink> findByIssueIdAndUrl(Long issueId, String url);

    @Query("SELECT l FROM IssueCodeLink l JOIN l.issue i WHERE i.space.id = :spaceId ORDER BY COALESCE(l.lastActivityAt, l.createdAt) DESC")
    List<IssueCodeLink> findByIssueSpaceIdOrderByActivityDesc(@Param("spaceId") Long spaceId);

    void deleteByIssue_Id(Long issueId);
}
