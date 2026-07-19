package com.jiraagentic.app.controller;

import com.jiraagentic.app.dto.CreateLabelRequest;
import com.jiraagentic.app.dto.LabelDto;
import com.jiraagentic.app.security.AuthSupport;
import com.jiraagentic.app.service.LabelService;
import lombok.RequiredArgsConstructor;
import org.springframework.http.ResponseEntity;
import org.springframework.security.core.Authentication;
import org.springframework.web.bind.annotation.*;

import java.util.List;

@RestController
@RequestMapping("/api/spaces/{spaceId}/labels")
@RequiredArgsConstructor
public class LabelController {

    private final LabelService labelService;

    @GetMapping
    public List<LabelDto> getBySpace(@PathVariable Long spaceId) {
        return labelService.findBySpace(spaceId);
    }

    @PostMapping
    public LabelDto create(@PathVariable Long spaceId, @RequestBody CreateLabelRequest request) {
        return labelService.create(spaceId, request.getName());
    }

    @DeleteMapping("/{labelId}")
    public ResponseEntity<Void> delete(
            @PathVariable Long spaceId,
            @PathVariable Long labelId,
            Authentication authentication) {
        labelService.delete(spaceId, labelId, AuthSupport.extractUid(authentication));
        return ResponseEntity.noContent().build();
    }
}
