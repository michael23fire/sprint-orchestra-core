package com.jiraagentic.app.repository;

import com.jiraagentic.app.entity.Sprint;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Query;
import org.springframework.data.repository.query.Param;

import java.util.List;
import java.util.Optional;

public interface SprintRepository extends JpaRepository<Sprint, Long> {

    List<Sprint> findBySpaceIdOrderByStartDateAsc(Long spaceId);

    List<Sprint> findBySpaceIdAndStatusOrderByIdAsc(Long spaceId, String status);

    List<Sprint> findBySpaceIdAndStatusOrderBySprintOrderAscIdAsc(Long spaceId, String status);

    @Query("SELECT COALESCE(MAX(s.sprintOrder), -1) FROM Sprint s WHERE s.space.id = :spaceId AND s.status = :status")
    Optional<Integer> findMaxSprintOrderBySpaceIdAndStatus(@Param("spaceId") Long spaceId, @Param("status") String status);
}
