package com.jiraagentic.app.repository;

import com.jiraagentic.app.entity.Issue;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Query;
import org.springframework.data.repository.query.Param;

import java.util.Collection;
import java.util.List;
import java.util.Optional;

public interface IssueRepository extends JpaRepository<Issue, Long> {

    @Query(value = """
            WITH q AS (
                SELECT websearch_to_tsquery('english', regexp_replace(:query, '[-_/.~+]+', ' ', 'g')) AS tsq
            )
            SELECT i.id AS "id", i.issue_key AS "issueKey", i.space_id AS "spaceId", i.title AS "title",
                   ts_headline('english', coalesce(i.description, ''), q.tsq,
                       'MaxFragments=1,MaxWords=30,MinWords=10') AS "snippet",
                   ts_rank_cd(i.search_vector, q.tsq) AS "rank",
                   i.updated_at AS "updatedAt"
            FROM issues i, q
            WHERE i.space_id IN (:spaceIds)
              AND i.search_vector @@ q.tsq
            ORDER BY "rank" DESC, i.updated_at DESC
            LIMIT :limit
            """, nativeQuery = true)
    List<IssueSearchRow> search(@Param("spaceIds") Collection<Long> spaceIds,
                                 @Param("query") String query,
                                 @Param("limit") int limit);

    /** Tier 1: exact/case-insensitive-prefix issue-key lookup ("SP1-3" -&gt; SP1-3, SP1-30..39). */
    @Query(value = """
            SELECT i.id AS "id", i.issue_key AS "issueKey", i.space_id AS "spaceId", i.title AS "title",
                   '' AS "snippet",
                   CASE WHEN lower(i.issue_key) = lower(:query) THEN 1.0 ELSE 0.9 END AS "rank",
                   i.updated_at AS "updatedAt"
            FROM issues i
            WHERE i.space_id IN (:spaceIds)
              AND lower(i.issue_key) LIKE lower(:query) || '%'
            ORDER BY (lower(i.issue_key) = lower(:query)) DESC, i.issue_key ASC
            LIMIT :limit
            """, nativeQuery = true)
    List<IssueSearchRow> searchByKeyPrefix(@Param("spaceIds") Collection<Long> spaceIds,
                                            @Param("query") String query,
                                            @Param("limit") int limit);

    /**
     * Tier 3 (fallback): trigram word-similarity over title/description — typo tolerance and
     * partial-word matches that full-text search's stem/lexeme matching can't catch. Uses
     * word_similarity ("&lt;%") rather than similarity ("%"): the latter compares whole strings,
     * so a short query gets diluted against a long title/description and never clears the
     * threshold even when the word is present verbatim.
     */
    @Query(value = """
            SELECT i.id AS "id", i.issue_key AS "issueKey", i.space_id AS "spaceId", i.title AS "title",
                   CASE
                       WHEN word_similarity(lower(:query), lower(i.title))
                            >= word_similarity(lower(:query), lower(coalesce(i.description, '')))
                       THEN i.title
                       ELSE substring(coalesce(i.description, '') for 200)
                   END AS "snippet",
                   GREATEST(
                       word_similarity(lower(:query), lower(i.title)),
                       word_similarity(lower(:query), lower(coalesce(i.description, '')))
                   ) AS "rank",
                   i.updated_at AS "updatedAt"
            FROM issues i
            WHERE i.space_id IN (:spaceIds)
              AND (lower(:query) <% lower(i.title) OR lower(:query) <% lower(coalesce(i.description, '')))
            ORDER BY "rank" DESC, i.updated_at DESC
            LIMIT :limit
            """, nativeQuery = true)
    List<IssueSearchRow> searchFuzzy(@Param("spaceIds") Collection<Long> spaceIds,
                                      @Param("query") String query,
                                      @Param("limit") int limit);

    Optional<Issue> findByIssueKey(String issueKey);

    List<Issue> findBySpaceIdOrderByIssueOrderAsc(Long spaceId);

    List<Issue> findBySpaceIdAndSprintIdOrderByIssueOrderAsc(Long spaceId, Long sprintId);

    List<Issue> findBySpaceIdAndSprintIsNullOrderByIssueOrderAsc(Long spaceId);

    List<Issue> findByParentId(Long parentId);

    List<Issue> findByParent_IdIn(Collection<Long> parentIds);

    List<Issue> findBySprint_Id(Long sprintId);

    List<Issue> findDistinctByLabels_Id(Long labelId);

    boolean existsByIssueKey(String issueKey);
}
