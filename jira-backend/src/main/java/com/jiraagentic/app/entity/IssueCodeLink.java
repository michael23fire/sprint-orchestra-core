package com.jiraagentic.app.entity;

import jakarta.persistence.*;
import lombok.AllArgsConstructor;
import lombok.Data;
import lombok.NoArgsConstructor;

import java.io.Serializable;
import java.time.Instant;

@Entity
@Table(name = "issue_code_links", indexes = {
        @Index(name = "idx_issue_code_links_issue", columnList = "issue_id"),
        @Index(name = "idx_issue_code_links_kind", columnList = "kind"),
}, uniqueConstraints = {
        @UniqueConstraint(name = "uk_issue_code_links_issue_url", columnNames = {"issue_id", "url"})
})
@Data
@NoArgsConstructor
@AllArgsConstructor
public class IssueCodeLink implements Serializable {

    @Id
    @GeneratedValue(strategy = GenerationType.IDENTITY)
    private Long id;

    @ManyToOne(fetch = FetchType.LAZY, optional = false)
    @JoinColumn(name = "issue_id", nullable = false)
    private Issue issue;

    @ManyToOne(fetch = FetchType.LAZY)
    @JoinColumn(name = "creator_id")
    private User creator;

    @Column(name = "url", nullable = false, length = 1024)
    private String url;

    /** pull_request | commit | branch | repo | other */
    @Column(name = "kind", nullable = false, length = 30)
    private String kind;

    @Column(name = "provider", nullable = false, length = 30)
    private String provider = "github";

    @Column(name = "owner", length = 120)
    private String owner;

    @Column(name = "repo", length = 200)
    private String repo;

    /** PR number, branch name, or short commit SHA. */
    @Column(name = "ref_id", length = 200)
    private String refId;

    @Column(name = "title", length = 500)
    private String title;

    @Column(name = "state", length = 40)
    private String state;

    @Column(name = "author_login", length = 120)
    private String authorLogin;

    @Column(name = "created_at", nullable = false, updatable = false)
    private Instant createdAt;

    /** GitHub-side activity time when known (PR updated_at, commit date, …). */
    @Column(name = "last_activity_at")
    private Instant lastActivityAt;

    @PrePersist
    void onCreate() {
        if (createdAt == null) createdAt = Instant.now();
        if (provider == null) provider = "github";
    }
}
