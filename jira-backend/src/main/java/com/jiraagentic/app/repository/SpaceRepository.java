package com.jiraagentic.app.repository;

import com.jiraagentic.app.entity.Space;
import jakarta.persistence.LockModeType;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Lock;
import org.springframework.data.jpa.repository.Query;
import org.springframework.data.repository.query.Param;

import java.util.Collection;
import java.util.List;
import java.util.Optional;

public interface SpaceRepository extends JpaRepository<Space, Long> {

    Optional<Space> findByIdAndDeletedAtIsNull(Long id);

    @Lock(LockModeType.PESSIMISTIC_WRITE)
    @Query("select s from Space s where s.id = :id and s.deletedAt is null")
    Optional<Space> findActiveByIdForUpdate(@Param("id") Long id);

    boolean existsByIdAndDeletedAtIsNull(Long id);

    List<Space> findAllByDeletedAtIsNullOrderByIdAsc();

    List<Space> findByOwnerIdAndDeletedAtIsNull(Long ownerId);

    List<Space> findAllByIdInAndDeletedAtIsNull(Collection<Long> ids);

    Optional<Space> findByKeyAndDeletedAtIsNull(String key);

    boolean existsByKeyAndDeletedAtIsNull(String key);
}
