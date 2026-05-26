package com.jiraagentic.app.service;

import com.jiraagentic.app.dto.*;
import com.jiraagentic.app.entity.GroupMember;
import com.jiraagentic.app.entity.User;
import com.jiraagentic.app.entity.UserGroup;
import com.jiraagentic.app.repository.GroupMemberRepository;
import com.jiraagentic.app.repository.UserGroupRepository;
import com.jiraagentic.app.repository.UserRepository;
import lombok.RequiredArgsConstructor;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.stream.Collectors;

@Service
@RequiredArgsConstructor
public class GroupService {

    private final UserGroupRepository groupRepository;
    private final GroupMemberRepository groupMemberRepository;
    private final UserRepository userRepository;

    public List<GroupDto> findAll() {
        List<UserGroup> groups = groupRepository.findAllByOrderByIdAsc();
        if (groups.isEmpty()) {
            return List.of();
        }
        List<Long> ids = groups.stream().map(UserGroup::getId).collect(Collectors.toList());
        Map<Long, List<UserDto>> membersByGroup = groupMemberRepository.findWithUserByGroupIdIn(ids).stream()
                .collect(Collectors.groupingBy(
                        gm -> gm.getGroup().getId(),
                        LinkedHashMap::new,
                        Collectors.mapping(gm -> UserDto.from(gm.getUser()), Collectors.toList())));
        return groups.stream()
                .map(g -> {
                    GroupDto dto = GroupDto.from(g);
                    dto.setMembers(membersByGroup.getOrDefault(g.getId(), List.of()));
                    return dto;
                })
                .collect(Collectors.toList());
    }

    public GroupDto findById(Long id) {
        UserGroup group = groupRepository.findById(id)
                .orElseThrow(() -> new RuntimeException("Group not found: " + id));
        return toDto(group);
    }

    @Transactional
    public GroupDto create(CreateGroupRequest req, Long ownerId) {
        User owner = userRepository.findById(ownerId)
                .orElseThrow(() -> new RuntimeException("User not found: " + ownerId));

        UserGroup group = new UserGroup();
        group.setName(req.getName());
        group.setDescription(req.getDescription());
        group.setOwner(owner);
        group = groupRepository.save(group);

        GroupMember ownerMember = new GroupMember();
        ownerMember.setGroup(group);
        ownerMember.setUser(owner);
        ownerMember.setRole("ADMIN");
        groupMemberRepository.save(ownerMember);

        return toDto(group);
    }

    @Transactional
    public GroupDto update(Long id, CreateGroupRequest req) {
        UserGroup group = groupRepository.findById(id)
                .orElseThrow(() -> new RuntimeException("Group not found: " + id));
        if (req.getName() != null) group.setName(req.getName());
        if (req.getDescription() != null) group.setDescription(req.getDescription());
        return toDto(groupRepository.save(group));
    }

    @Transactional
    public void delete(Long id) {
        if (!groupRepository.existsById(id)) {
            throw new RuntimeException("Group not found: " + id);
        }
        groupRepository.deleteById(id);
    }

    public List<UserDto> getMembers(Long groupId) {
        return groupMemberRepository.findByGroupId(groupId).stream()
                .map(gm -> UserDto.from(gm.getUser()))
                .collect(Collectors.toList());
    }

    @Transactional
    public void addMember(Long groupId, Long userId) {
        if (groupMemberRepository.existsByGroupIdAndUserId(groupId, userId)) {
            return;
        }
        UserGroup group = groupRepository.findById(groupId)
                .orElseThrow(() -> new RuntimeException("Group not found: " + groupId));
        User user = userRepository.findById(userId)
                .orElseThrow(() -> new RuntimeException("User not found: " + userId));

        GroupMember gm = new GroupMember();
        gm.setGroup(group);
        gm.setUser(user);
        gm.setRole("MEMBER");
        groupMemberRepository.save(gm);
    }

    @Transactional
    public void removeMember(Long groupId, Long userId) {
        groupMemberRepository.deleteByGroupIdAndUserId(groupId, userId);
    }

    private GroupDto toDto(UserGroup group) {
        GroupDto dto = GroupDto.from(group);
        List<UserDto> members = groupMemberRepository.findByGroupId(group.getId()).stream()
                .map(gm -> UserDto.from(gm.getUser()))
                .collect(Collectors.toList());
        dto.setMembers(members);
        return dto;
    }
}
