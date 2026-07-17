package com.jiraagentic.app.controller;

import com.jiraagentic.app.dto.CompleteSprintRequest;
import com.jiraagentic.app.dto.CreateSprintRequest;
import com.jiraagentic.app.dto.ReorderSprintRequest;
import com.jiraagentic.app.dto.SprintDto;
import com.jiraagentic.app.service.SprintService;
import lombok.RequiredArgsConstructor;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;

import java.util.List;

@RestController
@RequestMapping("/api/spaces/{spaceId}/sprints")
@RequiredArgsConstructor
public class SprintController {

    private final SprintService sprintService;

    @GetMapping
    public List<SprintDto> getBySpace(@PathVariable Long spaceId) {
        return sprintService.findBySpace(spaceId);
    }

    @GetMapping("/{id}")
    public SprintDto getById(@PathVariable Long id) {
        return sprintService.findById(id);
    }

    @PostMapping
    public SprintDto create(@PathVariable Long spaceId, @RequestBody CreateSprintRequest req) {
        return sprintService.create(spaceId, req);
    }

    @PutMapping("/{id}")
    public SprintDto update(@PathVariable Long id, @RequestBody CreateSprintRequest req) {
        return sprintService.update(id, req);
    }

    @PostMapping("/{id}/complete")
    public SprintDto complete(@PathVariable Long id, @RequestBody(required = false) CompleteSprintRequest req) {
        return sprintService.complete(id, req != null ? req : new CompleteSprintRequest());
    }

    @PostMapping("/{id}/reorder")
    public List<SprintDto> reorder(
            @PathVariable Long spaceId,
            @PathVariable Long id,
            @RequestBody ReorderSprintRequest req) {
        return sprintService.reorder(spaceId, id, req);
    }

    @DeleteMapping("/{id}")
    public ResponseEntity<Void> delete(@PathVariable Long id) {
        sprintService.delete(id);
        return ResponseEntity.noContent().build();
    }
}
