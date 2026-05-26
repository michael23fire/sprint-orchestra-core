package com.jiraagentic.app.repository;

import com.jiraagentic.app.entity.Space;
import org.springframework.data.jpa.repository.JpaRepository;

import java.util.Collection;
import java.util.List;
import java.util.Optional;

public interface SpaceRepository extends JpaRepository<Space, Long> {

    Optional<Space> findByIdAndDeletedAtIsNull(Long id);

    boolean existsByIdAndDeletedAtIsNull(Long id);

    List<Space> findAllByDeletedAtIsNullOrderByIdAsc();

    List<Space> findByOwnerIdAndDeletedAtIsNull(Long ownerId);

    List<Space> findAllByIdInAndDeletedAtIsNull(Collection<Long> ids);

    Optional<Space> findByKeyAndDeletedAtIsNull(String key);

    boolean existsByKeyAndDeletedAtIsNull(String key);
}
