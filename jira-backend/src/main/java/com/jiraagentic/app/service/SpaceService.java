package com.jiraagentic.app.service;

import com.jiraagentic.app.dto.*;
import com.jiraagentic.app.entity.*;
import com.jiraagentic.app.repository.*;
import lombok.RequiredArgsConstructor;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.time.Instant;
import java.util.*;
import java.util.stream.Collectors;

@Service
@RequiredArgsConstructor
public class SpaceService {

    private final SpaceRepository spaceRepository;
    private final SpaceMemberRepository spaceMemberRepository;
    private final SpaceGroupRepository spaceGroupRepository;
    private final GroupMemberRepository groupMemberRepository;
    private final UserGroupRepository userGroupRepository;
    private final UserRepository userRepository;

    /** List spaces for {@code GET /api/spaces}: optional {@code userId} narrows to spaces the user can see. */
    public List<SpaceDto> listSpaces(Long userId) {
        if (userId != null) {
            return findByUser(userId);
        }
        return findAll();
    }

    public List<SpaceDto> findAll() {
        List<Space> spaces = spaceRepository.findAllByDeletedAtIsNullOrderByIdAsc();
        if (spaces.isEmpty()) {
            return List.of();
        }
        Map<Long, List<UserDto>> membersBySpace = membersBySpaceId(
                spaces.stream().map(Space::getId).collect(Collectors.toList()));
        return spaces.stream()
                .map(s -> toListDto(s, membersBySpace))
                .collect(Collectors.toList());
    }

    public List<SpaceDto> findByUser(Long userId) {
        Set<Long> visibleSpaceIds = new LinkedHashSet<>();

        // Space owner should always see their own spaces.
        spaceRepository.findByOwnerIdAndDeletedAtIsNull(userId)
                .forEach(s -> visibleSpaceIds.add(s.getId()));

        spaceMemberRepository.findByUserId(userId)
                .forEach(sm -> visibleSpaceIds.add(sm.getSpace().getId()));

        List<Long> groupIds = groupMemberRepository.findGroupIdsByUserId(userId);
        if (!groupIds.isEmpty()) {
            visibleSpaceIds.addAll(spaceGroupRepository.findSpaceIdsByGroupIds(groupIds));
        }

        if (visibleSpaceIds.isEmpty()) return List.of();

        List<Space> spaces = spaceRepository.findAllByIdInAndDeletedAtIsNull(visibleSpaceIds).stream()
                .sorted(Comparator.comparingLong(Space::getId))
                .collect(Collectors.toList());
        Map<Long, List<UserDto>> membersBySpace = membersBySpaceId(
                spaces.stream().map(Space::getId).collect(Collectors.toList()));
        return spaces.stream()
                .map(s -> toListDto(s, membersBySpace))
                .collect(Collectors.toList());
    }

    public SpaceDto findById(Long id) {
        Space space = spaceRepository.findByIdAndDeletedAtIsNull(id)
                .orElseThrow(() -> new RuntimeException("Space not found: " + id));
        return toDto(space);
    }

    @Transactional
    public SpaceDto create(CreateSpaceRequest req, Long ownerId) {
        if (spaceRepository.existsByKeyAndDeletedAtIsNull(req.getKey().toUpperCase())) {
            throw new RuntimeException("Space key already exists: " + req.getKey());
        }

        User owner = userRepository.findById(ownerId)
                .orElseThrow(() -> new RuntimeException("User not found: " + ownerId));

        Space space = new Space();
        space.setName(req.getName());
        space.setKey(req.getKey().toUpperCase());
        space.setColor(req.getColor());
        space.setOwner(owner);
        space = spaceRepository.save(space);

        SpaceMember ownerMember = new SpaceMember();
        ownerMember.setSpace(space);
        ownerMember.setUser(owner);
        ownerMember.setRole("ADMIN");
        spaceMemberRepository.save(ownerMember);

        return toDto(space);
    }

    @Transactional
    public SpaceDto update(Long id, CreateSpaceRequest req) {
        Space space = spaceRepository.findByIdAndDeletedAtIsNull(id)
                .orElseThrow(() -> new RuntimeException("Space not found: " + id));
        if (req.getName() != null) space.setName(req.getName());
        if (req.getColor() != null) space.setColor(req.getColor());
        return toDto(spaceRepository.save(space));
    }

    @Transactional
    public void delete(Long id) {
        Space space = spaceRepository.findById(id)
                .orElseThrow(() -> new RuntimeException("Space not found: " + id));
        if (space.getDeletedAt() != null) {
            return;
        }
        space.setDeletedAt(Instant.now());
        spaceRepository.save(space);
    }

    public List<UserDto> getMembers(Long spaceId) {
        spaceRepository.findByIdAndDeletedAtIsNull(spaceId)
                .orElseThrow(() -> new RuntimeException("Space not found: " + spaceId));
        return spaceMemberRepository.findBySpaceId(spaceId).stream()
                .map(sm -> UserDto.from(sm.getUser()))
                .collect(Collectors.toList());
    }

