package com.jiraagentic.app.repository;

import com.jiraagentic.app.entity.GroupMember;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Query;
import org.springframework.data.repository.query.Param;

import java.util.Collection;
import java.util.List;

public interface GroupMemberRepository extends JpaRepository<GroupMember, Long> {

    @Query("SELECT gm FROM GroupMember gm JOIN FETCH gm.user WHERE gm.group.id IN :groupIds")
    List<GroupMember> findWithUserByGroupIdIn(@Param("groupIds") Collection<Long> groupIds);

    List<GroupMember> findByGroupId(Long groupId);

    boolean existsByGroupIdAndUserId(Long groupId, Long userId);

    void deleteByGroupIdAndUserId(Long groupId, Long userId);

    @Query("SELECT gm.group.id FROM GroupMember gm WHERE gm.user.id = :userId")
    List<Long> findGroupIdsByUserId(Long userId);
}
