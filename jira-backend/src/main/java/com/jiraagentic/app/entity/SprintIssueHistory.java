package com.jiraagentic.app.entity;

import jakarta.persistence.*;
import lombok.AllArgsConstructor;
import lombok.Data;
import lombok.NoArgsConstructor;

import java.io.Serializable;
import java.time.Instant;

@Entity
@Table(name = "sprint_issue_history", uniqueConstraints = {
        @UniqueConstraint(name = "uk_sprint_history_sprint_issue", columnNames = {"sprint_id", "issue_id"})
})
@Data
@NoArgsConstructor
@AllArgsConstructor
public class SprintIssueHistory implements Serializable {

    @Id
    @GeneratedValue(strategy = GenerationType.IDENTITY)
    private Long id;

    @ManyToOne(fetch = FetchType.LAZY, optional = false)
    @JoinColumn(name = "sprint_id", nullable = false)
    private Sprint sprint;

    @Column(name = "issue_id", nullable = false)
    private Long issueId;

    @Column(name = "issue_key", nullable = false, length = 30)
    private String issueKey;

    @Column(name = "issue_type", nullable = false, length = 20)
    private String issueType;

    @Column(name = "initial_scope", nullable = false)
    private boolean initialScope;

    @Column(name = "points_at_start")
    private Integer pointsAtStart;

    @Column(name = "points_at_end")
    private Integer pointsAtEnd;

    @Column(name = "status_at_end", length = 20)
    private String statusAtEnd;

    @Column(length = 20)
    private String outcome;

    @Column(name = "added_at", nullable = false, updatable = false)
    private Instant addedAt;

    @Column(name = "removed_at")
    private Instant removedAt;

    @PrePersist
    void onCreate() {
        if (addedAt == null) addedAt = Instant.now();
    }
}