    @Transactional
    public void addMember(Long spaceId, AddMemberRequest req) {
        if (spaceMemberRepository.existsBySpaceIdAndUserId(spaceId, req.getUserId())) {
            return;
        }
        Space space = spaceRepository.findByIdAndDeletedAtIsNull(spaceId)
                .orElseThrow(() -> new RuntimeException("Space not found: " + spaceId));
        User user = userRepository.findById(req.getUserId())
                .orElseThrow(() -> new RuntimeException("User not found: " + req.getUserId()));

        SpaceMember sm = new SpaceMember();
        sm.setSpace(space);
        sm.setUser(user);
        sm.setRole(req.getRole() != null ? req.getRole() : "MEMBER");
        spaceMemberRepository.save(sm);
    }

    @Transactional
    public void removeMember(Long spaceId, Long userId) {
        Space space = spaceRepository.findByIdAndDeletedAtIsNull(spaceId)
                .orElseThrow(() -> new RuntimeException("Space not found: " + spaceId));
        if (space.getOwner() != null && Objects.equals(space.getOwner().getId(), userId)) {
            throw new RuntimeException("Space admin cannot remove themselves");
        }
        spaceMemberRepository.deleteBySpaceIdAndUserId(spaceId, userId);
    }

    public List<GroupDto> getSpaceGroups(Long spaceId) {
        spaceRepository.findByIdAndDeletedAtIsNull(spaceId)
                .orElseThrow(() -> new RuntimeException("Space not found: " + spaceId));
        return spaceGroupRepository.findBySpaceId(spaceId).stream()
                .map(sg -> {
                    GroupDto dto = GroupDto.from(sg.getGroup());
                    dto.setMembers(
                        groupMemberRepository.findByGroupId(sg.getGroup().getId()).stream()
                            .map(gm -> UserDto.from(gm.getUser()))
                            .collect(Collectors.toList())
                    );
                    return dto;
                })
                .collect(Collectors.toList());
    }

    @Transactional
    public void addGroup(Long spaceId, Long groupId) {
        if (spaceGroupRepository.existsBySpaceIdAndGroupId(spaceId, groupId)) {
            return;
        }
        Space space = spaceRepository.findByIdAndDeletedAtIsNull(spaceId)
                .orElseThrow(() -> new RuntimeException("Space not found: " + spaceId));
        UserGroup group = userGroupRepository.findById(groupId)
                .orElseThrow(() -> new RuntimeException("Group not found: " + groupId));

        SpaceGroup sg = new SpaceGroup();
        sg.setSpace(space);
        sg.setGroup(group);
        spaceGroupRepository.save(sg);
    }

    @Transactional
    public void removeGroup(Long spaceId, Long groupId) {
        spaceRepository.findByIdAndDeletedAtIsNull(spaceId)
                .orElseThrow(() -> new RuntimeException("Space not found: " + spaceId));
        spaceGroupRepository.deleteBySpaceIdAndGroupId(spaceId, groupId);
    }

    public Set<Long> getEffectiveUserIds(Long spaceId) {
        spaceRepository.findByIdAndDeletedAtIsNull(spaceId)
                .orElseThrow(() -> new RuntimeException("Space not found: " + spaceId));
        Set<Long> userIds = new LinkedHashSet<>();
        spaceMemberRepository.findBySpaceId(spaceId).forEach(sm -> userIds.add(sm.getUser().getId()));
        spaceGroupRepository.findBySpaceId(spaceId).forEach(sg ->
            groupMemberRepository.findByGroupId(sg.getGroup().getId())
                .forEach(gm -> userIds.add(gm.getUser().getId()))
        );
        return userIds;
    }

    private Map<Long, List<UserDto>> membersBySpaceId(List<Long> spaceIds) {
        if (spaceIds == null || spaceIds.isEmpty()) {
            return Map.of();
        }
        return spaceMemberRepository.findWithUserBySpaceIdIn(spaceIds).stream()
                .collect(Collectors.groupingBy(
                        sm -> sm.getSpace().getId(),
                        LinkedHashMap::new,
                        Collectors.mapping(sm -> UserDto.from(sm.getUser()), Collectors.toList())));
    }

    /** List endpoints: space row + direct members only (one batched member query). Groups omitted until {@link #findById}. */
    private SpaceDto toListDto(Space space, Map<Long, List<UserDto>> membersBySpace) {
        SpaceDto dto = SpaceDto.from(space);
        dto.setMembers(membersBySpace.getOrDefault(space.getId(), List.of()));
        dto.setGroups(List.of());
        return dto;
    }

    private SpaceDto toDto(Space space) {
        SpaceDto dto = SpaceDto.from(space);
        List<UserDto> members = spaceMemberRepository.findBySpaceId(space.getId()).stream()
                .map(sm -> UserDto.from(sm.getUser()))
                .collect(Collectors.toList());
        dto.setMembers(members);

        List<GroupDto> groups = spaceGroupRepository.findBySpaceId(space.getId()).stream()
                .map(sg -> {
                    GroupDto g = GroupDto.from(sg.getGroup());
                    g.setMembers(
                        groupMemberRepository.findByGroupId(sg.getGroup().getId()).stream()
                            .map(gm -> UserDto.from(gm.getUser()))
                            .collect(Collectors.toList())
                    );
                    return g;
                })
                .collect(Collectors.toList());
        dto.setGroups(groups);

        return dto;
    }
}
