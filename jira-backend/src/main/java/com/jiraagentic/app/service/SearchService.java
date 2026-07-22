package com.jiraagentic.app.service;

import com.jiraagentic.app.dto.SearchResultDto;
import com.jiraagentic.app.repository.CommentRepository;
import com.jiraagentic.app.repository.CommentSearchRow;
import com.jiraagentic.app.repository.IssueRepository;
import com.jiraagentic.app.repository.IssueSearchRow;
import com.jiraagentic.app.repository.SpaceMemberRepository;
import lombok.RequiredArgsConstructor;
import org.springframework.stereotype.Service;

import java.util.ArrayList;
import java.util.Comparator;
import java.util.HashSet;
import java.util.List;
import java.util.Set;

/**
 * Tiered hybrid search — deliberately NOT a linear-weighted blend of scores from different
 * sources (trigram similarity, ts_rank_cd, and an exact-match flag live on unrelated scales,
 * so a "combined" numeric score would just be an unexplainable magic-weight formula). Instead:
 * <ol>
 *   <li>Exact/prefix issue-key match — always wins, always shown first.</li>
 *   <li>Full-text search (stemmed word/phrase matching) — the main signal.</li>
 *   <li>pg_trgm fuzzy fallback (typo tolerance, partial words) — only consulted when the two
 *       tiers above don't fill a page of results, so it never buries a solid FTS hit under noisy
 *       fuzzy matches.</li>
 * </ol>
 * Each tier's own rank is offset into a disjoint numeric band so the final sort never lets a
 * lower tier outrank a higher one, while still ranking sensibly within each tier.
 */
@Service
@RequiredArgsConstructor
public class SearchService {

    private static final int DEFAULT_LIMIT = 20;
    private static final int MAX_LIMIT = 50;

    /** Trigrams need at least 3 characters to mean anything; shorter queries skip tier 3. */
    private static final int FUZZY_MIN_QUERY_LENGTH = 3;

    private static final double TIER_EXACT_KEY = 10_000;
    private static final double TIER_FULL_TEXT = 1_000;
    private static final double TIER_FUZZY = 0;

    private final SpaceMemberRepository spaceMemberRepository;
    private final IssueRepository issueRepository;
    private final CommentRepository commentRepository;

    public List<SearchResultDto> search(Long userId, String query, Integer limit) {
        if (query == null || query.isBlank()) {
            return List.of();
        }
        int cappedLimit = clampLimit(limit);
        List<Long> spaceIds = spaceMemberRepository.findActiveSpaceIdsByUserId(userId);
        if (spaceIds.isEmpty()) {
            return List.of();
        }

        String trimmedQuery = query.trim();
        List<SearchResultDto> results = new ArrayList<>();
        Set<Long> seenIssueIds = new HashSet<>();
        Set<Long> seenCommentIds = new HashSet<>();

        for (IssueSearchRow row : issueRepository.searchByKeyPrefix(spaceIds, trimmedQuery, cappedLimit)) {
            if (seenIssueIds.add(row.getId())) {
                results.add(toIssueDto("EXACT_KEY", row, TIER_EXACT_KEY));
            }
        }

        for (IssueSearchRow row : issueRepository.search(spaceIds, trimmedQuery, cappedLimit)) {
            if (seenIssueIds.add(row.getId())) {
                results.add(toIssueDto("FULL_TEXT", row, TIER_FULL_TEXT));
            }
        }
        for (CommentSearchRow row : commentRepository.search(spaceIds, trimmedQuery, cappedLimit)) {
            if (seenCommentIds.add(row.getId())) {
                results.add(toCommentDto("FULL_TEXT", row, TIER_FULL_TEXT));
            }
        }

        if (results.size() < cappedLimit && trimmedQuery.length() >= FUZZY_MIN_QUERY_LENGTH) {
            for (IssueSearchRow row : issueRepository.searchFuzzy(spaceIds, trimmedQuery, cappedLimit)) {
                if (seenIssueIds.add(row.getId())) {
                    results.add(toIssueDto("FUZZY", row, TIER_FUZZY));
                }
            }
            for (CommentSearchRow row : commentRepository.searchFuzzy(spaceIds, trimmedQuery, cappedLimit)) {
                if (seenCommentIds.add(row.getId())) {
                    results.add(toCommentDto("FUZZY", row, TIER_FUZZY));
                }
            }
        }

        results.sort(Comparator.comparingDouble(SearchResultDto::getRank).reversed());
        return results.size() > cappedLimit ? results.subList(0, cappedLimit) : results;
    }

    private SearchResultDto toIssueDto(String matchType, IssueSearchRow row, double tierBase) {
        return new SearchResultDto("ISSUE", matchType, row.getSpaceId(), row.getId(), row.getIssueKey(),
                row.getTitle(), row.getSnippet(), tierBase + row.getRank(), row.getUpdatedAt());
    }

    private SearchResultDto toCommentDto(String matchType, CommentSearchRow row, double tierBase) {
        return new SearchResultDto("COMMENT", matchType, row.getSpaceId(), row.getIssueId(), row.getIssueKey(),
                row.getIssueTitle(), row.getSnippet(), tierBase + row.getRank(), row.getUpdatedAt());
    }

    private int clampLimit(Integer limit) {
        int value = limit == null ? DEFAULT_LIMIT : limit;
        return Math.min(Math.max(value, 1), MAX_LIMIT);
    }
}
