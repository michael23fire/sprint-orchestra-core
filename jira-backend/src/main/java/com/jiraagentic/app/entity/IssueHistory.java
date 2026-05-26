package com.jiraagentic.app.entity;

import jakarta.persistence.*;
import lombok.AllArgsConstructor;
import lombok.Data;
import lombok.NoArgsConstructor;

import java.io.Serializable;
import java.time.Instant;

@Entity
@Table(name = "issue_history", indexes = {
        @Index(name = "idx_issue_history_issue", columnList = "issue_id"),
        @Index(name = "idx_issue_history_created", columnList = "created_at"),
})
@Data
@NoArgsConstructor
@AllArgsConstructor
public class IssueHistory implements Serializable {

    @Id
    @GeneratedValue(strategy = GenerationType.IDENTITY)
    private Long id;

    @ManyToOne(fetch = FetchType.LAZY, optional = false)
    @JoinColumn(name = "issue_id", nullable = false)
    private Issue issue;

    @ManyToOne(fetch = FetchType.LAZY)
    @JoinColumn(name = "actor_id")
    private User actor;

    @Column(name = "event_type", nullable = false, length = 30)
    private String eventType;

    @Column(name = "field_name", length = 50)
    private String fieldName;

    @Column(name = "from_value", columnDefinition = "TEXT")
    private String fromValue;

    @Column(name = "to_value", columnDefinition = "TEXT")
    private String toValue;

    @Column(columnDefinition = "TEXT")
    private String description;

    @Column(name = "created_at", nullable = false, updatable = false)
    private Instant createdAt;

    @PrePersist
    void onCreate() {
        createdAt = Instant.now();
    }
}
