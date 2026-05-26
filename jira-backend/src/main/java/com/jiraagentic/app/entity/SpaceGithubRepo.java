package com.jiraagentic.app.entity;

import jakarta.persistence.*;
import lombok.AllArgsConstructor;
import lombok.Data;
import lombok.NoArgsConstructor;

import java.io.Serializable;
import java.time.Instant;

@Entity
@Table(name = "space_github_repos", indexes = {
        @Index(name = "idx_space_github_repos_space", columnList = "space_id"),
}, uniqueConstraints = {
        @UniqueConstraint(name = "uk_space_github_repos", columnNames = {"space_id", "owner", "repo"})
})
@Data
@NoArgsConstructor
@AllArgsConstructor
public class SpaceGithubRepo implements Serializable {

    @Id
    @GeneratedValue(strategy = GenerationType.IDENTITY)
    private Long id;

    @ManyToOne(fetch = FetchType.LAZY, optional = false)
    @JoinColumn(name = "space_id", nullable = false)
    private Space space;

    @Column(name = "owner", nullable = false, length = 120)
    private String owner;

    @Column(name = "repo", nullable = false, length = 200)
    private String repo;

    @Column(name = "created_at", nullable = false, updatable = false)
    private Instant createdAt;

    @Column(name = "last_scanned_at")
    private Instant lastScannedAt;

    @PrePersist
    void onCreate() {
        if (createdAt == null) createdAt = Instant.now();
    }
}
