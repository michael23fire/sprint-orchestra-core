package com.jiraagentic.app.entity;

import jakarta.persistence.*;
import lombok.Data;
import lombok.NoArgsConstructor;
import lombok.AllArgsConstructor;
import java.io.Serializable;
import java.time.Instant;
import java.time.LocalDate;

@Entity
@Table(name = "sprints")
@Data
@NoArgsConstructor
@AllArgsConstructor
public class Sprint implements Serializable {

    @Id
    @GeneratedValue(strategy = GenerationType.IDENTITY)
    private Long id;

    @ManyToOne(fetch = FetchType.LAZY, optional = false)
    @JoinColumn(name = "space_id", nullable = false)
    private Space space;

    @Column(nullable = false, length = 100)
    private String name;

    @Column(columnDefinition = "TEXT")
    private String goal;

    @Column(name = "start_date")
    private LocalDate startDate;

    @Column(name = "end_date")
    private LocalDate endDate;

    @Column(nullable = false, length = 20)
    private String status = "future";

    /** Relative order within a space (used for future-sprint reordering on the backlog). */
    @Column(name = "sprint_order", nullable = false)
    private Integer sprintOrder = 0;

    @Column(name = "initial_committed_points")
    private Integer initialCommittedPoints;

    @Column(name = "initial_completed_points")
    private Integer initialCompletedPoints;

    @Column(name = "final_scope_points")
    private Integer finalScopePoints;

    @Column(name = "completed_points")
    private Integer completedPoints;

    @Column(name = "initial_issue_count")
    private Integer initialIssueCount;

    @Column(name = "completed_issue_count")
    private Integer completedIssueCount;

    @Column(name = "final_issue_count")
    private Integer finalIssueCount;

    @Column(name = "unestimated_issue_count")
    private Integer unestimatedIssueCount;

    @Column(name = "created_at", nullable = false, updatable = false)
    private Instant createdAt;

    @Column(name = "updated_at", nullable = false)
    private Instant updatedAt;

    @PrePersist
    void onCreate() {
        createdAt = updatedAt = Instant.now();
        if (status == null) status = "future";
        if (sprintOrder == null) sprintOrder = 0;
    }

    @PreUpdate
    void onUpdate() {
        updatedAt = Instant.now();
    }
}
