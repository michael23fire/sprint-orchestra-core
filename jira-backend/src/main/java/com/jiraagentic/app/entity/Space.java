package com.jiraagentic.app.entity;

import jakarta.persistence.*;
import lombok.Data;
import lombok.NoArgsConstructor;
import lombok.AllArgsConstructor;
import java.io.Serializable;
import java.time.Instant;

@Entity
@Table(name = "spaces")
@Data
@NoArgsConstructor
@AllArgsConstructor
public class Space implements Serializable {

    @Id
    @GeneratedValue(strategy = GenerationType.IDENTITY)
    private Long id;

    @Column(nullable = false, length = 100)
    private String name;

    @Column(name = "space_key", unique = true, nullable = false, length = 20)
    private String key;

    @Column(length = 30)
    private String color;

    @ManyToOne(fetch = FetchType.LAZY)
    @JoinColumn(name = "owner_id")
    private User owner;

    @Column(name = "issue_counter", nullable = false)
    private Integer issueCounter = 0;

    @Column(name = "created_at", nullable = false, updatable = false)
    private Instant createdAt;

    @Column(name = "updated_at", nullable = false)
    private Instant updatedAt;

    /**
     * Optional GitHub PAT for API calls scoped to this space (scan, refresh, link metadata).
     * Set when bulk-importing with {@code githubToken}; not exposed on {@link com.jiraagentic.app.dto.SpaceDto}.
     */
    @Column(name = "github_pat", columnDefinition = "TEXT")
    private String githubPat;

    /** When set, the space is soft-deleted and hidden from normal queries. */
    @Column(name = "deleted_at")
    private Instant deletedAt;

    @PrePersist
    void onCreate() {
        createdAt = updatedAt = Instant.now();
        if (issueCounter == null) issueCounter = 0;
    }

    @PreUpdate
    void onUpdate() {
        updatedAt = Instant.now();
    }

    public int nextIssueNumber() {
        issueCounter++;
        return issueCounter;
    }
}
