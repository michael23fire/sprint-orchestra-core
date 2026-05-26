package com.jiraagentic.app.controller;

import com.jiraagentic.app.dto.*;
import com.jiraagentic.app.service.GroupService;
import lombok.RequiredArgsConstructor;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;

import java.util.List;
import java.util.Map;

@RestController
@RequestMapping("/api/groups")
@RequiredArgsConstructor
public class GroupController {

    private final GroupService groupService;

    @GetMapping
    public List<GroupDto> getAll() {
        return groupService.findAll();
    }

    @GetMapping("/{id}")
    public GroupDto getById(@PathVariable Long id) {
        return groupService.findById(id);
    }

    @PostMapping
    public GroupDto create(@RequestBody CreateGroupRequest req,
                           @RequestParam Long ownerId) {
        return groupService.create(req, ownerId);
    }

    @PutMapping("/{id}")
    public GroupDto update(@PathVariable Long id, @RequestBody CreateGroupRequest req) {
        return groupService.update(id, req);
    }

    @DeleteMapping("/{id}")
    public ResponseEntity<Void> delete(@PathVariable Long id) {
        groupService.delete(id);
        return ResponseEntity.noContent().build();
    }

    @GetMapping("/{id}/members")
    public List<UserDto> getMembers(@PathVariable Long id) {
        return groupService.getMembers(id);
    }

    @PostMapping("/{id}/members")
    public ResponseEntity<Void> addMember(@PathVariable Long id, @RequestBody Map<String, Long> body) {
        groupService.addMember(id, body.get("userId"));
        return ResponseEntity.ok().build();
    }

    @DeleteMapping("/{id}/members/{userId}")
    public ResponseEntity<Void> removeMember(@PathVariable Long id, @PathVariable Long userId) {
        groupService.removeMember(id, userId);
        return ResponseEntity.noContent().build();
    }
}
