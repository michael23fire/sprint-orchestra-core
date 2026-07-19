package com.jiraagentic.app.repository;

import com.jiraagentic.app.entity.Issue;
import org.springframework.data.jpa.repository.JpaRepository;

import java.util.Collection;
import java.util.List;
import java.util.Optional;

public interface IssueRepository extends JpaRepository<Issue, Long> {

    Optional<Issue> findByIssueKey(String issueKey);

    List<Issue> findBySpaceIdOrderByIssueOrderAsc(Long spaceId);

    List<Issue> findBySpaceIdAndSprintIdOrderByIssueOrderAsc(Long spaceId, Long sprintId);

    List<Issue> findBySpaceIdAndSprintIsNullOrderByIssueOrderAsc(Long spaceId);

    List<Issue> findByParentId(Long parentId);

    List<Issue> findByParent_IdIn(Collection<Long> parentIds);

    List<Issue> findBySprint_Id(Long sprintId);

    List<Issue> findDistinctByLabels_Id(Long labelId);

    boolean existsByIssueKey(String issueKey);
}
