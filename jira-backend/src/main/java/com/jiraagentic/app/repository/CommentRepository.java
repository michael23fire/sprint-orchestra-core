package com.jiraagentic.app.repository;

import com.jiraagentic.app.entity.Comment;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Query;
import org.springframework.data.repository.query.Param;

import java.util.Collection;
import java.util.List;

public interface CommentRepository extends JpaRepository<Comment, Long> {

    List<Comment> findByIssueIdOrderByCreatedAtAsc(Long issueId);

    void deleteByIssue_Id(Long issueId);

    @Query(value = """
            WITH q AS (
                SELECT websearch_to_tsquery('english', regexp_replace(:query, '[-_/.~+]+', ' ', 'g')) AS tsq
            )
            SELECT c.id AS "id", c.issue_id AS "issueId", i.issue_key AS "issueKey", i.space_id AS "spaceId",
                   i.title AS "issueTitle",
                   ts_headline('english', c.content, q.tsq,
                       'MaxFragments=1,MaxWords=30,MinWords=10') AS "snippet",
                   ts_rank_cd(c.search_vector, q.tsq) AS "rank",
                   c.updated_at AS "updatedAt"
            FROM comments c
            JOIN issues i ON i.id = c.issue_id, q
            WHERE i.space_id IN (:spaceIds)
              AND c.search_vector @@ q.tsq
            ORDER BY "rank" DESC, c.updated_at DESC
            LIMIT :limit
            """, nativeQuery = true)
    List<CommentSearchRow> search(@Param("spaceIds") Collection<Long> spaceIds,
                                   @Param("query") String query,
                                   @Param("limit") int limit);

    /** Tier 3 (fallback): trigram word-similarity — see {@link IssueRepository#searchFuzzy}. */
    @Query(value = """
            SELECT c.id AS "id", c.issue_id AS "issueId", i.issue_key AS "issueKey", i.space_id AS "spaceId",
                   i.title AS "issueTitle",
                   substring(c.content for 200) AS "snippet",
                   word_similarity(lower(:query), lower(c.content)) AS "rank",
                   c.updated_at AS "updatedAt"
            FROM comments c
            JOIN issues i ON i.id = c.issue_id
            WHERE i.space_id IN (:spaceIds)
              AND lower(:query) <% lower(c.content)
            ORDER BY "rank" DESC, c.updated_at DESC
            LIMIT :limit
            """, nativeQuery = true)
    List<CommentSearchRow> searchFuzzy(@Param("spaceIds") Collection<Long> spaceIds,
                                        @Param("query") String query,
                                        @Param("limit") int limit);
}
