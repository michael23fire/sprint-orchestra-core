package com.jiraagentic.app.entity;

import jakarta.persistence.*;
import lombok.AllArgsConstructor;
import lombok.Data;
import lombok.EqualsAndHashCode;
import lombok.NoArgsConstructor;
import lombok.ToString;

import java.io.Serializable;
import java.time.Instant;

@Entity
@Table(name = "labels", uniqueConstraints = {
        @UniqueConstraint(name = "uk_labels_space_normalized", columnNames = {"space_id", "normalized_name"})
})
@Data
@EqualsAndHashCode(onlyExplicitlyIncluded = true)
@ToString(exclude = "space")
@NoArgsConstructor
@AllArgsConstructor
public class Label implements Serializable {

    @Id
    @GeneratedValue(strategy = GenerationType.IDENTITY)
    @EqualsAndHashCode.Include
    private Long id;

    @ManyToOne(fetch = FetchType.LAZY, optional = false)
    @JoinColumn(name = "space_id", nullable = false)
    private Space space;

    @Column(nullable = false, length = 50)
    private String name;

    @Column(name = "normalized_name", nullable = false, length = 50)
    private String normalizedName;

    @Column(name = "created_at", nullable = false, updatable = false)
    private Instant createdAt;

    @Column(name = "deleted_at")
    private Instant deletedAt;

    @PrePersist
    void onCreate() {
        if (createdAt == null) createdAt = Instant.now();
    }
}
