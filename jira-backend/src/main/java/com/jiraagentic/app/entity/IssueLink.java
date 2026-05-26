package com.jiraagentic.app.entity;

import jakarta.persistence.*;
import lombok.AllArgsConstructor;
import lombok.Data;
import lombok.NoArgsConstructor;

import java.io.Serializable;
import java.time.Instant;

@Entity
@Table(name = "issue_links", indexes = {
        @Index(name = "idx_issue_links_source", columnList = "source_issue_id"),
        @Index(name = "idx_issue_links_target", columnList = "target_issue_id"),
}, uniqueConstraints = {
        @UniqueConstraint(name = "uk_issue_links_source_target_type", columnNames = {"source_issue_id", "target_issue_id", "link_type"})
})
@Data
@NoArgsConstructor
@AllArgsConstructor
public class IssueLink implements Serializable {

    @Id
    @GeneratedValue(strategy = GenerationType.IDENTITY)
    private Long id;

    @ManyToOne(fetch = FetchType.LAZY, optional = false)
    @JoinColumn(name = "source_issue_id", nullable = false)
    private Issue sourceIssue;

    @ManyToOne(fetch = FetchType.LAZY, optional = false)
    @JoinColumn(name = "target_issue_id", nullable = false)
    private Issue targetIssue;

    @Column(name = "link_type", nullable = false, length = 30)
    private String linkType;

    @Column(name = "created_at", nullable = false, updatable = false)
    private Instant createdAt;

    @PrePersist
    void onCreate() {
        createdAt = Instant.now();
    }
}
