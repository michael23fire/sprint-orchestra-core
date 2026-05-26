package com.jiraagentic.app.repository;

import com.jiraagentic.app.entity.IssueAttachment;
import org.springframework.data.jpa.repository.JpaRepository;

import java.util.List;

public interface IssueAttachmentRepository extends JpaRepository<IssueAttachment, Long> {
    List<IssueAttachment> findByIssueIdOrderByCreatedAtDesc(Long issueId);
}
