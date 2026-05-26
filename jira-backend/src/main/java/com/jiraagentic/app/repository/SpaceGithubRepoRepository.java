package com.jiraagentic.app.repository;

import com.jiraagentic.app.entity.SpaceGithubRepo;
import org.springframework.data.jpa.repository.JpaRepository;

import java.util.List;
import java.util.Optional;

public interface SpaceGithubRepoRepository extends JpaRepository<SpaceGithubRepo, Long> {
    List<SpaceGithubRepo> findBySpaceIdOrderByCreatedAtAsc(Long spaceId);

    Optional<SpaceGithubRepo> findBySpaceIdAndOwnerAndRepo(Long spaceId, String owner, String repo);
}
