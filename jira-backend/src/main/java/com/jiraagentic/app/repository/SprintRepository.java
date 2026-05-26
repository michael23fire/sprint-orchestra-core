package com.jiraagentic.app.repository;

import com.jiraagentic.app.entity.Sprint;
import org.springframework.data.jpa.repository.JpaRepository;

import java.util.List;

public interface SprintRepository extends JpaRepository<Sprint, Long> {

    List<Sprint> findBySpaceIdOrderByStartDateAsc(Long spaceId);
}
