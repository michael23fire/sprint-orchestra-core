package com.jiraagentic.app.repository;

import com.jiraagentic.app.entity.UserGroup;
import org.springframework.data.jpa.repository.JpaRepository;

import java.util.List;

public interface UserGroupRepository extends JpaRepository<UserGroup, Long> {

    List<UserGroup> findAllByOrderByIdAsc();
}
