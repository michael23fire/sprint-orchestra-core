package com.jiraagentic.app.controller;

import com.jiraagentic.app.dto.*;
import com.jiraagentic.app.service.SpaceService;
import lombok.RequiredArgsConstructor;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;

import java.util.List;
import java.util.Map;

@RestController
@RequestMapping("/api/spaces")
@RequiredArgsConstructor
public class SpaceController {

    private final SpaceService spaceService;

    @GetMapping
    public List<SpaceDto> getAll(@RequestParam(required = false) Long userId) {
        return spaceService.listSpaces(userId);
    }

    @GetMapping("/{id}")
    public SpaceDto getById(@PathVariable Long id) {
        return spaceService.findById(id);
    }

    @PostMapping
    public SpaceDto create(@RequestBody CreateSpaceRequest req,
                           @RequestParam Long ownerId) {
        return spaceService.create(req, ownerId);
    }

    @PutMapping("/{id}")
    public SpaceDto update(@PathVariable Long id, @RequestBody CreateSpaceRequest req) {
        return spaceService.update(id, req);
    }

    @DeleteMapping("/{id}")
    public ResponseEntity<Void> delete(@PathVariable Long id) {
        spaceService.delete(id);
        return ResponseEntity.noContent().build();
    }

    // ── Individual members ──

    @GetMapping("/{id}/members")
    public List<UserDto> getMembers(@PathVariable Long id) {
        return spaceService.getMembers(id);
    }

    @PostMapping("/{id}/members")
    public ResponseEntity<Void> addMember(@PathVariable Long id, @RequestBody AddMemberRequest req) {
        spaceService.addMember(id, req);
        return ResponseEntity.ok().build();
    }

    @DeleteMapping("/{id}/members/{userId}")
    public ResponseEntity<Void> removeMember(@PathVariable Long id, @PathVariable Long userId) {
        spaceService.removeMember(id, userId);
        return ResponseEntity.noContent().build();
    }

    // ── Group members ──

    @GetMapping("/{id}/groups")
    public List<GroupDto> getSpaceGroups(@PathVariable Long id) {
        return spaceService.getSpaceGroups(id);
    }

    @PostMapping("/{id}/groups")
    public ResponseEntity<Void> addGroup(@PathVariable Long id, @RequestBody Map<String, Long> body) {
        spaceService.addGroup(id, body.get("groupId"));
        return ResponseEntity.ok().build();
    }

    @DeleteMapping("/{id}/groups/{groupId}")
    public ResponseEntity<Void> removeGroup(@PathVariable Long id, @PathVariable Long groupId) {
        spaceService.removeGroup(id, groupId);
        return ResponseEntity.noContent().build();
    }
}
