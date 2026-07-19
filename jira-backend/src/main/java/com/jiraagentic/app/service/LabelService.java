package com.jiraagentic.app.service;

import com.jiraagentic.app.dto.LabelDto;
import com.jiraagentic.app.entity.Issue;
import com.jiraagentic.app.entity.Label;
import com.jiraagentic.app.entity.Space;
import com.jiraagentic.app.repository.IssueRepository;
import com.jiraagentic.app.repository.LabelRepository;
import com.jiraagentic.app.repository.SpaceRepository;
import lombok.RequiredArgsConstructor;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.text.Normalizer;
import java.time.Instant;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Locale;
import java.util.Objects;
import java.util.Set;
import java.util.stream.Collectors;

@Service
@RequiredArgsConstructor
public class LabelService {

    private static final int MAX_NAME_LENGTH = 50;
    private static final Set<String> RESERVED = Set.of("bug", "story", "epic");

    private final LabelRepository labelRepository;
    private final IssueRepository issueRepository;
    private final SpaceRepository spaceRepository;
    private final IssueHistoryService issueHistoryService;
    private final ActiveSpaceGuard activeSpaceGuard;

    public List<LabelDto> findBySpace(Long spaceId) {
        activeSpaceGuard.requireActive(spaceId);
        return labelRepository.findBySpace_IdAndDeletedAtIsNullOrderByNameAsc(spaceId).stream()
                .filter(label -> !"flagged".equals(label.getNormalizedName()))
                .map(LabelDto::from)
                .collect(Collectors.toList());
    }

    @Transactional
    public LabelDto create(Long spaceId, String rawName) {
        String normalized = normalize(rawName);
        if ("flagged".equals(normalized) || RESERVED.contains(normalized)) {
            throw new IllegalArgumentException(rawName.trim() + " is a reserved label");
        }
        Space space = spaceRepository.findActiveByIdForUpdate(spaceId)
                .orElseThrow(() -> new IllegalArgumentException("Space not found or deleted: " + spaceId));
        String displayName = displayName(rawName);
        Label label = labelRepository.findBySpace_IdAndNormalizedName(spaceId, normalized)
                .map(existing -> reactivate(existing, displayName))
                .orElseGet(() -> createLabel(space, displayName, normalized));
        return LabelDto.from(label);
    }

    /**
     * Resolves names against a space-local, case-insensitive catalog and creates
     * missing entries. Locking the space serializes concurrent create-on-the-fly
     * requests so the database unique constraint never leaks as a user error.
     */
    @Transactional
    public Set<Label> resolveLabels(Long spaceId, List<String> rawNames) {
        Space space = spaceRepository.findActiveByIdForUpdate(spaceId)
                .orElseThrow(() -> new IllegalArgumentException("Space not found or deleted: " + spaceId));
        LinkedHashSet<Label> result = new LinkedHashSet<>();
        if (rawNames == null) return result;

        for (String rawName : rawNames) {
            if (rawName == null || rawName.isBlank()) continue;
            String normalized = normalize(rawName);
            if (RESERVED.contains(normalized)) continue;
            String displayName = displayName(rawName);
            Label label = labelRepository.findBySpace_IdAndNormalizedName(spaceId, normalized)
                    .orElse(null);
            // A stale issue update must not silently resurrect a label that was
            // deleted at space level. Only the explicit create endpoint may do so.
            if (label != null && label.getDeletedAt() != null) continue;
            if (label == null) label = createLabel(space, displayName, normalized);
            result.add(label);
        }
        return result;
    }

    @Transactional
    public void delete(Long spaceId, Long labelId, Long actorUserId) {
        activeSpaceGuard.requireActive(spaceId);
        Label label = labelRepository.findById(labelId)
                .orElseThrow(() -> new IllegalArgumentException("Label not found: " + labelId));
        if (!label.getSpace().getId().equals(spaceId) || label.getDeletedAt() != null) {
            throw new IllegalArgumentException("Label not found in space: " + labelId);
        }

        List<Issue> issues = issueRepository.findDistinctByLabels_Id(labelId);
        for (Issue issue : issues) {
            String before = issue.getLabels().stream()
                    .map(Label::getName)
                    .collect(Collectors.joining(", "));
            issue.getLabels().removeIf(item -> Objects.equals(item.getId(), labelId));
            String after = issue.getLabels().stream()
                    .map(Label::getName)
                    .collect(Collectors.joining(", "));
            issueHistoryService.recordFieldChange(issue, actorUserId, "labels", before, after);
        }
        issueRepository.saveAll(issues);
        label.setDeletedAt(Instant.now());
        labelRepository.save(label);
    }

    private Label reactivate(Label label, String displayName) {
        if (label.getDeletedAt() != null) {
            label.setDeletedAt(null);
            label.setName(displayName);
            return labelRepository.save(label);
        }
        return label;
    }

    private Label createLabel(Space space, String displayName, String normalized) {
        Label label = new Label();
        label.setSpace(space);
        label.setName(displayName);
        label.setNormalizedName(normalized);
        return labelRepository.save(label);
    }

    private static String normalize(String rawName) {
        if (rawName == null) {
            throw new IllegalArgumentException("Label name is required");
        }
        String compact = Normalizer.normalize(rawName, Normalizer.Form.NFKC)
                .trim()
                .replaceAll("\\s+", " ");
        if (compact.length() > MAX_NAME_LENGTH) {
            throw new IllegalArgumentException("Label name must be at most " + MAX_NAME_LENGTH + " characters");
        }
        String normalized = compact.toLowerCase(Locale.ROOT);
        if (normalized.isBlank()) {
            throw new IllegalArgumentException("Label name is required");
        }
        return normalized;
    }

    private static String displayName(String rawName) {
        String compact = Normalizer.normalize(rawName, Normalizer.Form.NFKC)
                .trim()
                .replaceAll("\\s+", " ");
        boolean hasLetter = compact.codePoints().anyMatch(Character::isLetter);
        boolean allLettersLower = compact.codePoints()
                .filter(Character::isLetter)
                .allMatch(Character::isLowerCase);
        if (!hasLetter || !allLettersLower) return compact;
        for (int offset = 0; offset < compact.length();) {
            int codePoint = compact.codePointAt(offset);
            if (Character.isLetter(codePoint)) {
                int length = Character.charCount(codePoint);
                return compact.substring(0, offset)
                        + new String(Character.toChars(Character.toUpperCase(codePoint)))
                        + compact.substring(offset + length);
            }
            offset += Character.charCount(codePoint);
        }
        return compact;
    }
}
