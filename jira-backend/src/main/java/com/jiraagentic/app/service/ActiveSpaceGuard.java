package com.jiraagentic.app.service;

import com.jiraagentic.app.entity.Space;
import com.jiraagentic.app.repository.SpaceRepository;
import lombok.RequiredArgsConstructor;
import org.springframework.stereotype.Service;

@Service
@RequiredArgsConstructor
public class ActiveSpaceGuard {

    private final SpaceRepository spaceRepository;

    public Space requireActive(Long spaceId) {
        return spaceRepository.findByIdAndDeletedAtIsNull(spaceId)
                .orElseThrow(() -> new RuntimeException("Space not found: " + spaceId));
    }

    /**
     * Use when a {@link Space} is already loaded (e.g. from {@code issue.getSpace()}) to avoid an extra fetch.
     */
    public void requireActive(Space space) {
        if (space == null || space.getDeletedAt() != null) {
            throw new RuntimeException("Space not found: " + (space != null ? space.getId() : ""));
        }
    }
}
