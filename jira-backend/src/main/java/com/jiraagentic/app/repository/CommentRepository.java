package com.jiraagentic.app.repository;

import com.jiraagentic.app.entity.Comment;
import org.springframework.data.jpa.repository.JpaRepository;

import java.util.List;

public interface CommentRepository extends JpaRepository<Comment, Long> {

    List<Comment> findByIssueIdOrderByCreatedAtAsc(Long issueId);

    void deleteByIssue_Id(Long issueId);
}
