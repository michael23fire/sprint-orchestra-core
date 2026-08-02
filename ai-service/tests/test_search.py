"""Tests for app/search/service.py's chunk-to-issue dedup — no network, pure function.

Note: `dedupe_by_issue` defaults to both `exclude_chunk_types={"attachment"}` and
`min_score=DEFAULT_MIN_SCORE` (0.7). Tests that are specifically about ranking/limit/dedup mechanics
(not about the attachment or score-floor behavior) pass `min_score=None` to isolate what they're
actually testing, rather than needing every fixture score to clear an unrelated threshold.
"""
from app.agent.retrieval_tool import RetrievedChunk
from app.search.service import DEFAULT_MIN_SCORE, RERANKED_MIN_SCORE, dedupe_by_issue


def _chunk(issue_id, score, chunk_id=None, chunk_type="issue"):
    return RetrievedChunk(
        id=chunk_id or f"chunk-{issue_id}-{score}",
        chunk_type=chunk_type,
        issue_id=issue_id,
        issue_key=f"SCRUM-{issue_id}",
        source_id=issue_id,
        content=f"content for issue {issue_id}",
        score=score,
        retrievers=["vector"],
    )


def test_dedupe_keeps_best_scoring_chunk_per_issue():
    chunks = [_chunk(1, 0.4), _chunk(1, 0.9), _chunk(1, 0.6)]

    result = dedupe_by_issue(chunks, limit=10, min_score=None)

    assert len(result) == 1
    assert result[0].score == 0.9


def test_dedupe_ranks_distinct_issues_by_score_descending():
    chunks = [_chunk(1, 0.3), _chunk(2, 0.9), _chunk(3, 0.6)]

    result = dedupe_by_issue(chunks, limit=10, min_score=None)

    assert [c.issue_id for c in result] == [2, 3, 1]


def test_dedupe_respects_limit_after_collapsing_duplicates():
    chunks = [_chunk(1, 0.9), _chunk(1, 0.8), _chunk(2, 0.7), _chunk(3, 0.6)]

    result = dedupe_by_issue(chunks, limit=2, min_score=None)

    assert [c.issue_id for c in result] == [1, 2]


def test_dedupe_handles_empty_input():
    assert dedupe_by_issue([], limit=10) == []


def test_dedupe_excludes_attachment_chunks_by_default():
    # Found live against real ingested data (see module docstring): OCR'd screenshot chunks are
    # frequently near-templated boilerplate that outranks genuinely relevant issue/comment content.
    chunks = [_chunk(1, 0.9, chunk_type="attachment"), _chunk(2, 0.8, chunk_type="issue")]

    result = dedupe_by_issue(chunks, limit=10, min_score=None)

    assert [c.issue_id for c in result] == [2]


def test_dedupe_still_ranks_an_issue_by_its_best_non_attachment_chunk():
    chunks = [
        _chunk(1, 0.99, chunk_id="a", chunk_type="attachment"),
        _chunk(1, 0.8, chunk_id="b", chunk_type="comment"),
    ]

    result = dedupe_by_issue(chunks, limit=10, min_score=None)

    assert len(result) == 1
    assert result[0].id == "b"


def test_dedupe_exclude_chunk_types_can_be_overridden_to_none():
    chunks = [_chunk(1, 0.9, chunk_type="attachment")]

    result = dedupe_by_issue(chunks, limit=10, exclude_chunk_types=None, min_score=None)

    assert len(result) == 1


def test_dedupe_drops_results_below_the_score_floor_instead_of_always_returning_top_n():
    # The exact ask this answers: don't hand back "the least-bad of a bad bunch" as if it were a
    # confident match — if nothing clears the floor, the caller should see nothing, not a fabricated
    # top-N.
    chunks = [_chunk(1, 0.5), _chunk(2, 0.4), _chunk(3, 0.3)]

    result = dedupe_by_issue(chunks, limit=10, min_score=0.7)

    assert result == []


def test_dedupe_keeps_only_results_at_or_above_the_score_floor():
    chunks = [_chunk(1, 0.9), _chunk(2, 0.71), _chunk(3, 0.69)]

    result = dedupe_by_issue(chunks, limit=10, min_score=0.7)

    assert [c.issue_id for c in result] == [1, 2]


def test_dedupe_default_min_score_is_applied_when_not_overridden():
    chunks = [_chunk(1, DEFAULT_MIN_SCORE - 0.01), _chunk(2, DEFAULT_MIN_SCORE)]

    result = dedupe_by_issue(chunks, limit=10)

    assert [c.issue_id for c in result] == [2]


def test_reranked_min_score_works_on_the_cross_encoder_scale_not_cosine_scale():
    # Real scores observed live against a reranked deployment (see module docstring): genuine matches
    # scored 5.39/1.71/0.47, a totally unrelated query scored every candidate around -11.4. Regression
    # test that RERANKED_MIN_SCORE (0.0) actually separates that real distribution, not just a
    # symbolic placeholder value.
    chunks = [
        _chunk(1, 5.39), _chunk(2, 1.71), _chunk(3, 0.47),  # genuine matches
        _chunk(4, -4.65), _chunk(5, -11.4), _chunk(6, -11.43),  # irrelevant
    ]

    result = dedupe_by_issue(chunks, limit=10, min_score=RERANKED_MIN_SCORE)

    assert {c.issue_id for c in result} == {1, 2, 3}
