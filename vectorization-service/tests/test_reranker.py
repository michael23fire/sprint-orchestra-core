"""Reranker tests — hermetic, no real model download.

CrossEncoderReranker is tested by monkeypatching sentence_transformers.CrossEncoder with a fake
that returns scripted scores, so the test verifies *this module's* sort/truncate/asyncio.to_thread
wiring, not sentence-transformers' own model quality (that's the library's job to test, not ours).
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from app.db.reranker import CrossEncoderReranker, NoopReranker
from app.models import SearchHit


def _hit(id_: str, score: float) -> SearchHit:
    return SearchHit(
        id=id_, chunk_type="issue", issue_id=1, issue_key="ATLAS-1", space_id=1,
        source_id=1, content=f"content for {id_}", score=score, retrievers=["vector"],
    )


class _FakeCrossEncoder:
    """Stands in for sentence_transformers.CrossEncoder: scores by a scripted lookup.

    Keyed on the full ``"{issue_key} {content}"`` text CrossEncoderReranker actually scores against
    (see app/db/reranker.py), not on ``content`` alone.
    """

    def __init__(self, scores_by_content: dict):
        self._scores = scores_by_content

    def predict(self, pairs):
        return [self._scores[content] for _, content in pairs]


@pytest.mark.asyncio
async def test_noop_reranker_passes_through_truncated():
    hits = [_hit("a", 0.9), _hit("b", 0.8), _hit("c", 0.7)]
    result = await NoopReranker().rerank("query", hits, top_n=2)
    assert [h.id for h in result] == ["a", "b"]


@pytest.mark.asyncio
async def test_cross_encoder_reranker_reorders_by_relevance_score():
    # First-stage (vector) ranking says a > b > c, but the cross-encoder (which actually looks at
    # query+content jointly) disagrees — this is the whole point of a second stage: it can promote
    # a lower-ranked-but-more-relevant chunk.
    hits = [_hit("a", 0.9), _hit("b", 0.8), _hit("c", 0.7)]
    fake_scores = {"ATLAS-1 content for a": 0.1, "ATLAS-1 content for b": 0.95, "ATLAS-1 content for c": 0.5}

    with patch("sentence_transformers.CrossEncoder", return_value=_FakeCrossEncoder(fake_scores)):
        reranker = CrossEncoderReranker("fake-model")
        result = await reranker.rerank("query", hits, top_n=2)

    assert [h.id for h in result] == ["b", "c"]
    assert result[0].score == pytest.approx(0.95)


@pytest.mark.asyncio
async def test_cross_encoder_reranker_handles_empty_hits():
    with patch("sentence_transformers.CrossEncoder", return_value=_FakeCrossEncoder({})):
        reranker = CrossEncoderReranker("fake-model")
        result = await reranker.rerank("query", [], top_n=5)
    assert result == []


@pytest.mark.asyncio
async def test_cross_encoder_reranker_drops_hits_below_score_threshold():
    hits = [_hit("a", 0.9), _hit("b", 0.8), _hit("c", 0.7)]
    fake_scores = {"ATLAS-1 content for a": 0.1, "ATLAS-1 content for b": 0.95, "ATLAS-1 content for c": 0.5}

    with patch("sentence_transformers.CrossEncoder", return_value=_FakeCrossEncoder(fake_scores)):
        reranker = CrossEncoderReranker("fake-model", score_threshold=0.4)
        result = await reranker.rerank("query", hits, top_n=5)

    # "a" scored 0.1 (below threshold) and is dropped even though top_n=5 leaves room for it.
    assert [h.id for h in result] == ["b", "c"]


@pytest.mark.asyncio
async def test_cross_encoder_reranker_threshold_can_return_empty():
    hits = [_hit("a", 0.9), _hit("b", 0.8)]
    fake_scores = {"ATLAS-1 content for a": 0.1, "ATLAS-1 content for b": 0.2}

    with patch("sentence_transformers.CrossEncoder", return_value=_FakeCrossEncoder(fake_scores)):
        reranker = CrossEncoderReranker("fake-model", score_threshold=0.5)
        result = await reranker.rerank("query", hits, top_n=5)

    assert result == []


@pytest.mark.asyncio
async def test_cross_encoder_reranker_scores_issue_key_alongside_content():
    # A chunk's `content` never contains its own issue key (see app/ingest/pipeline.py — key and
    # content are stored as separate fields), so an exact-identifier query like "ATLAS-42" only has
    # something to match against if the reranker prefixes the key onto the scored text itself.
    hit = SearchHit(
        id="x", chunk_type="issue", issue_id=1, issue_key="ATLAS-42", space_id=1,
        source_id=1, content="some unrelated prose", score=0.5, retrievers=["lexical"],
    )
    fake_scores = {"ATLAS-42 some unrelated prose": 0.9}

    with patch("sentence_transformers.CrossEncoder", return_value=_FakeCrossEncoder(fake_scores)):
        reranker = CrossEncoderReranker("fake-model")
        result = await reranker.rerank("ATLAS-42", [hit], top_n=5)

    assert result[0].score == pytest.approx(0.9)


@pytest.mark.asyncio
async def test_cross_encoder_reranker_keeps_page_provenance():
    hit = SearchHit(
        id="page", chunk_type="attachment", issue_id=1, issue_key="ATLAS-42", space_id=1,
        source_id=9, content="invoice total", score=0.5, retrievers=["vector"], page_number=3,
    )
    with patch("sentence_transformers.CrossEncoder", return_value=_FakeCrossEncoder({
        "ATLAS-42 invoice total": 0.9,
    })):
        result = await CrossEncoderReranker("fake-model").rerank("invoice", [hit], top_n=1)

    assert result[0].page_number == 3
