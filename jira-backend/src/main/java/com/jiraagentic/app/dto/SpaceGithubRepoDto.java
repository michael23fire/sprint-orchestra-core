package com.jiraagentic.app.dto;

import com.jiraagentic.app.entity.SpaceGithubRepo;
import lombok.Data;

import java.time.Instant;

@Data
public class SpaceGithubRepoDto {
    private Long id;
    private Long spaceId;
    private String owner;
    private String repo;
    private Instant createdAt;
    private Instant lastScannedAt;

    public static SpaceGithubRepoDto from(SpaceGithubRepo e) {
        SpaceGithubRepoDto dto = new SpaceGithubRepoDto();
        dto.setId(e.getId());
        if (e.getSpace() != null) dto.setSpaceId(e.getSpace().getId());
        dto.setOwner(e.getOwner());
        dto.setRepo(e.getRepo());
        dto.setCreatedAt(e.getCreatedAt());
        dto.setLastScannedAt(e.getLastScannedAt());
        return dto;
    }
}
