package com.jiraagentic.app.repository;

import com.jiraagentic.app.entity.Label;
import org.springframework.data.jpa.repository.JpaRepository;

import java.util.List;
import java.util.Optional;

public interface LabelRepository extends JpaRepository<Label, Long> {
    List<Label> findBySpace_IdAndDeletedAtIsNullOrderByNameAsc(Long spaceId);
    Optional<Label> findBySpace_IdAndNormalizedName(Long spaceId, String normalizedName);
}
