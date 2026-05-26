package com.jiraagentic.app.repository;

import com.jiraagentic.app.entity.SpaceMember;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Query;
import org.springframework.data.repository.query.Param;

import java.util.Collection;
import java.util.List;
import java.util.Optional;

public interface SpaceMemberRepository extends JpaRepository<SpaceMember, Long> {

    @Query("SELECT sm FROM SpaceMember sm JOIN FETCH sm.user WHERE sm.space.id IN :spaceIds")
    List<SpaceMember> findWithUserBySpaceIdIn(@Param("spaceIds") Collection<Long> spaceIds);

    List<SpaceMember> findBySpaceId(Long spaceId);

    List<SpaceMember> findByUserId(Long userId);

    Optional<SpaceMember> findBySpaceIdAndUserId(Long spaceId, Long userId);

    boolean existsBySpaceIdAndUserId(Long spaceId, Long userId);

    void deleteBySpaceIdAndUserId(Long spaceId, Long userId);
}
