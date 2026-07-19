package com.jiraagentic.app.entity;

import jakarta.persistence.*;
import lombok.Data;
import lombok.NoArgsConstructor;
import lombok.AllArgsConstructor;
import java.io.Serializable;
import java.time.Instant;
import java.time.LocalDate;
import java.util.LinkedHashSet;
import java.util.Set;

@Entity
@Table(name = "issues", indexes = {
    @Index(name = "idx_issue_space", columnList = "space_id"),
    @Index(name = "idx_issue_sprint", columnList = "sprint_id"),
    @Index(name = "idx_issue_parent", columnList = "parent_id"),
    @Index(name = "idx_issue_key", columnList = "issue_key", unique = true),
})
@Data
@NoArgsConstructor
@AllArgsConstructor
public class Issue implements Serializable {

    @Id
    @GeneratedValue(strategy = GenerationType.IDENTITY)
    private Long id;

    @Column(name = "issue_key", unique = true, nullable = false, length = 30)
    private String issueKey;

    @ManyToOne(fetch = FetchType.LAZY, optional = false)
    @JoinColumn(name = "space_id", nullable = false)
    private Space space;

    @ManyToOne(fetch = FetchType.LAZY)
    @JoinColumn(name = "sprint_id")
    private Sprint sprint;

    @ManyToOne(fetch = FetchType.LAZY)
    @JoinColumn(name = "parent_id")
    private Issue parent;

    @Column(nullable = false, length = 255)
    private String title;

    @Column(columnDefinition = "TEXT")
    private String description;

    @Column(name = "issue_type", nullable = false, length = 20)
    private String issueType = "task";

    @Column(nullable = false, length = 20)
    private String status = "planned";

    @Column(length = 20)
    private String priority;

    @ManyToOne(fetch = FetchType.LAZY)
    @JoinColumn(name = "assignee_id")
    private User assignee;

    @ManyToOne(fetch = FetchType.LAZY)
    @JoinColumn(name = "reporter_id")
    private User reporter;

    @Column(name = "story_points")
    private Integer storyPoints;

    @Column(name = "start_date")
    private LocalDate startDate;

    @Column(name = "due_date")
    private LocalDate dueDate;

    @Column(name = "issue_order")
    private Integer issueOrder = 0;

    @ManyToMany
    @JoinTable(
            name = "issue_labels",
            joinColumns = @JoinColumn(name = "issue_id"),
            inverseJoinColumns = @JoinColumn(name = "label_id"))
    @OrderBy("name ASC")
    private Set<Label> labels = new LinkedHashSet<>();

    @Column(name = "created_at", nullable = false, updatable = false)
    private Instant createdAt;

    @Column(name = "updated_at", nullable = false)
    private Instant updatedAt;

    @PrePersist
    void onCreate() {
        createdAt = updatedAt = Instant.now();
        if (issueType == null) issueType = "task";
        if (status == null) status = "planned";
        if (issueOrder == null) issueOrder = 0;
    }

    @PreUpdate
    void onUpdate() {
        updatedAt = Instant.now();
    }
}
